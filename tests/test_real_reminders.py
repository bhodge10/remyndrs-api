"""
Real OpenAI E2E Tests — Reminders

Tests reminder creation, AM/PM clarification, relative time, recurring, view, and delete
using real OpenAI API.

Run with:
    USE_REAL_OPENAI=true pytest tests/test_real_reminders.py -v --tb=short
"""

import os
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("USE_REAL_OPENAI", "").lower() != "true",
    reason="Requires USE_REAL_OPENAI=true for real OpenAI API calls"
)

PHONE = "+15559876543"


@pytest.fixture(autouse=True)
def clean_test_user_data():
    """Ensure a clean slate before and after each test."""
    from database import get_db_connection, return_db_connection

    def cleanup():
        conn = None
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("DELETE FROM conversation_analysis WHERE log_id IN (SELECT id FROM logs WHERE phone_number = %s)", (PHONE,))
            c.execute("DELETE FROM list_items WHERE phone_number = %s", (PHONE,))
            c.execute("DELETE FROM lists WHERE phone_number = %s", (PHONE,))
            c.execute("DELETE FROM reminders WHERE phone_number = %s", (PHONE,))
            c.execute("DELETE FROM recurring_reminders WHERE phone_number = %s", (PHONE,))
            c.execute("DELETE FROM memories WHERE phone_number = %s", (PHONE,))
            c.execute("DELETE FROM support_tickets WHERE phone_number = %s", (PHONE,))
            c.execute("DELETE FROM logs WHERE phone_number = %s", (PHONE,))
            c.execute("DELETE FROM users WHERE phone_number = %s", (PHONE,))
            c.execute("DELETE FROM onboarding_progress WHERE phone_number = %s", (PHONE,))
            conn.commit()
        except Exception as e:
            print(f"Cleanup error: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                return_db_connection(conn)

    cleanup()
    yield
    cleanup()


async def onboard_user(sim):
    """Run the onboarding flow and pre-configure daily summary."""
    results = await sim.complete_onboarding(PHONE)

    from models.user import is_user_onboarded, create_or_update_user
    assert is_user_onboarded(PHONE), \
        f"User not onboarded after flow. Last response: {results[-1]['output']}"

    create_or_update_user(
        PHONE,
        daily_summary_enabled=True,
        daily_summary_time="08:00"
    )
    return results


class TestRealReminders:
    """Reminder feature E2E tests using real OpenAI."""

    @pytest.mark.asyncio
    async def test_reminder_explicit_time(self, real_ai_simulator):
        """Create a reminder with an explicit AM time."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Remind me tomorrow at 9am to check email")
        output = result["output"].lower()
        assert any(w in output for w in ["remind", "set", "9", "email"]), \
            f"Expected reminder confirmation, got: {result['output']}"

        from models.reminder import get_user_reminders
        reminders = get_user_reminders(PHONE)
        assert len(reminders) >= 1, \
            f"Expected at least 1 reminder in DB, got {len(reminders)}"

    @pytest.mark.asyncio
    async def test_reminder_pm_specified(self, real_ai_simulator):
        """Create a reminder with an explicit PM time."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Remind me at 5pm to pick up groceries")
        output = result["output"].lower()
        assert any(w in output for w in ["remind", "set", "5", "groceries"]), \
            f"Expected reminder confirmation, got: {result['output']}"

        from models.reminder import get_user_reminders
        reminders = get_user_reminders(PHONE)
        assert len(reminders) >= 1, "No reminder found in DB"
        reminder_texts = [r[2].lower() for r in reminders]
        assert any("groceries" in t or "grocer" in t for t in reminder_texts), \
            f"Expected 'groceries' in reminder text, got: {reminder_texts}"

    @pytest.mark.asyncio
    async def test_reminder_ampm_clarification(self, real_ai_simulator):
        """Ambiguous time triggers AM/PM question, then confirm with PM."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Remind me at 3 to call mom")
        output = result["output"].lower()
        assert any(w in output for w in ["am", "pm", "morning", "afternoon"]), \
            f"Expected AM/PM clarification, got: {result['output']}"

        result = await sim.send_message(PHONE, "PM")
        output = result["output"].lower()
        assert any(w in output for w in ["3", "pm", "remind", "set", "mom"]), \
            f"Expected 3 PM confirmation, got: {result['output']}"

        from models.reminder import get_user_reminders
        reminders = get_user_reminders(PHONE)
        assert len(reminders) >= 1, "No reminder created after AM/PM clarification"

    @pytest.mark.asyncio
    async def test_reminder_ampm_morning(self, real_ai_simulator):
        """Ambiguous time resolved with 'morning' instead of 'AM'."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Remind me at 8 to take medicine")
        output = result["output"].lower()
        assert any(w in output for w in ["am", "pm", "morning", "afternoon"]), \
            f"Expected AM/PM clarification, got: {result['output']}"

        result = await sim.send_message(PHONE, "morning")
        output = result["output"].lower()
        assert any(w in output for w in ["8", "am", "morning", "remind", "set", "medicine"]), \
            f"Expected morning confirmation, got: {result['output']}"

        from models.reminder import get_user_reminders
        reminders = get_user_reminders(PHONE)
        assert len(reminders) >= 1, "No reminder created after morning clarification"

    @pytest.mark.asyncio
    async def test_relative_minutes(self, real_ai_simulator):
        """Create a reminder relative to now in minutes."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Remind me in 5 minutes to test this")
        output = result["output"].lower()
        assert any(w in output for w in ["remind", "set", "5", "minute", "test"]), \
            f"Expected reminder confirmation, got: {result['output']}"

        from models.reminder import get_user_reminders
        reminders = get_user_reminders(PHONE)
        assert len(reminders) >= 1, "No reminder found for relative minutes"

    @pytest.mark.asyncio
    async def test_relative_hours(self, real_ai_simulator):
        """Create a reminder relative to now in hours."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Remind me in 2 hours to call back")
        output = result["output"].lower()
        assert any(w in output for w in ["remind", "set", "2", "hour", "call"]), \
            f"Expected reminder confirmation, got: {result['output']}"

        from models.reminder import get_user_reminders
        reminders = get_user_reminders(PHONE)
        assert len(reminders) >= 1, "No reminder found for relative hours"

    @pytest.mark.asyncio
    async def test_relative_tomorrow(self, real_ai_simulator):
        """Create a reminder for tomorrow — may ask for time or auto-set."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Remind me tomorrow to pay rent")
        output = result["output"].lower()

        # AI may ask for a specific time, or set a default
        if any(w in output for w in ["what time", "when", "am", "pm"]):
            result = await sim.send_message(PHONE, "9am")
            output = result["output"].lower()

        assert any(w in output for w in ["remind", "set", "tomorrow", "rent", "9"]), \
            f"Expected reminder confirmation, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_recurring_daily(self, real_ai_simulator):
        """Create a daily recurring reminder."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Remind me every day at 9am to take vitamins")
        output = result["output"].lower()
        assert any(w in output for w in ["daily", "every", "9", "vitamin", "recurring", "repeat"]), \
            f"Expected recurring reminder confirmation, got: {result['output']}"

        from models.reminder import get_recurring_reminders
        recurring = get_recurring_reminders(PHONE)
        assert len(recurring) >= 1, \
            f"Expected at least 1 recurring reminder in DB, got {len(recurring)}"

    @pytest.mark.asyncio
    async def test_recurring_weekly(self, real_ai_simulator):
        """Create a weekly recurring reminder."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Remind me every Monday at 9am about team meeting")
        output = result["output"].lower()
        assert any(w in output for w in ["monday", "weekly", "meeting", "every", "recurring", "repeat"]), \
            f"Expected weekly recurring confirmation, got: {result['output']}"

        from models.reminder import get_recurring_reminders
        recurring = get_recurring_reminders(PHONE)
        assert len(recurring) >= 1, \
            f"Expected at least 1 recurring reminder in DB, got {len(recurring)}"

    @pytest.mark.asyncio
    async def test_view_reminders(self, real_ai_simulator):
        """Create a reminder then view all reminders."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Remind me in 30 minutes to stretch")

        result = await sim.send_message(PHONE, "Show me my reminders")
        output = result["output"].lower()
        assert any(w in output for w in ["stretch", "remind", "upcoming", "scheduled"]), \
            f"Expected reminders listed, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_delete_reminder(self, real_ai_simulator):
        """Create a reminder then delete it via DELETE REMINDERS keyword flow."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Remind me in 30 minutes to test deletion")

        from models.reminder import get_user_reminders
        reminders_before = get_user_reminders(PHONE)
        assert len(reminders_before) >= 1, "Reminder not created for delete test"

        result = await sim.send_message(PHONE, "DELETE REMINDERS")
        output = result["output"].lower()
        assert any(w in output for w in ["delete", "which", "select", "remind", "1"]), \
            f"Expected delete prompt, got: {result['output']}"

        result = await sim.send_message(PHONE, "1")
        output = result["output"].lower()
        # May ask for confirmation
        if any(w in output for w in ["confirm", "sure", "yes", "no"]):
            result = await sim.send_message(PHONE, "YES")
            output = result["output"].lower()

        assert any(w in output for w in ["deleted", "removed", "cancelled", "canceled"]), \
            f"Expected deletion confirmation, got: {result['output']}"

        # Verify no unsent reminders remain
        reminders_after = get_user_reminders(PHONE)
        unsent = [r for r in reminders_after if not r[4]]  # r[4] = is_sent
        assert len(unsent) == 0, \
            f"Expected 0 unsent reminders after delete, got {len(unsent)}"
