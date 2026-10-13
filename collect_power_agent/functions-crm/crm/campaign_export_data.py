"""campaign_export_data.py -- Shared data layer for campaign export.

Loads campaign metadata, campaign_leads, and campaign_contacts from Firestore,
joins them into one row per contact, and returns a uniform list of dicts.

Used by both:
  campaign_export_lib.py  -- Google Sheets output
  app/campaign_exporter.py -- local Excel output

Column order is the single source of truth here.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

CAMPAIGNS_COLLECTION  = "campaigns"
CAMPAIGN_LEADS_SUB    = "campaign_leads"
CAMPAIGN_CONTACTS_SUB = "campaign_contacts"

# (field_key, header_label, excel_col_width)
# Both exporters use this list in order.
COLUMNS = [
    ("campaign_id",     "Campaign",       20),
    ("lead_id",         "Lead ID",        26),
    ("company",         "Company",        28),
    ("website",         "Website",        34),
    ("country",         "Country",        12),
    ("location",        "Location",       24),
    ("platform",        "Platform",       18),
    ("page_count",      "Pages",          10),
    ("description",     "Description",    50),
    ("priority",        "Priority",       14),
    ("reseller_score",  "Score",          10),
    ("suggested_angle", "Angle",          50),
    ("categories",      "Categories",     30),
    ("email",           "Email",          34),
    ("name",            "Name",           24),
    ("title",           "Title",          28),
    ("occupation",      "Occupation",     24),
    ("email_type",      "Email Type",     14),
    ("phone",           "Phone",          18),
    ("linkedin",        "LinkedIn",       34),
]

WRAP_COLS = {"description", "suggested_angle"}


def load_campaign_meta(db, campaign_id: str) -> dict:
    snap = db.collection(CAMPAIGNS_COLLECTION).document(campaign_id).get()
    if not snap.exists:
        raise ValueError(f"Campaign not found: {campaign_id!r}")
    return snap.to_dict() or {}


def load_rows(db, campaign_id: str) -> list[dict]:
    """Return one dict per contact, with lead fields merged in."""
    # Load leads index
    leads_col = (
        db.collection(CAMPAIGNS_COLLECTION)
          .document(campaign_id)
          .collection(CAMPAIGN_LEADS_SUB)
    )
    leads: dict[str, dict] = {doc.id: (doc.to_dict() or {}) for doc in leads_col.stream()}

    # Load contacts
    contacts_col = (
        db.collection(CAMPAIGNS_COLLECTION)
          .document(campaign_id)
          .collection(CAMPAIGN_CONTACTS_SUB)
    )
    rows = []
    for doc in contacts_col.stream():
        contact = doc.to_dict() or {}
        lead_id = contact.get("lead_id", "")
        lead    = leads.get(lead_id, {})

        row = {"campaign_id": campaign_id}

        # lead fields
        for key in ("lead_id", "company", "website", "country", "location",
                    "platform", "page_count", "description", "priority",
                    "reseller_score", "suggested_angle", "categories"):
            row[key] = lead.get(key, "")

        # contact fields
        for key in ("email", "name", "title", "occupation", "email_type",
                    "phone", "linkedin"):
            row[key] = contact.get(key, "")

        # normalise list fields to string
        if isinstance(row.get("categories"), list):
            row["categories"] = ", ".join(str(x) for x in row["categories"])

        rows.append(row)

    rows.sort(key=lambda r: (r.get("lead_id", ""), r.get("email", "")))
    return rows


def build_summary_rows(campaign_id: str, campaign: dict, rows: list[dict]) -> list[list]:
    """Return a list-of-lists suitable for both Google Sheets and Excel summary."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lead_ids = {r["lead_id"] for r in rows}
    country_counts  = Counter(r.get("country",  "") for r in rows)
    platform_counts = Counter(r.get("platform", "") for r in rows if r.get("platform"))

    out = [
        ["Campaign",        campaign_id],
        ["Name",            campaign.get("name", "")],
        ["Owner",           campaign.get("owner", "")],
        ["Send account",    campaign.get("outreach_email_account", "")],
        ["Countries",       ", ".join(campaign.get("countries") or [])],
        ["Created",         str(campaign.get("created_at", ""))],
        ["Exported",        now],
        [],
        ["Leads",    len(lead_ids)],
        ["Contacts", len(rows)],
        [],
        ["Country", "Contacts"],
    ]
    for country, n in country_counts.most_common():
        out.append([country or "?", n])
    out.append([])
    out.append(["Platform", "Contacts"])
    for platform, n in platform_counts.most_common():
        out.append([platform, n])
    return out
