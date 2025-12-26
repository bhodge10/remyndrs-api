"""
Reminder Service
Handles reminder background checking and sending
"""

import threading
import time
from datetime import datetime

from config import logger, REMINDER_CHECK_INTERVAL
from models.reminder import get_due_reminders, mark_reminder_sent, update_last_sent_reminder
from services.sms_service import send_sms
from services.metrics_service import track_reminder_delivery

def check_reminders():
    """Background job that runs every minute to check for due reminders"""
    logger.info("🔄 Reminder checker thread started")
    
    while True:
        try:
            logger.info(f"⏰ Checking for due reminders at {datetime.utcnow()}")
            due_reminders = get_due_reminders()

            if due_reminders:
                logger.info(f"📬 Found {len(due_reminders)} due reminders")
                
                for reminder_id, phone_number, reminder_text in due_reminders:
                    try:
                        send_sms(phone_number, f"Reminder: {reminder_text}\n\n(Reply SNOOZE to snooze)")
                        mark_reminder_sent(reminder_id)
                        update_last_sent_reminder(phone_number, reminder_id)
                        track_reminder_delivery(reminder_id, 'sent')
                        logger.info(f"Sent reminder {reminder_id} to {phone_number}")
                    except Exception as e:
                        track_reminder_delivery(reminder_id, 'failed', str(e))
                        logger.error(f"Failed to send reminder {reminder_id}: {e}")
            else:
                logger.info("No due reminders")

        except Exception as e:
            logger.error(f"❌ Error in reminder checker loop: {e}")
            # Don't crash - just log and continue

        # Wait for next check
        time.sleep(REMINDER_CHECK_INTERVAL)

def start_reminder_checker():
    """Start the reminder checker background thread"""
    try:
        reminder_thread = threading.Thread(target=check_reminders, daemon=True)
        reminder_thread.start()
        logger.info("✅ Reminder checker thread launched")
    except Exception as e:
        logger.error(f"❌ Failed to start reminder thread: {e}")
