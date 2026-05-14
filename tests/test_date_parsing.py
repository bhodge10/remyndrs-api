"""
Tests for utils/date_parsing.extract_explicit_date and the
reminder handler's AI-date verification override.
"""

import pytest
from datetime import date, datetime
from unittest.mock import patch

from utils.date_parsing import extract_explicit_date


REF = date(2026, 4, 18)  # Saturday


class TestExtractExplicitDate:
    def test_month_name_with_day(self):
        assert extract_explicit_date("Remind me at 8 AM on May 8", REF) == date(2026, 5, 8)

    def test_month_name_with_ordinal(self):
        assert extract_explicit_date("meeting on October 3rd at 2pm", REF) == date(2026, 10, 3)

    def test_month_abbreviation(self):
        assert extract_explicit_date("pay bill on Dec 15", REF) == date(2026, 12, 15)

    def test_sept_abbrev(self):
        assert extract_explicit_date("on Sept 5", REF) == date(2026, 9, 5)

    def test_with_explicit_year(self):
        assert extract_explicit_date("on May 8, 2027", REF) == date(2027, 5, 8)
        assert extract_explicit_date("on May 8 2027", REF) == date(2027, 5, 8)

    def test_rolls_to_next_year_when_past(self):
        # Jan 5 is before April 18, 2026
        assert extract_explicit_date("on January 5", REF) == date(2027, 1, 5)

    def test_does_not_roll_when_year_explicit(self):
        assert extract_explicit_date("on January 5, 2026", REF) == date(2026, 1, 5)

    def test_no_date_in_message(self):
        assert extract_explicit_date("remind me tomorrow to call mom", REF) is None

    def test_empty_message(self):
        assert extract_explicit_date("", REF) is None
        assert extract_explicit_date(None, REF) is None

    def test_invalid_day(self):
        assert extract_explicit_date("on February 30", REF) is None

    def test_ambiguous_multiple_dates_no_anchor(self):
        # Two dates, neither preceded by on/for/by — refuse to guess
        result = extract_explicit_date("between May 8 and June 12", REF)
        assert result is None

    def test_anchored_date_wins_over_unanchored(self):
        # The AI should have scheduled the reminder for the "on" date
        result = extract_explicit_date("my Dec 30 trip, remind me on Feb 14", REF)
        assert result == date(2027, 2, 14)

    def test_case_insensitive(self):
        assert extract_explicit_date("on MAY 8", REF) == date(2026, 5, 8)
        assert extract_explicit_date("on may 8", REF) == date(2026, 5, 8)

    def test_day_with_th_suffix(self):
        assert extract_explicit_date("on May 8th", REF) == date(2026, 5, 8)
        assert extract_explicit_date("on May 1st", REF) == date(2026, 5, 1)
        assert extract_explicit_date("on May 2nd", REF) == date(2026, 5, 2)
        assert extract_explicit_date("on May 3rd", REF) == date(2026, 5, 3)


class TestReminderHandlerDateOverride:
    """
    Regression guard for the bug where AI returned April 19 for
    "Remind me at 8 AM on May 8" (today was April 18, 2026).
    """

    def test_override_corrects_ai_date(self):
        from routes.handlers import reminders as rh

        ai_response = {
            "action": "reminder",
            "reminder_text": "cannot have any more ibuprofen until surgery",
            "reminder_date": "2026-04-19 08:00:00",  # AI's wrong answer
            "confidence": 95,
        }
        incoming = "Remind me at 8:AM on May 8, that I cannot have any more ibuprofen until surgery"

        with patch.object(rh, 'get_user_current_time', return_value=datetime(2026, 4, 18, 15, 42)), \
             patch.object(rh, 'get_user_timezone', return_value='America/New_York'), \
             patch('services.tier_service.can_create_reminder', return_value=(True, None)), \
             patch.object(rh, 'save_reminder_with_local_time'), \
             patch.object(rh, 'create_or_update_user'), \
             patch.object(rh, 'log_interaction'):
            reply = rh.handle_reminder(
                phone_number="+15559876543",
                incoming_msg=incoming,
                ai_response=ai_response,
                should_prompt_daily_summary=lambda _p: False,
                get_daily_summary_prompt_message=lambda r: r,
                mark_daily_summary_prompted=lambda _p: None,
                log_confidence=lambda *a, **kw: None,
                get_setting=lambda _k, default: default,
            )

        # handler mutates ai_response in place with the corrected date
        assert ai_response["reminder_date"].startswith("2026-05-08")
        # confidence drops into confirmation range, so the reply should ask for confirmation
        assert "YES" in reply or "right" in reply.lower()
