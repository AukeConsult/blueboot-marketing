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


# -- Job tracker --------------------------------------------------------------
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _new_job(name: str) -> str:
    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            "id":         job_id,
            "name":       name,
            "status":     "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "result":     None,
            "error":      None,
        }
    return job_id


def _finish_job(job_id: str, result: dict):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["status"]      = "done"
            _jobs[job_id]["result"]      = result
            _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()


def _fail_job(job_id: str, error: str):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["status"]      = "error"
            _jobs[job_id]["error"]       = error
            _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()


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


# -- Status -------------------------------------------------------------------

@app.route("/api/crm/status/<job_id>", methods=["GET"])
def job_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return _err(f"Job '{job_id}' not found", 404)
    return jsonify(job)


@app.route("/api/crm/jobs", methods=["GET"])
def list_jobs():
    with _jobs_lock:
        jobs = list(_jobs.values())
    jobs.sort(key=lambda j: j["started_at"], reverse=True)
    return jsonify({"jobs": jobs[:20]})


# -- Endpoints ----------------------------------------------------------------

@app.route("/api/crm/contact-sync", methods=["GET"])
def contact_sync():
    """Export email_contacts -> contact sheet + crm/contact_select."""
    countries_raw = request.args.get("countries", "NO")
    countries     = [c.strip().upper() for c in countries_raw.split(",") if c.strip()]
    max_rows      = request.args.get("max", type=int)
    status        = request.args.get("status")
    campaign      = request.args.get("campaign")

    from crm.contact_sync_lib import run_contact_sync

    def _run():
        added = run_contact_sync(
            db=_get_db(), svc=_sheets_service(),
            countries=countries, status=status,
            campaign=campaign, max_rows=max_rows,
        )
        return {"added": added, "countries": countries}

    job_id = _new_job("contact-sync")
    _run_async(job_id, _run)
    return _accepted(job_id, "contact-sync")


@app.route("/api/crm/push-and-sync", methods=["GET"])
def push_and_sync():
    """Push selected contacts -> CRM template + sync site_leads."""
    from crm.push_and_sync_lib import run_push_and_sync

    def _run():
        return run_push_and_sync(db=_get_db(), svc=_sheets_service())

    job_id = _new_job("push-and-sync")
    _run_async(job_id, _run)
    return _accepted(job_id, "push-and-sync")


@app.route("/api/crm/template-sync", methods=["GET"])
def template_sync():
    """Sync CRM template sheet -> Firestore + update site_leads CRM fields."""
    from crm.crm_template_sync_lib import run_template_sync

    def _run():
        count = run_template_sync(db=_get_db(), svc=_sheets_service())
        return {"synced": count}

    job_id = _new_job("template-sync")
    _run_async(job_id, _run)
    return _accepted(job_id, "template-sync")


# -- Cloud Function entry point -----------------------------------------------

@https_fn.on_request(region="us-central1", timeout_sec=540)
def crmApi(req: https_fn.Request) -> https_fn.Response:
    with app.request_context(req.environ):
        try:
            return app.full_dispatch_request()
        except Exception as exc:
            return _err(str(exc), 500)
