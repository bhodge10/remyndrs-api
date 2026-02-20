# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

For detailed roadmaps, see:
- `docs/ux-roadmap.md` - SMS app UX improvement plan
- `docs/website-roadmap.md` - Website (remyndrs.com) improvement plan

## Project Overview

Remyndrs is an SMS-based AI memory and reminder service built with Python/FastAPI. Users interact entirely via SMS to store memories, create reminders, and manage to-do lists using natural language.

**Stack:** Python 3.11.9, FastAPI, PostgreSQL, Celery + Redis (Upstash), OpenAI GPT-4o-mini, Twilio SMS, Stripe billing

## Git Workflow

**Branching model:** Feature branches off `main`. No long-lived staging branch.

```bash
# Starting a session
git checkout main && git pull origin main

# Making changes
git checkout -b feature/short-description
# ... work and commit ...
git push -u origin feature/short-description
# Open PR -> merge to main -> delete branch
```

Never deploy directly to Render -- always push to git and let auto-deploy handle it.

## Common Commands

```bash
pip install -r requirements.txt          # Install dependencies
uvicorn main:app --reload                # Run FastAPI server
celery -A celery_app worker --loglevel=info  # Run Celery worker
celery -A celery_app beat --loglevel=info    # Run Celery Beat

python run_tests.py                      # Run all tests
python run_tests.py --quick              # Skip slow tests
python run_tests.py --onboarding         # Specific category
python run_tests.py --reminders
python run_tests.py --lists
python run_tests.py --memories
python run_tests.py --edge
python run_tests.py --tasks
python run_tests.py --scenarios
python run_tests.py --coverage           # With coverage

# Single test
pytest tests/test_reminders.py::TestReminderCreation::test_reminder_with_specific_time
```

## Architecture

### Request Flow
```
SMS -> Twilio webhook (/sms) -> main.py validates -> ai_service.py processes with OpenAI
  -> models/*.py persists to PostgreSQL -> sms_service.py sends confirmation
  -> Celery Beat (every 30s) checks due reminders -> sends at user's timezone
```

### Layered Structure
- **HTTP Layer:** `main.py` (routes), `admin_dashboard.py` (admin), `cs_portal.py` (support)
- **Route Handlers:** `routes/handlers/` - modular handlers for reminders, lists, memories, pending states
- **Business Logic:** `services/` - AI processing, payments, onboarding, metrics
- **Data Access:** `models/` - user, reminder, memory, list operations
- **Background Tasks:** `tasks/reminder_tasks.py` - Celery periodic jobs
- **Utils:** timezone conversions, encryption, input validation, `db_helpers.py`

### Key Files
| File | Purpose |
|------|---------|
| `main.py` | FastAPI routes, Twilio webhook handling |
| `services/ai_service.py` | OpenAI integration, prompt engineering |
| `models/reminder.py` | Reminder CRUD, recurring reminder logic |
| `models/user.py` | User management, encryption support |
| `tasks/reminder_tasks.py` | Celery tasks (reminder checking, daily summaries) |
| `config.py` | Environment variables, tier limits, constants |
| `database.py` | PostgreSQL connection pooling, schema init |
| `routes/handlers/` | Modular handlers for reminders, lists, memories |
| `utils/db_helpers.py` | Encryption-aware database query helpers |

### Database Tables
`users`, `reminders`, `recurring_reminders`, `memories`, `lists`, `list_items`, `interactions`, `support_tickets`, `broadcast_messages`, `conversation_flags`

## Deployment

Deployed on Render with four services:
1. **sms-reminders-api** - FastAPI web service
2. **sms-reminders-worker** - Celery worker
3. **sms-reminders-beat** - Celery Beat scheduler
4. **sms-reminders-monitoring** - Celery worker for monitoring pipeline

Config in `render.yaml`. Auto-deploys on push to main.

- Production dependencies: `requirements-prod.txt` (no test frameworks)
- Development dependencies: `requirements.txt` includes `-r requirements-prod.txt` + pytest
- If database is recreated, manually update `DATABASE_URL` in all 4 Render services

**CORS:** Use FastAPI's `CORSMiddleware`. Do NOT use manual `@app.options()` handlers.

**Website:** remyndrs.com hosted on Netlify. API calls go to `https://sms-reminders-api-1gmm.onrender.com`.

## Testing

Tests use `ConversationSimulator` to simulate SMS interactions. Key fixtures:
- `simulator` - simulates user SMS interactions
- `sms_capture` - captures outbound SMS for verification
- `ai_mock` - mocks AI responses for predictable testing
- `onboarded_user` - pre-created test user (auto-cleaned)
- `mock_datetime` - time mocking for reminder tests

Test phone number: `+15559876543`

Tests **never hit real Twilio or OpenAI APIs**:
- `conftest.py` has autouse fixtures that mock all SMS/AI calls
- `sms_service.py` detects test environment and blocks real Twilio calls
- Use `.env.test` with `ENVIRONMENT=test` and fake API keys

### AI Mock AM/PM Normalization Gotcha
`main.py` normalizes time strings before sending to AI (e.g., `10am` → `10:AM` at line ~2777). When writing tests with `ai_mock.set_response()`, register mock responses under **both** the original and normalized forms:
```python
ai_mock.set_response("remind me every monday at 10am about team meeting", response)
ai_mock.set_response("remind me every monday at 10:am about team meeting", response)
```
The mock uses exact-match on the lowercased message. If only the original form is registered, the normalized message won't match and will fall through to the default `unknown` action.

## Key Patterns

### Timezone Handling
All timestamps stored in UTC, converted to user timezone on display. Timezone determined during onboarding from ZIP code.

### Reminder Atomicity
Uses `SELECT FOR UPDATE SKIP LOCKED` for distributed reminder claiming. Stale tasks released every 5 minutes.

### Subscription Tiers
- **Free:** 2 reminders/day, 5 lists, 10 items/list, 5 memories
- **Premium:** Unlimited reminders, 20 lists, 30 items/list, recurring reminders
- **Family:** Premium features for 4-10 members

### Progressive Education for Tier Limits (Feb 2026)
The Education Pyramid helps free tier users understand limits without frustration:

**Level 1 (0-70%):** Silent - no counters, zero friction
**Level 2 (70-90%):** Gentle nudge - shows "(7 of 10 items)" for free tier only
**Level 3 (90-100%):** Clear warning - adds "Almost full!" or "Last one!"
**Level 4 (Over limit):** Blocked with WHY-WHAT-HOW structure

**Implementation:**
- **Progressive counters:** `add_list_item_counter_to_message()`, `add_memory_counter_to_message()`, `add_list_counter_to_message()` in `services/tier_service.py`
- **Level 4 formatters:** `format_list_item_limit_message()`, `format_memory_limit_message()`, `format_list_limit_message()` in `services/tier_service.py`
- **Handlers updated:** `routes/handlers/lists.py` (3 functions), `routes/handlers/pending_states.py` (1 function), `main.py` (4 locations)
- **Enhanced STATUS:** Free users see tier comparison showing Premium benefits

**Key rules:**
- Only FREE tier users see counters (premium/trial never see them)
- Percentage-based thresholds (70%, 90%)
- All limit messages follow WHY (tier limit) + WHAT (attempted action) + HOW (remove items OR upgrade)
- Trial hint for expired trial users: "Still on trial? Text STATUS"

**Testing:** `tests/test_tier_service_progressive.py` (13 unit tests), full integration suite passes. See `PROGRESSIVE_EDUCATION_IMPLEMENTATION.md` for complete details.

### Low-Confidence Reminder Confirmation
When AI confidence is below threshold, reminders enter pending confirmation stored in `pending_reminder_confirmation` on the user record.
- **Pending storage:** `routes/handlers/reminders.py` stores pending JSON
- **Confirmation handling:** `main.py` (search `pending_confirmation`) processes YES/NO and calls `save_reminder_with_local_time()`
- `save_reminder_with_local_time()` requires 5 args: `(phone_number, reminder_text, reminder_date_utc, local_time, timezone)` where `local_time` is HH:MM and `reminder_date` must be UTC
- **Also used for recurrence day clarification:** When weekly/monthly reminders are missing a day, `pending_reminder_confirmation` is set with `type: 'needs_recurrence_day'`. A dedicated handler in `main.py` (before the YES/NO handler) parses the day from the user's reply and saves the recurring reminder.

### AM/PM and Time-of-Day Recognition
The system recognizes AM/PM in three forms:
1. **Explicit:** `am`, `pm`, `a.m.`, `p.m.`
2. **Natural language:** `morning` (→ AM), `afternoon`/`evening`/`night` (→ PM)

This affects three code locations in `main.py`:
- `has_am_pm` check (~line 760): Determines if the message already specifies AM/PM
- `clarify_time` handler (~line 931): Maps time-of-day words to AM/PM when processing ambiguous times
- `is_valid_response` check (~line 2744): Recognizes time-of-day words as valid AM/PM clarification responses

### Keyword Handlers vs AI Processing
`main.py` has keyword-based handlers (exact string matches and regex patterns) that run **before** AI processing. Messages not caught by keywords fall through to OpenAI. When adding new user-facing commands:
- Add keyword matches for common phrasings (e.g., `SUMMARY OFF`, `DISABLE SUMMARY`, etc.)
- Consider natural language variations users might send
- Add safeguards in AI action handlers for misclassified intents (e.g., `update_reminder` handler redirects "daily summary" terms to the settings handler)

### Field Encryption
Optional AES-256-GCM encryption for PII (names, emails). Enabled via `ENCRYPTION_KEY` and `HASH_KEY` env vars.

## Environment Variables

**Required:** `OPENAI_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `DATABASE_URL`

**Optional:** `UPSTASH_REDIS_URL`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ENCRYPTION_KEY`, `HASH_KEY`, `STRIPE_*` keys, `SMTP_*` for email, `ANTHROPIC_API_KEY` (Agent 4 AI file identification)

## Rate Limiting

15 messages per 60-second window per user (configurable in `config.py`).

## Multi-Agent Monitoring System

Dashboard at `/admin/monitoring`. Four agents run on Celery schedules:

1. **Agent 1 - Interaction Monitor** (`agents/interaction_monitor.py`) - Detects anomalies: user confusion, parsing failures, error responses, context loss, flow violations
2. **Agent 2 - Issue Validator** (`agents/issue_validator.py`) - Validates issues, filters false positives, optional AI analysis
3. **Agent 3 - Resolution Tracker** (`agents/resolution_tracker.py`) - Health score (0-100), resolution tracking, weekly reports
4. **Agent 4 - Fix Planner** (`agents/fix_planner.py`) - Identifies affected files, generates Claude Code prompts for fixes

**Celery schedule:** Hourly critical check, every 4h Agent 1, every 6h Agent 2, daily 6AM UTC full pipeline, weekly Monday report.

**Manual triggers:** Dashboard button, `GET /admin/pipeline/run?hours=24`, `python -m agents.fix_planner --issue 123`

**Issue types:** `user_confusion`, `error_response`, `parsing_failure`, `timezone_issue`, `failed_action`, `action_not_found`, `confidence_rejection`, `repeated_attempts`, `delivery_failure`, `context_loss`, `flow_violation`

**DB tables:** `monitoring_issues`, `monitoring_runs`, `issue_patterns`, `issue_pattern_links`, `validation_runs`, `issue_resolutions`, `pattern_resolutions`, `health_snapshots`, `fix_proposals`, `fix_proposal_runs`

## Recent Improvements & Bug Fixes

### Progressive Education for Tier Limits (Feb 2026)
Complete overhaul of tier limit messaging to improve free-to-premium conversion. Replaced confusing messages like "Added 0 items... (7 items skipped - list full)" with clear WHY-WHAT-HOW explanations. Implemented Education Pyramid (Levels 1-4) with progressive counters, warnings, and educational blocking. Enhanced STATUS command to show tier comparison. All functionality tested with 13 new unit tests. See `PROGRESSIVE_EDUCATION_IMPLEMENTATION.md` for details.

### Daily Summary Interactive Flow Removed (Feb 2026)
The `daily_summary_setup` interactive flow trapped users in an inescapable loop — commands like "Upgrade", "Premium", and natural language got caught with "I didn't understand that time..." responses. Replaced with a one-shot tip shown after the user's first reminder: "Text SUMMARY ON to enable it!" The existing keyword handlers (`SUMMARY ON`, `SUMMARY OFF`, `SUMMARY TIME 7AM`) already cover full functionality. Deleted ~380 lines from `services/first_action_service.py` (interactive handler, time validation, welcome builder, delayed SMS). Daily summary tip only triggers on reminder actions, not on memory/list actions.

### Context Loss Fix (Feb 2026)
List selection by number (e.g., "1") was intercepted by daily summary handler asking "1 AM or 1 PM?". Fixed by removing the interactive daily summary flow entirely (see above). New monitoring detectors (`context_loss`, `flow_violation`) prevent similar issues.

### Broadcast System Improvements (Feb 2026)
Three fixes to the admin broadcast system (`admin_dashboard.py`):

1. **Single Number Test Mode:** New "Single Number (Test)" audience option sends to one phone number instead of all users. Bypasses time window check. Works for both immediate and scheduled broadcasts. Phone input validates US numbers and normalizes to E.164.

2. **Scheduled Broadcast Reliability:** The daemon thread (`check_scheduled_broadcasts`) had two issues: (a) a single DB exception could crash the thread silently, and (b) completed scheduled broadcasts only updated `scheduled_broadcasts` table but never inserted into `broadcast_logs`, making them invisible in Broadcast History. Fixed with inner try/except around DB connections and auto-insertion into `broadcast_logs` with `source='scheduled'` on completion.

3. **Message Viewer:** Broadcast History messages (previously truncated to 100 chars with no expansion) are now clickable to open a modal showing the full text with a Copy button. History also shows "Immediate" vs "Scheduled" source indicator.

**DB changes:** Added `target_phone TEXT` to `scheduled_broadcasts`, `source TEXT DEFAULT 'immediate'` to `broadcast_logs` (with ALTER TABLE migrations).

### Delete Account Duplicate Message Fix (Feb 2026)
When a premium user texted YES DELETE ACCOUNT, they received two conflicting messages: the correct "Your account has been deleted..." and a second "You've been moved to the free plan..." from the Stripe cancellation webhook. Fixed by adding a check in `handle_subscription_cancelled()` (`services/stripe_service.py`) that skips the downgrade and cancellation notice when the user has `pending_delete_account` or `opted_out` set, since the delete flow in `main.py` already sends its own confirmation.

### List Creation With Items Fix (Feb 2026)
When a user texted "Create a grocery list" with items on separate lines in the same message, the system only created the list and silently discarded all items. Root cause: the AI prompt's `create_list` action has no `items` field. Fix: updated the AI prompt (`services/ai_service.py` ~line 461) to instruct using `add_to_list` instead of `create_list` when items are present, since `add_to_list` already auto-creates non-existent lists AND adds items in one step. Added explicit example for this pattern.

### Custom Interval Recurring Reminder Rejection (Feb 2026)
"Every N days/weeks/months" patterns (e.g., "every 30 days", "every 2 weeks") were not recognized as recurring reminders. The AI silently fell back to creating a wrong one-time reminder with today's date. Updated the unsupported intervals section in the AI prompt (`services/ai_service.py` ~line 402) to explicitly catch custom day/week/month intervals and return a helpful message suggesting supported alternatives (daily, weekly, weekdays, weekends, monthly). Supported recurrence types remain: `daily`, `weekly`, `weekdays`, `weekends`, `monthly`.

### Recurring Reminder Day Context Fix (Feb 2026)
When a user created a monthly or weekly recurring reminder without specifying the day (e.g., "Remind me every month to change my cpap filter"), the system asked "which day?" but didn't store any pending state. The user's reply (e.g., "Every 1st at 9:30pm") was treated as a brand new message and misclassified by AI. Fix: store a `needs_recurrence_day` pending confirmation in `main.py` before asking the clarification question. Added a handler that parses the user's response to extract day (number 1-31 for monthly, day name for weekly) with optional time override, then saves the recurring reminder. Excluded `needs_recurrence_day` from the low-confidence YES/NO handler to prevent misclassification. Existing undo handler already covers cancellation.

### Multi-Day Reminder Error Fix (Feb 2026)
"Remind me every day for the next five days at 5 PM..." caused "Sorry, I'm having trouble right now" error. Two root causes: (1) conflicting AI prompt instructions — the PRIORITY CHECK classified "every day" as `reminder_recurring`, while the MULTI-DAY REMINDERS section said "for the next X days" should create multiple separate `reminder` actions; (2) `OPENAI_MAX_TOKENS=300` was too low for 5 reminder JSON objects (~400 tokens needed), causing truncated JSON that failed parsing on all retries. Fix: added exception to PRIORITY CHECK so finite periods ("for the next X days") skip `reminder_recurring` and create multiple separate reminders. Increased `OPENAI_MAX_TOKENS` from 300 to 800 in `config.py`. Added `finish_reason=length` truncation detection in `services/ai_service.py` that retries and falls back to a helpful "break it into smaller parts" message. Tests: `TestMultiDayReminders` class in `tests/test_reminders.py` with 2 test cases.

### Clarify Time O'Clock and Date Context Fix (Feb 2026)
"Remind me Sunday at 1 o'clock to look at Robyn's registry" → user replies "Pm" → crashed with "Sorry, I had trouble setting that reminder." Two bugs: (1) AI stored `time_mentioned: "1 o'clock"` and the parser at `main.py:1407` did `int("1 O'CLOCK")` which threw `ValueError`; (2) the `clarify_time` handler stored `pending_reminder_text` and `pending_reminder_time` but NOT the date, so "Sunday" was lost. Fix: added `re.sub(r"O'?CLOCK", ...)` to strip o'clock from the time string before parsing. Added optional `reminder_date` field to the `clarify_time` JSON format in the AI prompt (`services/ai_service.py`). The clarify_time handler now stores `pending_reminder_date` from the AI response, and the AM/PM handler reads it back via `get_pending_reminder_date()` to use the specific date instead of today/tomorrow logic. Clears `pending_reminder_date` on completion. Tests: `TestClarifyTimeOclock` class in `tests/test_reminders.py` with 2 test cases.

### Desktop Signup Flow (Feb 2026)
Added `POST /api/signup` endpoint for desktop visitors. Phone validation, E.164 formatting, sends welcome SMS. Frontend form on remyndrs.com with responsive design. Uses CORSMiddleware.

### Memory Upsert — Duplicate Detection (Feb 2026)
`save_memory()` in `models/memory.py` now detects duplicate memories before inserting. Uses Jaccard keyword similarity (stop words filtered, 60% threshold) to find existing memories with high overlap. If a match is found, the existing memory is updated in place (text, parsed_data, and created_at refreshed) instead of creating a duplicate row. `save_memory()` returns `bool` — `True` if updated, `False` if new insert. Both call sites (`main.py` ~line 3575, `routes/handlers/memories.py` ~line 40) show "Updated: ..." vs "Got it! Saved: ..." accordingly.

**Key details:**
- `_extract_keywords()` strips stop words and short tokens, returns set of meaningful words
- `_memory_similarity()` computes Jaccard similarity (intersection / union of keyword sets)
- `_find_similar_memory()` scans all user memories, returns ID of best match above threshold
- Threshold `_SIMILARITY_THRESHOLD = 0.6` for standard memories, `_SHORT_MEMORY_THRESHOLD = 0.4` for short memories (≤4 keywords)
- Short-memory threshold catches key-value updates like "WiFi is ABC" → "WiFi is XYZ" where Jaccard drops disproportionately (2/4 = 0.5 < 0.6)
- Encryption-aware: uses `phone_hash` lookup with plaintext fallback, same as other memory functions

### Pricing Update — $8.99/mo, $89.99/yr (Feb 2026)
Premium pricing changed from $6.99/mo ($76.89/yr) to $8.99/mo ($89.99/yr, ~17% discount, save $18). Constants `PREMIUM_MONTHLY_PRICE` and `PREMIUM_ANNUAL_PRICE` in `config.py` are used everywhere — no hardcoded prices remain. Stripe cents values updated in `PRICING` dict (899/8999). Annual pricing exposed in UPGRADE flow (both monthly + annual Stripe checkout links). All limit-hit messages include inline pricing (e.g., "Text UPGRADE for unlimited reminders ($8.99/mo)"). Trial expiration messages list specific free-tier limits. Updated in: `config.py`, `main.py`, `cs_portal.py`, `services/tier_service.py`, `tasks/reminder_tasks.py`, website `index.html`.

### Expanded HELP Command (Feb 2026)
`get_help_text()` in `utils/formatting.py` expanded from 5 lines to a comprehensive guide covering reminders, lists, memories, management commands (MY REMINDERS, MEMORIES, MY LISTS, SNOOZE, UNDO), and account commands (STATUS, UPGRADE, SUMMARY ON/OFF). Points to remyndrs.com/commands for full guide.

### Snooze Duration in Fire Messages (Feb 2026)
Reminder fire messages now say "(Reply SNOOZE to snooze 15 min)" instead of just "(Reply SNOOZE to snooze)" to set expectations on the default duration. Changed in `tasks/reminder_tasks.py`.

### Annual Pricing in UPGRADE Flow (Feb 2026)
UPGRADE command now generates two Stripe checkout links — monthly and annual. Uses `create_checkout_session(phone_number, 'premium', 'annual')` which already supported the `annual` billing cycle. Message format: monthly link first, then "Save $18/yr with annual ($89.99/yr):" with the annual link. Both links expire in 24 hours. Changed in `main.py` UPGRADE handler.

### MY REMINDERS Fallback in AI Handlers (Feb 2026)
When AI `delete_reminder` or `update_reminder` handlers can't find a matching reminder, the error message now includes "Text MY REMINDERS to see your list" so users can find the correct reminder to reference. Changed in `main.py` at the delete and update handler no-match fallbacks.

### Personalized Trial Warnings (Feb 2026)
Day 7 and Day 1 trial expiration warnings now include the user's actual usage stats — reminder count, list count, and memory count — to reinforce value before expiration. Example: "So far you've used: 12 reminders, 3 lists, 5 memories." Queries `reminders` table directly and uses `get_list_count()` and `get_memory_count()` from models/services. Changed in `tasks/reminder_tasks.py` `check_trial_expirations`.

### 30-Day Post-Trial Win-Back (Feb 2026)
New `send_30d_winback` Celery task sends a re-engagement SMS 30 days after trial expiry. Targets users who are on the free plan, haven't upgraded, and haven't opted out. Message includes both monthly and annual pricing. Tracks with `winback_30d_sent` column on users table (added in `database.py`). Runs daily at 12 PM UTC via Celery Beat (`celery_config.py`). Uses a 1-day window (`trial_end_date` between 30-31 days ago) to avoid missing users. Follows same pattern as `send_post_trial_reengagement` (Day 3).

### MORE COMMANDS Keyword (Feb 2026)
Added `MORE COMMANDS` (also `MORE`, `ALL COMMANDS`, `FULL COMMANDS`) keyword handler in `main.py` for power users wanting the full command list. Returns `get_extended_help_text()` from `utils/formatting.py` which lists recurring reminder commands, past reminders, data/account management, and support commands. The base HELP text now ends with "Text MORE COMMANDS for the full list" to guide discovery.

### Trial Expiration Recurring Reminder Fate (Feb 2026)
Trial expiration (Day 0) message now includes "Existing recurring reminders keep working, but you can't create new ones" so users understand their recurring reminders aren't deleted. Changed in `tasks/reminder_tasks.py` `check_trial_expirations` Day 0 message.

### Effective Monthly Rate in Annual Pricing (Feb 2026)
All annual pricing references changed from "(save $18)" to "($7.50/mo)" to show the effective monthly rate, which is more meaningful to users comparing plans. Updated in `tasks/reminder_tasks.py` (trial warnings at 7d, 1d, 0d, 30d win-back) and `main.py` (UPGRADE handler).

### Rate Limit Cooldown Duration (Feb 2026)
Rate limit message changed from "wait a moment" to "wait about 30 seconds" so users know exactly how long to wait. The actual window is 60 seconds (`RATE_LIMIT_WINDOW` in `config.py`), but "30 seconds" accounts for partial window elapsed. Changed in `main.py` rate limit handler.

### Snooze 24-Hour Cap Communication (Feb 2026)
When a user requests a snooze longer than 24 hours and it gets silently capped, the confirmation now includes "(max snooze is 24 hours)" to explain why the duration differs. Uses regex to detect when the raw user input exceeds 1440 minutes (24h) by comparing against the capped `snooze_minutes` value. Changed in `main.py` snooze handler.

### Day 7 Trial Double-Message Deduplication (Feb 2026)
Users were receiving two messages on Day 7: the trial warning from `check_trial_expirations` (9 AM UTC) and the mid-trial value reminder from `send_mid_trial_value_reminders` (10 AM UTC). Fixed by merging both into the 7d trial warning — now includes personalized greeting, checkmark-format usage stats with recurring reminder count, and marks `mid_trial_reminder_sent=True`. The mid-trial value task additionally filters out users where `trial_warning_7d_sent` is already True. Changed in `tasks/reminder_tasks.py`.

### Multi-Line List Item Adds (Feb 2026)
`parse_list_items()` in `services/ai_service.py` now handles newline-separated items. If the input contains `\n` with multiple lines, each line is split and recursively parsed for commas/and. This allows users to add items one-per-line in a single SMS message. The newline check runs before the existing comma and "and" parsing logic.

### Expanded UNDO for All Actions (Feb 2026)
UNDO handler in `main.py` now checks the most recent action across reminders, list items, and memories (comparing `created_at` timestamps) and offers to undo whichever was most recent. Previously only checked reminders. New model functions: `get_most_recent_list_item()` and `delete_list_item_by_id()` in `models/list_model.py`, `get_most_recent_memory()` in `models/memory.py`. `get_most_recent_reminder()` in `models/reminder.py` now returns 4-tuple `(id, text, date, created_at)` instead of 3-tuple. The pending delete confirmation handler also gained a `memory` type handler alongside existing `reminder`, `recurring`, and `list_item` types.

### Smarter AI Recurrence Fallback (Feb 2026)
When users request unsupported recurrence intervals (e.g., "every 2 weeks", "every 30 days"), the AI prompt now instructs suggesting the closest supported alternative instead of a generic error. Examples: "every 2 weeks" → suggests weekly, "every 30 days" → suggests monthly, "every 2 hours" → suggests daily. Changed in `services/ai_service.py` AI prompt NOT SUPPORTED section.

### 14-Day Post-Trial Touchpoint (Feb 2026)
New `send_14d_post_trial_touchpoint` Celery task sends a personalized message 14 days after trial expiry, highlighting specific features the user is missing — paused recurring reminders, list/memory counts exceeding free tier limits. Fills the gap between existing Day 3 re-engagement and Day 30 win-back. Tracks with `post_trial_14d_sent` column on users table. Runs daily at 11:45 AM UTC via Celery Beat. Changed in `tasks/reminder_tasks.py`, `database.py`, `celery_config.py`.

### Trial Lifecycle Timeline
The complete post-onboarding trial lifecycle message schedule (all timezone-aware, sends at 9-10 AM user's local time):
- **Day 3:** Engagement nudge (`send_day_3_engagement_nudges`, hourly at :10)
- **Day 7:** Combined trial warning + value reminder (`check_trial_expirations`, hourly at :00) — personalized stats, upgrade CTA
- **Day 13 (1d left):** Urgent trial warning (`check_trial_expirations`, hourly at :00)
- **Day 14 (expired):** Downgrade notice (`check_trial_expirations`, hourly at :00)
- **Day 17 (3d post):** Re-engagement (`send_post_trial_reengagement`, hourly at :15)
- **Day 28 (14d post):** Feature-loss touchpoint (`send_14d_post_trial_touchpoint`, hourly at :20)
- **Day 44 (30d post):** Win-back (`send_30d_winback`, hourly at :25)

### Round 4 Audit Fixes — Trial, Stripe, and Data Safety (Feb 2026)
Narrow-scope audit focused on three questions: (1) Do trial messages fire correctly? (2) Does UPGRADE flow work end-to-end? (3) Any critical data-loss/double-billing/cancellation bugs?

**Phase 1 (Critical — PR #132):**
- **Trial flag whitelist:** Added 8 trial lifecycle flags (`trial_warning_7d_sent`, `trial_warning_1d_sent`, `trial_warning_0d_sent`, `mid_trial_reminder_sent`, `day_3_nudge_sent`, `post_trial_reengagement_sent`, `post_trial_14d_sent`, `winback_30d_sent`) to `ALLOWED_USER_FIELDS` in `models/user.py`. Without these, `create_or_update_user()` silently dropped the flags, causing every trial message to repeat daily.
- **Day 0 premium_status reset:** Added `create_or_update_user(phone_number, premium_status='free')` in the Day 0 handler of `check_trial_expirations` so post-trial tasks (Day 3, Day 14, Day 30) can find expired users.
- **TCPA opted_out filter:** Added `(opted_out IS NULL OR opted_out = FALSE)` to `check_trial_expirations` and `send_mid_trial_value_reminders` SQL queries to honor SMS opt-out.
- **Premium subscriber filter:** Added `(stripe_subscription_id IS NULL OR subscription_status != 'active')` to `check_trial_expirations` so paying subscribers don't receive "your trial is expiring" messages.
- **Double-billing prevention:** Added `stripe.Subscription.list(customer=id, status='active')` check in `create_checkout_session()` (`services/stripe_service.py`) before creating a new checkout, returning an error if the user already has an active subscription.

**Phase 2 (High/Medium — PR #133):**
- **CANCEL keyword handler:** Added `CANCEL SUBSCRIPTION` / `CANCEL PLAN` / `CANCEL PREMIUM` keyword handler in `main.py` that redirects users to `ACCOUNT` for the Stripe billing portal.
- **Connection pool safety:** Added `conn.rollback()` in `return_db_connection()` (`database.py`) before returning connections to the pool, preventing cascading failures from aborted transactions leaving connections in an error state.
- **Trial day-range checks:** Replaced all 7 exact-day comparisons in trial lifecycle tasks (`tasks/reminder_tasks.py`) with range checks to handle signup time-of-day edge cases where `timedelta.days` floor causes missed windows:
  - Day 7: `== 7` → `6 <= days_remaining <= 7`
  - Day 1: `== 1` → `0 < days_remaining <= 1`
  - Mid-trial Day 7: `!= 7` → `not (6 <= days_remaining <= 7)`
  - Day 3 nudge: `!= 3` → `not (2 <= days_in_trial <= 3)`
  - Post-trial Day 3: `!= 3` → `not (2 <= days_since_expiry <= 3)`
  - 14-day touchpoint: `!= 14` → `not (13 <= days_since_expiry <= 14)`

### Mobile Optimization Pass — Website (Feb 2026)
Comprehensive mobile optimization across all 5 website HTML files (`index.html`, `faq.html`, `commands.html`, `privacy.html`, `terms.html`). Changes made in the Remyndrs-Website repo:

- **Hamburger tap target:** Enlarged from `padding: 4px 8px` (~24px) to `padding: 10px 12px` with `min-width: 44px; min-height: 44px` to meet WCAG/Apple/Google 44x44px minimum (all 5 files).
- **Conversation popup width:** Changed from fixed `width: 320px` to `max-width: 320px; width: calc(100% - 20px)` so popups shrink on 320px-wide phones (`index.html`).
- **Footer contact buttons:** Enlarged from `padding: 10px 18px; font-size: 13px` to `padding: 12px 20px; font-size: 14px` for ~40px tap height (`index.html`, `faq.html`, `commands.html`).
- **Inline contact buttons:** Enlarged from `padding: 12px 20px` to `padding: 14px 22px` for ~44px tap height (`index.html`).
- **SMS disclaimer readability:** Added `font-size: 13px` in `@media (max-width: 768px)` block, up from 12px base (`index.html`).
- **Animation performance:** Added `will-change: transform, opacity` on `.conversation-message` and `will-change: transform` on `.pricing-card.featured` for GPU compositing on budget phones (`index.html`).
- **Nav link tap targets:** Changed mobile nav from `padding: 10px 0` to `padding: 12px 20px` for full-width comfortable taps (all 5 files).
- **Hamburger accessibility:** Added `aria-controls="main-nav"` to hamburger button and `id="main-nav"` to `<nav>` element (`index.html`).

### Demo Video — Website (Feb 2026)
Added a 30-second animated demo video to `index.html` showing the SMS flow: user texts a reminder, gets confirmation, and receives the reminder at the right time. Embedded in the Remyndrs-Website repo.

### Website Roadmap Phase 3 Complete (Feb 2026)
All Phase 3 (Feature Communication) items in `docs/website-roadmap.md` are now done: feature benefits rewrite (#8), demo video (#9), and mobile optimization pass (#10). Phase header updated to COMPLETED.

### Timezone-Aware Trial Lifecycle Messages (Feb 2026)
Beta user reported receiving "Premium Trial has expired" SMS every other morning at 4:00 AM EST. Three root causes found and fixed:

**Bug 1 — NULL flag columns caused repeated sends:** `ALTER TABLE ADD COLUMN DEFAULT FALSE` doesn't backfill existing rows. Existing users had NULL flags, and `not None = True` in Python bypassed the "already sent" check. Fix: `COALESCE(flag, FALSE)` in all SQL queries + NULL→FALSE backfill migration in `database.py` for all 8 trial lifecycle flags.

**Bug 2 — Silent error swallowing:** `create_or_update_user()` in `models/user.py` catches exceptions without re-raising, so flag updates silently failed. Fix: replaced all `create_or_update_user()` calls in trial tasks with direct `c.execute("UPDATE users SET flag = TRUE WHERE phone_number = %s")` using the existing connection/cursor.

**Bug 3 — Fixed UTC schedule ignored user timezones:** Tasks ran at 9 AM UTC (4 AM EST, 1 AM PST). Fix: all 6 trial lifecycle tasks changed from daily fixed-hour crontab to hourly execution with per-user timezone checks — only sends when it's 9-10 AM in the user's local timezone. Falls back to `America/New_York` for NULL/invalid timezone values.

**Schedule changes in `celery_config.py`:** Tasks staggered at :00, :05, :10, :15, :20, :25 past each hour to avoid thundering herd:
- `check_trial_expirations` — `crontab(minute=0)`
- `send_mid_trial_value_reminders` — `crontab(minute=5)`
- `send_day_3_engagement_nudges` — `crontab(minute=10)`
- `send_post_trial_reengagement` — `crontab(minute=15)`
- `send_14d_post_trial_touchpoint` — `crontab(minute=20)`
- `send_30d_winback` — `crontab(minute=25)`

**Files changed:** `celery_config.py`, `database.py`, `tasks/reminder_tasks.py`

### Round 5 Audit — Critical Fixes (Feb 2026)
Comprehensive audit of the full codebase identified 6 critical issues, all fixed in PR #146:

**C1 — Duplicate SMS race condition:** `send_single_reminder` released the `FOR UPDATE` lock via `conn.commit()` before marking `sent=TRUE` on a separate connection. If the second connection failed, the lock expired and the reminder was re-sent. Fixed by keeping the lock and marking sent in a single atomic commit. Added a fresh-connection fallback with `CRITICAL` log level as last resort (SMS already sent, must not retry).

**C2 — Trial SMS + flag atomicity:** Trial lifecycle tasks committed DB flag changes (e.g., `trial_warning_7d_sent`) BEFORE sending SMS. If SMS failed, the flag was set but the user never received the message (silent skip). Fixed by moving all state changes (flags, Day 0 `premium_status='free'` downgrade, Day 7 `mid_trial_reminder_sent`) to AFTER SMS send, committed atomically. Converted 5 trial tasks from `create_or_update_user()` (which opens its own connection and silently swallows errors) to direct SQL with existing cursor + `conn.rollback()` in exception handlers.

**C3 — Monitoring connection pool poisoning:** `return_monitoring_connection()` returned connections without rolling back aborted transactions. An error in one monitoring task could leave the connection in a broken state, causing cascading failures. Fixed by adding `conn.rollback()` before `putconn()` (same pattern already existed for the main pool).

**C4 — Missing Celery task timeouts:** 22 Celery tasks across `reminder_tasks.py` and `monitoring_tasks.py` lacked `time_limit`/`soft_time_limit`, meaning a hung task could block a worker indefinitely. Added appropriate timeouts to all tasks (60s–3600s depending on expected duration).

**C5 — PII exposure in admin API:** `/admin/stats` endpoint returned full phone numbers in the `top_users` response. Fixed by masking to `***-***-1234` format.

**C6 — SQL injection in cost analytics:** `get_cost_analytics()` in `metrics_service.py` used f-string interpolation for `INTERVAL '{interval}'`. While `interval` came from a hardcoded dict (not user input), this violated parameterized query best practices. Fixed with `%s::interval` parameterized casting.

**Bonus:** Fixed bare `except:` to `except Exception:` in `reminder_tasks.py` outer exception handler.

**Files changed:** `database.py`, `main.py`, `services/metrics_service.py`, `tasks/monitoring_tasks.py`, `tasks/reminder_tasks.py`

### Round 5 Audit — High-Priority Fixes (Feb 2026)
11 high-priority issues fixed in PR #148:

- **H1 — Bare except clauses:** Replaced 6 remaining bare `except:` with specific types (`ValueError`, `TypeError`, `Exception`) across `main.py`, `admin_dashboard.py`, `services/reminder_service.py`.
- **H2 — XSS in admin changelog:** Added `html.escape()` on `title` and `description` in the public updates page (`admin_dashboard.py`).
- **H3 — Secrets printed to stdout:** `generate_keys()` in `utils/encryption.py` no longer prints keys; returns them silently.
- **H4 — Silent decryption failures:** `safe_decrypt()` now logs `logger.warning` with exception type on failure instead of silently returning fallback.
- **H5 — Silent migration failures:** `database.py` `init_db()` now distinguishes expected "already exists" errors from unexpected ones, logging unexpected errors.
- **H6 — Broadcast checker crash recovery:** Added consecutive failure tracking (stops after 10), exponential backoff (60s→5min) in `check_scheduled_broadcasts()`.
- **H7 — Daily summary SMS overflow:** Truncate daily summary to 1500 chars with "Text MY REMINDERS for full list" fallback.
- **H8 — Outbound SMS length validation:** `send_sms()` truncates messages exceeding Twilio's 1600-char limit with warning log.
- **H9 — Monitoring window overlap:** Changed interaction monitor from 24h to 4h analysis window (matches 4h schedule in `celery_config.py`).
- **H10 — Stale claims timeout:** Increased `release_stale_claims` from 5 to 15 minutes to prevent premature release of actively-processing reminders.
- **H11 — SQL identifier quoting:** Dynamic table/column names in DELETE queries now use `psycopg2.sql.Identifier()` instead of f-strings in `admin_dashboard.py` and `main.py`.

**Files changed:** `admin_dashboard.py`, `celery_config.py`, `database.py`, `main.py`, `services/reminder_service.py`, `services/sms_service.py`, `tasks/reminder_tasks.py`, `utils/encryption.py`

### Round 5 Audit — Medium-Priority Fixes (Feb 2026)
10 medium-priority issues fixed in PR #150:

- **M1 — Hardcoded trial days:** Replaced hardcoded `14` with `FREE_TRIAL_DAYS` constant in Day 3 nudge calculation (`tasks/reminder_tasks.py`).
- **M2/M9 — Session ID validation:** Added regex validation (`cs_[a-zA-Z0-9_]+`) on `/payment/success` and `/api/payment-info` to reject malformed Stripe session IDs. URL-encodes session_id on redirect.
- **M3 — TCPA opt-out keywords:** Added STOPALL, UNSUBSCRIBE, END, QUIT handling alongside STOP to ensure `opted_out` flag stays in sync.
- **M4/M10 — PII in admin delete:** Masked phone numbers in admin user deletion log and API response (`***-***-1234` format).
- **M5 — Public endpoint rate limiting:** Added IP-based rate limiting (5 requests per 5 minutes) on `/api/signup` and `/api/contact` to prevent SMS spam.
- **M6 — Auth failure rate limiting:** Added brute force protection (5 failures per 5 minutes lockout) on admin `verify_admin()` with `AUTH_LOCKOUT` security event.
- **M7 — CS portal audit logging:** Added logging for which credential type (CS vs admin) was used and failed attempts in `cs_portal.py`.
- **M8 — Webhook idempotency:** Added MessageSid deduplication in SMS webhook to prevent duplicate message processing on Twilio retries. Uses in-memory dict with periodic cleanup.

**Files changed:** `main.py`, `admin_dashboard.py`, `cs_portal.py`, `tasks/reminder_tasks.py`

### Round 5 Audit — Low-Priority Fixes (Feb 2026)
4 low-priority issues fixed in PR #152:

- **L1 — Admin error message sanitization:** Replaced 48 instances of `detail=str(e)` with `detail="Internal server error"` in `admin_dashboard.py`. All errors were already logged before the raise, so no diagnostic info is lost — this just prevents leaking stack traces to the admin UI.
- **L2 — BETA_MODE default:** Changed default from `"true"` to `"false"` in `config.py`. Production should require explicit opt-in via `BETA_MODE=true` env var rather than defaulting to beta behavior.
- **L3 — Dynamic SQL field whitelist:** Added whitelist validation (`VALID_TRIAL_FIELDS` set) for the `update_field` variable in `check_trial_expirations` (`tasks/reminder_tasks.py`). Replaced f-string SQL with `psycopg2.sql.Identifier()` for parameterized column names.
- **L4 — Contact form sanitization:** Applied `sanitize_text()` to the message body in `/api/contact` endpoint (`main.py`) to strip control characters from web form submissions.

**Files changed:** `admin_dashboard.py`, `config.py`, `main.py`, `tasks/reminder_tasks.py`

### Cancel Not Working During NEEDS_TIME Pending State (Feb 2026)
Two bugs fixed in PR #154:

1. **"Cancel" intercepted by UNDO handler:** When a user texted "cancel" during the `NEEDS_TIME` flow (system asking "what time?"), the UNDO handler (line 912) intercepted it because "cancel" is in `is_undo_command`. The `has_pending_time_clarify` guard at line 1212 specifically excluded `NEEDS_TIME` (`!= "NEEDS_TIME"`), so the UNDO handler ran and offered to delete the most recent action instead of cancelling the pending time. Fixed by removing the `!= "NEEDS_TIME"` exclusion so the UNDO handler defers to the vague time handler's cancel logic for ALL pending time states.

2. **New intent blocked by NEEDS_TIME:** When a user sent a clear new-intent message (e.g., "Remember the show...") during `NEEDS_TIME`, the handler blocked with "I still need a time..." instead of context-switching. Added `is_new_intent` detection for keywords (`remember`, `add to`, `my lists`, `help`, `status`, `upgrade`, `memories`, `summary`) that auto-cancels the pending time state and lets the new intent fall through to normal processing.

**Files changed:** `main.py`

### Test Suite Fixes — 3 Failing Tests (Feb 2026)
Three long-standing test failures fixed in PR #156. Suite now at 112 passed, 0 failed.

1. **`test_due_reminder_gets_sent`:** `check_and_send_reminders()` dispatches via `send_single_reminder.delay()`, but no Celery broker was configured for tests. Tasks silently failed to execute. Fixed by adding `task_always_eager=True` and `task_eager_propagates=True` to Celery config in `conftest.py`. Also fixed mock target from `services.sms_service.send_sms` to `tasks.reminder_tasks.send_sms` (must patch where the function is used, not where it's defined).

2. **`test_every_day_for_next_five_days` / `test_multi_day_reminder_not_classified`:** The `onboarded_user` fixture creates a free-tier user (2 reminders/day limit). The `multiple` action handler processed 5 or 3 sub-actions but `can_create_reminder()` blocked after the 2nd. Fixed by setting test users to an active trial (`premium_status='trial'`, `trial_end_date = NOW() + 14 days`).

3. **`test_sent_reminder_not_resent`:** `save_reminder()` returns `None` (not the inserted ID). The test did `UPDATE SET sent=TRUE WHERE id=NULL` — matching no rows. The reminder stayed `sent=FALSE` and was picked up by `claim_due_reminders`. Fixed by matching on `phone_number + reminder_text` instead of ID.

**Files changed:** `tests/conftest.py`, `tests/test_background_tasks.py`, `tests/test_reminders.py`
