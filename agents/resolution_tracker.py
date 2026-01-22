"""
Agent 3: Resolution Tracker
Tracks issue resolutions, measures system health, and generates improvement reports.

Designed for multi-agent pipeline:
- Reads validated issues from Agent 2
- Tracks resolutions and fixes applied
- Measures system health metrics over time
- Detects pattern recurrence (regressions)
- Generates weekly/monthly improvement reports

Usage:
    python -m agents.resolution_tracker                  # Show health dashboard
    python -m agents.resolution_tracker --resolve 123    # Resolve issue #123
    python -m agents.resolution_tracker --report weekly  # Weekly report
    python -m agents.resolution_tracker --trends         # Show trend analysis
"""

import sys
import json
import argparse
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from collections import defaultdict

# Add parent directory to path for imports
sys.path.insert(0, '.')

from database import get_db_cursor, logger
from config import ENVIRONMENT


# ============================================================================
# RESOLUTION CATEGORIES
# ============================================================================

RESOLUTION_TYPES = {
    'code_fix': {
        'label': 'Code Fix',
        'description': 'Bug fix or code improvement deployed',
        'icon': '🔧'
    },
    'prompt_update': {
        'label': 'AI Prompt Update',
        'description': 'Updated AI prompts or response templates',
        'icon': '🤖'
    },
    'config_change': {
        'label': 'Configuration Change',
        'description': 'Threshold, limit, or setting adjustment',
        'icon': '⚙️'
    },
    'documentation': {
        'label': 'Documentation',
        'description': 'Help text or user guidance improved',
        'icon': '📝'
    },
    'user_education': {
        'label': 'User Education',
        'description': 'Issue resolved through user communication',
        'icon': '💡'
    },
    'wont_fix': {
        'label': "Won't Fix",
        'description': 'Accepted behavior, not a bug',
        'icon': '🚫'
    },
    'duplicate': {
        'label': 'Duplicate',
        'description': 'Already tracked under another issue',
        'icon': '📋'
    },
    'cannot_reproduce': {
        'label': 'Cannot Reproduce',
        'description': 'Unable to reproduce the issue',
        'icon': '❓'
    },
}

# Health score thresholds
HEALTH_THRESHOLDS = {
    'excellent': 95,  # < 5% issue rate
    'good': 90,       # < 10% issue rate
    'fair': 80,       # < 20% issue rate
    'poor': 70,       # < 30% issue rate
    'critical': 0,    # >= 30% issue rate
}


# ============================================================================
# DATABASE OPERATIONS
# ============================================================================

def init_tracker_tables():
    """Create resolution tracker tables if they don't exist"""
    with get_db_cursor() as cursor:
        # Resolutions table - tracks how issues were resolved
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS issue_resolutions (
                id SERIAL PRIMARY KEY,
                issue_id INTEGER REFERENCES monitoring_issues(id),
                resolution_type TEXT NOT NULL,
                description TEXT,
                commit_ref TEXT,
                resolved_by TEXT,
                resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                verified BOOLEAN DEFAULT FALSE,
                verified_at TIMESTAMP
            )
        ''')

        # Health snapshots - periodic system health metrics
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS health_snapshots (
                id SERIAL PRIMARY KEY,
                snapshot_date DATE NOT NULL UNIQUE,
                total_interactions INTEGER DEFAULT 0,
                total_issues INTEGER DEFAULT 0,
                false_positives INTEGER DEFAULT 0,
                resolved_issues INTEGER DEFAULT 0,
                open_issues INTEGER DEFAULT 0,
                health_score FLOAT,
                issue_rate FLOAT,
                resolution_rate FLOAT,
                avg_resolution_hours FLOAT,
                top_issue_types JSONB,
                top_patterns JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Pattern resolutions - tracks when patterns are addressed
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pattern_resolutions (
                id SERIAL PRIMARY KEY,
                pattern_id INTEGER REFERENCES issue_patterns(id),
                resolution_type TEXT NOT NULL,
                description TEXT,
                resolved_by TEXT,
                resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                recurrence_count INTEGER DEFAULT 0,
                last_recurrence TIMESTAMP
            )
        ''')

        # Indexes
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_resolutions_issue
            ON issue_resolutions(issue_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_health_snapshots_date
            ON health_snapshots(snapshot_date DESC)
        ''')

        logger.info("Resolution tracker tables initialized")


def resolve_issue(issue_id: int, resolution_type: str, description: str = None,
                  commit_ref: str = None, resolved_by: str = 'agent3') -> bool:
    """Mark an issue as resolved"""
    if resolution_type not in RESOLUTION_TYPES:
        logger.error(f"Invalid resolution type: {resolution_type}")
        return False

    with get_db_cursor() as cursor:
        # Insert resolution record
        cursor.execute('''
            INSERT INTO issue_resolutions (issue_id, resolution_type, description, commit_ref, resolved_by)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        ''', (issue_id, resolution_type, description, commit_ref, resolved_by))

        resolution_id = cursor.fetchone()[0]

        # Update the monitoring issue
        cursor.execute('''
            UPDATE monitoring_issues
            SET resolution = %s,
                resolved_at = NOW()
            WHERE id = %s
        ''', (f"{resolution_type}: {description or ''}"[:500], issue_id))

        logger.info(f"Resolved issue #{issue_id} as {resolution_type}")
        return True


def resolve_pattern(pattern_id: int, resolution_type: str, description: str = None,
                    resolved_by: str = 'agent3') -> bool:
    """Mark a pattern as resolved"""
    with get_db_cursor() as cursor:
        # Insert pattern resolution
        cursor.execute('''
            INSERT INTO pattern_resolutions (pattern_id, resolution_type, description, resolved_by)
            VALUES (%s, %s, %s, %s)
            RETURNING id
        ''', (pattern_id, resolution_type, description, resolved_by))

        # Update pattern status
        cursor.execute('''
            UPDATE issue_patterns
            SET status = 'resolved',
                suggested_fix = %s
            WHERE id = %s
        ''', (description, pattern_id))

        logger.info(f"Resolved pattern #{pattern_id} as {resolution_type}")
        return True


def get_open_issues(limit: int = 50) -> List[Dict]:
    """Get validated issues that haven't been resolved"""
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT mi.id, mi.log_id, mi.phone_number, mi.issue_type,
                   mi.severity, mi.details, mi.detected_at, mi.validated_at,
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

        columns = ['id', 'log_id', 'phone_number', 'issue_type', 'severity',
                   'details', 'detected_at', 'validated_at', 'message_in', 'message_out']
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def get_resolved_issues(days: int = 7, limit: int = 50) -> List[Dict]:
    """Get recently resolved issues"""
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT mi.id, mi.issue_type, mi.severity, mi.resolved_at,
                   ir.resolution_type, ir.description, ir.commit_ref, ir.resolved_by
            FROM monitoring_issues mi
            LEFT JOIN issue_resolutions ir ON mi.id = ir.issue_id
            WHERE mi.resolved_at > NOW() - INTERVAL '%s days'
            ORDER BY mi.resolved_at DESC
            LIMIT %s
        ''', (days, limit))

        columns = ['id', 'issue_type', 'severity', 'resolved_at',
                   'resolution_type', 'description', 'commit_ref', 'resolved_by']
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


# ============================================================================
# HEALTH METRICS
# ============================================================================

def calculate_health_metrics(days: int = 7) -> Dict:
    """Calculate system health metrics for a time period"""
    with get_db_cursor() as cursor:
        metrics = {}

        # Total interactions
        cursor.execute('''
            SELECT COUNT(*) FROM logs
            WHERE created_at > NOW() - INTERVAL '%s days'
        ''', (days,))
        metrics['total_interactions'] = cursor.fetchone()[0]

        # Total issues detected
        cursor.execute('''
            SELECT COUNT(*) FROM monitoring_issues
            WHERE detected_at > NOW() - INTERVAL '%s days'
        ''', (days,))
        metrics['total_issues'] = cursor.fetchone()[0]

        # False positives
        cursor.execute('''
            SELECT COUNT(*) FROM monitoring_issues
            WHERE detected_at > NOW() - INTERVAL '%s days'
              AND false_positive = TRUE
        ''', (days,))
        metrics['false_positives'] = cursor.fetchone()[0]

        # Real issues (validated, not false positive)
        metrics['real_issues'] = metrics['total_issues'] - metrics['false_positives']

        # Resolved issues
        cursor.execute('''
            SELECT COUNT(*) FROM monitoring_issues
            WHERE detected_at > NOW() - INTERVAL '%s days'
              AND resolved_at IS NOT NULL
        ''', (days,))
        metrics['resolved_issues'] = cursor.fetchone()[0]

        # Open issues
        cursor.execute('''
            SELECT COUNT(*) FROM monitoring_issues
            WHERE validated = TRUE
              AND false_positive = FALSE
              AND resolved_at IS NULL
        ''', ())
        metrics['open_issues'] = cursor.fetchone()[0]

        # Calculate rates
        if metrics['total_interactions'] > 0:
            metrics['issue_rate'] = round(
                metrics['real_issues'] / metrics['total_interactions'] * 100, 2
            )
        else:
            metrics['issue_rate'] = 0

        if metrics['real_issues'] > 0:
            metrics['resolution_rate'] = round(
                metrics['resolved_issues'] / metrics['real_issues'] * 100, 1
            )
        else:
            metrics['resolution_rate'] = 100

        # Average resolution time
        cursor.execute('''
            SELECT AVG(EXTRACT(EPOCH FROM (resolved_at - detected_at)) / 3600)
            FROM monitoring_issues
            WHERE resolved_at IS NOT NULL
              AND detected_at > NOW() - INTERVAL '%s days'
        ''', (days,))
        result = cursor.fetchone()[0]
        metrics['avg_resolution_hours'] = round(result, 1) if result else 0

        # Health score (inverse of issue rate, weighted)
        if metrics['total_interactions'] > 0:
            # Base score from issue rate
            base_score = 100 - (metrics['issue_rate'] * 5)  # 1% issues = 5 point penalty

            # Bonus for high resolution rate
            resolution_bonus = (metrics['resolution_rate'] - 50) / 10 if metrics['resolution_rate'] > 50 else 0

            # Penalty for open critical/high issues
            cursor.execute('''
                SELECT COUNT(*) FROM monitoring_issues
                WHERE validated = TRUE AND false_positive = FALSE
                  AND resolved_at IS NULL
                  AND severity IN ('critical', 'high')
            ''')
            critical_open = cursor.fetchone()[0]
            critical_penalty = critical_open * 2

            metrics['health_score'] = max(0, min(100, base_score + resolution_bonus - critical_penalty))
        else:
            metrics['health_score'] = 100

        # Health status
        for status, threshold in HEALTH_THRESHOLDS.items():
            if metrics['health_score'] >= threshold:
                metrics['health_status'] = status
                break

        # Top issue types
        cursor.execute('''
            SELECT issue_type, COUNT(*) as cnt
            FROM monitoring_issues
            WHERE detected_at > NOW() - INTERVAL '%s days'
              AND false_positive = FALSE
            GROUP BY issue_type
            ORDER BY cnt DESC
            LIMIT 5
        ''', (days,))
        metrics['top_issue_types'] = [
            {'type': row[0], 'count': row[1]}
            for row in cursor.fetchall()
        ]

        # Top patterns
        cursor.execute('''
            SELECT ip.pattern_name, COUNT(ipl.id) as cnt
            FROM issue_patterns ip
            JOIN issue_pattern_links ipl ON ip.id = ipl.pattern_id
            JOIN monitoring_issues mi ON ipl.issue_id = mi.id
            WHERE mi.detected_at > NOW() - INTERVAL '%s days'
            GROUP BY ip.pattern_name
            ORDER BY cnt DESC
            LIMIT 5
        ''', (days,))
        metrics['top_patterns'] = [
            {'pattern': row[0], 'count': row[1]}
            for row in cursor.fetchall()
        ]

        return metrics


def save_health_snapshot(metrics: Dict = None):
    """Save a daily health snapshot"""
    if metrics is None:
        metrics = calculate_health_metrics(days=1)

    with get_db_cursor() as cursor:
        cursor.execute('''
            INSERT INTO health_snapshots (
                snapshot_date, total_interactions, total_issues, false_positives,
                resolved_issues, open_issues, health_score, issue_rate,
                resolution_rate, avg_resolution_hours, top_issue_types, top_patterns
            ) VALUES (
                CURRENT_DATE, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (snapshot_date) DO UPDATE SET
                total_interactions = EXCLUDED.total_interactions,
                total_issues = EXCLUDED.total_issues,
                false_positives = EXCLUDED.false_positives,
                resolved_issues = EXCLUDED.resolved_issues,
                open_issues = EXCLUDED.open_issues,
                health_score = EXCLUDED.health_score,
                issue_rate = EXCLUDED.issue_rate,
                resolution_rate = EXCLUDED.resolution_rate,
                avg_resolution_hours = EXCLUDED.avg_resolution_hours,
                top_issue_types = EXCLUDED.top_issue_types,
                top_patterns = EXCLUDED.top_patterns
        ''', (
            metrics['total_interactions'],
            metrics['total_issues'],
            metrics['false_positives'],
            metrics['resolved_issues'],
            metrics['open_issues'],
            metrics['health_score'],
            metrics['issue_rate'],
            metrics['resolution_rate'],
            metrics['avg_resolution_hours'],
            json.dumps(metrics['top_issue_types']),
            json.dumps(metrics['top_patterns'])
        ))

        logger.info(f"Saved health snapshot: score={metrics['health_score']}")


def get_health_trend(days: int = 30) -> List[Dict]:
    """Get health score trend over time"""
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT snapshot_date, health_score, issue_rate, resolution_rate,
                   total_interactions, total_issues, open_issues
            FROM health_snapshots
            WHERE snapshot_date > CURRENT_DATE - INTERVAL '%s days'
            ORDER BY snapshot_date ASC
        ''', (days,))

        columns = ['date', 'health_score', 'issue_rate', 'resolution_rate',
                   'interactions', 'issues', 'open_issues']
        return [
            {**dict(zip(columns, row)), 'date': row[0].isoformat()}
            for row in cursor.fetchall()
        ]


# ============================================================================
# REGRESSION DETECTION
# ============================================================================

def detect_regressions() -> List[Dict]:
    """Detect patterns that were resolved but are recurring"""
    regressions = []

    with get_db_cursor() as cursor:
        # Find patterns marked as resolved that have new issues
        cursor.execute('''
            SELECT ip.id, ip.pattern_name, pr.resolved_at,
                   COUNT(mi.id) as new_issues,
                   MAX(mi.detected_at) as latest_issue
            FROM issue_patterns ip
            JOIN pattern_resolutions pr ON ip.id = pr.pattern_id
            JOIN issue_pattern_links ipl ON ip.id = ipl.pattern_id
            JOIN monitoring_issues mi ON ipl.issue_id = mi.id
            WHERE ip.status = 'resolved'
              AND mi.detected_at > pr.resolved_at
              AND mi.false_positive = FALSE
            GROUP BY ip.id, ip.pattern_name, pr.resolved_at
            HAVING COUNT(mi.id) >= 2
            ORDER BY COUNT(mi.id) DESC
        ''')

        for row in cursor.fetchall():
            regressions.append({
                'pattern_id': row[0],
                'pattern_name': row[1],
                'resolved_at': row[2].isoformat() if row[2] else None,
                'new_issues_since': row[3],
                'latest_issue': row[4].isoformat() if row[4] else None,
                'status': 'regression'
            })

            # Update recurrence count
            cursor.execute('''
                UPDATE pattern_resolutions
                SET recurrence_count = recurrence_count + 1,
                    last_recurrence = NOW()
                WHERE pattern_id = %s
            ''', (row[0],))

            # Reopen pattern
            cursor.execute('''
                UPDATE issue_patterns
                SET status = 'regression'
                WHERE id = %s
            ''', (row[0],))

    return regressions


# ============================================================================
# REPORTS
# ============================================================================

def generate_weekly_report() -> Dict:
    """Generate a weekly improvement report"""
    report = {
        'period': 'weekly',
        'generated_at': datetime.utcnow().isoformat(),
        'start_date': (datetime.utcnow() - timedelta(days=7)).date().isoformat(),
        'end_date': datetime.utcnow().date().isoformat(),
    }

    # Current metrics
    current = calculate_health_metrics(days=7)
    report['current_health'] = current

    # Previous week for comparison
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT AVG(health_score), AVG(issue_rate), AVG(resolution_rate)
            FROM health_snapshots
            WHERE snapshot_date BETWEEN CURRENT_DATE - INTERVAL '14 days'
                                    AND CURRENT_DATE - INTERVAL '7 days'
        ''')
        row = cursor.fetchone()
        if row[0]:
            report['previous_health'] = {
                'health_score': round(row[0], 1),
                'issue_rate': round(row[1], 2) if row[1] else 0,
                'resolution_rate': round(row[2], 1) if row[2] else 0
            }
            report['health_change'] = round(current['health_score'] - row[0], 1)
        else:
            report['previous_health'] = None
            report['health_change'] = 0

    # Issues resolved this week
    report['resolved_this_week'] = get_resolved_issues(days=7)

    # Open issues needing attention
    report['open_issues'] = get_open_issues(limit=10)

    # Regressions
    report['regressions'] = detect_regressions()

    # Resolution breakdown
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT resolution_type, COUNT(*)
            FROM issue_resolutions
            WHERE resolved_at > NOW() - INTERVAL '7 days'
            GROUP BY resolution_type
            ORDER BY COUNT(*) DESC
        ''')
        report['resolution_breakdown'] = [
            {
                'type': row[0],
                'count': row[1],
                'label': RESOLUTION_TYPES.get(row[0], {}).get('label', row[0])
            }
            for row in cursor.fetchall()
        ]

    # Recommendations
    report['recommendations'] = generate_recommendations(current, report)

    return report


def generate_recommendations(metrics: Dict, report: Dict = None) -> List[Dict]:
    """Generate actionable recommendations based on metrics"""
    recommendations = []

    # High issue rate
    if metrics['issue_rate'] > 5:
        recommendations.append({
            'priority': 'high',
            'category': 'issue_rate',
            'title': 'High Issue Rate',
            'description': f"Issue rate is {metrics['issue_rate']}% (target: <5%). "
                          f"Focus on top issue types: {', '.join(t['type'] for t in metrics['top_issue_types'][:3])}",
            'action': 'Review top issue patterns and prioritize fixes'
        })

    # Low resolution rate
    if metrics['resolution_rate'] < 70:
        recommendations.append({
            'priority': 'high',
            'category': 'resolution_rate',
            'title': 'Low Resolution Rate',
            'description': f"Only {metrics['resolution_rate']}% of issues resolved. "
                          f"{metrics['open_issues']} issues still open.",
            'action': 'Schedule time to triage and resolve open issues'
        })

    # Slow resolution time
    if metrics['avg_resolution_hours'] > 48:
        recommendations.append({
            'priority': 'medium',
            'category': 'resolution_time',
            'title': 'Slow Resolution Time',
            'description': f"Average resolution time is {metrics['avg_resolution_hours']:.0f} hours. "
                          f"Target: <24 hours for high priority.",
            'action': 'Implement faster triage process for critical issues'
        })

    # Regressions
    if report and report.get('regressions'):
        recommendations.append({
            'priority': 'high',
            'category': 'regression',
            'title': f"{len(report['regressions'])} Pattern Regressions",
            'description': f"Previously fixed patterns are recurring: "
                          f"{', '.join(r['pattern_name'] for r in report['regressions'][:3])}",
            'action': 'Review fixes for effectiveness, add tests to prevent recurrence'
        })

    # Recurring patterns
    if metrics.get('top_patterns'):
        top_pattern = metrics['top_patterns'][0] if metrics['top_patterns'] else None
        if top_pattern and top_pattern['count'] >= 5:
            recommendations.append({
                'priority': 'medium',
                'category': 'pattern',
                'title': f"Recurring Pattern: {top_pattern['pattern']}",
                'description': f"Pattern '{top_pattern['pattern']}' has {top_pattern['count']} issues this period.",
                'action': 'Investigate root cause and implement systemic fix'
            })

    # Good health - maintenance mode
    if metrics['health_score'] >= 95 and not recommendations:
        recommendations.append({
            'priority': 'low',
            'category': 'maintenance',
            'title': 'System Health Excellent',
            'description': 'No critical issues. Focus on proactive improvements.',
            'action': 'Review user feedback for enhancement opportunities'
        })

    return recommendations


def format_report(report: Dict) -> str:
    """Format report as human-readable text"""
    lines = [
        "=" * 70,
        "  REMYNDRS WEEKLY HEALTH REPORT",
        f"  Period: {report['start_date']} to {report['end_date']}",
        "=" * 70,
        "",
    ]

    # Health Score
    health = report['current_health']
    status_icons = {
        'excellent': '🟢',
        'good': '🟢',
        'fair': '🟡',
        'poor': '🟠',
        'critical': '🔴'
    }
    icon = status_icons.get(health['health_status'], '⚪')

    lines.append(f"  {icon} HEALTH SCORE: {health['health_score']:.0f}/100 ({health['health_status'].upper()})")

    if report.get('health_change'):
        change_icon = '📈' if report['health_change'] > 0 else '📉' if report['health_change'] < 0 else '➡️'
        lines.append(f"     {change_icon} Change from last week: {report['health_change']:+.1f}")

    lines.append("")

    # Key Metrics
    lines.append("  KEY METRICS:")
    lines.append("  " + "-" * 50)
    lines.append(f"    Total Interactions:    {health['total_interactions']:,}")
    lines.append(f"    Issues Detected:       {health['total_issues']} ({health['issue_rate']}% rate)")
    lines.append(f"    False Positives:       {health['false_positives']}")
    lines.append(f"    Issues Resolved:       {health['resolved_issues']} ({health['resolution_rate']}% rate)")
    lines.append(f"    Still Open:            {health['open_issues']}")
    lines.append(f"    Avg Resolution Time:   {health['avg_resolution_hours']:.1f} hours")
    lines.append("")

    # Top Issues
    if health.get('top_issue_types'):
        lines.append("  TOP ISSUE TYPES:")
        lines.append("  " + "-" * 50)
        for t in health['top_issue_types']:
            bar = "█" * min(t['count'], 15)
            lines.append(f"    {t['type']:25} {bar} {t['count']}")
        lines.append("")

    # Regressions
    if report.get('regressions'):
        lines.append("  ⚠️  REGRESSIONS DETECTED:")
        lines.append("  " + "-" * 50)
        for r in report['regressions']:
            lines.append(f"    • {r['pattern_name']}: {r['new_issues_since']} new issues since fix")
        lines.append("")

    # Resolution Breakdown
    if report.get('resolution_breakdown'):
        lines.append("  RESOLUTIONS THIS WEEK:")
        lines.append("  " + "-" * 50)
        for rb in report['resolution_breakdown']:
            icon = RESOLUTION_TYPES.get(rb['type'], {}).get('icon', '•')
            lines.append(f"    {icon} {rb['label']}: {rb['count']}")
        lines.append("")

    # Open Issues
    if report.get('open_issues'):
        lines.append(f"  OPEN ISSUES ({len(report['open_issues'])} shown):")
        lines.append("  " + "-" * 50)
        for issue in report['open_issues'][:5]:
            lines.append(f"    [{issue['severity'].upper():8}] #{issue['id']} - {issue['issue_type']}")
        if len(report['open_issues']) > 5:
            lines.append(f"    ... and {len(report['open_issues']) - 5} more")
        lines.append("")

    # Recommendations
    if report.get('recommendations'):
        lines.append("  📋 RECOMMENDATIONS:")
        lines.append("  " + "-" * 50)
        for rec in report['recommendations']:
            priority_icon = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}.get(rec['priority'], '⚪')
            lines.append(f"    {priority_icon} [{rec['priority'].upper()}] {rec['title']}")
            lines.append(f"       {rec['description'][:70]}")
            lines.append(f"       → Action: {rec['action']}")
            lines.append("")

    lines.append("=" * 70)
    lines.append("  Report generated by Agent 3: Resolution Tracker")
    lines.append("=" * 70)

    return "\n".join(lines)


# ============================================================================
# DASHBOARD
# ============================================================================

def show_dashboard():
    """Display a terminal-based health dashboard"""
    init_tracker_tables()

    print("\n" + "=" * 70)
    print("  REMYNDRS MONITORING DASHBOARD")
    print(f"  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 70)

    # Current health
    metrics = calculate_health_metrics(days=7)

    status_colors = {
        'excellent': '\033[92m',  # Green
        'good': '\033[92m',
        'fair': '\033[93m',       # Yellow
        'poor': '\033[91m',       # Red
        'critical': '\033[91m',
    }
    reset = '\033[0m'
    color = status_colors.get(metrics['health_status'], '')

    print(f"\n  {color}HEALTH: {metrics['health_score']:.0f}/100 ({metrics['health_status'].upper()}){reset}")
    print()

    # Quick stats
    print("  ┌─────────────────┬─────────────────┬─────────────────┐")
    print(f"  │ Issues: {metrics['total_issues']:>6} │ Open: {metrics['open_issues']:>8} │ Rate: {metrics['issue_rate']:>7}% │")
    print("  └─────────────────┴─────────────────┴─────────────────┘")
    print()

    # Open issues by severity
    with get_db_cursor() as cursor:
        cursor.execute('''
            SELECT severity, COUNT(*)
            FROM monitoring_issues
            WHERE validated = TRUE AND false_positive = FALSE AND resolved_at IS NULL
            GROUP BY severity
            ORDER BY
                CASE severity
                    WHEN 'critical' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    ELSE 3
                END
        ''')
        severity_counts = cursor.fetchall()

    if severity_counts:
        print("  OPEN BY SEVERITY:")
        for sev, count in severity_counts:
            icon = {'critical': '🔴', 'high': '🟠', 'medium': '🟡', 'low': '🟢'}.get(sev, '⚪')
            bar = "█" * min(count, 20)
            print(f"    {icon} {sev:10} {bar} {count}")
        print()

    # Recent activity
    print("  RECENT RESOLUTIONS:")
    resolved = get_resolved_issues(days=3, limit=5)
    if resolved:
        for r in resolved:
            icon = RESOLUTION_TYPES.get(r['resolution_type'], {}).get('icon', '•')
            print(f"    {icon} #{r['id']} {r['issue_type'][:20]} → {r['resolution_type'] or 'pending'}")
    else:
        print("    (none in last 3 days)")
    print()

    # Trend (last 7 days)
    trend = get_health_trend(days=7)
    if len(trend) >= 2:
        print("  7-DAY TREND:")
        scores = [t['health_score'] for t in trend if t['health_score']]
        if scores:
            min_score, max_score = min(scores), max(scores)
            for t in trend[-7:]:
                if t['health_score']:
                    bar_len = int((t['health_score'] - min_score + 1) / (max_score - min_score + 1) * 20)
                    bar = "▓" * bar_len
                    print(f"    {t['date'][-5:]} {bar:20} {t['health_score']:.0f}")
        print()

    # Quick actions
    print("  QUICK ACTIONS:")
    print("    python -m agents.resolution_tracker --resolve <id>   Resolve an issue")
    print("    python -m agents.resolution_tracker --report weekly  Full report")
    print("    python -m agents.resolution_tracker --trends         Trend analysis")
    print()


# ============================================================================
# CLI INTERFACE
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Agent 3: Resolution Tracker - Track fixes and measure health'
    )
    parser.add_argument(
        '--resolve', type=int, metavar='ID',
        help='Resolve issue by ID'
    )
    parser.add_argument(
        '--type', type=str, choices=list(RESOLUTION_TYPES.keys()),
        help='Resolution type (use with --resolve)'
    )
    parser.add_argument(
        '--note', type=str,
        help='Resolution description (use with --resolve)'
    )
    parser.add_argument(
        '--commit', type=str,
        help='Commit reference (use with --resolve)'
    )
    parser.add_argument(
        '--report', type=str, choices=['weekly', 'snapshot'],
        help='Generate report'
    )
    parser.add_argument(
        '--trends', action='store_true',
        help='Show trend analysis'
    )
    parser.add_argument(
        '--open', action='store_true',
        help='List open issues'
    )
    parser.add_argument(
        '--json', action='store_true',
        help='Output as JSON'
    )

    args = parser.parse_args()

    init_tracker_tables()

    # Resolve an issue
    if args.resolve:
        if not args.type:
            print("\nResolution types:")
            for key, info in RESOLUTION_TYPES.items():
                print(f"  {info['icon']} {key:20} - {info['description']}")
            print("\nUsage: --resolve <id> --type <type> [--note 'description'] [--commit 'abc123']")
            return

        success = resolve_issue(
            args.resolve,
            args.type,
            description=args.note,
            commit_ref=args.commit
        )
        if success:
            print(f"✅ Issue #{args.resolve} resolved as {args.type}")
        else:
            print(f"❌ Failed to resolve issue #{args.resolve}")
        return

    # Generate report
    if args.report == 'weekly':
        report = generate_weekly_report()
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print(format_report(report))
        return

    if args.report == 'snapshot':
        metrics = calculate_health_metrics(days=1)
        save_health_snapshot(metrics)
        print(f"✅ Health snapshot saved: score={metrics['health_score']:.0f}")
        return

    # Show trends
    if args.trends:
        trend = get_health_trend(days=30)
        if args.json:
            print(json.dumps(trend, indent=2))
        else:
            print("\n30-DAY HEALTH TREND:")
            print("-" * 60)
            for t in trend:
                score = t['health_score'] or 0
                bar = "█" * int(score / 5)
                print(f"  {t['date']} │ {bar:20} │ {score:.0f} │ {t['issues'] or 0} issues")
        return

    # List open issues
    if args.open:
        issues = get_open_issues(limit=20)
        if args.json:
            print(json.dumps(issues, indent=2, default=str))
        else:
            print(f"\nOPEN ISSUES ({len(issues)}):")
            print("-" * 60)
            for i in issues:
                print(f"  [{i['severity']:8}] #{i['id']:4} {i['issue_type']:20} (...{i['phone_number'][-4:]})")
        return

    # Default: show dashboard
    show_dashboard()


if __name__ == '__main__':
    main()
