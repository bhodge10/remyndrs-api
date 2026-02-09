"""
Real OpenAI E2E Tests — Memories

Tests memory storage, retrieval, deletion, and special characters using real OpenAI API.

Run with:
    USE_REAL_OPENAI=true pytest tests/test_real_memories.py -v --tb=short
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


class TestRealMemories:
    """Memory feature E2E tests using real OpenAI."""

    @pytest.mark.asyncio
    async def test_store_simple(self, real_ai_simulator):
        """Store a simple memory and verify confirmation."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Remember my wifi password is Home2024")
        output = result["output"].lower()
        assert any(w in output for w in ["remember", "stored", "saved", "got it", "noted", "wifi", "home2024"]), \
            f"Expected memory confirmation, got: {result['output']}"

        from models.memory import get_memories
        memories = get_memories(PHONE)
        assert len(memories) >= 1, f"Expected at least 1 memory in DB, got {len(memories)}"

    @pytest.mark.asyncio
    async def test_retrieve(self, real_ai_simulator):
        """Store a memory then retrieve it by asking a question."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Remember my wifi password is Home2024")

        result = await sim.send_message(PHONE, "What's my wifi password?")
        output = result["output"].lower()
        assert "home2024" in output, \
            f"Expected wifi password in response, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_store_about_person(self, real_ai_simulator):
        """Store and retrieve a memory about a person."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Remember my doctor is Dr. Smith")
        output = result["output"].lower()
        assert any(w in output for w in ["remember", "stored", "saved", "got it", "noted", "doctor", "smith"]), \
            f"Expected memory confirmation, got: {result['output']}"

        result = await sim.send_message(PHONE, "Who is my doctor?")
        output = result["output"].lower()
        assert any(w in output for w in ["smith", "dr"]), \
            f"Expected doctor info, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_store_about_place(self, real_ai_simulator):
        """Store and retrieve a memory about a place."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Remember I parked on Level 3 Section B")
        output = result["output"].lower()
        assert any(w in output for w in ["remember", "stored", "saved", "got it", "noted", "parked", "level"]), \
            f"Expected parking memory confirmation, got: {result['output']}"

        result = await sim.send_message(PHONE, "Where did I park?")
        output = result["output"].lower()
        assert any(w in output for w in ["level 3", "section b", "level", "section"]), \
            f"Expected parking info, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_store_with_numbers(self, real_ai_simulator):
        """Store and retrieve a memory with numeric values."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Remember my locker combo is 42-15-33")
        output = result["output"].lower()
        assert any(w in output for w in ["remember", "stored", "saved", "got it", "noted", "locker", "combo"]), \
            f"Expected memory confirmation, got: {result['output']}"

        result = await sim.send_message(PHONE, "What's my locker combo?")
        output = result["output"].lower()
        assert any(w in output for w in ["42", "15", "33"]), \
            f"Expected combo numbers, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_view_all(self, real_ai_simulator):
        """Store multiple memories then view all via keyword."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Remember my wifi password is Home2024")
        await sim.send_message(PHONE, "Remember I parked on Level 3 Section B")

        result = await sim.send_message(PHONE, "MY MEMORIES")
        output = result["output"].lower()
        assert any(w in output for w in ["wifi", "park", "memor"]), \
            f"Expected memories listed, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_delete_keyword(self, real_ai_simulator):
        """Store a memory then delete via DELETE MEMORIES keyword flow."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Remember my favorite color is blue")

        from models.memory import get_memories
        memories_before = get_memories(PHONE)
        assert len(memories_before) >= 1, "Memory not stored"

        result = await sim.send_message(PHONE, "DELETE MEMORIES")
        output = result["output"].lower()
        assert any(w in output for w in ["delete", "which", "select", "memor", "1"]), \
            f"Expected delete prompt, got: {result['output']}"

        result = await sim.send_message(PHONE, "1")
        output = result["output"].lower()
        # May ask for confirmation or delete directly
        if any(w in output for w in ["confirm", "sure", "yes"]):
            result = await sim.send_message(PHONE, "YES")
            output = result["output"].lower()

        assert any(w in output for w in ["deleted", "removed", "gone", "no memor"]), \
            f"Expected deletion confirmation, got: {result['output']}"

        memories_after = get_memories(PHONE)
        # Onboarding auto-creates a signup memory, so filter it out
        user_memories = [m for m in memories_after if '"auto_created"' not in (m[2] or '')]
        assert len(user_memories) == 0, \
            f"Expected 0 user memories after delete, got {len(user_memories)}: {user_memories}"

    @pytest.mark.asyncio
    async def test_delete_via_ai(self, real_ai_simulator):
        """Store a memory then ask AI to forget it."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Remember my favorite color is blue")

        from models.memory import get_memories
        assert len(get_memories(PHONE)) >= 1, "Memory not stored"

        result = await sim.send_message(PHONE, "Forget my favorite color")
        output = result["output"].lower()
        # AI may ask for confirmation or delete directly
        if any(w in output for w in ["confirm", "sure", "delete", "which"]):
            result = await sim.send_message(PHONE, "YES")
            output = result["output"].lower()

        assert any(w in output for w in ["deleted", "removed", "forgot", "forgotten", "gone", "no longer"]), \
            f"Expected deletion confirmation, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_store_and_recall(self, real_ai_simulator):
        """Store a memory and recall it immediately."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Remember our meeting room is Conference Room 4B")

        result = await sim.send_message(PHONE, "What's our meeting room?")
        output = result["output"].lower()
        assert any(w in output for w in ["4b", "conference", "room"]), \
            f"Expected meeting room info, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_special_characters(self, real_ai_simulator):
        """Store and retrieve a memory with special characters (email)."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Remember my email is test.user@example.com")

        result = await sim.send_message(PHONE, "What's my email?")
        output = result["output"].lower()
        assert any(w in output for w in ["test.user", "example.com", "test", "example"]), \
            f"Expected email in response, got: {result['output']}"
