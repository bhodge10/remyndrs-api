#!/usr/bin/env python
"""
Quick runner for the Resolution Tracker agent (Agent 3).
Designed for easy manual runs and scheduled jobs.

Usage:
    python agents/run_tracker.py              # Show dashboard
    python agents/run_tracker.py --report     # Weekly report
    python agents/run_tracker.py --snapshot   # Save daily snapshot
"""

import sys
import os

# Ensure we're in the right directory
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, '.')

from agents.resolution_tracker import (
    show_dashboard, generate_weekly_report, format_report,
    calculate_health_metrics, save_health_snapshot, get_open_issues
)


def main():
    if '--report' in sys.argv:
        report = generate_weekly_report()
        print(format_report(report))
        return

    if '--snapshot' in sys.argv:
        metrics = calculate_health_metrics(days=1)
        save_health_snapshot(metrics)
        print(f"✅ Daily snapshot saved: health={metrics['health_score']:.0f}/100")
        return

    if '--open' in sys.argv:
        issues = get_open_issues(limit=20)
        print(f"\n📋 OPEN ISSUES ({len(issues)})")
        print("-" * 50)
        for i in issues:
            sev_icon = {'critical': '🔴', 'high': '🟠', 'medium': '🟡', 'low': '🟢'}.get(i['severity'], '⚪')
            print(f"  {sev_icon} #{i['id']:4} [{i['severity']:8}] {i['issue_type']}")
        return

    show_dashboard()


if __name__ == '__main__':
    main()
