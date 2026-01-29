"""
Agent 4: Fix Planner
Analyzes validated issues, reads relevant source code, and generates Claude Code prompts
that can be pasted to get a fix plan.

Designed for multi-agent pipeline:
- Reads validated, unresolved issues from Agent 2/3
- Identifies affected source files based on issue type/pattern
- Extracts relevant code context
- Generates paste-ready Claude Code prompts

Usage:
    python -m agents.fix_planner                    # Process all unresolved issues
    python -m agents.fix_planner --issue 123        # Single issue
    python -m agents.fix_planner --pattern timezone # All issues with pattern
    python -m agents.fix_planner --limit 5          # Process max 5 issues
    python -m agents.fix_planner --output clipboard # Copy prompt to clipboard
    python -m agents.fix_planner --output file      # Save to .md file
    python -m agents.fix_planner --list             # Show pending proposals
"""

import os
import sys
import json
import argparse
from datetime import datetime
from typing import Optional, List, Dict
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, '.')

from database import get_monitoring_cursor, logger
from config import ENVIRONMENT

# ============================================================================
# FILE MAPPING - Maps issue types to likely affected source files
# ============================================================================

# Base directory of the project
PROJECT_ROOT = Path(__file__).parent.parent

# Issue type to file mapping
ISSUE_TYPE_TO_FILES = {
    'parsing_failure': [
        'services/ai_service.py',
        'main.py',
        'routes/handlers/',
    ],
    'timezone_issue': [
        'utils/timezone.py',
        'models/reminder.py',
        'tasks/reminder_tasks.py',
        'services/ai_service.py',
    ],
    'delivery_failure': [
        'services/sms_service.py',
        'tasks/reminder_tasks.py',
        'celery_app.py',
    ],
    'error_response': [
        'main.py',
        'services/ai_service.py',
        'routes/handlers/',
    ],
    'user_confusion': [
        'services/ai_service.py',
        'services/onboarding_service.py',
        'routes/handlers/',
    ],
    'failed_action': [
        'services/ai_service.py',
        'models/',
        'routes/handlers/',
    ],
    'confidence_rejection': [
        'services/ai_service.py',
        'config.py',
    ],
    'repeated_attempts': [
        'services/ai_service.py',
        'main.py',
        'routes/handlers/',
    ],
}

# Pattern-specific file mappings (more granular)
PATTERN_TO_FILES = {
    'reminder_parsing': [
        'services/ai_service.py',
        'models/reminder.py',
        'routes/handlers/reminders.py',
    ],
    'list_operations': [
        'services/ai_service.py',
        'models/list_model.py',
        'routes/handlers/lists.py',
    ],
    'memory_operations': [
        'services/ai_service.py',
        'models/memory.py',
        'routes/handlers/memories.py',
    ],
    'onboarding': [
        'services/onboarding_service.py',
        'services/ai_service.py',
        'main.py',
    ],
    'timezone': [
        'utils/timezone.py',
        'models/reminder.py',
        'tasks/reminder_tasks.py',
    ],
    'sms_delivery': [
        'services/sms_service.py',
        'tasks/reminder_tasks.py',
    ],
}


# ============================================================================
# DATABASE OPERATIONS
# ============================================================================

def init_fix_planner_tables():
    """Create fix planner tables if they don't exist"""
    with get_monitoring_cursor() as cursor:
        # Fix proposals table - stores generated prompts
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS fix_proposals (
                id SERIAL PRIMARY KEY,
                issue_id INTEGER REFERENCES monitoring_issues(id),
                pattern_id INTEGER,
                affected_files JSONB,
                code_context TEXT,
                claude_prompt TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Fix proposal runs table - audit trail
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS fix_proposal_runs (
                id SERIAL PRIMARY KEY,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                issues_analyzed INTEGER DEFAULT 0,
                proposals_generated INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running'
            )
        ''')

        # Indexes
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_fix_proposals_issue
            ON fix_proposals(issue_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_fix_proposals_status
            ON fix_proposals(status)
        ''')

        logger.info("Fix planner tables initialized")


def get_unresolved_issues(limit: int = 20, issue_id: int = None,
                          pattern_name: str = None) -> List[Dict]:
    """Get validated issues that need fixes (validated=true, resolved_at=null)"""
    with get_monitoring_cursor() as cursor:
        if issue_id:
            # Get specific issue
            cursor.execute('''
                SELECT mi.id, mi.log_id, mi.phone_number, mi.issue_type,
                       mi.severity, mi.details, mi.detected_at, mi.validated_at,
                       l.message_in, l.message_out, l.intent,
                       ip.pattern_name, ip.description as pattern_description
                FROM monitoring_issues mi
                LEFT JOIN logs l ON mi.log_id = l.id
                LEFT JOIN issue_pattern_links ipl ON mi.id = ipl.issue_id
                LEFT JOIN issue_patterns ip ON ipl.pattern_id = ip.id
                WHERE mi.id = %s
            ''', (issue_id,))
        elif pattern_name:
            # Get issues by pattern name
            cursor.execute('''
                SELECT mi.id, mi.log_id, mi.phone_number, mi.issue_type,
                       mi.severity, mi.details, mi.detected_at, mi.validated_at,
                       l.message_in, l.message_out, l.intent,
                       ip.pattern_name, ip.description as pattern_description
                FROM monitoring_issues mi
                LEFT JOIN logs l ON mi.log_id = l.id
                LEFT JOIN issue_pattern_links ipl ON mi.id = ipl.issue_id
                LEFT JOIN issue_patterns ip ON ipl.pattern_id = ip.id
                WHERE mi.validated = TRUE
                  AND mi.false_positive = FALSE
                  AND mi.resolved_at IS NULL
                  AND ip.pattern_name ILIKE %s
                ORDER BY mi.severity DESC, mi.detected_at DESC
                LIMIT %s
            ''', (f'%{pattern_name}%', limit))
        else:
            # Get all unresolved validated issues
            cursor.execute('''
                SELECT mi.id, mi.log_id, mi.phone_number, mi.issue_type,
                       mi.severity, mi.details, mi.detected_at, mi.validated_at,
                       l.message_in, l.message_out, l.intent,
                       ip.pattern_name, ip.description as pattern_description
                FROM monitoring_issues mi
                LEFT JOIN logs l ON mi.log_id = l.id
                LEFT JOIN issue_pattern_links ipl ON mi.id = ipl.issue_id
                LEFT JOIN issue_patterns ip ON ipl.pattern_id = ip.id
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
                   'details', 'detected_at', 'validated_at', 'message_in',
                   'message_out', 'intent', 'pattern_name', 'pattern_description']
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def get_pending_proposals(limit: int = 20) -> List[Dict]:
    """Get pending fix proposals"""
    with get_monitoring_cursor() as cursor:
        cursor.execute('''
            SELECT fp.id, fp.issue_id, fp.affected_files, fp.status, fp.created_at,
                   mi.issue_type, mi.severity
            FROM fix_proposals fp
            JOIN monitoring_issues mi ON fp.issue_id = mi.id
            WHERE fp.status = 'pending'
            ORDER BY fp.created_at DESC
            LIMIT %s
        ''', (limit,))

        columns = ['id', 'issue_id', 'affected_files', 'status', 'created_at',
                   'issue_type', 'severity']
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def save_proposal(issue_id: int, pattern_id: int, affected_files: List[str],
                  code_context: str, claude_prompt: str) -> int:
    """Save a fix proposal to the database"""
    with get_monitoring_cursor() as cursor:
        cursor.execute('''
            INSERT INTO fix_proposals (issue_id, pattern_id, affected_files, code_context, claude_prompt)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        ''', (issue_id, pattern_id, json.dumps(affected_files), code_context, claude_prompt))
        return cursor.fetchone()[0]


def update_proposal_status(proposal_id: int, status: str):
    """Update proposal status (pending, applied, rejected)"""
    with get_monitoring_cursor() as cursor:
        cursor.execute('''
            UPDATE fix_proposals SET status = %s WHERE id = %s
        ''', (status, proposal_id))


# ============================================================================
# FILE IDENTIFICATION
# ============================================================================

def identify_affected_files(issue: Dict, use_ai: bool = False) -> List[str]:
    """
    Identify which source files are likely affected by an issue.

    Args:
        issue: Issue dictionary with type, pattern, details
        use_ai: Whether to use Claude API for intelligent file identification

    Returns:
        List of file paths relative to project root
    """
    affected_files = set()

    # Start with issue type mapping
    issue_type = issue.get('issue_type', '')
    if issue_type in ISSUE_TYPE_TO_FILES:
        for path in ISSUE_TYPE_TO_FILES[issue_type]:
            affected_files.add(path)

    # Add pattern-specific files
    pattern_name = issue.get('pattern_name', '')
    if pattern_name:
        pattern_lower = pattern_name.lower()
        for pattern_key, files in PATTERN_TO_FILES.items():
            if pattern_key in pattern_lower:
                for path in files:
                    affected_files.add(path)

    # Check details for additional hints
    details = issue.get('details', {})
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except json.JSONDecodeError:
            details = {}

    intent = details.get('intent') or issue.get('intent', '')
    if intent:
        intent_lower = intent.lower()
        if 'reminder' in intent_lower:
            affected_files.update([
                'services/ai_service.py',
                'models/reminder.py',
                'routes/handlers/reminders.py',
            ])
        elif 'list' in intent_lower:
            affected_files.update([
                'services/ai_service.py',
                'models/list_model.py',
                'routes/handlers/lists.py',
            ])
        elif 'memory' in intent_lower or 'remember' in intent_lower:
            affected_files.update([
                'services/ai_service.py',
                'models/memory.py',
                'routes/handlers/memories.py',
            ])

    # Use AI for more intelligent file identification if enabled
    if use_ai and affected_files:
        try:
            ai_files = identify_files_with_ai(issue, list(affected_files))
            if ai_files:
                affected_files = set(ai_files)
        except Exception as e:
            logger.warning(f"AI file identification failed: {e}")

    return list(affected_files)


def identify_files_with_ai(issue: Dict, candidate_files: List[str]) -> List[str]:
    """
    Use Claude API to identify which files are most likely affected.

    Args:
        issue: Issue dictionary
        candidate_files: List of candidate file paths

    Returns:
        Ordered list of file paths, most relevant first
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        logger.warning("anthropic package not installed, skipping AI file identification")
        return candidate_files

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set, skipping AI file identification")
        return candidate_files

    client = Anthropic()

    # Build issue summary
    details = issue.get('details', {})
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except json.JSONDecodeError:
            details = {}

    issue_summary = f"""
Issue Type: {issue.get('issue_type', 'unknown')}
Severity: {issue.get('severity', 'unknown')}
Pattern: {issue.get('pattern_name', 'none')}
User Message: {issue.get('message_in', details.get('user_message', 'N/A'))[:200]}
System Response: {issue.get('message_out', details.get('our_response', 'N/A'))[:200]}
Intent: {issue.get('intent', details.get('intent', 'N/A'))}
"""

    # Get list of all Python files in project
    all_files = []
    for py_file in PROJECT_ROOT.rglob('*.py'):
        rel_path = py_file.relative_to(PROJECT_ROOT)
        # Skip test files and __pycache__
        if 'test' not in str(rel_path).lower() and '__pycache__' not in str(rel_path):
            all_files.append(str(rel_path))

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": f"""Given this issue in an SMS reminder application, which files are most likely to need changes?

{issue_summary}

Candidate files (already identified):
{json.dumps(candidate_files, indent=2)}

All available Python files:
{json.dumps(sorted(all_files)[:50], indent=2)}

Return ONLY a JSON array of file paths (strings), ordered from most to least relevant.
Include 3-6 files maximum. Example: ["services/ai_service.py", "main.py"]"""
        }]
    )

    # Parse response
    response_text = response.content[0].text.strip()

    # Try to extract JSON array from response
    try:
        # Handle case where response might have markdown code blocks
        if '```' in response_text:
            import re
            json_match = re.search(r'\[.*?\]', response_text, re.DOTALL)
            if json_match:
                response_text = json_match.group()

        files = json.loads(response_text)
        if isinstance(files, list):
            # Validate files exist
            valid_files = []
            for f in files:
                if (PROJECT_ROOT / f).exists() or any(f.endswith('/') for f in files):
                    valid_files.append(f)
            return valid_files if valid_files else candidate_files
    except json.JSONDecodeError:
        logger.warning(f"Could not parse AI file response: {response_text}")

    return candidate_files


# ============================================================================
# CODE CONTEXT EXTRACTION
# ============================================================================

def extract_code_context(files: List[str], max_lines_per_file: int = 100) -> str:
    """
    Extract relevant code snippets from identified files.

    Args:
        files: List of file paths to read
        max_lines_per_file: Maximum lines to include per file

    Returns:
        Formatted code context string
    """
    context_parts = []

    for file_path in files:
        full_path = PROJECT_ROOT / file_path

        # Handle directory paths (read all .py files in directory)
        if file_path.endswith('/'):
            dir_path = PROJECT_ROOT / file_path.rstrip('/')
            if dir_path.is_dir():
                for py_file in dir_path.glob('*.py'):
                    if py_file.name != '__init__.py':
                        snippet = read_file_snippet(py_file, max_lines_per_file // 2)
                        if snippet:
                            rel_path = py_file.relative_to(PROJECT_ROOT)
                            context_parts.append(f"### {rel_path}\n```python\n{snippet}\n```")
        elif full_path.exists():
            snippet = read_file_snippet(full_path, max_lines_per_file)
            if snippet:
                context_parts.append(f"### {file_path}\n```python\n{snippet}\n```")
        else:
            logger.warning(f"File not found: {file_path}")

    return "\n\n".join(context_parts)


def read_file_snippet(file_path: Path, max_lines: int = 100) -> Optional[str]:
    """
    Read a file and return a relevant snippet.

    For large files, tries to identify the most relevant section
    (functions, classes related to the file's purpose).
    """
    try:
        content = file_path.read_text(encoding='utf-8')
        lines = content.split('\n')

        if len(lines) <= max_lines:
            return content

        # For large files, return the first max_lines with a note
        truncated = '\n'.join(lines[:max_lines])
        return f"{truncated}\n\n# ... (file truncated, {len(lines)} total lines)"

    except Exception as e:
        logger.error(f"Error reading {file_path}: {e}")
        return None


# ============================================================================
# PROMPT GENERATION
# ============================================================================

def generate_claude_prompt(issue: Dict, code_context: str, affected_files: List[str]) -> str:
    """
    Generate a paste-ready Claude Code prompt for fixing the issue.

    Args:
        issue: Issue dictionary with all details
        code_context: Extracted code snippets
        affected_files: List of affected file paths

    Returns:
        Formatted prompt string
    """
    # Parse details
    details = issue.get('details', {})
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except json.JSONDecodeError:
            details = {}

    # Build the prompt
    prompt = f"""## Issue Context

- **Issue ID:** #{issue.get('id', 'N/A')}
- **Type:** {issue.get('issue_type', 'unknown')}
- **Severity:** {issue.get('severity', 'unknown')}
- **Pattern:** {issue.get('pattern_name', 'N/A')}
- **Pattern Description:** {issue.get('pattern_description', 'N/A')}
- **Detected:** {issue.get('detected_at', 'N/A')}

## User Interaction

**User said:**
> {issue.get('message_in', details.get('user_message', 'N/A'))}

**System replied:**
> {issue.get('message_out', details.get('our_response', 'N/A'))}

**Detected Intent:** {issue.get('intent', details.get('intent', 'N/A'))}

## Additional Details

"""

    # Add relevant details
    for key, value in details.items():
        if key not in ('user_message', 'our_response', 'intent'):
            prompt += f"- **{key}:** {value}\n"

    prompt += f"""
## Affected Files

The following files are likely involved in this issue:
{chr(10).join(f'- `{f}`' for f in affected_files)}

## Relevant Code

{code_context}

## Task

Review the issue above and create a plan to fix it. **Do not execute the fix** - only provide:

1. **Root Cause Analysis:** What is causing this issue?
2. **Proposed Solution:** How should this be fixed?
3. **Files to Modify:** Which specific files and functions need changes?
4. **Step-by-Step Implementation Plan:** Detailed steps to implement the fix
5. **Testing Approach:** How to verify the fix works correctly

Focus on:
- Minimal changes that fix the core issue
- Not introducing regressions
- Maintaining code consistency with existing patterns
"""

    return prompt


# ============================================================================
# MAIN PROCESSING
# ============================================================================

def process_issue(issue: Dict, use_ai: bool = False) -> Dict:
    """
    Process a single issue and generate a fix proposal.

    Args:
        issue: Issue dictionary
        use_ai: Whether to use AI for file identification

    Returns:
        Dict with proposal details
    """
    # Identify affected files
    affected_files = identify_affected_files(issue, use_ai=use_ai)

    # Extract code context
    code_context = extract_code_context(affected_files)

    # Generate prompt
    claude_prompt = generate_claude_prompt(issue, code_context, affected_files)

    # Get pattern_id if available
    pattern_id = None
    if issue.get('pattern_name'):
        with get_monitoring_cursor() as cursor:
            cursor.execute(
                'SELECT id FROM issue_patterns WHERE pattern_name = %s',
                (issue['pattern_name'],)
            )
            row = cursor.fetchone()
            if row:
                pattern_id = row[0]

    # Save proposal
    proposal_id = save_proposal(
        issue_id=issue['id'],
        pattern_id=pattern_id,
        affected_files=affected_files,
        code_context=code_context,
        claude_prompt=claude_prompt
    )

    return {
        'proposal_id': proposal_id,
        'issue_id': issue['id'],
        'affected_files': affected_files,
        'prompt': claude_prompt
    }


def run_fix_planner(limit: int = 20, issue_id: int = None, pattern: str = None,
                    use_ai: bool = False, dry_run: bool = False) -> Dict:
    """
    Run the fix planner on unresolved issues.

    Args:
        limit: Maximum number of issues to process
        issue_id: Specific issue ID to process
        pattern: Filter by pattern name
        use_ai: Use AI for file identification
        dry_run: Don't save to database

    Returns:
        Dict with results
    """
    init_fix_planner_tables()

    # Start run record
    run_id = None
    if not dry_run:
        with get_monitoring_cursor() as cursor:
            cursor.execute('''
                INSERT INTO fix_proposal_runs DEFAULT VALUES
                RETURNING id
            ''')
            run_id = cursor.fetchone()[0]

    results = {
        'run_id': run_id,
        'started_at': datetime.utcnow().isoformat(),
        'issues_analyzed': 0,
        'proposals_generated': 0,
        'proposals': []
    }

    try:
        # Get issues to process
        issues = get_unresolved_issues(limit=limit, issue_id=issue_id, pattern_name=pattern)
        results['issues_analyzed'] = len(issues)

        for issue in issues:
            try:
                proposal = process_issue(issue, use_ai=use_ai)
                results['proposals'].append(proposal)
                results['proposals_generated'] += 1
            except Exception as e:
                logger.error(f"Error processing issue #{issue['id']}: {e}")

        # Update run record
        if not dry_run and run_id:
            with get_monitoring_cursor() as cursor:
                cursor.execute('''
                    UPDATE fix_proposal_runs
                    SET completed_at = NOW(),
                        issues_analyzed = %s,
                        proposals_generated = %s,
                        status = 'completed'
                    WHERE id = %s
                ''', (results['issues_analyzed'], results['proposals_generated'], run_id))

        results['completed_at'] = datetime.utcnow().isoformat()

    except Exception as e:
        logger.error(f"Fix planner run failed: {e}", exc_info=True)
        results['error'] = str(e)

        if not dry_run and run_id:
            with get_monitoring_cursor() as cursor:
                cursor.execute('''
                    UPDATE fix_proposal_runs SET status = 'failed' WHERE id = %s
                ''', (run_id,))

    return results


# ============================================================================
# OUTPUT FUNCTIONS
# ============================================================================

def output_to_clipboard(prompt: str) -> bool:
    """Copy prompt to clipboard using pyperclip"""
    try:
        import pyperclip
        pyperclip.copy(prompt)
        return True
    except ImportError:
        logger.warning("pyperclip not installed. Install with: pip install pyperclip")
        return False
    except Exception as e:
        logger.error(f"Failed to copy to clipboard: {e}")
        return False


def output_to_file(prompt: str, issue_id: int) -> str:
    """Save prompt to a markdown file"""
    output_dir = PROJECT_ROOT / 'fix_proposals'
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    filename = f"issue_{issue_id}_{timestamp}.md"
    filepath = output_dir / filename

    filepath.write_text(prompt, encoding='utf-8')
    return str(filepath)


# ============================================================================
# CLI INTERFACE
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Agent 4: Fix Planner - Generate Claude Code prompts for issue fixes'
    )
    parser.add_argument(
        '--issue', type=int, metavar='ID',
        help='Process a specific issue by ID'
    )
    parser.add_argument(
        '--pattern', type=str,
        help='Filter issues by pattern name'
    )
    parser.add_argument(
        '--limit', type=int, default=5,
        help='Maximum number of issues to process (default: 5)'
    )
    parser.add_argument(
        '--output', choices=['stdout', 'clipboard', 'file'],
        default='stdout',
        help='Output destination (default: stdout)'
    )
    parser.add_argument(
        '--ai', action='store_true',
        help='Use Claude API for intelligent file identification'
    )
    parser.add_argument(
        '--list', action='store_true', dest='list_proposals',
        help='List pending fix proposals'
    )
    parser.add_argument(
        '--json', action='store_true',
        help='Output results as JSON'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Run without saving to database'
    )

    args = parser.parse_args()

    init_fix_planner_tables()

    # List pending proposals
    if args.list_proposals:
        proposals = get_pending_proposals()
        if args.json:
            print(json.dumps(proposals, indent=2, default=str))
        else:
            print(f"\nPending Fix Proposals: {len(proposals)}")
            print("-" * 60)
            for p in proposals:
                print(f"  #{p['id']} | Issue #{p['issue_id']} | {p['issue_type']} | {p['severity']}")
                print(f"       Files: {', '.join(p['affected_files'][:3]) if p['affected_files'] else 'N/A'}")
        return

    # Run fix planner
    print(f"\n{'='*60}")
    print("  AGENT 4: FIX PLANNER")
    print(f"  {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")

    results = run_fix_planner(
        limit=args.limit,
        issue_id=args.issue,
        pattern=args.pattern,
        use_ai=args.ai,
        dry_run=args.dry_run
    )

    print(f"  Issues analyzed: {results['issues_analyzed']}")
    print(f"  Proposals generated: {results['proposals_generated']}")
    print()

    if not results['proposals']:
        print("  No issues found to process.")
        return

    # Output prompts
    for proposal in results['proposals']:
        prompt = proposal['prompt']
        issue_id = proposal['issue_id']

        if args.output == 'clipboard':
            if output_to_clipboard(prompt):
                print(f"  Copied prompt for issue #{issue_id} to clipboard")
            else:
                print(f"  Failed to copy to clipboard, printing instead:\n")
                print(prompt)
        elif args.output == 'file':
            filepath = output_to_file(prompt, issue_id)
            print(f"  Saved prompt for issue #{issue_id} to: {filepath}")
        else:
            # stdout
            print(f"\n{'='*60}")
            print(f"  FIX PROPOSAL FOR ISSUE #{issue_id}")
            print(f"{'='*60}\n")
            print(prompt)
            print(f"\n{'='*60}\n")

    if args.json:
        # Also output JSON summary
        summary = {
            'run_id': results['run_id'],
            'issues_analyzed': results['issues_analyzed'],
            'proposals_generated': results['proposals_generated'],
            'proposal_ids': [p['proposal_id'] for p in results['proposals']]
        }
        print("\nJSON Summary:")
        print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
