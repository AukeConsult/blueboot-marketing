# CRM Follow-up — Concepts and Logic

This document explains how the follow-up system works, what the statuses mean, and how contacts move through the outreach lifecycle. For instructions on how to use the page itself — filters, the side card, row selection, batch actions — see the [Follow-up page usage guide](doc-viewer.html?doc=followup-page-usage).

---

## The outreach lifecycle

Blueboot CRM connects two separate stages: building campaigns and working them.

### Stage 1 — Contacts arrive from the pipelines

Contacts are not entered manually. They come from the discovery pipelines — automated processes that scan the web for companies and extract contact emails. When a pipeline run completes, the contacts are held in a central pool.

From the **Filter facets** page you define exactly which companies and contacts you want to target (by country, sector, importance, page size, etc.). Once you are satisfied with the filter, you create a campaign from that selection. All matching contacts are pulled into the new campaign automatically.

### Stage 2 — Review and prepare on the Campaign page

After a campaign is created it lands on the **Campaign page**. Before activating you should:

- Remove contacts you do not want to pursue.
- Exclude contacts temporarily — excluded contacts are skipped when outreach runs but remain in the campaign.
- Correct names and titles inline.
- Sync from the Google Drive sheet — contacts can be reviewed and edited in the sheet, then synced back to the database.
- Write and test the outreach email — the campaign page contains the template that goes to every contact. Use the Send test button to send yourself a copy before going live.

Only activate the campaign when both the contact list and the email are fully prepared. Activation queues the emails for sending.

### Stage 3 — Follow-up takes over

After activation and email delivery, the **Follow-up page** becomes the primary workspace. It surfaces every open contact across all campaigns in a single unified view, so you never have to switch between campaign pages to manage conversations.

---

## Follow-up statuses

The follow-up status is the single most important field for tracking where a conversation stands. Update it every time the relationship moves forward.

| Status | Meaning |
|---|---|
| *(none)* | Not yet reviewed or actioned |
| **Open** | On your list; you intend to follow up but have not done so yet |
| **Contacted** | You have sent a follow-up message beyond the initial outreach |
| **Replied** | The contact has replied — positive, neutral, or asking for more information |
| **Meeting booked** | A call or meeting is scheduled |
| **Offer sent** | A proposal or offer has been sent |
| **Accepted offer** | The contact has agreed to proceed |
| **Closed** | Fully resolved — won, lost, or no longer relevant |
| **Not interested** | The contact has declined or explicitly opted out |

Work the statuses honestly. A contact sitting at "Open" for weeks is a signal that it needs a decision: follow up, skip, or close.

---

## Importance levels

Importance is your own prioritisation of how valuable a contact is, independent of where they are in the conversation.

| Level | When to use it |
|---|---|
| **High** | Ideal prospect — strong fit, high potential value, worth extra effort |
| **Medium** | Good fit, worth pursuing but not top priority |
| **Low** | Marginal fit or low expected value; follow up only if capacity allows |
| *(none)* | Not yet assessed |

Use importance together with the due-date filter to build a daily working list: high-importance contacts that are past due or due today.

---

## Visual date indicators

Follow-up dates are colour-coded across the list so you can spot what needs attention at a glance.

- **Red** — the follow-up date has passed. This contact is overdue for action.
- **Amber** — the follow-up is due today or within the next seven days. Plan to act soon.
- No highlight — the date is in the future, or no date has been set.

The same colours apply both in the main table and in the side card.

---

## The history log

Every change to a contact's follow-up fields is automatically recorded in a permanent history log. Over time this becomes a complete record of everything that has happened with each contact — what was said, when, by whom, and whether they replied.

### What gets recorded

| Event | What triggers it |
|---|---|
| **Comment** | A comment or note is saved |
| **Status** | The follow-up status is changed |
| **Follow-up date** | A follow-up date is set or changed |
| **Importance** | The importance level is changed |
| **Email received** | An incoming email from this contact is synced |
| **Email sent** | An outgoing email to this contact is synced |
| **Chat** | A Google Chat conversation is opened from the contact card |

Every entry records the date and time, the user who made the change, and a description of what changed.

### Why this matters

Because every action is logged automatically, you can always answer:

- When did we last follow up, and what did we say?
- Has this contact replied, and when?
- What has a colleague done on a shared contact?
- What is the full conversation thread before picking up the phone?

The log is permanent and cannot be deleted or edited. It accumulates automatically as the team works.

---

## Email sync logic

The email sync connects each contact to your actual email history. When a sync runs, the system searches the outreach email accounts for any thread where the To or From address matches the contact's email. Matches are added to the history log as Email sent or Email received entries.

Each email is identified by a unique message ID. Re-syncing the same period never duplicates entries — only genuinely new emails are added.

Email sync covers the outreach email accounts configured in the system. Personal or secondary email accounts not registered in the system are not included.

---

## Shared contacts and multi-user teams

When multiple team members work on the same campaign, all their actions appear in the same history log. This means:

- You can see at a glance whether a colleague has already followed up before you do.
- The status a colleague sets is visible to everyone immediately.
- Comments and notes from all team members are interleaved chronologically.

There is no locking or conflict resolution — the last save wins for editable fields. Use the history log and the comment field to coordinate.
