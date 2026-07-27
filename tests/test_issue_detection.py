"""
Tests for auto-detected issue reports (services/issue_detector.py).

Users report outages in plain language rather than texting SUPPORT/BUG. The
detector notices those messages, records them, and notifies -- without ever
changing what the user sees.

The false-negative cost here is a week-long silent outage; the false-positive
cost is a junk email. The prefilter is tuned accordingly, so the negative
cases below matter as much as the positive ones.
"""

import pytest
from unittest.mock import patch

from database import get_db_connection, return_db_connection
from services.issue_detector import (
    AUTO_SOURCE,
    looks_like_issue_report,
    record_and_notify,
    maybe_flag_issue_report,
    count_distinct_reporters,
)


def _fetch_flags(phone):
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            'SELECT message, category, source FROM contact_messages '
            'WHERE phone_number = %s AND source = %s ORDER BY id',
            (phone, AUTO_SOURCE)
        )
        return c.fetchall()
    finally:
        return_db_connection(conn)


def _clear_flags(*phones):
    conn = get_db_connection()
    try:
        c = conn.cursor()
        for phone in phones:
            c.execute(
                'DELETE FROM contact_messages WHERE phone_number = %s AND source = %s',
                (phone, AUTO_SOURCE)
            )
        conn.commit()
    finally:
        return_db_connection(conn)


def _clear_escalation_marker():
    from database import set_setting
    set_setting("auto_issue_last_escalation", "")


ISSUE_CLASSIFICATION = {
    'category': 'reminder_delivery',
    'severity': 'high',
    'summary': 'User says reminders stopped arriving',
}


class TestPrefilter:
    """Stage 1: free regex. Generous, but must not fire on ordinary requests."""

    @pytest.mark.parametrize("message", [
        "my reminders aren't working",
        "I never got my reminder this morning",
        "is this service down?",
        "hey are you having issues? nothing came through",
        "the app is broken",
        "reminders stopped working a few days ago",
        "I didn't receive my 8am text",
        "why didn't I get my reminder",
        "something's wrong with my account",
        "I got double charged this month",
        "I need to talk to customer service",
        "what happened to my reminders",
        "no reminders for 3 days now",
    ])
    def test_flags_issue_language(self, message):
        assert looks_like_issue_report(message) is True

    @pytest.mark.parametrize("message", [
        # The critical false positive: issue words inside reminder content
        "remind me to fix the broken sink",
        "Remind me to call about the refund tomorrow at 3pm",
        "remind me that the dishwasher is broken",
        "add bug spray to my grocery list",
        "add drain cleaner to my list for the broken sink",
        "remember that the garage door is broken",
        "create a list for things that are broken",
        # Ordinary traffic
        "remind me to call mom at 5pm",
        "show my lists",
        "what's on my grocery list",
        "thanks!",
        "yes",
        "",
    ])
    def test_ignores_normal_messages(self, message):
        assert looks_like_issue_report(message) is False

    @pytest.mark.parametrize("message", [
        "SUPPORT my reminders are broken",
        "BUG nothing is working",
        "FEEDBACK this doesn't work",
        "HELP",
        "STOP",
    ])
    def test_ignores_keyword_commands(self, message):
        """These already notify through their own handlers - don't double-report."""
        assert looks_like_issue_report(message) is False


class TestRecording:
    """Stage 3: persistence, email rate limiting, escalation."""

    def test_records_flag_row(self, test_phone):
        _clear_flags(test_phone)
        with patch('services.email_service.send_issue_flag_notification', return_value=True):
            result = record_and_notify(test_phone, "my reminders aren't working", ISSUE_CLASSIFICATION)

        assert result['recorded'] is True
        rows = _fetch_flags(test_phone)
        assert len(rows) == 1
        assert rows[0][0] == "my reminders aren't working"
        assert rows[0][1] == "auto_reminder_delivery"
        assert rows[0][2] == AUTO_SOURCE
        _clear_flags(test_phone)

    def test_email_cooldown_suppresses_second_email(self, test_phone):
        """A frustrated user texting repeatedly produces rows, but one email."""
        _clear_flags(test_phone)
        with patch('services.email_service.send_issue_flag_notification', return_value=True) as mock_email:
            record_and_notify(test_phone, "reminders aren't working", ISSUE_CLASSIFICATION)
            record_and_notify(test_phone, "still not working!", ISSUE_CLASSIFICATION)
            record_and_notify(test_phone, "hello?? nothing is working", ISSUE_CLASSIFICATION)

        assert len(_fetch_flags(test_phone)) == 3, "every report should be recorded"
        assert mock_email.call_count == 1, "only the first should email"
        _clear_flags(test_phone)

    def test_counts_distinct_reporters(self, test_phone):
        phones = [test_phone, "+15550002222", "+15550003333"]
        _clear_flags(*phones)
        with patch('services.email_service.send_issue_flag_notification', return_value=True), \
             patch('services.issue_detector._maybe_escalate'):
            for phone in phones:
                record_and_notify(phone, "nothing is working", ISSUE_CLASSIFICATION)

        assert count_distinct_reporters(24) >= 3
        _clear_flags(*phones)

    def test_escalates_once_when_multiple_users_report(self, test_phone):
        """Three distinct users inside 24h is the outage signature."""
        phones = [test_phone, "+15550002222", "+15550003333", "+15550004444"]
        _clear_flags(*phones)
        _clear_escalation_marker()

        with patch('services.email_service.send_issue_flag_notification', return_value=True), \
             patch('services.email_service.send_outage_escalation_notification', return_value=True) as mock_escalate, \
             patch('services.sms_service.notify_admin') as mock_sms:
            for phone in phones:
                record_and_notify(phone, "nothing is working", ISSUE_CLASSIFICATION)

        assert mock_escalate.call_count == 1, "escalate once, not once per reporter"
        assert mock_sms.call_count == 1
        _clear_flags(*phones)
        _clear_escalation_marker()

    def test_survives_db_failure(self, test_phone):
        """Recording must never raise into the webhook."""
        with patch('services.issue_detector.get_db_connection', side_effect=RuntimeError('db down')):
            result = record_and_notify(test_phone, "not working", ISSUE_CLASSIFICATION)
        assert result['recorded'] is False


class TestPipeline:
    """maybe_flag_issue_report end to end, with the AI stage mocked."""

    def test_flags_when_classifier_confirms(self, test_phone):
        _clear_flags(test_phone)
        with patch('services.issue_detector.classify_issue_report', return_value=ISSUE_CLASSIFICATION), \
             patch('services.email_service.send_issue_flag_notification', return_value=True):
            result = maybe_flag_issue_report(test_phone, "my reminders aren't working")

        assert result is not None and result['recorded'] is True
        assert len(_fetch_flags(test_phone)) == 1
        _clear_flags(test_phone)

    def test_no_flag_when_classifier_rejects(self, test_phone):
        """The AI stage is what kills prefilter false positives."""
        _clear_flags(test_phone)
        with patch('services.issue_detector.classify_issue_report', return_value=None):
            result = maybe_flag_issue_report(test_phone, "the app is broken")

        assert result is None
        assert _fetch_flags(test_phone) == []

    def test_classifier_not_called_when_prefilter_misses(self, test_phone):
        """Ordinary traffic must not cost an API call."""
        with patch('services.issue_detector.classify_issue_report') as mock_classify:
            maybe_flag_issue_report(test_phone, "remind me to call mom at 5pm")
        mock_classify.assert_not_called()

    def test_kill_switch_disables_everything(self, test_phone):
        with patch('services.issue_detector.is_detection_enabled', return_value=False), \
             patch('services.issue_detector.classify_issue_report') as mock_classify:
            result = maybe_flag_issue_report(test_phone, "nothing is working")

        assert result is None
        mock_classify.assert_not_called()

    def test_never_raises(self, test_phone):
        with patch('services.issue_detector.looks_like_issue_report', side_effect=RuntimeError('boom')):
            assert maybe_flag_issue_report(test_phone, "anything") is None


class TestWebhookIntegration:
    """The observer must be invisible to the user."""

    @pytest.mark.asyncio
    async def test_reply_unchanged_when_flagged(self, simulator, onboarded_user):
        """
        Regression guard: the user-visible reply is identical whether or not
        the message was flagged. Silent flagging was a deliberate choice.
        """
        phone = onboarded_user["phone"]
        _clear_flags(phone)
        message = "my reminders aren't working"

        # Baseline: detector stubbed out entirely
        with patch('services.issue_detector.maybe_flag_issue_report') as mock_detector:
            baseline = await simulator.send_message(phone, message)
        assert mock_detector.called, "the hook should reach the detector"

        with patch('services.issue_detector.classify_issue_report', return_value=ISSUE_CLASSIFICATION), \
             patch('services.email_service.send_issue_flag_notification', return_value=True):
            flagged = await simulator.send_message(phone, message)

        assert flagged["output"] == baseline["output"]
        assert len(_fetch_flags(phone)) == 1, "flagged exactly once, silently"
        _clear_flags(phone)

    @pytest.mark.asyncio
    async def test_detector_failure_does_not_break_webhook(self, simulator, onboarded_user):
        phone = onboarded_user["phone"]
        with patch('services.issue_detector.maybe_flag_issue_report',
                   side_effect=RuntimeError('detector exploded')):
            result = await simulator.send_message(phone, "my reminders aren't working")
        assert result["output"]  # user still got a reply

    @pytest.mark.asyncio
    async def test_normal_message_creates_no_flag(self, simulator, onboarded_user):
        phone = onboarded_user["phone"]
        _clear_flags(phone)
        await simulator.send_message(phone, "remind me to fix the broken sink tomorrow at 9am")
        assert _fetch_flags(phone) == []
