"""
Input Validation Utilities
Handles input sanitization and validation for security
"""

import re
import html
from datetime import datetime
from config import MAX_LIST_NAME_LENGTH, MAX_ITEM_TEXT_LENGTH, MAX_MESSAGE_LENGTH, logger


def sanitize_text(text: str) -> str:
    """Sanitize text input - remove control characters.

    Note: We don't HTML-escape here because:
    1. Output goes to SMS (plain text, not HTML)
    2. SQL injection is prevented by parameterized queries
    3. HTML escaping should happen at display time in web contexts, not at storage time
    """
    if not text:
        return ""
    # Remove control characters except newlines and tabs
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
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


def detect_sensitive_data(text: str) -> dict:
    """
    Detect potentially sensitive data patterns in text.
    Returns dict with 'has_sensitive': bool and 'types': list of detected types.

    Detects:
    - Credit card numbers (16 digits, with or without separators)
    - Social Security Numbers (9 digits, with or without dashes)
    """
    if not text:
        return {'has_sensitive': False, 'types': []}

    detected = []

    # Remove common separators for pattern matching
    normalized = re.sub(r'[\s\-\.]', '', text)

    # Credit Card Pattern: 13-19 consecutive digits (covers all card types)
    # Amex: 15, Visa/MC/Discover: 16, some cards up to 19
    cc_pattern_formatted = r'\b\d{4}[\s\-\.]\d{4}[\s\-\.]\d{4}[\s\-\.]\d{4}\b'
    # For normalized text, find any 13-19 digit sequence
    cc_matches = re.findall(r'\d{13,}', normalized)

    if re.search(cc_pattern_formatted, text) or any(13 <= len(m) <= 19 for m in cc_matches):
        detected.append('credit_card')

    # SSN Pattern: 9 consecutive digits (but not if part of longer number)
    # Format: 123-45-6789 or 123456789
    ssn_pattern_formatted = r'\b\d{3}[\s\-]\d{2}[\s\-]\d{4}\b'

    if re.search(ssn_pattern_formatted, text):
        detected.append('ssn')
    else:
        # Check for 9 consecutive digits that could be SSN
        # Find digit sequences and check for exactly 9 digits (not part of longer number)
        ssn_candidates = [m for m in re.findall(r'\d{9,}', normalized) if len(m) == 9]
        for candidate in ssn_candidates:
            # Basic SSN validation: first 3 digits can't be 000, 666, or 900-999
            area = int(candidate[:3])
            if area != 0 and area != 666 and area < 900:
                detected.append('ssn')
                break

    return {
        'has_sensitive': len(detected) > 0,
        'types': detected
    }


def get_sensitive_data_warning() -> str:
    """Return user-friendly warning message for sensitive data detection"""
    return (
        "I detected what looks like a credit card number or Social Security Number. "
        "For your security, I can't store this type of sensitive financial or personal data. "
        "Please don't send payment card numbers or SSNs via text.\n\n"
        "If you believe this is an error, please text FEEDBACK (your message) and we'll review it."
    )


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
    if event_type in ['AUTH_FAILURE', 'RATE_LIMIT', 'INVALID_SIGNATURE', 'VALIDATION_FAILURE', 'REQUEST_TIMEOUT']:
        logger.warning(log_message)
    else:
        logger.info(log_message)
