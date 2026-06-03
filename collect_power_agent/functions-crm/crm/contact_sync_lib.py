"""
contact_sync_lib.py -- Library wrapper for contact_sync operations.

Accepts db + svc as arguments (no auth setup inside).
"""
from __future__ import annotations
from google.cloud.firestore_v1.base_query import FieldFilter
from crm.sheets_config import CONTACT_SHEET_ID, CONTACT_TAB, CRM_COLLECTION, CRM_CONTACT_DOC

COLS = [
    ("Select",        None),
    ("Campaign",      "campaign"),
    ("Tier",          "tier_label"),
    ("Outreach",      "outreach_priority"),
    ("Status",        "status"),
    ("Email",         "email"),
    ("Website",       "website"),
    ("Name",          "name"),
    ("Title",         "title"),
    ("Phone",         "phone"),
    ("LinkedIn",      "linkedin"),
    ("Email Type",    "email_type"),
    ("Contact Role",  "contact_type"),
    ("Domain",        "domain"),
    ("Country",       "country"),
    ("Location",      "location"),
    ("City",          "location_city"),
    ("Region",        "location_region"),
    ("Platform",      "ai_platform"),
    ("Sector",        "ai_sector"),
    ("Client Base",   "ai_client_base"),
    ("Company Type",  "ai_company_type"),
    ("Pages",         "page_count"),
    ("Confidence",    "ai_confidence"),
    ("Summary",       "ai_summary"),
    ("Keywords",      "keywords"),
    ("Lead ID Site",  "lead_id_site"),
    ("Lead ID Leads", "lead_id_leads"),
    ("Created",       "created_at"),
    ("Doc ID",        "doc_id"),
]

OUTREACH_LABELS = {1: "Direct", 2: "Strong", 3: "Role/Dept", 4: "Admin/Generic"}


def _val(v, field=""):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "YES" if v else ""
    if isinstance(v, list):
        return ", ".join(str(i) for i in v if i not in (None, ""))
    if isinstance(v, dict):
        return "; ".join(f"{k}={w}" for k, w in v.items() if w not in (None, ""))
    if hasattr(v, "isoformat"):
        return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else v.isoformat()[:10]
    if field == "outreach_priority":
        return OUTREACH_LABELS.get(int(v) if str(v).isdigit() else 0, str(v))
    if field == "phone":
        s = str(v).strip()
        return ("'" + s) if s else ""
    return str(v)


def _load_contacts(db, countries=None, campaign=None, status=None,
                   min_pages=None, max_pages=None,
                   collection="email_contacts"):
    col   = db.collection(collection)
    query = col.where(filter=FieldFilter("mark_site_leads", "==", True))
    if campaign:
        query = query.where(filter=FieldFilter("campaign", "==", campaign))
    if status:
        query = query.where(filter=FieldFilter("status", "==", status))

    docs = list(query.stream())
    rows, skipped = [], 0
    for doc in docs:
        d = doc.to_dict() or {}
        if not d.get("doc_id"):
            d = dict(d); d["doc_id"] = doc.id
        if countries:
            cc = (d.get("country") or d.get("ai_country") or "").upper()
            if cc not in countries:
                skipped += 1; continue
        if min_pages is not None:
            try:
                if int(d.get("page_count") or 0) < min_pages:
                    skipped += 1; continue
            except (ValueError, TypeError):
                skipped += 1; continue
        if max_pages is not None:
            try:
                if int(d.get("page_count") or 0) > max_pages:
                    skipped += 1; continue
            except (ValueError, TypeError):
                pass
        rows.append(d)
    print(f"[lib] {len(rows)} contacts loaded ({skipped} skipped)", flush=True)
    return rows


def _read_existing_doc_ids(svc, sheet_id, tab):
    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{tab}!A:ZZ"
    ).execute()
    rows = result.get("values", [])
    if not rows:
        return {}
    headers = [h.lower().replace(" ", "_") for h in rows[0]]
    col_map = {h: i for i, h in enumerate(headers)}
    docid_idx    = col_map.get("doc_id", -1)
    select_idx   = col_map.get("select", -1)
    campaign_idx = col_map.get("campaign", -1)
    overrides = {}
    for row in rows[1:]:
        did = row[docid_idx].strip() if docid_idx >= 0 and docid_idx < len(row) else ""
        if not did:
            continue
        entry = {}
        if select_idx >= 0 and select_idx < len(row):
            entry["select"] = row[select_idx].strip()
        if campaign_idx >= 0 and campaign_idx < len(row):
            entry["campaign"] = row[campaign_idx].strip()
        overrides[did] = entry
    return overrides


def _upsert_to_crm(db, rows):
    col = db.collection(CRM_COLLECTION).document(CRM_CONTACT_DOC).collection("items")
    pairs = [(r.get("doc_id", "").strip(), r) for r in rows if r.get("doc_id", "").strip()]
    for i in range(0, len(pairs), 400):
        batch = db.batch()
        for doc_id, r in pairs[i:i+400]:
            batch.set(col.document(doc_id), r, merge=True)
        batch.commit()
    return len(pairs)


def run_contact_sync(db, svc, countries=None, status=None, campaign=None,
                     max_rows=None, min_pages=None, max_pages=None,
                     tab=CONTACT_TAB) -> int:
    """Append new contacts to contact sheet + upsert to Firestore."""
    existing = _read_existing_doc_ids(svc, CONTACT_SHEET_ID, tab)
    existing_ids = set(existing.keys())
    print(f"[lib] {len(existing_ids)} rows already in sheet", flush=True)

    rows = _load_contacts(db, countries=countries, campaign=campaign, status=status,
                          min_pages=min_pages, max_pages=max_pages)
    new_rows = [r for r in rows if (r.get("doc_id") or "").strip() not in existing_ids]
    new_rows.sort(key=lambda r: (int(r.get("tier") or 9), (r.get("company") or "").lower()))
    if max_rows:
        new_rows = new_rows[:max_rows]

    if not new_rows:
        print("[lib] Nothing new to add", flush=True)
        return 0

    headers = [h for h, _ in COLS]
    append_rows = [] if existing_ids else [headers]
    for r in new_rows:
        append_rows.append([("" if f is None else _val(r.get(f), f)) for _, f in COLS])

    if existing_ids:
        svc.spreadsheets().values().append(
            spreadsheetId=CONTACT_SHEET_ID, range=f"{tab}!A1",
            valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
            body={"values": append_rows},
        ).execute()
    else:
        svc.spreadsheets().values().update(
            spreadsheetId=CONTACT_SHEET_ID, range=f"{tab}!A1",
            valueInputOption="USER_ENTERED", body={"values": append_rows},
        ).execute()

    _upsert_to_crm(db, new_rows)
    print(f"[lib] contact_sync: {len(new_rows)} rows added", flush=True)
    return len(new_rows)


def run_sync_back(db, svc, tab=CONTACT_TAB) -> None:
    """Full sync: read sheet overrides, re-fetch Firestore data, merge, write back."""
    from google.cloud.firestore_v1.base_query import FieldFilter
    overrides = _read_existing_doc_ids(svc, CONTACT_SHEET_ID, tab)
    if not overrides:
        print("[lib] Sheet is empty -- nothing to sync back")
        return
    doc_ids  = list(overrides.keys())
    col      = db.collection("email_contacts")
    fs_rows  = {}
    for i in range(0, len(doc_ids), 30):
        batch = doc_ids[i:i+30]
        for doc in col.where(filter=FieldFilter("doc_id", "in", batch)).stream():
            d = doc.to_dict() or {}
            fs_rows[d.get("doc_id") or doc.id] = d

    merged = []
    for doc_id, overrides_vals in overrides.items():
        row = dict(fs_rows.get(doc_id, {"doc_id": doc_id}))
        for field in ("select", "campaign"):
            if overrides_vals.get(field):
                row[field] = overrides_vals[field]
        merged.append(row)

    merged.sort(key=lambda r: (int(r.get("tier") or 9), (r.get("company") or "").lower()))
    headers  = [h for h, _ in COLS]
    rows_out = [headers]
    for r in merged:
        rows_out.append([("" if f is None else _val(r.get(f), f)) for _, f in COLS])

    CHUNK = 200
    svc.spreadsheets().values().clear(
        spreadsheetId=CONTACT_SHEET_ID, range=f"{tab}!A:ZZ"
    ).execute()
    for start in range(0, len(rows_out), CHUNK):
        svc.spreadsheets().values().update(
            spreadsheetId=CONTACT_SHEET_ID,
            range=f"{tab}!A{start+1}",
            valueInputOption="USER_ENTERED",
            body={"values": rows_out[start:start+CHUNK]},
        ).execute()
    _upsert_to_crm(db, merged)
    print(f"[lib] sync_back: {len(merged)} rows written", flush=True)
