---
name: blueboot-agency-power-agent
description: >
  Project context skill for the blueboot_agency_power_agent codebase.
  Use this skill proactively whenever working on any script in this project,
  adding queries, fixing bugs, creating new pipeline steps, or answering
  questions about how the system works. Covers both pipelines, Firestore
  structure, coding rules, and the critical distinction between site_agent
  (end-user companies) and lead_agent (web agencies).
---

# BlueBoot Agency Power Agent — Project Context

## THE MOST IMPORTANT RULE

**`site_agent` finds END-USER COMPANIES** — businesses that need a web partner
(manufacturers, shops, SaaS companies, hospitality, healthcare, etc.).

**`lead_agent` finds WEB AGENCIES** — WordPress/WooCommerce developers, digital
agencies, SEO agencies that could become resellers.

These are two completely separate pipelines. Never add agency-focused search
queries to `site_agent_queries.json`. Never add end-user company queries to
`lead_agent` catalog sources.

---

## Two Pipelines

### Site Pipeline — `site_agent.py` (end-user companies)

```
site_agent.py              → Bing/Brave search + crawl → site_leads + site_contacts
site_enrich_agent.py       → GPT classify → sector, platform, summary, ai_country
site_contact_enrich.py     → Brave + GPT → occupation, LinkedIn, socials
site_location_enrich.py    → GPT batches of 50 → location_full, city, region, country
site_contact_export.py     → Excel per contact  (--location, --sector, --category, --page-count)
site_leads_export.py       → Excel per lead     (--location, --sector, --category)
site_contact_export.py --campaign NAME  → saves to site_campaigns/{NAME}
site_campaign_mail_prepare.py           → out_mail + out_mail_contacts docs
```

### Lead Pipeline — `lead_agent.py` (web agencies)

```
lead_agent.py              → Bing/Brave + catalog scrapers → leads_extracted
lead_enrich_agent.py       → GPT classify + score
lead_enrich_contacts.py    → find contacts
lead_campaign_mail_prepare.py --extract NAME → out_mail + out_mail_contacts
```

---

## Firestore Collections

### Site Pipeline
```
site_leads/{lead_id}
  ├── domain, website, country, ai_country, ai_sector, ai_company_type
  ├── ai_platform, ai_hosting, ai_summary, ai_confidence
  ├── page_count, sitemap_type, query_category
  ├── location, location_full, location_city, location_region
  ├── location_country, location_confidence, location_source
  └── site_contacts/{contact_id}
        ├── email, name, title, phone, occupation
        └── linkedin, found_on, brave_enriched_at

sites_excluded/{lead_id}   ← domains that failed quality check (auto-skip on re-run)

site_campaigns/{campaign}/
  ├── site_campaign_sites/{site_id}/
  │     └── site_campaign_contacts/{contact_id}
  ├── out_mail/{country}             ← template doc
  └── out_mail_contacts/{contact_id} ← personalised doc, status=pending
```

### Lead Pipeline
```
leads_extract/{extract_id}/
  ├── leads_extracted/{lead_id}/
  │     └── contacts_extracted/{contact_id}
  ├── out_mail/{country}
  └── out_mail_contacts/{contact_id}
```

---

## Config Files

| File | Purpose |
|------|---------|
| `config/site_agent_queries.json` | Per-country search queries for end-user companies |
| `config/catalogs.json` | Agency directory URLs for lead_agent (Sortlist, DesignRush, TopDevelopers, DAN) |
| `config/countries.json` | Country metadata (TLDs, language, min_pages) |
| `config/wp_plugin_queries.json` | WordPress plugin catalogue lead source (wp_plugin_leads.py) |
| `config/blocklist_domains.txt` | Domains to always skip |

### site_agent_queries.json structure
```json
{
  "IN": {
    "name": "India",
    "min_pages": 10,
    "target_types": ["company", "ecommerce", "technology", ...],
    "query_categories": {
      "company": ["manufacturing company india website", ...],
      "pune": ["company pune website", "saas company pune", ...]
    }
  }
}
```

Queries are end-user companies — NOT agencies, NOT WordPress developers.

---

## Key Scripts

### wp_plugin_leads.py (project root)
Standalone script — scrapes WordPress.org plugin API for author websites.
Config in `config/wp_plugin_queries.json`.
```
python wp_plugin_leads.py --countries UK IN --dry-run 20 --verbose
python wp_plugin_leads.py --countries NO SE DK --per-term 150 --out leads.csv
```

### run_india.bat
Full India pipeline batch: discover → classify → contact enrich → location enrich → export by city.

---

## Coding Rules (from CLAUDE.md)

### Thread Safety — MANDATORY

#### `firestore_client.py` — always use the lock

`app/firestore_client.py` has a global `_db` singleton. When multiple `_write_exec`
threads call `get_firestore()` simultaneously, a race condition causes double-init and
connection pool corruption → hangs. The file already has a `threading.Lock()` fix.

**NEVER** remove or bypass the lock. **NEVER** call `get_firestore()` without the
double-checked locking pattern:
```python
_db = None
_lock = threading.Lock()

def get_firestore():
    global _db
    if _db is not None:        # fast path — no lock needed once initialised
        return _db
    with _lock:
        if _db is not None:    # re-check inside lock
            return _db
        # ... initialise ...
        _db = firestore.client()
    return _db
```

#### Fire-and-forget with `_write_exec.submit()` — DO NOT USE

**NEVER** do fire-and-forget writes in the site_agent consumer loop:
```python
# WRONG — no backpressure, floods pool when Firestore is slow
_write_exec.submit(lambda: upsert_site_lead(lead, col))
```

**ALWAYS** await with timeout so the consumer stays bounded:
```python
# CORRECT
await asyncio.wait_for(
    loop.run_in_executor(_write_exec, lambda _l=lead: upsert_site_lead(_l, col)),
    timeout=20.0,
)
```

#### `gather(*tasks)` fan-out helpers need per-worker `wait_for`

The fix/enrich helpers (`fix_rescrape_contacts.py` `_recrawl_one`,
`lead_enrich_contacts.py` `_enrich_one`) don't use the queue/consumer pattern — they build
`tasks = [...]` and `await asyncio.gather(*tasks, return_exceptions=True)`. `return_exceptions=True`
only catches *raised* exceptions; a worker whose chained awaits (crawl chain or per-item Bing
loop) simply never return will hang `gather` forever — the run freezes after its last printed
line. Wrap every worker:
```python
async def _worker_guarded(item):
    try:
        await asyncio.wait_for(_worker(session, item, ...), timeout=120.0)
    except asyncio.TimeoutError:
        print(f"  timeout on {item}")
tasks = [_worker_guarded(it) for it in items]
await asyncio.gather(*tasks, return_exceptions=True)
```
Audit: `grep -c wait_for` == 0 while `grep -c gather` >= 1 ⇒ hang candidate.

#### `_write_exec.shutdown(wait=True)` — blocks event loop

**NEVER** call `shutdown(wait=True)` synchronously inside an `async def`:
```python
# WRONG — freezes entire event loop if any write thread is still running
_write_exec.shutdown(wait=True)

# CORRECT — threads finish in background, event loop stays alive
_write_exec.shutdown(wait=False)
```

#### `_write_exec` thread pool size

Set `max_workers` equal to the number of consumer workers so every consumer
can submit a write simultaneously without queueing:
```python
_write_exec = ThreadPoolExecutor(max_workers=max(workers, 8), ...)
```

#### Thread locks on ALL shared mutable state

Any variable accessed by more than one thread MUST be protected by a `threading.Lock`.
This applies to globals, module-level singletons, and any shared dict/list/counter.

**Common shared variables that need locks in this project:**

| Variable | File | Status |
|----------|------|--------|
| `_db` Firestore singleton | `firestore_client.py` | ✅ `threading.Lock()` double-checked locking |
| `_firebase_db` + `initialize_app` | `functions/firebase_sync.py` | ✅ `_firebase_lock` wraps both |
| `initialize_app` (×4) | `lead_agent.py` | ✅ `_firebase_init_lock` wraps all |
| `initialize_app` in 21 other scripts | all `app/*.py` | ✅ `_local_fb_lock` added by audit pass |

**Audit command** — run this after any new file is added to verify:
```bash
python3 -c "
import os, re
for root, dirs, files in os.walk('app'):
    dirs[:] = [d for d in dirs if d != '__pycache__']
    for f in files:
        if not f.endswith('.py'): continue
        src = open(os.path.join(root,f), errors='replace').read()
        lines = src.splitlines()
        for i, l in enumerate(lines):
            if ('initialize_app' in l or 'firestore.client()' in l) and not l.strip().startswith('#'):
                ctx = chr(10).join(lines[max(0,i-30):i])
                if not re.search(r'with\s+_\w*lock\w*\s*:', ctx):
                    print(f'UNPROTECTED {root}/{f}:{i+1}  {l.strip()[:70]}')
"
```

**Pattern — always use double-checked locking for singletons:**
```python
_singleton = None
_lock = threading.Lock()

def get_singleton():
    if _singleton is not None:   # fast path, no lock
        return _singleton
    with _lock:
        if _singleton is not None:  # re-check inside lock
            return _singleton
        _singleton = create_it()
    return _singleton
```

**asyncio counters are safe without locks** — asyncio is single-threaded so
`counters["done"] += 1` inside a coroutine needs no lock. Only code running in
`ThreadPoolExecutor` threads needs locks.

---

## MANDATORY: After every code change — run this health check

After editing ANY .py file anywhere in this project, always run:

```bash
python3 -c "
import os, subprocess, ast
issues = []
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in ('__pycache__', '.venv', 'venv', '.git', 'node_modules')]
    for f in sorted(files):
        if not f.endswith('.py'): continue
        path = os.path.join(root, f)
        src  = open(path, errors='replace').read()
        if len(src) < 100: continue
        # 1. Compile
        r = subprocess.run(['python3','-W','error','-m','py_compile', path], capture_output=True)
        if r.returncode != 0:
            issues.append(f'COMPILE  {path}')
            continue
        # 2. Any file with main() must have if __name__ == '__main__': main()
        try:
            tree = ast.parse(src)
            has_main = any(isinstance(n, ast.FunctionDef) and n.name == 'main' for n in ast.walk(tree))
            if has_main and 'if __name__' not in src:
                issues.append(f'NO_ENTRY {path}')
        except: pass
        # 3. Missing trailing newline = file was truncated
        if not src.endswith('\n'):
            issues.append(f'NO_NL    {path}')
print('ALL OK' if not issues else '\n'.join(issues))
"
```

**What each failure means:**
- `COMPILE` — syntax error or bad import — fix immediately
- `NO_ENTRY` — `main()` defined but no `if __name__ == '__main__': main()` → script runs silently and exits with code 0 without doing anything
- `NO_NL` — file truncated mid-write (Edit/Write tool byte limit hit) → may cause runtime errors

**Fix `NO_ENTRY`:**
```python
src = open(path).read()
src = src.rstrip() + '\n\n\nif __name__ == "__main__":\n    main()\n'
open(path, 'w').write(src)
```

**Fix `NO_NL`:**
```python
content = open(path, 'rb').read()
if not content.endswith(b'\n'):
    open(path, 'ab').write(b'\n')
```
