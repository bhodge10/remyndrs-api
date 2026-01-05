"""
SMS Memory Service - Main Application
Entry point for the FastAPI application
"""

import re
import json
import pytz
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import Response, HTMLResponse, FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator

# Local imports
import secrets
from config import logger, ENVIRONMENT, MAX_LISTS_PER_USER, MAX_ITEMS_PER_LIST, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, PUBLIC_PHONE_NUMBER, ADMIN_USERNAME, ADMIN_PASSWORD, RATE_LIMIT_MESSAGES, RATE_LIMIT_WINDOW, REQUEST_TIMEOUT
from collections import defaultdict
import time
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi import Depends
from database import init_db, log_interaction, get_setting
from models.user import get_user, is_user_onboarded, create_or_update_user, get_user_timezone, get_last_active_list, get_pending_list_item, get_pending_reminder_delete, get_pending_memory_delete, mark_user_opted_out
from models.memory import save_memory, get_memories, search_memories, delete_memory
from models.reminder import (
    save_reminder, get_user_reminders, search_pending_reminders, delete_reminder,
    get_last_sent_reminder, mark_reminder_snoozed, save_recurring_reminder,
    get_recurring_reminders, delete_recurring_reminder, pause_recurring_reminder,
    resume_recurring_reminder, save_reminder_with_local_time,
    recalculate_pending_reminders_for_timezone, update_recurring_reminders_timezone
)
from models.list_model import (
    create_list, get_lists, get_list_by_name, get_list_items,
    add_list_item, mark_item_complete, mark_item_incomplete,
    delete_list_item, delete_list, rename_list, clear_list,
    find_item_in_any_list, get_list_count, get_item_count
)
from services.sms_service import send_sms
from services.ai_service import process_with_ai, parse_list_items
from services.onboarding_service import handle_onboarding
# NOTE: Reminder checking is now handled by Celery Beat (see tasks/reminder_tasks.py)
from services.metrics_service import track_user_activity, increment_message_count
from utils.timezone import get_user_current_time
from utils.formatting import get_help_text, format_reminders_list
from utils.validation import mask_phone_number, validate_list_name, validate_item_text, validate_message, log_security_event, detect_sensitive_data, get_sensitive_data_warning
from admin_dashboard import router as dashboard_router, start_broadcast_checker
from cs_portal import router as cs_router


def staging_prefix(message):
    """Add [STAGING] prefix to messages when in staging environment"""
    if ENVIRONMENT == "staging":
        return f"[STAGING] {message}"
    return message


# Snooze duration parser
def parse_snooze_duration(text):
    """
    Parse snooze duration from user input.
    Returns duration in minutes.
    Max: 24 hours (1440 minutes)
    Default: 15 minutes

    Examples:
    - "" or None -> 15 minutes
    - "30" or "30m" -> 30 minutes
    - "1h" or "1 hour" -> 60 minutes
    - "2 hours" -> 120 minutes
    - "1h30m" -> 90 minutes
    """
    import re

    if not text:
        return 15  # Default

    text = text.strip().lower()

    # Try to parse various formats
    try:
        # Just a number (assume minutes)
        if re.match(r'^\d+$', text):
            minutes = int(text)
            return min(minutes, 1440)  # Max 24 hours

        # Minutes: "30m", "30 min", "30 mins", "30 minutes"
        match = re.match(r'^(\d+)\s*(?:m|min|mins|minutes?)$', text)
        if match:
            minutes = int(match.group(1))
            return min(minutes, 1440)

        # Hours: "1h", "1 hr", "1 hour", "2 hours"
        match = re.match(r'^(\d+)\s*(?:h|hr|hrs|hours?)$', text)
        if match:
            hours = int(match.group(1))
            return min(hours * 60, 1440)

        # Combined: "1h30m", "1h 30m", "1 hour 30 minutes"
        match = re.match(r'^(\d+)\s*(?:h|hr|hrs|hours?)\s*(\d+)?\s*(?:m|min|mins|minutes?)?$', text)
        if match:
            hours = int(match.group(1))
            mins = int(match.group(2)) if match.group(2) else 0
            total = hours * 60 + mins
            return min(total, 1440)

        # Fallback: default to 15 minutes
        return 15

    except (ValueError, AttributeError):
        return 15


# Initialize application
logger.info("🚀 SMS Memory Service starting...")
app = FastAPI()


# Request timeout middleware
class TimeoutMiddleware(BaseHTTPMiddleware):
    """Middleware to enforce request timeout limits"""
    async def dispatch(self, request: Request, call_next):
        try:
            return await asyncio.wait_for(
                call_next(request),
                timeout=REQUEST_TIMEOUT
            )
        except asyncio.TimeoutError:
            log_security_event("REQUEST_TIMEOUT", {
                "path": str(request.url.path),
                "method": request.method
            })
            return Response(
                content='{"error": "Request timed out. Please try again."}',
                status_code=504,
                media_type="application/json"
            )

app.add_middleware(TimeoutMiddleware)


# Global exception handler - sanitize all error responses
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch unhandled exceptions and return safe error messages"""
    # Let HTTPException pass through (e.g., for staging fallback 503)
    if isinstance(exc, HTTPException):
        raise exc

    # Log full error details internally
    log_security_event("UNHANDLED_ERROR", {
        "path": str(request.url.path),
        "method": request.method,
        "error_type": type(exc).__name__
    })
    logger.error(f"Unhandled exception: {exc}", exc_info=True)

    # Return sanitized response - never expose internal details
    return Response(
        content='{"error": "An unexpected error occurred. Please try again."}',
        status_code=500,
        media_type="application/json"
    )

# Rate limiting storage: {phone_number: [timestamp1, timestamp2, ...]}
rate_limit_store = defaultdict(list)

def check_rate_limit(phone_number: str) -> bool:
    """Check if phone number has exceeded rate limit. Returns True if allowed."""
    current_time = time.time()
    window_start = current_time - RATE_LIMIT_WINDOW

    # Clean old timestamps and keep only those within the window
    rate_limit_store[phone_number] = [
        ts for ts in rate_limit_store[phone_number] if ts > window_start
    ]

    # Check if under limit
    if len(rate_limit_store[phone_number]) >= RATE_LIMIT_MESSAGES:
        log_security_event("RATE_LIMIT", {"phone": phone_number, "count": len(rate_limit_store[phone_number])})
        return False

    # Add current timestamp
    rate_limit_store[phone_number].append(current_time)
    return True

# HTTP Basic Auth for admin endpoints
security = HTTPBasic()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """Verify admin credentials for protected endpoints"""
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=500, detail="Admin password not configured")

    correct_username = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)

    if not (correct_username and correct_password):
        log_security_event("AUTH_FAILURE", {"username": credentials.username, "endpoint": "admin"})
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# Include admin dashboard router
app.include_router(dashboard_router)

# Include CS portal router
app.include_router(cs_router)

# Initialize database
init_db()

# NOTE: Background reminder checking is now handled by Celery Beat
# See celery_config.py for the schedule and tasks/reminder_tasks.py for the task

# Start scheduled broadcast checker
start_broadcast_checker()

logger.info(f"✅ Application initialized in {ENVIRONMENT} mode")

# =====================================================
# WEBHOOK ENDPOINT
# =====================================================

@app.post("/sms")
async def sms_reply(request: Request, Body: str = Form(...), From: str = Form(...)):
    """Handle incoming SMS from Twilio"""
    try:
        # Validate Twilio signature (skip in development and staging)
        # Note: Staging skips validation because fallback requests have signatures
        # computed for the production URL, which won't validate against staging URL
        if ENVIRONMENT not in ("development", "staging"):
            validator = RequestValidator(TWILIO_AUTH_TOKEN)

            # Get the full URL and signature
            signature = request.headers.get("X-Twilio-Signature", "")

            # Build the full URL (Render uses HTTPS)
            url = str(request.url)
            if url.startswith("http://"):
                url = url.replace("http://", "https://", 1)

            # Get form data for validation
            form_data = await request.form()
            params = {key: form_data[key] for key in form_data}

            if not validator.validate(url, params, signature):
                log_security_event("INVALID_SIGNATURE", {"ip": request.client.host, "url": url[:50]})
                raise HTTPException(status_code=403, detail="Invalid signature")

        incoming_msg = Body.strip()
        phone_number = From

        # Staging Fallback: If enabled in production, fail for test numbers to trigger Twilio fallback URL
        if ENVIRONMENT == "production":
            staging_fallback_enabled = get_setting("staging_fallback_enabled", "false") == "true"
            if staging_fallback_enabled:
                staging_numbers_raw = get_setting("staging_fallback_numbers", "")
                staging_numbers = [n.strip() for n in staging_numbers_raw.split("\n") if n.strip()]
                if phone_number in staging_numbers:
                    logger.info(f"Staging fallback: Triggering fallback for {mask_phone_number(phone_number)}")
                    raise HTTPException(status_code=503, detail="Routing to staging")

        # Staging environment: Only allow phone numbers configured in staging fallback settings
        # If no numbers configured locally, allow all (production controls routing via fallback)
        if ENVIRONMENT == "staging":
            staging_numbers_raw = get_setting("staging_fallback_numbers", "")
            staging_allowed_numbers = [n.strip() for n in staging_numbers_raw.split("\n") if n.strip()]
            if staging_allowed_numbers and phone_number not in staging_allowed_numbers:
                resp = MessagingResponse()
                # Get maintenance message from database (or use default)
                default_msg = "Remyndrs is undergoing maintenance. The service will be back up soon. You will receive a message when it's back up."
                maintenance_msg = get_setting("maintenance_message", default_msg)
                resp.message(maintenance_msg)
                logger.info(f"Staging: Blocked non-test number {mask_phone_number(phone_number)}")
                return Response(content=str(resp), media_type="application/xml")

        # Check rate limit
        if not check_rate_limit(phone_number):
            resp = MessagingResponse()
            resp.message(staging_prefix("You're sending messages too quickly. Please wait a moment and try again."))
            return Response(content=str(resp), media_type="application/xml")

        logger.info(f"Received from {mask_phone_number(phone_number)}: {incoming_msg[:50]}...")

        # Track user activity for metrics
        track_user_activity(phone_number)
        increment_message_count(phone_number)

        # ==========================================
        # RESET ACCOUNT COMMAND (works for everyone)
        # ==========================================
        logger.info(f"Checking reset: '{incoming_msg.upper()}' in ['RESET ACCOUNT', 'RESTART'] = {incoming_msg.upper() in ['RESET ACCOUNT', 'RESTART']}")
        if incoming_msg.upper() in ["RESET ACCOUNT", "RESTART"]:
            logger.info("RESET ACCOUNT matched - resetting user")

            # In staging, reset to step 0 to show the actual new user welcome message
            # In production, reset to step 1 (skips welcome, asks for name directly)
            reset_step = 0 if ENVIRONMENT == "staging" else 1

            create_or_update_user(
                phone_number,
                first_name=None,
                last_name=None,
                email=None,
                zip_code=None,
                timezone='America/New_York',
                onboarding_complete=False,
                onboarding_step=reset_step,
                pending_delete=False,
                pending_reminder_text=None,
                pending_reminder_time=None,
                trial_end_date=None,
                premium_status='free'
            )

            log_interaction(phone_number, incoming_msg, "Account reset", "reset", True)

            # In staging, trigger the actual onboarding flow to show welcome message
            if ENVIRONMENT == "staging":
                return handle_onboarding(phone_number, incoming_msg)

            # In production, show abbreviated reset message
            resp = MessagingResponse()
            resp.message("✅ Your account has been reset. Let's start over!\n\nWhat's your first name?")
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # STOP/UNSUBSCRIBE COMMAND (Twilio compliance)
        # ==========================================
        # Handle all standard Twilio opt-out keywords
        if incoming_msg.upper() in ["STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"]:
            logger.info(f"STOP command received from {mask_phone_number(phone_number)}")

            # Mark user as opted out in our database
            mark_user_opted_out(phone_number)

            resp = MessagingResponse()
            resp.message("You have been unsubscribed from Remyndrs and will no longer receive messages. Reply START to resubscribe.")
            log_interaction(phone_number, incoming_msg, "User opted out", "stop", True)
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # START/RESUBSCRIBE COMMAND (Twilio compliance)
        # ==========================================
        # Note: "YES" is also used for confirmations, so check if user has pending action first
        if incoming_msg.upper() in ["START", "YES", "UNSTOP"]:
            # Check if user has a pending confirmation (delete, etc.)
            user_check = get_user(phone_number)
            has_pending_action = user_check and user_check[9]  # pending_delete flag

            # Only treat as START if no pending action AND message is START/UNSTOP (not YES)
            # OR if opted_out and saying YES to resubscribe
            is_opted_out = user_check and user_check[10] if user_check else False

            if not has_pending_action and (incoming_msg.upper() in ["START", "UNSTOP"] or is_opted_out):
                logger.info(f"START command received from {mask_phone_number(phone_number)}")

                # Clear the opted_out flag
                create_or_update_user(phone_number, opted_out=False, opted_out_at=None)

                resp = MessagingResponse()
                resp.message("Welcome back to Remyndrs! You're now resubscribed and will receive messages again.")
                log_interaction(phone_number, incoming_msg, "User resubscribed", "start", True)
                return Response(content=str(resp), media_type="application/xml")
            # If has pending action or YES without opt-out, fall through to handle confirmation

        # ==========================================
        # SUPPORT MODE CHECK
        # ==========================================
        # Check if user is in an active support conversation
        from services.support_service import (
            get_active_support_ticket, add_support_message,
            exit_support_mode, close_ticket_by_phone,
            is_technician_actively_engaged
        )

        active_ticket = get_active_support_ticket(phone_number)

        if active_ticket:
            ticket_id = active_ticket['ticket_id']
            msg_upper = incoming_msg.strip().upper()

            # Handle EXIT command - leave support mode but keep ticket open
            if msg_upper == "EXIT":
                exit_support_mode(phone_number)
                resp = MessagingResponse()
                resp.message(staging_prefix(f"You've exited support mode. Your ticket #{ticket_id} is still open - text 'SUPPORT: message' anytime to continue the conversation."))
                log_interaction(phone_number, incoming_msg, f"Exited support mode (ticket #{ticket_id})", "support_exit", True)
                return Response(content=str(resp), media_type="application/xml")

            # Handle CLOSE TICKET command - close the ticket
            if msg_upper in ["CLOSE TICKET", "CLOSE"]:
                # Add a system message to the ticket so CS agents can see user closed it
                add_support_message(phone_number, "[User closed the ticket]", 'inbound')
                result = close_ticket_by_phone(phone_number)
                if result['success']:
                    resp = MessagingResponse()
                    resp.message(staging_prefix(f"Your support ticket #{ticket_id} has been closed. Thank you for contacting us! Text 'SUPPORT: message' anytime to open a new ticket."))
                    log_interaction(phone_number, incoming_msg, f"Closed support ticket #{ticket_id}", "support_close", True)
                    return Response(content=str(resp), media_type="application/xml")

            # Route message to support ticket
            result = add_support_message(phone_number, incoming_msg, 'inbound')
            if result['success']:
                resp = MessagingResponse()
                # Skip automated acknowledgment if a support agent is actively engaged
                if is_technician_actively_engaged(ticket_id):
                    # Silent acknowledgment - agent is actively responding
                    log_interaction(phone_number, incoming_msg, f"Support message (ticket #{ticket_id}) - active conversation", "support", True)
                else:
                    # Send acknowledgment when no agent has responded recently
                    resp.message(staging_prefix(f"[Support Ticket #{ticket_id}]\n\nMessage received. We'll respond shortly.\n\n(You're now in support mode - replies will continue to go to support. Text EXIT to leave support or CLOSE to close ticket)"))
                    log_interaction(phone_number, incoming_msg, f"Support message (ticket #{ticket_id})", "support", True)
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # ONBOARDING CHECK
        # ==========================================
        if not is_user_onboarded(phone_number):
            return handle_onboarding(phone_number, incoming_msg)

        # ==========================================
        # AM/PM CLARIFICATION RESPONSE
        # ==========================================
        # Check if user has a pending reminder and their message contains AM or PM
        user = get_user(phone_number)
        msg_upper = incoming_msg.upper()
        has_am_pm = any(x in msg_upper for x in ["AM", "PM", "A.M.", "P.M."])

        if user and len(user) > 11 and user[10] and has_am_pm:  # pending_reminder_text exists and has AM/PM
            pending_text = user[10]
            pending_time = user[11]

            am_pm = "AM" if any(x in msg_upper for x in ["AM", "A.M."]) else "PM"

            try:
                user_time = get_user_current_time(phone_number)
                user_tz = get_user_timezone(phone_number)

                # Clean up the pending_time - remove any existing AM/PM
                clean_time = pending_time.upper().replace("AM", "").replace("PM", "").replace("A.M.", "").replace("P.M.", "").strip()

                # Parse the time
                time_parts = clean_time.split(":")
                hour = int(time_parts[0])
                minute = int(time_parts[1]) if len(time_parts) > 1 else 0

                # Convert to 24-hour format
                if am_pm == "PM" and hour != 12:
                    hour += 12
                elif am_pm == "AM" and hour == 12:
                    hour = 0

                # Create reminder datetime in user's timezone
                reminder_datetime = user_time.replace(hour=hour, minute=minute, second=0, microsecond=0)

                # If time has already passed today, schedule for tomorrow
                if reminder_datetime <= user_time:
                    reminder_datetime = reminder_datetime + timedelta(days=1)

                # Convert to UTC for storage
                reminder_datetime_utc = reminder_datetime.astimezone(pytz.UTC)
                reminder_date_str = reminder_datetime_utc.strftime('%Y-%m-%d %H:%M:%S')

                # Save the reminder
                save_reminder(phone_number, pending_text, reminder_date_str)

                # Format confirmation
                readable_date = reminder_datetime.strftime('%A, %B %d at %I:%M %p')
                reply_text = f"I'll remind you on {readable_date} to {pending_text}."

                # Clear pending reminder
                create_or_update_user(phone_number, pending_reminder_text=None, pending_reminder_time=None)

                log_interaction(phone_number, incoming_msg, reply_text, "reminder_confirmed", True)
                resp = MessagingResponse()
                resp.message(staging_prefix(reply_text))
                return Response(content=str(resp), media_type="application/xml")

            except Exception as e:
                logger.error(f"Error processing time: {e}")
                resp = MessagingResponse()
                resp.message(staging_prefix("Sorry, I had trouble setting that reminder. Please try again."))
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # PENDING DELETE SELECTION (reminders, list items, memories)
        # ==========================================
        # Check if user has pending deletion and sent a number or CANCEL
        pending_delete_data = get_pending_reminder_delete(phone_number)
        if pending_delete_data:
            # Handle CANCEL
            if incoming_msg.strip().upper() == "CANCEL":
                create_or_update_user(phone_number, pending_reminder_delete=None)
                resp = MessagingResponse()
                resp.message("Cancelled.")
                return Response(content=str(resp), media_type="application/xml")

            # Handle number selection
            if incoming_msg.strip().isdigit():
                try:
                    delete_options = json.loads(pending_delete_data)
                    selection = int(incoming_msg.strip())
                    if 1 <= selection <= len(delete_options):
                        selected = delete_options[selection - 1]
                        delete_type = selected.get('type', 'reminder')
                        reply_msg = None

                        if delete_type == 'reminder':
                            if delete_reminder(phone_number, selected['id']):
                                reply_msg = f"Deleted reminder: {selected['text']}"
                                # If this was a recurring reminder, also delete the recurring pattern
                                recurring_id = selected.get('recurring_id')
                                if recurring_id:
                                    if delete_recurring_reminder(recurring_id, phone_number):
                                        reply_msg += " (and its recurring schedule)"
                            else:
                                reply_msg = "Couldn't delete that reminder."

                        elif delete_type == 'list_item':
                            if delete_list_item(phone_number, selected['list_name'], selected['text']):
                                reply_msg = f"Removed '{selected['text']}' from {selected['list_name']}"
                            else:
                                reply_msg = "Couldn't delete that list item."

                        elif delete_type == 'memory':
                            if delete_memory(phone_number, selected['id']):
                                reply_msg = f"Deleted memory: {selected['text']}"
                            else:
                                reply_msg = "Couldn't delete that memory."

                        # Clear pending delete
                        create_or_update_user(phone_number, pending_reminder_delete=None)

                        resp = MessagingResponse()
                        resp.message(staging_prefix(reply_msg))
                        log_interaction(phone_number, incoming_msg, reply_msg, f"delete_{delete_type}", True)
                        return Response(content=str(resp), media_type="application/xml")
                    else:
                        resp = MessagingResponse()
                        resp.message(staging_prefix(f"Please reply with a number between 1 and {len(delete_options)}, or CANCEL"))
                        return Response(content=str(resp), media_type="application/xml")
                except (json.JSONDecodeError, KeyError) as e:
                    logger.error(f"Error parsing pending delete data: {e}")
                    create_or_update_user(phone_number, pending_reminder_delete=None)

        # ==========================================
        # PENDING MEMORY DELETE SELECTION/CONFIRMATION
        # ==========================================
        # Check if user has pending memory deletion
        pending_memory_data = get_pending_memory_delete(phone_number)
        if pending_memory_data:
            try:
                memory_data = json.loads(pending_memory_data)

                # Check if this is a confirmation (single memory awaiting YES)
                if memory_data.get('awaiting_confirmation'):
                    if incoming_msg.upper() == "YES":
                        memory_id = memory_data['id']
                        memory_text = memory_data['text']
                        if delete_memory(phone_number, memory_id):
                            reply_msg = f"Deleted memory: {memory_text[:100]}{'...' if len(memory_text) > 100 else ''}"
                        else:
                            reply_msg = "Couldn't delete that memory."
                        create_or_update_user(phone_number, pending_memory_delete=None)
                        resp = MessagingResponse()
                        resp.message(reply_msg)
                        log_interaction(phone_number, incoming_msg, reply_msg, "delete_memory_confirmed", True)
                        return Response(content=str(resp), media_type="application/xml")
                    elif incoming_msg.upper() in ["NO", "CANCEL"]:
                        create_or_update_user(phone_number, pending_memory_delete=None)
                        resp = MessagingResponse()
                        resp.message("Cancelled. Your memory is safe!")
                        log_interaction(phone_number, incoming_msg, "Delete cancelled", "delete_memory_cancelled", True)
                        return Response(content=str(resp), media_type="application/xml")

                # Check if this is a number selection (multiple memories)
                elif memory_data.get('options') and incoming_msg.strip().isdigit():
                    memory_options = memory_data['options']
                    selection = int(incoming_msg.strip())
                    if 1 <= selection <= len(memory_options):
                        selected_memory = memory_options[selection - 1]
                        memory_id = selected_memory['id']
                        memory_text = selected_memory['text']

                        # Ask for confirmation before deleting
                        confirm_data = json.dumps({
                            'awaiting_confirmation': True,
                            'id': memory_id,
                            'text': memory_text
                        })
                        create_or_update_user(phone_number, pending_memory_delete=confirm_data)

                        # Truncate long memory text for display
                        display_text = memory_text[:100] + ('...' if len(memory_text) > 100 else '')
                        reply_msg = f"Delete memory: '{display_text}'?\n\nReply YES to confirm or NO to cancel."

                        resp = MessagingResponse()
                        resp.message(reply_msg)
                        log_interaction(phone_number, incoming_msg, reply_msg, "delete_memory_confirm_request", True)
                        return Response(content=str(resp), media_type="application/xml")
                    else:
                        resp = MessagingResponse()
                        resp.message(f"Please reply with a number between 1 and {len(memory_options)}")
                        return Response(content=str(resp), media_type="application/xml")

            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"Error parsing pending memory delete data: {e}")
                create_or_update_user(phone_number, pending_memory_delete=None)

        # ==========================================
        # PENDING LIST ITEM SELECTION
        # ==========================================
        # Check if user has a pending list item and sent a number
        # But NOT if we're in a pending delete flow (pending_list_item stores list name for deletion)
        pending_item = get_pending_list_item(phone_number)
        user_for_pending = get_user(phone_number)
        pending_delete_flag = user_for_pending and user_for_pending[9] if user_for_pending else False
        if pending_item and not pending_delete_flag:
            if incoming_msg.strip().isdigit():
                list_num = int(incoming_msg.strip())
                lists = get_lists(phone_number)
                if 1 <= list_num <= len(lists):
                    selected_list = lists[list_num - 1]
                    list_id = selected_list[0]
                    list_name = selected_list[1]

                    # Parse multiple items from the pending item
                    items_to_add = parse_list_items(pending_item, phone_number)

                    # Check item limit
                    item_count = get_item_count(list_id)
                    available_slots = MAX_ITEMS_PER_LIST - item_count

                    if available_slots <= 0:
                        resp = MessagingResponse()
                        resp.message(f"Your {list_name} is full ({MAX_ITEMS_PER_LIST} items max). Remove some items first.")
                        create_or_update_user(phone_number, pending_list_item=None)
                        return Response(content=str(resp), media_type="application/xml")

                    # Add items up to the limit
                    added_items = []
                    for item in items_to_add:
                        if len(added_items) < available_slots:
                            add_list_item(list_id, phone_number, item)
                            added_items.append(item)

                    # Clear pending item and track last active list
                    create_or_update_user(phone_number, pending_list_item=None, last_active_list=list_name)

                    resp = MessagingResponse()
                    if len(added_items) == 1:
                        resp.message(f"Added {added_items[0]} to your {list_name}")
                    elif len(added_items) < len(items_to_add):
                        resp.message(f"Added {len(added_items)} items to your {list_name}: {', '.join(added_items)}. ({len(items_to_add) - len(added_items)} items skipped - list full)")
                    else:
                        resp.message(f"Added {len(added_items)} items to your {list_name}: {', '.join(added_items)}")
                    log_interaction(phone_number, incoming_msg, f"Added {len(added_items)} items to {list_name}", "add_to_list", True)
                    return Response(content=str(resp), media_type="application/xml")
                else:
                    resp = MessagingResponse()
                    resp.message(f"Please reply with a number between 1 and {len(lists)}")
                    return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # RECURRING REMINDER DELETE BY NUMBER
        # ==========================================
        # If user sends just a number and was viewing recurring reminders, delete that one
        if incoming_msg.strip().isdigit():
            last_active = get_last_active_list(phone_number)
            if last_active == "__RECURRING__":
                item_num = int(incoming_msg.strip())
                recurring_list = get_recurring_reminders(phone_number, include_inactive=True)
                if recurring_list and 1 <= item_num <= len(recurring_list):
                    r = recurring_list[item_num - 1]
                    if delete_recurring_reminder(r['id'], phone_number):
                        resp = MessagingResponse()
                        resp.message(f"Deleted recurring reminder: {r['reminder_text']}")
                        log_interaction(phone_number, incoming_msg, f"Deleted recurring {r['id']}", "delete_recurring", True)
                    else:
                        resp = MessagingResponse()
                        resp.message("Couldn't delete that recurring reminder.")
                    # Clear the recurring context
                    create_or_update_user(phone_number, last_active_list=None)
                    return Response(content=str(resp), media_type="application/xml")
                else:
                    resp = MessagingResponse()
                    resp.message(f"Please enter a number between 1 and {len(recurring_list)}. Text 'MY RECURRING' to see the list.")
                    return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # LIST SELECTION BY NUMBER
        # ==========================================
        # If user sends just a number and has lists, show that list
        if incoming_msg.strip().isdigit():
            list_num = int(incoming_msg.strip())
            lists = get_lists(phone_number)
            if lists and 1 <= list_num <= len(lists):
                selected_list = lists[list_num - 1]
                list_id = selected_list[0]
                list_name = selected_list[1]

                # Track last active list
                create_or_update_user(phone_number, last_active_list=list_name)

                items = get_list_items(list_id)
                if items:
                    item_lines = []
                    for i, (item_id, item_text, completed) in enumerate(items, 1):
                        if completed:
                            item_lines.append(f"{i}. [x] {item_text}")
                        else:
                            item_lines.append(f"{i}. {item_text}")
                    reply_msg = f"{list_name}:\n\n" + "\n".join(item_lines)
                else:
                    reply_msg = f"Your {list_name} is empty."

                resp = MessagingResponse()
                resp.message(reply_msg)
                log_interaction(phone_number, incoming_msg, reply_msg, "show_list", True)
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # DELETE REMINDER BY NUMBER
        # ==========================================
        # Handle "Delete reminder 1", "Cancel reminder 2", etc.
        delete_reminder_match = re.match(r'^(?:delete|remove|cancel)\s+reminder\s+(\d+)$', incoming_msg.strip(), re.IGNORECASE)
        if delete_reminder_match:
            reminder_num = int(delete_reminder_match.group(1))
            # Get user's reminders (pending only)
            reminders = get_user_reminders(phone_number)
            pending = [r for r in reminders if not r[4]]  # r[4] is 'sent' flag

            if not pending:
                resp = MessagingResponse()
                resp.message("You don't have any pending reminders to delete.")
                return Response(content=str(resp), media_type="application/xml")

            if reminder_num < 1 or reminder_num > len(pending):
                resp = MessagingResponse()
                resp.message(f"Please enter a number between 1 and {len(pending)}. Text 'MY REMINDERS' to see the list.")
                return Response(content=str(resp), media_type="application/xml")

            reminder = pending[reminder_num - 1]
            reminder_id = reminder[0]
            reminder_text = reminder[2]

            if delete_reminder(phone_number, reminder_id):
                resp = MessagingResponse()
                resp.message(f"Deleted reminder: {reminder_text}")
                log_interaction(phone_number, incoming_msg, f"Deleted reminder {reminder_id}", "delete_reminder", True)
            else:
                resp = MessagingResponse()
                resp.message("Couldn't delete that reminder. Please try again.")
                log_interaction(phone_number, incoming_msg, "Delete reminder failed", "delete_reminder", False)
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # DELETE BY NUMBER (smart disambiguation)
        # ==========================================
        # Handle "Delete 1", "Remove 2", etc. - ask user what they want to delete
        delete_match = re.match(r'^(?:delete|remove)\s+(\d+)$', incoming_msg.strip(), re.IGNORECASE)
        if delete_match:
            item_num = int(delete_match.group(1))

            # Check if user was viewing recurring reminders
            last_active = get_last_active_list(phone_number)
            if last_active == "__RECURRING__":
                # Delete from recurring reminders list
                recurring_list = get_recurring_reminders(phone_number, include_inactive=True)
                if recurring_list and 1 <= item_num <= len(recurring_list):
                    r = recurring_list[item_num - 1]
                    if delete_recurring_reminder(r['id'], phone_number):
                        resp = MessagingResponse()
                        resp.message(f"Deleted recurring reminder: {r['reminder_text']}")
                        log_interaction(phone_number, incoming_msg, f"Deleted recurring {r['id']}", "delete_recurring", True)
                    else:
                        resp = MessagingResponse()
                        resp.message("Couldn't delete that recurring reminder.")
                    # Clear the recurring context
                    create_or_update_user(phone_number, last_active_list=None)
                    return Response(content=str(resp), media_type="application/xml")
                else:
                    resp = MessagingResponse()
                    resp.message(f"Please enter a number between 1 and {len(recurring_list)}. Text 'MY RECURRING' to see the list.")
                    return Response(content=str(resp), media_type="application/xml")

            delete_options = []

            # Check for reminder at this position
            # Tuple format: (id, reminder_date, reminder_text, recurring_id, sent)
            reminders = get_user_reminders(phone_number)
            pending_reminders = [r for r in reminders if not r[4]]  # unsent only
            if pending_reminders and 1 <= item_num <= len(pending_reminders):
                reminder = pending_reminders[item_num - 1]
                recurring_id = reminder[3]
                # Show [R] prefix for recurring reminders
                display_prefix = "[R] " if recurring_id else ""
                delete_options.append({
                    'type': 'reminder',
                    'id': reminder[0],
                    'text': reminder[2],
                    'recurring_id': recurring_id,
                    'display': f"Reminder: {display_prefix}{reminder[2][:40]}"
                })

            # Check for list items at this position
            # If user has an active list, check that one; otherwise check all lists
            if last_active and last_active != "__RECURRING__":
                # Check specific active list
                list_info = get_list_by_name(phone_number, last_active)
                if list_info:
                    list_id = list_info[0]
                    list_name = list_info[1]
                    items = get_list_items(list_id)
                    if items and 1 <= item_num <= len(items):
                        item_id, item_text, _ = items[item_num - 1]
                        delete_options.append({
                            'type': 'list_item',
                            'list_name': list_name,
                            'text': item_text,
                            'display': f"'{item_text}' from {list_name} list"
                        })
            else:
                # No active list - check all lists for items at this position
                all_lists = get_lists(phone_number)
                for lst in all_lists:
                    list_id = lst[0]
                    list_name = lst[1]
                    items = get_list_items(list_id)
                    if items and 1 <= item_num <= len(items):
                        item_id, item_text, _ = items[item_num - 1]
                        delete_options.append({
                            'type': 'list_item',
                            'list_name': list_name,
                            'text': item_text,
                            'display': f"'{item_text}' from {list_name} list"
                        })

            # Check for memory at this position
            memories = get_memories(phone_number)
            if memories and 1 <= item_num <= len(memories):
                memory = memories[item_num - 1]
                memory_text = memory[1] if len(memory) > 1 else str(memory)
                delete_options.append({
                    'type': 'memory',
                    'id': memory[0] if len(memory) > 0 else None,
                    'text': memory_text[:40] if memory_text else "memory",
                    'display': f"Memory: {memory_text[:40] if memory_text else 'memory'}..."
                })

            if not delete_options:
                resp = MessagingResponse()
                resp.message(f"Nothing found at position #{item_num} to delete.")
                return Response(content=str(resp), media_type="application/xml")

            if len(delete_options) == 1:
                # Only one option - ask for confirmation
                opt = delete_options[0]
                create_or_update_user(phone_number, pending_reminder_delete=json.dumps(delete_options))
                resp = MessagingResponse()
                resp.message(f"Delete {opt['display']}?\n\nReply 1 to confirm or CANCEL to cancel.")
                log_interaction(phone_number, incoming_msg, "Asking delete confirmation", "delete_confirm", True)
                return Response(content=str(resp), media_type="application/xml")
            else:
                # Multiple options - show menu
                create_or_update_user(phone_number, pending_reminder_delete=json.dumps(delete_options))
                lines = ["What would you like to delete? Reply with a number:\n"]
                for i, opt in enumerate(delete_options, 1):
                    lines.append(f"{i}. {opt['display']}")
                lines.append("\nOr reply CANCEL to cancel.")
                resp = MessagingResponse()
                resp.message("\n".join(lines))
                log_interaction(phone_number, incoming_msg, "Showing delete options", "delete_options", True)
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # CHECK OFF ITEM BY NUMBER (context-aware)
        # ==========================================
        # Handle "Check 2", "Check off 3", "Done 1", etc. when user has a last active list
        check_match = re.match(r'^(?:check|check off|done|complete|finished)\s+(\d+)$', incoming_msg.strip(), re.IGNORECASE)
        if check_match:
            item_num = int(check_match.group(1))
            last_active = get_last_active_list(phone_number)
            if last_active:
                list_info = get_list_by_name(phone_number, last_active)
                if list_info:
                    list_id = list_info[0]
                    list_name = list_info[1]
                    items = get_list_items(list_id)
                    if items and 1 <= item_num <= len(items):
                        item_id, item_text, _ = items[item_num - 1]
                        if mark_item_complete(phone_number, list_name, item_text):
                            reply_msg = f"Checked off '{item_text}'"
                        else:
                            reply_msg = f"Couldn't check off item #{item_num}"
                        resp = MessagingResponse()
                        resp.message(reply_msg)
                        log_interaction(phone_number, incoming_msg, reply_msg, "complete_item", True)
                        return Response(content=str(resp), media_type="application/xml")
                    else:
                        resp = MessagingResponse()
                        resp.message(f"Item #{item_num} not found. Your {list_name} has {len(items)} items.")
                        return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # FEEDBACK HANDLING
        # ==========================================
        if incoming_msg.upper().startswith("FEEDBACK:"):
            feedback_message = incoming_msg[9:].strip()  # Extract everything after "feedback:"
            if feedback_message:
                # Save feedback to database
                from database import get_db_connection, return_db_connection
                conn = None
                try:
                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute(
                        'INSERT INTO feedback (user_phone, message) VALUES (%s, %s)',
                        (phone_number, feedback_message)
                    )
                    conn.commit()
                    resp = MessagingResponse()
                    resp.message("Thank you for your feedback! We appreciate you taking the time to share your thoughts with us.")
                    log_interaction(phone_number, incoming_msg, "Feedback received", "feedback", True)
                    return Response(content=str(resp), media_type="application/xml")
                except Exception as e:
                    logger.error(f"Error saving feedback: {e}")
                    resp = MessagingResponse()
                    resp.message("Sorry, there was an error saving your feedback. Please try again later.")
                    return Response(content=str(resp), media_type="application/xml")
                finally:
                    if conn:
                        return_db_connection(conn)
            else:
                resp = MessagingResponse()
                resp.message("Please include your feedback after 'Feedback:'. For example: 'Feedback: I love this app!'")
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # UPGRADE / SUBSCRIPTION HANDLING
        # ==========================================
        if incoming_msg.upper() in ["UPGRADE", "SUBSCRIBE", "PREMIUM", "FAMILY", "PRICING"]:
            from services.stripe_service import get_upgrade_message, get_user_subscription, create_checkout_session
            from config import STRIPE_ENABLED, APP_BASE_URL

            subscription = get_user_subscription(phone_number)

            # If already premium, show account management
            if subscription['tier'] != 'free' and subscription['status'] == 'active':
                resp = MessagingResponse()
                resp.message(f"You're already on the {subscription['tier'].title()} plan! Text ACCOUNT to manage your subscription.")
                log_interaction(phone_number, incoming_msg, "Already subscribed", "upgrade_already_premium", True)
                return Response(content=str(resp), media_type="application/xml")

            # Show upgrade options
            msg_upper = incoming_msg.upper()
            if msg_upper in ["PREMIUM", "FAMILY"] and STRIPE_ENABLED:
                # User selected a specific plan - create checkout link
                plan = 'premium' if msg_upper == "PREMIUM" else 'family'
                result = create_checkout_session(phone_number, plan, 'monthly')

                if 'url' in result:
                    resp = MessagingResponse()
                    resp.message(f"Great choice! Complete your {plan.title()} subscription here:\n\n{result['url']}\n\nThis link expires in 24 hours.")
                    log_interaction(phone_number, incoming_msg, f"Checkout link sent for {plan}", f"upgrade_{plan}_checkout", True)
                else:
                    resp = MessagingResponse()
                    resp.message(f"Visit {APP_BASE_URL}/upgrade to subscribe to {plan.title()}!")
                    log_interaction(phone_number, incoming_msg, "Checkout fallback", f"upgrade_{plan}_fallback", True)
                return Response(content=str(resp), media_type="application/xml")
            else:
                # Show pricing info
                upgrade_msg = get_upgrade_message(phone_number)
                resp = MessagingResponse()
                resp.message(upgrade_msg)
                log_interaction(phone_number, incoming_msg, "Upgrade info sent", "upgrade_info", True)
                return Response(content=str(resp), media_type="application/xml")

        if incoming_msg.upper() in ["ACCOUNT", "MANAGE", "BILLING", "SUBSCRIPTION"]:
            from services.stripe_service import get_user_subscription, create_customer_portal_session
            from config import STRIPE_ENABLED, APP_BASE_URL

            subscription = get_user_subscription(phone_number)

            if subscription['tier'] == 'free':
                resp = MessagingResponse()
                resp.message("You're on the free plan. Text UPGRADE to see premium options!")
                log_interaction(phone_number, incoming_msg, "Free user - no account", "account_free", True)
                return Response(content=str(resp), media_type="application/xml")

            if STRIPE_ENABLED:
                result = create_customer_portal_session(phone_number)
                if 'url' in result:
                    resp = MessagingResponse()
                    resp.message(f"Manage your {subscription['tier'].title()} subscription here:\n\n{result['url']}")
                    log_interaction(phone_number, incoming_msg, "Portal link sent", "account_portal", True)
                    return Response(content=str(resp), media_type="application/xml")

            resp = MessagingResponse()
            resp.message(f"You're on the {subscription['tier'].title()} plan. Visit {APP_BASE_URL}/account to manage your subscription.")
            log_interaction(phone_number, incoming_msg, "Account info sent", "account_info", True)
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # SUPPORT HANDLING (Premium users, or all users in beta mode)
        # ==========================================
        if incoming_msg.upper().startswith("SUPPORT:") or incoming_msg.upper().startswith("SUPPORT "):
            from services.support_service import is_premium_user, add_support_message
            from config import BETA_MODE

            # Check if user is premium (or beta mode allows all users)
            if not BETA_MODE and not is_premium_user(phone_number):
                resp = MessagingResponse()
                resp.message("Support chat is available for premium members. Use 'Feedback: [message]' to send us feedback, or upgrade to premium for direct support.")
                log_interaction(phone_number, incoming_msg, "Support denied - not premium", "support_denied", False)
                return Response(content=str(resp), media_type="application/xml")

            # Extract message after "support:" or "support "
            if incoming_msg.upper().startswith("SUPPORT:"):
                support_message = incoming_msg[8:].strip()
            else:
                support_message = incoming_msg[8:].strip()

            if support_message:
                result = add_support_message(phone_number, support_message, 'inbound')
                if result['success']:
                    resp = MessagingResponse()
                    resp.message(staging_prefix(f"[Support Ticket #{result['ticket_id']}] Your message has been sent to our support team. We'll reply as soon as possible!\n\n(You're now in support mode - replies go to support. Text EXIT to leave or CLOSE to close ticket)"))
                    log_interaction(phone_number, incoming_msg, f"Support ticket #{result['ticket_id']}", "support", True)
                    return Response(content=str(resp), media_type="application/xml")
                else:
                    resp = MessagingResponse()
                    resp.message("Sorry, there was an error sending your message. Please try again.")
                    return Response(content=str(resp), media_type="application/xml")
            else:
                resp = MessagingResponse()
                resp.message("Please include your message after 'Support:'. For example: 'Support: I need help with reminders'")
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # SNOOZE HANDLING
        # ==========================================
        if incoming_msg.upper().startswith("SNOOZE"):
            # Check if there's a recent reminder to snooze
            last_reminder = get_last_sent_reminder(phone_number, max_age_minutes=30)

            if last_reminder:
                # Parse snooze duration
                snooze_text = incoming_msg[6:].strip().lower()  # Everything after "snooze"
                snooze_minutes = parse_snooze_duration(snooze_text)

                # Create new reminder with snoozed time
                user_tz = get_user_timezone(phone_number)
                new_reminder_time = datetime.utcnow() + timedelta(minutes=snooze_minutes)

                # Save the new reminder
                save_reminder(phone_number, last_reminder['text'], new_reminder_time)

                # Mark original as snoozed
                mark_reminder_snoozed(last_reminder['id'])

                # Format confirmation message
                if snooze_minutes < 60:
                    time_str = f"{snooze_minutes} minute{'s' if snooze_minutes != 1 else ''}"
                else:
                    hours = snooze_minutes // 60
                    mins = snooze_minutes % 60
                    if mins > 0:
                        time_str = f"{hours} hour{'s' if hours != 1 else ''} and {mins} minute{'s' if mins != 1 else ''}"
                    else:
                        time_str = f"{hours} hour{'s' if hours != 1 else ''}"

                resp = MessagingResponse()
                resp.message(f"Snoozed! I'll remind you again in {time_str}.")
                log_interaction(phone_number, incoming_msg, f"Snoozed for {time_str}", "snooze", True)
                return Response(content=str(resp), media_type="application/xml")
            else:
                resp = MessagingResponse()
                resp.message("No recent reminder to snooze. You can only snooze within 30 minutes of receiving a reminder.")
                log_interaction(phone_number, incoming_msg, "No reminder to snooze", "snooze", False)
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # TIMEZONE COMMANDS
        # ==========================================
        msg_upper = incoming_msg.upper().strip()

        # Show current timezone
        if msg_upper in ["MY TIMEZONE", "TIMEZONE", "MY TZ", "SHOW TIMEZONE"]:
            user_tz = get_user_timezone(phone_number)
            user_time = get_user_current_time(phone_number)
            current_time_str = user_time.strftime('%I:%M %p on %A, %B %d').lstrip('0')

            resp = MessagingResponse()
            resp.message(f"Your timezone is set to: {user_tz}\n\nCurrent time: {current_time_str}\n\n(To change, text: TIMEZONE [city or timezone])")
            log_interaction(phone_number, incoming_msg, f"Timezone: {user_tz}", "my_timezone", True)
            return Response(content=str(resp), media_type="application/xml")

        # Update timezone
        if msg_upper.startswith("TIMEZONE ") or msg_upper.startswith("SET TIMEZONE "):
            # Extract timezone string
            if msg_upper.startswith("SET TIMEZONE "):
                tz_input = incoming_msg[13:].strip()
            else:
                tz_input = incoming_msg[9:].strip()

            # Try to parse the timezone
            from utils.timezone import parse_timezone_input

            new_tz = parse_timezone_input(tz_input)

            if new_tz:
                # Update user's timezone
                from models.user import update_user_timezone
                update_user_timezone(phone_number, new_tz)

                # Recalculate pending reminders
                updated_count = recalculate_pending_reminders_for_timezone(phone_number, new_tz)

                # Update recurring reminders timezone
                recurring_count = update_recurring_reminders_timezone(phone_number, new_tz)

                # Get new current time
                tz = pytz.timezone(new_tz)
                new_time = datetime.now(tz)
                current_time_str = new_time.strftime('%I:%M %p').lstrip('0')

                reply_parts = [f"Timezone updated to: {new_tz}"]
                reply_parts.append(f"Current time: {current_time_str}")

                if updated_count > 0:
                    reply_parts.append(f"\n{updated_count} pending reminder(s) adjusted to new timezone.")
                if recurring_count > 0:
                    reply_parts.append(f"{recurring_count} recurring reminder(s) updated.")

                resp = MessagingResponse()
                resp.message("\n".join(reply_parts))
                log_interaction(phone_number, incoming_msg, f"Timezone updated to {new_tz}", "timezone_update", True)
                return Response(content=str(resp), media_type="application/xml")
            else:
                resp = MessagingResponse()
                resp.message(f"Sorry, I couldn't recognize '{tz_input}' as a timezone.\n\nTry: Pacific, Eastern, Central, Mountain, or a city name like 'Los Angeles' or 'New York'.")
                log_interaction(phone_number, incoming_msg, f"Unrecognized timezone: {tz_input}", "timezone_update", False)
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # SHOW COMPLETED REMINDERS
        # ==========================================
        if msg_upper in ["SHOW COMPLETED REMINDERS", "SHOW COMPLETED", "COMPLETED REMINDERS", "PAST REMINDERS", "SHOW PAST REMINDERS"]:
            reminders = get_user_reminders(phone_number)
            # Filter to only sent/completed reminders
            completed = [r for r in reminders if r[4]]  # r[4] is sent flag

            if not completed:
                resp = MessagingResponse()
                resp.message("You don't have any completed reminders yet.")
                log_interaction(phone_number, incoming_msg, "No completed reminders", "show_completed", True)
                return Response(content=str(resp), media_type="application/xml")

            # Format completed reminders (show last 10)
            user_tz = get_user_timezone(phone_number)
            tz = pytz.timezone(user_tz)
            lines = ["COMPLETED REMINDERS:\n"]

            for i, reminder in enumerate(completed[-10:], 1):
                reminder_id, reminder_date_utc, reminder_text, recurring_id, sent = reminder
                try:
                    if isinstance(reminder_date_utc, datetime):
                        utc_dt = reminder_date_utc
                        if utc_dt.tzinfo is None:
                            utc_dt = pytz.UTC.localize(utc_dt)
                    else:
                        utc_dt = datetime.strptime(str(reminder_date_utc), '%Y-%m-%d %H:%M:%S')
                        utc_dt = pytz.UTC.localize(utc_dt)
                    user_dt = utc_dt.astimezone(tz)
                    date_str = user_dt.strftime('%a, %b %d at %I:%M %p')
                except:
                    date_str = ""

                display_text = f"[R] {reminder_text}" if recurring_id else reminder_text
                lines.append(f"{i}. {display_text}")
                if date_str:
                    lines.append(f"   {date_str}")
                lines.append("")

            resp = MessagingResponse()
            resp.message("\n".join(lines).strip())
            log_interaction(phone_number, incoming_msg, f"Listed {len(completed[-10:])} completed reminders", "show_completed", True)
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # RECURRING REMINDER COMMANDS
        # ==========================================

        # Show all recurring reminders
        if msg_upper in ["MY RECURRING", "MY RECURRING REMINDERS", "RECURRING", "RECURRING REMINDERS", "SHOW RECURRING"]:
            recurring_list = get_recurring_reminders(phone_number, include_inactive=True)

            if not recurring_list:
                resp = MessagingResponse()
                resp.message("You don't have any recurring reminders set up yet.\n\nTry: 'Remind me every day at 7pm to take medicine'")
                log_interaction(phone_number, incoming_msg, "No recurring reminders", "my_recurring", True)
                return Response(content=str(resp), media_type="application/xml")

            # Format list
            user_tz_str = get_user_timezone(phone_number)
            tz = pytz.timezone(user_tz_str)
            lines = ["Your recurring reminders:\n"]

            for i, r in enumerate(recurring_list, 1):
                # Parse time for display
                time_parts = r['reminder_time'].split(':')
                hour = int(time_parts[0])
                minute = int(time_parts[1].split(':')[0]) if ':' in time_parts[1] else int(time_parts[1])
                display_time = datetime(2000, 1, 1, hour, minute).strftime('%I:%M %p').lstrip('0')

                # Format pattern
                if r['recurrence_type'] == 'daily':
                    pattern = "Every day"
                elif r['recurrence_type'] == 'weekly':
                    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
                    pattern = f"Every {days[r['recurrence_day']]}"
                elif r['recurrence_type'] == 'weekdays':
                    pattern = "Weekdays"
                elif r['recurrence_type'] == 'weekends':
                    pattern = "Weekends"
                elif r['recurrence_type'] == 'monthly':
                    suffix = 'th'
                    if r['recurrence_day'] in [1, 21, 31]:
                        suffix = 'st'
                    elif r['recurrence_day'] in [2, 22]:
                        suffix = 'nd'
                    elif r['recurrence_day'] in [3, 23]:
                        suffix = 'rd'
                    pattern = f"Monthly on the {r['recurrence_day']}{suffix}"

                status = ""
                if not r['active']:
                    status = " (PAUSED)"

                lines.append(f"{i}. {r['reminder_text']}")
                lines.append(f"   {pattern} at {display_time}{status}")

                # Show next occurrence if available and active
                if r['active'] and r['next_occurrence']:
                    try:
                        next_dt = datetime.fromisoformat(r['next_occurrence'].replace('Z', '+00:00'))
                        if next_dt.tzinfo is None:
                            next_dt = pytz.UTC.localize(next_dt)
                        next_local = next_dt.astimezone(tz)
                        next_str = next_local.strftime('%a, %b %d at %I:%M %p').replace(' 0', ' ')
                        lines.append(f"   Next: {next_str}")
                    except:
                        pass

                lines.append("")  # Blank line between entries

            lines.append("(Reply with number to delete, or 'PAUSE [#]' to pause)")

            # Set context so "Delete #" knows we're in recurring mode
            create_or_update_user(phone_number, last_active_list="__RECURRING__")

            resp = MessagingResponse()
            resp.message("\n".join(lines))
            log_interaction(phone_number, incoming_msg, f"Listed {len(recurring_list)} recurring reminders", "my_recurring", True)
            return Response(content=str(resp), media_type="application/xml")

        # Delete recurring reminder (avoid "STOP" prefix - conflicts with carrier opt-out)
        if msg_upper.startswith("DELETE RECURRING ") or msg_upper.startswith("CANCEL RECURRING ") or msg_upper.startswith("REMOVE RECURRING "):
            # Extract number
            parts = incoming_msg.split()
            if len(parts) >= 3:
                try:
                    num = int(parts[2])
                    recurring_list = get_recurring_reminders(phone_number, include_inactive=True)

                    if num < 1 or num > len(recurring_list):
                        resp = MessagingResponse()
                        resp.message(f"Please enter a number between 1 and {len(recurring_list)}.")
                        return Response(content=str(resp), media_type="application/xml")

                    recurring = recurring_list[num - 1]
                    if delete_recurring_reminder(recurring['id'], phone_number):
                        resp = MessagingResponse()
                        resp.message(f"Deleted recurring reminder: {recurring['reminder_text']}\n\nYou won't receive any more reminders for this.")
                        log_interaction(phone_number, incoming_msg, f"Deleted recurring {recurring['id']}", "stop_recurring", True)
                    else:
                        resp = MessagingResponse()
                        resp.message("Couldn't delete that recurring reminder.")
                        log_interaction(phone_number, incoming_msg, "Delete failed", "stop_recurring", False)
                    return Response(content=str(resp), media_type="application/xml")
                except ValueError:
                    pass

            resp = MessagingResponse()
            resp.message("Please specify which recurring reminder to delete.\n\nText 'MY RECURRING' to see the list, then 'DELETE RECURRING [number]'.")
            return Response(content=str(resp), media_type="application/xml")

        # Pause recurring reminder
        if msg_upper.startswith("PAUSE RECURRING "):
            parts = incoming_msg.split()
            if len(parts) >= 3:
                try:
                    num = int(parts[2])
                    recurring_list = get_recurring_reminders(phone_number, include_inactive=True)

                    if num < 1 or num > len(recurring_list):
                        resp = MessagingResponse()
                        resp.message(f"Please enter a number between 1 and {len(recurring_list)}.")
                        return Response(content=str(resp), media_type="application/xml")

                    recurring = recurring_list[num - 1]
                    if not recurring['active']:
                        resp = MessagingResponse()
                        resp.message(f"That reminder is already paused. Text 'RESUME RECURRING {num}' to resume it.")
                        return Response(content=str(resp), media_type="application/xml")

                    if pause_recurring_reminder(recurring['id'], phone_number):
                        resp = MessagingResponse()
                        resp.message(f"Paused: {recurring['reminder_text']}\n\nText 'RESUME RECURRING {num}' when you want to restart it.")
                        log_interaction(phone_number, incoming_msg, f"Paused recurring {recurring['id']}", "pause_recurring", True)
                    else:
                        resp = MessagingResponse()
                        resp.message("Couldn't pause that recurring reminder.")
                    return Response(content=str(resp), media_type="application/xml")
                except ValueError:
                    pass

            resp = MessagingResponse()
            resp.message("Please specify which recurring reminder to pause.\n\nText 'MY RECURRING' to see the list.")
            return Response(content=str(resp), media_type="application/xml")

        # Resume recurring reminder
        if msg_upper.startswith("RESUME RECURRING "):
            parts = incoming_msg.split()
            if len(parts) >= 3:
                try:
                    num = int(parts[2])
                    recurring_list = get_recurring_reminders(phone_number, include_inactive=True)

                    if num < 1 or num > len(recurring_list):
                        resp = MessagingResponse()
                        resp.message(f"Please enter a number between 1 and {len(recurring_list)}.")
                        return Response(content=str(resp), media_type="application/xml")

                    recurring = recurring_list[num - 1]
                    if recurring['active']:
                        resp = MessagingResponse()
                        resp.message(f"That reminder is already active!")
                        return Response(content=str(resp), media_type="application/xml")

                    if resume_recurring_reminder(recurring['id'], phone_number):
                        # Generate next occurrence
                        from tasks.reminder_tasks import generate_first_occurrence
                        generate_first_occurrence(recurring['id'])

                        resp = MessagingResponse()
                        resp.message(f"Resumed: {recurring['reminder_text']}\n\nYou'll start receiving reminders again.")
                        log_interaction(phone_number, incoming_msg, f"Resumed recurring {recurring['id']}", "resume_recurring", True)
                    else:
                        resp = MessagingResponse()
                        resp.message("Couldn't resume that recurring reminder.")
                    return Response(content=str(resp), media_type="application/xml")
                except ValueError:
                    pass

            resp = MessagingResponse()
            resp.message("Please specify which recurring reminder to resume.\n\nText 'MY RECURRING' to see the list.")
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # DELETE ALL COMMANDS (separated by type)
        # ==========================================

        # Delete all memories only
        if msg_upper in ["DELETE ALL MEMORIES", "DELETE ALL MY MEMORIES", "FORGET ALL MEMORIES", "FORGET ALL MY MEMORIES"]:
            resp = MessagingResponse()
            resp.message("⚠️ WARNING: This will permanently delete ALL your memories.\n\nReply YES to confirm or anything else to cancel.")
            create_or_update_user(phone_number, pending_delete=True, pending_list_item="__DELETE_ALL_MEMORIES__")
            log_interaction(phone_number, incoming_msg, "Asking for delete memories confirmation", "delete_memories_request", True)
            return Response(content=str(resp), media_type="application/xml")

        # Delete all reminders only
        if msg_upper in ["DELETE ALL REMINDERS", "DELETE ALL MY REMINDERS", "CANCEL ALL REMINDERS", "CANCEL ALL MY REMINDERS"]:
            resp = MessagingResponse()
            resp.message("⚠️ WARNING: This will permanently delete ALL your reminders.\n\nReply YES to confirm or anything else to cancel.")
            create_or_update_user(phone_number, pending_delete=True, pending_list_item="__DELETE_ALL_REMINDERS__")
            log_interaction(phone_number, incoming_msg, "Asking for delete reminders confirmation", "delete_reminders_request", True)
            return Response(content=str(resp), media_type="application/xml")

        # Delete all lists only
        if msg_upper in ["DELETE ALL LISTS", "DELETE ALL MY LISTS", "FORGET ALL LISTS", "FORGET ALL MY LISTS"]:
            resp = MessagingResponse()
            resp.message("⚠️ WARNING: This will permanently delete ALL your lists and their items.\n\nReply YES to confirm or anything else to cancel.")
            create_or_update_user(phone_number, pending_delete=True, pending_list_item="__DELETE_ALL_LISTS__")
            log_interaction(phone_number, incoming_msg, "Asking for delete lists confirmation", "delete_lists_request", True)
            return Response(content=str(resp), media_type="application/xml")

        # Delete all - show options menu
        if msg_upper == "DELETE ALL":
            resp = MessagingResponse()
            resp.message("What would you like to delete?\n\n• DELETE ALL MEMORIES\n\n• DELETE ALL REMINDERS\n\n• DELETE ALL LISTS\n\n• DELETE ALL DATA (deletes everything)\n\nText one of the above to continue.")
            log_interaction(phone_number, incoming_msg, "Showing delete options", "delete_all_options", True)
            return Response(content=str(resp), media_type="application/xml")

        # Delete everything (all data) - requires explicit "DELETE ALL DATA"
        if msg_upper in ["DELETE ALL DATA", "DELETE ALL MY DATA", "DELETE EVERYTHING", "FORGET EVERYTHING"]:
            resp = MessagingResponse()
            resp.message("⚠️ WARNING: This will permanently delete ALL your data (memories, reminders, and lists).\n\nReply YES to confirm or anything else to cancel.")
            create_or_update_user(phone_number, pending_delete=True, pending_list_item="__DELETE_ALL_DATA__")
            log_interaction(phone_number, incoming_msg, "Asking for delete all data confirmation", "delete_all_request", True)
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # HANDLE CONFIRMATION RESPONSES
        # ==========================================
        if incoming_msg.upper() == "YES":
            user = get_user(phone_number)
            if user and user[9]:  # pending_delete flag
                pending_action = get_pending_list_item(phone_number)
                logger.info(f"Delete confirmation: pending_action={pending_action}")

                from database import get_db_connection, return_db_connection

                # Handle bulk deletion types
                if pending_action == "__DELETE_ALL_MEMORIES__":
                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute('DELETE FROM memories WHERE phone_number = %s', (phone_number,))
                    conn.commit()
                    return_db_connection(conn)
                    create_or_update_user(phone_number, pending_delete=False, pending_list_item=None)
                    resp = MessagingResponse()
                    resp.message("All your memories have been permanently deleted.")
                    log_interaction(phone_number, incoming_msg, "All memories deleted", "delete_memories_confirmed", True)
                    return Response(content=str(resp), media_type="application/xml")

                elif pending_action == "__DELETE_ALL_REMINDERS__":
                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute('DELETE FROM reminders WHERE phone_number = %s', (phone_number,))
                    conn.commit()
                    return_db_connection(conn)
                    create_or_update_user(phone_number, pending_delete=False, pending_list_item=None)
                    resp = MessagingResponse()
                    resp.message("All your reminders have been permanently deleted.")
                    log_interaction(phone_number, incoming_msg, "All reminders deleted", "delete_reminders_confirmed", True)
                    return Response(content=str(resp), media_type="application/xml")

                elif pending_action == "__DELETE_ALL_LISTS__":
                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute('DELETE FROM lists WHERE phone_number = %s', (phone_number,))
                    conn.commit()
                    return_db_connection(conn)
                    create_or_update_user(phone_number, pending_delete=False, pending_list_item=None)
                    resp = MessagingResponse()
                    resp.message("All your lists have been permanently deleted.")
                    log_interaction(phone_number, incoming_msg, "All lists deleted", "delete_lists_confirmed", True)
                    return Response(content=str(resp), media_type="application/xml")

                elif pending_action == "__DELETE_ALL_DATA__":
                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute('DELETE FROM memories WHERE phone_number = %s', (phone_number,))
                    c.execute('DELETE FROM reminders WHERE phone_number = %s', (phone_number,))
                    c.execute('DELETE FROM lists WHERE phone_number = %s', (phone_number,))
                    conn.commit()
                    return_db_connection(conn)
                    create_or_update_user(phone_number, pending_delete=False, pending_list_item=None)
                    resp = MessagingResponse()
                    resp.message("All your data (memories, reminders, and lists) has been permanently deleted.")
                    log_interaction(phone_number, incoming_msg, "All data deleted", "delete_all_confirmed", True)
                    return Response(content=str(resp), media_type="application/xml")

                elif pending_action:
                    # Delete specific list (original behavior)
                    logger.info(f"Attempting to delete list: {pending_action}")
                    delete_result = delete_list(phone_number, pending_action)
                    logger.info(f"Delete result: {delete_result}")
                    if delete_result:
                        reply_msg = f"Deleted your {pending_action} and all its items."
                    else:
                        reply_msg = "Couldn't delete that list."
                    create_or_update_user(phone_number, pending_delete=False, pending_list_item=None)
                    resp = MessagingResponse()
                    resp.message(reply_msg)
                    log_interaction(phone_number, incoming_msg, reply_msg, "delete_list_confirmed", True)
                    return Response(content=str(resp), media_type="application/xml")

        if incoming_msg.upper() in ["NO", "CANCEL"]:
            user = get_user(phone_number)
            if user and user[9]:  # pending_delete flag
                create_or_update_user(phone_number, pending_delete=False, pending_list_item=None)
                resp = MessagingResponse()
                resp.message("Cancelled. Your data is safe!")
                log_interaction(phone_number, incoming_msg, "Delete cancelled", "delete_cancelled", True)
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # LIST ALL COMMAND
        # ==========================================
        if incoming_msg.upper() in ["LIST ALL", "LIST MEMORIES", "SHOW MEMORIES", "MY MEMORIES"]:
            # Tuple format: (id, memory_text, parsed_data, created_at)
            memories = get_memories(phone_number)
            if memories:
                memory_list = "\n\n".join([f"{i+1}. {m[1]}" for i, m in enumerate(memories[:20])])
                reply = f"Your stored memories:\n\n{memory_list}"
            else:
                reply = "You don't have any memories stored yet."

            resp = MessagingResponse()
            resp.message(reply)
            log_interaction(phone_number, incoming_msg, reply, "list", True)
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # LIST COMMANDS (MY LISTS, SHOW LISTS)
        # ==========================================
        if incoming_msg.upper() in ["MY LISTS", "SHOW LISTS", "LIST LISTS", "LISTS"]:
            # Clear any pending list item state so number responses show lists, not add items
            create_or_update_user(phone_number, pending_list_item=None, pending_delete=False)
            lists = get_lists(phone_number)
            if len(lists) == 1:
                # Only one list, show it directly
                list_id = lists[0][0]
                list_name = lists[0][1]
                create_or_update_user(phone_number, last_active_list=list_name)
                items = get_list_items(list_id)
                if items:
                    item_lines = []
                    for i, (item_id, item_text, completed) in enumerate(items, 1):
                        if completed:
                            item_lines.append(f"{i}. [x] {item_text}")
                        else:
                            item_lines.append(f"{i}. {item_text}")
                    reply = f"{list_name}:\n\n" + "\n".join(item_lines)
                else:
                    reply = f"Your {list_name} is empty."
            elif lists:
                list_lines = []
                for i, (list_id, list_name, item_count, completed_count) in enumerate(lists, 1):
                    list_lines.append(f"{i}. {list_name} ({item_count} items)")
                reply = "Your lists:\n\n" + "\n".join(list_lines) + "\n\nReply with a number to see that list."
            else:
                reply = "You don't have any lists yet. Try saying 'Create a grocery list'!"

            resp = MessagingResponse()
            resp.message(reply)
            log_interaction(phone_number, incoming_msg, reply, "show_lists", True)
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # NUMBER RESPONSE TO SHOW LIST
        # ==========================================
        # If user sends just a number and has lists (but no pending item), show that list
        if incoming_msg.strip().isdigit() and not get_pending_list_item(phone_number):
            list_num = int(incoming_msg.strip())
            lists = get_lists(phone_number)
            if lists and 1 <= list_num <= len(lists):
                selected_list = lists[list_num - 1]
                list_id = selected_list[0]
                list_name = selected_list[1]
                items = get_list_items(list_id)
                if items:
                    item_lines = []
                    for i, (item_id, item_text, completed) in enumerate(items, 1):
                        if completed:
                            item_lines.append(f"{i}. [x] {item_text}")
                        else:
                            item_lines.append(f"{i}. {item_text}")
                    reply = f"{list_name}:\n\n" + "\n".join(item_lines)
                else:
                    reply = f"Your {list_name} is empty."

                resp = MessagingResponse()
                resp.message(reply)
                log_interaction(phone_number, incoming_msg, reply, "show_list", True)
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # INFO COMMAND (Help Guide)
        # ==========================================
        if incoming_msg.upper() in ["INFO", "GUIDE", "COMMANDS", "?"]:
            resp = MessagingResponse()
            resp.message(get_help_text())
            log_interaction(phone_number, incoming_msg, "Help guide sent", "help_command", True)
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # PROCESS WITH AI
        # ==========================================
        # Normalize AM/PM variations before sending to AI
        normalized_msg = incoming_msg
        normalized_msg = re.sub(r'\b(\d+):?(\d*)\s*(a\.?m\.?)\b', r'\1:\2AM', normalized_msg, flags=re.IGNORECASE)
        normalized_msg = re.sub(r'\b(\d+):?(\d*)\s*(p\.?m\.?)\b', r'\1:\2PM', normalized_msg, flags=re.IGNORECASE)

        # Check for sensitive data BEFORE sending to AI
        sensitive_check = detect_sensitive_data(incoming_msg)
        if sensitive_check['has_sensitive']:
            log_security_event('SENSITIVE_DATA_BLOCKED', {
                'phone': phone_number,
                'action': 'pre_ai_check',
                'types': sensitive_check['types']
            })
            reply_text = get_sensitive_data_warning()
            log_interaction(phone_number, incoming_msg, reply_text, "sensitive_blocked", False)
            resp = MessagingResponse()
            resp.message(staging_prefix(reply_text))
            return Response(content=str(resp), media_type="application/xml")

        ai_response = process_with_ai(normalized_msg, phone_number, None)
        logger.info(f"AI response: {ai_response}")

        # Check for multi-command response (handle both formats: action="multiple" or multiple=true)
        if (ai_response.get("action") == "multiple" or ai_response.get("multiple")) and isinstance(ai_response.get("actions"), list):
            actions_to_process = ai_response["actions"]
            logger.info(f"Processing {len(actions_to_process)} actions")
        else:
            actions_to_process = [ai_response]

        # Process each action and collect replies
        all_replies = []
        for action_index, current_action in enumerate(actions_to_process):
            action_type = current_action.get("action", "error")
            logger.info(f"Processing action {action_index + 1}/{len(actions_to_process)}: {action_type}")

            # Handle each action and get reply
            reply_text = process_single_action(current_action, phone_number, incoming_msg)
            if reply_text:
                all_replies.append(reply_text)

        # Combine all replies
        if len(all_replies) > 1:
            reply_text = "\n\n".join(all_replies)
        elif len(all_replies) == 1:
            reply_text = all_replies[0]
        else:
            reply_text = "I processed your request."

        # Send response
        resp = MessagingResponse()
        resp.message(staging_prefix(reply_text))
        return Response(content=str(resp), media_type="application/xml")

    except HTTPException:
        # Re-raise HTTPException (e.g., for staging fallback 503)
        raise
    except Exception as e:
        logger.error(f"❌ CRITICAL ERROR in webhook: {e}", exc_info=True)
        resp = MessagingResponse()
        resp.message(staging_prefix("Sorry, something went wrong. Please try again in a moment."))
        return Response(content=str(resp), media_type="application/xml")


def process_single_action(ai_response, phone_number, incoming_msg):
    """Process a single AI action and return the reply text"""
    try:
        # Handle AI response based on action
        if ai_response["action"] == "store":
            # Use memory_text if AI provided date-converted version, otherwise use original
            memory_text = ai_response.get("memory_text", incoming_msg)

            # Check for sensitive data (staging only)
            if ENVIRONMENT == "staging":
                sensitive_check = detect_sensitive_data(memory_text)
                if sensitive_check['has_sensitive']:
                    log_security_event('SENSITIVE_DATA_BLOCKED', {
                        'phone': phone_number,
                        'action': 'store',
                        'types': sensitive_check['types']
                    })
                    reply_text = get_sensitive_data_warning()
                    log_interaction(phone_number, incoming_msg, reply_text, "store_blocked", False)
                    return reply_text

            # Check tier limit for memories
            from services.tier_service import can_save_memory
            allowed, limit_msg = can_save_memory(phone_number)
            if not allowed:
                reply_text = limit_msg
                log_interaction(phone_number, incoming_msg, reply_text, "memory_limit_reached", False)
                return reply_text

            save_memory(phone_number, memory_text, ai_response)
            reply_text = ai_response.get("confirmation", "Got it! I'll remember that.")
            log_interaction(phone_number, incoming_msg, reply_text, "store", True)

        elif ai_response["action"] == "retrieve":
            reply_text = ai_response.get("response", "I don't have that information stored yet.")
            log_interaction(phone_number, incoming_msg, reply_text, "retrieve", True)

        elif ai_response["action"] == "list_reminders":
            reminders = get_user_reminders(phone_number)
            user_tz = get_user_timezone(phone_number)
            reply_text = format_reminders_list(reminders, user_tz)
            log_interaction(phone_number, incoming_msg, reply_text, "list_reminders", True)

        elif ai_response["action"] == "show_help":
            reply_text = get_help_text()
            log_interaction(phone_number, incoming_msg, "Help guide sent", "help_ai", True)

        elif ai_response["action"] == "clarify_time":
            reminder_text = ai_response.get("reminder_text")
            time_mentioned = ai_response.get("time_mentioned")

            create_or_update_user(
                phone_number, 
                pending_reminder_text=reminder_text,
                pending_reminder_time=time_mentioned
            )

            reply_text = ai_response.get("response", f"Do you mean {time_mentioned} AM or PM?")
            log_interaction(phone_number, incoming_msg, reply_text, "clarify_time", True)

        elif ai_response["action"] == "reminder":
            reminder_date = ai_response.get("reminder_date")
            reminder_text = ai_response.get("reminder_text")

            # Check for sensitive data (staging only)
            if ENVIRONMENT == "staging":
                sensitive_check = detect_sensitive_data(reminder_text)
                if sensitive_check['has_sensitive']:
                    log_security_event('SENSITIVE_DATA_BLOCKED', {
                        'phone': phone_number,
                        'action': 'reminder',
                        'types': sensitive_check['types']
                    })
                    reply_text = get_sensitive_data_warning()
                    log_interaction(phone_number, incoming_msg, reply_text, "reminder_blocked", False)
                    return reply_text

            # Check tier limit for reminders
            from services.tier_service import can_create_reminder
            allowed, limit_msg = can_create_reminder(phone_number)
            if not allowed:
                reply_text = limit_msg
                log_interaction(phone_number, incoming_msg, reply_text, "reminder_limit_reached", False)
                return reply_text

            try:
                user_tz_str = get_user_timezone(phone_number)
                tz = pytz.timezone(user_tz_str)

                naive_dt = datetime.strptime(reminder_date, '%Y-%m-%d %H:%M:%S')
                aware_dt = tz.localize(naive_dt)

                utc_dt = aware_dt.astimezone(pytz.UTC)
                reminder_date_utc = utc_dt.strftime('%Y-%m-%d %H:%M:%S')

                # Extract local time for timezone recalculation support
                local_time_str = naive_dt.strftime('%H:%M')

                # Use save_reminder_with_local_time for timezone recalculation support
                save_reminder_with_local_time(
                    phone_number, reminder_text, reminder_date_utc,
                    local_time_str, user_tz_str
                )
            except Exception as e:
                logger.error(f"Error converting reminder time to UTC: {e}")
                save_reminder(phone_number, reminder_text, reminder_date)

            reply_text = ai_response.get("confirmation", "Got it! I'll remind you.")
            log_interaction(phone_number, incoming_msg, reply_text, "reminder", True)

        elif ai_response["action"] == "reminder_relative":
            # Handle relative time reminders (e.g., "in 30 minutes", "in 5 months")
            # Server calculates the actual time to avoid AI arithmetic errors
            from dateutil.relativedelta import relativedelta

            reminder_text = ai_response.get("reminder_text", "your reminder")

            # Check for sensitive data (staging only)
            if ENVIRONMENT == "staging":
                sensitive_check = detect_sensitive_data(reminder_text)
                if sensitive_check['has_sensitive']:
                    log_security_event('SENSITIVE_DATA_BLOCKED', {
                        'phone': phone_number,
                        'action': 'reminder_relative',
                        'types': sensitive_check['types']
                    })
                    reply_text = get_sensitive_data_warning()
                    log_interaction(phone_number, incoming_msg, reply_text, "reminder_blocked", False)
                    return reply_text

            # Check tier limit for reminders
            from services.tier_service import can_create_reminder
            allowed, limit_msg = can_create_reminder(phone_number)
            if not allowed:
                reply_text = limit_msg
                log_interaction(phone_number, incoming_msg, reply_text, "reminder_limit_reached", False)
                return reply_text

            try:
                # Helper to parse numeric value from AI response
                def parse_offset(raw_value, default=0):
                    if raw_value is None:
                        return default
                    if isinstance(raw_value, (int, float)):
                        return int(raw_value)
                    match = re.search(r'(\d+)', str(raw_value))
                    return int(match.group(1)) if match else default

                # Get all possible offset types
                offset_minutes = parse_offset(ai_response.get("offset_minutes"))
                offset_days = parse_offset(ai_response.get("offset_days"))
                offset_weeks = parse_offset(ai_response.get("offset_weeks"))
                offset_months = parse_offset(ai_response.get("offset_months"))

                logger.info(f"reminder_relative: minutes={offset_minutes}, days={offset_days}, weeks={offset_weeks}, months={offset_months}, reminder_text={reminder_text}")

                # Validate at least one offset is provided
                if offset_minutes == 0 and offset_days == 0 and offset_weeks == 0 and offset_months == 0:
                    # Default to 15 minutes if nothing specified
                    offset_minutes = 15

                # Max limits
                MAX_MONTHS = 24  # 2 years
                MAX_WEEKS = 104  # 2 years
                MAX_DAYS = 730   # 2 years
                MAX_MINUTES = 1051200  # 2 years

                # Check limits
                if offset_months > MAX_MONTHS or offset_weeks > MAX_WEEKS or offset_days > MAX_DAYS or offset_minutes > MAX_MINUTES:
                    reply_text = "I can only set reminders up to 2 years in advance. Please try a shorter timeframe."
                    log_interaction(phone_number, incoming_msg, reply_text, "reminder_exceeded_limit", False)
                    return reply_text

                # Calculate reminder time from current UTC
                # Use relativedelta for months (handles variable month lengths correctly)
                reminder_dt_utc = datetime.utcnow() + relativedelta(
                    months=offset_months,
                    weeks=offset_weeks,
                    days=offset_days,
                    minutes=offset_minutes
                )
                reminder_date_utc = reminder_dt_utc.strftime('%Y-%m-%d %H:%M:%S')

                # Save the reminder
                save_reminder(phone_number, reminder_text, reminder_date_utc)

                # Generate confirmation in user's timezone
                user_tz_str = get_user_timezone(phone_number)
                tz = pytz.timezone(user_tz_str)
                reminder_dt_local = pytz.UTC.localize(reminder_dt_utc).astimezone(tz)

                # Format the time nicely
                time_str = reminder_dt_local.strftime('%I:%M %p').lstrip('0')
                date_str = reminder_dt_local.strftime('%A, %B %d, %Y')

                reply_text = f"Got it! I'll remind you on {date_str} at {time_str} to {reminder_text}."
                log_interaction(phone_number, incoming_msg, reply_text, "reminder_relative", True)

            except Exception as e:
                logger.error(f"Error setting relative reminder: {e}, ai_response={ai_response}")
                reply_text = "Sorry, I couldn't set that reminder. Please try again."
                log_interaction(phone_number, incoming_msg, reply_text, "reminder_relative", False)

        elif ai_response["action"] == "reminder_recurring":
            # Handle recurring reminders (e.g., "every day at 7pm", "every Sunday at 6pm")
            reminder_text = ai_response.get("reminder_text", "your reminder")
            recurrence_type = ai_response.get("recurrence_type")
            recurrence_day = ai_response.get("recurrence_day")
            time_str = ai_response.get("time")  # HH:MM format

            # Check for sensitive data (staging only)
            if ENVIRONMENT == "staging":
                sensitive_check = detect_sensitive_data(reminder_text)
                if sensitive_check['has_sensitive']:
                    log_security_event('SENSITIVE_DATA_BLOCKED', {
                        'phone': phone_number,
                        'action': 'reminder_recurring',
                        'types': sensitive_check['types']
                    })
                    reply_text = get_sensitive_data_warning()
                    log_interaction(phone_number, incoming_msg, reply_text, "reminder_blocked", False)
                    return reply_text

            # Check tier limit for recurring reminders (premium feature)
            from services.tier_service import can_create_recurring_reminder
            allowed, limit_msg = can_create_recurring_reminder(phone_number)
            if not allowed:
                reply_text = limit_msg
                log_interaction(phone_number, incoming_msg, reply_text, "recurring_not_allowed", False)
                return reply_text

            try:
                # Validate required fields
                if not recurrence_type or not time_str:
                    reply_text = "Sorry, I couldn't understand that recurring reminder. Please try again with a specific pattern like 'every day at 7pm'."
                    log_interaction(phone_number, incoming_msg, reply_text, "reminder_recurring", False)
                    return reply_text

                # Validate recurrence_type
                valid_types = ['daily', 'weekly', 'weekdays', 'weekends', 'monthly']
                if recurrence_type not in valid_types:
                    reply_text = f"Sorry, I don't recognize that recurrence pattern. Try: daily, weekly, weekdays, weekends, or monthly."
                    log_interaction(phone_number, incoming_msg, reply_text, "reminder_recurring", False)
                    return reply_text

                # For weekly and monthly, validate recurrence_day
                if recurrence_type == 'weekly' and recurrence_day is None:
                    reply_text = "Please specify which day of the week (e.g., 'every Sunday at 6pm')."
                    log_interaction(phone_number, incoming_msg, reply_text, "reminder_recurring", False)
                    return reply_text

                if recurrence_type == 'monthly' and recurrence_day is None:
                    reply_text = "Please specify which day of the month (e.g., 'every 1st at noon')."
                    log_interaction(phone_number, incoming_msg, reply_text, "reminder_recurring", False)
                    return reply_text

                # Get user's timezone
                user_tz_str = get_user_timezone(phone_number)

                # Save the recurring reminder
                recurring_id = save_recurring_reminder(
                    phone_number=phone_number,
                    reminder_text=reminder_text,
                    recurrence_type=recurrence_type,
                    recurrence_day=recurrence_day,
                    reminder_time=time_str,
                    timezone=user_tz_str
                )

                if not recurring_id:
                    reply_text = "Sorry, I couldn't save that recurring reminder. Please try again."
                    log_interaction(phone_number, incoming_msg, reply_text, "reminder_recurring", False)
                    return reply_text

                # Generate the first occurrence immediately
                from tasks.reminder_tasks import generate_first_occurrence
                next_occurrence = generate_first_occurrence(recurring_id)

                # Format confirmation message
                # Parse time for display
                hour, minute = map(int, time_str.split(':'))
                display_time = datetime(2000, 1, 1, hour, minute).strftime('%I:%M %p').lstrip('0')

                # Format recurrence pattern for display
                if recurrence_type == 'daily':
                    pattern_text = "every day"
                elif recurrence_type == 'weekly':
                    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
                    pattern_text = f"every {days[recurrence_day]}"
                elif recurrence_type == 'weekdays':
                    pattern_text = "every weekday"
                elif recurrence_type == 'weekends':
                    pattern_text = "every weekend"
                elif recurrence_type == 'monthly':
                    # Add ordinal suffix
                    suffix = 'th'
                    if recurrence_day in [1, 21, 31]:
                        suffix = 'st'
                    elif recurrence_day in [2, 22]:
                        suffix = 'nd'
                    elif recurrence_day in [3, 23]:
                        suffix = 'rd'
                    pattern_text = f"on the {recurrence_day}{suffix} of every month"

                reply_text = f"Got it! I'll remind you {pattern_text} at {display_time} to {reminder_text}.\n\n"

                if next_occurrence:
                    # Format next occurrence
                    tz = pytz.timezone(user_tz_str)
                    if isinstance(next_occurrence, str):
                        next_dt = datetime.fromisoformat(next_occurrence.replace('Z', '+00:00'))
                    else:
                        next_dt = next_occurrence
                    if next_dt.tzinfo is None:
                        next_dt = pytz.UTC.localize(next_dt)
                    next_local = next_dt.astimezone(tz)
                    next_str = next_local.strftime('%A, %B %d at %I:%M %p').replace(' 0', ' ')
                    reply_text += f"Next reminder: {next_str}\n\n"

                reply_text += "(Text 'SHOW RECURRING' to see all, 'DELETE RECURRING [#]' to remove)"

                log_interaction(phone_number, incoming_msg, reply_text, "reminder_recurring", True)

            except Exception as e:
                logger.error(f"Error setting recurring reminder: {e}, ai_response={ai_response}")
                reply_text = "Sorry, I couldn't set that recurring reminder. Please try again."
                log_interaction(phone_number, incoming_msg, reply_text, "reminder_recurring", False)

        # ==========================================
        # LIST ACTION HANDLERS
        # ==========================================
        elif ai_response["action"] == "create_list":
            list_name = ai_response.get("list_name")
            # Validate list name
            is_valid, result = validate_list_name(list_name)
            if not is_valid:
                reply_text = result
            else:
                list_name = result  # Use sanitized name
                # Check tier limit for lists
                from services.tier_service import can_create_list
                allowed, limit_msg = can_create_list(phone_number)
                if not allowed:
                    reply_text = limit_msg
                elif get_list_by_name(phone_number, list_name):
                    reply_text = f"You already have a list called '{list_name}'."
                else:
                    create_list(phone_number, list_name)
                    reply_text = ai_response.get("confirmation", f"Created your {list_name}!")
            log_interaction(phone_number, incoming_msg, reply_text, "create_list", True)

        elif ai_response["action"] == "add_to_list":
            list_name = ai_response.get("list_name")
            item_text = ai_response.get("item_text")

            # Check for sensitive data (staging only)
            if ENVIRONMENT == "staging":
                sensitive_check = detect_sensitive_data(item_text)
                if sensitive_check['has_sensitive']:
                    log_security_event('SENSITIVE_DATA_BLOCKED', {
                        'phone': phone_number,
                        'action': 'add_to_list',
                        'types': sensitive_check['types']
                    })
                    reply_text = get_sensitive_data_warning()
                    log_interaction(phone_number, incoming_msg, reply_text, "add_to_list_blocked", False)
                    return reply_text

            # Validate inputs
            name_valid, name_result = validate_list_name(list_name)
            item_valid, item_result = validate_item_text(item_text)

            if not name_valid:
                reply_text = name_result
                log_interaction(phone_number, incoming_msg, reply_text, "add_to_list", False)
            elif not item_valid:
                reply_text = item_result
                log_interaction(phone_number, incoming_msg, reply_text, "add_to_list", False)
            else:
                list_name = name_result  # Sanitized
                item_text = item_result  # Sanitized

                # Parse multiple items from the text
                items_to_add = parse_list_items(item_text, phone_number)

                list_info = get_list_by_name(phone_number, list_name)

                # Auto-create list if it doesn't exist
                if not list_info:
                    from services.tier_service import can_create_list, can_add_list_item, get_tier_limits, get_user_tier
                    allowed, limit_msg = can_create_list(phone_number)
                    if not allowed:
                        reply_text = limit_msg
                    else:
                        list_id = create_list(phone_number, list_name)
                        # Add all parsed items (check tier item limit)
                        tier_limits = get_tier_limits(get_user_tier(phone_number))
                        max_items = tier_limits['max_items_per_list']
                        added_items = []
                        for item in items_to_add:
                            if len(added_items) < max_items:
                                add_list_item(list_id, phone_number, item)
                                added_items.append(item)
                        # Track last active list
                        create_or_update_user(phone_number, last_active_list=list_name)
                        if len(added_items) == 1:
                            reply_text = f"Created your {list_name} and added {added_items[0]}!"
                        else:
                            reply_text = f"Created your {list_name} and added {len(added_items)} items: {', '.join(added_items)}"
                else:
                    list_id = list_info[0]
                    list_name = list_info[1]  # Use actual list name from DB
                    # Check tier limit for items per list
                    from services.tier_service import can_add_list_item, get_tier_limits, get_user_tier
                    allowed, limit_msg = can_add_list_item(phone_number, list_id)
                    if not allowed:
                        reply_text = limit_msg
                    else:
                        tier_limits = get_tier_limits(get_user_tier(phone_number))
                        max_items = tier_limits['max_items_per_list']
                        item_count = get_item_count(list_id)
                        available_slots = max_items - item_count

                        # Add items up to the limit
                        added_items = []
                        for item in items_to_add:
                            if len(added_items) < available_slots:
                                add_list_item(list_id, phone_number, item)
                                added_items.append(item)

                        # Track last active list
                        create_or_update_user(phone_number, last_active_list=list_name)

                        if len(added_items) == 1:
                            reply_text = ai_response.get("confirmation", f"Added {added_items[0]} to your {list_name}")
                        elif len(added_items) < len(items_to_add):
                            reply_text = f"Added {len(added_items)} items to your {list_name}: {', '.join(added_items)}. ({len(items_to_add) - len(added_items)} items skipped - list full)"
                        else:
                            reply_text = f"Added {len(added_items)} items to your {list_name}: {', '.join(added_items)}"
                log_interaction(phone_number, incoming_msg, reply_text, "add_to_list", True)

        elif ai_response["action"] == "add_item_ask_list":
            item_text = ai_response.get("item_text")
            lists = get_lists(phone_number)
            if len(lists) == 1:
                # Only one list, add directly with multi-item parsing
                list_id = lists[0][0]
                list_name = lists[0][1]

                # Parse multiple items
                items_to_add = parse_list_items(item_text, phone_number)

                # Check tier limit for items per list
                from services.tier_service import can_add_list_item, get_tier_limits, get_user_tier
                allowed, limit_msg = can_add_list_item(phone_number, list_id)
                if not allowed:
                    reply_text = limit_msg
                else:
                    tier_limits = get_tier_limits(get_user_tier(phone_number))
                    max_items = tier_limits['max_items_per_list']
                    item_count = get_item_count(list_id)
                    available_slots = max_items - item_count

                    # Add items up to the limit
                    added_items = []
                    for item in items_to_add:
                        if len(added_items) < available_slots:
                            add_list_item(list_id, phone_number, item)
                            added_items.append(item)

                    # Track last active list
                    create_or_update_user(phone_number, last_active_list=list_name)

                    if len(added_items) == 1:
                        reply_text = f"Added {added_items[0]} to your {list_name}"
                    elif len(added_items) < len(items_to_add):
                        reply_text = f"Added {len(added_items)} items to your {list_name}: {', '.join(added_items)}. ({len(items_to_add) - len(added_items)} items skipped - list full)"
                    else:
                        reply_text = f"Added {len(added_items)} items to your {list_name}: {', '.join(added_items)}"
            elif len(lists) > 1:
                # Multiple lists, ask which one (store original text for parsing later)
                create_or_update_user(phone_number, pending_list_item=item_text)
                list_options = "\n".join([f"{i+1}. {l[1]}" for i, l in enumerate(lists)])
                reply_text = f"Which list would you like to add these to?\n\n{list_options}\n\nReply with a number:"
            else:
                reply_text = "You don't have any lists yet. Try 'Create a grocery list' first!"
            log_interaction(phone_number, incoming_msg, reply_text, "add_item_ask_list", True)

        elif ai_response["action"] == "show_list":
            list_name = ai_response.get("list_name")
            list_info = get_list_by_name(phone_number, list_name)
            if list_info:
                # Track last active list
                create_or_update_user(phone_number, last_active_list=list_info[1])
                items = get_list_items(list_info[0])
                if items:
                    item_lines = []
                    for i, (item_id, item_text, completed) in enumerate(items, 1):
                        if completed:
                            item_lines.append(f"{i}. [x] {item_text}")
                        else:
                            item_lines.append(f"{i}. {item_text}")
                    reply_text = f"{list_info[1]}:\n\n" + "\n".join(item_lines)
                else:
                    reply_text = f"Your {list_info[1]} is empty."
            else:
                reply_text = f"I couldn't find a list called '{list_name}'."
            log_interaction(phone_number, incoming_msg, reply_text, "show_list", True)

        elif ai_response["action"] == "show_current_list":
            # Show the last active list or fall back to showing all lists
            last_active = get_last_active_list(phone_number)
            logger.info(f"show_current_list: last_active={last_active}")
            if last_active:
                list_info = get_list_by_name(phone_number, last_active)
                if list_info:
                    items = get_list_items(list_info[0])
                    if items:
                        item_lines = []
                        for i, (item_id, item_text, completed) in enumerate(items, 1):
                            if completed:
                                item_lines.append(f"{i}. [x] {item_text}")
                            else:
                                item_lines.append(f"{i}. {item_text}")
                        reply_text = f"{list_info[1]}:\n\n" + "\n".join(item_lines)
                    else:
                        reply_text = f"Your {list_info[1]} is empty."
                else:
                    # Last active list was deleted, show all lists (or single list directly)
                    lists = get_lists(phone_number)
                    if len(lists) == 1:
                        list_id = lists[0][0]
                        list_name = lists[0][1]
                        create_or_update_user(phone_number, last_active_list=list_name)
                        items = get_list_items(list_id)
                        if items:
                            item_lines = []
                            for i, (item_id, item_text, completed) in enumerate(items, 1):
                                if completed:
                                    item_lines.append(f"{i}. [x] {item_text}")
                                else:
                                    item_lines.append(f"{i}. {item_text}")
                            reply_text = f"{list_name}:\n\n" + "\n".join(item_lines)
                        else:
                            reply_text = f"Your {list_name} is empty."
                    elif lists:
                        list_lines = [f"{i+1}. {l[1]} ({l[2]} items)" for i, l in enumerate(lists)]
                        reply_text = "Your lists:\n\n" + "\n".join(list_lines) + "\n\nReply with a number to see that list."
                    else:
                        reply_text = "You don't have any lists yet. Try 'Create a grocery list'!"
            else:
                # No last active list, show all lists (or single list directly)
                lists = get_lists(phone_number)
                if len(lists) == 1:
                    list_id = lists[0][0]
                    list_name = lists[0][1]
                    create_or_update_user(phone_number, last_active_list=list_name)
                    items = get_list_items(list_id)
                    if items:
                        item_lines = []
                        for i, (item_id, item_text, completed) in enumerate(items, 1):
                            if completed:
                                item_lines.append(f"{i}. [x] {item_text}")
                            else:
                                item_lines.append(f"{i}. {item_text}")
                        reply_text = f"{list_name}:\n\n" + "\n".join(item_lines)
                    else:
                        reply_text = f"Your {list_name} is empty."
                elif lists:
                    list_lines = [f"{i+1}. {l[1]} ({l[2]} items)" for i, l in enumerate(lists)]
                    reply_text = "Your lists:\n\n" + "\n".join(list_lines) + "\n\nReply with a number to see that list."
                else:
                    reply_text = "You don't have any lists yet. Try 'Create a grocery list'!"
            log_interaction(phone_number, incoming_msg, reply_text, "show_current_list", True)

        elif ai_response["action"] == "show_all_lists":
            lists = get_lists(phone_number)
            logger.info(f"show_all_lists: found {len(lists)} lists")
            if len(lists) == 1:
                # Only one list, show it directly
                list_id = lists[0][0]
                list_name = lists[0][1]
                create_or_update_user(phone_number, last_active_list=list_name)
                items = get_list_items(list_id)
                if items:
                    item_lines = []
                    for i, (item_id, item_text, completed) in enumerate(items, 1):
                        if completed:
                            item_lines.append(f"{i}. [x] {item_text}")
                        else:
                            item_lines.append(f"{i}. {item_text}")
                    reply_text = f"{list_name}:\n\n" + "\n".join(item_lines)
                else:
                    reply_text = f"Your {list_name} is empty."
            elif lists:
                list_lines = []
                for i, (list_id, list_name, item_count, completed_count) in enumerate(lists, 1):
                    list_lines.append(f"{i}. {list_name} ({item_count} items)")
                reply_text = "Your lists:\n\n" + "\n".join(list_lines) + "\n\nReply with a number to see that list."
            else:
                reply_text = "You don't have any lists yet. Try 'Create a grocery list'!"
            log_interaction(phone_number, incoming_msg, reply_text, "show_all_lists", True)

        elif ai_response["action"] == "complete_item":
            list_name = ai_response.get("list_name")
            item_text = ai_response.get("item_text")
            if mark_item_complete(phone_number, list_name, item_text):
                reply_text = ai_response.get("confirmation", f"Checked off {item_text}")
            else:
                # Try to find item in any list
                found = find_item_in_any_list(phone_number, item_text)
                if len(found) == 1:
                    list_name = found[0][1]
                    if mark_item_complete(phone_number, list_name, item_text):
                        reply_text = f"Checked off {item_text} from your {list_name}"
                    else:
                        reply_text = f"Couldn't find '{item_text}' in your lists."
                elif len(found) > 1:
                    reply_text = f"'{item_text}' is in multiple lists. Please specify which list."
                else:
                    reply_text = f"Couldn't find '{item_text}' in your lists."
            log_interaction(phone_number, incoming_msg, reply_text, "complete_item", True)

        elif ai_response["action"] == "uncomplete_item":
            list_name = ai_response.get("list_name")
            item_text = ai_response.get("item_text")
            if mark_item_incomplete(phone_number, list_name, item_text):
                reply_text = ai_response.get("confirmation", f"Unmarked {item_text}")
            else:
                reply_text = f"Couldn't find '{item_text}' to unmark."
            log_interaction(phone_number, incoming_msg, reply_text, "uncomplete_item", True)

        elif ai_response["action"] == "delete_item":
            list_name = ai_response.get("list_name")
            item_text = ai_response.get("item_text")
            if delete_list_item(phone_number, list_name, item_text):
                reply_text = ai_response.get("confirmation", f"Removed {item_text} from your {list_name}")
            else:
                reply_text = f"Couldn't find '{item_text}' to remove."
            log_interaction(phone_number, incoming_msg, reply_text, "delete_item", True)

        elif ai_response["action"] == "delete_list":
            list_name = ai_response.get("list_name")
            list_info = get_list_by_name(phone_number, list_name)
            if list_info:
                # Store pending delete for list
                create_or_update_user(phone_number, pending_delete=True, pending_list_item=list_name)
                reply_text = f"Are you sure you want to delete your {list_info[1]} and all its items?\n\nReply YES to confirm."
            else:
                reply_text = f"I couldn't find a list called '{list_name}'."
            log_interaction(phone_number, incoming_msg, reply_text, "delete_list", True)

        elif ai_response["action"] == "clear_list":
            list_name = ai_response.get("list_name")
            if clear_list(phone_number, list_name):
                reply_text = ai_response.get("confirmation", f"Cleared all items from your {list_name}")
            else:
                reply_text = f"I couldn't find a list called '{list_name}'."
            log_interaction(phone_number, incoming_msg, reply_text, "clear_list", True)

        elif ai_response["action"] == "rename_list":
            old_name = ai_response.get("old_name")
            new_name = ai_response.get("new_name")
            if get_list_by_name(phone_number, new_name):
                reply_text = f"You already have a list called '{new_name}'."
            elif rename_list(phone_number, old_name, new_name):
                reply_text = ai_response.get("confirmation", f"Renamed {old_name} to {new_name}")
            else:
                reply_text = f"I couldn't find a list called '{old_name}'."
            log_interaction(phone_number, incoming_msg, reply_text, "rename_list", True)

        elif ai_response["action"] == "delete_reminder":
            search_term = ai_response.get("search_term", "")
            user_tz = get_user_timezone(phone_number)
            tz = pytz.timezone(user_tz)

            # Search for matching pending reminders
            matching_reminders = search_pending_reminders(phone_number, search_term)

            if len(matching_reminders) == 0:
                reply_text = f"No pending reminders found matching '{search_term}'."
            elif len(matching_reminders) == 1:
                # Single match - delete directly
                reminder_id, reminder_text, reminder_date = matching_reminders[0]
                if delete_reminder(phone_number, reminder_id):
                    reply_text = f"Deleted your reminder: {reminder_text}"
                else:
                    reply_text = "Couldn't delete that reminder."
            else:
                # Multiple matches - ask user to choose
                reminder_options = []
                lines = ["Found multiple reminders:"]
                for i, (reminder_id, reminder_text, reminder_date) in enumerate(matching_reminders, 1):
                    # Convert UTC to user timezone for display
                    if isinstance(reminder_date, str):
                        utc_dt = datetime.strptime(reminder_date, '%Y-%m-%d %H:%M:%S')
                    else:
                        utc_dt = reminder_date
                    utc_dt = pytz.UTC.localize(utc_dt)
                    local_dt = utc_dt.astimezone(tz)
                    formatted_date = local_dt.strftime('%b %d at %I:%M %p')
                    lines.append(f"{i}. {reminder_text} ({formatted_date})")
                    reminder_options.append({'id': reminder_id, 'text': reminder_text})

                lines.append("\nReply with a number to delete that reminder:")
                reply_text = "\n".join(lines)

                # Store options for number selection
                create_or_update_user(phone_number, pending_reminder_delete=json.dumps(reminder_options))

            log_interaction(phone_number, incoming_msg, reply_text, "delete_reminder", True)

        elif ai_response["action"] == "delete_memory":
            search_term = ai_response.get("search_term", "")

            # Search for matching memories
            matching_memories = search_memories(phone_number, search_term)

            if len(matching_memories) == 0:
                reply_text = f"No memories found matching '{search_term}'."
            elif len(matching_memories) == 1:
                # Single match - ask for confirmation before deleting
                memory_id, memory_text, created_at = matching_memories[0]

                # Store pending delete with confirmation flag
                confirm_data = json.dumps({
                    'awaiting_confirmation': True,
                    'id': memory_id,
                    'text': memory_text
                })
                create_or_update_user(phone_number, pending_memory_delete=confirm_data)

                # Truncate long memory text for display
                display_text = memory_text[:100] + ('...' if len(memory_text) > 100 else '')
                reply_text = f"Delete memory: '{display_text}'?\n\nReply YES to confirm or NO to cancel."
            else:
                # Multiple matches - ask user to choose
                memory_options = []
                lines = ["Found multiple memories:"]
                for i, (memory_id, memory_text, created_at) in enumerate(matching_memories, 1):
                    # Truncate long memory text for display
                    display_text = memory_text[:80] + ('...' if len(memory_text) > 80 else '')
                    lines.append(f"{i}. {display_text}")
                    memory_options.append({'id': memory_id, 'text': memory_text})

                lines.append("\nReply with a number to select:")
                reply_text = "\n".join(lines)

                # Store options for number selection
                create_or_update_user(phone_number, pending_memory_delete=json.dumps({'options': memory_options}))

            log_interaction(phone_number, incoming_msg, reply_text, "delete_memory", True)

        else:  # help or error
            reply_text = ai_response.get("response", "Hi! Text me to remember things, set reminders, or ask me about stored info.")
            log_interaction(phone_number, incoming_msg, reply_text, "help", True)

        return reply_text

    except Exception as e:
        logger.error(f"Error processing action: {e}", exc_info=True)
        return "Sorry, I couldn't complete that action."


# =====================================================
# STRIPE WEBHOOK ENDPOINT
# =====================================================

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events"""
    from services.stripe_service import handle_webhook_event
    from config import STRIPE_ENABLED

    if not STRIPE_ENABLED:
        logger.warning("Stripe webhook received but Stripe is not configured")
        return {"error": "Stripe not configured"}, 400

    try:
        payload = await request.body()
        sig_header = request.headers.get("stripe-signature", "")

        result = handle_webhook_event(payload, sig_header)

        if result.get('success'):
            return {"status": "success"}
        else:
            logger.error(f"Webhook error: {result.get('error')}")
            return {"error": result.get('error')}, 400

    except Exception as e:
        logger.error(f"Stripe webhook error: {e}")
        return {"error": str(e)}, 500


@app.get("/payment/success")
async def payment_success(session_id: str = None):
    """Handle successful payment redirect"""
    return {
        "status": "success",
        "message": "Thank you for subscribing! You can close this page and continue using Remyndrs via SMS."
    }


@app.get("/payment/cancelled")
async def payment_cancelled():
    """Handle cancelled payment redirect"""
    return {
        "status": "cancelled",
        "message": "Payment was cancelled. Text UPGRADE anytime to try again."
    }


# =====================================================
# ADMIN & UTILITY ENDPOINTS
# =====================================================

@app.get("/")
async def health_check():
    """Health check endpoint"""
    logger.info("Health check called")
    return {
        "status": "running",
        "service": "SMS Memory Assistant",
        "environment": ENVIRONMENT,
        "features": ["memory_storage", "reminders", "onboarding", "timezone_support"]
    }


@app.get("/contact.vcf")
async def get_contact_vcf():
    """Serve Remyndrs contact card (VCF) for saving to phone contacts"""
    import os
    import base64

    # Load and base64 encode the logo for better phone compatibility
    logo_path = os.path.join(os.path.dirname(__file__), "static", "remyndrs-logo.png")
    try:
        with open(logo_path, "rb") as f:
            logo_b64 = base64.b64encode(f.read()).decode("utf-8")
        photo_line = f"PHOTO;ENCODING=b;TYPE=PNG:{logo_b64}"
    except Exception as e:
        logger.error(f"Error loading logo for VCF: {e}")
        photo_line = ""

    vcf_content = f"""BEGIN:VCARD
VERSION:3.0
FN:Remyndrs
ORG:Remyndrs
TEL;TYPE=CELL:{PUBLIC_PHONE_NUMBER}
{photo_line}
NOTE:Your personal SMS assistant for reminders, lists, and memories. Text ? for help.
END:VCARD"""

    return Response(
        content=vcf_content,
        media_type="text/vcard",
        headers={"Content-Disposition": "attachment; filename=remyndrs.vcf"}
    )


@app.get("/static/remyndrs-logo.png")
async def get_logo():
    """Serve Remyndrs logo for contact card"""
    import os
    logo_path = os.path.join(os.path.dirname(__file__), "static", "remyndrs-logo.png")
    return FileResponse(logo_path, media_type="image/png")


@app.get("/consent", response_class=HTMLResponse)
async def consent_page():
    """Public page showing SMS opt-in consent information for Twilio verification"""
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>Remyndrs - SMS Consent & Opt-In Policy</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 800px;
            margin: 0 auto;
            padding: 40px 20px;
            line-height: 1.6;
            color: #333;
        }
        h1 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; }
        h2 { color: #34495e; margin-top: 30px; }
        .section { background: #f8f9fa; padding: 20px; border-radius: 8px; margin: 20px 0; }
        .highlight { background: #e8f6ff; border-left: 4px solid #3498db; padding: 15px; margin: 15px 0; }
        ul { padding-left: 20px; }
        li { margin: 10px 0; }
        .footer { margin-top: 40px; padding-top: 20px; border-top: 1px solid #ddd; color: #7f8c8d; font-size: 0.9em; }
    </style>
</head>
<body>
    <h1>Remyndrs SMS Consent & Opt-In Policy</h1>

    <div class="section">
        <h2>Service Description</h2>
        <p>Remyndrs is an SMS-based personal assistant service that helps users:</p>
        <ul>
            <li>Store and recall personal information and memories</li>
            <li>Set and receive SMS reminders</li>
            <li>Create and manage lists</li>
        </ul>
    </div>

    <h2>How Users Opt-In</h2>
    <div class="highlight">
        <p><strong>Users initiate contact by texting our phone number first.</strong></p>
        <p>When a user sends their first SMS message to Remyndrs, they begin an onboarding process where they:</p>
        <ol>
            <li>Receive a welcome message explaining the service</li>
            <li>Provide their first name</li>
            <li>Provide their last name</li>
            <li>Provide their email address</li>
            <li>Provide their ZIP code (for timezone detection)</li>
            <li>Optionally share how they heard about us</li>
        </ol>
        <p>By completing this onboarding process, users explicitly consent to receive SMS messages from Remyndrs.</p>
    </div>

    <h2>Types of Messages Sent</h2>
    <div class="section">
        <ul>
            <li><strong>Conversational responses:</strong> Direct replies to user queries about their stored information</li>
            <li><strong>Reminder notifications:</strong> Scheduled reminders that users explicitly request</li>
            <li><strong>System messages:</strong> Occasional service announcements (users can opt-out)</li>
        </ul>
    </div>

    <h2>Opt-Out Instructions</h2>
    <div class="highlight">
        <p>Users can opt-out at any time by:</p>
        <ul>
            <li>Texting <strong>STOP</strong> to unsubscribe from all messages</li>
            <li>Texting <strong>DELETE ALL</strong> to remove all their stored data</li>
            <li>Texting <strong>RESET ACCOUNT</strong> to restart their account</li>
        </ul>
    </div>

    <h2>Message Frequency</h2>
    <p>Message frequency varies based on user interaction. Users only receive messages when:</p>
    <ul>
        <li>They send a message and receive a response</li>
        <li>A reminder they scheduled is triggered</li>
        <li>Occasional system announcements (limited to reasonable hours 8am-8pm local time)</li>
    </ul>

    <h2>Privacy & Data</h2>
    <p>User data is stored securely and used solely to provide the Remyndrs service. We do not sell or share user information with third parties.</p>

    <h2>Contact Information</h2>
    <p>For questions about this service or to request data deletion, users can text <strong>FEEDBACK</strong> followed by their message, or contact us at our support channels.</p>

    <div class="footer">
        <p>Last updated: December 2024</p>
        <p>&copy; 2024 Remyndrs. All rights reserved.</p>
    </div>
</body>
</html>
    """
    return HTMLResponse(content=html)


@app.get("/memories/{phone_number}")
async def view_memories(phone_number: str, admin: str = Depends(verify_admin)):
    """View all memories for a phone number - for testing/admin"""
    # Tuple format: (id, memory_text, parsed_data, created_at)
    memories = get_memories(phone_number)
    return {
        "phone_number": phone_number,
        "total_memories": len(memories),
        "memories": [
            {
                "id": m[0],
                "text": m[1],
                "data": json.loads(m[2]) if m[2] else {},
                "created": m[3]
            } for m in memories
        ]
    }

@app.get("/reminders/{phone_number}")
async def view_reminders(phone_number: str, admin: str = Depends(verify_admin)):
    """View all reminders for a phone number - for testing/admin"""
    # Tuple format: (id, reminder_date, reminder_text, recurring_id, sent)
    reminders = get_user_reminders(phone_number)
    return {
        "phone_number": phone_number,
        "total_reminders": len(reminders),
        "reminders": [
            {
                "id": r[0],
                "date": r[1],
                "text": r[2],
                "recurring_id": r[3],
                "sent": bool(r[4])
            } for r in reminders
        ]
    }

@app.get("/admin/stats")
async def admin_stats(admin: str = Depends(verify_admin)):
    """Admin dashboard showing key metrics"""
    from database import get_db_connection
    conn = get_db_connection()
    c = conn.cursor()

    # Total users
    c.execute('SELECT COUNT(DISTINCT phone_number) FROM users WHERE onboarding_complete = TRUE')
    total_users = c.fetchone()[0]

    # Total memories
    c.execute('SELECT COUNT(*) FROM memories')
    total_memories = c.fetchone()[0]

    # Total reminders
    c.execute('SELECT COUNT(*) FROM reminders')
    total_reminders = c.fetchone()[0]

    # Pending reminders
    c.execute('SELECT COUNT(*) FROM reminders WHERE sent = FALSE')
    pending_reminders = c.fetchone()[0]

    # Sent reminders
    c.execute('SELECT COUNT(*) FROM reminders WHERE sent = TRUE')
    sent_reminders = c.fetchone()[0]

    # Most active users (top 5)
    c.execute('''
        SELECT phone_number, COUNT(*) as interaction_count
        FROM logs
        GROUP BY phone_number
        ORDER BY interaction_count DESC
        LIMIT 5
    ''')
    top_users = c.fetchall()

    # Activity last 24 hours
    c.execute('''
        SELECT COUNT(*)
        FROM logs
        WHERE created_at >= NOW() - INTERVAL '1 day'
    ''')
    activity_24h = c.fetchone()[0]

    conn.close()

    return {
        "overview": {
            "total_users": total_users,
            "total_memories": total_memories,
            "total_reminders": total_reminders,
            "pending_reminders": pending_reminders,
            "sent_reminders": sent_reminders,
            "avg_memories_per_user": round(total_memories / total_users, 2) if total_users > 0 else 0,
            "avg_reminders_per_user": round(total_reminders / total_users, 2) if total_users > 0 else 0
        },
        "top_users": [
            {
                "phone_number": user[0],
                "interactions": user[1]
            } for user in top_users
        ],
        "activity": {
            "last_24_hours": activity_24h
        },
        "environment": ENVIRONMENT
    }

# =====================================================
# APPLICATION ENTRY POINT
# =====================================================

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting uvicorn server...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
