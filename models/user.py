"""
User Model
Handles all user-related database operations
"""

from database import get_db_connection, return_db_connection
from config import logger, ENCRYPTION_ENABLED

def get_user(phone_number):
    """Get user info from database"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone, safe_decrypt
            phone_hash = hash_phone(phone_number)
            c.execute('SELECT * FROM users WHERE phone_hash = %s', (phone_hash,))
        else:
            c.execute('SELECT * FROM users WHERE phone_number = %s', (phone_number,))

        result = c.fetchone()
        return result
    except Exception as e:
        logger.error(f"Error getting user: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)

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
    """Create or update user record with optional encryption"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Encrypt sensitive fields if encryption is enabled
        if ENCRYPTION_ENABLED:
            from utils.encryption import encrypt_field, hash_phone
            phone_hash = hash_phone(phone_number)

            # Encrypt PII fields
            encrypted_fields = {'first_name', 'last_name', 'email'}
            for field in encrypted_fields:
                if field in kwargs and kwargs[field]:
                    kwargs[f'{field}_encrypted'] = encrypt_field(kwargs[field])

        # Check if user exists
        if ENCRYPTION_ENABLED:
            c.execute('SELECT phone_number FROM users WHERE phone_hash = %s', (phone_hash,))
        else:
            c.execute('SELECT phone_number FROM users WHERE phone_number = %s', (phone_number,))
        exists = c.fetchone()

        if exists:
            # Update existing user
            if kwargs:
                update_fields = []
                values = []
                for key, value in kwargs.items():
                    update_fields.append(f"{key} = %s")
                    values.append(value)
                values.append(phone_number)

                query = f"UPDATE users SET {', '.join(update_fields)} WHERE phone_number = %s"
                c.execute(query, values)
        else:
            # Insert new user with any provided fields
            fields = ['phone_number']
            values = [phone_number]
            placeholders = ['%s']

            if ENCRYPTION_ENABLED:
                fields.append('phone_hash')
                values.append(phone_hash)
                placeholders.append('%s')

            for key, value in kwargs.items():
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

def get_user_timezone(phone_number):
    """Get user's timezone"""
    user = get_user(phone_number)
    if user and user[5]:  # timezone column
        return user[5]
    return 'America/New_York'  # Default
