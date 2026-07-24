"""
Sentry initialization: error tracking + Crons heartbeat for the reminder pipeline.

No-ops unless SENTRY_DSN is set (and never runs in tests), so local dev and CI
are unaffected. Must be called in every process type, so it's invoked from both
main.py (web) and celery_app.py (worker/beat/monitoring).

The FastAPI and Celery integrations auto-enable when their packages are
installed; no explicit integration list is needed.
"""

import os


def init_sentry():
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn or os.environ.get("ENVIRONMENT") == "test":
        return

    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("ENVIRONMENT", "production"),
        send_default_pii=False,  # never ship phone numbers or message bodies
        traces_sample_rate=0.0,  # errors + crons only, no perf tracing cost
    )
