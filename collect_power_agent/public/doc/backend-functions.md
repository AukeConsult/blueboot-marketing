# Backend Functions

All backend scripts live in the `app/` directory and run from the project root.
Scripts that are also triggered from the CRM web frontend are marked **🌐 Frontend triggered**.

---

## Site Pipeline

### `site_agent.py` — Site discovery

Discovers content-heavy websites via Bing search, measures site size via sitemap, and extracts contact information. The primary intake for the site leads pipeline.

```bash
python app/site_agent.py --countries NO
python app/site_agent.py --countries NO,SE,DK --max-results 50
python app/site_agent.py --countries NO --min-pages 100 --dry-run
```

**Key flags:** `--countries`, `--max-results`, `--min-pages`, `--dry-run`

**Writes to:** `site_leads/{domain}`, `site_leads/{domain}/site_contacts/`, `sites_excluded/{domain}`

---

### `site_enrich_agent.py` — AI enrichment of site leads

Classifies un-enriched `site_leads` documents using OpenAI: sector, company type, AI platform, AI country, confidence score.

```bash
python app/site_enrich_agent.py
python app/site_enrich_agent.py --countries NO,SE --batch-size 20
python app/site_enrich_agent.py --limit 50 --dry-run
```

**Writes to:** `site_leads` (updates `ai_sector`, `ai_company_type`, `ai_platform`, `ai_country`, `ai_classified_at`)

---

### `site_location_enrich.py` — Location enrichment

Resolves site country to a standardised city + country location string.

```bash
python app/site_location_enrich.py --countries UK
python app/site_location_enrich.py --countries UK IN --batch-size 50 --concurrent 4
python app/site_location_enrich.py --countries IN --dry-run 20
```

**Writes to:** `site_leads` (updates `location`, `location_country`, `location_enriched_at`)

---

### `site_contact_enrich.py` — Contact enrichment via Brave Search

Enriches `site_contacts` with LinkedIn profiles and additional context using Brave Search + GPT.

```bash
python app/site_contact_enrich.py
python app/site_contact_enrich.py --countries NO,SE
python app/site_contact_enrich.py --limit 100 --dry-run
```

**Writes to:** `site_leads/{domain}/site_contacts/` (updates `occupation`, `linkedin_url`, `brave_enriched_at`)

---

### `site_email_check.py` — Email & role classification

Classifies `site_contacts` by email type (personal/role/department/admin) and contact role (decision_maker/marketing/…). Assigns outreach priority 1–4.

```bash
python app/site_email_check.py --countries UK
python app/site_email_check.py --countries UK --dry-run 20
python app/site_email_check.py --countries IN --force
python app/site_email_check.py --countries UK --batch-size 50 --concurrent 4
```

**Writes to:** `site_leads/{domain}/site_contacts/` (updates `email_type`, `contact_type`, `outreach_priority`, `email_checked_at`)

---

### `site_smart_export.py` — Tiered export to Excel / email_contacts

Scores and tiers site_leads (Enterprise / Hot / Good / Warm / Cold) based on page count, platform and sector. Exports to Excel and optionally writes to `email_contacts`.

```bash
python app/site_smart_export.py --countries NO
python app/site_smart_export.py --countries UK IN --out exports/smart_uk_in.xlsx
python app/site_smart_export.py --countries NO --min-pages 50
python app/site_smart_export.py --countries NO --write-contacts --campaign NO_jun
```

**Key flags:** `--countries`, `--min-pages`, `--out`, `--write-contacts`, `--campaign`, `--dry-run`

**Writes to:** Excel file; optionally `email_contacts/` collection

---

## Lead Pipeline

### `lead_agent.py` — Agency/reseller discovery

Discovers web agencies and digital resellers via **two channels**: Bing search queries AND paginated scraping of agency catalog services (Sortlist, DesignRush, Proff, DAN, TopDevelopers, and country-specific directories). Catalog sources are configured per country in `config/catalogs.json`. The primary intake for the legacy lead pipeline.

```bash
python app/lead_agent.py --countries NO
python app/lead_agent.py --countries NO,SE --limit 200
```

**Writes to:** `leads/{lead_id}`, `leads/{lead_id}/contacts/`, `leads_excluded/`

---

### `lead_enrich_agent.py` — AI enrichment of leads

AI classification of leads: sector, company type, reseller potential score.

```bash
python app/lead_enrich_agent.py
python app/lead_enrich_agent.py --countries NO,SE
python app/lead_enrich_agent.py --countries NO --limit 200
```

**Writes to:** `leads` (updates `ai_sector`, `ai_company_type`, `reseller_score`, `ai_classified_at`)

---

### `lead_enrich_contacts.py` — Contact social enrichment

Enriches lead `contacts` subcollection with social profiles via Brave Search.

```bash
python app/lead_enrich_contacts.py [options]
```

**Writes to:** `leads/{id}/contacts/` (updates `social_enriched_at`, `linkedin_url`)

---

### `leads_email_check.py` — Email & role classification for leads

Same OpenAI-based classification as `site_email_check.py` but for the leads pipeline.

```bash
python app/leads_email_check.py --countries UK
python app/leads_email_check.py --countries UK --dry-run 20
python app/leads_email_check.py --countries IN --force
```

**Writes to:** `leads/{id}/contacts/` (updates `email_type`, `contact_type`, `outreach_priority`, `email_checked_at`)

---

### `leads_smart_export.py` — Tiered export to Excel / email_contacts

Scores leads by reseller potential and exports to Excel. Optionally writes to `email_contacts`.

```bash
python app/leads_smart_export.py --countries UK
python app/leads_smart_export.py --countries UK IN NO --out exports/leads_resellers.xlsx
python app/leads_smart_export.py --countries NO SE DK --min-score 50
python app/leads_smart_export.py --countries NO --write-contacts --campaign NO_agencies_jun
```

**Writes to:** Excel file; optionally `email_contacts/` collection

---

### `wp_plugin_leads.py` — WordPress plugin catalogue leads

Discovers leads from the WordPress.org plugin catalogue.

```bash
python app/wp_plugin_leads.py --countries UK IN --dry-run
```

---

## CRM & Contacts

### `email_contacts_export.py` — Export unified contacts to Excel

Exports from the `email_contacts` Firestore collection to Excel. Supports filtering by country, campaign, status, and pipeline membership.

```bash
python app/email_contacts_export.py --countries NO
python app/email_contacts_export.py --countries UK NO --status pending
python app/email_contacts_export.py --campaign NO_resellers_jun02
python app/email_contacts_export.py --mark site    # site_leads contacts only
python app/email_contacts_export.py --mark leads   # leads contacts only
python app/email_contacts_export.py --mark both    # contacts in both pipelines
```

---

### `filter_site_leads.py` — Filter site_leads by facets

Filters `site_leads` and their contacts using the stored filter facets, and exports results.

```bash
python app/filter_site_leads.py --filter ai_sector=technology,ecommerce
python app/filter_site_leads.py --filter country=NO --min-pages 500
```

---

## CRM Workflow (also triggered from frontend)

### `crm/contact_sync.py` — Import contacts to contact sheet 🌐 Frontend triggered

Exports selected `email_contacts` to the master CRM contact sheet.

**Frontend trigger:** CRM page → Step 1 "Run import"
→ API: `GET /api/crm/contact-sync`
→ Cloud Tasks job: `contact-sync`

---

### `crm/push_and_sync.py` — Push selected to CRM template 🌐 Frontend triggered

Takes contacts marked in the contact sheet and pushes them to the CRM template spreadsheet, grouped by site.

**Frontend trigger:** CRM page → Step 3 "Push to CRM"
→ API: `GET /api/crm/push-and-sync`
→ Cloud Tasks job: `push-and-sync`

---

### `crm/template_sync.py` — Sync CRM template back to Leads DB 🌐 Frontend triggered

Reads `crm_status`, `crm_sales_person`, and `crm_date` from the CRM template and writes them back to Firestore.

**Frontend trigger:** CRM page → Step 5 "Sync now"
→ API: `GET /api/crm/template-sync`
→ Cloud Tasks job: `template-sync`

---

### `crm/sync_campaign.py` — Sync campaign from master sheet 🌐 Frontend triggered

Reads the master CRM contact sheet and syncs contacts into the correct campaign in Firestore.

**Frontend trigger:** CRM page → Step 6 "Sync campaigns" / Discover new button
→ API: `GET /api/crm/crm-sync`
→ Cloud Tasks job: `crm-sync`

---

## Campaign management (frontend only) 🌐 Frontend triggered

These operations have no standalone CLI — they run as Cloud Tasks jobs triggered from the Campaigns or single Campaign page.

| Operation | Frontend | API endpoint | Job name |
|---|---|---|---|
| Campaign sync (Drive sheet → DB) | Campaign page → Sync | `GET /api/crm/campaign-sync` | `campaign-sync` |
| Full override (DB → Drive sheet) | Campaign page → Full override | `GET /api/crm/campaign-export` | `campaign-export` |
| Discover new campaigns | Campaigns list → Discover new | `GET /api/crm/discover-campaigns` | — (sync jobs spawned) |
| Collect statistics | Statistics page → Collect statistics | `POST /api/crm/statistics/collect` | `statistics` |

---

## Maintenance & Data Quality

### `maint_statistics.py` — Aggregate all pipeline statistics 🌐 Frontend triggered

Runs all statistics aggregations across both pipelines and writes results to Firestore `statistics/` collection. Also exports a dated Excel file to `output/`.

```bash
python app/maint_statistics.py
python app/maint_statistics.py --no-excel
python app/maint_statistics.py --only site-funnel
python app/maint_statistics.py --only leads-overview
python app/maint_statistics.py --only site-leads-overview
python app/maint_statistics.py --only quality
python app/maint_statistics.py --only email-funnel
python app/maint_statistics.py --only coverage
python app/maint_statistics.py --only campaigns
```

**Frontend trigger:** Statistics page → Collect statistics button
→ API: `POST /api/crm/statistics/collect`
→ Cloud Tasks job: `statistics`

---

### `build_filter_facets.py` — Build filter facet catalog 🌐 Frontend triggered

Scans `site_leads` + `site_contacts` and builds the filter facet catalog stored in `filter_facets/site_leads`.

```bash
python app/build_filter_facets.py
python app/build_filter_facets.py --cap 300
python app/build_filter_facets.py --no-write          # JSON preview only
```

**Frontend trigger:** Filter facets page → Rebuild button (if present)
→ API: `GET /api/crm/filter-count` (for counting selected filters)

---

### `maint_site_leads_export.py` — Raw export of site_leads to Excel

Exports `site_leads` + `site_contacts` to a flat Excel file without scoring.

```bash
python app/maint_site_leads_export.py
python app/maint_site_leads_export.py --countries NO,SE
python app/maint_site_leads_export.py --countries NO --output exports/no_leads.xlsx
```

---

### `maint_site_excluded_recheck.py` — Re-check excluded sites

Re-crawls sites in `sites_excluded` to see if they now meet minimum criteria.

```bash
python app/maint_site_excluded_recheck.py
python app/maint_site_excluded_recheck.py --countries NO,SE
python app/maint_site_excluded_recheck.py --reason min_pages
```

---

### `maint_site_sitemap_backfill.py` — Backfill sitemap data

Re-fetches sitemap data for `site_leads` that are missing `sitemap_url`, `sitemap_type`, or `page_count`.

```bash
python app/maint_site_sitemap_backfill.py
python app/maint_site_sitemap_backfill.py --countries NO,SE
python app/maint_site_sitemap_backfill.py --limit 200 --dry-run
```

---

### `maint_firestore_snapshot.py` — Search Firestore by keyword

Quick diagnostic to search any Firestore collection by keyword across all fields.

```bash
python app/maint_firestore_snapshot.py wordpress
python app/maint_firestore_snapshot.py wordpress --field source_query
python app/maint_firestore_snapshot.py wordpress --limit 20
```

---

### `maint_firestore_index_sync.py` — Merge Firestore indexes

Merges newly generated indexes into `firestore.indexes.json` without losing existing ones.

```bash
python app/maint_firestore_index_sync.py
python app/maint_firestore_index_sync.py --dry-run
python app/maint_firestore_index_sync.py --discover-only
```

---

### `maint_fix_contact_country.py` — Fix contact country fields

One-off migration to standardise country field values on contact documents.

```bash
python app/maint_fix_contact_country.py --dry-run
python app/maint_fix_contact_country.py
```

---

### `maint_fix_rescrape_contacts.py` — Re-scrape contacts with bad data

Re-crawls leads where phone/email data is mismatched or corrupted.

```bash
python app/maint_fix_rescrape_contacts.py --dry-run
python app/maint_fix_rescrape_contacts.py
python app/maint_fix_rescrape_contacts.py --country FI
```

---

## Deployment

```bash
# Deploy frontend (HTML/CSS/JS)
firebase deploy --only hosting

# Deploy backend API
firebase deploy --only functions:crm

# Deploy Firestore indexes + rules
firebase deploy --only firestore

# Deploy everything
firebase deploy
```
