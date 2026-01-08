"""
User Model
Handles all user-related database operations
"""

from database import get_db_connection, return_db_connection
from config import logger, ENCRYPTION_ENABLED

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
}


def get_user(phone_number):
    """Get user info from database"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            # Try phone_hash first, fallback to phone_number for existing users
            c.execute('SELECT * FROM users WHERE phone_hash = %s', (phone_hash,))
            result = c.fetchone()
            if not result:
                # Fallback for users created before encryption was enabled
                c.execute('SELECT * FROM users WHERE phone_number = %s', (phone_number,))
                result = c.fetchone()
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

def get_user_timezone(phone_number):
    """Get user's timezone"""
    user = get_user(phone_number)
    if user and user[5]:  # timezone column
        return user[5]
    return 'America/New_York'  # Default


def update_user_timezone(phone_number, new_timezone):
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


def get_user_first_name(phone_number):
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


def get_last_active_list(phone_number):
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


def get_pending_list_item(phone_number):
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


def get_pending_reminder_delete(phone_number):
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


def get_pending_memory_delete(phone_number):
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


def get_pending_reminder_date(phone_number):
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


def get_pending_list_create(phone_number):
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


def mark_user_opted_out(phone_number):
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


def is_user_opted_out(phone_number):
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
