"""handlers/statistics.py — Statistics endpoints."""
from __future__ import annotations
from flask import Blueprint, request, jsonify
from handlers.shared import _get_db, _new_job, _enqueue_task, _accepted, _err

bp = Blueprint("statistics", __name__)


@bp.route("/api/crm/statistics/collect", methods=["POST"])
def collect_statistics():
    """Queue a job to run all statistics aggregations."""
    try:
        body   = request.get_json(silent=True) or {}
        only   = body.get("only", "")
        params = {"only": only} if only else {}
        job_id = _new_job("statistics", params)
        _enqueue_task("statistics", job_id, params)
        return _accepted(job_id, "statistics")
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/statistics", methods=["GET"])
def get_statistics():
    """Return all statistics documents from the statistics collection."""
    try:
        db   = _get_db()
        col  = db.collection("statistics")
        docs = {d.id: d.to_dict() for d in col.stream() if d.exists}

        if "priority-pr-country" in docs:
            countries = {
                d.id: d.to_dict()
                for d in col.document("priority-pr-country").collection("countries").stream()
            }
            docs["priority-pr-country"]["countries"] = countries

        return jsonify({"status": "ok", "statistics": docs, "doc_count": len(docs)})
    except Exception as exc:
        return _err(str(exc), 500)
