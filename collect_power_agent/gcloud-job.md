# Google Batch Jobs — Architecture & Setup Guide

## Overview

`cloud_batch/` is a long-running job orchestration framework for the Blueboot pipeline scripts. It runs pipeline steps as isolated subprocesses on a Cloud Run service, tracks progress in Firestore, and exposes job management through the CRM frontend (`google-job.html`).

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  TRIGGER LAYER                                                              │
│                                                                             │
│   Cloud Scheduler          CRM Frontend            Manual CLI              │
│   (cron per job)          (google-job.html)     (scheduler_setup.py)      │
│        │                        │                        │                 │
│        └──────── POST /run ─────┴────────────────────────┘                 │
└─────────────────────────────────────────────────┬───────────────────────────┘
                                                  │ HTTP POST /run
                                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  CRM API  (functions-crm / crmApi Cloud Function)                          │
│                                                                             │
│   POST /api/crm/batch/jobs/{job}/run                                       │
│     → validate params → call batch-runner Cloud Run → return 202           │
│                                                                             │
│   GET  /api/crm/batch/jobs                  list definitions + last run    │
│   GET  /api/crm/batch/jobs/{job}/runs       run history                    │
│   GET  /api/crm/batch/jobs/{job}/runs/{id}  poll single run (for live UI)  │
└─────────────────────────────────────────────────┬───────────────────────────┘
                                                  │ HTTP POST /run
                                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  BATCH RUNNER  (Cloud Run service: batch-runner, min-instances=1)          │
│                                                                             │
│   entrypoint.py  Flask /run endpoint                                       │
│     → dedup check (Firestore: is this job already running?)                │
│     → create run doc in gcloud-batch-jobs/{job}/runs/{run_id}              │
│     → spawn background thread: job_runner.py                               │
│     → return 202 immediately                                               │
│                                                                             │
│   job_runner.py  runs each step as subprocess:                             │
│     python -m app.site_agent --countries NO --workers 8                    │
│     python -m app.site_enrich_agent --countries NO                         │
│     ...                                                                    │
│     → updates Firestore step-by-step (status, exit_code, log_tail)        │
└─────────────────────────────────────────────────┬───────────────────────────┘
                                                  │ writes
                                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  FIRESTORE — gcloud-batch-jobs/                                            │
│                                                                             │
│   {job_name}                          job definition doc                   │
│     name, description, schedule                                            │
│     params_schema, steps[]                                                 │
│                                                                             │
│     runs/                             subcollection                        │
│       {run_id}                        one doc per execution                │
│         status: running|done|failed                                        │
│         params: {countries, campaign, ...}                                 │
│         triggered_by: scheduler|manual                                     │
│         started_at / ended_at                                              │
│         steps: [                                                           │
│           {name, status, exit_code, started_at, ended_at, log_tail}       │
│         ]                                                                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Pipelines

### site_pipeline (full)
Discovers + enriches end-user company websites. Runs all 7 steps.

| # | Step | Script | Key params |
|---|---|---|---|
| 1 | discover | `site_agent` | `--countries`, `--workers`, `--max-results` |
| 2 | enrich_ai | `site_enrich_agent` | `--countries` |
| 3 | enrich_contacts | `site_contact_enrich` | `--countries` |
| 4 | enrich_location | `site_location_enrich` | `--countries` |
| 5 | email_check | `site_email_check` | `--countries` |
| 6 | export | `site_smart_export` | `--countries`, `--campaign`, `--write-contacts` |
| 7 | export_contacts | `email_contacts_export` | `--countries`, `--campaign` |

**Default schedule:** `0 2 * * 1` (Mondays at 02:00 UTC)

### site_enrich_pipeline (no discovery)
Steps 2–6 of site_pipeline only. Use when sites are already discovered.

### lead_pipeline (full)
Discovers + enriches web agency / reseller leads. 6 steps.

| # | Step | Script |
|---|---|---|
| 1 | discover | `lead_agent` |
| 2 | enrich_ai | `lead_enrich_agent` |
| 3 | enrich_contacts | `lead_enrich_contacts` |
| 4 | email_check | `leads_email_check` |
| 5 | export | `leads_smart_export` |
| 6 | export_contacts | `email_contacts_export` |

**Default schedule:** `0 3 * * 1` (Mondays at 03:00 UTC)

### lead_enrich_pipeline (no discovery)
Steps 2–5 of lead_pipeline only.

---

## Firestore Layout

All data lives under the single collection `gcloud-batch-jobs`.

```
gcloud-batch-jobs/
  site_pipeline                    ← job definition document
    name:        "site_pipeline"
    description: "Full site discovery + enrichment pipeline"
    schedule:    "0 2 * * 1"
    params:      { countries:{...}, campaign:{...}, workers:{...}, ... }
    steps:       [ {name, module, args, on_error}, ... ]
    updated_at:  "2026-06-09T..."

    runs/                          ← subcollection
      20260609_143201_a1b2c3       ← run document
        run_id:       "20260609_143201_a1b2c3"
        job:          "site_pipeline"
        status:       "done"
        params:       { countries: "NO", campaign: "NO_jun02" }
        triggered_by: "scheduler"
        started_at:   "2026-06-09T14:32:01Z"
        ended_at:     "2026-06-09T17:15:44Z"
        steps: [
          { name: "discover",  status: "done",   exit_code: 0,
            started_at: "...", ended_at: "...",  log_tail: "...last 50 lines" },
          { name: "enrich_ai", status: "done",   exit_code: 0, ... },
          ...
        ]

  lead_pipeline                    ← same structure
    runs/ ...
```

---

## File Structure

```
cloud_batch/
  job_definitions/
    site_pipeline.json
    site_enrich_pipeline.json
    lead_pipeline.json
    lead_enrich_pipeline.json
  __init__.py
  job_status.py          Firestore helpers (gcloud-batch-jobs)
  job_runner.py          Runs steps as subprocesses, updates Firestore
  entrypoint.py          Flask HTTP server (Cloud Run target)
  scheduler_setup.py     CLI: read job defs → create Cloud Scheduler jobs
  Dockerfile             Build from project root: docker build -f cloud_batch/Dockerfile .
  requirements.txt       Flask, firebase-admin, gunicorn

  setup/
    01_enable_apis.sh    Enable Cloud Run, Scheduler, Artifact Registry, Secret Manager
    02_service_account.sh  Create batch-runner SA + grant IAM roles
    03_artifact_registry.sh  Create repo, build + push Docker image
    04_deploy_cloudrun.sh  Deploy Cloud Run service (min-instances=1)
    05_setup_scheduler.sh  Create Cloud Scheduler jobs from job_definitions/
    06_secrets.sh        Push API keys to Secret Manager
    setup_all.sh         Runs 01 → 06 in order (idempotent)
    teardown.sh          Delete Cloud Run + Scheduler jobs (keeps Firestore)

functions-crm/handlers/
  batch.py               CRM API blueprint (list jobs, trigger run, poll run)

public/
  google-job.html        Frontend: run jobs on demand, live step progress, history
```

---

## First-Time Setup

```bash
# 1. Set environment
export GCP_PROJECT=blueboot-market
export GCP_LOCATION=us-central1

# 2. Run all setup steps
bash cloud_batch/setup/setup_all.sh

# 3. Set BATCH_RUNNER_URL in the CRM Cloud Function env vars
#    (printed at end of setup_all.sh)
gcloud functions deploy crmApi \
  --update-env-vars BATCH_RUNNER_URL=https://batch-runner-xxx-uc.a.run.app
```

Or run individual steps:

```bash
bash cloud_batch/setup/01_enable_apis.sh
bash cloud_batch/setup/02_service_account.sh
bash cloud_batch/setup/06_secrets.sh          # stores OPENAI_KEY, BATCH_SECRET, BING_KEY
bash cloud_batch/setup/03_artifact_registry.sh
bash cloud_batch/setup/04_deploy_cloudrun.sh
bash cloud_batch/setup/05_setup_scheduler.sh
```

---

## Triggering a Job Manually

### From the CRM frontend
Open `google-job.html` → click **Run now** on any job → fill params → submit.

### Via the API
```bash
curl -X POST https://.../api/crm/batch/jobs/site_pipeline/run \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"params": {"countries": "NO", "campaign": "NO_jun02"}}'
```

### Poll for status
```bash
curl https://.../api/crm/batch/jobs/site_pipeline/runs/20260609_143201_a1b2c3 \
  -H "Authorization: Bearer $TOKEN"
```

---

## Adding a New Pipeline

1. Create `cloud_batch/job_definitions/my_pipeline.json`:
```json
{
  "name": "my_pipeline",
  "description": "...",
  "schedule": null,
  "params": {
    "countries": { "type": "str", "required": true, "help": "..." }
  },
  "steps": [
    { "name": "step1", "module": "my_script", "args": ["--countries", "{countries}"], "on_error": "abort" }
  ]
}
```

2. Redeploy the Cloud Run service:
```bash
bash cloud_batch/setup/03_artifact_registry.sh
bash cloud_batch/setup/04_deploy_cloudrun.sh
```

3. If the job has a `schedule`, re-run the scheduler setup:
```bash
bash cloud_batch/setup/05_setup_scheduler.sh
```

---

## on_error Behaviour

| Value | Behaviour |
|---|---|
| `abort` | Stop all remaining steps, mark run as `failed` |
| `continue` | Mark step as `failed`, continue with next step. Run final status is `failed` even if later steps succeed. |

---

## GCP Services Used

| Service | Purpose |
|---|---|
| Cloud Run | Hosts the batch-runner Flask service (min-instances=1, no timeout) |
| Cloud Scheduler | Fires HTTP POST /run on cron schedule per job definition |
| Artifact Registry | Stores the Docker image for the batch-runner |
| Secret Manager | Stores OPENAI_API_KEY, BING_API_KEY, BATCH_SECRET |
| Firestore | Job definitions + run status in `gcloud-batch-jobs/` |
| IAM | `batch-runner` service account with `datastore.user`, `secretmanager.secretAccessor`, `run.invoker` |
