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
from models.user import get_user, is_user_onboarded, create_or_update_user, get_user_timezone
from models.memory import save_memory, get_memories
from models.reminder import save_reminder, get_user_reminders
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
        # PENDING LIST ITEM SELECTION
        # ==========================================
        # Check if user has a pending list item and sent a number
        if user and len(user) > 18 and user[18]:  # pending_list_item exists (index 18)
            pending_item = user[18]
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
        # DELETE ALL COMMAND
        # ==========================================
        if incoming_msg.upper() == "DELETE ALL":
            resp = MessagingResponse()
            resp.message("⚠️ WARNING: This will permanently delete ALL your memories and reminders.\n\nReply YES to confirm or anything else to cancel.")
            create_or_update_user(phone_number, pending_delete=True)
            log_interaction(phone_number, incoming_msg, "Asking for delete confirmation", "delete_request", True)
            return Response(content=str(resp), media_type="application/xml")

        # ==========================================
        # HANDLE CONFIRMATION RESPONSES
        # ==========================================
        if incoming_msg.upper() == "YES":
            user = get_user(phone_number)
            if user and user[9]:  # pending_delete flag
                # Check if this is a list deletion
                pending_list_name = user[18] if len(user) > 18 else None
                if pending_list_name:
                    # Delete specific list
                    if delete_list(phone_number, pending_list_name):
                        reply_msg = f"Deleted your {pending_list_name} and all its items."
                    else:
                        reply_msg = "Couldn't delete that list."
                    create_or_update_user(phone_number, pending_delete=False, pending_list_item=None)
                    resp = MessagingResponse()
                    resp.message(reply_msg)
                    log_interaction(phone_number, incoming_msg, reply_msg, "delete_list_confirmed", True)
                    return Response(content=str(resp), media_type="application/xml")
                else:
                    # Delete all memories and reminders
                    from database import get_db_connection
                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute('DELETE FROM memories WHERE phone_number = %s', (phone_number,))
                    c.execute('DELETE FROM reminders WHERE phone_number = %s', (phone_number,))
                    conn.commit()
                    conn.close()

                    create_or_update_user(phone_number, pending_delete=False)

                    resp = MessagingResponse()
                    resp.message("All your data has been permanently deleted.")
                    log_interaction(phone_number, incoming_msg, "All data deleted", "delete_confirmed", True)
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
            if lists:
                list_lines = []
                for i, (list_id, list_name, item_count, completed_count) in enumerate(lists, 1):
                    list_lines.append(f"{i}. {list_name} ({item_count} items)")
                reply = "Your lists:\n\n" + "\n".join(list_lines) + "\n\nReply with a number to see that list:"
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
        if incoming_msg.strip().isdigit() and not (user and len(user) > 18 and user[18]):
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

        # Handle AI response based on action
        if ai_response["action"] == "store":
            save_memory(phone_number, incoming_msg, ai_response)
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
            last_active = user[19] if user and len(user) > 19 else None
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
                    # Last active list was deleted, show all lists
                    lists = get_lists(phone_number)
                    if lists:
                        list_lines = [f"{i+1}. {l[1]} ({l[2]} items)" for i, l in enumerate(lists)]
                        reply_text = "Your lists:\n\n" + "\n".join(list_lines)
                    else:
                        reply_text = "You don't have any lists yet. Try 'Create a grocery list'!"
            else:
                # No last active list, show all lists
                lists = get_lists(phone_number)
                if lists:
                    list_lines = [f"{i+1}. {l[1]} ({l[2]} items)" for i, l in enumerate(lists)]
                    reply_text = "Your lists:\n\n" + "\n".join(list_lines)
                else:
                    reply_text = "You don't have any lists yet. Try 'Create a grocery list'!"
            log_interaction(phone_number, incoming_msg, reply_text, "show_current_list", True)

        elif ai_response["action"] == "show_all_lists":
            lists = get_lists(phone_number)
            if lists:
                list_lines = []
                for i, (list_id, list_name, item_count, completed_count) in enumerate(lists, 1):
                    list_lines.append(f"{i}. {list_name} ({item_count} items)")
                reply_text = "Your lists:\n\n" + "\n".join(list_lines)
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
