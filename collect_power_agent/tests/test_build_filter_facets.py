"""Offline test for app/build_filter_facets.py -- no Firestore credentials needed.

Stubs the Firestore client with a small in-memory fake dataset, runs build_facets(),
and asserts every facet comes out with the right shape, counts and merge behaviour.

Run directly:      python tests/test_build_filter_facets.py
Run with pytest:   pytest tests/test_build_filter_facets.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import _pathsetup  # noqa: F401,E402
import build_filter_facets as B  # noqa: E402


# --- a minimal Firestore fake ------------------------------------------------
class _FakeDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _FakeQuery:
    """Supports .select(fields).stream() -- select is a no-op (we return all fields)."""
    def __init__(self, docs):
        self._docs = docs

    def select(self, _fields):
        return self

    def stream(self):
        return iter(self._docs)


class _FakeDB:
    def __init__(self, leads, contacts):
        self._leads = leads
        self._contacts = contacts

    def collection(self, name):
        assert name == "site_leads", name
        return _FakeQuery(self._leads)

    def collection_group(self, name):
        assert name == "site_contacts", name
        return _FakeQuery(self._contacts)


# --- fixture data ------------------------------------------------------------
LEADS = [
    _FakeDoc("l1", {"platform": "woocommerce", "ai_platform": "wordpress",
                    "ai_sector": "ecommerce", "ai_company_type": "B2C",
                    "country": "NO", "ai_country": "NO",
                    "location": "Oslo, Norway", "location_country": "Norway",
                    "keywords": ["webshop", "ecommerce"], "page_count": 1500}),
    _FakeDoc("l2", {"platform": "shopify", "ai_platform": "shopify",
                    "ai_sector": "ecommerce", "ai_company_type": "B2B",
                    "country": "SE", "ai_country": "SE",
                    "location": "Stockholm, Sweden", "location_country": "Sweden",
                    "keywords": ["webshop"], "page_count": 40}),       # micro
    _FakeDoc("l3", {"platform": "", "ai_platform": "unknown",
                    "ai_sector": "technology", "ai_company_type": "B2B",
                    "country": "NO", "ai_country": "NO",
                    "location": "", "location_country": "",
                    "keywords": [], "page_count": 0}),                 # unknown band
]
CONTACTS = [
    _FakeDoc("c1", {"country": "NO", "ai_country": "NO", "occupation": "CEO",
                    "title": "Daglig leder", "email_type": "personal"}),
    _FakeDoc("c2", {"country": "NO", "ai_country": "NO", "occupation": "CEO",
                    "title": "CEO", "email_type": "role"}),
    _FakeDoc("c3", {"country": "DK", "ai_country": "DK", "occupation": "CTO",
                    "title": "CTO", "email_type": "personal"}),
]


def _facets():
    B._get_db = lambda: _FakeDB(LEADS, CONTACTS)   # monkeypatch the DB accessor
    return B.build_facets("site_leads", cap=300)


def test_counts_and_structure():
    f = _facets()
    assert f["lead_count"] == 3
    assert f["contact_count"] == 3
    assert set(f["filters"]) == {
        "platform", "ai_platform", "ai_sector", "ai_company_type",
        "country", "ai_country", "location", "location_country",
        "keywords", "page_count", "occupation", "title", "email_type",
    }


def test_enum_values_and_counts():
    f = _facets()["filters"]
    # platform: blank values are dropped, two real values remain
    plat = {v["value"]: v["count"] for v in f["platform"]["values"]}
    assert plat == {"woocommerce": 1, "shopify": 1}
    # ai_sector: ecommerce appears twice
    sec = {v["value"]: v["count"] for v in f["ai_sector"]["values"]}
    assert sec == {"ecommerce": 2, "technology": 1}


def test_country_is_merged_across_both_collections():
    f = _facets()["filters"]
    cty = {v["value"]: v["count"] for v in f["country"]["values"]}
    # NO: 2 leads + 2 contacts = 4 ; SE: 1 lead ; DK: 1 contact
    assert cty == {"NO": 4, "SE": 1, "DK": 1}
    assert f["country"]["source"].startswith("merged:")


def test_keywords_is_array_enum():
    f = _facets()["filters"]
    assert f["keywords"]["type"] == "array_enum"
    kw = {v["value"]: v["count"] for v in f["keywords"]["values"]}
    assert kw == {"webshop": 2, "ecommerce": 1}


def test_page_count_group_buckets():
    f = _facets()["filters"]
    counts = {g["key"]: g["count"] for g in f["page_count"]["groups"]}
    assert counts["medium"] == 1     # l1 = 1500
    assert counts["micro"] == 1      # l2 = 40
    assert counts["unknown"] == 1    # l3 = 0
    assert f["page_count"]["type"] == "group"


def test_truncated_flag():
    B._get_db = lambda: _FakeDB(LEADS, CONTACTS)
    tight = B.build_facets("site_leads", cap=300)["filters"]
    assert tight["ai_sector"]["truncated"] is False
    small = B.build_facets("site_leads", cap=1)["filters"]
    assert small["ai_sector"]["truncated"] is True       # 2 distinct > cap 1


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
