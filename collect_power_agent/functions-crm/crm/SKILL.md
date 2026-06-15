# SKILL.md — `functions-crm/crm/` filter-facets count & copy

Rules for `filter_count_lib.py` (count job) and `facet_campaign_lib.py`
(copy-to-campaign job). These take precedence over general project rules for
files in this directory.

There are two pipelines, each with its own matcher and field set:

- **site_leads** — `site_leads` + `site_contacts` → `email_contacts`
- **leads** — `leads` + `email_contacts` (`mark_leads == True`)

---

## RULE: One source of truth — count and copy share the matcher (per pipeline)

The count job and the copy-to-campaign job MUST produce the identical matched
set. Each pipeline therefore has exactly ONE matcher that both call:

| Pipeline   | Matcher (full)        | Public wrapper (sets only)        |
|------------|-----------------------|-----------------------------------|
| site_leads | `_site_leads_match()` | `site_leads_matched_email_ids()`  |
| leads      | `_leads_match()`      | `leads_matched_email_ids()`       |

- `run_filter_count` / `run_leads_filter_count` consume the matcher for headline
  counts AND per-value `selected_count`.
- `_run_facet_campaign_site_leads` / `run_facet_campaign_leads` consume the same
  matcher's `matched_email_ids`.
- NEVER re-implement lead/contact matching inside a count or copy job. If you
  need the predicates, use `_site_leads_predicates()` (site_leads). Add new
  filter fields in ONE place (the matcher), never per-job.

---

## RULE: Contact-first, email-backed — site and contact both in the matched set

The match is contact-driven, not lead-driven:

1. Apply the contact filter first. A contact matches only if it passes the
   contact filter AND its site/lead passes the site/lead filter.
2. **email-backed:** a contact is in the canonical set only if it exists in
   `email_contacts` (the campaign is built from `email_contacts`). For
   site_leads this means the matching `site_contact`'s email must have an
   `email_contacts` doc; for leads the contact already IS an `email_contacts`
   doc.
3. A site/lead is matched ONLY if it has ≥1 such contact. So every matched
   contact's site is matched, and every matched site has ≥1 matched contact —
   they always belong to each other.

Consequences that MUST hold:

- `site_leads` count: `leads` = sites that have ≥1 email-backed matching
  contact (NOT every lead that passes the lead filter). `contacts_found` is the
  broader coverage number (all matching `site_contacts`); keep it separate.
- `leads` count: `leads` = leads that have ≥1 matching contact (NOT
  `candidate_leads`). A lead passing the lead filter but with no matching
  contact is excluded.
- Contact filters that live only on `site_contacts` (e.g. **occupation**, which
  is NOT stored on `email_contacts`) are applied by the matcher via
  `site_contacts`. Do not drop them.

---

## RULE: Copy uses the same set — select by `doc.id in matched_email_ids`

Both copy paths build the campaign by streaming `email_contacts` and keeping
`doc.id in matched_email_ids` from the shared matcher — never by re-filtering
`email_contacts` independently. The only thing layered on top is dedup against
other campaigns (surfaced as `skipped_dedup`). Invariant:

```
copied + skipped_dedup == counted (contacts_in_email_contacts)
```

`selected_count` per facet value is computed over the canonical matched set, so
`sum(selected_count)` per lead category == the headline `leads`, and per contact
category == the headline contacts.

---

## RULE: Test count↔copy alignment with `app/filter_count.py --compare`

After any change to a matcher, a count job, or a copy job, verify on real data:

```bash
python app/filter_count.py --facet <facet_name> --compare   # asserts copied+deduped == counted
```

Cover both pipelines, plus a site_leads facet with an **occupation** selection
and a leads facet where some matching lead has no matching contact.

In-memory regression (no Firestore) lives alongside the simulation pattern used
during development: stub `google.cloud.firestore_v1.base_query.FieldFilter`, feed
fake `site_leads`/`site_contacts`/`email_contacts`, and assert
`run_filter_count` counts == `*_matched_email_ids` == the copy's selected set.

---

## RULE: Hard cap of `MAX_CONTACTS_PER_COPY` (200) contacts per copy-to-campaign

Each copy-to-campaign run writes at most `MAX_CONTACTS_PER_COPY` contacts
(defined in `facet_campaign_lib.py`). The cap is applied to the `matched` list
**after** dedup and **before** the campaign stats and writes, in BOTH pipelines,
so `contacts`, `sites_count`, `countries`, and the batch writes all reflect the
capped set. `contacts_capped_from` is returned (0 when no cap was hit, else the
pre-cap size). The count job is NOT capped — only the copy is.

---

## RULE: These files are large — edit via a script, never the Edit/Write tool

`filter_count_lib.py` and `facet_campaign_lib.py` are large enough that the
Edit/Write tools truncate them mid-function (silent data loss). Always edit with
a Python script:

```bash
python3 - << 'PY'
p = "crm/filter_count_lib.py"; s = open(p, encoding="utf-8").read()
s = s.replace(OLD, NEW, 1)
open(p, "w", encoding="utf-8").write(s)
PY
```

Then ALWAYS verify (syntax is not enough — check for truncation and undefined
names):

```bash
python3 -m py_compile crm/filter_count_lib.py crm/facet_campaign_lib.py
python3 -m pyflakes crm/filter_count_lib.py crm/facet_campaign_lib.py | grep -i "undefined name"  # must be empty
tail -3 crm/filter_count_lib.py    # confirm the file is not truncated
```
