"""crm/campaign_move_lib.py — Move contacts between campaigns.

All Firestore reads and writes execute in a single atomic transaction.
If any contact is not found the entire transaction is aborted — no partial moves.

Doc IDs are preserved (campaigns/A/campaign_contacts/{id} →
campaigns/B/campaign_contacts/{id}).  Because campaign_contacts is a
subcollection the same doc ID can coexist under different campaign parents
without violating collection-group uniqueness.

Params (passed via job body):
    src_campaign_id    str        Source campaign ID
    doc_ids            list[str]  Contact doc IDs to move
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

    now = datetime.now(timezone.utc).isoformat()

    print(
        f"[campaign-move] src={src_campaign_id} doc_ids={doc_ids} "
        f"target={target_campaign_id!r} new_name={new_campaign_name!r}",
        flush=True,
    )

    if not doc_ids:
        raise ValueError("doc_ids must be a non-empty list")
    if not target_campaign_id and not new_campaign_name:
        raise ValueError("Provide target_campaign_id or new_campaign_name")
    if target_campaign_id and new_campaign_name:
        raise ValueError("Provide target_campaign_id OR new_campaign_name, not both")

    src_ref = db.collection("campaigns").document(src_campaign_id)
    src_col = src_ref.collection("campaign_contacts")

    # ── Resolve target ref before opening the transaction ────────────────────
    # All variables captured by _move() are read-only inside the transaction
    # so retries are safe.
    is_new_campaign: bool
    if target_campaign_id:
        tgt_ref = db.collection("campaigns").document(target_campaign_id)
        is_new_campaign = False
    else:
        import re as _re
        safe_id = _re.sub(r"[^a-z0-9_-]", "_", new_campaign_name.lower())[:80] or "campaign"
        tgt_ref = db.collection("campaigns").document(safe_id)
        # Quick pre-check: if the name is already taken append a timestamp so the
        # transaction can just do a blind set without a collision guard read.
        if tgt_ref.get().exists:
            safe_id = f"{safe_id}_{int(datetime.now(timezone.utc).timestamp())}"
            tgt_ref = db.collection("campaigns").document(safe_id)
        target_campaign_id = safe_id
        is_new_campaign = True

    tgt_col = tgt_ref.collection("campaign_contacts")
    n = len(doc_ids)

    # MOVED history entry (same value for every contact; computed once)
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
            raise ValueError(f"Source campaign \'{src_campaign_id}\' not found")

        if is_new_campaign:
            # Verify no race-condition creation since the pre-check above
            if tgt_ref.get(transaction=transaction).exists:
                raise ValueError(
                    f"Campaign \'{target_campaign_id}\' was created concurrently — retry"
                )
        else:
            if not tgt_ref.get(transaction=transaction).exists:
                raise ValueError(f"Target campaign \'{target_campaign_id}\' not found")

        # Read every source contact — abort whole transaction if any is missing
        src_snaps: dict = {}
        for did in doc_ids:
            snap = src_col.document(did).get(transaction=transaction)
            if not snap.exists:
                raise ValueError(
                    f"Contact not found: {src_campaign_id}/campaign_contacts/{did}"
                )
            src_snaps[did] = snap

        # ── Phase 2: all writes ───────────────────────────────────────────────
        # Target campaign: create or increment contact_count
        if is_new_campaign:
            transaction.set(tgt_ref, {
                "campaign_id":   target_campaign_id,
                "name":          new_campaign_name,
                "owner":         user,
                "status":        "draft",
                "created_at":    now,
                "updated_at":    now,
                "contact_count": n,
                "comment":       f"Auto-created by move from campaign {src_campaign_id} by {user}",
            })
        else:
            transaction.update(tgt_ref, {
                "contact_count": _gfs.Increment(n),
                "updated_at":    now,
            })

        # Source campaign: decrement contact_count
        transaction.update(src_ref, {
            "contact_count": _gfs.Increment(-n),
            "updated_at":    now,
        })

        # Copy each contact (same doc ID, history appended) + delete from source
        for did, snap in src_snaps.items():
            data = snap.to_dict() or {}
            history = data.get("comment_history") or []
            if not isinstance(history, list):
                history = []
            data["comment_history"] = history + [move_entry]
            transaction.set(tgt_col.document(did), data)
            transaction.delete(src_col.document(did))

        return n  # returned to the outer scope after commit

    moved = _move(db.transaction())

    print(
        f"[campaign-move] transaction committed — moved {moved} contacts "
        f"from \'{src_campaign_id}\' to \'{target_campaign_id}\'"
        + (" (new campaign)" if is_new_campaign else ""),
        flush=True,
    )

    return {
        "moved":              moved,
        "errors":             [],
        "rolled_back":        False,
        "src_campaign_id":    src_campaign_id,
        "target_campaign_id": target_campaign_id,
    }
