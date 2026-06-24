"""campaign_scrape_emails.py -- Scrape contact emails from campaign_leads websites.

For each lead in campaigns/{campaign_id}/campaign_leads that has a website:
  1. Fetch the homepage
  2. Find and fetch the contact/kontakt page (if present)
  3. Extract all emails via extract_contacts()
  4. Write each found email as a new campaign_contacts doc:
       campaigns/{id}/campaign_contacts/{contact_id}
         lead_id, campaign_id, email, title, status="pending",
         scraped_at, found_on
  5. Mark email_scraped_at on the campaign_leads doc (used for --force skip)

Usage:
    python app/campaign_scrape_emails.py --campaign copenhagen
    python app/campaign_scrape_emails.py --campaign copenhagen --dry-run
    python app/campaign_scrape_emails.py --campaign copenhagen --workers 5 --force

Options:
    --campaign   Campaign ID (required)
    --workers N  Parallel async workers  (default: 6)
    --force      Re-scrape leads already scraped
    --dry-run    Print found emails without writing to Firestore
"""
from __future__ import annotations

import threading as _threading
_local_fb_lock = _threading.Lock()

import argparse
import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import aiohttp

import _pathsetup  # noqa: F401  -- sets Windows selector loop + sys.path
from functions.async_worker import BoundedFetcher, Worker, WorkerResult
from functions.utils import (
    extract_contacts,
    pair_names_to_contacts,
    pair_phones_to_contacts,
    domain_of,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CAMPAIGNS_COLLECTION  = "campaigns"
CAMPAIGN_LEADS_SUB    = "campaign_leads"
CAMPAIGN_CONTACTS_SUB = "campaign_contacts"

WORKERS_DEFAULT = 6
SITE_TIMEOUT    = 30.0   # hard ceiling per lead (homepage + contact page)
FETCH_TIMEOUT   = 12.0   # per individual HTTP fetch
WRITE_TIMEOUT   = 12.0   # Firestore write timeout

# Contact/about page keyword list (mirrors site_agent._CONTACT_WORDS)
_CONTACT_WORDS = [
    # English / global
    "contact", "about", "team", "people", "staff", "company",
    "reach", "connect", "enquiry", "inquiry", "get-in-touch",
    "reach-us", "our-team", "about-us", "meet-the-team",
    "support", "hello", "hire-us", "work-with-us",
    # Scandinavian
    "kontakt", "kontakta", "om-oss", "om oss", "ansatte", "selskapet",
    # Indian context
    "sampark", "hamare", "humse", "hum-se",
]
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# ID helpers (mirrors campaign_import_lib)
# ---------------------------------------------------------------------------

def _contact_id(email: str) -> str:
    """adrian@blisynlig.no -> adrian_blisynlig_no"""
    s = email.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")


# ---------------------------------------------------------------------------
# Firestore helpers
# ---------------------------------------------------------------------------

def _get_db():
    from dotenv import load_dotenv
    load_dotenv()
    import firebase_admin
    from firebase_admin import firestore
    import firebase_admin.credentials as fb_creds
    from functions.firebase_cred import get_firebase_cred
    cred = get_firebase_cred()
    with _local_fb_lock:
        if not firebase_admin._apps:
            firebase_admin.initialize_app(
                cred if isinstance(cred, fb_creds.Base) else fb_creds.Certificate(cred)
            )
    return firestore.client()


def _load_leads(db, campaign_id: str, force: bool) -> list[tuple[str, dict]]:
    """Return [(lead_id, data)] for leads that have a website and need scraping."""
    col = (
        db.collection(CAMPAIGNS_COLLECTION)
          .document(campaign_id)
          .collection(CAMPAIGN_LEADS_SUB)
    )
    results = []
    skipped = 0
    for doc in col.stream():
        d = doc.to_dict() or {}
        url = (d.get("website") or "").strip()
        if not url:
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


def _existing_contacts(db, campaign_id: str) -> set[str]:
    """Return set of existing campaign_contacts doc IDs (to detect new vs update)."""
    col = (
        db.collection(CAMPAIGNS_COLLECTION)
          .document(campaign_id)
          .collection(CAMPAIGN_CONTACTS_SUB)
    )
    return {doc.id for doc in col.select([]).stream()}


def _write_contacts(
    db,
    campaign_id: str,
    lead_id: str,
    emails: list[dict],
    existing: set[str],
    now: str,
) -> tuple[int, int]:
    """
    Upsert one campaign_contacts doc per email.
    Returns (new_count, updated_count).
    Protected fields (status, mail_sent, etc.) are never overwritten on existing docs.
    """
    _PROTECTED = {
        "status", "mail_sent", "next_mail_index", "in_reply_to",
        "followup_status", "followup_date", "followup_comment",
        "followup_importance", "followup_owner", "comment_history",
        "sent_at", "message_id", "sender_account", "created_at",
    }
    contacts_col = (
        db.collection(CAMPAIGNS_COLLECTION)
          .document(campaign_id)
          .collection(CAMPAIGN_CONTACTS_SUB)
    )
    leads_ref = (
        db.collection(CAMPAIGNS_COLLECTION)
          .document(campaign_id)
          .collection(CAMPAIGN_LEADS_SUB)
          .document(lead_id)
    )

    new_count = upd_count = 0
    batch = db.batch()

    for item in emails:
        cid = _contact_id(item["email"])
        doc = {
            "lead_id":     lead_id,
            "campaign_id": campaign_id,
            "email":       item["email"],
            "name":        item.get("name", ""),
            "title":       item.get("title", ""),
            "phone":       item.get("phone", ""),
            "found_on":    item.get("found_on", ""),
            "scraped_at":  now,
        }
        if cid in existing:
            # update only non-protected fields
            safe = {k: v for k, v in doc.items() if k not in _PROTECTED}
            batch.update(contacts_col.document(cid), safe)
            upd_count += 1
        else:
            doc["status"]     = "pending"
            doc["created_at"] = now
            batch.set(contacts_col.document(cid), doc)
            existing.add(cid)
            new_count += 1

    # Mark lead as scraped
    batch.update(leads_ref, {"email_scraped_at": now})
    batch.commit()
    return new_count, upd_count


# ---------------------------------------------------------------------------
# Contact-page discovery
# ---------------------------------------------------------------------------

def _find_contact_links(html: str, base_url: str, max_links: int = 5) -> list[str]:
    """Find contact/about page links — mirrors site_agent._find_contact_links."""
    dom = domain_of(base_url)
    links: list[str] = []
    for m in _HREF_RE.finditer(html):
        href = m.group(1).strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base_url, href).split("#")[0].split("?")[0]
        if domain_of(full) != dom:
            continue
        path = urlparse(full).path.lower()
        if any(w in path for w in _CONTACT_WORDS) and full not in links:
            links.append(full)
        if len(links) >= max_links:
            break
    return links


# ---------------------------------------------------------------------------
# Per-lead worker (isolated — CLAUDE.md rule)
# ---------------------------------------------------------------------------

class LeadEmailWorker(Worker):
    """Scrape one lead's website for contact emails."""

    def __init__(self, fetcher: BoundedFetcher, lead_id: str, data: dict,
                 *, timeout: float = SITE_TIMEOUT):
        super().__init__(lead_id, timeout=timeout)
        self._fetcher = fetcher
        self.lead_id  = lead_id
        self.data     = data
        self.website  = (data.get("website") or "").strip()
        self.found_emails: list[dict] = []

    async def process(self) -> WorkerResult:
        url = self.website
        if not url.startswith("http"):
            url = "https://" + url

        page_html_map: dict[str, str] = {}

        # 1. Homepage
        homepage_html = await self._fetcher.get(url, timeout=FETCH_TIMEOUT)
        if homepage_html:
            page_html_map[url] = homepage_html

        # 2. Contact/about pages
        for cu in _find_contact_links(homepage_html, url):
            html = await self._fetcher.get(cu, timeout=FETCH_TIMEOUT)
            if html:
                page_html_map[cu] = html

        if not page_html_map:
            return WorkerResult(self.worker_id, "error", error="no content fetched")

        all_html      = "\n".join(page_html_map.values())
        combined_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", all_html))

        # 3. Extract emails + enrich with names and phones
        country = self.data.get("country", "DK")
        contacts = extract_contacts(all_html, combined_text)
        if not contacts:
            self.found_emails = []
            return WorkerResult(self.worker_id, "ok")

        phones = pair_phones_to_contacts(contacts, all_html + " " + combined_text, country)
        names  = pair_names_to_contacts(contacts, all_html + " " + combined_text, all_html)

        def _found_on(email: str) -> str:
            for page_url, page_html in page_html_map.items():
                if email.lower() in page_html.lower():
                    return page_url
            return url

        self.found_emails = [
            {
                "email":    email,
                "title":    title,
                "name":     names.get(email, ""),
                "phone":    phones.get(email, ""),
                "found_on": _found_on(email),
            }
            for email, title in contacts.items()
        ]
        return WorkerResult(self.worker_id, "ok")


# ---------------------------------------------------------------------------
# Main async loop
# ---------------------------------------------------------------------------

async def _run(
    db,
    campaign_id: str,
    leads: list[tuple[str, dict]],
    *,
    dry_run: bool,
    workers: int,
) -> None:
    connector = aiohttp.TCPConnector(limit=workers * 2, ssl=False)
    headers   = {"User-Agent": _BROWSER_UA, "Accept-Language": "en,da;q=0.9"}
    now       = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Pre-load existing contacts to detect new vs update
    loop = asyncio.get_event_loop()
    existing: set[str] = await asyncio.wait_for(
        loop.run_in_executor(None, lambda: _existing_contacts(db, campaign_id)),
        timeout=20.0,
    )
    print(f"[scrape-emails] {len(existing)} existing campaign_contacts found", flush=True)

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        fetcher = BoundedFetcher(session, headers=headers)
        queue: asyncio.Queue = asyncio.Queue()

        for lead_id, data in leads:
            await queue.put((lead_id, data))
        SENTINEL = object()
        for _ in range(workers):
            await queue.put(SENTINEL)

        done = total_new = total_upd = errors = 0

        async def consumer():
            nonlocal done, total_new, total_upd, errors
            while True:
                item = await queue.get()
                try:
                    if item is SENTINEL:
                        break
                    lead_id, data = item
                    website = (data.get("website") or "").strip()

                    worker = LeadEmailWorker(fetcher, lead_id, data, timeout=SITE_TIMEOUT)
                    result = await worker.run()

                    emails = worker.found_emails
                    done += 1

                    if result.status != "ok":
                        errors += 1
                        print(f"  [{done}/{len(leads)}] {website[:55]:<55}  ✗ {result.error}", flush=True)
                    else:
                        print(f"  [{done}/{len(leads)}] {website[:55]:<55}  ✓ {len(emails)} email(s)", flush=True)
                        for e in emails:
                            tag = "exists" if _contact_id(e['email']) in existing else "NEW"
                            print(f"      → {e['email']}  [{tag}]", flush=True)

                    if not dry_run and emails:
                        new_c, upd_c = await asyncio.wait_for(
                            loop.run_in_executor(
                                None,
                                lambda _lid=lead_id, _em=emails: _write_contacts(
                                    db, campaign_id, _lid, _em, existing, now
                                ),
                            ),
                            timeout=WRITE_TIMEOUT,
                        )
                        total_new += new_c
                        total_upd += upd_c
                    elif not dry_run and not emails:
                        # Still mark lead as scraped (even if no emails found)
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

        consumer_tasks = [asyncio.create_task(consumer()) for _ in range(workers)]
        await asyncio.gather(*consumer_tasks)

    suffix = " (DRY RUN — nothing written)" if dry_run else f", {total_new} new contacts, {total_upd} updated"
    print(f"\n[scrape-emails] Done — {done} sites scraped, {errors} errors{suffix}", flush=True)
    return {"scraped": done, "new_contacts": total_new, "updated_contacts": total_upd, "errors": errors}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Scrape contact emails from campaign_leads websites into campaign_contacts.")
    ap.add_argument("--campaign", required=True, help="Campaign ID (e.g. 'copenhagen')")
    ap.add_argument("--workers",  type=int, default=WORKERS_DEFAULT,
                    help=f"Parallel async workers (default: {WORKERS_DEFAULT})")
    ap.add_argument("--force",    action="store_true",
                    help="Re-scrape leads already scraped")
    ap.add_argument("--dry-run",  action="store_true",
                    help="Print found emails without writing to Firestore")
    args = ap.parse_args(argv)

    print(f"[scrape-emails] campaign={args.campaign}  workers={args.workers}"
          f"  force={args.force}  dry_run={args.dry_run}", flush=True)

    db    = _get_db()
    leads = _load_leads(db, args.campaign, args.force)

    if not leads:
        print("[scrape-emails] Nothing to do.", flush=True)
        return

    asyncio.run(_run(db, args.campaign, leads, dry_run=args.dry_run, workers=args.workers))


if __name__ == "__main__":
    main()
