"""Local wrapper for the central smart-mail sender.

Selection and sent-confirmation rules live in
functions-smartmail/outreach_mail_select.py. This module only preserves the
local script entrypoint by delegating to the current sender implementation.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_FUNCTIONS_SMARTMAIL = Path(__file__).resolve().parents[2] / "functions-smartmail"
if str(_FUNCTIONS_SMARTMAIL) not in sys.path:
    sys.path.insert(0, str(_FUNCTIONS_SMARTMAIL))

from smart_mail.smart_campaign_sender import (  # noqa: E402
    bounce_rate_tripped,
    compute_send_budget,
    send_outreach,
)


def send_campaign(_campaign_id: str | None = None) -> dict:
    """Run one intro pass and one follow-up pass through the central sender."""
    intro = send_outreach("intro")
    followup = send_outreach("followup")
    return {"intro": intro, "followup": followup}


def main() -> None:
    mode = os.getenv("OUTREACH_MODE", "both").strip().lower()
    if mode in {"intro", "followup"}:
        print(send_outreach(mode))
    else:
        print(send_campaign())


if __name__ == "__main__":
    main()
