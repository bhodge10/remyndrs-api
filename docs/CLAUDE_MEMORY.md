# Remyndrs Project Memory

> **Sync note:** This file is mirrored at `docs/CLAUDE_MEMORY.md` in the repo for cross-machine access. Update both when making changes.

## Environment
- Python path: `/c/Users/BradHodge/AppData/Local/Programs/Python/Python311/python.exe`
- `python` and `python3` don't work in bash; use full path above
- Windows platform, git bash shell

## Deployment (Render)
- DATABASE_URL is now `sync: false` in render.yaml (as of PR #77) — must be set manually in Render dashboard on all 4 services (api, worker, beat, monitoring)
- Previously used `fromDatabase` blueprint references which caused recurring breakage when the database hostname changed
- Internal database URL is correct for all services (all on Render)
- After database changes, always verify worker logs — the worker silently reports `{'processed': 0}` even when it can't connect to the DB

## Known Pre-existing Test Failures
- Multiple list-related test failures (item addition, checking, etc.) — "Item cannot be empty" errors
- ~31 failures in `--quick`, 3 in `--scenarios`; 18/18 `--reminders` pass

## Python Scoping Gotcha (Fixed Feb 2026)
- A `from datetime import datetime` inside a try block at line ~2165 (STATUS handler) was shadowing the module-level import for the *entire* `sms_reply` function due to Python's scoping rules
- This caused `UnboundLocalError` in unrelated code paths (lines 880, 2643) that ran before the local import
- Fix: removed the redundant local import since datetime is already imported at module level

## Daily Summary Handler Interception
- The daily summary handler aggressively catches messages that look like times
- `is_new_reminder_request` and `command_patterns` in `first_action_service.py` are the two guards that prevent legitimate commands from being intercepted
- Compact time formats (e.g., `125pm`) need normalization early in the pipeline (main.py ~line 335) before any regex matching

## Midnight Default Prevention (Fixed Feb 2026)
- AI sometimes returns `00:00:00` as default time when user doesn't specify one (e.g., "remind me tomorrow to X")
- Fix: in `reminder` action handler (~line 3515), detect midnight + no explicit time in message → redirect to `clarify_date_time` flow
- Checks for explicit time patterns and "midnight"/"12am" before redirecting

## AI Day-of-Week Miscalculation (Fixed Feb 2026)
- AI sometimes returns wrong day names for dates (e.g., "Saturday" for a Friday)
- Already noted in CLAUDE.md for reminder confirmations, but `clarify_date_time` handler was still trusting AI's response
- Fix: generate date strings server-side using `strftime('%A, %B %d')` instead of using AI's response text
- Applies to both `clarify_date_time` handler (~line 3495) and midnight detection redirect (~line 3535)
