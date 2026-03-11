"""
Google Analytics 4 and Search Console data service.
Pulls website analytics data for the admin dashboard.
"""

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


def _get_credentials_path():
    """Get credentials file path, creating a temp file from env var if needed."""
    from config import GA_CREDENTIALS_PATH

    # Option 1: Direct file path
    if GA_CREDENTIALS_PATH and os.path.isfile(GA_CREDENTIALS_PATH):
        return GA_CREDENTIALS_PATH

    # Option 2: JSON string in environment variable
    creds_json = os.environ.get("GA_CREDENTIALS_JSON")
    if creds_json:
        try:
            # Validate it's real JSON
            json.loads(creds_json)
            # Write to a temp file (persists for process lifetime)
            tmp = tempfile.NamedTemporaryFile(
                mode='w', suffix='.json', delete=False, prefix='ga_creds_'
            )
            tmp.write(creds_json)
            tmp.close()
            return tmp.name
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Invalid GA_CREDENTIALS_JSON: {e}")
            return None

    return None


# Cache the resolved path so we only write the temp file once
_resolved_creds_path = None


def _get_creds_path():
    """Get cached credentials path."""
    global _resolved_creds_path
    if _resolved_creds_path is None:
        _resolved_creds_path = _get_credentials_path() or ""
    return _resolved_creds_path or None

# Simple in-memory cache
_cache = {}
_CACHE_TTL = 3600  # 1 hour


def _get_cache(key: str):
    """Get cached data if still valid."""
    if key in _cache:
        data, timestamp = _cache[key]
        if time.time() - timestamp < _CACHE_TTL:
            return data
        del _cache[key]
    return None


def _set_cache(key: str, data):
    """Store data in cache."""
    _cache[key] = (data, time.time())


def _get_ga4_client():
    """Create GA4 Data API client."""
    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.oauth2 import service_account

        creds_path = _get_creds_path()
        if not creds_path:
            return None

        credentials = service_account.Credentials.from_service_account_file(
            creds_path,
            scopes=["https://www.googleapis.com/auth/analytics.readonly"]
        )
        return BetaAnalyticsDataClient(credentials=credentials)
    except Exception as e:
        logger.error(f"Failed to create GA4 client: {e}")
        return None


def _get_search_console_service():
    """Create Search Console API service."""
    try:
        from googleapiclient.discovery import build
        from google.oauth2 import service_account

        creds_path = _get_creds_path()
        if not creds_path:
            return None

        credentials = service_account.Credentials.from_service_account_file(
            creds_path,
            scopes=["https://www.googleapis.com/auth/webmasters.readonly"]
        )
        return build("searchconsole", "v1", credentials=credentials)
    except Exception as e:
        logger.error(f"Failed to create Search Console service: {e}")
        return None


def get_ga4_data(days: int = 7) -> dict:
    """Fetch all GA4 metrics for the given date range."""
    from config import GA4_PROPERTY_ID

    if not GA4_PROPERTY_ID:
        return {"error": "GA4_PROPERTY_ID not configured"}

    cache_key = f"ga4_{days}"
    cached = _get_cache(cache_key)
    if cached:
        return cached

    client = _get_ga4_client()
    if not client:
        return {"error": "GA4 client not available. Check GA_CREDENTIALS_PATH."}

    try:
        from google.analytics.data_v1beta.types import (
            RunReportRequest, DateRange, Dimension, Metric, FilterExpression,
            Filter, InListFilter
        )

        property_id = f"properties/{GA4_PROPERTY_ID}"
        date_range = DateRange(start_date=f"{days}daysAgo", end_date="today")

        result = {}

        # 1. Traffic by source/medium
        response = client.run_report(RunReportRequest(
            property=property_id,
            date_ranges=[date_range],
            dimensions=[Dimension(name="sessionSourceMedium")],
            metrics=[
                Metric(name="sessions"),
                Metric(name="totalUsers"),
                Metric(name="engagedSessions"),
                Metric(name="engagementRate"),
                Metric(name="averageSessionDuration"),
            ],
            order_bys=[{"metric": {"metric_name": "sessions"}, "desc": True}],
            limit=20
        ))
        result["traffic_sources"] = _parse_report(
            response,
            ["source_medium"],
            ["sessions", "users", "engaged_sessions", "engagement_rate", "avg_engagement_time"]
        )

        # 2. Landing page performance
        response = client.run_report(RunReportRequest(
            property=property_id,
            date_ranges=[date_range],
            dimensions=[Dimension(name="landingPage")],
            metrics=[
                Metric(name="sessions"),
                Metric(name="totalUsers"),
                Metric(name="engagementRate"),
                Metric(name="averageSessionDuration"),
            ],
            order_bys=[{"metric": {"metric_name": "sessions"}, "desc": True}],
            limit=20
        ))
        result["landing_pages"] = _parse_report(
            response,
            ["page"],
            ["sessions", "users", "engagement_rate", "avg_engagement_time"]
        )

        # 3. A/B variant performance
        try:
            response = client.run_report(RunReportRequest(
                property=property_id,
                date_ranges=[date_range],
                dimensions=[
                    Dimension(name="customEvent:variant"),
                ],
                metrics=[
                    Metric(name="eventCount"),
                    Metric(name="totalUsers"),
                ],
                dimension_filter=FilterExpression(
                    filter=Filter(
                        field_name="eventName",
                        string_filter=Filter.StringFilter(value="ab_variant_view")
                    )
                ),
            ))
            result["ab_variants"] = _parse_report(
                response,
                ["variant"],
                ["event_count", "users"]
            )
        except Exception as e:
            logger.warning(f"A/B variant data not available: {e}")
            result["ab_variants"] = []

        # 4. Device breakdown
        response = client.run_report(RunReportRequest(
            property=property_id,
            date_ranges=[date_range],
            dimensions=[
                Dimension(name="deviceCategory"),
                Dimension(name="operatingSystem"),
            ],
            metrics=[
                Metric(name="sessions"),
                Metric(name="totalUsers"),
            ],
            order_bys=[{"metric": {"metric_name": "sessions"}, "desc": True}],
            limit=20
        ))
        result["devices"] = _parse_report(
            response,
            ["device_category", "operating_system"],
            ["sessions", "users"]
        )

        # 5. Key events summary
        try:
            response = client.run_report(RunReportRequest(
                property=property_id,
                date_ranges=[date_range],
                dimensions=[Dimension(name="eventName")],
                metrics=[
                    Metric(name="eventCount"),
                    Metric(name="totalUsers"),
                ],
                dimension_filter=FilterExpression(
                    filter=Filter(
                        field_name="eventName",
                        in_list_filter=InListFilter(
                            values=["scroll_depth", "persona_select", "demo_interaction", "android_form_mode"]
                        )
                    )
                ),
            ))
            result["key_events"] = _parse_report(
                response,
                ["event_name"],
                ["event_count", "users"]
            )
        except Exception as e:
            logger.warning(f"Key events data not available: {e}")
            result["key_events"] = []

        # 6. Daily traffic trend
        response = client.run_report(RunReportRequest(
            property=property_id,
            date_ranges=[date_range],
            dimensions=[Dimension(name="date")],
            metrics=[
                Metric(name="sessions"),
                Metric(name="totalUsers"),
            ],
            order_bys=[{"dimension": {"dimension_name": "date"}}],
        ))
        result["daily_trend"] = _parse_report(
            response,
            ["date"],
            ["sessions", "users"]
        )

        # 7. Totals for summary cards
        response = client.run_report(RunReportRequest(
            property=property_id,
            date_ranges=[date_range],
            metrics=[
                Metric(name="totalUsers"),
                Metric(name="sessions"),
                Metric(name="averageSessionDuration"),
                Metric(name="engagementRate"),
            ],
        ))
        if response.rows:
            row = response.rows[0]
            result["totals"] = {
                "total_users": int(row.metric_values[0].value),
                "total_sessions": int(row.metric_values[1].value),
                "avg_engagement_time": round(float(row.metric_values[2].value), 1),
                "engagement_rate": round(float(row.metric_values[3].value) * 100, 1),
            }
        else:
            result["totals"] = {
                "total_users": 0, "total_sessions": 0,
                "avg_engagement_time": 0, "engagement_rate": 0,
            }

        # A/B landing page engagement (for side-by-side comparison)
        try:
            ab_pages = [r for r in result["landing_pages"]
                        if r.get("page") in ["/", "/index.html", "/index-b.html"]]
            result["ab_landing_pages"] = ab_pages
        except Exception:
            result["ab_landing_pages"] = []

        _set_cache(cache_key, result)
        return result

    except Exception as e:
        logger.error(f"Error fetching GA4 data: {e}")
        return {"error": str(e)}


def get_search_console_data(days: int = 7) -> dict:
    """Fetch Search Console data for the given date range."""
    from config import SEARCH_CONSOLE_SITE_URL

    if not SEARCH_CONSOLE_SITE_URL:
        return {"error": "SEARCH_CONSOLE_SITE_URL not configured"}

    cache_key = f"sc_{days}"
    cached = _get_cache(cache_key)
    if cached:
        return cached

    service = _get_search_console_service()
    if not service:
        return {"error": "Search Console service not available. Check GA_CREDENTIALS_PATH."}

    try:
        end_date = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d")
        start_date = (datetime.utcnow() - timedelta(days=days + 2)).strftime("%Y-%m-%d")

        result = {}

        # Top search queries
        query_response = service.searchanalytics().query(
            siteUrl=SEARCH_CONSOLE_SITE_URL,
            body={
                "startDate": start_date,
                "endDate": end_date,
                "dimensions": ["query"],
                "rowLimit": 20,
                "dataState": "final"
            }
        ).execute()

        result["top_queries"] = []
        for row in query_response.get("rows", []):
            result["top_queries"].append({
                "query": row["keys"][0],
                "impressions": row.get("impressions", 0),
                "clicks": row.get("clicks", 0),
                "ctr": round(row.get("ctr", 0) * 100, 2),
                "position": round(row.get("position", 0), 1),
            })

        # Top pages
        page_response = service.searchanalytics().query(
            siteUrl=SEARCH_CONSOLE_SITE_URL,
            body={
                "startDate": start_date,
                "endDate": end_date,
                "dimensions": ["page"],
                "rowLimit": 20,
                "dataState": "final"
            }
        ).execute()

        result["top_pages"] = []
        for row in page_response.get("rows", []):
            result["top_pages"].append({
                "page": row["keys"][0],
                "impressions": row.get("impressions", 0),
                "clicks": row.get("clicks", 0),
                "ctr": round(row.get("ctr", 0) * 100, 2),
                "position": round(row.get("position", 0), 1),
            })

        _set_cache(cache_key, result)
        return result

    except Exception as e:
        logger.error(f"Error fetching Search Console data: {e}")
        return {"error": str(e)}


def get_all_analytics(days: int = 7) -> dict:
    """Get all analytics data combined."""
    return {
        "ga4": get_ga4_data(days),
        "search_console": get_search_console_data(days),
        "date_range_days": days,
        "pulled_at": datetime.utcnow().isoformat() + "Z",
    }


def clear_analytics_cache():
    """Clear all cached analytics data."""
    global _cache
    _cache = {}


def _parse_report(response, dimension_names: list, metric_names: list) -> list:
    """Parse a GA4 RunReportResponse into a list of dicts."""
    rows = []
    for row in response.rows:
        entry = {}
        for i, dim_name in enumerate(dimension_names):
            entry[dim_name] = row.dimension_values[i].value
        for i, met_name in enumerate(metric_names):
            val = row.metric_values[i].value
            # Try to convert to number
            try:
                if "." in val:
                    entry[met_name] = round(float(val), 2)
                else:
                    entry[met_name] = int(val)
            except (ValueError, TypeError):
                entry[met_name] = val
        rows.append(entry)
    return rows
