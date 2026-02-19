"""
Celery Tasks for SMS Reminder Processing
Implements atomic reminder claiming with SELECT FOR UPDATE SKIP LOCKED.
"""

from celery import shared_task
from celery.utils.log import get_task_logger

from celery_app import celery_app
from models.reminder import (
    claim_due_reminders,
    mark_reminder_sent,
    update_last_sent_reminder,
    release_stale_claims,
    get_all_active_recurring_reminders,
    get_recurring_reminder_by_id,
    save_reminder_with_local_time,
    check_reminder_exists_for_recurring,
    update_recurring_reminder_generated,
)
from services.sms_service import send_sms
from services.metrics_service import track_reminder_delivery

logger = get_task_logger(__name__)


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def check_and_send_reminders(self):
    """
    Periodic task to check for due reminders and dispatch them.
    Uses SELECT FOR UPDATE SKIP LOCKED for atomic claiming.

    This task runs every 30 seconds via Celery Beat.
    Each invocation claims a batch of reminders atomically.
    """
    try:
        # Claim up to 10 reminders atomically
        reminders = claim_due_reminders(batch_size=10)

        if not reminders:
            logger.debug("No due reminders found")
            return {"processed": 0}

        logger.info(f"Claimed {len(reminders)} reminders for processing")

        # Dispatch individual send tasks for each reminder
        for reminder in reminders:
            try:
                logger.info(f"Dispatching reminder {reminder['id']} for {reminder['phone_number'][-4:]}: {reminder['reminder_text'][:30]}")
                result = send_single_reminder.delay(
                    reminder_id=reminder["id"],
                    phone_number=reminder["phone_number"],
                    reminder_text=reminder["reminder_text"],
                )
                logger.info(f"[DISPATCH SUCCESS] reminder {reminder['id']} queued with task_id={result.id}")
            except Exception as dispatch_err:
                logger.exception(f"[DISPATCH FAILED] reminder {reminder['id']}: {dispatch_err}")

        return {"processed": len(reminders)}

    except Exception as exc:
        logger.exception("Error in check_and_send_reminders")
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    retry_backoff=True,
    retry_backoff_max=300,
)
def send_single_reminder(self, reminder_id: int, phone_number: str, reminder_text: str):
    """
    Send a single SMS reminder via Twilio.
    Marks reminder as sent only after successful delivery.

    Uses exponential backoff for retries (30s, 60s, 120s, max 300s).
    IMPORTANT: Once SMS is sent, we do NOT retry even if subsequent operations fail.

    CRITICAL: We hold the FOR UPDATE lock throughout the entire send+mark operation
    to prevent race conditions where mark_reminder_sent fails and the reminder
    gets picked up again.
    """
    # Log immediately at task start - before ANY other operations
    # Use defensive logging in case phone_number is None
    phone_suffix = phone_number[-4:] if phone_number else "NONE"
    logger.info(f"[TASK START] send_single_reminder received: reminder_id={reminder_id}, phone={phone_suffix}")

    # Validate inputs early
    if not phone_number:
        logger.error(f"[INVALID INPUT] reminder_id={reminder_id} has no phone_number!")
        return {"reminder_id": reminder_id, "status": "error", "reason": "no_phone"}

    from database import get_db_connection, return_db_connection

    conn = None
    try:
        conn = get_db_connection()
        logger.info(f"[DB] Got connection for reminder {reminder_id}")
    except Exception as e:
        logger.error(f"[DB ERROR] Failed to get connection for reminder {reminder_id}: {e}")
        raise self.retry(exc=e)
    try:
        c = conn.cursor()

        # Lock the row for the entire operation - prevents race conditions
        c.execute('SELECT sent FROM reminders WHERE id = %s FOR UPDATE', (reminder_id,))
        result = c.fetchone()

        if result and result[0]:
            logger.warning(f"Reminder {reminder_id} already sent (sent={result[0]}), skipping duplicate")
            conn.commit()
            return {
                "reminder_id": reminder_id,
                "status": "already_sent",
            }

        logger.info(f"Reminder {reminder_id} not yet sent, proceeding to send")

        # Try to send SMS - if this fails, we CAN retry (rollback releases lock)
        try:
            logger.info(f"Sending reminder {reminder_id} to {phone_number}")

            # Format message with friendly opener and snooze option
            import random
            openers = [
                "Hey, just a heads up",
                "Quick reminder",
                "Don't forget",
                "Friendly reminder",
            ]
            opener = random.choice(openers)
            message = f"{opener} — {reminder_text}\n\n(Reply SNOOZE to snooze 15 min)"

            # Send SMS via Twilio
            send_sms(phone_number, message)

        except Exception as exc:
            # SMS failed - rollback to release lock, then retry
            conn.rollback()
            logger.exception(f"Error sending SMS for reminder {reminder_id}")
            track_reminder_delivery(reminder_id, "failed", str(exc))
            raise self.retry(exc=exc)

        # SMS sent successfully - mark as sent
        # Release the FOR UPDATE lock first, then use a fresh connection to update
        # This avoids any connection state issues with the long-held transaction
        try:
            conn.commit()  # Release the FOR UPDATE lock
            logger.info(f"Released lock for reminder {reminder_id}, now marking as sent")
        except Exception as e:
            logger.error(f"Failed to release lock for reminder {reminder_id}: {e}")

        # Use a completely fresh connection to mark as sent
        # This ensures no transaction state issues
        from database import get_db_connection as get_fresh_conn, return_db_connection as return_fresh_conn
        mark_conn = None
        try:
            mark_conn = get_fresh_conn()
            mark_cursor = mark_conn.cursor()
            mark_cursor.execute('UPDATE reminders SET sent = TRUE WHERE id = %s', (reminder_id,))
            mark_conn.commit()

            # Verify the update actually persisted
            mark_cursor.execute('SELECT sent FROM reminders WHERE id = %s', (reminder_id,))
            verify_result = mark_cursor.fetchone()
            if verify_result and verify_result[0]:
                logger.info(f"[VERIFIED] Reminder {reminder_id} marked as sent (sent={verify_result[0]})")
            else:
                logger.error(f"[VERIFY FAILED] Reminder {reminder_id} sent flag is still {verify_result}")
                # Try one more time with explicit transaction
                mark_cursor.execute('BEGIN')
                mark_cursor.execute('UPDATE reminders SET sent = TRUE WHERE id = %s', (reminder_id,))
                mark_cursor.execute('COMMIT')
                logger.info(f"Retried with explicit BEGIN/COMMIT for reminder {reminder_id}")
        except Exception as e:
            logger.exception(f"Failed to mark reminder {reminder_id} as sent: {e}")
        finally:
            if mark_conn:
                return_fresh_conn(mark_conn)

    except Exception as outer_exc:
        # Catch-all for any unexpected errors
        logger.exception(f"[UNEXPECTED ERROR] in send_single_reminder for {reminder_id}: {outer_exc}")
        if conn:
            try:
                conn.rollback()
            except:
                pass
        raise self.retry(exc=outer_exc)
    finally:
        if conn:
            return_db_connection(conn)

    # These are nice-to-have, don't retry if they fail
    try:
        update_last_sent_reminder(phone_number, reminder_id)
        logger.info(f"Updated last_sent_reminder for {phone_number[-4:]} to reminder {reminder_id}")
    except Exception as e:
        logger.error(f"Failed to update last_sent_reminder: {e}")

    try:
        track_reminder_delivery(reminder_id, "sent")
    except Exception as e:
        logger.error(f"Failed to track delivery metrics: {e}")

    logger.info(f"[TASK COMPLETE] Reminder {reminder_id} sent successfully")
    return {
        "reminder_id": reminder_id,
        "status": "sent",
    }


@celery_app.task
def release_stale_claims_task():
    """
    Release reminders that were claimed but not processed.

    This handles cases where a worker crashes after claiming
    but before sending. Runs every 5 minutes via Beat.
    """
    try:
        count = release_stale_claims(timeout_minutes=5)
        if count > 0:
            logger.warning(f"Released {count} stale reminder claims")
        return {"released": count}
    except Exception as exc:
        logger.exception("Error releasing stale claims")
        raise


@celery_app.task
def analyze_conversations_task():
    """
    Analyze recent conversations for quality issues.
    Runs every 4 hours via Beat.
    """
    try:
        from services.conversation_analyzer import analyze_recent_conversations
        result = analyze_recent_conversations(batch_size=50)
        logger.info(f"Conversation analysis complete: {result}")
        return result
    except Exception as exc:
        logger.exception("Error in conversation analysis task")
        raise


# =====================================================
# RECURRING REMINDER FUNCTIONS
# =====================================================

def should_trigger_on_date(recurrence_type, recurrence_day, dt):
    """
    Check if a recurring reminder should trigger on a given date.

    Args:
        recurrence_type: 'daily', 'weekly', 'weekdays', 'weekends', 'monthly'
        recurrence_day: Day of week (0-6) for weekly, day of month (1-31) for monthly
        dt: The datetime to check

    Returns:
        True if reminder should trigger on this date
    """
    weekday = dt.weekday()  # 0=Monday, 6=Sunday

    if recurrence_type == 'daily':
        return True
    elif recurrence_type == 'weekly':
        return weekday == recurrence_day
    elif recurrence_type == 'weekdays':
        return weekday < 5  # Mon-Fri
    elif recurrence_type == 'weekends':
        return weekday >= 5  # Sat-Sun
    elif recurrence_type == 'monthly':
        # Handle months with fewer days
        import calendar
        last_day = calendar.monthrange(dt.year, dt.month)[1]
        target_day = min(recurrence_day, last_day)
        return dt.day == target_day
    return False


def generate_first_occurrence(recurring_id):
    """
    Generate the first occurrence for a newly created recurring reminder.
    Called immediately when user creates a recurring reminder.

    Args:
        recurring_id: The recurring reminder ID

    Returns:
        The datetime of the next occurrence, or None if error
    """
    import pytz
    from datetime import datetime, timedelta

    try:
        recurring = get_recurring_reminder_by_id(recurring_id)
        if not recurring:
            logger.error(f"Recurring reminder {recurring_id} not found")
            return None

        # Parse time
        time_parts = recurring['reminder_time'].split(':')
        hour = int(time_parts[0])
        minute = int(time_parts[1].split(':')[0]) if ':' in time_parts[1] else int(time_parts[1])

        # Get user's timezone
        user_tz = pytz.timezone(recurring['timezone'])
        now = datetime.now(user_tz)

        # Find next occurrence
        check_date = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # If time already passed today, start from tomorrow
        if check_date <= now:
            check_date = check_date + timedelta(days=1)

        # Find the next date that matches the recurrence pattern
        max_days = 366  # Don't search more than a year
        for _ in range(max_days):
            if should_trigger_on_date(
                recurring['recurrence_type'],
                recurring['recurrence_day'],
                check_date
            ):
                break
            check_date = check_date + timedelta(days=1)
        else:
            logger.error(f"Could not find next occurrence for recurring {recurring_id}")
            return None

        # Convert to UTC FIRST (must match what's stored in DB)
        utc_dt = check_date.astimezone(pytz.UTC)

        # Check if reminder already exists for this date (using UTC date to match DB)
        if check_reminder_exists_for_recurring(recurring_id, utc_dt.date()):
            logger.info(f"Reminder already exists for recurring {recurring_id} on {utc_dt.date()}")
            # Return the next occurrence datetime anyway
            update_recurring_reminder_generated(recurring_id, utc_dt.date(), utc_dt)
            return utc_dt

        # Create the reminder
        reminder_id = save_reminder_with_local_time(
            phone_number=recurring['phone_number'],
            reminder_text=recurring['reminder_text'],
            reminder_date=utc_dt.strftime('%Y-%m-%d %H:%M:%S'),
            local_time=recurring['reminder_time'],
            timezone=recurring['timezone'],
            recurring_id=recurring_id
        )

        if reminder_id:
            logger.info(f"Generated first occurrence for recurring {recurring_id}: reminder {reminder_id} at {utc_dt}")
            # Update the recurring reminder with generation info
            update_recurring_reminder_generated(recurring_id, utc_dt.date(), utc_dt)
            return utc_dt
        else:
            logger.error(f"Failed to save first occurrence for recurring {recurring_id}")
            return None

    except Exception as e:
        logger.exception(f"Error generating first occurrence for recurring {recurring_id}: {e}")
        return None


@celery_app.task
def generate_recurring_reminders():
    """
    Generate concrete reminders from recurring patterns.
    Runs hourly via Celery Beat.
    Creates reminders for the next 24 hours.

    Returns:
        dict with count of generated reminders
    """
    import pytz
    from datetime import datetime, timedelta

    try:
        recurring_list = get_all_active_recurring_reminders()

        if not recurring_list:
            logger.debug("No active recurring reminders")
            return {"generated": 0}

        logger.info(f"Processing {len(recurring_list)} active recurring reminders")

        generated_count = 0
        hours_ahead = 24  # Generate reminders for next 24 hours

        for recurring in recurring_list:
            try:
                recurring_id = recurring['id']

                # Parse time
                time_str = recurring['reminder_time']
                time_parts = time_str.split(':')
                hour = int(time_parts[0])
                minute = int(time_parts[1].split(':')[0]) if ':' in time_parts[1] else int(time_parts[1])

                # Get user's timezone
                user_tz = pytz.timezone(recurring['timezone'])
                now = datetime.now(user_tz)
                end_time = now + timedelta(hours=hours_ahead)

                # Start checking from now
                check_date = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

                # If time already passed today, start from tomorrow
                if check_date <= now:
                    check_date = check_date + timedelta(days=1)

                # Check each day in the window
                while check_date <= end_time:
                    if should_trigger_on_date(
                        recurring['recurrence_type'],
                        recurring['recurrence_day'],
                        check_date
                    ):
                        # Convert to UTC FIRST (must match what's stored in DB)
                        utc_dt = check_date.astimezone(pytz.UTC)

                        # Check if reminder already exists for this date (using UTC date to match DB)
                        if not check_reminder_exists_for_recurring(recurring_id, utc_dt.date()):
                            # Create the reminder
                            reminder_id = save_reminder_with_local_time(
                                phone_number=recurring['phone_number'],
                                reminder_text=recurring['reminder_text'],
                                reminder_date=utc_dt.strftime('%Y-%m-%d %H:%M:%S'),
                                local_time=time_str,
                                timezone=recurring['timezone'],
                                recurring_id=recurring_id
                            )

                            if reminder_id:
                                logger.info(f"Generated reminder {reminder_id} for recurring {recurring_id} at {utc_dt}")
                                generated_count += 1

                                # Update next occurrence
                                update_recurring_reminder_generated(recurring_id, utc_dt.date(), utc_dt)

                    check_date = check_date + timedelta(days=1)

            except Exception as e:
                logger.error(f"Error processing recurring {recurring['id']}: {e}")
                continue

        logger.info(f"Generated {generated_count} reminders from recurring patterns")
        return {"generated": generated_count}

    except Exception as exc:
        logger.exception("Error in generate_recurring_reminders")
        raise


# =====================================================
# DAILY SUMMARY FUNCTIONS
# =====================================================

@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def send_daily_summaries(self):
    """
    Periodic task to send daily reminder summaries.
    Runs every minute via Celery Beat.

    For each minute, checks which users have their summary time set to
    that minute (in their local timezone) and sends their daily summary.
    """
    import pytz
    from datetime import datetime
    from models.user import get_users_due_for_daily_summary, claim_user_for_daily_summary
    from models.reminder import get_reminders_for_date

    try:
        utc_now = datetime.now(pytz.UTC)

        # Get users whose local time matches their summary time preference
        due_users = get_users_due_for_daily_summary()

        if not due_users:
            logger.debug("No users due for daily summary")
            return {"sent": 0}

        logger.info(f"Checking daily summaries for {len(due_users)} candidates")

        sent_count = 0
        for user in due_users:
            try:
                phone_number = user['phone_number']
                timezone_str = user['timezone']
                first_name = user.get('first_name', '')

                # Get user's local date
                user_tz = pytz.timezone(timezone_str)
                user_now = utc_now.astimezone(user_tz)
                user_today = user_now.date()

                # Atomically claim this user to prevent duplicates from concurrent workers
                if not claim_user_for_daily_summary(phone_number, user_today):
                    # Already claimed by another worker or already sent today
                    continue

                # Get today's reminders
                reminders = get_reminders_for_date(phone_number, user_today, timezone_str)

                # Format and send summary
                message = format_daily_summary(reminders, first_name, user_today, user_tz)
                send_sms(phone_number, message)

                sent_count += 1

                logger.info(f"Sent daily summary to {phone_number[-4:]} ({len(reminders)} reminders)")

            except Exception as e:
                logger.error(f"Error sending daily summary to {user['phone_number'][-4:]}: {e}")
                continue

        return {"sent": sent_count}

    except Exception as exc:
        logger.exception("Error in send_daily_summaries")
        raise self.retry(exc=exc)


def format_daily_summary(reminders, first_name, date, user_tz):
    """Format the daily summary message.

    Args:
        reminders: List of (id, reminder_text, reminder_date_utc) tuples
        first_name: User's first name (may be None)
        date: User's local date
        user_tz: User's pytz timezone object

    Returns:
        Formatted summary message string
    """
    import pytz
    from datetime import datetime

    # Build greeting
    greeting = f"Good day{', ' + first_name if first_name else ''}!"

    # Format date
    date_str = date.strftime('%A, %B %d')

    if not reminders:
        return f"{greeting}\n\nNo reminders scheduled for today ({date_str}). Enjoy your day!"

    # Build reminder list
    lines = [
        f"{greeting}",
        "",
        f"Your reminders for {date_str}:",
        ""
    ]

    for i, (reminder_id, text, reminder_date_utc) in enumerate(reminders, 1):
        # Convert UTC to local time for display
        try:
            if isinstance(reminder_date_utc, datetime):
                utc_dt = reminder_date_utc
                if utc_dt.tzinfo is None:
                    utc_dt = pytz.UTC.localize(utc_dt)
            else:
                utc_dt = datetime.strptime(str(reminder_date_utc), '%Y-%m-%d %H:%M:%S')
                utc_dt = pytz.UTC.localize(utc_dt)

            local_dt = utc_dt.astimezone(user_tz)
            time_str = local_dt.strftime('%I:%M %p').lstrip('0')
        except (ValueError, TypeError, AttributeError):
            time_str = "TBD"

        lines.append(f"{i}. {time_str} - {text}")

    lines.append("")
    lines.append("(You'll still receive each reminder at its scheduled time)")

    return "\n".join(lines)


@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def send_abandoned_onboarding_followups(self):
    """
    Periodic task to send follow-up messages to abandoned onboardings.
    Runs every hour via Celery Beat.
    """
    from services.onboarding_recovery_service import (
        get_abandoned_onboardings_24h,
        get_abandoned_onboardings_7d,
        mark_followup_sent,
        build_24h_followup_message,
        build_7d_followup_message,
    )

    try:
        sent_count = 0

        # 24-hour follow-ups
        abandoned_24h = get_abandoned_onboardings_24h()
        for user in abandoned_24h:
            try:
                message = build_24h_followup_message(
                    user['first_name'],
                    user['current_step']
                )
                send_sms(user['phone_number'], message)
                mark_followup_sent(user['phone_number'], '24h')
                sent_count += 1
                logger.info(f"Sent 24h onboarding followup to ...{user['phone_number'][-4:]}")
            except Exception as e:
                logger.error(f"Error sending 24h followup: {e}")

        # 7-day follow-ups (final attempt)
        abandoned_7d = get_abandoned_onboardings_7d()
        for user in abandoned_7d:
            try:
                message = build_7d_followup_message(user['first_name'])
                send_sms(user['phone_number'], message)
                mark_followup_sent(user['phone_number'], '7d')
                sent_count += 1
                logger.info(f"Sent 7d onboarding followup to ...{user['phone_number'][-4:]}")
            except Exception as e:
                logger.error(f"Error sending 7d followup: {e}")

        logger.info(f"Abandoned onboarding followups complete: {sent_count} sent")
        return {"followups_sent": sent_count}

    except Exception as exc:
        logger.exception("Error in abandoned onboarding followups")
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def send_delayed_sms(self, to_number: str, message: str, media_url: str = None):
    """
    Send an SMS/MMS with optional delay (use .apply_async(countdown=N)).
    Used for onboarding VCF card delivery with delay.
    """
    try:
        send_sms(to_number, message, media_url=media_url)
        logger.info(f"Sent delayed SMS to ...{to_number[-4:]}")
        return {"success": True}
    except Exception as exc:
        logger.error(f"Error sending delayed SMS: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def send_engagement_nudge(self, phone_number: str):
    """
    Send 5-minute post-onboarding engagement nudge.
    Checks all conditions at execution time for safety.

    Conditions checked:
    - Nudge not already sent
    - Nudge not cancelled (scheduled_at still set)
    - User not opted out (STOP)
    - User has sent fewer than 2 messages since onboarding
    """
    from models.user import get_user_nudge_status, create_or_update_user
    from twilio.base.exceptions import TwilioException

    logger.info(f"Engagement nudge task started for user ...{phone_number[-4:]}")

    try:
        # Fetch fresh user nudge status
        status = get_user_nudge_status(phone_number)
        logger.info(f"Nudge status for ...{phone_number[-4:]}: sent={status['sent']}, "
                   f"scheduled_at={status['scheduled_at']}, interactions={status['interactions']}, "
                   f"opted_out={status['opted_out']}")

        # Check: Already sent
        if status['sent']:
            logger.info(f"Engagement nudge already sent for ...{phone_number[-4:]}, skipping")
            return {"status": "skipped", "reason": "already_sent"}

        # Check: Cancelled (scheduled_at cleared)
        if status['scheduled_at'] is None:
            logger.info(f"Engagement nudge cancelled for ...{phone_number[-4:]} (user engaged), skipping")
            return {"status": "skipped", "reason": "cancelled"}

        # Check: User opted out
        if status['opted_out']:
            logger.info(f"User ...{phone_number[-4:]} opted out, skipping engagement nudge")
            # Mark as sent to prevent future attempts
            create_or_update_user(phone_number, five_minute_nudge_sent=True)
            return {"status": "skipped", "reason": "opted_out"}

        # Check: User has sent 2+ messages since onboarding
        if status['interactions'] >= 2:
            logger.info(f"User ...{phone_number[-4:]} has {status['interactions']} interactions, "
                       f"skipping engagement nudge (already engaged)")
            # Mark as sent to prevent future attempts
            create_or_update_user(phone_number, five_minute_nudge_sent=True)
            return {"status": "skipped", "reason": "already_engaged",
                    "interactions": status['interactions']}

        # All conditions passed - send the nudge
        nudge_message = "Quick question: What's something you always forget?\n\n(I'm really good at remembering it for you 😊)"

        try:
            send_sms(phone_number, nudge_message)
            # Mark as sent
            create_or_update_user(phone_number, five_minute_nudge_sent=True)
            logger.info(f"Successfully sent engagement nudge to ...{phone_number[-4:]}")
            return {"status": "sent"}

        except TwilioException as twilio_exc:
            # Retry on Twilio errors
            logger.error(f"Twilio error sending engagement nudge to ...{phone_number[-4:]}: {twilio_exc}")
            raise self.retry(exc=twilio_exc)

    except TwilioException:
        # Re-raise to trigger retry
        raise

    except Exception as exc:
        # Log other errors but don't retry - this is a non-critical message
        logger.error(f"Error in engagement nudge task for ...{phone_number[-4:]}: {exc}")
        return {"status": "error", "error": str(exc)}


# =====================================================
# TRIAL EXPIRATION WARNINGS
# =====================================================

@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def check_trial_expirations(self):
    """
    Check for users whose trials are expiring soon and send warnings.
    Sends warnings on:
    - Day 7: 7 days remaining
    - Day 1: 1 day remaining
    - Day 0: Trial expired (downgraded to free)

    Runs daily via Celery Beat.
    Tracks sent warnings to avoid duplicates.
    """
    import pytz
    from datetime import datetime
    from database import get_db_connection, return_db_connection
    from models.user import create_or_update_user
    from models.list_model import get_list_count
    from services.tier_service import get_memory_count
    from config import PREMIUM_MONTHLY_PRICE, PREMIUM_ANNUAL_PRICE

    logger.info("Starting trial expiration check")

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get all users with active trials (trial_end_date in the future and premium_status = 'premium')
        # Also get their warning status columns
        now_utc = datetime.utcnow()

        c.execute("""
            SELECT phone_number, first_name, trial_end_date,
                   trial_warning_7d_sent, trial_warning_1d_sent, trial_warning_0d_sent
            FROM users
            WHERE trial_end_date IS NOT NULL
              AND trial_end_date > %s - INTERVAL '8 days'
              AND onboarding_complete = TRUE
              AND (opted_out IS NULL OR opted_out = FALSE)
              AND (stripe_subscription_id IS NULL OR subscription_status != 'active')
        """, (now_utc,))

        users = c.fetchall()

        if not users:
            logger.info("No users with active or recently expired trials")
            return {"warnings_sent": 0}

        logger.info(f"Checking {len(users)} users with trials")

        warnings_sent = 0

        for user in users:
            phone_number, first_name, trial_end_date, warning_7d_sent, warning_1d_sent, warning_0d_sent = user

            # Calculate days remaining
            time_remaining = trial_end_date - now_utc
            days_remaining = time_remaining.days

            # Determine which warning to send
            warning_to_send = None
            update_field = None

            if 6 <= days_remaining <= 7 and not warning_7d_sent:
                # 7 days remaining warning — personalized with usage stats
                # (Also serves as mid-trial value reminder to avoid double-message)
                from services.tier_service import get_recurring_reminder_count

                c.execute("SELECT COUNT(*) FROM reminders WHERE phone_number = %s", (phone_number,))
                reminder_count = (c.fetchone() or (0,))[0]
                list_count = get_list_count(phone_number)
                memory_count = get_memory_count(phone_number)
                recurring_count = get_recurring_reminder_count(phone_number)

                greeting = f"Hi {first_name}!" if first_name else "Hi there!"

                accomplishments = []
                if reminder_count > 0:
                    accomplishments.append(f"  ✓ {reminder_count} reminder{'s' if reminder_count != 1 else ''} created")
                if list_count > 0:
                    accomplishments.append(f"  ✓ {list_count} list{'s' if list_count != 1 else ''} organized")
                if memory_count > 0:
                    accomplishments.append(f"  ✓ {memory_count} memor{'ies' if memory_count != 1 else 'y'} saved")
                if recurring_count > 0:
                    accomplishments.append(f"  ✓ {recurring_count} recurring reminder{'s' if recurring_count != 1 else ''}")

                stats_block = ""
                if accomplishments:
                    stats_block = "\n\nSo far you've:\n" + "\n".join(accomplishments)

                warning_to_send = f"""{greeting} You have 7 days left in your Premium trial! ⏰{stats_block}

After your trial, you'll move to the free plan (2 reminders/day).

Text UPGRADE to keep unlimited reminders — {PREMIUM_MONTHLY_PRICE}/mo or {PREMIUM_ANNUAL_PRICE}/yr ($7.50/mo)."""
                update_field = 'trial_warning_7d_sent'

                # Also mark mid-trial reminder as sent to prevent duplicate
                create_or_update_user(phone_number, mid_trial_reminder_sent=True)

            elif 0 < days_remaining <= 1 and not warning_1d_sent:
                # 1 day remaining warning — include usage stats
                c.execute("SELECT COUNT(*) FROM reminders WHERE phone_number = %s", (phone_number,))
                reminder_count = (c.fetchone() or (0,))[0]
                list_count = get_list_count(phone_number)
                memory_count = get_memory_count(phone_number)

                stats_parts = []
                if reminder_count > 0:
                    stats_parts.append(f"{reminder_count} reminder{'s' if reminder_count != 1 else ''}")
                if list_count > 0:
                    stats_parts.append(f"{list_count} list{'s' if list_count != 1 else ''}")
                if memory_count > 0:
                    stats_parts.append(f"{memory_count} memor{'ies' if memory_count != 1 else 'y'}")

                stats_line = ""
                if stats_parts:
                    stats_line = f" You've created {', '.join(stats_parts)} so far."

                warning_to_send = f"""Tomorrow is your last day of Premium trial! ⏰{stats_line}

After that, you'll be on the free plan (2 reminders/day).

Text UPGRADE now — {PREMIUM_MONTHLY_PRICE}/mo or {PREMIUM_ANNUAL_PRICE}/yr ($7.50/mo)."""
                update_field = 'trial_warning_1d_sent'

            elif days_remaining <= 0 and not warning_0d_sent:
                # Trial expired - downgrade to free and send message
                create_or_update_user(phone_number, premium_status='free')

                warning_to_send = f"""Your Premium trial has ended. You're now on the free plan:
• 2 reminders/day
• 5 lists, 5 memories
• Existing recurring reminders keep working, but you can't create new ones

All your data is safe!

Want unlimited access back? Text UPGRADE — {PREMIUM_MONTHLY_PRICE}/mo or {PREMIUM_ANNUAL_PRICE}/yr ($7.50/mo)."""
                update_field = 'trial_warning_0d_sent'

            # Send warning if needed
            if warning_to_send and update_field:
                try:
                    send_sms(phone_number, warning_to_send)

                    # Mark warning as sent
                    create_or_update_user(phone_number, **{update_field: True})

                    warnings_sent += 1
                    logger.info(f"Sent trial warning (day {days_remaining}) to ...{phone_number[-4:]}")

                except Exception as sms_error:
                    logger.error(f"Failed to send trial warning to ...{phone_number[-4:]}: {sms_error}")
                    continue

        logger.info(f"Trial expiration check complete: {warnings_sent} warnings sent")
        return {"warnings_sent": warnings_sent}

    except Exception as exc:
        logger.exception("Error in check_trial_expirations")
        raise self.retry(exc=exc)

    finally:
        if conn:
            return_db_connection(conn)


@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def send_mid_trial_value_reminders(self):
    """
    Send value reminder on Day 7 of trial showing what users have accomplished.
    Highlights features used and encourages continued engagement.

    Runs daily via Celery Beat.
    Only sends once per user (tracks with mid_trial_reminder_sent flag).
    """
    import pytz
    from datetime import datetime
    from database import get_db_connection, return_db_connection
    from models.user import create_or_update_user
    from models.list_model import get_list_count
    from services.tier_service import get_memory_count

    logger.info("Starting mid-trial value reminder check")

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get users on Day 7 of trial (7 days remaining) who haven't received this reminder
        # Also skip users who already got the 7d trial warning (to avoid double-message)
        now_utc = datetime.utcnow()

        c.execute("""
            SELECT phone_number, first_name, trial_end_date
            FROM users
            WHERE trial_end_date IS NOT NULL
              AND trial_end_date > %s
              AND trial_end_date <= %s + INTERVAL '8 days'
              AND onboarding_complete = TRUE
              AND (mid_trial_reminder_sent IS NULL OR mid_trial_reminder_sent = FALSE)
              AND (trial_warning_7d_sent IS NULL OR trial_warning_7d_sent = FALSE)
              AND (opted_out IS NULL OR opted_out = FALSE)
        """, (now_utc, now_utc))

        users = c.fetchall()

        if not users:
            logger.info("No users due for mid-trial value reminder")
            return {"reminders_sent": 0}

        logger.info(f"Checking {len(users)} users for mid-trial reminder")

        reminders_sent = 0

        for user in users:
            phone_number, first_name, trial_end_date = user

            # Calculate exact days remaining
            time_remaining = trial_end_date - now_utc
            days_remaining = time_remaining.days

            # Only send on Day 7 (6-7 days remaining, range for signup time-of-day tolerance)
            if not (6 <= days_remaining <= 7):
                continue

            try:
                # Get user's usage stats
                from services.tier_service import get_reminders_created_today, get_recurring_reminder_count

                # Get total reminders count
                c.execute("SELECT COUNT(*) FROM reminders WHERE phone_number = %s", (phone_number,))
                result = c.fetchone()
                total_reminders = result[0] if result else 0

                list_count = get_list_count(phone_number)
                memory_count = get_memory_count(phone_number)
                recurring_count = get_recurring_reminder_count(phone_number)

                # Build personalized value message
                greeting = f"Hi {first_name}!" if first_name else "Hi there!"

                message_lines = [
                    f"{greeting} You're halfway through your Premium trial! 🎉",
                    ""
                ]

                # Show what they've accomplished
                accomplishments = []
                if total_reminders > 0:
                    accomplishments.append(f"✓ {total_reminders} reminder{'s' if total_reminders != 1 else ''} created")
                if list_count > 0:
                    accomplishments.append(f"✓ {list_count} list{'s' if list_count != 1 else ''} organized")
                if memory_count > 0:
                    accomplishments.append(f"✓ {memory_count} memor{'ies' if memory_count != 1 else 'y'} saved")
                if recurring_count > 0:
                    accomplishments.append(f"✓ {recurring_count} recurring reminder{'s' if recurring_count != 1 else ''}")

                if accomplishments:
                    message_lines.append("So far you've:")
                    message_lines.extend(accomplishments)
                    message_lines.append("")

                # Reminder about trial ending
                message_lines.extend([
                    "Your trial ends in 7 days. After that, you'll move to the free plan (2 reminders/day).",
                    "",
                    "Want to keep unlimited access? Text UPGRADE anytime!"
                ])

                message = "\n".join(message_lines)

                # Send the reminder
                send_sms(phone_number, message)

                # Mark as sent
                create_or_update_user(phone_number, mid_trial_reminder_sent=True)

                reminders_sent += 1
                logger.info(f"Sent mid-trial value reminder to ...{phone_number[-4:]}")

            except Exception as reminder_error:
                logger.error(f"Failed to send mid-trial reminder to ...{phone_number[-4:]}: {reminder_error}")
                continue

        logger.info(f"Mid-trial value reminders complete: {reminders_sent} sent")
        return {"reminders_sent": reminders_sent}

    except Exception as exc:
        logger.exception("Error in send_mid_trial_value_reminders")
        raise self.retry(exc=exc)

    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# DAY 3 ENGAGEMENT NUDGE
# =====================================================

@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def send_day_3_engagement_nudges(self):
    """
    Send engagement nudge on Day 3 of trial to encourage feature discovery.
    Targets users who signed up ~3 days ago and haven't been very active.

    Runs daily via Celery Beat.
    Only sends once per user (tracks with day_3_nudge_sent flag).
    """
    from datetime import datetime, timedelta
    from database import get_db_connection, return_db_connection
    from models.user import create_or_update_user

    logger.info("Starting Day 3 engagement nudge check")

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        now_utc = datetime.utcnow()

        # Find users on Day 3 of trial (11 days remaining = 3 days since signup for 14-day trial)
        # trial_end_date - 11 days = Day 3 of trial
        c.execute("""
            SELECT phone_number, first_name, trial_end_date
            FROM users
            WHERE trial_end_date IS NOT NULL
              AND trial_end_date > %s
              AND onboarding_complete = TRUE
              AND (day_3_nudge_sent IS NULL OR day_3_nudge_sent = FALSE)
              AND (opted_out IS NULL OR opted_out = FALSE)
        """, (now_utc,))

        users = c.fetchall()

        if not users:
            logger.info("No users due for Day 3 nudge")
            return {"nudges_sent": 0}

        nudges_sent = 0

        for user in users:
            phone_number, first_name, trial_end_date = user

            # Calculate days since signup (trial is 14 days, so days_in = 14 - days_remaining)
            time_remaining = trial_end_date - now_utc
            days_remaining = time_remaining.days
            days_in_trial = 14 - days_remaining

            # Only send on Day 3 (range check to handle signup time-of-day)
            if not (2 <= days_in_trial <= 3):
                continue

            try:
                greeting = f"Hey {first_name}!" if first_name else "Hey there!"

                message = f"""{greeting} You've been on Remyndrs for 3 days now.

Have you tried these yet?
• Save a memory: "Remember my WiFi is ABC123"
• Create a list: "Start a grocery list"
• Set a recurring reminder: "Remind me every Monday at 9am to submit my timesheet"

Just text me naturally — I'll figure out what you need!"""

                send_sms(phone_number, message)
                create_or_update_user(phone_number, day_3_nudge_sent=True)

                nudges_sent += 1
                logger.info(f"Sent Day 3 nudge to ...{phone_number[-4:]}")

            except Exception as e:
                logger.error(f"Failed to send Day 3 nudge to ...{phone_number[-4:]}: {e}")
                continue

        logger.info(f"Day 3 engagement nudges complete: {nudges_sent} sent")
        return {"nudges_sent": nudges_sent}

    except Exception as exc:
        logger.exception("Error in send_day_3_engagement_nudges")
        raise self.retry(exc=exc)

    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# POST-TRIAL RE-ENGAGEMENT
# =====================================================

@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def send_post_trial_reengagement(self):
    """
    Send re-engagement message 3 days after trial expires.
    Targets users who were downgraded to free but haven't upgraded.

    Runs daily via Celery Beat.
    Only sends once per user (tracks with post_trial_reengagement_sent flag).
    """
    from datetime import datetime, timedelta
    from database import get_db_connection, return_db_connection
    from models.user import create_or_update_user
    from config import PREMIUM_MONTHLY_PRICE

    logger.info("Starting post-trial re-engagement check")

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        now_utc = datetime.utcnow()

        # Find users whose trial ended 3 days ago and haven't upgraded
        c.execute("""
            SELECT phone_number, first_name, trial_end_date
            FROM users
            WHERE trial_end_date IS NOT NULL
              AND trial_end_date < %s
              AND onboarding_complete = TRUE
              AND premium_status = 'free'
              AND (post_trial_reengagement_sent IS NULL OR post_trial_reengagement_sent = FALSE)
              AND (opted_out IS NULL OR opted_out = FALSE)
              AND (stripe_subscription_id IS NULL OR subscription_status != 'active')
        """, (now_utc,))

        users = c.fetchall()

        if not users:
            logger.info("No users due for post-trial re-engagement")
            return {"messages_sent": 0}

        messages_sent = 0

        for user in users:
            phone_number, first_name, trial_end_date = user

            # Calculate days since trial ended
            days_since_expiry = (now_utc - trial_end_date).days

            # Only send on Day 3 after expiration (range check to handle signup time-of-day)
            if not (2 <= days_since_expiry <= 3):
                continue

            try:
                greeting = f"Hi {first_name}!" if first_name else "Hi there!"

                message = f"""{greeting} Your Remyndrs data is still here and safe.

You're on the free plan (2 reminders/day). Want unlimited reminders, lists & memories back?

Text UPGRADE for Premium at {PREMIUM_MONTHLY_PRICE}/month — pick up right where you left off."""

                send_sms(phone_number, message)
                create_or_update_user(phone_number, post_trial_reengagement_sent=True)

                messages_sent += 1
                logger.info(f"Sent post-trial re-engagement to ...{phone_number[-4:]}")

            except Exception as e:
                logger.error(f"Failed to send post-trial re-engagement to ...{phone_number[-4:]}: {e}")
                continue

        logger.info(f"Post-trial re-engagement complete: {messages_sent} sent")
        return {"messages_sent": messages_sent}

    except Exception as exc:
        logger.exception("Error in send_post_trial_reengagement")
        raise self.retry(exc=exc)

    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# 14-DAY POST-TRIAL TOUCHPOINT
# =====================================================

@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def send_14d_post_trial_touchpoint(self):
    """
    Send a touchpoint 14 days after trial expires highlighting what free users are missing.
    Focuses on features they actually used during trial to create urgency.

    Runs daily via Celery Beat.
    Only sends once per user (tracks with post_trial_14d_sent flag).
    """
    from datetime import datetime, timedelta
    from database import get_db_connection, return_db_connection
    from models.user import create_or_update_user
    from models.list_model import get_list_count
    from services.tier_service import get_memory_count, get_recurring_reminder_count
    from config import PREMIUM_MONTHLY_PRICE, PREMIUM_ANNUAL_PRICE

    logger.info("Starting 14-day post-trial touchpoint check")

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        now_utc = datetime.utcnow()

        c.execute("""
            SELECT phone_number, first_name, trial_end_date
            FROM users
            WHERE trial_end_date IS NOT NULL
              AND trial_end_date < %s
              AND onboarding_complete = TRUE
              AND premium_status = 'free'
              AND (post_trial_14d_sent IS NULL OR post_trial_14d_sent = FALSE)
              AND (opted_out IS NULL OR opted_out = FALSE)
              AND (stripe_subscription_id IS NULL OR subscription_status != 'active')
        """, (now_utc,))

        users = c.fetchall()

        if not users:
            logger.info("No users due for 14-day post-trial touchpoint")
            return {"messages_sent": 0}

        messages_sent = 0

        for user in users:
            phone_number, first_name, trial_end_date = user

            days_since_expiry = (now_utc - trial_end_date).days

            # Range check to handle signup time-of-day
            if not (13 <= days_since_expiry <= 14):
                continue

            try:
                greeting = f"Hi {first_name}!" if first_name else "Hi there!"

                # Check what features they used during trial
                recurring_count = get_recurring_reminder_count(phone_number)
                list_count = get_list_count(phone_number)
                memory_count = get_memory_count(phone_number)

                # Build a message highlighting what they're missing
                missing_parts = []
                if recurring_count > 0:
                    missing_parts.append(f"Your {recurring_count} recurring reminder{'s are' if recurring_count != 1 else ' is'} paused on the free plan")
                if list_count > 5:
                    missing_parts.append(f"You have {list_count} lists but free only allows 5")
                if memory_count and memory_count > 5:
                    missing_parts.append(f"You have {memory_count} memories but free only allows 5")

                if missing_parts:
                    missing_line = "\n\n" + missing_parts[0] + "."
                else:
                    missing_line = "\n\nOn the free plan, you're limited to 2 reminders/day."

                message = f"""{greeting} It's been 2 weeks since your trial ended.{missing_line}

Text UPGRADE to get unlimited access back — {PREMIUM_MONTHLY_PRICE}/mo or {PREMIUM_ANNUAL_PRICE}/yr."""

                send_sms(phone_number, message)
                create_or_update_user(phone_number, post_trial_14d_sent=True)

                messages_sent += 1
                logger.info(f"Sent 14-day post-trial touchpoint to ...{phone_number[-4:]}")

            except Exception as e:
                logger.error(f"Failed to send 14-day touchpoint to ...{phone_number[-4:]}: {e}")
                continue

        logger.info(f"14-day post-trial touchpoint complete: {messages_sent} sent")
        return {"messages_sent": messages_sent}

    except Exception as exc:
        logger.exception("Error in send_14d_post_trial_touchpoint")
        raise self.retry(exc=exc)

    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# 30-DAY WIN-BACK MESSAGE
# =====================================================

@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def send_30d_winback(self):
    """
    Send win-back message 30 days after trial expires.
    Targets users who were downgraded to free and never upgraded.

    Runs daily via Celery Beat.
    Only sends once per user (tracks with winback_30d_sent flag).
    """
    from datetime import datetime, timedelta
    from database import get_db_connection, return_db_connection
    from models.user import create_or_update_user
    from config import PREMIUM_MONTHLY_PRICE, PREMIUM_ANNUAL_PRICE

    logger.info("Starting 30-day win-back check")

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        now_utc = datetime.utcnow()
        target_date = now_utc - timedelta(days=30)
        # Window: trial ended between 30 and 31 days ago
        window_start = target_date - timedelta(days=1)

        c.execute("""
            SELECT phone_number, first_name, trial_end_date
            FROM users
            WHERE trial_end_date IS NOT NULL
              AND trial_end_date >= %s
              AND trial_end_date < %s
              AND onboarding_complete = TRUE
              AND premium_status = 'free'
              AND (winback_30d_sent IS NULL OR winback_30d_sent = FALSE)
              AND (opted_out IS NULL OR opted_out = FALSE)
              AND (stripe_subscription_id IS NULL OR subscription_status != 'active')
        """, (window_start, target_date))

        users = c.fetchall()

        if not users:
            logger.info("No users due for 30-day win-back")
            return {"messages_sent": 0}

        messages_sent = 0

        for user in users:
            phone_number, first_name, trial_end_date = user

            try:
                greeting = f"Hi {first_name}!" if first_name else "Hi there!"

                message = f"""{greeting} It's been a month since your Remyndrs trial ended.

Your reminders, lists & memories are still here waiting for you.

Text UPGRADE to unlock unlimited access — {PREMIUM_MONTHLY_PRICE}/mo or {PREMIUM_ANNUAL_PRICE}/yr ($7.50/mo).

Or just text me anything to keep using the free plan!"""

                send_sms(phone_number, message)
                create_or_update_user(phone_number, winback_30d_sent=True)

                messages_sent += 1
                logger.info(f"Sent 30-day win-back to ...{phone_number[-4:]}")

            except Exception as e:
                logger.error(f"Failed to send 30-day win-back to ...{phone_number[-4:]}: {e}")
                continue

        logger.info(f"30-day win-back complete: {messages_sent} sent")
        return {"messages_sent": messages_sent}

    except Exception as exc:
        logger.exception("Error in send_30d_winback")
        raise self.retry(exc=exc)

    finally:
        if conn:
            return_db_connection(conn)
