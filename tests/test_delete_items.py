"""
Tests for list-item and memory deletion.

Covers the SMS keyword path (Delete N + YES), the AI delete_item /
delete_memory actions, partial name matching, HTML-encoded leftover
list names, and confirmation follow-up turns.
"""

import pytest

from models.list_model import (
    create_list, add_list_item, get_list_items, get_list_by_name,
    resolve_item_for_delete, delete_list_item_from_pending,
)
from models.memory import save_memory, get_memories
from models.user import create_or_update_user


def _item_texts(list_id):
    return [item[1] for item in get_list_items(list_id)]


class TestOwnedListDeleteByNumber:
    """Show a list, Delete N, YES should remove that item."""

    @pytest.mark.asyncio
    async def test_show_then_delete_by_number_then_yes(self, simulator, onboarded_user, ai_mock):
        phone = onboarded_user["phone"]
        list_id = create_list(phone, "Grocery List")
        add_list_item(list_id, phone, "Milk")
        add_list_item(list_id, phone, "Eggs")

        ai_mock.set_response("show grocery list", {
            "action": "show_list",
            "list_name": "Grocery List",
        })
        await simulator.send_message(phone, "Show Grocery List")

        result = await simulator.send_message(phone, "Delete 1")
        out = result["output"].lower()
        assert "milk" in out and "yes" in out, f"Expected confirmation. Got: {result['output']}"

        confirm = await simulator.send_message(phone, "YES")
        assert "couldn't delete" not in confirm["output"].lower(), confirm["output"]
        assert "removed" in confirm["output"].lower()
        assert "Milk" not in _item_texts(list_id)
        assert "Eggs" in _item_texts(list_id)


class TestAiDeleteItemResolution:
    """AI delete_item must resolve the real list/item before confirming."""

    @pytest.mark.asyncio
    async def test_exact_names_then_yes(self, simulator, onboarded_user, ai_mock):
        phone = onboarded_user["phone"]
        list_id = create_list(phone, "Grocery List")
        add_list_item(list_id, phone, "Milk")
        add_list_item(list_id, phone, "Eggs")

        ai_mock.set_response("delete eggs from grocery list", {
            "action": "delete_item",
            "list_name": "Grocery List",
            "item_text": "Eggs",
        })
        result = await simulator.send_message(phone, "Delete eggs from grocery list")
        assert "eggs" in result["output"].lower()
        assert "yes" in result["output"].lower()

        confirm = await simulator.send_message(phone, "YES")
        assert "couldn't delete" not in confirm["output"].lower(), confirm["output"]
        assert "Eggs" not in _item_texts(list_id)
        assert "Milk" in _item_texts(list_id)

    @pytest.mark.asyncio
    async def test_partial_list_name_grocery_vs_grocery_list(self, simulator, onboarded_user, ai_mock):
        """AI often extracts 'grocery' when the stored name is 'Grocery List'."""
        phone = onboarded_user["phone"]
        list_id = create_list(phone, "Grocery List")
        add_list_item(list_id, phone, "Milk")
        add_list_item(list_id, phone, "Bread")

        ai_mock.set_response("remove milk from grocery", {
            "action": "delete_item",
            "list_name": "grocery",
            "item_text": "milk",
        })
        result = await simulator.send_message(phone, "Remove milk from grocery")
        assert "couldn't find" not in result["output"].lower(), result["output"]
        assert "milk" in result["output"].lower()
        assert "yes" in result["output"].lower()

        confirm = await simulator.send_message(phone, "YES")
        assert "couldn't delete" not in confirm["output"].lower(), confirm["output"]
        assert "Milk" not in _item_texts(list_id)

    @pytest.mark.asyncio
    async def test_partial_item_name(self, simulator, onboarded_user, ai_mock):
        phone = onboarded_user["phone"]
        list_id = create_list(phone, "Grocery List")
        add_list_item(list_id, phone, "2% milk")
        add_list_item(list_id, phone, "Eggs")

        ai_mock.set_response("remove milk from grocery list", {
            "action": "delete_item",
            "list_name": "Grocery List",
            "item_text": "milk",
        })
        result = await simulator.send_message(phone, "Remove milk from grocery list")
        assert "couldn't find" not in result["output"].lower(), result["output"]
        assert "2% milk" in result["output"].lower()

        await simulator.send_message(phone, "YES")
        texts = [t.lower() for t in _item_texts(list_id)]
        assert "2% milk" not in texts
        assert "eggs" in texts

    @pytest.mark.asyncio
    async def test_uses_last_active_list_when_ai_omits_list_name(self, simulator, onboarded_user, ai_mock):
        phone = onboarded_user["phone"]
        list_id = create_list(phone, "Grocery List")
        add_list_item(list_id, phone, "Milk")
        add_list_item(list_id, phone, "Eggs")
        create_or_update_user(phone, last_active_list="Grocery List")

        ai_mock.set_response("delete eggs", {
            "action": "delete_item",
            "list_name": None,
            "item_text": "Eggs",
        })
        result = await simulator.send_message(phone, "Delete eggs")
        assert "couldn't find" not in result["output"].lower(), result["output"]
        assert "eggs" in result["output"].lower()

        await simulator.send_message(phone, "YES")
        assert "Eggs" not in _item_texts(list_id)

    @pytest.mark.asyncio
    async def test_does_not_confirm_when_item_missing(self, simulator, onboarded_user, ai_mock):
        phone = onboarded_user["phone"]
        list_id = create_list(phone, "Grocery List")
        add_list_item(list_id, phone, "Milk")

        ai_mock.set_response("delete tofu from grocery list", {
            "action": "delete_item",
            "list_name": "Grocery List",
            "item_text": "tofu",
        })
        result = await simulator.send_message(phone, "Delete tofu from grocery list")
        assert "couldn't find" in result["output"].lower(), result["output"]
        assert "Milk" in _item_texts(list_id)

    @pytest.mark.asyncio
    async def test_html_encoded_list_name_lookup(self, simulator, onboarded_user, ai_mock):
        """Leftover encoded names (issue #15) should still match user/AI apostrophes."""
        phone = onboarded_user["phone"]
        list_id = create_list(phone, "Sam&#39;s Club")
        add_list_item(list_id, phone, "Paper towels")

        found = get_list_by_name(phone, "Sam's Club")
        assert found is not None, "get_list_by_name should unescape leftover HTML entities"

        ai_mock.set_response("remove paper towels from sam's club", {
            "action": "delete_item",
            "list_name": "Sam's Club",
            "item_text": "Paper towels",
        })
        result = await simulator.send_message(phone, "Remove paper towels from Sam's Club")
        assert "couldn't find" not in result["output"].lower(), result["output"]

        await simulator.send_message(phone, "YES")
        assert _item_texts(list_id) == []


class TestMemoryDelete:
    @pytest.mark.asyncio
    async def test_show_memories_then_delete_by_number(self, simulator, onboarded_user, ai_mock):
        phone = onboarded_user["phone"]
        save_memory(phone, "wifi password is Home2024", {})

        result = await simulator.send_message(phone, "SHOW MEMORIES")
        assert "wifi" in result["output"].lower()

        confirm_prompt = await simulator.send_message(phone, "Delete 1")
        assert "yes" in confirm_prompt["output"].lower() or "delete" in confirm_prompt["output"].lower()

        confirm = await simulator.send_message(phone, "YES")
        assert "couldn't delete" not in confirm["output"].lower(), confirm["output"]
        assert "deleted" in confirm["output"].lower()
        leftover = [m for m in get_memories(phone) if "wifi" in (m[1] or "").lower()]
        assert leftover == []

    @pytest.mark.asyncio
    async def test_ai_delete_memory_accepts_query_field(self, simulator, onboarded_user, ai_mock):
        """The model (and some tests) send 'query' instead of 'search_term'."""
        phone = onboarded_user["phone"]
        save_memory(phone, "wifi password is Home2024", {})

        ai_mock.set_response("forget my wifi password", {
            "action": "delete_memory",
            "query": "wifi password",
        })
        result = await simulator.send_message(phone, "Forget my wifi password")
        assert "wifi" in result["output"].lower()
        assert "yes" in result["output"].lower()

        confirm = await simulator.send_message(phone, "YES")
        assert "deleted" in confirm["output"].lower()
        leftover = [m for m in get_memories(phone) if "wifi" in (m[1] or "").lower()]
        assert leftover == []

    @pytest.mark.asyncio
    async def test_ai_delete_memory_yes_with_trailing_whitespace(self, simulator, onboarded_user, ai_mock):
        phone = onboarded_user["phone"]
        save_memory(phone, "locker combo is 42-15-33", {})

        ai_mock.set_response("forget my locker combo", {
            "action": "delete_memory",
            "search_term": "locker",
        })
        await simulator.send_message(phone, "Forget my locker combo")

        confirm = await simulator.send_message(phone, "YES ")
        assert "couldn't delete" not in confirm["output"].lower(), confirm["output"]
        assert "deleted" in confirm["output"].lower()


class TestDeleteHelpers:
    def test_resolve_partial_and_encoded_names(self, onboarded_user):
        phone = onboarded_user["phone"]
        list_id = create_list(phone, "Sam&#39;s Club")
        add_list_item(list_id, phone, "Milk")

        resolved = resolve_item_for_delete(phone, list_name="Sam's Club", item_text="milk")
        assert resolved is not None
        assert not resolved.get("ambiguous")
        assert resolved["list_id"] == list_id
        assert resolved["item_text"] == "Milk"

        ok = delete_list_item_from_pending(phone, {
            "list_id": list_id,
            "list_name": resolved["list_name"],
            "text": resolved["item_text"],
        })
        assert ok is True
        assert _item_texts(list_id) == []
