#!/usr/bin/env python
"""
Quick runner for the Fix Planner agent (Agent 4).
Generates Claude Code prompts for fixing validated issues.

Usage:
    python agents/run_fix_planner.py              # Process up to 5 unresolved issues
    python agents/run_fix_planner.py 123          # Process specific issue #123
    python agents/run_fix_planner.py --list       # Show pending proposals
    python agents/run_fix_planner.py --clipboard  # Copy prompt to clipboard
"""

import sys
import os

# Ensure we're in the right directory
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, '.')

from agents.fix_planner import (
    run_fix_planner,
    get_pending_proposals,
    get_unresolved_issues,
    output_to_clipboard,
    output_to_file,
    init_fix_planner_tables
)


def show_issues():
    """Show unresolved issues that can be processed"""
    issues = get_unresolved_issues(limit=20)
    print(f"\nUnresolved Issues ({len(issues)}):")
    print("-" * 70)
    for issue in issues:
        pattern = issue.get('pattern_name', 'N/A')
        print(f"  #{issue['id']:4} [{issue['severity']:8}] {issue['issue_type']:20} | {pattern}")
        if issue.get('message_in'):
            msg = issue['message_in'][:50] + '...' if len(issue['message_in']) > 50 else issue['message_in']
            print(f"         User: \"{msg}\"")
    print()


def main():
    init_fix_planner_tables()

    # Parse simple arguments
    issue_id = None
    output_mode = 'stdout'
    show_list = False
    show_unresolved = False

    for arg in sys.argv[1:]:
        if arg == '--list':
            show_list = True
        elif arg == '--issues':
            show_unresolved = True
        elif arg == '--clipboard':
            output_mode = 'clipboard'
        elif arg == '--file':
            output_mode = 'file'
        elif arg.isdigit():
            issue_id = int(arg)

    # Show pending proposals
    if show_list:
        proposals = get_pending_proposals()
        print(f"\nPending Fix Proposals ({len(proposals)}):")
        print("-" * 60)
        for p in proposals:
            files = p['affected_files'][:2] if p['affected_files'] else []
            files_str = ', '.join(files) + ('...' if len(p['affected_files'] or []) > 2 else '')
            print(f"  #{p['id']:4} | Issue #{p['issue_id']} | {p['issue_type']} | {p['severity']}")
            if files_str:
                print(f"         Files: {files_str}")
        return

    # Show unresolved issues
    if show_unresolved:
        show_issues()
        return

    # Run fix planner
    if issue_id:
        print(f"\nProcessing issue #{issue_id}...")
    else:
        print("\nProcessing unresolved issues...")

    results = run_fix_planner(
        limit=1 if issue_id else 5,
        issue_id=issue_id,
        use_ai=False
    )

    if not results['proposals']:
        print("\nNo issues found to process.")
        print("\nTip: Run 'python agents/run_fix_planner.py --issues' to see available issues")
        return

    # Output the prompts
    for proposal in results['proposals']:
        prompt = proposal['prompt']
        pid = proposal['issue_id']

        if output_mode == 'clipboard':
            if output_to_clipboard(prompt):
                print(f"\nCopied prompt for issue #{pid} to clipboard")
                print("Paste it into Claude Code to get a fix plan.")
            else:
                print("\nFailed to copy to clipboard. Printing instead:\n")
                print(prompt)
        elif output_mode == 'file':
            filepath = output_to_file(prompt, pid)
            print(f"\nSaved prompt for issue #{pid} to: {filepath}")
        else:
            print(f"\n{'='*70}")
            print(f"  FIX PROPOSAL FOR ISSUE #{pid}")
            print(f"{'='*70}")
            print(prompt)
            print(f"{'='*70}\n")

    print(f"\nProcessed {results['issues_analyzed']} issues, generated {results['proposals_generated']} proposals")


if __name__ == '__main__':
    main()
