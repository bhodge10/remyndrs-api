"""
Admin Dashboard
HTML dashboard for viewing metrics and broadcast messaging
"""

import secrets
import asyncio
import threading
import time
from datetime import datetime
import pytz
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional
from services.metrics_service import get_all_metrics, get_cost_analytics
from services.sms_service import send_sms
from database import (
    get_db_connection, return_db_connection, get_setting, set_setting,
    get_recent_logs, get_flagged_conversations, mark_analysis_reviewed,
    manual_flag_conversation, mark_conversation_good, get_good_conversations,
    dismiss_conversation
)
from config import ADMIN_USERNAME, ADMIN_PASSWORD, logger
from utils.validation import log_security_event

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

router = APIRouter()
security = HTTPBasic()


class BroadcastRequest(BaseModel):
    message: str
    audience: str  # "all", "free", "premium"


def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """Verify admin credentials for protected endpoints"""
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=500, detail="Admin password not configured")

    correct_username = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)

    if not (correct_username and correct_password):
        log_security_event("AUTH_FAILURE", {"username": credentials.username, "endpoint": "dashboard"})
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


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

        # Get users with their timezones and plan types
        c.execute('''
            SELECT
                phone_number,
                COALESCE(premium_status, 'free') as plan,
                timezone
            FROM users
            WHERE onboarding_complete = TRUE
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


@router.get("/admin/broadcast/history")
async def get_broadcast_history(admin: str = Depends(verify_admin)):
    """Get history of past broadcasts"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute('''
            SELECT id, sender, message, audience, recipient_count,
                   success_count, fail_count, status, created_at, completed_at
            FROM broadcast_logs
            ORDER BY created_at DESC
            LIMIT 20
        ''')
        results = c.fetchall()

        history = []
        for row in results:
            history.append({
                "id": row[0],
                "sender": row[1],
                "message": row[2][:100] + "..." if len(row[2]) > 100 else row[2],
                "audience": row[3],
                "recipient_count": row[4],
                "success_count": row[5],
                "fail_count": row[6],
                "status": row[7],
                "created_at": row[8].isoformat() if row[8] else None,
                "completed_at": row[9].isoformat() if row[9] else None
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
                send_sms(phone, full_message)
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
            INSERT INTO broadcast_logs (sender, message, audience, recipient_count, status)
            VALUES (%s, %s, %s, %s, 'pending')
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


@router.post("/admin/broadcast/schedule")
async def schedule_broadcast(request: ScheduleBroadcastRequest, admin: str = Depends(verify_admin)):
    """Schedule a broadcast for future delivery"""
    conn = None
    try:
        if request.audience not in ["all", "free", "premium"]:
            raise HTTPException(status_code=400, detail="Invalid audience type")

        if not request.message.strip():
            raise HTTPException(status_code=400, detail="Message cannot be empty")

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
            INSERT INTO scheduled_broadcasts (sender, message, audience, scheduled_date, status)
            VALUES (%s, %s, %s, %s, 'scheduled')
            RETURNING id
        ''', (admin, request.message.strip(), request.audience, scheduled_dt))

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

def send_scheduled_broadcast(broadcast_id: int, message: str, audience: str):
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
                send_sms(phone, full_message)
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

        # Final update
        c.execute('''
            UPDATE scheduled_broadcasts
            SET success_count = %s, fail_count = %s, status = 'completed', sent_at = NOW()
            WHERE id = %s
        ''', (success_count, fail_count, broadcast_id))
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
            except:
                pass
    finally:
        if conn:
            return_db_connection(conn)


def check_scheduled_broadcasts():
    """Background thread that checks for due scheduled broadcasts"""
    logger.info("Starting scheduled broadcast checker")

    while True:
        try:
            now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            logger.info(f"Checking for scheduled broadcasts at {now}")

            conn = get_db_connection()
            c = conn.cursor()

            # Find broadcasts that are due
            c.execute('''
                SELECT id, message, audience
                FROM scheduled_broadcasts
                WHERE scheduled_date <= %s AND status = 'scheduled'
            ''', (now,))

            due_broadcasts = c.fetchall()
            return_db_connection(conn)

            if due_broadcasts:
                logger.info(f"Found {len(due_broadcasts)} scheduled broadcasts to send")

            for broadcast_id, message, audience in due_broadcasts:
                logger.info(f"Sending scheduled broadcast {broadcast_id}")
                send_scheduled_broadcast(broadcast_id, message, audience)

        except Exception as e:
            logger.error(f"Error in scheduled broadcast checker: {e}")

        # Check every 60 seconds
        time.sleep(60)


def start_broadcast_checker():
    """Start the scheduled broadcast checker in a daemon thread"""
    thread = threading.Thread(target=check_scheduled_broadcasts, daemon=True)
    thread.start()
    logger.info("Scheduled broadcast checker thread started")


# =====================================================
# FEEDBACK API ENDPOINTS
# =====================================================

@router.get("/admin/feedback")
async def get_feedback(admin: str = Depends(verify_admin)):
    """Get all feedback entries, sorted by most recent first"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute('''
            SELECT id, user_phone, message, created_at, resolved
            FROM feedback
            ORDER BY created_at DESC
        ''')
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
# COST ANALYTICS API ENDPOINT
# =====================================================

@router.get("/admin/costs")
async def get_costs(admin: str = Depends(verify_admin)):
    """Get cost analytics broken down by plan tier and time period"""
    try:
        costs = get_cost_analytics()
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
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================
# MAINTENANCE MESSAGE SETTINGS
# =====================================================

DEFAULT_MAINTENANCE_MESSAGE = "Remyndrs is undergoing maintenance. The service will be back up soon. You will receive a message when it's back up."

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
        raise HTTPException(status_code=500, detail=str(e))


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
    admin: str = Depends(verify_admin)
):
    """Get recent conversation logs"""
    try:
        logs = get_recent_logs(limit=limit, offset=offset, phone_filter=phone, intent_filter=intent, hide_reviewed=hide_reviewed)
        return JSONResponse(content=logs)
    except Exception as e:
        logger.error(f"Error getting conversations: {e}")
        raise HTTPException(status_code=500, detail="Error getting conversations")


@router.get("/admin/conversations/flagged")
async def get_flagged(include_reviewed: bool = False, admin: str = Depends(verify_admin)):
    """Get AI-flagged conversations"""
    try:
        flagged = get_flagged_conversations(limit=50, include_reviewed=include_reviewed)
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
        raise HTTPException(status_code=500, detail=str(e))


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
                <span class="entry-badge" style="background-color: {badge_color}">{badge_label}</span>
                <span class="entry-title">{title}</span>
                {f'<p class="entry-description">{description}</p>' if description else ''}
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
async def get_changelog_entries(admin: str = Depends(verify_admin)):
    """Get all changelog entries for admin management"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            SELECT id, title, description, entry_type, created_at, published
            FROM changelog
            ORDER BY created_at DESC
        ''')
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
        raise HTTPException(status_code=500, detail=str(e))
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
        raise HTTPException(status_code=500, detail=str(e))
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
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# SUPPORT TICKET API ENDPOINTS
# =====================================================

class SupportReplyRequest(BaseModel):
    message: str


@router.get("/admin/support/tickets")
async def get_support_tickets(include_closed: bool = False, admin: str = Depends(verify_admin)):
    """Get all support tickets"""
    from services.support_service import get_all_tickets
    tickets = get_all_tickets(include_closed)
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
            scroll-margin-top: 70px;
        }}
    </style>
</head>
<body>
    <div class="nav-menu">
        <span class="nav-title">Remyndrs Dashboard</span>
        <a href="#overview">Overview</a>
        <a href="#broadcast">Broadcast</a>
        <a href="#feedback">Feedback</a>
        <a href="#costs">Costs</a>
        <a href="#conversations">Conversations</a>
    </div>

    <h2 id="overview" class="section-anchor" style="margin-top: 0;">Overview</h2>

    <div class="cards">
        <div class="card">
            <div class="card-title">Total Users</div>
            <div class="card-value">{metrics.get('total_users', 0)}</div>
            <div class="card-subtitle">completed onboarding</div>
        </div>
        <div class="card orange">
            <div class="card-title">Pending Onboarding</div>
            <div class="card-value">{metrics.get('pending_onboarding', 0)}</div>
            <div class="card-subtitle">started but not finished</div>
            <button class="cleanup-btn" onclick="cleanupIncomplete()">Clean Up</button>
        </div>
        <div class="card green">
            <div class="card-title">Active (7 days)</div>
            <div class="card-value">{metrics.get('active_7d', 0)}</div>
            <div class="card-subtitle">sent a message</div>
        </div>
        <div class="card blue">
            <div class="card-title">Active (30 days)</div>
            <div class="card-value">{metrics.get('active_30d', 0)}</div>
            <div class="card-subtitle">sent a message</div>
        </div>
        <div class="card purple">
            <div class="card-title">Premium Users</div>
            <div class="card-value">{premium.get('premium', 0)}</div>
            <div class="card-subtitle">free: {premium.get('free', 0)}</div>
        </div>
    </div>

    <h2>New User Signups</h2>
    <div class="cards">
        <div class="card green">
            <div class="card-title">Today</div>
            <div class="card-value">{new_users.get('today', 0)}</div>
            <div class="card-subtitle">new users</div>
        </div>
        <div class="card blue">
            <div class="card-title">This Week</div>
            <div class="card-value">{new_users.get('this_week', 0)}</div>
            <div class="card-subtitle">last 7 days</div>
        </div>
        <div class="card orange">
            <div class="card-title">This Month</div>
            <div class="card-value">{new_users.get('this_month', 0)}</div>
            <div class="card-subtitle">last 30 days</div>
        </div>
    </div>

    <div class="grid-2">
        <div class="section">
            <h2>Engagement Stats</h2>
            <table>
                <tr><th>Metric</th><th>Value</th></tr>
                <tr><td>Avg Messages / User</td><td>{engagement.get('avg_messages_per_user', 0)}</td></tr>
                <tr><td>Avg Memories / User</td><td>{engagement.get('avg_memories_per_user', 0)}</td></tr>
                <tr><td>Avg Reminders / User</td><td>{engagement.get('avg_reminders_per_user', 0)}</td></tr>
                <tr><td>Avg Lists / User</td><td>{engagement.get('avg_lists_per_user', 0)}</td></tr>
                <tr><td>Avg Items / List</td><td>{engagement.get('avg_items_per_list', 0)}</td></tr>
                <tr><td>Total Messages</td><td>{engagement.get('total_messages', 0)}</td></tr>
                <tr><td>Total Memories</td><td>{engagement.get('total_memories', 0)}</td></tr>
                <tr><td>Total Reminders</td><td>{engagement.get('total_reminders', 0)}</td></tr>
                <tr><td>Total Lists</td><td>{engagement.get('total_lists', 0)}</td></tr>
            </table>
        </div>

        <div class="section">
            <h2>Reminder Delivery</h2>
            <table>
                <tr><th>Status</th><th>Count</th></tr>
                <tr><td>Pending</td><td>{reminder_stats.get('pending', 0)}</td></tr>
                <tr><td>Sent</td><td>{reminder_stats.get('sent', 0)}</td></tr>
                <tr><td>Failed</td><td>{reminder_stats.get('failed', 0)}</td></tr>
                <tr><td><strong>Completion Rate</strong></td><td><strong>{reminder_stats.get('completion_rate', 0)}%</strong></td></tr>
            </table>
        </div>
    </div>

    <div class="section">
        <h2>Daily Signups (Last 14 Days)</h2>
        <div class="chart">
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
            {referral_rows if referral_rows else '<tr><td colspan="2" style="color: #95a5a6;">No referral data yet</td></tr>'}
        </table>
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
            </select>
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
    <div id="costs" class="cost-section section-anchor">
        <h2>💰 Cost Analytics</h2>

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

        <div style="margin-top: 15px; font-size: 0.85em; color: #7f8c8d;">
            <em>SMS: $0.0079/message (inbound + outbound) | AI: GPT-4o-mini pricing</em>
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

    <!-- Support Tickets Section -->
    <div id="support" class="section section-anchor">
        <h2>🎧 Support Tickets <span id="openTicketCount" style="font-size: 0.7em; color: #7f8c8d;"></span></h2>
        <p style="color: #7f8c8d; margin-bottom: 15px;">
            Premium users can text "Support: [message]" to create tickets. Replies are sent via SMS.
        </p>

        <div style="margin-bottom: 15px;">
            <label style="margin-right: 10px;">
                <input type="checkbox" id="showClosedTickets" onchange="loadSupportTickets()"> Show closed tickets
            </label>
        </div>

        <div id="supportTicketsList" style="margin-bottom: 20px;">
            <p style="color: #95a5a6;">Loading...</p>
        </div>

        <!-- Ticket Detail Modal -->
        <div id="ticketModal" style="display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 1000;">
            <div style="background: white; max-width: 600px; margin: 50px auto; border-radius: 8px; max-height: 80vh; overflow: hidden; display: flex; flex-direction: column;">
                <div style="padding: 15px 20px; border-bottom: 1px solid #ddd; display: flex; justify-content: space-between; align-items: center;">
                    <h3 style="margin: 0;" id="ticketModalTitle">Ticket #</h3>
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

    <!-- Conversation Viewer Section -->
    <div id="conversations" class="conversation-section section-anchor">
        <h2>💬 Conversation Viewer</h2>

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
            <p style="color: #e74c3c;"><strong>This action cannot be undone.</strong></p>
            <div class="modal-buttons">
                <button class="btn btn-secondary" onclick="hideConfirmModal()">Cancel</button>
                <button class="btn btn-danger" id="modalConfirmBtn" onclick="handleBroadcastSubmit()">Send Now</button>
            </div>
        </div>
    </div>

    <p class="refresh-note">Refresh page to update metrics</p>

    <script>
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
        async function loadHistory() {{
            try {{
                const response = await fetch('/admin/broadcast/history');
                const history = await response.json();

                const table = document.getElementById('historyTable');
                const loadingRow = document.getElementById('historyLoading');
                if (loadingRow) loadingRow.remove();

                if (history.length === 0) {{
                    const row = table.insertRow(-1);
                    row.innerHTML = '<td colspan="7" style="color: #95a5a6; text-align: center;">No broadcasts yet</td>';
                    return;
                }}

                history.forEach(b => {{
                    const row = table.insertRow(-1);
                    const date = new Date(b.created_at).toLocaleString();
                    const statusClass = 'status-' + b.status;
                    row.innerHTML = `
                        <td>${{date}}</td>
                        <td>${{b.audience}}</td>
                        <td class="message-preview" title="${{b.message}}">${{b.message}}</td>
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

        // Load user feedback
        async function loadFeedback() {{
            try {{
                const response = await fetch('/admin/feedback');
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
                const response = await fetch('/admin/costs');
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

            // Add rows for each plan
            ['free', 'premium'].forEach(plan => {{
                const data = periodData[plan];
                if (data) {{
                    const row = table.insertRow(-1);
                    row.className = 'plan-row';
                    const totalTokens = (data.prompt_tokens || 0) + (data.completion_tokens || 0);
                    row.innerHTML = `
                        <td>${{plan.charAt(0).toUpperCase() + plan.slice(1)}}</td>
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
        }}

        function formatCurrency(value) {{
            if (value === 0) return '$0.00';
            if (value < 0.01) return '<$0.01';
            return '$' + value.toFixed(2);
        }}

        function updatePreview() {{
            const audience = document.getElementById('audience').value;
            const message = document.getElementById('message').value;

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

            // Update recipient count (use timezone-aware counts)
            const inWindowCount = audienceStats[audience + '_in_window'] || 0;
            const totalCount = audienceStats[audience] || 0;
            const outsideCount = totalCount - inWindowCount;

            document.getElementById('recipientCount').textContent = inWindowCount;

            // Show outside window info
            const outsideInfo = document.getElementById('outsideWindowInfo');
            if (outsideCount > 0) {{
                outsideInfo.textContent = `(${{outsideCount}} outside window, won't receive)`;
            }} else {{
                outsideInfo.textContent = '';
            }}

            // Enable/disable send button
            // For scheduled broadcasts, don't require current in-window count
            const sendBtn = document.getElementById('sendBtn');
            const isScheduled = document.getElementById('scheduleCheckbox').checked;
            const hasMessage = message.trim().length > 0;

            if (isScheduled) {{
                // For scheduled: only need a message
                sendBtn.disabled = !hasMessage;
            }} else {{
                // For immediate: need message AND users in window
                sendBtn.disabled = !hasMessage || inWindowCount === 0;
            }}
        }}

        function showConfirmModal() {{
            const audience = document.getElementById('audience').value;
            const message = document.getElementById('message').value;
            const inWindowCount = audienceStats[audience + '_in_window'] || 0;
            const isScheduled = document.getElementById('scheduleCheckbox').checked;
            const scheduleDate = document.getElementById('scheduleDate').value;

            // Validate scheduled date if scheduling
            if (isScheduled) {{
                if (!validateScheduleDate()) {{
                    return; // Don't show modal if date is invalid
                }}
            }}

            document.getElementById('modalCount').textContent = inWindowCount;
            document.getElementById('modalMessage').textContent = '[Remyndrs System Message] ' + message;

            const scheduleInfo = document.getElementById('modalScheduleInfo');
            const modalTitle = document.getElementById('modalTitle');
            const modalSubtitle = document.getElementById('modalSubtitle');
            const confirmBtn = document.getElementById('modalConfirmBtn');

            if (isScheduled && scheduleDate) {{
                const scheduledTime = new Date(scheduleDate).toLocaleString();
                scheduleInfo.style.display = 'block';
                document.getElementById('modalScheduleTime').textContent = scheduledTime;
                modalTitle.textContent = '📅 Confirm Scheduled Broadcast';
                modalSubtitle.innerHTML = 'This message will be sent to users in the 8am-8pm window at the scheduled time:';
                confirmBtn.textContent = 'Schedule';
                confirmBtn.style.background = '#3498db';
            }} else {{
                scheduleInfo.style.display = 'none';
                modalTitle.textContent = '⚠️ Confirm Broadcast';
                modalSubtitle.innerHTML = 'You are about to send the following message to <strong id="modalCount">' + inWindowCount + '</strong> users:';
                confirmBtn.textContent = 'Send Now';
                confirmBtn.style.background = '#e74c3c';
            }}

            document.getElementById('confirmModal').classList.add('active');
        }}

        function hideConfirmModal() {{
            document.getElementById('confirmModal').classList.remove('active');
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
                    body: JSON.stringify({{ message, audience }})
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
                        scheduled_date: utcDate
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

                const response = await fetch(url);
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
                const response = await fetch(`/admin/conversations/flagged?include_reviewed=${{includeReviewed}}`);
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
                const response = await fetch('/admin/changelog');
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

        // Support ticket functions
        let currentTicketId = null;
        let currentTicketStatus = null;

        async function loadSupportTickets() {{
            try {{
                const includeClosed = document.getElementById('showClosedTickets').checked;
                const response = await fetch(`/admin/support/tickets?include_closed=${{includeClosed}}`);
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
                        <div style="background: white; padding: 15px; border-radius: 8px; margin-bottom: 10px; cursor: pointer; border-left: 4px solid ${{statusColor}};" onclick="openTicketModal(${{t.id}}, '${{t.status}}')">
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

        async function openTicketModal(ticketId, status) {{
            currentTicketId = ticketId;
            currentTicketStatus = status;
            document.getElementById('ticketModalTitle').textContent = `Ticket #${{ticketId}}`;
            document.getElementById('ticketModal').style.display = 'block';

            // Show/hide close/reopen buttons based on status
            document.getElementById('closeTicketBtn').style.display = status === 'open' ? 'block' : 'none';
            document.getElementById('reopenTicketBtn').style.display = status === 'closed' ? 'block' : 'none';

            await loadTicketMessages(ticketId);
        }}

        function closeTicketModal() {{
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
                    const label = isInbound ? 'User' : 'Support';
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
        loadStats();
        loadHistory();
        loadFeedback();
        loadCostData();
        loadMaintenanceMessage();
        loadScheduledBroadcasts();
        loadConversations();
        loadFlaggedConversations();
        loadChangelog();
        loadSupportTickets();

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
    </script>
</body>
</html>
    """

    return HTMLResponse(content=html)
