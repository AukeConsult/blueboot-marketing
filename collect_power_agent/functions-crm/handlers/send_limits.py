"""handlers/send_limits.py — Outreach send limits settings and run reservations."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

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
