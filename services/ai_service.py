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
from models.user import get_user_timezone
from models.list_model import get_lists, get_list_items
from utils.timezone import get_user_current_time

def process_with_ai(message, phone_number, context):
    """Process user message with OpenAI and determine action"""
    try:
        logger.info(f"Processing message with AI for {phone_number}")
        
        # Get and format memories
        memories = get_memories(phone_number)
        if memories:
            formatted_memories = []
            for m in memories[:MAX_MEMORIES_IN_CONTEXT]:
                memory_text = m[0]
                created_date = m[2]
                try:
                    # Handle both datetime objects and strings from PostgreSQL
                    if isinstance(created_date, datetime):
                        date_obj = created_date
                    else:
                        date_obj = datetime.strptime(str(created_date), '%Y-%m-%d %H:%M:%S')
                    readable_date = date_obj.strftime('%B %d, %Y')
                    formatted_memories.append(f"- {memory_text} (recorded on {readable_date})")
                except:
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

            for reminder_text, reminder_date_utc, sent in reminders:
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

                    if sent:
                        completed_num += 1
                        completed.append(f"{completed_num}. {reminder_text}\n   {date_str}")
                    else:
                        scheduled_num += 1
                        scheduled.append(f"{scheduled_num}. {reminder_text}\n   {date_str}")
                except:
                    if sent:
                        completed_num += 1
                        completed.append(f"{completed_num}. {reminder_text}")
                    else:
                        scheduled_num += 1
                        scheduled.append(f"{scheduled_num}. {reminder_text}")

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

        current_datetime = user_time.strftime('%Y-%m-%d %H:%M:%S')
        current_day_of_week = user_time.strftime('%A')
        current_date_readable = user_time.strftime('%A, %B %d, %Y')
        current_time_readable = user_time.strftime('%I:%M %p')

        # Build system prompt
        system_prompt = f"""You are a helpful SMS memory assistant with reminder capabilities.

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
- If time includes AM/PM in ANY format (like "9pm", "9 pm", "9PM", "9 PM", "3:30am", "3:30 a.m.", "3:30AM", "3:30 A.M."): Process normally
- If time does NOT include AM/PM (like "9:00" or "4:35"): Ask for clarification - respond with action "clarify_time"
- Accept all variations: pm, PM, p.m., P.M., am, AM, a.m., A.M.

For reminder requests with RELATIVE TIMES:
- "in 30 minutes" = add 30 minutes to {current_datetime}
- "in 2 hours" = add 2 hours to {current_datetime}
- "tomorrow at 9am" = tomorrow's date at 09:00:00
- "Saturday at 8am" = next Saturday at 08:00:00

For reminder requests with DAYS OF THE WEEK:
- Use "Today is: {current_day_of_week}" to calculate
- "Saturday" = the next Saturday from today
- "this Saturday" = this week's Saturday
- "next Monday" = Monday of next week

Examples:
- "Remind me at 9pm to take meds" → Process normally (has PM)
- "Remind me at 4:22 pm to call wife" → Process normally (has pm in lowercase)
- "Remind me at 3:30 AM to wake up" → Process normally (has AM)
- "Remind me at 4:35 to call wife" → Ask "Do you mean 4:35 AM or 4:35 PM?"
- "Remind me in 30 minutes" → Add 30 minutes to {current_datetime}
- "Remind me tomorrow at 2pm" → Tomorrow's date at 14:00:00

RESPONSE FORMAT (must be valid JSON):

For STORING new information:
{{
    "action": "store",
    "item": "the item/object being stored",
    "details": "key details",
    "confirmation": "Brief, friendly confirmation message"
}}

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

For SETTING REMINDERS WITH CLEAR TIME:
{{
    "action": "reminder",
    "reminder_text": "what to remind them about",
    "reminder_date": "YYYY-MM-DD HH:MM:SS format (this will be in {user_tz} timezone)",
    "confirmation": "I'll remind you on [readable date/time including day of week] to [action]"
}}

For ASKING TIME CLARIFICATION:
{{
    "action": "clarify_time",
    "reminder_text": "what to remind them about",
    "time_mentioned": "the ambiguous time they said (e.g., '4:35')",
    "response": "Got it! Do you mean [time] AM or PM?"
}}

For UNCLEAR requests or GREETINGS:
{{
    "action": "help",
    "response": "Friendly, helpful response"
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
    "item_text": "ALL items exactly as the user said them (e.g., 'milk, eggs, bread' or 'chips and salsa, mac and cheese')",
    "confirmation": "Added [item] to your [list name]"
}}
Note: ALWAYS use add_to_list when the user specifies a list name, even if that list doesn't exist yet. The system will auto-create it.
IMPORTANT: Keep ALL items in item_text exactly as the user said them. Do NOT extract just one item. If user says "add milk, eggs, bread", item_text should be "milk, eggs, bread".

For ADDING ITEM BUT NO LIST SPECIFIED (user has lists but didn't say which):
{{
    "action": "add_item_ask_list",
    "item_text": "ALL items exactly as the user said them",
    "response": "Which list would you like to add these to?"
}}
Note: Only use add_item_ask_list if user has multiple lists and didn't specify which one. If user specifies a list name like "grocery list", use add_to_list instead.
IMPORTANT: Keep ALL items in item_text exactly as the user said them. Do NOT extract just one item.

For SHOWING A SPECIFIC LIST (user specifies which list):
{{
    "action": "show_list",
    "list_name": "the name of the list",
    "response": "Format the list contents from USER'S LISTS above"
}}

For SHOWING THE CURRENT/LAST ACTIVE LIST:
{{
    "action": "show_current_list",
    "response": "Showing your current list"
}}
IMPORTANT: Use show_current_list when user says "show list" (SINGULAR), "show my list", "what's on my list", "view list", or just "list". This shows their last active list.

For SHOWING ALL LISTS:
{{
    "action": "show_all_lists",
    "response": "List all the user's lists with item counts"
}}
Note: ONLY use show_all_lists when user explicitly says "lists" (PLURAL), "all lists", "all my lists", or "show all lists".

CRITICAL: "show list" = show_current_list, "show lists" = show_all_lists

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
    "list_name": "the list to delete",
    "confirmation": "Are you sure you want to delete your [list name]? Reply YES to confirm."
}}

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

CRITICAL RULES:
- All times are in user's timezone: {user_tz}
- Check for AM/PM in a case-insensitive way: "pm", "PM", "p.m.", "P.M.", "am", "AM", "a.m.", "A.M." are ALL valid
- If you see ANY variation of AM/PM in the user's message, use action "reminder" NOT "clarify_time"
- If a time does NOT have ANY form of AM/PM specified, you MUST use action "clarify_time" instead of setting the reminder
- When answering "when did I..." questions, use the "(recorded on DATE)" timestamp from the memories above
- Never say "today" when referring to a date that shows "(recorded on [past date])" - use the actual recorded date
- When retrieving information, ONLY use the memories listed above
- Always include the day of the week in reminder confirmations (e.g., "Saturday, December 21st at 8:00 AM")"""

        # Call OpenAI API with timeout
        client = OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ],
            temperature=OPENAI_TEMPERATURE,
            max_tokens=OPENAI_MAX_TOKENS
        )

        result = json.loads(response.choices[0].message.content)
        logger.info(f"✅ AI processed successfully: {result.get('action')}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"JSON Parse Error: {e}")
        logger.error(f"OpenAI Response: {response.choices[0].message.content}")
        return {
            "action": "error",
            "response": "Sorry, I had trouble processing that. Could you try again?"
        }
    except Exception as e:
        import traceback
        logger.error(f"OpenAI Error: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return {
            "action": "error",
            "response": "Sorry, I had trouble understanding that. Could you rephrase?"
        }


def parse_list_items(item_text):
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
