"""handlers/leads.py — Lead lookup, exclusion, and name-enrich endpoints."""
from __future__ import annotations
import re as _re
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from handlers.shared import _get_db, _new_job, _enqueue_task, _ok, _err

bp = Blueprint("leads", __name__)

LEAD_FIELDS = [
    "company", "website", "location", "location_country",
    "ai_company_type", "ai_sector", "ai_platform", "page_count",
    "title", "description", "ai_summary", "ai_confidence",
    "ai_client_base", "reseller_score", "ai_specialisation",
    "ai_reseller_potential", "keywords",
]


def _lead_id(domain_str: str) -> str:
    slug = _re.sub(r"[.\-]+", "_", domain_str.rstrip(".").lower())
    return _re.sub(r"_+", "_", slug).strip("_")


@bp.route("/api/crm/leads/by-domain/<path:domain>", methods=["GET"])
def lead_by_domain(domain):
    """Fetch lead data for a domain — checks site_leads first, then leads."""
    try:
        db      = _get_db()
        lead_id = _lead_id(domain)

        snap = db.collection("site_leads").document(lead_id).get()
        if snap.exists:
            data = {k: v for k, v in (snap.to_dict() or {}).items()
                    if k in LEAD_FIELDS and v not in (None, "", [], {})}
            data["source_pipeline"] = "site_leads"
            return jsonify(data)

        snap = db.collection("leads").document(lead_id).get()
        if snap.exists:
            data = {k: v for k, v in (snap.to_dict() or {}).items()
                    if k in LEAD_FIELDS and v not in (None, "", [], {})}
            data["source_pipeline"] = "leads"
            return jsonify(data)

        return jsonify({"source_pipeline": None})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/leads/by-domain/<path:domain>/exclude", methods=["POST"])
def exclude_lead_by_domain(domain):
    """Add a domain to sites_excluded or leads_excluded."""
    try:
        db     = _get_db()
        body   = request.get_json(silent=True) or {}
        reason = (body.get("reason") or "manual").strip()
        lead_id_val = _lead_id(domain)
        pipeline = (body.get("pipeline") or "").strip()

        if not pipeline:
            if db.collection("site_leads").document(lead_id_val).get().exists:
                pipeline = "site_leads"
            elif db.collection("leads").document(lead_id_val).get().exists:
                pipeline = "leads"
            else:
                pipeline = "site_leads"

        now = datetime.now(timezone.utc).isoformat()
        if pipeline == "leads":
            db.collection("leads_excluded").document(lead_id_val).set(
                {"lead_id": lead_id_val, "domain": domain, "reason": reason, "excluded_at": now},
                merge=True)
        else:
            snap = db.collection("site_leads").document(lead_id_val).get()
            data = snap.to_dict() or {} if snap.exists else {}
            db.collection("sites_excluded").document(lead_id_val).set({
                "lead_id": lead_id_val, "domain": domain,
                "website": data.get("website", ""), "country": data.get("country", ""),
                "page_count": data.get("page_count", 0), "reason": reason, "excluded_at": now,
            }, merge=True)

        return _ok(f"Domain '{domain}' added to {pipeline} excluded list",
                   pipeline=pipeline, lead_id=lead_id_val)
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/name-enrich", methods=["POST"])
def name_enrich():
    """Enrich a list of email addresses with names using rules + Bing + AI."""
    try:
        body    = request.get_json(silent=True) or {}
        emails  = body.get("emails") or []
        dry_run = bool(body.get("dry_run", False))
        skip_ai = bool(body.get("skip_ai", False))
        if not emails or not isinstance(emails, list):
            return _err("'emails' must be a non-empty list", 400)
        if len(emails) > 50:
            return _err("Maximum 50 emails per request", 400)

        db = _get_db()
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "..", "..", "app"))
        from campaign_name_enrich import enrich_email_list
        result = enrich_email_list(emails, db=db, dry_run=dry_run, skip_ai=skip_ai)
        return jsonify({
            "results": result.get("resolved", {}),
            "stats":   {k: v for k, v in result.items() if k != "resolved"},
        })
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/campaigns/<campaign_id>/name-enrich", methods=["POST"])
def name_enrich_campaign(campaign_id):
    """Enrich all contacts in a specific campaign (background job)."""
    try:
        body       = request.get_json(silent=True) or {}
        dry_run    = bool(body.get("dry_run", False))
        skip_ai    = bool(body.get("skip_ai", False))
        job_params = {"campaign_id": campaign_id, "emails": [],
                      "dry_run": dry_run, "skip_ai": skip_ai}
        job_id     = _new_job("name-enrich", job_params)
        _enqueue_task("name-enrich", job_id, job_params)
        from handlers.shared import _accepted
        return _accepted(job_id, "name-enrich")
    except Exception as exc:
        return _err(str(exc), 500)
