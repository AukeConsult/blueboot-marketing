"""
functions/statistics_builder.py -- StatisticsBuilder class.

Encapsulates all statistics aggregations (formerly scattered inner functions
inside maint_statistics.py) with a single shared Firestore connection and
consistent timestamps across one run.

Usage:
    from functions.statistics_builder import StatisticsBuilder

    sb = StatisticsBuilder()                          # uses cfg defaults
    overview  = sb.collection_overview()
    funnel    = sb.site_pipeline_enrichment_funnel()
    all_r     = sb.run_all()
"""
from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, timezone

_local_fb_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Firestore streaming helpers (module-level so they can be reused elsewhere)
# ---------------------------------------------------------------------------

def stream_safe(query):
    """Stream a Firestore query, skipping docs that raise ValueError."""
    gen = query.stream()
    while True:
        try:
            doc = next(gen)
        except StopIteration:
            break
        except (ValueError, AttributeError):
            continue
        try:
            yield doc.to_dict() or {}, doc
        except Exception:
            continue


def stream_partitioned(query, partitions: int = 16, workers: int = 16):
    """Stream a collection using parallel partitions for speed.
    Returns an iterable of (dict, doc) tuples."""
    from concurrent.futures import ThreadPoolExecutor

    try:
        partition_queries = [p.query() for p in query.get_partitions(partitions)]
    except Exception:
        partition_queries = [query]

    def _fetch(q):
        return list(stream_safe(q))

    all_results = []
    with ThreadPoolExecutor(max_workers=min(workers, len(partition_queries))) as pool:
        for batch in pool.map(_fetch, partition_queries):
            all_results.extend(batch)
    return all_results


def count_by_field(db, col_name: str, field: str, limit: int = 5000) -> dict[str, int]:
    """Stream a collection and tally values of a single field."""
    counts: dict[str, int] = {}
    for doc in db.collection(col_name).select([field]).limit(limit).stream():
        val = (doc.to_dict() or {}).get(field) or "?"
        if isinstance(val, str):
            val = val.strip().upper() or "?"
        counts[str(val)] = counts.get(str(val), 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


# ---------------------------------------------------------------------------
# StatisticsBuilder
# ---------------------------------------------------------------------------

class StatisticsBuilder:
    """All statistics aggregations with a shared db connection and timestamp."""

    def __init__(self, leads_collection: str | None = None,
                 stats_collection: str = "statistics"):
        from functions.config import cfg
        self.leads_col    = leads_collection or cfg.FIRESTORE_COLLECTION
        self.stats_col    = stats_collection
        self.db           = self._init_firebase()
        self.generated_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

    # ── Firebase init ─────────────────────────────────────────────────────

    def _init_firebase(self):
        import firebase_admin
        from firebase_admin import firestore
        from functions.firebase_cred import get_firebase_cred
        with _local_fb_lock:
            if not firebase_admin._apps:
                firebase_admin.initialize_app(get_firebase_cred())
        return firestore.client()

    # ── Shared helpers ────────────────────────────────────────────────────

    def _pct(self, n: int, total: int) -> str:
        return f"{n:>6}  ({100 * n // total if total else 0}%)"

    def _write(self, doc_id: str, data: dict) -> None:
        """Write a document to the stats collection."""
        self.db.collection(self.stats_col).document(doc_id).set(
            {**data, "generated_at": self.generated_at}, merge=True
        )
        print(f"  [stats] written → {self.stats_col}/{doc_id}", flush=True)

    # ── collection_overview sub-scanners ──────────────────────────────────

    def _leads_stats(self) -> dict:
        count       = sum(1 for _ in self.db.collection("leads").select([]).stream())
        by_country  = count_by_field(self.db, "leads", "country")
        by_priority = count_by_field(self.db, "leads", "priority")
        top_c = "  ".join(f"{k}:{v}" for k, v in list(by_country.items())[:5])
        top_p = "  ".join(f"{k}:{v}" for k, v in by_priority.items())
        print(f"  {'Leads (legacy)':<30} {count:>7}  country: {top_c}")
        print(f"  {'':30}         priority: {top_p}")
        return {"count": count, "by_country": by_country, "by_priority": by_priority}

    def _leads_excluded_stats(self) -> dict:
        by_country: dict = {}
        by_reason:  dict = {}
        count = 0
        for d, _ in stream_partitioned(
                self.db.collection("leads_excluded").select(["country", "reason"])):
            count += 1
            c = (d.get("country") or "?").strip().upper() or "?"
            r = (d.get("reason")  or "?").strip() or "?"
            by_country[c] = by_country.get(c, 0) + 1
            by_reason[r]  = by_reason.get(r, 0)  + 1
        by_country = dict(sorted(by_country.items(), key=lambda x: -x[1]))
        by_reason  = dict(sorted(by_reason.items(),  key=lambda x: -x[1]))
        top_c = "  ".join(f"{k}:{v}" for k, v in list(by_country.items())[:5])
        top_r = "  ".join(f"{k}:{v}" for k, v in list(by_reason.items())[:3])
        print(f"  {'Leads Excluded':<30} {count:>7}  country: {top_c or '—'}")
        print(f"  {'':<30}         reason: {top_r or '—'}")
        return {"count": count, "by_country": by_country, "by_reason": by_reason}

    def _site_leads_stats(self) -> dict:
        count         = sum(1 for _ in self.db.collection("site_leads").select([]).stream())
        by_country    = count_by_field(self.db, "site_leads", "country")
        by_ai_country = count_by_field(self.db, "site_leads", "ai_country")
        by_ai_sector  = count_by_field(self.db, "site_leads", "ai_sector")
        page_buckets  = self._page_count_buckets()
        top_c   = "  ".join(f"{k}:{v}" for k, v in list(by_country.items())[:5])
        top_aic = "  ".join(f"{k}:{v}" for k, v in list(by_ai_country.items())[:5])
        top_s   = "  ".join(f"{k}:{v}" for k, v in list(by_ai_sector.items())[:4])
        top_pg  = "  ".join(f"{k}:{v}" for k, v in page_buckets.items() if v > 0)
        print(f"  {'Site Leads':<30} {count:>7}  country: {top_c}")
        print(f"  {'':30}         ai_country: {top_aic}")
        print(f"  {'':30}         ai_sector: {top_s}")
        print(f"  {'':30}         page_size: {top_pg}")
        return {"count": count, "by_country": by_country,
                "by_ai_country": by_ai_country, "by_ai_sector": by_ai_sector,
                "by_page_size": page_buckets}

    def _sites_excluded_stats(self) -> dict:
        by_country: dict = {}
        by_reason:  dict = {}
        count = 0
        for d, _ in stream_partitioned(
                self.db.collection("sites_excluded").select(["country", "reason"])):
            count += 1
            c = (d.get("country") or "?").strip().upper() or "?"
            r = (d.get("reason")  or "?").strip() or "?"
            by_country[c] = by_country.get(c, 0) + 1
            by_reason[r]  = by_reason.get(r, 0)  + 1
        by_country = dict(sorted(by_country.items(), key=lambda x: -x[1]))
        by_reason  = dict(sorted(by_reason.items(),  key=lambda x: -x[1]))
        top_c = "  ".join(f"{k}:{v}" for k, v in list(by_country.items())[:5])
        top_r = "  ".join(f"{k}:{v}" for k, v in list(by_reason.items())[:3])
        print(f"  {'Sites Excluded':<30} {count:>7}  country: {top_c or '—'}")
        print(f"  {'':<30}         reason: {top_r or '—'}")
        return {"count": count, "by_country": by_country, "by_reason": by_reason}

    def _page_count_buckets(self) -> dict:
        buckets = {
            "micro   (1–50)":     0,
            "small   (51–500)":   0,
            "medium  (501–3k)":   0,
            "large   (3k–10k)":   0,
            "huge    (10k–100k)": 0,
            "ultra   (100k+)":    0,
            "unknown (0/None)":   0,
        }
        for doc in self.db.collection("site_leads").select(["page_count"]).stream():
            try:
                pc = int((doc.to_dict() or {}).get("page_count") or 0)
            except (TypeError, ValueError):
                pc = 0
            if pc == 0:         buckets["unknown (0/None)"] += 1
            elif pc <= 50:      buckets["micro   (1–50)"]   += 1
            elif pc <= 500:     buckets["small   (51–500)"] += 1
            elif pc <= 3000:    buckets["medium  (501–3k)"] += 1
            elif pc <= 10000:   buckets["large   (3k–10k)"] += 1
            elif pc <= 100000:  buckets["huge    (10k–100k)"] += 1
            else:               buckets["ultra   (100k+)"]  += 1
        return buckets

    # ── funnel sub-scanners (reused by site funnel + quality report) ───────

    def _scan_site_leads(self) -> dict:
        r = {"total": 0, "ai": 0, "loc": 0, "both": 0,
             "no_sitemap": 0, "zero_pages": 0, "not_classified": 0}
        for d, _ in stream_partitioned(
                self.db.collection("site_leads")
                .select(["ai_classified_at", "location_enriched_at",
                         "sitemap_type", "page_count"])):
            r["total"] += 1
            ai  = bool((d.get("ai_classified_at")     or "").strip())
            loc = bool((d.get("location_enriched_at") or "").strip())
            if ai:           r["ai"]   += 1
            if loc:          r["loc"]  += 1
            if ai and loc:   r["both"] += 1
            if (d.get("sitemap_type") or "") in ("none", ""):
                r["no_sitemap"] += 1
            if int(d.get("page_count") or 0) == 0:
                r["zero_pages"] += 1
            if not (d.get("ai_classified_at") or "").strip():
                r["not_classified"] += 1
        return r

    def _scan_site_contacts(self) -> dict:
        from functions.utils import email_matches_name
        r = {"total": 0, "brave": 0, "checked": 0, "both": 0,
             "no_name": 0, "no_email": 0, "mismatch": 0}
        for d, _ in stream_partitioned(
                self.db.collection_group("site_contacts")
                .select(["brave_enriched_at", "email_checked_at", "name", "email"])):
            r["total"] += 1
            brave = bool((d.get("brave_enriched_at") or "").strip())
            chkd  = bool((d.get("email_checked_at")  or "").strip())
            name  = (d.get("name")  or "").strip()
            email = (d.get("email") or "").strip()
            if brave:          r["brave"]   += 1
            if chkd:           r["checked"] += 1
            if brave and chkd: r["both"]    += 1
            if not name:       r["no_name"]  += 1
            if not email:      r["no_email"] += 1
            elif name and not email_matches_name(email, name):
                r["mismatch"] += 1
        return r

    def _scan_lead_contacts(self) -> dict:
        r = {"total": 0, "social": 0, "checked": 0, "both": 0}
        for d, _ in stream_partitioned(
                self.db.collection_group("contacts")
                .select(["social_enriched_at", "email_checked_at"])):
            r["total"] += 1
            s = bool((d.get("social_enriched_at") or "").strip())
            c = bool((d.get("email_checked_at")   or "").strip())
            if s:      r["social"]  += 1
            if c:      r["checked"] += 1
            if s and c: r["both"]   += 1
        return r

    # ── public aggregation methods ─────────────────────────────────────────

    def leads_overview(self) -> dict:
        """Legacy lead pipeline: leads + leads_excluded."""
        print("\n--- Lead Pipeline Overview ---")
        result = {"generated_at": self.generated_at, "collections": {}}
        for col_name, fn in [
            ("leads",          self._leads_stats),
            ("leads_excluded", self._leads_excluded_stats),
        ]:
            try:
                result["collections"][col_name] = fn()
            except Exception as exc:
                result["collections"][col_name] = {"error": str(exc)}
        n_stored = result["collections"].get("leads", {}).get("count", 0)
        n_excl   = result["collections"].get("leads_excluded", {}).get("count", 0)
        total    = n_stored + n_excl
        result["exclusion_rate"] = int(100 * n_excl / total) if total else 0
        print(f"  Leads stored={n_stored}  excluded={n_excl}  "
              f"rejection={result['exclusion_rate']}%")
        self._write("leads-overview", result)
        return result

    def site_leads_overview(self) -> dict:
        """Site lead pipeline: site_leads + sites_excluded."""
        print("\n--- Site Lead Pipeline Overview ---")
        result = {"generated_at": self.generated_at, "collections": {}}
        for col_name, fn in [
            ("site_leads",     self._site_leads_stats),
            ("sites_excluded", self._sites_excluded_stats),
        ]:
            try:
                result["collections"][col_name] = fn()
            except Exception as exc:
                result["collections"][col_name] = {"error": str(exc)}
        n_stored = result["collections"].get("site_leads", {}).get("count", 0)
        n_excl   = result["collections"].get("sites_excluded", {}).get("count", 0)
        total    = n_stored + n_excl
        result["exclusion_rate"] = int(100 * n_excl / total) if total else 0
        print(f"  Site leads stored={n_stored}  excluded={n_excl}  "
              f"rejection={result['exclusion_rate']}%")
        self._write("site-leads-overview", result)
        return result

    def collection_overview(self) -> dict:
        """Combined overview — calls both pipeline methods."""
        leads = self.leads_overview()
        site  = self.site_leads_overview()
        combined = {
            "generated_at": self.generated_at,
            "collections":  {**leads["collections"], **site["collections"]},
            "leads_excluded_rate":  leads["exclusion_rate"],
            "sites_excluded_rate":  site["exclusion_rate"],
        }
        self._write("collection-overview", combined)
        return combined

    def site_pipeline_enrichment_funnel(self) -> dict:
        from concurrent.futures import ThreadPoolExecutor
        print("\n--- Site Pipeline Enrichment Funnel ---")
        print("  [funnel] streaming site_leads + site_contacts in parallel…")
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_leads    = pool.submit(self._scan_site_leads)
            f_contacts = pool.submit(self._scan_site_contacts)
            L = f_leads.result()
            C = f_contacts.result()

        lt = L["total"]; ct = C["total"]
        print(f"\n  SITE LEADS ({lt} total)")
        print(f"    AI classified:       {self._pct(L['ai'],   lt)}")
        print(f"    Location enriched:   {self._pct(L['loc'],  lt)}")
        print(f"    Fully enriched:      {self._pct(L['both'], lt)}")
        print(f"\n  SITE CONTACTS ({ct} total)")
        print(f"    Brave enriched:      {self._pct(C['brave'],   ct)}")
        print(f"    Email checked:       {self._pct(C['checked'], ct)}")
        print(f"    Fully ready:         {self._pct(C['both'],    ct)}")
        print(f"    Missing name:        {self._pct(C['no_name'], ct)}")
        print(f"    Missing email:       {self._pct(C['no_email'],ct)}")

        result = {
            "leads_total":              lt,
            "leads_ai_classified":      L["ai"],
            "leads_location_enriched":  L["loc"],
            "leads_both_enriched":      L["both"],
            "contacts_total":           ct,
            "contacts_brave_enriched":  C["brave"],
            "contacts_email_checked":   C["checked"],
            "contacts_fully_ready":     C["both"],
            "contacts_no_name":         C["no_name"],
            "contacts_no_email":        C["no_email"],
        }
        self._write("site-enrichment-funnel", result)
        return result

    def lead_pipeline_enrichment_funnel(self) -> dict:
        from concurrent.futures import ThreadPoolExecutor
        print("\n--- Lead Pipeline Enrichment Funnel ---")

        def _scan_leads():
            r = {"total": 0, "ai": 0, "no_email": 0}
            for d, _ in stream_partitioned(
                    self.db.collection(self.leads_col)
                    .select(["ai_classified_at", "emails"])):
                r["total"] += 1
                if (d.get("ai_classified_at") or "").strip(): r["ai"] += 1
                if not (d.get("emails") or "").strip():       r["no_email"] += 1
            return r

        print(f"  [funnel] streaming {self.leads_col} + contacts in parallel…")
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_leads    = pool.submit(_scan_leads)
            f_contacts = pool.submit(self._scan_lead_contacts)
            L = f_leads.result()
            C = f_contacts.result()

        lt = L["total"]; ct = C["total"]
        print(f"\n  LEADS ({lt}) | AI classified: {self._pct(L['ai'], lt)} | "
              f"No email: {self._pct(L['no_email'], lt)}")
        print(f"  LEAD CONTACTS ({ct}) | Social: {self._pct(C['social'], ct)} | "
              f"Email-checked: {self._pct(C['checked'], ct)} | Both: {self._pct(C['both'], ct)}")

        result = {
            "leads_total":             lt,
            "leads_ai_classified":     L["ai"],
            "leads_no_email":          L["no_email"],
            "contacts_total":          ct,
            "contacts_social":         C["social"],
            "contacts_email_checked":  C["checked"],
            "contacts_both":           C["both"],
        }
        self._write("lead-enrichment-funnel", result)
        return result

    def data_quality_report(self) -> dict:
        from concurrent.futures import ThreadPoolExecutor
        print("\n--- Data Quality Report ---")
        print("  [quality] scanning site_leads + site_contacts in parallel…")
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_leads    = pool.submit(self._scan_site_leads)
            f_contacts = pool.submit(self._scan_site_contacts)
            L = f_leads.result()
            C = f_contacts.result()

        lt = L["total"]; ct = C["total"]
        print(f"\n  SITE LEADS quality ({lt}) | "
              f"No sitemap: {self._pct(L['no_sitemap'], lt)} | "
              f"Zero pages: {self._pct(L['zero_pages'], lt)} | "
              f"Not classified: {self._pct(L['not_classified'], lt)}")
        print(f"  SITE CONTACTS quality ({ct}) | "
              f"No name: {self._pct(C['no_name'], ct)} | "
              f"Name mismatch: {self._pct(C['mismatch'], ct)}")

        result = {
            "leads_total":            lt,
            "leads_no_sitemap":       L["no_sitemap"],
            "leads_zero_pages":       L["zero_pages"],
            "leads_not_classified":   L["not_classified"],
            "contacts_total":         ct,
            "contacts_no_name":       C["no_name"],
            "contacts_name_mismatch": C["mismatch"],
        }
        self._write("data-quality", result)
        return result

    def email_contacts_funnel(self) -> dict:
        print("\n--- email_contacts Outreach Funnel ---")
        print("  [funnel] streaming email_contacts...")
        total = 0
        by_status: dict = {}; by_pipeline = {"site_only": 0, "leads_only": 0, "both": 0}
        by_email_type: dict = {}; by_priority: dict = {}; by_country: dict = {}

        gen = self.db.collection("email_contacts").stream()
        while True:
            try:    doc = next(gen)
            except StopIteration: break
            except (ValueError, AttributeError): continue
            try:    d = doc.to_dict() or {}
            except Exception: continue
            total += 1
            st = (d.get("status") or "pending").strip()
            by_status[st] = by_status.get(st, 0) + 1
            site  = bool(d.get("mark_site_leads"))
            leads = bool(d.get("mark_leads"))
            if site and leads: by_pipeline["both"] += 1
            elif site:         by_pipeline["site_only"] += 1
            elif leads:        by_pipeline["leads_only"] += 1
            et = (d.get("email_type") or "?").strip()
            by_email_type[et] = by_email_type.get(et, 0) + 1
            pr = str(d.get("outreach_priority") or "?")
            by_priority[pr] = by_priority.get(pr, 0) + 1
            cc = (d.get("country") or "?").strip().upper() or "?"
            by_country[cc] = by_country.get(cc, 0) + 1

        def pct(n): return f"{n:>6}  ({100*n//total if total else 0}%)"
        print(f"\n  EMAIL CONTACTS ({total} total)")
        print("  Status: "     + "  ".join(f"{k}={v}" for k, v in sorted(by_status.items(), key=lambda x: -x[1])))
        print("  Pipeline: "   + "  ".join(f"{k}={v}" for k, v in by_pipeline.items()))
        print("  Email type: " + "  ".join(f"{k}={v}" for k, v in sorted(by_email_type.items(), key=lambda x: -x[1])))
        print("  Priority: "   + "  ".join(f"p{k}={v}" for k, v in sorted(by_priority.items())))

        result = {
            "total":          total,
            "by_status":      by_status,
            "by_pipeline":    by_pipeline,
            "by_email_type":  by_email_type,
            "by_priority":    by_priority,
            "top_countries":  dict(sorted(by_country.items(), key=lambda x: -x[1])[:20]),
        }
        self._write("email-contacts-funnel", result)
        return result

    def pipeline_coverage(self) -> dict:
        print("\n--- Pipeline Cross-Coverage ---")
        print("  [coverage] streaming email_contacts...")
        total = site_only = leads_only = both = 0
        by_country: dict = {}

        gen = self.db.collection("email_contacts") \
            .select(["mark_site_leads", "mark_leads", "country"]).stream()
        while True:
            try:    doc = next(gen)
            except StopIteration: break
            except (ValueError, AttributeError): continue
            try:    d = doc.to_dict() or {}
            except Exception: continue
            total += 1
            site  = bool(d.get("mark_site_leads"))
            leads = bool(d.get("mark_leads"))
            cc = (d.get("country") or "?").strip().upper() or "?"
            if cc not in by_country:
                by_country[cc] = {"total": 0, "site": 0, "leads": 0, "both": 0}
            by_country[cc]["total"] += 1
            if site and leads: both       += 1; by_country[cc]["both"]  += 1
            elif site:         site_only  += 1; by_country[cc]["site"]  += 1
            elif leads:        leads_only += 1; by_country[cc]["leads"] += 1

        def pct(n, t): return f"({100*n//t if t else 0}%)"
        print(f"\n  PIPELINE COVERAGE ({total}) | "
              f"Site only:{site_only} {pct(site_only, total)} | "
              f"Leads only:{leads_only} {pct(leads_only, total)} | "
              f"Both:{both} {pct(both, total)}")
        for cc, c in sorted(by_country.items(), key=lambda x: -x[1]["total"])[:10]:
            print(f"    {cc:<5} total={c['total']:>5}  site={c['site']:>5}  "
                  f"leads={c['leads']:>5}  both={c['both']:>5}")

        result = {
            "total":          total,
            "site_only":      site_only,
            "leads_only":     leads_only,
            "both_pipelines": both,
            "by_country":     by_country,
        }
        self._write("pipeline-coverage", result)
        return result


    def campaign_statistics(self) -> dict:
        """Aggregate statistics across all campaign documents."""
        from collections import Counter
        print("\n--- Campaign Statistics ---")

        total = 0
        by_status:   Counter = Counter()
        by_source:   Counter = Counter()
        by_owner:    Counter = Counter()
        by_country:  Counter = Counter()
        total_contacts = 0
        total_sites    = 0
        contact_status: Counter = Counter()   # rolled-up status_breakdown across all campaigns

        for doc in self.db.collection("campaigns").stream():
            d = doc.to_dict() or {}
            total += 1
            by_status[d.get("status") or "draft"] += 1
            by_source[d.get("source") or "manual"] += 1
            if d.get("owner"):
                by_owner[d.get("owner")] += 1
            for c in (d.get("countries") or []):
                by_country[c.upper()] += 1
            total_contacts += int(d.get("contact_count") or 0)
            total_sites    += int(d.get("sites_count")   or 0)
            for st, cnt in (d.get("status_breakdown") or {}).items():
                contact_status[st] += int(cnt or 0)

        def pct(n, t): return f"{n}  ({100*n//t if t else 0}%)"
        print(f"  Total campaigns: {total}")
        print("  By status: " + "  ".join(f"{k}={v}" for k,v in by_status.most_common()))
        print(f"  Total contacts: {total_contacts}  Total sites: {total_sites}")

        result = {
            "total":            total,
            "total_contacts":   total_contacts,
            "total_sites":      total_sites,
            "by_status":        dict(by_status.most_common()),
            "by_source":        dict(by_source.most_common()),
            "by_owner":         dict(by_owner.most_common()),
            "top_countries":    dict(by_country.most_common(30)),
            "contact_status":   dict(contact_status.most_common()),
        }
        self._write("campaigns", result)
        return result

    def run_all(self, include_reasons: bool = True,
                no_writeback: bool = False) -> dict:
        """Run all aggregations and return combined results."""
        from maint_statistics import (
            summarise_country_pr_priority,
            summarise_reasons_count,
            _print_summary,
        )
        all_results: dict = {}

        print("\n--- Priority x Country ---")
        r = summarise_country_pr_priority(self.leads_col, self.stats_col)
        _print_summary(r)
        all_results["priority"] = r

        if include_reasons:
            print("\n--- Reasons Count ---")
            all_results["reasons"] = summarise_reasons_count(
                self.leads_col, self.stats_col, writeback=not no_writeback
            )

        all_results["leads_overview"]      = self.leads_overview()
        all_results["site_leads_overview"] = self.site_leads_overview()
        all_results["site_funnel"] = self.site_pipeline_enrichment_funnel()
        all_results["lead_funnel"] = self.lead_pipeline_enrichment_funnel()
        all_results["quality"]     = self.data_quality_report()
        all_results["email_funnel"]= self.email_contacts_funnel()
        all_results["coverage"]    = self.pipeline_coverage()
        all_results["campaigns"]    = self.campaign_statistics()
        return all_results
