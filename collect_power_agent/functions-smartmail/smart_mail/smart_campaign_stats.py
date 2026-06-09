# functions-smartmail/smart_mail/smart_campaign_stats.py
# Adapted copy of app/smart-mail/smart_campaign_stats.py -- only the
# Firestore import is relative.
from .firestore_client import get_firestore


def refresh_campaign_stats(campaign_id: str):
    db = get_firestore()

    contacts = list(db.collection("campaigns").document(campaign_id).collection("campaign_contacts").stream())
    total = len(contacts)

    sent = 0
    failed = 0
    pending = 0
    sending = 0
    replied = 0

    for doc in contacts:
        status = (doc.to_dict().get("status"))

        if status == "sent":
            sent += 1
        elif status == "failed":
            failed += 1
        elif status == "sending":
            sending += 1
        elif status == "replied":
            replied += 1
        else:
            pending += 1

    db.collection("campaigns").document(campaign_id).update(
        {
            "contact_count": total,
            "sent_count": sent,
            "failed_count": failed,
            "pending_count": pending,
            "sending_count": sending,
            "reply_count": replied,
        }
    )
