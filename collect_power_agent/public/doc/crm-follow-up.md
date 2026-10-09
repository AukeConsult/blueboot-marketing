# CRM Follow-up — Concepts and Logic

This document explains how the follow-up system works, what the statuses mean, and how contacts move through the outreach lifecycle. For instructions on how to use the page itself — filters, the side card, row selection, the action bar — see the [Follow-up page usage guide](doc-viewer.html?doc=followup-page-usage).

---

## The outreach lifecycle

Blueboot CRM connects three things: building campaigns, sending the outreach mail, and working the replies.

### Stage 1 — Contacts arrive

Contacts reach a campaign in one of two ways. Both end up on the same Campaign page.

**From the discovery pipelines.** Automated processes scan the web for companies and extract contact emails into a central pool. On the **Leads DB** page you define which companies and contacts you want (country, sector, importance, page size, etc.) and create a campaign from that selection. All matching contacts are pulled into the new campaign.

**By dropping an Excel file into Campaigns.** Open **Campaigns → Import campaign** and drop an `.xlsx` file onto the upload area (or click *Browse file*). Use this when your list comes from somewhere else: a research team, a purchased list, or a campaign you exported earlier. There are two tabs:

- **Standard (Leads+Contacts)** — a file with a `Leads+Contacts` tab, one row per contact. *Download example file* gives a ready template with the 20 columns and a field guide. Type the campaign ID first. A campaign that does not exist yet is created as a draft.
- **Prospect catalogue** — a BlueSearch prospect file (`BlueSearch-<country>-prospects.xlsx`). Rows are grouped by country into one campaign each (`BS_UK`, `BS_DK`, …), or all into one campaign if you enter a Campaign ID.

How an import behaves:

1. **Dry run first.** It is on by default. The preview shows how many sites and contacts are new, changed or skipped, with warnings. Nothing is written until you click **Confirm and write to Firestore**.
2. **Every email is checked.** Each address goes through the same check used everywhere in the system. Junk such as `logo@2x.avif` is rejected and reported, never added as a contact.
3. **Prospect catalogue: no email, no import.** A row without a proper email is not imported at all, not even as a site. In the Standard import the site is still created, but no contact.
4. **Every contact gets a site.** After the import each contact is linked to a site in the same campaign (matched on website, domain or email domain, otherwise a new site is created). The site list on the Follow-up page shows the contact count per site.
5. **Safe to repeat.** The contact ID comes from the email address, so importing the same file twice updates records instead of duplicating them. Outreach data (status, mails sent, follow-up fields, send confirmation) is never overwritten by an import. A contact that is already active in another campaign is skipped.
6. **New campaigns require confirmation.** Imported contacts are not mailed until you tick them (see Stage 2 and Stage 3).

The full column list and the command-line versions are in [Campaign import & export](doc-viewer.html?doc=campaign-import-export) and [Prospect catalogue sync](doc-viewer.html?doc=prospect-catalogue-sync).

### Stage 2 — Review and prepare on the Campaign page

After a campaign is created it lands on the **Campaign page**. Before activating you should:

- Remove contacts you do not want to pursue.
- Exclude contacts temporarily — excluded contacts are skipped when outreach runs but remain in the campaign.
- Correct names and titles inline.
- Sync from the Google Drive sheet — contacts can be reviewed and edited in the sheet, then synced back to the database.
- Write and test the outreach email — the campaign page contains the template that goes to every contact. Use the Send test button to send yourself a copy before going live.

- **Tick who gets the first mail.** New campaigns have **Require confirmation** switched on. The first (intro) mail is only sent to pending contacts you have ticked in the *Send* column of the Contacts tab (also available in the site list and on the contact page). Reminders are not affected, because they only go to contacts who already received the first mail. Filter on *Confirmed to send* or *Not confirmed* to review the list.

Only activate the campaign when both the contact list and the email are fully prepared. Activation queues the emails for sending. The **Outreach status** page (Daily Admin) shows which campaigns are ready, what is waiting to be sent, and the remaining send budget per mail account.

### Stage 3 — Send the outreach mail

When the campaign is prepared, the first mail goes out. In short:

- **Tick who gets it.** The first (intro) mail is only sent to contacts you tick in the *Send* column. Reminders follow automatically and need no tick.
- **The sender respects the budget.** Each mail account has an hourly and a daily limit (rolling windows). What does not fit waits for a later run.
- **Three ways to start it:** the **Send now** button on the **Outreach status** page (admins, you choose the campaigns), the command line (`python app/outreach_send.py`, a dry run unless `--send`), or the **scheduled sendmail** (a Cloud Scheduler job that sends on its own; pause it to stop and resume it to start again).
- **Afterwards** every sent mail is logged on the contact, the contact and its site become *Contacted*, and a Ready campaign becomes Active. **Check replies** matches incoming mail to contacts and sets the site to *Received*.

#### Sending directly from the Outreach status page

You do not need the command line or the scheduler to send. Open **Daily Admin → Outreach status** and do it from there:

1. **Look at the numbers.** The tiles show how many campaigns are ready, how many intro mails and reminders are waiting, how many contacts are still *not confirmed*, how many mails the next run will send, and how many are left over because of the budget. The budget table shows each mail account's sent mails (last hour and day), what is reserved, and the budget left. The campaign table shows, per campaign, whether it is *ready* or *not ready* and why.
2. **Click Send now** (admins). A popup lists the ready campaigns that have something to send, each with its waiting intro mails and reminders. All are ticked: leave them to send everything, or untick the campaigns you want to skip. The popup shows how many mails will be sent now.
3. **Confirm.** The job starts in the background and the page shows its progress, then *sent / failed / skipped*. The numbers refresh when it finishes. The button refuses to start while another outreach send is running.
4. **Click Check replies** whenever you want incoming mail matched to contacts. The page shows when replies were last checked.

Use **Refresh** to recalculate the numbers at any time. The page is read-only apart from these two buttons.

The full description, with requirements, budget rules and troubleshooting, is in [Send Outreach Mail](doc-viewer.html?doc=send-outreach).

### Stage 4 — Follow-up takes over

After activation and email delivery, the **Follow-up page** becomes the primary workspace. It surfaces every open contact across all campaigns in a single unified view, so you never have to switch between campaign pages to manage conversations.

---

## Follow-up statuses

The follow-up status is the single most important field for tracking where a conversation stands. Update it every time the relationship moves forward.

| Status | Meaning |
|---|---|
| *(none)* | Not yet reviewed or actioned |
| **In-work** | General work state; the contact needs attention or is being worked |
| **Contacted** | A mail has been sent, either from the follow-up popup or set manually |
| **Received** | A mail has come in from the contact; set automatically on the site |
| **Replied** | The contact has replied — positive, neutral, or asking for more information |
| **Meeting** | A call or meeting is scheduled |
| **Offer** | A proposal or offer has been sent or is being handled |
| **Not-interested** | The contact has declined or explicitly opted out |

Sites have a follow-up status too, and two steps are set automatically: when a mail is sent the site becomes **Contacted** (only if it had no status), and when a reply arrives it becomes **Received** (only if it was empty or Contacted). A status you set yourself is never overwritten.

Work the statuses honestly. A contact sitting at "In-work" for weeks is a signal that it needs a decision: follow up, move forward, or mark not interested.

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
