"""handlers/send_limits.py — Outreach send limits settings and run reservations."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from google.cloud.firestore_v1.base_query import FieldFilter
from handlers.shared import _get_db, _err, _ok

bp = Blueprint("send_limits", __name__)

_DEFAULTS: dict = {
    "max_sends_per_hour":          50,
    "max_sends_per_day":           300,
    "bounce_rate_pause_threshold": 0.05,
}


# ── Routes ────────────────────────────────────────────────────────────────────

@bp.route("/api/crm/settings/send-limits", methods=["GET"])
def get_send_limits():
    """Return current send limits from settings/send_limits (with defaults fallback)."""
    try:
        db  = _get_db()
        doc = db.collection("settings").document("send_limits").get()
        if doc.exists:
            d = doc.to_dict() or {}
            return jsonify({
                "status":  "ok",
                "source":  "Firestore",
                "max_sends_per_hour":          int(d.get("max_sends_per_hour",          _DEFAULTS["max_sends_per_hour"])),
                "max_sends_per_day":           int(d.get("max_sends_per_day",           _DEFAULTS["max_sends_per_day"])),
                "bounce_rate_pause_threshold": float(d.get("bounce_rate_pause_threshold", _DEFAULTS["bounce_rate_pause_threshold"])),
            })
        return jsonify({"status": "ok", "source": "defaults", **_DEFAULTS})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/settings/send-limits", methods=["PUT"])
def put_send_limits():
    """Write send limits to settings/send_limits.  Admin only (enforced via _ADMIN_ENDPOINTS)."""
    try:
        db   = _get_db()
        body = request.get_json(silent=True) or {}
        hour   = int(body.get("max_sends_per_hour",          _DEFAULTS["max_sends_per_hour"]))
        day    = int(body.get("max_sends_per_day",           _DEFAULTS["max_sends_per_day"]))
        bounce = float(body.get("bounce_rate_pause_threshold", _DEFAULTS["bounce_rate_pause_threshold"]))
        if hour < 1 or day < 1:
            return _err("max_sends_per_hour and max_sends_per_day must be >= 1", 400)
        if not 0 < bounce < 1:
            return _err("bounce_rate_pause_threshold must be between 0 and 1", 400)
        db.collection("settings").document("send_limits").set({
            "max_sends_per_hour":          hour,
            "max_sends_per_day":           day,
            "bounce_rate_pause_threshold": bounce,
        })
        return _ok(
            "Send limits saved.",
            max_sends_per_hour=hour,
            max_sends_per_day=day,
            bounce_rate_pause_threshold=bounce,
        )
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/settings/ai-model", methods=["GET"])
def get_ai_model():
    """Read-only: which OpenAI model each AI feature uses right now (set with OPENAI_MODEL in functions-crm/.env)."""
    import os
    from crm.mail_translate_lib import current_model, DEFAULT_MODEL
    override = os.getenv("OPENAI_MODEL", "").strip()
    return jsonify({
        "status":          "ok",
        "env_override":    override,                       # empty = nothing set, defaults apply
        "api_key_set":     bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "mail_translation": {"model": current_model(), "default": DEFAULT_MODEL},
        "name_enrichment":  {"model": override or "gpt-4o-mini", "default": "gpt-4o-mini"},
    })


@bp.route("/api/crm/settings/mail-trailer", methods=["GET"])
def get_mail_trailer():
    """Return the trailer added to every outreach mail (defaults when not saved yet)."""
    try:
        from smart_mail.mail_trailer import load_trailer
        return jsonify({"status": "ok", **load_trailer(_get_db())})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/settings/mail-trailer", methods=["PUT"])
def put_mail_trailer():
    """Save the mail trailer (HTML + plain-text version).  Admin only (enforced via _ADMIN_ENDPOINTS)."""
    try:
        from smart_mail.mail_trailer import clean_html, DOC_PATH
        from smart_mail.outreach_render_mail import _html_to_text
        body = request.get_json(silent=True) or {}
        html = clean_html(str(body.get("html") or ""))
        text = str(body.get("text") or "").strip() or _html_to_text(html)
        enabled = bool(body.get("enabled"))
        if enabled and not (html or text):
            return _err("Write a trailer text before switching it on.", 400)
        _get_db().collection(DOC_PATH[0]).document(DOC_PATH[1]).set({
            "enabled": enabled, "html": html, "text": text,
        })
        return _ok("Mail trailer saved.", enabled=enabled, html=html, text=text)
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/settings/send-run-reservations", methods=["GET"])
def get_send_run_reservations():
    """Return recent send_run_reservations, newest first."""
    try:
        db    = _get_db()
        limit = min(int(request.args.get("limit", 50)), 200)
        docs  = (
            db.collection("send_run_reservations")
            .order_by("started_at", direction="DESCENDING")
            .limit(limit)
            .stream()
        )
        rows = []
        for doc in docs:
            d = doc.to_dict() or {}
            rows.append({
                "run_id":         doc.id,
                "account":        d.get("account", ""),
                "started_at":     d.get("started_at", ""),
                "budget_claimed": d.get("budget_claimed", 0),
                "sent_actual":    d.get("sent_actual"),
                "failed":         d.get("failed"),
                "unused":         d.get("unused"),
                "status":         d.get("status", ""),
                "finished_at":    d.get("finished_at"),
            })
        return jsonify({"status": "ok", "reservations": rows})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/outreach/budget", methods=["GET"])
@bp.route("/api/crm/settings/send-budget", methods=["GET"])
def get_send_budget():
    """Return current send budget per mail account. Reads only the limits, the sends of the
    last 24 h and the active run reservations - no campaigns or contacts - so it is cheap.

    Optional ?account=a@b.com limits it to one account.

    For each active mail account, calculates:
      sent_last_hour, sent_last_day, already_reserved (active runs),
      remaining_hour, remaining_day, budget (min of both).
    """
    try:
        from datetime import datetime, timedelta, timezone

        db  = _get_db()
        now = datetime.now(timezone.utc)

        # Load limits
        lim_doc = db.collection("settings").document("send_limits").get()
        lim = lim_doc.to_dict() if lim_doc.exists else {}
        max_hour = int(lim.get("max_sends_per_hour", _DEFAULTS["max_sends_per_hour"]))
        max_day  = int(lim.get("max_sends_per_day",  _DEFAULTS["max_sends_per_day"]))

        # Load all mail accounts
        accounts_col = (
            db.collection("settings").document("mail_accounts").collection("accounts")
        )
        account_docs = list(accounts_col.stream())
        accounts = [d.id for d in account_docs]
        only = (request.args.get("account") or "").strip().lower()
        if only:
            accounts = [a for a in accounts if a.lower() == only]

        from smart_mail.outreach_status_lib import account_budget
        limits = {"max_per_hour": max_hour, "max_per_day": max_day}
        results = [account_budget(db, a, limits, now) for a in accounts]

        return jsonify({"status": "ok", "accounts": results,
                        "calculated_at": now.isoformat()})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/outreach/status", methods=["GET"])
def get_outreach_status():
    """Read-only overview of what the next outreach run would do.

    Query: campaign_ids=a,b (optional). Nothing is sent or written.
    """
    try:
        from smart_mail.outreach_status_lib import compute_outreach_status
        ids = [x.strip() for x in (request.args.get("campaign_ids") or "").split(",") if x.strip()]
        return jsonify(compute_outreach_status(_get_db(), ids))
    except Exception as exc:
        return _err(str(exc), 500)
