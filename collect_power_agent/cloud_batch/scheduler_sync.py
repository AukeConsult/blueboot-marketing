"""cloud_batch/scheduler_sync.py — Sync Cloud Scheduler jobs from Firestore tasks.

Uses the google-cloud-scheduler Python client — no gcloud CLI needed.
Called from entrypoint.py /sync-schedulers which runs inside the Cloud Run service.

Required IAM on the Cloud Run service account:
    roles/cloudscheduler.admin   (to create/update/delete scheduler jobs)

Environment variables read:
    GCP_PROJECT      — GCP project id (default: blueboot-market)
    GCP_LOCATION     — region for Cloud Scheduler (default: us-central1)
    BATCH_RUNNER_URL — Cloud Run service URL (used as HTTP target)
    BATCH_SA         — service account email for OIDC auth on scheduled calls
    BATCH_SECRET     — optional shared secret added to scheduler request body
"""
from __future__ import annotations

import json
import os

from cloud_batch.job_status import list_definitions, list_tasks

# ── Constants ─────────────────────────────────────────────────────────────────

_PROJECT  = os.getenv("GCP_PROJECT",  "blueboot-market")
_LOCATION = os.getenv("GCP_LOCATION", "us-central1")
_RUNNER   = os.getenv("BATCH_RUNNER_URL", "").rstrip("/")
_SA       = os.getenv("BATCH_SA", "")
_SECRET   = os.getenv("BATCH_SECRET", "")

_PARENT   = f"projects/{_PROJECT}/locations/{_LOCATION}"


def _sched_job_name(job_name: str, task_id: str) -> str:
    safe = job_name.replace("_", "-")
    return f"{_PARENT}/jobs/batch-{safe}-{task_id}"


def _make_http_target(job_name: str, task_id: str):
    """Build an HttpTarget proto for the given task."""
    from google.cloud import scheduler_v1

    body = json.dumps({
        "job":          job_name,
        "task_id":      task_id,
        "triggered_by": "scheduler",
        **( {"secret": _SECRET} if _SECRET else {} ),
    }).encode()

    target = scheduler_v1.HttpTarget(
        uri=f"{_RUNNER}/run",
        http_method=scheduler_v1.HttpMethod.POST,
        body=body,
        headers={"Content-Type": "application/json"},
    )

    if _SA:
        target.oidc_token = scheduler_v1.OidcToken(
            service_account_email=_SA,
            audience=_RUNNER,
        )

    return target


def sync_all() -> dict:
    """Read all tasks from Firestore and create/update Cloud Scheduler jobs.

    Returns a summary dict with counts: created, updated, skipped, errors.
    """
    from google.cloud import scheduler_v1
    from google.api_core.exceptions import NotFound

    if not _RUNNER:
        raise RuntimeError("BATCH_RUNNER_URL is not set — cannot build scheduler targets")

    client  = scheduler_v1.CloudSchedulerClient()
    summary = {"created": 0, "updated": 0, "skipped": 0, "errors": [], "jobs": []}

    defs = list_definitions()
    for defn in sorted(defs, key=lambda d: d.get("name", "")):
        job_name = defn["name"]
        tasks    = list_tasks(job_name)

        for task in tasks:
            task_id  = task.get("task_id", "")
            schedule = (task.get("schedule") or "").strip()
            active   = task.get("active", True)
            name     = task.get("name", task_id)

            # No schedule or inactive → skip (leave any existing scheduler job as-is)
            if not schedule or not active:
                summary["skipped"] += 1
                continue

            sched_name = _sched_job_name(job_name, task_id)

            job = scheduler_v1.Job(
                name=sched_name,
                description=f"{job_name} / {name}"[:499],
                http_target=_make_http_target(job_name, task_id),
                schedule=schedule,
                time_zone="UTC",
                attempt_deadline={"seconds": 1800},  # 30 minutes
            )

            try:
                # Try to update (job exists)
                client.get_job(name=sched_name)
                client.update_job(
                    job=job,
                    update_mask={"paths": ["http_target", "schedule", "time_zone",
                                           "attempt_deadline", "description"]},
                )
                summary["updated"] += 1
                summary["jobs"].append({"action": "updated", "name": sched_name, "task": name})
            except NotFound:
                # Create new
                try:
                    client.create_job(parent=_PARENT, job=job)
                    summary["created"] += 1
                    summary["jobs"].append({"action": "created", "name": sched_name, "task": name})
                except Exception as exc:
                    summary["errors"].append({"task": name, "error": str(exc)})
            except Exception as exc:
                summary["errors"].append({"task": name, "error": str(exc)})

    return summary
