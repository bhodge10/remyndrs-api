"""
Trial Messaging Service
Handles one-time trial information messaging after user's first real interaction.
"""

import re
from typing import Optional, Tuple
from config import logger, FREE_TRIAL_DAYS, PREMIUM_MONTHLY_PRICE, TIER_LIMITS, TIER_FREE, TIER_PREMIUM
from models.user import create_or_update_user
from database import get_db_connection, return_db_connection


# Actions that trigger trial info message
# These match the actual action types returned by the AI
TRIAL_TRIGGER_ACTIONS = {
    # Save actions
    'store', 'reminder', 'list_add', 'list_create',
    # Retrieval actions
    'retrieve', 'list_reminders', 'list_show', 'show_lists',
}

# Pricing question patterns
PRICING_PATTERNS = [
    r'\b(cost|price|pricing|how much|free|paid|subscription|subscribe)\b',
    r'\b(what\'?s? the (cost|price)|what does it cost)\b',
    r'\b(is (this|it) free)\b',
    r'\b(do i (have to|need to) pay)\b',
]

# Premium comparison patterns
COMPARISON_PATTERNS = [
    r'\b(what\'?s? the difference|free vs premium|premium vs free)\b',
    r'\b(compare|comparison|between free and premium)\b',
    r'\b(what do i get|what\'?s? included)\b',
    r'\b(premium features|free features|what\'?s? premium)\b',
]

# Acknowledgment patterns (for responses to trial message)
ACKNOWLEDGMENT_PATTERNS = [
    r'^(ok|okay|k|thanks|thank you|got it|cool|sounds good|perfect|great|nice|alright|understood)[\.\!\?]?$',
]


def is_pricing_question(message: str) -> bool:
    """Check if message is asking about pricing."""
    message_lower = message.lower().strip()
    for pattern in PRICING_PATTERNS:
        if re.search(pattern, message_lower, re.IGNORECASE):
            return True
    return False


def is_comparison_question(message: str) -> bool:
    """Check if message is asking about free vs premium comparison."""
    message_lower = message.lower().strip()
    for pattern in COMPARISON_PATTERNS:
        if re.search(pattern, message_lower, re.IGNORECASE):
            return True
    return False


def is_acknowledgment(message: str) -> bool:
    """Check if message is a simple acknowledgment."""
    message_lower = message.lower().strip()
    for pattern in ACKNOWLEDGMENT_PATTERNS:
        if re.match(pattern, message_lower, re.IGNORECASE):
            return True
    return False


def get_trial_info_sent(phone_number: str) -> bool:
    """Check if trial info has already been sent to user."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT trial_info_sent FROM users WHERE phone_number = %s', (phone_number,))
        result = c.fetchone()
        return result[0] if result and result[0] else False
    except Exception as e:
        logger.error(f"Error checking trial_info_sent: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def mark_trial_info_sent(phone_number: str) -> None:
    """Mark that trial info has been sent to user."""
    try:
        create_or_update_user(phone_number, trial_info_sent=True)
        logger.info(f"Trial info marked as sent for ...{phone_number[-4:]}")
    except Exception as e:
        logger.error(f"Error marking trial_info_sent: {e}")


def get_trial_info_for_save_action() -> str:
    """Get trial info message for save/reminder actions."""
    free_limits = TIER_LIMITS[TIER_FREE]
    return f"""
---

You're on a FREE {FREE_TRIAL_DAYS}-day Premium trial!

After that, choose:
- Premium: {PREMIUM_MONTHLY_PRICE}/mo (unlimited everything)
- Free: {free_limits['reminders_per_day']} reminders/day (still useful!)

For now, just use me naturally. Text 'help' anytime!"""


def get_trial_info_for_retrieval_action() -> str:
    """Get trial info message for show/retrieval actions."""
    free_limits = TIER_LIMITS[TIER_FREE]
    return f"""
---

You're on a FREE {FREE_TRIAL_DAYS}-day Premium trial!

After that, choose:
- Premium: {PREMIUM_MONTHLY_PRICE}/mo (unlimited everything)
- Free: {free_limits['reminders_per_day']} reminders/day (still useful!)

For now, just explore! Text 'help' anytime."""


def get_pricing_response_during_onboarding(current_step_prompt: str) -> str:
    """Get pricing response for questions during onboarding."""
    return f"""Great question! You get a FREE {FREE_TRIAL_DAYS}-day Premium trial to start. After that, it's {PREMIUM_MONTHLY_PRICE}/mo for Premium or a free tier with 2 reminders/day.

Let's finish setup first - {current_step_prompt}"""


def get_pricing_response() -> str:
    """Get full pricing response for questions after onboarding."""
    free_limits = TIER_LIMITS[TIER_FREE]
    return f"""You're on a FREE {FREE_TRIAL_DAYS}-day Premium trial!

After that, choose:
- Premium: {PREMIUM_MONTHLY_PRICE}/mo (unlimited everything)
- Free: {free_limits['reminders_per_day']} reminders/day (still useful!)

For now, just use me naturally. Text 'help' anytime!"""


def get_pricing_faq_response() -> str:
    """Get shorter pricing FAQ for users who already received trial info."""
    free_limits = TIER_LIMITS[TIER_FREE]
    return f"""You're on a {FREE_TRIAL_DAYS}-day Premium trial (unlimited everything).

After that:
• Premium: {PREMIUM_MONTHLY_PRICE}/mo
• Free: {free_limits['reminders_per_day']} reminders/day

Want details? Text 'what's premium?'"""


def get_comparison_response() -> str:
    """Get detailed free vs premium comparison."""
    free = TIER_LIMITS[TIER_FREE]
    return f"""Here's what you get with each:

FREE TIER:
- {free['reminders_per_day']} reminders per day
- {free['max_lists']} lists ({free['max_items_per_list']} items each)
- {free['max_memories']} memories
- Perfect for light use

PREMIUM ({PREMIUM_MONTHLY_PRICE}/mo):
- Unlimited reminders
- Unlimited lists & items
- Unlimited memories
- Recurring reminders
- Priority support

You're on Premium trial now - try everything! What do you want to save?"""


def get_comparison_faq_response() -> str:
    """Get comparison table for users who already received trial info."""
    free = TIER_LIMITS[TIER_FREE]
    return f"""FREE vs PREMIUM:

FREE: {free['reminders_per_day']} reminders/day, {free['max_lists']} lists, {free['max_memories']} memories
PREMIUM ({PREMIUM_MONTHLY_PRICE}/mo): Unlimited everything + recurring reminders

You're on Premium trial now!"""


def get_acknowledgment_response() -> str:
    """Get response for acknowledgments to trial message."""
    return "Perfect! What can I help you remember today?"


def should_append_trial_info(phone_number: str, action_type: str) -> bool:
    """
    Determine if trial info should be appended to the response.

    Returns True if:
    - User has completed onboarding (implied by reaching this point)
    - trial_info_sent is False
    - action_type is a qualifying trigger action
    """
    if not action_type:
        return False

    # Normalize action type
    action_lower = action_type.lower().replace('-', '_')

    # Check if it's a trigger action
    if action_lower not in TRIAL_TRIGGER_ACTIONS:
        return False

    # Check if trial info already sent
    if get_trial_info_sent(phone_number):
        return False

    return True


def append_trial_info_to_response(response: str, action_type: str, phone_number: str) -> str:
    """
    Append trial info to AI response if conditions are met.

    Args:
        response: The AI-generated response
        action_type: The type of action performed
        phone_number: User's phone number

    Returns:
        Response with trial info appended (or original response if not applicable)
    """
    if not should_append_trial_info(phone_number, action_type):
        return response

    # Determine message type based on action
    action_lower = action_type.lower().replace('-', '_')

    save_actions = {'save_memory', 'save_reminder', 'create_list', 'add_to_list',
                    'list_add', 'list_create', 'reminder_confirmed'}
    retrieval_actions = {'show_memories', 'show_lists', 'show_reminders', 'show_all', 'retrieve'}

    if action_lower in save_actions:
        trial_info = get_trial_info_for_save_action()
    elif action_lower in retrieval_actions:
        trial_info = get_trial_info_for_retrieval_action()
    else:
        # Fallback to save action message
        trial_info = get_trial_info_for_save_action()

    # Mark as sent
    mark_trial_info_sent(phone_number)
    logger.info(f"Trial info sent to ...{phone_number[-4:]} after {action_type}")

    return response + trial_info
