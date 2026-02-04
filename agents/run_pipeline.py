#!/usr/bin/env python
"""
Multi-Agent Monitoring Pipeline Runner
Runs all four agents in sequence:
  Agent 1: Interaction Monitor - Detect anomalies
  Agent 2: Issue Validator - Validate and categorize
  Agent 3: Resolution Tracker - Track health and report
  Agent 4: Code Analyzer - Identify root causes and generate fix prompts

Usage:
    python agents/run_pipeline.py              # Full pipeline (Agents 1-3)
    python agents/run_pipeline.py --hours 48   # Custom time range
    python agents/run_pipeline.py --no-ai      # Skip AI validation
    python agents/run_pipeline.py --report     # Dry run (no DB writes)
    python agents/run_pipeline.py --snapshot   # Save daily health snapshot
    python agents/run_pipeline.py --fix-planner  # Include Agent 4 (fix prompts)
"""

import sys
import os
import argparse
from datetime import datetime

# Ensure we're in the right directory
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, '.')


def run_pipeline(hours: int = 24, use_ai: bool = True, dry_run: bool = False,
                 save_snapshot: bool = False, include_fix_planner: bool = False,
                 fix_limit: int = 5):
    """Run the complete monitoring pipeline"""
    from agents.interaction_monitor import analyze_interactions, generate_report as monitor_report
    from agents.issue_validator import validate_issues, generate_report as validator_report, analyze_patterns
    from agents.resolution_tracker import calculate_health_metrics, save_health_snapshot, get_open_issues, detect_regressions, auto_resolve_stale_issues
    from agents.code_analyzer import run_code_analysis, generate_report as analyzer_report

    print("=" * 70)
    print("  REMYNDRS MONITORING PIPELINE")
    print(f"  Started: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 70)
    print()

    validator_results = None

    # ========================================
    # AGENT 1: Interaction Monitor
    # ========================================
    print("┌" + "─" * 68 + "┐")
    print("│ AGENT 1: Interaction Monitor" + " " * 38 + "│")
    print("└" + "─" * 68 + "┘")
    print(f"  Analyzing last {hours} hours of interactions...")
    print()

    monitor_results = analyze_interactions(hours=hours, dry_run=dry_run)

    print(f"  ✓ Logs analyzed: {monitor_results['logs_analyzed']}")
    print(f"  ✓ Issues detected: {len(monitor_results['issues_found'])}")

    if monitor_results.get('summary'):
        print("\n  Issues by type:")
        for issue_type, count in sorted(monitor_results['summary'].items(), key=lambda x: -x[1]):
            severity_icon = {
                'delivery_failure': '🚨',
                'error_response': '❌',
                'failed_action': '⚠️',
                'timezone_issue': '🌐',
                'user_confusion': '❓',
                'parsing_failure': '🔍',
                'confidence_rejection': '🎯',
                'repeated_attempts': '🔄',
            }.get(issue_type, '•')
            print(f"    {severity_icon} {issue_type}: {count}")

    print()

    # ========================================
    # AGENT 2: Issue Validator
    # ========================================
    print("┌" + "─" * 68 + "┐")
    print("│ AGENT 2: Issue Validator" + " " * 42 + "│")
    print("└" + "─" * 68 + "┘")

    pending_count = len(monitor_results['issues_found'])
    if pending_count == 0:
        print("  No new issues to validate.")
        print()
    else:
        print(f"  Validating {pending_count} issues (AI: {use_ai})...")
        print()

        validator_results = validate_issues(
            limit=100,
            use_ai=use_ai,
            dry_run=dry_run
        )

        print(f"  ✓ Issues processed: {validator_results['issues_processed']}")
        print(f"  ✓ Real issues: {len(validator_results['validated'])}")
        print(f"  ✓ False positives: {len(validator_results['false_positives'])}")

        if validator_results['issues_processed'] > 0:
            fp_rate = len(validator_results['false_positives']) / validator_results['issues_processed'] * 100
            print(f"  ✓ Detection accuracy: {100 - fp_rate:.1f}%")

        if validator_results.get('patterns_found'):
            print("\n  Patterns identified:")
            for pattern, count in validator_results['patterns_found'].items():
                print(f"    • {pattern}: {count} issues")

        if validator_results.get('severity_adjustments'):
            print(f"\n  Severity adjustments: {len(validator_results['severity_adjustments'])}")

    print()

    # ========================================
    # AGENT 3: Resolution Tracker
    # ========================================
    print("┌" + "─" * 68 + "┐")
    print("│ AGENT 3: Resolution Tracker" + " " * 39 + "│")
    print("└" + "─" * 68 + "┘")

    # Calculate health metrics
    health = calculate_health_metrics(days=7)

    status_icons = {
        'excellent': '🟢',
        'good': '🟢',
        'fair': '🟡',
        'poor': '🟠',
        'critical': '🔴'
    }
    icon = status_icons.get(health['health_status'], '⚪')

    print(f"\n  {icon} HEALTH SCORE: {health['health_score']:.0f}/100 ({health['health_status'].upper()})")
    print()
    print(f"  ✓ Total interactions (7d): {health['total_interactions']:,}")
    print(f"  ✓ Issue rate: {health['issue_rate']}%")
    print(f"  ✓ Resolution rate: {health['resolution_rate']}%")
    print(f"  ✓ Open issues: {health['open_issues']}")

    # Check for regressions
    if not dry_run:
        regressions = detect_regressions()
        if regressions:
            print(f"\n  ⚠️  REGRESSIONS DETECTED: {len(regressions)}")
            for r in regressions[:3]:
                print(f"    • {r['pattern_name']}: {r['new_issues_since']} new issues since fix")

        # Auto-resolve stale issues
        auto_resolved = auto_resolve_stale_issues()
        if auto_resolved:
            print(f"\n  ✅ AUTO-RESOLVED: {len(auto_resolved)} stale issues")
            for ar in auto_resolved[:3]:
                print(f"    • #{ar['issue_id']} {ar['issue_type']} [{ar['severity']}]")
            if len(auto_resolved) > 3:
                print(f"    ... and {len(auto_resolved) - 3} more")

    # Save snapshot if requested
    if save_snapshot and not dry_run:
        save_health_snapshot(health)
        print(f"\n  ✓ Daily health snapshot saved")

    print()

    # ========================================
    # AGENT 4: Code Analyzer
    # ========================================
    print("┌" + "─" * 68 + "┐")
    print("│ AGENT 4: Code Analyzer" + " " * 44 + "│")
    print("└" + "─" * 68 + "┘")

    # Run code analysis on unanalyzed issues
    analyzer_results = run_code_analysis(use_ai=use_ai, dry_run=dry_run)

    print(f"\n  ✓ Issues analyzed: {analyzer_results['issues_analyzed']}")
    print(f"  ✓ Analyses generated: {analyzer_results['analyses_generated']}")

    if analyzer_results.get('analyses'):
        print("\n  Code analyses created:")
        for analysis in analyzer_results['analyses'][:3]:
            confidence = analysis.get('confidence_score', 0)
            conf_icon = '🟢' if confidence >= 80 else '🟡' if confidence >= 60 else '🔴'
            print(f"    {conf_icon} Issue #{analysis.get('issue_id', 'N/A')}: {analysis['root_cause_summary'][:50]}...")

        if len(analyzer_results['analyses']) > 3:
            print(f"    ... and {len(analyzer_results['analyses']) - 3} more")

    if analyzer_results.get('errors'):
        print(f"\n  ⚠️  Errors: {len(analyzer_results['errors'])}")

    print()

    # ========================================
    # SUMMARY
    # ========================================
    print("┌" + "─" * 68 + "┐")
    print("│ PIPELINE SUMMARY" + " " * 50 + "│")
    print("└" + "─" * 68 + "┘")

    # Get pattern analysis
    patterns = analyze_patterns()

    print(f"\n  Total issues in system:")
    if patterns.get('type_distribution'):
        for td in patterns['type_distribution'][:5]:
            bar_len = min(td['total'], 20)
            bar = "█" * bar_len
            print(f"    {td['type']:22} {bar:20} {td['total']:3} ({td['accuracy']}% real)")

    if patterns.get('top_affected_users'):
        print(f"\n  Users needing attention:")
        for u in patterns['top_affected_users'][:3]:
            print(f"    {u['phone']}: {u['count']} issues")

    # Show open issues needing resolution
    open_issues = get_open_issues(limit=5)
    if open_issues:
        print(f"\n  📋 Open issues needing resolution:")
        for issue in open_issues:
            sev_icon = {'critical': '🔴', 'high': '🟠', 'medium': '🟡', 'low': '🟢'}.get(issue['severity'], '⚪')
            print(f"    {sev_icon} #{issue['id']} [{issue['severity']:8}] {issue['issue_type']}")

    if patterns.get('patterns') and any(p['count'] >= 3 for p in patterns['patterns']):
        print(f"\n  🎯 Action items (patterns with 3+ issues):")
        for p in patterns['patterns']:
            if p['count'] >= 3:
                print(f"    • {p['name']}: {p['count']} issues")
                if p.get('description'):
                    print(f"      └─ {p['description']}")

    print()
    print("=" * 70)
    print(f"  Pipeline completed: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    if dry_run:
        print("  (DRY RUN - no changes saved)")
    print("=" * 70)

    return {
        'monitor': monitor_results,
        'validator': validator_results,
        'analyzer': analyzer_results,
        'health': health,
        'patterns': patterns
    }


def main():
    parser = argparse.ArgumentParser(
        description='Run the complete monitoring pipeline (Agent 1 + Agent 2 + Agent 3 + optional Agent 4)'
    )
    parser.add_argument(
        '--hours', type=int, default=24,
        help='Hours of history to analyze (default: 24)'
    )
    parser.add_argument(
        '--no-ai', action='store_true',
        help='Disable AI validation in Agent 2 and Agent 4'
    )
    parser.add_argument(
        '--report', action='store_true',
        help='Dry run - analyze only, no database writes'
    )
    parser.add_argument(
        '--snapshot', action='store_true',
        help='Save daily health snapshot (for scheduled runs)'
    )
    parser.add_argument(
        '--fix-planner', action='store_true',
        help='Include Agent 4 (Fix Planner) to generate Claude Code prompts'
    )
    parser.add_argument(
        '--fix-limit', type=int, default=5,
        help='Max issues to process in Agent 4 (default: 5)'
    )

    args = parser.parse_args()

    run_pipeline(
        hours=args.hours,
        use_ai=not args.no_ai,
        dry_run=args.report,
        save_snapshot=args.snapshot,
        include_fix_planner=args.fix_planner,
        fix_limit=args.fix_limit
    )


if __name__ == '__main__':
    main()
