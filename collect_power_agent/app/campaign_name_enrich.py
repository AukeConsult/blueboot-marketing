"""campaign_name_enrich.py -- Fill missing contact names using email pattern rules
and (optionally) OpenAI for ambiguous cases.

Targets:
  --campaign CAMPAIGN_ID   — enrich campaign_contacts for that campaign
  --all                    — enrich all email_contacts

Rule-based extraction handles common patterns:
  john.doe@…           → John Doe      (high confidence)
  john_doe@…           → John Doe      (high confidence)
  john-doe@…           → John Doe      (high confidence)
  j.doe@…              → (ambiguous → AI)
  johnd@…              → (ambiguous → AI)
  info/contact/admin@… → skip (role address)

AI (GPT) is called in batches for ambiguous cases: given (email, domain),
it returns the most likely full name or null.

Writes back to:
  - campaigns/{id}/campaign_contacts/{doc_id}.name
  - email_contacts/{doc_id}.name   (keeps collections in sync)

Usage:
    python app/campaign_name_enrich.py --campaign MY_CAMPAIGN_ID
    python app/campaign_name_enrich.py --campaign MY_CAMPAIGN_ID --dry-run
    python app/campaign_name_enrich.py --campaign MY_CAMPAIGN_ID --skip-ai
    python app/campaign_name_enrich.py --all          # all campaigns
    python app/campaign_name_enrich.py --all --dry-run --skip-ai
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import threading as _threading

_local_fb_lock = _threading.Lock()

import _pathsetup  # noqa: F401

# ── Role / generic addresses — never yield a name ─────────────────────────────
ROLE_PREFIXES = {
    # ── English ────────────────────────────────────────────────────────────────
    "info", "contact", "hello", "hi", "hey", "support", "help", "service",
    "sales", "marketing", "office", "mail", "email", "admin", "administrator",
    "noreply", "no-reply", "webmaster", "hostmaster", "postmaster",
    "abuse", "billing", "accounts", "reception", "enquiries", "enquiry",
    "general", "team", "staff", "hr", "jobs", "careers", "press", "media",
    "legal", "privacy", "security", "feedback", "newsletter", "news",
    "shop", "store", "orders", "booking", "bookings", "reservations",
    "customercare", "customers", "client", "clients", "post", "invoice",
    "invoice", "purchase", "procurement", "it", "helpdesk", "desk",
    # ── Norwegian / Danish / Swedish ───────────────────────────────────────────
    "post", "hjelp", "kundeservice", "kundservice", "salg", "kontakt",
    "bestilling", "ordre", "faktura", "butikk", "butik", "butiken",
    "kundtjanst", "kundetjeneste", "henvendelse", "support", "drift",
    # ── German ────────────────────────────────────────────────────────────────
    "kontakt", "bestellung", "anfrage", "kundenservice", "verwaltung",
    "buchhaltung", "versand", "einkauf", "vertrieb", "sekretariat",
    "empfang", "buchhaltung", "rechnung", "lager", "recht", "presse",
    # ── Dutch ─────────────────────────────────────────────────────────────────
    "bestelling", "klantenservice", "winkel", "inkoop", "verkoop",
    "boekhouding", "facturatie", "ontvangst", "klant", "klanten",
    # ── French ────────────────────────────────────────────────────────────────
    "commande", "aide", "client", "clients", "boutique", "comptabilite",
    "facturation", "accueil", "direction", "secretariat", "juridique",
    # ── Spanish / Portuguese ──────────────────────────────────────────────────
    "correo", "contacto", "soporte", "pedido", "factura", "tienda",
    "atencion", "ventas", "compras", "administracion", "contato",
    "suporte", "pedidos", "atendimento",
    # ── Finnish ───────────────────────────────────────────────────────────────
    "tilaukset", "asiakaspalvelu", "myynti", "laskutus", "tuki",
}

# ── Separators in the local part ─────────────────────────────────────────────
_SEP_RE = re.compile(r"[._\-+]")


def _extract_local(email: str) -> str:
    """Return the local part of an email (before @), lowercased."""
    local = email.split("@")[0].lower().strip()
    # strip numeric suffixes: john.doe1988 → john.doe
    local = re.sub(r"\d+$", "", local)
    return local


def _is_role(local: str) -> bool:
    parts = _SEP_RE.split(local)
    base = parts[0] if parts else local
    return base in ROLE_PREFIXES or local in ROLE_PREFIXES


def _looks_like_name_part(s: str) -> bool:
    """Return True if the string looks like a real name word (≥2 letters, no digits)."""
    return bool(re.match(r"^[a-z]{2,}$", s))


def extract_name_rule_based(email: str) -> tuple[str | None, str]:
    """Attempt to extract a name from the email local part.

    Returns (name_or_None, confidence) where confidence is:
      'high'      — two full name parts found (first + last)
      'ambiguous' — only one part or short initials (send to AI)
      'skip'      — role address or unresolvable
    """
    local = _extract_local(email)
    if not local or _is_role(local):
        return None, "skip"

    parts = [p for p in _SEP_RE.split(local) if p]

    # Filter out purely numeric or single-char parts used as separators
    name_parts = [p for p in parts if _looks_like_name_part(p)]

    if len(name_parts) >= 2:
        # e.g. john.doe → John Doe, or john.e.doe → John Doe (skip middle initial)
        first = name_parts[0]
        last  = name_parts[-1]
        if first != last:
            # Guard: never use the domain name as a surname
            domain_base = _extract_local(email.split("@")[1]) if "@" in email else ""
            if last.lower() == domain_base.lower():
                return None, "ambiguous"
            return f"{first.capitalize()} {last.capitalize()}", "high"

    if len(name_parts) == 1 and len(name_parts[0]) >= 4:
        # e.g. "johndoe" — could be one word but ambiguous
        return None, "ambiguous"

    # Single short part, initials, etc.
    return None, "ambiguous"


def _doc_id_from_email(email: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", email.strip().lower())


def _get_db():
    try:
        from app.firestore_client import get_firestore
    except ImportError:
        from firestore_client import get_firestore
    return get_firestore()


# ── Bing search for a person's name from their email ─────────────────────────

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Matches a person name at start or after separator: "John Doe - CEO | LinkedIn"
_NAME_IN_TITLE_RE = re.compile(
    r"""(?:^|[\|\-–—·•,])\s*([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20}){1,3})""",
    re.MULTILINE,
)

# Matches a job title after name separator: "John Doe - CEO at Acme" or "- Head of Marketing"
_TITLE_IN_RESULT_RE = re.compile(
    r"""[\|\-–—·•]\s*([A-Z][A-Za-z &/,]{3,50?})(?:\s+(?:at|@|of|for|in)\s+|\s*[\|\-–—]|\s*$)""",
    re.MULTILINE,
)

# Job title keywords that help identify a title fragment
_TITLE_KEYWORDS = {
    "ceo", "cto", "cfo", "coo", "cmo", "founder", "co-founder",
    "director", "manager", "head", "lead", "chief", "vp", "vice",
    "president", "partner", "owner", "principal", "engineer", "developer",
    "consultant", "advisor", "specialist", "analyst", "architect",
    "officer", "executive", "account", "sales", "marketing", "product",
    "customer", "success", "operations", "senior", "junior", "associate",
}


def _looks_like_job_title(s: str) -> bool:
    words = s.lower().split()
    return any(w in _TITLE_KEYWORDS for w in words)


async def _bing_search_name(session, email: str, domain: str) -> dict:
    """Search Bing for the person behind an email — two-pass strategy.

    Pass 1 — EXACT: search for the literal email address in quotes.
      Only accepts results where the email appears in the snippet (strongest proof).

    Pass 2 — DOMAIN: only runs when Pass 1 finds nothing.
      Searches for the local-part name on the company site:
        "<firstname>" site:domain.com   →  finds About/Team/Contact pages
      Accepts results where the domain appears AND a candidate full name
      starts with the local-part first name. Lower confidence — flagged so
      AI knows the email was not explicitly confirmed.

    Returns dict with optional keys: 'context_chunks', 'name', 'title'.
    """
    import xml.etree.ElementTree as _ET

    local = email.split("@")[0] if "@" in email else email

    async def _fetch_rss(query: str, count: int = 8) -> list:
        """Fetch Bing RSS results.
        Returns list of (page_title, snippet, combined, url).
        """
        try:
            async with session.get(
                "https://www.bing.com/search",
                params={"q": query, "format": "rss", "count": count},
                headers={"User-Agent": _BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
            ) as resp:
                raw = await resp.content.read(500_001)
            text = raw[:500_000].decode("utf-8", errors="replace")
            root = _ET.fromstring(text)
            items = []
            for item in root.findall(".//item"):
                title_el = item.find("title")
                desc_el  = item.find("description")
                link_el  = item.find("link")
                pt  = (title_el.text or "").strip() if title_el is not None else ""
                snp = (desc_el.text  or "").strip() if desc_el  is not None else ""
                url = (link_el.text  or "").strip() if link_el  is not None else ""
                items.append((pt, snp, pt + " | " + snp, url))
            return items
        except Exception:
            return []

    result: dict = {}

    # ── Pass 1: exact email in quotes ────────────────────────────────────────
    for pt, snp, combined, url in await _fetch_rss(f'"{email}"'):
        idx = combined.lower().find(email.lower())
        if idx < 0:
            continue   # email not in snippet — no verified link

        ctx_start = max(0, idx - 500)
        ctx_end   = min(len(combined), idx + len(email) + 500)
        result.setdefault("context_chunks", []).append(
            combined[ctx_start:ctx_end].strip()
        )
        if "name" not in result:
            m = _NAME_IN_TITLE_RE.search(combined)
            if m:
                cand = m.group(1).strip()
                if len(cand.split()) <= 4 and len(cand) <= 40:
                    result["name"] = cand
        if "title" not in result:
            for m in _TITLE_IN_RESULT_RE.finditer(combined):
                cand = m.group(1).strip().strip(",")
                if _looks_like_job_title(cand) and len(cand) <= 60:
                    result["title"] = cand
                    break
        if "name" in result and "title" in result and len(result.get("context_chunks", [])) >= 3:
            break

    if result.get("context_chunks"):
        print(f"             pass1 ✓ exact email found in snippet", flush=True)
        return result   # Pass 1 succeeded — return strong evidence

    print(f"             pass1 – no snippet with email → trying pass2 (domain search)…", flush=True)
    # ── Pass 2: firstname + domain ───────────────────────────────────────────
    # The email was scraped FROM the company site, so a page there mentions
    # this person. We try two queries — site: first (precise), then keyword
    # (broader fallback for domains Bing doesn't index with site:).
    #
    # Verification uses the RESULT URL: if the URL contains the domain, the
    # result is from that site regardless of what the snippet text says.
    domain_lo = domain.lower()

    def _from_domain(url: str) -> bool:
        """True if the result URL is from our domain."""
        try:
            from urllib.parse import urlparse
            host = urlparse(url).netloc.lower().lstrip("www.")
            return host == domain_lo or host.endswith("." + domain_lo)
        except Exception:
            return domain_lo in url.lower()

    def _try_extract(combined: str, url: str) -> bool:
        """Try to extract a name+context from this result. Returns True if found."""
        for m in _NAME_IN_TITLE_RE.finditer(combined):
            cand = m.group(1).strip()
            if len(cand.split()) < 2 or len(cand) > 40:
                continue
            if not cand.lower().startswith(local.lower()):
                continue   # name must start with the email local part
            idx = combined.find(m.group(1))
            ctx_start = max(0, idx - 150)
            ctx_end   = min(len(combined), idx + len(m.group(1)) + 150)
            chunk = combined[ctx_start:ctx_end].strip()
            result.setdefault("context_chunks", []).append(
                f"[domain search — email not explicitly confirmed] {chunk}"
            )
            result["name"]           = cand
            result["low_confidence"] = True
            return True
        return False

    # Query A: site: operator (precise — only pages on the domain)
    for pt, snp, combined, url in await _fetch_rss(f'"{local}" site:{domain}', count=8):
        if not (_from_domain(url) or domain_lo in combined.lower()):
            continue
        if _try_extract(combined, url):
            if "title" not in result:
                for m in _TITLE_IN_RESULT_RE.finditer(combined):
                    cand = m.group(1).strip().strip(",")
                    if _looks_like_job_title(cand) and len(cand) <= 60:
                        result["title"] = cand
                        break
            print(f"             pass2a ✓ (site:) name '{result['name']}' found", flush=True)
            break

    # Query B: keyword fallback — broader, catches domains Bing won't site:-index
    if not result.get("context_chunks"):
        for pt, snp, combined, url in await _fetch_rss(f'"{local}" "{domain}"', count=8):
            if not (_from_domain(url) or domain_lo in combined.lower()):
                continue
            if _try_extract(combined, url):
                if "title" not in result:
                    for m in _TITLE_IN_RESULT_RE.finditer(combined):
                        cand = m.group(1).strip().strip(",")
                        if _looks_like_job_title(cand) and len(cand) <= 60:
                            result["title"] = cand
                            break
                print(f"             pass2b ✓ (keyword) name '{result['name']}' found", flush=True)
                break

    if not result.get("context_chunks"):
        print(f"             pass2 – nothing found", flush=True)

    # ── Pass 3: Brave Search API ──────────────────────────────────────────────
    # Runs only when both Bing passes found nothing.
    # Brave has a proper JSON API and often indexes pages Bing misses.
    if not result.get("context_chunks"):
        brave_result = await _brave_search_name(session, email, domain, local)
        if brave_result.get("context_chunks"):
            return brave_result
        print(f"             pass3 – nothing found", flush=True)

    return result


async def _brave_search_name(session, email: str, domain: str, local: str) -> dict:
    """Search Brave for the person behind an email.

    Uses the Brave Web Search API (JSON, not RSS) — richer results than Bing.
    Requires BRAVE_API_KEY in .env.
    Tries exact-email query first, then domain-scoped queries.
    """
    try:
        from app.functions.config import cfg as _cfg
    except ImportError:
        from functions.config import cfg as _cfg

    api_key = (_cfg.BRAVE_API_KEY or "").strip()
    if not api_key:
        print(f"             pass3 skip — no BRAVE_API_KEY in .env", flush=True)
        return {}

    domain_lo = domain.lower()

    def _from_domain(url: str) -> bool:
        try:
            from urllib.parse import urlparse as _up
            host = _up(url).netloc.lower().lstrip("www.")
            return host == domain_lo or host.endswith("." + domain_lo)
        except Exception:
            return domain_lo in url.lower()

    async def _brave_fetch(query: str) -> list:
        """Call Brave API; return list of (title, description, url)."""
        try:
            async with session.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": 8, "safesearch": "off"},
                headers={
                    "Accept":               "application/json",
                    "Accept-Encoding":      "gzip",
                    "X-Subscription-Token": api_key,
                },
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
            return [
                (r.get("title", ""), r.get("description", ""), r.get("url", ""))
                for r in data.get("web", {}).get("results", [])
            ]
        except Exception:
            return []

    result: dict = {}

    def _try_extract(title: str, desc: str, url: str, require_email: bool) -> bool:
        combined = title + " | " + desc
        combined_lo = combined.lower()

        if require_email:
            idx = combined_lo.find(email.lower())
            if idx < 0:
                return False
            ctx_start = max(0, idx - 500)
            ctx_end   = min(len(combined), idx + len(email) + 500)
            result.setdefault("context_chunks", []).append(
                combined[ctx_start:ctx_end].strip()
            )
            if "name" not in result:
                m = _NAME_IN_TITLE_RE.search(combined)
                if m:
                    cand = m.group(1).strip()
                    if len(cand.split()) <= 4 and len(cand) <= 40:
                        result["name"] = cand
        else:
            # Domain search — name must start with local part
            if not (_from_domain(url) or domain_lo in combined_lo):
                return False
            for m in _NAME_IN_TITLE_RE.finditer(combined):
                cand = m.group(1).strip()
                if len(cand.split()) < 2 or len(cand) > 40:
                    continue
                if not cand.lower().startswith(local.lower()):
                    continue
                idx = combined.find(m.group(1))
                ctx_start = max(0, idx - 400)
                ctx_end   = min(len(combined), idx + len(m.group(1)) + 400)
                result.setdefault("context_chunks", []).append(
                    f"[domain search — email not explicitly confirmed] "
                    f"{combined[ctx_start:ctx_end].strip()}"
                )
                result["name"]           = cand
                result["low_confidence"] = True
                break
            if not result.get("context_chunks"):
                return False

        # Extract job title if not already found
        combined = title + " | " + desc
        if "title" not in result:
            for m in _TITLE_IN_RESULT_RE.finditer(combined):
                cand = m.group(1).strip().strip(",")
                if _looks_like_job_title(cand) and len(cand) <= 60:
                    result["title"] = cand
                    break
        return bool(result.get("context_chunks"))

    # Query A: exact email (strongest)
    print(f"             pass3 Brave exact…", flush=True)
    for title, desc, url in await _brave_fetch(f'"{email}"'):
        if _try_extract(title, desc, url, require_email=True):
            print(f"             pass3 ✓ Brave exact — name: {result.get('name', '?')}", flush=True)
            return result

    # Query B: site: scoped to domain
    print(f"             pass3 Brave site:{domain}…", flush=True)
    for title, desc, url in await _brave_fetch(f'"{local}" site:{domain}'):
        if _try_extract(title, desc, url, require_email=False):
            print(f"             pass3 ✓ Brave site: — name: {result.get('name', '?')}", flush=True)
            return result

    # Query C: keyword fallback
    print(f"             pass3 Brave keyword…", flush=True)
    for title, desc, url in await _brave_fetch(f'"{local}" "{domain}"'):
        if _try_extract(title, desc, url, require_email=False):
            print(f"             pass3 ✓ Brave keyword — name: {result.get('name', '?')}", flush=True)
            return result

    return result


# ── AI name resolution ────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a contact data verification expert. Your job is to determine
whether a given email address can be VERIFIABLY linked to a specific real person.

CORE RULE — PERSON-CENTRIC:
The name you return must belong to THIS SPECIFIC email address. You need evidence.
Do NOT guess a name just because the email local part resembles one. Guessing
"John" for john@company.com is WRONG unless you can verify this John at this company.

WHAT COUNTS AS EVIDENCE (use if available):
1. verified_context contains the exact email address — strongest proof. A name near
   the email on that page belongs to this person.
   Example: "Contact: Jane Smith <jane@acme.com>, Head of Sales" → "Jane Smith"

2. verified_context starts with "[domain search — email not explicitly confirmed]" —
   this means we found the name on the company's own website (About/Team page) and
   the name starts with the email's local part. This is MODERATE evidence.
   Accept ONLY if: the name clearly starts with the email local part AND there is no
   other plausible candidate on the same page. Be conservative — return null if uncertain.
   Example: email is john@acme.com, context shows "John Smith — CEO | acme.com" → "John Smith"
   But if context shows "John Adams and John Baker" → null (ambiguous)

3. Email local part is an unambiguous full name: first.lastname@… → safe to infer
   (e.g. john.doe@acme.com → "John Doe" because both first AND last name are present)

4. suggested field matches the evidence from verified_context → confirm it

WHAT IS NOT EVIDENCE (return null):
- Email has only a first name: john@company.com → null (which John?)
- Email has only initials: j.s@company.com → null
- No web snippet, and the local part alone doesn't contain a clear first+last name
- The snippet mentions a company but not a specific person at that email
- Role/department emails in ANY language: info, contact, post, support, sales, hjelp,
  kontakt, bestellung, kundenservice, klantenservice, commande, soporte, suporte,
  bestilling, ordre, faktura, salg, drift, tjeneste → always null

DOMAIN-AS-SURNAME RULE:
NEVER use the company/domain name as a surname.
john@acme.com → NOT "John Acme". If you can't find the surname → null.

FORMAT: Return null for any uncertain case. A wrong name is worse than no name.
Return ONLY valid JSON — each email maps to an object with "name" and "title",
or null if the name cannot be verified:
{"email@domain.com": {"name": "Full Name", "title": "CEO"}, "other@domain.com": null}
"title" is optional — omit or set to null if not found in context.
Never invent a title; only return one if it appears in the verified_context."""


async def _ai_resolve_batch(
    client,
    model: str,
    contacts: list[dict],   # [{email, domain, doc_id, snippet?}, ...]
    _debug: bool = False,
) -> dict[str, str]:
    """Send a batch of contacts to GPT with web snippets and return {doc_id: name}."""
    items = [
        {
            "email":     c["email"],
            "domain":    c.get("domain", ""),
            "suggested": c.get("rule_name", ""),   # name hint from email pattern (needs validation)
            # verified_context: text from a web page found by Bing.
            # If it starts with "[domain search — email not explicitly confirmed]",
            # the email was not on the page directly — treat as moderate evidence only.
            "verified_context": c.get("context", ""),
        }
        for c in contacts
    ]
    user_msg = (
        "Resolve names for these contacts. "
        "Use verified_context as PRIMARY evidence when present — it is raw text "
        "from a web page that contained the exact email address. "
        "Return valid JSON.\n"
        + json.dumps(items, ensure_ascii=False)
    )
    # Debug: show exactly what is sent to the AI
    if _debug:
        print("  ── [DEBUG] → AI input ────────────────────────────────────────", flush=True)
        for item in items:
            has_ctx  = bool(item.get("verified_context"))
            ctx_preview = (item.get("verified_context") or "")[:600].replace("\n", " ")
            sugg = item.get("suggested") or "(none)"
            ctx_line = ("YES — " + ctx_preview) if has_ctx else "(none — AI has no web evidence)"
            print(f"    {item['email']}", flush=True)
            print(f"      suggested : {sugg}", flush=True)
            print(f"      context   : {ctx_line}", flush=True)
        print("  ─────────────────────────────────────────────────────────────", flush=True)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.1,
            max_tokens=4000,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        # Debug: show what AI returned for each contact
        if _debug:
            print("  ── [DEBUG] ← AI response ─────────────────────────────────────", flush=True)
            for c in contacts:
                val = data.get(c["email"])
                if isinstance(val, dict):
                    ai_name  = (val.get("name")  or "").strip()
                    ai_title = (val.get("title") or "").strip()
                    if ai_name:
                        suffix = f"  [{ai_title}]" if ai_title else ""
                        print(f"    {c['email']:45s} → {ai_name}{suffix}", flush=True)
                    else:
                        print(f"    {c['email']:45s} → null (no verifiable evidence)", flush=True)
                else:
                    reason = "(null — no verifiable evidence)" if not val else "(unexpected format)"
                    print(f"    {c['email']:45s} → null {reason}", flush=True)
            print("  ─────────────────────────────────────────────────────────────", flush=True)
        # Map back to doc_id → {name, title?}
        out: dict[str, dict] = {}
        for c in contacts:
            val = data.get(c["email"])
            if isinstance(val, dict):
                ai_name  = (val.get("name")  or "").strip()
                ai_title = (val.get("title") or "").strip()
                if ai_name and len(ai_name) > 2:
                    entry: dict = {"name": ai_name.title()}
                    if ai_title:
                        entry["title"] = ai_title
                    out[c["doc_id"]] = entry
            elif isinstance(val, str) and len(val.strip()) > 2:
                # backwards-compat if model returns plain string
                out[c["doc_id"]] = {"name": val.strip().title()}
        return out
    except Exception as exc:
        print(f"  [ai] batch failed: {exc}", flush=True)
        return {}


# ── Main enrichment logic ─────────────────────────────────────────────────────

async def _enrich(
    db,
    contacts: list[dict],   # [{doc_id, email, domain, campaign_doc_ref?, ec_doc_id?}]
    dry_run: bool,
    skip_ai: bool,
    model: str,
    batch_size: int = 5,
    skip_ec_lookup: bool = False,   # True when contacts already come from email_contacts
    propagate_to_campaigns: bool = False,  # True in email-list mode: sync campaign_contacts too
    debug: bool = False,                   # print Bing→AI payload for each contact
) -> dict:
    rule_resolved = 0
    ai_resolved   = 0
    ec_resolved   = 0   # names copied from email_contacts
    skipped       = 0
    ambiguous_batch: list[dict] = []

    # Build writes list from rule-based pass
    writes: list[tuple] = []  # (doc_ref, name)

    # ── Calibration contact ───────────────────────────────────────────────────
    # When debug=True, leif@auke.no is always prepended as a known test case.
    # Expected result: "Leif Auke" — use this to verify the pipeline is working.
    _CALIB_EMAIL = "leif@auke.no"
    if debug:
        calib = {
            "doc_id":    _doc_id_from_email(_CALIB_EMAIL),
            "email":     _CALIB_EMAIL,
            "domain":    "auke.no",
            "ec_doc_id": _doc_id_from_email(_CALIB_EMAIL),
        }
        # Prepend so it's always the first contact processed
        contacts = [calib] + [c for c in contacts if c["email"] != _CALIB_EMAIL]
        print(f"  [calibration] leif@auke.no prepended — expected name: 'Leif Auke'  title: e.g. 'Founder / CEO'", flush=True)

    # ── Step 0: copy names already in email_contacts ──────────────────────────
    # For campaign_contacts that are missing a name, check email_contacts first
    # before going to Bing/AI (free and instant).
    # Skipped when contacts already come from email_contacts (--all mode).
    if skip_ec_lookup:
        still_missing = contacts
    else:
        # Batch read email_contacts (30 per db.get_all call — much faster than one-by-one)
        BATCH_GET = 30
        ec_col = db.collection("email_contacts")
        id_list = [c.get("ec_doc_id") or _doc_id_from_email(c["email"]) for c in contacts]
        ec_names: dict[str, str] = {}
        for i in range(0, len(id_list), BATCH_GET):
            for snap in db.get_all([ec_col.document(eid) for eid in id_list[i:i + BATCH_GET]]):
                if snap.exists:
                    n = ((snap.to_dict() or {}).get("name") or "").strip()
                    if n:
                        ec_names[snap.id] = n
        still_missing = []
        for c, eid in zip(contacts, id_list):
            if eid in ec_names:
                writes.append((c, ec_names[eid], "ec  "))
                ec_resolved += 1
            else:
                still_missing.append(c)
    contacts = still_missing
    if not skip_ec_lookup:
        print(f"  [name-enrich] {ec_resolved} names copied from email_contacts, "
              f"{len(contacts)} still need resolution", flush=True)

    ai_batch: list[dict] = []   # ALL non-skipped contacts go to AI for validation

    for c in contacts:
        name, confidence = extract_name_rule_based(c["email"])
        if confidence == "skip":
            skipped += 1
            continue
        if name:
            c["rule_name"] = name   # store as hint for AI, not written directly
        ai_batch.append(c)

    ambiguous_batch = ai_batch   # keep variable name for Bing step compatibility

    # Bing search pass — enrich ALL contacts with web snippets before AI
    if ambiguous_batch and not skip_ai:
        import aiohttp
        print(f"  [name-enrich] Bing-searching {len(ambiguous_batch)} ambiguous emails…", flush=True)
        bing_found = 0
        total_bing = len(ambiguous_batch)
        connector = aiohttp.TCPConnector(limit=3, ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            for idx, c in enumerate(ambiguous_batch, 1):
                print(f"  [bing] [{idx:>4}/{total_bing}] {c['email']}", flush=True)
                await asyncio.sleep(0.8)   # gentle rate limit
                found = await _bing_search_name(session, c["email"], c.get("domain", ""))
                if found:
                    chunks = found.get("context_chunks", [])
                    if chunks:
                        # Join up to 3 verified context windows; cap at 800 chars total
                        c["context"] = " […] ".join(chunks[:8])[:6000]
                        bing_found += 1
                        print(f"           → context: {len(chunks)} chunk(s) found", flush=True)
                    if "name" in found:
                        print(f"           → hint name: {found['name']}", flush=True)
                        c["rule_name"] = c.get("rule_name") or found["name"]
                    if "title" in found and not c.get("title"):
                        c["bing_title"] = found["title"]
                        print(f"           → hint title: {found['title']}", flush=True)
        print(f"  [name-enrich] Bing found snippets for {bing_found}/{total_bing}", flush=True)

    # AI pass for ambiguous contacts
    if ambiguous_batch and not skip_ai:
        print(f"  [name-enrich] Sending {len(ambiguous_batch)} ambiguous emails to AI in batches of {batch_size}…",
              flush=True)
        # Load key from config (which reads .env)
        try:
            from app.functions.config import cfg as _cfg
        except ImportError:
            from functions.config import cfg as _cfg
        _key   = _cfg.OPENAI_API_KEY or ""
        _model = model or getattr(_cfg, "OPENAI_MODEL", "gpt-4o-mini") or "gpt-4o-mini"

        if not _key:
            print("  [name-enrich] No OPENAI_API_KEY in .env — skipping AI pass", flush=True)
        else:
            try:
                from openai import AsyncOpenAI
                client = AsyncOpenAI(api_key=_key)
            except ImportError:
                client = None
                print("  [name-enrich] openai package not installed", flush=True)

            if client:
                for i in range(0, len(ambiguous_batch), batch_size):
                    chunk = ambiguous_batch[i:i + batch_size]
                    results = await _ai_resolve_batch(client, _model, chunk, _debug=debug)
                    for c in chunk:
                        if c["doc_id"] in results:
                            ai_result = results[c["doc_id"]]  # {name, title?}
                            ai_name  = ai_result.get("name", "")
                            ai_title = ai_result.get("title", "")
                            # Prefer AI title over Bing hint if both exist
                            if ai_title:
                                c["bing_title"] = ai_title
                            rule_name = c.get("rule_name", "")
                            if rule_name and rule_name.lower() == ai_name.lower():
                                writes.append((c, ai_name, "rule"))
                                rule_resolved += 1
                            else:
                                writes.append((c, ai_name, "ai  "))
                                ai_resolved += 1
                        else:
                            # AI returned null — trust the rejection.
                            # Do NOT fall back to rule_name; a wrong name is
                            # worse than no name.
                            skipped += 1
            else:
                skipped += len(ambiguous_batch)
    else:
        for c in ambiguous_batch:
            if c.get("rule_name"):
                writes.append((c, c["rule_name"], "rule"))
                rule_resolved += 1
            else:
                skipped += 1

    # Apply writes
    if not dry_run:
        batch = db.batch()
        batch_count = 0
        for c, name, source in writes:
            bing_title = c.get("bing_title", "")
            title_suffix = f"  [{bing_title}]" if bing_title else ""
            print(f"  [{source:4s}] {c['email']:40s} → {name}{title_suffix}", flush=True)
            update = {"name": name}
            if bing_title:
                update["title"] = bing_title
            # campaign_contacts doc
            if c.get("campaign_ref"):
                batch.update(c["campaign_ref"], update)
                batch_count += 1
            # email_contacts doc (keep in sync)
            ec_id = c.get("ec_doc_id") or _doc_id_from_email(c["email"])
            ec_ref = db.collection("email_contacts").document(ec_id)
            snap = ec_ref.get()
            if snap.exists:
                ec_data = snap.to_dict() or {}
                ec_update = {}
                if not ec_data.get("name"): ec_update["name"] = name
                if bing_title and not ec_data.get("title"): ec_update["title"] = bing_title
                if ec_update:
                    batch.update(ec_ref, ec_update)
                    batch_count += 1
            if batch_count >= 400:
                batch.commit()
                batch = db.batch()
                batch_count = 0
        if batch_count:
            batch.commit()
    else:
        for c, name, source in writes:
            print(f"  [{source:4s}] {c['email']:40s} → {name}  [DRY]", flush=True)

    # ── Propagate to campaign_contacts when called in email-list mode ────────
    # The campaign_contacts doc ID is _doc_id_from_email(email) — same formula
    # used everywhere in the pipeline. So we load all campaign IDs once, then
    # directly address campaigns/{id}/campaign_contacts/{email_doc_id} for each
    # resolved email. No collection-group index required.
    campaign_contacts_updated = 0
    if not dry_run and propagate_to_campaigns and writes:
        # Build set of (email, doc_id, name, title) for emails that got a name
        to_propagate = [
            (c["email"], c.get("ec_doc_id") or _doc_id_from_email(c["email"]),
             name, c.get("bing_title", ""))
            for c, name, source in writes
            if not c.get("campaign_ref")   # skip: already handled via campaign_ref
        ]
        if to_propagate:
            # Load all campaign IDs (lightweight — stream with select([]) for IDs only)
            camp_ids = [d.id for d in db.collection("campaigns").select([]).stream()]
            cc_batch = db.batch()
            cc_count = 0
            # Batch-read all (campaign_id, email_doc_id) combos in groups of 30
            BGET = 30
            refs = [
                db.collection("campaigns").document(cid)
                  .collection("campaign_contacts").document(edid)
                for cid in camp_ids
                for _, edid, _, _ in to_propagate
            ]
            # Build lookup: (camp_id, doc_id) → (name, title)
            name_map = {edid: (name, title) for _, edid, name, title in to_propagate}
            for i in range(0, len(refs), BGET):
                for snap in db.get_all(refs[i:i + BGET]):
                    if not snap.exists:
                        continue
                    existing = snap.to_dict() or {}
                    doc_id = snap.id
                    name, title = name_map.get(doc_id, (None, None))
                    if not name:
                        continue
                    upd: dict = {}
                    if not existing.get("name"):
                        upd["name"] = name
                    if title and not existing.get("title"):
                        upd["title"] = title
                    if upd:
                        cc_batch.update(snap.reference, upd)
                        cc_count += 1
                        if cc_count >= 400:
                            cc_batch.commit()
                            cc_batch = db.batch()
                            cc_count = 0
            if cc_count:
                cc_batch.commit()
        campaign_contacts_updated = cc_count if to_propagate else 0
        print(f"  [name-enrich] propagated to {campaign_contacts_updated} campaign_contacts doc(s)",
              flush=True)

    # Build per-email resolved map — used by API callers
    resolved: dict[str, dict] = {}
    for c, name, source in writes:
        entry: dict = {"name": name, "source": source.strip()}
        bing_title = c.get("bing_title", "")
        if bing_title:
            entry["title"] = bing_title
        resolved[c["email"]] = entry

    return {
        "total":         len(contacts),
        "ec_resolved":   ec_resolved,
        "rule_resolved": rule_resolved,
        "ai_resolved":   ai_resolved,
        "skipped":       skipped,
        "written":                  len(writes),
        "campaign_contacts_updated": campaign_contacts_updated,
        "resolved":                  resolved,   # email → {name, title?, source}
    }


def enrich_email_list(
    emails: list[str],
    *,
    db=None,
    dry_run: bool = False,
    skip_ai: bool = False,
    model: str = "gpt-4o-mini",
    batch_size: int = 5,
) -> dict:
    """Enrich a flat list of email addresses — no campaign context required.

    Used by the API endpoint POST /api/crm/name-enrich.
    Returns the full _enrich result dict including 'resolved' map.
    """
    if db is None:
        db = _get_db()
    contacts = []
    for email in emails:
        email = email.strip().lower()
        if not email or "@" not in email:
            continue
        contacts.append({
            "doc_id":    _doc_id_from_email(email),
            "email":     email,
            "domain":    email.split("@")[1],
            "ec_doc_id": _doc_id_from_email(email),
            # no campaign_ref — writes go to email_contacts only
        })
    if not contacts:
        return {"total": 0, "ec_resolved": 0, "rule_resolved": 0,
                "ai_resolved": 0, "skipped": 0, "written": 0, "resolved": {}}
    return asyncio.run(_enrich(
        db, contacts,
        dry_run=dry_run,
        skip_ai=skip_ai,
        model=model,
        batch_size=batch_size,
        skip_ec_lookup=False,
        propagate_to_campaigns=True,   # sync campaign_contacts across all campaigns
    ))


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Fill missing contact names from email addresses using rules + AI.")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--campaign", metavar="ID",
                     help="Campaign ID — enrich campaign_contacts for this campaign")
    grp.add_argument("--all", action="store_true",
                     help="Enrich campaign_contacts for ALL campaigns in the campaigns collection")
    grp.add_argument("--emails", metavar="EMAIL", nargs="+",
                     help="One or more email addresses to enrich directly (no campaign context)")
    ap.add_argument("--dry-run",  action="store_true", help="Preview without writing")
    ap.add_argument("--skip-ai",  action="store_true", help="Rule-based only, no OpenAI")
    ap.add_argument("--model",    default="gpt-4o-mini", help="OpenAI model (default: gpt-4o-mini)")
    ap.add_argument("--batch",    type=int, default=5,  help="AI batch size (default: 5)")
    ap.add_argument("--limit",    type=int, default=None, help="Max contacts to process (useful with --all)")
    ap.add_argument("--debug",    action="store_true", help="Print what Bing sends to AI for each contact")
    args = ap.parse_args()

    db = _get_db()
    contacts: list[dict] = []

    if args.campaign:
        print(f"[name-enrich] loading campaign_contacts for '{args.campaign}'…", flush=True)
        camp_ref = db.collection("campaigns").document(args.campaign)
        if not camp_ref.get().exists:
            print(f"[name-enrich] ERROR: campaign '{args.campaign}' not found", file=sys.stderr)
            sys.exit(1)
        for doc in camp_ref.collection("campaign_contacts").stream():
            data = doc.to_dict() or {}
            if data.get("name", "").strip():
                continue   # already has a name
            email = (data.get("email") or "").strip()
            if not email:
                continue
            contacts.append({
                "doc_id":       doc.id,
                "email":        email,
                "domain":       email.split("@")[1] if "@" in email else "",
                "campaign_ref": doc.reference,
                "ec_doc_id":    _doc_id_from_email(email),
            })
    elif args.emails:
        print(f"[name-enrich] enriching {len(args.emails)} email(s) from --emails list…", flush=True)
        result = enrich_email_list(
            args.emails, db=db,
            dry_run=args.dry_run, skip_ai=args.skip_ai,
            model=args.model, batch_size=args.batch,
        )
        print(f"\n[name-enrich] Done — "
              f"rule={result['rule_resolved']}  ai={result['ai_resolved']}  "
              f"skipped={result['skipped']}  written={result['written']}"
              f"{'  (DRY RUN)' if args.dry_run else ''}", flush=True)
        return
    else:
        print("[name-enrich] loading all campaigns…", flush=True)
        for camp_doc in db.collection("campaigns").stream():
            camp_id = camp_doc.id
            camp_contacts = db.collection("campaigns").document(camp_id).collection("campaign_contacts").stream()
            for doc in camp_contacts:
                data = doc.to_dict() or {}
                if data.get("name", "").strip():
                    continue
                email = (data.get("email") or "").strip()
                if not email:
                    continue
                contacts.append({
                    "doc_id":       doc.id,
                    "email":        email,
                    "domain":       email.split("@")[1] if "@" in email else "",
                    "campaign_ref": doc.reference,
                    "ec_doc_id":    _doc_id_from_email(email),
                    "_campaign_id": camp_id,
                })
        print(f"[name-enrich] found {len(contacts)} contacts without names across all campaigns", flush=True)

    if args.limit:
        contacts = contacts[:args.limit]
        print(f"[name-enrich] limited to {len(contacts)} contacts (--limit {args.limit})", flush=True)
    print(f"[name-enrich] {len(contacts)} contacts with missing names", flush=True)
    if not contacts:
        print("[name-enrich] Nothing to do.", flush=True)
        return

    result = asyncio.run(_enrich(
        db, contacts,
        dry_run=args.dry_run,
        skip_ai=args.skip_ai,
        model=args.model,
        batch_size=args.batch,
        skip_ec_lookup=False,   # always check email_contacts first
        debug=args.debug,
    ))

    print(f"\n[name-enrich] Done — "
          f"rule={result['rule_resolved']}  ai={result['ai_resolved']}  "
          f"skipped={result['skipped']}  written={result['written']}"
          f"{'  (DRY RUN)' if args.dry_run else ''}", flush=True)


if __name__ == "__main__":
    main()
