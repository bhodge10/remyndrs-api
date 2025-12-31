"""
Reminder Model
Handles all reminder-related database operations
"""

from datetime import datetime, timedelta
from database import get_db_connection, return_db_connection
from config import logger, ENCRYPTION_ENABLED

def save_reminder(phone_number, reminder_text, reminder_date):
    """Save a new reminder to the database with optional encryption"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import encrypt_field, hash_phone
            phone_hash = hash_phone(phone_number)
            reminder_text_encrypted = encrypt_field(reminder_text)
            c.execute(
                '''INSERT INTO reminders (phone_number, phone_hash, reminder_text, reminder_text_encrypted, reminder_date)
                   VALUES (%s, %s, %s, %s, %s)''',
                (phone_number, phone_hash, reminder_text, reminder_text_encrypted, reminder_date)
            )
        else:
            c.execute(
                'INSERT INTO reminders (phone_number, reminder_text, reminder_date) VALUES (%s, %s, %s)',
                (phone_number, reminder_text, reminder_date)
            )

        conn.commit()
        logger.info(f"Saved reminder at {reminder_date}")
    except Exception as e:
        logger.error(f"Error saving reminder: {e}")
    finally:
        if conn:
            return_db_connection(conn)

def get_due_reminders():
    """Get all reminders that are due to be sent"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        c.execute(
            'SELECT id, phone_number, reminder_text FROM reminders WHERE reminder_date <= %s AND sent = FALSE',
            (now,)
        )
        results = c.fetchall()
        return results
    except Exception as e:
        logger.error(f"Error getting due reminders: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)

def mark_reminder_sent(reminder_id):
    """Mark a reminder as sent. Raises exception on failure to trigger retry."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE reminders SET sent = TRUE WHERE id = %s', (reminder_id,))
        conn.commit()
        logger.info(f"Marked reminder {reminder_id} as sent")
    except Exception as e:
        logger.error(f"Error marking reminder sent: {e}")
        raise  # Re-raise so Celery task knows to retry
    finally:
        if conn:
            return_db_connection(conn)

def get_user_reminders(phone_number):
    """Get all reminders for a user (both pending and sent)

    Returns tuples of: (id, reminder_date, reminder_text, recurring_id, sent)
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                'SELECT id, reminder_date, reminder_text, recurring_id, sent FROM reminders WHERE phone_hash = %s ORDER BY reminder_date',
                (phone_hash,)
            )
            results = c.fetchall()
            if not results:
                # Fallback for reminders created before encryption
                c.execute(
                    'SELECT id, reminder_date, reminder_text, recurring_id, sent FROM reminders WHERE phone_number = %s ORDER BY reminder_date',
                    (phone_number,)
                )
                results = c.fetchall()
        else:
            c.execute(
                'SELECT id, reminder_date, reminder_text, recurring_id, sent FROM reminders WHERE phone_number = %s ORDER BY reminder_date',
                (phone_number,)
            )
            results = c.fetchall()

        return results
    except Exception as e:
        logger.error(f"Error getting user reminders: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def get_pending_reminders(phone_number):
    """Get all pending (not yet sent) reminders for a user with IDs"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                'SELECT id, reminder_text, reminder_date FROM reminders WHERE phone_hash = %s AND sent = FALSE ORDER BY reminder_date',
                (phone_hash,)
            )
            results = c.fetchall()
            if not results:
                # Fallback for reminders created before encryption
                c.execute(
                    'SELECT id, reminder_text, reminder_date FROM reminders WHERE phone_number = %s AND sent = FALSE ORDER BY reminder_date',
                    (phone_number,)
                )
                results = c.fetchall()
        else:
            c.execute(
                'SELECT id, reminder_text, reminder_date FROM reminders WHERE phone_number = %s AND sent = FALSE ORDER BY reminder_date',
                (phone_number,)
            )
            results = c.fetchall()

        return results
    except Exception as e:
        logger.error(f"Error getting pending reminders: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def search_pending_reminders(phone_number, search_term):
    """Search pending reminders by keyword (case-insensitive)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        search_pattern = f'%{search_term}%'

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                '''SELECT id, reminder_text, reminder_date FROM reminders
                   WHERE phone_hash = %s AND sent = FALSE AND LOWER(reminder_text) LIKE LOWER(%s)
                   ORDER BY reminder_date''',
                (phone_hash, search_pattern)
            )
            results = c.fetchall()
            if not results:
                # Fallback for reminders created before encryption
                c.execute(
                    '''SELECT id, reminder_text, reminder_date FROM reminders
                       WHERE phone_number = %s AND sent = FALSE AND LOWER(reminder_text) LIKE LOWER(%s)
                       ORDER BY reminder_date''',
                    (phone_number, search_pattern)
                )
                results = c.fetchall()
        else:
            c.execute(
                '''SELECT id, reminder_text, reminder_date FROM reminders
                   WHERE phone_number = %s AND sent = FALSE AND LOWER(reminder_text) LIKE LOWER(%s)
                   ORDER BY reminder_date''',
                (phone_number, search_pattern)
            )
            results = c.fetchall()

        return results
    except Exception as e:
        logger.error(f"Error searching pending reminders: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def delete_reminder(phone_number, reminder_id):
    """Delete a specific pending reminder by ID (only if not sent)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            # Only delete if it belongs to this user and hasn't been sent
            c.execute(
                'DELETE FROM reminders WHERE id = %s AND phone_hash = %s AND sent = FALSE',
                (reminder_id, phone_hash)
            )
            if c.rowcount == 0:
                # Fallback for reminders created before encryption
                c.execute(
                    'DELETE FROM reminders WHERE id = %s AND phone_number = %s AND sent = FALSE',
                    (reminder_id, phone_number)
                )
        else:
            c.execute(
                'DELETE FROM reminders WHERE id = %s AND phone_number = %s AND sent = FALSE',
                (reminder_id, phone_number)
            )

        deleted = c.rowcount > 0
        conn.commit()
        if deleted:
            logger.info(f"Deleted reminder {reminder_id}")
        return deleted
    except Exception as e:
        logger.error(f"Error deleting reminder: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def update_last_sent_reminder(phone_number, reminder_id):
    """Update the last sent reminder for a user (for snooze detection)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'UPDATE users SET last_sent_reminder_id = %s, last_sent_reminder_at = %s WHERE phone_number = %s',
            (reminder_id, datetime.utcnow(), phone_number)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Error updating last sent reminder: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def get_last_sent_reminder(phone_number, max_age_minutes=30):
    """Get the last sent reminder for a user if within the snooze window"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get the last sent reminder info from users table
        c.execute(
            '''SELECT last_sent_reminder_id, last_sent_reminder_at
               FROM users WHERE phone_number = %s''',
            (phone_number,)
        )
        result = c.fetchone()

        if not result or not result[0] or not result[1]:
            return None

        reminder_id, sent_at = result

        # Check if within the snooze window
        if isinstance(sent_at, datetime):
            age_minutes = (datetime.utcnow() - sent_at).total_seconds() / 60
            if age_minutes > max_age_minutes:
                return None
        else:
            return None

        # Get the reminder details
        c.execute(
            'SELECT id, reminder_text FROM reminders WHERE id = %s',
            (reminder_id,)
        )
        reminder = c.fetchone()

        if reminder:
            return {'id': reminder[0], 'text': reminder[1], 'sent_at': sent_at}
        return None

    except Exception as e:
        logger.error(f"Error getting last sent reminder: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def mark_reminder_snoozed(reminder_id):
    """Mark a reminder as snoozed"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE reminders SET snoozed = TRUE WHERE id = %s', (reminder_id,))
        conn.commit()
    except Exception as e:
        logger.error(f"Error marking reminder snoozed: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def claim_due_reminders(batch_size=10):
    """
    Atomically claim due reminders using SELECT FOR UPDATE SKIP LOCKED.

    This prevents race conditions when multiple workers try to claim
    the same reminder. SKIP LOCKED ensures workers don't block each other.

    Args:
        batch_size: Maximum number of reminders to claim per call

    Returns:
        List of claimed reminder dicts with id, phone_number, reminder_text
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

        # Claim reminders atomically with SKIP LOCKED
        # This query:
        # 1. Finds due reminders that haven't been sent and aren't claimed
        # 2. Locks them (other workers will skip these rows)
        # 3. Updates claimed_at to mark them as in-progress
        # 4. Returns the claimed reminders
        c.execute("""
            WITH claimed AS (
                SELECT id, phone_number, reminder_text
                FROM reminders
                WHERE reminder_date <= %s
                  AND sent = FALSE
                  AND (claimed_at IS NULL OR claimed_at < NOW() - INTERVAL '5 minutes')
                ORDER BY reminder_date ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE reminders r
            SET claimed_at = NOW()
            FROM claimed c
            WHERE r.id = c.id
            RETURNING r.id, r.phone_number, r.reminder_text
        """, (now, batch_size))

        results = c.fetchall()
        conn.commit()

        # Convert to list of dicts
        return [
            {
                "id": row[0],
                "phone_number": row[1],
                "reminder_text": row[2],
            }
            for row in results
        ]

    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Error claiming due reminders: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def release_stale_claims(timeout_minutes=5):
    """
    Release reminders that were claimed but not processed.

    This handles cases where a worker crashes after claiming
    but before sending. Should be run periodically.

    Args:
        timeout_minutes: How old a claim must be to be considered stale

    Returns:
        Number of reminders released
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            UPDATE reminders
            SET claimed_at = NULL
            WHERE claimed_at IS NOT NULL
              AND sent = FALSE
              AND claimed_at < NOW() - INTERVAL '%s minutes'
        """, (timeout_minutes,))
        count = c.rowcount
        conn.commit()
        if count > 0:
            logger.info(f"Released {count} stale reminder claims")
        return count
    except Exception as e:
        logger.error(f"Error releasing stale claims: {e}")
        return 0
    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# RECURRING REMINDER FUNCTIONS
# =====================================================

def save_recurring_reminder(phone_number, reminder_text, recurrence_type, recurrence_day, reminder_time, timezone):
    """
    Save a new recurring reminder.

    Args:
        phone_number: User's phone number
        reminder_text: What to remind about
        recurrence_type: 'daily', 'weekly', 'weekdays', 'weekends', 'monthly'
        recurrence_day: Day of week (0-6) for weekly, day of month (1-31) for monthly, None for others
        reminder_time: Time string in HH:MM format (24-hour)
        timezone: User's timezone (e.g., 'America/New_York')

    Returns:
        The new recurring reminder ID, or None on failure
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            '''INSERT INTO recurring_reminders
               (phone_number, reminder_text, recurrence_type, recurrence_day, reminder_time, timezone)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING id''',
            (phone_number, reminder_text, recurrence_type, recurrence_day, reminder_time, timezone)
        )
        recurring_id = c.fetchone()[0]
        conn.commit()
        logger.info(f"Saved recurring reminder {recurring_id}: {recurrence_type} at {reminder_time}")
        return recurring_id
    except Exception as e:
        logger.error(f"Error saving recurring reminder: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def get_recurring_reminders(phone_number, include_inactive=False):
    """
    Get all recurring reminders for a user.

    Args:
        phone_number: User's phone number
        include_inactive: If True, include paused/inactive reminders

    Returns:
        List of recurring reminder dicts
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if include_inactive:
            c.execute(
                '''SELECT id, reminder_text, recurrence_type, recurrence_day, reminder_time,
                          timezone, active, created_at, next_occurrence
                   FROM recurring_reminders
                   WHERE phone_number = %s
                   ORDER BY created_at DESC''',
                (phone_number,)
            )
        else:
            c.execute(
                '''SELECT id, reminder_text, recurrence_type, recurrence_day, reminder_time,
                          timezone, active, created_at, next_occurrence
                   FROM recurring_reminders
                   WHERE phone_number = %s AND active = TRUE
                   ORDER BY created_at DESC''',
                (phone_number,)
            )

        results = c.fetchall()
        return [
            {
                'id': row[0],
                'reminder_text': row[1],
                'recurrence_type': row[2],
                'recurrence_day': row[3],
                'reminder_time': str(row[4]) if row[4] else None,
                'timezone': row[5],
                'active': row[6],
                'created_at': row[7].isoformat() if row[7] else None,
                'next_occurrence': row[8].isoformat() if row[8] else None
            }
            for row in results
        ]
    except Exception as e:
        logger.error(f"Error getting recurring reminders: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def get_recurring_reminder_by_id(recurring_id, phone_number=None):
    """
    Get a specific recurring reminder by ID.

    Args:
        recurring_id: The recurring reminder ID
        phone_number: Optional - verify ownership if provided

    Returns:
        Recurring reminder dict or None
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if phone_number:
            c.execute(
                '''SELECT id, phone_number, reminder_text, recurrence_type, recurrence_day,
                          reminder_time, timezone, active, created_at, next_occurrence
                   FROM recurring_reminders
                   WHERE id = %s AND phone_number = %s''',
                (recurring_id, phone_number)
            )
        else:
            c.execute(
                '''SELECT id, phone_number, reminder_text, recurrence_type, recurrence_day,
                          reminder_time, timezone, active, created_at, next_occurrence
                   FROM recurring_reminders
                   WHERE id = %s''',
                (recurring_id,)
            )

        row = c.fetchone()
        if row:
            return {
                'id': row[0],
                'phone_number': row[1],
                'reminder_text': row[2],
                'recurrence_type': row[3],
                'recurrence_day': row[4],
                'reminder_time': str(row[5]) if row[5] else None,
                'timezone': row[6],
                'active': row[7],
                'created_at': row[8].isoformat() if row[8] else None,
                'next_occurrence': row[9].isoformat() if row[9] else None
            }
        return None
    except Exception as e:
        logger.error(f"Error getting recurring reminder {recurring_id}: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def pause_recurring_reminder(recurring_id, phone_number):
    """Pause a recurring reminder (set active=FALSE)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'UPDATE recurring_reminders SET active = FALSE WHERE id = %s AND phone_number = %s',
            (recurring_id, phone_number)
        )
        success = c.rowcount > 0
        conn.commit()
        if success:
            logger.info(f"Paused recurring reminder {recurring_id}")
        return success
    except Exception as e:
        logger.error(f"Error pausing recurring reminder: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def resume_recurring_reminder(recurring_id, phone_number):
    """Resume a paused recurring reminder (set active=TRUE)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'UPDATE recurring_reminders SET active = TRUE WHERE id = %s AND phone_number = %s',
            (recurring_id, phone_number)
        )
        success = c.rowcount > 0
        conn.commit()
        if success:
            logger.info(f"Resumed recurring reminder {recurring_id}")
        return success
    except Exception as e:
        logger.error(f"Error resuming recurring reminder: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def delete_recurring_reminder(recurring_id, phone_number):
    """Delete a recurring reminder and its pending occurrences"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # First, delete any unsent reminders linked to this recurring reminder
        c.execute(
            'DELETE FROM reminders WHERE recurring_id = %s AND sent = FALSE',
            (recurring_id,)
        )
        deleted_pending = c.rowcount
        logger.info(f"Deleted {deleted_pending} pending reminders for recurring {recurring_id}")

        # Set recurring_id to NULL for sent reminders (keep history)
        c.execute(
            'UPDATE reminders SET recurring_id = NULL WHERE recurring_id = %s',
            (recurring_id,)
        )

        # Now delete the recurring reminder itself
        c.execute(
            'DELETE FROM recurring_reminders WHERE id = %s AND phone_number = %s',
            (recurring_id, phone_number)
        )
        success = c.rowcount > 0
        conn.commit()

        if success:
            logger.info(f"Deleted recurring reminder {recurring_id}")
        else:
            logger.warning(f"Delete failed for recurring {recurring_id} - rowcount=0")
        return success
    except Exception as e:
        logger.error(f"Error deleting recurring reminder: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def get_all_active_recurring_reminders():
    """
    Get all active recurring reminders (for the generation task).

    Returns:
        List of recurring reminder dicts
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            '''SELECT id, phone_number, reminder_text, recurrence_type, recurrence_day,
                      reminder_time, timezone, last_generated_date, next_occurrence
               FROM recurring_reminders
               WHERE active = TRUE'''
        )
        results = c.fetchall()
        return [
            {
                'id': row[0],
                'phone_number': row[1],
                'reminder_text': row[2],
                'recurrence_type': row[3],
                'recurrence_day': row[4],
                'reminder_time': str(row[5]) if row[5] else None,
                'timezone': row[6],
                'last_generated_date': row[7],
                'next_occurrence': row[8]
            }
            for row in results
        ]
    except Exception as e:
        logger.error(f"Error getting active recurring reminders: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def update_recurring_reminder_generated(recurring_id, last_generated_date, next_occurrence):
    """
    Update the last_generated_date and next_occurrence after generating reminders.

    Args:
        recurring_id: The recurring reminder ID
        last_generated_date: Date of last generated reminder
        next_occurrence: Datetime of next occurrence
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            '''UPDATE recurring_reminders
               SET last_generated_date = %s, next_occurrence = %s
               WHERE id = %s''',
            (last_generated_date, next_occurrence, recurring_id)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Error updating recurring reminder generated: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def save_reminder_with_local_time(phone_number, reminder_text, reminder_date, local_time, timezone, recurring_id=None):
    """
    Save a new reminder with local time info for timezone recalculation.

    Args:
        phone_number: User's phone number
        reminder_text: The reminder text
        reminder_date: UTC datetime for the reminder
        local_time: Local time string (HH:MM format)
        timezone: User's timezone
        recurring_id: Optional - link to recurring reminder

    Returns:
        The new reminder ID, or None on failure
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import encrypt_field, hash_phone
            phone_hash = hash_phone(phone_number)
            reminder_text_encrypted = encrypt_field(reminder_text)
            c.execute(
                '''INSERT INTO reminders
                   (phone_number, phone_hash, reminder_text, reminder_text_encrypted,
                    reminder_date, local_time, original_timezone, recurring_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id''',
                (phone_number, phone_hash, reminder_text, reminder_text_encrypted,
                 reminder_date, local_time, timezone, recurring_id)
            )
        else:
            c.execute(
                '''INSERT INTO reminders
                   (phone_number, reminder_text, reminder_date, local_time, original_timezone, recurring_id)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   RETURNING id''',
                (phone_number, reminder_text, reminder_date, local_time, timezone, recurring_id)
            )

        reminder_id = c.fetchone()[0]
        conn.commit()
        logger.info(f"Saved reminder {reminder_id} at {reminder_date} (local: {local_time} {timezone})")
        return reminder_id
    except Exception as e:
        logger.error(f"Error saving reminder with local time: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def check_reminder_exists_for_recurring(recurring_id, target_date):
    """
    Check if a reminder already exists for a recurring reminder on a specific date.

    Args:
        recurring_id: The recurring reminder ID
        target_date: The date to check (date object)

    Returns:
        True if reminder exists, False otherwise
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            '''SELECT 1 FROM reminders
               WHERE recurring_id = %s AND DATE(reminder_date) = %s''',
            (recurring_id, target_date)
        )
        return c.fetchone() is not None
    except Exception as e:
        logger.error(f"Error checking recurring reminder existence: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def recalculate_pending_reminders_for_timezone(phone_number, new_timezone):
    """
    Recalculate all pending reminders when user changes timezone.

    Args:
        phone_number: User's phone number
        new_timezone: New timezone string (e.g., 'America/Los_Angeles')

    Returns:
        Number of reminders updated
    """
    import pytz
    from datetime import datetime as dt

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get all pending reminders with local_time set
        c.execute(
            '''SELECT id, local_time, original_timezone
               FROM reminders
               WHERE phone_number = %s AND sent = FALSE AND local_time IS NOT NULL''',
            (phone_number,)
        )
        reminders = c.fetchall()

        updated_count = 0
        new_tz = pytz.timezone(new_timezone)

        for reminder_id, local_time, original_tz in reminders:
            try:
                # Parse local time
                if isinstance(local_time, str):
                    hour, minute = map(int, local_time.split(':'))
                else:
                    hour, minute = local_time.hour, local_time.minute

                # Get today's date in new timezone
                now = dt.now(new_tz)
                # Create new reminder datetime in new timezone
                new_reminder_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

                # If time already passed today, schedule for tomorrow
                if new_reminder_dt <= now:
                    new_reminder_dt = new_reminder_dt + timedelta(days=1)

                # Convert to UTC for storage
                new_utc = new_reminder_dt.astimezone(pytz.UTC)

                # Update the reminder
                c.execute(
                    '''UPDATE reminders
                       SET reminder_date = %s, original_timezone = %s
                       WHERE id = %s''',
                    (new_utc, new_timezone, reminder_id)
                )
                updated_count += 1

            except Exception as e:
                logger.error(f"Error recalculating reminder {reminder_id}: {e}")
                continue

        conn.commit()
        logger.info(f"Recalculated {updated_count} reminders for new timezone {new_timezone}")
        return updated_count

    except Exception as e:
        logger.error(f"Error recalculating reminders for timezone: {e}")
        return 0
    finally:
        if conn:
            return_db_connection(conn)


def update_recurring_reminders_timezone(phone_number, new_timezone):
    """
    Update timezone for all recurring reminders when user changes timezone.

    Args:
        phone_number: User's phone number
        new_timezone: New timezone string

    Returns:
        Number of recurring reminders updated
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            '''UPDATE recurring_reminders
               SET timezone = %s
               WHERE phone_number = %s AND active = TRUE''',
            (new_timezone, phone_number)
        )
        count = c.rowcount
        conn.commit()
        logger.info(f"Updated timezone for {count} recurring reminders")
        return count
    except Exception as e:
        logger.error(f"Error updating recurring reminders timezone: {e}")
        return 0
    finally:
        if conn:
            return_db_connection(conn)
