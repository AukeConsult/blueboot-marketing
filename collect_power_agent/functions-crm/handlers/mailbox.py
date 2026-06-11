"""handlers/mailbox.py — IMAP mailbox reading endpoints."""
from __future__ import annotations
from flask import Blueprint, request, jsonify
from handlers.shared import _get_db, _ma_col, _get_mail_account, _err

bp = Blueprint("mailbox", __name__)

_SMTP_PORTS = {25, 465, 587, 2525}


def _imap_host(ma: dict) -> str:
    imap_host = str(ma.get("imap_host") or "").strip()
    host = str(ma.get("host") or "").strip()
    smtp_host = str(ma.get("smtp_host") or "").strip()
    if imap_host:
        return imap_host
    if host.startswith("smtp."):
        return host.replace("smtp.", "imap.", 1)
    if smtp_host and host and host == smtp_host and smtp_host.startswith("smtp."):
        return smtp_host.replace("smtp.", "imap.", 1)
    return host


def _imap_port(ma: dict, use_ssl: bool) -> int:
    raw_port = ma.get("imap_port")
    if raw_port in (None, ""):
        fallback_port = int(ma.get("port") or 0)
        port = 993 if fallback_port in _SMTP_PORTS or fallback_port <= 0 else fallback_port
    else:
        port = int(raw_port)
    if port in _SMTP_PORTS:
        port = 993 if use_ssl else 143
    return 143 if not use_ssl and port == 993 else port


@bp.route("/api/crm/settings/mail-accounts/<email>/mailbox", methods=["GET"])
def read_mailbox(email):
    """Read recent emails from all folders of a mail account.

    Query params:
      limit        int  total messages to return (default 50, no cap)
      folder_scope str  inbox|sent|inbox_sent|all (default inbox)

    Returns list of messages sorted newest first:
      { uid, folder, subject, from, to, date, preview, body }
    """
    import imaplib
    import email as _email
    import ssl as _ssl
    import base64
    import re as _re
    from email.header import decode_header as _dh
    from email.utils import parsedate_to_datetime

    def _decode_str(val):
        if not val:
            return ""
        parts = _dh(val)
        out = []
        for raw, enc in parts:
            if isinstance(raw, bytes):
                out.append(raw.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(raw)
        return " ".join(out)

    def _parse_folder(item):
        # Parse IMAP LIST item -> (selectable: bool, folder_name: str)
        # Format: (\flags) "delim" "name"  or  (\flags) "delim" name
        # Noselect folders are containers and cannot be selected.
        if isinstance(item, bytes):
            item = item.decode("utf-8", errors="replace")
        import re as _r
        flags_m = _r.match(r"\(([^)]*)\)", item)
        flags = flags_m.group(1).lower() if flags_m else ""
        selectable = "noselect" not in flags
        name_part = _r.sub(r"^\(.*?\)\s+(?:\"[^\"]*\"|NIL)\s*", "", item).strip()
        if name_part.startswith('"') and name_part.endswith('"'):
            name_part = name_part[1:-1]
        return selectable, name_part
    def _get_body_parts(msg):
        """Return (plain_text, html_body). Both may be empty strings."""
        text_body = ""
        html_body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                cd = str(part.get("Content-Disposition", ""))
                if "attachment" in cd:
                    continue
                charset = part.get_content_charset() or "utf-8"
                raw = part.get_payload(decode=True) or b""
                content = raw.decode(charset, errors="replace")
                if ct == "text/plain" and not text_body:
                    text_body = content
                elif ct == "text/html" and not html_body:
                    html_body = content
        else:
            ct = msg.get_content_type()
            raw = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            content = raw.decode(charset, errors="replace")
            if ct == "text/html":
                html_body = content
            else:
                text_body = content
        return text_body.strip(), html_body.strip()

    # Folders to skip — no outreach value
    _SKIP_FOLDERS = {
        "trash", "spam", "junk", "deleted items", "deleted messages",
        "[gmail]/trash", "[gmail]/spam", "[gmail]/important",
        "[gmail]/all mail", "[gmail]/starred",
    }

    def _select_folder(conn, fname):
        quoted = '"' + fname.replace('"', '\\"') + '"'
        typ, _ = conn.select(quoted, readonly=True)
        if typ != "OK":
            typ, _ = conn.select(fname, readonly=True)
        return typ == "OK"

    def _fetch_folder(conn, folder_name, per_folder):
        """Batch-fetch headers + short text preview in one IMAP round-trip.
        Groups the multi-literal response by message so we get both
        HEADER.FIELDS and BODY[TEXT] per UID."""
        msgs = []
        if folder_name.lower() in _SKIP_FOLDERS:
            return msgs
        try:
            if not _select_folder(conn, folder_name):
                return msgs
            typ, data = conn.uid("search", None, "ALL")
            if typ != "OK" or not data[0]:
                return msgs
            all_uids = data[0].split()
            if not all_uids:
                return msgs

            batch   = all_uids[-per_folder:]
            uid_set = b",".join(batch)
            # Two literals per message: HEADER.FIELDS + TEXT preview
            typ, raw_data = conn.uid(
                "fetch", uid_set,
                "(UID FLAGS INTERNALDATE"
                " BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)]"
                " BODY.PEEK[TEXT]<0.350>)"
            )
            if typ != "OK" or not raw_data:
                return msgs

            # Group raw_data items by message — each group ends with b')' 
            import re as _r2
            groups, cur = [], []
            for item in raw_data:
                if item == b")":
                    if cur:
                        groups.append(cur)
                    cur = []
                else:
                    cur.append(item)
            if cur:
                groups.append(cur)

            for group in groups:
                # group[0] = (meta_bytes_with_uid+internaldate+header_size, header_literal)
                # group[1] = (text_section_meta, text_literal)  — may be absent
                if not group or not isinstance(group[0], tuple) or len(group[0]) < 2:
                    continue
                # Identify each literal by its section name in the meta line so
                # we're resilient to servers that return sections in a different
                # order than requested.
                meta_raw = b""
                hdr_raw  = b""
                text_raw = b""
                for g_item in group:
                    if not isinstance(g_item, tuple) or len(g_item) < 2:
                        continue
                    g_meta = g_item[0] if isinstance(g_item[0], bytes) else b""
                    g_lit  = g_item[1] if isinstance(g_item[1], bytes) else b""
                    if b"HEADER.FIELDS" in g_meta or b"header.fields" in g_meta.lower():
                        hdr_raw  = g_lit
                        if not meta_raw:
                            meta_raw = g_meta  # UID/INTERNALDATE are on this line
                    elif b"BODY[TEXT]" in g_meta or b"body[text]" in g_meta.lower():
                        text_raw = g_lit
                    else:
                        # First tuple carries UID/FLAGS/INTERNALDATE regardless
                        if not meta_raw:
                            meta_raw = g_meta
                        if not hdr_raw and g_lit:
                            hdr_raw = g_lit   # fallback: first literal is headers
                try:
                    uid_m   = _r2.search(rb"UID\s+(\d+)", meta_raw)
                    uid_str = uid_m.group(1).decode() if uid_m else ""

                    id_m = _r2.search(rb'INTERNALDATE\s+"([^"]+)"', meta_raw)
                    date_received = ""
                    if id_m:
                        try:
                            import imaplib as _il2, datetime as _dt
                            tup = _il2.Internaldate2tuple(
                                b'INTERNALDATE "' + id_m.group(1) + b'"'
                            )
                            if tup:
                                date_received = _dt.datetime(
                                    *tup[:6], tzinfo=_dt.timezone.utc
                                ).isoformat()
                        except Exception:
                            pass

                    hdr_msg    = _email.message_from_bytes(hdr_raw)
                    subject    = _decode_str(hdr_msg.get("Subject", "(no subject)")) or "(no subject)"
                    from_str   = _decode_str(hdr_msg.get("From", ""))
                    to_str     = _decode_str(hdr_msg.get("To", ""))
                    raw_date   = hdr_msg.get("Date", "")
                    message_id = hdr_msg.get("Message-ID", "").strip()
                    try:
                        date_sent = parsedate_to_datetime(raw_date).isoformat()
                    except Exception:
                        date_sent = raw_date

                    preview = ""
                    if text_raw:
                        preview = text_raw.decode("utf-8", errors="replace")
                        preview = _re.sub(r"<[^>]+>", " ", preview)
                        preview = _re.sub(r"\s+", " ", preview).strip()[:200]

                    msgs.append({
                        "uid":           uid_str,
                        "folder":        folder_name,
                        "subject":       subject,
                        "from":          from_str,
                        "to":            to_str,
                        "date_sent":     date_sent,
                        "date_received": date_received,
                        "message_id":    message_id,
                        "preview":       preview,
                        "body":          "",   # full body loaded on demand
                    })
                except Exception:
                    pass

            msgs.sort(key=lambda m: m.get("date_received") or m.get("date_sent", ""), reverse=True)
        except Exception:
            pass
        return msgs

    try:
        db  = _get_db()
        key = email.strip().lower()
        ma  = _get_mail_account(db, key)
        if not ma:
            return _err(f"Mail account '{key}' not found", 404)

        limit        = max(10, int(request.args.get("limit", 50)))   # no upper cap
        folder_scope = request.args.get("folder_scope", "inbox")    # inbox|sent|inbox_sent|all
        account_type = ma.get("account_type", "imap")
        messages     = []

        if account_type == "imap":
            host     = _imap_host(ma)
            username = ma.get("username", "").strip()
            password = ma.get("password", "")
            use_ssl  = ma.get("ssl", True)
            port     = _imap_port(ma, use_ssl)
            if not host or not username:
                return _err("IMAP host and username are required", 400)
            if use_ssl:
                conn = imaplib.IMAP4_SSL(host, port, ssl_context=_ssl.create_default_context())
            else:
                conn = imaplib.IMAP4(host, port)
            conn.login(username, password)

        elif account_type == "gmail":
            client_id     = ma.get("client_id", "").strip()
            client_secret = ma.get("client_secret", "").strip()
            refresh_token = ma.get("refresh_token", "").strip()
            access_token  = ma.get("access_token", "").strip()
            if not refresh_token:
                return _err("refresh_token is required for Gmail", 400)
            if not access_token:
                import urllib.request
                import json as _json
                p = (
                    f"client_id={urllib.parse.quote(client_id)}"
                    f"&client_secret={urllib.parse.quote(client_secret)}"
                    f"&refresh_token={urllib.parse.quote(refresh_token)}"
                    f"&grant_type=refresh_token"
                ).encode()
                req = urllib.request.Request(
                    "https://oauth2.googleapis.com/token", data=p,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    td = _json.loads(resp.read())
                access_token = td.get("access_token", "")
                if not access_token:
                    return jsonify({"status": "error",
                                    "message": td.get("error_description", "Token refresh failed")})
                _ma_col(db).document(key).update({"access_token": access_token})

        else:
            return _err(f"Unsupported account_type '{account_type}'", 400)

        # --- IMAP: build connection for Gmail via XOAUTH2 ---
        if account_type == "gmail":
            auth_str = f"user={key}auth=Bearer {access_token}"
            conn = imaplib.IMAP4_SSL("imap.gmail.com", 993,
                                     ssl_context=_ssl.create_default_context())
            conn.authenticate("XOAUTH2", lambda _: auth_str.encode())

        # --- resolve folders based on scope ---
        _SENT_CANDIDATES = [
            "Sent", "Sent Items", "Sent Messages",
            "[Gmail]/Sent Mail", "INBOX.Sent",
        ]

        def _find_sent(conn):
            """Return the first selectable sent-folder name found on the server."""
            for name in _SENT_CANDIDATES:
                try:
                    quoted = '"' + name.replace('"', '\\"') + '"'
                    typ, _ = conn.select(quoted, readonly=True)
                    if typ == "OK":
                        conn.close()
                        return name
                except Exception:
                    pass
            return None

        # --- list selectable folders and fetch messages ---
        try:
            if folder_scope == "inbox":
                folders = ["INBOX"]
            elif folder_scope == "sent":
                sent = _find_sent(conn)
                folders = [sent] if sent else []
            elif folder_scope == "inbox_sent":
                sent = _find_sent(conn)
                folders = ["INBOX"] + ([sent] if sent else [])
            else:  # "all"
                typ, raw_list = conn.list()
                folders = []
                if typ == "OK":
                    for item in raw_list:
                        selectable, fname = _parse_folder(item)
                        if selectable and fname:
                            folders.append(fname)
                if not folders:
                    folders = ["INBOX"]

            for folder in folders:
                messages.extend(_fetch_folder(conn, folder, limit))
        finally:
            try:
                conn.logout()
            except Exception:
                pass

        messages.sort(
            key=lambda m: m.get("date_received") or m.get("date_sent", ""),
            reverse=True
        )
        messages = messages[:limit]   # trim merged results to requested limit

        return jsonify({"status": "ok", "messages": messages})

    except Exception as exc:
        return _err(str(exc))


@bp.route("/api/crm/settings/mail-accounts/<email>/message", methods=["GET"])
def read_message_body(email):
    """Fetch the full body of a single message on demand.

    Query params:
      folder  str  IMAP folder name
      uid     str  IMAP UID

    Returns: { status: "ok", body: "..." }
    """
    import imaplib
    import email as _email
    import ssl as _ssl
    import re as _re
    from email.header import decode_header as _dh

    def _decode_str(val):
        if not val:
            return ""
        parts = _dh(val)
        out = []
        for raw, enc in parts:
            if isinstance(raw, bytes):
                out.append(raw.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(raw)
        return " ".join(out)

    def _get_body_parts(msg):
        """Return (plain_text, html_body). Prefers text/plain; also extracts text/html."""
        text_body, html_body = "", ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                cd = str(part.get("Content-Disposition", ""))
                if "attachment" in cd:
                    continue
                charset = part.get_content_charset() or "utf-8"
                raw = part.get_payload(decode=True) or b""
                content = raw.decode(charset, errors="replace")
                if ct == "text/plain" and not text_body:
                    text_body = content
                elif ct == "text/html" and not html_body:
                    html_body = content
        else:
            ct = msg.get_content_type()
            raw = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            content = raw.decode(charset, errors="replace")
            if ct == "text/html":
                html_body = content
            else:
                text_body = content
        return text_body.strip(), html_body.strip()

    try:
        folder = request.args.get("folder", "").strip()
        uid    = request.args.get("uid", "").strip()
        if not folder or not uid:
            return _err("folder and uid are required", 400)

        db  = _get_db()
        key = email.strip().lower()
        ma  = _get_mail_account(db, key)
        if not ma:
            return _err(f"Mail account '{key}' not found", 404)

        account_type = ma.get("account_type", "imap")
        if account_type == "imap":
            host    = _imap_host(ma)
            use_ssl = ma.get("ssl", True)
            port    = _imap_port(ma, use_ssl)
            if not host:
                return _err("IMAP host not configured", 400)
            if use_ssl:
                conn = imaplib.IMAP4_SSL(host, port, ssl_context=_ssl.create_default_context())
            else:
                conn = imaplib.IMAP4(host, port)
            conn.login(ma.get("username", ""), ma.get("password", ""))
        elif account_type == "gmail":
            access_token = ma.get("access_token", "").strip()
            if not access_token:
                return _err("Gmail access_token not available", 400)
            auth_str = f"user={key}\x01auth=Bearer {access_token}\x01\x01"
            conn = imaplib.IMAP4_SSL("imap.gmail.com", 993,
                                     ssl_context=_ssl.create_default_context())
            conn.authenticate("XOAUTH2", lambda _: auth_str.encode())
        else:
            return _err(f"Unsupported account_type", 400)

        try:
            quoted = '"' + folder.replace('"', '\\"') + '"'
            typ, _ = conn.select(quoted, readonly=True)
            if typ != "OK":
                conn.select(folder, readonly=True)
            typ, raw = conn.uid("fetch", uid.encode(), "(RFC822)")
            if typ != "OK" or not raw or not raw[0]:
                return _err("Message not found", 404)
            msg  = _email.message_from_bytes(raw[0][1])
            text_body, html_body = _get_body_parts(msg)

            # Replace cid: image references with inline base64 data URIs
            if html_body:
                import base64 as _b64, re as _ri
                cid_map = {}
                for part in msg.walk():
                    for hdr in ("Content-ID", "X-Attachment-Id"):
                        raw_cid = part.get(hdr, "").strip()
                        if not raw_cid:
                            continue
                        cid_clean = raw_cid.strip("<>").strip()
                        if not cid_clean:
                            continue
                        ct      = part.get_content_type()
                        raw_img = part.get_payload(decode=True)
                        if raw_img and len(raw_img) <= 600_000:
                            data_uri = (
                                "data:" + ct + ";base64,"
                                + _b64.b64encode(raw_img).decode("ascii")
                            )
                            cid_map[cid_clean] = data_uri
                            local = cid_clean.split("@")[0]
                            if local != cid_clean:
                                cid_map.setdefault(local, data_uri)
                if cid_map:
                    def _sub_cid(m):
                        key = m.group(2).strip()
                        repl = (
                            cid_map.get(key)
                            or cid_map.get(key.split("@")[0])
                            or ("cid:" + key)
                        )
                        return m.group(1) + repl + m.group(3)
                    html_body = _ri.sub(
                        r'(src=["\'"])cid:([^"\' ]+)(["\'"])',
                        _sub_cid, html_body, flags=_ri.IGNORECASE
                    )
        finally:
            try:
                conn.logout()
            except Exception:
                pass

        return jsonify({
            "status":    "ok",
            "body":      text_body[:20000],
            "body_html": html_body[:5_000_000],
        })
    except Exception as exc:
        return _err(str(exc))
