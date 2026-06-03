"""
functions-crm/main.py -- CRM API as a Python Firebase Cloud Function (2nd gen).

All endpoints are GET. Jobs run async in a background thread and return a job_id.
Poll GET /api/crm/status/<job_id> for progress and result.

Endpoints:
  GET /api/crm/contact-sync              Export email_contacts -> contact sheet
  GET /api/crm/push-and-sync             Push selected -> CRM template + sync site_leads
  GET /api/crm/template-sync             CRM template -> Firestore + update site_leads
  GET /api/crm/status/<job_id>           Poll job status
  GET /api/crm/whoami                    Debug: show service account

Query params for contact-sync:
  ?countries=NO,UK  &max=500  &status=pending  &campaign=NO_jun

Deploy:
  firebase deploy --only functions:crm

Share both Google Sheets with: blueboot-market@appspot.gserviceaccount.com
"""
from __future__ import annotations

import threading
import uuid
import time
from datetime import datetime, timezone
from firebase_functions import https_fn
from flask import Flask, request, jsonify

import firebase_admin
from firebase_admin import credentials, firestore as fs

# -- Bootstrap ----------------------------------------------------------------
_fb_lock = threading.Lock()
_db = None


def _get_db():
    global _db
    if _db is not None:
        return _db
    with _fb_lock:
        if _db is not None:
            return _db
        if not firebase_admin._apps:
            cred = credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred, {"projectId": "blueboot-market"})
        _db = fs.client()
    return _db


def _sheets_service():
    import google.auth
    from googleapiclient.discovery import build
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
    creds, _ = google.auth.default(scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


# -- Job tracker (Firestore-backed) ------------------------------------------
JOBS_COLLECTION = "crm_jobs"


def _jobs_col():
    return _get_db().collection(JOBS_COLLECTION)


def _new_job(name: str) -> str:
    job_id = str(uuid.uuid4())[:8]
    _jobs_col().document(job_id).set({
        "id":          job_id,
        "name":        name,
        "status":      "running",
        "started_at":  datetime.now(timezone.utc).isoformat(),
        "result":      None,
        "error":       None,
        "finished_at": None,
    })
    return job_id


def _finish_job(job_id: str, result: dict):
    _jobs_col().document(job_id).update({
        "status":      "done",
        "result":      result,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    })


def _fail_job(job_id: str, error: str):
    _jobs_col().document(job_id).update({
        "status":      "error",
        "error":       error,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    })


def _run_async(job_id: str, fn, *args, **kwargs):
    def _worker():
        try:
            result = fn(*args, **kwargs)
            _finish_job(job_id, result)
        except Exception as exc:
            _fail_job(job_id, str(exc))
    t = threading.Thread(target=_worker, daemon=True)
    t.start()


# -- Flask app ----------------------------------------------------------------
app = Flask(__name__)


def _accepted(job_id: str, name: str):
    return jsonify({
        "status":   "accepted",
        "job_id":   job_id,
        "name":     name,
        "poll":     f"/api/crm/status/{job_id}",
        "message":  f"Job '{name}' started. Poll /api/crm/status/{job_id} for result.",
    }), 202


def _err(message: str, code: int = 400):
    return jsonify({"status": "error", "message": message}), code


# -- Debug --------------------------------------------------------------------

@app.route("/api/crm/whoami", methods=["GET"])
def whoami():
    try:
        import google.auth
        creds, project = google.auth.default()
        return jsonify({
            "status":          "ok",
            "project":         project,
            "service_account": getattr(creds, "service_account_email", str(type(creds))),
        })
    except Exception as exc:
        return _err(str(exc), 500)


# -- Status ----------------------------------------------------------------