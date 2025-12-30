"""
Reminder Service
Handles reminder background checking and sending

Uses atomic SELECT FOR UPDATE to prevent duplicate sends:
1. Lock the row before sending
2. Send SMS while holding lock
3. Mark as sent in same transaction
4. Commit releases lock
"""

import threading
import time
from datetime import datetime

from config import logger, REMINDER_CHECK_INTERVAL
from database import get_db_connection, return_db_connection
from models.reminder import update_last_sent_reminder
from services.sms_service import send_sms
from services.metrics_service import track_reminder_delivery


def send_reminder_atomically(reminder_id: int, phone_number: str, reminder_text: str) -> bool:
    """
    Send a single reminder with atomic locking to prevent duplicates.

    Holds FOR UPDATE lock throughout: check -> send -> mark sent -> commit
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Lock the row and verify it hasn't been sent yet
        c.execute(
            'SELECT sent FROM reminders WHERE id = %s FOR UPDATE',
            (reminder_id,)
        )
        result = c.fetchone()

        if not result:
            logger.warning(f"Reminder {reminder_id} not found")
            conn.rollback()
            return False

        if result[0]:  # Already sent
            logger.warning(f"Reminder {reminder_id} already sent, skipping")
            conn.commit()  # Release lock
            return False

        # Send SMS while holding the lock
        try:
            send_sms(phone_number, f"Reminder: {reminder_text}\n\n(Reply SNOOZE to snooze)")
        except Exception as e:
            logger.error(f"Failed to send SMS for reminder {reminder_id}: {e}")
            conn.rollback()  # Release lock
            track_reminder_delivery(reminder_id, 'failed', str(e))
            return False

        # SMS sent - mark as sent in the SAME transaction (still holding lock)
        c.execute('UPDATE reminders SET sent = TRUE WHERE id = %s', (reminder_id,))
        conn.commit()  # This releases the lock

        logger.info(f"Reminder {reminder_id} sent and marked successfully")

        # These are nice-to-have, don't fail the whole operation
        try:
            update_last_sent_reminder(phone_number, reminder_id)
        except Exception as e:
            logger.error(f"Failed to update last_sent_reminder: {e}")

        try:
            track_reminder_delivery(reminder_id, 'sent')
        except Exception as e:
            logger.error(f"Failed to track delivery: {e}")

        return True

    except Exception as e:
        logger.error(f"Error in send_reminder_atomically for {reminder_id}: {e}")
        if conn:
            try:
                conn.rollback()
            except:
                pass
        return False
    finally:
        if conn:
            return_db_connection(conn)


def get_due_reminders_batch(batch_size: int = 10):
    """
    Get a batch of due reminders that haven't been sent.
    Does NOT lock them - locking happens in send_reminder_atomically.
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        c.execute(
            '''SELECT id, phone_number, reminder_text
               FROM reminders
               WHERE reminder_date <= %s AND sent = FALSE
               ORDER BY reminder_date ASC
               LIMIT %s''',
            (now, batch_size)
        )
        return c.fetchall()
    except Exception as e:
        logger.error(f"Error getting due reminders: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def check_reminders():
    """Background job that runs every minute to check for due reminders"""
    logger.info("Reminder checker thread started")

    while True:
        try:
            logger.info(f"Checking for due reminders at {datetime.utcnow()}")
            due_reminders = get_due_reminders_batch(batch_size=10)

            if due_reminders:
                logger.info(f"Found {len(due_reminders)} due reminders")

                sent_count = 0
                for reminder_id, phone_number, reminder_text in due_reminders:
                    if send_reminder_atomically(reminder_id, phone_number, reminder_text):
                        sent_count += 1

                logger.info(f"Successfully sent {sent_count}/{len(due_reminders)} reminders")
            else:
                logger.debug("No due reminders")

        except Exception as e:
            logger.error(f"Error in reminder checker loop: {e}")
            # Don't crash - just log and continue

        # Wait for next check
        time.sleep(REMINDER_CHECK_INTERVAL)


def start_reminder_checker():
    """Start the reminder checker background thread"""
    try:
        reminder_thread = threading.Thread(target=check_reminders, daemon=True)
        reminder_thread.start()
        logger.info("Reminder checker thread launched")
    except Exception as e:
        logger.error(f"Failed to start reminder thread: {e}")
