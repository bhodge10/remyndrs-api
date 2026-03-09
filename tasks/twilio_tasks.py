"""
Twilio Tasks
Polls Twilio Usage Records API and monitors failed message delivery.
"""

import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from celery_app import celery_app
from config import logger
from database import get_db_connection, return_db_connection


@celery_app.task(name="tasks.twilio_tasks.poll_twilio_costs")
def poll_twilio_costs():
    """Poll Twilio Usage Records API for yesterday's actual SMS costs.

    Fetches inbound and outbound SMS usage from Twilio's daily usage records,
    then upserts the results into the twilio_costs table. Safe to re-run —
    uses ON CONFLICT to update existing rows.
    """
    # Skip in test environment
    environment = os.environ.get("ENVIRONMENT", "production").lower()
    if environment in ("test", "testing"):
        logger.info("poll_twilio_costs: Skipping in test environment")
        return

    try:
        # Import here to avoid circular imports and to get the initialized client
        from services.sms_service import twilio_client

        if twilio_client is None:
            logger.warning("poll_twilio_costs: Twilio client not initialized, skipping")
            return

        yesterday = date.today() - timedelta(days=1)

        # Fetch inbound SMS usage
        inbound_records = twilio_client.usage.records.daily.list(
            category="sms-inbound",
            start_date=yesterday,
            end_date=yesterday,
        )
        inbound_count = 0
        inbound_cost = 0.0
        for record in inbound_records:
            inbound_count += int(record.count)
            inbound_cost += float(record.price) if record.price else 0.0

        # Fetch outbound SMS usage
        outbound_records = twilio_client.usage.records.daily.list(
            category="sms-outbound",
            start_date=yesterday,
            end_date=yesterday,
        )
        outbound_count = 0
        outbound_cost = 0.0
        for record in outbound_records:
            outbound_count += int(record.count)
            outbound_cost += float(record.price) if record.price else 0.0

        # Twilio reports costs as negative values — use absolute values
        inbound_cost = abs(inbound_cost)
        outbound_cost = abs(outbound_cost)
        total_cost = inbound_cost + outbound_cost

        # Fetch failed + undelivered messages for yesterday to track wasted spend
        yesterday_start = datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=timezone.utc)
        yesterday_end = yesterday_start + timedelta(days=1)
        failed_count = 0
        failed_cost = 0.0
        try:
            for status in ("failed", "undelivered"):
                msgs = twilio_client.messages.list(
                    status=status,
                    date_sent_after=yesterday_start,
                    date_sent_before=yesterday_end,
                )
                for msg in msgs:
                    failed_count += 1
                    if msg.price:
                        failed_cost += abs(float(msg.price))
        except Exception as failed_err:
            logger.warning(f"poll_twilio_costs: Could not fetch failed messages — {failed_err}")

        # Upsert into twilio_costs table
        conn = None
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute('''
                INSERT INTO twilio_costs (cost_date, inbound_count, inbound_cost, outbound_count, outbound_cost, total_cost, failed_count, failed_cost)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (cost_date) DO UPDATE SET
                    inbound_count = EXCLUDED.inbound_count,
                    inbound_cost = EXCLUDED.inbound_cost,
                    outbound_count = EXCLUDED.outbound_count,
                    outbound_cost = EXCLUDED.outbound_cost,
                    total_cost = EXCLUDED.total_cost,
                    failed_count = EXCLUDED.failed_count,
                    failed_cost = EXCLUDED.failed_cost
            ''', (yesterday, inbound_count, inbound_cost, outbound_count, outbound_cost, total_cost, failed_count, failed_cost))
            conn.commit()

            logger.info(
                f"poll_twilio_costs: {yesterday} — "
                f"inbound: {inbound_count} msgs / ${inbound_cost:.4f}, "
                f"outbound: {outbound_count} msgs / ${outbound_cost:.4f}, "
                f"failed: {failed_count} msgs / ${failed_cost:.4f}, "
                f"total: ${total_cost:.4f}"
            )
        finally:
            if conn:
                return_db_connection(conn)

    except Exception as e:
        logger.error(f"poll_twilio_costs: Error polling Twilio usage — {e}")
        raise


@celery_app.task(name="tasks.twilio_tasks.check_failed_messages")
def check_failed_messages():
    """Check Twilio for failed outbound messages in the last 12 hours.

    Groups failures by recipient phone number. Any number with 2+ failures
    is auto-marked as opted_out to prevent further wasted sends. Logs a
    summary of all failures for visibility.
    """
    environment = os.environ.get("ENVIRONMENT", "production").lower()
    if environment in ("test", "testing"):
        logger.info("check_failed_messages: Skipping in test environment")
        return

    try:
        from services.sms_service import twilio_client

        if twilio_client is None:
            logger.warning("check_failed_messages: Twilio client not initialized, skipping")
            return

        since = datetime.now(timezone.utc) - timedelta(hours=12)

        # Fetch failed messages from the last 12 hours
        failed_messages = twilio_client.messages.list(
            status="failed",
            date_sent_after=since,
        )
        # Also check 'undelivered' status (includes carrier rejections)
        undelivered_messages = twilio_client.messages.list(
            status="undelivered",
            date_sent_after=since,
        )

        # Group all failures by recipient phone number
        failures_by_number = defaultdict(list)
        for msg in failed_messages + undelivered_messages:
            error_code = msg.error_code
            failures_by_number[msg.to].append({
                "sid": msg.sid,
                "error_code": error_code,
                "error_message": msg.error_message,
                "date_sent": str(msg.date_sent),
            })

        if not failures_by_number:
            logger.info("check_failed_messages: No failed messages in the last 12 hours")
            return {"failed_numbers": 0, "opted_out": 0}

        logger.warning(
            f"check_failed_messages: Found {sum(len(v) for v in failures_by_number.values())} "
            f"failed messages across {len(failures_by_number)} numbers"
        )

        # Auto-opt-out numbers with 2+ failures
        from models.user import mark_user_opted_out, is_user_opted_out
        opted_out_count = 0

        for phone_number, failures in failures_by_number.items():
            # Log each number's failures
            error_codes = [f.get("error_code") for f in failures]
            logger.warning(
                f"check_failed_messages: {phone_number} — "
                f"{len(failures)} failures, error codes: {error_codes}"
            )

            # Auto-opt-out if 2+ failures and not already opted out
            if len(failures) >= 2:
                try:
                    if not is_user_opted_out(phone_number):
                        mark_user_opted_out(phone_number)
                        opted_out_count += 1
                        logger.warning(
                            f"check_failed_messages: Auto-opted-out {phone_number} "
                            f"({len(failures)} consecutive failures)"
                        )
                except Exception as e:
                    logger.error(f"check_failed_messages: Error opting out {phone_number}: {e}")

        result = {
            "failed_numbers": len(failures_by_number),
            "total_failures": sum(len(v) for v in failures_by_number.values()),
            "opted_out": opted_out_count,
        }
        logger.info(f"check_failed_messages: Complete — {result}")
        return result

    except Exception as e:
        logger.error(f"check_failed_messages: Error — {e}")
        raise
