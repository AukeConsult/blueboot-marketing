# Contact Name & Email Cleaning

Scraped contact data contains noise: link text such as **"Email Adam Kenneman."**,
**"Mejla Anna Lindqvist"** (Swedish for "send an email to") or **"Contact us"**, quotes,
addresses like `logo@2x.png`. Every scraper now cleans names and e-mail addresses before they
are saved. The rules live in one module, `functions-crm/crm/contact_clean_lib.py`.

## Where it runs

| Place | What is cleaned |
|---|---|
| Campaign **Scrape site** (`crm/campaign_scrape_lib.py`) | mailto link names and addresses, AI results, and a final check when contacts are written |
| `site_agent` / `site_enrich_agent` (`app/functions/utils.py`, `app/site_enrich_agent.py`) | extracted addresses, paired names, AI contacts |
| `app/maint_clean_contacts.py` | contacts that already exist (see below) |

## Names

- Quotes, HTML entities, invisible characters and stray punctuation are removed.
- Call-to-action words are removed from the front or end in English, Swedish, Danish,
  Norwegian, German, French, Spanish, Italian, Dutch, Finnish, Portuguese and Polish
  (for example *Email, Mejla, Skicka e-post till, Send e-mail til, E-Mail an, Contactez,
  Escribir a, Scrivi a, Mail naar, Lähetä sähköpostia, Napisz do*).
- If the language is not known but the e-mail address is, the words that match the address are
  kept (`anna.lindqvist@` keeps "Anna Lindqvist").
- Titles are removed (Mr, Dr, Herr, Frau, ... and ", PhD").
- Text that is not a name becomes empty: "Contact us", "Kontakta oss", anything with digits,
  `@` or a link, more than five words.
- ALL CAPS or all lower case is turned into proper case (`JOHN SMITH` gives `John Smith`).
- When a name is changed the original is stored in `name_raw`.

To add a language, add its phrases to `CTA_PHRASES` in `contact_clean_lib.py`.

## E-mail addresses

`mailto:` and `?subject=...` are removed, `%40` and `&#64;` decoded, `name [at] firma [dot] se`
converted, the address lower-cased and syntax-checked. Image/asset names (`logo@2x.png`),
placeholders (`your@email.com`, `example.com`), hash addresses and phone digits stuck to the front
are removed.

## Cleaning existing contacts

```
python app/maint_clean_contacts.py                  # preview, nothing is written
python app/maint_clean_contacts.py --apply          # write
python app/maint_clean_contacts.py --campaign BS_UK # one campaign
python app/maint_clean_contacts.py --only campaign  # campaign | site | email
python app/maint_clean_contacts.py --all            # include campaign contacts without scraped_at
```

Dry run by default. Only the `name` field is changed (original kept in `name_raw`). Campaign
contacts without `scraped_at` (possibly typed by hand in the CRM) are skipped unless `--all`.
E-mail addresses are only reported, because the contact id is derived from the address.

## Tests

`python app/test_contact_clean.py` runs about 50 name and e-mail cases in many languages.

## Recalculation on every "Update info"

Each run of **Update info** (normal and force) first re-checks *all* existing contacts of the
campaign with the same name/email cleaning. The scrape itself still only visits sites that have
not been scraped (normal mode); force re-scrapes every site. Per contact: the cleaned name replaces
`name` (original kept in `name_raw`), a fixable email is corrected in place (original in `email_raw`),
and a contact without a proper email is **deleted**. Already mailed contacts (sent, replied, bounced, converted) are never deleted. The result shows `recalc_checked / recalc_names /
recalc_emails / recalc_excluded`.

## Contacts always belong to a site of their campaign

`crm/site_link_lib.py` enforces that every contact's `lead_id` names a site in the **same** campaign.
It runs at the end of **Update info**, of the standard campaign import and of the prospect import
(apply). A broken link is repaired by matching the contact's website / domain / e-mail domain against
the campaign's sites; if nothing matches the site is created in the campaign from the contact's data
(free-mail addresses without a website get their own `mail_<address>` site). The previous value is kept
in `lead_id_old`.

## One e-mail check everywhere

`crm.contact_clean_lib.clean_email()` is the single check for "is this a proper e-mail". It is used by the
scrape (and its AI extraction), the standard and prospect imports, the master-sheet sync, the export
validators and Update info. Imports skip a row/contact whose email fails it (reported as a warning /
`invalid_emails`); Update info deletes existing contacts that fail it.

## One detector, one check

- **Finding** e-mail addresses in html/text: `crm.contact_clean_lib.find_emails()` -- the only detector.
  Used by the campaign scrape, the site/lead agents (`app/functions/utils.extract_contacts`), the
  prospect import and the reply matcher.
- **Judging** a single address: `clean_email()` (used by `find_emails`, the imports, exports and audits).

## LinkedIn profiles

`find_linkedin_profiles()` (contact_clean_lib) collects personal LinkedIn links (`linkedin.com/in/...`,
company pages are ignored) from the scraped pages, together with the person's name and title taken from
the block above the link (the name must fit the profile slug or the "Forbind med <first name>" link text).
- A contact with the same name / matching email gets `linkedin` set.
- Every profile found, with or without an email, is stored on the site as `campaign_leads.people`
  (`[{name, title, linkedin}]`), so people on pages without personal emails are not lost.

## People directories (list page -> one page per person)

The scrape follows pages whose address suggests a people list (team, people, staff, consultants, experts,
management, ledelse, medarbejder ...). On such a list page, links below the page's own path whose last
part looks like a person's name (`.../meet/stuart-turnbull`) are fetched too (second hop, max 40 per site,
only when the list has at least 3 of them). On a person's page the h1 is the name, and the page's single
personal LinkedIn link and email/phone belong to that person.

## Send confirmation (first mail only)

Each contact has a `send_confirmation` flag. When the campaign has `require_send_confirmation: true`
(default for new campaigns), the first (intro) mail is only sent to pending contacts that are ticked.
Reminders/follow-ups ignore the flag because they already depend on a sent intro mail.

- Campaign page → Contacts: first column tick, header tick (shown contacts), "Confirm shown / Unconfirm",
  filter "Confirmed / Not confirmed", counter, and the "Require confirmation" switch. Turning the switch on
  never confirms anyone automatically: contacts are only sent to when you tick them.
- Single contact page: tick "Confirmed to send first mail".
- API: `PATCH .../contacts/<id>` `{send_confirmation}`; `POST .../contacts/confirm` `{value, doc_ids | scope:"all_unsent"}`.
- Only pending contacts with no `mail_sent` can be changed. Import/scrape never overwrite the flag.
- Campaigns without the setting keep the old behaviour (no gate).
