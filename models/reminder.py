"""
Reminder Model
Handles all reminder-related database operations
"""

from datetime import datetime
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
    """Mark a reminder as sent"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE reminders SET sent = TRUE WHERE id = %s', (reminder_id,))
        conn.commit()
    except Exception as e:
        logger.error(f"Error marking reminder sent: {e}")
    finally:
        if conn:
            return_db_connection(conn)

def get_user_reminders(phone_number):
    """Get all reminders for a user (both pending and sent)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                'SELECT reminder_text, reminder_date, sent FROM reminders WHERE phone_hash = %s ORDER BY reminder_date',
                (phone_hash,)
            )
        else:
            c.execute(
                'SELECT reminder_text, reminder_date, sent FROM reminders WHERE phone_number = %s ORDER BY reminder_date',
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
