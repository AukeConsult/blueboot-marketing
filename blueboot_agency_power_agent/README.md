# BlueBoot Agency Power Agent

## Pipeline Overview

Two independent pipelines share the same Firestore project. The **Site Pipeline** (Section 1)
is the actively developed one. The **Lead Agent Pipeline** (Section 2) is the original and
remains fully operational.

---

### Site Pipeline (Section 1 — current)

Discovers content-heavy websites, measures them via sitemap, extracts and enriches contacts.

```
── Discover & collect ─────────────────────────────────────────────────────────

1. site_agent.py              Discover sites via Bing + Brave search
                              → site_leads + site_contacts in Firestore

2. site_enrich_agent.py       AI classification of each site_lead (GPT)
                              → sector, type, platform, hosting, keywords,
                                summary, ai_contacts, confidence

3. site_contact_enrich.py     Enrich site_contacts via Brave Search + GPT
                              → occupation, company, linkedin, twitter,
                                facebook, other_links

── Maintenance ────────────────────────────────────────────────────────────────

4. site_excluded_recheck.py   Re-check sites_excluded — recover passing sites
5. site_sitemap_backfill.py   Backfill sitemap data on existing site_leads

── Export ─────────────────────────────────────────────────────────────────────

6. site_leads_export.py       Excel export — one row per lead
7. site_contact_export.py     Excel export — one row per contact + site fields
```

**Quick start — Norway**

```bat
python app\site_agent.py --countries NO
python app\site_enrich_agent.py --countries NO
python app\site_contact_enrich.py --countries NO
python app\site_contact_export.py --countries NO --with-email-only
python app\site_leads_export.py --countries NO
```

---

### Lead Agent Pipeline (Section 2 — legacy)

Finds web agencies, WordPress/WooCommerce providers and digital agencies. Scores them
for reseller fit and exports to Excel + Firestore.

```
── Discover ───────────────────────────────────────────────────────────────────

1. lead_agent.py              Search (Bing + Brave + Google) + catalog scraping
                              → leads + contacts in Firestore
                                Modes: search | catalog | both | audit
                                Brave runs in parallel with Bing per query
                                (requires BRAVE_API_KEY in .env)

── Enrich ─────────────────────────────────────────────────────────────────────

2. lead_enrich_agent.py       AI classification of each lead (GPT)
                              → sector, specialisation, client_base,
                                reseller_potential, platform, summary,
                                confidence

3. lead_enrich_contacts.py         Social profile enrichment via Bing search
                              → linkedin_personal, twitter, facebook,
                                instagram, telegram, whatsapp per contact

── Export ─────────────────────────────────────────────────────────────────────

4. lead_extract.py           Filtered Excel export from leads collection
                              → filter by score, country, priority, keyword
                                Optionally saves extract to Firestore

── Analytics ──────────────────────────────────────────────────────────────────

5. statistics.py              Aggregates leads into Firestore statistics docs
                              → priority × country breakdown
                              → reasons count per country
                              → Excel reports
```

**Quick start — Norway**

```bat
python app\lead_agent.py --countries NO --mode both
python app\lead_enrich_agent.py --countries NO
python app\lead_enrich_contacts.py --country NO --skip-enriched
python app\lead_extract.py --country NO --with-email --min-score 60 --save-extract NO_jun01
python app\campaign_exporter.py NO_jun01
python app\statistics.py
```

---

## Section 1 — Site Agent Pipeline (current)

Async Python pipeline that discovers content-heavy websites via Bing search, measures site
size via sitemap, extracts contact emails, and stores results in Firestore.

### Architecture

```
Bing search (5 concurrent)
    ↓ URLs
Queue (asyncio)
    ↓
Site consumers (20 concurrent)
    ├─ Fetch robots.txt + sitemap → page count
    ├─ Fetch homepage → title, description, meta
    └─ Scrape contact pages → emails, phones
        ↓
Firestore
    site_leads/{lead_id}
    site_leads/{lead_id}/site_contacts/{contact_id}
    sites_excluded/{lead_id}   ← rejected sites, never re-fetched
```

### Scripts

| Script | Purpose |
|---|---|
| `app/site_agent.py` | Discovers sites, stores `site_leads` + `site_contacts` |
| `app/site_enrich_agent.py` | AI classification — sector, platform, hosting, contacts |
| `app/site_contact_enrich.py` | Enriches `site_contacts` via Brave Search + GPT |
| `app/site_contact_export.py` | Exports `site_contacts` to Excel (one row per contact) |
| `app/site_excluded_recheck.py` | Re-checks `sites_excluded` and recovers passing sites |
| `app/site_sitemap_backfill.py` | Backfills sitemap data for existing `site_leads` |
| `app/site_leads_export.py` | Exports `site_leads` + contacts to Excel (one row per lead) |
| `site_scrape.bat` | Runs site_agent + site_enrich_agent for all countries |

### CLI — site_agent.py

```bash
python app/site_agent.py --countries NO,SE
python app/site_agent.py --countries NO --category real_estate
python app/site_agent.py --countries ALL --workers 20
python app/site_agent.py --countries NO --dry-run --max-results 20
python app/site_agent.py --countries NO --main-page-only
```

| Flag | Default | Description |
|---|---|---|
| `--countries` | `NO` | Comma-separated ISO codes or `ALL` |
| `--category` | _(all)_ | Run only one query category (e.g. `real_estate`, `tech`, `company`) |
| `--max-results` | `500` | Max Bing results per query |
| `--min-pages` | `0` | Minimum sitemap page count to keep a site |
| `--workers` | `20` | Async consumer concurrency |
| `--delay` | `1.5` | Seconds between Bing queries |
| `--no-firebase` | off | Skip all Firestore writes |
| `--dry-run` | off | Process sites but don't write to Firestore |
| `--collection` | `site_leads` | Firestore collection for accepted sites |
| `--excl-collection` | `sites_excluded` | Firestore collection for rejected sites |
| `--main-page-only` | off | Discard Bing results that are not homepage/root URLs |

### CLI — site_enrich_agent.py

```bash
python app/site_enrich_agent.py --countries NO,SE
python app/site_enrich_agent.py --countries NO,SE --limit 100
python app/site_enrich_agent.py --force          # re-classify already classified sites
```

Reads unprocessed `site_leads` documents and writes back AI-inferred fields:

| Field | Description |
|---|---|
| `ai_sector` | e.g. `public_sector`, `ecommerce`, `media`, `healthcare` |
| `ai_company_type` | e.g. `agency`, `inhouse`, `brand`, `institution` |
| `ai_country` | ISO 3166-1 alpha-2, inferred from TLD / language / address |
| `ai_keywords` | Up to 25 enriched English keywords |
| `ai_summary` | One-sentence description of the site |
| `ai_platform` | Detected CMS/site builder e.g. `WordPress`, `Shopify`, `Webflow` |
| `ai_hosting` | Detected hosting provider e.g. `WP Engine`, `Cloudflare`, `AWS` |
| `ai_contacts` | Array of `{name, email, role}` contacts found on the site |
| `ai_confidence` | Float 0.0–1.0 |
| `ai_classified_at` | ISO 8601 UTC timestamp |

### CLI — site_contact_enrich.py

```bash
python app/site_contact_enrich.py --countries NO,SE
python app/site_contact_enrich.py --countries NO --limit 100 --dry-run
python app/site_contact_enrich.py --force        # re-enrich already enriched contacts
python app/site_contact_enrich.py --concurrent 5
```

Reads every document from the `site_contacts` collectionGroup
(`site_leads/{lead_id}/site_contacts/{contact_id}`), runs a Brave Search per
contact, then uses GPT to extract and write back enriched fields:

| Field | Description |
|---|---|
| `occupation` | Confirmed/enriched job title |
| `company` | Confirmed company name |
| `linkedin` | LinkedIn profile URL |
| `twitter` | Twitter/X profile URL |
| `facebook` | Facebook profile URL |
| `other_links` | Array of other relevant URLs |
| `brave_enriched_at` | ISO 8601 UTC timestamp |

Requires `BRAVE_API_KEY` in `.env`. Skips contacts with no name, and skips
already-enriched contacts unless `--force` is passed.

### CLI — site_excluded_recheck.py

```bash
python app/site_excluded_recheck.py --countries NO
python app/site_excluded_recheck.py --domains example.no
python app/site_excluded_recheck.py --min-pages 50 --dry-run
```

Re-checks sites in `sites_excluded` that were previously rejected (e.g. due to
missing sitemaps). Sites that now pass are moved to `site_leads` and removed from
`sites_excluded`.

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Comma-separated country codes |
| `--domains` | all | Comma-separated domains to re-check |
| `--reason` | all | Only re-check sites whose exclusion reason contains this text |
| `--min-pages` | `50` | Minimum page count to recover a site |
| `--limit` | none | Max sites to re-check |
| `--concurrent` | `50` | Parallel fetches |
| `--dry-run` | off | Print results without writing to Firestore |
| `--force` | off | Re-check even sites with page_count > 0 |

### CLI — site_sitemap_backfill.py

```bash
python app/site_sitemap_backfill.py --countries NO
python app/site_sitemap_backfill.py --countries NO --force
python app/site_sitemap_backfill.py --limit 500 --dry-run
```

Backfills sitemap data (`page_count`, `sitemap_url`, `sitemap_type`, `sitemap_urls`,
`sitemap_oldest_date`) for existing `site_leads` documents that are missing it.

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Comma-separated country codes |
| `--limit` | none | Max leads to process |
| `--concurrent` | `20` | Parallel fetches |
| `--dry-run` | off | Print results without writing to Firestore |
| `--force` | off | Re-scan even leads that already have sitemap data |

### CLI — site_leads_export.py

```bash
python app/site_leads_export.py
python app/site_leads_export.py --countries NO,SE
python app/site_leads_export.py --countries NO --sector ecommerce
python app/site_leads_export.py --countries NO --with-contacts-only
python app/site_leads_export.py --output exports/no_leads.xlsx
```

Exports `site_leads` to Excel — one row per lead, with all contacts folded into a
single cell. Good for a full lead overview.

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Comma-separated country codes |
| `--sector` | all | Filter by `ai_sector` e.g. `ecommerce`, `technology` |
| `--category` | all | Filter by `query_category` e.g. `real_estate`, `healthcare` |
| `--with-contacts-only` | off | Only include leads that have at least one contact |
| `--limit` | none | Max leads to export |
| `--output` | auto-timestamped | Output `.xlsx` path |
| `--dry-run` | off | Count leads without fetching contacts or writing file |

### CLI — site_contact_export.py

```bash
python app/site_contact_export.py
python app/site_contact_export.py --countries NO,SE
python app/site_contact_export.py --countries NO --sector ecommerce
python app/site_contact_export.py --countries NO --with-email-only
python app/site_contact_export.py --countries NO --category healthcare
python app/site_contact_export.py --output exports/contacts_no.xlsx
```

Exports `site_contacts` to Excel — one row per contact, enriched with key fields
from the parent `site_lead` (domain, pages, sector, platform, hosting, AI summary,
keywords). Ideal for outreach — filter by sector, platform, or country to find
exactly the right contacts.

| Flag | Default | Description |
|---|---|---|
| `--countries` | all | Comma-separated country codes |
| `--sector` | all | Filter by `ai_sector` e.g. `ecommerce`, `technology` |
| `--category` | all | Filter by `query_category` e.g. `real_estate`, `healthcare` |
| `--with-email-only` | off | Only include contacts that have an email address |
| `--limit` | none | Max contacts to export |
| `--output` | auto-timestamped | Output `.xlsx` path |

Output file includes a **Summary sheet** with totals by country and AI sector.

### Supported countries

| Code | Country | Native queries |
|---|---|---|
| `NO` | Norway | ✓ |
| `SE` | Sweden | ✓ |
| `DK` | Denmark | ✓ |
| `DE` | Germany | ✓ |
| `UK` | United Kingdom | ✓ |
| `FI` | Finland | ✓ |
| `NL` | Netherlands | ✓ |
| `FR` | France | ✓ |
| `EU` | European Union (.eu domains) | ✓ (English) |

### Query categories (16 per country, ~12 queries each)

`municipality`, `public`, `healthcare`, `education`, `media`, `company`, `shop`,
`association`, `finance`, `legal`, `real_estate`, `logistics`, `construction`,
`tech`, `hr`, `hospitality`

Each `site_lead` document carries a `query_category` field so results can be filtered
by category in Firestore or exports.

### Firestore structure

```
site_leads/{lead_id}
    domain, website, country, country_name, company
    title, description, page_count, sitemap_url, sitemap_type
    source_query, query_category, crawled_at
    target_types[], keywords[]
    ai_sector, ai_company_type, ai_country, ai_confidence  ← written by site_enrich_agent
    ai_keywords[], ai_summary, ai_platform, ai_hosting     ← written by site_enrich_agent
    ai_contacts[{name, email, role}]                       ← written by site_enrich_agent
    ai_classified_at

site_leads/{lead_id}/site_contacts/{contact_id}
    email, name, title, phone, found_on                    ← written by site_agent
    lead_id, domain, website, country, country_name
    occupation, company, linkedin, twitter, facebook       ← written by site_contact_enrich
    other_links[], brave_enriched_at                       ← written by site_contact_enrich

sites_excluded/{lead_id}
    domain, website, country, reason, page_count
    source_query, query_category, excluded_at
```

### Config files

| File | Purpose |
|---|---|
| `config/site_agent_queries.json` | Per-country query categories and search queries |
| `config/countries.json` | TLD filters, accepted_tlds, keywords per country |
| `config/site_agent_blocklist.txt` | Domain patterns to skip |

**Adding a new country:** add entries to both `countries.json` and `site_agent_queries.json`
— no code changes needed.

**Adding a new query category:** add the category + queries to every country entry in
`query_categories` in `site_agent_queries.json` — no code changes needed.

---

## Section 2 — Lead Agent Pipeline (legacy)

Local Python lead-generation agent for finding web agencies, WordPress/WooCommerce providers, SEO agencies, digital agencies and communication agencies that may resell BlueSearch.

Supported countries: Norway (`NO`), Sweden (`SE`), Denmark (`DK`), Germany (`DE`), United Kingdom (`UK`), and any country with a `config/queries_<CODE>.txt` file.

### Scripts

| Script | Purpose |
|---|---|
| `app/lead_agent.py` | Discover agency leads via Bing + Brave + Google + catalog scraping |
| `app/lead_enrich_agent.py` | AI classification of each lead (GPT) → sector, specialisation, reseller_potential |
| `app/lead_enrich_contacts.py` | Enrich contacts with social media profiles via Bing |
| `app/lead_extract.py` | Filtered Excel export + optional Firestore extract save |
| `app/campaign_exporter.py` | Export a `leads_extract` campaign to `output/<campaign_id>/campaign.xlsx` + JSON |
| `app/statistics.py` | Aggregate lead counts by priority/country/reason → Excel + Firestore |
| `app/fix_contact_country.py` | One-time migration: fix country field on contact docs |
| `app/gmail_outreach.py` | Send personalised outreach emails via Gmail OAuth |

---

## What the agent does

1. Loads country-specific search queries from `config/queries_<COUNTRY>.txt`.
2. Optionally scrapes curated agency directories (Clutch, Sortlist, DesignRush, GoodFirms, etc.).
3. Runs a GitHub organisation pre-pass to find agency orgs with a website.
4. Searches Bing + Brave (in parallel) and optionally Google Custom Search per query. Results are merged and de-duplicated. Brave requires `BRAVE_API_KEY` in `.env`; if not set it is silently skipped.
5. Filters candidate domains against a domain blocklist (`config/blocklist_domains.txt`).
6. Crawls each website and selected internal pages (contact, about, services, cases).
7. Extracts emails, phone numbers, contact pages, and LinkedIn company links.
8. Detects technologies: WordPress, WooCommerce, Webflow, Shopify, HubSpot, and more.
9. Classifies and scores each lead (0–100) for reseller fit.
10. Generates a suggested sales angle.
11. Writes results to Firestore in real time (one upsert per crawled site).
12. Exports to `output/agency_leads.xlsx`, `.csv`, and `.json`.

---

## Installation

```bash
pip install -r requirements.txt
```

Copy the example environment file and fill in your keys:

```bash
cp .env.example .env
```

---

## `lead_agent.py` — all parameters

| Parameter | Default | Description |
|---|---|---|
| `--mode` | `both` | `search` = Bing/Google keyword search; `catalog` = scrape directory listings; `both` = catalog first, then search; `audit` = run database cleanup passes (see below) |
| `--countries` | all configured | Comma-separated ISO codes, e.g. `NO,SE,DK` |
| `--queries` | _(per-country files)_ | Path to a custom queries file (overrides per-country files) |
| `--output` | `output` | Directory for Excel/CSV/JSON output files |
| `--max-results` | `200` | Max results per search engine per query (Bing, Brave, Google each) |
| `--min-score` | `50` | Minimum reseller score (0–100) to store a lead |
| `--max-pages` | `3` | Max pages to crawl per agency website |
| `--max-country` | `5000` | Stop a country once this many leads are found (0 = unlimited) |
| `--give-up-after` | `10` | Give up a country after this many consecutive empty queries |
| `--delay` | `1.0` | Seconds to wait between page fetches within one site |
| `--workers` | `20` | Parallel crawl workers / batch size |
| `--max-catalog-pages` | _(unlimited)_ | Limit pages per catalog source (useful for testing) |
| `--no-output` | off | Skip writing the Excel/CSV/JSON files |
| `--no-firebase` | off | Skip uploading results to Firestore |
| `--no-github` | off | Skip the GitHub org pre-pass |
| `--firebase-preload` | off | _(legacy flag, now always active)_ Preload seen domains from Firestore |
| `--firebase-collection` | `leads` | Override Firestore collection name |

### Audit mode (`--mode audit`)

Scans the entire `leads` collection and applies three cleanup passes. Always run with `--audit-dry-run` first.

```bat
python app\lead_agent.py --mode audit --audit-dry-run
python app\lead_agent.py --mode audit
```

| Pass | What it does |
|---|---|
| **Pass 1 — TLD corrections** | Leads with a ccTLD belonging to a different known country are re-assigned (`country` + `country_name` updated, original saved to `country_original`). Leads with a global TLD (`.com` / `.org` / `.net`, configurable in `countries.json` → `global_tlds`) are set to `country="*"` / `country_name="global"`. Leads with an unrecognised TLD not in `accepted_tlds` are deleted. |
| **Pass 2 — Contact audit** | Contacts with a blank or malformed email address are deleted. |
| **Pass 3 — Blocklist re-check** | Leads whose domain, website URL, company name, title, or description match the blocklist or content-negative keywords are deleted. |

### Discovery modes (`--mode`)

- **`search`** — runs Bing + Brave (+ optional Google CSE) queries in parallel per query, applies the full domain blocklist. Results are merged and de-duplicated before crawling.
- **`catalog`** — scrapes curated agency directories (Clutch, Sortlist, DesignRush, etc.); blocklist is **not** applied since catalog sources are already curated.
- **`both`** — catalog runs first, then search. Domains found in catalog phase are skipped during search.

### Example runs

```bash
# Norway only, both modes, stop at 200 leads
python app/lead_agent.py --countries NO --mode both --max-country 200

# Scandinavia search-only, 50 results per query
python app/lead_agent.py --countries NO,SE,DK --mode search --max-results 50

# Catalog only, first 5 pages per source (test run)
python app/lead_agent.py --mode catalog --max-catalog-pages 5 --no-github

# Full run, no output file, no Firebase (dry run)
python app/lead_agent.py --countries NO --no-output --no-firebase
```

---

## `lead_extract.py` — export a filtered extract from Firestore

Reads lead documents and their contacts sub-collections directly from Firestore and writes a focused Excel file. No local CSV is required. Global leads (`country="*"`) are excluded from all extracts.

```bat
python app\lead_extract.py [options]
```

### Filter parameters

| Parameter | Default | Description |
|---|---|---|
| `--collection` | `leads` | Firestore collection name |
| `--output` | `<project_root>/output` | Directory to write the Excel file |
| `--min-score` | `0` | Minimum reseller_score to include |
| `--max-score` | `100` | Maximum reseller_score to include |
| `--country CODE` | all | Country code(s), comma-separated or repeatable: `--country NO,SE` |
| `--source` | all | `search` / `catalog` / `both` — filter by discovery mode |
| `--query TEXT` | _(none)_ | Substring match on `source_query` (case-insensitive) |
| `--priority P` | all | Priority label(s), repeatable: `--priority A --priority B` |
| `--with-email` | off | Only include leads with at least one contact email |
| `--keywords KW` | _(none)_ | Comma-separated keywords (OR logic). A lead matches if **any** keyword appears in `source_query`, `title`, `description`, `company`, `domain`, `website`, `keywords`, or `reasons`. E.g. `--keywords wordpress,woocommerce` |
| `--limit N` | _(none)_ | Maximum number of leads to include. Applied after all other filters; stops the Firestore stream early once reached. |
| `--out FILE` | auto-timestamped | Output filename |

### Save-extract parameters

Saving an extract persists the filtered leads to a dedicated `leads_extract` Firestore collection. A lead can only belong to **one** extract — any lead already in a previous extract is automatically skipped (detected via a `collectionGroup` query on `leads_extracted`, no fields are written back to the main `leads` collection).

| Parameter | Default | Description |
|---|---|---|
| `--save-extract NAME` | _(none)_ | Save extract to `leads_extract/<NAME>` in Firestore |
| `--extract-dry-run` | off | Preview what `--save-extract` would write without touching Firestore |

### Firestore structure written by `--save-extract`

```
leads_extract/
  {extract_name}/
    name, created_at, lead_count, contact_count, filters{…}
    leads_extracted/
      {lead_id}/
        (all lead fields)
        contacts_extracted/
          {contact_id}/   (all contact fields)
```

### Output Excel sheets

- **Extract** — one row per email contact with all lead fields merged in; leads without email appear at the bottom.
- **Leads** — one row per lead (raw Firestore fields).
- **Summary** — filter criteria, counts, and extract name.

### Example runs

```bat
REM A-priority Norwegian leads with email, score ≥ 70
python app\lead_extract.py --min-score 70 --country NO --priority A --with-email

REM Catalog-sourced leads across Norway and Sweden
python app\lead_extract.py --source catalog --country NO,SE

REM All leads matching a specific query keyword
python app\lead_extract.py --query "webbyrå"

REM Keyword search — WordPress or WooCommerce leads
python app\lead_extract.py --keywords wordpress,woocommerce

REM Dry-run: preview what would be saved to Firestore
python app\lead_extract.py ^
  --keywords wordpress ^
  --country NO,SE ^
  --min-score 60 ^
  --save-extract "wordpress_nordic_may26" ^
  --extract-dry-run

REM Live save — writes to leads_extract/wordpress_nordic_may26
python app\lead_extract.py ^
  --keywords wordpress ^
  --country NO,SE ^
  --min-score 60 ^
  --save-extract "wordpress_nordic_may26"

REM Second extract — already-extracted leads are skipped automatically
python app\lead_extract.py ^
  --keywords shopify ^
  --country NO,SE ^
  --save-extract "shopify_nordic_jun01"
```

### Function API

```python
from extract_leads import extract_leads

path = extract_leads(
    min_score=70,
    countries=["NO", "SE"],
    source="search",
    priorities=["A", "B"],
    with_email=True,
    keywords=["wordpress", "woocommerce"],
    save_extract="wordpress_nordic_may26",
    extract_dry_run=False,
    out_file="my_extract.xlsx",
)
```

---

## `gmail_outreach.py` — send outreach emails via Gmail

Sends personalised outreach emails to contacts from an Excel file, tracks opens and replies, and avoids re-sending to anyone who has already replied.

```bash
cd app
python gmail_outreach.py [options]
```

Requires Gmail OAuth credentials (`credentials.json` in the project root). On first run the browser opens for authorisation; a `token.json` is saved for future runs.

---

## Output files

After a run, the `output/` directory contains:

| File | Contents |
|---|---|
| `agency_leads.xlsx` | Leads sheet + Contacts sheet + Dashboard + Queries |
| `agency_leads.csv` | All leads, one row per lead |
| `agency_contacts.csv` | All contacts, one row per email address |
| `agency_leads.json` | All leads as JSON |
| `agency_contacts.json` | All contacts as JSON |
| `extract_leads_*.xlsx` | Filtered extracts produced by `lead_extract.py` |
| `output/<campaign_id>/campaign.xlsx` | Campaign export (Summary + Leads + Contacts sheets) |
| `output/<campaign_id>/campaign.json` | Same campaign data as JSON |

### Key columns in leads

| Column | Description |
|---|---|
| `company` | Agency name (derived from domain) |
| `website` | Canonical website URL |
| `country` / `country_name` | ISO code and full name |
| `emails` | Comma-separated email addresses |
| `phones` | Comma-separated phone numbers |
| `linkedin` | LinkedIn company page URL |
| `detected_tech` | Technologies detected on the site |
| `categories` | Agency category labels |
| `reseller_score` | Fit score 0–100 |
| `priority` | A / B / C based on score |
| `reasons` | Scoring rationale |
| `suggested_angle` | Recommended BlueSearch sales angle |
| `found_by_search` | `yes` if discovered via keyword search |
| `found_by_catalog` | `yes` if discovered via directory catalog |
| `crawled_at` | ISO timestamp of last crawl |

---

## Configuration files

| File | Purpose |
|---|---|
| `config/countries.json` | Per-country settings: language, TLDs, keywords, phone region |
| `config/queries_<CODE>.txt` | Search queries per country |
| `config/blocklist_domains.txt` | Domain glob patterns and content negative keywords to exclude |
| `config/catalogs.json` | Directory catalog sources and page counts per country |

### `config/blocklist_domains.txt`

Single source of truth for all domain filtering. Contains two types of entries:

- **Domain glob patterns** (e.g. `*pizza*`, `*hotel*`) — any domain matching a pattern is skipped during search.
- **Content negative keywords** (under the `CONTENT NEGATIVE KEYWORDS` section) — plain substrings checked against visible page text during scoring; two or more occurrences trigger a score penalty.

Blocklist filtering applies to **search mode only**. Catalog sources are pre-curated and bypass the blocklist.

---

## Firebase / Firestore

Results are written to Firestore in real time as each site is crawled. Structure:

```
leads/{lead_id}                                                  — one document per agency
leads/{lead_id}/contacts/{id}                                    — one document per email address

leads_extract/{extract_name}                                     — one document per named extract
leads_extract/{extract_name}/leads_extracted/{lead_id}           — extracted lead snapshot
leads_extract/{extract_name}/leads_extracted/{lead_id}/
    contacts_extracted/{contact_id}                              — extracted contact snapshot
```

The `leads_extract` collection is populated by `lead_extract.py --save-extract`. A lead can belong to at most one extract; duplicates are detected via a `collectionGroup` query on `leads_extracted` before each run.

Credentials are loaded from (in order):

1. `blueboot_secrets.py` in the project root (`fireBaseAdminKey` dict)
2. `FIREBASE_CREDENTIALS` environment variable (path to a service account JSON)
3. `config/serviceAccountKey.json`

At startup, already-crawled domains are preloaded from Firestore so they are never re-crawled within the same run or across runs.

---

## Optional Google Search API

The agent works without API keys using Bing as fallback. For more stable results, add Google Custom Search credentials to `.env`:

```env
GOOGLE_API_KEY=your_key
GOOGLE_CSE_ID=your_cse_id
```

---

## Notes

- Use reasonable rate limits (`--delay`, `--workers`) and only collect public business contact information.
- This agent is designed for B2B lead research, not aggressive scraping or spam.
- `blueboot_secrets.py` is never committed to version control.

---

---

## `lead_enrich_agent.py` — AI classification for leads collection

Reads documents from the `leads` collection and runs GPT classification to determine the agency type, specialisation, client base, and reseller potential. Uses the same batch/concurrent pattern as `site_enrich_agent.py`.

New fields written to each `leads/{id}` document:

| Field | Description |
|---|---|
| `ai_sector` | Agency category: `web_agency`, `seo_agency`, `design_agency`, `marketing_agency`, `hosting_provider`, `ecommerce_agency`, `communication_agency`, `it_consulting`, `pr_agency`, `media_agency`, `other` |
| `ai_specialisation` | Array of service tags, e.g. `["wordpress", "woocommerce", "seo"]` |
| `ai_client_base` | Primary client type: `SMB`, `enterprise`, `mixed`, `local`, `unknown` |
| `ai_reseller_potential` | Reseller fit: `high`, `medium`, `low` |
| `ai_platform` | Detected CMS/site builder |
| `ai_summary` | One-sentence agency description |
| `ai_confidence` | GPT confidence score (0.0–1.0) |
| `ai_classified_at` | ISO timestamp of classification run |

```bat
python app\lead_enrich_agent.py --countries NO
python app\lead_enrich_agent.py --countries NO,SE --force
python app\lead_enrich_agent.py --limit 200 --dry-run
```

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `--collection NAME` | `leads` | Firestore collection to classify |
| `--countries CODES` | all | Comma-separated country codes, e.g. `NO,SE` |
| `--limit N` | _(none)_ | Maximum number of leads to classify |
| `--batch-size N` | `10` | Leads per GPT batch |
| `--concurrent N` | `3` | Parallel GPT batch workers |
| `--dry-run` | off | Print results without writing to Firestore |
| `--force` | off | Re-classify leads that already have `ai_classified_at` set |

## `lead_enrich_contacts.py` — social media profile enrichment

Reads contact documents from Firestore and adds personal social media profile links. Searches Bing in parallel for LinkedIn, Twitter/X, Facebook, Instagram and Telegram profiles; derives a WhatsApp deep-link from the contact's phone number without any search.

New fields written to each `contacts/{id}` document:

| Field | Source |
|---|---|
| `linkedin_personal` | Bing: `"Name" "Company" site:linkedin.com/in/` |
| `twitter` | Bing: `"Name" "Company" site:twitter.com OR site:x.com` |
| `facebook` | Bing: `"Name" "Company" site:facebook.com` |
| `instagram` | Bing: `"Name" "Company" site:instagram.com` |
| `telegram` | Bing: `"Name" "Company" site:t.me` |
| `whatsapp` | Derived from `phone` → `https://wa.me/{e164}` |
| `social_enriched_at` | ISO timestamp of enrichment run |

Only contacts with a valid email address and at least a name or phone number are processed. Already-populated fields are never overwritten.

```bat
cd app
python lead_enrich_contacts.py [options]
```

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `--collection NAME` | `leads` | Firestore leads collection |
| `--country CODE` | all | Country code(s), comma-separated or repeatable: `--country NO,SE` |
| `--limit N` | _(none)_ | Maximum number of contacts to process |
| `--workers N` | `50` | Parallel async workers (Bing searches run concurrently) |
| `--delay SECS` | `1.0` | Seconds to wait between Bing searches per worker |
| `--skip-enriched` | off | Skip contacts that already have `social_enriched_at` set |
| `--platforms LIST` | all | Comma-separated subset: `linkedin,twitter,facebook,instagram,telegram,whatsapp` |
| `--dry-run` | off | Print what would be written without touching Firestore |

### How parallelism works

Contacts are first filtered synchronously from Firestore. All filtered contacts are then enriched concurrently using `asyncio.gather` capped by a semaphore of `--workers`. Results are batch-written to Firestore after all workers finish. With 20 workers and 5 platforms per contact, throughput is roughly 20× faster than a sequential run.

### Example runs

```bat
REM Preview first — no writes
python app\lead_enrich_contacts.py --country NO --limit 50 --dry-run

REM LinkedIn + WhatsApp only, Norway and Sweden, skip already enriched
python app\lead_enrich_contacts.py --country NO,SE --platforms linkedin,whatsapp --skip-enriched

REM Full run, all platforms
python app\lead_enrich_contacts.py --country NO,SE,DK

REM Reduce concurrency if Bing starts rate-limiting
python app\lead_enrich_contacts.py --workers 10 --delay 2.0
```

---

## `fix_contact_country.py` — one-time country field migration

Fixes an earlier data issue where contact documents stored the full country name (e.g. `"Norway"`) in the `country` field instead of the ISO code (`"NO"`). After this migration every contact document has:

- `country` — ISO code, e.g. `"NO"`
- `country_name` — full name, e.g. `"Norway"`

The script loads all lead documents into memory first (to get the correct `country` / `country_name` values), then streams all contacts via a `collectionGroup` query and batch-writes the corrected fields. Contacts whose fields are already correct are skipped.

```bat
REM Preview — no writes
python app\fix_contact_country.py --dry-run

REM Live run
python app\fix_contact_country.py
```

| Parameter | Default | Description |
|---|---|---|
| `--collection NAME` | `leads` | Firestore leads collection |
| `--dry-run` | off | Print what would be changed without writing |

This script only needs to be run once on existing data. All new contacts written by the crawler and catalog scraper now include both fields correctly.

---

---

## `campaign_exporter.py` — export a campaign to Excel + JSON

Reads a named campaign from the `leads_extract` Firestore collection (populated by `lead_extract.py --save-extract`) and writes two files to `output/<campaign_id>/`:

| File | Contents |
|---|---|
| `campaign.xlsx` | Four sheets: Summary, Campaign, Leads, Contacts |
| `campaign.json` | Full campaign payload (schema_version, campaign, leads, contacts) |

```bat
:: List all saved campaigns
python app\campaign_exporter.py --list

:: Export a specific campaign
python app\campaign_exporter.py NO_high_score_may26

:: Export to a custom directory
python app\campaign_exporter.py NO_high_score_may26 --output exports\no_may26
```

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `campaign_id` | _(required)_ | Firestore document ID under `leads_extract/` |
| `--list` | off | List all available campaign IDs and exit |
| `--output DIR` | `output/<campaign_id>/` | Custom output directory |

## `statistics.py` — lead statistics & Firestore aggregations

Reads all leads and contacts from Firestore, computes aggregated statistics, writes results back to a `statistics` collection, and exports Excel reports to `output/`.

```bash
cd app
python statistics.py [options]
```

Runs **both** aggregations by default. Use `--only` to target one.

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `--leads-collection` | `leads` | Firestore collection to read leads from |
| `--stats-collection` | `statistics` | Firestore collection to write statistics into |
| `--output` | `output/` | Directory for Excel output files |
| `--only` | _(both)_ | `priority` or `reasons` — run only one aggregation |
| `--no-excel` | off | Skip writing Excel files |
| `--no-writeback` | off | Skip writing `reasons-list` back to each lead document |

### Aggregation 1 — Priority × Country

Counts leads and contacts per country, broken down by priority (A / B / C / unset).

Firestore structure written:

```
statistics/priority-pr-country               ← head document (grand totals + by_priority summary)
statistics/priority-pr-country/countries/NO  ← one sub-document per country
statistics/priority-pr-country/countries/SE
...
```

Head document fields: `generated_at`, `total_leads`, `total_contacts`, `country_codes`, `by_priority`.

Each country sub-document fields: `country`, `country_name`, `total_leads`, `total_contacts`, `by_priority`.

Excel output: `output/statistics.xlsx` — sheets **Summary**, **By Priority**, **By Country**, **Country x Prio**.

### Aggregation 2 — Reasons Count

Parses each lead's `reasons` field (`;`-separated signals, `:`-separated label/detail, `/`-separated compound labels) into individual reason tokens, counts occurrences per country, and optionally writes the parsed list back to each lead.

Delimiters applied in order:
- `;` — separates distinct reason groups
- `:` — strips detail, keeps label (`"wordpress: site, plugins"` → `"wordpress"`)
- `/` — expands compound labels (`"has services/customers/cases language"` → `"has services"`, `"customers"`, `"cases language"`)

Firestore structure written:

```
statistics/reasons-count               ← head document (global reason counts)
statistics/reasons-count/countries/NO  ← one sub-document per country
statistics/reasons-count/countries/SE
...
```

Reason counts are stored as a list of `{reason, count}` objects sorted by count descending.

Each lead document is also updated with a `reasons-list` field (array of parsed reason strings) unless `--no-writeback` is passed.

Excel output: `output/statistics_reasons.xlsx` — sheets **Global Reasons**, **By Country**.

### Example runs

```bash
# Run both aggregations (default)
python statistics.py

# Priority stats only, no Excel
python statistics.py --only priority --no-excel

# Reasons count only, skip writing back to leads
python statistics.py --only reasons --no-writeback

# Write to a non-default stats collection
python statistics.py --stats-collection statistics_test
```

### Function API

```python
from statistics import summarise_country_pr_priority, summarise_reasons_count
from statistics import export_to_excel, export_reasons_to_excel

# Priority aggregation
results = summarise_country_pr_priority(leads_collection="leads")
export_to_excel(results, outdir="output")

# Reasons aggregation (writeback on by default)
results = summarise_reasons_count(leads_collection="leads", writeback=True)
export_reasons_to_excel(results, outdir="output")
```
