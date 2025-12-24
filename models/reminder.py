"""
Reminder Model
Handles all reminder-related database operations
"""

from datetime import datetime
from database import get_db_connection
from config import logger

def save_reminder(phone_number, reminder_text, reminder_date):
    """Save a new reminder to the database"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'INSERT INTO reminders (phone_number, reminder_text, reminder_date) VALUES (%s, %s, %s)',
            (phone_number, reminder_text, reminder_date)
        )
        conn.commit()
        conn.close()
        logger.info(f"Saved reminder for {phone_number} at {reminder_date}")
    except Exception as e:
        logger.error(f"Error saving reminder: {e}")

def get_due_reminders():
    """Get all reminders that are due to be sent"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        c.execute(
            'SELECT id, phone_number, reminder_text FROM reminders WHERE reminder_date <= %s AND sent = FALSE',
            (now,)
        )
        results = c.fetchall()
        conn.close()
        return results
    except Exception as e:
        logger.error(f"Error getting due reminders: {e}")
        return []

def mark_reminder_sent(reminder_id):
    """Mark a reminder as sent"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE reminders SET sent = TRUE WHERE id = %s', (reminder_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error marking reminder sent: {e}")

def get_user_reminders(phone_number):
    """Get all reminders for a user (both pending and sent)"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'SELECT reminder_text, reminder_date, sent FROM reminders WHERE phone_number = %s ORDER BY reminder_date',
            (phone_number,)
        )
        results = c.fetchall()
        conn.close()
        return results
    except Exception as e:
        logger.error(f"Error getting user reminders: {e}")
        return []
