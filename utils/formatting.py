"""
Formatting Utilities
Helper functions for formatting text and data
"""

def get_help_text():
    """Return help guide for users"""
    return """REMYNDRS HELP GUIDE

MEMORIES
Store: "My car is a 2018 Honda, VIN ABC123"
Recall: "What's my VIN?"
Delete: "Forget my car info"

REMINDERS
Set: "Remind me at 3pm to call mom"
Relative: "Remind me in 30 minutes to check oven"
View: "Show my reminders"
Delete: "Delete reminder about mom"
Snooze: Reply SNOOZE when you get a reminder (adds 15 min), or "SNOOZE 1h" for 1 hour

LISTS
Create: "Create a grocery list"
Add: "Add milk, eggs, bread to grocery list"
View: "Show my grocery list" or "MY LISTS"
Check off: "Check off milk"
Remove: "Remove eggs from list"

QUICK COMMANDS
? - This help guide
MY LISTS - View all lists
LIST ALL - View all memories
SNOOZE - Snooze last reminder
STOP - Pauses texts from Remyndrs (your account and data stay active, text START to resume)
FEEDBACK: [message] - Send us feedback

TIPS
- Always include AM or PM for times
- I understand natural language - just talk!
- Reply SNOOZE within 30 min of a reminder"""

def get_onboarding_prompt(step):
    """Get the appropriate prompt for the current onboarding step"""
    prompts = {
        1: "What's your first name?",
        2: "What's your last name?",
        3: "What's your email address?",
        4: "What's your ZIP code?",
        5: "How did you hear about us? (Reply: Reddit, Facebook, Google, Friend, Ad, or skip)"
    }
    return prompts.get(step, "Let's continue your setup!")

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

    # Tuple format: (id, reminder_date, reminder_text, snoozed_until, sent)
    for reminder in reminders:
        reminder_id, reminder_date_utc, reminder_text, snoozed_until, sent = reminder
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

            if sent:
                completed.append((reminder_text, date_str))
            else:
                scheduled.append((reminder_text, date_str))
        except:
            if sent:
                completed.append((reminder_text, ""))
            else:
                scheduled.append((reminder_text, ""))

    # Build response
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

    if completed:
        if lines:
            lines.append("")
        lines.append("COMPLETED:")
        lines.append("")
        for i, (text, date) in enumerate(completed[-5:], 1):
            if date:
                lines.append(f"{i}. {text}")
                lines.append(f"   {date}")
            else:
                lines.append(f"{i}. {text}")
            lines.append("")  # Empty line between reminders

    return "\n".join(lines).strip() if lines else "You don't have any reminders set."
