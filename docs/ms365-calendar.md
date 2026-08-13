# Microsoft 365 Calendar Integration — Implementation Plan

**Status:** Planned, not yet implemented.
**Feature:** Premium users connect their Microsoft 365 / Outlook.com calendar via OAuth; Remyndrs syncs upcoming events and sends an SMS reminder before each one through the existing reminder pipeline.
**Consent model:** End-user delegated consent only. One multi-tenant Entra ID app registration; Remyndrs never touches a customer tenant. Hardened enterprise tenants may require the user's own admin to approve — Remyndrs shows a friendly fallback message and is never involved.
**Google Calendar** follows later behind the same provider abstraction (`provider` column everywhere from day one).

---

## Phase 0 — External setup (start first; verification is calendar time)

### Entra ID app registration (one-time, in the simple-it.us tenant)
1. New app registration "Remyndrs".
   - Supported account types: **Accounts in any organizational directory and personal Microsoft accounts** (`AzureADandPersonalMicrosoftAccount`, authority `/common`).
   - Redirect URIs (type **Web**):
     - `https://sms-reminders-api-1gmm.onrender.com/calendar/callback`
     - `http://localhost:8000/calendar/callback` (dev)
2. Delegated permissions (minimal — all user-consentable by default):
   - `Calendars.Read`, `offline_access`, `openid`, `profile`, `email`
   - No `User.Read` needed — the connected account's email comes from ID-token claims (`preferred_username`/`email`).
3. Client secret — **max 24-month lifetime; set a recurring reminder to rotate it** (a silent expiry kills all syncs).
4. Branding: name, logo, publisher domain, links to privacy policy + terms on remyndrs.com.

### Publisher verification (Partner Center)
- Create a Microsoft Partner Center account (MPN ID), verify the domain, mark the app as publisher-verified.
- **Why it matters:** unverified multi-tenant apps trigger Microsoft's risk-based step-up consent — the "Need admin approval" wall — even in default-settings tenants. Verification is the difference between "works for most business users" and "blocked almost everywhere".
- **Not needed for dogfooding:** Brad can consent (or self-admin-approve) in his own tenant immediately, so dev/beta proceeds in parallel with verification.

### Website / policy
- Update remyndrs.com privacy policy: what calendar data is read (event id, time, subject), that only the minimum is stored, tokens encrypted at rest, disconnect at any time. Required for publisher verification anyway.
- Netlify success/error landing pages: `/calendar-connected`, `/calendar-error` (callback redirects there, mirroring the Stripe `/payment/success` → Netlify pattern, `main.py:6316`).

### Render env
- New env group **`Microsoft 365 Calendar`** linked to `sms-reminders-api`, `sms-reminders-worker`, `sms-reminders-beat`:
  - `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, `MS_TENANT=common`
- Add `CALENDAR_BETA_PHONES` to the existing **`Beta phone Numbers`** group (start with Brad's number; exact E.164 `+1...` match, no normalization — same as `SHARED_LISTS_BETA_PHONES`).

---

## Architecture

```
CONNECT CALENDAR (SMS keyword, main.py)
  -> can_connect_calendar() gate (tier_service)
  -> create oauth state row -> SMS short link {API_BASE_URL}/calendar/connect/{token}
  -> GET /calendar/connect/{token}  (routes/calendar_routes.py)
       302 -> login.microsoftonline.com/common/oauth2/v2.0/authorize (state=token, PKCE)
  -> GET /calendar/callback
       validate state -> exchange code (services/ms_graph.py) -> store encrypted tokens
       -> queue initial sync -> confirmation SMS -> 302 remyndrs.com/calendar-connected

Celery Beat (every 5 min)
  -> tasks.calendar_tasks.sync_all_calendars      (fan-out, like check_and_send_reminders)
       -> sync_one_connection.delay(connection_id) per active connection
            -> refresh token if needed -> Graph calendarView delta (next 7 days, UTC)
            -> materialize/update/cancel rows in `reminders` via calendar_events mapping
  -> existing check-and-send pipeline delivers them (no changes to delivery)
```

**Design decision — reuse the `reminders` table for delivery.** Calendar events materialize as ordinary reminder rows (`reminder_date = event start − lead time`). Everything downstream is free: 30s claim loop, `SELECT FOR UPDATE SKIP LOCKED`, retries, snooze, opt-out handling, daily-summary inclusion, delete-by-number. A `calendar_events` mapping table tracks event↔reminder linkage, change detection, and tombstones.

### New files
| File | Purpose |
|---|---|
| `services/ms_graph.py` | OAuth (authorize URL, code exchange, refresh — hand-rolled v2 endpoints via `requests`, PKCE S256) + Graph calls. No MSAL dep; it's 3 HTTP calls, matching house style. **Persist the rotated refresh token returned on every refresh.** |
| `services/calendar_service.py` | Provider-agnostic logic: connect-link creation, sync orchestration, event→reminder materialization, disconnect, needs-reauth handling |
| `models/calendar.py` | CRUD for `calendar_connections`, `calendar_events`, `oauth_states` |
| `routes/calendar_routes.py` | `APIRouter` with the two HTTP endpoints (kept out of 6900-line main.py; included like `cs_portal.router` at `main.py:369-375`) |
| `tasks/calendar_tasks.py` | `sync_all_calendars`, `sync_one_connection`, `cleanup_oauth_states` |

---

## Data model (append to `migrations` list in `database.py`, indexes to `index_migrations`)

```sql
CREATE TABLE IF NOT EXISTS calendar_connections (
    id SERIAL PRIMARY KEY,
    phone_number TEXT NOT NULL,
    phone_hash TEXT,
    provider TEXT NOT NULL DEFAULT 'ms365',
    account_email_encrypted TEXT,          -- which account is linked (from ID token)
    access_token_encrypted TEXT,
    refresh_token_encrypted TEXT,
    token_expires_at TIMESTAMP,
    delta_link TEXT,                       -- Graph delta token for incremental sync
    delta_window_end TIMESTAMP,            -- when the calendarView window needs re-init
    status TEXT NOT NULL DEFAULT 'active', -- active | needs_reauth | disconnected
    lead_time_minutes INTEGER NOT NULL DEFAULT 30,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    reauth_notified_at TIMESTAMP,
    last_sync_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (phone_number, provider)        -- one connection per provider for MVP
);

CREATE TABLE IF NOT EXISTS calendar_events (
    id SERIAL PRIMARY KEY,
    connection_id INTEGER NOT NULL REFERENCES calendar_connections(id) ON DELETE CASCADE,
    external_event_id TEXT NOT NULL,       -- Graph event id (occurrence-level)
    series_master_id TEXT,                 -- set for recurring occurrences
    reminder_id INTEGER,                   -- FK-less on purpose: reminder may be user-deleted
    start_utc TIMESTAMP NOT NULL,
    content_hash TEXT,                     -- sha256(start_utc|subject) for change detection
    status TEXT NOT NULL DEFAULT 'active', -- active | cancelled | dismissed | sent
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (connection_id, external_event_id)
);
-- No subject column: event text lives only in reminders.reminder_text (already
-- encrypted by save path). Mapping table stores ids + hash only — data minimization.

CREATE TABLE IF NOT EXISTS oauth_states (
    token TEXT PRIMARY KEY,                -- secrets.token_urlsafe(32)
    phone_number TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'ms365',
    code_verifier TEXT NOT NULL,           -- PKCE
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    used_at TIMESTAMP                      -- single-use; 15-min TTL enforced in code
);
```

```sql
-- reminders: tag calendar-derived rows
ALTER TABLE reminders ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'user';
-- indexes
CREATE INDEX IF NOT EXISTS idx_cal_conn_status ON calendar_connections(status);
CREATE INDEX IF NOT EXISTS idx_cal_events_conn ON calendar_events(connection_id, status);
```

**Why `reminders.source`:** (1) `recalculate_pending_reminders_for_timezone()` (`models/reminder.py:967`) shifts pending reminders when a user changes timezone — correct for wall-clock reminders, **wrong** for calendar events (absolute UTC). It must exclude `source = 'ms365_calendar'`. (2) Lets display/summaries show the 📅 prefix consistently. No `users` columns are added at all, so `ALLOWED_USER_FIELDS` is untouched.

**Timezone semantics:** calendar reminders are anchored to the event's UTC instant, so they fire at the right moment even if the user travels (a 4:00 PM ET meeting *is* a 1:00 PM PT meeting). The message leads with relative time ("in 30 minutes"), which is timezone-proof; only the parenthetical clock time is rendered in `users.timezone`. On a user timezone change: never shift `reminder_date` for calendar rows, but **do re-render the display text** of pending unsent ones so the parenthetical matches the user's new local clock.

Tokens are stored via the existing `encrypt_field()`/`decrypt_field()` (`utils/encryption.py`). **The feature requires `ENCRYPTION_ENABLED`** — refuse to store OAuth tokens plaintext: `M365_ENABLED = bool(MS_CLIENT_ID and MS_CLIENT_SECRET and ENCRYPTION_ENABLED)` (config idiom #2, like `STRIPE_ENABLED`).

---

## Config & gating

`config.py`:
- `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, `MS_TENANT` (default `common`), `M365_ENABLED` (above), `CALENDAR_BETA_PHONES` (same parse idiom as `SHARED_LISTS_BETA_PHONES`, `config.py:251-261`).
- Add `'calendar_sync'` key to **all four** tier dicts (`FREE_TIER_LIMITS[1]`/`[2]`: `False`; `TIER_PREMIUM`/`TIER_FAMILY`: `True`) — call sites index with `['key']`, partial additions break.
- `CALENDAR_SYNC_WINDOW_DAYS = 7`, `CALENDAR_LEAD_MIN = 5`, `CALENDAR_LEAD_MAX = 120`.

`services/tier_service.py` — `can_connect_calendar(phone_number) -> tuple[bool, str | None]`, modeled on `can_share_list()` (`tier_service.py:387`):
1. If `not M365_ENABLED` → `(False, "Calendar sync isn't available yet.")`
2. If `CALENDAR_BETA_PHONES` non-empty and phone not in it → beta message. (Empty allowlist = open to all Premium, same semantics as shared lists.)
3. If tier not in (premium, family) → upgrade message: `"Calendar sync is a Premium feature. Text UPGRADE to connect your Outlook calendar ($8.99/mo)."`
4. No `BETA_MODE` bypass (match `can_share_list`).

Runtime kill switch (no deploy): `settings` key **`calendar_sync_enabled`** (default `"true"`), checked at the top of `sync_all_calendars` via `get_setting()` — same pattern as `auto_issue_detection_enabled`.

---

## SMS keywords (main.py, before AI processing — insert near the shared-lists handlers, `main.py:3557`)

| Keyword (exact-match alias list, uppercased) | Behavior |
|---|---|
| `CONNECT CALENDAR`, `CALENDAR CONNECT`, `LINK CALENDAR`, `SYNC CALENDAR`, `CONNECT OUTLOOK`, `LINK OUTLOOK` | Gate via `can_connect_calendar()`. Already connected → status message. Else create `oauth_states` row, reply: `"Tap to securely connect your Microsoft calendar:\n{API_BASE_URL}/calendar/connect/{token}\n\nLink expires in 15 minutes."` |
| `DISCONNECT CALENDAR`, `CALENDAR OFF`, `UNLINK CALENDAR` | Set connection `disconnected`, null out tokens, delete **unsent** `source='ms365_calendar'` reminders, confirm. |
| `CALENDAR`, `CALENDAR STATUS`, `MY CALENDAR` | Show: connected account, lead time, last sync, event count in window; or explain how to connect. |
| `CALENDAR LEAD <n>` (regex `^CALENDAR LEAD (\d+)$`) | Set `lead_time_minutes` (clamped 5–120), reschedule pending unsent calendar reminders, confirm. |

Each handler follows the JOIN SHARED LISTS structure (`main.py:3557-3599`): lazy imports, tier check with early return, idempotency check, write, `staging_prefix()` reply, `log_interaction()`. Also add an AI-safeguard: if the AI classifies "connect my calendar to remyndrs" as anything else, natural-language variants in the alias contains-check should catch it first.

The keyword handler does **no external HTTP** (state row + TwiML only), so the 14s Twilio webhook budget is untouched.

---

## OAuth flow (`routes/calendar_routes.py` + `services/ms_graph.py`)

**`GET /calendar/connect/{token}`**
1. Look up state: exists, `used_at IS NULL`, age < 15 min → else 302 to `{APP_BASE_URL}/calendar-error`.
2. 302 to `https://login.microsoftonline.com/common/oauth2/v2.0/authorize` with `client_id`, `response_type=code`, `redirect_uri={API_BASE_URL}/calendar/callback`, `scope=openid profile email offline_access Calendars.Read`, `state={token}`, `code_challenge` (S256 from stored `code_verifier`), `prompt=select_account`.

**`GET /calendar/callback?code=&state=`** (browser GET — 60s middleware timeout applies, not Twilio's 14s)
1. Validate state (same checks), mark `used_at` immediately (single-use, atomic `UPDATE ... WHERE used_at IS NULL RETURNING`).
2. `error=access_denied` / consent failures → SMS the user the admin-blocked fallback (`"Your workplace admin has restricted app access. Ask them to approve Remyndrs, or connect a personal Outlook.com calendar instead."`) → redirect to error page.
3. Exchange code at `/common/oauth2/v2.0/token` (with `code_verifier`). Parse ID token claims for account email (no signature validation needed — it came over TLS direct from Microsoft; we use it for display only).
4. Upsert `calendar_connections` (encrypt tokens; reset `status='active'`, failures, delta state).
5. `sync_one_connection.delay(connection_id)` for immediate first sync.
6. Confirmation SMS: `"📅 Calendar connected! I'll text you 30 min before events. CALENDAR LEAD 15 changes timing, CALENDAR OFF disconnects."` (`message_type="calendar"`)
7. 302 → `{APP_BASE_URL}/calendar-connected`.

IP rate-limit both endpoints with the existing `check_ip_rate_limit()` (`main.py:326`).

---

## Sync engine (`tasks/calendar_tasks.py`)

Registration checklist (all three, or nothing runs): `@celery_app.task(bind=True, ...)` decorators; add `"tasks.calendar_tasks"` to `include=[...]` in `celery_app.py:24`; beat entries in `celery_config.py`. Default `celery` queue → runs on `sms-reminders-worker`; no render.yaml change.

**Beat entries:**
```python
"sync-ms365-calendars": {
    "task": "tasks.calendar_tasks.sync_all_calendars",
    "schedule": timedelta(minutes=5), "options": {"expires": 290},
},
"cleanup-oauth-states": {   # delete used/expired rows > 1 day old
    "task": "tasks.calendar_tasks.cleanup_oauth_states",
    "schedule": crontab(hour=5, minute=40),  # unused minute — hourly tasks own :02-:30
},
```

**`sync_all_calendars`** — kill-switch check, then fan out `sync_one_connection.delay(id)` per `status='active'` connection (mirrors `check_and_send_reminders` → `send_single_reminder` fan-out). Trivial load at current scale.

**`sync_one_connection(connection_id)`** (time_limit ~120s):
1. **Token refresh** if `token_expires_at` within 5 min: POST refresh_token grant. Microsoft rotates refresh tokens — **always persist the new one**. On `invalid_grant`: increment `consecutive_failures`; at 3, set `status='needs_reauth'` and SMS once (guard with `reauth_notified_at`): `"Your calendar connection expired. Text CONNECT CALENDAR to relink."`
2. **Fetch changes** — Graph `calendarView` delta:
   - Init: `GET /me/calendarView/delta?startDateTime={now}&endDateTime={now+7d}` with headers `Prefer: outlook.timezone="UTC"`, `Prefer: odata.maxpagesize=50`; follow `@odata.nextLink`; store final `@odata.deltaLink` + `delta_window_end`.
   - Incremental: GET stored `delta_link`. On **HTTP 410 Gone** → drop delta state, full re-init.
   - **The delta window is fixed at init** — when `delta_window_end < now + 2 days`, drop the delta link and re-init to slide the window forward.
   - `$select=id,subject,start,end,isAllDay,isCancelled,showAs,responseStatus,seriesMasterId,type,sensitivity` — `calendarView` returns recurring series pre-expanded into occurrences (this is why calendarView, not `/events`).
3. **Materialize** each returned event:
   - Skip: `@removed`/`isCancelled` (→ cancel below), `responseStatus.response == 'declined'`, `isAllDay` (MVP: no per-event ping; surfaces later via daily summary), start in the past.
   - `sensitivity == 'private'` → subject displayed as `"Private event"`.
   - Subject truncated to 80 chars. Reminder text: `📅 {subject} in {lead} minutes ({h:MM AM/PM})` — relative-first (timezone-proof for traveling users); the parenthetical local time (via `users.timezone`) keeps the message truthful when delivery is late or the user snoozes (a snoozed re-send of pure-relative text would claim "in 30 minutes" after the meeting started). Future refinement: compute the relative phrase at send time from `start_utc` instead of baking it in at sync time.
   - `reminder_date = start_utc − lead_time_minutes`; if that's already past but the event is still ahead, schedule for now+1 min (last-minute invites still warn).
   - **New event** → `save_reminder_with_local_time(phone, text, reminder_date_utc, local_time, tz)` (`models/reminder.py:884`), then `UPDATE reminders SET source='ms365_calendar'`, insert mapping row with `content_hash`.
   - **Changed** (`content_hash` differs): unsent → update reminder time/text; already sent → treat as new (re-remind at new time).
   - **Cancelled/removed** → delete unsent linked reminder, mapping `status='cancelled'`.
   - **Tombstone:** mapping has `reminder_id` but the reminder row is gone (user deleted it) → `status='dismissed'`, never recreate this occurrence.
4. Update `last_sync_at`, reset `consecutive_failures` on success. Non-auth HTTP failures: failures++, plain log; alert only if a connection stays failing > 24h (surfaces in monitoring pipeline).

**Delivery is untouched** — `send_single_reminder` picks calendar rows up like any reminder (random opener + snooze hint included; snooze works for free).

**SMS-cost guard:** cap calendar-reminder materialization at 8/user/day (skip beyond, log). A packed calendar must not turn into 30 texts — revisit with an agenda digest later.

---

## Testing (house style — no HTTP-mock lib; patch by name)

- **conftest gotcha:** `tasks/calendar_tasks.py` and `routes/calendar_routes.py` import `send_sms` → add both modules to the `sms_capture` patch chain (`conftest.py:302-311`) **and** the autouse `disable_twilio_globally` list (`conftest.py:609-617`), or tests attempt real sends.
- Patch `services.ms_graph` functions (`exchange_code`, `refresh_tokens`, `fetch_calendar_view`) by name; add a `graph_mock` fixture returning canned Graph payloads.
- `tests/test_calendar.py` coverage:
  - Keyword gating: free user → upgrade message; non-beta → beta message; premium+beta → link SMS'd; idempotent reconnect.
  - OAuth: state expiry/single-use/invalid; `access_denied` → admin-blocked SMS; happy path stores encrypted tokens (assert ciphertext ≠ plaintext).
  - Sync: create / update-unsent / update-sent / cancel / declined-skip / all-day-skip / recurring occurrences distinct / private-subject masking / past-event skip / last-minute event / daily cap.
  - Tombstone: user deletes reminder → next sync does not resurrect.
  - Refresh: rotation persisted; `invalid_grant` ×3 → `needs_reauth` + single notification SMS.
  - Timezone: `recalculate_pending_reminders_for_timezone` leaves `source='ms365_calendar'` rows alone.
  - `CALENDAR LEAD 15` reschedules pending unsent rows.

`requirements-prod.txt`: pin `requests>=2.31,<3` explicitly (already used undeclared by `services/alerts_service.py:14`; currently only transitive).

---

## Rollout

| Phase | Scope | Size |
|---|---|---|
| 0 | Entra app + Partner Center verification kickoff + env groups + privacy page | ~half day + verification wait (background) |
| 1 | Schema, config, tier gate, OAuth connect flow end-to-end (tokens stored, confirmation SMS) — dogfood connect with Brad's account | 1–2 days |
| 2 | Sync engine + materialization + lifecycle (update/cancel/tombstone/refresh) | 2–4 days (the real work) |
| 3 | Remaining keywords (STATUS, LEAD, OFF), needs_reauth UX, daily cap | 1 day |
| 4 | Tests, docs, `CALENDAR_BETA_PHONES=<Brad>` live beta; iterate on real calendar | 1 day + soak |
| 5 | Publisher verification lands → open beta to premium users (empty the allowlist), announce | — |

**Later (explicitly out of MVP):** Google Calendar provider (same tables, `provider='google'`; needs Google app verification), morning agenda folded into the existing daily summary (not a separate send pipeline), Graph change-notification webhooks instead of polling (subscriptions expire ~3 days, need renewal task), multi-calendar selection, ICS-feed fallback for blocked tenants.

## Open product decisions (defaults chosen, easy to change)
- Lead time default **30 min** (`CALENDAR LEAD n` to adjust; per-event overrides out of scope).
- All-day events: **no per-event ping** in MVP.
- Events the user organizes vs. attends: both remind (no distinction in MVP).
- `showAs=free` events still remind (only declined/cancelled are skipped).
