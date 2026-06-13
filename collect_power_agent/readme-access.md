# Access Control

## Overview

Access is enforced at two layers independently:

- **Frontend** — `requireRole()` in `crm-common.js` redirects unauthenticated or insufficient-role users before any page renders.
- **Backend** — a Flask `before_request` hook in `functions-crm/main.py` verifies the Firebase ID token and checks the caller's role against per-Blueprint minimums on every API request.

Both layers read the same role value from the same Firestore document, so they stay in sync automatically.

---

## Role hierarchy

| Role | Level | How a user gets it |
|---|---|---|
| `guest` | 0 | Signed in but no role doc exists, or role field is empty / unrecognised |
| `user` | 1 | Assigned by an admin |
| `campaign-user` | 2 | Assigned by an admin |
| `admin` | 3 | Assigned by an admin |

Higher levels inherit all permissions of lower levels (an `admin` can do everything a `campaign-user` can, etc.).

---

## Where roles are stored

```
Firestore: settings/users/users/{email}
  role: "user" | "campaign-user" | "admin"   (absent = guest)
```

Roles are managed via the **Users** page (`users.html`) — admin only.

---

## Frontend

### Public pages (no login required)

```
index.html
login.html
doc-viewer.html
```

All other pages require the user to be signed in and to have a role of at least `user`.

### How it works

1. Every page except the public ones calls `requireAuth()` (from `auth.js`), which waits for Firebase Auth to resolve. If not signed in, the user is redirected to `login.html`.
2. After auth, `requireAuth()` calls `_fetchRole()` which reads the user doc from Firestore. If the doc is missing or the role is empty, it returns `'guest'`.
3. `requireRole(PAGE_ROLES[page])` is then called. If the user's role is not in the page's allowed list (which never includes `guest`), they are redirected to `index.html`.
4. `index.html` detects the `guest` role and shows a warning banner: *"Your account is pending access — contact an administrator."*

### Key files

| File | Role |
|---|---|
| `public/js/auth.js` | `_fetchRole()` — reads role from Firestore; falls back to `'guest'` |
| `public/js/crm-common.js` | `PAGE_ROLES` map, `requireRole()`, `requireAuth()` |
| `public/index.html` | Shows `#guest-notice` banner for guests |

### PAGE_ROLES map (crm-common.js)

```js
const PAGE_ROLES = {
  'campaigns.html':     ['admin', 'campaign-user', 'user'],
  'campaign.html':      ['admin', 'campaign-user', 'user'],
  'campaign-edit.html': ['admin', 'campaign-user', 'user'],
  'crm-bp.html':        ['admin', 'user'],
  'crm-sync.html':      ['admin', 'user'],
  'crm_follow.html':    ['admin', 'campaign-user', 'user'],
  'mailbox.html':       ['admin', 'campaign-user', 'user'],
  'jobs.html':          ['admin', 'campaign-user', 'user'],
  'statistics.html':    ['admin', 'campaign-user', 'user'],
  'filter-facets.html': ['admin', 'campaign-user', 'user'],
  'gdisk.html':         ['admin', 'campaign-user', 'user'],
  'settings.html':      ['admin'],
  'users.html':         ['admin'],
};
```

> **Rule:** when adding a new page, always add it to `PAGE_ROLES` with the correct minimum role. Never omit a page unless it is explicitly listed in `PUBLIC_PAGES`.

### Token attachment

`fetchJSON()` in `crm-common.js` automatically attaches `Authorization: Bearer <token>` to every API request (GET and non-GET). This means the backend can verify the caller on all requests without any per-page or per-call setup.

---

## Backend

### How it works

A Flask `before_request` hook runs before every route handler in `functions-crm/main.py`:

Note: the clean policy is defined in `functions-crm/auth_settings.py`. `main.py`
still uses the legacy runtime tables until the guarded `check_auth()` switch is approved.

```
Request arrives
  ├─ OPTIONS?                      -> skip (CORS preflight)
  ├─ /api/crm/worker/?             -> skip (Cloud Tasks OIDC)
  └─ Everything else:
     1. Verify Firebase ID token from Authorization header
        └─ Missing / invalid -> 401
     2. Fetch role from Firestore (settings/users/users/{email})
        └─ Missing / unrecognised -> 'guest'
     3. Attach to flask.g (user_email, user_role)
     4. Match method + path to an auth rule
     5. Role below rule minimum?
        └─ yes -> 403
```

### Role model summary

| Role | GET reads | POST / PATCH / PUT / DELETE |
|---|---|---|
| `guest` | Low-risk general routes only | none |
| `user` | Normal campaign views | own page state only |
| `campaign-user` | Campaign views, campaign work, jobs, Smart Mail, operational tools | campaign work, jobs, Smart Mail, operational tools |
| `admin` | everything, including settings and users | everything, including settings and users |

### Central API policy

Backend API rules are organized in `functions-crm/auth_settings.py` by business intent,
not by Flask blueprint internals:

| Group | Min role | Routes |
|---|---|---|
| Guest-readable general routes | `guest` | `/`, `/api/crm/whoami`, statistics, filter metadata, lead lookup |
| Campaign read views | `user` | campaign/contact/follow-up reads, job status/history, batch reads, Drive file reads, mailbox tag reads |
| Personal page state | `user` | `GET/PUT /api/crm/user-prefs` |
| Campaign work | `campaign-user` | campaign writes, contact writes, job triggers, Smart Mail, batch mutations, Drive file writes |
| Smart Mail direct aliases | service role | `/outreach-send`, `/inbound-read`, `/reply-match` |
| Settings and users | `admin` | all `/api/crm/settings/...`, `/api/crm/gdisk/settings`, `/api/crm/auth/users...` |

Service-role access is defined at the top of `functions-crm/auth_settings.py` in
`SERVICE_ROLE_POLICIES`. The code should validate service identity by Google/IAM
role membership, not by hardcoded service account email address.

### Frontend state persistence (`frontend-status` collection)

`GET /api/crm/user-prefs?page=<name>` and `PUT /api/crm/user-prefs?page=<name>` read
and write the caller's own frontend state. Firestore path:

```
frontend-status/{user_email}/pages/{page_name}
```

Each page gets its own document. Currently registered pages:

| Page name | HTML page | What is stored |
|---|---|---|
| `followup` | `crm_follow.html` | Owner/campaign/outreach filters, view mode, group fields, search, filter bar, sort |

`GET` and `PUT` require at least `user`. This is intentionally treated as personal
page state, not campaign data mutation.

### Settings and users — admin only

Any endpoint that reads or writes settings, mail-account configuration, gdisk
settings, mail-tag status settings, or user role documents requires `admin`.

| Endpoint family | Min role | Notes |
|---|---|---|
| `/api/crm/settings/...` | `admin` | mail accounts, mailbox-by-account reads, messages, mail-tag statuses |
| `/api/crm/gdisk/settings` | `admin` | Drive folder/system configuration |
| `/api/crm/auth/users...` | `admin` | user role management |

### Server-side user identity

Once the token is verified, `flask.g.user_email` holds the real authenticated identity. Route handlers that write to `comment_history` (e.g. `update_campaign_contact`) use this value instead of trusting a `_user` field sent from the client.

### Auth caching

`functions-crm/auth_cache.py` caches `settings/users/users/{email}` role lookups
for 300 seconds per warm Firebase Function instance. This reduces Firestore reads on
busy instances. Role changes may take up to that TTL to appear on a warm instance;
cold starts always begin with an empty cache.

### Key files

| File | Role |
|---|---|
| `functions-crm/auth_cache.py` | Warm-instance user role cache |
| `functions-crm/auth_settings.py` | Editable API route/auth/role policy table |
| `functions-crm/main.py` | `before_request` hook and current runtime enforcement |
| `functions-crm/handlers/shared.py` | `_get_user_role()`, `ROLE_LEVELS` |

---

## Adding a new role-protected endpoint

**Backend:**
1. Add the route to the correct handler Blueprint (or a new one).
2. Add or update the matching `ApiRule` in `functions-crm/auth_settings.py`.
3. If runtime enforcement still depends on the legacy tables in `main.py`, keep those aligned until `check_auth()` is switched fully to `auth_settings.py`.
4. If it is a write endpoint, use `flask.g.user_email` for any audit trail.

**Frontend:**
1. If it is a new page, add it to `PAGE_ROLES` in `crm-common.js`.
2. If it calls the API, use `fetchJSON()` — token is attached automatically.

---

## Error responses

| Code | Meaning |
|---|---|
| `401` | No token or invalid/expired token — user must sign in |
| `403` | Valid token but insufficient role — guest or wrong role for this action |


### Blueprint minimum read roles

GET (read) requests are controlled by `ApiRule` entries in
`functions-crm/auth_settings.py`.

**Rule:** when a route returns campaign data, use at least `user`. When it returns
settings, credentials, mail-account configuration, or user role documents, use `admin`.

| Route family | Min read role | What it protects |
|---|---|---|
| Campaign reads | `user` | Campaign docs and follow-up/contact views |
| General metadata/statistics | `guest` | Low-risk general dashboard support data |
| Operational tools | `campaign-user` | Jobs, batch, Drive files, mailbox tags |
| Settings and user docs | `admin` | Credentials, system config, role management |

The policy is role-level based, so higher roles inherit lower-role access.

**How to add a new blueprint:**
1. Add or update the matching `ApiRule` in `functions-crm/auth_settings.py`
2. Add a row to this table in `readme-access.md`
3. Add a rule note to `CLAUDE.md` if it introduces a new category of protected data

---

### Job-trigger endpoints

GET endpoints that start background jobs are `campaign_work(...)` rules in
`functions-crm/auth_settings.py` and require `campaign-user`. Read-only job
status/history endpoints are `user_read(...)` rules.

| Endpoint | Blueprint | Why blocked for guests |
|---|---|---|
| `GET /api/crm/contact-sync` | `jobs` | Starts a contact-sync job |
| `GET /api/crm/push-and-sync` | `jobs` | Starts a push-and-sync job |
| `GET /api/crm/template-sync` | `jobs` | Starts a template-sync job |
| `GET /api/crm/crm-sync` | `jobs` | Starts a CRM sync job |
| `GET /api/crm/campaign-sync` | `jobs` | Starts a campaign-sync job |
| `GET /api/crm/campaign-export` | `jobs` | Starts a campaign-export job |
| `GET /api/crm/discover-campaigns` | `campaigns` | Starts CRM sync jobs |
| `POST /api/crm/inbound-read` | `inbound_read` | Starts inbound mail read job |
| `POST /api/crm/statistics/collect` | `statistics` | Starts statistics job |

---

## Adding a new role-protected endpoint

**Backend:**
1. Add the route to the correct handler Blueprint (or a new one).
2. Add or update the matching `ApiRule` in `functions-crm/auth_settings.py`.
3. If runtime enforcement still depends on the legacy tables in `main.py`, keep `_BLUEPRINT_MIN_ROLES`, `_BLUEPRINT_MIN_READ_ROLES`, `_JOB_ENDPOINTS`, and `_ADMIN_ENDPOINTS` aligned until `check_auth()` is switched fully to `auth_settings.py`.
4. If it writes, use `flask.g.user_email` for any audit trail.

*

