# Multi-account plan (future work)

Goal: other customers can sign up and get their own isolated account (data, settings, mailboxes, jobs).
Status: **planned, not started.** Decisions below were made on 2026-10-17.

## Current state (why it is not isolated today)

- Roles only: `guest` < `user` < `campaign-user` < `admin` (`settings/users/users/{email}`, see `readme-access.md`).
- Campaigns have an `owner` (email). Non-admins are limited to campaigns they own (server check in
  `functions-crm/main.py` `check_auth` + `_owner_allowed` in `handlers/shared.py`; page-side filtering in
  `crm-follow-data.js`, `campaign.html`, `campaigns.html`). This is per-person scoping, not per-customer.
- Settings are global: `settings/mail_accounts`, `settings/send_limits`, `settings/users`.
- Shared collections: `campaigns` (+ `campaign_contacts`, `campaign_leads`), `outreach_sent`,
  `send_run_reservations`, `sites_excluded`, `filter_facets`, `user_prefs`, jobs/runs, lead DBs (`site_leads`, `leads`).
- Background jobs (outreach send, reply matcher, scrapes) loop over all campaigns and use the global mailboxes.
- **`firestore.rules` is `allow read, write: if true`** — anyone with the project id can read/write everything.
  `campaign.html` also reads the user list through the open Firestore REST API.

## Options

**A. One Firebase project per customer.** Same code deployed to another project/hosting site. Full isolation,
almost no code change (~1 day per customer), but every release is deployed N times. Fine for ~3–5 customers.

**B. Accounts inside one project (chosen direction).** Everything lives under the account document.

## Target data model (option B)

```
accounts/{accountId}
  settings/…                (mail accounts, send limits, users, branding)
  campaigns/{campaignId}
    campaign_contacts/…
    campaign_leads/…
  outreach_sent/…  sites_excluded/…  user_prefs/…  filter_facets/…  jobs/…
users/{email}  ->  { account_id, role }      # lookup used by check_auth
```

- Isolation comes from the path: the server builds every collection path from the caller's account.
- Roles per account: account admin (manages users/settings of the account) vs platform admin (you).
- `owner` stays as "person inside the account"; the owner scoping we have now then applies within an account.
- Lead DBs (`site_leads`, `leads`) stay global and read-only; leads are copied into a campaign when used
  (or one copy per account if preferred).

## API design

- Keep URLs unchanged (`/api/crm/campaigns/...`). The account comes from the ID token:
  `check_auth` sets `g.account_id` from the user record.
- One helper, e.g. `acct_col(db, "campaigns")` in `handlers/shared.py`, builds `accounts/{g.account_id}/…`.
  All ~74 `collection("campaigns")` call sites (and CLI scripts) switch to it.
- Platform admin may act on another account via `X-Account-Id` / `?account=` (admin only).
- `collection_group("campaign_contacts")` (follow-up list) becomes a loop over the account's campaigns;
  update `firestore.indexes.json`.
- Jobs and Cloud Tasks payloads carry `account_id`; scheduled jobs loop over accounts and use that
  account's mailboxes and limits. CLI scripts get `--account` (default: the first/own account).
- The path guard in `check_auth` (owner lookup) is replaced by the path itself.

## Steps (in order)

1. **Lock down Firestore rules** — close direct client access, route everything through the API;
   remove direct Firestore/REST calls from pages (e.g. user list in `campaign.html`). *~0.5 day. Do first, needed for both options.*
2. **Account on the user + `check_auth`** — `account_id` on user records, `g.account_id`, account-admin vs platform-admin. *~1 day*
3. **Path helper + move campaigns and shared collections** under `accounts/{id}/…`. *~2–3 days*
4. **Move settings** (mail accounts, send limits, users) under the account. *~1 day*
5. **Make jobs / reply matcher / CLI account-aware.** *~1 day*
6. **Migrate existing data** — one-off script: copy to `accounts/<yours>/…`, verify, then switch over (keep the old data until verified). *~0.5–1 day*
7. **Signup + onboarding** — create account, invite users, platform-admin page. *~1–2 days*

Total ≈ 7–9 working days.

## Open decisions

- Lead DB: shared read-only vs one copy per account.
- Option A first as a stop-gap for an early customer, while B is built?
- Sign-up: open self-service or invite-only?
- Billing/limits per account (send limits already exist; plan limits are new).

## Risks / notes

- Mailbox credentials per account must be stored per account and never be readable by other accounts.
- Reply matching must only touch the account's own mailboxes.
- Existing hosting caches pages ~1h; bump `?v=` tags when releasing changes to shared JS.
