"""
Agent 2: Issue Validator
Validates issues detected by Agent 1, analyzes context, identifies patterns,
and categorizes for resolution.

Designed for multi-agent pipeline:
- Reads pending issues from monitoring_issues table
- Uses AI (optional) to analyze context and validate
- Groups similar issues into patterns
- Marks false positives
- Prepares actionable insights for Agent 3

Usage:
    python -m agents.issue_validator                    # Validate pending issues
    python -m agents.issue_validator --no-ai           # Rule-based only (no AI)
    python -m agents.issue_validator --batch 10        # Process 10 issues
    python -m agents.issue_validator --patterns        # Show pattern analysis
"""

import re
import sys
import json
import argparse
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from collections import defaultdict

# Add parent directory to path for imports
sys.path.insert(0, '.')

from database import get_monitoring_cursor, logger
from config import ENVIRONMENT, OPENAI_API_KEY


# ============================================================================
# VALIDATION RULES (Rule-based, no AI needed)
# ============================================================================

# Known false positive patterns
FALSE_POSITIVE_PATTERNS = {
    'user_confusion': [
        # "what" at start of legitimate questions
        r'^what time',
        r'^what.*remind',
        r'^what.*list',
        r'^what do i have',
        # "help" as a command, not confusion
        r'^help$',
        r'^help me',
    ],
    'parsing_failure': [
        # Legitimate clarification flows (not failures)
        r'which list',
        r'what time would you like',
        r'for today or tomorrow',
    ],
    'error_response': [
        # Intentional error messages (not system failures)
        r'you.ve reached your.*limit',
        r'upgrade to premium',
        r'already have a list',
    ],
    'action_not_found': [
        # Legitimate "not found" cases (user explicitly referencing something that doesn't exist)
        r'^(delete|remove)\b.*\breminder\b',  # User deleting a reminder that doesn't exist is expected
        r'^(show|view|check)\b.*\blist\b',    # User checking a list that doesn't exist is expected
    ],
}

# Issue patterns that indicate the same root cause
ROOT_CAUSE_SIGNATURES = {
    'timezone_confusion': {
        'indicators': ['timezone', 'wrong time', 'am', 'pm', 'supposed to be'],
        'description': 'Users experiencing timezone-related confusion'
    },
    'reminder_format_unclear': {
        'indicators': ['remind me', 'what time', 'when', 'clarify'],
        'description': 'Users unsure how to format reminder requests'
    },
    'list_management_confusion': {
        'indicators': ['list', 'add to', 'which list', 'create list'],
        'description': 'Users confused about list management'
    },
    'feature_discovery': {
        'indicators': ['how do i', 'can you', 'is there a way', 'what can'],
        'description': 'Users trying to discover features'
    },
    'delivery_reliability': {
        'indicators': ['didn\'t get', 'never received', 'missed', 'not sent'],
        'description': 'Users reporting missed reminder deliveries'
    },
    'intent_misclassification': {
        'indicators': ['no pending reminders found', 'no reminders found matching',
                       'no memories found matching', 'couldn\'t find a list',
                       'change', 'update', 'modify', 'settings', 'summary time'],
        'description': 'System attempted wrong action type for user request (e.g., searched reminders when user wanted settings change)'
    },
}

# Severity adjustments based on context
SEVERITY_ADJUSTMENTS = {
    # Upgrade severity for these patterns
    'upgrade': [
        ('repeated_attempts', 'critical'),  # Repeated failures are critical
        ('delivery_failure', 'critical'),   # Delivery issues always critical
    ],
    # Downgrade severity for these patterns
    'downgrade': [
        ('user_confusion', 'low', r'^(help|what can)'),  # Feature discovery is low
        ('parsing_failure', 'low', r'which (list|time)'),  # Clarification is expected
    ],
}


# ============================================================================
# DATABASE OPERATIONS
# ============================================================================

def init_validator_tables():
    """Create validator-specific tables if they don't exist"""
    with get_monitoring_cursor() as cursor:
        # Issue patterns table - groups related issues
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS issue_patterns (
                id SERIAL PRIMARY KEY,
                pattern_name TEXT NOT NULL,
                description TEXT,
                issue_count INTEGER DEFAULT 0,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'active',
                root_cause TEXT,
                suggested_fix TEXT,
                priority TEXT DEFAULT 'medium'
            )
        ''')

        # Link issues to patterns
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS issue_pattern_links (
                id SERIAL PRIMARY KEY,
                issue_id INTEGER REFERENCES monitoring_issues(id),
                pattern_id INTEGER REFERENCES issue_patterns(id),
                confidence FLOAT DEFAULT 1.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Validation runs audit
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS validation_runs (
                id SERIAL PRIMARY KEY,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                issues_processed INTEGER DEFAULT 0,
                validated_count INTEGER DEFAULT 0,
                false_positive_count INTEGER DEFAULT 0,
                patterns_found INTEGER DEFAULT 0,
                ai_used BOOLEAN DEFAULT FALSE,
                status TEXT DEFAULT 'running'
            )
        ''')

        # Indexes
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_issue_patterns_status
            ON issue_patterns(status) WHERE status = 'active'
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_issue_pattern_links_issue
            ON issue_pattern_links(issue_id)
        ''')

        logger.info("Validator tables initialized")


def get_pending_issues(limit: int = 50) -> List[Dict]:
    """Get issues pending validation from Agent 1"""
    with get_monitoring_cursor() as cursor:
        cursor.execute('''
            SELECT mi.id, mi.log_id, mi.phone_number, mi.issue_type,
                   mi.severity, mi.details, mi.detected_at,
                   l.message_in, l.message_out, l.intent,
                   u.timezone, u.premium_status
            FROM monitoring_issues mi
            LEFT JOIN logs l ON mi.log_id = l.id
            LEFT JOIN users u ON mi.phone_number = u.phone_number
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
                   'details', 'detected_at', 'message_in', 'message_out',
                   'intent', 'timezone', 'premium_status']
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def mark_issue_validated(issue_id: int, false_positive: bool = False,
                         adjusted_severity: str = None, notes: str = None):
    """Mark an issue as validated"""
    with get_monitoring_cursor() as cursor:
        if adjusted_severity:
            cursor.execute('''
                UPDATE monitoring_issues
                SET validated = TRUE,
                    validated_by = 'agent2',
                    validated_at = NOW(),
                    false_positive = %s,
                    severity = %s,
                    resolution = %s
                WHERE id = %s
            ''', (false_positive, adjusted_severity, notes, issue_id))
        else:
            cursor.execute('''
                UPDATE monitoring_issues
                SET validated = TRUE,
                    validated_by = 'agent2',
                    validated_at = NOW(),
                    false_positive = %s,
                    resolution = %s
                WHERE id = %s
            ''', (false_positive, notes, issue_id))


def get_or_create_pattern(pattern_name: str, description: str = None) -> int:
    """Get or create an issue pattern, returns pattern_id"""
    with get_monitoring_cursor() as cursor:
        # Try to find existing
        cursor.execute('''
            SELECT id FROM issue_patterns WHERE pattern_name = %s
        ''', (pattern_name,))
        result = cursor.fetchone()

        if result:
            # Update last_seen and count
            cursor.execute('''
                UPDATE issue_patterns
                SET last_seen = NOW(), issue_count = issue_count + 1
                WHERE id = %s
            ''', (result[0],))
            return result[0]
        else:
            # Create new
            cursor.execute('''
                INSERT INTO issue_patterns (pattern_name, description, issue_count)
                VALUES (%s, %s, 1)
                RETURNING id
            ''', (pattern_name, description))
            return cursor.fetchone()[0]


def link_issue_to_pattern(issue_id: int, pattern_id: int, confidence: float = 1.0):
    """Link an issue to a pattern"""
    with get_monitoring_cursor() as cursor:
        cursor.execute('''
            INSERT INTO issue_pattern_links (issue_id, pattern_id, confidence)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
        ''', (issue_id, pattern_id, confidence))


# ============================================================================
# VALIDATION LOGIC
# ============================================================================

def check_false_positive(issue: Dict) -> tuple:
    """
    Check if an issue is a false positive using rule-based patterns.
    Returns (is_false_positive, reason)
    """
    issue_type = issue['issue_type']
    message_in = (issue.get('message_in') or '').lower()
    message_out = (issue.get('message_out') or '').lower()
    details = issue.get('details') or {}

    # Check type-specific false positive patterns
    if issue_type in FALSE_POSITIVE_PATTERNS:
        for pattern in FALSE_POSITIVE_PATTERNS[issue_type]:
            if re.search(pattern, message_in, re.IGNORECASE):
                return True, f"Matched false positive pattern: {pattern}"
            if re.search(pattern, message_out, re.IGNORECASE):
                return True, f"Response matched expected pattern: {pattern}"

    # Check for user immediately succeeding after "confusion"
    # (e.g., they said "what?" then immediately got the right response)
    if issue_type == 'user_confusion' and issue.get('log_id'):
        # This would require looking at the next interaction - skip for now
        pass

    return False, None


def adjust_severity(issue: Dict) -> tuple:
    """
    Adjust issue severity based on context.
    Returns (new_severity, reason) or (None, None) if no change
    """
    issue_type = issue['issue_type']
    current_severity = issue['severity']
    message_in = (issue.get('message_in') or '').lower()

    # Check upgrade rules
    for check_type, new_severity in SEVERITY_ADJUSTMENTS['upgrade']:
        if issue_type == check_type and current_severity != new_severity:
            return new_severity, f"Upgraded: {issue_type} issues are always {new_severity}"

    # Check downgrade rules
    for check_type, new_severity, pattern in SEVERITY_ADJUSTMENTS['downgrade']:
        if issue_type == check_type:
            if re.search(pattern, message_in, re.IGNORECASE):
                return new_severity, f"Downgraded: matches low-priority pattern"

    # Premium user issues are higher priority
    if issue.get('premium_status') == 'premium' and current_severity == 'medium':
        return 'high', "Upgraded: premium user impact"

    return None, None


def identify_pattern(issue: Dict) -> tuple:
    """
    Identify which root cause pattern this issue belongs to.
    Returns (pattern_name, confidence) or (None, 0)
    """
    message_in = (issue.get('message_in') or '').lower()
    message_out = (issue.get('message_out') or '').lower()
    combined = f"{message_in} {message_out}"

    best_match = None
    best_score = 0

    for pattern_name, pattern_info in ROOT_CAUSE_SIGNATURES.items():
        indicators = pattern_info['indicators']
        score = sum(1 for ind in indicators if ind in combined)

        if score > best_score:
            best_score = score
            best_match = pattern_name

    if best_match and best_score >= 2:
        confidence = min(best_score / len(ROOT_CAUSE_SIGNATURES[best_match]['indicators']), 1.0)
        return best_match, confidence

    return None, 0


def validate_with_ai(issues: List[Dict]) -> Dict:
    """
    Use AI to validate and analyze issues in batch.
    Returns dict mapping issue_id to validation result.
    """
    if not OPENAI_API_KEY:
        logger.warning("No OpenAI API key configured, skipping AI validation")
        return {}

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
    except ImportError:
        logger.warning("OpenAI not installed, skipping AI validation")
        return {}

    # Prepare batch prompt
    issues_text = []
    for i, issue in enumerate(issues[:10]):  # Limit to 10 per batch
        issues_text.append(f"""
Issue #{issue['id']}:
- Type: {issue['issue_type']}
- Severity: {issue['severity']}
- User said: "{issue.get('message_in', 'N/A')[:150]}"
- We responded: "{issue.get('message_out', 'N/A')[:150]}"
- Intent detected: {issue.get('intent', 'N/A')}
""")

    prompt = f"""Analyze these detected issues from an SMS reminder service.
For each issue, determine:
1. Is it a FALSE POSITIVE? (user was actually fine, system worked correctly)
2. What's the ROOT CAUSE? (e.g., unclear UX, missing feature, bug, user error)
3. Suggested SEVERITY (critical/high/medium/low)
4. Brief ACTION to fix it

Issues:
{"".join(issues_text)}

Respond in JSON format:
{{
  "issues": [
    {{
      "id": <issue_id>,
      "false_positive": true/false,
      "root_cause": "string",
      "adjusted_severity": "critical/high/medium/low",
      "action": "brief suggestion",
      "pattern": "pattern_name or null"
    }}
  ]
}}
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an expert at analyzing user interaction issues in software. Be concise and practical."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=1000
        )

        content = response.choices[0].message.content

        # Parse JSON from response
        # Handle markdown code blocks
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        result = json.loads(content)

        # Convert to dict keyed by issue ID
        return {item['id']: item for item in result.get('issues', [])}

    except Exception as e:
        logger.error(f"AI validation failed: {e}")
        return {}


# ============================================================================
# PATTERN ANALYSIS
# ============================================================================

def analyze_patterns() -> Dict:
    """Analyze issue patterns and generate insights"""
    with get_monitoring_cursor() as cursor:
        # Get active patterns with counts
        cursor.execute('''
            SELECT ip.id, ip.pattern_name, ip.description, ip.issue_count,
                   ip.first_seen, ip.last_seen, ip.priority, ip.root_cause,
                   ip.suggested_fix
            FROM issue_patterns ip
            WHERE ip.status = 'active'
            ORDER BY ip.issue_count DESC
            LIMIT 20
        ''')

        patterns = []
        for row in cursor.fetchall():
            patterns.append({
                'id': row[0],
                'name': row[1],
                'description': row[2],
                'count': row[3],
                'first_seen': row[4].isoformat() if row[4] else None,
                'last_seen': row[5].isoformat() if row[5] else None,
                'priority': row[6],
                'root_cause': row[7],
                'suggested_fix': row[8]
            })

        # Get issue type distribution
        cursor.execute('''
            SELECT issue_type, COUNT(*),
                   SUM(CASE WHEN false_positive THEN 1 ELSE 0 END) as fp_count
            FROM monitoring_issues
            WHERE detected_at > NOW() - INTERVAL '7 days'
            GROUP BY issue_type
            ORDER BY COUNT(*) DESC
        ''')

        type_distribution = []
        for row in cursor.fetchall():
            type_distribution.append({
                'type': row[0],
                'total': row[1],
                'false_positives': row[2],
                'accuracy': round((row[1] - row[2]) / row[1] * 100, 1) if row[1] > 0 else 0
            })

        # Get hourly distribution (when do issues occur?)
        cursor.execute('''
            SELECT EXTRACT(HOUR FROM detected_at) as hour, COUNT(*)
            FROM monitoring_issues
            WHERE detected_at > NOW() - INTERVAL '7 days'
            GROUP BY EXTRACT(HOUR FROM detected_at)
            ORDER BY hour
        ''')

        hourly = {int(row[0]): row[1] for row in cursor.fetchall()}

        # Users with most issues
        cursor.execute('''
            SELECT phone_number, COUNT(*) as issue_count
            FROM monitoring_issues
            WHERE detected_at > NOW() - INTERVAL '7 days'
            AND false_positive = FALSE
            GROUP BY phone_number
            ORDER BY COUNT(*) DESC
            LIMIT 5
        ''')

        top_users = [
            {'phone': f"...{row[0][-4:]}" if row[0] else "N/A", 'count': row[1]}
            for row in cursor.fetchall()
        ]

        return {
            'patterns': patterns,
            'type_distribution': type_distribution,
            'hourly_distribution': hourly,
            'top_affected_users': top_users
        }


# ============================================================================
# MAIN VALIDATION ENGINE
# ============================================================================

def validate_issues(limit: int = 50, use_ai: bool = True, dry_run: bool = False) -> Dict:
    """
    Main validation function. Processes pending issues.

    Args:
        limit: Max issues to process
        use_ai: Whether to use AI for validation
        dry_run: If True, don't write to database

    Returns:
        dict with validation results
    """
    init_validator_tables()

    # Start validation run
    run_id = None
    if not dry_run:
        with get_monitoring_cursor() as cursor:
            cursor.execute('''
                INSERT INTO validation_runs (ai_used)
                VALUES (%s)
                RETURNING id
            ''', (use_ai,))
            run_id = cursor.fetchone()[0]

    results = {
        'run_id': run_id,
        'started_at': datetime.utcnow().isoformat(),
        'issues_processed': 0,
        'validated': [],
        'false_positives': [],
        'patterns_found': defaultdict(int),
        'severity_adjustments': []
    }

    try:
        # Get pending issues
        issues = get_pending_issues(limit)
        results['issues_processed'] = len(issues)

        if not issues:
            logger.info("No pending issues to validate")
            return results

        # AI validation (batch)
        ai_results = {}
        if use_ai and OPENAI_API_KEY:
            ai_results = validate_with_ai(issues)

        # Process each issue
        for issue in issues:
            issue_id = issue['id']
            validation_notes = []

            # 1. Check for false positive (rule-based)
            is_fp, fp_reason = check_false_positive(issue)

            # 2. Check AI result if available
            ai_result = ai_results.get(issue_id, {})
            if ai_result.get('false_positive'):
                is_fp = True
                fp_reason = ai_result.get('root_cause', 'AI detected false positive')

            # 3. Adjust severity
            new_severity, severity_reason = adjust_severity(issue)
            if ai_result.get('adjusted_severity'):
                new_severity = ai_result['adjusted_severity']
                severity_reason = ai_result.get('action', 'AI recommendation')

            # 4. Identify pattern
            pattern_name, confidence = identify_pattern(issue)
            if ai_result.get('pattern'):
                pattern_name = ai_result['pattern']
                confidence = 0.9

            # Build notes
            if fp_reason:
                validation_notes.append(f"FP: {fp_reason}")
            if severity_reason:
                validation_notes.append(f"Severity: {severity_reason}")
            if ai_result.get('action'):
                validation_notes.append(f"Action: {ai_result['action']}")

            notes = " | ".join(validation_notes) if validation_notes else None

            # Update database
            if not dry_run:
                mark_issue_validated(
                    issue_id,
                    false_positive=is_fp,
                    adjusted_severity=new_severity,
                    notes=notes
                )

                # Link to pattern if found
                if pattern_name:
                    desc = ROOT_CAUSE_SIGNATURES.get(pattern_name, {}).get('description')
                    pattern_id = get_or_create_pattern(pattern_name, desc)
                    link_issue_to_pattern(issue_id, pattern_id, confidence)
                    results['patterns_found'][pattern_name] += 1

            # Track results
            if is_fp:
                results['false_positives'].append({
                    'id': issue_id,
                    'type': issue['issue_type'],
                    'reason': fp_reason
                })
            else:
                results['validated'].append({
                    'id': issue_id,
                    'type': issue['issue_type'],
                    'severity': new_severity or issue['severity'],
                    'pattern': pattern_name
                })

            if new_severity and new_severity != issue['severity']:
                results['severity_adjustments'].append({
                    'id': issue_id,
                    'from': issue['severity'],
                    'to': new_severity
                })

        # Complete validation run
        if not dry_run and run_id:
            with get_monitoring_cursor() as cursor:
                cursor.execute('''
                    UPDATE validation_runs
                    SET completed_at = NOW(),
                        issues_processed = %s,
                        validated_count = %s,
                        false_positive_count = %s,
                        patterns_found = %s,
                        status = 'completed'
                    WHERE id = %s
                ''', (
                    results['issues_processed'],
                    len(results['validated']),
                    len(results['false_positives']),
                    len(results['patterns_found']),
                    run_id
                ))

        results['completed_at'] = datetime.utcnow().isoformat()
        results['patterns_found'] = dict(results['patterns_found'])

    except Exception as e:
        logger.error(f"Validation failed: {e}", exc_info=True)
        results['error'] = str(e)

        if not dry_run and run_id:
            with get_monitoring_cursor() as cursor:
                cursor.execute('''
                    UPDATE validation_runs SET status = 'failed' WHERE id = %s
                ''', (run_id,))

    return results


def generate_report(results: Dict, patterns: Dict = None) -> str:
    """Generate a human-readable validation report"""
    lines = [
        "=" * 60,
        "ISSUE VALIDATOR REPORT",
        f"Agent 2 - {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 60,
        "",
        f"Issues Processed: {results['issues_processed']}",
        f"Validated (real issues): {len(results['validated'])}",
        f"False Positives: {len(results['false_positives'])}",
        f"Accuracy Rate: {len(results['validated']) / max(results['issues_processed'], 1) * 100:.1f}%",
        "",
    ]

    if results.get('error'):
        lines.append(f"ERROR: {results['error']}")
        lines.append("")

    # Severity adjustments
    if results['severity_adjustments']:
        lines.append("SEVERITY ADJUSTMENTS:")
        lines.append("-" * 40)
        for adj in results['severity_adjustments']:
            lines.append(f"  Issue #{adj['id']}: {adj['from']} → {adj['to']}")
        lines.append("")

    # Patterns found
    if results['patterns_found']:
        lines.append("PATTERNS IDENTIFIED:")
        lines.append("-" * 40)
        for pattern, count in sorted(results['patterns_found'].items(), key=lambda x: -x[1]):
            desc = ROOT_CAUSE_SIGNATURES.get(pattern, {}).get('description', '')
            lines.append(f"  {pattern}: {count} issues")
            if desc:
                lines.append(f"    └─ {desc}")
        lines.append("")

    # False positives
    if results['false_positives']:
        lines.append(f"FALSE POSITIVES ({len(results['false_positives'])}):")
        lines.append("-" * 40)
        for fp in results['false_positives'][:5]:  # Limit display
            lines.append(f"  #{fp['id']} [{fp['type']}]: {fp['reason'][:60]}")
        if len(results['false_positives']) > 5:
            lines.append(f"  ... and {len(results['false_positives']) - 5} more")
        lines.append("")

    # Pattern analysis
    if patterns:
        lines.append("PATTERN ANALYSIS (Last 7 Days):")
        lines.append("-" * 40)

        if patterns.get('type_distribution'):
            lines.append("\nIssue Type Accuracy:")
            for td in patterns['type_distribution'][:5]:
                lines.append(f"  {td['type']}: {td['total']} total, {td['accuracy']}% accuracy")

        if patterns.get('top_affected_users'):
            lines.append("\nMost Affected Users:")
            for u in patterns['top_affected_users']:
                lines.append(f"  {u['phone']}: {u['count']} issues")

        lines.append("")

    lines.append("=" * 60)
    lines.append("END OF REPORT")
    lines.append("=" * 60)

    return "\n".join(lines)


# ============================================================================
# CLI INTERFACE
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Agent 2: Issue Validator - Validate and categorize detected issues'
    )
    parser.add_argument(
        '--batch', type=int, default=50,
        help='Number of issues to process (default: 50)'
    )
    parser.add_argument(
        '--no-ai', action='store_true',
        help='Disable AI validation (rule-based only)'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Analyze only, no database writes'
    )
    parser.add_argument(
        '--json', action='store_true',
        help='Output results as JSON'
    )
    parser.add_argument(
        '--patterns', action='store_true',
        help='Show pattern analysis'
    )

    args = parser.parse_args()

    if args.patterns:
        print("\n📊 Analyzing patterns...")
        patterns = analyze_patterns()

        if args.json:
            print(json.dumps(patterns, indent=2, default=str))
        else:
            print("\n" + "=" * 60)
            print("PATTERN ANALYSIS")
            print("=" * 60)

            if patterns['patterns']:
                print("\nTop Patterns:")
                for p in patterns['patterns'][:10]:
                    print(f"  [{p['priority'].upper()}] {p['name']}: {p['count']} issues")
                    if p['description']:
                        print(f"       └─ {p['description']}")

            if patterns['type_distribution']:
                print("\nIssue Type Distribution (7 days):")
                for td in patterns['type_distribution']:
                    bar = "█" * min(td['total'] // 2, 20)
                    print(f"  {td['type']:25} {bar} {td['total']} ({td['accuracy']}% accuracy)")

            if patterns['top_affected_users']:
                print("\nTop Affected Users:")
                for u in patterns['top_affected_users']:
                    print(f"  {u['phone']}: {u['count']} issues")

        return

    print(f"\n🔍 Running Issue Validator...")
    print(f"   Environment: {ENVIRONMENT}")
    print(f"   Batch size: {args.batch}")
    print(f"   AI enabled: {not args.no_ai}")
    print(f"   Dry run: {args.dry_run}")
    print()

    results = validate_issues(
        limit=args.batch,
        use_ai=not args.no_ai,
        dry_run=args.dry_run
    )

    patterns = analyze_patterns() if not args.json else None

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        report = generate_report(results, patterns)
        print(report)


if __name__ == '__main__':
    main()
