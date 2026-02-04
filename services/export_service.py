"""
Export Service
Handles exporting user data and emailing it to the user
"""

import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from database import get_db_connection, return_db_connection
from config import (
    SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD,
    SMTP_FROM_EMAIL, SMTP_ENABLED, logger
)


def get_user_export_data(phone_number: str) -> dict:
    """Collect all user data for export"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # User profile
        c.execute("""
            SELECT phone_number, first_name, last_name, email, zip_code, timezone,
                   onboarding_complete, premium_status, created_at, last_active_at
            FROM users WHERE phone_number = %s
        """, (phone_number,))
        user_row = c.fetchone()
        if not user_row:
            return None

        profile = {
            'phone_number': user_row[0], 'first_name': user_row[1], 'last_name': user_row[2],
            'email': user_row[3], 'zip_code': user_row[4], 'timezone': user_row[5],
            'onboarding_complete': user_row[6], 'premium_status': user_row[7],
            'created_at': user_row[8].isoformat() if user_row[8] else None,
            'last_active_at': user_row[9].isoformat() if user_row[9] else None
        }

        # Reminders
        c.execute("""
            SELECT id, reminder_text, reminder_date, sent, created_at
            FROM reminders WHERE phone_number = %s ORDER BY created_at DESC
        """, (phone_number,))
        reminders = [
            {'id': r[0], 'text': r[1], 'date': r[2].isoformat() if r[2] else None,
             'sent': r[3], 'created_at': r[4].isoformat() if r[4] else None}
            for r in c.fetchall()
        ]

        # Recurring reminders
        c.execute("""
            SELECT id, reminder_text, recurrence_type, recurrence_day, reminder_time, timezone, active, created_at
            FROM recurring_reminders WHERE phone_number = %s
        """, (phone_number,))
        recurring = [
            {'id': r[0], 'text': r[1], 'type': r[2], 'day': r[3], 'time': str(r[4]),
             'timezone': r[5], 'active': r[6], 'created_at': r[7].isoformat() if r[7] else None}
            for r in c.fetchall()
        ]

        # Memories
        c.execute("""
            SELECT id, memory_text, created_at
            FROM memories WHERE phone_number = %s ORDER BY created_at DESC
        """, (phone_number,))
        memories = [
            {'id': r[0], 'text': r[1], 'created_at': r[2].isoformat() if r[2] else None}
            for r in c.fetchall()
        ]

        # Lists and items
        c.execute("""
            SELECT id, list_name, created_at
            FROM lists WHERE phone_number = %s ORDER BY created_at
        """, (phone_number,))
        lists_data = []
        for lst in c.fetchall():
            c.execute("""
                SELECT id, item_text, completed, created_at
                FROM list_items WHERE list_id = %s ORDER BY created_at
            """, (lst[0],))
            items = [
                {'id': i[0], 'text': i[1], 'completed': i[2],
                 'created_at': i[3].isoformat() if i[3] else None}
                for i in c.fetchall()
            ]
            lists_data.append({
                'id': lst[0], 'name': lst[1],
                'created_at': lst[2].isoformat() if lst[2] else None,
                'items': items
            })

        from datetime import datetime
        return {
            'exported_at': datetime.utcnow().isoformat() + 'Z',
            'profile': profile,
            'reminders': reminders,
            'recurring_reminders': recurring,
            'memories': memories,
            'lists': lists_data
        }
    except Exception as e:
        logger.error(f"Error collecting export data: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def export_and_email_user_data(phone_number: str, email: str) -> bool:
    """Export user data and email it as a JSON attachment"""
    if not SMTP_ENABLED:
        logger.warning("SMTP not configured - cannot email data export")
        return False

    data = get_user_export_data(phone_number)
    if not data:
        return False

    try:
        msg = MIMEMultipart()
        msg['Subject'] = 'Your Remyndrs Data Export'
        msg['From'] = SMTP_FROM_EMAIL
        msg['To'] = email

        body = MIMEText(
            "Hi!\n\n"
            "Attached is your complete Remyndrs data export. "
            "This includes your profile, reminders, recurring reminders, memories, and lists.\n\n"
            "The file is in JSON format, which can be opened with any text editor.\n\n"
            "- The Remyndrs Team",
            'plain'
        )
        msg.attach(body)

        # Attach JSON data
        json_data = json.dumps(data, indent=2, default=str)
        attachment = MIMEApplication(json_data.encode('utf-8'), _subtype='json')
        attachment.add_header('Content-Disposition', 'attachment', filename='remyndrs-data-export.json')
        msg.attach(attachment)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM_EMAIL, email, msg.as_string())

        logger.info(f"Data export emailed to ...{phone_number[-4:]}")
        return True

    except Exception as e:
        logger.error(f"Failed to email data export: {e}")
        return False
