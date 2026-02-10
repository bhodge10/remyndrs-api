"""
Pending State Handlers
Handles user pending states for confirmations, deletions, and clarifications

These functions process user responses when they're in various pending states
(e.g., confirming a deletion, selecting from multiple options, clarifying time).

Each handler returns a tuple: (handled: bool, response: str or None)
- If handled=True, the response should be sent and processing should stop
- If handled=False, normal message processing should continue
"""

import json
import re
from datetime import timedelta
from typing import Any, Optional, Tuple

import pytz

from config import logger, MAX_ITEMS_PER_LIST
from database import log_interaction
from models.user import get_user, create_or_update_user, get_user_timezone
from models.reminder import (
    save_reminder, delete_reminder, delete_recurring_reminder,
    save_reminder_with_local_time
)
from models.list_model import (
    get_list_by_name, get_lists, get_list_items,
    add_list_item, delete_list_item, get_item_count, rename_list,
    get_next_available_list_name, create_list
)
from models.memory import delete_memory
from utils.timezone import get_user_current_time
from utils.formatting import format_reminder_confirmation
from services.ai_service import parse_list_items
from services.first_action_service import (
    should_prompt_daily_summary, mark_daily_summary_prompted,
    get_daily_summary_prompt_message
)


def handle_pending_delete(
    phone_number: str,
    incoming_msg: str,
    pending_delete_data: str
) -> Tuple[bool, Optional[str]]:
    """
    Handle pending delete state for reminders and list items.

    Args:
        phone_number: User's phone number
        incoming_msg: The user's message
        pending_delete_data: JSON string with delete options/confirmation data

    Returns:
        Tuple of (handled, response_message)
    """
    # Handle CANCEL
    if incoming_msg.strip().upper() in ["CANCEL", "NO"]:
        create_or_update_user(phone_number, pending_reminder_delete=None)
        return (True, "Cancelled.")

    try:
        delete_data = json.loads(pending_delete_data)
    except json.JSONDecodeError:
        return (False, None)

    # Handle YES confirmation for single-item delete
    if isinstance(delete_data, dict) and delete_data.get('awaiting_confirmation'):
        if incoming_msg.strip().upper() == "YES":
            delete_type = delete_data.get('type', 'reminder')
            reply_msg = None

            if delete_type == 'reminder':
                if delete_reminder(phone_number, delete_data['id']):
                    reply_msg = f"Deleted reminder: {delete_data['text']}"
                    recurring_id = delete_data.get('recurring_id')
                    if recurring_id:
                        if delete_recurring_reminder(recurring_id, phone_number):
                            reply_msg += " (and its recurring schedule)"
                else:
                    reply_msg = "Couldn't delete that reminder."

            elif delete_type == 'recurring':
                if delete_recurring_reminder(delete_data['id'], phone_number):
                    reply_msg = f"Deleted recurring reminder: {delete_data['text']}"
                else:
                    reply_msg = "Couldn't delete that recurring reminder."

            elif delete_type == 'list_item':
                if delete_list_item(phone_number, delete_data['list_name'], delete_data['text']):
                    reply_msg = f"Removed '{delete_data['text']}' from {delete_data['list_name']}"
                else:
                    reply_msg = "Couldn't delete that item."

            create_or_update_user(phone_number, pending_reminder_delete=None)
            log_interaction(phone_number, incoming_msg, reply_msg, f"delete_{delete_type}_confirmed", True)
            return (True, reply_msg)
        else:
            # Not YES or CANCEL - clear pending state and let message be processed normally
            create_or_update_user(phone_number, pending_reminder_delete=None)
            return (False, None)

    # Handle number selection from list
    if incoming_msg.strip().isdigit():
        try:
            delete_options = json.loads(pending_delete_data)
            if not isinstance(delete_options, list):
                return (False, None)

            selection = int(incoming_msg.strip())
            if 1 <= selection <= len(delete_options):
                selected = delete_options[selection - 1]
                delete_type = selected.get('type', 'reminder')
                reply_msg = None

                if delete_type == 'reminder':
                    if delete_reminder(phone_number, selected['id']):
                        reply_msg = f"Deleted reminder: {selected['text']}"
                        recurring_id = selected.get('recurring_id')
                        if recurring_id:
                            if delete_recurring_reminder(recurring_id, phone_number):
                                reply_msg += " (and its recurring schedule)"
                    else:
                        reply_msg = "Couldn't delete that reminder."

                elif delete_type == 'list_item':
                    if delete_list_item(phone_number, selected['list_name'], selected['text']):
                        reply_msg = f"Removed '{selected['text']}' from {selected['list_name']}"
                    else:
                        reply_msg = "Couldn't delete that list item."

                elif delete_type == 'memory':
                    if delete_memory(phone_number, selected['id']):
                        reply_msg = f"Deleted memory: {selected['text']}"
                    else:
                        reply_msg = "Couldn't delete that memory."

                create_or_update_user(phone_number, pending_reminder_delete=None)
                log_interaction(phone_number, incoming_msg, reply_msg, f"delete_{delete_type}", True)
                return (True, reply_msg)
            else:
                return (True, f"Please reply with a number between 1 and {len(delete_options)}, or CANCEL")

        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Error parsing pending delete data: {e}")
            create_or_update_user(phone_number, pending_reminder_delete=None)

    return (False, None)


def handle_pending_memory_delete(
    phone_number: str,
    incoming_msg: str,
    pending_memory_data: str
) -> Tuple[bool, Optional[str]]:
    """
    Handle pending memory deletion state.

    Args:
        phone_number: User's phone number
        incoming_msg: The user's message
        pending_memory_data: JSON string with memory delete data

    Returns:
        Tuple of (handled, response_message)
    """
    try:
        memory_data = json.loads(pending_memory_data)

        # Handle YES/NO confirmation for single memory
        if memory_data.get('awaiting_confirmation'):
            if incoming_msg.upper() == "YES":
                memory_id = memory_data['id']
                memory_text = memory_data['text']
                if delete_memory(phone_number, memory_id):
                    reply_msg = f"Deleted memory: {memory_text[:100]}{'...' if len(memory_text) > 100 else ''}"
                else:
                    reply_msg = "Couldn't delete that memory."
                create_or_update_user(phone_number, pending_memory_delete=None)
                log_interaction(phone_number, incoming_msg, reply_msg, "delete_memory_confirmed", True)
                return (True, reply_msg)

            elif incoming_msg.upper() in ["NO", "CANCEL"]:
                create_or_update_user(phone_number, pending_memory_delete=None)
                log_interaction(phone_number, incoming_msg, "Delete cancelled", "delete_memory_cancelled", True)
                return (True, "Cancelled. Your memory is safe!")

        # Handle number selection from multiple memories
        elif memory_data.get('options') and incoming_msg.strip().isdigit():
            memory_options = memory_data['options']
            selection = int(incoming_msg.strip())
            if 1 <= selection <= len(memory_options):
                selected_memory = memory_options[selection - 1]
                memory_id = selected_memory['id']
                memory_text = selected_memory['text']

                # Ask for confirmation
                confirm_data = json.dumps({
                    'awaiting_confirmation': True,
                    'id': memory_id,
                    'text': memory_text
                })
                create_or_update_user(phone_number, pending_memory_delete=confirm_data)

                display_text = memory_text[:100] + ('...' if len(memory_text) > 100 else '')
                reply_msg = f"Delete memory: '{display_text}'?\n\nReply YES to confirm or NO to cancel."

                log_interaction(phone_number, incoming_msg, reply_msg, "delete_memory_confirm_request", True)
                return (True, reply_msg)
            else:
                return (True, f"Please reply with a number between 1 and {len(memory_options)}")

    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Error parsing pending memory delete data: {e}")
        create_or_update_user(phone_number, pending_memory_delete=None)

    return (False, None)


def handle_pending_list_create(
    phone_number: str,
    incoming_msg: str,
    pending_list_name: str
) -> Tuple[bool, Optional[str]]:
    """
    Handle pending list creation (duplicate list handling).

    Args:
        phone_number: User's phone number
        incoming_msg: The user's message
        pending_list_name: Name of the list being created

    Returns:
        Tuple of (handled, response_message)
    """
    msg_lower = incoming_msg.strip().lower()

    # Check for "add" responses
    add_keywords = ['add', 'existing', 'use', 'yes', 'that one', 'add to it', 'add items']
    wants_add = any(kw in msg_lower for kw in add_keywords)

    # Check for "new" responses
    new_keywords = ['new', 'create', 'another', 'different', 'new one', 'create new']
    wants_new = any(kw in msg_lower for kw in new_keywords)

    if wants_add and not wants_new:
        existing_list = get_list_by_name(phone_number, pending_list_name)
        if existing_list:
            list_id, actual_name = existing_list
            create_or_update_user(phone_number, pending_list_create=None, last_active_list=actual_name)
            reply_msg = f"Great! Your {actual_name} is ready. What would you like to add?"
        else:
            reply_msg = "That list no longer exists. Would you like to create it?"
            create_or_update_user(phone_number, pending_list_create=None)

        log_interaction(phone_number, incoming_msg, reply_msg, "list_duplicate_add_existing", True)
        return (True, reply_msg)

    elif wants_new:
        # Rename original to #1 if needed
        original_list = get_list_by_name(phone_number, pending_list_name)
        if original_list and not re.search(r'#\s*\d+$', pending_list_name):
            new_original_name = f"{pending_list_name} #1"
            rename_list(phone_number, pending_list_name, new_original_name)
            logger.info(f"Renamed original list '{pending_list_name}' to '{new_original_name}'")

        # Create new list with incremented name
        new_list_name = get_next_available_list_name(phone_number, pending_list_name)
        create_list(phone_number, new_list_name)
        create_or_update_user(phone_number, pending_list_create=None, last_active_list=new_list_name)
        reply_msg = f"Created your {new_list_name}!"

        log_interaction(phone_number, incoming_msg, reply_msg, "list_duplicate_create_new", True)
        return (True, reply_msg)

    # If unclear, clear pending state
    create_or_update_user(phone_number, pending_list_create=None)
    return (False, None)


def handle_pending_list_item(
    phone_number: str,
    incoming_msg: str,
    pending_item: str
) -> Tuple[bool, Optional[str]]:
    """
    Handle pending list item selection (when user needs to select which list to add to).

    Args:
        phone_number: User's phone number
        incoming_msg: The user's message
        pending_item: The item(s) to add

    Returns:
        Tuple of (handled, response_message)
    """
    from services.tier_service import (
        format_list_item_limit_message,
        add_list_item_counter_to_message,
        get_tier_limits,
        get_user_tier
    )

    if not incoming_msg.strip().isdigit():
        return (False, None)

    list_num = int(incoming_msg.strip())
    lists = get_lists(phone_number)

    if 1 <= list_num <= len(lists):
        selected_list = lists[list_num - 1]
        list_id = selected_list[0]
        list_name = selected_list[1]

        # Parse multiple items
        items_to_add = parse_list_items(pending_item, phone_number)

        # Check item limit using tier-aware limits
        tier_limits = get_tier_limits(get_user_tier(phone_number))
        max_items = tier_limits['max_items_per_list']
        item_count = get_item_count(list_id)
        available_slots = max_items - item_count

        if available_slots <= 0:
            create_or_update_user(phone_number, pending_list_item=None)
            # Use Level 4 formatter for clear WHY-WHAT-HOW message
            reply_msg = format_list_item_limit_message(
                phone_number, list_name, items_to_add, 0
            )
            return (True, reply_msg)

        # Add items
        added_items = []
        for item in items_to_add:
            if len(added_items) < available_slots:
                add_list_item(list_id, phone_number, item)
                added_items.append(item)

        create_or_update_user(phone_number, pending_list_item=None, last_active_list=list_name)

        # Handle partial or full adds with progressive education
        if len(added_items) < len(items_to_add):
            # Some items skipped - use Level 4 formatter
            reply_msg = format_list_item_limit_message(
                phone_number, list_name, items_to_add, len(added_items)
            )
        else:
            # All items added successfully
            if len(added_items) == 1:
                base_reply = f"Added {added_items[0]} to your {list_name}"
            else:
                base_reply = f"Added {len(added_items)} items to your {list_name}: {', '.join(added_items)}"

            # Add progressive counter
            reply_msg = add_list_item_counter_to_message(phone_number, list_id, base_reply)

        log_interaction(phone_number, incoming_msg, f"Added {len(added_items)} items to {list_name}", "add_to_list", True)
        return (True, reply_msg)
    else:
        return (True, f"Please reply with a number between 1 and {len(lists)}")


def handle_pending_reminder_confirmation(
    phone_number: str,
    incoming_msg: str,
    pending_data: dict[str, Any]
) -> Tuple[bool, Optional[str]]:
    """
    Handle pending reminder confirmation (low-confidence confirmations).

    Args:
        phone_number: User's phone number
        incoming_msg: The user's message
        pending_data: Dictionary with reminder details awaiting confirmation

    Returns:
        Tuple of (handled, response_message)
    """
    msg_upper = incoming_msg.strip().upper()

    if msg_upper == "YES":
        # User confirmed - save the reminder
        reminder_text = pending_data.get('reminder_text')
        reminder_datetime_utc = pending_data.get('reminder_datetime_utc')
        local_time = pending_data.get('local_time')
        user_tz = pending_data.get('timezone')

        save_reminder_with_local_time(
            phone_number,
            reminder_text,
            reminder_datetime_utc,
            local_time,
            user_tz
        )

        # Format confirmation
        from datetime import datetime
        if isinstance(reminder_datetime_utc, str):
            utc_dt = datetime.strptime(reminder_datetime_utc, '%Y-%m-%d %H:%M:%S')
        else:
            utc_dt = reminder_datetime_utc

        tz = pytz.timezone(user_tz)
        if utc_dt.tzinfo is None:
            utc_dt = pytz.UTC.localize(utc_dt)
        local_dt = utc_dt.astimezone(tz)

        readable_date = local_dt.strftime('%A, %B %d at %I:%M %p')
        reply_text = f"I'll remind you on {readable_date} {format_reminder_confirmation(reminder_text)}."

        # Clear pending confirmation
        create_or_update_user(phone_number, pending_reminder_confirmation=None)

        # Check for daily summary prompt
        if should_prompt_daily_summary(phone_number):
            reply_text = get_daily_summary_prompt_message(reply_text)
            mark_daily_summary_prompted(phone_number)

        log_interaction(phone_number, incoming_msg, reply_text, "reminder_confirmed_yes", True)
        return (True, reply_text)

    elif msg_upper in ["NO", "CANCEL"]:
        create_or_update_user(phone_number, pending_reminder_confirmation=None)
        log_interaction(phone_number, incoming_msg, "Cancelled", "reminder_confirmation_cancelled", True)
        return (True, "Got it, cancelled.")

    # Not YES/NO - clear pending state and let message be processed normally
    create_or_update_user(phone_number, pending_reminder_confirmation=None)
    return (False, None)


def handle_pending_reminder_date(
    phone_number: str,
    incoming_msg: str,
    pending_date_data: dict[str, Any]
) -> Tuple[bool, Optional[str]]:
    """
    Handle pending reminder date clarification (when user provides date without time).

    Args:
        phone_number: User's phone number
        incoming_msg: The user's message
        pending_date_data: Dictionary with date/reminder info

    Returns:
        Tuple of (handled, response_message)
    """
    # This handler primarily checks if the user's response contains a time
    # The actual time parsing is done in the main handler
    # This is a placeholder for the extracted logic
    return (False, None)


def handle_time_clarification(
    phone_number: str,
    incoming_msg: str,
    pending_text: str,
    pending_time: str
) -> Tuple[bool, Optional[str]]:
    """
    Handle AM/PM clarification for reminders.

    Args:
        phone_number: User's phone number
        incoming_msg: The user's message (should contain AM or PM)
        pending_text: The reminder text
        pending_time: The pending time (needs AM/PM clarification)

    Returns:
        Tuple of (handled, response_message)
    """
    # Detect AM vs PM
    am_match = re.search(r'(^|[\d\s])(am|a\.m\.?)(\b|$)', incoming_msg, re.IGNORECASE)
    am_pm = "AM" if am_match else "PM"

    try:
        user_time = get_user_current_time(phone_number)
        user_tz = get_user_timezone(phone_number)

        # Clean up the pending_time
        clean_time = pending_time.upper().replace("AM", "").replace("PM", "").replace("A.M.", "").replace("P.M.", "").strip()
        clean_time = re.sub(r'[AP]$', '', clean_time).strip()

        # Parse the time
        time_parts = clean_time.split(":")
        hour = int(time_parts[0])
        minute = int(time_parts[1]) if len(time_parts) > 1 else 0

        # Convert to 24-hour format
        if am_pm == "PM" and hour != 12:
            hour += 12
        elif am_pm == "AM" and hour == 12:
            hour = 0

        # Create reminder datetime
        reminder_datetime = user_time.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # If time has passed today, schedule for tomorrow
        if reminder_datetime <= user_time:
            reminder_datetime = reminder_datetime + timedelta(days=1)

        # Convert to UTC
        reminder_datetime_utc = reminder_datetime.astimezone(pytz.UTC)
        reminder_date_str = reminder_datetime_utc.strftime('%Y-%m-%d %H:%M:%S')

        # Save the reminder
        save_reminder(phone_number, pending_text, reminder_date_str)

        # Format confirmation
        readable_date = reminder_datetime.strftime('%A, %B %d at %I:%M %p')
        reply_text = f"I'll remind you on {readable_date} {format_reminder_confirmation(pending_text)}."

        # Clear pending reminder
        create_or_update_user(phone_number, pending_reminder_text=None, pending_reminder_time=None)

        # Check for daily summary prompt
        if should_prompt_daily_summary(phone_number):
            reply_text = get_daily_summary_prompt_message(reply_text)
            mark_daily_summary_prompted(phone_number)

        log_interaction(phone_number, incoming_msg, reply_text, "reminder_confirmed", True)
        return (True, reply_text)

    except Exception as e:
        logger.error(f"Error processing time: {e}")
        create_or_update_user(phone_number, pending_reminder_text=None, pending_reminder_time=None, pending_reminder_date=None)
        return (True, "Sorry, I had trouble setting that reminder. Please try again with a clear time like '3pm' or '3:00 PM'.")
