"""campaign_delete_lib.py -- Safely delete a draft campaign and all its contacts.

Algorithm
---------
1. Open a Firestore transaction:
     - Read the campaign doc.
     - Verify it exists and status == "draft" (raises if not).
     - Write status = "deleting" atomically inside the transaction.
   The transaction commits before any destructive work begins.  Any concurrent
   request that reads the doc after commit will see "deleting" and bail out.

2. Batch-delete all campaign_contacts subcollection documents in chunks of 400.

3. Delete the campaign document itself.

4. Return a summary dict.

Guard against stale job replays: if status is already "deleting" when the job
worker calls run_campaign_delete(), we proceed with the deletion (idempotent).
If status is anything else (e.g. someone manually reset it to "draft" between
the transaction and the job starting), we abort to avoid data loss.
"""
from __future__ import annotations

CAMPAIGNS_COLLECTION  = "campaigns"
CAMPAIGN_CONTACTS_SUB = "campaign_contacts"
CAMPAIGN_LEADS_SUB    = "campaign_leads"
BATCH_SIZE = 400


def _campaign_status(value) -> str:
    status = str(value or "draft").strip().lower()
    return status


def run_campaign_delete(db, campaign_id: str) -> dict:
    """Delete a campaign that is in 'draft' or 'deleting' status.

    Called by the campaign-delete job worker.  The DELETE API route should
    already have flipped status to 'deleting' via a transaction before
    enqueueing this job — but we handle the 'draft' case too for safety.

    Returns:
        dict with campaign_id, contacts_deleted.
    Raises:
        ValueError if campaign not found or status is not draft/deleting.
    """
    if not campaign_id:
        raise ValueError("campaign_id is required")

    camp_ref = db.collection(CAMPAIGNS_COLLECTION).document(campaign_id)

    # ── 1. Atomic guard — flip to "deleting" if still in draft ───────────────
    from google.cloud import firestore as _fs

    @_fs.transactional
    def _claim(tx, ref):
        snap = ref.get(transaction=tx)
        if not snap.exists:
            raise ValueError(f"Campaign '{campaign_id}' not found")
        status = _campaign_status((snap.to_dict() or {}).get("status", ""))
        if status not in ("draft", "canceled", "deleting"):
            raise ValueError(
                f"Campaign '{campaign_id}' has status '{status}' — "
                "only draft campaigns can be deleted"
            )
        if status in ("draft", "canceled"):
            tx.update(ref, {"status": "deleting"})

    _claim(db.transaction(), camp_ref)
    print(f"[campaign-delete] status locked to 'deleting' for '{campaign_id}'",
          flush=True)

    # ── 2. Batch-delete subcollections ────────────────────────────────────────
    def _delete_subcollection(col_ref, label: str) -> int:
        count = 0
        while True:
            docs = list(col_ref.limit(BATCH_SIZE).stream())
            if not docs:
                break
            batch = db.batch()
            for doc in docs:
                batch.delete(doc.reference)
            batch.commit()
            count += len(docs)
            print(f"[campaign-delete]   deleted {count} {label} so far…", flush=True)
        print(f"[campaign-delete] {count} {label} deleted", flush=True)
        return count

    contacts_deleted = _delete_subcollection(
        camp_ref.collection(CAMPAIGN_CONTACTS_SUB), "contacts"
    )
    leads_deleted = _delete_subcollection(
        camp_ref.collection(CAMPAIGN_LEADS_SUB), "leads"
    )

    # ── 3. Delete campaign document ───────────────────────────────────────────
    camp_ref.delete()
    print(f"[campaign-delete] campaign '{campaign_id}' deleted", flush=True)

    return {
        "campaign_id":      campaign_id,
        "contacts_deleted": contacts_deleted,
        "leads_deleted":    leads_deleted,
    }
