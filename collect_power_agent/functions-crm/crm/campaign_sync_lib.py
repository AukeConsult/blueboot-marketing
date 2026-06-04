"""
campaign_sync_lib.py -- Sync campaign data from contact sheet to Firestore.

Three steps:
  1. Read contact sheet -> upsert crm/contact_select/items (sheet wins)
  2. Update email_contacts.campaign (blank-only unless force=True)
  3. Create/update campaigns/{campaign_id} with statistics
"""
from __future__ import annotations
from datetime import datetime, timezone
from collections import Counter, defaultdict
from crm.sheets_config import CONTACT_SHEET_ID, CONTACT_TAB, CRM_COLLECTION, CRM_CONTACT_DOC

CAMPAIGNS_COLLECTION = "campaigns"


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
    print(f"[campaign-sync] Read {len(records)} rows from sheet", flush=True)
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
        print(f"[campaign-sync]   contact_select upserted {count}/{len(pairs)}", flush=True)
    print(f"[campaign-sync] Step 1 done: {count} docs -> crm/{CRM_CONTACT_DOC}/items")
    return count


def _update_email_contacts_campaign(db, records: list[dict], force: bool = False) -> dict:
    from google.cloud.firestore_v1.base_query import FieldFilter
    col = db.collection("email_contacts")
    BATCH_SIZE = 400
    candidates = [
        r for r in records
        if (r.get("doc_id") or "").strip() and (r.get("campaign") or "").strip()
    ]
    print(f"[campaign-sync] {len(candidates)} rows with doc_id + campaign", flush=True)
    updated = skipped = 0
    to_update = candidates
    print(f"[campaign-sync]   updating all {len(to_update)} docs (sheet always wins)", flush=True)
    for i in range(0, len(to_update), BATCH_SIZE):
        batch = db.batch()
        for r in to_update[i:i+BATCH_SIZE]:
            batch.update(col.document(r["doc_id"]), {"campaign": r["campaign"]})
        batch.commit()
        updated += len(to_update[i:i+BATCH_SIZE])
        print(f"[campaign-sync]   email_contacts updated {updated}/{len(to_update)}", flush=True)
    print(f"[campaign-sync] Step 2 done: updated={updated}")
    return {"updated": updated, "skipped": 0}


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
        statuses  = Counter(r.get("status", "pending") or "pending" for r in rows)
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
            # These fields are set manually / preserved via merge=True
            # "status":                 "draft"  (draft | dosend | sent | cancelled)
            # "sent_at":                None
            # "outreach_email_account": ""
            # "mail":                   {"subject": "", "body": ""}
        }
        # Set defaults only on first creation (merge=True preserves existing values)
        stats[campaign_id]["_defaults"] = {
            "status":                 "draft",
            "sent_at":                None,
            "outreach_email_account": "",
            "mail":                   {"subject": "", "body": ""},
        }
    return stats


def _upsert_campaigns(db, campaign_stats: dict[str, dict]) -> int:
    col = db.collection(CAMPAIGNS_COLLECTION)
    count = 0
    for campaign_id, data in campaign_stats.items():
        defaults = data.pop("_defaults", {})
        # Check which default fields are missing in Firestore
        existing = col.document(campaign_id).get()
        existing_data = existing.to_dict() or {} if existing.exists else {}
        for field, default_val in defaults.items():
            if field not in existing_data:
                data[field] = default_val
        col.document(campaign_id).set(data, merge=True)
        count += 1
        print(f"[campaign-sync]   {campaign_id}: {data['contact_count']} contacts, {data['sites_count']} sites, status={existing_data.get('status', data.get('status', 'draft'))}", flush=True)
    print(f"[campaign-sync] Step 3 done: {count} campaign docs upserted")
    return count



def _upsert_campaign_contacts(db, campaign_id: str, records: list[dict]) -> dict:
    """Sync campaign_contacts subcollection.
    - New contacts -> add with status=pending
    - Existing pending -> update fields
    - Existing sent/error -> skip (never touch)
    - Removed from sheet + pending -> delete
    - Removed from sheet + sent/error -> leave
    """
    col = db.collection("campaigns").document(campaign_id).collection("campaign_contacts")
    BATCH_SIZE = 400
    existing_docs = {d.id: d.to_dict() for d in col.stream()}
    print(f"[campaign-sync]   {len(existing_docs)} existing, {len(records)} in sheet", flush=True)

    sheet_ids = set()
    to_write  = []
    skipped   = 0

    for r in records:
        doc_id = (r.get("doc_id") or "").strip()
        if not doc_id:
            continue
        sheet_ids.add(doc_id)
        existing_status = existing_docs.get(doc_id, {}).get("status", "pending")
        if doc_id in existing_docs and existing_status != "pending":
            skipped += 1
            continue
        is_new = doc_id not in existing_docs
        entry = {
            "doc_id":  doc_id,
            "email":   r.get("email", ""),
            "lead_id": r.get("lead_id_site") or r.get("lead_id_leads") or doc_id,
            "website": r.get("website", ""),
            "name":    r.get("name", ""),
            "title":   r.get("title", ""),
            "status":  "pending",
            "sent_at": existing_docs.get(doc_id, {}).get("sent_at", None),
        }
        if is_new:
            entry["created_at"] = datetime.now(timezone.utc).isoformat()
        else:
            # Preserve existing created_at
            entry["created_at"] = existing_docs[doc_id].get("created_at",
                                   datetime.now(timezone.utc).isoformat())
        to_write.append((doc_id, entry))

    to_delete = [
        did for did, doc in existing_docs.items()
        if did not in sheet_ids and doc.get("status", "pending") == "pending"
    ]

    added   = sum(1 for did, _ in to_write if did not in existing_docs)
    updated = len(to_write) - added
    deleted = 0

    for i in range(0, len(to_write), BATCH_SIZE):
        batch = db.batch()
        for doc_id, data in to_write[i:i+BATCH_SIZE]:
            batch.set(col.document(doc_id), data, merge=True)
        batch.commit()

    for i in range(0, len(to_delete), BATCH_SIZE):
        batch = db.batch()
        for doc_id in to_delete[i:i+BATCH_SIZE]:
            batch.delete(col.document(doc_id))
        batch.commit()
        deleted += len(to_delete[i:i+BATCH_SIZE])

    print(f"[campaign-sync] campaign_contacts: added={added} updated={updated} skipped={skipped}(non-pending) deleted={deleted}")
    return {"added": added, "updated": updated, "skipped": skipped, "deleted": deleted}


def run_campaign_sync(db, svc, campaign_id: str, tab: str = CONTACT_TAB,
                      force: bool = False, dry_run: bool = False) -> dict:
    """
    Full 3-step campaign sync for a specific campaign_id.
    force=False:   only update email_contacts.campaign if currently blank
    force=True:    always overwrite
    dry_run=True:  show what would be written without touching Firestore
    """
    print(f"[campaign-sync] Campaign: {campaign_id}", flush=True)

    # Check campaign status — only sync if status is "draft" or "dosend" (pending states)
    campaign_doc = db.collection("campaigns").document(campaign_id).get()
    if campaign_doc.exists:
        campaign_status = (campaign_doc.to_dict() or {}).get("status", "draft")
        if campaign_status not in ("draft", "dosend"):
            print(f"[campaign-sync] BLOCKED: campaign status is '{campaign_status}' — sync only allowed for draft/dosend")
            return {
                "contact_select_synced": 0,
                "email_updated":         0,
                "email_skipped":         0,
                "campaigns_upserted":    0,
                "campaign_ids":          [],
                "blocked":               True,
                "reason":                f"Campaign status is '{campaign_status}'. Only draft/dosend campaigns can be synced.",
            }
        print(f"[campaign-sync] Campaign status: {campaign_status} — sync allowed", flush=True)

    print("[campaign-sync] Step 1: reading sheet + syncing contact_select...", flush=True)
    records = _read_sheet_contacts(svc, tab)
    if not records:
        print("[campaign-sync] Sheet is empty.")
        return {"contact_select_synced": 0, "email_updated": 0, "email_skipped": 0, "campaigns_upserted": 0, "campaign_ids": []}

    # Filter to only rows matching the requested campaign_id
    filtered = [r for r in records if (r.get("campaign") or "").strip() == campaign_id]
    print(f"[campaign-sync] {len(filtered)}/{len(records)} rows match campaign '{campaign_id}'", flush=True)
    if not filtered:
        print(f"[campaign-sync] No rows found for campaign '{campaign_id}'. Check the Campaign column in the sheet.")
        return {"contact_select_synced": 0, "email_updated": 0, "email_skipped": 0, "campaigns_upserted": 0, "campaign_ids": []}

    if dry_run:
        print(f"[campaign-sync] DRY RUN -- would sync {len(filtered)} contacts for '{campaign_id}'")
        for r in filtered[:5]:
            print(f"  {r.get('doc_id','?')} | {r.get('email','?')} | campaign={r.get('campaign','')}")
        if len(filtered) > 5:
            print(f"  ... and {len(filtered)-5} more")
        return {"contact_select_synced": 0, "email_updated": 0,
                "email_skipped": len(filtered), "campaigns_upserted": 0,
                "campaign_ids": [campaign_id], "dry_run": True}
    synced = _sync_contact_select(db, filtered)
    print("[campaign-sync] Step 2: updating email_contacts.campaign...", flush=True)
    email_result = _update_email_contacts_campaign(db, filtered, force=force)
    print("[campaign-sync] Step 3: building campaign statistics...", flush=True)
    campaign_stats = _build_campaign_stats(filtered)
    campaigns = _upsert_campaigns(db, campaign_stats)
    result = {
        "contact_select_synced": synced,
        "email_updated":         email_result["updated"],
        "email_skipped":         email_result["skipped"],
        "campaigns_upserted":    campaigns,
        "campaign_ids":          list(campaign_stats.keys()),
    }
    print("[campaign-sync] Step 4: storing contacts subcollection...", flush=True)
    contacts_result = {}
    for campaign_id in campaign_stats.keys():
        contacts_result[campaign_id] = _upsert_campaign_contacts(db, campaign_id, filtered)

    result["contacts_sync"] = contacts_result
    print(f"[campaign-sync] All done: {result}", flush=True)
    return result
