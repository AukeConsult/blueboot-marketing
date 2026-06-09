# functions-smartmail/main.py
"""
Cloud Function entry point for the smart-mail service.

Mirrors functions-crm/main.py's pattern: a single Flask `app` (CORS-enabled)
exposed through one `@https_fn.on_request` wrapper that dispatches into Flask
via `app.request_context(...)`. Two routes replace the two always-on polling
loops in app/smart-mail/ with one-pass HTTP endpoints meant to be triggered
periodically by Cloud Scheduler (cheaper than an always-on worker, easy to
pause/resume, fits this project's existing serverless pattern):

  POST/GET /run-campaigns  -- one pass of smart_campaign_worker.py's loop body
                              (find queued campaigns, send_campaign() each)
  POST/GET /run-replies    -- one pass of smart_reply_worker.py's loop body
                              (poll mailboxes, match new replies)
  GET      /healthz        -- trivial liveness check

The SMTP password is injected as the Secret Manager secret
SALES_SMTP_PASSWORD (declared below); all other mail/tuning config comes
from .env.<project-id>, loaded natively by the Cloud Functions runtime.
"""
import os

from flask import Flask, request, jsonify
from flask_cors import CORS
from firebase_functions import https_fn, options

from smart_mail.firestore_client import get_firestore
from smart_mail.smart_campaign_sender import send_campaign
from smart_mail.smart_reply_matcher import match_new_replies

app = Flask(__name__)
CORS(app)

GCP_PROJECT = os.getenv("GCP_PROJECT", "blueboot-market")
GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1")


def _ok(data=None, **extra):
    payload = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return jsonify(payload), 200


def _err(message, status=400):
    return jsonify({"ok": False, "error": str(message)}), status


def _find_queued_campaigns(db):
    return list(db.collection("campaigns").where("status", "==", "queued").stream())


@app.route("/run-campaigns", methods=["GET", "POST"])
def run_campaigns():
    """
    One-pass replacement for smart_campaign_worker.py's `while True` loop body.
    Finds every campaign with status=="queued" and runs send_campaign() on
    each -- which itself enforces the per-account send-rate budget and the
    bounce-rate breaker, and requeues any work it can't finish this pass so
    the next scheduled invocation picks it up. Each campaign is wrapped in
    its own try/except (mirroring process_campaign()) so one bad campaign
    can never abort the batch or fail the whole scheduler invocation.
    """
    db = get_firestore()
    campaigns = _find_queued_campaigns(db)
    results = []

    print(f"[run-campaigns] queued campaigns: {len(campaigns)}")

    for campaign_doc in campaigns:
        campaign_id = campaign_doc.id
        try:
            send_campaign(campaign_id)
            results.append({"campaign_id": campaign_id, "status": "ok"})
            print(f"[run-campaigns] completed pass for {campaign_id}")
        except Exception as ex:
            try:
                db.collection("campaigns").document(campaign_id).update(
                    {"status": "failed", "last_error": str(ex)}
                )
            except Exception:
                pass
            results.append({"campaign_id": campaign_id, "status": "error", "error": str(ex)})
            print(f"[run-campaigns] {campaign_id} failed: {ex}")

    return _ok(data={"queued_found": len(campaigns), "results": results})


@app.route("/run-replies", methods=["GET", "POST"])
def run_replies():
    """
    One-pass replacement for smart_reply_worker.py's `while True` loop body.
    Polls every account in REPLY_ACCOUNTS for unread mail
    (mail_reader.read_unread_emails), then matches newly-stored
    inbox_messages to outreach sends (smart_reply_matcher.match_new_replies).
    Each mailbox poll is independently wrapped so one broken mailbox can't
    block the others or the matching step.
    """
    from smart_mail.config import REPLY_ACCOUNTS
    from smart_mail.mail_reader import read_unread_emails

    poll_results = []
    for alias in REPLY_ACCOUNTS:
        try:
            read_unread_emails(alias)
            poll_results.append({"account": alias, "status": "ok"})
        except Exception as ex:
            poll_results.append({"account": alias, "status": "error", "error": str(ex)})
            print(f"[run-replies] poll failed for {alias}: {ex}")

    try:
        limit = int(request.args.get("limit", "200"))
    except (TypeError, ValueError):
        limit = 200

    try:
        match_summary = match_new_replies(limit=limit)
    except Exception as ex:
        match_summary = {"error": str(ex)}
        print(f"[run-replies] match_new_replies failed: {ex}")

    return _ok(data={"poll_results": poll_results, "match_summary": match_summary})


@app.route("/healthz", methods=["GET"])
def healthz():
    return _ok(data={"service": "smartmail", "project": GCP_PROJECT})


@https_fn.on_request(
    region=GCP_LOCATION,
    timeout_sec=540,
    memory=options.MemoryOption.MB_512,
    max_instances=1,
    secrets=["SALES_SMTP_PASSWORD"],
)
def smartMail(req: https_fn.Request) -> https_fn.Response:
    with app.request_context(req.environ):
        return app.full_dispatch_request()
