"""
Smart Nudge Service
Proactive AI intelligence layer that sends daily contextual insights
by analyzing user data (memories, reminders, lists, interaction patterns).
"""

import json
from datetime import datetime, timedelta
from typing import Any, Optional

import pytz
from openai import OpenAI

from config import (
    OPENAI_API_KEY, OPENAI_MODEL, logger,
    NUDGE_MAX_TOKENS, NUDGE_TEMPERATURE, NUDGE_CONFIDENCE_THRESHOLD, NUDGE_MAX_CHARS,
    TIER_FREE, TIER_PREMIUM,
)
from database import get_db_connection, return_db_connection, log_api_usage
from models.user import create_or_update_user, get_user_first_name


def gather_user_data(phone_number: str, timezone_str: str) -> dict[str, Any]:
    """Gather all user data needed for nudge generation.

    Returns dict with memories, reminders, lists, and interaction patterns.
    """
    from models.memory import get_memories
    from models.reminder import get_pending_reminders, get_user_reminders
    from models.list_model import get_lists, get_list_items

    user_tz = pytz.timezone(timezone_str)
    utc_now = datetime.now(pytz.UTC)
    user_now = utc_now.astimezone(user_tz)

    data = {
        'current_date': user_now.strftime('%Y-%m-%d'),
        'current_day': user_now.strftime('%A'),
        'memories': [],
        'upcoming_reminders': [],
        'recently_completed_reminders': [],
        'lists': [],
        'recent_nudges': [],
    }

    # Gather memories
    memories = get_memories(phone_number)
    for mem_id, text, parsed_data, created_at in memories:
        data['memories'].append({
            'text': text,
            'created_at': created_at.strftime('%Y-%m-%d') if created_at else None,
        })

    # Gather reminders (upcoming and recently completed)
    all_reminders = get_user_reminders(phone_number)
    for rem_id, reminder_date, text, recurring_id, sent in all_reminders:
        if reminder_date:
            if reminder_date.tzinfo is None:
                reminder_date = pytz.UTC.localize(reminder_date)
            local_dt = reminder_date.astimezone(user_tz)

            if not sent and reminder_date > utc_now:
                # Upcoming reminder
                data['upcoming_reminders'].append({
                    'id': rem_id,
                    'text': text,
                    'date': local_dt.strftime('%Y-%m-%d %I:%M %p'),
                    'is_recurring': recurring_id is not None,
                })
            elif sent and (utc_now - reminder_date) < timedelta(days=3):
                # Recently completed (last 3 days)
                data['recently_completed_reminders'].append({
                    'id': rem_id,
                    'text': text,
                    'date': local_dt.strftime('%Y-%m-%d %I:%M %p'),
                })

    # Gather lists with items
    lists = get_lists(phone_number)
    for list_id, list_name, item_count, completed_count in lists:
        items = get_list_items(list_id)
        list_data = {
            'name': list_name,
            'total_items': item_count,
            'completed_items': completed_count or 0,
            'items': [],
        }
        for item_id, item_text, completed in items:
            list_data['items'].append({
                'text': item_text,
                'completed': completed,
            })
        data['lists'].append(list_data)

    # Gather recently sent nudges (last 14 days for repetition prevention)
    data['recent_nudges'] = get_recent_nudges(phone_number, days=14)

    return data


def get_recent_nudges(phone_number: str, days: int = 14) -> list[dict[str, Any]]:
    """Get recently sent nudges for repetition prevention."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            SELECT nudge_type, nudge_text, sent_at
            FROM smart_nudges
            WHERE phone_number = %s AND sent_at > NOW() - INTERVAL '%s days'
            ORDER BY sent_at DESC
            LIMIT 20
        ''', (phone_number, days))
        results = c.fetchall()
        return [
            {
                'type': row[0],
                'text': row[1],
                'sent_at': row[2].strftime('%Y-%m-%d') if row[2] else None,
            }
            for row in results
        ]
    except Exception as e:
        logger.error(f"Error getting recent nudges: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def build_nudge_prompt(user_data: dict, first_name: str, premium_status: str) -> str:
    """Build the AI prompt for nudge generation."""
    # Determine available nudge types based on tier
    is_free = premium_status == TIER_FREE
    if is_free:
        nudge_types = "weekly_reflection"
        type_instruction = "You MUST generate a weekly_reflection nudge type. This is the only type available for free tier users."
    else:
        nudge_types = "date_extraction, reminder_followup, cross_reference, stale_list, pattern_recognition, weekly_reflection, memory_anniversary, upcoming_preparation"
        type_instruction = "Pick the single most relevant and useful nudge type from the list above."

    recent_nudge_text = ""
    if user_data['recent_nudges']:
        recent_nudge_text = "\n\nRecently sent nudges (DO NOT repeat similar content):\n"
        for nudge in user_data['recent_nudges'][:10]:
            recent_nudge_text += f"- [{nudge['sent_at']}] {nudge['type']}: {nudge['text']}\n"

    prompt = f"""You are Remyndrs, an SMS-based AI assistant. Generate ONE proactive nudge for the user based on their data.

User name: {first_name or 'User'}
Current date: {user_data['current_date']} ({user_data['current_day']})

Available nudge types: {nudge_types}
{type_instruction}

Nudge type descriptions:
- date_extraction: Find dates in memories and suggest setting reminders (e.g., birthdays, appointments)
- reminder_followup: Ask about recently sent reminders (e.g., "Did you call the dentist?")
- cross_reference: Connect upcoming reminders with related memories
- stale_list: Point out lists with unchecked items that haven't been touched recently
- pattern_recognition: Identify patterns in reminder creation and suggest recurring reminders
- weekly_reflection: Summarize the week's activity (reminders completed, lists updated, memories saved)
- memory_anniversary: Note anniversaries of saved memories (1 month, 6 months, 1 year)
- upcoming_preparation: Combine upcoming reminders with related memories for preparation

USER DATA:

Memories ({len(user_data['memories'])} total):
"""
    for mem in user_data['memories'][:15]:
        prompt += f"- [{mem['created_at']}] {mem['text']}\n"

    prompt += f"\nUpcoming reminders ({len(user_data['upcoming_reminders'])} total):\n"
    for rem in user_data['upcoming_reminders'][:10]:
        prompt += f"- [ID:{rem['id']}] {rem['date']} - {rem['text']}"
        if rem['is_recurring']:
            prompt += " (recurring)"
        prompt += "\n"

    prompt += f"\nRecently completed reminders ({len(user_data['recently_completed_reminders'])} total):\n"
    for rem in user_data['recently_completed_reminders'][:10]:
        prompt += f"- [ID:{rem['id']}] {rem['date']} - {rem['text']}\n"

    prompt += f"\nLists ({len(user_data['lists'])} total):\n"
    for lst in user_data['lists'][:10]:
        unchecked = lst['total_items'] - lst['completed_items']
        prompt += f"- {lst['name']}: {lst['total_items']} items ({unchecked} unchecked)\n"
        for item in lst['items'][:5]:
            status = "done" if item['completed'] else "todo"
            prompt += f"  [{status}] {item['text']}\n"

    prompt += recent_nudge_text

    prompt += f"""

RULES:
1. Return ONLY valid JSON. No extra text.
2. nudge_text MUST be under {NUDGE_MAX_CHARS} characters (this is critical for SMS)
3. Be genuinely helpful, not annoying. No nudge is better than a bad nudge.
4. Use a warm, brief, conversational tone. Address user by name if known.
5. If suggesting an action, tell user what to reply (e.g., "Reply YES to set this reminder")
6. If there's nothing genuinely useful to say, return nudge_type: "none"
7. DO NOT repeat content from recently sent nudges
8. For weekly_reflection, summarize actual numbers from the data
9. DO NOT list or summarize upcoming reminders in the nudge text — they are shown separately above your message.

Return JSON:
{{
  "nudge_type": "one_of_the_types_above_or_none",
  "nudge_text": "the message to send to the user",
  "confidence": 0-100,
  "suggested_reminder_text": "optional - reminder text if suggesting a new reminder",
  "related_reminder_id": null_or_int
}}"""

    return prompt


def generate_nudge(phone_number: str, timezone_str: str, first_name: str, premium_status: str) -> Optional[dict[str, Any]]:
    """Generate a smart nudge for a user using AI.

    Returns:
        dict with nudge data if generated, None if skipped
    """
    try:
        # Gather all user data
        user_data = gather_user_data(phone_number, timezone_str)

        # Check if user has enough data for a meaningful nudge
        total_data = (
            len(user_data['memories']) +
            len(user_data['upcoming_reminders']) +
            len(user_data['recently_completed_reminders']) +
            len(user_data['lists'])
        )
        if total_data == 0:
            logger.info(f"No data for nudge generation for {phone_number[-4:]}")
            return None

        # Build prompt
        prompt = build_nudge_prompt(user_data, first_name, premium_status)

        # Call OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are a helpful SMS assistant that generates proactive nudges. Return only valid JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=NUDGE_TEMPERATURE,
            max_tokens=NUDGE_MAX_TOKENS,
            response_format={"type": "json_object"},
        )

        # Log API usage
        usage = response.usage
        if usage:
            log_api_usage(
                phone_number, 'smart_nudge',
                usage.prompt_tokens, usage.completion_tokens,
                usage.total_tokens, OPENAI_MODEL
            )

        # Parse response
        raw_response = response.choices[0].message.content
        try:
            nudge_data = json.loads(raw_response)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse nudge JSON for {phone_number[-4:]}: {raw_response[:200]}")
            return None

        # Validate response
        nudge_type = nudge_data.get('nudge_type', 'none')
        nudge_text = nudge_data.get('nudge_text', '')
        confidence = nudge_data.get('confidence', 0)

        # Skip if AI decided no nudge or low confidence
        if nudge_type == 'none' or confidence < NUDGE_CONFIDENCE_THRESHOLD:
            logger.info(f"Nudge skipped for {phone_number[-4:]}: type={nudge_type}, confidence={confidence}")
            return None

        # Enforce character limit
        if len(nudge_text) > NUDGE_MAX_CHARS:
            nudge_text = nudge_text[:NUDGE_MAX_CHARS - 3] + "..."

        return {
            'nudge_type': nudge_type,
            'nudge_text': nudge_text,
            'confidence': confidence,
            'suggested_reminder_text': nudge_data.get('suggested_reminder_text'),
            'related_reminder_id': nudge_data.get('related_reminder_id'),
            'raw_response': raw_response,
        }

    except Exception as e:
        logger.error(f"Error generating nudge for {phone_number[-4:]}: {e}")
        return None


def save_nudge(phone_number: str, nudge_data: dict[str, Any]) -> Optional[int]:
    """Save a nudge to the smart_nudges table.

    Returns the nudge ID.
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            INSERT INTO smart_nudges (phone_number, nudge_type, nudge_text, ai_raw_response, metadata)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        ''', (
            phone_number,
            nudge_data['nudge_type'],
            nudge_data['nudge_text'],
            nudge_data.get('raw_response'),
            json.dumps({
                'confidence': nudge_data.get('confidence'),
                'suggested_reminder_text': nudge_data.get('suggested_reminder_text'),
                'related_reminder_id': nudge_data.get('related_reminder_id'),
            })
        ))
        result = c.fetchone()
        conn.commit()
        nudge_id = result[0] if result else None
        logger.info(f"Saved nudge {nudge_id} for {phone_number[-4:]}: {nudge_data['nudge_type']}")
        return nudge_id
    except Exception as e:
        logger.error(f"Error saving nudge for {phone_number[-4:]}: {e}")
        if conn:
            conn.rollback()
        return None
    finally:
        if conn:
            return_db_connection(conn)


def set_pending_nudge_response(phone_number: str, nudge_id: int, nudge_data: dict[str, Any]) -> None:
    """Set pending nudge response data on the user record."""
    pending_data = json.dumps({
        'nudge_id': nudge_id,
        'nudge_type': nudge_data['nudge_type'],
        'suggested_reminder_text': nudge_data.get('suggested_reminder_text'),
        'related_reminder_id': nudge_data.get('related_reminder_id'),
    })
    create_or_update_user(phone_number, pending_nudge_response=pending_data)


def send_nudge_to_user(phone_number: str, nudge_data: dict[str, Any]) -> bool:
    """Send a nudge SMS to the user, save it, and set pending response.

    Returns True if sent successfully.
    """
    from services.sms_service import send_sms

    try:
        # Save to database
        nudge_id = save_nudge(phone_number, nudge_data)
        if not nudge_id:
            return False

        # Send SMS
        send_sms(phone_number, nudge_data['nudge_text'], message_type="smart_nudge")

        # Set pending response if the nudge expects a reply
        actionable_types = {'date_extraction', 'reminder_followup', 'stale_list', 'pattern_recognition'}
        if nudge_data['nudge_type'] in actionable_types:
            set_pending_nudge_response(phone_number, nudge_id, nudge_data)

        logger.info(f"Sent nudge to {phone_number[-4:]}: {nudge_data['nudge_type']}")
        return True

    except Exception as e:
        logger.error(f"Error sending nudge to {phone_number[-4:]}: {e}")
        return False


FOUNDER_SURVEY_QUESTION = (
    "Hi {name}! Quick favor from the Remyndrs team: what's the one thing you "
    "use Remyndrs for most, and what would make it a must-have for you? "
    "Your reply comes straight to us. Thanks so much!"
)


def send_founder_survey(phone_number: str, first_name: str = None) -> Optional[int]:
    """Send the one-off founder survey question and arm free-text capture.

    Stores a smart_nudges row (nudge_type='founder_survey') and sets the pending
    nudge response so the user's NEXT reply is captured verbatim by
    handle_nudge_response (rather than being processed as a memory/reminder).

    Returns the nudge id, or None on failure.
    """
    from services.sms_service import send_sms

    greeting_name = (first_name or "").strip() or "there"
    message = FOUNDER_SURVEY_QUESTION.format(name=greeting_name)

    nudge_data = {'nudge_type': 'founder_survey', 'nudge_text': message}
    try:
        nudge_id = save_nudge(phone_number, nudge_data)
        if not nudge_id:
            return None
        send_sms(phone_number, message, message_type="smart_nudge")
        set_pending_nudge_response(phone_number, nudge_id, nudge_data)
        logger.info(f"Sent founder survey to {phone_number[-4:]} (nudge {nudge_id})")
        return nudge_id
    except Exception as e:
        logger.error(f"Error sending founder survey to {phone_number[-4:]}: {e}")
        return None


def is_nudge_eligible(premium_status: str, current_day: str) -> bool:
    """Check if user is eligible for a nudge based on tier.

    Premium/Trial: daily nudges (all types)
    Free: weekly reflection only (Sundays)
    """
    if premium_status == TIER_FREE:
        return current_day == 'Sunday'
    return True


def record_nudge_response(phone_number: str, nudge_id: int, response: str, action_taken: str = None, created_reminder_id: int = None) -> bool:
    """Record a user's response to a nudge."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            UPDATE smart_nudges
            SET user_response = %s, user_responded_at = CURRENT_TIMESTAMP,
                action_taken = %s, created_reminder_id = %s
            WHERE id = %s
        ''', (response, action_taken, created_reminder_id, nudge_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error recording nudge response: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)


def handle_nudge_response(phone_number: str, message: str, pending_nudge: dict[str, Any]) -> Optional[str]:
    """Handle a user's response to a nudge.

    Args:
        phone_number: User's phone number
        message: User's message (uppercased)
        pending_nudge: The pending nudge response data

    Returns:
        Response message string, or None if message is not a nudge response
    """
    nudge_id = pending_nudge.get('nudge_id')
    nudge_type = pending_nudge.get('nudge_type')
    suggested_reminder = pending_nudge.get('suggested_reminder_text')
    related_reminder_id = pending_nudge.get('related_reminder_id')

    # Founder survey: capture the entire free-text reply verbatim. This is an
    # open-ended question, not a YES/NO keyword flow, so it must run before any
    # keyword handling below.
    if nudge_type == 'founder_survey':
        create_or_update_user(phone_number, pending_nudge_response=None)
        record_nudge_response(phone_number, nudge_id, message.strip(), 'survey_response')
        return "Thank you — this genuinely helps us make Remyndrs better for you!"

    msg = message.strip().upper()

    # STOP disables nudges entirely
    if msg == 'STOP':
        create_or_update_user(phone_number, smart_nudges_enabled=False, pending_nudge_response=None)
        record_nudge_response(phone_number, nudge_id, 'STOP', 'disabled_nudges')
        return "Smart nudges disabled. Text NUDGE ON anytime to re-enable."

    # NO / NOPE - dismiss the nudge
    if msg in ('NO', 'NOPE', 'NAH', 'NO THANKS'):
        create_or_update_user(phone_number, pending_nudge_response=None)
        record_nudge_response(phone_number, nudge_id, msg, 'dismissed')
        return "Got it!"

    # YES - create a suggested reminder or confirm action
    if msg == 'YES':
        create_or_update_user(phone_number, pending_nudge_response=None)

        if nudge_type == 'date_extraction' and suggested_reminder:
            # Create the suggested reminder
            reminder_id = _create_reminder_from_nudge(phone_number, suggested_reminder)
            if reminder_id:
                record_nudge_response(phone_number, nudge_id, 'YES', 'created_reminder', reminder_id)
                return f"Done! Reminder set: \"{suggested_reminder}\""
            else:
                record_nudge_response(phone_number, nudge_id, 'YES', 'reminder_creation_failed')
                return "Sorry, I couldn't create that reminder. Try texting me the reminder directly."

        elif nudge_type == 'pattern_recognition' and suggested_reminder:
            record_nudge_response(phone_number, nudge_id, 'YES', 'pattern_acknowledged')
            return "To set up a recurring reminder, text me something like: \"Remind me every Monday at 10am about team meeting\""

        else:
            record_nudge_response(phone_number, nudge_id, 'YES', 'acknowledged')
            return "Noted!"

    # DONE - mark a follow-up as completed
    if msg == 'DONE':
        create_or_update_user(phone_number, pending_nudge_response=None)
        record_nudge_response(phone_number, nudge_id, 'DONE', 'completed')
        return "Nice work!"

    # SNOOZE - snooze the follow-up
    snooze_match = msg.startswith('SNOOZE')
    if snooze_match:
        create_or_update_user(phone_number, pending_nudge_response=None)
        record_nudge_response(phone_number, nudge_id, msg, 'snoozed')
        return "Got it, I'll check back later."

    # SHOW - show a stale list
    if msg == 'SHOW' and nudge_type == 'stale_list':
        create_or_update_user(phone_number, pending_nudge_response=None)
        record_nudge_response(phone_number, nudge_id, 'SHOW', 'showed_list')
        # Return None to let the message fall through to normal processing
        # where "show [list_name]" would be handled
        return None

    # Auto-clear: If message is longer than 3 chars and not a recognized nudge response,
    # clear the pending state and let it fall through to normal processing
    if len(message.strip()) > 3:
        create_or_update_user(phone_number, pending_nudge_response=None)
        return None

    return None


def _create_reminder_from_nudge(phone_number: str, reminder_text: str) -> Optional[int]:
    """Create a reminder from a nudge suggestion.

    Uses a simple approach: creates a reminder for tomorrow at 9 AM user local time.
    """
    from models.user import get_user_timezone
    from models.reminder import save_reminder_with_local_time

    try:
        timezone_str = get_user_timezone(phone_number)
        user_tz = pytz.timezone(timezone_str)
        user_now = datetime.now(pytz.UTC).astimezone(user_tz)

        # Set for tomorrow at 9 AM local time
        tomorrow = user_now + timedelta(days=1)
        reminder_time = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
        utc_time = reminder_time.astimezone(pytz.UTC)

        reminder_id = save_reminder_with_local_time(
            phone_number=phone_number,
            reminder_text=reminder_text,
            reminder_date=utc_time.strftime('%Y-%m-%d %H:%M:%S'),
            local_time='09:00',
            timezone=timezone_str,
        )
        return reminder_id
    except Exception as e:
        logger.error(f"Error creating reminder from nudge: {e}")
        return None
