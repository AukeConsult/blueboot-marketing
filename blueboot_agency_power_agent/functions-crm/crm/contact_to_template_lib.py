"""
contact_to_template_lib.py -- Push selected contacts -> CRM template sheet.
"""
from __future__ import annotations
import re
from datetime import date
from collections import defaultdict
from crm.sheets_config import (
    CONTACT_SHEET_ID, CONTACT_TAB,
    TEMPLATE_SHEET_ID, TEMPLATE_TAB,
    CRM_COLLECTION, CRM_TEMPLATE_DOC,
)

TEMPLATE_HEADERS = [
    "Dato lagt i", "Bedrift", "Nettside", "Bransje", "Størrelse",
    "Oppsummert", "Land", "Site-sider", "Beslutningstaker", "Rolle",
    "E-post", "Telefon", "Contacts", "Score", "Status", "Selger",
    "Kommentar", "Tilbud", "site_lead_id", "ai_sector", "ai_company_type", "ai_platform",
]


def _map_storrelse(page_count):
    try:
        p = int(page_count or 0)
    except (ValueError, TypeError):
        p = 0
    if p >= 25000: return "Ultra Enterprise"
    if p >= 5000:  return "Enterprise"
    if p >= 2000:  return "Stor"
    if p >= 500:   return "Mellomstor"
    return "Liten"


def _lead_id_from_url(url: str) -> str:
    from urllib.parse import urlparse
    host = urlparse(url).hostname or url
    slug = re.sub(r"[.\-]+", "_", host.rstrip(".").lower())
    return re.sub(r"_+", "_", slug).strip("_")[:200]


def _normalize_url(url: str) -> str:
    from urllib.parse import urlparse, urlunparse
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, "/", "", "", ""))


def _read_selected_contacts(svc, tab):
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
        return []
    records = []
    for row in rows[1:]:
        if (row[select_idx].strip() if select_idx < len(row) else ""):
            records.append({h: (row[i].strip() if i < len(row) else "") for h, i in col.items()})
    print(f"[lib] {len(records)} selected contacts", flush=True)
    return records


def _group_by_site(contacts):
    groups = defaultdict(list)
    for c in contacts:
        site_id = (c.get("lead_id_site") or "").strip()
        if not site_id:
            ws = c.get("website", "")
            if ws:
                ws = ws if ws.startswith("http") else "https://" + ws
                site_id = _lead_id_from_url(_normalize_url(ws))
        if site_id:
            groups[site_id].append(c)
    return dict(groups)


def _fetch_site_leads(db, site_ids):
    col = db.collection("site_leads")
    result = {}
    for sid in site_ids:
        doc = col.document(sid).get()
        if doc.exists:
            result[sid] = doc.to_dict() or {}
    return result


def _read_existing_site_ids(svc, tab):
    result = svc.spreadsheets().values().get(
        spreadsheetId=TEMPLATE_SHEET_ID, range=f"{tab}!A:ZZ"
    ).execute()
    rows = result.get("values", [])
    if not rows:
        return set()
    headers = rows[0]
    col_map = {h.lower().strip(): i for i, h in enumerate(headers)}
    sid_idx = col_map.get("site_lead_id", -1)
    if sid_idx < 0:
        return set()
    return {row[sid_idx].strip() for row in rows[1:] if sid_idx < len(row) and row[sid_idx].strip()}


def _ensure_headers(svc, tab):
    result = svc.spreadsheets().values().get(
        spreadsheetId=TEMPLATE_SHEET_ID, range=f"{tab}!1:1"
    ).execute()
    current = result.get("values", [[]])[0] if result.get("values") else []
    if not current:
        svc.spreadsheets().values().update(
            spreadsheetId=TEMPLATE_SHEET_ID, range=f"{tab}!A1",
            valueInputOption="USER_ENTERED", body={"values": [TEMPLATE_HEADERS]},
        ).execute()
        return TEMPLATE_HEADERS
    current_lower = [h.lower().strip() for h in current]
    additions = [h for h in TEMPLATE_HEADERS if h.lower() not in current_lower]
    if additions:
        from openpyxl.utils import get_column_letter
        col = get_column_letter(len(current) + 1)
        svc.spreadsheets().values().update(
            spreadsheetId=TEMPLATE_SHEET_ID, range=f"{tab}!{col}1",
            valueInputOption="USER_ENTERED", body={"values": [additions]},
        ).execute()
        current.extend(additions)
    return current


def _clean_phone(p):
    s = (p or "").lstrip("'").strip()
    return ("'" + s) if s else ""


def _build_row(site_id, contacts, site_data, headers):
    first           = contacts[0]
    ai_company_type = site_data.get("ai_company_type", "")
    ai_sector       = first.get("ai_sector") or site_data.get("ai_sector", "")
    ai_platform     = site_data.get("ai_platform", "")
    page_count      = first.get("page_count") or site_data.get("page_count", "")
    location        = site_data.get("location") or first.get("location", "")
    storrelse       = " | ".join(x for x in [_map_storrelse(page_count), location] if x)
    bransje         = " | ".join(x for x in [ai_sector, ai_platform, ai_company_type] if x)

    def fmt(c):
        return ",".join([c.get("name",""), c.get("email",""),
                         _clean_phone(c.get("phone","")).lstrip("'"), c.get("title","")])
    contacts_str = "|" + "|".join(fmt(c) for c in contacts) + "|"

    field_map = {
        "dato lagt i":      date.today().strftime("%Y-%m-%d"),
        "bedrift":          site_data.get("company") or first.get("domain", ""),
        "nettside":         first.get("website", ""),
        "bransje":          bransje,
        "størrelse":        storrelse,
        "oppsummert":       site_data.get("ai_summary", ""),
        "land":             first.get("country") or site_data.get("country", ""),
        "site-sider":       str(page_count),
        "beslutningstaker": first.get("name", ""),
        "rolle":            first.get("title", ""),
        "e-post":           first.get("email", ""),
        "telefon":          _clean_phone(first.get("phone", "")),
        "contacts":         contacts_str,
        "score":            "", "status": "", "selger": "",
        "kommentar":        "", "tilbud": "",
        "site_lead_id":     site_id,
        "ai_sector":        ai_sector,
        "ai_company_type":  ai_company_type,
        "ai_platform":      ai_platform,
    }
    return [field_map.get(h.lower().strip(), "") for h in headers]


def run_push_selected(db, svc, contact_tab=CONTACT_TAB, template_tab=TEMPLATE_TAB) -> int:
    contacts = _read_selected_contacts(svc, contact_tab)
    if not contacts:
        return 0
    groups    = _group_by_site(contacts)
    site_data = _fetch_site_leads(db, list(groups.keys()))
    existing  = _read_existing_site_ids(svc, template_tab)
    headers   = _ensure_headers(svc, template_tab)

    new_rows = []
    for site_id, site_contacts in groups.items():
        if site_id not in existing:
            new_rows.append(_build_row(site_id, site_contacts,
                                       site_data.get(site_id, {}), headers))

    if not new_rows:
        print("[lib] Nothing new to push", flush=True)
        return 0

    svc.spreadsheets().values().append(
        spreadsheetId=TEMPLATE_SHEET_ID, range=f"{template_tab}!A1",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": new_rows},
    ).execute()

    # Upsert to Firestore
    crm_col = db.collection(CRM_COLLECTION).document(CRM_TEMPLATE_DOC).collection("items")
    pairs = [(dict(zip(headers, r)).get("site_lead_id","").strip(), dict(zip(headers, r)))
             for r in new_rows]
    pairs = [(sid, rec) for sid, rec in pairs if sid]
    for i in range(0, len(pairs), 400):
        batch = db.batch()
        for sid, rec in pairs[i:i+400]:
            batch.set(crm_col.document(sid), rec, merge=True)
        batch.commit()

    print(f"[lib] push_selected: {len(new_rows)} sites added", flush=True)
    return len(new_rows)
