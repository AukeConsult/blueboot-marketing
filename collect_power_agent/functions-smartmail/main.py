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
                              (intro + followup passes via send_outreach())
  POST/GET /run-replies    -- one pass of smart_reply_worker.py's loop body
                              (poll mailboxes, match new replies)
  GET      /healthz        -- trivial liveness check

Mail account login settings are loaded from Firestore by the shared
MailSender path; the smart sender does not use SMTP password environment
secrets.
"""
import os

from flask import Flask, request, jsonify
from flask_cors import CORS
from firebase_functions import https_fn, options

from smart_mail.smart_campaign_sender import send_outreach
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


@app.route("/run-campaigns", methods=["GET", "POST"])
def run_campaigns():
    """
    One-pass send for both intro (new contacts) and followup modes.
    Delegates to send_outreach() which uses read_outreach() for candidate
    selection, render_mail() for rendering, and confirm_sent() for write-back.
    Rate limiting and the bounce-rate breaker are enforced inside send_outreach().
    """
    try:
        intro_summary = send_outreach("intro")
    except Exception as ex:
        intro_summary = {"error": str(ex)}
        print(f"[run-campaigns] intro send failed: {ex}")

    try:
        followup_summary = send_outreach("followup")
    except Exception as ex:
        followup_summary = {"error": str(ex)}
        print(f"[run-campaigns] followup send failed: {ex}")

    return _ok(data={"intro": intro_summary, "followup": followup_summary})


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
)
def smartMail(req: https_fn.Request) -> https_fn.Response:
    with app.request_context(req.environ):
        return app.full_dispatch_request()
