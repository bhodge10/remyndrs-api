"""
AI Analytics Summary Service
Uses Claude API to generate daily analytics summaries with trend analysis
from Google Analytics 4 and Search Console data.
"""

import base64
import json
import logging
from datetime import datetime, timedelta

import psycopg2

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
        error_detail = trends.get("_error", "Unknown error") if isinstance(trends, dict) else "Unknown error"
        return {"error": f"Failed to generate AI summary: {error_detail}"}

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
        return None, {"_error": "anthropic package not installed"}

    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set, cannot generate AI summary")
        return None, {"_error": "ANTHROPIC_API_KEY not set in environment"}

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
        return None, {"_error": f"Claude API call failed: {e}"}


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


_MODEL_ID_BY_CHOICE = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
}

_VALID_OPUS_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _get_latest_raw_data(max_age_hours: int = 1) -> tuple:
    """Return (raw_data, period_days, summary_date, age_hours) for the most recent summary,
    or (None, None, None, None) if none exists or the latest is older than max_age_hours."""
    try:
        with get_db_cursor() as cur:
            cur.execute("""
                SELECT raw_data, period_days, summary_date, created_at
                FROM analytics_summaries
                ORDER BY created_at DESC
                LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return None, None, None, None
            raw_data, period_days, summary_date, created_at = row
            if isinstance(raw_data, str):
                try:
                    raw_data = json.loads(raw_data)
                except (json.JSONDecodeError, TypeError):
                    return None, None, None, None
            age = datetime.utcnow() - (created_at.replace(tzinfo=None) if created_at.tzinfo else created_at)
            age_hours = age.total_seconds() / 3600
            if age_hours > max_age_hours:
                return None, period_days, summary_date, age_hours
            return raw_data, period_days, summary_date, age_hours
    except Exception as e:
        logger.error(f"Error fetching latest raw_data: {e}")
        return None, None, None, None


def answer_analytics_question(
    question: str,
    model_choice: str = "haiku",
    effort: str = "medium",
    period_days: int = 7,
) -> dict:
    """Answer an ad-hoc question about the website analytics data using Claude.

    Args:
        question: Admin's natural language question.
        model_choice: "haiku" | "sonnet" | "opus".
        effort: "low" | "medium" | "high" | "xhigh" | "max". Only applied to Opus.
        period_days: Window used when the raw data needs to be re-pulled.

    Returns dict with keys:
        answer (str), model (str), effort (str|None), data_source (str),
        usage (dict), error (str, only on failure).
    """
    if not question or not question.strip():
        return {"error": "Question is required"}

    model_key = (model_choice or "haiku").lower()
    if model_key not in _MODEL_ID_BY_CHOICE:
        return {"error": f"Invalid model choice '{model_choice}'"}
    model_id = _MODEL_ID_BY_CHOICE[model_key]

    effort_to_send = None
    if model_key == "opus":
        effort_normalized = (effort or "medium").lower()
        if effort_normalized not in _VALID_OPUS_EFFORTS:
            return {"error": f"Invalid effort '{effort}'"}
        effort_to_send = effort_normalized

    # Try to reuse recently stored raw data; fall back to a fresh pull.
    raw_data, stored_period_days, summary_date, age_hours = _get_latest_raw_data(max_age_hours=1)
    data_source = "cache"
    if not raw_data:
        try:
            from services.analytics_service import get_ga4_data, get_search_console_data
        except ImportError as e:
            return {"error": f"Analytics service unavailable: {e}"}
        ga4 = get_ga4_data(period_days)
        if ga4.get("error"):
            return {"error": f"GA4 data unavailable: {ga4['error']}"}
        sc = get_search_console_data(period_days)
        raw_data = {"ga4": ga4, "search_console": sc}
        stored_period_days = period_days
        summary_date = datetime.utcnow().date()
        data_source = "fresh"

    try:
        from anthropic import Anthropic
    except ImportError:
        return {"error": "anthropic package not installed"}

    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"error": "ANTHROPIC_API_KEY not set in environment"}

    client = Anthropic()

    ga4 = raw_data.get("ga4", {})
    sc = raw_data.get("search_console", {})

    data_payload = {
        "period_days": stored_period_days,
        "summary_date": str(summary_date) if summary_date else None,
        "totals": ga4.get("totals", {}),
        "daily_trend": ga4.get("daily_trend", []),
        "traffic_sources": ga4.get("traffic_sources", [])[:10],
        "landing_pages": ga4.get("landing_pages", [])[:10],
        "devices": ga4.get("devices", [])[:5],
        "key_events": ga4.get("key_events", []),
        "ab_variants": ga4.get("ab_variants", []),
        "search_top_queries": sc.get("top_queries", [])[:10] if not sc.get("error") else [],
        "search_top_pages": sc.get("top_pages", [])[:5] if not sc.get("error") else [],
    }

    system_prompt = (
        "You are an analytics expert for Remyndrs, an SMS-based AI memory and reminder service. "
        "Answer the admin's question using the provided website analytics data (Google Analytics 4 + "
        "Search Console). Be concise, cite specific numbers from the data, and flag any limitations "
        "in what the data can tell us. If the data does not contain what the admin asked about, say so."
    )

    user_content = (
        f"DATA (JSON):\n{json.dumps(data_payload, indent=2)}\n\n"
        f"QUESTION: {question.strip()}"
    )

    kwargs = {
        "model": model_id,
        "max_tokens": 2048,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
    }
    if model_key == "opus":
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": effort_to_send}
        # Opus 4.7 can return very long outputs at high effort — give it room.
        if effort_to_send in ("high", "xhigh", "max"):
            kwargs["max_tokens"] = 8192

    try:
        response = client.messages.create(**kwargs)
    except Exception as e:
        logger.error(f"Claude API error answering analytics question: {e}")
        return {"error": f"Claude API call failed: {e}"}

    answer_text = next(
        (b.text for b in response.content if getattr(b, "type", None) == "text"),
        "",
    ).strip()

    usage = getattr(response, "usage", None)
    usage_dict = {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
    } if usage else {}

    return {
        "answer": answer_text,
        "model": model_id,
        "effort": effort_to_send,
        "data_source": data_source,
        "data_age_hours": round(age_hours, 2) if age_hours is not None else None,
        "usage": usage_dict,
    }


# Image attachment policy
MAX_IMAGES_PER_MESSAGE = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB
ALLOWED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
# Only include image BYTES on the last N user turns when calling Claude; older
# turns keep their text so the model remembers the conversation, but the bytes
# are dropped to keep long threads affordable.
RECENT_IMAGE_TURN_LIMIT = 3


def _validate_image(filename: str, mime_type: str, data: bytes) -> str:
    """Return an error string if invalid, or None if OK."""
    if not data:
        return f"{filename or 'attachment'}: empty file"
    if mime_type not in ALLOWED_IMAGE_MIME_TYPES:
        return f"{filename or 'attachment'}: unsupported type '{mime_type}' (allowed: JPEG/PNG/GIF/WEBP)"
    if len(data) > MAX_IMAGE_BYTES:
        return f"{filename or 'attachment'}: too large ({len(data)} bytes; max {MAX_IMAGE_BYTES})"
    return None


def _insert_attachment(message_id: int, filename: str, mime_type: str, data: bytes) -> int:
    with get_db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO analytics_attachments (message_id, filename, mime_type, size_bytes, data)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (message_id, filename, mime_type, len(data), psycopg2.Binary(data)),
        )
        return cur.fetchone()[0]


def _get_attachments_metadata_by_message(message_ids: list) -> dict:
    """Return {message_id: [ {id, filename, mime_type, size_bytes}, ... ]} for the given message ids."""
    if not message_ids:
        return {}
    try:
        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT id, message_id, filename, mime_type, size_bytes
                FROM analytics_attachments
                WHERE message_id = ANY(%s)
                ORDER BY id ASC
                """,
                (list(message_ids),),
            )
            rows = cur.fetchall()
        out = {}
        for r in rows:
            out.setdefault(r[1], []).append({
                "id": r[0],
                "filename": r[2],
                "mime_type": r[3],
                "size_bytes": r[4],
            })
        return out
    except Exception as e:
        logger.error(f"Error fetching attachment metadata: {e}")
        return {}


def get_attachment_bytes(att_id: int) -> tuple:
    """Fetch raw attachment bytes for serving. Returns (bytes, mime_type, filename, message_id) or (None, None, None, None)."""
    try:
        with get_db_cursor() as cur:
            cur.execute(
                "SELECT data, mime_type, filename, message_id FROM analytics_attachments WHERE id = %s",
                (att_id,),
            )
            row = cur.fetchone()
            if not row:
                return None, None, None, None
            data = row[0]
            if isinstance(data, memoryview):
                data = bytes(data)
            return data, row[1], row[2], row[3]
    except Exception as e:
        logger.error(f"Error fetching attachment {att_id}: {e}")
        return None, None, None, None


def _fetch_attachment_data_batch(att_ids: list) -> dict:
    """Return {attachment_id: (bytes, mime_type)} for the given ids."""
    if not att_ids:
        return {}
    try:
        with get_db_cursor() as cur:
            cur.execute(
                "SELECT id, data, mime_type FROM analytics_attachments WHERE id = ANY(%s)",
                (list(att_ids),),
            )
            rows = cur.fetchall()
        out = {}
        for r in rows:
            data = r[1]
            if isinstance(data, memoryview):
                data = bytes(data)
            out[r[0]] = (data, r[2])
        return out
    except Exception as e:
        logger.error(f"Error batch-fetching attachments: {e}")
        return {}


_CONVERSATION_SYSTEM_PROMPT = (
    "You are an analytics expert for Remyndrs, an SMS-based AI memory and reminder service. "
    "You are in an ongoing planning conversation with the admin about website analytics and "
    "growth strategy. Use the provided Google Analytics 4 + Search Console data to answer. "
    "Cite specific numbers when relevant. Be concise but substantive. When the admin asks a "
    "follow-up, remember what you've already discussed and build on it rather than repeating "
    "yourself. If the data does not contain what they asked about, say so plainly."
)


def _build_data_payload(raw_data: dict, period_days, summary_date) -> dict:
    ga4 = raw_data.get("ga4", {})
    sc = raw_data.get("search_console", {})
    return {
        "period_days": period_days,
        "summary_date": str(summary_date) if summary_date else None,
        "totals": ga4.get("totals", {}),
        "daily_trend": ga4.get("daily_trend", []),
        "traffic_sources": ga4.get("traffic_sources", [])[:10],
        "landing_pages": ga4.get("landing_pages", [])[:10],
        "devices": ga4.get("devices", [])[:5],
        "key_events": ga4.get("key_events", []),
        "ab_variants": ga4.get("ab_variants", []),
        "search_top_queries": sc.get("top_queries", [])[:10] if not sc.get("error") else [],
        "search_top_pages": sc.get("top_pages", [])[:5] if not sc.get("error") else [],
    }


def _auto_title(question: str) -> str:
    """Derive a short conversation title from the first user question."""
    text = (question or "").strip().split("\n")[0]
    if len(text) > 60:
        text = text[:57].rstrip() + "..."
    return text or "New conversation"


def create_conversation(admin_user: str = None, title: str = None) -> dict:
    try:
        with get_db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO analytics_conversations (title, admin_user)
                VALUES (%s, %s)
                RETURNING id, title, admin_user, created_at, last_active_at, archived
                """,
                (title or "New conversation", admin_user),
            )
            row = cur.fetchone()
        return {
            "id": row[0],
            "title": row[1],
            "admin_user": row[2],
            "created_at": row[3].isoformat() if row[3] else None,
            "last_active_at": row[4].isoformat() if row[4] else None,
            "archived": row[5],
        }
    except Exception as e:
        logger.error(f"Error creating analytics conversation: {e}")
        return {"error": str(e)}


def list_conversations(include_archived: bool = False, limit: int = 50) -> list:
    try:
        with get_db_cursor() as cur:
            if include_archived:
                cur.execute(
                    """
                    SELECT c.id, c.title, c.admin_user, c.created_at, c.last_active_at, c.archived,
                           (SELECT COUNT(*) FROM analytics_messages m WHERE m.conversation_id = c.id) AS msg_count
                    FROM analytics_conversations c
                    ORDER BY c.last_active_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
            else:
                cur.execute(
                    """
                    SELECT c.id, c.title, c.admin_user, c.created_at, c.last_active_at, c.archived,
                           (SELECT COUNT(*) FROM analytics_messages m WHERE m.conversation_id = c.id) AS msg_count
                    FROM analytics_conversations c
                    WHERE c.archived = FALSE
                    ORDER BY c.last_active_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
            rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "title": r[1],
                "admin_user": r[2],
                "created_at": r[3].isoformat() if r[3] else None,
                "last_active_at": r[4].isoformat() if r[4] else None,
                "archived": r[5],
                "message_count": r[6],
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"Error listing analytics conversations: {e}")
        return []


def get_conversation_with_messages(conv_id: int) -> dict:
    try:
        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT id, title, admin_user, created_at, last_active_at, archived
                FROM analytics_conversations
                WHERE id = %s
                """,
                (conv_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            conv = {
                "id": row[0],
                "title": row[1],
                "admin_user": row[2],
                "created_at": row[3].isoformat() if row[3] else None,
                "last_active_at": row[4].isoformat() if row[4] else None,
                "archived": row[5],
            }
            cur.execute(
                """
                SELECT id, role, content, model, effort, data_source,
                       input_tokens, output_tokens, cache_read_input_tokens, created_at
                FROM analytics_messages
                WHERE conversation_id = %s
                ORDER BY id ASC
                """,
                (conv_id,),
            )
            msgs = cur.fetchall()
        message_ids = [m[0] for m in msgs]
        attachments_by_msg = _get_attachments_metadata_by_message(message_ids)
        conv["messages"] = [
            {
                "id": m[0],
                "role": m[1],
                "content": m[2],
                "model": m[3],
                "effort": m[4],
                "data_source": m[5],
                "input_tokens": m[6],
                "output_tokens": m[7],
                "cache_read_input_tokens": m[8],
                "created_at": m[9].isoformat() if m[9] else None,
                "attachments": attachments_by_msg.get(m[0], []),
            }
            for m in msgs
        ]
        return conv
    except Exception as e:
        logger.error(f"Error fetching analytics conversation {conv_id}: {e}")
        return None


def rename_conversation(conv_id: int, title: str) -> bool:
    title = (title or "").strip()
    if not title:
        return False
    if len(title) > 200:
        title = title[:200]
    try:
        with get_db_cursor() as cur:
            cur.execute(
                "UPDATE analytics_conversations SET title = %s WHERE id = %s",
                (title, conv_id),
            )
            return cur.rowcount > 0
    except Exception as e:
        logger.error(f"Error renaming conversation {conv_id}: {e}")
        return False


def delete_conversation(conv_id: int) -> bool:
    try:
        with get_db_cursor() as cur:
            cur.execute(
                "DELETE FROM analytics_conversations WHERE id = %s",
                (conv_id,),
            )
            return cur.rowcount > 0
    except Exception as e:
        logger.error(f"Error deleting conversation {conv_id}: {e}")
        return False


def _insert_message(
    conv_id: int, role: str, content: str,
    model: str = None, effort: str = None, data_source: str = None,
    input_tokens: int = None, output_tokens: int = None,
    cache_read_input_tokens: int = None,
) -> int:
    with get_db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO analytics_messages
                (conversation_id, role, content, model, effort, data_source,
                 input_tokens, output_tokens, cache_read_input_tokens)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (conv_id, role, content, model, effort, data_source,
             input_tokens, output_tokens, cache_read_input_tokens),
        )
        return cur.fetchone()[0]


def send_message_in_conversation(
    conv_id: int,
    question: str,
    model_choice: str = "haiku",
    effort: str = "medium",
    period_days: int = 7,
    image_files: list = None,
) -> dict:
    """Append a user message to a conversation, call Claude with full history, store the reply.

    image_files: optional list of dicts with keys {filename, mime_type, data (bytes)} — attached
    to the outgoing user message.

    Returns dict with keys:
        user_message (dict), assistant_message (dict), conversation (dict), error (on failure).
    """
    question = (question or "").strip()
    image_files = image_files or []

    # Allow image-only messages (common claude.ai pattern: drop an image, ask "thoughts?")
    if not question and not image_files:
        return {"error": "Question or at least one image is required"}
    if len(question) > 4000:
        return {"error": "Question too long (max 4000 chars)"}

    if len(image_files) > MAX_IMAGES_PER_MESSAGE:
        return {"error": f"Too many images (max {MAX_IMAGES_PER_MESSAGE} per message)"}
    for f in image_files:
        err = _validate_image(f.get("filename"), f.get("mime_type"), f.get("data"))
        if err:
            return {"error": err}

    model_key = (model_choice or "haiku").lower()
    if model_key not in _MODEL_ID_BY_CHOICE:
        return {"error": f"Invalid model choice '{model_choice}'"}
    model_id = _MODEL_ID_BY_CHOICE[model_key]

    effort_to_send = None
    if model_key == "opus":
        effort_normalized = (effort or "medium").lower()
        if effort_normalized not in _VALID_OPUS_EFFORTS:
            return {"error": f"Invalid effort '{effort}'"}
        effort_to_send = effort_normalized

    conv = get_conversation_with_messages(conv_id)
    if not conv:
        return {"error": "Conversation not found"}

    # Fetch data — wider cache window for threaded chat so caching pays off across a planning session
    raw_data, stored_period_days, summary_date, age_hours = _get_latest_raw_data(max_age_hours=24)
    data_source = "cache"
    if not raw_data:
        try:
            from services.analytics_service import get_ga4_data, get_search_console_data
        except ImportError as e:
            return {"error": f"Analytics service unavailable: {e}"}
        ga4 = get_ga4_data(period_days)
        if ga4.get("error"):
            return {"error": f"GA4 data unavailable: {ga4['error']}"}
        sc = get_search_console_data(period_days)
        raw_data = {"ga4": ga4, "search_console": sc}
        stored_period_days = period_days
        summary_date = datetime.utcnow().date()
        data_source = "fresh"

    try:
        from anthropic import Anthropic
    except ImportError:
        return {"error": "anthropic package not installed"}

    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"error": "ANTHROPIC_API_KEY not set in environment"}

    client = Anthropic()

    data_payload = _build_data_payload(raw_data, stored_period_days, summary_date)

    # System = stable instructions + data snapshot. Cache-control marker here makes everything
    # up through system cacheable across turns — a win once the thread has 2+ messages.
    system = [
        {"type": "text", "text": _CONVERSATION_SYSTEM_PROMPT},
        {
            "type": "text",
            "text": f"ANALYTICS DATA (JSON):\n{json.dumps(data_payload, indent=2)}",
            "cache_control": {"type": "ephemeral"},
        },
    ]

    # Decide which prior user turns should carry image bytes. Keep bytes on the most recent
    # RECENT_IMAGE_TURN_LIMIT user turns (counting the new one being sent); older user turns
    # drop image bytes but keep their text for conversational continuity.
    prior_user_turns_with_images = []
    for m in conv["messages"]:
        if m["role"] == "user" and m.get("attachments"):
            prior_user_turns_with_images.append(m["id"])
    # Current turn occupies one slot
    slots_for_prior = max(0, RECENT_IMAGE_TURN_LIMIT - 1)
    include_image_ids_for_messages = set(prior_user_turns_with_images[-slots_for_prior:]) if slots_for_prior else set()

    # Batch-fetch bytes for the attachments we actually need
    att_ids_to_fetch = []
    for m in conv["messages"]:
        if m["id"] in include_image_ids_for_messages:
            att_ids_to_fetch.extend([a["id"] for a in m.get("attachments", [])])
    attachment_bytes = _fetch_attachment_data_batch(att_ids_to_fetch)

    messages = []
    for m in conv["messages"]:
        if m["role"] == "user" and m["id"] in include_image_ids_for_messages and m.get("attachments"):
            content = []
            for att in m["attachments"]:
                entry = attachment_bytes.get(att["id"])
                if not entry:
                    continue
                data, mime = entry
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                })
            if m["content"]:
                content.append({"type": "text", "text": m["content"]})
            messages.append({"role": "user", "content": content or [{"type": "text", "text": ""}]})
        else:
            messages.append({"role": m["role"], "content": m["content"]})

    # Build the new user message content (current turn includes its images)
    new_user_content = []
    for f in image_files:
        new_user_content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": f["mime_type"],
                "data": base64.b64encode(f["data"]).decode("ascii"),
            },
        })
    if question:
        new_user_content.append({"type": "text", "text": question})
    if len(new_user_content) == 1 and new_user_content[0]["type"] == "text":
        # Collapse single-text content to a string for API simplicity
        messages.append({"role": "user", "content": question})
    else:
        messages.append({"role": "user", "content": new_user_content})

    kwargs = {
        "model": model_id,
        "max_tokens": 2048,
        "system": system,
        "messages": messages,
    }
    if model_key == "opus":
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": effort_to_send}
        if effort_to_send in ("high", "xhigh", "max"):
            kwargs["max_tokens"] = 8192

    try:
        response = client.messages.create(**kwargs)
    except Exception as e:
        logger.error(f"Claude API error in conversation {conv_id}: {e}")
        return {"error": f"Claude API call failed: {e}"}

    answer_text = next(
        (b.text for b in response.content if getattr(b, "type", None) == "text"),
        "",
    ).strip()
    if not answer_text:
        return {"error": "Empty response from Claude"}

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None) if usage else None
    output_tokens = getattr(usage, "output_tokens", None) if usage else None
    cache_read = getattr(usage, "cache_read_input_tokens", None) if usage else None

    # Persist both turns and bump last_active_at. Auto-title on first user turn.
    stored_attachments = []
    try:
        with get_db_cursor() as cur:
            is_first = len(conv["messages"]) == 0
            user_msg_id = _insert_message(
                conv_id, "user", question,
                model=model_id, effort=effort_to_send, data_source=data_source,
            )
            for f in image_files:
                att_id = _insert_attachment(user_msg_id, f.get("filename"), f["mime_type"], f["data"])
                stored_attachments.append({
                    "id": att_id,
                    "filename": f.get("filename"),
                    "mime_type": f["mime_type"],
                    "size_bytes": len(f["data"]),
                })
            assistant_msg_id = _insert_message(
                conv_id, "assistant", answer_text,
                model=model_id, effort=effort_to_send, data_source=data_source,
                input_tokens=input_tokens, output_tokens=output_tokens,
                cache_read_input_tokens=cache_read,
            )
            if is_first and (conv["title"] in (None, "", "New conversation")):
                title_source = question if question else (image_files[0].get("filename") or "Image review")
                cur.execute(
                    "UPDATE analytics_conversations SET title = %s, last_active_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (_auto_title(title_source), conv_id),
                )
            else:
                cur.execute(
                    "UPDATE analytics_conversations SET last_active_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (conv_id,),
                )
    except Exception as e:
        logger.error(f"Error persisting conversation {conv_id} messages: {e}")
        return {"error": f"Failed to save messages: {e}"}

    return {
        "user_message": {
            "id": user_msg_id,
            "role": "user",
            "content": question,
            "model": model_id,
            "effort": effort_to_send,
            "attachments": stored_attachments,
        },
        "assistant_message": {
            "id": assistant_msg_id,
            "role": "assistant",
            "content": answer_text,
            "model": model_id,
            "effort": effort_to_send,
            "data_source": data_source,
            "data_age_hours": round(age_hours, 2) if age_hours is not None else None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
        },
    }


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
