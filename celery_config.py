"""
Celery Beat Schedule Configuration
Defines periodic tasks for reminder checking and monitoring.
"""

from datetime import timedelta
from celery.schedules import crontab

# Beat schedule - periodic tasks
beat_schedule = {
    # ===========================================
    # KEEP-WARM PING
    # ===========================================
    "keep-web-service-warm": {
        "task": "tasks.reminder_tasks.keep_web_service_warm",
        "schedule": timedelta(minutes=5),
        "options": {
            "expires": 240,  # 4 minute expiry
        },
    },

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
    # Check trial expirations hourly — sends when user's local time is 9-10 AM
    "check-trial-expirations": {
        "task": "tasks.reminder_tasks.check_trial_expirations",
        "schedule": crontab(minute=0),  # Every hour, on the hour
        "options": {
            "expires": 3500,  # Just under 1 hour
        },
    },
    # Send mid-trial value reminders hourly — timezone-aware (9-10 AM local)
    "send-mid-trial-value-reminders": {
        "task": "tasks.reminder_tasks.send_mid_trial_value_reminders",
        "schedule": crontab(minute=5),  # Every hour, at :05
        "options": {
            "expires": 3500,
        },
    },
    # Send Day 3 engagement nudges hourly — timezone-aware (9-10 AM local)
    "send-day-3-engagement-nudges": {
        "task": "tasks.reminder_tasks.send_day_3_engagement_nudges",
        "schedule": crontab(minute=10),  # Every hour, at :10
        "options": {
            "expires": 3500,
        },
    },
    # Send post-trial re-engagement hourly — timezone-aware (9-10 AM local)
    "send-post-trial-reengagement": {
        "task": "tasks.reminder_tasks.send_post_trial_reengagement",
        "schedule": crontab(minute=15),  # Every hour, at :15
        "options": {
            "expires": 3500,
        },
    },
    # Send 14-day post-trial touchpoint hourly — timezone-aware (9-10 AM local)
    "send-14d-post-trial-touchpoint": {
        "task": "tasks.reminder_tasks.send_14d_post_trial_touchpoint",
        "schedule": crontab(minute=20),  # Every hour, at :20
        "options": {
            "expires": 3500,
        },
    },
    # Send 30-day win-back hourly — timezone-aware (9-10 AM local)
    "send-30d-winback": {
        "task": "tasks.reminder_tasks.send_30d_winback",
        "schedule": crontab(minute=25),  # Every hour, at :25
        "options": {
            "expires": 3500,
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
        "args": [4],  # Analyze last 4 hours (matches 4-hour schedule to avoid overlap)
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
