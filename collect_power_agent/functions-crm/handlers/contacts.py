"""handlers/contacts.py — Campaign contact endpoints + CRM follow-up."""
from __future__ import annotations
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from handlers.shared import _get_db, _ok, _err, _accepted

bp = Blueprint("contacts", __name__)

# ── Follow-up field helpers ───────────────────────────────────────────────────

_FOLLOWUP_FIELDS = {"followup_date", "followup_status", "followup_comment", "followup_importance", "followup_owner"}
_CONTACT_STATUSES = {"pending", "active", "excluded"}
_LEGACY_ACTIVE_STATUSES = {"sent", "dosend", "emailed", "replied", "bounced", "error"}

_FOLLOWUP_HISTORY_TYPE = {
    "followup_status":     "STATUS",
    "followup_comment":    "COMMENT",
    "followup_date":       "FOLLOWUP",
    "followup_importance": "IMPORTANCE",
    "followup_owner":      "OWNER",
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
    if field == "followup_owner":
        return f"Owner → {value}" if value else "Owner cleared"
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


def _contact_status(value) -> str:
    status = str(value or "pending").strip().lower()
    if status in _CONTACT_STATUSES:
        return status
    if status in _LEGACY_ACTIVE_STATUSES:
        return "active"
    return "pending"


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
            "status":              _contact_status(d.get("status")),
            "followup_date":       d.get("followup_date", "") or "",
            "followup_status":     d.get("followup_status", "") or "",
            "followup_comment":    d.get("followup_comment", "") or "",
            "followup_importance": d.get("followup_importance", "") or "",
            "followup_owner":      d.get("followup_owner", "") or "",
            "comment_history":     _safe_history(d.get("comment_history", [])),
            "new_mail":            bool(d.get("new_mail", False)),
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

        allowed = {"name", "title", "status", "phone", "linkedin", "twitter", "facebook", "instagram", "whatsapp", "teams", "telegram", "googlechat", "messenger"} | _FOLLOWUP_FIELDS
        update  = {k: str(v).strip() for k, v in body.items() if k in allowed}
        if "status" in update:
            update["status"] = update["status"].lower()
            if update["status"] not in _CONTACT_STATUSES:
                return _err(
                    "Invalid contact status. Must be one of: active, excluded, pending.",
                    400,
                )
        # Boolean fields — handled separately (must not be coerced to str)
        if "new_mail" in body:
            update["new_mail"] = bool(body["new_mail"])
        has_entry = bool((request.get_json(silent=True) or {}).get("_history_entry"))
        if not update and not has_entry:
            return _err("No editable fields provided.", 400)

        ref = (db.collection("campaigns").document(campaign_id)
                 .collection("campaign_contacts").document(doc_id))
        if not ref.get().exists:
            return _err(f"Contact '{doc_id}' not found in campaign '{campaign_id}'", 404)

        changed_fu = [f for f in _FOLLOWUP_FIELDS if f in update]
        from flask import g as _g
        user = getattr(_g, 'user_email', None) or (body.get("_user") or "api").strip()
        now  = datetime.now(timezone.utc).isoformat()
        entries = []
        if changed_fu:
            entries += [
                {
                    "date":  now,
                    "user":  user,
                    "text":  _followup_history_text(f, update[f]),
                    "type":  _FOLLOWUP_HISTORY_TYPE[f],
                }
                for f in changed_fu
            ]
        # Direct history entry (e.g. channel interaction log)
        raw_entry = body.get("_history_entry")
        if isinstance(raw_entry, dict) and raw_entry.get("text"):
            entries.append({
                "date":  raw_entry.get("date", now),
                "user":  user,
                "text":  str(raw_entry["text"])[:200],
                "type":  str(raw_entry.get("type", "NOTE"))[:20],
            })
        if entries:
            update["comment_history"] = _gfs.ArrayUnion(entries)

        ref.update(update)
        safe = {k: v for k, v in update.items() if k != "comment_history"}
        return _ok(f"Contact '{doc_id}' updated", updated=safe)
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/campaigns/<campaign_id>/contacts/<doc_id>/send-mail", methods=["POST"])
def send_mail_to_campaign_contact(campaign_id, doc_id):
    """Send a one-off mail to a campaign contact and log it in comment_history."""
    try:
        from google.cloud import firestore as _gfs
        from flask import g as _g
        db = _get_db()
        body = request.get_json(silent=True) or {}

        camp_ref = db.collection("campaigns").document(campaign_id)
        camp_doc = camp_ref.get()
        if not camp_doc.exists:
            return _err(f"Campaign '{campaign_id}' not found", 404)
        camp = camp_doc.to_dict() or {}

        contact_ref = camp_ref.collection("campaign_contacts").document(doc_id)
        contact_doc = contact_ref.get()
        if not contact_doc.exists:
            return _err(f"Contact '{doc_id}' not found in campaign '{campaign_id}'", 404)
        contact = contact_doc.to_dict() or {}

        to_addr = (body.get("to") or contact.get("email") or "").strip()
        subject = (body.get("subject") or "Follow-up").strip()
        body_html = (body.get("body_html") or "").strip()
        body_plain = (body.get("body_plain") or body.get("body") or "").strip()
        if not to_addr:
            return _err("Contact has no email address.", 400)
        if not body_html and not body_plain:
            return _err("Mail body is required.", 400)

        outreach_email = (camp.get("outreach_email_account") or "").strip().lower()
        if not outreach_email:
            return _err("Campaign has no outreach email account configured.", 400)
        from handlers.shared import _get_mail_account
        ma = _get_mail_account(db, outreach_email)
        if not ma:
            return _err(f"No mail account found for '{outreach_email}'.", 400)

        from crm.mail_sender import MailSender
        result = MailSender(ma).send(
            to=to_addr,
            subject=subject,
            body_plain=body_plain,
            body_html=body_html,
        )
        if result.get("status") != "ok":
            return jsonify(result)

        now = datetime.now(timezone.utc).isoformat()
        user = getattr(_g, "user_email", None) or (body.get("_user") or outreach_email or "api").strip()
        contact_ref.update({
            "followup_status": "contacted",
            "new_mail": False,
            "comment_history": _gfs.ArrayUnion([{
                "date": now,
                "user": user,
                "type": "EMAIL_OUT",
                "text": f"Mail sent: {subject}",
                "from": outreach_email,
                "to": to_addr,
                "subject": subject,
            }]),
        })
        result.update({"logged": True, "from": outreach_email, "to": to_addr})
        return jsonify(result)
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


@bp.route("/api/crm/campaigns/<src_campaign_id>/contacts/move", methods=["POST"])
def move_campaign_contacts(src_campaign_id):
    """Enqueue a campaign-move job.

    Body:
        doc_ids            list[str]  — doc IDs within the source campaign
        target_campaign_id str        — existing campaign ID  (one of these two required)
        new_campaign_name  str        — name for a brand-new campaign to create
    Returns:
        { job_id, status: "queued" }  — poll /api/crm/status/<job_id> for completion
    """
    try:
        from flask import g as _g
        from handlers.shared import _new_job, _enqueue_task
        body = request.get_json(force=True, silent=True) or {}

        doc_ids = body.get("doc_ids", [])
        if not doc_ids or not isinstance(doc_ids, list):
            return _err("Body must contain a non-empty 'doc_ids' list", 400)

        target_id = (body.get("target_campaign_id") or "").strip()
        new_name  = (body.get("new_campaign_name") or "").strip()
        if not target_id and not new_name:
            return _err("Provide either 'target_campaign_id' or 'new_campaign_name'", 400)
        if target_id and new_name:
            return _err("Provide 'target_campaign_id' OR 'new_campaign_name', not both", 400)

        user = getattr(_g, "user_email", None) or "api"
        params = {
            "src_campaign_id":    src_campaign_id,
            "doc_ids":            doc_ids,
            "target_campaign_id": target_id,
            "new_campaign_name":  new_name,
            "user":               user,
        }
        job_id = _new_job("campaign-move", params)
        _enqueue_task("campaign-move", job_id, params)
        return _accepted(job_id, "campaign-move")
    except Exception as exc:
        return _err(str(exc), 500)

@bp.route("/api/crm/followup-contacts", methods=["GET"])
def followup_contacts():
    """Return all campaign_contacts with campaign owner + outreach_email joined."""
    try:
        db          = _get_db()
        campaign_id = request.args.get("campaign_id", "").strip()
        owner_filter = request.args.get("owner", "").strip()
        include_pending = request.args.get("include_pending", "").strip().lower() in {"1", "true", "yes", "on"}
        limit       = min(int(request.args.get("limit", 2000)), 5000)

        camp_map: dict = {}
        for doc in db.collection("campaigns").stream():
            d = doc.to_dict() or {}
            outreach_email = d.get("outreach_email_account", "")
            outreach_display_name = ""
            if outreach_email:
                from handlers.shared import _get_mail_account
                ma = _get_mail_account(db, outreach_email)
                outreach_display_name = (ma or {}).get("display_name", "")
            camp_map[doc.id] = {
                "owner":                 d.get("owner", ""),
                "outreach_email":        outreach_email,
                "outreach_display_name": outreach_display_name,
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
            status = _contact_status(d.get("status"))
            if not include_pending and status == "pending":
                continue
            parts = doc.reference.path.split("/")
            cid   = parts[1] if len(parts) >= 4 else campaign_id
            info  = camp_map.get(cid, {})
            if owner_filter:
                fu_owner   = d.get("followup_owner", "") or ""
                camp_owner = info.get("owner", "") or ""
                if owner_filter == "__none__":
                    # no followup_owner AND no campaign owner
                    if fu_owner or camp_owner:
                        continue
                else:
                    # 1. explicit followup_owner match
                    # 2. fallback: campaign owner matches AND no followup_owner set
                    if fu_owner:
                        if fu_owner != owner_filter:
                            continue
                    else:
                        if camp_owner != owner_filter:
                            continue
            contacts.append({
                "campaign_id":         cid,
                "doc_id":              doc.id,
                "doc_path":            f"campaigns/{cid}/campaign_contacts/{doc.id}",
                "name":                d.get("name", ""),
                "email":               d.get("email", ""),
                "title":               d.get("title", ""),
                "website":             d.get("website", ""),
                "status":              status,
                "followup_date":       d.get("followup_date", "") or "",
                "followup_status":     d.get("followup_status", "") or "",
                "followup_comment":    d.get("followup_comment", "") or "",
                "followup_importance": d.get("followup_importance", "") or "",
                "followup_owner":      d.get("followup_owner", "") or "",
                "comment_history":     _safe_history(d.get("comment_history", [])),
                "phone":               d.get("phone", "") or "",
                "linkedin":            d.get("linkedin", "") or "",
                "twitter":             d.get("twitter", "") or "",
                "facebook":            d.get("facebook", "") or "",
                "instagram":           d.get("instagram", "") or "",
                "whatsapp":            d.get("whatsapp", "") or "",
                "teams":               d.get("teams", "") or "",
                "telegram":            d.get("telegram", "") or "",
                "googlechat":          d.get("googlechat", "") or "",
                "messenger":           d.get("messenger", "") or "",
                "new_mail":            bool(d.get("new_mail", False)),
                "owner":                 info.get("owner", ""),
                "outreach_email":        info.get("outreach_email", ""),
                "outreach_display_name": info.get("outreach_display_name", ""),
            })

        return jsonify({"contacts": contacts, "count": len(contacts)})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/followup-meta", methods=["GET"])
def followup_meta():
    """Return owners, campaigns and users for populating the follow-up page dropdowns."""
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

        # Users for the followup_owner dropdown
        users = []
        for doc in (db.collection("settings").document("users")
                      .collection("users").order_by("email").limit(500).stream()):
            d = doc.to_dict() or {}
            email = d.get("email") or doc.id
            users.append({
                "email":       email,
                "displayName": d.get("displayName", "") or "",
            })

        return jsonify({"owners": owners, "campaigns": campaigns, "users": users})
    except Exception as exc:
        return _err(str(exc), 500)
