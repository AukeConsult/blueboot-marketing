"""handlers/shared.py — shared infrastructure for all CRM handler Blueprints.

Every handler file imports from here. Nothing in this module imports from
any handler module (no circular deps).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, firestore as fs
from flask import jsonify

# ── Config ────────────────────────────────────────────────────────────────────

GCP_PROJECT     = os.getenv("GCP_PROJECT",     "blueboot-market")
GCP_LOCATION    = os.getenv("GCP_LOCATION",    "us-central1")
TASKS_QUEUE     = os.getenv("TASKS_QUEUE",     "crm-queue")
WORKER_BASE_URL = os.getenv(
    "WORKER_BASE_URL",
    "https://us-central1-blueboot-market.cloudfunctions.net/crmWorker/api/crm/worker",
)
JOBS_COLLECTION = os.getenv("JOBS_COLLECTION", "crm_jobs")

# ── Firestore singleton ───────────────────────────────────────────────────────

_fb_lock = threading.Lock()
_db      = None


def _get_db():
    global _db
    if _db is not None:
        return _db
    with _fb_lock:
        if _db is not None:
            return _db
        if not firebase_admin._apps:
            cred = credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred, {"projectId": GCP_PROJECT})
        _db = fs.client()
    return _db


def _sheets_service():
    import google.auth
    from googleapiclient.discovery import build
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)


# ── Job store (Firestore) ─────────────────────────────────────────────────────

def _jobs_col():
    return _get_db().collection(JOBS_COLLECTION)


def _new_job(name: str, params: dict) -> str:
    job_id = str(uuid.uuid4())[:8]
    _jobs_col().document(job_id).set({
        "id":          job_id,
        "name":        name,
        "params":      params,
        "status":      "queued",
        "queued_at":   datetime.now(timezone.utc).isoformat(),
        "started_at":  None,
        "finished_at": None,
        "result":      None,
        "error":       None,
    })
    return job_id


def _update_job(job_id: str, **kwargs):
    _jobs_col().document(job_id).update(kwargs)


# ── Cloud Tasks enqueue ───────────────────────────────────────────────────────

def _enqueue_task(name: str, job_id: str, params: dict):
    from google.cloud import tasks_v2
    client = tasks_v2.CloudTasksClient()
    queue  = client.queue_path(GCP_PROJECT, GCP_LOCATION, TASKS_QUEUE)
    url    = f"{WORKER_BASE_URL}/{name}/{job_id}"
    task   = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url":         url,
            "headers":     {"Content-Type": "application/json"},
            "body":        json.dumps(params).encode(),
            "oidc_token":  {
                "service_account_email":
                    f"{GCP_PROJECT}@appspot.gserviceaccount.com"
            },
        }
    }
    client.create_task(request={"parent": queue, "task": task})


# ── Mail account helpers ──────────────────────────────────────────────────────

def _ma_col(db):
    """Shortcut to settings/mail_accounts/accounts collection."""
    settings_ma = db.collection("settings").document("mail_accounts")
    if not settings_ma.get().exists:
        settings_ma.set({"_type": "mail_accounts"})
    return settings_ma.collection("accounts")


def _get_mail_account(db, outreach_email: str):
    """Fetch mail account settings from settings/mail_accounts/accounts/{email}."""
    key = (outreach_email or "").strip().lower()
    if not key:
        return None
    doc = (
        db.collection("settings")
        .document("mail_accounts")
        .collection("accounts")
        .document(key)
        .get()
    )
    return doc.to_dict() if doc.exists else None


# ── Google Drive ──────────────────────────────────────────────────────────────

def _gdisk():
    from crm.gdisk_interface import GdiskInterface
    return GdiskInterface.from_settings(_get_db())


# ── Role-based access control ─────────────────────────────────────────────────

# guest         = authenticated but no role assigned yet (read-only)
# user          = standard CRM user
# campaign-user = can create / manage campaigns
# admin         = full access
ROLE_LEVELS: dict[str, int] = {
    "guest":         0,
    "user":          1,
    "campaign-user": 2,
    "admin":         3,
}

_VALID_ROLES = set(ROLE_LEVELS)


def _get_user_role(db, email: str) -> str:
    """Return the user role from Firestore.
    Falls back to 'guest' when the user doc is missing or the role is unrecognised.
    """
    if not email:
        return "guest"
    try:
        doc = (
            db.collection("settings")
              .document("users")
              .collection("users")
              .document(email.strip().lower())
              .get()
        )
        if doc.exists:
            role = (doc.to_dict() or {}).get("role", "").strip()
            if role in _VALID_ROLES:
                return role
    except Exception:
        pass
    return "guest"


# ── Flask response helpers ────────────────────────────────────────────────────

def _ok(message: str, **kwargs):
    return jsonify({"status": "ok", "message": message, **kwargs})


def _err(message: str, code: int = 400):
    return jsonify({"status": "error", "message": message}), code


def _accepted(job_id: str, name: str):
    return jsonify({
        "status":  "queued",
        "job_id":  job_id,
        "name":    name,
        "poll":    f"/api/crm/status/{job_id}",
        "message": f"Job queued. Poll /api/crm/status/{job_id} for result.",
    }), 202
