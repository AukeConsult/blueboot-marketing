# Prospect Catalogue Sync

How the BlueSearch prospect spreadsheets are loaded into campaigns, and kept up to
date as the research team adds or changes rows.

The command is `app/prospects_import.py` (launchers: `run_prospects_import.bat` /
`.sh`). The logic lives in `functions-crm/crm/prospect_import_lib.py`.

---

## What it does

The catalogue is a folder of Excel files, one per country plus an "international"
file:

```
G:\Shared drives\BlueBoot R&D\marketing\prospects\
    BlueSearch-Denmark-prospects.xlsx
    BlueSearch-Finland-prospects.xlsx
    BlueSearch-Germany-prospects.xlsx
    BlueSearch-Sweden-prospects.xlsx
    BlueSearch-UK-prospects.xlsx
    BlueSearch-US-expanded-prospects.xlsx
    BlueSearch-international-prospects.xlsx
```

Each run reads every `.xlsx` in the folder, groups the rows by **country**, and
brings Firestore in line with them:

| Situation | Result |
|---|---|
| Row not in Firestore yet | **new** — created |
| Row exists, a catalogue field differs | **changed** — only the differing fields are updated |
| Row exists and is identical | **unchanged** — left alone |
| Row removed from the catalogue | reported only — **never deleted** |

It is safe to run as often as you like. Run it again after the research team has
edited a file and only the differences are written.

---

## Dry run first

The command **writes nothing unless you pass `--apply`**. The default run reads the
files, compares them with Firestore and prints a report:

```
== BS_UK (exists)
   campaign_leads      1 new   0 chg  29 same
   campaign_contacts   1 new   1 chg  21 same
   site_leads          1 new   0 chg  29 same
   site_contacts       1 new   1 chg  21 same
   email_contacts      1 new   1 chg  21 same
   7 row(s) without email -> not imported
   ~ contact piers_atomicsmash_co_uk: title: 'Managing director' -> 'CEO'
```

Read it, then re-run with `--apply`.

---

## Usage

```
run_prospects_import.bat                      preview everything
run_prospects_import.bat --apply              write to Firestore
run_prospects_import.bat --country UK,DK      only these countries
run_prospects_import.bat --file BlueSearch-UK-prospects.xlsx
run_prospects_import.bat --campaign-only      skip site_leads / site_contacts / email_contacts
run_prospects_import.bat --show-changes 50    list more field-level changes
```

(or `python app/prospects_import.py ...`; on Linux/macOS `./run_prospects_import.sh ...`)

| Option | Meaning |
|---|---|
| `--apply` | Write to Firestore. Without it nothing is changed. |
| `--dir PATH` | Folder with the `.xlsx` files. **Required** (no built-in default) unless `--file` gets full paths; can also be set once as `PROSPECTS_DIR` in `.env`/environment, e.g. `PROSPECTS_DIR=G:\Shared drives\BlueBoot R&D\marketing\prospects`. |
| `--file NAME` | Only this file (name inside `--dir`, or a full path). Can be repeated. |
| `--country CC,CC` | Only these country codes, for example `UK,DK`. |
| `--prefix BS` | Campaign prefix. Campaigns are named `<prefix>_<CC>`. Default `BS`. |
| `--campaign ID` | Put **all** rows in one campaign instead of one per country. |
| `--campaign-only` | Write only the campaign documents (see below). |
| `--show-changes N` | Show up to N field-level changes per campaign. Default 10. |

The script needs the same `.env` and Firestore credentials as the other `app/`
scripts.

---

## Campaigns

One campaign per country, created as a **draft** the first time it is needed:

| Country in the sheet | Campaign |
|---|---|
| Denmark | `BS_DK` |
| Finland | `BS_FI` |
| Germany | `BS_DE` |
| Sweden | `BS_SE` |
| United Kingdom | `BS_UK` |
| United States / "US" / "US and Canada" | `BS_US` |

Rows are grouped by the **Country** column, not by file, so the international file
is split into the right country campaigns automatically. If the same company or
email appears in two files, the two rows are merged into one record (the newer
*Research date* wins, and empty cells never overwrite filled ones).

A new campaign has no mail text or sending account yet. Set those on the campaign
page before sending.

---

## Where each column goes

### Lead — `campaigns/{id}/campaign_leads/{lead_id}`

| Spreadsheet column | Field |
|---|---|
| Site | `website`, `domain`, and the document ID `lead_id` (`https://www.dude.fi` → `www_dude_fi`) |
| Company | `company` |
| Country | `country` (code) and `country_name` |
| City / state | `location` |
| Prospect type | `prospect_type` (Agency partner / Direct buyer) |
| Priority | `priority` — High / Medium / Exploratory. A number (1, 2, 3 …) becomes `High`, and the number is kept in `priority_rank`. |
| Why BlueSearch could fit | `description`, and `summary` (with the sales angle) |
| Suggested sales angle | `suggested_angle` |
| Source URLs | `source_urls` |
| Research date | `researched_at` |
| Notes | `notes` |

### Contact — `campaigns/{id}/campaign_contacts/{doc_id}`

| Spreadsheet column | Field |
|---|---|
| Email | `email`, and the document ID `doc_id` (see below) |
| Contact person | `name` |
| Role to approach | `title` |
| Phone | `phone` |
| Email type | `email_type` — `personal` or `generic` (the original text is kept in `email_type_raw`) |
| Contact URL | `contact_page` |
| Contact source | `contact_source` |
| Notes | `notes` |
| Status / Last contacted / Next follow-up | `prospect_status`, `prospect_last_contacted`, `prospect_next_followup` |

Every new contact starts with `status: "pending"`, so it enters the normal sending
flow. All imported documents carry `sources: ["prospect_import"]`.

### Other collections (unless `--campaign-only`)

| Collection | Rule |
|---|---|
| `site_leads/{lead_id}` | Created only if the site is not there yet. Existing documents (for example from the crawler) are **never touched**. |
| `site_leads/{lead_id}/site_contacts/{contact_id}` | Created, or empty fields filled in. |
| `email_contacts/{doc_id}` | Created, or empty fields filled in. This is the contact pool the Leads DB searches and the "active in another campaign" check read. |

Use `--campaign-only` if you want the prospects to stay out of the crawler
collections.

---

## IDs

The same address always gets the same ID, so repeated runs update instead of
duplicating:

| ID | Rule | Example |
|---|---|---|
| `doc_id` | email lower-cased, every character other than letters, digits, `_` and `-` replaced with `_` | `anna@my-firm.no` → `anna_my-firm_no` |
| `lead_id` | host of the website, dots and hyphens replaced with `_` | `www.dude.fi` → `www_dude_fi` |
| `contact_id` | first 12 characters of the SHA-1 of the lower-cased email | `3f9a…` |

---

## What is never overwritten

On existing contacts the sync never changes the outreach history:
`status`, `mail_sent`, `next_mail_index`, `in_reply_to`, `followup_*`,
`comment_history`, `sent_at`, `message_id`, `sender_account`, `created_at`,
`added_at`, `last_action`, `last_action_status`.

A blank cell in the catalogue never removes a value that is already in Firestore.
To clear a value, edit it in the CRM.

---

## Rows that are skipped or handled specially

| Case | What happens |
|---|---|
| No email (contact form only) | The **lead** is imported, no contact is created. It shows in the campaign's sites list but cannot be mailed. |
| Status is anything other than "Not contacted" | The contact is **not** imported, and the report lists it. |
| Country not recognised | Row skipped, with a warning. |
| No proper email address | Row is not imported at all (no site, no contact); counted in the warnings. Sites already imported earlier are kept. |
| New contact is already in another campaign (status pending, active, sent, replied, bounced or converted; excluded/rejected free the email) | Skipped, and listed in the report. Contacts already in *this* campaign are updated as usual. |
| Same email appears in two campaigns of the same run (e.g. UK and international sheets) | The first campaign (alphabetical) gets it, the others skip it and the report says where it went. |
| The duplicate check against other campaigns cannot run (e.g. missing Firestore index) | The report shows `!! duplicate check ... FAILED` and `--apply` **aborts without writing**, unless you pass `--skip-dup-check`. |
| Same site is a lead in another campaign | Allowed (leads are per campaign); the report notes how many, since only contacts are protected from duplicates. |
| Contact imported earlier but no longer in the catalogue | Kept, and listed in the report as "no longer in the catalogue". |

---

## Site size (sitemap page count) — `--measure`

Optional pre-step that fills two extra sheet columns, **`page_count`** and **`sitemap_url`**
(appended after the last used column if missing; the headers `site size`, `sitesize` and
`sitesie` are also read as `page_count`).

```
run_prospects_import.bat --measure                 :: preview: read sitemaps, show what would be written
run_prospects_import.bat --measure --apply         :: write the sheet (keeps a .xlsx.bak) and import
run_prospects_import.bat --measure-only --apply    :: only update the sheets
run_prospects_import.bat --measure --force --apply :: re-measure and OVERWRITE existing values
```

1. Rows with an empty `page_count` are grouped by site; each site is read once with
   `crm.sitemap_reader.SitemapReader` (same reader the campaign scraper uses, 8 in parallel,
   120 s per site, `--workers N`).
2. The result is written to the sheet columns; sites without a readable sitemap stay empty
   and are retried next run.
3. The import stores it as **`campaigns/{id}/campaign_leads/{lead_id}.page_count`** and
   `.sitemap_url` (shown as *Pages* on the campaign's Sites table), and on new `site_leads`.

Without `--force`, existing sheet cells and existing `campaign_leads.page_count` /
`sitemap_url` values are only **filled when empty**, never replaced. Close the workbooks in
Excel before using `--apply`.

## Typical workflow

1. The research team adds or edits rows in the shared catalogue.
2. `run_prospects_import.bat` — read the report: new, changed and unchanged counts,
   warnings and skipped rows.
3. `run_prospects_import.bat --apply` — write.
4. Open the campaign (for example `BS_UK`) in the CRM, check the contacts, set the
   mail text and sending account, and send as usual.

Rows that were already mailed are unaffected, so re-running after a send is safe.

---

## Adding a country

Add the country name to `COUNTRY_CODES` and `COUNTRY_NAMES` at the top of
`functions-crm/crm/prospect_import_lib.py`. Campaign names then follow
automatically (`BS_<code>`).
