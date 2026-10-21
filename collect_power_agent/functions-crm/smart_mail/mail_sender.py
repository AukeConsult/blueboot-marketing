"""
functions-crm/smart_mail/mail_sender.py -- Isolated mail sending class.

All outbound email goes through MailSender so fixes (display name, CSS
inlining, headers, SSL/STARTTLS) apply everywhere.

Usage:
    from smart_mail.mail_sender import MailSender
    sender = MailSender(mail_account_dict)
    result = sender.send(to="x@y.com", subject="Hi", body_plain="Hello")
    ok     = sender.ping()
"""
from __future__ import annotations

import re
import time
import uuid
import smtplib
import imaplib
import ssl
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate

_SMTP_PORTS = {25, 465, 587, 2525}

# Where a copy of a sent mail is filed on IMAP accounts (SMTP never saves one by itself):
#   "sent"      -> the Sent folder        (mail a person sent by hand: CRM page, test mails)
#   "processed" -> the Processed_Sent folder (mail the automatic outreach run sent)
#   "none"      -> no copy
# Gmail accounts already get every mail in Sent from Gmail itself, so nothing is appended there.
SAVE_SENT, SAVE_PROCESSED, SAVE_NONE = "sent", "processed", "none"
_SENT_NAMES = ("sent", "sent items", "sent messages", "inbox.sent", "inbox/sent", "[gmail]/sent mail")
_PROCESSED_NAME = "Processed_Sent"


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


class MailSender:
    def __init__(self, ma: dict):
        self.ma           = ma
        self.account_type = ma.get("account_type", "imap")
        self.email        = ma.get("email", "").strip().lower()
        self.display_name = ma.get("display_name", "").strip()
        self._smtp_server = None
        self._smtp_username = ""
        self._smtp_host = ""
        self._smtp_port = 0
        self._imap_conn = None          # lazily opened, reused for every copy of one run
        self._folder_cache: dict = {}

    # ── Public API ────────────────────────────────────────────────────────

    def send(self, *, to: str, subject: str,
             body_plain: str = "", body_html: str = "",
             in_reply_to: str | None = None, headers: dict | None = None,
             save_copy: str = SAVE_SENT) -> dict:
        """One-off send (manual: CRM page, test mails). A copy goes to the Sent folder."""
        try:
            msg = self._build_message(to=to, subject=subject,
                                      body_plain=body_plain, body_html=body_html,
                                      in_reply_to=in_reply_to, headers=headers)
            if self.account_type == "imap":
                return self._send_smtp(msg, to, save_copy)
            elif self.account_type == "gmail":
                return self._send_gmail(msg, to)
            return {"status": "error", "message": f"Unknown account type: {self.account_type}"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def open(self) -> dict:
        """Open one authenticated SMTP session for repeated sends."""
        try:
            self.close()
            if self.account_type == "imap":
                server, username, smtp_host, smtp_port = self._smtp_connect()
                self._smtp_server = server
                self._smtp_username = username
                self._smtp_host = smtp_host
                self._smtp_port = smtp_port
                return {"status": "ok", "message": f"SMTP opened for {username} via {smtp_host}:{smtp_port}"}
            if self.account_type == "gmail":
                if not (self.ma.get("access_token") or self.ma.get("refresh_token")):
                    return {"status": "error", "message": "access_token or refresh_token is required"}
                server = smtplib.SMTP("smtp.gmail.com", 587, timeout=15)
                server.ehlo()
                server.starttls()
                server.ehlo()
                self._xoauth2(server)
                self._smtp_server = server
                self._smtp_username = self.email
                self._smtp_host = "smtp.gmail.com"
                self._smtp_port = 587
                return {"status": "ok", "message": f"Gmail SMTP opened for {self.email}"}
            return {"status": "error", "message": f"Unknown account type: {self.account_type}"}
        except smtplib.SMTPAuthenticationError as exc:
            return {"status": "error", "message": f"SMTP auth failed: {exc}"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def close(self) -> None:
        conn, self._imap_conn = self._imap_conn, None
        self._folder_cache = {}
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass
        server = self._smtp_server
        self._smtp_server = None
        self._smtp_username = ""
        self._smtp_host = ""
        self._smtp_port = 0
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass

    def send_open(self, *, to: str, subject: str,
                  body_plain: str = "", body_html: str = "",
                  in_reply_to: str | None = None,
                  headers: dict | None = None, _retry: bool = True,
                  save_copy: str = SAVE_PROCESSED) -> dict:
        """Send through the currently open SMTP session (the automatic outreach run).
        A copy goes to the Processed_Sent folder, not Sent.
        If the server dropped the connection, it is reopened once and the mail is sent again."""
        if self._smtp_server is None:
            return {"status": "error", "message": "MailSender is not open"}
        try:
            msg = self._build_message(to=to, subject=subject,
                                      body_plain=body_plain, body_html=body_html,
                                      in_reply_to=in_reply_to, headers=headers)
            if self.account_type == "imap":
                sender_addr = self._smtp_username
                msg["From"] = self._from_header(sender_addr)
                self._smtp_server.sendmail(sender_addr, [to], msg.as_string())
                return {
                    "status": "ok",
                    "message": f"Email sent to {to} via {self._smtp_host}:{self._smtp_port}",
                    "message_id": msg.get("Message-ID", ""),
                    **self._save_copy(msg, save_copy),
                }
            if self.account_type == "gmail":
                msg["From"] = self._from_header(self.email)
                self._smtp_server.sendmail(self.email, [to], msg.as_string())
                return {
                    "status": "ok",
                    "message": f"Email sent to {to} via Gmail",
                    "message_id": msg.get("Message-ID", ""),
                }
            return {"status": "error", "message": f"Unknown account type: {self.account_type}"}
        except smtplib.SMTPServerDisconnected as exc:
            if _retry and self.open().get("status") == "ok":
                return self.send_open(to=to, subject=subject, body_plain=body_plain, body_html=body_html,
                                      in_reply_to=in_reply_to, headers=headers, _retry=False,
                                      save_copy=save_copy)
            return {"status": "error", "message": f"SMTP connection lost: {exc}"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def __enter__(self):
        result = self.open()
        if result.get("status") != "ok":
            raise RuntimeError(result.get("message", "MailSender open failed"))
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def ping(self) -> dict:
        try:
            if self.account_type == "imap":
                return self._ping_imap()
            elif self.account_type == "gmail":
                return self._ping_gmail()
            return {"status": "error", "message": f"Unknown account type: {self.account_type}"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    # ── Message building ──────────────────────────────────────────────────

    def _from_header(self, addr: str) -> str:
        return f"{self.display_name} <{addr}>" if self.display_name else addr

    @staticmethod
    def _inline_css(html: str) -> str:
        try:
            from premailer import transform
            return transform(html, remove_classes=False,
                             strip_important=False, allow_network=False)
        except Exception:
            return re.sub(r"<style[^>]*>.*?</style>", "", html,
                          flags=re.DOTALL | re.IGNORECASE)

    @staticmethod
    def _html_to_plain(html: str) -> str:
        text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
        text = re.sub(r"<p[^>]*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    @staticmethod
    def _extract_inline_images(html: str):
        """Replace data: URI images with cid: references.
        Returns (new_html, [(cid, mime_type, raw_bytes), ...]).
        """
        import re as _re, base64 as _b64
        parts = []
        def _replace(m):
            quote    = m.group(1)   # ' or "
            mime_type = m.group(2)  # e.g. image/png
            b64data   = m.group(3)
            try:
                raw = _b64.b64decode(b64data)
            except Exception:
                return m.group(0)   # leave untouched on decode error
            cid = f"img_{uuid.uuid4().hex}@blueboot.ai"
            parts.append((cid, mime_type, raw))
            return f"{quote}cid:{cid}{quote}"
        new_html = _re.sub(
            r"""(?:src=)(["'])data:(image/[^;]+);base64,([A-Za-z0-9+/=]+)\1""",
            _replace, html
        )
        return new_html, parts

    def _build_message(self, *, to: str, subject: str,
                       body_plain: str, body_html: str,
                       in_reply_to: str | None = None,
                       headers: dict | None = None):
        if body_html:
            clean_html = self._inline_css(body_html)
            # Convert any data: URI images → CID MIME attachments so they
            # survive email servers (e.g. Gmail) that strip inline base64.
            clean_html, inline_images = self._extract_inline_images(clean_html)
            plain = body_plain or self._html_to_plain(clean_html)

            if inline_images:
                # multipart/related wraps html + inline images
                from email.mime.image import MIMEImage
                related = MIMEMultipart("related")
                alt = MIMEMultipart("alternative")
                alt.attach(MIMEText(plain,      "plain", "utf-8"))
                alt.attach(MIMEText(clean_html, "html",  "utf-8"))
                related.attach(alt)
                for cid, mime_type, raw in inline_images:
                    subtype = mime_type.split("/", 1)[1] if "/" in mime_type else mime_type
                    img_part = MIMEImage(raw, _subtype=subtype)
                    img_part["Content-ID"]          = f"<{cid}>"
                    img_part["Content-Disposition"] = "inline"
                    related.attach(img_part)
                msg = related
            else:
                msg = MIMEMultipart("alternative")
                msg.attach(MIMEText(plain,      "plain", "utf-8"))
                msg.attach(MIMEText(clean_html, "html",  "utf-8"))
        else:
            msg = MIMEText(body_plain or "(no body)", "plain", "utf-8")
        msg["Subject"]    = subject
        msg["To"]         = to
        msg["Date"]       = formatdate(localtime=False)
        # Message-ID uses the domain of the mailbox we send from (matches From/DKIM).
        _dom = (getattr(self, "email", "") or "").rpartition("@")[2].strip().lower() or "blueboot.ai"
        msg["Message-ID"] = f"<{uuid.uuid4().hex}@{_dom}>"
        # Tag CRM-originated mail so the SENT-folder sync (run_sent_sync) can
        # recognise it and NOT re-log it (outreach + manual already log at send
        # time). Only mail sent from OUTSIDE the CRM lacks this header.
        msg["X-Blueboot-Sent"] = "crm"
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        for key, value in (headers or {}).items():
            if value:
                msg[key] = value
        return msg

    # ── SMTP (IMAP account) ───────────────────────────────────────────────

    def _smtp_params(self):
        imap_host = (self.ma.get("imap_host") or self.ma.get("host") or "").strip()
        username  = self.ma.get("username", "").strip()
        password  = self.ma.get("password", "")
        smtp_host = self.ma.get("smtp_host", "").strip() or (
            imap_host.replace("imap.", "smtp.", 1)
            if imap_host.startswith("imap.") else imap_host
        )
        smtp_port = int(self.ma.get("smtp_port") or 587)
        smtp_ssl  = self._as_bool(self.ma.get("smtp_ssl", False)) or smtp_port == 465
        return username, password, smtp_host, smtp_port, smtp_ssl

    @staticmethod
    def _as_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
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

    def _smtp_connect(self):
        username, password, smtp_host, smtp_port, smtp_ssl = self._smtp_params()
        # Say a real hostname in EHLO; the default in Cloud Functions is "[127.0.0.1]",
        # which then shows up in the first Received header.
        helo = (self.email or "").rpartition("@")[2].strip().lower() or "blueboot.ai"
        if smtp_ssl:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15, local_hostname=helo)
            server.ehlo()
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=15, local_hostname=helo)
            server.ehlo()
            server.starttls()
            server.ehlo()
        server.login(username, password)
        return server, username, smtp_host, smtp_port

    def _send_smtp(self, msg, to: str, save_copy: str = SAVE_SENT) -> dict:
        username = self.ma.get("username", "").strip()
        _, _, smtp_host, smtp_port, _ = self._smtp_params()
        if not smtp_host or not username:
            return {"status": "error", "message": "SMTP host and username are required"}
        msg["From"] = self._from_header(username)
        print(f"[mail_sender] SMTP send to {to} via {smtp_host}:{smtp_port} as {username}")
        try:
            server, username, smtp_host, smtp_port = self._smtp_connect()
            with server:
                server.sendmail(username, [to], msg.as_string())
            res = {"status": "ok",
                   "message": f"Email sent to {to} via {smtp_host}:{smtp_port}",
                   "message_id": msg.get("Message-ID", "")}
            res.update(self._save_copy(msg, save_copy))
            try:
                self.close()          # the IMAP connection used for the copy (one-off send)
            except Exception:
                pass
            return res
        except smtplib.SMTPAuthenticationError as e:
            print(f"[mail_sender] SMTP auth failed for {username} on {smtp_host}:{smtp_port}: {e}")
            return {"status": "error", "message": f"SMTP auth failed: {e}"}
        except Exception as e:
            print(f"[mail_sender] SMTP error on {smtp_host}:{smtp_port}: {type(e).__name__}: {e}")
            return {"status": "error",
                    "message": f"SMTP error ({smtp_host}:{smtp_port}): {e}"}

    def _imap_connect(self, timeout: float | None = None):
        host     = _imap_host(self.ma)
        username = self.ma.get("username", "").strip()
        password = self.ma.get("password", "")
        use_ssl  = self._as_bool(self.ma.get("ssl", True))
        port     = self._imap_port(self.ma, use_ssl)
        if use_ssl:
            conn = imaplib.IMAP4_SSL(host, port,
                   ssl_context=ssl.create_default_context(), timeout=timeout)
        else:
            conn = imaplib.IMAP4(host, port, timeout=timeout)
        conn.login(username, password)
        return conn

    # ── Copy of a sent mail into an IMAP folder ───────────────────────────

    def _save_copy(self, msg, kind: str) -> dict:
        """File a copy of the sent mail (IMAP accounts only). Never raises: the mail is already sent."""
        if kind == SAVE_NONE or self.account_type != "imap":
            return {}
        try:
            if self._imap_conn is None:
                print(f"[mail_sender] opening IMAP to file the {kind!r} copy for {self.email}")
                self._imap_conn = self._imap_connect(timeout=15)   # never let the copy hang a sent mail
            conn = self._imap_conn
            folder = self._folder_cache.get(kind)
            if folder is None:
                folder = self._ensure_folder(conn, kind)
                self._folder_cache[kind] = folder
            typ, resp = conn.append(f'"{folder}"', "(\\Seen)", imaplib.Time2Internaldate(time.time()),
                                    msg.as_bytes())
            if typ != "OK":
                raise RuntimeError(f"APPEND refused: {resp}")
            print(f"[mail_sender] copy saved in {folder!r} for {self.email}")
            return {"copy_folder": folder}
        except Exception as exc:
            print(f"[mail_sender] could not save a copy in the {kind!r} folder: {exc}")
            self._imap_conn = None        # reconnect next time
            return {"copy_error": str(exc)}

    @staticmethod
    def _list_folders(conn) -> tuple[list[tuple[str, str]], str]:
        """([(folder name, flags)], hierarchy delimiter)"""
        out, delim = [], "/"
        _typ, items = conn.list()
        for it in items or []:
            raw = it.decode(errors="replace") if isinstance(it, bytes) else str(it)
            m = re.match(r'\((?P<fl>[^)]*)\)\s+(?:"(?P<d>[^"]*)"|NIL)\s+(?P<n>.+)$', raw)
            if not m:
                continue
            delim = m.group("d") or delim
            out.append((m.group("n").strip().strip('"'), m.group("fl")))
        return out, delim

    def _ensure_folder(self, conn, kind: str) -> str:
        """Name of the Sent / Processed folder on this server; created and subscribed when missing."""
        folders, delim = self._list_folders(conn)
        wanted = _PROCESSED_NAME if kind == SAVE_PROCESSED else "Sent"
        found = None
        for name, flags in folders:
            low = name.lower()
            if kind == SAVE_SENT and "\\sent" in flags.lower():
                found = name
                break
            if kind == SAVE_SENT and low in _SENT_NAMES:
                found = found or name
            if kind == SAVE_PROCESSED and low in (_PROCESSED_NAME.lower(), f"inbox{delim}{_PROCESSED_NAME}".lower()):
                found = name
                break
        if found is None:
            # cPanel / Dovecot keep folders under INBOX (INBOX.Sent): only INBOX.<name> is accepted there
            prefix = f"INBOX{delim}" if any(n.startswith(f"INBOX{delim}") for n, _f in folders) else ""
            found = prefix + wanted
            typ, resp = conn.create(f'"{found}"')
            if typ != "OK":
                raise RuntimeError(f"could not create folder {found!r}: {resp}")
        try:
            conn.subscribe(f'"{found}"')     # unsubscribed folders are hidden in many mail clients
        except Exception:
            pass
        return found

    def _ping_imap(self) -> dict:
        host     = _imap_host(self.ma)
        username = self.ma.get("username", "").strip()
        password = self.ma.get("password", "")
        use_ssl  = self._as_bool(self.ma.get("ssl", True))
        port     = self._imap_port(self.ma, use_ssl)
        if not host or not username:
            return {"status": "error", "message": "IMAP host and username are required"}
        try:
            conn = self._imap_connect()
            conn.logout()
            return {"status": "ok", "message": f"Connected to {host}:{port} as {username}"}
        except imaplib.IMAP4.error as e:
            return {"status": "error", "message": f"IMAP auth failed: {e}"}
        except Exception as e:
            return {"status": "error", "message": f"Cannot reach {host}:{port} — {e}"}

    # ── Gmail OAuth2 ──────────────────────────────────────────────────────

    def _get_access_token(self) -> str:
        import urllib.request, urllib.parse as _up, json as _json
        client_id     = self.ma.get("client_id", "").strip()
        client_secret = self.ma.get("client_secret", "").strip()
        refresh_token = self.ma.get("refresh_token", "").strip()
        payload = (
            f"client_id={_up.quote(client_id)}"
            f"&client_secret={_up.quote(client_secret)}"
            f"&refresh_token={_up.quote(refresh_token)}"
            f"&grant_type=refresh_token"
        ).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())
        token = data.get("access_token", "")
        if not token:
            raise ValueError(data.get("error_description", "Token refresh failed"))
        return token

    def _xoauth2(self, server, auth_b64: str | None = None) -> None:
        """Authenticate and CHECK the answer: docmd only returns it, it never raises on a refusal."""
        code, resp = server.docmd("AUTH", "XOAUTH2 " + (auth_b64 or self._gmail_auth_b64()))
        if code != 235:
            raise smtplib.SMTPAuthenticationError(code, resp)

    def _gmail_auth_b64(self) -> str:
        token    = self.ma.get("access_token", "").strip() or self._get_access_token()
        auth_str = f"user={self.email}\x01auth=Bearer {token}\x01\x01"
        return base64.b64encode(auth_str.encode()).decode()

    def _send_gmail(self, msg, to: str) -> dict:
        if not (self.ma.get("access_token") or self.ma.get("refresh_token")):
            return {"status": "error", "message": "access_token or refresh_token is required"}
        msg["From"] = self._from_header(self.email)
        try:
            auth_b64 = self._gmail_auth_b64()
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
                server.ehlo()
                server.starttls()
                self._xoauth2(server, auth_b64)
                server.sendmail(self.email, [to], msg.as_string())
            # Gmail saves to Sent automatically — no APPEND needed
            return {"status": "ok",
                    "message": f"Email sent to {to} via Gmail",
                    "message_id": msg.get("Message-ID", "")}
        except smtplib.SMTPAuthenticationError as e:
            return {"status": "error", "message": f"Gmail auth failed: {e}"}
        except Exception as e:
            return {"status": "error", "message": f"Gmail SMTP error: {e}"}

    def _ping_gmail(self) -> dict:
        if not (self.ma.get("access_token") or self.ma.get("refresh_token")):
            return {"status": "error",
                    "message": "access_token or refresh_token is required for Gmail"}
        try:
            if not self.ma.get("access_token"):
                self._get_access_token()
            return {"status": "ok", "message": f"Gmail auth OK for {self.email}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
