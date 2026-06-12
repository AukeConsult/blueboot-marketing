"""handlers/inbound_read.py - inbound mail read job trigger."""
from __future__ import annotations

import re

from flask import Blueprint, request

from handlers.shared import _accepted, _enqueue_task, _err, _new_job

bp = Blueprint("inbound_read", __name__)


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


@bp.route("/api/crm/inbound_read", methods=["POST"])
@bp.route("/inbound_read", methods=["POST"])
def inbound_read():
    """Trigger a background job that reads inbound/sent mail into contact logs."""
    try:
        body = request.get_json(silent=True) or {}
        campaign_ids = _split_list_value(
            body.get("campaign_ids")
            or body.get("campaigns")
            or body.get("campaign_id")
        )
        params = {
            "campaign_ids": campaign_ids,
            "contact_doc_id": (body.get("contact_doc_id") or "").strip(),
            "outreach_account": (body.get("outreach_account") or "").strip(),
            "days": int(body.get("days") or 7),
        }
        job_id = _new_job("inbound-read", params)
        _enqueue_task("inbound-read", job_id, params)
        return _accepted(job_id, "inbound-read")
    except Exception as exc:
        return _err(str(exc), 500)

