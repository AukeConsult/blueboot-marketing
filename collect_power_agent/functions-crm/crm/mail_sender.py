"""
crm/mail_sender.py -- Isolated mail sending class.

All outbound email goes through MailSender so fixes (display name, CSS
inlining, headers, SSL/STARTTLS, Sent folder append) apply everywhere.

Usage:
    from crm.mail_sender import MailSender
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


class MailSender:
    def __init__(self, ma: dict):
        self.ma           = ma
        self.account_type = ma.get("account_type", "imap")
        self.email        = ma.get("email", "").strip().lower()
        self.display_name = ma.get("display_name", "").strip()

    # ── Public API ────────────────────────────────────────────────────────

    def send(self, *, to: str, subject: str,
             body_plain: str = "", body_html: str = "") -> dict:
        try:
            msg = self._build_message(to=to, subject=subject,
                                      body_plain=body_plain, body_html=body_html)
            if self.account_type == "imap":
                return self._send_smtp(msg, to)
            elif self.account_type == "gmail":
                return self._send_gmail(msg, to)
            return {"status": "error", "message": f"Unknown account type: {self.account_type}"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

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

    def _build_message(self, *, to: str, subject: str,
                       body_plain: str, body_html: str):
        if body_html:
            clean_html = self._inline_css(body_html)
            plain      = body_plain or self._html_to_plain(clean_html)
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(plain,      "plain", "utf-8"))
            msg.attach(MIMEText(clean_html, "html",  "utf-8"))
        else:
            msg = MIMEText(body_plain or "(no body)", "plain", "utf-8")
        msg["Subject"]    = subject
        msg["To"]         = to
        msg["Date"]       = formatdate(localtime=False)
        msg["Message-ID"] = f"<{uuid.uuid4().hex}@blueboot.ai>"
        return msg

    # ── SMTP (IMAP account) ───────────────────────────────────────────────

    def _smtp_params(self):
        imap_host = self.ma.get("host", "").strip()
        username  = self.ma.get("username", "").strip()
        password  = self.ma.get("password", "")
        smtp_host = self.ma.get("smtp_host", "").strip() or (
            imap_host.replace("imap.", "smtp.", 1)
            if imap_host.startswith("imap.") else imap_host
        )
        smtp_port = int(self.ma.get("smtp_port") or 587)
        smtp_ssl  = bool(self.ma.get("smtp_ssl", False)) or smtp_port == 465
        return username, password, smtp_host, smtp_port, smtp_ssl

    def _smtp_connect(self):
        username, password, smtp_host, smtp_port, smtp_ssl = self._smtp_params()
        if smtp_ssl:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)
            server.ehlo()
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
            server.ehlo()
            server.starttls()
        server.login(username, password)
        return server, username, smtp_host, smtp_port

    def _send_smtp(self, msg, to: str) -> dict:
        username = self.ma.get("username", "").strip()
        _, _, smtp_host, smtp_port, _ = self._smtp_params()
        if not self.ma.get("host") or not username:
            return {"status": "error", "message": "IMAP host and username are required"}
        msg["From"] = self._from_header(username)
        try:
            server, username, smtp_host, smtp_port = self._smtp_connect()
            with server:
                server.sendmail(username, [to], msg.as_string())
            self._append_to_sent(msg)
            return {"status": "ok",
                    "message": f"Email sent to {to} via {smtp_host}:{smtp_port}"}
        except smtplib.SMTPAuthenticationError as e:
            return {"status": "error", "message": f"SMTP auth failed: {e}"}
        except Exception as e:
            return {"status": "error",
                    "message": f"SMTP error ({smtp_host}:{smtp_port}): {e}"}

    # ── IMAP helpers (connect, find Sent, append) ─────────────────────────

    def _imap_connect(self):
        host     = self.ma.get("host", "").strip()
        port     = int(self.ma.get("port") or 993)
        username = self.ma.get("username", "").strip()
        password = self.ma.get("password", "")
        use_ssl  = self.ma.get("ssl", True)
        if use_ssl:
            conn = imaplib.IMAP4_SSL(host, port,
                   ssl_context=ssl.create_default_context())
        else:
            conn = imaplib.IMAP4(host, port)
        conn.login(username, password)
        return conn

    def _find_sent_folder(self, conn) -> str | None:
        """Find Sent folder by \\Sent flag, then fallback to common names."""
        try:
            _, folder_list = conn.list()
        except Exception:
            return None
        parsed = []
        for item in (folder_list or []):
            if item is None:
                continue
            raw = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
            flags_m = re.match(r"\(([^)]*)\)", raw)
            flags   = flags_m.group(1).lower() if flags_m else ""
            name    = re.sub(r"^\(.*?\)\s+(?:\"[^\"]*\"|NIL)\s*", "", raw).strip().strip('"')
            parsed.append((flags, name))
        # Pass 1: \Sent flag
        for flags, name in parsed:
            if "\\sent" in flags or "sent" in flags.split():
                return name
        # Pass 2: common names
        names = {n for _, n in parsed}
        for candidate in ["Sent", "Sent Items", "Sent Messages",
                          "[Gmail]/Sent Mail", "Sent Mail", "INBOX.Sent"]:
            if candidate in names:
                return candidate
        return None

    def _append_to_sent(self, msg) -> None:
        """Append sent message to IMAP Sent folder. Best-effort — never raises."""
        try:
            conn   = self._imap_connect()
            folder = self._find_sent_folder(conn)
            if not folder:
                conn.logout()
                return
            quoted = '"' + folder.replace('"', '\\"') + '"'
            conn.append(quoted, "\\Seen",
                        imaplib.Time2Internaldate(time.time()),
                        msg.as_bytes())
            conn.logout()
        except Exception as e:
            print(f"[mail_sender] append_to_sent failed (non-fatal): {e}", flush=True)

    def _ping_imap(self) -> dict:
        host     = self.ma.get("host", "").strip()
        port     = int(self.ma.get("port") or 993)
        username = self.ma.get("username", "").strip()
        password = self.ma.get("password", "")
        use_ssl  = self.ma.get("ssl", True)
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

    def _gmail_auth_b64(self) -> str:
        token    = self.ma.get("access_token", "").strip() or self._get_access_token()
        auth_str = f"user={self.email}\x01auth=Bearer {token}\x01\x01"
        return base64.b64encode(auth_str.encode()).decode()

    def _send_gmail(self, msg, to: str) -> dict:
        if not self.ma.get("refresh_token"):
            return {"status": "error", "message": "refresh_token is required"}
        msg["From"] = self._from_header(self.email)
        try:
            auth_b64 = self._gmail_auth_b64()
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.docmd("AUTH", "XOAUTH2 " + auth_b64)
                server.sendmail(self.email, [to], msg.as_string())
            # Gmail saves to Sent automatically — no APPEND needed
            return {"status": "ok", "message": f"Email sent to {to} via Gmail"}
        except smtplib.SMTPAuthenticationError as e:
            return {"status": "error", "message": f"Gmail auth failed: {e}"}
        except Exception as e:
            return {"status": "error", "message": f"Gmail SMTP error: {e}"}

    def _ping_gmail(self) -> dict:
        if not self.ma.get("refresh_token"):
            return {"status": "error",
                    "message": "client_id, client_secret and refresh_token are required"}
        try:
            self._get_access_token()
            return {"status": "ok", "message": "OAuth2 token refreshed successfully"}
        except Exception as e:
            return {"status": "error", "message": f"Google OAuth2 error: {e}"}
