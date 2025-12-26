"""
Metrics Service
Handles user activity tracking and metrics aggregation
"""

from datetime import datetime
from database import get_db_connection, return_db_connection
from config import logger


# =============================================================================
# TRACKING FUNCTIONS
# =============================================================================

def track_user_activity(phone_number):
    """Update user's last active timestamp"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'UPDATE users SET last_active_at = %s WHERE phone_number = %s',
            (datetime.utcnow(), phone_number)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Error tracking user activity: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def increment_message_count(phone_number):
    """Increment user's total message count"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'UPDATE users SET total_messages = COALESCE(total_messages, 0) + 1 WHERE phone_number = %s',
            (phone_number,)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Error incrementing message count: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def track_reminder_delivery(reminder_id, status, error=None):
    """Track reminder delivery status"""
    conn = None
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
    except Exception as e:
        logger.error(f"Error tracking reminder delivery: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def set_referral_source(phone_number, source):
    """Set user's referral source"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'UPDATE users SET referral_source = %s WHERE phone_number = %s',
            (source, phone_number)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Error setting referral source: {e}")
    finally:
        if conn:
            return_db_connection(conn)


# =============================================================================
# AGGREGATION QUERIES
# =============================================================================

def get_active_users(days=7):
    """Get count of users active in last N days"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            SELECT COUNT(*) FROM users
            WHERE last_active_at >= NOW() - INTERVAL '%s days'
            AND onboarding_complete = TRUE
        ''', (days,))
        result = c.fetchone()[0]
        return result
    except Exception as e:
        logger.error(f"Error getting active users: {e}")
        return 0
    finally:
        if conn:
            return_db_connection(conn)


def get_daily_signups(days=30):
    """Get daily signup counts for last N days"""
    conn = None
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
        return results
    except Exception as e:
        logger.error(f"Error getting daily signups: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def get_new_user_counts():
    """Get new user counts for today, this week, and this month"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Today
        c.execute('''
            SELECT COUNT(*) FROM users
            WHERE DATE(created_at) = CURRENT_DATE
            AND onboarding_complete = TRUE
        ''')
        today = c.fetchone()[0]

        # This week (last 7 days)
        c.execute('''
            SELECT COUNT(*) FROM users
            WHERE created_at >= NOW() - INTERVAL '7 days'
            AND onboarding_complete = TRUE
        ''')
        this_week = c.fetchone()[0]

        # This month (last 30 days)
        c.execute('''
            SELECT COUNT(*) FROM users
            WHERE created_at >= NOW() - INTERVAL '30 days'
            AND onboarding_complete = TRUE
        ''')
        this_month = c.fetchone()[0]

        return {
            'today': today,
            'this_week': this_week,
            'this_month': this_month
        }
    except Exception as e:
        logger.error(f"Error getting new user counts: {e}")
        return {'today': 0, 'this_week': 0, 'this_month': 0}
    finally:
        if conn:
            return_db_connection(conn)


def get_premium_stats():
    """Get premium vs free user counts"""
    conn = None
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

        stats = {'free': 0, 'premium': 0, 'churned': 0}
        for status, count in results:
            if status in stats:
                stats[status] = count
        return stats
    except Exception as e:
        logger.error(f"Error getting premium stats: {e}")
        return {'free': 0, 'premium': 0, 'churned': 0}
    finally:
        if conn:
            return_db_connection(conn)


def get_reminder_completion_rate():
    """Get reminder delivery statistics"""
    conn = None
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
    finally:
        if conn:
            return_db_connection(conn)


def get_engagement_stats():
    """Get average engagement metrics per user"""
    conn = None
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

        # Get total lists
        c.execute('SELECT COUNT(*) FROM lists')
        total_lists = c.fetchone()[0]

        # Get total list items
        c.execute('SELECT COUNT(*) FROM list_items')
        total_list_items = c.fetchone()[0]

        # Calculate avg items per list
        avg_items_per_list = round(total_list_items / total_lists, 2) if total_lists > 0 else 0

        return {
            'avg_memories_per_user': round(total_memories / total_users, 2),
            'avg_reminders_per_user': round(total_reminders / total_users, 2),
            'avg_messages_per_user': round(total_messages / total_users, 2),
            'avg_lists_per_user': round(total_lists / total_users, 2),
            'avg_items_per_list': avg_items_per_list,
            'total_memories': total_memories,
            'total_reminders': total_reminders,
            'total_messages': total_messages,
            'total_lists': total_lists
        }
    except Exception as e:
        logger.error(f"Error getting engagement stats: {e}")
        return {
            'avg_memories_per_user': 0,
            'avg_reminders_per_user': 0,
            'avg_messages_per_user': 0,
            'avg_lists_per_user': 0,
            'avg_items_per_list': 0,
            'total_memories': 0,
            'total_reminders': 0,
            'total_messages': 0,
            'total_lists': 0
        }
    finally:
        if conn:
            return_db_connection(conn)


def get_referral_breakdown():
    """Get user counts by referral source"""
    conn = None
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
        return results
    except Exception as e:
        logger.error(f"Error getting referral breakdown: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def get_all_metrics():
    """Get all metrics for dashboard"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Total users
        c.execute('SELECT COUNT(*) FROM users WHERE onboarding_complete = TRUE')
        total_users = c.fetchone()[0]

        return {
            'total_users': total_users,
            'active_7d': get_active_users(7),
            'active_30d': get_active_users(30),
            'new_users': get_new_user_counts(),
            'premium_stats': get_premium_stats(),
            'reminder_stats': get_reminder_completion_rate(),
            'engagement': get_engagement_stats(),
            'referrals': get_referral_breakdown(),
            'daily_signups': get_daily_signups(30)
        }
    except Exception as e:
        logger.error(f"Error getting all metrics: {e}")
        return {}
    finally:
        if conn:
            return_db_connection(conn)
