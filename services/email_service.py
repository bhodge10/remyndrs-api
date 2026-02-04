"""
Email Service
Handles sending email notifications via SMTP2GO
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from config import (
    SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD,
    SMTP_FROM_EMAIL, SUPPORT_EMAIL, SMTP_ENABLED, logger,
    APP_BASE_URL
)


def send_support_notification(ticket_id: int, phone_number: str, message: str, user_name: str = None):
    """Send email notification for new support ticket/message"""
    if not SMTP_ENABLED:
        logger.warning("SMTP not configured - skipping email notification")
        return False

    try:
        # Create email
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"[Support #{ticket_id}] New message from {phone_number[-4:]}"
        msg['From'] = SMTP_FROM_EMAIL
        msg['To'] = SUPPORT_EMAIL

        # Plain text version
        text_content = f"""
New Support Message

Ticket: #{ticket_id}
From: {user_name or 'Unknown'} (...{phone_number[-4:]})
Phone: {phone_number}

Message:
{message}

---
Reply via admin dashboard: {APP_BASE_URL}/admin/dashboard#support-{ticket_id}
        """

        # HTML version
        html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: #3498db; color: white; padding: 15px; border-radius: 8px 8px 0 0; }}
        .content {{ background: #f9f9f9; padding: 20px; border: 1px solid #ddd; }}
        .message {{ background: white; padding: 15px; border-radius: 8px; margin: 15px 0; border-left: 4px solid #3498db; }}
        .footer {{ padding: 15px; font-size: 12px; color: #666; }}
        .btn {{ display: inline-block; background: #27ae60; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2 style="margin: 0;">Support Ticket #{ticket_id}</h2>
        </div>
        <div class="content">
            <p><strong>From:</strong> {user_name or 'Unknown'} (...{phone_number[-4:]})</p>
            <div class="message">
                {message}
            </div>
            <p>
                <a href="{APP_BASE_URL}/admin/dashboard#support-{ticket_id}" class="btn">Reply in Dashboard</a>
            </p>
        </div>
        <div class="footer">
            <p>This is an automated notification from Remyndrs Support System.</p>
        </div>
    </div>
</body>
</html>
        """

        msg.attach(MIMEText(text_content, 'plain'))
        msg.attach(MIMEText(html_content, 'html'))

        # Send email with timeout to prevent hanging
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM_EMAIL, SUPPORT_EMAIL, msg.as_string())

        logger.info(f"Support notification sent for ticket #{ticket_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to send support notification: {e}")
        return False


def send_feedback_notification(category: str, phone_number: str, message: str, source: str = 'sms', user_name: str = None):
    """Send email notification for feedback, bug report, or web contact submission"""
    if not SMTP_ENABLED:
        logger.warning("SMTP not configured - skipping feedback notification")
        return False

    try:
        category_labels = {
            'feedback': 'Feedback',
            'bug': 'Bug Report',
            'question': 'Question',
            'support': 'Support Request',
        }
        category_label = category_labels.get(category, category.title())
        source_label = 'Web Form' if source == 'web' else 'SMS'

        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"[{category_label}] New {source_label.lower()} submission from ...{phone_number[-4:]}"
        msg['From'] = SMTP_FROM_EMAIL
        msg['To'] = SUPPORT_EMAIL

        text_content = f"""
New {category_label} ({source_label})

From: {user_name or 'Unknown'} (...{phone_number[-4:]})
Phone: {phone_number}
Category: {category_label}
Source: {source_label}

Message:
{message}

---
View in CS Portal: {APP_BASE_URL}/cs
        """

        html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: {'#e74c3c' if category == 'bug' else '#f39c12' if category == 'feedback' else '#3498db'}; color: white; padding: 15px; border-radius: 8px 8px 0 0; }}
        .content {{ background: #f9f9f9; padding: 20px; border: 1px solid #ddd; }}
        .message {{ background: white; padding: 15px; border-radius: 8px; margin: 15px 0; border-left: 4px solid {'#e74c3c' if category == 'bug' else '#f39c12' if category == 'feedback' else '#3498db'}; }}
        .footer {{ padding: 15px; font-size: 12px; color: #666; }}
        .btn {{ display: inline-block; background: #27ae60; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; }}
        .badge {{ display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2 style="margin: 0;">{category_label} from ...{phone_number[-4:]}</h2>
        </div>
        <div class="content">
            <p><strong>From:</strong> {user_name or 'Unknown'} (...{phone_number[-4:]})</p>
            <p><strong>Source:</strong> {source_label}</p>
            <div class="message">
                {message}
            </div>
            <p>
                <a href="{APP_BASE_URL}/cs" class="btn">View in CS Portal</a>
            </p>
        </div>
        <div class="footer">
            <p>This is an automated notification from Remyndrs.</p>
        </div>
    </div>
</body>
</html>
        """

        msg.attach(MIMEText(text_content, 'plain'))
        msg.attach(MIMEText(html_content, 'html'))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM_EMAIL, SUPPORT_EMAIL, msg.as_string())

        logger.info(f"Feedback notification sent for {category} from ...{phone_number[-4:]}")
        return True

    except Exception as e:
        logger.error(f"Failed to send feedback notification: {e}")
        return False
