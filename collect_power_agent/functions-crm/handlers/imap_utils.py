"""handlers/imap_utils.py — Shared IMAP connection helpers.

Imported by both mailbox.py and mail_tags.py. No Blueprint here.
"""
from __future__ import annotations


def _sanitize_imap_keyword(s: str) -> str:
    """Convert a string to a valid IMAP keyword atom (no spaces/special chars)."""
    import re as _r
    return _r.sub(r"[^A-Za-z0-9_\-]", "_", s)[:64]


def _imap_connect(ma: dict, account_email: str):
    """Return an authenticated imaplib connection for imap or gmail accounts.
    Caller is responsible for conn.logout(). Raises on failure.
    """
    import imaplib as _il, ssl as _ssl
    account_type = ma.get("account_type", "imap")
    if account_type == "imap":
        host    = ma.get("host", "").strip()
        port    = int(ma.get("port") or 993)
        use_ssl = ma.get("ssl", True)
        if not host:
            raise ValueError("IMAP host is not configured")
        if use_ssl:
            conn = _il.IMAP4_SSL(host, port, ssl_context=_ssl.create_default_context())
        else:
            conn = _il.IMAP4(host, port)
        conn.login(ma.get("username", ""), ma.get("password", ""))
        return conn
    elif account_type == "gmail":
        access_token = ma.get("access_token", "").strip()
        if not access_token:
            raise ValueError("Gmail access_token not available — refresh first")
        auth_str = f"user={account_email}\x01auth=Bearer {access_token}\x01\x01"
        import imaplib as _il2, ssl as _ssl2
        conn = _il2.IMAP4_SSL("imap.gmail.com", 993,
                              ssl_context=_ssl2.create_default_context())
        conn.authenticate("XOAUTH2", lambda _: auth_str.encode())
        return conn
    else:
        raise ValueError(f"Unsupported account_type '{account_type}'")


def _sync_tags_to_imap(ma: dict, account_email: str,
                        folder: str, uid: str,
                        status: str, labels: list) -> str | None:
    """Apply status + label tags as IMAP keyword flags on the given message.

    Removes all existing Blueboot_* flags first, then adds new ones.
    Returns None on success, error string on failure (best-effort).
    """
    if not folder or not uid:
        return "folder/uid missing — cannot sync to IMAP"
    try:
        conn = _imap_connect(ma, account_email)
        try:
            quoted = '"' + folder.replace('"', '\\"') + '"'
            typ, _ = conn.select(quoted)
            if typ != "OK":
                typ, _ = conn.select(folder)
            if typ != "OK":
                return f"Could not select folder {folder!r}"

            uid_b = uid.encode() if isinstance(uid, str) else uid

            import re as _r
            typ, fdata = conn.uid("fetch", uid_b, "(FLAGS)")
            old_bb = []
            if typ == "OK" and fdata:
                for item in fdata:
                    raw  = (item[0] if isinstance(item, tuple) else item) or b""
                    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
                    old_bb.extend(_r.findall(r"Blueboot_\S+", text))
            if old_bb:
                conn.uid("store", uid_b, "-FLAGS",
                         "(" + " ".join(dict.fromkeys(old_bb)) + ")")

            new_flags = []
            if status:
                new_flags.append("Blueboot_" + _sanitize_imap_keyword(status))
            for lbl in labels:
                if lbl:
                    new_flags.append("Blueboot_" + _sanitize_imap_keyword(lbl))
            if new_flags:
                conn.uid("store", uid_b, "+FLAGS",
                         "(" + " ".join(new_flags) + ")")
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return None
    except Exception as exc:
        return str(exc)
