"""handlers/followup_email.py — Follow-up email sync job trigger."""
from __future__ import annotations
import re
from flask import Blueprint, request
from handlers.shared import _new_job, _enqueue_task, _accepted, _err

bp = Blueprint("followup_email", __name__)


def _split_list_value(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = [value]
    out: list[str] = []
    for item in values:
        out.extend(v.strip() for v in re.split(r"[,;|\n]", str(item)) if v.strip())
    return out


@bp.route("/api/crm/followup-email-sync", methods=["POST"])
def followup_email_sync():
    """Trigger a background job that syncs email history into contact follow-up logs."""
    try:
        body   = request.get_json(silent=True) or {}
        campaign_ids = _split_list_value(
            body.get("campaign_ids")
            or body.get("campaigns")
            or body.get("campaign_id")
        )
        params = {
            "campaign_ids":     campaign_ids,
            "contact_doc_id":   (body.get("contact_doc_id")   or "").strip(),
            "outreach_account": (body.get("outreach_account") or "").strip(),
            "days":             int(body.get("days") or 7),
        }
        job_id = _new_job("followup-email-sync", params)
        _enqueue_task("followup-email-sync", job_id, params)
        return _accepted(job_id, "followup-email-sync")
    except Exception as exc:
        return _err(str(exc), 500)
