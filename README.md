# BlueBoot Agency Power Agent

> **AI-powered lead discovery and outreach pipeline for BlueSearch**

Finds end-user companies and web agencies, enriches contacts with AI, and delivers a unified outreach-ready contact list.

---

## What it does

Two independent pipelines discover and qualify leads, then converge into a single unified `email_contacts` Firestore collection for human review and automated outreach.

```
SITE PIPELINE                          LEAD PIPELINE
(end-user companies)                   (web agencies / resellers)
        │                                      │
  Bing + Brave search                   Bing + Brave search
  Sitemap analysis                      Agency directories (952 sources)
  Contact extraction                    Contact enrichment
  GPT: sector, platform,               GPT: reseller potential,
       location, email type                  specialisation, score
        │                                      │
        └──────────────┬────────────────────────┘
                       ▼
              email_contacts
            (Firestore collection)
                       │
              Human review in Excel
                       │
              Automated outreach sender
```

---

## Key highlights

**🔍 Discovery at scale**
- Searches Bing, Brave and 952 agency directory sources across 23 countries
- 5,400+ search queries in `countries.json`, 1,996 site queries in `site_agent_queries.json`
- Async pipeline with 20 parallel workers — sitemap analysis + contact scraping

**🤖 AI enrichment**
- GPT classifies every site and agency: sector, platform, company type, reseller potential
- GPT infers city, region and country from site content (no geocoding API needed)
- GPT classifies every contact: email type + role → outreach priority 1–4

**📊 Unified outreach store**
- Single `email_contacts` Firestore collection fed by both pipelines
- Contacts from both pipelines deduplicated by email
- Pipeline marks (`mark_site_leads`, `mark_leads`) — contacts in both = highest confidence
- Status lifecycle: pending → approved → sent → replied → converted

**🛡️ Data quality**
- `clean_str()` — strips JSON artifacts, phone labels, Scandinavian email labels from names/titles
- `email_matches_name()` — validates name field is consistent with email local part
- `normalize_url()` — strips deep URL paths to base domain
- `_valid_email()` — blocks system addresses, numeric domains, hex UUIDs, single-char TLDs

**⚡ Performance**
- Firestore reads use `get_partitions(16)` with `ThreadPoolExecutor(16)` — 10× faster on large collections
- Statistics functions scan site_leads + site_contacts in parallel (2 simultaneous streams)
- Catalog scraping and Bing search run concurrently

**📈 Statistics & reporting**
- `maint_statistics.py` — 8 aggregations: priority × country, reasons, collection overview, site/lead enrichment funnels, data quality, email_contacts funnel, pipeline coverage
- One combined Excel workbook (`statistics_YYYY-MM-DD.xlsx`) with one sheet per aggregation
- One Firestore summary doc per day (`statistics/summary-YYYY-MM-DD`)
- Exclusion rates reported: % of discovered leads/sites that were rejected

**🧪 Tested**
- `run_test_all.bat` — 9-step full pipeline dry-run test
- `run_test_maint.bat` — 8 maintenance script dry-run tests

---

## Quick start

```bat
REM 1. Copy and fill in .env
copy .env.example .env

REM 2. Run the site pipeline (edit COUNTRIES and CAMPAIGN at top of bat)
run_site_pipeline.bat

REM 3. Run the lead pipeline
run_lead_pipeline.bat

REM 4. Test everything is working
run_test_all.bat
```

---

## Project structure

```
blueboot_agency_power_agent/
├── app/                        Python scripts
│   ├── site_agent.py           Site pipeline — discovery
│   ├── site_enrich_agent.py    Site pipeline — AI classification
│   ├── site_contact_enrich.py  Site pipeline — contact enrichment
│   ├── site_location_enrich.py Site pipeline — location inference
│   ├── site_email_check.py     Site pipeline — email classification
│   ├── site_smart_export.py    Site pipeline — export + write to email_contacts
│   ├── lead_agent.py           Lead pipeline — discovery
│   ├── lead_enrich_agent.py    Lead pipeline — AI classification
│   ├── lead_enrich_contacts.py Lead pipeline — social enrichment
│   ├── leads_email_check.py    Lead pipeline — email classification
│   ├── leads_smart_export.py   Lead pipeline — export + write to email_contacts
│   ├── email_contacts_export.py Unified review Excel (both pipelines)
│   ├── maint_statistics.py     Statistics + reporting
│   ├── maint_*.py              Maintenance scripts
│   └── functions/
│       ├── config.py           Central env config (all keys from .env)
│       ├── firebase_cred.py    Firebase credential loader
│       ├── utils.py            clean_str, resolve_country, email_matches_name, ...
│       └── excel_builder.py    Shared Excel sheet builder
├── config/
│   ├── countries.json          TLDs, keywords, lead queries (23 countries)
│   ├── site_agent_queries.json Site search queries (11 countries, 1,996 queries)
│   └── catalogs.json           Agency directories (952 entries, 23 countries)
├── docs/
│   ├── BlueSearch_Pipeline_Reference.docx  Full technical reference
│   └── email_contacts_field_reference.docx email_contacts schema + review guide
├── tests/                      Test scripts
├── .env                        Your secrets (git-ignored)
├── .env.example                Template — copy to .env
├── run_site_pipeline.bat       Full site pipeline starter
├── run_lead_pipeline.bat       Full lead pipeline starter
├── run_test_all.bat            Full pipeline dry-run test
└── run_test_maint.bat          Maintenance scripts dry-run test
```

---

## Documentation

| Document | Contents |
|---|---|
| `README_detailed.md` | Full pipeline reference — all scripts, CLI flags, Firestore schema |
| `docs/BlueSearch_Pipeline_Reference.docx` | Architecture diagrams, field schema, workflow |
| `docs/email_contacts_field_reference.docx` | email_contacts fields, review workflow |
| `.env.example` | All environment variables with descriptions |

---

## Requirements

- Python 3.11+
- `.venv` with `pip install -r requirements.txt`
- Firebase project with Firestore enabled
- OpenAI API key (for enrichment steps)
- Brave Search API key (for search + contact enrichment)
