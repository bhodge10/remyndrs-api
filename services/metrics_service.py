"""
Metrics Service
Handles user activity tracking and metrics aggregation
"""

from datetime import datetime
from database import get_db_connection
from config import logger


# =============================================================================
# TRACKING FUNCTIONS
# =============================================================================

def track_user_activity(phone_number):
    """Update user's last active timestamp"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'UPDATE users SET last_active_at = %s WHERE phone_number = %s',
            (datetime.utcnow(), phone_number)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error tracking user activity: {e}")


def increment_message_count(phone_number):
    """Increment user's total message count"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'UPDATE users SET total_messages = COALESCE(total_messages, 0) + 1 WHERE phone_number = %s',
            (phone_number,)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error incrementing message count: {e}")


def track_reminder_delivery(reminder_id, status, error=None):
    """Track reminder delivery status"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        if status == 'sent':
            c.execute(
                'UPDATE reminders SET delivery_status = %s, sent_at = %s WHERE id = %s',
                (status, datetime.utcnow(), reminder_id)
            )
        else:
            c.execute(
                'UPDATE reminders SET delivery_status = %s, error_message = %s WHERE id = %s',
                (status, error, reminder_id)
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error tracking reminder delivery: {e}")


def set_referral_source(phone_number, source):
    """Set user's referral source"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'UPDATE users SET referral_source = %s WHERE phone_number = %s',
            (source, phone_number)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error setting referral source: {e}")


# =============================================================================
# AGGREGATION QUERIES
# =============================================================================

def get_active_users(days=7):
    """Get count of users active in last N days"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            SELECT COUNT(*) FROM users
            WHERE last_active_at >= NOW() - INTERVAL '%s days'
            AND onboarding_complete = TRUE
        ''', (days,))
        result = c.fetchone()[0]
        conn.close()
        return result
    except Exception as e:
        logger.error(f"Error getting active users: {e}")
        return 0


def get_daily_signups(days=30):
    """Get daily signup counts for last N days"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            SELECT DATE(created_at) as signup_date, COUNT(*) as count
            FROM users
            WHERE created_at >= NOW() - INTERVAL '%s days'
            AND onboarding_complete = TRUE
            GROUP BY DATE(created_at)
            ORDER BY signup_date DESC
        ''', (days,))
        results = c.fetchall()
        conn.close()
        return results
    except Exception as e:
        logger.error(f"Error getting daily signups: {e}")
        return []


def get_premium_stats():
    """Get premium vs free user counts"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            SELECT
                COALESCE(premium_status, 'free') as status,
                COUNT(*) as count
            FROM users
            WHERE onboarding_complete = TRUE
            GROUP BY COALESCE(premium_status, 'free')
        ''')
        results = c.fetchall()
        conn.close()

        stats = {'free': 0, 'premium': 0, 'churned': 0}
        for status, count in results:
            if status in stats:
                stats[status] = count
        return stats
    except Exception as e:
        logger.error(f"Error getting premium stats: {e}")
        return {'free': 0, 'premium': 0, 'churned': 0}


def get_reminder_completion_rate():
    """Get reminder delivery statistics"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            SELECT
                COALESCE(delivery_status, 'pending') as status,
                COUNT(*) as count
            FROM reminders
            GROUP BY COALESCE(delivery_status, 'pending')
        ''')
        results = c.fetchall()
        conn.close()

        stats = {'pending': 0, 'sent': 0, 'failed': 0}
        for status, count in results:
            if status in stats:
                stats[status] = count

        total = stats['sent'] + stats['failed']
        if total > 0:
            stats['completion_rate'] = round(stats['sent'] / total * 100, 1)
        else:
            stats['completion_rate'] = 100.0

        return stats
    except Exception as e:
        logger.error(f"Error getting reminder completion rate: {e}")
        return {'pending': 0, 'sent': 0, 'failed': 0, 'completion_rate': 0}


def get_engagement_stats():
    """Get average engagement metrics per user"""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get total users
        c.execute('SELECT COUNT(*) FROM users WHERE onboarding_complete = TRUE')
        total_users = c.fetchone()[0] or 1

        # Get total memories
        c.execute('SELECT COUNT(*) FROM memories')
        total_memories = c.fetchone()[0]

        # Get total reminders
        c.execute('SELECT COUNT(*) FROM reminders')
        total_reminders = c.fetchone()[0]

        # Get total messages
        c.execute('SELECT SUM(COALESCE(total_messages, 0)) FROM users')
        total_messages = c.fetchone()[0] or 0

        conn.close()

        return {
            'avg_memories_per_user': round(total_memories / total_users, 2),
            'avg_reminders_per_user': round(total_reminders / total_users, 2),
            'avg_messages_per_user': round(total_messages / total_users, 2),
            'total_memories': total_memories,
            'total_reminders': total_reminders,
            'total_messages': total_messages
        }
    except Exception as e:
        logger.error(f"Error getting engagement stats: {e}")
        return {
            'avg_memories_per_user': 0,
            'avg_reminders_per_user': 0,
            'avg_messages_per_user': 0,
            'total_memories': 0,
            'total_reminders': 0,
            'total_messages': 0
        }


def get_referral_breakdown():
    """Get user counts by referral source"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            SELECT
                COALESCE(referral_source, 'unknown') as source,
                COUNT(*) as count
            FROM users
            WHERE onboarding_complete = TRUE
            GROUP BY COALESCE(referral_source, 'unknown')
            ORDER BY count DESC
        ''')
        results = c.fetchall()
        conn.close()
        return results
    except Exception as e:
        logger.error(f"Error getting referral breakdown: {e}")
        return []


def get_all_metrics():
    """Get all metrics for dashboard"""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Total users
        c.execute('SELECT COUNT(*) FROM users WHERE onboarding_complete = TRUE')
        total_users = c.fetchone()[0]

        conn.close()

        return {
            'total_users': total_users,
            'active_7d': get_active_users(7),
            'active_30d': get_active_users(30),
            'premium_stats': get_premium_stats(),
            'reminder_stats': get_reminder_completion_rate(),
            'engagement': get_engagement_stats(),
            'referrals': get_referral_breakdown(),
            'daily_signups': get_daily_signups(30)
        }
    except Exception as e:
        logger.error(f"Error getting all metrics: {e}")
        return {}
