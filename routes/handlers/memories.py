"""
Memory Action Handlers
Handles memory-related AI actions: store, retrieve, delete
"""

import json
from typing import Any

from config import logger, ENVIRONMENT
from models.user import create_or_update_user
from models.memory import save_memory, search_memories, delete_memory as db_delete_memory
from utils.validation import detect_sensitive_data, get_sensitive_data_warning
from database import log_interaction


def handle_store_memory(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle store action - save a memory."""
    from services.tier_service import can_save_memory

    memory_text = ai_response.get("memory_text", incoming_msg)

    # Check for sensitive data (staging only)
    if ENVIRONMENT == "staging":
        sensitive_check = detect_sensitive_data(memory_text)
        if sensitive_check['has_sensitive']:
            reply_text = get_sensitive_data_warning()
            log_interaction(phone_number, incoming_msg, reply_text, "store_blocked", False)
            return reply_text

    # Check tier limit
    allowed, limit_msg = can_save_memory(phone_number)
    if not allowed:
        log_interaction(phone_number, incoming_msg, limit_msg, "memory_limit_reached", False)
        return limit_msg

    was_update = save_memory(phone_number, memory_text, ai_response)

    # Echo back exactly what was saved
    saved_text = ai_response.get("memory_text", "")
    if saved_text:
        if was_update:
            reply_text = f'Updated: "{saved_text}"'
        else:
            reply_text = f'Got it! Saved: "{saved_text}"'
    else:
        reply_text = ai_response.get("confirmation", "Got it! I'll remember that.")

    log_interaction(phone_number, incoming_msg, reply_text, "store", True)
    return reply_text


def handle_retrieve_memory(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle retrieve action - recall memories."""
    reply_text = ai_response.get("response", "I don't have that information stored yet.")
    log_interaction(phone_number, incoming_msg, reply_text, "retrieve", True)
    return reply_text


def handle_delete_memory(
    phone_number: str,
    incoming_msg: str,
    ai_response: dict[str, Any]
) -> str:
    """Handle delete_memory action."""
    search_term = (ai_response.get("search_term") or ai_response.get("query") or "").strip()

    if not search_term:
        reply_text = "Which memory would you like to delete? Text SHOW MEMORIES to see them, then 'Delete 1'."
        log_interaction(phone_number, incoming_msg, reply_text, "delete_memory", True)
        return reply_text

    matching_memories = search_memories(phone_number, search_term)

    if len(matching_memories) == 0:
        reply_text = f"No memories found matching '{search_term}'."
    elif len(matching_memories) == 1:
        # Single match - ask for confirmation
        memory_id, memory_text, created_at = matching_memories[0]

        confirm_data = json.dumps({
            'awaiting_confirmation': True,
            'id': memory_id,
            'text': memory_text
        })
        create_or_update_user(phone_number, pending_memory_delete=confirm_data)

        display_text = memory_text[:100] + ('...' if len(memory_text) > 100 else '')
        reply_text = f"Delete memory: '{display_text}'?\n\nReply YES to confirm or NO to cancel."
    else:
        # Multiple matches - ask user to choose
        lines = ["Found multiple memories:"]
        memory_options = []

        for i, (memory_id, memory_text, created_at) in enumerate(matching_memories, 1):
            display_text = memory_text[:80] + ('...' if len(memory_text) > 80 else '')
            lines.append(f"{i}. {display_text}")
            memory_options.append({'id': memory_id, 'text': memory_text})

        lines.append("\nReply with a number to select:")
        reply_text = "\n".join(lines)

        create_or_update_user(phone_number, pending_memory_delete=json.dumps({'options': memory_options}))

    log_interaction(phone_number, incoming_msg, reply_text, "delete_memory", True)
    return reply_text
