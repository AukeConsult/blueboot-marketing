"""handlers/mail_accounts.py — Mail account settings (CRUD, ping, test-send)."""
from __future__ import annotations
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from handlers.shared import _get_db, _ma_col, _get_mail_account, _ok, _err

bp = Blueprint("mail_accounts", __name__)


@bp.route("/api/crm/settings/mail-accounts", methods=["GET"])
def list_mail_accounts():
    try:
        db       = _get_db()
        docs     = _ma_col(db).stream()
        accounts = [d.to_dict() for d in docs]
        return jsonify({"status": "ok", "accounts": accounts, "count": len(accounts)})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/settings/mail-accounts", methods=["POST"])
def upsert_mail_account():
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


@bp.route("/api/crm/settings/mail-accounts/<email>", methods=["DELETE"])
def delete_mail_account(email):
    try:
        db  = _get_db()
        key = email.strip().lower()
        _ma_col(db).document(key).delete()
        return jsonify({"status": "ok", "deleted": key})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/settings/mail-accounts/<email>/ping", methods=["POST"])
def ping_mail_account_settings(email):
    try:
        db  = _get_db()
        key = email.strip().lower()
        ma  = _get_mail_account(db, key)
        if not ma:
            return _err(f"Mail account '{key}' not found", 404)
        from smart_mail.mail_sender import MailSender
        result = MailSender(ma).ping()
        return jsonify(result)
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/settings/mail-accounts/<email>/send-test", methods=["POST"])
def send_test_mail_settings(email):
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
        from smart_mail.mail_sender import MailSender
        result = MailSender(ma).send(to=to_addr, subject=subject,
                                     body_plain=body_plain, body_html=body_html)
        return jsonify(result)
    except Exception as exc:
        return _err(str(exc), 500)
