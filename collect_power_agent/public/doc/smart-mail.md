# Smart Mail

Smart Mail is the shared mail runtime for automatic campaign outreach, inbound mail reading, and reply matching. It lives under `functions-crm/smart_mail/` and is used by both API jobs and local command-line tools.

Quick reference:

| Item | Value |
|---|---|
| Firebase codebase | `crm` |
| Source folder | `functions-crm` |
| Runtime | `python312` |
| Function entrypoint | `smartMail` |
| Defined in | `functions-crm/main.py` |
| Decorator | `@https_fn.on_request(region="us-central1", timeout_sec=540, memory=MB_512, max_instances=1)` |
| Deploy command | `firebase deploy --only functions:crm` |
| Scheduler method | `POST` |

Direct deployed Smart Mail URLs:

```text
POST https://us-central1-blueboot-market.cloudfunctions.net/smartMail/outreach-send
POST https://us-central1-blueboot-market.cloudfunctions.net/smartMail/inbound-read
POST https://us-central1-blueboot-market.cloudfunctions.net/smartMail/reply-match
```

The direct `smartMail` trigger URLs are service-authenticated. The `/api/crm/...`
compatibility trigger URLs require `campaign-user` or `admin`.

The important rule is that the mail logic is centralized: selection and sent confirmation are in `outreach_mail_select.py`, real sending is in `outreach_sender.py`, sent-folder reading is in `inbound_read_lib.py` (`run_sent_sync`), and reply reading + matching is in `reply_matcher.py` (`match_new_replies`).

---

## Deployment

Smart Mail is deployed from the Firebase Functions codebase named `crm`.

The codebase is defined in `firebase.json`:

```json
{
  "codebase": "crm",
  "source": "functions-crm",
  "runtime": "python312"
}
```

The exported function entrypoint is `smartMail` in `functions-crm/main.py`.

Deploy it with the CRM functions codebase:

```bash
firebase deploy --only functions:crm
```

That deploys the `functions-crm` entrypoints together:

```text
crmApi
smartMail
crmWorker
```

Deployed Smart Mail trigger URLs:

```text
POST https://us-central1-blueboot-market.cloudfunctions.net/smartMail/outreach-send
POST https://us-central1-blueboot-market.cloudfunctions.net/smartMail/inbound-read
POST https://us-central1-blueboot-market.cloudfunctions.net/smartMail/reply-match
```

`smartMail` only accepts the Smart Mail trigger paths. Use `POST` for scheduled or manual job triggers. Long-running work is still queued and executed by `crmWorker`.

---

## Main Components

| Component | Purpose |
|---|---|
| `functions-crm/smart_mail/outreach_mail_select.py` | Selects contacts for outreach and records successful sends. No SMTP code lives here. |
| `functions-crm/smart_mail/outreach_sender.py` | Opens mail accounts, renders messages, sends mail, applies rate limits, and calls `confirm_sent()`. |
| `functions-crm/smart_mail/outreach_render_mail.py` | Renders campaign mail templates into subject, plain text, and HTML. |
| `functions-crm/smart_mail/mail_sender.py` | Shared SMTP/Gmail sender. Handles account settings, CSS/image preparation, display names, headers, and actual delivery. |
| `functions-crm/smart_mail/inbound_read_lib.py` | Reads the SENT folder only (`run_sent_sync`) and writes `EMAIL_OUT` history with body. Replies are read by `reply_matcher`. |
| `functions-crm/smart_mail/reply_matcher.py` | Fetches IMAP mail, classifies replies / bounces / DMARC, and matches the sender (or a bounce's recovered recipient) to `campaign_contacts` by email. |

Mail accounts are read from Firestore:

```text
settings/mail_accounts/accounts/{email}
```

Campaign contacts are read and updated here:

```text
campaigns/{campaign_id}/campaign_contacts/{contact_doc_id}
```

---

## Outreach Send

Outreach send is the automatic mail sender for campaign sequences. It supports three modes:

| Mode | Meaning |
|---|---|
| `intro` | Send the first Intro step to pending contacts with no previous `mail_sent` history. |
| `followup` | Send the next due sequence step to pending contacts that already have sent mail history. |
| `both` | Run `intro`, then `followup`. |

### Command Line

Dry-run is the default. It selects and renders through the same path as live send, but does not open the sender, send mail, call `confirm_sent()`, sleep, or refresh stats.

```bash
python app/outreach_send.py --dry-run --mode intro --limit 20
python app/outreach_send.py --mode followup --preview
python app/outreach_send.py --send --mode intro --limit 20
python app/outreach_send.py --send --mode both --campaigns NO_jun,SE_jun
python app/outreach_send.py --list-campaigns
```

Flags:

| Flag | Default | Meaning |
|---|---:|---|
| `--mode` / `-m` | `intro` | `intro`, `followup`, or `both`. |
| `--campaigns` / `-c` | all | Optional campaign filter. Accepts space, comma, semicolon, or pipe separated values. |
| `--limit` / `-n` | `500` | Maximum selected contacts per pass. |
| `--preview` | off | In dry-run mode, print rendered body snippets. |
| `--send` | off | Send real mail and write confirmations. |
| `--dry-run` | on | Preview without sending or writing confirmations. |
| `--list-campaigns` | off | Print campaign IDs and exit. |

### API

Trigger automatic outreach with `POST`. This endpoint can queue real mail sends, so it should not be treated as a read-only URL.

```text
POST /api/crm/outreach-send
POST https://us-central1-blueboot-market.cloudfunctions.net/smartMail/outreach-send
```

POST body example:

```json
{
  "mode": "both",
  "limit": 20,
  "campaigns": "NO_jun,SE_jun",
  "dry_run": true,
  "preview": true
}
```

Accepted parameters:

| Parameter | Meaning |
|---|---|
| `mode` | `intro`, `followup`, or `both`. |
| `limit` | Number of contacts selected per pass. |
| `campaigns` | Optional campaign list. `campaign_ids` and `campaign_id` are also accepted by the handler. |
| `dry_run` | `true` selects/renders only. `false` sends and confirms. |
| `preview` | Adds rendered snippets to worker logs when dry-running. |

### Cloud Scheduler

Cloud Scheduler should call outreach send with `POST`, not `GET`.

Target URL:

```text
https://us-central1-blueboot-market.cloudfunctions.net/smartMail/outreach-send
```

Body:

```json
{
  "mode": "both",
  "limit": 50,
  "campaigns": "",
  "dry_run": false,
  "preview": false
}
```

Do not expose this as an unauthenticated public GET URL. Use Cloud Scheduler OIDC or another explicit scheduler-only protection mechanism.

The API queues a CRM worker job named:

```text
outreach-send
```

---

## Outreach Selection Rules

Selection is handled by `read_outreach(mode, limit, campaign_ids)` in `outreach_mail_select.py`.

The first guard is always contact status:

```text
campaign_contacts.status == "pending"
```

Only pending contacts are eligible for automatic outreach.

### Intro Mode

Intro mode selects contacts when all of these are true:

- Contact status is `pending`.
- Contact has no sent history: `mail_sent` is empty.
- Campaign status is `ready`.
- Campaign has an Intro step in `mail_sequence`.
- Campaign has a configured outreach account.
- The optional campaign filter, if supplied, includes this campaign.

The first send always uses the campaign sequence step named or marked as Intro. If no Intro step exists, the contact is not sent and the campaign gets a skip log entry.

### Follow-up Mode

Follow-up mode selects contacts when all of these are true:

- Contact status is `pending`.
- Contact already has at least one `mail_sent` entry.
- Campaign status is `active`.
- The next sequence index exists.
- The next step is due.
- Campaign has a configured outreach account.
- The optional campaign filter, if supplied, includes this campaign.

The next sequence index is calculated per contact:

```text
next_mail_index = len(contact.mail_sent)
selected step = campaign.mail_sequence[next_mail_index]
```

The delay is also calculated per contact. The current implementation compares the next step's `delay_days` against the first sent mail:

```text
due when now >= contact.mail_sent[0].sent_at + selected_step.delay_days
```

That means contacts can enter the same campaign at different times and still move through the shared campaign sequence on their own clock.

---

## Send Confirmation

After a real mail send succeeds, `outreach_sender.py` calls `confirm_sent()`.

`confirm_sent()` appends to the contact document:

```json
{
  "mail_sent": [
    {
      "mail_type": "intro",
      "sent_at": "2026-06-12T10:15:30.123456+00:00",
      "message_id": "<smtp-message-id>"
    }
  ],
  "comment_history": [
    {
      "date": "2026-06-12T10:15:30.123456+00:00",
      "user": "sales@blueboot.ai",
      "text": "Mail sent: Rendered subject",
      "type": "MAIL_SENT",
      "body_text": "Rendered plain-text body...",
      "body_html": "<p>Rendered HTML body...</p>"
    }
  ],
  "followup_status": "contacted",
  "new_mail": false
}
```

The contact's main `status` is left unchanged.

It also writes a technical sent log:

```text
outreach_sent/{auto_id}
```

with:

```json
{
  "campaign_id": "NO_jun",
  "contact_doc_id": "person_example_com",
  "to_email": "person@example.com",
  "sender_account": "sales@blueboot.ai",
  "message_id": "<smtp-message-id>",
  "mail_type": "intro",
  "sent_at": "2026-06-12T10:15:30.123456+00:00",
  "status": "sent"
}
```

`message_id` remains in `mail_sent` and `outreach_sent` because it is needed for threading, reply matching, and deduplication. The CRM-visible history line uses the rendered subject.

If the campaign is `ready`, the first confirmed send changes the campaign to `active` and stamps `sent_at`.

---

## Send Limits

The sender applies per-account deliverability guards before sending:

| Setting | Default | Meaning |
|---|---:|---|
| `MAX_SENDS_PER_HOUR` | `50` | Maximum sent attempts per sender account per hour. |
| `MAX_SENDS_PER_DAY` | `300` | Maximum sent attempts per sender account per day. |
| `CAMPAIGN_SEND_DELAY_SECONDS` | `12` | Delay between live sends. |
| `BOUNCE_RATE_PAUSE_THRESHOLD` | `0.05` | Stops the account batch when failures exceed the threshold after enough attempts. |

The sender calculates a budget from `outreach_sent` for each account. Both successful sends and failures count against the current run's budget.

---

## Mailbox Sync (replies + sent)

The mailbox sync runs **two single-purpose readers**, so each direction has exactly one source of truth:

- **Replies** are read only by `reply_matcher.match_new_replies` (INBOX). It classifies reply / bounce / DMARC, matches by sender, writes `EMAIL_IN` with the mail body, updates follow-up status, and deletes bounces. See the Reply Matcher section.
- **Sent mail** is read only by `inbound_read_lib.run_sent_sync` (the SENT folder). It records mails sent *outside* the CRM as `EMAIL_OUT` with the mail body.

The `inbound-read` job and `app/inbound_read.py` run **both** readers in one pass.

### Command Line

```bash
python app/inbound_read.py
python app/inbound_read.py --days 30
python app/inbound_read.py --campaigns NO_jun SE_jun --days 0
python app/inbound_read.py --campaigns NO_jun --contact person_example_com
python app/inbound_read.py --dry-run
python app/inbound_read.py --list-campaigns
```

Launcher scripts:

```bash
run_inbound_read.bat --campaigns NO_jun --days 30
./run_inbound_read.sh --campaigns NO_jun --days 30
```

Flags:

| Flag | Default | Meaning |
|---|---:|---|
| `--campaigns` / `-c` | all | Campaign IDs to sync. Accepts space, comma, semicolon, or pipe separated values. |
| `--contact` / `-d` | all | Sync one contact doc ID. Requires exactly one campaign. |
| `--days` / `-n` | `7` | Lookback window. Use `0` for all time. |
| `--dry-run` | off | Fetch and match without writing to Firestore. |
| `--list-campaigns` | off | Print campaign IDs and exit. |

### API

```text
POST /api/crm/inbound-read
POST /api/crm/inbound_read
```

The dedicated Smart Mail function accepts:

```text
https://us-central1-blueboot-market.cloudfunctions.net/smartMail/inbound-read
```

POST body example:

```json
{
  "campaigns": "NO_jun,SE_jun",
  "contact_doc_id": "",
  "outreach_account": "sales@blueboot.ai",
  "days": 7
}
```

Accepted campaign parameters are:

```text
campaigns
campaign_ids
campaign_id
```

The API queues a CRM worker job named:

```text
inbound-read
```

### What the Sent Reader Writes

The sent reader (`run_sent_sync`) appends one `EMAIL_OUT` entry per sent message found in the SENT folder, including the mail body:

```json
{
  "email_id": "stable-message-key",
  "type": "EMAIL_OUT",
  "text": "Message subject",
  "date": "2026-06-12T10:15:30+00:00",
  "user": "sales@blueboot.ai",
  "from": "sales@blueboot.ai",
  "to": "person@example.com",
  "body_text": "Plain-text body…",
  "body_html": "<p>HTML body…</p>"
}
```

The write uses Firestore `ArrayUnion` keyed by `email_id`, so repeat runs never duplicate a sent message (already-synced mail is skipped).

Incoming replies (`EMAIL_IN`) are **not** written here — they are written by the reply matcher (which also sets `new_mail` and the follow-up status on a new reply).

### Avoiding duplicate sent entries — the `X-Blueboot-Sent` tag

Outgoing mail reaches the SENT folder from three sources, and only one of them should be logged by the sent reader:

| Case | Source | History written at send time? | Logged by `run_sent_sync`? |
|---|---|---|---|
| 1 | Outreach send (`confirm_sent` → `MAIL_SENT`) | yes | **no** |
| 2 | Manual send (`send-mail` → `EMAIL_OUT`) | yes | **no** |
| 3 | Sent from outside the CRM (another mail client / system) | no | **yes** |

Cases 1 and 2 already write their own history entry (with body) at send time, so the sent reader must not log them again. This is handled two ways:

- **Primary — header tag.** Every mail sent through the CRM goes through `MailSender`, which stamps the header `X-Blueboot-Sent: crm`. `run_sent_sync` reads this header from each SENT-folder message and **skips anything tagged `crm`**. Mail sent from outside the CRM has no such header, so only case 3 is logged.
- **Fallback — `email_id` dedup.** Send-time entries (`MAIL_SENT` / `EMAIL_OUT`) also store `email_id`, the normalized SMTP `Message-ID` (`email_id_from_message_id`, the same key `run_sent_sync` computes). If a mail server ever strips the custom header, the matching `email_id` already in history still prevents a duplicate.

Both rely on a stable `Message-ID`: `MailSender` sets it explicitly, and it is preserved in the SENT-folder copy. The tag only applies to mail sent after this is deployed; older CRM-sent mail is protected by the `email_id` fallback.

---

## Reply Matcher

Reply matcher processes documents in:

```text
inbox_messages
```

where:

```text
reply_matched == false
```

Trigger it through the API:

```text
GET  /api/crm/reply-match?limit=200&days=7
POST /api/crm/reply-match
GET  /api/crm/reply_match?limit=200
POST /api/crm/reply_match
```

Parameters (all optional):

| Param | Default | Meaning |
|---|---|---|
| `limit` | 200 | Max messages to process per account |
| `accounts` | all | Restrict to these account emails (comma / space / semicolon / pipe separated) |
| `campaigns` | all | Restrict matching to these campaign ids (comma / space / semicolon / pipe separated) |
| `days` | 30 | How many days back to search the IMAP mailbox |
| `dry_run` | false | Find matches and log intended changes, but write nothing and delete nothing |

The dedicated Smart Mail function accepts:

```text
https://us-central1-blueboot-market.cloudfunctions.net/smartMail/reply-match
```

POST body (all fields optional):

```json
{
  "limit": 200,
  "accounts": ["sales@blueboot.ai"],
  "campaigns": ["NO_jun"],
  "days": 7,
  "dry_run": true
}
```

The API queues a CRM worker job named:

```text
reply-match
```

### Match Strategy

Replies and bounces are matched on the **email address**, against
`campaign_contacts`:

1. **Replies** — the sender address is normalized to a contact `doc_id` and
   looked up in the `campaign_contacts` collection group; if that misses, it
   falls back to the raw `email` field.
2. **Bounces** — the original failed recipient is recovered from the bounce
   (the `X-Failed-Recipients` header, the `message/delivery-status`
   `Final-Recipient` / `Original-Recipient`, the embedded original message's
   `To:` header, or an Exim / cPanel "following address(es) failed:" block) and
   then matched the same way.

Matching is scoped to the `campaigns` parameter when one is supplied.

### What Reply Matcher Writes

On a matched **reply**, it updates the campaign contact (`campaign_contacts`)
with:

```json
{
  "replied_at": "2026-06-12T10:15:30+00:00",
  "reply_snippet": "First part of the reply body...",
  "reply_subject": "Re: Subject",
  "reply_from": "person@example.com",
  "matched_via": "doc_id"
}
```

and sets `status` to `active` and `followup_status` to `replied` when the
contact was still `pending` (an already-`active` contact keeps its status; any
other status is left unchanged). A `comment_history` entry of type `EMAIL_IN`
is appended, carrying the reply body in two separate fields — `body_text`
(plain text) and `body_html` (the HTML alternative, when present) — extracted
from the inbound message's MIME parts. If the contact has a `lead_id`, the
matching `campaign_leads` document is moved `pending → active`. `email_contacts`
is not updated.

`matched_via` is either:

```text
doc_id        sender email normalized to the contact doc id
email_field   matched on the raw campaign_contacts email field
```

On a matched **bounce**, the contact is moved to `excluded` (with
`bounce_detected`, `bounced_at`, `bounce_reason`) **only if it was still
`pending`** — an `active` / `replied` contact keeps its status and the bounce
is recorded in `comment_history` (type `BOUNCE`) only.

Every processed message is also written to `inbox_messages` as an audit log,
carrying `match_outcome` (`updated` / `already_handled` / `unmatched`),
`match_campaign_id`, `match_contact_doc_id`, and `match_via`.

After a successful match, campaign outreach stats are refreshed.

#### Mail body in history

Every mail recorded in `comment_history` stores the message body in two
separate fields so the CRM can show plain-text and HTML versions independently:

| Field | Holds |
|---|---|
| `body_text` | the plain-text body (capped at 10000 chars) |
| `body_html` | the HTML body, when the mail has one (capped at 30000 chars) |

This applies to all three sources:

- **Received replies** (`EMAIL_IN`) — the inbound message's `text/plain` and
  `text/html` MIME parts.
- **Outreach sends** (`MAIL_SENT`) — the rendered `text_body` and `html_body`.
- **Manual mails** (`EMAIL_OUT`) — the plain and HTML body supplied with the send.

Either field may be empty (e.g. a plain-text-only mail has no `body_html`).

#### Skipped and deleted messages

- **Internal / system mail is skipped.** If a reply's sender — or a bounce's
  recovered recipient — is on one of your own sending domains, or is a system
  mailbox (`cpanel@`, `no-reply@`, `mailer-daemon@`), the message is skipped:
  not matched, not logged as unmatched, and counted under `skipped`.
- **Bounces are deleted after processing.** Once the contacts are updated, all
  bounce messages are removed from the IMAP mailbox (DMARC reports are deleted
  too). Nothing is deleted on a `dry_run`.

---

## Quick Operational Flow

1. Build and review a campaign.
2. Keep contacts eligible for automatic outreach with `status = pending`.
3. Mark the campaign `ready`.
4. Run outreach dry-run:

```bash
python app/outreach_send.py --dry-run --mode intro --campaigns NO_jun --preview
```

5. Send live:

```bash
python app/outreach_send.py --send --mode intro --campaigns NO_jun --limit 20
```

6. Sync the mailbox (runs both readers — replies + sent):

```bash
python app/inbound_read.py --campaigns NO_jun --days 7
```

7. Or trigger reply matching on its own:

```text
POST /api/crm/reply-match
```
