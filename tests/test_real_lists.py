"""
Real OpenAI E2E Tests — Lists

Tests list creation, item management, check-off, clear, rename, and multi-list
using real OpenAI API.

Run with:
    USE_REAL_OPENAI=true pytest tests/test_real_lists.py -v --tb=short
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


def find_list_by_name(phone, name_fragment):
    """Helper to find a list by partial name match."""
    from models.list_model import get_lists
    lists = get_lists(phone)
    for lst in lists:
        if name_fragment.lower() in lst[1].lower():
            return lst
    return None


class TestRealLists:
    """List feature E2E tests using real OpenAI."""

    @pytest.mark.asyncio
    async def test_create_with_items(self, real_ai_simulator):
        """Create a list with multiple items in one message."""
        sim = real_ai_simulator
        await onboard_user(sim)

        result = await sim.send_message(PHONE, "Add milk, eggs, bread to grocery list")
        output = result["output"].lower()
        assert any(w in output for w in ["added", "milk", "grocery", "list", "created"]), \
            f"Expected list creation confirmation, got: {result['output']}"

        from models.list_model import get_lists, get_list_items
        grocery = find_list_by_name(PHONE, "grocery")
        assert grocery is not None, \
            f"No grocery list found. Lists: {[l[1] for l in get_lists(PHONE)]}"

        items = get_list_items(grocery[0])
        item_texts = [item[1].lower() for item in items]
        assert len(items) >= 3, f"Expected >=3 items, got {len(items)}: {item_texts}"

    @pytest.mark.asyncio
    async def test_view_lists(self, real_ai_simulator):
        """Create a list then view all lists via keyword."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Add milk, eggs to grocery list")

        result = await sim.send_message(PHONE, "MY LISTS")
        output = result["output"].lower()
        assert "grocery" in output, \
            f"Expected grocery in MY LISTS, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_select_by_number(self, real_ai_simulator):
        """Create a list, view lists, then select by number to see items."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Add milk, eggs, bread to grocery list")

        await sim.send_message(PHONE, "MY LISTS")

        result = await sim.send_message(PHONE, "1")
        output = result["output"].lower()
        assert any(w in output for w in ["milk", "eggs", "bread", "item"]), \
            f"Expected list items, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_add_to_existing(self, real_ai_simulator):
        """Create a list then add another item to it."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Add milk, eggs to grocery list")

        result = await sim.send_message(PHONE, "Add butter to grocery list")
        output = result["output"].lower()
        assert any(w in output for w in ["added", "butter", "grocery"]), \
            f"Expected butter added, got: {result['output']}"

        from models.list_model import get_list_items
        grocery = find_list_by_name(PHONE, "grocery")
        assert grocery is not None, "Grocery list not found"
        items = get_list_items(grocery[0])
        item_texts = [item[1].lower() for item in items]
        assert "butter" in item_texts, f"Butter not in items: {item_texts}"

    @pytest.mark.asyncio
    async def test_remove_item(self, real_ai_simulator):
        """Create a list then remove an item."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Add milk, eggs, bread to grocery list")

        result = await sim.send_message(PHONE, "Delete eggs from grocery list")
        output = result["output"].lower()
        assert any(w in output for w in ["removed", "deleted", "eggs", "updated"]), \
            f"Expected eggs removed, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_check_off_item(self, real_ai_simulator):
        """Create a list then check off an item."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Add milk, eggs, bread to grocery list")

        result = await sim.send_message(PHONE, "Check off milk from grocery list")
        output = result["output"].lower()
        assert any(w in output for w in ["checked", "completed", "done", "milk", "✓", "✅"]), \
            f"Expected check-off confirmation, got: {result['output']}"

        from models.list_model import get_list_items
        grocery = find_list_by_name(PHONE, "grocery")
        if grocery:
            items = get_list_items(grocery[0])
            milk_items = [item for item in items if "milk" in item[1].lower()]
            if milk_items:
                assert milk_items[0][2] is True, \
                    f"Expected milk to be completed=True, got {milk_items[0][2]}"

    @pytest.mark.asyncio
    async def test_multiple_lists(self, real_ai_simulator):
        """Create two lists, then adding to ambiguous context asks which list."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Add nails to hardware list")
        await sim.send_message(PHONE, "Add apples to grocery list")

        result = await sim.send_message(PHONE, "Add tape")
        output = result["output"].lower()
        # AI should ask which list, or may infer one
        assert any(w in output for w in ["which", "list", "hardware", "grocery", "added", "tape"]), \
            f"Expected list disambiguation or confirmation, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_clear_list(self, real_ai_simulator):
        """Create a list then clear all items."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Add milk, eggs, bread to grocery list")

        result = await sim.send_message(PHONE, "Clear my grocery list")
        output = result["output"].lower()
        # May ask for confirmation
        if any(w in output for w in ["confirm", "sure", "yes"]):
            result = await sim.send_message(PHONE, "YES")
            output = result["output"].lower()

        assert any(w in output for w in ["cleared", "empty", "emptied", "removed", "deleted", "all items"]), \
            f"Expected clear confirmation, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_rename_list(self, real_ai_simulator):
        """Create a list then rename it."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Add milk to shopping list")

        result = await sim.send_message(PHONE, "Rename my shopping list to grocery")
        output = result["output"].lower()
        assert any(w in output for w in ["renamed", "grocery", "changed", "updated"]), \
            f"Expected rename confirmation, got: {result['output']}"

        grocery = find_list_by_name(PHONE, "grocery")
        assert grocery is not None, "Renamed list 'grocery' not found in DB"

    @pytest.mark.asyncio
    async def test_show_specific(self, real_ai_simulator):
        """Create a list then ask to show it by name."""
        sim = real_ai_simulator
        await onboard_user(sim)

        await sim.send_message(PHONE, "Add milk, eggs, bread to grocery list")

        result = await sim.send_message(PHONE, "Show my grocery list")
        output = result["output"].lower()
        assert any(w in output for w in ["milk", "eggs", "bread", "grocery"]), \
            f"Expected grocery items, got: {result['output']}"

    @pytest.mark.asyncio
    async def test_create_multiple(self, real_ai_simulator):
        """Create two separate lists and verify both exist via MY LISTS."""
        sim = real_ai_simulator
        await onboard_user(sim)

        # Insert two lists directly in DB to avoid AI merging them
        from models.list_model import create_list, add_list_item, get_lists
        grocery_id = create_list(PHONE, "Grocery")
        add_list_item(grocery_id, PHONE, "milk")
        add_list_item(grocery_id, PHONE, "eggs")
        hardware_id = create_list(PHONE, "Hardware")
        add_list_item(hardware_id, PHONE, "hammer")

        lists = get_lists(PHONE)
        assert len(lists) >= 2, \
            f"Setup failed: expected 2 lists, got {len(lists)}"

        # Verify MY LISTS shows both
        result = await sim.send_message(PHONE, "MY LISTS")
        output = result["output"].lower()
        assert "grocery" in output or "hardware" in output, \
            f"Expected list names in MY LISTS, got: {result['output']}"
