"""
Metrics Service
Handles user activity tracking and metrics aggregation
"""

from datetime import datetime
from database import get_db_connection, return_db_connection
from config import logger

# Cost constants
SMS_COST_PER_MESSAGE = 0.0079  # $0.0079 per inbound/outbound SMS
# OpenAI pricing (per 1K tokens) - GPT-4o-mini
OPENAI_INPUT_COST_PER_1K = 0.00015   # $0.00015 per 1K input tokens
OPENAI_OUTPUT_COST_PER_1K = 0.0006   # $0.0006 per 1K output tokens


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


def get_cost_analytics():
    """Get cost analytics broken down by plan tier and time period"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Time period intervals
        periods = {
            'hour': '1 hour',
            'day': '1 day',
            'week': '7 days',
            'month': '30 days'
        }

        # Get user counts by plan (distinguishing trial users)
        # Trial = premium_status is premium/family AND trial_end_date > NOW()
        c.execute('''
            SELECT
                CASE
                    WHEN trial_end_date > NOW() AND premium_status IN ('premium', 'family')
                        THEN 'trial'
                    ELSE COALESCE(premium_status, 'free')
                END as plan,
                COUNT(*) as count
            FROM users
            WHERE onboarding_complete = TRUE
            GROUP BY 1
        ''')
        user_counts = {row[0]: row[1] for row in c.fetchall()}

        results = {}

        for period_name, interval in periods.items():
            period_data = {}

            # Get SMS costs by plan (count messages from logs table)
            # Each log entry = 1 inbound + 1 outbound message
            c.execute(f'''
                SELECT
                    CASE
                        WHEN u.trial_end_date > NOW() AND u.premium_status IN ('premium', 'family')
                            THEN 'trial'
                        ELSE COALESCE(u.premium_status, 'free')
                    END as plan,
                    COUNT(*) as message_count
                FROM logs l
                JOIN users u ON l.phone_number = u.phone_number
                WHERE l.created_at >= NOW() - INTERVAL '{interval}'
                GROUP BY 1
            ''')
            sms_by_plan = {row[0]: row[1] for row in c.fetchall()}

            # Get AI costs by plan (from api_usage table)
            c.execute(f'''
                SELECT
                    CASE
                        WHEN u.trial_end_date > NOW() AND u.premium_status IN ('premium', 'family')
                            THEN 'trial'
                        ELSE COALESCE(u.premium_status, 'free')
                    END as plan,
                    SUM(a.prompt_tokens) as prompt_tokens,
                    SUM(a.completion_tokens) as completion_tokens
                FROM api_usage a
                JOIN users u ON a.phone_number = u.phone_number
                WHERE a.created_at >= NOW() - INTERVAL '{interval}'
                GROUP BY 1
            ''')
            ai_by_plan = {row[0]: {'prompt': row[1] or 0, 'completion': row[2] or 0} for row in c.fetchall()}

            # Calculate costs for each plan tier (including trial)
            for plan in ['free', 'trial', 'premium', 'family']:
                message_count = sms_by_plan.get(plan, 0)
                # Each interaction = 1 inbound + 1 outbound
                sms_cost = message_count * 2 * SMS_COST_PER_MESSAGE

                ai_tokens = ai_by_plan.get(plan, {'prompt': 0, 'completion': 0})
                ai_cost = (
                    (ai_tokens['prompt'] / 1000) * OPENAI_INPUT_COST_PER_1K +
                    (ai_tokens['completion'] / 1000) * OPENAI_OUTPUT_COST_PER_1K
                )

                total_cost = sms_cost + ai_cost
                user_count = user_counts.get(plan, 0)
                cost_per_user = total_cost / user_count if user_count > 0 else 0

                period_data[plan] = {
                    'sms_cost': round(sms_cost, 4),
                    'ai_cost': round(ai_cost, 4),
                    'total_cost': round(total_cost, 4),
                    'user_count': user_count,
                    'cost_per_user': round(cost_per_user, 4),
                    'message_count': message_count,
                    'prompt_tokens': ai_tokens['prompt'],
                    'completion_tokens': ai_tokens['completion']
                }

            # Add totals
            total_sms = sum(p.get('sms_cost', 0) for p in period_data.values())
            total_ai = sum(p.get('ai_cost', 0) for p in period_data.values())
            total_users = sum(user_counts.values())
            period_data['total'] = {
                'sms_cost': round(total_sms, 4),
                'ai_cost': round(total_ai, 4),
                'total_cost': round(total_sms + total_ai, 4),
                'user_count': total_users,
                'cost_per_user': round((total_sms + total_ai) / total_users, 4) if total_users > 0 else 0
            }

            results[period_name] = period_data

        return results

    except Exception as e:
        logger.error(f"Error getting cost analytics: {e}")
        return {}
    finally:
        if conn:
            return_db_connection(conn)


def get_all_metrics():
    """Get all metrics for dashboard"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Total users (completed onboarding)
        c.execute('SELECT COUNT(*) FROM users WHERE onboarding_complete = TRUE')
        total_users = c.fetchone()[0]

        # Pending onboarding (started but not completed)
        c.execute('SELECT COUNT(*) FROM users WHERE onboarding_complete = FALSE AND onboarding_step > 0')
        pending_onboarding = c.fetchone()[0]

        return {
            'total_users': total_users,
            'pending_onboarding': pending_onboarding,
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
