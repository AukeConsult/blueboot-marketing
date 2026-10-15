# functions-crm/smart_mail/config.py
"""
Campaign-pipeline tuning config: send pause, per-account caps, bounce threshold,
unsubscribe headers. Mail accounts themselves live in Firestore
(settings/mail_accounts/accounts/{email}), not in .env.
"""
from __future__ import annotations

import os

def _int(name: str, default: str) -> int:
    try:
        return int((os.getenv(name) or default).strip())
    except (TypeError, ValueError):
        return int(default)


def _float(name: str, default: str) -> float:
    try:
        return float((os.getenv(name) or default).strip())
    except (TypeError, ValueError):
        return float(default)


def _bool(name: str, default: str) -> bool:
    return (os.getenv(name, default) or default).strip().lower() in ("1", "true", "yes", "on")


CAMPAIGN_SEND_DELAY_SECONDS  = _int("CAMPAIGN_SEND_DELAY_SECONDS", "12")
CAMPAIGN_WORKER_POLL_SECONDS = _int("CAMPAIGN_WORKER_POLL_SECONDS", "15")
MAX_SENDS_PER_HOUR           = _int("MAX_SENDS_PER_HOUR", "50")
MAX_SENDS_PER_DAY            = _int("MAX_SENDS_PER_DAY", "300")
BOUNCE_RATE_PAUSE_THRESHOLD  = _float("BOUNCE_RATE_PAUSE_THRESHOLD", "0.05")

UNSUBSCRIBE_BASE_URL = (os.getenv("UNSUBSCRIBE_BASE_URL") or "").strip()
UNSUBSCRIBE_MAILTO   = (os.getenv("UNSUBSCRIBE_MAILTO") or "").strip()

REPLY_POLL_SECONDS  = _int("REPLY_POLL_SECONDS", "120")
REPLY_LOOKBACK_DAYS = _int("REPLY_LOOKBACK_DAYS", "3")
REPLY_ACCOUNTS = [
    a.strip() for a in (os.getenv("REPLY_ACCOUNTS") or "sales").split(",") if a.strip()
]
REPLY_CLASSIFY_WITH_AI = _bool("REPLY_CLASSIFY_WITH_AI", "false")


__all__ = [
    "CAMPAIGN_SEND_DELAY_SECONDS", "CAMPAIGN_WORKER_POLL_SECONDS",
    "MAX_SENDS_PER_HOUR", "MAX_SENDS_PER_DAY", "BOUNCE_RATE_PAUSE_THRESHOLD",
    "UNSUBSCRIBE_BASE_URL", "UNSUBSCRIBE_MAILTO",
    "REPLY_POLL_SECONDS", "REPLY_LOOKBACK_DAYS", "REPLY_ACCOUNTS", "REPLY_CLASSIFY_WITH_AI",
]
