# Send Outreach Mail

This is the stage between preparing a campaign and working the replies. It explains what has to be true before a mail goes out, who gets mail, how many mails can go out, how to start a send, and what the system updates afterwards. For the technical details of the sender (command line, API, scheduler) see [Smart Mail](doc-viewer.html?doc=smart-mail).

---

## The short version

1. The campaign is prepared: contacts reviewed, mail sequence written, mail account chosen, status **Ready** or **Active**.
2. You **tick** the contacts who should get the first mail (the *Send* column).
3. The sender picks the ticked contacts, respects the send budget of the mail account, and sends.
4. Each sent mail is logged on the contact, and the site is marked **Contacted**.
5. Reminders follow automatically on their own schedule. You never tick reminders.
6. You check **Outreach status** to see what is waiting and what is left of the budget.

---

## Before anything is sent

A campaign can only send when all of this is true:

| Requirement | Where to set it |
|---|---|
| Campaign status is **Ready** or **Active** (intro) / **Active** (reminders) | Campaign page |
| The mail sequence has an **Intro** step | Campaign page, mail editor |
| The campaign has an **outreach mail account**, and the account is complete (host, user name, password, or Gmail sign-in) | Campaign page, mail account |
| The contact is **pending**, not excluded | Contacts tab |
| The contact's site is **pending** or **active**, not excluded | Sites tab |
| For the first mail: the contact is **ticked** (when *Require confirmation* is on) | Send column |

The **Outreach status** page (Daily Admin) shows these checks for every campaign. A campaign that cannot send is listed as *not ready*, with the reason (for example "no Intro mail step" or "mail account not found").

---

## Who gets the first mail: the Send tick

New campaigns have **Require confirmation** switched on. The first (intro) mail is then only sent to contacts you have ticked.

- Tick contacts in the **Contacts** tab (*Send* column), in the expanded site list, or on the contact page.
- The header tick confirms everything currently shown. **Confirm shown** and **Unconfirm** do the same with a popup that states the count.
- Filter on **Confirmed to send** or **Not confirmed** to review the list before sending.
- Only pending contacts that have not received a mail can be ticked. Once the first mail is sent, the tick is locked.
- Turning *Require confirmation* off sends the first mail to every pending contact. Do this deliberately.

Reminders never need a tick: they only go to contacts who already received the first mail.

---

## Sequence: intro and reminders

The mail sequence has an Intro step followed by reminder steps, each with a delay in days.

- **Intro** goes to pending contacts with no mail sent.
- **Reminders** go to pending contacts that already received at least one mail, when the next step is due. The delay counts from the contact's **first** sent mail, so contacts that enter the campaign on different days move through the sequence on their own clock.
- Reminders require the campaign to be **Active**. The first successful send turns a **Ready** campaign into **Active**.
- A contact that replies, bounces, or is excluded stops receiving reminders.

---

## How many mails can go out: the send budget

Each mail account has its own budget so one account is never overloaded.

- Limits are set on the Outreach Settings page (defaults: **50 per hour**, **300 per day**).
- Both are **rolling** windows: the last 60 minutes and the last 24 hours, counted from the mails already sent from that account (all campaigns together).
- Runs that are in progress reserve part of the budget, so two runs cannot overspend it together.
- A run sends at most its budget, with a short pause between mails. Failed attempts count too.
- If the failure rate gets too high (default 5%, after at least 8 attempts) the run stops for that account.

What does not fit in the budget stays waiting and goes out in a later run.

---

## Three ways to send

All three use the same sender, so the same rules apply to every one of them: the same selection (including the Send tick), the same send budget, the same logging afterwards. Only the way you start it differs.

| | From the page | From the command line | Scheduled (Cloud Scheduler) |
|---|---|---|---|
| **What it is** | **Send now** button on the **Outreach status** page | `python app/outreach_send.py` on a computer with the project | A Cloud Scheduler job that calls the `smartMail` function |
| **Who can use it** | Admins | Someone with the project and credentials | Runs by itself, no person needed |
| **Sends real mail?** | Yes, after a confirmation popup | **No by default (dry run).** Only with `--send` | Yes, if the job body has `"dry_run": false` |
| **Choose campaigns** | Yes, tick them in the popup (or all ready campaigns) | `--campaigns A,B` (default: all) | `campaigns` in the job body (default: all) |
| **Intro / reminders** | Both | `--mode intro`, `followup` or `both` | `mode` in the job body (normally `both`) |
| **Best for** | A deliberate send right now, with the numbers in front of you | Previewing what a run would do; testing | The normal day-to-day sending |

### 1. From the page: Send now

1. Open **Daily Admin → Outreach status** and check the numbers.
2. Click **Send now**. A popup lists the ready campaigns with their waiting intro mails and reminders. All are ticked; untick the ones you do not want.
3. The popup shows how many mails will be sent now, limited by each account's budget. Click **Send**.
4. The job runs in the background. The page shows its progress and, when it is done, how many were sent, failed and skipped.

It refuses to start while another outreach send is queued or running. There is no cancel button once it has started, so check the popup before you confirm.

#### What the Outreach status page shows

| Area | Content |
|---|---|
| **Tiles** | Campaigns ready / not ready, intro mails to send, reminders due, intro mails not confirmed, what the next run sends, what is left over. |
| **Send budget per mail account** | Sent in the last hour and day against the limits, reserved by running sends, budget now, mails waiting, what the next run sends, what is left over, and a state (*ready*, *budget used up*, or the account problem). |
| **Campaigns** | One row per campaign: *ready* or *not ready* with the reasons, status, mail account, intro waiting, not confirmed, site blocked, intro to send, reminders waiting and due, mailed and contacts. Filter on ready / not ready, or search. |
| **Buttons** | **Send now** (admins), **Check replies**, **Refresh**. |

The page only reads data. Nothing is sent until you click **Send now** and confirm.

### 2. From the command line

The command line is a **dry run unless you add `--send`**. A dry run selects and renders every mail exactly like a real send, but sends nothing and writes nothing.

```bash
python app/outreach_send.py --dry-run --mode intro --limit 20      # preview
python app/outreach_send.py --mode followup --preview              # preview with text snippets
python app/outreach_send.py --send --mode intro --limit 20         # REAL mail
python app/outreach_send.py --send --mode both --campaigns NO_jun,SE_jun
python app/outreach_send.py --list-campaigns
```

Use the dry run to check which contacts a run would pick (it honours the Send tick and the budget) before you send for real.

### 3. Scheduled sendmail

A Cloud Scheduler job calls the `smartMail` function on a fixed schedule. The function queues the same background job as the page does (`outreach-send`) and the sender works through the waiting mails within the budget. Each run sends no more than the account budget, so a frequent schedule spreads the day's mails out over time.

The job is a normal HTTP job:

| Setting | Value |
|---|---|
| Target | `POST https://us-central1-blueboot-market.cloudfunctions.net/smartMail/outreach-send` |
| Body | `{"mode": "both", "limit": 50, "campaigns": "", "dry_run": false}` |
| Authentication | OIDC token of a service account that may invoke the function. Never expose this as an open URL. |
| Schedule | Your choice, as a cron expression. A frequent schedule (for example every 15 minutes in working hours) gives smooth sending because every run is capped by the budget. |

> The scheduler job itself lives in Google Cloud, not in this repository, so its name, schedule and service account are whatever was set up there. Find it with the first command below.

#### Start the scheduled sendmail

If the job does not exist yet, create it (adjust name, schedule, time zone and service account):

```bash
gcloud scheduler jobs create http outreach-send \
  --location=us-central1 \
  --schedule="*/15 8-17 * * 1-5" --time-zone="Europe/Oslo" \
  --uri="https://us-central1-blueboot-market.cloudfunctions.net/smartMail/outreach-send" \
  --http-method=POST \
  --headers="Content-Type=application/json" \
  --message-body='{"mode":"both","limit":50,"campaigns":"","dry_run":false}' \
  --oidc-service-account-email=SERVICE_ACCOUNT_EMAIL
```

If it exists but is paused, resume it. To test, trigger it once by hand:

```bash
gcloud scheduler jobs list --location=us-central1          # find the job and its state
gcloud scheduler jobs resume outreach-send --location=us-central1
gcloud scheduler jobs run    outreach-send --location=us-central1   # run once now
```

The same actions are in the Google Cloud console: **Cloud Scheduler**, select the job, then **Resume** or **Force run**.

Before switching it on for the first time, create a second job (or edit the body) with `"dry_run": true` and read the job result: it tells you how many mails would be sent.

#### Stop the scheduled sendmail

```bash
gcloud scheduler jobs pause outreach-send --location=us-central1
```

**Pause** stops all future runs and keeps the job so you can resume it later. This is the normal way to stop sending. To remove it for good, use `gcloud scheduler jobs delete outreach-send --location=us-central1`.

Things to know when stopping:

- **A run that has already started keeps going.** Pausing stops new runs only. A running send ends when it has used its budget or finished its list, normally within minutes. There is no cancel for a started job.
- **Queued jobs:** if jobs are waiting but have not started, pausing the Cloud Tasks queue holds them: `gcloud tasks queues pause crm-queue --location=us-central1` (resume with `resume`). This pauses every CRM job, not only sending.
- **Stop only some mail** without touching the scheduler: set the campaign to **On hold**, or untick the contacts, or set the hour limit very low on the Outreach Settings page. These take effect at the start of the next run.
- **Emergency stop:** pause the scheduler job, then set the active campaigns to **On hold**.
- The **Send now** button and the command line keep working while the schedule is paused, so you can still send by hand.

> Note: the older route `/api/crm/outreach-send` is meant for the scheduler only. It is declared as blocked for browsers in the access rules, but that rule is not enforced at runtime yet, so a signed-in campaign-user could still call it. Use **Send now** instead; it is limited to admins and refuses to run twice at the same time.

---

## What happens after a mail is sent

For every mail the system:

- adds the mail to the contact's **sent history** and to the history log (with the full text, so you can read what was sent),
- sets the contact's follow-up status to **Contacted**,
- sets the **site's** follow-up status to **Contacted**, but only if the site had no status (a status you set is never overwritten),
- turns a **Ready** campaign into **Active**,
- records the mail so the send budget stays correct,
- refreshes the campaign statistics.

---

## After sending: replies

Run **Check replies** on the Outreach status page (or let the scheduler do it). Incoming mail is matched to the contact and the system:

- marks the contact as having new mail and sets its follow-up status to **Replied**,
- sets the **site** to **Received** if it was empty or **Contacted**,
- moves bounces to the contact as bounced, so no more mail goes there.

From here the **Follow-up page** takes over: see [CRM Follow-up](doc-viewer.html?doc=crm-follow-up).

---

## Checklist before a big send

1. Open **Outreach status** and check that the campaign is **ready** and its mail account shows **ready**.
2. Check *Intro to send*, *Not confirmed* and the budget numbers.
3. Send a test mail from the campaign page.
4. Do a dry run (command line) if you changed the sequence or the selection.
5. Click **Send now**, choose the campaigns, and confirm.
6. Afterwards, run **Check replies** and look at the Follow-up page.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Campaign shows *not ready* | Status is draft/on hold/canceled, no Intro step, or no/incomplete mail account. The reason is listed on the page. |
| *Intro to send* is 0 but contacts are pending | They are not ticked, or their site is excluded. Check *Not confirmed* and *Site blocked*. |
| *Next run sends* is lower than *Waiting* | The account's hour or day budget is used up. The rest goes in a later run. |
| Reminders do not go out | The campaign is not **Active**, or the delay since the first mail has not passed yet. |
| Send now says a send is already running | Another outreach job is queued or running. Wait for it to finish. |
