# UX Improvement Roadmap

**Overall UX Score: B+ (82/100)** - Based on comprehensive user journey analysis (Jan 2026)

## User Journey Map

### 1. Onboarding Flow (Grade: A-)
**Journey:** Hi -> Name -> Last Name -> Email -> ZIP -> Complete (14-day trial)

**Strengths:**
- Progressive disclosure (one question at a time)
- Flexible input (accepts "John Smith" as full name)
- Clear validation with helpful errors
- Safety valves (HELP, CANCEL, RESTART, SKIP)
- Immediate value (auto-creates first memory, sends VCF)
- Generous 14-day trial with no credit card

**Issues:**
- CRITICAL: No welcome message explaining what Remyndrs is
- MODERATE: Skip friction (persuasive text feels pushy)
- MODERATE: No progress indicator shown proactively

### 2. Premium Upgrade Flow (Grade: C+)
**Journey:** UPGRADE -> Pricing display -> PREMIUM/FAMILY -> Stripe link -> Payment -> Activated

**Strengths:**
- Clear pricing (monthly/annual options)
- Stripe Checkout (trusted, secure)
- Immediate premium access

**Issues:**
- CRITICAL: No trial end warning (silent downgrade on day 15)
- CRITICAL: Link-only upgrade (no fallback if link fails)
- MODERATE: No value reminder before purchase

### 3. Downgrade/Cancellation Flow (Grade: B)
**Strengths:**
- Data preservation (reminders/memories kept)
- Self-service Stripe Customer Portal
- Clear SMS confirmation
- Cancellation feedback collection (numbered options 1-4 + SKIP) *(added Feb 2026)*
- EXPORT command lets users download data before leaving *(added Feb 2026)*

**Issues:**
- ~~CRITICAL: No exit interview or cancellation feedback~~ DONE (Feb 2026)
- MODERATE: Unclear downgrade impact on existing data
- MODERATE: No win-back attempt after cancellation

### 4. Account Management (Grade: C)
**Issues:**
- CRITICAL: No account overview command (can't see status, tier, usage)
- CRITICAL: No usage visibility (free users don't know they're at 1/2 reminders)

---

## Prioritized Action Plan

**Status (Feb 2026):** Phases 1-2 not started. Phase 3 partially complete (cancellation feedback done via CS overhaul). Phase 4 partially complete (export before delete done). Website roadmap phases 1-2 are mostly complete (see `docs/website-roadmap.md`).

### PHASE 1: Critical Fixes (NOT STARTED)
**Impact: High | Effort: Low | Expected: +15% trial conversion**

#### 1. Trial Expiration Warnings
**Files:** `tasks/reminder_tasks.py`, add database column `trial_warning_sent`

```python
# Add Celery scheduled tasks for:
# Day 7:  "You have 7 days left in your Premium trial!
#          Text UPGRADE to keep unlimited reminders."
# Day 13: "Tomorrow is your last day of Premium trial.
#          Text UPGRADE now to continue unlimited features!"
# Day 14: "Your Premium trial has ended. You're now on the free plan
#          (2 reminders/day). Text UPGRADE anytime!"
```

#### 2. Welcome Message
**File:** `services/onboarding_service.py:121`

```python
# BEFORE asking for name, add:
"Welcome to Remyndrs! I'm your AI-powered reminder assistant.
I'll help you remember anything--from daily tasks to important dates.

Let's get you set up in under a minute! What's your first name?"
```

#### 3. Usage Counter for Free Users
**Files:** `main.py` (reminder confirmation), `services/tier_service.py`

```python
# After creating reminder (free tier):
"Reminder saved! (1 of 2 today)"

# When hitting limit:
"You've used your 2 free reminders today.
Resets at midnight, or text UPGRADE for unlimited!"
```

### PHASE 2: Value & Visibility
**Impact: Medium-High | Effort: Medium | Expected: +10% retention**

#### 4. Account Status Command
**Files:** `main.py`, create new handler in `routes/handlers/account.py`

```python
# Add INFO or STATUS command:
"""
Your Account

Plan: Premium ($6.99/month)
Member since: Jan 15, 2024
Next billing: Feb 15, 2024

This month:
- 47 reminders created
- 3 active lists
- 12 memories saved

Text ACCOUNT to manage billing
"""
```

#### 5. Mid-Trial Value Reminder
**Files:** `tasks/reminder_tasks.py`

```python
# Day 7 of trial:
"You're halfway through your Premium trial!

So far you've created 12 reminders and 2 lists.
After trial: only 2 reminders/day on free plan.

Text UPGRADE to keep unlimited access!"
```

### PHASE 3: Feedback & Retention
**Impact: Medium | Effort: Low-Medium | Expected: Better product insights**

#### 6. Cancellation Feedback Loop (COMPLETED - Feb 2026)
**Status:** Implemented as part of CS system overhaul. After Stripe cancellation webhook, users receive numbered options (1-4 + SKIP). Responses stored with `[CANCELLATION]` prefix in support tickets. Uses `pending_cancellation_feedback` flag on users table.
**Files modified:** `services/stripe_service.py`, `main.py`, `database.py`, `models/user.py`

#### 7. Win-Back Campaign
**Files:** `tasks/reminder_tasks.py` (30-day task)

```python
# 30 days after cancel:
"Hey! We've missed you at Remyndrs.

Since you left, we've added:
- Improved AI accuracy
- Faster response times
- New list features

Want to come back? Text UPGRADE for 20% off your first month!"
```

### PHASE 4: Polish & Optimization (Ongoing)

#### 8. Conversational Tone Improvements
- Replace robotic language with warm, friendly tone
- Shorten confirmation messages
- Add personality to error messages

#### 9. Progress Indicators
- Show "Step X of 4" in all onboarding prompts
- Add progress confirmation for multi-step workflows
- Clearer action feedback

#### 10. Export Before Delete (PARTIALLY COMPLETED - Feb 2026)
- ~~Offer to email data export before deletion~~ DONE: EXPORT SMS command emails JSON data; DELETE ACCOUNT now suggests "Text EXPORT first"
- 24-hour soft delete with UNDO option
- Clear communication about what gets deleted

---

## Key UX Principles

1. **Proactive Communication:** Warn users before limits, trial expiration, or changes
2. **Visibility:** Show usage stats, account status, and progress clearly
3. **Warmth:** Use friendly, conversational tone
4. **Flexibility:** Provide fallbacks (email links if SMS link fails)
5. **Data Safety:** Always preserve data, offer exports, soft deletes
6. **Feedback Loops:** Ask why users cancel, what features they want
7. **Progressive Enhancement:** Free tier should feel complete, not crippled
8. **Clear Value:** Remind users of benefits, especially before conversion points

---

## Quick Reference: Critical UX Files

| Journey Phase | Primary Files | Key Improvements Needed |
|---------------|---------------|------------------------|
| **Onboarding** | `services/onboarding_service.py` (121-391) | Welcome message, progress indicators |
| **Trial Management** | `tasks/reminder_tasks.py` | Expiration warnings (day 7, 13, 14) |
| **Premium Upgrade** | `main.py` (1774-1811), `services/stripe_service.py` | Value reminders, fallback options |
| **Usage Limits** | `services/tier_service.py` (225-331) | Proactive counters (X of Y) |
| **Account Status** | NEW: Create `routes/handlers/account.py` | INFO/STATUS command with stats |
| **Cancellation** | `services/stripe_service.py` (204-228) | ~~Feedback collection~~, win-back |
| **CS Portal** | `cs_portal.py` | Ticket management, assignment, SLA tracking |
| **Data Export** | `services/export_service.py`, `main.py` | EXPORT command, CS portal export |
