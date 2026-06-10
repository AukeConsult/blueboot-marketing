---
name: cloud-batch
description: >
  Use this skill when working with anything in cloud_batch/ — the long-running
  batch job orchestration framework. Triggers include: adding or editing job
  definitions (job_definitions/*.json), modifying job_runner.py or entrypoint.py,
  changing how tasks or runs are stored in Firestore (job_status.py), editing
  scheduler_sync.py, updating deploy_batch.sh or cloud_batch/setup/*.sh, debugging
  Cloud Scheduler 403 errors, fixing OIDC token / IAM permission issues, changing
  Cloud Run resource settings (CPU, memory, timeout, instances), seeding job
  definitions with seed_batch_jobs.py, or adding new pipeline steps. Also use when
  the user asks how scheduled tasks work, why a Cloud Scheduler job is firing the
  wrong URL, why background threads lose CPU, or how the sync-schedulers endpoint
  works.
---

# cloud-batch

Long-running batch job orchestration framework. Jobs run as sequences of Python
subprocesses on a Cloud Run service, track progress in Firestore, and are managed
via the CRM frontend (Batch Services → Cloud Batch).

---

## Key files

| File | Purpose |
|---|---|
| `cloud_batch/entrypoint.py` | Flask HTTP server — `/run`, `/status`, `/jobs`, `/sync-schedulers`, `/health` |
| `cloud_batch/job_runner.py` | Runs job steps as subprocesses (`python -m app.<module>`), writes Firestore |
| `cloud_batch/job_status.py` | All Firestore helpers — definitions, tasks, runs |
| `cloud_batch/scheduler_sync.py` | Syncs Firestore tasks → Cloud Scheduler jobs (create/update/delete) |
| `cloud_batch/job_definitions/*.json` | Static job schemas (params, steps) — seeded into Firestore |
| `app/seed_batch_jobs.py` | CLI to seed/force-update Firestore job definitions from JSON files |
| `deploy_batch.sh` | Day-to-day redeploy: build → deploy → env vars + IAM → seed |
| `cloud_batch/setup/` | First-time GCP setup scripts (01–06 + setup_all.sh) |

---

## Firestore layout

```
gcloud-batch-jobs/
  {job_name}                     ← job definition (schema only, no schedule)
    params:  { name: {type, required, default, help}, ... }
    steps:   [ {name, module, args, on_error, skip_if_empty}, ... ]

    tasks/                       ← one task = one Cloud Scheduler job
      {task_id}
        name:     "NO monday"
        schedule: "0 2 * * 1"   ← cron; empty = not scheduled
        active:   true
        params:   { countries: "NO", campaign: "NO_jun" }

    runs/                        ← execution history
      {run_id}
        status:       running | done | failed
        params:       { ... }
        triggered_by: scheduler | manual
        started_at / ended_at
        steps: [ {name, status, exit_code, started_at, ended_at, log_tail} ]
```

**Schedules live on tasks, not job definitions.** One job can have multiple tasks
with different schedules and params.

---

## Execution model

```
Cloud Scheduler → POST /run (with OIDC token)
                       ↓
              entrypoint.py Flask
                ├── dedup check (is_running)  → 409 if already running
                ├── create run doc in Firestore
                ├── spawn background thread → job_runner.py
                └── return 202 immediately

job_runner.py (background thread)
  for each step:
    subprocess: python -m app.{module} {args with {param} substitution}
    → update Firestore step status after each subprocess
```

**`--no-cpu-throttling` is required** — Cloud Run throttles CPU after the 202
response without it, starving background threads.

**`--concurrency` is NOT set** — `/run` returns 202 immediately so Cloud Run
never sees concurrent requests. Job concurrency is controlled by the `is_running()`
guard in `entrypoint.py`.

**Instance scale-down risk** — extra instances spun up by autoscaling may be shut
down while a background thread is still running (Cloud Run sees the request as done
after the 202). Use `BATCH_MIN_INSTANCES=N` to keep N instances alive if N jobs
run concurrently.

---

## Scheduler sync

`scheduler_sync.sync_all()` is called via `POST /sync-schedulers`. It:
1. Lists all Firestore tasks across all job definitions
2. Creates/updates Cloud Scheduler jobs for active tasks with a schedule
3. Deletes Cloud Scheduler jobs for tasks that were removed or deactivated

**All env vars are read at call time** (not import time) so changes to
`BATCH_RUNNER_URL` or `BATCH_SA` take effect on the next sync without restarting
the service.

### Required IAM on the batch-runner service account

| Role | Why |
|---|---|
| `roles/cloudscheduler.admin` | Create/update/delete Cloud Scheduler jobs |
| `roles/iam.serviceAccountUser` on itself | `actAs` required when setting OIDC token on scheduler jobs |
| `roles/run.invoker` | Allow Cloud Scheduler to call the Cloud Run service |
| `roles/datastore.user` | Read/write Firestore |
| `roles/secretmanager.secretAccessor` | Read secrets at startup |

All grants are applied by `02_service_account.sh` and `deploy_batch.sh` step 3.

### Common 403 causes

| Symptom | Fix |
|---|---|
| Cloud Scheduler → 403 PERMISSION_DENIED | SA missing `run.invoker`; run `deploy_batch.sh` |
| Sync error: `lacks iam.serviceAccounts.actAs` | SA missing `serviceAccountUser` on itself; run `02_service_account.sh` |
| Scheduler job points to wrong URL | `BATCH_RUNNER_URL` stale in service env; run sync after `deploy_batch.sh` |

---

## Cloud Run resource settings

Set in `deploy_batch.sh`, overridable via env vars:

| Env var | Default | Flag |
|---|---|---|
| `BATCH_MEMORY` | `4Gi` | `--memory` |
| `BATCH_CPU` | `4` | `--cpu` |
| `BATCH_TIMEOUT` | `3600` | `--timeout` |
| `BATCH_MIN_INSTANCES` | `1` | `--min-instances` |
| `BATCH_MAX_INSTANCES` | `3` | `--max-instances` |

`--no-cpu-throttling` is always set (not overridable — required for background threads).

---

## Job definition JSON schema

```json
{
  "name": "my_pipeline",
  "description": "What this pipeline does",
  "params": {
    "countries": { "type": "str", "required": true,  "help": "ISO country codes e.g. NO,SE" },
    "campaign":  { "type": "str", "required": false, "default": "", "help": "Campaign ID" },
    "workers":   { "type": "int", "required": false, "default": "8" }
  },
  "steps": [
    {
      "name":          "discover",
      "module":        "site_agent",
      "args":          ["--countries", "{countries}", "--workers", "{workers}"],
      "on_error":      "abort",
      "skip_if_empty": ["countries"],
      "dry_run_flag":  "--dry-run",
      "gdisk_flag":    "--gdisk"
    }
  ]
}
```

**`{param}` placeholders** in `args` are substituted at run time. If the param value
is empty, the placeholder token and its preceding `--flag` are dropped from the command.

**`skip_if_empty`** — if any listed param is empty, the whole step is skipped (not failed).

**`on_error`** — `"abort"` stops all remaining steps; `"continue"` marks step failed but keeps going.

---

## Seeding job definitions

```bash
python app/seed_batch_jobs.py              # skip existing docs (safe default)
python app/seed_batch_jobs.py --force      # overwrite all (fixes stale required flags etc.)
python app/seed_batch_jobs.py --dry-run    # preview without writing
```

**The Cloud Run service never overwrites existing Firestore job docs on restart** —
it only seeds if the doc is missing. Use `--force` after editing JSON files to push
the changes to Firestore.

---

## Deploy

```bash
# Redeploy (normal)
bash deploy_batch.sh

# Custom resources
BATCH_MEMORY=8Gi BATCH_CPU=8 BATCH_MIN_INSTANCES=2 bash deploy_batch.sh

# First-time setup
bash cloud_batch/setup/setup_all.sh
```

`deploy_batch.sh` step 3 sets `BATCH_RUNNER_URL` and `BATCH_SA` on the service
and grants `run.invoker` + `serviceAccountUser` — idempotent, safe to re-run.

---

## Editing rules

- **Large files** (`entrypoint.py`, `job_runner.py`, `job_status.py`) — use bash
  `python3 - << 'PY' ... PY` for edits; never Write/Edit tools directly (truncation risk).
- After any edit: `python3 -m py_compile cloud_batch/*.py && python3 -m pyflakes cloud_batch/*.py`
- `scheduler_sync.py` reads env vars inside `sync_all()`, not at module level —
  keep it this way so URL/SA changes take effect without restart.
