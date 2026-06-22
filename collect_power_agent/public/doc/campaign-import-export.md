# Campaign Import & Export

This guide covers how to move campaign data in and out of the system — both the
Google Sheets export used for the live working list, and the Excel import for
bringing leads and contacts into a new or existing campaign.

---

## Export — Full override

The **Full override** button is on the Campaign page, next to the Sheet link. It
regenerates the connected Google Sheet for the selected campaign, replacing all
content with the current data from the database.

### When to use it

Use Full override when:

- You open the campaign for the first time and no sheet exists yet — the button
  creates the sheet automatically.
- The sheet has drifted out of sync and you want to start fresh from the database.
- You have added new contacts through the filter or import flow and want them
  visible in the sheet immediately.

### What gets exported

The sheet contains one row per contact. Lead information (company, website,
country, location, platform, description, priority, score, suggested angle, and
categories) is repeated on each row alongside the contact's name, email, title,
occupation, phone, and LinkedIn.

The sheet has two tabs:

- **Leads+Contacts** — the working list, one row per contact.
- **Summary** — campaign metadata plus a breakdown by country and platform.

### After export

A status message confirms the export and shows a link to open the sheet directly.
If the sheet already existed, all previous content is replaced. The sheet name is
always the campaign ID.

---

## Export — Local Excel file (command line)

For offline work or archiving, the CLI tool exports the same data to a local
`.xlsx` file with the same columns and two-sheet layout as the Google Sheets
export.

```
python app/campaign_exporter.py NO_tech_jul01
python app/campaign_exporter.py NO_tech_jul01 --output C:\exports\
python app/campaign_exporter.py --list
```

The file is saved to `output/<campaign_id>/campaign.xlsx` by default, or to the
path you specify with `--output`.

---

## Import — Upload form

The **Import campaign** page lets you load leads and contacts into the system from
an Excel file. It accepts any file that was previously exported (or built manually)
using the standard column layout.

Open it from the Campaigns nav group: **Campaigns → Import campaign**.

### Download the example template

Click **Download example file** below the upload area to get a ready-to-fill `.xlsx`. It has two tabs:

- **Leads+Contacts** — all 20 column headers plus two sample rows (delete them before importing your real data).
- **Field guide** — description, example value, and required/optional label for every column.

### Steps

**1. Enter the campaign ID**

Type the campaign ID you want to import into (e.g. `NO_tech_jul01`). If the
campaign does not exist yet, it will be created automatically with a draft status.

**2. Select the file**

Drop your `.xlsx` file onto the upload area, or click **Browse file** to pick it
from your computer. The file must have a tab named **Leads+Contacts**.

**3. Run a dry run first**

The **Dry run** checkbox is on by default. Click **Import** to see a preview of
what will happen — how many leads and contacts are new, how many will be updated,
and how many rows will be skipped. No data is written yet.

**4. Confirm**

If the preview looks correct, click **Confirm and write to Firestore** to apply
the import. A success message shows the final counts.

To skip the dry run and write directly, uncheck **Dry run** before clicking
Import.

### What gets imported

**Leads** — company, website, country, location, platform, page count,
description, priority, score, suggested angle, and categories. If a lead already
exists in the campaign, its fields are updated. Fields not in the sheet are left
unchanged.

**Contacts** — name, email, title, occupation, email type, phone, and LinkedIn.
New contacts are created with status `pending`. Existing contacts have their
profile fields updated, but outreach state (status, send history, follow-up
fields) is never overwritten by an import.

### Skipped rows

A row is skipped if it has neither an email nor a website. Skipped rows are listed
in the preview under Warnings.

### How IDs are assigned

You do not need to manage IDs manually. The system derives them automatically:

- **Lead ID** — taken from the Lead ID column if present; otherwise derived from
  the website address (e.g. `www.sol.no` becomes `www_sol_no`).
- **Contact ID** — always derived from the email address (e.g.
  `adrian@blisynlig.no` becomes `adrian_blisynlig_no`). This means importing the
  same contact twice is safe — it updates the existing record rather than creating
  a duplicate.

### Import via command line

For large files or automated pipelines, the CLI tool accepts the same format:

```
python app/campaign_importer.py NO_tech_jul01 campaign.xlsx --dry-run
python app/campaign_importer.py NO_tech_jul01 campaign.xlsx
```

Add `--dry-run` to preview counts without writing. Remove it to apply the import.

---

## Column reference

Both the import and export use the same column order:

| Column | Description |
|---|---|
| Campaign | Campaign ID |
| Lead ID | Unique identifier for the lead |
| Company | Company or site name |
| Website | Website URL |
| Country | Country code (e.g. NO, SE) |
| Location | City or region |
| Platform | CMS or technology platform |
| Pages | Approximate page count |
| Description | Site or company description |
| Priority | Lead priority (A / B / C or similar) |
| Score | Reseller potential score |
| Angle | Suggested outreach angle |
| Categories | Product or service categories |
| Email | Contact email address |
| Name | Contact full name |
| Title | Job title |
| Occupation | Occupation type |
| Email type | How the email was found |
| Phone | Phone number |
| LinkedIn | LinkedIn profile URL |
