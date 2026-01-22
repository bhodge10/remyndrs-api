#!/usr/bin/env python
"""
Quick runner for the Issue Validator agent (Agent 2).
Designed for easy manual runs and scheduled jobs.

Usage:
    python agents/run_validator.py           # Validate pending issues
    python agents/run_validator.py --no-ai   # Rule-based only
    python agents/run_validator.py --patterns # Show pattern analysis
"""

import sys
import os

# Ensure we're in the right directory
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, '.')

from agents.issue_validator import validate_issues, generate_report, analyze_patterns


def main():
    use_ai = '--no-ai' not in sys.argv

    if '--patterns' in sys.argv:
        print("\n📊 Pattern Analysis")
        print("=" * 50)
        patterns = analyze_patterns()

        if patterns['patterns']:
            print("\nActive Patterns:")
            for p in patterns['patterns'][:10]:
                print(f"  [{p['priority']:6}] {p['name']}: {p['count']} issues")

        if patterns['type_distribution']:
            print("\nIssue Type Accuracy (7 days):")
            for td in patterns['type_distribution']:
                print(f"  {td['type']:25} {td['total']:3} issues, {td['accuracy']}% real")

        return

    print(f"\n🔍 Running Issue Validator (AI: {use_ai})...\n")

    results = validate_issues(limit=50, use_ai=use_ai, dry_run=False)
    patterns = analyze_patterns()
    report = generate_report(results, patterns)
    print(report)

    # Quick summary
    fp_rate = len(results['false_positives']) / max(results['issues_processed'], 1) * 100
    print(f"\n✅ Validated {len(results['validated'])} issues, {len(results['false_positives'])} false positives ({fp_rate:.0f}%)")


if __name__ == '__main__':
    main()
