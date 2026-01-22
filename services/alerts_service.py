"""
Alerts Service
Handles sending monitoring alerts via Microsoft Teams and Email.

Alerts are sent for:
- Critical issues detected
- Health score drops below threshold
- Pattern regressions
- Weekly health reports
"""

import json
import smtplib
import requests
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, List, Optional

from config import (
    SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD,
    SMTP_FROM_EMAIL, SMTP_ENABLED, logger, APP_BASE_URL, ENVIRONMENT
)
from database import get_setting
from services.sms_service import send_sms

# ============================================================================
# CONFIGURATION
# ============================================================================

# Environment variables for alerts (loaded from database settings for flexibility)
# Set via admin dashboard or database:
#   - alert_teams_webhook_url: Microsoft Teams incoming webhook URL
#   - alert_email_recipients: Comma-separated email addresses
#   - alert_health_threshold: Health score threshold for alerts (default: 70)
#   - alert_enabled: "true" or "false" to enable/disable alerts

def get_teams_webhook_url() -> Optional[str]:
    """Get Teams webhook URL from settings"""
    return get_setting("alert_teams_webhook_url")

def get_alert_email_recipients() -> List[str]:
    """Get email recipients from settings"""
    recipients = get_setting("alert_email_recipients", "")
    if not recipients:
        return []
    return [email.strip() for email in recipients.split(",") if email.strip()]

def get_health_threshold() -> int:
    """Get health score alert threshold"""
    try:
        return int(get_setting("alert_health_threshold", "70"))
    except ValueError:
        return 70

def is_alerts_enabled() -> bool:
    """Check if alerts are enabled"""
    return get_setting("alert_enabled", "true").lower() == "true"

def get_sms_alert_numbers() -> List[str]:
    """Get phone numbers for SMS alerts (critical only)"""
    numbers = get_setting("alert_sms_numbers", "")
    if not numbers:
        return []
    return [num.strip() for num in numbers.split(",") if num.strip()]

def is_sms_alerts_enabled() -> bool:
    """Check if SMS alerts are enabled for critical issues"""
    return get_setting("alert_sms_enabled", "false").lower() == "true"


# ============================================================================
# MICROSOFT TEAMS ALERTS
# ============================================================================

def send_teams_alert(
    title: str,
    message: str,
    severity: str = "info",
    facts: List[Dict] = None,
    actions: List[Dict] = None
) -> bool:
    """
    Send an alert to Microsoft Teams via incoming webhook.

    Args:
        title: Alert title
        message: Alert message body
        severity: "critical", "warning", "info", "success"
        facts: List of {"name": "key", "value": "value"} for details
        actions: List of {"name": "text", "url": "link"} for action buttons

    Returns:
        True if sent successfully, False otherwise
    """
    webhook_url = get_teams_webhook_url()

    if not webhook_url:
        logger.debug("Teams webhook URL not configured - skipping Teams alert")
        return False

    if not is_alerts_enabled():
        logger.debug("Alerts disabled - skipping Teams alert")
        return False

    try:
        # Color coding based on severity
        colors = {
            "critical": "FF0000",  # Red
            "warning": "FFA500",   # Orange
            "info": "0078D7",      # Blue
            "success": "00FF00",   # Green
        }
        theme_color = colors.get(severity, colors["info"])

        # Build the adaptive card payload (Teams message format)
        card = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": theme_color,
            "summary": title,
            "sections": [{
                "activityTitle": f"🔔 {title}",
                "activitySubtitle": f"Remyndrs Monitoring ({ENVIRONMENT})",
                "activityImage": "https://remyndrs.com/icon.png",  # Optional logo
                "facts": facts or [],
                "markdown": True,
                "text": message
            }]
        }

        # Add action buttons if provided
        if actions:
            card["potentialAction"] = [
                {
                    "@type": "OpenUri",
                    "name": action["name"],
                    "targets": [{"os": "default", "uri": action["url"]}]
                }
                for action in actions
            ]

        # Send to Teams
        response = requests.post(
            webhook_url,
            json=card,
            headers={"Content-Type": "application/json"},
            timeout=10
        )

        if response.status_code == 200:
            logger.info(f"Teams alert sent: {title}")
            return True
        else:
            logger.error(f"Teams alert failed: {response.status_code} - {response.text}")
            return False

    except Exception as e:
        logger.error(f"Failed to send Teams alert: {e}")
        return False


# ============================================================================
# SMS ALERTS (Critical issues only)
# ============================================================================

def send_sms_alert(message: str, recipients: List[str] = None) -> bool:
    """
    Send SMS alert for critical issues only.

    Args:
        message: Alert message (keep short - SMS has 160 char limit)
        recipients: Optional list of phone numbers (defaults to configured numbers)

    Returns:
        True if at least one SMS sent successfully
    """
    if not is_sms_alerts_enabled():
        logger.debug("SMS alerts disabled - skipping")
        return False

    if not is_alerts_enabled():
        logger.debug("Alerts disabled - skipping SMS")
        return False

    recipients = recipients or get_sms_alert_numbers()
    if not recipients:
        logger.debug("No SMS recipients configured - skipping")
        return False

    success_count = 0
    for phone in recipients:
        try:
            result = send_sms(phone, message)
            if result:
                success_count += 1
                logger.info(f"SMS alert sent to ...{phone[-4:]}")
        except Exception as e:
            logger.error(f"Failed to send SMS to ...{phone[-4:]}: {e}")

    return success_count > 0


# ============================================================================
# EMAIL ALERTS
# ============================================================================

def send_email_alert(
    subject: str,
    text_content: str,
    html_content: str = None,
    recipients: List[str] = None
) -> bool:
    """
    Send an alert email.

    Args:
        subject: Email subject
        text_content: Plain text body
        html_content: Optional HTML body
        recipients: Optional list of recipients (defaults to configured recipients)

    Returns:
        True if sent successfully, False otherwise
    """
    if not SMTP_ENABLED:
        logger.debug("SMTP not configured - skipping email alert")
        return False

    if not is_alerts_enabled():
        logger.debug("Alerts disabled - skipping email alert")
        return False

    recipients = recipients or get_alert_email_recipients()
    if not recipients:
        logger.debug("No email recipients configured - skipping email alert")
        return False

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"[Remyndrs {ENVIRONMENT.upper()}] {subject}"
        msg['From'] = SMTP_FROM_EMAIL
        msg['To'] = ", ".join(recipients)

        msg.attach(MIMEText(text_content, 'plain'))

        if html_content:
            msg.attach(MIMEText(html_content, 'html'))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM_EMAIL, recipients, msg.as_string())

        logger.info(f"Email alert sent: {subject} to {len(recipients)} recipients")
        return True

    except Exception as e:
        logger.error(f"Failed to send email alert: {e}")
        return False


# ============================================================================
# HIGH-LEVEL ALERT FUNCTIONS
# ============================================================================

def alert_critical_issues(issues: List[Dict]) -> bool:
    """
    Send alert for critical issues detected.

    Args:
        issues: List of critical issue dicts with id, issue_type, severity, details
    """
    if not issues:
        return False

    count = len(issues)
    title = f"🚨 {count} Critical Issue{'s' if count > 1 else ''} Detected"

    # Build facts for Teams
    facts = [
        {"name": "Issues", "value": str(count)},
        {"name": "Environment", "value": ENVIRONMENT},
        {"name": "Time", "value": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")},
    ]

    # Issue details
    issue_lines = []
    for issue in issues[:5]:  # Limit to 5
        issue_lines.append(f"• #{issue['id']} [{issue['severity']}] {issue['issue_type']}")

    if count > 5:
        issue_lines.append(f"• ... and {count - 5} more")

    message = "Critical issues require immediate attention:\n\n" + "\n".join(issue_lines)

    # Send to both channels
    teams_sent = send_teams_alert(
        title=title,
        message=message,
        severity="critical",
        facts=facts,
        actions=[{"name": "View Issues", "url": f"{APP_BASE_URL}/admin/dashboard#monitoring"}]
    )

    # Email
    text_content = f"""
CRITICAL ISSUES DETECTED

{count} critical issue(s) need immediate attention:

{chr(10).join(issue_lines)}

View and resolve issues:
{APP_BASE_URL}/admin/dashboard#monitoring

---
Remyndrs Monitoring System
Environment: {ENVIRONMENT}
Time: {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
    """

    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: #e74c3c; color: white; padding: 15px; border-radius: 8px 8px 0 0; }}
        .content {{ background: #f9f9f9; padding: 20px; border: 1px solid #ddd; }}
        .issue {{ background: white; padding: 10px; margin: 5px 0; border-left: 4px solid #e74c3c; }}
        .btn {{ display: inline-block; background: #e74c3c; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; }}
        .footer {{ padding: 15px; font-size: 12px; color: #666; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2 style="margin: 0;">🚨 Critical Issues Detected</h2>
        </div>
        <div class="content">
            <p><strong>{count} critical issue(s)</strong> need immediate attention:</p>
            {''.join(f'<div class="issue">{line[2:]}</div>' for line in issue_lines)}
            <p style="margin-top: 20px;">
                <a href="{APP_BASE_URL}/admin/dashboard#monitoring" class="btn">View Issues</a>
            </p>
        </div>
        <div class="footer">
            <p>Remyndrs Monitoring | {ENVIRONMENT} | {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}</p>
        </div>
    </div>
</body>
</html>
    """

    email_sent = send_email_alert(
        subject=f"🚨 {count} Critical Issues Detected",
        text_content=text_content,
        html_content=html_content
    )

    # SMS alert for critical issues
    sms_message = f"🚨 REMYNDRS ALERT: {count} critical issue(s) detected. Check {APP_BASE_URL}/admin/dashboard#monitoring"
    sms_sent = send_sms_alert(sms_message)

    return teams_sent or email_sent or sms_sent


def alert_health_drop(health_score: float, previous_score: float = None, details: Dict = None) -> bool:
    """
    Send alert when health score drops below threshold.

    Args:
        health_score: Current health score
        previous_score: Previous health score (for comparison)
        details: Health metrics dict
    """
    threshold = get_health_threshold()

    if health_score >= threshold:
        return False  # No alert needed

    title = f"⚠️ Health Score Critical: {health_score:.0f}/100"

    severity = "critical" if health_score < 50 else "warning"

    facts = [
        {"name": "Health Score", "value": f"{health_score:.0f}/100"},
        {"name": "Threshold", "value": str(threshold)},
    ]

    if previous_score:
        change = health_score - previous_score
        facts.append({"name": "Change", "value": f"{change:+.1f}"})

    if details:
        facts.extend([
            {"name": "Issue Rate", "value": f"{details.get('issue_rate', 0)}%"},
            {"name": "Open Issues", "value": str(details.get('open_issues', 0))},
        ])

    message = f"System health has dropped to {health_score:.0f}/100, below the threshold of {threshold}."

    if details and details.get('top_issue_types'):
        top_issues = ", ".join(t['type'] for t in details['top_issue_types'][:3])
        message += f"\n\nTop issues: {top_issues}"

    # Send Teams
    teams_sent = send_teams_alert(
        title=title,
        message=message,
        severity=severity,
        facts=facts,
        actions=[{"name": "View Dashboard", "url": f"{APP_BASE_URL}/admin/dashboard#monitoring"}]
    )

    # Send Email
    text_content = f"""
HEALTH SCORE ALERT

System health has dropped to {health_score:.0f}/100
Threshold: {threshold}
{"Change: " + str(round(health_score - previous_score, 1)) if previous_score else ""}

{f"Issue Rate: {details.get('issue_rate', 0)}%" if details else ""}
{f"Open Issues: {details.get('open_issues', 0)}" if details else ""}

Take action:
{APP_BASE_URL}/admin/dashboard#monitoring

---
Remyndrs Monitoring System
    """

    email_sent = send_email_alert(
        subject=f"⚠️ Health Score Critical: {health_score:.0f}/100",
        text_content=text_content
    )

    # SMS only for critical health (< 50)
    sms_sent = False
    if health_score < 50:
        sms_message = f"🚨 REMYNDRS: Health critical at {health_score:.0f}/100! {details.get('open_issues', 0)} open issues. Check dashboard now."
        sms_sent = send_sms_alert(sms_message)

    return teams_sent or email_sent or sms_sent


def alert_regressions(regressions: List[Dict]) -> bool:
    """
    Send alert when fixed patterns start recurring.

    Args:
        regressions: List of regression dicts with pattern_name, new_issues_since, etc.
    """
    if not regressions:
        return False

    count = len(regressions)
    title = f"🔄 {count} Pattern Regression{'s' if count > 1 else ''} Detected"

    facts = [
        {"name": "Regressions", "value": str(count)},
        {"name": "Time", "value": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")},
    ]

    regression_lines = []
    for r in regressions[:5]:
        regression_lines.append(f"• {r['pattern_name']}: {r['new_issues_since']} new issues")

    message = "Previously fixed patterns are recurring:\n\n" + "\n".join(regression_lines)

    # Send Teams
    teams_sent = send_teams_alert(
        title=title,
        message=message,
        severity="warning",
        facts=facts,
        actions=[{"name": "View Patterns", "url": f"{APP_BASE_URL}/admin/dashboard#monitoring"}]
    )

    # Send Email
    text_content = f"""
PATTERN REGRESSIONS DETECTED

{count} previously fixed pattern(s) are recurring:

{chr(10).join(regression_lines)}

These patterns were marked as resolved but new issues matching them have appeared.
Review the fixes and consider more permanent solutions.

{APP_BASE_URL}/admin/dashboard#monitoring

---
Remyndrs Monitoring System
    """

    email_sent = send_email_alert(
        subject=f"🔄 {count} Pattern Regressions Detected",
        text_content=text_content
    )

    return teams_sent or email_sent


def send_weekly_report_alert(report: Dict) -> bool:
    """
    Send weekly health report via Teams and Email.

    Args:
        report: Weekly report dict from generate_weekly_report()
    """
    health = report.get('current_health', {})
    health_score = health.get('health_score', 0)

    # Determine severity based on health
    if health_score >= 90:
        severity = "success"
        emoji = "✅"
    elif health_score >= 70:
        severity = "info"
        emoji = "📊"
    else:
        severity = "warning"
        emoji = "⚠️"

    title = f"{emoji} Weekly Health Report: {health_score:.0f}/100"

    facts = [
        {"name": "Health Score", "value": f"{health_score:.0f}/100 ({health.get('health_status', 'unknown')})"},
        {"name": "Issue Rate", "value": f"{health.get('issue_rate', 0)}%"},
        {"name": "Resolution Rate", "value": f"{health.get('resolution_rate', 0)}%"},
        {"name": "Open Issues", "value": str(health.get('open_issues', 0))},
    ]

    if report.get('health_change'):
        change = report['health_change']
        change_text = f"{change:+.1f}" + (" 📈" if change > 0 else " 📉" if change < 0 else "")
        facts.append({"name": "Weekly Change", "value": change_text})

    message = f"Weekly health report for {report.get('start_date', '')} to {report.get('end_date', '')}."

    if report.get('recommendations'):
        high_priority = [r for r in report['recommendations'] if r['priority'] == 'high']
        if high_priority:
            message += f"\n\n⚠️ {len(high_priority)} high-priority recommendation(s)"

    if report.get('regressions'):
        message += f"\n\n🔄 {len(report['regressions'])} regression(s) detected"

    # Send Teams
    teams_sent = send_teams_alert(
        title=title,
        message=message,
        severity=severity,
        facts=facts,
        actions=[{"name": "View Full Report", "url": f"{APP_BASE_URL}/admin/dashboard#monitoring"}]
    )

    # Build detailed email
    recommendations_text = ""
    if report.get('recommendations'):
        recommendations_text = "\nRECOMMENDATIONS:\n"
        for rec in report['recommendations']:
            recommendations_text += f"  [{rec['priority'].upper()}] {rec['title']}\n"
            recommendations_text += f"    → {rec['action']}\n"

    text_content = f"""
WEEKLY HEALTH REPORT
{report.get('start_date', '')} to {report.get('end_date', '')}

HEALTH SCORE: {health_score:.0f}/100 ({health.get('health_status', 'unknown')})
{f"Weekly Change: {report.get('health_change', 0):+.1f}" if report.get('health_change') else ""}

KEY METRICS:
  Issue Rate: {health.get('issue_rate', 0)}%
  Resolution Rate: {health.get('resolution_rate', 0)}%
  Open Issues: {health.get('open_issues', 0)}
  Total Interactions: {health.get('total_interactions', 0):,}
{recommendations_text}
{f"REGRESSIONS: {len(report.get('regressions', []))} pattern(s) recurring" if report.get('regressions') else ""}

View full report:
{APP_BASE_URL}/admin/dashboard#monitoring

---
Remyndrs Monitoring System
    """

    email_sent = send_email_alert(
        subject=f"{emoji} Weekly Health Report: {health_score:.0f}/100",
        text_content=text_content
    )

    return teams_sent or email_sent


# ============================================================================
# TEST FUNCTION
# ============================================================================

def send_test_alert() -> Dict:
    """
    Send a test alert to verify configuration.
    Returns dict with results for each channel.
    """
    results = {
        "teams": False,
        "email": False,
        "sms": False,
        "config": {
            "alerts_enabled": is_alerts_enabled(),
            "teams_configured": bool(get_teams_webhook_url()),
            "email_configured": SMTP_ENABLED and bool(get_alert_email_recipients()),
            "sms_configured": is_sms_alerts_enabled() and bool(get_sms_alert_numbers()),
            "health_threshold": get_health_threshold(),
            "email_recipients": get_alert_email_recipients(),
            "sms_recipients": [f"...{n[-4:]}" for n in get_sms_alert_numbers()],
        }
    }

    if not is_alerts_enabled():
        return results

    # Test Teams
    results["teams"] = send_teams_alert(
        title="🧪 Test Alert",
        message="This is a test alert from Remyndrs monitoring system.",
        severity="info",
        facts=[
            {"name": "Type", "value": "Test"},
            {"name": "Time", "value": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")},
        ],
        actions=[{"name": "View Dashboard", "url": f"{APP_BASE_URL}/admin/dashboard"}]
    )

    # Test Email
    results["email"] = send_email_alert(
        subject="🧪 Test Alert",
        text_content=f"""
This is a test alert from Remyndrs monitoring system.

Time: {datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
Environment: {ENVIRONMENT}

If you received this, email alerts are working correctly.

---
Remyndrs Monitoring System
        """
    )

    # Test SMS (only if explicitly enabled - costs money)
    if is_sms_alerts_enabled():
        results["sms"] = send_sms_alert(
            f"🧪 Remyndrs test alert - {datetime.utcnow().strftime('%H:%M UTC')}. SMS alerts working!"
        )

    return results
