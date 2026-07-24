"""
Sentry initialization: error tracking + Crons heartbeat for the reminder pipeline.

No-ops unless SENTRY_DSN is set (and never runs in tests), so local dev and CI
are unaffected. Must be called in every process type, so it's invoked from both
main.py (web) and celery_app.py (worker/beat/monitoring).

The FastAPI and Celery integrations auto-enable when their packages are
installed; no explicit integration list is needed.

IMPORTANT: this must never take the app down. A malformed SENTRY_DSN crashed
the 2026-07-23 web deploy at import time (BadDsn: Unsupported scheme '') —
init failures now degrade to a logged warning and the app runs without Sentry.
"""

import logging
import os

logger = logging.getLogger(__name__)


def init_sentry():
    # Strip whitespace and stray quotes from copy/paste into the env editor.
    dsn = (os.environ.get("SENTRY_DSN") or "").strip().strip("'\"")
    if not dsn or os.environ.get("ENVIRONMENT") == "test":
        return

    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get("ENVIRONMENT", "production"),
            send_default_pii=False,  # never ship phone numbers or message bodies
            traces_sample_rate=0.0,  # errors + crons only, no perf tracing cost
        )
        logger.info("Sentry initialized")
    except Exception as e:
        logger.warning(f"Sentry disabled — init failed: {e}")
