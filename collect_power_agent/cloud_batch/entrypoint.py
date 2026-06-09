"""cloud_batch/entrypoint.py — Cloud Run HTTP service for the batch job runner.

Routes
------
POST /run
    Body: { "job": "site_pipeline", "params": {"countries":"NO","campaign":"NO_jun02"}, "triggered_by": "scheduler" }
    Returns 202 immediately; runs the job in a background thread.
    Returns 409 if the same job is already running.

GET /status/<job_name>/<run_id>
    Returns the current state of a run from Firestore.

GET /health
    Liveness probe.

The service must be deployed with min-instances=1 so background threads
are not evicted mid-run. Cloud Scheduler calls POST /run on its cron schedule.
The CRM API (handlers/batch.py) also calls POST /run for on-demand runs.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request

# Load all job definitions from the json files at startup
_DEFS_DIR  = Path(__file__).resolve().parent / "job_definitions"
_JOB_DEFS: dict[str, dict] = {}

for _p in sorted(_DEFS_DIR.glob("*.json")):
    with open(_p) as _f:
        _d = json.load(_f)
        _JOB_DEFS[_d["name"]] = _d

# Sync definitions into Firestore once at startup
from cloud_batch.job_status import sync_definition, is_running, get_run, list_runs
from cloud_batch.job_runner import run_in_background

for _d in _JOB_DEFS.values():
    try:
        sync_definition(_d)
    except Exception as _e:
        print(f"[batch] warn: could not sync def {_d['name']}: {_e}")

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)

BATCH_SECRET = os.getenv("BATCH_SECRET", "")   # optional shared secret for scheduler calls


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
    # Optional shared-secret auth (Cloud Scheduler adds it as a header)
    if BATCH_SECRET:
        if request.headers.get("X-Batch-Secret") != BATCH_SECRET:
            return _err("Unauthorized", 401)

    body = request.get_json(silent=True) or {}
    job_name     = body.get("job", "").strip()
    params       = body.get("params", {})
    triggered_by = body.get("triggered_by", "manual")

    if not job_name:
        return _err("'job' is required")
    if job_name not in _JOB_DEFS:
        return _err(f"Unknown job '{job_name}'. Known: {list(_JOB_DEFS)}")

    defn = _JOB_DEFS[job_name]

    # Validate required params
    for pname, pdef in defn.get("params", {}).items():
        if pdef.get("required") and not params.get(pname):
            return _err(f"Required param '{pname}' is missing")

    # Apply defaults
    for pname, pdef in defn.get("params", {}).items():
        if pname not in params and "default" in pdef:
            params[pname] = pdef["default"]

    # Dedup guard — reject if same job already running
    try:
        if is_running(job_name):
            return _err(f"Job '{job_name}' is already running", 409)
    except Exception as e:
        return _err(f"Firestore error: {e}", 500)

    run_id = _make_run_id()
    run_in_background(defn, run_id, params, triggered_by)

    return jsonify({
        "status":     "accepted",
        "job":        job_name,
        "run_id":     run_id,
        "triggered_by": triggered_by,
        "message":    f"Run {run_id} started in background.",
    }), 202


@app.route("/status/<job_name>/<run_id>")
def run_status(job_name: str, run_id: str):
    if job_name not in _JOB_DEFS:
        return _err(f"Unknown job '{job_name}'", 404)
    data = get_run(job_name, run_id)
    if data is None:
        return _err("Run not found", 404)
    return jsonify(data), 200


@app.route("/jobs")
def list_jobs():
    """Return all job definitions with their last run summary."""
    result = []
    for name, defn in _JOB_DEFS.items():
        runs = list_runs(name, limit=1)
        result.append({
            "name":        name,
            "description": defn.get("description", ""),
            "schedule":    defn.get("schedule"),
            "params":      defn.get("params", {}),
            "step_count":  len(defn.get("steps", [])),
            "last_run":    runs[0] if runs else None,
        })
    return jsonify(result), 200


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
