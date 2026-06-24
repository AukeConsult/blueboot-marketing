"""Dedup tests for the sent-folder sync — ensures a CRM-sent mail logged at
send time is NOT re-added by run_sent_sync (cross-source duplicate guard).

Loads inbound_read_lib in isolation (its top-level imports are stdlib only).
    python functions-crm/smart_mail/test_inbound_read_lib.py
"""
import importlib.util
import os
import unittest

DIR = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "inbound_read_lib_under_test", os.path.join(DIR, "inbound_read_lib.py"))
ir = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ir)


class TestEmailIdDedup(unittest.TestCase):
    MID = "<deadbeef1234@blueboot.ai>"

    def test_normalizer_strips_brackets(self):
        self.assertEqual(ir.email_id_from_message_id(self.MID), "deadbeef1234@blueboot.ai")

    def test_normalizer_matches_msg_key(self):
        # the key stored at send time must equal what run_sent_sync computes
        # for the same mail found in the SENT folder (any folder/uid)
        self.assertEqual(
            ir.email_id_from_message_id(self.MID),
            ir._msg_key(self.MID, "INBOX.Sent", "42"))

    def test_send_time_entry_blocks_sent_folder_dup(self):
        # send-time MAIL_SENT / EMAIL_OUT entry carrying email_id
        for typ in ("MAIL_SENT", "EMAIL_OUT"):
            history = [{"type": typ, "email_id": ir.email_id_from_message_id(self.MID)}]
            existing = ir._history_email_ids(history)
            sent_key = ir._msg_key(self.MID, "INBOX.Sent", "99")
            self.assertIn(sent_key, existing,
                          f"{typ} with email_id should block the sent-folder duplicate")

    def test_entry_without_email_id_does_not_dedup(self):
        # regression guard: this is exactly the bug — no email_id => no match
        history = [{"type": "EMAIL_OUT", "message_id": self.MID}]  # no email_id field
        self.assertEqual(ir._history_email_ids(history), set())

    def test_blank_message_id_falls_back_to_folder_uid(self):
        self.assertEqual(ir._msg_key("", "INBOX.Sent", "7"), "INBOX.Sent__7")


import email as _email_lib


class TestCrmOriginTag(unittest.TestCase):
    """The X-Blueboot-Sent header lets run_sent_sync skip CRM-sent mail so only
    externally-sent mail (case 3) is logged from the SENT folder."""

    @staticmethod
    def _origin(raw: bytes) -> str:
        parsed = _email_lib.message_from_bytes(raw)
        return (parsed.get("X-Blueboot-Sent", "") or "").strip().lower()

    def test_crm_tagged_mail_is_recognised(self):
        raw = (b"From: sales@blueboot.ai\r\nTo: x@y.com\r\n"
               b"Message-ID: <a@blueboot.ai>\r\nX-Blueboot-Sent: crm\r\n"
               b"Subject: hi\r\n\r\nbody\r\n")
        self.assertEqual(self._origin(raw), "crm")   # run_sent_sync -> skip

    def test_external_mail_is_not_tagged(self):
        raw = (b"From: rep@external.com\r\nTo: x@y.com\r\n"
               b"Message-ID: <b@external.com>\r\nSubject: hi\r\n\r\nbody\r\n")
        self.assertEqual(self._origin(raw), "")       # run_sent_sync -> log it


if __name__ == "__main__":
    unittest.main(verbosity=2)
