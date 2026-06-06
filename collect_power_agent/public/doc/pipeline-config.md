# Pipeline Configuration

The two discovery pipelines are controlled by configuration files in the `config/` directory. Each file is clearly owned by one pipeline — editing the wrong file has no effect on the other.

---

## Site Pipeline configuration

> **Scripts that use these files:** `site_agent.py`, `site_enrich_agent.py`, `site_smart_export.py`, `build_filter_facets.py`
> **Target:** Content-heavy commercial websites (municipalities, healthcare, ecommerce, media, companies)

---

### `config/site_agent_queries.json`

The primary search configuration for the site pipeline. Controls what Bing searches are run per country to discover candidate websites.

**Top-level structure:**

```json
{
  "_comment": "...",
  "NO": { ... },
  "SE": { ... },
  "UK": { ... }
}
```

**Per-country fields:**

| Field | Description |
|---|---|
| `name` | Human-readable country name |
| `language` | Primary language code (`no`, `sv`, `en`, …) |
| `accept_language` | HTTP `Accept-Language` header value |
| `description` | What kind of sites this country config targets |
| `min_pages` | Minimum sitemap page count to accept a site (e.g. `50`) |
| `target_types` | List of accepted site types — used for AI classification alignment |
| `query_categories` | Map of category name → list of Bing search strings |

**Query categories** represent the types of sites being targeted. Each category holds a list of Bing queries in the country's language:

```json
"query_categories": {
  "municipality":  ["kommune.no tjenester innbyggere", "norsk kommune selvbetjening ..."],
  "healthcare":    ["sykehus pasient informasjon søk", ...],
  "education":     ["videregående skole elever fagplaner søk", ...],
  "media":         ["nettavis nyheter søk artikler", ...],
  "company":       ["norsk bedrift produkter kunder tjenester søk", ...],
  "shop":          ["nettbutikk produkter varer kjøp", ...],
  "association":   ["norsk forening medlemmer arrangementer", ...],
  "finance":       [...],
  "legal":         [...],
  "real_estate":   [...],
  "construction":  [...],
  "tech":          [...],
  "hr":            [...],
  "hospitality":   [...]
}
```

**Countries currently configured:** NO, SE, DK, DE, UK, FI, NL, FR, EU, IN, BE

**To add a new country:** copy an existing country block, translate the queries, set the correct `language`, `accept_language`, and `min_pages`.

**To add queries to a category:** append to the list under the relevant `query_categories` key. Each string is sent as a separate Bing search.

---

### `config/site_agent_blocklist.txt`

A **lean** blocklist of domains the site agent must never crawl or store. 385 entries.

This list is intentionally much smaller than `blocklist_domains.txt` (the lead pipeline blocklist) because the site pipeline **wants** municipalities, hospitals, and universities — which the broader blocklist would wrongly exclude.

**Categories in the file:**

- Social media platforms (facebook.*, instagram.com, linkedin.*, tiktok.*, …)
- Major cloud/hosting infrastructure (aws.amazon.com, azure.com, …)
- Obvious irrelevant domains (google.*, bing.com, wikipedia.org, …)
- Known bot-detection / scraping-hostile domains

**Syntax:** one domain per line, glob wildcards supported (`facebook.*` matches all TLDs).

**When to add entries:** when `site_agent.py` repeatedly discovers a domain that is clearly out of scope (a CDN, a social network, a search engine), add it here. Do **not** add sectors or site types — use `min_pages` and `target_types` in `site_agent_queries.json` for that.

---

## Lead Pipeline configuration

> **Scripts that use these files:** `lead_agent.py`, `lead_enrich_agent.py`, `leads_smart_export.py`, `wp_plugin_leads.py`
> **Target:** Web agencies, digital resellers, WordPress/Shopify developers

---

### `config/countries.json`

The primary search and validation configuration for the **lead pipeline**. Controls what keywords identify a site as a web agency, what Bing queries find them, and which TLDs are accepted.

**Top-level structure:**

```json
{
  "global_tlds": [".com", ".org", ".net", ".eu"],
  "NO": { ... },
  "SE": { ... },
  ...
}
```

**Per-country fields:**

| Field | Description |
|---|---|
| `name` | Country name |
| `tlds` | Country-specific TLDs (e.g. `[".no"]`) |
| `accepted_tlds` | All TLDs considered valid for this country |
| `phone_region` | ISO region for phone number parsing |
| `accept_language` | HTTP header value |
| `queries` | Bing search strings to find agencies in this country |
| `keywords` | Map of keyword groups used to classify a site as an agency |
| `agency_words` | Phrases found on agency sites ("we build websites", "our clients") |
| `service_words` | Service-related terms (used in scoring) |
| `support_words` | Support/contact page signals |
| `contact_words` | Contact page indicators |

**Keyword groups** (`keywords` field):

| Group | Purpose |
|---|---|
| `web_agency` | Core agency terms — webdesign, nettside, webutvikling, … |
| `wordpress` | WordPress/WooCommerce specialist signals |
| `seo` | SEO/SEM agency indicators |
| `communication` | PR/communication agency signals |
| `public_sector` | Terms suggesting a public-sector focus |
| `ai_interest` | AI/ML service indicators |
| `smb_focus` | Signals the agency targets SMBs |
| `care_plan` | Maintenance/care plan offering signals |

**Countries currently configured:** NO, SE, DK, FI, NL, BE, DE, AT, UK, IE, FR, ES, IT, PL, HU, EE, LV, LT, IN, TN, TH, BR, AR, EU

**To add a new country:** copy an existing entry, translate `queries`, `agency_words`, and `keywords` to the local language, and set `tlds` and `accepted_tlds`.

---

### `config/catalogs.json`

A curated list of agency directory websites to scrape per country — Sortlist, DesignRush, Proff, etc. Used by `lead_agent.py` alongside Bing search results.

**Structure:**

```json
{
  "_notes": ["Sortlist: JS infinite-scroll — URL pagination does not work...",
             "Clutch: Cloudflare WAF blocks all requests. Removed from all countries.",
             "GoodFirms: WAF drops connection after ~20s. Removed from all countries."],
  "NO": [
    { "name": "Sortlist NO – web design",
      "type": "sortlist",
      "url":  "https://www.sortlist.com/web-design/norway-no?page={page}",
      "pages": 10 },
    { "name": "DesignRush NO",
      "type": "designrush",
      "url":  "https://www.designrush.com/agency/website-design/no?page={page}",
      "pages": 5 },
    ...
  ]
}
```

**Catalog entry fields:**

| Field | Description |
|---|---|
| `name` | Human-readable name for logging |
| `type` | Scraper type: `sortlist`, `designrush`, `dan`, `topdevelopers`, `proff`, `gulesider`, `generic` |
| `url` | URL template — `{page}` is replaced by page number |
| `pages` | How many pages to scrape (scraping stops on 404 or empty result) |

**`_notes` field** documents catalogs that were investigated but removed due to technical blockers:
- **Clutch** — Cloudflare WAF blocks all requests (403). Removed from all countries.
- **GoodFirms** — WAF drops connection after ~20s timeout. Removed from all countries.
- **Sortlist** — JS infinite-scroll; URL pagination does not work; scrapes ~20 top agencies only.

**Catalog types currently used by country (Norway example):** sortlist, designrush, dan, topdevelopers, gulesider, proff, generic (39 entries total for NO)

**To add a new catalog:** add an entry with the correct `type` and a paginated `url`. If the type is new, a corresponding scraper case must be added in `lead_agent.py`.

---

### `config/blocklist_domains.txt`

A **broad** domain blocklist for the lead pipeline. 992 entries across many categories.

**This is not used by the site pipeline** — the site pipeline has its own leaner blocklist (`site_agent_blocklist.txt`) so it can still discover municipalities and public institutions.

**Blocked categories:**

- Social media platforms
- Search engines (google.*, bing.com, …)
- Marketplaces (amazon.*, ebay.*, etsy.com, …)
- News aggregators and global media
- Government portals (gov.uk, regjeringen.no, …)
- Cloud/SaaS platforms that aren't web agencies
- Known SEO tools, job boards, and listing aggregators

**Syntax:** one domain per line, glob wildcards supported (`facebook.*`).

**When to add entries:** when `lead_agent.py` discovers a domain that is clearly not a web agency (a bank, a government portal, a SaaS company), add it here.

---

### `config/wp_plugin_queries.json`

Configuration for `wp_plugin_leads.py` — discovers leads from the WordPress.org plugin catalogue.

**Structure:**

```json
{
  "_comment":       "WordPress Plugin Catalogue lead config.",
  "_tld_strict_note": "tld_strict=true: hard-filter by TLD (UK/IN/AU). tld_strict=false: flag tld_match only (NO/SE/DK/FI).",
  "blocked_domains": ["wordpress.org", "wordpress.com", "github.com", ...],
  "countries": {
    "NO": {
      "label":      "Norway",
      "tlds":       [".no"],
      "tld_strict": false,
      "terms":      ["...]
    },
    "UK": {
      "label":      "United Kingdom",
      "tlds":       [".co.uk", ".uk"],
      "tld_strict": true,
      "terms":      [...]
    }
  }
}
```

**Key fields:**

| Field | Description |
|---|---|
| `blocked_domains` | Domains to skip (core WordPress infrastructure, GitHub, etc.) |
| `tld_strict` | `true` = only accept domains with matching TLD; `false` = accept all, flag `tld_match` in output |
| `terms` | Search terms used to find relevant plugin authors (agency-related keywords) |

**Countries currently configured:** UK, IN, NO, SE, DK, FI, AU, NZ

---

## Configuration files at a glance

| File | Pipeline | Purpose |
|---|---|---|
| `site_agent_queries.json` | **Site pipeline** | Bing search queries + target types per country |
| `site_agent_blocklist.txt` | **Site pipeline** | Lean domain blocklist (385 entries) |
| `countries.json` | **Lead pipeline** | Agency keywords + Bing queries per country |
| `catalogs.json` | **Lead pipeline** | Agency directory sites to scrape per country |
| `blocklist_domains.txt` | **Lead pipeline** | Broad domain blocklist (992 entries) |
| `wp_plugin_queries.json` | **Lead pipeline** | WordPress plugin catalogue discovery config |
