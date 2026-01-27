"""
Agent 1: Interaction Monitor
Analyzes user interactions to detect anomalies, errors, and confusion patterns.

Designed for multi-agent pipeline:
- Runs independently (manual or scheduled)
- Stores findings in monitoring_issues table
- Outputs structured data for Agent 2 (validator)

Usage:
    python -m agents.interaction_monitor              # Analyze last 24 hours
    python -m agents.interaction_monitor --hours 48   # Analyze last 48 hours
    python -m agents.interaction_monitor --report     # Generate report only (no DB writes)
"""

import re
import sys
import json
import argparse
from datetime import datetime, timedelta
from typing import Optional
from collections import defaultdict

# Add parent directory to path for imports
sys.path.insert(0, '.')

from database import get_monitoring_cursor, logger
from config import ENVIRONMENT


# ============================================================================
# ANOMALY DETECTION RULES
# ============================================================================

# Messages indicating user confusion
# Note: Be specific - avoid matching common words in legitimate requests
CONFUSION_PATTERNS = [
    r'\b(huh|confused|dont understand|don\'t understand)\b',
    r'\?\s*\?',  # Multiple question marks (e.g., "??")
    r'^(help|huh)\s*$',  # Single-word confusion (removed "how", "why" - too common)
    r'\b(not working|doesn\'t work|didn\'t work|broken|this is broken)\b',
    r'\b(that\'s wrong|that\'s incorrect|you got it wrong)\b',  # More specific than just "wrong"
    r'\b(try again)\b',  # Keep this - explicit retry request indicates failure
    r'^what\?+$',  # Just "what?" or "what??" indicates confusion
]

# System error responses (from our responses)
ERROR_RESPONSE_PATTERNS = [
    r'sorry.*went wrong',
    r'couldn\'t.*process',
    r'error.*occurred',
    r'please try again',
    r'unable to',
    r'failed to',
]

# Timezone-related issues
TIMEZONE_PATTERNS = [
    r'\b(wrong time|wrong timezone|different time)\b',
    r'\b(am|pm).*\b(should be|supposed to be)\b',
    r'\b(timezone|time zone)\b.*\b(change|update|fix)\b',
]

# Parsing failure indicators (in our responses)
# Note: Be careful with patterns - avoid matching normal conversational prompts
# "what would you like" and "did you mean" are often legitimate prompts, not failures
PARSING_FAILURE_PATTERNS = [
    r'i\'m not sure what you mean',
    r'i couldn\'t understand',
    r'i don\'t understand',
    r'i\'m having trouble understanding',
    r'could you (please )?rephrase',
    r'i\'m confused by',
    r'not sure (what|how) to',
]


# ============================================================================
# DATABASE SCHEMA
# ============================================================================

def init_monitoring_tables():
    """Create monitoring tables if they don't exist"""
    with get_monitoring_cursor() as cursor:
        # Main issues table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS monitoring_issues (
                id SERIAL PRIMARY KEY,
                log_id INTEGER REFERENCES logs(id),
                phone_number TEXT NOT NULL,
                issue_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'medium',
                details JSONB,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                validated BOOLEAN DEFAULT FALSE,
                validated_by TEXT,
                validated_at TIMESTAMP,
                resolution TEXT,
                resolved_at TIMESTAMP,
                false_positive BOOLEAN DEFAULT FALSE
            )
        ''')

        # Index for efficient queries
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_monitoring_issues_type
            ON monitoring_issues(issue_type, detected_at DESC)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_monitoring_issues_validated
            ON monitoring_issues(validated) WHERE validated = FALSE
        ''')

        # Unique constraint to prevent duplicate issues for same log
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_monitoring_issues_log_type
            ON monitoring_issues(log_id, issue_type) WHERE log_id IS NOT NULL
        ''')

        # Monitoring runs table (audit trail)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS monitoring_runs (
                id SERIAL PRIMARY KEY,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                logs_analyzed INTEGER DEFAULT 0,
                issues_found INTEGER DEFAULT 0,
                time_range_start TIMESTAMP,
                time_range_end TIMESTAMP,
                status TEXT DEFAULT 'running'
            )
        ''')

        logger.info("Monitoring tables initialized")


# ============================================================================
# ANOMALY DETECTORS
# ============================================================================

def detect_confusion(log: dict) -> Optional[dict]:
    """Detect user confusion based on message patterns"""
    msg_in = log['message_in'].lower()

    for pattern in CONFUSION_PATTERNS:
        if re.search(pattern, msg_in, re.IGNORECASE):
            return {
                'issue_type': 'user_confusion',
                'severity': 'medium',
                'details': {
                    'pattern_matched': pattern,
                    'user_message': log['message_in'][:200],
                    'our_response': log['message_out'][:200]
                }
            }
    return None


def detect_error_response(log: dict) -> Optional[dict]:
    """Detect when we sent an error response"""
    msg_out = log['message_out'].lower()

    for pattern in ERROR_RESPONSE_PATTERNS:
        if re.search(pattern, msg_out, re.IGNORECASE):
            return {
                'issue_type': 'error_response',
                'severity': 'high',
                'details': {
                    'pattern_matched': pattern,
                    'user_message': log['message_in'][:200],
                    'our_response': log['message_out'][:200],
                    'intent': log.get('intent')
                }
            }
    return None


def detect_parsing_failure(log: dict) -> Optional[dict]:
    """Detect when we couldn't parse the user's intent"""
    msg_out = log['message_out'].lower()

    for pattern in PARSING_FAILURE_PATTERNS:
        if re.search(pattern, msg_out, re.IGNORECASE):
            return {
                'issue_type': 'parsing_failure',
                'severity': 'medium',
                'details': {
                    'pattern_matched': pattern,
                    'user_message': log['message_in'][:200],
                    'our_response': log['message_out'][:200],
                    'intent': log.get('intent')
                }
            }
    return None


def detect_timezone_issue(log: dict) -> Optional[dict]:
    """Detect timezone-related complaints"""
    msg_in = log['message_in'].lower()

    for pattern in TIMEZONE_PATTERNS:
        if re.search(pattern, msg_in, re.IGNORECASE):
            return {
                'issue_type': 'timezone_issue',
                'severity': 'high',
                'details': {
                    'pattern_matched': pattern,
                    'user_message': log['message_in'][:200],
                    'our_response': log['message_out'][:200]
                }
            }
    return None


def detect_failed_action(log: dict) -> Optional[dict]:
    """Detect explicitly failed actions (success=False)"""
    if log.get('success') is False:
        return {
            'issue_type': 'failed_action',
            'severity': 'high',
            'details': {
                'intent': log.get('intent'),
                'user_message': log['message_in'][:200],
                'our_response': log['message_out'][:200]
            }
        }
    return None


def detect_low_confidence_rejection(log: dict, confidence_logs: list) -> Optional[dict]:
    """Detect when user rejected a low-confidence interpretation"""
    # Look for rejections in confidence logs for this user around this time
    for cl in confidence_logs:
        if cl['confirmed'] is False and cl['phone_number'] == log['phone_number']:
            # Check if timestamps are close (within 5 minutes)
            log_time = log.get('created_at')
            conf_time = cl.get('created_at')
            if log_time and conf_time:
                if abs((log_time - conf_time).total_seconds()) < 300:
                    return {
                        'issue_type': 'confidence_rejection',
                        'severity': 'medium',
                        'details': {
                            'confidence_score': cl['confidence_score'],
                            'threshold': cl['threshold'],
                            'action_type': cl['action_type'],
                            'user_message': cl.get('user_message', '')[:200]
                        }
                    }
    return None


def detect_repeated_attempts(logs_by_phone: dict) -> list:
    """Detect users making repeated similar attempts (frustration indicator)"""
    issues = []

    for phone, logs in logs_by_phone.items():
        if len(logs) < 3:
            continue

        # Look for similar messages within short time windows
        for i in range(len(logs) - 2):
            window = logs[i:i+3]
            messages = [l['message_in'].lower()[:50] for l in window]

            # Check if messages are similar (basic similarity)
            if len(set(messages)) == 1:  # All same
                time_span = (window[0]['created_at'] - window[-1]['created_at']).total_seconds()
                if time_span < 300:  # Within 5 minutes
                    issues.append({
                        'log_id': window[0]['id'],
                        'phone_number': phone,
                        'issue_type': 'repeated_attempts',
                        'severity': 'high',
                        'details': {
                            'attempt_count': 3,
                            'time_span_seconds': time_span,
                            'message': window[0]['message_in'][:200]
                        }
                    })
                    break  # One issue per user per run

    return issues


def detect_delivery_failures(hours: int) -> list:
    """Check for reminder delivery failures"""
    issues = []

    with get_monitoring_cursor() as cursor:
        cursor.execute('''
            SELECT id, phone_number, reminder_text, reminder_date,
                   delivery_status, error_message, created_at
            FROM reminders
            WHERE delivery_status = 'failed'
            AND created_at > NOW() - INTERVAL '%s hours'
            ORDER BY created_at DESC
        ''', (hours,))

        rows = cursor.fetchall()
        for row in rows:
            issues.append({
                'log_id': None,  # Not from logs table
                'phone_number': row[1],
                'issue_type': 'delivery_failure',
                'severity': 'critical',
                'details': {
                    'reminder_id': row[0],
                    'reminder_text': row[2][:100] if row[2] else '',
                    'scheduled_time': row[3].isoformat() if row[3] else None,
                    'error_message': row[5]
                }
            })

    return issues


# ============================================================================
# MAIN ANALYSIS ENGINE
# ============================================================================

def analyze_interactions(hours: int = 24, dry_run: bool = False) -> dict:
    """
    Main analysis function. Queries recent logs and detects anomalies.

    Args:
        hours: Number of hours to look back
        dry_run: If True, don't write to database

    Returns:
        dict with analysis results
    """
    init_monitoring_tables()

    # Start monitoring run
    run_id = None
    if not dry_run:
        with get_monitoring_cursor() as cursor:
            cursor.execute('''
                INSERT INTO monitoring_runs (time_range_start, time_range_end)
                VALUES (NOW() - INTERVAL '%s hours', NOW())
                RETURNING id
            ''', (hours,))
            run_id = cursor.fetchone()[0]

    results = {
        'run_id': run_id,
        'time_range_hours': hours,
        'started_at': datetime.utcnow().isoformat(),
        'logs_analyzed': 0,
        'issues_found': [],
        'summary': defaultdict(int)
    }

    try:
        # Fetch recent logs
        with get_monitoring_cursor() as cursor:
            cursor.execute('''
                SELECT id, phone_number, message_in, message_out, intent, success, created_at
                FROM logs
                WHERE created_at > NOW() - INTERVAL '%s hours'
                ORDER BY created_at DESC
            ''', (hours,))

            columns = ['id', 'phone_number', 'message_in', 'message_out', 'intent', 'success', 'created_at']
            logs = [dict(zip(columns, row)) for row in cursor.fetchall()]

        results['logs_analyzed'] = len(logs)

        # Fetch recent confidence logs for correlation
        with get_monitoring_cursor() as cursor:
            cursor.execute('''
                SELECT phone_number, action_type, confidence_score, threshold,
                       confirmed, user_message, created_at
                FROM confidence_logs
                WHERE created_at > NOW() - INTERVAL '%s hours'
            ''', (hours,))

            columns = ['phone_number', 'action_type', 'confidence_score', 'threshold',
                      'confirmed', 'user_message', 'created_at']
            confidence_logs = [dict(zip(columns, row)) for row in cursor.fetchall()]

        # Group logs by phone for pattern detection
        logs_by_phone = defaultdict(list)
        for log in logs:
            logs_by_phone[log['phone_number']].append(log)

        # Run all detectors on each log
        detectors = [
            detect_confusion,
            detect_error_response,
            detect_parsing_failure,
            detect_timezone_issue,
            detect_failed_action,
        ]

        seen_issues = set()  # Avoid duplicates

        for log in logs:
            for detector in detectors:
                issue = detector(log)
                if issue:
                    issue_key = (log['id'], issue['issue_type'])
                    if issue_key not in seen_issues:
                        seen_issues.add(issue_key)
                        issue['log_id'] = log['id']
                        issue['phone_number'] = log['phone_number']
                        results['issues_found'].append(issue)
                        results['summary'][issue['issue_type']] += 1

            # Check confidence rejections
            issue = detect_low_confidence_rejection(log, confidence_logs)
            if issue:
                issue_key = (log['id'], 'confidence_rejection')
                if issue_key not in seen_issues:
                    seen_issues.add(issue_key)
                    issue['log_id'] = log['id']
                    issue['phone_number'] = log['phone_number']
                    results['issues_found'].append(issue)
                    results['summary']['confidence_rejection'] += 1

        # Run aggregate pattern detectors
        repeated_issues = detect_repeated_attempts(logs_by_phone)
        for issue in repeated_issues:
            results['issues_found'].append(issue)
            results['summary']['repeated_attempts'] += 1

        # Check delivery failures
        delivery_issues = detect_delivery_failures(hours)
        for issue in delivery_issues:
            results['issues_found'].append(issue)
            results['summary']['delivery_failure'] += 1

        # Store issues in database (skip duplicates via unique constraint)
        if not dry_run and results['issues_found']:
            with get_monitoring_cursor() as cursor:
                for issue in results['issues_found']:
                    cursor.execute('''
                        INSERT INTO monitoring_issues
                        (log_id, phone_number, issue_type, severity, details)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (log_id, issue_type) WHERE log_id IS NOT NULL
                        DO NOTHING
                    ''', (
                        issue.get('log_id'),
                        issue['phone_number'],
                        issue['issue_type'],
                        issue['severity'],
                        json.dumps(issue['details'])
                    ))

        # Complete monitoring run
        if not dry_run and run_id:
            with get_monitoring_cursor() as cursor:
                cursor.execute('''
                    UPDATE monitoring_runs
                    SET completed_at = NOW(),
                        logs_analyzed = %s,
                        issues_found = %s,
                        status = 'completed'
                    WHERE id = %s
                ''', (results['logs_analyzed'], len(results['issues_found']), run_id))

        results['completed_at'] = datetime.utcnow().isoformat()
        results['summary'] = dict(results['summary'])

    except Exception as e:
        logger.error(f"Monitoring analysis failed: {e}", exc_info=True)
        results['error'] = str(e)

        if not dry_run and run_id:
            with get_monitoring_cursor() as cursor:
                cursor.execute('''
                    UPDATE monitoring_runs SET status = 'failed' WHERE id = %s
                ''', (run_id,))

    return results


def generate_report(results: dict) -> str:
    """Generate a human-readable report from analysis results"""
    lines = [
        "=" * 60,
        "INTERACTION MONITOR REPORT",
        f"Agent 1 - {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 60,
        "",
        f"Time Range: Last {results['time_range_hours']} hours",
        f"Logs Analyzed: {results['logs_analyzed']}",
        f"Issues Found: {len(results['issues_found'])}",
        "",
    ]

    if results.get('error'):
        lines.append(f"ERROR: {results['error']}")
        lines.append("")

    # Summary by type
    if results['summary']:
        lines.append("SUMMARY BY ISSUE TYPE:")
        lines.append("-" * 40)

        severity_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
        sorted_types = sorted(
            results['summary'].items(),
            key=lambda x: x[1],
            reverse=True
        )

        for issue_type, count in sorted_types:
            icon = {
                'delivery_failure': '🚨',
                'error_response': '❌',
                'failed_action': '⚠️',
                'timezone_issue': '🌐',
                'user_confusion': '❓',
                'parsing_failure': '🔍',
                'confidence_rejection': '🎯',
                'repeated_attempts': '🔄',
            }.get(issue_type, '•')
            lines.append(f"  {icon} {issue_type}: {count}")
        lines.append("")

    # Critical/High severity details
    critical_high = [i for i in results['issues_found']
                     if i['severity'] in ('critical', 'high')]

    if critical_high:
        lines.append("CRITICAL/HIGH SEVERITY ISSUES:")
        lines.append("-" * 40)

        for issue in critical_high[:10]:  # Limit to 10
            lines.append(f"\n[{issue['severity'].upper()}] {issue['issue_type']}")
            lines.append(f"  Phone: {issue['phone_number'][-4:]}****")
            if issue.get('log_id'):
                lines.append(f"  Log ID: {issue['log_id']}")

            details = issue.get('details', {})
            if details.get('user_message'):
                lines.append(f"  User: \"{details['user_message'][:80]}...\"")
            if details.get('our_response'):
                lines.append(f"  Response: \"{details['our_response'][:80]}...\"")
            if details.get('error_message'):
                lines.append(f"  Error: {details['error_message']}")

    lines.append("")
    lines.append("=" * 60)
    lines.append("END OF REPORT")
    lines.append("=" * 60)

    return "\n".join(lines)


def get_pending_issues(limit: int = 50) -> list:
    """Get issues pending validation (for Agent 2)"""
    with get_monitoring_cursor() as cursor:
        cursor.execute('''
            SELECT mi.id, mi.log_id, mi.phone_number, mi.issue_type,
                   mi.severity, mi.details, mi.detected_at,
                   l.message_in, l.message_out, l.intent
            FROM monitoring_issues mi
            LEFT JOIN logs l ON mi.log_id = l.id
            WHERE mi.validated = FALSE AND mi.false_positive = FALSE
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

        columns = ['id', 'log_id', 'phone_number', 'issue_type', 'severity',
                   'details', 'detected_at', 'message_in', 'message_out', 'intent']
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


# ============================================================================
# CLI INTERFACE
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Agent 1: Interaction Monitor - Detect anomalies in user interactions'
    )
    parser.add_argument(
        '--hours', type=int, default=24,
        help='Number of hours to analyze (default: 24)'
    )
    parser.add_argument(
        '--report', action='store_true',
        help='Generate report only (dry run, no DB writes)'
    )
    parser.add_argument(
        '--json', action='store_true',
        help='Output results as JSON'
    )
    parser.add_argument(
        '--pending', action='store_true',
        help='Show pending issues for validation'
    )

    args = parser.parse_args()

    if args.pending:
        issues = get_pending_issues()
        if args.json:
            # Convert datetime objects to strings
            for issue in issues:
                if issue.get('detected_at'):
                    issue['detected_at'] = issue['detected_at'].isoformat()
            print(json.dumps(issues, indent=2))
        else:
            print(f"\nPending Issues for Validation: {len(issues)}")
            print("-" * 50)
            for issue in issues:
                print(f"\n[{issue['severity'].upper()}] {issue['issue_type']}")
                print(f"  ID: {issue['id']}, Log: {issue['log_id']}")
                if issue.get('message_in'):
                    print(f"  User: \"{issue['message_in'][:60]}...\"")
        return

    print(f"\n🔍 Running Interaction Monitor (last {args.hours} hours)...")
    print(f"   Environment: {ENVIRONMENT}")
    print(f"   Dry run: {args.report}")
    print()

    results = analyze_interactions(hours=args.hours, dry_run=args.report)

    if args.json:
        # Convert for JSON serialization
        output = {
            'run_id': results['run_id'],
            'time_range_hours': results['time_range_hours'],
            'logs_analyzed': results['logs_analyzed'],
            'issues_count': len(results['issues_found']),
            'summary': results['summary'],
            'issues': results['issues_found']
        }
        print(json.dumps(output, indent=2, default=str))
    else:
        report = generate_report(results)
        print(report)


if __name__ == '__main__':
    main()
