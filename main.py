"""
SMS Memory Service - Main Application
Entry point for the FastAPI application
"""

import re
import pytz
import asyncio
from datetime import datetime, timedelta
from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator

# Local imports
import secrets
from config import logger, ENVIRONMENT, MAX_LISTS_PER_USER, MAX_ITEMS_PER_LIST, TWILIO_AUTH_TOKEN, ADMIN_USERNAME, ADMIN_PASSWORD, RATE_LIMIT_MESSAGES, RATE_LIMIT_WINDOW, REQUEST_TIMEOUT
from collections import defaultdict
import time
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi import Depends
from database import init_db, log_interaction
from models.user import get_user, is_user_onboarded, create_or_update_user, get_user_timezone, get_last_active_list, get_pending_list_item, get_pending_reminder_delete, get_pending_memory_delete
from models.memory import save_memory, get_memories, search_memories, delete_memory
from models.reminder import save_reminder, get_user_reminders, search_pending_reminders, delete_reminder
from models.list_model import (
    create_list, get_lists, get_list_by_name, get_list_items,
    add_list_item, mark_item_complete, mark_item_incomplete,
    delete_list_item, delete_list, rename_list, clear_list,
    find_item_in_any_list, get_list_count, get_item_count
)
from services.sms_service import send_sms
from services.ai_service import process_with_ai, parse_list_items
from services.onboarding_service import handle_onboarding
from services.reminder_service import start_reminder_checker
from services.metrics_service import track_user_activity, increment_message_count
from utils.timezone import get_user_current_time
from utils.formatting import get_help_text, format_reminders_list
from utils.validation import mask_phone_number, validate_list_name, validate_item_text, validate_message, log_security_event
from admin_dashboard import router as dashboard_router

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

# Initialize database
init_db()

# Start background reminder checker
start_reminder_checker()

logger.info(f"✅ Application initialized in {ENVIRONMENT} mode")

# =====================================================
# WEBHOOK ENDPOINT
# =====================================================

@app.post("/sms")
async def sms_reply(request: Request, Body: str = Form(...), From: str = Form(...)):
    """Handle incoming SMS from Twilio"""
    try:
        # Validate Twilio signature (skip in development for testing)
        if ENVIRONMENT != "development":
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

        # Check rate limit
        if not check_rate_limit(phone_number):
            resp = MessagingResponse()
            resp.message("You're sending messages too quickly. Please wait a moment and try again.")
            return Response(content=str(resp), media_type="application/xml")

        logger.info(f"Received from {mask_phone_number(phone_number)}: {incoming_msg[:50]}...")

        # Track user activity for metrics
        track_user_activity(phone_number)
        increment_message_count(phone_number)

        # ==========================================
        # RESET ACCOUNT COMMAND (works for everyone)
        # ==========================================
        if incoming_msg.upper() in ["RESET ACCOUNT", "RESTART"]:
            create_or_update_user(
                phone_number,
                first_name=None,
                last_name=None,
                email=None,
                zip_code=None,
                timezone='America/New_York',
                onboarding_complete=False,
                onboarding_step=1,
                pending_delete=False,
                pending_reminder_text=None,
                pending_reminder_time=None
            )

            resp = MessagingResponse()
            resp.message("✅ Your account has been reset. Let's start over!\n\nWhat's your first name?")
            log_interaction(phone_number, incoming_msg, "Account reset", "reset", True)
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

                # Parse the time
                time_parts = pending_time.split(":")
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
                resp.message(reply_text)
                return Response(content=str(resp), media_type="application/xml")

            except Exception as e:
                logger.error(f"Error processing time: {e}")
                resp = MessagingResponse()
                resp.message("Sorry, I had trouble setting that reminder. Please try again.")
                return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # PENDING REMINDER DELETE SELECTION
        # ==========================================
        # Check if user has pending reminder deletion and sent a number
        pending_reminder_data = get_pending_reminder_delete(phone_number)
        if pending_reminder_data and incoming_msg.strip().isdigit():
            import json
            try:
                reminder_options = json.loads(pending_reminder_data)
                selection = int(incoming_msg.strip())
                if 1 <= selection <= len(reminder_options):
                    selected_reminder = reminder_options[selection - 1]
                    reminder_id = selected_reminder['id']
                    reminder_text = selected_reminder['text']

                    if delete_reminder(phone_number, reminder_id):
                        reply_msg = f"Deleted your reminder: {reminder_text}"
                    else:
                        reply_msg = "Couldn't delete that reminder."

                    # Clear pending delete
                    create_or_update_user(phone_number, pending_reminder_delete=None)

                    resp = MessagingResponse()
                    resp.message(reply_msg)
                    log_interaction(phone_number, incoming_msg, reply_msg, "delete_reminder_selected", True)
                    return Response(content=str(resp), media_type="application/xml")
                else:
                    resp = MessagingResponse()
                    resp.message(f"Please reply with a number between 1 and {len(reminder_options)}")
                    return Response(content=str(resp), media_type="application/xml")
            except (json.JSONDecodeError, KeyError) as e:
                logger.error(f"Error parsing pending reminder delete data: {e}")
                create_or_update_user(phone_number, pending_reminder_delete=None)

        # ==========================================
        # PENDING MEMORY DELETE SELECTION/CONFIRMATION
        # ==========================================
        # Check if user has pending memory deletion
        pending_memory_data = get_pending_memory_delete(phone_number)
        if pending_memory_data:
            import json
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
        pending_item = get_pending_list_item(phone_number)
        if pending_item:
            if incoming_msg.strip().isdigit():
                list_num = int(incoming_msg.strip())
                lists = get_lists(phone_number)
                if 1 <= list_num <= len(lists):
                    selected_list = lists[list_num - 1]
                    list_id = selected_list[0]
                    list_name = selected_list[1]

                    # Parse multiple items from the pending item
                    items_to_add = parse_list_items(pending_item)

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
        # FEEDBACK HANDLING
        # ==========================================
        if incoming_msg.upper().startswith("FEEDBACK:"):
            feedback_message = incoming_msg[9:].strip()  # Extract everything after "feedback:"
            if feedback_message:
                # Save feedback to database
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
        # DELETE ALL COMMANDS (separated by type)
        # ==========================================
        msg_upper = incoming_msg.upper()

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
            memories = get_memories(phone_number)
            if memories:
                memory_list = "\n\n".join([f"{i+1}. {m[0]}" for i, m in enumerate(memories[:20])])
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

        ai_response = process_with_ai(normalized_msg, phone_number, None)
        logger.info(f"AI action: {ai_response.get('action')}")

        # Handle AI response based on action
        if ai_response["action"] == "store":
            # Use memory_text if AI provided date-converted version, otherwise use original
            memory_text = ai_response.get("memory_text", incoming_msg)
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

            try:
                user_tz_str = get_user_timezone(phone_number)
                tz = pytz.timezone(user_tz_str)

                naive_dt = datetime.strptime(reminder_date, '%Y-%m-%d %H:%M:%S')
                aware_dt = tz.localize(naive_dt)

                utc_dt = aware_dt.astimezone(pytz.UTC)
                reminder_date_utc = utc_dt.strftime('%Y-%m-%d %H:%M:%S')

                save_reminder(phone_number, reminder_text, reminder_date_utc)
            except Exception as e:
                logger.error(f"Error converting reminder time to UTC: {e}")
                save_reminder(phone_number, reminder_text, reminder_date)

            reply_text = ai_response.get("confirmation", "Got it! I'll remind you.")
            log_interaction(phone_number, incoming_msg, reply_text, "reminder", True)

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
                # Check list limit
                list_count = get_list_count(phone_number)
                if list_count >= MAX_LISTS_PER_USER:
                    reply_text = f"You've reached the maximum of {MAX_LISTS_PER_USER} lists. Delete a list to create a new one."
                elif get_list_by_name(phone_number, list_name):
                    reply_text = f"You already have a list called '{list_name}'."
                else:
                    create_list(phone_number, list_name)
                    reply_text = ai_response.get("confirmation", f"Created your {list_name}!")
            log_interaction(phone_number, incoming_msg, reply_text, "create_list", True)

        elif ai_response["action"] == "add_to_list":
            list_name = ai_response.get("list_name")
            item_text = ai_response.get("item_text")

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
                items_to_add = parse_list_items(item_text)

                list_info = get_list_by_name(phone_number, list_name)

                # Auto-create list if it doesn't exist
                if not list_info:
                    list_count = get_list_count(phone_number)
                    if list_count >= MAX_LISTS_PER_USER:
                        reply_text = f"You've reached the maximum of {MAX_LISTS_PER_USER} lists. Delete a list first."
                    else:
                        list_id = create_list(phone_number, list_name)
                        # Add all parsed items
                        added_items = []
                        for item in items_to_add:
                            if len(added_items) < MAX_ITEMS_PER_LIST:
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
                    item_count = get_item_count(list_id)
                    available_slots = MAX_ITEMS_PER_LIST - item_count

                    if available_slots <= 0:
                        reply_text = f"Your {list_name} is full ({MAX_ITEMS_PER_LIST} items max). Remove some items first."
                    else:
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
                items_to_add = parse_list_items(item_text)

                item_count = get_item_count(list_id)
                available_slots = MAX_ITEMS_PER_LIST - item_count

                if available_slots <= 0:
                    reply_text = f"Your {list_name} is full ({MAX_ITEMS_PER_LIST} items max). Remove some items first."
                else:
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
            import json
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
            import json
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

        # Send response
        resp = MessagingResponse()
        resp.message(reply_text)
        return Response(content=str(resp), media_type="application/xml")
    
    except Exception as e:
        logger.error(f"❌ CRITICAL ERROR in webhook: {e}", exc_info=True)
        resp = MessagingResponse()
        resp.message("Sorry, something went wrong. Please try again in a moment.")
        return Response(content=str(resp), media_type="application/xml")

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

@app.get("/memories/{phone_number}")
async def view_memories(phone_number: str, admin: str = Depends(verify_admin)):
    """View all memories for a phone number - for testing/admin"""
    import json
    memories = get_memories(phone_number)
    return {
        "phone_number": phone_number,
        "total_memories": len(memories),
        "memories": [
            {
                "text": m[0],
                "data": json.loads(m[1]) if m[1] else {},
                "created": m[2]
            } for m in memories
        ]
    }

@app.get("/reminders/{phone_number}")
async def view_reminders(phone_number: str, admin: str = Depends(verify_admin)):
    """View all reminders for a phone number - for testing/admin"""
    reminders = get_user_reminders(phone_number)
    return {
        "phone_number": phone_number,
        "total_reminders": len(reminders),
        "reminders": [
            {
                "text": r[0],
                "date": r[1],
                "sent": bool(r[2])
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
