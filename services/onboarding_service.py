"""
Onboarding Service
Handles new user onboarding flow
"""

import re
from datetime import datetime, timedelta
from fastapi.responses import Response
from twilio.twiml.messaging_response import MessagingResponse

from config import logger, FREE_TRIAL_DAYS, TIER_PREMIUM, APP_BASE_URL
from models.user import get_user, get_onboarding_step, create_or_update_user
from models.memory import save_memory
from utils.timezone import get_timezone_from_zip, get_user_current_time
from utils.formatting import get_onboarding_prompt
from tasks.reminder_tasks import send_delayed_sms
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


def get_email_error_message(error_type):
    """Return appropriate error message for email validation failure"""
    messages = {
        "spaces": """Email addresses can't have spaces in them!

What's your email address?
(Should look like: name@email.com)""",
        "no_at": """Oops! That email is missing the @ symbol.

It should look like: yourname@gmail.com

What's your email address?""",
        "no_domain": """Almost! But that email needs a domain (like @gmail.com or @yahoo.com).

What's your email address?"""
    }
    return messages.get(error_type, "Please enter a valid email address:")


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
            resp.message(f"""I'm helping you set up your account! It's quick - just 4 questions total.

You're currently on step {step} of 4:
{get_onboarding_prompt(step)}

Why I need this info:
• Name: Personalize your experience
• Email: Account recovery & important updates only
• ZIP: Set your timezone for accurate reminders

Text "cancel" to cancel setup, or just answer the question to continue!""")
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

        # Handle skip requests during email/ZIP steps
        if message_lower in ['skip', 'pass', "i don't want to", "dont want to"]:
            if step == 3:
                resp.message("""I understand privacy concerns! But I need your email for account recovery - if you forget your info or get a new phone, this is how you get back in.

I promise we only use it for:
✅ Account recovery
✅ Critical service updates (like scheduled maintenance)
❌ No marketing emails
❌ No selling your data

What's your email address?""")
                return Response(content=str(resp), media_type="application/xml")
            elif step == 4:
                resp.message("""I totally get it! But here's why I need it:

Without your ZIP code, I can't figure out your timezone. That means reminders might arrive at the wrong time (imagine getting a 2pm reminder at 5am 😬).

Your 5-digit ZIP code helps me send reminders when YOU need them.

What's your ZIP code?""")
                return Response(content=str(resp), media_type="application/xml")

        # Check if user is trying to use the service before completing onboarding
        if any(keyword in message_lower for keyword in service_keywords) and step > 0:
            remaining = 4 - step + 1
            question_word = "question" if remaining == 1 else "questions"
            resp.message(f"""⚠️ Almost there! Please finish setup first.

You're on step {step} of 4 - just {remaining} more {question_word}!

{get_onboarding_prompt(step)}""")
            return Response(content=str(resp), media_type="application/xml")

        if step == 0:
            # Welcome message - ask for first name
            create_or_update_user(phone_number, onboarding_step=1)
            track_onboarding_progress(phone_number, 1)

            # Check if user texted START specifically
            if message_lower == 'start':
                resp.message("""Welcome to Remyndrs! 👋

Thanks for texting START!

I help you remember anything - from grocery lists to important reminders.

Just 4 quick questions to get started (takes about 1 minute), then you're all set!

What's your first name?""")
            else:
                resp.message("""Welcome to Remyndrs! 👋

I help you remember anything - from grocery lists to important reminders.

Just 4 quick questions to get started (takes about 1 minute), then you're all set!

What's your first name?""")

        elif step == 1:
            # Check if user sent START again (maybe trying to restart)
            if message_lower in ['start', 'unstop', 'begin']:
                resp.message("""You're already in setup! Let's continue.

What's your first name?""")
                return Response(content=str(resp), media_type="application/xml")

            # Check if user accidentally entered an email address
            if '@' in message_stripped and '.' in message_stripped:
                resp.message("""That looks like an email address! I'll ask for that in a moment.

What's your first name?""")
                return Response(content=str(resp), media_type="application/xml")

            # Check for full name (two words)
            words = message_stripped.split()
            if len(words) == 2 and all(word.isalpha() for word in words):
                # User provided full name - skip to email
                first_name, last_name = words[0].title(), words[1].title()
                create_or_update_user(phone_number, first_name=first_name, last_name=last_name, onboarding_step=3)
                track_onboarding_progress(phone_number, 3, first_name=first_name, last_name=last_name)
                resp.message(f"""Nice to meet you, {first_name} {last_name}!

Email for account recovery?

(We only use this for important updates - no spam!)""")
            else:
                # Store first name, ask for last name
                first_name = message_stripped.title()
                create_or_update_user(phone_number, first_name=first_name, onboarding_step=2)
                track_onboarding_progress(phone_number, 2, first_name=first_name)
                resp.message(f"Nice to meet you, {first_name}! Last name?")

        elif step == 2:
            # Store last name, ask for email
            last_name = message_stripped.title()
            create_or_update_user(phone_number, last_name=last_name, onboarding_step=3)
            track_onboarding_progress(phone_number, 3, last_name=last_name)
            resp.message("""Got it! Email for account recovery?

(We only use this for important updates - no spam!)""")

        elif step == 3:
            # Validate and store email, ask for zip code
            email = message_stripped
            is_valid, error_type = validate_email(email)

            if not is_valid:
                resp.message(get_email_error_message(error_type))
                return Response(content=str(resp), media_type="application/xml")

            create_or_update_user(phone_number, email=email, onboarding_step=4)
            track_onboarding_progress(phone_number, 4, email=email)
            resp.message("""Perfect! Last question: ZIP code?

(This helps me send reminders at the right time in your timezone)""")

        elif step == 4:
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
                onboarding_step=5,
                premium_status=TIER_PREMIUM,
                trial_end_date=trial_end_date
            )

            # Remove from abandoned onboarding tracking
            mark_onboarding_complete(phone_number)

            # Get user's name for personalized message
            user = get_user(phone_number)
            first_name = user[1]

            # Save first memory: signup date
            signup_date = datetime.utcnow().strftime("%B %d, %Y")
            first_memory = f"Signed up for Remyndrs on {signup_date}"
            save_memory(phone_number, first_memory, {"type": "signup", "auto_created": True})

            # Send completion message - focused on immediate value
            resp.message(f"""Perfect! You're all set, {first_name}! 🎉

I just saved your first memory: "{first_memory}"

Try asking me: "What do I have saved?"

(Tip: Check your next message to save me as a contact!)""")

            # Send VCF contact card after 5-second delay
            try:
                vcf_url = f"{APP_BASE_URL}/contact.vcf"
                send_delayed_sms.apply_async(
                    args=[phone_number, "📱 Tap to save Remyndrs to your contacts!"],
                    kwargs={"media_url": vcf_url},
                    countdown=5
                )
            except Exception as vcf_error:
                # Don't fail onboarding if VCF send fails
                logger.warning(f"Could not queue VCF card for {phone_number}: {vcf_error}")

        return Response(content=str(resp), media_type="application/xml")

    except Exception as e:
        logger.error(f"❌ Error in onboarding for {phone_number}: {e}")
        resp = MessagingResponse()
        resp.message("Sorry, something went wrong. Please try again.")
        return Response(content=str(resp), media_type="application/xml")
