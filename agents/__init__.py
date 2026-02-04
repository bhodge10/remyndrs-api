"""
Multi-Agent Monitoring System for Remyndrs

A lightweight pipeline for detecting, validating, tracking, and analyzing issues
in user interactions. Designed for small teams with limited resources.

AGENTS:
=======

Agent 1: Interaction Monitor (interaction_monitor.py)
    Detects anomalies in user SMS interactions using pattern matching.
    - Queries recent logs for: confusion, errors, failures, timezone issues
    - Detects repeated attempts, delivery failures, parsing problems
    - Stores findings in monitoring_issues table
    - Run: python -m agents.interaction_monitor

Agent 2: Issue Validator (issue_validator.py)
    Validates issues and identifies root cause patterns.
    - Filters false positives using rules + optional AI
    - Groups similar issues into patterns
    - Adjusts severity based on context (premium users, repeats)
    - Run: python -m agents.issue_validator

Agent 3: Resolution Tracker (resolution_tracker.py)
    Tracks resolutions and measures system health over time.
    - Marks issues as resolved with resolution type
    - Calculates health score (0-100) based on issue rate
    - Detects regressions (fixed patterns that recur)
    - Generates weekly health reports
    - Run: python -m agents.resolution_tracker

Agent 4: Code Analyzer (code_analyzer.py)
    Analyzes issues and generates actionable fix prompts.
    - Maps issue types to likely code locations
    - Identifies root causes using rules + optional AI
    - Generates Claude Code prompts for fixes
    - Dashboard integration for one-click analysis
    - Run: python -m agents.code_analyzer

PIPELINE:
=========
Run all four agents in sequence:
    python agents/run_pipeline.py
    python agents/run_pipeline.py --hours 48 --snapshot  # With daily snapshot
    python agents/run_pipeline.py --fix-planner          # Include Agent 4

Quick runners:
    python agents/run_monitor.py       # Agent 1 only
    python agents/run_validator.py     # Agent 2 only
    python agents/run_tracker.py       # Agent 3 dashboard
    python -m agents.code_analyzer     # Agent 4 only

Scheduled job (recommended daily):
    python agents/run_pipeline.py --snapshot

API ENDPOINTS:
==============
Agent 1 - Monitoring:
    GET  /admin/monitoring/run           - Run interaction monitor
    GET  /admin/monitoring/issues        - View detected issues
    GET  /admin/monitoring/stats         - Monitoring statistics
    POST /admin/monitoring/issues/{id}/validate - Validate an issue

Agent 2 - Validator:
    GET  /admin/validator/run            - Run issue validator
    GET  /admin/validator/patterns       - View issue patterns
    GET  /admin/validator/stats          - Validation statistics

Agent 3 - Tracker:
    GET  /admin/tracker/health           - Current health metrics
    GET  /admin/tracker/open             - Open issues list
    GET  /admin/tracker/report           - Weekly health report
    GET  /admin/tracker/trends           - 30-day health trend
    POST /admin/tracker/resolve/{id}     - Resolve an issue
    POST /admin/tracker/snapshot         - Save daily snapshot
    GET  /admin/tracker/resolution-types - Available resolution types

Agent 4 - Code Analyzer:
    GET  /admin/analyzer/issue/{id}      - Get/generate analysis for issue
    GET  /admin/analyzer/pattern/{id}    - Get/generate analysis for pattern
    GET  /admin/analyzer/run             - Run code analyzer on unanalyzed issues
    POST /admin/analyzer/{id}/applied    - Mark analysis fix as applied
    GET  /admin/analyzer/stats           - Code analyzer statistics

DATABASE TABLES:
================
Agent 1:
    monitoring_issues    - Detected anomalies
    monitoring_runs      - Run audit trail

Agent 2:
    issue_patterns       - Grouped root cause patterns
    issue_pattern_links  - Links issues to patterns
    validation_runs      - Validation audit trail

Agent 3:
    issue_resolutions    - How issues were resolved
    pattern_resolutions  - How patterns were addressed
    health_snapshots     - Daily health metrics

Agent 4:
    code_analysis        - Root cause analyses and Claude prompts
    code_analysis_runs   - Analysis run audit trail

RESOLUTION TYPES:
=================
    code_fix        🔧  Bug fix or code improvement
    prompt_update   🤖  AI prompt or template update
    config_change   ⚙️  Threshold or setting adjustment
    documentation   📝  Help text improvement
    user_education  💡  User communication
    wont_fix        🚫  Accepted behavior
    duplicate       📋  Already tracked
    cannot_reproduce ❓ Unable to reproduce
    auto_resolved   ✅ Issue type stopped appearing in interactions
"""

# Convenience imports
from agents.interaction_monitor import analyze_interactions, get_pending_issues
from agents.issue_validator import validate_issues, analyze_patterns
from agents.resolution_tracker import calculate_health_metrics, resolve_issue, generate_weekly_report, auto_resolve_stale_issues
from agents.code_analyzer import analyze_issue, run_code_analysis, get_existing_analysis
