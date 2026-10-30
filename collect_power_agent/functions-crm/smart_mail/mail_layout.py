"""functions-crm/smart_mail/mail_layout.py - the frame around every outreach mail.

Stored in Firestore as settings/mail_layout:
    company_name     str   printed in the footer as {{company_name}}; the sender's name when empty
    company_address  str   printed in the footer as {{company_address}}; its line is left out when empty
    layout           {text: {frame, footer, unsubscribe}, html: {frame, footer, unsubscribe}}

One version for plain text and one for HTML. The FRAME is the whole template and says where the parts go:
    {{message}}      the mail written in the campaign 
    {{footer}}       the footer block
    {{unsubscribe}}  the unsubscribe block (only when the mail has an unsubscribe link)
Inside the frame, the footer and the unsubscribe block:
    {{sender_name}} {{sender_email}} {{company_name}} {{company_address}} {{unsubscribe_url}}
and the contact placeholders of the campaign mails ({{name}}, {{company}}, ...). The CRM's {{name}}/{{company}}/{{email}}
mean the CONTACT, so the sender and the company have their own names here.

The same rules as the Sup-Chat mail layout (blueboot sup-chat, shared-library email-compose.ts), written again here
in Python with the CRM's own placeholder names. Keep the two in step when one of them changes.
"""
from __future__ import annotations

import re
from html import escape

DOC_PATH = ("settings", "mail_layout")

MAX_LEN = {
    ("text", "frame"): 2000, ("text", "footer"): 1000, ("text", "unsubscribe"): 1000,
    ("html", "frame"): 10000, ("html", "footer"): 5000, ("html", "unsubscribe"): 3000,
}
MAX_COMPANY_NAME = 100
MAX_COMPANY_ADDRESS = 300

DEFAULT_FRAME_TEXT = "{{message}}\n\n{{footer}}\n\n{{unsubscribe}}"
DEFAULT_FOOTER_TEXT = "--\n{{company_name}}\n{{company_address}}\n{{sender_email}}"
DEFAULT_UNSUBSCRIBE_TEXT = "Do not want mail from us? Unsubscribe:\n{{unsubscribe_url}}"

DEFAULT_FRAME_HTML = (
    '<div style="padding:24px 0 24px 24px;">'
    '<div style="max-width:600px;margin:0;background:#eaf3fb;border:1px solid #b9d5ee;padding:20px;'
    'font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#1f2d3d;">'
    "{{message}}{{footer}}{{unsubscribe}}"
    "</div></div>"
)
DEFAULT_FOOTER_HTML = (
    '<div style="margin-top:24px;padding-top:12px;border-top:1px solid #dce3ec;font-family:Arial,Helvetica,sans-serif;'
    'font-size:13px;line-height:1.5;color:#5b6b7f;">'
    '<div><strong style="color:#1f2d3d;">{{company_name}}</strong></div>'
    "<div>{{company_address}}</div>"
    '<div><a href="mailto:{{sender_email}}" style="color:#2b6cb0;text-decoration:none;">{{sender_email}}</a></div>'
    "</div>"
)
DEFAULT_UNSUBSCRIBE_HTML = (
    '<div style="margin-top:12px;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.5;color:#8896a6;">'
    'Do not want mail from us? <a href="{{unsubscribe_url}}" style="color:#8896a6;">Unsubscribe</a>.'
    "</div>"
)

DEFAULT_LAYOUT = {
    "text": {"frame": DEFAULT_FRAME_TEXT, "footer": DEFAULT_FOOTER_TEXT, "unsubscribe": DEFAULT_UNSUBSCRIBE_TEXT},
    "html": {"frame": DEFAULT_FRAME_HTML, "footer": DEFAULT_FOOTER_HTML, "unsubscribe": DEFAULT_UNSUBSCRIBE_HTML},
}

_PART_RE = re.compile(r"\{\{\s*(message|footer|unsubscribe)\s*\}\}")
_VAR_RE = re.compile(r"\{\{\s*(sender_name|sender_email|company_name|company_address|unsubscribe_url)\s*\}\}")


def _copy_default() -> dict:
    return {k: dict(v) for k, v in DEFAULT_LAYOUT.items()}


def normalize(raw: dict | None, legacy_unsubscribe: dict | None = None) -> dict:
    """The saved settings with the defaults where a part is missing.

    legacy_unsubscribe: {"html", "text"} from the older settings/unsubscribe footers ({url} placeholder). It is the
    unsubscribe block of an account that never saved a layout, so the text an admin wrote there is not lost.
    """
    raw = raw or {}
    saved = raw.get("layout") or {}
    layout = _copy_default()
    for kind in ("text", "html"):
        mine = saved.get(kind) or {}
        for part in ("frame", "footer", "unsubscribe"):
            value = mine.get(part)
            if isinstance(value, str):
                layout[kind][part] = value
            elif part == "unsubscribe" and legacy_unsubscribe and legacy_unsubscribe.get(kind):
                layout[kind][part] = str(legacy_unsubscribe[kind]).replace("{url}", "{{unsubscribe_url}}")
    return {
        "company_name": str(raw.get("company_name") or ""),
        "company_address": str(raw.get("company_address") or ""),
        "layout": layout,
    }


def load_layout(db) -> dict:
    """Return the layout settings; defaults when nothing is saved yet."""
    snap = db.collection(DOC_PATH[0]).document(DOC_PATH[1]).get()
    raw = (snap.to_dict() or {}) if snap.exists else {}
    legacy = None
    try:
        u = db.collection("settings").document("unsubscribe").get()
        d = (u.to_dict() or {}) if u.exists else {}
        legacy = {"html": d.get("footer_html"), "text": d.get("footer_text")}
    except Exception:
        legacy = None
    return normalize(raw, legacy)


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


def validate(body: dict) -> dict:
    """Check and clean what the page sends. Returns the document to save; raises ValueError with a message for the admin."""
    body = body or {}
    company_name = str(body.get("company_name") or "").strip()
    company_address = str(body.get("company_address") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(company_name) > MAX_COMPANY_NAME:
        raise ValueError(f"The company name is too long (max {MAX_COMPANY_NAME} characters).")
    if len(company_address) > MAX_COMPANY_ADDRESS:
        raise ValueError(f"The company address is too long (max {MAX_COMPANY_ADDRESS} characters).")
    saved = body.get("layout") or {}
    layout = _copy_default()
    label = {"text": "Plain text", "html": "HTML"}
    for kind in ("text", "html"):
        mine = saved.get(kind) or {}
        for part in ("frame", "footer", "unsubscribe"):
            value = mine.get(part)
            if value is None:
                continue
            value = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
            if kind == "html":
                value = clean_html(value)
            if len(value) > MAX_LEN[(kind, part)]:
                raise ValueError(f"The {label[kind]} {part} is too long (max {MAX_LEN[(kind, part)]} characters).")
            layout[kind][part] = value
        frame = layout[kind]["frame"]
        if frame and not re.search(r"\{\{\s*message\s*\}\}", frame):
            raise ValueError(f"The {label[kind]} frame must contain {{{{message}}}}, where the mail goes.")
    return {"company_name": company_name, "company_address": company_address, "layout": layout}


def _contact_render(template: str, contact: dict | None) -> str:
    if not contact:
        return template
    try:
        from smart_mail.template_engine import render_template
    except ImportError:
        from template_engine import render_template  # type: ignore[no-redef]
    return render_template(template, contact)


def _fill_vars(template: str, values: dict, contact: dict | None) -> str:
    return _VAR_RE.sub(lambda m: values[m.group(1)], _contact_render(template, contact))


def _fill_block(template: str, values: dict, contact: dict | None, html: bool) -> str:
    """A footer or unsubscribe block. Plain text: a line that was only placeholders and came out empty is dropped."""
    if html:
        return _fill_vars(template, values, contact).strip()
    kept = []
    for line in template.split("\n"):
        out = _fill_vars(line, values, contact)
        if out.strip() or not line.strip():
            kept.append(out)
    return "\n".join(kept).strip()


def compose(settings: dict, kind: str, message: str, *, sender_name: str = "", sender_email: str = "",
            unsubscribe_url: str = "", contact: dict | None = None) -> str:
    """One finished version ("text" or "html") of a mail.

    The message is put in last, so nothing in it is taken for a placeholder. The unsubscribe block is filled only when
    there is a link; a frame that has no {{unsubscribe}} still gets the block at the end, so the link is never lost.
    """
    html = kind == "html"
    part = settings["layout"][kind]
    company = (settings.get("company_name") or "").strip() or sender_name
    address = (settings.get("company_address") or "").strip()
    esc = (lambda s: escape(s, quote=True)) if html else (lambda s: s)
    values = {
        "sender_name": esc(sender_name),
        "sender_email": esc(sender_email),
        "company_name": esc(company),
        "company_address": esc(address).replace("\n", "<br>") if html else address,
        "unsubscribe_url": esc(unsubscribe_url),
    }
    footer = _fill_block(part["footer"], values, contact, html) if part["footer"].strip() else ""
    unsubscribe = _fill_block(part["unsubscribe"], values, contact, html) if (unsubscribe_url and part["unsubscribe"].strip()) else ""

    frame = part["frame"].strip() or ("{{message}}{{footer}}{{unsubscribe}}" if html else DEFAULT_FRAME_TEXT)
    frame = _fill_vars(frame, values, contact)
    if unsubscribe and not re.search(r"\{\{\s*unsubscribe\s*\}\}", frame):
        frame += ("" if html else "\n\n") + "{{unsubscribe}}"
    parts = {"message": message, "footer": footer, "unsubscribe": unsubscribe}
    if html:
        return _PART_RE.sub(lambda m: parts[m.group(1)], frame)
    lines = [ln for ln in frame.split("\n") if not (_PART_RE.search(ln) and not _PART_RE.sub(lambda m: parts[m.group(1)], ln).strip())]
    return re.sub(r"\n{3,}", "\n\n", _PART_RE.sub(lambda m: parts[m.group(1)], "\n".join(lines))).strip()


def compose_mail(settings: dict, text_message: str, html_message: str | None, **who) -> tuple[str, str | None]:
    """(text_body, html_body) of a mail. An HTML mail also gets the plain text layout around its text version."""
    text = compose(settings, "text", text_message, **who)
    html = compose(settings, "html", html_message, **who) if html_message is not None else None
    return text, html


SAMPLE_CONTACT = {"name": "Anna Berg", "company": "Example AS", "email": "anna@example.com",
                  "website": "example.com", "country": "NO"}
SAMPLE_MESSAGE_HTML = ("<p>Hello {{name}},</p><p>This is a sample message to show how your mail looks with the layout. "
                       "The text you write in a campaign goes here.</p><p>Best regards</p>")
SAMPLE_MESSAGE_TEXT = ("Hello {{name}},\n\nThis is a sample message to show how your mail looks with the layout. "
                       "The text you write in a campaign goes here.\n\nBest regards")
SAMPLE_UNSUBSCRIBE_URL = "https://example.com/unsubscribe?t=sample"
