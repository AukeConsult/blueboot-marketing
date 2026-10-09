"""Tests for outreach_status_lib using a tiny in-memory Firestore fake."""
import sys, os, unittest
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import google.cloud.firestore_v1.base_query  # noqa: F401
except ImportError:                                # run without the Google SDK installed
    import types
    for name in ("google", "google.cloud", "google.cloud.firestore_v1", "google.cloud.firestore_v1.base_query"):
        sys.modules.setdefault(name, types.ModuleType(name))
    class _FF:
        def __init__(self, field_path, op_string, value):
            self.field_path, self.op_string, self.value = field_path, op_string, value
    sys.modules["google.cloud.firestore_v1.base_query"].FieldFilter = _FF

from smart_mail import outreach_status_lib as lib
from smart_mail.outreach_mail_select import MailAccountSettings


class Doc:
    def __init__(self, id, data, db, path):
        self.id, self._d, self._db, self._path = id, data, db, path
        self.exists = data is not None
    def to_dict(self): return dict(self._d or {})
    def get(self): return self


class Q:
    def __init__(self, items): self.items = items
    def where(self, filter=None):
        f = filter
        op = {"==": lambda a, b: a == b, ">=": lambda a, b: a is not None and a >= b,
              "<": lambda a, b: a is not None and a < b}[f.op_string]
        return Q([d for d in self.items if op(d.to_dict().get(f.field_path), f.value)])
    def select(self, fields): return self
    def stream(self): return iter(self.items)


class Col(Q):
    def __init__(self, db, path):
        self.db, self.path = db, path
    @property
    def items(self):
        pre = self.path + "/"
        return [Doc(k[len(pre):], v, self.db, k) for k, v in sorted(self.db.data.items())
                if k.startswith(pre) and "/" not in k[len(pre):]]
    def document(self, id): return DocRef(self.db, self.path + "/" + id)


class DocRef:
    def __init__(self, db, path): self.db, self.path = db, path
    def get(self): return Doc(self.path.split("/")[-1], self.db.data.get(self.path), self.db, self.path)
    def collection(self, name): return Col(self.db, self.path + "/" + name)


class FakeDB:
    def __init__(self, data): self.data = data
    def collection(self, name): return Col(self, name)


NOW = datetime.now(timezone.utc)
ACCOUNT = MailAccountSettings(email="a@x.com", account_type="imap", host="smtp.x.com", port=587,
                              username="a", password="pw", from_name="A", imap_host="imap.x.com",
                              imap_port=993, use_ssl=False)
SEQ = [{"index": 0, "mail_type": "intro"}, {"index": 1, "mail_type": "reminder", "delay_days": 3}]


class T(unittest.TestCase):
    def run_status(self, data, account=ACCOUNT):
        orig = lib._load_account
        lib._load_account = lambda db, e: account
        try:
            return lib.compute_outreach_status(FakeDB(data))
        finally:
            lib._load_account = orig

    def test_counts_and_budget(self):
        old = (NOW - timedelta(days=5)).isoformat()
        c = "campaigns/c1"
        data = {
            c: {"status": "active", "name": "C1", "outreach_email_account": "a@x.com",
                "mail_sequence": SEQ, "require_send_confirmation": True},
            c + "/campaign_leads/s1": {"status": "pending"},
            c + "/campaign_leads/s2": {"status": "excluded"},
            c + "/campaign_contacts/1": {"status": "pending", "send_confirmation": True, "lead_id": "s1"},
            c + "/campaign_contacts/2": {"status": "pending", "send_confirmation": False, "lead_id": "s1"},
            c + "/campaign_contacts/3": {"status": "pending", "send_confirmation": True, "lead_id": "s2"},
            c + "/campaign_contacts/4": {"status": "pending", "mail_sent": [{"sent_at": old}], "lead_id": "s1"},
            c + "/campaign_contacts/5": {"status": "pending", "mail_sent": [{"sent_at": NOW.isoformat()}]},
            c + "/campaign_contacts/6": {"status": "excluded"},
            "settings/send_limits": {"max_sends_per_hour": 10, "max_sends_per_day": 100},
            "outreach_sent/x": {"sender_account": "a@x.com", "sent_at": NOW.isoformat()},
        }
        r = self.run_status(data)
        row = r["campaigns"][0]
        self.assertTrue(row["ready"])
        self.assertEqual((row["intro_waiting"], row["intro_not_confirmed"], row["intro_site_blocked"],
                          row["intro_to_send"]), (3, 1, 1, 1))
        self.assertEqual((row["followup_waiting"], row["followup_due"], row["followup_not_due"]), (2, 1, 1))
        a = r["accounts"][0]
        self.assertEqual(a["budget"], 9)
        self.assertEqual(a["waiting"], 2)
        self.assertEqual(a["next_run_sends"], 2)
        self.assertEqual(r["totals"]["next_run_sends"], 2)

    def test_blockers(self):
        data = {"campaigns/d": {"status": "draft", "name": "D", "mail_sequence": []}}
        r = self.run_status(data, account=None)
        row = r["campaigns"][0]
        self.assertFalse(row["ready"])
        self.assertTrue(any("draft" in b for b in row["blockers"]))
        self.assertTrue(any("Intro" in b for b in row["blockers"]))
        self.assertTrue(any("mail account" in b for b in row["blockers"]))
        self.assertEqual(r["totals"]["next_run_sends"], 0)

    def test_budget_caps_next_run(self):
        c = "campaigns/c1"
        data = {c: {"status": "ready", "outreach_email_account": "a@x.com", "mail_sequence": SEQ},
                "settings/send_limits": {"max_sends_per_hour": 1, "max_sends_per_day": 5}}
        for i in range(4):
            data[f"{c}/campaign_contacts/{i}"] = {"status": "pending"}
        r = self.run_status(data)
        a = r["accounts"][0]
        self.assertEqual((a["next_run_sends"], a["left_over"]), (1, 3))


if __name__ == "__main__":
    unittest.main()
