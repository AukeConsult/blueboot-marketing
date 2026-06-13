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

    # ── 1. Collect unique lead_ids from campaign_contacts ────────────────────
    contacts_col = (
        db.collection(CAMPAIGNS_COLLECTION)
          .document(campaign_id)
          .collection(CAMPAIGN_CONTACTS_SUB)
    )
    lead_ids: set[str] = set()
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
            lead_ids.add(lid)
            contacts_per_lead[lid]  = contacts_per_lead.get(lid, 0) + 1
            if st == "pending":
                pending_per_lead[lid]  = pending_per_lead.get(lid, 0) + 1
            elif st == "excluded":
                excluded_per_lead[lid] = excluded_per_lead.get(lid, 0) + 1

    print(f"[campaign-leads] {contact_count} contacts → {len(lead_ids)} unique lead_ids",
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

    return {
        "campaign_id":   campaign_id,
        "contacts_read": contact_count,
        "leads_found":   len(to_write) + skipped,
        "leads_written": written,
        "leads_skipped": skipped,
        "dry_run":       False,
    }
