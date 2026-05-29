"""site_contact_export.py -- Export site_contacts to Excel, enriched with site data.

Reads every document from the site_contacts collectionGroup
(site_leads/{lead_id}/site_contacts/{contact_id}), joins in key fields from
the parent site_leads document, and writes one Excel row per contact.

Each row contains:
  Contact:  name, email, phone, title, occupation, company, linkedin, twitter, facebook
  Site:     domain, website, country, page_count, ai_sector, ai_company_type,
            ai_platform, ai_hosting, ai_summary, ai_confidence, query_category,
            ai_keywords, crawled_at, found_on

This makes it easy to filter by country, sector, platform, or any other field
to find the most relevant contacts for outreach.

Usage:
    python app/site_contact_export.py
    python app/site_contact_export.py --countries NO,SE
    python app/site_contact_export.py --countries NO --sector ecommerce
    python app/site_contact_export.py --countries NO --with-email-only
    python app/site_contact_export.py --output exports/contacts_no.xlsx
    python app/site_contact_export.py --limit 2000
"""
from __future__ import annotations

import argparse
import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path

import _pathsetup  # noqa: F401

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LEADS_COLLECTION    = "site_leads"
CONTACTS_COLLECTION = "site_contacts"

# Columns in output order: (contact_field, header, width)
CONTACT_COLUMNS: list[tuple[str, str, int]] = [
    ("name",             "Name",             28),
    ("email",            "Email",            32),
    ("phone",            "Phone",            18),
    ("title",            "Title (scraped)",  28),
    ("occupation",       "Occupation",       28),
    ("company",          "Company",          28),
    ("linkedin",         "LinkedIn",         40),
    ("twitter",          "Twitter",          30),
    ("facebook",         "Facebook",         30),
]

LEAD_COLUMNS: list[tuple[str, str, int]] = [
    ("domain",           "Domain",           28),
    ("website",          "Website",          34),
    ("country",          "Country",           8),
    ("country_name",     "Country Name",     16),
    ("query_category",   "Category",         16),
    ("page_count",       "Pages",             9),
    ("ai_sector",        "AI Sector",        18),
    ("ai_company_type",  "AI Type",          14),
    ("ai_platform",      "Platform",         16),
    ("ai_hosting",       "Hosting",          16),
    ("ai_confidence",    "AI Conf",           9),
    ("ai_summary",       "AI Summary",       45),
    ("ai_keywords",      "AI Keywords",      45),
    ("crawled_at",       "Crawled At",       20),
    ("found_on",         "Found On",         30),
]

# ---------------------------------------------------------------------------
# Secrets / Firestore
# ---------------------------------------------------------------------------

def _load_secrets():
    secrets_path = Path(__file__).parent.parent / "blueboot_secrets.py"
    if not secrets_path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("blueboot_secrets", secrets_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "fireBaseAdminKey", None)
    except Exception as e:
        print(f"  [contact-export] could not load blueboot_secrets: {e}")
        return None


def _init_firestore(fb_key_dict):
    try:
        import firebase_admin
        from firebase_admin import firestore
        import firebase_admin.credentials as fb_creds
    except ImportError:
        raise RuntimeError("firebase-admin not installed — run: pip install firebase-admin")
    cred = (fb_creds.Certificate(fb_key_dict) if fb_key_dict
            else fb_creds.Certificate(os.getenv("FIREBASE_CREDENTIALS",
                                                "config/serviceAccountKey.json")))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()


# ---------------------------------------------------------------------------
# Firestore fetch
# ---------------------------------------------------------------------------

def _load_leads_index(db, countries: list[str] | None) -> dict[str, dict]:
    """Load all site_leads into a dict keyed by lead_id for fast join."""
    print("  [contact-export] Loading site_leads index…", flush=True)
    col   = db.collection(LEADS_COLLECTION)
    index = {}
    for doc in col.stream():
        data = doc.to_dict() or {}
        if countries:
            c = (data.get("country") or "").upper()
            if c not in countries:
                continue
        index[doc.id] = data
    print(f"  [contact-export] {len(index)} leads loaded into index", flush=True)
    return index


def _stream_contacts(
    db,
    leads_index:     dict[str, dict],
    with_email_only: bool,
    sector:          str | None,
    category:        str | None,
    limit:           int | None,
) -> list[dict]:
    """Stream site_contacts collectionGroup, join with parent lead data."""
    print(f"  [contact-export] Scanning collectionGroup '{CONTACTS_COLLECTION}'…", flush=True)
    col     = db.collection_group(CONTACTS_COLLECTION)
    rows    = []
    scanned = skipped = 0

    for doc in col.stream():
        scanned += 1
        contact = doc.to_dict() or {}

        # Must have a name
        if not (contact.get("name") or "").strip():
            skipped += 1
            continue

        if with_email_only and not (contact.get("email") or "").strip():
            skipped += 1
            continue

        # Get parent lead_id from the doc path: site_leads/{lead_id}/site_contacts/{id}
        lead_id = doc.reference.parent.parent.id
        lead    = leads_index.get(lead_id, {})

        # If countries filter active, skip contacts whose parent lead isn't in index
        if not lead and leads_index:
            skipped += 1
            continue

        # Sector filter
        if sector and (lead.get("ai_sector") or "").lower() != sector.lower():
            skipped += 1
            continue

        # Category filter
        if category and (lead.get("query_category") or "").lower() != category.lower():
            skipped += 1
            continue

        # Merge contact + lead fields into one flat row
        row = dict(contact)
        row["_lead_id"] = lead_id
        # Copy lead fields (prefix collision avoided — lead fields go under their own keys)
        for key in [c[0] for c in LEAD_COLUMNS]:
            if key not in row:  # don't overwrite contact's own field
                row[key] = lead.get(key, "")
        # found_on may be on contact, not lead
        if "found_on" not in contact:
            row["found_on"] = ""

        # Normalise ai_keywords list → comma string
        kw = row.get("ai_keywords")
        if isinstance(kw, list):
            row["ai_keywords"] = ", ".join(kw)

        rows.append(row)

        if len(rows) % 200 == 0:
            print(f"  [contact-export] … {len(rows)} contacts collected (scanned {scanned})", flush=True)

        if limit and len(rows) >= limit:
            break

    print(
        f"  [contact-export] {scanned} scanned → {len(rows)} rows  "
        f"({skipped} skipped by filter)",
        flush=True,
    )
    return rows


# ---------------------------------------------------------------------------
# Excel builder
# ---------------------------------------------------------------------------

def _build_xlsx(rows: list[dict], output_path: str) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise RuntimeError("openpyxl not installed — run: pip install openpyxl")

    wb = Workbook()

    # ── Contacts sheet ──────────────────────────────────────────────────────
    ws = wb.active
    ws.title = "Contacts"

    all_columns = CONTACT_COLUMNS + LEAD_COLUMNS
    headers     = [h for _, h, _ in all_columns]
    widths      = [w for _, _, w in all_columns]
    fields      = [f for f, _, _ in all_columns]

    # Header style
    hdr_font    = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    hdr_fill    = PatternFill("solid", start_color="1F497D")
    hdr_align   = Alignment(horizontal="center", vertical="center", wrap_text=False)
    thin        = Side(style="thin", color="CCCCCC")
    cell_border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # Write headers
    for col_idx, (header, width) in enumerate(zip(headers, widths), start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font      = hdr_font
        cell.fill      = hdr_fill
        cell.alignment = hdr_align
        cell.border    = cell_border
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    ws.row_dimensions[1].height = 20
    ws.freeze_panes = "A2"

    # Alternating row fill
    fill_even = PatternFill("solid", start_color="EEF2F7")
    fill_odd  = PatternFill("solid", start_color="FFFFFF")
    data_font = Font(name="Arial", size=9)
    data_align_wrap = Alignment(vertical="top", wrap_text=True)
    data_align      = Alignment(vertical="top", wrap_text=False)

    # Contact section separator — light blue left border for first lead column
    lead_start_col = len(CONTACT_COLUMNS) + 1
    sep_border = Border(left=Side(style="medium", color="1F497D"),
                        right=thin, top=thin, bottom=thin)

    for row_idx, row in enumerate(rows, start=2):
        fill = fill_even if row_idx % 2 == 0 else fill_odd
        for col_idx, field in enumerate(fields, start=1):
            val = row.get(field, "") or ""
            if isinstance(val, list):
                val = ", ".join(str(v) for v in val)
            if isinstance(val, float):
                val = round(val, 3)
            cell           = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.font      = data_font
            cell.fill      = fill
            cell.alignment = data_align_wrap if col_idx in (2, 16) else data_align
            cell.border    = sep_border if col_idx == lead_start_col else cell_border

    # Auto-filter
    ws.auto_filter.ref = f"A1:{get_column_letter(len(all_columns))}1"

    # ── Summary sheet ───────────────────────────────────────────────────────
    ws2          = wb.create_sheet("Summary")
    ws2["A1"]    = "Site Contact Export"
    ws2["A1"].font = Font(name="Arial", bold=True, size=14)
    summary_rows = [
        ("Generated at",  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        ("Total contacts", len(rows)),
        ("With email",    sum(1 for r in rows if r.get("email"))),
        ("With LinkedIn", sum(1 for r in rows if r.get("linkedin"))),
        ("With phone",    sum(1 for r in rows if r.get("phone"))),
        ("Enriched (Brave)", sum(1 for r in rows if r.get("brave_enriched_at"))),
    ]
    for i, (label, value) in enumerate(summary_rows, start=3):
        ws2.cell(row=i, column=1, value=label).font = Font(name="Arial", bold=True, size=10)
        ws2.cell(row=i, column=2, value=value).font  = Font(name="Arial", size=10)
    ws2.column_dimensions["A"].width = 22
    ws2.column_dimensions["B"].width = 30

    # Country breakdown
    ws2.cell(row=10, column=1, value="By Country").font = Font(name="Arial", bold=True, size=10)
    country_counts: dict[str, int] = {}
    for r in rows:
        c = (r.get("country") or "?").upper()
        country_counts[c] = country_counts.get(c, 0) + 1
    for i, (c, cnt) in enumerate(sorted(country_counts.items()), start=11):
        ws2.cell(row=i, column=1, value=c).font   = Font(name="Arial", size=9)
        ws2.cell(row=i, column=2, value=cnt).font = Font(name="Arial", size=9)

    # Sector breakdown
    sector_start = 11 + len(country_counts) + 2
    ws2.cell(row=sector_start, column=1, value="By AI Sector").font = Font(name="Arial", bold=True, size=10)
    sector_counts: dict[str, int] = {}
    for r in rows:
        s = r.get("ai_sector") or "unknown"
        sector_counts[s] = sector_counts.get(s, 0) + 1
    for i, (s, cnt) in enumerate(sorted(sector_counts.items(), key=lambda x: -x[1]), start=sector_start + 1):
        ws2.cell(row=i, column=1, value=s).font   = Font(name="Arial", size=9)
        ws2.cell(row=i, column=2, value=cnt).font = Font(name="Arial", size=9)

    wb.save(output_path)
    print(f"  [contact-export] Saved → {output_path}", flush=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def export_contacts(
    countries:       list[str] | None = None,
    sector:          str | None       = None,
    category:        str | None       = None,
    with_email_only: bool             = False,
    limit:           int | None       = None,
    output_path:     str | None       = None,
) -> str:
    fb_key = _load_secrets()
    db     = _init_firestore(fb_key)

    # Load leads first (for the join) — filtered by country
    leads_index = _load_leads_index(db, countries)

    rows = _stream_contacts(db, leads_index, with_email_only, sector, category, limit)
    if not rows:
        print("  [contact-export] No contacts found matching filters.")
        return ""

    if not output_path:
        ts          = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        suffix      = ("_" + "_".join(countries)) if countries else ""
        output_path = str(
            Path(__file__).parent.parent / "exports" / f"contacts{suffix}_{ts}.xlsx"
        )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    print(f"\n  [contact-export] Building Excel — {len(rows)} contacts…", flush=True)
    _build_xlsx(rows, output_path)

    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    p = argparse.ArgumentParser(
        description="Export site_contacts to Excel, enriched with site_leads data"
    )
    p.add_argument("--countries",       default=None, metavar="CODES",
                   help="Comma-separated country codes  e.g. NO,SE")
    p.add_argument("--sector",          default=None, metavar="SECTOR",
                   help="Filter by ai_sector  e.g. ecommerce, technology")
    p.add_argument("--category",        default=None, metavar="CATEGORY",
                   help="Filter by query_category  e.g. real_estate, healthcare")
    p.add_argument("--with-email-only", action="store_true",
                   help="Only include contacts that have an email address")
    p.add_argument("--limit",           type=int, default=None, metavar="N",
                   help="Max contacts to export")
    p.add_argument("--output",          default=None, metavar="FILE",
                   help="Output .xlsx path  (default: exports/contacts_<country>_<ts>.xlsx)")

    args = p.parse_args(argv)

    countries = None
    if args.countries:
        countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]

    path = export_contacts(
        countries       = countries,
        sector          = args.sector,
        category        = args.category,
        with_email_only = args.with_email_only,
        limit           = args.limit,
        output_path     = args.output,
    )
    if path:
        print(f"\n  Done → {path}")


if __name__ == "__main__":
    main()
