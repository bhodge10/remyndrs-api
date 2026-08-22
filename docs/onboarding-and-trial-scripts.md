# Remyndrs — Onboarding & 0–14 Day Premium Trial Scripts

This document captures the **actual SMS copy** sent to users during onboarding
and across the 14-day Premium trial, transcribed from the codebase.

**Source of truth:**
- Onboarding — `services/onboarding_service.py`
- Inline trial info — `services/trial_messaging_service.py`
- Day-by-day lifecycle — `tasks/reminder_tasks.py`
- Schedule — `celery_config.py`

**Key facts:**
- Trial length: **14 days** of full Premium (`FREE_TRIAL_DAYS = 14`), auto-enabled at signup. No credit card required.
- Pricing referenced in copy: **Premium $8.99/mo or $89.99/yr** (~$7.50/mo).
- Free plan after trial: **2 reminders/day**, 5 lists, 5 memories (grandfathered v1 limits referenced in trial copy).
- All lifecycle messages are **timezone-aware** and only send at **9–10 AM in the user's local time**, staggered across the hour.
- `{first_name}` and usage-stat blocks are filled in per user; where a user has no first name, a generic greeting is used.

---

## Part 1 — Onboarding (Day 0)

A 3-step flow: **Welcome → First Name → ZIP Code → Done.**

### Step 0 — Welcome (asks for first name)

**Standard:**
```
Welcome to Remyndrs! 👋

I'm your AI-powered reminder assistant. I'll help you remember anything—from daily tasks to important dates.

No app needed - just text me naturally and I'll handle the rest!

Let's get you set up in 30 seconds. What's your first name?
```

**Variant — invited via a shared list** (`{owner_name}`, `{list_name}` filled in):
```
{owner_name} shared '{list_name}' with you on Remyndrs!

Let's get you set up in 30 seconds so you can see it. What's your first name?
```

### Step 1 — First name captured (asks for ZIP)

**Single first name given:**
```
Nice to meet you, {first_name}!

Last question: ZIP code?

(This helps me send reminders at the right time in your timezone)
```

**Full name given (two words → stores both, same next step):**
```
Nice to meet you, {first_name} {last_name}!

Last question: ZIP code?

(This helps me send reminders at the right time in your timezone)
```

### Step 2 — ZIP captured → onboarding complete

**Standard completion** (`{trial_end_str}` = trial end date, e.g. "September 05"; `{first_memory}` = auto-saved signup memory):
```
Perfect! You're all set, {first_name}! 🎉

You have full Premium access until {trial_end_str} — unlimited reminders, lists & memories. After that, the core service is free forever — no credit card ever needed.

I just saved your first memory: "{first_memory}"

Keep an eye out for a quick morning tip over the next week or so — I'll show you what else I can do.

Try asking me: "What do I have saved?"
```

**Completion — joined via shared list** (`{shared_note}` lists the accepted list(s)):
```
You're all set, {first_name}! 🎉

You have full Premium access until {trial_end_str} — unlimited reminders, lists & memories. After that, the core service is free forever — no credit card ever needed.{shared_note}

Keep an eye out for a quick morning tip over the next week or so — I'll show you what else I can do.
```
> `{shared_note}` example: `You now have access to 'Groceries'! Text 'Show Groceries' to see it.`

### Post-completion automated sends

**VCF contact card** — sent **1 hour** after completion (with a `.vcf` attachment):
```
📱 Tap to save Remyndrs to your contacts!

Tip: Pin this conversation to keep me at the top of your texts — that way I'm always one tap away when you need to remember something!
```

**5-minute engagement nudge** — sent **5 minutes** after completion, **only if the user hasn't sent 2+ messages** since onboarding:
```
Quick question: What's something you always forget?

(I'm really good at remembering it for you 😊)
```

---

### Onboarding edge-case replies

These fire when a user says something other than the expected answer mid-setup.

**"help" or "?"** (`{step}` = current step, `{prompt}` = that step's question):
```
I'm helping you set up your account! It's quick - just 2 questions total.

You're currently on step {step} of 2:
{prompt}

Why I need this info:
• Name: Personalize your experience
• ZIP: Set your timezone for accurate reminders

Text "cancel" to cancel setup, or just answer the question to continue!
```

**Pricing question during onboarding** (cost/price/free/etc.):
```
Great question! You get a FREE 14-day Premium trial to start. After that, it's $8.99/mo for Premium or a free tier with 2 reminders/day.

Let's finish setup first - {current_prompt}
```

**"cancel" / "nevermind" / "quit":**
```
No problem! Setup cancelled.

If you change your mind, just text me again and we'll start fresh.

Have a great day! 👋
```

**"restart":**
```
No problem{, {first_name}}! Let's start over.

What's your first name?
```

**"skip" at the ZIP step:**
```
I totally get it! But here's why I need it:

Without your ZIP code, I can't figure out your timezone. That means reminders might arrive at the wrong time (imagine getting a 2pm reminder at 5am 😬).

Your 5-digit ZIP code helps me send reminders when YOU need them.

What's your ZIP code?
```

**Tried to use the service before finishing** (e.g. "remind me…" at step 1):
```
⚠️ Almost there! Please finish setup first.

You're on step {step} of 2 - just {remaining} more question(s)!

{prompt}
```

**Email entered where a name was expected (step 1):**
```
That looks like an email! What's your first name?
```

**"START" / "begin" again at step 1:**
```
You're already in setup! Let's continue.

What's your first name?
```

#### ZIP validation errors (step 2)

**International postal code:**
```
I recognize that's an international postal code!

Currently, Remyndrs only supports US ZIP codes for timezone detection.

If you're outside the US, you can enter a US ZIP code that matches your timezone:
- Eastern Time: 10001 (New York)
- Central Time: 60601 (Chicago)
- Mountain Time: 80202 (Denver)
- Pacific Time: 90001 (Los Angeles)

What ZIP code should I use?
```

**Looks like a city name / address:**
```
Hmm, that looks like a city name or address!

I just need the 5-digit ZIP code (like 45202).

What's your ZIP code?
```

**Wrong length (with digits entered):**
```
ZIP codes are exactly 5 digits!

You entered {n} digit(s). Try again?

What's your 5-digit ZIP code?
```

**Wrong length (no digits):**
```
Please enter a valid 5-digit ZIP code (like 45202):
```

---

### Inline trial-info footer (first qualifying action)

Appended **once**, the first time a newly-onboarded user performs a save or
retrieval action (from `trial_messaging_service.py`). Two variants:

**After a save/reminder/list action:**
```
---

You're on a FREE 14-day Premium trial!

After that, choose:
- Premium: $8.99/mo (unlimited everything)
- Free: 2 reminders/day (still useful!)

For now, just use me naturally. Text 'help' anytime!
```

**After a show/retrieval action:** identical, except the last line is:
```
For now, just explore! Text 'help' anytime.
```

---

## Part 2 — 0–14 Day Premium Trial Lifecycle

Automated messages across the trial. All timezone-aware (9–10 AM local),
each sent once per user, gated so opted-out / lifecycle-paused / already-upgraded
users are skipped. Some messages branch on the user's activity or are suppressed
by anti-bunching (skip if another proactive message went out in the last 48h).

| Day | Message | Task | Notes |
|-----|---------|------|-------|
| 0 | Onboarding completion + VCF (+1h) + engagement nudge (+5m) | `handle_onboarding` | See Part 1 |
| 1 | Morning nudge | `send_day_1_morning_nudge` | Everyone; anti-bunch |
| 2 | Feature prompt (A or B) | `send_day_2_feature_prompt` | Branches on activity |
| 3 | Engagement / feature discovery | `send_day_3_engagement_nudges` | |
| 3–4 | Email collection | `send_day_4_email_collection` | Only if no email on file; anti-bunch |
| 7 | Trial warning + value recap | `check_trial_expirations` | Merged w/ mid-trial value msg |
| 7 | Mid-trial value recap (fallback) | `send_mid_trial_value_reminders` | Only if 7d warning didn't send |
| 13 | Urgent "1 day left" warning | `check_trial_expirations` | |
| 14 | Trial-expired downgrade notice | `check_trial_expirations` | Downgrades to free |

> Post-trial messages (Day 17 re-engagement, Day 28 feature-loss touchpoint,
> Day 44 win-back) fall outside the 0–14 window and are not included here.

---

### Day 1 — Morning nudge
`send_day_1_morning_nudge` (`{greeting}` = "Good morning {first_name}!" or "Good morning!")
```
{greeting} 👋 Quick idea — text me something you need to remember today. A grocery item, a reminder, anything at all. I'll keep it safe for you.
```

### Day 2 — Feature prompt (branches on activity)
`send_day_2_feature_prompt` (`{greeting}` = "Hey {first_name}" or "Hey there")

**Version A — user has created 0 reminders:**
```
{greeting} — try this: text me "Remind me at 5pm to take the chicken out of the freezer" (or whatever you actually need to remember today). I'll text you right at 5. 🍗
```

**Version B — has reminders but 0 memories:**
```
{greeting} — did you know I can save things too? Try texting me something like "My Netflix password is StarLight99" or "Mom's birthday is April 12." I'll remember it so you don't have to. 🧠
```

**If the user already has both reminders and memories:** skipped (no message).

### Day 3 — Engagement / feature discovery
`send_day_3_engagement_nudges` (`{greeting}` = "Hey {first_name}!" or "Hey there!")
```
{greeting} You've been on Remyndrs for 3 days now.

Have you tried these yet?
• Save a memory: "Remember my WiFi is ABC123"
• Create a list: "Start a grocery list"
• Set a recurring reminder: "Remind me every Monday at 9am to submit my timesheet"

Just text me naturally — I'll figure out what you need!
```

### Day 3–4 — Email collection
`send_day_4_email_collection` — only sent to users with **no email on file**.
```
{greeting} Quick question — what's your email address?

I only need it for account recovery (in case you get a new phone number).

No spam, no marketing — just a safety net for your data.

(Reply with your email or text SKIP if you'd rather not)
```

### Day 7 — Trial warning + value recap (combined)
`check_trial_expirations` (7-day branch). Personalized with the user's usage
stats. `{greeting}` = "Hi {first_name}!" or "Hi there!"; `{stats_block}` is
included only if the user has activity.
```
{greeting} You have 7 days left in your Premium trial! ⏰{stats_block}

After your trial, you'll move to the free plan (2 reminders/day).

Text UPGRADE to keep unlimited reminders — $8.99/mo or $89.99/yr ($7.50/mo).
```
`{stats_block}` example:
```

So far you've:
  ✓ 5 reminders created
  ✓ 2 lists organized
  ✓ 3 memories saved
```

**Fallback — mid-trial value recap** (`send_mid_trial_value_reminders`, only
sent if the Day 7 warning above did **not** go out, to avoid double-messaging):
```
{greeting} You're halfway through your Premium trial! 🎉

So far you've:
✓ {N} reminders created
✓ {N} lists organized
✓ {N} memories saved

Your trial ends in 7 days. After that, you'll move to the free plan (2 reminders/day).

Want to keep unlimited access? Text UPGRADE anytime!
```

### Day 13 — Urgent "1 day left" warning
`check_trial_expirations` (1-day branch). `{stats_line}` included only if user has activity.
```
Tomorrow is your last day of Premium trial! ⏰{stats_line}

After that, you'll be on the free plan (2 reminders/day).

Text UPGRADE now — $8.99/mo or $89.99/yr ($7.50/mo).
```
`{stats_line}` example: ` You've created 5 reminders, 2 lists, 3 memories so far.`

### Day 14 — Trial expired (downgrade notice)
`check_trial_expirations` (expired branch). Also downgrades the account to free.
```
Your Premium trial has ended. You're now on the free plan:
• 2 reminders/day
• 5 lists, 5 memories
• Existing recurring reminders keep working, but you can't create new ones

All your data is safe!

Want unlimited access back? Text UPGRADE — $8.99/mo or $89.99/yr ($7.50/mo).
```

---

*Generated from the codebase. If message copy changes in the source files above,
regenerate this doc to keep it in sync.*
