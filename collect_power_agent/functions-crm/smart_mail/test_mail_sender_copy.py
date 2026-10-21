"""Copy of a sent mail: manual sends -> Sent, automatic outreach -> Processed_Sent, Gmail -> nothing."""
import unittest
from email.mime.text import MIMEText

from smart_mail.mail_sender import MailSender


class FakeImap:
    def __init__(self, folders):
        self.folders, self.created, self.subscribed, self.appended = folders, [], [], []

    def list(self):
        return "OK", [f'({fl}) "." "{n}"'.encode() for n, fl in self.folders]

    def create(self, name):
        self.created.append(name.strip('"')); return "OK", [b"done"]

    def subscribe(self, name):
        self.subscribed.append(name.strip('"')); return "OK", [b""]

    def append(self, folder, flags, date, data):
        self.appended.append(folder.strip('"')); return "OK", [b"done"]


def _sender(account_type="imap", folders=()):
    s = MailSender({"email": "a@b.no", "account_type": account_type})
    s._imap_conn = FakeImap(list(folders))
    return s, s._imap_conn


class SaveCopy(unittest.TestCase):
    msg = MIMEText("hi")

    def test_manual_goes_to_existing_sent_folder(self):
        s, c = _sender(folders=[("INBOX", ""), ("INBOX.Sent", "\\HasNoChildren \\Sent")])
        self.assertEqual(s._save_copy(self.msg, "sent"), {"copy_folder": "INBOX.Sent"})
        self.assertEqual(c.appended, ["INBOX.Sent"])
        self.assertEqual(c.created, [])

    def test_auto_goes_to_processed_created_under_inbox(self):
        s, c = _sender(folders=[("INBOX", ""), ("INBOX.Sent", "")])
        self.assertEqual(s._save_copy(self.msg, "processed"), {"copy_folder": "INBOX.Processed_Sent"})
        self.assertEqual(c.created, ["INBOX.Processed_Sent"])
        self.assertEqual(c.subscribed, ["INBOX.Processed_Sent"])
        self.assertEqual(c.appended, ["INBOX.Processed_Sent"])

    def test_existing_processed_is_reused(self):
        s, c = _sender(folders=[("INBOX", ""), ("Processed_Sent", "")])
        self.assertEqual(s._save_copy(self.msg, "processed"), {"copy_folder": "Processed_Sent"})
        self.assertEqual(c.created, [])

    def test_missing_sent_is_created(self):
        s, c = _sender(folders=[("INBOX", "")])
        self.assertEqual(s._save_copy(self.msg, "sent"), {"copy_folder": "Sent"})
        self.assertEqual(c.created, ["Sent"])

    def test_none_and_gmail_save_nothing(self):
        s, c = _sender(folders=[("INBOX", "")])
        self.assertEqual(s._save_copy(self.msg, "none"), {})
        g, gc = _sender("gmail", [("INBOX", "")])
        self.assertEqual(g._save_copy(self.msg, "sent"), {})
        self.assertEqual(c.appended + gc.appended, [])

    def test_failure_never_raises(self):
        s, c = _sender(folders=[("INBOX", "")])
        c.append = lambda *a, **k: ("NO", [b"quota"])
        r = s._save_copy(self.msg, "sent")
        self.assertIn("copy_error", r)

    def test_defaults(self):
        import inspect
        self.assertEqual(inspect.signature(MailSender.send).parameters["save_copy"].default, "sent")
        self.assertEqual(inspect.signature(MailSender.send_open).parameters["save_copy"].default, "processed")


if __name__ == "__main__":
    unittest.main()
