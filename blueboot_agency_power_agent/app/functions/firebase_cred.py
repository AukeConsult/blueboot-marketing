"""Shared Firebase credential loader.

Priority:
  1. FIREBASE_KEY_JSON env var — inline JSON string (Option B)
  2. FIREBASE_CREDENTIALS env var — path to a JSON key file
  3. config/serviceAccountKey.json — hardcoded fallback
"""
import json
import os
from pathlib import Path


def get_firebase_cred():
    """Return a firebase_admin.credentials.Certificate or raise RuntimeError."""
    import firebase_admin.credentials as fb_creds

    # Option B: inline JSON in env
    key_json = os.getenv("FIREBASE_KEY_JSON", "").strip()
    if key_json:
        try:
            key_dict = json.loads(key_json)
            return fb_creds.Certificate(key_dict)
        except Exception as e:
            raise RuntimeError(
                f"[firebase] FIREBASE_KEY_JSON is set but could not be parsed: {e}\n"
                "Check that the value is valid single-line JSON."
            )

    # Option A: path to JSON file
    creds_path = os.getenv("FIREBASE_CREDENTIALS", "")
    if not creds_path:
        # Absolute fallback
        creds_path = str(Path(__file__).parent.parent.parent / "config" / "serviceAccountKey.json")

    if Path(creds_path).exists():
        return fb_creds.Certificate(creds_path)

    raise RuntimeError(
        "[firebase] No Firebase credentials found.\n"
        "Set FIREBASE_KEY_JSON (inline JSON) or FIREBASE_CREDENTIALS (path to key file) in .env"
    )
