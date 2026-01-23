# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Remyndrs is an SMS-based AI memory and reminder service built with Python/FastAPI. Users interact entirely via SMS to store memories, create reminders, and manage to-do lists using natural language.

**Stack:** Python 3.11.9, FastAPI, PostgreSQL, Celery + Redis (Upstash), OpenAI GPT-4o-mini, Twilio SMS, Stripe billing

## Session Start/End

**START of session (run first!):**
```bash
git fetch --all && git pull origin main
```

**END of session (before switching computers):**
- Commit and push all changes to git
- Never deploy directly to Render - always push to git and let auto-deploy handle it

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run FastAPI server
uvicorn main:app --reload

# Run Celery worker (processes reminder tasks)
celery -A celery_app worker --loglevel=info

# Run Celery Beat (schedules periodic tasks)
celery -A celery_app beat --loglevel=info

# Run all tests
python run_tests.py

# Run quick tests (skip slow)
python run_tests.py --quick

# Run specific test categories
python run_tests.py --onboarding
python run_tests.py --reminders
python run_tests.py --lists
python run_tests.py --memories
python run_tests.py --edge
python run_tests.py --tasks
python run_tests.py --scenarios

# Run with coverage
python run_tests.py --coverage

# Run single test
pytest tests/test_reminders.py::TestReminderCreation::test_reminder_with_specific_time
```

## Architecture

### Request Flow
```
SMS → Twilio webhook (/sms) → main.py validates → ai_service.py processes with OpenAI
  → models/*.py persists to PostgreSQL → sms_service.py sends confirmation
  → Celery Beat (every 30s) checks due reminders → sends at user's timezone
```

### Layered Structure
- **HTTP Layer:** `main.py` (routes), `admin_dashboard.py` (admin metrics/broadcast), `cs_portal.py` (customer support)
- **Route Handlers:** `routes/handlers/` - modular handlers for reminders, lists, memories, pending states
- **Business Logic:** `services/` - AI processing, payments, onboarding, metrics
- **Data Access:** `models/` - user, reminder, memory, list operations (with type hints)
- **Background Tasks:** `tasks/reminder_tasks.py` - Celery periodic jobs
- **Utils:** timezone conversions, encryption, input validation, `db_helpers.py` for encryption queries

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

Deployed on Render with three services:
1. **sms-reminders-api** - FastAPI web service
2. **sms-reminders-worker** - Celery worker
3. **sms-reminders-beat** - Celery Beat scheduler

Config in `render.yaml`. Auto-deploys on push to main.

## Testing

Tests use `ConversationSimulator` to simulate SMS interactions without real messages. Key fixtures:
- `simulator` - simulates user SMS interactions
- `sms_capture` - captures outbound SMS for verification
- `ai_mock` - mocks AI responses for predictable testing
- `onboarded_user` - pre-created test user (auto-cleaned)
- `mock_datetime` - time mocking for reminder tests

Test phone number: `+15559876543`

### Test Safety
Tests are configured to **never hit real Twilio or OpenAI APIs**:
- `conftest.py` has autouse fixtures that mock all SMS/AI calls
- `sms_service.py` detects test environment and blocks real Twilio calls
- Use `.env.test` with `ENVIRONMENT=test` and fake API keys
- Run tests with: `py -3 -m pytest tests/test_onboarding.py -v`

## Key Patterns

### Timezone Handling
All timestamps stored in UTC, converted to user timezone on display. User timezone determined during onboarding from ZIP code.

### Reminder Atomicity
Uses `SELECT FOR UPDATE SKIP LOCKED` for distributed reminder claiming. Stale tasks released every 5 minutes if worker crashes.

### Subscription Tiers
- **Free:** 2 reminders/day, 5 lists, 10 items/list, 5 memories
- **Premium:** Unlimited reminders, 20 lists, 30 items/list, recurring reminders
- **Family:** Premium features for 4-10 members

### Field Encryption
Optional AES-256-GCM encryption for PII (names, emails). Enabled via `ENCRYPTION_KEY` and `HASH_KEY` env vars.

## Environment Variables

Required: `OPENAI_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `DATABASE_URL`

Optional: `UPSTASH_REDIS_URL`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ENCRYPTION_KEY`, `HASH_KEY`, `STRIPE_*` keys, `SMTP_*` for email

## Rate Limiting

15 messages per 60-second window per user (configurable in `config.py`).

## Multi-Agent Monitoring System

Automated issue detection and health tracking for the SMS service.

### Dashboard
- **URL:** `/admin/monitoring`
- Visual dashboard showing health score, open issues, patterns, and trends
- Click any issue to see full message context (user message + bot response)
- "Mark False Positive" button to dismiss non-issues

### Three Agents
1. **Agent 1 - Interaction Monitor** (`agents/interaction_monitor.py`)
   - Detects anomalies: user confusion, parsing failures, error responses, timezone issues
   - Scans `logs` table for patterns indicating problems

2. **Agent 2 - Issue Validator** (`agents/issue_validator.py`)
   - Validates issues from Agent 1, filters false positives
   - Optional AI analysis with GPT-4o-mini (`use_ai=True`)
   - Identifies recurring patterns

3. **Agent 3 - Resolution Tracker** (`agents/resolution_tracker.py`)
   - Calculates health score (0-100)
   - Tracks issue resolutions and detects regressions
   - Generates weekly reports

### Celery Schedule (automatic)
- **Hourly:** Critical issue check
- **Every 4 hours:** Agent 1 (interaction monitor)
- **Every 6 hours:** Agent 2 (issue validator)
- **Daily 6 AM UTC:** Full pipeline with AI
- **Weekly Monday 8 AM UTC:** Weekly report

### Manual Triggers
- Dashboard "Run Full Pipeline" button
- API: `GET /admin/pipeline/run?hours=24`

### Alerts
Configured via dashboard Alert Settings section:
- **Teams:** Requires `TEAMS_WEBHOOK_URL` env var
- **Email:** Requires `SMTP_*` env vars and recipient list
- **SMS:** For critical issues only (health < 50)

### Database Tables
`monitoring_issues`, `monitoring_runs`, `issue_patterns`, `issue_pattern_links`, `validation_runs`, `issue_resolutions`, `pattern_resolutions`, `health_snapshots`
