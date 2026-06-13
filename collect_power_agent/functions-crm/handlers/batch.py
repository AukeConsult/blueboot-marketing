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
POST   /api/crm/batch/sync-schedulers                    Sync Cloud Scheduler jobs from Firestore tasks
"""
from __future__ import annotations

import logging
import os
import traceback

import requests
from flask import Blueprint, jsonify, request

from handlers.shared import _err, _get_db

bp = Blueprint("batch", __name__)

BATCH_COLLECTION = "gcloud-batch-jobs"
BATCH_RUNNER_URL = os.getenv("BATCH_RUNNER_URL", "").rstrip("/")
BATCH_SECRET     = os.getenv("BATCH_SECRET", "")

logger = logging.getLogger(__name__)


# ── Logging helpers ───────────────────────────────────────────────────────────

def _log_request(**extra) -> None:
    """Log incoming request: method, path, query args, JSON body, plus any extras."""
    body = {}
    try:
        body = request.get_json(silent=True) or {}
    except Exception:
        pass
    parts = {
        "method":  request.method,
        "path":    request.path,
    }
    if request.args:
        parts["query"] = dict(request.args)
    if body:
        parts["body"] = body
    if extra:
        parts.update(extra)
    logger.info("[batch] %s", parts)


def _log_error(endpoint: str, exc: Exception) -> None:
    """Log an exception with full traceback."""
    logger.error(
        "[batch] %s error: %s\n%s",
        endpoint, exc, traceback.format_exc(),
    )


# ── Auth / runner helpers ─────────────────────────────────────────────────────

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
    _log_request()
    try:
        db   = _get_db()
        docs = db.collection(BATCH_COLLECTION).stream()
        jobs = []
        for doc in docs:
            if not doc.exists:
                continue
            d = doc.to_dict()
            task_docs = doc.reference.collection("tasks").order_by("created_at").stream()
            d["tasks"] = [t.to_dict() for t in task_docs if t.exists]
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
        logger.info("[batch] list_jobs returned %d jobs", len(jobs))
        return jsonify({"status": "ok", "jobs": jobs}), 200
    except Exception as exc:
        _log_error("list_jobs", exc)
        return _err(str(exc), 500)


@bp.route("/api/crm/batch/jobs/<job_name>", methods=["PATCH"])
def update_job(job_name: str):
    """Update job definition fields: description, params (schema), steps."""
    body = request.get_json(silent=True) or {}
    _log_request(job=job_name, fields=list(body.keys()))

    if "name" in body and body["name"] != job_name:
        return _err("Cannot change job name — it is the document ID.", 400)

    allowed = {"description", "params", "steps"}
    update  = {k: v for k, v in body.items() if k in allowed}

    if not update:
        return _err("No valid fields to update. Allowed: description, params, steps.", 400)

    try:
        ref = _job_ref(job_name)
        if not ref.get().exists:
            logger.warning("[batch] update_job: job '%s' not found", job_name)
            return _err(f"Job '{job_name}' not found.", 404)
        ref.update(update)
        doc = ref.get().to_dict()
        logger.info("[batch] update_job '%s' updated fields: %s", job_name, list(update.keys()))
        return jsonify({"status": "ok", "job": doc}), 200
    except Exception as exc:
        _log_error(f"update_job({job_name})", exc)
        return _err(str(exc), 500)


# ── Run routes ────────────────────────────────────────────────────────────────

@bp.route("/api/crm/batch/jobs/<job_name>/runs", methods=["GET"])
def list_runs(job_name: str):
    _log_request(job=job_name)
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
        result = [r.to_dict() for r in runs if r.exists]
        logger.info("[batch] list_runs '%s' returned %d runs", job_name, len(result))
        return jsonify({"status": "ok", "job": job_name, "runs": result}), 200
    except Exception as exc:
        _log_error(f"list_runs({job_name})", exc)
        return _err(str(exc), 500)


@bp.route("/api/crm/batch/jobs/<job_name>/runs/<run_id>", methods=["GET"])
def get_run(job_name: str, run_id: str):
    _log_request(job=job_name, run_id=run_id)
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
            logger.warning("[batch] get_run: run '%s/%s' not found", job_name, run_id)
            return _err("Run not found", 404)
        return jsonify({"status": "ok", "run": doc.to_dict()}), 200
    except Exception as exc:
        _log_error(f"get_run({job_name}/{run_id})", exc)
        return _err(str(exc), 500)


@bp.route("/api/crm/batch/jobs/<job_name>/run", methods=["POST"])
def trigger_run(job_name: str):
    """Ad-hoc run with caller-supplied params."""
    body   = request.get_json(silent=True) or {}
    params = body.get("params", {})
    _log_request(job=job_name, params=params)

    if not BATCH_RUNNER_URL:
        logger.error("[batch] trigger_run: BATCH_RUNNER_URL not configured")
        return _err("BATCH_RUNNER_URL is not configured on the server", 503)

    try:
        resp = requests.post(
            f"{BATCH_RUNNER_URL}/run",
            json={"job": job_name, "params": params, "triggered_by": "manual"},
            headers=_runner_headers(),
            timeout=15,
        )
    except requests.Timeout:
        logger.error("[batch] trigger_run('%s'): runner timeout", job_name)
        return _err("Batch runner did not respond in time", 504)
    except requests.ConnectionError as exc:
        _log_error(f"trigger_run({job_name}) connection", exc)
        return _err(f"Could not reach batch runner: {exc}", 503)
    except Exception as exc:
        _log_error(f"trigger_run({job_name})", exc)
        return _err(str(exc), 500)

    data = {}
    try:
        data = resp.json()
    except Exception:
        pass

    logger.info("[batch] trigger_run '%s' → runner status %d", job_name, resp.status_code)
    if resp.status_code == 409:
        return jsonify({"status": "conflict", **data}), 409
    if resp.status_code not in (200, 202):
        logger.error("[batch] trigger_run '%s' runner error: %s", job_name, data)
        return _err(data.get("message", f"Runner returned {resp.status_code}"), resp.status_code)

    return jsonify({"status": "accepted", **data}), 202


# ── Task routes ────────────────────────────────────────────────────────────────

@bp.route("/api/crm/batch/jobs/<job_name>/tasks", methods=["GET"])
def list_tasks(job_name: str):
    """List tasks for a job."""
    _log_request(job=job_name)
    try:
        ref = _job_ref(job_name)
        if not ref.get().exists:
            logger.warning("[batch] list_tasks: job '%s' not found", job_name)
            return _err(f"Job '{job_name}' not found.", 404)
        task_docs = ref.collection("tasks").order_by("created_at").stream()
        tasks     = [t.to_dict() for t in task_docs if t.exists]
        logger.info("[batch] list_tasks '%s' returned %d tasks", job_name, len(tasks))
        return jsonify({"status": "ok", "job": job_name, "tasks": tasks}), 200
    except Exception as exc:
        _log_error(f"list_tasks({job_name})", exc)
        return _err(str(exc), 500)


@bp.route("/api/crm/batch/jobs/<job_name>/tasks", methods=["POST"])
def create_task(job_name: str):
    """Create a new task for a job."""
    body     = request.get_json(silent=True) or {}
    name     = (body.get("name") or "").strip()
    schedule = (body.get("schedule") or "").strip()
    params   = body.get("params") or {}
    active   = body.get("active", True)
    _log_request(job=job_name, name=name, schedule=schedule, params=params, active=active)

    if not name:
        return _err("'name' is required.", 400)

    try:
        ref = _job_ref(job_name)
        if not ref.get().exists:
            logger.warning("[batch] create_task: job '%s' not found", job_name)
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
        logger.info("[batch] create_task '%s/%s' (name=%s, schedule=%s)", job_name, task_id, name, schedule)
        return jsonify({"status": "ok", "task": task}), 201
    except Exception as exc:
        _log_error(f"create_task({job_name})", exc)
        return _err(str(exc), 500)


@bp.route("/api/crm/batch/jobs/<job_name>/tasks/<task_id>", methods=["PATCH"])
def update_task(job_name: str, task_id: str):
    """Update a task's name, schedule, params, or active flag."""
    body    = request.get_json(silent=True) or {}
    allowed = {"name", "schedule", "params", "active"}
    update  = {k: v for k, v in body.items() if k in allowed}
    _log_request(job=job_name, task_id=task_id, update=update)

    if not update:
        return _err("No valid fields. Allowed: name, schedule, params, active.", 400)

    try:
        from datetime import datetime, timezone
        ref = _task_ref(job_name, task_id)
        if not ref.get().exists:
            logger.warning("[batch] update_task: task '%s/%s' not found", job_name, task_id)
            return _err("Task not found.", 404)
        update["updated_at"] = datetime.now(timezone.utc).isoformat()
        ref.update(update)
        doc = ref.get().to_dict()
        logger.info("[batch] update_task '%s/%s' updated: %s", job_name, task_id, list(update.keys()))
        return jsonify({"status": "ok", "task": doc}), 200
    except Exception as exc:
        _log_error(f"update_task({job_name}/{task_id})", exc)
        return _err(str(exc), 500)


@bp.route("/api/crm/batch/jobs/<job_name>/tasks/<task_id>", methods=["DELETE"])
def delete_task(job_name: str, task_id: str):
    """Delete a task."""
    _log_request(job=job_name, task_id=task_id)
    try:
        ref = _task_ref(job_name, task_id)
        if not ref.get().exists:
            logger.warning("[batch] delete_task: task '%s/%s' not found", job_name, task_id)
            return _err("Task not found.", 404)
        ref.delete()
        logger.info("[batch] delete_task '%s/%s' deleted", job_name, task_id)
        return jsonify({"status": "ok", "deleted": task_id}), 200
    except Exception as exc:
        _log_error(f"delete_task({job_name}/{task_id})", exc)
        return _err(str(exc), 500)


@bp.route("/api/crm/batch/jobs/<job_name>/tasks/<task_id>/run", methods=["POST"])
def run_task(job_name: str, task_id: str):
    """Trigger a specific task (uses the task's stored params)."""
    _log_request(job=job_name, task_id=task_id)

    if not BATCH_RUNNER_URL:
        logger.error("[batch] run_task: BATCH_RUNNER_URL not configured")
        return _err("BATCH_RUNNER_URL is not configured on the server", 503)

    try:
        resp = requests.post(
            f"{BATCH_RUNNER_URL}/run",
            json={"job": job_name, "task_id": task_id, "triggered_by": "manual"},
            headers=_runner_headers(),
            timeout=15,
        )
    except requests.Timeout:
        logger.error("[batch] run_task('%s/%s'): runner timeout", job_name, task_id)
        return _err("Batch runner did not respond in time", 504)
    except requests.ConnectionError as exc:
        _log_error(f"run_task({job_name}/{task_id}) connection", exc)
        return _err(f"Could not reach batch runner: {exc}", 503)
    except Exception as exc:
        _log_error(f"run_task({job_name}/{task_id})", exc)
        return _err(str(exc), 500)

    data = {}
    try:
        data = resp.json()
    except Exception:
        pass

    logger.info("[batch] run_task '%s/%s' → runner status %d", job_name, task_id, resp.status_code)
    if resp.status_code == 409:
        return jsonify({"status": "conflict", **data}), 409
    if resp.status_code not in (200, 202):
        logger.error("[batch] run_task '%s/%s' runner error: %s", job_name, task_id, data)
        return _err(data.get("message", f"Runner returned {resp.status_code}"), resp.status_code)

    return jsonify({"status": "accepted", **data}), 202


@bp.route("/api/crm/batch/sync-schedulers", methods=["POST"])
def sync_schedulers():
    """Trigger a Cloud Scheduler sync on the batch-runner service."""
    _log_request()

    if not BATCH_RUNNER_URL:
        logger.error("[batch] sync_schedulers: BATCH_RUNNER_URL not configured")
        return _err("BATCH_RUNNER_URL is not configured on the server", 503)

    try:
        resp = requests.post(
            f"{BATCH_RUNNER_URL}/sync-schedulers",
            json={},
            headers=_runner_headers(),
            timeout=60,
        )
    except requests.Timeout:
        logger.error("[batch] sync_schedulers: runner timeout")
        return _err("Batch runner did not respond in time", 504)
    except requests.ConnectionError as exc:
        _log_error("sync_schedulers connection", exc)
        return _err(f"Could not reach batch runner: {exc}", 503)
    except Exception as exc:
        _log_error("sync_schedulers", exc)
        return _err(str(exc), 500)

    data = {}
    try:
        data = resp.json()
    except Exception:
        pass

    logger.info("[batch] sync_schedulers → runner status %d, summary=%s",
                resp.status_code, data.get("summary"))
    if resp.status_code not in (200, 202):
        logger.error("[batch] sync_schedulers runner error: %s", data)
        return _err(data.get("message", f"Runner returned {resp.status_code}"), resp.status_code)

    return jsonify({"status": "ok", **data}), 200
