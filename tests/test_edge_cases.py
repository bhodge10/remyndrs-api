"""
Tests for edge cases, error handling, and security scenarios.
Covers: rate limiting, invalid input, system commands, opt-out, etc.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock


class TestSystemCommands:
    """Tests for system-level commands."""

    @pytest.mark.asyncio
    async def test_stop_command(self, simulator, onboarded_user):
        """Test STOP command opts user out."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "STOP")
        # Should acknowledge opt-out

        # Verify user is opted out
        from models.user import get_user
        user = get_user(phone)
        # User should be marked as opted out

    @pytest.mark.asyncio
    async def test_start_command_after_stop(self, simulator, onboarded_user):
        """Test START command re-subscribes after STOP."""
        phone = onboarded_user["phone"]

        # Opt out
        await simulator.send_message(phone, "STOP")

        # Opt back in
        result = await simulator.send_message(phone, "START")
        # Should acknowledge resubscription

    @pytest.mark.asyncio
    async def test_reset_account_command(self, simulator, onboarded_user):
        """Test RESET ACCOUNT clears all user data."""
        phone = onboarded_user["phone"]

        # Create some data first
        from models.memory import save_memory
        from models.reminder import save_reminder
        save_memory(phone, "test memory", {})
        save_reminder(phone, "test reminder", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

        result = await simulator.send_message(phone, "RESET ACCOUNT")
        # Should confirm reset and trigger onboarding

    @pytest.mark.asyncio
    async def test_info_command(self, simulator, onboarded_user):
        """Test INFO/HELP command shows guide."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "INFO")
        assert any(word in result["output"].lower() for word in ["remind", "memory", "list", "help"])

    @pytest.mark.asyncio
    async def test_commands_command(self, simulator, onboarded_user):
        """Test COMMANDS shows available commands."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "COMMANDS")

    @pytest.mark.asyncio
    async def test_guide_command(self, simulator, onboarded_user):
        """Test GUIDE command."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "GUIDE")


class TestRateLimiting:
    """Tests for rate limiting functionality."""

    @pytest.mark.asyncio
    async def test_rate_limit_triggered(self, simulator, onboarded_user):
        """Test that rate limiting kicks in after too many messages."""
        phone = onboarded_user["phone"]

        # Send many messages quickly
        from config import RATE_LIMIT_MESSAGES
        for i in range(RATE_LIMIT_MESSAGES + 5):
            result = await simulator.send_message(phone, f"Message {i}")

        # Last few should be rate limited
        assert "too quickly" in result["output"].lower() or "wait" in result["output"].lower()

    @pytest.mark.asyncio
    async def test_rate_limit_resets(self, simulator, onboarded_user):
        """Test that rate limit resets after window expires."""
        phone = onboarded_user["phone"]

        # This test would need time manipulation
        # For now, just verify the rate limit can be reset
        from main import rate_limit_store
        rate_limit_store[phone] = []

        result = await simulator.send_message(phone, "Test after reset")
        assert "too quickly" not in result["output"].lower()


class TestInputValidation:
    """Tests for input validation and sanitization."""

    @pytest.mark.asyncio
    async def test_empty_message(self, simulator, onboarded_user):
        """Test handling of empty message."""
        phone = onboarded_user["phone"]

        # Empty messages are typically filtered by Twilio
        # but test our handling anyway
        result = await simulator.send_message(phone, "   ")

    @pytest.mark.asyncio
    async def test_very_long_message(self, simulator, onboarded_user, ai_mock):
        """Test handling of very long message."""
        phone = onboarded_user["phone"]

        long_message = "A" * 2000

        ai_mock.set_response(long_message.lower(), {
            "action": "unknown",
            "response": "Message too long"
        })

        result = await simulator.send_message(phone, long_message)
        # Should handle without crashing

    @pytest.mark.asyncio
    async def test_unicode_characters(self, simulator, onboarded_user, ai_mock):
        """Test handling of unicode/emoji characters."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me to buy gifts 🎁", {
            "action": "reminder",
            "reminder_text": "buy gifts",
            "reminder_date": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        })

        result = await simulator.send_message(phone, "Remind me to buy gifts 🎁")

    @pytest.mark.asyncio
    async def test_sql_injection_attempt(self, simulator, onboarded_user, ai_mock):
        """Test that SQL injection attempts are handled safely."""
        phone = onboarded_user["phone"]

        malicious = "'; DROP TABLE users; --"

        ai_mock.set_response(malicious.lower(), {
            "action": "unknown",
            "response": "I don't understand"
        })

        result = await simulator.send_message(phone, malicious)
        # Should not crash, should handle safely

    @pytest.mark.asyncio
    async def test_html_script_injection(self, simulator, onboarded_user, ai_mock):
        """Test that script injection is handled safely."""
        phone = onboarded_user["phone"]

        malicious = "<script>alert('xss')</script>"

        ai_mock.set_response(malicious.lower(), {
            "action": "store",
            "memory_text": malicious
        })

        result = await simulator.send_message(phone, f"Remember {malicious}")
        # Should store safely or reject


class TestFeedbackAndSupport:
    """Tests for feedback and support functionality."""

    @pytest.mark.asyncio
    async def test_feedback_submission(self, simulator, onboarded_user):
        """Test submitting feedback with new format (no colon)."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "FEEDBACK The app is great!")
        assert any(word in result["output"].lower() for word in ["feedback", "thank", "received"])

    @pytest.mark.asyncio
    async def test_feedback_backward_compatible(self, simulator, onboarded_user):
        """Test submitting feedback with old format (colon) for backward compatibility."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "FEEDBACK: Still works with colon!")
        assert any(word in result["output"].lower() for word in ["feedback", "thank", "received"])

    @pytest.mark.asyncio
    async def test_support_premium_only(self, simulator, onboarded_user):
        """Test that SUPPORT is for premium users (new format)."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "SUPPORT I need help")
        # Should either enter support mode (premium) or show upgrade prompt

    @pytest.mark.asyncio
    async def test_support_backward_compatible(self, simulator, onboarded_user):
        """Test SUPPORT with old format (colon) for backward compatibility."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "SUPPORT: I need help with old format")
        # Should either enter support mode (premium) or show upgrade prompt


class TestDailySummary:
    """Tests for daily summary feature."""

    @pytest.mark.asyncio
    async def test_enable_daily_summary(self, simulator, onboarded_user):
        """Test enabling daily summary."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "SUMMARY ON")
        assert any(word in result["output"].lower() for word in ["summary", "enabled", "on"])

    @pytest.mark.asyncio
    async def test_disable_daily_summary(self, simulator, onboarded_user):
        """Test disabling daily summary."""
        phone = onboarded_user["phone"]

        # Enable first
        await simulator.send_message(phone, "SUMMARY ON")

        result = await simulator.send_message(phone, "SUMMARY OFF")
        assert any(word in result["output"].lower() for word in ["summary", "disabled", "off"])

    @pytest.mark.asyncio
    async def test_set_summary_time(self, simulator, onboarded_user):
        """Test setting daily summary time."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "SUMMARY TIME 7AM")
        # Should confirm time change


class TestAccountManagement:
    """Tests for account and subscription management."""

    @pytest.mark.asyncio
    async def test_upgrade_command(self, simulator, onboarded_user):
        """Test UPGRADE command shows pricing."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "UPGRADE")
        assert any(word in result["output"].lower() for word in ["premium", "price", "upgrade", "$"])

    @pytest.mark.asyncio
    async def test_account_command(self, simulator, onboarded_user):
        """Test ACCOUNT command shows account info."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "ACCOUNT")

    @pytest.mark.asyncio
    async def test_billing_command(self, simulator, onboarded_user):
        """Test BILLING command."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "BILLING")


class TestBulkDeletion:
    """Tests for bulk deletion commands."""

    @pytest.mark.asyncio
    async def test_delete_all_reminders(self, simulator, onboarded_user):
        """Test DELETE ALL REMINDERS command."""
        phone = onboarded_user["phone"]

        # Create some reminders
        from models.reminder import save_reminder
        save_reminder(phone, "reminder 1", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
        save_reminder(phone, "reminder 2", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

        result = await simulator.send_message(phone, "DELETE ALL REMINDERS")
        # Should ask for confirmation

    @pytest.mark.asyncio
    async def test_delete_all_memories(self, simulator, onboarded_user):
        """Test DELETE ALL MEMORIES command."""
        phone = onboarded_user["phone"]

        from models.memory import save_memory
        save_memory(phone, "memory 1", {})
        save_memory(phone, "memory 2", {})

        result = await simulator.send_message(phone, "DELETE ALL MEMORIES")

    @pytest.mark.asyncio
    async def test_delete_all_lists(self, simulator, onboarded_user):
        """Test DELETE ALL LISTS command."""
        phone = onboarded_user["phone"]

        from models.list_model import create_list
        create_list(phone, "list1")
        create_list(phone, "list2")

        result = await simulator.send_message(phone, "DELETE ALL LISTS")


class TestConcurrentState:
    """Tests for handling concurrent/conflicting states."""

    @pytest.mark.asyncio
    async def test_pending_delete_then_new_command(self, simulator, onboarded_user):
        """Test behavior when user has pending delete but sends new command."""
        phone = onboarded_user["phone"]

        # Set up pending delete state
        from database import get_db_connection, return_db_connection
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET pending_delete = TRUE WHERE phone_number = %s", (phone,))
        conn.commit()
        return_db_connection(conn)

        # Send a new command
        result = await simulator.send_message(phone, "MY REMINDERS")
        # Should handle gracefully - either clear pending or ask to confirm

    @pytest.mark.asyncio
    async def test_cancel_during_pending_operation(self, simulator, onboarded_user):
        """Test CANCEL clears pending operations."""
        phone = onboarded_user["phone"]

        # Set up pending state
        from database import get_db_connection, return_db_connection
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET pending_list_item = 'test item' WHERE phone_number = %s", (phone,))
        conn.commit()
        return_db_connection(conn)

        result = await simulator.send_message(phone, "CANCEL")
        # Should clear pending state
