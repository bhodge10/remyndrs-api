"""
Metrics Service
Handles user activity tracking and metrics aggregation
"""

from datetime import datetime
from database import get_db_connection, return_db_connection
from config import logger


def _date_filter(column, start_date=None, end_date=None):
    """Returns (sql_fragment, params_list) for date range filtering.

    Args:
        column: Fully qualified column name (e.g., 'u.created_at')
        start_date: datetime or None for range start (inclusive)
        end_date: datetime or None for range end (exclusive)

    Returns:
        Tuple of (sql_fragment_string, params_list)
    """
    clauses = []
    params = []
    if start_date:
        clauses.append(f"{column} >= %s")
        params.append(start_date)
    if end_date:
        clauses.append(f"{column} < %s")
        params.append(end_date)
    if clauses:
        return " AND " + " AND ".join(clauses), params
    return "", []

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

def get_active_users(days=7, start_date=None, end_date=None):
    """Get count of users active in last N days"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        query = '''
            SELECT COUNT(*) FROM users
            WHERE last_active_at >= NOW() - INTERVAL '%s days'
            AND onboarding_complete = TRUE
        '''
        params = [days]
        df, dp = _date_filter('created_at', start_date, end_date)
        query += df
        params.extend(dp)
        c.execute(query, params)
        result = c.fetchone()[0]
        return result
    except Exception as e:
        logger.error(f"Error getting active users: {e}")
        return 0
    finally:
        if conn:
            return_db_connection(conn)


def get_daily_signups(days=30, start_date=None, end_date=None):
    """Get daily signup counts for last N days"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        if start_date or end_date:
            query = '''
                SELECT DATE(created_at) as signup_date, COUNT(*) as count
                FROM users
                WHERE onboarding_complete = TRUE
            '''
            params = []
            df, dp = _date_filter('created_at', start_date, end_date)
            query += df
            params.extend(dp)
            query += ' GROUP BY DATE(created_at) ORDER BY signup_date DESC'
            c.execute(query, params)
        else:
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


def get_new_user_counts(start_date=None, end_date=None):
    """Get new user counts for today, this week, and this month"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        df, dp = _date_filter('created_at', start_date, end_date)

        # Today
        query = '''
            SELECT COUNT(*) FROM users
            WHERE DATE(created_at) = CURRENT_DATE
            AND onboarding_complete = TRUE
        '''
        query += df
        c.execute(query, dp)
        today = c.fetchone()[0]

        # This week (last 7 days)
        query = '''
            SELECT COUNT(*) FROM users
            WHERE created_at >= NOW() - INTERVAL '7 days'
            AND onboarding_complete = TRUE
        '''
        query += df
        c.execute(query, list(dp))
        this_week = c.fetchone()[0]

        # This month (last 30 days)
        query = '''
            SELECT COUNT(*) FROM users
            WHERE created_at >= NOW() - INTERVAL '30 days'
            AND onboarding_complete = TRUE
        '''
        query += df
        c.execute(query, list(dp))
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


def get_premium_stats(start_date=None, end_date=None):
    """Get premium vs free user counts"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        query = '''
            SELECT
                COALESCE(premium_status, 'free') as status,
                COUNT(*) as count
            FROM users
            WHERE onboarding_complete = TRUE
        '''
        params = []
        df, dp = _date_filter('created_at', start_date, end_date)
        query += df
        params.extend(dp)
        query += ' GROUP BY COALESCE(premium_status, \'free\')'
        c.execute(query, params)
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


def get_reminder_completion_rate(start_date=None, end_date=None):
    """Get reminder delivery statistics"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        query = '''
            SELECT
                COALESCE(delivery_status, 'pending') as status,
                COUNT(*) as count
            FROM reminders
            WHERE 1=1
        '''
        params = []
        df, dp = _date_filter('created_at', start_date, end_date)
        query += df
        params.extend(dp)
        query += " GROUP BY COALESCE(delivery_status, 'pending')"
        c.execute(query, params)
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


def get_engagement_stats(start_date=None, end_date=None):
    """Get average engagement metrics per user"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        df_user, dp_user = _date_filter('created_at', start_date, end_date)

        # Get total users
        query = 'SELECT COUNT(*) FROM users WHERE onboarding_complete = TRUE'
        query += df_user
        c.execute(query, dp_user)
        total_users = c.fetchone()[0] or 1

        # Get total memories
        df_mem, dp_mem = _date_filter('created_at', start_date, end_date)
        query = 'SELECT COUNT(*) FROM memories WHERE 1=1'
        query += df_mem
        c.execute(query, dp_mem)
        total_memories = c.fetchone()[0]

        # Get total reminders
        df_rem, dp_rem = _date_filter('created_at', start_date, end_date)
        query = 'SELECT COUNT(*) FROM reminders WHERE 1=1'
        query += df_rem
        c.execute(query, dp_rem)
        total_reminders = c.fetchone()[0]

        # Get total messages (from users created in range)
        query = 'SELECT SUM(COALESCE(total_messages, 0)) FROM users WHERE 1=1'
        query += df_user
        c.execute(query, list(dp_user))
        total_messages = c.fetchone()[0] or 0

        # Get total lists
        df_list, dp_list = _date_filter('created_at', start_date, end_date)
        query = 'SELECT COUNT(*) FROM lists WHERE 1=1'
        query += df_list
        c.execute(query, dp_list)
        total_lists = c.fetchone()[0]

        # Get total list items
        df_li, dp_li = _date_filter('created_at', start_date, end_date)
        query = 'SELECT COUNT(*) FROM list_items WHERE 1=1'
        query += df_li
        c.execute(query, dp_li)
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


def get_referral_breakdown(start_date=None, end_date=None):
    """Get user counts by referral source"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        query = '''
            SELECT
                COALESCE(referral_source, 'unknown') as source,
                COUNT(*) as count
            FROM users
            WHERE onboarding_complete = TRUE
        '''
        params = []
        df, dp = _date_filter('created_at', start_date, end_date)
        query += df
        params.extend(dp)
        query += " GROUP BY COALESCE(referral_source, 'unknown') ORDER BY count DESC"
        c.execute(query, params)
        results = c.fetchall()
        return results
    except Exception as e:
        logger.error(f"Error getting referral breakdown: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def get_cost_analytics(start_date=None, end_date=None):
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
        user_query = '''
            SELECT
                CASE
                    WHEN trial_end_date > NOW() AND premium_status IN ('premium', 'family')
                        THEN 'trial'
                    ELSE COALESCE(premium_status, 'free')
                END as plan,
                COUNT(*) as count
            FROM users
            WHERE onboarding_complete = TRUE
        '''
        user_params = []
        df_u, dp_u = _date_filter('created_at', start_date, end_date)
        user_query += df_u + ' GROUP BY 1'
        user_params.extend(dp_u)
        c.execute(user_query, user_params)
        user_counts = {row[0]: row[1] for row in c.fetchall()}

        results = {}

        for period_name, interval in periods.items():
            period_data = {}

            # Build additional date filter for logs/api_usage
            df_l, dp_l = _date_filter('l.created_at', start_date, end_date)
            df_a, dp_a = _date_filter('a.created_at', start_date, end_date)

            # Get SMS costs by plan (count messages from logs table)
            sms_query = '''
                SELECT
                    CASE
                        WHEN u.trial_end_date > NOW() AND u.premium_status IN ('premium', 'family')
                            THEN 'trial'
                        ELSE COALESCE(u.premium_status, 'free')
                    END as plan,
                    COUNT(*) as message_count
                FROM logs l
                JOIN users u ON l.phone_number = u.phone_number
                WHERE l.created_at >= NOW() - %s::interval
            '''
            sms_params = [interval]
            sms_query += df_l
            sms_params.extend(dp_l)
            sms_query += ' GROUP BY 1'
            c.execute(sms_query, sms_params)
            sms_by_plan = {row[0]: row[1] for row in c.fetchall()}

            # Get AI costs by plan (from api_usage table)
            ai_query = '''
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
                WHERE a.created_at >= NOW() - %s::interval
            '''
            ai_params = [interval]
            ai_query += df_a
            ai_params.extend(dp_a)
            ai_query += ' GROUP BY 1'
            c.execute(ai_query, ai_params)
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
            total_users_count = sum(user_counts.values())
            period_data['total'] = {
                'sms_cost': round(total_sms, 4),
                'ai_cost': round(total_ai, 4),
                'total_cost': round(total_sms + total_ai, 4),
                'user_count': total_users_count,
                'cost_per_user': round((total_sms + total_ai) / total_users_count, 4) if total_users_count > 0 else 0
            }

            results[period_name] = period_data

        # Add actual Twilio costs alongside estimates
        results['twilio_actual'] = get_twilio_actual_costs(start_date=start_date, end_date=end_date)

        return results

    except Exception as e:
        logger.error(f"Error getting cost analytics: {e}")
        return {}
    finally:
        if conn:
            return_db_connection(conn)


def get_twilio_actual_costs(start_date=None, end_date=None):
    """Get actual Twilio costs from the twilio_costs table.

    Returns summary for today, last 7 days, and last 30 days.
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        periods = {
            'day': '1 day',
            'week': '7 days',
            'month': '30 days',
        }

        df, dp = _date_filter('cost_date', start_date, end_date)

        results = {}
        for period_name, interval in periods.items():
            query = '''
                SELECT
                    COALESCE(SUM(inbound_count), 0),
                    COALESCE(SUM(inbound_cost), 0),
                    COALESCE(SUM(outbound_count), 0),
                    COALESCE(SUM(outbound_cost), 0),
                    COALESCE(SUM(total_cost), 0),
                    COUNT(*)
                FROM twilio_costs
                WHERE cost_date >= CURRENT_DATE - %s::interval
            '''
            params = [interval]
            query += df
            params.extend(list(dp))
            c.execute(query, params)
            row = c.fetchone()
            results[period_name] = {
                'inbound_count': row[0],
                'inbound_cost': float(row[1]),
                'outbound_count': row[2],
                'outbound_cost': float(row[3]),
                'total_cost': float(row[4]),
                'days_with_data': row[5],
            }

        return results

    except Exception as e:
        logger.error(f"Error getting Twilio actual costs: {e}")
        return {}
    finally:
        if conn:
            return_db_connection(conn)


def get_all_metrics(start_date=None, end_date=None):
    """Get all metrics for dashboard"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Total users all-time (never date-filtered)
        c.execute('SELECT COUNT(*) FROM users WHERE onboarding_complete = TRUE')
        total_users_all_time = c.fetchone()[0]

        # Total users (completed onboarding) - date-filtered
        query = 'SELECT COUNT(*) FROM users WHERE onboarding_complete = TRUE'
        params = []
        df, dp = _date_filter('created_at', start_date, end_date)
        query += df
        params.extend(dp)
        c.execute(query, params)
        total_users = c.fetchone()[0]

        # Pending onboarding (started but not completed)
        query = 'SELECT COUNT(*) FROM users WHERE onboarding_complete = FALSE AND onboarding_step > 0'
        query += df
        c.execute(query, list(dp))
        pending_onboarding = c.fetchone()[0]

        # Convert referrals to serializable format
        referrals_raw = get_referral_breakdown(start_date=start_date, end_date=end_date)
        referrals = [[str(r[0]), r[1]] for r in referrals_raw]

        # Convert daily_signups to serializable format
        signups_raw = get_daily_signups(30, start_date=start_date, end_date=end_date)
        daily_signups = [[str(s[0]), s[1]] for s in signups_raw]

        return {
            'total_users': total_users,
            'total_users_all_time': total_users_all_time,
            'pending_onboarding': pending_onboarding,
            'active_7d': get_active_users(7, start_date=start_date, end_date=end_date),
            'active_7d_all': get_active_users(7),
            'active_30d': get_active_users(30, start_date=start_date, end_date=end_date),
            'active_30d_all': get_active_users(30),
            'new_users': get_new_user_counts(start_date=start_date, end_date=end_date),
            'premium_stats': get_premium_stats(start_date=start_date, end_date=end_date),
            'reminder_stats': get_reminder_completion_rate(start_date=start_date, end_date=end_date),
            'engagement': get_engagement_stats(start_date=start_date, end_date=end_date),
            'referrals': referrals,
            'daily_signups': daily_signups
        }
    except Exception as e:
        logger.error(f"Error getting all metrics: {e}")
        return {}
    finally:
        if conn:
            return_db_connection(conn)
