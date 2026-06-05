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
GCP_PROJECT     = os.getenv("GCP_PROJECT", "blueboot-market")
GCP_LOCATION    = os.getenv("GCP_LOCATION", "us-central1")
TASKS_QUEUE     = os.getenv("TASKS_QUEUE", "crm-queue")
WORKER_BASE_URL = os.getenv(
    "WORKER_BASE_URL",
    "https://us-central1-blueboot-market.cloudfunctions.net/crmWorker/api/crm/worker")
JOBS_COLLECTION = os.getenv("JOBS_COLLECTION", "crm_jobs")

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
    """Sync campaign data from contact sheet -> Firestore.
    Required: ?campaign_id=NO_jun
    Optional: ?force=true
    """
    campaign_id = request.args.get("campaign_id", "").strip()
    if not campaign_id:
        return _err("campaign_id is required e.g. ?campaign_id=NO_jun", 400)
    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    try:
        job_id = _new_job("campaign-sync", {"campaign_id": campaign_id, "force": force})
        _enqueue_task("campaign-sync", job_id, {"campaign_id": campaign_id, "force": force})
        return _accepted(job_id, "campaign-sync")
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/campaigns", methods=["GET"])
def list_campaigns():
    """List all campaigns, ordered by updated_at descending.
    Optional: ?status=draft  to filter by status
    """
    try:
        db     = _get_db()
        status = request.args.get("status", "").strip()
        col    = db.collection("campaigns")
        query  = col.order_by("updated_at", direction="DESCENDING")
        if status:
            from google.cloud.firestore_v1.base_query import FieldFilter
            query = col.where(filter=FieldFilter("status", "==", status))
        docs = list(query.stream())
        campaigns = [d.to_dict() for d in docs]
        return jsonify({"campaigns": campaigns, "count": len(campaigns)})
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/campaigns/<campaign_id>", methods=["GET"])
def get_campaign(campaign_id):
    """Get a single campaign by ID, including contacts subcollection."""
    try:
        db  = _get_db()
        doc = db.collection("campaigns").document(campaign_id).get()
        if not doc.exists:
            return _err(f"Campaign '{campaign_id}' not found", 404)
        data = doc.to_dict()
        # Include contacts subcollection
        contacts_docs = db.collection("campaigns").document(campaign_id).collection("campaign_contacts").stream()
        data["campaign_contacts"] = [c.to_dict() for c in contacts_docs]
        return jsonify(data)
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/campaigns/<campaign_id>/create", methods=["POST"])
def create_campaign(campaign_id):
    """Create a new campaign document. Fails if already exists."""
    try:
        db      = _get_db()
        doc_ref = db.collection("campaigns").document(campaign_id)
        if doc_ref.get().exists:
            return _err(f"Campaign '{campaign_id}' already exists. Use PATCH to update.", 409)
        body  = request.get_json(silent=True) or {}
        now   = datetime.now(timezone.utc).isoformat()
        data  = {
            "campaign_id":            campaign_id,
            "status":                 "draft",
            "sent_at":                None,
            "outreach_email_account": body.get("outreach_email_account", ""),
            "mail":                   {"subject": "", "body": "", "type": "plain"},
            "contact_count":          0,
            "sites_count":            0,
            "countries":              [],
            "status_breakdown":       {},
            "select_breakdown":       {},
            "tier_breakdown":         {},
            "outreach_breakdown":     {},
            "updated_at":             now,
        }
        doc_ref.set(data)
        return _ok(f"Campaign '{campaign_id}' created", campaign=data)
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/campaigns/<campaign_id>", methods=["POST", "PATCH"])
def update_campaign(campaign_id):
    """Update a campaign document.

    Body fields (all optional):
        status                  draft | dosend | sent | cancelled
        outreach_email_account  e.g. "tone@blueboot.no"
        mail.subject            email subject line
        mail.body               email body text
        sent_at                 ISO timestamp (set automatically when status=sent)

    Setting status=sent automatically sets sent_at if not provided.
    """
    try:
        db   = _get_db()
        body = request.get_json(silent=True) or {}
        if not body:
            return _err("Request body is required", 400)

        doc_ref = db.collection("campaigns").document(campaign_id)
        doc     = doc_ref.get()
        if not doc.exists:
            return _err(f"Campaign '{campaign_id}' not found", 404)

        update = {}

        if "status" in body:
            valid = {"draft", "dosend", "sent", "cancelled"}
            if body["status"] not in valid:
                return _err(f"Invalid status. Must be one of: {', '.join(sorted(valid))}", 400)
            update["status"] = body["status"]
            # Auto-set sent_at when status becomes sent
            if body["status"] == "sent" and not body.get("sent_at"):
                update["sent_at"] = datetime.now(timezone.utc).isoformat()

        if "sent_at" in body:
            update["sent_at"] = body["sent_at"]

        if "outreach_email_account" in body:
            update["outreach_email_account"] = body["outreach_email_account"]

        if "owner" in body:
            update["owner"] = body["owner"]

        if "mail" in body:
            existing_mail = (doc.to_dict() or {}).get("mail", {})
            merged_mail   = dict(existing_mail)
            merged_mail.update(body["mail"])
            # css is allowed as a mail sub-field
            update["mail"] = merged_mail

        if not update:
            return _err("No valid fields to update", 400)

        update["updated_at"] = datetime.now(timezone.utc).isoformat() if "updated_at" not in update else update["updated_at"]
        doc_ref.update(update)

        updated_doc = doc_ref.get().to_dict()
        return jsonify({"status": "ok", "message": f"Campaign '{campaign_id}' updated", "campaign": updated_doc})

    except Exception as exc:
        return _err(str(exc), 500)


# -- gdisk (Google Drive folder) ---------------------------------------------
GDISK_SETTINGS_COLLECTION = os.getenv("GDISK_SETTINGS_COLLECTION", "settings")
GDISK_SETTINGS_DOC = os.getenv("GDISK_SETTINGS_DOC", "gdisk")


def _gdisk():
    from crm.gdisk_interface import GdiskInterface
    return GdiskInterface.from_settings(_get_db())


@app.route("/api/crm/gdisk/settings", methods=["GET"])
def gdisk_get_settings():
    try:
        gd = _gdisk()
        return jsonify({"folder_id": gd.folder_id, "configured": gd.is_configured()})
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/gdisk/settings", methods=["POST", "PATCH"])
def gdisk_set_settings():
    try:
        body = request.get_json(silent=True) or {}
        folder_id = (body.get("folder_id") or "").strip()
        _get_db().collection(GDISK_SETTINGS_COLLECTION).document(GDISK_SETTINGS_DOC).set(
            {"folder_id": folder_id,
             "updated_at": datetime.now(timezone.utc).isoformat()}, merge=True)
        return _ok("gdisk folder saved", folder_id=folder_id)
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/gdisk/check", methods=["GET"])
def gdisk_check_access():
    """Report what the service account can do with the configured folder."""
    try:
        return jsonify(_gdisk().check_access())
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/gdisk/files", methods=["GET"])
def gdisk_list_files():
    try:
        gd = _gdisk()
        if not gd.is_configured():
            return _err("No gdisk folder configured. Set one in settings.", 400)
        return jsonify({"folder_id": gd.folder_id, "files": gd.list_files()})
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/gdisk/files", methods=["POST"])
def gdisk_upload_file():
    try:
        gd = _gdisk()
        if not gd.is_configured():
            return _err("No gdisk folder configured. Set one in settings.", 400)
        f = request.files.get("file")
        if f is None:
            return _err("No file uploaded (form field 'file').")
        name = f.filename or "upload.bin"
        data = f.read()
        mime = f.mimetype or "application/octet-stream"
        file_id = gd.write_bytes(name, data, mime=mime)
        return _ok(f"Uploaded {name}", name=name, file_id=file_id, bytes=len(data))
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/gdisk/files/<path:name>", methods=["GET"])
def gdisk_download_file(name):
    try:
        from flask import Response
        gd = _gdisk()
        if not gd.is_configured():
            return _err("No gdisk folder configured. Set one in settings.", 400)
        data = gd.read_bytes(name)
        if data is None:
            return _err(f"'{name}' not found in gdisk folder", 404)
        meta = gd.get_meta(name) or {}
        mime = meta.get("mimeType") or "application/octet-stream"
        return Response(
            data, mimetype=mime,
            headers={"Content-Disposition": f'attachment; filename="{name}"'})
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/gdisk/files/<path:name>", methods=["DELETE"])
def gdisk_delete_file(name):
    try:
        gd = _gdisk()
        if not gd.is_configured():
            return _err("No gdisk folder configured. Set one in settings.", 400)
        ok = gd.delete_file(name)
        if not ok:
            return _err(f"'{name}' not found", 404)
        return _ok(f"Deleted {name}", name=name)
    except Exception as exc:
        return _err(str(exc), 500)


# -- Filter facets ------------------------------------------------------------
FILTER_FACETS_COLLECTION = os.getenv("FILTER_FACETS_COLLECTION", "filter_facets")


@app.route("/api/crm/filter-facets", methods=["GET"])
def list_filter_facets():
    """List filter-facets documents (the generated catalog + saved presets)."""
    try:
        db = _get_db()
        out = []
        for d in db.collection(FILTER_FACETS_COLLECTION).stream():
            data = d.to_dict() or {}
            out.append({
                "name":              d.id,
                "source_collection": data.get("source_collection"),
                "generated_at":      data.get("generated_at"),
                "saved_at":          data.get("saved_at"),
                "lead_count":        data.get("lead_count"),
                "contact_count":     data.get("contact_count"),
            })
        out.sort(key=lambda x: x["name"])
        return jsonify({"facets": out, "count": len(out)})
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/filter-facets/<name>", methods=["GET"])
def get_filter_facets(name):
    """Return a single filter-facets document (e.g. the 'site_leads' catalog)."""
    try:
        db  = _get_db()
        doc = db.collection(FILTER_FACETS_COLLECTION).document(name).get()
        if not doc.exists:
            return _err(f"filter_facets/'{name}' not found", 404)
        return jsonify(doc.to_dict())
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/filter-facets/<name>", methods=["POST", "PATCH"])
def save_filter_facets(name):
    """Save a filter-facets document (with selections) under <name>."""
    try:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict) or "filters" not in body:
            return _err("body must be a filter-facets object containing a 'filters' key")
        db = _get_db()
        body["name"]     = name
        body["saved_at"] = datetime.now(timezone.utc).isoformat()
        db.collection(FILTER_FACETS_COLLECTION).document(name).set(body, merge=False)
        # Kick off a job to count the leads/contacts this selection matches.
        job_params = {"name": name}
        job_id = _new_job("filter-count", job_params)
        _enqueue_task("filter-count", job_id, job_params)
        return _ok(f"Saved filter_facets/'{name}'", name=name,
                   saved_at=body["saved_at"], job_id=job_id,
                   poll=f"/api/crm/status/{job_id}")
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/discover-campaigns", methods=["GET"])
def discover_campaigns():
    """Scan the contact sheet for campaign IDs. Create + sync any new ones.
    Returns lists of existing and newly discovered campaign IDs.
    """
    try:
        db  = _get_db()
        svc = _sheets_service()

        # Read campaign column from contact sheet
        from crm.sheets_config import CONTACT_SHEET_ID, CONTACT_TAB
        result = svc.spreadsheets().values().get(
            spreadsheetId=CONTACT_SHEET_ID, range=f"{CONTACT_TAB}!A:ZZ"
        ).execute()
        rows = result.get("values", [])
        if not rows:
            return jsonify({"existing": [], "created": [], "message": "Sheet is empty"})

        headers = [h.lower().replace(" ", "_") for h in rows[0]]
        camp_idx = next((i for i, h in enumerate(headers) if h == "campaign"), -1)
        if camp_idx < 0:
            return _err("No 'Campaign' column found in contact sheet", 400)

        # Collect unique non-blank campaign IDs from sheet
        sheet_campaigns = set()
        for row in rows[1:]:
            val = row[camp_idx].strip() if camp_idx < len(row) else ""
            if val:
                sheet_campaigns.add(val)

        if not sheet_campaigns:
            return jsonify({"existing": [], "created": [], "message": "No campaign IDs found in sheet"})

        # Get existing campaigns from Firestore
        existing_docs = {d.id for d in db.collection("campaigns").stream()}

        new_campaigns = sheet_campaigns - existing_docs
        existing      = list(sheet_campaigns & existing_docs)
        created       = []

        for campaign_id in sorted(new_campaigns):
            # Create campaign document
            now  = datetime.now(timezone.utc).isoformat()
            data = {
                "campaign_id":            campaign_id,
                "status":                 "draft",
                "sent_at":                None,
                "outreach_email_account": "",
                "mail":                   {"subject": "", "body": "", "type": "plain"},
                "contact_count":          0,
                "sites_count":            0,
                "countries":              [],
                "status_breakdown":       {},
                "select_breakdown":       {},
                "tier_breakdown":         {},
                "outreach_breakdown":     {},
                "updated_at":             now,
            }
            db.collection("campaigns").document(campaign_id).set(data)
            # Enqueue sync job
            job_id = _new_job("campaign-sync", {"campaign_id": campaign_id, "force": False})
            _enqueue_task("campaign-sync", job_id, {"campaign_id": campaign_id, "force": False})
            created.append({"campaign_id": campaign_id, "job_id": job_id})

        msg = f"Found {len(sheet_campaigns)} campaign(s) in sheet. {len(new_campaigns)} new — sync jobs queued." if new_campaigns else f"All {len(sheet_campaigns)} campaign(s) already exist."
        return jsonify({
            "existing": sorted(existing),
            "created":  created,
            "message":  msg,
        })
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
                                       campaign_id=body.get("campaign_id", ""),
                                       force=body.get("force", False))

        elif name == "filter-count":
            from crm.filter_count_lib import run_filter_count
            counts = run_filter_count(db=db, name=body.get("name", ""))
            result = {"name": body.get("name", ""), "counts": counts}

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
    """List recent jobs sorted by queued_at descending.
    ?limit=20       max results (default 20, max 100)
    ?running=true   only return running or queued jobs
    ?campaign_id=X  only return jobs for a specific campaign
    """
    limit       = min(int(request.args.get("limit", 20)), 100)
    running     = request.args.get("running", "").lower() in ("1", "true", "yes")
    campaign_id = request.args.get("campaign_id", "").strip()

    query = _jobs_col().order_by("queued_at", direction="DESCENDING")

    # Compute cutoff time if since parameter given
    since_minutes = request.args.get("since", type=int)
    cutoff = None
    if since_minutes:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).isoformat()

    if running:
        from google.cloud.firestore_v1.base_query import FieldFilter as FF
        queued       = list(_jobs_col().where(filter=FF("status", "==", "queued")).stream())
        running_docs = list(_jobs_col().where(filter=FF("status", "==", "running")).stream())
        all_jobs = [d.to_dict() for d in queued + running_docs]
        # Filter by campaign_id
        if campaign_id:
            all_jobs = [j for j in all_jobs if (j.get("params") or {}).get("campaign_id") == campaign_id]
        # Filter by time window (ignore stale jobs)
        if cutoff:
            all_jobs = [j for j in all_jobs if (j.get("queued_at") or "") >= cutoff]
        # Only truly active statuses
        all_jobs = [j for j in all_jobs if j.get("status") in ("queued", "running")]
        all_jobs.sort(key=lambda j: j.get("queued_at", ""), reverse=True)
        return jsonify({"jobs": all_jobs[:limit], "count": len(all_jobs)})

    docs = list(query.limit(limit).stream())
    jobs = [d.to_dict() for d in docs]
    if campaign_id:
        jobs = [j for j in jobs if (j.get("params") or {}).get("campaign_id") == campaign_id]
    if cutoff:
        jobs = [j for j in jobs if (j.get("queued_at") or "") >= cutoff]
    return jsonify({"jobs": jobs, "count": len(jobs)})


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
