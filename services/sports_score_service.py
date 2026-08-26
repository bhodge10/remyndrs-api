"""NFL morning-after score beta — opt-in, SCORE, asks, pause, anti-bunch.

Closed beta plumbing. Invites are NOT sent from this module.
"""

import re
from datetime import datetime, timedelta, date
from typing import Optional

import pytz

from config import logger, FOUNDER_SURVEY_EXCLUDE_PHONES, TIER_PREMIUM, TIER_FAMILY
from database import get_setting
from models.sports import (
    COHORT_WEEKLY,
    COHORT_DORMANT,
    EVENT_SCORE_ASK,
    EVENT_SCORE_REPLY,
    EVENT_SCORE_IGNORE,
    EVENT_UPGRADE_TO_KEEP,
    EVENT_SPORTS_YES,
    get_optin,
    upsert_optin,
    update_optin,
    list_active_optins,
    log_sports_event,
    get_invite_state,
    sports_invite_sent_this_week,
)
from services.espn_nfl import (
    fetch_scoreboard,
    parse_finals,
    find_team_final,
    format_final,
    fake_game_for_team,
)
from services.nfl_teams import resolve_team
from services.sms_service import send_sms

# Locked SMS copy — do not date, do not rewrite.
INVITE_WEEKLY = (
    "Trying something for football season. Want your team's score the morning after? "
    "I'll ask first. Reply YES + team (NFL)."
)
INVITE_DORMANT = (
    "Still here if you want. Trying football scores the morning after a game "
    "(I'll ask first — no spoilers). Reply YES + team, or ignore."
)
ASK_TEMPLATE = "{team} played last night. Reply SCORE if you want it, or ignore."
PAUSE_COPY = (
    "That's the end of score texts unless you keep Premium. "
    "Text UPGRADE to keep them — or ignore and I'll stop."
)
UNKNOWN_TEAM_COPY = "I didn't recognize that team. Reply YES + team (NFL), like YES Bengals."
NO_PENDING_SCORE_COPY = "No score waiting — I'll ask the morning after your team plays."

YES_TEAM_RE = re.compile(r"^\s*YES\s*\+?\s+(.+)$", re.IGNORECASE)
SCORE_RE = re.compile(r"^\s*SCORE\s*$", re.IGNORECASE)

BETA_DAYS = 28
IGNORE_STOP_STREAK = 3

# Day 7/13/14 of a 14-day trial, matching check_trial_expirations windows.
DAY7_REMAINING = (6, 7)
DAY13_REMAINING = (0, 1)  # 0 < remaining <= 1 handled separately


def is_yes_team_message(text: str) -> bool:
    return bool(YES_TEAM_RE.match(text or ""))


def is_score_message(text: str) -> bool:
    return bool(SCORE_RE.match(text or ""))


def parse_yes_team(text: str) -> Optional[str]:
    match = YES_TEAM_RE.match(text or "")
    if not match:
        return None
    return match.group(1).strip()


def _user_tz(timezone_str: Optional[str]):
    try:
        return pytz.timezone(timezone_str or "America/New_York")
    except pytz.exceptions.UnknownTimeZoneError:
        return pytz.timezone("America/New_York")


def _local_now(now_utc: datetime, timezone_str: Optional[str]):
    tz = _user_tz(timezone_str)
    if now_utc.tzinfo is None:
        now_utc = pytz.utc.localize(now_utc)
    return now_utc.astimezone(tz)


def is_paid_premium(user_row: dict) -> bool:
    """Paid Premium/Family (Stripe active). Trial-only premium does not count."""
    status = (user_row.get("subscription_status") or "").lower()
    if status == "active" and user_row.get("stripe_subscription_id"):
        return True
    # Family/premium with active sub but missing stripe id shouldn't happen; be conservative.
    tier = (user_row.get("premium_status") or "").lower()
    if status == "active" and tier in (TIER_PREMIUM, TIER_FAMILY) and user_row.get("stripe_subscription_id"):
        return True
    return False


def compute_pause_at(opted_in_at: datetime, trial_end_date: Optional[datetime]) -> datetime:
    """Pause after 4 weeks or trial end, whichever comes first."""
    four_weeks = opted_in_at + timedelta(days=BETA_DAYS)
    if trial_end_date and trial_end_date < four_weeks:
        return trial_end_date
    return four_weeks


def should_pause_scores(optin: dict, now_utc: datetime, user_row: Optional[dict] = None) -> bool:
    if user_row and is_paid_premium(user_row):
        return False
    pause_at = optin.get("pause_at")
    if pause_at and now_utc >= pause_at:
        return True
    trial_end = (user_row or {}).get("trial_end_date") or optin.get("trial_end_date")
    if trial_end and now_utc >= trial_end and not (user_row and is_paid_premium(user_row)):
        return True
    return False


def trial_day_conflict(user_row: dict, now_utc: datetime) -> Optional[str]:
    """Return skip reason if this local morning is Day 7, 13, 14, win-back, or sports invite."""
    trial_end = user_row.get("trial_end_date")
    local = _local_now(now_utc, user_row.get("timezone"))

    if trial_end:
        if trial_end.tzinfo is None:
            trial_end_naive = trial_end
        else:
            trial_end_naive = trial_end.replace(tzinfo=None)
        now_naive = now_utc.replace(tzinfo=None) if now_utc.tzinfo else now_utc
        days_remaining = (trial_end_naive - now_naive).days

        # Day 7 of trial (7 days remaining) — mid-trial / 7d warning
        if DAY7_REMAINING[0] <= days_remaining <= DAY7_REMAINING[1]:
            return "day_7"
        # Day 13 (1 day remaining)
        if 0 < days_remaining <= 1:
            return "day_13"
        # Day 14 (expired today)
        if days_remaining <= 0:
            days_since = (now_naive - trial_end_naive).days
            if days_since <= 0:
                return "day_14"
            # 30-day win-back window (trial ended 29-31 days ago), unsent or sent today
            if 29 <= days_since <= 31 and not user_row.get("winback_30d_sent"):
                return "winback"
            if user_row.get("winback_30d_sent"):
                # If winback flag is set, only skip when it was sent this local morning
                # (we don't store sent-at; skip whenever still in the 30d window after send
                # would over-skip). Check invite/outbound instead below.
                pass

    invite_at = user_row.get("sports_invite_sent_at")
    if invite_at:
        invite_local = invite_at
        if invite_at.tzinfo is None:
            invite_local = pytz.utc.localize(invite_at).astimezone(_user_tz(user_row.get("timezone")))
        elif invite_at.tzinfo:
            invite_local = invite_at.astimezone(_user_tz(user_row.get("timezone")))
        if getattr(invite_local, "date", None) and invite_local.date() == local.date():
            return "sports_invite"

    return None


def winback_due_this_morning(user_row: dict, now_utc: datetime) -> bool:
    trial_end = user_row.get("trial_end_date")
    if not trial_end or user_row.get("winback_30d_sent"):
        return False
    now_naive = now_utc.replace(tzinfo=None) if now_utc.tzinfo else now_utc
    trial_naive = trial_end.replace(tzinfo=None) if getattr(trial_end, "tzinfo", None) else trial_end
    days_since = (now_naive - trial_naive).days
    return 29 <= days_since <= 31


def should_skip_score_ask(user_row: dict, now_utc: datetime) -> Optional[str]:
    """Anti-bunch: score ping loses to Day 7/13/14, win-back, or the sports invite."""
    reason = trial_day_conflict(user_row, now_utc)
    if reason:
        return reason
    if winback_due_this_morning(user_row, now_utc):
        return "winback"
    return None


def handle_yes_team(phone_number: str, incoming_msg: str) -> str:
    """Opt the user in. Returns SMS reply (never None — caller always answers)."""
    team_raw = parse_yes_team(incoming_msg)
    if team_raw is None:
        return UNKNOWN_TEAM_COPY

    team, err = resolve_team(team_raw)
    if err:
        return err

    invite = get_invite_state(phone_number)
    cohort = invite.get("sports_invite_cohort") or COHORT_WEEKLY
    if cohort not in (COHORT_WEEKLY, COHORT_DORMANT):
        cohort = COHORT_WEEKLY

    trial_end = _fetch_trial_end(phone_number)

    now = datetime.utcnow()
    pause_at = compute_pause_at(now, trial_end)
    upsert_optin(
        phone_number,
        team["abbr"],
        team["short"],
        cohort,
        opted_in_at=now,
        pause_at=pause_at,
        beta_started_at=now,
    )
    log_sports_event(
        phone_number,
        EVENT_SPORTS_YES,
        cohort,
        metadata={"team": team["abbr"]},
    )
    return (
        f"Got it — {team['short']}. I'll ask the morning after they play. "
        "Reply SCORE if you want it, or ignore."
    )


def _fetch_trial_end(phone_number: str) -> Optional[datetime]:
    from database import get_db_connection, return_db_connection
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT trial_end_date FROM users WHERE phone_number = %s", (phone_number,))
        row = c.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"Error fetching trial_end_date: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def handle_score_keyword(phone_number: str) -> Optional[str]:
    """If a pending ask exists, send the final and mark replied. Never hits the AI."""
    optin = get_optin(phone_number)
    if not optin:
        return NO_PENDING_SCORE_COPY
    payload = optin.get("pending_score_payload")
    if not payload or optin.get("last_ask_replied"):
        return NO_PENDING_SCORE_COPY

    reply = format_final(payload)
    update_optin(
        phone_number,
        last_ask_replied=True,
        ignore_streak=0,
        pending_score_payload=None,
    )
    log_sports_event(
        phone_number,
        EVENT_SCORE_REPLY,
        optin["cohort"],
        metadata={"game_id": optin.get("last_ask_game_id"), "team": optin.get("team_abbr")},
    )
    return reply


def maybe_log_upgrade_to_keep(phone_number: str) -> None:
    """If the user was paused on scores, log upgrade_to_keep (does not change UPGRADE copy)."""
    optin = get_optin(phone_number)
    if not optin:
        return
    if optin.get("paused_at") or should_pause_scores(optin, datetime.utcnow(), optin):
        log_sports_event(
            phone_number,
            EVENT_UPGRADE_TO_KEEP,
            optin["cohort"],
            metadata={"team": optin.get("team_abbr")},
        )


def _asks_enabled() -> bool:
    return get_setting("nfl_score_asks_enabled", "true") != "false"


def _in_morning_window(local_dt: datetime) -> bool:
    return 9 <= local_dt.hour < 10


def dry_run_phones() -> list[str]:
    extra = get_setting("nfl_score_dry_run_phones", "") or ""
    phones = list(FOUNDER_SURVEY_EXCLUDE_PHONES)
    for part in extra.replace("\n", ",").split(","):
        p = part.strip()
        if p and p not in phones:
            phones.append(p)
    return phones


def is_dry_run_allowed(phone_number: str) -> bool:
    from config import ENVIRONMENT
    if ENVIRONMENT in ("test", "testing", "development"):
        return True
    return phone_number in dry_run_phones()


def process_morning_asks(
    now_utc: Optional[datetime] = None,
    dry_run_phone: Optional[str] = None,
    fake_game: bool = False,
    scoreboard_date: Optional[date] = None,
    skip_window_check: bool = False,
    fetch_fn=None,
) -> dict:
    """Send spoiler-free asks to opted-in users whose team finished yesterday.

    dry_run_phone: process only that phone (founder fake/preseason morning).
    fake_game: use canned Bengals/Chiefs-style final instead of ESPN.
    Invites are never sent here.
    """
    now_utc = now_utc or datetime.utcnow()
    if not _asks_enabled() and not dry_run_phone:
        logger.info("NFL score asks disabled via nfl_score_asks_enabled setting")
        return {"asked": 0, "paused": 0, "skipped": 0, "stopped": 0, "reason": "disabled"}

    if dry_run_phone and not is_dry_run_allowed(dry_run_phone):
        logger.warning("NFL score dry-run refused — phone not on founder allowlist")
        return {"asked": 0, "paused": 0, "skipped": 0, "stopped": 0, "reason": "dry_run_not_allowed"}

    optins = list_active_optins()
    if dry_run_phone:
        optins = [o for o in optins if o["phone_number"] == dry_run_phone]
        skip_window_check = True

    scoreboard_cache = {}
    stats = {"asked": 0, "paused": 0, "skipped": 0, "stopped": 0, "ignored": 0}

    for optin in optins:
        phone = optin["phone_number"]
        local = _local_now(now_utc, optin.get("timezone"))

        if not skip_window_check and not _in_morning_window(local):
            stats["skipped"] += 1
            continue

        skip_reason = should_skip_score_ask(optin, now_utc)
        if skip_reason and not dry_run_phone:
            logger.info(f"Skipping NFL score ask for ...{phone[-4:]} — {skip_reason} same morning")
            stats["skipped"] += 1
            continue

        if should_pause_scores(optin, now_utc, optin):
            if not optin.get("paused_at"):
                try:
                    send_sms(phone, PAUSE_COPY, message_type="sports_score_pause")
                    update_optin(phone, paused_at=now_utc, pending_score_payload=None)
                    stats["paused"] += 1
                    logger.info(f"Sent NFL score pause to ...{phone[-4:]}")
                except Exception as e:
                    logger.error(f"Failed to send NFL score pause to ...{phone[-4:]}: {e}")
            else:
                stats["skipped"] += 1
            continue

        yesterday = scoreboard_date or (local.date() - timedelta(days=1))

        if fake_game:
            game = fake_game_for_team(optin["team_abbr"], optin["team_short"])
            if dry_run_phone:
                game = dict(game)
                game["game_id"] = f"{game['game_id']}-{int(now_utc.timestamp())}"
        else:
            cache_key = yesterday.isoformat()
            if cache_key not in scoreboard_cache:
                kwargs = {}
                if fetch_fn is not None:
                    board = fetch_scoreboard(yesterday, fetch_fn=fetch_fn)
                else:
                    board = fetch_scoreboard(yesterday)
                scoreboard_cache[cache_key] = parse_finals(board)
            game = find_team_final(scoreboard_cache[cache_key], optin["team_abbr"])

        if not game:
            continue

        # Don't ping again for the same game.
        if optin.get("last_ask_game_id") == game["game_id"]:
            stats["skipped"] += 1
            continue

        # Previous ask went unanswered → ignore streak.
        if optin.get("last_ask_game_id") and not optin.get("last_ask_replied"):
            new_streak = (optin.get("ignore_streak") or 0) + 1
            log_sports_event(
                phone,
                EVENT_SCORE_IGNORE,
                optin["cohort"],
                metadata={"game_id": optin.get("last_ask_game_id"), "streak": new_streak},
            )
            stats["ignored"] += 1
            if new_streak >= IGNORE_STOP_STREAK:
                update_optin(
                    phone,
                    ignore_streak=new_streak,
                    stopped_silently=True,
                    pending_score_payload=None,
                )
                stats["stopped"] += 1
                logger.info(f"NFL score beta silent-stop for ...{phone[-4:]} after {new_streak} ignores")
                continue
            update_optin(phone, ignore_streak=new_streak)
            optin["ignore_streak"] = new_streak

        ask_text = ASK_TEMPLATE.format(team=optin["team_short"])
        try:
            send_sms(phone, ask_text, message_type="sports_score_ask")
            update_optin(
                phone,
                last_ask_date=local.date(),
                last_ask_game_id=game["game_id"],
                last_ask_replied=False,
                pending_score_payload=game,
            )
            log_sports_event(
                phone,
                EVENT_SCORE_ASK,
                optin["cohort"],
                metadata={"game_id": game["game_id"], "team": optin["team_abbr"]},
            )
            stats["asked"] += 1
            logger.info(f"Sent NFL score ask to ...{phone[-4:]} ({optin['team_short']}, {optin['cohort']})")
        except Exception as e:
            logger.error(f"Failed to send NFL score ask to ...{phone[-4:]}: {e}")

    return stats
