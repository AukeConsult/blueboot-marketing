"""test_push_to_campaign.py -- unit tests for crm.push_to_campaign_lib (stubbed Firestore + Sheets).

Run:  python app/test_push_to_campaign.py     (or: python -m unittest app.test_push_to_campaign)
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "functions-crm"))
import crm.push_to_campaign_lib as L  # noqa: E402


class _Doc:
    def __init__(self, campaign, doc_id, data):
        self.id, self._data = doc_id, data
        self.reference = mock.Mock()
        self.reference.parent.parent.id = campaign

    def to_dict(self):
        return self._data


class _Query:
    def __init__(self, docs):
        self.docs = docs

    def select(self, _fields):
        return self

    def stream(self):
        return iter(self.docs)


class _Db:
    def __init__(self, leads, contacts, campaigns=()):
        self.q = {"campaign_leads": leads, "campaign_contacts": contacts}
        self.campaigns = [_Doc(None, c, {}) for c in campaigns]

    def collection_group(self, name):
        return _Query(self.q[name])

    def collection(self, name):
        assert name == "campaigns"
        return _Query(self.campaigns)


def _svc(rows):
    svc = mock.Mock()
    svc.spreadsheets().values().get().execute.return_value = {"values": rows}
    return svc


HEAD = ["Select", "Campaign", "Email", "Website", "Name", "Lead ID Site"]
SHEET = [HEAD,
         ["x", "Alpha", "new@a.no", "a.no", "New", "site_a"],          # added
         ["x", "Alpha", "two@a.no", "a.no", "Two", "site_a"],          # added, same site
         ["x", "Alpha", "b@b.no", "b.no", "B", "site_b"],              # site in Beta
         ["x", "Alpha", "c@c.no", "c.no", "C", "site_c"],              # contact in Beta
         ["x", "Alpha", "d@d.no", "d.no", "D", "site_d"],              # already in Alpha
         ["x", "", "e@e.no", "e.no", "E", "site_e"],                   # no campaign
         ["x", "Alpha", "bad", "f.no", "F", "site_f"],                 # no valid e-mail
         ["", "Alpha", "skip@a.no", "a.no", "Not selected", "site_a"]]  # Select blank


def _db():
    return _Db(leads=[_Doc("Beta", "site_b", {}), _Doc("Alpha", "site_d", {})],
               contacts=[_Doc("Beta", "x1", {"email": "C@c.no"}), _Doc("Alpha", "x2", {"email": "d@d.no"})],
               campaigns=["alpha"])


class PreviewTest(unittest.TestCase):
    def test_rules(self):
        r = L.preview_push(_db(), _svc(SHEET))
        self.assertEqual(r["selected"], 7)
        self.assertEqual((r["add"]["contacts"], r["add"]["sites"]), (2, 1))
        ign = r["ignored"]
        self.assertEqual(ign["site_in_other"]["items"][0]["others"], ["Beta"])
        self.assertEqual(ign["contact_in_other"]["items"][0]["others"], ["Beta"])
        self.assertEqual(ign["already_in_campaign"]["count"], 1)
        self.assertEqual(ign["no_campaign"]["count"], 1)
        self.assertEqual(ign["no_email"]["count"], 1)
        self.assertTrue(r["can_push"])
        self.assertEqual(r["new_campaigns"], [])

    def test_campaign_filter_and_unknown_campaign(self):
        r = L.preview_push(_Db([], []), _svc(SHEET), campaign="alpha")
        self.assertEqual(r["new_campaigns"], ["Alpha"])
        self.assertTrue(any("will be created" in w for w in r["warnings"]))

    def test_missing_select_column(self):
        r = L.preview_push(_db(), _svc([["Email"], ["a@a.no"]]))
        self.assertFalse(r["can_push"])
        self.assertTrue(r["warnings"])


class PushTest(unittest.TestCase):
    def test_only_added_rows_are_imported(self):
        with mock.patch.object(L, "run_campaign_import",
                               return_value={"contacts_new": 2, "leads_new": 1}) as imp:
            out = L.run_push_to_campaign(_db(), _svc(SHEET))
        self.assertEqual(imp.call_count, 1)
        _db_arg, campaign, rows = imp.call_args[0]
        self.assertEqual(campaign, "Alpha")
        self.assertEqual({r["email"] for r in rows}, {"new@a.no", "two@a.no"})
        self.assertTrue(all(r["lead_id"] == "site_a" for r in rows))
        self.assertEqual((out["contacts_added"], out["sites_added"]), (2, 1))
        self.assertEqual(out["ignored"]["site_in_other"], 1)


if __name__ == "__main__":
    unittest.main()
