# Remyndrs API — Codebase Review (Aug 2026)

Reviewed `origin/main` at `aa45524` (NFL morning-after score plumbing). Read the live tree, not the README. Recent merges (PRs 278/279/280) were checked as leftovers, not re-litigated.

This is an opinion, not a cleanup PR. No kitchen-sink fixes. One data-layer landmine is called out first because it can silently undo the v2 free plan on every process start.

---

## Overall opinion

The product path that users actually hit — inbound SMS → keyword traps → AI → Postgres → Celery reminder send — is more mature than the repo looks. Reminder claiming (`SELECT FOR UPDATE SKIP LOCKED` + row lock through mark-as-sent), Twilio 21610 opt-out, staging fallback, email reminder fallback, and the 278/279 delete/show fixes are real production thinking. Security rounds 4–6 left fingerprints that still hold: parameterized SQL, Basic Auth with timing-safe compare and lockout, Stripe signature verification.

The problem is not “nobody cares.” It is that **live behavior lives in two 7k/11k-line files (`main.py`, `admin_dashboard.py`) while a parallel handler layer (`routes/handlers/`) exists and is almost unused**. Fixes land in `main.py`. Tests sometimes import the unwired copy. Docs (README, CLAUDE trial timeline, `run_tests.py` flags) describe a smaller, older app. That split will keep producing 278/279-class bugs: the numbered-list UX is correct in one path and wrong in the sibling path.

Do not spend the next engineering cycle wiring issue #17 or rewriting `main.py`. Spend it on the five items below. They are the ones that can steal a reminder, apply the wrong free-tier, delete the wrong list, or ship untested.

---

## Top bugs / risks

### 1. Critical — v2 free tier is reset to v1 on every `init_db()`

`database.py` `init_db()` re-runs a long `migrations` list on **every** API and worker start (`main.py:380`, `worker.py` `setup_db`). There is no applied-migration table. Most entries are `ALTER … IF NOT EXISTS` (safe). This one is not:

```712:714:database.py
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS free_tier_version INTEGER DEFAULT 2",
            # Backfill all existing users to v1 (grandfathered)
            "UPDATE users SET free_tier_version = 1 WHERE free_tier_version = 2 OR free_tier_version IS NULL",
```

New users inherit column default **2**. `create_or_update_user()` does not set the column. The next web/worker boot converts every `2` back to `1`. Enforcement in `services/tier_service.py` (`get_user_free_tier_version`, `can_create_reminder`) then treats those users as grandfathered **2 reminders/day** instead of **3/week**.

**Fix:** delete that `UPDATE` from the list (the backfill already ran). Do not “make it safer” by adding more runtime DML. Confirm in prod: `SELECT free_tier_version, count(*) FROM users GROUP BY 1`.

### 2. High — `Delete N` after `MY LISTS` ignores shared lists

`SHOW LISTS` / `MY LISTS` numbers **owned + shared** via `format_all_lists_display` → `get_all_lists_with_shared()` (`routes/handlers/lists.py:1108–1133`, `main.py:4270+`).

`Delete N` with `last_active_list == "__LISTS__"` uses **owned only**:

```2694:2698:main.py
            if last_active == "__LISTS__":
                all_lists = get_lists(phone_number)
                if all_lists and 1 <= item_num <= len(all_lists):
                    lst = all_lists[item_num - 1]
```

Same owned-only shortcut for **bare digits** opening a list (`main.py:2429–2432`, `get_lists`) *before* the shared-aware picker at `main.py:4331`. Owned lists are prepended in `get_all_lists_with_shared`, so `1`/`2` often match; a shared list as row 3 does not.

PRs 278/279 fixed “Show 3 opens a reminder delete” and “YES on a list item no-ops.” They did not make delete numbering share the same source as display. No test covers `__LISTS__` + a shared row.

**Fix:** one helper for “lists as the user last saw them”; use it in display, picker, and `Delete N`.

### 3. High — single-option delete menu: prompt says YES-ish, code only accepts `1`

When disambiguation finds one candidate, state is stored as a **JSON array**, and the prompt says “Reply **1**”:

```2835:2840:main.py
            if len(delete_options) == 1:
                opt = delete_options[0]
                create_or_update_user(phone_number, pending_reminder_delete=json.dumps(delete_options))
                resp.message(f"Delete {opt['display']}?\n\nReply 1 to confirm or CANCEL to cancel.")
```

The YES branch only runs for a **dict** with `awaiting_confirmation` (`main.py:2066`). `YES` hits the “new intent” clearer (`main.py:2177–2184`) and **cancels the delete**. Context-aware deletes (viewing a list, `__REMINDERS__`, etc.) correctly use the dict + YES. The fallback menu does not.

**Fix:** store the same `awaiting_confirmation` dict as the other paths, or accept YES when the array has length 1.

### 4. High — recurring generation fail-open + no unique constraint

```938:961:models/reminder.py
def check_reminder_exists_for_recurring(recurring_id: int, target_date: date) -> bool:
    ...
    except Exception as e:
        logger.error(f"Error checking recurring reminder existence: {e}")
        return False
```

On DB error this returns “doesn’t exist” → `generate_recurring_reminders` inserts another row. There is no `UNIQUE (recurring_id, DATE(reminder_date))`. Admin already has `cleanup_duplicate_reminders` (`main.py` ~6857), which is a confession this happened.

Related delivery risk: `send_single_reminder` (`tasks/reminder_tasks.py:223–244`) will not Celery-retry after a successful SMS if `sent=TRUE` fails — good — but the row stays unsent and is reclaimed after 5 minutes (`claim_due_reminders` stale window). Duplicate reminder SMS is still possible on a mark-as-sent outage.

**Fix:** return `True` (skip) on check error; add the unique constraint; alert on the existing `[CRITICAL] SMS sent but could not mark as sent` log.

### 5. Medium — in-memory Twilio dedup and rate limits (wrong the moment web scales)

```301:302:main.py
rate_limit_store = defaultdict(list)
_processed_message_sids = {}
```

`sms_reply` dedups `MessageSid` in process memory (`main.py:427–442`) and rate-limits per phone the same way. Render is currently one web instance; the code assumes that. Twilio retries to a second instance would double-process (create a reminder twice, etc.). Same for auth lockout (`utils/auth.py`) and IP limits on `/api/signup`. Upstash Redis is already the Celery broker.

Staging skips Twilio signature validation (`main.py:405–408`) so fallback URLs work. Anyone who can POST the staging URL can inject SMS. Fine if ingress is locked; not fine if the URL is guessable.

---

## What sticks out

**`main.py` is the product.** 7,053 lines. `sms_reply` is a ~4,000-line decision tree; `process_single_action` is the rest. Keyword handlers run before AI. Pending traps (support mode, smart nudge, NEEDS_TIME, low-confidence confirm, delete menus) swallow messages that look like new intents. That is the right architecture for SMS; it is also why 279 happened: `"Show 3"` missed a picker and the AI emitted `delete_reminder`.

**Issue #17 is still true, and wiring it now would be a mistake.** `routes/handlers/` exports reminder, list, memory, and pending-state functions. Production `main.py` only imports the **shared-list / list-picker** subset (`main.py:48–53`). Live delete YES lives in `sms_reply` (`main.py:2066–2113`). The unwired twin `handle_pending_delete` (`routes/handlers/pending_states.py:42–106`) was partially synced for `list_item` in PR 278 but still has **no `memory` or `list` YES branches**. Tests in `tests/test_shared_lists.py` call `handle_add_to_list` / `handle_delete_list` directly — those are not the `/sms` path. Two sources of truth is the debt. Delete the dead modules or wire one action the next time you touch it. Do not “incrementally adopt” in the abstract.

**README is fiction.** It still says `main.py` is 288 lines and `reminder_service.py` is the background checker. Production checking is Celery (`celery_config.py`, `tasks/reminder_tasks.py`). `services/reminder_service.py` `start_reminder_checker()` is never called.

**Runtime DDL instead of migrations.** `database.py` `init_db()` is CREATE TABLE + a 300-statement ALTER/UPDATE soup. Phone-specific backfills (`%4793`, `%2936`, `%6167` at lines 708–710) run in every environment. Combined with finding #1, this is the scariest ops pattern in the repo.

**Encryption is dual-write, plaintext-read.** `ENCRYPTION_ENABLED` writes `first_name` *and* `first_name_encrypted` (`models/user.py:110–118`). `log_interaction` still stores plaintext `message_in`/`message_out` (`database.py:851–862`). Celery tasks `SELECT first_name`. `get_user()` never decrypts. If the threat model is “disk at rest,” this does not meet it. If the threat model is “optional future,” document that and stop implying it is on.

**Trial docs vs Beat.** CLAUDE.md still describes 8 messages Day 3–44. `celery_config.py` disabled trial-expiration warnings (crying wolf, May 2026) and Day 2/Day 3. Live: Day 1, mid-trial, Day 4 email, post-trial, 14d, 30d winback, inactivity, plus onboarding 5-min nudge + VCF. `trial_messaging_service.py:100–108` and onboarding pricing (`onboarding_service.py:136`) still say **“2 reminders/day”** via `TIER_LIMITS[TIER_FREE]` (= v1), even if finding #1 were fixed.

**Sports plumbing (PR 280) looks contained.** Invites are not on Beat (`celery_config.py:156–159`). `list_active_optins()` (`models/sports.py:248–254`) filters `opted_out` and `stopped_silently`, **not** `lifecycle_messages_opted_out`. `tests/test_lifecycle_pause.py:83–97` only scans `tasks/reminder_tasks.py`, so this gap would not fail CI. Do not expand ALWAYS-send here.

**Issue #15 (HTML list names) is leftover data, not leftover code.** Storage-time `html.escape` is gone (`utils/validation.py` `sanitize_text`). `_norm_text()` unescapes on some lookups (`get_list_by_name`, item match) but **not** on `delete_list` / `rename_list` / `clear_list`. Users can still *see* `Sam&#39;s Club` in SMS. One SQL cleanup; then drop the unescape if you want.

**Tests: good conversation coverage, no merge gate.** ~452 tests under `tests/`. Strengths: `test_list_picker.py` (279), `test_delete_items.py` (278), `test_shared_lists.py`, `test_nfl_scores.py`, `test_email_fallback.py`. Gaps: claiming races, Twilio signature, encryption, Stripe, admin auth, trial Beat tasks. `test_background_tasks.py:131–151` generates a weekly reminder and **asserts nothing about the weekday**. `.github/workflows/deploy.yml` curl-hooks Render with **no pytest**. `run_tests.py --lists/--memories/--scenarios` points at files that do not exist. Root `test_confidence*.py` are live-server scripts, not the suite.

**Admin surface is a second product.** `admin_dashboard.py` is 11,325 lines (Basic Auth, CSRF-unprotected browser POSTs). CS portal can grant Premium and export PII. Fine for a founder-operated tool; treat it as high-privilege, not “just HTML.”

**PII in logs.** `main.py` usually masks. `services/ai_service.py:25` and `tasks/reminder_tasks.py:180` log full phone numbers.

---

## High-leverage improvements (priority order)

1. **Remove the repeating `free_tier_version = 1` UPDATE** (`database.py:714`). Verify counts in prod. This is a one-line deletion plus a SQL check — highest expected value in the repo.

2. **Unify numbered-list identity** for display / picker / `Delete N` / bare digit. Use `get_all_lists_with_shared` (or a single formatter) everywhere `__LISTS__` is set. Add one test: two owned + one shared, `MY LISTS`, `Delete 3`. While there: store single-option deletes as `awaiting_confirmation` dicts so YES works (`main.py:2835`).

3. **Put pytest on the deploy path** (GitHub Action on PRs, even `--quick`). Fix `run_tests.py` flags. Delete or move root `test_confidence*.py`. Add three missing tests that match real outages: `claim_due_reminders` skip-locked, `check_reminder_exists_for_recurring` error → skip, Twilio signature fail → 403 with `ENVIRONMENT=production`.

4. **Fail closed on recurring duplicates.** Unique index + `check_reminder_exists_for_recurring` returns `True` on error. Optional: persist `MessageSid` in Redis (same Upstash) before processing `/sms`.

5. **Stop lying in user-facing limit copy.** Thread `get_tier_limits(tier, free_tier_version)` into `get_trial_info_for_save_action` and onboarding pricing. Sync CLAUDE.md / Beat comments with the live 3 first-week + post-trial set.

**Deliberately later (do not start as a project):**
- Wiring `routes/handlers` into `sms_reply` (issue #17). If you touch a feature, call the handler *or* delete the unused file in the same PR.
- Alembic / schema version table — worth it before MS 365 columns land, not as a rewrite.
- Making field encryption real (stop plaintext columns) — product decision, not a drive-by.
- Admin CSRF / Redis rate limits — after you actually scale web.
- Sports ALWAYS-send (separate v2 spec).
- HTML entity backfill (issue #15) — cosmetic; run SQL when convenient.

**Pending-state hygiene (when you next touch `sms_reply`):** support mode and NEEDS_TIME still trap unrelated text. That is mostly correct for SMS. The remaining footgun is stale `pending_reminder_delete` as an array + a bare digit (early picker uses `allow_bare_number=False` at `main.py:2024`). 279 made `Show 3` safe; bare `3` can still confirm an old menu.

---

## Evidence index (programmer cheat sheet)

| Area | Live code | Unwired / stale twin |
|------|-----------|----------------------|
| SMS routing | `main.py` `sms_reply` (~394–4564), `process_single_action` (~4567+) | `routes/handlers/{pending_states,reminders,memories}.py` |
| Shared lists (wired) | `main.py:48–53`, AI actions ~5976–5986 | Other `handle_*` in `lists.py` used by tests only |
| Reminder send | `tasks/reminder_tasks.py` `claim_due_reminders` consumer + `send_single_reminder` | `services/reminder_service.py` (dead) |
| Schema | `database.py` `init_db` | No `migrations/` directory |
| Tier | `config.py` `FREE_TIER_LIMITS`, `tier_service.py` | Copy in `trial_messaging_service.py` / `onboarding_service.py` |
| Sports | `celery_config.py` `send-nfl-score-asks`, `sports_score_service.py` | Invites not scheduled (intentional) |

Issue #15: data cleanup, lookup already mitigated in `_norm_text`. Issue #17: still accurate; do not treat as a near-term project.
