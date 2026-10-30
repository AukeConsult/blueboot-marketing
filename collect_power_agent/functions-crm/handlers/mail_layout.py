"""handlers/mail_layout.py — the Mail layout page: the frame, footer and unsubscribe block around every outreach mail."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from handlers.shared import _get_db, _get_mail_account, _err, _ok

bp = Blueprint("mail_layout", __name__)

PLACEHOLDERS = {
    "parts":   ["{{message}}", "{{footer}}", "{{unsubscribe}}"],
    "inside":  ["{{sender_name}}", "{{sender_email}}", "{{company_name}}", "{{company_address}}", "{{unsubscribe_url}}"],
}


def _sample_mail(settings: dict, mass: bool, sender: dict) -> dict:
    """The sample mail of the page: the layout around a sample message for a sample contact."""
    from smart_mail import mail_layout as ml
    from smart_mail.template_engine import render_template
    who = dict(sender_name=sender["name"], sender_email=sender["email"],
               unsubscribe_url=ml.SAMPLE_UNSUBSCRIBE_URL if mass else "", contact=ml.SAMPLE_CONTACT)
    text_message = render_template(ml.SAMPLE_MESSAGE_TEXT, ml.SAMPLE_CONTACT)
    html_message = render_template(ml.SAMPLE_MESSAGE_HTML, ml.SAMPLE_CONTACT)
    return {"text": ml.compose(settings, "text", text_message, **who),
            "html": ml.compose(settings, "html", html_message, **who)}


def _sender(db, account_email: str) -> dict:
    """The sender of the sample: a saved mail account, or a made-up one."""
    ma = _get_mail_account(db, account_email) if account_email else None
    if ma:
        return {"name": str(ma.get("from_name") or ma.get("email") or ""), "email": str(ma.get("email") or account_email)}
    return {"name": "Sender Name", "email": "sender@example.com"}


@bp.route("/api/crm/settings/mail-layout", methods=["GET"])
def get_mail_layout():
    """The saved layout (defaults when nothing is saved), the defaults for "Use default", and the placeholders."""
    try:
        from smart_mail import mail_layout as ml
        return jsonify({"status": "ok", **ml.load_layout(_get_db()),
                        "defaults": ml.DEFAULT_LAYOUT, "placeholders": PLACEHOLDERS})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/settings/mail-layout", methods=["PUT"])
def put_mail_layout():
    """Save the layout. Admin only (enforced via _ADMIN_ENDPOINTS)."""
    try:
        from smart_mail import mail_layout as ml
        try:
            doc = ml.validate(request.get_json(silent=True) or {})
        except ValueError as ve:
            return _err(str(ve), 400)
        _get_db().collection(ml.DOC_PATH[0]).document(ml.DOC_PATH[1]).set(doc)
        return _ok("Mail layout saved.", **doc)
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/settings/mail-layout/preview", methods=["POST"])
def preview_mail_layout():
    """The finished sample mail for the settings in the request (not saved). mass: with the unsubscribe block."""
    try:
        from smart_mail import mail_layout as ml
        body = request.get_json(silent=True) or {}
        try:
            settings = ml.normalize(ml.validate(body))
        except ValueError as ve:
            return _err(str(ve), 400)
        mail = _sample_mail(settings, bool(body.get("mass")), _sender(_get_db(), str(body.get("account") or "")))
        return jsonify({"status": "ok", **mail})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/settings/mail-layout/send-test", methods=["POST"])
def send_test_mail_layout():
    """Sends the sample mail for the settings in the request through a mail account, to see it in a real inbox."""
    try:
        from smart_mail import mail_layout as ml
        from smart_mail.mail_sender import MailSender
        db = _get_db()
        body = request.get_json(silent=True) or {}
        account = str(body.get("account") or "").strip().lower()
        to_addr = str(body.get("to") or "").strip()
        if not account or not to_addr:
            return _err("'account' and 'to' are required", 400)
        ma = _get_mail_account(db, account)
        if not ma:
            return _err(f"Mail account '{account}' not found", 404)
        try:
            settings = ml.normalize(ml.validate(body))
        except ValueError as ve:
            return _err(str(ve), 400)
        mail = _sample_mail(settings, bool(body.get("mass")), _sender(db, account))
        result = MailSender(ma).send(to=to_addr, subject="Test: mail layout",
                                     body_plain=mail["text"], body_html=mail["html"])
        return jsonify(result)
    except Exception as exc:
        return _err(str(exc), 500)
