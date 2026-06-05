"""
crm/mail_sender.py -- Isolated mail sending class.

All outbound email — test mails, campaign sends, pings — goes through
MailSender so fixes (display name, CSS stripping, headers, SSL/STARTTLS
selection) apply everywhere automatically.

Usage:
    from crm.mail_sender import MailSender

    sender = MailSender(mail_account_dict)
    result = sender.send(to="x@y.com", subject="Hi", body_plain="Hello")
    # result = {"status": "ok"|"error", "message": "..."}

    ok = sender.ping()
    # ok = {"status": "ok"|"error", "message": "..."}
"""
from __future__ import annotations

import re
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
        """
        ma: mail account document from settings/mail_accounts/accounts/{email}
        Expected keys: account_type, email, display_name,
                       host, port, username, password, ssl (IMAP)
                       smtp_host, smtp_port, smtp_ssl          (SMTP override)
                       client_id, client_secret, refresh_token, access_token (Gmail)
        """
        self.ma = ma
        self.account_type = ma.get("account_type", "imap")
        self.email        = ma.get("email", "").strip().lower()
        self.display_name = ma.get("display_name", "").strip()

    # ── Public API ────────────────────────────────────────────────────────

    def send(self, *, to: str, subject: str,
             body_plain: str = "", body_html: str = "") -> dict:
        """Send an email. Returns {"status": "ok"|"error", "message": "..."}."""
        try:
            msg = self._build_message(to=to, subject=subject,
                                      body_plain=body_plain, body_html=body_html)
            if self.account_type == "imap":
                return self._send_smtp(msg, to)
            elif self.account_type == "gmail":
                return self._send_gmail(msg, to)
            else:
                return {"status": "error",
                        "message": f"Unknown account type: {self.account_type}"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def ping(self) -> dict:
        """Test the connection without sending anything.
        Returns {"status": "ok"|"error", "message": "..."}."""
        try:
            if self.account_type == "imap":
                return self._ping_imap()
            elif self.account_type == "gmail":
                return self._ping_gmail()
            else:
                return {"status": "error",
                        "message": f"Unknown account type: {self.account_type}"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    # ── Message building ──────────────────────────────────────────────────

    def _from_header(self, addr: str) -> str:
        return f"{self.display_name} <{addr}>" if self.display_name else addr

    @staticmethod
    def _inline_css(html: str) -> str:
        """Inline all <style> block rules as element-level style= attributes.

        MailChannels (and Gmail/Outlook) strip embedded <style> blocks, so CSS
        must be inlined before sending. premailer handles all selector types and
        leaves the resulting HTML without any <style> blocks.
        """
        try:
            from premailer import transform
            return transform(
                html,
                remove_classes=False,
                strip_important=False,
                allow_network=False,
            )
        except Exception:
            # Fallback: just strip the style blocks so we still send something
            return re.sub(r"<style[^>]*>.*?</style>", "", html,
                          flags=re.DOTALL | re.IGNORECASE)

    @staticmethod
    def _html_to_plain(html: str) -> str:
        """Convert HTML to readable plain text."""
        text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
        text = re.sub(r"<p[^>]*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _build_message(self, *, to: str, subject: str,
                       body_plain: str, body_html: str) -> MIMEMultipart | MIMEText:
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
            return {"status": "ok",
                    "message": f"Email sent to {to} via {smtp_host}:{smtp_port}"}
        except smtplib.SMTPAuthenticationError as e:
            return {"status": "error", "message": f"SMTP auth failed: {e}"}
        except Exception as e:
            return {"status": "error",
                    "message": f"SMTP error ({smtp_host}:{smtp_port}): {e}"}

    def _ping_imap(self) -> dict:
        host     = self.ma.get("host", "").strip()
        port     = int(self.ma.get("port") or 993)
        username = self.ma.get("username", "").strip()
        password = self.ma.get("password", "")
        use_ssl  = self.ma.get("ssl", True)
        if not host or not username:
            return {"status": "error", "message": "IMAP host and username are required"}
        try:
            if use_ssl:
                conn = imaplib.IMAP4_SSL(host, port,
                       ssl_context=ssl.create_default_context())
            else:
                conn = imaplib.IMAP4(host, port)
            conn.login(username, password)
            conn.logout()
            return {"status": "ok", "message": f"Connected to {host}:{port} as {username}"}
        except imaplib.IMAP4.error as e:
            return {"status": "error", "message": f"IMAP auth failed: {e}"}
        except Exception as e:
            return {"status": "error", "message": f"Cannot reach {host}:{port} — {e}"}

    # ── Gmail OAuth2 ──────────────────────────────────────────────────────

    def _get_access_token(self) -> str:
        """Refresh and return a Gmail access token."""
        import urllib.request, json as _json
        client_id     = self.ma.get("client_id", "").strip()
        client_secret = self.ma.get("client_secret", "").strip()
        refresh_token = self.ma.get("refresh_token", "").strip()
        import urllib.parse as _up
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
        token = self.ma.get("access_token", "").strip() or self._get_access_token()
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
            token = self._get_access_token()
            return {"status": "ok", "message": "OAuth2 token refreshed successfully"}
        except Exception as e:
            return {"status": "error", "message": f"Google OAuth2 error: {e}"}
