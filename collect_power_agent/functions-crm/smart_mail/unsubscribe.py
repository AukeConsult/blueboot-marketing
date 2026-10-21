"""functions-crm/smart_mail/unsubscribe.py -- one-click unsubscribe for outreach mail.

Stored in Firestore as settings/unsubscribe (created on first use):
    enabled     bool  default True   footer link + List-Unsubscribe headers on automatic outreach mail
    secret      str   random, generated once; signs the links so nobody can unsubscribe other people
    base_url    str   default https://us-central1-<project>.cloudfunctions.net/crmApi
    mailto      str   address for the mailto: fallback (default: the sending mailbox)
    footer_html str   footer for HTML mails  ({url} is replaced by the unsubscribe link)
    footer_text str   footer for plain-text mails

The link is  <base_url>/unsubscribe?t=<token>  with token = base64url("campaign|contact") + "." + HMAC.
A GET only shows a confirmation page (mail scanners open links); the POST (button, or the mail client's
one-click "List-Unsubscribe=One-Click") does the work: contact -> excluded, follow-up status not_interested.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import secrets
from datetime import datetime, timezone

DOC_PATH = ("settings", "unsubscribe")

DEFAULT_FOOTER_HTML = ('<p style="font-size:12px;color:#6b7280">Do not want mail from us? '
                       '<a href="{url}">Unsubscribe</a>.</p>')
DEFAULT_FOOTER_TEXT = "Do not want mail from us? Unsubscribe: {url}"

_cache: dict = {}
_CACHE_TTL = 60  # seconds; a settings change reaches every instance within a minute


def _default_base_url() -> str:
    project = (os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
               or os.getenv("GCLOUD_PROJECT") or "blueboot-market")
    return f"https://us-central1-{project}.cloudfunctions.net/crmApi"


def load_config(db) -> dict:
    """Return the settings; creates the doc (with a fresh secret) the first time."""
    hit = _cache.get("cfg")
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    ref = db.collection(DOC_PATH[0]).document(DOC_PATH[1])
    snap = ref.get()
    d = (snap.to_dict() or {}) if snap.exists else {}
    if not d.get("secret"):
        d["secret"] = secrets.token_hex(32)
        ref.set({"secret": d["secret"], "enabled": bool(d.get("enabled", True))}, merge=True)
    cfg = {
        "enabled":     bool(d.get("enabled", True)),
        "secret":      d["secret"],
        "base_url":    (d.get("base_url") or _default_base_url()).rstrip("/"),
        "mailto":      (d.get("mailto") or os.getenv("UNSUBSCRIBE_MAILTO") or "").strip(),
        "footer_html": d.get("footer_html") or DEFAULT_FOOTER_HTML,
        "footer_text": d.get("footer_text") or DEFAULT_FOOTER_TEXT,
    }
    _cache["cfg"] = (time.time(), cfg)
    return cfg


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sig(secret: str, payload: str) -> str:
    return _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()[:16])


def make_token(secret: str, campaign_id: str, doc_id: str) -> str:
    payload = _b64(f"{campaign_id}|{doc_id}".encode())
    return f"{payload}.{_sig(secret, payload)}"


def parse_token(secret: str, token: str) -> tuple[str, str] | None:
    try:
        payload, sig = (token or "").split(".", 1)
        if not hmac.compare_digest(sig, _sig(secret, payload)):
            return None
        campaign_id, doc_id = _unb64(payload).decode().split("|", 1)
        return (campaign_id, doc_id) if campaign_id and doc_id else None
    except Exception:
        return None


def unsubscribe_url(cfg: dict, campaign_id: str, doc_id: str) -> str:
    return f"{cfg['base_url']}/unsubscribe?t={make_token(cfg['secret'], campaign_id, doc_id)}"


def list_headers(cfg: dict, url: str, sender_email: str = "") -> tuple[str, str]:
    """(List-Unsubscribe, List-Unsubscribe-Post) header values."""
    mailto = cfg.get("mailto") or sender_email
    parts = []
    if mailto:
        parts.append(f"<mailto:{mailto}?subject=unsubscribe>")
    parts.append(f"<{url}>")
    return ", ".join(parts), "List-Unsubscribe=One-Click"


def footers(cfg: dict, url: str) -> tuple[str, str]:
    """(html, text) footer with the link filled in."""
    return cfg["footer_html"].replace("{url}", url), cfg["footer_text"].replace("{url}", url)


def apply_unsubscribe(db, campaign_id: str, doc_id: str, how: str = "link") -> str:
    """Exclude the contact. Returns "ok", "already" or "missing". Safe to repeat."""
    from google.cloud.firestore_v1 import ArrayUnion
    ref = (db.collection("campaigns").document(campaign_id)
             .collection("campaign_contacts").document(doc_id))
    snap = ref.get()
    if not snap.exists:
        return "missing"
    d = snap.to_dict() or {}
    if d.get("unsubscribed"):
        return "already"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    update = {
        "status": "excluded",
        "followup_status": "not_interested",
        "unsubscribed": True,
        "unsubscribed_at": now,
        "last_updated_at": now,
        "comment_history": ArrayUnion([{
            "date": now, "user": d.get("email") or "contact", "type": "UNSUBSCRIBE",
            "text": f"Unsubscribed ({how}) - excluded from all mail",
        }]),
    }
    ref.update(update)
    return "ok"


import re as _re
_STOP_RE = _re.compile(r"(?i)\b(unsubscribe|opt[ -]?out|remove me|take me off|stop (sending|emailing|mailing)|"
                       r"do not (contact|email)|don't (contact|email)|avmeld\w*|meld meg av|ikke send)\b")


def is_unsubscribe_request(subject: str, body_text: str) -> bool:
    """A reply that asks to stop: checked on the subject and the first lines of the reply only."""
    head = "\n".join((body_text or "").strip().splitlines()[:6])[:400]
    return bool(_STOP_RE.search(subject or "") or _STOP_RE.search(head))
