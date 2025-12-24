"""
User Model
Handles all user-related database operations
"""

from database import get_db_connection
from config import logger

def get_user(phone_number):
    """Get user info from database"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT * FROM users WHERE phone_number = %s', (phone_number,))
        result = c.fetchone()
        conn.close()
        return result
    except Exception as e:
        logger.error(f"Error getting user {phone_number}: {e}")
        return None

def is_user_onboarded(phone_number):
    """Check if user has completed onboarding"""
    user = get_user(phone_number)
    if user:
        return user[6]  # onboarding_complete column
    return False

def get_onboarding_step(phone_number):
    """Get current onboarding step"""
    user = get_user(phone_number)
    if user:
        return user[7]  # onboarding_step column
    return 0

def create_or_update_user(phone_number, **kwargs):
    """Create or update user record"""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Check if user exists
        c.execute('SELECT phone_number FROM users WHERE phone_number = %s', (phone_number,))
        exists = c.fetchone()

        if exists:
            # Update existing user
            update_fields = []
            values = []
            for key, value in kwargs.items():
                update_fields.append(f"{key} = %s")
                values.append(value)
            values.append(phone_number)

            query = f"UPDATE users SET {', '.join(update_fields)} WHERE phone_number = %s"
            c.execute(query, values)
        else:
            # Insert new user
            c.execute('INSERT INTO users (phone_number) VALUES (%s)', (phone_number,))

        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error creating/updating user {phone_number}: {e}")

def get_user_timezone(phone_number):
    """Get user's timezone"""
    user = get_user(phone_number)
    if user and user[5]:  # timezone column
        return user[5]
    return 'America/New_York'  # Default
