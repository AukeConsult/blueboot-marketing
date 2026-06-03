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

## Coding rules → see CLAUDE.md

All engineering rules (async/asyncio timeouts, producer/consumer sentinels,
thread safety & locking, `_write_exec` usage, large-file editing, ElementTree
`.find()` gotcha, and the `py_compile` + `pyflakes` + health-check verification
steps) live in `CLAUDE.md` at the project root. Follow them for any code change.
This skill covers project *domain* context only — the two pipelines, Firestore
layout, config files, and scripts above.
