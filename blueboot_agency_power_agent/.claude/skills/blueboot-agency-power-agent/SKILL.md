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

### asyncio — MANDATORY

Every `run_in_executor` MUST have `asyncio.wait_for(timeout=12.0)`:
```python
# CORRECT
await asyncio.wait_for(
    loop.run_in_executor(None, lambda: blocking_call(...)),
    timeout=12.0,
)
# WRONG — can hang forever
await loop.run_in_executor(None, lambda: blocking_call(...))
```

Every top-level coroutine chain MUST have a ceiling timeout:
```python
lead, reason = await asyncio.wait_for(
    process_site_async(session, url, ...),
    timeout=120.0,
)
```

Consumer loops MUST call `queue.task_done()` unconditionally in `finally`:
```python
while True:
    item = await queue.get()
    try:
        if item is SENTINEL:
            break
        # process...
    except Exception as exc:
        print(f"error: {exc}")
    finally:
        queue.task_done()   # ALWAYS — even on break/continue
```

### After EVERY code change — mandatory verification

After every code change, no matter how small, run all three checks before reporting done:

```bash
# 1. Syntax check
python3 -m py_compile app/the_file.py && echo "OK"

# 2. Tail check — confirm file is not truncated
tail -5 app/the_file.py

# 3. Line count — sanity check against previous known size
wc -l app/the_file.py
```

The file is truncated if:
- The last line is not `    main()` or `if __name__ == "__main__":` (for entry-point scripts)
- The tail ends mid-string, mid-function, or with trailing whitespace only
- Line count dropped unexpectedly

If truncated: append the missing tail using `cat >>` (never Edit/Write to fix truncation —
those tools are what cause it). Then re-verify.

### Large Python files
Never use Edit/Write tools directly on `site_agent.py` or other large files.
Use bash Python replace scripts instead:
```bash
python3 << 'PYEOF'
src = open(path).read()
src = src.replace(old, new, 1)
open(path, 'w').write(src)
PYEOF
```
After any edit: `python3 -m py_compile app/site_agent.py && tail -5 app/site_agent.py`

### ElementTree
Never use `or` to chain `Element.find()` — falsy elements cause silent drops:
```python
# WRONG
loc = sm.find(f"{{{ns}}}loc") or sm.find("loc")
# CORRECT
loc = sm.find(f"{{{ns}}}loc")
if loc is None:
    loc = sm.find("loc")
```

### Secrets
Never modify `blueboot_secrets.py`. OpenAI key is at `openAiConfig.defaultProjectKey`.
Firebase key is at `fireBaseAdminKey`.

---

## Export Filters

Both `site_contact_export.py` and `site_leads_export.py` support:

| Flag | Filters on |
|------|-----------|
| `--countries IN` | `ai_country` field |
| `--sector ecommerce` | `ai_sector` |
| `--category pune` | `query_category` (set at crawl time) |
| `--location Pune` | `location_full` substring match |
| `--page-count medium` | page size bucket (micro/small/medium/large/huge/ultra) |
| `--with-email-only` | contacts with email address |

Page count buckets: micro=1-50, small=51-500, medium=501-3k, large=3k-10k, huge=10k-100k, ultra=100k+

---

## Location Enrichment

`site_location_enrich.py` uses GPT-5.4-mini in batches of 50, 3 parallel.

Fields written to `site_leads`:
- `location` / `location_full` — "London, England, United Kingdom"
- `location_city`, `location_region`, `location_country` (ISO code: GB/IN/NO...)
- `location_confidence` (1.0=address found, 0.3=TLD only)
- `location_source` (address/phone/postcode/content/company_name/domain)

```
python app\site_location_enrich.py --countries UK --dry-run 20
python app\site_location_enrich.py --countries IN
```

---

## Country Codes Used Internally

| Code | Country | TLD | tld_strict |
|------|---------|-----|-----------|
| UK | United Kingdom | .co.uk / .uk | true |
| IN | India | .in / .co.in | true |
| NO | Norway | .no | false |
| SE | Sweden | .se | false |
| DK | Denmark | .dk | false |
| FI | Finland | .fi | false |
| AU | Australia | .com.au / .au | true |
| DE | Germany | .de | — |

`tld_strict=false` means domain TLD is not filtered — search terms drive targeting.
