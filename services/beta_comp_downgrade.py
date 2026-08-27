"""One-shot beta-comp wall: Thu/Fri warning, Saturday flip to Free.

This is NOT a restore of check_trial_expirations (still unscheduled on
purpose — a fake Day 13 would lie to September-dated comps). Target is the
32 premium comps whose trial_end_date is 2026-08-29 or 2026-06-18. The four
September comps, Stripe subscribers, and subscription_status='manual' are
left alone.

Copy is locked by Retention. Do not rewrite.
"""

from datetime import date, datetime
from typing import Optional

import pytz

from config import logger
from database import get_db_connection, return_db_connection
from services.sms_service import send_sms

# Locked target dates. September 2026-09-07 / 2026-09-12 stay premium.
BETA_COMP_TRIAL_END_DATES = (date(2026, 8, 29), date(2026, 6, 18))
BETA_COMP_SKIP_TRIAL_END_DATES = (date(2026, 9, 7), date(2026, 9, 12))

BETA_COMP_TARGET_SQL = """
    premium_status = 'premium'
    AND (stripe_subscription_id IS NULL OR stripe_subscription_id = '')
    AND (subscription_status IS NULL OR subscription_status NOT IN ('active', 'manual'))
    AND trial_end_date::date IN ('2026-08-29', '2026-06-18')
"""

WARNING_COPY = (
    "You've had full Premium. Saturday you go to Free "
    "(2 reminders/day, 2 lists, 5 memories, no new recurring). "
    "Existing recurring keep running.\n\n"
    "Text UPGRADE to keep unlimited — $8.99/mo or $89.99/yr ($7.50/mo)."
)

DOWNGRADE_COPY = (
    "You're on Free now: 2 reminders/day, 2 lists, 5 memories. "
    "New recurring is Premium.\n\n"
    "Text UPGRADE to get unlimited back — $8.99/mo or $89.99/yr."
)

WARNING_MESSAGE_TYPE = "beta_comp_warning"
DOWNGRADE_MESSAGE_TYPE = "beta_comp_downgrade"

THURSDAY = 3
FRIDAY = 4
SATURDAY = 5
WARNING_WEEKDAYS = (THURSDAY, FRIDAY)
MORNING_START_HOUR = 9
MORNING_END_HOUR = 10


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _user_tz(timezone_str: Optional[str]):
    try:
        return pytz.timezone(timezone_str or "America/New_York")
    except pytz.exceptions.UnknownTimeZoneError:
        return pytz.timezone("America/New_York")


def _aware_utc(now_utc: datetime) -> datetime:
    if now_utc.tzinfo is None:
        return pytz.utc.localize(now_utc)
    return now_utc.astimezone(pytz.utc)


def local_now(now_utc: datetime, timezone_str: Optional[str]):
    return _aware_utc(now_utc).astimezone(_user_tz(timezone_str))


def in_morning_window(local_dt: datetime) -> bool:
    return MORNING_START_HOUR <= local_dt.hour < MORNING_END_HOUR


def is_warning_weekday(local_dt: datetime) -> bool:
    return local_dt.weekday() in WARNING_WEEKDAYS


def is_flip_weekday(local_dt: datetime) -> bool:
    return local_dt.weekday() == SATURDAY


def is_beta_comp_target_row(user_row: dict) -> bool:
    """Python equivalent of BETA_COMP_TARGET_SQL for sports skip / tests."""
    if (user_row.get("premium_status") or "").lower() != "premium":
        return False
    stripe = user_row.get("stripe_subscription_id") or ""
    if str(stripe).strip():
        return False
    status = (user_row.get("subscription_status") or "").lower()
    if status in ("active", "manual"):
        return False
    trial_day = _as_date(user_row.get("trial_end_date"))
    return trial_day in BETA_COMP_TRIAL_END_DATES


def _timestamp_on_local_date(ts, timezone_str: Optional[str], local_day: date) -> bool:
    if not ts:
        return False
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts_local = pytz.utc.localize(ts).astimezone(_user_tz(timezone_str))
        else:
            ts_local = ts.astimezone(_user_tz(timezone_str))
        return ts_local.date() == local_day
    if isinstance(ts, date):
        return ts == local_day
    return False


def beta_comp_message_sent_this_local_morning(user_row: dict, now_utc: datetime) -> bool:
    """True if the Thu/Fri warning or Saturday confirm already went this local day."""
    local = local_now(now_utc, user_row.get("timezone"))
    tz = user_row.get("timezone")
    return (
        _timestamp_on_local_date(user_row.get("beta_comp_warning_sent_at"), tz, local.date())
        or _timestamp_on_local_date(user_row.get("beta_comp_downgraded_at"), tz, local.date())
    )


def should_skip_score_for_beta_comp(user_row: dict, now_utc: datetime) -> Optional[str]:
    """Score ping loses the local morning this warning/confirm is (or will be) sent."""
    if beta_comp_message_sent_this_local_morning(user_row, now_utc):
        return "beta_comp_warning"
    local = local_now(now_utc, user_row.get("timezone"))
    if local.weekday() in (THURSDAY, FRIDAY, SATURDAY) and is_beta_comp_target_row(user_row):
        return "beta_comp_warning"
    return None


def list_beta_comp_targets() -> list[dict]:
    """All 32-set rows (no SMS-opt-out filter). Used by tests and both tasks."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            f"""
            SELECT phone_number, first_name, timezone, trial_end_date,
                   premium_status, subscription_status, stripe_subscription_id,
                   beta_comp_warning_sent_at, beta_comp_downgraded_at
            FROM users
            WHERE {BETA_COMP_TARGET_SQL}
            """
        )
        rows = c.fetchall()
        return [
            {
                "phone_number": row[0],
                "first_name": row[1],
                "timezone": row[2],
                "trial_end_date": row[3],
                "premium_status": row[4],
                "subscription_status": row[5],
                "stripe_subscription_id": row[6],
                "beta_comp_warning_sent_at": row[7],
                "beta_comp_downgraded_at": row[8],
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"Error listing beta-comp targets: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def _is_sms_silenced(opted_out, lifecycle_paused) -> bool:
    return bool(opted_out) or bool(lifecycle_paused)


def process_beta_comp_warnings(now_utc: datetime = None) -> dict:
    """Thu/Fri 9-10am local warning. Once per user. Does not flip."""
    if now_utc is None:
        now_utc = datetime.utcnow()

    logger.info("Starting beta-comp warning check")
    conn = None
    warnings_sent = 0
    skipped = 0
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            f"""
            SELECT phone_number, timezone, opted_out, lifecycle_messages_opted_out
            FROM users
            WHERE {BETA_COMP_TARGET_SQL}
              AND onboarding_complete = TRUE
              AND beta_comp_warning_sent_at IS NULL
            """
        )
        users = c.fetchall()
        if not users:
            logger.info("No beta-comp users due for warning")
            return {"warnings_sent": 0, "skipped": 0}

        for phone_number, timezone_str, opted_out, lifecycle_paused in users:
            local = local_now(now_utc, timezone_str)
            if not is_warning_weekday(local) or not in_morning_window(local):
                skipped += 1
                continue
            if _is_sms_silenced(opted_out, lifecycle_paused):
                skipped += 1
                continue
            try:
                send_sms(phone_number, WARNING_COPY, message_type=WARNING_MESSAGE_TYPE)
                c.execute(
                    """
                    UPDATE users
                    SET beta_comp_warning_sent_at = %s
                    WHERE phone_number = %s
                      AND beta_comp_warning_sent_at IS NULL
                    """,
                    (now_utc.replace(tzinfo=None) if now_utc.tzinfo else now_utc, phone_number),
                )
                conn.commit()
                if c.rowcount:
                    warnings_sent += 1
                    logger.info(f"Sent beta-comp warning to ...{phone_number[-4:]}")
                else:
                    skipped += 1
            except Exception as sms_error:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.error(f"Failed to send beta-comp warning to ...{phone_number[-4:]}: {sms_error}")
                continue

        logger.info(f"Beta-comp warning check complete: {warnings_sent} sent")
        return {"warnings_sent": warnings_sent, "skipped": skipped}
    except Exception:
        logger.exception("Error in process_beta_comp_warnings")
        raise
    finally:
        if conn:
            return_db_connection(conn)


def _apply_downgrade(cursor, phone_number: str, now_utc: datetime) -> int:
    """Match Day-14 (premium_status=free) and actually drop the trial so
    get_user_tier cannot keep them premium. Pin v1 so locked copy matches.
    Existing recurring rows are untouched — generate_recurring_reminders
    does not check tier.
    """
    naive_now = now_utc.replace(tzinfo=None) if getattr(now_utc, "tzinfo", None) else now_utc
    cursor.execute(
        f"""
        UPDATE users
        SET premium_status = 'free',
            free_tier_version = 1,
            trial_end_date = CASE
                WHEN trial_end_date IS NOT NULL AND trial_end_date > %s THEN %s
                ELSE trial_end_date
            END,
            beta_comp_downgraded_at = %s
        WHERE phone_number = %s
          AND beta_comp_downgraded_at IS NULL
          AND {BETA_COMP_TARGET_SQL}
        """,
        (naive_now, naive_now, naive_now, phone_number),
    )
    return cursor.rowcount


def process_beta_comp_downgrade(now_utc: datetime = None) -> dict:
    """Saturday 9-10am local: flip the 32 to free, then confirm SMS.

    Runs only on Saturday so the Thu/Fri warning has had a chance to send.
    Idempotent via beta_comp_downgraded_at. Stripe/manual still excluded by
    BETA_COMP_TARGET_SQL.
    """
    if now_utc is None:
        now_utc = datetime.utcnow()

    logger.info("Starting beta-comp Saturday downgrade")
    conn = None
    downgraded = 0
    confirms_sent = 0
    skipped = 0
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            f"""
            SELECT phone_number, timezone, opted_out, lifecycle_messages_opted_out
            FROM users
            WHERE {BETA_COMP_TARGET_SQL}
              AND onboarding_complete = TRUE
              AND beta_comp_downgraded_at IS NULL
            """
        )
        users = c.fetchall()
        if not users:
            logger.info("No beta-comp users due for Saturday downgrade")
            return {"downgraded": 0, "confirms_sent": 0, "skipped": 0}

        for phone_number, timezone_str, opted_out, lifecycle_paused in users:
            local = local_now(now_utc, timezone_str)
            if not is_flip_weekday(local) or not in_morning_window(local):
                skipped += 1
                continue

            silenced = _is_sms_silenced(opted_out, lifecycle_paused)
            try:
                if not silenced:
                    send_sms(phone_number, DOWNGRADE_COPY, message_type=DOWNGRADE_MESSAGE_TYPE)
                updated = _apply_downgrade(c, phone_number, now_utc)
                conn.commit()
                if updated:
                    downgraded += 1
                    if not silenced:
                        confirms_sent += 1
                    logger.info(f"Beta-comp downgraded ...{phone_number[-4:]}")
                else:
                    skipped += 1
            except Exception as err:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.error(f"Failed beta-comp downgrade for ...{phone_number[-4:]}: {err}")
                continue

        logger.info(
            f"Beta-comp Saturday downgrade complete: {downgraded} flipped, "
            f"{confirms_sent} confirms sent"
        )
        return {"downgraded": downgraded, "confirms_sent": confirms_sent, "skipped": skipped}
    except Exception:
        logger.exception("Error in process_beta_comp_downgrade")
        raise
    finally:
        if conn:
            return_db_connection(conn)
