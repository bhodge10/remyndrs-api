"""
Onboarding Service
Handles new user onboarding flow
"""

import re
from datetime import datetime, timedelta
from typing import Optional

import pytz
from fastapi.responses import Response
from twilio.twiml.messaging_response import MessagingResponse

from config import logger, FREE_TRIAL_DAYS, TIER_PREMIUM, API_BASE_URL, PREMIUM_MONTHLY_PRICE
from models.user import get_user, get_onboarding_step, create_or_update_user, get_user_timezone
from models.memory import save_memory
from models.list_model import get_pending_shares, accept_share, get_share_name
from models.reminder import get_pending_reminders
from utils.timezone import get_timezone_from_zip
from utils.formatting import get_onboarding_prompt
from services.sms_service import send_sms
from tasks.reminder_tasks import send_delayed_sms, send_engagement_nudge
from services.onboarding_recovery_service import (
    track_onboarding_progress,
    mark_onboarding_complete,
    mark_onboarding_cancelled,
    get_onboarding_progress,
)

# Locked first-reply copy (Retention). Do not rewrite.
FIRST_REPLY_WELCOME = (
    "Welcome to Remyndrs. You just forgot something — text me what to remember and when.\n"
    "No app, no card. Reply STOP anytime."
)
FIRST_REPLY_REMINDER_CONFIRM = (
    "Got it — I'll text you at {time}. Pin this chat so I don't land in spam.\n"
    "No app, no card. Reply STOP anytime."
)

DEFAULT_ONBOARDING_TIMEZONE = "America/New_York"

# First inbound that is Hello / START / empty / a CTA is NOT a reminder.
_NOT_REMINDER_MESSAGES = {
    "",
    "hello", "hi", "hey", "yo", "sup", "hiya", "howdy",
    "hello!", "hi!", "hey!", "hello.", "hi.", "hey.",
    "hi there", "hello there", "hey there",
    "start", "unstop", "begin", "yes", "ok", "okay",
    "thanks", "thank you",
    "go", "work", "try", "kristen",
    "hi, sign me up!", "hey, sign me up!",
    "hi, i'd like to sign up!", "hey, i'd like to sign up!",
    "sign me up!",
}

_TIME_CUE_RE = re.compile(
    r"\b("
    r"tomorrow|tonight|today|"
    r"this\s+(morning|afternoon|evening|weekend|week)|"
    r"next\s+(week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"in\s+\d+\s*(min|mins|minute|minutes|hr|hrs|hour|hours|day|days|week|weeks)|"
    r"at\s+\d{1,2}"
    r")\b"
    r"|\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b",
    re.IGNORECASE,
)
_REMIND_CREATE_RE = re.compile(r"\bremind", re.IGNORECASE)
_REMINDER_VIEW_RE = re.compile(
    r"\b((show|list|see|view)\s+(my\s+)?reminders?|my\s+reminders?|what(?:'s| is)\s+(on\s+)?my\s+reminders?)\b",
    re.IGNORECASE,
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


def looks_like_reminder_intent(message: str) -> bool:
    """True when the first inbound is a reminder (or a what+when), not a greeting/CTA."""
    msg = (message or "").strip()
    if not msg:
        return False
    msg_lower = msg.lower()
    if msg_lower in _NOT_REMINDER_MESSAGES:
        return False
    if _REMINDER_VIEW_RE.search(msg_lower):
        return False
    if _REMIND_CREATE_RE.search(msg_lower):
        return True
    if _TIME_CUE_RE.search(msg_lower) and len(msg_lower.split()) >= 2:
        return True
    return False


def format_first_reminder_time(phone_number: str) -> Optional[str]:
    """Human time for the locked first-reminder confirm, or None if nothing was saved."""
    pending = get_pending_reminders(phone_number)
    if not pending:
        return None

    _id, _text, reminder_date = pending[0]
    tz_str = get_user_timezone(phone_number) or DEFAULT_ONBOARDING_TIMEZONE
    try:
        tz = pytz.timezone(tz_str)
    except pytz.exceptions.UnknownTimeZoneError:
        tz = pytz.timezone(DEFAULT_ONBOARDING_TIMEZONE)

    if isinstance(reminder_date, datetime):
        utc_dt = reminder_date
        if utc_dt.tzinfo is None:
            utc_dt = pytz.UTC.localize(utc_dt)
    else:
        utc_dt = datetime.strptime(str(reminder_date), "%Y-%m-%d %H:%M:%S")
        utc_dt = pytz.UTC.localize(utc_dt)

    local = utc_dt.astimezone(tz)
    time_part = local.strftime("%I:%M %p").lstrip("0")
    now_local = datetime.now(tz)
    if local.date() == now_local.date():
        return time_part
    if local.date() == (now_local + timedelta(days=1)).date():
        return f"{time_part} tomorrow"
    return f"{time_part} on {local.strftime('%A, %B')} {local.day}"


def first_inbound_reminder_reply(phone_number: str) -> Optional[str]:
    """Locked first-reply confirm if a reminder was created; otherwise None."""
    time_display = format_first_reminder_time(phone_number)
    if not time_display:
        return None
    return FIRST_REPLY_REMINDER_CONFIRM.format(time=time_display)


def _auto_accept_pending_shares(phone_number):
    """Accept pending shared-list invites. Returns list of (list_name, owner_phone)."""
    accepted_lists = []
    pending = get_pending_shares(phone_number)
    for _share_id, list_id, owner_phone, list_name in pending:
        success, _ = accept_share(phone_number, list_id)
        if success:
            accepted_lists.append((list_name, owner_phone))
            display_name = get_share_name(list_id, phone_number)
            if not display_name:
                new_user = get_user(phone_number)
                display_name = (new_user[1] if new_user else None) or "Someone"
            send_sms(
                owner_phone,
                f"{display_name} joined Remyndrs and accepted your shared list '{list_name}'!",
                message_type="reply",
            )
    return accepted_lists


def _schedule_post_onboarding_touchbacks(phone_number):
    """1-hour delayed VCF (not on message 1) and 5-minute engagement nudge."""
    vcf_url = f"{API_BASE_URL}/contact.vcf"
    vcf_message = """📱 Tap to save Remyndrs to your contacts!

Tip: Pin this conversation to keep me at the top of your texts — that way I'm always one tap away when you need to remember something!"""
    try:
        send_delayed_sms.apply_async(
            args=[phone_number, vcf_message],
            kwargs={"media_url": vcf_url},
            countdown=3600,  # 1 hour — do not send on message 1
        )
    except Exception as celery_error:
        # Celery not available - fall back to immediate send
        logger.info(f"Celery unavailable, sending VCF immediately: {celery_error}")
        try:
            send_sms(phone_number, vcf_message, media_url=vcf_url, message_type="onboarding")
        except Exception as sms_error:
            logger.warning(f"Could not send VCF card for {phone_number}: {sms_error}")

    try:
        nudge_scheduled_at = datetime.utcnow()
        create_or_update_user(
            phone_number,
            five_minute_nudge_scheduled_at=nudge_scheduled_at,
            five_minute_nudge_sent=False,
            post_onboarding_interactions=0,
        )
        send_engagement_nudge.apply_async(
            args=[phone_number],
            countdown=300,  # 5 minutes
        )
        logger.info(f"Scheduled 5-minute engagement nudge for ...{phone_number[-4:]}")
    except Exception as nudge_error:
        logger.warning(f"Could not schedule engagement nudge for {phone_number}: {nudge_error}")


def complete_fast_onboarding(phone_number, zip_code=None, timezone=None):
    """
    Open the account so the first reminder is not gated on name/ZIP.

    Uses America/New_York when timezone is unknown. Starts the Premium trial.
    Does not send a first-reply SMS — the caller owns that copy.
    """
    user = get_user(phone_number)
    existing_timezone = user[5] if user else None
    existing_zip = user[4] if user else None
    first_name = user[1] if user else None

    timezone = timezone or existing_timezone or DEFAULT_ONBOARDING_TIMEZONE
    zip_code = zip_code or existing_zip

    trial_end_date = datetime.utcnow() + timedelta(days=FREE_TRIAL_DAYS)
    kwargs = {
        "timezone": timezone,
        "onboarding_complete": True,
        "onboarding_step": 3,
        "premium_status": TIER_PREMIUM,
        "trial_end_date": trial_end_date,
    }
    if zip_code:
        kwargs["zip_code"] = zip_code
    create_or_update_user(phone_number, **kwargs)

    mark_onboarding_complete(phone_number)
    accepted_lists = _auto_accept_pending_shares(phone_number)

    signup_date = datetime.utcnow().strftime("%B %d, %Y")
    first_memory = f"Signed up for Remyndrs on {signup_date}"
    save_memory(phone_number, first_memory, {"type": "signup", "auto_created": True})

    _schedule_post_onboarding_touchbacks(phone_number)

    user_tz = pytz.timezone(timezone)
    trial_end_local = trial_end_date.replace(tzinfo=pytz.UTC).astimezone(user_tz)
    trial_end_str = trial_end_local.strftime("%B %d")

    logger.info(f"Reminder-first onboarding complete for ...{phone_number[-4:]} tz={timezone}")
    return {
        "first_name": first_name,
        "timezone": timezone,
        "trial_end_str": trial_end_str,
        "accepted_lists": accepted_lists,
        "first_memory": first_memory,
    }


def handle_onboarding(phone_number, message):
    """Handle onboarding for new users.

    Returns a TwiML Response for greetings / leftover name-ZIP answers.
    Returns None when the inbound is a reminder so main.py can create it
    (name/ZIP must not gate the first reminder).
    """
    try:
        step = get_onboarding_step(phone_number)
        resp = MessagingResponse()

        message_lower = message.lower().strip()
        message_stripped = message.strip()

        # First inbound that looks like a reminder (or leftover users still on
        # step 1/2) must create it — not bounce to the name/ZIP quiz.
        if looks_like_reminder_intent(message):
            complete_fast_onboarding(phone_number)
            return None

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

        # Handle skip requests during ZIP step (leftover in-flight users only)
        if message_lower in ['skip', 'pass', "i don't want to", "dont want to"]:
            if step == 2:
                resp.message("""I totally get it! But here's why I need it:

Without your ZIP code, I can't figure out your timezone. That means reminders might arrive at the wrong time (imagine getting a 2pm reminder at 5am 😬).

Your 5-digit ZIP code helps me send reminders when YOU need them.

What's your ZIP code?""")
                return Response(content=str(resp), media_type="application/xml")

        if step == 0:
            # Reminder-first: no name/ZIP quiz, no AI-powered pitch, no VCF on msg 1.
            complete_fast_onboarding(phone_number)
            resp.message(FIRST_REPLY_WELCOME)

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
            # Leftover in-flight users still answering ZIP
            zip_code, error_type = validate_zip_code(message_stripped)

            if error_type:
                resp.message(get_zip_error_message(error_type, message_stripped))
                return Response(content=str(resp), media_type="application/xml")

            timezone = get_timezone_from_zip(zip_code)
            result = complete_fast_onboarding(phone_number, zip_code=zip_code, timezone=timezone)
            first_name = result["first_name"] or "there"
            trial_end_str = result["trial_end_str"]
            accepted_lists = result["accepted_lists"]
            first_memory = result["first_memory"]

            if accepted_lists:
                list_name, _ = accepted_lists[0]
                shared_note = f"\n\nYou now have access to '{list_name}'! Text 'Show {list_name}' to see it."
                if len(accepted_lists) > 1:
                    shared_note += f"\n(Plus {len(accepted_lists) - 1} more shared list{'s' if len(accepted_lists) > 2 else ''}!)"
                resp.message(f"""You're all set, {first_name}! 🎉

You have full Premium access until {trial_end_str} — unlimited reminders, lists & memories. After that, the core service is free forever — no credit card ever needed.{shared_note}

Keep an eye out for a quick morning tip over the next week or so — I'll show you what else I can do.""")
            else:
                resp.message(f"""Perfect! You're all set, {first_name}! 🎉

You have full Premium access until {trial_end_str} — unlimited reminders, lists & memories. After that, the core service is free forever — no credit card ever needed.

I just saved your first memory: "{first_memory}"

Keep an eye out for a quick morning tip over the next week or so — I'll show you what else I can do.

Try asking me: "What do I have saved?" """)

        return Response(content=str(resp), media_type="application/xml")

    except Exception as e:
        logger.error(f"❌ Error in onboarding for {phone_number}: {e}")
        resp = MessagingResponse()
        resp.message("Sorry, something went wrong. Please try again.")
        return Response(content=str(resp), media_type="application/xml")
