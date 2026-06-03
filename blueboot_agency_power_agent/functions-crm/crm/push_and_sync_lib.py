"""
push_and_sync_lib.py -- Combined: push selected contacts -> CRM template + sync back.

In one call:
  1. Read contact sheet, filter Select != blank
  2. Push new sites to CRM template sheet
  3. Upsert to crm/crm_template/items in Firestore
  4. Sync crm_status, crm_sales_person, crm_date back to site_leads
"""
from __future__ import annotations
from crm.contact_to_template_lib import (
    _read_selected_contacts, _group_by_site, _fetch_site_leads,
    _read_existing_site_ids, _ensure_headers, _build_row,
    TEMPLATE_HEADERS,
)
from crm.crm_template_sync_lib import _read_sheet, _upsert_records, _update_site_leads
from crm.sheets_config import (
    CONTACT_TAB, TEMPLATE_TAB, TEMPLATE_SHEET_ID,
    CRM_COLLECTION, CRM_TEMPLATE_DOC,
)


def run_push_and_sync(db, svc, contact_tab=CONTACT_TAB,
                      template_tab=TEMPLATE_TAB, dry_run=False) -> dict:
    """
    Full pipeline in one call:
      1. Read contact sheet -> filter selected
      2. Push new sites to CRM template sheet
      3. Upsert to crm/crm_template/items
      4. Sync crm_status / crm_sales_person / crm_date -> site_leads
    Returns dict with counts for each step.
    """
    # Step 1: read selected contacts
    contacts = _read_selected_contacts(svc, contact_tab)
    if not contacts:
        print("[push-sync] No selected contacts found.", flush=True)
        return {"selected": 0, "pushed": 0, "synced": 0}

    # Step 2: group by site, fetch enrichment, find new ones
    groups    = _group_by_site(contacts)
    site_data = _fetch_site_leads(db, list(groups.keys()))
    existing  = _read_existing_site_ids(svc, template_tab)
    headers   = _ensure_headers(svc, template_tab)

    new_rows = []
    for site_id, site_contacts in groups.items():
        if site_id not in existing:
            new_rows.append(_build_row(site_id, site_contacts,
                                       site_data.get(site_id, {}), headers))

    print(f"[push-sync] {len(new_rows)} new sites to push ({len(groups) - len(new_rows)} already in template)", flush=True)

    if dry_run:
        print(f"[push-sync] DRY RUN -- would push {len(new_rows)} sites:")
        for row in new_rows[:5]:
            rec = dict(zip(headers, row))
            print(f"  {rec.get('site_lead_id','?')} | {rec.get('bedrift') or rec.get('nettside','?')}")
        if len(new_rows) > 5:
            print(f"  ... and {len(new_rows)-5} more")
        return {"selected": len(contacts), "pushed": len(new_rows), "synced": 0}

    # Step 3: append to sheet + upsert to Firestore
    pushed = 0
    if new_rows:
        svc.spreadsheets().values().append(
            spreadsheetId=TEMPLATE_SHEET_ID,
            range=f"{template_tab}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": new_rows},
        ).execute()
        pushed = len(new_rows)
        print(f"[push-sync] {pushed} rows appended to sheet", flush=True)

        crm_col = db.collection(CRM_COLLECTION).document(CRM_TEMPLATE_DOC).collection("items")
        pairs = []
        for row in new_rows:
            rec = dict(zip(headers, row))
            sid = rec.get("site_lead_id", "").strip()
            if sid:
                pairs.append((sid, rec))
        for i in range(0, len(pairs), 400):
            batch = db.batch()
            for sid, rec in pairs[i:i+400]:
                batch.set(crm_col.document(sid), rec, merge=True)
            batch.commit()
        print(f"[push-sync] {len(pairs)} docs upserted to crm/{CRM_TEMPLATE_DOC}/items", flush=True)

    # Step 4: read full template sheet and sync CRM fields back to site_leads
    print("[push-sync] Syncing CRM fields back to site_leads...", flush=True)
    all_records = _read_sheet(svc, template_tab)
    synced = _update_site_leads(db, all_records)

    print(f"[push-sync] Done -- pushed={pushed}, synced={synced}", flush=True)
    return {"selected": len(contacts), "pushed": pushed, "synced": synced}
