"""
Smart Suggestion Service
Detects opportunities to suggest follow-up actions after reminders, lists, and memories.
Phase 1: Prep reminders for high-importance events.
"""

import re
from datetime import datetime, timedelta
from config import logger

# Minimum hours between the prep reminder and the actual event.
# Don't suggest a prep reminder if there isn't enough lead time.
MIN_PREP_GAP_HOURS = 4

# Pattern definitions: (compiled regex, default prep hours before event, suggestion template)
# Prep hours are overridden by _calculate_prep_time for smarter timing.
PREP_REMINDER_PATTERNS = [
    # Medical
    (re.compile(r'\b(doctor|dentist|appointment|checkup|check-up|physical|therapist|surgeon|specialist|dermatologist|orthodontist|optometrist)\b', re.I),
     12, "prepare for your {event}"),
    (re.compile(r'\b(vet|veterinarian|pet appointment)\b', re.I),
     12, "prepare for the vet appointment"),

    # Professional
    (re.compile(r'\b(interview|job interview)\b', re.I),
     14, "prepare for your interview"),
    (re.compile(r'\b(presentation|demo|pitch)\b', re.I),
     14, "prepare for your {event}"),

    # Travel
    (re.compile(r'\b(flight|airport|travel|fly out|flying)\b', re.I),
     3, "leave for the airport"),
    (re.compile(r'\b(road trip|drive to|driving to)\b', re.I),
     2, "pack the car"),

    # Social / milestones
    (re.compile(r'\b(birthday|anniversary)\b', re.I),
     48, "get a gift for the {event}"),
    (re.compile(r'\b(wedding|ceremony|reception)\b', re.I),
     168, "confirm details for the {event}"),  # 1 week

    # Academic
    (re.compile(r'\b(exam|test|quiz|midterm|final)\b', re.I),
     14, "review for your {event}"),

    # Deadlines
    (re.compile(r'\b(deadline|due date|submission|due)\b', re.I),
     24, "finish up before the {event}"),

    # Moving
    (re.compile(r'\b(moving|move-in|move-out|moving day)\b', re.I),
     168, "start packing for your move"),  # 1 week

    # Financial
    (re.compile(r'\b(payment|bill|rent|mortgage|insurance)\b', re.I),
     48, "prepare for your {event}"),

    # Meetings (less aggressive — only if it looks formal)
    (re.compile(r'\b(meeting with|meet with|conference|call with)\b', re.I),
     2, "prepare for your {event}"),
]


def get_reminder_suggestion(
    reminder_text: str,
    reminder_date_utc: str,
    timezone: str,
) -> dict | None:
    """Check if a reminder warrants a prep suggestion.

    Args:
        reminder_text: The text of the reminder the user just created.
        reminder_date_utc: UTC datetime string (YYYY-MM-DD HH:MM:SS).
        timezone: User's timezone string (e.g., "America/New_York").

    Returns:
        dict with {suggestion_text, prep_date_utc, prep_reminder_text} or None.
    """
    import pytz

    # Match against patterns
    matched_pattern = None
    for pattern, default_hours, template in PREP_REMINDER_PATTERNS:
        if pattern.search(reminder_text):
            matched_pattern = (pattern, default_hours, template)
            break

    if not matched_pattern:
        return None

    pattern, default_hours, template = matched_pattern

    # Parse the reminder date
    try:
        reminder_dt_utc = datetime.strptime(reminder_date_utc, '%Y-%m-%d %H:%M:%S')
        tz = pytz.timezone(timezone)
        reminder_dt_local = pytz.UTC.localize(reminder_dt_utc).astimezone(tz)
        now_local = datetime.now(tz)
    except Exception as e:
        logger.error(f"Error parsing reminder date for suggestion: {e}")
        return None

    # Calculate prep time
    prep_dt_local = _calculate_prep_time(reminder_dt_local, default_hours, now_local)

    if prep_dt_local is None:
        return None

    # Check minimum gap
    hours_until_event = (reminder_dt_local - now_local).total_seconds() / 3600
    if hours_until_event < MIN_PREP_GAP_HOURS:
        return None

    # Check prep time is in the future
    if prep_dt_local <= now_local:
        return None

    # Build the suggestion
    # Extract the event keyword for the template
    match = matched_pattern[0].search(reminder_text)
    event_word = match.group(0) if match else "event"
    prep_text = template.format(event=event_word)

    # Format the suggestion time for display
    prep_time_str = prep_dt_local.strftime('%I:%M %p').lstrip('0')
    prep_date_str = prep_dt_local.strftime('%A, %B %d')

    # Determine how to phrase the suggestion
    days_diff = (reminder_dt_local.date() - prep_dt_local.date()).days
    if days_diff == 0:
        time_phrase = f"earlier that day at {prep_time_str}"
    elif days_diff == 1:
        time_phrase = f"the evening before at {prep_time_str}"
    elif days_diff <= 7:
        time_phrase = f"on {prep_date_str} at {prep_time_str}"
    else:
        time_phrase = f"on {prep_date_str}"

    suggestion_text = f"Want me to also remind you {time_phrase} to {prep_text}?"

    # Convert prep time back to UTC for storage
    prep_dt_utc = prep_dt_local.astimezone(pytz.UTC).replace(tzinfo=None)

    return {
        'suggestion_text': suggestion_text,
        'prep_date_utc': prep_dt_utc.strftime('%Y-%m-%d %H:%M:%S'),
        'prep_local_time': prep_dt_local.strftime('%H:%M'),
        'prep_reminder_text': prep_text.capitalize(),
        'prep_date_display': f"{prep_date_str} at {prep_time_str}",
    }


def _calculate_prep_time(
    event_dt_local: datetime,
    default_hours: int,
    now_local: datetime,
) -> datetime | None:
    """Calculate when the prep reminder should fire.

    Logic:
    - Events before noon → reminder at 7 PM the evening before
    - Events at noon or later → reminder 12 hours before (or default_hours)
    - Events days/weeks away (birthday, deadline, wedding) → use default_hours
    - If default_hours >= 48, use default_hours directly (multi-day lead)
    - Never schedule before now + 1 hour (give the user breathing room)
    """
    # Multi-day lead events (birthday, deadline, wedding, moving)
    if default_hours >= 48:
        prep_dt = event_dt_local - timedelta(hours=default_hours)
        # Set to 9 AM local for multi-day leads
        prep_dt = prep_dt.replace(hour=9, minute=0, second=0, microsecond=0)
        if prep_dt <= now_local + timedelta(hours=1):
            return None
        return prep_dt

    # Short-lead events (same day or next day)
    if event_dt_local.hour < 12:
        # Morning event → evening before at 7 PM
        prep_dt = (event_dt_local - timedelta(days=1)).replace(
            hour=19, minute=0, second=0, microsecond=0
        )
    else:
        # Afternoon/evening event → default_hours before
        prep_dt = event_dt_local - timedelta(hours=default_hours)

    # Don't schedule before now + 1 hour
    if prep_dt <= now_local + timedelta(hours=1):
        return None

    return prep_dt
