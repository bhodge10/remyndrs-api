"""
Tier Service
Handles subscription tier checks and limit enforcement
"""

from datetime import datetime, timedelta
from database import get_db_connection, return_db_connection
from config import (
    logger, ENCRYPTION_ENABLED, BETA_MODE,
    TIER_FREE, TIER_PREMIUM, TIER_FAMILY,
    TIER_LIMITS, get_tier_limits
)


def get_user_tier(phone_number: str) -> str:
    """Get user's subscription tier. Returns 'free' if not found."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                'SELECT premium_status FROM users WHERE phone_hash = %s',
                (phone_hash,)
            )
            result = c.fetchone()
            if not result:
                c.execute(
                    'SELECT premium_status FROM users WHERE phone_number = %s',
                    (phone_number,)
                )
                result = c.fetchone()
        else:
            c.execute(
                'SELECT premium_status FROM users WHERE phone_number = %s',
                (phone_number,)
            )
            result = c.fetchone()

        if result and result[0]:
            return result[0]
        return TIER_FREE
    except Exception as e:
        logger.error(f"Error getting user tier: {e}")
        return TIER_FREE
    finally:
        if conn:
            return_db_connection(conn)


def get_memory_count(phone_number: str) -> int:
    """Get count of user's memories."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                'SELECT COUNT(*) FROM memories WHERE phone_hash = %s OR phone_number = %s',
                (phone_hash, phone_number)
            )
        else:
            c.execute(
                'SELECT COUNT(*) FROM memories WHERE phone_number = %s',
                (phone_number,)
            )

        result = c.fetchone()
        return result[0] if result else 0
    except Exception as e:
        logger.error(f"Error getting memory count: {e}")
        return 0
    finally:
        if conn:
            return_db_connection(conn)


def get_reminders_created_today(phone_number: str) -> int:
    """Get count of reminders created today by this user."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get start of today (UTC)
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                '''SELECT COUNT(*) FROM reminders
                   WHERE (phone_hash = %s OR phone_number = %s)
                   AND created_at >= %s''',
                (phone_hash, phone_number, today_start)
            )
        else:
            c.execute(
                '''SELECT COUNT(*) FROM reminders
                   WHERE phone_number = %s AND created_at >= %s''',
                (phone_number, today_start)
            )

        result = c.fetchone()
        return result[0] if result else 0
    except Exception as e:
        logger.error(f"Error getting today's reminder count: {e}")
        return 0
    finally:
        if conn:
            return_db_connection(conn)


def get_recurring_reminder_count(phone_number: str) -> int:
    """Get count of active recurring reminders for user."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                '''SELECT COUNT(*) FROM recurring_reminders
                   WHERE (phone_hash = %s OR phone_number = %s)
                   AND is_active = TRUE''',
                (phone_hash, phone_number)
            )
        else:
            c.execute(
                '''SELECT COUNT(*) FROM recurring_reminders
                   WHERE phone_number = %s AND is_active = TRUE''',
                (phone_number,)
            )

        result = c.fetchone()
        return result[0] if result else 0
    except Exception as e:
        logger.error(f"Error getting recurring reminder count: {e}")
        return 0
    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# LIMIT CHECK FUNCTIONS
# Each returns (allowed: bool, message: str or None)
# =====================================================

def can_create_reminder(phone_number: str) -> tuple[bool, str | None]:
    """Check if user can create another reminder today."""
    # Beta mode bypasses limits
    if BETA_MODE:
        return (True, None)

    tier = get_user_tier(phone_number)
    limits = get_tier_limits(tier)

    # None means unlimited
    if limits['reminders_per_day'] is None:
        return (True, None)

    current_count = get_reminders_created_today(phone_number)

    if current_count >= limits['reminders_per_day']:
        return (
            False,
            f"You've reached your daily limit of {limits['reminders_per_day']} reminders. "
            f"Upgrade to Premium for unlimited reminders! Text UPGRADE for details."
        )

    return (True, None)


def can_create_list(phone_number: str) -> tuple[bool, str | None]:
    """Check if user can create another list."""
    if BETA_MODE:
        return (True, None)

    tier = get_user_tier(phone_number)
    limits = get_tier_limits(tier)

    from models.list_model import get_list_count
    current_count = get_list_count(phone_number)

    if current_count >= limits['max_lists']:
        return (
            False,
            f"You've reached your limit of {limits['max_lists']} lists. "
            f"Delete a list or upgrade to Premium for more! Text UPGRADE for details."
        )

    return (True, None)


def can_add_list_item(phone_number: str, list_id: int) -> tuple[bool, str | None]:
    """Check if user can add another item to a list."""
    if BETA_MODE:
        return (True, None)

    tier = get_user_tier(phone_number)
    limits = get_tier_limits(tier)

    from models.list_model import get_item_count
    current_count = get_item_count(list_id)

    if current_count >= limits['max_items_per_list']:
        return (
            False,
            f"This list has reached its limit of {limits['max_items_per_list']} items. "
            f"Remove some items or upgrade to Premium for more! Text UPGRADE for details."
        )

    return (True, None)


def can_save_memory(phone_number: str) -> tuple[bool, str | None]:
    """Check if user can save another memory."""
    if BETA_MODE:
        return (True, None)

    tier = get_user_tier(phone_number)
    limits = get_tier_limits(tier)

    # None means unlimited
    if limits['max_memories'] is None:
        return (True, None)

    current_count = get_memory_count(phone_number)

    if current_count >= limits['max_memories']:
        return (
            False,
            f"You've reached your limit of {limits['max_memories']} saved memories. "
            f"Delete some memories or upgrade to Premium for unlimited storage! Text UPGRADE for details."
        )

    return (True, None)


def can_create_recurring_reminder(phone_number: str) -> tuple[bool, str | None]:
    """Check if user can create recurring reminders."""
    if BETA_MODE:
        return (True, None)

    tier = get_user_tier(phone_number)
    limits = get_tier_limits(tier)

    if not limits['recurring_reminders']:
        return (
            False,
            "Recurring reminders are a Premium feature. "
            "Upgrade to set daily, weekly, or monthly reminders! Text UPGRADE for details."
        )

    return (True, None)


def can_access_support(phone_number: str) -> tuple[bool, str | None]:
    """Check if user can access support tickets."""
    if BETA_MODE:
        return (True, None)

    tier = get_user_tier(phone_number)
    limits = get_tier_limits(tier)

    if not limits['support_tickets']:
        return (
            False,
            "Support tickets are a Premium feature. "
            "Upgrade for direct access to our support team! Text UPGRADE for details."
        )

    return (True, None)


def get_usage_summary(phone_number: str) -> dict:
    """Get a summary of user's current usage vs limits."""
    tier = get_user_tier(phone_number)
    limits = get_tier_limits(tier)

    from models.list_model import get_list_count

    return {
        'tier': tier,
        'reminders_today': get_reminders_created_today(phone_number),
        'reminders_limit': limits['reminders_per_day'],
        'lists': get_list_count(phone_number),
        'lists_limit': limits['max_lists'],
        'memories': get_memory_count(phone_number),
        'memories_limit': limits['max_memories'],
        'recurring_allowed': limits['recurring_reminders'],
        'support_allowed': limits['support_tickets'],
    }
