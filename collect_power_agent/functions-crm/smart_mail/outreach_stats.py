# functions-crm/smart_mail/outreach_stats.py
# Campaign outreach statistics helpers.
from .firestore_client import get_firestore


def refresh_campaign_stats(campaign_id: str):
    db = get_firestore()

    contacts = list(db.collection("campaigns").document(campaign_id).collection("campaign_contacts").stream())
    total = len(contacts)

    active = 0
    pending = 0
    excluded = 0
    contacted = 0
    received = 0
    replied = 0

    for doc in contacts:
        data = doc.to_dict() or {}
        status = data.get("status") or "pending"
        followup_status = data.get("followup_status") or ""

        if status == "active":
            active += 1
        elif status == "excluded":
            excluded += 1
        else:
            pending += 1

        if followup_status == "contacted":
            contacted += 1
        elif followup_status == "received":
            received += 1
        elif followup_status == "replied":
            replied += 1

    db.collection("campaigns").document(campaign_id).update(
        {
            "contact_count": total,
            "active_count": active,
            "pending_count": pending,
            "excluded_count": excluded,
            "contacted_count": contacted,
            "received_count": received,
            "reply_count": received + replied,
            "status_breakdown": {
                "pending": pending,
                "active": active,
                "excluded": excluded,
            },
        }
    )
