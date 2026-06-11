"""functions-smartmail/smart_mail/outreach_mail_select.py — Outreach mail select library.

Two public functions:

    read_outreach(status, limit)
        Query the campaign_contacts collectionGroup for all pending contacts
        and return an account-first three-level structure:

            list[AccountBatch]
              .account   : MailAccountSettings        (level 1 - who sends)
              .campaigns : list[CampaignWithContacts] (level 2 - what to send)
                .campaign  : CampaignMail
                .contacts  : list[ContactRow]         (level 3 - who to send to)

        The sender can open one SMTP connection per AccountBatch and work
        through all its campaigns and contacts before closing.

    confirm_sent(campaign_id, contact_doc_id, message_id, sender_account, sent_at)
        After an external sender has dispatched an email, stamp the contact
        "contacted" and append a deduplicated log entry to outreach_sent.

No SMTP / sending code lives here.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import re


# ---------------------------------------------------------------------------
# Lazy Firestore accessor
# ---------------------------------------------------------------------------

def _get_db():
    try:
        from smart_mail.firestore_client import get_firestore
    except ImportError:
        try:
            from app.firestore_client import get_firestore
        except ImportError:
            from firestore_client import get_firestore  # type: ignore[no-redef]
    return get_firestore()


# ---------------------------------------------------------------------------
# Level 1 - Mail account settings
# ---------------------------------------------------------------------------

@dataclass
class MailAccountSettings:
    """Sending account resolved from settings/mail_accounts/accounts/{email}."""
    email:        str
    account_type: str       # "imap" | "gmail"
    host:         str       # SMTP host
    port:         int       # SMTP port
    username:     str
    password:     str
    from_name:    str
    imap_host:    str
    imap_port:    int
    use_ssl:      bool
    client_id:    str = ""
    client_secret: str = ""
    refresh_token: str = ""
    access_token: str = ""
    extra:        dict = field(default_factory=dict)
    raw:          dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Level 2 - Campaign + mail template
# ---------------------------------------------------------------------------

@dataclass
class CampaignMail:
    """Campaign metadata and the mail template to render per contact."""
    campaign_id:      str
    campaign_name:    str
    status:           str
    subject_template: str   # may contain {{contact_name}} etc.
    body_html:        str   # Quill HTML, may contain template vars
    sender_email:     str   # account email stored on the campaign doc
    mail_sequence:    list  = field(default_factory=list)  # ordered MailStep dicts from campaign doc
    extra:            dict  = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Level 3 - Individual contact
# ---------------------------------------------------------------------------

@dataclass
class ContactRow:
    """One recipient from campaign_contacts."""
    contact_doc_id: str
    campaign_id:    str
    email:          str
    contact_name:   str
    company:        str
    domain:         str
    country:        str
    status:         str
    mail_sent:      list  = field(default_factory=list)
    next_mail_index: int  = 0
    in_reply_to:    str | None = None
    selected_step:  dict | None = None
    extra:          dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Level 2 wrapper - campaign paired with its contacts
# ---------------------------------------------------------------------------

@dataclass
class CampaignWithContacts:
    """One campaign and all contacts selected for the next send."""
    campaign: CampaignMail
    contacts: list[ContactRow]


# ---------------------------------------------------------------------------
# Top-level batch (one per sending account)
# ---------------------------------------------------------------------------

@dataclass
class AccountBatch:
    """All outreach work for one sending account.

    Contains every campaign that uses this account, each with its full
    contact list. The sender opens one SMTP connection for the whole batch.
    """
    account:   MailAccountSettings
    campaigns: list[CampaignWithContacts]


# ---------------------------------------------------------------------------
# Return value of confirm_sent
# ---------------------------------------------------------------------------

@dataclass
class SentConfirmation:
    campaign_id:    str
    contact_doc_id: str
    message_id:     str
    confirmed_at:   str


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

_CONTACT_KNOWN = {
    "email", "contact_name", "company", "domain", "country",
    "status", "followup_status", "mail_sent", "created_at", "sent_at", "message_id", "sender_account",
}

_CAMPAIGN_KNOWN = {
    "name", "status", "mail", "mail_sequence",
    "outreach_email_account", "sender_account",
    "created_at", "updated_at", "started_at", "_type",
}

_ACCOUNT_KNOWN = {
    "email", "account_type", "host", "port", "username",
    "password", "display_name", "from_name", "imap_host", "imap_port", "ssl",
    "smtp_host", "smtp_port", "smtp_ssl", "client_id", "client_secret",
    "refresh_token", "access_token",
    "updated_at", "_type",
}


def _load_account(db, sender_email: str) -> MailAccountSettings | None:
    """Fetch one mail account from settings/mail_accounts/accounts/{email}."""
    key = (sender_email or "").strip().lower()
    if not key:
        return None
    doc = (
        db.collection("settings")
        .document("mail_accounts")
        .collection("accounts")
        .document(key)
        .get()
    )
    if not doc.exists:
        return None
    d = doc.to_dict() or {}
    account_type = str(d.get("account_type") or "imap").strip().lower()
    imap_host = str(d.get("host") or "").strip()
    smtp_host = str(d.get("smtp_host") or "").strip()
    if not smtp_host and imap_host.startswith("imap."):
        smtp_host = imap_host.replace("imap.", "smtp.", 1)
    smtp_host = smtp_host or imap_host
    smtp_port = int(d.get("smtp_port") or 587)
    return MailAccountSettings(
        email        = str(d.get("email") or key).strip().lower(),
        account_type = account_type,
        host         = smtp_host,
        port         = smtp_port,
        username     = str(d.get("username") or d.get("email") or key).strip(),
        password     = d.get("password", ""),
        from_name    = str(d.get("display_name") or d.get("from_name") or "").strip(),
        imap_host    = imap_host,
        imap_port    = int(d.get("port") or d.get("imap_port") or 993),
        use_ssl      = _as_bool(d.get("smtp_ssl", False)) or smtp_port == 465,
        client_id    = str(d.get("client_id") or "").strip(),
        client_secret = str(d.get("client_secret") or "").strip(),
        refresh_token = str(d.get("refresh_token") or "").strip(),
        access_token = str(d.get("access_token") or "").strip(),
        extra        = {k: v for k, v in d.items() if k not in _ACCOUNT_KNOWN},
        raw          = dict(d),
    )


def _load_campaign(db, campaign_id: str) -> CampaignMail | None:
    """Fetch campaign doc and extract mail template fields."""
    doc = db.collection("campaigns").document(campaign_id).get()
    if not doc.exists:
        return None
    d        = doc.to_dict() or {}
    mail_cfg = d.get("mail") or {}
    sender   = (
        d.get("outreach_email_account") or
        d.get("sender_account") or ""
    ).strip()
    mail_sequence = d.get("mail_sequence") or []
    if not isinstance(mail_sequence, list):
        mail_sequence = []

    return CampaignMail(
        campaign_id      = campaign_id,
        campaign_name    = d.get("name", ""),
        status           = d.get("status", ""),
        subject_template = mail_cfg.get("subject", ""),
        body_html        = mail_cfg.get("body", ""),
        sender_email     = sender,
        mail_sequence    = mail_sequence,
        extra            = {k: v for k, v in d.items() if k not in _CAMPAIGN_KNOWN},
    )


def _campaign_status(value) -> str:
    status = str(value or "draft").strip().lower()
    return status


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


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


def _next_step_due(contact: ContactRow, campaign: CampaignMail, now: datetime) -> bool:
    mail_sent = contact.mail_sent or []
    next_idx = len(mail_sent)
    if next_idx >= len(campaign.mail_sequence):
        return False

    step = campaign.mail_sequence[next_idx] or {}
    if "delay_days" not in step:
        return False
    delay_days = int(step.get("delay_days") or 0)
    first_sent_at = _parse_dt((mail_sent[0] or {}).get("sent_at"))
    if not first_sent_at:
        return True
    if first_sent_at.tzinfo is None:
        first_sent_at = first_sent_at.replace(tzinfo=timezone.utc)
    return now >= first_sent_at + timedelta(days=delay_days)


def _attach_selected_step(contact: ContactRow, campaign: CampaignMail) -> bool:
    mail_sent = contact.mail_sent or []
    next_idx = len(mail_sent)
    if next_idx >= len(campaign.mail_sequence):
        return False
    contact.next_mail_index = next_idx
    contact.in_reply_to = mail_sent[-1].get("message_id") if mail_sent else None
    contact.selected_step = campaign.mail_sequence[next_idx] or {}
    return True


def _is_intro_step(step: dict) -> bool:
    marker = " ".join(
        str(step.get(k) or "")
        for k in ("mail_type", "name", "step_name", "step_id")
    ).strip().lower()
    return "intro" in marker


def _prepare_mail_sequence(seq: list) -> list:
    if not isinstance(seq, list):
        return []
    intro_idx = next((i for i, step in enumerate(seq) if _is_intro_step(step or {})), None)
    if intro_idx is None:
        return []
    intro = seq[intro_idx]
    rest = [step for i, step in enumerate(seq) if i != intro_idx]
    return [intro] + rest


def _log_campaign_skip(db, campaign_id: str, text: str) -> None:
    try:
        from google.cloud.firestore_v1 import ArrayUnion
        now = datetime.now(timezone.utc).isoformat()
        db.collection("campaigns").document(campaign_id).update({
            "outreach_log": ArrayUnion([{
                "date": now,
                "type": "OUTREACH_SKIP",
                "text": text,
            }]),
            "updated_at": now,
        })
    except Exception as exc:
        print(f"[outreach_mail_select] skip-log failed for {campaign_id}: {exc}")


def prepare_mail_sequences(db=None) -> int:
    """Build mail_sequence from the current mail_schedule format when needed."""
    db = db or _get_db()
    updated = 0
    for doc in db.collection("campaigns").stream():
        d = doc.to_dict() or {}
        if d.get("mail_sequence"):
            continue

        mail_schedule = d.get("mail_schedule") or []
        if mail_schedule and isinstance(mail_schedule, list):
            mail_sequence = []
            for i, step in enumerate(mail_schedule):
                step_mail = step.get("mail") or {}
                subject   = step_mail.get("subject", "").strip()
                body      = step_mail.get("body",    "").strip()
                step_name = (step.get("name") or "").strip().lower()
                mail_type = "intro" if i == 0 or "intro" in step_name else f"followup_{i}"
                is_plain = step_mail.get("type", "html") == "plain"
                mail_sequence.append({
                    "index":      i,
                    "mail_type":  mail_type,
                    "delay_days": int(step.get("delay_days") or 0),
                    "subject":    subject,
                    "body_html":  "" if is_plain else body,
                    "body_text":  body if is_plain else "",
                })
            if mail_sequence:
                doc.reference.update({"mail_sequence": mail_sequence})
                print(f"[prepare-mail-sequence] {doc.id!r}: built {len(mail_sequence)} step(s) from mail_schedule")
                updated += 1
    return updated


# ---------------------------------------------------------------------------
# read_outreach
# ---------------------------------------------------------------------------

def read_outreach(
    mode:        str = "intro",
    limit:       int = 500,
    campaign_ids: list[str] | None = None,
) -> list[AccountBatch]:
    """Read outreach candidates grouped by sending account.

    Two modes:

      "intro"    — contacts where status == "pending"
                   (first-ever mail, always mail_sequence[0])
      "followup" — pending contacts that already received at least one mail
                   (next due step = mail_sequence[len(contact.mail_sent)])

    Queries the campaign_contacts collectionGroup in one round-trip, groups
    contacts by campaign, resolves each campaign's mail template and sending
    account, then re-groups the result by account so the caller gets one
    AccountBatch per distinct sender.

    Campaigns or accounts that cannot be resolved are skipped with a warning
    and never abort the whole result.

    Parameters
    ----------
    mode : "intro" | "followup"
        Selects the Firestore filter. Default "intro".
    limit : int
        Maximum total contacts to read across all campaigns.
    campaign_ids : list[str] | None
        Optional campaign guard. When set, only contacts whose parent campaign
        document id is in this list are selected.

    Returns
    -------
    list[AccountBatch]
        One AccountBatch per sending account, each containing:
          .account              MailAccountSettings    (level 1)
          .campaigns            list[CampaignWithContacts]
            .campaign           CampaignMail           (level 2)
            .contacts           list[ContactRow]       (level 3)

    Usage
    -----
    for batch in read_outreach(mode="intro"):
        # open one SMTP connection for batch.account
        for cwc in batch.campaigns:
            for contact in cwc.contacts:
                # render and send
    """
    db = _get_db()

    # Step 1: query contacts. If campaign_ids is set, read those campaigns'
    # subcollections directly so non-matching campaigns can never leak in.
    from google.cloud.firestore_v1.base_query import FieldFilter
    campaign_filter = _coerce_id_set(campaign_ids)
    if campaign_filter:
        queries = []
        for cid in sorted(campaign_filter):
            camp_doc = db.collection("campaigns").document(cid).get()
            if not camp_doc.exists:
                print(f"[outreach_mail_select] campaign '{cid}' not found -- skipping")
                continue
            queries.append(
                db.collection("campaigns")
                .document(cid)
                .collection("campaign_contacts")
                .where(filter=FieldFilter("status", "==", "pending"))
            )
        if not queries:
            print("[outreach_mail_select] no requested campaigns found -- no candidates")
            return []
    else:
        queries = [(
            db.collection_group("campaign_contacts")
            .where(filter=FieldFilter("status", "==", "pending"))
        )]

    # Group contacts by campaign_id (extracted from the Firestore path:
    # campaigns/{campaign_id}/campaign_contacts/{doc_id})
    by_campaign: dict[str, list[ContactRow]] = defaultdict(list)
    total = 0
    for query in queries:
        for doc in query.stream():
            if total >= limit:
                break
            d           = doc.to_dict() or {}
            contact_status = str(d.get("status", "")).strip().lower()
            mail_sent = d.get("mail_sent") or []
            if not isinstance(mail_sent, list):
                mail_sent = []
            if contact_status != "pending":
                continue
            if mode == "followup" and not mail_sent:
                continue
            if mode != "followup" and mail_sent:
                continue
            doc_campaign_id = doc.reference.parent.parent.id
            if campaign_filter and doc_campaign_id not in campaign_filter:
                continue
            by_campaign[doc_campaign_id].append(ContactRow(
                contact_doc_id = doc.id,
                campaign_id    = doc_campaign_id,
                email          = d.get("email", ""),
                contact_name   = d.get("contact_name", ""),
                company        = d.get("company", ""),
                domain         = d.get("domain", ""),
                country        = d.get("country", ""),
                status         = contact_status,
                mail_sent      = mail_sent,
                extra          = {k: v for k, v in d.items() if k not in _CONTACT_KNOWN},
            ))
            total += 1
        if total >= limit:
            break

    if not by_campaign:
        return []

    # Step 2: resolve campaigns; cache accounts (one Firestore read per account)
    account_cache:   dict[str, MailAccountSettings | None] = {}
    # Group CampaignWithContacts by sender_email for the final assembly
    by_account: dict[str, list[CampaignWithContacts]] = defaultdict(list)

    for campaign_id, contacts in by_campaign.items():
        campaign = _load_campaign(db, campaign_id)
        if campaign is None:
            print(f"[outreach_mail_select] campaign '{campaign_id}' not found -- skipping")
            continue

        campaign.status = _campaign_status(campaign.status)
        required_status = "active" if mode == "followup" else "ready"
        if campaign.status != required_status:
            print(
                f"[outreach_mail_select] {mode} send requires campaign status "
                f"{required_status!r}; campaign '{campaign_id}' is "
                f"{campaign.status!r} -- skipping"
            )
            continue

        campaign.mail_sequence = _prepare_mail_sequence(campaign.mail_sequence)
        if not campaign.mail_sequence:
            _log_campaign_skip(db, campaign_id, "Automatic outreach skipped: campaign has no Intro mail step.")
            print(
                f"[outreach_mail_select] campaign '{campaign_id}' has no Intro "
                f"mail step -- skipping"
            )
            continue

        if mode == "followup":
            now = datetime.now(timezone.utc)
            contacts = [c for c in contacts if _next_step_due(c, campaign, now)]
            if not contacts:
                continue

        contacts = [c for c in contacts if _attach_selected_step(c, campaign)]
        if not contacts:
            continue

        sender_email = campaign.sender_email
        if sender_email not in account_cache:
            account_cache[sender_email] = _load_account(db, sender_email)

        if account_cache[sender_email] is None:
            print(
                f"[outreach_mail_select] mail account '{sender_email}' "
                f"not found for campaign '{campaign_id}' -- skipping"
            )
            continue

        by_account[sender_email].append(
            CampaignWithContacts(campaign=campaign, contacts=contacts)
        )

    # Step 3: assemble one AccountBatch per sending account
    batches: list[AccountBatch] = []
    for sender_email, campaign_list in by_account.items():
        account = account_cache[sender_email]
        if account is None:
            continue
        batches.append(AccountBatch(account=account, campaigns=campaign_list))

    return batches


# ---------------------------------------------------------------------------
# confirm_sent
# ---------------------------------------------------------------------------

def confirm_sent(
    campaign_id:    str,
    contact_doc_id: str,
    message_id:     str        = "",
    mail_type:      str        = "intro",
    mode:           str        = "intro",
    sender_account: str        = "",
    sent_at:        str | None = None,
) -> SentConfirmation:
    """Record that an outreach email was successfully sent.

    Writes atomically to campaign_contacts in one .update() call:
      1. mail_sent        → ArrayUnion({mail_type, sent_at, message_id})
      2. comment_history  → ArrayUnion({date, user, text, type="MAIL_SENT"})
      3. status stamp     → keeps status="pending" and sets
                            followup_status="contacted"

    Also appends a log doc to outreach_sent (deduplicated by message_id).

    Returns a SentConfirmation echo of the written values.
    """
    from google.cloud.firestore_v1 import ArrayUnion
    from email.utils import make_msgid

    confirmed_at = sent_at or datetime.now(timezone.utc).isoformat()
    # Always guarantee a unique message_id — generate one if caller did not provide it
    if not message_id:
        message_id = make_msgid()
    db = _get_db()
    contact_ref = (
        db.collection("campaigns")
        .document(campaign_id)
        .collection("campaign_contacts")
        .document(contact_doc_id)
    )
    contact_doc = contact_ref.get()
    contact_data = contact_doc.to_dict() if contact_doc.exists else {}

    # --- build the single update dict ---
    update: dict = {
        # append-only sent history
        "mail_sent": ArrayUnion([{
            "mail_type":  mail_type,
            "sent_at":    confirmed_at,
            "message_id": message_id,
        }]),
        # CRM-visible history line
        "comment_history": ArrayUnion([{
            "date": confirmed_at,
            "user": sender_account or "outreach",
            "text": "Mail sent: %s  %s" % (mail_type, message_id),
            "type": "MAIL_SENT",
        }]),
    }

    # status stamp — which field depends on mode
    update["status"] = "pending"
    update["followup_status"] = "contacted"
    update["new_mail"] = False

    # 1 - write contact row
    contact_ref.update(update)

    camp_ref = db.collection("campaigns").document(campaign_id)
    camp_doc = camp_ref.get()
    if camp_doc.exists:
        camp = camp_doc.to_dict() or {}
        if _campaign_status(camp.get("status")) == "ready":
            camp_ref.update({
                "status": "active",
                "sent_at": camp.get("sent_at") or confirmed_at,
                "updated_at": confirmed_at,
            })

    # 2 - append to outreach_sent (skip if duplicate message_id)
    existing = (
        db.collection("outreach_sent")
        .where("message_id", "==", message_id)
        .limit(1)
        .stream()
    )
    if not any(existing):
        db.collection("outreach_sent").add({
            "campaign_id":    campaign_id,
            "contact_doc_id": contact_doc_id,
            "to_email":       (contact_data or {}).get("email", ""),
            "sender_account": sender_account,
            "message_id":     message_id,
            "mail_type":      mail_type,
            "sent_at":        confirmed_at,
            "status":         "sent",
        })

    return SentConfirmation(
        campaign_id    = campaign_id,
        contact_doc_id = contact_doc_id,
        message_id     = message_id,
        confirmed_at   = confirmed_at,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "MailAccountSettings",
    "CampaignMail",
    "ContactRow",
    "CampaignWithContacts",
    "AccountBatch",
    "SentConfirmation",
    "read_outreach",
    "confirm_sent",
    "prepare_mail_sequences",
]
