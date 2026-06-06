"""
crm/statistics_builder.py -- StatisticsBuilder for the Cloud Function environment.

Accepts an already-initialised Firestore client so it works inside both the
crmApi Cloud Function and local scripts.

Usage (inside main.py worker):
    from crm.statistics_builder import StatisticsBuilder
    sb = StatisticsBuilder(db=_get_db())
    sb.leads_overview()
    sb.site_leads_overview()
    sb.campaign_statistics()
    ...
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------

def stream_safe(query):
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


def count_by_field(db, col_name: str, field: str, limit: int = 5000) -> dict:
    counts: dict = {}
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

    def __init__(self, db, leads_collection: str = "leads",
                 stats_collection: str = "statistics"):
        self.db           = db
        self.leads_col    = leads_collection
        self.stats_col    = stats_collection
        self.generated_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

    # ── helpers ────────────────────────────────────────────────────────────

    def _pct(self, n: int, total: int) -> str:
        return f"{n:>6}  ({100 * n // total if total else 0}%)"

    def _write(self, doc_id: str, data: dict) -> None:
        self.db.collection(self.stats_col).document(doc_id).set(
            {**data, "generated_at": self.generated_at}, merge=True
        )
        print(f"  [stats] written → {self.stats_col}/{doc_id}", flush=True)

    # ── sub-scanners ───────────────────────────────────────────────────────

    def _leads_stats(self) -> dict:
        count       = sum(1 for _ in self.db.collection("leads").select([]).stream())
        by_country  = count_by_field(self.db, "leads", "country")
        by_priority = count_by_field(self.db, "leads", "priority")
        print(f"  {'Leads':<30} {count:>7}", flush=True)
        return {"count": count, "by_country": by_country, "by_priority": by_priority}

    def _leads_excluded_stats(self) -> dict:
        by_country: dict = {}; by_reason: dict = {}; count = 0
        for d, _ in stream_partitioned(
                self.db.collection("leads_excluded").select(["country", "reason"])):
            count += 1
            c = (d.get("country") or "?").strip().upper() or "?"
            r = (d.get("reason")  or "?").strip() or "?"
            by_country[c] = by_country.get(c, 0) + 1
            by_reason[r]  = by_reason.get(r, 0)  + 1
        print(f"  {'Leads excluded':<30} {count:>7}", flush=True)
        return {"count": count,
                "by_country": dict(sorted(by_country.items(), key=lambda x: -x[1])),
                "by_reason":  dict(sorted(by_reason.items(),  key=lambda x: -x[1]))}

    def _site_leads_stats(self) -> dict:
        count         = sum(1 for _ in self.db.collection("site_leads").select([]).stream())
        by_country    = count_by_field(self.db, "site_leads", "country")
        by_ai_country = count_by_field(self.db, "site_leads", "ai_country")
        by_ai_sector  = count_by_field(self.db, "site_leads", "ai_sector")
        page_buckets  = self._page_count_buckets()
        print(f"  {'Site leads':<30} {count:>7}", flush=True)
        return {"count": count, "by_country": by_country,
                "by_ai_country": by_ai_country, "by_ai_sector": by_ai_sector,
                "by_page_size": page_buckets}

    def _sites_excluded_stats(self) -> dict:
        by_country: dict = {}; by_reason: dict = {}; count = 0
        for d, _ in stream_partitioned(
                self.db.collection("sites_excluded").select(["country", "reason"])):
            count += 1
            c = (d.get("country") or "?").strip().upper() or "?"
            r = (d.get("reason")  or "?").strip() or "?"
            by_country[c] = by_country.get(c, 0) + 1
            by_reason[r]  = by_reason.get(r, 0)  + 1
        print(f"  {'Sites excluded':<30} {count:>7}", flush=True)
        return {"count": count,
                "by_country": dict(sorted(by_country.items(), key=lambda x: -x[1])),
                "by_reason":  dict(sorted(by_reason.items(),  key=lambda x: -x[1]))}

    def _page_count_buckets(self) -> dict:
        buckets = {"micro (1-50)": 0, "small (51-500)": 0, "medium (501-3k)": 0,
                   "large (3k-10k)": 0, "huge (10k-100k)": 0,
                   "ultra (100k+)": 0, "unknown": 0}
        for doc in self.db.collection("site_leads").select(["page_count"]).stream():
            try: pc = int((doc.to_dict() or {}).get("page_count") or 0)
            except (TypeError, ValueError): pc = 0
            if pc == 0:          buckets["unknown"]        += 1
            elif pc <= 50:       buckets["micro (1-50)"]   += 1
            elif pc <= 500:      buckets["small (51-500)"]  += 1
            elif pc <= 3000:     buckets["medium (501-3k)"] += 1
            elif pc <= 10000:    buckets["large (3k-10k)"]  += 1
            elif pc <= 100000:   buckets["huge (10k-100k)"] += 1
            else:                buckets["ultra (100k+)"]   += 1
        return buckets

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
            if ai:         r["ai"]   += 1
            if loc:        r["loc"]  += 1
            if ai and loc: r["both"] += 1
            if (d.get("sitemap_type") or "") in ("none", ""): r["no_sitemap"]      += 1
            if int(d.get("page_count") or 0) == 0:           r["zero_pages"]      += 1
            if not ai:                                         r["not_classified"]  += 1
        return r

    def _scan_site_contacts(self) -> dict:
        r = {"total": 0, "brave": 0, "checked": 0, "both": 0,
             "no_name": 0, "no_email": 0}
        for d, _ in stream_partitioned(
                self.db.collection_group("site_contacts")
                .select(["brave_enriched_at", "email_checked_at", "name", "email"])):
            r["total"] += 1
            brave = bool((d.get("brave_enriched_at") or "").strip())
            chkd  = bool((d.get("email_checked_at")  or "").strip())
            if brave:          r["brave"]   += 1
            if chkd:           r["checked"] += 1
            if brave and chkd: r["both"]    += 1
            if not (d.get("name")  or "").strip(): r["no_name"]  += 1
            if not (d.get("email") or "").strip(): r["no_email"] += 1
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

    # ── public methods ─────────────────────────────────────────────────────

    def leads_overview(self) -> dict:
        print("\n[stats] Lead pipeline overview", flush=True)
        result = {"generated_at": self.generated_at, "collections": {}}
        for name, fn in [("leads", self._leads_stats),
                         ("leads_excluded", self._leads_excluded_stats)]:
            try:    result["collections"][name] = fn()
            except Exception as e: result["collections"][name] = {"error": str(e)}
        n  = result["collections"].get("leads", {}).get("count", 0)
        nx = result["collections"].get("leads_excluded", {}).get("count", 0)
        result["exclusion_rate"] = int(100 * nx / (n + nx)) if (n + nx) else 0
        self._write("leads-overview", result)
        return result

    def site_leads_overview(self) -> dict:
        print("\n[stats] Site lead pipeline overview", flush=True)
        result = {"generated_at": self.generated_at, "collections": {}}
        for name, fn in [("site_leads", self._site_leads_stats),
                         ("sites_excluded", self._sites_excluded_stats)]:
            try:    result["collections"][name] = fn()
            except Exception as e: result["collections"][name] = {"error": str(e)}
        n  = result["collections"].get("site_leads", {}).get("count", 0)
        nx = result["collections"].get("sites_excluded", {}).get("count", 0)
        result["exclusion_rate"] = int(100 * nx / (n + nx)) if (n + nx) else 0
        self._write("site-leads-overview", result)
        return result

    def site_pipeline_enrichment_funnel(self) -> dict:
        from concurrent.futures import ThreadPoolExecutor
        print("\n[stats] Site enrichment funnel", flush=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            L = pool.submit(self._scan_site_leads).result()
            C = pool.submit(self._scan_site_contacts).result()
        result = {
            "leads_total": L["total"], "leads_ai_classified": L["ai"],
            "leads_location_enriched": L["loc"], "leads_both_enriched": L["both"],
            "contacts_total": C["total"], "contacts_brave_enriched": C["brave"],
            "contacts_email_checked": C["checked"], "contacts_fully_ready": C["both"],
            "contacts_no_name": C["no_name"], "contacts_no_email": C["no_email"],
        }
        self._write("site-enrichment-funnel", result)
        return result

    def lead_pipeline_enrichment_funnel(self) -> dict:
        from concurrent.futures import ThreadPoolExecutor
        print("\n[stats] Lead enrichment funnel", flush=True)
        def _scan_leads():
            r = {"total": 0, "ai": 0, "no_email": 0}
            for d, _ in stream_partitioned(
                    self.db.collection(self.leads_col)
                    .select(["ai_classified_at", "emails"])):
                r["total"] += 1
                if (d.get("ai_classified_at") or "").strip(): r["ai"] += 1
                if not (d.get("emails") or "").strip():       r["no_email"] += 1
            return r
        with ThreadPoolExecutor(max_workers=2) as pool:
            L = pool.submit(_scan_leads).result()
            C = pool.submit(self._scan_lead_contacts).result()
        result = {
            "leads_total": L["total"], "leads_ai_classified": L["ai"],
            "leads_no_email": L["no_email"], "contacts_total": C["total"],
            "contacts_social": C["social"], "contacts_email_checked": C["checked"],
            "contacts_both": C["both"],
        }
        self._write("lead-enrichment-funnel", result)
        return result

    def data_quality_report(self) -> dict:
        from concurrent.futures import ThreadPoolExecutor
        print("\n[stats] Data quality", flush=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            L = pool.submit(self._scan_site_leads).result()
            C = pool.submit(self._scan_site_contacts).result()
        result = {
            "leads_total": L["total"], "leads_no_sitemap": L["no_sitemap"],
            "leads_zero_pages": L["zero_pages"], "leads_not_classified": L["not_classified"],
            "contacts_total": C["total"], "contacts_no_name": C["no_name"],
            "contacts_name_mismatch": 0,   # email_matches_name not available in CF env
        }
        self._write("data-quality", result)
        return result

    def email_contacts_funnel(self) -> dict:
        print("\n[stats] email_contacts funnel", flush=True)
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
        result = {"total": total, "by_status": by_status, "by_pipeline": by_pipeline,
                  "by_email_type": by_email_type, "by_priority": by_priority,
                  "top_countries": dict(sorted(by_country.items(), key=lambda x: -x[1])[:30])}
        self._write("email-contacts-funnel", result)
        return result

    def pipeline_coverage(self) -> dict:
        print("\n[stats] Pipeline coverage", flush=True)
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
            if site and leads: both += 1; by_country[cc]["both"]  += 1
            elif site:         site_only += 1; by_country[cc]["site"]  += 1
            elif leads:        leads_only += 1; by_country[cc]["leads"] += 1
        result = {"total": total, "site_only": site_only, "leads_only": leads_only,
                  "both_pipelines": both, "by_country": by_country}
        self._write("pipeline-coverage", result)
        return result

    def campaign_statistics(self) -> dict:
        print("\n[stats] Campaign statistics", flush=True)
        total = 0
        by_status:  Counter = Counter()
        by_source:  Counter = Counter()
        by_owner:   Counter = Counter()
        by_country: Counter = Counter()
        total_contacts = 0; total_sites = 0
        contact_status: Counter = Counter()
        for doc in self.db.collection("campaigns").stream():
            d = doc.to_dict() or {}
            total += 1
            by_status[d.get("status") or "draft"] += 1
            by_source[d.get("source") or "manual"] += 1
            if d.get("owner"): by_owner[d.get("owner")] += 1
            for c in (d.get("countries") or []): by_country[c.upper()] += 1
            total_contacts += int(d.get("contact_count") or 0)
            total_sites    += int(d.get("sites_count")   or 0)
            for st, cnt in (d.get("status_breakdown") or {}).items():
                contact_status[st] += int(cnt or 0)
        result = {
            "total": total, "total_contacts": total_contacts, "total_sites": total_sites,
            "by_status":      dict(by_status.most_common()),
            "by_source":      dict(by_source.most_common()),
            "by_owner":       dict(by_owner.most_common()),
            "top_countries":  dict(by_country.most_common(30)),
            "contact_status": dict(contact_status.most_common()),
        }
        self._write("campaigns", result)
        return result
