"""
User Model
Handles all user-related database operations
"""

from datetime import date
from typing import Any, Optional, Tuple

from database import get_db_connection, return_db_connection
from config import logger, ENCRYPTION_ENABLED
from utils.db_helpers import USER_COLUMNS

# Whitelist of allowed fields for SQL updates (prevents SQL injection via kwargs)
ALLOWED_USER_FIELDS = {
    'first_name', 'last_name', 'email', 'zip_code', 'timezone',
    'onboarding_complete', 'onboarding_step', 'pending_delete',
    'pending_reminder_text', 'pending_reminder_time', 'referral_source',
    'premium_status', 'premium_since', 'last_active_at', 'signup_source',
    'total_messages', 'pending_list_item', 'last_active_list',
    'first_name_encrypted', 'last_name_encrypted', 'email_encrypted',
    'pending_reminder_delete', 'pending_memory_delete', 'trial_end_date',
    'last_sent_reminder_id', 'last_sent_reminder_at', 'opted_out', 'opted_out_at',
    'stripe_customer_id', 'stripe_subscription_id', 'subscription_status',
    'pending_reminder_date', 'pending_list_create',
    'daily_summary_enabled', 'daily_summary_time', 'daily_summary_last_sent',
    'daily_summary_prompted', 'pending_daily_summary_time',
    'pending_reminder_confirmation',
    'five_minute_nudge_scheduled_at', 'five_minute_nudge_sent', 'post_onboarding_interactions',
    'trial_info_sent',
    'pending_delete_account',
}


def get_user(phone_number: str) -> Optional[Tuple[Any, ...]]:
    """Get user info from database"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            # Try phone_hash first, fallback to phone_number for existing users
            c.execute(f'SELECT {USER_COLUMNS} FROM users WHERE phone_hash = %s', (phone_hash,))
            result = c.fetchone()
            if not result:
                # Fallback for users created before encryption was enabled
                c.execute(f'SELECT {USER_COLUMNS} FROM users WHERE phone_number = %s', (phone_number,))
                result = c.fetchone()
        else:
            c.execute(f'SELECT {USER_COLUMNS} FROM users WHERE phone_number = %s', (phone_number,))
            result = c.fetchone()

        return result
    except Exception as e:
        logger.error(f"Error getting user: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)

def is_user_onboarded(phone_number: str) -> bool:
    """Check if user has completed onboarding"""
    user = get_user(phone_number)
    if user:
        return user[6]  # onboarding_complete column
    return False

def get_onboarding_step(phone_number: str) -> int:
    """Get current onboarding step"""
    user = get_user(phone_number)
    if user:
        return user[7]  # onboarding_step column
    return 0

def create_or_update_user(phone_number: str, **kwargs: Any) -> None:
    """Create or update user record with optional encryption"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        phone_hash = None
        # Encrypt sensitive fields if encryption is enabled
        if ENCRYPTION_ENABLED:
            from utils.encryption import encrypt_field, hash_phone
            phone_hash = hash_phone(phone_number)

            # Encrypt PII fields
            encrypted_fields = {'first_name', 'last_name', 'email'}
            for field in encrypted_fields:
                if field in kwargs and kwargs[field]:
                    kwargs[f'{field}_encrypted'] = encrypt_field(kwargs[field])

        # Check if user exists (always by phone_number for reliability)
        c.execute('SELECT phone_number, phone_hash FROM users WHERE phone_number = %s', (phone_number,))
        exists = c.fetchone()

        if exists:
            # Update existing user
            update_fields = []
            values = []

            # Add phone_hash if encryption enabled and user doesn't have it yet
            if ENCRYPTION_ENABLED and phone_hash and not exists[1]:
                update_fields.append("phone_hash = %s")
                values.append(phone_hash)

            for key, value in kwargs.items():
                if key not in ALLOWED_USER_FIELDS:
                    logger.warning(f"Ignoring invalid field in user update: {key}")
                    continue
                update_fields.append(f"{key} = %s")
                values.append(value)

            if update_fields:
                values.append(phone_number)
                query = f"UPDATE users SET {', '.join(update_fields)} WHERE phone_number = %s"
                c.execute(query, values)
        else:
            # Insert new user with any provided fields
            fields = ['phone_number']
            values = [phone_number]
            placeholders = ['%s']

            if ENCRYPTION_ENABLED and phone_hash:
                fields.append('phone_hash')
                values.append(phone_hash)
                placeholders.append('%s')

            for key, value in kwargs.items():
                if key not in ALLOWED_USER_FIELDS:
                    logger.warning(f"Ignoring invalid field in user insert: {key}")
                    continue
                fields.append(key)
                values.append(value)
                placeholders.append('%s')

            query = f"INSERT INTO users ({', '.join(fields)}) VALUES ({', '.join(placeholders)})"
            c.execute(query, values)

        conn.commit()
    except Exception as e:
        logger.error(f"Error creating/updating user: {e}")
    finally:
        if conn:
            return_db_connection(conn)

def get_user_timezone(phone_number: str) -> str:
    """Get user's timezone"""
    user = get_user(phone_number)
    if user and user[5]:  # timezone column
        return user[5]
    return 'America/New_York'  # Default


def update_user_timezone(phone_number: str, new_timezone: str) -> Tuple[bool, Optional[str]]:
    """
    Update user's timezone setting.

    Args:
        phone_number: User's phone number
        new_timezone: Valid pytz timezone string (e.g., 'America/Los_Angeles')

    Returns:
        tuple: (success: bool, old_timezone: str or None)
    """
    conn = None
    try:
        # Get current timezone first
        old_timezone = get_user_timezone(phone_number)

        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            # Try phone_hash first
            c.execute('UPDATE users SET timezone = %s WHERE phone_hash = %s', (new_timezone, phone_hash))
            if c.rowcount == 0:
                # Fallback to phone_number
                c.execute('UPDATE users SET timezone = %s WHERE phone_number = %s', (new_timezone, phone_number))
        else:
            c.execute('UPDATE users SET timezone = %s WHERE phone_number = %s', (new_timezone, phone_number))

        conn.commit()
        logger.info(f"Updated timezone for {phone_number[-4:]} from {old_timezone} to {new_timezone}")
        return (True, old_timezone)
    except Exception as e:
        logger.error(f"Error updating user timezone: {e}")
        return (False, None)
    finally:
        if conn:
            return_db_connection(conn)


def get_user_first_name(phone_number: str) -> Optional[str]:
    """Get user's first name"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone, decrypt_field
            phone_hash = hash_phone(phone_number)
            # Try to get encrypted name first
            c.execute('SELECT first_name, first_name_encrypted FROM users WHERE phone_hash = %s', (phone_hash,))
            result = c.fetchone()
            if not result:
                c.execute('SELECT first_name, first_name_encrypted FROM users WHERE phone_number = %s', (phone_number,))
                result = c.fetchone()
            if result:
                # Prefer encrypted field if available
                if result[1]:
                    return decrypt_field(result[1])
                return result[0]
        else:
            c.execute('SELECT first_name FROM users WHERE phone_number = %s', (phone_number,))
            result = c.fetchone()
            if result:
                return result[0]
        return None
    except Exception as e:
        logger.error(f"Error getting user first name: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def get_last_active_list(phone_number: str) -> Optional[str]:
    """Get user's last active list name"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute('SELECT last_active_list FROM users WHERE phone_hash = %s', (phone_hash,))
            result = c.fetchone()
            if not result:
                c.execute('SELECT last_active_list FROM users WHERE phone_number = %s', (phone_number,))
                result = c.fetchone()
        else:
            c.execute('SELECT last_active_list FROM users WHERE phone_number = %s', (phone_number,))
            result = c.fetchone()

        if result and result[0]:
            return result[0]
        return None
    except Exception as e:
        logger.error(f"Error getting last active list: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def get_pending_list_item(phone_number: str) -> Optional[str]:
    """Get user's pending list item (for list selection or deletion)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute('SELECT pending_list_item FROM users WHERE phone_hash = %s', (phone_hash,))
            result = c.fetchone()
            if not result:
                c.execute('SELECT pending_list_item FROM users WHERE phone_number = %s', (phone_number,))
                result = c.fetchone()
        else:
            c.execute('SELECT pending_list_item FROM users WHERE phone_number = %s', (phone_number,))
            result = c.fetchone()

        if result and result[0]:
            return result[0]
        return None
    except Exception as e:
        logger.error(f"Error getting pending list item: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def get_pending_reminder_delete(phone_number: str) -> Optional[str]:
    """Get user's pending reminder delete data (stores matching reminder IDs when multiple found)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute('SELECT pending_reminder_delete FROM users WHERE phone_hash = %s', (phone_hash,))
            result = c.fetchone()
            if not result:
                c.execute('SELECT pending_reminder_delete FROM users WHERE phone_number = %s', (phone_number,))
                result = c.fetchone()
        else:
            c.execute('SELECT pending_reminder_delete FROM users WHERE phone_number = %s', (phone_number,))
            result = c.fetchone()

        if result and result[0]:
            return result[0]
        return None
    except Exception as e:
        logger.error(f"Error getting pending reminder delete: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def get_pending_memory_delete(phone_number: str) -> Optional[str]:
    """Get user's pending memory delete data (stores matching memory IDs when multiple found or awaiting confirmation)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute('SELECT pending_memory_delete FROM users WHERE phone_hash = %s', (phone_hash,))
            result = c.fetchone()
            if not result:
                c.execute('SELECT pending_memory_delete FROM users WHERE phone_number = %s', (phone_number,))
                result = c.fetchone()
        else:
            c.execute('SELECT pending_memory_delete FROM users WHERE phone_number = %s', (phone_number,))
            result = c.fetchone()

        if result and result[0]:
            return result[0]
        return None
    except Exception as e:
        logger.error(f"Error getting pending memory delete: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def get_pending_reminder_date(phone_number: str) -> Optional[dict[str, Any]]:
    """Get user's pending reminder date (for clarify_date_time flow - date without time)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute('SELECT pending_reminder_text, pending_reminder_date FROM users WHERE phone_hash = %s', (phone_hash,))
            result = c.fetchone()
            if not result:
                c.execute('SELECT pending_reminder_text, pending_reminder_date FROM users WHERE phone_number = %s', (phone_number,))
                result = c.fetchone()
        else:
            c.execute('SELECT pending_reminder_text, pending_reminder_date FROM users WHERE phone_number = %s', (phone_number,))
            result = c.fetchone()

        if result and result[1]:
            return {'text': result[0], 'date': result[1]}
        return None
    except Exception as e:
        logger.error(f"Error getting pending reminder date: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def get_pending_list_create(phone_number: str) -> Optional[str]:
    """Get user's pending list create data (for duplicate list handling)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute('SELECT pending_list_create FROM users WHERE phone_hash = %s', (phone_hash,))
            result = c.fetchone()
            if not result:
                c.execute('SELECT pending_list_create FROM users WHERE phone_number = %s', (phone_number,))
                result = c.fetchone()
        else:
            c.execute('SELECT pending_list_create FROM users WHERE phone_number = %s', (phone_number,))
            result = c.fetchone()

        if result and result[0]:
            return result[0]
        return None
    except Exception as e:
        logger.error(f"Error getting pending list create: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def mark_user_opted_out(phone_number: str) -> bool:
    """Mark a user as opted out (STOP command compliance)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'UPDATE users SET opted_out = TRUE, opted_out_at = CURRENT_TIMESTAMP WHERE phone_number = %s',
            (phone_number,)
        )
        conn.commit()
        logger.info(f"User opted out: {phone_number[-4:]}")
        return True
    except Exception as e:
        logger.error(f"Error marking user opted out: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def is_user_opted_out(phone_number: str) -> bool:
    """Check if a user has opted out"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute('SELECT opted_out FROM users WHERE phone_hash = %s', (phone_hash,))
            result = c.fetchone()
            if not result:
                c.execute('SELECT opted_out FROM users WHERE phone_number = %s', (phone_number,))
                result = c.fetchone()
        else:
            c.execute('SELECT opted_out FROM users WHERE phone_number = %s', (phone_number,))
            result = c.fetchone()

        return result and result[0] == True
    except Exception as e:
        logger.error(f"Error checking user opt-out status: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def get_daily_summary_settings(phone_number: str) -> Optional[dict[str, Any]]:
    """Get user's daily summary settings.

    Returns:
        dict: {'enabled': bool, 'time': str (HH:MM), 'last_sent': date or None}
        or None if user not found
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                'SELECT daily_summary_enabled, daily_summary_time, daily_summary_last_sent FROM users WHERE phone_hash = %s',
                (phone_hash,)
            )
            result = c.fetchone()
            if not result:
                c.execute(
                    'SELECT daily_summary_enabled, daily_summary_time, daily_summary_last_sent FROM users WHERE phone_number = %s',
                    (phone_number,)
                )
                result = c.fetchone()
        else:
            c.execute(
                'SELECT daily_summary_enabled, daily_summary_time, daily_summary_last_sent FROM users WHERE phone_number = %s',
                (phone_number,)
            )
            result = c.fetchone()

        if result:
            return {
                'enabled': result[0] or False,
                'time': str(result[1]) if result[1] else '08:00',
                'last_sent': result[2]
            }
        return None
    except Exception as e:
        logger.error(f"Error getting daily summary settings: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def get_users_due_for_daily_summary() -> list[dict[str, Any]]:
    """Get all users who should receive their daily summary now.

    This function is timezone-aware: it finds users whose local time
    matches their summary time preference and haven't received a summary today.

    Returns:
        List of dicts: [{'phone_number': str, 'timezone': str, 'first_name': str}]
    """
    import pytz
    from datetime import datetime

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get all users with daily summary enabled
        c.execute('''
            SELECT phone_number, timezone, first_name, daily_summary_time, daily_summary_last_sent
            FROM users
            WHERE daily_summary_enabled = TRUE
              AND onboarding_complete = TRUE
              AND (opted_out IS NULL OR opted_out = FALSE)
        ''')

        results = c.fetchall()
        due_users = []

        utc_now = datetime.now(pytz.UTC)

        for row in results:
            phone_number, user_tz_str, first_name, summary_time, last_sent = row

            try:
                user_tz = pytz.timezone(user_tz_str or 'America/New_York')
                user_now = utc_now.astimezone(user_tz)

                # Parse user's summary time preference
                if summary_time:
                    time_str = str(summary_time)
                    time_parts = time_str.split(':')
                    user_hour = int(time_parts[0])
                    user_minute = int(time_parts[1]) if len(time_parts) > 1 else 0
                else:
                    user_hour, user_minute = 8, 0  # Default to 8:00 AM

                # Check if current local time matches user's preference (within same minute)
                if user_now.hour == user_hour and user_now.minute == user_minute:
                    # Check if we already sent today (in user's local date)
                    user_today = user_now.date()
                    if last_sent != user_today:
                        due_users.append({
                            'phone_number': phone_number,
                            'timezone': user_tz_str or 'America/New_York',
                            'first_name': first_name
                        })
            except Exception as e:
                logger.error(f"Error checking summary for user {phone_number[-4:]}: {e}")
                continue

        return due_users
    except Exception as e:
        logger.error(f"Error getting users for daily summary: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def mark_daily_summary_sent(phone_number: str) -> bool:
    """Mark that we sent the daily summary to this user today."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'UPDATE users SET daily_summary_last_sent = CURRENT_DATE WHERE phone_number = %s',
            (phone_number,)
        )
        conn.commit()
        logger.info(f"Marked daily summary sent for {phone_number[-4:]}")
        return True
    except Exception as e:
        logger.error(f"Error marking daily summary sent: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def claim_user_for_daily_summary(phone_number: str, user_local_date: date) -> bool:
    """Atomically claim a user for daily summary to prevent duplicates.

    Uses UPDATE ... WHERE to atomically check and update in one operation.
    Only succeeds if the user hasn't already received summary today.

    Args:
        phone_number: User's phone number
        user_local_date: The user's local date (for timezone-aware comparison)

    Returns:
        bool: True if successfully claimed, False if already sent today
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Atomic update: only updates if last_sent is different from user's local date
        # This prevents race conditions where two workers claim the same user
        c.execute('''
            UPDATE users
            SET daily_summary_last_sent = %s
            WHERE phone_number = %s
              AND (daily_summary_last_sent IS NULL OR daily_summary_last_sent != %s)
            RETURNING phone_number
        ''', (user_local_date, phone_number, user_local_date))

        result = c.fetchone()
        conn.commit()

        if result:
            logger.info(f"Claimed daily summary for {phone_number[-4:]}")
            return True
        else:
            logger.debug(f"Daily summary already sent for {phone_number[-4:]}")
            return False
    except Exception as e:
        logger.error(f"Error claiming daily summary for {phone_number[-4:]}: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)


def get_pending_reminder_confirmation(phone_number: str) -> Optional[dict[str, Any]]:
    """Get user's pending reminder confirmation (for low-confidence confirmations).

    Returns:
        dict: The parsed reminder details awaiting confirmation, or None if no pending confirmation
    """
    import json
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute('SELECT pending_reminder_confirmation FROM users WHERE phone_hash = %s', (phone_hash,))
            result = c.fetchone()
            if not result:
                c.execute('SELECT pending_reminder_confirmation FROM users WHERE phone_number = %s', (phone_number,))
                result = c.fetchone()
        else:
            c.execute('SELECT pending_reminder_confirmation FROM users WHERE phone_number = %s', (phone_number,))
            result = c.fetchone()

        if result and result[0]:
            return json.loads(result[0])
        return None
    except Exception as e:
        logger.error(f"Error getting pending reminder confirmation: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def cancel_engagement_nudge(phone_number: str) -> bool:
    """Cancel a pending engagement nudge by clearing the scheduled timestamp.

    Returns:
        bool: True if a nudge was cancelled, False if no nudge was pending
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute('''
                UPDATE users
                SET five_minute_nudge_scheduled_at = NULL
                WHERE phone_hash = %s
                  AND five_minute_nudge_scheduled_at IS NOT NULL
                  AND five_minute_nudge_sent = FALSE
                RETURNING phone_number
            ''', (phone_hash,))
            result = c.fetchone()
            if not result:
                c.execute('''
                    UPDATE users
                    SET five_minute_nudge_scheduled_at = NULL
                    WHERE phone_number = %s
                      AND five_minute_nudge_scheduled_at IS NOT NULL
                      AND five_minute_nudge_sent = FALSE
                    RETURNING phone_number
                ''', (phone_number,))
                result = c.fetchone()
        else:
            c.execute('''
                UPDATE users
                SET five_minute_nudge_scheduled_at = NULL
                WHERE phone_number = %s
                  AND five_minute_nudge_scheduled_at IS NOT NULL
                  AND five_minute_nudge_sent = FALSE
                RETURNING phone_number
            ''', (phone_number,))
            result = c.fetchone()

        conn.commit()

        if result:
            logger.info(f"Cancelled engagement nudge for user ...{phone_number[-4:]}")
            return True
        return False
    except Exception as e:
        logger.error(f"Error cancelling engagement nudge: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)


def increment_post_onboarding_interactions(phone_number: str) -> int:
    """Increment the post-onboarding interaction counter.

    Returns:
        int: The new interaction count, or -1 on error
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute('''
                UPDATE users
                SET post_onboarding_interactions = COALESCE(post_onboarding_interactions, 0) + 1
                WHERE phone_hash = %s
                RETURNING post_onboarding_interactions
            ''', (phone_hash,))
            result = c.fetchone()
            if not result:
                c.execute('''
                    UPDATE users
                    SET post_onboarding_interactions = COALESCE(post_onboarding_interactions, 0) + 1
                    WHERE phone_number = %s
                    RETURNING post_onboarding_interactions
                ''', (phone_number,))
                result = c.fetchone()
        else:
            c.execute('''
                UPDATE users
                SET post_onboarding_interactions = COALESCE(post_onboarding_interactions, 0) + 1
                WHERE phone_number = %s
                RETURNING post_onboarding_interactions
            ''', (phone_number,))
            result = c.fetchone()

        conn.commit()
        return result[0] if result else -1
    except Exception as e:
        logger.error(f"Error incrementing post-onboarding interactions: {e}")
        if conn:
            conn.rollback()
        return -1
    finally:
        if conn:
            return_db_connection(conn)


def get_user_nudge_status(phone_number: str) -> dict:
    """Get the engagement nudge status for a user.

    Returns:
        dict with keys: scheduled_at, sent, interactions, opted_out
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        query = '''
            SELECT five_minute_nudge_scheduled_at, five_minute_nudge_sent,
                   post_onboarding_interactions, opted_out
            FROM users WHERE {phone_condition}
        '''

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(query.format(phone_condition="phone_hash = %s"), (phone_hash,))
            result = c.fetchone()
            if not result:
                c.execute(query.format(phone_condition="phone_number = %s"), (phone_number,))
                result = c.fetchone()
        else:
            c.execute(query.format(phone_condition="phone_number = %s"), (phone_number,))
            result = c.fetchone()

        if result:
            return {
                'scheduled_at': result[0],
                'sent': result[1] or False,
                'interactions': result[2] or 0,
                'opted_out': result[3] or False
            }
        return {'scheduled_at': None, 'sent': False, 'interactions': 0, 'opted_out': False}
    except Exception as e:
        logger.error(f"Error getting nudge status: {e}")
        return {'scheduled_at': None, 'sent': False, 'interactions': 0, 'opted_out': False}
    finally:
        if conn:
            return_db_connection(conn)
