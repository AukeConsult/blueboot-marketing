"""handlers/unsubscribe.py -- PUBLIC unsubscribe page (no sign-in; main.check_auth lets /unsubscribe through)."""
from __future__ import annotations

import html

from flask import Blueprint, request, Response

from handlers.shared import _get_db

bp = Blueprint("unsubscribe", __name__)

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unsubscribe</title><style>body{{font-family:system-ui,sans-serif;background:#f3f4f6;margin:0;display:flex;
min-height:100vh;align-items:center;justify-content:center}}.c{{background:#fff;border-radius:12px;padding:32px;max-width:420px;
box-shadow:0 2px 12px rgba(0,0,0,.08);text-align:center}}button{{background:#2563eb;color:#fff;border:0;border-radius:8px;
padding:10px 22px;font-size:1rem;cursor:pointer}}p{{color:#374151}}</style></head><body><div class="c">{body}</div></body></html>"""


def _page(body: str, status: int = 200) -> Response:
    return Response(_PAGE.format(body=body), status=status, mimetype="text/html")


@bp.route("/unsubscribe", methods=["GET", "POST"])
def unsubscribe():
    from smart_mail.unsubscribe import load_config, parse_token, apply_unsubscribe
    token = request.args.get("t", "")
    try:
        db = _get_db()
        parsed = parse_token(load_config(db)["secret"], token)
    except Exception as exc:
        print(f"[unsubscribe] config error: {exc}")
        return _page("<h2>Something went wrong</h2><p>Please try again later.</p>", 500)
    if not parsed:
        return _page("<h2>Invalid link</h2><p>This unsubscribe link is not valid.</p>", 400)
    campaign_id, doc_id = parsed

    if request.method == "GET":
        # A mail scanner or preview may open the link: only a POST changes anything.
        return _page('<h2>Unsubscribe</h2><p>Click the button to stop receiving mail from us.</p>'
                     f'<form method="post" action="unsubscribe?t={html.escape(token)}">'
                     '<button type="submit">Unsubscribe</button></form>')
    try:
        res = apply_unsubscribe(db, campaign_id, doc_id,
                                "one-click" if request.form.get("List-Unsubscribe") else "link")
    except Exception as exc:
        print(f"[unsubscribe] failed for {campaign_id}/{doc_id}: {exc}")
        return _page("<h2>Something went wrong</h2><p>Please try again later.</p>", 500)
    print(f"[unsubscribe] {campaign_id}/{doc_id}: {res}")
    return _page("<h2>You are unsubscribed</h2><p>You will not receive more mail from us. "
                 "We are sorry to see you go.</p>")
