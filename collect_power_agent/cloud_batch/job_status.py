"""cloud_batch/job_status.py — Firestore helpers for gcloud-batch-jobs collection."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

import firebase_admin
from firebase_admin import credentials, firestore as fs

# ── Constants ─────────────────────────────────────────────────────────────────

BATCH_COLLECTION = "gcloud-batch-jobs"

# ── Firestore singleton ───────────────────────────────────────────────────────

_lock = threading.Lock()
_db   = None


def get_db():
    global _db
    if _db is not None:
        return _db
    with _lock:
        if _db is not None:
            return _db
        if not firebase_admin._apps:
            cred = credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred)
        _db = fs.client()
    return _db


# ── Collection helpers ────────────────────────────────────────────────────────

def _job_doc(job_name: str):
    return get_db().collection(BATCH_COLLECTION).document(job_name)


def _run_doc(job_name: str, run_id: str):
    return _job_doc(job_name).collection("runs").document(run_id)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Job definition sync ───────────────────────────────────────────────────────

def sync_definition(defn: dict) -> None:
    """Write/update a job definition doc in gcloud-batch-jobs/{name}."""
    job_name = defn["name"]
    _job_doc(job_name).set({
        "name":        job_name,
        "description": defn.get("description", ""),
        "schedule":    defn.get("schedule"),
        "params":      defn.get("params", {}),
        "steps":       defn.get("steps", []),
        "updated_at":  now_iso(),
    }, merge=True)


def list_definitions() -> list[dict]:
    """Return all job definition docs from Firestore."""
    return [d.to_dict() for d in get_db().collection(BATCH_COLLECTION).stream() if d.exists]




def get_definition(job_name: str) -> dict | None:
    """Return the live job definition from Firestore, or None if not found."""
    doc = _job_doc(job_name).get()
    return doc.to_dict() if doc.exists else None

# ── Run lifecycle ─────────────────────────────────────────────────────────────

def create_run(job_name: str, run_id: str, params: dict, triggered_by: str, steps: list[dict]) -> None:
    """Create a new run doc with status=running."""
    _run_doc(job_name, run_id).set({
        "run_id":       run_id,
        "job":          job_name,
        "status":       "running",
        "params":       params,
        "triggered_by": triggered_by,
        "started_at":   now_iso(),
        "ended_at":     None,
        "steps":        [
            {
                "name":       s["name"],
                "status":     "pending",
                "exit_code":  None,
                "started_at": None,
                "ended_at":   None,
                "log_tail":   "",
            }
            for s in steps
        ],
    })


def update_step_start(job_name: str, run_id: str, step_index: int) -> None:
    ref  = _run_doc(job_name, run_id)
    doc  = ref.get().to_dict()
    steps = doc.get("steps", [])
    if step_index < len(steps):
        steps[step_index]["status"]     = "running"
        steps[step_index]["started_at"] = now_iso()
    ref.update({"steps": steps})


def update_step_done(
    job_name: str,
    run_id: str,
    step_index: int,
    exit_code: int,
    log_tail: str,
    status: str,          # "done" | "failed" | "skipped"
) -> None:
    ref   = _run_doc(job_name, run_id)
    doc   = ref.get().to_dict()
    steps = doc.get("steps", [])
    if step_index < len(steps):
        steps[step_index]["status"]    = status
        steps[step_index]["exit_code"] = exit_code
        steps[step_index]["ended_at"]  = now_iso()
        steps[step_index]["log_tail"]  = log_tail
    ref.update({"steps": steps})


def finish_run(job_name: str, run_id: str, status: str) -> None:
    """Mark the run as done or failed."""
    _run_doc(job_name, run_id).update({
        "status":   status,
        "ended_at": now_iso(),
    })


def is_running(job_name: str) -> bool:
    """Return True if a run with status=running exists for this job (dedup guard)."""
    runs = (
        _job_doc(job_name)
        .collection("runs")
        .where("status", "==", "running")
        .limit(1)
        .stream()
    )
    return any(True for _ in runs)


def list_runs(job_name: str, limit: int = 20) -> list[dict]:
    """Return the most recent runs for a job, newest first."""
    docs = (
        _job_doc(job_name)
        .collection("runs")
        .order_by("started_at", direction=fs.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    return [d.to_dict() for d in docs if d.exists]


def get_run(job_name: str, run_id: str) -> dict | None:
    doc = _run_doc(job_name, run_id).get()
    return doc.to_dict() if doc.exists else None

# ── Task CRUD ─────────────────────────────────────────────────────────────────

import uuid as _uuid


def _task_col(job_name: str):
    return _job_doc(job_name).collection("tasks")


def _task_ref(job_name: str, task_id: str):
    return _task_col(job_name).document(task_id)


def list_tasks(job_name: str) -> list[dict]:
    """Return all tasks for a job, sorted by created_at."""
    docs = _task_col(job_name).order_by("created_at").stream()
    return [d.to_dict() for d in docs if d.exists]


def get_task(job_name: str, task_id: str) -> dict | None:
    doc = _task_ref(job_name, task_id).get()
    return doc.to_dict() if doc.exists else None


def create_task(job_name: str, data: dict) -> dict:
    """Create a new task doc in tasks/ subcollection. Returns the created doc."""
    task_id = str(_uuid.uuid4())[:8]
    task = {
        "task_id":    task_id,
        "job":        job_name,
        "name":       data.get("name", "unnamed"),
        "schedule":   data.get("schedule", ""),
        "params":     data.get("params", {}),
        "active":     data.get("active", True),
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    _task_ref(job_name, task_id).set(task)
    return task


def update_task(job_name: str, task_id: str, data: dict) -> dict | None:
    """Update allowed task fields. Returns updated doc or None if not found."""
    ref = _task_ref(job_name, task_id)
    if not ref.get().exists:
        return None
    allowed = {"name", "schedule", "params", "active"}
    update  = {k: v for k, v in data.items() if k in allowed}
    update["updated_at"] = now_iso()
    ref.update(update)
    return ref.get().to_dict()


def delete_task(job_name: str, task_id: str) -> bool:
    """Delete a task doc. Returns True if deleted, False if not found."""
    ref = _task_ref(job_name, task_id)
    if not ref.get().exists:
        return False
    ref.delete()
    return True
