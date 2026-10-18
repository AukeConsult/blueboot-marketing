# functions-crm/smart_mail/reply_matcher.py
# Full reply pipeline: fetch from IMAP → classify → find contact → apply actions.
# Single entry point: match_new_replies() — called identically from the HTTP
# worker, Cloud Task, and the app/reply_match.py CLI.

from __future__ import annotations

import imaplib
import email as _email_lib
from email.header import decode_header
from datetime import datetime, timedelta, timezone
import re

from .firestore_client import get_firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from .outreach_stats import refresh_campaign_stats


_MSGID_RE = re.compile(r"<[^<>\s]+>")
from crm.contact_clean_lib import find_emails          # the one e-mail detector

_SETTINGS  = "settings"
_MAIL_ACCS = "mail_accounts"
_INBOX     = "inbox_messages"
_OUTREACH  = "outreach_sent"
_CAMPAIGNS = "campaigns"
_CC        = "campaign_contacts"
_CL        = "campaign_leads"
_PROCESSED_FOLDER = "Processed"      # target folder when _MOVE_REPLIES is on
_MOVE_REPLIES = False             # False: handled replies stay in the INBOX (they are skipped next run via the audit log)
imaplib.Commands.setdefault("MOVE", ("SELECTED",))     # lets mail.uid("MOVE", ...) work (RFC 6851)


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
    found = find_emails(value)
    return found[0] if found else ""


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


# Caps for body text stored in comment_history (Firestore doc stays small)
_BODY_TEXT_CAP = 10000
_BODY_HTML_CAP = 30000


def _extract_bodies(msg) -> tuple[str, str]:
    """Return (plain_text, html) bodies from a message. Either may be ''.

    Walks the MIME tree and takes the first non-attachment text/plain part and
    the first non-attachment text/html part.
    """
    text, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if "attachment" in str(part.get("Content-Disposition") or ""):
                continue
            if ct == "text/plain" and not text:
                payload = part.get_payload(decode=True)
                if payload:
                    text = payload.decode(errors="ignore")
            elif ct == "text/html" and not html:
                payload = part.get_payload(decode=True)
                if payload:
                    html = payload.decode(errors="ignore")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(errors="ignore")
            if msg.get_content_type() == "text/html":
                html = body
            else:
                text = body
    return text, html


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
    """The most likely ORIGINAL recipient of a bounce (first of _bounce_candidates), or None."""
    found = _bounce_candidates(msg, sender_addr)
    return found[0] if found else None


def _bounce_candidates(msg, sender_addr: str = "") -> list[str]:
    """Best-effort recovery of the ORIGINAL intended recipient of a bounce, as a ranked list.

    The first address is the one the mail server says FAILED, which is not always the contact we
    mailed: when the contact's mailbox forwards to another address, the failed address is the
    forward target. So the caller tries every candidate in turn.

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

            elif ct == "text/rfc822-headers":   # Exim often returns only the headers of the original
                pb = part.get_payload(decode=True)
                if pb:
                    v = _email_lib.message_from_bytes(pb).get("To")
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
    for addr in find_emails(body):
        add(3, addr)

    found.sort(key=lambda t: t[0])
    ranked: list[str] = []
    for _pri, addr in found:
        if addr not in ranked:
            ranked.append(addr)
    return ranked


# A bounce that says the ADDRESS itself is bad (unknown user / mailbox / domain). Only these exclude a
# contact: a block of OUR mail (5.7.x spam / reputation / policy) or a full mailbox says nothing about
# whether the address is valid.
_UNKNOWN_ADDRESS_RE = re.compile(
    r"\b5\.1\.[1236]\b|user unknown|unknown user|unknown (recipient|address|mailbox)"
    r"|no such (user|recipient|mailbox|address)|invalid (recipient|address|mailbox)"
    r"|mailbox (not found|unavailable|does not exist|doesn't exist)"
    r"|(recipient|address|mailbox|user|account) (does not exist|doesn't exist|not found)"
    r"|unrouteable address|(host or )?domain (name )?(not found|does not exist)|\bno mx\b",
    re.IGNORECASE)
# Follow-up statuses an incoming reply may overwrite with "replied" ("received" is the retired name).
_REPLY_OVERWRITES = ("", "contacted", "auto_replied", "received", "replied")
# Automatic replies (out of office, vacation, "we received your mail"): a machine answered, not the person.
_AUTO_SUBJECT_RE = re.compile(
    r"^\s*(auto(matic|matisk|matische|mated)?[ -]?(reply|response|svar|antwort|respuesta)"
    r"|autosvar|autoreply|auto-reply|out of (the )?office|ooo\b|away from (my )?(desk|office)"
    r"|on (annual |maternity |paternity )?(leave|vacation|holiday)|vacation reply"
    r"|abwesenheit|abwesend|automatische antwort|frav[æe]r|fravaer|ferie|ikke til stede|"
    r"r[ée]ponse automatique|absence du bureau|respuesta autom[áa]tica|risposta automatica)",
    re.IGNORECASE)


def _is_auto_reply(msg, subject: str = "") -> bool:
    """True for an automatic reply: Auto-Submitted / autoreply headers, bulk precedence, or a typical subject."""
    try:
        auto = str(msg.get("Auto-Submitted", "") or "").strip().lower()
        if auto and auto != "no":
            return True
        for h in ("X-Autoreply", "X-Autorespond", "X-Auto-Response-Suppress"):
            if msg.get(h):
                return True
        if str(msg.get("Precedence", "") or "").strip().lower() in ("auto_reply", "auto-reply", "bulk", "junk"):
            return True
    except Exception:
        pass
    return bool(_AUTO_SUBJECT_RE.search(subject or ""))


_SMTP_REASON_RE = re.compile(r"\b[45]\d\d[ -]\d\.\d{1,3}\.\d{1,3}[^\r\n]{0,160}")


def _bounce_text(msg) -> str:
    """Readable text of a bounce: its plain-text parts and the delivery-status part."""
    parts: list[str] = []
    for part in msg.walk():
        ct = part.get_content_type()
        if ct == "text/plain":
            pb = part.get_payload(decode=True)
            if pb:
                parts.append(pb.decode(errors="ignore"))
        elif ct == "message/delivery-status":
            parts.append(str(part))
    return "\n".join(parts)


def _bounce_kind(text: str) -> str:
    """'unknown_address' when the bounce says the address does not exist, else 'other' (blocked, full...)."""
    return "unknown_address" if _UNKNOWN_ADDRESS_RE.search(text or "") else "other"


def _bounce_reason(text: str, subject: str) -> str:
    m = _SMTP_REASON_RE.search(text or "")
    return (m.group(0).strip() if m else (subject or "delivery failure"))[:200]


_OUR_MSGID_RE = re.compile(r"<[0-9a-f]{32}@[\w.-]+>")


def _our_message_ids(raw_text: str) -> list[str]:
    """Message-IDs of mails WE sent that are quoted inside a bounce (ours are <32 hex chars@domain>)."""
    out: list[str] = []
    for m in _OUR_MSGID_RE.findall(raw_text or ""):
        if m not in out:
            out.append(m)
    return out[:5]

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


# ── Faster mailbox reading ────────────────────────────────────────────────────
# 1. Headers only (Message-ID + size) for every mail; mails already in the audit log are not downloaded.
# 2. The rest is downloaded in batches, not one request per mail.
# 3. A big mail is downloaded only as far as _MAX_MAIL_BYTES (text and headers come first; we keep
#    only the text/HTML bodies, capped, and never the attachments).
_FETCH_BATCH        = 20
_HEADER_BATCH       = 100
_MAX_MAIL_BYTES     = 500_000
_UNMATCHED_RETRY_HOURS = 24     # an unmatched / skipped mail is looked at again after this long
_HANDLED = ("updated", "already_handled")
_WAITS   = ("unmatched", "system_skipped")      # outcomes that are only retried after the waiting time


def _parse_fetch(data) -> list[tuple[str, bytes, bytes]]:
    """(uid, payload, fetch-info) for every message in an IMAP FETCH answer.
    The UID and size sit either in the first line or in the line that closes the item."""
    out: list[list] = []
    for item in data or []:
        if isinstance(item, tuple):
            out.append([item[0], item[1]])
        elif isinstance(item, bytes) and out:
            out[-1][0] += b" " + item
    res = []
    for info, payload in out:
        m = re.search(rb"UID\s+(\d+)", info)
        if m:
            res.append((m.group(1).decode(), payload, info))
    return res


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _plan_mailbox(db, mail, uids, dry_run, recheck, to_move, bounce_del, dmarc_del, counts) -> tuple[list, dict]:
    """Decide which mails must be downloaded. Mails already in the audit log (inbox_messages) are not:
    handled ones just go to the cleanup lists (their move/delete is retried), unmatched or skipped ones
    are looked at again only after _UNMATCHED_RETRY_HOURS (or always with recheck=True)."""
    info: dict[str, tuple[str, int]] = {}
    for chunk in _chunks(uids, _HEADER_BATCH):
        try:
            _t, data = mail.uid("FETCH", b",".join(chunk).decode(),
                                "(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            for uid, payload, meta in _parse_fetch(data):
                sz = re.search(rb"RFC822\.SIZE\s+(\d+)", meta)
                mid = (_email_lib.message_from_bytes(payload or b"").get("Message-ID") or "").strip()
                info[uid] = (mid, int(sz.group(1)) if sz else 0)
        except Exception as ex:
            print(f"[reply_matcher]   header fetch failed ({ex}) — these mails are downloaded in full")

    audit: dict[str, dict] = {}
    mids = sorted({m for m, _ in info.values() if m})
    for chunk in _chunks(mids, 30):         # Firestore allows 30 values in an "in" filter
        try:
            for d in (db.collection(_INBOX).where(filter=FieldFilter("message_id", "in", chunk)).stream()):
                row = d.to_dict() or {}
                old = audit.get(row.get("message_id"))
                if not old or row.get("match_outcome") in _HANDLED:
                    audit[row.get("message_id")] = row
        except Exception as ex:
            print(f"[reply_matcher]   audit lookup failed ({ex}) — these mails are downloaded in full")

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=0 if recheck else _UNMATCHED_RETRY_HOURS)).isoformat()
    todo = []
    for uid in uids:
        mid, _size = info.get(uid.decode(), ("", 0))
        a = audit.get(mid) if mid else None
        if not a:
            todo.append(uid)
            continue
        outcome, mtype = a.get("match_outcome"), a.get("message_type")
        if mtype == "dmarc_report":
            if not dry_run:
                dmarc_del.append(uid)
        elif outcome in _HANDLED:
            if not dry_run:
                (bounce_del if mtype == "bounce" else to_move).append(uid)
        elif (outcome not in _WAITS
              or str(a.get("match_checked_at") or a.get("received_at") or "") < cutoff):
            todo.append(uid)    # failed last time (error / unknown outcome), or unmatched long ago: look again
            continue
        counts["seen_skipped"] += 1
    sizes = {u: info.get(u.decode(), ("", 0))[1] for u in todo}
    return todo, sizes


def _download(mail, todo, sizes):
    """Yield (uid, raw bytes) for the mails to read, in batches; big mails only their first part."""
    big = {u for u in todo if sizes.get(u, 0) > _MAX_MAIL_BYTES}
    small = [u for u in todo if u not in big]
    got: dict[bytes, bytes] = {}
    pos = 0
    for uid in todo:
        if uid in big:
            _t, data = mail.uid("FETCH", uid.decode(), f"(UID BODY.PEEK[]<0.{_MAX_MAIL_BYTES}>)")
            rows = _parse_fetch(data)
            if rows:
                yield uid, rows[0][1]
            continue
        if uid not in got:
            batch = small[pos:pos + _FETCH_BATCH]
            pos += len(batch)
            _t, data = mail.uid("FETCH", b",".join(batch).decode(), "(UID BODY.PEEK[])")
            for u, payload, _m in _parse_fetch(data):
                got[u.encode()] = payload
            for u in batch:                     # a server that answered a mail oddly: ask for it alone
                if u not in got:
                    _t, d1 = mail.uid("FETCH", u.decode(), "(UID BODY.PEEK[])")
                    r1 = _parse_fetch(d1)
                    if r1:
                        got[u] = r1[0][1]
        if uid in got:
            yield uid, got.pop(uid)


def _process_account(
    db,
    account_email: str,
    acc: dict,
    campaign_filter: set[str] | None,
    days: int,
    limit: int,
    dry_run: bool,
    own_domains: set[str] | None = None,
    recheck: bool = False,
) -> dict:
    """Fetch messages from one IMAP account and match each directly against
    campaign_contacts.  inbox_messages is written as an audit log only."""
    imap_host = str(acc.get("imap_host") or acc.get("host") or "").strip()
    imap_port = int(acc.get("imap_port") or acc.get("port") or 993)
    user      = str(acc.get("username") or acc.get("email") or account_email).strip()
    password  = acc.get("password", "")

    counts = {"replies": 0, "bounces": 0, "dmarc": 0,
              "matched": 0, "bounced": 0, "unmatched": 0, "skipped": 0, "errors": 0, "seen_skipped": 0}

    if not imap_host:
        print(f"[reply_matcher]   {account_email}: no imap_host — skipped")
        return counts

    print(f"\n[reply_matcher] → {account_email}  ({imap_host}:{imap_port})  last {days}d")
    mail          = imaplib.IMAP4_SSL(imap_host, imap_port)
    dmarc_to_del:  list[bytes] = []
    bounce_to_del: list[bytes] = []
    reply_to_move: list[bytes] = []

    try:
        mail.login(user, password)
        mail.select("INBOX")
        since = _imap_since(days)
        _status, messages = mail.uid("SEARCH", None, f"SINCE {since}")
        email_ids = messages[0].split()
        print(f"[reply_matcher]   {len(email_ids)} message(s) since {since}")

        todo, sizes = _plan_mailbox(db, mail, email_ids, dry_run, recheck,
                                    reply_to_move, bounce_to_del, dmarc_to_del, counts)
        print(f"[reply_matcher]   {len(todo)} to read, {counts['seen_skipped']} already seen (not downloaded)")

        processed = 0
        for eid, raw in _download(mail, todo, sizes):
            if processed >= limit:
                print(f"[reply_matcher]   limit={limit} reached — stopping")
                break
            try:
                msg      = _email_lib.message_from_bytes(raw)

                message_id  = (msg.get("Message-ID") or "").strip()
                in_reply_to = msg.get("In-Reply-To")
                references  = msg.get("References")
                subject     = _decode_mime(msg.get("Subject"))
                from_raw    = _decode_mime(msg.get("From"))
                to_raw      = _decode_mime(msg.get("To"))
                from_addr   = _bare(from_raw)
                email_date  = (msg.get("Date") or "").strip()
                _body_text, _body_html = _extract_bodies(msg)
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
                    if not dry_run:
                        _audit_log(db, message_id, {
                            "account": account_email, "from_email": from_raw, "subject": subject,
                            "received_at": received_at, "message_id": message_id,
                            "message_type": message_type, "match_outcome": "system_skipped"})
                    processed += 1
                    continue

                # ── Build message dict for matching ───────────────────────────
                message = {
                    "account":            account_email,
                    "from_email":         from_raw,
                    "from_address":       from_addr,
                    "to_email":           _bare(to_raw),
                    "subject":            subject,
                    "body_text":          _body_text[:_BODY_TEXT_CAP],
                    "body_html":          _body_html[:_BODY_HTML_CAP],
                    "received_at":        received_at,
                    "message_id":         message_id,
                    "in_reply_to":        in_reply_to,
                    "references":         references,
                    "imap_uid":           eid.decode(),     # eid is the IMAP UID (stable, unlike sequence numbers)
                    "message_type":       message_type,
                    "original_recipient": orig_rcpt,
                    "bounce_candidates":  _bounce_candidates(msg, from_addr) if message_type == "bounce" else [],
                    "bounce_kind":        _bounce_kind(_bounce_text(msg)) if message_type == "bounce" else "",
                    "bounce_reason":      _bounce_reason(_bounce_text(msg), subject) if message_type == "bounce" else "",
                    "original_message_ids": (_our_message_ids(raw.decode("utf-8", "ignore"))
                                             if message_type == "bounce" else []),
                    "email_date":         email_date,
                    "auto_reply":         message_type == "reply" and _is_auto_reply(msg, subject),
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
                            # Only a bounce that was applied (or already was) is deleted
                            if outcome in ("updated", "already_handled"):
                                bounce_to_del.append(eid)
                    else:
                        print(f"[reply_matcher]     ✗ NO MATCH  (kept in mailbox)")
                        counts["unmatched"] += 1
                        if not dry_run:
                            _audit_log(db, message_id, {**message, "match_outcome": "unmatched"})

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
                            # The reply is saved on the contact. It stays in the INBOX (unless _MOVE_REPLIES);
                            # the audit log makes the next run skip it.
                            if outcome in ("updated", "already_handled"):
                                reply_to_move.append(eid)
                    else:
                        print(f"[reply_matcher]     ✗ NO MATCH")
                        counts["unmatched"] += 1
                        if not dry_run:
                            _audit_log(db, message_id, {**message, "match_outcome": "unmatched"})

            except Exception as ex:
                counts["errors"] += 1
                print(f"[reply_matcher]     ✗ ERROR  {ex}")

        # One cleanup pass, by UID (UIDs do not shift when mails are removed).
        if not dry_run:
            moved = flagged = 0
            folder = _PROCESSED_FOLDER
            if reply_to_move and _MOVE_REPLIES:
                folder, exists = _processed_folder(mail)
                if not exists:
                    try:
                        typ, resp = mail.create(f'"{folder}"')
                        print(f"[reply_matcher]   create folder {folder!r}: {typ} {resp}")
                    except Exception as ex:
                        print(f"[reply_matcher]   create folder {folder!r} failed: {ex}")
                try:
                    mail.subscribe(f'"{folder}"')    # unsubscribed folders are hidden in many mail clients
                except Exception as ex:
                    print(f"[reply_matcher]   subscribe {folder!r} failed: {ex}")
                can_move = "MOVE" in (getattr(mail, "capabilities", ()) or ())
                for eid in reply_to_move:
                    try:
                        if can_move:
                            # Real IMAP MOVE: the server copies and removes in one step.
                            typ, resp = mail.uid("MOVE", eid, f'"{folder}"')
                        else:
                            # No MOVE support: copy, then flag as deleted ONLY if the copy worked.
                            typ, resp = mail.uid("COPY", eid, f'"{folder}"')
                            if typ == "OK":
                                mail.uid("STORE", eid, "+FLAGS", "\\Deleted")
                                flagged += 1
                        if typ == "OK":
                            moved += 1
                        else:
                            print(f"[reply_matcher]   move to {folder!r} refused ({resp}) — reply stays in INBOX")
                    except Exception as ex:
                        print(f"[reply_matcher]   move to {folder!r} failed: {ex}")
            try:
                for eid in dmarc_to_del + bounce_to_del:
                    mail.uid("STORE", eid, "+FLAGS", "\\Deleted")
                    flagged += 1
                if flagged:
                    mail.expunge()
                if moved or flagged:
                    print(f"[reply_matcher]   cleaned INBOX: moved {moved} repl(ies) to {folder!r}, "
                          f"deleted {len(dmarc_to_del)} DMARC report(s) and {len(bounce_to_del)} bounce(s)")
            except Exception as ex:
                print(f"[reply_matcher]   IMAP cleanup failed: {ex}")

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


def _processed_folder(mail) -> tuple[str, bool]:
    """(folder name, already exists) for the Processed folder on this server.

    Servers that keep their folders under INBOX (cPanel/Dovecot: INBOX.Sent) only accept
    INBOX.Processed; a plain "Processed" is refused there.
    """
    names: list[str] = []
    delim = "/"
    try:
        _typ, items = mail.list()
        for it in items or []:
            raw = it.decode(errors="replace") if isinstance(it, bytes) else str(it)
            m = re.match(r'\((?P<fl>[^)]*)\)\s+(?:"(?P<d>[^"]*)"|NIL)\s+(?P<n>.+)$', raw)
            if not m:
                continue
            delim = m.group("d") or delim
            names.append(m.group("n").strip().strip('"'))
    except Exception as ex:
        print(f"[reply_matcher]   could not list folders: {ex}")
    for n in names:
        if n.lower() in (_PROCESSED_FOLDER.lower(), f"inbox{delim}{_PROCESSED_FOLDER}".lower()):
            return n, True
    if any(n.startswith(f"INBOX{delim}") for n in names):
        return f"INBOX{delim}{_PROCESSED_FOLDER}", False
    return _PROCESSED_FOLDER, False


def _audit_log(db, message_id: str, doc: dict) -> None:
    """Write one inbox_messages document as an audit log entry (fire-and-forget).
    match_checked_at says when the mail was last looked at (an unmatched mail is retried after a day)."""
    doc = {**doc, "match_checked_at": datetime.now(timezone.utc).isoformat()}
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
    """Return (contact_doc_id, campaign_id, lead_id) or all None.

    1. Exact: the Message-ID of OUR mail quoted in the bounce -> outreach_sent -> that contact.
       This also works when the contact's mailbox forwards elsewhere, so the failed address is
       not the address we mailed.
    2. Otherwise every recovered address (failed address first, then the original To:) is looked
       up in campaign_contacts, by doc_id and then by the email field.
    """
    for mid in message.get("original_message_ids") or []:
        try:
            for sdoc in (db.collection(_OUTREACH)
                         .where(filter=FieldFilter("message_id", "==", mid)).limit(1).stream()):
                sd = sdoc.to_dict() or {}
                cid, did = sd.get("campaign_id"), sd.get("contact_doc_id")
                if cid and did and (not campaign_filter or cid in campaign_filter):
                    cc = (db.collection(_CAMPAIGNS).document(cid).collection(_CC).document(did).get())
                    lead_id = ((cc.to_dict() or {}).get("lead_id")) if cc.exists else None
                    print(f"[reply_matcher]     matched by original Message-ID {mid}")
                    return did, cid, lead_id
        except Exception as ex:
            print(f"[reply_matcher]   bounce lookup by Message-ID failed: {ex}")

    recipients = list(message.get("bounce_candidates") or [])
    first = (message.get("original_recipient") or "").strip().lower()
    if first and first not in recipients:
        recipients.insert(0, first)

    for recipient in recipients:
        contact_doc_id = _doc_id(recipient)         # same formula as the rest of the CRM
        for field, value, label in (("doc_id", contact_doc_id, "doc_id"), ("email", recipient, "email")):
            try:
                docs = list(db.collection_group(_CC)
                            .where(filter=FieldFilter(field, "==", value)).limit(5).stream())
                for doc in docs:
                    cid = doc.reference.parent.parent.id
                    if campaign_filter and cid not in campaign_filter:
                        continue
                    return doc.id, cid, (doc.to_dict() or {}).get("lead_id")
            except Exception as ex:
                print(f"[reply_matcher]   bounce contact lookup ({label}) failed: {ex}")
    return None, None, None


# ── Step 3a: apply reply actions ──────────────────────────────────────────────

def _entry_fingerprint(entry: dict) -> str:
    """Stable fingerprint for an inbound history entry, used to dedupe messages
    that arrive WITHOUT a Message-ID. Built from fields that are identical
    across re-fetches of the same email: type, sender, the history text (which
    embeds the subject), and the email's own Date header."""
    return "|".join([
        str(entry.get("type") or ""),
        str(entry.get("user") or "").strip().lower(),
        str(entry.get("text") or "").strip(),
        str(entry.get("email_date") or "").strip(),
    ])


def _already_handled(cc_data: dict, message_id: str,
                     fingerprint: str | None = None) -> bool:
    """Return True if this inbound message is already recorded in
    comment_history, so the contact is not updated twice.

    Primary key is the SMTP Message-ID. When the message has no Message-ID,
    fall back to a (type|sender|text|email_date) fingerprint so a re-fetched
    reply/bounce with no Message-ID is still not applied again.
    """
    history = [
        e for e in (cc_data.get("comment_history") or [])
        if e.get("type") in ("EMAIL_IN", "BOUNCE")
    ]
    mid = (message_id or "").strip()
    if mid:
        return any(str(e.get("message_id") or "").strip() == mid for e in history)
    if fingerprint:
        return any(_entry_fingerprint(e) == fingerprint for e in history)
    return False


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
        "text":       (f"Auto-reply: {subject}" if message.get("auto_reply") else f"Reply: {subject}") if subject
                      else ("Auto-reply received" if message.get("auto_reply") else "Reply received"),
        "type":       "EMAIL_IN",
        "message_id": message_id,
        "email_date": message.get("email_date", ""),
        "body_text":  (message.get("body_text") or "")[:_BODY_TEXT_CAP],
        "body_html":  (message.get("body_html") or "")[:_BODY_HTML_CAP],
    }

    # Read the contact once — needed for both the idempotency guard and the
    # status transition.
    cc_ref = (db.collection(_CAMPAIGNS).document(campaign_id)
                .collection(_CC).document(contact_doc_id))
    try:
        cc_snap = cc_ref.get()
        cc_data = (cc_snap.to_dict() or {}) if cc_snap.exists else {}
    except Exception as ex:
        print(f"[reply_matcher]       action  → ✗ campaign_contacts read failed: {ex}")
        cc_data = {}

    # Idempotency — NEVER update a contact whose history already records this
    # reply. Checked before the dry-run preview so the rule is honoured and
    # visible in both dry-run and live runs.
    if _already_handled(cc_data, message_id, _entry_fingerprint(history_entry)):
        print(f"[reply_matcher]       action  → ALREADY HANDLED "
              f"(reply already in comment_history)")
        # Repair: a reply stored by an older run may never have been marked "replied".
        if not dry_run and (cc_data.get("followup_status") or "").strip().lower() in ("", "contacted"):
            try:
                fixed = "auto_replied" if message.get("auto_reply") else "replied"
                cc_ref.update({"followup_status": fixed, "last_updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                print(f"[reply_matcher]       action  → followup_status set to {fixed} (repair)")
            except Exception as ex:
                print(f"[reply_matcher]       action  → ✗ repair failed: {ex}")
        return "already_handled"

    if dry_run:
        print(f"[reply_matcher]       action  → [dry-run] would set status=active "
              f"followup_status=replied  message_id={message_id!r}")
        if lead_id:
            print(f"[reply_matcher]       lead    → [dry-run] campaign_leads/{lead_id}: status=active")
            print(f"[reply_matcher]       lead    → [dry-run] site followup_status=replied if empty/contacted")
        return "dry_run"

    try:
        current = cc_data.get("status", "pending")
        update  = {**reply_payload, "comment_history": ArrayUnion([history_entry])}
        update["new_mail"] = True   # flag the contact as having new incoming mail
        update["last_updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # A reply is marked "replied", an automatic reply "auto_replied" (whatever the contact status), but never
        # over a later stage somebody set by hand (meeting, offer, in work, not interested). An auto-reply
        # never downgrades "replied" and leaves the contact status alone. A real reply turns pending into active.
        is_auto = bool(message.get("auto_reply"))
        new_fu  = "auto_replied" if is_auto else "replied"
        cur_fu  = (cc_data.get("followup_status") or "").strip().lower()
        if cur_fu in (("", "contacted") if is_auto else _REPLY_OVERWRITES):
            update["followup_status"] = new_fu
        if update.get("followup_status") == "auto_replied":
            # the person has not really answered: stay (or go back to) pending so reminders continue
            if current != "excluded":
                update["status"] = "pending"          # always written on the contact document
                status_change = f"{current} → pending"
            else:
                status_change = "excluded (no change)"
        elif current == "pending" and not is_auto:
            update["status"] = "active"
            status_change = "pending → active"
        else:
            status_change = f"{current} (no change)"
        cc_ref.update(update)
        print(f"[reply_matcher]       action  → UPDATED  "
              f"campaign_contacts status={status_change}  followup_status={update.get('followup_status', cur_fu)}")
    except Exception as ex:
        print(f"[reply_matcher]       action  → ✗ campaign_contacts failed: {ex}")
        return "error"          # the reply stays in the INBOX and is retried next run

    # Site follow-up: a mail came in -> "replied", but only over an empty / "contacted" status.
    if lead_id:
        from smart_mail.site_followup import set_site_followup
        if set_site_followup(db, campaign_id, lead_id, new_fu, ("", "contacted") if is_auto else ("", "contacted", "auto_replied", "received"), received_at):
            print(f"[reply_matcher]       lead    → followup_status={new_fu}  campaign_leads/{lead_id}")

    if lead_id:
        try:
            cl_ref  = (db.collection(_CAMPAIGNS).document(campaign_id)
                         .collection(_CL).document(lead_id))
            cl_snap = cl_ref.get()
            cl_status = (cl_snap.to_dict() or {}).get("status", "pending") if cl_snap.exists else "pending"
            if cl_status == "pending":
                cl_ref.update({"status": "active", "last_updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
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
    bounce_reason = (message.get("bounce_reason") or message.get("subject") or "delivery failure")[:200]
    # Only an unknown address excludes the contact. Mails that do not say so (older callers) count as such.
    address_failed = (message.get("bounce_kind") or "unknown_address") == "unknown_address"

    history_entry = {
        "date":       received_at,
        "user":       "system",
        "text":       (f"Bounce: {bounce_reason}" if address_failed
                       else f"Bounce (delivery blocked, address NOT excluded): {bounce_reason}"),
        "type":       "BOUNCE",
        "message_id": message_id,
        "email_date": message.get("email_date", ""),
    }

    # Read the contact once — for the idempotency guard and the status check.
    cc_ref = (db.collection(_CAMPAIGNS).document(campaign_id)
                .collection(_CC).document(contact_doc_id))
    try:
        cc_snap = cc_ref.get()
        cc_data = (cc_snap.to_dict() or {}) if cc_snap.exists else {}
    except Exception as ex:
        print(f"[reply_matcher]   ✗ campaign_contacts bounce read failed {campaign_id}/{contact_doc_id}: {ex}")
        cc_data = {}

    # Idempotency — NEVER update a contact whose history already records this
    # bounce. Checked before the dry-run preview so the rule is honoured in both.
    if _already_handled(cc_data, message_id, _entry_fingerprint(history_entry)):
        print(f"[reply_matcher]       action  → ALREADY HANDLED "
              f"(bounce already in comment_history)")
        return "already_handled"

    if dry_run:
        print(f"[reply_matcher]       action  → [dry-run] would set status=excluded "
              f"bounce_detected=True  message_id={message_id!r}")
        return "dry_run"

    try:
        current = cc_data.get("status", "pending")
        update  = {"comment_history": ArrayUnion([history_entry])}
        # A contact whose ADDRESS failed is excluded on bounce, whether pending or active
        # (a mailed contact is "active", so it must be covered). Bounces that are not an address
        # failure (our mail was blocked, mailbox full ...) are only recorded in comment_history.
        if current != "excluded" and address_failed:
            update.update({
                "status":          "excluded",
                "bounce_detected": True,
                "bounced_at":      received_at,
                "bounce_reason":   bounce_reason,
                "last_updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            print(f"[reply_matcher]       action  → UPDATED  "
                  f"campaign_contacts status={current!r} → excluded  bounce_detected=True")
        else:
            print(f"[reply_matcher]       action  → no status change  "
                  f"campaign_contacts status={current!r} (bounce noted in history only)")
        cc_ref.update(update)
    except Exception as ex:
        print(f"[reply_matcher]   ✗ campaign_contacts bounce update failed {campaign_id}/{contact_doc_id}: {ex}")
        return "error"

    # email_contacts is intentionally not updated here.
    # inbox_messages is updated by the caller as an audit log

    try:
        refresh_campaign_stats(campaign_id)
    except Exception as ex:
        print(f"[reply_matcher]       action  → ✗ stats refresh failed: {ex}")

    return "updated"


# ── Step 4: match loop ────────────────────────────────────────────────────────




# ── Public entry point ────────────────────────────────────────────────────────

def match_new_replies(
    limit: int = 200,
    accounts: list[str] | None = None,
    campaigns: list[str] | None = None,
    dry_run: bool = False,
    days: int = 30,
    recheck: bool = False,
) -> dict:
    """Fetch emails directly from IMAP, classify, and match to campaign_contacts
    in a single pass per account.  inbox_messages is written only as an audit log.

    accounts:  restrict to these account emails (None = all)
    campaigns: restrict matching to these campaign IDs (None = all)
    dry_run:   find matches and print what would change; write nothing
    days:      how many days back to look in IMAP SINCE filter (default 30)
    recheck:   also read mails already seen as unmatched/skipped, not only those older than a day
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
              "matched": 0, "bounced": 0, "unmatched": 0, "skipped": 0, "errors": 0,
              "seen_skipped": 0}

    for account_email, acc in to_check.items():
        try:
            counts = _process_account(
                db, account_email, acc,
                campaign_filter=campaign_filter,
                days=days, limit=limit, dry_run=dry_run,
                own_domains=own_domains, recheck=recheck,
            )
            for k in ("replies", "bounces", "dmarc", "matched", "bounced", "unmatched", "skipped", "errors", "seen_skipped"):
                totals[k] += counts.get(k, 0)
        except Exception as ex:
            totals["accounts_failed"] += 1
            print(f"[reply_matcher] ✗  IMAP error for {account_email!r}: {ex}")

    print(
        f"\n[reply_matcher] Done — "
        f"accounts={totals['accounts_checked']} failed={totals['accounts_failed']} "
        f"replies={totals['replies']} bounces={totals['bounces']} dmarc={totals['dmarc']} "
        f"matched={totals['matched']} bounced={totals['bounced']} "
        f"unmatched={totals['unmatched']} skipped={totals['skipped']} errors={totals['errors']} "
        f"already_seen={totals['seen_skipped']}"
    )
    return {**totals, "dry_run": dry_run}
