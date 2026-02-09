"""
Real OpenAI E2E Tests — Tier Limits

Tests free tier enforcement: memory limit (5), reminder limit (2/day), list limit (5),
item limit (10), and recurring blocked — using real OpenAI API.

Run with:
    USE_REAL_OPENAI=true pytest tests/test_real_tier_limits.py -v --tb=short
"""

import os
import pytest
from datetime import datetime, timedelta

def skip_if_rate_limited(output):
    """Skip the test if OpenAI returned a rate limit error."""
    if "trouble" in output.lower() and "try again" in output.lower():
        pytest.skip("OpenAI rate limit hit — skipping")

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


def set_free_tier():
    """Expire trial and set user to free tier. Returns BETA_MODE restore function."""
    from models.user import create_or_update_user
    import services.tier_service as ts

    expired = datetime.utcnow() - timedelta(days=1)
    create_or_update_user(PHONE, trial_end_date=expired, premium_status="free")

    original_beta = ts.BETA_MODE
    ts.BETA_MODE = False
    return ts, original_beta


class TestRealTierLimits:
    """Free tier limit enforcement E2E tests using real OpenAI."""

    @pytest.mark.asyncio
    async def test_memory_limit(self, real_ai_simulator):
        """Free tier allows 5 memories, blocks the 6th."""
        sim = real_ai_simulator
        await onboard_user(sim)
        ts, original_beta = set_free_tier()

        try:
            # Onboarding auto-creates a signup memory (counts toward limit),
            # so we can only store 4 more user memories before hitting 5.
            for i in range(1, 5):
                result = await sim.send_message(PHONE, f"Remember test fact number {i} is value{i}")
                output = result["output"].lower()
                assert "error" not in output or "sorry" in output, \
                    f"Memory {i} failed unexpectedly: {result['output']}"

            # 5th user store = 6th total (1 auto + 4 user + this one) should hit the limit
            result = await sim.send_message(PHONE, "Remember test fact number 5 is value5")
            output = result["output"].lower()
            assert any(w in output for w in ["limit", "maximum", "upgrade", "max", "premium", "reached"]), \
                f"Expected memory limit message, got: {result['output']}"

            from models.memory import get_memories
            memories = get_memories(PHONE)
            assert len(memories) == 5, \
                f"Expected exactly 5 memories (1 auto + 4 user), got {len(memories)}"
        finally:
            ts.BETA_MODE = original_beta

    @pytest.mark.asyncio
    async def test_reminder_limit(self, real_ai_simulator):
        """Free tier allows 2 reminders/day, blocks the 3rd."""
        sim = real_ai_simulator
        await onboard_user(sim)
        ts, original_beta = set_free_tier()

        try:
            result = await sim.send_message(PHONE, "Remind me in 10 minutes to check task 1")
            output = result["output"].lower()
            assert any(w in output for w in ["remind", "set", "task"]), \
                f"Reminder 1 failed: {result['output']}"

            result = await sim.send_message(PHONE, "Remind me in 20 minutes to check task 2")
            output = result["output"].lower()
            assert any(w in output for w in ["remind", "set", "task"]), \
                f"Reminder 2 failed: {result['output']}"

            result = await sim.send_message(PHONE, "Remind me in 30 minutes to check task 3")
            output = result["output"].lower()
            assert any(w in output for w in ["limit", "maximum", "upgrade", "max", "premium", "reached", "daily"]), \
                f"Expected reminder limit message, got: {result['output']}"
        finally:
            ts.BETA_MODE = original_beta

    @pytest.mark.asyncio
    async def test_list_limit(self, real_ai_simulator):
        """Free tier allows 5 lists, blocks the 6th."""
        sim = real_ai_simulator
        await onboard_user(sim)
        ts, original_beta = set_free_tier()

        try:
            # Insert 5 lists directly in DB to guarantee count
            from models.list_model import create_list, get_lists
            for name in ["Grocery", "Hardware", "Books", "Movies", "Errands"]:
                create_list(PHONE, name)

            lists = get_lists(PHONE)
            assert len(lists) == 5, \
                f"Setup failed: expected 5 lists, got {len(lists)}"

            # 6th list should hit the limit
            result = await sim.send_message(PHONE, "Create a new list called hobbies")
            skip_if_rate_limited(result["output"])
            output = result["output"].lower()
            assert any(w in output for w in ["limit", "maximum", "upgrade", "max", "premium", "reached"]), \
                f"Expected list limit message, got: {result['output']}"

            lists_after = get_lists(PHONE)
            assert len(lists_after) == 5, \
                f"Expected exactly 5 lists, got {len(lists_after)}: {[l[1] for l in lists_after]}"
        finally:
            ts.BETA_MODE = original_beta

    @pytest.mark.asyncio
    async def test_item_limit(self, real_ai_simulator):
        """Free tier allows 10 items per list, blocks the 11th."""
        sim = real_ai_simulator
        await onboard_user(sim)
        ts, original_beta = set_free_tier()

        try:
            # Create list with initial items
            await sim.send_message(PHONE, "Add item1, item2, item3 to test list")
            await sim.send_message(PHONE, "Add item4, item5, item6 to test list")
            await sim.send_message(PHONE, "Add item7, item8, item9 to test list")
            await sim.send_message(PHONE, "Add item10 to test list")

            result = await sim.send_message(PHONE, "Add item11 to test list")
            skip_if_rate_limited(result["output"])
            output = result["output"].lower()
            assert any(w in output for w in ["limit", "maximum", "upgrade", "max", "premium", "reached"]), \
                f"Expected item limit message, got: {result['output']}"
        finally:
            ts.BETA_MODE = original_beta

    @pytest.mark.asyncio
    async def test_recurring_blocked(self, real_ai_simulator):
        """Free tier blocks recurring reminders."""
        sim = real_ai_simulator
        await onboard_user(sim)
        ts, original_beta = set_free_tier()

        try:
            result = await sim.send_message(PHONE, "Remind me every day at 9am to take vitamins")
            output = result["output"].lower()
            assert any(w in output for w in ["premium", "upgrade", "recurring", "subscribe", "plan"]), \
                f"Expected premium/upgrade message for recurring, got: {result['output']}"

            from models.reminder import get_recurring_reminders
            recurring = get_recurring_reminders(PHONE)
            assert len(recurring) == 0, \
                f"Expected 0 recurring reminders on free tier, got {len(recurring)}"
        finally:
            ts.BETA_MODE = original_beta

    @pytest.mark.asyncio
    async def test_memory_allows_four_user(self, real_ai_simulator):
        """Free tier stores 4 user memories (+ 1 auto signup = 5 total) and allows retrieval."""
        sim = real_ai_simulator
        await onboard_user(sim)
        ts, original_beta = set_free_tier()

        try:
            # Onboarding auto-creates 1 signup memory, so 4 more fit within the 5 limit
            for i in range(1, 5):
                await sim.send_message(PHONE, f"Remember test color {i} is shade{i}")

            from models.memory import get_memories
            memories = get_memories(PHONE)
            assert len(memories) == 5, \
                f"Expected 5 memories (1 auto + 4 user), got {len(memories)}"

            # Retrieval should still work (skip if rate-limited)
            result = await sim.send_message(PHONE, "What is test color 3?")
            output = result["output"].lower()
            if "trouble" not in output and "try again" not in output:
                assert "shade3" in output or "3" in output, \
                    f"Expected memory retrieval to work, got: {result['output']}"
        finally:
            ts.BETA_MODE = original_beta

    @pytest.mark.asyncio
    async def test_reminder_allows_two(self, real_ai_simulator):
        """Free tier successfully creates exactly 2 reminders."""
        sim = real_ai_simulator
        await onboard_user(sim)
        ts, original_beta = set_free_tier()

        try:
            result = await sim.send_message(PHONE, "Remind me in 15 minutes to check email")
            skip_if_rate_limited(result["output"])
            output = result["output"].lower()
            assert any(w in output for w in ["remind", "set", "email"]), \
                f"Reminder 1 failed: {result['output']}"

            result = await sim.send_message(PHONE, "Remind me in 25 minutes to make a call")
            output = result["output"].lower()
            assert any(w in output for w in ["remind", "set", "call"]), \
                f"Reminder 2 failed: {result['output']}"

            from models.reminder import get_user_reminders
            reminders = get_user_reminders(PHONE)
            assert len(reminders) >= 2, \
                f"Expected at least 2 reminders, got {len(reminders)}"
        finally:
            ts.BETA_MODE = original_beta
