---
name: outreach-mail-select
description: >
  Use this skill when working with functions-smartmail/outreach_mail_select.py — the
  library that reads outreach candidates from Firestore and records sent status.
  Triggers include: calling read_outreach() in mode "intro" or "followup", using
  AccountBatch / CampaignWithContacts / ContactRow dataclasses, the mail_sequence
  array on campaigns, the mail_sent history array on contacts, calling confirm_sent()
  to append a MailSentEntry and stamp contact status, enforcing campaign mail order
  via next_mail_index, threading followup replies via in_reply_to, or wiring this
  library into a send loop in smart_campaign_sender.py. Also use when the user asks
  how outreach candidates are selected by mode, how the next mail step in a sequence
  is resolved per contact, how sent history is written back with ArrayUnion, or how
  confirm_sent records sent mail while the contact remains pending for automation.
---

# outreach-mail-select

**File:** `functions-smartmail/outreach_mail_select.py`

Read-only selection library — no SMTP, no sending. Two public functions.

---

## Public API

### `read_outreach(mode, limit) → list[AccountBatch]`

Queries `campaign_contacts` collectionGroup, groups by campaign then by sending
account, and returns a three-level account-first structure.

**Two modes:**

| mode | filter | mail template |
|------|--------|---------------|
| `"intro"` | `status == "pending"` and no sent mail | Intro step |
| `"followup"` | `status == "pending"`, sent mail exists, campaign is active, and the next step is due | `mail_sequence[next_mail_index]` where `next_mail_index = len(contact.mail_sent)` |

**Return shape:**

```
list[AccountBatch]
  .account   : MailAccountSettings        ← level 1 — who sends
  .campaigns : list[CampaignWithContacts] ← level 2 — what to send
    .campaign  : CampaignMail              (includes full mail_sequence)
    .contacts  : list[ContactRow]         ← level 3 — who to send to
                  (includes mail_sent history, next_mail_index, in_reply_to)
```

**Typical send loop:**

```python
from outreach_mail_select import read_outreach, confirm_sent

for batch in read_outreach(mode="intro"):        # or mode="followup"
    # open one SMTP connection for batch.account
    for cwc in batch.campaigns:
        step = cwc.campaign.mail_sequence[0]     # intro always index 0
        for contact in cwc.contacts:
            msg_id = smtp_send(
                account    = batch.account,
                to         = contact.email,
                subject    = render(step.subject, contact),
                body       = render(step.body_html, contact),
                in_reply_to = contact.in_reply_to,   # None for intro
            )
            confirm_sent(
                campaign_id    = contact.campaign_id,
                contact_doc_id = contact.contact_doc_id,
                message_id     = msg_id,
                mail_type      = step.mail_type,
                mode           = "intro",
                sender_account = batch.account.email,
            )
```

#### Parameters
| param | type | default | notes |
|-------|------|---------|-------|
| `mode` | `"intro" \| "followup"` | `"intro"` | selects filter and template source |
| `limit` | `int` | `500` | max total contacts across all campaigns |

#### Firestore reads
1. `collection_group("campaign_contacts")` — one query
2. `campaigns/{id}` — one read per distinct campaign
3. `settings/mail_accounts/accounts/{email}` — one read per distinct account (cached)

---

### `confirm_sent(campaign_id, contact_doc_id, message_id, mail_type, mode, sender_account, sent_at) → SentConfirmation`

Appends a `MailSentEntry` to `contact.mail_sent` and stamps status fields.
Deduplicated by `message_id` — safe to call on retry.

**Writes to `campaign_contacts/{contact_doc_id}`** (single `.update()` call):
- `mail_sent` → `ArrayUnion({mail_type, sent_at, message_id})`
- `comment_history` → `ArrayUnion({date, user=sender_account, text="Mail sent: <mail_type> <message_id>", type="MAIL_SENT"})` — visible in CRM dashboard
- Keeps `status = "pending"` so automatic outreach can continue through the sequence.
- Sets `followup_status = "contacted"` and clears `new_mail`.
- If the campaign is `ready`, marks it `active` after the first sent mail.

**Appends to `outreach_sent`** (skipped if `message_id` already present):
- `campaign_id`, `contact_doc_id`, `sender_account`, `message_id`, `mail_type`, `sent_at`, `status="sent"`

#### Parameters
| param | type | default | notes |
|-------|------|---------|-------|
| `campaign_id` | `str` | required | |
| `contact_doc_id` | `str` | required | |
| `message_id` | `str` | `""` | SMTP Message-ID; **auto-generated if not supplied** via `email.utils.make_msgid()` — always unique |
| `mail_type` | `str` | `"intro"` | e.g. `"intro"`, `"followup_1"` — from `MailStep.mail_type` |
| `mode` | `"intro" \| "followup"` | `"intro"` | controls which status field is cleared |
| `sender_account` | `str` | `""` | account email logged to outreach_sent and comment_history |
| `sent_at` | `str \| None` | now UTC ISO | override if needed |

#### message_id uniqueness guarantee
`message_id` is always unique. If the caller passes the SMTP `Message-ID` header value
(recommended — enables reply threading via `In-Reply-To`), that value is used. If the
caller passes `""` or omits the argument, `confirm_sent` calls `email.utils.make_msgid()`
internally, which combines timestamp + PID + hostname to produce a globally unique RFC 2822
message ID. Either way, deduplication on `outreach_sent` is always safe.

---

## Data classes

### `MailStep`
One step in the campaign's mail sequence. Stored in `campaign.mail_sequence`.

| field | type | notes |
|-------|------|-------|
| `index` | int | position in sequence; 0 = intro |
| `mail_type` | str | `"intro"`, `"followup_1"`, `"followup_2"`, … |
| `subject` | str | subject template (may contain `{{vars}}`) |
| `body_html` | str | Quill HTML template |

### `MailSentEntry`
One sent record in the contact's `mail_sent` history array.

| field | type | notes |
|-------|------|-------|
| `mail_type` | str | matches `MailStep.mail_type` |
| `sent_at` | str | ISO-8601 UTC |
| `message_id` | str | SMTP Message-ID for reply threading |

### `MailAccountSettings` (level 1)
Resolved from `settings/mail_accounts/accounts/{email}`.

| field | type | notes |
|-------|------|-------|
| `email` | str | doc key |
| `account_type` | str | `"imap"` \| `"gmail"` |
| `host` | str | SMTP host |
| `port` | int | SMTP port (default 587) |
| `username` | str | |
| `from_name` | str | display name |
| `imap_host` | str | |
| `imap_port` | int | default 993 |
| `use_ssl` | bool | from `ssl` field |

### `CampaignMail` (level 2)
Resolved from `campaigns/{campaign_id}`.

| field | type | notes |
|-------|------|-------|
| `campaign_id` | str | doc id |
| `campaign_name` | str | `name` |
| `status` | str | campaign status |
| `mail_sequence` | list[MailStep] | ordered send steps; index 0 = intro |
| `sender_email` | str | `outreach_email_account` or `sender_account` |

### `ContactRow` (level 3)
From `campaign_contacts` collectionGroup.

| field | type | notes |
|-------|------|-------|
| `contact_doc_id` | str | |
| `campaign_id` | str | |
| `email` | str | |
| `contact_name` | str | |
| `company` | str | |
| `domain` | str | |
| `country` | str | |
| `status` | str | |
| `followup_status` | str | current follow-up state, such as `contacted` or `replied` |
| `mail_sent` | list[MailSentEntry] | sent history, append-only |
| `next_mail_index` | int | `= len(mail_sent)`; index into `campaign.mail_sequence` |
| `in_reply_to` | str \| None | `mail_sent[-1].message_id` or None; set as SMTP In-Reply-To |

---

## Sequence order rule

The next mail step is always `campaign.mail_sequence[contact.next_mail_index]`.

- A contact with `mail_sent = []` → `next_mail_index = 0` → intro step.
- A contact with 1 sent → `next_mail_index = 1` → first followup.
- If `next_mail_index >= len(mail_sequence)` the contact is **skipped** — sequence exhausted.

`confirm_sent` uses Firestore `ArrayUnion` so concurrent sends never corrupt the history.

---

## Schedule ownership rule

The campaign owns the reusable mail plan. The contact owns its own send clock.

Campaign document:

- `mail_schedule` is edited in the campaign workspace.
- `prepare_mail_sequences()` converts `mail_schedule` into `mail_sequence` for automatic sending.
- Each step carries `delay_days`, subject/body, and a step identity such as `intro` or `followup_1`.

Campaign contact document:

- `mail_sent` is the append-only history of what this contact has already received.
- `confirm_sent()` appends `{mail_type, sent_at, message_id}` to `mail_sent`.
- `read_outreach()` uses `len(contact.mail_sent)` to choose the next campaign sequence step.

Delay rule:

- Follow-up due dates are calculated per contact from the first sent mail date, normally the Intro send.
- `_next_step_due()` reads `mail_sent[0].sent_at` and checks `first_sent_at + step.delay_days`.
- This lets contacts enter the same campaign on different days and still follow the same campaign schedule independently.

Example:

```
Campaign step: Follow-up 1, delay_days = 7

Contact A Intro sent June 1 -> Follow-up 1 due June 8
Contact B Intro sent June 5 -> Follow-up 1 due June 12
```

Do not store per-contact copies of the whole campaign schedule unless there is a future explicit need to fork a contact into a different sequence. The current contract is: campaign sequence plus contact `mail_sent` history.

---

## Firestore paths

```
campaigns/{campaign_id}                             ← CampaignMail + mail_sequence source
campaigns/{campaign_id}/campaign_contacts/{doc_id}  ← ContactRow source + mail_sent + confirm_sent target
settings/mail_accounts/accounts/{email}             ← MailAccountSettings source
outreach_sent/{auto_id}                             ← confirm_sent log target
```

---

## CLI dry-run: `app/outreach_select_run.py`

Prints the resolved `read_outreach()` batches without sending anything or writing to Firestore.
Use it to verify account resolution, campaign grouping, and contact counts before a live run.

```bash
# Intro mode — all pending contacts (default)
python app/outreach_select_run.py

# Followup mode
python app/outreach_select_run.py --mode followup

# Filter to one campaign
python app/outreach_select_run.py --campaign NO_jun

# Cap contacts fetched
python app/outreach_select_run.py --limit 50

# Also print subject template per campaign
python app/outreach_select_run.py --verbose

# List all campaign IDs and exit
python app/outreach_select_run.py --list-campaigns
```

**Flags:**

| flag | short | default | notes |
|------|-------|---------|-------|
| `--mode` | `-m` | `intro` | `intro` or `followup` |
| `--campaign` | `-c` | all | filter to one campaign ID |
| `--limit` | `-n` | `500` | max total contacts to fetch |
| `--verbose` | `-v` | off | print subject template per campaign |
| `--list-campaigns` | | | list campaign IDs and exit |

**Output format** (nothing written to Firestore):
```
[dry-run] read_outreach  mode=intro  limit=500

Account : sender@example.com  (Sender Name)  host=smtp.example.com:587  STARTTLS  campaigns=2  contacts=47

  Campaign : NO_jun  (Norway June)  status=active  contacts=31
    contact@company.no  Company AS         NO    sent=0  next_idx=0
    ...

[dry-run] total  accounts=1  campaigns=2  contacts=47  (nothing written)
```

---

## Error handling

- Campaign not found → skipped with warning, never raises
- Mail account not found → skipped with warning, never raises
- `next_mail_index` out of bounds → contact skipped silently
- `confirm_sent` deduplicates on `message_id` — safe to retry
- Firestore import chain: `smart_mail.firestore_client` → `app.firestore_client` → `firestore_client`
