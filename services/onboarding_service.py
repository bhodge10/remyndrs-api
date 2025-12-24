"""
Onboarding Service
Handles new user onboarding flow
"""

from fastapi.responses import Response
from twilio.twiml.messaging_response import MessagingResponse

from config import logger
from models.user import get_user, get_onboarding_step, create_or_update_user
from utils.timezone import get_timezone_from_zip, get_user_current_time
from utils.formatting import get_onboarding_prompt
from services.metrics_service import set_referral_source

def handle_onboarding(phone_number, message):
    """Handle onboarding flow for new users"""
    try:
        step = get_onboarding_step(phone_number)
        resp = MessagingResponse()

        message_lower = message.lower().strip()
        service_keywords = ['remind', 'list', 'delete', 'what', 'when', 'where', 'how', 'my']

        # Check if user is trying to use the service before completing onboarding
        if any(keyword in message_lower for keyword in service_keywords) and step > 0:
            resp.message(f"""⚠️ Please complete your setup first!

You're on step {step} of 4. Let's finish quickly:

{get_onboarding_prompt(step)}""")
            return Response(content=str(resp), media_type="application/xml")

        if step == 0:
            # Welcome message - ask for first name
            create_or_update_user(phone_number, onboarding_step=1)
            resp.message("""Welcome! 👋

I help you remember details about your stuff and set reminders.

Let's get you set up!

What's your first name?""")

        elif step == 1:
            # Store first name, ask for last name
            first_name = message.strip()
            create_or_update_user(phone_number, first_name=first_name, onboarding_step=2)
            resp.message(f"Nice to meet you, {first_name}! What's your last name?")

        elif step == 2:
            # Store last name, ask for email
            last_name = message.strip()
            create_or_update_user(phone_number, last_name=last_name, onboarding_step=3)
            resp.message("Great! What's your email address?")

        elif step == 3:
            # Store email, ask for zip code
            email = message.strip()
            create_or_update_user(phone_number, email=email, onboarding_step=4)
            resp.message("Perfect! What's your ZIP code? (I'll use this to set your timezone for reminders)")

        elif step == 4:
            # Store zip code, calculate timezone, ask for referral source
            zip_code = message.strip()

            # Validate zip code (basic validation)
            if not zip_code.isdigit() or len(zip_code) != 5:
                resp.message("Please enter a valid 5-digit ZIP code:")
                return Response(content=str(resp), media_type="application/xml")

            # Get timezone from zip code
            timezone = get_timezone_from_zip(zip_code)

            # Save zip and timezone, move to referral step
            create_or_update_user(
                phone_number,
                zip_code=zip_code,
                timezone=timezone,
                onboarding_step=5
            )

            resp.message("""Almost done! One quick question:

How did you hear about us?

Reply: Reddit, Facebook, Google, Friend, Ad, or skip""")

        elif step == 5:
            # Handle referral source (optional)
            response_lower = message.lower().strip()

            # Map common responses to standardized sources
            source_map = {
                'reddit': 'reddit',
                'facebook': 'facebook',
                'fb': 'facebook',
                'google': 'google',
                'friend': 'friend',
                'ad': 'ad',
                'ads': 'ad',
                'tiktok': 'tiktok',
                'twitter': 'twitter',
                'x': 'twitter',
                'other': 'other',
                'skip': None
            }

            # Get the source or default to 'other'
            referral = source_map.get(response_lower, 'other')
            if referral:
                set_referral_source(phone_number, referral)

            # Mark onboarding complete
            create_or_update_user(
                phone_number,
                onboarding_complete=True,
                onboarding_step=6
            )

            # Get user's name for personalized message
            user = get_user(phone_number)
            first_name = user[1]
            user_time = get_user_current_time(phone_number)
            timezone = user[5]

            resp.message(f"""All set, {first_name}!

Your timezone: {timezone}
Your current time: {user_time.strftime('%I:%M %p')}

Try me out:
- "My Honda Accord is a 2018, VIN ABC123"
- "Remind me at 9pm to take meds"
- "When did I get new tires?"

You can also text:
- "LIST ALL" to see all your memories
- "DELETE ALL" to clear your data""")

        return Response(content=str(resp), media_type="application/xml")
    
    except Exception as e:
        logger.error(f"❌ Error in onboarding for {phone_number}: {e}")
        resp = MessagingResponse()
        resp.message("Sorry, something went wrong. Please try again.")
        return Response(content=str(resp), media_type="application/xml")
