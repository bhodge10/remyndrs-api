"""
Onboarding Service
Handles new user onboarding flow
"""

import re
import pytz
from datetime import datetime, timedelta
from fastapi.responses import Response
from twilio.twiml.messaging_response import MessagingResponse

from config import logger, FREE_TRIAL_DAYS, TIER_PREMIUM, API_BASE_URL, PREMIUM_MONTHLY_PRICE
from models.user import get_user, get_onboarding_step, create_or_update_user
from models.memory import save_memory
from utils.timezone import get_timezone_from_zip, get_user_current_time
from utils.formatting import get_onboarding_prompt
from services.sms_service import send_sms
from tasks.reminder_tasks import send_delayed_sms, send_engagement_nudge
from services.onboarding_recovery_service import (
    track_onboarding_progress,
    mark_onboarding_complete,
    mark_onboarding_cancelled,
    get_onboarding_progress,
)


def validate_email(email):
    """Validate email format and return (is_valid, error_type)"""
    if ' ' in email:
        return False, "spaces"
    if '@' not in email:
        return False, "no_at"
    parts = email.split('@')
    if len(parts) != 2 or '.' not in parts[1]:
        return False, "no_domain"
    return True, None


def validate_zip_code(zip_input):
    """Validate ZIP code and return (cleaned_zip, error_type)"""
    zip_code = zip_input.strip().upper()

    # Handle ZIP+4 format (12345-6789) - extract first 5 digits
    if '-' in zip_code and zip_code.split('-')[0].isdigit():
        zip_code = zip_code.split('-')[0]

    # Check for international postal codes
    # Canadian postal codes: A1A 1A1 format
    canadian_pattern = re.match(r'^[A-Z]\d[A-Z]\s?\d[A-Z]\d$', zip_code)
    # UK postal codes: various formats like SW1A 1AA
    uk_pattern = re.match(r'^[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}$', zip_code)

    if canadian_pattern or uk_pattern:
        return None, "international"

    # Check for letters (likely a city name or invalid format)
    if any(c.isalpha() for c in zip_code):
        return None, "city_name"

    # Remove any non-digit characters
    zip_code = ''.join(c for c in zip_code if c.isdigit())

    # Check length
    if len(zip_code) != 5:
        return None, "wrong_length"

    return zip_code, None


def get_zip_error_message(error_type, original_input):
    """Return appropriate error message for ZIP validation failure"""
    if error_type == "international":
        return """I recognize that's an international postal code!

Currently, Remyndrs only supports US ZIP codes for timezone detection.

If you're outside the US, you can enter a US ZIP code that matches your timezone:
- Eastern Time: 10001 (New York)
- Central Time: 60601 (Chicago)
- Mountain Time: 80202 (Denver)
- Pacific Time: 90001 (Los Angeles)

What ZIP code should I use?"""
    elif error_type == "city_name":
        return """Hmm, that looks like a city name or address!

I just need the 5-digit ZIP code (like 45202).

What's your ZIP code?"""
    elif error_type == "wrong_length":
        digit_count = len(''.join(c for c in original_input if c.isdigit()))
        if digit_count > 0:
            return f"""ZIP codes are exactly 5 digits!

You entered {digit_count} digit{'s' if digit_count != 1 else ''}. Try again?

What's your 5-digit ZIP code?"""
        else:
            return """Please enter a valid 5-digit ZIP code (like 45202):"""
    return """Please enter a valid 5-digit ZIP code:"""


def handle_onboarding(phone_number, message):
    """Handle onboarding flow for new users"""
    try:
        step = get_onboarding_step(phone_number)
        resp = MessagingResponse()

        message_lower = message.lower().strip()
        message_stripped = message.strip()

        # Expanded service keywords
        service_keywords = ['remind', 'list', 'delete', 'what', 'when', 'where', 'how', 'my',
                           'add', 'show', 'create', 'set', 'save', 'remember']

        # Handle help request during onboarding
        if message_lower in ['help', '?'] and step > 0:
            resp.message(f"""I'm helping you set up your account! It's quick - just 2 questions total.

You're currently on step {step} of 2:
{get_onboarding_prompt(step)}

Why I need this info:
• Name: Personalize your experience
• ZIP: Set your timezone for accurate reminders

Text "cancel" to cancel setup, or just answer the question to continue!""")
            return Response(content=str(resp), media_type="application/xml")

        # Handle pricing questions during onboarding
        pricing_keywords = ['cost', 'price', 'pricing', 'how much', 'free', 'paid', 'subscription']
        if step > 0 and any(keyword in message_lower for keyword in pricing_keywords):
            logger.info(f"Pricing question during onboarding from ...{phone_number[-4:]}")
            current_prompt = get_onboarding_prompt(step)
            resp.message(f"""Great question! You get a FREE {FREE_TRIAL_DAYS}-day Premium trial to start. After that, it's {PREMIUM_MONTHLY_PRICE}/mo for Premium or a free tier with 2 reminders/day.

Let's finish setup first - {current_prompt}""")
            return Response(content=str(resp), media_type="application/xml")

        # Handle cancel request during onboarding
        if message_lower in ['cancel', 'nevermind', 'quit'] and step > 0:
            create_or_update_user(phone_number, onboarding_step=0)
            mark_onboarding_cancelled(phone_number)
            resp.message("""No problem! Setup cancelled.

If you change your mind, just text me again and we'll start fresh.

Have a great day! 👋""")
            return Response(content=str(resp), media_type="application/xml")

        # Handle restart request during onboarding
        if message_lower == 'restart' and step > 0:
            progress = get_onboarding_progress(phone_number)
            first_name = progress.get('first_name') if progress else None
            create_or_update_user(phone_number, onboarding_step=1)
            track_onboarding_progress(phone_number, 1)

            if first_name:
                resp.message(f"""No problem, {first_name}! Let's start over.

What's your first name?""")
            else:
                resp.message("""No problem! Let's start over.

What's your first name?""")
            return Response(content=str(resp), media_type="application/xml")

        # Handle skip requests during ZIP step
        if message_lower in ['skip', 'pass', "i don't want to", "dont want to"]:
            if step == 2:
                resp.message("""I totally get it! But here's why I need it:

Without your ZIP code, I can't figure out your timezone. That means reminders might arrive at the wrong time (imagine getting a 2pm reminder at 5am 😬).

Your 5-digit ZIP code helps me send reminders when YOU need them.

What's your ZIP code?""")
                return Response(content=str(resp), media_type="application/xml")

        # Check if user is trying to use the service before completing onboarding
        if any(keyword in message_lower for keyword in service_keywords) and step > 0:
            remaining = 2 - step + 1
            question_word = "question" if remaining == 1 else "questions"
            resp.message(f"""⚠️ Almost there! Please finish setup first.

You're on step {step} of 2 - just {remaining} more {question_word}!

{get_onboarding_prompt(step)}""")
            return Response(content=str(resp), media_type="application/xml")

        if step == 0:
            # Welcome message - ask for first name
            create_or_update_user(phone_number, onboarding_step=1)
            track_onboarding_progress(phone_number, 1)

            # Enhanced welcome message with clearer value proposition
            resp.message("""Welcome to Remyndrs! 👋

I'm your AI-powered reminder assistant. I'll help you remember anything—from daily tasks to important dates.

No app needed - just text me naturally and I'll handle the rest!

Let's get you set up in 30 seconds. What's your first name?""")

        elif step == 1:
            # Check if user sent START again (maybe trying to restart)
            if message_lower in ['start', 'unstop', 'begin']:
                resp.message("""You're already in setup! Let's continue.

What's your first name?""")
                return Response(content=str(resp), media_type="application/xml")

            # Check if user accidentally entered an email address
            if '@' in message_stripped and '.' in message_stripped:
                resp.message("""That looks like an email! What's your first name?""")
                return Response(content=str(resp), media_type="application/xml")

            # Check for full name (two words)
            words = message_stripped.split()
            if len(words) == 2 and all(word.isalpha() for word in words):
                # User provided full name - skip to ZIP
                first_name, last_name = words[0].title(), words[1].title()
                create_or_update_user(phone_number, first_name=first_name, last_name=last_name, onboarding_step=2)
                track_onboarding_progress(phone_number, 2, first_name=first_name, last_name=last_name)
                resp.message(f"""Nice to meet you, {first_name} {last_name}!

Last question: ZIP code?

(This helps me send reminders at the right time in your timezone)""")
            else:
                # Store first name, ask for ZIP code
                first_name = message_stripped.title()
                create_or_update_user(phone_number, first_name=first_name, onboarding_step=2)
                track_onboarding_progress(phone_number, 2, first_name=first_name)
                resp.message(f"""Nice to meet you, {first_name}!

Last question: ZIP code?

(This helps me send reminders at the right time in your timezone)""")

        elif step == 2:
            # Validate and store zip code, calculate timezone, complete onboarding
            zip_code, error_type = validate_zip_code(message_stripped)

            if error_type:
                resp.message(get_zip_error_message(error_type, message_stripped))
                return Response(content=str(resp), media_type="application/xml")

            # Get timezone from zip code
            timezone = get_timezone_from_zip(zip_code)

            # Calculate trial end date
            trial_end_date = datetime.utcnow() + timedelta(days=FREE_TRIAL_DAYS)

            # Save zip, timezone, trial info, and mark onboarding complete
            create_or_update_user(
                phone_number,
                zip_code=zip_code,
                timezone=timezone,
                onboarding_complete=True,
                onboarding_step=3,
                premium_status=TIER_PREMIUM,
                trial_end_date=trial_end_date
            )

            # Remove from abandoned onboarding tracking
            mark_onboarding_complete(phone_number)

            # SMART NUDGES: Auto-enable during trial for engagement
            # Uncomment post-launch when ready to activate for new trial users:
            # create_or_update_user(phone_number, smart_nudges_enabled=True)

            # Get user's name for personalized message
            user = get_user(phone_number)
            first_name = user[1]

            # Save first memory: signup date
            signup_date = datetime.utcnow().strftime("%B %d, %Y")
            first_memory = f"Signed up for Remyndrs on {signup_date}"
            save_memory(phone_number, first_memory, {"type": "signup", "auto_created": True})

            # Format trial end date in user's timezone
            user_tz = pytz.timezone(timezone)
            trial_end_local = trial_end_date.replace(tzinfo=pytz.UTC).astimezone(user_tz)
            trial_end_str = trial_end_local.strftime('%B %d')

            # Send completion message - focused on immediate value + trial awareness
            resp.message(f"""Perfect! You're all set, {first_name}! 🎉

You have full Premium access until {trial_end_str} — unlimited reminders, lists & memories.

I just saved your first memory: "{first_memory}"

Try asking me: "What do I have saved?" """)

            # Send VCF contact card after 1-hour delay — acts as a warm touchback
            vcf_url = f"{API_BASE_URL}/contact.vcf"
            vcf_message = """📱 Tap to save Remyndrs to your contacts!

Tip: Pin this conversation to keep me at the top of your texts — that way I'm always one tap away when you need to remember something!"""
            try:
                send_delayed_sms.apply_async(
                    args=[phone_number, vcf_message],
                    kwargs={"media_url": vcf_url},
                    countdown=3600  # 1 hour
                )
            except Exception as celery_error:
                # Celery not available - fall back to immediate send
                logger.info(f"Celery unavailable, sending VCF immediately: {celery_error}")
                try:
                    send_sms(phone_number, vcf_message, media_url=vcf_url)
                except Exception as sms_error:
                    logger.warning(f"Could not send VCF card for {phone_number}: {sms_error}")

            # Schedule 5-minute engagement nudge (only if user doesn't text back)
            try:
                nudge_scheduled_at = datetime.utcnow()
                create_or_update_user(
                    phone_number,
                    five_minute_nudge_scheduled_at=nudge_scheduled_at,
                    five_minute_nudge_sent=False,
                    post_onboarding_interactions=0
                )
                send_engagement_nudge.apply_async(
                    args=[phone_number],
                    countdown=300  # 5 minutes
                )
                logger.info(f"Scheduled 5-minute engagement nudge for ...{phone_number[-4:]}")
            except Exception as nudge_error:
                # Non-critical - log and continue
                logger.warning(f"Could not schedule engagement nudge for {phone_number}: {nudge_error}")

        return Response(content=str(resp), media_type="application/xml")

    except Exception as e:
        logger.error(f"❌ Error in onboarding for {phone_number}: {e}")
        resp = MessagingResponse()
        resp.message("Sorry, something went wrong. Please try again.")
        return Response(content=str(resp), media_type="application/xml")
