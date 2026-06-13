---
name: campaign-workspace
description: >
  Use this skill when editing or extending the Blueboot CRM campaign workspace
  frontend, especially public/campaign.html, public/campaigns.html,
  public/campaign-edit.html, public/js/mail-editor-component.js, campaign contact
  status controls, mail schedule editing, remove-excluded behavior, or user
  documentation for the campaign page.
---

# Campaign Workspace

Use this skill for the campaign workspace frontend and its docs.

## Key files

| File | Purpose |
|---|---|
| `public/campaign.html` | Main full-viewport campaign workspace |
| `public/campaigns.html` | Campaign list entry that opens `campaign.html` |
| `public/campaign-edit.html` | Direct mail editor route |
| `public/js/mail-editor-component.js` | Reusable mail editor component |
| `public/doc/user-guide.md` | Main user documentation |
| `public/doc/filter-to-campaign.md` | Step-by-step campaign creation guide |

## Layout rules

- `campaign.html` is the canonical campaign page.
- Keep the full-viewport split layout:
  - left sidebar: campaign list, search, status filter, owner filter, Discover/Filter new actions
  - main workspace: selected campaign details
  - detail grid: campaign details and mail schedule on the left, right-hand work column on the right
- The right-hand work column is a view switcher:
  - default view: contact list
  - mail editor view: shown only when adding or editing a mail schedule step
  - provide a clear `Contacts` back button from editor view to list view
- Do not show the mail editor by default when selecting a campaign.

## Mail editor rules

- Reuse `window.MailEditorComponent` from `public/js/mail-editor-component.js`.
- Include Quill assets before using the component:
  - `https://cdn.jsdelivr.net/npm/quill@2.0.2/dist/quill.snow.css`
  - `https://cdn.jsdelivr.net/npm/quill@2.0.2/dist/quill.js`
- Schedule step edit/add should call the component inline, not navigate to `campaign-edit.html`.
- The component saves through `POST /api/crm/campaigns/<campaign_id>`:
  - campaign mail: `{ mail, outreach_email_account }`
  - schedule step: `{ mail_schedule_step: { step_id, name, delay_days, mail } }`
- For the campaign workspace right-column editor, instantiate with `showMainButton: false`.
- Keep test mail routed through the existing campaign test modal by listening for the `mail-editor:test` event.

## Mail schedule rules

- The campaign owns the reusable schedule/template sequence in `mail_schedule`.
- Each schedule step has `step_id`, `name`, `delay_days`, and `mail`.
- Automatic sending converts campaign `mail_schedule` to `mail_sequence` in `functions-smartmail/outreach_mail_select.py`.
- Contacts do not store their own copy of the schedule. They store `mail_sent`.
- The next automatic mail for a contact is selected by `len(contact.mail_sent)`.
- Follow-up delays are counted from that contact's first sent mail date, normally Intro, not from the campaign creation date and not from the previous follow-up.
- Example: if Follow-up 1 has `delay_days = 7`, a contact whose Intro was sent June 1 is due June 8; a contact whose Intro was sent June 5 is due June 12.

## Contact status rules

- Contact lifecycle statuses are only `pending`, `active`, and `excluded`.
- Campaign statuses are separate: `draft`, `ready`, `active`, `canceled`.
- Status flow is `draft` -> `ready` -> `active` -> `canceled`, then the campaign can be deleted.
- A campaign becomes `active` after the first real outreach mail is sent.
- Contact row actions are toggles:
  - Active toggles `active` <-> `pending`
  - Exclude toggles `excluded` <-> `pending`
- Status changes write directly to the campaign contact document with:
  `PATCH /api/crm/campaigns/<campaign_id>/contacts/<doc_id>`.

## Remove excluded rules

- The toolbar trash button opens the remove-excluded confirmation modal.
- Do not use `window.confirm` for this action.
- Use "Remove from campaign" wording, not "Delete", unless referring to draft campaign deletion.
- The modal must explain:
  - removing excluded contacts does not delete the contact from the database
  - it removes the contact only from this campaign
  - the email can then be picked up by another campaign later
  - if kept excluded in this campaign, the email stays reserved here and will not appear in other campaigns
- The API remains:
  `POST /api/crm/campaigns/<campaign_id>/contacts/remove`

## Documentation rules

When changing the campaign workspace, update:

- `public/doc/user-guide.md` for user-facing behavior
- `public/doc/filter-to-campaign.md` when the campaign setup flow changes

Keep docs consistent with current UI labels: `Active`, `Exclude`, `Remove excluded`, `Mail schedule`, `Contacts`.

## Verification

After editing campaign JS/HTML, run:

```powershell
node -c public\js\mail-editor-component.js
node -e "const fs=require('fs'); const s=fs.readFileSync('public/campaign.html','utf8'); const m=[...s.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)].pop(); if(!m) throw new Error('inline script not found'); new Function(m[1]); console.log('campaign inline script syntax ok');"
```

Also grep docs for stale labels:

```powershell
rg -n "Delete excluded|Mail template|Exclude selector|Emailed|bulk selection" public\doc
```
