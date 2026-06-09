# cloud_batch — Google Cloud Batch Job Framework

Runs the Blueboot pipeline scripts as scheduled or on-demand batch jobs on Google Cloud Run.
Each pipeline is a sequence of sub-jobs (steps) defined in a JSON file. Progress is tracked
live in Firestore and visible in the CRM frontend at `google-job.html`.

---

## How it works

```
TRIGGER
  Cloud Scheduler (cron)          CRM frontend (google-job.html)
  or CLI (scheduler_setup.py)     or API (POST /api/crm/batch/jobs/{job}/run)
        │                                        │
        └────────── HTTP POST /run ──────────────┘
                              │
                              ▼
                    CRM API  (functions-crm)
                    handlers/batch.py
                    validates params, calls batch-runner
                              │
                              ▼
               batch-runner  (Cloud Run service, min-instances=1)
               entrypoint.py  Flask /run endpoint
                 ├── dedup check  (is this job already running?)
                 ├── create run doc in Firestore
                 └── spawn background thread → job_runner.py
                              │
                              │  python -m app.site_agent --countries NO
                              │  python -m app.site_enrich_agent --countries NO
                              │  python -m app.site_contact_enrich --countries NO
                              │  ...
                              ▼
                    Firestore  gcloud-batch-jobs/
                    updated after every step
                    (status, exit_code, log_tail)
```

The runner returns `202 Accepted` immediately. The actual pipeline runs in a background thread,
which can take hours. Cloud Run keeps the container alive as long as the thread is running
(`min-instances=1` prevents cold eviction).

---

## Pipelines

### `site_pipeline` — Full site discovery + enrichment

Discovers end-user company websites and enriches them through the full stack.
Default schedule: **Mondays 02:00 UTC**

```
Step 1  site_agent              Discover sites via Bing/Brave → site_leads
Step 2  site_enrich_agent       AI classify each site (GPT)
Step 3  site_contact_enrich     Enrich contacts via Brave + GPT
Step 4  site_location_enrich    AI-infer company city/region
Step 5  site_email_check        Classify email type + contact role
Step 6  site_smart_export       Export to email_contacts + Excel
Step 7  email_contacts_export   Unified Excel (pending contacts)
```

Parameters:

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

Discovers web agencies and resellers and pushes them through the lead stack.
Default schedule: **Mondays 03:00 UTC**

```
Step 1  lead_agent              Discover agency leads → leads collection
Step 2  lead_enrich_agent       AI classify each lead (GPT)
Step 3  lead_enrich_contacts    Enrich contacts: LinkedIn, social profiles
Step 4  leads_email_check       Classify email type + contact role
Step 5  leads_smart_export      Export to email_contacts + Excel
Step 6  email_contacts_export   Unified Excel (pending contacts)
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

All data lives under the single top-level collection `gcloud-batch-jobs`.

```
gcloud-batch-jobs/
  {job_name}                         ← job definition document
    name:        "site_pipeline"
    description: "..."
    schedule:    "0 2 * * 1"         ← null if no schedule
    params:      { ... schema ... }
    steps:       [ ... ]
    updated_at:  "2026-06-09T..."

    runs/                            ← subcollection, one doc per execution
      {run_id}                       ← e.g. 20260609_143201_a1b2c3
        run_id:       "20260609_143201_a1b2c3"
        job:          "site_pipeline"
        status:       "running" | "done" | "failed"
        params:       { countries: "NO", campaign: "NO_jun02", ... }
        triggered_by: "scheduler" | "manual"
        started_at:   "2026-06-09T14:32:01Z"
        ended_at:     "2026-06-09T17:15:44Z"   ← null while running
        steps: [
          {
            name:       "discover",
            status:     "done" | "running" | "failed" | "pending" | "skipped",
            exit_code:  0,
            started_at: "...",
            ended_at:   "...",
            log_tail:   "...last 50 lines of stdout+stderr"
          },
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
  requirements.txt             ← Flask, firebase-admin, gunicorn

  job_definitions/
    site_pipeline.json
    site_enrich_pipeline.json
    lead_pipeline.json
    lead_enrich_pipeline.json

  job_status.py                Firestore helpers (read/write gcloud-batch-jobs)
  job_runner.py                Runs steps as subprocesses, writes Firestore
  entrypoint.py                Flask HTTP server (Cloud Run target)
  scheduler_setup.py           CLI: job defs → Cloud Scheduler jobs

  setup/
    01_enable_apis.sh          Cloud Run, Scheduler, Artifact Registry, Secret Manager
    02_service_account.sh      Create batch-runner SA + IAM roles
    03_artifact_registry.sh    Build + push Docker image
    04_deploy_cloudrun.sh      Deploy Cloud Run service
    05_setup_scheduler.sh      Create Cloud Scheduler cron jobs
    06_secrets.sh              Push secrets to Secret Manager
    setup_all.sh               Run 01→06 in order (idempotent, use for first-time setup)
    teardown.sh                Delete Cloud Run + Scheduler (keeps Firestore data)
```

Other files created alongside this framework:

```
functions-crm/handlers/batch.py    CRM API Blueprint for the frontend
public/google-job.html             Frontend management page
gcloud-job.md                      Architecture reference doc
```

---

## First-Time Setup

### Prerequisites

- `gcloud` CLI installed and authenticated (`gcloud auth login`)
- Docker installed and running
- Firebase service account key at `config/serviceAccountKey.json`
- Environment variables set:

```bash
export GCP_PROJECT=blueboot-market
export GCP_LOCATION=us-central1
```

### How the scripts get into Cloud Run

The `Dockerfile` uses `COPY . .` to copy the entire project root into `/workspace`
inside the container. This includes all `app/*.py` pipeline scripts and the
`config/` query/country JSON files. The `job_runner.py` then calls them as:

```
python -m app.site_agent --countries NO --workers 8
```

**Secrets are NOT baked into the image.** A `.dockerignore` at the project root
excludes `config/serviceAccountKey.json` and `.env`. Instead, secrets are stored
in Secret Manager (`06_secrets.sh`) and Cloud Run injects them as env vars at
runtime. The pipeline scripts read them via `app/functions/config.py` which loads
from env vars:

| Env var | Secret Manager key | Used by |
|---|---|---|
| `FIREBASE_KEY_JSON` | `firebase-key-json` | All scripts (Firebase auth) |
| `OPENAI_API_KEY` | `openai-key` | Enrich agents (GPT calls) |
| `BING_API_KEY` | `bing-key` | site_agent, lead_agent |
| `BRAVE_API_KEY` | `brave-key` | site_contact_enrich, lead_enrich_contacts |
| `BATCH_SECRET` | `batch-secret` | entrypoint.py (auth on /run) |

### Run all steps

```bash
bash cloud_batch/setup/setup_all.sh
```

This runs steps 1–6 in sequence. Each step is idempotent — safe to re-run.

### Or run steps individually

```bash
# Step 1 — Enable GCP APIs
bash cloud_batch/setup/01_enable_apis.sh

# Step 2 — Create service account + IAM roles
bash cloud_batch/setup/02_service_account.sh
export BATCH_SA=batch-runner@blueboot-market.iam.gserviceaccount.com

# Step 3 — Store API keys in Secret Manager
bash cloud_batch/setup/06_secrets.sh
# (prompts for OPENAI_API_KEY, BING_API_KEY; generates BATCH_SECRET)

# Step 4 — Build and push Docker image
bash cloud_batch/setup/03_artifact_registry.sh
export BATCH_IMAGE=us-central1-docker.pkg.dev/blueboot-market/batch-runner/batch-runner:latest

# Step 5 — Deploy Cloud Run service
bash cloud_batch/setup/04_deploy_cloudrun.sh
export BATCH_RUNNER_URL=https://batch-runner-xxx-uc.a.run.app

# Step 6 — Create Cloud Scheduler cron jobs
bash cloud_batch/setup/05_setup_scheduler.sh
```

### After deploy — wire up the CRM API

The CRM Cloud Function needs to know the batch-runner URL so it can trigger on-demand jobs:

```bash
gcloud functions deploy crmApi \
  --update-env-vars BATCH_RUNNER_URL=https://batch-runner-xxx-uc.a.run.app,BATCH_SECRET=<your-secret>
```

---

## Updating Secrets / Env Variables

All secrets come from `.env` at the project root. When a value changes:

**1. Edit `.env` locally** — update the value as normal.

**2. Push to Secret Manager** — re-run `06_secrets.sh` (idempotent, adds a new version):

```bash
bash cloud_batch/setup/06_secrets.sh
```

**3. Restart Cloud Run** to pick up the new version:

```bash
bash cloud_batch/setup/04_deploy_cloudrun.sh
```

Or force a restart without a full redeploy:

```bash
gcloud run services update batch-runner \
  --region us-central1 --project blueboot-market
```

Cloud Run always uses `:latest` for each secret, so the new value is active on the next container start. Running jobs are not affected mid-run.

**Rotating a single secret** without re-running the full script:

```bash
echo -n "new-value" | gcloud secrets versions add openai-key \
  --data-file=- --project blueboot-market
```

Secret Manager key → env var name mapping:

| Secret Manager key | Env var in container |
|---|---|
| `firebase-key-json` | `FIREBASE_KEY_JSON` |
| `openai-key` | `OPENAI_API_KEY` |
| `brave-key` | `BRAVE_API_KEY` |
| `bing-key` | `BING_API_KEY` |
| `github-token` | `GITHUB_TOKEN` |
| `smtp-password` | `SMTP_PASSWORD` |
| `batch-secret` | `BATCH_SECRET` |

---

## Dry-Run Options

### Option 1 — Print setup commands only (no GCP calls)

Add `DRY_RUN=1` before any setup script. The scripts print every `gcloud` command
they would run without executing any of them:

```bash
DRY_RUN=1 bash cloud_batch/setup/setup_all.sh
DRY_RUN=1 bash cloud_batch/setup/05_setup_scheduler.sh
```

`scheduler_setup.py` has its own flag:

```bash
python -m cloud_batch.scheduler_setup --dry-run \
  --runner-url https://example.com \
  --project blueboot-market
```

### Option 2 — Run the batch runner locally (hits real Firestore)

The Flask service runs without any GCP infrastructure — it just needs a Firebase credential:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=config/serviceAccountKey.json
export PORT=8081
python -m cloud_batch.entrypoint
```

Then trigger a job (with `dry_run: true` so pipeline scripts don't write to Firestore):

```bash
curl -s -X POST localhost:8081/run \
  -H "Content-Type: application/json" \
  -d '{"job":"site_pipeline","params":{"countries":"NO","campaign":"NO_test","dry_run":true}}'
```

Check status:

```bash
# List all jobs and last run
curl localhost:8081/jobs

# Poll a specific run
curl localhost:8081/status/site_pipeline/20260609_143201_a1b2c3
```

### Option 3 — Fully offline with Firebase emulator

No GCP account needed at all:

```bash
# Terminal 1 — start Firestore emulator
firebase emulators:start --only firestore

# Terminal 2 — start batch runner against the emulator
export FIRESTORE_EMULATOR_HOST=localhost:8080
export PORT=8081
python -m cloud_batch.entrypoint

# Terminal 3 — trigger a dry-run job
curl -s -X POST localhost:8081/run \
  -H "Content-Type: application/json" \
  -d '{"job":"lead_enrich_pipeline","params":{"campaign":"test","dry_run":true}}'
```

The emulator UI at `http://localhost:4000` shows all Firestore writes live.

---

## Triggering Jobs

### From the CRM frontend

Open `google-job.html` → click **Run now** on a job → fill in params → submit.
Step progress updates every 3 seconds. Click any step's **log** link to see its last 50 lines.

### Via the CRM API

```bash
# Trigger a run
curl -X POST https://.../api/crm/batch/jobs/site_pipeline/run \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"params": {"countries": "NO", "campaign": "NO_jun02"}}'
# → 202 {"status":"accepted","run_id":"20260609_143201_a1b2c3"}

# Poll for status
curl https://.../api/crm/batch/jobs/site_pipeline/runs/20260609_143201_a1b2c3 \
  -H "Authorization: Bearer $TOKEN"

# List all definitions + last run
curl https://.../api/crm/batch/jobs \
  -H "Authorization: Bearer $TOKEN"

# List run history for a job (last 20)
curl https://.../api/crm/batch/jobs/site_pipeline/runs \
  -H "Authorization: Bearer $TOKEN"
```

### Directly on the batch-runner (from a GCP service)

```bash
curl -X POST https://batch-runner-xxx-uc.a.run.app/run \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H "X-Batch-Secret: $BATCH_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"job":"lead_pipeline","params":{"countries":"FI","campaign":"FI_jun03"}}'
```

---

## Step Error Behaviour

Each step in a job definition has an `on_error` field:

| Value | Behaviour |
|---|---|
| `abort` | Stop immediately. Remaining steps are marked `skipped`. Run status = `failed`. |
| `continue` | Mark step `failed`, keep going. Run status = `failed` even if later steps pass. |

Discovery and export steps default to `abort`. Enrichment steps default to `continue`
so a failed contact enrichment doesn't block the email check and export.

---

## Redeploying After Changes

```bash
# Rebuild and push image
bash cloud_batch/setup/03_artifact_registry.sh

# Redeploy Cloud Run
bash cloud_batch/setup/04_deploy_cloudrun.sh

# Re-sync scheduler jobs (only needed if schedules changed)
bash cloud_batch/setup/05_setup_scheduler.sh
```

---

## Adding a New Pipeline

1. Create `cloud_batch/job_definitions/my_pipeline.json`:

```json
{
  "name": "my_pipeline",
  "description": "What this pipeline does",
  "schedule": null,
  "params": {
    "countries": { "type": "str", "required": true,  "help": "Country codes" },
    "dry_run":   { "type": "bool", "default": false, "help": "Preview only" }
  },
  "steps": [
    {
      "name": "step_one",
      "module": "my_script",
      "args": ["--countries", "{countries}"],
      "dry_run_flag": "--dry-run",
      "on_error": "abort"
    },
    {
      "name": "step_two",
      "module": "my_other_script",
      "args": ["--countries", "{countries}"],
      "on_error": "continue"
    }
  ]
}
```

2. Redeploy:

```bash
bash cloud_batch/setup/03_artifact_registry.sh
bash cloud_batch/setup/04_deploy_cloudrun.sh
```

3. If it has a schedule, re-run step 5:

```bash
bash cloud_batch/setup/05_setup_scheduler.sh
```

The new job appears in `google-job.html` automatically on the next page load.

---

## Teardown

Removes the Cloud Run service and all `batch-*` Cloud Scheduler jobs.
**Firestore data (`gcloud-batch-jobs/`) is preserved.**

```bash
bash cloud_batch/setup/teardown.sh
```

---

## GCP Services Used

| Service | What for |
|---|---|
| Cloud Run | Hosts the batch-runner Flask service (min-instances=1) |
| Cloud Scheduler | Fires `POST /run` on cron schedule per job definition |
| Artifact Registry | Stores the Docker image |
| Secret Manager | Stores `OPENAI_API_KEY`, `BING_API_KEY`, `BATCH_SECRET` |
| Firestore | Job definitions + run history under `gcloud-batch-jobs/` |
| IAM | `batch-runner` SA: `datastore.user`, `secretmanager.secretAccessor`, `run.invoker` |

---

## Troubleshooting

**Job starts then goes silent**
Check Cloud Run logs: `gcloud run services logs read batch-runner --region us-central1`
The background thread may have crashed. Look for a Python traceback.

**`dedup check failed — already running` but no job is active**
A previous run crashed before it could write `status: done`. Fix it directly in Firestore:
open `gcloud-batch-jobs/{job}/runs/{stuck_run_id}` and set `status` to `failed`.

**`BATCH_RUNNER_URL is not configured` in CRM API**
The `BATCH_RUNNER_URL` env var is missing from the `crmApi` Cloud Function.
Run the `gcloud functions deploy` command shown in the setup section above.

**Steps run but nothing appears in site_leads / leads**
Check if `dry_run: true` is set in the run's params — dry-run mode skips all Firestore writes.
