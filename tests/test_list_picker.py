"""
Tests for SHOW LISTS number-picker replies.

Production bug: after SHOW LISTS ("Reply with a number to see that list"),
the user texted "Show 3" expecting list #3 and instead got a reminder-delete
confirmation. The picker only matched /^\d+$/, so "Show 3" fell through to
the AI which emitted delete_reminder for scheduled reminder #3.
"""

import json
from datetime import datetime, timedelta

import pytest

from models.list_model import create_list, add_list_item, get_list_items
from models.reminder import save_reminder, get_user_reminders
from models.memory import save_memory, get_memories
from models.user import (
    create_or_update_user,
    get_pending_reminder_delete,
    get_pending_memory_delete,
)
from routes.handlers.lists import parse_list_picker_reply, get_all_lists_with_shared


CAMPING_ITEMS = [
    "tent",
    "sleeping bag",
    "camp stove",
    "lantern",
    "cooler",
]


def _seed_three_lists_camping_is_third(phone):
    """Create 3 lists so camping is row 3 in SHOW LISTS order.

    get_lists() is ORDER BY created_at DESC (newest first), so create
    camping first (oldest → last → #3).
    """
    camping_id = create_list(phone, "camping list")
    create_list(phone, "Home Depot list")
    create_list(phone, "Vancouver expense list")
    for item in CAMPING_ITEMS:
        add_list_item(camping_id, phone, item)

    all_lists = get_all_lists_with_shared(phone)
    names = [lst["list_name"] for lst in all_lists]
    assert names[2] == "camping list", f"Expected camping list at #3, got {names}"
    return camping_id


def _seed_succeeded_reminder(phone):
    save_reminder(
        phone,
        "You've succeeded.",
        (datetime.utcnow() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
    )
    reminders = get_user_reminders(phone)
    assert reminders, "Expected a pending reminder for the hijack fixture"
    return reminders[0]


def _assert_not_delete_loop(output):
    lower = output.lower()
    assert "still need" not in lower, f"Re-prompted pending delete: {output}"
    assert "reply yes" not in lower, f"Asked to confirm a delete: {output}"
    assert "reply 1 to confirm" not in lower, f"Asked to confirm a delete: {output}"


def _assert_camping_list_not_delete(output):
    _assert_not_delete_loop(output)
    lower = output.lower()
    assert "delete reminder" not in lower, f"Opened delete-reminder instead of list: {output}"
    assert "camping list" in lower, f"Expected camping list contents, got: {output}"
    for item in CAMPING_ITEMS:
        assert item in lower, f"Expected camping item '{item}' in: {output}"


def _assert_lists_menu_not_delete(output):
    _assert_not_delete_loop(output)
    lower = output.lower()
    assert "camping list" in lower, f"Expected lists menu, got: {output}"
    assert "reply with a number" in lower, f"Expected list picker footer, got: {output}"


def _assert_reminder_kept(phone):
    texts = [r[2] for r in get_user_reminders(phone)]
    assert any("succeeded" in (t or "").lower() for t in texts), texts


def _assert_tent_kept(camping_id):
    texts = [item[1].lower() for item in get_list_items(camping_id)]
    assert "tent" in texts, texts


def _arm_reminder_delete(phone):
    reminder = _seed_succeeded_reminder(phone)
    create_or_update_user(phone, last_active_list=None, pending_reminder_delete=json.dumps({
        "awaiting_confirmation": True,
        "type": "reminder",
        "id": reminder[0],
        "text": "You've succeeded.",
    }))
    return reminder


def _arm_list_item_delete(phone, camping_id):
    create_or_update_user(phone, last_active_list=None, pending_reminder_delete=json.dumps({
        "awaiting_confirmation": True,
        "type": "list_item",
        "list_name": "camping list",
        "list_id": camping_id,
        "is_shared": False,
        "text": "tent",
    }))


def _arm_memory_delete(phone):
    save_memory(phone, "wifi password is Home2024", {})
    memories = get_memories(phone)
    assert memories, "Expected a stored memory"
    create_or_update_user(phone, last_active_list=None, pending_memory_delete=json.dumps({
        "awaiting_confirmation": True,
        "id": memories[0][0],
        "text": memories[0][1],
    }))
    return memories[0]


class TestParseListPickerReply:
    """Unit tests for the SHOW LISTS picker regex."""

    def test_bare_number(self):
        assert parse_list_picker_reply("3") == 3
        assert parse_list_picker_reply(" 12 ") == 12

    def test_show_n_variants(self):
        assert parse_list_picker_reply("Show 3") == 3
        assert parse_list_picker_reply("show 3") == 3
        assert parse_list_picker_reply("#3") == 3
        assert parse_list_picker_reply("show #3") == 3
        assert parse_list_picker_reply("show list 3") == 3

    def test_prefixed_only_skips_bare_digit(self):
        assert parse_list_picker_reply("3", allow_bare_number=False) is None
        assert parse_list_picker_reply("Show 3", allow_bare_number=False) == 3
        assert parse_list_picker_reply("#3", allow_bare_number=False) == 3

    def test_non_picker_messages(self):
        assert parse_list_picker_reply("Show lists") is None
        assert parse_list_picker_reply("SHOW LISTS") is None
        assert parse_list_picker_reply("delete 3") is None
        assert parse_list_picker_reply("show grocery list") is None
        assert parse_list_picker_reply("remind me at 3") is None


class TestShowListsThenShowN:
    """Reproduce the live SMS sequence: Show lists → Show 3."""

    @pytest.mark.asyncio
    async def test_show_lists_then_show_3_opens_camping_list(
        self, simulator, onboarded_user, ai_mock
    ):
        phone = onboarded_user["phone"]
        _seed_three_lists_camping_is_third(phone)
        _seed_succeeded_reminder(phone)

        # If "Show 3" falls through to AI, this is the production failure mode.
        ai_mock.set_response("show 3", {
            "action": "delete_reminder",
            "search_term": "You've succeeded.",
        })

        listed = await simulator.send_message(phone, "Show lists")
        assert "camping list" in listed["output"].lower()
        assert "reply with a number" in listed["output"].lower()

        result = await simulator.send_message(phone, "Show 3")
        _assert_camping_list_not_delete(result["output"])

    @pytest.mark.asyncio
    async def test_show_lists_then_lowercase_show_3(
        self, simulator, onboarded_user, ai_mock
    ):
        phone = onboarded_user["phone"]
        _seed_three_lists_camping_is_third(phone)
        _seed_succeeded_reminder(phone)
        ai_mock.set_response("show 3", {
            "action": "delete_reminder",
            "search_term": "You've succeeded.",
        })

        await simulator.send_message(phone, "Show lists")
        result = await simulator.send_message(phone, "show 3")
        _assert_camping_list_not_delete(result["output"])

    @pytest.mark.asyncio
    async def test_show_lists_then_hash_3(self, simulator, onboarded_user, ai_mock):
        phone = onboarded_user["phone"]
        _seed_three_lists_camping_is_third(phone)
        _seed_succeeded_reminder(phone)
        ai_mock.set_response("#3", {
            "action": "delete_reminder",
            "search_term": "You've succeeded.",
        })

        await simulator.send_message(phone, "Show lists")
        result = await simulator.send_message(phone, "#3")
        _assert_camping_list_not_delete(result["output"])

    @pytest.mark.asyncio
    async def test_show_lists_then_bare_3(self, simulator, onboarded_user, ai_mock):
        phone = onboarded_user["phone"]
        _seed_three_lists_camping_is_third(phone)
        _seed_succeeded_reminder(phone)
        ai_mock.set_response("3", {
            "action": "delete_reminder",
            "search_term": "You've succeeded.",
        })

        await simulator.send_message(phone, "Show lists")
        result = await simulator.send_message(phone, "3")
        _assert_camping_list_not_delete(result["output"])


class TestStalePendingDeleteDoesNotHijackPicker:
    """Leftover pending_reminder_delete must not steal Show N / list-picker replies."""

    @pytest.mark.asyncio
    async def test_stale_awaiting_confirmation_then_show_3(
        self, simulator, onboarded_user, ai_mock
    ):
        phone = onboarded_user["phone"]
        _seed_three_lists_camping_is_third(phone)
        reminder = _seed_succeeded_reminder(phone)
        create_or_update_user(phone, pending_reminder_delete=json.dumps({
            "awaiting_confirmation": True,
            "type": "reminder",
            "id": reminder[0],
            "text": "You've succeeded.",
        }))
        ai_mock.set_response("show 3", {
            "action": "delete_reminder",
            "search_term": "You've succeeded.",
        })

        await simulator.send_message(phone, "Show lists")
        assert get_pending_reminder_delete(phone) is None

        result = await simulator.send_message(phone, "Show 3")
        _assert_camping_list_not_delete(result["output"])

    @pytest.mark.asyncio
    async def test_stale_delete_options_then_bare_3(
        self, simulator, onboarded_user, ai_mock
    ):
        phone = onboarded_user["phone"]
        _seed_three_lists_camping_is_third(phone)
        reminder = _seed_succeeded_reminder(phone)
        create_or_update_user(phone, pending_reminder_delete=json.dumps([{
            "type": "reminder",
            "id": reminder[0],
            "text": "You've succeeded.",
        }]))
        ai_mock.set_response("3", {
            "action": "delete_reminder",
            "search_term": "You've succeeded.",
        })

        await simulator.send_message(phone, "Show lists")
        assert get_pending_reminder_delete(phone) is None

        result = await simulator.send_message(phone, "3")
        _assert_camping_list_not_delete(result["output"])

    @pytest.mark.asyncio
    async def test_show_3_wins_over_pending_still_set(
        self, simulator, onboarded_user, ai_mock
    ):
        """Even if pending_reminder_delete is still set, Show 3 opens the list."""
        phone = onboarded_user["phone"]
        _seed_three_lists_camping_is_third(phone)
        reminder = _seed_succeeded_reminder(phone)
        ai_mock.set_response("show 3", {
            "action": "delete_reminder",
            "search_term": "You've succeeded.",
        })

        await simulator.send_message(phone, "Show lists")
        create_or_update_user(phone, pending_reminder_delete=json.dumps({
            "awaiting_confirmation": True,
            "type": "reminder",
            "id": reminder[0],
            "text": "You've succeeded.",
        }))

        result = await simulator.send_message(phone, "Show 3")
        _assert_camping_list_not_delete(result["output"])
        assert get_pending_reminder_delete(phone) is None


class TestPendingDeleteClearsOnNewIntent:
    """Once a delete is pending, a new intent must not stay in the confirm loop."""

    @pytest.mark.asyncio
    async def test_pending_reminder_delete_then_show_3(
        self, simulator, onboarded_user, ai_mock
    ):
        phone = onboarded_user["phone"]
        _seed_three_lists_camping_is_third(phone)
        _arm_reminder_delete(phone)
        ai_mock.set_response("show 3", {
            "action": "delete_reminder",
            "search_term": "You've succeeded.",
        })

        result = await simulator.send_message(phone, "Show 3")
        _assert_camping_list_not_delete(result["output"])
        assert get_pending_reminder_delete(phone) is None
        _assert_reminder_kept(phone)

    @pytest.mark.asyncio
    async def test_pending_reminder_delete_then_show_lists(
        self, simulator, onboarded_user, ai_mock
    ):
        phone = onboarded_user["phone"]
        _seed_three_lists_camping_is_third(phone)
        _arm_reminder_delete(phone)

        result = await simulator.send_message(phone, "Show lists")
        _assert_lists_menu_not_delete(result["output"])
        assert get_pending_reminder_delete(phone) is None
        _assert_reminder_kept(phone)

    @pytest.mark.asyncio
    async def test_pending_list_item_delete_then_show_3(
        self, simulator, onboarded_user, ai_mock
    ):
        phone = onboarded_user["phone"]
        camping_id = _seed_three_lists_camping_is_third(phone)
        _arm_list_item_delete(phone, camping_id)
        ai_mock.set_response("show 3", {
            "action": "delete_item",
            "list_name": "camping list",
            "item_text": "tent",
        })

        result = await simulator.send_message(phone, "Show 3")
        _assert_camping_list_not_delete(result["output"])
        assert get_pending_reminder_delete(phone) is None
        _assert_tent_kept(camping_id)

    @pytest.mark.asyncio
    async def test_pending_list_item_delete_then_show_lists(
        self, simulator, onboarded_user, ai_mock
    ):
        phone = onboarded_user["phone"]
        camping_id = _seed_three_lists_camping_is_third(phone)
        _arm_list_item_delete(phone, camping_id)

        result = await simulator.send_message(phone, "Show lists")
        _assert_lists_menu_not_delete(result["output"])
        assert get_pending_reminder_delete(phone) is None
        _assert_tent_kept(camping_id)

    @pytest.mark.asyncio
    async def test_pending_memory_delete_then_show_3(
        self, simulator, onboarded_user, ai_mock
    ):
        phone = onboarded_user["phone"]
        _seed_three_lists_camping_is_third(phone)
        _arm_memory_delete(phone)
        ai_mock.set_response("show 3", {
            "action": "delete_memory",
            "search_term": "wifi",
        })

        result = await simulator.send_message(phone, "Show 3")
        _assert_camping_list_not_delete(result["output"])
        assert get_pending_memory_delete(phone) is None
        assert any("wifi" in (m[1] or "").lower() for m in get_memories(phone))

    @pytest.mark.asyncio
    async def test_pending_memory_delete_then_show_lists(
        self, simulator, onboarded_user, ai_mock
    ):
        phone = onboarded_user["phone"]
        _seed_three_lists_camping_is_third(phone)
        _arm_memory_delete(phone)

        result = await simulator.send_message(phone, "Show lists")
        _assert_lists_menu_not_delete(result["output"])
        assert get_pending_memory_delete(phone) is None
        assert any("wifi" in (m[1] or "").lower() for m in get_memories(phone))

