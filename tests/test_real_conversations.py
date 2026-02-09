"""
Real OpenAI E2E Tests — Conversations

Tests context switching, cancel commands, multi-turn flows, and rapid action switches
using real OpenAI API.

Run with:
    USE_REAL_OPENAI=true pytest tests/test_real_conversations.py -v --tb=short
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


class TestRealConversations:
    """Conversation flow E2E tests using real OpenAI."""

    @pytest.mark.asyncio
    async def test_ampm_then_cancel(self, real_ai_simulator):
        """Start a reminder with ambiguous time, then cancel during AM/PM prompt."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Remind me at 6 to exercise")
        output = result["output"].lower()
        assert any(w in output for w in ["am", "pm", "morning", "afternoon"]), \
            f"Expected AM/PM clarification, got: {result['output']}"

        result = await sim.send_message(PHONE, "cancel")
        output = result["output"].lower()
        assert any(w in output for w in ["cancel", "cancelled", "canceled", "ok", "never mind", "no problem"]), \
            f"Expected cancellation confirmation, got: {result['output']}"

        from models.reminder import get_user_reminders
        reminders = get_user_reminders(PHONE)
        unsent = [r for r in reminders if not r[4]]
        assert len(unsent) == 0, \
            f"Expected no pending reminders after cancel, got {len(unsent)}"

    @pytest.mark.asyncio
    async def test_ampm_interrupted(self, real_ai_simulator):
        """Start AM/PM clarification, then interrupt with a different action."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Remind me at 3 to call mom")
        output = result["output"].lower()
        assert any(w in output for w in ["am", "pm", "morning", "afternoon"]), \
            f"Expected AM/PM clarification, got: {result['output']}"

        # Interrupt with a completely different action
        result = await sim.send_message(PHONE, "Remember my wifi password is Test123")
        output = result["output"].lower()
        # Should handle the new request — either store the memory or still ask AM/PM
        assert len(output) > 10, \
            f"Expected meaningful response, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_delete_flow_cancel(self, real_ai_simulator):
        """Start delete flow but cancel at the number selection step."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Remind me in 30 minutes to test deletion")

        from models.reminder import get_user_reminders
        reminders_before = get_user_reminders(PHONE)
        assert len(reminders_before) >= 1, "Reminder not created"

        result = await sim.send_message(PHONE, "DELETE REMINDERS")
        output = result["output"].lower()
        assert any(w in output for w in ["delete", "which", "select", "remind", "1"]), \
            f"Expected delete prompt, got: {result['output']}"

        # Cancel instead of selecting a number
        result = await sim.send_message(PHONE, "cancel")
        output = result["output"].lower()
        assert any(w in output for w in ["cancel", "ok", "no problem", "never mind", "kept"]), \
            f"Expected cancel confirmation, got: {result['output']}"

        # Reminder should still exist
        reminders_after = get_user_reminders(PHONE)
        unsent_after = [r for r in reminders_after if not r[4]]
        assert len(unsent_after) >= 1, \
            "Reminder was deleted despite cancellation"

    @pytest.mark.asyncio
    async def test_rapid_switches(self, real_ai_simulator):
        """Rapidly switch between different action types."""
        sim = real_ai_simulator
        await onboard_user(sim)

        # Cancel anything pending
        await sim.send_message(PHONE, "cancel")

        # Store a memory
        result = await sim.send_message(PHONE, "Remember my pin is 1234")
        output = result["output"].lower()
        assert any(w in output for w in ["remember", "stored", "saved", "got it", "noted", "pin"]), \
            f"Expected memory confirmation, got: {result['output']}"

        # Add a list item
        result = await sim.send_message(PHONE, "Add apples to fruit list")
        output = result["output"].lower()
        assert any(w in output for w in ["added", "apple", "fruit", "list", "created"]), \
            f"Expected list confirmation, got: {result['output']}"

        # Create a reminder
        result = await sim.send_message(PHONE, "Remind me in 10 minutes to check mail")
        output = result["output"].lower()
        assert any(w in output for w in ["remind", "set", "10", "minute", "mail"]), \
            f"Expected reminder confirmation, got: {result['output']}"

        # Verify all three actions persisted
        from models.memory import get_memories
        from models.list_model import get_lists
        from models.reminder import get_user_reminders
        assert len(get_memories(PHONE)) >= 1, "Memory not stored"
        assert len(get_lists(PHONE)) >= 1, "List not created"
        assert len(get_user_reminders(PHONE)) >= 1, "Reminder not created"

    @pytest.mark.asyncio
    async def test_multiple_items_one_msg(self, real_ai_simulator):
        """Add multiple items to a list in a single message."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Add apples, bananas, oranges to fruit list")
        output = result["output"].lower()
        assert any(w in output for w in ["added", "apple", "banana", "orange", "fruit"]), \
            f"Expected items added, got: {result['output']}"

        from models.list_model import get_list_items
        from models.list_model import get_lists
        lists = get_lists(PHONE)
        fruit = None
        for lst in lists:
            if "fruit" in lst[1].lower():
                fruit = lst
                break
        assert fruit is not None, f"Fruit list not found. Lists: {[l[1] for l in lists]}"

        items = get_list_items(fruit[0])
        assert len(items) >= 3, \
            f"Expected >=3 items in fruit list, got {len(items)}: {[i[1] for i in items]}"

    @pytest.mark.asyncio
    async def test_cancel_list_selection(self, real_ai_simulator):
        """Create two lists, trigger disambiguation, then cancel."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Add nails to hardware list")
        await sim.send_message(PHONE, "Add milk to grocery list")

        result = await sim.send_message(PHONE, "Add tape")
        output = result["output"].lower()

        # If AI asks which list, cancel
        if any(w in output for w in ["which", "list"]):
            result = await sim.send_message(PHONE, "cancel")
            output = result["output"].lower()
            assert any(w in output for w in ["cancel", "ok", "no problem", "never mind"]), \
                f"Expected cancel response, got: {result['output']}"

            # Verify tape wasn't added to any list
            from models.list_model import get_lists, get_list_items
            for lst in get_lists(PHONE):
                items = get_list_items(lst[0])
                item_texts = [i[1].lower() for i in items]
                assert "tape" not in item_texts, \
                    f"Tape should not be in {lst[1]} after cancel"

    @pytest.mark.asyncio
    async def test_onboarding_then_action(self, real_ai_simulator):
        """Complete onboarding then immediately use a feature."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Remember my PIN is 1234")
        output = result["output"].lower()
        assert any(w in output for w in ["remember", "stored", "saved", "got it", "noted", "pin"]), \
            f"Expected memory stored right after onboarding, got: {result['output']}"

        from models.memory import get_memories
        memories = get_memories(PHONE)
        assert len(memories) >= 1, "Memory not stored after fresh onboarding"

    @pytest.mark.asyncio
    async def test_yes_without_context(self, real_ai_simulator):
        """Send YES with no pending state — should handle gracefully."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "YES")
        output = result["output"].lower()
        # Should not crash — respond gracefully
        assert len(output) > 5, \
            f"Expected graceful response to orphan YES, got: {result['output']}"
        assert "error" not in output, \
            f"Got error response to orphan YES: {result['output']}"
