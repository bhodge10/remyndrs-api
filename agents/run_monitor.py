#!/usr/bin/env python
"""
Quick runner for the Interaction Monitor agent.
Designed for easy manual runs and Replit scheduled jobs.

Usage:
    python agents/run_monitor.py          # Default 24 hours
    python agents/run_monitor.py 48       # Last 48 hours
    python agents/run_monitor.py --stats  # Show current stats
"""

import sys
import os

# Ensure we're in the right directory
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, '.')

from agents.interaction_monitor import analyze_interactions, generate_report, get_pending_issues


def show_stats():
    """Show current monitoring stats"""
    from database import get_monitoring_cursor

    print("\n📊 MONITORING STATS")
    print("=" * 50)

    with get_monitoring_cursor() as cursor:
        # Total issues
        cursor.execute('SELECT COUNT(*) FROM monitoring_issues')
        total = cursor.fetchone()[0]
        print(f"Total issues detected: {total}")

        # Pending validation
        cursor.execute('SELECT COUNT(*) FROM monitoring_issues WHERE validated = FALSE')
        pending = cursor.fetchone()[0]
        print(f"Pending validation: {pending}")

        # By severity
        cursor.execute('''
            SELECT severity, COUNT(*) FROM monitoring_issues
            WHERE validated = FALSE
            GROUP BY severity ORDER BY COUNT(*) DESC
        ''')
        print("\nBy Severity (unvalidated):")
        for row in cursor.fetchall():
            print(f"  {row[0]}: {row[1]}")

        # By type
        cursor.execute('''
            SELECT issue_type, COUNT(*) FROM monitoring_issues
            WHERE validated = FALSE
            GROUP BY issue_type ORDER BY COUNT(*) DESC
            LIMIT 10
        ''')
        print("\nBy Type (unvalidated):")
        for row in cursor.fetchall():
            print(f"  {row[0]}: {row[1]}")

        # Recent runs
        cursor.execute('''
            SELECT id, started_at, logs_analyzed, issues_found, status
            FROM monitoring_runs
            ORDER BY started_at DESC
            LIMIT 5
        ''')
        runs = cursor.fetchall()
        if runs:
            print("\nRecent Runs:")
            for run in runs:
                print(f"  #{run[0]} | {run[1].strftime('%Y-%m-%d %H:%M')} | "
                      f"Logs: {run[2]} | Issues: {run[3]} | {run[4]}")

    print("=" * 50)


def main():
    if len(sys.argv) > 1:
        if sys.argv[1] == '--stats':
            show_stats()
            return
        elif sys.argv[1] == '--pending':
            issues = get_pending_issues(20)
            print(f"\n🔔 {len(issues)} issues pending validation:\n")
            for i in issues:
                print(f"[{i['severity'].upper():8}] {i['issue_type']:20} | "
                      f"Log #{i['log_id']} | {i['phone_number'][-4:]}****")
            return
        else:
            try:
                hours = int(sys.argv[1])
            except ValueError:
                hours = 24
    else:
        hours = 24

    print(f"🔍 Running Interaction Monitor (last {hours} hours)...\n")

    results = analyze_interactions(hours=hours, dry_run=False)
    report = generate_report(results)
    print(report)

    # Quick summary for logs
    print(f"\n✅ Run complete: {len(results['issues_found'])} issues found")


if __name__ == '__main__':
    main()
