"""handlers/batch.py — CRM API endpoints for cloud_batch job management.

Routes
------
GET  /api/crm/batch/jobs                List all job definitions with last-run summary
GET  /api/crm/batch/jobs/<job>/runs     List recent runs for a job
GET  /api/crm/batch/jobs/<job>/runs/<run_id>  Get a single run (for polling)
POST /api/crm/batch/jobs/<job>/run      Trigger a job on demand (calls the Cloud Run runner)
"""
from __future__ import annotations

import json
import os

import requests
from flask import Blueprint, jsonify, request

from handlers.shared import _err, _get_db

bp = Blueprint("batch", __name__)

BATCH_COLLECTION = "gcloud-batch-jobs"
BATCH_RUNNER_URL = os.getenv("BATCH_RUNNER_URL", "").rstrip("/")
BATCH_SECRET     = os.getenv("BATCH_SECRET", "")

# Internal Cloud Run to Cloud Run calls use the service URL set in env.
# The CRM handler calls POST /run on the batch-runner Cloud Run service.


def _runner_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if BATCH_SECRET:
        headers["X-Batch-Secret"] = BATCH_SECRET
    # When running on GCP, attach an OIDC token so the unauthenticated=false
    # Cloud Run service accepts the call.
    try:
        import google.auth.transport.requests as gtr
        import google.oauth2.id_token as id_token
        audience = BATCH_RUNNER_URL
        auth_req = gtr.Request()
        token    = id_token.fetch_id_token(auth_req, audience)
        headers["Authorization"] = f"Bearer {token}"
    except Exception:
        pass  # local dev — no OIDC needed
    return headers


# ── Routes ────────────────────────────────────────────────────────────────────

@bp.route("/api/crm/batch/jobs", methods=["GET"])
def list_jobs():
    """List all job definitions with their last run summary."""
    try:
        db   = _get_db()
        docs = db.collection(BATCH_COLLECTION).stream()
        jobs = []
        for doc in docs:
            if not doc.exists:
                continue
            d = doc.to_dict()
            # Fetch the most recent run
            runs = (
                doc.reference.collection("runs")
                .order_by("started_at", direction="DESCENDING")
                .limit(1)
                .stream()
            )
            run_list = [r.to_dict() for r in runs if r.exists]
            d["last_run"] = run_list[0] if run_list else None
            jobs.append(d)
        jobs.sort(key=lambda x: x.get("name", ""))
        return jsonify({"status": "ok", "jobs": jobs}), 200
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/batch/jobs/<job_name>/runs", methods=["GET"])
def list_runs(job_name: str):
    """List recent runs for a job."""
    try:
        limit = int(request.args.get("limit", 20))
        db    = _get_db()
        runs  = (
            db.collection(BATCH_COLLECTION)
            .document(job_name)
            .collection("runs")
            .order_by("started_at", direction="DESCENDING")
            .limit(limit)
            .stream()
        )
        return jsonify({
            "status": "ok",
            "job":    job_name,
            "runs":   [r.to_dict() for r in runs if r.exists],
        }), 200
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/batch/jobs/<job_name>/runs/<run_id>", methods=["GET"])
def get_run(job_name: str, run_id: str):
    """Get a single run doc (used for polling from frontend)."""
    try:
        db  = _get_db()
        doc = (
            db.collection(BATCH_COLLECTION)
            .document(job_name)
            .collection("runs")
            .document(run_id)
            .get()
        )
        if not doc.exists:
            return _err("Run not found", 404)
        return jsonify({"status": "ok", "run": doc.to_dict()}), 200
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/batch/jobs/<job_name>/run", methods=["POST"])
def trigger_run(job_name: str):
    """Trigger a job on demand. Calls the Cloud Run batch-runner service."""
    if not BATCH_RUNNER_URL:
        return _err("BATCH_RUNNER_URL is not configured on the server", 503)

    body   = request.get_json(silent=True) or {}
    params = body.get("params", {})

    try:
        resp = requests.post(
            f"{BATCH_RUNNER_URL}/run",
            json={
                "job":          job_name,
                "params":       params,
                "triggered_by": "manual",
            },
            headers=_runner_headers(),
            timeout=15,
        )
    except requests.Timeout:
        return _err("Batch runner did not respond in time", 504)
    except requests.ConnectionError as exc:
        return _err(f"Could not reach batch runner: {exc}", 503)

    data = {}
    try:
        data = resp.json()
    except Exception:
        pass

    if resp.status_code == 409:
        return jsonify({"status": "conflict", **data}), 409
    if resp.status_code not in (200, 202):
        return _err(data.get("message", f"Runner returned {resp.status_code}"), resp.status_code)

    return jsonify({"status": "accepted", **data}), 202
