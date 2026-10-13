"""functions-crm/crm/mail_translate_lib.py - pure AI translation of a campaign mail.

translate_mail(subject, body, language) -> {"subject": ..., "body": ...}

The AI is told to translate and nothing else. Afterwards the result is checked: the
{{placeholders}}, links, e-mail addresses and HTML tags must be exactly the same as before,
otherwise the translation is refused (ValueError) so a broken mail is never stored.
Embedded images (data: URIs) never go to the AI; they are put back afterwards.
"""
from __future__ import annotations

import json
import os
import re

MAX_CHARS = 20000
DEFAULT_MODEL = "gpt-5.4-mini"      # override with OPENAI_MODEL

# Language codes the mail editor offers (the same codes are used in blueboot.ai/lang/<code>/ links).
LANGUAGE_NAMES = {
    "en": "English (UK)", "da": "Danish", "no": "Norwegian", "sv": "Swedish", "fi": "Finnish",
    "de": "German", "nl": "Dutch", "fr": "French", "es": "Spanish", "it": "Italian",
    "pt": "Portuguese", "pl": "Polish",
}


def language_name(code: str) -> str:
    """'da' -> 'Danish'. An unknown code is passed to the AI as it is."""
    code = (code or "").strip()
    name = LANGUAGE_NAMES.get(code.lower())
    return f"{name} (language code {code.lower()})" if name else f"the language with code or name '{code}'"


_PLACEHOLDER = re.compile(r"\{\{\s*\w+\s*\}\}")
_URL         = re.compile(r"""https?://[^\s"'<>]+""")
_EMAIL       = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TAG         = re.compile(r"<\s*/?\s*([a-zA-Z][\w-]*)")
_DATA_URI    = re.compile(r"data:[^\"')\s]+")

_SYSTEM = (
    "You are a professional translator. Translate the mail into {language}. "
    "This is a pure translation: do not add, remove, shorten, summarise, improve or reword anything, "
    "keep the tone and the meaning, and keep the same paragraphs, line breaks and order. "
    "Keep EXACTLY as written, without translating: every {{{{placeholder}}}} in double curly braces, "
    "all HTML tags and attributes (translate only the text between tags), URLs, e-mail addresses, "
    "numbers, and product and company names such as BlueSearch and Blueboot. "
    'Answer only with JSON: {{"subject": "...", "body": "..."}}.'
)


def current_model() -> str:
    """The model used for mail translation: OPENAI_MODEL if set, else the default."""
    return os.getenv("OPENAI_MODEL", "").strip() or DEFAULT_MODEL


def _signature(text: str) -> dict:
    """What must survive translation unchanged."""
    return {
        "placeholders": sorted(re.sub(r"\s+", "", m) for m in _PLACEHOLDER.findall(text)),
        "urls":         sorted(_URL.findall(text)),
        "emails":       sorted(_EMAIL.findall(text)),
        "tags":         [t.lower() for t in _TAG.findall(text)],
    }


def _call_ai(subject: str, body: str, language: str, client=None, model: str | None = None) -> dict:
    if client is None:
        key = os.getenv("OPENAI_API_KEY", "")
        if not key:
            raise ValueError("OPENAI_API_KEY is not set, so the mail cannot be translated.")
        from openai import OpenAI
        client = OpenAI(api_key=key)
    resp = client.chat.completions.create(
        model=model or current_model(),
        messages=[
            {"role": "system", "content": _SYSTEM.format(language=language_name(language))},
            {"role": "user",   "content": json.dumps({"subject": subject, "body": body}, ensure_ascii=False)},
        ],
        max_completion_tokens=4000,      # GPT-5 models take this name and no custom temperature
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content or "{}")


def translate_mail(subject: str, body: str, language: str, *, client=None, model: str | None = None) -> dict:
    """Translate subject and body (plain text or HTML) into `language`. Raises ValueError on any problem."""
    language = (language or "").strip()
    if not language:
        raise ValueError("Choose a language to translate into.")
    if not (subject or "").strip() and not (body or "").strip():
        raise ValueError("The mail is empty, there is nothing to translate.")
    if len(subject or "") + len(body or "") > MAX_CHARS:
        raise ValueError(f"The mail is too long to translate in one go (limit {MAX_CHARS} characters).")

    # Embedded images are huge and must not be translated: swap them for short markers.
    images: list[str] = []
    def _hide(m):
        images.append(m.group(0))
        return f"IMG_DATA_{len(images) - 1}_END"
    body_in = _DATA_URI.sub(_hide, body or "")

    out = _call_ai(subject or "", body_in, language, client=client, model=model)
    new_subject, new_body = str(out.get("subject", "")), str(out.get("body", ""))

    for i, original in enumerate(images):
        marker = f"IMG_DATA_{i}_END"
        if marker not in new_body:
            raise ValueError("The translation lost an image, nothing was changed. Try again.")
        new_body = new_body.replace(marker, original)

    for label, before, after in (("subject", subject or "", new_subject), ("text", body or "", new_body)):
        if _signature(before) != _signature(after):
            raise ValueError(f"The translated {label} changed a placeholder, link or HTML tag, "
                             "so nothing was changed. Try again.")
    return {"subject": new_subject, "body": new_body}
