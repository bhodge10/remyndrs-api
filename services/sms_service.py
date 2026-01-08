"""
SMS Service
Handles sending SMS messages via Twilio
"""

from twilio.rest import Client
from config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, logger

# Initialize Twilio client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def send_sms(to_number, message, media_url=None):
    """Send an SMS/MMS message via Twilio

    Args:
        to_number: Recipient phone number
        message: Text message body
        media_url: Optional URL for MMS attachment (e.g., image, VCF file)
    """
    try:
        kwargs = {
            "body": message,
            "from_": TWILIO_PHONE_NUMBER,
            "to": to_number
        }

        if media_url:
            kwargs["media_url"] = [media_url]

        twilio_client.messages.create(**kwargs)
        logger.info(f"✅ Sent {'MMS' if media_url else 'SMS'} to {to_number}")
    except Exception as e:
        logger.error(f"❌ Error sending SMS to {to_number}: {e}")
        raise  # Re-raise so callers can handle/retry
