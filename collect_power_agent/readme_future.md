# Future Functionality

## Scraping of Additional Outreach Channels

### Overview

The pipeline currently extracts `email`, `phone`, `name`, `title`, and `website` per contact. LinkedIn profile URLs (and potentially other social channels) are visible on many contact/team pages but are not extracted. The importer/exporter already have a `linkedin` column in their schema — the field exists in the contract but the scraper never populates it.

---

### What exists today

| Layer | State |
|---|---|
| `SiteContact` dataclass (`app/site_agent.py`) | No `linkedin` field |
| `_extract_contacts()` (`app/site_agent.py`) | Does not parse social URLs from `<a>` tags |
| `CONTACT_UPDATABLE_FIELDS` (`app/campaign_importer.py`) | Already includes `"linkedin"` |
| `CONTACT_HEADER_MAP` (`app/campaign_importer.py`) | Already maps `"LinkedIn"` → `"linkedin"` |
| `campaign_exporter.py` | Already exports `("linkedin", "LinkedIn", 35)` column |
| `followup_contacts()` (`functions-crm/handlers/contacts.py`) | Does not return `linkedin` in payload |
| PATCH `allowed` set (`functions-crm/handlers/contacts.py`) | Does not include `"linkedin"` |
| Side panel / table (`public/crm_follow.html`) | No LinkedIn display or edit |

The gap is entirely in the scraper and the API surface. The data contract (importer/exporter) is already in place.

---

### Changes required — by layer

#### 1. `app/site_agent.py` — SiteContact + extraction

Add `linkedin: str = ""` to the `SiteContact` dataclass:

```python
@dataclass
class SiteContact:
    contact_id:   str
    email:        str
    name:         str
    title:        str
    phone:        str
    linkedin:     str        # ← add
    lead_id:      str
    domain:       str
    website:      str
    country:      str
    country_name: str
    found_on:     str
```

In `_extract_contacts()`, after extracting email/phone/name/title, scan all `<a href>` tags on the same page for LinkedIn profile URLs:

```python
import re as _re

_LINKEDIN_PROFILE_RE = _re.compile(
    r'https?://(www\.)?linkedin\.com/in/[a-zA-Z0-9\-_%]+/?',
    _re.IGNORECASE,
)

def _extract_linkedin(soup) -> str:
    for a in soup.find_all("a", href=True):
        m = _LINKEDIN_PROFILE_RE.match(a["href"].strip())
        if m:
            return m.group(0).rstrip("/")
    return ""
```

Associate the LinkedIn URL with the contact by proximity — look for a LinkedIn link within the same parent container (`<div>`, `<li>`, `<article>`) as the email address. Fall back to the first profile URL found on the page if no proximity match is available.

**Risks:**
- Many sites link to the *company* LinkedIn page (`/company/...`) not a personal profile (`/in/...`) — the regex above filters these out by requiring `/in/`.
- Some sites load social links via JavaScript; `_async_get` fetches raw HTML so these will be missed.
- A contact page may list multiple team members, each with their own LinkedIn link — proximity matching is essential to avoid misassigning URLs.

#### 2. `app/site_agent.py` — Firestore write

The `SiteContact` is written to Firestore via `asdict(contact)` in the batch write at the end of `process_site_async`. Once `linkedin` is on the dataclass, it is automatically included in the write with no further changes needed.

The `campaign_contacts` Firestore documents accept arbitrary string fields — no schema migration is required.

#### 3. `functions-crm/handlers/contacts.py` — API surface

In `followup_contacts()`, add `linkedin` to the contact dict returned to the frontend:

```python
contacts.append({
    ...
    "phone":    d.get("phone", "") or "",
    "linkedin": d.get("linkedin", "") or "",   # ← add
})
```

In `update_campaign_contact()`, add `"linkedin"` to the `allowed` set:

```python
allowed = {"name", "title", "status", "phone", "linkedin"} | _FOLLOWUP_FIELDS
```

#### 4. `public/crm_follow.html` — Frontend

**Side panel header** — add a LinkedIn icon link alongside the existing email/phone icons:

```javascript
${r.linkedin
  ? `<a href="${escapeHtml(r.linkedin)}" target="_blank" class="collapse-all-btn" title="LinkedIn profile">
       <i class="ti ti-brand-linkedin"></i>
     </a>`
  : ''}
```

**Side panel contact section** — show LinkedIn as an editable `follow-input` text field (same pattern as phone/name) so users can manually correct or add a URL:

```javascript
<div class="sp-row">
  <i class="ti ti-brand-linkedin"></i>
  <input type="text" class="follow-input small flex-grow-1"
    value="${escapeHtml(r.linkedin || '')}" placeholder="LinkedIn URL"
    data-gidx="${gidx}" data-field="linkedin"
    onchange="saveField(this)"
    onkeydown="if(event.key==='Enter'){event.preventDefault();this.blur()}">
</div>
```

**Table column (optional)** — LinkedIn is lower priority for the main table since the side panel shows it. Can be added as a `d-none d-xxl-table-cell` column alongside Phone and Website if needed later.

---

### Additional channels beyond LinkedIn

The same pattern applies to any future social channel:

| Channel | Field name | URL pattern to scrape |
|---|---|---|
| Twitter/X | `twitter` | `twitter.com/` or `x.com/` — exclude `/home`, `/search`, company handles |
| Instagram | `instagram` | `instagram.com/` — personal handles only, exclude brand pages |
| WhatsApp | `whatsapp` | `wa.me/` or `api.whatsapp.com/send?phone=` |
| Facebook | `facebook` | Lower priority — personal profiles vs. company pages hard to distinguish |

Each channel follows the same pipeline: add field to `SiteContact`, extract in `_extract_contacts()`, pass through API, display in side panel.

---

### Proximity matching — implementation note

When a page lists multiple team members, the scraper must not assign one person's LinkedIn URL to another person's email. The recommended approach:

1. For each extracted contact email, find the smallest DOM ancestor that also contains the LinkedIn link.
2. If no ancestor contains both within N levels of the tree, fall back to distance-based scoring (character distance between the `<a mailto:>` and `<a linkedin.com/in/>` in raw HTML).
3. If ambiguous (multiple profiles equidistant), leave `linkedin = ""` rather than guess.

This logic belongs in a helper `_match_linkedin_to_contact(contacts, soup)` called after initial contact extraction — so the extraction and matching steps are testable independently.

---

### Testing considerations

- Unit test `_extract_linkedin(soup)` with sample HTML fixtures covering: single profile link, company link only (expect empty), multiple profile links, JS-only links (expect empty).
- Integration test: run `site_agent.py` against a known page with a team listing that includes LinkedIn URLs and verify the extracted contacts have correct profiles.
- Add `linkedin` to the `python-unit-tests` skill's field coverage for `site_agent.py`.

---

## Direct Messaging Integration — Read & Send via CRM

### Overview

The CRM side panel stores channel handles (LinkedIn, Twitter, WhatsApp, Teams, Telegram, Google Chat, Messenger, Facebook, Instagram). A natural next step is to read and send messages on these channels directly from the CRM, without switching apps. The feasibility and approach differ significantly per channel.

---

### Channel feasibility assessment

#### Tier 1 — Fully viable, clean APIs, recommended

**Telegram**
The easiest integration. Create a Bot via BotFather (free, instant). The bot can send messages to any user who has initiated a conversation with it (`/start`), and receive messages via webhook. No business account or per-message cost. Store `telegram_chat_id` on the contact after first contact. Buildable in a weekend.

- API: Telegram Bot API (`https://api.telegram.org/bot<token>/`)
- Auth: single bot token, no OAuth
- Constraint: contact must `/start` your bot once before you can message them
- Cost: free

**Microsoft Teams**
Microsoft Graph API supports sending and reading direct chat messages. Register an Azure AD app, OAuth once per sending user. Mature, well-documented API. Works best for B2B outreach where both sides use Teams.

- API: Microsoft Graph (`https://graph.microsoft.com/v1.0/chats`)
- Auth: OAuth2 / Azure AD app registration
- Constraint: recipient must be in the same tenant or federated via External Access
- Cost: free (Azure AD app registration)

---

#### Tier 2 — Works but with business constraints

**WhatsApp (Meta Cloud API)**
The official WhatsApp Business API allows sending and receiving messages programmatically. First outreach message must use a pre-approved template (e.g. "Hi {{name}}, following up on…"). Once the contact replies, a 24-hour free-form window opens. Requires a registered business phone number and Meta Business account.

- API: Meta Cloud API (`https://graph.facebook.com/v18.0/<phone_id>/messages`)
- Auth: Meta App + system user token
- Constraint: cold outreach requires approved message templates; 24-hour window for free-form replies
- Cost: ~$0.005–0.08 per conversation depending on country

**Google Chat**
Simple webhook for sending (one HTTP call). Reading messages requires a Pub/Sub subscription and Google Cloud project. Only practical for B2B where both sides are on Google Workspace.

- API: Google Chat API + Pub/Sub for incoming
- Auth: Service account or OAuth
- Constraint: both sides must use Google Workspace
- Cost: free within Google Cloud free tier

---

#### Tier 3 — Restricted or impractical for outreach

**Messenger (Facebook)**
Messenger Platform API exists but the 24-hour rule makes cold outreach impractical — you can only send free-form messages within 24 hours of the contact messaging your Facebook Page. After that, only approved notification templates. Best for inbound leads, not cold outreach.

**LinkedIn**
Messaging API is locked behind LinkedIn Sales Navigator API (enterprise, ~$10k/year). No DM access on the standard API. Not viable without the Sales Navigator contract.

**Twitter/X**
DM API exists in v2 but X has made API access expensive since 2023 — Pro tier required for DMs ($5000/month). Not worth it at typical CRM scale.

**Instagram**
Via Meta Business API. Same 24-hour window constraint as Messenger. Designed for customer service replies, not cold outreach.

---

### Recommended implementation paths

#### Option A — Native Telegram + Teams integration (1–2 weeks)

Build Telegram and Teams directly into the CRM backend. These are the two cleanest APIs, no per-message cost, and cover the most common professional outreach scenarios.

Data model additions per contact:
- `telegram_chat_id` — stored after first contact
- `teams_thread_id` — stored after first message

New Firestore subcollection: `campaign_contacts/{doc_id}/messages`

```
{
  channel:    "telegram" | "teams",
  direction:  "out" | "in",
  body:       "...",
  sent_at:    <timestamp>,
  status:     "sent" | "delivered" | "read" | "failed",
}
```

New Cloud Function: `crmMessenger` — handles webhook ingestion from Telegram/Teams and writes to the messages subcollection.

Frontend addition: a **Messages** section in the side panel below History — scrollable thread (last 5 messages, load more), text input, Send button.

#### Option B — Messaging aggregator (Chatwoot or Respond.io)

Use an open-source aggregator (**Chatwoot**, self-hostable) or commercial (**Respond.io**, **MessageBird**) as a unified backend. These handle Telegram, WhatsApp, Messenger, Instagram, and Google Chat behind a single API. The CRM integrates once to the aggregator and stores a `chatwoot_contact_id` per contact.

Pros: all channels in one integration, inbox/thread UI already built, webhook standardised.
Cons: additional service to host/pay for, messages live outside Firestore.

This is the right path if more than 2–3 channels are needed long-term.

---

### Side panel UX — Messages section

When implemented, the Messages section sits below History in the side panel:

```
┌─────────────────────────────────┐
│ [telegram] 2d ago               │
│   "Thanks, let's connect"       │
│ [you] 1d ago                    │
│   "Great, sending the deck now" │
│─────────────────────────────────│
│ [_____________ Send __________] │
└─────────────────────────────────┘
```

Channel selector (Telegram / Teams / WhatsApp) shown only when more than one channel is configured for the contact. Send button disabled if `chat_id` / `thread_id` not yet set, with a helper link to initiate first contact.

---

### Suggested first step

Start with **Telegram** — cleanest API, zero cost, fastest to validate the end-to-end loop (webhook → Firestore → side panel thread → send). Once the message subcollection schema and side panel UX are proven, extending to Teams or a Chatwoot aggregator is straightforward.
