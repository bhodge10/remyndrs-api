"""
Celery Application Configuration
Uses Upstash Redis as broker and result backend for production-grade task queue.
"""

import os
import ssl
from celery import Celery
from dotenv import load_dotenv

load_dotenv()

# Get Redis URL from environment (Upstash format: rediss://:<password>@<host>:<port>)
REDIS_URL = os.environ.get("UPSTASH_REDIS_URL", "redis://localhost:6379/0")

# Create Celery application
celery_app = Celery(
    "sms_reminders",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["tasks.reminder_tasks", "tasks.monitoring_tasks", "tasks.twilio_tasks"],
)

# SSL configuration for Upstash (uses rediss:// protocol)
ssl_config = {
    "ssl_cert_reqs": ssl.CERT_REQUIRED,
} if REDIS_URL.startswith("rediss://") else None

# Apply configuration
celery_app.conf.update(
    # Task serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,

    # Reliability settings for graceful shutdown
    task_acks_late=True,              # Acknowledge after task completes
    task_reject_on_worker_lost=True,  # Re-queue if worker dies
    worker_prefetch_multiplier=1,     # Fetch one task at a time

    # Queue routing: monitoring tasks go to dedicated 'monitoring' queue
    # All other tasks (reminders) stay on the default 'celery' queue
    task_routes={
        "tasks.monitoring_tasks.*": {"queue": "monitoring"},
    },

    # Result settings
    result_expires=3600,  # Results expire after 1 hour

    # Broker connection settings
    broker_connection_retry_on_startup=True,

    # Upstash closes idle/long-lived connections ("Connection closed by server").
    # Without socket timeouts, a half-open socket makes Beat hang forever without
    # an error (outage of 2026-07-16: beat froze for a week while Render showed
    # the service as live). Timeouts turn the hang into an exception that
    # Celery's retry machinery recovers from; health_check_interval proactively
    # PINGs so dead connections are replaced before they're used.
    broker_transport_options={
        "socket_timeout": 30,
        "socket_connect_timeout": 15,
        "socket_keepalive": True,
        "retry_on_timeout": True,
        "health_check_interval": 25,
    },

    # Same protection for the Redis result backend connections.
    redis_socket_timeout=30,
    redis_socket_connect_timeout=15,
    redis_socket_keepalive=True,
    redis_retry_on_timeout=True,
    redis_backend_health_check_interval=25,

    # On connection loss, cancel acks_late tasks so they're redelivered cleanly
    # (send_single_reminder already guards against duplicate sends via the
    # `sent` flag + row lock). This is also the Celery 6.0 default.
    worker_cancel_long_running_tasks_on_connection_loss=True,

    # Beat scheduler settings
    beat_scheduler="celery.beat:PersistentScheduler",
    beat_schedule_filename="celerybeat-schedule",
)

# Apply SSL config for Upstash
if ssl_config:
    celery_app.conf.update(
        broker_use_ssl=ssl_config,
        redis_backend_use_ssl=ssl_config,
    )

# Load beat schedule from celery_config
celery_app.config_from_object("celery_config")
