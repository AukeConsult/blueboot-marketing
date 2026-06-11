# app/smart_mail/config_mail.py
"""
Campaign-pipeline tuning config for the smart-mail-not-in-use system.

Account resolution (MAIL_ACCOUNTS / get_account / SSL-vs-STARTTLS) lives in
app/mail_accounts_config.py -- the single source of truth shared with
app/mail_sender.py and app/mail_reader.py. This module re-exports that plus
adds the campaign-specific tuning knobs (send delay, rate caps, reply-poll
settings, unsubscribe headers), all read straight from .env.

No separate secrets module: every credential lives in .env (gitignored),
exactly like the rest of the project's `cfg` (app/functions/config_mail.py).
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root -- same file functions/config_mail.py and
# mail_accounts_config.py load. Safe to call more than once (dotenv no-ops
# if the vars are already in os.environ).
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

# Re-export account resolution from the shared module (proper package import --
# the same pattern smart_campaign_sender.py already uses for `app.mail_sender`).
from app.mail_accounts_config import (   # noqa: E402  (must follow load_dotenv)
    MAIL_ACCOUNTS,
    DEFAULT_MAIL_ACCOUNT,
    get_account,
    smtp_uses_ssl,
)


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


# ---------------------------------------------------------------------------
# Smart-mail campaign sender / worker tuning
# ---------------------------------------------------------------------------
CAMPAIGN_SEND_DELAY_SECONDS  = _int("CAMPAIGN_SEND_DELAY_SECONDS", "12")
CAMPAIGN_WORKER_POLL_SECONDS = _int("CAMPAIGN_WORKER_POLL_SECONDS", "15")
MAX_SENDS_PER_HOUR           = _int("MAX_SENDS_PER_HOUR", "50")
MAX_SENDS_PER_DAY            = _int("MAX_SENDS_PER_DAY", "300")
BOUNCE_RATE_PAUSE_THRESHOLD  = _float("BOUNCE_RATE_PAUSE_THRESHOLD", "0.05")

UNSUBSCRIBE_BASE_URL = (os.getenv("UNSUBSCRIBE_BASE_URL") or "").strip()
UNSUBSCRIBE_MAILTO   = (os.getenv("UNSUBSCRIBE_MAILTO") or "").strip()

# ---------------------------------------------------------------------------
# Reply reader (IMAP polling) -- phase 2
# ---------------------------------------------------------------------------
REPLY_POLL_SECONDS  = _int("REPLY_POLL_SECONDS", "120")
REPLY_LOOKBACK_DAYS = _int("REPLY_LOOKBACK_DAYS", "3")
REPLY_ACCOUNTS = [
    a.strip() for a in (os.getenv("REPLY_ACCOUNTS") or "sales").split(",") if a.strip()
]
REPLY_CLASSIFY_WITH_AI = _bool("REPLY_CLASSIFY_WITH_AI", "false")


__all__ = [
    "MAIL_ACCOUNTS",
    "DEFAULT_MAIL_ACCOUNT",
    "get_account",
    "smtp_uses_ssl",
    "CAMPAIGN_SEND_DELAY_SECONDS",
    "CAMPAIGN_WORKER_POLL_SECONDS",
    "MAX_SENDS_PER_HOUR",
    "MAX_SENDS_PER_DAY",
    "BOUNCE_RATE_PAUSE_THRESHOLD",
    "UNSUBSCRIBE_BASE_URL",
    "UNSUBSCRIBE_MAILTO",
    "REPLY_POLL_SECONDS",
    "REPLY_LOOKBACK_DAYS",
    "REPLY_ACCOUNTS",
    "REPLY_CLASSIFY_WITH_AI",
]


if __name__ == "__main__":
    from app.mail_accounts_config import describe
    print(describe())
    print(f"DEFAULT_MAIL_ACCOUNT = {DEFAULT_MAIL_ACCOUNT}")
    print(f"REPLY_ACCOUNTS       = {REPLY_ACCOUNTS}")
    print(f"CAMPAIGN_SEND_DELAY_SECONDS = {CAMPAIGN_SEND_DELAY_SECONDS}")
