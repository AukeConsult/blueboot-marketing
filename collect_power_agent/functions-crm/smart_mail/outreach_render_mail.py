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
  - When a step has plain text only: html_body is made from the text (paragraphs, line breaks, links),
    so every mail goes out as HTML and plain text
  - RFC 2045 multipart/alternative order: plain FIRST, HTML SECOND
    (mail clients render the last part they understand, so HTML goes last
    to be preferred when supported)
  - text_body always falls back to subject if both body fields are empty

No SMTP / sending code lives here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape as html_escape, unescape
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


_URL_RE = re.compile(r"https?://[^\s<>\"]+")


def _text_to_html(text: str) -> str:
    """A plain-text mail as HTML: paragraphs on blank lines, <br> on line breaks, web links clickable."""
    def link(m: "re.Match[str]") -> str:
        url = m.group(0)
        tail = ""
        while url and url[-1] in ".,;:!?)":
            tail = url[-1] + tail
            url = url[:-1]
        return '<a href="%s">%s</a>%s' % (url, url, tail)

    paras = [p for p in re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n").strip()) if p.strip()]
    out = []
    for p in paras:
        safe = _URL_RE.sub(link, html_escape(p.strip(), quote=False))
        out.append("<p>%s</p>" % safe.replace("\n", "<br>"))
    return "".join(out)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MailStep:
    """One step in a campaign mail_sequence.

    Stored as an element of campaign.mail_sequence in Firestore:
        { index, mail_type, subject, body_html, body_text }

    body_html set    -> an HTML mail. body_text, when also set, is its separate plain-text version
                        (its own text, translated on its own); when empty the plain text is derived
                        from the HTML.
    body_html empty  -> a plain-text mail made from body_text.
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

def render_mail(step: MailStep, contact: Any,
                footer: tuple[str, str] | None = None, layout: dict | None = None,
                sender: dict | None = None, unsubscribe_url: str = "") -> RenderedMail:
    """Render a MailStep template against a ContactRow.

    Parameters
    ----------
    step : MailStep
        The campaign mail step to render (subject + body template).
    contact : ContactRow
        The recipient; converted to a dict for render_template().
    footer : (html, text), optional
        Unsubscribe footer (smart_mail.unsubscribe.footers), added last. Not used while the mail layout is on.
    layout : dict, optional
        Result of mail_layout.load_layout(). When given, the finished mail is put in the layout's
        frame with its footer and unsubscribe block, for both the HTML and the plain text version, and `footer` is
        ignored. `sender` is {"name", "email"} of the sending account; `unsubscribe_url` is the link of this contact
        (empty: the unsubscribe block is left out).

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
         - text_body = render_template(body_text, contact) when step.body_text is set (the separate
           plain-text version), else _html_to_text(html_body), else subject as last resort
    3. If step.body_html is empty and step.body_text is non-empty:
         - text_body = render_template(body_text, contact)
         - html_body = None
    4. If both body fields are empty:
         - text_body = subject  (fallback — campaign is misconfigured)
         - html_body = None
    """
    contact_dict = _contact_to_dict(contact)

    subject = _render_template(step.subject, contact_dict)

    layout_on = layout is not None

    if step.body_html:
        html_body  = _render_template(step.body_html, contact_dict)
        own_text   = _render_template(step.body_text, contact_dict).strip() if (step.body_text or "").strip() else ""
        text_body  = own_text or _html_to_text(html_body) or subject
        if layout_on:
            text_body, html_body = _apply_layout(layout, text_body, html_body, sender, unsubscribe_url, contact_dict)
        elif footer:
            html_body += footer[0]
            text_body += "\n\n" + footer[1]
        return RenderedMail(subject=subject, text_body=text_body, html_body=html_body)

    if step.body_text:
        text_body = _render_template(step.body_text, contact_dict)
        html_body = _text_to_html(text_body)     # every mail also goes out as HTML
        if layout_on:
            text_body, html_body = _apply_layout(layout, text_body, html_body, sender, unsubscribe_url, contact_dict)
        elif footer:
            html_body += footer[0]
            text_body += "\n\n" + footer[1]
        return RenderedMail(subject=subject, text_body=text_body, html_body=html_body)

    # Both body fields empty — misconfigured step; degrade gracefully
    return RenderedMail(subject=subject, text_body=subject, html_body=None)


def _apply_layout(layout: dict, text_body: str, html_body: str | None, sender: dict | None,
                  unsubscribe_url: str, contact_dict: dict) -> tuple[str, str | None]:
    """Put the finished mail in the mail layout (smart_mail.mail_layout)."""
    from smart_mail.mail_layout import compose_mail
    who = sender or {}
    return compose_mail(layout, text_body, html_body, sender_name=who.get("name", ""),
                        sender_email=who.get("email", ""), unsubscribe_url=unsubscribe_url, contact=contact_dict)


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
