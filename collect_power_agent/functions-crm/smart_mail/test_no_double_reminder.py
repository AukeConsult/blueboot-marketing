"""A reminder must never follow the previous mail within a day, and a replied contact gets none."""
import sys, os, types, unittest
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _name in ("google", "google.cloud", "google.cloud.firestore_v1", "google.cloud.firestore_v1.base_query"):
    sys.modules.setdefault(_name, types.ModuleType(_name))
sys.modules["google.cloud.firestore_v1.base_query"].FieldFilter = getattr(
    sys.modules["google.cloud.firestore_v1.base_query"], "FieldFilter", object)
_fc = types.ModuleType("smart_mail.firestore_client"); _fc.get_firestore = lambda: None
sys.modules.setdefault("smart_mail.firestore_client", _fc)

from smart_mail import outreach_mail_select as sel

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


def _campaign():
    return types.SimpleNamespace(mail_sequence=[
        {"index": 0, "mail_type": "intro"},
        {"index": 1, "mail_type": "reminder", "delay_days": 3},
        {"index": 2, "mail_type": "reminder", "delay_days": 7},
    ])


def _contact(sent):
    return types.SimpleNamespace(mail_sent=[{"sent_at": _iso(d)} for d in sent])


class Due(unittest.TestCase):
    def test_first_reminder_waits_for_delay(self):
        self.assertFalse(sel._next_step_due(_contact([2]), _campaign(), NOW))
        self.assertTrue(sel._next_step_due(_contact([4]), _campaign(), NOW))

    def test_second_reminder_not_sent_right_after_the_first(self):
        # intro 20 days ago; reminder 1 sent 15 minutes ago (late catch-up). Step 2 is overdue by the
        # calendar but must wait because the last mail is fresh.
        c = types.SimpleNamespace(mail_sent=[{"sent_at": _iso(20)}, {"sent_at": (NOW - timedelta(minutes=15)).isoformat()}])
        self.assertFalse(sel._next_step_due(c, _campaign(), NOW))

    def test_second_reminder_after_the_gap(self):
        self.assertTrue(sel._next_step_due(_contact([20, 2]), _campaign(), NOW))

    def test_sequence_finished(self):
        self.assertFalse(sel._next_step_due(_contact([30, 20, 10]), _campaign(), NOW))


class Reread(unittest.TestCase):
    def _patch(self, doc):
        snap = types.SimpleNamespace(exists=doc is not None, to_dict=lambda: doc)
        chain = types.SimpleNamespace()
        chain.collection = lambda *_: chain
        chain.document = lambda *_: chain
        chain.get = lambda: snap
        sel._get_db = lambda: chain

    def test_blocks_replied_contact(self):
        self._patch({"status": "pending", "followup_status": "replied", "mail_sent": [{"sent_at": _iso(5)}]})
        self.assertIn("replied", sel.contact_not_sendable_reason("C", "d", 1))

    def test_blocks_fresh_last_mail(self):
        recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self._patch({"status": "pending", "mail_sent": [{"sent_at": recent}]})
        self.assertIn("less than a day", sel.contact_not_sendable_reason("C", "d", 1))

    def test_blocks_when_count_changed(self):
        self._patch({"status": "pending", "mail_sent": [{"sent_at": _iso(5)}, {"sent_at": _iso(4)}]})
        self.assertIn("another run", sel.contact_not_sendable_reason("C", "d", 1))

    def test_allows_normal_reminder(self):
        self._patch({"status": "pending", "mail_sent": [{"sent_at": _iso(5)}]})
        self.assertEqual(sel.contact_not_sendable_reason("C", "d", 1), "")

    def test_intro_unaffected(self):
        self._patch({"status": "pending", "mail_sent": []})
        self.assertEqual(sel.contact_not_sendable_reason("C", "d", 0), "")


if __name__ == "__main__":
    unittest.main()
