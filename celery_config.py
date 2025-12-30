"""
Celery Beat Schedule Configuration
Defines periodic tasks for reminder checking.
"""

from datetime import timedelta

# Beat schedule - periodic tasks
beat_schedule = {
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
}

# Note: All tasks use the default 'celery' queue for simplicity.
# For future scaling, you can add task_routes to distribute tasks across queues.
