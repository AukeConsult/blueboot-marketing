# functions-smartmail/smart_mail/smart_campaign_sender.py
# Adapted copy of app/smart-mail/smart_campaign_sender.py for the deployed
# Cloud Function codebase. Logic is identical (HTML->text conversion, per-
# account send-rate budget, bounce-rate breaker, full send_campaign()
# orchestration); only the imports are rewritten to relative `smart_mail.*`
# paths and the CLI __main__ block is dropped (main.py's /run-campaigns
# route is the entry point here, mirroring smart_campaign_worker.py).

import re
import time
import os

from datetime import datetime, timedelta, timezone
from html import unescape

from .firestore_client import get_firestore
from .mail_sender import send_email
from .smart_template_engine import render_template
from .smart_campaign_stats import refresh_campaign_stats
from .config import (
    CAMPAIGN_SEND_DELAY_SECONDS as _CFG_SEND_DELAY,
    MAX_SENDS_PER_HOUR,
    MAX_SENDS_PER_DAY,
    BOUNCE_RATE_PAUSE_THRESHOLD,
)


SEND_DELAY_SECONDS = int(os.getenv("CAMPAIGN_SEND_DELAY_SECONDS", str(_CFG_SEND_DELAY)))

_MIN_ATTEMPTS_BEFORE_BREAKER = 8


# ---------------------------------------------------------------------------
# HTML -> plain text (Quill-editor bodies have no separate text template)
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_CLOSE_RE = re.compile(r"(?i)</(p|div|tr|li|h[1-6])>")
_BR_RE = re.compile(r"(?i)<br\s*/?>")
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_SPACE_RUN_RE = re.compile(r"[ \t]+")


def html_to_text(html: str) -> str:
    """Best-effort HTML -> plain-text conversion for the multipart text part."""
    if not html:
        return ""

    text = _SCRIPT_STYLE_RE.sub("", html)
    text = _BR_RE.sub("\n", text)
    text = _BLOCK_CLOSE_RE.sub("\n\n", text)
    text = _TAG_RE.sub("", text)
    text = unescape(text)
    text = _SPACE_RUN_RE.sub(" ", text)
    text = _BLANK_RUN_RE.sub("\n\n", text)

    return text.strip()


# ---------------------------------------------------------------------------
# Firestore helpers
# ---------------------------------------------------------------------------
def load_campaign(db, campaign_id: str):
    doc = db.collection("campaigns").document(campaign_id).get()
    if not doc.exists:
        raise ValueError(f"Campaign not found: {campaign_id}")

    return doc.to_dict()


def get_pending_contacts(db, campaign_id: str):
    contacts = (
        db.collection("campaigns")
        .document(campaign_id)
        .collection("campaign_contacts")
        .where("status", "==", "pending")
        .stream()
    )

    return list(contacts)


def update_campaign_contact(db, campaign_id: str, contact_doc_id: str, payload: dict):
    (
        db.collection("campaigns")
        .document(campaign_id)
        .collection("campaign_contacts")
        .document(contact_doc_id)
        .update(payload)
    )


def update_email_contact(db, email_contact_doc_id: str, payload: dict):
    (
        db.collection("email_contacts")
        .document(email_contact_doc_id)
        .update(payload)
    )


# ---------------------------------------------------------------------------
# Deliverability guards -- per-account send caps + bounce-rate breaker
# ---------------------------------------------------------------------------
def _sent_count_since(db, sender_account: str, since_iso: str) -> int:
    try:
        docs = (
            db.collection("outreach_sent")
            .where("sender_account", "==", sender_account)
            .where("sent_at", ">=", since_iso)
            .stream()
        )
        return sum(1 for _ in docs)
    except Exception as ex:
        print(f"[throttle] count query failed ({ex}); assuming budget exhausted")
        return 10**9


def compute_send_budget(db, sender_account: str) -> int:
    """How many more emails this account may send right now, this run."""
    now = datetime.now(timezone.utc)
    hour_ago = (now - timedelta(hours=1)).isoformat()
    day_ago = (now - timedelta(days=1)).isoformat()

    sent_last_hour = _sent_count_since(db, sender_account, hour_ago)
    sent_last_day = _sent_count_since(db, sender_account, day_ago)

    remaining_hour = max(0, MAX_SENDS_PER_HOUR - sent_last_hour)
    remaining_day = max(0, MAX_SENDS_PER_DAY - sent_last_day)

    budget = min(remaining_hour, remaining_day)
    print(
        f"[throttle] {sender_account}: sent_last_hour={sent_last_hour}/{MAX_SENDS_PER_HOUR}  "
        f"sent_last_day={sent_last_day}/{MAX_SENDS_PER_DAY}  budget_this_run={budget}"
    )
    return budget


def bounce_rate_tripped(sent_count: int, failed_count: int) -> bool:
    attempts = sent_count + failed_count
    if attempts < _MIN_ATTEMPTS_BEFORE_BREAKER:
        return False

    return (failed_count / attempts) > BOUNCE_RATE_PAUSE_THRESHOLD


# ---------------------------------------------------------------------------
# Main send loop
# ---------------------------------------------------------------------------
def send_campaign(campaign_id: str):
    db = get_firestore()
    db.collection("campaigns").document(campaign_id).update({
        "status": "sending",
        "started_at": datetime.now(timezone.utc).isoformat()})

    campaign = load_campaign(db, campaign_id)
    sender_account = (campaign.get("outreach_email_account") or campaign.get("sender_account") or "sales")
    mail_cfg = campaign.get("mail", {})
    subject_template = mail_cfg.get("subject", "")
    html_template = mail_cfg.get("body", "")
    contacts = get_pending_contacts(db, campaign_id)
    sent_count = 0
    failed_count = 0

    print(
        f"Campaign {campaign_id}: "
        f"{len(contacts)} pending contacts  (account={sender_account})"
    )

    budget = compute_send_budget(db, sender_account)
    if budget <= 0:
        print(f"[throttle] {sender_account} has no send budget left right now -- "
              f"requeuing campaign {campaign_id} for the next scheduled run")
        db.collection("campaigns").document(campaign_id).update({"status": "queued"})
        return

    if budget < len(contacts):
        print(f"[throttle] sending {budget} of {len(contacts)} this run; "
              f"the remainder will go out on the next scheduled run")

    contacts = contacts[:budget]
    breaker_tripped = False

    try:
        for contact_doc in contacts:
            contact = contact_doc.to_dict()
            email = contact.get("email")

            if not email:
                update_campaign_contact(db, campaign_id, contact_doc.id, {
                        "status": "failed",
                        "last_error": "Missing email address",
                    },
                )

                update_email_contact(db, contact_doc.id, {
                        "status": "failed",
                        "last_error": "Missing email address",
                    },
                )

                failed_count += 1
                continue

            update_campaign_contact(db, campaign_id, contact_doc.id, {"status": "sending"})

            subject = render_template(subject_template, contact)
            html_body = render_template(html_template, contact)
            text_body = html_to_text(html_body) or subject

            try:
                result = send_email(
                    to_email=email,
                    subject=subject,
                    text_body=text_body,
                    html_body=html_body,
                    account=sender_account,
                    campaign_id=campaign_id,
                    contact_doc_id=contact_doc.id,
                )

                update_campaign_contact(
                    db,
                    campaign_id,
                    contact_doc.id,
                    {
                        "status": "sent",
                        "campaign_id": campaign_id,
                        "sent_at": result["sent_at"],
                        "message_id": result["message_id"],
                        "sender_account": sender_account,
                    },
                )

                update_email_contact(
                    db,
                    contact_doc.id,
                    {
                        "status": "sent",
                        "campaign": campaign_id,
                        "sent_at": result["sent_at"],
                        "last_message_id": result["message_id"],
                        "sender_account": sender_account,
                    },
                )

                sent_count += 1
                print(f"Sent: {email}")

            except Exception as ex:
                failed_count += 1
                update_campaign_contact(db, campaign_id, contact_doc.id, {
                        "status": "failed",
                        "last_error": str(ex),
                    },
                )

                update_email_contact(db, contact_doc.id, {
                        "status": "failed",
                        "last_error": str(ex),
                    },
                )

                print(f"FAILED {email}: {ex}")

            if bounce_rate_tripped(sent_count, failed_count):
                breaker_tripped = True
                print(
                    f"[breaker] {campaign_id}: failure rate "
                    f"{failed_count}/{sent_count + failed_count} exceeded "
                    f"{BOUNCE_RATE_PAUSE_THRESHOLD:.0%} -- pausing campaign"
                )
                break

            time.sleep(SEND_DELAY_SECONDS)

        refresh_campaign_stats(campaign_id)

        if breaker_tripped:
            db.collection("campaigns").document(campaign_id).update(
                {
                    "status": "paused",
                    "paused_at": datetime.now(timezone.utc).isoformat(),
                    "pause_reason": (
                        f"bounce/failure rate exceeded "
                        f"{BOUNCE_RATE_PAUSE_THRESHOLD:.0%} "
                        f"({failed_count}/{sent_count + failed_count})"
                    ),
                    "sent_count": sent_count,
                    "failed_count": failed_count,
                }
            )
            return

        remaining = get_pending_contacts(db, campaign_id)
        if remaining:
            db.collection("campaigns").document(campaign_id).update(
                {
                    "status": "queued",
                    "sent_count": sent_count,
                    "failed_count": failed_count,
                }
            )
            print(f"Campaign {campaign_id}: {len(remaining)} contacts remain -- requeued")
        else:
            db.collection("campaigns").document(campaign_id).update(
                {
                    "status": "completed",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "sent_count": sent_count,
                    "failed_count": failed_count,
                }
            )

    except Exception as ex:
        db.collection("campaigns").document(campaign_id).update(
            {
                "status": "failed",
                "last_error": str(ex),
            }
        )
        raise
