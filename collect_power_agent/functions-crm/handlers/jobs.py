"""handlers/jobs.py — Job trigger endpoints, worker dispatcher, status/list."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, jsonify
from handlers.shared import (
    _get_db, _sheets_service, _gdisk, _jobs_col,
    _new_job, _update_job, _enqueue_task,
    _accepted, _ok, _err,
)

bp = Blueprint("jobs", __name__)


# -- Trigger endpoints --------------------------------------------------------

@bp.route("/api/crm/contact-sync", methods=["GET"])
def contact_sync():
    countries_raw = request.args.get("countries", "NO")
    params = {
        "countries": [c.strip().upper() for c in countries_raw.split(",") if c.strip()],
        "max_rows":  request.args.get("max", type=int),
        "status":    request.args.get("status"),
        "campaign":  request.args.get("campaign"),
        "min_pages": request.args.get("min_pages", type=int),
        "max_pages": request.args.get("max_pages", type=int),
    }
    try:
        job_id = _new_job("contact-sync", params)
        _enqueue_task("contact-sync", job_id, params)
        return _accepted(job_id, "contact-sync")
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/push-and-sync", methods=["GET"])
def push_and_sync():
    try:
        job_id = _new_job("push-and-sync", {})
        _enqueue_task("push-and-sync", job_id, {})
        return _accepted(job_id, "push-and-sync")
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/template-sync", methods=["GET"])
def template_sync():
    try:
        job_id = _new_job("template-sync", {})
        _enqueue_task("template-sync", job_id, {})
        return _accepted(job_id, "template-sync")
    except Exception as exc:
        return _err(str(exc), 500)



@bp.route("/api/crm/crm-sync", methods=["GET"])
def crm_sync_trigger():
    """Trigger a CRM sync from the master contact sheet.

    Optional: ?campaign_id=X  to sync only one campaign.
    Returns a job_id to poll via GET /api/crm/status/<job_id>.
    """
    try:
        campaign_id = request.args.get("campaign_id", "").strip()
        params = {"campaign_id": campaign_id}
        job_id = _new_job("crm-sync", params)
        _enqueue_task("crm-sync", job_id, params)
        return _accepted(job_id, "crm-sync")
    except Exception as exc:
        return _err(str(exc), 500)

@bp.route("/api/crm/campaign-sync", methods=["GET"])
def campaign_sync():
    """Sync campaign data from contact sheet -> Firestore.
    Required: ?campaign_id=NO_jun
    Optional: ?force=true
    """
    campaign_id = request.args.get("campaign_id", "").strip()
    if not campaign_id:
        return _err("campaign_id is required e.g. ?campaign_id=NO_jun", 400)
    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    try:
        job_id = _new_job("campaign-sync", {"campaign_id": campaign_id, "force": force})
        _enqueue_task("campaign-sync", job_id, {"campaign_id": campaign_id, "force": force})
        return _accepted(job_id, "campaign-sync")
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/campaign-export", methods=["GET"])
def campaign_export():
    """Export a campaign + its contacts to a Sheet (named after the campaign)
    in the gdisk Drive folder. Required: ?campaign_id=NO_jun"""
    campaign_id = request.args.get("campaign_id", "").strip()
    if not campaign_id:
        return _err("campaign_id is required")
    try:
        params = {"campaign_id": campaign_id}
        job_id = _new_job("campaign-export", params)
        _enqueue_task("campaign-export", job_id, params)
        return _accepted(job_id, "campaign-export")
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/worker/<name>/<job_id>", methods=["POST"])
def worker(name, job_id):
    try:
        _update_job(job_id,
                    status="running",
                    started_at=datetime.now(timezone.utc).isoformat())
        body = request.get_json(silent=True) or {}
        db   = _get_db()
        svc  = _sheets_service()

        if name == "contact-sync":
            from crm.contact_sync_lib import run_contact_sync
            added  = run_contact_sync(
                db=db, svc=svc,
                countries=body.get("countries", ["NO"]),
                status=body.get("status"),
                campaign=body.get("campaign"),
                max_rows=body.get("max_rows"),
                min_pages=body.get("min_pages"),
                max_pages=body.get("max_pages"),
            )
            result = {"added": added, "countries": body.get("countries", ["NO"])}

        elif name == "push-and-sync":
            from crm.push_and_sync_lib import run_push_and_sync
            result = run_push_and_sync(db=db, svc=svc)

        elif name == "template-sync":
            from crm.crm_template_sync_lib import run_template_sync
            count  = run_template_sync(db=db, svc=svc)
            result = {"synced": count}

        elif name == "crm-sync":
            from crm.crm_sync_lib import run_crm_sync
            result = run_crm_sync(db=db, svc=svc,
                                  campaign_id=body.get("campaign_id", ""))

        elif name == "statistics":
            from crm.statistics_builder import StatisticsBuilder
            only = body.get("only", "")
            sb   = StatisticsBuilder(db=db)
            if only == "leads-overview":
                result = sb.leads_overview()
            elif only == "site-leads-overview":
                result = sb.site_leads_overview()
            elif only == "site-funnel":
                result = sb.site_pipeline_enrichment_funnel()
            elif only == "lead-funnel":
                result = sb.lead_pipeline_enrichment_funnel()
            elif only == "quality":
                result = sb.data_quality_report()
            elif only == "email-funnel":
                result = sb.email_contacts_funnel()
            elif only == "coverage":
                result = sb.pipeline_coverage()
            elif only == "campaigns":
                result = sb.campaign_statistics()
            else:
                sb.leads_overview()
                sb.site_leads_overview()
                sb.site_pipeline_enrichment_funnel()
                sb.lead_pipeline_enrichment_funnel()
                sb.data_quality_report()
                sb.email_contacts_funnel()
                sb.pipeline_coverage()
                sb.campaign_statistics()
                result = {"collected": True}

        elif name == "campaign-sync":
            from crm.campaign_sync_lib import run_campaign_sync
            result = run_campaign_sync(db=db, svc=svc, gd=_gdisk(),
                                       campaign_id=body.get("campaign_id", ""))

        elif name == "filter-count":
            from crm.filter_count_lib import run_filter_count, run_leads_filter_count
            fname = body.get("name", "")
            # Auto-detect pipeline from facet doc
            facet_snap = db.collection("filter_facets").document(fname).get()
            pipeline = (facet_snap.to_dict() or {}).get("pipeline", "site_leads") if facet_snap.exists else "site_leads"
            if pipeline == "leads":
                counts = run_leads_filter_count(db=db, name=fname)
            else:
                counts = run_filter_count(db=db, name=fname)
            result = {"name": fname, "counts": counts, "pipeline": pipeline}

        elif name == "campaign-delete":
            from crm.campaign_delete_lib import run_campaign_delete
            result = run_campaign_delete(db=db, campaign_id=body.get("campaign_id", ""))

        elif name == "facet-campaign":
            from crm.facet_campaign_lib import run_facet_campaign
            result = run_facet_campaign(
                db=db,
                facet_name=body.get("facet_name", ""),
                campaign_id=body.get("campaign_id", ""),
                dry_run=bool(body.get("dry_run", False)),
            )

        elif name == "campaign-export":
            from crm.campaign_export_lib import run_campaign_export
            result = run_campaign_export(db=db, svc=svc, gd=_gdisk(),
                                         campaign_id=body.get("campaign_id", ""))
            # Persist sheet_url on the campaign document for quick access
            cid = body.get("campaign_id", "")
            if cid and result.get("url"):
                db.collection("campaigns").document(cid).update({
                    "sheet_url":  result["url"],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })

        elif name == "name-enrich":
            from crm.name_enrich_lib import enrich_email_list, _enrich, _doc_id_from_email
            import asyncio as _asyncio
            campaign_id = body.get("campaign_id", "").strip()
            emails      = body.get("emails") or []
            dry_run     = bool(body.get("dry_run", False))
            skip_ai     = bool(body.get("skip_ai", False))
            if campaign_id:
                camp_ref = db.collection("campaigns").document(campaign_id)
                contacts = []
                for doc in camp_ref.collection("campaign_contacts").stream():
                    data = doc.to_dict() or {}
                    if data.get("name", "").strip():
                        continue
                    email = (data.get("email") or "").strip()
                    if not email:
                        continue
                    contacts.append({
                        "doc_id":       doc.id,
                        "email":        email,
                        "domain":       email.split("@")[1] if "@" in email else "",
                        "campaign_ref": doc.reference,
                        "ec_doc_id":    _doc_id_from_email(email),
                    })
                result = _asyncio.run(_enrich(
                    db, contacts,
                    dry_run=dry_run, skip_ai=skip_ai,
                    model="gpt-4o-mini", batch_size=5,
                    skip_ec_lookup=False, propagate_to_campaigns=False,
                ))
            else:
                result = enrich_email_list(
                    emails, db=db, dry_run=dry_run, skip_ai=skip_ai,
                )

        elif name == "followup-email-sync":
            from crm.followup_email_sync_lib import run_followup_email_sync
            result = run_followup_email_sync(
                db             = db,
                campaign_id    = body.get("campaign_id")    or None,
                contact_doc_id = body.get("contact_doc_id") or None,
                days           = int(body.get("days") or 7),
            )

        else:
            _update_job(job_id, status="error",
                        error=f"Unknown job: {name}",
                        finished_at=datetime.now(timezone.utc).isoformat())
            return _err(f"Unknown job: {name}", 400)

        _update_job(job_id,
                    status="done",
                    result=result,
                    finished_at=datetime.now(timezone.utc).isoformat())
        return jsonify({"status": "ok", "message": f"Job {job_id} done", "result": result})

    except Exception as exc:
        _update_job(job_id,
                    status="error",
                    error=str(exc),
                    finished_at=datetime.now(timezone.utc).isoformat())
        return _err(str(exc), 500)

# -- Status endpoints ---------------------------------------------------------

@bp.route("/api/crm/status/<job_id>", methods=["GET"])
def job_status(job_id):
    doc = _jobs_col().document(job_id).get()
    if not doc.exists:
        return _err(f"Job '{job_id}' not found", 404)
    return jsonify(doc.to_dict())


@bp.route("/api/crm/jobs", methods=["GET"])
def list_jobs():
    """List recent jobs sorted by queued_at descending.
    ?limit=20       max results (default 20, max 500)
    ?running=true   only return running or queued jobs
    ?campaign_id=X  only return jobs for a specific campaign
    """
    limit       = min(int(request.args.get("limit", 20)), 500)
    running     = request.args.get("running", "").lower() in ("1", "true", "yes")
    campaign_id = request.args.get("campaign_id", "").strip()

    query = _jobs_col().order_by("queued_at", direction="DESCENDING")

    # Compute cutoff time if since parameter given
    since_minutes = request.args.get("since", type=int)
    cutoff = None
    if since_minutes:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).isoformat()

    if running:
        from google.cloud.firestore_v1.base_query import FieldFilter as FF
        queued       = list(_jobs_col().where(filter=FF("status", "==", "queued")).stream())
        running_docs = list(_jobs_col().where(filter=FF("status", "==", "running")).stream())
        all_jobs = [d.to_dict() for d in queued + running_docs]
        # Filter by campaign_id
        if campaign_id:
            all_jobs = [j for j in all_jobs if (j.get("params") or {}).get("campaign_id") == campaign_id]
        # Filter by time window (ignore stale jobs)
        if cutoff:
            all_jobs = [j for j in all_jobs if (j.get("queued_at") or "") >= cutoff]
        # Only truly active statuses
        all_jobs = [j for j in all_jobs if j.get("status") in ("queued", "running")]
        all_jobs.sort(key=lambda j: j.get("queued_at", ""), reverse=True)
        return jsonify({"jobs": all_jobs[:limit], "count": len(all_jobs)})

    docs = list(query.limit(limit).stream())
    jobs = [d.to_dict() for d in docs]
    if campaign_id:
        jobs = [j for j in jobs if (j.get("params") or {}).get("campaign_id") == campaign_id]
    if cutoff:
        jobs = [j for j in jobs if (j.get("queued_at") or "") >= cutoff]
    return jsonify({"jobs": jobs, "count": len(jobs)})
