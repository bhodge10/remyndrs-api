"""
AI Service
Handles all OpenAI API interactions and natural language processing
"""

import json
import openai
from datetime import datetime, timedelta
import pytz

from config import OPENAI_API_KEY, OPENAI_MODEL, OPENAI_TEMPERATURE, OPENAI_MAX_TOKENS, logger, MAX_MEMORIES_IN_CONTEXT, MAX_COMPLETED_REMINDERS_DISPLAY
from models.memory import get_memories
from models.reminder import get_user_reminders
from models.user import get_user_timezone
from utils.timezone import get_user_current_time

# Initialize OpenAI
openai.api_key = OPENAI_API_KEY

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
                        completed.append(f"  • {reminder_text}\n    {date_str}")
                    else:
                        scheduled.append(f"  • {reminder_text}\n    {date_str}")
                except:
                    if sent:
                        completed.append(f"  • {reminder_text}")
                    else:
                        scheduled.append(f"  • {reminder_text}")
            
            # Build context - limit completed to last 5
            parts = []
            if scheduled:
                parts.append("⏰ SCHEDULED:\n" + "\n\n".join(scheduled))
            if completed:
                # Show only last N completed reminders
                completed_to_show = completed[-MAX_COMPLETED_REMINDERS_DISPLAY:]
                completed_text = "\n\n".join(completed_to_show)
                if len(completed) > MAX_COMPLETED_REMINDERS_DISPLAY:
                    parts.append(f"✅ COMPLETED (last {MAX_COMPLETED_REMINDERS_DISPLAY} of {len(completed)}):\n" + completed_text)
                else:
                    parts.append("✅ COMPLETED:\n" + completed_text)
            
            reminders_context = "\n\n".join(parts) if parts else "No reminders set."
        else:
            reminders_context = "No reminders set."

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

IMPORTANT: Each memory shows when it was recorded. Use these dates when answering questions about "when did I..."

CAPABILITIES:
1. STORE new information from the user
2. RETRIEVE information from the stored memories above
3. SET REMINDERS for future tasks
4. LIST REMINDERS when asked
5. PROVIDE HELP when asked

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

CRITICAL RULES:
- All times are in user's timezone: {user_tz}
- Check for AM/PM in a case-insensitive way: "pm", "PM", "p.m.", "P.M.", "am", "AM", "a.m.", "A.M." are ALL valid
- If you see ANY variation of AM/PM in the user's message, use action "reminder" NOT "clarify_time"
- If a time does NOT have ANY form of AM/PM specified, you MUST use action "clarify_time" instead of setting the reminder
- When answering "when did I..." questions, use the "(recorded on DATE)" timestamp from the memories above
- Never say "today" when referring to a date that shows "(recorded on [past date])" - use the actual recorded date
- When retrieving information, ONLY use the memories listed above
- Always include the day of the week in reminder confirmations (e.g., "Saturday, December 21st at 8:00 AM")"""

        # Call OpenAI API
        response = openai.chat.completions.create(
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
        logger.error(f"❌ OpenAI Error: {e}")
        return {
            "action": "error",
            "response": "Sorry, I had trouble understanding that. Could you rephrase?"
        }
