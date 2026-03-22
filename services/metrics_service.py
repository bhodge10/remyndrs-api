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

            # Get SMS costs by plan — combine inbound (logs) + outbound (sms_outbound_log)
            # Inbound: count from logs table (each row = 1 inbound message)
            inbound_query = '''
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
            inbound_params = [interval]
            inbound_query += df_l
            inbound_params.extend(dp_l)
            inbound_query += ' GROUP BY 1'
            c.execute(inbound_query, inbound_params)
            inbound_by_plan = {row[0]: row[1] for row in c.fetchall()}

            # Outbound: count from sms_outbound_log table (every send_sms call)
            df_o, dp_o = _date_filter('o.created_at', start_date, end_date)
            outbound_query = '''
                SELECT
                    CASE
                        WHEN u.trial_end_date > NOW() AND u.premium_status IN ('premium', 'family')
                            THEN 'trial'
                        ELSE COALESCE(u.premium_status, 'free')
                    END as plan,
                    COUNT(*) as message_count
                FROM sms_outbound_log o
                JOIN users u ON o.phone_number = u.phone_number
                WHERE o.created_at >= NOW() - %s::interval
            '''
            outbound_params = [interval]
            outbound_query += df_o
            outbound_params.extend(dp_o)
            outbound_query += ' GROUP BY 1'
            c.execute(outbound_query, outbound_params)
            outbound_by_plan = {row[0]: row[1] for row in c.fetchall()}

            # Combine: total messages = inbound + outbound
            all_plans = set(list(inbound_by_plan.keys()) + list(outbound_by_plan.keys()))
            sms_by_plan = {}
            for plan in all_plans:
                sms_by_plan[plan] = inbound_by_plan.get(plan, 0) + outbound_by_plan.get(plan, 0)

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
                sms_cost = message_count * SMS_COST_PER_MESSAGE

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
                    COUNT(*),
                    COALESCE(SUM(failed_count), 0),
                    COALESCE(SUM(failed_cost), 0)
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
                'failed_count': row[6],
                'failed_cost': float(row[7]),
            }

        return results

    except Exception as e:
        logger.error(f"Error getting Twilio actual costs: {e}")
        return {}
    finally:
        if conn:
            return_db_connection(conn)


def get_lifecycle_message_stats():
    """Get counts of users who received each lifecycle message type"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        stats = {}

        # Boolean-flagged lifecycle messages
        flag_columns = [
            ('day_1_nudge_sent', 'Day 1 Morning Nudge'),
            ('day_2_nudge_sent', 'Day 2 Feature Prompt'),
            ('day_3_nudge_sent', 'Day 3 Welcome Nudge'),
            ('mid_trial_reminder_sent', 'Mid-Trial Reminder (Day 7)'),
            ('trial_warning_7d_sent', 'Trial Warning (7 days)'),
            ('trial_warning_1d_sent', 'Trial Warning (1 day)'),
            ('trial_warning_0d_sent', 'Trial Expiry Notice'),
            ('post_trial_reengagement_sent', 'Post-Trial Re-engagement'),
            ('post_trial_14d_sent', 'Post-Trial 14-Day Touchpoint'),
            ('winback_30d_sent', '30-Day Win-back'),
        ]

        for column, label in flag_columns:
            c.execute(f"SELECT COUNT(*) FROM users WHERE {column} = TRUE")
            stats[column] = {'label': label, 'count': c.fetchone()[0]}

        # Timestamp-based: inactivity nudge
        c.execute("SELECT COUNT(*) FROM users WHERE inactivity_nudge_sent_at IS NOT NULL")
        stats['inactivity_nudge'] = {
            'label': 'Inactivity Re-engagement Nudge',
            'count': c.fetchone()[0],
        }

        return stats

    except Exception as e:
        logger.error(f"Error getting lifecycle message stats: {e}")
        return {}
    finally:
        if conn:
            return_db_connection(conn)


def get_product_metrics():
    """Get advanced product metrics: attribution, retention, funnel, time-to-action, DAU."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        result = {}

        # =================================================================
        # 1. SIGNUPS BY ATTRIBUTION KEYWORD
        # =================================================================
        keyword_map = {
            'TRY': 'Reddit Paid',
            'GO': 'Facebook/Instagram Organic',
            'HI': 'Organic / Direct',
            'FRIEND': 'Referral',
        }
        c.execute('''
            SELECT
                UPPER(COALESCE(referral_source, 'unknown')) as keyword,
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days') as last_7d,
                COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days') as last_30d
            FROM users
            WHERE onboarding_complete = TRUE
            GROUP BY UPPER(COALESCE(referral_source, 'unknown'))
            ORDER BY total DESC
        ''')
        attribution = []
        for row in c.fetchall():
            kw = row[0]
            channel = keyword_map.get(kw, 'Other')
            attribution.append({
                'keyword': kw,
                'channel': channel,
                'total': row[1],
                'last_7d': row[2],
                'last_30d': row[3],
            })
        result['attribution'] = attribution

        # =================================================================
        # 2. USER RETENTION METRICS
        # =================================================================
        # Day 1 retention: signed up 2+ days ago, sent message day after signup
        c.execute('''
            SELECT
                COUNT(*) as eligible,
                COUNT(*) FILTER (
                    WHERE EXISTS (
                        SELECT 1 FROM logs l
                        WHERE l.phone_number = u.phone_number
                        AND l.created_at::date = u.created_at::date + INTERVAL '1 day'
                    )
                ) as retained
            FROM users u
            WHERE u.onboarding_complete = TRUE
            AND u.created_at < NOW() - INTERVAL '2 days'
        ''')
        r = c.fetchone()
        day1_eligible, day1_retained = r[0], r[1]

        # Day 7 retention: signed up 8+ days ago, sent message between day 2-7
        c.execute('''
            SELECT
                COUNT(*) as eligible,
                COUNT(*) FILTER (
                    WHERE EXISTS (
                        SELECT 1 FROM logs l
                        WHERE l.phone_number = u.phone_number
                        AND l.created_at >= u.created_at + INTERVAL '2 days'
                        AND l.created_at < u.created_at + INTERVAL '8 days'
                    )
                ) as retained
            FROM users u
            WHERE u.onboarding_complete = TRUE
            AND u.created_at < NOW() - INTERVAL '8 days'
        ''')
        r = c.fetchone()
        day7_eligible, day7_retained = r[0], r[1]

        # Day 14 retention: signed up 15+ days ago, sent message between day 8-14
        c.execute('''
            SELECT
                COUNT(*) as eligible,
                COUNT(*) FILTER (
                    WHERE EXISTS (
                        SELECT 1 FROM logs l
                        WHERE l.phone_number = u.phone_number
                        AND l.created_at >= u.created_at + INTERVAL '8 days'
                        AND l.created_at < u.created_at + INTERVAL '15 days'
                    )
                ) as retained
            FROM users u
            WHERE u.onboarding_complete = TRUE
            AND u.created_at < NOW() - INTERVAL '15 days'
        ''')
        r = c.fetchone()
        day14_eligible, day14_retained = r[0], r[1]

        # Currently active (message in last 7 days)
        c.execute('''
            SELECT COUNT(*) FROM users
            WHERE onboarding_complete = TRUE
            AND last_active_at >= NOW() - INTERVAL '7 days'
        ''')
        currently_active = c.fetchone()[0]

        # Churned (onboarded 14+ days ago, no message in 14 days)
        c.execute('''
            SELECT COUNT(*) FROM users
            WHERE onboarding_complete = TRUE
            AND created_at < NOW() - INTERVAL '14 days'
            AND (last_active_at IS NULL OR last_active_at < NOW() - INTERVAL '14 days')
        ''')
        churned = c.fetchone()[0]

        # Total onboarded
        c.execute('SELECT COUNT(*) FROM users WHERE onboarding_complete = TRUE')
        total_onboarded = c.fetchone()[0]

        result['retention'] = {
            'day1': {'eligible': day1_eligible, 'retained': day1_retained,
                     'rate': round(day1_retained / day1_eligible * 100, 1) if day1_eligible > 0 else None},
            'day7': {'eligible': day7_eligible, 'retained': day7_retained,
                     'rate': round(day7_retained / day7_eligible * 100, 1) if day7_eligible > 0 else None},
            'day14': {'eligible': day14_eligible, 'retained': day14_retained,
                      'rate': round(day14_retained / day14_eligible * 100, 1) if day14_eligible > 0 else None},
            'currently_active': currently_active,
            'churned': churned,
            'total_onboarded': total_onboarded,
        }

        # =================================================================
        # 3. TRIAL CONVERSION FUNNEL
        # =================================================================
        # Active trial: premium with trial_end_date in future
        c.execute('''
            SELECT COUNT(*) FROM users
            WHERE onboarding_complete = TRUE
            AND trial_end_date > NOW()
            AND premium_status IN ('premium', 'family')
            AND (stripe_subscription_id IS NULL OR subscription_status != 'active')
        ''')
        active_trial = c.fetchone()[0]

        # Trial expired, free tier, still active (message in last 14 days)
        c.execute('''
            SELECT COUNT(*) FROM users
            WHERE onboarding_complete = TRUE
            AND (trial_end_date IS NOT NULL AND trial_end_date <= NOW())
            AND COALESCE(premium_status, 'free') = 'free'
            AND last_active_at >= NOW() - INTERVAL '14 days'
        ''')
        trial_expired_active = c.fetchone()[0]

        # Converted to paid: active stripe subscription
        c.execute('''
            SELECT COUNT(*) FROM users
            WHERE onboarding_complete = TRUE
            AND subscription_status = 'active'
            AND stripe_subscription_id IS NOT NULL
        ''')
        converted_paid = c.fetchone()[0]

        # Churned: trial expired, no message in 14+ days
        c.execute('''
            SELECT COUNT(*) FROM users
            WHERE onboarding_complete = TRUE
            AND (trial_end_date IS NOT NULL AND trial_end_date <= NOW())
            AND (last_active_at IS NULL OR last_active_at < NOW() - INTERVAL '14 days')
        ''')
        funnel_churned = c.fetchone()[0]

        funnel_total = active_trial + trial_expired_active + converted_paid + funnel_churned
        result['funnel'] = {
            'active_trial': active_trial,
            'trial_expired_active': trial_expired_active,
            'converted_paid': converted_paid,
            'churned': funnel_churned,
            'total': funnel_total,
        }

        # =================================================================
        # 4. TIME TO FIRST ACTION
        # =================================================================
        # Median time to first reminder
        c.execute('''
            SELECT EXTRACT(EPOCH FROM (MIN(r.created_at) - u.created_at)) as seconds_to_first
            FROM users u
            JOIN reminders r ON r.phone_number = u.phone_number
            WHERE u.onboarding_complete = TRUE
            GROUP BY u.phone_number, u.created_at
            ORDER BY seconds_to_first
        ''')
        reminder_times = [float(row[0]) for row in c.fetchall() if row[0] is not None and row[0] > 0]

        # Median time to first memory
        c.execute('''
            SELECT EXTRACT(EPOCH FROM (MIN(m.created_at) - u.created_at)) as seconds_to_first
            FROM users u
            JOIN memories m ON m.phone_number = u.phone_number
            WHERE u.onboarding_complete = TRUE
            GROUP BY u.phone_number, u.created_at
            ORDER BY seconds_to_first
        ''')
        memory_times = [float(row[0]) for row in c.fetchall() if row[0] is not None and row[0] > 0]

        def median(lst):
            if not lst:
                return None
            s = sorted(lst)
            n = len(s)
            mid = n // 2
            return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2

        def format_duration(seconds):
            if seconds is None:
                return None
            seconds = int(seconds)
            if seconds < 60:
                return f"{seconds}s"
            elif seconds < 3600:
                return f"{seconds // 60}m {seconds % 60}s"
            elif seconds < 86400:
                hours = seconds // 3600
                mins = (seconds % 3600) // 60
                return f"{hours}h {mins}m"
            else:
                days = seconds // 86400
                hours = (seconds % 86400) // 3600
                return f"{days}d {hours}h"

        median_reminder = median(reminder_times)
        median_memory = median(memory_times)

        # % who set reminder within 1 hour / 24 hours
        within_1h = len([t for t in reminder_times if t <= 3600])
        within_24h = len([t for t in reminder_times if t <= 86400])

        # % who never set a reminder
        c.execute('''
            SELECT COUNT(*) FROM users u
            WHERE u.onboarding_complete = TRUE
            AND NOT EXISTS (SELECT 1 FROM reminders r WHERE r.phone_number = u.phone_number)
        ''')
        never_set_reminder = c.fetchone()[0]

        result['time_to_action'] = {
            'median_to_reminder': format_duration(median_reminder),
            'median_to_reminder_seconds': float(median_reminder) if median_reminder is not None else None,
            'median_to_memory': format_duration(median_memory),
            'median_to_memory_seconds': float(median_memory) if median_memory is not None else None,
            'pct_within_1h': round(within_1h / total_onboarded * 100, 1) if total_onboarded > 0 else 0,
            'pct_within_24h': round(within_24h / total_onboarded * 100, 1) if total_onboarded > 0 else 0,
            'pct_never_reminder': round(never_set_reminder / total_onboarded * 100, 1) if total_onboarded > 0 else 0,
            'users_with_reminder': len(reminder_times),
            'users_with_memory': len(memory_times),
            'total_onboarded': total_onboarded,
        }

        # =================================================================
        # 5. DAILY ACTIVE USERS TREND (last 30 days)
        # =================================================================
        c.execute('''
            SELECT d::date as day,
                   COUNT(DISTINCT l.phone_number) as active_users
            FROM generate_series(
                NOW() - INTERVAL '30 days',
                NOW(),
                '1 day'::interval
            ) d
            LEFT JOIN logs l ON l.created_at::date = d::date
            LEFT JOIN users u ON u.phone_number = l.phone_number AND u.onboarding_complete = TRUE
            GROUP BY d::date
            ORDER BY d::date
        ''')
        dau_trend = [{'date': str(row[0]), 'active_users': row[1]} for row in c.fetchall()]
        result['dau_trend'] = dau_trend

        # =================================================================
        # 6. RETENTION COHORTS (by signup week)
        # =================================================================
        c.execute('''
            SELECT
                DATE_TRUNC('week', u.created_at)::date as cohort_week,
                COUNT(*) as cohort_size,
                COUNT(*) FILTER (
                    WHERE EXISTS (
                        SELECT 1 FROM logs l
                        WHERE l.phone_number = u.phone_number
                        AND l.created_at::date = u.created_at::date + INTERVAL '1 day'
                    )
                ) as day1_retained,
                COUNT(*) FILTER (
                    WHERE u.created_at < NOW() - INTERVAL '8 days'
                    AND EXISTS (
                        SELECT 1 FROM logs l
                        WHERE l.phone_number = u.phone_number
                        AND l.created_at >= u.created_at + INTERVAL '2 days'
                        AND l.created_at < u.created_at + INTERVAL '8 days'
                    )
                ) as day7_retained,
                COUNT(*) FILTER (
                    WHERE u.created_at < NOW() - INTERVAL '15 days'
                    AND EXISTS (
                        SELECT 1 FROM logs l
                        WHERE l.phone_number = u.phone_number
                        AND l.created_at >= u.created_at + INTERVAL '8 days'
                        AND l.created_at < u.created_at + INTERVAL '15 days'
                    )
                ) as day14_retained,
                COUNT(*) FILTER (WHERE u.created_at < NOW() - INTERVAL '8 days') as day7_eligible,
                COUNT(*) FILTER (WHERE u.created_at < NOW() - INTERVAL '15 days') as day14_eligible
            FROM users u
            WHERE u.onboarding_complete = TRUE
            AND u.created_at >= NOW() - INTERVAL '90 days'
            GROUP BY DATE_TRUNC('week', u.created_at)
            ORDER BY cohort_week DESC
            LIMIT 12
        ''')
        cohorts = []
        for row in c.fetchall():
            cohort = {
                'week': str(row[0]),
                'size': row[1],
                'day1_pct': round(row[2] / row[1] * 100, 1) if row[1] > 0 else None,
                'day7_pct': round(row[3] / row[5] * 100, 1) if row[5] > 0 else None,
                'day14_pct': round(row[4] / row[6] * 100, 1) if row[6] > 0 else None,
            }
            cohorts.append(cohort)
        result['cohorts'] = cohorts

        return result

    except Exception as e:
        logger.error(f"Error getting product metrics: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {'error': str(e)}
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
            'daily_signups': daily_signups,
            'lifecycle_messages': get_lifecycle_message_stats()
        }
    except Exception as e:
        logger.error(f"Error getting all metrics: {e}")
        return {}
    finally:
        if conn:
            return_db_connection(conn)
