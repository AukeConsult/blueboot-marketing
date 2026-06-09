# Blueboot CRM — System Architecture

## System Overview

Blueboot CRM is a multi-stage outreach automation system. Two independent discovery pipelines identify and enrich potential leads, which are then funnelled into a unified contact store, reviewed, grouped into campaigns, and sent personalised outreach emails.

```
SITE PIPELINE                  LEAD PIPELINE
(end-user companies)           (web agencies / resellers)
        │                              │
  discover → enrich             discover → enrich
  site_agent                    lead_agent
  site_enrich_agent             lead_enrich_agent
  site_contact_enrich           lead_enrich_contacts
  site_location_enrich          leads_email_check
  site_email_check                     │
        │                              │
        └──────────┬───────────────────┘
                   ▼
          ┌─────────────────┐
          │  email_contacts │  ← unified Firestore collection
          └────────┬────────┘
                   │
          ┌────────▼────────┐
          │  CRM / Campaigns│  ← select, group, track
          └────────┬────────┘
                   │
          ┌────────▼────────┐
          │  Outreach sender│  ← personalised email, rate-limited
          └─────────────────┘
```

---

## 1. Site Pipeline

Discovers content-heavy commercial websites — the primary targets for BlueSearch (SEO / search visibility services).

### 1.1 Discovery — `site_agent.py`

**Input:** Bing search queries from `config/site_agent_queries.json` (configurable categories × countries).

**Process per site:**
1. Bing search → collect candidate URLs
2. Fetch `robots.txt` → parse sitemap references
3. Fetch sitemap(s) → count indexable pages
4. Extract contact pages and personal names/emails from the homepage and contact page
5. Write result to `site_leads` in Firestore

**Key rules:**
- Minimum page count threshold (configurable)
- Sites with no sitemap or too few pages go to `sites_excluded`
- Async producer/consumer with per-site hard timeout (`asyncio.wait_for`)
- Each site runs in an isolated `SiteWorker` class — one failure cannot stall others

**Firestore output — `site_leads/{domain}`:**

| Field | Description |
|---|---|
| `domain` | Normalised domain |
| `url` | Canonical homepage URL |
| `country` | Scraped country hint |
| `page_count` | Total indexable pages from sitemap |
| `sitemap_type` | `index`, `standard`, `none` |
| `platform` | Detected CMS (wordpress / shopify / woocommerce / …) |
| `crawled_at` | ISO timestamp |

**Firestore subcollection — `site_leads/{domain}/site_contacts/{id}`:**

| Field | Description |
|---|---|
| `name` | Extracted person name |
| `email` | Extracted email address |
| `title` | Job title if found |
| `source_url` | Page the contact was found on |
| `country` | Contact country |

**Excluded sites — `sites_excluded/{domain}`:**

Sites that failed minimum criteria (too few pages, no sitemap, blocked, duplicate) are written here with a `reason` field so they are not re-crawled.

---

### 1.2 AI Enrichment — `site_enrich_agent.py`

Reads un-enriched `site_leads` documents and sends batches to OpenAI to classify:

| Field written | Values |
|---|---|
| `ai_sector` | manufacturing / technology / ecommerce / media / public_sector / … |
| `ai_company_type` | B2B / B2C / government / NGO / … |
| `ai_platform` | wordpress / shopify / custom / … |
| `ai_country` | ISO country code inferred from content |
| `ai_confidence` | 0.0 – 1.0 |
| `ai_classified_at` | ISO timestamp |

---

### 1.3 Location Enrichment — `site_location_enrich.py`

Resolves `ai_country` to a standardised country name and city, writing:
- `location` — "City, Country" string (top 200 most-used used by filter facets)
- `location_country` — ISO country of HQ
- `location_enriched_at`

---

### 1.4 Contact Enrichment — `site_contact_enrich.py`

Reads `site_contacts` subcollection entries that have a name but no enriched data. Uses Brave Search to find LinkedIn profiles and additional context for each person.

Fields written to `site_contacts`:
- `occupation`, `linkedin_url`, `brave_enriched_at`

---

### 1.5 Email Classification — `site_email_check.py`

Classifies `site_contacts` by email type and contact role using OpenAI. Batch size: 50 contacts per API call.

| Field | Values |
|---|---|
| `email_type` | `personal` / `role` / `department` / `admin` |
| `contact_type` | `decision_maker` / `marketing` / `developer` / `sales` / `operations` / `unknown` |
| `outreach_priority` | 1 (best) – 4 (lowest) |
| `email_checked_at` | ISO timestamp |

**Priority logic:**
- **P1** — personal email + decision_maker or marketing role
- **P2** — personal email + other role, OR role email + decision_maker
- **P3** — role/department email + non-admin contact type
- **P4** — admin email or unknown contact type

---

### 1.6 Smart Export — `site_smart_export.py`

Scores and tiers site_leads for export to Excel or direct write to `email_contacts`.

**Tiers by page count:**

| Tier | Name | Pages |
|---|---|---|
| 1 | Enterprise | > 10,000 |
| 2 | Hot | 500 – 10,000 |
| 3 | Good | 100 – 500 |
| 4 | Warm | 50 – 100 |
| 5 | Cold | < 50 |

Bonus signals that bump a site up one tier: WordPress/WooCommerce platform, priority sector (ecommerce/technology/media), 3+ valid contacts found.

**Usage:**
```bash
python app/site_smart_export.py --countries NO --write-contacts --campaign NO_jun
```

---

## 2. Lead Pipeline

Discovers web agencies and digital resellers — companies that could re-sell BlueSearch to their clients.

> **Key difference from the site pipeline:** the lead pipeline uses two complementary discovery methods — Bing search AND curated agency catalog services (Sortlist, DesignRush, Proff, DAN, TopDevelopers, and country-specific business directories configured in `config/catalogs.json`). This gives broader and more targeted coverage than search alone.

### 2.1 Discovery — `lead_agent.py`

Searches for agencies by country using a configured query set, fetches their websites, and writes to the `leads` Firestore collection.

**Firestore output — `leads/{lead_id}`:**

| Field | Description |
|---|---|
| `lead_id` | Unique ID (hash of domain) |
| `domain` | Agency domain |
| `country`, `country_name` | Country |
| `priority` | A / B / C / unset |
| `emails` | Comma-separated emails found |
| `reasons` | Semicolon-separated scoring signals |

**Subcollection — `leads/{lead_id}/contacts/{id}`:**
Contact persons extracted from the agency website.

---

### 2.2 Enrichment — `lead_enrich_agent.py`, `lead_enrich_contacts.py`

AI classification of the agency (sector, company type), and social enrichment of individual contacts via Brave Search.

---

### 2.3 Email Classification — `leads_email_check.py`

Same OpenAI-based classification as site contacts — writes `email_type`, `contact_type`, `outreach_priority`, `email_checked_at` to `contacts` subcollection.

---

### 2.4 Smart Export — `leads_smart_export.py`

Exports leads + contacts to Excel and/or writes to `email_contacts`.

---

## 3. Unified Contact Store — `email_contacts`

Both pipelines converge here. Each document represents one contactable person.

**Firestore collection — `email_contacts/{doc_id}`:**

| Field | Description |
|---|---|
| `email` | Contact email |
| `name` | Person name |
| `title` | Job title |
| `website` | Company website |
| `country` | Country |
| `campaign` | Campaign tag (e.g. `NO_jun`) |
| `status` | `pending` / `approved` / `rejected` / `sent` / `replied` / `bounced` / `unsubscribed` / `converted` |
| `email_type` | `personal` / `role` / `department` / `admin` |
| `outreach_priority` | 1 – 4 |
| `mark_site_leads` | `true` if from site pipeline |
| `mark_leads` | `true` if from lead pipeline |
| `lead_id` | Reference to parent site/lead |

**Contact status lifecycle:**

```
pending → approved → sent → replied / bounced / unsubscribed / converted
       ↘ rejected
```

---

## 4. Campaigns

Campaigns group `email_contacts` by a shared tag and track the outreach process for that group.

### 4.1 Campaign creation

**From the master CRM sheet (automated):**
1. The master Google Sheet has a `Campaign` column
2. `Discover campaigns` button (or `crm-sync` API) scans the sheet for unique campaign IDs not yet in Firestore
3. New campaign documents are created with `status: draft` and `source: master-sheet`
4. A `crm-sync` job runs immediately to populate `campaign_contacts`

**Manually:**
Campaign documents can also be created directly via the API (`POST /api/crm/campaigns/{id}/create`).

### 4.2 Firestore structure — `campaigns/{campaign_id}`

| Field | Description |
|---|---|
| `campaign_id` | Unique string (e.g. `NO_jun`) |
| `status` | `draft` / `dosend` / `sent` / `cancelled` |
| `source` | `master-sheet` or `manual` |
| `outreach_email_account` | Email address of sending account |
| `owner` | Responsible person name |
| `contact_count` | Total contacts |
| `sites_count` | Unique domains |
| `countries` | List of country codes |
| `status_breakdown` | Map of contact status → count |
| `mail` | `{subject, body, type, css}` — email template |
| `sheet_url` | Google Drive spreadsheet URL (set after first export) |
| `sent_at` | ISO timestamp when activated |
| `updated_at` | Last modified |

**Subcollection — `campaigns/{id}/campaign_contacts/{doc_id}`:**

| Field | Description |
|---|---|
| `doc_id` | Matches `email_contacts` document ID |
| `email`, `name`, `title`, `website` | Contact details |
| `status` | `pending` / `excluded` / `sent` |
| `sent_at` | When outreach was sent |
| `last_action`, `last_action_status` | User-editable follow-up fields (sheet-controlled) |

### 4.3 Campaign sync flows

**CRM sync (master sheet → DB):** `crm_sync_lib.run_crm_sync()`
- Reads the master Google Sheet (configured in `sheets_config.py`)
- Creates/updates campaign documents
- Populates `campaign_contacts` subcollection (new contacts added, pending contacts updated, sent contacts never touched)

**Campaign sync (Drive sheet ↔ DB):** `campaign_sync_lib.run_campaign_sync()`
- Reads the campaign's own Google Drive spreadsheet
- Sheet wins for all fields **except** `status` and `sent_at` (always DB-controlled)
- New DB contacts not in the sheet are appended to the sheet
- No sheet exists → delegates to export (creates the sheet)

**Export / Full override (DB → sheet):** `campaign_export_lib.run_campaign_export()`
- Writes all contacts from DB to the Drive sheet
- Preserves `last_action` and `last_action_status` from existing sheet (matched by `doc_id`)
- Creates the sheet if it doesn't exist
- Adds a `Summary` tab with campaign-level statistics

### 4.4 Activation

When a campaign is set to `dosend` the **Activate** button appears. Activating marks it `sent` and timestamps `sent_at`. Sent/cancelled campaigns cannot be modified or synced.

---

## 5. Mail Accounts

Mail accounts are stored in `settings/mail_accounts/accounts/{email}` and referenced by campaigns via `outreach_email_account`.

**IMAP account fields:** `host`, `port`, `username`, `password`, `ssl`, `smtp_host`, `smtp_port`, `smtp_ssl`, `display_name`

**Gmail account fields:** `client_id`, `client_secret`, `refresh_token`, `access_token`, `display_name`

### Mail sending (`functions-crm/crm/mail_sender.py`)

All outbound email goes through `MailSender`:

1. **CSS inlining** via `premailer` — converts `<style>` block rules to inline `style=` attributes. Required because Gmail, Outlook, and MailChannels all strip embedded stylesheets.
2. **From header** formatted as `"Display Name <email>"` to avoid spam signals from bare addresses.
3. **`Message-ID`** and **`Date`** headers added — missing headers are a spam trigger.
4. **STARTTLS** (port 587) or **SSL** (port 465) selected automatically based on `smtp_ssl` flag.
5. After successful send, the message is **appended to the IMAP Sent folder** using `IMAP APPEND` so it appears in the sending account's sent box. Gmail saves to Sent natively.

---

## 6. Background Jobs

Long-running operations run as Cloud Tasks jobs:

| Job name | Triggered by | What it does |
|---|---|---|
| `contact-sync` | CRM discover step 1 | Export contacts from Leads DB to contact sheet |
| `push-and-sync` | CRM discover step 3 | Push selected contacts to CRM work sheet |
| `template-sync` | CRM discover step 5 | Sync CRM work sheet back to Leads DB |
| `campaign-sync` | Campaign Sync button | Drive sheet → Firestore sync |
| `campaign-export` | Full override button | Firestore → Drive sheet |
| `crm-sync` | Discover campaigns | Master sheet → Firestore |
| `statistics` | Collect statistics button | Run all `StatisticsBuilder` aggregations |
| `filter-count` | Filter facets page | Count leads matching a filter selection |

Jobs are stored in `jobs/{job_id}` in Firestore with fields: `name`, `status`, `params`, `result`, `error`, `queued_at`, `started_at`, `finished_at`.

---

## 7. Statistics

Aggregated statistics are written to the `statistics` Firestore collection by `StatisticsBuilder` (`functions-crm/crm/statistics_builder.py`):

| Document | Contents |
|---|---|
| `leads-overview` | Lead + leads_excluded counts, by-country, by-priority, exclusion rate |
| `site-leads-overview` | Site_leads + sites_excluded counts, by-country, by-sector, page-size buckets, exclusion rate |
| `lead-enrichment-funnel` | AI classified, social enriched, email checked completion % |
| `site-enrichment-funnel` | AI classified, location enriched, Brave enriched, email checked % |
| `data-quality` | Zero page counts, missing sitemaps, name/email mismatches |
| `email-contacts-funnel` | Breakdown by status, pipeline membership, email type, priority |
| `pipeline-coverage` | Contacts in site-only / leads-only / both pipelines, by country |
| `campaigns` | Total campaigns, contacts, sites; by status, source, owner, country |
| `priority-pr-country` | Lead counts by priority per country (with `countries/` subcollection) |
| `reasons-count` | Reason signal frequency per country |

Statistics can be regenerated from the Statistics page (**Collect statistics** button) or via CLI:
```bash
python app/maint_statistics.py
python app/maint_statistics.py --only site-funnel
```

---

## 8. Filter Facets

Precomputed catalog of selectable filter values, stored in `filter_facets/site_leads`. Built by scanning all `site_leads` + `site_contacts` documents:

- Platform, AI platform, AI sector, AI company type
- Country, AI country, location, location country
- Keywords (top 100 from array field)
- Page count (bucketed into size bands)
- Occupation, title (first word), email type

Regenerate with:
```bash
python app/build_filter_facets.py
python app/build_filter_facets.py --cap 300 --no-write   # JSON preview only
```

---

## 9. Technology Stack

| Layer | Technology |
|---|---|
| Backend API | Python 3.12 · Flask · Firebase Functions (Cloud Functions gen 2) |
| Job queue | Google Cloud Tasks |
| Database | Firestore (NoSQL) |
| File storage | Google Drive (via Drive API) |
| AI enrichment | OpenAI GPT-5.4 |
| Contact enrichment | Brave Search API |
| Web discovery | Bing Search API · aiohttp · asyncio |
| Agency catalogs | Sortlist · DesignRush · Proff · DAN · TopDevelopers · local directories *(lead pipeline only)* |
| Frontend | Vanilla HTML · Bootstrap 5 · Tabler Icons |
| Hosting | Firebase Hosting |
| Email sending | smtplib · imaplib · premailer |
| Spreadsheet sync | Google Sheets API v4 |

---

## 10. Google Cloud Batch Jobs (`cloud_batch/`)

The `cloud_batch/` framework runs long-running pipeline scripts on Google Cloud so they don't time out or block local machines. Jobs are triggered from the CRM frontend, from Cloud Scheduler (cron), or manually via CLI.

### Architecture

```
Cloud Scheduler / google-job.html / CLI
          │
          ▼ POST /api/crm/batch/jobs/{job}/run
CRM API (Firebase Cloud Function)
          │
          ▼ HTTP POST /run
Batch Runner (Cloud Run — batch-runner, min-instances=1)
          │  ┌──────────────────────────────────────────┐
          │  │ Flask /run  →  background thread          │
          │  │   job_runner.py                          │
          │  │     step 1: python -m app.site_agent ... │
          │  │     step 2: python -m app.site_enrich ... │
          │  │     ...                                  │
          └──┤ writes step status to Firestore          │
             └──────────────────────────────────────────┘
          │
          ▼ gcloud-batch-jobs/{job}/runs/{run_id}
Firestore
```

### Key components

`cloud_batch/job_definitions/*.json` — one JSON file per pipeline (site_pipeline, lead_pipeline, etc.) listing the steps, Cloud Scheduler cron expression, and parameter defaults.

`cloud_batch/job_runner.py` — runs each step as `python -m app.<module>` via `subprocess.Popen`, captures the last 50 lines of stdout+stderr, and writes per-step progress to Firestore.

`cloud_batch/entrypoint.py` — Flask HTTP server (gunicorn, 1 worker, 4 threads). Dedup-guards via Firestore (`status == "running"`), spawns a daemon thread for the job, and returns 202 immediately.

`cloud_batch/job_status.py` — all Firestore helpers for the `gcloud-batch-jobs` collection.

`functions-crm/handlers/batch.py` — CRM API blueprint that proxies requests to the Cloud Run runner using OIDC service-to-service authentication.

### Firestore layout

```
gcloud-batch-jobs/
  {job_name}/                      ← definition snapshot (synced at startup)
    runs/
      {run_id}/                    ← one doc per run
        status:   running | done | failed
        started:  timestamp
        steps: [
          { name, status, exit_code, started, finished, log_tail }
        ]
```

### Pipelines defined

| Job | Schedule | Steps |
|---|---|---|
| `site_pipeline` | Mon 02:00 UTC | site_agent → site_enrich → site_location → site_contact → site_email → site_smart_export → build_filter_facets |
| `site_enrich_pipeline` | on-demand | site_enrich → site_location → site_contact → site_email → build_filter_facets |
| `lead_pipeline` | Mon 03:00 UTC | lead_agent → lead_enrich → lead_contacts → leads_email → leads_smart_export → build_filter_facets |
| `lead_enrich_pipeline` | on-demand | lead_enrich → lead_contacts → leads_email → build_filter_facets |

### GCP services used

| Service | Purpose |
|---|---|
| Cloud Run | Hosts the batch-runner Flask service (always-warm, min 1 instance) |
| Cloud Scheduler | Triggers pipeline runs on cron schedule |
| Artifact Registry | Stores the Docker image (`batch-runner`) |
| Secret Manager | Stores all secrets (API keys, Firebase credentials) — injected as env vars |
| Firestore | Tracks job definitions, run history, and per-step progress |

Setup scripts live in `cloud_batch/setup/` (run once, in order 01→06). See `gcloud-job.md` (Documentation → Google Cloud Jobs) for the full setup guide.

---

## 11. Key Design Principles

**Isolation** — every parallel unit of work (site crawl, contact enrichment, job worker) runs in its own class with a hard timeout. One unit failing cannot stall or corrupt siblings.

**Idempotency** — all Firestore writes use `merge=True`. Re-running any pipeline step is safe — it updates existing documents and skips what hasn't changed.

**Sheet as working surface** — the Google Drive spreadsheet is the human interface. The DB is the source of truth for protected fields (`status`, `sent_at`); all other fields are sheet-controlled via the Sync operation.

**Single source for mail** — all outbound email goes through `MailSender`. CSS inlining, header injection, sent-folder append, and display-name formatting are applied once, everywhere.

**Dual discovery in the lead pipeline** — agencies are found t

---

## Command-line operations

These scripts are run by a developer from the project root. They are not available from the frontend UI.

### Rebuild the filter facet catalog

The filter facet catalog (the selectable values on the Filter Facets page) is built by scanning all contacts in the pipeline and collecting every value that appears. Run this after a large import to refresh the available filter options:

```bash
python app/build_filter_facets.py
python app/build_filter_facets.py --pipeline leads     # leads pipeline only
python app/build_filter_facets.py --no-write           # preview without saving
```

### Name enrichment (bulk)

The Enrich names function on the campaign page handles individual campaigns. For bulk runs across all campaigns, or to enrich a specific list of email addresses, use the CLI directly:

```bash
python app/campaign_name_enrich.py --campaign MY_CAMPAIGN_ID
python app/campaign_name_enrich.py --campaign MY_CAMPAIGN_ID --dry-run
python app/campaign_name_enrich.py --all                  # all campaigns at once
python app/campaign_name_enrich.py --emails a@b.com c@d.com
python app/campaign_name_enrich.py --campaign MY_CAMPAIGN_ID --debug
```

The `--debug` flag prints exactly what context Bing and Brave found for each email and what the AI accepted or rejected — useful for diagnosing why a contact is not getting a name.
