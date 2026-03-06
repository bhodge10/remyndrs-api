# Changelog — Recent Improvements & Bug Fixes

## Shortened Onboarding + Day 4 Email Collection (Mar 2026)
Reduced onboarding from 4-5 steps to 3 (Welcome → First Name → ZIP Code) to reduce drop-off. Email is now collected on Day 4 via a Celery task instead of during onboarding.

**Onboarding changes:**
- Removed last name (step 2) and email (step 3) from onboarding flow.
- Step 1 (first name) now goes directly to ZIP code. Full name shortcut (two words) stores both names and proceeds to ZIP.
- Updated help text, step counts ("2 questions total"), and welcome message ("set up in 30 seconds").
- Removed `get_email_error_message()` from onboarding_service (email validation still exists for Day 4 collection).

**Day 4 email collection:**
- New Celery task `send_day_4_email_collection` targets users on Day 3-4 of trial who have no email set.
- Runs hourly at :12 (between Day 3 nudge at :10 and post-trial at :15). Timezone-aware: 9-10 AM local.
- Sets `awaiting_email_collection = TRUE` on user when sending.
- Response handler in `main.py` checks `awaiting_email_collection` flag before smart nudge handling.
  - "skip"/"no"/"pass" → clears flag, suggests texting EMAIL later.
  - Valid email (contains @ and .) → validates, saves, clears flag.
  - Anything else → clears flag, processes normally (no trapping).

**New columns:** `day_4_email_sent BOOLEAN DEFAULT FALSE`, `awaiting_email_collection BOOLEAN DEFAULT FALSE` on `users` table.

**Files modified:** `services/onboarding_service.py`, `utils/formatting.py`, `services/onboarding_recovery_service.py`, `database.py`, `tasks/reminder_tasks.py`, `celery_config.py`, `main.py`, `models/user.py`, `tests/conftest.py`, `tests/test_onboarding.py`, `CLAUDE.md`.

## Beta Tester Reward: 3 Months Free Premium (Mar 2026)
Granted all 31 pre-launch beta testers (signed up before March 1, 2026) three months of free Premium tier through June 1, 2026. Applied via temporary admin endpoints that were added, used, and removed in PRs #190–#192.

**What changed:** `premium_status` set to `premium`, `premium_since` set to current timestamp (preserved if already set), `trial_end_date` extended to `2026-06-01`. No Stripe fields modified — users will naturally revert to free tier after June 1 unless they subscribe.

**How it works:** `get_user_tier()` in `tier_service.py` returns `'premium'` whenever `trial_end_date > now`, so extending the trial end date is sufficient to grant premium access without Stripe involvement.

## Contact Message Reply from Admin Dashboard (Feb 2026)
Admins can now reply to contact messages (feedback, bug reports, questions) directly via SMS without creating a support ticket. Adds an inline reply button to each message card in the Contact Messages section.

**New column:** `admin_reply TEXT` on `contact_messages` table (with ALTER TABLE migration for existing databases).

**New function:** `reply_to_contact_message()` in `services/support_service.py` — looks up phone number by message ID, sends SMS formatted as `[Remyndrs]\n\n{message}`, stores the reply text. Does not auto-resolve; admin resolves separately.

**New endpoint:** `POST /admin/contact-messages/{message_id}/reply` — accepts `{ message: str }`, validates, sends SMS, stores reply.

**Dashboard UI:** Blue "Reply" button on each contact message card expands an inline text input with Send/Cancel. After sending, the reply is displayed in a green-highlighted box below the message. Button changes to "Reply Again" if a reply already exists. `get_contact_messages()` updated to include `admin_reply` in query and response.

## Daily Twilio Cost Polling (Feb 2026)
The admin dashboard Cost Analytics section previously estimated SMS costs using a flat rate (`message_count * 2 * $0.0079`), which didn't account for carrier surcharges, multi-segment messages, or price changes. Added a daily Celery task that polls Twilio's Usage Records API for actual costs.

**New table:** `twilio_costs` (cost_date UNIQUE, inbound_count, inbound_cost, outbound_count, outbound_cost, total_cost, created_at)

**New task:** `poll_twilio_costs` in `tasks/twilio_tasks.py` — runs daily at 6:30 AM UTC via Celery Beat. Fetches yesterday's `sms-inbound` and `sms-outbound` usage records from Twilio, upserts into `twilio_costs` using `ON CONFLICT DO UPDATE` for idempotency. Skips in test environment; handles negative Twilio price values with `abs()`.

**Metrics:** `get_twilio_actual_costs()` in `services/metrics_service.py` queries day/week/month summaries. `get_cost_analytics()` now includes a `twilio_actual` key alongside existing per-plan estimates.

**Dashboard UI:** "Actual vs Estimated" summary below the cost table shows estimated SMS cost, actual Twilio cost, and color-coded difference (green if actual <= estimated, red if higher). Includes inbound/outbound message count and cost breakdown. Shows "No Twilio data yet" before the first poll runs.

## Auto-Clear Stale Opted-Out Flags & Admin Manual Clear (Feb 2026)
Users who texted STOP and later re-engaged could have a stale `opted_out` database flag, causing them to be silently excluded from broadcasts even though Twilio was no longer blocking them. Added automatic and manual fixes.

**Auto-clear:** When any user texts in (past the STOP keyword handler), if their `opted_out` flag is `TRUE`, it's automatically cleared. Uses the existing `get_user()` result — `opted_out` added to `USER_COLUMNS` at index 21 — so no extra DB query on normal requests. The UPDATE only fires for the rare stale case.

**Admin manual clear:** Customer detail profile (`/admin/cs/customer/{phone}`) now shows a red "Opted Out" banner with date when the flag is set. A "Clear Flag" button calls `POST /admin/cs/customer/{phone}/clear-opted-out`, logs a CS note, and refreshes the profile. Also added a "Clear" button next to each opted-out user in the broadcast recipients preview excluded list — clears the flag and refreshes the preview inline.

**Why both?** Broadcasts are outbound-only and don't trigger the auto-clear. The manual clear covers users who haven't texted recently.

## Broadcast Recipients Preview & Opted-Out Count Fix (Feb 2026)
The broadcast stats endpoint included opted-out users in its counts, making the preview numbers misleading — the actual send logic correctly filtered them out. Fixed the stats query and added a full recipients preview so admins can see exactly who will receive a broadcast before sending.

**Bug fix:** `/admin/broadcast/stats` now excludes opted-out users (`AND (opted_out = FALSE OR opted_out IS NULL)`), matching the send queries in lines 608-627.

**New endpoint:** `GET /admin/broadcast/recipients-preview?audience=all|free|premium` — returns included/excluded user lists with masked phones, decrypted names, tiers, timezones, current local times, and exclusion reasons (`opted_out`, `outside_window`). Summary counts included.

**UI changes:**
- "Preview Recipients" button in broadcast preview box (hidden for single-number mode)
- Collapsible panel with color-coded included (green) and excluded (red) tables
- Tier badges (blue/gold) and reason badges (red "Opted Out", orange "Outside Window")
- Panel resets when audience selection changes
- Confirmation modal shows yellow excluded-users warning with breakdown when preview data is available

## Contact Messages — Separate from Support Tickets (Feb 2026)
FEEDBACK, BUG, and web question/feedback/bug submissions no longer create support tickets. They go to a new lightweight `contact_messages` table instead. Only SUPPORT requests create tracked tickets in `support_tickets`.

**New table:** `contact_messages` (id, phone_number, message, category, source, resolved, created_at)

**New functions in `services/support_service.py`:** `save_contact_message()`, `get_contact_messages()`, `toggle_contact_message_resolved()`

**Dashboard changes:**
- Admin dashboard: new "Contact Messages" collapsible section with category filter and resolve toggle
- CS portal: new "Contact Messages" tab with badge count, category filter, resolve/unresolve actions
- Legacy Feedback tab relabeled in CS portal

**Routing changes:**
- SMS FEEDBACK/BUG handlers → `save_contact_message()` (was `create_categorized_ticket()`)
- `/api/contact` endpoint → `save_contact_message()` for feedback/bug/question, `create_categorized_ticket()` only for support
- Email notifications still sent for all types via `send_feedback_notification()`

## Progressive Education for Tier Limits (Feb 2026)
Complete overhaul of tier limit messaging to improve free-to-premium conversion. Replaced confusing messages like "Added 0 items... (7 items skipped - list full)" with clear WHY-WHAT-HOW explanations. Implemented Education Pyramid (Levels 1-4) with progressive counters, warnings, and educational blocking. Enhanced STATUS command to show tier comparison. All functionality tested with 13 new unit tests. See `PROGRESSIVE_EDUCATION_IMPLEMENTATION.md` for details.

## Daily Summary Interactive Flow Removed (Feb 2026)
The `daily_summary_setup` interactive flow trapped users in an inescapable loop. Replaced with a one-shot tip shown after the user's first reminder: "Text SUMMARY ON to enable it!" Deleted ~380 lines from `services/first_action_service.py`. Daily summary tip only triggers on reminder actions, not on memory/list actions.

## Context Loss Fix (Feb 2026)
List selection by number (e.g., "1") was intercepted by daily summary handler asking "1 AM or 1 PM?". Fixed by removing the interactive daily summary flow entirely. New monitoring detectors (`context_loss`, `flow_violation`) prevent similar issues.

## Broadcast System Improvements (Feb 2026)
Three fixes to the admin broadcast system (`admin_dashboard.py`):

1. **Single Number Test Mode:** New "Single Number (Test)" audience option sends to one phone number instead of all users.
2. **Scheduled Broadcast Reliability:** Fixed daemon thread crash and missing broadcast history entries.
3. **Message Viewer:** Broadcast History messages now clickable to open a modal showing full text.

**DB changes:** Added `target_phone TEXT` to `scheduled_broadcasts`, `source TEXT DEFAULT 'immediate'` to `broadcast_logs`.

## Delete Account Duplicate Message Fix (Feb 2026)
When a premium user texted YES DELETE ACCOUNT, they received two conflicting messages. Fixed by adding a check in `handle_subscription_cancelled()` (`services/stripe_service.py`) that skips the downgrade notice when the user has `pending_delete_account` or `opted_out` set.

## List Creation With Items Fix (Feb 2026)
When a user texted "Create a grocery list" with items on separate lines, the system only created the list and silently discarded all items. Fix: updated the AI prompt to instruct using `add_to_list` instead of `create_list` when items are present.

## Custom Interval Recurring Reminder Rejection (Feb 2026)
"Every N days/weeks/months" patterns were not recognized as recurring reminders. Updated the AI prompt to explicitly catch custom intervals and return a helpful message suggesting supported alternatives. Supported recurrence types: `daily`, `weekly`, `weekdays`, `weekends`, `monthly`.

## Recurring Reminder Day Context Fix (Feb 2026)
When a user created a monthly or weekly recurring reminder without specifying the day, the system asked "which day?" but didn't store pending state. Fix: store a `needs_recurrence_day` pending confirmation in `main.py` before asking the clarification question. Added a handler that parses the user's response to extract day with optional time override.

## Multi-Day Reminder Error Fix (Feb 2026)
"Remind me every day for the next five days at 5 PM..." caused an error. Two root causes: (1) conflicting AI prompt instructions; (2) `OPENAI_MAX_TOKENS=300` was too low for 5 reminder JSON objects. Fix: added exception to PRIORITY CHECK for finite periods, increased `OPENAI_MAX_TOKENS` to 800, added `finish_reason=length` truncation detection with retry. Tests: `TestMultiDayReminders` in `tests/test_reminders.py`.

## Clarify Time O'Clock and Date Context Fix (Feb 2026)
"Remind me Sunday at 1 o'clock..." → user replies "Pm" → crashed. Two bugs: (1) parser did `int("1 O'CLOCK")`; (2) the date ("Sunday") was lost. Fix: strip o'clock from time string, added optional `reminder_date` field to `clarify_time` JSON, stores and retrieves `pending_reminder_date`. Tests: `TestClarifyTimeOclock` in `tests/test_reminders.py`.

## Desktop Signup Flow (Feb 2026)
Added `POST /api/signup` endpoint for desktop visitors. Phone validation, E.164 formatting, sends welcome SMS. Frontend form on remyndrs.com with responsive design.

## Smart Nudges: Proactive AI Intelligence Layer (Feb 2026)
Transforms Remyndrs from a reactive command processor into a proactive personal assistant. Sends ONE intelligent, contextual insight per day by analyzing all stored user data.

**8 Nudge Types:** date_extraction, reminder_followup, cross_reference, stale_list, pattern_recognition, weekly_reflection, memory_anniversary, upcoming_preparation.

**Tier Strategy:**
- Premium/Trial: Daily nudges (all 8 types)
- Free: Weekly reflection only (Sundays)

**User Controls:** `NUDGE ON`, `NUDGE OFF`, `NUDGE TIME 9AM`, `NUDGE STATUS`. Responses: `YES`, `DONE`, `SNOOZE`, `NO`, `STOP`.

**Soft-Launch:** OFF by default. Auto-enable for trial users prepared but commented out in `services/onboarding_service.py` and `services/first_action_service.py`.

**Implementation:**
- **Core engine:** `services/nudge_service.py` - data gathering, AI prompt, generation, tier gating, response handling
- **Celery task:** `tasks/reminder_tasks.py` `send_smart_nudges` - timezone-aware, atomic claiming
- **Beat schedule:** `celery_config.py` - runs every minute
- **Keyword handlers:** `main.py` - NUDGE ON/OFF/TIME/STATUS, response handlers, UNDO support
- **User model:** `models/user.py` - `get_users_due_for_smart_nudge()`, `claim_user_for_smart_nudge()`, `get_smart_nudge_settings()`, `get_pending_nudge_response()`

**DB changes:** New `smart_nudges` table. New user columns: `smart_nudges_enabled`, `smart_nudge_time`, `smart_nudge_last_sent`, `pending_nudge_response`.

**Key design:** Auto-clear pending nudge response if user sends a real command (>3 chars). STOP reply disables nudges. AI can return `nudge_type: "none"`. Confidence threshold of 50. All nudge texts under 280 chars.

**Testing:** `tests/test_smart_nudges.py`

## Memory Upsert — Duplicate Detection (Feb 2026)
`save_memory()` in `models/memory.py` now detects duplicate memories before inserting. Uses Jaccard keyword similarity (stop words filtered, 60% threshold). Returns `bool` — `True` if updated, `False` if new insert.

**Key details:**
- `_SIMILARITY_THRESHOLD = 0.6` for standard memories, `_SHORT_MEMORY_THRESHOLD = 0.4` for short memories (≤4 keywords)
- Encryption-aware: uses `phone_hash` lookup with plaintext fallback

## Pricing Update — $8.99/mo, $89.99/yr (Feb 2026)
Premium pricing changed from $6.99/mo to $8.99/mo ($89.99/yr, ~17% discount). Constants `PREMIUM_MONTHLY_PRICE` and `PREMIUM_ANNUAL_PRICE` in `config.py` are used everywhere. Stripe cents values updated in `PRICING` dict (899/8999).

## Expanded HELP Command (Feb 2026)
`get_help_text()` in `utils/formatting.py` expanded to a comprehensive guide. Points to remyndrs.com/commands for full guide.

## Snooze Duration in Fire Messages (Feb 2026)
Reminder fire messages now say "(Reply SNOOZE to snooze 15 min)" to set expectations on the default duration.

## Annual Pricing in UPGRADE Flow (Feb 2026)
UPGRADE command now generates two Stripe checkout links — monthly and annual. Both links expire in 24 hours.

## MY REMINDERS Fallback in AI Handlers (Feb 2026)
When AI `delete_reminder` or `update_reminder` handlers can't find a match, the error message now includes "Text MY REMINDERS to see your list".

## Personalized Trial Warnings (Feb 2026)
Day 7 and Day 1 trial warnings now include the user's actual usage stats (reminder/list/memory counts).

## 30-Day Post-Trial Win-Back (Feb 2026)
New `send_30d_winback` Celery task sends a re-engagement SMS 30 days after trial expiry. Tracks with `winback_30d_sent` column. Runs daily via Celery Beat.

## MORE COMMANDS Keyword (Feb 2026)
Added `MORE COMMANDS` (also `MORE`, `ALL COMMANDS`, `FULL COMMANDS`) keyword handler. Returns `get_extended_help_text()` from `utils/formatting.py`.

## Trial Expiration Recurring Reminder Fate (Feb 2026)
Day 0 message now includes "Existing recurring reminders keep working, but you can't create new ones".

## Effective Monthly Rate in Annual Pricing (Feb 2026)
All annual pricing references changed from "(save $18)" to "($7.50/mo)".

## Rate Limit Cooldown Duration (Feb 2026)
Rate limit message changed from "wait a moment" to "wait about 30 seconds".

## Snooze 24-Hour Cap Communication (Feb 2026)
When a snooze exceeds 24 hours and gets capped, the confirmation now includes "(max snooze is 24 hours)".

## Day 7 Trial Double-Message Deduplication (Feb 2026)
Fixed duplicate Day 7 messages by merging trial warning + value reminder into a single message.

## Multi-Line List Item Adds (Feb 2026)
`parse_list_items()` in `services/ai_service.py` now handles newline-separated items.

## Expanded UNDO for All Actions (Feb 2026)
UNDO handler now checks the most recent action across reminders, list items, and memories.

## Smarter AI Recurrence Fallback (Feb 2026)
AI prompt now suggests the closest supported alternative for unsupported recurrence intervals.

## 14-Day Post-Trial Touchpoint (Feb 2026)
New `send_14d_post_trial_touchpoint` Celery task. Fills the gap between Day 3 re-engagement and Day 30 win-back.

## Trial Lifecycle Timeline
The complete post-onboarding trial lifecycle message schedule (all timezone-aware, sends at 9-10 AM user's local time):
- **Day 3:** Engagement nudge (`send_day_3_engagement_nudges`, hourly at :10)
- **Day 7:** Combined trial warning + value reminder (`check_trial_expirations`, hourly at :00)
- **Day 13 (1d left):** Urgent trial warning (`check_trial_expirations`, hourly at :00)
- **Day 14 (expired):** Downgrade notice (`check_trial_expirations`, hourly at :00)
- **Day 17 (3d post):** Re-engagement (`send_post_trial_reengagement`, hourly at :15)
- **Day 28 (14d post):** Feature-loss touchpoint (`send_14d_post_trial_touchpoint`, hourly at :20)
- **Day 44 (30d post):** Win-back (`send_30d_winback`, hourly at :25)

## Mobile Optimization Pass — Website (Feb 2026)
Comprehensive mobile optimization across all 5 website HTML files. Changes made in the Remyndrs-Website repo. Key fixes: hamburger tap target (44x44px), conversation popup responsive width, footer/nav link tap targets, animation performance (`will-change`), hamburger accessibility.

## Demo Video — Website (Feb 2026)
Added a 30-second animated demo video to `index.html`.

## Timezone-Aware Trial Lifecycle Messages (Feb 2026)
Beta user reported repeated 4 AM trial messages. Three root causes:

1. **NULL flag columns caused repeated sends:** Fix: `COALESCE(flag, FALSE)` + NULL→FALSE backfill migration.
2. **Silent error swallowing:** Fix: direct SQL updates instead of `create_or_update_user()`.
3. **Fixed UTC schedule ignored user timezones:** Fix: hourly execution with per-user timezone checks (9-10 AM local).

**Schedule in `celery_config.py`:** Tasks staggered at :00, :05, :10, :15, :20, :25 past each hour.

## Cancel Not Working During NEEDS_TIME Pending State (Feb 2026)
Two bugs fixed (PR #154):
1. "Cancel" intercepted by UNDO handler during NEEDS_TIME flow. Fixed by removing the `!= "NEEDS_TIME"` exclusion.
2. New intent blocked by NEEDS_TIME. Added `is_new_intent` detection that auto-cancels the pending time state.

## Test Suite Fixes — 3 Failing Tests (Feb 2026)
Suite at 112 passed, 0 failed (PR #156). Fixed Celery eager mode in conftest, free-tier limits blocking multi-day tests, and `save_reminder()` returning `None` instead of ID.

## Combined Daily Summary + Smart Nudges (Feb 2026)
Users with both features enabled were receiving TWO morning messages. Now the nudge task takes over entirely for nudge-enabled users: prepends today's reminders (compact format) above the AI insight.

**Behavior matrix:**
- **Nudge + reminders:** Combined message — compact reminder list + AI nudge
- **Nudge only:** Just the AI nudge text
- **No nudge but reminders:** Compact summary sent as fallback
- **Neither:** Nothing sent

**Implementation:** `format_compact_summary()` in `tasks/reminder_tasks.py`, `COMBINED_NUDGE_MAX_CHARS = 1500` in `config.py`.
