"""
SMS Service
Handles sending SMS messages via Twilio
"""

import os
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, logger


class UserOptedOutError(Exception):
    """Raised when sending to a user who has opted out (Twilio error 21610).
    Callers should NOT retry when this is raised."""
    pass

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


def _log_outbound(phone_number, message_type):
    """Log an outbound SMS to sms_outbound_log for cost tracking."""
    try:
        from database import get_db_connection, return_db_connection
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute(
                'INSERT INTO sms_outbound_log (phone_number, message_type) VALUES (%s, %s)',
                (phone_number, message_type)
            )
            conn.commit()
        finally:
            return_db_connection(conn)
    except Exception as e:
        logger.warning(f"Failed to log outbound SMS: {e}")


def send_sms(to_number, message, media_url=None, message_type="other"):
    """Send an SMS/MMS message via Twilio

    Args:
        to_number: Recipient phone number
        message: Text message body
        media_url: Optional URL for MMS attachment (e.g., image, VCF file)
        message_type: Category for cost tracking (e.g., 'reminder', 'nudge', 'trial', 'reply', 'other')

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
        _log_outbound(to_number, message_type)
    except TwilioRestException as e:
        if e.code == 21610:
            # User has unsubscribed at the carrier level — mark opted out and don't retry
            logger.warning(f"Twilio 21610: User {to_number} is unsubscribed. Marking opted out.")
            try:
                from models.user import mark_user_opted_out
                mark_user_opted_out(to_number)
            except Exception as opt_out_err:
                logger.error(f"Failed to mark {to_number} as opted out: {opt_out_err}")
            raise UserOptedOutError(f"User {to_number} has opted out (Twilio 21610)")
        logger.error(f"Twilio error sending SMS to {to_number}: {e}")
        raise
    except Exception as e:
        logger.error(f"Error sending SMS to {to_number}: {e}")
        raise  # Re-raise so callers can handle/retry


def notify_admin(message):
    """Send an admin notification SMS. Never raises — failure is logged and swallowed
    so user-facing request handling is never blocked by a notification error."""
    from config import ADMIN_NOTIFICATION_PHONE
    if not ADMIN_NOTIFICATION_PHONE:
        return
    try:
        send_sms(ADMIN_NOTIFICATION_PHONE, message, message_type="admin_notification")
    except Exception as e:
        logger.warning(f"Admin notification to {ADMIN_NOTIFICATION_PHONE} failed: {e}")
