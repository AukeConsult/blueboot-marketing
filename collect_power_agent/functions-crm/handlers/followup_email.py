"""handlers/followup_email.py — Follow-up email sync job trigger."""
from __future__ import annotations
from flask import Blueprint, request
from handlers.shared import _new_job, _enqueue_task, _accepted, _err

bp = Blueprint("followup_email", __name__)


@bp.route("/api/crm/followup-email-sync", methods=["POST"])
def followup_email_sync():
    """Trigger a background job that syncs email history into contact follow-up logs."""
    try:
        body   = request.get_json(silent=True) or {}
        params = {
            "campaign_id":      (body.get("campaign_id")      or "").strip(),
            "contact_doc_id":   (body.get("contact_doc_id")   or "").strip(),
            "outreach_account": (body.get("outreach_account") or "").strip(),
            "days":             int(body.get("days") or 7),
        }
        job_id = _new_job("followup-email-sync", params)
        _enqueue_task("followup-email-sync", job_id, params)
        return _accepted(job_id, "followup-email-sync")
    except Exception as exc:
        return _err(str(exc), 500)
