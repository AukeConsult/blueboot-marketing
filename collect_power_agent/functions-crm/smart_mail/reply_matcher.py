# functions-crm/smart_mail/reply_matcher.py
# Full reply pipeline: fetch from IMAP → classify → find contact → apply actions.
# Single entry point: match_new_replies() — called identically from the HTTP
# worker, Cloud Task, and the app/reply_match.py CLI.

from __future__ import annotations

import imaplib
import email as _email_lib
from email.header import decode_header
from datetime import datetime, timezone
import re

from .firestore_client import get_firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from .outreach_stats import refresh_campaign_stats


_MSGID_RE = re.compile(r"<[^<>\s]+>")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

_SETTINGS  = "settings"
_MAIL_ACCS = "mail_accounts"
_INBOX     = "inbox_messages"
_OUTREACH  = "outreach_sent"
_CAMPAIGNS = "campaigns"
_CC        = "campaign_contacts"
_CL        = "campaign_leads"


# Bounce heuristic patterns (used only as fallback — structural checks take priority)
_BOUNCE_FROM_RE = re.compile(
    r"(mailer-daemon|postmaster|mail-daemon|noreply\+bounce|bounce\+)",
    re.IGNORECASE,
)
_BOUNCE_SUBJECT_RE = re.compile(
    r"(mail delivery failed|undeliverable|delivery status notification|"
    r"failure notice|returned mail|delivery failure|non.delivery|"
    r"message not delivered|could not be delivered)",
    re.IGNORECASE,
)
_FINAL_RECIPIENT_RE = re.compile(
    r"Final-Recipient\s*:.*?rfc822;\s*([\w.+-]+@[\w.-]+)", re.IGNORECASE
)
_FAILED_HEADER_RE = re.compile(
    r"X-Failed-Recipients\s*:\s*([\w.+-]+@[\w.-]+)", re.IGNORECASE
)
_ORIG_RECIPIENT_RE = re.compile(
    r"Original-Recipient\s*:.*?rfc822;\s*([\w.+-]+@[\w.-]+)", re.IGNORECASE
)
# Local-parts that identify an automated/daemon sender — never a real contact
_DAEMON_LOCALPARTS = (
    "mailer-daemon", "postmaster", "mail-daemon", "mdaemon",
    "noreply", "no-reply", "donotreply", "do-not-reply", "bounce",
)
# Exim / cPanel style: "The following address(es) failed:\n\n  user@host"
_FAILED_BLOCK_RE = re.compile(
    r"following\s+address(?:\(es\))?\s+failed\s*:\s*(?:\r?\n)+\s*"
    r"<?([\w.+-]+@[\w.-]+)>?",
    re.IGNORECASE,
)

# DMARC report subject pattern
_DMARC_SUBJECT_RE = re.compile(
    r"Report\s+domain:\s*(?P<domain>[\w.-]+)"
    r".*?Submitter:\s*(?P<submitter>[\w.-]+)"
    r".*?Report-ID:\s*(?P<report_id>[\S]+)",
    re.IGNORECASE | re.DOTALL,
)
_DMARC_ATTACH_RE = re.compile(r"\.(xml\.gz|xml\.zip|gz|zip)$", re.IGNORECASE)


# ── helpers ──────────────────────────────────────────────────────────────────

def _bare(value: str) -> str:
    if not value:
        return ""
    m = _EMAIL_RE.search(value)
    return m.group(0).lower() if m else ""


def _decode_mime(value) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for part, enc in parts:
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="ignore"))
        else:
            out.append(part)
    return "".join(out)


def _extract_mids(*headers) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for h in headers:
        if not h:
            continue
        for token in _MSGID_RE.findall(h):
            if token not in seen:
                seen.add(token)
                out.append(token)
    return out


def _doc_id(email: str) -> str:
    """Normalize an email address to the doc_id used across campaign_contacts,
    email_contacts — same formula as _doc_id_from_email in name_enrich_lib.py.
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "_", (email or "").strip().lower())


def _extract_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if (part.get_content_type() == "text/plain"
                    and "attachment" not in str(part.get("Content-Disposition"))):
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(errors="ignore")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(errors="ignore")
    return ""


def _parse_dmarc_subject(subject: str) -> dict | None:
    """Extract domain, submitter, report_id from a DMARC aggregate report subject.
    Returns None if subject does not match the DMARC pattern.
    """
    m = _DMARC_SUBJECT_RE.search(subject or "")
    if not m:
        return None
    return {
        "dmarc_domain":    m.group("domain").lower(),
        "dmarc_submitter": m.group("submitter").lower(),
        "dmarc_report_id": m.group("report_id").strip(),
    }


def _is_daemon_addr(addr: str) -> bool:
    """True for mailer-daemon / postmaster / noreply-style addresses."""
    a = (addr or "").lower()
    if not a:
        return True
    local = a.split("@", 1)[0]
    return any(d in local for d in _DAEMON_LOCALPARTS)


# Local-parts that identify a system/automation mailbox (never a real contact)
_SYSTEM_LOCALPARTS = (
    "cpanel", "mailer-daemon", "mail-daemon", "mdaemon", "postmaster",
    "noreply", "no-reply", "donotreply", "do-not-reply", "bounce",
)


def _is_self_or_system(addr: str, own_domains: set[str] | None = None) -> bool:
    """True if addr is on one of our own sending domains, or is a system /
    automation mailbox (cpanel@, no-reply@, mailer-daemon@). Such addresses
    can never be a campaign contact, so the message is skipped."""
    a = (addr or "").lower()
    if not a or "@" not in a:
        return False
    local, _, domain = a.partition("@")
    if own_domains and domain in own_domains:
        return True
    return any(sysp in local for sysp in _SYSTEM_LOCALPARTS)


def _extract_bounce_recipient(msg, sender_addr: str = "") -> str | None:
    """Best-effort recovery of the ORIGINAL intended recipient of a bounce.

    Candidates are gathered from several sources and ranked by how
    authoritative each source is (lower = more trustworthy). The best
    non-daemon, non-sender address wins:

      P0  X-Failed-Recipients header; delivery-status Final-/Original-Recipient
      P1  "The following address(es) failed:" block (Exim/cPanel);
          Final-/Original-Recipient lines found in the body text
      P2  embedded original message (message/rfc822) To: header
      P3  first plain address in the body that is neither daemon nor sender

    Note: Delivered-To / X-Original-To on the embedded original are NOT used —
    on an outbound bounce they name the SENDER's local mailbox, not the failed
    recipient (this caused cpanel@own-domain to be picked by mistake).
    """
    sender_addr = (sender_addr or "").lower()
    found: list[tuple[int, str]] = []

    def add(pri: int, value: str) -> None:
        addr = _bare(value) if value and "@" in value else (value or "").lower()
        if addr and not _is_daemon_addr(addr) and addr != sender_addr:
            found.append((pri, addr))

    # P0 — X-Failed-Recipients header
    fh = msg.get("X-Failed-Recipients", "")
    if fh:
        add(0, fh)

    body_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()

            if ct == "message/delivery-status":
                # delivery-status is itself multipart: its per-recipient
                # sub-blocks carry Final-/Original-Recipient as HEADERS.
                subs = part.get_payload()
                if isinstance(subs, list):
                    for sub in subs:
                        get = getattr(sub, "get", None)
                        if not get:
                            continue
                        for hdr in ("Final-Recipient", "Original-Recipient"):
                            v = get(hdr)
                            if v:
                                add(0, v)
                txt = str(part)
                for rx in (_FINAL_RECIPIENT_RE, _ORIG_RECIPIENT_RE):
                    m = rx.search(txt)
                    if m:
                        add(0, m.group(1))

            elif ct == "message/rfc822":
                payload = part.get_payload()
                orig = payload[0] if isinstance(payload, list) and payload else None
                if orig is not None:
                    v = orig.get("To")          # original recipient — To only
                    if v:
                        add(2, _decode_mime(v))

            elif ct == "text/plain":
                pb = part.get_payload(decode=True)
                if pb:
                    body_parts.append(pb.decode(errors="ignore"))
    else:
        body_parts.append(_extract_body(msg))

    body = "\n".join(body_parts) or _extract_body(msg)

    # P1 — explicit failed-address block (Exim/cPanel) + recipient lines in body
    m = _FAILED_BLOCK_RE.search(body)
    if m:
        add(1, m.group(1))
    for rx in (_FINAL_RECIPIENT_RE, _ORIG_RECIPIENT_RE, _FAILED_HEADER_RE):
        m = rx.search(body)
        if m:
            add(1, m.group(1))

    # P3 — generic: first plain address that is not a daemon / not the sender
    for m in _EMAIL_RE.finditer(body):
        add(3, m.group(0))

    if not found:
        return None
    found.sort(key=lambda t: t[0])
    return found[0][1]

def _classify_message(
    msg, from_raw: str, subject: str
) -> tuple[str, str | None, dict | None]:
    """Return (message_type, original_recipient, dmarc_info).

    message_type:        'reply' | 'bounce' | 'dmarc_report'
    original_recipient:  ORIGINAL intended recipient for bounces, else None
    dmarc_info:          {dmarc_domain, dmarc_submitter, dmarc_report_id} or None
    """
    sender = _bare(from_raw)

    # ── 1. DMARC subject check (definitive — parse all three fields) ──────────
    dmarc_info = _parse_dmarc_subject(subject)
    if dmarc_info:
        return "dmarc_report", None, dmarc_info

    # ── 2. X-Failed-Recipients header (definitive bounce) ────────────────────
    if msg.get("X-Failed-Recipients", ""):
        return "bounce", _extract_bounce_recipient(msg, sender), None

    # ── 3. Walk MIME parts for structural evidence ────────────────────────────
    has_delivery_status = False
    if msg.is_multipart():
        for part in msg.walk():
            ct       = part.get_content_type()
            filename = part.get_filename() or ""

            # RFC 3464 delivery-status part → definitive bounce
            if ct == "message/delivery-status":
                has_delivery_status = True

            # DMARC XML attachment (.xml.gz / .xml.zip) → DMARC report
            if _DMARC_ATTACH_RE.search(filename):
                # Subject didn't match but attachment is DMARC-shaped;
                # try to extract fields from subject anyway
                info = _parse_dmarc_subject(subject) or {
                    "dmarc_domain":    "",
                    "dmarc_submitter": _bare(msg.get("From", "")),
                    "dmarc_report_id": "",
                }
                return "dmarc_report", None, info

    if has_delivery_status:
        return "bounce", _extract_bounce_recipient(msg, sender), None

    # ── 4. Heuristic fallbacks (last resort) ─────────────────────────────────
    if _BOUNCE_SUBJECT_RE.search(subject) or _BOUNCE_FROM_RE.search(from_raw):
        return "bounce", _extract_bounce_recipient(msg, sender), None

    return "reply", None, None


# ── Step 1: process one IMAP account (fetch + classify + match in one pass) ──

def _imap_since(days: int) -> str:
    """Return IMAP SINCE date string for the last `days` days."""
    from datetime import timedelta
    d = datetime.now(timezone.utc) - timedelta(days=days)
    return d.strftime("%d-%b-%Y")


def _process_account(
    db,
    account_email: str,
    acc: dict,
    campaign_filter: set[str] | None,
    days: int,
    limit: int,
    dry_run: bool,
    own_domains: set[str] | None = None,
) -> dict:
    """Fetch messages from one IMAP account and match each directly against
    campaign_contacts.  inbox_messages is written as an audit log only."""
    imap_host = str(acc.get("imap_host") or acc.get("host") or "").strip()
    imap_port = int(acc.get("imap_port") or acc.get("port") or 993)
    user      = str(acc.get("username") or acc.get("email") or account_email).strip()
    password  = acc.get("password", "")

    counts = {"replies": 0, "bounces": 0, "dmarc": 0,
              "matched": 0, "bounced": 0, "unmatched": 0, "skipped": 0, "errors": 0}

    if not imap_host:
        print(f"[reply_matcher]   {account_email}: no imap_host — skipped")
        return counts

    print(f"\n[reply_matcher] → {account_email}  ({imap_host}:{imap_port})  last {days}d")
    mail          = imaplib.IMAP4_SSL(imap_host, imap_port)
    dmarc_to_del:  list[bytes] = []
    bounce_to_del: list[bytes] = []

    try:
        mail.login(user, password)
        mail.select("INBOX")
        since = _imap_since(days)
        _status, messages = mail.search(None, f"SINCE {since}")
        email_ids = messages[0].split()
        print(f"[reply_matcher]   {len(email_ids)} message(s) since {since}")

        processed = 0
        for eid in email_ids:
            if processed >= limit:
                print(f"[reply_matcher]   limit={limit} reached — stopping")
                break
            try:
                _s, msg_data = mail.fetch(eid, "(RFC822)")
                raw      = msg_data[0][1]
                msg      = _email_lib.message_from_bytes(raw)

                message_id  = (msg.get("Message-ID") or "").strip()
                in_reply_to = msg.get("In-Reply-To")
                references  = msg.get("References")
                subject     = _decode_mime(msg.get("Subject"))
                from_raw    = _decode_mime(msg.get("From"))
                to_raw      = _decode_mime(msg.get("To"))
                from_addr   = _bare(from_raw)
                received_at = datetime.now(timezone.utc).isoformat()

                message_type, orig_rcpt, dmarc_info = _classify_message(
                    msg, from_raw, subject
                )

                # ── DMARC: log + delete, no contact matching ──────────────────
                if message_type == "dmarc_report":
                    counts["dmarc"] += 1
                    if dry_run:
                        print(f"[reply_matcher]   [DMARC]  dry-run: "
                              f"domain={dmarc_info.get('dmarc_domain')!r}  "
                              f"id={dmarc_info.get('dmarc_report_id')!r}")
                    else:
                        _audit_log(db, message_id, {
                            "account": account_email, "from_email": from_raw,
                            "subject": subject, "received_at": received_at,
                            "message_id": message_id, "message_type": "dmarc_report",
                            **(dmarc_info or {}),
                        })
                        dmarc_to_del.append(eid)
                    processed += 1
                    continue

                # ── Skip internal / system mail (own-domain, cpanel@, etc.) ───
                subject_addr = orig_rcpt if message_type == "bounce" else from_addr
                if subject_addr and _is_self_or_system(subject_addr, own_domains):
                    counts["skipped"] += 1
                    label = "BOUNCE" if message_type == "bounce" else "REPLY"
                    print(f"[reply_matcher] ─── {label}  from={from_addr!r}  "
                          f"rcpt={subject_addr!r}  → SKIP (own-domain/system)")
                    processed += 1
                    continue

                # ── Build message dict for matching ───────────────────────────
                message = {
                    "account":            account_email,
                    "from_email":         from_raw,
                    "from_address":       from_addr,
                    "to_email":           _bare(to_raw),
                    "subject":            subject,
                    "body_text":          _extract_body(msg)[:2000],
                    "received_at":        received_at,
                    "message_id":         message_id,
                    "in_reply_to":        in_reply_to,
                    "references":         references,
                    "imap_uid":           eid.decode(),
                    "message_type":       message_type,
                    "original_recipient": orig_rcpt,
                }

                processed += 1

                if message_type == "bounce":
                    counts["bounces"] += 1
                    orig = orig_rcpt or from_addr or "?"
                    print(f"[reply_matcher] ─── BOUNCE  from={from_addr!r}  rcpt={orig!r}")
                    print(f"[reply_matcher]     subj={subject!r}")
                    contact_doc_id, campaign_id, lead_id = _find_bounced_contact(
                        db, message, campaign_filter=campaign_filter
                    )
                    if contact_doc_id and campaign_id:
                        print(f"[reply_matcher]     matched → {campaign_id} / {contact_doc_id}")
                        outcome = _apply_bounce_actions(
                            db, contact_doc_id, campaign_id, lead_id,
                            None, message, dry_run=dry_run,
                        )
                        counts["bounced"] += 1
                        print(f"[reply_matcher]     ✓ DONE  ({outcome})")
                        if not dry_run:
                            _audit_log(db, message_id, {
                                **message,
                                "match_campaign_id":    campaign_id,
                                "match_contact_doc_id": contact_doc_id,
                                "match_outcome":        outcome,
                            })
                    else:
                        print(f"[reply_matcher]     ✗ NO MATCH")
                        counts["unmatched"] += 1
                        if not dry_run:
                            _audit_log(db, message_id, {**message, "match_outcome": "unmatched"})

                    # Contacts are now updated — queue this bounce for deletion
                    if not dry_run:
                        bounce_to_del.append(eid)

                else:  # reply
                    counts["replies"] += 1
                    print(f"[reply_matcher] ─── REPLY   from={from_addr!r}")
                    print(f"[reply_matcher]     subj={subject!r}")
                    contact_doc_id, campaign_id, lead_id, matched_via = _find_contact(
                        db, message, campaign_filter=campaign_filter
                    )
                    if contact_doc_id and campaign_id:
                        print(f"[reply_matcher]     matched → {campaign_id} / {contact_doc_id}  via={matched_via}")
                        outcome = _apply_actions(
                            db, contact_doc_id, campaign_id, lead_id,
                            None, message, matched_via, dry_run=dry_run,
                        )
                        counts["matched"] += 1
                        print(f"[reply_matcher]     ✓ DONE  ({outcome})")
                        if not dry_run:
                            _audit_log(db, message_id, {
                                **message,
                                "match_campaign_id":    campaign_id,
                                "match_contact_doc_id": contact_doc_id,
                                "match_via":            matched_via,
                                "match_outcome":        outcome,
                            })
                    else:
                        print(f"[reply_matcher]     ✗ NO MATCH")
                        counts["unmatched"] += 1
                        if not dry_run:
                            _audit_log(db, message_id, {**message, "match_outcome": "unmatched"})

            except Exception as ex:
                counts["errors"] += 1
                print(f"[reply_matcher]     ✗ ERROR  {ex}")

        # Delete DMARC reports from IMAP
        if dmarc_to_del and not dry_run:
            try:
                for eid in dmarc_to_del:
                    mail.store(eid, "+FLAGS", "\\Deleted")
                mail.expunge()
                print(f"[reply_matcher]   deleted {len(dmarc_to_del)} DMARC report(s) from IMAP")
            except Exception as ex:
                print(f"[reply_matcher]   IMAP delete failed: {ex}")

        # Delete processed bounce messages from IMAP (contacts already updated)
        if bounce_to_del and not dry_run:
            try:
                for eid in bounce_to_del:
                    mail.store(eid, "+FLAGS", "\\Deleted")
                mail.expunge()
                print(f"[reply_matcher]   deleted {len(bounce_to_del)} bounce message(s) from IMAP")
            except Exception as ex:
                print(f"[reply_matcher]   IMAP bounce delete failed: {ex}")

    finally:
        try:
            mail.logout()
        except Exception:
            pass

    print(
        f"[reply_matcher]   {account_email}: "
        f"replies={counts['replies']} bounces={counts['bounces']} dmarc={counts['dmarc']} "
        f"matched={counts['matched']} bounced={counts['bounced']} "
        f"unmatched={counts['unmatched']} skipped={counts['skipped']} errors={counts['errors']}"
    )
    return counts


def _audit_log(db, message_id: str, doc: dict) -> None:
    """Write one inbox_messages document as an audit log entry (fire-and-forget)."""
    try:
        if message_id:
            existing = list(
                db.collection(_INBOX)
                .where(filter=FieldFilter("message_id", "==", message_id))
                .limit(1).stream()
            )
            if existing:
                existing[0].reference.update({
                    k: v for k, v in doc.items()
                    if k.startswith("match_")
                })
                return
        db.collection(_INBOX).add(doc)
    except Exception as ex:
        print(f"[reply_matcher]   ✗ audit log failed: {ex}")



# ── Step 2a: find contact for a reply ────────────────────────────────────────

def _find_contact(
    db,
    message: dict,
    campaign_filter: set[str] | None = None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Match the sender's email address against campaign_contacts.

    Returns (contact_doc_id, campaign_id, lead_id, matched_via) or all None.
    """
    from_addr = _bare(message.get("from_email") or message.get("from_address") or "")
    if not from_addr:
        return None, None, None, None

    # Normalize sender email to the doc_id used across the CRM
    contact_doc_id = _doc_id(from_addr)
    print(f"[reply_matcher]     [dbg] from={from_addr!r}  doc_id={contact_doc_id!r}")

    # Try 1: campaign_contacts collection-group by normalized doc_id
    try:
        cc_docs = list(
            db.collection_group(_CC)
            .where(filter=FieldFilter("doc_id", "==", contact_doc_id))
            .limit(5)
            .stream()
        )
        print(f"[reply_matcher]     [dbg] campaign_contacts by doc_id: {len(cc_docs)} hit(s)")
        for doc in cc_docs:
            cid = doc.reference.parent.parent.id
            if campaign_filter and cid not in campaign_filter:
                print(f"[reply_matcher]     [dbg] filtered out campaign {cid!r}")
                continue
            lead_id = (doc.to_dict() or {}).get("lead_id")
            return doc.id, cid, lead_id, "doc_id"
    except Exception as ex:
        print(f"[reply_matcher]     [dbg] doc_id query failed: {ex}")

    # Try 2: campaign_contacts by raw email field
    #        (contacts imported without a doc_id field set)
    try:
        cc_by_email = list(
            db.collection_group(_CC)
            .where(filter=FieldFilter("email", "==", from_addr))
            .limit(5)
            .stream()
        )
        print(f"[reply_matcher]     [dbg] campaign_contacts by email field: {len(cc_by_email)} hit(s)")
        for doc in cc_by_email:
            cid = doc.reference.parent.parent.id
            if campaign_filter and cid not in campaign_filter:
                print(f"[reply_matcher]     [dbg] filtered out campaign {cid!r}")
                continue
            lead_id = (doc.to_dict() or {}).get("lead_id")
            return doc.id, cid, lead_id, "email_field"
    except Exception as ex:
        print(f"[reply_matcher]     [dbg] email field query failed: {ex}")

    return None, None, None, None


# ── Step 2b: find contact for a bounce ───────────────────────────────────────

def _find_bounced_contact(
    db,
    message: dict,
    campaign_filter: set[str] | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Return (contact_doc_id, campaign_id, lead_id) or all None."""
    recipient = (message.get("original_recipient") or "").strip().lower()
    if not recipient:
        return None, None, None

    # Normalize to doc_id — same formula as the rest of the CRM
    contact_doc_id = _doc_id(recipient)

    # Try 1: campaign_contacts by normalized doc_id
    try:
        cc_docs = list(
            db.collection_group(_CC)
            .where(filter=FieldFilter("doc_id", "==", contact_doc_id))
            .limit(5)
            .stream()
        )
        for doc in cc_docs:
            cid = doc.reference.parent.parent.id
            if campaign_filter and cid not in campaign_filter:
                continue
            lead_id = (doc.to_dict() or {}).get("lead_id")
            return doc.id, cid, lead_id
    except Exception as ex:
        print(f"[reply_matcher]   bounce contact lookup (doc_id) failed: {ex}")

    # Try 2: campaign_contacts by raw email field
    try:
        cc_by_email = list(
            db.collection_group(_CC)
            .where(filter=FieldFilter("email", "==", recipient))
            .limit(5)
            .stream()
        )
        for doc in cc_by_email:
            cid = doc.reference.parent.parent.id
            if campaign_filter and cid not in campaign_filter:
                continue
            lead_id = (doc.to_dict() or {}).get("lead_id")
            return doc.id, cid, lead_id
    except Exception as ex:
        print(f"[reply_matcher]   bounce contact lookup (email) failed: {ex}")

    return None, None, None


# ── Step 3a: apply reply actions ──────────────────────────────────────────────

def _already_handled(cc_data: dict, message_id: str) -> bool:
    """Return True if this SMTP Message-ID is already recorded in comment_history."""
    if not message_id:
        return False
    history = cc_data.get("comment_history") or []
    handled = {
        str(e.get("message_id") or "").strip()
        for e in history
        if e.get("type") in ("EMAIL_IN", "BOUNCE")
    }
    return message_id.strip() in handled


def _apply_actions(
    db,
    contact_doc_id: str,
    campaign_id: str,
    lead_id: str | None,
    inbox_doc_id: str,
    message: dict,
    matched_via: str,
    dry_run: bool = False,
) -> None:
    from google.cloud.firestore_v1 import ArrayUnion
    received_at = message.get("received_at") or datetime.now(timezone.utc).isoformat()
    message_id  = (message.get("message_id") or "").strip()
    subject     = (message.get("subject") or "")
    from_addr   = _bare(message.get("from_email") or message.get("from_address") or "")

    reply_payload = {
        "replied_at":    received_at,
        "reply_snippet": (message.get("body_text") or "")[:2000],
        "reply_subject": subject,
        "reply_from":    message.get("from_email") or message.get("from_address"),
        "matched_via":   matched_via,
    }
    history_entry = {
        "date":       received_at,
        "user":       from_addr,
        "text":       f"Reply: {subject}" if subject else "Reply received",
        "type":       "EMAIL_IN",
        "message_id": message_id,
    }

    if dry_run:
        print(f"[reply_matcher]       action  → [dry-run] would set status=active "
              f"followup_status=replied  message_id={message_id!r}")
        if lead_id:
            print(f"[reply_matcher]       lead    → [dry-run] campaign_leads/{lead_id}: status=active")
        return "dry_run"

    try:
        cc_ref  = (db.collection(_CAMPAIGNS).document(campaign_id)
                     .collection(_CC).document(contact_doc_id))
        cc_snap = cc_ref.get()
        cc_data = (cc_snap.to_dict() or {}) if cc_snap.exists else {}

        # Idempotency — skip if this SMTP message_id is already in comment_history
        if _already_handled(cc_data, message_id):
            print(f"[reply_matcher]       action  → ALREADY HANDLED "
                  f"(message_id already in comment_history)")
            return "already_handled"

        current = cc_data.get("status", "pending")
        update  = {**reply_payload, "comment_history": ArrayUnion([history_entry])}
        if current == "pending":
            update["status"]          = "active"
            update["followup_status"] = "replied"
            status_change = "pending → active"
        elif current == "active":
            update["followup_status"] = "replied"
            status_change = "active (no change)"
        else:
            status_change = f"{current} (no change)"
        cc_ref.update(update)
        print(f"[reply_matcher]       action  → UPDATED  "
              f"campaign_contacts status={status_change}  followup_status=replied")
    except Exception as ex:
        print(f"[reply_matcher]       action  → ✗ campaign_contacts failed: {ex}")

    if lead_id:
        try:
            cl_ref  = (db.collection(_CAMPAIGNS).document(campaign_id)
                         .collection(_CL).document(lead_id))
            cl_snap = cl_ref.get()
            cl_status = (cl_snap.to_dict() or {}).get("status", "pending") if cl_snap.exists else "pending"
            if cl_status == "pending":
                cl_ref.update({"status": "active"})
                print(f"[reply_matcher]       lead    → UPDATED  "
                      f"campaign_leads/{lead_id}: pending → active")
            else:
                print(f"[reply_matcher]       lead    → no change  "
                      f"campaign_leads/{lead_id}: already {cl_status!r}")
        except Exception as ex:
            print(f"[reply_matcher]       lead    → ✗ update failed: {ex}")

    # email_contacts is intentionally not updated here.
    # inbox_messages is updated by the caller as an audit log

    try:
        refresh_campaign_stats(campaign_id)
    except Exception as ex:
        print(f"[reply_matcher]       action  → ✗ stats refresh failed: {ex}")

    return "updated"


# ── Step 3b: apply bounce actions ─────────────────────────────────────────────

def _apply_bounce_actions(
    db,
    contact_doc_id: str,
    campaign_id: str,
    lead_id: str | None,
    inbox_doc_id,       # kept for compat but unused — audit log written by caller
    message: dict,
    dry_run: bool = False,
):
    from google.cloud.firestore_v1 import ArrayUnion
    received_at   = message.get("received_at") or datetime.now(timezone.utc).isoformat()
    message_id    = (message.get("message_id") or "").strip()
    bounce_reason = (message.get("subject") or "delivery failure")[:200]

    history_entry = {
        "date":       received_at,
        "user":       "system",
        "text":       f"Bounce: {bounce_reason}",
        "type":       "BOUNCE",
        "message_id": message_id,
    }

    if dry_run:
        print(f"[reply_matcher]       action  → [dry-run] would set status=excluded "
              f"bounce_detected=True  message_id={message_id!r}")
        return "dry_run"

    try:
        cc_ref  = (db.collection(_CAMPAIGNS).document(campaign_id)
                     .collection(_CC).document(contact_doc_id))
        cc_snap = cc_ref.get()
        cc_data = (cc_snap.to_dict() or {}) if cc_snap.exists else {}

        # Idempotency — skip if this SMTP message_id is already in comment_history
        if _already_handled(cc_data, message_id):
            print(f"[reply_matcher]       action  → ALREADY HANDLED "
                  f"(message_id already in comment_history)")
            return "already_handled"

        current = cc_data.get("status", "pending")
        update  = {"comment_history": ArrayUnion([history_entry])}
        # Only a still-pending contact is excluded on bounce.
        # active / replied / any other status is left as-is — bounce is
        # recorded in comment_history only.
        if current == "pending":
            update.update({
                "status":          "excluded",
                "bounce_detected": True,
                "bounced_at":      received_at,
                "bounce_reason":   bounce_reason,
            })
            print(f"[reply_matcher]       action  → UPDATED  "
                  f"campaign_contacts status=pending → excluded  bounce_detected=True")
        else:
            print(f"[reply_matcher]       action  → no status change  "
                  f"campaign_contacts status={current!r} (bounce noted in history only)")
        cc_ref.update(update)
    except Exception as ex:
        print(f"[reply_matcher]   ✗ campaign_contacts bounce update failed {campaign_id}/{contact_doc_id}: {ex}")

    # email_contacts is intentionally not updated here.
    # inbox_messages is updated by the caller as an audit log

    try:
        refresh_campaign_stats(campaign_id)
    except Exception as ex:
        print(f"[reply_matcher]       action  → ✗ stats refresh failed: {ex}")


# ── Step 4: match loop ────────────────────────────────────────────────────────




# ── Public entry point ────────────────────────────────────────────────────────

def match_new_replies(
    limit: int = 200,
    accounts: list[str] | None = None,
    campaigns: list[str] | None = None,
    dry_run: bool = False,
    days: int = 30,
) -> dict:
    """Fetch emails directly from IMAP, classify, and match to campaign_contacts
    in a single pass per account.  inbox_messages is written only as an audit log.

    accounts:  restrict to these account emails (None = all)
    campaigns: restrict matching to these campaign IDs (None = all)
    dry_run:   find matches and print what would change; write nothing
    days:      how many days back to look in IMAP SINCE filter (default 30)
    limit:     max messages to process per account
    """
    db = get_firestore()
    campaign_filter = set(campaigns) if campaigns else None

    ma_col   = db.collection(_SETTINGS).document(_MAIL_ACCS).collection("accounts")
    all_accs = {d.id: d.to_dict() for d in ma_col.stream()}
    if not all_accs:
        print("[reply_matcher] ⚠  No mail accounts found in settings")
        return {"accounts_checked": 0, "accounts_failed": 0,
                "replies": 0, "bounces": 0, "dmarc": 0,
                "matched": 0, "bounced": 0, "unmatched": 0, "errors": 0}

    filter_set = {a.strip().lower() for a in accounts} if accounts else None
    to_check   = {k: v for k, v in all_accs.items()
                  if filter_set is None or k.lower() in filter_set}
    if not to_check:
        print(f"[reply_matcher] ⚠  No accounts matched filter: {filter_set}")
        return {"accounts_checked": 0, "accounts_failed": 0,
                "replies": 0, "bounces": 0, "dmarc": 0,
                "matched": 0, "bounced": 0, "unmatched": 0, "errors": 0}

    own_domains = {e.split("@", 1)[1].lower() for e in all_accs if "@" in e}

    totals = {"accounts_checked": len(to_check), "accounts_failed": 0,
              "replies": 0, "bounces": 0, "dmarc": 0,
              "matched": 0, "bounced": 0, "unmatched": 0, "skipped": 0, "errors": 0}

    for account_email, acc in to_check.items():
        try:
            counts = _process_account(
                db, account_email, acc,
                campaign_filter=campaign_filter,
                days=days, limit=limit, dry_run=dry_run,
                own_domains=own_domains,
            )
            for k in ("replies", "bounces", "dmarc", "matched", "bounced", "unmatched", "skipped", "errors"):
                totals[k] += counts.get(k, 0)
        except Exception as ex:
            totals["accounts_failed"] += 1
            print(f"[reply_matcher] ✗  IMAP error for {account_email!r}: {ex}")

    print(
        f"\n[reply_matcher] Done — "
        f"accounts={totals['accounts_checked']} failed={totals['accounts_failed']} "
        f"replies={totals['replies']} bounces={totals['bounces']} dmarc={totals['dmarc']} "
        f"matched={totals['matched']} bounced={totals['bounced']} "
        f"unmatched={totals['unmatched']} skipped={totals['skipped']} errors={totals['errors']}"
    )
    return {**totals, "dry_run": dry_run}
