# app/mail_accounts_config.py
"""
Resolves named outreach-mailbox accounts (SMTP + IMAP) from .env.

This is the single source of truth for "which mailbox sends/reads mail for
alias X" -- used by app/mail_sender.py, app/mail_reader.py, and the
app/smart-mail-not-in-use/* campaign pipeline (via app/smart-mail-not-in-use/config_mail.py).

Per-account credentials live as ALIAS_*-prefixed vars in .env (gitignored
via the project's `*secrets.py`/`.env` patterns -- no values are hardcoded
here, so this file is safe to commit). Add a new mailbox by adding its alias
to _KNOWN_ALIASES and the matching ALIAS_SMTP_*/ALIAS_IMAP_* vars to .env.

Exposes:
    MAIL_ACCOUNTS          {alias: {alias, host, port, user, password,
                                     from_name, from_addr, imap_host, imap_port}}
    DEFAULT_MAIL_ACCOUNT   alias used when nothing more specific is requested
    get_account(alias)     resolve an account dict; never raises at import time
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load .env from the project root -- same file app/functions/config_mail.py loads.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _int(name: str, default: str) -> int:
    try:
        return int((os.getenv(name) or default).strip())
    except (TypeError, ValueError):
        return int(default)


def _account_from_env(alias: str) -> dict[str, Any] | None:
    """Build one account dict from ALIAS_* env vars; None if not configured."""
    prefix = alias.upper()
    user = (os.getenv(f"{prefix}_SMTP_USER") or "").strip()

    if not user:
        return None

    return {
        "alias":     alias,
        "host":      (os.getenv(f"{prefix}_SMTP_HOST") or "").strip(),
        "port":      _int(f"{prefix}_SMTP_PORT", "587"),
        "user":      user,
        "password":  os.getenv(f"{prefix}_SMTP_PASSWORD") or "",
        "from_name": (os.getenv(f"{prefix}_FROM_NAME") or alias).strip(),
        "from_addr": (os.getenv(f"{prefix}_FROM_ADDR") or user).strip(),
        "imap_host": (os.getenv(f"{prefix}_IMAP_HOST") or "").strip(),
        "imap_port": _int(f"{prefix}_IMAP_PORT", "993"),
    }


# Aliases the system knows about. Add new ones here (and the matching
# ALIAS_SMTP_*/ALIAS_IMAP_* vars to .env) as new mailboxes get wired up.
_KNOWN_ALIASES = ["sales", "leif"]

MAIL_ACCOUNTS: dict[str, dict[str, Any]] = {}
for _alias in _KNOWN_ALIASES:
    _acc = _account_from_env(_alias)
    if _acc is not None:
        MAIL_ACCOUNTS[_alias] = _acc

DEFAULT_MAIL_ACCOUNT = (os.getenv("DEFAULT_MAIL_ACCOUNT") or "sales").strip()


def get_account(alias: str | None = None) -> dict[str, Any]:
    """
    Resolve an account dict by alias.

    Falls back to DEFAULT_MAIL_ACCOUNT, then to the first configured account.
    Raises RuntimeError only when NOTHING is configured. Callers MUST call this
    lazily (inside function bodies, never at module-import time) -- per the
    project's isolation rules, a missing/misconfigured account must not be able
    to crash an entire module or pipeline at import.
    """
    if alias and alias in MAIL_ACCOUNTS:
        return MAIL_ACCOUNTS[alias]

    if DEFAULT_MAIL_ACCOUNT in MAIL_ACCOUNTS:
        return MAIL_ACCOUNTS[DEFAULT_MAIL_ACCOUNT]

    if MAIL_ACCOUNTS:
        return next(iter(MAIL_ACCOUNTS.values()))

    raise RuntimeError(
        "[mail_accounts_config] No mail accounts configured. "
        "Set SALES_SMTP_USER (and the related SALES_*/LEIF_* vars) in .env"
    )


def smtp_uses_ssl(account: dict[str, Any]) -> bool:
    """
    Port 465 = implicit TLS/SSL (smtplib.SMTP_SSL).
    Port 587 (or anything else) = STARTTLS (smtplib.SMTP + .starttls()).
    """
    return account.get("port") == 465


def describe() -> str:  # pragma: no cover -- debugging helper, run this file directly
    def mask(v: str) -> str:
        return f"{v[:3]}***" if v else "(not set)"

    if not MAIL_ACCOUNTS:
        return "MAIL_ACCOUNTS: (none configured -- check .env)"

    lines = [
        f"  {alias}: smtp={acc['host']}:{acc['port']} "
        f"({'SSL' if smtp_uses_ssl(acc) else 'STARTTLS'})  user={acc['user']}  "
        f"password={mask(acc['password'])}  imap={acc['imap_host']}:{acc['imap_port']}"
        for alias, acc in MAIL_ACCOUNTS.items()
    ]
    return "MAIL_ACCOUNTS:\n" + "\n".join(lines)


if __name__ == "__main__":
    print(describe())
    print(f"DEFAULT_MAIL_ACCOUNT = {DEFAULT_MAIL_ACCOUNT}")
