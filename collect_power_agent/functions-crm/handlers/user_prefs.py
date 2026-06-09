"""handlers/user_prefs.py — Per-user frontend state persistence.

Firestore path:  frontend-status/{user_email}/pages/{page_name}

GET  /api/crm/user-prefs?page=<name>   — return saved state for the page
PUT  /api/crm/user-prefs?page=<name>   — overwrite saved state for the page

The caller is always identified by g.user_email (set by check_auth).
"""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from handlers.shared import _get_db, _err

bp = Blueprint("user_prefs", __name__)

_COLLECTION    = "frontend-status"
_PAGES_SUB     = "pages"
_ALLOWED_PAGES = frozenset({"followup"})

# ── Helpers ───────────────────────────────────────────────────────────────────

def _page_ref(db, page: str):
    """Return the Firestore DocumentReference for the caller's page state."""
    return (
        db.collection(_COLLECTION)
          .document(g.user_email)
          .collection(_PAGES_SUB)
          .document(page)
    )


def _validate_page(page: str):
    """Return (page, None) if valid, else (None, error response)."""
    if page not in _ALLOWED_PAGES:
        return None, _err(
            f"Unknown page '{page}'. Allowed: {sorted(_ALLOWED_PAGES)}", 400
        )
    return page, None


# ── Routes ────────────────────────────────────────────────────────────────────

@bp.route("/api/crm/user-prefs", methods=["GET"])
def get_user_prefs():
    """Return stored frontend state for the requested page."""
    page, err = _validate_page(request.args.get("page", "").strip())
    if err:
        return err
    try:
        doc = _page_ref(_get_db(), page).get()
        return jsonify(doc.to_dict() or {})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/user-prefs", methods=["PUT"])
def put_user_prefs():
    """Overwrite stored frontend state for the requested page."""
    page, err = _validate_page(request.args.get("page", "").strip())
    if err:
        return err
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _err("Request body must be a JSON object.", 400)
    try:
        _page_ref(_get_db(), page).set(body)
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _err(str(exc), 500)
