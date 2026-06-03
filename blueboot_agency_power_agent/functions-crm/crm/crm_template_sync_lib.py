"""
crm_template_sync_lib.py -- Sync CRM template sheet -> Firestore + update site_leads.
"""
from __future__ import annotations
import re
from urllib.parse import urlparse, urlunparse
from crm.sheets_config import TEMPLATE_SHEET_ID, TEMPLATE_TAB, CRM_COLLECTION, CRM_TEMPLATE_DOC

HEADER_MAP = {
    "dato lagt i":      "created_date",
    "bedrift":          "company",
    "nettside":         "website",
    "bransje":          "sector",
    "størrelse":        "size",
    "oppsummert":       "ai_summary",
    "land":             "country",
    "site-sider":       "page_count",
    "beslutningstaker": "decision_maker",
    "rolle":            "role",
    "e-post":           "email",
    "telefon":          "phone",
    "contacts":         "contacts",
    "score":            "score",
    "status":           "status",
    "selger":           "seller",
    "kommentar":        "comment",
    "tilbud":           "offer",
    "site_lead_id":     "site_lead_id",
    "ai_sector":        "ai_sector",
    "ai_company_type":  "ai_company_type",
    "ai_platform":      "ai_platform",
}


def _read_sheet(svc, tab=TEMPLATE_TAB) -> list[dict]:
    result = svc.spreadsheets().values().get(
        spreadsheetId=TEMPLATE_SHEET_ID, range=f"{tab}!A:ZZ"
    ).execute()
    rows = result.get("values", [])
    if not rows:
        return []
    raw_headers = rows[0]
    fields = [HEADER_MAP.get(h.lower().strip(), h.lower().strip().replace(" ", "_"))
              for h in raw_headers]
    records = []
    for row in rows[1:]:
        rec = {fields[i]: (row[i].strip() if i < len(row) else "") for i in range(len(fields))}
        if any(v for v in rec.values()):
            records.append(rec)
    print(f"[lib] {len(records)} template rows read", flush=True)
    return records


def _upsert_records(db, records) -> int:
    col = db.collection(CRM_COLLECTION).document(CRM_TEMPLATE_DOC).collection("items")
    # Build doc_id from site_lead_id or website
    def _make_id(rec):
        sid = (rec.get("site_lead_id") or "").strip()
        if sid:
            return sid
        ws = (rec.get("website") or "").strip()
        if ws:
            ws = ws if ws.startswith("http") else "https://" + ws
            host = urlparse(ws).hostname or ws
            slug = re.sub(r"[.\-]+", "_", host.rstrip(".").lower())
            return re.sub(r"_+", "_", slug).strip("_")[:200]
        return ""

    pairs = [(_make_id(r), r) for r in records]
    pairs = [(did, r) for did, r in pairs if did]
    count = 0
    for i in range(0, len(pairs), 400):
        batch = db.batch()
        for doc_id, r in pairs[i:i+400]:
            batch.set(col.document(doc_id), r, merge=True)
        batch.commit()
        count += len(pairs[i:i+400])
    print(f"[lib] upserted {count} docs -> crm/{CRM_TEMPLATE_DOC}/items", flush=True)
    return count


def _update_site_leads(db, records) -> int:
    site_col = db.collection("site_leads")
    updates = []
    for rec in records:
        site_id = (rec.get("site_lead_id") or "").strip()
        if not site_id:
            continue
        patch = {}
        if rec.get("status"):
            patch["crm_status"] = rec["status"]
        if rec.get("seller"):
            patch["crm_sales_person"] = rec["seller"]
        if rec.get("created_date"):
            patch["crm_date"] = rec["created_date"]
        if patch:
            updates.append((site_id, patch))

    count = 0
    for i in range(0, len(updates), 400):
        batch = db.batch()
        for site_id, patch in updates[i:i+400]:
            batch.update(site_col.document(site_id), patch)
        batch.commit()
        count += len(updates[i:i+400])
    print(f"[lib] updated {count} site_leads with CRM fields", flush=True)
    return count


def run_template_sync(db, svc, tab=TEMPLATE_TAB) -> int:
    records = _read_sheet(svc, tab)
    if not records:
        return 0
    _upsert_records(db, records)
    _update_site_leads(db, records)
    return len(records)


def run_template_enrich(db, svc, tab=TEMPLATE_TAB) -> int:
    from openpyxl.utils import get_column_letter

    crm_col  = db.collection(CRM_COLLECTION).document(CRM_TEMPLATE_DOC).collection("items")
    site_col = db.collection("site_leads")

    docs = list(crm_col.stream())
    found, not_found = 0, 0
    updates = []
    sheet_matches = {}

    for doc in docs:
        crm_data = doc.to_dict() or {}
        website  = (crm_data.get("website") or "").strip()
        if not website:
            not_found += 1; continue
        ws = website if website.startswith("http") else "https://" + website
        host = urlparse(ws).hostname or ws
        site_id = re.sub(r"_+", "_",
                         re.sub(r"[.\-]+", "_", host.rstrip(".").lower())).strip("_")[:200]
        site_doc = site_col.document(site_id).get()
        if not site_doc.exists:
            not_found += 1; continue
        site_data = site_doc.to_dict() or {}
        merged = {**site_data, **crm_data}
        merged["site_lead_id"] = site_id
        updates.append((doc.id, merged))
        sheet_matches[website] = site_id
        found += 1

    print(f"[lib] enrich: matched {found}, missed {not_found}", flush=True)

    for i in range(0, len(updates), 400):
        batch = db.batch()
        for doc_id, data in updates[i:i+400]:
            batch.set(crm_col.document(doc_id), data, merge=True)
        batch.commit()

    # Write site_lead_id back to sheet
    if sheet_matches and svc:
        result = svc.spreadsheets().values().get(
            spreadsheetId=TEMPLATE_SHEET_ID, range=f"{tab}!1:1"
        ).execute()
        headers = result.get("values", [[]])[0]
        col_map = {h.lower().strip(): i for i, h in enumerate(headers)}
        sid_idx = col_map.get("site_lead_id", -1)
        if sid_idx >= 0:
            result2 = svc.spreadsheets().values().get(
                spreadsheetId=TEMPLATE_SHEET_ID, range=f"{tab}!A:ZZ"
            ).execute()
            sheet_rows = result2.get("values", [])
            ws_idx = col_map.get("nettside", col_map.get("website", -1))
            col_letter = get_column_letter(sid_idx + 1)
            data = []
            for ri, row in enumerate(sheet_rows[1:], 2):
                ws_val = row[ws_idx].strip() if ws_idx >= 0 and ws_idx < len(row) else ""
                ws_norm = ws_val if ws_val.startswith("http") else "https://" + ws_val
                sid = sheet_matches.get(ws_val) or sheet_matches.get(ws_norm)
                if sid:
                    data.append({"range": f"{tab}!{col_letter}{ri}", "values": [[sid]]})
            if data:
                svc.spreadsheets().values().batchUpdate(
                    spreadsheetId=TEMPLATE_SHEET_ID,
                    body={"valueInputOption": "USER_ENTERED", "data": data},
                ).execute()

    return found
