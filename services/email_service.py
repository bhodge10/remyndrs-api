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


# Colors by severity, used by the two auto-detected issue emails below
_SEVERITY_COLORS = {
    'high': '#c0392b',
    'medium': '#e67e22',
    'low': '#3498db',
}

_CATEGORY_LABELS = {
    'service_outage': 'Service Outage',
    'reminder_delivery': 'Reminder Delivery',
    'billing': 'Billing',
    'confusion': 'Confusion',
    'other': 'Other',
}


def send_issue_flag_notification(phone_number: str, message: str, category: str,
                                 severity: str, summary: str = '',
                                 user_name: str = None):
    """
    Send email notification for an auto-detected issue report.

    Unlike FEEDBACK/BUG these were never explicitly submitted - the user just
    said something was wrong in normal conversation and we noticed.
    """
    if not SMTP_ENABLED:
        logger.warning("SMTP not configured - skipping issue flag notification")
        return False

    try:
        category_label = _CATEGORY_LABELS.get(category, category.replace('_', ' ').title())
        color = _SEVERITY_COLORS.get(severity, '#e67e22')
        last4 = phone_number[-4:] if phone_number else '????'

        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"[Auto-detected {severity.upper()}] {category_label} reported by ...{last4}"
        msg['From'] = SMTP_FROM_EMAIL
        msg['To'] = SUPPORT_EMAIL

        text_content = f"""
Auto-detected issue report

This user did not text SUPPORT or BUG - they described a problem in normal
conversation and it was flagged automatically. They received no acknowledgement,
so they are not expecting a reply yet.

From: {user_name or 'Unknown'} (...{last4})
Phone: {phone_number}
Category: {category_label}
Severity: {severity}
Summary: {summary or 'n/a'}

What they said:
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
        .header {{ background: {color}; color: white; padding: 15px; border-radius: 8px 8px 0 0; }}
        .content {{ background: #f9f9f9; padding: 20px; border: 1px solid #ddd; }}
        .message {{ background: white; padding: 15px; border-radius: 8px; margin: 15px 0; border-left: 4px solid {color}; }}
        .note {{ font-size: 13px; color: #555; background: #fff8e1; padding: 10px; border-radius: 6px; }}
        .footer {{ padding: 15px; font-size: 12px; color: #666; }}
        .btn {{ display: inline-block; background: #27ae60; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2 style="margin: 0;">{category_label} &mdash; severity {severity}</h2>
        </div>
        <div class="content">
            <p><strong>From:</strong> {user_name or 'Unknown'} (...{last4})</p>
            <p><strong>Summary:</strong> {summary or 'n/a'}</p>
            <div class="message">
                {message}
            </div>
            <p class="note">
                Auto-detected &mdash; the user did not text SUPPORT or BUG, and received
                no acknowledgement, so they are not expecting a reply yet.
            </p>
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

        logger.info(f"Issue flag notification sent ({category}/{severity}) for ...{last4}")
        return True

    except Exception as e:
        logger.error(f"Failed to send issue flag notification: {e}")
        return False


def send_outage_escalation_notification(reporter_count: int, hours: int = 24):
    """
    Send the loud email for the multi-user case.

    Several unrelated users complaining inside one day is what a real outage
    looks like from the inbox - this is the message that should have gone out
    on day one of the reminder outage.
    """
    if not SMTP_ENABLED:
        logger.warning("SMTP not configured - skipping outage escalation")
        return False

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"[URGENT] Possible outage - {reporter_count} users reported problems in {hours}h"
        msg['From'] = SMTP_FROM_EMAIL
        msg['To'] = SUPPORT_EMAIL

        text_content = f"""
POSSIBLE OUTAGE

{reporter_count} different users have reported a problem in the last {hours} hours.

Independent users complaining at the same time usually means something is
actually broken rather than individual confusion. Worth checking now:

- Are reminders going out? (Celery beat + worker on Render)
- Recent deploys?
- Twilio and OpenAI status

Review the individual reports in the CS Portal: {APP_BASE_URL}/cs
        """

        html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: #c0392b; color: white; padding: 15px; border-radius: 8px 8px 0 0; }}
        .content {{ background: #f9f9f9; padding: 20px; border: 1px solid #ddd; }}
        .big {{ font-size: 32px; font-weight: 700; color: #c0392b; }}
        .footer {{ padding: 15px; font-size: 12px; color: #666; }}
        .btn {{ display: inline-block; background: #c0392b; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2 style="margin: 0;">Possible Outage</h2>
        </div>
        <div class="content">
            <p><span class="big">{reporter_count}</span> different users reported a problem in the last {hours} hours.</p>
            <p>Independent users complaining at the same time usually means something is
               actually broken rather than individual confusion. Worth checking now:</p>
            <ul>
                <li>Are reminders going out? (Celery beat + worker on Render)</li>
                <li>Recent deploys?</li>
                <li>Twilio and OpenAI status</li>
            </ul>
            <p>
                <a href="{APP_BASE_URL}/cs" class="btn">View reports in CS Portal</a>
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

        logger.info(f"Outage escalation email sent ({reporter_count} reporters)")
        return True

    except Exception as e:
        logger.error(f"Failed to send outage escalation: {e}")
        return False


def send_issue_digest_notification(flags: list, hours: int = 24):
    """Send the daily rollup of auto-detected issue reports."""
    if not SMTP_ENABLED:
        logger.warning("SMTP not configured - skipping issue digest")
        return False

    if not flags:
        return False

    try:
        reporters = len({f['phone_number'] for f in flags})

        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"[Daily digest] {len(flags)} auto-detected issue report(s) from {reporters} user(s)"
        msg['From'] = SMTP_FROM_EMAIL
        msg['To'] = SUPPORT_EMAIL

        # Group by category so an outage pattern is visible at a glance
        by_category = {}
        for f in flags:
            by_category.setdefault(f['category'], []).append(f)

        text_lines = [
            f"Auto-detected issue reports - last {hours}h",
            f"{len(flags)} report(s) from {reporters} distinct user(s)",
            "",
        ]
        html_sections = []

        for category, items in sorted(by_category.items(), key=lambda kv: -len(kv[1])):
            label = _CATEGORY_LABELS.get(
                category.replace('auto_', ''), category.replace('auto_', '').replace('_', ' ').title()
            )
            text_lines.append(f"{label} ({len(items)})")
            rows = []
            for item in items:
                last4 = (item['phone_number'] or '????')[-4:]
                preview = (item['message'] or '').strip().replace('\n', ' ')[:160]
                text_lines.append(f"  - ...{last4}: {preview}")
                rows.append(f"<li><strong>...{last4}</strong>: {preview}</li>")
            text_lines.append("")
            html_sections.append(
                f"<h3 style='margin-bottom:4px;'>{label} ({len(items)})</h3><ul>{''.join(rows)}</ul>"
            )

        text_lines.append("---")
        text_lines.append(f"View in CS Portal: {APP_BASE_URL}/cs")
        text_content = "\n".join(text_lines)

        html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: #34495e; color: white; padding: 15px; border-radius: 8px 8px 0 0; }}
        .content {{ background: #f9f9f9; padding: 20px; border: 1px solid #ddd; }}
        .footer {{ padding: 15px; font-size: 12px; color: #666; }}
        .btn {{ display: inline-block; background: #27ae60; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; }}
        li {{ margin-bottom: 6px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2 style="margin: 0;">Auto-detected issue reports</h2>
            <p style="margin: 4px 0 0;">Last {hours}h &mdash; {len(flags)} report(s) from {reporters} user(s)</p>
        </div>
        <div class="content">
            {''.join(html_sections)}
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

        logger.info(f"Issue digest sent ({len(flags)} flags, {reporters} users)")
        return True

    except Exception as e:
        logger.error(f"Failed to send issue digest: {e}")
        return False
