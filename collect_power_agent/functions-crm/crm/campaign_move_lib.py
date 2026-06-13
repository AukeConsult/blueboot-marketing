"""crm/campaign_move_lib.py — Move contacts between campaigns.

Invariant: a lead and all its contacts always live in the same campaign.
When any subset of contacts is requested for a move, the operation is
automatically expanded to include ALL contacts that share the same lead_id,
plus the lead doc itself.

Steps:
  1. Pre-transaction: resolve lead_ids from the requested doc_ids.
  2. Pre-transaction: expand to every contact doc in the source that belongs
     to any of those lead_ids (query outside the transaction).
  3. Atomic transaction:
       • Copy all expanded contacts to target, delete from source.
       • Move each lead doc: set in target, delete from source.
       • Update contact_count on both campaign docs.

Doc IDs are preserved across campaigns (subcollection isolation).

Params (passed via job body):
    src_campaign_id    str        Source campaign ID
    doc_ids            list[str]  Any contact doc IDs in the source (expanded to full leads)
    target_campaign_id str        Existing target campaign ID  (one of these two)
    new_campaign_name  str        Name for a brand-new campaign to create
    user               str        Email of the user who triggered the move
"""
from __future__ import annotations

from datetime import datetime, timezone


def run_campaign_move(
    db,
    src_campaign_id: str,
    doc_ids: list,
    target_campaign_id: str = "",
    new_campaign_name: str = "",
    user: str = "api",
) -> dict:
    from google.cloud import firestore as _gfs
    from google.cloud.firestore_v1.base_query import FieldFilter

    now = datetime.now(timezone.utc).isoformat()

    print(
        f"[campaign-move] src={src_campaign_id} requested_doc_ids={len(doc_ids)} "
        f"target={target_campaign_id!r} new_name={new_campaign_name!r}",
        flush=True,
    )

    if not doc_ids:
        raise ValueError("doc_ids must be a non-empty list")
    if not target_campaign_id and not new_campaign_name:
        raise ValueError("Provide target_campaign_id or new_campaign_name")
    if target_campaign_id and new_campaign_name:
        raise ValueError("Provide target_campaign_id OR new_campaign_name, not both")

    src_ref       = db.collection("campaigns").document(src_campaign_id)
    src_col       = src_ref.collection("campaign_contacts")
    src_leads_col = src_ref.collection("campaign_leads")

    # ── Resolve target ref ────────────────────────────────────────────────────
    is_new_campaign: bool
    if target_campaign_id:
        tgt_ref = db.collection("campaigns").document(target_campaign_id)
        is_new_campaign = False
    else:
        import re as _re
        safe_id = _re.sub(r"[^a-z0-9_-]", "_", new_campaign_name.lower())[:80] or "campaign"
        tgt_ref = db.collection("campaigns").document(safe_id)
        if tgt_ref.get().exists:
            safe_id = f"{safe_id}_{int(datetime.now(timezone.utc).timestamp())}"
            tgt_ref = db.collection("campaigns").document(safe_id)
        target_campaign_id = safe_id
        is_new_campaign = True

    tgt_col       = tgt_ref.collection("campaign_contacts")
    tgt_leads_col = tgt_ref.collection("campaign_leads")

    # ── Pre-transaction: expand doc_ids to full leads ─────────────────────────
    # Step 1: read the requested contacts to collect their lead_ids
    lead_ids: set[str] = set()
    for did in doc_ids:
        snap = src_col.document(did).get()
        if snap.exists:
            lid = ((snap.to_dict() or {}).get("lead_id") or "").strip()
            if lid:
                lead_ids.add(lid)

    # Step 2: query ALL contacts in source for those lead_ids
    # (queries cannot run inside a Firestore transaction)
    all_doc_ids: set[str] = set(doc_ids)  # seed with the originals (covers no-lead_id contacts)
    for lid in lead_ids:
        for snap in src_col.where(filter=FieldFilter("lead_id", "==", lid)).stream():
            all_doc_ids.add(snap.id)

    n = len(all_doc_ids)

    print(
        f"[campaign-move] expanded to {n} contacts across {len(lead_ids)} lead(s): {sorted(lead_ids)}",
        flush=True,
    )

    # MOVED history entry
    move_entry = {
        "date": now,
        "user": user,
        "text": f"Moved from campaign {src_campaign_id}",
        "type": "MOVED",
    }

    # ── Atomic transaction ────────────────────────────────────────────────────
    @_gfs.transactional
    def _move(transaction):
        # ── Phase 1: all reads ────────────────────────────────────────────────
        src_snap = src_ref.get(transaction=transaction)
        if not src_snap.exists:
            raise ValueError(f"Source campaign '{src_campaign_id}' not found")

        if is_new_campaign:
            if tgt_ref.get(transaction=transaction).exists:
                raise ValueError(
                    f"Campaign '{target_campaign_id}' was created concurrently — retry"
                )
        else:
            if not tgt_ref.get(transaction=transaction).exists:
                raise ValueError(f"Target campaign '{target_campaign_id}' not found")

        # Read every contact to move
        contact_snaps: dict = {}
        for did in all_doc_ids:
            snap = src_col.document(did).get(transaction=transaction)
            if not snap.exists:
                # Contact may have been deleted after the pre-query — skip gracefully
                print(f"[campaign-move] WARNING: contact {did} vanished — skipping", flush=True)
                continue
            contact_snaps[did] = snap

        # Read source and target lead docs
        src_lead_snaps: dict = {}
        tgt_lead_snaps: dict = {}
        for lid in lead_ids:
            src_lead_snaps[lid] = src_leads_col.document(lid).get(transaction=transaction)
            tgt_lead_snaps[lid] = tgt_leads_col.document(lid).get(transaction=transaction)

        actual_n = len(contact_snaps)

        # ── Phase 2: all writes ───────────────────────────────────────────────
        # Target campaign
        if is_new_campaign:
            transaction.set(tgt_ref, {
                "campaign_id":   target_campaign_id,
                "name":          new_campaign_name,
                "owner":         user,
                "status":        "draft",
                "created_at":    now,
                "updated_at":    now,
                "contact_count": actual_n,
                "comment":       f"Auto-created by move from campaign {src_campaign_id} by {user}",
            })
        else:
            transaction.update(tgt_ref, {
                "contact_count": _gfs.Increment(actual_n),
                "updated_at":    now,
            })

        # Source campaign
        transaction.update(src_ref, {
            "contact_count": _gfs.Increment(-actual_n),
            "updated_at":    now,
        })

        # Copy contacts to target, delete from source
        for did, snap in contact_snaps.items():
            data = snap.to_dict() or {}
            history = data.get("comment_history") or []
            if not isinstance(history, list):
                history = []
            data["comment_history"] = history + [move_entry]
            transaction.set(tgt_col.document(did), data)
            transaction.delete(src_col.document(did))

        # Move lead docs: delete from source, set in target
        for lid in lead_ids:
            src_lead_snap = src_lead_snaps[lid]
            tgt_lead_snap = tgt_lead_snaps[lid]

            if src_lead_snap.exists:
                transaction.delete(src_leads_col.document(lid))

            if not tgt_lead_snap.exists:
                lead_data = (src_lead_snap.to_dict() if src_lead_snap.exists else {}).copy()
                lead_data["campaign_id"] = target_campaign_id
                lead_data["synced_at"]   = now
                transaction.set(tgt_leads_col.document(lid), lead_data)
            # If it already exists in target, leave it — counts will be slightly
            # off until the next campaign_leads_populate run, but data is intact.

        return actual_n, len(lead_ids)

    moved, leads_moved = _move(db.transaction())

    print(
        f"[campaign-move] committed — {moved} contacts, {leads_moved} lead(s) "
        f"moved from '{src_campaign_id}' to '{target_campaign_id}'"
        + (" (new campaign)" if is_new_campaign else ""),
        flush=True,
    )

    return {
        "moved":              moved,
        "leads_moved":        leads_moved,
        "errors":             [],
        "rolled_back":        False,
        "src_campaign_id":    src_campaign_id,
        "target_campaign_id": target_campaign_id,
    }
