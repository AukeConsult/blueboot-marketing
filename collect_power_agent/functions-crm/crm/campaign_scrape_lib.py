"""campaign_scrape_lib.py -- Scrape contact emails from campaign_leads websites.

Called by the jobs worker (name="scrape-emails").
Self-contained -- no dependency on app/ so it deploys cleanly as a Cloud Function.
"""
from __future__ import annotations

import asyncio
import gzip as _gzip
import io as _io
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from crm.sitemap_reader import SitemapReader

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CAMPAIGNS_COLLECTION  = "campaigns"
CAMPAIGN_LEADS_SUB    = "campaign_leads"
CAMPAIGN_CONTACTS_SUB = "campaign_contacts"

SITE_TIMEOUT  = 180.0  # per-site budget: email scraping + sitemap reading run concurrently
FETCH_TIMEOUT = 12.0
WRITE_TIMEOUT = 12.0
MAX_BODY      = 4_000_000   # 4 MB cap per page

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_CONTACT_WORDS = [
    "contact", "about", "team", "people", "staff", "company",
    "reach", "connect", "enquiry", "inquiry", "get-in-touch",
    "reach-us", "our-team", "about-us", "meet-the-team",
    "support", "hello", "hire-us", "work-with-us",
    "kontakt", "kontakta", "om-oss", "om oss", "ansatte", "selskapet",
    "sampark", "hamare", "humse",
]

HREF_PATTERN  = r"""href=["']([^"']+)["']"""
EMAIL_PATTERN = r"""[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"""
MAILTO_PATTERN = r"""<a[^>]+href=["']mailto:([^"'> \s]+)["'][^>]*>([^<]{1,80})</a>"""

_HREF_RE   = re.compile(HREF_PATTERN, re.IGNORECASE)
_EMAIL_RE  = re.compile(EMAIL_PATTERN, re.IGNORECASE)
_MAILTO_RE = re.compile(MAILTO_PATTERN, re.IGNORECASE)

_ROLE_RE = re.compile(
    r"^(info|contact|hello|hi|support|help|service|sales|marketing|office|"
    r"mail|email|admin|noreply|no-reply|webmaster|postmaster|abuse|billing|"
    r"accounts|reception|enquiries|general|team|staff|hr|jobs|careers|"
    r"press|media|legal|privacy|security|feedback|newsletter|shop|orders|"
    r"booking|post|kontakt|salg|bestilling|faktura|kundeservice|drift)$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Inline BoundedFetcher / Worker / WorkerResult
# ---------------------------------------------------------------------------

@dataclass
class WorkerResult:
    worker_id: str
    status: str          # "ok" | "error" | "timeout"
    value: Any = None
    reason: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class BoundedFetcher:
    """Size-capped, never-raising HTTP GET."""

    def __init__(self, session: aiohttp.ClientSession, *, headers: dict | None = None):
        self._session = session
        self._headers = dict(headers or {})

    async def get(self, url: str, *, timeout: float = 15.0) -> str:
        try:
            async with self._session.get(
                url, headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True, ssl=False,
            ) as resp:
                if resp.status != 200:
                    return ""
                raw = await resp.content.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raw = raw[:MAX_BODY]
                if raw[:2] == b"\x1f\x8b":
                    try:
                        with _gzip.GzipFile(fileobj=_io.BytesIO(raw)) as gz:
                            raw = gz.read(MAX_BODY)
                    except Exception:
                        return ""
                return raw.decode("utf-8", errors="replace")[:MAX_BODY]
        except Exception:
            return ""


class Worker:
    def __init__(self, worker_id: str, *, timeout: float = 120.0):
        self.worker_id = worker_id
        self.timeout = timeout

    async def process(self) -> WorkerResult:
        raise NotImplementedError

    async def run(self) -> WorkerResult:
        try:
            return await asyncio.wait_for(self.process(), timeout=self.timeout)
        except asyncio.TimeoutError:
            return WorkerResult(self.worker_id, "timeout", reason="timeout")
        except Exception as exc:
            return WorkerResult(self.worker_id, "error", error=str(exc))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _domain_of(url: str) -> str:
    try:
        h = urlparse(url).hostname or ""
        return h.lower().lstrip("www.")
    except Exception:
        return ""


def _contact_id(email: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", email.lower().strip())
    return re.sub(r"_+", "_", s).strip("_")


def _extract_emails(html: str) -> dict:
    """Return {email: name} -- name from mailto anchors where available."""
    names: dict = {}
    for m in _MAILTO_RE.finditer(html):
        addr = m.group(1).strip().lower()
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))).strip()
        words = text.split()
        if 1 < len(words) <= 5 and all(w[0].isupper() for w in words if w.isalpha()):
            names[addr] = text

    decoded = re.sub(r"""\\u([0-9a-fA-F]{4})""", lambda m: chr(int(m.group(1), 16)), html)
    emails: dict = {}
    for e in _EMAIL_RE.findall(decoded):
        e = e.strip(".,;:()[]<>").lower()
        if not e or "@" not in e:
            continue
        local = e.split("@")[0]
        if _ROLE_RE.match(local):
            continue
        if len(local) >= 16 and re.fullmatch(r"[0-9a-f\-]+", local):
            continue
        if e not in emails:
            emails[e] = names.get(e, "")
    return emails


def _find_contact_links(html: str, base_url: str, max_links: int = 5) -> list:
    dom = _domain_of(base_url)
    links: list = []
    for m in _HREF_RE.finditer(html):
        href = m.group(1).strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base_url, href).split("#")[0].split("?")[0]
        if _domain_of(full) != dom:
            continue
        path = urlparse(full).path.lower()
        if any(w in path for w in _CONTACT_WORDS) and full not in links:
            links.append(full)
        if len(links) >= max_links:
            break
    return links


# ---------------------------------------------------------------------------
# Firestore helpers
# ---------------------------------------------------------------------------

def _load_leads(db, campaign_id: str, force: bool) -> list:
    col = (db.collection(CAMPAIGNS_COLLECTION)
             .document(campaign_id)
             .collection(CAMPAIGN_LEADS_SUB))
    results, skipped = [], 0
    for doc in col.stream():
        d = doc.to_dict() or {}
        if not (d.get("website") or "").strip():
            skipped += 1
            continue
        if (d.get("status") or "").lower() == "excluded":
            skipped += 1
            continue
        if not force and d.get("email_scraped_at"):
            skipped += 1
            continue
        results.append((doc.id, d))
    print(f"[scrape-emails] {len(results)} leads to scrape, {skipped} skipped", flush=True)
    return results


def _existing_contacts(db, campaign_id: str) -> set:
    col = (db.collection(CAMPAIGNS_COLLECTION)
             .document(campaign_id)
             .collection(CAMPAIGN_CONTACTS_SUB))
    return {doc.id for doc in col.select([]).stream()}


_PROTECTED = {
    "status", "mail_sent", "next_mail_index", "in_reply_to",
    "followup_status", "followup_date", "followup_comment",
    "followup_importance", "followup_owner", "comment_history",
    "sent_at", "message_id", "sender_account", "created_at",
}


def _write_contacts(db, campaign_id: str, lead_id: str,
                    emails: dict, existing: set, now: str) -> tuple:
    contacts_col = (db.collection(CAMPAIGNS_COLLECTION)
                      .document(campaign_id)
                      .collection(CAMPAIGN_CONTACTS_SUB))
    leads_ref = (db.collection(CAMPAIGNS_COLLECTION)
                   .document(campaign_id)
                   .collection(CAMPAIGN_LEADS_SUB)
                   .document(lead_id))
    batch = db.batch()
    new_count = upd_count = 0
    for email, name in emails.items():
        cid = _contact_id(email)
        doc = {"lead_id": lead_id, "campaign_id": campaign_id,
               "email": email, "name": name, "scraped_at": now}
        if cid in existing:
            batch.update(contacts_col.document(cid),
                         {k: v for k, v in doc.items() if k not in _PROTECTED})
            upd_count += 1
        else:
            doc.update({"status": "pending", "created_at": now})
            batch.set(contacts_col.document(cid), doc)
            existing.add(cid)
            new_count += 1
    batch.update(leads_ref, {"email_scraped_at": now})
    batch.commit()
    return new_count, upd_count


# ---------------------------------------------------------------------------
# Per-lead worker
# ---------------------------------------------------------------------------

class LeadEmailWorker(Worker):
    def __init__(self, session: aiohttp.ClientSession, fetcher: BoundedFetcher,
                 lead_id: str, data: dict, *, timeout: float = SITE_TIMEOUT):
        super().__init__(lead_id, timeout=timeout)
        self._session = session
        self._fetcher = fetcher
        self.data = data
        self.website = (data.get("website") or "").strip()
        self.found: dict = {}
        self.page_count: int | None = None

    async def process(self) -> WorkerResult:
        url = self.website
        if not url.startswith("http"):
            url = "https://" + url

        # Run email scraping and sitemap reading concurrently so neither
        # has to wait for the other — both complete within SITE_TIMEOUT.
        async def _scrape_emails() -> dict:
            pages: dict = {}
            hp = await self._fetcher.get(url, timeout=FETCH_TIMEOUT)
            if hp:
                pages[url] = hp
            for cu in _find_contact_links(hp, url):
                html = await self._fetcher.get(cu, timeout=FETCH_TIMEOUT)
                if html:
                    pages[cu] = html
            return _extract_emails("\n".join(pages.values())) if pages else {}

        async def _read_page_count() -> int | None:
            if self.data.get("page_count"):
                return None   # already set — skip
            try:
                pc, *_ = await asyncio.wait_for(
                    SitemapReader(self._session, url).read(),
                    timeout=120.0,  # hard cap: 18 candidate paths × HTML fallback worst case
                )
                return pc if pc > 0 else None
            except Exception:
                return None

        # return_exceptions=True ensures both sub-tasks always run to completion
        # independently — if one fails the other is NOT cancelled.
        results = await asyncio.gather(
            _scrape_emails(), _read_page_count(),
            return_exceptions=True,
        )

        emails = results[0] if isinstance(results[0], dict) else {}
        pc     = results[1] if isinstance(results[1], int)  else None

        if not emails and pc is None:
            err = str(results[0]) if isinstance(results[0], Exception) else "no content fetched"
            return WorkerResult(self.worker_id, "error", error=err)

        self.found      = emails
        self.page_count = pc
        return WorkerResult(self.worker_id, "ok")


# ---------------------------------------------------------------------------
# Async runner
# ---------------------------------------------------------------------------

async def _run(db, campaign_id: str, leads: list,
               *, dry_run: bool, workers: int) -> dict:
    loop = asyncio.get_event_loop()
    now  = datetime.now(timezone.utc).isoformat(timespec="seconds")

    existing: set = await asyncio.wait_for(
        loop.run_in_executor(None, lambda: _existing_contacts(db, campaign_id)),
        timeout=20.0,
    )
    print(f"[scrape-emails] {len(existing)} existing contacts", flush=True)

    connector = aiohttp.TCPConnector(limit=workers * 2, ssl=False)
    headers   = {"User-Agent": _BROWSER_UA, "Accept-Language": "en,da;q=0.9"}

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        fetcher = BoundedFetcher(session, headers=headers)
        queue: asyncio.Queue = asyncio.Queue()
        for item in leads:
            await queue.put(item)
        SENTINEL = object()
        for _ in range(workers):
            await queue.put(SENTINEL)

        done = total_new = total_upd = total_pages = errors = 0

        async def consumer():
            nonlocal done, total_new, total_upd, total_pages, errors
            while True:
                item = await queue.get()
                try:
                    if item is SENTINEL:
                        break
                    lead_id, data = item
                    worker = LeadEmailWorker(session, fetcher, lead_id, data)
                    result = await worker.run()
                    done += 1
                    site = (data.get("website") or "")[:55]
                    if result.status != "ok":
                        errors += 1
                        print(f"  [{done}/{len(leads)}] {site:<55}  x {result.error}", flush=True)
                    else:
                        print(f"  [{done}/{len(leads)}] {site:<55}  ok {len(worker.found)} email(s)", flush=True)
                        for e in worker.found:
                            tag = "exists" if _contact_id(e) in existing else "NEW"
                            print(f"      -> {e}  [{tag}]", flush=True)

                    if not dry_run and worker.page_count is not None:
                        await asyncio.wait_for(
                            loop.run_in_executor(
                                None,
                                lambda _lid=lead_id, _pc=worker.page_count: (
                                    db.collection(CAMPAIGNS_COLLECTION)
                                      .document(campaign_id)
                                      .collection(CAMPAIGN_LEADS_SUB)
                                      .document(_lid)
                                      .update({"page_count": _pc})
                                ),
                            ),
                            timeout=WRITE_TIMEOUT,
                        )
                        total_pages += 1
                        print(f"      -> page_count={worker.page_count}", flush=True)

                    if not dry_run and worker.found:
                        new_c, upd_c = await asyncio.wait_for(
                            loop.run_in_executor(
                                None,
                                lambda _lid=lead_id, _em=dict(worker.found): _write_contacts(
                                    db, campaign_id, _lid, _em, existing, now
                                ),
                            ),
                            timeout=WRITE_TIMEOUT,
                        )
                        total_new += new_c
                        total_upd += upd_c
                    elif not dry_run:
                        await asyncio.wait_for(
                            loop.run_in_executor(
                                None,
                                lambda _lid=lead_id: (
                                    db.collection(CAMPAIGNS_COLLECTION)
                                      .document(campaign_id)
                                      .collection(CAMPAIGN_LEADS_SUB)
                                      .document(_lid)
                                      .update({"email_scraped_at": now})
                                ),
                            ),
                            timeout=WRITE_TIMEOUT,
                        )
                except Exception as exc:
                    errors += 1
                    print(f"  [worker error] {exc}", flush=True)
                finally:
                    queue.task_done()

        await asyncio.gather(*[asyncio.create_task(consumer()) for _ in range(workers)])

    suffix = " (DRY RUN)" if dry_run else f", {total_new} new, {total_upd} updated"
    print(f"\n[scrape-emails] Done -- {done} sites, {errors} errors{suffix}", flush=True)
    return {"scraped": done, "new_contacts": total_new,
            "updated_contacts": total_upd, "pages_updated": total_pages,
            "errors": errors}


# ---------------------------------------------------------------------------
# Public entry point (called by jobs worker)
# ---------------------------------------------------------------------------

def run_campaign_scrape(
    db,
    campaign_id: str,
    *,
    force: bool = False,
    workers: int = 6,
    dry_run: bool = False,
) -> dict:
    """Scrape campaign_leads websites and write found emails to campaign_contacts."""
    leads = _load_leads(db, campaign_id, force)
    if not leads:
        return {"scraped": 0, "new_contacts": 0, "updated_contacts": 0, "errors": 0}

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            _run(db, campaign_id, leads, dry_run=dry_run, workers=workers)
        )
    finally:
        loop.close()

    return result or {"scraped": len(leads), "new_contacts": 0,
                      "updated_contacts": 0, "pages_updated": 0, "errors": 0}
