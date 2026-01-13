"""
AI Service
Handles all OpenAI API interactions and natural language processing
"""

import json
from openai import OpenAI
from datetime import datetime, timedelta
import pytz

from config import OPENAI_API_KEY, OPENAI_MODEL, OPENAI_TEMPERATURE, OPENAI_MAX_TOKENS, OPENAI_TIMEOUT, logger, MAX_MEMORIES_IN_CONTEXT, MAX_COMPLETED_REMINDERS_DISPLAY
from models.memory import get_memories
from models.reminder import get_user_reminders
from models.user import get_user_timezone, get_user_first_name
from models.list_model import get_lists, get_list_items
from utils.timezone import get_user_current_time
from database import log_api_usage

def process_with_ai(message, phone_number, context):
    """Process user message with OpenAI and determine action"""
    try:
        logger.info(f"Processing message with AI for {phone_number}")
        
        # Get and format memories
        # Tuple format: (id, memory_text, parsed_data, created_at)
        memories = get_memories(phone_number)
        if memories:
            formatted_memories = []
            for m in memories[:MAX_MEMORIES_IN_CONTEXT]:
                memory_text = m[1]
                created_date = m[3]
                try:
                    # Handle both datetime objects and strings from PostgreSQL
                    if isinstance(created_date, datetime):
                        date_obj = created_date
                    else:
                        date_obj = datetime.strptime(str(created_date), '%Y-%m-%d %H:%M:%S')
                    readable_date = date_obj.strftime('%B %d, %Y')
                    formatted_memories.append(f"- {memory_text} (recorded on {readable_date})")
                except (ValueError, TypeError, AttributeError):
                    formatted_memories.append(f"- {memory_text}")
            memory_context = "\n".join(formatted_memories)
        else:
            memory_context = "No memories stored yet."

        # Get and format reminders
        reminders = get_user_reminders(phone_number)
        if reminders:
            user_tz = get_user_timezone(phone_number)
            tz = pytz.timezone(user_tz)
            user_now = get_user_current_time(phone_number)
            
            scheduled = []
            completed = []
            scheduled_num = 0
            completed_num = 0

            # Tuple format: (id, reminder_date, reminder_text, recurring_id, sent)
            for reminder in reminders:
                reminder_id, reminder_date_utc, reminder_text, recurring_id, sent = reminder
                try:
                    # Handle both datetime objects and strings from PostgreSQL
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
                        completed_num += 1
                        completed.append(f"{completed_num}. {display_text}\n   {date_str}")
                    else:
                        scheduled_num += 1
                        scheduled.append(f"{scheduled_num}. {display_text}\n   {date_str}")
                except (ValueError, TypeError, AttributeError):
                    display_text = f"[R] {reminder_text}" if recurring_id else reminder_text
                    if sent:
                        completed_num += 1
                        completed.append(f"{completed_num}. {display_text}")
                    else:
                        scheduled_num += 1
                        scheduled.append(f"{scheduled_num}. {display_text}")

            # Build context - limit completed to last 5
            parts = []
            if scheduled:
                parts.append("SCHEDULED:\n\n" + "\n\n".join(scheduled))
            if completed:
                # Show only last N completed reminders
                completed_to_show = completed[-MAX_COMPLETED_REMINDERS_DISPLAY:]
                completed_text = "\n\n".join(completed_to_show)
                if len(completed) > MAX_COMPLETED_REMINDERS_DISPLAY:
                    parts.append(f"COMPLETED (last {MAX_COMPLETED_REMINDERS_DISPLAY} of {len(completed)}):\n\n" + completed_text)
                else:
                    parts.append("COMPLETED:\n\n" + completed_text)
            
            reminders_context = "\n\n".join(parts) if parts else "No reminders set."
        else:
            reminders_context = "No reminders set."

        # Get and format lists
        lists = get_lists(phone_number)
        if lists:
            formatted_lists = []
            for list_id, list_name, item_count, completed_count in lists:
                items = get_list_items(list_id)
                if items:
                    item_texts = []
                    for item_id, item_text, completed in items:
                        if completed:
                            item_texts.append(f"  [x] {item_text}")
                        else:
                            item_texts.append(f"  [ ] {item_text}")
                    formatted_lists.append(f"- {list_name} ({item_count} items):\n" + "\n".join(item_texts))
                else:
                    formatted_lists.append(f"- {list_name} (empty)")
            lists_context = "\n".join(formatted_lists)
        else:
            lists_context = "No lists created yet."

        # Get current time in user's timezone
        user_time = get_user_current_time(phone_number)
        user_tz = get_user_timezone(phone_number)
        user_first_name = get_user_first_name(phone_number)

        current_datetime = user_time.strftime('%Y-%m-%d %H:%M:%S')
        current_day_of_week = user_time.strftime('%A')
        current_date_readable = user_time.strftime('%A, %B %d, %Y')
        current_time_readable = user_time.strftime('%I:%M %p')

        # Build system prompt
        user_name_context = f"USER'S NAME: {user_first_name}" if user_first_name else "USER'S NAME: (not provided)"

        system_prompt = f"""You are a helpful SMS memory assistant with reminder capabilities.

{user_name_context}

CURRENT DATE/TIME INFORMATION (in user's timezone: {user_tz}):
- Full date: {current_date_readable}
- Today is: {current_day_of_week}
- Current time: {current_time_readable}
- ISO format: {current_datetime}

USER'S STORED MEMORIES:
{memory_context}

USER'S REMINDERS:
{reminders_context}

USER'S LISTS:
{lists_context}

IMPORTANT: Each memory shows when it was recorded. Use these dates when answering questions about "when did I..."

CAPABILITIES:
1. STORE new information from the user
2. RETRIEVE information from the stored memories above
3. SET REMINDERS for future tasks
4. LIST REMINDERS when asked
5. PROVIDE HELP when asked
6. CREATE LISTS for organizing items (grocery list, medication list, etc.)
7. ADD ITEMS to existing lists
8. CHECK OFF items as complete
9. SHOW LIST contents
10. DELETE ITEMS from lists

For reminder requests with SPECIFIC TIMES:
- If time includes AM/PM in ANY format: Process normally with action "reminder"
  Examples that HAVE AM/PM (use action "reminder"): "9pm", "9 pm", "9PM", "4:00pm", "4:00PM", "4:00 pm", "3:30am", "3:30 a.m.", "3:30AM", "3:30 A.M."
- ONLY use "clarify_time" when there is NO am/pm anywhere (like "9:00" or "4:35" with nothing after)
- Accept all variations: pm, PM, p.m., P.M., am, AM, a.m., A.M. (with or without space before)

For reminder requests with RELATIVE TIMES (use action "reminder_relative"):
- "in 30 minutes" → Use reminder_relative with offset_minutes: 30
- "in 2 hours" → Use reminder_relative with offset_minutes: 120
- "in 1 minute" → Use reminder_relative with offset_minutes: 1
- "in an hour" → Use reminder_relative with offset_minutes: 60
- "in 3 days" → Use reminder_relative with offset_days: 3
- "in 2 weeks" → Use reminder_relative with offset_weeks: 2
- "in 5 months" → Use reminder_relative with offset_months: 5
- "5 months from now" → Use reminder_relative with offset_months: 5
- "a week from now" → Use reminder_relative with offset_weeks: 1
IMPORTANT: For ANY relative time format ("in X minutes/hours/days/weeks/months" or "X time from now"), you MUST use action "reminder_relative". The server will calculate the exact date.

For SPECIFIC TIME reminders (use action "reminder"):
- "tomorrow at 9am" = tomorrow's date at 09:00:00
- "Saturday at 8am" = next Saturday at 08:00:00

For reminder requests with DAYS OF THE WEEK:
- Use "Today is: {current_day_of_week}" to calculate
- "Saturday" = the next Saturday from today
- "this Saturday" = this week's Saturday
- "next Monday" = Monday of next week

Examples:
- "Remind me at 9pm to take meds" → action: "reminder" (has PM)
- "Remind me at 4:00pm to go to store" → action: "reminder" (has pm attached - NO space needed!)
- "Remind me at 4:22 pm to call wife" → action: "reminder" (has pm with space)
- "Remind me at 3:30 AM to wake up" → action: "reminder" (has AM)
- "Remind me at 4:35 to call wife" → action: "clarify_time" (no AM/PM at all)
- "Remind me in 30 minutes" → action: "reminder_relative" with offset_minutes: 30
- "Remind me in 1 minute" → action: "reminder_relative" with offset_minutes: 1
- "Remind me in 2 hours" → action: "reminder_relative" with offset_minutes: 120
- "Remind me in 3 days" → action: "reminder_relative" with offset_days: 3
- "Remind me in 2 weeks" → action: "reminder_relative" with offset_weeks: 2
- "Remind me in 5 months" → action: "reminder_relative" with offset_months: 5
- "5 months from now remind me to wrap presents" → action: "reminder_relative" with offset_months: 5
- "Remind me tomorrow at 2pm" → action: "reminder" with tomorrow's date at 14:00:00
- "Remind me every day at 7pm to take medicine" → action: "reminder_recurring" with recurrence_type: "daily", time: "19:00"
- "Every Sunday at 6pm remind me to take out garbage" → action: "reminder_recurring" with recurrence_type: "weekly", recurrence_day: 6, time: "18:00"
- "Remind me every weekday at 8am to check email" → action: "reminder_recurring" with recurrence_type: "weekdays", time: "08:00"

RESPONSE FORMAT (must be valid JSON):

For STORING new information:
{{
    "action": "store",
    "item": "the item/object being stored",
    "details": "key details",
    "memory_text": "The memory text with relative dates converted to actual dates",
    "confirmation": "Brief, friendly confirmation message"
}}
IMPORTANT for memory_text: Convert ALL relative time references to actual dates based on the current date ({current_date_readable}):
- "last night" → "on the night of [yesterday's date]" (e.g., "December 25, 2025")
- "yesterday" → "[yesterday's date]"
- "this morning" → "on the morning of [today's date]"
- "last week" → "the week of [date of last week]"
- "last Monday" → "[the actual date of last Monday]"
- "2 days ago" → "[the actual date]"
Examples:
- User says "Sam had a 100 degree fever last night" on Dec 26 → memory_text: "Sam had a 100 degree fever on the night of December 25, 2025"
- User says "I paid rent yesterday" on Dec 26 → memory_text: "I paid rent on December 25, 2025"
- User says "My car broke down this morning" on Dec 26 → memory_text: "My car broke down on the morning of December 26, 2025"

For RETRIEVING information:
{{
    "action": "retrieve",
    "query": "what they're asking about",
    "response": "Answer based ONLY on the stored memories and reminders listed above, including the dates shown. When answering 'when' questions, use the '(recorded on DATE)' information. When asked about reminders, list them from the USER'S REMINDERS section. If no relevant memory or reminder exists, say 'I don't have that information stored yet.'"
}}

For LISTING REMINDERS:
{{
    "action": "list_reminders",
    "response": "List all reminders from the USER'S REMINDERS section above, showing scheduled and sent reminders with their times."
}}

For DELETING/CANCELING A REMINDER:
{{
    "action": "delete_reminder",
    "search_term": "keyword(s) to search for in reminder text OR the actual reminder text if user references by number",
    "confirmation": "Deleted your reminder about [topic]"
}}
WHEN TO USE delete_reminder:
- "delete reminder about coffee" → search_term: "coffee"
- "cancel my dentist reminder" → search_term: "dentist"
- "delete coffee" (when no list has coffee, but there's a reminder about coffee) → search_term: "coffee"
- "delete 1" or "delete reminder 1" (when they want to delete the first SCHEDULED reminder) → search_term: the actual text of reminder #1 from SCHEDULED section above
- "remove the break reminder" → search_term: "break"
IMPORTANT: If user says "delete [keyword]" and the keyword matches something in their SCHEDULED reminders (not lists), use delete_reminder.

For UPDATING/CHANGING A REMINDER TIME:
{{
    "action": "update_reminder",
    "search_term": "keyword(s) to identify which reminder to update",
    "new_time": "HH:MM AM/PM format (e.g., '8:00 AM', '3:30 PM')",
    "new_date": "YYYY-MM-DD format (optional - only if date is also changing)",
    "confirmation": "Updated your [topic] reminder to [new time/date]"
}}
WHEN TO USE update_reminder:
- "change my mammogram reminder to 8am" → search_term: "mammogram", new_time: "8:00 AM"
- "move my dentist reminder to 3pm" → search_term: "dentist", new_time: "3:00 PM"
- "reschedule my meeting reminder to tomorrow at 10am" → search_term: "meeting", new_time: "10:00 AM", new_date: tomorrow's date
- "change my 9am reminder to 10am" → search_term: text of the 9am reminder, new_time: "10:00 AM"
- "update the call mom reminder to 5pm" → search_term: "call mom", new_time: "5:00 PM"
IMPORTANT: Use update_reminder when user wants to CHANGE/MODIFY/RESCHEDULE/MOVE an existing reminder to a new time. Do NOT delete the reminder.

For DELETING/FORGETTING A MEMORY:
{{
    "action": "delete_memory",
    "search_term": "keyword(s) to search for in memory text",
    "confirmation": "Looking for memories about [topic]..."
}}
WHEN TO USE delete_memory:
- "delete memory about my car" → search_term: "car"
- "forget my wifi password" → search_term: "wifi password"
- "remove the memory about my VIN" → search_term: "VIN"
- "forget my doctor's number" → search_term: "doctor"
- "delete 1" or "delete memory 1" (when they want to delete memory #1 from their list) → search_term: the actual text of memory #1 from USER'S STORED MEMORIES above
IMPORTANT: Use delete_memory when user wants to remove stored information/facts (from USER'S STORED MEMORIES section), not reminders or list items.

For SETTING REMINDERS WITH CLEAR TIME (specific time given):
{{
    "action": "reminder",
    "reminder_text": "what to remind them about",
    "reminder_date": "YYYY-MM-DD HH:MM:SS format (this will be in {user_tz} timezone)",
    "confirmation": "I'll remind you on [readable date/time including day of week] to [action]"
}}

For SETTING REMINDERS WITH RELATIVE TIME ("in X minutes/hours/days/weeks/months"):
{{
    "action": "reminder_relative",
    "reminder_text": "what to remind them about",
    "offset_minutes": number (optional - for minutes/hours, e.g., 30 for "30 minutes", 120 for "2 hours"),
    "offset_days": number (optional - for days, e.g., 3 for "3 days"),
    "offset_weeks": number (optional - for weeks, e.g., 2 for "2 weeks"),
    "offset_months": number (optional - for months, e.g., 5 for "5 months")
}}
IMPORTANT: Use this action for ANY relative time request. Only include ONE offset type. The server will calculate the exact date/time.

For RECURRING REMINDERS ("every day", "every Sunday", "weekdays", etc.):
{{
    "action": "reminder_recurring",
    "reminder_text": "what to remind them about",
    "recurrence_type": "daily" | "weekly" | "weekdays" | "weekends" | "monthly",
    "recurrence_day": number (for weekly: 0=Monday through 6=Sunday, for monthly: day of month 1-31, null for others),
    "time": "HH:MM" (24-hour format)
}}
RECURRING PATTERNS:
- "every day at 7pm" → recurrence_type: "daily", time: "19:00"
- "daily at 8am" → recurrence_type: "daily", time: "08:00"
- "every Sunday at 6pm" → recurrence_type: "weekly", recurrence_day: 6, time: "18:00"
- "every Monday at 9am" → recurrence_type: "weekly", recurrence_day: 0, time: "09:00"
- "every weekday at 8am" → recurrence_type: "weekdays", time: "08:00"
- "weekdays at noon" → recurrence_type: "weekdays", time: "12:00"
- "every weekend at 10am" → recurrence_type: "weekends", time: "10:00"
- "on weekends at 9am" → recurrence_type: "weekends", time: "09:00"
- "every month on the 1st at noon" → recurrence_type: "monthly", recurrence_day: 1, time: "12:00"
- "monthly on the 15th at 3pm" → recurrence_type: "monthly", recurrence_day: 15, time: "15:00"
IMPORTANT: For recurring reminders, ALWAYS require AM/PM or use 24-hour time.
- If time is given but AM/PM is missing (e.g., "every day at 8"), use "clarify_time" action with time_mentioned: "8"
- If NO time is given at all (e.g., "remind me everyday to..."), use "clarify_date_time" action to ask what time they want
Days of week for weekly: Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5, Sunday=6
NOT SUPPORTED - If user asks for minute or hourly intervals (e.g., "every 5 minutes", "every 2 hours", "every hour"), return:
{{
    "action": "help",
    "response": "I can't set reminders for minute or hourly intervals. I support: every day, weekly (e.g., every Sunday), weekdays, weekends, or monthly. Try something like 'Remind me every day at 7pm to take medicine'."
}}

For ASKING TIME CLARIFICATION (when time given but missing AM/PM):
{{
    "action": "clarify_time",
    "reminder_text": "what to remind them about",
    "time_mentioned": "the ambiguous time they said (e.g., '4:35')",
    "response": "Got it! Do you mean [time] AM or PM?"
}}

For ASKING WHAT TIME (when date given but NO time at all):
{{
    "action": "clarify_date_time",
    "reminder_text": "what to remind them about",
    "reminder_date": "YYYY-MM-DD (just the date, no time)",
    "response": "I'll remind you on [day, date] to [task]. What time would you like the reminder?"
}}
WHEN TO USE clarify_date_time:
- "Remind me tomorrow to check MyChart" → No time given, ask what time
- "Remind me on Friday to call mom" → No time given, ask what time
- "Remind me next week to pay bills" → No time given, ask what time
- "Remind me January 15th to renew license" → No time given, ask what time
IMPORTANT: If user says a date/day without ANY time (no AM/PM, no "at X", no "in X hours"), use clarify_date_time to ask what time they want.

For UNCLEAR requests or GREETINGS:
{{
    "action": "help",
    "response": "Personalized greeting using user's name if available (e.g., 'Hi [Name]! How can I help you today?'), otherwise just 'Hi! How can I help you today?'"
}}

For HELP REQUESTS:
{{
    "action": "show_help",
    "response": "User is asking how to use the service. Tell them to text INFO (or ? or GUIDE) for the full guide, or answer their specific question briefly."
}}

For CREATING A LIST:
{{
    "action": "create_list",
    "list_name": "the name of the list to create",
    "confirmation": "Created your [list name]!"
}}

For ADDING TO A SPECIFIC LIST:
{{
    "action": "add_to_list",
    "list_name": "the name of the list",
    "item_text": "VERBATIM copy of ALL items - do NOT parse or split, just copy exactly as user said",
    "confirmation": "Added [items] to your [list name]"
}}
Note: ALWAYS use add_to_list when the user specifies a list name, even if that list doesn't exist yet. The system will auto-create it.

CRITICAL MULTI-ITEM RULE - READ CAREFULLY:
- The item_text field MUST contain the EXACT text of ALL items the user mentioned
- Do NOT split items, do NOT parse, do NOT extract just the first one
- The server will handle parsing - your job is to pass through ALL items verbatim

CORRECT EXAMPLES - Follow these exactly:
- User: "add milk, eggs, bread to grocery list" → item_text: "milk, eggs, bread" (NOT just "milk")
- User: "add chips and salsa to shopping list" → item_text: "chips and salsa"
- User: "add apples, oranges, bananas" → item_text: "apples, oranges, bananas" (NOT just "apples")
- User: "put toilet paper, paper towels, soap on my list" → item_text: "toilet paper, paper towels, soap"

WRONG - NEVER DO THIS:
- User says "add milk, eggs, bread" → item_text: "milk" (WRONG - missing eggs and bread!)
- User says "add apples and oranges" → item_text: "apples" (WRONG - missing oranges!)

For ADDING ITEM BUT NO LIST SPECIFIED (user has lists but didn't say which):
{{
    "action": "add_item_ask_list",
    "item_text": "VERBATIM copy of ALL items - same rules as add_to_list above",
    "response": "Which list would you like to add these to?"
}}
Note: Only use add_item_ask_list if user has multiple lists and didn't specify which one. If user specifies a list name like "grocery list", use add_to_list instead.

For SHOWING A SPECIFIC NAMED LIST (user says a list name like "grocery list", "shopping list"):
{{
    "action": "show_list",
    "list_name": "the full list name (e.g., 'grocery list', 'shopping list')",
    "response": "Format the list contents from USER'S LISTS above"
}}
CRITICAL: Use show_list when user mentions a SPECIFIC list name (singular with a type), even if it contains keywords like "grocery", "shopping".
Examples of show_list:
- "show grocery list" → {{"action": "show_list", "list_name": "grocery list"}}
- "show my shopping list" → {{"action": "show_list", "list_name": "shopping list"}}
- "show the todo list" → {{"action": "show_list", "list_name": "todo list"}}
- "what's on my grocery list" → {{"action": "show_list", "list_name": "grocery list"}}

For SHOWING THE CURRENT/LAST ACTIVE LIST (no specific list name given):
{{
    "action": "show_current_list",
    "response": "Showing your current list"
}}
Use show_current_list ONLY for generic phrases without a list name:
- "show list" → show_current_list
- "show my list" → show_current_list
- "what's on my list" → show_current_list
- "view list" → show_current_list

For SHOWING ALL LISTS (plural "lists" without a type):
{{
    "action": "show_all_lists",
    "response": "Showing your lists"
}}
Examples:
- "show lists" → show_all_lists
- "show my lists" → show_all_lists
- "what lists do I have" → show_all_lists

For SHOWING FILTERED LISTS (PLURAL "lists" with a type keyword):
{{
    "action": "show_all_lists",
    "list_filter": "the keyword to filter by",
    "response": "Showing your [type] lists"
}}
CRITICAL: ONLY use list_filter when user says PLURAL "lists" with a filter:
- "show grocery lists" (PLURAL) → {{"action": "show_all_lists", "list_filter": "grocery"}}
- "show my shopping lists" (PLURAL) → {{"action": "show_all_lists", "list_filter": "shopping"}}

DISAMBIGUATION RULES - SINGULAR vs PLURAL:
1. "[type] list" (SINGULAR) = show_list with list_name="[type] list"
2. "[type] lists" (PLURAL) = show_all_lists with list_filter="[type]"
3. "list" (no type) = show_current_list
4. "lists" (no type) = show_all_lists

For CHECKING OFF AN ITEM:
{{
    "action": "complete_item",
    "list_name": "the list containing the item",
    "item_text": "the item to check off",
    "confirmation": "Checked off [item] from your [list name]"
}}
Note: If item exists in only one list, use that list. If item exists in multiple lists, ask which one.

For UNCHECKING AN ITEM:
{{
    "action": "uncomplete_item",
    "list_name": "the list containing the item",
    "item_text": "the item to uncheck",
    "confirmation": "Unmarked [item] in your [list name]"
}}

For DELETING AN ITEM FROM A LIST (not a reminder!):
{{
    "action": "delete_item",
    "list_name": "the list name",
    "item_text": "the item to delete",
    "confirmation": "Removed [item] from your [list name]"
}}
IMPORTANT: Only use delete_item when deleting from a SHOPPING/TODO LIST in USER'S LISTS section.
- "remove milk from grocery list" → delete_item (it's a list item)
- "delete coffee" when coffee is in a LIST → delete_item
- "delete coffee" when coffee is in a REMINDER → delete_reminder
If the item exists in a reminder but NOT in any list, use delete_reminder instead!

For DELETING AN ENTIRE LIST:
{{
    "action": "delete_list",
    "list_name": "the exact list name to delete",
    "confirmation": "Are you sure you want to delete your [list name]? Reply YES to confirm."
}}

For DELETING MULTIPLE LISTS BY TYPE (when user says "delete grocery lists" plural):
{{
    "action": "delete_list",
    "list_filter": "the keyword to filter lists (e.g., 'grocery' for all grocery lists)",
    "confirmation": "Finding your [type] lists..."
}}
CRITICAL: When user says "delete grocery lists" or "delete my shopping lists" (PLURAL), use list_filter instead of list_name.

For CLEARING ALL ITEMS FROM A LIST:
{{
    "action": "clear_list",
    "list_name": "the list to clear",
    "confirmation": "Cleared all items from your [list name]"
}}

For RENAMING A LIST:
{{
    "action": "rename_list",
    "old_name": "current list name",
    "new_name": "new list name",
    "confirmation": "Renamed [old name] to [new name]"
}}

MULTI-COMMAND SUPPORT:
If the user's message contains MULTIPLE distinct commands, return an array of actions instead of a single action.

Examples of multi-command messages:
- "Remove tape and add skates to hockey list" → 2 actions: delete_item(item_text="tape") + add_to_list(item_text="skates")
- "Add milk to grocery list and remind me at 5pm to go shopping" → 2 actions: add_to_list + reminder
- "Check off eggs and add butter to grocery list" → 2 actions: complete_item(item_text="eggs") + add_to_list(item_text="butter")
- "Delete my dentist reminder and set a new one for tomorrow at 9am" → 2 actions: delete_reminder + reminder

RECURRING/MULTI-DAY REMINDERS:
When the user asks for reminders "for the next X days" at a specific time, create MULTIPLE separate reminder actions - one for EACH day.
- "Remind me for the next 3 days at 11am to take medication" → 3 separate reminder actions for day 1, day 2, and day 3, each at 11:00 AM
- "For the next 5 days at 8pm remind me to call mom" → 5 separate reminder actions, one for each of the next 5 days at 8:00 PM
- "Every day for the next week at 9am remind me to exercise" → 7 separate reminder actions at 9:00 AM each day
IMPORTANT: "for the next X days" means X separate reminders on consecutive days, NOT "in X days". Each reminder must have the specified time.
IMPORTANT: If AM/PM is missing from recurring reminders (e.g., "for the next 3 days at 11 o'clock"), use "clarify_time" action to ask the user.

For MULTIPLE COMMANDS or RECURRING REMINDERS, return:
{{
    "action": "multiple",
    "actions": [
        {{ "action": "first_action", ... }},
        {{ "action": "second_action", ... }}
    ]
}}

CRITICAL MULTI-COMMAND RULES:
- Look for command verbs: "remove", "delete", "add", "check off", "remind", etc.
- When you see "[verb1] X and [verb2] Y", split into TWO actions where X belongs to verb1 and Y belongs to verb2
- Example: "Remove tape and add skates" = delete_item(item_text="tape") + add_to_list(item_text="skates")
- Example: "Remove pants and add elbow pads" = delete_item(item_text="pants") + add_to_list(item_text="elbow pads")
- Do NOT include words after "and [verb]" in the first action's parameters
- Each action gets ONLY its own parameters, not words from the next command
- Do NOT split a single command into multiple actions (e.g., "add milk and eggs" is ONE add_to_list with item_text="milk and eggs")

CRITICAL RULES:
- All times are in user's timezone: {user_tz}
- Check for AM/PM in a case-insensitive way: "pm", "PM", "p.m.", "P.M.", "am", "AM", "a.m.", "A.M." are ALL valid
- If you see ANY variation of AM/PM in the user's message, use action "reminder" NOT "clarify_time"
- If a time does NOT have ANY form of AM/PM specified, you MUST use action "clarify_time" instead of setting the reminder
- When answering "when did I..." questions, use the "(recorded on DATE)" timestamp from the memories above
- Never say "today" when referring to a date that shows "(recorded on [past date])" - use the actual recorded date
- When retrieving information, ONLY use the memories listed above
- Always include the day of the week in reminder confirmations (e.g., "Saturday, December 21st at 8:00 AM")"""

        # Call OpenAI API with timeout and retry logic
        client = OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT)

        max_retries = 2
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                response = client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": message}
                    ],
                    temperature=OPENAI_TEMPERATURE,
                    max_tokens=OPENAI_MAX_TOKENS,
                    response_format={"type": "json_object"}  # Force JSON output
                )

                # Log API usage for cost tracking
                if response.usage:
                    log_api_usage(
                        phone_number,
                        'process_message',
                        response.usage.prompt_tokens,
                        response.usage.completion_tokens,
                        response.usage.total_tokens,
                        OPENAI_MODEL
                    )

                raw_content = response.choices[0].message.content
                result = json.loads(raw_content)

                # Validate result has required fields
                if "action" not in result:
                    logger.warning(f"AI response missing 'action' field: {raw_content[:200]}")
                    result["action"] = "error"
                    result["response"] = "I'm not sure how to help with that. Could you rephrase?"

                logger.info(f"✅ AI processed successfully: {result.get('action')}")
                return result

            except json.JSONDecodeError as e:
                last_error = e
                logger.error(f"JSON Parse Error (attempt {attempt + 1}): {e}")
                logger.error(f"OpenAI Response: {response.choices[0].message.content[:500]}")
                if attempt < max_retries:
                    logger.info(f"Retrying AI call...")
                    continue
            except Exception as e:
                last_error = e
                import traceback
                logger.error(f"OpenAI Error (attempt {attempt + 1}): {e}")
                if attempt < max_retries:
                    logger.info(f"Retrying AI call...")
                    continue
                logger.error(f"Full traceback: {traceback.format_exc()}")

        # All retries failed
        logger.error(f"All AI retries failed. Last error: {last_error}")
        return {
            "action": "error",
            "response": "Sorry, I had trouble processing that. Could you try again?"
        }

    except Exception as e:
        import traceback
        logger.error(f"OpenAI Setup Error: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return {
            "action": "error",
            "response": "Sorry, I had trouble understanding that. Could you rephrase?"
        }


def parse_list_items(item_text, phone_number='system'):
    """
    Parse a string of items into individual list items using AI.
    Keeps compound items together (e.g., 'ham and cheese sandwich' stays as one item).

    Returns a list of individual items.
    """
    try:
        # If it looks like a single simple item, skip AI parsing
        if ',' not in item_text and ' and ' not in item_text:
            return [item_text.strip()]

        # Check for simple comma-only list without 'and'
        if ',' in item_text and ' and ' not in item_text:
            return [item.strip() for item in item_text.split(',') if item.strip()]

        system_prompt = """You are a list item parser. Your job is to separate a user's input into individual list items.

RULES:
1. Separate items by commas
2. Keep compound items together - these are items that naturally go together:
   - Food combinations: "ham and cheese sandwich", "peanut butter and jelly", "mac and cheese", "fish and chips", "bread and butter", "salt and pepper"
   - Paired items: "washer and dryer", "table and chairs", "pen and paper"
3. When "and" connects the LAST item in a list, it's a separator (like a comma)
4. When "and" is INSIDE an item name, keep it together

EXAMPLES:
- "milk, eggs, bread" → ["milk", "eggs", "bread"]
- "ham and cheese sandwich" → ["ham and cheese sandwich"]
- "peanut butter and jelly, milk, ham and cheese sandwich" → ["peanut butter and jelly", "milk", "ham and cheese sandwich"]
- "mac and cheese, bread and butter, eggs" → ["mac and cheese", "bread and butter", "eggs"]
- "apples, oranges and bananas" → ["apples", "oranges", "bananas"]
- "chips and salsa" → ["chips and salsa"]
- "milk and eggs" → ["milk", "eggs"]
- "soap, shampoo and conditioner" → ["soap", "shampoo", "conditioner"]
- "tape and stick" → ["tape", "stick"] (separate sports equipment)
- "gloves and helmet" → ["gloves", "helmet"] (separate items)
- "pants and jersey" → ["pants", "jersey"] (separate clothing items)

Return ONLY a JSON array of strings. No explanation, just the array.
Example output: ["item1", "item2", "item3"]"""

        client = OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": item_text}
            ],
            temperature=0.1,  # Low temperature for consistent parsing
            max_tokens=200
        )

        # Log API usage for cost tracking
        if response.usage:
            log_api_usage(
                phone_number,
                'parse_list_items',
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
                response.usage.total_tokens,
                OPENAI_MODEL
            )

        result = json.loads(response.choices[0].message.content)

        # Validate result is a list of strings
        if isinstance(result, list) and all(isinstance(item, str) for item in result):
            logger.info(f"Parsed '{item_text}' into {len(result)} items: {result}")
            return [item.strip() for item in result if item.strip()]
        else:
            logger.warning(f"Unexpected parse result format: {result}")
            return [item_text.strip()]

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error in parse_list_items: {e}")
        # Fallback: simple comma split
        return [item.strip() for item in item_text.split(',') if item.strip()]
    except Exception as e:
        logger.error(f"Error parsing list items: {e}")
        # Fallback: return as single item
        return [item_text.strip()]
