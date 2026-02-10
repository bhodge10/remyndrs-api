# UX Improvement Roadmap

**Overall UX Score: A- (90/100)** - Updated Feb 2026 after Phase 1 completion

**🚀 LAUNCH READY** - All critical UX blockers resolved as of Feb 9, 2026

## User Journey Map

### 1. Onboarding Flow (Grade: A)
**Journey:** Hi -> Name -> Last Name -> Email -> ZIP -> Complete (14-day trial)

**Strengths:**
- Progressive disclosure (one question at a time)
- Flexible input (accepts "John Smith" as full name)
- Clear validation with helpful errors
- Safety valves (HELP, CANCEL, RESTART, SKIP)
- Immediate value (auto-creates first memory, sends VCF)
- Generous 14-day trial with no credit card
- **✅ Clear welcome message explaining value proposition** *(added Feb 2026)*

**Remaining Minor Issues:**
- MODERATE: Skip friction (persuasive text feels pushy)
- MODERATE: No progress indicator shown proactively

### 2. Premium Upgrade Flow (Grade: A-)
**Journey:** UPGRADE -> Pricing display -> PREMIUM/FAMILY -> Stripe link -> Payment -> Activated

**Strengths:**
- Clear pricing (monthly/annual options)
- Stripe Checkout (trusted, secure)
- Immediate premium access
- **✅ Trial expiration warnings (day 7, 1, 0)** *(added Feb 2026)*

**Remaining Minor Issues:**
- MODERATE: Link-only upgrade (no fallback if link fails)
- MODERATE: No mid-trial value reminder (day 7)

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

### 4. Account Management (Grade: A)
**Strengths:**
- **✅ STATUS command shows account overview** *(added Feb 2026 - PRs #55-58)*
- **✅ Usage counter for free tier** *(added Feb 2026 - PR #53)*
- Trial status visibility with days remaining
- Next billing date for paid users
- Usage stats (reminders, lists, memories)
- Quick action buttons (UPGRADE, ACCOUNT)

**All critical issues resolved.**

---

## Prioritized Action Plan

**Status (Feb 9, 2026):**
- ✅ **Phase 1: COMPLETED** (PR #53 - Feb 2026)
- ✅ **Phase 2: COMPLETED** (PRs #53, #55-58 - Feb 2026)
- ✅ **Phase 3: PARTIALLY COMPLETE** (cancellation feedback done, win-back pending)
- 🔄 **Phase 4: ONGOING** (polish & optimization)

Website roadmap phases 1-2 are mostly complete (see `docs/website-roadmap.md`).

---

### PHASE 1: Critical Fixes ✅ **COMPLETED - Feb 2026**
**Impact: High | Effort: Low | Expected: +15% trial conversion**

#### 1. Trial Expiration Warnings ✅ **DONE** (PR #53)
**Location:** `tasks/reminder_tasks.py:740-849`
**Database:** Columns added: `trial_warning_7d_sent`, `trial_warning_1d_sent`, `trial_warning_0d_sent`

**Implementation:**
- Day 7: "You have 7 days left in your Premium trial! ⏰"
- Day 1: "Tomorrow is your last day of Premium trial! ⏰"
- Day 0: "Your Premium trial has ended. You're now on the free plan (2 reminders/day)."
- Runs daily via Celery Beat at 9 AM UTC
- Tracks sent warnings to avoid duplicates

#### 2. Welcome Message ✅ **DONE** (PR #53)
**Location:** `services/onboarding_service.py:226-233`

**Implementation:**
```
Welcome to Remyndrs! 👋

I'm your AI-powered reminder assistant. I'll help you remember anything—from
daily tasks to important dates.

No app needed - just text me naturally and I'll handle the rest!

Let's get you set up in under a minute. What's your first name?
```

#### 3. Usage Counter for Free Users ✅ **DONE** (PR #53)
**Location:** `services/tier_service.py:372-399`, used in `main.py:3631-3632` and `3761-3762`

**Implementation:**
- Shows "✓ Reminder saved! (1 of 2 today)" after each reminder
- At limit: "⏰ Daily limit reached! Resets at midnight, or text UPGRADE for unlimited."
- Only displays for free tier users
- Automatic counter tracking

#### 3b. Progressive Education for Tier Limits ✅ **DONE** (Feb 2026)
**Location:** `services/tier_service.py` (6 new functions), `routes/handlers/lists.py`, `routes/handlers/pending_states.py`, `main.py`
**PR:** feature/progressive-education

**The Education Pyramid:**
Replaces confusing limit messages with progressive education that helps users understand why they hit limits and how to proceed.

**Level 1 (0-70% of limit): Silent**
- No friction, no counters shown
- Users unaware of limits until relevant

**Level 2 (70-90%): Gentle Nudge**
- Shows usage counters: "(7 of 10 items)", "(4 of 5 memories)"
- Only for free tier users (premium never sees counters)
- Passive education before limits become a problem

**Level 3 (90-100%): Clear Warning**
- Enhanced counter: "(9 of 10 items) - Almost full!"
- Shows "Last one!" at 100%
- Prepares users for upcoming limits

**Level 4 (Over limit): Blocked with Solution**
- WHY-WHAT-HOW message structure:
  - **WHY:** "Your grocery list is full (10 items max on Free plan)"
  - **WHAT:** "Can't add: Cat litter, cat food, yogurt, granola, fruit, milk, potatoes"
  - **HOW:** "To add more: • Remove items from grocery list • Text UPGRADE for 30 items/list"
- Trial hint for expired trial users: "Still on trial? Text STATUS"

**Before (Confusing):**
```
Added 0 items to your grocery list: . (7 items skipped - list full)
```

**After (Clear):**
```
Your grocery list is full (10 items max on Free plan).

Can't add: Cat litter, cat food, yogurt, granola, fruit, milk, potatoes

To add more:
• Remove items from grocery list
• Text UPGRADE for 30 items/list
```

**Enhanced STATUS Command:**
Free tier users now see a tier comparison showing what they'd get with Premium:
```
✨ Premium Benefits:
• Unlimited reminders (you: 2/day)
• 20 lists (you: 5)
• 30 items per list (you: 10)
• Unlimited memories (you: 5)
• Recurring reminders

Only $6.99/month - Text UPGRADE
```

**Testing:** 13/13 new unit tests pass, 96/98 integration tests pass. See `TEST_RESULTS.md` for details.

### PHASE 2: Value & Visibility ✅ **COMPLETED - Feb 2026**
**Impact: Medium-High | Effort: Medium | Expected: +10% retention**

#### 4. Account Status Command ✅ **DONE** (PRs #55-58)
**Location:** `main.py:2159-2268`
**Keywords:** `STATUS`, `MY ACCOUNT`, `ACCOUNT INFO`, `USAGE`

**Implementation:**
```
📊 Account Status

Plan: Premium (Trial - 7 days left)
Member since: Jan 15, 2024
Next billing: Feb 15, 2024

This Month:
• 12 reminders created today
• 3 of 20 lists
• 8 memories saved

Quick Actions:
• Text ACCOUNT to manage billing
```

**Features:**
- Trial status with days remaining
- Member since date
- Next billing date (paid users)
- Usage stats: reminders (with daily counter for free), lists, memories
- Context-aware quick actions

#### 5. Mid-Trial Value Reminder ⏳ **NOT STARTED**
**Files:** `tasks/reminder_tasks.py` (task exists but value reminder not implemented)

**Planned Implementation:**
```python
# Day 7 of trial:
"You're halfway through your Premium trial!

So far you've created 12 reminders and 2 lists.
After trial: only 2 reminders/day on free plan.

Text UPGRADE to keep unlimited access!"
```

**Priority:** Medium (post-launch optimization)

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

| Journey Phase | Primary Files | Status |
|---------------|---------------|--------|
| **Onboarding** | `services/onboarding_service.py:226-233` | ✅ Welcome message implemented |
| **Trial Management** | `tasks/reminder_tasks.py:740-849` | ✅ Warnings (day 7, 1, 0) implemented |
| **Premium Upgrade** | `main.py:1774-1811`, `services/stripe_service.py` | ✅ Core flow complete, mid-trial reminder pending |
| **Usage Limits** | `services/tier_service.py:372-399` | ✅ Usage counters implemented |
| **Account Status** | `main.py:2159-2268` | ✅ STATUS command implemented |
| **Cancellation** | `services/stripe_service.py` | ✅ Feedback collection done, win-back pending |
| **CS Portal** | `cs_portal.py` | ✅ Complete (tickets, assignment, SLA, export) |
| **Data Export** | `services/export_service.py`, `main.py` | ✅ EXPORT command, pre-delete suggestion |

---

## 🚀 Launch Readiness Summary

**All critical UX blockers resolved as of Feb 9, 2026.**

### ✅ Launch-Ready Features:
- Trial expiration warnings (no more silent downgrades)
- Clear welcome message explaining value proposition
- Usage visibility for free tier users
- Account status overview (STATUS command)
- Cancellation feedback collection
- Data export before deletion

### ⏳ Post-Launch Priorities:
1. Mid-trial value reminder (day 7 engagement)
2. Win-back campaign (30 days post-cancellation)
3. Progress indicators in onboarding
4. Upgrade link fallbacks
5. Conversational tone polish

**Recommendation:** Ready for public launch. Remaining items are optimizations, not blockers.
