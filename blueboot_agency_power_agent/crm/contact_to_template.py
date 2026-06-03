"""
contact_to_template.py -- Push selected contacts from the contact sheet
into the CRM template Google Sheet.

Procedure:
  1. Read contact sheet (contact_sync TAB) -- filter rows where Select != blank
  2. Group contacts by site (lead_id_site)
  3. For each site group, look up site_leads in Firestore for enrichment
  4. Read crm_template sheet -- collect existing site_lead_ids
  5. Skip sites already present in crm_template
  6. Append new rows to crm_template (one row per site)

Contacts column: all selected contacts for a site as a JSON string:
  [{"name":"...","role":"...","email":"...","phone":"..."}]

Usage:
    python crm/contact_to_template.py
    python crm/contact_to_template.py --dry-run
    python crm/contact_to_template.py --contact-tab "contacts" --template-tab "Outreach"
"""
from __future__ import annotations

import sys
import json
import threading
import argparse
from datetime import date
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import _pathsetup  # noqa: F401,F811

from dotenv import load_dotenv
load_dotenv()

from functions.models import lead_id_from_url
from functions.utils import normalize_url
from functions.firebase_cred import get_firebase_cred
import firebase_admin
from firebase_admin import firestore
import firebase_admin.credentials as fb_creds

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from crm.config import CRM_TEMPLATE_ID

# -- Config -------------------------------------------------------------------
CONTACT_SHEET_ID   = "1aMglV53NiMEArjld37HN5cxliyNRGzIP2mrM4kwlupA"
CONTACT_TAB        = "contacts"
TEMPLATE_SHEET_ID  = CRM_TEMPLATE_ID
TEMPLATE_TAB       = "Outreach"

SCOPES        = ["https://www.googleapis.com/auth/spreadsheets"]
TOKEN_PATH    = str(Path(__file__).resolve().parent.parent / "config" / "google_token.json")
CLIENT_SECRET = str(Path(__file__).resolve().parent.parent / "config" / "google_oauth_client.json")

# CRM template column order
TEMPLATE_HEADERS = [
    "Dato lagt i",
    "Bedrift",
    "Nettside",
    "Bransje",
    "Størrelse",
    "Oppsummert",
    "Land",
    "Site-sider",
    "Beslutningstaker",
    "Rolle",
    "E-post",
    "Telefon",
    "Contacts",
    "Score",
    "Status",
    "Selger",
    "Kommentar",
    "Tilbud",
    "site_lead_id",
    "ai_sector",
    "ai_company_type",
    "ai_platform",
]

# Størrelse mapping from page_count
def _map_storrelse(tier_label: str, ai_company_type: str, page_count) -> str:
    try:
        pages = int(page_count or 0)
    except (ValueError, TypeError):
        pages = 0

    if pages >= 25000:
        return "Ultra Enterprise"
    if pages >= 5000:
        return "Enterprise"
    if pages >= 2000:
        return "Stor"
    if pages >= 500:
        return "Mellomstor"
    return "Liten"

# -- Auth ---------------------------------------------------------------------
_fb_lock = threading.Lock()


def _init_firestore():
    cred_obj = get_firebase_cred()
    cred = cred_obj if isinstance(cred_obj, fb_creds.Base) else fb_creds.Certificate(cred_obj)
    with _fb_lock:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
    return firestore.client()


def _sheets_service():
    creds = None
    if Path(TOKEN_PATH).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow  = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(TOKEN_PATH).write_text(creds.to_json())
    return build("sheets", "v4", credentials=creds)


# -- Step 1: read contact sheet -----------------------------------------------

def _read_selected_contacts(svc, tab: str) -> list[dict]:
    """Read contact sheet, return rows where Select is non-blank."""
    print(f"[c2t] Reading contact sheet tab '{tab}'...", flush=True)
    result = svc.spreadsheets().values().get(
        spreadsheetId=CONTACT_SHEET_ID, range=f"{tab}!A:ZZ"
    ).execute()
    rows = result.get("values", [])
    if not rows:
        return []

    headers = [h.lower().replace(" ", "_") for h in rows[0]]
    col = {h: i for i, h in enumerate(headers)}

    select_idx = col.get("select", -1)
    if select_idx < 0:
        print("[c2t] No 'Select' column found in contact sheet")
        return []

    records = []
    for row in rows[1:]:
        select_val = row[select_idx].strip() if select_idx < len(row) else ""
        if not select_val:
            continue
        rec = {h: (row[i].strip() if i < len(row) else "") for h, i in col.items()}
        records.append(rec)

    print(f"[c2t] {len(records)} selected contacts", flush=True)
    return records


# -- Step 2: group by site ----------------------------------------------------

def _group_by_site(contacts: list[dict]) -> dict[str, list[dict]]:
    """Group contacts by lead_id_site. Falls back to normalizing website."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in contacts:
        site_id = (c.get("lead_id_site") or "").strip()
        if not site_id:
            website = (c.get("website") or "").strip()
            if website:
                ws = website if website.startswith("http") else "https://" + website
                site_id = lead_id_from_url(normalize_url(ws))
        if not site_id:
            print(f"  [skip] no site_id for contact: {c.get('email','?')}")
            continue
        groups[site_id].append(c)
    print(f"[c2t] {len(groups)} unique sites", flush=True)
    return dict(groups)


# -- Step 3: fetch site_leads enrichment --------------------------------------

def _fetch_site_leads(db, site_ids: list[str]) -> dict[str, dict]:
    """Fetch site_leads docs for given IDs. Returns {site_id: data}."""
    col = db.collection("site_leads")
    result = {}
    BATCH = 30
    for i in range(0, len(site_ids), BATCH):
        chunk = site_ids[i:i + BATCH]
        for sid in chunk:
            doc = col.document(sid).get()
            if doc.exists:
                result[sid] = doc.to_dict() or {}
    print(f"[c2t] site_leads found: {len(result)}/{len(site_ids)}", flush=True)
    return result


# -- Step 4: read existing site_lead_ids from crm_template --------------------

def _read_existing_site_ids(svc, tab: str) -> set[str]:
    """Return set of site_lead_ids already in the crm_template sheet."""
    result = svc.spreadsheets().values().get(
        spreadsheetId=TEMPLATE_SHEET_ID, range=f"{tab}!1:1"
    ).execute()
    header_row = result.get("values", [[]])[0]
    col_map = {h.lower().strip(): i for i, h in enumerate(header_row)}
    sid_idx = col_map.get("site_lead_id", -1)

    if sid_idx < 0:
        return set()

    result2 = svc.spreadsheets().values().get(
        spreadsheetId=TEMPLATE_SHEET_ID, range=f"{tab}!A:ZZ"
    ).execute()
    rows = result2.get("values", [])
    existing = set()
    for row in rows[1:]:
        val = row[sid_idx].strip() if sid_idx < len(row) else ""
        if val:
            existing.add(val)
    print(f"[c2t] {len(existing)} sites already in crm_template", flush=True)
    return existing


# -- Step 5: ensure headers + append rows -------------------------------------

def _ensure_template_headers(svc, tab: str) -> list[str]:
    """Ensure crm_template tab has the correct headers. Returns current headers."""
    result = svc.spreadsheets().values().get(
        spreadsheetId=TEMPLATE_SHEET_ID, range=f"{tab}!1:1"
    ).execute()
    current = result.get("values", [[]])[0] if result.get("values") else []

    if not current:
        # Write full header row
        svc.spreadsheets().values().update(
            spreadsheetId=TEMPLATE_SHEET_ID,
            range=f"{tab}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [TEMPLATE_HEADERS]},
        ).execute()
        print(f"[c2t] Wrote headers to '{tab}'", flush=True)
        return TEMPLATE_HEADERS

    # Add any missing columns at end
    current_lower = [h.lower().strip() for h in current]
    additions = []
    for h in TEMPLATE_HEADERS:
        if h.lower() not in current_lower:
            additions.append(h)

    if additions:
        from openpyxl.utils import get_column_letter
        start_col = get_column_letter(len(current) + 1)
        svc.spreadsheets().values().update(
            spreadsheetId=TEMPLATE_SHEET_ID,
            range=f"{tab}!{start_col}1",
            valueInputOption="USER_ENTERED",
            body={"values": [additions]},
        ).execute()
        print(f"[c2t] Added columns: {additions}", flush=True)
        current.extend(additions)

    return current


def _build_row(site_id: str, contacts: list[dict],
               site_data: dict, headers: list[str]) -> list[str]:
    """Build a single crm_template row from contact group + site_leads data."""
    first = contacts[0]
    today = date.today().strftime("%Y-%m-%d")

    # Contacts JSON
    def _clean_phone(p):
        s = (p or "").lstrip("'").strip()
        return ("'" + s) if s else ""

    def _fmt_contact(c):
        parts = [
            c.get("name", ""),
            c.get("email", ""),
            _clean_phone(c.get("phone", "")).lstrip("'"),
            c.get("title", ""),
        ]
        return ",".join(parts)

    contacts_json = "|" + "|".join(_fmt_contact(c) for c in contacts) + "|"

    tier_label      = first.get("tier_label") or site_data.get("tier_label", "")
    ai_company_type = site_data.get("ai_company_type", "")
    page_count      = first.get("page_count") or site_data.get("page_count", "")
    storrelse       = _map_storrelse(tier_label, ai_company_type, page_count)

    ai_sector   = first.get("ai_sector") or site_data.get("ai_sector", "")
    ai_platform = site_data.get("ai_platform", "")
    location    = site_data.get("location") or first.get("location", "")
    bransje     = " | ".join(x for x in [ai_sector, ai_platform, ai_company_type] if x)
    storrelse_full = " | ".join(x for x in [storrelse, location] if x)

    field_map = {
        "dato lagt i":      today,
        "bedrift":          site_data.get("company") or first.get("domain", ""),
        "nettside":         first.get("website", ""),
        "bransje":          bransje,
        "størrelse":        storrelse_full,
        "land":             first.get("country") or site_data.get("country", ""),
        "site-sider":            str(page_count),
        "beslutningstaker": first.get("name", ""),
        "rolle":            first.get("title", ""),
        "e-post":           first.get("email", ""),
        "telefon":          _clean_phone(first.get("phone", "")),
        "contacts":         contacts_json,
        "score":            "",
        "status":           "",
        "selger":           "",
        "kommentar":        "",
        "tilbud":           "",
        "site_lead_id":     site_id,
        "ai_sector":        ai_sector,
        "ai_company_type":  ai_company_type,
        "oppsummert":       site_data.get("ai_summary", ""),
        "ai_platform":      ai_platform,
    }

    return [field_map.get(h.lower().strip(), "") for h in headers]


def _append_rows(svc, tab: str, rows: list[list[str]]) -> None:
    svc.spreadsheets().values().append(
        spreadsheetId=TEMPLATE_SHEET_ID,
        range=f"{tab}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()
    print(f"[c2t] Appended {len(rows)} rows to '{tab}'", flush=True)


# -- Main ---------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(
        description="Push selected contacts from contact sheet -> CRM template")
    p.add_argument("--contact-tab",  default=CONTACT_TAB,  metavar="TAB")
    p.add_argument("--template-tab", default=TEMPLATE_TAB, metavar="TAB")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be added without writing")
    args = p.parse_args(argv)

    svc = _sheets_service()
    db  = _init_firestore()

    # 1. Read selected contacts
    contacts = _read_selected_contacts(svc, args.contact_tab)
    if not contacts:
        print("[c2t] No selected contacts found.")
        return

    # 2. Group by site
    groups = _group_by_site(contacts)

    # 3. Fetch site_leads enrichment
    site_data = _fetch_site_leads(db, list(groups.keys()))

    # 4. Read existing site_lead_ids from crm_template
    existing = _read_existing_site_ids(svc, args.template_tab)

    # 5. Build new rows — skip already present sites
    headers = _ensure_template_headers(svc, args.template_tab)
    new_rows = []
    skipped  = 0
    for site_id, site_contacts in groups.items():
        if site_id in existing:
            skipped += 1
            continue
        row = _build_row(site_id, site_contacts,
                         site_data.get(site_id, {}), headers)
        new_rows.append(row)

    print(f"[c2t] {len(new_rows)} new rows to add, {skipped} already in template",
          flush=True)

    if not new_rows:
        print("[c2t] Nothing new to add.")
        return

    if args.dry_run:
        print("[c2t] DRY RUN -- first 3 rows:")
        for row in new_rows[:3]:
            print(dict(zip(headers, row)))
        return

    # 6. Append to crm_template sheet
    _append_rows(svc, args.template_tab, new_rows)
    print(f"[c2t] Sheet updated -- {len(new_rows)} rows added.", flush=True)

    # 7. Upsert to Firestore crm/crm_template/items — only the rows just written
    crm_col = db.collection("crm").document("crm_template").collection("items")
    BATCH_SIZE = 400
    pairs = []
    for row in new_rows:
        rec = dict(zip(headers, row))
        site_id = rec.get("site_lead_id", "").strip()
        if site_id:
            pairs.append((site_id, rec))

    count = 0
    for i in range(0, len(pairs), BATCH_SIZE):
        chunk = pairs[i:i + BATCH_SIZE]
        batch = db.batch()
        for doc_id, rec in chunk:
            batch.set(crm_col.document(doc_id), rec, merge=True)
        batch.commit()
        count += len(chunk)
        print(f"[c2t]   Firestore upserted {count}/{len(pairs)}...", flush=True)

    print(f"[c2t] Done -- {len(new_rows)} sites added to sheet + Firestore.")


if __name__ == "__main__":
    main()
