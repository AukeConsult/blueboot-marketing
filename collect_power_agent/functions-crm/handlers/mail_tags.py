"""handlers/mail_tags.py — Mailbox tag/label management endpoints."""
from __future__ import annotations
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from handlers.shared import _get_db, _get_mail_account, _err
from handlers.imap_utils import _sync_tags_to_imap

bp = Blueprint("mail_tags", __name__)

_MAIL_TAG_STATUSES_DEFAULT = ["New", "Replied", "Interested", "Not interested", "Closed"]
_MAIL_TAG_STATUSES_DOC     = ("settings", "mail_tag_statuses")


def _get_mail_tag_statuses(db) -> list:
    """Return the current status list from Firestore, seeding defaults if missing."""
    ref  = db.collection(_MAIL_TAG_STATUSES_DOC[0]).document(_MAIL_TAG_STATUSES_DOC[1])
    snap = ref.get()
    if snap.exists:
        return snap.to_dict().get("statuses", _MAIL_TAG_STATUSES_DEFAULT)
    ref.set({"statuses": _MAIL_TAG_STATUSES_DEFAULT})
    return list(_MAIL_TAG_STATUSES_DEFAULT)


def _msg_id_to_key(message_id: str) -> str:
    """Sanitize a Message-ID into a safe Firestore document ID."""
    key = (message_id or "").strip().strip("<>").replace("/", "_").replace("\\", "_")
    return key[:500]


def _mail_tags_col(db, account_email: str):
    return (
        db.collection("mail_tags")
        .document(account_email.strip().lower())
        .collection("messages")
    )


@bp.route("/api/crm/settings/mail-tag-statuses", methods=["GET"])
def get_mail_tag_statuses():
    try:
        db       = _get_db()
        statuses = _get_mail_tag_statuses(db)
        return jsonify({"status": "ok", "statuses": statuses})
    except Exception as exc:
        return _err(str(exc))


@bp.route("/api/crm/settings/mail-tag-statuses", methods=["PUT"])
def put_mail_tag_statuses():
    try:
        db   = _get_db()
        body = request.get_json(silent=True) or {}
        raw  = body.get("statuses", [])
        if not isinstance(raw, list):
            return _err("'statuses' must be a list", 400)
        statuses = [str(s).strip() for s in raw if str(s).strip()]
        if not statuses:
            return _err("At least one status is required", 400)
        if len(statuses) > 50:
            return _err("Maximum 50 statuses", 400)
        ref = db.collection(_MAIL_TAG_STATUSES_DOC[0]).document(_MAIL_TAG_STATUSES_DOC[1])
        ref.set({"statuses": statuses, "updated_at": datetime.now(timezone.utc).isoformat()})
        return jsonify({"status": "ok", "statuses": statuses})
    except Exception as exc:
        return _err(str(exc))


@bp.route("/api/crm/mailbox-tags/<path:account_email>", methods=["GET"])
def list_mail_tags(account_email):
    try:
        db   = _get_db()
        col  = _mail_tags_col(db, account_email)
        docs = col.limit(500).stream()
        tags = []
        for d in docs:
            rec = d.to_dict()
            rec["msg_key"] = d.id
            tags.append(rec)
        return jsonify({"status": "ok", "tags": tags})
    except Exception as exc:
        return _err(str(exc))


@bp.route("/api/crm/mailbox-tags/<path:account_email>/<path:msg_key>", methods=["PUT"])
def upsert_mail_tag(account_email, msg_key):
    try:
        db   = _get_db()
        body = request.get_json(silent=True) or {}
        col  = _mail_tags_col(db, account_email)
        ref  = col.document(msg_key)
        existing = ref.get()
        data = existing.to_dict() if existing.exists else {}

        if "status" in body:
            st = body["status"]
            if st:
                allowed = _get_mail_tag_statuses(db)
                if st not in allowed:
                    return _err(f"Invalid status {st!r}. Allowed: {allowed}", 400)
            data["status"] = st
        if "labels" in body:
            raw = body["labels"]
            data["labels"] = [l.strip() for l in (raw if isinstance(raw, list) else []) if l.strip()]
        for field in ("subject", "from_addr", "folder", "uid", "message_id"):
            if field in body:
                data[field] = body[field]
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        ref.set(data)

        imap_err = None
        try:
            ma = _get_mail_account(db, account_email.strip().lower())
            if ma:
                imap_err = _sync_tags_to_imap(
                    ma, account_email,
                    data.get("folder", ""), data.get("uid", ""),
                    data.get("status", ""), data.get("labels", []),
                )
        except Exception as _ie:
            imap_err = str(_ie)

        resp = {"status": "ok", "msg_key": msg_key, "data": data}
        if imap_err:
            resp["imap_warning"] = imap_err
        return jsonify(resp)
    except Exception as exc:
        return _err(str(exc))


@bp.route("/api/crm/mailbox-tags/<path:account_email>/<path:msg_key>", methods=["DELETE"])
def delete_mail_tag(account_email, msg_key):
    try:
        db  = _get_db()
        col = _mail_tags_col(db, account_email)
        ref = col.document(msg_key)

        snap   = ref.get()
        stored = snap.to_dict() if snap.exists else {}
        ref.delete()

        imap_err = None
        try:
            ma = _get_mail_account(db, account_email.strip().lower())
            if ma and stored.get("folder") and stored.get("uid"):
                imap_err = _sync_tags_to_imap(
                    ma, account_email,
                    stored["folder"], stored["uid"],
                    status="", labels=[],
                )
        except Exception as _ie:
            imap_err = str(_ie)

        resp = {"status": "ok"}
        if imap_err:
            resp["imap_warning"] = imap_err
        return jsonify(resp)
    except Exception as exc:
        return _err(str(exc))
