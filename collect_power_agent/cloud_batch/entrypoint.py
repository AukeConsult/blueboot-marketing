"""cloud_batch/entrypoint.py — Cloud Run HTTP service for the batch job runner.

Routes
------
POST /run
    Body: { "job": "site_pipeline", "task_id": "abc123", "triggered_by": "scheduler" }
       or { "job": "site_pipeline", "params": {"countries":"NO"}, "triggered_by": "manual" }
    Returns 202 immediately; runs the job in a background thread.
    Returns 409 if the same job is already running.

GET /status/<job_name>/<run_id>
    Returns the current state of a run from Firestore.

GET /jobs
    Returns all job definitions with their tasks (from Firestore).

GET /health
    Liveness probe.

Param resolution order for /run:
  1. If task_id given: load task.params from Firestore tasks/ subcollection
  2. Merge any params from request body on top (allows overrides)
  3. Apply param defaults from job definition for anything still unset
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request

# Load all job definitions from json files at startup — used only for initial seed
_DEFS_DIR  = Path(__file__).resolve().parent / "job_definitions"
_JOB_DEFS: dict[str, dict] = {}

for _p in sorted(_DEFS_DIR.glob("*.json")):
    with open(_p) as _f:
        _d = json.load(_f)
        _JOB_DEFS[_d["name"]] = _d

# Sync definitions into Firestore once at startup (seed only — not used at run time)
from cloud_batch.job_status import (
    sync_definition,
    get_definition,
    get_task,
    list_tasks,
    is_running,
    get_run,
    list_runs,
    list_definitions,
)
from cloud_batch.job_runner import run_in_background

for _d in _JOB_DEFS.values():
    try:
        # Only seed if the Firestore doc is missing — never overwrite user edits on restart.
        # To force-update: run  python app/seed_batch_jobs.py --force
        if not get_definition(_d["name"]):
            sync_definition(_d)
            print(f"[batch] seeded new job def: {_d['name']}")
        else:
            print(f"[batch] job def exists, skipping seed: {_d['name']}")
    except Exception as _e:
        print(f"[batch] warn: could not sync def {_d['name']}: {_e}")

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)

BATCH_SECRET = os.getenv("BATCH_SECRET", "")


def _make_run_id() -> str:
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    rand = str(uuid.uuid4())[:6]
    return f"{ts}_{rand}"


def _err(msg: str, code: int = 400):
    return jsonify({"status": "error", "message": msg}), code


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "jobs": list(_JOB_DEFS.keys())}), 200


@app.route("/run", methods=["POST"])
def run_job():
    body = request.get_json(silent=True) or {}

    # Optional shared-secret auth
    if BATCH_SECRET:
        provided = (request.headers.get("X-Batch-Secret") or body.get("secret", ""))
        if provided != BATCH_SECRET:
            return _err("Unauthorized", 401)

    job_name     = body.get("job", "").strip()
    task_id      = body.get("task_id", "").strip()
    params       = body.get("params") or {}
    triggered_by = body.get("triggered_by", "manual")

    if not job_name:
        return _err("'job' is required")

    # Read job definition live from Firestore
    try:
        defn = get_definition(job_name)
    except Exception as e:
        return _err(f"Firestore error: {e}", 500)
    if defn is None:
        return _err(f"Unknown job '{job_name}'")

    # If task_id given, load that task's stored params as the base
    if task_id:
        try:
            task = get_task(job_name, task_id)
        except Exception as e:
            return _err(f"Firestore error loading task: {e}", 500)
        if task is None:
            return _err(f"Task '{task_id}' not found for job '{job_name}'")
        # Task params are the base; body params override (e.g. for debugging)
        params = {**task.get("params", {}), **params}
    elif triggered_by == "scheduler":
        # Legacy fallback: job-level scheduled_params (pre-task model)
        scheduled = defn.get("scheduled_params") or {}
        params = {**scheduled, **params}

    # Validate required params
    for pname, pdef in defn.get("params", {}).items():
        if pdef.get("required") and not params.get(pname):
            return _err(f"Required param '{pname}' is missing or empty")

    # Apply defaults for params not yet set
    for pname, pdef in defn.get("params", {}).items():
        if pname not in params and "default" in pdef:
            params[pname] = pdef["default"]

    # Dedup guard
    try:
        if is_running(job_name):
            return _err(f"Job '{job_name}' is already running", 409)
    except Exception as e:
        return _err(f"Firestore error: {e}", 500)

    run_id = _make_run_id()
    run_in_background(defn, run_id, params, triggered_by)

    return jsonify({
        "status":       "accepted",
        "job":          job_name,
        "run_id":       run_id,
        "triggered_by": triggered_by,
        "message":      f"Run {run_id} started in background.",
    }), 202


@app.route("/status/<job_name>/<run_id>")
def run_status(job_name: str, run_id: str):
    data = get_run(job_name, run_id)
    if data is None:
        return _err("Run not found", 404)
    return jsonify(data), 200


@app.route("/jobs")
def list_jobs():
    """Return all job definitions (from Firestore) with tasks and last run summary."""
    try:
        defs = list_definitions()
    except Exception as e:
        return _err(f"Firestore error: {e}", 500)
    result = []
    for defn in sorted(defs, key=lambda d: d.get("name", "")):
        name = defn.get("name", "")
        runs  = list_runs(name, limit=1)
        tasks = list_tasks(name)
        result.append({
            "name":        name,
            "description": defn.get("description", ""),
            "params":      defn.get("params", {}),
            "step_count":  len(defn.get("steps", [])),
            "tasks":       tasks,
            "last_run":    runs[0] if runs else None,
        })
    return jsonify(result), 200


@app.route("/sync-schedulers", methods=["POST"])
def sync_schedulers():
    """Sync all Cloud Scheduler jobs from Firestore tasks.

    Called by the CRM API after a task is saved/deleted.
    Reads every active task with a schedule and creates/updates the
    corresponding Cloud Scheduler job via the google-cloud-scheduler client.
    Requires the Cloud Run service account to have roles/cloudscheduler.admin.
    """
    if BATCH_SECRET:
        body     = request.get_json(silent=True) or {}
        provided = (request.headers.get("X-Batch-Secret") or body.get("secret", ""))
        if provided != BATCH_SECRET:
            return _err("Unauthorized", 401)

    try:
        from cloud_batch.scheduler_sync import sync_all
        summary = sync_all()
    except Exception as e:
        return _err(f"Scheduler sync failed: {e}", 500)

    return jsonify({"status": "ok", "summary": summary}), 200
