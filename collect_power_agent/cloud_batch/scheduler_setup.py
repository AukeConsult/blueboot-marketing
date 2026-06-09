"""cloud_batch/scheduler_setup.py — CLI to create/update Cloud Scheduler jobs.

Reads all tasks from Firestore (gcloud-batch-jobs/{job}/tasks) and creates one
Cloud Scheduler job per active task that has a non-empty 'schedule' cron field.

Scheduler job naming: batch-{job_name}-{task_id}
Scheduler body:       {"job": "<name>", "task_id": "<id>", "triggered_by": "scheduler"}

Usage
-----
    python -m cloud_batch.scheduler_setup [--project PROJECT] [--location LOCATION] [--dry-run]

Requires Application Default Credentials (gcloud auth application-default login)
so it can read tasks from Firestore.

Set BATCH_RUNNER_URL as an env var or pass --runner-url.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

# On Windows, gcloud is a .cmd file — subprocess needs shell=True to find it.
_SHELL = sys.platform == "win32"


def _scheduler_job_name(job_name: str, task_id: str) -> str:
    safe_job = job_name.replace("_", "-")
    return f"batch-{safe_job}-{task_id}"


def main(argv=None):
    p = argparse.ArgumentParser(description="Create/update Cloud Scheduler jobs for cloud_batch tasks")
    p.add_argument("--project",          default=os.getenv("GCP_PROJECT", "blueboot-market"))
    p.add_argument("--location",         default=os.getenv("GCP_LOCATION", "us-central1"))
    p.add_argument("--runner-url",       default=os.getenv("BATCH_RUNNER_URL", ""),
                   help="Cloud Run service URL, e.g. https://batch-runner-xxx-uc.a.run.app")
    p.add_argument("--service-account",  default=os.getenv("BATCH_SA", ""),
                   help="Service account email for OIDC auth on scheduler calls")
    p.add_argument("--secret",           default=os.getenv("BATCH_SECRET", ""),
                   help="Shared secret sent in scheduler request body")
    p.add_argument("--dry-run",          action="store_true", help="Print commands without running them")
    args = p.parse_args(argv)

    if not args.runner_url:
        p.error("--runner-url or BATCH_RUNNER_URL env var is required")

    # Import here so credentials are initialised lazily (only when script runs)
    from cloud_batch.job_status import list_definitions, list_tasks

    defs = list_definitions()
    print(f"Found {len(defs)} job definitions in Firestore.\n")

    total_tasks = 0
    for defn in sorted(defs, key=lambda d: d.get("name", "")):
        job_name = defn["name"]
        tasks    = list_tasks(job_name)
        active   = [t for t in tasks if t.get("schedule") and t.get("active", True)]
        if not active:
            print(f"  {job_name}: no active scheduled tasks — skipping")
            continue

        print(f"  {job_name}: {len(active)} task(s) to schedule")
        for task in active:
            task_id    = task["task_id"]
            task_name  = task.get("name", task_id)
            schedule   = task["schedule"]
            sched_name = _scheduler_job_name(job_name, task_id)
            body       = json.dumps({
                "job":          job_name,
                "task_id":      task_id,
                "triggered_by": "scheduler",
                **({"secret": args.secret} if args.secret else {}),
            })

            base_cmd = ["gcloud", "scheduler", "jobs", "--project", args.project]
            target_flags = [
                "--location",         args.location,
                "--schedule",         schedule,
                "--uri",              f"{args.runner_url.rstrip('/')}/run",
                "--http-method",      "POST",
                "--message-body",     body,
                "--time-zone",        "UTC",
                "--attempt-deadline", "30m",
            ]
            if args.service_account:
                target_flags += ["--oidc-service-account-email", args.service_account]

            update_cmd = base_cmd + ["update", "http", sched_name] + target_flags
            create_cmd = base_cmd + ["create", "http", sched_name] + target_flags + [
                "--description", f"{job_name} / {task_name}"[:499],
            ]

            print(f"    [{sched_name}]  cron: {schedule}  task: {task_name}")
            total_tasks += 1

            if args.dry_run:
                print("    [dry-run]", " ".join(create_cmd))
                continue

            result = subprocess.run(update_cmd, capture_output=True, text=True, shell=_SHELL)
            if result.returncode != 0:
                print(f"    update failed ({result.stderr.strip()[:80]}), creating...")
                result = subprocess.run(create_cmd, capture_output=True, text=True, shell=_SHELL)
                if result.returncode != 0:
                    print(f"    ERROR: {result.stderr.strip()}")
                else:
                    print("    created OK")
            else:
                print("    updated OK")

    print(f"\nDone. Processed {total_tasks} task(s).")


if __name__ == "__main__":
    main()
