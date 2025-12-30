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
            resp.message("""Welcome to Remyndrs! 👋

I help you remember details about your stuff and set reminders.

Just 4 quick questions to get started (name, email, ZIP), then you're all set!

Note: During beta, you may experience brief periods without replies or delayed reminders as we roll out updates.

Tip: Text ? anytime for help.

What's your first name?""")

        elif step == 1:
            # Store first name, ask for last name
            first_name = message.strip()
            create_or_update_user(phone_number, first_name=first_name, onboarding_step=2)
            resp.message(f"Nice to meet you, {first_name}! Last name?")

        elif step == 2:
            # Store last name, ask for email
            last_name = message.strip()
            create_or_update_user(phone_number, last_name=last_name, onboarding_step=3)
            resp.message("Got it! Email address?")

        elif step == 3:
            # Store email, ask for zip code
            email = message.strip()
            create_or_update_user(phone_number, email=email, onboarding_step=4)
            resp.message("Almost done! ZIP code? (for your timezone)")

        elif step == 4:
            # Store zip code, calculate timezone, complete onboarding
            zip_code = message.strip()

            # Validate zip code (basic validation)
            if not zip_code.isdigit() or len(zip_code) != 5:
                resp.message("Please enter a valid 5-digit ZIP code:")
                return Response(content=str(resp), media_type="application/xml")

            # Get timezone from zip code
            timezone = get_timezone_from_zip(zip_code)

            # Save zip and timezone, mark onboarding complete
            create_or_update_user(
                phone_number,
                zip_code=zip_code,
                timezone=timezone,
                onboarding_complete=True,
                onboarding_step=5
            )

            # Get user's name for personalized message
            user = get_user(phone_number)
            first_name = user[1]
            user_time = get_user_current_time(phone_number)

            resp.message(f"""You're all set, {first_name}! Try these:

📝 "Add milk, eggs, bread to grocery list"
🧠 "Remember I parked on Level 3 spot 1"
⏰ "Remind me to call mom tomorrow at 2pm"

Your timezone: {timezone}
Your current time: {user_time.strftime('%I:%M %p')}

Text ? anytime for help.""")

        return Response(content=str(resp), media_type="application/xml")
    
    except Exception as e:
        logger.error(f"❌ Error in onboarding for {phone_number}: {e}")
        resp = MessagingResponse()
        resp.message("Sorry, something went wrong. Please try again.")
        return Response(content=str(resp), media_type="application/xml")
