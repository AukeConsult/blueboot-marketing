"""handlers/contacts.py — Campaign contact endpoints + CRM follow-up."""
from __future__ import annotations
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from handlers.shared import _get_db, _ok, _err

bp = Blueprint("contacts", __name__)

# ── Follow-up field helpers ───────────────────────────────────────────────────

_FOLLOWUP_FIELDS = {"followup_date", "followup_status", "followup_comment", "followup_importance"}

_FOLLOWUP_HISTORY_TYPE = {
    "followup_status":     "STATUS",
    "followup_comment":    "COMMENT",
    "followup_date":       "FOLLOWUP",
    "followup_importance": "IMPORTANCE",
}


def _followup_history_text(field: str, value: str) -> str:
    if field == "followup_status":
        return f"Status → {value}" if value else "Status cleared"
    if field == "followup_comment":
        return value or "(comment cleared)"
    if field == "followup_date":
        return f"Follow-up date set to {value}" if value else "Follow-up date cleared"
    if field == "followup_importance":
        return f"Importance → {value}" if value else "Importance cleared"
    return value


def _safe_history(h_list) -> list:
    """Coerce comment_history entries to plain JSON-serialisable dicts."""
    if not isinstance(h_list, list):
        return []
    out = []
    for entry in h_list:
        if not isinstance(entry, dict):
            continue
        out.append({k: (str(v) if v is not None else "") for k, v in entry.items()})
    return out


# ── Routes ────────────────────────────────────────────────────────────────────

@bp.route("/api/crm/campaigns/<campaign_id>/contacts/<doc_id>", methods=["GET"])
def get_campaign_contact(campaign_id, doc_id):
    """Return a single campaign_contact doc."""
    try:
        db  = _get_db()
        ref = (db.collection("campaigns").document(campaign_id)
                 .collection("campaign_contacts").document(doc_id))
        doc = ref.get()
        if not doc.exists:
            return _err(f"Contact '{doc_id}' not found in campaign '{campaign_id}'", 404)
        d = doc.to_dict() or {}
        return jsonify({
            "campaign_id":         campaign_id,
            "doc_id":              doc_id,
            "name":                d.get("name", ""),
            "email":               d.get("email", ""),
            "title":               d.get("title", ""),
            "website":             d.get("website", ""),
            "status":              d.get("status", "pending"),
            "followup_date":       d.get("followup_date", "") or "",
            "followup_status":     d.get("followup_status", "") or "",
            "followup_comment":    d.get("followup_comment", "") or "",
            "followup_importance": d.get("followup_importance", "") or "",
            "comment_history":     _safe_history(d.get("comment_history", [])),
        })
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/campaigns/<campaign_id>/contacts/<doc_id>", methods=["PATCH", "POST"])
def update_campaign_contact(campaign_id, doc_id):
    """Update editable fields on a single campaign_contact doc.

    Standard fields: name, title, status.
    Follow-up fields: followup_date, followup_status, followup_comment, followup_importance.
    _user  optional  logged as the history entry author (defaults to "api")
    """
    try:
        from google.cloud import firestore as _gfs
        db   = _get_db()
        body = request.get_json(silent=True) or {}

        allowed = {"name", "title", "status"} | _FOLLOWUP_FIELDS
        update  = {k: str(v).strip() for k, v in body.items() if k in allowed}
        if not update:
            return _err("No editable fields provided.", 400)

        ref = (db.collection("campaigns").document(campaign_id)
                 .collection("campaign_contacts").document(doc_id))
        if not ref.get().exists:
            return _err(f"Contact '{doc_id}' not found in campaign '{campaign_id}'", 404)

        changed_fu = [f for f in _FOLLOWUP_FIELDS if f in update]
        if changed_fu:
            from flask import g as _g
            # Use server-verified identity; fall back to body '_user' for backward compat.
            user = getattr(_g, 'user_email', None) or (body.get("_user") or "api").strip()
            now  = datetime.now(timezone.utc).isoformat()
            entries = [
                {
                    "date":  now,
                    "user":  user,
                    "text":  _followup_history_text(f, update[f]),
                    "type":  _FOLLOWUP_HISTORY_TYPE[f],
                }
                for f in changed_fu
            ]
            update["comment_history"] = _gfs.ArrayUnion(entries)

        ref.update(update)
        safe = {k: v for k, v in update.items() if k != "comment_history"}
        return _ok(f"Contact '{doc_id}' updated", updated=safe)
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/campaigns/<campaign_id>/contacts/remove", methods=["POST"])
def remove_campaign_contacts(campaign_id):
    """Remove contacts from a campaign by email list."""
    try:
        db   = _get_db()
        body = request.get_json(silent=True) or {}
        emails = body.get("emails", [])
        if not emails or not isinstance(emails, list):
            return _err("Body must contain a non-empty 'emails' list", 400)

        doc_ref = db.collection("campaigns").document(campaign_id)
        if not doc_ref.get().exists:
            return _err(f"Campaign '{campaign_id}' not found", 404)

        contacts_col = doc_ref.collection("campaign_contacts")
        deleted = 0
        for email in emails:
            matches = contacts_col.where("email", "==", email).stream()
            for m in matches:
                m.reference.delete()
                deleted += 1

        remaining = sum(1 for _ in contacts_col.stream())
        doc_ref.update({"contact_count": remaining, "updated_at": datetime.now(timezone.utc).isoformat()})

        from handlers.shared import _new_job, _enqueue_task
        export_params = {"campaign_id": campaign_id}
        export_job_id = _new_job("campaign-export", export_params)
        _enqueue_task("campaign-export", export_job_id, export_params)

        return jsonify({"status": "ok", "deleted": deleted,
                        "contact_count": remaining, "export_job_id": export_job_id})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/followup-contacts", methods=["GET"])
def followup_contacts():
    """Return all campaign_contacts with campaign owner + outreach_email joined."""
    try:
        db          = _get_db()
        campaign_id = request.args.get("campaign_id", "").strip()
        owner_filter = request.args.get("owner", "").strip()
        limit       = min(int(request.args.get("limit", 2000)), 5000)

        camp_map: dict = {}
        for doc in db.collection("campaigns").stream():
            d = doc.to_dict() or {}
            camp_map[doc.id] = {
                "owner":          d.get("owner", ""),
                "outreach_email": d.get("outreach_email_account", ""),
            }

        if campaign_id:
            contacts_iter = (
                db.collection("campaigns")
                  .document(campaign_id)
                  .collection("campaign_contacts")
                  .stream()
            )
        else:
            contacts_iter = (
                db.collection_group("campaign_contacts")
                  .limit(limit)
                  .stream()
            )

        contacts = []
        for doc in contacts_iter:
            d     = doc.to_dict() or {}
            parts = doc.reference.path.split("/")
            cid   = parts[1] if len(parts) >= 4 else campaign_id
            info  = camp_map.get(cid, {})
            if owner_filter:
                if owner_filter == "__none__":
                    if info.get("owner", ""):   # skip contacts that DO have an owner
                        continue
                elif info.get("owner", "") != owner_filter:
                    continue
            contacts.append({
                "campaign_id":         cid,
                "doc_id":              doc.id,
                "doc_path":            f"campaigns/{cid}/campaign_contacts/{doc.id}",
                "name":                d.get("name", ""),
                "email":               d.get("email", ""),
                "title":               d.get("title", ""),
                "website":             d.get("website", ""),
                "status":              d.get("status", "pending"),
                "followup_date":       d.get("followup_date", "") or "",
                "followup_status":     d.get("followup_status", "") or "",
                "followup_comment":    d.get("followup_comment", "") or "",
                "followup_importance": d.get("followup_importance", "") or "",
                "comment_history":     _safe_history(d.get("comment_history", [])),
                "phone":               d.get("phone", "") or "",
                "owner":               info.get("owner", ""),
                "outreach_email":      info.get("outreach_email", ""),
            })

        return jsonify({"contacts": contacts, "count": len(contacts)})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/followup-meta", methods=["GET"])
def followup_meta():
    """Return owners and campaigns for populating the follow-up page header dropdowns."""
    try:
        db = _get_db()
        owners = []
        campaigns = []
        for doc in db.collection("campaigns").stream():
            d = doc.to_dict() or {}
            owner = d.get("owner", "")
            campaigns.append({
                "id":            doc.id,
                "owner":         owner,
                "outreach_email": d.get("outreach_email_account", ""),
            })
            if owner and owner not in owners:
                owners.append(owner)
        owners.sort()
        campaigns.sort(key=lambda c: c["id"])
        return jsonify({"owners": owners, "campaigns": campaigns})
    except Exception as exc:
        return _err(str(exc), 500)
