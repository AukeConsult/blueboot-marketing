"""Offline unit tests for reply_matcher's matching + update rules.

Runs with NO IMAP and NO Firestore — every external dependency is stubbed,
so it executes anywhere (CI, sandbox, dev box) in well under a second:

    python functions-crm/smart_mail/test_reply_matcher.py
"""
import os
import sys
import types
import unittest

DIR = os.path.dirname(os.path.abspath(__file__))

# ── stub external deps BEFORE importing the module under test ────────────────

# google.cloud.firestore_v1 (ArrayUnion) + .base_query (FieldFilter)
g  = types.ModuleType("google");                 g.__path__  = []
gc = types.ModuleType("google.cloud");           gc.__path__ = []
fv = types.ModuleType("google.cloud.firestore_v1")
bq = types.ModuleType("google.cloud.firestore_v1.base_query")

class FieldFilter:
    def __init__(self, field, op, value):
        self.field, self.op, self.value = field, op, value

class ArrayUnion:
    def __init__(self, values): self.values = values

fv.ArrayUnion = ArrayUnion
bq.FieldFilter = FieldFilter
sys.modules.update({
    "google": g, "google.cloud": gc,
    "google.cloud.firestore_v1": fv,
    "google.cloud.firestore_v1.base_query": bq,
})

# the smart_mail package + the two sibling modules it imports at load time
pkg = types.ModuleType("smart_mail"); pkg.__path__ = [DIR]
sys.modules["smart_mail"] = pkg
fc = types.ModuleType("smart_mail.firestore_client"); fc.get_firestore = lambda: None
sys.modules["smart_mail.firestore_client"] = fc
osm = types.ModuleType("smart_mail.outreach_stats")
osm.refresh_campaign_stats = lambda cid: None
sys.modules["smart_mail.outreach_stats"] = osm

import importlib
rm = importlib.import_module("smart_mail.reply_matcher")


# ── minimal fake Firestore ────────────────────────────────────────────────────

class FakeDoc:
    def __init__(self, doc_id, data, campaign_id):
        self.id = doc_id
        self._data = data
        self.reference = types.SimpleNamespace(
            parent=types.SimpleNamespace(
                parent=types.SimpleNamespace(id=campaign_id)))
    def to_dict(self): return dict(self._data)

class FakeQuery:
    def __init__(self, rows): self._rows = rows
    def where(self, filter=None):
        f = filter
        return FakeQuery([r for r in self._rows
                          if r._data.get(f.field) == f.value or
                             (f.field == "doc_id" and r.id == f.value)])
    def limit(self, n): return FakeQuery(self._rows[:n])
    def stream(self): return list(self._rows)

class FakeDocRef:
    def __init__(self, store, key):
        self.store, self.key = store, key
    def get(self):
        data = self.store.get(self.key)
        return types.SimpleNamespace(
            exists=data is not None,
            to_dict=lambda: dict(data) if data else {})
    def update(self, payload):
        self.store.setdefault(self.key, {})
        # expand ArrayUnion the way Firestore would
        for k, v in payload.items():
            if isinstance(v, ArrayUnion):
                self.store[self.key].setdefault(k, [])
                self.store[self.key][k].extend(v.values)
            else:
                self.store[self.key][k] = v

class FakeCollection:
    def __init__(self, db, name): self.db, self.name = db, name
    def document(self, doc_id):
        self.db.collections_accessed.add(self.name)
        return _FakeCampaignDoc(self.db, self.name, doc_id)

class _FakeCampaignDoc:
    def __init__(self, db, coll, doc_id):
        self.db, self.coll, self.doc_id = db, coll, doc_id
    def collection(self, sub):
        return _FakeSubColl(self.db, self.doc_id, sub)

class _FakeSubColl:
    def __init__(self, db, campaign_id, sub):
        self.db, self.campaign_id, self.sub = db, campaign_id, sub
    def document(self, doc_id):
        key = (self.campaign_id, self.sub, doc_id)
        return FakeDocRef(self.db.docstore, key)

class FakeDB:
    """Holds collection-group rows and a key/value docstore for writes."""
    def __init__(self, group_rows=None, docstore=None):
        self.group_rows = group_rows or []
        self.docstore = docstore or {}
        self.collections_accessed = set()
    def collection(self, name):
        self.collections_accessed.add(name)
        return FakeCollection(self, name)
    def collection_group(self, name):
        self.collections_accessed.add(name)
        return FakeQuery(self.group_rows)


# ── tests ─────────────────────────────────────────────────────────────────────

class TestFindContact(unittest.TestCase):
    def test_match_by_doc_id(self):
        db = FakeDB(group_rows=[
            FakeDoc("foo_bar_com", {"doc_id": "foo_bar_com", "lead_id": "L1"}, "CAMP1")])
        msg = {"from_email": "Foo <foo@bar.com>"}
        cid, camp, lead, via = rm._find_contact(db, msg)
        self.assertEqual((camp, lead, via), ("CAMP1", "L1", "doc_id"))

    def test_match_by_email_field_when_no_doc_id(self):
        db = FakeDB(group_rows=[
            FakeDoc("xyz", {"email": "foo@bar.com", "lead_id": "L2"}, "CAMP2")])
        msg = {"from_email": "foo@bar.com"}
        cid, camp, lead, via = rm._find_contact(db, msg)
        self.assertEqual((camp, lead, via), ("CAMP2", "L2", "email_field"))

    def test_campaign_filter_excludes(self):
        db = FakeDB(group_rows=[
            FakeDoc("foo_bar_com", {"doc_id": "foo_bar_com"}, "OTHER")])
        msg = {"from_email": "foo@bar.com"}
        res = rm._find_contact(db, msg, campaign_filter={"ONLY_THIS"})
        self.assertEqual(res, (None, None, None, None))

    def test_no_match_returns_none(self):
        db = FakeDB(group_rows=[])
        res = rm._find_contact(db, {"from_email": "nobody@nowhere.com"})
        self.assertEqual(res, (None, None, None, None))


class TestApplyReply(unittest.TestCase):
    def test_pending_to_active_and_no_email_contacts_write(self):
        db = FakeDB(docstore={("CAMP1", "campaign_contacts", "c1"): {"status": "pending"}})
        msg = {"message_id": "<m1>", "subject": "Re: hi", "from_email": "a@b.com",
               "body_text": "hello", "received_at": "2026-06-23T00:00:00+00:00"}
        out = rm._apply_actions(db, "c1", "CAMP1", None, None, msg, "doc_id")
        cc = db.docstore[("CAMP1", "campaign_contacts", "c1")]
        self.assertEqual(out, "updated")
        self.assertEqual(cc["status"], "active")
        self.assertEqual(cc["followup_status"], "replied")
        self.assertEqual(cc["comment_history"][0]["type"], "EMAIL_IN")
        self.assertNotIn("email_contacts", db.collections_accessed)

    def test_idempotent_skip(self):
        existing = {"status": "active",
                    "comment_history": [{"type": "EMAIL_IN", "message_id": "<dup>"}]}
        db = FakeDB(docstore={("CAMP1", "campaign_contacts", "c1"): existing})
        msg = {"message_id": "<dup>", "subject": "x", "from_email": "a@b.com"}
        out = rm._apply_actions(db, "c1", "CAMP1", None, None, msg, "doc_id")
        self.assertEqual(out, "already_handled")


class TestApplyBounce(unittest.TestCase):
    def test_pending_is_excluded(self):
        db = FakeDB(docstore={("CAMP1", "campaign_contacts", "c1"): {"status": "pending"}})
        msg = {"message_id": "<b1>", "subject": "Undeliverable",
               "received_at": "2026-06-23T00:00:00+00:00"}
        rm._apply_bounce_actions(db, "c1", "CAMP1", None, None, msg)
        cc = db.docstore[("CAMP1", "campaign_contacts", "c1")]
        self.assertEqual(cc["status"], "excluded")
        self.assertTrue(cc["bounce_detected"])

    def test_active_is_left_as_is(self):
        db = FakeDB(docstore={("CAMP1", "campaign_contacts", "c1"): {"status": "active"}})
        msg = {"message_id": "<b2>", "subject": "Undeliverable",
               "received_at": "2026-06-23T00:00:00+00:00"}
        rm._apply_bounce_actions(db, "c1", "CAMP1", None, None, msg)
        cc = db.docstore[("CAMP1", "campaign_contacts", "c1")]
        self.assertEqual(cc["status"], "active")                  # unchanged
        self.assertNotIn("bounce_detected", cc)                   # no exclusion fields
        self.assertEqual(cc["comment_history"][0]["type"], "BOUNCE")  # but logged
        self.assertNotIn("email_contacts", db.collections_accessed)



# ── bounce-recipient extraction (the mailer-daemon NO MATCH bug) ──────────────

import email as _emaillib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.message import EmailMessage


def _make_dsn_bounce(original_to):
    """Build, from raw bytes (as IMAP delivers), a multipart/report bounce with
    a message/delivery-status part naming the failed recipient."""
    raw = (
        "From: Mail Delivery System <mailer-daemon@mailchannels.net>\r\n"
        "Subject: Undelivered Mail Returned to Sender\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="B"\r\n'
        "\r\n"
        "--B\r\n"
        "Content-Type: text/plain; charset=us-ascii\r\n\r\n"
        "This is the mail system at host mailchannels.net.\r\n"
        "\r\n"
        "--B\r\n"
        "Content-Type: message/delivery-status\r\n\r\n"
        "Reporting-MTA: dns; mailchannels.net\r\n"
        "\r\n"
        f"Final-Recipient: rfc822; {original_to}\r\n"
        "Action: failed\r\n"
        "Status: 5.1.1\r\n"
        "\r\n"
        "--B--\r\n"
    )
    return _emaillib.message_from_bytes(raw.encode())


def _make_rfc822_bounce(original_to):
    """Bounce that embeds the original message (no delivery-status part) —
    recipient must be recovered from the embedded message's To header."""
    outer = MIMEMultipart("report")
    outer["From"] = "postmaster@mailchannels.net"
    outer["Subject"] = "Undeliverable: your message"
    outer.attach(MIMEText("Delivery failed.\n", "plain"))
    orig = EmailMessage()
    orig["From"] = "sales@blueboot.ai"
    orig["To"] = original_to
    orig["Subject"] = "Our offer"
    orig.set_content("hi")
    rfc = MIMEMultipart()  # placeholder container
    rfc.set_type("message/rfc822")
    rfc.attach(orig)
    outer.attach(rfc)
    return _emaillib.message_from_bytes(outer.as_bytes())


class TestBounceRecipient(unittest.TestCase):
    def test_dsn_recipient_recovered(self):
        msg = _make_dsn_bounce("jane@acme.com")
        mtype, rcpt, _ = rm._classify_message(
            msg, "mailer-daemon@mailchannels.net", "Undelivered Mail Returned to Sender")
        self.assertEqual(mtype, "bounce")
        self.assertEqual(rcpt, "jane@acme.com")          # NOT the daemon address

    def test_embedded_original_to_recovered(self):
        msg = _make_rfc822_bounce("bob@acme.com")
        mtype, rcpt, _ = rm._classify_message(
            msg, "postmaster@mailchannels.net", "Undeliverable: your message")
        self.assertEqual(mtype, "bounce")
        self.assertEqual(rcpt, "bob@acme.com")

    def test_daemon_never_returned(self):
        # plain heuristic bounce with no recipient anywhere → None, not the daemon
        m = EmailMessage()
        m["From"] = "mailer-daemon@mailchannels.net"
        m["Subject"] = "failure notice"
        m.set_content("Sorry, we were unable to deliver your message.")
        mtype, rcpt, _ = rm._classify_message(
            m, "mailer-daemon@mailchannels.net", "failure notice")
        self.assertEqual(mtype, "bounce")
        self.assertIsNone(rcpt)

    def test_bounced_contact_matched_by_recovered_recipient(self):
        # end-to-end: recovered recipient → _find_bounced_contact → campaign_contacts
        db = FakeDB(group_rows=[
            FakeDoc("jane_acme_com", {"doc_id": "jane_acme_com", "lead_id": "L9"}, "CAMP9")])
        cid, camp, lead = rm._find_bounced_contact(
            db, {"original_recipient": "jane@acme.com"})
        self.assertEqual((camp, lead), ("CAMP9", "L9"))


def _make_exim_bounce(failed_to, original_from="cpanel@blueboot.ai"):
    """cPanel/Exim bounce: failed recipient is in the body 'following
    address(es) failed:' block; the embedded original is FROM the sender's
    own system mailbox. The extractor must return failed_to, not the sender."""
    raw = (
        "From: Mail Delivery System <mailer-daemon@cpweb02.misshosting.no>\r\n"
        "Subject: Mail delivery failed: returning message to sender\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="X"\r\n'
        "\r\n"
        "--X\r\n"
        "Content-Type: text/plain; charset=us-ascii\r\n\r\n"
        "This message was created automatically by mail delivery software.\r\n\r\n"
        "A message that you sent could not be delivered. The following "
        "address(es) failed:\r\n\r\n"
        f"  {failed_to}\r\n"
        "    SMTP error from remote mail server\r\n\r\n"
        "--X\r\n"
        "Content-Type: message/rfc822\r\n\r\n"
        f"From: {original_from}\r\n"
        f"To: {failed_to}\r\n"
        "Delivered-To: cpanel@blueboot.ai\r\n"
        "X-Original-To: cpanel@blueboot.ai\r\n"
        "Subject: System notice\r\n\r\n"
        "body\r\n"
        "--X--\r\n"
    )
    return _emaillib.message_from_bytes(raw.encode())


class TestEximBounce(unittest.TestCase):
    def test_failed_block_recipient_recovered(self):
        msg = _make_exim_bounce("realcontact@example.com")
        mtype, rcpt, _ = rm._classify_message(
            msg, "mailer-daemon@cpweb02.misshosting.no",
            "Mail delivery failed: returning message to sender")
        self.assertEqual(mtype, "bounce")
        self.assertEqual(rcpt, "realcontact@example.com")

    def test_sender_mailbox_not_picked(self):
        # the embedded Delivered-To / X-Original-To = cpanel@blueboot.ai
        # must never be returned
        msg = _make_exim_bounce("realcontact@example.com")
        rcpt = rm._extract_bounce_recipient(msg, "mailer-daemon@cpweb02.misshosting.no")
        self.assertNotEqual(rcpt, "cpanel@blueboot.ai")
        self.assertEqual(rcpt, "realcontact@example.com")


class TestSelfOrSystem(unittest.TestCase):
    OWN = {"blueboot.ai", "blueboot.no"}

    def test_own_domain_skipped(self):
        self.assertTrue(rm._is_self_or_system("cpanel@blueboot.ai", self.OWN))
        self.assertTrue(rm._is_self_or_system("anyone@blueboot.no", self.OWN))

    def test_system_localpart_skipped_any_domain(self):
        self.assertTrue(rm._is_self_or_system("no-reply@somehost.com", self.OWN))
        self.assertTrue(rm._is_self_or_system("mailer-daemon@x.net", self.OWN))

    def test_external_contact_not_skipped(self):
        self.assertFalse(rm._is_self_or_system("astri@drople.no", self.OWN))
        self.assertFalse(rm._is_self_or_system("leifauke@gmail.com", self.OWN))

    def test_empty_or_garbage_not_skipped(self):
        self.assertFalse(rm._is_self_or_system("", self.OWN))
        self.assertFalse(rm._is_self_or_system(None, self.OWN))
        self.assertFalse(rm._is_self_or_system("notanemail", self.OWN))


class TestAlreadyInHistory(unittest.TestCase):
    """The rule: do NOT update a contact whose history already has this msg."""

    def _db_with_history(self, msg_id, status="pending", htype="EMAIL_IN"):
        existing = {"status": status,
                    "comment_history": [{"type": htype, "message_id": msg_id}]}
        return FakeDB(docstore={("C", "campaign_contacts", "c1"): dict(existing)})

    def test_reply_already_handled_live(self):
        db = self._db_with_history("<dup>")
        out = rm._apply_actions(db, "c1", "C", None, None,
                                {"message_id": "<dup>", "subject": "x",
                                 "from_email": "a@b.com"}, "doc_id")
        self.assertEqual(out, "already_handled")
        # status untouched
        self.assertEqual(db.docstore[("C", "campaign_contacts", "c1")]["status"], "pending")

    def test_reply_already_handled_dry_run(self):
        db = self._db_with_history("<dup>")
        out = rm._apply_actions(db, "c1", "C", None, None,
                                {"message_id": "<dup>", "subject": "x",
                                 "from_email": "a@b.com"}, "doc_id", dry_run=True)
        self.assertEqual(out, "already_handled")   # not "dry_run"

    def test_bounce_already_handled_dry_run(self):
        db = self._db_with_history("<b>", htype="BOUNCE")
        out = rm._apply_bounce_actions(db, "c1", "C", None, None,
                                       {"message_id": "<b>", "subject": "Undeliverable",
                                        "received_at": "2026-06-23T00:00:00+00:00"},
                                       dry_run=True)
        self.assertEqual(out, "already_handled")

    def test_new_reply_not_blocked(self):
        db = self._db_with_history("<old>")
        out = rm._apply_actions(db, "c1", "C", None, None,
                                {"message_id": "<new>", "subject": "x",
                                 "from_email": "a@b.com"}, "doc_id")
        self.assertEqual(out, "updated")


if __name__ == "__main__":
    unittest.main(verbosity=2)
