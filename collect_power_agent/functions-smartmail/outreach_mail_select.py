"""functions-smartmail/outreach_mail_select.py — Outreach mail select library.

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
from datetime import datetime, timezone


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
    from_name:    str
    imap_host:    str
    imap_port:    int
    use_ssl:      bool
    extra:        dict = field(default_factory=dict)


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
    extra:          dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Level 2 wrapper - campaign paired with its contacts
# ---------------------------------------------------------------------------

@dataclass
class CampaignWithContacts:
    """One campaign and all its pending contacts, ready to send."""
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
    "status", "created_at", "sent_at", "message_id", "sender_account",
}

_CAMPAIGN_KNOWN = {
    "name", "status", "mail", "mail_sequence",
    "outreach_email_account", "sender_account",
    "created_at", "updated_at", "started_at", "_type",
}

_ACCOUNT_KNOWN = {
    "email", "account_type", "host", "port", "username",
    "from_name", "imap_host", "imap_port", "ssl",
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
    return MailAccountSettings(
        email        = d.get("email", key),
        account_type = d.get("account_type", "imap"),
        host         = d.get("host", ""),
        port         = int(d.get("port") or 587),
        username     = d.get("username", ""),
        from_name    = d.get("from_name", ""),
        imap_host    = d.get("imap_host", ""),
        imap_port    = int(d.get("imap_port") or 993),
        use_ssl      = bool(d.get("ssl", False)),
        extra        = {k: v for k, v in d.items() if k not in _ACCOUNT_KNOWN},
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


# ---------------------------------------------------------------------------
# read_outreach
# ---------------------------------------------------------------------------

def read_outreach(
    mode:  str = "intro",
    limit: int = 500,
) -> list[AccountBatch]:
    """Read outreach candidates grouped by sending account.

    Two modes:

      "intro"    — contacts where status == "pending"
                   (first-ever mail, always mail_sequence[0])
      "followup" — contacts where followup_status == "Send mail"
                   (next step = mail_sequence[len(contact.mail_sent)])

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

    # Step 1: collectionGroup query — filter depends on mode
    from google.cloud.firestore_v1.base_query import FieldFilter
    col_group = db.collection_group("campaign_contacts")
    if mode == "followup":
        query = col_group.where(filter=FieldFilter("followup_status", "==", "Send mail"))
    else:
        query = col_group.where(filter=FieldFilter("status", "==", "pending"))

    # Group contacts by campaign_id (extracted from the Firestore path:
    # campaigns/{campaign_id}/campaign_contacts/{doc_id})
    by_campaign: dict[str, list[ContactRow]] = defaultdict(list)
    total = 0
    for doc in query.stream():
        if total >= limit:
            break
        d           = doc.to_dict() or {}
        campaign_id = doc.reference.parent.parent.id
        by_campaign[campaign_id].append(ContactRow(
            contact_doc_id = doc.id,
            campaign_id    = campaign_id,
            email          = d.get("email", ""),
            contact_name   = d.get("contact_name", ""),
            company        = d.get("company", ""),
            domain         = d.get("domain", ""),
            country        = d.get("country", ""),
            status         = d.get("status", ""),
            extra          = {k: v for k, v in d.items() if k not in _CONTACT_KNOWN},
        ))
        total += 1

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

        if not campaign.mail_sequence:
            print(
                f"[outreach_mail_select] campaign '{campaign_id}' has no mail_sequence defined"
                f" -- skipping (add mail_sequence array to the campaign doc)"
            )
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
      3. status stamp     → mode "intro"    sets status="contacted"
                            mode "followup" sets followup_status="contacted"

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
    if mode == "followup":
        update["followup_status"] = "contacted"
    else:
        update["status"] = "contacted"

    # 1 - write contact row
    (
        db.collection("campaigns")
        .document(campaign_id)
        .collection("campaign_contacts")
        .document(contact_doc_id)
        .update(update)
    )

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
]
