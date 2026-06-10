# functions-smartmail/smart_mail/smart_campaign_sender.py
"""Outreach send loop — reads candidates via read_outreach(), renders via
render_mail(), sends via SMTP, and writes back via confirm_sent().

Public entry point:
    send_outreach(mode="intro") -> dict
        mode "intro"    — contacts with status="pending"
        mode "followup" — contacts with followup_status="Send mail"

Rate limiting and the bounce-rate circuit-breaker are applied per sending account.
The old send_campaign(campaign_id) is removed — use send_outreach() instead.
"""
from __future__ import annotations

import os
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid

from .firestore_client import get_firestore
from .smart_campaign_stats import refresh_campaign_stats
from .config import (
    CAMPAIGN_SEND_DELAY_SECONDS as _CFG_SEND_DELAY,
    MAX_SENDS_PER_HOUR,
    MAX_SENDS_PER_DAY,
    BOUNCE_RATE_PAUSE_THRESHOLD,
    UNSUBSCRIBE_BASE_URL,
    UNSUBSCRIBE_MAILTO,
)


SEND_DELAY_SECONDS = int(os.getenv("CAMPAIGN_SEND_DELAY_SECONDS", str(_CFG_SEND_DELAY)))

_MIN_ATTEMPTS_BEFORE_BREAKER = 8


# ---------------------------------------------------------------------------
# Password resolution
# ---------------------------------------------------------------------------

def _get_smtp_password(account_email: str) -> str:
    """Derive the SMTP password env-var name from the account email and return it.

    Convention: sales@blueboot.ai  ->  SALES_SMTP_PASSWORD
                info@blueboot.ai   ->  INFO_SMTP_PASSWORD
    """
    alias = account_email.split("@")[0].upper()
    return os.getenv(f"{alias}_SMTP_PASSWORD", "")


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
    """How many more emails this account may send right now."""
    now      = datetime.now(timezone.utc)
    hour_ago = (now - timedelta(hours=1)).isoformat()
    day_ago  = (now - timedelta(days=1)).isoformat()

    sent_last_hour = _sent_count_since(db, sender_account, hour_ago)
    sent_last_day  = _sent_count_since(db, sender_account, day_ago)

    remaining_hour = max(0, MAX_SENDS_PER_HOUR - sent_last_hour)
    remaining_day  = max(0, MAX_SENDS_PER_DAY  - sent_last_day)

    budget = min(remaining_hour, remaining_day)
    print(
        f"[throttle] {sender_account}: "
        f"sent_last_hour={sent_last_hour}/{MAX_SENDS_PER_HOUR}  "
        f"sent_last_day={sent_last_day}/{MAX_SENDS_PER_DAY}  "
        f"budget_this_run={budget}"
    )
    return budget


def bounce_rate_tripped(sent_count: int, failed_count: int) -> bool:
    attempts = sent_count + failed_count
    if attempts < _MIN_ATTEMPTS_BEFORE_BREAKER:
        return False
    return (failed_count / attempts) > BOUNCE_RATE_PAUSE_THRESHOLD


# ---------------------------------------------------------------------------
# Unsubscribe header helper
# ---------------------------------------------------------------------------

def _build_unsubscribe_headers(contact_doc_id: str | None) -> tuple[str, str]:
    """Return (List-Unsubscribe, List-Unsubscribe-Post) header values, or ('', '')."""
    unsub = ""
    unsub_post = ""
    if UNSUBSCRIBE_BASE_URL and contact_doc_id:
        url  = f"{UNSUBSCRIBE_BASE_URL.rstrip('/')}/unsubscribe?id={contact_doc_id}"
        unsub = f"<{url}>"
        if UNSUBSCRIBE_MAILTO:
            unsub = f"<mailto:{UNSUBSCRIBE_MAILTO}>, {unsub}"
        unsub_post = "List-Unsubscribe=One-Click"
    elif UNSUBSCRIBE_MAILTO:
        unsub = f"<mailto:{UNSUBSCRIBE_MAILTO}>"
    return unsub, unsub_post


# ---------------------------------------------------------------------------
# SMTP send helper (account-settings-aware, no Firestore writes)
# ---------------------------------------------------------------------------

def _send_smtp(
    account,                    # MailAccountSettings from outreach_mail_select
    password: str,
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str | None,
    in_reply_to: str | None = None,
    contact_doc_id: str | None = None,
) -> str:
    """Build and send one SMTP message. Returns the Message-ID header value.

    Uses account.host / account.port / account.use_ssl from MailAccountSettings.
    Does NOT write to Firestore -- call confirm_sent() after this returns.
    """
    message_id = make_msgid()

    msg = MIMEMultipart("alternative")
    msg["Message-ID"] = message_id
    msg["Subject"]    = subject
    msg["From"]       = f"{account.from_name} <{account.email}>"
    msg["To"]         = to_email

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"]  = in_reply_to

    unsub, unsub_post = _build_unsubscribe_headers(contact_doc_id)
    if unsub:
        msg["List-Unsubscribe"] = unsub
    if unsub_post:
        msg["List-Unsubscribe-Post"] = unsub_post

    # RFC 2045: plain FIRST, HTML SECOND (clients render the last part they understand)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    if account.use_ssl:
        server = smtplib.SMTP_SSL(account.host, account.port, timeout=30)
    else:
        server = smtplib.SMTP(account.host, account.port, timeout=30)
        server.ehlo()
        server.starttls()
        server.ehlo()

    try:
        server.login(account.username, password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass

    print(f"[smtp] -> {to_email}  via {account.email}  ({account.host}:{account.port})")
    return message_id



# ---------------------------------------------------------------------------
# Backward-compatibility: auto-migrate old mail.subject / mail.body campaigns
# ---------------------------------------------------------------------------

def _backfill_mail_sequence(db) -> int:
    """One-pass migration: build mail_sequence from whatever format the campaign uses.

    Checks three sources in order of preference:
      1. mail_schedule (current frontend format) -- array of step dicts, each with
         a nested mail.subject / mail.body / mail.type and a step name
      2. mail.subject + mail.body (legacy flat format)
      3. Neither found: skip (campaign has no email content at all)

    Idempotent -- campaigns that already have mail_sequence are never touched.
    Returns the number of campaigns updated.
    """
    updated = 0
    for doc in db.collection("campaigns").stream():
        d = doc.to_dict() or {}
        if d.get("mail_sequence"):
            continue  # already migrated, skip

        # ── Source 1: mail_schedule (current frontend format) ────────────────
        # Structure: [{name, step_id, delay_days, mail: {subject, body, type}}, ...]
        mail_schedule = d.get("mail_schedule") or []
        if mail_schedule and isinstance(mail_schedule, list):
            mail_sequence = []
            for i, step in enumerate(mail_schedule):
                step_mail = step.get("mail") or {}
                subject   = step_mail.get("subject", "").strip()
                body      = step_mail.get("body",    "").strip()
                # Step name e.g. "Intro" -> "intro", "Follow-up 1" -> "followup_1"
                step_name = (step.get("name") or "").strip().lower()
                if i == 0 or "intro" in step_name:
                    mail_type = "intro"
                else:
                    mail_type = f"followup_{i}"
                # type=="plain" means plain text; anything else is HTML
                is_plain = step_mail.get("type", "html") == "plain"
                mail_sequence.append({
                    "index":     i,
                    "mail_type": mail_type,
                    "subject":   subject,
                    "body_html": "" if is_plain else body,
                    "body_text": body if is_plain else "",
                })
            if mail_sequence:
                doc.reference.update({"mail_sequence": mail_sequence})
                print(f"[backfill] {doc.id!r}: built {len(mail_sequence)} step(s) "
                      f"from mail_schedule")
                updated += 1
            continue  # done with this campaign regardless

        # ── Source 2: legacy flat mail.subject / mail.body ───────────────────
        mail_cfg = d.get("mail") or {}
        subject  = mail_cfg.get("subject", "").strip()
        body     = mail_cfg.get("body",    "").strip()
        if subject or body:
            doc.reference.update({
                "mail_sequence": [{
                    "index":     0,
                    "mail_type": "intro",
                    "subject":   subject,
                    "body_html": body,
                    "body_text": "",
                }]
            })
            print(f"[backfill] {doc.id!r}: built 1 step from legacy mail.subject/body")
            updated += 1

    if updated:
        print(f"[backfill] migrated {updated} campaign(s) to mail_sequence")
    return updated


# ---------------------------------------------------------------------------
# Main send loop
# ---------------------------------------------------------------------------

def send_outreach(mode: str = "intro") -> dict:
    """One-pass send loop for intro or followup outreach.

    Reads candidates from Firestore via read_outreach(mode), renders each mail
    via render_mail(), sends via SMTP, and records each success via confirm_sent().

    mode "intro"    -- contacts with status="pending"; always uses mail_sequence[0]
    mode "followup" -- contacts with followup_status="Send mail";
                       uses mail_sequence[len(contact.mail_sent)]

    Returns a summary dict: {"mode", "sent", "failed", "skipped"}.
    """
    # Lazy imports -- outreach_mail_select and outreach_render_mail live at the
    # functions-smartmail/ root (parent of this smart_mail/ package).
    from outreach_mail_select import read_outreach, confirm_sent  # noqa: PLC0415
    from outreach_render_mail import render_mail, MailStep        # noqa: PLC0415

    db = get_firestore()

    # Auto-migrate old campaigns that predate mail_sequence
    _backfill_mail_sequence(db)

    summary: dict = {"mode": mode, "sent": 0, "failed": 0, "skipped": 0}

    batches = read_outreach(mode=mode)
    if not batches:
        print(f"[sender] no outreach candidates for mode={mode!r}")
        return summary

    for batch in batches:
        account  = batch.account
        password = _get_smtp_password(account.email)

        if not password:
            print(f"[sender] no SMTP password for {account.email} -- skipping batch")
            continue

        budget = compute_send_budget(db, account.email)
        if budget <= 0:
            print(f"[sender] {account.email} budget exhausted -- skipping batch")
            continue

        sent_batch   = 0
        failed_batch = 0

        for cwc in batch.campaigns:
            campaign = cwc.campaign
            print(
                f"[sender] campaign={campaign.campaign_id!r}  "
                f"contacts={len(cwc.contacts)}  account={account.email}"
            )

            for contact in cwc.contacts:
                # Stop if budget used up for this account
                if sent_batch + failed_batch >= budget:
                    print(f"[sender] budget reached for {account.email}")
                    break

                # Determine which step in the mail sequence to send.
                # mail_sent lives in contact.extra (not a direct ContactRow attr)
                # because _CONTACT_KNOWN in outreach_mail_select.py doesn't list it.
                mail_sent   = (contact.extra or {}).get("mail_sent") or []
                next_idx    = len(mail_sent)
                in_reply_to = mail_sent[-1].get("message_id") if mail_sent else None

                if next_idx >= len(campaign.mail_sequence):
                    print(
                        f"[sender] skip {contact.email} -- "
                        f"sequence exhausted (idx={next_idx}, "
                        f"seq_len={len(campaign.mail_sequence)})"
                    )
                    summary["skipped"] += 1
                    continue

                # Build a MailStep from the Firestore dict stored in mail_sequence
                step_dict = campaign.mail_sequence[next_idx]
                step = MailStep(
                    index     = step_dict.get("index",     next_idx),
                    mail_type = step_dict.get("mail_type", mode),
                    subject   = step_dict.get("subject",   ""),
                    # "body_html" is the modern key; "body" is the legacy key
                    body_html = (
                        step_dict.get("body_html", "")
                        or step_dict.get("body", "")
                    ),
                    body_text = step_dict.get("body_text", ""),
                )

                rendered = render_mail(step, contact)

                try:
                    message_id = _send_smtp(
                        account        = account,
                        password       = password,
                        to_email       = contact.email,
                        subject        = rendered.subject,
                        text_body      = rendered.text_body,
                        html_body      = rendered.html_body,
                        in_reply_to    = in_reply_to,
                        contact_doc_id = contact.contact_doc_id,
                    )

                    confirm_sent(
                        campaign_id    = contact.campaign_id,
                        contact_doc_id = contact.contact_doc_id,
                        message_id     = message_id,
                        mail_type      = step.mail_type,
                        mode           = mode,
                        sender_account = account.email,
                    )

                    sent_batch      += 1
                    summary["sent"] += 1
                    print(f"[sender] sent {contact.email}")

                except Exception as ex:
                    failed_batch       += 1
                    summary["failed"]  += 1
                    print(f"[sender] FAILED {contact.email}: {ex}")

                if bounce_rate_tripped(sent_batch, failed_batch):
                    print(
                        f"[breaker] {account.email}: failure rate "
                        f"{failed_batch}/{sent_batch + failed_batch} exceeded "
                        f"{BOUNCE_RATE_PAUSE_THRESHOLD:.0%} -- stopping batch"
                    )
                    break

                time.sleep(SEND_DELAY_SECONDS)

            # Refresh campaign stats after finishing each campaign's contacts
            try:
                refresh_campaign_stats(campaign.campaign_id)
            except Exception as ex:
                print(f"[sender] stats refresh failed for {campaign.campaign_id}: {ex}")

    print(
        f"[sender] done  mode={mode!r}  "
        f"sent={summary['sent']}  failed={summary['failed']}  "
        f"skipped={summary['skipped']}"
    )
    return summary
