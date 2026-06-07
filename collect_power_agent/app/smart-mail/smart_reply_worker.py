# app/smart_mail/smart_reply_worker.py
"""
Polling loop for the reply-detection pipeline -- mirrors the structure of
smart_campaign_worker.py.

Each cycle:
  1. For every mailbox alias in REPLY_ACCOUNTS, poll its INBOX for unread
     messages via app.mail_reader.read_unread_emails(alias) (which dedupes
     into the `inbox_messages` collection).
  2. Run smart_reply_matcher.match_new_replies() once to link any newly
     stored messages to campaign sends and flip matched contacts to
     status="replied".
  3. Sleep REPLY_POLL_SECONDS and repeat.

Each mailbox is polled in its own try/except so one misconfigured or
unreachable account (e.g. an IMAP host typo) can never stop the others from
being checked, and never crashes the loop itself -- exactly the isolation
guarantee smart_campaign_worker.py already provides for campaign sends.
"""
import time

from config_mail import REPLY_ACCOUNTS, REPLY_POLL_SECONDS

from app.mail_reader import read_unread_emails
from smart_reply_matcher import match_new_replies


def poll_accounts():
    """Poll every configured mailbox once; never raises."""
    for alias in REPLY_ACCOUNTS:
        try:
            read_unread_emails(alias)
        except Exception as ex:
            print(f"[reply_worker] {alias}: poll failed -- {ex}")


def run_worker():
    print("Smart reply worker started")
    print(f"[reply_worker] accounts={REPLY_ACCOUNTS}  poll_seconds={REPLY_POLL_SECONDS}")

    while True:
        try:
            poll_accounts()

            summary = match_new_replies()
            if summary["checked"]:
                print(f"[reply_worker] matcher: {summary}")

        except Exception as ex:
            # Catch-all for the whole cycle -- a bug here must never end the
            # loop; the worker is meant to run unattended indefinitely.
            print(f"[reply_worker] cycle error: {ex}")

        time.sleep(REPLY_POLL_SECONDS)


if __name__ == "__main__":
    run_worker()
