"""
Admin Dashboard
HTML dashboard for viewing metrics and broadcast messaging
"""

import secrets
import asyncio
from datetime import datetime
import pytz
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional
from services.metrics_service import get_all_metrics, get_cost_analytics
from services.sms_service import send_sms
from database import get_db_connection, return_db_connection
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
        if request.audience == "all":
            c.execute('''
                SELECT phone_number, timezone FROM users
                WHERE onboarding_complete = TRUE
            ''')
        elif request.audience == "free":
            c.execute('''
                SELECT phone_number, timezone FROM users
                WHERE onboarding_complete = TRUE
                AND (premium_status = 'free' OR premium_status IS NULL)
            ''')
        elif request.audience == "premium":
            c.execute('''
                SELECT phone_number, timezone FROM users
                WHERE onboarding_complete = TRUE
                AND premium_status = 'premium'
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
    </style>
</head>
<body>
    <h1>Remyndrs Dashboard</h1>

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

    <!-- Broadcast Section -->
    <div class="broadcast-section">
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
            Send Broadcast
        </button>
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
    <div class="section">
        <h2>User Feedback</h2>
        <table class="feedback-table" id="feedbackTable">
            <tr>
                <th>Date</th>
                <th>Phone</th>
                <th>Message</th>
                <th style="width: 80px; text-align: center;">Resolved</th>
            </tr>
            <tr id="feedbackLoading">
                <td colspan="4" style="color: #95a5a6; text-align: center;">Loading feedback...</td>
            </tr>
        </table>
    </div>

    <!-- Cost Analytics Section -->
    <div class="cost-section">
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

    <!-- Confirmation Modal -->
    <div class="modal" id="confirmModal">
        <div class="modal-content">
            <h3>⚠️ Confirm Broadcast</h3>
            <p>You are about to send the following message to <strong id="modalCount">0</strong> users:</p>
            <div style="background: #f8f9fa; padding: 15px; border-radius: 4px; margin: 15px 0; white-space: pre-wrap;">
                <em id="modalMessage"></em>
            </div>
            <p style="color: #e74c3c;"><strong>This action cannot be undone.</strong></p>
            <div class="modal-buttons">
                <button class="btn btn-secondary" onclick="hideConfirmModal()">Cancel</button>
                <button class="btn btn-danger" onclick="sendBroadcast()">Send Now</button>
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

                const table = document.getElementById('feedbackTable');
                const loadingRow = document.getElementById('feedbackLoading');
                if (loadingRow) loadingRow.remove();

                if (feedback.length === 0) {{
                    const row = table.insertRow(-1);
                    row.innerHTML = '<td colspan="4" style="color: #95a5a6; text-align: center;">No feedback yet</td>';
                    return;
                }}

                feedback.forEach(f => {{
                    const row = table.insertRow(-1);
                    const date = new Date(f.created_at).toLocaleString();
                    const resolvedClass = f.resolved ? '' : 'unresolved';
                    const checkedAttr = f.resolved ? 'checked' : '';
                    row.className = resolvedClass;
                    row.id = `feedback-row-${{f.id}}`;
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
                }});
            }} catch (e) {{
                console.error('Error loading feedback:', e);
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
                    if (result.resolved) {{
                        row.classList.remove('unresolved');
                    }} else {{
                        row.classList.add('unresolved');
                    }}
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

            // Enable/disable send button (based on in-window count)
            const sendBtn = document.getElementById('sendBtn');
            sendBtn.disabled = !message.trim() || inWindowCount === 0;
        }}

        function showConfirmModal() {{
            const audience = document.getElementById('audience').value;
            const message = document.getElementById('message').value;
            const inWindowCount = audienceStats[audience + '_in_window'] || 0;

            document.getElementById('modalCount').textContent = inWindowCount;
            document.getElementById('modalMessage').textContent = '[Remyndrs System Message] ' + message;
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

        // Initialize
        loadStats();
        loadHistory();
        loadFeedback();
        loadCostData();

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
