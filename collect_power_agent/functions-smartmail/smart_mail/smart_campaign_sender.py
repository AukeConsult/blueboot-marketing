# functions-smartmail/smart_mail/smart_campaign_sender.py
"""Outreach send loop — reads candidates via read_outreach(), renders via
render_mail(), sends via MailSender, and writes back via confirm_sent().

Public entry point:
    send_outreach(mode="intro", dry_run=False) -> dict
        mode "intro"     — contacts with status="pending"
        mode "followup"  — pending contacts with a due sequence step
        dry_run=True     — select and render, but do not send or confirm

Rate limiting and the bounce-rate circuit-breaker are applied per sending account.
Use send_outreach() for campaign outreach.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone

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


def _account_ready(account) -> tuple[bool, str]:
    if account.account_type == "gmail":
        if account.access_token or (account.client_id and account.client_secret and account.refresh_token):
            return True, ""
        return False, "Gmail account is missing OAuth settings in mail account configuration"
    if not account.host:
        return False, "IMAP/SMTP account is missing smtp_host/host in mail account configuration"
    if not account.username:
        return False, "IMAP/SMTP account is missing username in mail account configuration"
    if not account.password:
        return False, "IMAP/SMTP account is missing password in mail account configuration"
    return True, ""



# ---------------------------------------------------------------------------
# Main send loop
# ---------------------------------------------------------------------------

def _account_settings(account) -> dict:
    settings = dict(account.raw or {})
    settings["email"] = settings.get("email") or account.email
    settings["account_type"] = settings.get("account_type") or account.account_type
    settings["username"] = settings.get("username") or account.username
    settings["password"] = settings.get("password") or account.password
    settings["display_name"] = settings.get("display_name") or account.from_name
    settings["host"] = settings.get("host") or account.imap_host
    settings["imap_host"] = settings.get("imap_host") or account.imap_host
    settings["port"] = settings.get("port") or account.imap_port
    settings["smtp_host"] = settings.get("smtp_host") or account.host
    settings["smtp_port"] = settings.get("smtp_port") or account.port
    settings["smtp_ssl"] = settings.get("smtp_ssl") or account.use_ssl
    return settings


def _coerce_id_set(values) -> set[str]:
    """Coerce string-or-list campaign filters into a normalized id set."""
    if not values:
        return set()
    if isinstance(values, str):
        return {v.strip() for v in re.split(r"[,;|\n]", values) if v.strip()}
    out: set[str] = set()
    for item in values:
        if item is None:
            continue
        out.update(v.strip() for v in re.split(r"[,;|\n]", str(item)) if v.strip())
    return out


def _print_preview(contact, step, rendered, preview: bool) -> None:
    print(
        "    %-35s  %-20s  [step %d: %s]  subj: %r" % (
            contact.email,
            (contact.company or "")[:20],
            step.index,
            step.mail_type,
            rendered.subject[:60],
        )
    )
    if preview:
        body = (rendered.html_body or rendered.text_body or "").strip()
        snippet = " ".join(body[:200].split())
        if len(body) > 200:
            snippet += " ..."
        print("      body: %s" % snippet)


def send_outreach(
    mode: str = "intro",
    *,
    limit: int = 500,
    campaign_ids: list[str] | None = None,
    dry_run: bool = False,
    preview: bool = False,
) -> dict:
    """One-pass send loop for intro or followup outreach.

    Reads candidates from Firestore via read_outreach(mode), renders each mail
    via render_mail(), sends via MailSender, and records each success via confirm_sent().
    With dry_run=True, the same selection and rendering path runs, but no sender
    is opened and no Firestore confirmation is written.
    When campaign_ids is provided it is applied in read_outreach() and checked
    again before sending.

    mode "intro"    -- pending contacts with no sent mail; uses Intro step.
    mode "followup" -- pending contacts with sent mail; uses next due sequence step.

    Returns a summary dict.
    """
    # Lazy imports keep dry-run cheap and avoid import-time Firestore work.
    if not dry_run:
        from smart_mail.mail_sender import MailSender              # noqa: PLC0415
    else:
        MailSender = None
    from smart_mail.outreach_mail_select import read_outreach, confirm_sent, prepare_mail_sequences  # noqa: PLC0415
    from smart_mail.outreach_render_mail import render_mail, MailStep  # noqa: PLC0415

    db = get_firestore()

    # Prepare current campaigns that have mail_schedule but no mail_sequence.
    prepare_mail_sequences(db)

    summary: dict = {
        "mode": mode,
        "dry_run": dry_run,
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "would_send": 0,
    }
    campaign_filter = _coerce_id_set(campaign_ids)

    batches = read_outreach(mode=mode, limit=limit, campaign_ids=sorted(campaign_filter))
    if not batches:
        print(f"[sender] no outreach candidates for mode={mode!r}")
        return summary

    for batch in batches:
        account  = batch.account
        campaigns = [
            cwc for cwc in batch.campaigns
            if not campaign_filter or cwc.campaign.campaign_id in campaign_filter
        ]
        if not campaigns:
            continue

        ready, reason = _account_ready(account)
        if not ready:
            skipped = sum(len(cwc.contacts) for cwc in campaigns)
            summary["skipped"] += skipped
            print(f"[sender] {reason} for {account.email} -- skipping {skipped} contact(s)")
            continue

        budget = compute_send_budget(db, account.email)
        if budget <= 0:
            skipped = sum(len(cwc.contacts) for cwc in campaigns)
            summary["skipped"] += skipped
            print(f"[sender] {account.email} budget exhausted -- skipping {skipped} contact(s)")
            continue

        sender = None
        if not dry_run:
            sender = MailSender(_account_settings(account))
            open_result = sender.open()
            if open_result.get("status") != "ok":
                skipped = sum(len(cwc.contacts) for cwc in campaigns)
                summary["skipped"] += skipped
                print(
                    f"[sender] cannot open sender for {account.email}: "
                    f"{open_result.get('message', 'unknown error')} -- skipping {skipped} contact(s)"
                )
                continue

        print(
            "\nAccount : %s  (%s)  host=%s:%d  %s  budget=%d  campaigns=%d  contacts=%d%s"
            % (
                account.email,
                account.from_name,
                account.host,
                account.port,
                "SSL" if account.use_ssl else "STARTTLS",
                budget,
                len(campaigns),
                sum(len(cwc.contacts) for cwc in campaigns),
                "  [dry-run]" if dry_run else "",
            )
        )

        sent_batch   = 0
        failed_batch = 0
        tried_batch  = 0

        try:
            for cwc in campaigns:
                campaign = cwc.campaign
                print(
                    f"[sender] campaign={campaign.campaign_id!r}  "
                    f"contacts={len(cwc.contacts)}  account={account.email}"
                )

                for contact in cwc.contacts:
                    if campaign_filter and contact.campaign_id not in campaign_filter:
                        summary["skipped"] += 1
                        print(
                            f"[sender] guard skipped {contact.email}: "
                            f"contact campaign {contact.campaign_id!r} not in {sorted(campaign_filter)!r}"
                        )
                        continue

                    # Stop if budget used up for this account
                    if tried_batch >= budget:
                        print(f"[sender] budget reached for {account.email}")
                        break

                    step_dict = contact.selected_step or {}
                    step = MailStep(
                        index     = step_dict.get("index",     contact.next_mail_index),
                        mail_type = step_dict.get("mail_type", mode),
                        subject   = step_dict.get("subject",   ""),
                        body_html = step_dict.get("body_html", ""),
                        body_text = step_dict.get("body_text", ""),
                    )

                    rendered = render_mail(step, contact)

                    try:
                        if dry_run:
                            _print_preview(contact, step, rendered, preview)
                            sent_batch += 1
                            tried_batch += 1
                            summary["would_send"] += 1
                            continue

                        unsub, unsub_post = _build_unsubscribe_headers(contact.contact_doc_id)
                        headers = {}
                        if unsub:
                            headers["List-Unsubscribe"] = unsub
                        if unsub_post:
                            headers["List-Unsubscribe-Post"] = unsub_post

                        result = sender.send_open(
                            to          = contact.email,
                            subject     = rendered.subject,
                            body_plain  = rendered.text_body,
                            body_html   = rendered.html_body,
                            in_reply_to = contact.in_reply_to,
                            headers     = headers,
                        )
                        if result.get("status") != "ok":
                            raise RuntimeError(result.get("message", "send failed"))
                        message_id = result.get("message_id", "")

                        confirm_sent(
                            campaign_id    = contact.campaign_id,
                            contact_doc_id = contact.contact_doc_id,
                            message_id     = message_id,
                            mail_type      = step.mail_type,
                            mode           = mode,
                            sender_account = account.email,
                        )

                        sent_batch      += 1
                        tried_batch     += 1
                        summary["sent"] += 1
                        print(f"[sender] sent {contact.email}")

                    except Exception as ex:
                        failed_batch       += 1
                        tried_batch        += 1
                        summary["failed"]  += 1
                        print(f"[sender] FAILED {contact.email}: {ex}")

                    if not dry_run and bounce_rate_tripped(sent_batch, failed_batch):
                        print(
                            f"[breaker] {account.email}: failure rate "
                            f"{failed_batch}/{sent_batch + failed_batch} exceeded "
                            f"{BOUNCE_RATE_PAUSE_THRESHOLD:.0%} -- stopping batch"
                        )
                        break

                    if not dry_run:
                        time.sleep(SEND_DELAY_SECONDS)

                # Refresh campaign stats after finishing each campaign's contacts
                if not dry_run:
                    try:
                        refresh_campaign_stats(campaign.campaign_id)
                    except Exception as ex:
                        print(f"[sender] stats refresh failed for {campaign.campaign_id}: {ex}")
        finally:
            if sender is not None:
                sender.close()

    print(
        f"[sender] done  mode={mode!r}  dry_run={dry_run}  "
        f"sent={summary['sent']}  failed={summary['failed']}  "
        f"skipped={summary['skipped']}  would_send={summary['would_send']}"
    )
    return summary
