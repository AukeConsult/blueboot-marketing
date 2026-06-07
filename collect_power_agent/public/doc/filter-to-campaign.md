# From filter to campaign — a step-by-step guide

This guide explains how to go from a raw pool of discovered contacts to a focused outreach campaign using the Filter Facets page. No technical knowledge is required.

---

## Background

### What is the contact pool?

Every site and lead that passes through the Blueboot pipeline ends up with one or more contacts. The ones that have a valid email address are written into a unified list called **email contacts**. Think of it as your master address book — everyone who could potentially receive an outreach email.

At any given time this list might contain thousands of contacts from many different countries, industries, and company sizes. You do not want to email all of them at once. You want to pick a specific, relevant slice and create a focused campaign from it.

### What are filter facets?

Filter facets are the set of selectable values that describe your contacts — things like country, industry sector, company type, email type, site size, and keywords. The system scans the entire contact pool and builds a catalog of every value that actually appears, along with how many contacts have it.

The **Filter Facets page** lets you tick the values you want, save that selection as a named preset, and then turn it directly into a campaign.

---

## The workflow — four steps

### Step 1 — Load a preset

Open the Filter Facets page. Use the **Load facets** dropdown at the top to select a preset. If you are starting fresh, select `site_leads` (the base catalog built from the full contact pool). If a colleague has saved a preset for a specific market or segment, it will appear in the list too.

Switching the dropdown clears the "Save as" name field so you always start clean.

### Step 2 — Select your values

Each filter category appears as a card with checkboxes. Tick the values you want. The logic is:

- **Within one card** — selecting multiple values means "any of these" (OR). For example, ticking both `technology` and `ecommerce` under Sector will include contacts from either industry.
- **Across cards** — each card you use adds a requirement (AND). For example, if you also tick `NO` under Country, the result must be technology OR ecommerce **and** from Norway.

After a count job has run (see step 3), each value also shows a **blue number** next to the total — that is the `selected_count`, meaning how many of your currently matched contacts have that value. It updates every time you save and count, so you can use it to understand the shape of your results without leaving the page.

### Step 3 — Save & count

When your selections look right, type a name in the **Save as** field (e.g. `NO_b2b_personal`) and click **Save & count**. This does two things:

1. Saves your selections as a named preset so you can come back to them.
2. Runs a background count job that scans the contact pool, applies your filter, and tells you exactly how many sites and contacts match — and crucially, how many of those contacts already exist in the email contacts list and are ready for a campaign.

The count result appears as a blue info bar: **N sites · N contacts · N in email contacts**. The number that matters for campaign creation is **in email contacts** — that is the actual pool your campaign will draw from.

If that number is 0, the **Create campaign** button stays disabled. Adjust your selections, save again, and wait for the new count.

### Step 4 — Create campaign

Once the count shows contacts in email contacts, click **Create campaign**. A popup appears asking for:

- **Campaign ID** — the name your campaign will have (e.g. `NO_b2b_personal_jul01`). It is pre-filled with your preset name as a suggestion. Pick something descriptive that includes the month so you can tell campaigns apart later.
- **Dry run** — tick this if you want to see the numbers without actually creating anything. Useful for a final sanity check.

Click **Create** and a background job starts. It will:

1. Scan email contacts and apply your filter.
2. Check every matched contact against all existing campaigns — anyone already in another campaign is automatically excluded (you will see a "Dedup" summary showing which campaigns blocked how many contacts).
3. Write the remaining contacts into your new campaign.
4. Record which filter preset was used and when, so the campaign page shows exactly what filter produced it.

When the job finishes, a success message shows the result: how many contacts were added, how many already existed and were refreshed, and a direct link to open the campaign.

---

## Rerunning on the same campaign

You can run the same preset → campaign flow again at any time. This is safe and expected — it keeps the campaign in sync as new contacts appear in the pool.

On a rerun, the system is careful with contacts that are already in the campaign. If a contact has been emailed or replied, their history is never touched. Only contacts that are still in "pending" status (not yet contacted) can be removed if they no longer match the current filter. New contacts that now match are added. The result summary tells you how many were added, refreshed, removed, or left untouched.

---

## Deduplication explained

A contact can only be in one active campaign at a time. When the campaign is built, every contact's email is checked against all existing campaigns. Anyone already assigned elsewhere is skipped.

The result summary always shows two numbers: how many emails are blocked across other campaigns globally, and how many of those actually overlapped with your filter results. An overlap of 0 is completely normal — it just means none of the blocked contacts happened to match your filter.

---

## Deleting a draft campaign

If you created a campaign by mistake or want to start over, open the campaign page. If the campaign is still in **draft** status, a red **Delete** button appears in the top bar. Clicking it asks for confirmation, shows you the contact count, and then permanently deletes the entire campaign and all its contacts in the background. This cannot be undone, and is only available for drafts — campaigns that have been activated or sent cannot be deleted.

---

## Step 5 — Allocate owner and outreach email

Once the campaign exists, open it from the Campaigns list. Expand the **Campaign details** section and fill in two fields before doing anything else.

**Owner** — select the team member responsible for this campaign. This is the person who will manage replies and follow-up. Choosing an owner also auto-fills the outreach email if that person has a default mailbox configured in their user profile.

**Email account** — select which configured mail account sends and receives for this campaign. Every outreach email goes out from this address, and incoming replies are synced back to the campaign. The contact's status in the contact list updates as mail is tracked — so you can see at a glance who has replied, who has not been contacted yet, and who has bounced. The email account must be set up in **Settings → Mail accounts** before it appears in the dropdown.

Changing the email account at any point saves immediately. You can verify it is working by clicking the eye icon next to the dropdown to inspect the account settings, or use **Send test** on the mail template to confirm delivery.

---

## Step 6 — Review contacts and prepare the mail template

Before activating the campaign, take two more steps: review the contact list and prepare the outreach email.

### Reviewing contacts

The contact list shows every person who will receive an email. Scroll through it and use the search box to find specific names, emails, titles, or websites. For any contact you do not want to include, use the **Exclude** dropdown on that row. This marks them locally without deleting anything yet.

When you have finished reviewing, the **Remove excluded** button appears at the top of the table. Clicking it permanently removes those contacts from the campaign in one go. This is the right time to clean the list — once the campaign is activated, contacts cannot be removed.

### Writing and editing the mail template

Each campaign has its own subject line and email body. Click **Edit** in the Mail template section to open the campaign editor, where you can write the subject and body in either plain text or HTML. You can use personalisation placeholders — for example the contact's first name — so each email feels individual rather than mass-sent.

Once you have written the template, use **Send test** to send a preview to your own address and confirm it looks right. The preview renders the full email exactly as recipients will see it, including CSS styling for HTML templates.

### Activating the campaign

When the owner, email account, contact list, and mail template are all ready, change the campaign status to **dosend** using the status editor on the campaign page. The green **Activate campaign** button will then appear. Clicking it — after confirming — marks the campaign as sent and queues it for outreach delivery. Once activated, the campaign is locked: contacts cannot be added or removed, and the mail template cannot be changed.

---

## Tips

- Use descriptive preset names that include country and month: `NO_b2b_personal_jul01` is much easier to find later than `test2`.
- Always count before creating. The "in email contacts" number is the one that matters — not the total contacts count.
- If the count is lower than expected, check whether you have selected values across multiple cards. Each card you use narrows the results.
- A large dedup overlap (many contacts blocked by other campaigns) means your filter overlaps with an existing campaign. Consider narrowing by an additional field, or check whether the other campaign is still active.
- The campaign page always shows which filter preset produced it and when, so you can trace back any campaign to the exact selections that created it.
