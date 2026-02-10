"""
List Action Handlers
Handles list-related AI actions: create, add items, view, complete, delete
"""

import json
from typing import Any

from config import logger, ENVIRONMENT
from models.user import create_or_update_user, get_last_active_list
from models.list_model import (
    create_list, get_list_by_name, get_lists, get_list_items,
    add_list_item, get_item_count, mark_item_complete, mark_item_incomplete,
    delete_list_item, delete_list as db_delete_list, clear_list as db_clear_list,
    rename_list as db_rename_list, find_item_in_any_list
)
from services.ai_service import parse_list_items
from utils.validation import (
    validate_list_name, validate_item_text,
    detect_sensitive_data, get_sensitive_data_warning
)
from database import log_interaction


def handle_create_list(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle create_list action."""
    from services.tier_service import can_create_list, format_list_limit_message, add_list_counter_to_message

    list_name = ai_response.get("list_name")

    # Validate list name
    is_valid, result = validate_list_name(list_name)
    if not is_valid:
        reply_text = result
    else:
        list_name = result  # Use sanitized name

        # Check tier limit
        allowed, limit_msg = can_create_list(phone_number)
        if not allowed:
            # Use Level 4 formatter for clear WHY-WHAT-HOW message
            reply_text = format_list_limit_message(phone_number)
        else:
            existing_list = get_list_by_name(phone_number, list_name)
            if existing_list:
                list_id, actual_name = existing_list
                items = get_list_items(list_id)
                item_count = len(items)

                create_or_update_user(phone_number, pending_list_create=list_name)

                if item_count == 0:
                    reply_text = f"You already have an empty {actual_name}. Would you like to add items to it, or create a new one?"
                elif item_count == 1:
                    reply_text = f"You already have a {actual_name} with 1 item. Would you like to add items to it, or create a new one?"
                else:
                    reply_text = f"You already have a {actual_name} with {item_count} items. Would you like to add items to it, or create a new one?"
            else:
                create_list(phone_number, list_name)
                base_reply = ai_response.get("confirmation", f"Created your {list_name}!")
                # Add progressive counter for free tier users
                reply_text = add_list_counter_to_message(phone_number, base_reply)

    log_interaction(phone_number, incoming_msg, reply_text, "create_list", True)
    return reply_text


def handle_add_to_list(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any],
    should_prompt_daily_summary: callable,
    get_daily_summary_prompt_message: callable,
    mark_daily_summary_prompted: callable
) -> str:
    """Handle add_to_list action."""
    from services.tier_service import (
        can_create_list, can_add_list_item, get_tier_limits, get_user_tier,
        format_list_limit_message, format_list_item_limit_message,
        add_list_item_counter_to_message, add_list_counter_to_message
    )

    list_name = ai_response.get("list_name")
    item_text = ai_response.get("item_text")

    # Check for sensitive data (staging only)
    if ENVIRONMENT == "staging":
        sensitive_check = detect_sensitive_data(item_text)
        if sensitive_check['has_sensitive']:
            reply_text = get_sensitive_data_warning()
            log_interaction(phone_number, incoming_msg, reply_text, "add_to_list_blocked", False)
            return reply_text

    # Validate inputs
    name_valid, name_result = validate_list_name(list_name)
    item_valid, item_result = validate_item_text(item_text)

    if not name_valid:
        reply_text = name_result
        log_interaction(phone_number, incoming_msg, reply_text, "add_to_list", False)
        return reply_text

    if not item_valid:
        reply_text = item_result
        log_interaction(phone_number, incoming_msg, reply_text, "add_to_list", False)
        return reply_text

    list_name = name_result
    item_text = item_result

    # Parse multiple items
    items_to_add = parse_list_items(item_text, phone_number)
    list_info = get_list_by_name(phone_number, list_name)

    # Auto-create list if it doesn't exist
    if not list_info:
        allowed, limit_msg = can_create_list(phone_number)
        if not allowed:
            # Use Level 4 formatter
            reply_text = format_list_limit_message(phone_number)
        else:
            list_id = create_list(phone_number, list_name)
            tier_limits = get_tier_limits(get_user_tier(phone_number))
            max_items = tier_limits['max_items_per_list']

            added_items = []
            for item in items_to_add:
                if len(added_items) < max_items:
                    add_list_item(list_id, phone_number, item)
                    added_items.append(item)

            create_or_update_user(phone_number, last_active_list=list_name)

            # Handle partial or full adds with progressive education
            if len(added_items) < len(items_to_add):
                # Some items skipped - use Level 4 formatter
                reply_text = format_list_item_limit_message(
                    phone_number, list_name, items_to_add, len(added_items)
                )
            else:
                # All items added successfully
                if len(added_items) == 1:
                    base_reply = f"Created your {list_name} and added {added_items[0]}!"
                else:
                    base_reply = f"Created your {list_name} and added {len(added_items)} items: {', '.join(added_items)}"

                # Add list counter (for list creation) and item counter
                reply_text = add_list_counter_to_message(phone_number, base_reply)
                reply_text = add_list_item_counter_to_message(phone_number, list_id, reply_text)
    else:
        list_id = list_info[0]
        list_name = list_info[1]

        allowed, limit_msg = can_add_list_item(phone_number, list_id)
        if not allowed:
            # List is already full - use Level 4 formatter
            reply_text = format_list_item_limit_message(
                phone_number, list_name, items_to_add, 0
            )
        else:
            tier_limits = get_tier_limits(get_user_tier(phone_number))
            max_items = tier_limits['max_items_per_list']
            item_count = get_item_count(list_id)
            available_slots = max_items - item_count

            added_items = []
            for item in items_to_add:
                if len(added_items) < available_slots:
                    add_list_item(list_id, phone_number, item)
                    added_items.append(item)

            create_or_update_user(phone_number, last_active_list=list_name)

            # Handle partial or full adds with progressive education
            if len(added_items) < len(items_to_add):
                # Some items skipped - use Level 4 formatter
                reply_text = format_list_item_limit_message(
                    phone_number, list_name, items_to_add, len(added_items)
                )
            else:
                # All items added successfully
                if len(added_items) == 1:
                    base_reply = ai_response.get("confirmation", f"Added {added_items[0]} to your {list_name}")
                else:
                    base_reply = f"Added {len(added_items)} items to your {list_name}: {', '.join(added_items)}"

                # Add progressive counter
                reply_text = add_list_item_counter_to_message(phone_number, list_id, base_reply)

            if should_prompt_daily_summary(phone_number):
                reply_text = get_daily_summary_prompt_message(reply_text)
                mark_daily_summary_prompted(phone_number)

    log_interaction(phone_number, incoming_msg, reply_text, "add_to_list", True)
    return reply_text


def handle_add_item_ask_list(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any],
    should_prompt_daily_summary: callable,
    get_daily_summary_prompt_message: callable,
    mark_daily_summary_prompted: callable
) -> str:
    """Handle add_item_ask_list action - when list name is ambiguous."""
    from services.tier_service import (
        can_add_list_item, get_tier_limits, get_user_tier,
        format_list_item_limit_message, add_list_item_counter_to_message
    )

    item_text = ai_response.get("item_text")
    lists = get_lists(phone_number)

    if len(lists) == 1:
        # Only one list, add directly
        list_id = lists[0][0]
        list_name = lists[0][1]

        items_to_add = parse_list_items(item_text, phone_number)

        allowed, limit_msg = can_add_list_item(phone_number, list_id)
        if not allowed:
            # List is full - use Level 4 formatter
            reply_text = format_list_item_limit_message(
                phone_number, list_name, items_to_add, 0
            )
        else:
            tier_limits = get_tier_limits(get_user_tier(phone_number))
            max_items = tier_limits['max_items_per_list']
            item_count = get_item_count(list_id)
            available_slots = max_items - item_count

            added_items = []
            for item in items_to_add:
                if len(added_items) < available_slots:
                    add_list_item(list_id, phone_number, item)
                    added_items.append(item)

            create_or_update_user(phone_number, last_active_list=list_name)

            # Handle partial or full adds with progressive education
            if len(added_items) < len(items_to_add):
                # Some items skipped - use Level 4 formatter
                reply_text = format_list_item_limit_message(
                    phone_number, list_name, items_to_add, len(added_items)
                )
            else:
                # All items added successfully
                if len(added_items) == 1:
                    base_reply = f"Added {added_items[0]} to your {list_name}"
                else:
                    base_reply = f"Added {len(added_items)} items to your {list_name}: {', '.join(added_items)}"

                # Add progressive counter
                reply_text = add_list_item_counter_to_message(phone_number, list_id, base_reply)

            if should_prompt_daily_summary(phone_number):
                reply_text = get_daily_summary_prompt_message(reply_text)
                mark_daily_summary_prompted(phone_number)

    elif len(lists) > 1:
        # Multiple lists, ask which one
        create_or_update_user(phone_number, pending_list_item=item_text)
        list_options = "\n".join([f"{i+1}. {l[1]}" for i, l in enumerate(lists)])
        reply_text = f"Which list would you like to add these to?\n\n{list_options}\n\nReply with a number:"
    else:
        reply_text = "You don't have any lists yet. Try 'Create a grocery list' first!"

    log_interaction(phone_number, incoming_msg, reply_text, "add_item_ask_list", True)
    return reply_text


def handle_show_list(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle show_list action - show a specific list."""
    list_name = ai_response.get("list_name")
    list_info = get_list_by_name(phone_number, list_name)

    if list_info:
        create_or_update_user(phone_number, last_active_list=list_info[1])
        items = get_list_items(list_info[0])

        if items:
            item_lines = []
            for i, (item_id, item_text, completed) in enumerate(items, 1):
                if completed:
                    item_lines.append(f"{i}. [x] {item_text}")
                else:
                    item_lines.append(f"{i}. {item_text}")
            reply_text = f"{list_info[1]}:\n\n" + "\n".join(item_lines)
        else:
            reply_text = f"Your {list_info[1]} is empty."
    else:
        reply_text = f"I couldn't find a list called '{list_name}'."

    log_interaction(phone_number, incoming_msg, reply_text, "show_list", True)
    return reply_text


def handle_show_current_list(phone_number: str, incoming_msg: str) -> str:
    """Handle show_current_list action - show last active or default list."""
    last_active = get_last_active_list(phone_number)
    logger.info(f"show_current_list: last_active={last_active}")

    if last_active:
        list_info = get_list_by_name(phone_number, last_active)
        if list_info:
            items = get_list_items(list_info[0])
            if items:
                item_lines = []
                for i, (item_id, item_text, completed) in enumerate(items, 1):
                    if completed:
                        item_lines.append(f"{i}. [x] {item_text}")
                    else:
                        item_lines.append(f"{i}. {item_text}")
                reply_text = f"{list_info[1]}:\n\n" + "\n".join(item_lines)
            else:
                reply_text = f"Your {list_info[1]} is empty."
        else:
            # Last active list was deleted
            reply_text = _show_all_lists_or_single(phone_number)
    else:
        reply_text = _show_all_lists_or_single(phone_number)

    log_interaction(phone_number, incoming_msg, reply_text, "show_current_list", True)
    return reply_text


def _show_all_lists_or_single(phone_number: str) -> str:
    """Helper to show all lists or single list directly."""
    lists = get_lists(phone_number)

    if len(lists) == 1:
        list_id = lists[0][0]
        list_name = lists[0][1]
        create_or_update_user(phone_number, last_active_list=list_name)
        items = get_list_items(list_id)

        if items:
            item_lines = []
            for i, (item_id, item_text, completed) in enumerate(items, 1):
                if completed:
                    item_lines.append(f"{i}. [x] {item_text}")
                else:
                    item_lines.append(f"{i}. {item_text}")
            return f"{list_name}:\n\n" + "\n".join(item_lines)
        else:
            return f"Your {list_name} is empty."
    elif lists:
        list_lines = [f"{i+1}. {l[1]} ({l[2]} items)" for i, l in enumerate(lists)]
        return "Your lists:\n\n" + "\n".join(list_lines) + "\n\nReply with a number to see that list."
    else:
        return "You don't have any lists yet. Try 'Create a grocery list'!"


def handle_show_all_lists(phone_number: str, incoming_msg: str) -> str:
    """Handle show_all_lists action."""
    lists = get_lists(phone_number)

    if lists:
        list_lines = [f"{i+1}. {l[1]} ({l[2]} items)" for i, l in enumerate(lists)]
        reply_text = "Your lists:\n\n" + "\n".join(list_lines) + "\n\nReply with a number to see that list."
    else:
        reply_text = "You don't have any lists yet. Try 'Create a grocery list'!"

    log_interaction(phone_number, incoming_msg, reply_text, "show_all_lists", True)
    return reply_text


def handle_complete_item(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle complete_item action - mark item as done."""
    list_name = ai_response.get("list_name")
    item_text = ai_response.get("item_text")

    if mark_item_complete(phone_number, list_name, item_text):
        reply_text = ai_response.get("confirmation", f"Checked off {item_text}")
    else:
        # Try to find item in any list
        found = find_item_in_any_list(phone_number, item_text)
        if len(found) == 1:
            list_name = found[0][1]
            if mark_item_complete(phone_number, list_name, item_text):
                reply_text = f"Checked off {item_text} from your {list_name}"
            else:
                reply_text = f"Couldn't find '{item_text}' in your lists."
        elif len(found) > 1:
            reply_text = f"'{item_text}' is in multiple lists. Please specify which list."
        else:
            reply_text = f"Couldn't find '{item_text}' in your lists."

    log_interaction(phone_number, incoming_msg, reply_text, "complete_item", True)
    return reply_text


def handle_uncomplete_item(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle uncomplete_item action - unmark item as done."""
    list_name = ai_response.get("list_name")
    item_text = ai_response.get("item_text")

    if mark_item_incomplete(phone_number, list_name, item_text):
        reply_text = ai_response.get("confirmation", f"Unmarked {item_text}")
    else:
        reply_text = f"Couldn't find '{item_text}' to unmark."

    log_interaction(phone_number, incoming_msg, reply_text, "uncomplete_item", True)
    return reply_text


def handle_delete_item(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle delete_item action - remove item from list (asks for confirmation)."""
    list_name = ai_response.get("list_name")
    item_text = ai_response.get("item_text")

    # Store pending delete for confirmation
    confirm_data = json.dumps({
        'awaiting_confirmation': True,
        'type': 'list_item',
        'list_name': list_name,
        'text': item_text
    })
    create_or_update_user(phone_number, pending_reminder_delete=confirm_data)

    reply_text = f"Remove '{item_text}' from {list_name}?\n\nReply YES to confirm or CANCEL to keep it."
    log_interaction(phone_number, incoming_msg, "Asking delete_item confirmation", "delete_item_confirm", True)
    return reply_text


def handle_delete_list(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle delete_list action - delete entire list."""
    list_name = ai_response.get("list_name")
    list_info = get_list_by_name(phone_number, list_name)

    if list_info:
        list_id, actual_name = list_info
        items = get_list_items(list_id)

        if items:
            # Has items - ask for confirmation
            create_or_update_user(phone_number, pending_delete=True, pending_list_item=actual_name)
            reply_text = f"Are you sure you want to delete your {actual_name} and all its items?\n\nReply YES to confirm."
        else:
            # Empty list - delete immediately
            db_delete_list(phone_number, actual_name)
            reply_text = f"Deleted your {actual_name}."
    else:
        reply_text = f"I couldn't find a list called '{list_name}'."

    log_interaction(phone_number, incoming_msg, reply_text, "delete_list", True)
    return reply_text


def handle_clear_list(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle clear_list action - remove all items from list."""
    list_name = ai_response.get("list_name")
    list_info = get_list_by_name(phone_number, list_name)

    if list_info:
        db_clear_list(phone_number, list_info[1])
        reply_text = f"Cleared all items from your {list_info[1]}."
    else:
        reply_text = f"I couldn't find a list called '{list_name}'."

    log_interaction(phone_number, incoming_msg, reply_text, "clear_list", True)
    return reply_text


def handle_rename_list(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle rename_list action."""
    old_name = ai_response.get("old_name")
    new_name = ai_response.get("new_name")

    list_info = get_list_by_name(phone_number, old_name)

    if list_info:
        is_valid, result = validate_list_name(new_name)
        if is_valid:
            db_rename_list(phone_number, list_info[1], result)
            reply_text = f"Renamed '{list_info[1]}' to '{result}'."
        else:
            reply_text = result
    else:
        reply_text = f"I couldn't find a list called '{old_name}'."

    log_interaction(phone_number, incoming_msg, reply_text, "rename_list", True)
    return reply_text
