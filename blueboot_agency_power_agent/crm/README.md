# CRM

Google Sheets + Firestore CRM pipeline for outreach tracking.

---

## Architecture

```
blueboot_agency_power_agent/
  crm/                         <- CLI wrappers (run locally)
    contact_sync.py
    push_and_sync.py
    template_sync.py

  functions-crm/               <- Firebase Cloud Functions (deployed to GCP)
    main.py                    <- Flask app + 2 Cloud Function entry points
    requirements.txt
    crm/
      contact_sync_lib.py      <- single source of truth for all logic
      push_and_sync_lib.py
      crm_template_sync_lib.py
      push_and_sync_lib.py
      sheets_config.py

  setup_gcp.sh                 <- one-time GCP setup
  deploy_crm.sh                <- deploy to Firebase
  test_crm_api.sh              <- test all API endpoints
```

### Single source of truth

All business logic lives in `functions-crm/crm/` lib files. The local CLI scripts
in `crm/` are thin wrappers that set up local OAuth2 auth and call the same libs.
When you deploy, all of `functions-crm/` is uploaded to Cloud Run — the libs are
already on the server and just get called at runtime.

---

## Google Sheets

| Sheet | ID | Tab |
|---|---|---|
| Contact Sheet | `1aMglV53NiMEArjld37HN5cxliyNRGzIP2mrM4kwlupA` | `contacts` |
| CRM Template | `1b1kGKIldeawESH3RYiYjOqRFXRR5kG_81qYRFZI1gSY` | `Outreach` |

Share both with: `77823673522-compute@developer.gserviceaccount.com` (Editor)

---

## Firestore Structure

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
1. contact-sync      fill contact sheet from email_contacts (default: NO)
2. (manual)          fill Select column in contact sheet
3. push-and-sync     push selected -> CRM template + sync to Firestore + update site_leads
4. (manual)          fill Status and Selger in CRM template
5. template-sync     sync CRM template -> Firestore + push crm_status/crm_sales_person back
```

---

## CLI Commands (local)

### 1. contact-sync
```bash
python crm\contact_sync.py --countries NO
python crm\contact_sync.py --countries NO UK --max 500
python crm\contact_sync.py --sync-back
```

### 2. push-and-sync
```bash
python crm\push_and_sync.py
python crm\push_and_sync.py --dry-run
```

### 3. template-sync
```bash
python crm\template_sync.py
python crm\template_sync.py --tab Outreach
```

---

## API (Firebase Cloud Functions)

### Two Cloud Functions, one Flask app

Both `crmApi` and `crmWorker` run the same Flask app but with different resource limits.
The URL determines which Cloud Run service handles the request:

| Function | URL | Timeout | Memory | Purpose |
|---|---|---|---|---|
| `crmApi` | `.../crmApi/...` | 30 sec | 256MB | Trigger jobs, poll status |
| `crmWorker` | `.../crmWorker/...` | 15 min | 1GB | Run the actual job |

### How async jobs work

```
1. Client calls GET /api/crm/contact-sync   (hits crmApi)
2. crmApi creates job doc in crm_jobs/       (Firestore)
3. crmApi enqueues a Cloud Task pointing to crmWorker URL
4. crmApi returns job_id immediately         (202 Accepted)

5. Cloud Tasks calls POST /api/crm/worker/contact-sync/{job_id}  (hits crmWorker)
6. crmWorker runs the actual job (up to 15 min, 1GB RAM)
7. crmWorker updates job status in crm_jobs/ when done

8. Client polls GET /api/crm/status/{job_id} anytime
```

### Parallelism

`crmWorker` has `max_instances=3` — up to 3 jobs run simultaneously.
Cloud Tasks queues extras and retries when a slot opens.

**Warning:** avoid running `contact-sync` and `push-and-sync` in parallel —
both touch the contact sheet and may conflict.

### Endpoints

Base URL: `https://us-central1-blueboot-market.cloudfunctions.net/crmApi`

```bash
# Trigger contact-sync
GET /api/crm/contact-sync?countries=NO&max=500
GET /api/crm/contact-sync?countries=NO,UK&status=pending&campaign=NO_jun

# Trigger push-and-sync
GET /api/crm/push-and-sync

# Trigger template-sync
GET /api/crm/template-sync

# Poll job status
GET /api/crm/status/{job_id}

# List last 20 jobs
GET /api/crm/jobs

# Debug: show service account
GET /api/crm/whoami
```

### Example responses

Trigger:
```json
{
  "status": "queued",
  "job_id": "a3f2c1b8",
  "name": "contact-sync",
  "poll": "/api/crm/status/a3f2c1b8",
  "message": "Job queued. Poll /api/crm/status/a3f2c1b8 for result."
}
```

Status (done):
```json
{
  "id": "a3f2c1b8",
  "name": "contact-sync",
  "status": "done",
  "result": {"added": 42, "countries": ["NO"]},
  "queued_at": "2026-06-03T17:49:09Z",
  "started_at": "2026-06-03T17:49:12Z",
  "finished_at": "2026-06-03T17:51:44Z"
}
```

---

## Setup & Deploy

### One-time GCP setup
```bash
bash setup_gcp.sh
```
This enables Cloud Tasks API, creates the `crm-queue`, and grants the service account the required roles.

### Deploy
```bash
bash deploy_crm.sh
```
Creates `functions-crm/venv`, installs requirements, deploys both `crmApi` and `crmWorker`.

### Test
```bash
bash test_crm_api.sh
```

### Cloud Tasks queue config
Queue name: `crm-queue` (us-central1)
```bash
gcloud tasks queues describe crm-queue --location=us-central1
```

### crmWorker Cloud Run config
All in `functions-crm/main.py`:
```python
@https_fn.on_request(
    region="us-central1",
    timeout_sec=900,                      # 15 minutes
    memory=fn_options.MemoryOption.GB_1,  # 1GB RAM
    max_instances=3,                      # max parallel jobs
    concurrency=1,                        # one job per instance
)
def crmWorker(...):
```

---

## Local Setup

```bash
# Install dependencies
pip install google-api-python-client google-auth-oauthlib flask firebase-functions

# OAuth2 setup (one time — opens browser)
python crm\contact_sync.py --countries NO --max 1

# Token cached at: config/google_token.json
# Client secret:   config/google_oauth_client.json
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
| 16 | Selger | — | manual → `crm_sales_person` |
| 17 | Kommentar | — | manual |
| 18 | Tilbud | — | manual |
| 19 | site_lead_id | normalized website | deduplication key |
| 20 | ai_sector | `site_leads.ai_sector` | raw |
| 21 | ai_company_type | `site_leads.ai_company_type` | raw |
| 22 | ai_platform | `site_leads.ai_platform` | raw |

### Størrelse mapping

| page_count | Label |
|---|---|
| < 500 | Liten |
| 500 – 1 999 | Mellomstor |
| 2 000 – 4 999 | Stor |
| 5 000 – 24 999 | Enterprise |
| ≥ 25 000 | Ultra Enterprise |
