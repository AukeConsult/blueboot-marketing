"""Centralised environment configuration.

Loads .env once at import time. All scripts should import `cfg` from here
instead of calling os.getenv() directly.

Usage:
    print(cfg.OPENAI_API_KEY)
    print(cfg.MAX_RESULTS)
"""
from __future__ import annotations
from pathlib import Path

# Load .env from project root (parent of app/)
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

import os


class _Config:
    # ── Firebase ───────────────────────────────────────────────────────────
    FIREBASE_KEY_JSON:     str         = os.getenv("FIREBASE_KEY_JSON", "").strip()
    FIREBASE_CREDENTIALS:  str         = os.getenv("FIREBASE_CREDENTIALS", "")
    FIRESTORE_COLLECTION:  str         = os.getenv("FIRESTORE_COLLECTION", "") or "leads"

    # ── APIs ───────────────────────────────────────────────────────────────
    OPENAI_API_KEY:  str = os.getenv("OPENAI_API_KEY", "")
    BRAVE_API_KEY:   str = os.getenv("BRAVE_API_KEY", "")
    GITHUB_TOKEN:    str = os.getenv("GITHUB_TOKEN", "")
    GOOGLE_API_KEY:  str = os.getenv("GOOGLE_API_KEY", "")
    GOOGLE_CSE_ID:   str = os.getenv("GOOGLE_CSE_ID", "")

    # ── lead_agent tuning ──────────────────────────────────────────────────
    MAX_RESULTS:   int         = int(os.getenv("MAX_RESULTS",   "200"))
    MIN_SCORE:     int         = int(os.getenv("MIN_SCORE",     "50"))
    MAX_PAGES:     int         = int(os.getenv("MAX_PAGES",     "6"))
    MAX_COUNTRY:   int | None  = int(os.getenv("MAX_COUNTRY",   "1000")) or None
    GIVE_UP_AFTER: int         = int(os.getenv("GIVE_UP_AFTER", "15"))
    CRAWL_DELAY:   float       = float(os.getenv("CRAWL_DELAY", "1.0"))
    CRAWL_WORKERS: int         = int(os.getenv("CRAWL_WORKERS", "20"))
    LIMIT_PER_HOST: int        = int(os.getenv("LIMIT_PER_HOST", "3"))

    # ── SMTP / mail ────────────────────────────────────────────────────────
    SMTP_HOST:     str = os.getenv("SMTP_HOST",     "smtp.gmail.com")
    SMTP_PORT:     int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER:     str = os.getenv("SMTP_USER",     "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    MAIL_FROM:     str = os.getenv("MAIL_FROM",     "")
    MAIL_REPLY_TO: str = os.getenv("MAIL_REPLY_TO", "")
    GMAIL_SENDER:  str = os.getenv("GMAIL_SENDER",  "")

    # ── Misc ───────────────────────────────────────────────────────────────
    CAMPAIGNS_DIR:   str = os.getenv("CAMPAIGNS_DIR",   "")
    CAMPAIGN_LABEL:  str = os.getenv("CAMPAIGN_LABEL",  "")
    QUERIES_FILE:    str = os.getenv("QUERIES_FILE",     "")

    # ── OpenAI model ──────────────────────────────────────────────────────
    OPENAI_MODEL:    str = os.getenv("OPENAI_MODEL",    "gpt-4.1-mini")

    def validate_firebase(self) -> None:
        """Raise RuntimeError if no Firebase credentials are configured."""
        if not self.FIREBASE_KEY_JSON and not self.FIREBASE_CREDENTIALS:
            p = Path(__file__).parent.parent.parent / "config" / "serviceAccountKey.json"
            if not p.exists():
                raise RuntimeError(
                    "[config] No Firebase credentials found.\n"
                    "Set FIREBASE_KEY_JSON or FIREBASE_CREDENTIALS in .env"
                )

    def validate_openai(self) -> None:
        """Raise RuntimeError if OPENAI_API_KEY is not set."""
        if not self.OPENAI_API_KEY:
            raise RuntimeError(
                "[config] OPENAI_API_KEY is not set.\n"
                "Add OPENAI_API_KEY=sk-... to .env"
            )

    def validate_brave(self) -> None:
        """Raise RuntimeError if BRAVE_API_KEY is not set."""
        if not self.BRAVE_API_KEY:
            raise RuntimeError(
                "[config] BRAVE_API_KEY is not set.\n"
                "Add BRAVE_API_KEY=... to .env"
            )

    def __repr__(self) -> str:
        def mask(v: str) -> str:
            return v[:4] + "***" if len(v) > 4 else ("***" if v else "(not set)")
        return (
            f"Config(\n"
            f"  FIREBASE_KEY_JSON = {mask(self.FIREBASE_KEY_JSON)}\n"
            f"  OPENAI_API_KEY    = {mask(self.OPENAI_API_KEY)}\n"
            f"  BRAVE_API_KEY     = {mask(self.BRAVE_API_KEY)}\n"
            f"  GITHUB_TOKEN      = {mask(self.GITHUB_TOKEN)}\n"
            f"  MAX_RESULTS={self.MAX_RESULTS}  MIN_SCORE={self.MIN_SCORE}  "
            f"MAX_COUNTRY={self.MAX_COUNTRY}  CRAWL_WORKERS={self.CRAWL_WORKERS}\n"
            f"  SMTP_HOST={self.SMTP_HOST}  SMTP_USER={self.SMTP_USER}\n"
            f")"
        )


# Singleton — import this everywhere
cfg = _Config()