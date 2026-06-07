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
import urllib.parse
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



@app.route("/api/crm/crm-sync", methods=["GET"])
def crm_sync_trigger():
    """Trigger a CRM sync from the master contact sheet.

    Optional: ?campaign_id=X  to sync only one campaign.
    Returns a job_id to poll via GET /api/crm/status/<job_id>.
    """
    try:
        campaign_id = request.args.get("campaign_id", "").strip()
        params = {"campaign_id": campaign_id}
        job_id = _new_job("crm-sync", params)
        _enqueue_task("crm-sync", job_id, params)
        return _accepted(job_id, "crm-sync")
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


@app.route("/api/crm/campaign-export", methods=["GET"])
def campaign_export():
    """Export a campaign + its contacts to a Sheet (named after the campaign)
    in the gdisk Drive folder. Required: ?campaign_id=NO_jun"""
    campaign_id = request.args.get("campaign_id", "").strip()
    if not campaign_id:
        return _err("campaign_id is required")
    try:
        params = {"campaign_id": campaign_id}
        job_id = _new_job("campaign-export", params)
        _enqueue_task("campaign-export", job_id, params)
        return _accepted(job_id, "campaign-export")
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



def _get_mail_account(db, outreach_email):
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

@app.route("/api/crm/campaigns/<campaign_id>", methods=["GET"])
def get_campaign(campaign_id):
    """Get a single campaign by ID, including contacts subcollection."""
    try:
        db  = _get_db()
        doc = db.collection("campaigns").document(campaign_id).get()
        if not doc.exists:
            return _err(f"Campaign '{campaign_id}' not found", 404)
        data = doc.to_dict()
        # Attach mail account settings from settings/mail_accounts
        outreach_email = data.get("outreach_email_account", "")
        data["mail_account"] = _get_mail_account(db, outreach_email) or {}
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

        # imap / gmail / mail_account_type are stored in settings/mail_accounts,
        # NOT on the campaign document. Look up the outreach account key.
        if "imap" in body or "gmail" in body or "mail_account_type" in body:
            campaign_data    = doc.to_dict() or {}
            outreach_account = (
                body.get("outreach_email_account")
                or campaign_data.get("outreach_email_account", "")
            ).strip().lower()
            if outreach_account:
                # Load existing settings so we can merge
                existing_ma   = _get_mail_account(db, outreach_account) or {}
                account_type  = (
                    body.get("mail_account_type")
                    or existing_ma.get("account_type", "imap")
                )
                if account_type == "imap":
                    merged_imap = dict(existing_ma)
                    merged_imap.update(body.get("imap", {}))
                    account_doc = {
                        "account_type": "imap",
                        "email":        outreach_account,
                        "host":         merged_imap.get("host", ""),
                        "port":         merged_imap.get("port", 993),
                        "username":     merged_imap.get("username", ""),
                        "password":     merged_imap.get("password", ""),
                        "ssl":          merged_imap.get("ssl", True),
                    }
                else:
                    merged_gmail = dict(existing_ma)
                    merged_gmail.update(body.get("gmail", {}))
                    account_doc = {
                        "account_type":  "gmail",
                        "email":         outreach_account,
                        "client_id":     merged_gmail.get("client_id", ""),
                        "client_secret": merged_gmail.get("client_secret", ""),
                        "refresh_token": merged_gmail.get("refresh_token", ""),
                        "access_token":  merged_gmail.get("access_token", ""),
                    }
                account_doc["updated_at"] = datetime.now(timezone.utc).isoformat()
                settings_ma = db.collection("settings").document("mail_accounts")
                if not settings_ma.get().exists:
                    settings_ma.set({"_type": "mail_accounts"})
                settings_ma.collection("accounts").document(outreach_account).set(
                    account_doc, merge=True
                )

        if not update:
            return _err("No valid fields to update", 400)

        update["updated_at"] = datetime.now(timezone.utc).isoformat() if "updated_at" not in update else update["updated_at"]
        doc_ref.update(update)

        updated_doc = doc_ref.get().to_dict()
        return jsonify({"status": "ok", "message": f"Campaign '{campaign_id}' updated", "campaign": updated_doc})

    except Exception as exc:
        return _err(str(exc), 500)



@app.route("/api/crm/campaigns/<campaign_id>/ping-mail-account", methods=["POST"])
def ping_mail_account(campaign_id):
    """Test the mail account configured on a campaign."""
    try:
        db  = _get_db()
        doc = db.collection("campaigns").document(campaign_id).get()
        if not doc.exists:
            return _err(f"Campaign '{campaign_id}' not found", 404)
        data           = doc.to_dict() or {}
        outreach_email = data.get("outreach_email_account", "")
        ma = _get_mail_account(db, outreach_email)
        if not ma:
            return _err(f"No mail account found for '{outreach_email}'. Save IMAP/Gmail settings first.", 400)
        from crm.mail_sender import MailSender
        result = MailSender(ma).ping()
        return jsonify(result)
    except Exception as exc:
        return _err(str(exc), 500)

@app.route("/api/crm/campaigns/<campaign_id>/send-test-mail", methods=["POST"])
def send_test_mail(campaign_id):
    """Send a test email using the campaign mail account settings.
    Body: { "to": "...", "subject": "...", "body_plain": "...", "body_html": "..." }
    """
    try:
        db  = _get_db()
        doc = db.collection("campaigns").document(campaign_id).get()
        if not doc.exists:
            return _err(f"Campaign '{campaign_id}' not found", 404)
        data           = doc.to_dict() or {}
        outreach_email = data.get("outreach_email_account", "")
        ma             = _get_mail_account(db, outreach_email)
        if not ma:
            return _err(f"No mail account found for '{outreach_email}'. Save IMAP/Gmail settings first.", 400)
        body       = request.get_json(silent=True) or {}
        to_addr    = body.get("to", "").strip()
        subject    = body.get("subject", "Test email").strip()
        body_html  = body.get("body_html", "").strip()
        body_plain = body.get("body_plain", body.get("body", "")).strip()
        if not to_addr:
            return _err("'to' is required", 400)
        from crm.mail_sender import MailSender
        result = MailSender(ma).send(to=to_addr, subject=subject,
                                     body_plain=body_plain, body_html=body_html)
        return jsonify(result)
    except Exception as exc:
        return _err(str(exc), 500)

@app.route("/api/crm/campaigns/<campaign_id>/contacts/remove", methods=["POST"])
def remove_campaign_contacts(campaign_id):
    """Remove contacts from a campaign by email list.

    Body: { "emails": ["a@b.com", "c@d.com"] }

    Deletes matching documents from the campaign_contacts subcollection
    and updates the campaign's contact_count.
    """
    try:
        db   = _get_db()
        body = request.get_json(silent=True) or {}
        emails = body.get("emails", [])
        if not emails or not isinstance(emails, list):
            return _err("Body must contain a non-empty 'emails' list", 400)

        doc_ref = db.collection("campaigns").document(campaign_id)
        if not doc_ref.get().exists:
            return _err(f"Campaign '{campaign_id}' not found", 404)

        contacts_col = doc_ref.collection("campaign_contacts")
        deleted = 0
        for email in emails:
            # query by email field
            matches = contacts_col.where("email", "==", email).stream()
            for m in matches:
                m.reference.delete()
                deleted += 1

        # recount and update campaign document
        remaining = sum(1 for _ in contacts_col.stream())
        doc_ref.update({
            "contact_count": remaining,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

        return jsonify({"status": "ok", "deleted": deleted, "contact_count": remaining})

    except Exception as exc:
        return _err(str(exc), 500)


# -- gdisk (Google Drive folder) ---------------------------------------------
GDISK_SETTINGS_COLLECTION = os.getenv("GDISK_SETTINGS_COLLECTION", "settings")
GDISK_SETTINGS_DOC = os.getenv("GDISK_SETTINGS_DOC", "gdisk")


def _gdisk():
    from crm.gdisk_interface import GdiskInterface
    return GdiskInterface.from_settings(_get_db())



# ── Mail accounts settings ────────────────────────────────────────────────

def _ma_col(db):
    """Shortcut to settings/mail_accounts/accounts collection."""
    settings_ma = db.collection("settings").document("mail_accounts")
    if not settings_ma.get().exists:
        settings_ma.set({"_type": "mail_accounts"})
    return settings_ma.collection("accounts")


@app.route("/api/crm/settings/mail-accounts", methods=["GET"])
def list_mail_accounts():
    """List all mail accounts from settings/mail_accounts/accounts."""
    try:
        db   = _get_db()
        docs = _ma_col(db).stream()
        accounts = [d.to_dict() for d in docs]
        return jsonify({"status": "ok", "accounts": accounts, "count": len(accounts)})
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/settings/mail-accounts", methods=["POST"])
def upsert_mail_account():
    """Create or update a mail account.

    Body must include 'email' (used as document ID) and 'account_type' (imap | gmail).
    All other fields are merged.
    """
    try:
        db   = _get_db()
        body = request.get_json(silent=True) or {}
        email = body.get("email", "").strip().lower()
        if not email:
            return _err("'email' is required", 400)
        account_type = body.get("account_type", "imap")
        if account_type not in ("imap", "gmail"):
            return _err("account_type must be 'imap' or 'gmail'", 400)
        body["email"]      = email
        body["updated_at"] = datetime.now(timezone.utc).isoformat()
        _ma_col(db).document(email).set(body, merge=True)
        doc = _ma_col(db).document(email).get().to_dict()
        return jsonify({"status": "ok", "account": doc})
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/settings/mail-accounts/<email>", methods=["DELETE"])
def delete_mail_account(email):
    """Delete a mail account by email."""
    try:
        db  = _get_db()
        key = email.strip().lower()
        _ma_col(db).document(key).delete()
        return jsonify({"status": "ok", "deleted": key})
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/settings/mail-accounts/<email>/ping", methods=["POST"])
def ping_mail_account_settings(email):
    """Ping a mail account directly from settings (no campaign needed)."""
    try:
        db  = _get_db()
        key = email.strip().lower()
        ma  = _get_mail_account(db, key)
        if not ma:
            return _err(f"Mail account '{key}' not found", 404)
        from crm.mail_sender import MailSender
        result = MailSender(ma).ping()
        return jsonify(result)
    except Exception as exc:
        return _err(str(exc), 500)

@app.route("/api/crm/settings/mail-accounts/<email>/send-test", methods=["POST"])
def send_test_mail_settings(email):
    """Send a test email using a mail account from settings (no campaign needed).
    Body: { "to": "...", "subject": "...", "body_plain": "...", "body_html": "..." }
    """
    try:
        db   = _get_db()
        key  = email.strip().lower()
        ma   = _get_mail_account(db, key)
        if not ma:
            return _err(f"Mail account '{key}' not found", 404)
        body       = request.get_json(silent=True) or {}
        to_addr    = body.get("to", "").strip()
        subject    = body.get("subject", "Test email").strip()
        body_html  = body.get("body_html", "").strip()
        body_plain = body.get("body_plain", body.get("body", "This is a test email.")).strip()
        if not to_addr:
            return _err("'to' is required", 400)
        from crm.mail_sender import MailSender
        result = MailSender(ma).send(to=to_addr, subject=subject,
                                     body_plain=body_plain, body_html=body_html)
        return jsonify(result)
    except Exception as exc:
        return _err(str(exc), 500)



@app.route("/api/crm/statistics/collect", methods=["POST"])
def collect_statistics():
    """Queue a job to run all statistics aggregations."""
    try:
        body   = request.get_json(silent=True) or {}
        only   = body.get("only", "")   # optional: run one section only
        params = {"only": only} if only else {}
        job_id = _new_job("statistics", params)
        _enqueue_task("statistics", job_id, params)
        return _accepted(job_id, "statistics")
    except Exception as exc:
        return _err(str(exc), 500)

@app.route("/api/crm/statistics", methods=["GET"])
def get_statistics():
    """Return all statistics documents from the statistics collection.

    Returns a map of doc_id -> document dict for the main stat documents.
    Sub-collections (countries) are included for priority-pr-country.
    """
    try:
        db   = _get_db()
        col  = db.collection("statistics")
        docs = {d.id: d.to_dict() for d in col.stream() if d.exists}

        # Attach countries sub-collection for priority-pr-country
        if "priority-pr-country" in docs:
            countries = {
                d.id: d.to_dict()
                for d in col.document("priority-pr-country")
                             .collection("countries").stream()
            }
            docs["priority-pr-country"]["countries"] = countries

        return jsonify({"status": "ok", "statistics": docs,
                        "doc_count": len(docs)})
    except Exception as exc:
        return _err(str(exc), 500)

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
            # Enqueue crm-sync (master sheet) to populate contacts for the new campaign
            job_id = _new_job("crm-sync", {"campaign_id": campaign_id})
            _enqueue_task("crm-sync", job_id, {"campaign_id": campaign_id})
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

        elif name == "crm-sync":
            from crm.crm_sync_lib import run_crm_sync
            result = run_crm_sync(db=db, svc=svc,
                                  campaign_id=body.get("campaign_id", ""))

        elif name == "statistics":
            from crm.statistics_builder import StatisticsBuilder
            only = body.get("only", "")
            sb   = StatisticsBuilder(db=db)
            if only == "leads-overview":
                result = sb.leads_overview()
            elif only == "site-leads-overview":
                result = sb.site_leads_overview()
            elif only == "site-funnel":
                result = sb.site_pipeline_enrichment_funnel()
            elif only == "lead-funnel":
                result = sb.lead_pipeline_enrichment_funnel()
            elif only == "quality":
                result = sb.data_quality_report()
            elif only == "email-funnel":
                result = sb.email_contacts_funnel()
            elif only == "coverage":
                result = sb.pipeline_coverage()
            elif only == "campaigns":
                result = sb.campaign_statistics()
            else:
                sb.leads_overview()
                sb.site_leads_overview()
                sb.site_pipeline_enrichment_funnel()
                sb.lead_pipeline_enrichment_funnel()
                sb.data_quality_report()
                sb.email_contacts_funnel()
                sb.pipeline_coverage()
                sb.campaign_statistics()
                result = {"collected": True}

        elif name == "campaign-sync":
            from crm.campaign_sync_lib import run_campaign_sync
            result = run_campaign_sync(db=db, svc=svc, gd=_gdisk(),
                                       campaign_id=body.get("campaign_id", ""))

        elif name == "filter-count":
            from crm.filter_count_lib import run_filter_count
            counts = run_filter_count(db=db, name=body.get("name", ""))
            result = {"name": body.get("name", ""), "counts": counts}

        elif name == "campaign-export":
            from crm.campaign_export_lib import run_campaign_export
            result = run_campaign_export(db=db, svc=svc, gd=_gdisk(),
                                         campaign_id=body.get("campaign_id", ""))
            # Persist sheet_url on the campaign document for quick access
            cid = body.get("campaign_id", "")
            if cid and result.get("url"):
                db.collection("campaigns").document(cid).update({
                    "sheet_url":  result["url"],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })

        else:
            _update_job(job_id, status="error",
                        error=f"Unknown job: {name}",
                        finished_at=datetime.now(timezone.utc).isoformat())
            return _err(f"Unknown job: {name}", 400)

        _update_job(job_id,
                    status="done",
                    result=result,
                    finished_at=datetime.now(timezone.utc).isoformat())
        return jsonify({"status": "ok", "message": f"Job {job_id} done", "result": result})

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
    ?limit=20       max results (default 20, max 500)
    ?running=true   only return running or queued jobs
    ?campaign_id=X  only return jobs for a specific campaign
    """
    limit       = min(int(request.args.get("limit", 20)), 500)
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


@app.route("/api/crm/settings/mail-accounts/<email>/mailbox", methods=["GET"])
def read_mailbox(email):
    """Read recent emails from all folders of a mail account.

    Query params:
      per_folder  int  max messages per folder (default 10, max 50)

    Returns list of messages sorted newest first:
      { uid, folder, subject, from, to, date, preview, body }
    """
    import imaplib
    import email as _email
    import ssl as _ssl
    import base64
    import re as _re
    from email.header import decode_header as _dh
    from email.utils import parsedate_to_datetime

    def _decode_str(val):
        if not val:
            return ""
        parts = _dh(val)
        out = []
        for raw, enc in parts:
            if isinstance(raw, bytes):
                out.append(raw.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(raw)
        return " ".join(out)

    def _parse_folder(item):
        # Parse IMAP LIST item -> (selectable: bool, folder_name: str)
        # Format: (\flags) "delim" "name"  or  (\flags) "delim" name
        # Noselect folders are containers and cannot be selected.
        if isinstance(item, bytes):
            item = item.decode("utf-8", errors="replace")
        import re as _r
        flags_m = _r.match(r"\(([^)]*)\)", item)
        flags = flags_m.group(1).lower() if flags_m else ""
        selectable = "noselect" not in flags
        name_part = _r.sub(r"^\(.*?\)\s+(?:\"[^\"]*\"|NIL)\s*", "", item).strip()
        if name_part.startswith('"') and name_part.endswith('"'):
            name_part = name_part[1:-1]
        return selectable, name_part
    def _get_body(msg):
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                cd = str(part.get("Content-Disposition", ""))
                if ct == "text/plain" and "attachment" not in cd:
                    charset = part.get_content_charset() or "utf-8"
                    raw = part.get_payload(decode=True)
                    body = raw.decode(charset, errors="replace") if raw else ""
                    break
            if not body:
                for part in msg.walk():
                    ct = part.get_content_type()
                    cd = str(part.get("Content-Disposition", ""))
                    if ct == "text/html" and "attachment" not in cd:
                        charset = part.get_content_charset() or "utf-8"
                        raw = part.get_payload(decode=True)
                        html = raw.decode(charset, errors="replace") if raw else ""
                        body = _re.sub(r"<[^>]+>", " ", html)
                        body = _re.sub(r"\s+", " ", body).strip()
                        break
        else:
            raw = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            body = raw.decode(charset, errors="replace") if raw else ""
        return body.strip()

    def _fetch_folder(conn, folder_name, per_folder):
        msgs = []
        try:
            quoted = '"' + folder_name.replace('"', '\\"') + '"'
            typ, _ = conn.select(quoted, readonly=True)
            if typ != "OK":
                typ, _ = conn.select(folder_name, readonly=True)
            if typ != "OK":
                return msgs
        except Exception:
            return msgs
        typ, data = conn.uid("search", None, "ALL")
        if typ != "OK" or not data[0]:
            return msgs
        uids = data[0].split()
        for uid in uids[-per_folder:][::-1]:
            try:
                typ, raw = conn.uid("fetch", uid, "(RFC822)")
                if typ != "OK" or not raw or not raw[0]:
                    continue
                msg = _email.message_from_bytes(raw[0][1])
                body = _get_body(msg)
                try:
                    date_str = parsedate_to_datetime(msg.get("Date", "")).isoformat()
                except Exception:
                    date_str = msg.get("Date", "")
                msgs.append({
                    "uid":     uid.decode(),
                    "folder":  folder_name,
                    "subject": _decode_str(msg.get("Subject", "(no subject)")),
                    "from":    _decode_str(msg.get("From", "")),
                    "to":      _decode_str(msg.get("To", "")),
                    "date":    date_str,
                    "preview": body[:200],
                    "body":    body[:8000],
                })
            except Exception:
                continue
        return msgs

    try:
        db  = _get_db()
        key = email.strip().lower()
        ma  = _get_mail_account(db, key)
        if not ma:
            return _err(f"Mail account '{key}' not found", 404)

        per_folder   = min(int(request.args.get("per_folder", 10)), 50)
        account_type = ma.get("account_type", "imap")
        messages     = []

        if account_type == "imap":
            host     = ma.get("host", "").strip()
            port     = int(ma.get("port") or 993)
            username = ma.get("username", "").strip()
            password = ma.get("password", "")
            use_ssl  = ma.get("ssl", True)
            if not host or not username:
                return _err("IMAP host and username are required", 400)
            if use_ssl:
                conn = imaplib.IMAP4_SSL(host, port, ssl_context=_ssl.create_default_context())
            else:
                conn = imaplib.IMAP4(host, port)
            conn.login(username, password)

        elif account_type == "gmail":
            client_id     = ma.get("client_id", "").strip()
            client_secret = ma.get("client_secret", "").strip()
            refresh_token = ma.get("refresh_token", "").strip()
            access_token  = ma.get("access_token", "").strip()
            if not refresh_token:
                return _err("refresh_token is required for Gmail", 400)
            if not access_token:
                import urllib.request
                import json as _json
                p = (
                    f"client_id={urllib.parse.quote(client_id)}"
                    f"&client_secret={urllib.parse.quote(client_secret)}"
                    f"&refresh_token={urllib.parse.quote(refresh_token)}"
                    f"&grant_type=refresh_token"
                ).encode()
                req = urllib.request.Request(
                    "https://oauth2.googleapis.com/token", data=p,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    td = _json.loads(resp.read())
                access_token = td.get("access_token", "")
                if not access_token:
                    return jsonify({"status": "error",
                                    "message": td.get("error_description", "Token refresh failed")})
                _ma_col(db).document(key).update({"access_token": access_token})

        else:
            return _err(f"Unsupported account_type '{account_type}'", 400)

        # --- IMAP: build connection for Gmail via XOAUTH2 ---
        if account_type == "gmail":
            auth_str = f"user={key}auth=Bearer {access_token}"
            conn = imaplib.IMAP4_SSL("imap.gmail.com", 993,
                                     ssl_context=_ssl.create_default_context())
            conn.authenticate("XOAUTH2", lambda _: auth_str.encode())

        # --- list selectable folders and fetch messages ---
        try:
            typ, raw_list = conn.list()
            folders = []
            if typ == "OK":
                for item in raw_list:
                    selectable, fname = _parse_folder(item)
                    if selectable and fname:
                        folders.append(fname)
            if not folders:
                folders = ["INBOX"]

            for folder in folders:
                messages.extend(_fetch_folder(conn, folder, per_folder))
        finally:
            try:
                conn.logout()
            except Exception:
                pass

        # sort newest first
        def _msg_key(m):
            try:
                from email.utils import parsedate_to_datetime as _p
                return _p(m["date"]) if not m["date"].endswith("+00:00") else m["date"]
            except Exception:
                return m.get("date", "")

        messages.sort(key=lambda m: m.get("date", ""), reverse=True)

        return jsonify({"status": "ok", "messages": messages})

    except Exception as exc:
        return _err(str(exc))


# ---------------------------------------------------------------------------
# Auth user sync is handled by non-blocking Node.js event triggers in
# functions-auth/index.js (onCreate / onDelete).
#
# This endpoint is kept as an admin convenience — e.g. to manually remove
# a stale doc when an account was deleted outside the normal flow.
#   DELETE /api/crm/auth/users/<normalizedEmail>
# ---------------------------------------------------------------------------

_USERS_COLLECTION = "settings/users/users"


def _normalize_email(email: str) -> str | None:
    """Lower-case + strip; returns None if email is falsy."""
    return email.strip().lower() if email else None


@app.route("/api/crm/auth/users", methods=["GET"])
def list_auth_users():
    """Return all user docs from settings/users/users, sorted by email."""
    try:
        db    = _get_db()
        docs  = db.collection("settings").document("users").collection("users")                   .order_by("email").limit(500).stream()
        users = [d.to_dict() | {"id": d.id} for d in docs]
        return _ok("ok", users=users)
    except Exception as exc:
        return _err(str(exc))


@app.route("/api/crm/auth/users/<path:email_key>", methods=["PATCH"])
def update_auth_user_doc(email_key: str):
    """Update editable fields on a user doc (displayName, role, notes)."""
    try:
        key = _normalize_email(email_key)
        if not key:
            return _err("email_key required", 400)
        body = request.get_json(silent=True) or {}
        allowed = {"displayName", "role", "notes", "defaultMailbox"}
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            return _err("No editable fields provided (allowed: displayName, role, notes)", 400)
        updates["updatedAt"] = datetime.now(timezone.utc).isoformat()
        db = _get_db()
        db.document(f"{_USERS_COLLECTION}/{key}").update(updates)
        return _ok(f"User '{key}' updated")
    except Exception as exc:
        return _err(str(exc))


@app.route("/api/crm/auth/users/<path:email_key>", methods=["DELETE"])
def delete_auth_user_doc(email_key: str):
    """Remove the Firestore mirror doc for a deleted Firebase Auth user."""
    try:
        key = _normalize_email(email_key)
        if not key:
            return _err("email_key required", 400)
        db = _get_db()
        db.document(f"{_USERS_COLLECTION}/{key}").delete()
        return _ok(f"User doc '{key}' deleted")
    except Exception as exc:
        return _err(str(exc))
