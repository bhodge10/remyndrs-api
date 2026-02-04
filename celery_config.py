"""
Celery Beat Schedule Configuration
Defines periodic tasks for reminder checking and monitoring.
"""

from datetime import timedelta
from celery.schedules import crontab

# Beat schedule - periodic tasks
beat_schedule = {
    # ===========================================
    # REMINDER TASKS
    # ===========================================

    # Check for due reminders every 30 seconds
    "check-reminders-every-30-seconds": {
        "task": "tasks.reminder_tasks.check_and_send_reminders",
        "schedule": timedelta(seconds=30),
        "options": {
            "expires": 25,  # Task expires if not picked up in 25 seconds
        },
    },
    # Release stale claims every 5 minutes (handles crashed workers)
    "release-stale-claims": {
        "task": "tasks.reminder_tasks.release_stale_claims_task",
        "schedule": timedelta(minutes=5),
    },
    # Analyze conversations every 4 hours
    "analyze-conversations": {
        "task": "tasks.reminder_tasks.analyze_conversations_task",
        "schedule": timedelta(hours=4),
    },
    # Generate recurring reminders every hour
    "generate-recurring-reminders": {
        "task": "tasks.reminder_tasks.generate_recurring_reminders",
        "schedule": timedelta(hours=1),
    },
    # Send daily summaries every minute (checks for users whose local time matches their preference)
    "send-daily-summaries": {
        "task": "tasks.reminder_tasks.send_daily_summaries",
        "schedule": timedelta(minutes=1),
        "options": {
            "expires": 55,  # Task expires if not picked up in 55 seconds
        },
    },
    # Send abandoned onboarding follow-ups every hour
    "abandoned-onboarding-followups": {
        "task": "tasks.reminder_tasks.send_abandoned_onboarding_followups",
        "schedule": timedelta(hours=1),
    },
    # Check trial expirations and send warnings daily at 9 AM UTC
    "check-trial-expirations": {
        "task": "tasks.reminder_tasks.check_trial_expirations",
        "schedule": crontab(hour=9, minute=0),  # 9:00 AM UTC daily
        "options": {
            "expires": 3600,  # 1 hour expiry
        },
    },
    # Send mid-trial value reminders daily at 10 AM UTC
    "send-mid-trial-value-reminders": {
        "task": "tasks.reminder_tasks.send_mid_trial_value_reminders",
        "schedule": crontab(hour=10, minute=0),  # 10:00 AM UTC daily
        "options": {
            "expires": 3600,  # 1 hour expiry
        },
    },

    # ===========================================
    # MONITORING PIPELINE TASKS (Agent 1 + 2 + 3)
    # ===========================================

    # Quick check for critical issues every hour
    # Catches urgent problems fast without heavy processing
    "monitoring-check-critical": {
        "task": "tasks.monitoring_tasks.check_critical_issues",
        "schedule": timedelta(hours=1),
        "options": {
            "expires": 300,  # 5 minute expiry
        },
    },

    # Run interaction monitor (Agent 1) every 4 hours
    # Detects anomalies in recent user interactions
    "monitoring-agent1-detect": {
        "task": "tasks.monitoring_tasks.run_interaction_monitor",
        "schedule": timedelta(hours=4),
        "args": [24],  # Analyze last 24 hours (duplicates prevented by unique constraint)
        "options": {
            "expires": 1800,  # 30 minute expiry
        },
    },

    # Run issue validator (Agent 2) every 6 hours
    # Validates detected issues, identifies patterns
    # Uses rule-based validation (no AI) for efficiency
    "monitoring-agent2-validate": {
        "task": "tasks.monitoring_tasks.run_issue_validator",
        "schedule": timedelta(hours=6),
        "kwargs": {"limit": 50, "use_ai": False},
        "options": {
            "expires": 1800,
        },
    },

    # Full pipeline with AI validation - once daily at 6 AM UTC
    # More thorough analysis with AI-powered validation
    "monitoring-full-pipeline-daily": {
        "task": "tasks.monitoring_tasks.run_monitoring_pipeline",
        "schedule": crontab(hour=6, minute=0),  # 6:00 AM UTC
        "kwargs": {"hours": 24, "use_ai": True, "save_snapshot": True},
        "options": {
            "expires": 3600,  # 1 hour expiry
        },
    },

    # Run code analyzer (Agent 4) every 8 hours
    # Generates root cause analyses and Claude Code prompts for open issues
    "monitoring-agent4-analyze": {
        "task": "tasks.monitoring_tasks.run_code_analyzer",
        "schedule": timedelta(hours=8),
        "kwargs": {"use_ai": True},
        "options": {
            "expires": 1800,  # 30 minute expiry
        },
    },

    # Save daily health snapshot at midnight UTC
    # Ensures consistent trend data even if pipeline fails
    "monitoring-daily-snapshot": {
        "task": "tasks.monitoring_tasks.save_daily_health_snapshot",
        "schedule": crontab(hour=0, minute=5),  # 12:05 AM UTC
        "options": {
            "expires": 3600,
        },
    },

    # Generate weekly health report - Mondays at 8 AM UTC
    # Comprehensive report with recommendations
    "monitoring-weekly-report": {
        "task": "tasks.monitoring_tasks.generate_weekly_health_report",
        "schedule": crontab(hour=8, minute=0, day_of_week=1),  # Monday 8 AM UTC
        "options": {
            "expires": 3600,
        },
    },
}

# Note: Monitoring tasks are routed to the 'monitoring' queue via task_routes in celery_app.py.
# Reminder tasks remain on the default 'celery' queue.

# Monitoring task schedule summary:
# ─────────────────────────────────────────────────────────────────────
# Task                      | Frequency        | Purpose
# ─────────────────────────────────────────────────────────────────────
# check_critical_issues     | Every 1 hour     | Quick alert for urgent issues
# run_interaction_monitor   | Every 4 hours    | Agent 1: Detect anomalies
# run_issue_validator       | Every 6 hours    | Agent 2: Validate (no AI)
# run_code_analyzer         | Every 8 hours    | Agent 4: Root cause analysis
# run_monitoring_pipeline   | Daily 6 AM UTC   | Full pipeline with AI
# save_daily_health_snapshot| Daily 12:05 AM   | Trend tracking
# generate_weekly_report    | Monday 8 AM UTC  | Weekly summary
# ─────────────────────────────────────────────────────────────────────
