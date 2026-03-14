"""
AI Analytics Summary Service
Uses Claude API to generate daily analytics summaries with trend analysis
from Google Analytics 4 and Search Console data.
"""

import json
import logging
from datetime import datetime, timedelta

from database import get_db_cursor

logger = logging.getLogger(__name__)


def generate_analytics_summary(period_days: int = 7) -> dict:
    """
    Pull GA4 + Search Console data for current and previous periods,
    send to Claude API for analysis, and store the summary.

    Returns dict with summary_text, trends, and metrics_snapshot.
    """
    from services.analytics_service import get_ga4_data, get_search_console_data

    # Pull current period data
    current_ga4 = get_ga4_data(period_days)
    current_sc = get_search_console_data(period_days)

    if current_ga4.get("error"):
        return {"error": f"GA4 data unavailable: {current_ga4['error']}"}

    # Pull previous period for comparison
    previous_ga4 = get_ga4_data(period_days * 2)

    # Build metrics snapshot with period-over-period comparison
    metrics_snapshot = _build_metrics_snapshot(current_ga4, previous_ga4, period_days)

    # Generate AI summary
    summary_text, trends = _generate_claude_summary(
        current_ga4, current_sc, metrics_snapshot, period_days
    )

    if not summary_text:
        return {"error": "Failed to generate AI summary"}

    # Store in database
    summary_date = datetime.utcnow().date()
    raw_data = {
        "ga4": current_ga4,
        "search_console": current_sc,
    }

    _store_summary(
        summary_date=summary_date,
        period_days=period_days,
        raw_data=raw_data,
        summary_text=summary_text,
        trends=trends,
        metrics_snapshot=metrics_snapshot,
    )

    return {
        "summary_date": str(summary_date),
        "period_days": period_days,
        "summary_text": summary_text,
        "trends": trends,
        "metrics_snapshot": metrics_snapshot,
    }


def get_latest_summary() -> dict | None:
    """Get the most recent analytics summary."""
    try:
        with get_db_cursor() as cur:
            cur.execute("""
                SELECT id, summary_date, period_days, summary_text, trends,
                       metrics_snapshot, created_at
                FROM analytics_summaries
                ORDER BY summary_date DESC, created_at DESC
                LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return None
            return _row_to_dict(row)
    except Exception as e:
        logger.error(f"Error fetching latest summary: {e}")
        return None


def get_summary_history(limit: int = 30) -> list:
    """Get historical analytics summaries."""
    try:
        with get_db_cursor() as cur:
            cur.execute("""
                SELECT id, summary_date, period_days, summary_text, trends,
                       metrics_snapshot, created_at
                FROM analytics_summaries
                ORDER BY summary_date DESC, created_at DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            return [_row_to_dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error fetching summary history: {e}")
        return []


def _row_to_dict(row) -> dict:
    """Convert a database row to a summary dict."""
    trends = row[4]
    metrics = row[5]

    # Handle JSONB — psycopg2 auto-deserializes, but handle strings just in case
    if isinstance(trends, str):
        try:
            trends = json.loads(trends)
        except (json.JSONDecodeError, TypeError):
            trends = {}
    if isinstance(metrics, str):
        try:
            metrics = json.loads(metrics)
        except (json.JSONDecodeError, TypeError):
            metrics = {}

    return {
        "id": row[0],
        "summary_date": str(row[1]),
        "period_days": row[2],
        "summary_text": row[3],
        "trends": trends or {},
        "metrics_snapshot": metrics or {},
        "created_at": row[6].isoformat() if row[6] else None,
    }


def _build_metrics_snapshot(current_ga4: dict, previous_ga4: dict, period_days: int) -> dict:
    """Build a metrics snapshot with period-over-period changes."""
    current_totals = current_ga4.get("totals", {})
    previous_totals = previous_ga4.get("totals", {})

    def pct_change(current_val, previous_val):
        """Calculate percentage change. Previous period is double the range,
        so we estimate the first half by subtracting current from it."""
        prev_estimate = max(previous_val - current_val, 0)
        if prev_estimate == 0:
            return 0 if current_val == 0 else 100.0
        return round(((current_val - prev_estimate) / prev_estimate) * 100, 1)

    snapshot = {
        "users": {
            "current": current_totals.get("total_users", 0),
            "change_pct": pct_change(
                current_totals.get("total_users", 0),
                previous_totals.get("total_users", 0)
            ),
        },
        "sessions": {
            "current": current_totals.get("total_sessions", 0),
            "change_pct": pct_change(
                current_totals.get("total_sessions", 0),
                previous_totals.get("total_sessions", 0)
            ),
        },
        "engagement_rate": {
            "current": current_totals.get("engagement_rate", 0),
            "change_pct": round(
                current_totals.get("engagement_rate", 0)
                - (previous_totals.get("engagement_rate", 0) if previous_totals else 0),
                1
            ),
        },
        "avg_engagement_time": {
            "current": current_totals.get("avg_engagement_time", 0),
            "change_pct": pct_change(
                current_totals.get("avg_engagement_time", 0),
                previous_totals.get("avg_engagement_time", 0)
            ),
        },
    }

    # Top traffic sources
    sources = current_ga4.get("traffic_sources", [])
    if sources:
        snapshot["top_source"] = sources[0].get("source_medium", "unknown")
        snapshot["top_source_sessions"] = sources[0].get("sessions", 0)

    # Top search query
    # (search console data is passed separately, but we include GA4 source info here)

    return snapshot


def _generate_claude_summary(
    ga4_data: dict, sc_data: dict, metrics_snapshot: dict, period_days: int
) -> tuple:
    """
    Use Claude API to generate a natural language summary and identify trends.

    Returns (summary_text, trends_dict) or (None, None) on failure.
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        logger.warning("anthropic package not installed, cannot generate AI summary")
        return None, None

    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set, cannot generate AI summary")
        return None, None

    client = Anthropic()

    # Build concise data payload for Claude
    totals = ga4_data.get("totals", {})
    sources = ga4_data.get("traffic_sources", [])[:10]
    landing_pages = ga4_data.get("landing_pages", [])[:5]
    daily_trend = ga4_data.get("daily_trend", [])
    devices = ga4_data.get("devices", [])[:5]
    key_events = ga4_data.get("key_events", [])
    ab_variants = ga4_data.get("ab_variants", [])
    sc_queries = sc_data.get("top_queries", [])[:10] if not sc_data.get("error") else []
    sc_pages = sc_data.get("top_pages", [])[:5] if not sc_data.get("error") else []

    data_summary = f"""
WEBSITE ANALYTICS DATA — Last {period_days} days (as of {datetime.utcnow().strftime('%Y-%m-%d')})

TOTALS:
- Users: {totals.get('total_users', 0)} ({metrics_snapshot['users']['change_pct']:+.1f}% vs previous period)
- Sessions: {totals.get('total_sessions', 0)} ({metrics_snapshot['sessions']['change_pct']:+.1f}% vs previous period)
- Engagement Rate: {totals.get('engagement_rate', 0)}% ({metrics_snapshot['engagement_rate']['change_pct']:+.1f}pp vs previous period)
- Avg Engagement Time: {totals.get('avg_engagement_time', 0):.0f}s ({metrics_snapshot['avg_engagement_time']['change_pct']:+.1f}% vs previous period)

DAILY TREND (date -> sessions, users):
{json.dumps(daily_trend, indent=2) if daily_trend else 'No data'}

TOP TRAFFIC SOURCES:
{json.dumps(sources, indent=2) if sources else 'No data'}

TOP LANDING PAGES:
{json.dumps(landing_pages, indent=2) if landing_pages else 'No data'}

DEVICES:
{json.dumps(devices, indent=2) if devices else 'No data'}

ENGAGEMENT EVENTS:
{json.dumps(key_events, indent=2) if key_events else 'No data'}

A/B TEST VARIANTS:
{json.dumps(ab_variants, indent=2) if ab_variants else 'No data'}

SEARCH CONSOLE - TOP QUERIES:
{json.dumps(sc_queries, indent=2) if sc_queries else 'No data'}

SEARCH CONSOLE - TOP PAGES:
{json.dumps(sc_pages, indent=2) if sc_pages else 'No data'}
"""

    prompt = f"""You are an analytics expert for Remyndrs, an SMS-based AI memory and reminder service.
Analyze the following website analytics data and provide:

1. A concise executive summary (3-5 sentences) of overall performance
2. Key highlights (what's working well)
3. Areas of concern (what needs attention)
4. Actionable recommendations (1-3 specific suggestions)

Also identify trends by returning a JSON object with this structure:
{{
    "direction": "up" | "down" | "stable",
    "confidence": "high" | "medium" | "low",
    "key_trends": ["trend 1 description", "trend 2 description", ...],
    "notable_changes": ["change 1", "change 2", ...]
}}

Format your response as:
---SUMMARY---
(your summary here)
---TRENDS_JSON---
(your trends JSON here)

{data_summary}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = response.content[0].text.strip()

        # Parse response into summary and trends
        summary_text, trends = _parse_claude_response(response_text)
        return summary_text, trends

    except Exception as e:
        logger.error(f"Claude API error generating analytics summary: {e}")
        return None, None


def _parse_claude_response(response_text: str) -> tuple:
    """Parse Claude's response into summary text and trends JSON."""
    summary_text = response_text
    trends = {}

    if "---SUMMARY---" in response_text and "---TRENDS_JSON---" in response_text:
        parts = response_text.split("---TRENDS_JSON---")
        summary_part = parts[0].replace("---SUMMARY---", "").strip()
        trends_part = parts[1].strip() if len(parts) > 1 else ""

        summary_text = summary_part

        # Extract JSON from trends part (handle markdown code blocks)
        if trends_part:
            # Remove markdown code fences if present
            trends_part = trends_part.strip()
            if trends_part.startswith("```"):
                lines = trends_part.split("\n")
                # Remove first and last lines (``` markers)
                lines = [l for l in lines if not l.strip().startswith("```")]
                trends_part = "\n".join(lines)

            try:
                trends = json.loads(trends_part)
            except json.JSONDecodeError:
                logger.warning(f"Could not parse trends JSON: {trends_part[:200]}")
                trends = {"parse_error": True, "raw": trends_part[:500]}
    else:
        # If Claude didn't follow the format, use whole response as summary
        trends = {"direction": "unknown", "confidence": "low", "key_trends": [], "notable_changes": []}

    return summary_text, trends


def _store_summary(
    summary_date, period_days: int, raw_data: dict,
    summary_text: str, trends: dict, metrics_snapshot: dict
):
    """Store analytics summary in the database, upserting by date+period."""
    try:
        with get_db_cursor() as cur:
            cur.execute("""
                INSERT INTO analytics_summaries
                    (summary_date, period_days, raw_data, summary_text, trends, metrics_snapshot)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (summary_date, period_days)
                DO UPDATE SET
                    raw_data = EXCLUDED.raw_data,
                    summary_text = EXCLUDED.summary_text,
                    trends = EXCLUDED.trends,
                    metrics_snapshot = EXCLUDED.metrics_snapshot,
                    created_at = CURRENT_TIMESTAMP
            """, (
                summary_date,
                period_days,
                json.dumps(raw_data),
                summary_text,
                json.dumps(trends),
                json.dumps(metrics_snapshot),
            ))
        logger.info(f"Stored analytics summary for {summary_date} ({period_days}d period)")
    except Exception as e:
        logger.error(f"Error storing analytics summary: {e}")
