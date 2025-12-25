"""
Input Validation Utilities
Handles input sanitization and validation for security
"""

import re
import html
from datetime import datetime
from config import MAX_LIST_NAME_LENGTH, MAX_ITEM_TEXT_LENGTH, MAX_MESSAGE_LENGTH, logger


def sanitize_text(text: str) -> str:
    """Sanitize text input - escape HTML and remove control characters"""
    if not text:
        return ""
    # Remove control characters except newlines and tabs
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    # Escape HTML entities to prevent XSS
    text = html.escape(text)
    return text.strip()


def validate_list_name(name: str) -> tuple[bool, str]:
    """Validate list name. Returns (is_valid, error_message or sanitized_name)"""
    if not name:
        return False, "List name cannot be empty"

    name = name.strip()

    if len(name) > MAX_LIST_NAME_LENGTH:
        return False, f"List name too long (max {MAX_LIST_NAME_LENGTH} characters)"

    if len(name) < 1:
        return False, "List name cannot be empty"

    # Sanitize and return
    return True, sanitize_text(name)


def validate_item_text(text: str) -> tuple[bool, str]:
    """Validate list item text. Returns (is_valid, error_message or sanitized_text)"""
    if not text:
        return False, "Item cannot be empty"

    text = text.strip()

    if len(text) > MAX_ITEM_TEXT_LENGTH:
        return False, f"Item too long (max {MAX_ITEM_TEXT_LENGTH} characters)"

    if len(text) < 1:
        return False, "Item cannot be empty"

    return True, sanitize_text(text)


def validate_message(text: str) -> tuple[bool, str]:
    """Validate incoming message. Returns (is_valid, error_message or sanitized_text)"""
    if not text:
        return False, "Message cannot be empty"

    text = text.strip()

    if len(text) > MAX_MESSAGE_LENGTH:
        return False, f"Message too long (max {MAX_MESSAGE_LENGTH} characters)"

    return True, sanitize_text(text)


def mask_phone_number(phone: str) -> str:
    """Mask phone number for logging - show only last 4 digits"""
    if not phone:
        return "unknown"
    # Remove any non-digit characters for processing
    digits = re.sub(r'\D', '', phone)
    if len(digits) <= 4:
        return "***" + digits
    return "***" + digits[-4:]


def log_security_event(event_type: str, details: dict):
    """Log security-related events with consistent format"""
    timestamp = datetime.utcnow().isoformat()

    # Mask any phone numbers in details
    safe_details = {}
    for key, value in details.items():
        if 'phone' in key.lower() and value:
            safe_details[key] = mask_phone_number(str(value))
        else:
            safe_details[key] = value

    log_message = f"[SECURITY] {event_type} | {safe_details}"

    # Use warning level for security events to ensure visibility
    if event_type in ['AUTH_FAILURE', 'RATE_LIMIT', 'INVALID_SIGNATURE', 'VALIDATION_FAILURE']:
        logger.warning(log_message)
    else:
        logger.info(log_message)
