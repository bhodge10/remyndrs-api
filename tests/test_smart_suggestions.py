"""
Tests for Smart Suggestions — Phase 1: Prep Reminders.
Covers: keyword detection, timing calculation, suggestion injection, YES/NO handling.
"""

import pytest
from datetime import datetime, timedelta
import pytz


# =====================================================
# DETECTION TESTS
# =====================================================


class TestPrepReminderDetection:
    """Tests for get_reminder_suggestion keyword matching."""

    def _make_future_utc(self, hours_ahead=48):
        """Helper: UTC datetime string N hours from now."""
        dt = datetime.utcnow() + timedelta(hours=hours_ahead)
        return dt.strftime('%Y-%m-%d %H:%M:%S')

    def test_detects_doctor_appointment(self):
        from services.smart_suggestion_service import get_reminder_suggestion
        result = get_reminder_suggestion(
            "doctor appointment",
            self._make_future_utc(48),
            "America/New_York"
        )
        assert result is not None
        assert "remind" in result['suggestion_text'].lower() or "want" in result['suggestion_text'].lower()

    def test_detects_interview(self):
        from services.smart_suggestion_service import get_reminder_suggestion
        result = get_reminder_suggestion(
            "job interview at Google",
            self._make_future_utc(48),
            "America/New_York"
        )
        assert result is not None
        assert "interview" in result['prep_reminder_text'].lower()

    def test_detects_flight(self):
        from services.smart_suggestion_service import get_reminder_suggestion
        result = get_reminder_suggestion(
            "flight to NYC",
            self._make_future_utc(48),
            "America/New_York"
        )
        assert result is not None
        assert "airport" in result['prep_reminder_text'].lower()

    def test_detects_birthday(self):
        from services.smart_suggestion_service import get_reminder_suggestion
        result = get_reminder_suggestion(
            "Mom's birthday",
            self._make_future_utc(72),
            "America/New_York"
        )
        assert result is not None
        assert "gift" in result['prep_reminder_text'].lower()

    def test_detects_deadline(self):
        from services.smart_suggestion_service import get_reminder_suggestion
        result = get_reminder_suggestion(
            "project deadline",
            self._make_future_utc(48),
            "America/New_York"
        )
        assert result is not None

    def test_detects_exam(self):
        from services.smart_suggestion_service import get_reminder_suggestion
        result = get_reminder_suggestion(
            "final exam",
            self._make_future_utc(48),
            "America/New_York"
        )
        assert result is not None
        assert "review" in result['prep_reminder_text'].lower()

    def test_no_match_for_generic_reminder(self):
        from services.smart_suggestion_service import get_reminder_suggestion
        result = get_reminder_suggestion(
            "call mom",
            self._make_future_utc(48),
            "America/New_York"
        )
        assert result is None

    def test_no_match_for_groceries(self):
        from services.smart_suggestion_service import get_reminder_suggestion
        result = get_reminder_suggestion(
            "buy groceries",
            self._make_future_utc(48),
            "America/New_York"
        )
        assert result is None

    def test_case_insensitive(self):
        from services.smart_suggestion_service import get_reminder_suggestion
        result = get_reminder_suggestion(
            "DOCTOR APPOINTMENT",
            self._make_future_utc(48),
            "America/New_York"
        )
        assert result is not None


# =====================================================
# TIMING TESTS
# =====================================================


class TestPrepReminderTiming:
    """Tests for prep reminder timing calculation."""

    def test_no_suggestion_if_too_soon(self):
        """Events less than 4 hours away shouldn't get suggestions."""
        from services.smart_suggestion_service import get_reminder_suggestion
        soon = datetime.utcnow() + timedelta(hours=2)
        result = get_reminder_suggestion(
            "doctor appointment",
            soon.strftime('%Y-%m-%d %H:%M:%S'),
            "America/New_York"
        )
        assert result is None

    def test_no_suggestion_if_prep_in_past(self):
        """If the calculated prep time would be in the past, no suggestion."""
        from services.smart_suggestion_service import get_reminder_suggestion
        # Event 5 hours away — prep would be now-ish or past for 12h default
        soon = datetime.utcnow() + timedelta(hours=5)
        result = get_reminder_suggestion(
            "doctor appointment",
            soon.strftime('%Y-%m-%d %H:%M:%S'),
            "America/New_York"
        )
        # May or may not return depending on exact timing — just shouldn't crash
        # The key constraint: if returned, prep_date_utc must be in the future
        if result:
            prep_dt = datetime.strptime(result['prep_date_utc'], '%Y-%m-%d %H:%M:%S')
            assert prep_dt > datetime.utcnow()

    def test_morning_event_gets_evening_before(self):
        """Morning events should get an evening-before prep reminder."""
        from services.smart_suggestion_service import _calculate_prep_time

        tz = pytz.timezone("America/New_York")
        now = datetime.now(tz)

        # Create a morning event 2 days from now at 9 AM
        event = (now + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
        prep = _calculate_prep_time(event, 12, now)

        assert prep is not None
        assert prep.hour == 19  # 7 PM
        assert prep.date() == (event - timedelta(days=1)).date()

    def test_afternoon_event_gets_hours_before(self):
        """Afternoon events should get a reminder N hours before."""
        from services.smart_suggestion_service import _calculate_prep_time

        tz = pytz.timezone("America/New_York")
        now = datetime.now(tz)

        # Create an afternoon event 2 days from now at 3 PM
        event = (now + timedelta(days=2)).replace(hour=15, minute=0, second=0, microsecond=0)
        prep = _calculate_prep_time(event, 12, now)

        assert prep is not None
        # Should be 12 hours before (3 AM) — but that's fine for the calculation
        assert prep < event

    def test_birthday_gets_days_before(self):
        """Birthdays (48h default) should get a reminder 2 days before at 9 AM."""
        from services.smart_suggestion_service import _calculate_prep_time

        tz = pytz.timezone("America/New_York")
        now = datetime.now(tz)

        event = (now + timedelta(days=7)).replace(hour=12, minute=0, second=0, microsecond=0)
        prep = _calculate_prep_time(event, 48, now)

        assert prep is not None
        assert prep.hour == 9  # 9 AM
        assert (event.date() - prep.date()).days == 2

    def test_suggestion_has_required_fields(self):
        """Returned suggestion dict should have all required fields."""
        from services.smart_suggestion_service import get_reminder_suggestion
        future = datetime.utcnow() + timedelta(days=3)
        result = get_reminder_suggestion(
            "dentist appointment",
            future.strftime('%Y-%m-%d %H:%M:%S'),
            "America/New_York"
        )
        assert result is not None
        assert 'suggestion_text' in result
        assert 'prep_date_utc' in result
        assert 'prep_local_time' in result
        assert 'prep_reminder_text' in result
        assert 'prep_date_display' in result
