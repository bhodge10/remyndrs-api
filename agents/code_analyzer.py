"""
Agent 4: Code Analyzer
Analyzes issues detected by Agents 1 & 2, identifies code root causes,
and generates Claude Code prompts for fixes.

Designed for multi-agent pipeline:
- Reads validated issues from monitoring_issues table
- Maps issue types to likely code locations
- Uses AI (optional) to analyze root causes
- Generates actionable Claude Code prompts
- Stores analyses for dashboard display

Usage:
    python -m agents.code_analyzer                     # Analyze recent unanalyzed issues
    python -m agents.code_analyzer --issue 123         # Analyze specific issue
    python -m agents.code_analyzer --pattern 456       # Analyze specific pattern
    python -m agents.code_analyzer --report            # Dry run (no DB writes)
    python -m agents.code_analyzer --no-ai             # Rule-based only (no AI)
"""

import sys
import json
import argparse
from datetime import datetime, timedelta
from typing import Optional, List, Dict

# Add parent directory to path for imports
sys.path.insert(0, '.')

from database import get_monitoring_cursor, logger
from config import ENVIRONMENT, OPENAI_API_KEY


# ============================================================================
# ISSUE-TO-CODE MAPPINGS
# ============================================================================

# Maps issue types to likely code locations for investigation
ISSUE_CODE_MAPPINGS = {
    'parsing_failure': {
        'files': ['services/ai_service.py'],
        'description': 'AI prompt may be missing examples or edge cases',
        'areas': ['system prompt', 'user intent parsing', 'time parsing'],
        'lines_hint': 'Search for SYSTEM_PROMPT or process_message'
    },
    'timezone_issue': {
        'files': ['utils/timezone.py', 'models/reminder.py', 'tasks/reminder_tasks.py'],
        'description': 'Timezone conversion or storage issue',
        'areas': ['timezone conversion', 'reminder scheduling', 'user timezone lookup'],
        'lines_hint': 'Search for pytz, timezone, or convert_to_utc'
    },
    'delivery_failure': {
        'files': ['tasks/reminder_tasks.py', 'services/sms_service.py'],
        'description': 'SMS delivery or reminder processing failure',
        'areas': ['Twilio integration', 'reminder claiming', 'delivery status'],
        'lines_hint': 'Search for send_sms, delivery_status, or claimed_at'
    },
    'error_response': {
        'files': ['main.py', 'services/ai_service.py', 'routes/handlers/'],
        'description': 'Unexpected error in request handling',
        'areas': ['exception handling', 'error messages', 'route handlers'],
        'lines_hint': 'Search for try/except blocks or error responses'
    },
    'user_confusion': {
        'files': ['services/ai_service.py'],
        'description': 'AI response may be unclear or missing guidance',
        'areas': ['response generation', 'help messages', 'clarification prompts'],
        'lines_hint': 'Search for response templates or clarification logic'
    },
    'failed_action': {
        'files': ['routes/handlers/', 'models/'],
        'description': 'Action failed during execution',
        'areas': ['database operations', 'validation logic', 'business rules'],
        'lines_hint': 'Check the intent field to find the specific handler'
    },
    'confidence_rejection': {
        'files': ['services/ai_service.py', 'config.py'],
        'description': 'User rejected AI interpretation - confidence calibration needed',
        'areas': ['confidence thresholds', 'confirmation prompts'],
        'lines_hint': 'Search for CONFIDENCE_THRESHOLD or confirmation logic'
    },
    'repeated_attempts': {
        'files': ['services/ai_service.py', 'routes/handlers/'],
        'description': 'User making repeated similar attempts indicates UX issue',
        'areas': ['input validation', 'error messages', 'response clarity'],
        'lines_hint': 'Review the specific action user was attempting'
    },
}

# Default mapping for unknown issue types
DEFAULT_CODE_MAPPING = {
    'files': ['main.py', 'services/ai_service.py'],
    'description': 'Unknown issue type - general investigation needed',
    'areas': ['request handling', 'AI processing'],
    'lines_hint': 'Start with main.py webhook handler'
}


# ============================================================================
# DATABASE OPERATIONS
# ============================================================================

def init_analyzer_tables():
    """Create code analyzer tables if they don't exist"""
    with get_monitoring_cursor() as cursor:
        # Code analysis results table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS code_analysis (
                id SERIAL PRIMARY KEY,
                issue_id INTEGER REFERENCES monitoring_issues(id),
                pattern_id INTEGER,
                root_cause_summary TEXT NOT NULL,
                root_cause_details TEXT,
                likely_files JSONB,
                claude_prompt TEXT NOT NULL,
                confidence_score INTEGER DEFAULT 50,
                analysis_model TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending',
                applied_at TIMESTAMP,
                applied_by TEXT
            )
        ''')

        # Code analysis runs audit table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS code_analysis_runs (
                id SERIAL PRIMARY KEY,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                issues_analyzed INTEGER DEFAULT 0,
                analyses_generated INTEGER DEFAULT 0,
                use_ai BOOLEAN DEFAULT TRUE,
                status TEXT DEFAULT 'running'
            )
        ''')

        # Indexes
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_code_analysis_issue
            ON code_analysis(issue_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_code_analysis_pattern
            ON code_analysis(pattern_id) WHERE pattern_id IS NOT NULL
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_code_analysis_status
            ON code_analysis(status)
        ''')

        # Unique constraint to prevent duplicate analyses
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_code_analysis_unique_issue
            ON code_analysis(issue_id) WHERE issue_id IS NOT NULL
        ''')

        logger.info("Code analyzer tables initialized")


def get_conversation_context(phone_number: str, log_id: int, context_size: int = 10) -> List[Dict]:
    """
    Get conversation context - messages leading up to and including the issue.

    Args:
        phone_number: User's phone number
        log_id: The log ID of the issue (to find messages up to this point)
        context_size: Number of messages to retrieve (default 10)

    Returns:
        List of messages in chronological order (oldest first)
    """
    from database import get_db_connection, return_db_connection, get_monitoring_connection, return_monitoring_connection

    messages = []

    # Try monitoring database first (used in staging where logs may be there)
    try:
        conn = get_monitoring_connection()
        cursor = conn.cursor()

        # Get messages from the same phone number up to and including the issue log
        cursor.execute('''
            SELECT id, message_in, message_out, intent, created_at
            FROM logs
            WHERE phone_number = %s
              AND id <= %s
            ORDER BY created_at DESC
            LIMIT %s
        ''', (phone_number, log_id, context_size))

        rows = cursor.fetchall()
        return_monitoring_connection(conn)

        if rows:
            # Reverse to get chronological order (oldest first)
            for row in reversed(rows):
                messages.append({
                    'log_id': row[0],
                    'user': row[1],
                    'bot': row[2],
                    'intent': row[3],
                    'timestamp': row[4].isoformat() if row[4] else None,
                    'is_issue': row[0] == log_id
                })
            return messages

    except Exception as e:
        logger.warning(f"Could not get conversation from monitoring DB: {e}")

    # Fall back to main database
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, message_in, message_out, intent, created_at
            FROM logs
            WHERE phone_number = %s
              AND id <= %s
            ORDER BY created_at DESC
            LIMIT %s
        ''', (phone_number, log_id, context_size))

        rows = cursor.fetchall()

        # Reverse to get chronological order (oldest first)
        for row in reversed(rows):
            messages.append({
                'log_id': row[0],
                'user': row[1],
                'bot': row[2],
                'intent': row[3],
                'timestamp': row[4].isoformat() if row[4] else None,
                'is_issue': row[0] == log_id
            })

        return messages

    except Exception as e:
        logger.error(f"Error getting conversation context: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def get_issue_details(issue_id: int) -> Optional[Dict]:
    """Get full issue details for analysis including conversation context"""
    with get_monitoring_cursor() as cursor:
        cursor.execute('''
            SELECT mi.id, mi.log_id, mi.phone_number, mi.issue_type,
                   mi.severity, mi.details, mi.detected_at, mi.validated,
                   mi.resolution, mi.false_positive,
                   l.message_in, l.message_out, l.intent
            FROM monitoring_issues mi
            LEFT JOIN logs l ON mi.log_id = l.id
            WHERE mi.id = %s
        ''', (issue_id,))

        row = cursor.fetchone()
        if not row:
            return None

        issue = {
            'id': row[0],
            'log_id': row[1],
            'phone_number': row[2],
            'issue_type': row[3],
            'severity': row[4],
            'details': row[5],
            'detected_at': row[6],
            'validated': row[7],
            'resolution': row[8],
            'false_positive': row[9],
            'message_in': row[10],
            'message_out': row[11],
            'intent': row[12],
            'conversation': []
        }

        # Get conversation context if we have phone number and log_id
        if issue['phone_number'] and issue['log_id']:
            issue['conversation'] = get_conversation_context(
                issue['phone_number'],
                issue['log_id'],
                context_size=10
            )

        return issue


def get_pattern_details(pattern_id: int) -> Optional[Dict]:
    """Get pattern details and related issues for analysis"""
    with get_monitoring_cursor() as cursor:
        cursor.execute('''
            SELECT ip.id, ip.pattern_name, ip.description, ip.issue_count,
                   ip.first_seen, ip.last_seen, ip.status, ip.root_cause,
                   ip.priority
            FROM issue_patterns ip
            WHERE ip.id = %s
        ''', (pattern_id,))

        row = cursor.fetchone()
        if not row:
            return None

        pattern = {
            'id': row[0],
            'pattern_name': row[1],
            'description': row[2],
            'issue_count': row[3],
            'first_seen': row[4],
            'last_seen': row[5],
            'status': row[6],
            'root_cause': row[7],
            'priority': row[8],
            'sample_issues': []
        }

        # Get sample issues for this pattern
        cursor.execute('''
            SELECT mi.id, mi.issue_type, mi.severity,
                   l.message_in, l.message_out
            FROM issue_pattern_links ipl
            JOIN monitoring_issues mi ON ipl.issue_id = mi.id
            LEFT JOIN logs l ON mi.log_id = l.id
            WHERE ipl.pattern_id = %s
            ORDER BY mi.detected_at DESC
            LIMIT 5
        ''', (pattern_id,))

        for r in cursor.fetchall():
            pattern['sample_issues'].append({
                'id': r[0],
                'issue_type': r[1],
                'severity': r[2],
                'message_in': r[3],
                'message_out': r[4]
            })

        return pattern


def get_existing_analysis(issue_id: int = None, pattern_id: int = None) -> Optional[Dict]:
    """Get existing code analysis if available"""
    with get_monitoring_cursor() as cursor:
        if issue_id:
            cursor.execute('''
                SELECT id, issue_id, pattern_id, root_cause_summary, root_cause_details,
                       likely_files, claude_prompt, confidence_score, analysis_model,
                       created_at, status, applied_at, applied_by
                FROM code_analysis
                WHERE issue_id = %s
                ORDER BY created_at DESC
                LIMIT 1
            ''', (issue_id,))
        elif pattern_id:
            cursor.execute('''
                SELECT id, issue_id, pattern_id, root_cause_summary, root_cause_details,
                       likely_files, claude_prompt, confidence_score, analysis_model,
                       created_at, status, applied_at, applied_by
                FROM code_analysis
                WHERE pattern_id = %s
                ORDER BY created_at DESC
                LIMIT 1
            ''', (pattern_id,))
        else:
            return None

        row = cursor.fetchone()
        if not row:
            return None

        return {
            'id': row[0],
            'issue_id': row[1],
            'pattern_id': row[2],
            'root_cause_summary': row[3],
            'root_cause_details': row[4],
            'likely_files': row[5],
            'claude_prompt': row[6],
            'confidence_score': row[7],
            'analysis_model': row[8],
            'created_at': row[9].isoformat() if row[9] else None,
            'status': row[10],
            'applied_at': row[11].isoformat() if row[11] else None,
            'applied_by': row[12]
        }


def save_analysis(issue_id: int = None, pattern_id: int = None,
                  root_cause_summary: str = '', root_cause_details: str = '',
                  likely_files: List[str] = None, claude_prompt: str = '',
                  confidence_score: int = 50, analysis_model: str = None) -> int:
    """Save code analysis to database"""
    with get_monitoring_cursor() as cursor:
        cursor.execute('''
            INSERT INTO code_analysis
            (issue_id, pattern_id, root_cause_summary, root_cause_details,
             likely_files, claude_prompt, confidence_score, analysis_model)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (issue_id) WHERE issue_id IS NOT NULL
            DO UPDATE SET
                root_cause_summary = EXCLUDED.root_cause_summary,
                root_cause_details = EXCLUDED.root_cause_details,
                likely_files = EXCLUDED.likely_files,
                claude_prompt = EXCLUDED.claude_prompt,
                confidence_score = EXCLUDED.confidence_score,
                analysis_model = EXCLUDED.analysis_model,
                created_at = CURRENT_TIMESTAMP
            RETURNING id
        ''', (
            issue_id, pattern_id, root_cause_summary, root_cause_details,
            json.dumps(likely_files or []), claude_prompt,
            confidence_score, analysis_model
        ))

        return cursor.fetchone()[0]


def mark_analysis_applied(analysis_id: int, applied_by: str = 'dashboard') -> bool:
    """Mark an analysis as applied (fix implemented)"""
    with get_monitoring_cursor() as cursor:
        cursor.execute('''
            UPDATE code_analysis
            SET status = 'applied',
                applied_at = CURRENT_TIMESTAMP,
                applied_by = %s
            WHERE id = %s
            RETURNING id
        ''', (applied_by, analysis_id))

        return cursor.fetchone() is not None


def get_unanalyzed_issues(limit: int = 20) -> List[Dict]:
    """Get validated issues that haven't been analyzed yet"""
    with get_monitoring_cursor() as cursor:
        cursor.execute('''
            SELECT mi.id, mi.issue_type, mi.severity, mi.details,
                   l.message_in, l.message_out, l.intent
            FROM monitoring_issues mi
            LEFT JOIN logs l ON mi.log_id = l.id
            LEFT JOIN code_analysis ca ON mi.id = ca.issue_id
            WHERE mi.validated = TRUE
              AND mi.false_positive = FALSE
              AND mi.resolved_at IS NULL
              AND ca.id IS NULL
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

        columns = ['id', 'issue_type', 'severity', 'details',
                   'message_in', 'message_out', 'intent']
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


# ============================================================================
# ANALYSIS LOGIC
# ============================================================================

def generate_rule_based_analysis(issue: Dict) -> Dict:
    """Generate analysis using rule-based mapping (no AI)"""
    issue_type = issue.get('issue_type', 'unknown')
    mapping = ISSUE_CODE_MAPPINGS.get(issue_type, DEFAULT_CODE_MAPPING)

    # Build root cause summary based on issue type and details
    details = issue.get('details') or {}
    message_in = issue.get('message_in', '')
    message_out = issue.get('message_out', '')

    root_cause_summary = f"{mapping['description']}"

    # Add specific details based on what we know
    root_cause_details = f"Issue Type: {issue_type}\n"
    root_cause_details += f"Severity: {issue.get('severity', 'unknown')}\n"

    if details.get('pattern_matched'):
        root_cause_details += f"Pattern Matched: {details['pattern_matched']}\n"

    if issue.get('intent'):
        root_cause_details += f"Detected Intent: {issue['intent']}\n"

    root_cause_details += f"\nAreas to investigate: {', '.join(mapping['areas'])}\n"
    root_cause_details += f"Hint: {mapping['lines_hint']}"

    # Generate Claude Code prompt
    claude_prompt = generate_claude_prompt(
        issue=issue,
        mapping=mapping,
        root_cause_summary=root_cause_summary
    )

    return {
        'root_cause_summary': root_cause_summary,
        'root_cause_details': root_cause_details,
        'likely_files': mapping['files'],
        'claude_prompt': claude_prompt,
        'confidence_score': 60,  # Rule-based = medium confidence
        'analysis_model': 'rule_based'
    }


def generate_ai_analysis(issue: Dict) -> Dict:
    """Generate analysis using AI for deeper insights"""
    if not OPENAI_API_KEY:
        logger.warning("No OpenAI API key, falling back to rule-based analysis")
        return generate_rule_based_analysis(issue)

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
    except ImportError:
        logger.warning("OpenAI not installed, falling back to rule-based")
        return generate_rule_based_analysis(issue)

    issue_type = issue.get('issue_type', 'unknown')
    mapping = ISSUE_CODE_MAPPINGS.get(issue_type, DEFAULT_CODE_MAPPING)
    details = issue.get('details') or {}
    conversation = issue.get('conversation', [])

    # Build conversation context for AI
    conversation_text = ""
    if conversation:
        conversation_text = "\nCONVERSATION HISTORY (oldest to newest):\n"
        for i, msg in enumerate(conversation, 1):
            marker = " [THIS IS THE ISSUE]" if msg.get('is_issue') else ""
            conversation_text += f"\n{i}. User: \"{msg.get('user', 'N/A')}\"{marker}\n"
            conversation_text += f"   Bot: \"{msg.get('bot', 'N/A')}\"\n"
            if msg.get('intent'):
                conversation_text += f"   Intent: {msg['intent']}\n"
    else:
        conversation_text = f"""
USER MESSAGE: "{issue.get('message_in', 'N/A')}"
BOT RESPONSE: "{issue.get('message_out', 'N/A')}"
"""

    prompt = f"""Analyze this issue from an SMS reminder service and identify the root cause.

ISSUE DETAILS:
- Type: {issue_type}
- Severity: {issue.get('severity', 'unknown')}
- Intent detected: {issue.get('intent', 'N/A')}
- Pattern matched: {details.get('pattern_matched', 'N/A')}
{conversation_text}
LIKELY CODE AREAS: {', '.join(mapping['files'])}
KNOWN ISSUES IN THIS AREA: {mapping['description']}

Review the conversation history to understand the full context. The issue may be caused by:
- Previous messages setting incorrect state
- Multi-turn conversation handling problems
- Context not being preserved between messages
- User getting confused by earlier bot responses

Provide a brief analysis in JSON format:
{{
    "root_cause_summary": "One sentence summary of the likely root cause",
    "root_cause_details": "2-3 sentences with specific details about what went wrong, referencing the conversation flow if relevant",
    "likely_files": ["file1.py", "file2.py"],
    "confidence": 50-100,
    "recommended_fix": "Brief description of how to fix it"
}}
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an expert software engineer analyzing bugs in a Python/FastAPI SMS service. Be specific and actionable."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=500
        )

        content = response.choices[0].message.content

        # Parse JSON from response
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        result = json.loads(content)

        # Generate Claude Code prompt with AI insights
        claude_prompt = generate_claude_prompt(
            issue=issue,
            mapping=mapping,
            root_cause_summary=result.get('root_cause_summary', ''),
            recommended_fix=result.get('recommended_fix', '')
        )

        return {
            'root_cause_summary': result.get('root_cause_summary', mapping['description']),
            'root_cause_details': result.get('root_cause_details', ''),
            'likely_files': result.get('likely_files', mapping['files']),
            'claude_prompt': claude_prompt,
            'confidence_score': result.get('confidence', 70),
            'analysis_model': 'gpt-4o-mini'
        }

    except Exception as e:
        logger.error(f"AI analysis failed: {e}")
        return generate_rule_based_analysis(issue)


def format_conversation_chain(conversation: List[Dict]) -> str:
    """Format conversation chain for display in prompts"""
    if not conversation:
        return "No conversation history available."

    lines = []
    for i, msg in enumerate(conversation, 1):
        marker = " **[ISSUE]**" if msg.get('is_issue') else ""
        lines.append(f"### Message {i}{marker}")
        lines.append(f"**User:** {msg.get('user', 'N/A')}")
        lines.append(f"**Bot:** {msg.get('bot', 'N/A')}")
        if msg.get('intent'):
            lines.append(f"*Intent: {msg['intent']}*")
        lines.append("")

    return "\n".join(lines)


def generate_claude_prompt(issue: Dict, mapping: Dict,
                           root_cause_summary: str = '',
                           recommended_fix: str = '') -> str:
    """Generate a copy-paste ready Claude Code prompt"""
    issue_type = issue.get('issue_type', 'unknown')
    details = issue.get('details') or {}
    message_in = issue.get('message_in', 'N/A')
    message_out = issue.get('message_out', 'N/A')
    conversation = issue.get('conversation', [])

    prompt = f"""## Issue Summary
{issue_type.replace('_', ' ').title()}: {root_cause_summary}

## Conversation Context
"""

    # Add full conversation chain if available
    if conversation and len(conversation) > 1:
        prompt += f"*Showing {len(conversation)} messages leading up to the issue:*\n\n"
        prompt += format_conversation_chain(conversation)
    else:
        # Fallback to single message display
        prompt += f"""**User:** "{message_in}"
**Bot:** "{message_out}"
"""

    prompt += f"""
## Root Cause Analysis
{root_cause_summary}
"""

    if details.get('pattern_matched'):
        prompt += f"\nPattern matched: `{details['pattern_matched']}`"

    if issue.get('intent'):
        prompt += f"\nDetected intent: {issue['intent']}"

    prompt += f"""

## Files to Review
"""
    for f in mapping['files']:
        prompt += f"- {f}\n"

    prompt += f"\nSearch hint: {mapping['lines_hint']}"

    prompt += """

## Recommended Fix
"""
    if recommended_fix:
        prompt += f"{recommended_fix}\n"
    else:
        prompt += f"""1. Review the files listed above
2. Look for edge cases related to: {', '.join(mapping['areas'])}
3. Add handling for this specific scenario
4. Add a test case to prevent regression
"""

    prompt += """
## Acceptance Criteria
- The specific user input should now be handled correctly
- Existing functionality remains unaffected
- Add test case for this scenario if applicable
"""

    return prompt.strip()


# ============================================================================
# MAIN ANALYSIS ENGINE
# ============================================================================

def analyze_issue(issue_id: int, use_ai: bool = True, force: bool = False) -> Dict:
    """Analyze a single issue and generate Claude Code prompt"""
    init_analyzer_tables()

    # Check for existing analysis
    if not force:
        existing = get_existing_analysis(issue_id=issue_id)
        if existing:
            return existing

    # Get issue details
    issue = get_issue_details(issue_id)
    if not issue:
        return {'error': f'Issue #{issue_id} not found'}

    if issue.get('false_positive'):
        return {'error': f'Issue #{issue_id} is a false positive'}

    # Generate analysis
    if use_ai:
        analysis = generate_ai_analysis(issue)
    else:
        analysis = generate_rule_based_analysis(issue)

    # Save to database
    analysis_id = save_analysis(
        issue_id=issue_id,
        **analysis
    )

    analysis['id'] = analysis_id
    analysis['issue_id'] = issue_id
    return analysis


def analyze_pattern(pattern_id: int, use_ai: bool = True, force: bool = False) -> Dict:
    """Analyze a pattern and generate Claude Code prompt"""
    init_analyzer_tables()

    # Check for existing analysis
    if not force:
        existing = get_existing_analysis(pattern_id=pattern_id)
        if existing:
            return existing

    # Get pattern details
    pattern = get_pattern_details(pattern_id)
    if not pattern:
        return {'error': f'Pattern #{pattern_id} not found'}

    # Use the first sample issue to generate analysis
    if pattern['sample_issues']:
        sample = pattern['sample_issues'][0]
        issue_type = sample.get('issue_type', 'unknown')
        mapping = ISSUE_CODE_MAPPINGS.get(issue_type, DEFAULT_CODE_MAPPING)

        # Create a synthetic issue for analysis
        synthetic_issue = {
            'issue_type': issue_type,
            'severity': sample.get('severity', 'medium'),
            'message_in': sample.get('message_in', ''),
            'message_out': sample.get('message_out', ''),
            'details': {},
            'intent': None
        }

        if use_ai:
            analysis = generate_ai_analysis(synthetic_issue)
        else:
            analysis = generate_rule_based_analysis(synthetic_issue)

        # Adjust for pattern context
        analysis['root_cause_summary'] = f"Pattern '{pattern['pattern_name']}': {analysis['root_cause_summary']}"
        analysis['root_cause_details'] = f"Affects {pattern['issue_count']} issues\n" + analysis['root_cause_details']

        # Save to database
        analysis_id = save_analysis(
            pattern_id=pattern_id,
            **analysis
        )

        analysis['id'] = analysis_id
        analysis['pattern_id'] = pattern_id
        return analysis

    return {'error': 'No sample issues available for pattern'}


def run_code_analysis(hours: int = 24, use_ai: bool = True, dry_run: bool = False) -> Dict:
    """
    Main analysis function. Analyzes unanalyzed issues.

    Args:
        hours: Not used currently (analyzes all unanalyzed)
        use_ai: Whether to use AI for analysis
        dry_run: If True, don't write to database

    Returns:
        dict with analysis results
    """
    init_analyzer_tables()

    # Start analysis run
    run_id = None
    if not dry_run:
        with get_monitoring_cursor() as cursor:
            cursor.execute('''
                INSERT INTO code_analysis_runs (use_ai)
                VALUES (%s)
                RETURNING id
            ''', (use_ai,))
            run_id = cursor.fetchone()[0]

    results = {
        'run_id': run_id,
        'started_at': datetime.utcnow().isoformat(),
        'issues_analyzed': 0,
        'analyses_generated': 0,
        'analyses': [],
        'errors': []
    }

    try:
        # Get unanalyzed issues (basic info)
        issues = get_unanalyzed_issues(limit=20)
        results['issues_analyzed'] = len(issues)

        if not issues:
            logger.info("No unanalyzed issues found")
            return results

        # Analyze each issue
        for basic_issue in issues:
            try:
                # Fetch full issue details including conversation context
                issue = get_issue_details(basic_issue['id'])
                if not issue:
                    logger.warning(f"Could not fetch details for issue #{basic_issue['id']}")
                    issue = basic_issue  # Fall back to basic info

                if use_ai:
                    analysis = generate_ai_analysis(issue)
                else:
                    analysis = generate_rule_based_analysis(issue)

                if not dry_run:
                    analysis_id = save_analysis(
                        issue_id=basic_issue['id'],
                        **analysis
                    )
                    analysis['id'] = analysis_id

                analysis['issue_id'] = basic_issue['id']
                results['analyses'].append(analysis)
                results['analyses_generated'] += 1

            except Exception as e:
                logger.error(f"Failed to analyze issue #{basic_issue['id']}: {e}")
                results['errors'].append({
                    'issue_id': basic_issue['id'],
                    'error': str(e)
                })

        # Complete analysis run
        if not dry_run and run_id:
            with get_monitoring_cursor() as cursor:
                cursor.execute('''
                    UPDATE code_analysis_runs
                    SET completed_at = NOW(),
                        issues_analyzed = %s,
                        analyses_generated = %s,
                        status = 'completed'
                    WHERE id = %s
                ''', (results['issues_analyzed'], results['analyses_generated'], run_id))

        results['completed_at'] = datetime.utcnow().isoformat()

    except Exception as e:
        logger.error(f"Code analysis failed: {e}", exc_info=True)
        results['error'] = str(e)

        if not dry_run and run_id:
            with get_monitoring_cursor() as cursor:
                cursor.execute('''
                    UPDATE code_analysis_runs SET status = 'failed' WHERE id = %s
                ''', (run_id,))

    return results


def generate_report(results: Dict) -> str:
    """Generate a human-readable report from analysis results"""
    lines = [
        "=" * 60,
        "CODE ANALYZER REPORT",
        f"Agent 4 - {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 60,
        "",
        f"Issues Analyzed: {results['issues_analyzed']}",
        f"Analyses Generated: {results['analyses_generated']}",
        "",
    ]

    if results.get('error'):
        lines.append(f"ERROR: {results['error']}")
        lines.append("")

    if results.get('errors'):
        lines.append(f"ERRORS ({len(results['errors'])}):")
        lines.append("-" * 40)
        for err in results['errors']:
            lines.append(f"  Issue #{err['issue_id']}: {err['error']}")
        lines.append("")

    if results.get('analyses'):
        lines.append("ANALYSES GENERATED:")
        lines.append("-" * 40)
        for analysis in results['analyses'][:5]:
            lines.append(f"\nIssue #{analysis.get('issue_id', 'N/A')}:")
            lines.append(f"  Summary: {analysis['root_cause_summary'][:80]}...")
            lines.append(f"  Confidence: {analysis['confidence_score']}%")
            lines.append(f"  Files: {', '.join(analysis['likely_files'][:3])}")

        if len(results['analyses']) > 5:
            lines.append(f"\n  ... and {len(results['analyses']) - 5} more")

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
        description='Agent 4: Code Analyzer - Identify root causes and generate fix prompts'
    )
    parser.add_argument(
        '--issue', type=int, metavar='ID',
        help='Analyze specific issue by ID'
    )
    parser.add_argument(
        '--pattern', type=int, metavar='ID',
        help='Analyze specific pattern by ID'
    )
    parser.add_argument(
        '--no-ai', action='store_true',
        help='Use rule-based analysis only (no AI)'
    )
    parser.add_argument(
        '--report', action='store_true',
        help='Dry run - analyze but do not save'
    )
    parser.add_argument(
        '--force', action='store_true',
        help='Force re-analysis even if existing analysis exists'
    )
    parser.add_argument(
        '--json', action='store_true',
        help='Output as JSON'
    )

    args = parser.parse_args()

    print(f"\n{'=' * 60}")
    print("  AGENT 4: CODE ANALYZER")
    print(f"  Environment: {ENVIRONMENT}")
    print(f"  AI: {'Enabled' if not args.no_ai else 'Disabled'}")
    print(f"{'=' * 60}\n")

    # Analyze specific issue
    if args.issue:
        print(f"Analyzing issue #{args.issue}...")
        result = analyze_issue(
            args.issue,
            use_ai=not args.no_ai,
            force=args.force
        )

        if args.json:
            print(json.dumps(result, indent=2, default=str))
        elif result.get('error'):
            print(f"\nError: {result['error']}")
        else:
            print(f"\n{'=' * 60}")
            print("ROOT CAUSE ANALYSIS")
            print(f"{'=' * 60}")
            print(f"\nSummary: {result['root_cause_summary']}")
            print(f"Confidence: {result['confidence_score']}%")
            print(f"Model: {result['analysis_model']}")
            print(f"\nFiles to review:")
            for f in result['likely_files']:
                print(f"  - {f}")
            print(f"\n{'=' * 60}")
            print("CLAUDE CODE PROMPT")
            print(f"{'=' * 60}")
            print(result['claude_prompt'])
        return

    # Analyze specific pattern
    if args.pattern:
        print(f"Analyzing pattern #{args.pattern}...")
        result = analyze_pattern(
            args.pattern,
            use_ai=not args.no_ai,
            force=args.force
        )

        if args.json:
            print(json.dumps(result, indent=2, default=str))
        elif result.get('error'):
            print(f"\nError: {result['error']}")
        else:
            print(f"\nSummary: {result['root_cause_summary']}")
            print(f"Confidence: {result['confidence_score']}%")
            print(f"\n{result['claude_prompt']}")
        return

    # Run batch analysis
    print("Running batch code analysis...")
    results = run_code_analysis(
        use_ai=not args.no_ai,
        dry_run=args.report
    )

    if args.json:
        # Remove full prompts from JSON output (too verbose)
        output = {**results}
        if output.get('analyses'):
            output['analyses'] = [
                {k: v for k, v in a.items() if k != 'claude_prompt'}
                for a in output['analyses']
            ]
        print(json.dumps(output, indent=2, default=str))
    else:
        report = generate_report(results)
        print(report)


if __name__ == '__main__':
    main()
