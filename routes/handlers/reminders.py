"""
Reminder Action Handlers
Handles reminder-related AI actions: create, list, delete, snooze, recurring
"""

import json
import re
import pytz
from datetime import datetime, timedelta
from typing import Tuple, Optional, Any

from config import logger, ENVIRONMENT
from models.user import create_or_update_user, get_user_timezone
from models.reminder import (
    save_reminder, save_reminder_with_local_time, get_user_reminders,
    save_recurring_reminder, delete_reminder as db_delete_reminder,
    search_pending_reminders, update_reminder_time
)
from utils.timezone import get_user_current_time
from utils.validation import detect_sensitive_data, get_sensitive_data_warning
from database import log_interaction


def format_reminder_confirmation(reminder_text: str) -> str:
    """Format reminder text for confirmation message."""
    if reminder_text.lower().startswith("to "):
        return reminder_text
    return f"to {reminder_text}"


def format_reminders_list(reminders: list, user_tz: str) -> str:
    """Format a list of reminders for display."""
    if not reminders:
        return "You don't have any upcoming reminders."

    tz = pytz.timezone(user_tz)
    lines = ["Your upcoming reminders:\n"]

    for i, (reminder_id, text, reminder_date) in enumerate(reminders, 1):
        # Convert UTC to user timezone
        if isinstance(reminder_date, str):
            dt = datetime.strptime(reminder_date, '%Y-%m-%d %H:%M:%S')
        else:
            dt = reminder_date

        if dt.tzinfo is None:
            dt = pytz.UTC.localize(dt)

        local_dt = dt.astimezone(tz)
        time_str = local_dt.strftime('%A, %B %d at %I:%M %p').replace(' 0', ' ')
        lines.append(f"{i}. {text} - {time_str}")

    lines.append("\n(Reply 'DELETE REMINDER [#]' to remove)")
    return "\n".join(lines)


def handle_list_reminders(phone_number: str, incoming_msg: str) -> str:
    """Handle list_reminders action."""
    reminders = get_user_reminders(phone_number)
    user_tz = get_user_timezone(phone_number)
    reply_text = format_reminders_list(reminders, user_tz)
    log_interaction(phone_number, incoming_msg, reply_text, "list_reminders", True)
    return reply_text


def handle_clarify_time(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle clarify_time action - when user provides time without AM/PM."""
    reminder_text = ai_response.get("reminder_text")
    time_mentioned = ai_response.get("time_mentioned")

    # SAFEGUARD: Check if original message already has AM/PM
    original_time_match = re.search(
        r'(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)\b',
        incoming_msg,
        re.IGNORECASE
    )

    if original_time_match:
        # Original message already has AM/PM - create reminder directly
        hour = int(original_time_match.group(1))
        minute = int(original_time_match.group(2)) if original_time_match.group(2) else 0
        am_pm_raw = original_time_match.group(3).lower().replace('.', '')
        am_pm = 'AM' if am_pm_raw in ['am', 'a'] else 'PM'

        # Convert to 24-hour format
        if am_pm == 'PM' and hour != 12:
            hour += 12
        elif am_pm == 'AM' and hour == 12:
            hour = 0

        logger.info(f"Safeguard: Original message had AM/PM, creating reminder directly at {hour}:{minute:02d}")

        user_time = get_user_current_time(phone_number)
        user_tz = get_user_timezone(phone_number)

        # Create reminder datetime in user's timezone
        reminder_datetime = user_time.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # If time has already passed today, schedule for tomorrow
        if reminder_datetime <= user_time:
            reminder_datetime = reminder_datetime + timedelta(days=1)

        # Convert to UTC for storage
        reminder_datetime_utc = reminder_datetime.astimezone(pytz.UTC)
        reminder_date_str = reminder_datetime_utc.strftime('%Y-%m-%d %H:%M:%S')

        # Save the reminder
        save_reminder(phone_number, reminder_text, reminder_date_str)

        # Format confirmation
        readable_date = reminder_datetime.strftime('%A, %B %d at %I:%M %p')
        reply_text = f"I'll remind you on {readable_date} {format_reminder_confirmation(reminder_text)}."
        log_interaction(phone_number, incoming_msg, reply_text, "reminder_safeguard", True)
    else:
        # No AM/PM in original - proceed with clarification
        if not time_mentioned:
            response_text = ai_response.get("response", "")
            time_match = re.search(r'(\d{1,2}(?::\d{2})?)\s*(?:AM|PM)', response_text, re.IGNORECASE)
            if time_match:
                time_mentioned = time_match.group(1)
                logger.info(f"Extracted time '{time_mentioned}' from AI response")

        create_or_update_user(
            phone_number,
            pending_reminder_text=reminder_text,
            pending_reminder_time=time_mentioned
        )

        reply_text = ai_response.get("response", f"Do you mean {time_mentioned} AM or PM?")
        log_interaction(phone_number, incoming_msg, reply_text, "clarify_time", True)

    return reply_text


def handle_clarify_date_time(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle clarify_date_time action - when user gives date but no time."""
    reminder_text = ai_response.get("reminder_text")
    reminder_date = ai_response.get("reminder_date")

    create_or_update_user(
        phone_number,
        pending_reminder_text=reminder_text,
        pending_reminder_date=reminder_date
    )

    reply_text = ai_response.get("response", "What time would you like the reminder?")
    log_interaction(phone_number, incoming_msg, reply_text, "clarify_date_time", True)
    return reply_text


def handle_clarify_specific_time(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle clarify_specific_time action - when user gives vague time like 'in a bit'."""
    reminder_text = ai_response.get("reminder_text")

    create_or_update_user(
        phone_number,
        pending_reminder_text=reminder_text,
        pending_reminder_time="NEEDS_TIME"
    )

    reply_text = ai_response.get("response", "What time would you like the reminder?")
    log_interaction(phone_number, incoming_msg, reply_text, "clarify_specific_time", True)
    return reply_text


def handle_reminder(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any],
    should_prompt_daily_summary: callable,
    get_daily_summary_prompt_message: callable,
    mark_daily_summary_prompted: callable,
    log_confidence: callable,
    get_setting: callable
) -> str:
    """Handle reminder action - create a one-time reminder."""
    from services.tier_service import can_create_reminder

    reminder_date = ai_response.get("reminder_date")
    reminder_text = ai_response.get("reminder_text")
    confidence = ai_response.get("confidence", 100)

    # Check for sensitive data (staging only)
    if ENVIRONMENT == "staging":
        sensitive_check = detect_sensitive_data(reminder_text)
        if sensitive_check['has_sensitive']:
            reply_text = get_sensitive_data_warning()
            log_interaction(phone_number, incoming_msg, reply_text, "reminder_blocked", False)
            return reply_text

    # Check tier limit
    allowed, limit_msg = can_create_reminder(phone_number)
    if not allowed:
        log_interaction(phone_number, incoming_msg, limit_msg, "reminder_limit_reached", False)
        return limit_msg

    # LOW CONFIDENCE: Ask for confirmation
    CONFIDENCE_THRESHOLD = int(get_setting('confidence_threshold', 70))
    if confidence < CONFIDENCE_THRESHOLD:
        log_confidence(phone_number, 'reminder', confidence, CONFIDENCE_THRESHOLD, confirmed=None, user_message=incoming_msg)
        try:
            user_tz_str = get_user_timezone(phone_number)
            tz = pytz.timezone(user_tz_str)
            naive_dt = datetime.strptime(reminder_date, '%Y-%m-%d %H:%M:%S')
            time_str = naive_dt.strftime('%I:%M %p').lstrip('0')
            date_str = naive_dt.strftime('%A, %B %d, %Y')

            pending_data = json.dumps({
                'action': 'reminder',
                'reminder_text': reminder_text,
                'reminder_date': reminder_date,
                'confirmation': ai_response.get('confirmation'),
                'confidence': confidence
            })
            create_or_update_user(phone_number, pending_reminder_confirmation=pending_data)

            reply_text = f"I understood: Reminder on {date_str} at {time_str} {format_reminder_confirmation(reminder_text)}.\n\nIs that right? Reply YES or tell me what to change."
            log_interaction(phone_number, incoming_msg, reply_text, "reminder_confirmation_needed", True)
            return reply_text
        except Exception as e:
            logger.error(f"Error preparing confirmation: {e}")

    # Create the reminder
    try:
        user_tz_str = get_user_timezone(phone_number)
        tz = pytz.timezone(user_tz_str)

        naive_dt = datetime.strptime(reminder_date, '%Y-%m-%d %H:%M:%S')
        aware_dt = tz.localize(naive_dt)
        utc_dt = aware_dt.astimezone(pytz.UTC)
        reminder_date_utc = utc_dt.strftime('%Y-%m-%d %H:%M:%S')

        local_time_str = naive_dt.strftime('%H:%M')

        save_reminder_with_local_time(
            phone_number, reminder_text, reminder_date_utc,
            local_time_str, user_tz_str
        )

        time_str = naive_dt.strftime('%I:%M %p').lstrip('0')
        date_str = naive_dt.strftime('%A, %B %d, %Y')
        reply_text = f"Got it! I'll remind you on {date_str} at {time_str} {format_reminder_confirmation(reminder_text)}."

    except Exception as e:
        logger.error(f"Error converting reminder time to UTC: {e}")
        save_reminder(phone_number, reminder_text, reminder_date)
        reply_text = ai_response.get("confirmation", "Got it! I'll remind you.")

    # Check for daily summary prompt
    if should_prompt_daily_summary(phone_number):
        reply_text = get_daily_summary_prompt_message(reply_text)
        mark_daily_summary_prompted(phone_number)

    log_interaction(phone_number, incoming_msg, reply_text, "reminder", True)
    return reply_text


def handle_reminder_relative(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any],
    should_prompt_daily_summary: callable,
    get_daily_summary_prompt_message: callable,
    mark_daily_summary_prompted: callable,
    log_confidence: callable,
    get_setting: callable
) -> str:
    """Handle reminder_relative action - reminders like 'in 30 minutes'."""
    from dateutil.relativedelta import relativedelta
    from services.tier_service import can_create_reminder

    reminder_text = ai_response.get("reminder_text", "your reminder")
    confidence = ai_response.get("confidence", 100)

    # Check for sensitive data (staging only)
    if ENVIRONMENT == "staging":
        sensitive_check = detect_sensitive_data(reminder_text)
        if sensitive_check['has_sensitive']:
            reply_text = get_sensitive_data_warning()
            log_interaction(phone_number, incoming_msg, reply_text, "reminder_blocked", False)
            return reply_text

    # Check tier limit
    allowed, limit_msg = can_create_reminder(phone_number)
    if not allowed:
        log_interaction(phone_number, incoming_msg, limit_msg, "reminder_limit_reached", False)
        return limit_msg

    try:
        def parse_offset(raw_value, default=0):
            if raw_value is None:
                return default
            if isinstance(raw_value, (int, float)):
                return int(raw_value)
            match = re.search(r'(\d+)', str(raw_value))
            return int(match.group(1)) if match else default

        offset_minutes = parse_offset(ai_response.get("offset_minutes"))
        offset_days = parse_offset(ai_response.get("offset_days"))
        offset_weeks = parse_offset(ai_response.get("offset_weeks"))
        offset_months = parse_offset(ai_response.get("offset_months"))

        logger.info(f"reminder_relative: minutes={offset_minutes}, days={offset_days}, weeks={offset_weeks}, months={offset_months}")

        if offset_minutes == 0 and offset_days == 0 and offset_weeks == 0 and offset_months == 0:
            offset_minutes = 15

        # Max limits (2 years)
        MAX_MONTHS, MAX_WEEKS, MAX_DAYS, MAX_MINUTES = 24, 104, 730, 1051200

        if offset_months > MAX_MONTHS or offset_weeks > MAX_WEEKS or offset_days > MAX_DAYS or offset_minutes > MAX_MINUTES:
            reply_text = "I can only set reminders up to 2 years in advance. Please try a shorter timeframe."
            log_interaction(phone_number, incoming_msg, reply_text, "reminder_exceeded_limit", False)
            return reply_text

        reminder_dt_utc = datetime.utcnow() + relativedelta(
            months=offset_months, weeks=offset_weeks, days=offset_days, minutes=offset_minutes
        )
        reminder_date_utc = reminder_dt_utc.strftime('%Y-%m-%d %H:%M:%S')

        # LOW CONFIDENCE: Ask for confirmation
        CONFIDENCE_THRESHOLD = int(get_setting('confidence_threshold', 70))
        if confidence < CONFIDENCE_THRESHOLD:
            log_confidence(phone_number, 'reminder_relative', confidence, CONFIDENCE_THRESHOLD, confirmed=None, user_message=incoming_msg)

            user_tz_str = get_user_timezone(phone_number)
            tz = pytz.timezone(user_tz_str)
            reminder_dt_local = pytz.UTC.localize(reminder_dt_utc).astimezone(tz)
            time_str = reminder_dt_local.strftime('%I:%M %p').lstrip('0')
            date_str = reminder_dt_local.strftime('%A, %B %d, %Y')

            pending_data = json.dumps({
                'action': 'reminder_relative',
                'reminder_text': reminder_text,
                'reminder_datetime_utc': reminder_date_utc,
                'local_time': reminder_dt_local.strftime('%H:%M'),
                'offset_minutes': offset_minutes,
                'offset_days': offset_days,
                'offset_weeks': offset_weeks,
                'offset_months': offset_months,
                'confidence': confidence
            })
            create_or_update_user(phone_number, pending_reminder_confirmation=pending_data)

            reply_text = f"I understood: Reminder on {date_str} at {time_str} {format_reminder_confirmation(reminder_text)}.\n\nIs that right? Reply YES or tell me what to change."
            log_interaction(phone_number, incoming_msg, reply_text, "reminder_confirmation_needed", True)
            return reply_text

        save_reminder(phone_number, reminder_text, reminder_date_utc)

        user_tz_str = get_user_timezone(phone_number)
        tz = pytz.timezone(user_tz_str)
        reminder_dt_local = pytz.UTC.localize(reminder_dt_utc).astimezone(tz)

        time_str = reminder_dt_local.strftime('%I:%M %p').lstrip('0')
        date_str = reminder_dt_local.strftime('%A, %B %d, %Y')

        reply_text = f"Got it! I'll remind you on {date_str} at {time_str} {format_reminder_confirmation(reminder_text)}."

        if should_prompt_daily_summary(phone_number):
            reply_text = get_daily_summary_prompt_message(reply_text)
            mark_daily_summary_prompted(phone_number)

        log_interaction(phone_number, incoming_msg, reply_text, "reminder_relative", True)

    except Exception as e:
        logger.error(f"Error setting relative reminder: {e}, ai_response={ai_response}")
        reply_text = "Sorry, I couldn't set that reminder. Please try again."
        log_interaction(phone_number, incoming_msg, reply_text, "reminder_relative", False)

    return reply_text


def handle_delete_reminder(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle delete_reminder action."""
    reminder_id = ai_response.get("reminder_id")
    reminder_number = ai_response.get("reminder_number")
    search_term = ai_response.get("search_term")

    if reminder_id:
        # Direct ID delete - verify ownership via get_user_reminders
        reminders = get_user_reminders(phone_number)
        reminder_match = next((r for r in reminders if r[0] == reminder_id), None)
        if reminder_match:
            db_delete_reminder(phone_number, reminder_id)
            reply_text = f"Deleted reminder: {reminder_match[1]}"
        else:
            reply_text = "I couldn't find that reminder."
    elif reminder_number:
        # Delete by list number
        reminders = get_user_reminders(phone_number)
        if 1 <= reminder_number <= len(reminders):
            reminder_id = reminders[reminder_number - 1][0]
            text = reminders[reminder_number - 1][1]
            db_delete_reminder(phone_number, reminder_id)
            reply_text = f"Deleted reminder: {text}"
        else:
            reply_text = f"Invalid reminder number. You have {len(reminders)} reminders."
    elif search_term:
        # Search and delete
        matches = search_pending_reminders(phone_number, search_term)
        if len(matches) == 1:
            reminder_id, text, _ = matches[0]
            pending_data = json.dumps({'id': reminder_id, 'text': text})
            create_or_update_user(phone_number, pending_delete=pending_data)
            reply_text = f"Delete reminder: '{text}'?\n\nReply YES to confirm."
        elif len(matches) > 1:
            lines = ["Found multiple reminders:"]
            for i, (rid, text, _) in enumerate(matches, 1):
                lines.append(f"{i}. {text}")
            lines.append("\nReply with a number to select:")
            options = [{'id': m[0], 'text': m[1]} for m in matches]
            create_or_update_user(phone_number, pending_delete=json.dumps({'options': options}))
            reply_text = "\n".join(lines)
        else:
            reply_text = f"No reminders found matching '{search_term}'."
    else:
        reply_text = "Please specify which reminder to delete."

    log_interaction(phone_number, incoming_msg, reply_text, "delete_reminder", True)
    return reply_text
