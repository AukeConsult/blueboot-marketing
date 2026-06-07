# app/smart_mail/smart_campaign_worker.py

import time
import os

from app.firestore_client import get_firestore


POLL_SECONDS = int(os.getenv("CAMPAIGN_WORKER_POLL_SECONDS", "15"))

def find_queued_campaigns(db):
    campaigns = (db.collection("campaigns").where("status", "==", "queued").stream())
    return list(campaigns)


def process_campaign(db, campaign_doc):
    campaign_id = campaign_doc.id
    print(f"Starting campaign: {campaign_id}")

    try:
        send_campaign(campaign_id)
        print(f"Campaign completed: {campaign_id}")

    except Exception as ex:
        db.collection("campaigns").document(campaign_id).update(
            {
                "status": "failed",
                "last_error": str(ex),
            }
        )

        print(f"Campaign failed: {campaign_id}")


def run_worker():
    db = get_firestore()
    print("Smart campaign worker started")

    while True:
        try:
            campaigns = find_queued_campaigns(db)
            print(f"Queued campaigns: {len(campaigns)}")

            for campaign_doc in campaigns:
                process_campaign(db, campaign_doc)

        except Exception as ex:
            print(f"Worker error: {ex}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run_worker()
