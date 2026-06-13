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

Discovers web agencies and digital resellers via **two channels**: Bing search queries AND paginated scraping of agency catalog services (Sortlist, DesignRush, Proff, DAN, TopDevelopers, and country-specific directories). Catalog sources are configured per country in `config/catalogs.json`. The primary intake for the lead pipeline.

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


### `campaign_name_enrich.py` — Fill missing contact names 🌐 Frontend triggered

Enriches campaign contacts that are missing a name using a three-pass search pipeline:

1. **Rules** — extracts names from email patterns (`john.doe@` → "John Doe")
2. **Bing search** — searches for the exact email address; if not found, searches `"firstname" site:domain` to find the person on the company's own site
3. **Brave Search** — same two queries via the Brave API for pages Bing misses
4. **AI validation** — GPT-4o-mini validates every candidate and returns both `name` and `title` from the verified context. AI returns null if evidence is insufficient — a wrong name is never written.

Writes back to both `campaigns/{id}/campaign_contacts` and `email_contacts` to keep collections in sync.

```bash
python app/campaign_name_enrich.py --campaign MY_CAMPAIGN_ID
python app/campaign_name_enrich.py --campaign MY_CAMPAIGN_ID --dry-run
python app/campaign_name_enrich.py --campaign MY_CAMPAIGN_ID --skip-ai
python app/campaign_name_enrich.py --all                   # all campaigns
python app/campaign_name_enrich.py --emails a@b.com c@d.com
python app/campaign_name_enrich.py --campaign MY_CAMPAIGN_ID --debug
```

**Key flags:**

| Flag | Description |
|---|---|
| `--campaign ID` | Enrich all contacts without a name in this campaign |
| `--all` | Enrich across all campaigns in the `campaigns` collection |
| `--emails a@b.com …` | Enrich a flat list of addresses (no campaign context needed) |
| `--dry-run` | Preview without writing to Firestore |
| `--skip-ai` | Rule-based only — no Bing, Brave, or OpenAI calls |
| `--debug` | Print exactly what Bing/Brave sends to AI and what AI returns; always prepends `leif@auke.no` as a calibration contact (expected: "Leif Auke") |
| `--limit N` | Cap the number of contacts processed (useful with `--all`) |

**Requires:** `OPENAI_API_KEY` and `BRAVE_API_KEY` in `.env`

**Frontend trigger:** Campaign page → **Enrich names** button
→ API: `POST /api/crm/campaigns/<id>/name-enrich`
→ Cloud Tasks job: `name-enrich`

The API also accepts a generic call with an email list:
```
POST /api/crm/name-enrich
{ "campaign_id": "MY_CAMPAIGN" }          — enrich all contacts in campaign
{ "emails": ["a@b.com", "c@d.com"] }      — enrich a specific list
```
Returns immediately with `job_id` — poll `GET /api/crm/status/<job_id>`.

### `inbound_read.py` — Read inbound/sent mail into contact logs 🌐 Frontend triggered

Connects to each configured outreach account via IMAP, fetches message headers (inbox + sent) within a configurable lookback window, matches messages to campaign contacts by email address, and appends `EMAIL_IN` / `EMAIL_OUT` entries to each matched contact's `comment_history` in Firestore. The operation is idempotent — each entry carries a unique `email_id` so re-running never creates duplicates.

```bash
python app/inbound_read.py                        # all campaigns, last 7 days
python app/inbound_read.py --days 30              # 30-day lookback
python app/inbound_read.py --campaigns NO_jun     # one campaign only
python app/inbound_read.py --contact doc_id --campaigns NO_jun # one contact
python app/inbound_read.py --dry-run              # preview without writing
python app/inbound_read.py --list-campaigns       # list available campaign IDs
```

| Flag | Default | Description |
|---|---|---|
| `--campaigns` / `-c` | all campaigns | Only sync contacts in these campaign IDs; accepts space, comma, semicolon, or pipe separated values |
| `--contact` / `-d` | all contacts | Only sync this contact doc ID (requires exactly one `--campaigns` value) |
| `--days` / `-n` | `7` | Lookback window in days (`0` = all time) |
| `--dry-run` | off | Fetch and match, print results, skip Firestore writes |
| `--list-campaigns` | off | Print all campaign IDs and exit |

**Writes to:** `campaigns/{id}/campaign_contacts/{doc_id}` — appends to `comment_history` array via Firestore `ArrayUnion`

**Launcher scripts:** `run_inbound_read.bat` (Windows) / `run_inbound_read.sh` (macOS/Linux)

**Frontend trigger:** CRM Follow-up page → **Sync all messages** button or per-contact mail icon
→ API: `POST /api/crm/inbound-read`
→ Cloud Tasks job: `inbound-read`

---

## CRM Workflow (also triggered from frontend)

### `crm/contact_sync.py` — Export contacts to contact sheet 🌐 Frontend triggered

Exports selected `email_contacts` to the master CRM contact sheet.

**Frontend trigger:** CRM page → Step 1 "Run import"
→ API: `GET /api/crm/contact-sync`
→ Cloud Tasks job: `contact-sync`

---

### `crm/push_and_sync.py` — Push selected to CRM work sheet 🌐 Frontend triggered

Takes contacts marked in the contact sheet and pushes them to the CRM work sheet, grouped by site.

**Frontend trigger:** CRM page → Step 3 "Push to CRM"
→ API: `GET /api/crm/push-and-sync`
→ Cloud Tasks job: `push-and-sync`

---

### `crm/template_sync.py` — Sync CRM work sheet back to Leads DB 🌐 Frontend triggered

Reads `crm_status`, `crm_sales_person`, and `crm_date` from the CRM work sheet and writes them back to Firestore.

**Frontend trigger:** CRM page → Step 5 "Sync now"
→ API: `GET /api/crm/template-sync`
→ Cloud Tasks job: `template-sync`

---

### `crm/sync_campaign.py` — Sync campaign from master sheet 🌐 Frontend triggered

Reads the master CRM contact sheet and syncs contacts into the correct campaign in Firestore.

**Frontend trigger:** CRM page → Step 6 "Sync campaigns" / Discover campaigns button
→ API: `GET /api/crm/crm-sync`
→ Cloud Tasks job: `crm-sync`

---

## Campaign management (frontend only) 🌐 Frontend triggered

These operations have no standalone CLI — they run as Cloud Tasks jobs triggered from the campaign workspace.

| Operation | Frontend | API endpoint | Job name |
|---|---|---|---|
| Campaign sync (Drive sheet → DB) | Campaign page → Sync | `GET /api/crm/campaign-sync` | `campaign-sync` |
| Full override (DB → Drive sheet) | Campaign page → Full override | `GET /api/crm/campaign-export` | `campaign-export` |
| Discover campaigns | Campaign workspace → Discover campaigns | `GET /api/crm/discover-campaigns` | — (sync jobs spawned) |
| Collect statistics | Statistics page → Collect statistics | `POST /api/crm/statistics/collect` | `statistics` |
| Load all follow-up contacts | CRM Follow-up page load | `GET /api/crm/followup-contacts` | — (direct read) |
| Update follow-up field | CRM Follow-up inline edit | `PATCH /api/crm/campaigns/<id>/contacts/<doc>` | — (direct write) |
| Read inbound/sent mail | CRM Follow-up → Sync messages | `POST /api/crm/inbound-read` | `inbound-read` |
| Enrich contact names | Campaign page → Enrich names | `POST /api/crm/campaigns/<id>/name-enrich` | `name-enrich` |

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
python app/build_fi

