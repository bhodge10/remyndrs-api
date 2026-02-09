"""
Real OpenAI E2E Tests — Edge Cases

Tests typos, gibberish, whitespace, long messages, special characters, and keyword
commands (INFO, SUPPORT, FEEDBACK) using real OpenAI API.

Run with:
    USE_REAL_OPENAI=true pytest tests/test_real_edge_cases.py -v --tb=short
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


class TestRealEdgeCases:
    """Edge case E2E tests using real OpenAI."""

    @pytest.mark.asyncio
    async def test_typos_reminder(self, real_ai_simulator):
        """Message with typos should still be processed."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "reminde me tmrw at 3pm to call doctor")
        output = result["output"].lower()
        assert len(output) > 10, \
            f"Expected meaningful response to typo, got: {result['output']}"
        assert "error" not in output or "sorry" in output, \
            f"Got unexpected error: {result['output']}"

    @pytest.mark.asyncio
    async def test_typos_memory(self, real_ai_simulator):
        """Memory request with typos should still work."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "rember my wifi pasword is test123")
        output = result["output"].lower()
        assert len(output) > 10, \
            f"Expected meaningful response, got: {result['output']}"
        # Should either store the memory or ask for clarification
        assert any(w in output for w in ["remember", "stored", "saved", "got it", "noted", "wifi", "test123", "did you mean"]), \
            f"Expected memory handling, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_gibberish(self, real_ai_simulator):
        """Gibberish input should get a graceful response."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "asdfghjkl")
        output = result["output"].lower()
        assert len(output) > 5, \
            f"Expected graceful response to gibberish, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_whitespace(self, real_ai_simulator):
        """Whitespace-only input should get some response."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "   ")
        output = result["output"].lower()
        assert len(output) > 0, \
            f"Expected some response to whitespace, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_long_message(self, real_ai_simulator):
        """Long message should be processed without issues."""
        sim = real_ai_simulator
        await onboard_user(sim)

        long_msg = "Remind me tomorrow at 2pm to " + "do something important " * 6 + "and finish everything"
        result = await sim.send_message(PHONE, long_msg)
        output = result["output"].lower()
        assert len(output) > 10, \
            f"Expected response to long message, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_special_chars(self, real_ai_simulator):
        """Store and retrieve a memory with special characters."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Remember my password is P@ss!w0rd#123$")

        result = await sim.send_message(PHONE, "What's my password?")
        output = result["output"].lower()
        assert any(w in output for w in ["p@ss", "w0rd", "123", "password"]), \
            f"Expected password fragments, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_info_command(self, real_ai_simulator):
        """INFO keyword should return help text."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "INFO")
        output = result["output"].lower()
        assert any(w in output for w in ["remind", "memor", "list", "help", "command", "how"]), \
            f"Expected help text from INFO, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_question_mark(self, real_ai_simulator):
        """? keyword should return help text."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "?")
        output = result["output"].lower()
        assert any(w in output for w in ["remind", "memor", "list", "help", "command", "how"]), \
            f"Expected help text from ?, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_support_exit(self, real_ai_simulator):
        """SUPPORT keyword creates a ticket, EXIT leaves support mode."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "SUPPORT I have a question about reminders")
        output = result["output"].lower()
        assert any(w in output for w in ["support", "ticket", "team", "help"]), \
            f"Expected support confirmation, got: {result['output']}"

        from services.support_service import get_active_support_ticket
        active = get_active_support_ticket(PHONE)
        if active:
            result = await sim.send_message(PHONE, "EXIT")
            output = result["output"].lower()
            assert any(w in output for w in ["exit", "exited", "left", "support", "ticket", "closed"]), \
                f"Expected exit confirmation, got: {result['output']}"
        else:
            # Ticket was created but support mode may have auto-closed
            from database import get_db_connection, return_db_connection
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM support_tickets WHERE phone_number = %s", (PHONE,))
            count = c.fetchone()[0]
            return_db_connection(conn)
            assert count >= 1, "Expected at least 1 support ticket in DB"

    @pytest.mark.asyncio
    async def test_feedback(self, real_ai_simulator):
        """FEEDBACK keyword should acknowledge the feedback."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "FEEDBACK I love this app!")
        output = result["output"].lower()
        assert any(w in output for w in ["feedback", "thank", "appreciate"]), \
            f"Expected feedback confirmation, got: {result['output']}"
