"""
Admin Dashboard
HTML dashboard for viewing metrics and broadcast messaging
"""

import secrets
import asyncio
import threading
import time
from datetime import datetime, timedelta
from html import escape as html_escape
import pytz
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request, Form, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional
from services.metrics_service import get_all_metrics, get_cost_analytics
from services.sms_service import send_sms
from database import (
    get_db_connection, return_db_connection, get_setting, set_setting,
    get_recent_logs, get_flagged_conversations, mark_analysis_reviewed,
    manual_flag_conversation, mark_conversation_good, get_good_conversations,
    dismiss_conversation,
    get_monitoring_connection, return_monitoring_connection
)
from config import ADMIN_USERNAME, ADMIN_PASSWORD, logger
from utils.validation import log_security_event, mask_phone_number
from utils.encryption import safe_decrypt
from utils.auth import enforce_auth_rate_limit, record_auth_failure
import re

def parse_date_filter(start_date: Optional[str], end_date: Optional[str]):
    """Parse date filter strings into datetime objects."""
    sd = datetime.strptime(start_date, '%Y-%m-%d') if start_date else None
    ed = datetime.strptime(end_date, '%Y-%m-%d') if end_date else None
    return sd, ed


# Broadcast time window (8am - 8pm in user's local timezone)
BROADCAST_START_HOUR = 8
BROADCAST_END_HOUR = 20  # 8pm
DEFAULT_TIMEZONE = 'America/New_York'


def is_within_broadcast_window(timezone_str: str) -> bool:
    """Check if current time is within 8am-8pm for the given timezone"""
    try:
        tz = pytz.timezone(timezone_str or DEFAULT_TIMEZONE)
    except pytz.UnknownTimezoneError:
        tz = pytz.timezone(DEFAULT_TIMEZONE)

    local_time = datetime.now(tz)
    return BROADCAST_START_HOUR <= local_time.hour < BROADCAST_END_HOUR


def validate_e164_phone(phone: str) -> str:
    """Validate and normalize a phone number to E.164 format. Returns normalized number or raises HTTPException."""
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 10:
        digits = '1' + digits
    if len(digits) == 11 and digits.startswith('1'):
        return '+' + digits
    raise HTTPException(status_code=400, detail="Invalid phone number. Use format: +1XXXXXXXXXX or (XXX) XXX-XXXX")


router = APIRouter()
security = HTTPBasic()


class BroadcastRequest(BaseModel):
    message: str
    audience: str  # "all", "free", "premium", "single"
    phone_number: Optional[str] = None  # Required when audience == "single"


class NudgeRequest(BaseModel):
    phone_numbers: list[str]
    message: str


def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """Verify admin credentials for protected endpoints"""
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=500, detail="Admin password not configured")

    enforce_auth_rate_limit(credentials.username, "dashboard")

    correct_username = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)

    if not (correct_username and correct_password):
        record_auth_failure(credentials.username)
        log_security_event("AUTH_FAILURE", {"username": credentials.username, "endpoint": "dashboard"})
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# =====================================================
# OVERVIEW STATS API ENDPOINT
# =====================================================

@router.get("/admin/stats/overview")
async def get_overview_stats(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: str = Depends(verify_admin)
):
    """Get all overview metrics with optional date filtering"""
    try:
        sd, ed = parse_date_filter(start_date, end_date)
        metrics = get_all_metrics(start_date=sd, end_date=ed)
        return JSONResponse(content=metrics)
    except Exception as e:
        logger.error(f"Error getting overview stats: {e}")
        raise HTTPException(status_code=500, detail="Error getting overview stats")


# =====================================================
# BROADCAST API ENDPOINTS
# =====================================================

@router.get("/admin/broadcast/stats")
async def get_broadcast_stats(admin: str = Depends(verify_admin)):
    """Get user counts by plan type for broadcast targeting, including timezone-aware counts"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get users with their timezones and plan types (exclude opted-out users)
        c.execute('''
            SELECT
                phone_number,
                COALESCE(premium_status, 'free') as plan,
                timezone
            FROM users
            WHERE onboarding_complete = TRUE
            AND (opted_out = FALSE OR opted_out IS NULL)
        ''')
        results = c.fetchall()

        # Total counts and in-window counts
        stats = {
            "all": 0, "free": 0, "premium": 0,
            "all_in_window": 0, "free_in_window": 0, "premium_in_window": 0
        }

        for phone, plan, timezone in results:
            in_window = is_within_broadcast_window(timezone)

            if plan == 'free':
                stats['free'] += 1
                if in_window:
                    stats['free_in_window'] += 1
            elif plan == 'premium':
                stats['premium'] += 1
                if in_window:
                    stats['premium_in_window'] += 1

            stats['all'] += 1
            if in_window:
                stats['all_in_window'] += 1

        return JSONResponse(content=stats)
    except Exception as e:
        logger.error(f"Error getting broadcast stats: {e}")
        raise HTTPException(status_code=500, detail="Error getting stats")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/broadcast/recipients-preview")
async def get_recipients_preview(audience: str = "all", admin: str = Depends(verify_admin)):
    """Preview which users will receive a broadcast and who's excluded (and why)"""
    if audience not in ("all", "free", "premium"):
        raise HTTPException(status_code=400, detail="Invalid audience. Must be all, free, or premium.")

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get all onboarded users with relevant fields
        c.execute('''
            SELECT phone_number, first_name, timezone,
                   COALESCE(premium_status, 'free') as plan,
                   COALESCE(opted_out, FALSE) as opted_out
            FROM users
            WHERE onboarding_complete = TRUE
        ''')
        rows = c.fetchall()

        included = []
        excluded = []
        excluded_opted_out = 0
        excluded_outside_window = 0

        for phone, first_name, timezone_str, plan, opted_out in rows:
            # Apply audience filter
            if audience == "free" and plan == "premium":
                continue
            if audience == "premium" and plan != "premium":
                continue

            masked = mask_phone_number(phone)
            name = safe_decrypt(first_name, "") if first_name else None

            # Determine local time for display
            try:
                tz = pytz.timezone(timezone_str or DEFAULT_TIMEZONE)
            except pytz.UnknownTimezoneError:
                tz = pytz.timezone(DEFAULT_TIMEZONE)
            local_now = datetime.now(tz)
            local_time_str = local_now.strftime("%I:%M %p").lstrip("0")

            user_info = {
                "phone": masked,
                "name": name,
                "tier": plan,
                "timezone": timezone_str or DEFAULT_TIMEZONE,
                "local_time": local_time_str
            }

            if opted_out:
                excluded.append({**user_info, "reason": "opted_out", "phone_full": phone})
                excluded_opted_out += 1
            elif not is_within_broadcast_window(timezone_str):
                excluded.append({**user_info, "reason": "outside_window"})
                excluded_outside_window += 1
            else:
                included.append(user_info)

        total_onboarded = len(included) + len(excluded)

        return JSONResponse(content={
            "included": included,
            "excluded": excluded,
            "summary": {
                "total_onboarded": total_onboarded,
                "included": len(included),
                "excluded_opted_out": excluded_opted_out,
                "excluded_outside_window": excluded_outside_window
            }
        })
    except Exception as e:
        logger.error(f"Error getting recipients preview: {e}")
        raise HTTPException(status_code=500, detail="Error getting recipients preview")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/broadcast/history")
async def get_broadcast_history(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: str = Depends(verify_admin)
):
    """Get history of past broadcasts"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        sd, ed = parse_date_filter(start_date, end_date)

        query = '''
            SELECT id, sender, message, audience, recipient_count,
                   success_count, fail_count, status, created_at, completed_at, source
            FROM broadcast_logs
            WHERE 1=1
        '''
        params = []
        if sd:
            query += ' AND created_at >= %s'
            params.append(sd)
        if ed:
            query += ' AND created_at < %s'
            params.append(ed)
        query += ' ORDER BY created_at DESC LIMIT 20'
        c.execute(query, params)
        results = c.fetchall()

        history = []
        for row in results:
            history.append({
                "id": row[0],
                "sender": row[1],
                "message": row[2][:100] + "..." if len(row[2]) > 100 else row[2],
                "full_message": row[2],
                "audience": row[3],
                "recipient_count": row[4],
                "success_count": row[5],
                "fail_count": row[6],
                "status": row[7],
                "created_at": row[8].isoformat() if row[8] else None,
                "completed_at": row[9].isoformat() if row[9] else None,
                "source": row[10] or "immediate"
            })

        return JSONResponse(content=history)
    except Exception as e:
        logger.error(f"Error getting broadcast history: {e}")
        raise HTTPException(status_code=500, detail="Error getting history")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/broadcast/status/{broadcast_id}")
async def get_broadcast_status(broadcast_id: int, admin: str = Depends(verify_admin)):
    """Get status of a specific broadcast"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute('''
            SELECT id, sender, message, audience, recipient_count,
                   success_count, fail_count, status, created_at, completed_at
            FROM broadcast_logs
            WHERE id = %s
        ''', (broadcast_id,))
        row = c.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Broadcast not found")

        return JSONResponse(content={
            "id": row[0],
            "sender": row[1],
            "message": row[2],
            "audience": row[3],
            "recipient_count": row[4],
            "success_count": row[5],
            "fail_count": row[6],
            "status": row[7],
            "created_at": row[8].isoformat() if row[8] else None,
            "completed_at": row[9].isoformat() if row[9] else None
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting broadcast status: {e}")
        raise HTTPException(status_code=500, detail="Error getting status")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/recent-messages")
async def get_recent_user_messages(admin: str = Depends(verify_admin)):
    """Get the last 10 messages received from users"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute('''
            SELECT l.id, l.phone_number, u.first_name, l.message_in, l.intent, l.created_at
            FROM logs l
            LEFT JOIN users u ON l.phone_number = u.phone_number
            ORDER BY l.created_at DESC
            LIMIT 10
        ''')
        results = c.fetchall()

        messages = []
        for row in results:
            messages.append({
                "id": row[0],
                "phone_number": row[1][-4:] if row[1] else "****",  # Only show last 4 digits
                "first_name": row[2] or "Unknown",
                "message": row[3],
                "intent": row[4],
                "created_at": row[5].isoformat() if row[5] else None
            })

        return JSONResponse(content=messages)
    except Exception as e:
        logger.error(f"Error getting recent messages: {e}")
        raise HTTPException(status_code=500, detail="Error getting recent messages")
    finally:
        if conn:
            return_db_connection(conn)


BROADCAST_PREFIX = "[Remyndrs System Message] "

def send_broadcast_messages(broadcast_id: int, phone_numbers: list, message: str):
    """Background task to send broadcast messages with rate limiting"""
    import time

    conn = None
    success_count = 0
    fail_count = 0

    # Prepend the broadcast prefix to the message
    full_message = BROADCAST_PREFIX + message

    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Update status to sending
        c.execute(
            "UPDATE broadcast_logs SET status = 'sending' WHERE id = %s",
            (broadcast_id,)
        )
        conn.commit()

        for i, phone in enumerate(phone_numbers):
            try:
                send_sms(phone, full_message, message_type="broadcast")
                success_count += 1
            except Exception as e:
                logger.error(f"Failed to send broadcast to {phone}: {e}")
                fail_count += 1

            # Update progress every 10 messages
            if (i + 1) % 10 == 0:
                c.execute(
                    "UPDATE broadcast_logs SET success_count = %s, fail_count = %s WHERE id = %s",
                    (success_count, fail_count, broadcast_id)
                )
                conn.commit()

            # Rate limit: 100ms delay between messages to avoid Twilio limits
            time.sleep(0.1)

        # Final update
        c.execute('''
            UPDATE broadcast_logs
            SET success_count = %s, fail_count = %s, status = 'completed', completed_at = NOW()
            WHERE id = %s
        ''', (success_count, fail_count, broadcast_id))
        conn.commit()

        logger.info(f"Broadcast {broadcast_id} completed: {success_count} success, {fail_count} failed")

    except Exception as e:
        logger.error(f"Broadcast {broadcast_id} error: {e}")
        if conn:
            c = conn.cursor()
            c.execute(
                "UPDATE broadcast_logs SET status = 'failed', completed_at = NOW() WHERE id = %s",
                (broadcast_id,)
            )
            conn.commit()
    finally:
        if conn:
            return_db_connection(conn)


@router.post("/admin/broadcast/send")
async def send_broadcast(request: BroadcastRequest, background_tasks: BackgroundTasks, admin: str = Depends(verify_admin)):
    """Send a broadcast message to selected audience (only users within 8am-8pm local time)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        skipped_count = 0

        if request.audience == "single":
            # Single number test mode - skip user query, send directly
            if not request.phone_number:
                raise HTTPException(status_code=400, detail="Phone number required for single number mode")
            phone = validate_e164_phone(request.phone_number)
            phone_numbers = [phone]
        else:
            # Build query based on audience - include timezone for filtering
            # Exclude opted-out users (STOP command compliance)
            if request.audience == "all":
                c.execute('''
                    SELECT phone_number, timezone FROM users
                    WHERE onboarding_complete = TRUE
                    AND (opted_out = FALSE OR opted_out IS NULL)
                ''')
            elif request.audience == "free":
                c.execute('''
                    SELECT phone_number, timezone FROM users
                    WHERE onboarding_complete = TRUE
                    AND (premium_status = 'free' OR premium_status IS NULL)
                    AND (opted_out = FALSE OR opted_out IS NULL)
                ''')
            elif request.audience == "premium":
                c.execute('''
                    SELECT phone_number, timezone FROM users
                    WHERE onboarding_complete = TRUE
                    AND premium_status = 'premium'
                    AND (opted_out = FALSE OR opted_out IS NULL)
                ''')
            else:
                raise HTTPException(status_code=400, detail="Invalid audience")

            results = c.fetchall()

            # Filter to only users within the 8am-8pm window in their timezone
            phone_numbers = [
                r[0] for r in results
                if is_within_broadcast_window(r[1])
            ]

            total_audience = len(results)
            skipped_count = total_audience - len(phone_numbers)

            if not phone_numbers:
                raise HTTPException(
                    status_code=400,
                    detail=f"No recipients currently in the 8am-8pm window. {total_audience} users are outside the allowed time."
                )

        # Create broadcast log entry
        c.execute('''
            INSERT INTO broadcast_logs (sender, message, audience, recipient_count, status, source)
            VALUES (%s, %s, %s, %s, 'pending', 'immediate')
            RETURNING id
        ''', (admin, request.message, request.audience, len(phone_numbers)))
        broadcast_id = c.fetchone()[0]
        conn.commit()

        # Start background task to send messages
        background_tasks.add_task(send_broadcast_messages, broadcast_id, phone_numbers, request.message)

        logger.info(f"Broadcast {broadcast_id} started by {admin}: {len(phone_numbers)} recipients ({skipped_count} skipped - outside time window)")

        return JSONResponse(content={
            "broadcast_id": broadcast_id,
            "recipient_count": len(phone_numbers),
            "skipped_count": skipped_count,
            "status": "started",
            "message": f"Sending to {len(phone_numbers)} recipients..." + (f" ({skipped_count} skipped - outside 8am-8pm)" if skipped_count > 0 else "")
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting broadcast: {e}")
        raise HTTPException(status_code=500, detail="Error starting broadcast")
    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# SCHEDULED BROADCAST API ENDPOINTS
# =====================================================

class ScheduleBroadcastRequest(BaseModel):
    message: str
    audience: str
    scheduled_date: str  # ISO format datetime string
    phone_number: Optional[str] = None  # Required when audience == "single"


@router.post("/admin/broadcast/schedule")
async def schedule_broadcast(request: ScheduleBroadcastRequest, admin: str = Depends(verify_admin)):
    """Schedule a broadcast for future delivery"""
    conn = None
    try:
        if request.audience not in ["all", "free", "premium", "single"]:
            raise HTTPException(status_code=400, detail="Invalid audience type")

        if not request.message.strip():
            raise HTTPException(status_code=400, detail="Message cannot be empty")

        # Validate phone number for single number mode
        target_phone = None
        if request.audience == "single":
            if not request.phone_number:
                raise HTTPException(status_code=400, detail="Phone number required for single number mode")
            target_phone = validate_e164_phone(request.phone_number)

        # Parse the scheduled date
        try:
            scheduled_dt = datetime.fromisoformat(request.scheduled_date.replace('Z', '+00:00'))
            # Convert to naive UTC for storage and comparison
            if scheduled_dt.tzinfo is not None:
                scheduled_dt = scheduled_dt.astimezone(pytz.UTC).replace(tzinfo=None)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format")

        # Ensure scheduled time is in the future
        if scheduled_dt <= datetime.utcnow():
            raise HTTPException(status_code=400, detail="Scheduled time must be in the future")

        conn = get_db_connection()
        c = conn.cursor()

        # Insert scheduled broadcast
        c.execute('''
            INSERT INTO scheduled_broadcasts (sender, message, audience, scheduled_date, status, target_phone)
            VALUES (%s, %s, %s, %s, 'scheduled', %s)
            RETURNING id
        ''', (admin, request.message.strip(), request.audience, scheduled_dt, target_phone))

        broadcast_id = c.fetchone()[0]
        conn.commit()

        return JSONResponse(content={
            "broadcast_id": broadcast_id,
            "status": "scheduled",
            "scheduled_date": scheduled_dt.isoformat(),
            "message": f"Broadcast scheduled for {scheduled_dt.strftime('%B %d, %Y at %I:%M %p')} UTC"
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error scheduling broadcast: {e}")
        raise HTTPException(status_code=500, detail="Error scheduling broadcast")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/broadcast/scheduled")
async def get_scheduled_broadcasts(admin: str = Depends(verify_admin)):
    """Get all scheduled broadcasts"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute('''
            SELECT id, sender, message, audience, scheduled_date, status,
                   recipient_count, success_count, fail_count, created_at, sent_at
            FROM scheduled_broadcasts
            WHERE status IN ('scheduled', 'sending')
            ORDER BY scheduled_date ASC
        ''')
        results = c.fetchall()

        broadcasts = []
        for row in results:
            broadcasts.append({
                "id": row[0],
                "sender": row[1],
                "message": row[2][:100] + "..." if len(row[2]) > 100 else row[2],
                "full_message": row[2],
                "audience": row[3],
                "scheduled_date": row[4].isoformat() if row[4] else None,
                "status": row[5],
                "recipient_count": row[6],
                "success_count": row[7],
                "fail_count": row[8],
                "created_at": row[9].isoformat() if row[9] else None,
                "sent_at": row[10].isoformat() if row[10] else None
            })

        return JSONResponse(content=broadcasts)

    except Exception as e:
        logger.error(f"Error getting scheduled broadcasts: {e}")
        raise HTTPException(status_code=500, detail="Error getting scheduled broadcasts")
    finally:
        if conn:
            return_db_connection(conn)


@router.delete("/admin/broadcast/scheduled/{broadcast_id}/cancel")
async def cancel_scheduled_broadcast(broadcast_id: int, admin: str = Depends(verify_admin)):
    """Cancel a scheduled broadcast"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Check if broadcast exists and is still scheduled
        c.execute('SELECT status FROM scheduled_broadcasts WHERE id = %s', (broadcast_id,))
        result = c.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Broadcast not found")

        if result[0] != 'scheduled':
            raise HTTPException(status_code=400, detail=f"Cannot cancel broadcast with status '{result[0]}'")

        # Update status to cancelled
        c.execute('''
            UPDATE scheduled_broadcasts
            SET status = 'cancelled'
            WHERE id = %s
        ''', (broadcast_id,))
        conn.commit()

        return JSONResponse(content={
            "success": True,
            "message": "Broadcast cancelled"
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error cancelling broadcast: {e}")
        raise HTTPException(status_code=500, detail="Error cancelling broadcast")
    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# SCHEDULED BROADCAST CHECKER
# =====================================================

def send_scheduled_broadcast(broadcast_id: int, message: str, audience: str, sender: str = 'system', target_phone: str = None):
    """Send a scheduled broadcast - filters recipients and sends messages"""
    conn = None
    success_count = 0
    fail_count = 0

    full_message = BROADCAST_PREFIX + message

    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Update status to sending
        c.execute(
            "UPDATE scheduled_broadcasts SET status = 'sending' WHERE id = %s",
            (broadcast_id,)
        )
        conn.commit()

        if audience == "single" and target_phone:
            # Single number mode - send directly, skip user query
            phone_numbers = [target_phone]
        else:
            # Get recipients based on audience (exclude opted-out users)
            if audience == "all":
                c.execute('''
                    SELECT phone_number, timezone FROM users
                    WHERE onboarding_complete = TRUE
                    AND (opted_out = FALSE OR opted_out IS NULL)
                ''')
            elif audience == "free":
                c.execute('''
                    SELECT phone_number, timezone FROM users
                    WHERE onboarding_complete = TRUE
                    AND (premium_status = 'free' OR premium_status IS NULL)
                    AND (opted_out = FALSE OR opted_out IS NULL)
                ''')
            elif audience == "premium":
                c.execute('''
                    SELECT phone_number, timezone FROM users
                    WHERE onboarding_complete = TRUE
                    AND premium_status = 'premium'
                    AND (opted_out = FALSE OR opted_out IS NULL)
                ''')
            else:
                logger.error(f"Invalid audience for scheduled broadcast {broadcast_id}: {audience}")
                return

            results = c.fetchall()

            # Filter to only users within the 8am-8pm window
            phone_numbers = [r[0] for r in results if is_within_broadcast_window(r[1])]

        if not phone_numbers:
            logger.info(f"Scheduled broadcast {broadcast_id}: No recipients in time window")
            c.execute('''
                UPDATE scheduled_broadcasts
                SET status = 'completed', recipient_count = 0, sent_at = NOW()
                WHERE id = %s
            ''', (broadcast_id,))
            conn.commit()
            # Still log to broadcast_logs so it appears in history
            c.execute('''
                INSERT INTO broadcast_logs (sender, message, audience, recipient_count, success_count, fail_count, status, completed_at, source)
                VALUES (%s, %s, %s, 0, 0, 0, 'completed', NOW(), 'scheduled')
            ''', (sender, message, audience))
            conn.commit()
            return

        # Update recipient count
        c.execute(
            "UPDATE scheduled_broadcasts SET recipient_count = %s WHERE id = %s",
            (len(phone_numbers), broadcast_id)
        )
        conn.commit()

        # Send messages with rate limiting
        for i, phone in enumerate(phone_numbers):
            try:
                send_sms(phone, full_message, message_type="broadcast")
                success_count += 1
            except Exception as e:
                logger.error(f"Failed to send scheduled broadcast to {phone}: {e}")
                fail_count += 1

            # Update progress every 10 messages
            if (i + 1) % 10 == 0:
                c.execute(
                    "UPDATE scheduled_broadcasts SET success_count = %s, fail_count = %s WHERE id = %s",
                    (success_count, fail_count, broadcast_id)
                )
                conn.commit()

            # Rate limit: 100ms delay
            time.sleep(0.1)

        # Final update on scheduled_broadcasts
        c.execute('''
            UPDATE scheduled_broadcasts
            SET success_count = %s, fail_count = %s, status = 'completed', sent_at = NOW()
            WHERE id = %s
        ''', (success_count, fail_count, broadcast_id))
        conn.commit()

        # Also insert into broadcast_logs so it appears in Broadcast History
        c.execute('''
            INSERT INTO broadcast_logs (sender, message, audience, recipient_count, success_count, fail_count, status, completed_at, source)
            VALUES (%s, %s, %s, %s, %s, %s, 'completed', NOW(), 'scheduled')
        ''', (sender, message, audience, len(phone_numbers), success_count, fail_count))
        conn.commit()

        logger.info(f"Scheduled broadcast {broadcast_id} completed: {success_count} success, {fail_count} failed")

    except Exception as e:
        logger.error(f"Scheduled broadcast {broadcast_id} error: {e}")
        if conn:
            try:
                c = conn.cursor()
                c.execute(
                    "UPDATE scheduled_broadcasts SET status = 'failed', sent_at = NOW() WHERE id = %s",
                    (broadcast_id,)
                )
                conn.commit()
            except Exception:
                pass
    finally:
        if conn:
            return_db_connection(conn)


def check_scheduled_broadcasts():
    """Background thread that checks for due scheduled broadcasts"""
    logger.info("Starting scheduled broadcast checker")
    consecutive_failures = 0
    max_consecutive_failures = 10

    while True:
        conn = None
        try:
            now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            logger.info(f"Checking for scheduled broadcasts at {now}")

            try:
                conn = get_db_connection()
                c = conn.cursor()

                # Find broadcasts that are due
                c.execute('''
                    SELECT id, message, audience, sender, target_phone
                    FROM scheduled_broadcasts
                    WHERE scheduled_date <= %s AND status = 'scheduled'
                ''', (now,))

                due_broadcasts = c.fetchall()
            except Exception as db_err:
                logger.error(f"DB error in scheduled broadcast checker: {db_err}")
                due_broadcasts = []
            finally:
                if conn:
                    return_db_connection(conn)
                    conn = None

            if due_broadcasts:
                logger.info(f"Found {len(due_broadcasts)} scheduled broadcasts to send")

            for broadcast_id, message, audience, sender, target_phone in due_broadcasts:
                logger.info(f"Sending scheduled broadcast {broadcast_id}")
                try:
                    send_scheduled_broadcast(broadcast_id, message, audience, sender=sender or 'system', target_phone=target_phone)
                except Exception as send_err:
                    logger.error(f"Error sending scheduled broadcast {broadcast_id}: {send_err}")

            consecutive_failures = 0  # Reset on successful loop

        except Exception as e:
            consecutive_failures += 1
            logger.error(f"Error in scheduled broadcast checker ({consecutive_failures}/{max_consecutive_failures}): {e}")
            if consecutive_failures >= max_consecutive_failures:
                logger.critical(f"Broadcast checker exceeded {max_consecutive_failures} consecutive failures, stopping thread")
                return

        # Backoff on repeated failures (60s normal, up to 5min on failures)
        sleep_time = min(60 * (2 ** min(consecutive_failures, 3)), 300) if consecutive_failures > 0 else 60
        time.sleep(sleep_time)


def start_broadcast_checker():
    """Start the scheduled broadcast checker in a daemon thread"""
    thread = threading.Thread(target=check_scheduled_broadcasts, daemon=True)
    thread.start()
    logger.info("Scheduled broadcast checker thread started")


# =====================================================
# FOUNDER SURVEY (one-off: ask active users what they use Remyndrs for)
# =====================================================

def _founder_survey_recipients(c):
    """Active-in-last-14-days users, excluding opted-out and the founder's own
    numbers. Returns list of dicts with decrypted name + already-surveyed flag."""
    from config import FOUNDER_SURVEY_EXCLUDE_PHONES
    c.execute('''
        SELECT u.phone_number, u.first_name, u.last_active_at, u.timezone,
               EXISTS(
                   SELECT 1 FROM smart_nudges sn
                   WHERE sn.phone_number = u.phone_number
                     AND sn.nudge_type = 'founder_survey'
               ) AS already_surveyed
        FROM users u
        WHERE u.onboarding_complete = TRUE
          AND u.last_active_at >= NOW() - INTERVAL '14 days'
          AND (u.opted_out = FALSE OR u.opted_out IS NULL)
          AND (u.lifecycle_messages_opted_out = FALSE OR u.lifecycle_messages_opted_out IS NULL)
          AND u.phone_number <> ALL(%s)
        ORDER BY u.last_active_at DESC
    ''', (FOUNDER_SURVEY_EXCLUDE_PHONES,))
    recipients = []
    for phone, first_name, last_active, timezone_str, already in c.fetchall():
        try:
            tz = pytz.timezone(timezone_str or DEFAULT_TIMEZONE)
        except pytz.UnknownTimezoneError:
            tz = pytz.timezone(DEFAULT_TIMEZONE)
        local_time = datetime.now(tz).strftime("%I:%M %p").lstrip("0")
        recipients.append({
            "phone_full": phone,
            "phone": mask_phone_number(phone),
            "name": (safe_decrypt(first_name, "") if first_name else "") or None,
            "last_active": last_active.strftime("%Y-%m-%d") if last_active else None,
            "timezone": timezone_str or DEFAULT_TIMEZONE,
            "local_time": local_time,
            # Only send during daytime (8am-8pm local), matching broadcast policy
            "in_window": is_within_broadcast_window(timezone_str),
            "already_surveyed": bool(already),
        })
    return recipients


class SportsScoreDryRunRequest(BaseModel):
    phone: str
    fake_game: bool = True
    scoreboard_date: Optional[str] = None  # YYYYMMDD for a real ESPN preseason date
    send_invite: bool = False
    full_loop: bool = False  # invite + Bengals/Lions opt-in + staggered asks
    teams: Optional[list] = None  # default CIN, DET when full_loop


@router.post("/admin/sports-scores/dry-run")
async def sports_score_dry_run(
    request: SportsScoreDryRunRequest,
    admin: str = Depends(verify_admin),
):
    """Trigger a fake/preseason morning-after ask for one founder phone.

    Does not send weekly or dormant invites to production. Phone must be on the
    founder allowlist (FOUNDER_SURVEY_EXCLUDE_PHONES or nfl_score_dry_run_phones).

    full_loop=true: send locked invite copy, opt that phone into Bengals + Lions,
    then fire canned/ESPN asks staggered by a few minutes.
    """
    from services.sports_score_service import process_morning_asks, is_dry_run_allowed
    from models.sports import get_optin, list_optins

    phone = (request.phone or "").strip()
    if not phone.startswith("+"):
        raise HTTPException(status_code=400, detail="phone must be E.164 (e.g. +18593935374)")
    if not is_dry_run_allowed(phone):
        raise HTTPException(status_code=403, detail="phone is not on the founder dry-run allowlist")
    if not request.full_loop and not request.send_invite and not get_optin(phone):
        raise HTTPException(
            status_code=400,
            detail="that phone is not opted in — text YES + team first, or pass full_loop=true",
        )

    parsed_date = None
    if request.scoreboard_date:
        try:
            parsed_date = datetime.strptime(request.scoreboard_date, "%Y%m%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="scoreboard_date must be YYYYMMDD")

    result = process_morning_asks(
        dry_run_phone=phone,
        fake_game=request.fake_game,
        scoreboard_date=parsed_date,
        send_invite=request.send_invite or request.full_loop,
        full_loop=request.full_loop,
        teams=request.teams,
    )
    return JSONResponse(content={
        "ok": True,
        "result": result,
        "optins": [
            {"team": o["team_abbr"], "short": o["team_short"]}
            for o in list_optins(phone)
        ],
    })


@router.get("/admin/founder-survey/recipients-preview")
async def founder_survey_preview(admin: str = Depends(verify_admin)):
    """Preview who would receive the founder survey (no sends)."""
    conn = None
    try:
        conn = get_db_connection()
        recipients = _founder_survey_recipients(conn.cursor())
        not_yet = [r for r in recipients if not r["already_surveyed"]]
        sendable_now = [r for r in not_yet if r["in_window"]]
        return JSONResponse(content={
            "recipients": recipients,
            "summary": {
                "total": len(recipients),
                "not_yet_surveyed": len(not_yet),
                "already_surveyed": len(recipients) - len(not_yet),
                "sendable_now": len(sendable_now),
                "waiting_for_window": len(not_yet) - len(sendable_now),
            },
        })
    except Exception as e:
        logger.error(f"Error previewing founder survey: {e}")
        raise HTTPException(status_code=500, detail="Error previewing founder survey")
    finally:
        if conn:
            return_db_connection(conn)


def _send_founder_surveys(recipients: list):
    """Background task: send the survey + arm capture for each recipient."""
    import time
    from services.nudge_service import send_founder_survey
    sent = 0
    for r in recipients:
        try:
            if send_founder_survey(r["phone_full"], r.get("name")):
                sent += 1
        except Exception as e:
            logger.error(f"Founder survey send failed for {r['phone_full'][-4:]}: {e}")
        time.sleep(0.1)  # gentle pacing for Twilio
    logger.info(f"Founder survey batch complete: {sent}/{len(recipients)} sent")


@router.post("/admin/founder-survey/send")
async def founder_survey_send(
    background_tasks: BackgroundTasks,
    skip_surveyed: bool = True,
    admin: str = Depends(verify_admin),
):
    """Send the founder survey to active users (skips already-surveyed by default)."""
    conn = None
    try:
        conn = get_db_connection()
        recipients = _founder_survey_recipients(conn.cursor())
        if skip_surveyed:
            recipients = [r for r in recipients if not r["already_surveyed"]]
        # Only text users currently in their 8am-8pm local window; the rest can
        # be reached by sending again later (skip_surveyed avoids double-texts).
        held = [r for r in recipients if not r["in_window"]]
        recipients = [r for r in recipients if r["in_window"]]
        if not recipients:
            msg = "No recipients are within their 8am-8pm local window right now."
            if held:
                msg += f" {len(held)} waiting for daytime — try again later."
            return JSONResponse(content={"queued": 0, "held_out_of_window": len(held), "message": msg})
        background_tasks.add_task(_send_founder_surveys, recipients)
        logger.info(f"Founder survey queued by {admin}: {len(recipients)} recipients ({len(held)} held - outside window)")
        message = f"Sending the survey to {len(recipients)} user(s)..."
        if held:
            message += f" ({len(held)} held until daytime — re-send later to reach them.)"
        return JSONResponse(content={
            "queued": len(recipients),
            "held_out_of_window": len(held),
            "message": message,
        })
    except Exception as e:
        logger.error(f"Error sending founder survey: {e}")
        raise HTTPException(status_code=500, detail="Error sending founder survey")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/founder-survey/responses")
async def founder_survey_responses(admin: str = Depends(verify_admin)):
    """List every founder-survey send and its response (if any)."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            SELECT sn.phone_number, u.first_name, sn.sent_at,
                   sn.user_response, sn.user_responded_at
            FROM smart_nudges sn
            LEFT JOIN users u ON u.phone_number = sn.phone_number
            WHERE sn.nudge_type = 'founder_survey'
            ORDER BY (sn.user_response IS NOT NULL) DESC, sn.sent_at DESC
        ''')
        rows = []
        responded = 0
        for phone, first_name, sent_at, response, responded_at in c.fetchall():
            if response:
                responded += 1
            rows.append({
                "phone": mask_phone_number(phone),
                "name": (safe_decrypt(first_name, "") if first_name else "") or None,
                "sent_at": sent_at.strftime("%Y-%m-%d %H:%M") if sent_at else None,
                "response": response,
                "responded_at": responded_at.strftime("%Y-%m-%d %H:%M") if responded_at else None,
            })
        return JSONResponse(content={
            "responses": rows,
            "summary": {"sent": len(rows), "responded": responded},
        })
    except Exception as e:
        logger.error(f"Error loading founder survey responses: {e}")
        raise HTTPException(status_code=500, detail="Error loading founder survey responses")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/founder-survey", response_class=HTMLResponse)
async def founder_survey_page(admin: str = Depends(verify_admin)):
    """Self-contained admin page to preview, send, and read founder survey answers."""
    return HTMLResponse(content=FOUNDER_SURVEY_HTML)


FOUNDER_SURVEY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Founder Survey · Remyndrs Admin</title>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 880px; margin: 24px auto; padding: 0 16px; color: #1a1a1a; }
  h1 { font-size: 22px; } h2 { font-size: 16px; margin-top: 28px; }
  p.sub { color: #666; font-size: 14px; }
  button { background: #2563eb; color: #fff; border: 0; padding: 9px 14px; border-radius: 6px; font-size: 14px; cursor: pointer; }
  button.secondary { background: #e5e7eb; color: #111; }
  button:disabled { opacity: .5; cursor: default; }
  table { border-collapse: collapse; width: 100%; margin-top: 10px; font-size: 14px; }
  th, td { text-align: left; padding: 7px 9px; border-bottom: 1px solid #eee; vertical-align: top; }
  th { color: #555; font-weight: 600; }
  .pill { font-size: 12px; padding: 2px 8px; border-radius: 999px; }
  .pending { background: #fef3c7; color: #92400e; } .done { background: #dcfce7; color: #166534; }
  .muted { color: #999; } .answer { white-space: pre-wrap; }
  #status { margin-left: 10px; font-size: 14px; color: #166534; }
</style>
</head>
<body>
  <p><a href="/admin/dashboard" style="color:#2563eb;text-decoration:none;font-size:13px">&larr; Back to Dashboard</a></p>
  <h1>Founder Survey</h1>
  <p class="sub">Asks active users what they use Remyndrs for. Each reply is captured verbatim (not processed as a reminder/memory) and shown below. Your own numbers are excluded automatically.</p>

  <h2>1 · Recipients</h2>
  <button class="secondary" onclick="loadPreview()">Preview recipients</button>
  <button id="sendBtn" onclick="sendSurvey()" style="display:none">Send survey</button>
  <span id="status"></span>
  <div id="preview"></div>

  <h2>2 · Responses</h2>
  <button class="secondary" onclick="loadResponses()">Refresh responses</button>
  <div id="responses"></div>

<script>
async function loadPreview() {
  const el = document.getElementById('preview');
  el.innerHTML = 'Loading...';
  const r = await fetch('/admin/founder-survey/recipients-preview');
  const d = await r.json();
  const s = d.summary;
  let html = `<p class="sub">${s.total} active recipient(s) · ${s.not_yet_surveyed} not yet surveyed · ${s.already_surveyed} already surveyed`
           + ` · <strong>${s.sendable_now} sendable now</strong> (8am-8pm local), ${s.waiting_for_window} waiting for daytime</p>`;
  html += '<table><tr><th>Name</th><th>Phone</th><th>Last active</th><th>Local time</th><th>Status</th></tr>';
  for (const u of d.recipients) {
    let status;
    if (u.already_surveyed) status = '<span class="pill done">surveyed</span>';
    else if (u.in_window) status = '<span class="pill pending">new</span>';
    else status = '<span class="pill" style="background:#e5e7eb;color:#555">waiting for daytime</span>';
    html += `<tr><td>${u.name || '<span class=muted>(no name)</span>'}</td><td>${u.phone}</td><td>${u.last_active || ''}</td>`
         + `<td class="muted">${u.local_time || ''}</td><td>${status}</td></tr>`;
  }
  html += '</table>';
  el.innerHTML = html;
  const btn = document.getElementById('sendBtn');
  btn.style.display = s.sendable_now > 0 ? 'inline-block' : 'none';
  btn.textContent = `Send survey to ${s.sendable_now} user(s) in daytime now`;
}
async function sendSurvey() {
  if (!confirm('Send the founder survey now? This texts real users.')) return;
  const btn = document.getElementById('sendBtn'); btn.disabled = true;
  const r = await fetch('/admin/founder-survey/send?skip_surveyed=true', { method: 'POST' });
  const d = await r.json();
  document.getElementById('status').textContent = d.message || 'Done.';
  btn.disabled = false;
  setTimeout(loadResponses, 1500);
}
async function loadResponses() {
  const el = document.getElementById('responses');
  el.innerHTML = 'Loading...';
  const r = await fetch('/admin/founder-survey/responses');
  const d = await r.json();
  let html = `<p class="sub">${d.summary.responded} of ${d.summary.sent} sent have replied</p>`;
  html += '<table><tr><th>Name</th><th>Phone</th><th>Sent</th><th>Answer</th></tr>';
  for (const x of d.responses) {
    const ans = x.response
      ? `<span class="answer">${(x.response||'').replace(/</g,'&lt;')}</span><br><span class="muted">${x.responded_at||''}</span>`
      : '<span class="pill pending">waiting</span>';
    html += `<tr><td>${x.name || '<span class=muted>(no name)</span>'}</td><td>${x.phone}</td><td class="muted">${x.sent_at||''}</td><td>${ans}</td></tr>`;
  }
  html += '</table>';
  el.innerHTML = d.responses.length ? html : '<p class="sub">No surveys sent yet.</p>';
}
loadResponses();
</script>
</body>
</html>"""


# =====================================================
# CONVERSION FUNNEL
# =====================================================

@router.get("/admin/funnel/data")
async def funnel_data(admin: str = Depends(verify_admin)):
    """Current-state conversion funnel counts.

    'Paying' counts REAL Stripe subscriptions only — comped/beta premium is
    reported separately so the funnel doesn't overstate revenue.
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        def scalar(sql):
            c.execute(sql)
            return c.fetchone()[0]

        try:
            signed_up = scalar("""
                SELECT COUNT(*) FROM (
                    SELECT phone_number FROM onboarding_progress
                    UNION SELECT phone_number FROM users
                ) s
            """)
        except Exception:
            signed_up = scalar("SELECT COUNT(*) FROM users")  # fallback if no onboarding_progress

        onboarded = scalar("SELECT COUNT(*) FROM users WHERE onboarding_complete = TRUE")
        activated = scalar("""
            SELECT COUNT(*) FROM users u
            WHERE u.onboarding_complete = TRUE AND (
                EXISTS(SELECT 1 FROM reminders r WHERE r.phone_number = u.phone_number) OR
                EXISTS(SELECT 1 FROM memories m WHERE m.phone_number = u.phone_number) OR
                EXISTS(SELECT 1 FROM lists l WHERE l.phone_number = u.phone_number)
            )
        """)
        engaged_30d = scalar("SELECT COUNT(*) FROM users WHERE onboarding_complete AND last_active_at >= NOW() - INTERVAL '30 days'")
        habit_7d = scalar("SELECT COUNT(*) FROM users WHERE onboarding_complete AND last_active_at >= NOW() - INTERVAL '7 days'")
        paying = scalar("""
            SELECT COUNT(*) FROM users
            WHERE stripe_subscription_id IS NOT NULL AND stripe_subscription_id <> ''
              AND subscription_status = 'active'
        """)
        comped = scalar("""
            SELECT COUNT(*) FROM users
            WHERE premium_status = 'premium'
              AND (stripe_subscription_id IS NULL OR stripe_subscription_id = '')
        """)

        stages = [
            {"label": "Signed up", "count": signed_up, "hint": "started onboarding"},
            {"label": "Onboarded", "count": onboarded, "hint": "completed onboarding"},
            {"label": "Activated", "count": activated, "hint": "created a reminder, list, or memory"},
            {"label": "Engaged (30d)", "count": engaged_30d, "hint": "active in the last 30 days"},
            {"label": "Habit (7d)", "count": habit_7d, "hint": "active in the last 7 days"},
            {"label": "Paying", "count": paying, "hint": "active Stripe subscription (real revenue)"},
        ]
        return JSONResponse(content={"stages": stages, "comped_premium": comped})
    except Exception as e:
        logger.error(f"Error building funnel data: {e}")
        raise HTTPException(status_code=500, detail="Error building funnel data")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/funnel", response_class=HTMLResponse)
async def funnel_page(admin: str = Depends(verify_admin)):
    return HTMLResponse(content=FUNNEL_HTML)


FUNNEL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Conversion Funnel · Remyndrs Admin</title>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 760px; margin: 24px auto; padding: 0 16px; color: #1a1a1a; }
  h1 { font-size: 22px; margin-bottom: 2px; }
  p.sub { color: #666; font-size: 13px; margin-top: 0; }
  button { background: #e5e7eb; color: #111; border: 0; padding: 8px 13px; border-radius: 6px; font-size: 14px; cursor: pointer; }
  #funnel { margin-top: 18px; text-align: center; }
  .stage { margin: 0 auto; color: #fff; border-radius: 8px; padding: 12px 16px; display: flex;
           align-items: center; justify-content: space-between; box-sizing: border-box; transition: width .3s; }
  .stage .label { font-weight: 600; font-size: 15px; }
  .stage .hint { font-weight: 400; font-size: 11px; opacity: .85; display: block; }
  .stage .count { font-size: 22px; font-weight: 700; }
  .conv { font-size: 12px; color: #555; margin: 5px 0; }
  .conv.drop-big { color: #b91c1c; font-weight: 700; }
  .note { margin-top: 22px; padding: 12px 14px; background: #fef3c7; border-radius: 8px; font-size: 13px; color: #92400e; }
  .pct { font-size: 12px; opacity: .9; }
</style>
</head>
<body>
  <p><a href="/admin/dashboard" style="color:#2563eb;text-decoration:none;font-size:13px">&larr; Back to Dashboard</a></p>
  <h1>Conversion Funnel</h1>
  <p class="sub">Current snapshot. "Paying" = real Stripe revenue only. <button onclick="load()">Refresh</button></p>
  <div id="funnel">Loading...</div>
  <div id="note"></div>

<script>
const COLORS = ['#1d4ed8','#2563eb','#3b82f6','#60a5fa','#93c5fd','#16a34a'];
async function load() {
  const r = await fetch('/admin/funnel/data');
  const d = await r.json();
  const stages = d.stages, top = stages[0].count || 1;
  // find biggest drop for highlight
  let maxDropIdx = -1, maxDrop = -1;
  for (let i = 1; i < stages.length; i++) {
    const prev = stages[i-1].count || 0, cur = stages[i].count || 0;
    const drop = prev ? (prev - cur) / prev : 0;
    if (drop > maxDrop) { maxDrop = drop; maxDropIdx = i; }
  }
  let html = '';
  stages.forEach((s, i) => {
    if (i > 0) {
      const prev = stages[i-1].count || 0, cur = s.count || 0;
      const contPct = prev ? Math.round(cur / prev * 100) : 0;
      const dropPct = 100 - contPct;
      const big = (i === maxDropIdx && dropPct > 0) ? ' drop-big' : '';
      html += `<div class="conv${big}">▼ ${contPct}% continue${dropPct>0?` · −${dropPct}% drop`:''}${big?'  ⟵ biggest leak':''}</div>`;
    }
    const widthPct = Math.max((s.count / top) * 100, 16);
    const ofTop = Math.round((s.count / top) * 100);
    html += `<div class="stage" style="width:${widthPct}%;background:${COLORS[i]}">`
          + `<span class="label">${s.label}<span class="hint">${s.hint}</span></span>`
          + `<span><span class="count">${s.count}</span> <span class="pct">${ofTop}%</span></span>`
          + `</div>`;
  });
  document.getElementById('funnel').innerHTML = html;
  const paying = stages[stages.length-1].count;
  document.getElementById('note').innerHTML =
    `<div class="note"><strong>Revenue reality:</strong> ${paying} paying subscriber(s). `
    + `${d.comped_premium} user(s) are on comped/beta premium — engaged, but <strong>not revenue</strong>. `
    + `The funnel deliberately does not count them as conversions.</div>`;
}
load();
</script>
</body>
</html>"""


# =====================================================
# FEEDBACK API ENDPOINTS
# =====================================================

@router.get("/admin/feedback")
async def get_feedback(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: str = Depends(verify_admin)
):
    """Get all feedback entries, sorted by most recent first"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        sd, ed = parse_date_filter(start_date, end_date)

        query = '''
            SELECT id, user_phone, message, created_at, resolved
            FROM feedback
            WHERE 1=1
        '''
        params = []
        if sd:
            query += ' AND created_at >= %s'
            params.append(sd)
        if ed:
            query += ' AND created_at < %s'
            params.append(ed)
        query += ' ORDER BY created_at DESC'
        c.execute(query, params)
        results = c.fetchall()

        feedback_list = []
        for row in results:
            feedback_list.append({
                "id": row[0],
                "user_phone": row[1],
                "message": row[2],
                "created_at": row[3].isoformat() if row[3] else None,
                "resolved": row[4]
            })

        return JSONResponse(content=feedback_list)
    except Exception as e:
        logger.error(f"Error getting feedback: {e}")
        raise HTTPException(status_code=500, detail="Error getting feedback")
    finally:
        if conn:
            return_db_connection(conn)


@router.post("/admin/feedback/{feedback_id}/toggle")
async def toggle_feedback_resolved(feedback_id: int, admin: str = Depends(verify_admin)):
    """Toggle the resolved status of a feedback entry"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get current status
        c.execute('SELECT resolved FROM feedback WHERE id = %s', (feedback_id,))
        result = c.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Feedback not found")

        # Toggle the status
        new_status = not result[0]
        c.execute(
            'UPDATE feedback SET resolved = %s WHERE id = %s',
            (new_status, feedback_id)
        )
        conn.commit()

        return JSONResponse(content={"id": feedback_id, "resolved": new_status})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling feedback status: {e}")
        raise HTTPException(status_code=500, detail="Error updating feedback")
    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# CONTACT MESSAGES API ENDPOINTS
# =====================================================

@router.get("/admin/contact-messages")
async def get_contact_messages_endpoint(
    category: str = None,
    include_resolved: bool = False,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: str = Depends(verify_admin)
):
    """Get contact messages (feedback, bug reports, questions)"""
    from services.support_service import get_contact_messages
    sd, ed = parse_date_filter(start_date, end_date)
    messages = get_contact_messages(category_filter=category, include_resolved=include_resolved, start_date=sd, end_date=ed)
    return JSONResponse(content={"messages": messages})


@router.post("/admin/contact-messages/{message_id}/toggle")
async def toggle_contact_message(message_id: int, admin: str = Depends(verify_admin)):
    """Toggle resolved status of a contact message"""
    from services.support_service import toggle_contact_message_resolved
    success = toggle_contact_message_resolved(message_id)
    if not success:
        raise HTTPException(status_code=404, detail="Contact message not found")
    return JSONResponse(content={"success": True})


@router.post("/admin/contact-messages/{message_id}/reply")
async def reply_to_contact_message_endpoint(message_id: int, request: Request, admin: str = Depends(verify_admin)):
    """Reply to a contact message via SMS"""
    from services.support_service import reply_to_contact_message
    body = await request.json()
    message = body.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")
    result = reply_to_contact_message(message_id, message)
    if not result['success']:
        raise HTTPException(status_code=404, detail=result.get('error', 'Failed to send reply'))
    return JSONResponse(content={"success": True})


# =====================================================
# COST ANALYTICS API ENDPOINT
# =====================================================

@router.get("/admin/costs")
async def get_costs(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: str = Depends(verify_admin)
):
    """Get cost analytics broken down by plan tier and time period"""
    try:
        sd, ed = parse_date_filter(start_date, end_date)
        costs = get_cost_analytics(start_date=sd, end_date=ed)
        return JSONResponse(content=costs)
    except Exception as e:
        logger.error(f"Error getting cost analytics: {e}")
        raise HTTPException(status_code=500, detail="Error getting cost analytics")


@router.get("/admin/debug/users")
async def debug_users(admin: str = Depends(verify_admin)):
    """Debug endpoint to check user onboarding status"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            SELECT phone_number, first_name, onboarding_complete, onboarding_step, created_at
            FROM users
            ORDER BY created_at DESC
            LIMIT 20
        ''')
        users = c.fetchall()
        return_db_connection(conn)

        return JSONResponse(content={
            "users": [
                {
                    "phone": u[0][-4:] if u[0] else "N/A",  # Last 4 digits only
                    "first_name": u[1],
                    "onboarding_complete": u[2],
                    "onboarding_step": u[3],
                    "created_at": str(u[4]) if u[4] else None
                }
                for u in users
            ]
        })
    except Exception as e:
        logger.error(f"Error in debug users: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/admin/debug/inactivity-nudge")
async def debug_inactivity_nudge(admin: str = Depends(verify_admin)):
    """Debug endpoint to diagnose why inactivity nudges aren't sending"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        now_utc = datetime.utcnow()
        inactive_threshold = now_utc - timedelta(days=7)

        # 1. Total onboarded users
        c.execute("SELECT COUNT(*) FROM users WHERE onboarding_complete = TRUE")
        total_onboarded = c.fetchone()[0]

        # 2. Users with last_active_at set
        c.execute("SELECT COUNT(*) FROM users WHERE onboarding_complete = TRUE AND last_active_at IS NOT NULL")
        has_last_active = c.fetchone()[0]

        # 3. Users with NULL last_active_at
        c.execute("SELECT COUNT(*) FROM users WHERE onboarding_complete = TRUE AND last_active_at IS NULL")
        null_last_active = c.fetchone()[0]

        # 4. Users inactive 7+ days
        c.execute("""
            SELECT COUNT(*) FROM users
            WHERE onboarding_complete = TRUE
              AND last_active_at IS NOT NULL
              AND last_active_at < %s
        """, (inactive_threshold,))
        inactive_7d = c.fetchone()[0]

        # 5. Of those, how many are opted out?
        c.execute("""
            SELECT COUNT(*) FROM users
            WHERE onboarding_complete = TRUE
              AND last_active_at IS NOT NULL
              AND last_active_at < %s
              AND opted_out = TRUE
        """, (inactive_threshold,))
        inactive_opted_out = c.fetchone()[0]

        # 6. Of those, how many already got a nudge (within cooldown)?
        cooldown_threshold = now_utc - timedelta(days=7)
        c.execute("""
            SELECT COUNT(*) FROM users
            WHERE onboarding_complete = TRUE
              AND last_active_at IS NOT NULL
              AND last_active_at < %s
              AND (opted_out IS NULL OR opted_out = FALSE)
              AND inactivity_nudge_sent_at IS NOT NULL
              AND inactivity_nudge_sent_at >= %s
        """, (inactive_threshold, cooldown_threshold))
        in_cooldown = c.fetchone()[0]

        # 7. Users who SHOULD qualify right now (match the task query exactly)
        c.execute("""
            SELECT phone_number, first_name, last_active_at, timezone,
                   inactivity_nudge_sent_at, opted_out
            FROM users
            WHERE last_active_at IS NOT NULL
              AND last_active_at < %s
              AND onboarding_complete = TRUE
              AND (inactivity_nudge_sent_at IS NULL OR inactivity_nudge_sent_at < %s)
              AND (opted_out IS NULL OR opted_out = FALSE)
        """, (inactive_threshold, cooldown_threshold))
        qualifying_users = c.fetchall()

        qualifying_details = []
        for u in qualifying_users:
            phone, first_name, last_active, tz, nudge_sent, opted = u
            # Check timezone window
            try:
                user_tz = pytz.timezone(tz or 'America/New_York')
            except Exception:
                user_tz = pytz.timezone('America/New_York')
            local_hour = datetime.now(pytz.utc).astimezone(user_tz).hour

            qualifying_details.append({
                "phone_last4": phone[-4:] if phone else "N/A",
                "first_name": first_name,
                "last_active_at": str(last_active) if last_active else None,
                "days_inactive": (now_utc - last_active).days if last_active else None,
                "timezone": tz,
                "local_hour_now": local_hour,
                "in_9am_window": 9 <= local_hour < 10,
                "nudge_sent_at": str(nudge_sent) if nudge_sent else None,
                "opted_out": opted,
            })

        in_window = sum(1 for d in qualifying_details if d["in_9am_window"])

        return JSONResponse(content={
            "timestamp_utc": str(now_utc),
            "summary": {
                "total_onboarded": total_onboarded,
                "has_last_active_at": has_last_active,
                "null_last_active_at": null_last_active,
                "inactive_7_plus_days": inactive_7d,
                "inactive_and_opted_out": inactive_opted_out,
                "inactive_in_nudge_cooldown": in_cooldown,
                "qualifying_for_nudge": len(qualifying_details),
                "in_9am_window_right_now": in_window,
            },
            "qualifying_users": qualifying_details,
        })
    except Exception as e:
        logger.error(f"Error in debug inactivity nudge: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/debug/day-1-2-nudges")
async def debug_day_1_2_nudges(admin: str = Depends(verify_admin)):
    """Diagnose why Day 1 / Day 2 lifecycle nudges aren't firing for recent signups.

    Returns the state of every signup in the last 21 days alongside the per-user
    verdict from each nudge's gating rules, so we can see which gate is filtering
    users out.
    """
    from config import FREE_TRIAL_DAYS

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        now_utc = datetime.utcnow()

        c.execute("""
            SELECT
                phone_number,
                created_at,
                onboarding_complete,
                trial_end_date,
                timezone,
                premium_status,
                opted_out,
                COALESCE(post_onboarding_interactions, 0),
                COALESCE(day_1_nudge_sent, FALSE),
                COALESCE(day_2_nudge_sent, FALSE),
                COALESCE(day_3_nudge_sent, FALSE)
            FROM users
            WHERE created_at > NOW() - INTERVAL '21 days'
            ORDER BY created_at DESC
        """)
        rows = c.fetchall()

        users_out = []
        for row in rows:
            (phone, created_at, onboarded, trial_end, tz_str, premium_status,
             opted_out, interactions, d1_sent, d2_sent, d3_sent) = row

            try:
                user_tz = pytz.timezone(tz_str or 'America/New_York')
            except pytz.exceptions.UnknownTimeZoneError:
                user_tz = pytz.timezone('America/New_York')
            local_hour_now = datetime.now(pytz.utc).astimezone(user_tz).hour

            days_in_trial = None
            if trial_end:
                signup_date_utc = trial_end - timedelta(days=FREE_TRIAL_DAYS)
                days_in_trial = (now_utc - signup_date_utc).days

            d1_reasons = []
            if not onboarded:
                d1_reasons.append("onboarding_incomplete")
            if opted_out:
                d1_reasons.append("opted_out")
            if trial_end is None or trial_end <= now_utc:
                d1_reasons.append("trial_ended_or_missing")
            if d1_sent:
                d1_reasons.append("already_sent")
            if interactions > 0:
                d1_reasons.append(f"interactions>0 (={interactions})")
            if days_in_trial is not None and not (0 <= days_in_trial <= 1):
                d1_reasons.append(f"days_in_trial={days_in_trial} outside 0-1")
            if not (9 <= local_hour_now < 10):
                d1_reasons.append(f"local_hour={local_hour_now} outside 9-10")

            d2_reasons = []
            if not onboarded:
                d2_reasons.append("onboarding_incomplete")
            if opted_out:
                d2_reasons.append("opted_out")
            if trial_end is None or trial_end <= now_utc:
                d2_reasons.append("trial_ended_or_missing")
            if d2_sent:
                d2_reasons.append("already_sent")
            if interactions >= 3:
                d2_reasons.append(f"interactions>=3 (={interactions})")
            if days_in_trial is not None and not (1 <= days_in_trial <= 2):
                d2_reasons.append(f"days_in_trial={days_in_trial} outside 1-2")
            if not (9 <= local_hour_now < 10):
                d2_reasons.append(f"local_hour={local_hour_now} outside 9-10")

            users_out.append({
                "phone_last4": phone[-4:] if phone else None,
                "created_at_utc": str(created_at) if created_at else None,
                "age_hours": round((now_utc - created_at.replace(tzinfo=None)).total_seconds() / 3600, 1) if created_at else None,
                "onboarding_complete": onboarded,
                "trial_end_date_utc": str(trial_end) if trial_end else None,
                "days_in_trial": days_in_trial,
                "timezone": tz_str,
                "local_hour_now": local_hour_now,
                "premium_status": premium_status,
                "opted_out": opted_out,
                "post_onboarding_interactions": interactions,
                "day_1_nudge_sent": d1_sent,
                "day_2_nudge_sent": d2_sent,
                "day_3_nudge_sent": d3_sent,
                "day_1_blocked_by": d1_reasons or ["eligible_right_now"],
                "day_2_blocked_by": d2_reasons or ["eligible_right_now"],
            })

        return JSONResponse(content={
            "now_utc": str(now_utc),
            "free_trial_days": FREE_TRIAL_DAYS,
            "total_signups_last_21d": len(users_out),
            "users": users_out,
        })

    except Exception as e:
        logger.error(f"Error in debug_day_1_2_nudges: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/debug/unknown-referrals")
async def debug_unknown_referrals(
    start_date: Optional[str] = "2026-03-01",
    end_date: Optional[str] = None,
    admin: str = Depends(verify_admin)
):
    """Show users with unknown referral source and their first message"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        query = '''
            SELECT
                u.phone_number,
                u.first_name,
                u.created_at AS signup_date,
                u.onboarding_complete,
                l.message_in AS first_message,
                l.created_at AS first_message_at
            FROM users u
            LEFT JOIN LATERAL (
                SELECT message_in, created_at
                FROM logs l
                WHERE l.phone_number = u.phone_number
                ORDER BY l.created_at ASC
                LIMIT 1
            ) l ON true
            WHERE u.referral_source IS NULL
        '''
        params = []

        if start_date:
            query += " AND u.created_at >= %s"
            params.append(start_date)
        if end_date:
            query += " AND u.created_at < %s"
            params.append(end_date)

        query += " ORDER BY u.created_at DESC"

        c.execute(query, params)
        rows = c.fetchall()

        users = []
        for r in rows:
            first_name = safe_decrypt(r[1], "") if r[1] else ""
            first_msg = safe_decrypt(r[4], "") if r[4] else ""
            users.append({
                "phone_last4": r[0][-4:] if r[0] else "N/A",
                "first_name": first_name,
                "signup_date": str(r[2]) if r[2] else None,
                "onboarding_complete": r[3],
                "first_message": first_msg,
                "first_message_at": str(r[5]) if r[5] else None,
            })

        return JSONResponse(content={
            "count": len(users),
            "start_date": start_date,
            "end_date": end_date,
            "users": users
        })
    except Exception as e:
        logger.error(f"Error in unknown referrals debug: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.post("/admin/debug/backfill-referrals")
async def backfill_unknown_referrals(admin: str = Depends(verify_admin)):
    """Backfill NULL referral sources to 'sms-organic' for existing users"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            UPDATE users
            SET referral_source = 'sms-organic'
            WHERE referral_source IS NULL
        ''')
        updated = c.rowcount
        conn.commit()
        return JSONResponse(content={
            "updated": updated,
            "message": f"Backfilled {updated} users with 'sms-organic'"
        })
    except Exception as e:
        logger.error(f"Error backfilling referrals: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/debug/fix-timezone")
async def fix_user_timezone(admin: str = Depends(verify_admin)):
    """
    One-time fix: Look up user by last 4 digits (4321), verify uniqueness,
    and correct timezone from America/Denver to America/Phoenix.
    Returns the user's full phone number for use in broadcast messaging.
    """
    from models.user import update_user_timezone, get_user_timezone
    from models.reminder import recalculate_pending_reminders_for_timezone, update_recurring_reminders_timezone

    last4 = "4321"
    new_timezone = "America/Phoenix"
    conn = None

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "SELECT phone_number, first_name, timezone FROM users WHERE phone_number LIKE %s",
            (f"%{last4}",)
        )
        results = c.fetchall()

        if len(results) == 0:
            return JSONResponse(content={"error": f"No users found ending in {last4}"}, status_code=404)

        if len(results) > 1:
            matches = [{"phone_last4": f"...{r[0][-4:]}", "name": r[1], "timezone": r[2]} for r in results]
            return JSONResponse(content={
                "error": f"Multiple users found ending in {last4}",
                "matches": matches
            }, status_code=409)

        phone, name, current_tz = results[0]

        if current_tz == new_timezone:
            return JSONResponse(content={
                "status": "already_fixed",
                "phone": phone,
                "name": name,
                "timezone": current_tz,
                "message": f"Timezone is already {new_timezone}. Use phone number in broadcast to send notification."
            })

        # Fix timezone
        success, old_tz = update_user_timezone(phone, new_timezone)
        if not success:
            return JSONResponse(content={"error": "Failed to update timezone"}, status_code=500)

        # Recalculate reminders
        updated_reminders = recalculate_pending_reminders_for_timezone(phone, new_timezone)
        updated_recurring = update_recurring_reminders_timezone(phone, new_timezone)

        logger.info(f"Admin timezone fix: {phone[-4:]} {old_tz} -> {new_timezone}")

        return JSONResponse(content={
            "status": "fixed",
            "phone": phone,
            "name": name,
            "old_timezone": old_tz,
            "new_timezone": new_timezone,
            "reminders_recalculated": updated_reminders,
            "recurring_updated": updated_recurring,
            "suggested_broadcast_message": (
                "Our system detected an error with the timezone assigned to your "
                "zip code (85003). Your timezone has been corrected to Phoenix (MST), "
                "and any reminders you set will now be sent at the expected time. "
                "This issue has been resolved for all future users."
            )
        })

    except Exception as e:
        logger.error(f"Error in fix-timezone: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/users/pending-onboarding")
async def get_pending_onboarding_users(admin: str = Depends(verify_admin)):
    """Get users who are stuck in the onboarding flow"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            SELECT u.phone_number, u.first_name, u.onboarding_step, u.created_at,
                   u.referral_source,
                   (SELECT MAX(l.created_at) FROM logs l WHERE l.phone_number = u.phone_number) as last_interaction,
                   u.last_nudged_at
            FROM users u
            WHERE u.onboarding_complete = FALSE
              AND (u.opted_out = FALSE OR u.opted_out IS NULL)
            ORDER BY u.created_at DESC
        ''')
        users = c.fetchall()
        return_db_connection(conn)

        step_labels = {
            0: "Welcome (not started)",
            1: "Awaiting first name",
            2: "Awaiting ZIP code",
        }

        return JSONResponse(content={
            "users": [
                {
                    "phone_number": u[0],
                    "phone_last4": u[0][-4:] if u[0] else "N/A",
                    "first_name": u[1] or "—",
                    "onboarding_step": u[2],
                    "step_label": step_labels.get(u[2], f"Step {u[2]}"),
                    "created_at": str(u[3]) if u[3] else None,
                    "referral_source": u[4] or "—",
                    "last_interaction": str(u[5]) if u[5] else "Never",
                    "last_nudged": str(u[6]) if u[6] else None,
                }
                for u in users
            ],
            "total": len(users)
        })
    except Exception as e:
        logger.error(f"Error getting pending onboarding users: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/admin/users/pending-onboarding/nudge")
async def nudge_pending_users(request: NudgeRequest, admin: str = Depends(verify_admin)):
    """Send a nudge message to pending onboarding users"""
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if not request.phone_numbers:
        raise HTTPException(status_code=400, detail="No phone numbers provided")

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Filter out phones that have been nudged 2+ times in the last 24 hours
        c.execute('''
            SELECT phone_number FROM users
            WHERE phone_number = ANY(%s)
              AND last_nudged_at > NOW() - INTERVAL '24 hours'
              AND nudge_count_24h >= 2
        ''', (request.phone_numbers,))
        blocked_phones = {row[0] for row in c.fetchall()}

        eligible_phones = [p for p in request.phone_numbers if p not in blocked_phones]

        if not eligible_phones:
            raise HTTPException(
                status_code=429,
                detail=f"All {len(request.phone_numbers)} user(s) have already been nudged 2 times in the last 24 hours."
            )

        # Log the nudge as a broadcast
        c.execute('''
            INSERT INTO broadcast_logs (sender, message, audience, recipient_count, status, source)
            VALUES (%s, %s, 'pending_onboarding_nudge', %s, 'sending', 'immediate')
            RETURNING id
        ''', (admin, request.message.strip(), len(eligible_phones)))
        nudge_id = c.fetchone()[0]
        conn.commit()

        success_count = 0
        fail_count = 0
        skipped_count = len(blocked_phones)

        for phone in eligible_phones:
            try:
                send_sms(phone, request.message.strip(), message_type="broadcast")
                # Update per-phone nudge tracking
                c.execute('''
                    UPDATE users SET
                        last_nudged_at = NOW(),
                        nudge_count_24h = CASE
                            WHEN last_nudged_at > NOW() - INTERVAL '24 hours' THEN nudge_count_24h + 1
                            ELSE 1
                        END
                    WHERE phone_number = %s
                ''', (phone,))
                conn.commit()
                success_count += 1
                logger.info(f"Nudge sent to pending user {phone[-4:]}")
            except Exception as e:
                logger.error(f"Failed to nudge {phone[-4:]}: {e}")
                fail_count += 1

        # Update the log entry with results
        c.execute('''
            UPDATE broadcast_logs
            SET success_count = %s, fail_count = %s, status = 'completed', completed_at = NOW()
            WHERE id = %s
        ''', (success_count, fail_count, nudge_id))
        conn.commit()

        msg = f"Sent {success_count} nudge(s), {fail_count} failed"
        if skipped_count > 0:
            msg += f", {skipped_count} skipped (already nudged 2x today)"

        return JSONResponse(content={
            "success": True,
            "message": msg,
            "success_count": success_count,
            "fail_count": fail_count,
            "skipped_count": skipped_count
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending nudges: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.delete("/admin/users/incomplete")
async def delete_incomplete_users(admin: str = Depends(verify_admin)):
    """Delete users who haven't completed onboarding"""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # First count how many will be deleted
        c.execute('SELECT COUNT(*) FROM users WHERE onboarding_complete = FALSE')
        count = c.fetchone()[0]

        # Delete incomplete users
        c.execute('DELETE FROM users WHERE onboarding_complete = FALSE')
        conn.commit()
        return_db_connection(conn)

        logger.info(f"Deleted {count} incomplete user(s)")
        return JSONResponse(content={
            "success": True,
            "deleted_count": count,
            "message": f"Deleted {count} incomplete user(s)"
        })
    except Exception as e:
        logger.error(f"Error deleting incomplete users: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/admin/users/{phone_number}")
async def delete_user(phone_number: str, admin: str = Depends(verify_admin)):
    """Delete a specific user and all their data from all tables"""
    from urllib.parse import unquote
    phone_number = unquote(phone_number)

    conn = get_db_connection()
    try:
        c = conn.cursor()

        # Verify user exists
        c.execute('SELECT phone_number FROM users WHERE phone_number = %s', (phone_number,))
        if not c.fetchone():
            return_db_connection(conn)
            raise HTTPException(status_code=404, detail="User not found")

        deleted_counts = {}

        # Delete from child tables first, then parent table last
        tables = [
            ("list_items", "phone_number"),
            ("lists", "phone_number"),
            ("support_messages", "phone_number"),
            ("support_tickets", "phone_number"),
            ("customer_notes", "phone_number"),
            ("reminders", "phone_number"),
            ("recurring_reminders", "phone_number"),
            ("memories", "phone_number"),
            ("logs", "phone_number"),
            ("conversation_analysis", "phone_number"),
            ("api_usage", "phone_number"),
            ("confidence_logs", "phone_number"),
            ("onboarding_progress", "phone_number"),
            ("feedback", "user_phone"),
            ("users", "phone_number"),
        ]

        for table_name, column in tables:
            try:
                # Use identifier quoting for table/column names (hardcoded whitelist above)
                from psycopg2 import sql
                query = sql.SQL('DELETE FROM {} WHERE {} = %s').format(
                    sql.Identifier(table_name), sql.Identifier(column)
                )
                c.execute(query, (phone_number,))
                deleted_counts[table_name] = c.rowcount
            except Exception:
                # Table may not exist in all environments
                deleted_counts[table_name] = 0

        conn.commit()
        return_db_connection(conn)

        masked_phone = f"***-***-{phone_number[-4:]}" if phone_number and len(phone_number) >= 4 else "***"
        logger.info(f"Admin '{admin}' deleted user {masked_phone}: {deleted_counts}")
        return JSONResponse(content={
            "success": True,
            "phone_number": masked_phone,
            "deleted_counts": deleted_counts,
            "message": f"User {masked_phone} and all associated data deleted"
        })
    except HTTPException:
        return_db_connection(conn)
        raise
    except Exception as e:
        conn.rollback()
        return_db_connection(conn)
        logger.error(f"Error deleting user {phone_number}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# =====================================================
# MAINTENANCE MESSAGE SETTINGS
# =====================================================

DEFAULT_MAINTENANCE_MESSAGE = "Remyndrs is undergoing maintenance. The service will be back up soon. You will receive a message when it's back up."


# =====================================================
# STAGING FALLBACK SETTINGS API
# =====================================================

@router.get("/admin/settings/staging-fallback")
async def get_staging_fallback(admin: str = Depends(verify_admin)):
    """Get staging fallback configuration"""
    enabled = get_setting("staging_fallback_enabled", "false") == "true"
    numbers = get_setting("staging_fallback_numbers", "")
    return JSONResponse(content={
        "enabled": enabled,
        "numbers": numbers
    })


@router.post("/admin/settings/staging-fallback")
async def update_staging_fallback(request: Request, admin: str = Depends(verify_admin)):
    """Update staging fallback configuration"""
    try:
        data = await request.json()
        enabled = data.get("enabled", False)
        numbers = data.get("numbers", "").strip()

        set_setting("staging_fallback_enabled", "true" if enabled else "false")
        set_setting("staging_fallback_numbers", numbers)

        logger.info(f"Staging fallback updated: enabled={enabled}, numbers={numbers}")
        return JSONResponse(content={"success": True, "enabled": enabled, "numbers": numbers})
    except Exception as e:
        logger.error(f"Error updating staging fallback: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/admin/settings/maintenance-message")
async def get_maintenance_message(admin: str = Depends(verify_admin)):
    """Get the current maintenance message"""
    message = get_setting("maintenance_message", DEFAULT_MAINTENANCE_MESSAGE)
    return JSONResponse(content={"message": message, "is_default": message == DEFAULT_MAINTENANCE_MESSAGE})


@router.post("/admin/settings/maintenance-message")
async def update_maintenance_message(request: Request, admin: str = Depends(verify_admin)):
    """Update the maintenance message"""
    try:
        data = await request.json()
        message = data.get("message", "").strip()

        if not message:
            # Reset to default
            set_setting("maintenance_message", DEFAULT_MAINTENANCE_MESSAGE)
            return JSONResponse(content={"success": True, "message": DEFAULT_MAINTENANCE_MESSAGE, "reset_to_default": True})

        set_setting("maintenance_message", message)
        return JSONResponse(content={"success": True, "message": message})
    except Exception as e:
        logger.error(f"Error updating maintenance message: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# =====================================================
# CONVERSATION LOGS API ENDPOINTS
# =====================================================

@router.get("/admin/conversations")
async def get_conversations(
    limit: int = 100,
    offset: int = 0,
    phone: Optional[str] = None,
    intent: Optional[str] = None,
    hide_reviewed: bool = True,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: str = Depends(verify_admin)
):
    """Get recent conversation logs"""
    try:
        sd, ed = parse_date_filter(start_date, end_date)
        logs = get_recent_logs(limit=limit, offset=offset, phone_filter=phone, intent_filter=intent, hide_reviewed=hide_reviewed, start_date=sd, end_date=ed)
        return JSONResponse(content=logs)
    except Exception as e:
        logger.error(f"Error getting conversations: {e}")
        raise HTTPException(status_code=500, detail="Error getting conversations")


@router.get("/admin/conversations/flagged")
async def get_flagged(
    include_reviewed: bool = False,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: str = Depends(verify_admin)
):
    """Get AI-flagged conversations"""
    try:
        sd, ed = parse_date_filter(start_date, end_date)
        flagged = get_flagged_conversations(limit=50, include_reviewed=include_reviewed, start_date=sd, end_date=ed)
        return JSONResponse(content=flagged)
    except Exception as e:
        logger.error(f"Error getting flagged conversations: {e}")
        raise HTTPException(status_code=500, detail="Error getting flagged conversations")


@router.post("/admin/conversations/flagged/{analysis_id}/reviewed")
async def mark_reviewed(analysis_id: int, admin: str = Depends(verify_admin)):
    """Mark a flagged conversation as reviewed"""
    try:
        success = mark_analysis_reviewed(analysis_id)
        if success:
            return JSONResponse(content={"success": True})
        else:
            raise HTTPException(status_code=500, detail="Failed to mark as reviewed")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error marking analysis reviewed: {e}")
        raise HTTPException(status_code=500, detail="Error marking as reviewed")


@router.post("/admin/conversations/analyze")
async def trigger_analysis(background_tasks: BackgroundTasks, admin: str = Depends(verify_admin)):
    """Manually trigger conversation analysis"""
    from services.conversation_analyzer import analyze_recent_conversations
    try:
        background_tasks.add_task(analyze_recent_conversations)
        return JSONResponse(content={"success": True, "message": "Analysis started"})
    except Exception as e:
        logger.error(f"Error triggering analysis: {e}")
        raise HTTPException(status_code=500, detail="Error triggering analysis")


class ManualFlagRequest(BaseModel):
    log_id: int
    phone_number: str
    issue_type: str
    notes: str


class MarkGoodRequest(BaseModel):
    log_id: int
    phone_number: str
    notes: Optional[str] = ""


class DismissRequest(BaseModel):
    log_id: int
    phone_number: str


@router.post("/admin/conversations/good")
async def mark_good(request: MarkGoodRequest, admin: str = Depends(verify_admin)):
    """Mark a conversation as good/accurate"""
    try:
        success = mark_conversation_good(
            log_id=request.log_id,
            phone_number=request.phone_number,
            notes=request.notes
        )
        if success:
            return JSONResponse(content={"success": True})
        else:
            raise HTTPException(status_code=500, detail="Failed to mark as good")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error marking conversation as good: {e}")
        raise HTTPException(status_code=500, detail="Error marking as good")


@router.get("/admin/conversations/good")
async def get_good(admin: str = Depends(verify_admin)):
    """Get conversations marked as good"""
    try:
        good = get_good_conversations(limit=50)
        return JSONResponse(content=good)
    except Exception as e:
        logger.error(f"Error getting good conversations: {e}")
        raise HTTPException(status_code=500, detail="Error getting good conversations")


@router.post("/admin/conversations/dismiss")
async def dismiss_conv(request: DismissRequest, admin: str = Depends(verify_admin)):
    """Dismiss a conversation (already fixed, not applicable)"""
    try:
        success = dismiss_conversation(
            log_id=request.log_id,
            phone_number=request.phone_number
        )
        if success:
            return JSONResponse(content={"success": True})
        else:
            raise HTTPException(status_code=500, detail="Failed to dismiss")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error dismissing conversation: {e}")
        raise HTTPException(status_code=500, detail="Error dismissing conversation")


@router.post("/admin/conversations/flag")
async def flag_conversation(request: ManualFlagRequest, admin: str = Depends(verify_admin)):
    """Manually flag a conversation for review"""
    try:
        success = manual_flag_conversation(
            log_id=request.log_id,
            phone_number=request.phone_number,
            issue_type=request.issue_type,
            notes=request.notes
        )
        if success:
            return JSONResponse(content={"success": True})
        else:
            raise HTTPException(status_code=500, detail="Failed to flag conversation")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error flagging conversation: {e}")
        raise HTTPException(status_code=500, detail="Error flagging conversation")


@router.get("/admin/user/reminders")
async def get_user_reminders_admin(phone: str, admin: str = Depends(verify_admin)):
    """Get all reminders for a user by phone number (full or partial ending)"""
    try:
        conn = get_db_connection()
        try:
            c = conn.cursor()
            # Support partial phone lookup (e.g., "3047" matches phones ending in 3047)
            if len(phone) < 10:
                c.execute("""
                    SELECT id, phone_number, reminder_text, reminder_date, sent, claimed_at, created_at
                    FROM reminders
                    WHERE phone_number LIKE %s
                    ORDER BY reminder_date DESC
                    LIMIT 50
                """, (f'%{phone}',))
            else:
                c.execute("""
                    SELECT id, phone_number, reminder_text, reminder_date, sent, claimed_at, created_at
                    FROM reminders
                    WHERE phone_number = %s
                    ORDER BY reminder_date DESC
                    LIMIT 50
                """, (phone,))

            reminders = c.fetchall()

            # Check for duplicates
            c.execute("""
                SELECT reminder_text, COUNT(*) as cnt
                FROM reminders
                WHERE phone_number LIKE %s AND sent = FALSE
                GROUP BY reminder_text
                HAVING COUNT(*) > 1
            """, (f'%{phone}',))
            duplicates = c.fetchall()

            return JSONResponse(content={
                "reminders": [
                    {
                        "id": r[0],
                        "phone": "..." + r[1][-4:] if r[1] else None,
                        "text": r[2],
                        "date": r[3].isoformat() if r[3] else None,
                        "sent": r[4],
                        "claimed_at": r[5].isoformat() if r[5] else None,
                        "created_at": r[6].isoformat() if r[6] else None,
                    }
                    for r in reminders
                ],
                "duplicates": [
                    {"text": d[0], "count": d[1]}
                    for d in duplicates
                ]
            })
        finally:
            return_db_connection(conn)
    except Exception as e:
        logger.error(f"Error getting user reminders: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/admin/reminder/{reminder_id}/mark-sent")
async def mark_reminder_as_sent(reminder_id: int, admin: str = Depends(verify_admin)):
    """Manually mark a reminder as sent (for fixing stuck reminders)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "UPDATE reminders SET sent = TRUE, claimed_at = NULL WHERE id = %s RETURNING id",
            (reminder_id,)
        )
        result = c.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Reminder not found")
        conn.commit()
        logger.info(f"Admin manually marked reminder {reminder_id} as sent")
        return {"success": True, "reminder_id": reminder_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error marking reminder as sent: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.post("/admin/reminders/cleanup-stuck")
async def cleanup_stuck_reminders(admin: str = Depends(verify_admin)):
    """
    Mark all old unsent reminders as sent to prevent duplicate sends.
    This cleans up reminders that are more than 30 minutes past their scheduled time.
    Use this before resuming production after fixing duplicate reminder bugs.
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Find and mark all old stuck reminders (more than 30 min past due)
        c.execute("""
            UPDATE reminders
            SET sent = TRUE, claimed_at = NULL
            WHERE sent = FALSE
              AND reminder_date < NOW() - INTERVAL '30 minutes'
            RETURNING id, phone_number, reminder_text, reminder_date
        """)
        cleaned = c.fetchall()
        conn.commit()

        cleaned_list = [
            {
                "id": r[0],
                "phone": r[1][-4:] if r[1] else "????",
                "text": r[2][:50] if r[2] else "",
                "scheduled": r[3].isoformat() if r[3] else None
            }
            for r in cleaned
        ]

        logger.warning(f"Admin cleaned up {len(cleaned)} stuck reminders: {[r['id'] for r in cleaned_list]}")

        return {
            "success": True,
            "cleaned_count": len(cleaned),
            "cleaned_reminders": cleaned_list
        }
    except Exception as e:
        logger.error(f"Error cleaning stuck reminders: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# MONITORING AGENT API ENDPOINTS
# =====================================================

@router.get("/admin/pipeline/run")
async def run_full_pipeline(
    hours: int = 24,
    use_ai: bool = False,
    admin: str = Depends(verify_admin)
):
    """Run the full monitoring pipeline (Agent 1 + 2 + 3)"""
    try:
        from agents.interaction_monitor import analyze_interactions
        from agents.issue_validator import validate_issues
        from agents.resolution_tracker import calculate_health_metrics

        results = {
            'agent1': None,
            'agent2': None,
            'agent3': None,
        }

        # Agent 1: Interaction Monitor
        monitor_results = analyze_interactions(hours=hours, dry_run=False)
        results['agent1'] = {
            'logs_analyzed': monitor_results['logs_analyzed'],
            'issues_found': len(monitor_results['issues_found']),
        }

        # Agent 2: Issue Validator
        if results['agent1']['issues_found'] > 0:
            validator_results = validate_issues(limit=100, use_ai=use_ai, dry_run=False)
            results['agent2'] = {
                'processed': validator_results['issues_processed'],
                'validated': len(validator_results['validated']),
                'false_positives': len(validator_results['false_positives']),
            }
        else:
            results['agent2'] = {'processed': 0, 'validated': 0, 'false_positives': 0}

        # Agent 3: Health Metrics
        health = calculate_health_metrics(days=7)
        results['agent3'] = {
            'health_score': health['health_score'],
            'health_status': health['health_status'],
        }

        return JSONResponse(content={
            "success": True,
            "results": results
        })
    except Exception as e:
        logger.error(f"Error running full pipeline: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/admin/monitoring/run")
async def run_interaction_monitor(
    hours: int = 24,
    dry_run: bool = False,
    admin: str = Depends(verify_admin)
):
    """Run the interaction monitor agent"""
    try:
        from agents.interaction_monitor import analyze_interactions, generate_report
        results = analyze_interactions(hours=hours, dry_run=dry_run)
        return JSONResponse(content={
            "success": True,
            "run_id": results.get('run_id'),
            "logs_analyzed": results['logs_analyzed'],
            "issues_found": len(results['issues_found']),
            "summary": results['summary'],
            "report": generate_report(results) if not dry_run else None
        })
    except Exception as e:
        logger.error(f"Error running interaction monitor: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/admin/monitoring/issues")
async def get_monitoring_issues(
    limit: int = 50,
    show_all: bool = False,
    admin: str = Depends(verify_admin)
):
    """Get detected monitoring issues (open issues by default, or all if show_all=true)"""
    conn = None
    try:
        # Ensure monitoring tables exist
        from agents.interaction_monitor import init_monitoring_tables
        init_monitoring_tables()

        conn = get_monitoring_connection()
        c = conn.cursor()

        if show_all:
            # Show everything including false positives and resolved
            c.execute('''
                SELECT mi.id, mi.log_id, mi.phone_number, mi.issue_type,
                       mi.severity, mi.details, mi.detected_at, mi.validated,
                       mi.resolution, mi.false_positive,
                       l.message_in, l.message_out
                FROM monitoring_issues mi
                LEFT JOIN logs l ON mi.log_id = l.id
                ORDER BY mi.detected_at DESC
                LIMIT %s
            ''', (limit,))
        else:
            # Show open issues: validated, not false positive, not resolved
            # Must match health card criteria from resolution_tracker.py
            c.execute('''
                SELECT mi.id, mi.log_id, mi.phone_number, mi.issue_type,
                       mi.severity, mi.details, mi.detected_at, mi.validated,
                       mi.resolution, mi.false_positive,
                       l.message_in, l.message_out
                FROM monitoring_issues mi
                LEFT JOIN logs l ON mi.log_id = l.id
                WHERE mi.validated = TRUE
                  AND mi.false_positive = FALSE
                  AND mi.resolved_at IS NULL
                ORDER BY
                    CASE mi.severity
                        WHEN 'critical' THEN 0
                        WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2
                        ELSE 3
                    END,
                    mi.detected_at DESC
                LIMIT %s
            ''', (limit,))

        rows = c.fetchall()
        issues = []
        for r in rows:
            issues.append({
                "id": r[0],
                "log_id": r[1],
                "phone": "..." + r[2][-4:] if r[2] else None,
                "issue_type": r[3],
                "severity": r[4],
                "details": r[5],
                "detected_at": r[6].isoformat() if r[6] else None,
                "validated": r[7],
                "resolution": r[8],
                "false_positive": r[9],
                "message_in": r[10],
                "message_out": r[11]
            })

        return JSONResponse(content={"issues": issues, "count": len(issues)})
    except Exception as e:
        logger.error(f"Error getting monitoring issues: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_monitoring_connection(conn)


@router.post("/admin/monitoring/issues/{issue_id}/validate")
async def validate_monitoring_issue(
    issue_id: int,
    request: Request,
    admin: str = Depends(verify_admin)
):
    """Mark a monitoring issue as validated (true issue or false positive)"""
    conn = None
    try:
        data = await request.json()
        false_positive = data.get("false_positive", False)
        resolution = data.get("resolution", "")

        conn = get_monitoring_connection()
        c = conn.cursor()
        c.execute('''
            UPDATE monitoring_issues
            SET validated = TRUE,
                validated_by = %s,
                validated_at = NOW(),
                false_positive = %s,
                resolution = %s
            WHERE id = %s
            RETURNING id
        ''', (admin, false_positive, resolution, issue_id))

        result = c.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Issue not found")
        conn.commit()

        return JSONResponse(content={"success": True, "issue_id": issue_id})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error validating monitoring issue: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_monitoring_connection(conn)


@router.post("/admin/monitoring/issues/{issue_id}/false-positive")
async def mark_issue_false_positive(
    issue_id: int,
    admin: str = Depends(verify_admin)
):
    """Quick endpoint to mark an issue as false positive"""
    conn = None
    try:
        conn = get_monitoring_connection()
        c = conn.cursor()
        c.execute('''
            UPDATE monitoring_issues
            SET validated = TRUE,
                validated_by = %s,
                validated_at = NOW(),
                false_positive = TRUE,
                resolution = 'Marked as false positive from dashboard'
            WHERE id = %s
            RETURNING id
        ''', (admin, issue_id))

        result = c.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Issue not found")
        conn.commit()

        return JSONResponse(content={"success": True, "issue_id": issue_id})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error marking issue as false positive: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_monitoring_connection(conn)


@router.get("/admin/monitoring/stats")
async def get_monitoring_stats(admin: str = Depends(verify_admin)):
    """Get monitoring statistics"""
    conn = None
    try:
        conn = get_monitoring_connection()
        c = conn.cursor()

        stats = {}

        # Total issues
        c.execute('SELECT COUNT(*) FROM monitoring_issues')
        stats['total_issues'] = c.fetchone()[0]

        # Pending validation
        c.execute('SELECT COUNT(*) FROM monitoring_issues WHERE validated = FALSE')
        stats['pending_validation'] = c.fetchone()[0]

        # By severity
        c.execute('''
            SELECT severity, COUNT(*) FROM monitoring_issues
            WHERE validated = FALSE
            GROUP BY severity
        ''')
        stats['by_severity'] = {row[0]: row[1] for row in c.fetchall()}

        # By type
        c.execute('''
            SELECT issue_type, COUNT(*) FROM monitoring_issues
            WHERE validated = FALSE
            GROUP BY issue_type ORDER BY COUNT(*) DESC
        ''')
        stats['by_type'] = {row[0]: row[1] for row in c.fetchall()}

        # Recent runs
        c.execute('''
            SELECT id, started_at, logs_analyzed, issues_found, status
            FROM monitoring_runs
            ORDER BY started_at DESC
            LIMIT 5
        ''')
        stats['recent_runs'] = [
            {
                "id": r[0],
                "started_at": r[1].isoformat() if r[1] else None,
                "logs_analyzed": r[2],
                "issues_found": r[3],
                "status": r[4]
            }
            for r in c.fetchall()
        ]

        return JSONResponse(content=stats)
    except Exception as e:
        logger.error(f"Error getting monitoring stats: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_monitoring_connection(conn)


# =====================================================
# AGENT 2: ISSUE VALIDATOR API ENDPOINTS
# =====================================================

@router.get("/admin/validator/run")
async def run_issue_validator(
    batch: int = 50,
    use_ai: bool = True,
    dry_run: bool = False,
    admin: str = Depends(verify_admin)
):
    """Run the issue validator agent"""
    try:
        from agents.issue_validator import validate_issues, generate_report
        results = validate_issues(limit=batch, use_ai=use_ai, dry_run=dry_run)
        return JSONResponse(content={
            "success": True,
            "run_id": results.get('run_id'),
            "issues_processed": results['issues_processed'],
            "validated_count": len(results['validated']),
            "false_positive_count": len(results['false_positives']),
            "patterns_found": results['patterns_found'],
            "severity_adjustments": results['severity_adjustments']
        })
    except Exception as e:
        logger.error(f"Error running issue validator: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/admin/validator/patterns")
async def get_issue_patterns(admin: str = Depends(verify_admin)):
    """Get issue pattern analysis"""
    try:
        from agents.issue_validator import analyze_patterns, init_validator_tables
        init_validator_tables()  # Ensure tables exist
        patterns = analyze_patterns()
        return JSONResponse(content=patterns)
    except Exception as e:
        logger.error(f"Error getting issue patterns: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/admin/validator/stats")
async def get_validator_stats(admin: str = Depends(verify_admin)):
    """Get validator statistics"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        stats = {}

        # Validation runs
        c.execute('''
            SELECT COUNT(*), SUM(issues_processed), SUM(validated_count), SUM(false_positive_count)
            FROM validation_runs WHERE status = 'completed'
        ''')
        row = c.fetchone()
        stats['total_runs'] = row[0] or 0
        stats['total_processed'] = row[1] or 0
        stats['total_validated'] = row[2] or 0
        stats['total_false_positives'] = row[3] or 0

        # False positive rate
        if stats['total_processed'] > 0:
            stats['false_positive_rate'] = round(
                stats['total_false_positives'] / stats['total_processed'] * 100, 1
            )
        else:
            stats['false_positive_rate'] = 0

        # Active patterns
        c.execute('''
            SELECT COUNT(*) FROM issue_patterns WHERE status = 'active'
        ''')
        stats['active_patterns'] = c.fetchone()[0]

        # Recent validation runs
        c.execute('''
            SELECT id, started_at, issues_processed, validated_count,
                   false_positive_count, ai_used, status
            FROM validation_runs
            ORDER BY started_at DESC
            LIMIT 5
        ''')
        stats['recent_runs'] = [
            {
                "id": r[0],
                "started_at": r[1].isoformat() if r[1] else None,
                "processed": r[2],
                "validated": r[3],
                "false_positives": r[4],
                "ai_used": r[5],
                "status": r[6]
            }
            for r in c.fetchall()
        ]

        return JSONResponse(content=stats)
    except Exception as e:
        logger.error(f"Error getting validator stats: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# AGENT 3: RESOLUTION TRACKER API ENDPOINTS
# =====================================================

@router.get("/admin/tracker/health")
async def get_system_health(days: int = 7, admin: str = Depends(verify_admin)):
    """Get system health metrics"""
    try:
        from agents.interaction_monitor import init_monitoring_tables
        from agents.issue_validator import init_validator_tables
        from agents.resolution_tracker import calculate_health_metrics, init_tracker_tables
        init_monitoring_tables()  # Base tables (monitoring_issues)
        init_validator_tables()   # Pattern tables (issue_patterns)
        init_tracker_tables()     # Tracker tables (health_snapshots, issue_resolutions, pattern_resolutions)
        metrics = calculate_health_metrics(days=days)
        # Convert Decimal values for JSON serialization
        from decimal import Decimal
        def sanitize(obj):
            if isinstance(obj, dict):
                return {k: sanitize(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [sanitize(i) for i in obj]
            elif isinstance(obj, Decimal):
                return float(obj)
            elif hasattr(obj, 'isoformat'):
                return obj.isoformat()
            return obj
        return JSONResponse(content=sanitize(metrics))
    except Exception as e:
        logger.error(f"Error getting health metrics: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/admin/tracker/open")
async def get_open_issues_tracker(limit: int = 50, admin: str = Depends(verify_admin)):
    """Get open issues needing resolution"""
    try:
        from agents.resolution_tracker import get_open_issues
        issues = get_open_issues(limit=limit)
        # Convert datetime objects
        for issue in issues:
            if issue.get('detected_at'):
                issue['detected_at'] = issue['detected_at'].isoformat()
            if issue.get('validated_at'):
                issue['validated_at'] = issue['validated_at'].isoformat()
        return JSONResponse(content={"issues": issues, "count": len(issues)})
    except Exception as e:
        logger.error(f"Error getting open issues: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/admin/tracker/resolve/{issue_id}")
async def resolve_issue_tracker(
    issue_id: int,
    request: Request,
    admin: str = Depends(verify_admin)
):
    """Resolve an issue"""
    try:
        from agents.resolution_tracker import resolve_issue, RESOLUTION_TYPES

        data = await request.json()
        resolution_type = data.get("resolution_type")
        description = data.get("description", "")
        commit_ref = data.get("commit_ref", "")

        if resolution_type not in RESOLUTION_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid resolution type. Valid types: {list(RESOLUTION_TYPES.keys())}"
            )

        success = resolve_issue(
            issue_id,
            resolution_type,
            description=description,
            commit_ref=commit_ref,
            resolved_by=admin
        )

        if success:
            return JSONResponse(content={"success": True, "issue_id": issue_id})
        else:
            raise HTTPException(status_code=500, detail="Failed to resolve issue")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resolving issue: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/admin/tracker/report")
async def get_weekly_report(admin: str = Depends(verify_admin)):
    """Get weekly health report"""
    try:
        from agents.interaction_monitor import init_monitoring_tables
        from agents.issue_validator import init_validator_tables
        from agents.resolution_tracker import generate_weekly_report, init_tracker_tables
        init_monitoring_tables()
        init_validator_tables()
        init_tracker_tables()
        report = generate_weekly_report()

        # Convert datetime and Decimal objects for JSON
        from decimal import Decimal
        def convert_dates(obj):
            if isinstance(obj, dict):
                return {k: convert_dates(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [convert_dates(i) for i in obj]
            elif isinstance(obj, Decimal):
                return float(obj)
            elif hasattr(obj, 'isoformat'):
                return obj.isoformat()
            return obj

        return JSONResponse(content=convert_dates(report))
    except Exception as e:
        logger.error(f"Error generating report: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/admin/tracker/trends")
async def get_health_trends(days: int = 30, admin: str = Depends(verify_admin)):
    """Get health score trends over time"""
    try:
        from agents.interaction_monitor import init_monitoring_tables
        from agents.issue_validator import init_validator_tables
        from agents.resolution_tracker import get_health_trend, init_tracker_tables
        init_monitoring_tables()
        init_validator_tables()
        init_tracker_tables()
        trend = get_health_trend(days=days)
        # Convert Decimal values for JSON serialization
        from decimal import Decimal
        def sanitize_trend(obj):
            if isinstance(obj, dict):
                return {k: sanitize_trend(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [sanitize_trend(i) for i in obj]
            elif isinstance(obj, Decimal):
                return float(obj)
            elif hasattr(obj, 'isoformat'):
                return obj.isoformat()
            return obj
        return JSONResponse(content=sanitize_trend({"trend": trend, "days": days}))
    except Exception as e:
        logger.error(f"Error getting trends: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/admin/tracker/snapshot")
async def save_daily_snapshot(admin: str = Depends(verify_admin)):
    """Save a daily health snapshot"""
    try:
        from agents.resolution_tracker import calculate_health_metrics, save_health_snapshot
        metrics = calculate_health_metrics(days=1)
        save_health_snapshot(metrics)
        from decimal import Decimal
        score = float(metrics['health_score']) if isinstance(metrics['health_score'], Decimal) else metrics['health_score']
        return JSONResponse(content={
            "success": True,
            "health_score": score,
            "message": "Daily snapshot saved"
        })
    except Exception as e:
        logger.error(f"Error saving snapshot: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/admin/tracker/resolution-types")
async def get_resolution_types(admin: str = Depends(verify_admin)):
    """Get available resolution types"""
    try:
        from agents.resolution_tracker import RESOLUTION_TYPES
        return JSONResponse(content=RESOLUTION_TYPES)
    except Exception as e:
        logger.error(f"Error getting resolution types: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# =====================================================
# AGENT 4: CODE ANALYZER API ENDPOINTS
# =====================================================

@router.get("/admin/analyzer/issue/{issue_id}")
async def get_issue_code_analysis(
    issue_id: int,
    force: bool = False,
    use_ai: bool = True,
    admin: str = Depends(verify_admin)
):
    """Get or generate code analysis for a specific issue"""
    try:
        from agents.code_analyzer import (
            get_existing_analysis, analyze_issue, init_analyzer_tables
        )
        init_analyzer_tables()

        # Check for existing analysis unless force regenerate
        if not force:
            existing = get_existing_analysis(issue_id=issue_id)
            if existing:
                return JSONResponse(content=existing)

        # Generate new analysis
        result = analyze_issue(issue_id, use_ai=use_ai, force=force)

        if result.get('error'):
            raise HTTPException(status_code=404, detail=result['error'])

        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting issue analysis: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/admin/analyzer/pattern/{pattern_id}")
async def get_pattern_code_analysis(
    pattern_id: int,
    force: bool = False,
    use_ai: bool = True,
    admin: str = Depends(verify_admin)
):
    """Get or generate code analysis for a specific pattern"""
    try:
        from agents.code_analyzer import (
            get_existing_analysis, analyze_pattern, init_analyzer_tables
        )
        init_analyzer_tables()

        # Check for existing analysis unless force regenerate
        if not force:
            existing = get_existing_analysis(pattern_id=pattern_id)
            if existing:
                return JSONResponse(content=existing)

        # Generate new analysis
        result = analyze_pattern(pattern_id, use_ai=use_ai, force=force)

        if result.get('error'):
            raise HTTPException(status_code=404, detail=result['error'])

        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting pattern analysis: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/admin/analyzer/run")
async def run_code_analyzer(
    use_ai: bool = True,
    dry_run: bool = False,
    admin: str = Depends(verify_admin)
):
    """Run the code analyzer agent on unanalyzed issues"""
    try:
        from agents.code_analyzer import run_code_analysis, init_analyzer_tables
        init_analyzer_tables()

        results = run_code_analysis(use_ai=use_ai, dry_run=dry_run)

        # Simplify output (remove full prompts)
        output = {
            'success': True,
            'run_id': results.get('run_id'),
            'issues_analyzed': results['issues_analyzed'],
            'analyses_generated': results['analyses_generated'],
            'errors': results.get('errors', [])
        }

        return JSONResponse(content=output)
    except Exception as e:
        logger.error(f"Error running code analyzer: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/admin/analyzer/{analysis_id}/applied")
async def mark_analysis_applied(
    analysis_id: int,
    admin: str = Depends(verify_admin)
):
    """Mark a code analysis fix as applied"""
    try:
        from agents.code_analyzer import mark_analysis_applied, init_analyzer_tables
        init_analyzer_tables()

        success = mark_analysis_applied(analysis_id, applied_by=admin)

        if not success:
            raise HTTPException(status_code=404, detail="Analysis not found")

        return JSONResponse(content={
            "success": True,
            "analysis_id": analysis_id,
            "applied_by": admin
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error marking analysis as applied: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/admin/analyzer/stats")
async def get_analyzer_stats(admin: str = Depends(verify_admin)):
    """Get code analyzer statistics"""
    conn = None
    try:
        # Ensure tables exist
        from agents.code_analyzer import init_analyzer_tables
        init_analyzer_tables()

        conn = get_monitoring_connection()
        c = conn.cursor()

        stats = {}

        # Total analyses
        c.execute('SELECT COUNT(*) FROM code_analysis')
        stats['total_analyses'] = c.fetchone()[0]

        # By status
        c.execute('''
            SELECT status, COUNT(*) FROM code_analysis
            GROUP BY status
        ''')
        stats['by_status'] = {row[0]: row[1] for row in c.fetchall()}

        # Average confidence
        c.execute('SELECT AVG(confidence_score) FROM code_analysis')
        avg = c.fetchone()[0]
        stats['avg_confidence'] = round(avg, 1) if avg else 0

        # Recent runs
        c.execute('''
            SELECT id, started_at, issues_analyzed, analyses_generated, use_ai, status
            FROM code_analysis_runs
            ORDER BY started_at DESC
            LIMIT 5
        ''')
        stats['recent_runs'] = [
            {
                "id": r[0],
                "started_at": r[1].isoformat() if r[1] else None,
                "issues_analyzed": r[2],
                "analyses_generated": r[3],
                "use_ai": r[4],
                "status": r[5]
            }
            for r in c.fetchall()
        ]

        return JSONResponse(content=stats)
    except Exception as e:
        logger.error(f"Error getting analyzer stats: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_monitoring_connection(conn)


# =====================================================
# ALERT SETTINGS (Teams + Email)
# =====================================================

@router.get("/admin/alerts/settings")
async def get_alert_settings(admin: str = Depends(verify_admin)):
    """Get current alert configuration"""
    try:
        from services.alerts_service import (
            get_teams_webhook_url, get_alert_email_recipients,
            get_health_threshold, is_alerts_enabled,
            get_sms_alert_numbers, is_sms_alerts_enabled
        )
        from config import SMTP_ENABLED

        # Mask webhook URL for security (show only domain)
        webhook_url = get_teams_webhook_url()
        webhook_display = None
        if webhook_url:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(webhook_url)
                webhook_display = f"***{parsed.netloc}***"
            except Exception:
                webhook_display = "***configured***"

        # Mask phone numbers for security
        sms_numbers = get_sms_alert_numbers()
        sms_display = [f"...{n[-4:]}" for n in sms_numbers] if sms_numbers else []

        return JSONResponse(content={
            "alerts_enabled": is_alerts_enabled(),
            "teams_configured": bool(webhook_url),
            "teams_webhook_display": webhook_display,
            "email_configured": SMTP_ENABLED,
            "email_recipients": get_alert_email_recipients(),
            "sms_enabled": is_sms_alerts_enabled(),
            "sms_recipients": sms_display,
            "health_threshold": get_health_threshold(),
        })
    except Exception as e:
        logger.error(f"Error getting alert settings: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/admin/alerts/settings")
async def update_alert_settings(request: Request, admin: str = Depends(verify_admin)):
    """Update alert configuration"""
    try:
        data = await request.json()

        # Update settings
        if "alerts_enabled" in data:
            set_setting("alert_enabled", "true" if data["alerts_enabled"] else "false")

        if "teams_webhook_url" in data:
            # Only update if provided (don't clear existing)
            webhook = data["teams_webhook_url"].strip()
            if webhook:
                set_setting("alert_teams_webhook_url", webhook)

        if "email_recipients" in data:
            recipients = data["email_recipients"]
            if isinstance(recipients, list):
                recipients = ",".join(recipients)
            set_setting("alert_email_recipients", recipients.strip())

        if "sms_enabled" in data:
            set_setting("alert_sms_enabled", "true" if data["sms_enabled"] else "false")

        if "sms_numbers" in data:
            numbers = data["sms_numbers"]
            if isinstance(numbers, list):
                numbers = ",".join(numbers)
            set_setting("alert_sms_numbers", numbers.strip())

        if "health_threshold" in data:
            try:
                threshold = int(data["health_threshold"])
                if 0 <= threshold <= 100:
                    set_setting("alert_health_threshold", str(threshold))
            except ValueError:
                pass

        logger.info(f"Alert settings updated by {admin}")
        return JSONResponse(content={"success": True, "message": "Settings updated"})

    except Exception as e:
        logger.error(f"Error updating alert settings: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/admin/alerts/test")
async def send_test_alert(admin: str = Depends(verify_admin)):
    """Send a test alert to verify configuration"""
    try:
        from services.alerts_service import send_test_alert
        results = send_test_alert()
        return JSONResponse(content={
            "success": results["teams"] or results["email"],
            "results": results
        })
    except Exception as e:
        logger.error(f"Error sending test alert: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/admin/alerts/teams-webhook")
async def clear_teams_webhook(admin: str = Depends(verify_admin)):
    """Clear the Teams webhook URL"""
    try:
        set_setting("alert_teams_webhook_url", "")
        logger.info(f"Teams webhook cleared by {admin}")
        return JSONResponse(content={"success": True, "message": "Teams webhook cleared"})
    except Exception as e:
        logger.error(f"Error clearing Teams webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# =====================================================
# RECURRING REMINDERS MANAGEMENT
# =====================================================

@router.get("/admin/recurring")
async def get_all_recurring_reminders(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: str = Depends(verify_admin)
):
    """Get all recurring reminders for admin view"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        sd, ed = parse_date_filter(start_date, end_date)

        query = """
            SELECT id, phone_number, reminder_text, recurrence_type, recurrence_day,
                   reminder_time, timezone, active, created_at, last_generated_date, next_occurrence
            FROM recurring_reminders
            WHERE 1=1
        """
        params = []
        if sd:
            query += ' AND created_at >= %s'
            params.append(sd)
        if ed:
            query += ' AND created_at < %s'
            params.append(ed)
        query += ' ORDER BY created_at DESC LIMIT 200'
        c.execute(query, params)
        rows = c.fetchall()

        recurring_list = []
        for r in rows:
            # Format pattern for display
            recurrence_type = r[3]
            recurrence_day = r[4]
            if recurrence_type == 'daily':
                pattern = "Every day"
            elif recurrence_type == 'weekly':
                days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
                pattern = f"Every {days[recurrence_day]}" if recurrence_day is not None else "Weekly"
            elif recurrence_type == 'weekdays':
                pattern = "Weekdays (Mon-Fri)"
            elif recurrence_type == 'weekends':
                pattern = "Weekends (Sat-Sun)"
            elif recurrence_type == 'monthly':
                suffix = 'th'
                if recurrence_day in [1, 21, 31]:
                    suffix = 'st'
                elif recurrence_day in [2, 22]:
                    suffix = 'nd'
                elif recurrence_day in [3, 23]:
                    suffix = 'rd'
                pattern = f"Monthly on the {recurrence_day}{suffix}" if recurrence_day else "Monthly"
            else:
                pattern = recurrence_type

            recurring_list.append({
                "id": r[0],
                "phone": "..." + r[1][-4:] if r[1] else None,
                "phone_full": r[1],
                "text": r[2],
                "pattern": pattern,
                "recurrence_type": recurrence_type,
                "time": str(r[5]) if r[5] else None,
                "timezone": r[6],
                "active": r[7],
                "created_at": r[8].isoformat() if r[8] else None,
                "last_generated": str(r[9]) if r[9] else None,
                "next_occurrence": r[10].isoformat() if r[10] else None,
            })

        return JSONResponse(content={"recurring": recurring_list, "count": len(recurring_list)})
    except Exception as e:
        logger.error(f"Error getting recurring reminders: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.post("/admin/recurring/{recurring_id}/pause")
async def pause_recurring_admin(recurring_id: int, admin: str = Depends(verify_admin)):
    """Pause a recurring reminder"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "UPDATE recurring_reminders SET active = FALSE WHERE id = %s RETURNING id, reminder_text",
            (recurring_id,)
        )
        result = c.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Recurring reminder not found")
        conn.commit()
        logger.info(f"Admin paused recurring reminder {recurring_id}")
        return {"success": True, "id": result[0], "text": result[1]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error pausing recurring reminder: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.post("/admin/recurring/{recurring_id}/resume")
async def resume_recurring_admin(recurring_id: int, admin: str = Depends(verify_admin)):
    """Resume a paused recurring reminder"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "UPDATE recurring_reminders SET active = TRUE WHERE id = %s RETURNING id, reminder_text",
            (recurring_id,)
        )
        result = c.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Recurring reminder not found")
        conn.commit()
        logger.info(f"Admin resumed recurring reminder {recurring_id}")
        return {"success": True, "id": result[0], "text": result[1]}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resuming recurring reminder: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.delete("/admin/recurring/{recurring_id}")
async def delete_recurring_admin(recurring_id: int, admin: str = Depends(verify_admin)):
    """Delete a recurring reminder and handle related reminders"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # First check if the recurring reminder exists
        c.execute(
            "SELECT id, reminder_text FROM recurring_reminders WHERE id = %s",
            (recurring_id,)
        )
        result = c.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Recurring reminder not found")

        reminder_text = result[1]

        # Delete pending (unsent) reminders linked to this recurring reminder
        c.execute(
            "DELETE FROM reminders WHERE recurring_id = %s AND sent = FALSE",
            (recurring_id,)
        )
        deleted_pending = c.rowcount

        # Set recurring_id to NULL for sent reminders (preserve history)
        c.execute(
            "UPDATE reminders SET recurring_id = NULL WHERE recurring_id = %s",
            (recurring_id,)
        )
        unlinked_sent = c.rowcount

        # Now delete the recurring reminder itself
        c.execute(
            "DELETE FROM recurring_reminders WHERE id = %s",
            (recurring_id,)
        )

        conn.commit()
        logger.info(f"Admin deleted recurring reminder {recurring_id}: {reminder_text} (deleted {deleted_pending} pending, unlinked {unlinked_sent} sent)")
        return {"success": True, "id": recurring_id, "text": reminder_text}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting recurring reminder: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# PUBLIC CHANGELOG / UPDATES PAGE
# =====================================================

class ChangelogEntry(BaseModel):
    title: str
    description: Optional[str] = None
    entry_type: str = "improvement"  # bug_fix, feature, improvement


@router.get("/updates", response_class=HTMLResponse)
async def public_updates_page():
    """Public changelog page - no auth required"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            SELECT id, title, description, entry_type, created_at
            FROM changelog
            WHERE published = TRUE
            ORDER BY created_at DESC
            LIMIT 50
        ''')
        entries = c.fetchall()

        # Build changelog entries HTML
        entries_html = ""
        current_date = None
        for entry_id, title, description, entry_type, created_at in entries:
            entry_date = created_at.strftime('%B %d, %Y') if created_at else ''

            # Add date header if new date
            if entry_date != current_date:
                if current_date is not None:
                    entries_html += "</div>"  # Close previous date group
                entries_html += f'<div class="date-group"><h3 class="date-header">{entry_date}</h3>'
                current_date = entry_date

            # Entry type badge
            type_colors = {
                'bug_fix': '#e74c3c',
                'feature': '#27ae60',
                'improvement': '#3498db'
            }
            type_labels = {
                'bug_fix': 'Bug Fix',
                'feature': 'New Feature',
                'improvement': 'Improvement'
            }
            badge_color = type_colors.get(entry_type, '#95a5a6')
            badge_label = type_labels.get(entry_type, entry_type)

            entries_html += f'''
            <div class="changelog-entry">
                <span class="entry-badge" style="background-color: {badge_color}">{html_escape(badge_label)}</span>
                <span class="entry-title">{html_escape(title)}</span>
                {f'<p class="entry-description">{html_escape(description)}</p>' if description else ''}
            </div>
            '''

        if current_date is not None:
            entries_html += "</div>"  # Close last date group

        if not entries:
            entries_html = '<p class="no-entries">No updates yet. Check back soon!</p>'

        html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Remyndrs Updates</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 700px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f6fa;
            color: #2c3e50;
        }}
        .header {{
            text-align: center;
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 2px solid #3498db;
        }}
        .header h1 {{
            margin: 0 0 10px 0;
            color: #2c3e50;
        }}
        .header p {{
            margin: 0;
            color: #7f8c8d;
        }}
        .date-group {{
            margin-bottom: 25px;
        }}
        .date-header {{
            font-size: 14px;
            color: #7f8c8d;
            margin: 0 0 10px 0;
            text-transform: uppercase;
            letter-spacing: 1px;
        }}
        .changelog-entry {{
            background: white;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 10px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}
        .entry-badge {{
            display: inline-block;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            color: white;
            text-transform: uppercase;
            margin-right: 10px;
        }}
        .entry-title {{
            font-weight: 500;
        }}
        .entry-description {{
            margin: 10px 0 0 0;
            color: #666;
            font-size: 14px;
            line-height: 1.5;
        }}
        .no-entries {{
            text-align: center;
            color: #7f8c8d;
            padding: 40px;
        }}
        .footer {{
            text-align: center;
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid #ddd;
            color: #7f8c8d;
            font-size: 14px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Remyndrs Updates</h1>
        <p>Latest bug fixes, improvements, and new features</p>
    </div>

    {entries_html}

    <div class="footer">
        <p>Text <strong>?</strong> to Remyndrs anytime for help</p>
    </div>
</body>
</html>
        """
        return HTMLResponse(content=html)

    except Exception as e:
        logger.error(f"Error loading updates page: {e}")
        return HTMLResponse(content="<h1>Error loading updates</h1>", status_code=500)
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/changelog")
async def get_changelog_entries(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: str = Depends(verify_admin)
):
    """Get all changelog entries for admin management"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        sd, ed = parse_date_filter(start_date, end_date)
        query = 'SELECT id, title, description, entry_type, created_at, published FROM changelog WHERE 1=1'
        params = []
        if sd:
            query += ' AND created_at >= %s'
            params.append(sd)
        if ed:
            query += ' AND created_at < %s'
            params.append(ed)
        query += ' ORDER BY created_at DESC'
        c.execute(query, params)
        entries = c.fetchall()
        return [
            {
                'id': e[0],
                'title': e[1],
                'description': e[2],
                'entry_type': e[3],
                'created_at': e[4].isoformat() if e[4] else None,
                'published': e[5]
            }
            for e in entries
        ]
    except Exception as e:
        logger.error(f"Error getting changelog: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.post("/admin/changelog")
async def add_changelog_entry(entry: ChangelogEntry, admin: str = Depends(verify_admin)):
    """Add a new changelog entry"""
    conn = None
    try:
        if entry.entry_type not in ['bug_fix', 'feature', 'improvement']:
            raise HTTPException(status_code=400, detail="Invalid entry type")

        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            INSERT INTO changelog (title, description, entry_type)
            VALUES (%s, %s, %s)
            RETURNING id
        ''', (entry.title, entry.description, entry.entry_type))
        entry_id = c.fetchone()[0]
        conn.commit()

        logger.info(f"Changelog entry added by {admin}: {entry.title}")
        return {"id": entry_id, "message": "Entry added successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding changelog entry: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.delete("/admin/changelog/{entry_id}")
async def delete_changelog_entry(entry_id: int, admin: str = Depends(verify_admin)):
    """Delete a changelog entry"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('DELETE FROM changelog WHERE id = %s', (entry_id,))
        conn.commit()

        if c.rowcount == 0:
            raise HTTPException(status_code=404, detail="Entry not found")

        logger.info(f"Changelog entry {entry_id} deleted by {admin}")
        return {"message": "Entry deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting changelog entry: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# SUPPORT TICKET API ENDPOINTS (kept for admin dashboard compatibility)
# Primary ticket management is now in CS Portal (/cs)
# =====================================================

class SupportReplyRequest(BaseModel):
    message: str


@router.get("/admin/support/tickets")
async def get_support_tickets(
    include_closed: bool = False,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: str = Depends(verify_admin)
):
    """Get all support tickets (delegates to support_service)"""
    from services.support_service import get_all_tickets
    sd, ed = parse_date_filter(start_date, end_date)
    tickets = get_all_tickets(include_closed, start_date=sd, end_date=ed)
    return tickets


@router.get("/admin/support/tickets/{ticket_id}/messages")
async def get_ticket_messages(ticket_id: int, admin: str = Depends(verify_admin)):
    """Get all messages for a specific ticket"""
    from services.support_service import get_ticket_messages
    messages = get_ticket_messages(ticket_id)
    return messages


@router.post("/admin/support/tickets/{ticket_id}/reply")
async def reply_to_support_ticket(ticket_id: int, request: SupportReplyRequest, admin: str = Depends(verify_admin)):
    """Send a reply to a support ticket (sends SMS to user)"""
    from services.support_service import reply_to_ticket

    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    result = reply_to_ticket(ticket_id, request.message.strip())

    if result['success']:
        logger.info(f"Support reply sent to ticket #{ticket_id} by {admin}")
        return {"message": "Reply sent successfully"}
    else:
        raise HTTPException(status_code=500, detail=result.get('error', 'Failed to send reply'))


@router.post("/admin/support/tickets/{ticket_id}/close")
async def close_support_ticket(ticket_id: int, admin: str = Depends(verify_admin)):
    """Close a support ticket"""
    from services.support_service import close_ticket

    if close_ticket(ticket_id):
        logger.info(f"Support ticket #{ticket_id} closed by {admin}")
        return {"message": "Ticket closed successfully"}
    else:
        raise HTTPException(status_code=500, detail="Failed to close ticket")


@router.post("/admin/support/tickets/{ticket_id}/reopen")
async def reopen_support_ticket(ticket_id: int, admin: str = Depends(verify_admin)):
    """Reopen a closed support ticket"""
    from services.support_service import reopen_ticket

    if reopen_ticket(ticket_id):
        logger.info(f"Support ticket #{ticket_id} reopened by {admin}")
        return {"message": "Ticket reopened successfully"}
    else:
        raise HTTPException(status_code=500, detail="Failed to reopen ticket")


# =====================================================
# CUSTOMER SERVICE API ENDPOINTS
# =====================================================

@router.get("/admin/cs/search")
async def cs_search_customers(
    q: str = "",
    admin: str = Depends(verify_admin)
):
    """Search customers by phone number or name"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if not q or len(q) < 2:
            return {"customers": [], "message": "Enter at least 2 characters to search"}

        # Search by phone (partial) or name
        search_pattern = f"%{q}%"
        c.execute('''
            SELECT
                phone_number,
                first_name,
                last_name,
                COALESCE(premium_status, 'free') as tier,
                subscription_status,
                created_at,
                last_active_at,
                timezone,
                onboarding_complete
            FROM users
            WHERE phone_number LIKE %s
               OR LOWER(first_name) LIKE LOWER(%s)
               OR LOWER(last_name) LIKE LOWER(%s)
            ORDER BY last_active_at DESC NULLS LAST
            LIMIT 50
        ''', (search_pattern, search_pattern, search_pattern))

        results = c.fetchall()
        customers = []
        for row in results:
            customers.append({
                "phone": row[0],
                "phone_masked": f"***{row[0][-4:]}" if row[0] else None,
                "first_name": row[1],
                "last_name": row[2],
                "tier": row[3],
                "subscription_status": row[4],
                "created_at": str(row[5]) if row[5] else None,
                "last_active_at": str(row[6]) if row[6] else None,
                "timezone": row[7],
                "onboarding_complete": row[8],
            })

        return {"customers": customers, "count": len(customers)}
    except Exception as e:
        logger.error(f"CS search error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/cs/customer/{phone_number}")
async def cs_get_customer(phone_number: str, admin: str = Depends(verify_admin)):
    """Get full customer profile"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get user info
        c.execute('''
            SELECT
                phone_number, first_name, last_name, email, zip_code, timezone,
                onboarding_complete, created_at, premium_status, premium_since,
                subscription_status, stripe_customer_id, stripe_subscription_id,
                last_active_at, total_messages,
                COALESCE(opted_out, FALSE) as opted_out, opted_out_at
            FROM users WHERE phone_number = %s
        ''', (phone_number,))
        user = c.fetchone()

        if not user:
            raise HTTPException(status_code=404, detail="Customer not found")

        # Get counts
        c.execute('SELECT COUNT(*) FROM reminders WHERE phone_number = %s', (phone_number,))
        reminder_count = c.fetchone()[0]

        c.execute('SELECT COUNT(*) FROM reminders WHERE phone_number = %s AND sent = FALSE', (phone_number,))
        pending_reminders = c.fetchone()[0]

        c.execute('SELECT COUNT(*) FROM lists WHERE phone_number = %s', (phone_number,))
        list_count = c.fetchone()[0]

        c.execute('SELECT COUNT(*) FROM memories WHERE phone_number = %s', (phone_number,))
        memory_count = c.fetchone()[0]

        c.execute('SELECT COUNT(*) FROM recurring_reminders WHERE phone_number = %s AND active = TRUE', (phone_number,))
        recurring_count = c.fetchone()[0]

        # Get recent messages
        c.execute('''
            SELECT message_in, message_out, intent, created_at
            FROM logs WHERE phone_number = %s
            ORDER BY created_at DESC LIMIT 20
        ''', (phone_number,))
        recent_messages = []
        for row in c.fetchall():
            recent_messages.append({
                "message_in": row[0],
                "message_out": row[1][:100] + "..." if row[1] and len(row[1]) > 100 else row[1],
                "intent": row[2],
                "timestamp": str(row[3])
            })

        # Get CS notes
        c.execute('''
            SELECT note, created_by, created_at
            FROM customer_notes WHERE phone_number = %s
            ORDER BY created_at DESC
        ''', (phone_number,))
        notes = []
        for row in c.fetchall():
            notes.append({
                "note": row[0],
                "created_by": row[1],
                "created_at": str(row[2])
            })

        return {
            "phone": user[0],
            "phone_masked": f"***{user[0][-4:]}",
            "first_name": user[1],
            "last_name": user[2],
            "email": user[3],
            "zip_code": user[4],
            "timezone": user[5],
            "onboarding_complete": user[6],
            "created_at": str(user[7]) if user[7] else None,
            "tier": user[8] or 'free',
            "premium_since": str(user[9]) if user[9] else None,
            "subscription_status": user[10],
            "stripe_customer_id": user[11],
            "stripe_subscription_id": user[12],
            "last_active_at": str(user[13]) if user[13] else None,
            "total_messages": user[14] or 0,
            "opted_out": user[15],
            "opted_out_at": str(user[16]) if user[16] else None,
            "stats": {
                "reminders": reminder_count,
                "pending_reminders": pending_reminders,
                "lists": list_count,
                "memories": memory_count,
                "recurring_reminders": recurring_count,
            },
            "recent_messages": recent_messages,
            "notes": notes,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"CS get customer error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/cs/customer/{phone_number}/reminders")
async def cs_get_customer_reminders(phone_number: str, admin: str = Depends(verify_admin)):
    """Get customer's reminders"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute('''
            SELECT id, reminder_text, reminder_date, sent, recurring_id, created_at
            FROM reminders WHERE phone_number = %s
            ORDER BY reminder_date DESC LIMIT 50
        ''', (phone_number,))

        reminders = []
        for row in c.fetchall():
            reminders.append({
                "id": row[0],
                "text": row[1],
                "date": str(row[2]),
                "sent": row[3],
                "is_recurring": row[4] is not None,
                "created_at": str(row[5])
            })

        return {"reminders": reminders}
    except Exception as e:
        logger.error(f"CS get reminders error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/cs/customer/{phone_number}/lists")
async def cs_get_customer_lists(phone_number: str, admin: str = Depends(verify_admin)):
    """Get customer's lists and items"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute('''
            SELECT id, list_name, created_at FROM lists
            WHERE phone_number = %s ORDER BY created_at DESC
        ''', (phone_number,))

        lists = []
        for row in c.fetchall():
            list_id = row[0]
            c.execute('''
                SELECT item_text, completed FROM list_items
                WHERE list_id = %s ORDER BY created_at DESC
            ''', (list_id,))
            items = [{"text": i[0], "completed": i[1]} for i in c.fetchall()]

            lists.append({
                "id": list_id,
                "name": row[1],
                "created_at": str(row[2]),
                "items": items
            })

        return {"lists": lists}
    except Exception as e:
        logger.error(f"CS get lists error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/cs/customer/{phone_number}/memories")
async def cs_get_customer_memories(phone_number: str, admin: str = Depends(verify_admin)):
    """Get customer's memories"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute('''
            SELECT id, memory_text, created_at FROM memories
            WHERE phone_number = %s ORDER BY created_at DESC LIMIT 50
        ''', (phone_number,))

        memories = []
        for row in c.fetchall():
            memories.append({
                "id": row[0],
                "text": row[1],
                "created_at": str(row[2])
            })

        return {"memories": memories}
    except Exception as e:
        logger.error(f"CS get memories error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/admin/cs/customer/{phone_number}/nudges")
async def cs_get_customer_nudges(phone_number: str, admin: str = Depends(verify_admin)):
    """Smart-nudge config + recent history for a single user.

    Returns enough info to answer "is this user being nudged correctly?":
      - config: tier, trial state, nudges-enabled flag, configured nudge time
      - expected_cadence: what the schedule rules say should happen
      - daily_counts: last 30 days of actual sends (UTC date)
      - recent: last 50 nudges with type, text, response
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute('''
            SELECT premium_status, trial_end_date,
                   smart_nudges_enabled, smart_nudge_time,
                   daily_summary_enabled, daily_summary_last_sent,
                   timezone, COALESCE(opted_out, FALSE)
            FROM users WHERE phone_number = %s
        ''', (phone_number,))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Customer not found")

        premium_status = row[0] or 'free'
        trial_end_date = row[1]
        nudges_enabled = bool(row[2])
        nudge_time = str(row[3]) if row[3] else None
        daily_summary_enabled = bool(row[4])
        daily_summary_last_sent = row[5]
        timezone_str = row[6]
        opted_out = bool(row[7])

        # Expected cadence per services/nudge_service.py:is_nudge_eligible
        if not nudges_enabled or opted_out:
            expected_cadence = "disabled"
        elif premium_status == 'free':
            expected_cadence = "weekly (Sundays only)"
        else:
            expected_cadence = "daily"

        trial_active = False
        if trial_end_date:
            from datetime import datetime
            now = datetime.utcnow()
            te = trial_end_date if trial_end_date.tzinfo is None else trial_end_date.replace(tzinfo=None)
            trial_active = te > now

        # Per-day counts, last 30 days
        c.execute('''
            SELECT DATE(sent_at AT TIME ZONE 'UTC') AS day, COUNT(*)
            FROM smart_nudges
            WHERE phone_number = %s AND sent_at > NOW() - INTERVAL '30 days'
            GROUP BY day ORDER BY day DESC
        ''', (phone_number,))
        daily_counts = [{"date": str(d), "count": n} for d, n in c.fetchall()]

        # Recent nudges
        c.execute('''
            SELECT id, nudge_type, nudge_text, sent_at,
                   user_response, user_responded_at, action_taken
            FROM smart_nudges
            WHERE phone_number = %s
            ORDER BY sent_at DESC LIMIT 50
        ''', (phone_number,))
        recent = []
        for r in c.fetchall():
            recent.append({
                "id": r[0],
                "type": r[1],
                "text": r[2],
                "sent_at": str(r[3]) if r[3] else None,
                "user_response": r[4],
                "responded_at": str(r[5]) if r[5] else None,
                "action_taken": r[6],
            })

        # Total + duplicate-day check
        c.execute('''
            SELECT COUNT(*), COUNT(DISTINCT DATE(sent_at AT TIME ZONE 'UTC'))
            FROM smart_nudges
            WHERE phone_number = %s AND sent_at > NOW() - INTERVAL '30 days'
        ''', (phone_number,))
        total_30d, distinct_days_30d = c.fetchone()
        days_with_multiple = sum(1 for d in daily_counts if d["count"] > 1)

        # ALL outbound messages (last 30 days) — captures lifecycle, broadcasts,
        # billing, replies, etc. that wouldn't show in smart_nudges. Critical for
        # diagnosing "why is this user getting so many messages?" when smart
        # nudges are OFF but messages are still flowing.
        c.execute('''
            SELECT message_type, COUNT(*) AS total,
                   COUNT(DISTINCT DATE(created_at AT TIME ZONE 'UTC')) AS days,
                   MAX(created_at) AS last_sent
            FROM sms_outbound_log
            WHERE phone_number = %s AND created_at > NOW() - INTERVAL '30 days'
            GROUP BY message_type ORDER BY total DESC
        ''', (phone_number,))
        outbound_by_type = [
            {
                "type": r[0],
                "total": r[1],
                "distinct_days": r[2],
                "last_sent": str(r[3]) if r[3] else None,
            }
            for r in c.fetchall()
        ]

        # Per-day outbound history with message types
        c.execute('''
            SELECT DATE(created_at AT TIME ZONE 'UTC') AS day,
                   COUNT(*) AS total,
                   STRING_AGG(DISTINCT message_type, ', ') AS types
            FROM sms_outbound_log
            WHERE phone_number = %s AND created_at > NOW() - INTERVAL '30 days'
            GROUP BY day ORDER BY day DESC
        ''', (phone_number,))
        outbound_by_day = [
            {"date": str(r[0]), "count": r[1], "types": r[2]}
            for r in c.fetchall()
        ]

        return {
            "config": {
                "tier": premium_status,
                "trial_end_date": str(trial_end_date) if trial_end_date else None,
                "trial_active": trial_active,
                "smart_nudges_enabled": nudges_enabled,
                "smart_nudge_time": nudge_time,
                "daily_summary_enabled": daily_summary_enabled,
                "daily_summary_last_sent": str(daily_summary_last_sent) if daily_summary_last_sent else None,
                "timezone": timezone_str,
                "opted_out": opted_out,
                "expected_cadence": expected_cadence,
            },
            "stats_30d": {
                "total": total_30d or 0,
                "distinct_days": distinct_days_30d or 0,
                "days_with_multiple": days_with_multiple,
            },
            "daily_counts": daily_counts,
            "recent": recent,
            "outbound_by_type": outbound_by_type,
            "outbound_by_day": outbound_by_day,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"CS get nudges error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


class UpdateTierRequest(BaseModel):
    tier: str
    reason: str = ""
    trial_end_date: str = None  # ISO format date string for trial extension


@router.post("/admin/cs/customer/{phone_number}/tier")
async def cs_update_customer_tier(
    phone_number: str,
    request: UpdateTierRequest,
    admin: str = Depends(verify_admin)
):
    """Update customer's subscription tier (manual override)"""
    conn = None
    try:
        if request.tier not in ['free', 'premium', 'family']:
            raise HTTPException(status_code=400, detail="Invalid tier")

        conn = get_db_connection()
        c = conn.cursor()

        # Parse trial end date if provided
        trial_end = None
        if request.trial_end_date:
            from datetime import datetime
            try:
                trial_end = datetime.fromisoformat(request.trial_end_date.replace('Z', '+00:00'))
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid trial_end_date format")

        # Update tier
        if request.tier == 'free':
            c.execute('''
                UPDATE users SET
                    premium_status = %s,
                    subscription_status = 'manual',
                    trial_end_date = NULL
                WHERE phone_number = %s
            ''', (request.tier, phone_number))
        elif trial_end:
            # Setting premium with trial end date (free trial extension)
            c.execute('''
                UPDATE users SET
                    premium_status = %s,
                    premium_since = COALESCE(premium_since, CURRENT_TIMESTAMP),
                    subscription_status = 'trial',
                    trial_end_date = %s
                WHERE phone_number = %s
            ''', (request.tier, trial_end, phone_number))
        else:
            # Regular premium upgrade (no trial end date)
            c.execute('''
                UPDATE users SET
                    premium_status = %s,
                    premium_since = COALESCE(premium_since, CURRENT_TIMESTAMP),
                    subscription_status = 'manual',
                    trial_end_date = NULL
                WHERE phone_number = %s
            ''', (request.tier, phone_number))

        # Build note about the change
        note_text = f"Tier changed to {request.tier}"
        if trial_end:
            note_text += f" (trial until {trial_end.strftime('%Y-%m-%d')})"
        note_text += f". Reason: {request.reason or 'Not specified'}"

        c.execute('''
            INSERT INTO customer_notes (phone_number, note, created_by)
            VALUES (%s, %s, %s)
        ''', (phone_number, note_text, admin))

        conn.commit()
        logger.info(f"CS: {admin} changed {phone_number[-4:]} tier to {request.tier}" + (f" (trial until {trial_end})" if trial_end else ""))

        return {"message": f"Tier updated to {request.tier}"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"CS update tier error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.post("/admin/cs/customer/{phone_number}/clear-opted-out")
async def cs_clear_opted_out(phone_number: str, admin: str = Depends(verify_admin)):
    """Manually clear the opted_out flag for a user"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute('SELECT opted_out FROM users WHERE phone_number = %s', (phone_number,))
        user = c.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="Customer not found")

        c.execute('''
            UPDATE users SET opted_out = FALSE, opted_out_at = NULL
            WHERE phone_number = %s
        ''', (phone_number,))

        c.execute('''
            INSERT INTO customer_notes (phone_number, note, created_by)
            VALUES (%s, %s, %s)
        ''', (phone_number, "Manually cleared opted_out flag", admin))

        conn.commit()
        logger.info(f"CS: {admin} cleared opted_out for ***{phone_number[-4:]}")

        return {"message": "Opted-out flag cleared"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"CS clear opted_out error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


class AddNoteRequest(BaseModel):
    note: str


@router.post("/admin/cs/customer/{phone_number}/notes")
async def cs_add_customer_note(
    phone_number: str,
    request: AddNoteRequest,
    admin: str = Depends(verify_admin)
):
    """Add a note to customer's record"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute('''
            INSERT INTO customer_notes (phone_number, note, created_by)
            VALUES (%s, %s, %s)
        ''', (phone_number, request.note, admin))

        conn.commit()
        logger.info(f"CS: {admin} added note for {phone_number[-4:]}")

        return {"message": "Note added"}
    except Exception as e:
        logger.error(f"CS add note error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.delete("/admin/cs/customer/{phone_number}/reminder/{reminder_id}")
async def cs_delete_reminder(
    phone_number: str,
    reminder_id: int,
    admin: str = Depends(verify_admin)
):
    """Delete a customer's reminder"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute('''
            DELETE FROM reminders WHERE id = %s AND phone_number = %s
        ''', (reminder_id, phone_number))

        if c.rowcount == 0:
            raise HTTPException(status_code=404, detail="Reminder not found")

        conn.commit()
        logger.info(f"CS: {admin} deleted reminder {reminder_id} for {phone_number[-4:]}")

        return {"message": "Reminder deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"CS delete reminder error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# WEBSITE ANALYTICS ENDPOINTS
# =====================================================

@router.get("/admin/analytics/data")
async def get_analytics_data(days: int = 7, admin: str = Depends(verify_admin)):
    """Get GA4 and Search Console analytics data."""
    try:
        from services.analytics_service import get_all_analytics
        data = get_all_analytics(days)
        return JSONResponse(content=data)
    except Exception as e:
        logger.error(f"Error fetching analytics: {e}")
        return JSONResponse(content={"error": str(e)})

@router.get("/admin/analytics/export")
async def export_analytics_data(days: int = 7, admin: str = Depends(verify_admin)):
    """Export all analytics data as JSON for analysis."""
    try:
        from services.analytics_service import get_all_analytics
        data = get_all_analytics(days)
        return JSONResponse(content=data)
    except Exception as e:
        logger.error(f"Error exporting analytics: {e}")
        return JSONResponse(content={"error": str(e)})

@router.post("/admin/analytics/clear-cache")
async def clear_analytics_cache_endpoint(admin: str = Depends(verify_admin)):
    """Clear the analytics data cache."""
    from services.analytics_service import clear_analytics_cache
    clear_analytics_cache()
    return JSONResponse(content={"status": "cache_cleared"})


# =====================================================
# AI ANALYTICS SUMMARY ENDPOINTS
# =====================================================

@router.get("/admin/analytics/ai-summary")
async def get_ai_analytics_summary(admin: str = Depends(verify_admin)):
    """Get the latest AI-generated analytics summary."""
    try:
        from services.analytics_summary_service import get_latest_summary
        summary = get_latest_summary()
        if not summary:
            return JSONResponse(content={"status": "no_summary", "message": "No summaries generated yet. Click 'Generate Now' to create one."})
        return JSONResponse(content=summary)
    except Exception as e:
        logger.error(f"Error fetching AI summary: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.get("/admin/analytics/ai-summaries")
async def get_ai_analytics_history(limit: int = 30, admin: str = Depends(verify_admin)):
    """Get historical AI-generated analytics summaries."""
    try:
        from services.analytics_summary_service import get_summary_history
        summaries = get_summary_history(limit=limit)
        return JSONResponse(content={"summaries": summaries, "count": len(summaries)})
    except Exception as e:
        logger.error(f"Error fetching AI summary history: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.post("/admin/analytics/ai-summary/generate")
async def generate_ai_analytics_summary(admin: str = Depends(verify_admin)):
    """Generate a new AI analytics summary on demand."""
    try:
        from services.analytics_summary_service import generate_analytics_summary
        result = generate_analytics_summary(period_days=7)
        if result.get("error"):
            return JSONResponse(content={"status": "error", "error": result["error"]}, status_code=400)
        return JSONResponse(content={"status": "success", **result})
    except Exception as e:
        logger.error(f"Error generating AI summary: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@router.post("/admin/analytics/ai-query")
async def ai_analytics_query(request: Request, admin: str = Depends(verify_admin)):
    """Answer an ad-hoc question about the analytics data using Claude."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(content={"error": "Invalid JSON body"}, status_code=400)

    question = (body.get("question") or "").strip()
    if not question:
        return JSONResponse(content={"error": "Question is required"}, status_code=400)
    if len(question) > 2000:
        return JSONResponse(content={"error": "Question too long (max 2000 chars)"}, status_code=400)

    model_choice = body.get("model", "haiku")
    effort = body.get("effort", "medium")

    try:
        from services.analytics_summary_service import answer_analytics_question
        result = answer_analytics_question(
            question=question,
            model_choice=model_choice,
            effort=effort,
        )
        if result.get("error"):
            return JSONResponse(content={"status": "error", "error": result["error"]}, status_code=400)
        return JSONResponse(content={"status": "success", **result})
    except Exception as e:
        logger.error(f"Error answering analytics question: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


# =====================================================
# THREADED ANALYTICS CHAT
# =====================================================

@router.get("/admin/analytics/conversations")
async def list_analytics_conversations(
    include_archived: bool = False,
    limit: int = 50,
    admin: str = Depends(verify_admin),
):
    from services.analytics_summary_service import list_conversations
    items = list_conversations(include_archived=include_archived, limit=min(limit, 200))
    return JSONResponse(content={"conversations": items, "count": len(items)})


@router.post("/admin/analytics/conversations")
async def create_analytics_conversation(request: Request, admin: str = Depends(verify_admin)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    title = (body.get("title") or "").strip() or None
    from services.analytics_summary_service import create_conversation
    result = create_conversation(admin_user=admin, title=title)
    if result.get("error"):
        return JSONResponse(content={"error": result["error"]}, status_code=500)
    return JSONResponse(content=result, status_code=201)


@router.get("/admin/analytics/conversations/{conv_id}")
async def get_analytics_conversation(conv_id: int, admin: str = Depends(verify_admin)):
    from services.analytics_summary_service import get_conversation_with_messages
    conv = get_conversation_with_messages(conv_id)
    if not conv:
        return JSONResponse(content={"error": "Not found"}, status_code=404)
    return JSONResponse(content=conv)


@router.patch("/admin/analytics/conversations/{conv_id}")
async def rename_analytics_conversation(
    conv_id: int, request: Request, admin: str = Depends(verify_admin)
):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(content={"error": "Invalid JSON body"}, status_code=400)
    title = (body.get("title") or "").strip()
    if not title:
        return JSONResponse(content={"error": "Title is required"}, status_code=400)
    from services.analytics_summary_service import rename_conversation
    ok = rename_conversation(conv_id, title)
    if not ok:
        return JSONResponse(content={"error": "Not found or rename failed"}, status_code=404)
    return JSONResponse(content={"status": "success", "id": conv_id, "title": title[:200]})


@router.delete("/admin/analytics/conversations/{conv_id}")
async def delete_analytics_conversation(conv_id: int, admin: str = Depends(verify_admin)):
    from services.analytics_summary_service import delete_conversation
    ok = delete_conversation(conv_id)
    if not ok:
        return JSONResponse(content={"error": "Not found"}, status_code=404)
    return JSONResponse(content={"status": "success", "id": conv_id})


@router.post("/admin/analytics/conversations/{conv_id}/messages")
async def post_analytics_message(
    conv_id: int,
    question: str = Form(""),
    model: str = Form("haiku"),
    effort: str = Form("medium"),
    files: list[UploadFile] = File(default=[]),
    admin: str = Depends(verify_admin),
):
    """Multipart endpoint: accepts `question` + optional image `files[]`."""
    image_files = []
    for f in files or []:
        if not f or not getattr(f, "filename", None):
            continue
        data = await f.read()
        image_files.append({
            "filename": f.filename,
            "mime_type": f.content_type or "application/octet-stream",
            "data": data,
        })

    from services.analytics_summary_service import send_message_in_conversation
    result = send_message_in_conversation(
        conv_id=conv_id,
        question=question,
        model_choice=model,
        effort=effort,
        image_files=image_files,
    )
    if result.get("error"):
        status = 404 if result["error"] == "Conversation not found" else 400
        return JSONResponse(content={"error": result["error"]}, status_code=status)
    return JSONResponse(content={"status": "success", **result})


@router.get("/admin/analytics/conversations/{conv_id}/attachments/{att_id}")
async def get_analytics_attachment(conv_id: int, att_id: int, admin: str = Depends(verify_admin)):
    """Serve an image attachment's raw bytes. Validates that the attachment belongs to the given conversation."""
    from services.analytics_summary_service import get_attachment_bytes, get_conversation_with_messages
    data, mime, filename, message_id = get_attachment_bytes(att_id)
    if not data:
        return JSONResponse(content={"error": "Not found"}, status_code=404)
    # Verify the attachment's message belongs to this conversation
    conv = get_conversation_with_messages(conv_id)
    if not conv or not any(m["id"] == message_id for m in conv.get("messages", [])):
        return JSONResponse(content={"error": "Not found"}, status_code=404)
    headers = {"Cache-Control": "private, max-age=3600"}
    if filename:
        headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return Response(content=data, media_type=mime, headers=headers)


# =====================================================
# PRODUCT METRICS ENDPOINT
# =====================================================

# In-memory cache for product metrics (1 hour TTL)
_product_metrics_cache = {'data': None, 'timestamp': 0}

@router.get("/admin/product-metrics")
async def get_product_metrics_endpoint(admin: str = Depends(verify_admin)):
    """Get advanced product metrics (cached for 1 hour)."""
    import time as _time
    now = _time.time()
    if _product_metrics_cache['data'] and now - _product_metrics_cache['timestamp'] < 3600:
        return JSONResponse(content=_product_metrics_cache['data'])

    try:
        from services.metrics_service import get_product_metrics
        data = get_product_metrics()
        if 'error' not in data:
            _product_metrics_cache['data'] = data
            _product_metrics_cache['timestamp'] = now
        return JSONResponse(content=data)
    except Exception as e:
        logger.error(f"Error fetching product metrics: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


# =====================================================
# DASHBOARD UI
# =====================================================

@router.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(admin: str = Depends(verify_admin)):
    """Render HTML admin dashboard"""
    metrics = get_all_metrics()

    # Build referral rows
    referral_rows = ""
    for source, count in metrics.get('referrals', []):
        referral_rows += f"<tr><td>{source}</td><td>{count}</td></tr>"

    # Build daily signups data for simple chart
    signups = metrics.get('daily_signups', [])
    signup_labels = [str(row[0]) for row in signups[:14]]  # Last 14 days
    signup_values = [row[1] for row in signups[:14]]

    # Reverse to show oldest first
    signup_labels.reverse()
    signup_values.reverse()

    # Premium stats
    premium = metrics.get('premium_stats', {})
    reminder_stats = metrics.get('reminder_stats', {})
    engagement = metrics.get('engagement', {})
    new_users = metrics.get('new_users', {})
    lifecycle = metrics.get('lifecycle_messages', {})

    # Build lifecycle message rows
    lifecycle_rows = ""
    for key, data in lifecycle.items():
        label = data.get('label', key)
        count = data.get('count', 0)
        lifecycle_rows += f"<tr><td>{label}</td><td>{count}</td></tr>"

    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Remyndrs Admin Dashboard</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: #f5f5f5;
            padding: 20px;
            color: #333;
        }}
        h1 {{ margin-bottom: 20px; color: #2c3e50; }}
        h2 {{ margin: 20px 0 10px; color: #34495e; font-size: 1.2em; }}

        .cards {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 30px;
        }}
        .card {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .card-title {{
            font-size: 0.9em;
            color: #7f8c8d;
            margin-bottom: 5px;
        }}
        .card-value {{
            font-size: 2em;
            font-weight: bold;
            color: #2c3e50;
        }}
        .card-subtitle {{
            font-size: 0.8em;
            color: #95a5a6;
        }}
        .card.green .card-value {{ color: #27ae60; }}
        .card.blue .card-value {{ color: #3498db; }}
        .card.orange .card-value {{ color: #e67e22; }}
        .card.purple .card-value {{ color: #9b59b6; }}

        table {{
            width: 100%;
            background: white;
            border-collapse: collapse;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }}
        th, td {{
            padding: 12px 15px;
            text-align: left;
            border-bottom: 1px solid #ecf0f1;
        }}
        th {{
            background: #34495e;
            color: white;
            font-weight: 500;
        }}
        tr:hover {{
            background: #f8f9fa;
        }}

        .section {{
            margin-bottom: 30px;
        }}

        .chart {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .bar-chart {{
            display: flex;
            align-items: flex-end;
            height: 150px;
            gap: 8px;
            padding-top: 20px;
        }}
        .bar {{
            flex: 1;
            background: #3498db;
            border-radius: 4px 4px 0 0;
            min-width: 20px;
            position: relative;
        }}
        .bar:hover {{
            background: #2980b9;
        }}
        .bar-label {{
            position: absolute;
            bottom: -20px;
            left: 50%;
            transform: translateX(-50%);
            font-size: 0.7em;
            color: #7f8c8d;
            white-space: nowrap;
        }}
        .bar-value {{
            position: absolute;
            top: -18px;
            left: 50%;
            transform: translateX(-50%);
            font-size: 0.75em;
            color: #2c3e50;
            font-weight: bold;
        }}

        .grid-2 {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
        }}

        .refresh-note {{
            text-align: center;
            color: #95a5a6;
            font-size: 0.9em;
            margin-top: 30px;
        }}

        /* Broadcast Section Styles */
        .broadcast-section {{
            background: white;
            padding: 25px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 30px;
        }}
        .broadcast-section h2 {{
            margin-top: 0;
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 2px solid #3498db;
        }}
        .form-group {{
            margin-bottom: 15px;
        }}
        .form-group label {{
            display: block;
            margin-bottom: 5px;
            font-weight: 500;
            color: #2c3e50;
        }}
        .form-group select, .form-group textarea {{
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
            font-family: inherit;
        }}
        .form-group textarea {{
            min-height: 100px;
            resize: vertical;
        }}
        .preview-box {{
            background: #f8f9fa;
            padding: 15px;
            border-radius: 4px;
            margin-bottom: 15px;
            border-left: 4px solid #3498db;
        }}
        .preview-box .count {{
            font-size: 1.2em;
            font-weight: bold;
            color: #2c3e50;
        }}
        .btn {{
            padding: 12px 24px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 500;
            transition: background 0.2s;
        }}
        .btn-primary {{
            background: #3498db;
            color: white;
        }}
        .btn-primary:hover {{
            background: #2980b9;
        }}
        .btn-primary:disabled {{
            background: #bdc3c7;
            cursor: not-allowed;
        }}
        .btn-danger {{
            background: #e74c3c;
            color: white;
        }}
        .btn-danger:hover {{
            background: #c0392b;
        }}
        .btn-secondary {{
            background: #95a5a6;
            color: white;
        }}
        .btn-secondary:hover {{
            background: #7f8c8d;
        }}

        /* Modal Styles */
        .modal {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.5);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }}
        .modal.active {{
            display: flex;
        }}
        .modal-content {{
            background: white;
            padding: 25px;
            border-radius: 8px;
            max-width: 500px;
            width: 90%;
            max-height: 80vh;
            overflow-y: auto;
        }}
        .modal-content h3 {{
            margin-top: 0;
            color: #e74c3c;
        }}
        .modal-buttons {{
            display: flex;
            gap: 10px;
            margin-top: 20px;
            justify-content: flex-end;
        }}

        /* Status Styles */
        .status-badge {{
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.8em;
            font-weight: 500;
        }}
        .status-pending {{ background: #f39c12; color: white; }}
        .status-sending {{ background: #3498db; color: white; }}
        .status-completed {{ background: #27ae60; color: white; }}
        .status-failed {{ background: #e74c3c; color: white; }}

        .history-table {{
            font-size: 0.9em;
        }}
        .history-table td {{
            vertical-align: middle;
        }}
        .message-preview {{
            max-width: 200px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}

        .progress-info {{
            background: #e8f6ff;
            padding: 15px;
            border-radius: 4px;
            margin-top: 15px;
            display: none;
        }}
        .progress-info.active {{
            display: block;
        }}

        /* Feedback table styles */
        .feedback-table {{
            font-size: 0.9em;
        }}
        .feedback-table td {{
            vertical-align: middle;
        }}
        .feedback-table .unresolved {{
            background: #fff3cd;
            font-weight: 600;
        }}
        .feedback-table .unresolved td {{
            border-left: 3px solid #f39c12;
        }}
        .feedback-table .unresolved td:first-child {{
            border-left: 3px solid #f39c12;
        }}
        .feedback-message {{
            max-width: 400px;
            word-wrap: break-word;
        }}
        .resolve-checkbox {{
            width: 18px;
            height: 18px;
            cursor: pointer;
        }}

        /* Cost Analytics Styles */
        .cost-section {{
            background: white;
            padding: 25px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 30px;
        }}
        .cost-section h2 {{
            margin-top: 0;
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 2px solid #27ae60;
        }}
        .cost-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.9em;
        }}
        .cost-table th, .cost-table td {{
            padding: 10px 12px;
            text-align: right;
            border-bottom: 1px solid #ecf0f1;
        }}
        .cost-table th {{
            background: #34495e;
            color: white;
            font-weight: 500;
        }}
        .cost-table th:first-child,
        .cost-table td:first-child {{
            text-align: left;
        }}
        .cost-table tr:hover {{
            background: #f8f9fa;
        }}
        .cost-table .plan-row {{
            font-weight: 500;
        }}
        .cost-table .total-row {{
            background: #f8f9fa;
            font-weight: 600;
            border-top: 2px solid #34495e;
        }}
        .cost-table .money {{
            color: #27ae60;
        }}
        .cost-table .cost-header {{
            background: #2c3e50;
        }}
        .period-tabs {{
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
        }}
        .period-tab {{
            padding: 8px 16px;
            border: 1px solid #ddd;
            border-radius: 4px;
            cursor: pointer;
            background: white;
            transition: all 0.2s;
        }}
        .period-tab:hover {{
            background: #f8f9fa;
        }}
        .period-tab.active {{
            background: #27ae60;
            color: white;
            border-color: #27ae60;
        }}
        .cleanup-btn {{
            margin-top: 10px;
            padding: 5px 10px;
            font-size: 0.75em;
            background: #e74c3c;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
        }}
        .cleanup-btn:hover {{
            background: #c0392b;
        }}

        /* Conversation Viewer Styles */
        .conversation-section {{
            background: white;
            padding: 25px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 30px;
        }}
        .conversation-section h2 {{
            margin-top: 0;
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 2px solid #9b59b6;
        }}
        .conversation-filters {{
            display: flex;
            gap: 15px;
            margin-bottom: 20px;
            flex-wrap: wrap;
            align-items: center;
        }}
        .conversation-filters input {{
            padding: 8px 12px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
        }}
        .conversation-filters button {{
            padding: 8px 16px;
        }}
        .conversation-table {{
            font-size: 0.85em;
        }}
        .conversation-table th {{
            background: #34495e;
        }}
        .conversation-table td {{
            vertical-align: top;
            max-width: 300px;
        }}
        .msg-in {{
            background: #e8f4fd;
            padding: 8px;
            border-radius: 4px;
            margin-bottom: 5px;
            word-wrap: break-word;
        }}
        .msg-out {{
            background: #f0f0f0;
            padding: 8px;
            border-radius: 4px;
            word-wrap: break-word;
            font-size: 0.9em;
            max-height: 100px;
            overflow-y: auto;
        }}
        .intent-badge {{
            display: inline-block;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 0.75em;
            background: #3498db;
            color: white;
        }}
        .flagged-section {{
            margin-top: 30px;
            padding-top: 20px;
            border-top: 2px solid #e74c3c;
        }}
        .severity-high {{
            background: #e74c3c;
            color: white;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.8em;
        }}
        .severity-medium {{
            background: #f39c12;
            color: white;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.8em;
        }}
        .severity-low {{
            background: #95a5a6;
            color: white;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.8em;
        }}
        .ai-explanation {{
            background: #fff3cd;
            padding: 10px;
            border-radius: 4px;
            margin-top: 5px;
            font-size: 0.9em;
            border-left: 3px solid #f39c12;
        }}
        .tabs {{
            display: flex;
            gap: 10px;
            margin-bottom: 15px;
        }}
        .tab {{
            padding: 8px 16px;
            border: 1px solid #ddd;
            border-radius: 4px;
            cursor: pointer;
            background: white;
        }}
        .tab:hover {{
            background: #f8f9fa;
        }}
        .tab.active {{
            background: #9b59b6;
            color: white;
            border-color: #9b59b6;
        }}
        .pagination {{
            display: flex;
            gap: 10px;
            justify-content: center;
            margin-top: 15px;
        }}

        /* Navigation Menu */
        .nav-menu {{
            position: sticky;
            top: 0;
            background: white;
            padding: 12px 20px;
            margin: -20px -20px 20px -20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            z-index: 100;
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            align-items: center;
        }}
        .nav-menu a {{
            padding: 8px 16px;
            background: #f8f9fa;
            border-radius: 4px;
            text-decoration: none;
            color: #2c3e50;
            font-size: 0.9em;
            font-weight: 500;
            transition: all 0.2s;
            border: 1px solid #e0e0e0;
        }}
        .nav-menu a:hover {{
            background: #3498db;
            color: white;
            border-color: #3498db;
        }}
        .nav-menu .nav-title {{
            font-weight: bold;
            color: #2c3e50;
            margin-right: 10px;
        }}
        .section-anchor {{
            scroll-margin-top: 120px;
        }}

        /* Date Filter Bar */
        .date-filter-bar {{
            position: sticky;
            top: 52px;
            background: #eef2f7;
            padding: 10px 20px;
            margin: -0px -20px 20px -20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.08);
            z-index: 99;
            display: flex;
            gap: 12px;
            align-items: center;
            flex-wrap: wrap;
            border-bottom: 1px solid #d5dce6;
        }}
        .filter-btn {{
            padding: 6px 18px;
            border: 2px solid #3498db;
            border-radius: 20px;
            cursor: pointer;
            font-size: 0.9em;
            font-weight: 600;
            background: white;
            color: #3498db;
            transition: all 0.2s;
        }}
        .filter-btn:hover {{
            background: #ebf5fb;
        }}
        .filter-btn.active {{
            background: #3498db;
            color: white;
        }}
        .filter-date-input {{
            padding: 6px 12px;
            border: 1px solid #bdc3c7;
            border-radius: 4px;
            font-size: 0.9em;
        }}
        .filter-label {{
            font-size: 0.85em;
            color: #555;
            font-weight: 500;
        }}

        /* Collapsible Sections */
        .collapsible-section {{
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 20px;
            overflow: hidden;
        }}
        .section-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 15px 20px;
            background: #f8f9fa;
            cursor: pointer;
            user-select: none;
            border-bottom: 1px solid #eee;
        }}
        .section-header:hover {{
            background: #ecf0f1;
        }}
        .section-header h2 {{
            margin: 0;
            font-size: 1.3em;
            color: #2c3e50;
        }}
        .section-toggle {{
            font-size: 1.2em;
            color: #7f8c8d;
            transition: transform 0.3s ease;
        }}
        .section-header.collapsed .section-toggle {{
            transform: rotate(-90deg);
        }}
        .section-content {{
            padding: 20px;
            transition: max-height 0.3s ease-out, padding 0.3s ease-out;
            overflow: hidden;
        }}
        .section-content.collapsed {{
            max-height: 0;
            padding: 0 20px;
        }}
    </style>
</head>
<body>
    <div class="nav-menu">
        <span class="nav-title">Remyndrs Dashboard</span>
        <button onclick="showRecentMessages()" style="padding: 8px 16px; background: #9b59b6; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 0.9em; font-weight: 500;">Recent Messages</button>
        <a href="/admin/monitoring" style="padding: 8px 16px; background: #27ae60; color: white; border: none; border-radius: 4px; text-decoration: none; font-size: 0.9em; font-weight: 500;">Monitoring</a>
        <a href="/admin/investment-health" style="padding: 8px 16px; background: #2c3e50; color: white; border: none; border-radius: 4px; text-decoration: none; font-size: 0.9em; font-weight: 500;">Investment Health</a>
        <a href="/admin/funnel" style="padding: 8px 16px; background: #2563eb; color: white; border: none; border-radius: 4px; text-decoration: none; font-size: 0.9em; font-weight: 500;">Funnel</a>
        <a href="/admin/founder-survey" style="padding: 8px 16px; background: #e67e22; color: white; border: none; border-radius: 4px; text-decoration: none; font-size: 0.9em; font-weight: 500;">Founder Survey</a>
        <a href="#overview">Overview</a>
        <a href="#broadcast">Broadcast</a>
        <a href="#support">Support Tickets</a>
        <a href="#contact-messages">Contact Messages</a>
        <a href="#feedback">Feedback</a>
        <a href="#costs">Costs</a>
        <a href="#changelog">Changelog</a>
        <a href="#conversations">Conversations</a>
        <a href="#recurring">Recurring</a>
        <a href="#customer-service">Customer Service</a>
        <a href="#product-metrics" style="background: #4A90A4; color: white; border-color: #4A90A4;">Product Metrics</a>
        <a href="#website-analytics">Analytics</a>
        <a href="#ai-analytics-summary">AI Summary</a>
        <a href="#settings">Settings</a>
    </div>

    <!-- Date Filter Bar -->
    <div class="date-filter-bar">
        <span class="filter-label">View:</span>
        <button class="filter-btn" id="filterBetaBtn" onclick="setDateFilter('beta')">Beta</button>
        <button class="filter-btn active" id="filterLiveBtn" onclick="setDateFilter('live')">Live</button>
        <span id="filterDateGroup" style="display: flex; gap: 8px; align-items: center;">
            <span class="filter-label">From:</span>
            <input type="date" id="filterStartDate" class="filter-date-input" value="2026-03-01" min="2026-03-01" onchange="onDateFilterChange()">
        </span>
        <span id="filterRangeLabel" class="filter-label" style="margin-left: 8px;"></span>
    </div>

    <h2 id="overview" class="section-anchor" style="margin-top: 0;">Overview</h2>

    <div id="overviewSection">
        <div class="cards">
            <div class="card">
                <div class="card-title">All Users</div>
                <div class="card-value" id="overviewAllUsers">{metrics.get('total_users_all_time', 0)}</div>
                <div class="card-subtitle">all-time signups</div>
            </div>
            <div class="card">
                <div class="card-title" id="overviewFilteredUsersTitle">New Signups</div>
                <div class="card-value" id="overviewTotalUsers">{metrics.get('total_users', 0)}</div>
                <div class="card-subtitle" id="overviewFilteredUsersSub">since launch</div>
            </div>
            <div class="card orange">
                <div class="card-title">Pending Onboarding</div>
                <div class="card-value" id="overviewPendingOnboarding">{metrics.get('pending_onboarding', 0)}</div>
                <div class="card-subtitle">started but not finished</div>
                <button class="cleanup-btn" style="background: #2980b9;" onclick="viewPendingOnboarding()">View</button>
                <button class="cleanup-btn" onclick="cleanupIncomplete()">Clean Up</button>
            </div>
            <div class="card green">
                <div class="card-title">Active (7 days)</div>
                <div class="card-value" id="overviewActive7dAll">{metrics.get('active_7d_all', 0)}</div>
                <div class="card-subtitle">all users · filtered: <span id="overviewActive7d">{metrics.get('active_7d', 0)}</span></div>
            </div>
            <div class="card blue">
                <div class="card-title">Active (30 days)</div>
                <div class="card-value" id="overviewActive30dAll">{metrics.get('active_30d_all', 0)}</div>
                <div class="card-subtitle">all users · filtered: <span id="overviewActive30d">{metrics.get('active_30d', 0)}</span></div>
            </div>
            <div class="card purple">
                <div class="card-title">Premium Users</div>
                <div class="card-value" id="overviewPremiumCount">{premium.get('premium', 0)}</div>
                <div class="card-subtitle">free: <span id="overviewFreeCount">{premium.get('free', 0)}</span></div>
            </div>
        </div>

        <h2>New User Signups</h2>
        <div class="cards">
            <div class="card green">
                <div class="card-title">Today</div>
                <div class="card-value" id="overviewNewToday">{new_users.get('today', 0)}</div>
                <div class="card-subtitle">new users</div>
            </div>
            <div class="card blue">
                <div class="card-title">This Week</div>
                <div class="card-value" id="overviewNewWeek">{new_users.get('this_week', 0)}</div>
                <div class="card-subtitle">last 7 days</div>
            </div>
            <div class="card orange">
                <div class="card-title">This Month</div>
                <div class="card-value" id="overviewNewMonth">{new_users.get('this_month', 0)}</div>
                <div class="card-subtitle">last 30 days</div>
            </div>
        </div>

        <div class="grid-2">
            <div class="section">
                <h2>Engagement Stats</h2>
                <table>
                    <tr><th>Metric</th><th>Value</th></tr>
                    <tbody id="engagementTableBody">
                    <tr><td>Avg Messages / User</td><td>{engagement.get('avg_messages_per_user', 0)}</td></tr>
                    <tr><td>Avg Memories / User</td><td>{engagement.get('avg_memories_per_user', 0)}</td></tr>
                    <tr><td>Avg Reminders / User</td><td>{engagement.get('avg_reminders_per_user', 0)}</td></tr>
                    <tr><td>Avg Lists / User</td><td>{engagement.get('avg_lists_per_user', 0)}</td></tr>
                    <tr><td>Avg Items / List</td><td>{engagement.get('avg_items_per_list', 0)}</td></tr>
                    <tr><td>Total Messages</td><td>{engagement.get('total_messages', 0)}</td></tr>
                    <tr><td>Total Memories</td><td>{engagement.get('total_memories', 0)}</td></tr>
                    <tr><td>Total Reminders</td><td>{engagement.get('total_reminders', 0)}</td></tr>
                    <tr><td>Total Lists</td><td>{engagement.get('total_lists', 0)}</td></tr>
                    </tbody>
                </table>
            </div>

            <div class="section">
                <h2>Reminder Delivery</h2>
                <table>
                    <tr><th>Status</th><th>Count</th></tr>
                    <tbody id="reminderTableBody">
                    <tr><td>Pending</td><td>{reminder_stats.get('pending', 0)}</td></tr>
                    <tr><td>Sent</td><td>{reminder_stats.get('sent', 0)}</td></tr>
                    <tr><td>Failed</td><td>{reminder_stats.get('failed', 0)}</td></tr>
                    <tr><td><strong>Completion Rate</strong></td><td><strong>{reminder_stats.get('completion_rate', 0)}%</strong></td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div class="section">
            <h2>Daily Signups (Last 14 Days)</h2>
            <div class="chart" id="signupChartContainer">
                <div class="bar-chart">
                    {"".join([
                        f'<div class="bar" style="height: {max(10, (v / max(signup_values) * 100) if signup_values and max(signup_values) > 0 else 10)}%"><span class="bar-value">{v}</span><span class="bar-label">{signup_labels[i][-5:]}</span></div>'
                        for i, v in enumerate(signup_values)
                    ]) if signup_values else '<div style="color: #95a5a6; padding: 40px;">No signup data yet</div>'}
                </div>
            </div>
        </div>

        <div class="section">
            <h2>Referral Sources</h2>
            <table>
                <tr><th>Source</th><th>Users</th></tr>
                <tbody id="referralTableBody">
                {referral_rows if referral_rows else '<tr><td colspan="2" style="color: #95a5a6;">No referral data yet</td></tr>'}
                </tbody>
            </table>
        </div>

        <div class="section">
            <h2>Lifecycle Messages Sent</h2>
            <table>
                <tr><th>Message Type</th><th>Users Reached</th></tr>
                <tbody id="lifecycleTableBody">
                {lifecycle_rows if lifecycle_rows else '<tr><td colspan="2" style="color: #95a5a6;">No lifecycle data yet</td></tr>'}
                </tbody>
            </table>
        </div>
    </div>

    <!-- Maintenance Message Section (Staging Only) -->
    <div class="section" id="maintenanceSection">
        <h2>🔧 Staging Maintenance Message</h2>
        <p style="color: #7f8c8d; margin-bottom: 15px;">This message is shown to non-test users when they text the staging number.</p>

        <div class="form-group">
            <label for="maintenanceMessage">Maintenance Message</label>
            <textarea id="maintenanceMessage" style="width: 100%; min-height: 80px; padding: 10px; border: 1px solid #ddd; border-radius: 4px;" placeholder="Enter maintenance message..."></textarea>
        </div>

        <div style="display: flex; gap: 10px;">
            <button class="btn btn-primary" onclick="saveMaintenanceMessage()">Save Message</button>
            <button class="btn" style="background: #95a5a6;" onclick="resetMaintenanceMessage()">Reset to Default</button>
        </div>
        <div id="maintenanceStatus" style="margin-top: 10px; color: #27ae60;"></div>
    </div>

    <!-- Broadcast Section -->
    <div id="broadcast" class="broadcast-section section-anchor">
        <h2>📢 Broadcast Message</h2>

        <div class="form-group">
            <label for="audience">Select Audience</label>
            <select id="audience" onchange="updatePreview()">
                <option value="all">All Users</option>
                <option value="free">Free Tier Only</option>
                <option value="premium">Premium Only</option>
                <option value="single">Single Number (Test)</option>
            </select>
        </div>

        <div class="form-group" id="singlePhoneGroup" style="display: none;">
            <label for="singlePhone">Phone Number</label>
            <input type="tel" id="singlePhone" placeholder="+1 (555) 123-4567" oninput="updatePreview()" style="width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 1em;">
            <small style="color: #7f8c8d;">Enter a US phone number in any format</small>
        </div>

        <div class="form-group">
            <label for="message">Message Content</label>
            <textarea id="message" placeholder="Type your broadcast message here..." oninput="updatePreview()"></textarea>
            <small style="color: #7f8c8d;">Character count: <span id="charCount">0</span>/160 (SMS segment)</small>
        </div>

        <div class="form-group" style="margin-top: 15px;">
            <label style="display: flex; align-items: center; cursor: pointer;">
                <input type="checkbox" id="scheduleCheckbox" onchange="toggleScheduleMode()" style="margin-right: 8px; width: 18px; height: 18px;">
                <span>Schedule for later</span>
            </label>
        </div>

        <div class="form-group" id="scheduleDateGroup" style="display: none;">
            <label for="scheduleDate">Scheduled Date & Time (your local time)</label>
            <input type="datetime-local" id="scheduleDate" onchange="validateScheduleDate()" style="width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 1em;">
            <small id="scheduleDateError" style="color: #e74c3c; display: none;"></small>
            <small id="scheduleDateHint" style="color: #7f8c8d;">The broadcast will be sent at this time to users within their 8am-8pm window</small>
        </div>

        <div class="preview-box">
            <div><strong>Preview (what users will receive):</strong></div>
            <div style="margin: 10px 0; padding: 10px; background: white; border-radius: 4px; white-space: pre-wrap;">
                <span style="color: #7f8c8d;">[Remyndrs System Message] </span><span id="messagePreview" style="color: #7f8c8d; font-style: italic;">Your message will appear here...</span>
            </div>
            <div>
                <span style="color: #27ae60; font-weight: bold;"><span id="recipientCount" class="count">0</span></span> users within 8am-8pm window
                <span id="outsideWindowInfo" style="color: #95a5a6; margin-left: 10px;"></span>
            </div>
            <div style="margin-top: 8px; font-size: 0.85em; color: #7f8c8d;">
                <em>Broadcasts only send to users between 8:00 AM and 8:00 PM in their local timezone</em>
            </div>
            <div id="previewBtnContainer" style="margin-top: 10px;">
                <button type="button" onclick="loadRecipientsPreview()" id="previewRecipientsBtn" class="btn" style="background: #8e44ad; color: white; padding: 6px 14px; font-size: 0.85em;">
                    Preview Recipients
                </button>
                <span id="previewLoading" style="display: none; color: #7f8c8d; margin-left: 8px; font-size: 0.85em;">Loading...</span>
            </div>
        </div>

        <div id="recipientsPreviewPanel" style="display: none; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 6px; overflow: hidden;">
            <div style="background: #f8f9fa; padding: 12px 15px; border-bottom: 1px solid #ddd;">
                <strong id="previewSummaryLine"></strong>
            </div>
            <div style="padding: 15px;">
                <div id="includedSection">
                    <h4 style="margin: 0 0 8px 0; color: #27ae60; cursor: pointer;" onclick="togglePreviewSection('includedTable')">
                        Included <span id="includedCount"></span>
                    </h4>
                    <div id="includedTable" style="overflow-x: auto;">
                        <table style="width: 100%; border-collapse: collapse; font-size: 0.85em;">
                            <thead>
                                <tr style="background: #eafaf1;">
                                    <th style="padding: 6px 10px; text-align: left; border-bottom: 1px solid #ddd;">Phone</th>
                                    <th style="padding: 6px 10px; text-align: left; border-bottom: 1px solid #ddd;">Name</th>
                                    <th style="padding: 6px 10px; text-align: left; border-bottom: 1px solid #ddd;">Tier</th>
                                    <th style="padding: 6px 10px; text-align: left; border-bottom: 1px solid #ddd;">Timezone</th>
                                    <th style="padding: 6px 10px; text-align: left; border-bottom: 1px solid #ddd;">Local Time</th>
                                </tr>
                            </thead>
                            <tbody id="includedTableBody"></tbody>
                        </table>
                    </div>
                </div>
                <div id="excludedSection" style="margin-top: 15px;">
                    <h4 style="margin: 0 0 8px 0; color: #e74c3c; cursor: pointer;" onclick="togglePreviewSection('excludedTable')">
                        Excluded <span id="excludedCount"></span>
                    </h4>
                    <div id="excludedTable" style="overflow-x: auto;">
                        <table style="width: 100%; border-collapse: collapse; font-size: 0.85em;">
                            <thead>
                                <tr style="background: #fdedec;">
                                    <th style="padding: 6px 10px; text-align: left; border-bottom: 1px solid #ddd;">Phone</th>
                                    <th style="padding: 6px 10px; text-align: left; border-bottom: 1px solid #ddd;">Name</th>
                                    <th style="padding: 6px 10px; text-align: left; border-bottom: 1px solid #ddd;">Reason</th>
                                    <th style="padding: 6px 10px; text-align: left; border-bottom: 1px solid #ddd;">Timezone</th>
                                    <th style="padding: 6px 10px; text-align: left; border-bottom: 1px solid #ddd;">Local Time</th>
                                    <th style="padding: 6px 10px; text-align: left; border-bottom: 1px solid #ddd;"></th>
                                </tr>
                            </thead>
                            <tbody id="excludedTableBody"></tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>

        <div class="progress-info" id="progressInfo">
            <strong>Broadcast Status:</strong>
            <div id="progressText">Sending...</div>
        </div>

        <button class="btn btn-primary" id="sendBtn" onclick="showConfirmModal()" disabled>
            Send Now
        </button>
    </div>

    <!-- Scheduled Broadcasts -->
    <div class="section" id="scheduledSection">
        <h2>Scheduled Broadcasts</h2>
        <table class="history-table" id="scheduledTable">
            <tr>
                <th>Scheduled For</th>
                <th>Audience</th>
                <th>Message</th>
                <th>Status</th>
                <th style="width: 100px;">Actions</th>
            </tr>
            <tr id="scheduledLoading">
                <td colspan="5" style="color: #95a5a6; text-align: center;">Loading...</td>
            </tr>
        </table>
    </div>

    <!-- Broadcast History -->
    <div class="section">
        <h2>Broadcast History</h2>
        <table class="history-table" id="historyTable">
            <tr>
                <th>Date</th>
                <th>Audience</th>
                <th>Message</th>
                <th>Recipients</th>
                <th>Success</th>
                <th>Failed</th>
                <th>Status</th>
            </tr>
            <tr id="historyLoading">
                <td colspan="7" style="color: #95a5a6; text-align: center;">Loading history...</td>
            </tr>
        </table>
    </div>

    <!-- Support Tickets Section -->
    <div id="support" class="collapsible-section section-anchor">
        <div class="section-header" onclick="toggleSection('support')">
            <h2>🎧 Support Tickets <span id="openTicketCount" style="font-size: 0.7em; color: #7f8c8d;"></span></h2>
            <span class="section-toggle">▼</span>
        </div>
        <div class="section-content">
            <p style="color: #7f8c8d; margin-bottom: 15px;">
                Users can text "Support [message]" to create tickets. Feedback and bug reports go to <a href="#contact-messages" style="color: #3498db;">Contact Messages</a>.
                <a href="/cs" style="color: #3498db; font-weight: 600;">Open CS Portal</a> for full ticket management with filtering, assignment, and canned responses.
            </p>

            <div style="margin-bottom: 15px;">
                <label style="margin-right: 10px;">
                    <input type="checkbox" id="showClosedTickets" onchange="loadSupportTickets()"> Show closed tickets
                </label>
            </div>

            <div id="supportTicketsList" style="margin-bottom: 20px;">
                <p style="color: #95a5a6;">Loading...</p>
            </div>
        </div>

        <!-- Ticket Detail Modal (outside section-content so it's not affected by collapse) -->
        <div id="ticketModal" style="display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 1000;">
            <div style="background: white; max-width: 600px; margin: 50px auto; border-radius: 8px; max-height: 80vh; overflow: hidden; display: flex; flex-direction: column;">
                <div style="padding: 15px 20px; border-bottom: 1px solid #ddd; display: flex; justify-content: space-between; align-items: center;">
                    <div style="display: flex; align-items: center; gap: 15px;">
                        <h3 style="margin: 0;" id="ticketModalTitle">Ticket #</h3>
                        <button onclick="viewTicketCustomer()" class="btn" style="background: #9b59b6; color: white; font-size: 0.85em; padding: 5px 10px;">Customer Profile</button>
                    </div>
                    <button onclick="closeTicketModal()" style="background: none; border: none; font-size: 24px; cursor: pointer;">&times;</button>
                </div>
                <div id="ticketMessages" style="flex: 1; overflow-y: auto; padding: 20px; background: #f5f6fa;">
                    <!-- Messages will be loaded here -->
                </div>
                <div style="padding: 15px 20px; border-top: 1px solid #ddd; background: white;">
                    <div style="display: flex; gap: 10px;">
                        <input type="text" id="ticketReplyInput" placeholder="Type your reply..." style="flex: 1; padding: 10px; border: 1px solid #ddd; border-radius: 4px;">
                        <button onclick="sendTicketReply()" class="btn" style="background: #27ae60;">Send</button>
                    </div>
                    <div style="margin-top: 10px; display: flex; gap: 10px;">
                        <button onclick="closeCurrentTicket()" id="closeTicketBtn" class="btn" style="background: #e74c3c;">Close Ticket</button>
                        <button onclick="reopenCurrentTicket()" id="reopenTicketBtn" class="btn" style="background: #f39c12; display: none;">Reopen Ticket</button>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Contact Messages Section -->
    <div id="contact-messages" class="collapsible-section section-anchor">
        <div class="section-header" onclick="toggleSection('contact-messages')">
            <h2>📨 Contact Messages <span id="contactMsgCount" style="font-size: 0.7em; color: #7f8c8d;"></span></h2>
            <span class="section-toggle">▼</span>
        </div>
        <div class="section-content">
            <p style="color: #7f8c8d; margin-bottom: 15px;">
                Feedback, bug reports, and questions from SMS and web. These are lightweight messages — not support tickets.
            </p>
            <div style="margin-bottom: 15px; display: flex; gap: 15px; align-items: center;">
                <label>
                    <input type="checkbox" id="showResolvedContactMsgs" onchange="loadContactMessages()"> Show resolved
                </label>
                <label>Category:
                    <select id="contactMsgCategory" onchange="loadContactMessages()" style="padding: 4px 8px; border-radius: 4px; border: 1px solid #ddd;">
                        <option value="">All</option>
                        <option value="feedback">Feedback</option>
                        <option value="bug">Bug</option>
                        <option value="question">Question</option>
                    </select>
                </label>
            </div>
            <div id="contactMessagesList">
                <p style="color: #95a5a6;">Loading...</p>
            </div>
        </div>
    </div>

    <!-- User Feedback Section -->
    <div id="feedback" class="section section-anchor">
        <h2>User Feedback <span id="feedbackCount" style="font-size: 0.7em; color: #7f8c8d;"></span></h2>

        <!-- Open Feedback -->
        <h3 style="margin: 15px 0 10px; font-size: 1em; color: #e67e22;">Open Feedback <span id="openFeedbackCount" style="font-weight: normal;"></span></h3>
        <table class="feedback-table" id="openFeedbackTable">
            <tr>
                <th>Date</th>
                <th>Phone</th>
                <th>Message</th>
                <th style="width: 80px; text-align: center;">Resolved</th>
            </tr>
            <tr id="openFeedbackLoading">
                <td colspan="4" style="color: #95a5a6; text-align: center;">Loading feedback...</td>
            </tr>
        </table>

        <!-- Resolved Feedback (Collapsible) -->
        <div style="margin-top: 20px;">
            <h3 style="margin: 0 0 10px; font-size: 1em; color: #27ae60; cursor: pointer;" onclick="toggleResolvedSection()">
                <span id="resolvedToggleIcon">▶</span> Resolved Feedback <span id="resolvedFeedbackCount" style="font-weight: normal;"></span>
            </h3>
            <div id="resolvedFeedbackSection" style="display: none;">
                <table class="feedback-table" id="resolvedFeedbackTable">
                    <tr>
                        <th>Date</th>
                        <th>Phone</th>
                        <th>Message</th>
                        <th style="width: 80px; text-align: center;">Resolved</th>
                    </tr>
                </table>
            </div>
        </div>
    </div>

    <!-- Cost Analytics Section -->
    <div id="costs" class="collapsible-section section-anchor">
        <div class="section-header" onclick="toggleSection('costs')">
            <h2>💰 Cost Analytics</h2>
            <span class="section-toggle">▼</span>
        </div>
        <div class="section-content">
            <div class="period-tabs">
                <button class="period-tab active" onclick="showCostPeriod('day')">Today</button>
                <button class="period-tab" onclick="showCostPeriod('week')">This Week</button>
                <button class="period-tab" onclick="showCostPeriod('month')">This Month</button>
                <button class="period-tab" onclick="showCostPeriod('hour')">Last Hour</button>
            </div>

            <table class="cost-table" id="costTable">
                <tr class="cost-header">
                    <th>Plan Tier</th>
                    <th>Users</th>
                    <th>Messages</th>
                    <th>SMS Cost</th>
                    <th>AI Tokens</th>
                    <th>AI Cost</th>
                    <th>Total Cost</th>
                    <th>Cost/User</th>
                </tr>
                <tr id="costLoading">
                    <td colspan="8" style="color: #95a5a6; text-align: center;">Loading cost data...</td>
                </tr>
            </table>

            <div id="twilioActualSummary" style="margin-top: 15px; padding: 12px 15px; background: #f8f9fa; border-radius: 6px; font-size: 0.9em; display: none;">
                <!-- Populated by JS -->
            </div>

            <div style="margin-top: 15px; font-size: 0.85em; color: #7f8c8d;">
                <em>SMS Estimated: $0.0079/message (inbound + outbound) | AI: GPT-4o-mini pricing | Actual SMS costs polled daily from Twilio</em>
            </div>
        </div>
    </div>

    <!-- Changelog Management Section -->
    <div id="changelog" class="section section-anchor">
        <h2>📋 Updates & Changelog</h2>
        <p style="color: #7f8c8d; margin-bottom: 15px;">
            Public page: <a href="/updates" target="_blank">/updates</a> - Share this link with users instead of sending broadcast messages for every update.
        </p>

        <div class="broadcast-form" style="margin-bottom: 20px;">
            <h3 style="margin-top: 0;">Add New Entry</h3>
            <div style="margin-bottom: 10px;">
                <label style="display: block; margin-bottom: 5px; font-weight: 500;">Type:</label>
                <select id="changelogType" style="padding: 8px; border: 1px solid #ddd; border-radius: 4px; width: 200px;">
                    <option value="bug_fix">🐛 Bug Fix</option>
                    <option value="feature">✨ New Feature</option>
                    <option value="improvement" selected>🔧 Improvement</option>
                </select>
            </div>
            <div style="margin-bottom: 10px;">
                <label style="display: block; margin-bottom: 5px; font-weight: 500;">Title:</label>
                <input type="text" id="changelogTitle" placeholder="Brief title (e.g., 'Fixed reminder timezone bug')" style="width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px;">
            </div>
            <div style="margin-bottom: 10px;">
                <label style="display: block; margin-bottom: 5px; font-weight: 500;">Description (optional):</label>
                <textarea id="changelogDescription" placeholder="More details about the change..." style="width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; height: 60px;"></textarea>
            </div>
            <button onclick="addChangelogEntry()" class="btn" style="background: #27ae60;">Add Entry</button>
        </div>

        <h3>Recent Entries</h3>
        <div id="changelogEntries" style="max-height: 400px; overflow-y: auto;">
            <p style="color: #95a5a6;">Loading...</p>
        </div>
    </div>

    <!-- Conversation Viewer Section -->
    <div id="conversations" class="collapsible-section section-anchor">
        <div class="section-header" onclick="toggleSection('conversations')">
            <h2>💬 Conversation Viewer</h2>
            <span class="section-toggle">▼</span>
        </div>
        <div class="section-content">
        <div class="tabs">
            <button class="tab active" onclick="showConversationTab('recent')">Recent Conversations</button>
            <button class="tab" onclick="showConversationTab('flagged')">
                Flagged <span id="flaggedCount" style="background: #e74c3c; color: white; padding: 2px 6px; border-radius: 10px; font-size: 0.8em; margin-left: 5px;">0</span>
            </button>
        </div>

        <!-- Recent Conversations Tab -->
        <div id="recentTab">
            <div class="conversation-filters">
                <button class="btn" id="toggleReviewedBtn" style="background: #27ae60; color: white;" onclick="toggleHideReviewed()">Show Reviewed</button>
                <input type="text" id="phoneFilter" placeholder="Filter by phone (last 4 digits)..." style="width: 180px;">
                <select id="intentFilter" style="padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px;">
                    <option value="">All Intents</option>
                    <option value="store">Store</option>
                    <option value="retrieve">Retrieve</option>
                    <option value="reminder">Reminder</option>
                    <option value="reminder_relative">Reminder (Relative)</option>
                    <option value="list_reminders">List Reminders</option>
                    <option value="delete_reminder">Delete Reminder</option>
                    <option value="delete_memory">Delete Memory</option>
                    <option value="create_list">Create List</option>
                    <option value="add_to_list">Add to List</option>
                    <option value="show_list">Show List</option>
                    <option value="show_all_lists">Show All Lists</option>
                    <option value="complete_item">Complete Item</option>
                    <option value="delete_item">Delete Item</option>
                    <option value="help">Help</option>
                    <option value="clarify_time">Clarify Time</option>
                    <option value="error">Error</option>
                </select>
                <button class="btn btn-primary" onclick="loadConversations()">Search</button>
                <button class="btn btn-secondary" onclick="clearFilter()">Clear</button>
                <span style="color: #7f8c8d; margin-left: auto;">Showing <span id="conversationCount">0</span> conversations</span>
            </div>

            <table class="conversation-table" id="conversationTable">
                <tr>
                    <th style="width: 150px;">Time</th>
                    <th style="width: 100px;">Phone</th>
                    <th>User Message</th>
                    <th>System Response</th>
                    <th style="width: 100px;">Intent</th>
                    <th style="width: 70px;">Action</th>
                </tr>
                <tr id="conversationLoading">
                    <td colspan="6" style="color: #95a5a6; text-align: center;">Loading conversations...</td>
                </tr>
            </table>

            <div class="pagination">
                <button class="btn btn-secondary" id="prevBtn" onclick="loadConversations(currentOffset - 50)" disabled>Previous</button>
                <span id="pageInfo" style="padding: 8px;">Page 1</span>
                <button class="btn btn-secondary" id="nextBtn" onclick="loadConversations(currentOffset + 50)">Next</button>
            </div>
        </div>

        <!-- Flagged Conversations Tab -->
        <div id="flaggedTab" style="display: none;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                <div>
                    <label style="cursor: pointer;">
                        <input type="checkbox" id="showReviewedCheckbox" onchange="loadFlaggedConversations()">
                        Show reviewed items
                    </label>
                </div>
                <div style="display: flex; gap: 10px;">
                    <button class="btn" style="background: #9b59b6; color: white;" onclick="exportFlagged()">Export for Claude</button>
                    <button class="btn btn-primary" onclick="runAnalysis()">Run AI Analysis Now</button>
                </div>
            </div>

            <div id="analysisStatus" style="display: none; padding: 10px; background: #d4edda; border-radius: 4px; margin-bottom: 15px;"></div>

            <table class="conversation-table" id="flaggedTable">
                <tr>
                    <th style="width: 60px;">Source</th>
                    <th style="width: 140px;">Time</th>
                    <th style="width: 90px;">Phone</th>
                    <th>Conversation</th>
                    <th style="width: 120px;">Issue</th>
                    <th style="width: 80px;">Actions</th>
                </tr>
                <tr id="flaggedLoading">
                    <td colspan="6" style="color: #95a5a6; text-align: center;">Loading flagged conversations...</td>
                </tr>
            </table>
        </div>
        </div>
    </div>

    <!-- Recurring Reminders Section -->
    <div id="recurring" class="collapsible-section section-anchor">
        <div class="section-header" onclick="toggleSection('recurring')">
            <h2>🔄 Recurring Reminders</h2>
            <span class="section-toggle">▼</span>
        </div>
        <div class="section-content">
        <p style="color: #7f8c8d; margin-bottom: 15px;">Manage all recurring reminders across users.</p>

        <div style="display: flex; gap: 10px; margin-bottom: 15px;">
            <button class="btn btn-primary" onclick="loadRecurring()">Refresh</button>
            <input type="text" id="recurringPhoneFilter" placeholder="Filter by phone (last 4 digits)..." style="padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px; width: 200px;">
            <select id="recurringStatusFilter" style="padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px;">
                <option value="">All Status</option>
                <option value="active">Active Only</option>
                <option value="paused">Paused Only</option>
            </select>
            <span style="color: #7f8c8d; margin-left: auto; padding: 8px;">Total: <span id="recurringCount">0</span></span>
        </div>

        <table id="recurringTable">
            <tr>
                <th style="width: 80px;">ID</th>
                <th style="width: 80px;">Phone</th>
                <th>Reminder</th>
                <th style="width: 140px;">Pattern</th>
                <th style="width: 80px;">Time</th>
                <th style="width: 100px;">Timezone</th>
                <th style="width: 70px;">Status</th>
                <th style="width: 140px;">Next</th>
                <th style="width: 120px;">Actions</th>
            </tr>
            <tr id="recurringLoading">
                <td colspan="9" style="color: #95a5a6; text-align: center;">Loading recurring reminders...</td>
            </tr>
        </table>
        </div>
    </div>

    <!-- Customer Service Section -->
    <div id="customer-service" class="collapsible-section section-anchor">
        <div class="section-header" onclick="toggleSection('customer-service')">
            <h2>👥 Customer Service</h2>
            <span class="section-toggle">▼</span>
        </div>
        <div class="section-content">

        <div style="display: flex; gap: 20px; margin-bottom: 20px;">
            <div style="flex: 1;">
                <input type="text" id="csSearchInput" placeholder="Search by phone number or name..."
                    style="width: 100%; padding: 12px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px;"
                    onkeyup="if(event.key === 'Enter') csSearch()">
            </div>
            <button class="btn" onclick="csSearch()" style="background: #3498db; color: white; padding: 12px 24px;">
                Search
            </button>
        </div>

        <div id="csSearchResults" style="display: none; margin-bottom: 20px;">
            <h3>Search Results <span id="csResultCount" style="color: #7f8c8d; font-weight: normal;"></span></h3>
            <table class="history-table" id="csResultsTable">
                <thead>
                    <tr>
                        <th>Phone</th>
                        <th>Name</th>
                        <th>Tier</th>
                        <th>Status</th>
                        <th>Last Active</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody id="csResultsBody"></tbody>
            </table>
        </div>

        <div id="csCustomerProfile" style="display: none;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                <h3>Customer Profile</h3>
                <button class="btn btn-secondary" onclick="csCloseProfile()" style="padding: 8px 16px;">
                    ← Back to Search
                </button>
            </div>

            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
                <!-- Left Column - Profile Info -->
                <div class="card" style="padding: 20px;">
                    <h4 style="margin-bottom: 15px; color: #2c3e50;">Profile Information</h4>
                    <div id="csProfileInfo" style="line-height: 1.8;"></div>

                    <h4 style="margin: 20px 0 15px; color: #2c3e50;">Usage Stats</h4>
                    <div id="csProfileStats" style="line-height: 1.8;"></div>

                    <h4 style="margin: 20px 0 15px; color: #2c3e50;">Change Tier</h4>
                    <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
                        <select id="csTierSelect" style="padding: 8px; border: 1px solid #ddd; border-radius: 4px;">
                            <option value="free">Free</option>
                            <option value="premium">Premium</option>
                            <option value="family">Family</option>
                        </select>
                        <input type="text" id="csTierReason" placeholder="Reason..." style="flex: 1; min-width: 150px; padding: 8px; border: 1px solid #ddd; border-radius: 4px;">
                        <button class="btn" onclick="csUpdateTier()" style="background: #27ae60; color: white; padding: 8px 16px;">Update</button>
                    </div>
                    <div style="margin-top: 10px; display: flex; gap: 10px; align-items: center;">
                        <label style="display: flex; align-items: center; gap: 5px; cursor: pointer;">
                            <input type="checkbox" id="csTrialMode" onchange="toggleTrialDatePicker()">
                            <span style="font-size: 0.9em;">Set as free trial (expires on date)</span>
                        </label>
                        <input type="date" id="csTrialEndDate" style="padding: 8px; border: 1px solid #ddd; border-radius: 4px; display: none;">
                    </div>
                </div>

                <!-- Right Column - Notes -->
                <div class="card" style="padding: 20px;">
                    <h4 style="margin-bottom: 15px; color: #2c3e50;">Notes</h4>
                    <div style="display: flex; gap: 10px; margin-bottom: 15px;">
                        <input type="text" id="csNewNote" placeholder="Add a note..." style="flex: 1; padding: 8px; border: 1px solid #ddd; border-radius: 4px;">
                        <button class="btn" onclick="csAddNote()" style="background: #3498db; color: white; padding: 8px 16px;">Add</button>
                    </div>
                    <div id="csNotesList" style="max-height: 200px; overflow-y: auto;"></div>
                </div>
            </div>

            <!-- Recent Messages -->
            <div class="card" style="padding: 20px; margin-top: 20px;">
                <h4 style="margin-bottom: 15px; color: #2c3e50;">Recent Messages</h4>
                <table class="history-table">
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>User Said</th>
                            <th>System Replied</th>
                            <th>Intent</th>
                        </tr>
                    </thead>
                    <tbody id="csMessagesBody"></tbody>
                </table>
            </div>

            <!-- Data Tabs -->
            <div class="card" style="padding: 20px; margin-top: 20px;">
                <div style="display: flex; gap: 10px; margin-bottom: 15px;">
                    <button class="btn" onclick="csShowTab('reminders')" id="csTabReminders" style="background: #3498db; color: white;">Reminders</button>
                    <button class="btn btn-secondary" onclick="csShowTab('lists')" id="csTabLists">Lists</button>
                    <button class="btn btn-secondary" onclick="csShowTab('memories')" id="csTabMemories">Memories</button>
                    <button class="btn btn-secondary" onclick="csShowTab('nudges')" id="csTabNudges">Nudges</button>
                </div>

                <div id="csTabContent">
                    <div id="csRemindersTab"></div>
                    <div id="csListsTab" style="display: none;"></div>
                    <div id="csMemoriesTab" style="display: none;"></div>
                    <div id="csNudgesTab" style="display: none;"></div>
                </div>
            </div>
        </div>
        </div>
    </div>

    <!-- Product Metrics Section -->
    <div id="product-metrics" class="collapsible-section section-anchor">
        <div class="section-header" onclick="toggleSection('product-metrics')">
            <h2>Product Metrics</h2>
            <span class="section-toggle">▼</span>
        </div>
        <div class="section-content">
            <div id="pmLoading" style="text-align: center; padding: 40px; color: #95a5a6;">
                Loading product metrics...
            </div>
            <div id="pmError" style="display: none; background: #fdf2f2; border: 1px solid #e74c3c; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                <h4 style="color: #e74c3c; margin: 0 0 10px 0;">Error Loading Product Metrics</h4>
                <p id="pmErrorMsg" style="color: #7f8c8d; margin: 0;"></p>
            </div>
            <div id="pmContent" style="display: none;">

                <!-- 1. Trial Conversion Funnel -->
                <h3 style="color: #2c3e50; margin-bottom: 15px;">Trial Conversion Funnel</h3>
                <div id="pmFunnelBar" style="display: flex; height: 40px; border-radius: 8px; overflow: hidden; margin-bottom: 15px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);"></div>
                <div style="overflow-x: auto; margin-bottom: 30px;">
                    <table>
                        <thead><tr><th>State</th><th>Users</th><th>% of Total</th></tr></thead>
                        <tbody id="pmFunnelBody"></tbody>
                    </table>
                </div>

                <!-- 2. Signups by Attribution Keyword -->
                <h3 style="color: #2c3e50; margin-bottom: 15px;">Signups by Attribution Keyword</h3>
                <div id="pmAttributionChart" style="background: white; border-radius: 8px; padding: 20px; margin-bottom: 15px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);"></div>
                <div style="overflow-x: auto; margin-bottom: 30px;">
                    <table>
                        <thead><tr><th>Keyword</th><th>Source / Channel</th><th>Total</th><th>Last 7 Days</th><th>Last 30 Days</th></tr></thead>
                        <tbody id="pmAttributionBody"></tbody>
                    </table>
                </div>

                <!-- 3. Daily Active Users Trend -->
                <h3 style="color: #2c3e50; margin-bottom: 15px;">Daily Active Users (Last 30 Days)</h3>
                <div style="background: white; border-radius: 8px; padding: 20px; margin-bottom: 30px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                    <div id="pmDAUChart" style="height: 200px; display: flex; align-items: flex-end; gap: 2px;"></div>
                    <div id="pmDAULabels" style="display: flex; gap: 2px; margin-top: 4px;"></div>
                </div>

                <!-- 4. User Retention Metrics -->
                <h3 style="color: #2c3e50; margin-bottom: 15px;">User Retention</h3>
                <div class="cards" style="margin-bottom: 15px;">
                    <div class="card">
                        <div class="card-title">Total Onboarded</div>
                        <div class="card-value" id="pmRetTotal">0</div>
                    </div>
                    <div class="card green">
                        <div class="card-title">Active (7 days)</div>
                        <div class="card-value" id="pmRetActive">0</div>
                    </div>
                    <div class="card orange">
                        <div class="card-title">Churned (14+ days inactive)</div>
                        <div class="card-value" id="pmRetChurned">0</div>
                    </div>
                </div>
                <div style="overflow-x: auto; margin-bottom: 15px;">
                    <table>
                        <thead><tr><th>Metric</th><th>Eligible</th><th>Retained</th><th>Rate</th></tr></thead>
                        <tbody id="pmRetentionBody"></tbody>
                    </table>
                </div>
                <h4 style="color: #34495e; margin-bottom: 10px;">Retention by Signup Cohort</h4>
                <div style="overflow-x: auto; margin-bottom: 30px;">
                    <table>
                        <thead><tr><th>Cohort (Week)</th><th>Size</th><th>Day 1 %</th><th>Day 7 %</th><th>Day 14 %</th></tr></thead>
                        <tbody id="pmCohortBody"></tbody>
                    </table>
                </div>

                <!-- 5. Time to First Action -->
                <h3 style="color: #2c3e50; margin-bottom: 15px;">Time to First Action</h3>
                <div class="cards" style="margin-bottom: 30px;">
                    <div class="card blue">
                        <div class="card-title">Median to First Reminder</div>
                        <div class="card-value" id="pmTTAReminder" style="font-size: 1.5em;">-</div>
                    </div>
                    <div class="card blue">
                        <div class="card-title">Median to First Memory</div>
                        <div class="card-value" id="pmTTAMemory" style="font-size: 1.5em;">-</div>
                    </div>
                    <div class="card green">
                        <div class="card-title">Reminder Within 1 Hour</div>
                        <div class="card-value" id="pmTTA1h">0%</div>
                    </div>
                    <div class="card green">
                        <div class="card-title">Reminder Within 24 Hours</div>
                        <div class="card-value" id="pmTTA24h">0%</div>
                    </div>
                    <div class="card orange">
                        <div class="card-title">Never Set Reminder</div>
                        <div class="card-value" id="pmTTANever">0%</div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Website Analytics Section -->
    <div id="website-analytics" class="collapsible-section section-anchor">
        <div class="section-header" onclick="toggleSection('website-analytics')">
            <h2>Website Analytics</h2>
            <span class="section-toggle">▼</span>
        </div>
        <div class="section-content">
            <div style="display: flex; gap: 10px; align-items: center; margin-bottom: 20px;">
                <span style="font-weight: 500; color: #2c3e50;">Date Range:</span>
                <button class="filter-btn analytics-range-btn active" onclick="setAnalyticsRange(7)" id="analyticsRange7">Last 7 days</button>
                <button class="filter-btn analytics-range-btn" onclick="setAnalyticsRange(14)" id="analyticsRange14">Last 14 days</button>
                <button class="filter-btn analytics-range-btn" onclick="setAnalyticsRange(30)" id="analyticsRange30">Last 30 days</button>
                <button class="btn btn-secondary" style="margin-left: auto; padding: 6px 14px; font-size: 0.85em;" onclick="clearAnalyticsCache()">Clear Cache</button>
                <button class="btn btn-primary" style="padding: 6px 14px; font-size: 0.85em;" onclick="exportAnalyticsJSON()">Export JSON</button>
            </div>

            <div id="analyticsLoading" style="text-align: center; padding: 40px; color: #95a5a6;">
                Loading analytics data...
            </div>

            <div id="analyticsError" style="display: none; background: #fdf2f2; border: 1px solid #e74c3c; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                <h4 style="color: #e74c3c; margin: 0 0 10px 0;">Analytics Not Available</h4>
                <p id="analyticsErrorMsg" style="color: #7f8c8d; margin: 0;"></p>
            </div>

            <div id="analyticsContent" style="display: none;">
                <!-- Summary Cards -->
                <div class="cards" style="margin-bottom: 30px;">
                    <div class="card blue">
                        <div class="card-title">Total Users</div>
                        <div class="card-value" id="anTotalUsers">0</div>
                    </div>
                    <div class="card green">
                        <div class="card-title">Total Sessions</div>
                        <div class="card-value" id="anTotalSessions">0</div>
                    </div>
                    <div class="card">
                        <div class="card-title">Avg Engagement Time</div>
                        <div class="card-value" id="anAvgEngagement">0s</div>
                    </div>
                    <div class="card">
                        <div class="card-title">Engagement Rate</div>
                        <div class="card-value" id="anEngagementRate">0%</div>
                    </div>
                </div>

                <!-- Daily Traffic Trend -->
                <h3 style="color: #2c3e50; margin-bottom: 15px;">Daily Traffic Trend</h3>
                <div style="background: white; border-radius: 8px; padding: 20px; margin-bottom: 30px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                    <canvas id="trafficChart" height="200"></canvas>
                </div>

                <!-- A/B Test Comparison -->
                <h3 style="color: #2c3e50; margin-bottom: 15px;">A/B Test Comparison</h3>
                <div id="abTestSection" style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-bottom: 30px;">
                    <div class="card" style="border-top: 3px solid #4A90A4;">
                        <div class="card-title">Variant A (Long Form)</div>
                        <div id="abVariantA" style="margin-top: 10px;">
                            <div style="display: flex; justify-content: space-between; margin-bottom: 5px;">
                                <span style="color: #7f8c8d;">Users</span>
                                <span id="abAUsers" style="font-weight: 600;">-</span>
                            </div>
                            <div style="display: flex; justify-content: space-between; margin-bottom: 5px;">
                                <span style="color: #7f8c8d;">Event Count</span>
                                <span id="abACount" style="font-weight: 600;">-</span>
                            </div>
                        </div>
                    </div>
                    <div class="card" style="border-top: 3px solid #50B688;">
                        <div class="card-title">Variant B (Short Form)</div>
                        <div id="abVariantB" style="margin-top: 10px;">
                            <div style="display: flex; justify-content: space-between; margin-bottom: 5px;">
                                <span style="color: #7f8c8d;">Users</span>
                                <span id="abBUsers" style="font-weight: 600;">-</span>
                            </div>
                            <div style="display: flex; justify-content: space-between; margin-bottom: 5px;">
                                <span style="color: #7f8c8d;">Event Count</span>
                                <span id="abBCount" style="font-weight: 600;">-</span>
                            </div>
                        </div>
                    </div>
                </div>
                <div id="abWinner" style="text-align: center; margin-bottom: 30px; display: none;">
                    <span id="abWinnerText" style="padding: 6px 16px; border-radius: 20px; font-weight: 600; font-size: 0.9em;"></span>
                </div>

                <!-- Traffic Sources -->
                <h3 style="color: #2c3e50; margin-bottom: 15px;">Traffic Sources</h3>
                <div style="overflow-x: auto; margin-bottom: 30px;">
                    <table>
                        <thead>
                            <tr><th>Source / Medium</th><th>Sessions</th><th>Users</th><th>Engagement Rate</th><th>Avg Engagement Time</th></tr>
                        </thead>
                        <tbody id="trafficSourcesBody">
                            <tr><td colspan="5" style="color: #95a5a6; text-align: center;">Loading...</td></tr>
                        </tbody>
                    </table>
                </div>

                <!-- Landing Page Performance -->
                <h3 style="color: #2c3e50; margin-bottom: 15px;">Landing Page Performance</h3>
                <div style="overflow-x: auto; margin-bottom: 30px;">
                    <table>
                        <thead>
                            <tr><th>Page</th><th>Sessions</th><th>Users</th><th>Engagement Rate</th><th>Avg Engagement Time</th></tr>
                        </thead>
                        <tbody id="landingPagesBody">
                            <tr><td colspan="5" style="color: #95a5a6; text-align: center;">Loading...</td></tr>
                        </tbody>
                    </table>
                </div>

                <!-- Device Breakdown -->
                <h3 style="color: #2c3e50; margin-bottom: 15px;">Device Breakdown</h3>
                <div style="overflow-x: auto; margin-bottom: 30px;">
                    <table>
                        <thead>
                            <tr><th>Device</th><th>OS</th><th>Sessions</th><th>Users</th><th>%</th></tr>
                        </thead>
                        <tbody id="devicesBody">
                            <tr><td colspan="5" style="color: #95a5a6; text-align: center;">Loading...</td></tr>
                        </tbody>
                    </table>
                </div>

                <!-- Engagement Events -->
                <h3 style="color: #2c3e50; margin-bottom: 15px;">Engagement Events</h3>
                <div style="overflow-x: auto; margin-bottom: 30px;">
                    <table>
                        <thead>
                            <tr><th>Event Name</th><th>Event Count</th><th>Users</th></tr>
                        </thead>
                        <tbody id="keyEventsBody">
                            <tr><td colspan="3" style="color: #95a5a6; text-align: center;">Loading...</td></tr>
                        </tbody>
                    </table>
                </div>

                <!-- Search Console: Top Queries -->
                <h3 style="color: #2c3e50; margin-bottom: 15px;">Organic Search - Top Queries</h3>
                <div style="overflow-x: auto; margin-bottom: 30px;">
                    <table>
                        <thead>
                            <tr><th>Query</th><th>Impressions</th><th>Clicks</th><th>CTR</th><th>Avg Position</th></tr>
                        </thead>
                        <tbody id="searchQueriesBody">
                            <tr><td colspan="5" style="color: #95a5a6; text-align: center;">Loading...</td></tr>
                        </tbody>
                    </table>
                </div>

                <!-- Search Console: Top Pages -->
                <h3 style="color: #2c3e50; margin-bottom: 15px;">Organic Search - Top Pages</h3>
                <div style="overflow-x: auto; margin-bottom: 30px;">
                    <table>
                        <thead>
                            <tr><th>Page</th><th>Impressions</th><th>Clicks</th><th>CTR</th><th>Avg Position</th></tr>
                        </thead>
                        <tbody id="searchPagesBody">
                            <tr><td colspan="5" style="color: #95a5a6; text-align: center;">Loading...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <!-- AI Analytics Summary Section -->
    <div id="ai-analytics-summary" class="collapsible-section section-anchor">
        <div class="section-header" onclick="toggleSection('ai-analytics-summary')">
            <h2>AI Analytics Summary</h2>
            <span class="section-toggle">▼</span>
        </div>
        <div class="section-content">
            <div style="display: flex; gap: 10px; align-items: center; margin-bottom: 20px;">
                <button class="btn btn-primary" style="padding: 8px 16px;" onclick="generateAISummary()">Generate Now</button>
                <button class="btn btn-secondary" style="padding: 8px 16px;" onclick="loadAISummaryHistory()">View History</button>
                <span id="aiSummaryStatus" style="color: #7f8c8d; font-size: 0.9em;"></span>
            </div>

            <div id="aiSummaryLoading" style="display: none; text-align: center; padding: 30px; color: #95a5a6;">
                Generating AI summary... this may take 15-30 seconds.
            </div>

            <div id="aiSummaryEmpty" style="display: none; background: #fdf9e8; border: 1px solid #f0e68c; border-radius: 8px; padding: 20px; margin-bottom: 20px;">
                <h4 style="color: #8b7500; margin: 0 0 8px 0;">No Summaries Yet</h4>
                <p style="color: #7f8c8d; margin: 0;">Click "Generate Now" to create your first AI-powered analytics summary.</p>
            </div>

            <div id="aiSummaryContent" style="display: none;">
                <!-- Trend Direction Banner -->
                <div id="aiTrendBanner" style="display: flex; align-items: center; gap: 12px; padding: 14px 20px; border-radius: 8px; margin-bottom: 20px; font-weight: 600;">
                    <span id="aiTrendIcon" style="font-size: 1.5em;"></span>
                    <span id="aiTrendLabel"></span>
                    <span id="aiTrendConfidence" style="margin-left: auto; font-size: 0.8em; font-weight: 400;"></span>
                </div>

                <!-- Metrics Cards -->
                <div class="cards" style="margin-bottom: 20px;">
                    <div class="card blue">
                        <div class="card-title">Users</div>
                        <div class="card-value" id="aiMetricUsers">-</div>
                        <div id="aiMetricUsersChange" style="font-size: 0.85em; margin-top: 4px;"></div>
                    </div>
                    <div class="card green">
                        <div class="card-title">Sessions</div>
                        <div class="card-value" id="aiMetricSessions">-</div>
                        <div id="aiMetricSessionsChange" style="font-size: 0.85em; margin-top: 4px;"></div>
                    </div>
                    <div class="card">
                        <div class="card-title">Engagement Rate</div>
                        <div class="card-value" id="aiMetricEngRate">-</div>
                        <div id="aiMetricEngRateChange" style="font-size: 0.85em; margin-top: 4px;"></div>
                    </div>
                    <div class="card">
                        <div class="card-title">Avg Engagement</div>
                        <div class="card-value" id="aiMetricEngTime">-</div>
                        <div id="aiMetricEngTimeChange" style="font-size: 0.85em; margin-top: 4px;"></div>
                    </div>
                </div>

                <!-- AI Summary Text -->
                <div style="background: white; border-radius: 8px; padding: 24px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); line-height: 1.7;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                        <h3 style="color: #2c3e50; margin: 0;">AI Analysis</h3>
                        <span id="aiSummaryDate" style="color: #95a5a6; font-size: 0.85em;"></span>
                    </div>
                    <div id="aiSummaryText" style="color: #34495e; white-space: pre-wrap;"></div>
                </div>

                <!-- Key Trends -->
                <div id="aiKeyTrends" style="display: none; background: white; border-radius: 8px; padding: 24px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                    <h3 style="color: #2c3e50; margin: 0 0 12px 0;">Key Trends</h3>
                    <ul id="aiKeyTrendsList" style="list-style: none; padding: 0; margin: 0;"></ul>
                </div>

                <!-- Notable Changes -->
                <div id="aiNotableChanges" style="display: none; background: white; border-radius: 8px; padding: 24px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                    <h3 style="color: #2c3e50; margin: 0 0 12px 0;">Notable Changes</h3>
                    <ul id="aiNotableChangesList" style="list-style: none; padding: 0; margin: 0;"></ul>
                </div>
            </div>

            <!-- History View -->
            <div id="aiSummaryHistory" style="display: none;">
                <h3 style="color: #2c3e50; margin-bottom: 15px;">Summary History</h3>
                <div id="aiHistoryList"></div>
            </div>

            <!-- Threaded Analytics Chat -->
            <div style="background: white; border-radius: 8px; padding: 0; margin-top: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); overflow: hidden;">
                <div style="padding: 20px 24px 12px 24px; border-bottom: 1px solid #eee;">
                    <h3 style="color: #2c3e50; margin: 0 0 6px 0;">Planning conversations</h3>
                    <p style="color: #7f8c8d; font-size: 0.9em; margin: 0;">
                        Ongoing chats with Claude about this data. Pick up where you left off across days.
                    </p>
                </div>

                <div style="display: flex; min-height: 500px;">
                    <!-- Sidebar: conversation list -->
                    <div style="width: 260px; border-right: 1px solid #eee; display: flex; flex-direction: column; background: #fafafa;">
                        <div style="padding: 12px;">
                            <button class="btn btn-primary" style="width: 100%; padding: 8px;" onclick="newChatConversation()">+ New conversation</button>
                        </div>
                        <div id="chatConvList" style="flex: 1; overflow-y: auto; max-height: 520px;">
                            <div style="padding: 20px; color: #95a5a6; text-align: center; font-size: 0.9em;">Loading...</div>
                        </div>
                    </div>

                    <!-- Main: active conversation -->
                    <div style="flex: 1; display: flex; flex-direction: column; min-width: 0;">
                        <div id="chatHeader" style="padding: 12px 20px; border-bottom: 1px solid #eee; display: none; align-items: center; gap: 8px;">
                            <span id="chatTitle" style="font-weight: 600; color: #2c3e50; flex: 1; cursor: text;" onclick="renameActiveChat()" title="Click to rename"></span>
                            <button class="btn btn-danger" style="padding: 4px 10px; font-size: 0.85em;" onclick="deleteActiveChat()">Delete</button>
                        </div>

                        <div id="chatMessages" style="flex: 1; overflow-y: auto; max-height: 440px; padding: 20px;">
                            <div style="color: #95a5a6; text-align: center; padding: 40px 20px;">
                                Select a conversation on the left, or start a new one.
                            </div>
                        </div>

                        <div id="chatInputWrap" style="padding: 12px 20px 20px 20px; border-top: 1px solid #eee; display: none;">
                            <div style="display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 8px; font-size: 0.85em;">
                                <label style="color: #7f8c8d;">Model:</label>
                                <select id="chatModel" onchange="updateChatModelUI()" style="padding: 4px 8px; border: 1px solid #ddd; border-radius: 4px;">
                                    <option value="haiku">Haiku 4.5</option>
                                    <option value="sonnet">Sonnet 4.6</option>
                                    <option value="opus">Opus 4.7</option>
                                </select>
                                <span id="chatEffortWrap" style="display: none;">
                                    <label style="color: #7f8c8d;">Effort:</label>
                                    <select id="chatEffort" onchange="updateChatModelUI()" style="padding: 4px 8px; border: 1px solid #ddd; border-radius: 4px;">
                                        <option value="low">Low</option>
                                        <option value="medium" selected>Medium</option>
                                        <option value="high">High</option>
                                        <option value="xhigh">X-High</option>
                                        <option value="max">Max</option>
                                    </select>
                                </span>
                                <span id="chatCostHint" style="color: #95a5a6; margin-left: auto;">~$0.007 / msg</span>
                            </div>

                            <!-- Staged attachments (before send) -->
                            <div id="chatStagedFiles" style="display: none; gap: 8px; flex-wrap: wrap; margin-bottom: 8px;"></div>

                            <div id="chatDropZone" style="display: flex; gap: 8px; align-items: flex-end; border: 2px dashed transparent; border-radius: 4px; transition: border-color 0.15s;">
                                <textarea id="chatInput" placeholder="Ask a question, follow up, or drop an image..." rows="2" style="flex: 1; padding: 10px 12px; border: 1px solid #ddd; border-radius: 4px; font-family: inherit; font-size: 0.95em; resize: vertical;" onkeydown="handleChatKey(event)"></textarea>
                                <div style="display: flex; flex-direction: column; gap: 6px;">
                                    <button type="button" class="btn btn-secondary" style="padding: 8px 14px; font-size: 0.85em;" onclick="document.getElementById('chatFileInput').click()" title="Attach image (max 3, 5MB each)">+ Image</button>
                                    <button class="btn btn-primary" id="chatSendBtn" style="padding: 8px 14px;" onclick="sendChatMessage()">Send</button>
                                </div>
                                <input type="file" id="chatFileInput" accept="image/jpeg,image/png,image/gif,image/webp" multiple style="display: none;" onchange="handleChatFilesPicked(event)">
                            </div>
                            <div id="chatStatus" style="color: #7f8c8d; font-size: 0.85em; margin-top: 6px; min-height: 1em;"></div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Settings Section -->
    <div id="settings" class="collapsible-section section-anchor">
        <div class="section-header" onclick="toggleSection('settings')">
            <h2>⚙️ Settings</h2>
            <span class="section-toggle">▼</span>
        </div>
        <div class="section-content">

        <!-- Staging Fallback Settings -->
        <div class="card" style="padding: 20px; margin-bottom: 20px;">
            <h4 style="margin-bottom: 15px; color: #2c3e50;">Staging Fallback Testing</h4>
            <p style="color: #7f8c8d; margin-bottom: 15px; font-size: 0.9em;">
                When enabled, messages from these phone numbers will fail in production,
                triggering Twilio to use the fallback URL (staging environment).
                <br><strong>Note:</strong> Configure the fallback URL in your Twilio phone number settings.
            </p>

            <div style="display: flex; align-items: center; gap: 15px; margin-bottom: 15px;">
                <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                    <input type="checkbox" id="stagingFallbackEnabled" onchange="updateStagingFallback()" style="width: 18px; height: 18px;">
                    <span style="font-weight: 500;">Enable Staging Fallback</span>
                </label>
                <span id="stagingFallbackStatus" style="padding: 4px 10px; border-radius: 4px; font-size: 0.85em;"></span>
            </div>

            <div class="form-group">
                <label for="stagingFallbackNumbers" style="display: block; margin-bottom: 5px; font-weight: 500; color: #2c3e50;">
                    Phone Numbers (one per line, include +1)
                </label>
                <textarea id="stagingFallbackNumbers"
                    style="width: 100%; min-height: 80px; padding: 10px; border: 1px solid #ddd; border-radius: 4px; font-family: monospace;"
                    placeholder="+15551234567&#10;+15559876543"
                    onchange="updateStagingFallback()"></textarea>
            </div>

            <div style="display: flex; gap: 10px; align-items: center;">
                <button class="btn" onclick="updateStagingFallback()" style="background: #27ae60; color: white; padding: 10px 20px;">
                    Save Settings
                </button>
                <span id="stagingFallbackSaveStatus" style="color: #27ae60; font-size: 0.9em;"></span>
            </div>
        </div>

        <!-- Maintenance Message Settings -->
        <div class="card" style="padding: 20px;">
            <h4 style="margin-bottom: 15px; color: #2c3e50;">Maintenance Message</h4>
            <p style="color: #7f8c8d; margin-bottom: 15px; font-size: 0.9em;">
                This message is shown to non-test users when staging environment receives their messages.
            </p>

            <div class="form-group">
                <textarea id="maintenanceMessage"
                    style="width: 100%; min-height: 80px; padding: 10px; border: 1px solid #ddd; border-radius: 4px;"
                    placeholder="Loading..."></textarea>
            </div>

            <div style="display: flex; gap: 10px; align-items: center;">
                <button class="btn" onclick="saveMaintenanceMessage()" style="background: #3498db; color: white; padding: 10px 20px;">
                    Save Message
                </button>
                <button class="btn btn-secondary" onclick="resetMaintenanceMessage()" style="padding: 10px 20px;">
                    Reset to Default
                </button>
                <span id="maintenanceSaveStatus" style="color: #27ae60; font-size: 0.9em;"></span>
            </div>
        </div>
        </div>
    </div>

    <!-- Pending Onboarding Modal -->
    <div class="modal" id="pendingOnboardingModal">
        <div class="modal-content" style="max-width: 850px;">
            <h3 style="color: #e67e22; margin-top: 0;">Pending Onboarding Users</h3>

            <!-- Nudge Message Selector -->
            <div id="nudgeControls" style="display: none; background: #fef9e7; padding: 15px; border-radius: 6px; margin-bottom: 15px; border: 1px solid #f0e6c0;">
                <label style="font-weight: bold; font-size: 0.9em; color: #7d6608;">Nudge Message:</label>
                <select id="nudgePreset" onchange="toggleCustomNudge()" style="width: 100%; padding: 8px; margin-top: 5px; border: 1px solid #ddd; border-radius: 4px; font-size: 0.9em;">
                    <option value="preset1">Hey! You started signing up for Remyndrs but didn't finish. Just reply with your first name to continue! Reply STOP to opt out.</option>
                    <option value="preset2">Still interested in Remyndrs? Reply to pick up where you left off. Or reply STOP to opt out.</option>
                    <option value="custom">Custom message...</option>
                </select>
                <textarea id="nudgeCustomMsg" style="display: none; width: 100%; min-height: 60px; padding: 8px; margin-top: 8px; border: 1px solid #ddd; border-radius: 4px; font-size: 0.9em;" placeholder="Type your custom nudge message..."></textarea>
                <div style="margin-top: 10px; display: flex; gap: 8px;">
                    <button class="btn" style="background: #e67e22; color: white; padding: 6px 14px; font-size: 0.85em;" onclick="nudgeAllPending()">Nudge All</button>
                </div>
            </div>

            <div id="pendingOnboardingContent" style="max-height: 50vh; overflow-y: auto;">
                <p style="color: #7f8c8d;">Loading...</p>
            </div>
            <div class="modal-buttons">
                <button class="btn btn-secondary" onclick="document.getElementById('pendingOnboardingModal').classList.remove('active')">Close</button>
            </div>
        </div>
    </div>

    <!-- Flag Conversation Modal -->
    <div class="modal" id="flagModal">
        <div class="modal-content">
            <h3 style="color: #e67e22;">🚩 Flag Conversation</h3>
            <input type="hidden" id="flagLogId">
            <input type="hidden" id="flagPhone">

            <div style="background: #f8f9fa; padding: 10px; border-radius: 4px; margin-bottom: 15px;">
                <div><strong>User:</strong> <span id="flagMsgIn"></span></div>
                <div style="margin-top: 8px;"><strong>System:</strong> <span id="flagMsgOut" style="font-size: 0.9em; color: #666;"></span></div>
            </div>

            <div class="form-group">
                <label for="flagIssueType">Issue Type</label>
                <select id="flagIssueType" style="width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px;">
                    <option value="misunderstood_intent">Misunderstood Intent</option>
                    <option value="poor_response">Poor Response</option>
                    <option value="frustrated_user">Frustrated User</option>
                    <option value="failed_action">Failed Action</option>
                    <option value="confused_user">Confused User</option>
                    <option value="needs_review">Needs Review</option>
                    <option value="other">Other</option>
                </select>
            </div>

            <div class="form-group">
                <label for="flagNotes">Notes (what went wrong?)</label>
                <textarea id="flagNotes" style="width: 100%; min-height: 80px; padding: 10px; border: 1px solid #ddd; border-radius: 4px;" placeholder="Describe the issue..."></textarea>
            </div>

            <div class="modal-buttons">
                <button class="btn btn-secondary" onclick="hideFlagModal()">Cancel</button>
                <button class="btn" style="background: #e67e22; color: white;" onclick="submitFlag()">Flag for Review</button>
            </div>
        </div>
    </div>

    <!-- Recent Messages Modal -->
    <div class="modal" id="recentMessagesModal">
        <div class="modal-content" style="max-width: 700px;">
            <h3 style="color: #9b59b6; margin-bottom: 15px;">Recent User Messages</h3>
            <div id="recentMessagesContent" style="max-height: 60vh; overflow-y: auto;">
                <p style="color: #7f8c8d;">Loading...</p>
            </div>
            <div class="modal-buttons">
                <button class="btn btn-secondary" onclick="hideRecentMessages()">Close</button>
            </div>
        </div>
    </div>

    <!-- Confirmation Modal -->
    <div class="modal" id="confirmModal">
        <div class="modal-content">
            <h3 id="modalTitle">⚠️ Confirm Broadcast</h3>
            <p id="modalSubtitle">You are about to send the following message to <strong id="modalCount">0</strong> users:</p>
            <div id="modalScheduleInfo" style="display: none; background: #e8f4fd; padding: 10px; border-radius: 4px; margin-bottom: 10px; color: #2980b9;">
                📅 Scheduled for: <strong id="modalScheduleTime"></strong>
            </div>
            <div style="background: #f8f9fa; padding: 15px; border-radius: 4px; margin: 15px 0; white-space: pre-wrap;">
                <em id="modalMessage"></em>
            </div>
            <div id="modalExcludedWarning" style="display: none; background: #fef9e7; border: 1px solid #f39c12; padding: 10px; border-radius: 4px; margin-bottom: 10px; font-size: 0.9em;">
            </div>
            <p style="color: #e74c3c;"><strong>This action cannot be undone.</strong></p>
            <div class="modal-buttons">
                <button class="btn btn-secondary" onclick="hideConfirmModal()">Cancel</button>
                <button class="btn btn-danger" id="modalConfirmBtn" onclick="handleBroadcastSubmit()">Send Now</button>
            </div>
        </div>
    </div>

    <!-- Message Viewer Modal -->
    <div class="modal" id="messageViewerModal">
        <div class="modal-content">
            <h3>Full Broadcast Message</h3>
            <div style="background: #f8f9fa; padding: 15px; border-radius: 4px; margin: 15px 0; white-space: pre-wrap; max-height: 400px; overflow-y: auto;">
                <span id="messageViewerText"></span>
            </div>
            <div class="modal-buttons">
                <button class="btn btn-secondary" onclick="hideMessageViewer()">Close</button>
                <button class="btn btn-primary" onclick="copyMessageText()">Copy</button>
            </div>
            <div id="copyConfirmation" style="display: none; color: #27ae60; text-align: center; margin-top: 8px;">Copied!</div>
        </div>
    </div>

    <p class="refresh-note">Refresh page to update metrics</p>

    <script>
        // Collapsible sections
        function toggleSection(sectionId) {{
            const header = document.querySelector(`#${{sectionId}} .section-header`);
            const content = document.querySelector(`#${{sectionId}} .section-content`);

            if (header && content) {{
                header.classList.toggle('collapsed');
                content.classList.toggle('collapsed');

                // Save state to localStorage
                const collapsed = JSON.parse(localStorage.getItem('collapsedSections') || '{{}}');
                collapsed[sectionId] = header.classList.contains('collapsed');
                localStorage.setItem('collapsedSections', JSON.stringify(collapsed));
            }}
        }}

        // Restore collapsed states on page load
        function restoreCollapsedStates() {{
            const collapsed = JSON.parse(localStorage.getItem('collapsedSections') || '{{}}');
            Object.keys(collapsed).forEach(sectionId => {{
                if (collapsed[sectionId]) {{
                    const header = document.querySelector(`#${{sectionId}} .section-header`);
                    const content = document.querySelector(`#${{sectionId}} .section-content`);
                    if (header && content) {{
                        header.classList.add('collapsed');
                        content.classList.add('collapsed');
                    }}
                }}
            }});
        }}

        // Call on page load
        document.addEventListener('DOMContentLoaded', function() {{
            restoreCollapsedStates();
            reloadAllSections();
        }});

        // ===== Date Filter State & Helpers =====
        const LIVE_DATE = '2026-03-01';
        let dateFilterMode = 'live';

        function getDateFilterParams() {{
            if (dateFilterMode === 'beta') {{
                return 'end_date=' + LIVE_DATE;
            }} else {{
                const startDate = document.getElementById('filterStartDate').value || LIVE_DATE;
                return 'start_date=' + startDate;
            }}
        }}

        function appendDateFilter(url) {{
            const params = getDateFilterParams();
            if (!params) return url;
            return url + (url.includes('?') ? '&' : '?') + params;
        }}

        function setDateFilter(mode) {{
            dateFilterMode = mode;
            document.getElementById('filterBetaBtn').classList.toggle('active', mode === 'beta');
            document.getElementById('filterLiveBtn').classList.toggle('active', mode === 'live');
            document.getElementById('filterDateGroup').style.display = mode === 'live' ? 'flex' : 'none';
            updateFilterLabel();
            reloadAllSections();
        }}

        function onDateFilterChange() {{
            updateFilterLabel();
            reloadAllSections();
        }}

        function updateFilterLabel() {{
            const label = document.getElementById('filterRangeLabel');
            if (dateFilterMode === 'beta') {{
                label.textContent = 'Showing: All data before Mar 1, 2026';
            }} else {{
                const startDate = document.getElementById('filterStartDate').value || LIVE_DATE;
                const d = new Date(startDate + 'T00:00:00');
                const formatted = d.toLocaleDateString('en-US', {{ month: 'short', day: 'numeric', year: 'numeric' }});
                label.textContent = 'Showing: ' + formatted + ' to now';
            }}
        }}

        function reloadAllSections() {{
            loadOverviewStats();
            loadHistory();
            loadFeedback();
            loadCostData();
            loadConversations();
            loadFlaggedConversations();
            loadChangelog();
            loadSupportTickets();
            loadContactMessages();
            loadRecurring();
            loadAnalyticsData();
            loadProductMetrics();
        }}

        async function loadOverviewStats() {{
            try {{
                const url = appendDateFilter('/admin/stats/overview');
                const response = await fetch(url);
                const m = await response.json();

                // Update card values
                const el = (id) => document.getElementById(id);
                el('overviewAllUsers').textContent = m.total_users_all_time || 0;
                el('overviewTotalUsers').textContent = m.total_users || 0;
                // Update filtered users label based on view mode
                if (dateFilterMode === 'beta') {{
                    el('overviewFilteredUsersTitle').textContent = 'Beta Users';
                    el('overviewFilteredUsersSub').textContent = 'before launch';
                }} else {{
                    el('overviewFilteredUsersTitle').textContent = 'New Signups';
                    el('overviewFilteredUsersSub').textContent = 'since launch';
                }}
                el('overviewPendingOnboarding').textContent = m.pending_onboarding || 0;
                el('overviewActive7dAll').textContent = m.active_7d_all || 0;
                el('overviewActive7d').textContent = m.active_7d || 0;
                el('overviewActive30dAll').textContent = m.active_30d_all || 0;
                el('overviewActive30d').textContent = m.active_30d || 0;

                const ps = m.premium_stats || {{}};
                el('overviewPremiumCount').textContent = ps.premium || 0;
                el('overviewFreeCount').textContent = ps.free || 0;

                const nu = m.new_users || {{}};
                el('overviewNewToday').textContent = nu.today || 0;
                el('overviewNewWeek').textContent = nu.this_week || 0;
                el('overviewNewMonth').textContent = nu.this_month || 0;

                // Engagement table
                const eng = m.engagement || {{}};
                el('engagementTableBody').innerHTML = `
                    <tr><td>Avg Messages / User</td><td>${{eng.avg_messages_per_user || 0}}</td></tr>
                    <tr><td>Avg Memories / User</td><td>${{eng.avg_memories_per_user || 0}}</td></tr>
                    <tr><td>Avg Reminders / User</td><td>${{eng.avg_reminders_per_user || 0}}</td></tr>
                    <tr><td>Avg Lists / User</td><td>${{eng.avg_lists_per_user || 0}}</td></tr>
                    <tr><td>Avg Items / List</td><td>${{eng.avg_items_per_list || 0}}</td></tr>
                    <tr><td>Total Messages</td><td>${{eng.total_messages || 0}}</td></tr>
                    <tr><td>Total Memories</td><td>${{eng.total_memories || 0}}</td></tr>
                    <tr><td>Total Reminders</td><td>${{eng.total_reminders || 0}}</td></tr>
                    <tr><td>Total Lists</td><td>${{eng.total_lists || 0}}</td></tr>
                `;

                // Reminder table
                const rs = m.reminder_stats || {{}};
                el('reminderTableBody').innerHTML = `
                    <tr><td>Pending</td><td>${{rs.pending || 0}}</td></tr>
                    <tr><td>Sent</td><td>${{rs.sent || 0}}</td></tr>
                    <tr><td>Failed</td><td>${{rs.failed || 0}}</td></tr>
                    <tr><td><strong>Completion Rate</strong></td><td><strong>${{rs.completion_rate || 0}}%</strong></td></tr>
                `;

                // Signup chart
                const signups = (m.daily_signups || []).slice(0, 14);
                const labels = signups.map(s => s[0]);
                const values = signups.map(s => s[1]);
                labels.reverse();
                values.reverse();
                const maxVal = Math.max(...values, 1);
                const chartContainer = el('signupChartContainer');
                if (values.length === 0) {{
                    chartContainer.innerHTML = '<div style="color: #95a5a6; padding: 40px;">No signup data</div>';
                }} else {{
                    chartContainer.innerHTML = '<div class="bar-chart">' + values.map((v, i) => {{
                        const h = Math.max(10, (v / maxVal) * 100);
                        const lbl = labels[i].slice(-5);
                        return `<div class="bar" style="height: ${{h}}%"><span class="bar-value">${{v}}</span><span class="bar-label">${{lbl}}</span></div>`;
                    }}).join('') + '</div>';
                }}

                // Referral table
                const refs = m.referrals || [];
                if (refs.length === 0) {{
                    el('referralTableBody').innerHTML = '<tr><td colspan="2" style="color: #95a5a6;">No referral data</td></tr>';
                }} else {{
                    el('referralTableBody').innerHTML = refs.map(r => `<tr><td>${{r[0]}}</td><td>${{r[1]}}</td></tr>`).join('');
                }}

                // Lifecycle messages table
                const lc = m.lifecycle_messages || {{}};
                const lcKeys = Object.keys(lc);
                if (lcKeys.length === 0) {{
                    el('lifecycleTableBody').innerHTML = '<tr><td colspan="2" style="color: #95a5a6;">No lifecycle data</td></tr>';
                }} else {{
                    el('lifecycleTableBody').innerHTML = lcKeys.map(k => `<tr><td>${{lc[k].label}}</td><td>${{lc[k].count}}</td></tr>`).join('');
                }}
            }} catch (e) {{
                console.error('Error loading overview stats:', e);
            }}
        }}

        // ===== End Date Filter =====

        let audienceStats = {{ all: 0, free: 0, premium: 0 }};
        let currentBroadcastId = null;

        // Load stats on page load
        async function loadStats() {{
            try {{
                const response = await fetch('/admin/broadcast/stats');
                audienceStats = await response.json();
                updatePreview();
            }} catch (e) {{
                console.error('Error loading stats:', e);
            }}
        }}

        // Load broadcast history
        // Store history data for message viewer
        let broadcastHistoryData = [];

        async function loadHistory() {{
            try {{
                const response = await fetch(appendDateFilter('/admin/broadcast/history'));
                const history = await response.json();
                broadcastHistoryData = history;

                const table = document.getElementById('historyTable');
                const loadingRow = document.getElementById('historyLoading');
                if (loadingRow) loadingRow.remove();

                if (history.length === 0) {{
                    const row = table.insertRow(-1);
                    row.innerHTML = '<td colspan="7" style="color: #95a5a6; text-align: center;">No broadcasts yet</td>';
                    return;
                }}

                history.forEach((b, idx) => {{
                    const row = table.insertRow(-1);
                    const date = new Date(b.created_at).toLocaleString();
                    const statusClass = 'status-' + b.status;
                    const sourceLabel = b.source === 'scheduled' ? 'Scheduled' : 'Immediate';
                    const sourceColor = b.source === 'scheduled' ? '#3498db' : '#95a5a6';
                    const escapedMsg = b.message.replace(/"/g, '&quot;');
                    row.innerHTML = `
                        <td>${{date}}</td>
                        <td>${{b.audience}}<br><span style="font-size: 0.8em; color: ${{sourceColor}};">${{sourceLabel}}</span></td>
                        <td class="message-preview" style="cursor: pointer; text-decoration: underline; color: #2980b9;" onclick="showMessageViewer(${{idx}})" title="Click to view full message">${{escapedMsg}}</td>
                        <td>${{b.recipient_count}}</td>
                        <td style="color: #27ae60;">${{b.success_count}}</td>
                        <td style="color: #e74c3c;">${{b.fail_count}}</td>
                        <td><span class="status-badge ${{statusClass}}">${{b.status}}</span></td>
                    `;
                }});
            }} catch (e) {{
                console.error('Error loading history:', e);
            }}
        }}

        function showMessageViewer(idx) {{
            const b = broadcastHistoryData[idx];
            if (!b) return;
            document.getElementById('messageViewerText').textContent = '[Remyndrs System Message] ' + b.full_message;
            document.getElementById('copyConfirmation').style.display = 'none';
            document.getElementById('messageViewerModal').classList.add('active');
        }}

        function hideMessageViewer() {{
            document.getElementById('messageViewerModal').classList.remove('active');
        }}

        function copyMessageText() {{
            const text = document.getElementById('messageViewerText').textContent;
            navigator.clipboard.writeText(text).then(() => {{
                const conf = document.getElementById('copyConfirmation');
                conf.style.display = 'block';
                setTimeout(() => {{ conf.style.display = 'none'; }}, 2000);
            }}).catch(() => {{
                // Fallback for older browsers
                const textarea = document.createElement('textarea');
                textarea.value = text;
                document.body.appendChild(textarea);
                textarea.select();
                document.execCommand('copy');
                document.body.removeChild(textarea);
                const conf = document.getElementById('copyConfirmation');
                conf.style.display = 'block';
                setTimeout(() => {{ conf.style.display = 'none'; }}, 2000);
            }});
        }}

        // Load user feedback
        async function loadFeedback() {{
            try {{
                const response = await fetch(appendDateFilter('/admin/feedback'));
                const feedback = await response.json();

                const openTable = document.getElementById('openFeedbackTable');
                const resolvedTable = document.getElementById('resolvedFeedbackTable');
                const loadingRow = document.getElementById('openFeedbackLoading');
                if (loadingRow) loadingRow.remove();

                // Separate into open and resolved
                const openFeedback = feedback.filter(f => !f.resolved);
                const resolvedFeedback = feedback.filter(f => f.resolved);

                // Update counts
                document.getElementById('openFeedbackCount').textContent = `(${{openFeedback.length}})`;
                document.getElementById('resolvedFeedbackCount').textContent = `(${{resolvedFeedback.length}})`;

                // Render open feedback
                if (openFeedback.length === 0) {{
                    const row = openTable.insertRow(-1);
                    row.id = 'noOpenFeedback';
                    row.innerHTML = '<td colspan="4" style="color: #95a5a6; text-align: center;">No open feedback</td>';
                }} else {{
                    openFeedback.forEach(f => {{
                        const row = openTable.insertRow(-1);
                        renderFeedbackRow(row, f);
                    }});
                }}

                // Render resolved feedback
                if (resolvedFeedback.length === 0) {{
                    const row = resolvedTable.insertRow(-1);
                    row.id = 'noResolvedFeedback';
                    row.innerHTML = '<td colspan="4" style="color: #95a5a6; text-align: center;">No resolved feedback</td>';
                }} else {{
                    resolvedFeedback.forEach(f => {{
                        const row = resolvedTable.insertRow(-1);
                        renderFeedbackRow(row, f);
                    }});
                }}
            }} catch (e) {{
                console.error('Error loading feedback:', e);
            }}
        }}

        function renderFeedbackRow(row, f) {{
            const date = new Date(f.created_at).toLocaleString();
            const resolvedClass = f.resolved ? '' : 'unresolved';
            const checkedAttr = f.resolved ? 'checked' : '';
            row.className = resolvedClass;
            row.id = `feedback-row-${{f.id}}`;
            row.setAttribute('data-id', f.id);
            row.setAttribute('data-resolved', f.resolved);
            row.innerHTML = `
                <td>${{date}}</td>
                <td>${{f.user_phone}}</td>
                <td class="feedback-message">${{f.message}}</td>
                <td style="text-align: center;">
                    <input type="checkbox" class="resolve-checkbox" ${{checkedAttr}}
                           onchange="toggleResolved(${{f.id}}, this.checked)"
                           title="${{f.resolved ? 'Mark as unresolved' : 'Mark as resolved'}}">
                </td>
            `;
        }}

        // Toggle resolved section visibility
        function toggleResolvedSection() {{
            const section = document.getElementById('resolvedFeedbackSection');
            const icon = document.getElementById('resolvedToggleIcon');
            if (section.style.display === 'none') {{
                section.style.display = 'block';
                icon.textContent = '▼';
            }} else {{
                section.style.display = 'none';
                icon.textContent = '▶';
            }}
        }}

        // Toggle feedback resolved status
        async function toggleResolved(feedbackId, isChecked) {{
            try {{
                const response = await fetch(`/admin/feedback/${{feedbackId}}/toggle`, {{
                    method: 'POST'
                }});

                if (response.ok) {{
                    const result = await response.json();
                    const row = document.getElementById(`feedback-row-${{feedbackId}}`);

                    // Move row to appropriate table
                    const openTable = document.getElementById('openFeedbackTable');
                    const resolvedTable = document.getElementById('resolvedFeedbackTable');

                    // Remove "no feedback" placeholders if they exist
                    const noOpen = document.getElementById('noOpenFeedback');
                    const noResolved = document.getElementById('noResolvedFeedback');

                    if (result.resolved) {{
                        // Move to resolved table
                        row.classList.remove('unresolved');
                        row.setAttribute('data-resolved', 'true');
                        if (noResolved) noResolved.remove();
                        resolvedTable.appendChild(row);

                        // Check if open table is now empty (excluding header)
                        if (openTable.rows.length === 1) {{
                            const emptyRow = openTable.insertRow(-1);
                            emptyRow.id = 'noOpenFeedback';
                            emptyRow.innerHTML = '<td colspan="4" style="color: #95a5a6; text-align: center;">No open feedback</td>';
                        }}
                    }} else {{
                        // Move to open table
                        row.classList.add('unresolved');
                        row.setAttribute('data-resolved', 'false');
                        if (noOpen) noOpen.remove();
                        openTable.appendChild(row);

                        // Check if resolved table is now empty (excluding header)
                        if (resolvedTable.rows.length === 1) {{
                            const emptyRow = resolvedTable.insertRow(-1);
                            emptyRow.id = 'noResolvedFeedback';
                            emptyRow.innerHTML = '<td colspan="4" style="color: #95a5a6; text-align: center;">No resolved feedback</td>';
                        }}
                    }}

                    // Update counts
                    const openCount = openTable.querySelectorAll('tr[data-id]').length;
                    const resolvedCount = resolvedTable.querySelectorAll('tr[data-id]').length;
                    document.getElementById('openFeedbackCount').textContent = `(${{openCount}})`;
                    document.getElementById('resolvedFeedbackCount').textContent = `(${{resolvedCount}})`;

                }} else {{
                    // Revert checkbox on error
                    const checkbox = document.querySelector(`#feedback-row-${{feedbackId}} .resolve-checkbox`);
                    checkbox.checked = !isChecked;
                    alert('Error updating feedback status');
                }}
            }} catch (e) {{
                console.error('Error toggling feedback:', e);
                // Revert checkbox on error
                const checkbox = document.querySelector(`#feedback-row-${{feedbackId}} .resolve-checkbox`);
                checkbox.checked = !isChecked;
            }}
        }}

        // Cost Analytics
        let costData = {{}};
        let currentPeriod = 'day';

        async function loadCostData() {{
            try {{
                const response = await fetch(appendDateFilter('/admin/costs'));
                costData = await response.json();
                renderCostTable(currentPeriod);
            }} catch (e) {{
                console.error('Error loading cost data:', e);
                const loadingRow = document.getElementById('costLoading');
                if (loadingRow) {{
                    loadingRow.innerHTML = '<td colspan="8" style="color: #e74c3c; text-align: center;">Error loading cost data</td>';
                }}
            }}
        }}

        function showCostPeriod(period) {{
            currentPeriod = period;
            // Update tab styles
            document.querySelectorAll('.period-tab').forEach(tab => tab.classList.remove('active'));
            event.target.classList.add('active');
            renderCostTable(period);
        }}

        function renderCostTable(period) {{
            const table = document.getElementById('costTable');
            const loadingRow = document.getElementById('costLoading');
            if (loadingRow) loadingRow.remove();

            // Remove existing data rows (keep header)
            while (table.rows.length > 1) {{
                table.deleteRow(1);
            }}

            const periodData = costData[period];
            if (!periodData) {{
                const row = table.insertRow(-1);
                row.innerHTML = '<td colspan="8" style="color: #95a5a6; text-align: center;">No cost data available</td>';
                return;
            }}

            // Add rows for each plan (including trial)
            const planLabels = {{
                'free': 'Free',
                'trial': 'Premium (Trial)',
                'premium': 'Premium (Paid)',
                'family': 'Family'
            }};
            ['free', 'trial', 'premium', 'family'].forEach(plan => {{
                const data = periodData[plan];
                if (data && (data.user_count > 0 || data.message_count > 0)) {{
                    const row = table.insertRow(-1);
                    row.className = 'plan-row';
                    if (plan === 'trial') row.style.backgroundColor = '#fff8e1';
                    const totalTokens = (data.prompt_tokens || 0) + (data.completion_tokens || 0);
                    row.innerHTML = `
                        <td>${{planLabels[plan]}}</td>
                        <td>${{data.user_count}}</td>
                        <td>${{data.message_count * 2}}</td>
                        <td class="money">${{formatCurrency(data.sms_cost)}}</td>
                        <td>${{totalTokens.toLocaleString()}}</td>
                        <td class="money">${{formatCurrency(data.ai_cost)}}</td>
                        <td class="money">${{formatCurrency(data.total_cost)}}</td>
                        <td class="money">${{formatCurrency(data.cost_per_user)}}</td>
                    `;
                }}
            }});

            // Add total row
            const total = periodData['total'];
            if (total) {{
                const row = table.insertRow(-1);
                row.className = 'total-row';
                row.innerHTML = `
                    <td><strong>Total</strong></td>
                    <td><strong>${{total.user_count}}</strong></td>
                    <td><strong>-</strong></td>
                    <td class="money"><strong>${{formatCurrency(total.sms_cost)}}</strong></td>
                    <td><strong>-</strong></td>
                    <td class="money"><strong>${{formatCurrency(total.ai_cost)}}</strong></td>
                    <td class="money"><strong>${{formatCurrency(total.total_cost)}}</strong></td>
                    <td class="money"><strong>${{formatCurrency(total.cost_per_user)}}</strong></td>
                `;
            }}

            // Render Twilio actual vs estimated summary
            renderTwilioActualSummary(period, total);
        }}

        function renderTwilioActualSummary(period, totalEstimated) {{
            const container = document.getElementById('twilioActualSummary');
            const twilioData = costData.twilio_actual;

            if (!twilioData || Object.keys(twilioData).length === 0) {{
                container.style.display = 'block';
                container.innerHTML = '<span style="color: #95a5a6;">No Twilio data yet \u2014 actual costs are polled daily at 6:30 AM UTC.</span>';
                return;
            }}

            // Map period tabs to twilio_actual keys (hour has no Twilio data)
            const periodMap = {{ 'day': 'day', 'week': 'week', 'month': 'month' }};
            const actualPeriod = periodMap[period];
            if (!actualPeriod || !twilioData[actualPeriod]) {{
                container.style.display = 'block';
                container.innerHTML = '<span style="color: #95a5a6;">Twilio actual costs not available for this period.</span>';
                return;
            }}

            const actual = twilioData[actualPeriod];
            const estimatedSms = totalEstimated ? totalEstimated.sms_cost : 0;
            const actualSms = actual.total_cost;
            const diff = actualSms - estimatedSms;
            const diffColor = diff <= 0 ? '#27ae60' : '#e74c3c';
            const diffSign = diff <= 0 ? '' : '+';

            const failedCount = actual.failed_count || 0;
            const failedCost = actual.failed_cost || 0;
            const deliveredCount = actual.outbound_count - failedCount;
            const deliveredCost = actual.outbound_cost - failedCost;
            const wastePercent = actual.outbound_count > 0 ? ((failedCount / actual.outbound_count) * 100).toFixed(1) : 0;

            container.style.display = 'block';
            container.innerHTML = `
                <div style="display: flex; gap: 25px; align-items: center; flex-wrap: wrap;">
                    <span><strong>SMS Estimated:</strong> ${{formatCurrency(estimatedSms)}}</span>
                    <span><strong>SMS Actual (Twilio):</strong> ${{formatCurrency(actualSms)}}</span>
                    <span style="color: ${{diffColor}}; font-weight: 600;">Difference: ${{diffSign}}${{formatCurrency(Math.abs(diff))}}</span>
                    <span style="color: #95a5a6; font-size: 0.85em;">(${{actual.days_with_data}} day${{actual.days_with_data !== 1 ? 's' : ''}} of data)</span>
                </div>
                <div style="margin-top: 6px; font-size: 0.85em; color: #7f8c8d;">
                    Inbound: ${{actual.inbound_count}} msgs / ${{formatCurrency(actual.inbound_cost)}} |
                    Outbound: ${{actual.outbound_count}} msgs / ${{formatCurrency(actual.outbound_cost)}}
                </div>
                ${{failedCount > 0 ? `
                <div style="margin-top: 8px; padding: 8px 12px; background: #fdf2f2; border-left: 3px solid #e74c3c; border-radius: 4px; font-size: 0.85em;">
                    <strong style="color: #e74c3c;">Failed/Undelivered:</strong>
                    <span style="color: #c0392b;">${{failedCount}} msgs / ${{formatCurrency(failedCost)}} wasted (${{wastePercent}}% of outbound)</span>
                    <span style="margin-left: 10px; color: #27ae60;">Delivered: ${{deliveredCount}} msgs / ${{formatCurrency(deliveredCost)}}</span>
                </div>` : `
                <div style="margin-top: 8px; padding: 8px 12px; background: #f0faf0; border-left: 3px solid #27ae60; border-radius: 4px; font-size: 0.85em;">
                    <strong style="color: #27ae60;">No failed messages</strong> — all outbound messages delivered successfully.
                </div>`}}
            `;
        }}

        function formatCurrency(value) {{
            if (value === 0) return '$0.00';
            if (value < 0.01 && value > 0) return '<$0.01';
            return '$' + value.toFixed(2);
        }}

        let lastPreviewData = null;

        function escapeHtml(str) {{
            if (!str) return '';
            const div = document.createElement('div');
            div.textContent = str;
            return div.innerHTML;
        }}

        async function loadRecipientsPreview() {{
            const audience = document.getElementById('audience').value;
            if (audience === 'single') return;

            const btn = document.getElementById('previewRecipientsBtn');
            const loading = document.getElementById('previewLoading');
            btn.disabled = true;
            loading.style.display = 'inline';

            try {{
                const response = await fetch(`/admin/broadcast/recipients-preview?audience=${{audience}}`);
                if (!response.ok) throw new Error('Failed to fetch preview');
                const data = await response.json();
                lastPreviewData = data;
                renderRecipientsPreview(data);
            }} catch (e) {{
                console.error('Error loading recipients preview:', e);
                document.getElementById('recipientsPreviewPanel').style.display = 'none';
            }} finally {{
                btn.disabled = false;
                loading.style.display = 'none';
            }}
        }}

        function renderRecipientsPreview(data) {{
            const panel = document.getElementById('recipientsPreviewPanel');
            panel.style.display = 'block';

            const summary = data.summary;
            const excludedTotal = summary.excluded_opted_out + summary.excluded_outside_window;
            document.getElementById('previewSummaryLine').innerHTML =
                `<span style="color: #27ae60;">${{summary.included}} will receive</span>` +
                (excludedTotal > 0 ? ` &nbsp;|&nbsp; <span style="color: #e74c3c;">${{excludedTotal}} excluded</span>` : '') +
                ` &nbsp;|&nbsp; <span style="color: #7f8c8d;">${{summary.total_onboarded}} total onboarded</span>`;

            document.getElementById('includedCount').textContent = `(${{data.included.length}})`;
            document.getElementById('excludedCount').textContent = `(${{data.excluded.length}})`;

            // Render included table
            const includedBody = document.getElementById('includedTableBody');
            includedBody.innerHTML = '';
            if (data.included.length === 0) {{
                includedBody.innerHTML = '<tr><td colspan="5" style="padding: 8px; color: #95a5a6; text-align: center;">No users in window</td></tr>';
            }} else {{
                data.included.forEach(u => {{
                    const row = document.createElement('tr');
                    row.innerHTML = `
                        <td style="padding: 6px 10px; border-bottom: 1px solid #eee;">${{escapeHtml(u.phone)}}</td>
                        <td style="padding: 6px 10px; border-bottom: 1px solid #eee;">${{escapeHtml(u.name) || '<span style="color:#95a5a6">-</span>'}}</td>
                        <td style="padding: 6px 10px; border-bottom: 1px solid #eee;"><span style="background: ${{u.tier === 'premium' ? '#f39c12' : '#3498db'}}; color: white; padding: 2px 6px; border-radius: 3px; font-size: 0.8em;">${{u.tier}}</span></td>
                        <td style="padding: 6px 10px; border-bottom: 1px solid #eee; font-size: 0.85em;">${{escapeHtml(u.timezone)}}</td>
                        <td style="padding: 6px 10px; border-bottom: 1px solid #eee;">${{escapeHtml(u.local_time)}}</td>
                    `;
                    includedBody.appendChild(row);
                }});
            }}

            // Render excluded table
            const excludedBody = document.getElementById('excludedTableBody');
            excludedBody.innerHTML = '';
            const excludedSection = document.getElementById('excludedSection');
            if (data.excluded.length === 0) {{
                excludedSection.style.display = 'none';
            }} else {{
                excludedSection.style.display = 'block';
                data.excluded.forEach(u => {{
                    const row = document.createElement('tr');
                    const reasonLabel = u.reason === 'opted_out'
                        ? '<span style="background: #e74c3c; color: white; padding: 2px 6px; border-radius: 3px; font-size: 0.8em;">Opted Out</span>'
                        : '<span style="background: #e67e22; color: white; padding: 2px 6px; border-radius: 3px; font-size: 0.8em;">Outside Window</span>';
                    const clearBtn = u.reason === 'opted_out' && u.phone_full
                        ? `<button onclick="clearOptedOutFromPreview('${{u.phone_full}}')" style="background: #e74c3c; color: white; border: none; padding: 3px 8px; border-radius: 3px; font-size: 0.8em; cursor: pointer;">Clear</button>`
                        : '';
                    row.innerHTML = `
                        <td style="padding: 6px 10px; border-bottom: 1px solid #eee;">${{escapeHtml(u.phone)}}</td>
                        <td style="padding: 6px 10px; border-bottom: 1px solid #eee;">${{escapeHtml(u.name) || '<span style="color:#95a5a6">-</span>'}}</td>
                        <td style="padding: 6px 10px; border-bottom: 1px solid #eee;">${{reasonLabel}}</td>
                        <td style="padding: 6px 10px; border-bottom: 1px solid #eee; font-size: 0.85em;">${{escapeHtml(u.timezone)}}</td>
                        <td style="padding: 6px 10px; border-bottom: 1px solid #eee;">${{escapeHtml(u.local_time)}}</td>
                        <td style="padding: 6px 10px; border-bottom: 1px solid #eee;">${{clearBtn}}</td>
                    `;
                    excludedBody.appendChild(row);
                }});
            }}
        }}

        function togglePreviewSection(sectionId) {{
            const section = document.getElementById(sectionId);
            section.style.display = section.style.display === 'none' ? 'block' : 'none';
        }}

        async function clearOptedOutFromPreview(phone) {{
            if (!confirm('Clear the opted-out flag for this user? They will be included in future broadcasts.')) return;
            try {{
                const response = await fetch(`/admin/cs/customer/${{encodeURIComponent(phone)}}/clear-opted-out`, {{
                    method: 'POST'
                }});
                if (!response.ok) throw new Error('Failed to clear flag');
                // Refresh the preview and stats
                await loadStats();
                await loadRecipientsPreview();
            }} catch (e) {{
                alert('Error clearing opted-out flag: ' + e.message);
            }}
        }}

        function updatePreview() {{
            const audience = document.getElementById('audience').value;
            const message = document.getElementById('message').value;
            const isSingle = audience === 'single';

            // Show/hide single phone input
            document.getElementById('singlePhoneGroup').style.display = isSingle ? 'block' : 'none';

            // Show/hide preview recipients button; hide panel on audience change
            document.getElementById('previewBtnContainer').style.display = isSingle ? 'none' : 'block';
            document.getElementById('recipientsPreviewPanel').style.display = 'none';
            lastPreviewData = null;

            // Update character count
            document.getElementById('charCount').textContent = message.length;

            // Update message preview
            const preview = document.getElementById('messagePreview');
            if (message.trim()) {{
                preview.textContent = message;
                preview.style.color = '#2c3e50';
                preview.style.fontStyle = 'normal';
            }} else {{
                preview.textContent = 'Your message will appear here...';
                preview.style.color = '#7f8c8d';
                preview.style.fontStyle = 'italic';
            }}

            const outsideInfo = document.getElementById('outsideWindowInfo');

            if (isSingle) {{
                // Single number mode
                const phone = document.getElementById('singlePhone').value.trim();
                const digits = phone.replace(/\\D/g, '');
                const validPhone = digits.length === 10 || (digits.length === 11 && digits.startsWith('1'));
                document.getElementById('recipientCount').textContent = validPhone ? '1' : '0';
                outsideInfo.textContent = validPhone ? '(single number test)' : '(enter a valid US phone number)';
            }} else {{
                // Update recipient count (use timezone-aware counts)
                const inWindowCount = audienceStats[audience + '_in_window'] || 0;
                const totalCount = audienceStats[audience] || 0;
                const outsideCount = totalCount - inWindowCount;

                document.getElementById('recipientCount').textContent = inWindowCount;

                // Show outside window info
                if (outsideCount > 0) {{
                    outsideInfo.textContent = `(${{outsideCount}} outside window, won't receive)`;
                }} else {{
                    outsideInfo.textContent = '';
                }}
            }}

            // Enable/disable send button
            const sendBtn = document.getElementById('sendBtn');
            const isScheduled = document.getElementById('scheduleCheckbox').checked;
            const hasMessage = message.trim().length > 0;

            if (isSingle) {{
                const phone = document.getElementById('singlePhone').value.trim();
                const digits = phone.replace(/\\D/g, '');
                const validPhone = digits.length === 10 || (digits.length === 11 && digits.startsWith('1'));
                sendBtn.disabled = !hasMessage || !validPhone;
            }} else if (isScheduled) {{
                // For scheduled: only need a message
                sendBtn.disabled = !hasMessage;
            }} else {{
                // For immediate: need message AND users in window
                const inWindowCount = audienceStats[audience + '_in_window'] || 0;
                sendBtn.disabled = !hasMessage || inWindowCount === 0;
            }}
        }}

        function showConfirmModal() {{
            const audience = document.getElementById('audience').value;
            const message = document.getElementById('message').value;
            const inWindowCount = audienceStats[audience + '_in_window'] || 0;
            const isScheduled = document.getElementById('scheduleCheckbox').checked;
            const scheduleDate = document.getElementById('scheduleDate').value;
            const isSingle = audience === 'single';

            // Validate scheduled date if scheduling
            if (isScheduled) {{
                if (!validateScheduleDate()) {{
                    return; // Don't show modal if date is invalid
                }}
            }}

            document.getElementById('modalMessage').textContent = '[Remyndrs System Message] ' + message;

            const scheduleInfo = document.getElementById('modalScheduleInfo');
            const modalTitle = document.getElementById('modalTitle');
            const modalSubtitle = document.getElementById('modalSubtitle');
            const confirmBtn = document.getElementById('modalConfirmBtn');

            if (isSingle) {{
                const phone = document.getElementById('singlePhone').value.trim();
                document.getElementById('modalCount').textContent = '1';
                scheduleInfo.style.display = 'none';
                if (isScheduled && scheduleDate) {{
                    const scheduledTime = new Date(scheduleDate).toLocaleString();
                    scheduleInfo.style.display = 'block';
                    document.getElementById('modalScheduleTime').textContent = scheduledTime;
                    modalTitle.textContent = '📅 Schedule Test Message';
                    modalSubtitle.innerHTML = 'This message will be sent to <strong>' + phone + '</strong> at the scheduled time:';
                    confirmBtn.textContent = 'Schedule';
                    confirmBtn.style.background = '#3498db';
                }} else {{
                    modalTitle.textContent = '📱 Confirm Test Send';
                    modalSubtitle.innerHTML = 'Send this message to <strong>' + phone + '</strong>:';
                    confirmBtn.textContent = 'Send Test';
                    confirmBtn.style.background = '#27ae60';
                }}
            }} else if (isScheduled && scheduleDate) {{
                document.getElementById('modalCount').textContent = inWindowCount;
                const scheduledTime = new Date(scheduleDate).toLocaleString();
                scheduleInfo.style.display = 'block';
                document.getElementById('modalScheduleTime').textContent = scheduledTime;
                modalTitle.textContent = '📅 Confirm Scheduled Broadcast';
                modalSubtitle.innerHTML = 'This message will be sent to users in the 8am-8pm window at the scheduled time:';
                confirmBtn.textContent = 'Schedule';
                confirmBtn.style.background = '#3498db';
            }} else {{
                document.getElementById('modalCount').textContent = inWindowCount;
                scheduleInfo.style.display = 'none';
                modalTitle.textContent = '⚠️ Confirm Broadcast';
                modalSubtitle.innerHTML = 'You are about to send the following message to <strong>' + inWindowCount + '</strong> users:';
                confirmBtn.textContent = 'Send Now';
                confirmBtn.style.background = '#e74c3c';
            }}

            // Show excluded warning if preview data available
            const excludedWarning = document.getElementById('modalExcludedWarning');
            if (!isSingle && lastPreviewData && lastPreviewData.summary) {{
                const s = lastPreviewData.summary;
                const excludedTotal = s.excluded_opted_out + s.excluded_outside_window;
                if (excludedTotal > 0) {{
                    let parts = [];
                    if (s.excluded_opted_out > 0) parts.push(s.excluded_opted_out + ' opted out');
                    if (s.excluded_outside_window > 0) parts.push(s.excluded_outside_window + ' outside time window');
                    excludedWarning.innerHTML = `⚠️ <strong>${{excludedTotal}} user${{excludedTotal !== 1 ? 's' : ''}} excluded:</strong> ${{parts.join(', ')}}`;
                    excludedWarning.style.display = 'block';
                }} else {{
                    excludedWarning.style.display = 'none';
                }}
            }} else {{
                excludedWarning.style.display = 'none';
            }}

            document.getElementById('confirmModal').classList.add('active');
        }}

        function hideConfirmModal() {{
            document.getElementById('confirmModal').classList.remove('active');
        }}

        async function showRecentMessages() {{
            document.getElementById('recentMessagesModal').classList.add('active');
            document.getElementById('recentMessagesContent').innerHTML = '<p style="color: #7f8c8d;">Loading...</p>';

            try {{
                const response = await fetch('/admin/recent-messages');
                if (!response.ok) throw new Error('Failed to fetch');
                const messages = await response.json();

                if (messages.length === 0) {{
                    document.getElementById('recentMessagesContent').innerHTML = '<p style="color: #7f8c8d;">No messages found.</p>';
                    return;
                }}

                let html = '<table style="width: 100%; border-collapse: collapse;">';
                html += '<tr style="background: #f8f9fa;"><th style="padding: 10px; text-align: left; border-bottom: 2px solid #ddd;">User</th><th style="padding: 10px; text-align: left; border-bottom: 2px solid #ddd;">Message</th><th style="padding: 10px; text-align: left; border-bottom: 2px solid #ddd;">Intent</th><th style="padding: 10px; text-align: left; border-bottom: 2px solid #ddd;">Time</th></tr>';

                messages.forEach(m => {{
                    const time = m.created_at ? new Date(m.created_at).toLocaleString() : 'Unknown';
                    const intent = m.intent || '-';
                    const msgPreview = m.message.length > 80 ? m.message.substring(0, 80) + '...' : m.message;
                    html += `<tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px; vertical-align: top;"><strong>${{m.first_name}}</strong><br><span style="color: #7f8c8d; font-size: 0.85em;">...${{m.phone_number}}</span></td>
                        <td style="padding: 10px; vertical-align: top;">${{msgPreview}}</td>
                        <td style="padding: 10px; vertical-align: top;"><span style="background: #e8f4fd; padding: 2px 6px; border-radius: 3px; font-size: 0.85em;">${{intent}}</span></td>
                        <td style="padding: 10px; vertical-align: top; font-size: 0.85em; color: #7f8c8d; white-space: nowrap;">${{time}}</td>
                    </tr>`;
                }});

                html += '</table>';
                document.getElementById('recentMessagesContent').innerHTML = html;
            }} catch (e) {{
                document.getElementById('recentMessagesContent').innerHTML = '<p style="color: #e74c3c;">Error loading messages.</p>';
            }}
        }}

        function hideRecentMessages() {{
            document.getElementById('recentMessagesModal').classList.remove('active');
        }}

        async function sendBroadcast() {{
            hideConfirmModal();

            const audience = document.getElementById('audience').value;
            const message = document.getElementById('message').value;
            const sendBtn = document.getElementById('sendBtn');
            const progressInfo = document.getElementById('progressInfo');
            const progressText = document.getElementById('progressText');

            sendBtn.disabled = true;
            sendBtn.textContent = 'Sending...';
            progressInfo.classList.add('active');
            progressText.textContent = 'Starting broadcast...';

            try {{
                const response = await fetch('/admin/broadcast/send', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json'
                    }},
                    body: JSON.stringify({{
                        message,
                        audience,
                        phone_number: audience === 'single' ? document.getElementById('singlePhone').value.trim() : undefined
                    }})
                }});

                const result = await response.json();

                if (response.ok) {{
                    currentBroadcastId = result.broadcast_id;
                    progressText.textContent = `Broadcast started! Sending to ${{result.recipient_count}} recipients...`;

                    // Poll for status updates
                    pollBroadcastStatus(result.broadcast_id);
                }} else {{
                    progressText.textContent = `Error: ${{result.detail || 'Unknown error'}}`;
                    sendBtn.disabled = false;
                    sendBtn.textContent = 'Send Broadcast';
                }}
            }} catch (e) {{
                progressText.textContent = `Error: ${{e.message}}`;
                sendBtn.disabled = false;
                sendBtn.textContent = 'Send Broadcast';
            }}
        }}

        async function pollBroadcastStatus(broadcastId) {{
            const progressText = document.getElementById('progressText');
            const sendBtn = document.getElementById('sendBtn');
            const progressInfo = document.getElementById('progressInfo');

            try {{
                const response = await fetch(`/admin/broadcast/status/${{broadcastId}}`);
                const status = await response.json();

                progressText.textContent = `Status: ${{status.status}} | Success: ${{status.success_count}} | Failed: ${{status.fail_count}}`;

                if (status.status === 'sending' || status.status === 'pending') {{
                    // Continue polling
                    setTimeout(() => pollBroadcastStatus(broadcastId), 2000);
                }} else {{
                    // Completed or failed
                    sendBtn.disabled = false;
                    sendBtn.textContent = 'Send Broadcast';
                    document.getElementById('message').value = '';
                    updatePreview();

                    if (status.status === 'completed') {{
                        progressText.innerHTML = `<span style="color: #27ae60;">✅ Broadcast completed! ${{status.success_count}} sent, ${{status.fail_count}} failed.</span>`;
                    }} else {{
                        progressText.innerHTML = `<span style="color: #e74c3c;">❌ Broadcast failed.</span>`;
                    }}

                    // Reload history
                    setTimeout(() => {{
                        location.reload();
                    }}, 3000);
                }}
            }} catch (e) {{
                console.error('Error polling status:', e);
                setTimeout(() => pollBroadcastStatus(broadcastId), 5000);
            }}
        }}

        // Maintenance Message Functions
        async function loadMaintenanceMessage() {{
            try {{
                const response = await fetch('/admin/settings/maintenance-message');
                const data = await response.json();
                document.getElementById('maintenanceMessage').value = data.message;
                if (data.is_default) {{
                    document.getElementById('maintenanceStatus').innerHTML = '<span style="color: #7f8c8d;">Using default message</span>';
                }} else {{
                    document.getElementById('maintenanceStatus').innerHTML = '<span style="color: #27ae60;">Custom message saved</span>';
                }}
            }} catch (e) {{
                console.error('Error loading maintenance message:', e);
            }}
        }}

        async function saveMaintenanceMessage() {{
            const message = document.getElementById('maintenanceMessage').value.trim();
            const statusDiv = document.getElementById('maintenanceStatus');

            try {{
                const response = await fetch('/admin/settings/maintenance-message', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ message }})
                }});

                const data = await response.json();
                if (data.success) {{
                    if (data.reset_to_default) {{
                        statusDiv.innerHTML = '<span style="color: #27ae60;">Reset to default message</span>';
                    }} else {{
                        statusDiv.innerHTML = '<span style="color: #27ae60;">Message saved successfully!</span>';
                    }}
                }} else {{
                    statusDiv.innerHTML = '<span style="color: #e74c3c;">Error saving message</span>';
                }}
            }} catch (e) {{
                console.error('Error saving maintenance message:', e);
                statusDiv.innerHTML = '<span style="color: #e74c3c;">Error: ' + e.message + '</span>';
            }}
        }}

        async function resetMaintenanceMessage() {{
            if (!confirm('Reset to the default maintenance message?')) return;

            const statusDiv = document.getElementById('maintenanceStatus');
            try {{
                const response = await fetch('/admin/settings/maintenance-message', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ message: '' }})
                }});

                const data = await response.json();
                if (data.success) {{
                    document.getElementById('maintenanceMessage').value = data.message;
                    statusDiv.innerHTML = '<span style="color: #27ae60;">Reset to default message</span>';
                }}
            }} catch (e) {{
                console.error('Error resetting maintenance message:', e);
                statusDiv.innerHTML = '<span style="color: #e74c3c;">Error: ' + e.message + '</span>';
            }}
        }}

        // =====================================================
        // STAGING FALLBACK FUNCTIONS
        // =====================================================

        async function loadStagingFallback() {{
            try {{
                const response = await fetch('/admin/settings/staging-fallback');
                const data = await response.json();

                document.getElementById('stagingFallbackEnabled').checked = data.enabled;
                document.getElementById('stagingFallbackNumbers').value = data.numbers;
                updateStagingFallbackStatus(data.enabled);
            }} catch (e) {{
                console.error('Error loading staging fallback settings:', e);
            }}
        }}

        function updateStagingFallbackStatus(enabled) {{
            const statusEl = document.getElementById('stagingFallbackStatus');
            if (enabled) {{
                statusEl.textContent = 'Active';
                statusEl.style.background = '#d4edda';
                statusEl.style.color = '#155724';
            }} else {{
                statusEl.textContent = 'Disabled';
                statusEl.style.background = '#f8d7da';
                statusEl.style.color = '#721c24';
            }}
        }}

        async function updateStagingFallback() {{
            const enabled = document.getElementById('stagingFallbackEnabled').checked;
            const numbers = document.getElementById('stagingFallbackNumbers').value.trim();
            const statusEl = document.getElementById('stagingFallbackSaveStatus');

            try {{
                const response = await fetch('/admin/settings/staging-fallback', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ enabled, numbers }})
                }});

                const data = await response.json();
                if (data.success) {{
                    updateStagingFallbackStatus(data.enabled);
                    statusEl.textContent = 'Settings saved!';
                    statusEl.style.color = '#27ae60';
                    setTimeout(() => {{ statusEl.textContent = ''; }}, 3000);
                }} else {{
                    statusEl.textContent = 'Error saving settings';
                    statusEl.style.color = '#e74c3c';
                }}
            }} catch (e) {{
                console.error('Error updating staging fallback:', e);
                statusEl.textContent = 'Error: ' + e.message;
                statusEl.style.color = '#e74c3c';
            }}
        }}

        // Scheduled Broadcast Functions
        function validateScheduleDate() {{
            const scheduleDate = document.getElementById('scheduleDate').value;
            const errorEl = document.getElementById('scheduleDateError');
            const hintEl = document.getElementById('scheduleDateHint');
            const sendBtn = document.getElementById('sendBtn');

            if (!scheduleDate) {{
                errorEl.textContent = 'Please select a date and time';
                errorEl.style.display = 'block';
                hintEl.style.display = 'none';
                sendBtn.disabled = true;
                return false;
            }}

            const selectedDate = new Date(scheduleDate);
            if (isNaN(selectedDate.getTime())) {{
                errorEl.textContent = 'Invalid date format';
                errorEl.style.display = 'block';
                hintEl.style.display = 'none';
                sendBtn.disabled = true;
                return false;
            }}

            if (selectedDate <= new Date()) {{
                errorEl.textContent = 'Scheduled time must be in the future';
                errorEl.style.display = 'block';
                hintEl.style.display = 'none';
                sendBtn.disabled = true;
                return false;
            }}

            errorEl.style.display = 'none';
            hintEl.style.display = 'block';
            sendBtn.disabled = !document.getElementById('message').value.trim();
            return true;
        }}

        function toggleScheduleMode() {{
            const checkbox = document.getElementById('scheduleCheckbox');
            const dateGroup = document.getElementById('scheduleDateGroup');
            const sendBtn = document.getElementById('sendBtn');

            if (checkbox.checked) {{
                dateGroup.style.display = 'block';
                sendBtn.textContent = 'Schedule';
                // Set default to tomorrow at 10am
                const tomorrow = new Date();
                tomorrow.setDate(tomorrow.getDate() + 1);
                tomorrow.setHours(10, 0, 0, 0);
                document.getElementById('scheduleDate').value = tomorrow.toISOString().slice(0, 16);
            }} else {{
                dateGroup.style.display = 'none';
                sendBtn.textContent = 'Send Now';
            }}
            // Update button enabled state
            updatePreview();
        }}

        async function loadScheduledBroadcasts() {{
            try {{
                const response = await fetch('/admin/broadcast/scheduled');
                const broadcasts = await response.json();
                const table = document.getElementById('scheduledTable');
                const loadingRow = document.getElementById('scheduledLoading');

                if (loadingRow) loadingRow.remove();

                // Remove existing rows except header
                while (table.rows.length > 1) {{
                    table.deleteRow(1);
                }}

                if (broadcasts.length === 0) {{
                    const row = table.insertRow();
                    row.innerHTML = '<td colspan="5" style="color: #95a5a6; text-align: center;">No scheduled broadcasts</td>';
                    return;
                }}

                broadcasts.forEach(b => {{
                    const row = table.insertRow();
                    const scheduledDate = new Date(b.scheduled_date).toLocaleString();
                    const statusBadge = b.status === 'scheduled'
                        ? '<span class="status-badge status-pending">Scheduled</span>'
                        : '<span class="status-badge status-sending">Sending</span>';
                    const cancelBtn = b.status === 'scheduled'
                        ? `<button onclick="cancelScheduledBroadcast(${{b.id}})" style="background: #e74c3c; color: white; border: none; padding: 5px 10px; border-radius: 4px; cursor: pointer;">Cancel</button>`
                        : '-';

                    row.innerHTML = `
                        <td>${{scheduledDate}}</td>
                        <td style="text-transform: capitalize;">${{b.audience}}</td>
                        <td title="${{b.full_message}}">${{b.message}}</td>
                        <td>${{statusBadge}}</td>
                        <td style="text-align: center;">${{cancelBtn}}</td>
                    `;
                }});
            }} catch (e) {{
                console.error('Error loading scheduled broadcasts:', e);
            }}
        }}

        async function cancelScheduledBroadcast(id) {{
            if (!confirm('Are you sure you want to cancel this scheduled broadcast?')) return;

            try {{
                const response = await fetch(`/admin/broadcast/scheduled/${{id}}/cancel`, {{
                    method: 'DELETE'
                }});

                if (response.ok) {{
                    alert('Broadcast cancelled');
                    loadScheduledBroadcasts();
                }} else {{
                    const data = await response.json();
                    alert('Error: ' + (data.detail || 'Unknown error'));
                }}
            }} catch (e) {{
                alert('Error: ' + e.message);
            }}
        }}

        async function handleBroadcastSubmit() {{
            const isScheduled = document.getElementById('scheduleCheckbox').checked;

            if (isScheduled) {{
                await scheduleBroadcast();
            }} else {{
                await sendBroadcast();
            }}
        }}

        async function scheduleBroadcast() {{
            hideConfirmModal();

            const audience = document.getElementById('audience').value;
            const message = document.getElementById('message').value;
            const scheduledDate = document.getElementById('scheduleDate').value;
            const sendBtn = document.getElementById('sendBtn');
            const progressInfo = document.getElementById('progressInfo');
            const progressText = document.getElementById('progressText');

            if (!scheduledDate) {{
                alert('Please select a date and time for the scheduled broadcast');
                return;
            }}

            sendBtn.disabled = true;
            sendBtn.textContent = 'Scheduling...';
            progressInfo.classList.add('active');
            progressText.textContent = 'Scheduling broadcast...';

            try {{
                // Convert local datetime to UTC ISO string
                const localDate = new Date(scheduledDate);
                const utcDate = localDate.toISOString();

                const response = await fetch('/admin/broadcast/schedule', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json'
                    }},
                    body: JSON.stringify({{
                        message: message,
                        audience: audience,
                        scheduled_date: utcDate,
                        phone_number: audience === 'single' ? document.getElementById('singlePhone').value.trim() : undefined
                    }})
                }});

                const result = await response.json();

                if (response.ok) {{
                    progressText.innerHTML = `<span style="color: #27ae60;">✅ Broadcast scheduled for ${{new Date(scheduledDate).toLocaleString()}}</span>`;
                    document.getElementById('message').value = '';
                    document.getElementById('scheduleCheckbox').checked = false;
                    toggleScheduleMode();
                    updatePreview();
                    loadScheduledBroadcasts();

                    sendBtn.disabled = false;
                    sendBtn.textContent = 'Send Now';
                }} else {{
                    progressText.textContent = `Error: ${{result.detail || 'Unknown error'}}`;
                    sendBtn.disabled = false;
                    sendBtn.textContent = 'Schedule';
                }}
            }} catch (e) {{
                progressText.textContent = `Error: ${{e.message}}`;
                sendBtn.disabled = false;
                sendBtn.textContent = 'Schedule';
            }}
        }}

        // Conversation Viewer Functions
        let currentOffset = 0;
        const PAGE_SIZE = 50;
        let hideReviewed = true;  // Default to hiding reviewed conversations

        function toggleHideReviewed() {{
            hideReviewed = !hideReviewed;
            const btn = document.getElementById('toggleReviewedBtn');
            if (hideReviewed) {{
                btn.textContent = 'Show Reviewed';
                btn.style.background = '#27ae60';
            }} else {{
                btn.textContent = 'Hide Reviewed';
                btn.style.background = '#95a5a6';
            }}
            currentOffset = 0;
            loadConversations();
        }}

        function showConversationTab(tab) {{
            document.querySelectorAll('.tabs .tab').forEach(t => t.classList.remove('active'));
            event.target.classList.add('active');

            if (tab === 'recent') {{
                document.getElementById('recentTab').style.display = 'block';
                document.getElementById('flaggedTab').style.display = 'none';
            }} else {{
                document.getElementById('recentTab').style.display = 'none';
                document.getElementById('flaggedTab').style.display = 'block';
                loadFlaggedConversations();
            }}
        }}

        async function loadConversations(offset = 0) {{
            currentOffset = Math.max(0, offset);
            const phone = document.getElementById('phoneFilter').value.trim();
            const intent = document.getElementById('intentFilter').value;
            const table = document.getElementById('conversationTable');
            const loadingRow = document.getElementById('conversationLoading');

            // Show loading
            if (loadingRow) {{
                loadingRow.innerHTML = '<td colspan="6" style="color: #95a5a6; text-align: center;">Loading...</td>';
            }}

            try {{
                let url = `/admin/conversations?limit=${{PAGE_SIZE}}&offset=${{currentOffset}}&hide_reviewed=${{hideReviewed}}`;
                if (phone) {{
                    url += `&phone=${{encodeURIComponent(phone)}}`;
                }}
                if (intent) {{
                    url += `&intent=${{encodeURIComponent(intent)}}`;
                }}

                const response = await fetch(appendDateFilter(url));
                const conversations = await response.json();

                // Clear existing rows except header
                while (table.rows.length > 1) {{
                    table.deleteRow(1);
                }}

                if (conversations.length === 0) {{
                    const row = table.insertRow();
                    row.innerHTML = '<td colspan="6" style="color: #95a5a6; text-align: center;">No conversations found</td>';
                }} else {{
                    conversations.forEach(c => {{
                        const row = table.insertRow();
                        const userTz = c.timezone || 'America/New_York';
                        const date = new Date(c.created_at).toLocaleString('en-US', {{ timeZone: userTz }});
                        const phoneMasked = c.phone_number ? '...' + c.phone_number.slice(-4) : 'N/A';
                        const intentBadge = c.intent ? `<span class="intent-badge">${{c.intent}}</span>` : '-';
                        const msgInEscaped = escapeHtml(c.message_in).replace(/'/g, "\\'").replace(/"/g, "&quot;");
                        const msgOutEscaped = escapeHtml(c.message_out).replace(/'/g, "\\'").replace(/"/g, "&quot;");
                        const reviewStatus = c.review_status;

                        // Highlight based on review status
                        if (reviewStatus === 'good') {{
                            row.style.background = '#e8f5e9';  // Light green for good
                        }} else if (reviewStatus) {{
                            row.style.background = '#fef3e2';  // Light orange for flagged
                        }}

                        let actionButtons;
                        if (reviewStatus === 'good') {{
                            actionButtons = '<span style="color: #27ae60; font-size: 0.85em;">Good</span>';
                        }} else if (reviewStatus === 'dismissed') {{
                            actionButtons = '<span style="color: #95a5a6; font-size: 0.85em;">Dismissed</span>';
                        }} else if (reviewStatus) {{
                            actionButtons = '<span style="color: #e67e22; font-size: 0.85em;">Flagged</span>';
                        }} else {{
                            actionButtons = `
                                <button class="btn" style="padding: 3px 6px; font-size: 0.75em; background: #27ae60; color: white; margin-right: 3px;"
                                    onclick="markAsGood(${{c.id}}, '${{c.phone_number}}')">Good</button>
                                <button class="btn" style="padding: 3px 6px; font-size: 0.75em; background: #e67e22; color: white; margin-right: 3px;"
                                    onclick="showFlagModal(${{c.id}}, '${{c.phone_number}}', '${{msgInEscaped}}', '${{msgOutEscaped}}')">Flag</button>
                                <button class="btn" style="padding: 3px 6px; font-size: 0.75em; background: #95a5a6; color: white;"
                                    onclick="dismissConversation(${{c.id}}, '${{c.phone_number}}')">Dismiss</button>
                            `;
                        }}

                        row.innerHTML = `
                            <td>${{date}}</td>
                            <td>${{phoneMasked}}</td>
                            <td><div class="msg-in">${{escapeHtml(c.message_in)}}</div></td>
                            <td><div class="msg-out">${{escapeHtml(c.message_out)}}</div></td>
                            <td>${{intentBadge}}</td>
                            <td>${{actionButtons}}</td>
                        `;
                    }});
                }}

                // Update UI
                document.getElementById('conversationCount').textContent = conversations.length;
                document.getElementById('prevBtn').disabled = currentOffset === 0;
                document.getElementById('nextBtn').disabled = conversations.length < PAGE_SIZE;
                document.getElementById('pageInfo').textContent = `Page ${{Math.floor(currentOffset / PAGE_SIZE) + 1}}`;

            }} catch (e) {{
                console.error('Error loading conversations:', e);
                const row = table.insertRow();
                row.innerHTML = '<td colspan="5" style="color: #e74c3c; text-align: center;">Error loading conversations</td>';
            }}
        }}

        function clearFilter() {{
            document.getElementById('phoneFilter').value = '';
            document.getElementById('intentFilter').value = '';
            currentOffset = 0;
            loadConversations();
        }}

        function escapeHtml(text) {{
            if (!text) return '';
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }}

        async function loadFlaggedConversations() {{
            const includeReviewed = document.getElementById('showReviewedCheckbox').checked;
            const table = document.getElementById('flaggedTable');
            const loadingRow = document.getElementById('flaggedLoading');

            if (loadingRow) {{
                loadingRow.innerHTML = '<td colspan="5" style="color: #95a5a6; text-align: center;">Loading...</td>';
            }}

            try {{
                const response = await fetch(appendDateFilter(`/admin/conversations/flagged?include_reviewed=${{includeReviewed}}`));
                const flagged = await response.json();

                // Store for export
                flaggedData = flagged;

                // Clear existing rows except header
                while (table.rows.length > 1) {{
                    table.deleteRow(1);
                }}

                // Update flagged count badge
                const unreviewedCount = flagged.filter(f => !f.reviewed).length;
                document.getElementById('flaggedCount').textContent = unreviewedCount;

                if (flagged.length === 0) {{
                    const row = table.insertRow();
                    row.innerHTML = '<td colspan="6" style="color: #95a5a6; text-align: center;">No flagged conversations</td>';
                }} else {{
                    flagged.forEach(f => {{
                        const row = table.insertRow();
                        if (!f.reviewed) {{
                            row.style.background = '#fff8e1';
                        }}
                        const userTz = f.timezone || 'America/New_York';
                        const date = new Date(f.created_at).toLocaleString('en-US', {{ timeZone: userTz }});
                        const phoneMasked = f.phone_number ? '...' + f.phone_number.slice(-4) : 'N/A';
                        const severityClass = `severity-${{f.severity || 'low'}}`;
                        const source = f.source || 'ai';
                        const sourceLabel = source === 'manual' ? 'Manual' : 'AI';
                        const sourceColor = source === 'manual' ? '#9b59b6' : '#3498db';

                        row.innerHTML = `
                            <td><span style="background: ${{sourceColor}}; color: white; padding: 2px 6px; border-radius: 3px; font-size: 0.75em;">${{sourceLabel}}</span></td>
                            <td>${{date}}</td>
                            <td>${{phoneMasked}}</td>
                            <td>
                                <div class="msg-in">${{escapeHtml(f.message_in)}}</div>
                                <div class="msg-out">${{escapeHtml(f.message_out)}}</div>
                                <div class="ai-explanation">${{escapeHtml(f.ai_explanation)}}</div>
                            </td>
                            <td>
                                <span class="${{severityClass}}">${{f.severity}}</span><br>
                                <small>${{f.issue_type}}</small>
                            </td>
                            <td>
                                ${{f.reviewed
                                    ? '<span style="color: #27ae60;">Reviewed</span>'
                                    : `<button class="btn btn-secondary" style="padding: 4px 8px; font-size: 0.8em;" onclick="markAsReviewed(${{f.id}})">Mark Reviewed</button>`
                                }}
                            </td>
                        `;
                    }});
                }}

            }} catch (e) {{
                console.error('Error loading flagged conversations:', e);
                const row = table.insertRow();
                row.innerHTML = '<td colspan="5" style="color: #e74c3c; text-align: center;">Error loading flagged conversations</td>';
            }}
        }}

        async function markAsReviewed(analysisId) {{
            try {{
                const response = await fetch(`/admin/conversations/flagged/${{analysisId}}/reviewed`, {{
                    method: 'POST'
                }});

                if (response.ok) {{
                    loadFlaggedConversations();
                }} else {{
                    alert('Error marking as reviewed');
                }}
            }} catch (e) {{
                console.error('Error:', e);
                alert('Error marking as reviewed');
            }}
        }}

        async function runAnalysis() {{
            const statusDiv = document.getElementById('analysisStatus');
            statusDiv.style.display = 'block';
            statusDiv.innerHTML = 'Starting AI analysis...';
            statusDiv.style.background = '#cce5ff';

            try {{
                const response = await fetch('/admin/conversations/analyze', {{
                    method: 'POST'
                }});

                if (response.ok) {{
                    statusDiv.innerHTML = '✅ Analysis started! Results will appear shortly. Refresh the page in a minute to see flagged items.';
                    statusDiv.style.background = '#d4edda';

                    // Reload flagged after a delay
                    setTimeout(() => {{
                        loadFlaggedConversations();
                    }}, 5000);
                }} else {{
                    statusDiv.innerHTML = '❌ Error starting analysis';
                    statusDiv.style.background = '#f8d7da';
                }}
            }} catch (e) {{
                statusDiv.innerHTML = '❌ Error: ' + e.message;
                statusDiv.style.background = '#f8d7da';
            }}
        }}

        // Store flagged data for export
        let flaggedData = [];

        // Export flagged and good conversations for sharing with Claude
        async function exportFlagged() {{
            // Get flagged items (already in flaggedData)
            const unreviewedFlagged = flaggedData.filter(f => !f.reviewed && f.issue_type !== 'good');

            // Fetch good conversations
            let goodConversations = [];
            try {{
                const response = await fetch('/admin/conversations/good');
                if (response.ok) {{
                    goodConversations = await response.json();
                }}
            }} catch (e) {{
                console.error('Error fetching good conversations:', e);
            }}

            if (unreviewedFlagged.length === 0 && goodConversations.length === 0) {{
                alert('No flagged or good conversations to export');
                return;
            }}

            let exportText = `## Conversation Review for AI Improvement\\n\\n`;

            // Add good conversations section
            if (goodConversations.length > 0) {{
                exportText += `### Good Conversations (preserve this behavior)\\n\\n`;
                goodConversations.slice(0, 10).forEach((g, i) => {{
                    exportText += `**${{i + 1}}. User:** ${{g.message_in}}\\n`;
                    exportText += `**System:** ${{g.message_out}}\\n`;
                    if (g.intent) exportText += `*Intent: ${{g.intent}}*\\n`;
                    exportText += `\\n`;
                }});
            }}

            // Add flagged conversations section
            if (unreviewedFlagged.length > 0) {{
                exportText += `### Flagged Conversations (need improvement)\\n\\n`;
                unreviewedFlagged.forEach((f, i) => {{
                    exportText += `**${{i + 1}}. Issue:** ${{f.issue_type.replace(/_/g, ' ')}} (${{f.severity}})\\n`;
                    exportText += `**User:** ${{f.message_in}}\\n`;
                    exportText += `**System:** ${{f.message_out}}\\n`;
                    exportText += `**Problem:** ${{f.ai_explanation}}\\n\\n`;
                }});
            }}

            exportText += `---\\nPlease help improve the AI to handle the flagged cases better while preserving the good behavior.`;

            // Copy to clipboard
            navigator.clipboard.writeText(exportText).then(() => {{
                alert('Copied to clipboard! Paste this into your conversation with Claude.');
            }}).catch(err => {{
                // Fallback: show in a textarea
                const textarea = document.createElement('textarea');
                textarea.value = exportText;
                textarea.style.position = 'fixed';
                textarea.style.top = '50%';
                textarea.style.left = '50%';
                textarea.style.transform = 'translate(-50%, -50%)';
                textarea.style.width = '80%';
                textarea.style.height = '400px';
                textarea.style.zIndex = '10000';
                textarea.style.padding = '20px';
                textarea.style.border = '2px solid #3498db';
                textarea.style.borderRadius = '8px';
                document.body.appendChild(textarea);
                textarea.select();
                alert('Copy the text from the textarea, then click anywhere to close it.');
                textarea.addEventListener('blur', () => textarea.remove());
            }});
        }}

        // Mark as Good Function
        async function markAsGood(logId, phone) {{
            try {{
                const response = await fetch('/admin/conversations/good', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        log_id: logId,
                        phone_number: phone,
                        notes: ''
                    }})
                }});

                if (response.ok) {{
                    loadConversations();  // Refresh to update status
                }} else {{
                    alert('Error marking as good');
                }}
            }} catch (e) {{
                console.error('Error:', e);
                alert('Error marking as good');
            }}
        }}

        // Dismiss Conversation Function
        async function dismissConversation(logId, phone) {{
            try {{
                const response = await fetch('/admin/conversations/dismiss', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        log_id: logId,
                        phone_number: phone
                    }})
                }});

                if (response.ok) {{
                    loadConversations();  // Refresh to update status
                }} else {{
                    alert('Error dismissing conversation');
                }}
            }} catch (e) {{
                console.error('Error:', e);
                alert('Error dismissing conversation');
            }}
        }}

        // Manual Flag Functions
        function showFlagModal(logId, phone, msgIn, msgOut) {{
            document.getElementById('flagLogId').value = logId;
            document.getElementById('flagPhone').value = phone;
            document.getElementById('flagMsgIn').textContent = msgIn;
            document.getElementById('flagMsgOut').textContent = msgOut.substring(0, 200) + (msgOut.length > 200 ? '...' : '');
            document.getElementById('flagIssueType').value = 'needs_review';
            document.getElementById('flagNotes').value = '';
            document.getElementById('flagModal').classList.add('active');
        }}

        function hideFlagModal() {{
            document.getElementById('flagModal').classList.remove('active');
        }}

        async function submitFlag() {{
            const logId = document.getElementById('flagLogId').value;
            const phone = document.getElementById('flagPhone').value;
            const issueType = document.getElementById('flagIssueType').value;
            const notes = document.getElementById('flagNotes').value.trim();

            if (!notes) {{
                alert('Please add notes describing the issue');
                return;
            }}

            try {{
                const response = await fetch('/admin/conversations/flag', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        log_id: parseInt(logId),
                        phone_number: phone,
                        issue_type: issueType,
                        notes: notes
                    }})
                }});

                if (response.ok) {{
                    hideFlagModal();
                    alert('Conversation flagged for review');
                    loadFlaggedConversations();
                }} else {{
                    const data = await response.json();
                    alert('Error: ' + (data.detail || 'Unknown error'));
                }}
            }} catch (e) {{
                alert('Error: ' + e.message);
            }}
        }}

        // Changelog functions
        async function loadChangelog() {{
            try {{
                const response = await fetch(appendDateFilter('/admin/changelog'));
                const entries = await response.json();

                const container = document.getElementById('changelogEntries');

                if (entries.length === 0) {{
                    container.innerHTML = '<p style="color: #95a5a6;">No changelog entries yet.</p>';
                    return;
                }}

                const typeLabels = {{
                    'bug_fix': '🐛 Bug Fix',
                    'feature': '✨ Feature',
                    'improvement': '🔧 Improvement'
                }};

                container.innerHTML = entries.map(e => {{
                    const date = new Date(e.created_at).toLocaleDateString();
                    return `
                        <div style="background: white; padding: 12px; border-radius: 6px; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: flex-start;">
                            <div>
                                <span style="font-size: 0.85em; color: #7f8c8d;">${{date}}</span>
                                <span style="margin-left: 10px; font-size: 0.85em;">${{typeLabels[e.entry_type] || e.entry_type}}</span>
                                <div style="font-weight: 500; margin-top: 4px;">${{e.title}}</div>
                                ${{e.description ? `<div style="color: #666; font-size: 0.9em; margin-top: 4px;">${{e.description}}</div>` : ''}}
                            </div>
                            <button onclick="deleteChangelogEntry(${{e.id}})" style="background: #e74c3c; color: white; border: none; padding: 4px 8px; border-radius: 4px; cursor: pointer; font-size: 0.8em;">Delete</button>
                        </div>
                    `;
                }}).join('');
            }} catch (e) {{
                console.error('Error loading changelog:', e);
                document.getElementById('changelogEntries').innerHTML = '<p style="color: #e74c3c;">Error loading changelog</p>';
            }}
        }}

        async function addChangelogEntry() {{
            const title = document.getElementById('changelogTitle').value.trim();
            const description = document.getElementById('changelogDescription').value.trim();
            const entryType = document.getElementById('changelogType').value;

            if (!title) {{
                alert('Please enter a title');
                return;
            }}

            try {{
                const response = await fetch('/admin/changelog', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        title: title,
                        description: description || null,
                        entry_type: entryType
                    }})
                }});

                if (response.ok) {{
                    document.getElementById('changelogTitle').value = '';
                    document.getElementById('changelogDescription').value = '';
                    loadChangelog();
                }} else {{
                    const error = await response.json();
                    alert('Error: ' + (error.detail || 'Failed to add entry'));
                }}
            }} catch (e) {{
                alert('Error: ' + e.message);
            }}
        }}

        async function deleteChangelogEntry(id) {{
            if (!confirm('Delete this changelog entry?')) return;

            try {{
                const response = await fetch(`/admin/changelog/${{id}}`, {{
                    method: 'DELETE'
                }});

                if (response.ok) {{
                    loadChangelog();
                }} else {{
                    const error = await response.json();
                    alert('Error: ' + (error.detail || 'Failed to delete entry'));
                }}
            }} catch (e) {{
                alert('Error: ' + e.message);
            }}
        }}

        // Contact Messages functions
        async function loadContactMessages() {{
            try {{
                const includeResolved = document.getElementById('showResolvedContactMsgs').checked;
                const category = document.getElementById('contactMsgCategory').value;
                let url = `/admin/contact-messages?include_resolved=${{includeResolved}}`;
                if (category) url += `&category=${{category}}`;

                const response = await fetch(appendDateFilter(url));
                const data = await response.json();
                const messages = data.messages || [];

                const container = document.getElementById('contactMessagesList');
                const unresolvedCount = messages.filter(m => !m.resolved).length;
                document.getElementById('contactMsgCount').textContent = unresolvedCount > 0 ? `(${{unresolvedCount}} unresolved)` : '';

                if (messages.length === 0) {{
                    container.innerHTML = '<p style="color: #95a5a6;">No contact messages.</p>';
                    return;
                }}

                const catColors = {{ feedback: '#f39c12', bug: '#e74c3c', question: '#3498db' }};
                const srcColors = {{ sms: '#9b59b6', web: '#2ecc71' }};

                container.innerHTML = messages.map(m => {{
                    const catColor = catColors[m.category] || '#95a5a6';
                    const srcColor = srcColors[m.source] || '#95a5a6';
                    const date = new Date(m.created_at).toLocaleString();
                    const resolvedStyle = m.resolved ? 'opacity: 0.6;' : '';
                    const replyHtml = m.admin_reply
                        ? `<div style="background: #eaf7ea; padding: 8px 12px; border-radius: 6px; margin-top: 8px; border-left: 3px solid #27ae60;">
                               <span style="color: #27ae60; font-weight: bold; font-size: 0.85em;">Admin Reply:</span>
                               <span style="color: #333; margin-left: 6px;">${{m.admin_reply}}</span>
                           </div>`
                        : '';
                    return `
                        <div style="background: white; padding: 15px; border-radius: 8px; margin-bottom: 10px; border-left: 4px solid ${{catColor}}; ${{resolvedStyle}}">
                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                <div>
                                    <strong>${{m.user_name || 'Unknown'}}</strong> (...${{m.phone_number.slice(-4)}})
                                    <span style="background: ${{catColor}}; color: white; padding: 2px 8px; border-radius: 4px; font-size: 0.8em; margin-left: 8px;">${{m.category.toUpperCase()}}</span>
                                    <span style="background: ${{srcColor}}; color: white; padding: 2px 8px; border-radius: 4px; font-size: 0.8em; margin-left: 4px;">${{m.source.toUpperCase()}}</span>
                                </div>
                                <div style="display: flex; gap: 6px;">
                                    <button onclick="showContactReplyForm(${{m.id}})" class="btn" style="background: #3498db; font-size: 0.85em; padding: 5px 12px;">
                                        ${{m.admin_reply ? 'Reply Again' : 'Reply'}}
                                    </button>
                                    <button onclick="toggleContactMsg(${{m.id}})" class="btn" style="background: ${{m.resolved ? '#f39c12' : '#27ae60'}}; font-size: 0.85em; padding: 5px 12px;">
                                        ${{m.resolved ? 'Unresolve' : 'Resolve'}}
                                    </button>
                                </div>
                            </div>
                            <div style="color: #333; margin-top: 8px;">${{m.message}}</div>
                            ${{replyHtml}}
                            <div style="color: #95a5a6; font-size: 0.8em; margin-top: 5px;">${{date}}${{m.resolved ? ' — Resolved' : ''}}</div>
                            <div id="contactReplyForm-${{m.id}}" style="display: none; margin-top: 10px;">
                                <div style="display: flex; gap: 8px;">
                                    <input type="text" id="contactReplyInput-${{m.id}}" placeholder="Type your reply..." style="flex: 1; padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px;">
                                    <button onclick="sendContactReply(${{m.id}})" class="btn" style="background: #3498db; padding: 8px 16px;">Send</button>
                                    <button onclick="hideContactReplyForm(${{m.id}})" class="btn" style="background: #95a5a6; padding: 8px 12px;">Cancel</button>
                                </div>
                            </div>
                        </div>
                    `;
                }}).join('');
            }} catch (e) {{
                console.error('Error loading contact messages:', e);
                document.getElementById('contactMessagesList').innerHTML = '<p style="color: #e74c3c;">Error loading contact messages</p>';
            }}
        }}

        async function toggleContactMsg(id) {{
            try {{
                const response = await fetch(`/admin/contact-messages/${{id}}/toggle`, {{ method: 'POST' }});
                if (response.ok) loadContactMessages();
            }} catch (e) {{
                alert('Error updating contact message');
            }}
        }}

        function showContactReplyForm(id) {{
            document.getElementById(`contactReplyForm-${{id}}`).style.display = 'block';
            document.getElementById(`contactReplyInput-${{id}}`).focus();
        }}

        function hideContactReplyForm(id) {{
            document.getElementById(`contactReplyForm-${{id}}`).style.display = 'none';
            document.getElementById(`contactReplyInput-${{id}}`).value = '';
        }}

        async function sendContactReply(id) {{
            const input = document.getElementById(`contactReplyInput-${{id}}`);
            const message = input.value.trim();
            if (!message) return;

            try {{
                const response = await fetch(`/admin/contact-messages/${{id}}/reply`, {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ message }})
                }});
                if (response.ok) {{
                    loadContactMessages();
                }} else {{
                    const error = await response.json();
                    alert('Error: ' + (error.detail || 'Failed to send reply'));
                }}
            }} catch (e) {{
                alert('Error sending reply: ' + e.message);
            }}
        }}

        // Support ticket functions
        let currentTicketId = null;
        let currentTicketStatus = null;

        async function loadSupportTickets() {{
            try {{
                const includeClosed = document.getElementById('showClosedTickets').checked;
                const response = await fetch(appendDateFilter(`/admin/support/tickets?include_closed=${{includeClosed}}`));
                const tickets = await response.json();

                const container = document.getElementById('supportTicketsList');
                const openCount = tickets.filter(t => t.status === 'open').length;
                document.getElementById('openTicketCount').textContent = openCount > 0 ? `(${{openCount}} open)` : '';

                if (tickets.length === 0) {{
                    container.innerHTML = '<p style="color: #95a5a6;">No support tickets yet.</p>';
                    return;
                }}

                container.innerHTML = tickets.map(t => {{
                    const statusColor = t.status === 'open' ? '#27ae60' : '#95a5a6';
                    const date = new Date(t.updated_at).toLocaleString();
                    return `
                        <div style="background: white; padding: 15px; border-radius: 8px; margin-bottom: 10px; cursor: pointer; border-left: 4px solid ${{statusColor}};" onclick="openTicketModal(${{t.id}}, '${{t.status}}', '${{t.user_name || 'Unknown'}}', '${{t.phone_number}}')">
                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                <div>
                                    <strong>#${{t.id}}</strong> - ${{t.user_name || 'Unknown'}} (...${{t.phone_number.slice(-4)}})
                                    <span style="background: ${{statusColor}}; color: white; padding: 2px 8px; border-radius: 4px; font-size: 0.8em; margin-left: 10px;">${{t.status}}</span>
                                </div>
                                <span style="color: #7f8c8d; font-size: 0.85em;">${{t.message_count}} messages</span>
                            </div>
                            <div style="color: #666; font-size: 0.9em; margin-top: 8px;">${{t.last_message || 'No messages'}}</div>
                            <div style="color: #95a5a6; font-size: 0.8em; margin-top: 5px;">Last updated: ${{date}}</div>
                        </div>
                    `;
                }}).join('');
            }} catch (e) {{
                console.error('Error loading support tickets:', e);
                document.getElementById('supportTicketsList').innerHTML = '<p style="color: #e74c3c;">Error loading tickets</p>';
            }}
        }}

        let ticketRefreshInterval = null;
        let currentTicketUserName = null;
        let currentTicketPhone = null;

        async function openTicketModal(ticketId, status, userName, phoneNumber) {{
            currentTicketId = ticketId;
            currentTicketStatus = status;
            currentTicketUserName = userName || 'Unknown';
            currentTicketPhone = phoneNumber;
            document.getElementById('ticketModalTitle').textContent = `Ticket #${{ticketId}}`;
            document.getElementById('ticketModal').style.display = 'block';

            // Show/hide close/reopen buttons based on status
            document.getElementById('closeTicketBtn').style.display = status === 'open' ? 'block' : 'none';
            document.getElementById('reopenTicketBtn').style.display = status === 'closed' ? 'block' : 'none';

            await loadTicketMessages(ticketId);

            // Start auto-refresh for new messages (every 5 seconds)
            if (ticketRefreshInterval) clearInterval(ticketRefreshInterval);
            ticketRefreshInterval = setInterval(() => {{
                if (currentTicketId) loadTicketMessages(currentTicketId);
            }}, 5000);
        }}

        function viewTicketCustomer() {{
            if (currentTicketPhone) {{
                closeTicketModal();
                // Scroll to customer service section and search for the customer
                document.getElementById('customer-service').scrollIntoView({{ behavior: 'smooth' }});
                document.getElementById('csSearchInput').value = currentTicketPhone;
                csSearch();
            }}
        }}

        function closeTicketModal() {{
            // Stop auto-refresh
            if (ticketRefreshInterval) {{
                clearInterval(ticketRefreshInterval);
                ticketRefreshInterval = null;
            }}
            document.getElementById('ticketModal').style.display = 'none';
            currentTicketId = null;
            currentTicketStatus = null;
            document.getElementById('ticketReplyInput').value = '';
        }}

        async function loadTicketMessages(ticketId) {{
            try {{
                const response = await fetch(`/admin/support/tickets/${{ticketId}}/messages`);
                const messages = await response.json();

                const container = document.getElementById('ticketMessages');

                if (messages.length === 0) {{
                    container.innerHTML = '<p style="color: #95a5a6; text-align: center;">No messages yet.</p>';
                    return;
                }}

                container.innerHTML = messages.map(m => {{
                    const isInbound = m.direction === 'inbound';
                    const align = isInbound ? 'flex-start' : 'flex-end';
                    const bgColor = isInbound ? 'white' : '#3498db';
                    const textColor = isInbound ? '#333' : 'white';
                    const label = isInbound ? currentTicketUserName : 'Support';
                    const time = new Date(m.created_at).toLocaleString();

                    return `
                        <div style="display: flex; justify-content: ${{align}}; margin-bottom: 10px;">
                            <div style="max-width: 80%; background: ${{bgColor}}; color: ${{textColor}}; padding: 10px 15px; border-radius: 12px; box-shadow: 0 1px 2px rgba(0,0,0,0.1);">
                                <div style="font-size: 0.75em; opacity: 0.8; margin-bottom: 4px;">${{label}} - ${{time}}</div>
                                <div>${{m.message}}</div>
                            </div>
                        </div>
                    `;
                }}).join('');

                // Scroll to bottom
                container.scrollTop = container.scrollHeight;
            }} catch (e) {{
                console.error('Error loading ticket messages:', e);
            }}
        }}

        async function sendTicketReply() {{
            const input = document.getElementById('ticketReplyInput');
            const message = input.value.trim();

            if (!message || !currentTicketId) return;

            try {{
                const response = await fetch(`/admin/support/tickets/${{currentTicketId}}/reply`, {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ message: message }})
                }});

                if (response.ok) {{
                    input.value = '';
                    await loadTicketMessages(currentTicketId);
                    loadSupportTickets();
                }} else {{
                    const error = await response.json();
                    alert('Error: ' + (error.detail || 'Failed to send reply'));
                }}
            }} catch (e) {{
                alert('Error: ' + e.message);
            }}
        }}

        async function closeCurrentTicket() {{
            if (!currentTicketId || !confirm('Close this ticket?')) return;

            try {{
                const response = await fetch(`/admin/support/tickets/${{currentTicketId}}/close`, {{
                    method: 'POST'
                }});

                if (response.ok) {{
                    closeTicketModal();
                    loadSupportTickets();
                }} else {{
                    alert('Error closing ticket');
                }}
            }} catch (e) {{
                alert('Error: ' + e.message);
            }}
        }}

        async function reopenCurrentTicket() {{
            if (!currentTicketId) return;

            try {{
                const response = await fetch(`/admin/support/tickets/${{currentTicketId}}/reopen`, {{
                    method: 'POST'
                }});

                if (response.ok) {{
                    currentTicketStatus = 'open';
                    document.getElementById('closeTicketBtn').style.display = 'block';
                    document.getElementById('reopenTicketBtn').style.display = 'none';
                    loadSupportTickets();
                }} else {{
                    alert('Error reopening ticket');
                }}
            }} catch (e) {{
                alert('Error: ' + e.message);
            }}
        }}

        // Allow Enter key to send reply
        document.getElementById('ticketReplyInput')?.addEventListener('keypress', function(e) {{
            if (e.key === 'Enter') sendTicketReply();
        }});

        // Initialize
        updateFilterLabel();
        loadOverviewStats();
        loadStats();
        loadHistory();
        loadFeedback();
        loadCostData();
        loadMaintenanceMessage();
        loadStagingFallback();
        loadScheduledBroadcasts();
        loadConversations();
        loadFlaggedConversations();
        loadChangelog();
        loadSupportTickets();
        loadContactMessages();
        loadRecurring();

        // Handle URL hash for deep linking to support tickets
        async function handleSupportHash() {{
            const hash = window.location.hash;
            if (hash && hash.startsWith('#support-')) {{
                const ticketId = hash.replace('#support-', '');
                if (ticketId && !isNaN(ticketId)) {{
                    // Scroll to support section and open the ticket
                    document.getElementById('support').scrollIntoView({{ behavior: 'smooth' }});
                    // Wait for tickets to load, then find the ticket info and open it
                    setTimeout(async () => {{
                        // Try to find ticket info from loaded tickets
                        const response = await fetch('/admin/support/tickets?include_closed=true');
                        const data = await response.json();
                        const ticket = data.tickets.find(t => t.id === parseInt(ticketId));
                        if (ticket) {{
                            openTicketModal(parseInt(ticketId), ticket.status, ticket.user_name, ticket.phone_number);
                        }} else {{
                            openTicketModal(parseInt(ticketId), 'open', 'Unknown', '');
                        }}
                    }}, 500);
                }}
            }}
        }}
        handleSupportHash();
        window.addEventListener('hashchange', handleSupportHash);

        // =====================================================
        // RECURRING REMINDERS FUNCTIONS
        // =====================================================

        let allRecurring = [];

        async function loadRecurring() {{
            try {{
                const response = await fetch(appendDateFilter('/admin/recurring'));
                const data = await response.json();
                allRecurring = data.recurring || [];
                document.getElementById('recurringCount').textContent = data.count || 0;
                renderRecurring();
            }} catch (e) {{
                console.error('Error loading recurring:', e);
                document.getElementById('recurringLoading').innerHTML = '<td colspan="9" style="color: #e74c3c; text-align: center;">Error loading recurring reminders</td>';
            }}
        }}

        function renderRecurring() {{
            const table = document.getElementById('recurringTable');
            const phoneFilter = document.getElementById('recurringPhoneFilter').value.toLowerCase();
            const statusFilter = document.getElementById('recurringStatusFilter').value;

            // Clear existing rows except header
            while (table.rows.length > 1) {{
                table.deleteRow(1);
            }}

            let filtered = allRecurring.filter(r => {{
                if (phoneFilter && !r.phone.toLowerCase().includes(phoneFilter)) return false;
                if (statusFilter === 'active' && !r.active) return false;
                if (statusFilter === 'paused' && r.active) return false;
                return true;
            }});

            if (filtered.length === 0) {{
                const row = table.insertRow(-1);
                row.innerHTML = '<td colspan="9" style="color: #95a5a6; text-align: center;">No recurring reminders found</td>';
                return;
            }}

            for (const r of filtered) {{
                const row = table.insertRow(-1);
                const statusColor = r.active ? '#27ae60' : '#e74c3c';
                const statusText = r.active ? 'Active' : 'Paused';
                const toggleBtn = r.active
                    ? `<button class="btn btn-secondary" style="padding: 4px 8px; font-size: 0.8em;" onclick="pauseRecurring(${{r.id}})">Pause</button>`
                    : `<button class="btn" style="padding: 4px 8px; font-size: 0.8em; background: #27ae60; color: white;" onclick="resumeRecurring(${{r.id}})">Resume</button>`;

                // Format next occurrence
                let nextStr = '-';
                if (r.next_occurrence) {{
                    const next = new Date(r.next_occurrence);
                    nextStr = next.toLocaleDateString('en-US', {{ month: 'short', day: 'numeric' }}) + ' ' +
                              next.toLocaleTimeString('en-US', {{ hour: 'numeric', minute: '2-digit' }});
                }}

                row.innerHTML = `
                    <td>${{r.id}}</td>
                    <td>${{r.phone}}</td>
                    <td style="max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${{r.text}}">${{r.text}}</td>
                    <td>${{r.pattern}}</td>
                    <td>${{r.time || '-'}}</td>
                    <td style="font-size: 0.85em;">${{r.timezone || '-'}}</td>
                    <td><span style="color: ${{statusColor}}; font-weight: 500;">${{statusText}}</span></td>
                    <td style="font-size: 0.85em;">${{nextStr}}</td>
                    <td>
                        ${{toggleBtn}}
                        <button class="btn btn-danger" style="padding: 4px 8px; font-size: 0.8em;" onclick="deleteRecurring(${{r.id}})">Delete</button>
                    </td>
                `;
            }}
        }}

        async function pauseRecurring(id) {{
            if (!confirm('Pause this recurring reminder?')) return;
            try {{
                const response = await fetch(`/admin/recurring/${{id}}/pause`, {{ method: 'POST' }});
                const data = await response.json();
                if (data.success) {{
                    loadRecurring();
                }} else {{
                    alert('Failed to pause: ' + (data.detail || 'Unknown error'));
                }}
            }} catch (e) {{
                alert('Error: ' + e.message);
            }}
        }}

        async function resumeRecurring(id) {{
            if (!confirm('Resume this recurring reminder?')) return;
            try {{
                const response = await fetch(`/admin/recurring/${{id}}/resume`, {{ method: 'POST' }});
                const data = await response.json();
                if (data.success) {{
                    loadRecurring();
                }} else {{
                    alert('Failed to resume: ' + (data.detail || 'Unknown error'));
                }}
            }} catch (e) {{
                alert('Error: ' + e.message);
            }}
        }}

        async function deleteRecurring(id) {{
            const recurring = allRecurring.find(r => r.id === id);
            const preview = recurring ? recurring.text.substring(0, 30) : '';
            if (!confirm(`Delete recurring reminder "${{preview}}"? This cannot be undone.`)) return;
            try {{
                const response = await fetch(`/admin/recurring/${{id}}`, {{ method: 'DELETE' }});
                const data = await response.json();
                if (data.success) {{
                    loadRecurring();
                }} else {{
                    alert('Failed to delete: ' + (data.detail || 'Unknown error'));
                }}
            }} catch (e) {{
                alert('Error: ' + e.message);
            }}
        }}

        // Filter handlers
        document.getElementById('recurringPhoneFilter')?.addEventListener('input', renderRecurring);
        document.getElementById('recurringStatusFilter')?.addEventListener('change', renderRecurring);

        async function cleanupIncomplete() {{
            if (!confirm('Delete all users who have not completed onboarding?')) return;

            try {{
                const response = await fetch('/admin/users/incomplete', {{
                    method: 'DELETE',
                    headers: {{ 'Authorization': 'Basic ' + btoa('{ADMIN_USERNAME}:{ADMIN_PASSWORD}') }}
                }});
                const data = await response.json();
                alert(data.message);
                location.reload();
            }} catch (err) {{
                alert('Error: ' + err.message);
            }}
        }}

        let pendingOnboardingUsers = [];

        async function viewPendingOnboarding() {{
            const modal = document.getElementById('pendingOnboardingModal');
            const content = document.getElementById('pendingOnboardingContent');
            const controls = document.getElementById('nudgeControls');
            content.innerHTML = '<p style="color: #7f8c8d;">Loading...</p>';
            controls.style.display = 'none';
            modal.classList.add('active');

            try {{
                const response = await fetch('/admin/users/pending-onboarding', {{
                    headers: {{ 'Authorization': 'Basic ' + btoa('{ADMIN_USERNAME}:{ADMIN_PASSWORD}') }}
                }});
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Server error');

                pendingOnboardingUsers = data.users;

                if (data.users.length === 0) {{
                    content.innerHTML = '<p style="color: #27ae60; text-align: center; padding: 20px;">No users stuck in onboarding!</p>';
                    return;
                }}

                controls.style.display = 'block';
                const stepColors = {{ 0: '#e74c3c', 1: '#e67e22', 2: '#f39c12' }};

                let html = `<p style="margin-bottom: 12px; color: #7f8c8d;">${{data.total}} user(s) pending</p>`;
                html += '<table style="width: 100%; border-collapse: collapse; font-size: 0.9em;">';
                html += '<thead><tr style="background: #f8f9fa; text-align: left;">';
                html += '<th style="padding: 8px; border-bottom: 2px solid #ddd;">Phone</th>';
                html += '<th style="padding: 8px; border-bottom: 2px solid #ddd;">Name</th>';
                html += '<th style="padding: 8px; border-bottom: 2px solid #ddd;">Stage</th>';
                html += '<th style="padding: 8px; border-bottom: 2px solid #ddd;">Signed Up</th>';
                html += '<th style="padding: 8px; border-bottom: 2px solid #ddd;">Last Activity</th>';
                html += '<th style="padding: 8px; border-bottom: 2px solid #ddd;">Source</th>';
                html += '<th style="padding: 8px; border-bottom: 2px solid #ddd;">Last Nudged</th>';
                html += '<th style="padding: 8px; border-bottom: 2px solid #ddd;">Actions</th>';
                html += '</tr></thead><tbody>';

                data.users.forEach(u => {{
                    const color = stepColors[u.onboarding_step] || '#95a5a6';
                    const created = u.created_at ? new Date(u.created_at).toLocaleDateString() : '—';
                    const lastAct = u.last_interaction && u.last_interaction !== 'Never'
                        ? new Date(u.last_interaction).toLocaleDateString()
                        : 'Never';
                    const phone = u.phone_number;

                    html += '<tr style="border-bottom: 1px solid #eee;">';
                    html += `<td style="padding: 8px; font-family: monospace;">***${{u.phone_last4}}</td>`;
                    html += `<td style="padding: 8px;">${{u.first_name}}</td>`;
                    html += `<td style="padding: 8px;"><span style="background: ${{color}}; color: white; padding: 2px 8px; border-radius: 12px; font-size: 0.85em;">${{u.step_label}}</span></td>`;
                    html += `<td style="padding: 8px;">${{created}}</td>`;
                    html += `<td style="padding: 8px;">${{lastAct}}</td>`;
                    html += `<td style="padding: 8px;">${{u.referral_source}}</td>`;
                    const lastNudged = u.last_nudged ? new Date(u.last_nudged).toLocaleString() : 'Never';
                    html += `<td style="padding: 8px; font-size: 0.85em; color: ${{u.last_nudged ? '#e67e22' : '#95a5a6'}};">${{lastNudged}}</td>`;
                    html += `<td style="padding: 8px; white-space: nowrap;">`;
                    html += `<button onclick="nudgePendingUser('${{phone}}')" style="background: #e67e22; color: white; border: none; padding: 3px 8px; border-radius: 3px; cursor: pointer; font-size: 0.85em; margin-right: 4px;">Nudge</button>`;
                    html += `<button onclick="deleteUser('${{phone}}')" style="background: #e74c3c; color: white; border: none; padding: 3px 8px; border-radius: 3px; cursor: pointer; font-size: 0.85em;">Remove</button>`;
                    html += `</td>`;
                    html += '</tr>';
                }});

                html += '</tbody></table>';
                content.innerHTML = html;
            }} catch (err) {{
                content.innerHTML = `<p style="color: #e74c3c;">Error loading data: ${{err.message}}</p>`;
            }}
        }}

        function toggleCustomNudge() {{
            const sel = document.getElementById('nudgePreset');
            const custom = document.getElementById('nudgeCustomMsg');
            custom.style.display = sel.value === 'custom' ? 'block' : 'none';
        }}

        function getNudgeMessage() {{
            const sel = document.getElementById('nudgePreset');
            if (sel.value === 'custom') {{
                return document.getElementById('nudgeCustomMsg').value.trim();
            }}
            return sel.options[sel.selectedIndex].text;
        }}

        async function nudgePendingUser(phone) {{
            const msg = getNudgeMessage();
            if (!msg) {{ alert('Please enter a nudge message.'); return; }}
            if (!confirm(`Send nudge to ***${{phone.slice(-4)}}?\n\n"${{msg}}"`)) return;

            try {{
                const response = await fetch('/admin/users/pending-onboarding/nudge', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json',
                        'Authorization': 'Basic ' + btoa('{ADMIN_USERNAME}:{ADMIN_PASSWORD}')
                    }},
                    body: JSON.stringify({{ phone_numbers: [phone], message: msg }})
                }});
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Server error');
                alert(data.message);
                viewPendingOnboarding();
            }} catch (err) {{
                alert('Error: ' + err.message);
            }}
        }}

        async function nudgeAllPending() {{
            const msg = getNudgeMessage();
            if (!msg) {{ alert('Please enter a nudge message.'); return; }}
            const phones = pendingOnboardingUsers.map(u => u.phone_number);
            if (!confirm(`Send nudge to ${{phones.length}} user(s)?\n\n"${{msg}}"`)) return;

            try {{
                const response = await fetch('/admin/users/pending-onboarding/nudge', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json',
                        'Authorization': 'Basic ' + btoa('{ADMIN_USERNAME}:{ADMIN_PASSWORD}')
                    }},
                    body: JSON.stringify({{ phone_numbers: phones, message: msg }})
                }});
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Server error');
                alert(data.message);
                viewPendingOnboarding();
            }} catch (err) {{
                alert('Error: ' + err.message);
            }}
        }}

        async function deleteUser(phone) {{
            if (!confirm(`Delete user ${{phone}} and ALL their data? This cannot be undone.`)) return;
            if (!confirm(`Are you SURE? This will permanently delete all reminders, lists, memories, and account data for ${{phone}}.`)) return;

            try {{
                const response = await fetch(`/admin/users/${{encodeURIComponent(phone)}}`, {{
                    method: 'DELETE',
                    headers: {{ 'Authorization': 'Basic ' + btoa('{ADMIN_USERNAME}:{ADMIN_PASSWORD}') }}
                }});
                const data = await response.json();
                if (response.ok && data.success) {{
                    alert(data.message);
                    csSearch();
                }} else {{
                    alert('Failed to delete: ' + (data.detail || 'Unknown error'));
                }}
            }} catch (e) {{
                alert('Error: ' + e.message);
            }}
        }}

        // =====================================================
        // CUSTOMER SERVICE FUNCTIONS
        // =====================================================
        let csCurrentPhone = null;

        async function csSearch() {{
            const query = document.getElementById('csSearchInput').value.trim();
            if (query.length < 2) {{
                alert('Enter at least 2 characters to search');
                return;
            }}

            try {{
                const response = await fetch(`/admin/cs/search?q=${{encodeURIComponent(query)}}`);
                const data = await response.json();

                const resultsDiv = document.getElementById('csSearchResults');
                const tbody = document.getElementById('csResultsBody');
                const countSpan = document.getElementById('csResultCount');

                tbody.innerHTML = '';
                countSpan.textContent = `(${{data.count || 0}} found)`;

                if (!data.customers || data.customers.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="6" style="color: #95a5a6; text-align: center;">No customers found</td></tr>';
                }} else {{
                    for (const c of data.customers) {{
                        const row = document.createElement('tr');
                        const tierColor = c.tier === 'premium' ? '#9b59b6' : (c.tier === 'family' ? '#3498db' : '#95a5a6');
                        row.innerHTML = `
                            <td>${{c.phone_masked || '***'}}</td>
                            <td>${{c.first_name || ''}} ${{c.last_name || ''}}</td>
                            <td><span style="color: ${{tierColor}}; font-weight: 500;">${{c.tier || 'free'}}</span></td>
                            <td>${{c.subscription_status || '-'}}</td>
                            <td style="font-size: 0.85em;">${{c.last_active_at ? new Date(c.last_active_at).toLocaleDateString() : '-'}}</td>
                            <td>
                                <button class="btn" style="padding: 4px 12px; font-size: 0.85em;" onclick="csViewCustomer('${{c.phone}}')">View</button>
                                <button class="btn" style="padding: 4px 12px; font-size: 0.85em; background: #e74c3c; color: white;" onclick="deleteUser('${{c.phone}}')">Delete</button>
                            </td>
                        `;
                        tbody.appendChild(row);
                    }}
                }}

                resultsDiv.style.display = 'block';
                document.getElementById('csCustomerProfile').style.display = 'none';
            }} catch (e) {{
                alert('Search error: ' + e.message);
            }}
        }}

        async function csViewCustomer(phone) {{
            csCurrentPhone = phone;

            try {{
                const response = await fetch(`/admin/cs/customer/${{encodeURIComponent(phone)}}`);
                const data = await response.json();

                // Profile Info
                document.getElementById('csProfileInfo').innerHTML = `
                    <div><strong>Phone:</strong> ${{data.phone_masked}}</div>
                    <div><strong>Name:</strong> ${{data.first_name || '-'}} ${{data.last_name || ''}}</div>
                    <div><strong>Email:</strong> ${{data.email || '-'}}</div>
                    <div><strong>Timezone:</strong> ${{data.timezone || '-'}}</div>
                    <div><strong>Joined:</strong> ${{data.created_at ? new Date(data.created_at).toLocaleDateString() : '-'}}</div>
                    <div><strong>Last Active:</strong> ${{data.last_active_at ? new Date(data.last_active_at).toLocaleDateString() : '-'}}</div>
                    <div><strong>Tier:</strong> <span style="color: ${{data.tier === 'premium' ? '#9b59b6' : '#3498db'}}; font-weight: 500;">${{data.tier}}</span></div>
                    <div><strong>Subscription Status:</strong> ${{data.subscription_status || '-'}}</div>
                    ${{data.opted_out ? `
                        <div style="margin-top: 8px; padding: 8px 12px; background: #fdedec; border: 1px solid #e74c3c; border-radius: 4px;">
                            <span style="color: #e74c3c; font-weight: bold;">Opted Out</span>
                            <span style="color: #7f8c8d; font-size: 0.85em; margin-left: 6px;">${{data.opted_out_at ? 'since ' + new Date(data.opted_out_at).toLocaleDateString() : ''}}</span>
                            <button onclick="clearOptedOut()" style="margin-left: 10px; background: #e74c3c; color: white; border: none; padding: 4px 10px; border-radius: 3px; font-size: 0.8em; cursor: pointer;">Clear Flag</button>
                        </div>
                    ` : ''}}
                `;

                // Stats
                document.getElementById('csProfileStats').innerHTML = `
                    <div><strong>Total Reminders:</strong> ${{data.stats.reminders}} (${{data.stats.pending_reminders}} pending)</div>
                    <div><strong>Recurring Reminders:</strong> ${{data.stats.recurring_reminders}}</div>
                    <div><strong>Lists:</strong> ${{data.stats.lists}}</div>
                    <div><strong>Memories:</strong> ${{data.stats.memories}}</div>
                    <div><strong>Total Messages:</strong> ${{data.total_messages}}</div>
                `;

                // Set tier dropdown
                document.getElementById('csTierSelect').value = data.tier;

                // Notes
                const notesList = document.getElementById('csNotesList');
                if (data.notes && data.notes.length > 0) {{
                    notesList.innerHTML = data.notes.map(n => `
                        <div style="padding: 8px; background: #f8f9fa; border-radius: 4px; margin-bottom: 8px;">
                            <div style="font-size: 0.85em; color: #7f8c8d;">${{new Date(n.created_at).toLocaleString()}} by ${{n.created_by || 'Unknown'}}</div>
                            <div>${{n.note}}</div>
                        </div>
                    `).join('');
                }} else {{
                    notesList.innerHTML = '<div style="color: #95a5a6;">No notes yet</div>';
                }}

                // Recent Messages
                const msgBody = document.getElementById('csMessagesBody');
                if (data.recent_messages && data.recent_messages.length > 0) {{
                    msgBody.innerHTML = data.recent_messages.map(m => `
                        <tr>
                            <td style="font-size: 0.85em;">${{new Date(m.timestamp).toLocaleString()}}</td>
                            <td>${{m.message_in || '-'}}</td>
                            <td style="max-width: 300px; overflow: hidden; text-overflow: ellipsis;">${{m.message_out || '-'}}</td>
                            <td><span style="background: #ecf0f1; padding: 2px 6px; border-radius: 3px; font-size: 0.8em;">${{m.intent || '-'}}</span></td>
                        </tr>
                    `).join('');
                }} else {{
                    msgBody.innerHTML = '<tr><td colspan="4" style="color: #95a5a6; text-align: center;">No messages</td></tr>';
                }}

                // Load default tab
                csShowTab('reminders');

                // Show profile, hide search results
                document.getElementById('csSearchResults').style.display = 'none';
                document.getElementById('csCustomerProfile').style.display = 'block';
            }} catch (e) {{
                alert('Error loading customer: ' + e.message);
            }}
        }}

        function csCloseProfile() {{
            document.getElementById('csCustomerProfile').style.display = 'none';
            document.getElementById('csSearchResults').style.display = 'block';
            csCurrentPhone = null;
        }}

        async function csShowTab(tab) {{
            // Update button styles
            ['reminders', 'lists', 'memories'].forEach(t => {{
                const btn = document.getElementById('csTab' + t.charAt(0).toUpperCase() + t.slice(1));
                if (t === tab) {{
                    btn.style.background = '#3498db';
                    btn.style.color = 'white';
                    btn.classList.remove('btn-secondary');
                }} else {{
                    btn.style.background = '';
                    btn.style.color = '';
                    btn.classList.add('btn-secondary');
                }}
            }});

            // Hide all tabs
            document.getElementById('csRemindersTab').style.display = 'none';
            document.getElementById('csListsTab').style.display = 'none';
            document.getElementById('csMemoriesTab').style.display = 'none';
            document.getElementById('csNudgesTab').style.display = 'none';

            // Load and show selected tab
            const tabDiv = document.getElementById('cs' + tab.charAt(0).toUpperCase() + tab.slice(1) + 'Tab');
            tabDiv.style.display = 'block';

            if (!csCurrentPhone) return;

            try {{
                const response = await fetch(`/admin/cs/customer/${{encodeURIComponent(csCurrentPhone)}}/${{tab}}`);
                const data = await response.json();

                if (tab === 'reminders') {{
                    if (data.reminders && data.reminders.length > 0) {{
                        tabDiv.innerHTML = `<table class="history-table">
                            <thead><tr><th>ID</th><th>Text</th><th>Date</th><th>Status</th><th>Actions</th></tr></thead>
                            <tbody>${{data.reminders.map(r => `
                                <tr>
                                    <td>${{r.id}}</td>
                                    <td>${{r.text}}</td>
                                    <td>${{new Date(r.date).toLocaleString()}}</td>
                                    <td>${{r.sent ? '<span style="color:#27ae60">Sent</span>' : '<span style="color:#e67e22">Pending</span>'}}</td>
                                    <td>${{!r.sent ? `<button class="btn btn-danger" style="padding:2px 8px;font-size:0.8em;" onclick="csDeleteReminder(${{r.id}})">Delete</button>` : ''}}</td>
                                </tr>
                            `).join('')}}</tbody>
                        </table>`;
                    }} else {{
                        tabDiv.innerHTML = '<div style="color: #95a5a6; padding: 20px; text-align: center;">No reminders</div>';
                    }}
                }} else if (tab === 'lists') {{
                    if (data.lists && data.lists.length > 0) {{
                        tabDiv.innerHTML = data.lists.map(l => `
                            <div style="margin-bottom: 15px; padding: 15px; background: #f8f9fa; border-radius: 4px;">
                                <h4 style="margin: 0 0 10px;">${{l.name}}</h4>
                                ${{l.items.length > 0 ? `<ul style="margin: 0; padding-left: 20px;">${{l.items.map(i => `
                                    <li style="color: ${{i.completed ? '#95a5a6' : '#2c3e50'}}; ${{i.completed ? 'text-decoration: line-through;' : ''}}">${{i.text}}</li>
                                `).join('')}}</ul>` : '<div style="color: #95a5a6;">Empty list</div>'}}
                            </div>
                        `).join('');
                    }} else {{
                        tabDiv.innerHTML = '<div style="color: #95a5a6; padding: 20px; text-align: center;">No lists</div>';
                    }}
                }} else if (tab === 'memories') {{
                    if (data.memories && data.memories.length > 0) {{
                        tabDiv.innerHTML = `<table class="history-table">
                            <thead><tr><th>Memory</th><th>Created</th></tr></thead>
                            <tbody>${{data.memories.map(m => `
                                <tr>
                                    <td>${{m.text}}</td>
                                    <td style="font-size:0.85em;">${{new Date(m.created_at).toLocaleString()}}</td>
                                </tr>
                            `).join('')}}</tbody>
                        </table>`;
                    }} else {{
                        tabDiv.innerHTML = '<div style="color: #95a5a6; padding: 20px; text-align: center;">No memories</div>';
                    }}
                }} else if (tab === 'nudges') {{
                    const cfg = data.config || {{}};
                    const stats = data.stats_30d || {{}};
                    const recent = data.recent || [];
                    const daily = data.daily_counts || [];

                    // Compare actual sends to expected cadence to flag anomalies
                    let cadenceColor = '#27ae60';
                    let cadenceNote = 'Matches expected cadence';
                    if (cfg.expected_cadence === 'disabled' && stats.total > 0) {{
                        cadenceColor = '#e67e22';
                        cadenceNote = 'Sends recorded despite nudges being disabled — may be stale history';
                    }} else if (cfg.expected_cadence === 'weekly (Sundays only)' && stats.distinct_days > 5) {{
                        cadenceColor = '#e74c3c';
                        cadenceNote = `Free user receiving on ${{stats.distinct_days}} distinct days in last 30d — should be Sundays only (≤5)`;
                    }} else if (stats.days_with_multiple > 0) {{
                        cadenceColor = '#e74c3c';
                        cadenceNote = `${{stats.days_with_multiple}} day(s) with more than one nudge — possible duplicate sends`;
                    }} else if (cfg.expected_cadence === 'daily' && stats.distinct_days > 0) {{
                        cadenceNote = `${{stats.distinct_days}} of last 30 days received a nudge (daily expected)`;
                    }}

                    const dailyBars = daily.length > 0
                        ? daily.slice(0, 30).map(d => `
                            <div style="display: flex; align-items: center; gap: 10px; padding: 4px 0; font-size: 0.85em;">
                                <span style="color: #7f8c8d; min-width: 90px;">${{d.date}}</span>
                                <span style="background: ${{d.count > 1 ? '#e74c3c' : '#3498db'}}; color: white; padding: 2px 10px; border-radius: 10px; min-width: 30px; text-align: center;">${{d.count}}</span>
                                ${{d.count > 1 ? '<span style="color: #e74c3c; font-size: 0.85em;">⚠ multiple sends</span>' : ''}}
                            </div>
                        `).join('')
                        : '<div style="color: #95a5a6;">No sends in last 30 days</div>';

                    const recentRows = recent.length > 0
                        ? recent.map(n => {{
                            const responseCell = n.user_response
                                ? `<span style="color:#2c3e50;">${{n.user_response}}</span>` + (n.action_taken ? `<br><span style="color:#95a5a6; font-size:0.8em;">${{n.action_taken}}</span>` : '')
                                : '<span style="color:#bdc3c7;">—</span>';
                            return `<tr>
                                <td style="font-size:0.85em; white-space: nowrap;">${{new Date(n.sent_at).toLocaleString()}}</td>
                                <td><span style="background:#ecf0f1; padding: 2px 8px; border-radius: 10px; font-size: 0.8em;">${{n.type}}</span></td>
                                <td style="max-width: 400px;">${{n.text}}</td>
                                <td>${{responseCell}}</td>
                            </tr>`;
                          }}).join('')
                        : '<tr><td colspan="4" style="color:#95a5a6; text-align:center; padding:20px;">No nudges sent yet</td></tr>';

                    const outboundByType = data.outbound_by_type || [];
                    const outboundByDay = data.outbound_by_day || [];

                    const outboundTypeRows = outboundByType.length > 0
                        ? outboundByType.map(t => `
                            <tr>
                                <td><span style="background:#ecf0f1; padding: 2px 8px; border-radius: 10px; font-size: 0.85em;">${{t.type}}</span></td>
                                <td><strong>${{t.total}}</strong></td>
                                <td>${{t.distinct_days}}</td>
                                <td style="font-size:0.85em;">${{t.last_sent ? new Date(t.last_sent).toLocaleString() : '—'}}</td>
                            </tr>
                        `).join('')
                        : '<tr><td colspan="4" style="color:#95a5a6; text-align:center; padding:15px;">No outbound messages in last 30 days</td></tr>';

                    const outboundDayRows = outboundByDay.length > 0
                        ? outboundByDay.slice(0, 30).map(d => `
                            <div style="display: flex; align-items: center; gap: 10px; padding: 4px 0; font-size: 0.85em; border-bottom: 1px solid #f4f4f4;">
                                <span style="color: #7f8c8d; min-width: 90px;">${{d.date}}</span>
                                <span style="background: ${{d.count > 1 ? '#e67e22' : '#3498db'}}; color: white; padding: 2px 10px; border-radius: 10px; min-width: 30px; text-align: center;">${{d.count}}</span>
                                <span style="color: #2c3e50; font-size: 0.85em;">${{d.types}}</span>
                            </div>
                        `).join('')
                        : '<div style="color: #95a5a6;">No outbound messages in last 30 days</div>';

                    tabDiv.innerHTML = `
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-bottom: 20px;">
                            <div style="background: #f8f9fa; padding: 15px; border-radius: 4px;">
                                <h4 style="margin: 0 0 10px; color: #2c3e50;">Configuration</h4>
                                <div style="line-height: 1.7; font-size: 0.9em;">
                                    <div><strong>Tier:</strong> ${{cfg.tier}}${{cfg.trial_active ? ' (trial active)' : ''}}</div>
                                    <div><strong>Trial ends:</strong> ${{cfg.trial_end_date || '—'}}</div>
                                    <div><strong>Smart nudges:</strong> ${{cfg.smart_nudges_enabled ? 'ON' : 'OFF'}}</div>
                                    <div><strong>Nudge time:</strong> ${{cfg.smart_nudge_time || '—'}} (${{cfg.timezone || '—'}})</div>
                                    <div><strong>Daily summary:</strong> ${{cfg.daily_summary_enabled ? 'ON' : 'OFF'}}</div>
                                    <div><strong>Opted out:</strong> ${{cfg.opted_out ? 'YES' : 'no'}}</div>
                                    <div style="margin-top: 8px; padding-top: 8px; border-top: 1px solid #ddd;"><strong>Expected cadence (smart_nudge):</strong> ${{cfg.expected_cadence}}</div>
                                </div>
                            </div>
                            <div style="background: #f8f9fa; padding: 15px; border-radius: 4px;">
                                <h4 style="margin: 0 0 10px; color: #2c3e50;">Smart nudges, last 30 days</h4>
                                <div style="line-height: 1.7; font-size: 0.9em;">
                                    <div><strong>Total sent:</strong> ${{stats.total || 0}}</div>
                                    <div><strong>Distinct days:</strong> ${{stats.distinct_days || 0}}</div>
                                    <div><strong>Days with >1 send:</strong> ${{stats.days_with_multiple || 0}}</div>
                                </div>
                                <div style="margin-top: 12px; padding: 10px; background: ${{cadenceColor}}; color: white; border-radius: 4px; font-size: 0.85em;">
                                    ${{cadenceNote}}
                                </div>
                            </div>
                        </div>

                        <h4 style="margin: 20px 0 10px; color: #2c3e50;">All outbound messages by type (last 30d)</h4>
                        <div style="background: #fff8e1; border-left: 3px solid #f39c12; padding: 8px 12px; margin-bottom: 10px; font-size: 0.85em; color: #555;">
                            Includes lifecycle, broadcast, billing, replies, etc. — not just smart nudges. Use this to find which message type is firing.
                        </div>
                        <div style="overflow-x: auto; margin-bottom: 20px;">
                            <table class="history-table">
                                <thead><tr><th>Message type</th><th>Total sends</th><th>Distinct days</th><th>Last sent</th></tr></thead>
                                <tbody>${{outboundTypeRows}}</tbody>
                            </table>
                        </div>

                        <h4 style="margin: 20px 0 10px; color: #2c3e50;">All outbound messages by day (last 30d)</h4>
                        <div style="background: white; padding: 10px 15px; border-radius: 4px; max-height: 280px; overflow-y: auto; border: 1px solid #ecf0f1; margin-bottom: 20px;">
                            ${{outboundDayRows}}
                        </div>

                        <h4 style="margin: 20px 0 10px; color: #2c3e50;">Smart nudge daily counts</h4>
                        <div style="background: white; padding: 10px 15px; border-radius: 4px; max-height: 240px; overflow-y: auto; border: 1px solid #ecf0f1; margin-bottom: 20px;">
                            ${{dailyBars}}
                        </div>

                        <h4 style="margin: 20px 0 10px; color: #2c3e50;">Recent smart nudges</h4>
                        <div style="overflow-x: auto;">
                            <table class="history-table">
                                <thead><tr><th>Sent</th><th>Type</th><th>Text</th><th>User response</th></tr></thead>
                                <tbody>${{recentRows}}</tbody>
                            </table>
                        </div>
                    `;
                }}
            }} catch (e) {{
                tabDiv.innerHTML = `<div style="color: #e74c3c;">Error loading ${{tab}}: ${{e.message}}</div>`;
            }}
        }}

        function toggleTrialDatePicker() {{
            const checkbox = document.getElementById('csTrialMode');
            const datePicker = document.getElementById('csTrialEndDate');
            const tierSelect = document.getElementById('csTierSelect');

            if (checkbox.checked) {{
                datePicker.style.display = 'block';
                // Default to 14 days from now
                const defaultDate = new Date();
                defaultDate.setDate(defaultDate.getDate() + 14);
                datePicker.value = defaultDate.toISOString().split('T')[0];
                // Auto-select premium if free is selected
                if (tierSelect.value === 'free') {{
                    tierSelect.value = 'premium';
                }}
            }} else {{
                datePicker.style.display = 'none';
                datePicker.value = '';
            }}
        }}

        async function clearOptedOut() {{
            if (!csCurrentPhone) return;
            if (!confirm('Clear the opted-out flag for this user? They will be included in future broadcasts.')) return;

            try {{
                const response = await fetch(`/admin/cs/customer/${{encodeURIComponent(csCurrentPhone)}}/clear-opted-out`, {{
                    method: 'POST'
                }});
                if (!response.ok) throw new Error('Failed to clear flag');
                alert('Opted-out flag cleared successfully.');
                csLoadCustomer(csCurrentPhone);
            }} catch (e) {{
                alert('Error clearing opted-out flag: ' + e.message);
            }}
        }}

        async function csUpdateTier() {{
            if (!csCurrentPhone) return;

            const tier = document.getElementById('csTierSelect').value;
            const reason = document.getElementById('csTierReason').value;
            const isTrialMode = document.getElementById('csTrialMode').checked;
            const trialEndDate = document.getElementById('csTrialEndDate').value;

            // Validate trial mode
            if (isTrialMode && tier === 'free') {{
                alert('Cannot set a trial for Free tier. Please select Premium or Family.');
                return;
            }}

            if (isTrialMode && !trialEndDate) {{
                alert('Please select a trial end date.');
                return;
            }}

            const confirmMsg = isTrialMode
                ? `Set this customer to ${{tier}} trial until ${{trialEndDate}}?`
                : `Change this customer to ${{tier}} tier?`;

            if (!confirm(confirmMsg)) return;

            try {{
                const body = {{ tier, reason }};
                if (isTrialMode && trialEndDate) {{
                    body.trial_end_date = trialEndDate;
                }}

                const response = await fetch(`/admin/cs/customer/${{encodeURIComponent(csCurrentPhone)}}/tier`, {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(body)
                }});
                const data = await response.json();
                alert(data.message);
                document.getElementById('csTierReason').value = '';
                document.getElementById('csTrialMode').checked = false;
                document.getElementById('csTrialEndDate').style.display = 'none';
                document.getElementById('csTrialEndDate').value = '';
                csViewCustomer(csCurrentPhone); // Refresh
            }} catch (e) {{
                alert('Error: ' + e.message);
            }}
        }}

        async function csAddNote() {{
            if (!csCurrentPhone) return;

            const note = document.getElementById('csNewNote').value.trim();
            if (!note) {{
                alert('Enter a note');
                return;
            }}

            try {{
                const response = await fetch(`/admin/cs/customer/${{encodeURIComponent(csCurrentPhone)}}/notes`, {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ note }})
                }});
                const data = await response.json();
                document.getElementById('csNewNote').value = '';
                csViewCustomer(csCurrentPhone); // Refresh
            }} catch (e) {{
                alert('Error: ' + e.message);
            }}
        }}

        async function csDeleteReminder(reminderId) {{
            if (!csCurrentPhone) return;
            if (!confirm('Delete this reminder?')) return;

            try {{
                const response = await fetch(`/admin/cs/customer/${{encodeURIComponent(csCurrentPhone)}}/reminder/${{reminderId}}`, {{
                    method: 'DELETE'
                }});
                const data = await response.json();
                csShowTab('reminders'); // Refresh
            }} catch (e) {{
                alert('Error: ' + e.message);
            }}
        }}
        // =====================================================
        // Product Metrics
        // =====================================================
        async function loadProductMetrics() {{
            const loading = document.getElementById('pmLoading');
            const error = document.getElementById('pmError');
            const content = document.getElementById('pmContent');

            loading.style.display = 'block';
            error.style.display = 'none';
            content.style.display = 'none';

            try {{
                const response = await fetch('/admin/product-metrics');
                const data = await response.json();

                if (data.error) {{
                    loading.style.display = 'none';
                    error.style.display = 'block';
                    document.getElementById('pmErrorMsg').textContent = data.error;
                    return;
                }}

                loading.style.display = 'none';
                content.style.display = 'block';

                // --- 1. Trial Conversion Funnel ---
                const funnel = data.funnel || {{}};
                const funnelItems = [
                    {{ label: 'Active Trial', count: funnel.active_trial || 0, color: '#4A90A4' }},
                    {{ label: 'Trial Expired (Active)', count: funnel.trial_expired_active || 0, color: '#50B688' }},
                    {{ label: 'Converted to Paid', count: funnel.converted_paid || 0, color: '#27ae60' }},
                    {{ label: 'Churned', count: funnel.churned || 0, color: '#e74c3c' }},
                ];
                const funnelTotal = funnel.total || 1;

                // Stacked bar
                const funnelBar = document.getElementById('pmFunnelBar');
                funnelBar.innerHTML = funnelItems.map(f => {{
                    const pct = (f.count / funnelTotal * 100);
                    if (pct < 1) return '';
                    return `<div style="width: ${{pct}}%; background: ${{f.color}}; display: flex; align-items: center; justify-content: center; color: white; font-size: 0.8em; font-weight: 600; min-width: 40px;" title="${{f.label}}: ${{f.count}} (${{pct.toFixed(1)}}%)">${{f.count}}</div>`;
                }}).join('');

                // Table
                document.getElementById('pmFunnelBody').innerHTML = funnelItems.map(f => {{
                    const pct = (f.count / funnelTotal * 100).toFixed(1);
                    return `<tr><td><span style="display: inline-block; width: 12px; height: 12px; border-radius: 2px; background: ${{f.color}}; margin-right: 8px; vertical-align: middle;"></span>${{f.label}}</td><td>${{f.count}}</td><td>${{pct}}%</td></tr>`;
                }}).join('') + `<tr style="font-weight: 600;"><td>Total</td><td>${{funnelTotal}}</td><td>100%</td></tr>`;

                // --- 2. Attribution ---
                const attr = data.attribution || [];
                const attrMax = Math.max(...attr.map(a => a.total), 1);
                document.getElementById('pmAttributionChart').innerHTML = attr.map(a => {{
                    const barW = (a.total / attrMax * 100);
                    return `<div style="display: flex; align-items: center; margin-bottom: 8px;">
                        <span style="width: 120px; font-size: 0.85em; color: #2c3e50; font-weight: 500;">${{a.keyword}}</span>
                        <div style="flex: 1; background: #f0f0f0; border-radius: 4px; height: 24px; overflow: hidden;">
                            <div style="width: ${{barW}}%; background: #4A90A4; height: 100%; border-radius: 4px; display: flex; align-items: center; padding-left: 8px; color: white; font-size: 0.8em; font-weight: 600; min-width: 30px;">${{a.total}}</div>
                        </div>
                        <span style="width: 100px; text-align: right; font-size: 0.8em; color: #7f8c8d; margin-left: 8px;">${{a.channel}}</span>
                    </div>`;
                }}).join('');
                document.getElementById('pmAttributionBody').innerHTML = attr.map(a =>
                    `<tr><td>${{a.keyword}}</td><td>${{a.channel}}</td><td>${{a.total}}</td><td>${{a.last_7d}}</td><td>${{a.last_30d}}</td></tr>`
                ).join('');

                // --- 3. DAU Trend ---
                const dau = data.dau_trend || [];
                const dauMax = Math.max(...dau.map(d => d.active_users), 1);
                const dauChart = document.getElementById('pmDAUChart');
                const dauLabels = document.getElementById('pmDAULabels');
                const barWidth = `${{100 / Math.max(dau.length, 1)}}%`;
                dauChart.innerHTML = dau.map((d, i) => {{
                    const h = Math.max(d.active_users / dauMax * 180, 2);
                    const isToday = i === dau.length - 1;
                    return `<div style="flex: 1; background: ${{isToday ? '#4A90A4' : '#b0d4e8'}}; height: ${{h}}px; border-radius: 2px 2px 0 0; position: relative;" title="${{d.date}}: ${{d.active_users}} users">
                        <span style="position: absolute; top: -18px; left: 50%; transform: translateX(-50%); font-size: 0.7em; color: #2c3e50; font-weight: 600;">${{d.active_users > 0 ? d.active_users : ''}}</span>
                    </div>`;
                }}).join('');
                dauLabels.innerHTML = dau.map((d, i) => {{
                    const show = i === 0 || i === dau.length - 1 || i % 7 === 0;
                    return `<div style="flex: 1; text-align: center; font-size: 0.65em; color: #95a5a6;">${{show ? d.date.slice(5) : ''}}</div>`;
                }}).join('');

                // --- 4. Retention ---
                const ret = data.retention || {{}};
                document.getElementById('pmRetTotal').textContent = ret.total_onboarded || 0;
                document.getElementById('pmRetActive').textContent = ret.currently_active || 0;
                document.getElementById('pmRetChurned').textContent = ret.churned || 0;

                const retRows = [
                    ['Day 1 Retention', ret.day1],
                    ['Day 7 Retention', ret.day7],
                    ['Day 14 Retention', ret.day14],
                ];
                document.getElementById('pmRetentionBody').innerHTML = retRows.map(([label, d]) => {{
                    if (!d) return `<tr><td>${{label}}</td><td colspan="3" style="color: #95a5a6;">Not enough data</td></tr>`;
                    const rateColor = d.rate >= 50 ? '#27ae60' : d.rate >= 25 ? '#e67e22' : '#e74c3c';
                    return `<tr><td>${{label}}</td><td>${{d.eligible}}</td><td>${{d.retained}}</td><td style="color: ${{rateColor}}; font-weight: 600;">${{d.rate !== null ? d.rate + '%' : 'N/A'}}</td></tr>`;
                }}).join('');

                // Cohorts
                const cohorts = data.cohorts || [];
                if (cohorts.length === 0) {{
                    document.getElementById('pmCohortBody').innerHTML = '<tr><td colspan="5" style="color: #95a5a6; text-align: center;">Not enough data for cohort analysis</td></tr>';
                }} else {{
                    document.getElementById('pmCohortBody').innerHTML = cohorts.map(c => {{
                        const fmtPct = (v) => v !== null && v !== undefined ? `<span style="color: ${{v >= 50 ? '#27ae60' : v >= 25 ? '#e67e22' : '#e74c3c'}}; font-weight: 600;">${{v}}%</span>` : '<span style="color: #95a5a6;">-</span>';
                        return `<tr><td>${{c.week}}</td><td>${{c.size}}</td><td>${{fmtPct(c.day1_pct)}}</td><td>${{fmtPct(c.day7_pct)}}</td><td>${{fmtPct(c.day14_pct)}}</td></tr>`;
                    }}).join('');
                }}

                // --- 5. Time to First Action ---
                const tta = data.time_to_action || {{}};
                document.getElementById('pmTTAReminder').textContent = tta.median_to_reminder || 'N/A';
                document.getElementById('pmTTAMemory').textContent = tta.median_to_memory || 'N/A';
                document.getElementById('pmTTA1h').textContent = (tta.pct_within_1h || 0) + '%';
                document.getElementById('pmTTA24h').textContent = (tta.pct_within_24h || 0) + '%';
                document.getElementById('pmTTANever').textContent = (tta.pct_never_reminder || 0) + '%';

            }} catch (e) {{
                loading.style.display = 'none';
                error.style.display = 'block';
                document.getElementById('pmErrorMsg').textContent = e.message;
            }}
        }}

        // =====================================================
        // Website Analytics
        // =====================================================
        let analyticsRangeDays = 7;

        function setAnalyticsRange(days) {{
            analyticsRangeDays = days;
            document.querySelectorAll('.analytics-range-btn').forEach(b => b.classList.remove('active'));
            document.getElementById('analyticsRange' + days).classList.add('active');
            loadAnalyticsData();
        }}

        async function loadAnalyticsData() {{
            const loading = document.getElementById('analyticsLoading');
            const error = document.getElementById('analyticsError');
            const content = document.getElementById('analyticsContent');

            loading.style.display = 'block';
            error.style.display = 'none';
            content.style.display = 'none';

            try {{
                const response = await fetch('/admin/analytics/data?days=' + analyticsRangeDays);
                const data = await response.json();

                const ga4 = data.ga4 || {{}};
                const sc = data.search_console || {{}};

                if (ga4.error) {{
                    loading.style.display = 'none';
                    error.style.display = 'block';
                    document.getElementById('analyticsErrorMsg').textContent = ga4.error;
                    return;
                }}

                loading.style.display = 'none';
                content.style.display = 'block';

                // Summary cards
                const totals = ga4.totals || {{}};
                document.getElementById('anTotalUsers').textContent = (totals.total_users || 0).toLocaleString();
                document.getElementById('anTotalSessions').textContent = (totals.total_sessions || 0).toLocaleString();
                const engTime = totals.avg_engagement_time || 0;
                document.getElementById('anAvgEngagement').textContent = engTime >= 60 ? Math.round(engTime / 60) + 'm ' + Math.round(engTime % 60) + 's' : Math.round(engTime) + 's';
                document.getElementById('anEngagementRate').textContent = (totals.engagement_rate || 0) + '%';

                // Daily traffic chart
                renderTrafficChart(ga4.daily_trend || []);

                // A/B test
                renderABTest(ga4.ab_variants || [], ga4.ab_landing_pages || []);

                // Traffic sources table
                renderTable('trafficSourcesBody', ga4.traffic_sources || [], [
                    {{ key: 'source_medium', label: 'Source/Medium', highlight: v => /\/(cpc|paid)/.test(v) }},
                    {{ key: 'sessions' }},
                    {{ key: 'users' }},
                    {{ key: 'engagement_rate', format: v => typeof v === 'number' ? (v > 1 ? v.toFixed(1) : (v * 100).toFixed(1)) + '%' : v }},
                    {{ key: 'avg_engagement_time', format: v => typeof v === 'number' ? Math.round(v) + 's' : v }},
                ]);

                // Landing pages table
                renderTable('landingPagesBody', ga4.landing_pages || [], [
                    {{ key: 'page' }},
                    {{ key: 'sessions' }},
                    {{ key: 'users' }},
                    {{ key: 'engagement_rate', format: v => typeof v === 'number' ? (v > 1 ? v.toFixed(1) : (v * 100).toFixed(1)) + '%' : v }},
                    {{ key: 'avg_engagement_time', format: v => typeof v === 'number' ? Math.round(v) + 's' : v }},
                ]);

                // Device breakdown
                const devices = ga4.devices || [];
                const totalDeviceSessions = devices.reduce((sum, d) => sum + (d.sessions || 0), 0);
                renderTable('devicesBody', devices, [
                    {{ key: 'device_category' }},
                    {{ key: 'operating_system' }},
                    {{ key: 'sessions' }},
                    {{ key: 'users' }},
                    {{ key: 'sessions', format: v => totalDeviceSessions ? ((v / totalDeviceSessions) * 100).toFixed(1) + '%' : '0%' }},
                ]);

                // Key events
                renderTable('keyEventsBody', ga4.key_events || [], [
                    {{ key: 'event_name' }},
                    {{ key: 'event_count' }},
                    {{ key: 'users' }},
                ]);

                // Search Console - queries
                if (sc.error) {{
                    document.getElementById('searchQueriesBody').innerHTML = '<tr><td colspan="5" style="color: #95a5a6; text-align: center;">' + sc.error + '</td></tr>';
                    document.getElementById('searchPagesBody').innerHTML = '<tr><td colspan="5" style="color: #95a5a6; text-align: center;">' + sc.error + '</td></tr>';
                }} else {{
                    renderTable('searchQueriesBody', sc.top_queries || [], [
                        {{ key: 'query' }},
                        {{ key: 'impressions' }},
                        {{ key: 'clicks' }},
                        {{ key: 'ctr', format: v => v + '%' }},
                        {{ key: 'position' }},
                    ]);
                    renderTable('searchPagesBody', sc.top_pages || [], [
                        {{ key: 'page' }},
                        {{ key: 'impressions' }},
                        {{ key: 'clicks' }},
                        {{ key: 'ctr', format: v => v + '%' }},
                        {{ key: 'position' }},
                    ]);
                }}
            }} catch (e) {{
                loading.style.display = 'none';
                error.style.display = 'block';
                document.getElementById('analyticsErrorMsg').textContent = 'Failed to load analytics: ' + e.message;
            }}
        }}

        function renderTable(tbodyId, rows, columns) {{
            const tbody = document.getElementById(tbodyId);
            if (!rows.length) {{
                tbody.innerHTML = '<tr><td colspan="' + columns.length + '" style="color: #95a5a6; text-align: center;">No data</td></tr>';
                return;
            }}
            tbody.innerHTML = rows.map(row => {{
                const cells = columns.map(col => {{
                    let val = row[col.key];
                    if (val === undefined || val === null) val = '-';
                    const displayVal = col.format ? col.format(val) : val;
                    const style = col.highlight && col.highlight(String(val)) ? 'color: #e67e22; font-weight: 600;' : '';
                    return '<td style="' + style + '">' + displayVal + '</td>';
                }}).join('');
                return '<tr>' + cells + '</tr>';
            }}).join('');
        }}

        function renderTrafficChart(dailyData) {{
            const canvas = document.getElementById('trafficChart');
            const ctx = canvas.getContext('2d');

            // Set actual pixel dimensions
            const rect = canvas.parentElement.getBoundingClientRect();
            canvas.width = rect.width - 40;
            canvas.height = 200;

            ctx.clearRect(0, 0, canvas.width, canvas.height);

            if (!dailyData.length) {{
                ctx.fillStyle = '#95a5a6';
                ctx.font = '14px sans-serif';
                ctx.textAlign = 'center';
                ctx.fillText('No data available', canvas.width / 2, 100);
                return;
            }}

            const maxSessions = Math.max(...dailyData.map(d => d.sessions || 0), 1);
            const maxUsers = Math.max(...dailyData.map(d => d.users || 0), 1);
            const maxVal = Math.max(maxSessions, maxUsers);

            const padding = {{ left: 50, right: 20, top: 20, bottom: 40 }};
            const chartW = canvas.width - padding.left - padding.right;
            const chartH = canvas.height - padding.top - padding.bottom;
            const stepX = chartW / Math.max(dailyData.length - 1, 1);

            // Grid lines
            ctx.strokeStyle = '#ecf0f1';
            ctx.lineWidth = 1;
            for (let i = 0; i <= 4; i++) {{
                const y = padding.top + (chartH / 4) * i;
                ctx.beginPath();
                ctx.moveTo(padding.left, y);
                ctx.lineTo(canvas.width - padding.right, y);
                ctx.stroke();
                ctx.fillStyle = '#95a5a6';
                ctx.font = '11px sans-serif';
                ctx.textAlign = 'right';
                ctx.fillText(Math.round(maxVal * (1 - i / 4)), padding.left - 8, y + 4);
            }}

            // Sessions line (blue)
            ctx.strokeStyle = '#4A90A4';
            ctx.lineWidth = 2;
            ctx.beginPath();
            dailyData.forEach((d, i) => {{
                const x = padding.left + i * stepX;
                const y = padding.top + chartH - (((d.sessions || 0) / maxVal) * chartH);
                if (i === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            }});
            ctx.stroke();

            // Users line (green)
            ctx.strokeStyle = '#50B688';
            ctx.lineWidth = 2;
            ctx.beginPath();
            dailyData.forEach((d, i) => {{
                const x = padding.left + i * stepX;
                const y = padding.top + chartH - (((d.users || 0) / maxVal) * chartH);
                if (i === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            }});
            ctx.stroke();

            // X-axis labels
            ctx.fillStyle = '#7f8c8d';
            ctx.font = '10px sans-serif';
            ctx.textAlign = 'center';
            const labelInterval = Math.max(1, Math.floor(dailyData.length / 7));
            dailyData.forEach((d, i) => {{
                if (i % labelInterval === 0 || i === dailyData.length - 1) {{
                    const x = padding.left + i * stepX;
                    const dateStr = d.date || '';
                    const label = dateStr.length === 8 ? dateStr.substring(4, 6) + '/' + dateStr.substring(6) : dateStr;
                    ctx.fillText(label, x, canvas.height - 10);
                }}
            }});

            // Legend
            ctx.font = '12px sans-serif';
            ctx.fillStyle = '#4A90A4';
            ctx.fillRect(canvas.width - 180, 10, 12, 12);
            ctx.fillText('Sessions', canvas.width - 163, 21);
            ctx.fillStyle = '#50B688';
            ctx.fillRect(canvas.width - 100, 10, 12, 12);
            ctx.fillText('Users', canvas.width - 83, 21);
        }}

        function renderABTest(variants, landingPages) {{
            const aData = variants.find(v => v.variant === 'A') || {{}};
            const bData = variants.find(v => v.variant === 'B') || {{}};

            document.getElementById('abAUsers').textContent = (aData.users || 0).toLocaleString();
            document.getElementById('abACount').textContent = (aData.event_count || 0).toLocaleString();
            document.getElementById('abBUsers').textContent = (bData.users || 0).toLocaleString();
            document.getElementById('abBCount').textContent = (bData.event_count || 0).toLocaleString();

            // Show winner indicator
            const winnerEl = document.getElementById('abWinner');
            const winnerText = document.getElementById('abWinnerText');
            const aUsers = aData.users || 0;
            const bUsers = bData.users || 0;
            if (aUsers > 0 || bUsers > 0) {{
                winnerEl.style.display = 'block';
                if (aUsers > bUsers) {{
                    winnerText.textContent = 'Variant A leading (' + aUsers + ' vs ' + bUsers + ' users)';
                    winnerText.style.background = '#e8f4fd';
                    winnerText.style.color = '#4A90A4';
                }} else if (bUsers > aUsers) {{
                    winnerText.textContent = 'Variant B leading (' + bUsers + ' vs ' + aUsers + ' users)';
                    winnerText.style.background = '#e8f8f0';
                    winnerText.style.color = '#50B688';
                }} else {{
                    winnerText.textContent = 'Tied (' + aUsers + ' users each)';
                    winnerText.style.background = '#f0f0f0';
                    winnerText.style.color = '#7f8c8d';
                }}
            }} else {{
                winnerEl.style.display = 'none';
            }}
        }}

        async function clearAnalyticsCache() {{
            try {{
                await fetch('/admin/analytics/clear-cache', {{ method: 'POST' }});
                loadAnalyticsData();
            }} catch (e) {{
                alert('Error clearing cache: ' + e.message);
            }}
        }}

        async function exportAnalyticsJSON() {{
            try {{
                const response = await fetch('/admin/analytics/export?days=' + analyticsRangeDays);
                const data = await response.json();
                const json = JSON.stringify(data, null, 2);

                // Copy to clipboard
                if (navigator.clipboard) {{
                    await navigator.clipboard.writeText(json);
                    alert('Analytics JSON copied to clipboard! Paste into Claude for analysis.');
                }} else {{
                    // Fallback: open in new window
                    const w = window.open('', '_blank');
                    w.document.write('<pre>' + json + '</pre>');
                }}
            }} catch (e) {{
                alert('Error exporting: ' + e.message);
            }}
        }}

        // =====================================================
        // AI Analytics Summary
        // =====================================================

        async function loadAISummary() {{
            const loading = document.getElementById('aiSummaryLoading');
            const empty = document.getElementById('aiSummaryEmpty');
            const content = document.getElementById('aiSummaryContent');
            const history = document.getElementById('aiSummaryHistory');

            try {{
                const response = await fetch('/admin/analytics/ai-summary');
                const data = await response.json();

                if (data.status === 'no_summary') {{
                    empty.style.display = 'block';
                    content.style.display = 'none';
                    history.style.display = 'none';
                    return;
                }}

                if (data.error) {{
                    document.getElementById('aiSummaryStatus').textContent = 'Error: ' + data.error;
                    return;
                }}

                empty.style.display = 'none';
                content.style.display = 'block';
                history.style.display = 'none';
                renderAISummary(data);
            }} catch (e) {{
                document.getElementById('aiSummaryStatus').textContent = 'Failed to load: ' + e.message;
            }}
        }}

        function renderAISummary(data) {{
            // Summary text and date
            document.getElementById('aiSummaryText').textContent = data.summary_text || 'No summary available';
            document.getElementById('aiSummaryDate').textContent = data.summary_date ? 'Generated: ' + data.summary_date : '';

            // Metrics cards
            const metrics = data.metrics_snapshot || {{}};
            if (metrics.users) {{
                document.getElementById('aiMetricUsers').textContent = (metrics.users.current || 0).toLocaleString();
                renderChangeIndicator('aiMetricUsersChange', metrics.users.change_pct);
            }}
            if (metrics.sessions) {{
                document.getElementById('aiMetricSessions').textContent = (metrics.sessions.current || 0).toLocaleString();
                renderChangeIndicator('aiMetricSessionsChange', metrics.sessions.change_pct);
            }}
            if (metrics.engagement_rate) {{
                document.getElementById('aiMetricEngRate').textContent = metrics.engagement_rate.current + '%';
                renderChangeIndicator('aiMetricEngRateChange', metrics.engagement_rate.change_pct, 'pp');
            }}
            if (metrics.avg_engagement_time) {{
                const engTime = metrics.avg_engagement_time.current || 0;
                document.getElementById('aiMetricEngTime').textContent = engTime >= 60 ? Math.round(engTime / 60) + 'm ' + Math.round(engTime % 60) + 's' : Math.round(engTime) + 's';
                renderChangeIndicator('aiMetricEngTimeChange', metrics.avg_engagement_time.change_pct);
            }}

            // Trend banner
            const trends = data.trends || {{}};
            const banner = document.getElementById('aiTrendBanner');
            const direction = trends.direction || 'unknown';

            if (direction === 'up') {{
                banner.style.background = '#e8f8f0';
                banner.style.color = '#27ae60';
                document.getElementById('aiTrendIcon').textContent = '↑';
                document.getElementById('aiTrendLabel').textContent = 'Trending Up';
            }} else if (direction === 'down') {{
                banner.style.background = '#fdf2f2';
                banner.style.color = '#e74c3c';
                document.getElementById('aiTrendIcon').textContent = '↓';
                document.getElementById('aiTrendLabel').textContent = 'Trending Down';
            }} else {{
                banner.style.background = '#f0f4f8';
                banner.style.color = '#7f8c8d';
                document.getElementById('aiTrendIcon').textContent = '→';
                document.getElementById('aiTrendLabel').textContent = direction === 'stable' ? 'Stable' : 'Trend Unknown';
            }}

            const confidence = trends.confidence || 'unknown';
            document.getElementById('aiTrendConfidence').textContent = confidence !== 'unknown' ? 'Confidence: ' + confidence : '';

            // Key trends list
            const keyTrends = trends.key_trends || [];
            const trendsSection = document.getElementById('aiKeyTrends');
            const trendsList = document.getElementById('aiKeyTrendsList');
            if (keyTrends.length > 0) {{
                trendsSection.style.display = 'block';
                trendsList.innerHTML = keyTrends.map(t =>
                    '<li style="padding: 8px 0; border-bottom: 1px solid #f0f0f0; color: #34495e;">• ' + escapeHtml(t) + '</li>'
                ).join('');
            }} else {{
                trendsSection.style.display = 'none';
            }}

            // Notable changes
            const changes = trends.notable_changes || [];
            const changesSection = document.getElementById('aiNotableChanges');
            const changesList = document.getElementById('aiNotableChangesList');
            if (changes.length > 0) {{
                changesSection.style.display = 'block';
                changesList.innerHTML = changes.map(c =>
                    '<li style="padding: 8px 0; border-bottom: 1px solid #f0f0f0; color: #34495e;">• ' + escapeHtml(c) + '</li>'
                ).join('');
            }} else {{
                changesSection.style.display = 'none';
            }}
        }}

        function renderChangeIndicator(elementId, changePct, suffix) {{
            const el = document.getElementById(elementId);
            if (!el) return;
            suffix = suffix || '%';
            const val = parseFloat(changePct) || 0;
            if (val > 0) {{
                el.textContent = '+' + val.toFixed(1) + suffix + ' vs prev';
                el.style.color = '#27ae60';
            }} else if (val < 0) {{
                el.textContent = val.toFixed(1) + suffix + ' vs prev';
                el.style.color = '#e74c3c';
            }} else {{
                el.textContent = 'No change';
                el.style.color = '#95a5a6';
            }}
        }}

        function escapeHtml(str) {{
            const div = document.createElement('div');
            div.textContent = str;
            return div.innerHTML;
        }}

        async function generateAISummary() {{
            const statusEl = document.getElementById('aiSummaryStatus');
            const loading = document.getElementById('aiSummaryLoading');

            statusEl.textContent = '';
            loading.style.display = 'block';
            document.getElementById('aiSummaryContent').style.display = 'none';
            document.getElementById('aiSummaryEmpty').style.display = 'none';
            document.getElementById('aiSummaryHistory').style.display = 'none';

            try {{
                const response = await fetch('/admin/analytics/ai-summary/generate', {{ method: 'POST' }});
                const data = await response.json();

                loading.style.display = 'none';

                if (data.error) {{
                    statusEl.textContent = 'Error: ' + data.error;
                    return;
                }}

                statusEl.textContent = 'Summary generated successfully!';
                setTimeout(() => {{ statusEl.textContent = ''; }}, 5000);

                document.getElementById('aiSummaryContent').style.display = 'block';
                renderAISummary(data);
            }} catch (e) {{
                loading.style.display = 'none';
                statusEl.textContent = 'Failed: ' + e.message;
            }}
        }}

        // =============================================
        // Threaded analytics chat
        // =============================================
        const CHAT_COST_HINTS = {{
            haiku: '~$0.007 / msg',
            sonnet: '~$0.02 / msg',
            'opus-low': '~$0.03 / msg',
            'opus-medium': '~$0.08 / msg',
            'opus-high': '~$0.15 / msg',
            'opus-xhigh': '~$0.25 / msg',
            'opus-max': '~$0.40+ / msg',
        }};

        let chatActiveConvId = null;
        let chatConversations = [];
        let chatStagedFiles = [];
        const CHAT_MAX_IMAGES = 3;
        const CHAT_MAX_IMAGE_BYTES = 5 * 1024 * 1024;
        const CHAT_ALLOWED_MIME = ['image/jpeg', 'image/png', 'image/gif', 'image/webp'];

        function updateChatModelUI() {{
            const model = document.getElementById('chatModel').value;
            const effortWrap = document.getElementById('chatEffortWrap');
            const hint = document.getElementById('chatCostHint');
            if (model === 'opus') {{
                effortWrap.style.display = 'inline';
                const effort = document.getElementById('chatEffort').value;
                hint.textContent = CHAT_COST_HINTS['opus-' + effort] || '';
            }} else {{
                effortWrap.style.display = 'none';
                hint.textContent = CHAT_COST_HINTS[model] || '';
            }}
        }}

        function escapeHtmlChat(s) {{
            const div = document.createElement('div');
            div.textContent = s == null ? '' : String(s);
            return div.innerHTML;
        }}

        function fmtChatRelative(iso) {{
            if (!iso) return '';
            const d = new Date(iso);
            const diff = (Date.now() - d.getTime()) / 1000;
            if (diff < 60) return 'just now';
            if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
            if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
            return Math.floor(diff / 86400) + 'd ago';
        }}

        async function loadChatConversations() {{
            const list = document.getElementById('chatConvList');
            try {{
                const r = await fetch('/admin/analytics/conversations');
                const data = await r.json();
                chatConversations = data.conversations || [];
                if (chatConversations.length === 0) {{
                    list.innerHTML = '<div style="padding: 20px; color: #95a5a6; text-align: center; font-size: 0.9em;">No conversations yet. Start one above.</div>';
                    return;
                }}
                list.innerHTML = chatConversations.map(c => {{
                    const active = c.id === chatActiveConvId;
                    const bg = active ? '#e3f2fd' : 'transparent';
                    const border = active ? '3px solid #3498db' : '3px solid transparent';
                    return `
                        <div onclick="openChatConversation(${{c.id}})" style="padding: 10px 12px; cursor: pointer; border-left: ${{border}}; background: ${{bg}}; border-bottom: 1px solid #eee;">
                            <div style="font-size: 0.9em; color: #2c3e50; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${{escapeHtmlChat(c.title)}}">${{escapeHtmlChat(c.title)}}</div>
                            <div style="font-size: 0.75em; color: #95a5a6; margin-top: 2px;">${{c.message_count}} msgs · ${{fmtChatRelative(c.last_active_at)}}</div>
                        </div>`;
                }}).join('');
            }} catch (e) {{
                list.innerHTML = '<div style="padding: 20px; color: #e74c3c; text-align: center; font-size: 0.9em;">Failed to load</div>';
            }}
        }}

        async function newChatConversation() {{
            try {{
                const r = await fetch('/admin/analytics/conversations', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{}}),
                }});
                const data = await r.json();
                if (data.error) {{ alert('Could not create conversation: ' + data.error); return; }}
                chatActiveConvId = data.id;
                await loadChatConversations();
                await openChatConversation(data.id);
                document.getElementById('chatInput').focus();
            }} catch (e) {{
                alert('Failed to create conversation: ' + e.message);
            }}
        }}

        async function openChatConversation(convId) {{
            chatActiveConvId = convId;
            const messagesEl = document.getElementById('chatMessages');
            const header = document.getElementById('chatHeader');
            const inputWrap = document.getElementById('chatInputWrap');
            messagesEl.innerHTML = '<div style="color: #95a5a6; text-align: center;">Loading...</div>';
            header.style.display = 'flex';
            inputWrap.style.display = 'block';

            try {{
                const r = await fetch('/admin/analytics/conversations/' + convId);
                if (r.status === 404) {{
                    messagesEl.innerHTML = '<div style="color: #e74c3c;">Conversation not found</div>';
                    return;
                }}
                const conv = await r.json();
                document.getElementById('chatTitle').textContent = conv.title || 'Untitled';
                renderChatMessages(conv.messages || []);
                // Refresh list to update highlight
                loadChatConversations();
            }} catch (e) {{
                messagesEl.innerHTML = '<div style="color: #e74c3c;">Failed to load: ' + escapeHtmlChat(e.message) + '</div>';
            }}
        }}

        function renderChatMessages(messages) {{
            const messagesEl = document.getElementById('chatMessages');
            if (messages.length === 0) {{
                messagesEl.innerHTML = '<div style="color: #95a5a6; text-align: center; padding: 30px 20px;">Ask your first question below.</div>';
                return;
            }}
            messagesEl.innerHTML = messages.map(m => {{
                const isUser = m.role === 'user';
                const align = isUser ? 'flex-end' : 'flex-start';
                const bg = isUser ? '#3498db' : '#ecf0f1';
                const color = isUser ? 'white' : '#2c3e50';
                const meta = [];
                if (m.model) meta.push(m.model);
                if (m.effort) meta.push('effort: ' + m.effort);
                if (m.input_tokens != null || m.output_tokens != null) {{
                    meta.push((m.input_tokens || 0) + ' in / ' + (m.output_tokens || 0) + ' out');
                }}
                if (m.cache_read_input_tokens) meta.push(m.cache_read_input_tokens + ' cached');

                let attachmentsHtml = '';
                if (m.attachments && m.attachments.length) {{
                    attachmentsHtml = '<div style="display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 6px;">' +
                        m.attachments.map(att => {{
                            const url = '/admin/analytics/conversations/' + chatActiveConvId + '/attachments/' + att.id;
                            return `<a href="${{url}}" target="_blank" title="${{escapeHtmlChat(att.filename || '')}}"><img src="${{url}}" style="max-width: 180px; max-height: 140px; border-radius: 6px; border: 1px solid #ddd;"></a>`;
                        }}).join('') +
                        '</div>';
                }}

                return `
                    <div style="display: flex; justify-content: ${{align}}; margin-bottom: 14px;">
                        <div style="max-width: 85%;">
                            ${{attachmentsHtml}}
                            ${{m.content ? `<div style="background: ${{bg}}; color: ${{color}}; padding: 10px 14px; border-radius: 10px; white-space: pre-wrap; line-height: 1.5;">${{escapeHtmlChat(m.content)}}</div>` : ''}}
                            ${{!isUser && meta.length ? `<div style="font-size: 0.75em; color: #95a5a6; margin-top: 4px;">${{escapeHtmlChat(meta.join(' · '))}}</div>` : ''}}
                        </div>
                    </div>`;
            }}).join('');
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }}

        // ---- File staging + drag-and-drop ----
        function fmtChatBytes(n) {{
            if (n < 1024) return n + ' B';
            if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
            return (n / (1024 * 1024)).toFixed(1) + ' MB';
        }}

        function renderStagedFiles() {{
            const el = document.getElementById('chatStagedFiles');
            if (chatStagedFiles.length === 0) {{
                el.style.display = 'none';
                el.innerHTML = '';
                return;
            }}
            el.style.display = 'flex';
            el.innerHTML = chatStagedFiles.map((f, idx) => {{
                const url = URL.createObjectURL(f.file);
                return `
                    <div style="position: relative; width: 80px; height: 80px; border-radius: 6px; overflow: hidden; border: 1px solid #ddd; background: #f0f0f0;">
                        <img src="${{url}}" style="width: 100%; height: 100%; object-fit: cover;">
                        <button type="button" onclick="removeStagedFile(${{idx}})" title="Remove" style="position: absolute; top: 2px; right: 2px; background: rgba(0,0,0,0.6); color: white; border: none; border-radius: 50%; width: 22px; height: 22px; cursor: pointer; font-size: 14px; line-height: 1; padding: 0;">×</button>
                        <div style="position: absolute; bottom: 0; left: 0; right: 0; background: rgba(0,0,0,0.6); color: white; font-size: 0.7em; padding: 2px 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${{escapeHtmlChat(fmtChatBytes(f.file.size))}}</div>
                    </div>`;
            }}).join('');
        }}

        function removeStagedFile(idx) {{
            chatStagedFiles.splice(idx, 1);
            renderStagedFiles();
        }}

        function addStagedFiles(fileList) {{
            const status = document.getElementById('chatStatus');
            const incoming = Array.from(fileList || []);
            const errors = [];
            for (const f of incoming) {{
                if (chatStagedFiles.length >= CHAT_MAX_IMAGES) {{
                    errors.push('Max ' + CHAT_MAX_IMAGES + ' images per message');
                    break;
                }}
                if (!CHAT_ALLOWED_MIME.includes(f.type)) {{
                    errors.push(f.name + ': unsupported type (' + (f.type || 'unknown') + ')');
                    continue;
                }}
                if (f.size > CHAT_MAX_IMAGE_BYTES) {{
                    errors.push(f.name + ': too large (' + fmtChatBytes(f.size) + ')');
                    continue;
                }}
                chatStagedFiles.push({{ file: f }});
            }}
            renderStagedFiles();
            status.textContent = errors.length ? errors.join(' · ') : '';
        }}

        function handleChatFilesPicked(ev) {{
            addStagedFiles(ev.target.files);
            // Allow picking the same file twice in a row
            ev.target.value = '';
        }}

        function setupChatDropZone() {{
            const zone = document.getElementById('chatDropZone');
            if (!zone || zone.dataset.dropWired === '1') return;
            zone.dataset.dropWired = '1';
            ['dragenter', 'dragover'].forEach(evt => {{
                zone.addEventListener(evt, (e) => {{
                    e.preventDefault();
                    zone.style.borderColor = '#3498db';
                }});
            }});
            ['dragleave', 'drop'].forEach(evt => {{
                zone.addEventListener(evt, (e) => {{
                    e.preventDefault();
                    zone.style.borderColor = 'transparent';
                }});
            }});
            zone.addEventListener('drop', (e) => {{
                const files = e.dataTransfer && e.dataTransfer.files;
                if (files && files.length) addStagedFiles(files);
            }});
            // Paste-to-attach from clipboard
            document.getElementById('chatInput').addEventListener('paste', (e) => {{
                const items = (e.clipboardData && e.clipboardData.items) || [];
                const imgs = [];
                for (const it of items) {{
                    if (it.type && it.type.startsWith('image/')) {{
                        const f = it.getAsFile();
                        if (f) imgs.push(f);
                    }}
                }}
                if (imgs.length) addStagedFiles(imgs);
            }});
        }}

        function handleChatKey(e) {{
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {{
                e.preventDefault();
                sendChatMessage();
            }}
        }}

        async function sendChatMessage() {{
            if (!chatActiveConvId) {{
                alert('No conversation selected. Click "+ New conversation" first.');
                return;
            }}
            const inputEl = document.getElementById('chatInput');
            const question = (inputEl.value || '').trim();
            if (!question && chatStagedFiles.length === 0) return;

            const model = document.getElementById('chatModel').value;
            const effort = document.getElementById('chatEffort').value;
            const btn = document.getElementById('chatSendBtn');
            const status = document.getElementById('chatStatus');

            btn.disabled = true;
            btn.textContent = 'Thinking...';
            status.textContent = '';

            // Optimistically append the user message (no thumbnails for staged files yet — keep it simple)
            const r0 = await fetch('/admin/analytics/conversations/' + chatActiveConvId);
            let existing = [];
            if (r0.ok) {{
                const c = await r0.json();
                existing = c.messages || [];
            }}
            const optimisticAttachments = chatStagedFiles.map(s => ({{
                filename: s.file.name,
                mime_type: s.file.type,
            }}));
            renderChatMessages([...existing, {{ role: 'user', content: question, attachments: [] }}]);

            try {{
                const fd = new FormData();
                fd.append('question', question);
                fd.append('model', model);
                fd.append('effort', effort);
                for (const s of chatStagedFiles) fd.append('files', s.file, s.file.name);

                const r = await fetch('/admin/analytics/conversations/' + chatActiveConvId + '/messages', {{
                    method: 'POST',
                    body: fd,
                }});
                const data = await r.json();

                if (!r.ok || data.error) {{
                    status.textContent = 'Error: ' + (data.error || r.statusText);
                    renderChatMessages(existing);
                    return;
                }}

                inputEl.value = '';
                chatStagedFiles = [];
                renderStagedFiles();
                await openChatConversation(chatActiveConvId);
            }} catch (e) {{
                status.textContent = 'Failed: ' + e.message;
                renderChatMessages(existing);
            }} finally {{
                btn.disabled = false;
                btn.textContent = 'Send';
            }}
        }}

        async function renameActiveChat() {{
            if (!chatActiveConvId) return;
            const current = document.getElementById('chatTitle').textContent;
            const next = prompt('Rename conversation:', current);
            if (next == null) return;
            const title = next.trim();
            if (!title || title === current) return;
            try {{
                const r = await fetch('/admin/analytics/conversations/' + chatActiveConvId, {{
                    method: 'PATCH',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ title }}),
                }});
                if (!r.ok) {{
                    const data = await r.json();
                    alert('Rename failed: ' + (data.error || r.statusText));
                    return;
                }}
                document.getElementById('chatTitle').textContent = title;
                loadChatConversations();
            }} catch (e) {{
                alert('Rename failed: ' + e.message);
            }}
        }}

        async function deleteActiveChat() {{
            if (!chatActiveConvId) return;
            if (!confirm('Delete this conversation and all its messages? This cannot be undone.')) return;
            try {{
                const r = await fetch('/admin/analytics/conversations/' + chatActiveConvId, {{ method: 'DELETE' }});
                if (!r.ok) {{
                    const data = await r.json();
                    alert('Delete failed: ' + (data.error || r.statusText));
                    return;
                }}
                chatActiveConvId = null;
                document.getElementById('chatHeader').style.display = 'none';
                document.getElementById('chatInputWrap').style.display = 'none';
                document.getElementById('chatMessages').innerHTML = '<div style="color: #95a5a6; text-align: center; padding: 40px 20px;">Select a conversation on the left, or start a new one.</div>';
                loadChatConversations();
            }} catch (e) {{
                alert('Delete failed: ' + e.message);
            }}
        }}

        document.addEventListener('DOMContentLoaded', function() {{
            updateChatModelUI();
            loadChatConversations();
            setupChatDropZone();
        }});

        async function loadAISummaryHistory() {{
            const history = document.getElementById('aiSummaryHistory');
            const historyList = document.getElementById('aiHistoryList');
            const content = document.getElementById('aiSummaryContent');

            // Toggle: if history is visible, go back to latest
            if (history.style.display === 'block') {{
                history.style.display = 'none';
                loadAISummary();
                return;
            }}

            content.style.display = 'none';
            document.getElementById('aiSummaryEmpty').style.display = 'none';
            historyList.innerHTML = '<p style="color: #95a5a6; text-align: center;">Loading history...</p>';
            history.style.display = 'block';

            try {{
                const response = await fetch('/admin/analytics/ai-summaries?limit=30');
                const data = await response.json();
                const summaries = data.summaries || [];

                if (summaries.length === 0) {{
                    historyList.innerHTML = '<p style="color: #95a5a6; text-align: center;">No summaries found.</p>';
                    return;
                }}

                historyList.innerHTML = summaries.map((s, idx) => {{
                    const trends = s.trends || {{}};
                    const direction = trends.direction || 'unknown';
                    const arrow = direction === 'up' ? '↑' : direction === 'down' ? '↓' : '→';
                    const arrowColor = direction === 'up' ? '#27ae60' : direction === 'down' ? '#e74c3c' : '#95a5a6';
                    const metrics = s.metrics_snapshot || {{}};
                    const users = metrics.users ? metrics.users.current : '-';
                    const sessions = metrics.sessions ? metrics.sessions.current : '-';

                    return `<div style="background: white; border-radius: 8px; padding: 16px; margin-bottom: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); cursor: pointer;" onclick="expandHistoryItem(${{idx}})">
                        <div style="display: flex; justify-content: space-between; align-items: center;">
                            <div>
                                <span style="font-weight: 600; color: #2c3e50;">${{s.summary_date}}</span>
                                <span style="color: ${{arrowColor}}; font-weight: 600; margin-left: 8px;">${{arrow}}</span>
                                <span style="color: #95a5a6; margin-left: 12px; font-size: 0.85em;">Users: ${{users}} | Sessions: ${{sessions}}</span>
                            </div>
                            <span style="color: #95a5a6; font-size: 0.8em;">${{s.period_days}}d period</span>
                        </div>
                        <div id="historyDetail${{idx}}" style="display: none; margin-top: 12px; padding-top: 12px; border-top: 1px solid #f0f0f0; color: #34495e; white-space: pre-wrap; font-size: 0.9em; line-height: 1.6;">${{escapeHtml(s.summary_text || '')}}</div>
                    </div>`;
                }}).join('');
            }} catch (e) {{
                historyList.innerHTML = '<p style="color: #e74c3c; text-align: center;">Failed to load history: ' + e.message + '</p>';
            }}
        }}

        function expandHistoryItem(idx) {{
            const detail = document.getElementById('historyDetail' + idx);
            if (detail) {{
                detail.style.display = detail.style.display === 'none' ? 'block' : 'none';
            }}
        }}

        // Load AI summary on page load
        loadAISummary();

    </script>
</body>
</html>
    """

    return HTMLResponse(content=html)


# =====================================================
# INVESTMENT HEALTH DASHBOARD
# =====================================================

@router.get("/admin/investment-health/data")
async def admin_investment_health_data(admin: str = Depends(verify_admin)):
    """Live Stripe-derived inputs for the Investment Health page."""
    from services.stripe_service import get_investment_health_metrics
    from services.investment_health import DEFAULT_INPUTS

    live = get_investment_health_metrics()
    return JSONResponse({
        "live": live,
        "manual_defaults": {
            "cac": DEFAULT_INPUTS["cac"],
            "monthly_burn": DEFAULT_INPUTS["monthly_burn"],
        },
    })


@router.get("/admin/investment-health", response_class=HTMLResponse)
async def admin_investment_health(admin: str = Depends(verify_admin)):
    """Interactive investment-health decision tool — live Stripe metrics with
    manual override per input, reactive verdict card, and 6 health checks."""
    html = """
<!DOCTYPE html>
<html>
<head>
    <title>Investment Health · Remyndrs Admin</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: #f5f5f5;
            padding: 20px;
            color: #333;
        }
        h1 { margin-bottom: 8px; color: #2c3e50; }
        h2 { margin: 24px 0 12px; color: #34495e; font-size: 1.2em; }
        .subhead { color: #7f8c8d; margin-bottom: 20px; font-size: 0.95em; }

        .nav-menu {
            position: sticky; top: 0; background: white;
            padding: 12px 20px; margin: -20px -20px 20px -20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            z-index: 100; display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
        }
        .nav-menu a {
            padding: 8px 16px; background: #f8f9fa; border-radius: 4px;
            text-decoration: none; color: #2c3e50; font-size: 0.9em;
            font-weight: 500; transition: all 0.2s; border: 1px solid #e0e0e0;
        }
        .nav-menu a:hover { background: #3498db; color: white; border-color: #3498db; }
        .nav-menu .nav-title { font-weight: bold; color: #2c3e50; margin-right: 10px; }

        .header-row {
            display: flex; justify-content: space-between; align-items: center;
            flex-wrap: wrap; gap: 12px; margin-bottom: 20px;
        }
        .reset-btn {
            padding: 10px 20px; background: #3498db; color: white;
            border: none; border-radius: 4px; cursor: pointer;
            font-size: 0.95em; font-weight: 500;
        }
        .reset-btn:hover { background: #2980b9; }
        .reset-btn:disabled { background: #bdc3c7; cursor: not-allowed; }
        .last-synced { color: #95a5a6; font-size: 0.85em; }

        .inputs-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 15px; margin-bottom: 30px;
        }
        .input-card {
            background: white; padding: 18px; border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .input-label {
            font-size: 0.9em; color: #7f8c8d; margin-bottom: 8px;
            display: flex; justify-content: space-between; align-items: center;
        }
        .badge {
            font-size: 0.7em; padding: 2px 8px; border-radius: 10px;
            font-weight: 600; cursor: pointer; user-select: none;
        }
        .badge.live { background: #d5f5e3; color: #27ae60; }
        .badge.manual { background: #fdebd0; color: #e67e22; }
        .badge.manual-only { background: #ecf0f1; color: #7f8c8d; cursor: default; }
        .input-row { display: flex; align-items: center; gap: 10px; }
        .input-row input[type="number"] {
            flex: 1; padding: 8px 10px; font-size: 1.3em;
            border: 1px solid #e0e0e0; border-radius: 4px; font-weight: 600;
            color: #2c3e50; background: white;
        }
        .input-row input[type="number"]:focus { outline: none; border-color: #3498db; }
        .input-prefix { font-size: 1.3em; color: #95a5a6; font-weight: 600; }
        .input-suffix { font-size: 1.1em; color: #95a5a6; font-weight: 500; }

        .verdict-card {
            padding: 24px 28px; border-radius: 8px; color: white;
            box-shadow: 0 2px 8px rgba(0,0,0,0.15); margin-bottom: 30px;
        }
        .verdict-card.green { background: linear-gradient(135deg, #27ae60, #229954); }
        .verdict-card.yellow { background: linear-gradient(135deg, #f39c12, #d68910); }
        .verdict-card.red { background: linear-gradient(135deg, #e74c3c, #c0392b); }
        .verdict-title { font-size: 1.6em; font-weight: 700; margin-bottom: 8px; }
        .verdict-message { font-size: 1em; line-height: 1.5; opacity: 0.95; }

        .checks-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 15px;
        }
        .check-card {
            background: white; padding: 18px; border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .check-header {
            display: flex; align-items: center; gap: 8px; margin-bottom: 10px;
        }
        .status-dot { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }
        .status-dot.green { background: #27ae60; }
        .status-dot.yellow { background: #f39c12; }
        .status-dot.red { background: #e74c3c; }
        .check-label { color: #7f8c8d; font-size: 0.9em; font-weight: 500; }
        .check-value {
            font-size: 1.8em; font-weight: bold; color: #2c3e50; margin-bottom: 4px;
        }
        .check-context { font-size: 0.8em; color: #95a5a6; }

        .loading-overlay {
            position: fixed; inset: 0; background: rgba(255,255,255,0.85);
            display: flex; align-items: center; justify-content: center;
            font-size: 1.1em; color: #7f8c8d; z-index: 200;
        }
    </style>
</head>
<body>
    <div class="nav-menu">
        <span class="nav-title">Remyndrs Dashboard</span>
        <a href="/admin/dashboard">Back to Dashboard</a>
        <a href="/admin/monitoring" style="background: #27ae60; color: white; border-color: #27ae60;">Monitoring</a>
    </div>

    <h1>Investment Health</h1>
    <p class="subhead">Decision-support tool: should we keep investing? Pulls live Stripe metrics; click any "live" badge to switch to manual entry for "what if" scenarios.</p>

    <div class="header-row">
        <button class="reset-btn" id="resetBtn" onclick="resetToLive()">Reset to live data</button>
        <span class="last-synced" id="lastSynced">Loading…</span>
    </div>

    <div id="loadingOverlay" class="loading-overlay">Loading live Stripe data…</div>

    <h2>Inputs</h2>
    <div class="inputs-grid" id="inputsGrid"></div>

    <div class="verdict-card yellow" id="verdictCard">
        <div class="verdict-title" id="verdictTitle">—</div>
        <div class="verdict-message" id="verdictMessage">Computing…</div>
    </div>

    <h2>Health checks</h2>
    <div class="checks-grid" id="checksGrid"></div>

    <script>
        // ------- input definitions -------
        const INPUTS = [
            {key: 'current_users', label: 'Current paid users', live: true, type: 'int'},
            {key: 'net_now',       label: 'Net new paid this month', live: true, type: 'int'},
            {key: 'net_3mo',       label: 'Net new paid 3 months ago', live: true, type: 'int'},
            {key: 'churn_rate',    label: 'Monthly churn rate', live: true, type: 'percent', min: 1, step: 0.1},
            {key: 'cac',           label: 'Blended CAC', live: false, type: 'currency', min: 0},
            {key: 'arpu',          label: 'Net revenue per user', live: true, type: 'currency', min: 0, step: 0.5},
            {key: 'monthly_burn',  label: 'Monthly burn', live: false, type: 'currency', min: 0},
        ];

        // state.values holds the *current* shown value (live or manual)
        // state.live holds the most recent live data
        // state.mode[key] is 'live' or 'manual'
        const state = { values: {}, live: {}, mode: {} };

        // ------- math (mirrors services/investment_health.py) -------
        function clampChurn(p) { return Math.max(p, 1.0); }

        function computeDerived(v) {
            const churn_d = clampChurn(v.churn_rate) / 100.0;
            let ltv, ltv_cac, payback, breakeven_users;
            if (v.arpu <= 0) {
                ltv = 0; ltv_cac = 0; payback = Infinity; breakeven_users = Infinity;
            } else {
                ltv = v.arpu / churn_d;
                ltv_cac = v.cac > 0 ? ltv / v.cac : Infinity;
                payback = v.cac / v.arpu;
                breakeven_users = v.monthly_burn / v.arpu;
            }
            const gross_adds = Math.max(0, v.net_now + v.current_users * churn_d);
            const steady_state = churn_d > 0 ? gross_adds / churn_d : Infinity;
            const can_reach = steady_state >= breakeven_users;

            let months_to_breakeven = null;
            if (v.current_users >= breakeven_users) {
                months_to_breakeven = 0;
            } else if (can_reach && v.net_now > 0) {
                let u = v.current_users, m = 0;
                while (u < breakeven_users && m < 240) {
                    u = u * (1 - churn_d) + gross_adds;
                    m += 1;
                }
                months_to_breakeven = m < 240 ? m : null;
            }

            return {
                ltv, ltv_cac, payback, breakeven_users,
                gross_adds, steady_state, can_reach, months_to_breakeven,
                growth_delta: v.net_now - v.net_3mo,
            };
        }

        function statusOf(metric, v, d) {
            if (metric === 'churn') {
                if (v.churn_rate < 7) return 'green';
                if (v.churn_rate <= 10) return 'yellow';
                return 'red';
            }
            if (metric === 'growth') {
                if (v.net_now <= 0) return 'red';
                return d.growth_delta >= -3 ? 'green' : 'yellow';
            }
            if (metric === 'ltv_cac') {
                if (d.ltv_cac >= 3.0) return 'green';
                if (d.ltv_cac >= 1.5) return 'yellow';
                return 'red';
            }
            if (metric === 'payback') {
                if (d.payback < 12) return 'green';
                if (d.payback <= 24) return 'yellow';
                return 'red';
            }
            if (metric === 'breakeven') {
                if (d.months_to_breakeven === null) return 'red';
                if (d.months_to_breakeven <= 12) return 'green';
                if (d.months_to_breakeven <= 24) return 'yellow';
                return 'red';
            }
            if (metric === 'steady_state') {
                if (d.breakeven_users <= 0 || !isFinite(d.breakeven_users)) return 'red';
                const r = d.steady_state / d.breakeven_users;
                if (r >= 1.3) return 'green';
                if (r >= 1.0) return 'yellow';
                return 'red';
            }
        }

        function verdictFor(checks) {
            const primary = ['churn', 'growth', 'ltv_cac', 'breakeven'].map(k => checks[k]);
            const red_count = primary.filter(s => s === 'red').length;
            const green_count = primary.filter(s => s === 'green').length;
            const critical_red = checks.churn === 'red' || checks.ltv_cac === 'red';

            if (critical_red || red_count >= 2) {
                return {
                    color: 'red', title: 'Stop or restructure',
                    message: red_count + " critical fail(s). Unit economics or product stickiness won't fix themselves with more ad spend. Cut burn, fix retention or pricing, or wind down before adding more capital.",
                };
            }
            if (green_count >= 3) {
                return {
                    color: 'green', title: 'Continue investing',
                    message: green_count + " of 4 health checks pass. Slower ramp than original target is fine — the underlying business works. Keep optimizing creative, conversion, and the upgrade flow.",
                };
            }
            return {
                color: 'yellow', title: 'Pivot before reinvesting',
                message: green_count + " of 4 pass, " + red_count + " fail. Fixable problem in one dimension. Address the weak metric before adding more spend — don't double down on a leak.",
            };
        }

        // ------- formatting -------
        const fmtCurrency = n => '$' + Math.round(n).toLocaleString();
        const fmtCount = n => Math.round(n).toLocaleString();
        const fmtPercent = n => n.toFixed(1) + '%';
        const fmtRatio = n => isFinite(n) ? n.toFixed(1) + 'x' : '∞';
        const fmtMonths = m => m === null ? 'Never' : (m >= 240 ? '>20 yr' : m + ' mo');

        // ------- rendering -------
        function renderInputs() {
            const grid = document.getElementById('inputsGrid');
            grid.innerHTML = '';
            INPUTS.forEach(inp => {
                const card = document.createElement('div');
                card.className = 'input-card';

                let badge;
                if (!inp.live) {
                    badge = '<span class="badge manual-only">manual</span>';
                } else {
                    const m = state.mode[inp.key] || 'live';
                    badge = `<span class="badge ${m}" onclick="toggleMode('${inp.key}')" title="Click to ${m === 'live' ? 'override manually' : 'restore live value'}">${m}</span>`;
                }

                let prefix = '', suffix = '', step = inp.step || 1, min = inp.min;
                if (inp.type === 'currency') prefix = '$';
                if (inp.type === 'percent') suffix = '%';

                card.innerHTML = `
                    <div class="input-label">
                        <span>${inp.label}</span>
                        ${badge}
                    </div>
                    <div class="input-row">
                        ${prefix ? '<span class="input-prefix">' + prefix + '</span>' : ''}
                        <input type="number" id="input_${inp.key}" value="${state.values[inp.key]}" step="${step}" ${min !== undefined ? 'min="' + min + '"' : ''} oninput="onInput('${inp.key}', this.value)">
                        ${suffix ? '<span class="input-suffix">' + suffix + '</span>' : ''}
                    </div>
                `;
                grid.appendChild(card);
            });
        }

        function renderResults() {
            const v = state.values;
            const d = computeDerived(v);
            const checks = {
                churn: statusOf('churn', v, d),
                growth: statusOf('growth', v, d),
                ltv_cac: statusOf('ltv_cac', v, d),
                payback: statusOf('payback', v, d),
                breakeven: statusOf('breakeven', v, d),
                steady_state: statusOf('steady_state', v, d),
            };
            const verdict = verdictFor(checks);

            const card = document.getElementById('verdictCard');
            card.className = 'verdict-card ' + verdict.color;
            document.getElementById('verdictTitle').textContent = verdict.title;
            document.getElementById('verdictMessage').textContent = verdict.message;

            const cardDefs = [
                {k: 'churn',        label: 'Monthly churn',     value: fmtPercent(v.churn_rate), context: 'trailing 90 days'},
                {k: 'growth',       label: 'Growth trend',      value: (d.growth_delta >= 0 ? '+' : '') + Math.round(d.growth_delta), context: Math.round(v.net_now) + ' now vs ' + Math.round(v.net_3mo) + ' three months ago'},
                {k: 'ltv_cac',      label: 'LTV / CAC',         value: fmtRatio(d.ltv_cac), context: 'LTV ' + fmtCurrency(d.ltv) + ' / CAC ' + fmtCurrency(v.cac)},
                {k: 'payback',      label: 'CAC payback',       value: isFinite(d.payback) ? d.payback.toFixed(1) + ' mo' : '∞', context: 'months to recover acquisition cost'},
                {k: 'breakeven',    label: 'Months to breakeven', value: fmtMonths(d.months_to_breakeven), context: 'need ' + fmtCount(d.breakeven_users) + ' paid users'},
                {k: 'steady_state', label: 'Steady-state ceiling', value: fmtCount(d.steady_state), context: 'breakeven needs ' + fmtCount(d.breakeven_users)},
            ];

            const grid = document.getElementById('checksGrid');
            grid.innerHTML = cardDefs.map(c => `
                <div class="check-card">
                    <div class="check-header">
                        <span class="status-dot ${checks[c.k]}"></span>
                        <span class="check-label">${c.label}</span>
                    </div>
                    <div class="check-value">${c.value}</div>
                    <div class="check-context">${c.context}</div>
                </div>
            `).join('');
        }

        // ------- event handlers -------
        function onInput(key, raw) {
            const inp = INPUTS.find(i => i.key === key);
            let val = parseFloat(raw);
            if (isNaN(val)) val = 0;
            if (inp.min !== undefined && val < inp.min) val = inp.min;
            state.values[key] = val;
            if (inp.live) state.mode[key] = 'manual';
            renderInputs();
            // re-focus the changed input so the user can keep typing
            const el = document.getElementById('input_' + key);
            if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
            renderResults();
        }

        function toggleMode(key) {
            const inp = INPUTS.find(i => i.key === key);
            if (!inp.live) return;
            if (state.mode[key] === 'manual') {
                // restore live
                state.mode[key] = 'live';
                state.values[key] = state.live[key];
            } else {
                // switch to manual (keeps current value)
                state.mode[key] = 'manual';
            }
            renderInputs();
            renderResults();
        }

        function resetToLive() {
            INPUTS.forEach(inp => {
                if (inp.live) {
                    state.mode[inp.key] = 'live';
                    state.values[inp.key] = state.live[inp.key];
                }
            });
            renderInputs();
            renderResults();
        }

        async function loadLive() {
            try {
                const res = await fetch('/admin/investment-health/data', {credentials: 'same-origin'});
                if (!res.ok) throw new Error('HTTP ' + res.status);
                const data = await res.json();
                const live = data.live || {};
                const manual_defaults = data.manual_defaults || {};

                state.live = {
                    current_users: live.current_users ?? 0,
                    net_now: live.net_now ?? 0,
                    net_3mo: live.net_3mo ?? 0,
                    churn_rate: live.churn_rate ?? 7,
                    arpu: live.arpu ?? 8,
                };
                state.values = {
                    ...state.live,
                    cac: manual_defaults.cac ?? 60,
                    monthly_burn: manual_defaults.monthly_burn ?? 4167,
                };
                INPUTS.forEach(i => { if (i.live) state.mode[i.key] = 'live'; });

                const ts = live.fetched_at ? new Date(live.fetched_at).toLocaleString() : 'unknown';
                let label = 'Live data synced ' + ts;
                if (live.error) label += ' · ' + live.error + ' (using fallback values)';
                document.getElementById('lastSynced').textContent = label;
            } catch (e) {
                document.getElementById('lastSynced').textContent = 'Could not load live data: ' + e.message + ' (using defaults)';
                state.live = { current_users: 0, net_now: 0, net_3mo: 0, churn_rate: 7, arpu: 8 };
                state.values = { ...state.live, cac: 60, monthly_burn: 4167 };
                INPUTS.forEach(i => { if (i.live) state.mode[i.key] = 'live'; });
            } finally {
                document.getElementById('loadingOverlay').style.display = 'none';
                renderInputs();
                renderResults();
            }
        }

        loadLive();
    </script>
</body>
</html>
    """

    return HTMLResponse(content=html)
