"""
Tests for the email reminder fallback (SMS provider outage mitigation).

When a Twilio send fails, send_single_reminder attempts email delivery for
users with an email on file. Gated by the 'reminder_email_fallback_enabled'
DB setting (default off) so it can be flipped without a deploy.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

from tasks.reminder_tasks import _try_reminder_email_fallback, send_single_reminder

TEST_PHONE = "+15559876543"


class TestEmailFallbackHelper:

    def test_disabled_by_default(self):
        with patch('database.get_setting', return_value="false"), \
             patch('services.email_service.send_reminder_email') as send:
            assert _try_reminder_email_fallback(TEST_PHONE, "take out trash") is False
            send.assert_not_called()

    def test_enabled_without_email_on_file(self):
        with patch('database.get_setting', return_value="true"), \
             patch('models.user.get_user_email', return_value=None), \
             patch('services.email_service.send_reminder_email') as send:
            assert _try_reminder_email_fallback(TEST_PHONE, "take out trash") is False
            send.assert_not_called()

    def test_enabled_with_email_delivers(self):
        with patch('database.get_setting', return_value="true"), \
             patch('models.user.get_user_email', return_value="user@example.com"), \
             patch('models.user.get_user_first_name', return_value="Pat"), \
             patch('services.email_service.send_reminder_email', return_value=True) as send:
            assert _try_reminder_email_fallback(TEST_PHONE, "take out trash") is True
            send.assert_called_once_with("user@example.com", "take out trash", "Pat")

    def test_email_send_failure_reports_false(self):
        with patch('database.get_setting', return_value="true"), \
             patch('models.user.get_user_email', return_value="user@example.com"), \
             patch('models.user.get_user_first_name', return_value=None), \
             patch('services.email_service.send_reminder_email', return_value=False):
            assert _try_reminder_email_fallback(TEST_PHONE, "take out trash") is False

    def test_unexpected_error_reports_false(self):
        with patch('database.get_setting', side_effect=RuntimeError("db down")):
            assert _try_reminder_email_fallback(TEST_PHONE, "take out trash") is False


def _reminder_sent(reminder_id):
    from database import get_db_connection, return_db_connection
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT sent FROM reminders WHERE id = %s', (reminder_id,))
        row = c.fetchone()
        return row[0] if row else None
    finally:
        return_db_connection(conn)


class TestSendSingleReminderFallback:

    def _save_due_reminder(self, phone):
        from models.reminder import save_reminder
        from database import get_db_connection, return_db_connection
        save_reminder(
            phone,
            "Fallback test reminder",
            (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        )
        # save_reminder doesn't return the id — look it up
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute('SELECT id FROM reminders WHERE phone_number = %s ORDER BY id DESC LIMIT 1', (phone,))
            return c.fetchone()[0]
        finally:
            return_db_connection(conn)

    def test_sms_failure_with_fallback_marks_sent(self, onboarded_user):
        """SMS fails, fallback enabled, email on file -> delivered by email, marked sent."""
        phone = onboarded_user["phone"]
        reminder_id = self._save_due_reminder(phone)

        with patch('tasks.reminder_tasks.send_sms', side_effect=Exception("twilio suspended")), \
             patch('database.get_setting', return_value="true"), \
             patch('models.user.get_user_email', return_value="user@example.com"), \
             patch('models.user.get_user_first_name', return_value="Pat"), \
             patch('services.email_service.send_reminder_email', return_value=True) as send:
            result = send_single_reminder(reminder_id, phone, "Fallback test reminder")

        assert result["status"] == "sent"
        send.assert_called_once()
        assert _reminder_sent(reminder_id) is True

    def test_sms_failure_without_fallback_stays_unsent(self, onboarded_user):
        """SMS fails, fallback disabled -> task retries, reminder stays unsent."""
        phone = onboarded_user["phone"]
        reminder_id = self._save_due_reminder(phone)

        with patch('tasks.reminder_tasks.send_sms', side_effect=Exception("twilio suspended")), \
             patch('database.get_setting', return_value="false"), \
             patch('services.email_service.send_reminder_email') as send:
            # Called directly (not via worker), retry() re-raises the original exception
            with pytest.raises(Exception):
                send_single_reminder(reminder_id, phone, "Fallback test reminder")

        send.assert_not_called()
        assert _reminder_sent(reminder_id) is False

    def test_sms_success_never_touches_email(self, onboarded_user, sms_capture):
        """Normal path: SMS delivers, email fallback is never consulted."""
        phone = onboarded_user["phone"]
        reminder_id = self._save_due_reminder(phone)

        with patch('tasks.reminder_tasks.send_sms', side_effect=sms_capture.send_sms), \
             patch('services.email_service.send_reminder_email') as send:
            result = send_single_reminder(reminder_id, phone, "Fallback test reminder")

        assert result["status"] == "sent"
        send.assert_not_called()
        assert _reminder_sent(reminder_id) is True
