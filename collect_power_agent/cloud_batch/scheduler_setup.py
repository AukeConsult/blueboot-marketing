"""cloud_batch/scheduler_setup.py — CLI to create/update Cloud Scheduler jobs.

Reads all job_definitions/*.json files and creates one Cloud Scheduler job
per definition that has a non-null 'schedule' field.

Usage
-----
    python -m cloud_batch.scheduler_setup [--project PROJECT] [--location LOCATION] [--dry-run]

Each scheduler job calls:
    POST https://<BATCH_RUNNER_URL>/run
    Body: {"job": "<name>", "params": <default_params>, "triggered_by": "scheduler"}

Set BATCH_RUNNER_URL as an env var or pass --runner-url.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# On Windows, gcloud is a .cmd file — subprocess needs shell=True to find it.
_SHELL = sys.platform == "win32"

DEFS_DIR = Path(__file__).resolve().parent / "job_definitions"


def _build_default_params(defn: dict) -> dict:
    """Build a params dict with all default values (required ones left empty)."""
    return {
        name: pdef.get("default", "")
        for name, pdef in defn.get("params", {}).items()
    }


def _scheduler_job_name(defn_name: str) -> str:
    return f"batch-{defn_name.replace('_', '-')}"


def main(argv=None):
    p = argparse.ArgumentParser(description="Create/update Cloud Scheduler jobs for cloud_batch")
    p.add_argument("--project",     default=os.getenv("GCP_PROJECT", "blueboot-market"))
    p.add_argument("--location",    default=os.getenv("GCP_LOCATION", "us-central1"))
    p.add_argument("--runner-url",  default=os.getenv("BATCH_RUNNER_URL", ""),
                   help="Cloud Run service URL, e.g. https://batch-runner-xxx-uc.a.run.app")
    p.add_argument("--service-account", default=os.getenv("BATCH_SA", ""),
                   help="Service account email for OIDC auth on scheduler calls")
    p.add_argument("--secret",      default=os.getenv("BATCH_SECRET", ""),
                   help="X-Batch-Secret header value")
    p.add_argument("--dry-run",     action="store_true", help="Print commands without running them")
    args = p.parse_args(argv)

    if not args.runner_url:
        p.error("--runner-url or BATCH_RUNNER_URL env var is required")

    defs = []
    for path in sorted(DEFS_DIR.glob("*.json")):
        with open(path) as f:
            defs.append(json.load(f))

    scheduled = [d for d in defs if d.get("schedule")]
    print(f"Found {len(defs)} job definitions, {len(scheduled)} with schedules.\n")

    for defn in scheduled:
        sched_name    = _scheduler_job_name(defn["name"])
        # Include secret in body so we avoid --headers quoting issues on Windows
        body          = json.dumps({
            "job":          defn["name"],
            "params":       _build_default_params(defn),
            "triggered_by": "scheduler",
            **({"secret": args.secret} if args.secret else {}),
        })

        # Try update first, then create
        base_cmd = [
            "gcloud", "scheduler", "jobs",
            "--project", args.project,
        ]
        target_flags = [
            "--location",        args.location,
            "--schedule",        defn["schedule"],
            "--uri",             f"{args.runner_url.rstrip('/')}/run",
            "--http-method",     "POST",
            "--message-body",    body,
            "--time-zone",       "UTC",
            "--attempt-deadline","30m",
        ]
        if args.service_account:
            target_flags += ["--oidc-service-account-email", args.service_account]

        update_cmd = base_cmd + ["update", "http", sched_name] + target_flags
        create_cmd = base_cmd + ["create", "http", sched_name] + target_flags + [
            "--description", defn.get("description", "")[:499],
        ]

        print(f"  Scheduler job: {sched_name}  cron: {defn['schedule']}")
        if args.dry_run:
            print("  [dry-run] would run:", " ".join(create_cmd))
            continue

        # Try update; if it fails (job doesn't exist), create
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

    print("\nDone.")


if __name__ == "__main__":
    main()
