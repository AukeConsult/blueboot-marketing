"""site_contact_check.py — Test contact scraping (emails, names, phones, AI) for one or more URLs.

Runs _extract_contacts (regex) and optionally _ai_extract_contacts (GPT) against
every contact page found on the site, then prints a combined result table.

Usage
-----
    python app/site_contact_check.py https://hammerfest.kommune.no
    python app/site_contact_check.py https://example.com https://other.no
    python app/site_contact_check.py https://example.com --ai          # also run AI extraction
    python app/site_contact_check.py --file urls.txt --ai
    python app/site_contact_check.py --campaign my-campaign --limit 10 --ai
    python app/site_contact_check.py --debug https://example.com       # show pages found
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "functions-crm"))
import _pathsetup  # noqa: F401

import aiohttp

from crm.campaign_scrape_lib import (
    BoundedFetcher,
    FETCH_TIMEOUT,
    MAX_BODY,
    _extract_contacts,
    _find_contact_links,
    _ai_extract_contacts,
)

_BROWSER_UA = "BlueBootLeadAgent/1.1 (+https://blueboot.ai)"


# ---------------------------------------------------------------------------
# Core per-URL logic
# ---------------------------------------------------------------------------

async def check_one(
    session: aiohttp.ClientSession,
    fetcher: BoundedFetcher,
    url: str,
    *,
    use_ai: bool = False,
    openai_client=None,
    model: str = "gpt-5.4-nano",
    debug: bool = False,
) -> dict:
    if not url.startswith("http"):
        url = "https://" + url

    t0 = time.monotonic()
    print(f"\n{'='*64}")
    print(f"  {url}")
    print(f"{'='*64}")

    # Fetch homepage
    hp = await fetcher.get(url, timeout=FETCH_TIMEOUT)
    if not hp:
        print("  homepage: EMPTY / failed")
        return {"url": url, "contacts": {}, "pages": 0, "elapsed": 0.0}

    # Find contact pages
    contact_links = _find_contact_links(hp, url)
    if debug:
        print(f"  homepage: {len(hp):,} chars")
        print(f"  contact pages found ({len(contact_links)}):")
        for lnk in contact_links:
            print(f"    {lnk}")

    # Fetch all contact pages concurrently
    pages: dict[str, str] = {url: hp}
    if contact_links:
        htmls = await asyncio.gather(
            *[fetcher.get(cu, timeout=FETCH_TIMEOUT) for cu in contact_links],
            return_exceptions=True,
        )
        for cu, html in zip(contact_links, htmls):
            if isinstance(html, str) and html:
                pages[cu] = html
                if debug:
                    print(f"    fetched {cu}: {len(html):,} chars")
            elif debug:
                print(f"    failed  {cu}")

    combined_html = "\n".join(pages.values())

    # ── Regex extraction ─────────────────────────────────────────────────────
    regex_contacts = _extract_contacts(combined_html)

    # ── AI extraction (optional) ─────────────────────────────────────────────
    # AI enriches the regex-found emails with better name/phone — never discovers new ones.
    ai_contacts: list[dict] = []
    if use_ai and openai_client and regex_contacts:
        emails_for_ai = list(regex_contacts.keys())
        print(f"  sending {len(emails_for_ai)} email(s) to AI for enrichment…")
        ai_contacts = await _ai_extract_contacts(
            combined_html, url, openai_client,
            emails=emails_for_ai, model=model,
        )

    # ── Merge: AI fills gaps, regex takes priority for emails it found ───────
    merged: dict[str, dict] = dict(regex_contacts)
    for c in ai_contacts:
        email = (c.get("email") or "").strip().lower()
        if not email:
            continue
        if email not in merged:
            merged[email] = {"name": c.get("name", ""), "phone": c.get("phone", "")}
        else:
            # fill empty fields from AI
            if not merged[email].get("name"):
                merged[email]["name"] = c.get("name", "")
            if not merged[email].get("phone"):
                merged[email]["phone"] = c.get("phone", "")

    elapsed = time.monotonic() - t0

    # ── Print results ─────────────────────────────────────────────────────────
    print(f"\n  Pages fetched : {len(pages)}")
    print(f"  Regex contacts: {len(regex_contacts)}")
    if use_ai:
        print(f"  AI contacts   : {len(ai_contacts)}")
    print(f"  Merged total  : {len(merged)}")
    print(f"  Elapsed       : {elapsed:.1f}s")

    if merged:
        print(f"\n  {'EMAIL':<40}  {'NAME':<25}  PHONE")
        print(f"  {'-'*40}  {'-'*25}  {'-'*15}")
        for email, info in sorted(merged.items()):
            name  = (info.get("name")  or "")[:25]
            phone = (info.get("phone") or "")[:20]
            src   = ""
            if email in regex_contacts and any(c.get("email") == email for c in ai_contacts):
                src = " [regex+AI]"
            elif email not in regex_contacts:
                src = " [AI only]"
            print(f"  {email:<40}  {name:<25}  {phone}{src}")
    else:
        print("  (no contacts found)")

    return {"url": url, "contacts": merged, "pages": len(pages), "elapsed": round(elapsed, 1)}


# ---------------------------------------------------------------------------
# Firestore loader (optional)
# ---------------------------------------------------------------------------

def _load_campaign_urls(campaign_id: str, limit: int) -> list[str]:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    try:
        from firestore_client import get_firestore
    except ImportError as exc:
        print(f"Cannot import firestore_client: {exc}", file=sys.stderr)
        sys.exit(1)
    db = get_firestore()
    col = db.collection("campaigns").document(campaign_id).collection("campaign_leads")
    urls = []
    for doc in col.stream():
        d = doc.to_dict() or {}
        u = (d.get("website") or "").strip()
        if u:
            urls.append(u)
        if len(urls) >= limit:
            break
    print(f"[contact-check] {len(urls)} leads from campaign '{campaign_id}'")
    return urls


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Test contact scraping (regex + optional AI) for one or more URLs"
    )
    ap.add_argument("urls", nargs="*", help="One or more URLs to check")
    ap.add_argument("--file", "-f", help="File with one URL per line")
    ap.add_argument("--campaign", "-c", metavar="CAMPAIGN_ID",
                    help="Read URLs from Firestore campaign_leads")
    ap.add_argument("--limit", "-n", type=int, default=20,
                    help="Max URLs when using --campaign (default 20)")
    ap.add_argument("--workers", "-w", type=int, default=3,
                    help="Concurrent site fetches (default 3)")
    ap.add_argument("--ai", action="store_true",
                    help="Also run AI extraction via OpenAI (needs OPENAI_API_KEY in .env)")
    ap.add_argument("--model", default="gpt-5.4-nano",
                    help="OpenAI model to use with --ai (default gpt-5.4-nano)")
    ap.add_argument("--debug", "-d", action="store_true",
                    help="Show each page URL and char count")
    args = ap.parse_args(argv)

    urls: list[str] = list(args.urls) if args.urls else []
    if args.file:
        for line in Path(args.file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    if args.campaign:
        urls += _load_campaign_urls(args.campaign, args.limit)
    if not urls:
        ap.print_help()
        sys.exit(0)

    # Build OpenAI client if needed
    openai_client = None
    if args.ai:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        import os
        key = os.getenv("OPENAI_API_KEY", "")
        if not key:
            print("[contact-check] WARNING: --ai set but OPENAI_API_KEY not found in .env — AI disabled")
            args.ai = False
        else:
            try:
                from openai import AsyncOpenAI
                openai_client = AsyncOpenAI(api_key=key)
                print(f"[contact-check] AI enabled  model={args.model}")
            except ImportError:
                print("[contact-check] WARNING: openai package not installed — AI disabled")
                args.ai = False

    async def run_all():
        connector = aiohttp.TCPConnector(limit=args.workers * 3, ssl=False)
        headers   = {"User-Agent": _BROWSER_UA, "Accept-Language": "en,no;q=0.9"}
        async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
            fetcher = BoundedFetcher(session, headers=headers)
            sem     = asyncio.Semaphore(args.workers)

            async def one(u):
                async with sem:
                    return await check_one(
                        session, fetcher, u,
                        use_ai=args.ai,
                        openai_client=openai_client,
                        model=args.model,
                        debug=args.debug,
                    )

            return await asyncio.gather(*[one(u) for u in urls])

    results = asyncio.run(run_all())

    # Summary
    print(f"\n{'='*64}")
    print(f"SUMMARY  ({len(results)} sites)")
    print(f"{'='*64}")
    print(f"  {'URL':<48}  {'contacts':>8}  {'pages':>5}  {'s':>5}")
    print(f"  {'-'*48}  {'-'*8}  {'-'*5}  {'-'*5}")
    total_contacts = 0
    for r in results:
        n = len(r["contacts"])
        total_contacts += n
        print(f"  {r['url']:<48}  {n:>8}  {r['pages']:>5}  {r['elapsed']:>5.1f}")
    print(f"\n  Total contacts found: {total_contacts}")


if __name__ == "__main__":
    main()
