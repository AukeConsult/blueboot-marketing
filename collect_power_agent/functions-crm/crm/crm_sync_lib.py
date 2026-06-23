"""
crm_sync_lib.py -- Sync from the master CRM contact sheet to Firestore.

Three steps:
  1. Read master contact sheet -> upsert crm/contact_select/items (sheet wins)
  2. Update email_contacts.campaign (sheet always wins)
  3. Create/update campaigns/{campaign_id} with statistics + campaign_contacts subcollection

This is the "overall" CRM sync that discovers and creates new campaigns
from the master sheet. See campaign_sync_lib.py for the per-campaign
Drive-sheet sync.
"""
from __future__ import annotations
from datetime import datetime, timezone
from collections import Counter, defaultdict
from crm.sheets_config import CONTACT_SHEET_ID, CONTACT_TAB, CRM_COLLECTION, CRM_CONTACT_DOC

CAMPAIGNS_COLLECTION = "campaigns"
CONTACT_STATUSES = {"pending", "active", "excluded"}


def _contact_status(value) -> str:
    status = str(value or "pending").strip().lower()
    if status in CONTACT_STATUSES:
        return status
    return "pending"


def _read_sheet_contacts(svc, tab: str) -> list[dict]:
    result = svc.spreadsheets().values().get(
        spreadsheetId=CONTACT_SHEET_ID, range=f"{tab}!A:ZZ"
    ).execute()
    rows = result.get("values", [])
    if not rows:
        return []
    headers = [h.lower().replace(" ", "_") for h in rows[0]]
    col = {h: i for i, h in enumerate(headers)}
    records = []
    for row in rows[1:]:
        rec = {h: (row[i].strip() if i < len(row) else "") for h, i in col.items()}
        if any(rec.values()):
            records.append(rec)
    print(f"[crm-sync] Read {len(records)} rows from master sheet", flush=True)
    return records


def _sync_contact_select(db, records: list[dict]) -> int:
    col = db.collection(CRM_COLLECTION).document(CRM_CONTACT_DOC).collection("items")
    BATCH_SIZE = 400
    pairs = []
    for rec in records:
        doc_id = (rec.get("doc_id") or "").strip()
        if not doc_id:
            continue
        pairs.append((doc_id, {
            "doc_id":   doc_id,
            "select":   rec.get("select", ""),
            "campaign": rec.get("campaign", ""),
            "status":   rec.get("status", ""),
            "email":    rec.get("email", ""),
            "website":  rec.get("website", ""),
        }))
    count = 0
    for i in range(0, len(pairs), BATCH_SIZE):
        batch = db.batch()
        for doc_id, data in pairs[i:i+BATCH_SIZE]:
            batch.set(col.document(doc_id), data, merge=True)
        batch.commit()
        count += len(pairs[i:i+BATCH_SIZE])
    print(f"[crm-sync] Step 1 done: {count} docs -> crm/{CRM_CONTACT_DOC}/items")
    return count


def _update_email_contacts_campaign(db, records: list[dict]) -> dict:
    col = db.collection("email_contacts")
    BATCH_SIZE = 400
    candidates = [
        r for r in records
        if (r.get("doc_id") or "").strip() and (r.get("campaign") or "").strip()
    ]
    print(f"[crm-sync] {len(candidates)} rows with doc_id + campaign", flush=True)
    updated = 0
    for i in range(0, len(candidates), BATCH_SIZE):
        batch = db.batch()
        for r in candidates[i:i+BATCH_SIZE]:
            batch.update(col.document(r["doc_id"]), {"campaign": r["campaign"]})
        batch.commit()
        updated += len(candidates[i:i+BATCH_SIZE])
    print(f"[crm-sync] Step 2 done: updated={updated}")
    return {"updated": updated}


def _build_campaign_stats(records: list[dict]) -> dict[str, dict]:
    by_campaign: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        campaign = (r.get("campaign") or "").strip()
        if campaign:
            by_campaign[campaign].append(r)
    stats = {}
    for campaign_id, rows in by_campaign.items():
        domains   = {r.get("domain", "") or r.get("website", "") for r in rows}
        countries = Counter(r.get("country", "").upper() for r in rows if r.get("country"))
        statuses  = Counter(_contact_status(r.get("status")) for r in rows)
        selects   = Counter("marked" if r.get("select", "").strip() else "blank" for r in rows)
        tiers     = Counter(r.get("tier", "") or "unknown" for r in rows)
        outreach  = Counter(r.get("outreach", "") or "unknown" for r in rows)
        stats[campaign_id] = {
            "campaign_id":        campaign_id,
            "updated_at":         datetime.now(timezone.utc).isoformat(),
            "contact_count":      len(rows),
            "sites_count":        len(domains - {""}),
            "countries":          [c for c, _ in countries.most_common() if c],
            "status_breakdown":   dict(statuses),
            "select_breakdown":   dict(selects),
            "tier_breakdown":     dict(tiers),
            "outreach_breakdown": dict(outreach),
            "_defaults": {
                "status":                 "draft",
                "sent_at":                None,
                "outreach_email_account": "",
                "mail":                   {"subject": "", "body": ""},
            }
        }
    return stats


def _upsert_campaigns(db, campaign_stats: dict[str, dict]) -> tuple[int, list[str]]:
    col = db.collection(CAMPAIGNS_COLLECTION)
    new_campaigns = []
    count = 0
    for campaign_id, data in campaign_stats.items():
        defaults = data.pop("_defaults", {})
        existing = col.document(campaign_id).get()
        existing_data = existing.to_dict() or {} if existing.exists else {}
        is_new = not existing.exists
        for field, default_val in defaults.items():
            if field not in existing_data:
                data[field] = default_val
        if is_new:
            data["source"] = "master-sheet"
        col.document(campaign_id).set(data, merge=True)
        count += 1
        if is_new:
            new_campaigns.append(campaign_id)
        print(f"[crm-sync]   {campaign_id}: {data['contact_count']} contacts, "
              f"{'NEW' if is_new else 'updated'}", flush=True)
    print(f"[crm-sync] Step 3 done: {count} campaign docs ({len(new_campaigns)} new)")
    return count, new_campaigns


def _upsert_campaign_contacts(db, campaign_id: str, records: list[dict]) -> dict:
    col = db.collection("campaigns").document(campaign_id).collection("campaign_contacts")
    BATCH_SIZE = 400
    existing_docs = {d.id: d.to_dict() for d in col.stream()}

    # Lead-ids already represented in this campaign — never add new contacts
    # to a site that already has contacts (user may have deleted some on purpose).
    existing_lead_ids = {
        (v.get("lead_id") or "").strip()
        for v in existing_docs.values()
        if (v.get("lead_id") or "").strip()
    }

    to_write = []
    skipped  = 0

    for r in records:
        doc_id = (r.get("doc_id") or "").strip()
        if not doc_id:
            continue

        # Skip contacts that already exist in the campaign
        if doc_id in existing_docs:
            skipped += 1
            continue

        # Derive lead_id for this contact
        lead_id = (r.get("lead_id_site") or r.get("lead_id_leads") or "").strip()
        if not lead_id:
            from crm.campaign_import_lib import lead_id_from_website
            website = (r.get("website") or r.get("domain") or "").strip()
            lead_id = lead_id_from_website(website) if website else doc_id

        # Skip if the site already has contacts in this campaign
        if lead_id in existing_lead_ids:
            skipped += 1
            continue

        entry = {
            "doc_id":     doc_id,
            "email":      r.get("email", ""),
            "lead_id":    lead_id,
            "website":    r.get("website", ""),
            "name":       r.get("name", ""),
            "title":      r.get("title", ""),
            "status":     "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        to_write.append((doc_id, entry))

    # Cross-campaign dedup: skip contacts already active in another campaign
    new_ids  = {did for did, _ in to_write}
    from crm.campaign_import_lib import _contacts_in_other_campaigns
    reserved = _contacts_in_other_campaigns(db, campaign_id, new_ids)
    if reserved:
        print(f"[crm-sync] {len(reserved)} contacts skipped — active in another campaign",
              flush=True)
    to_write = [(did, d) for did, d in to_write if did not in reserved]

    added = len(to_write)
    for i in range(0, len(to_write), BATCH_SIZE):
        batch = db.batch()
        for doc_id, data in to_write[i:i+BATCH_SIZE]:
            batch.set(col.document(doc_id), data, merge=True)
        batch.commit()

    print(f"[crm-sync] campaign_contacts: added={added} skipped={skipped}",
          flush=True)
    return {"added": added, "updated": 0, "skipped": skipped}



def run_crm_sync(db, svc, campaign_id: str = "", tab: str = CONTACT_TAB) -> dict:
    """Full CRM sync from master contact sheet.

    If campaign_id is given, only that campaign is synced.
    Otherwise all campaigns found in the sheet are synced.
    """
    print(f"[crm-sync] Starting {'all campaigns' if not campaign_id else campaign_id}", flush=True)

    records = _read_sheet_contacts(svc, tab)
    if not records:
        return {"contact_select_synced": 0, "campaigns_upserted": 0, "campaign_ids": []}

    filtered = records
    if campaign_id:
        filtered = [r for r in records if (r.get("campaign") or "").strip() == campaign_id]
        print(f"[crm-sync] {len(filtered)}/{len(records)} rows for campaign '{campaign_id}'", flush=True)
        if not filtered:
            return {"contact_select_synced": 0, "campaigns_upserted": 0,
                    "campaign_ids": [], "message": f"No rows found for campaign '{campaign_id}'"}

    synced          = _sync_contact_select(db, filtered)
    email_result    = _update_email_contacts_campaign(db, filtered)
    campaign_stats  = _build_campaign_stats(filtered)
    count, new_ids  = _upsert_campaigns(db, campaign_stats)

    from crm.campaign_leads_lib import populate_campaign_leads
    contacts_result = {}
    leads_result    = {}
    for cid in campaign_stats:
        rows = [r for r in filtered if (r.get("campaign") or "").strip() == cid]
        contacts_result[cid] = _upsert_campaign_contacts(db, cid, rows)
        leads_result[cid]    = populate_campaign_leads(db, cid)

    return {
        "contact_select_synced": synced,
        "email_updated":        email_result,
        "campaigns_upserted":   count,
        "new_campaign_ids":     new_ids,
        "campaign_ids":         list(campaign_stats.keys()),
        "contacts_by_campaign": contacts_result,
        "leads_by_campaign":    leads_result,
    }
