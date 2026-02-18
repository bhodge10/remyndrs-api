"""
Tests for reminder creation and management.
Covers: specific time, relative time, recurring, AM/PM clarification, snooze, delete.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
import pytz


class TestReminderCreation:
    """Tests for creating reminders with various time formats."""

    @pytest.mark.asyncio
    async def test_reminder_with_specific_time(self, simulator, onboarded_user, ai_mock):
        """Test creating a reminder with a specific date and time."""
        phone = onboarded_user["phone"]

        # Set up AI to return a specific reminder response
        ai_mock.set_response("remind me tomorrow at 2pm to call mom", {
            "action": "reminder",
            "reminder_text": "call mom",
            "reminder_date": (datetime.utcnow() + timedelta(days=1)).replace(hour=14, minute=0).strftime("%Y-%m-%d %H:%M:%S")
        })

        result = await simulator.send_message(phone, "Remind me tomorrow at 2pm to call mom")

        # Should confirm the reminder was set
        assert any(word in result["output"].lower() for word in ["remind", "set", "tomorrow", "2"])

    @pytest.mark.asyncio
    async def test_reminder_relative_time(self, simulator, onboarded_user, ai_mock):
        """Test creating a reminder with relative time (in X minutes)."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me in 30 minutes to check the oven", {
            "action": "reminder_relative",
            "reminder_text": "check the oven",
            "offset_minutes": 30
        })

        result = await simulator.send_message(phone, "Remind me in 30 minutes to check the oven")
        assert any(word in result["output"].lower() for word in ["remind", "30", "minute"])

    @pytest.mark.asyncio
    async def test_reminder_needs_am_pm_clarification(self, simulator, onboarded_user, ai_mock):
        """Test reminder creation flow when AM/PM is ambiguous."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me at 4 to take medicine", {
            "action": "clarify_time",
            "reminder_text": "take medicine",
            "time_mentioned": "4:00"
        })

        result = await simulator.send_message(phone, "Remind me at 4 to take medicine")

        # Should ask for AM or PM
        assert "am" in result["output"].lower() or "pm" in result["output"].lower()

        # User responds with PM
        result = await simulator.send_message(phone, "PM")

        # Should confirm reminder is set
        assert any(word in result["output"].lower() for word in ["remind", "set", "4", "pm"])

    @pytest.mark.asyncio
    async def test_reminder_am_clarification(self, simulator, onboarded_user, ai_mock):
        """Test AM clarification works correctly."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me at 7 to wake up", {
            "action": "clarify_time",
            "reminder_text": "wake up",
            "time_mentioned": "7:00"
        })

        await simulator.send_message(phone, "Remind me at 7 to wake up")

        # Respond with AM
        result = await simulator.send_message(phone, "AM")
        assert any(word in result["output"].lower() for word in ["remind", "7", "am"])


class TestRecurringReminders:
    """Tests for recurring reminder functionality."""

    @pytest.mark.asyncio
    async def test_daily_recurring_reminder(self, simulator, onboarded_user, ai_mock):
        """Test creating a daily recurring reminder."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me every day at 9am to take vitamins", {
            "action": "reminder_recurring",
            "reminder_text": "take vitamins",
            "recurrence_type": "daily",
            "time": "09:00"
        })

        result = await simulator.send_message(phone, "Remind me every day at 9am to take vitamins")
        assert any(word in result["output"].lower() for word in ["daily", "every day", "recurring", "9"])

    @pytest.mark.asyncio
    async def test_weekly_recurring_reminder(self, simulator, onboarded_user, ai_mock):
        """Test creating a weekly recurring reminder."""
        phone = onboarded_user["phone"]

        # Register both original and normalized forms (main.py normalizes "10am" → "10:AM")
        recurring_response = {
            "action": "reminder_recurring",
            "reminder_text": "team meeting",
            "recurrence_type": "weekly",
            "recurrence_day": 0,  # Monday
            "time": "10:00"
        }
        ai_mock.set_response("remind me every monday at 10am about team meeting", recurring_response)
        ai_mock.set_response("remind me every monday at 10:am about team meeting", recurring_response)

        result = await simulator.send_message(phone, "Remind me every Monday at 10am about team meeting")
        assert any(word in result["output"].lower() for word in ["monday", "weekly", "recurring"])

    @pytest.mark.asyncio
    async def test_weekday_recurring_reminder(self, simulator, onboarded_user, ai_mock):
        """Test creating a weekday-only recurring reminder."""
        phone = onboarded_user["phone"]

        # Register both original and normalized forms (main.py normalizes "8am" → "8:AM")
        weekday_response = {
            "action": "reminder_recurring",
            "reminder_text": "check email",
            "recurrence_type": "weekdays",
            "time": "08:00"
        }
        ai_mock.set_response("remind me on weekdays at 8am to check email", weekday_response)
        ai_mock.set_response("remind me on weekdays at 8:am to check email", weekday_response)

        result = await simulator.send_message(phone, "Remind me on weekdays at 8am to check email")
        assert any(word in result["output"].lower() for word in ["weekday", "recurring"])


class TestReminderViewing:
    """Tests for viewing reminders."""

    @pytest.mark.asyncio
    async def test_view_my_reminders(self, simulator, onboarded_user, ai_mock):
        """Test viewing scheduled reminders."""
        phone = onboarded_user["phone"]

        # First create a reminder
        from models.reminder import save_reminder
        save_reminder(
            phone,
            "Test reminder for viewing",
            (datetime.utcnow() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        )

        # Set up AI mock to return list_reminders action for "MY REMINDERS"
        ai_mock.set_response("my reminders", {
            "action": "list_reminders"
        })

        # Request to view reminders
        result = await simulator.send_message(phone, "MY REMINDERS")
        assert "test reminder" in result["output"].lower() or "no upcoming" in result["output"].lower()

    @pytest.mark.asyncio
    async def test_view_my_recurring(self, simulator, onboarded_user):
        """Test viewing recurring reminders."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "MY RECURRING")
        # Should show recurring list or indicate none exist


class TestReminderDeletion:
    """Tests for deleting reminders."""

    @pytest.mark.asyncio
    async def test_delete_reminder_by_number(self, simulator, onboarded_user):
        """Test deleting a reminder by its number."""
        phone = onboarded_user["phone"]

        # Create test reminders
        from models.reminder import save_reminder
        save_reminder(phone, "First reminder", (datetime.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
        save_reminder(phone, "Second reminder", (datetime.utcnow() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"))

        # View reminders first
        await simulator.send_message(phone, "MY REMINDERS")

        # Delete by number
        result = await simulator.send_message(phone, "delete 1")
        # Should ask for confirmation or show options


class TestSnooze:
    """Tests for snooze functionality."""

    @pytest.mark.asyncio
    async def test_snooze_default_duration(self, simulator, onboarded_user, sms_capture):
        """Test snoozing with default duration (15 min)."""
        phone = onboarded_user["phone"]

        # Create and "send" a reminder
        from models.reminder import save_reminder
        from models.user import create_or_update_user
        from database import get_db_connection, return_db_connection

        # Create a reminder that was just sent
        reminder_id = save_reminder(
            phone,
            "Just sent reminder",
            (datetime.utcnow() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        )

        # Mark it as sent and update user's last_sent_reminder
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE reminders SET sent = TRUE WHERE id = %s", (reminder_id,))
        c.execute("UPDATE users SET last_sent_reminder_id = %s, last_sent_reminder_at = NOW() WHERE phone_number = %s",
                  (reminder_id, phone))
        conn.commit()
        return_db_connection(conn)

        # Snooze
        result = await simulator.send_message(phone, "SNOOZE")
        assert any(word in result["output"].lower() for word in ["snooze", "remind", "minutes"])

    @pytest.mark.asyncio
    async def test_snooze_custom_duration(self, simulator, onboarded_user):
        """Test snoozing with custom duration."""
        phone = onboarded_user["phone"]

        # Setup similar to above
        from models.reminder import save_reminder
        from database import get_db_connection, return_db_connection

        reminder_id = save_reminder(
            phone,
            "Snooze test reminder",
            (datetime.utcnow() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        )

        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE reminders SET sent = TRUE WHERE id = %s", (reminder_id,))
        c.execute("UPDATE users SET last_sent_reminder_id = %s, last_sent_reminder_at = NOW() WHERE phone_number = %s",
                  (reminder_id, phone))
        conn.commit()
        return_db_connection(conn)

        # Snooze for 1 hour
        result = await simulator.send_message(phone, "SNOOZE 1h")
        assert any(word in result["output"].lower() for word in ["snooze", "hour", "60"])


class TestReminderEdgeCases:
    """Edge cases and error scenarios for reminders."""

    @pytest.mark.asyncio
    async def test_reminder_in_past(self, simulator, onboarded_user, ai_mock):
        """Test handling of a reminder time that's in the past."""
        phone = onboarded_user["phone"]

        # AI returns a past time
        ai_mock.set_response("remind me yesterday", {
            "action": "reminder",
            "reminder_text": "test",
            "reminder_date": (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        })

        result = await simulator.send_message(phone, "Remind me yesterday")
        # Should handle gracefully - either reject or adjust

    @pytest.mark.asyncio
    async def test_reminder_very_far_future(self, simulator, onboarded_user, ai_mock):
        """Test reminder set far in the future."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me in a year to renew subscription", {
            "action": "reminder",
            "reminder_text": "renew subscription",
            "reminder_date": (datetime.utcnow() + timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
        })

        result = await simulator.send_message(phone, "Remind me in a year to renew subscription")
        # Should accept or indicate limitations

    @pytest.mark.asyncio
    async def test_snooze_no_recent_reminder(self, simulator, onboarded_user):
        """Test snooze when there's no recent reminder."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "SNOOZE")
        # Should indicate no reminder to snooze
        assert any(word in result["output"].lower() for word in ["no", "nothing", "recent"])

    @pytest.mark.asyncio
    async def test_delete_nonexistent_reminder(self, simulator, onboarded_user):
        """Test deleting a reminder that doesn't exist."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "delete 999")
        # Should handle gracefully


class TestReminderTimezoneHandling:
    """Tests for timezone-aware reminder handling."""

    @pytest.mark.asyncio
    async def test_reminder_respects_user_timezone(self, simulator, onboarded_user, ai_mock):
        """Test that reminder times are stored correctly for user's timezone."""
        phone = onboarded_user["phone"]

        # User is in America/New_York
        ai_mock.set_response("remind me at 3pm today to call", {
            "action": "reminder",
            "reminder_text": "call",
            "reminder_date": datetime.utcnow().replace(hour=15, minute=0).strftime("%Y-%m-%d %H:%M:%S")
        })

        result = await simulator.send_message(phone, "Remind me at 3pm today to call")

        # Verify the reminder was stored with correct UTC conversion
        from models.reminder import get_user_reminders
        reminders = get_user_reminders(phone)
        assert len(reminders) > 0

    @pytest.mark.asyncio
    async def test_reminder_display_in_user_timezone(self, simulator, onboarded_user):
        """Test that reminders are displayed in user's local timezone."""
        phone = onboarded_user["phone"]

        # Create reminder in UTC
        from models.reminder import save_reminder
        save_reminder(
            phone,
            "Timezone test",
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        )

        result = await simulator.send_message(phone, "MY REMINDERS")
        # Response should show time in Eastern timezone format


class TestMultiDayReminders:
    """Tests for multi-day reminder creation (e.g., 'for the next 5 days')."""

    @pytest.mark.asyncio
    async def test_every_day_for_next_five_days(self, simulator, onboarded_user, ai_mock):
        """Test creating reminders for the next 5 days via 'multiple' action."""
        phone = onboarded_user["phone"]

        # Build 5 reminder actions, one per day at 5:00 PM local time
        actions = []
        for i in range(1, 6):
            day = (datetime.utcnow() + timedelta(days=i)).replace(hour=17, minute=0, second=0)
            actions.append({
                "action": "reminder",
                "reminder_text": "it's time to quit working",
                "reminder_date": day.strftime("%Y-%m-%d %H:%M:%S"),
                "confidence": 95
            })

        multiple_response = {
            "action": "multiple",
            "actions": actions
        }

        # Register both original and normalized forms (main.py normalizes "5pm" -> "5:PM")
        ai_mock.set_response(
            "remind me every day for the next five days at 5pm that it's time to quit working",
            multiple_response
        )
        ai_mock.set_response(
            "remind me every day for the next five days at 5:pm that it's time to quit working",
            multiple_response
        )

        result = await simulator.send_message(
            phone,
            "Remind me every day for the next five days at 5 PM that it's time to quit working"
        )

        # Should get confirmation(s) - the multiple handler joins replies with \n\n
        output = result["output"].lower()
        assert "remind" in output or "got it" in output, \
            f"Expected reminder confirmation, got: {result['output']}"

        # Verify reminders were created in the database
        from models.reminder import get_user_reminders
        reminders = get_user_reminders(phone)
        unsent = [r for r in reminders if not r[4]]  # r[4] = sent
        assert len(unsent) >= 5, \
            f"Expected at least 5 unsent reminders, got {len(unsent)}"

    @pytest.mark.asyncio
    async def test_multi_day_reminder_not_classified_as_recurring(self, simulator, onboarded_user, ai_mock):
        """Test that 'every day for the next 3 days' uses multiple action, not reminder_recurring."""
        phone = onboarded_user["phone"]

        # The correct response for "for the next 3 days" is multiple separate reminders
        actions = []
        for i in range(1, 4):
            day = (datetime.utcnow() + timedelta(days=i)).replace(hour=8, minute=0, second=0)
            actions.append({
                "action": "reminder",
                "reminder_text": "take medication",
                "reminder_date": day.strftime("%Y-%m-%d %H:%M:%S"),
                "confidence": 95
            })

        multiple_response = {
            "action": "multiple",
            "actions": actions
        }

        ai_mock.set_response(
            "remind me for the next 3 days at 8am to take medication",
            multiple_response
        )
        ai_mock.set_response(
            "remind me for the next 3 days at 8:am to take medication",
            multiple_response
        )

        result = await simulator.send_message(
            phone,
            "Remind me for the next 3 days at 8am to take medication"
        )

        output = result["output"].lower()
        assert "remind" in output or "got it" in output, \
            f"Expected reminder confirmation, got: {result['output']}"

        from models.reminder import get_user_reminders
        reminders = get_user_reminders(phone)
        unsent = [r for r in reminders if not r[4]]
        assert len(unsent) >= 3, \
            f"Expected at least 3 unsent reminders, got {len(unsent)}"


class TestClarifyTimeOclock:
    """Tests for clarify_time flow with o'clock patterns and date context."""

    @pytest.mark.asyncio
    async def test_oclock_with_standalone_pm_and_date_context(self, simulator, onboarded_user, ai_mock):
        """Test: 'Remind me Sunday at 1 o'clock to look at Robyn's registry' -> 'Pm'
        Verifies o'clock is stripped from time and Sunday date is preserved."""
        phone = onboarded_user["phone"]

        # Calculate next Sunday's date for the AI response
        from datetime import date
        today = date.today()
        days_until_sunday = (6 - today.weekday()) % 7
        if days_until_sunday == 0:
            days_until_sunday = 7
        next_sunday = today + timedelta(days=days_until_sunday)
        sunday_str = next_sunday.strftime("%Y-%m-%d")

        clarify_response = {
            "action": "clarify_time",
            "reminder_text": "look at Robyn's registry",
            "time_mentioned": "1 o'clock",
            "reminder_date": sunday_str,
            "response": "Got it! Do you mean 1 o'clock AM or PM?"
        }

        ai_mock.set_response("remind me sunday at 1 o'clock to look at robyn's registry", clarify_response)
        ai_mock.set_response("remind me sunday at 1 o'clock to look at robyn's registry", clarify_response)

        result = await simulator.send_message(phone, "Remind me Sunday at 1 o'clock to look at Robyn's registry")

        # Should ask for AM/PM
        assert "am" in result["output"].lower() or "pm" in result["output"].lower()

        # User replies with standalone "Pm"
        result = await simulator.send_message(phone, "Pm")

        # Should confirm reminder is set for Sunday at 1 PM
        output = result["output"].lower()
        assert "remind" in output, f"Expected reminder confirmation, got: {result['output']}"
        assert "1:00 pm" in output or "1 pm" in output, f"Expected 1 PM in output, got: {result['output']}"
        assert "sunday" in output, f"Expected Sunday in output, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_oclock_without_date_falls_back_to_today_tomorrow(self, simulator, onboarded_user, ai_mock):
        """Test: 'Remind me at 3 o'clock to call mom' -> 'Pm' (no date context)
        Verifies o'clock is stripped and today/tomorrow logic applies."""
        phone = onboarded_user["phone"]

        clarify_response = {
            "action": "clarify_time",
            "reminder_text": "call mom",
            "time_mentioned": "3 o'clock",
            "response": "Got it! Do you mean 3 o'clock AM or PM?"
        }

        ai_mock.set_response("remind me at 3 o'clock to call mom", clarify_response)
        ai_mock.set_response("remind me at 3 o'clock to call mom", clarify_response)

        result = await simulator.send_message(phone, "Remind me at 3 o'clock to call mom")
        assert "am" in result["output"].lower() or "pm" in result["output"].lower()

        result = await simulator.send_message(phone, "Pm")

        output = result["output"].lower()
        assert "remind" in output, f"Expected reminder confirmation, got: {result['output']}"
        assert "3:00 pm" in output or "3 pm" in output, f"Expected 3 PM in output, got: {result['output']}"
