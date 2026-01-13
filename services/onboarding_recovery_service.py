"""
Onboarding Recovery Service
Handles follow-up for abandoned onboarding flows
"""

from datetime import datetime, timedelta
from database import get_db_connection, return_db_connection
from config import logger
from services.sms_service import send_sms


def track_onboarding_progress(phone_number, step, **kwargs):
    """Update onboarding progress for recovery tracking"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Check if record exists
        c.execute('SELECT id FROM onboarding_progress WHERE phone_number = %s', (phone_number,))
        exists = c.fetchone()

        if exists:
            # Build update query
            update_fields = ['current_step = %s', 'last_activity_at = CURRENT_TIMESTAMP']
            values = [step]

            for key, value in kwargs.items():
                if key in ['first_name', 'last_name', 'email']:
                    update_fields.append(f'{key} = %s')
                    values.append(value)

            values.append(phone_number)
            query = f"UPDATE onboarding_progress SET {', '.join(update_fields)} WHERE phone_number = %s"
            c.execute(query, values)
        else:
            # Insert new record
            fields = ['phone_number', 'current_step']
            values = [phone_number, step]
            placeholders = ['%s', '%s']

            for key, value in kwargs.items():
                if key in ['first_name', 'last_name', 'email']:
                    fields.append(key)
                    values.append(value)
                    placeholders.append('%s')

            query = f"INSERT INTO onboarding_progress ({', '.join(fields)}) VALUES ({', '.join(placeholders)})"
            c.execute(query, values)

        conn.commit()
        logger.debug(f"Tracked onboarding progress for {phone_number[-4:]}: step {step}")
    except Exception as e:
        logger.error(f"Error tracking onboarding progress: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def mark_onboarding_complete(phone_number):
    """Remove from progress tracking when onboarding completes"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('DELETE FROM onboarding_progress WHERE phone_number = %s', (phone_number,))
        conn.commit()
        logger.debug(f"Cleared onboarding progress for {phone_number[-4:]}")
    except Exception as e:
        logger.error(f"Error marking onboarding complete: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def mark_onboarding_cancelled(phone_number):
    """Mark onboarding as cancelled (user chose to cancel)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'UPDATE onboarding_progress SET cancelled = TRUE WHERE phone_number = %s',
            (phone_number,)
        )
        conn.commit()
        logger.debug(f"Marked onboarding cancelled for {phone_number[-4:]}")
    except Exception as e:
        logger.error(f"Error marking onboarding cancelled: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def get_abandoned_onboardings_24h():
    """Get users who started onboarding but abandoned 24+ hours ago"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        cutoff = datetime.utcnow() - timedelta(hours=24)

        c.execute('''
            SELECT phone_number, first_name, current_step, started_at
            FROM onboarding_progress
            WHERE cancelled = FALSE
              AND followup_24h_sent = FALSE
              AND last_activity_at < %s
        ''', (cutoff,))

        return [
            {
                'phone_number': row[0],
                'first_name': row[1],
                'current_step': row[2],
                'started_at': row[3]
            }
            for row in c.fetchall()
        ]
    except Exception as e:
        logger.error(f"Error getting abandoned onboardings: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def get_abandoned_onboardings_7d():
    """Get users who need 7-day final follow-up"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        cutoff = datetime.utcnow() - timedelta(days=7)

        c.execute('''
            SELECT phone_number, first_name, current_step
            FROM onboarding_progress
            WHERE cancelled = FALSE
              AND followup_24h_sent = TRUE
              AND followup_7d_sent = FALSE
              AND last_activity_at < %s
        ''', (cutoff,))

        return [
            {
                'phone_number': row[0],
                'first_name': row[1],
                'current_step': row[2]
            }
            for row in c.fetchall()
        ]
    except Exception as e:
        logger.error(f"Error getting 7d abandoned onboardings: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def mark_followup_sent(phone_number, followup_type):
    """Mark that a follow-up was sent"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        field = 'followup_24h_sent' if followup_type == '24h' else 'followup_7d_sent'
        c.execute(
            f'UPDATE onboarding_progress SET {field} = TRUE WHERE phone_number = %s',
            (phone_number,)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Error marking followup sent: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def get_onboarding_progress(phone_number):
    """Get current onboarding progress for a user"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'SELECT current_step, first_name, last_name, email, cancelled FROM onboarding_progress WHERE phone_number = %s',
            (phone_number,)
        )
        result = c.fetchone()
        if result:
            return {
                'current_step': result[0],
                'first_name': result[1],
                'last_name': result[2],
                'email': result[3],
                'cancelled': result[4]
            }
        return None
    except Exception as e:
        logger.error(f"Error getting onboarding progress: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def get_step_info(step):
    """Get info about what's needed at a given onboarding step"""
    step_info = {
        1: {'needed': 'first name', 'remaining': 4},
        2: {'needed': 'last name', 'remaining': 3},
        3: {'needed': 'email address', 'remaining': 2},
        4: {'needed': 'ZIP code', 'remaining': 1},
    }
    return step_info.get(step, {'needed': 'information', 'remaining': 1})


def build_24h_followup_message(first_name, current_step):
    """Build the 24-hour follow-up message"""
    step_info = get_step_info(current_step)
    greeting = f"Hi {first_name}!" if first_name else "Hi!"

    questions_word = "question" if step_info['remaining'] == 1 else "questions"

    return f"""{greeting}

I noticed you started setting up Remyndrs but didn't finish.

You're so close - just {step_info['remaining']} more {questions_word} and you're all set with your free trial!

Text anything to pick up where you left off, or text CANCEL if you've changed your mind.

No pressure - I'll be here whenever you're ready!"""


def build_7d_followup_message(first_name):
    """Build the 7-day final follow-up message"""
    greeting = f"Hi {first_name}," if first_name else "Hi,"

    return f"""{greeting}

Just checking in one more time! Your Remyndrs account setup is still waiting.

Ready to finish? Just text anything and we'll pick up where you left off.

Or text CANCEL if you've changed your mind - no worries!

This is my last reminder - I won't bug you again."""
