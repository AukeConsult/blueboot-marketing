"""
functions-crm/main.py — CRM API entry point.

Thin shell: creates the Flask app, registers all Blueprints, and exposes the
two Cloud Function entry points (crmApi / crmWorker).  All business logic lives
in handlers/*.py and crm/*.py.

Trigger endpoints return job_id immediately (crmApi — 300 s timeout).
Cloud Tasks calls crmWorker which runs up to 15 minutes.
Poll GET /api/crm/status/<job_id> for result.
"""
from __future__ import annotations

import sys
import os
_HERE = os.path.dirname(__file__)
for _path in (_HERE,):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from firebase_functions import https_fn, scheduler_fn, options as fn_options
from flask import Flask, jsonify
from flask_cors import CORS

from auth_cache import get_user_role_cached
from handlers.shared import _err, GCP_PROJECT

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
from handlers.inbound_read import bp as inbound_read_bp
from handlers.gdisk         import bp as gdisk_bp
from handlers.filter_facets import bp as filter_facets_bp
from handlers.leads         import bp as leads_bp
from handlers.statistics    import bp as statistics_bp
from handlers.auth          import bp as auth_bp
from handlers.user_prefs    import bp as user_prefs_bp
from handlers.send_limits   import bp as send_limits_bp
from handlers.batch         import bp as batch_bp
from handlers.unsubscribe   import bp as unsubscribe_bp
from handlers.mail_layout   import bp as mail_layout_bp

for bp in (
    campaigns_bp, contacts_bp, jobs_bp, mailbox_bp,
    mail_tags_bp, mail_accounts_bp, inbound_read_bp,
    gdisk_bp, filter_facets_bp, leads_bp, statistics_bp, auth_bp,
    user_prefs_bp, batch_bp, send_limits_bp, unsubscribe_bp, mail_layout_bp,
):
    app.register_blueprint(bp)

# ── Per-Blueprint minimum role for mutating requests ─────────────────────────
# GET requests: any authenticated user (including guest) — full read access.
# POST / PATCH / PUT / DELETE: blocked for guests; per-Blueprint minimum below.

# Minimum role required for GET (read) requests per blueprint.
# Any blueprint not listed here allows any authenticated user (including guest) to read.
# Rule: add a blueprint here whenever its GET responses contain sensitive internal data
# (contact details, credentials, campaign data, user docs, system settings).
# API authentication rules are being moved to auth_settings.py. Until check_auth()
# is explicitly switched over, the existing runtime enforcement tables stay here.
_BLUEPRINT_MIN_READ_ROLES: dict[str, str] = {
    "campaigns":     "campaign-user",  # embeds full campaign_contacts subcollection
    "contacts":      "campaign-user",  # direct campaign_contacts reads
    "gdisk":         "campaign-user",  # Drive folder contents + settings/gdisk
    "mail_accounts": "campaign-user",  # mail account credentials (settings/mail_accounts)
    "auth":          "campaign-user",  # user role docs (settings/users)
    "send_limits":   "campaign-user",  # send limits + run reservations
    "mail_layout":   "campaign-user",  # mail layout settings (admin-only via _ADMIN_ENDPOINTS)
    "mail_tags":     "campaign-user",  # settings/mail_tag_statuses
    "mailbox":       "campaign-user",  # IMAP mailbox contents - no read for user/guest
    "batch":         "campaign-user",  # cloud_batch job definitions and run history
}

_JOB_ENDPOINTS = frozenset({
    "jobs.contact_sync",
    "jobs.push_and_sync",
    "jobs.template_sync",
    "jobs.crm_sync_trigger",
    "jobs.campaign_export",
    "jobs.campaign_import",
    "jobs.outreach_send",
    "jobs.reply_match",
    "jobs.build_facets_trigger",
    "jobs.job_status",
    "jobs.list_jobs",
    "campaigns.discover_campaigns",
    "inbound_read.inbound_read",
    "statistics.collect_statistics",
    "contacts.move_campaign_contacts",
    "batch.trigger_run",
    "batch.list_jobs",
    "batch.list_runs",
    "batch.get_run",
})

_ADMIN_ENDPOINTS = frozenset({
    "gdisk.gdisk_set_settings",          # POST/PATCH /api/crm/gdisk/settings
    "mail_tags.put_mail_tag_statuses",   # PUT /api/crm/settings/mail-tag-statuses
    "send_limits.put_send_limits",       # PUT /api/crm/settings/send-limits
    "send_limits.put_unsubscribe_settings",  # PUT /api/crm/settings/unsubscribe
    "send_limits.put_outreach_schedule", # PUT /api/crm/settings/outreach-schedule
    "mail_layout.get_mail_layout",       # GET  /api/crm/settings/mail-layout
    "mail_layout.put_mail_layout",       # PUT  /api/crm/settings/mail-layout
    "mail_layout.preview_mail_layout",   # POST /api/crm/settings/mail-layout/preview
    "mail_layout.send_test_mail_layout", # POST /api/crm/settings/mail-layout/send-test (real mail)
    "jobs.outreach_send_now",            # POST /api/crm/outreach/send-now (real mail)
    "campaigns.reset_campaign",          # POST /api/crm/campaigns/{id}/reset
})

_BLUEPRINT_MIN_ROLES: dict[str, str] = {
    "contacts":       "campaign-user",
    "inbound_read":   "campaign-user",
    "mailbox":        "campaign-user",
    "mail_tags":      "campaign-user",
    "gdisk":          "campaign-user",
    "leads":          "campaign-user",
    "statistics":     "campaign-user",
    "campaigns":      "campaign-user",
    "jobs":           "campaign-user",
    "filter_facets":  "campaign-user",
    "mail_accounts":  "admin",
    "mail_layout":    "admin",
    "auth":           "admin",
    "user_prefs":     "campaign-user",
    "batch":          "campaign-user",
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

    if req.path == "/unsubscribe":
        return  # public: recipients of our mail (signed link; a GET changes nothing)

    import logging as _log

    # ── Ensure Firebase Admin is initialised before any auth call ────────────
    from handlers.shared import _get_user_role, ROLE_LEVELS, _get_db
    db = _get_db()

    # ── Verify token ─────────────────────────────────────────────────────────
    auth_header = req.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        _log.warning(f"[auth] {req.method} {req.path} — no Bearer token")
        return _err("Sign in required", 401)

    try:
        decoded    = _fb_auth.verify_id_token(auth_header[7:])
        user_email = decoded.get("email", "").strip().lower()
        _log.info(f"[auth] token OK  user={user_email}  {req.method} {req.path}")
    except _fb_auth.ExpiredIdTokenError:
        _log.warning(f"[auth] token EXPIRED  {req.method} {req.path}")
        return _err("Session expired — please sign in again", 401)
    except _fb_auth.InvalidIdTokenError as exc:
        _log.warning(f"[auth] token INVALID  {req.method} {req.path}  {exc}")
        return _err("Sign in required — token invalid", 401)
    except _fb_auth.CertificateFetchError as exc:
        _log.error(f"[auth] cert fetch FAILED  {exc}")
        return _err("Auth service temporarily unavailable — please retry", 503)
    except Exception as exc:
        _log.error(f"[auth] verify_id_token ERROR  {type(exc).__name__}: {exc}")
        return _err("Auth service temporarily unavailable — please retry", 503)

    # ── Fetch role from Firestore user doc ────────────────────────────────────
    role = get_user_role_cached(db, user_email, _get_user_role)
    _log.info(f"[auth] role={role}  user={user_email}  {req.method} {req.path}")

    g.user_email = user_email
    g.user_role  = role

    # Determine endpoint and blueprint for role checks
    endpoint  = req.endpoint or ""
    blueprint = endpoint.split(".")[0] if "." in endpoint else None

    # Non-admins may only touch campaigns they own (/api/crm/campaigns/<id>/...).
    if role != "admin":
        import re as _re
        m = _re.match(r"^/api/crm/campaigns/([^/]+)(/.*)?$", req.path)
        if m:
            snap = db.collection("campaigns").document(m.group(1)).get()
            if snap.exists:
                owner = ((snap.to_dict() or {}).get("owner") or "").strip().lower()
                if owner != user_email:
                    _log.warning(f"[auth] BLOCKED foreign campaign  user={user_email}  "
                                 f"owner={owner}  {req.method} {req.path}")
                    return _err("Access denied (403) — this campaign belongs to another owner", 403)

    # GET requests: check blueprint minimum read role.
    # Endpoints that trigger jobs bypass this and fall through to the full check.
    if req.method == "GET" and endpoint not in _JOB_ENDPOINTS:
        min_read = _BLUEPRINT_MIN_READ_ROLES.get(blueprint)
        if min_read and ROLE_LEVELS.get(role, 0) < ROLE_LEVELS.get(min_read, 1):
            _log.warning(
                f"[auth] BLOCKED read  user={user_email}  role={role}  "
                f"required={min_read}  {req.path}"
            )
            return _err(f"Access denied (403) — '{min_read}' role required", 403)
        return  # allowed

    # From here: non-GET request OR a job-related endpoint (any method).
    # Guests are blocked entirely.
    if role == "guest":
        _log.warning(f"[auth] BLOCKED guest  {req.method} {req.path}  user={user_email}")
        return _err(
            "Access denied (403) — no role assigned. Contact an administrator.", 403
        )

    # Enforce Blueprint minimum role
    min_role  = _BLUEPRINT_MIN_ROLES.get(blueprint, "user")

    if ROLE_LEVELS.get(role, 0) < ROLE_LEVELS.get(min_role, 1):
        _log.warning(
            f"[auth] BLOCKED insufficient role  user={user_email}  "
            f"role={role}  required={min_role}  {req.method} {req.path}"
        )
        return _err(
            f"Access denied (403) — '{min_role}' role required", 403
        )

    # Settings collection — admin only, regardless of blueprint minimum
    if endpoint in _ADMIN_ENDPOINTS and role != "admin":
        _log.warning(
            f"[auth] BLOCKED settings write  user={user_email}  role={role}  {req.method} {req.path}"
        )
        return _err("Access denied (403) — admin role required for settings", 403)

    _log.info(f"[auth] ALLOWED  user={user_email}  role={role}  {req.method} {req.path}")



# -- Root / debug -------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "CRM API",
        "endpoints": [
            "GET  /api/crm/campaigns",
            "GET  /api/crm/followup-contacts",
            "POST /api/crm/inbound-read",
            "GET  /api/crm/status/<job_id>",
            "GET  /api/crm/jobs",
        ],
        "dashboard": f"https://{GCP_PROJECT}.web.app/",
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

@https_fn.on_request(region="us-central1", timeout_sec=300)
def crmApi(req: https_fn.Request) -> https_fn.Response:
    """Trigger + status endpoints — returns quickly."""
    with app.request_context(req.environ):
        try:
            return app.full_dispatch_request()
        except Exception as exc:
            return _err(str(exc), 500)


_SMART_MAIL_PATHS = {
    "/outreach-send",
    "/inbound-read",
    "/reply-match",
}

@https_fn.on_request(region="us-central1", timeout_sec=540,
                     memory=fn_options.MemoryOption.MB_512,
                     max_instances=1)
def smartMail(req: https_fn.Request) -> https_fn.Response:
    """Smart Mail trigger endpoint: outreach send, inbound read, and reply match only."""
    with app.request_context(req.environ):
        try:
            from flask import request as flask_request

            path = flask_request.path.rstrip("/") or "/"
            if path not in _SMART_MAIL_PATHS:
                return _err(
                    "smartMail only serves /outreach-send, /inbound-read, and /reply-match",
                    404,
                )
            return app.full_dispatch_request()
        except Exception as exc:
            return _err(str(exc), 500)


@scheduler_fn.on_schedule(
    schedule="*/30 * * * *",         # every 30 min (UTC); the send budget decides if anything goes out
    region="us-central1",
    timeout_sec=540,
    memory=fn_options.MemoryOption.MB_512,
    max_instances=1,
)
def outreachHourly(event: scheduler_fn.ScheduledEvent) -> None:
    """Every 30 min: if a sending account has send budget, queue one outreach-send.

    Every run is written to the job list (type "scheduled-send") with its start and end time and
    what it decided; repeats of the same outcome update one row (see _log_scheduled_run).
    The budget is a rolling 60-minute window (settings/send_limits), so capacity frees up
    continuously. A fixed hourly run would see its own previous mails still inside the window
    and miss the budget; checking often and acting only when budget exists never misses it.
    The send is queued through _start_outreach_send, so the 409 overlap guard, job record
    and per-account limits still apply. Replies are read by replyHourly.
    """
    from datetime import datetime, timezone
    from handlers.shared import _log_scheduled_run
    started = datetime.now(timezone.utc).isoformat()
    try:
        outcome, reason, extra = _outreach_hourly_run()
    except Exception as exc:
        print(f"[outreachHourly] error: {exc}")
        _log_scheduled_run("outreachHourly", "scheduled-send", "error", str(exc), started)
        raise
    print(f"[outreachHourly] {outcome}: {reason}")
    _log_scheduled_run("outreachHourly", "scheduled-send", outcome, reason, started, **extra)


def _outreach_hourly_run() -> tuple[str, str, dict]:
    """One check of the scheduled send. Returns (outcome, reason, extra) for the job list.

    Goes account by account, the one with the most send budget first. An account without budget
    (below OUTREACH_SCHEDULE_MIN_BUDGET) is skipped, and so is one with nothing to send; the first
    account that has both gets ONE outreach-send job for its own campaigns only. So a spent account
    can never use up the 50-mail fill of the others. The next account is served by the next run.
    """
    from handlers.jobs import _start_outreach_send
    from handlers.shared import _get_db
    from smart_mail.outreach_status_lib import account_budget, load_limits
    from smart_mail.outreach_schedule import load_schedule_flags
    from smart_mail.outreach_mail_select import has_outreach_work, explain_empty

    min_budget = int(os.getenv("OUTREACH_SCHEDULE_MIN_BUDGET", "10"))   # don't start a job for a trickle
    db = _get_db()
    if not load_schedule_flags(db)["send_enabled"]:
        return "switched_off", "switched off in Outreach Settings", {}
    by_account: dict[str, list[str]] = {}
    for d in db.collection("campaigns").stream():
        c = d.to_dict() or {}
        if str(c.get("status", "")).strip().lower() in ("ready", "active"):
            sender = (c.get("outreach_email_account") or c.get("sender_account") or "").strip().lower()
            if sender:
                by_account.setdefault(sender, []).append(d.id)
    if not by_account:
        return "no_campaigns", "no ready or active campaigns with a mail account", {}

    limits = load_limits(db)
    budgets = {a: account_budget(db, a, limits)["budget"] for a in sorted(by_account)}
    candidates = sorted((a for a in by_account if budgets[a] >= min_budget), key=lambda a: (-budgets[a], a))
    if not candidates:
        return "budget_low", f"no mail account has send budget (needs {min_budget})", {"budgets": budgets}

    skipped: dict[str, str] = {}          # accounts with budget but nothing to send, and why
    for account in candidates:
        ids = sorted(by_account[account])
        diag: dict = {}
        work, why = has_outreach_work(ids, diag=diag)
        if not work:
            skipped[account] = " | ".join(f"{m}: {explain_empty(diag.get(m), m)}" for m in ("followup", "intro"))
            continue
        with app.app_context():
            resp = _start_outreach_send({
                "mode": "both", "limit": 500, "campaign_ids": ids, "account": account,
                "dry_run": False, "preview": False, "started_by": "scheduler",
            })
            body, code = resp if isinstance(resp, tuple) else (resp, getattr(resp, "status_code", 200))
            data = body.get_json(silent=True) or {}
        extra = {"account": account, "campaigns": len(ids), "budgets": budgets}
        if skipped:
            extra["skipped"] = skipped
        if code == 202:
            return "queued", f"queued job {data.get('job_id')} for {account}", {**extra, "job_id": data.get("job_id")}
        return "not_queued", str(data.get("message") or data.get("error") or f"HTTP {code}"), {**extra, "http": code}
    return "nothing_to_send", "nothing to send for the accounts that have budget", {
        "budgets": budgets, "detail": skipped}


@scheduler_fn.on_schedule(
    schedule="55 * * * *",           # minute 55 of every hour (UTC), just before the :00 / :30 sends
    region="us-central1",
    timeout_sec=540,
    memory=fn_options.MemoryOption.MB_512,
    max_instances=1,
)
def replyHourly(event: scheduler_fn.ScheduledEvent) -> None:
    """Hourly: read replies/bounces for ready/active campaigns so replied contacts get no further mail.

    Every run is written to the job list (type "scheduled-reply") with its start and end time.
    """
    from datetime import datetime, timezone
    from handlers.shared import _get_db, _log_scheduled_run
    from smart_mail.reply_matcher import match_new_replies
    from smart_mail.outreach_schedule import load_schedule_flags

    started = datetime.now(timezone.utc).isoformat()

    def log(outcome, reason, **extra):
        print(f"[replyHourly] {outcome}: {reason}")
        _log_scheduled_run("replyHourly", "scheduled-reply", outcome, reason, started, **extra)

    try:
        db = _get_db()
        if not load_schedule_flags(db)["reply_enabled"]:
            return log("switched_off", "switched off in Outreach Settings")
        ids = [
            d.id for d in db.collection("campaigns").stream()
            if str((d.to_dict() or {}).get("status", "")).strip().lower() in ("ready", "active")
        ]
        if not ids:
            return log("no_campaigns", "no ready or active campaigns")
        res = match_new_replies(campaigns=ids, days=7)
        matched, bounced = int(res.get("matched", 0)), int(res.get("bounced", 0))
        log("replies_found" if (matched or bounced) else "done",
            f"read replies for {len(ids)} campaigns", campaigns=len(ids), matched=matched, bounced=bounced)
    except Exception as exc:
        log("error", str(exc))


@https_fn.on_request(region="us-central1", timeout_sec=900,
                     memory=fn_options.MemoryOption.GB_1,
                     max_instances=3)
def crmWorker(req: https_fn.Request) -> https_fn.Response:
    """Worker endpoint — called by Cloud Tasks, runs up to 15 min."""
    with app.request_context(req.environ):
        try:
            return app.full_dispatch_request()
        except Exception as exc:
            return https_fn.Response(f"Worker error: {exc}", status=500)
