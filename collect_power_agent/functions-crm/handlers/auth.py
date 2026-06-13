"""handlers/auth.py — User auth management endpoints."""
from __future__ import annotations
from datetime import datetime, timezone
from flask import Blueprint, request
from handlers.shared import _get_db, _ok, _err

_USERS_COLLECTION = "settings/users/users"

bp = Blueprint("auth", __name__)


def _normalize_email(email: str):
    return email.strip().lower() if email else None


@bp.route("/api/crm/auth/users", methods=["GET"])
def list_auth_users():
    """Return all user docs from settings/users/users, sorted by email."""
    try:
        db    = _get_db()
        docs  = (db.collection("settings").document("users").collection("users")
                   .order_by("email").limit(500).stream())
        users = [d.to_dict() | {"id": d.id} for d in docs]
        return _ok("ok", users=users)
    except Exception as exc:
        return _err(str(exc))


@bp.route("/api/crm/auth/users/<path:email_key>", methods=["PATCH"])
def update_auth_user_doc(email_key: str):
    """Update editable fields on a user doc."""
    try:
        key = _normalize_email(email_key)
        if not key:
            return _err("email_key required", 400)
        body    = request.get_json(silent=True) or {}
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


@bp.route("/api/crm/auth/users/<path:email_key>", methods=["DELETE"])
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
