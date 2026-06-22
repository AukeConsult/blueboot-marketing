"""campaign_import_lib.py -- Import leads + contacts from an Excel file into a campaign.

Sheet format: the standard export format produced by campaign_export_lib.py /
campaign_exporter.py — one tab named "Leads+Contacts", one row per contact,
lead fields repeated per row.

ID derivation
-------------
lead_id   : taken from the 'Lead ID' column if present; otherwise derived from
            the 'Website' column using lead_id_from_url() (same as site_agent).
contact   : always derived from email via contact_id_from_email() —
            e.g. adrian@blisynlig.no -> adrian_blisynlig_no

Import rules
------------
- Campaign doc created (status=draft) if it does not exist yet.
- Leads written to campaigns/{id}/campaign_leads/{lead_id} with merge=True.
- Contacts written to campaigns/{id}/campaign_contacts/{doc_id} with merge=True.
- Existing contact fields status, mail_sent, followup_* are NEVER overwritten.
- Rows with no email AND no website are skipped.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from io import BytesIO

CAMPAIGNS_COLLECTION  = "campaigns"
CAMPAIGN_LEADS_SUB    = "campaign_leads"
CAMPAIGN_CONTACTS_SUB = "campaign_contacts"

TAB_NAME = "Leads+Contacts"

# Lead columns that come from the sheet
_LEAD_FIELDS = [
    "lead_id", "company", "website", "country", "location",
    "platform", "page_count", "description", "priority",
    "reseller_score", "suggested_angle", "categories",
]

# Contact columns that come from the sheet
_CONTACT_FIELDS = [
    "email", "name", "title", "occupation", "email_type", "phone", "linkedin",
]

# Contact fields that are NEVER overwritten on existing docs
_PROTECTED_CONTACT_FIELDS = {
    "status", "mail_sent", "next_mail_index", "in_reply_to",
    "followup_status", "followup_date", "followup_comment",
    "followup_importance", "followup_owner", "comment_history",
    "sent_at", "message_id", "sender_account", "created_at",
}

BATCH_SIZE = 400


# ---------------------------------------------------------------------------
# ID derivation
# ---------------------------------------------------------------------------

def contact_id_from_email(email: str) -> str:
    """adrian@blisynlig.no -> adrian_blisynlig_no"""
    s = email.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def lead_id_from_website(website: str) -> str:
    """Derive lead_id from a website URL using the canonical project function."""
    try:
        from urllib.parse import urlparse
        host = urlparse(website).hostname or website
        slug = re.sub(r"[.\-]+", "_", host.rstrip(".").lower())
        return re.sub(r"_+", "_", slug).strip("_")
    except Exception:
        slug = re.sub(r"[^a-z0-9]+", "_", website.lower())
        return re.sub(r"_+", "_", slug).strip("_")


# ---------------------------------------------------------------------------
# Sheet parsing
# ---------------------------------------------------------------------------

def parse_sheet(file_bytes: bytes) -> tuple[list[dict], list[str]]:
    """Parse the Leads+Contacts tab from xlsx bytes.

    Returns (rows, warnings) where each row is a plain dict keyed by
    column header (lowercased + spaces→underscores).
    """
    from openpyxl import load_workbook
    wb = load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)

    # Find the tab
    sheet = None
    for name in wb.sheetnames:
        if name.lower().replace(" ", "") == TAB_NAME.lower().replace(" ", ""):
            sheet = wb[name]
            break
    if sheet is None:
        raise ValueError(
            f"No '{TAB_NAME}' tab found. "
            f"Available tabs: {', '.join(wb.sheetnames)}"
        )

    rows_iter = sheet.iter_rows(values_only=True)
    header_row = next(rows_iter, None)
    if not header_row:
        raise ValueError(f"'{TAB_NAME}' tab is empty.")

    # Normalise headers: "Lead ID" -> "lead_id"
    headers = [
        re.sub(r"[^a-z0-9]+", "_", str(h or "").lower()).strip("_")
        for h in header_row
    ]

    rows = []
    warnings = []
    for i, raw in enumerate(rows_iter, start=2):
        row = {headers[j]: (str(v).strip() if v is not None else "")
               for j, v in enumerate(raw) if j < len(headers)}
        email   = row.get("email",   "").strip()
        website = row.get("website", "").strip()
        if not email and not website:
            warnings.append(f"Row {i}: skipped — no email or website")
            continue
        rows.append(row)

    return rows, warnings


# ---------------------------------------------------------------------------
# Core import logic
# ---------------------------------------------------------------------------

def run_campaign_import(
    db,
    campaign_id: str,
    rows: list[dict],
    *,
    dry_run: bool = False,
) -> dict:
    """Import rows into campaign_leads + campaign_contacts.

    Returns a summary dict with counts.
    """
    if not campaign_id:
        raise ValueError("campaign_id is required")
    if not rows:
        return {
            "campaign_id":      campaign_id,
            "leads_new":        0,
            "leads_updated":    0,
            "contacts_new":     0,
            "contacts_updated": 0,
            "skipped":          0,
            "dry_run":          dry_run,
        }

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ── Build lead and contact dicts ─────────────────────────────────────────
    leads_by_id:    dict[str, dict] = {}
    contacts_by_id: dict[str, dict] = {}
    skipped = 0

    for row in rows:
        # Resolve lead_id
        lead_id = row.get("lead_id", "").strip()
        website = row.get("website", "").strip()
        if not lead_id:
            if not website:
                skipped += 1
                continue
            lead_id = lead_id_from_website(website)

        # Resolve contact doc_id
        email = row.get("email", "").strip()
        if not email:
            skipped += 1
            continue
        doc_id = contact_id_from_email(email)

        # Build lead doc (first occurrence wins for shared fields)
        if lead_id not in leads_by_id:
            lead: dict = {"lead_id": lead_id, "campaign_id": campaign_id}
            for f in _LEAD_FIELDS:
                v = row.get(f, "")
                if v:
                    lead[f] = v
            leads_by_id[lead_id] = lead

        # Build contact doc
        contact: dict = {"lead_id": lead_id, "campaign_id": campaign_id}
        for f in _CONTACT_FIELDS:
            v = row.get(f, "")
            if v:
                contact[f] = v
        contacts_by_id[doc_id] = contact

    # ── Check what already exists ────────────────────────────────────────────
    leads_col    = db.collection(CAMPAIGNS_COLLECTION).document(campaign_id).collection(CAMPAIGN_LEADS_SUB)
    contacts_col = db.collection(CAMPAIGNS_COLLECTION).document(campaign_id).collection(CAMPAIGN_CONTACTS_SUB)

    existing_leads    = {doc.id for doc in leads_col.select([]).stream()}
    existing_contacts = {doc.id for doc in contacts_col.select([]).stream()}

    leads_new     = [lid for lid in leads_by_id    if lid not in existing_leads]
    leads_updated = [lid for lid in leads_by_id    if lid in  existing_leads]
    contacts_new     = [cid for cid in contacts_by_id if cid not in existing_contacts]
    contacts_updated = [cid for cid in contacts_by_id if cid in  existing_contacts]

    summary = {
        "campaign_id":      campaign_id,
        "leads_new":        len(leads_new),
        "leads_updated":    len(leads_updated),
        "contacts_new":     len(contacts_new),
        "contacts_updated": len(contacts_updated),
        "skipped":          skipped,
        "dry_run":          dry_run,
    }

    if dry_run:
        return summary

    # ── Create campaign doc if missing ───────────────────────────────────────
    camp_ref = db.collection(CAMPAIGNS_COLLECTION).document(campaign_id)
    if not camp_ref.get().exists:
        camp_ref.set({
            "campaign_id": campaign_id,
            "status":      "draft",
            "created_at":  now,
            "updated_at":  now,
        })
        print(f"[campaign-import] created campaign '{campaign_id}'", flush=True)

    # ── Write leads ──────────────────────────────────────────────────────────
    lead_items = list(leads_by_id.items())
    written_leads = 0
    for i in range(0, len(lead_items), BATCH_SIZE):
        batch = db.batch()
        for lead_id, lead_doc in lead_items[i:i + BATCH_SIZE]:
            lead_doc["synced_at"] = now
            batch.set(leads_col.document(lead_id), lead_doc, merge=True)
        batch.commit()
        written_leads += len(lead_items[i:i + BATCH_SIZE])
        print(f"[campaign-import] leads {written_leads}/{len(lead_items)}", flush=True)

    # ── Write contacts (protected fields never overwritten) ──────────────────
    contact_items = list(contacts_by_id.items())
    written_contacts = 0
    for i in range(0, len(contact_items), BATCH_SIZE):
        batch = db.batch()
        for doc_id, contact_doc in contact_items[i:i + BATCH_SIZE]:
            if doc_id in existing_contacts:
                # Existing contact: remove protected fields so merge=True leaves them intact
                safe = {k: v for k, v in contact_doc.items()
                        if k not in _PROTECTED_CONTACT_FIELDS}
                batch.set(contacts_col.document(doc_id), safe, merge=True)
            else:
                # New contact: set defaults
                contact_doc.setdefault("status",    "pending")
                contact_doc.setdefault("created_at", now)
                batch.set(contacts_col.document(doc_id), contact_doc, merge=True)
        batch.commit()
        written_contacts += len(contact_items[i:i + BATCH_SIZE])
        print(f"[campaign-import] contacts {written_contacts}/{len(contact_items)}", flush=True)

    print(f"[campaign-import] done — {written_leads} leads, {written_contacts} contacts", flush=True)
    return summary
