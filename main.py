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
from fastapi.responses import Response, HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
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
from database import init_db, log_interaction, get_setting, log_confidence
from models.user import get_user, is_user_onboarded, create_or_update_user, get_user_timezone, get_last_active_list, get_pending_list_item, get_pending_reminder_delete, get_pending_memory_delete, get_pending_reminder_date, get_pending_list_create, mark_user_opted_out, get_user_first_name, get_pending_reminder_confirmation, is_user_opted_out, cancel_engagement_nudge, increment_post_onboarding_interactions
from models.memory import save_memory, get_memories, search_memories, delete_memory
from models.reminder import (
    save_reminder, get_user_reminders, search_pending_reminders, delete_reminder,
    get_last_sent_reminder, mark_reminder_snoozed, save_recurring_reminder,
    get_recurring_reminders, delete_recurring_reminder, pause_recurring_reminder,
    resume_recurring_reminder, save_reminder_with_local_time, update_reminder_time,
    recalculate_pending_reminders_for_timezone, update_recurring_reminders_timezone
)
from models.list_model import (
    create_list, get_lists, get_list_by_name, get_list_items,
    add_list_item, mark_item_complete, mark_item_incomplete,
    delete_list_item, delete_list, rename_list, clear_list,
    find_item_in_any_list, get_list_count, get_item_count,
    get_next_available_list_name
)
from services.sms_service import send_sms
from services.ai_service import process_with_ai, parse_list_items
from services.onboarding_service import handle_onboarding
from services.first_action_service import should_prompt_daily_summary, mark_daily_summary_prompted, get_daily_summary_prompt_message, handle_daily_summary_response
from services.trial_messaging_service import (
    is_pricing_question, is_comparison_question, is_acknowledgment,
    get_trial_info_sent, mark_trial_info_sent,
    get_pricing_response, get_pricing_faq_response,
    get_comparison_response, get_comparison_faq_response,
    get_acknowledgment_response, append_trial_info_to_response
)
# NOTE: Reminder checking is now handled by Celery Beat (see tasks/reminder_tasks.py)
from services.metrics_service import track_user_activity, increment_message_count
from utils.timezone import get_user_current_time
from utils.formatting import get_help_text, format_reminders_list, format_reminder_confirmation
from utils.validation import mask_phone_number, validate_list_name, validate_item_text, validate_message, log_security_event, detect_sensitive_data, get_sensitive_data_warning
from admin_dashboard import router as dashboard_router, start_broadcast_checker
from cs_portal import router as cs_router
from monitoring_dashboard import router as monitoring_router


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


def parse_command(message: str, known_commands: list = None):
    """
    Parse commands from SMS messages with improved format.

    Supports both new format (COMMAND message) and old format (COMMAND: message) for backward compatibility.

    Args:
        message: The incoming SMS message
        known_commands: List of known command names (case-insensitive).
                       Defaults to: START, STOP, HELP, SUPPORT, FEEDBACK, BUG, QUESTION

    Returns:
        tuple: (command, message_text) where:
            - command is uppercase command name or None if no match
            - message_text is the rest of the message after the command

    Examples:
        "SUPPORT I need help" -> ("SUPPORT", "I need help")
        "SUPPORT: I need help" -> ("SUPPORT", "I need help")  # backward compatible
        "support message" -> ("SUPPORT", "message")  # case insensitive
        "SUPPORT" -> ("SUPPORT", "")  # command only, no message
        "Remind me tomorrow" -> (None, "Remind me tomorrow")  # not a command
    """
    if not message:
        return (None, "")

    # Default known commands if not provided
    if known_commands is None:
        known_commands = ["START", "STOP", "HELP", "SUPPORT", "FEEDBACK", "BUG", "QUESTION"]

    # Normalize known commands to uppercase
    known_commands = [cmd.upper() for cmd in known_commands]

    # Strip leading/trailing whitespace
    message = message.strip()

    # Split on first whitespace or colon
    parts = re.split(r'[\s:]+', message, maxsplit=1)

    if not parts:
        return (None, message)

    potential_command = parts[0].upper()

    # Check if first word matches a known command
    if potential_command in known_commands:
        # Extract message (everything after first word/colon)
        if len(parts) > 1:
            message_text = parts[1].strip()
        else:
            message_text = ""

        return (potential_command, message_text)

    # Not a recognized command
    return (None, message)


# Initialize application
logger.info("🚀 SMS Memory Service starting...")
app = FastAPI()

# CORS middleware - allow requests from remyndrs.com
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with ["https://remyndrs.com"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

# Include monitoring dashboard router
app.include_router(monitoring_router)

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

        # Normalize compact time formats: "125pm" → "1:25 pm", "1215pm" → "12:15 pm"
        incoming_msg = re.sub(
            r'\b(\d{1,2})(\d{2})\s*(am|pm|a\.m\.|p\.m\.)\b',
            r'\1:\2 \3',
            incoming_msg,
            flags=re.IGNORECASE
        )

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
        # DELETE ACCOUNT COMMAND (two-step confirmation)
        # ==========================================
        if incoming_msg.upper() == "DELETE ACCOUNT":
            logger.info(f"DELETE ACCOUNT requested by {mask_phone_number(phone_number)}")
            create_or_update_user(phone_number, pending_delete_account=True)
            resp = MessagingResponse()
            resp.message(
                "This will permanently delete all your data (reminders, memories, lists) "
                "and cancel your Premium subscription if active. This cannot be undone.\n\n"
                "Want a copy of your data first? Text EXPORT to receive it by email.\n\n"
                "To confirm deletion, text YES DELETE ACCOUNT.\nTo cancel, text anything else."
            )
            log_interaction(phone_number, incoming_msg, "Delete account confirmation requested", "delete_account_request", True)
            return Response(content=str(resp), media_type="application/xml")

        # Handle YES DELETE ACCOUNT confirmation
        if incoming_msg.upper() == "YES DELETE ACCOUNT":
            user = get_user(phone_number)
            # Check pending_delete_account flag
            if user:
                from database import get_db_connection, return_db_connection
                conn_check = get_db_connection()
                c_check = conn_check.cursor()
                c_check.execute('SELECT pending_delete_account FROM users WHERE phone_number = %s', (phone_number,))
                result = c_check.fetchone()
                return_db_connection(conn_check)
                pending = result[0] if result else False
            else:
                pending = False

            if not pending:
                resp = MessagingResponse()
                resp.message("No pending account deletion. If you want to delete your account, text DELETE ACCOUNT first.")
                return Response(content=str(resp), media_type="application/xml")

            logger.info(f"DELETE ACCOUNT confirmed by {mask_phone_number(phone_number)} - deleting all data")

            try:
                # Cancel Stripe subscription if active
                from services.stripe_service import cancel_stripe_subscription
                cancel_result = cancel_stripe_subscription(phone_number)
                if cancel_result.get('success'):
                    logger.info(f"Stripe subscription cancelled for {mask_phone_number(phone_number)}")
                elif cancel_result.get('error'):
                    logger.warning(f"Stripe cancellation issue for {mask_phone_number(phone_number)}: {cancel_result['error']}")

                # Delete all user data (order matters for foreign key constraints)
                from database import get_db_connection, return_db_connection
                conn = get_db_connection()
                c = conn.cursor()

                # Clean up monitoring agent FK chain (these tables reference logs via monitoring_issues)
                # Use savepoints since monitoring tables may not exist in all environments
                c.execute("SELECT id FROM logs WHERE phone_number = %s", (phone_number,))
                log_ids = [row[0] for row in c.fetchall()]

                if log_ids:
                    # Get monitoring_issues IDs that reference this user's logs
                    mi_ids = []
                    try:
                        c.execute("SAVEPOINT mi_lookup")
                        c.execute("SELECT id FROM monitoring_issues WHERE log_id = ANY(%s)", (log_ids,))
                        mi_ids = [row[0] for row in c.fetchall()]
                    except Exception:
                        c.execute("ROLLBACK TO SAVEPOINT mi_lookup")

                    if mi_ids:
                        for table in ['code_analysis', 'issue_pattern_links', 'fix_proposals', 'issue_resolutions']:
                            try:
                                c.execute(f"SAVEPOINT del_{table}")
                                c.execute(f"DELETE FROM {table} WHERE issue_id = ANY(%s)", (mi_ids,))
                            except Exception:
                                c.execute(f"ROLLBACK TO SAVEPOINT del_{table}")
                        try:
                            c.execute("SAVEPOINT del_mi")
                            c.execute("DELETE FROM monitoring_issues WHERE id = ANY(%s)", (mi_ids,))
                        except Exception:
                            c.execute("ROLLBACK TO SAVEPOINT del_mi")

                    # conversation_analysis also references logs(id)
                    c.execute("DELETE FROM conversation_analysis WHERE log_id = ANY(%s)", (log_ids,))

                # Delete remaining monitoring/analysis rows by phone_number
                for table in ['conversation_analysis', 'monitoring_issues']:
                    try:
                        c.execute(f"SAVEPOINT del_{table}_ph")
                        c.execute(f"DELETE FROM {table} WHERE phone_number = %s", (phone_number,))
                    except Exception:
                        c.execute(f"ROLLBACK TO SAVEPOINT del_{table}_ph")
                c.execute("DELETE FROM support_messages WHERE phone_number = %s", (phone_number,))
                c.execute("DELETE FROM support_tickets WHERE phone_number = %s", (phone_number,))
                c.execute("DELETE FROM confidence_logs WHERE phone_number = %s", (phone_number,))
                c.execute("DELETE FROM api_usage WHERE phone_number = %s", (phone_number,))
                c.execute("DELETE FROM customer_notes WHERE phone_number = %s", (phone_number,))

                # Now delete the main tables
                c.execute("DELETE FROM reminders WHERE phone_number = %s", (phone_number,))
                c.execute("DELETE FROM recurring_reminders WHERE phone_number = %s", (phone_number,))
                c.execute("DELETE FROM memories WHERE phone_number = %s", (phone_number,))
                c.execute("DELETE FROM list_items WHERE phone_number = %s", (phone_number,))
                c.execute("DELETE FROM lists WHERE phone_number = %s", (phone_number,))
                c.execute("DELETE FROM logs WHERE phone_number = %s", (phone_number,))
                c.execute("DELETE FROM onboarding_progress WHERE phone_number = %s", (phone_number,))
                c.execute("DELETE FROM feedback WHERE user_phone = %s", (phone_number,))

                conn.commit()
                return_db_connection(conn)

                # Mark user as opted out (STOP equivalent)
                mark_user_opted_out(phone_number)

                # Clear user record fields
                create_or_update_user(
                    phone_number,
                    first_name=None,
                    last_name=None,
                    email=None,
                    zip_code=None,
                    onboarding_complete=False,
                    onboarding_step=0,
                    pending_delete=False,
                    pending_delete_account=False,
                    pending_reminder_text=None,
                    pending_reminder_time=None,
                    trial_end_date=None,
                    premium_status='free',
                    stripe_customer_id=None,
                    stripe_subscription_id=None,
                    subscription_status=None,
                )

                resp = MessagingResponse()
                resp.message(
                    "Your account has been deleted and your subscription cancelled. "
                    "All data has been removed. If you ever want to come back, text START."
                )
                log_interaction(phone_number, "YES DELETE ACCOUNT", "Account deleted", "delete_account_confirmed", True)
                return Response(content=str(resp), media_type="application/xml")

            except Exception as e:
                logger.error(f"Error during account deletion: {e}", exc_info=True)
                create_or_update_user(phone_number, pending_delete_account=False)
                resp = MessagingResponse()
                resp.message("Sorry, there was an error deleting your account. Please try again or contact support@remyndrs.com.")
                return Response(content=str(resp), media_type="application/xml")

        # Clear pending_delete_account if user sends anything else while it's pending
        user_check_delete = get_user(phone_number)
        if user_check_delete:
            from database import get_db_connection, return_db_connection
            conn_check = get_db_connection()
            c_check = conn_check.cursor()
            c_check.execute('SELECT pending_delete_account FROM users WHERE phone_number = %s', (phone_number,))
            result = c_check.fetchone()
            return_db_connection(conn_check)
            if result and result[0]:
                create_or_update_user(phone_number, pending_delete_account=False)
                resp = MessagingResponse()
                resp.message("Account deletion cancelled. Your data is safe!")
                log_interaction(phone_number, incoming_msg, "Delete account cancelled", "delete_account_cancelled", True)
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # CANCELLATION FEEDBACK HANDLING
        # ==========================================
        user_check_cancel = get_user(phone_number)
        if user_check_cancel:
            from database import get_db_connection, return_db_connection
            conn_check = get_db_connection()
            c_check = conn_check.cursor()
            c_check.execute('SELECT pending_cancellation_feedback FROM users WHERE phone_number = %s', (phone_number,))
            cancel_result = c_check.fetchone()
            return_db_connection(conn_check)
            if cancel_result and cancel_result[0]:
                # User has pending cancellation feedback
                msg_upper = incoming_msg.strip().upper()
                if msg_upper == "SKIP":
                    create_or_update_user(phone_number, pending_cancellation_feedback=False)
                    # Don't return - let the message flow through normally
                else:
                    feedback_map = {
                        '1': 'Too expensive',
                        '2': 'Not using enough',
                        '3': 'Missing a feature',
                        '4': 'Other',
                    }
                    feedback_text = feedback_map.get(msg_upper, incoming_msg.strip())
                    # Save as a categorized ticket
                    from services.support_service import create_categorized_ticket
                    create_categorized_ticket(
                        phone_number,
                        f"[CANCELLATION] {feedback_text}",
                        'feedback',
                        'sms'
                    )
                    create_or_update_user(phone_number, pending_cancellation_feedback=False)
                    resp = MessagingResponse()
                    resp.message("Thank you for the feedback! We'll use it to improve Remyndrs. Text UPGRADE anytime to resubscribe.")
                    log_interaction(phone_number, incoming_msg, "Cancellation feedback received", "cancellation_feedback", True)
                    return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # RESET ACCOUNT COMMAND (developer only)
        # ==========================================
        # Normalize phone number for comparison (remove +1 prefix if present)
        normalized_phone = phone_number.replace("+1", "").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
        is_developer = normalized_phone == "8593935374"

        if incoming_msg.upper() in ["RESET ACCOUNT", "RESTART"] and (is_developer or ENVIRONMENT == "staging"):
            logger.info(f"RESET ACCOUNT matched - resetting user (developer={is_developer}, env={ENVIRONMENT})")

            if is_developer:
                logger.info("Developer full reset - deleting all user data")
                try:
                    from database import get_db_connection, return_db_connection
                    conn = get_db_connection()
                    c = conn.cursor()

                    # Delete all user data to simulate brand new user
                    c.execute("DELETE FROM reminders WHERE phone_number = %s", (phone_number,))
                    c.execute("DELETE FROM recurring_reminders WHERE phone_number = %s", (phone_number,))
                    c.execute("DELETE FROM memories WHERE phone_number = %s", (phone_number,))
                    c.execute("DELETE FROM list_items WHERE phone_number = %s", (phone_number,))
                    c.execute("DELETE FROM lists WHERE phone_number = %s", (phone_number,))
                    c.execute("DELETE FROM logs WHERE phone_number = %s", (phone_number,))
                    c.execute("DELETE FROM onboarding_progress WHERE phone_number = %s", (phone_number,))
                    c.execute("DELETE FROM users WHERE phone_number = %s", (phone_number,))

                    conn.commit()
                    return_db_connection(conn)
                    logger.info("Full reset complete - all user data deleted")
                except Exception as e:
                    logger.error(f"Error during full reset: {e}")

            # In staging, reset to step 0 to show the actual new user welcome message
            reset_step = 0 if ENVIRONMENT == "staging" or is_developer else 1

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
                premium_status='free',
                # Reset trial warning flags for new user experience
                trial_warning_7d_sent=False,
                trial_warning_1d_sent=False,
                trial_warning_0d_sent=False,
                # Reset daily summary flags
                daily_summary_prompted=False,
                daily_summary_enabled=False,
                # Reset engagement nudge flags
                five_minute_nudge_sent=False,
                five_minute_nudge_scheduled_at=None,
                post_onboarding_interactions=0,
                # Reset trial info
                trial_info_sent=False,
                # Reset Stripe fields
                stripe_customer_id=None,
                stripe_subscription_id=None,
                subscription_status=None
            )

            log_interaction(phone_number, incoming_msg, "Account reset", "reset", True)

            # Trigger the actual onboarding flow to show welcome message
            if ENVIRONMENT == "staging" or is_developer:
                return handle_onboarding(phone_number, incoming_msg)

            resp = MessagingResponse()
            resp.message("✅ Your account has been reset. Let's start over!\n\nWhat's your first name?")
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # STOP - Twilio reserved keyword (handled by Twilio Messaging Service)
        # Return empty response so only Twilio's message is sent
        # ==========================================
        if incoming_msg.upper() == "STOP":
            logger.info(f"STOP command received from {mask_phone_number(phone_number)}")

            # Mark user as opted out in our database
            mark_user_opted_out(phone_number)

            log_interaction(phone_number, incoming_msg, "[Handled by Twilio]", "stop", True)
            resp = MessagingResponse()
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # START/RESUBSCRIBE COMMAND (Twilio sends basic subscription message)
        # ==========================================
        # Note: "YES" is also used for confirmations, so check if user has pending action first
        if incoming_msg.upper() in ["START", "YES", "UNSTOP"]:
            # Check if user has a pending confirmation (delete, reminder confirmation, etc.)
            user_check = get_user(phone_number)
            pending_delete = get_pending_reminder_delete(phone_number)
            pending_confirmation_check = get_pending_reminder_confirmation(phone_number)
            has_pending_action = pending_delete or pending_confirmation_check

            # Only treat as START if no pending action AND message is START/UNSTOP (not YES)
            # OR if opted_out and saying YES to resubscribe
            is_opted_out = is_user_opted_out(phone_number) if user_check else False

            if not has_pending_action and (incoming_msg.upper() in ["START", "UNSTOP"] or is_opted_out):
                logger.info(f"START command received from {mask_phone_number(phone_number)}")

                # Check if this is a brand new user or user who hasn't completed onboarding
                # If so, let them fall through to the onboarding flow
                if not user_check or not is_user_onboarded(phone_number):
                    logger.info(f"New user or incomplete onboarding - routing to onboarding flow")
                    # Fall through to onboarding check below
                    pass
                else:
                    # Existing onboarded user - clear opted_out if set and welcome back
                    if is_opted_out:
                        create_or_update_user(phone_number, opted_out=False, opted_out_at=None)

                    # Twilio sends basic subscription message, we send the follow-up
                    resp = MessagingResponse()
                    resp.message("Your reminders, lists, and memories are right where you left them. Just text me anytime to pick up where you left off.")
                    log_interaction(phone_number, incoming_msg, "User resubscribed" if is_opted_out else "User greeted", "start", True)
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
                resp.message(staging_prefix(f"You've exited support mode. Your ticket #{ticket_id} is still open - text 'SUPPORT message' anytime to continue the conversation."))
                log_interaction(phone_number, incoming_msg, f"Exited support mode (ticket #{ticket_id})", "support_exit", True)
                return Response(content=str(resp), media_type="application/xml")

            # Handle CLOSE TICKET command - close the ticket
            if msg_upper in ["CLOSE TICKET", "CLOSE"]:
                # Add a system message to the ticket so CS agents can see user closed it
                add_support_message(phone_number, "[User closed the ticket]", 'inbound')
                result = close_ticket_by_phone(phone_number)
                if result['success']:
                    resp = MessagingResponse()
                    resp.message(staging_prefix(f"Your support ticket #{ticket_id} has been closed. Thank you for contacting us! Text 'SUPPORT message' anytime to open a new ticket."))
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
        # POST-ONBOARDING ENGAGEMENT TRACKING
        # ==========================================
        # User is onboarded - cancel any pending nudge and track interaction
        # This runs on every message from an onboarded user
        nudge_cancelled = cancel_engagement_nudge(phone_number)
        if nudge_cancelled:
            logger.info(f"User ...{phone_number[-4:]} texted back - cancelled engagement nudge")
        increment_post_onboarding_interactions(phone_number)

        # ==========================================
        # PRICING & TRIAL QUESTIONS
        # ==========================================
        # Handle pricing questions directly without AI processing
        if is_comparison_question(incoming_msg):
            trial_already_sent = get_trial_info_sent(phone_number)
            if trial_already_sent:
                logger.info(f"Comparison question - trial already explained, sending FAQ for ...{phone_number[-4:]}")
                reply_text = get_comparison_faq_response()
            else:
                logger.info(f"Comparison question - first time, sending full comparison for ...{phone_number[-4:]}")
                reply_text = get_comparison_response()
                mark_trial_info_sent(phone_number)
            log_interaction(phone_number, incoming_msg, reply_text, "pricing_comparison", True)
            resp = MessagingResponse()
            resp.message(staging_prefix(reply_text))
            return Response(content=str(resp), media_type="application/xml")

        if is_pricing_question(incoming_msg):
            trial_already_sent = get_trial_info_sent(phone_number)
            if trial_already_sent:
                logger.info(f"Pricing question - trial already explained, sending FAQ for ...{phone_number[-4:]}")
                reply_text = get_pricing_faq_response()
            else:
                logger.info(f"Pricing question - first time, sending full trial info for ...{phone_number[-4:]}")
                reply_text = get_pricing_response()
                mark_trial_info_sent(phone_number)
            log_interaction(phone_number, incoming_msg, reply_text, "pricing_question", True)
            resp = MessagingResponse()
            resp.message(staging_prefix(reply_text))
            return Response(content=str(resp), media_type="application/xml")

        # Handle simple acknowledgments (e.g., "ok", "thanks" after trial message)
        if is_acknowledgment(incoming_msg):
            reply_text = get_acknowledgment_response()
            log_interaction(phone_number, incoming_msg, reply_text, "acknowledgment", True)
            resp = MessagingResponse()
            resp.message(staging_prefix(reply_text))
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # NEW REMINDER REQUEST DETECTION (must come first)
        # ==========================================
        # Check if message is a NEW reminder request - if so, don't treat as clarification response
        # This prevents "remind me tomorrow at 10am" from being caught by daily summary handler
        is_new_reminder_request = bool(re.search(r'\b(remind|timer|alarm)\b', incoming_msg, re.IGNORECASE))

        # Check for undo/correction commands that should bypass all pending states
        msg_lower_strip = incoming_msg.strip().lower()
        # Also strip trailing punctuation for matching (handles "undo!", "undo...", etc.)
        msg_lower_clean = re.sub(r'[.!?,;:\s]+$', '', msg_lower_strip)
        is_undo_command = msg_lower_clean in ['undo', "that's wrong", 'thats wrong', 'wrong', 'fix that', 'that was wrong', 'not what i meant', 'cancel']

        # ==========================================
        # DAILY SUMMARY RESPONSE (after first action)
        # ==========================================
        # Check if user is responding to daily summary prompt
        # Skip this if it's a new reminder request, undo command, or has pending confirmations/states
        pending_delete_check = get_pending_reminder_delete(phone_number)
        pending_confirm_check = get_pending_reminder_confirmation(phone_number)
        pending_list_check = get_pending_list_item(phone_number)
        has_pending_state = pending_delete_check or (pending_confirm_check and pending_confirm_check.get('type') != 'summary_undo') or pending_list_check

        if not is_new_reminder_request and not is_undo_command and not has_pending_state:
            handled, response_text = handle_daily_summary_response(phone_number, incoming_msg)
            if handled and response_text:
                log_interaction(phone_number, incoming_msg, response_text, "daily_summary_setup", True)
                resp = MessagingResponse()
                resp.message(staging_prefix(response_text))
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # LOW-CONFIDENCE REMINDER CONFIRMATION
        # ==========================================
        # Check if user is responding to a confirmation request for a low-confidence reminder
        pending_confirmation = get_pending_reminder_confirmation(phone_number)
        if pending_confirmation and pending_confirmation.get('type') != 'summary_undo' and not is_new_reminder_request:
            msg_lower = incoming_msg.strip().lower()

            # User confirms the reminder is correct
            if msg_lower in ['yes', 'y', 'correct', 'right', 'yep', 'yeah', 'ok', 'okay']:
                # Log confirmation for calibration tracking
                stored_confidence = pending_confirmation.get('confidence')
                if stored_confidence is not None:
                    CONFIDENCE_THRESHOLD = int(get_setting('confidence_threshold', 70))
                    log_confidence(phone_number, pending_confirmation.get('action', 'reminder'), stored_confidence, CONFIDENCE_THRESHOLD, confirmed=True, user_message=None)

                # Create the reminder as originally parsed
                try:
                    action = pending_confirmation.get('action')
                    if action == 'reminder':
                        # Standard reminder
                        reminder_text = pending_confirmation.get('reminder_text')
                        reminder_date = pending_confirmation.get('reminder_date')
                        confirmation_msg = pending_confirmation.get('confirmation')

                        if not reminder_date:
                            logger.error(f"Missing reminder_date in pending confirmation. Keys: {list(pending_confirmation.keys())}")
                            reply_text = "Sorry, something went wrong with that reminder. Please try creating it again."
                            log_interaction(phone_number, incoming_msg, reply_text, "reminder_confirmed", False)
                        else:
                            user_tz_str = get_user_timezone(phone_number)
                            tz = pytz.timezone(user_tz_str)
                            naive_dt = datetime.strptime(reminder_date, '%Y-%m-%d %H:%M:%S')
                            local_time_str = naive_dt.strftime('%H:%M')
                            aware_dt = tz.localize(naive_dt)
                            utc_dt = aware_dt.astimezone(pytz.UTC)
                            reminder_date_utc = utc_dt.strftime('%Y-%m-%d %H:%M:%S')

                            reminder_id = save_reminder_with_local_time(phone_number, reminder_text, reminder_date_utc, local_time_str, user_tz_str)

                            if reminder_id:
                                reply_text = confirmation_msg or f"Got it! I'll remind you about {reminder_text}."
                                log_interaction(phone_number, incoming_msg, reply_text, "reminder_confirmed", True)
                            else:
                                reply_text = "Sorry, I couldn't save that reminder. Please try again."
                                log_interaction(phone_number, incoming_msg, reply_text, "reminder_confirmed", False)

                    elif action == 'reminder_relative':
                        # Relative time reminder
                        reminder_text = pending_confirmation.get('reminder_text')
                        user_tz_str = get_user_timezone(phone_number)

                        # Use pre-calculated UTC datetime if available (avoids time drift)
                        reminder_date_utc = pending_confirmation.get('reminder_datetime_utc')
                        local_time_str = pending_confirmation.get('local_time')

                        if not reminder_date_utc or not local_time_str:
                            # Fallback: recalculate from offsets
                            offset_minutes = pending_confirmation.get('offset_minutes')
                            offset_days = pending_confirmation.get('offset_days')
                            offset_weeks = pending_confirmation.get('offset_weeks')
                            offset_months = pending_confirmation.get('offset_months')

                            tz = pytz.timezone(user_tz_str)
                            now_local = datetime.now(tz)

                            if offset_minutes:
                                reminder_dt = now_local + timedelta(minutes=offset_minutes)
                            elif offset_days:
                                reminder_dt = now_local + timedelta(days=offset_days)
                            elif offset_weeks:
                                reminder_dt = now_local + timedelta(weeks=offset_weeks)
                            elif offset_months:
                                reminder_dt = now_local + timedelta(days=offset_months * 30)
                            else:
                                reminder_dt = now_local + timedelta(hours=1)

                            local_time_str = reminder_dt.strftime('%H:%M')
                            reminder_date_utc = reminder_dt.astimezone(pytz.UTC).strftime('%Y-%m-%d %H:%M:%S')

                        reminder_id = save_reminder_with_local_time(phone_number, reminder_text, reminder_date_utc, local_time_str, user_tz_str)

                        if reminder_id:
                            tz = pytz.timezone(user_tz_str)
                            utc_dt = datetime.strptime(reminder_date_utc, '%Y-%m-%d %H:%M:%S')
                            utc_dt = pytz.UTC.localize(utc_dt)
                            local_dt = utc_dt.astimezone(tz)
                            readable_date = local_dt.strftime('%A, %B %d, %Y at %-I:%M %p')
                            reply_text = f"Got it! I'll remind you on {readable_date} {format_reminder_confirmation(reminder_text)}."
                            log_interaction(phone_number, incoming_msg, reply_text, "reminder_confirmed", True)
                        else:
                            reply_text = "Sorry, I couldn't save that reminder. Please try again."
                            log_interaction(phone_number, incoming_msg, reply_text, "reminder_confirmed", False)

                    elif action == 'reminder_recurring':
                        # Recurring reminder
                        reminder_text = pending_confirmation.get('reminder_text')
                        recurrence_type = pending_confirmation.get('recurrence_type')
                        recurrence_day = pending_confirmation.get('recurrence_day')
                        time_str = pending_confirmation.get('time')

                        user_tz_str = get_user_timezone(phone_number)
                        recurring_id = save_recurring_reminder(phone_number, reminder_text, recurrence_type, recurrence_day, time_str, user_tz_str)

                        if recurring_id:
                            reply_text = f"Got it! I'll remind you {recurrence_type} at {time_str} {format_reminder_confirmation(reminder_text)}."
                            log_interaction(phone_number, incoming_msg, reply_text, "reminder_confirmed", True)
                        else:
                            reply_text = "Sorry, I couldn't save that recurring reminder. Please try again."
                            log_interaction(phone_number, incoming_msg, reply_text, "reminder_confirmed", False)
                    else:
                        logger.error(f"Unrecognized pending confirmation action: '{action}'. Keys: {list(pending_confirmation.keys())}")
                        reply_text = "Sorry, something went wrong. Please try again."
                        log_interaction(phone_number, incoming_msg, reply_text, "reminder_confirmed", False)

                    # Clear the pending confirmation
                    create_or_update_user(phone_number, pending_reminder_confirmation=None)
                    resp = MessagingResponse()
                    resp.message(staging_prefix(reply_text))
                    return Response(content=str(resp), media_type="application/xml")

                except Exception as e:
                    logger.error(f"Error processing confirmed reminder: {e}. Action: {pending_confirmation.get('action')}, Keys: {list(pending_confirmation.keys())}", exc_info=True)
                    create_or_update_user(phone_number, pending_reminder_confirmation=None)
                    resp = MessagingResponse()
                    resp.message(staging_prefix("Sorry, something went wrong. Please try again."))
                    return Response(content=str(resp), media_type="application/xml")

            # User says it's wrong - ask for clarification
            elif msg_lower in ['no', 'n', 'wrong', 'nope', "that's wrong", 'thats wrong', 'incorrect', 'not right']:
                # Log rejection for calibration tracking
                stored_confidence = pending_confirmation.get('confidence')
                if stored_confidence is not None:
                    CONFIDENCE_THRESHOLD = int(get_setting('confidence_threshold', 70))
                    log_confidence(phone_number, pending_confirmation.get('action', 'reminder'), stored_confidence, CONFIDENCE_THRESHOLD, confirmed=False, user_message=None)

                create_or_update_user(phone_number, pending_reminder_confirmation=None)
                resp = MessagingResponse()
                resp.message(staging_prefix("No problem! Please tell me again what you'd like to be reminded about, and when.\n\nTip: Try something like \"remind me Tuesday at 3pm to call the dentist\""))
                log_interaction(phone_number, incoming_msg, "Reminder confirmation rejected", "reminder_rejected", True)
                return Response(content=str(resp), media_type="application/xml")

            # User provides a correction directly - treat as new request
            # (If they say something other than yes/no, assume it's a new/corrected request and let it fall through)

        # ==========================================
        # UNDO / THAT'S WRONG FALLBACK COMMANDS
        # ==========================================
        # Allow users to undo or correct mistakes at any time
        # But skip if there's a pending state that handles CANCEL itself
        pending_list_item_for_add = get_pending_list_item(phone_number)
        user_for_undo = get_user(phone_number)
        pending_delete_for_undo = user_for_undo and user_for_undo[9] if user_for_undo else False
        has_pending_add = pending_list_item_for_add and not pending_delete_for_undo
        # Also skip if there's a pending_reminder_delete (delete confirmation) - let that handler deal with cancel
        pending_reminder_del = get_pending_reminder_delete(phone_number)
        has_pending_delete_confirm = pending_reminder_del is not None
        # Also skip if there's a pending_reminder_time (clarify_time flow) - let that handler deal with cancel
        has_pending_time_clarify = user_for_undo and len(user_for_undo) > 11 and user_for_undo[11] and user_for_undo[11] != "NEEDS_TIME"

        if is_undo_command and not has_pending_add and not has_pending_delete_confirm and not has_pending_time_clarify:
            # First check if there's a pending confirmation to cancel
            if pending_confirmation:
                # Check if this is a summary_undo type (from SUMMARY TIME command)
                if pending_confirmation.get('type') == 'summary_undo':
                    # Revert to previous summary settings
                    previous_enabled = pending_confirmation.get('previous_enabled', False)
                    previous_time = pending_confirmation.get('previous_time')

                    create_or_update_user(
                        phone_number,
                        daily_summary_enabled=previous_enabled,
                        daily_summary_time=previous_time,
                        pending_reminder_confirmation=None
                    )

                    if previous_enabled and previous_time:
                        # Format previous time for display
                        h, m = map(int, previous_time.split(':'))
                        disp_am_pm = 'AM' if h < 12 else 'PM'
                        disp_h = h if h <= 12 else h - 12
                        if disp_h == 0:
                            disp_h = 12
                        resp = MessagingResponse()
                        resp.message(staging_prefix(f"Daily summary reverted to {disp_h}:{m:02d} {disp_am_pm}."))
                    elif previous_enabled:
                        resp = MessagingResponse()
                        resp.message(staging_prefix("Daily summary reverted to previous settings."))
                    else:
                        resp = MessagingResponse()
                        resp.message(staging_prefix("Daily summary turned off."))

                    log_interaction(phone_number, incoming_msg, "Summary settings reverted via undo", "undo_summary", True)
                    return Response(content=str(resp), media_type="application/xml")
                else:
                    # Regular pending confirmation - cancel it
                    create_or_update_user(phone_number, pending_reminder_confirmation=None)
                    resp = MessagingResponse()
                    resp.message(staging_prefix("Got it, cancelled! Please tell me what you'd like instead."))
                    log_interaction(phone_number, incoming_msg, "Pending confirmation cancelled via undo", "undo", True)
                    return Response(content=str(resp), media_type="application/xml")

            # No pending confirmation - check if we can offer to undo the last reminder
            from models.reminder import get_most_recent_reminder
            recent_reminder = get_most_recent_reminder(phone_number)
            if recent_reminder:
                reminder_id, reminder_text, reminder_date = recent_reminder
                # Store for confirmation (using same format as other delete confirmations)
                confirm_data = json.dumps({
                    'awaiting_confirmation': True,
                    'type': 'reminder',
                    'id': reminder_id,
                    'text': reminder_text
                })
                create_or_update_user(phone_number, pending_reminder_delete=confirm_data)
                resp = MessagingResponse()
                resp.message(staging_prefix(f"Delete your most recent reminder: \"{reminder_text}\"?\n\nReply YES to delete or NO to keep it."))
                log_interaction(phone_number, incoming_msg, "Offered to undo recent reminder", "undo_offer", True)
                return Response(content=str(resp), media_type="application/xml")
            else:
                resp = MessagingResponse()
                resp.message(staging_prefix("Nothing to undo! How can I help you?"))
                log_interaction(phone_number, incoming_msg, "No recent reminder to undo", "undo_nothing", True)
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # AM/PM CLARIFICATION RESPONSE
        # ==========================================
        # Check if user has a pending reminder and their message contains AM or PM
        user = get_user(phone_number)
        msg_upper = incoming_msg.upper()
        # Check for AM/PM in various formats: "8am", "8 am", "8:00am", "8a", "8:00a", "a.m.", etc.
        # Also recognize "morning" as AM and "afternoon"/"evening"/"night" as PM
        has_am_pm = bool(re.search(r'\d\s*(am|pm|a\.m\.|p\.m\.|a|p)\b', incoming_msg, re.IGNORECASE))
        if not has_am_pm:
            has_am_pm = bool(re.search(r'\b(morning|afternoon|evening|night)\b', incoming_msg, re.IGNORECASE))

        # If it's a new reminder request, clear any pending reminder states to avoid context bleed
        if is_new_reminder_request:
            create_or_update_user(phone_number, pending_reminder_text=None, pending_reminder_time=None, pending_reminder_date=None)

        # Check clarify_date_time flow FIRST (pending_reminder_date set)
        # Only if NOT a new reminder request (user is answering a clarification question)
        pending_date_data = get_pending_reminder_date(phone_number)
        if pending_date_data and has_am_pm and not is_new_reminder_request:
            pending_text = pending_date_data['text']
            pending_date = pending_date_data['date']  # YYYY-MM-DD format

            try:
                user_tz_str = get_user_timezone(phone_number)
                tz = pytz.timezone(user_tz_str)

                # Parse time from user message (e.g., "8am", "8:00 AM", "3:30pm", "8a", "8p")
                time_match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|a|p)\b', incoming_msg, re.IGNORECASE)
                if time_match:
                    hour = int(time_match.group(1))
                    minute = int(time_match.group(2)) if time_match.group(2) else 0
                    am_pm_raw = time_match.group(3).lower().replace('.', '')
                    am_pm = 'am' if am_pm_raw in ['am', 'a'] else 'pm'

                    # Convert to 24-hour format
                    if am_pm == 'pm' and hour != 12:
                        hour += 12
                    elif am_pm == 'am' and hour == 12:
                        hour = 0

                    # Parse the pending date and combine with time
                    reminder_date_obj = datetime.strptime(pending_date, '%Y-%m-%d')
                    reminder_datetime = reminder_date_obj.replace(hour=hour, minute=minute, second=0, microsecond=0)

                    # Localize to user's timezone
                    aware_dt = tz.localize(reminder_datetime)

                    # Convert to UTC for storage
                    utc_dt = aware_dt.astimezone(pytz.UTC)
                    reminder_date_str = utc_dt.strftime('%Y-%m-%d %H:%M:%S')

                    # Extract local time for timezone recalculation support
                    local_time_str = f"{hour:02d}:{minute:02d}"

                    # Save the reminder
                    save_reminder_with_local_time(
                        phone_number, pending_text, reminder_date_str,
                        local_time_str, user_tz_str
                    )

                    # Format confirmation
                    readable_date = aware_dt.strftime('%A, %B %d at %I:%M %p')
                    reply_text = f"I'll remind you on {readable_date} {format_reminder_confirmation(pending_text)}."

                    # Clear pending reminder data
                    create_or_update_user(phone_number, pending_reminder_text=None, pending_reminder_date=None)

                    log_interaction(phone_number, incoming_msg, reply_text, "reminder_date_time_confirmed", True)
                    resp = MessagingResponse()
                    resp.message(staging_prefix(reply_text))
                    return Response(content=str(resp), media_type="application/xml")

            except Exception as e:
                logger.error(f"Error processing date/time response: {e}")
                resp = MessagingResponse()
                resp.message(staging_prefix("Sorry, I had trouble setting that reminder. Please try again with a time like '8am' or '3:30pm'."))
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # VAGUE TIME FOLLOW-UP (clarify_specific_time flow)
        # ==========================================
        # Check if pending_reminder_time is "NEEDS_TIME" (from vague time like "in a bit")
        if user and len(user) > 11 and user[10] and user[11] == "NEEDS_TIME" and not is_new_reminder_request:
            pending_text = user[10]

            # Check for simple time like "3p", "3pm", "at 3pm", "8am"
            simple_time_match = re.match(r'^(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m?\.?)$', incoming_msg.strip(), re.IGNORECASE)
            if simple_time_match:
                hour = int(simple_time_match.group(1))
                minute = int(simple_time_match.group(2)) if simple_time_match.group(2) else 0
                am_pm_raw = simple_time_match.group(3).lower()
                am_pm = "AM" if 'a' in am_pm_raw else "PM"

                try:
                    user_time = get_user_current_time(phone_number)

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
                    reply_text = f"I'll remind you on {readable_date} {format_reminder_confirmation(pending_text)}."

                    # Clear pending reminder
                    create_or_update_user(phone_number, pending_reminder_text=None, pending_reminder_time=None)

                    # Check if this is user's first action and prompt for daily summary
                    if should_prompt_daily_summary(phone_number):
                        reply_text = get_daily_summary_prompt_message(reply_text)
                        mark_daily_summary_prompted(phone_number)

                    log_interaction(phone_number, incoming_msg, reply_text, "vague_time_confirmed", True)
                    resp = MessagingResponse()
                    resp.message(staging_prefix(reply_text))
                    return Response(content=str(resp), media_type="application/xml")

                except Exception as e:
                    logger.error(f"Error processing vague time follow-up: {e}")
                    # Fall through to AI processing

            # Check for cancel commands
            cancel_phrases = ['cancel', 'nevermind', 'never mind', 'skip', 'forget it', 'no thanks', 'undo']
            if incoming_msg.strip().lower() in cancel_phrases:
                create_or_update_user(phone_number, pending_reminder_text=None, pending_reminder_time=None)
                resp = MessagingResponse()
                resp.message(staging_prefix("Got it, cancelled. What would you like to do?"))
                log_interaction(phone_number, incoming_msg, "Pending reminder cancelled", "pending_cancel", True)
                return Response(content=str(resp), media_type="application/xml")

            # Check if message looks like a complex time (e.g., "in 30 minutes", "tomorrow", etc.)
            # These should be processed by AI with the pending reminder context
            looks_like_time = bool(re.search(
                r'\b(in\s+\d+|tomorrow|tonight|today|next\s+\w+|this\s+(morning|afternoon|evening)|at\s+\d)',
                incoming_msg, re.IGNORECASE
            ))

            if looks_like_time:
                # Complex time like "in 30 minutes" or "tomorrow at 9am"
                # Reconstruct the request and let AI process it
                incoming_msg = f"remind me to {pending_text} {incoming_msg}"
                create_or_update_user(phone_number, pending_reminder_text=None, pending_reminder_time=None)
                # Fall through to AI processing with reconstructed message
            else:
                # Message is unrelated - remind user about the pending time clarification
                reminder = f"I still need a time for your reminder: '{pending_text}'\n\nWhat time works? (e.g., '3pm', 'in 30 minutes')\n\n(Say 'cancel' to skip)"
                resp = MessagingResponse()
                resp.message(staging_prefix(reminder))
                log_interaction(phone_number, incoming_msg, reminder, "pending_state_reminder", True)
                return Response(content=str(resp), media_type="application/xml")

        # clarify_time flow - only if pending_reminder_time is set (not pending_reminder_date)
        # Check for AM/PM with number (8am) OR standalone AM/PM response
        is_standalone_am_pm = bool(re.match(r'^(am|pm|a\.m\.|p\.m\.)\.?$', incoming_msg.strip(), re.IGNORECASE))

        # Note: is_new_reminder_request was already defined and pending states were cleared above
        if user and len(user) > 11 and user[10] and user[11] and user[11] != "NEEDS_TIME" and (has_am_pm or is_standalone_am_pm) and not is_new_reminder_request:  # pending_reminder_text AND pending_reminder_time exist, but NOT a new reminder request
            pending_text = user[10]
            pending_time = user[11]

            # Detect AM vs PM from various formats (am, a.m., a, etc.), standalone, or words like "morning"
            am_match = re.search(r'(^|[\d\s])(am|a\.m\.?)(\b|$)', incoming_msg, re.IGNORECASE)
            morning_match = re.search(r'\bmorning\b', incoming_msg, re.IGNORECASE)
            pm_word_match = re.search(r'\b(afternoon|evening|night)\b', incoming_msg, re.IGNORECASE)
            if am_match or morning_match:
                am_pm = "AM"
            elif pm_word_match:
                am_pm = "PM"
            else:
                am_pm = "PM"  # Default to PM if no indicator found

            try:
                user_time = get_user_current_time(phone_number)
                user_tz = get_user_timezone(phone_number)

                # Clean up the pending_time - remove any existing AM/PM and standalone letters
                clean_time = pending_time.upper().replace("AM", "").replace("PM", "").replace("A.M.", "").replace("P.M.", "").strip()
                # Also remove any trailing letters (handles "3P", "8A", etc.)
                clean_time = re.sub(r'[AP]$', '', clean_time).strip()

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
                reply_text = f"I'll remind you on {readable_date} {format_reminder_confirmation(pending_text)}."

                # Clear pending reminder
                create_or_update_user(phone_number, pending_reminder_text=None, pending_reminder_time=None)

                # Check if this is user's first action and prompt for daily summary
                if should_prompt_daily_summary(phone_number):
                    reply_text = get_daily_summary_prompt_message(reply_text)
                    mark_daily_summary_prompted(phone_number)

                log_interaction(phone_number, incoming_msg, reply_text, "reminder_confirmed", True)
                resp = MessagingResponse()
                resp.message(staging_prefix(reply_text))
                return Response(content=str(resp), media_type="application/xml")

            except Exception as e:
                logger.error(f"Error processing time: {e}")
                # Clear pending states so user can start fresh
                create_or_update_user(phone_number, pending_reminder_text=None, pending_reminder_time=None, pending_reminder_date=None)
                resp = MessagingResponse()
                resp.message(staging_prefix("Sorry, I had trouble setting that reminder. Please try again with a clear time like '3pm' or '3:00 PM'."))
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # PENDING DELETE SELECTION (reminders, list items, memories)
        # ==========================================
        # Check if user has pending deletion and sent a number or CANCEL
        pending_delete_data = get_pending_reminder_delete(phone_number)
        if pending_delete_data:
            # Handle CANCEL
            if incoming_msg.strip().upper() in ["CANCEL", "NO"]:
                create_or_update_user(phone_number, pending_reminder_delete=None)
                resp = MessagingResponse()
                resp.message("Cancelled.")
                return Response(content=str(resp), media_type="application/xml")

            try:
                delete_data = json.loads(pending_delete_data)
            except json.JSONDecodeError:
                delete_data = None

            # Handle YES confirmation for single-item delete
            if delete_data and isinstance(delete_data, dict) and delete_data.get('awaiting_confirmation'):
                if incoming_msg.strip().upper() == "YES":
                    delete_type = delete_data.get('type', 'reminder')
                    reply_msg = None

                    if delete_type == 'reminder':
                        if delete_reminder(phone_number, delete_data['id']):
                            reply_msg = f"Deleted reminder: {delete_data['text']}"
                            # If this was a recurring reminder instance, also delete the recurring pattern
                            recurring_id = delete_data.get('recurring_id')
                            if recurring_id:
                                if delete_recurring_reminder(recurring_id, phone_number):
                                    reply_msg += " (and its recurring schedule)"
                        else:
                            reply_msg = "Couldn't delete that reminder."

                    elif delete_type == 'recurring':
                        if delete_recurring_reminder(delete_data['id'], phone_number):
                            reply_msg = f"Deleted recurring reminder: {delete_data['text']}"
                        else:
                            reply_msg = "Couldn't delete that recurring reminder."

                    elif delete_type == 'list_item':
                        if delete_list_item(phone_number, delete_data['list_name'], delete_data['text']):
                            reply_msg = f"Removed '{delete_data['text']}' from {delete_data['list_name']}"
                        else:
                            reply_msg = "Couldn't delete that item."

                    create_or_update_user(phone_number, pending_reminder_delete=None)
                    resp = MessagingResponse()
                    resp.message(staging_prefix(reply_msg))
                    log_interaction(phone_number, incoming_msg, reply_msg, f"delete_{delete_type}_confirmed", True)
                    return Response(content=str(resp), media_type="application/xml")
                else:
                    # Not YES or CANCEL - clear pending state and let message be processed normally
                    # This allows users to start a new request without explicitly cancelling
                    create_or_update_user(phone_number, pending_reminder_delete=None)
                    # Fall through to normal message processing

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

                        elif delete_type == 'list':
                            if delete_list(phone_number, selected['text']):
                                reply_msg = f"Deleted list: {selected['text']}"
                            else:
                                reply_msg = "Couldn't delete that list."

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
        # PENDING LIST CREATE (duplicate list handling)
        # ==========================================
        # Check if user was asked about duplicate list
        pending_list_name = get_pending_list_create(phone_number)
        if pending_list_name:
            msg_lower = incoming_msg.strip().lower()

            # Check for "add" responses
            add_keywords = ['add', 'existing', 'use', 'yes', 'that one', 'add to it', 'add items']
            wants_add = any(kw in msg_lower for kw in add_keywords)

            # Check for "new" responses
            new_keywords = ['new', 'create', 'another', 'different', 'new one', 'create new']
            wants_new = any(kw in msg_lower for kw in new_keywords)

            if wants_add and not wants_new:
                # User wants to add to existing list - set it as active
                existing_list = get_list_by_name(phone_number, pending_list_name)
                if existing_list:
                    list_id, actual_name = existing_list
                    create_or_update_user(phone_number, pending_list_create=None, last_active_list=actual_name)
                    reply_msg = f"Great! Your {actual_name} is ready. What would you like to add?"
                else:
                    reply_msg = "That list no longer exists. Would you like to create it?"
                    create_or_update_user(phone_number, pending_list_create=None)

                resp = MessagingResponse()
                resp.message(staging_prefix(reply_msg))
                log_interaction(phone_number, incoming_msg, reply_msg, "list_duplicate_add_existing", True)
                return Response(content=str(resp), media_type="application/xml")

            elif wants_new:
                # User wants to create a new list with incremented name
                # First, rename the original list to #1 if it doesn't already have a number
                original_list = get_list_by_name(phone_number, pending_list_name)
                if original_list and not re.search(r'#\s*\d+$', pending_list_name):
                    # Rename original to #1
                    new_original_name = f"{pending_list_name} #1"
                    rename_list(phone_number, pending_list_name, new_original_name)
                    logger.info(f"Renamed original list '{pending_list_name}' to '{new_original_name}'")

                # Now create the new list as #2
                new_list_name = get_next_available_list_name(phone_number, pending_list_name)
                create_list(phone_number, new_list_name)
                create_or_update_user(phone_number, pending_list_create=None, last_active_list=new_list_name)
                reply_msg = f"Created your {new_list_name}!"

                resp = MessagingResponse()
                resp.message(staging_prefix(reply_msg))
                log_interaction(phone_number, incoming_msg, reply_msg, "list_duplicate_create_new", True)
                return Response(content=str(resp), media_type="application/xml")

            # If unclear, let the AI handle it but clear the pending state
            # so the user can try again with "create a grocery list"
            create_or_update_user(phone_number, pending_list_create=None)

        # ==========================================
        # PENDING LIST ITEM SELECTION
        # ==========================================
        # Check if user has a pending list item and sent a number
        # But NOT if we're in a pending delete flow (pending_list_item stores list name for deletion)
        pending_item = get_pending_list_item(phone_number)
        user_for_pending = get_user(phone_number)
        pending_delete_flag = user_for_pending and user_for_pending[9] if user_for_pending else False
        if pending_item and not pending_delete_flag:
            # Handle CANCEL for pending list item
            if incoming_msg.strip().upper() in ["CANCEL", "NO", "NEVERMIND", "NEVER MIND"]:
                create_or_update_user(phone_number, pending_list_item=None)
                resp = MessagingResponse()
                resp.message(staging_prefix("Cancelled."))
                log_interaction(phone_number, incoming_msg, "Cancelled pending list item", "cancel_list_item", True)
                return Response(content=str(resp), media_type="application/xml")
            if incoming_msg.strip().isdigit():
                list_num = int(incoming_msg.strip())
                lists = get_lists(phone_number)
                if 1 <= list_num <= len(lists):
                    selected_list = lists[list_num - 1]
                    list_id = selected_list[0]
                    list_name = selected_list[1]

                    # Parse multiple items from the pending item
                    items_to_add = parse_list_items(pending_item, phone_number)

                    # Check item limit using tier-aware limits
                    from services.tier_service import (
                        get_tier_limits, get_user_tier,
                        format_list_item_limit_message, add_list_item_counter_to_message
                    )
                    tier_limits = get_tier_limits(get_user_tier(phone_number))
                    max_items = tier_limits['max_items_per_list']
                    item_count = get_item_count(list_id)
                    available_slots = max_items - item_count

                    if available_slots <= 0:
                        resp = MessagingResponse()
                        # Use Level 4 formatter for clear WHY-WHAT-HOW message
                        reply_msg = format_list_item_limit_message(
                            phone_number, list_name, items_to_add, 0
                        )
                        resp.message(reply_msg)
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
                    # Handle partial or full adds with progressive education
                    if len(added_items) < len(items_to_add):
                        # Some items skipped - use Level 4 formatter
                        reply_msg = format_list_item_limit_message(
                            phone_number, list_name, items_to_add, len(added_items)
                        )
                    else:
                        # All items added successfully
                        if len(added_items) == 1:
                            base_reply = f"Added {added_items[0]} to your {list_name}"
                        else:
                            base_reply = f"Added {len(added_items)} items to your {list_name}: {', '.join(added_items)}"

                        # Add progressive counter
                        reply_msg = add_list_item_counter_to_message(phone_number, list_id, base_reply)

                    resp.message(reply_msg)
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
        # But NOT if we're in a pending multi-delete flow
        user_check = get_user(phone_number)
        pending_delete_active = user_check and user_check[9] if user_check else False
        if incoming_msg.strip().isdigit() and not pending_delete_active:
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
        # DELETE REMINDERS (show list to pick from)
        # ==========================================
        # Handle "Delete reminders", "Cancel reminders", etc. - shows numbered list
        if msg_upper in ["DELETE REMINDERS", "DELETE MY REMINDERS", "CANCEL REMINDERS", "CANCEL MY REMINDERS", "REMOVE REMINDERS", "REMOVE MY REMINDERS"]:
            reminders = get_user_reminders(phone_number)
            pending = [r for r in reminders if not r[4]]  # r[4] is 'sent' flag

            if not pending:
                resp = MessagingResponse()
                resp.message(staging_prefix("You don't have any pending reminders to delete."))
                log_interaction(phone_number, incoming_msg, "No reminders to delete", "delete_reminders_list", True)
                return Response(content=str(resp), media_type="application/xml")

            # Build numbered list and store options for selection
            user_tz = get_user_timezone(phone_number)
            tz = pytz.timezone(user_tz)
            lines = ["Which reminder would you like to delete?\n"]
            delete_options = []

            for i, reminder in enumerate(pending, 1):
                reminder_id, reminder_date, reminder_text, recurring_id, sent = reminder
                # Convert UTC to user timezone for display
                if isinstance(reminder_date, str):
                    utc_dt = datetime.strptime(reminder_date, '%Y-%m-%d %H:%M:%S')
                else:
                    utc_dt = reminder_date
                utc_dt = pytz.UTC.localize(utc_dt)
                local_dt = utc_dt.astimezone(tz)
                formatted_date = local_dt.strftime('%b %d at %I:%M %p')

                prefix = "[R] " if recurring_id else ""
                lines.append(f"{i}. {prefix}{reminder_text}\n   {formatted_date}")
                delete_options.append({
                    'type': 'reminder',
                    'id': reminder_id,
                    'text': reminder_text,
                    'recurring_id': recurring_id
                })

            lines.append("\nReply with a number to delete, or CANCEL")
            reply_msg = "\n".join(lines)

            # Store options for number selection
            create_or_update_user(phone_number, pending_reminder_delete=json.dumps(delete_options))

            resp = MessagingResponse()
            resp.message(staging_prefix(reply_msg))
            log_interaction(phone_number, incoming_msg, "Showing delete reminders list", "delete_reminders_list", True)
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # DELETE MEMORIES (show list to pick from)
        # ==========================================
        # Handle "Delete memories", "Forget memories", etc. - shows numbered list
        if msg_upper in ["DELETE MEMORIES", "DELETE MY MEMORIES", "FORGET MEMORIES", "FORGET MY MEMORIES", "REMOVE MEMORIES", "REMOVE MY MEMORIES"]:
            memories = get_memories(phone_number)

            if not memories:
                resp = MessagingResponse()
                resp.message(staging_prefix("You don't have any memories to delete."))
                log_interaction(phone_number, incoming_msg, "No memories to delete", "delete_memories_list", True)
                return Response(content=str(resp), media_type="application/xml")

            # Build numbered list and store options for selection
            # Tuple format: (id, memory_text, parsed_data, created_at)
            lines = ["Which memory would you like to delete?\n"]
            delete_options = []

            for i, memory in enumerate(memories[:20], 1):  # Limit to 20 for readability
                memory_id, memory_text, parsed_data, created_at = memory
                # Truncate long memories for display
                display_text = memory_text[:50] + "..." if len(memory_text) > 50 else memory_text
                lines.append(f"{i}. {display_text}")
                delete_options.append({
                    'type': 'memory',
                    'id': memory_id,
                    'text': memory_text
                })

            lines.append("\nReply with a number to delete, or CANCEL")
            reply_msg = "\n".join(lines)

            # Store options for number selection (reuses pending_reminder_delete)
            create_or_update_user(phone_number, pending_reminder_delete=json.dumps(delete_options))

            resp = MessagingResponse()
            resp.message(staging_prefix(reply_msg))
            log_interaction(phone_number, incoming_msg, "Showing delete memories list", "delete_memories_list", True)
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # DELETE LISTS (show list to pick from)
        # ==========================================
        # Handle "Delete lists", "Remove lists", etc. - shows numbered list of lists
        if msg_upper in ["DELETE LISTS", "DELETE MY LISTS", "REMOVE LISTS", "REMOVE MY LISTS"]:
            lists = get_lists(phone_number)

            if not lists:
                resp = MessagingResponse()
                resp.message(staging_prefix("You don't have any lists to delete."))
                log_interaction(phone_number, incoming_msg, "No lists to delete", "delete_lists_list", True)
                return Response(content=str(resp), media_type="application/xml")

            # Build numbered list and store options for selection
            # get_lists returns: (list_id, list_name, item_count, completed_count)
            lines = ["Which list would you like to delete?\n"]
            delete_options = []

            for i, lst in enumerate(lists, 1):
                list_id, list_name, item_count, completed_count = lst
                lines.append(f"{i}. {list_name} ({item_count} items)")
                delete_options.append({
                    'type': 'list',
                    'id': list_id,
                    'text': list_name
                })

            lines.append("\nReply with a number to delete, or CANCEL")
            reply_msg = "\n".join(lines)

            # Store options for number selection (reuses pending_reminder_delete)
            create_or_update_user(phone_number, pending_reminder_delete=json.dumps(delete_options))

            resp = MessagingResponse()
            resp.message(staging_prefix(reply_msg))
            log_interaction(phone_number, incoming_msg, "Showing delete lists list", "delete_lists_list", True)
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
                # Delete from recurring reminders list - ask for confirmation first
                recurring_list = get_recurring_reminders(phone_number, include_inactive=True)
                if recurring_list and 1 <= item_num <= len(recurring_list):
                    r = recurring_list[item_num - 1]
                    # Store pending delete and ask for confirmation
                    confirm_data = json.dumps({
                        'awaiting_confirmation': True,
                        'type': 'recurring',
                        'id': r['id'],
                        'text': r['reminder_text']
                    })
                    create_or_update_user(phone_number, pending_reminder_delete=confirm_data, last_active_list=None)
                    resp = MessagingResponse()
                    resp.message(f"Delete recurring reminder: {r['reminder_text']}?\n\nReply YES to confirm or CANCEL to keep it.")
                    log_interaction(phone_number, incoming_msg, "Asking delete confirmation", "delete_recurring_confirm", True)
                    return Response(content=str(resp), media_type="application/xml")
                else:
                    resp = MessagingResponse()
                    resp.message(f"Please enter a number between 1 and {len(recurring_list)}. Text 'MY RECURRING' to see the list.")
                    return Response(content=str(resp), media_type="application/xml")

            delete_options = []

            # UX PRIORITY: If user is viewing a specific list, ONLY delete from that list
            # Don't show options from reminders/memories - that's confusing
            if last_active and last_active not in ("__RECURRING__", "__LISTS__"):
                # User is viewing a specific list - delete directly from it
                list_info = get_list_by_name(phone_number, last_active)
                if list_info:
                    list_id = list_info[0]
                    list_name = list_info[1]
                    items = get_list_items(list_id)
                    if items and 1 <= item_num <= len(items):
                        item_id, item_text, _ = items[item_num - 1]
                        # Single item from active list - ask for confirmation
                        confirm_data = json.dumps({
                            'awaiting_confirmation': True,
                            'type': 'list_item',
                            'list_name': list_name,
                            'text': item_text
                        })
                        create_or_update_user(phone_number, pending_reminder_delete=confirm_data)
                        resp = MessagingResponse()
                        resp.message(f"Remove '{item_text}' from {list_name}?\n\nReply YES to confirm or CANCEL to keep it.")
                        log_interaction(phone_number, incoming_msg, "Delete from active list", "delete_list_item_confirm", True)
                        return Response(content=str(resp), media_type="application/xml")
                    else:
                        resp = MessagingResponse()
                        resp.message(f"Your {list_name} doesn't have an item #{item_num}.")
                        return Response(content=str(resp), media_type="application/xml")

            # No active list context - show options from all types
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

            # Check all lists for items at this position
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
                        'display': f"'{item_text}' from {list_name}"
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
        command, message_text = parse_command(incoming_msg, known_commands=["FEEDBACK"])
        if command == "FEEDBACK":
            feedback_message = message_text.strip()
            if feedback_message:
                # Route through support tickets with category='feedback'
                from services.support_service import create_categorized_ticket
                result = create_categorized_ticket(phone_number, feedback_message, 'feedback', 'sms')
                if result['success']:
                    resp = MessagingResponse()
                    resp.message("Thank you for your feedback! We appreciate you taking the time to share your thoughts with us.")
                    log_interaction(phone_number, incoming_msg, f"Feedback ticket #{result['ticket_id']}", "feedback", True)
                    return Response(content=str(resp), media_type="application/xml")
                else:
                    resp = MessagingResponse()
                    resp.message("Sorry, there was an error saving your feedback. Please try again later.")
                    return Response(content=str(resp), media_type="application/xml")
            else:
                resp = MessagingResponse()
                resp.message("Please include your feedback after 'Feedback'. For example: 'Feedback I love this app!'")
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # BUG REPORT HANDLING (route to feedback)
        # ==========================================
        command, message_text = parse_command(incoming_msg, known_commands=["BUG"])
        if command == "BUG":
            bug_message = message_text.strip()
            if bug_message:
                # Route through support tickets with category='bug'
                from services.support_service import create_categorized_ticket
                result = create_categorized_ticket(phone_number, bug_message, 'bug', 'sms')
                if result['success']:
                    resp = MessagingResponse()
                    resp.message("Thank you for reporting this bug! Our team will look into it.")
                    log_interaction(phone_number, incoming_msg, f"Bug ticket #{result['ticket_id']}", "bug_report", True)
                    return Response(content=str(resp), media_type="application/xml")
                else:
                    resp = MessagingResponse()
                    resp.message("Sorry, there was an error saving your bug report. Please try again later.")
                    return Response(content=str(resp), media_type="application/xml")
            else:
                resp = MessagingResponse()
                resp.message("Please describe the bug after 'Bug'. For example: 'Bug my reminder didn't go off'")
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # EXPORT COMMAND - Email user their data
        # ==========================================
        if incoming_msg.upper() == "EXPORT":
            user = get_user(phone_number)
            user_email = user.get('email') if user else None

            if not user_email:
                resp = MessagingResponse()
                resp.message("We don't have an email address on file for your account. Please contact support@remyndrs.com to request a data export.")
                log_interaction(phone_number, incoming_msg, "Export - no email on file", "export_no_email", False)
                return Response(content=str(resp), media_type="application/xml")

            try:
                from services.export_service import export_and_email_user_data
                result = export_and_email_user_data(phone_number, user_email)
                if result:
                    resp = MessagingResponse()
                    resp.message(f"Your data export has been emailed to your address on file. Check your inbox!")
                    log_interaction(phone_number, incoming_msg, "Data export sent", "export", True)
                else:
                    resp = MessagingResponse()
                    resp.message("Sorry, there was an error creating your data export. Please try again later.")
                    log_interaction(phone_number, incoming_msg, "Export failed", "export", False)
            except Exception as e:
                logger.error(f"Error in EXPORT command: {e}", exc_info=True)
                resp = MessagingResponse()
                resp.message("Sorry, there was an error creating your data export. Please try again later.")
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # QUESTION HANDLING (route to AI with full message)
        # ==========================================
        is_question_command = False
        command, message_text = parse_command(incoming_msg, known_commands=["QUESTION"])
        if command == "QUESTION":
            question_text = message_text.strip()
            if question_text:
                # Route the question to AI processing as natural language
                # Fall through to AI processing at the bottom with the full question text
                incoming_msg = question_text
                is_question_command = True
                logger.info(f"QUESTION command - routing to AI: {question_text[:50]}...")
            else:
                resp = MessagingResponse()
                resp.message("Please include your question after 'Question'. For example: 'Question how do lists work?'")
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # UPGRADE / SUBSCRIPTION HANDLING
        # ==========================================
        if incoming_msg.upper() in ["UPGRADE", "SUBSCRIBE", "PREMIUM", "PRICING"]:
            from services.stripe_service import get_upgrade_message, get_user_subscription, create_checkout_session
            from config import STRIPE_ENABLED, APP_BASE_URL

            subscription = get_user_subscription(phone_number)

            # If already premium, show account management
            if subscription['tier'] != 'free' and subscription['status'] == 'active':
                resp = MessagingResponse()
                resp.message(f"You're already on the {subscription['tier'].title()} plan! Text ACCOUNT to manage your subscription.")
                log_interaction(phone_number, incoming_msg, "Already subscribed", "upgrade_already_premium", True)
                return Response(content=str(resp), media_type="application/xml")

            # User selected Premium - create checkout link
            if incoming_msg.upper() == "PREMIUM" and STRIPE_ENABLED:
                result = create_checkout_session(phone_number, 'premium', 'monthly')

                if 'url' in result:
                    resp = MessagingResponse()
                    resp.message(f"Great choice! Complete your Premium subscription here:\n\n{result['url']}\n\nThis link expires in 24 hours.")
                    log_interaction(phone_number, incoming_msg, "Checkout link sent for premium", "upgrade_premium_checkout", True)
                else:
                    resp = MessagingResponse()
                    resp.message(f"Visit {APP_BASE_URL}/upgrade to subscribe to Premium!")
                    log_interaction(phone_number, incoming_msg, "Checkout fallback", "upgrade_premium_fallback", True)
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
        # STATUS COMMAND (Account Overview)
        # ==========================================
        if incoming_msg.upper() in ["STATUS", "MY ACCOUNT", "ACCOUNT INFO", "USAGE"]:
            try:
                from services.tier_service import get_usage_summary, get_trial_info
                from services.stripe_service import get_user_subscription

                # Get user info
                user = get_user(phone_number)
                if not user:
                    resp = MessagingResponse()
                    resp.message("Unable to retrieve account info. Please try again.")
                    log_interaction(phone_number, incoming_msg, "Status - user not found", "status_error", False)
                    return Response(content=str(resp), media_type="application/xml")

                first_name = user[1] or "there"
                created_at = user[8] if len(user) > 8 else None  # CREATED_AT is index 8, not 7

                # Get tier and usage info
                usage = get_usage_summary(phone_number)
                tier = usage['tier']
                trial_info = get_trial_info(phone_number)
                subscription = get_user_subscription(phone_number)

                # Format member since date
                if created_at:
                    # Handle both datetime objects and timestamps
                    if isinstance(created_at, datetime):
                        member_since = created_at.strftime('%b %d, %Y')
                    elif isinstance(created_at, (int, float)):
                        # Unix timestamp - convert to datetime
                        # If timestamp is 0 or very old (before 2020), show "Recently"
                        if created_at < 1577836800:  # Jan 1, 2020 timestamp
                            member_since = "Recently"
                        else:
                            member_since = datetime.fromtimestamp(created_at).strftime('%b %d, %Y')
                    else:
                        member_since = str(created_at)
                else:
                    member_since = "Recently"

            except Exception as e:
                logger.error(f"Error in STATUS command for {phone_number}: {e}", exc_info=True)
                resp = MessagingResponse()
                resp.message("Unable to retrieve account status. Please try again later.")
                log_interaction(phone_number, incoming_msg, f"Status error: {str(e)}", "status_error", False)
                return Response(content=str(resp), media_type="application/xml")

            # Build status message
            status_lines = [f"📊 Account Status\n"]

            # Plan info with trial status
            if trial_info['is_trial']:
                days_left = trial_info['days_remaining']
                day_word = "day" if days_left == 1 else "days"
                status_lines.append(f"Plan: Premium (Trial - {days_left} {day_word} left)")
            else:
                status_lines.append(f"Plan: {tier.title()}")

            status_lines.append(f"Member since: {member_since}")

            # Next billing (if premium and not trial)
            if tier == 'premium' and not trial_info['is_trial']:
                # Get next billing date from Stripe if available
                if subscription.get('current_period_end'):
                    try:
                        next_billing = datetime.fromtimestamp(subscription['current_period_end'])
                        status_lines.append(f"Next billing: {next_billing.strftime('%b %d, %Y')}")
                    except Exception as e:
                        logger.debug(f"Could not format billing date: {e}")
                        pass

            # Usage stats
            status_lines.append(f"\nThis Month:")

            # Reminders today (free tier) or total reminders
            if tier == 'free':
                reminders_today = usage['reminders_today']
                reminders_limit = usage['reminders_limit']
                status_lines.append(f"• {reminders_today} of {reminders_limit} reminders today")
            else:
                reminders_today = usage['reminders_today']
                status_lines.append(f"• {reminders_today} reminders created today")

            # Lists
            lists_count = usage['lists']
            lists_limit = usage['lists_limit']
            status_lines.append(f"• {lists_count} of {lists_limit} lists")

            # Memories
            memories_count = usage['memories']
            memories_limit = usage['memories_limit']
            if memories_limit is None:
                status_lines.append(f"• {memories_count} memories saved")
            else:
                status_lines.append(f"• {memories_count} of {memories_limit} memories")

            # Tier comparison for free users (not on trial)
            if tier == 'free' and not trial_info['is_trial']:
                from config import get_tier_limits
                premium_limits = get_tier_limits('premium')
                free_limits = get_tier_limits('free')

                status_lines.append(f"\n✨ Premium Benefits:")
                status_lines.append(f"• Unlimited reminders (you: {free_limits['reminders_per_day']}/day)")
                status_lines.append(f"• {premium_limits['max_lists']} lists (you: {free_limits['max_lists']})")
                status_lines.append(f"• {premium_limits['max_items_per_list']} items per list (you: {free_limits['max_items_per_list']})")
                status_lines.append(f"• Unlimited memories (you: {free_limits['max_memories']})")
                status_lines.append(f"• Recurring reminders")
                status_lines.append(f"\nOnly $6.99/month - Text UPGRADE")

            # Quick actions
            status_lines.append(f"\nQuick Actions:")
            if tier == 'free':
                status_lines.append(f"• Text UPGRADE for unlimited")
            else:
                status_lines.append(f"• Text ACCOUNT to manage billing")

            message = "\n".join(status_lines)

            resp = MessagingResponse()
            resp.message(message)
            log_interaction(phone_number, incoming_msg, "Status sent", "status", True)
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # SUPPORT HANDLING (Premium users, or all users in beta mode)
        # ==========================================
        command, message_text = parse_command(incoming_msg, known_commands=["SUPPORT"])
        if command == "SUPPORT":
            from services.support_service import is_premium_user, add_support_message

            # Support is open to all users
            is_premium = is_premium_user(phone_number)

            support_message = message_text.strip()

            if support_message:
                result = add_support_message(phone_number, support_message, 'inbound')
                if result['success']:
                    upgrade_note = "" if is_premium else "\n\nFor priority support, text UPGRADE."
                    resp = MessagingResponse()
                    resp.message(staging_prefix(f"[Support Ticket #{result['ticket_id']}] Your message has been sent to our support team. We'll reply as soon as possible!{upgrade_note}\n\n(You're now in support mode - all replies and messages will go to our support team. Text EXIT to leave support but keep ticket open or text CLOSE to close ticket)"))
                    log_interaction(phone_number, incoming_msg, f"Support ticket #{result['ticket_id']}", "support", True)
                    return Response(content=str(resp), media_type="application/xml")
                else:
                    resp = MessagingResponse()
                    resp.message("Sorry, there was an error sending your message. Please try again.")
                    return Response(content=str(resp), media_type="application/xml")
            else:
                resp = MessagingResponse()
                resp.message("Please include your message after 'Support'. For example: 'Support I need help with reminders'")
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

                # Create new reminder with snoozed time (UTC is correct - snooze is relative to NOW)
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
        # DAILY SUMMARY COMMANDS
        # ==========================================
        msg_upper = incoming_msg.upper().strip()

        # Enable daily summary: "SUMMARY ON", "DAILY SUMMARY ON"
        if msg_upper in ["SUMMARY ON", "DAILY SUMMARY ON", "DAILY SUMMARY"]:
            from models.user import get_daily_summary_settings

            # Get current settings for undo capability
            current_settings = get_daily_summary_settings(phone_number)
            previous_enabled = current_settings['enabled'] if current_settings else False
            previous_time = current_settings['time'] if current_settings else None

            # Store undo data
            undo_data = json.dumps({
                'type': 'summary_undo',
                'previous_enabled': previous_enabled,
                'previous_time': previous_time,
                'action': 'enabled'
            })

            # Enable with default time (8:00 AM)
            create_or_update_user(phone_number, daily_summary_enabled=True, pending_reminder_confirmation=undo_data)

            settings = get_daily_summary_settings(phone_number)
            time_str = settings['time'] if settings else '08:00'

            # Format for display
            time_parts = time_str.split(':')
            hour = int(time_parts[0])
            minute = int(time_parts[1]) if len(time_parts) > 1 else 0
            am_pm = 'AM' if hour < 12 else 'PM'
            display_hour = hour if hour <= 12 else hour - 12
            if display_hour == 0:
                display_hour = 12
            display_time = f"{display_hour}:{minute:02d} {am_pm}"

            resp = MessagingResponse()
            resp.message(staging_prefix(f"Daily summary enabled! You'll receive a summary of your day's reminders at {display_time}.\n\nTo change the time, text: SUMMARY TIME 7AM"))
            log_interaction(phone_number, incoming_msg, "Daily summary enabled", "daily_summary_on", True)
            return Response(content=str(resp), media_type="application/xml")

        # Disable daily summary: "SUMMARY OFF", "DAILY SUMMARY OFF", "DISABLE SUMMARY", etc.
        if msg_upper in ["SUMMARY OFF", "DAILY SUMMARY OFF", "DISABLE SUMMARY", "DISABLE DAILY SUMMARY", "TURN OFF SUMMARY", "TURN OFF DAILY SUMMARY"] or msg_upper.startswith("SUMMARY OFF ") or msg_upper.startswith("DAILY SUMMARY OFF "):
            from models.user import get_daily_summary_settings

            # Get current settings for undo capability
            current_settings = get_daily_summary_settings(phone_number)
            previous_enabled = current_settings['enabled'] if current_settings else False
            previous_time = current_settings['time'] if current_settings else None

            # Store undo data
            undo_data = json.dumps({
                'type': 'summary_undo',
                'previous_enabled': previous_enabled,
                'previous_time': previous_time,
                'action': 'disabled'
            })

            # Clear daily_summary_prompted flag to exit the setup flow
            create_or_update_user(
                phone_number,
                daily_summary_enabled=False,
                daily_summary_prompted=False,
                pending_reminder_confirmation=undo_data
            )

            resp = MessagingResponse()
            resp.message(staging_prefix("Daily summary disabled. You'll no longer receive daily reminder summaries."))
            log_interaction(phone_number, incoming_msg, "Daily summary disabled", "daily_summary_off", True)
            return Response(content=str(resp), media_type="application/xml")

        # Check daily summary status: "MY SUMMARY", "SUMMARY STATUS"
        if msg_upper in ["MY SUMMARY", "SUMMARY STATUS", "SUMMARY"]:
            from models.user import get_daily_summary_settings

            settings = get_daily_summary_settings(phone_number)

            if settings and settings['enabled']:
                time_str = settings['time']
                time_parts = time_str.split(':')
                hour = int(time_parts[0])
                minute = int(time_parts[1]) if len(time_parts) > 1 else 0
                am_pm = 'AM' if hour < 12 else 'PM'
                display_hour = hour if hour <= 12 else hour - 12
                if display_hour == 0:
                    display_hour = 12
                display_time = f"{display_hour}:{minute:02d} {am_pm}"

                resp = MessagingResponse()
                resp.message(staging_prefix(f"Daily summary: ON at {display_time}\n\nCommands:\n- SUMMARY OFF - Disable\n- SUMMARY TIME 7AM - Change time"))
            else:
                resp = MessagingResponse()
                resp.message(staging_prefix("Daily summary: OFF\n\nTo enable, text: SUMMARY ON\nTo set a specific time: SUMMARY TIME 7AM"))

            log_interaction(phone_number, incoming_msg, "Daily summary status", "daily_summary_status", True)
            return Response(content=str(resp), media_type="application/xml")

        # Set daily summary time: "SUMMARY TIME 7AM", "SUMMARY ON 10PM", "DAILY SUMMARY TIME 8:30AM"
        # Also supports shorthand: "SUMMARY TIME 10a", "SUMMARY TIME 3p"
        # Also natural language: "change my daily summary time to 8am", "set my summary to 7am"
        summary_time_match = re.match(
            r'^(?:daily\s+)?summary\s+(?:on\s+|time\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)$',
            incoming_msg.strip(),
            re.IGNORECASE
        )
        if not summary_time_match:
            summary_time_match = re.match(
                r'^(?:change|set|move|update)\s+(?:my\s+)?(?:daily\s+)?summary\s+(?:time\s+)?(?:to\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)\b',
                incoming_msg.strip(),
                re.IGNORECASE
            )

        if summary_time_match:
            hour = int(summary_time_match.group(1))
            minute = int(summary_time_match.group(2)) if summary_time_match.group(2) else 0
            am_pm_raw = summary_time_match.group(3).upper()
            am_pm = 'AM' if am_pm_raw in ['AM', 'A'] else 'PM'

            # Convert to 24-hour format
            if am_pm == 'PM' and hour != 12:
                hour += 12
            elif am_pm == 'AM' and hour == 12:
                hour = 0

            # Validate hour
            if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                resp = MessagingResponse()
                resp.message(staging_prefix("Please enter a valid time like 7AM, 8:30AM, or 6PM"))
                return Response(content=str(resp), media_type="application/xml")

            # Format for storage (HH:MM)
            time_str = f"{hour:02d}:{minute:02d}"

            # Get current summary settings for undo capability
            from models.user import get_daily_summary_settings
            current_settings = get_daily_summary_settings(phone_number)
            previous_enabled = current_settings['enabled'] if current_settings else False
            previous_time = current_settings['time'] if current_settings else None

            # Store previous state for undo (using pending_reminder_confirmation with special type)
            undo_data = json.dumps({
                'type': 'summary_undo',
                'previous_enabled': previous_enabled,
                'previous_time': previous_time,
                'new_time': time_str
            })

            # Enable and set time, storing undo data
            create_or_update_user(
                phone_number,
                daily_summary_enabled=True,
                daily_summary_time=time_str,
                pending_reminder_confirmation=undo_data
            )

            # Format for display
            display_am_pm = 'AM' if hour < 12 else 'PM'
            display_hour = hour if hour <= 12 else hour - 12
            if display_hour == 0:
                display_hour = 12
            display_time = f"{display_hour}:{minute:02d} {display_am_pm}"

            resp = MessagingResponse()
            resp.message(staging_prefix(f"Daily summary set for {display_time}! You'll receive a summary of your day's reminders each morning."))
            log_interaction(phone_number, incoming_msg, f"Daily summary time set to {time_str}", "daily_summary_time", True)
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # TIMEZONE COMMANDS
        # ==========================================

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
                except (ValueError, TypeError, AttributeError) as e:
                    logger.debug(f"Date formatting failed: {e}")
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
                    except (ValueError, TypeError, AttributeError, KeyError) as e:
                        logger.debug(f"Next occurrence formatting failed: {e}")

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
        # Handle "ALL" or number for multi-list delete
        user = get_user(phone_number)
        if user and user[9]:  # pending_delete flag
            pending_action = get_pending_list_item(phone_number)
            if pending_action and pending_action.startswith("__DELETE_MULTI__:"):
                parts = pending_action.split(":")
                list_filter = parts[1]
                list_ids = parts[2].split(",") if len(parts) > 2 else []

                if incoming_msg.upper() == "ALL":
                    # Delete all matching lists
                    from database import get_db_connection, return_db_connection
                    deleted_count = 0
                    for list_id in list_ids:
                        try:
                            conn = get_db_connection()
                            c = conn.cursor()
                            c.execute('DELETE FROM lists WHERE id = %s', (int(list_id),))
                            if c.rowcount > 0:
                                deleted_count += 1
                            conn.commit()
                            return_db_connection(conn)
                        except Exception as e:
                            logger.error(f"Error deleting list {list_id}: {e}")

                    create_or_update_user(phone_number, pending_delete=False, pending_list_item=None)
                    reply_msg = f"Deleted {deleted_count} {list_filter} list{'s' if deleted_count != 1 else ''} and all their items."
                    resp = MessagingResponse()
                    resp.message(staging_prefix(reply_msg))
                    log_interaction(phone_number, incoming_msg, reply_msg, "delete_multi_list_all", True)
                    return Response(content=str(resp), media_type="application/xml")

                elif incoming_msg.strip().isdigit():
                    # Delete specific list by number
                    selection = int(incoming_msg.strip())
                    if 1 <= selection <= len(list_ids):
                        from database import get_db_connection, return_db_connection
                        list_id = list_ids[selection - 1]

                        # Get list name before deleting
                        conn = get_db_connection()
                        c = conn.cursor()
                        c.execute('SELECT list_name FROM lists WHERE id = %s', (int(list_id),))
                        result = c.fetchone()
                        list_name = result[0] if result else f"{list_filter} list"

                        c.execute('DELETE FROM lists WHERE id = %s', (int(list_id),))
                        deleted = c.rowcount > 0
                        conn.commit()
                        return_db_connection(conn)

                        create_or_update_user(phone_number, pending_delete=False, pending_list_item=None)
                        if deleted:
                            reply_msg = f"Deleted your {list_name} and all its items."
                        else:
                            reply_msg = "Couldn't delete that list."
                        resp = MessagingResponse()
                        resp.message(staging_prefix(reply_msg))
                        log_interaction(phone_number, incoming_msg, reply_msg, "delete_multi_list_single", True)
                        return Response(content=str(resp), media_type="application/xml")
                    else:
                        resp = MessagingResponse()
                        resp.message(staging_prefix(f"Please reply with a number between 1 and {len(list_ids)}, or ALL to delete all."))
                        return Response(content=str(resp), media_type="application/xml")

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

                elif pending_action and pending_action.startswith("__DELETE_MULTI__:"):
                    # Multi-list delete - user confirmed with YES to delete all
                    parts = pending_action.split(":")
                    list_filter = parts[1]
                    list_ids = parts[2].split(",") if len(parts) > 2 else []

                    deleted_count = 0
                    for list_id in list_ids:
                        try:
                            conn = get_db_connection()
                            c = conn.cursor()
                            c.execute('DELETE FROM lists WHERE id = %s', (int(list_id),))
                            if c.rowcount > 0:
                                deleted_count += 1
                            conn.commit()
                            return_db_connection(conn)
                        except Exception as e:
                            logger.error(f"Error deleting list {list_id}: {e}")

                    create_or_update_user(phone_number, pending_delete=False, pending_list_item=None)
                    reply_msg = f"Deleted {deleted_count} {list_filter} list{'s' if deleted_count != 1 else ''} and all their items."
                    resp = MessagingResponse()
                    resp.message(reply_msg)
                    log_interaction(phone_number, incoming_msg, reply_msg, "delete_multi_list_confirmed", True)
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
        # HELP - Twilio reserved keyword (handled by Twilio Messaging Service)
        # Return empty response so only Twilio's message is sent
        # ==========================================
        if incoming_msg.upper() == "HELP":
            log_interaction(phone_number, incoming_msg, "[Handled by Twilio]", "help_twilio", True)
            resp = MessagingResponse()
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
        # PENDING STATE REMINDER (Context Switch Protection)
        # ==========================================
        # Check if user has any pending state that requires their response
        # If they send an unrelated message, remind them of the pending question

        # Check for cancel commands first
        cancel_phrases = ['cancel', 'nevermind', 'never mind', 'skip', 'forget it', 'no thanks', 'nope', 'stop', 'undo']
        is_cancel_command = incoming_msg.strip().lower() in cancel_phrases

        # Get user for pending state checks
        user_for_pending_check = get_user(phone_number)

        # Gather all pending states
        pending_states = {}

        # Check for AM/PM clarification (pending_reminder_text + pending_reminder_time, time is NOT "NEEDS_TIME")
        if user_for_pending_check and len(user_for_pending_check) > 11:
            pending_text = user_for_pending_check[10]  # pending_reminder_text
            pending_time = user_for_pending_check[11]  # pending_reminder_time
            if pending_text and pending_time and pending_time != "NEEDS_TIME":
                pending_states['ampm_clarification'] = {
                    'text': pending_text,
                    'time': pending_time
                }
            elif pending_text and pending_time == "NEEDS_TIME":
                pending_states['time_needed'] = {
                    'text': pending_text
                }

        # Check for date clarification
        pending_date_check = get_pending_reminder_date(phone_number)
        if pending_date_check:
            pending_states['date_clarification'] = pending_date_check

        # Check for reminder delete confirmation
        pending_reminder_del = get_pending_reminder_delete(phone_number)
        if pending_reminder_del:
            try:
                del_data = json.loads(pending_reminder_del)
                if isinstance(del_data, list):
                    # It's a list of options for selection
                    pending_states['reminder_delete_selection'] = del_data
                elif isinstance(del_data, dict):
                    if del_data.get('awaiting_confirmation'):
                        pending_states['reminder_delete_confirmation'] = del_data
                    elif 'options' in del_data:
                        pending_states['reminder_delete_selection'] = del_data
            except json.JSONDecodeError:
                pass

        # Check for memory delete confirmation
        pending_memory_del = get_pending_memory_delete(phone_number)
        if pending_memory_del:
            try:
                mem_data = json.loads(pending_memory_del)
                if isinstance(mem_data, list):
                    # It's a list of options for selection
                    pending_states['memory_delete_selection'] = mem_data
                elif isinstance(mem_data, dict):
                    if mem_data.get('awaiting_confirmation'):
                        pending_states['memory_delete_confirmation'] = mem_data
                    elif 'options' in mem_data:
                        pending_states['memory_delete_selection'] = mem_data
            except json.JSONDecodeError:
                pass

        # Check for list item selection (pending_list_item without pending_delete)
        pending_list_item_check = get_pending_list_item(phone_number)
        if pending_list_item_check and not (user_for_pending_check and user_for_pending_check[9]):
            pending_states['list_selection'] = {
                'item': pending_list_item_check
            }

        # Check for pending list create (duplicate name handling)
        pending_list_create_check = get_pending_list_create(phone_number)
        if pending_list_create_check:
            pending_states['list_create_duplicate'] = {
                'list_name': pending_list_create_check
            }

        # Check for low-confidence reminder confirmation
        pending_confirm_check = get_pending_reminder_confirmation(phone_number)
        if pending_confirm_check and pending_confirm_check.get('type') != 'summary_undo':
            pending_states['reminder_confirmation'] = pending_confirm_check

        # If user wants to cancel, clear all pending states
        if is_cancel_command and pending_states:
            # Clear all pending states
            create_or_update_user(
                phone_number,
                pending_reminder_text=None,
                pending_reminder_time=None,
                pending_reminder_date=None,
                pending_list_item=None,
                pending_list_create=None,
                pending_delete=False,
                pending_reminder_delete=None,
                pending_memory_delete=None,
                pending_reminder_confirmation=None
            )

            resp = MessagingResponse()
            resp.message(staging_prefix("Got it, cancelled. What would you like to do?"))
            log_interaction(phone_number, incoming_msg, "Pending state cancelled", "pending_cancel", True)
            return Response(content=str(resp), media_type="application/xml")

        # If user has a pending state and message doesn't look like a valid response, remind them
        if pending_states:
            # Check if message looks like a valid response to the pending question
            msg_stripped = incoming_msg.strip()
            msg_lower = msg_stripped.lower()

            # Valid responses: YES/NO, numbers, AM/PM patterns, times, time-of-day words
            is_yes_no = msg_lower in ['yes', 'y', 'no', 'n', 'yep', 'yeah', 'nope', 'ok', 'okay', 'correct', 'right', 'wrong', 'incorrect']
            is_number = msg_stripped.isdigit()
            is_ampm = bool(re.match(r'^(am|pm|a\.m\.|p\.m\.)\.?$', msg_stripped, re.IGNORECASE))
            has_time_pattern = bool(re.search(r'\d+\s*(am|pm|a\.m\.|p\.m\.|a|p)\b', msg_stripped, re.IGNORECASE))
            is_time_response = bool(re.match(r'^(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m?\.?)?$', msg_stripped, re.IGNORECASE))
            has_time_of_day_word = bool(re.search(r'\b(morning|afternoon|evening|night)\b', msg_stripped, re.IGNORECASE))

            # Consider it a valid response if it matches expected patterns
            is_valid_response = is_yes_no or is_number or is_ampm or has_time_pattern or is_time_response or has_time_of_day_word

            # If not a valid response, remind user about the pending question
            if not is_valid_response:
                # Build reminder message based on which pending state is active
                if 'ampm_clarification' in pending_states:
                    state = pending_states['ampm_clarification']
                    reminder = f"I still need to know: Did you mean {state['time']} AM or PM for '{state['text']}'?\n\n(Say 'cancel' to skip this reminder)"
                elif 'time_needed' in pending_states:
                    state = pending_states['time_needed']
                    reminder = f"I still need a time for your reminder: '{state['text']}'\n\nWhat time works? (e.g., '3pm', 'in 30 minutes')\n\n(Say 'cancel' to skip)"
                elif 'date_clarification' in pending_states:
                    state = pending_states['date_clarification']
                    reminder = f"I still need to know what time for '{state['text']}' on {state['date']}.\n\nWhat time? (e.g., '9am', '3:30pm')\n\n(Say 'cancel' to skip)"
                elif 'reminder_delete_confirmation' in pending_states:
                    state = pending_states['reminder_delete_confirmation']
                    reminder = f"I still need your confirmation: Delete reminder '{state.get('text', 'your reminder')}'?\n\nReply YES to delete or NO to keep it. (Say 'cancel' to skip)"
                elif 'reminder_delete_selection' in pending_states:
                    reminder = "I still need you to select which reminder to delete.\n\nReply with the number of the reminder to delete, or say 'cancel' to skip."
                elif 'memory_delete_confirmation' in pending_states:
                    state = pending_states['memory_delete_confirmation']
                    reminder = f"I still need your confirmation: Delete this memory?\n\nReply YES to delete or NO to keep it. (Say 'cancel' to skip)"
                elif 'memory_delete_selection' in pending_states:
                    reminder = "I still need you to select which memory to delete.\n\nReply with the number of the memory to delete, or say 'cancel' to skip."
                elif 'list_selection' in pending_states:
                    state = pending_states['list_selection']
                    reminder = f"I still need to know which list to add '{state['item']}' to.\n\nReply with the list number, or say 'cancel' to skip."
                elif 'list_create_duplicate' in pending_states:
                    state = pending_states['list_create_duplicate']
                    reminder = f"A list named '{state['list_name']}' already exists. Reply YES to create a new numbered version, or NO to use the existing list.\n\n(Say 'cancel' to skip)"
                elif 'reminder_confirmation' in pending_states:
                    state = pending_states['reminder_confirmation']
                    reminder_text = state.get('reminder_text', 'your reminder')
                    reminder = f"I need your confirmation: Is this reminder correct?\n\n'{reminder_text}'\n\nReply YES to confirm or NO to re-enter. (Say 'cancel' to skip)"
                else:
                    reminder = "I have a pending question for you. Please answer it or say 'cancel' to skip."

                resp = MessagingResponse()
                resp.message(staging_prefix(reminder))
                log_interaction(phone_number, incoming_msg, reminder, "pending_state_reminder", True)
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
        first_action_type = None
        for action_index, current_action in enumerate(actions_to_process):
            action_type = current_action.get("action", "error")
            if action_index == 0:
                first_action_type = action_type
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

        # Append trial info if this is user's first real interaction
        if first_action_type:
            reply_text = append_trial_info_to_response(reply_text, first_action_type, phone_number)

        # If this was a QUESTION command, append escape hatch for human help
        if is_question_command:
            reply_text += "\n\nNeed more help? Text SUPPORT followed by your message to chat with a real person."

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
            from services.tier_service import can_save_memory, format_memory_limit_message, add_memory_counter_to_message
            allowed, limit_msg = can_save_memory(phone_number)
            if not allowed:
                # Use Level 4 formatter for clear WHY-HOW message
                reply_text = format_memory_limit_message(phone_number)
                log_interaction(phone_number, incoming_msg, reply_text, "memory_limit_reached", False)
                return reply_text

            save_memory(phone_number, memory_text, ai_response)
            # Echo back exactly what was saved for user trust
            saved_text = ai_response.get("memory_text", "")
            if saved_text:
                base_reply = f'Got it! Saved: "{saved_text}"'
            else:
                base_reply = ai_response.get("confirmation", "Got it! I'll remember that.")

            # Add progressive counter for free tier users
            reply_text = add_memory_counter_to_message(phone_number, base_reply)

            # Check if this is user's first action and prompt for daily summary
            if should_prompt_daily_summary(phone_number):
                reply_text = get_daily_summary_prompt_message(reply_text)
                mark_daily_summary_prompted(phone_number)

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

            # SAFEGUARD: Check if original message already has AM/PM - AI sometimes misses it
            # Match patterns like: 10am, 10:00am, 10a, 10:00a, 10 am, 10:00 a.m.
            original_time_match = re.search(
                r'(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|a|p)\b',
                incoming_msg,
                re.IGNORECASE
            )

            if original_time_match:
                # Original message already has AM/PM - create reminder directly
                hour = int(original_time_match.group(1))
                minute = int(original_time_match.group(2)) if original_time_match.group(2) else 0
                am_pm_raw = original_time_match.group(3).lower().replace('.', '')
                am_pm = 'AM' if am_pm_raw in ['am', 'a'] else 'PM'

                # Convert to 24-hour format
                if am_pm == 'PM' and hour != 12:
                    hour += 12
                elif am_pm == 'AM' and hour == 12:
                    hour = 0

                logger.info(f"Safeguard: Original message had AM/PM, creating reminder directly at {hour}:{minute:02d}")

                user_time = get_user_current_time(phone_number)
                user_tz = get_user_timezone(phone_number)

                # Create reminder datetime in user's timezone
                reminder_datetime = user_time.replace(hour=hour, minute=minute, second=0, microsecond=0)

                # If time has already passed today, schedule for tomorrow
                if reminder_datetime <= user_time:
                    reminder_datetime = reminder_datetime + timedelta(days=1)

                # Convert to UTC for storage
                reminder_datetime_utc = reminder_datetime.astimezone(pytz.UTC)
                reminder_date_str = reminder_datetime_utc.strftime('%Y-%m-%d %H:%M:%S')

                # Save the reminder
                save_reminder(phone_number, reminder_text, reminder_date_str)

                # Format confirmation
                readable_date = reminder_datetime.strftime('%A, %B %d at %I:%M %p')
                reply_text = f"I'll remind you on {readable_date} {format_reminder_confirmation(reminder_text)}."
                log_interaction(phone_number, incoming_msg, reply_text, "reminder_safeguard", True)
            else:
                # No AM/PM in original - proceed with clarification
                # Fallback: extract time from response if time_mentioned is missing
                if not time_mentioned:
                    response_text = ai_response.get("response", "")
                    # Look for patterns like "8:00", "8", "10:30" in the response
                    time_match = re.search(r'(\d{1,2}(?::\d{2})?)\s*(?:AM|PM)', response_text, re.IGNORECASE)
                    if time_match:
                        time_mentioned = time_match.group(1)
                        logger.info(f"Extracted time '{time_mentioned}' from AI response")

                create_or_update_user(
                    phone_number,
                    pending_reminder_text=reminder_text,
                    pending_reminder_time=time_mentioned
                )

                reply_text = ai_response.get("response", f"Do you mean {time_mentioned} AM or PM?")
                log_interaction(phone_number, incoming_msg, reply_text, "clarify_time", True)

        elif ai_response["action"] == "clarify_date_time":
            # User gave a date but no time - ask what time they want
            reminder_text = ai_response.get("reminder_text")
            reminder_date = ai_response.get("reminder_date")  # YYYY-MM-DD format

            create_or_update_user(
                phone_number,
                pending_reminder_text=reminder_text,
                pending_reminder_date=reminder_date
            )

            # Generate date string server-side to ensure accurate day-of-week
            # (AI sometimes miscalculates day names for dates)
            try:
                date_obj = datetime.strptime(reminder_date, '%Y-%m-%d')
                date_str = date_obj.strftime('%A, %B %d')
                reply_text = f"I'll remind you on {date_str} to {reminder_text}. What time would you like the reminder?"
            except:
                reply_text = ai_response.get("response", "What time would you like the reminder?")
            log_interaction(phone_number, incoming_msg, reply_text, "clarify_date_time", True)

        elif ai_response["action"] == "clarify_specific_time":
            # User gave a vague time like "in a bit" - ask for specific time
            reminder_text = ai_response.get("reminder_text")

            create_or_update_user(
                phone_number,
                pending_reminder_text=reminder_text,
                pending_reminder_time="NEEDS_TIME"  # Special marker
            )

            reply_text = ai_response.get("response", "What time would you like the reminder?")
            log_interaction(phone_number, incoming_msg, reply_text, "clarify_specific_time", True)

        elif ai_response["action"] == "reminder":
            reminder_date = ai_response.get("reminder_date")
            reminder_text = ai_response.get("reminder_text")
            confidence = ai_response.get("confidence", 100)  # Default to 100 if not provided

            # Detect midnight default - if AI returned 00:00:00 but user didn't specify a time,
            # redirect to clarify_date_time flow instead of creating a midnight reminder
            if reminder_date and reminder_date.endswith(' 00:00:00'):
                # Check if user actually specified midnight explicitly
                msg_lower = incoming_msg.lower()
                has_explicit_time = bool(re.search(r'\b(at\s+)?\d{1,2}(:\d{2})?\s*(am|pm|a\.m\.|p\.m\.)\b', msg_lower, re.IGNORECASE))
                has_midnight = bool(re.search(r'\b(midnight|12\s*(am|a\.m\.))\b', msg_lower, re.IGNORECASE))

                if not has_explicit_time and not has_midnight:
                    # No time specified - ask for clarification instead of defaulting to midnight
                    # Extract date portion for the clarify flow
                    reminder_date_only = reminder_date.split(' ')[0]  # YYYY-MM-DD
                    create_or_update_user(
                        phone_number,
                        pending_reminder_text=reminder_text,
                        pending_reminder_date=reminder_date_only
                    )
                    try:
                        date_obj = datetime.strptime(reminder_date_only, '%Y-%m-%d')
                        date_str = date_obj.strftime('%A, %B %d')
                    except:
                        date_str = "that day"
                    reply_text = f"I'll remind you on {date_str} to {reminder_text}. What time would you like the reminder?"
                    log_interaction(phone_number, incoming_msg, reply_text, "clarify_date_time", True)
                    return reply_text

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

            # LOW CONFIDENCE: Ask for confirmation before creating reminder
            CONFIDENCE_THRESHOLD = int(get_setting('confidence_threshold', 70))
            if confidence < CONFIDENCE_THRESHOLD:
                # Log low confidence for calibration tracking
                log_confidence(phone_number, 'reminder', confidence, CONFIDENCE_THRESHOLD, confirmed=None, user_message=incoming_msg)
                try:
                    user_tz_str = get_user_timezone(phone_number)
                    tz = pytz.timezone(user_tz_str)
                    naive_dt = datetime.strptime(reminder_date, '%Y-%m-%d %H:%M:%S')
                    time_str = naive_dt.strftime('%I:%M %p').lstrip('0')
                    date_str = naive_dt.strftime('%A, %B %d, %Y')

                    # Store the pending reminder for confirmation (including confidence for later logging)
                    pending_data = json.dumps({
                        'action': 'reminder',
                        'reminder_text': reminder_text,
                        'reminder_date': reminder_date,
                        'confirmation': ai_response.get('confirmation'),
                        'confidence': confidence
                    })
                    create_or_update_user(phone_number, pending_reminder_confirmation=pending_data)

                    # Ask for confirmation
                    reply_text = f"I understood: Reminder on {date_str} at {time_str} {format_reminder_confirmation(reminder_text)}.\n\nIs that right? Reply YES or tell me what to change."
                    log_interaction(phone_number, incoming_msg, reply_text, "reminder_confirmation_needed", True)
                    return reply_text
                except Exception as e:
                    logger.error(f"Error preparing confirmation: {e}")
                    # Fall through to create reminder anyway

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

                # Generate confirmation server-side to ensure accurate day-of-week
                # (AI sometimes miscalculates day names for dates)
                time_str = naive_dt.strftime('%I:%M %p').lstrip('0')
                date_str = naive_dt.strftime('%A, %B %d, %Y')
                reply_text = f"Got it! I'll remind you on {date_str} at {time_str} {format_reminder_confirmation(reminder_text)}."

            except Exception as e:
                logger.error(f"Error converting reminder time to UTC: {e}")
                save_reminder(phone_number, reminder_text, reminder_date)
                reply_text = ai_response.get("confirmation", "Got it! I'll remind you.")

            # Add usage counter for free tier users
            from services.tier_service import add_usage_counter_to_message
            reply_text = add_usage_counter_to_message(phone_number, reply_text)

            # Check if this is user's first action and prompt for daily summary
            if should_prompt_daily_summary(phone_number):
                reply_text = get_daily_summary_prompt_message(reply_text)
                mark_daily_summary_prompted(phone_number)

            log_interaction(phone_number, incoming_msg, reply_text, "reminder", True)

        elif ai_response["action"] == "reminder_relative":
            # Handle relative time reminders (e.g., "in 30 minutes", "in 5 months")
            # Server calculates the actual time to avoid AI arithmetic errors
            from dateutil.relativedelta import relativedelta

            reminder_text = ai_response.get("reminder_text", "your reminder")
            confidence = ai_response.get("confidence", 100)  # Default to 100 if not provided

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

                # LOW CONFIDENCE: Ask for confirmation before creating reminder
                CONFIDENCE_THRESHOLD = int(get_setting('confidence_threshold', 70))
                if confidence < CONFIDENCE_THRESHOLD:
                    # Log low confidence for calibration tracking
                    log_confidence(phone_number, 'reminder_relative', confidence, CONFIDENCE_THRESHOLD, confirmed=None, user_message=incoming_msg)

                    user_tz_str = get_user_timezone(phone_number)
                    tz = pytz.timezone(user_tz_str)
                    reminder_dt_local = pytz.UTC.localize(reminder_dt_utc).astimezone(tz)
                    time_str = reminder_dt_local.strftime('%I:%M %p').lstrip('0')
                    date_str = reminder_dt_local.strftime('%A, %B %d, %Y')

                    # Store the pending reminder for confirmation (including confidence for later logging)
                    pending_data = json.dumps({
                        'action': 'reminder_relative',
                        'reminder_text': reminder_text,
                        'reminder_datetime_utc': reminder_date_utc,
                        'local_time': reminder_dt_local.strftime('%H:%M'),
                        'offset_minutes': offset_minutes,
                        'offset_days': offset_days,
                        'offset_weeks': offset_weeks,
                        'offset_months': offset_months,
                        'confidence': confidence
                    })
                    create_or_update_user(phone_number, pending_reminder_confirmation=pending_data)

                    # Ask for confirmation
                    reply_text = f"I understood: Reminder on {date_str} at {time_str} {format_reminder_confirmation(reminder_text)}.\n\nIs that right? Reply YES or tell me what to change."
                    log_interaction(phone_number, incoming_msg, reply_text, "reminder_confirmation_needed", True)
                    return reply_text

                # Save the reminder
                save_reminder(phone_number, reminder_text, reminder_date_utc)

                # Generate confirmation in user's timezone
                user_tz_str = get_user_timezone(phone_number)
                tz = pytz.timezone(user_tz_str)
                reminder_dt_local = pytz.UTC.localize(reminder_dt_utc).astimezone(tz)

                # Format the time nicely
                time_str = reminder_dt_local.strftime('%I:%M %p').lstrip('0')
                date_str = reminder_dt_local.strftime('%A, %B %d, %Y')

                reply_text = f"Got it! I'll remind you on {date_str} at {time_str} {format_reminder_confirmation(reminder_text)}."

                # Add usage counter for free tier users
                from services.tier_service import add_usage_counter_to_message
                reply_text = add_usage_counter_to_message(phone_number, reply_text)

                # Check if this is user's first action and prompt for daily summary
                if should_prompt_daily_summary(phone_number):
                    reply_text = get_daily_summary_prompt_message(reply_text)
                    mark_daily_summary_prompted(phone_number)

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
            confidence = ai_response.get("confidence", 100)  # Default to 100 if not provided

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

                # LOW CONFIDENCE: Ask for confirmation before creating recurring reminder
                CONFIDENCE_THRESHOLD = int(get_setting('confidence_threshold', 70))
                if confidence < CONFIDENCE_THRESHOLD:
                    # Log low confidence for calibration tracking
                    log_confidence(phone_number, 'reminder_recurring', confidence, CONFIDENCE_THRESHOLD, confirmed=None, user_message=incoming_msg)

                    # Format time for display
                    try:
                        hour, minute = map(int, time_str.split(':'))
                        display_time = datetime(2000, 1, 1, hour, minute).strftime('%I:%M %p').lstrip('0')
                    except:
                        display_time = time_str

                    # Format recurrence pattern for display
                    if recurrence_type == 'daily':
                        pattern_desc = 'every day'
                    elif recurrence_type == 'weekly':
                        day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
                        day_name = day_names[recurrence_day] if recurrence_day is not None else 'the specified day'
                        pattern_desc = f'every {day_name}'
                    elif recurrence_type == 'weekdays':
                        pattern_desc = 'every weekday'
                    elif recurrence_type == 'weekends':
                        pattern_desc = 'every weekend'
                    elif recurrence_type == 'monthly':
                        pattern_desc = f'monthly on the {recurrence_day}{"st" if recurrence_day == 1 else "nd" if recurrence_day == 2 else "rd" if recurrence_day == 3 else "th"}'
                    else:
                        pattern_desc = recurrence_type

                    # Store the pending reminder for confirmation (including confidence for later logging)
                    pending_data = json.dumps({
                        'action': 'reminder_recurring',
                        'reminder_text': reminder_text,
                        'recurrence_type': recurrence_type,
                        'recurrence_day': recurrence_day,
                        'time': time_str,
                        'confidence': confidence
                    })
                    create_or_update_user(phone_number, pending_reminder_confirmation=pending_data)

                    # Ask for confirmation
                    reply_text = f"I understood: Recurring reminder {pattern_desc} at {display_time} {format_reminder_confirmation(reminder_text)}.\n\nIs that right? Reply YES or tell me what to change."
                    log_interaction(phone_number, incoming_msg, reply_text, "reminder_confirmation_needed", True)
                    return reply_text

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

                reply_text = f"Got it! I'll remind you {pattern_text} at {display_time} {format_reminder_confirmation(reminder_text)}.\n\n"

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
                else:
                    existing_list = get_list_by_name(phone_number, list_name)
                    if existing_list:
                        # List exists - ask user what they want to do
                        list_id, actual_name = existing_list
                        items = get_list_items(list_id)
                        item_count = len(items)

                        # Store pending state
                        create_or_update_user(phone_number, pending_list_create=list_name)

                        if item_count == 0:
                            reply_text = f"You already have an empty {actual_name}. Would you like to add items to it, or create a new one?"
                        elif item_count == 1:
                            reply_text = f"You already have a {actual_name} with 1 item. Would you like to add items to it, or create a new one?"
                        else:
                            reply_text = f"You already have a {actual_name} with {item_count} items. Would you like to add items to it, or create a new one?"
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
                    from services.tier_service import (
                        can_create_list, get_tier_limits, get_user_tier,
                        format_list_limit_message, format_list_item_limit_message,
                        add_list_counter_to_message, add_list_item_counter_to_message
                    )
                    allowed, limit_msg = can_create_list(phone_number)
                    if not allowed:
                        # Use Level 4 formatter
                        reply_text = format_list_limit_message(phone_number)
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

                        # Handle partial or full adds with progressive education
                        if len(added_items) < len(items_to_add):
                            # Some items skipped - use Level 4 formatter
                            reply_text = format_list_item_limit_message(
                                phone_number, list_name, items_to_add, len(added_items)
                            )
                        else:
                            # All items added successfully
                            if len(added_items) == 1:
                                base_reply = f"Created your {list_name} and added {added_items[0]}!"
                            else:
                                base_reply = f"Created your {list_name} and added {len(added_items)} items: {', '.join(added_items)}"

                            # Add list counter (for list creation) and item counter
                            reply_text = add_list_counter_to_message(phone_number, base_reply)
                            reply_text = add_list_item_counter_to_message(phone_number, list_id, reply_text)
                else:
                    list_id = list_info[0]
                    list_name = list_info[1]  # Use actual list name from DB
                    # Check tier limit for items per list
                    from services.tier_service import (
                        can_add_list_item, get_tier_limits, get_user_tier,
                        format_list_item_limit_message, add_list_item_counter_to_message
                    )
                    allowed, limit_msg = can_add_list_item(phone_number, list_id)
                    if not allowed:
                        # List is full - use Level 4 formatter
                        reply_text = format_list_item_limit_message(
                            phone_number, list_name, items_to_add, 0
                        )
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

                        # Handle partial or full adds with progressive education
                        if len(added_items) < len(items_to_add):
                            # Some items skipped - use Level 4 formatter
                            reply_text = format_list_item_limit_message(
                                phone_number, list_name, items_to_add, len(added_items)
                            )
                        else:
                            # All items added successfully
                            if len(added_items) == 1:
                                base_reply = ai_response.get("confirmation", f"Added {added_items[0]} to your {list_name}")
                            else:
                                base_reply = f"Added {len(added_items)} items to your {list_name}: {', '.join(added_items)}"

                            # Add progressive counter
                            reply_text = add_list_item_counter_to_message(phone_number, list_id, base_reply)

                        # Check if this is user's first action and prompt for daily summary
                        if should_prompt_daily_summary(phone_number):
                            reply_text = get_daily_summary_prompt_message(reply_text)
                            mark_daily_summary_prompted(phone_number)

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
                from services.tier_service import (
                    can_add_list_item, get_tier_limits, get_user_tier,
                    format_list_item_limit_message, add_list_item_counter_to_message
                )
                allowed, limit_msg = can_add_list_item(phone_number, list_id)
                if not allowed:
                    # List is full - use Level 4 formatter
                    reply_text = format_list_item_limit_message(
                        phone_number, list_name, items_to_add, 0
                    )
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

                    # Handle partial or full adds with progressive education
                    if len(added_items) < len(items_to_add):
                        # Some items skipped - use Level 4 formatter
                        reply_text = format_list_item_limit_message(
                            phone_number, list_name, items_to_add, len(added_items)
                        )
                    else:
                        # All items added successfully
                        if len(added_items) == 1:
                            base_reply = f"Added {added_items[0]} to your {list_name}"
                        else:
                            base_reply = f"Added {len(added_items)} items to your {list_name}: {', '.join(added_items)}"

                        # Add progressive counter
                        reply_text = add_list_item_counter_to_message(phone_number, list_id, base_reply)

                    # Check if this is user's first action and prompt for daily summary
                    if should_prompt_daily_summary(phone_number):
                        reply_text = get_daily_summary_prompt_message(reply_text)
                        mark_daily_summary_prompted(phone_number)

            elif len(lists) > 1:
                # Multiple lists, ask which one (store original text for parsing later)
                # Clear pending_delete flag to avoid blocking the list selection handler
                create_or_update_user(phone_number, pending_list_item=item_text, pending_delete=False)
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
            list_filter = ai_response.get("list_filter", "").lower().strip()

            # Fallback: detect filter from user's message if AI didn't provide it
            # Pattern: "show [my] X lists" where X is the filter keyword
            if not list_filter:
                match = re.search(r'show\s+(?:my\s+)?(\w+)\s+lists?', incoming_msg.lower())
                if match:
                    potential_filter = match.group(1)
                    # Don't treat "all" or "the" as filters
                    if potential_filter not in ['all', 'the', 'my']:
                        list_filter = potential_filter
                        logger.info(f"show_all_lists: detected filter '{list_filter}' from user message")

            # Filter lists if a filter keyword is provided
            if list_filter:
                lists = [l for l in lists if list_filter in l[1].lower()]
                logger.info(f"show_all_lists: filtered by '{list_filter}', found {len(lists)} matching lists")
            else:
                logger.info(f"show_all_lists: found {len(lists)} lists")

            filter_desc = f" {list_filter}" if list_filter else ""

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
                header = f"Your{filter_desc} lists:" if list_filter else "Your lists:"
                reply_text = header + "\n\n" + "\n".join(list_lines) + "\n\nReply with a number to see that list."
                # Track that user is viewing list of lists for number selection
                create_or_update_user(phone_number, last_active_list="__LISTS__")
            else:
                if list_filter:
                    reply_text = f"You don't have any {list_filter} lists. Try 'Create a {list_filter} list'!"
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
            # Ask for confirmation before deleting
            confirm_data = json.dumps({
                'awaiting_confirmation': True,
                'type': 'list_item',
                'list_name': list_name,
                'text': item_text
            })
            create_or_update_user(phone_number, pending_reminder_delete=confirm_data)
            reply_text = f"Remove '{item_text}' from {list_name}?\n\nReply YES to confirm or CANCEL to keep it."
            log_interaction(phone_number, incoming_msg, "Asking delete_item confirmation", "delete_item_confirm", True)

        elif ai_response["action"] == "delete_list":
            list_name = ai_response.get("list_name")
            list_filter = ai_response.get("list_filter", "").lower().strip()

            # Fallback: detect filter from user's message if AI didn't provide it
            if not list_filter and not list_name:
                match = re.search(r'delete\s+(?:my\s+)?(?:all\s+)?(\w+)\s+lists?', incoming_msg.lower())
                if match:
                    potential_filter = match.group(1)
                    if potential_filter not in ['all', 'the', 'my', 'this']:
                        list_filter = potential_filter

            if list_filter:
                # Find all lists matching the filter
                all_lists = get_lists(phone_number)
                matching_lists = [(l[0], l[1], l[2]) for l in all_lists if list_filter in l[1].lower()]

                if len(matching_lists) == 0:
                    reply_text = f"You don't have any {list_filter} lists."
                elif len(matching_lists) == 1:
                    # Only one match, treat as regular delete
                    list_info = matching_lists[0]
                    create_or_update_user(phone_number, pending_delete=True, pending_list_item=list_info[1])
                    reply_text = f"Are you sure you want to delete your {list_info[1]} and all its items?\n\nReply YES to confirm."
                else:
                    # Multiple matches - ask user what to do
                    list_lines = []
                    list_ids = []
                    for i, (list_id, name, item_count) in enumerate(matching_lists, 1):
                        list_lines.append(f"{i}. {name} ({item_count} items)")
                        list_ids.append(str(list_id))

                    # Store pending delete with filter info: __DELETE_MULTI__:filter:id1,id2,id3
                    pending_value = f"__DELETE_MULTI__:{list_filter}:{','.join(list_ids)}"
                    create_or_update_user(phone_number, pending_delete=True, pending_list_item=pending_value)

                    reply_text = f"You have {len(matching_lists)} {list_filter} lists:\n\n" + "\n".join(list_lines)
                    reply_text += "\n\nReply ALL to delete all of them, or a number to delete just one."
            else:
                # Original single list delete logic
                list_info = get_list_by_name(phone_number, list_name)
                if list_info:
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
                # Fallback: check if keyword matches a stored memory
                matching_memories = search_memories(phone_number, search_term)
                if len(matching_memories) == 1:
                    memory_id, memory_text, created_at = matching_memories[0]
                    confirm_data = json.dumps({
                        'awaiting_confirmation': True,
                        'id': memory_id,
                        'text': memory_text
                    })
                    create_or_update_user(phone_number, pending_memory_delete=confirm_data)
                    display_text = memory_text[:100] + ('...' if len(memory_text) > 100 else '')
                    reply_text = f"No reminders found, but I found a memory: '{display_text}'\n\nDelete it? Reply YES to confirm or NO to cancel."
                    log_interaction(phone_number, incoming_msg, reply_text, "delete_memory_fallback", True)
                    return reply_text
                elif len(matching_memories) > 1:
                    memory_options = []
                    lines = ["No reminders found, but I found these memories:"]
                    for i, (memory_id, memory_text, created_at) in enumerate(matching_memories, 1):
                        display_text = memory_text[:80] + ('...' if len(memory_text) > 80 else '')
                        lines.append(f"{i}. {display_text}")
                        memory_options.append({'id': memory_id, 'text': memory_text})
                    lines.append("\nReply with a number to select:")
                    reply_text = "\n".join(lines)
                    create_or_update_user(phone_number, pending_memory_delete=json.dumps({'options': memory_options}))
                    log_interaction(phone_number, incoming_msg, reply_text, "delete_memory_fallback", True)
                    return reply_text
                else:
                    reply_text = f"No pending reminders or memories found matching '{search_term}'."
            elif len(matching_reminders) == 1:
                # Single match - ask for confirmation first
                reminder_id, reminder_text, reminder_date = matching_reminders[0]
                confirm_data = json.dumps({
                    'awaiting_confirmation': True,
                    'type': 'reminder',
                    'id': reminder_id,
                    'text': reminder_text
                })
                create_or_update_user(phone_number, pending_reminder_delete=confirm_data)
                reply_text = f"Delete reminder: {reminder_text}?\n\nReply YES to confirm or CANCEL to keep it."
                log_interaction(phone_number, incoming_msg, "Asking delete confirmation", "delete_reminder_confirm", True)
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

        elif ai_response["action"] == "update_reminder":
            # Update/change the time of an existing reminder
            search_term = ai_response.get("search_term", "")
            new_time_str = ai_response.get("new_time", "")  # e.g., "8:00 AM", "3:30 PM"
            new_date_str = ai_response.get("new_date", "")  # optional, YYYY-MM-DD

            # Safeguard: "daily summary" is a setting, not a reminder
            if search_term and re.search(r'\b(daily\s+)?summary\b', search_term, re.IGNORECASE) and new_time_str:
                logger.info(f"Redirecting update_reminder for '{search_term}' to daily summary time update")
                time_match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|a|p)\b', new_time_str, re.IGNORECASE)
                if time_match:
                    hour = int(time_match.group(1))
                    minute = int(time_match.group(2)) if time_match.group(2) else 0
                    am_pm_raw = time_match.group(3).lower().replace('.', '')
                    am_pm = 'am' if am_pm_raw in ['am', 'a'] else 'pm'
                    if am_pm == 'pm' and hour != 12:
                        hour += 12
                    elif am_pm == 'am' and hour == 12:
                        hour = 0
                    time_str_24 = f"{hour:02d}:{minute:02d}"

                    from models.user import get_daily_summary_settings
                    current_settings = get_daily_summary_settings(phone_number)
                    previous_enabled = current_settings['enabled'] if current_settings else False
                    previous_time = current_settings['time'] if current_settings else None

                    undo_data = json.dumps({
                        'type': 'summary_undo',
                        'previous_enabled': previous_enabled,
                        'previous_time': previous_time,
                        'new_time': time_str_24
                    })

                    create_or_update_user(
                        phone_number,
                        daily_summary_enabled=True,
                        daily_summary_time=time_str_24,
                        pending_reminder_confirmation=undo_data
                    )

                    display_am_pm = 'AM' if hour < 12 else 'PM'
                    display_hour = hour if hour <= 12 else hour - 12
                    if display_hour == 0:
                        display_hour = 12
                    display_time = f"{display_hour}:{minute:02d} {display_am_pm}"

                    reply_text = staging_prefix(f"Daily summary set for {display_time}! You'll receive a summary of your day's reminders each morning.")
                    log_interaction(phone_number, incoming_msg, reply_text, "update_settings", True)
                    return reply_text

            user_tz_str = get_user_timezone(phone_number)
            tz = pytz.timezone(user_tz_str)

            # Search for matching pending reminders
            matching_reminders = search_pending_reminders(phone_number, search_term)

            if len(matching_reminders) == 0:
                reply_text = f"No pending reminders found matching '{search_term}'."
            elif len(matching_reminders) == 1:
                reminder_id, reminder_text, current_date = matching_reminders[0]

                try:
                    # Parse the new time (e.g., "8am", "8:00 AM", "3:30pm", "8a", "8p")
                    time_match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|a|p)\b', new_time_str, re.IGNORECASE)
                    if time_match:
                        hour = int(time_match.group(1))
                        minute = int(time_match.group(2)) if time_match.group(2) else 0
                        am_pm_raw = time_match.group(3).lower().replace('.', '')
                        am_pm = 'am' if am_pm_raw in ['am', 'a'] else 'pm'

                        # Convert to 24-hour format
                        if am_pm == 'pm' and hour != 12:
                            hour += 12
                        elif am_pm == 'am' and hour == 12:
                            hour = 0

                        # Determine the date (use new_date if provided, otherwise keep current date)
                        if new_date_str:
                            date_obj = datetime.strptime(new_date_str, '%Y-%m-%d')
                        else:
                            # Keep the same date from current reminder
                            if isinstance(current_date, str):
                                utc_dt = datetime.strptime(current_date, '%Y-%m-%d %H:%M:%S')
                            else:
                                utc_dt = current_date
                            utc_dt = pytz.UTC.localize(utc_dt)
                            local_dt = utc_dt.astimezone(tz)
                            date_obj = local_dt.replace(hour=0, minute=0, second=0, microsecond=0)

                        # Create new datetime with updated time
                        new_reminder_dt = date_obj.replace(hour=hour, minute=minute, second=0, microsecond=0)

                        # Localize to user's timezone if not already
                        if new_reminder_dt.tzinfo is None:
                            aware_dt = tz.localize(new_reminder_dt)
                        else:
                            aware_dt = new_reminder_dt

                        # Convert to UTC for storage
                        utc_dt = aware_dt.astimezone(pytz.UTC)
                        new_date_utc = utc_dt.strftime('%Y-%m-%d %H:%M:%S')
                        local_time = f"{hour:02d}:{minute:02d}"

                        if update_reminder_time(phone_number, reminder_id, new_date_utc, local_time, user_tz_str):
                            readable_date = aware_dt.strftime('%A, %B %d at %I:%M %p')
                            reply_text = f"Updated your reminder to: {reminder_text} on {readable_date}"
                        else:
                            reply_text = "Couldn't update that reminder."
                    else:
                        reply_text = "I couldn't parse that time. Please try again with a time like '8am' or '3:30pm'."
                except Exception as e:
                    logger.error(f"Error updating reminder: {e}")
                    reply_text = "Sorry, I had trouble updating that reminder."
            else:
                # Multiple matches - for now, ask to be more specific
                reply_text = f"Found multiple reminders matching '{search_term}'. Please be more specific about which one to update."

            log_interaction(phone_number, incoming_msg, reply_text, "update_reminder", True)

        elif ai_response["action"] == "update_settings":
            setting = ai_response.get("setting", "")
            value = ai_response.get("value", "")

            if setting == "daily_summary_time":
                # Parse the time value (e.g., "8:00 AM", "7pm", "6:30 AM")
                time_match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.|a|p)\b', value, re.IGNORECASE)
                if time_match:
                    hour = int(time_match.group(1))
                    minute = int(time_match.group(2)) if time_match.group(2) else 0
                    am_pm_raw = time_match.group(3).lower().replace('.', '')
                    am_pm = 'am' if am_pm_raw in ['am', 'a'] else 'pm'

                    if am_pm == 'pm' and hour != 12:
                        hour += 12
                    elif am_pm == 'am' and hour == 12:
                        hour = 0

                    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                        reply_text = "Please enter a valid time like 7AM, 8:30AM, or 6PM."
                    else:
                        time_str = f"{hour:02d}:{minute:02d}"

                        # Get current settings for undo
                        from models.user import get_daily_summary_settings
                        current_settings = get_daily_summary_settings(phone_number)
                        previous_enabled = current_settings['enabled'] if current_settings else False
                        previous_time = current_settings['time'] if current_settings else None

                        undo_data = json.dumps({
                            'type': 'summary_undo',
                            'previous_enabled': previous_enabled,
                            'previous_time': previous_time,
                            'new_time': time_str
                        })

                        create_or_update_user(
                            phone_number,
                            daily_summary_enabled=True,
                            daily_summary_time=time_str,
                            pending_reminder_confirmation=undo_data
                        )

                        display_am_pm = 'AM' if hour < 12 else 'PM'
                        display_hour = hour if hour <= 12 else hour - 12
                        if display_hour == 0:
                            display_hour = 12
                        display_time = f"{display_hour}:{minute:02d} {display_am_pm}"

                        reply_text = staging_prefix(f"Daily summary set for {display_time}! You'll receive a summary of your day's reminders each morning.")
                else:
                    reply_text = "I couldn't parse that time. Please try something like '8am' or '6:30pm'."

            elif setting == "daily_summary_enabled":
                enabled = value.lower() in ['true', 'on', 'yes', 'enable']

                from models.user import get_daily_summary_settings
                current_settings = get_daily_summary_settings(phone_number)
                previous_enabled = current_settings['enabled'] if current_settings else False
                previous_time = current_settings['time'] if current_settings else None

                undo_data = json.dumps({
                    'type': 'summary_undo',
                    'previous_enabled': previous_enabled,
                    'previous_time': previous_time,
                    'action': 'enabled' if enabled else 'disabled'
                })

                create_or_update_user(phone_number, daily_summary_enabled=enabled, pending_reminder_confirmation=undo_data)

                if enabled:
                    settings = get_daily_summary_settings(phone_number)
                    time_str = settings['time'] if settings else '08:00'
                    time_parts = time_str.split(':')
                    h = int(time_parts[0])
                    m = int(time_parts[1]) if len(time_parts) > 1 else 0
                    ap = 'AM' if h < 12 else 'PM'
                    dh = h if h <= 12 else h - 12
                    if dh == 0:
                        dh = 12
                    reply_text = staging_prefix(f"Daily summary enabled! You'll receive a summary of your day's reminders at {dh}:{m:02d} {ap}.\n\nTo change the time, text: SUMMARY TIME 7AM")
                else:
                    reply_text = staging_prefix("Daily summary disabled. You'll no longer receive daily reminder summaries.")
            else:
                reply_text = "I'm not sure which setting you'd like to change. You can change your daily summary time by texting something like 'change my summary time to 8am'."

            log_interaction(phone_number, incoming_msg, reply_text, "update_settings", True)

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


@app.post("/api/signup")
async def desktop_signup(request: Request):
    """
    Desktop signup endpoint - sends SMS with onboarding prompt.
    User enters phone number on website, receives text to begin onboarding.
    """
    try:
        data = await request.json()
        phone_number = data.get('phone')

        if not phone_number:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "Phone number is required"}
            )

        # Validate and format phone number
        import re
        # Remove all non-digits
        digits_only = re.sub(r'\D', '', phone_number)

        # Check if it's a valid US number (10 or 11 digits)
        if len(digits_only) == 10:
            formatted_phone = f"+1{digits_only}"
        elif len(digits_only) == 11 and digits_only[0] == '1':
            formatted_phone = f"+{digits_only}"
        else:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "Please enter a valid US phone number"}
            )

        # Check if user already exists
        from models.user import get_user
        existing_user = get_user(formatted_phone)

        # Always send new user message (for consistent testing experience)
        # In production, you could customize this for returning users
        message = """👋 Welcome to Remyndrs!

I'm your AI-powered reminder assistant. I'll help you remember anything—from daily tasks to important dates.

No app needed - just text me naturally and I'll handle the rest!

Reply with your first name to get started, or text HELP for more info."""

        # Send SMS
        from services.sms_service import send_sms
        send_sms(formatted_phone, message)

        # Log the signup
        log_interaction(formatted_phone, "Desktop signup", "Signup SMS sent", "desktop_signup", True)

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "message": "Check your phone! We just sent you a text to get started."
            }
        )

    except Exception as e:
        logger.error(f"Error in desktop signup: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something went wrong. Please try again or text us at (855) 552-1950"}
        )


@app.post("/api/contact")
async def website_contact(request: Request):
    """
    Website contact form endpoint - receives messages from desktop users
    who can't use SMS links directly.
    """
    try:
        data = await request.json()
        phone_number = data.get('phone')
        contact_type = data.get('type', '').lower()
        message = data.get('message', '').strip()

        if not phone_number:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "Phone number is required"}
            )

        if contact_type not in ('support', 'feedback', 'question', 'bug'):
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "Invalid contact type"}
            )

        if not message:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "Message is required"}
            )

        # Validate and format phone number
        import re
        digits_only = re.sub(r'\D', '', phone_number)

        if len(digits_only) == 10:
            formatted_phone = f"+1{digits_only}"
        elif len(digits_only) == 11 and digits_only[0] == '1':
            formatted_phone = f"+{digits_only}"
        else:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "Please enter a valid US phone number"}
            )

        # Route through support tickets with appropriate category
        from services.support_service import create_categorized_ticket
        result = create_categorized_ticket(formatted_phone, message, contact_type, 'web')
        if not result['success']:
            logger.error(f"Error creating ticket for web contact: {result.get('error')}")
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "Something went wrong. Please try again."}
            )

        # Send confirmation SMS
        from services.sms_service import send_sms
        type_labels = {
            'support': 'support request',
            'feedback': 'feedback',
            'question': 'question',
            'bug': 'bug report'
        }
        type_label = type_labels[contact_type]

        try:
            confirmation_msg = f"We received your {type_label}. To continue the conversation, text us anytime at this number."
            send_sms(formatted_phone, confirmation_msg)
        except Exception as e:
            logger.error(f"Error sending contact confirmation SMS: {e}")
            # Don't fail the request if SMS fails - the feedback was already saved

        log_interaction(formatted_phone, f"[WEB CONTACT] [{contact_type.upper()}] {message}", "Contact form ticket created", f"web_contact_{contact_type}", True)

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "message": f"Your {type_label} has been received! Check your phone for a confirmation text."
            }
        )

    except Exception as e:
        logger.error(f"Error in website contact form: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something went wrong. Please try again or text us at (855) 552-1950"}
        )


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


@app.post("/admin/cleanup-duplicate-reminders")
async def cleanup_duplicate_reminders(admin: str = Depends(verify_admin)):
    """
    Clean up duplicate reminders created by the recurring reminder bug.
    Keeps the oldest reminder for each (recurring_id, date) combination and deletes the rest.
    Only affects reminders with a recurring_id (not one-time reminders).
    """
    from database import get_db_connection, return_db_connection

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Find and delete duplicates, keeping the one with the lowest ID for each (recurring_id, DATE(reminder_date))
        # This query:
        # 1. Finds all reminders that have a recurring_id
        # 2. Groups by recurring_id and date
        # 3. Keeps only the minimum ID in each group
        # 4. Deletes all others
        c.execute('''
            WITH duplicates AS (
                SELECT id, recurring_id, DATE(reminder_date) as reminder_day,
                       ROW_NUMBER() OVER (
                           PARTITION BY recurring_id, DATE(reminder_date)
                           ORDER BY id ASC
                       ) as rn
                FROM reminders
                WHERE recurring_id IS NOT NULL
            )
            DELETE FROM reminders
            WHERE id IN (
                SELECT id FROM duplicates WHERE rn > 1
            )
            RETURNING id
        ''')

        deleted_ids = [row[0] for row in c.fetchall()]
        deleted_count = len(deleted_ids)

        conn.commit()

        logger.info(f"Cleaned up {deleted_count} duplicate reminders")

        return {
            "status": "success",
            "deleted_count": deleted_count,
            "deleted_ids": deleted_ids[:100] if deleted_count > 100 else deleted_ids,  # Limit response size
            "message": f"Deleted {deleted_count} duplicate reminders"
        }

    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception(f"Error cleaning up duplicate reminders: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# APPLICATION ENTRY POINT
# =====================================================

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting uvicorn server...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
