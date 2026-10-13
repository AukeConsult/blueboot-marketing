"""test_facet_campaign_sites.py -- one campaign per site rule of crm.facet_campaign_lib.

Run:  python app/test_facet_campaign_sites.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "functions-crm"))
try:   # the pure helper under test does not need Firestore; stub it where it is not installed
    import google.cloud.firestore_v1.base_query  # noqa: F401
except ImportError:
    from unittest import mock
    for m in ("google", "google.cloud", "google.cloud.firestore_v1", "google.cloud.firestore_v1.base_query"):
        sys.modules[m] = mock.MagicMock()
from crm.facet_campaign_lib import _keep_whole_sites  # noqa: E402


def _ec(site, n):
    return {"lead_id_site": site, "email": f"{site}{n}@x.no"}


SITE = lambda ec: ec["lead_id_site"]  # noqa: E731


class KeepWholeSitesTest(unittest.TestCase):
    def test_sites_in_other_campaigns_are_dropped(self):
        matched = [_ec("a", 1), _ec("a", 2), _ec("b", 1)]
        kept, dropped, capped = _keep_whole_sites(matched, SITE, {"a"}, 10)
        self.assertEqual([e["email"] for e in kept], ["b1@x.no"])
        self.assertEqual((dropped, capped), (2, 0))

    def test_cap_counts_sites_and_never_splits_a_site(self):
        matched = [_ec("a", 1), _ec("a", 2), _ec("a", 3), _ec("b", 1), _ec("c", 1)]
        kept, dropped, capped = _keep_whole_sites(matched, SITE, set(), 2)
        self.assertEqual(len(kept), 4)                       # all 3 of a + b, c cut as a whole site
        self.assertEqual({e["lead_id_site"] for e in kept}, {"a", "b"})
        self.assertEqual((dropped, capped), (0, 3))

    def test_contact_without_site_is_its_own_group(self):
        kept, _, _ = _keep_whole_sites([{"email": "z@x.no"}], lambda ec: "", set(), 5)
        self.assertEqual(len(kept), 1)


if __name__ == "__main__":
    unittest.main()
