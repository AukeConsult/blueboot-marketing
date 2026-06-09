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

# ── Per-Blueprint minimum role for mutating requests ─────────────────────────
# GET requests: any authenticated user (including guest) — full read access.
# POST / PATCH / PUT / DELETE: blocked for guests; per-Blueprint minimum below.

# Minimum role required for GET (read) requests per blueprint.
# Any blueprint not listed here allows any authenticated user (including guest) to read.
# Rule: add a blueprint here whenever its GET responses contain sensitive internal data
# (contact details, credentials, campaign data, user docs, system settings).
_BLUEPRINT_MIN_READ_ROLES: dict[str, str] = {
    "campaigns":     "campaign-user",  # embeds full campaign_contacts subcollection
    "contacts":      "campaign-user",  # direct campaign_contacts reads
    "gdisk":         "campaign-user",  # Drive folder contents + settings/gdisk
    "mail_accounts": "campaign-user",  # mail account credentials (settings/mail_accounts)
    "auth":          "campaign-user",  # user role docs (settings/users)
    "mail_tags":     "campaign-user",  # settings/mail_tag_statuses
    "mailbox":       "campaign-user",  # IMAP mailbox contents — no read for user/guest
}

# Endpoints that trigger background jobs or monitor job state.
# These are checked for role even on GET requests — guests cannot
# start jobs or see the job log regardless of HTTP method.
_JOB_ENDPOINTS = frozenset({
    # job triggers (jobs blueprint)
    "jobs.contact_sync",
    "jobs.push_and_sync",
    "jobs.template_sync",
    "jobs.crm_sync_trigger",
    "jobs.campaign_sync",
    "jobs.campaign_export",
    "jobs.job_status",
    "jobs.list_jobs",
    # campaign-level job triggers (campaigns blueprint)
    "campaigns.discover_campaigns",
    # follow-up email sync trigger (followup_email blueprint)
    "followup_email.followup_email_sync",
    # statistics collection (statistics blueprint)
    "statistics.collect_statistics",
})

# Minimum role required for mutating requests (POST / PATCH / PUT / DELETE).
#
# Role model:
#   guest         → no access to internal data, no writes
#   user          → read-only access to all internal data, NO writes
#   campaign-user → full read + write access
#   admin         → everything including settings and user management
#
# Rule: 'user' must NEVER appear as a min role here — all writes require
# at least 'campaign-user'. See CLAUDE.md — "user role is read-only".
# Endpoints that write to the Firestore 'settings' collection require admin
# regardless of their blueprint's default minimum role.
_ADMIN_ENDPOINTS = frozenset({
    "gdisk.gdisk_set_settings",          # POST/PATCH /api/crm/gdisk/settings
    "mail_tags.put_mail_tag_statuses",   # PUT /api/crm/settings/mail-tag-statuses
})

_BLUEPRINT_MIN_ROLES: dict[str, str] = {
    "contacts":       "campaign-user",
    "followup_email": "campaign-user",
    "mailbox":        "campaign-user",
    "mail_tags":      "campaign-user",
    "gdisk":          "campaign-user",
    "leads":          "campaign-user",
    "statistics":     "campaign-user",
    "campaigns":      "campaign-user",
    "jobs":           "campaign-user",
    "filter_facets":  "campaign-user",
    "mail_accounts":  "admin",
    "auth":           "admin",
}


@app.before_request
def check_auth():
    """Verify Firebase ID token on every request.

    Rules:
    - OPTIONS (CORS preflight) — always skip.
    - /api/crm/worker/ — skip; Cloud Tasks supplies its own OIDC token.
    - All other requests — token required.
      • GET  → any authenticated role, including guest (read-only).
      • POST / PATCH / PUT / DELETE → guest blocked; Blueprint minimum enforced.
    """
    from flask import g, request as req
    import firebase_admin.auth as _fb_auth

    if req.method == "OPTIONS":
        return  # CORS preflight handled by Flask-CORS

    if req.path.startswith("/api/crm/worker/"):
        return  # Cloud Tasks authenticates these with OIDC

    import logging as _log

    # ── Ensure Firebase Admin is initialised before any auth call ────────────
    from handlers.shared import _get_user_role, ROLE_LEVELS, _get_db
    db = _get_db()

    # ── Verify token ─────────────────────────────────────────────────────────
    auth_header = req.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        _log.warning(f"[auth] {req.method} {req.path} — no Bearer token")
        return _err("Authentication required — please sign in", 401)

    try:
        decoded    = _fb_auth.verify_id_token(auth_header[7:])
        user_email = decoded.get("email", "").strip().lower()
        _log.info(f"[auth] token OK  user={user_email}  {req.method} {req.path}")
    except _fb_auth.ExpiredIdTokenError:
        _log.warning(f"[auth] token EXPIRED  {req.method} {req.path}")
        return _err("Session expired — please sign in again", 401)
    except _fb_auth.InvalidIdTokenError as exc:
        _log.warning(f"[auth] token INVALID  {req.method} {req.path}  {exc}")
        return _err("Invalid token — please sign in again", 401)
    except _fb_auth.CertificateFetchError as exc:
        _log.error(f"[auth] cert fetch FAILED  {exc}")
        return _err("Auth service temporarily unavailable — please retry", 503)
    except Exception as exc:
        _log.error(f"[auth] verify_id_token ERROR  {type(exc).__name__}: {exc}")
        return _err("Authentication failed — please retry", 503)

    # ── Fetch role from Firestore user doc ────────────────────────────────────
    role = _get_user_role(db, user_email)
    _log.info(f"[auth] role={role}  user={user_email}  {req.method} {req.path}")

    g.user_email = user_email
    g.user_role  = role

    # Determine endpoint and blueprint for role checks
    endpoint  = req.endpoint or ""
    blueprint = endpoint.split(".")[0] if "." in endpoint else None

    # GET requests: check blueprint minimum read role.
    # Endpoints that trigger jobs bypass this and fall through to the full check.
    if req.method == "GET" and endpoint not in _JOB_ENDPOINTS:
        min_read = _BLUEPRINT_MIN_READ_ROLES.get(blueprint)
        if min_read and ROLE_LEVELS.get(role, 0) < ROLE_LEVELS.get(min_read, 1):
            _log.warning(
                f"[auth] BLOCKED read  user={user_email}  role={role}  "
                f"required={min_read}  {req.path}"
            )
            return _err(f"Access denied — requires \'{min_read}\' role to access this data.", 403)
        return  # allowed

    # From here: non-GET request OR a job-related endpoint (any method).
    # Guests are blocked entirely.
    if role == "guest":
        _log.warning(f"[auth] BLOCKED guest  {req.method} {req.path}  user={user_email}")
        return _err(
            "Access denied — your account has not been assigned a role yet. "
            "Contact an administrator.", 403
        )

    # Enforce Blueprint minimum role
    min_role  = _BLUEPRINT_MIN_ROLES.get(blueprint, "user")

    if ROLE_LEVELS.get(role, 0) < ROLE_LEVELS.get(min_role, 1):
        _log.warning(
            f"[auth] BLOCKED insufficient role  user={user_email}  "
            f"role={role}  required={min_role}  {req.method} {req.path}"
        )
        return _err(
            f"Access denied — this action requires '{min_role}' role or higher.", 403
        )

    # Settings collection — admin only, regardless of blueprint minimum
    if endpoint in _ADMIN_ENDPOINTS and role != "admin":
        _log.warning(
            f"[auth] BLOCKED settings write  user={user_email}  role={role}  {req.method} {req.path}"
        )
        return _err("Access denied — only admins can update system settings.", 403)

    _log.info(f"[auth] ALLOWED  user={user_email}  role={role}  {req.method} {req.path}")



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
