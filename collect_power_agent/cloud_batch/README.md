# cloud_batch — Google Cloud Batch Job Framework

Runs the Blueboot pipeline scripts as scheduled or on-demand batch jobs on Google Cloud Run.
Each pipeline is a **job definition** (what to run) paired with one or more **tasks** (when to run it and with what parameters). Progress is tracked live in Firestore and visible in the CRM frontend at `google-job.html`.

---

## How it works

```
TRIGGER
  Cloud Scheduler (cron, per task)   CRM frontend (google-job.html)
  one scheduler job per task         or API (POST /api/crm/batch/jobs/{job}/tasks/{task}/run)
        │                                        │
        └────────── HTTP POST /run ──────────────┘
                              │
                              ▼
                    CRM API  (functions-crm)
                    handlers/batch.py
                    validates, calls batch-runner
                              │
                              ▼
               batch-runner  (Cloud Run service, min-instances=1)
               entrypoint.py  Flask /run endpoint
                 ├── load task params from Firestore   (if task_id given)
                 ├── dedup check  (is this job already running?)
                 ├── create run doc in Firestore
                 └── spawn background thread → job_runner.py
                              │
                              │  python -m app.site_agent --countries NO
                              │  python -m app.site_enrich_agent --countries NO
                              │  ...
                              ▼
                    Firestore  gcloud-batch-jobs/
                    updated after every step
                    (status, exit_code, log_tail)
```

The runner returns `202 Accepted` immediately. The actual pipeline runs in a background thread
which can take hours. Cloud Run keeps the container alive as long as the thread is running
(`min-instances=1` prevents cold eviction).

---

## Jobs and Tasks

There are two distinct concepts:

**Job definition** — the static template describing what a pipeline does.
Defines the steps, accepted parameters (schema), and description. Rarely changes after setup.
Stored in Firestore under `gcloud-batch-jobs/{job_name}`.

**Task** — an operational run configuration attached to a job.
Defines *when* the job runs (cron schedule) and *with what parameters* (actual values:
countries, campaign, workers, etc.). One job can have multiple tasks — for example
`site_pipeline` might have a "NO Monday" task and a "SE Tuesday" task, each with
its own schedule and country/campaign settings.
Stored in Firestore under `gcloud-batch-jobs/{job_name}/tasks/{task_id}`.

---

## Pipelines

### `site_pipeline` — Full site discovery + enrichment

Discovers end-user company websites and enriches them through the full stack.

```
Step 1  site_agent              Discover sites via Bing/Brave → site_leads
Step 2  site_enrich_agent       AI classify each site (GPT)
Step 3  site_contact_enrich     Enrich contacts via Brave + GPT
Step 4  site_location_enrich    AI-infer company city/region
Step 5  site_email_check        Classify email type + contact role
Step 6  site_smart_export       Export to email_contacts + Excel → Google Drive
Step 7  email_contacts_export   Unified Excel (pending contacts) → Google Drive
```

Parameters (defined in params schema):

| Param | Required | Default | Description |
|---|---|---|---|
| `countries` | ✓ | — | Comma-separated codes: `NO`, `NO,DK,SE` |
| `campaign` | ✓ | — | Campaign label: `NO_jun02` |
| `workers` | | `8` | Concurrent async workers in site_agent |
| `max_results` | | `500` | Max Bing results per query |
| `dry_run` | | `false` | Preview only — no Firestore writes |
| `force` | | `false` | Re-process already enriched items |

---

### `site_enrich_pipeline` — Enrichment only (no discovery)

Steps 2–6 of `site_pipeline`. Use when sites are already in `site_leads`.

Parameters: `countries`, `campaign`, `dry_run`, `force`

---

### `lead_pipeline` — Full agency/reseller discovery + enrichment

```
Step 1  lead_agent              Discover agency leads → leads collection
Step 2  lead_enrich_agent       AI classify each lead (GPT)
Step 3  lead_enrich_contacts    Enrich contacts: LinkedIn, social profiles
Step 4  leads_email_check       Classify email type + contact role
Step 5  leads_smart_export      Export to email_contacts + Excel → Google Drive
Step 6  email_contacts_export   Unified Excel (pending contacts) → Google Drive
```

Parameters:

| Param | Required | Default | Description |
|---|---|---|---|
| `countries` | ✓ | — | Comma-separated country codes |
| `campaign` | ✓ | — | Campaign label |
| `mode` | | `both` | lead_agent mode: `search`, `sitemap`, `both` |
| `dry_run` | | `false` | Preview only |
| `force` | | `false` | Re-process already enriched items |

---

### `lead_enrich_pipeline` — Enrichment only (no discovery)

Steps 2–5 of `lead_pipeline`. `countries` is optional — omit to process all.

---

## Firestore Layout

All data lives under `gcloud-batch-jobs`.

```
gcloud-batch-jobs/
  {job_name}                         ← job definition document
    name:        "site_pipeline"
    description: "..."
    params:      { ... schema ... }  ← parameter types/defaults only, no values
    steps:       [ ... ]
    updated_at:  "2026-06-09T..."

    tasks/                           ← one doc per scheduled/named run config
      {task_id}
        task_id:    "a1b2c3d4"
        job:        "site_pipeline"
        name:       "NO Monday"
        schedule:   "0 2 * * 1"      ← cron, or "" for manual-only
        params:     { countries: "NO,DK", campaign: "NO_jun02", workers: 8 }
        active:     true
        created_at: "2026-06-09T..."
        updated_at: "2026-06-09T..."

    runs/                            ← one doc per execution
      {run_id}                       ← e.g. 20260609_143201_a1b2c3
        run_id:       "20260609_143201_a1b2c3"
        job:          "site_pipeline"
        status:       "running" | "done" | "failed"
        params:       { countries: "NO", campaign: "NO_jun02", ... }
        triggered_by: "scheduler" | "manual"
        started_at:   "2026-06-09T14:32:01Z"
        ended_at:     "2026-06-09T17:15:44Z"
        steps: [
          { name, status, exit_code, started_at, ended_at, log_tail },
          ...
        ]
```

`status = "running"` is the dedup key — the runner refuses to start a second
instance of a job that already has a running run doc.

---

## File Structure

```
cloud_batch/
  README.md                    ← this file
  __init__.py
  Dockerfile                   ← build from project root
  requirements.txt             ← Flask, firebase-admin, gunicorn, google-cloud-scheduler

  job_definitions/             ← JSON files used for initial Firestore seed only
    site_pipeline.json
    site_enrich_pipeline.json
    lead_pipeline.json
    lead_enrich_pipeline.json

  job_status.py                Firestore helpers (jobs, tasks, runs)
  job_runner.py                Runs steps as subprocesses, writes Firestore
  entrypoint.py                Flask HTTP server (Cloud Run target)
  scheduler_sync.py            Sync Firestore tasks → Cloud Scheduler (REST API, no CLI)
  scheduler_setup.py           CLI alternative to scheduler_sync (reads Firestore, uses gcloud)

  setup/
    01_enable_apis.sh
    02_service_account.sh
    03_artifact_registry.sh
    04_deploy_cloudrun.sh
    05_setup_scheduler.sh
    06_secrets.sh
    setup_all.sh
    teardown.sh
```

Other files:

```
functions-crm/handlers/batch.py    CRM API Blueprint (jobs + tasks + runs endpoints)
public/google-job.html             Frontend: manage jobs, tasks, run history
setup_gcp.sh                       One-time GCP project setup (APIs + IAM)
deploy_batch.sh                    Build → Deploy → Seed (all in one command)
```

---

## First-Time Setup

### Prerequisites

- `gcloud` CLI installed and authenticated
- No local Docker required — image built on GCP via Cloud Build
- All API keys filled in root `.env`

---

### Step 1 — GCP project setup (one time)

```bash
bash setup_gcp.sh
```

Enables APIs (Cloud Run, Build, Scheduler, Artifact Registry, Tasks), creates the
Cloud Tasks queue, and grants all required IAM roles including
`roles/cloudscheduler.admin` so the batch-runner can create/update Cloud Scheduler
jobs via the API.

---

### Step 2 — Push secrets to Secret Manager

```bash
bash cloud_batch/setup/06_secrets.sh
```

Reads `.env` and pushes all values to Secret Manager. Missing keys get a `placeholder`
value so the deploy never fails.

`BATCH_SECRET` is auto-generated and appended to `.env` if not present.

---

### Step 3 — Build, deploy, and seed

```bash
bash deploy_batch.sh
```

Does three things in sequence:
1. Builds the Docker image via Cloud Build
2. Deploys to Cloud Run
3. Seeds all job definitions into Firestore (`python app/seed_batch_jobs.py`)

After deploy, get the service URL and add it to `.env`:
```bash
gcloud run services describe batch-runner \
  --platform managed --region us-central1 --project blueboot-market \
  --format "value(status.url)"
```

```ini
# .env
BATCH_RUNNER_URL=https://batch-runner-xxxx-uc.a.run.app
```

---

### Step 4 — Wire up the CRM API

```bash
bash deploy_crm.sh
```

Reads `BATCH_RUNNER_URL` and `BATCH_SECRET` from `.env` and writes them to
`functions-crm/.env` automatically before deploying.

---

### Step 5 — Create tasks and sync schedules

Open `google-job.html` → click **Add task** on a job card.

Fill in:
- **Task name** — e.g. "NO Monday"
- **Cron schedule** — e.g. `0 2 * * 1`
- **Parameter values** — countries, campaign, workers, etc.

Click **Save task** → the task is saved to Firestore and Cloud Scheduler is
automatically updated. No CLI needed.

Verify: `google-job.html` shows the task row with its cron and params.

---

### Secrets → env var mapping

| `.env` key | Secret Manager name | Cloud Run env var |
|---|---|---|
| `FIREBASE_KEY_JSON` | `firebase-key-json` | `FIREBASE_KEY_JSON` |
| `OPENAI_API_KEY` | `openai-key` | `OPENAI_API_KEY` |
| `BRAVE_API_KEY` | `brave-key` | `BRAVE_API_KEY` |
| `BING_API_KEY` | `bing-key` | `BING_API_KEY` |
| `GITHUB_TOKEN` | `github-token` | `GITHUB_TOKEN` |
| `SMTP_PASSWORD` | `smtp-password` | `SMTP_PASSWORD` |
| `BATCH_SECRET` | `batch-secret` | `BATCH_SECRET` |

---

## Managing Jobs and Tasks

### Frontend (google-job.html)

Each job card shows its task list. Per task:

| Button | Action |
|---|---|
| ▶ (green) | Run task immediately using its stored params |
| ✏ Edit | Change name, schedule, params, or pause/activate |
| 🗑 Delete | Remove the task (and its Cloud Scheduler job on next sync) |

**Add task** — creates a new scheduled run config for a job.

**Sync schedules** (header button) — manually forces a full Cloud Scheduler sync
from Firestore. Also fires automatically whenever a task is saved or deleted.

**Edit job** — edits the static job definition (description, params schema, steps).
Rarely needed after initial setup.

**Ad-hoc run** — one-off run with custom params, not saved as a task.

**History** — shows recent runs with step-level status and log tails.

### Via the CRM API

```bash
# List all jobs (includes tasks + last run)
GET /api/crm/batch/jobs

# List tasks for a job
GET /api/crm/batch/jobs/site_pipeline/tasks

# Create a task
POST /api/crm/batch/jobs/site_pipeline/tasks
{ "name": "NO Monday", "schedule": "0 2 * * 1",
  "params": { "countries": "NO", "campaign": "NO_jun02" } }

# Update a task
PATCH /api/crm/batch/jobs/site_pipeline/tasks/{task_id}
{ "schedule": "0 3 * * 1", "params": { "workers": 12 } }

# Delete a task
DELETE /api/crm/batch/jobs/site_pipeline/tasks/{task_id}

# Run a specific task (uses stored params)
POST /api/crm/batch/jobs/site_pipeline/tasks/{task_id}/run

# Ad-hoc run with custom params
POST /api/crm/batch/jobs/site_pipeline/run
{ "params": { "countries": "FI", "campaign": "FI_test", "dry_run": true } }

# Sync Cloud Scheduler from Firestore tasks
POST /api/crm/batch/sync-schedulers

# Poll a run
GET /api/crm/batch/jobs/site_pipeline/runs/{run_id}
```

---

## Redeploying After Changes

### Code changes (Python, Dockerfile, requirements)

```bash
bash deploy_batch.sh
```

Builds, deploys, and seeds in one command. The seed step is now always included —
no need to run `seed_batch_jobs.py` separately.

### Job definition changes only (JSON files)

Job definitions are stored in Firestore, not baked into the image. After editing
a `.json` file:

```bash
python app/seed_batch_jobs.py
```

No image rebuild needed.

### Schedule / task changes

Edit tasks directly in `google-job.html`. Cloud Scheduler is updated automatically
on save — no CLI step needed.

To force a full re-sync from Firestore (e.g. after a manual Firestore edit):

```bash
# Via API
curl -X POST https://.../api/crm/batch/sync-schedulers \
  -H "Authorization: Bearer $TOKEN"

# Or locally via CLI (reads from Firestore, uses gcloud)
python -m cloud_batch.scheduler_setup --runner-url $BATCH_RUNNER_URL
```

---

## Adding a New Pipeline

1. Create `cloud_batch/job_definitions/my_pipeline.json` (steps and param schema only —
   no `schedule` or param values; those live on tasks):

```json
{
  "name": "my_pipeline",
  "description": "What this pipeline does",
  "params": {
    "countries": { "type": "str", "required": true,  "help": "Country codes" },
    "campaign":  { "type": "str", "required": true,  "help": "Campaign label" },
    "dry_run":   { "type": "bool", "default": false, "help": "Preview only" }
  },
  "steps": [
    { "name": "step_one",  "module": "my_script",       "args": ["--countries", "{countries}"], "retries": 2, "retry_delay_sec": 60, "on_error": "abort" },
    { "name": "step_two",  "module": "my_other_script",  "args": ["--countries", "{countries}"], "on_error": "continue" }
  ]
}
```

2. If script modules are new, redeploy (otherwise seed only):

```bash
bash deploy_batch.sh          # new modules: build + deploy + seed
# or
python app/seed_batch_jobs.py # definition changes only
```

3. Add tasks in the frontend — click **Add task** on the new job card, fill in
   the cron and parameter values, save. Cloud Scheduler is updated automatically.

---

## Step Error Behaviour

| `on_error` value | Behaviour |
|---|---|
| `abort` | Stop immediately. Remaining steps → `skipped`. Run status = `failed`. |
| `continue` | Mark step `failed`, keep going. Run status = `failed` even if later steps pass. |

Discovery and export steps default to `abort`. Enrichment steps default to `continue`.

Optional retry fields can be added to any step:

| Field | Behaviour |
|---|---|
| `retries` | Extra attempts after the first failed subprocess exit. Default `0`. |
| `retry_delay_sec` | Seconds to wait between attempts. Default `30`. |

Retries happen before `on_error` is applied. If the last attempt still fails,
`abort` or `continue` decides what happens to the rest of the pipeline.

---

## Local Development

### Run the batch runner locally (hits real Firestore)

```bash
export GOOGLE_APPLICATION_CREDENTIALS=config/serviceAccountKey.json
export PORT=8081
python -m cloud_batch.entrypoint
```

Trigger a dry-run:
```bash
curl -s -X POST localhost:8081/run \
  -H "Content-Type: application/json" \
  -d '{"job":"site_pipeline","params":{"countries":"NO","campaign":"NO_test","dry_run":true}}'
```

### Fully offline with Firebase emulator

```bash
# Terminal 1
firebase emulators:start --only firestore

# Terminal 2
export FIRESTORE_EMULATOR_HOST=localhost:8080
export PORT=8081
python -m cloud_batch.entrypoint

# Terminal 3
curl -s -X POST localhost:8081/run \
  -H "Content-Type: application/json" \
  -d '{"job":"lead_enrich_pipeline","params":{"campaign":"test","dry_run":true}}'
```

---

## GCP Services Used

| Service | Purpose |
|---|---|
| Cloud Run | Hosts the batch-runner Flask service (`min-instances=1`) |
| Cloud Scheduler | One cron job per active task — fires `POST /run` with `task_id` |
| Artifact Registry | Stores the Docker image |
| Secret Manager | API keys and `BATCH_SECRET` |
| Firestore | Job definitions, tasks, and run history under `gcloud-batch-jobs/` |
| IAM — compute SA | `datastore.user`, `secretmanager.secretAccessor`, `run.invoker`, `cloudscheduler.admin` |

---

## Updating Secrets

1. Edit `.env` locally
2. Push to Secret Manager: `bash cloud_batch/setup/06_secrets.sh`
3. Restart Cloud Run: `bash deploy_batch.sh`

Rotating a single secret without the full script:
```bash
echo -n "new-value" | gcloud secrets versions add openai-key \
  --data-file=- --project blueboot-market
```

---

## Troubleshooting

**Job starts then goes silent**
Check Cloud Run logs: `gcloud run services logs read batch-runner --region us-central1`

**`dedup check failed — already running` but no job is active**
A previous run crashed before writing `status: done`. Fix in Firestore:
open `gcloud-batch-jobs/{job}/runs/{stuck_run_id}` and set `status` to `failed`.

**`BATCH_RUNNER_URL is not configured` in CRM API**
`BATCH_RUNNER_URL` env var is missing from the `crmApi` Cloud Function. Re-run `bash deploy_crm.sh`.

**Sync schedules returns an error about `cloudscheduler.admin`**
The batch-runner service account is missing `roles/cloudscheduler.admin`.
Re-run `bash setup_gcp.sh` — it grants this role automatically.

**Steps run but nothing appears in site_leads / leads**
Check if `dry_run: true` is set in the run's params.
