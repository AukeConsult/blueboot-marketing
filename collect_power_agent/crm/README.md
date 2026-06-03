# CRM

## CRM Pipeline Flow

```
Leads Database (Firestore)
  email_contacts collection
        │
        │  python crm\contact_sync.py --countries NO --min-pages 500
        ▼
┌─────────────────────────┐
│     Contact Sheet        │  ← Google Sheet (contacts tab)
│  (imported contacts)     │     review list, fill Select column
└────────────┬────────────┘
             │  Select != blank
             │  python crm\push_and_sync.py
             ▼
┌─────────────────────────┐
│     CRM Template         │  ← Google Sheet (Outreach tab)
│  (one row per site)      │     fill Status + Selger per lead
└────────────┬────────────┘
             │
             │  python crm\template_sync.py
             ▼
┌─────────────────────────┐    ┌──────────────────────────┐
│  crm/crm_template        │    │  site_leads collection   │
│  (Firestore)             │───▶│  crm_status              │
│                          │    │  crm_sales_person        │
└─────────────────────────┘    │  crm_date                │
                                └──────────────────────────┘
```

### API (Firebase Cloud Functions)

```
Client
  │  GET /api/crm/contact-sync?countries=NO&min_pages=500
  │  GET /api/crm/push-and-sync
  │  GET /api/crm/template-sync
  ▼
crmApi (Cloud Run, 30s)      ← trigger + job status
  │  enqueues Cloud Task
  ▼
crmWorker (Cloud Run, 15min) ← runs actual job (1GB RAM)
  │  updates job status
  ▼
crm_jobs/{job_id} (Firestore) ← poll GET /api/crm/status/{job_id}
```

Dashboard: https://blueboot-market.web.app/

---

## Architecture

```
blueboot_agency_power_agent/
  crm/                         <- CLI wrappers (run locally)
    config.py                  <- sheet IDs and Firestore paths
    contact_sync.py            <- import contacts to contact sheet
    contact_to_template.py     <- push selected to CRM template (no sync back)
    crm_template_sync.py       <- sync CRM template + optional --enrich
    push_and_sync.py           <- push selected -> CRM template + sync (combined)
    template_sync.py           <- sync CRM template -> Leads Database
    setup_outreach_sheet.py    <- one-time: create the CRM Google Sheet

  functions-crm/               <- Firebase Cloud Functions (deployed to GCP)
    main.py                    <- Flask app + 2 Cloud Function entry points
    requirements.txt
    crm/
      contact_sync_lib.py      <- single source of truth: import contacts
      push_and_sync_lib.py     <- single source of truth: push + sync
      crm_template_sync_lib.py <- single source of truth: template sync
      sheets_config.py         <- shared sheet IDs and Firestore paths

  public/
    index.html                 <- CRM dashboard (Bootstrap, hosted on Firebase)

  setup_gcp.sh / setup_gcp.bat <- one-time GCP setup
  deploy_crm.sh / deploy_crm.bat <- deploy functions + hosting
  deploy_hosting.sh / deploy_hosting.bat <- deploy hosting only
  test_crm_api.sh              <- test all API endpoints
  test_pages_filter.sh         <- test min/max pages filter
```

### Single source of truth

All business logic lives in `functions-crm/crm/` lib files. Local CLI scripts
in `crm/` set up OAuth2 auth and call the same libs. When deployed, all of
`functions-crm/` is uploaded to Cloud Run — the libs are already on the server.

---

## Google Sheets

| Sheet | ID | Tab |
|---|---|---|
| Contact Sheet | `1aMglV53NiMEArjld37HN5cxliyNRGzIP2mrM4kwlupA` | `contacts` |
| CRM Template | `1b1kGKIldeawESH3RYiYjOqRFXRR5kG_81qYRFZI1gSY` | `Outreach` |

Share both with: `77823673522-compute@developer.gserviceaccount.com` (Editor)

---

## Leads Database (Firestore) Structure

```
crm/
  contact_select/
    items/ {doc_id}            <- contacts from email_contacts
      select, campaign, ...

  crm_template/
    items/ {site_lead_id}      <- one doc per site in CRM template

crm_jobs/ {job_id}             <- async job status (API jobs)
  status: queued | running | done | error
  result: {...}
  error: "..."
  queued_at, started_at, finished_at

site_leads/ {site_lead_id}
  crm_status                   <- from Status column
  crm_sales_person             <- from Selger column
  crm_date                     <- from Dato lagt i column
```

---

## Workflow

```
1. Import contacts     fill contact sheet from email_contacts (default: NO)
2. (manual)            fill Select column in contact sheet
3. Push selected       push selected -> CRM template + sync to Leads Database
4. (manual)            fill Status and Selger in CRM template
5. Sync CRM            sync CRM template -> Leads Database + push crm fields back
```

---

## CLI Commands

### 1. Import contacts
Copies contacts from `email_contacts` to the contact sheet. Skips existing. Upserts to `crm/contact_select/items`.

```bash
python crm\contact_sync.py --countries NO
python crm\contact_sync.py --countries NO --max 500
python crm\contact_sync.py --countries NO --min-pages 500
python crm\contact_sync.py --countries NO --min-pages 1000 --max-pages 5000
python crm\contact_sync.py --countries NO --status pending --campaign NO_jun
python crm\contact_sync.py --sync-back
```

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Country codes e.g. `NO UK` |
| `--max` | — | Cap new rows added |
| `--min-pages` | — | Min page count (site size filter) |
| `--max-pages` | — | Max page count |
| `--status` | — | Filter by status |
| `--campaign` | — | Filter by campaign |
| `--sync-back` | — | Re-fetch + merge with sheet overrides |

### Page count size guide

| page_count | Size label |
|---|---|
| < 500 | Liten |
| 500 – 1 999 | Mellomstor |
| 2 000 – 4 999 | Stor |
| 5 000 – 24 999 | Enterprise |
| ≥ 25 000 | Ultra Enterprise |

### 2. Push selected to CRM
Reads contact sheet (Select != blank), pushes new sites to CRM template,
upserts to `crm/crm_template/items`, syncs CRM fields back to `site_leads`.

```bash
python crm\push_and_sync.py
python crm\push_and_sync.py --dry-run
```

### 3. Sync CRM to Leads Database
Syncs CRM template sheet to `crm/crm_template/items`. Pushes
`crm_status`, `crm_sales_person`, `crm_date` back to `site_leads`.

```bash
python crm\template_sync.py
python crm\template_sync.py --tab Outreach
```


### `contact_to_template.py` — push selected (no sync)

Earlier version of push — reads Contact Sheet (Select != blank), groups by site,
pushes to CRM Template. Does **not** sync crm_status/crm_sales_person back to
site_leads. Use `push_and_sync.py` for the full combined operation.

```bash
python crm\contact_to_template.py
python crm\contact_to_template.py --dry-run
```

### `crm_template_sync.py` — template sync with enrich option

Extended version of template sync that also supports `--enrich` to match CRM
template items to site_leads by website URL and merge enriched data.

```bash
python crm\crm_template_sync.py
python crm\crm_template_sync.py --enrich --dry-run
python crm\crm_template_sync.py --enrich
```

### `config.py` — shared configuration

Stores the Google Sheet IDs used by all CRM scripts:

```python
CRM_TEMPLATE_ID = "1b1kGKIldeawESH3RYiYjOqRFXRR5kG_81qYRFZI1gSY"
```

### `setup_outreach_sheet.py` — one-time sheet creation

Creates a new Google Sheet with the CRM outreach structure — headers, frozen row,
Status dropdown with colour coding, auto-filter. Run once when setting up a new sheet.

```bash
python crm\setup_outreach_sheet.py
python crm\setup_outreach_sheet.py --title "My Outreach Sheet"
```


---

## API (Firebase Cloud Functions)

Base URL: `https://us-central1-blueboot-market.cloudfunctions.net/crmApi`

### Architecture — two Cloud Functions, one Flask app

| Function | URL | Timeout | Memory | Purpose |
|---|---|---|---|---|
| `crmApi` | `.../crmApi/...` | 30 sec | 256MB | Trigger jobs, poll status |
| `crmWorker` | `.../crmWorker/...` | 15 min | 1GB | Run the actual job |

### How async jobs work
```
1. Client calls GET /api/crm/contact-sync  (hits crmApi)
2. crmApi creates job in crm_jobs/          (Leads Database)
3. crmApi enqueues a Cloud Task -> crmWorker URL
4. Returns job_id immediately               (202 Accepted)
5. Cloud Tasks calls crmWorker -> runs job (up to 15 min)
6. crmWorker updates job status in crm_jobs/
7. Client polls GET /api/crm/status/{job_id}
```

### Endpoints

```bash
# Import contacts
GET /api/crm/contact-sync?countries=NO&max=500
GET /api/crm/contact-sync?countries=NO&min_pages=1000&max_pages=5000
GET /api/crm/contact-sync?countries=NO&status=pending&campaign=NO_jun

# Push selected to CRM
GET /api/crm/push-and-sync

# Sync CRM to Leads Database
GET /api/crm/template-sync

# Poll job status
GET /api/crm/status/{job_id}

# List last 10 jobs
GET /api/crm/jobs?limit=10

# Debug
GET /api/crm/whoami
```

### contact-sync query parameters

| Param | Example | Description |
|---|---|---|
| `countries` | `NO` | Country code (one at a time) |
| `max` | `500` | Max rows to import |
| `min_pages` | `500` | Min site page count |
| `max_pages` | `5000` | Max site page count |
| `status` | `pending` | Filter by contact status |
| `campaign` | `NO_jun` | Filter by campaign tag |

---

## Dashboard

URL: `https://blueboot-market.web.app/`

Bootstrap single-page app. Features:
- Collapsible import form with all parameters
- Trigger buttons for all 3 operations
- Job list (last 10) with status badges and expandable details
- Auto-refreshes every 5 seconds
- Links to both Google Sheets

---

## Deploy

### One-time GCP setup
```bash
bash setup_gcp.sh
```
Enables Cloud Tasks API, creates `crm-queue`, grants service account roles.

### Deploy everything
```bash
bash deploy_crm.sh
```
Recreates venv, installs requirements, deploys functions + hosting.

### Deploy hosting only
```bash
bash deploy_hosting.sh
```

### crmWorker config (in `functions-crm/main.py`)
```python
@https_fn.on_request(
    region="us-central1",
    timeout_sec=900,                      # 15 minutes
    memory=fn_options.MemoryOption.GB_1,  # 1GB RAM
    max_instances=3,                      # max parallel jobs
)
def crmWorker(...):
```

---

## Testing

### Test all API endpoints
```bash
bash test_crm_api.sh
```

### Test pages filter
```bash
bash test_pages_filter.sh
```
Runs 5 tests with different page count ranges and verifies the `added` count
decreases as `min_pages` increases.

### CLI test for pages filter
```bash
python crm\contact_sync.py --countries NO --max 5 --min-pages 1000
python crm\contact_sync.py --countries NO --max 5 --max-pages 500
python crm\contact_sync.py --countries NO --max 5 --min-pages 500 --max-pages 5000
```

---

## CRM Template Columns

| # | Column | Source | Notes |
|---|---|---|---|
| 1 | Dato lagt i | today | → `crm_date` in site_leads |
| 2 | Bedrift | `company` / `domain` | |
| 3 | Nettside | `website` | |
| 4 | Bransje | `ai_sector \| ai_platform \| ai_company_type` | |
| 5 | Størrelse | size label + location | page_count based |
| 6 | Oppsummert | `ai_summary` | |
| 7 | Land | `country` | |
| 8 | Site-sider | `page_count` | |
| 9 | Beslutningstaker | first contact name | |
| 10 | Rolle | first contact title | |
| 11 | E-post | first contact email | |
| 12 | Telefon | first contact phone | text format |
| 13 | Contacts | `\|name,email,phone,title\|...` | all selected contacts |
| 14 | Score | — | manual |
| 15 | Status | — | manual → `crm_status` |
| 16 | Selger | — | manual → `crm_sales_pe