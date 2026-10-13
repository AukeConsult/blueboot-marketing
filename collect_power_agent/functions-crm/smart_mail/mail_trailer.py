"""functions-crm/smart_mail/mail_trailer.py - one default trailer added to every outreach mail.

Stored in Firestore as settings/mail_trailer:
    enabled  bool   off until an admin switches it on (Outreach Settings page)
    html     str    trailer for HTML mails
    text     str    trailer for plain-text mails (also appended to the plain-text part of HTML mails)

The trailer text may use the same {{placeholders}} as the mail body.
"""
from __future__ import annotations

import re

DOC_PATH = ("settings", "mail_trailer")

DEFAULT_HTML = (
    "<p><strong>Who Is BlueSearch Built For?</strong></p>"
    "<p>BlueSearch is aimed at businesses and website owners who recognize that visitors "
    "increasingly expect to ask questions and receive answers directly.</p>"
    "<p>As AI-driven experiences become more familiar, traditional search alone may no longer "
    "meet every visitor's expectations.</p>"
    "<p>BlueSearch is designed for organizations that want to make their existing website content "
    "easier for people to navigate and understand, without requiring visitors to search manually "
    "through large amounts of information.</p>"
)

DEFAULT_TEXT = (
    "Who Is BlueSearch Built For?\n\n"
    "BlueSearch is aimed at businesses and website owners who recognize that visitors "
    "increasingly expect to ask questions and receive answers directly.\n\n"
    "As AI-driven experiences become more familiar, traditional search alone may no longer "
    "meet every visitor's expectations.\n\n"
    "BlueSearch is designed for organizations that want to make their existing website content "
    "easier for people to navigate and understand, without requiring visitors to search manually "
    "through large amounts of information."
)

DEFAULTS = {"enabled": False, "html": DEFAULT_HTML, "text": DEFAULT_TEXT}

_DANGEROUS_BLOCK = re.compile(r"(?is)<(script|style|iframe|object|embed)\b.*?</\1\s*>")
_DANGEROUS_TAG   = re.compile(r"(?i)</?(script|style|iframe|object|embed)\b[^>]*>")
_EVENT_ATTR      = re.compile(r"""(?i)\s+on\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""")
_JS_URL          = re.compile(r"(?i)javascript\s*:")


def clean_html(html: str) -> str:
    """Strip scripts, embedded objects, event handlers and javascript: links."""
    html = _DANGEROUS_BLOCK.sub("", html or "")
    html = _DANGEROUS_TAG.sub("", html)
    html = _EVENT_ATTR.sub("", html)
    return _JS_URL.sub("", html).strip()


def load_trailer(db) -> dict:
    """Return {"enabled", "html", "text"}; defaults when nothing is saved yet."""
    snap = db.collection(DOC_PATH[0]).document(DOC_PATH[1]).get()
    d = (snap.to_dict() or {}) if snap.exists else {}
    return {
        "enabled": bool(d.get("enabled", DEFAULTS["enabled"])),
        "html":    d["html"] if "html" in d else DEFAULTS["html"],
        "text":    d["text"] if "text" in d else DEFAULTS["text"],
    }
