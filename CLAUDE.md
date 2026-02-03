# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Remyndrs is an SMS-based AI memory and reminder service built with Python/FastAPI. Users interact entirely via SMS to store memories, create reminders, and manage to-do lists using natural language.

**Stack:** Python 3.11.9, FastAPI, PostgreSQL, Celery + Redis (Upstash), OpenAI GPT-4o-mini, Twilio SMS, Stripe billing

## Recommended Model for Development

**Always use Claude Opus 4.5 for coding tasks.**

When working with Claude Code on this repository, use the `/model` command to switch to Opus 4.5:
```
/model opus
```

**Why Opus 4.5 for coding:**
- Superior code understanding and debugging capabilities
- Better at identifying complex bugs and edge cases
- More reliable for multi-file refactoring and architectural changes
- Higher accuracy for critical fixes and feature implementation

**When to use other models:**
- Documentation updates: Sonnet is sufficient
- Simple file reads or searches: Sonnet or Haiku
- Quick questions: Sonnet

For any substantive code changes, bug fixes, or feature development, **always use Opus 4.5**.

## Git Workflow

**Branching model:** Feature branches off `main`. No long-lived staging branch.

**Starting a session:**
```bash
git checkout main && git pull origin main
```

**Making changes:**
```bash
git checkout -b feature/short-description
# ... work and commit ...
git push -u origin feature/short-description
# Open PR → merge to main → delete branch
```

**Ending a session:** Push your feature branch so work isn't lost.

**Important:** Never deploy directly to Render — always push to git and let auto-deploy handle it.

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

Deployed on Render with four services:
1. **sms-reminders-api** - FastAPI web service
2. **sms-reminders-worker** - Celery worker (processes reminder tasks)
3. **sms-reminders-beat** - Celery Beat scheduler (triggers periodic tasks)
4. **sms-reminders-monitoring** - Celery worker for monitoring pipeline (dedicated queue)

Config in `render.yaml`. Auto-deploys on push to main.

**Deployment Time:**
- Current: ~5-7 minutes (optimized from 8-12 minutes)
- All 4 services deploy simultaneously
- Each service rebuilds independently (no shared cache on free tier)
- Production dependencies: `requirements-prod.txt` (15 packages, no test frameworks)
- Development dependencies: `requirements.txt` includes `-r requirements-prod.txt` + pytest

**Important Deployment Notes:**
1. **DATABASE_URL Syncing:** If database is recreated, the DATABASE_URL environment variable in all services must be manually updated via Render dashboard. The `fromDatabase` auto-sync in render.yaml only works during initial blueprint deployment or manual blueprint re-sync.

2. **Overlapping Deploys:** Render's default policy cancels previous deployments when a new one starts. This is normal and saves time by not deploying intermediate commits.

3. **CORS for API Endpoints:** FastAPI endpoints that need to be called from remyndrs.com must use CORSMiddleware. Do NOT use manual `@app.options()` handlers as they can fail. Example:
```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Or ["https://remyndrs.com"] for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

**Website Hosting:**
- **remyndrs.com** hosted on Netlify
- Static files served directly
- API calls go directly to `https://sms-reminders-api-1gmm.onrender.com` (CORS enabled)
- Alternative: Use Netlify `_redirects` file to proxy API calls (cleaner URLs, backend abstraction)

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

### Low-Confidence Reminder Confirmation Flow
When AI confidence is below threshold, reminders enter a pending confirmation state stored in `pending_reminder_confirmation` on the user record. Two code paths handle this:
- **Pending data storage:** `routes/handlers/reminders.py` stores pending JSON (action type, text, datetime, offsets, confidence)
- **Confirmation handling:** `main.py` (search `pending_confirmation`) processes YES/NO responses and calls `save_reminder_with_local_time()`

**Important:** `save_reminder_with_local_time()` requires 5 positional args: `(phone_number, reminder_text, reminder_date_utc, local_time, timezone)`. The `local_time` param is HH:MM format and `reminder_date` must be UTC. For relative reminders, the pre-calculated UTC datetime and local_time should be stored in pending data to avoid time drift.

### Field Encryption
Optional AES-256-GCM encryption for PII (names, emails). Enabled via `ENCRYPTION_KEY` and `HASH_KEY` env vars.

## Environment Variables

Required: `OPENAI_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `DATABASE_URL`

Optional: `UPSTASH_REDIS_URL`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ENCRYPTION_KEY`, `HASH_KEY`, `STRIPE_*` keys, `SMTP_*` for email, `ANTHROPIC_API_KEY` (for Agent 4 AI file identification)

## Rate Limiting

15 messages per 60-second window per user (configurable in `config.py`).

## Recent Bug Fixes & Improvements

### Context Loss Bug Fix (Feb 2026)
**Issue:** When users were selecting a list by number (e.g., replying "1" to "Which list?"), the daily summary handler was intercepting the response and asking "Did you mean 1 AM or 1 PM?" instead of adding items to the selected list.

**Root Cause:** The `has_pending_state` check in `main.py:576` didn't include `pending_list_item`, allowing the daily summary handler to run during list selection flows.

**Fix:** Added `pending_list_item` to the pending state checks, ensuring the daily summary handler skips when users are in the middle of list operations.

**Files Changed:**
- `main.py:576` - Added `pending_list_item` check to `has_pending_state`
- `agents/interaction_monitor.py` - Added context loss and flow violation detectors
- `agents/interaction_monitor.py:576` - Fixed log ordering (DESC → ASC) for chronological analysis

**Prevention:** New monitoring detectors (`context_loss`, `flow_violation`) now automatically catch similar handler ordering issues.

### Desktop Signup Flow Implementation (Feb 2026)
**Feature:** Implemented desktop signup flow to eliminate device switching friction for desktop website visitors.

**Problem:** Desktop users had to scan QR code or manually type phone number, causing ~50% bounce rate and 3% conversion (vs 12% mobile).

**Solution:** Added phone number input form on remyndrs.com that sends SMS directly to start onboarding.

**Implementation:**

**Backend (main.py:4455-4510):**
- Added `POST /api/signup` endpoint
- Phone validation for US numbers (10 or 11 digits)
- E.164 formatting (+1XXXXXXXXXX)
- Sends welcome SMS with onboarding prompt
- Logs interaction as `desktop_signup` for analytics
- Uses FastAPI CORSMiddleware for proper cross-origin support

**Frontend (remyndrs.com):**
- Responsive design: form on desktop (≥768px), SMS link on mobile
- JavaScript form handler with fetch API
- Direct API call to Render backend (Netlify proxy not required)
- Success/error message display with proper UX

**CORS Configuration:**
```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows remyndrs.com to call Render API
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

**Key Learning:** FastAPI's CORSMiddleware is the proper way to handle CORS. Manual `@app.options()` handlers can fail. The middleware automatically handles preflight OPTIONS requests and adds proper headers to all responses.

**Deployment Optimization (PR #61):**
- Split `requirements.txt` into `requirements-prod.txt` (production only) and `requirements.txt` (dev + prod)
- Removed pytest, pytest-asyncio, pytest-cov from production builds
- Reduced deployment time from 8-12 minutes to 5-7 minutes (~40% improvement)
- Note: 4 services deploy simultaneously (API + 3 workers), each rebuilds independently

**Database Connectivity Issue:**
- Services had stale DATABASE_URL pointing to old database hostname
- Fixed by manually updating DATABASE_URL in all 4 services via Render dashboard
- Root cause: Database was recreated but environment variables didn't auto-sync
- Blueprint auto-sync only works during initial deployment or manual re-sync

**Expected Impact:**
- Desktop conversion rate: **3% → 12%+** (+300% improvement)
- Matches mobile conversion rate
- Eliminates device switching friction
- +200% overall website conversion improvement

**Files Changed:**
- `main.py` - Added `/api/signup` endpoint, CORSMiddleware
- `requirements-prod.txt` - Created production-only dependencies file
- `requirements.txt` - Now includes `-r requirements-prod.txt` + dev dependencies
- `render.yaml` - Updated all services to use `requirements-prod.txt`
- `C:\Users\BradHodge\OneDrive - Simple IT\Remyndrs\Website\index.html` - Updated JavaScript to call Render API directly

**PRs:**
- PR #59: Desktop signup flow implementation
- PR #60: Initial CORS headers attempt
- PR #61: Deployment optimization
- PR #62: Fixed CORS with proper middleware

## Multi-Agent Monitoring System

Automated issue detection and health tracking for the SMS service.

### Dashboard
- **URL:** `/admin/monitoring`
- Visual dashboard showing health score, open issues, patterns, and trends
- Click any issue to see full message context (user message + bot response)
- "Mark False Positive" button to dismiss non-issues

### Four Agents
1. **Agent 1 - Interaction Monitor** (`agents/interaction_monitor.py`)
   - Detects anomalies: user confusion, parsing failures, error responses, timezone issues
   - **Context Loss Detection:** Catches when bot asks for input (e.g., "reply with number") but responds with unrelated content
   - **Flow Violation Detection:** Detects when bot ignores YES/NO responses or expected input formats
   - Scans `logs` table for patterns indicating problems
   - Processes logs chronologically to detect multi-turn conversation issues

2. **Agent 2 - Issue Validator** (`agents/issue_validator.py`)
   - Validates issues from Agent 1, filters false positives
   - Optional AI analysis with GPT-4o-mini (`use_ai=True`)
   - Identifies recurring patterns

3. **Agent 3 - Resolution Tracker** (`agents/resolution_tracker.py`)
   - Calculates health score (0-100)
   - Tracks issue resolutions and detects regressions
   - Generates weekly reports

4. **Agent 4 - Fix Planner** (`agents/fix_planner.py`)
   - Analyzes validated, unresolved issues
   - Identifies affected source files based on issue type/pattern
   - Extracts relevant code context
   - Generates paste-ready Claude Code prompts for fixes
   - Optional AI file identification with Claude API (`--ai` flag)

### Celery Schedule (automatic)
- **Hourly:** Critical issue check
- **Every 4 hours:** Agent 1 (interaction monitor)
- **Every 6 hours:** Agent 2 (issue validator)
- **Daily 6 AM UTC:** Full pipeline with AI
- **Weekly Monday 8 AM UTC:** Weekly report

### Manual Triggers
- Dashboard "Run Full Pipeline" button
- API: `GET /admin/pipeline/run?hours=24`
- Agent 4: `python -m agents.fix_planner --issue 123` or `python agents/run_fix_planner.py`
- Full pipeline with fix planner: `python agents/run_pipeline.py --fix-planner`

### Alerts
Configured via dashboard Alert Settings section:
- **Teams:** Requires `TEAMS_WEBHOOK_URL` env var
- **Email:** Requires `SMTP_*` env vars and recipient list
- **SMS:** For critical issues only (health < 50)

### Issue Types Detected
- **user_confusion** - User expresses confusion ("huh?", "what?", "not working")
- **error_response** - System sends error message ("sorry", "couldn't process", "try again")
- **parsing_failure** - System can't understand user intent
- **timezone_issue** - User reports wrong time/timezone
- **failed_action** - Action explicitly failed (success=False)
- **action_not_found** - System tried to act on non-existent item
- **confidence_rejection** - User rejected low-confidence interpretation
- **repeated_attempts** - User sends same message 3+ times (frustration)
- **delivery_failure** - Reminder failed to send
- **context_loss** 🔀 - Bot asks for input but responds with unrelated content (e.g., asks "which list?" → user says "1" → bot talks about time)
- **flow_violation** ⛔ - Bot ignores expected response (e.g., asks YES/NO → user says "YES" → bot ignores it)

### Database Tables
`monitoring_issues`, `monitoring_runs`, `issue_patterns`, `issue_pattern_links`, `validation_runs`, `issue_resolutions`, `pattern_resolutions`, `health_snapshots`, `fix_proposals`, `fix_proposal_runs`

---

## UX Improvement Roadmap

**Overall UX Score: B+ (82/100)** - Based on comprehensive user journey analysis (Jan 2026)

### User Journey Map

#### 1. Onboarding Flow (Grade: A-)
**Journey:** Hi → Name → Last Name → Email → ZIP → Complete (14-day trial)

**Strengths:**
- Progressive disclosure (one question at a time)
- Flexible input (accepts "John Smith" as full name)
- Clear validation with helpful errors
- Safety valves (HELP, CANCEL, RESTART, SKIP)
- Immediate value (auto-creates first memory, sends VCF)
- Generous 14-day trial with no credit card

**Issues:**
- ❌ **CRITICAL:** No welcome message explaining what Remyndrs is
- ⚠️ **MODERATE:** Skip friction (persuasive text feels pushy)
- ⚠️ **MODERATE:** No progress indicator shown proactively

#### 2. Premium Upgrade Flow (Grade: C+)
**Journey:** UPGRADE → Pricing display → PREMIUM/FAMILY → Stripe link → Payment → Activated

**Strengths:**
- Clear pricing (monthly/annual options)
- Stripe Checkout (trusted, secure)
- Immediate premium access

**Issues:**
- ❌ **CRITICAL:** No trial end warning (silent downgrade on day 15)
- ❌ **CRITICAL:** Link-only upgrade (no fallback if link fails)
- ⚠️ **MODERATE:** No value reminder before purchase

#### 3. Downgrade/Cancellation Flow (Grade: B-)
**Strengths:**
- Data preservation (reminders/memories kept)
- Self-service Stripe Customer Portal
- Clear SMS confirmation

**Issues:**
- ❌ **CRITICAL:** No exit interview or cancellation feedback
- ⚠️ **MODERATE:** Unclear downgrade impact on existing data
- ⚠️ **MODERATE:** No win-back attempt after cancellation

#### 4. Account Management (Grade: C)
**Issues:**
- ❌ **CRITICAL:** No account overview command (can't see status, tier, usage)
- ❌ **CRITICAL:** No usage visibility (free users don't know they're at 1/2 reminders)

---

### Prioritized Action Plan

#### PHASE 1: Critical Fixes (Week 1)
**Impact: High | Effort: Low | Expected: +15% trial conversion**

##### 1. Trial Expiration Warnings (2 hours)
**Files:** `tasks/reminder_tasks.py`, add database column `trial_warning_sent`

```python
# Add Celery scheduled tasks for:
# Day 7:  "You have 7 days left in your Premium trial! ⏰
#          Text UPGRADE to keep unlimited reminders."
# Day 13: "Tomorrow is your last day of Premium trial.
#          Text UPGRADE now to continue unlimited features!"
# Day 14: "Your Premium trial has ended. You're now on the free plan
#          (2 reminders/day). Text UPGRADE anytime!"
```

##### 2. Welcome Message (15 minutes)
**File:** `services/onboarding_service.py:121`

```python
# BEFORE asking for name, add:
"Welcome to Remyndrs! 👋 I'm your AI-powered reminder assistant.
I'll help you remember anything—from daily tasks to important dates.

Let's get you set up in under a minute! What's your first name?"
```

##### 3. Usage Counter for Free Users (1 hour)
**Files:** `main.py` (reminder confirmation), `services/tier_service.py`

```python
# After creating reminder (free tier):
"✓ Reminder saved! (1 of 2 today)"

# When hitting limit:
"You've used your 2 free reminders today. ⏰
Resets at midnight, or text UPGRADE for unlimited!"
```

#### PHASE 2: Value & Visibility (Week 2)
**Impact: Medium-High | Effort: Medium | Expected: +10% retention**

##### 4. Account Status Command (3 hours)
**Files:** `main.py`, create new handler in `routes/handlers/account.py`

```python
# Add INFO or STATUS command:
"""
📊 Your Account

Plan: Premium ($6.99/month)
Member since: Jan 15, 2024
Next billing: Feb 15, 2024

This month:
• 47 reminders created
• 3 active lists
• 12 memories saved

Text ACCOUNT to manage billing
"""
```

##### 5. Mid-Trial Value Reminder (1 hour)
**Files:** `tasks/reminder_tasks.py`

```python
# Day 7 of trial:
"You're halfway through your Premium trial! 🎉

So far you've created 12 reminders and 2 lists.
After trial: only 2 reminders/day on free plan.

Text UPGRADE to keep unlimited access!"
```

#### PHASE 3: Feedback & Retention (Week 3)
**Impact: Medium | Effort: Low-Medium | Expected: Better product insights**

##### 6. Cancellation Feedback Loop (2 hours)
**Files:** `services/stripe_service.py` (webhook handler), add database column `cancellation_reason`

```python
# After Stripe cancellation webhook:
"Sorry to see you go! 😢

Quick question: Why did you cancel?
1. Too expensive
2. Not using it enough
3. Missing a feature
4. Other reason

(Reply with number or SKIP)"
```

##### 7. Win-Back Campaign (2 hours)
**Files:** `tasks/reminder_tasks.py` (30-day task)

```python
# 30 days after cancel:
"Hey! We've missed you at Remyndrs.

Since you left, we've added:
• Improved AI accuracy
• Faster response times
• New list features

Want to come back? Text UPGRADE for 20% off your first month!"
```

#### PHASE 4: Polish & Optimization (Ongoing)

##### 8. Conversational Tone Improvements
- Replace robotic language with warm, friendly tone
- Add appropriate emoji (✓ ⏰ 📊 🎉 😊)
- Shorten confirmation messages
- Add personality to error messages

**Examples:**
```
❌ "Your account has been reset. Let's start over!"
✅ "All set! Let's start fresh 😊"

❌ "Feedback received. Thank you!"
✅ "Thanks for the feedback! We really appreciate it."

❌ "You've reached your daily limit of 2 reminders."
✅ "Oops, you've hit your 2 reminders for today. Want unlimited? Text UPGRADE!"
```

##### 9. Progress Indicators
- Show "Step X of 4" in all onboarding prompts
- Add progress confirmation for multi-step workflows
- Clearer action feedback

##### 10. Export Before Delete
- Offer to email data export before deletion
- 24-hour soft delete with UNDO option
- Clear communication about what gets deleted

---

### Expected Impact Metrics

| Metric | Current | After Phase 1 | After Phase 2 | After Phase 3 |
|--------|---------|---------------|---------------|---------------|
| Trial → Paid Conversion | 15% | **25%** (+10%) | **28%** (+3%) | **30%** (+2%) |
| Onboarding Completion | 85% | **90%** (+5%) | 90% | 90% |
| 3-Month Retention | 60% | 60% | **68%** (+8%) | **72%** (+4%) |
| Support Tickets/Month | 100 | **80** (-20%) | **60** (-20%) | 60 |

**Revenue Impact (1000 signups/month):**
- Current: 150 conversions × $6.99 = $1,048/month
- After improvements: 300 conversions × $6.99 = $2,097/month
- **Net gain: +$1,049/month (+100%)**

---

### Key UX Principles for Future Development

1. **Proactive Communication:** Warn users before limits, trial expiration, or changes
2. **Visibility:** Show usage stats, account status, and progress clearly
3. **Warmth:** Use friendly, conversational tone with appropriate emoji
4. **Flexibility:** Provide fallbacks (email links if SMS link fails)
5. **Data Safety:** Always preserve data, offer exports, soft deletes
6. **Feedback Loops:** Ask why users cancel, what features they want
7. **Progressive Enhancement:** Free tier should feel complete, not crippled
8. **Clear Value:** Remind users of benefits, especially before conversion points

---

### Quick Reference: Critical UX Files

| Journey Phase | Primary Files | Key Improvements Needed |
|---------------|---------------|------------------------|
| **Onboarding** | `services/onboarding_service.py` (121-391) | Welcome message, progress indicators |
| **Trial Management** | `tasks/reminder_tasks.py` | Expiration warnings (day 7, 13, 14) |
| **Premium Upgrade** | `main.py` (1774-1811), `services/stripe_service.py` | Value reminders, fallback options |
| **Usage Limits** | `services/tier_service.py` (225-331) | Proactive counters (X of Y) |
| **Account Status** | NEW: Create `routes/handlers/account.py` | INFO/STATUS command with stats |
| **Cancellation** | `services/stripe_service.py` (204-228) | Feedback collection, win-back |

---

## Website UX Improvement Roadmap (remyndrs.com)

**Overall Grade: B+ (84/100)** - Based on comprehensive website analysis (Jan 2026)

### Website Strengths

1. **Clear Value Proposition (A)** - "Never Forget Anything Again" via SMS
2. **Strong Messaging Consistency (A)** - "No app required" reinforced throughout
3. **Effective Social Proof (B+)** - Relatable testimonial from beta user
4. **Smart CTAs (A-)** - Multiple touchpoints, device-aware SMS links
5. **Interactive Demo (A)** - Animated conversation bubbles demonstrate functionality

### Critical Website Issues

#### 1. Desktop Signup Friction (CRITICAL - P0)
**Problem:** Desktop visitors must switch devices (scan QR or manually type number)
**Impact:** 50-60% bounce rate from desktop traffic
**Current conversion:** 3% on desktop vs 12% on mobile

**Fix:** Add web signup flow
```html
<div class="desktop-signup">
  <h3>Sign up on your computer?</h3>
  <p>Enter your phone number - we'll text you to get started</p>
  <input type="tel" placeholder="+1 (555) 123-4567">
  <button>Text Me →</button>
  <small>We'll send a one-time signup link. Standard SMS rates apply.</small>
</div>
```

**Backend needed:** Create endpoint to send signup SMS with unique link
**Estimated impact:** +25-40% overall conversion rate

#### 2. Missing Trust Indicators (CRITICAL - P0)
**Issues:**
- ❌ Privacy policy returns 404
- ❌ No terms of service visible
- ❌ No "About Us" or team info
- ❌ Privacy messaging buried mid-page
- ❌ Only 1 testimonial
- ❌ No security badges/certifications

**Consumer concern:** "Who are you? Can I trust you with my data?"

**Fixes needed:**
```
Header: Add links to Privacy, Terms, About

Footer: Add
- © 2024 Remyndrs, Inc.
- Privacy Policy | Terms of Service | Contact
- Trust badges: "🔒 Encrypted" "SOC 2" (if applicable)
- Social links

Hero: Add mini trust bar
"🔒 Bank-level encryption | ⭐ 500+ users | 📱 Works on Any Phone"
```

**Priority:** Create `/privacy` and `/terms` pages ASAP (removes major trust barrier)

#### 3. Unclear Post-Trial Journey (HIGH - P0)
**Current:** "14-day free trial • No credit card required"

**User questions:**
- What happens on day 15?
- Do I get charged automatically?
- Can I stay free forever?

**Fix:**
```
Premium tier card:
"Start 14-Day Free Trial
• Full premium access
• No credit card required
• Auto-downgrades to free tier (no surprise charges)
• Upgrade anytime during or after trial"

Add FAQ:
"What happens after my trial?"
"Your account automatically moves to our always-free tier.
You keep all your data and can upgrade anytime."
```

#### 4. Weak Feature Differentiation (MODERATE - P1)
**Current vague features:**
- "Smart Reminders" ← What makes them smart?
- "Always Accessible" ← Too generic
- "Constantly Learning" ← Sounds like beta excuse

**Better approach:**
```
Replace with specific, measurable benefits:

❌ "Smart Reminders"
✅ "AI-Powered Parsing - 'Remind me tomorrow at 3pm' just works"

❌ "Always Accessible"
✅ "Works on Any Phone - Flip phone to iPhone, we've got you"

❌ "Constantly Learning"
✅ "Recurring Reminders - Daily, weekly, or custom schedules"

Add missing features:
✅ "Shared Lists - Collaborate via SMS"
✅ "Memory Storage - Ask 'What's my dentist's number?'"
```

#### 5. Pricing Confusion (MODERATE - P1)
**Current:** "$4.99/month (6 months) normally $6.99"

**User confusion:**
- Is this $30 upfront or monthly billing?
- When does it go to $6.99?
- Is "87 spots remaining" real or fake scarcity?

**Fix:**
```
Premium - Limited Time Offer
$4.99/month for your first 6 months
(Then $6.99/month - cancel anytime)

✓ Unlimited reminders
✓ 20 lists
✓ Recurring reminders
✓ Priority support

[Start Free Trial →]

Remove "87 spots remaining" unless real-time
Add annual option: "$59/year (save $24)"
```

#### 6. No FAQ Section (MODERATE - P1)
**Missing critical answers:**
- Do I pay for SMS messages?
- What carriers are supported?
- Can I use internationally?
- How do I cancel?
- What data is collected?
- Can I export my data?

**Fix:** Add comprehensive FAQ before pricing section

Top 10 FAQs to include:
1. How much do SMS messages cost?
2. What happens after my free trial?
3. How do I cancel?
4. Is my data private?
5. What carriers do you support?
6. Can I use this internationally?
7. How does the AI understand my messages?
8. Can I share lists with others?
9. What if I lose my phone?
10. How do I export my data?

---

### Website Improvement Action Plan

#### PHASE 1: Trust & Legal (Week 1 - CRITICAL)
**Impact:** Removes major conversion barrier
**Effort:** 1-2 days

##### 1. Create Privacy Policy Page (1 day)
- Create `/privacy` page (currently 404)
- Use plain language, not legalese
- Include: data collection, usage, retention, user rights
- Add "Last updated: [date]"
- Link from header and footer

##### 2. Create Terms of Service (1 day)
- Create `/terms` page
- Cover: usage rights, subscription terms, cancellation policy
- Link from header and footer

##### 3. Add Trust Indicators to Hero (2 hours)
```html
<!-- Below main CTA -->
<div class="trust-bar">
  🔒 Encrypted & Private | ⭐ 500+ Happy Users | 📱 Works on Any Phone
</div>
```

#### PHASE 2: Conversion Optimization (Week 2 - HIGH IMPACT)
**Impact:** +200% desktop conversion rate
**Effort:** 3-4 days

##### 4. Desktop Signup Flow ✅ COMPLETED (Feb 2026)
**Status:** Implemented and deployed
**Frontend:** Phone number input form on remyndrs.com (responsive design)
**Backend:** `/api/signup` endpoint with phone validation and SMS sending
**Flow:** User enters phone → receives SMS with welcome message → starts onboarding
**Result:** Desktop conversion expected to increase from 3% to 12%+ (+300%)
**See:** "Desktop Signup Flow Implementation" in Recent Bug Fixes & Improvements section above

##### 5. Add FAQ Section (1 day)
**Placement:** After "How It Works", before Pricing
**Content:** Top 10 FAQs with clear, helpful answers
**Format:** Expandable accordion (collapsed by default)

##### 6. Clarify Pricing Copy (2 hours)
- Explicit post-trial explanation
- Remove fake scarcity ("87 spots") unless real-time
- Add annual pricing option
- Show savings clearly

##### 7. Expand Social Proof (1 day)
- Add 3-5 more testimonials with photos
- Show metrics: "10,000+ reminders sent this week"
- Add trust badges (if applicable)
- Consider video testimonials

#### PHASE 3: Feature Communication (Week 3 - OPTIMIZATION)
**Impact:** Better product understanding
**Effort:** 2-3 days

##### 8. Rewrite Feature Benefits (3 hours)
Replace generic features with specific, measurable benefits
Focus on outcomes, not capabilities

##### 9. Create Demo Video (1 day)
**Length:** 30 seconds
**Content:**
1. Text "Remind me tomorrow at 3pm to call mom"
2. Get instant confirmation
3. Show reminder arriving at exact time

**Placement:** Near hero section, autoplay (muted)

##### 10. Mobile Optimization Pass (1 day)
- Test on actual devices (iPhone, Android)
- Larger tap targets (min 44px)
- Fix animation layout shifts
- Test QR code visibility

#### PHASE 4: Testing & Iteration (Ongoing)
**Impact:** Continuous improvement
**Effort:** Ongoing

##### 11. A/B Test Headlines
**Current:** "Never Forget Anything Again"

**Test alternatives:**
- "Your Phone. Your Reminders. That's It."
- "Remember Everything. Without Remembering to Check an App."
- "The Reminder App That Isn't an App"

##### 12. Add Live Chat (2 hours)
- Intercom, Drift, or simple email popup
- Answers questions in real-time
- Reduces bounce from confusion

##### 13. Social Sharing (1 hour)
```
"Love Remyndrs? Share with friends!"
[Twitter] [Facebook] [LinkedIn]
```

---

### Website Conversion Funnel Analysis

#### Current Estimated Funnel:
```
100 visitors to site
  ↓ 70% engage (scroll past hero)
  ↓ 40% read pricing
  ↓ 15% attempt signup
  ↓ 8% complete signup (desktop friction kills this)
  ↓ 5% activate (complete onboarding)
  ↓ 2% convert to paid
```

#### After Improvements:
```
100 visitors to site
  ↓ 80% engage (better trust signals)
  ↓ 55% read pricing (clearer value prop)
  ↓ 30% attempt signup (desktop flow added)
  ↓ 20% complete signup (reduced friction)
  ↓ 15% activate (clearer expectations)
  ↓ 5% convert to paid (better post-trial clarity)
```

#### Drop-off Points & Fixes:
1. **Desktop bounce (50%)** → Desktop signup flow → +40% desktop conversion
2. **Trust concerns (20%)** → Privacy policy + trust badges → +15% engagement
3. **Pricing confusion (25%)** → Clear post-trial messaging → +10% activation

---

### Expected Website Impact Metrics

| Metric | Current | After Phase 1 | After Phase 2 | Target |
|--------|---------|---------------|---------------|--------|
| **Overall Signup Rate** | 8% | **10%** (+2%) | **15%** (+5%) | 15% |
| **Desktop Conversion** | 3% | **5%** (+2%) | **12%** (+7%) | 12% |
| **Mobile Conversion** | 12% | **14%** (+2%) | **18%** (+4%) | 18% |
| **Trust Score (1-10)** | 6/10 | **8/10** (+2) | **8.5/10** | 9/10 |
| **Trial → Paid** | 15% | **20%** (+5%) | **25%** (+5%) | 30% |

**Revenue Impact (10,000 monthly website visitors):**
- Current: 800 signups → 120 paid → $840/month
- After Phase 2: 1,500 signups → 375 paid → **$2,625/month**
- **Net gain: +$1,785/month (+212%)**

---

### Website Quick Wins (< 1 hour each)

1. **Make phone number clickable** (5 min)
   ```html
   <a href="sms:+18555521950&body=START">Text START to (855) 552-1950</a>
   ```

2. **Fix 404 privacy page** (15 min)
   Even a placeholder is better than 404

3. **Add "As Seen In" section** (30 min)
   Product Hunt, TechCrunch (if applicable)

4. **Improve CTA copy** (15 min)
   ```
   Before: "Get Started"
   After: "Start Free Trial - No Credit Card"
   ```

5. **Add social proof numbers** (10 min)
   ```
   "Join 10,000+ users who never forget important things"
   ```

---

### Copy Improvements

#### Current Hero:
```
"Never Forget Anything Again"
"Your personal memory assistant via SMS"
```

#### Stronger Alternatives (A/B Test):

**Option A (Problem-focused):**
```
"Tired of Forgetting Important Things?"
"Text Remyndrs like a friend. We'll remember for you."
```

**Option B (Benefit-focused):**
```
"Your Brain, Enhanced"
"Remember everything without downloading anything."
```

**Option C (Friction-focused) - RECOMMENDED:**
```
"Reminders That Actually Work"
"No app. No login. Just text and we'll remind you."
```

**Recommendation:** Test Option C (most differentiated from competitors)

---

### Implementation Checklist

#### Week 1: Trust & Legal (CRITICAL)
- [ ] Create privacy policy page (`/privacy`)
- [ ] Create terms of service page (`/terms`)
- [ ] Add footer links to both
- [ ] Add trust bar to hero section
- [ ] Add "About Us" page with team/mission

#### Week 2: Conversion (HIGH IMPACT)
- [ ] Build desktop signup flow (phone input → SMS)
- [ ] Create backend endpoint for signup SMS
- [ ] Add comprehensive FAQ section
- [ ] Clarify pricing copy (post-trial path)
- [ ] Add 3+ testimonials with photos

#### Week 3: Optimization
- [ ] Rewrite feature benefits (specific outcomes)
- [ ] Create 30-second demo video
- [ ] Mobile optimization testing pass
- [ ] Set up A/B testing framework
- [ ] Add live chat widget

#### Ongoing
- [ ] Monitor conversion funnel metrics
- [ ] A/B test headlines and CTAs
- [ ] Collect and add new testimonials
- [ ] Update FAQ based on support questions
- [ ] Iterate on messaging based on user feedback

---

### Top 3 Website Priorities

If you only do 3 things:

1. **Create Privacy Policy** (1 day) - Currently 404, major trust issue
2. **Desktop Signup Flow** (3 days) - Doubles desktop conversion (+200%)
3. **Add FAQ Section** (1 day) - Answers concerns before they bounce

**Total time:** ~5 days
**Expected impact:** +200% overall conversion rate

---

### Website vs SMS App Alignment

**Critical:** Ensure website promises match SMS experience

| Website Claims | SMS Reality | Status |
|----------------|-------------|--------|
| "No app required" | ✅ SMS-only | ✓ Aligned |
| "Natural language" | ✅ AI parsing works | ✓ Aligned |
| "14-day free trial" | ✅ Granted on signup | ✓ Aligned |
| "No credit card" | ✅ True | ✓ Aligned |
| "Auto-downgrade to free" | ❌ Silent downgrade, no warning | ⚠️ Misaligned |
| "2 reminders/day free" | ✅ Enforced | ✓ Aligned |
| "Recurring reminders" | ✅ Premium feature | ✓ Aligned |

**Fix needed:** Website should mention trial expiration behavior matches SMS app roadmap (add warnings on day 7, 13, 14)
