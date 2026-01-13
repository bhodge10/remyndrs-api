"""
First Action Service
Handles daily summary prompt after user's first successful action
"""

import re
import threading
import time as time_module
from config import logger
from models.user import get_user, create_or_update_user
from utils.timezone import get_user_current_time
from services.sms_service import send_sms


def send_delayed_message(phone_number, message, delay_seconds=4):
    """Send a message after a delay (for split messages)"""
    def _send():
        time_module.sleep(delay_seconds)
        try:
            send_sms(phone_number, message)
            logger.debug(f"Sent delayed message to {phone_number[-4:]}")
        except Exception as e:
            logger.error(f"Error sending delayed message: {e}")

    thread = threading.Thread(target=_send)
    thread.daemon = True
    thread.start()


def should_prompt_daily_summary(phone_number):
    """Check if we should prompt user for daily summary after their action"""
    user = get_user(phone_number)
    if not user:
        return False

    # Check if daily_summary_prompted column exists and is set
    # User tuple indices may vary - need to check by field name approach
    # For now, check if daily_summary_enabled is None/False and not yet prompted
    try:
        # Get user's daily_summary_enabled status
        # If they already have it enabled or have been prompted, don't ask again
        daily_summary_enabled = get_daily_summary_status(phone_number)
        daily_summary_prompted = get_daily_summary_prompted(phone_number)

        # Only prompt if:
        # 1. They haven't been prompted yet
        # 2. They don't already have daily summary enabled
        if daily_summary_prompted:
            return False
        if daily_summary_enabled:
            return False

        return True
    except Exception as e:
        logger.error(f"Error checking daily summary prompt status: {e}")
        return False


def get_daily_summary_status(phone_number):
    """Get user's daily summary enabled status"""
    from database import get_db_connection, return_db_connection
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT daily_summary_enabled FROM users WHERE phone_number = %s', (phone_number,))
        result = c.fetchone()
        return result[0] if result else False
    except Exception as e:
        logger.error(f"Error getting daily summary status: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def get_daily_summary_prompted(phone_number):
    """Check if user has been prompted for daily summary"""
    from database import get_db_connection, return_db_connection
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT daily_summary_prompted FROM users WHERE phone_number = %s', (phone_number,))
        result = c.fetchone()
        return result[0] if result else False
    except Exception as e:
        # Column might not exist yet - return False
        logger.debug(f"daily_summary_prompted column may not exist: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def mark_daily_summary_prompted(phone_number):
    """Mark that user has been prompted for daily summary"""
    create_or_update_user(phone_number, daily_summary_prompted=True)


def get_pending_daily_summary_time(phone_number):
    """Get user's pending daily summary time (for evening confirmation flow)"""
    from database import get_db_connection, return_db_connection
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT pending_daily_summary_time FROM users WHERE phone_number = %s', (phone_number,))
        result = c.fetchone()
        return result[0] if result and result[0] else None
    except Exception as e:
        logger.debug(f"Error getting pending daily summary time: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def format_time_for_display(time_24h):
    """Convert 24-hour time string to display format (e.g., '08:00' -> '8:00 AM')"""
    hour, minute = map(int, time_24h.split(':'))
    am_pm = 'AM' if hour < 12 else 'PM'
    display_hour = hour if hour <= 12 else hour - 12
    if display_hour == 0:
        display_hour = 12
    return f"{display_hour}:{minute:02d} {am_pm}"


def validate_daily_summary_time(time_input):
    """
    Validate and parse time input for daily summary.
    Returns: (is_valid, parsed_time_24h, needs_am_pm_clarification, is_evening, clarification_hour, error_message)
    """
    msg = time_input.strip()
    msg_lower = msg.lower()

    # Pattern for time with AM/PM (flexible matching)
    full_time_pattern = r'(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)'
    # Pattern for bare time without AM/PM
    bare_time_pattern = r'^(\d{1,2})(?::(\d{2}))?$'

    # Check for full time (with AM/PM)
    full_match = re.search(full_time_pattern, msg_lower, re.IGNORECASE)
    if full_match:
        hour = int(full_match.group(1))
        minute = int(full_match.group(2)) if full_match.group(2) else 0
        am_pm = full_match.group(3).lower().replace('.', '')

        # Validate hour range (1-12 for 12-hour format)
        if hour < 1 or hour > 12:
            return (False, None, False, False, None, f"'{hour}' isn't a valid hour. Please use 1-12 with AM or PM (like 8AM or 7:30PM).")

        # Validate minute range
        if minute < 0 or minute > 59:
            return (False, None, False, False, None, f"'{minute}' isn't a valid minute. Please use 0-59.")

        # Convert to 24-hour format
        if am_pm in ['pm', 'p'] and hour != 12:
            hour += 12
        elif am_pm in ['am', 'a'] and hour == 12:
            hour = 0

        time_24h = f"{hour:02d}:{minute:02d}"

        # Check if evening time (6PM-11PM = 18:00-23:00)
        is_evening = 18 <= hour <= 23

        return (True, time_24h, False, is_evening, None, None)

    # Check for bare time (without AM/PM)
    bare_match = re.match(bare_time_pattern, msg)
    if bare_match:
        hour = int(bare_match.group(1))
        minute = int(bare_match.group(2)) if bare_match.group(2) else 0

        # Validate hour range - reject 0 or > 12 as clearly invalid
        if hour == 0 or hour > 12:
            return (False, None, False, False, None, f"I need a time like 8AM or 7:30AM. What time would you like your daily summary?")

        # Validate minute
        if minute < 0 or minute > 59:
            return (False, None, False, False, None, f"'{minute}' isn't a valid minute. Please use 0-59.")

        # This is a valid time but needs AM/PM clarification
        return (True, None, True, False, hour, None)

    # Check for common invalid word formats
    invalid_keywords = {
        'morning': "I need a specific time for your daily summary. What time works? (like 8AM or 7:30AM)",
        'afternoon': "I need a specific time. What time works? (like 2PM or 3:30PM)",
        'evening': "I need a specific time. What time works? (like 6PM or 7PM)",
        'night': "Daily summaries work best in the morning to plan your day. How about 8AM?",
        'asap': "Daily summaries are sent at a specific time each day. What time works? (like 8AM)",
        'later': "I need a specific time. What time works? (like 8AM or 9:30AM)",
    }

    for keyword, error_msg in invalid_keywords.items():
        if keyword in msg_lower:
            return (False, None, False, False, None, error_msg)

    # Check for noon/midnight special cases
    if 'noon' in msg_lower:
        return (True, "12:00", False, False, None, None)
    if 'midnight' in msg_lower:
        return (False, None, False, False, None, "Daily summaries work best in the morning to plan your day. How about 8AM instead?")

    # Generic invalid format
    return (False, None, False, False, None, "I didn't understand that time. Please enter something like 8AM, 7:30AM, or 9PM.")


def get_daily_summary_prompt_message(action_confirmation):
    """Generate the daily summary prompt message to append after first action"""
    return f"""{action_confirmation}

You're getting the hang of it!

Quick question: Want a daily text each morning with your reminders for the day?

This helps you plan ahead and never miss anything.

Reply:
- YES (I'll send at 8am)
- A time like 7AM or 9:30AM
- NO (you can enable later with "daily summary on")"""


def send_welcome_with_delay(phone_number, summary_time, timezone, current_time):
    """
    Build and send split welcome messages.
    Returns the first message immediately, schedules second message for delayed delivery.
    """
    message1, message2 = build_welcome_messages(summary_time, timezone, current_time)
    # Schedule the second message to send after a 4-second delay
    send_delayed_message(phone_number, message2, delay_seconds=4)
    return message1


def handle_daily_summary_response(phone_number, message):
    """
    Handle user's response to daily summary prompt.
    Returns (handled, response_text) tuple.
    - handled: True if this was a daily summary response
    - response_text: The response to send (or None if not handled)
    """
    msg_lower = message.lower().strip()

    # Check if user was prompted for daily summary
    if not get_daily_summary_prompted(phone_number):
        return False, None

    # Check if daily summary is already configured
    if get_daily_summary_status(phone_number):
        return False, None

    user = get_user(phone_number)
    if not user:
        return False, None

    first_name = user[1] or "there"
    timezone = user[5] or 'America/New_York'

    # Get current time for display
    try:
        user_time = get_user_current_time(phone_number)
        current_time = user_time.strftime('%I:%M %p')
    except (ValueError, TypeError, AttributeError):
        current_time = ""

    # Check for pending evening confirmation first
    pending_time = get_pending_daily_summary_time(phone_number)
    if pending_time:
        if msg_lower in ['yes', 'y', 'yep', 'yeah', 'sure', 'ok', 'okay']:
            # Confirm the evening time
            create_or_update_user(
                phone_number,
                daily_summary_enabled=True,
                daily_summary_time=pending_time,
                pending_daily_summary_time=None
            )
            display_time = format_time_for_display(pending_time)
            return True, send_welcome_with_delay(phone_number, display_time, timezone, current_time)
        elif msg_lower in ['no', 'n', 'nope', 'nah']:
            # Clear pending, ask for new time
            create_or_update_user(phone_number, pending_daily_summary_time=None)
            return True, "No problem! What time would you like your daily summary instead? (Morning times like 8AM work great)"
        else:
            # Treat as new time input - clear pending and continue processing
            create_or_update_user(phone_number, pending_daily_summary_time=None)
            # Fall through to process as new time

    # Handle YES/NO responses
    if msg_lower in ['yes', 'y', 'sure', 'ok', 'okay', 'yep', 'yeah']:
        create_or_update_user(
            phone_number,
            daily_summary_enabled=True,
            daily_summary_time='08:00'
        )
        return True, send_welcome_with_delay(phone_number, "8:00 AM", timezone, current_time)

    if msg_lower in ['no', 'n', 'nope', 'nah', 'skip']:
        return True, send_welcome_with_delay(phone_number, None, timezone, current_time)

    # Handle AM/PM clarification responses
    if msg_lower in ['am', 'a.m.', 'a.m', 'morning']:
        # Check if we have a pending clarification by looking for recent ambiguous time
        # For simplicity, re-parse their previous message context isn't available
        # So we'll handle this as them wanting a morning default
        create_or_update_user(
            phone_number,
            daily_summary_enabled=True,
            daily_summary_time='08:00'
        )
        return True, send_welcome_with_delay(phone_number, "8:00 AM", timezone, current_time)

    if msg_lower in ['pm', 'p.m.', 'p.m', 'evening', 'night']:
        # They said PM but we don't know the hour - ask for full time
        return True, "Got it, PM! What hour? (like 6PM or 7:30PM)"

    # Validate and parse the time input
    is_valid, parsed_time, needs_clarification, is_evening, clarify_hour, error_msg = validate_daily_summary_time(message)

    if not is_valid:
        if error_msg:
            return True, error_msg
        # If no error message, treat as unrecognized - don't handle
        return False, None

    if needs_clarification:
        # Time without AM/PM - ask for clarification
        return True, f"Got it! Did you mean {clarify_hour} AM or {clarify_hour} PM?\n\n(Most people prefer morning summaries around 7-9am to plan their day)"

    if is_evening:
        # Evening time (6PM-11PM) - confirm they really want this
        display_time = format_time_for_display(parsed_time)
        create_or_update_user(phone_number, pending_daily_summary_time=parsed_time)
        return True, f"""Just confirming - you want your daily summary at {display_time}?

Most people prefer a morning summary (like 7-9am) to plan their day, but evening works too!

Reply YES for {display_time}, or enter a different time like 8AM."""

    # Valid morning/afternoon time - save it
    create_or_update_user(
        phone_number,
        daily_summary_enabled=True,
        daily_summary_time=parsed_time
    )
    display_time = format_time_for_display(parsed_time)
    return True, send_welcome_with_delay(phone_number, display_time, timezone, current_time)


def build_welcome_messages(summary_time, timezone, current_time):
    """
    Build the welcome messages after daily summary choice.
    Returns tuple (message1, message2) for split delivery.
    """
    if summary_time:
        header = f"Great! You'll get your daily summary at {summary_time} each morning. 📅"
    else:
        header = "No problem! You can always enable it later by texting \"daily summary on\""

    # Message 1: Core features (immediate)
    message1 = f"""{header}

Here's what I can do:

📝 Lists: "Add milk to grocery list"
🧠 Memories: "Remember: garage code is 4582"
⏰ Reminders: "Remind me every Monday at 9am"
🔍 Search: "What did I save about my car?"

Text ? for help or "commands" for the full list."""

    # Message 2: Technical info (sent after delay)
    message2 = f"Your timezone: {timezone}"

    if current_time:
        message2 += f"\nYour local time: {current_time}"

    message2 += """

Beta Note: We're actively improving! Text "support" if you have any issues.

Welcome to Remyndrs! 🎉"""

    return (message1, message2)


def build_welcome_message(summary_time, timezone, current_time):
    """
    Build welcome message - returns first message only for backward compatibility.
    Use build_welcome_messages() for split message delivery.
    """
    message1, _ = build_welcome_messages(summary_time, timezone, current_time)
    return message1
