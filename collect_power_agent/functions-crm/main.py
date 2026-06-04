"""
functions-crm/main.py -- CRM API with Cloud Tasks for long-running jobs.

Trigger endpoints return job_id immediately.
Cloud Tasks calls crmWorker which runs up to 60 minutes.
Poll GET /api/crm/status/<job_id> for result.

Endpoints (crmApi - short timeout):
  GET  /api/crm/contact-sync          Trigger job  ?countries=NO,UK &max=500
  GET  /api/crm/push-and-sync         Trigger job
  GET  /api/crm/template-sync         Trigger job
  GET  /api/crm/status/<job_id>       Poll result
  GET  /api/crm/jobs                  List recent jobs
  GET  /api/crm/whoami                Debug

Endpoints (crmWorker - 60 min timeout, called by Cloud Tasks):
  POST /api/crm/worker/<name>/<job_id>

Deploy:
  firebase deploy --only functions:crm

One-time GCP setup:
  run setup_gcp.bat
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import json
import threading
import uuid
from datetime import datetime, timezone
from firebase_functions import https_fn, options as fn_options
from flask import Flask, request, jsonify
from flask_cors import CORS

import firebase_admin
from firebase_admin import credentials, firestore as fs

# -- Config -------------------------------------------------------------------
GCP_PROJECT     = "blueboot-market"
GCP_LOCATION    = "us-central1"
TASKS_QUEUE     = "crm-queue"
WORKER_BASE_URL = (
    "https://us-central1-blueboot-market.cloudfunctions.net"
    "/crmWorker/api/crm/worker"
)
JOBS_COLLECTION = "crm_jobs"

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


# -- Job store (Firestore) ----------------------------------------------------

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


# -- Cloud Tasks enqueue ------------------------------------------------------

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


# -- Flask app ----------------------------------------------------------------
app = Flask(__name__)
CORS(app)  # allow all origins


def _accepted(job_id: str, name: str):
    return jsonify({
        "status":  "queued",
        "job_id":  job_id,
        "name":    name,
        "poll":    f"/api/crm/status/{job_id}",
        "message": f"Job queued. Poll /api/crm/status/{job_id} for result.",
    }), 202


def _ok(message: str, **kwargs):
    return jsonify({"status": "ok", "message": message, **kwargs})


def _err(message: str, code: int = 400):
    return jsonify({"status": "error", "message": message}), code


# -- Root --------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "CRM API",
        "endpoints": [
            "GET /api/crm/contact-sync?countries=NO&max=500",
            "GET /api/crm/push-and-sync",
            "GET /api/crm/template-sync",
            "GET /api/crm/status/<job_id>",
            "GET /api/crm/jobs",
            "GET /api/crm/whoami",
        ],
        "dashboard": "https://blueboot-market.web.app/",
    })


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


# -- Trigger endpoints --------------------------------------------------------

@app.route("/api/crm/contact-sync", methods=["GET"])
def contact_sync():
    countries_raw = request.args.get("countries", "NO")
    params = {
        "countries": [c.strip().upper() for c in countries_raw.split(",") if c.strip()],
        "max_rows":  request.args.get("max", type=int),
        "status":    request.args.get("status"),
        "campaign":  request.args.get("campaign"),
        "min_pages": request.args.get("min_pages", type=int),
        "max_pages": request.args.get("max_pages", type=int),
    }
    try:
        job_id = _new_job("contact-sync", params)
        _enqueue_task("contact-sync", job_id, params)
        return _accepted(job_id, "contact-sync")
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/push-and-sync", methods=["GET"])
def push_and_sync():
    try:
        job_id = _new_job("push-and-sync", {})
        _enqueue_task("push-and-sync", job_id, {})
        return _accepted(job_id, "push-and-sync")
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/template-sync", methods=["GET"])
def template_sync():
    try:
        job_id = _new_job("template-sync", {})
        _enqueue_task("template-sync", job_id, {})
        return _accepted(job_id, "template-sync")
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/campaign-sync", methods=["GET"])
def campaign_sync():
    """Sync campaign data from contact sheet to Firestore."""
    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    try:
        job_id = _new_job("campaign-sync", {"force": force})
        _enqueue_task("campaign-sync", job_id, {"force": force})
        return _accepted(job_id, "campaign-sync")
    except Exception as exc:
        return _err(str(exc), 500)


# -- Worker endpoint ----------------------------------------------------------

@app.route("/api/crm/worker/<name>/<job_id>", methods=["POST"])
def worker(name, job_id):
    try:
        _update_job(job_id,
                    status="running",
                    started_at=datetime.now(timezone.utc).isoformat())
        body = request.get_json(silent=True) or {}
        db   = _get_db()
        svc  = _sheets_service()

        if name == "contact-sync":
            from crm.contact_sync_lib import run_contact_sync
            added  = run_contact_sync(
                db=db, svc=svc,
                countries=body.get("countries", ["NO"]),
                status=body.get("status"),
                campaign=body.get("campaign"),
                max_rows=body.get("max_rows"),
                min_pages=body.get("min_pages"),
                max_pages=body.get("max_pages"),
            )
            result = {"added": added, "countries": body.get("countries", ["NO"])}

        elif name == "push-and-sync":
            from crm.push_and_sync_lib import run_push_and_sync
            result = run_push_and_sync(db=db, svc=svc)

        elif name == "template-sync":
            from crm.crm_template_sync_lib import run_template_sync
            count  = run_template_sync(db=db, svc=svc)
            result = {"synced": count}

        elif name == "campaign-sync":
            from crm.campaign_sync_lib import run_campaign_sync
            result = run_campaign_sync(db=db, svc=svc,
                                       force=body.get("force", False))

        else:
            _update_job(job_id, status="error",
                        error=f"Unknown job: {name}",
                        finished_at=datetime.now(timezone.utc).isoformat())
            return _err(f"Unknown job: {name}", 400)

        _update_job(job_id,
                    status="done",
                    result=result,
                    finished_at=datetime.now(timezone.utc).isoformat())
        return _ok(f"Job {job_id} done", **result)

    except Exception as exc:
        _update_job(job_id,
                    status="error",
                    error=str(exc),
                    finished_at=datetime.now(timezone.utc).isoformat())
        return _err(str(exc), 500)


# -- Status endpoints ---------------------------------------------------------

@app.route("/api/crm/status/<job_id>", methods=["GET"])
def job_status(job_id):
    doc = _jobs_col().document(job_id).get()
    if not doc.exists:
        return _err(f"Job '{job_id}' not found", 404)
    return jsonify(doc.to_dict())


@app.route("/api/crm/jobs", methods=["GET"])
def list_jobs():
    limit = min(int(request.args.get("limit", 20)), 50)
    docs = _jobs_col().order_by(
        "queued_at", direction="DESCENDING"
    ).limit(limit).stream()
    return jsonify({"jobs": [d.to_dict() for d in docs]})


# -- Cloud Function entry points ----------------------------------------------

@https_fn.on_request(region="us-central1", timeout_sec=30)
def crmApi(req: https_fn.Request) -> https_fn.Response:
    """Trigger + status endpoints — returns quickly."""
    with app.request_context(req.environ):
        try:
            return app.full_dispatch_request()
        except Exception as exc:
            return _err(str(exc), 500)


@https_fn.on_request(region="us-central1", timeout_sec=900,
                     memory=fn_options.MemoryOption.GB_1,
                     max_instances=3)
def crmWorker(req: https_fn.Request) -> https_fn.Response:
    """Worker endpoint — called by Cloud Tasks, runs up to 15 min."""
    with app.request_context(req.environ):
        try:
            return app.full_dispatch_request()
        except Exception as exc:
            return _err(str(exc), 500)
