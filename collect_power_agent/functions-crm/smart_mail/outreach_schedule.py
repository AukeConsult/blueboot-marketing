# functions-crm/smart_mail/outreach_schedule.py
"""On/off switches for the two scheduled jobs (outreachHourly and replyHourly).

Stored in Firestore settings/outreach_schedule:
    send_enabled   - the 30-minute send job queues outreach-send when True
    reply_enabled  - the hourly reply read runs when True

A missing document, a missing field or an unreadable document means OFF, so deploying
the scheduled functions never starts sending by itself. The Outreach Settings page
writes this document; the scheduled functions read it at the start of every run.
"""
from __future__ import annotations

DOC_PATH = ("settings", "outreach_schedule")
DEFAULTS = {"send_enabled": False, "reply_enabled": False}


def load_schedule_flags(db) -> dict:
    """Return {"send_enabled": bool, "reply_enabled": bool}. Never raises; any problem means OFF."""
    try:
        doc = db.collection(DOC_PATH[0]).document(DOC_PATH[1]).get()
        d = (doc.to_dict() or {}) if doc.exists else {}
    except Exception as ex:
        print(f"[schedule] could not read {DOC_PATH[0]}/{DOC_PATH[1]} ({ex}); jobs treated as OFF")
        d = {}
    return {k: bool(d.get(k, v)) for k, v in DEFAULTS.items()}
