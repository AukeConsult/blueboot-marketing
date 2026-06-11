"""handlers/campaigns.py — Campaign CRUD endpoints."""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from handlers.shared import (
    _get_db, _sheets_service, _gdisk, _get_mail_account, _ma_col,
    _new_job, _enqueue_task, _ok, _err, _accepted,
)

bp = Blueprint("campaigns", __name__)

_CONTACT_STATUSES = {"pending", "active", "excluded"}
_LEGACY_ACTIVE_STATUSES = {"sent", "dosend", "emailed", "replied", "bounced", "error"}
_CAMPAIGN_STATUSES = {"draft", "ready", "active", "canceled"}
_LEGACY_CAMPAIGN_STATUSES = {
    "dosend": "ready",
    "sent": "active",
    "cancelled": "canceled",
}


def _contact_status(value) -> str:
    status = str(value or "pending").strip().lower()
    if status in _CONTACT_STATUSES:
        return status
    if status in _LEGACY_ACTIVE_STATUSES:
        return "active"
    return "pending"


def _campaign_status(value) -> str:
    status = str(value or "draft").strip().lower()
    status = _LEGACY_CAMPAIGN_STATUSES.get(status, status)
    return status if status in _CAMPAIGN_STATUSES else "draft"


@bp.route("/api/crm/campaigns", methods=["GET"])
def list_campaigns():
    """List all campaigns, ordered by updated_at descending."""
    try:
        db     = _get_db()
        status = _campaign_status(request.args.get("status", "").strip()) if request.args.get("status") else ""
        col    = db.collection("campaigns")
        query  = col.order_by("updated_at", direction="DESCENDING")
        docs = list(query.stream())
        campaigns = []
        for d in docs:
            data = d.to_dict() or {}
            data["status"] = _campaign_status(data.get("status"))
            if status and data["status"] != status:
                continue
            campaigns.append(data)
        return jsonify({"campaigns": campaigns, "count": len(campaigns)})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/campaigns/<campaign_id>", methods=["GET"])
def get_campaign(campaign_id):
    """Get a single campaign by ID, including contacts subcollection."""
    try:
        db  = _get_db()
        doc = db.collection("campaigns").document(campaign_id).get()
        if not doc.exists:
            return _err(f"Campaign '{campaign_id}' not found", 404)
        data = doc.to_dict()
        data["status"] = _campaign_status(data.get("status"))
        outreach_email = data.get("outreach_email_account", "")
        data["mail_account"] = _get_mail_account(db, outreach_email) or {}
        contacts_docs = db.collection("campaigns").document(campaign_id).collection("campaign_contacts").stream()
        contacts = []
        for c in contacts_docs:
            contact = c.to_dict() or {}
            contact["status"] = _contact_status(contact.get("status"))
            contacts.append(contact)
        data["campaign_contacts"] = contacts
        data["status_breakdown"] = dict(Counter(c.get("status", "pending") for c in contacts))
        return jsonify(data)
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/campaigns/<campaign_id>/create", methods=["POST"])
def create_campaign(campaign_id):
    """Create a new campaign document. Fails if already exists."""
    try:
        from handlers.shared import _unique_campaign_id
        db         = _get_db()
        actual_id  = _unique_campaign_id(db, campaign_id)
        renamed    = actual_id != campaign_id
        doc_ref    = db.collection("campaigns").document(actual_id)
        body  = request.get_json(silent=True) or {}
        now   = datetime.now(timezone.utc).isoformat()
        data  = {
            "campaign_id":            actual_id,
            "status":                 "draft",
            "sent_at":                None,
            "outreach_email_account": body.get("outreach_email_account", ""),
            "mail":                   {"subject": "", "body": "", "type": "plain"},
            "contact_count":          0,
            "sites_count":            0,
            "countries":              [],
            "status_breakdown":       {},
            "select_breakdown":       {},
            "tier_breakdown":         {},
            "outreach_breakdown":     {},
            "updated_at":             now,
        }
        doc_ref.set(data)
        msg = f"Campaign '{actual_id}' created"
        if renamed:
            msg += f" (renamed from '{campaign_id}' — already existed)"
        return _ok(msg, campaign=data, campaign_id=actual_id,
                   original_id=campaign_id, renamed=renamed)
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/campaigns/<campaign_id>", methods=["POST", "PATCH"])
def update_campaign(campaign_id):
    """Update a campaign document."""
    try:
        db   = _get_db()
        body = request.get_json(silent=True) or {}
        if not body:
            return _err("Request body is required", 400)

        doc_ref = db.collection("campaigns").document(campaign_id)
        doc     = doc_ref.get()
        if not doc.exists:
            return _err(f"Campaign '{campaign_id}' not found", 404)

        update = {}

        if "status" in body:
            raw_status = str(body["status"] or "").strip().lower()
            if raw_status not in _CAMPAIGN_STATUSES and raw_status not in _LEGACY_CAMPAIGN_STATUSES:
                return _err(f"Invalid status. Must be one of: {', '.join(sorted(_CAMPAIGN_STATUSES))}", 400)
            requested_status = _campaign_status(body["status"])
            valid = _CAMPAIGN_STATUSES
            current_status = _campaign_status((doc.to_dict() or {}).get("status", "draft"))
            allowed_next = {
                "draft":    {"ready", "canceled"},
                "ready":    {"active", "canceled"},
                "active":   {"canceled"},
                "canceled": set(),
            }
            if requested_status == current_status:
                update["status"] = requested_status
            elif requested_status in allowed_next.get(current_status, set()):
                update["status"] = requested_status
                if requested_status == "active" and not body.get("sent_at"):
                    update["sent_at"] = datetime.now(timezone.utc).isoformat()
            else:
                return _err(
                    f"Invalid campaign status transition: {current_status} -> {requested_status}.",
                    409,
                )

        if "sent_at"                in body: update["sent_at"]                = body["sent_at"]
        if "outreach_email_account" in body: update["outreach_email_account"] = body["outreach_email_account"]
        if "owner"                  in body: update["owner"]                  = body["owner"]

        if "mail" in body:
            existing_mail = (doc.to_dict() or {}).get("mail", {})
            merged_mail   = dict(existing_mail)
            merged_mail.update(body["mail"])
            update["mail"] = merged_mail

        # --- mail_schedule support -----------------------------------------
        # Full schedule replace: PATCH with {"mail_schedule": [...]}
        # Single-step upsert:   PATCH with {"mail_schedule_step": {...}}
        #   The step must contain a "step_id".  It is merged into (or appended
        #   to) the existing array.  To delete a step send {"delete_step": id}.
        if "mail_schedule" in body:
            sched = body["mail_schedule"]
            if not isinstance(sched, list):
                return _err("mail_schedule must be an array", 400)
            update["mail_schedule"] = sched

        if "mail_schedule_step" in body:
            step = body["mail_schedule_step"]
            if not isinstance(step, dict) or not step.get("step_id"):
                return _err("mail_schedule_step must be an object with step_id", 400)
            existing_schedule = list((doc.to_dict() or {}).get("mail_schedule") or [])
            idx = next((i for i, s in enumerate(existing_schedule)
                        if s.get("step_id") == step["step_id"]), None)
            if idx is not None:
                merged = dict(existing_schedule[idx])
                merged.update(step)
                existing_schedule[idx] = merged
            else:
                existing_schedule.append(step)
            update["mail_schedule"] = existing_schedule

        if "delete_step" in body:
            step_id = body["delete_step"]
            existing_schedule = list((doc.to_dict() or {}).get("mail_schedule") or [])
            update["mail_schedule"] = [s for s in existing_schedule
                                       if s.get("step_id") != step_id]

        if "imap" in body or "gmail" in body or "mail_account_type" in body:
            campaign_data    = doc.to_dict() or {}
            outreach_account = (
                body.get("outreach_email_account")
                or campaign_data.get("outreach_email_account", "")
            ).strip().lower()
            if outreach_account:
                existing_ma  = _get_mail_account(db, outreach_account) or {}
                account_type = body.get("mail_account_type") or existing_ma.get("account_type", "imap")
                if account_type == "imap":
                    merged = dict(existing_ma)
                    merged.update(body.get("imap", {}))
                    account_doc = {
                        "account_type": "imap",
                        "email":        outreach_account,
                        "host":         merged.get("host", ""),
                        "port":         merged.get("port", 993),
                        "username":     merged.get("username", ""),
                        "password":     merged.get("password", ""),
                        "ssl":          merged.get("ssl", True),
                    }
                else:
                    merged = dict(existing_ma)
                    merged.update(body.get("gmail", {}))
                    account_doc = {
                        "account_type":  "gmail",
                        "email":         outreach_account,
                        "client_id":     merged.get("client_id", ""),
                        "client_secret": merged.get("client_secret", ""),
                        "refresh_token": merged.get("refresh_token", ""),
                        "access_token":  merged.get("access_token", ""),
                    }
                account_doc["updated_at"] = datetime.now(timezone.utc).isoformat()
                settings_ma = db.collection("settings").document("mail_accounts")
                if not settings_ma.get().exists:
                    settings_ma.set({"_type": "mail_accounts"})
                settings_ma.collection("accounts").document(outreach_account).set(account_doc, merge=True)

        if not update:
            return _err("No valid fields to update", 400)

        update["updated_at"] = datetime.now(timezone.utc).isoformat()
        doc_ref.update(update)
        updated_doc = doc_ref.get().to_dict()
        return jsonify({"status": "ok", "message": f"Campaign '{campaign_id}' updated", "campaign": updated_doc})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/campaigns/<campaign_id>", methods=["DELETE"])
def delete_campaign(campaign_id):
    """Delete a draft campaign via a background job."""
    try:
        db  = _get_db()
        doc = db.collection("campaigns").document(campaign_id).get()
        if not doc.exists:
            return _err(f"Campaign '{campaign_id}' not found", 404)
        status = _campaign_status((doc.to_dict() or {}).get("status", ""))
        if status not in ("draft", "canceled"):
            return _err(f"Campaign '{campaign_id}' has status '{status}' — only draft or canceled campaigns can be deleted.", 409)

        from google.cloud import firestore as _fs
        camp_ref = db.collection("campaigns").document(campaign_id)

        @_fs.transactional
        def _claim(tx, ref):
            snap = ref.get(transaction=tx)
            if _campaign_status((snap.to_dict() or {}).get("status")) not in ("draft", "canceled"):
                raise ValueError("Status changed — delete aborted")
            tx.update(ref, {"status": "deleting"})

        _claim(db.transaction(), camp_ref)
        job_params = {"campaign_id": campaign_id}
        job_id     = _new_job("campaign-delete", job_params)
        _enqueue_task("campaign-delete", job_id, job_params)
        return _ok(f"Campaign '{campaign_id}' marked as deleting — job queued",
                   campaign_id=campaign_id, job_id=job_id,
                   poll=f"/api/crm/status/{job_id}")
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/campaigns/<campaign_id>/ping-mail-account", methods=["POST"])
def ping_mail_account(campaign_id):
    """Test the mail account configured on a campaign."""
    try:
        db  = _get_db()
        doc = db.collection("campaigns").document(campaign_id).get()
        if not doc.exists:
            return _err(f"Campaign '{campaign_id}' not found", 404)
        data           = doc.to_dict() or {}
        outreach_email = data.get("outreach_email_account", "")
        ma = _get_mail_account(db, outreach_email)
        if not ma:
            return _err(f"No mail account found for '{outreach_email}'.", 400)
        from crm.mail_sender import MailSender
        result = MailSender(ma).ping()
        return jsonify(result)
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/campaigns/<campaign_id>/send-test-mail", methods=["POST"])
def send_test_mail(campaign_id):
    """Send a test email using the campaign mail account settings."""
    try:
        db  = _get_db()
        doc = db.collection("campaigns").document(campaign_id).get()
        if not doc.exists:
            return _err(f"Campaign '{campaign_id}' not found", 404)
        data           = doc.to_dict() or {}
        outreach_email = data.get("outreach_email_account", "")
        ma             = _get_mail_account(db, outreach_email)
        if not ma:
            return _err(f"No mail account found for '{outreach_email}'.", 400)
        body       = request.get_json(silent=True) or {}
        to_addr    = body.get("to", "").strip()
        subject    = body.get("subject", "Test email").strip()
        body_html  = body.get("body_html", "").strip()
        body_plain = body.get("body_plain", body.get("body", "")).strip()
        if not to_addr:
            return _err("'to' is required", 400)
        from crm.mail_sender import MailSender
        result = MailSender(ma).send(to=to_addr, subject=subject,
                                     body_plain=body_plain, body_html=body_html)
        return jsonify(result)
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/discover-campaigns", methods=["GET"])
def discover_campaigns():
    """Scan the contact sheet for campaign IDs. Create + sync any new ones."""
    try:
        db  = _get_db()
        svc = _sheets_service()
        from crm.sheets_config import CONTACT_SHEET_ID, CONTACT_TAB
        result = svc.spreadsheets().values().get(
            spreadsheetId=CONTACT_SHEET_ID, range=f"{CONTACT_TAB}!A:ZZ"
        ).execute()
        rows = result.get("values", [])
        if not rows:
            return jsonify({"existing": [], "created": [], "message": "Sheet is empty"})

        headers  = [h.lower().replace(" ", "_") for h in rows[0]]
        camp_idx = next((i for i, h in enumerate(headers) if h == "campaign"), -1)
        if camp_idx < 0:
            return _err("No 'Campaign' column found in contact sheet", 400)

        sheet_campaigns = set()
        for row in rows[1:]:
            val = row[camp_idx].strip() if camp_idx < len(row) else ""
            if val:
                sheet_campaigns.add(val)

        if not sheet_campaigns:
            return jsonify({"existing": [], "created": [], "message": "No campaign IDs found in sheet"})

        existing_docs = {d.id for d in db.collection("campaigns").stream()}
        new_campaigns = sheet_campaigns - existing_docs
        existing      = list(sheet_campaigns & existing_docs)
        created       = []

        for campaign_id in sorted(new_campaigns):
            from handlers.shared import _unique_campaign_id
            actual_id = _unique_campaign_id(db, campaign_id)
            now  = datetime.now(timezone.utc).isoformat()
            data = {
                "campaign_id":            actual_id,
                "status":                 "draft",
                "sent_at":                None,
                "outreach_email_account": "",
                "mail":                   {"subject": "", "body": "", "type": "plain"},
                "contact_count":          0,
                "sites_count":            0,
                "countries":              [],
                "status_breakdown":       {},
                "select_breakdown":       {},
                "tier_breakdown":         {},
                "outreach_breakdown":     {},
                "updated_at":             now,
            }
            db.collection("campaigns").document(actual_id).set(data)
            job_id = _new_job("crm-sync", {"campaign_id": actual_id})
            _enqueue_task("crm-sync", job_id, {"campaign_id": actual_id})
            entry = {"campaign_id": actual_id, "job_id": job_id}
            if actual_id != campaign_id:
                entry["original_id"] = campaign_id
                entry["renamed"] = True
            created.append(entry)

        msg = (
            f"Found {len(sheet_campaigns)} campaign(s) in sheet. {len(new_campaigns)} new — sync jobs queued."
            if new_campaigns else
            f"All {len(sheet_campaigns)} campaign(s) already exist."
        )
        return jsonify({"existing": sorted(existing), "created": created, "message": msg})
    except Exception as exc:
        return _err(str(exc), 500)
