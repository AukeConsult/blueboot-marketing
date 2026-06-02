"""
send_mail.py — Outreach email generation and sending for BlueBoot Lead Agent.

Environment variables (add to .env):
    SMTP_HOST       e.g. smtp.gmail.com
    SMTP_PORT       e.g. 587
    SMTP_USER       your sending address, e.g. leifauke@gmail.com
    SMTP_PASSWORD   app password (not your login password)
    MAIL_FROM       display name + address, e.g. "Leif Auke <leifauke@gmail.com>"
    MAIL_REPLY_TO   optional reply-to address
"""

from __future__ import annotations

import os
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dataclasses import dataclass
from functions.config import cfg


# ---------------------------------------------------------------------------
# Outreach template
# ---------------------------------------------------------------------------

def make_outreach(company: str, domain: str, cats: set[str], lead_angle: str, country_name: str) -> tuple[str, str]:
    """Return (subject, plain-text email body) for a lead."""
    subject = f"AI search add-on for {company}'s website customers"
    body = f"""Hi {company},

I noticed you work with websites and digital customer communication in {country_name}. We build BlueSearch, an AI-powered search layer that can be added to existing websites so visitors can ask questions and get answers with source links.

Why this may fit your customers:
- easy add-on for existing sites
- useful for content-heavy websites, WordPress/WooCommerce, public information and documentation
- can be sold as a recurring managed service

Suggested angle for {domain}: {lead_angle}

Would it be useful if I sent a short demo showing how it works on a real website?

Best regards,
Leif Auke
BlueBoot R&D AS
https://blueboot.ai
"""
    return subject, body


# ---------------------------------------------------------------------------
# SMTP config
# ---------------------------------------------------------------------------

@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    mail_from: str
    reply_to: str = ""

    @staticmethod
    def from_env() -> "SmtpConfig":
        missing = [k for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD") if not os.getenv(k)]
        if missing:
            raise EnvironmentError(f"Missing required env vars for email: {', '.join(missing)}")
        return SmtpConfig(
            host=cfg.SMTP_HOST,
            port=cfg.SMTP_PORT,
            user=cfg.SMTP_USER,
            password=cfg.SMTP_PASSWORD,
            mail_from=os.getenv("MAIL_FROM", cfg.SMTP_USER),
            reply_to=cfg.MAIL_REPLY_TO,
        )


# ---------------------------------------------------------------------------
# Single email send
# ---------------------------------------------------------------------------

def send_mail(to: str, subject: str, body: str, cfg: SmtpConfig) -> None:
    """Send a plain-text email via SMTP with STARTTLS."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg.mail_from
    msg["To"] = to
    if cfg.reply_to:
        msg["Reply-To"] = cfg.reply_to
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP(cfg.host, cfg.port, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.login(cfg.user, cfg.password)
        server.sendmail(cfg.mail_from, [to], msg.as_string())


# ---------------------------------------------------------------------------
# Batch outreach over a list of Lead objects
# ---------------------------------------------------------------------------

def send_outreach_emails(leads: list, dry_run: bool = True, delay: float = 3.0) -> None:
    """
    Send outreach emails to all leads that have an email address and a
    non-empty outreach_subject / outreach_email.

    Args:
        leads:    List of Lead dataclass instances (from lead_agent.py).
        dry_run:  If True (default), prints what would be sent without actually sending.
        delay:    Seconds to wait between sends to avoid rate-limiting.
    """
    if not dry_run:
        cfg = SmtpConfig.from_env()

    sent, skipped = 0, 0
    for lead in leads:
        recipient = lead.emails.split(",")[0].strip() if lead.emails else ""
        if not recipient or not lead.outreach_subject or not lead.outreach_email:
            skipped += 1
            continue

        if dry_run:
            print(f"[DRY RUN] Would send to {recipient} — {lead.outreach_subject}")
        else:
            try:
                send_mail(recipient, lead.outreach_subject, lead.outreach_email, cfg)
                print(f"[SENT] {recipient} — {lead.outreach_subject}")
                sent += 1
                time.sleep(delay)
            except Exception as exc:
                print(f"[ERROR] {recipient}: {exc}")
                skipped += 1

    print(f"\nDone. Sent: {sent}, Skipped/errors: {skipped}")
