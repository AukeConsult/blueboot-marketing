"""
functions-crm/main.py — CRM API entry point.

Thin shell: creates the Flask app, registers all Blueprints, and exposes the
two Cloud Function entry points (crmApi / crmWorker).  All business logic lives
in handlers/*.py and crm/*.py.

Trigger endpoints return job_id immediately (crmApi — 30 s timeout).
Cloud Tasks calls crmWorker which runs up to 15 minutes.
Poll GET /api/crm/status/<job_id> for result.
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from firebase_functions import https_fn, options as fn_options
from flask import Flask, jsonify
from flask_cors import CORS

from handlers.shared import _err

# -- Flask app ----------------------------------------------------------------
app = Flask(__name__)
CORS(app)

# -- Register all Blueprints --------------------------------------------------
from handlers.campaigns     import bp as campaigns_bp
from handlers.contacts      import bp as contacts_bp
from handlers.jobs          import bp as jobs_bp
from handlers.mailbox       import bp as mailbox_bp
from handlers.mail_tags     import bp as mail_tags_bp
from handlers.mail_accounts import bp as mail_accounts_bp
from handlers.followup_email import bp as followup_email_bp
from handlers.gdisk         import bp as gdisk_bp
from handlers.filter_facets import bp as filter_facets_bp
from handlers.leads         import bp as leads_bp
from handlers.statistics    import bp as statistics_bp
from handlers.auth          import bp as auth_bp

for bp in (
    campaigns_bp, contacts_bp, jobs_bp, mailbox_bp,
    mail_tags_bp, mail_accounts_bp, followup_email_bp,
    gdisk_bp, filter_facets_bp, leads_bp, statistics_bp, auth_bp,
):
    app.register_blueprint(bp)

# -- Root / debug -------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "CRM API",
        "endpoints": [
            "GET  /api/crm/campaigns",
            "GET  /api/crm/followup-contacts",
            "POST /api/crm/followup-email-sync",
            "GET  /api/crm/status/<job_id>",
            "GET  /api/crm/jobs",
        ],
        "dashboard": "https://blueboot-market.web.app/",
    })


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
