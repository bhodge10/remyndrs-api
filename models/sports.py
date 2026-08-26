"""Data access for the NFL morning-after score beta.

sports_optins: one row per opted-in user (team, cohort, ask/pause state).
sports_score_events: growth-analytics events, always tagged with cohort so
weekly and dormant counters never mix.
users.sports_invite_*: invite tracking (sending is NOT in this PR).
"""

import json
from datetime import datetime, timedelta
from typing import Any, Optional

from config import logger
from database import get_db_connection, return_db_connection

COHORT_WEEKLY = "weekly"
COHORT_DORMANT = "dormant"
VALID_COHORTS = (COHORT_WEEKLY, COHORT_DORMANT)

EVENT_SCORE_ASK = "score_ask"
EVENT_SCORE_REPLY = "score_reply"
EVENT_SCORE_IGNORE = "score_ignore"
EVENT_UPGRADE_TO_KEEP = "upgrade_to_keep"
EVENT_SPORTS_YES = "sports_yes"


def get_optin(phone_number: str) -> Optional[dict]:
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            """
            SELECT phone_number, team_abbr, team_short, cohort, opted_in_at,
                   ignore_streak, last_ask_date, last_ask_game_id, last_ask_replied,
                   pending_score_payload, beta_started_at, pause_at, paused_at,
                   stopped_silently
            FROM sports_optins
            WHERE phone_number = %s
            """,
            (phone_number,),
        )
        row = c.fetchone()
        if not row:
            return None
        payload = row[9]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = None
        return {
            "phone_number": row[0],
            "team_abbr": row[1],
            "team_short": row[2],
            "cohort": row[3],
            "opted_in_at": row[4],
            "ignore_streak": row[5] or 0,
            "last_ask_date": row[6],
            "last_ask_game_id": row[7],
            "last_ask_replied": bool(row[8]),
            "pending_score_payload": payload,
            "beta_started_at": row[10],
            "pause_at": row[11],
            "paused_at": row[12],
            "stopped_silently": bool(row[13]),
        }
    except Exception as e:
        logger.error(f"Error getting sports opt-in: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def upsert_optin(
    phone_number: str,
    team_abbr: str,
    team_short: str,
    cohort: str,
    opted_in_at: Optional[datetime] = None,
    pause_at: Optional[datetime] = None,
    beta_started_at: Optional[datetime] = None,
) -> None:
    """Insert or replace the user's one-team opt-in. Resets ask/ignore state on team change."""
    if cohort not in VALID_COHORTS:
        raise ValueError(f"Invalid sports cohort: {cohort}")
    now = opted_in_at or datetime.utcnow()
    beta_started = beta_started_at or now
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO sports_optins (
                phone_number, team_abbr, team_short, cohort, opted_in_at,
                ignore_streak, last_ask_date, last_ask_game_id, last_ask_replied,
                pending_score_payload, beta_started_at, pause_at, paused_at,
                stopped_silently, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                0, NULL, NULL, FALSE,
                NULL, %s, %s, NULL,
                FALSE, CURRENT_TIMESTAMP
            )
            ON CONFLICT (phone_number) DO UPDATE SET
                team_abbr = EXCLUDED.team_abbr,
                team_short = EXCLUDED.team_short,
                cohort = sports_optins.cohort,
                ignore_streak = CASE
                    WHEN sports_optins.team_abbr = EXCLUDED.team_abbr
                    THEN sports_optins.ignore_streak ELSE 0 END,
                last_ask_date = CASE
                    WHEN sports_optins.team_abbr = EXCLUDED.team_abbr
                    THEN sports_optins.last_ask_date ELSE NULL END,
                last_ask_game_id = CASE
                    WHEN sports_optins.team_abbr = EXCLUDED.team_abbr
                    THEN sports_optins.last_ask_game_id ELSE NULL END,
                last_ask_replied = CASE
                    WHEN sports_optins.team_abbr = EXCLUDED.team_abbr
                    THEN sports_optins.last_ask_replied ELSE FALSE END,
                pending_score_payload = CASE
                    WHEN sports_optins.team_abbr = EXCLUDED.team_abbr
                    THEN sports_optins.pending_score_payload ELSE NULL END,
                stopped_silently = CASE
                    WHEN sports_optins.team_abbr = EXCLUDED.team_abbr
                    THEN sports_optins.stopped_silently ELSE FALSE END,
                pause_at = COALESCE(sports_optins.pause_at, EXCLUDED.pause_at),
                beta_started_at = COALESCE(sports_optins.beta_started_at, EXCLUDED.beta_started_at),
                updated_at = CURRENT_TIMESTAMP
            """,
            (phone_number, team_abbr, team_short, cohort, now, beta_started, pause_at),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Error upserting sports opt-in: {e}")
        raise
    finally:
        if conn:
            return_db_connection(conn)


def update_optin(phone_number: str, **fields: Any) -> None:
    allowed = {
        "ignore_streak", "last_ask_date", "last_ask_game_id", "last_ask_replied",
        "pending_score_payload", "pause_at", "paused_at", "stopped_silently",
        "team_abbr", "team_short",
    }
    sets = []
    values = []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"Invalid sports_optins field: {key}")
        if key == "pending_score_payload" and value is not None and not isinstance(value, str):
            value = json.dumps(value)
        sets.append(f"{key} = %s")
        values.append(value)
    if not sets:
        return
    sets.append("updated_at = CURRENT_TIMESTAMP")
    values.append(phone_number)
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            f"UPDATE sports_optins SET {', '.join(sets)} WHERE phone_number = %s",
            tuple(values),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Error updating sports opt-in: {e}")
        raise
    finally:
        if conn:
            return_db_connection(conn)


def list_active_optins() -> list[dict]:
    """Opted-in users who have not silently stopped. Caller applies pause/time filters."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            """
            SELECT s.phone_number, s.team_abbr, s.team_short, s.cohort, s.opted_in_at,
                   s.ignore_streak, s.last_ask_date, s.last_ask_game_id, s.last_ask_replied,
                   s.pending_score_payload, s.beta_started_at, s.pause_at, s.paused_at,
                   s.stopped_silently, u.timezone, u.trial_end_date, u.premium_status,
                   u.subscription_status, u.stripe_subscription_id, u.opted_out,
                   u.winback_30d_sent, u.sports_invite_sent_at
            FROM sports_optins s
            JOIN users u ON u.phone_number = s.phone_number
            WHERE COALESCE(s.stopped_silently, FALSE) = FALSE
              AND u.onboarding_complete = TRUE
              AND (u.opted_out IS NULL OR u.opted_out = FALSE)
            """
        )
        rows = c.fetchall()
        results = []
        for row in rows:
            payload = row[9]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, ValueError):
                    payload = None
            results.append({
                "phone_number": row[0],
                "team_abbr": row[1],
                "team_short": row[2],
                "cohort": row[3],
                "opted_in_at": row[4],
                "ignore_streak": row[5] or 0,
                "last_ask_date": row[6],
                "last_ask_game_id": row[7],
                "last_ask_replied": bool(row[8]),
                "pending_score_payload": payload,
                "beta_started_at": row[10],
                "pause_at": row[11],
                "paused_at": row[12],
                "stopped_silently": bool(row[13]),
                "timezone": row[14],
                "trial_end_date": row[15],
                "premium_status": row[16],
                "subscription_status": row[17],
                "stripe_subscription_id": row[18],
                "opted_out": row[19],
                "winback_30d_sent": row[20],
                "sports_invite_sent_at": row[21],
            })
        return results
    except Exception as e:
        logger.error(f"Error listing sports opt-ins: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def log_sports_event(
    phone_number: str,
    event_type: str,
    cohort: str,
    metadata: Optional[dict] = None,
) -> None:
    """Record a growth-analytics event. Cohort is required so counters never mix."""
    if cohort not in VALID_COHORTS:
        raise ValueError(f"Invalid sports cohort for event: {cohort}")
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO sports_score_events (phone_number, cohort, event_type, metadata)
            VALUES (%s, %s, %s, %s)
            """,
            (phone_number, cohort, event_type, json.dumps(metadata) if metadata else None),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Error logging sports event: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def count_sports_events(event_type: str, cohort: str) -> int:
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            """
            SELECT COUNT(*) FROM sports_score_events
            WHERE event_type = %s AND cohort = %s
            """,
            (event_type, cohort),
        )
        row = c.fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        logger.error(f"Error counting sports events: {e}")
        return 0
    finally:
        if conn:
            return_db_connection(conn)


def get_invite_state(phone_number: str) -> dict:
    """Invite tracking on the user (sending is later; used for cohort + KEEP/CLEAR skip)."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            """
            SELECT sports_invite_sent_at, sports_invite_cohort
            FROM users WHERE phone_number = %s
            """,
            (phone_number,),
        )
        row = c.fetchone()
        if not row:
            return {"sports_invite_sent_at": None, "sports_invite_cohort": None}
        return {
            "sports_invite_sent_at": row[0],
            "sports_invite_cohort": row[1],
        }
    except Exception as e:
        logger.error(f"Error getting sports invite state: {e}")
        return {"sports_invite_sent_at": None, "sports_invite_cohort": None}
    finally:
        if conn:
            return_db_connection(conn)


def mark_sports_invite(phone_number: str, cohort: str, sent_at: Optional[datetime] = None) -> None:
    """Record that an invite was sent. Does NOT send SMS — invite sending is out of scope."""
    if cohort not in VALID_COHORTS:
        raise ValueError(f"Invalid sports invite cohort: {cohort}")
    when = sent_at or datetime.utcnow()
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            """
            UPDATE users
            SET sports_invite_sent_at = %s, sports_invite_cohort = %s
            WHERE phone_number = %s
            """,
            (when, cohort, phone_number),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Error marking sports invite: {e}")
        raise
    finally:
        if conn:
            return_db_connection(conn)


def sports_invite_sent_this_week(phone_number: str, now: Optional[datetime] = None) -> bool:
    """True if a sports invite was recorded in the last 7 days (blocks KEEP/CLEAR)."""
    state = get_invite_state(phone_number)
    sent_at = state.get("sports_invite_sent_at")
    if not sent_at:
        return False
    now = now or datetime.utcnow()
    return sent_at >= now - timedelta(days=7)
