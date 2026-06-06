# AI Assistance

AI plays two distinct roles in the Blueboot CRM system:

1. **Internal enrichment** — GPT-5.4 runs automatically during the pipeline to classify and enrich discovered data.
2. **Setup generation** — the same AI was used to create the search query files, catalog lists, and keyword configurations that drive both pipelines.

---

## Part 1 — How AI enriches pipeline data

### Site pipeline

#### `site_enrich_agent.py` — Website classification

For every discovered website, GPT receives the site's URL, title, meta description, and extracted keywords and returns structured JSON:

**System prompt (summary):**
> You are a website classifier. Analyse each site and return: `sector`, `company_type`, `country`, `keywords` (up to 25), `summary` (max 20 words), `platform` (CMS/site builder), `hosting` (provider), `contacts` (names/emails found on site), `confidence` (0.0–1.0). Return ONLY a valid JSON array.

**Fields GPT writes to `site_leads`:**

| Field | What GPT decides |
|---|---|
| `ai_sector` | `municipality` / `healthcare` / `ecommerce` / `technology` / `media` / … |
| `ai_company_type` | `B2B` / `B2C` / `government` / `NGO` / `association` / … |
| `ai_country` | ISO country code inferred from TLD, language, phone prefix, address mentions |
| `ai_platform` | `WordPress` / `WooCommerce` / `Shopify` / `Webflow` / `custom` / … |
| `ai_keywords` | Cleaned, deduped, normalised keyword list |
| `ai_confidence` | 0.0–1.0 certainty score |

Batch size: configurable (default 20 sites per API call). The agent runs async with a bounded worker pool — one timeout cannot stall the pipeline.

---

#### `site_email_check.py` — Contact classification

For every contact with an email address, GPT receives the email, name, title, and domain and classifies:

**System prompt (summary):**
> You receive contact records. For each one return: `email_type` (personal/role/department/admin), `contact_type` (decision_maker/marketing/developer/sales/operations/unknown), `outreach_priority` (1=best to 4=lowest), `reasoning` (one short sentence). Return ONLY valid JSON.

**Outreach priority logic GPT applies:**
- **P1** — personal email + decision_maker or marketing role
- **P2** — personal email + other role, OR role email + decision_maker
- **P3** — role/department email + non-admin type
- **P4** — admin email or unknown contact type

Batch size: 50 contacts per API call.

---

#### `site_location_enrich.py` — Location resolution

GPT maps `ai_country` to a standardised `"City, Country"` location string used by the filter facets.

---

### Lead pipeline

#### `lead_enrich_agent.py` — Agency classification

The lead pipeline discovers agencies through two channels — Bing search and agency catalog services (Sortlist, DesignRush, Proff, DAN, TopDevelopers, etc.) — before AI enrichment runs. Both sources are deduplicated by domain. The same enrichment pattern then applies to all discovered agencies. GPT classifies each agency by sector, reseller potential score, and company type.

#### `leads_email_check.py` — Contact classification

Identical prompt structure as `site_email_check.py`, applied to contacts in the `leads/{id}/contacts/` subcollection.

---

## Part 2 — How AI generated the pipeline configuration

The query files, blocklists, and keyword configurations were not written by hand — they were generated with OpenAI and refined through iteration. The same approach can be used to extend or modify them.

### What was AI-generated

| File | What AI created |
|---|---|
| `config/site_agent_queries.json` | All Bing search queries per country and category (municipality, healthcare, education, media, ecommerce, …) |
| `config/countries.json` | Per-country agency keyword groups (`web_agency`, `wordpress`, `seo`, …), `agency_words`, `queries` |
| `config/catalogs.json` | The list of agency directory URLs per country (Sortlist, DesignRush, Proff, etc.) with pagination patterns |
| `config/wp_plugin_queries.json` | Search terms for discovering WordPress plugin authors per country |

---

## Part 3 — Using AI to extend the configuration

### Adding a new country to the site pipeline

Paste this prompt into ChatGPT or the API:

```
You are configuring the Blueboot site discovery pipeline for a new country.
The pipeline uses Bing to find content-heavy websites (municipalities, healthcare,
education, ecommerce, media, companies) that could benefit from an AI-powered
internal search widget.

Generate a new country entry for: [COUNTRY NAME]

Return a JSON object matching this structure exactly:
{
  "name": "...",
  "language": "...",               // primary language code
  "accept_language": "...",        // HTTP Accept-Language header
  "description": "...",            // what kinds of sites this country targets
  "min_pages": 50,
  "target_types": [...],           // subset of: municipality, public_sector, healthcare,
                                   //   education, media, company, ecommerce, association,
                                   //   finance, legal, real_estate, logistics,
                                   //   construction, tech, hr, hospitality
  "query_categories": {
    "municipality": [...],         // 8-12 Bing queries in the local language
    "healthcare":   [...],
    "education":    [...],
    "media":        [...],
    "company":      [...],
    "shop":         [...],
    "association":  [...],
    "finance":      [...],
    "legal":        [...],
    "real_estate":  [...],
    "construction": [...],
    "tech":         [...],
    "hr":           [...],
    "hospitality":  [...]
  }
}

Rules:
- Each query array should contain 8-12 Bing search strings in the LOCAL language.
- Queries should find sites with lots of pages/content where visitors need to search.
- Avoid queries that would return social media, news aggregators, or government
  policy sites that have no practical search need.
- min_pages: use 50 for smaller countries, 100 for larger ones.
```

Paste the result directly into `config/site_agent_queries.json` under the new country key.

---

### Adding a new country to the lead pipeline

```
You are configuring the Blueboot lead discovery pipeline for a new country.
The pipeline finds web agencies and digital resellers (companies that build
websites and could resell AI search services to their clients).

Generate a new country entry for: [COUNTRY NAME]

Return a JSON object matching this structure:
{
  "name": "...",
  "tlds": [...],                   // country-specific TLDs, e.g. [".no"]
  "accepted_tlds": [...],          // all valid TLDs including .com, .io, .agency, etc.
  "phone_region": "...",           // ISO region code for phone parsing
  "accept_language": "...",
  "queries": [...],                // 10-15 Bing search strings to find agencies
  "keywords": {
    "web_agency":    [...],        // terms on agency homepages
    "wordpress":     [...],        // WordPress/WooCommerce specialist terms
    "seo":           [...],        // SEO/SEM agency terms
    "communication": [...],        // PR/communication agency terms
    "public_sector": [...],
    "ai_interest":   [...],
    "smb_focus":     [...],
    "care_plan":     [...]         // maintenance/care plan offering terms
  },
  "agency_words":  [...],          // phrases found on agency sites
  "service_words": [...],
  "support_words": [...],
  "contact_words": [...]
}

Rules:
- All terms in the local language of the country.
- queries: Bing search strings that would return agency websites, not directories.
- keywords: words/phrases found ON the agency website's homepage or services page.
- agency_words: short phrases that signal "we build websites for clients".
```

---

### Adding catalog sources for a new country

```
You are adding agency directory sources for [COUNTRY NAME] to the Blueboot lead pipeline.

The pipeline scrapes paginated agency directories to find web agencies and resellers.
Generate a JSON array of catalog entries for this country.

Each entry must follow this schema:
{
  "name":  "...",          // human-readable label for logging
  "type":  "...",          // one of: sortlist, designrush, dan, topdevelopers,
                           //   gulesider, proff, generic
  "url":   "...",          // URL template — use {page} where the page number goes
  "pages": 10              // how many pages to try (set high; scraper stops on 404)
}

Rules:
- Only include directories that list agencies by country (not just global lists).
- Prefer directories with clean URL pagination (?page=N or /page/N).
- Do NOT include Clutch (Cloudflare WAF) or GoodFirms (WAF timeout).
- For "type": use "sortlist" for Sortlist, "designrush" for DesignRush,
  "proff" for proff.no-style business registries, "generic" for anything else.
- Aim for 5-15 sources covering different segments (web design, development, digital marketing).
```

---

### Adding new query categories to an existing country

```
Add [N] new Bing search query strings to the "[CATEGORY]" category for [COUNTRY].
Existing queries are: [paste current list]

The queries should find [CATEGORY] websites in [COUNTRY] that:
- Have a lot of content pages (50+)
- Have a real internal search need (users need to find things)
- Are NOT social media, news aggregators, or pure government policy sites

Return ONLY a JSON array of new query strings in [LANGUAGE].
Avoid duplicating the meaning of existing queries.
```

---

## Tips for AI-assisted config editing

- **Be specific about the country and language** — generic prompts produce generic queries.
- **Paste existing examples** — ask the model to match the style and density of existing entries.
- **Request JSON only** — add "Return ONLY valid JSON, no markdown" to every prompt.
- **Iterate** — run `site_agent.py --dry-run` or `lead_agent.py --dry-run` with the new config and check what comes back before a full crawl.
- **Blocklist additions** — paste a list of bad domains discovered during a run and ask GPT to identify the pattern (e.g. "these are all job boards") so you can add the right wildcard entries.
- **Use the `_comment` and `_notes` fields** — they are read by humans and AI alike; keep them updated when you add entries so future AI-assisted edits have context.
