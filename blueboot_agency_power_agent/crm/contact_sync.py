"""
contact_sync.py -- Export a subset of email_contacts (Firestore) into the CRM Google Sheet.

Reads email_contacts filtered by country, writes to the 'contacts' tab.
Creates the tab if it doesn't exist. Clears and rewrites on each run.

Usage:
    python crm/contact_sync.py --countries NO
    python crm/contact_sync.py --countries NO UK --status pending
    python crm/contact_sync.py --countries NO --max 200
"""
from __future__ import annotations

import sys
import threading
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import _pathsetup  # noqa: F401,F811

from dotenv import load_dotenv
load_dotenv()

from functions.utils import resolve_country, normalize_url, email_matches_name
from functions.firebase_cred import get_firebase_cred
import firebase_admin
from firebase_admin import firestore
import firebase_admin.credentials as fb_creds

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# -- Config -------------------------------------------------------------------
SHEET_ID      = "1aMglV53NiMEArjld37HN5cxliyNRGzIP2mrM4kwlupA"
TAB_NAME      = "contacts"
CRM_COLLECTION = "crm"
CRM_DOC        = "contact_select"
SCOPES        = ["https://www.googleapis.com/auth/spreadsheets"]
TOKEN_PATH    = str(Path(__file__).resolve().parent.parent / "config" / "google_token.json")
CLIENT_SECRET = str(Path(__file__).resolve().parent.parent / "config" / "google_oauth_client.json")

# Columns to write: (header_label, firestore_field)
# None as field = empty column (e.g. manual Select checkbox)
COLS = [
    # Manual
    ("Select",           None),
    ("Campaign",         "campaign"),
    # Contact
    ("Tier",             "tier_label"),
    ("Outreach",         "outreach_priority"),
    ("Status",           "status"),
    ("Email",            "email"),
    ("Website",          "website"),
    ("Name",             "name"),
    ("Title",            "title"),
    ("Phone",            "phone"),
    ("LinkedIn",         "linkedin"),
    ("Email Type",       "email_type"),
    ("Contact Role",     "contact_type"),
    # Source
    ("Domain",           "domain"),
    ("Country",          "country"),
    ("Location",         "location"),
    ("City",             "location_city"),
    ("Region",           "location_region"),
    # Classification
    ("Platform",         "ai_platform"),
    ("Sector",           "ai_sector"),
    ("Client Base",      "ai_client_base"),
    ("Company Type",     "ai_company_type"),
    ("Pages",            "page_count"),
    ("Confidence",       "ai_confidence"),
    ("Summary",          "ai_summary"),
    ("Keywords",         "keywords"),
    # Origin
    # Pipeline marks
    # IDs
    ("Lead ID Site",     "lead_id_site"),
    ("Lead ID Leads",    "lead_id_leads"),
    # Lifecycle
    ("Created",          "created_at"),
    # Index
    ("Doc ID",           "doc_id"),
]

# -- Firestore ----------------------------------------------------------------
_fb_lock = threading.Lock()


def _init_firestore():
    cred_obj = get_firebase_cred()
    cred = cred_obj if isinstance(cred_obj, fb_creds.Base) else fb_creds.Certificate(cred_obj)
    with _fb_lock:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
    return firestore.client()


def _load_contacts(db, countries=None, campaign=None, status=None,
                   collection="email_contacts"):
    from google.cloud.firestore_v1.base_query import FieldFilter

    col = db.collection(collection)
    query = col.where(filter=FieldFilter("mark_site_leads", "==", True))
    if campaign:
        query = query.where(filter=FieldFilter("campaign", "==", campaign))
    if status:
        query = query.where(filter=FieldFilter("status", "==", status))

    print(f"[crm-export] Loading {collection}...", flush=True)
    docs = list(query.stream())
    print(f"[crm-export] {len(docs)} docs fetched", flush=True)

    rows, skipped = [], 0
    for doc in docs:
        d = doc.to_dict() or {}
        if not d.get("doc_id"):
            d = dict(d)
            d["doc_id"] = doc.id

        if countries:
            if resolve_country(d) not in countries:
                skipped += 1
                continue

        email = (d.get("email") or "").strip()
        name  = (d.get("name")  or "").strip()
        if name and not email_matches_name(email, name):
            d = dict(d)
            d["name"] = ""

        if d.get("website"):
            d = dict(d)
            d["website"] = normalize_url(d["website"] or "")

        rows.append(d)

    print(f"[crm-export] {len(rows)} rows after filter  ({skipped} skipped)", flush=True)
    return rows


# -- Google Sheets ------------------------------------------------------------

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


def _ensure_tab(svc, sheet_id, tab):
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tabs = [s["properties"]["title"] for s in meta["sheets"]]
    if tab not in tabs:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
        ).execute()
        print(f"[crm-export] Created tab '{tab}'")
    else:
        print(f"[crm-export] Tab '{tab}' exists")


def _val(v, field=""):
    """Coerce any Firestore value to a plain string for Sheets."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "YES" if v else ""
    if isinstance(v, list):
        return ", ".join(str(i) for i in v if i not in (None, ""))
    if isinstance(v, dict):
        return "; ".join(f"{k}={w}" for k, w in v.items() if w not in (None, ""))
    # Firestore DatetimeWithNanoseconds -- date only
    if hasattr(v, "isoformat"):
        return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else v.isoformat()[:10]
    # Outreach priority: map 1-4 to label
    if field == "outreach_priority":
        return {1: "Direct", 2: "Strong", 3: "Role/Dept", 4: "Admin/Generic"}.get(int(v) if str(v).isdigit() else 0, str(v))
    # Phone: apostrophe prefix forces Sheets to treat as text
    if field == "phone":
        s = str(v).strip()
        return ("'" + s) if s else ""
    return str(v)


def _write_to_sheet(svc, sheet_id, tab, rows):
    headers = [h for h, _ in COLS]
    sheet_rows = [headers]
    for r in rows:
        sheet_rows.append([("" if field is None else _val(r.get(field), field)) for _, field in COLS])

    print(f"[crm-export] Clearing sheet tab '{tab}'...", flush=True)
    svc.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=f"{tab}!A:ZZ"
    ).execute()

    CHUNK = 200
    total_rows = len(sheet_rows) - 1  # exclude header
    print(f"[crm-export] Writing {total_rows} rows in chunks of {CHUNK}...", flush=True)
    for i, start in enumerate(range(0, len(sheet_rows), CHUNK)):
        chunk = sheet_rows[start:start + CHUNK]
        # Row 1 = header (index 0), data starts at row 2
        start_row = start + 1
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{tab}!A{start_row}",
            valueInputOption="USER_ENTERED",
            body={"values": chunk},
        ).execute()
        written = min(start + CHUNK, len(sheet_rows)) - 1
        print(f"[crm-export]   wrote {max(0, written)}/{total_rows} rows...", flush=True)
    print(f"[crm-export] Wrote {total_rows} rows + header -> {tab}")


# -- Firestore CRM sync -------------------------------------------------------

def _upsert_to_crm(db, rows: list[dict]) -> int:
    """Write exported rows to crm/contact_select/{doc_id} using batch commits."""
    col   = db.collection(CRM_COLLECTION).document(CRM_DOC).collection("items")
    count = 0
    total = len(rows)
    BATCH_SIZE = 400  # Firestore max is 500 ops per batch

    valid = [(r.get("doc_id") or "").strip() for r in rows]
    pairs = [(did, r) for did, r in zip(valid, rows) if did]

    for i in range(0, len(pairs), BATCH_SIZE):
        chunk = pairs[i:i + BATCH_SIZE]
        batch = db.batch()
        for doc_id, r in chunk:
            batch.set(col.document(doc_id), r, merge=True)
        batch.commit()
        count += len(chunk)
        print(f"[crm-sync]   upserted {count}/{total}...", flush=True)

    print(f"[crm-sync] Upserted {count} docs -> crm/{CRM_DOC}/items")
    return count


# Fields where the sheet value takes precedence over Firestore on sync
SHEET_PRECEDENCE_FIELDS = {"select", "campaign"}


def _read_sheet_overrides(svc, sheet_id: str, tab: str) -> dict:
    """Read sheet and return {doc_id: {field: value}} for user-editable fields.

    Sheet has precedence over Firestore for SHEET_PRECEDENCE_FIELDS.
    """
    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{tab}!A:ZZ"
    ).execute()
    sheet_rows = result.get("values", [])
    if not sheet_rows:
        return {}

    headers = [h.lower().replace(" ", "_") for h in sheet_rows[0]]
    # map normalised header -> original col name for precedence lookup
    col_map = {h: i for i, h in enumerate(headers)}

    docid_idx  = col_map.get("doc_id", -1)
    select_idx = col_map.get("select", -1)
    campaign_idx = col_map.get("campaign", -1)

    overrides = {}
    for row in sheet_rows[1:]:
        doc_id = (row[docid_idx].strip() if docid_idx >= 0 and docid_idx < len(row) else "")
        if not doc_id:
            continue
        entry = {}
        if select_idx >= 0 and select_idx < len(row):
            entry["select"] = row[select_idx].strip()
        if campaign_idx >= 0 and campaign_idx < len(row):
            entry["campaign"] = row[campaign_idx].strip()
        overrides[doc_id] = entry

    print(f"[crm-export] Read {len(overrides)} existing sheet rows (overrides captured)")
    return overrides


def _fetch_by_docids(db, doc_ids: list[str], collection: str = "email_contacts") -> dict:
    """Fetch email_contacts docs by doc_id in batches of 30. Returns {doc_id: data}."""
    from google.cloud.firestore_v1.base_query import FieldFilter
    col = db.collection(collection)
    result = {}
    batch_size = 30
    total_batches = (len(doc_ids) + batch_size - 1) // batch_size
    for i in range(0, len(doc_ids), batch_size):
        batch_num = i // batch_size + 1
        batch = doc_ids[i:i + batch_size]
        print(f"[crm-sync]   fetching batch {batch_num}/{total_batches} ({len(batch)} ids)...", flush=True)
        docs = col.where(filter=FieldFilter("doc_id", "in", batch)).stream()
        for doc in docs:
            d = doc.to_dict() or {}
            did = d.get("doc_id") or doc.id
            result[did] = d
    print(f"[crm-sync] Fetched {len(result)}/{len(doc_ids)} docs from {collection}")
    return result


def _merge_rows(sheet_overrides: dict, firestore_rows: dict) -> list[dict]:
    """Merge Firestore data with sheet overrides. Sheet wins for SHEET_PRECEDENCE_FIELDS."""
    merged = []
    for doc_id, fs_data in firestore_rows.items():
        row = dict(fs_data)
        if not row.get("doc_id"):
            row["doc_id"] = doc_id
        # Apply sheet overrides — sheet has precedence
        sheet_vals = sheet_overrides.get(doc_id, {})
        for field in SHEET_PRECEDENCE_FIELDS:
            if field in sheet_vals and sheet_vals[field] != "":
                row[field] = sheet_vals[field]
        merged.append(row)
    # Also include sheet rows whose doc_id was not found in Firestore (keep as-is)
    fs_ids = set(firestore_rows.keys())
    for doc_id, sheet_vals in sheet_overrides.items():
        if doc_id not in fs_ids:
            row = {"doc_id": doc_id}
            row.update(sheet_vals)
            merged.append(row)
    return merged


def sync_mode(svc, db, tab: str, collection: str = "email_contacts") -> None:
    """Full two-way sync:
      1. Read sheet -> capture Select + Campaign overrides
      2. Fetch matching email_contacts from Firestore by doc_id
      3. Merge (sheet wins for Select/Campaign)
      4. Write merged rows back to sheet
      5. Upsert merged rows to crm/contact_select/items
    """
    print("[crm-sync] Step 1: reading sheet overrides...")
    overrides = _read_sheet_overrides(svc, SHEET_ID, tab)
    if not overrides:
        print("[crm-sync] Sheet is empty -- nothing to sync. Run export first.")
        return

    print(f"[crm-sync] Step 2: fetching {len(overrides)} contacts from {collection}...")
    doc_ids = list(overrides.keys())
    fs_rows = _fetch_by_docids(db, doc_ids, collection)

    print("[crm-sync] Step 3: merging (sheet precedence for Select + Campaign)...")
    rows = _merge_rows(overrides, fs_rows)
    rows.sort(key=lambda r: (int(r.get("tier") or 9), (r.get("company") or "").lower()))

    print(f"[crm-sync] Step 4: writing {len(rows)} rows back to sheet...")
    _ensure_tab(svc, SHEET_ID, tab)
    _write_to_sheet(svc, SHEET_ID, tab, rows)

    print("[crm-sync] Step 5: upserting to Firestore crm/contact_select/items...")
    _upsert_to_crm(db, rows)

    print(f"[crm-sync] Done -- {len(rows)} contacts synced.")


# -- Main ---------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description="Export email_contacts -> CRM Google Sheet")
    p.add_argument("--countries", nargs="+", default=None, metavar="CC",
                   help="Country codes e.g. NO UK")
    p.add_argument("--campaign",   default=None, metavar="NAME")
    p.add_argument("--status",     default=None, metavar="STATUS",
                   help="pending / approved / sent")
    p.add_argument("--collection", default="email_contacts", metavar="NAME")
    p.add_argument("--tab",        default=TAB_NAME, metavar="TAB",
                   help=f"Sheet tab name (default: {TAB_NAME})")
    p.add_argument("--max",        default=None, type=int, metavar="N",
                   help="Max number of rows to write (after sort)")
    args = p.parse_args(argv)

    countries = None
    if args.countries:
        raw = []
        for t in args.countries:
            raw.extend(c.strip().upper() for c in t.split(",") if c.strip())
        countries = raw or None

    db  = _init_firestore()
    svc = _sheets_service()

    # Always read sheet first to capture existing doc_ids + overrides
    print("[crm-export] Reading existing sheet...", flush=True)
    _ensure_tab(svc, SHEET_ID, args.tab)
    existing_overrides = _read_sheet_overrides(svc, SHEET_ID, args.tab)
    existing_doc_ids   = set(existing_overrides.keys())
    print(f"[crm-export] {len(existing_doc_ids)} rows already in sheet", flush=True)

    rows = _load_contacts(db, countries=countries, campaign=args.campaign,
                          status=args.status, collection=args.collection)

    if not rows:
        print("[crm-export] No contacts found -- sheet not updated.")
        return

    # Skip rows already present in the sheet
    new_rows = [r for r in rows if (r.get("doc_id") or "").strip() not in existing_doc_ids]
    skipped  = len(rows) - len(new_rows)
    print(f"[crm-export] {len(new_rows)} new rows (skipped {skipped} already in sheet)", flush=True)

    if not new_rows:
        print("[crm-export] Nothing new to add.")
        return

    new_rows.sort(key=lambda r: (int(r.get("tier") or 9), (r.get("company") or "").lower()))
    if args.max:
        new_rows = new_rows[:args.max]
        print(f"[crm-export] Capped to {len(new_rows)} new rows (--max {args.max})")

    # Append new rows to sheet (don't touch existing rows)
    print(f"[crm-export] Appending {len(new_rows)} new rows to sheet...", flush=True)
    headers = [h for h, _ in COLS]
    append_rows = [headers] if not existing_doc_ids else []
    for r in new_rows:
        append_rows.append([("" if field is None else _val(r.get(field), field)) for _, field in COLS])

    if existing_doc_ids:
        # Append after existing data
        svc.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"{args.tab}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": append_rows},
        ).execute()
    else:
        # Sheet was empty — write with headers
        svc.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{args.tab}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": append_rows},
        ).execute()

    print(f"[crm-export] Sheet updated — {len(new_rows)} rows added")

    _upsert_to_crm(db, new_rows)
    print(f"\n[crm-export] Done -- {len(new_rows)} new contacts added.")


def sync_back(argv=None):
    """Full sync: sheet -> merge with Firestore -> write back to sheet + Firestore."""
    p = argparse.ArgumentParser(description="Sync sheet <-> Firestore crm/contact_select")
    p.add_argument("--tab",        default=TAB_NAME, metavar="TAB")
    p.add_argument("--collection", default="email_contacts", metavar="NAME")
    args = p.parse_args(argv)

    db  = _init_firestore()
    svc = _sheets_service()
    sync_mode(svc, db, tab=args.tab, collection=args.collection)


if __name__ == "__main__":
    import sys as _sys
    if "--sync-back" in _sys.argv:
        _sys.argv.remove("--sync-back")
        sync_back()
    else:
        main()
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 