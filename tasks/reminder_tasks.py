"""
Celery Tasks for SMS Reminder Processing
Implements atomic reminder claiming with SELECT FOR UPDATE SKIP LOCKED.
"""

from celery import shared_task
from celery.utils.log import get_task_logger

from celery_app import celery_app
from models.reminder import (
    claim_due_reminders,
    mark_reminder_sent,
    update_last_sent_reminder,
    release_stale_claims,
)
from services.sms_service import send_sms
from services.metrics_service import track_reminder_delivery

logger = get_task_logger(__name__)


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def check_and_send_reminders(self):
    """
    Periodic task to check for due reminders and dispatch them.
    Uses SELECT FOR UPDATE SKIP LOCKED for atomic claiming.

    This task runs every 30 seconds via Celery Beat.
    Each invocation claims a batch of reminders atomically.
    """
    try:
        # Claim up to 10 reminders atomically
        reminders = claim_due_reminders(batch_size=10)

        if not reminders:
            logger.debug("No due reminders found")
            return {"processed": 0}

        logger.info(f"Claimed {len(reminders)} reminders for processing")

        # Dispatch individual send tasks for each reminder
        for reminder in reminders:
            send_single_reminder.delay(
                reminder_id=reminder["id"],
                phone_number=reminder["phone_number"],
                reminder_text=reminder["reminder_text"],
            )

        return {"processed": len(reminders)}

    except Exception as exc:
        logger.exception("Error in check_and_send_reminders")
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
)
def send_single_reminder(self, reminder_id: int, phone_number: str, reminder_text: str):
    """
    Send a single SMS reminder via Twilio.
    Marks reminder as sent only after successful delivery.

    Uses exponential backoff for retries (30s, 60s, 120s, max 300s).
    """
    try:
        logger.info(f"Sending reminder {reminder_id} to {phone_number}")

        # Format message with snooze option
        message = f"Reminder: {reminder_text}\n\n(Reply SNOOZE to snooze)"

        # Send SMS via Twilio
        send_sms(phone_number, message)

        # Mark as sent only after successful send
        mark_reminder_sent(reminder_id)

        # Update last sent reminder for snooze feature
        update_last_sent_reminder(phone_number, reminder_id)

        # Track delivery metrics
        track_reminder_delivery(reminder_id, "sent")

        logger.info(f"Reminder {reminder_id} sent successfully")
        return {
            "reminder_id": reminder_id,
            "status": "sent",
        }

    except self.MaxRetriesExceededError:
        logger.error(f"Max retries exceeded for reminder {reminder_id}")
        track_reminder_delivery(reminder_id, "failed", "Max retries exceeded")
        raise
    except Exception as exc:
        logger.exception(f"Error sending reminder {reminder_id}")
        track_reminder_delivery(reminder_id, "failed", str(exc))
        raise self.retry(exc=exc)


@celery_app.task
def release_stale_claims_task():
    """
    Release reminders that were claimed but not processed.

    This handles cases where a worker crashes after claiming
    but before sending. Runs every 5 minutes via Beat.
    """
    try:
        count = release_stale_claims(timeout_minutes=5)
        if count > 0:
            logger.warning(f"Released {count} stale reminder claims")
        return {"released": count}
    except Exception as exc:
        logger.exception("Error releasing stale claims")
        raise


@celery_app.task
def analyze_conversations_task():
    """
    Analyze recent conversations for quality issues.
    Runs every 4 hours via Beat.
    """
    try:
        from services.conversation_analyzer import analyze_recent_conversations
        result = analyze_recent_conversations(batch_size=50)
        logger.info(f"Conversation analysis complete: {result}")
        return result
    except Exception as exc:
        logger.exception("Error in conversation analysis task")
        raise
