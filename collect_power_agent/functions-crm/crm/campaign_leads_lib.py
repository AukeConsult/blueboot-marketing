"""campaign_leads_lib.py -- Populate campaigns/{id}/campaign_leads from existing campaign_contacts.

Algorithm
---------
1. Stream campaign_contacts for the given campaign_id.
2. Collect unique lead_ids (from campaign_contacts.lead_id field).
3. For each lead_id, fetch from site_leads AND leads (both may exist).
4. Merge into a single unified doc.
5. Batch-write to campaigns/{campaign_id}/campaign_leads/{lead_id}.

Merge priority
--------------
Identity / company  : site_leads wins (richer: platform, page_count, query_category)
Quality signals     : leads adds reseller_score, priority, suggested_angle, categories
sources []          : computed list e.g. ["site_leads", "leads"]

Existing docs are updated with merge=True so any future outreach state on the lead
doc is never overwritten on re-runs.
"""
from __future__ import annotations

from datetime import datetime, timezone

CAMPAIGNS_COLLECTION      = "campaigns"
CAMPAIGN_CONTACTS_SUB     = "campaign_contacts"
CAMPAIGN_LEADS_SUB        = "campaign_leads"
SITE_LEADS_COLLECTION     = "site_leads"
LEADS_COLLECTION          = "leads"

BATCH_SIZE = 400


# ── Field specs ───────────────────────────────────────────────────────────────

# Fields taken from site_leads doc (these win over leads when both exist)
_SITE_LEAD_FIELDS = [
    "lead_id", "domain", "website", "country", "country_name",
    "company", "title", "description",
    "page_count", "sitemap_url", "sitemap_type",
    "platform", "query_category",
    "keywords", "target_types",
    "crawled_at",
    # location
    "location", "location_country",
    # AI enrichment (written by site_enrich_agent)
    "ai_sector", "ai_company_type", "ai_country", "ai_platform",
]

# Fields taken from leads doc (only if not already set by site_leads)
_LEADS_FIELDS_FILL = [
    "lead_id", "domain", "website", "country", "country_name",
    "company", "title", "description",
    "crawled_at",
]

# Fields taken from leads doc that site_leads never has (always additive)
_LEADS_FIELDS_ADDITIVE = [
    "reseller_score", "priority", "suggested_angle",
    "categories", "detected_tech", "linkedin", "contact_page",
    # AI enrichment (written by lead enrichment pipeline)
    "ai_reseller_potential", "ai_client_base", "ai_specialisation",
]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _merge_lead_docs(site_doc: dict | None, leads_doc: dict | None) -> dict:
    """Merge site_leads + leads into a single unified lead dict."""
    merged: dict = {}

    # Site_leads fields win
    if site_doc:
        for f in _SITE_LEAD_FIELDS:
            v = site_doc.get(f)
            if v is not None and v != "":
                merged[f] = v

    # Leads fills gaps for shared fields
    if leads_doc:
        for f in _LEADS_FIELDS_FILL:
            if not merged.get(f):
                v = leads_doc.get(f)
                if v is not None and v != "":
                    merged[f] = v

        # Leads-only quality signals always added
        for f in _LEADS_FIELDS_ADDITIVE:
            v = leads_doc.get(f)
            if v is not None and v != "":
                merged[f] = v

    # Build summary from description + suggested_angle
    description = (
        (site_doc  or {}).get("description") or
        (leads_doc or {}).get("description") or ""
    ).strip()
    angle = ((leads_doc or {}).get("suggested_angle") or "").strip()
    merged["summary"] = ((description + "\n\n" + angle).strip() if angle else description)

    # Track which sources contributed
    sources = []
    if site_doc:
        sources.append("site_leads")
    if leads_doc:
        sources.append("leads")
    merged["sources"] = sources

    return merged




# ── Public API ────────────────────────────────────────────────────────────────

def populate_campaign_leads(
    db,
    campaign_id: str,
    *,
    dry_run: bool = False,
) -> dict:
    """Build campaigns/{campaign_id}/campaign_leads from existing campaign_contacts.

    Safe to re-run: uses merge=True so outreach state on lead docs is preserved.

    Returns a summary dict.
    """
    print(f"[campaign-leads] campaign='{campaign_id}'  dry_run={dry_run}", flush=True)

    # ── 1a. Collect lead_ids directly from campaign_leads ────────────────────
    # This ensures leads imported without an email (website-only rows) are
    # also enriched from site_leads / leads collections.
    leads_col = (
        db.collection(CAMPAIGNS_COLLECTION)
          .document(campaign_id)
          .collection(CAMPAIGN_LEADS_SUB)
    )
    lead_ids: set[str] = {doc.id for doc in leads_col.select([]).stream()}
    print(f"[campaign-leads] {len(lead_ids)} lead_ids from campaign_leads", flush=True)

    # ── 1b. Count contacts per lead from campaign_contacts ───────────────────
    contacts_col = (
        db.collection(CAMPAIGNS_COLLECTION)
          .document(campaign_id)
          .collection(CAMPAIGN_CONTACTS_SUB)
    )
    contacts_per_lead:  dict[str, int] = {}
    pending_per_lead:   dict[str, int] = {}
    excluded_per_lead:  dict[str, int] = {}
    contact_count = 0
    for doc in contacts_col.select(["lead_id", "status"]).stream():
        contact_count += 1
        d   = doc.to_dict() or {}
        lid = d.get("lead_id", "").strip()
        st  = (d.get("status") or "pending").strip().lower()
        if lid:
            lead_ids.add(lid)   # also pick up any leads only known via contacts
            contacts_per_lead[lid]  = contacts_per_lead.get(lid, 0) + 1
            if st == "pending":
                pending_per_lead[lid]  = pending_per_lead.get(lid, 0) + 1
            elif st == "excluded":
                excluded_per_lead[lid] = excluded_per_lead.get(lid, 0) + 1

    print(f"[campaign-leads] {contact_count} contacts → {len(lead_ids)} total unique lead_ids",
          flush=True)

    if not lead_ids:
        return {
            "campaign_id":    campaign_id,
            "contacts_read":  contact_count,
            "leads_found":    0,
            "leads_written":  0,
            "leads_skipped":  0,
            "dry_run":        dry_run,
        }

    # ── 2. Load existing campaign_leads to preserve status on reruns ─────────
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    leads_col = db.collection(CAMPAIGNS_COLLECTION).document(campaign_id).collection(CAMPAIGN_LEADS_SUB)

    existing_statuses: dict[str, str] = {}
    for doc in leads_col.select(["status"]).stream():
        s = (doc.to_dict() or {}).get("status", "")
        if s:
            existing_statuses[doc.id] = s

    print(f"[campaign-leads] {len(existing_statuses)} existing lead docs found", flush=True)

    # ── 3. Fetch + merge each lead ───────────────────────────────────────────
    to_write: list[tuple[str, dict]] = []
    skipped = 0

    for lead_id in sorted(lead_ids):
        site_snap  = db.collection(SITE_LEADS_COLLECTION).document(lead_id).get()
        leads_snap = db.collection(LEADS_COLLECTION).document(lead_id).get()

        site_doc  = site_snap.to_dict()  if site_snap.exists  else None
        leads_doc = leads_snap.to_dict() if leads_snap.exists else None

        if not site_doc and not leads_doc:
            print(f"[campaign-leads]   SKIP {lead_id} — not found in site_leads or leads",
                  flush=True)
            skipped += 1
            continue

        unified = _merge_lead_docs(site_doc, leads_doc)

        # Preserve status for existing leads; new leads start as "pending"
        unified["status"]        = existing_statuses.get(lead_id, "pending")
        unified["contact_count"]  = contacts_per_lead.get(lead_id, 0)
        unified["pending_count"]   = pending_per_lead.get(lead_id, 0)
        unified["excluded_count"]  = excluded_per_lead.get(lead_id, 0)
        unified["campaign_id"]   = campaign_id
        unified["synced_at"]     = now

        to_write.append((lead_id, unified))

    print(f"[campaign-leads] {len(to_write)} leads to write, {skipped} skipped", flush=True)

    if dry_run:
        for lead_id, doc in to_write:
            print(f"[campaign-leads]   DRY RUN {lead_id}: "
                  f"sources={doc['sources']}", flush=True)
        return {
            "campaign_id":   campaign_id,
            "contacts_read": contact_count,
            "leads_found":   len(to_write) + skipped,
            "leads_written": 0,
            "leads_skipped": skipped,
            "dry_run":       True,
        }

    # ── 3. Batch-write to campaign_leads ─────────────────────────────────────
    written = 0
    for i in range(0, len(to_write), BATCH_SIZE):
        chunk = to_write[i:i + BATCH_SIZE]
        batch = db.batch()
        for lead_id, doc in chunk:
            batch.set(leads_col.document(lead_id), doc, merge=True)
        batch.commit()
        written += len(chunk)
        print(f"[campaign-leads]   written {written}/{len(to_write)}", flush=True)

    print(f"[campaign-leads] done. {written} leads written to "
          f"campaigns/{campaign_id}/{CAMPAIGN_LEADS_SUB}", flush=True)

    # ── 4. Enrich campaign_contacts from email_contacts ───────────────────────
    ec_result = enrich_contacts_from_email_contacts(db, campaign_id, dry_run=dry_run)

    return {
        "campaign_id":        campaign_id,
        "contacts_read":      contact_count,
        "leads_found":        len(to_write) + skipped,
        "leads_written":      written,
        "leads_skipped":      skipped,
        "contacts_enriched":  ec_result.get("enriched", 0),
        "dry_run":            False,
    }


# Fields copied from email_contacts → campaign_contacts (gaps only, never overwrite)
_EC_FILL_FIELDS = [
    "name", "title", "occupation", "phone", "linkedin",
    "email_type", "contact_type", "outreach_priority",
]
# Fields that must never be overwritten on existing campaign_contacts docs
_CONTACT_PROTECTED = {
    "status", "mail_sent", "next_mail_index", "in_reply_to",
    "followup_status", "followup_date", "followup_comment",
    "followup_importance", "followup_owner", "comment_history",
    "sent_at", "message_id", "sender_account", "created_at",
}


def _ec_doc_id(email: str) -> str:
    import re as _re
    return _re.sub(r"[^a-zA-Z0-9_-]", "_", email.strip().lower())


def enrich_contacts_from_email_contacts(db, campaign_id: str, dry_run: bool = False) -> dict:
    """Copy missing fields from email_contacts into campaign_contacts.

    For each contact in campaigns/{campaign_id}/campaign_contacts:
      - Looks up email_contacts/{doc_id} by email
      - Copies any _EC_FILL_FIELDS that are empty on the campaign contact
      - Never touches protected fields (status, mail_sent, etc.)

    Uses db.get_all() in batches of 30 for efficiency.
    Safe to re-run — only fills gaps, never overwrites existing values.
    """
    print(f"[ec-enrich] campaign='{campaign_id}'  dry_run={dry_run}", flush=True)

    contacts_col = (
        db.collection(CAMPAIGNS_COLLECTION)
          .document(campaign_id)
          .collection(CAMPAIGN_CONTACTS_SUB)
    )
    ec_col = db.collection("email_contacts")

    # Load all campaign_contacts
    contacts = []
    for doc in contacts_col.stream():
        d = doc.to_dict() or {}
        email = (d.get("email") or "").strip().lower()
        if not email:
            continue
        contacts.append({"ref": doc.reference, "data": d, "email": email})

    if not contacts:
        print(f"[ec-enrich] no contacts found", flush=True)
        return {"enriched": 0, "skipped": 0}

    print(f"[ec-enrich] {len(contacts)} campaign_contacts to check", flush=True)

    # Batch-read email_contacts (30 per call)
    BATCH_GET = 30
    ec_refs   = [ec_col.document(_ec_doc_id(c["email"])) for c in contacts]
    ec_by_id: dict[str, dict] = {}
    for i in range(0, len(ec_refs), BATCH_GET):
        for snap in db.get_all(ec_refs[i:i + BATCH_GET]):
            if snap.exists:
                ec_by_id[snap.id] = snap.to_dict() or {}

    print(f"[ec-enrich] {len(ec_by_id)} email_contacts found", flush=True)

    enriched = skipped = 0
    batch = db.batch()
    batch_count = 0

    for c in contacts:
        ec_id   = _ec_doc_id(c["email"])
        ec_data = ec_by_id.get(ec_id)
        if not ec_data:
            skipped += 1
            continue

        update = {}
        for f in _EC_FILL_FIELDS:
            if f in _CONTACT_PROTECTED:
                continue
            existing = c["data"].get(f)
            # Only fill if missing or empty
            if existing is not None and str(existing).strip():
                continue
            ec_val = ec_data.get(f)
            if ec_val is not None and str(ec_val).strip():
                update[f] = ec_val

        if not update:
            skipped += 1
            continue

        if dry_run:
            print(f"  [DRY] {c['email']}: {list(update.keys())}", flush=True)
        else:
            batch.update(c["ref"], update)
            batch_count += 1
            if batch_count >= 400:
                batch.commit()
                batch = db.batch()
                batch_count = 0
        enriched += 1

    if not dry_run and batch_count:
        batch.commit()

    print(f"[ec-enrich] done — {enriched} contacts enriched, {skipped} skipped", flush=True)
    return {"enriched": enriched, "skipped": skipped}
