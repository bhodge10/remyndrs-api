"""
Formatting Utilities
Helper functions for formatting text and data
"""

def get_help_text():
    """Return help guide for users"""
    return """REMYNDRS HELP

Reminders, lists & memories - just text naturally!

Examples:
• "Remind me at 3pm to call mom"
• "Add milk to my grocery list"
• "Remember my WiFi is ABC123"

Text STATUS for your plan info.
Full guide: remyndrs.com/commands"""

def get_onboarding_prompt(step):
    """Get the appropriate prompt for the current onboarding step"""
    prompts = {
        1: "What's your first name?",
        2: "What's your last name?",
        3: """Email for account recovery?

(We only use this for important updates - no spam!)""",
        4: """ZIP code?

(This helps me send reminders at the right time in your timezone)"""
    }
    return prompts.get(step, "Let's continue your setup!")

def format_reminder_confirmation(reminder_text):
    """
    Format reminder text for confirmation messages.
    Handles cases where text starts with prepositions to avoid "to about" or "to for".

    Examples:
    - "call mom" -> "to call mom"
    - "about the dentist" -> "about the dentist" (no "to" prefix)
    - "for the meeting" -> "for the meeting" (no "to" prefix)
    - "that I need groceries" -> "that I need groceries" (no "to" prefix)
    """
    if not reminder_text:
        return ""

    text = reminder_text.strip()
    text_lower = text.lower()

    # If text already starts with a preposition, don't add "to"
    preposition_prefixes = ['about ', 'for ', 'that ', 'to ']
    for prefix in preposition_prefixes:
        if text_lower.startswith(prefix):
            return text

    return f"to {text}"


def format_reminders_list(reminders, user_tz):
    """Format reminders list for display"""
    from datetime import datetime, timedelta
    import pytz

    if not reminders:
        return "You don't have any reminders set."

    tz = pytz.timezone(user_tz)
    user_now = datetime.now(tz)

    scheduled = []
    completed = []

    # Tuple format: (id, reminder_date, reminder_text, recurring_id, sent)
    for reminder in reminders:
        reminder_id, reminder_date_utc, reminder_text, recurring_id, sent = reminder
        try:
            # Handle both datetime objects and strings
            if isinstance(reminder_date_utc, datetime):
                utc_dt = reminder_date_utc
                if utc_dt.tzinfo is None:
                    utc_dt = pytz.UTC.localize(utc_dt)
            else:
                utc_dt = datetime.strptime(str(reminder_date_utc), '%Y-%m-%d %H:%M:%S')
                utc_dt = pytz.UTC.localize(utc_dt)
            user_dt = utc_dt.astimezone(tz)

            # Smart date formatting
            if user_dt.date() == user_now.date():
                date_str = f"Today at {user_dt.strftime('%I:%M %p')}"
            elif user_dt.date() == (user_now + timedelta(days=1)).date():
                date_str = f"Tomorrow at {user_dt.strftime('%I:%M %p')}"
            else:
                date_str = user_dt.strftime('%a, %b %d at %I:%M %p')

            # Add [R] prefix for recurring reminders
            display_text = f"[R] {reminder_text}" if recurring_id else reminder_text

            if sent:
                completed.append((display_text, date_str))
            else:
                scheduled.append((display_text, date_str))
        except (ValueError, TypeError, AttributeError):
            display_text = f"[R] {reminder_text}" if recurring_id else reminder_text
            if sent:
                completed.append((display_text, ""))
            else:
                scheduled.append((display_text, ""))

    # Build response - only show scheduled reminders
    lines = []

    if scheduled:
        lines.append("SCHEDULED:")
        lines.append("")
        for i, (text, date) in enumerate(scheduled, 1):
            if date:
                lines.append(f"{i}. {text}")
                lines.append(f"   {date}")
            else:
                lines.append(f"{i}. {text}")
            lines.append("")  # Empty line between reminders

        # Add hint about completed reminders if there are any
        if completed:
            lines.append("(Text 'SHOW COMPLETED REMINDERS' to see past reminders)")
    elif completed:
        # No scheduled but have completed
        lines.append("You don't have any upcoming reminders.")
        lines.append("")
        lines.append("(Text 'SHOW COMPLETED REMINDERS' to see past reminders)")
    else:
        return "You don't have any reminders set."

    return "\n".join(lines).strip()
