# functions-smartmail/smart_mail/smart_template_engine.py  (verbatim copy of app/smart-mail-not-in-use/smart_template_engine.py)

from typing import Any


SUPPORTED_FIELDS = [
    "name",
    "full_name",
    "company",
    "website",
    "country",
    "title",
    "email",
    "ai_sector",

    "domain",
    "phone",
    "location",
    "ai_company_type",
]


def _value(contact: dict[str, Any], field: str) -> str:
    if field == "full_name":
        return (
            contact.get("personalisation", {})
            .get("full_name", "")
        )

    if field == "name":
        name = (
                contact.get("personalisation", {})
                .get("name", "")
                or contact.get("name", "")
        )

        if name:
            return name

        email = contact.get("email", "")

        if "@" in email:
            return email.split("@")[0]

        return ""

    value = contact.get(field, "")

    if value is None:
        return ""

    return str(value)


def render_template(template: str, contact: dict[str, Any]) -> str:
    """
    Replace merge tags:

    {{name}}
    {{full_name}}
    {{company}}
    {{website}}
    {{country}}
    {{title}}
    {{email}}
    {{ai_sector}}
    """

    rendered = template

    for field in SUPPORTED_FIELDS:
        rendered = rendered.replace(
            "{{" + field + "}}",
            _value(contact, field)
        )

    return rendered
