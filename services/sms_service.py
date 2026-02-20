"""
SMS Service
Handles sending SMS messages via Twilio
"""

import os
from twilio.rest import Client
from config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, logger

# Safety check: Detect test environment
_ENVIRONMENT = os.environ.get("ENVIRONMENT", "production").lower()
_IS_TEST_ENV = _ENVIRONMENT in ("test", "testing", "development") or \
               TWILIO_ACCOUNT_SID == "test_sid" or \
               TWILIO_ACCOUNT_SID.startswith("AC_TEST")

# Initialize Twilio client (only if not in test mode)
if _IS_TEST_ENV:
    twilio_client = None
    logger.warning("SMS Service: Running in TEST mode - Twilio client NOT initialized")
else:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def send_sms(to_number, message, media_url=None):
    """Send an SMS/MMS message via Twilio

    Args:
        to_number: Recipient phone number
        message: Text message body
        media_url: Optional URL for MMS attachment (e.g., image, VCF file)

    Note:
        In test environments (ENVIRONMENT=test/development or test credentials),
        this function will log the message but NOT send via Twilio.
    """
    # Safety check: Block SMS in test environments
    if _IS_TEST_ENV or twilio_client is None:
        logger.info(f"[TEST MODE] Would send SMS to {to_number}: {message[:50]}...")
        return None

    # Validate outbound message length (Twilio limit is 1600 chars)
    if len(message) > 1600:
        logger.warning(f"Outbound SMS to {to_number} truncated from {len(message)} to 1600 chars")
        message = message[:1550] + "\n\n(Message truncated)"

    try:
        kwargs = {
            "body": message,
            "from_": TWILIO_PHONE_NUMBER,
            "to": to_number
        }

        if media_url:
            kwargs["media_url"] = [media_url]

        twilio_client.messages.create(**kwargs)
        logger.info(f"Sent {'MMS' if media_url else 'SMS'} to {to_number}")
    except Exception as e:
        logger.error(f"Error sending SMS to {to_number}: {e}")
        raise  # Re-raise so callers can handle/retry
