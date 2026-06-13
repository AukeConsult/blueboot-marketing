"""handlers/filter_facets.py — Filter facets endpoints."""
from __future__ import annotations
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from handlers.shared import _get_db, _new_job, _enqueue_task, _accepted, _ok, _err

FILTER_FACETS_COLLECTION = "filter_facets"

bp = Blueprint("filter_facets", __name__)


@bp.route("/api/crm/filter-facets", methods=["GET"])
def list_filter_facets():
    try:
        db  = _get_db()
        out = []
        for d in db.collection(FILTER_FACETS_COLLECTION).stream():
            data = d.to_dict() or {}
            out.append({
                "name":              d.id,
                "source_collection": data.get("source_collection"),
                "generated_at":      data.get("generated_at"),
                "saved_at":          data.get("saved_at"),
                "lead_count":        data.get("lead_count"),
                "contact_count":     data.get("contact_count"),
            })
        out.sort(key=lambda x: x["name"])
        return jsonify({"facets": out, "count": len(out)})
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/filter-facets/<name>", methods=["GET"])
def get_filter_facets(name):
    try:
        db  = _get_db()
        doc = db.collection(FILTER_FACETS_COLLECTION).document(name).get()
        if not doc.exists:
            return _err(f"filter_facets/'{name}' not found", 404)
        return jsonify(doc.to_dict())
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/filter-facets/<name>", methods=["POST", "PATCH"])
def save_filter_facets(name):
    try:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict) or "filters" not in body:
            return _err("body must be a filter-facets object containing a 'filters' key")
        db = _get_db()
        body["name"]     = name
        body["saved_at"] = datetime.now(timezone.utc).isoformat()
        db.collection(FILTER_FACETS_COLLECTION).document(name).set(body, merge=False)
        job_params = {"name": name}
        job_id = _new_job("filter-count", job_params)
        _enqueue_task("filter-count", job_id, job_params)
        return _ok(f"Saved filter_facets/'{name}'", name=name,
                   saved_at=body["saved_at"], job_id=job_id,
                   poll=f"/api/crm/status/{job_id}")
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/filter-facets/<name>/create-campaign", methods=["POST"])
def create_campaign_from_facet(name):
    try:
        body        = request.get_json(silent=True) or {}
        campaign_id = (body.get("campaign_id") or "").strip()
        if not campaign_id:
            return _err("'campaign_id' is required in the request body", 400)
        dry_run    = bool(body.get("dry_run", False))
        job_params = {"facet_name": name, "campaign_id": campaign_id, "dry_run": dry_run}
        job_id     = _new_job("facet-campaign", job_params)
        _enqueue_task("facet-campaign", job_id, job_params)
        return _ok(f"Queued facet-campaign job for facet='{name}' → campaign='{campaign_id}'",
                   job_id=job_id, poll=f"/api/crm/status/{job_id}")
    except Exception as exc:
        return _err(str(exc), 500)
