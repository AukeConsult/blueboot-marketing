# functions-crm/smart_mail/site_followup.py
"""Move a site's (campaign_leads) follow-up status forward automatically.

One rule used by the sender and the reply matcher:
a status is only written when the site's CURRENT status is one of `allowed_current`.
A status somebody chose by hand is never overwritten.
"""
from __future__ import annotations

from datetime import datetime, timezone


def set_site_followup(db, campaign_id: str, lead_id, new_status: str,
                      allowed_current: tuple[str, ...] = ("",),
                      when_iso: str | None = None) -> bool:
    """Set campaign_leads/{lead_id}.followup_status = new_status if the current one is allowed.

    Returns True when written. Never raises: a problem here must not break the caller
    (a mail was already sent / a reply was already stored).
    """
    lead_id = str(lead_id or "").strip()
    if not lead_id:
        return False
    try:
        ref = (db.collection("campaigns").document(campaign_id)
                 .collection("campaign_leads").document(lead_id))
        snap = ref.get()
        if not snap.exists:
            return False
        current = str((snap.to_dict() or {}).get("followup_status") or "").strip().lower()
        if current not in allowed_current:
            return False
        ref.update({"followup_status": new_status,
                    "updated_at": when_iso or datetime.now(timezone.utc).isoformat()})
        return True
    except Exception as exc:
        print(f"[site_followup] could not set {new_status!r} on site {lead_id!r}: {exc}", flush=True)
        return False
