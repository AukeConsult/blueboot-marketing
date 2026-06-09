# Follow-up Page — User Guide

The Follow-up page is the day-to-day workspace for managing outreach contacts: tracking who to contact, what stage each conversation is at, and what needs to happen next. It shows every open contact across all campaigns in one place.

---

## 1. Overall layout and filtering

At the top of the page you choose which contacts to work with.

**Campaign, owner, and outreach selectors** narrow the list to a specific campaign, owner, or outreach email address. Select "All campaigns" to see contacts across every active campaign.

**The filter bar** lets you slice the list further:

| Filter | What it does |
|---|---|
| **Search** | Free-text match across name, email, website, and title |
| **Follow-up status** | Contacts at a specific stage. Choose "No status set" to find untriaged contacts |
| **Importance** | High, Medium, Low, or Not set |
| **Contact status** | Defaults to Open contacts (pending + sent). Switch to All, Pending only, Sent only, or Excluded only |
| **Due date** | Past due, Due today, Due this week, No date set, or Any date |

All filters combine — for example, high-importance contacts owned by a specific person that are past their follow-up date.

The contact count on the right of the filter bar shows how many contacts match the current combination.

**Sync** — the Sync button (envelope icon, top right) pulls in emails for all contacts in the current view. Use the sync period dropdown to control how far back to look (7 days to all time). The refresh icon reloads contacts without syncing email.

---

## 2. List view and grouped view

Use the view toggle buttons in the top-right of the contacts card to switch layouts.

### List view

Contacts appear as a flat, sortable table. Click any column header to sort by that field; click again to reverse.

Columns shown depend on screen width — Email and Follow-up status are always visible. As the screen narrows, other columns are progressively hidden: Status and Follow-up date disappear below lg, Name and Importance below xl, Phone and Website below xxl.

**Editing directly in the list** — most fields are editable without opening the side card. Click into a name, title, or phone cell to edit, then press Enter or click away to save. A brief green dot confirms the save. Follow-up date, follow-up status, and importance are also editable inline in each row.

**Adding a comment** — a comment row sits beneath each contact row. Type a note and press Enter or click away; the note is saved and added to the contact's history automatically.

**Syncing one contact's email** — click the mail-bolt icon next to any contact name to sync only that contact's email history.

### Grouped view

Contacts are grouped by a primary field and optionally a secondary field. Use the two group-by dropdowns (Status, Importance, Date, Owner) to choose the grouping.

Click a group header to collapse or expand it. The collapse-all button (arrows icon) folds every group at once — useful when scanning a long list.

All the same inline editing features apply within each group's rows.

---

## 3. Sorting

Click any column header to sort by that column; click again to reverse. Sortable columns: email, name, website, status, follow-up date, follow-up status, and importance. Sort state is preserved when you refresh.

---

## 4. The contact side card

The side card is a detail panel that slides in from the right. Toggle it with the sidebar icon button in the top-right of the contacts card.

**Opening a contact** — while the side card is open, click anywhere on a contact row background (not directly on an input field) to load that contact. Clicking an input field in a different row also switches the card to that contact. The active row is highlighted in blue.

The card has the following sections:

### Header

Shows the contact's avatar initials, editable name, editable job title, and contact status. Icon buttons on the right open shortcuts to every channel that has a value filled in — email, phone, LinkedIn, Telegram, WhatsApp, Teams, Google Chat, Messenger, and others.

### Comment

A full-width text field immediately below the header. Type a note and press Enter or click away to save. Each saved comment is logged in the history with your name and timestamp.

### Channels (collapsible)

Click the **Channels** heading to expand or collapse. Lists all available outreach channels: LinkedIn, Twitter, Facebook, Instagram, WhatsApp, Teams, Telegram, Google Chat, and Messenger. Channels with a value filled in are sorted to the top.

Each row has an editable input for the handle or URL, and a shortcut button that opens the channel. For **Google Chat**, clicking Open Chat also logs one interaction entry to the history for the day — so you have a record that a chat happened without creating duplicate entries if you open it multiple times.

### Contact

Email (clickable mailto link), editable phone field, and website link.

### Follow-up

Three editable fields that save immediately on change and update in the main list in real time:

| Field | Notes |
|---|---|
| **Follow-up date** | Overdue dates appear in red; due today/this week in amber |
| **Follow-up status** | Full status list — see CRM Follow-up guide for status definitions |
| **Importance** | High, Medium, or Low |

### History

A log of all recorded interactions: comments, status changes, date changes, importance changes, email sent and received events, and channel interactions. Shows the most recent 5 entries; the count in the heading shows the full total.

---

## 5. Row selection

Each row has a checkbox on the left. Tick it to select that contact. The header checkbox selects all contacts currently visible (matching the active filters).

In grouped view, each group header has its own checkbox that selects or deselects all contacts in that group.

The selection persists while you scroll, filter, and sort — allowing you to build a selection across multiple searches before applying an action.

---

## 6. Action bar

When one or more contacts are selected, the **Action bar** appears above the table. It shows the number of selected contacts and gives you three actions:

**Clear** — deselects all contacts and hides the bar.

**Set follow-up date** — expands a panel directly inside the bar. Pick a date and optionally add a short comment (for example: *"batch scheduled after campaign review"*). Click **Apply to N selected** to write that follow-up date to every selected contact at once, with a history entry on each. The panel closes automatically when done.

**Move to campaign** — expands a panel inside the bar where you choose a destination campaign. You can either pick an existing campaign from the dropdown, or type a name to create a new one. Click **Move** to transfer all selected contacts. The contacts disappear from the current view and appear in the target campaign. If the target is a new campaign, you are set as its owner automatically.

Only one panel is open at a time — opening one closes the other. The action bar disappears when the selection is cleared.

---

## 7. Email sync

The page pulls actual email conversations into each contact's history log, showing emails alongside manual notes in one place.

**Sync all** — select a lookback period from the dropdown in the header (default: 7 days), then click Sync. The system checks each outreach email account for emails to or from your contacts and adds new ones to the history. A status line confirms progress and how many new entries were added.

**Sync one contact** — click the mail-bolt icon next to any contact name to sync only that contact.

**Re-syncing is safe** — emails already in the history are never duplicated. Each email has a unique identifier; anything already recorded is skipped.
