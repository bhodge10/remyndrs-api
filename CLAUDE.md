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
