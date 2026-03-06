# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

For detailed docs, see:
- `docs/changelog.md` - Feature history and bug fix details
- `docs/security-audits.md` - Round 4, 5, 6 audit findings and fixes
- `docs/monitoring.md` - Multi-agent monitoring system
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
`users`, `reminders`, `recurring_reminders`, `memories`, `lists`, `list_items`, `interactions`, `support_tickets`, `contact_messages`, `broadcast_messages`, `conversation_flags`, `smart_nudges`, `monitoring_issues`, `monitoring_runs`, `issue_patterns`, `issue_pattern_links`, `validation_runs`, `issue_resolutions`, `pattern_resolutions`, `health_snapshots`, `fix_proposals`, `fix_proposal_runs`

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

## Key Patterns

### Timezone Handling
All timestamps stored in UTC, converted to user timezone on display. Timezone determined during onboarding from ZIP code.

### Reminder Atomicity
Uses `SELECT FOR UPDATE SKIP LOCKED` for distributed reminder claiming. Stale tasks released every 15 minutes.

### Subscription Tiers
- **Free:** 2 reminders/day, 5 lists, 10 items/list, 5 memories
- **Premium ($8.99/mo, $89.99/yr):** Unlimited reminders, 20 lists, 30 items/list, recurring reminders
- **Family:** Premium features for 4-10 members

### Progressive Education for Tier Limits
Education Pyramid (Levels 1-4) for free tier users. Implementation in `services/tier_service.py`. See `docs/changelog.md` for details.

### Low-Confidence Reminder Confirmation
When AI confidence is below threshold, reminders enter pending confirmation stored in `pending_reminder_confirmation` on the user record.
- `save_reminder_with_local_time()` requires 5 args: `(phone_number, reminder_text, reminder_date_utc, local_time, timezone)`
- Also used for `needs_recurrence_day` clarification (weekly/monthly reminders missing a day)

### AM/PM and Time-of-Day Recognition
Recognizes AM/PM in three forms: explicit (`am`/`pm`), natural language (`morning`/`afternoon`/`evening`/`night`). Affects `has_am_pm` check, `clarify_time` handler, and `is_valid_response` check in `main.py`.

### Context-Aware Deletion
When users view a numbered list (memories, lists, reminders, recurring reminders), the system sets `last_active_list` to a context marker so "Delete #" knows what they were looking at. Without this, "Delete 2" would show a confusing disambiguation menu pulling items from all types.

**Context markers** (stored in `last_active_list` via `create_or_update_user()`):
- `"__MEMORIES__"` — set by "Show Memories" keyword handler and `list_memories` AI action
- `"__LISTS__"` — set by "My Lists" keyword handler (multiple lists) and `list_lists` AI action
- `"__REMINDERS__"` — set by `list_reminders` AI action
- `"__RECURRING__"` — set by "My Recurring" keyword handler
- `list_name` (actual name) — set when viewing a specific list's items

**Delete-by-number flow** in `main.py`: checks markers in order (`__RECURRING__` → `__REMINDERS__` → `__LISTS__` → `__MEMORIES__` → specific list → fallback disambiguation). Each stores a `pending_reminder_delete` JSON with `awaiting_confirmation: true` and clears `last_active_list`.

**YES confirmation handler** supports all types: `reminder`, `recurring`, `list_item`, `memory`, `list`.

**When adding new viewable lists:** set `last_active_list` to a `__MARKER__` when displaying, add a handler in the delete-by-number section, add the marker to the exclusion list, and add the type to the YES confirmation handler.

### Keyword Handlers vs AI Processing
`main.py` has keyword-based handlers that run **before** AI processing. When adding new commands:
- Add keyword matches for common phrasings
- Consider natural language variations
- Add safeguards in AI action handlers for misclassified intents

### Field Encryption
Optional AES-256-GCM encryption for PII (names, emails). Enabled via `ENCRYPTION_KEY` and `HASH_KEY` env vars.

### Smart Nudges
Proactive AI intelligence layer — sends ONE contextual insight per day. 8 nudge types, tier-gated. OFF by default. See `docs/changelog.md` for full implementation details.

### Onboarding Flow
3-step flow: Welcome (step 0) → First Name (step 1) → ZIP Code (step 2) → Done. Email is collected on Day 4 via a Celery task instead of during onboarding. If user provides full name (two words) at step 1, both first and last name are stored and flow proceeds to ZIP.

### Trial Lifecycle
8 automated messages from Day 3 to Day 44 post-signup. All timezone-aware (9-10 AM local). Celery tasks staggered hourly at :00/:05/:10/:12/:15/:20/:25. Day 4 email collection (:12) asks users for email (skipped during shortened onboarding). See `docs/changelog.md` "Trial Lifecycle Timeline" for full schedule.

## Environment Variables

**Required:** `OPENAI_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `DATABASE_URL`

**Optional:** `UPSTASH_REDIS_URL`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ENCRYPTION_KEY`, `HASH_KEY`, `STRIPE_*` keys, `SMTP_*` for email, `ANTHROPIC_API_KEY` (Agent 4 AI file identification)

## Rate Limiting

15 messages per 60-second window per user (configurable in `config.py`). IP-based rate limiting (5 req/5 min) on public endpoints (`/api/signup`, `/api/contact`). Brute force protection (5 failures/5 min lockout) on all admin auth endpoints.
