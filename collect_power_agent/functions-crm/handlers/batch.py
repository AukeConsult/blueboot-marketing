"""handlers/batch.py — CRM API endpoints for cloud_batch job management.

Routes
------
GET  /api/crm/batch/jobs                                 List all job definitions + tasks
GET  /api/crm/batch/jobs/<job>/runs                      List recent runs
GET  /api/crm/batch/jobs/<job>/runs/<run_id>             Get single run (polling)
POST /api/crm/batch/jobs/<job>/run                       Ad-hoc run with custom params
PATCH /api/crm/batch/jobs/<job>                          Update job definition (desc, params schema, steps)

GET    /api/crm/batch/jobs/<job>/tasks                   List tasks for a job
POST   /api/crm/batch/jobs/<job>/tasks                   Create a task
PATCH  /api/crm/batch/jobs/<job>/tasks/<task_id>         Update a task
DELETE /api/crm/batch/jobs/<job>/tasks/<task_id>         Delete a task
POST   /api/crm/batch/jobs/<job>/tasks/<task_id>/run     Run a specific task (uses stored params)
"""
from __future__ import annotations

import os

import requests
from flask import Blueprint, jsonify, request

from handlers.shared import _err, _get_db

bp = Blueprint("batch", __name__)

BATCH_COLLECTION = "gcloud-batch-jobs"
BATCH_RUNNER_URL = os.getenv("BATCH_RUNNER_URL", "").rstrip("/")
BATCH_SECRET     = os.getenv("BATCH_SECRET", "")


def _runner_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if BATCH_SECRET:
        headers["X-Batch-Secret"] = BATCH_SECRET
    try:
        import google.auth.transport.requests as gtr
        import google.oauth2.id_token as id_token
        audience = BATCH_RUNNER_URL
        auth_req = gtr.Request()
        token    = id_token.fetch_id_token(auth_req, audience)
        headers["Authorization"] = f"Bearer {token}"
    except Exception:
        pass
    return headers


def _job_ref(job_name: str):
    return _get_db().collection(BATCH_COLLECTION).document(job_name)


def _task_ref(job_name: str, task_id: str):
    return _job_ref(job_name).collection("tasks").document(task_id)


# ── Job routes ────────────────────────────────────────────────────────────────

@bp.route("/api/crm/batch/jobs", methods=["GET"])
def list_jobs():
    """List all job definitions with their tasks and last run summary."""
    try:
        db   = _get_db()
        docs = db.collection(BATCH_COLLECTION).stream()
        jobs = []
        for doc in docs:
            if not doc.exists:
                continue
            d = doc.to_dict()

            # Fetch tasks
            task_docs = doc.reference.collection("tasks").order_by("created_at").stream()
            d["tasks"] = [t.to_dict() for t in task_docs if t.exists]

            # Fetch last run
            runs = (
                doc.reference.collection("runs")
                .order_by("started_at", direction="DESCENDING")
                .limit(1)
                .stream()
            )
            run_list   = [r.to_dict() for r in runs if r.exists]
            d["last_run"] = run_list[0] if run_list else None

            jobs.append(d)
        jobs.sort(key=lambda x: x.get("name", ""))
        return jsonify({"status": "ok", "jobs": jobs}), 200
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/batch/jobs/<job_name>", methods=["PATCH"])
def update_job(job_name: str):
    """Update job definition fields: description, params (schema), steps.

    schedule and scheduled_params are intentionally excluded — they live on tasks now.
    """
    body = request.get_json(silent=True) or {}

    if "name" in body and body["name"] != job_name:
        return _err("Cannot change job name — it is the document ID.", 400)

    allowed = {"description", "params", "steps"}
    update  = {k: v for k, v in body.items() if k in allowed}

    if not update:
        return _err("No valid fields to update. Allowed: description, params, steps.", 400)

    try:
        ref = _job_ref(job_name)
        if not ref.get().exists:
            return _err(f"Job '{job_name}' not found.", 404)
        ref.update(update)
        doc = ref.get().to_dict()
        return jsonify({"status": "ok", "job": doc}), 200
    except Exception as exc:
        return _err(str(exc), 500)


# ── Run routes ────────────────────────────────────────────────────────────────

@bp.route("/api/crm/batch/jobs/<job_name>/runs", methods=["GET"])
def list_runs(job_name: str):
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
    """Ad-hoc run with caller-supplied params."""
    if not BATCH_RUNNER_URL:
        return _err("BATCH_RUNNER_URL is not configured on the server", 503)

    body   = request.get_json(silent=True) or {}
    params = body.get("params", {})

    try:
        resp = requests.post(
            f"{BATCH_RUNNER_URL}/run",
            json={"job": job_name, "params": params, "triggered_by": "manual"},
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


# ── Task routes ────────────────────────────────────────────────────────────────

@bp.route("/api/crm/batch/jobs/<job_name>/tasks", methods=["GET"])
def list_tasks(job_name: str):
    """List tasks for a job."""
    try:
        ref   = _job_ref(job_name)
        if not ref.get().exists:
            return _err(f"Job '{job_name}' not found.", 404)
        task_docs = ref.collection("tasks").order_by("created_at").stream()
        tasks     = [t.to_dict() for t in task_docs if t.exists]
        return jsonify({"status": "ok", "job": job_name, "tasks": tasks}), 200
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/batch/jobs/<job_name>/tasks", methods=["POST"])
def create_task(job_name: str):
    """Create a new task for a job."""
    body = request.get_json(silent=True) or {}

    name     = (body.get("name") or "").strip()
    schedule = (body.get("schedule") or "").strip()
    params   = body.get("params") or {}
    active   = body.get("active", True)

    if not name:
        return _err("'name' is required.", 400)

    try:
        ref = _job_ref(job_name)
        if not ref.get().exists:
            return _err(f"Job '{job_name}' not found.", 404)

        import uuid as _uuid
        from datetime import datetime, timezone
        task_id = str(_uuid.uuid4())[:8]
        now     = datetime.now(timezone.utc).isoformat()
        task    = {
            "task_id":    task_id,
            "job":        job_name,
            "name":       name,
            "schedule":   schedule,
            "params":     params,
            "active":     bool(active),
            "created_at": now,
            "updated_at": now,
        }
        ref.collection("tasks").document(task_id).set(task)
        return jsonify({"status": "ok", "task": task}), 201
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/batch/jobs/<job_name>/tasks/<task_id>", methods=["PATCH"])
def update_task(job_name: str, task_id: str):
    """Update a task's name, schedule, params, or active flag."""
    body = request.get_json(silent=True) or {}

    allowed = {"name", "schedule", "params", "active"}
    update  = {k: v for k, v in body.items() if k in allowed}
    if not update:
        return _err("No valid fields. Allowed: name, schedule, params, active.", 400)

    try:
        from datetime import datetime, timezone
        ref = _task_ref(job_name, task_id)
        if not ref.get().exists:
            return _err("Task not found.", 404)
        update["updated_at"] = datetime.now(timezone.utc).isoformat()
        ref.update(update)
        return jsonify({"status": "ok", "task": ref.get().to_dict()}), 200
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/batch/jobs/<job_name>/tasks/<task_id>", methods=["DELETE"])
def delete_task(job_name: str, task_id: str):
    """Delete a task."""
    try:
        ref = _task_ref(job_name, task_id)
        if not ref.get().exists:
            return _err("Task not found.", 404)
        ref.delete()
        return jsonify({"status": "ok", "deleted": task_id}), 200
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/batch/jobs/<job_name>/tasks/<task_id>/run", methods=["POST"])
def run_task(job_name: str, task_id: str):
    """Trigger a specific task (uses the task's stored params)."""
    if not BATCH_RUNNER_URL:
        return _err("BATCH_RUNNER_URL is not configured on the server", 503)

    try:
        resp = requests.post(
            f"{BATCH_RUNNER_URL}/run",
            json={
                "job":          job_name,
                "task_id":      task_id,
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


@bp.route("/api/crm/batch/sync-schedulers", methods=["POST"])
def sync_schedulers():
    """Trigger a Cloud Scheduler sync on the batch-runner service.

    Called automatically by the frontend after saving or deleting a task.
    The batch-runner reads all active tasks from Firestore and creates/updates
    the corresponding Cloud Scheduler jobs via the REST API.

    Requires the batch-runner service account to have roles/cloudscheduler.admin.
    """
    if not BATCH_RUNNER_URL:
        return _err("BATCH_RUNNER_URL is not configured on the server", 503)

    try:
        resp = requests.post(
            f"{BATCH_RUNNER_URL}/sync-schedulers",
            json={},
            headers=_runner_headers(),
            timeout=60,   # sync can take a few seconds per task
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

    if resp.status_code not in (200, 202):
        return _err(data.get("message", f"Runner returned {resp.status_code}"), resp.status_code)

    return jsonify({"status": "ok", **data}), 200
