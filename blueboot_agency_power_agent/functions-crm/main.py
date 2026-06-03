"""
functions-crm/main.py -- CRM API as a Python Firebase Cloud Function (2nd gen).

Endpoints (all under /api/crm/):
  POST /api/crm/contact-sync        Export email_contacts -> contact sheet
  POST /api/crm/push-selected       Push selected contacts -> CRM template sheet
  POST /api/crm/template-sync       Sync CRM template sheet -> Firestore + update site_leads
  POST /api/crm/template-enrich     Enrich CRM template from site_leads

Auth:
  Uses the Firebase service account (ADC / GOOGLE_APPLICATION_CREDENTIALS).
  Share both Google Sheets with the service account email:
    <project-id>@appspot.gserviceaccount.com
  No OAuth2 browser flow needed on the server.

Deploy:
  firebase deploy --only functions:crmApi
"""
from __future__ import annotations

import os
import sys
import threading
import functions_framework
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
            # On Cloud Functions: ADC picks up the service account automatically.
            # Locally: set GOOGLE_APPLICATION_CREDENTIALS=path/to/serviceAccountKey.json
            cred = credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred, {"projectId": "blueboot-market"})
        _db = fs.client()
    return _db


def _sheets_service():
    """Build a Sheets API client using Application Default Credentials."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    import google.auth

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
    creds, _ = google.auth.default(scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)


# -- Config -------------------------------------------------------------------
CONTACT_SHEET_ID  = "1aMglV53NiMEArjld37HN5cxliyNRGzIP2mrM4kwlupA"
TEMPLATE_SHEET_ID = "1b1kGKIldeawESH3RYiYjOqRFXRR5kG_81qYRFZI1gSY"
CONTACT_TAB       = "contacts"
TEMPLATE_TAB      = "Outreach"

# -- Flask app ----------------------------------------------------------------
app = Flask(__name__)


def _ok(message: str, **kwargs):
    return jsonify({"status": "ok", "message": message, **kwargs})


def _err(message: str, code: int = 400):
    return jsonify({"status": "error", "message": message}), code


# -- Endpoints ----------------------------------------------------------------

@app.route("/api/crm/contact-sync", methods=["POST"])
def contact_sync():
    """Export email_contacts (filtered by country) -> contact sheet."""
    body      = request.get_json(silent=True) or {}
    countries = body.get("countries")   # list of CC strings e.g. ["NO"]
    max_rows  = body.get("max")         # optional int
    status    = body.get("status")      # optional string
    campaign  = body.get("campaign")    # optional string

    try:
        from crm.contact_sync_lib import run_contact_sync
        added = run_contact_sync(
            db=_get_db(),
            svc=_sheets_service(),
            countries=countries,
            status=status,
            campaign=campaign,
            max_rows=max_rows,
        )
        return _ok(f"Contact sync complete — {added} rows added")
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/push-selected", methods=["POST"])
def push_selected():
    """Push selected contacts from contact sheet -> CRM template sheet."""
    try:
        from crm.contact_to_template_lib import run_push_selected
        added = run_push_selected(
            db=_get_db(),
            svc=_sheets_service(),
        )
        return _ok(f"Push complete — {added} sites added to CRM template")
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/template-sync", methods=["POST"])
def template_sync():
    """Sync CRM template sheet -> Firestore + update site_leads CRM fields."""
    try:
        from crm.crm_template_sync_lib import run_template_sync
        count = run_template_sync(db=_get_db(), svc=_sheets_service())
        return _ok(f"Template sync complete — {count} docs upserted")
    except Exception as exc:
        return _err(str(exc), 500)


@app.route("/api/crm/template-enrich", methods=["POST"])
def template_enrich():
    """Enrich CRM template from site_leads by website URL."""
    try:
        from crm.crm_template_sync_lib import run_template_enrich
        count = run_template_enrich(db=_get_db(), svc=_sheets_service())
        return _ok(f"Enrich complete — {count} items matched")
    except Exception as exc:
        return _err(str(exc), 500)


# -- Cloud Function entry point -----------------------------------------------

@functions_framework.http
def crmApi(request):
    """Firebase Cloud Function entry point — delegates to Flask app."""
    with app.request_context(request.environ):
        try:
            rv = app.full_dispatch_request()
            return rv
        except Exception as exc:
            return _err(str(exc), 500)
