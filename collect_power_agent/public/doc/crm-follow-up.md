# CRM Follow-up

## The outreach workflow — from discovery to follow-up

Blueboot CRM connects two separate stages: building campaigns and working them. Understanding the flow makes the Follow-up page much easier to use.

---

### Stage 1 — Contacts arrive from the pipelines

Contacts do not get entered manually. They come from the discovery pipelines — the automated processes that scan the web for companies and extract contact emails. When a pipeline run completes, the contacts are held in a central pool.

From the **Filter facets** page you define exactly which companies and contacts you want to target (by country, sector, importance, page size, etc.). Once you are satisfied with the filter and a count is confirmed, you create a campaign directly from that selection. All matching contacts are then pulled into the new campaign automatically.

---

### Stage 2 — Reviewing and cleaning on the Campaign page

After a campaign is created it lands on the **Campaign page** (`Campaigns → [campaign name]`). At this point you should review the incoming contacts before activating:

- **Remove contacts** you do not want to pursue — select them and delete them from the campaign.
- **Exclude contacts** temporarily without removing them — use the Exclude checkbox on each row. Excluded contacts are skipped when outreach runs.
- **Edit names and titles** inline — click any name or title cell to correct it.
- **Sync from the spreadsheet** — the campaign connects to a Google Drive sheet. You can review and edit contacts in the sheet, then sync back.
- **Prepare the outreach email** — the campaign page contains the email template that will be sent to every contact when the campaign is activated. This is the first and most important thing to get right. Expand the **Mail template** section on the campaign page, write a clear subject line and a personalised message body, and use the **Send test** button to send yourself a test copy before going live. Do not activate the campaign until the email looks exactly as intended.
- **Activate the campaign** — once the contact list is clean and the email is ready, click **Activate campaign**. This marks the campaign as ready for outreach delivery and queues the emails for sending.

Only activate when both the list and the email are fully prepared.

---

### Stage 3 — Follow-up takes over

After a campaign is activated, outreach emails go out. From that point on the **CRM Follow-up** page (`CRM → Follow-up`) becomes the main working tool. It shows every open contact across all campaigns in a single list, without requiring you to switch between campaign pages.

Access it from the navigation bar: **CRM → Follow-up**

---

## Filters

The filter bar lets you narrow the list to exactly the contacts you want to work with right now.

| Filter | What it does |
|---|---|
| **Search** | Free-text match across name, email, website, and title |
| **Owner** | Show only contacts in campaigns assigned to a specific owner |
| **Outreach email** | Show only contacts in campaigns using a specific outreach account — useful when you manage multiple email addresses and want to focus on one |
| **Follow-up status** | Show contacts at a specific stage of the follow-up process |
| **Importance** | Filter by the importance level you have assigned (High / Medium / Low / Not set) |
| **Due date** | Filter by date status: Past due, Due today, Due this week, No date set, or Any date |
| **Contact status** | Defaults to **Open contacts** (excludes already-sent contacts); switch to All, Pending only, Sent only, or Excluded only |

All filters combine — for example, show only high-importance contacts owned by a specific person that are past their follow-up date.

---

## Follow-up fields

Each contact row has four fields you can edit directly on the page. Changes save automatically the moment you leave the field — no Save button needed. A small dot next to the field pulses while saving and turns green on success.

| Field | Description |
|---|---|
| **Follow-up date** | When you plan to follow up next. Past-due dates are highlighted in red; dates due within seven days are highlighted in amber. |
| **Follow-up status** | The current stage of the conversation (see statuses below) |
| **Importance** | Your priority level for this contact — High, Medium, or Low |
| **Comment** | Free-text note — a quick reminder, outcome, or next action |

### Follow-up statuses

| Status | When to use it |
|---|---|
| *(none)* | Not yet actioned |
| **Open** | On your list, not yet followed up |
| **Contacted** | You have sent a follow-up message |
| **Replied** | The contact has replied |
| **Meeting booked** | A call or meeting is scheduled |
| **Offer sent** | You have sent a proposal |
| **Accepted offer** | The contact has said yes |
| **Closed** | Resolved — won, lost, or no longer relevant |
| **Not interested** | The contact has declined |

---

## Visual date indicators

Contacts with a follow-up date are colour-coded at a glance:

- **Red date + red left border** — the follow-up date has passed. Action needed.
- **Amber date + amber left border** — the follow-up is due today or within the next seven days.
- No highlight — the date is in the future or no date is set.

---

## The history log

Every change to a contact's follow-up fields is automatically recorded in a full history log. This is one of the most important features — over time the log becomes a complete record of everything that has happened with each contact.

### What gets recorded

Every event is logged with the date and time, the user who made the change, and a description of what changed:

| Event type | What triggers it |
|---|---|
| **Comment** | You update the comment field |
| **Status** | You change the follow-up status |
| **Follow-up date** | You set or change the follow-up date |
| **Importance** | You change the importance level |
| **Email received** | An incoming email from this contact is synced |
| **Email sent** | An outgoing email to this contact is synced |

### Viewing the history

Click the **chevron button** (›) to the right of the comment field on any contact row. The history panel expands below, showing all entries newest-first.

Email entries are colour-coded: green **IN** badge for received, blue **OUT** badge for sent.

### Why this matters

Because every action is logged, you can always see:
- When you last followed up and what you said
- Whether the contact replied and when
- What the conversation history looks like before picking up the phone or writing again
- What a colleague has done on a shared contact

The log is permanent and cannot be deleted. It builds up automatically as you work.

---

## Email sync

The Follow-up page can pull your actual email conversations into the history log, so you see emails alongside your manual notes in one place.

### Sync all contacts

Select a lookback period from the dropdown in the top-right (default: last 7 days), then click **Sync all emails**. The system connects to each outreach email account, finds emails sent to or received from your contacts, and adds them to the history log. A status line shows progress and confirms how many new entries were added.

### Sync one contact

Click the mail icon next to any contact's name to sync only that contact's email history.

### Re-syncing is safe

Emails already in the history are never duplicated — the system recognises each email by a unique identifier and skips anything it has already recorded.

---

## Setting follow-up dates

There are three ways to set a follow-up date, depending on whether you are working with one contact, a hand-picked group, or all contacts that have not been scheduled yet.

---

### On a single contact

Click directly in the **Follow-up date** field on any contact row and pick a date from the date picker. The date saves automatically when you leave the field. No other contacts are affected.

Use this when you have just spoken to someone and want to note exactly when to call or write again.

---

### On a group you choose yourself

1. Tick the checkboxes on the left side of the rows you want to update. You can tick as many as you like across the whole list.
2. A bar appears at the top of the page showing how many contacts are selected.
3. Click **Set follow-up date** in that bar.
4. Pick a date. Optionally add a short comment that will be saved in the history log for every contact in the group (for example: *"batch scheduled after campaign review"*).
5. Click **Apply to N selected**. All selected contacts are updated at once and the panel closes automatically.

Use this when you have reviewed a campaign and want to assign the same next follow-up date to a set of contacts you have chosen individually.

---

### On all contacts that have no date yet

Click the **Set date for contacts without one** button in the filter bar. This does two things at once: it automatically selects every contact currently visible in the list that has no follow-up date set, then opens the date panel so you can pick a date for all of them in one action.

A note next to the button explains this: *"selects all visible contacts that have no follow-up date, so you can set a date for all at once"*.

This is most useful at the start of a working week or after a new campaign has been activated — you can quickly bring all unscheduled contacts onto your calendar without ticking them one by one.

**Tip:** Combine this with the **Due date → No date set** filter first to make sure you are only looking at contacts without a date before clicking the button. That way you only select exactly the contacts you intend to schedule.

---

## Sorting

Click any column header to sort by that column. Click again to reverse the order. You can sort by name, email, website, status, follow-up date, follow-up status, and importance.

---

## Refreshing

Click **Refresh** in the top-right corner to reload all contacts. Filter and sort state is preserved across refreshes.
