"""functions-crm/smart_mail/outreach_render_mail.py - mail rendering for outreach.

Single public function:

    render_mail(step, contact) -> RenderedMail

Renders a MailStep template against a ContactRow, producing a ready-to-send
subject, plain-text body, and optional HTML body.

Rules enforced here so the send loop never has to think about them:
  - Template vars ({{name}}, {{company}}, ...) are filled via render_template()
    from template_engine.py
  - When a step has HTML: plain text is auto-derived via html_to_text() from
    outreach_sender.py; html_body is set
  - When a step has plain text only: html_body is None
  - RFC 2045 multipart/alternative order: plain FIRST, HTML SECOND
    (mail clients render the last part they understand, so HTML goes last
    to be preferred when supported)
  - text_body always falls back to subject if both body fields are empty

No SMTP / sending code lives here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from typing import Any


# ---------------------------------------------------------------------------
# Lazy imports of existing rendering helpers
# (resolved at call time so import order never matters)
# ---------------------------------------------------------------------------

def _render_template(template: str, contact: dict[str, Any]) -> str:
    """Delegate to template_engine.render_template."""
    try:
        from smart_mail.template_engine import render_template
    except ImportError:
        from template_engine import render_template  # type: ignore[no-redef]
    return render_template(template, contact)


# ---------------------------------------------------------------------------
# html_to_text - inlined from outreach_sender so this module has no
# circular dependency on the sender, but the logic is byte-identical.
# ---------------------------------------------------------------------------

_TAG_RE         = re.compile(r"<[^>]+>")
_BLOCK_CLOSE_RE = re.compile(r"(?i)</(p|div|tr|li|h[1-6])>")
_BR_RE          = re.compile(r"(?i)<br\s*/?>")
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_BLANK_RUN_RE   = re.compile(r"\n{3,}")
_SPACE_RUN_RE   = re.compile(r"[ \t]+")


def _html_to_text(html: str) -> str:
    """Best-effort HTML -> plain-text (mirrors outreach_sender.html_to_text)."""
    if not html:
        return ""
    text = _SCRIPT_STYLE_RE.sub("", html)
    text = _BR_RE.sub("\n", text)
    text = _BLOCK_CLOSE_RE.sub("\n\n", text)
    text = _TAG_RE.sub("", text)
    text = unescape(text)
    text = _SPACE_RUN_RE.sub(" ", text)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MailStep:
    """One step in a campaign mail_sequence.

    Stored as an element of campaign.mail_sequence in Firestore:
        { index, mail_type, subject, body_html, body_text }

    Exactly one of body_html / body_text should be non-empty.
    If both are set, body_html takes precedence and body_text is ignored
    (plain text will be derived automatically from the HTML).
    """
    index:      int
    mail_type:  str   # "intro" | "followup_1" | "followup_2" | ...
    subject:    str   # may contain {{vars}}
    body_html:  str   # Quill HTML; set for HTML mails, "" for plain-only
    body_text:  str   # plain text template; set for plain-only mails, "" otherwise


@dataclass
class RenderedMail:
    """A fully rendered, send-ready mail produced by render_mail().

    Always has subject + text_body.
    html_body is None for plain-text-only steps.

    RFC 2045 order note: when building a MIMEMultipart("alternative") message
    the caller must attach text_body FIRST and html_body SECOND so that mail
    clients that understand HTML will prefer it.
    """
    subject:   str
    text_body: str
    html_body: str | None   # None => plain-text step; send as text/plain only


# ---------------------------------------------------------------------------
# render_mail
# ---------------------------------------------------------------------------

def render_mail(step: MailStep, contact: Any) -> RenderedMail:
    """Render a MailStep template against a ContactRow.

    Parameters
    ----------
    step : MailStep
        The campaign mail step to render (subject + body template).
    contact : ContactRow
        The recipient; converted to a dict for render_template().

    Returns
    -------
    RenderedMail
        Ready-to-send subject, text_body, and optional html_body.
        text_body is ALWAYS present (falls back to subject if templates are empty).
        html_body is None for plain-text-only steps.

    Rendering rules
    ---------------
    1. subject is always rendered through render_template().
    2. If step.body_html is non-empty:
         - html_body = render_template(body_html, contact)
         - text_body = _html_to_text(html_body)  or subject as last resort
    3. If step.body_html is empty and step.body_text is non-empty:
         - text_body = render_template(body_text, contact)
         - html_body = None
    4. If both body fields are empty:
         - text_body = subject  (fallback — campaign is misconfigured)
         - html_body = None
    """
    contact_dict = _contact_to_dict(contact)

    subject = _render_template(step.subject, contact_dict)

    if step.body_html:
        html_body  = _render_template(step.body_html, contact_dict)
        text_body  = _html_to_text(html_body) or subject
        return RenderedMail(subject=subject, text_body=text_body, html_body=html_body)

    if step.body_text:
        text_body = _render_template(step.body_text, contact_dict)
        return RenderedMail(subject=subject, text_body=text_body, html_body=None)

    # Both body fields empty — misconfigured step; degrade gracefully
    return RenderedMail(subject=subject, text_body=subject, html_body=None)


# ---------------------------------------------------------------------------
# ContactRow -> dict helper
# (avoids a hard import of outreach_mail_select so modules stay independent)
# ---------------------------------------------------------------------------

def _contact_to_dict(contact: Any) -> dict[str, Any]:
    """Convert a ContactRow (or any object / dict) to the dict render_template expects.

    render_template looks up: name, full_name, company, website, country,
    title, email, ai_sector, domain, phone, location, ai_company_type.

    ContactRow exposes contact_name (mapped to "name"), company, domain
    (mapped to "website"), country, email.  Anything in contact.extra is
    also passed through so custom template fields work automatically.
    """
    if isinstance(contact, dict):
        return contact

    base: dict[str, Any] = {}

    # Direct field mappings
    for attr in ("email", "company", "domain", "country"):
        base[attr] = getattr(contact, attr, "") or ""

    # contact_name -> name  (render_template reads {{name}})
    base["name"]    = getattr(contact, "contact_name", "") or ""
    base["website"] = base.get("domain", "")   # {{website}} alias for domain

    # Pass through .extra so any custom fields in the campaign template work
    extra = getattr(contact, "extra", {}) or {}
    base.update(extra)

    return base


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "MailStep",
    "RenderedMail",
    "render_mail",
]
