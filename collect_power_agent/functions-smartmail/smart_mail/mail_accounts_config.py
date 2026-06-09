# functions-smartmail/smart_mail/mail_accounts_config.py
"""
Resolves named outreach-mailbox accounts (SMTP + IMAP) from the environment.

Deploy-time counterpart of app/mail_accounts_config.py. The logic is
identical -- the only difference is bootstrap: the local copy calls
load_dotenv() against the project .env; this one does NOT, because Cloud
Functions (2nd gen) loads .env / .env.<project-id> files from the function
source directory automatically at deploy time, and injects any declared
`secrets=[...]` (e.g. SALES_SMTP_PASSWORD) as environment variables at
runtime. Calling load_dotenv() here would be a no-op at best (no .env to
find at the deployed path) and a foot-gun at worst, so it's omitted.

Non-secret values (hosts, ports, usernames, tuning knobs) live in
functions-smartmail/.env.<project-id>; SALES_SMTP_PASSWORD is a Secret
Manager secret declared on the function (see main.py).

Exposes the same surface as the local module:
    MAIL_ACCOUNTS, DEFAULT_MAIL_ACCOUNT, get_account(alias), smtp_uses_ssl(account)
"""
from __future__ import annotations

import os
from typing import Any


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


# Aliases the system knows about -- keep in sync with app/mail_accounts_config.py.
_KNOWN_ALIASES = ["sales", "leif"]

MAIL_ACCOUNTS: dict[str, dict[str, Any]] = {}
for _alias in _KNOWN_ALIASES:
    _acc = _account_from_env(_alias)
    if _acc is not None:
        MAIL_ACCOUNTS[_alias] = _acc

DEFAULT_MAIL_ACCOUNT = (os.getenv("DEFAULT_MAIL_ACCOUNT") or "sales").strip()


def get_account(alias: str | None = None) -> dict[str, Any]:
    """
    Resolve an account dict by alias. Falls back to DEFAULT_MAIL_ACCOUNT,
    then the first configured account. Raises RuntimeError only when NOTHING
    is configured -- callers must call this lazily (never at import time).
    """
    if alias and alias in MAIL_ACCOUNTS:
        return MAIL_ACCOUNTS[alias]

    if DEFAULT_MAIL_ACCOUNT in MAIL_ACCOUNTS:
        return MAIL_ACCOUNTS[DEFAULT_MAIL_ACCOUNT]

    if MAIL_ACCOUNTS:
        return next(iter(MAIL_ACCOUNTS.values()))

    raise RuntimeError(
        "[mail_accounts_config] No mail accounts configured. "
        "Set SALES_SMTP_USER (and related SALES_*/LEIF_* vars) in "
        ".env.<project-id> + SALES_SMTP_PASSWORD as a function secret."
    )


def smtp_uses_ssl(account: dict[str, Any]) -> bool:
    """Port 465 = implicit TLS/SSL; 587 (or anything else) = STARTTLS."""
    return account.get("port") == 465


def describe() -> str:  # pragma: no cover -- debugging helper
    def mask(v: str) -> str:
        return f"{v[:3]}***" if v else "(not set)"

    if not MAIL_ACCOUNTS:
        return "MAIL_ACCOUNTS: (none configured -- check .env.<project-id> / secrets)"

    lines = [
        f"  {alias}: smtp={acc['host']}:{acc['port']} "
        f"({'SSL' if smtp_uses_ssl(acc) else 'STARTTLS'})  user={acc['user']}  "
        f"password={mask(acc['password'])}  imap={acc['imap_host']}:{acc['imap_port']}"
        for alias, acc in MAIL_ACCOUNTS.items()
    ]
    return "MAIL_ACCOUNTS:\n" + "\n".join(lines)
