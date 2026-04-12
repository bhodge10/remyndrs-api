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
    rename_list as db_rename_list, find_item_in_any_list,
    share_list, unshare_list, unshare_list_all, get_list_members,
    get_accessible_list_by_name, can_user_access_list,
    get_shared_lists_for_user, get_pending_shares,
    accept_share, decline_share, leave_shared_list,
    add_list_item as db_add_list_item,
    mark_item_complete_by_list_id, mark_item_incomplete_by_list_id,
    delete_list_item_by_list_id
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
    ai_response: dict[str, Any]
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

    log_interaction(phone_number, incoming_msg, reply_text, "add_to_list", True)
    return reply_text


def handle_add_item_ask_list(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
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


# =====================================================
# SHARED LIST HANDLERS
# =====================================================


def handle_share_list(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle share_list action — share a list with another user."""
    from services.tier_service import can_share_list
    from services.sms_service import send_sms
    from models.user import get_user

    list_name = ai_response.get("list_name")
    target_phone = ai_response.get("phone_number")

    if not target_phone:
        reply_text = "Please include the phone number of the person you want to share with."
        log_interaction(phone_number, incoming_msg, reply_text, "share_list", False)
        return reply_text

    # Normalize phone number
    target_phone = _normalize_phone(target_phone)

    # Can't share with yourself
    if target_phone == phone_number:
        reply_text = "You can't share a list with yourself!"
        log_interaction(phone_number, incoming_msg, reply_text, "share_list", False)
        return reply_text

    # Check premium status
    allowed, limit_msg = can_share_list(phone_number)
    if not allowed:
        reply_text = limit_msg
        log_interaction(phone_number, incoming_msg, reply_text, "share_list", False)
        return reply_text

    # Find the list
    list_info = get_list_by_name(phone_number, list_name)
    if not list_info:
        reply_text = f"I couldn't find a list called '{list_name}'."
        log_interaction(phone_number, incoming_msg, reply_text, "share_list", False)
        return reply_text

    list_id, actual_name = list_info

    # Create the share
    success, message = share_list(phone_number, list_id, target_phone)
    if not success:
        reply_text = message
        log_interaction(phone_number, incoming_msg, reply_text, "share_list", False)
        return reply_text

    # Store pending state — ask for the recipient's name before sending invitation
    import json
    pending_data = json.dumps({
        'list_id': list_id,
        'shared_with_phone': target_phone,
        'list_name': actual_name,
    })
    create_or_update_user(phone_number, pending_share_name=pending_data)

    reply_text = f"What's the name of the person at {_format_phone(target_phone)}?"
    log_interaction(phone_number, incoming_msg, reply_text, "share_list_ask_name", True)
    return reply_text


def handle_pending_share_name(phone_number: str, incoming_msg: str) -> str | None:
    """Handle the name response after a share_list action.
    Returns reply text if handled, None if no pending share name."""
    import json
    from services.sms_service import send_sms
    from models.user import get_user
    from models.list_model import set_share_name

    user = get_user(phone_number)
    if not user:
        return None

    # pending_share_name is stored in the user record
    from database import get_db_connection, return_db_connection
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT pending_share_name FROM users WHERE phone_number = %s', (phone_number,))
    row = c.fetchone()
    return_db_connection(conn)

    if not row or not row[0]:
        return None

    try:
        pending = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        create_or_update_user(phone_number, pending_share_name=None)
        return None

    list_id = pending['list_id']
    target_phone = pending['shared_with_phone']
    list_name = pending['list_name']

    # The incoming message is the name
    recipient_name = incoming_msg.strip().title()

    # Save the name on the share
    set_share_name(list_id, target_phone, recipient_name)

    # Clear the pending state
    create_or_update_user(phone_number, pending_share_name=None)

    # Get owner's name for the invitation message
    owner_name = user[1] if user[1] else "Someone"

    # Send the invitation SMS
    recipient = get_user(target_phone)
    if recipient:
        invite_msg = (
            f"[Shared List] Hi {recipient_name}! {owner_name} shared '{list_name}' with you.\n\n"
            f"Reply ACCEPT to join or DECLINE to skip."
        )
    else:
        invite_msg = (
            f"Hi {recipient_name}! {owner_name} shared a list with you on Remyndrs. "
            f"Reply YES to join and see '{list_name}'."
        )
    send_sms(target_phone, invite_msg, message_type="reply")

    reply_text = f"Invitation sent to {recipient_name} ({_format_phone(target_phone)}) for '{list_name}'."
    log_interaction(phone_number, incoming_msg, reply_text, "share_list_named", True)
    return reply_text


def handle_unshare_list(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle unshare_list action — remove sharing from a list."""
    list_name = ai_response.get("list_name")
    target_phone = ai_response.get("phone_number")

    list_info = get_list_by_name(phone_number, list_name)
    if not list_info:
        reply_text = f"I couldn't find a list called '{list_name}'."
        log_interaction(phone_number, incoming_msg, reply_text, "unshare_list", False)
        return reply_text

    list_id, actual_name = list_info

    if target_phone:
        # Remove specific user
        target_phone = _normalize_phone(target_phone)
        success, message = unshare_list(phone_number, list_id, target_phone)
        if success:
            reply_text = f"Removed {_format_phone(target_phone)} from '{actual_name}'."
        else:
            reply_text = message
    else:
        # Remove all sharing
        success, message = unshare_list_all(phone_number, list_id)
        if success:
            reply_text = f"Stopped sharing '{actual_name}'. {message}"
        else:
            reply_text = message

    log_interaction(phone_number, incoming_msg, reply_text, "unshare_list", success)
    return reply_text


def handle_show_list_members(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle show_list_members action — show who has access to a shared list."""
    from models.user import get_user

    list_name = ai_response.get("list_name")
    list_info = get_list_by_name(phone_number, list_name)

    if not list_info:
        reply_text = f"I couldn't find a list called '{list_name}'."
        log_interaction(phone_number, incoming_msg, reply_text, "show_list_members", False)
        return reply_text

    list_id, actual_name = list_info
    members = get_list_members(list_id)

    if not members:
        reply_text = f"'{actual_name}' isn't shared with anyone."
    else:
        lines = [f"'{actual_name}' is shared with:\n"]
        for i, (member_phone, status, permission, name) in enumerate(members, 1):
            display = name if name else _format_phone(member_phone)
            status_label = " (pending)" if status == 'pending' else ""
            lines.append(f"{i}. {display}{status_label}")

        reply_text = "\n".join(lines)

    log_interaction(phone_number, incoming_msg, reply_text, "show_list_members", True)
    return reply_text


def handle_leave_shared_list(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle leave_shared_list action — user leaves a shared list they don't own."""
    from models.list_model import get_accessible_list_by_name

    list_name = ai_response.get("list_name")
    accessible = get_accessible_list_by_name(phone_number, list_name)

    if not accessible:
        reply_text = f"I couldn't find a shared list called '{list_name}'."
        log_interaction(phone_number, incoming_msg, reply_text, "leave_shared_list", False)
        return reply_text

    list_id, actual_name, is_shared, owner_phone = accessible

    if not is_shared:
        reply_text = f"'{actual_name}' is your own list. Use 'delete {actual_name}' to remove it."
        log_interaction(phone_number, incoming_msg, reply_text, "leave_shared_list", False)
        return reply_text

    success, message = leave_shared_list(phone_number, list_id)
    if success:
        reply_text = f"You've left '{actual_name}'."
    else:
        reply_text = message

    log_interaction(phone_number, incoming_msg, reply_text, "leave_shared_list", success)
    return reply_text


def handle_accept_share(phone_number: str, incoming_msg: str) -> str:
    """Handle ACCEPT keyword for pending shared list invitations."""
    from services.sms_service import send_sms
    from models.list_model import get_share_name

    pending = get_pending_shares(phone_number)

    if not pending:
        return None  # No pending shares — let normal processing continue

    if len(pending) == 1:
        share_id, list_id, owner_phone, list_name = pending[0]
        success, message = accept_share(phone_number, list_id)
        if success:
            reply_text = f"You now have access to '{list_name}'! Text 'Show {list_name}' to see it."
            display_name = get_share_name(list_id, phone_number) or _format_phone(phone_number)
            send_sms(owner_phone, f"{display_name} accepted your shared list '{list_name}'.", message_type="reply")
        else:
            reply_text = message
    else:
        # Multiple pending — accept the most recent
        share_id, list_id, owner_phone, list_name = pending[0]
        success, message = accept_share(phone_number, list_id)
        remaining = len(pending) - 1
        if success:
            reply_text = (
                f"You now have access to '{list_name}'!\n\n"
                f"You have {remaining} more pending invitation{'s' if remaining > 1 else ''}. "
                f"Reply ACCEPT again to accept the next one."
            )
            display_name = get_share_name(list_id, phone_number) or _format_phone(phone_number)
            send_sms(owner_phone, f"{display_name} accepted your shared list '{list_name}'.", message_type="reply")
        else:
            reply_text = message

    log_interaction(phone_number, incoming_msg, reply_text, "accept_share", True)
    return reply_text


def handle_decline_share(phone_number: str, incoming_msg: str) -> str:
    """Handle DECLINE keyword for pending shared list invitations."""
    from services.sms_service import send_sms
    from models.list_model import get_share_name

    pending = get_pending_shares(phone_number)

    if not pending:
        return None  # No pending shares — let normal processing continue

    share_id, list_id, owner_phone, list_name = pending[0]
    success, message = decline_share(phone_number, list_id)

    if success:
        reply_text = f"Declined the invitation for '{list_name}'."
        # Notify the owner
        display_name = get_share_name(list_id, phone_number) or _format_phone(phone_number)
        send_sms(owner_phone, f"{display_name} declined your shared list '{list_name}'.", message_type="reply")
    else:
        reply_text = message

    log_interaction(phone_number, incoming_msg, reply_text, "decline_share", True)
    return reply_text


# =====================================================
# SHARED LIST DISPLAY HELPERS
# =====================================================


def get_all_lists_with_shared(phone_number: str) -> list[dict]:
    """Get all lists (owned + shared) for display. Returns list of dicts with list info."""
    own_lists = get_lists(phone_number)
    shared_lists = get_shared_lists_for_user(phone_number)

    result = []
    for list_id, list_name, item_count, completed_count in own_lists:
        result.append({
            'list_id': list_id,
            'list_name': list_name,
            'item_count': item_count or 0,
            'completed_count': completed_count or 0,
            'is_shared': False,
            'owner_phone': None,
        })

    for list_id, list_name, item_count, completed_count, owner_phone in shared_lists:
        result.append({
            'list_id': list_id,
            'list_name': list_name,
            'item_count': item_count or 0,
            'completed_count': completed_count or 0,
            'is_shared': True,
            'owner_phone': owner_phone,
        })

    return result


def format_all_lists_display(phone_number: str) -> str:
    """Format all lists (owned + shared) for SMS display."""
    all_lists = get_all_lists_with_shared(phone_number)

    if not all_lists:
        return "You don't have any lists yet. Try 'Create a grocery list'!"

    lines = []
    for i, lst in enumerate(all_lists, 1):
        prefix = "[Shared] " if lst['is_shared'] else ""
        lines.append(f"{i}. {prefix}{lst['list_name']} ({lst['item_count']} items)")

    # Check for pending invitations
    pending = get_pending_shares(phone_number)
    pending_note = ""
    if pending:
        pending_note = f"\n\nYou have {len(pending)} pending shared list invitation{'s' if len(pending) > 1 else ''}. Reply ACCEPT or DECLINE."

    return "Your lists:\n\n" + "\n".join(lines) + "\n\nReply with a number to see that list." + pending_note


# =====================================================
# PHONE NUMBER HELPERS
# =====================================================


def _normalize_phone(phone: str) -> str:
    """Normalize a phone number to E.164 format."""
    # Strip all non-digit characters
    digits = ''.join(c for c in phone if c.isdigit())

    # Add country code if missing
    if len(digits) == 10:
        digits = '1' + digits
    if not digits.startswith('+'):
        digits = '+' + digits

    return digits


def _format_phone(phone: str) -> str:
    """Format a phone number for display: (555) 123-4567."""
    digits = ''.join(c for c in phone if c.isdigit())
    if len(digits) == 11 and digits[0] == '1':
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return phone
