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

```
Request arrives
  ├─ OPTIONS?                      → skip (CORS preflight)
  ├─ /api/crm/worker/?             → skip (Cloud Tasks OIDC)
  └─ Everything else:
       1. Verify Firebase ID token from Authorization header
          └─ Missing / invalid → 401
       2. Fetch role from Firestore (settings/users/users/{email})
          └─ Missing / unrecognised → 'guest'
       3. Attach to flask.g (user_email, user_role)
       4. GET request (not a job endpoint)?
          ├─ Blueprint in _BLUEPRINT_MIN_READ_ROLES?
          │    └─ Role < minimum → 403 (blocks guest + user on sensitive blueprints)
          └─ Otherwise → allowed (any authenticated role)
       5. Job-triggering GET OR any non-GET:
          ├─ guest → 403 "not assigned a role yet"
          ├─ Check Blueprint minimum write role → 403 if insufficient
          └─ Endpoint in _ADMIN_ENDPOINTS? → require admin
```

### Role model summary

| Role | GET reads | POST / PATCH / PUT / DELETE |
|---|---|---|
| `guest` | blocked for sensitive blueprints | blocked everywhere |
| `user` | all internal data | **none — read-only** |
| `campaign-user` | everything | everything except admin endpoints |
| `admin` | everything | everything |

### Per-Blueprint minimum roles (POST / PATCH / PUT / DELETE)

`user` never appears here — all writes require at least `campaign-user`.

| Blueprint | Min role | Routes |
|---|---|---|
| `contacts` | `campaign-user` | Follow-up field updates |
| `inbound_read` | `campaign-user` | Inbound mail read trigger |
| `mailbox` | `campaign-user` | Message box write operations |
| `mail_tags` | `campaign-user` | Tag CRUD |
| `gdisk` | `campaign-user` | Drive file operations |
| `leads` | `campaign-user` | Lead exclusion |
| `statistics` | `campaign-user` | Stats collection trigger |
| `campaigns` | `campaign-user` | Campaign CRUD, discover |
| `jobs` | `campaign-user` | Job triggers |
| `filter_facets` | `campaign-user` | Facet save, create-campaign |
| `mail_accounts` | `admin` | Account CRUD, ping, test-send |
| `auth` | `admin` | User doc management |
| `user_prefs` | `campaign-user` | Save per-page frontend state |

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

Reads are not listed in `_BLUEPRINT_MIN_READ_ROLES` — any authenticated user can read
their own prefs. Writes require `campaign-user` (CLAUDE.md write rule).

### Settings collection — admin only (all write operations)

Any endpoint that writes to the Firestore `settings` collection requires `admin`
regardless of its blueprint's default minimum. These are listed in `_ADMIN_ENDPOINTS`
in `main.py` and checked after the blueprint minimum:

| Endpoint | Blueprint | Writes to |
|---|---|---|
| `PUT /api/crm/settings/mail-tag-statuses` | `mail_tags` | `settings/mail_tag_statuses` |
| `POST/PATCH /api/crm/gdisk/settings` | `gdisk` | `settings/gdisk` |

`mail_accounts` and `auth` blueprints already require `admin` at the blueprint level
and do not need to appear in `_ADMIN_ENDPOINTS`.

### Server-side user identity

Once the token is verified, `flask.g.user_email` holds the real authenticated identity. Route handlers that write to `comment_history` (e.g. `update_campaign_contact`) use this value instead of trusting a `_user` field sent from the client.

### Key files

| File | Role |
|---|---|
| `functions-crm/main.py` | `before_request` hook, `_BLUEPRINT_MIN_ROLES` |
| `functions-crm/handlers/shared.py` | `_get_user_role()`, `ROLE_LEVELS` |

---

## Adding a new role-protected endpoint

**Backend:**
1. Add the route to the correct handler Blueprint (or a new one).
2. Add the Blueprint name to `_BLUEPRINT_MIN_ROLES` in `main.py` with the minimum role.
3. If it is a write endpoint, use `flask.g.user_email` for any audit trail.

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

GET (read) requests are controlled per-blueprint via `_BLUEPRINT_MIN_READ_ROLES`
in `main.py`. Any blueprint not listed allows any authenticated user to read.

**Rule:** add a blueprint whenever its GET responses contain sensitive internal data
(contact details, credentials, campaign data, user docs, or system settings).
When in doubt, add it and set minimum to `campaign-user`.

| Blueprint | Min read role | What it protects |
|---|---|---|
| `campaigns` | `campaign-user` | Campaign docs embed full `campaign_contacts` subcollection |
| `contacts` | `campaign-user` | Direct `campaign_contacts` reads + followup-contacts |
| `gdisk` | `campaign-user` | Drive folder contents + `settings/gdisk` config |
| `mail_accounts` | `campaign-user` | Mail account credentials (`settings/mail_accounts`) |
| `auth` | `campaign-user` | User role docs (`settings/users`) |
| `mail_tags` | `campaign-user` | `settings/mail_tag_statuses` |
| `mailbox` | `campaign-user` | IMAP mailbox contents — no read access for user/guest |

Neither `guest` nor `user` roles can read from any of these blueprints.
Only `campaign-user` and `admin` can.

**How to add a new blueprint:**
1. Add it to `_BLUEPRINT_MIN_READ_ROLES` in `main.py`
2. Add a row to this table in `readme-access.md`
3. Add a rule note to `CLAUDE.md` if it introduces a new category of protected data

---

### Job-trigger endpoints — blocked for guests on GET too

GET endpoints that trigger or monitor background jobs are listed in `_JOB_ENDPOINTS`
in `main.py` and are blocked for guests regardless of HTTP method:

| Endpoint | Blueprint | Why blocked for guests |
|---|---|---|
| `GET /api/crm/contact-sync` | `jobs` | Starts a contact-sync job |
| `GET /api/crm/push-and-sync` | `jobs` | Starts a push-and-sync job |
| `GET /api/crm/template-sync` | `jobs` | Starts a template-sync job |
| `GET /api/crm/crm-sync` | `jobs` | Starts a CRM sync job |
| `GET /api/crm/campaign-sync` | `jobs` | Starts a campaign-sync job |
| `GET /api/crm/campaign-export` | `jobs` | Starts a campaign-export job |
| `GET /api/crm/status/<id>` | `jobs` | Monitors a job guests cannot start |
| `GET /api/crm/jobs` | `jobs` | Shows job history (internal) |
| `GET /api/crm/discover-campaigns` | `campaigns` | Starts CRM sync jobs |
| `POST /api/crm/inbound-read` | `inbound_read` | Starts inbound mail read job |
| `POST /api/crm/statistics/collect` | `statistics` | Starts statistics job |

---

## Adding a new role-protected endpoint

**Backend:**
1. Add the route to the correct handler Blueprint (or a new one).
2. Add the Blueprint name to `_BLUEPRINT_MIN_ROLES` in `main.py` with the minimum role.
3. If its GET response contains sensitive data, also add it to `_BLUEPRINTS_BLOCKED_FOR_GUESTS`.
4. If it triggers a job, add the endpoint name to `_JOB_ENDPOINTS`.
5. If it writes, use `flask.g.user_email` for any audit trail.

*

