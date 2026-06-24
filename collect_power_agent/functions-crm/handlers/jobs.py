"""handlers/jobs.py — Job trigger endpoints, worker dispatcher, status/list."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, jsonify
from google.cloud.firestore_v1.base_query import FieldFilter
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


@bp.route("/api/crm/campaign-import", methods=["POST"])
def campaign_import():
    """Import leads + contacts from an uploaded Excel file into a campaign.

    Multipart form fields:
      campaign_id  -- target campaign (created if missing)
      file         -- .xlsx file with a 'Leads+Contacts' tab
      dry_run      -- 'true' | '1' to preview counts without writing (default: false)
    """
    campaign_id = (request.form.get("campaign_id") or "").strip()
    # campaign_id may be empty — the sheet's Campaign column is used as fallback

    if "file" not in request.files:
        return _err("file is required (multipart field 'file')", 400)

    uploaded = request.files["file"]
    if not uploaded.filename or not uploaded.filename.lower().endswith(".xlsx"):
        return _err("file must be a .xlsx Excel file", 400)

    dry_run = request.form.get("dry_run", "").lower() in ("1", "true", "yes")

    try:
        from crm.campaign_import_lib import parse_sheet, run_campaign_import
        file_bytes = uploaded.read()
        rows, warnings, detected_campaign_id = parse_sheet(file_bytes)
        db = _get_db()
        result = run_campaign_import(
            db, campaign_id, rows,
            dry_run=dry_run,
            detected_campaign_id=detected_campaign_id,
        )
        result["warnings"] = warnings
        result["rows_parsed"] = len(rows)
        return jsonify({"status": "ok", **result})
    except Exception as exc:
        return _err(str(exc), 500)


def _split_list_param(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = [value]
    out: list[str] = []
    import re
    for item in items:
        out.extend(v.strip() for v in re.split(r"[,;|\s]+", str(item)) if v.strip())
    return out


def _request_data() -> dict:
    body = request.get_json(silent=True) or {}
    return {**request.args.to_dict(flat=False), **body}


def _first_param(data: dict, name: str, default=None):
    value = data.get(name, default)
    if isinstance(value, list):
        return value[0] if value else default
    return value


@bp.route("/api/crm/outreach-send", methods=["GET", "POST"])
@bp.route("/outreach-send", methods=["GET", "POST"])
def outreach_send():
    """Trigger automatic outreach sending through the CRM worker.

    Params:
      mode= intro | followup | both
      limit= number of contacts per pass
      campaigns= optional comma/space/semicolon/pipe separated campaign ids
      dry_run= true to select/render without sending or confirming
      preview= true to include rendered snippets in worker logs
    """
    data = _request_data()
    mode = str(_first_param(data, "mode", "both") or "both").strip().lower()
    if mode not in {"intro", "followup", "both"}:
        return _err("mode must be one of: intro, followup, both", 400)
    campaigns_raw = (
        data.get("campaigns")
        or data.get("campaign_ids")
        or data.get("campaign_id")
    )
    params = {
        "mode": mode,
        "limit": int(_first_param(data, "limit", 500) or 500),
        "campaign_ids": _split_list_param(campaigns_raw),
        "dry_run": str(_first_param(data, "dry_run", "") or "").lower() in ("1", "true", "yes"),
        "preview": str(_first_param(data, "preview", "") or "").lower() in ("1", "true", "yes"),
    }
    try:
        job_id = _new_job("outreach-send", params)
        _enqueue_task("outreach-send", job_id, params)
        return _accepted(job_id, "outreach-send")
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/reply_match", methods=["GET", "POST"])
@bp.route("/api/crm/reply-match", methods=["GET", "POST"])
@bp.route("/reply-match", methods=["GET", "POST"])
def reply_match():
    """Trigger one reply matching pass through the CRM worker.

    Params:
      limit=     max messages to process per account (default 200)
      accounts=  optional comma/space/semicolon/pipe separated account emails
      campaigns= optional comma/space/semicolon/pipe separated campaign ids
      days=      how many days back to search IMAP (default 30)
      dry_run=   true to find matches without writing or deleting anything
    """
    data = _request_data()
    accounts_raw  = data.get("accounts") or data.get("account")
    campaigns_raw = (
        data.get("campaigns")
        or data.get("campaign_ids")
        or data.get("campaign_id")
    )
    params = {
        "limit":     int(_first_param(data, "limit", 200) or 200),
        "accounts":  _split_list_param(accounts_raw),
        "campaigns": _split_list_param(campaigns_raw),
        "days":      int(_first_param(data, "days", 30) or 30),
        "dry_run":   str(_first_param(data, "dry_run", "") or "").lower() in ("1", "true", "yes"),
    }
    try:
        job_id = _new_job("reply-match", params)
        _enqueue_task("reply-match", job_id, params)
        return _accepted(job_id, "reply-match")
    except Exception as exc:
        return _err(str(exc), 500)


@bp.route("/api/crm/build-facets", methods=["GET", "POST"])
def build_facets_trigger():
    """Rebuild a base filter-facets catalog from current data.

    Params: pipeline=site_leads|leads (default site_leads), cap=N (default 300).
    Returns a job_id to poll via GET /api/crm/status/<job_id>.
    """
    data = _request_data()
    pipeline = str(_first_param(data, "pipeline", "site_leads") or "site_leads").strip().lower()
    if pipeline not in ("site_leads", "leads"):
        return _err("pipeline must be 'site_leads' or 'leads'", 400)
    params = {"pipeline": pipeline, "cap": int(_first_param(data, "cap", 300) or 300)}
    try:
        job_id = _new_job("build-facets", params)
        _enqueue_task("build-facets", job_id, params)
        return _accepted(job_id, "build-facets")
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

        elif name == "scrape-emails":
            from crm.campaign_scrape_lib import run_campaign_scrape
            dry_run = bool(body.get("dry_run", False))
            result = run_campaign_scrape(
                db,
                campaign_id=body.get("campaign_id", "").strip(),
                force=bool(body.get("force", False)),
                workers=int(body.get("workers", 6)),
                dry_run=dry_run,
            )

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

        elif name == "inbound-read":
            # One sync runs BOTH single-source readers:
            #   • reply_matcher.match_new_replies — the ONLY reader of replies (INBOX)
            #   • inbound_read_lib.run_sent_sync  — the ONLY reader of sent mail (SENT)
            from smart_mail.reply_matcher import match_new_replies
            from smart_mail.inbound_read_lib import run_sent_sync
            _camps = (
                body.get("campaign_ids")
                or body.get("campaigns")
                or body.get("campaign_id")
                or None
            )
            _days = int(body.get("days") or 7)
            reply_res = match_new_replies(campaigns=_camps, days=_days)
            sent_res = run_sent_sync(
                db               = db,
                campaign_ids     = _camps,
                contact_doc_id   = body.get("contact_doc_id")   or None,
                outreach_account = body.get("outreach_account") or None,
                days             = _days,
            )
            _replies = int(reply_res.get("matched", 0)) + int(reply_res.get("bounced", 0))
            result = {
                "reply": reply_res,
                "sent":  sent_res,
                "synced_entries":   _replies + int(sent_res.get("synced_entries", 0)),
                "updated_contacts": _replies + int(sent_res.get("updated_contacts", 0)),
            }

        elif name == "outreach-send":
            from smart_mail.outreach_sender import send_outreach
            mode = str(body.get("mode") or "both").strip().lower()
            modes = ["intro", "followup"] if mode == "both" else [mode]
            passes = []
            for pass_mode in modes:
                passes.append(send_outreach(
                    mode         = pass_mode,
                    limit        = int(body.get("limit") or 500),
                    campaign_ids = body.get("campaign_ids") or None,
                    dry_run      = bool(body.get("dry_run", False)),
                    preview      = bool(body.get("preview", False)),
                ))
            result = {
                "mode": mode,
                "passes": passes,
                "sent": sum(int(p.get("sent", 0)) for p in passes),
                "failed": sum(int(p.get("failed", 0)) for p in passes),
                "skipped": sum(int(p.get("skipped", 0)) for p in passes),
                "would_send": sum(int(p.get("would_send", 0)) for p in passes),
            }

        elif name == "reply-match":
            from smart_mail.reply_matcher import match_new_replies
            result = match_new_replies(
                limit     = int(body.get("limit") or 200),
                accounts  = body.get("accounts") or None,
                campaigns = body.get("campaigns") or None,
                days      = int(body.get("days") or 30),
                dry_run   = bool(body.get("dry_run", False)),
            )

        elif name == "campaign-move":
            from crm.campaign_move_lib import run_campaign_move
            result = run_campaign_move(
                db                 = db,
                src_campaign_id    = body.get("src_campaign_id", ""),
                doc_ids            = body.get("doc_ids", []),
                target_campaign_id = body.get("target_campaign_id", ""),
                new_campaign_name  = body.get("new_campaign_name", ""),
                user               = body.get("user", "api"),
            )

        elif name == "build-facets":
            from crm.build_facets_lib import run_build_facets
            result = run_build_facets(
                db=db,
                pipeline=body.get("pipeline", "site_leads"),
                cap=int(body.get("cap") or 300),
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
    limit = min(int(request.args.get("limit", 20)), 500)
    running = request.args.get("running", "").lower() in ("1", "true", "yes")
    campaign_id = request.args.get("campaign_id", "").strip()

    query = _jobs_col().order_by("queued_at", direction="DESCENDING")

    since_minutes = request.args.get("since", type=int)
    if since_minutes:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).isoformat()
        query = query.where(filter=FieldFilter("queued_at", ">=", cutoff))

    if running:
        query = query.where(filter=FieldFilter("status", "in", ["queued", "running"]))

    docs = list(query.limit(limit).stream())
    jobs = []
    for d in docs:
        j = d.to_dict()
        if campaign_id and (j.get("params") or {}).get("campaign_id") != campaign_id:
            continue
        jobs.append({**j, "job_id": d.id})

    return jsonify({"status": "ok", "jobs": jobs})
