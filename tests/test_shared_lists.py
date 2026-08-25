"""
Tests for Shared Lists feature.
Covers: sharing flow, permissions, limits, accept/decline, and list operations on shared lists.
"""

import pytest
import json
from unittest.mock import patch

PHONE_OWNER = "+15559876543"   # Premium owner (same as test_phone)
PHONE_SHARED = "+15559876544"  # Shared user (free tier)
PHONE_NEW = "+15559876545"     # Non-Remyndrs user (for invitation flow tests)


@pytest.fixture(autouse=True)
def clean_shared_list_data():
    """Clean up shared list test data before and after each test."""
    from database import get_db_connection, return_db_connection

    def cleanup():
        conn = None
        try:
            conn = get_db_connection()
            c = conn.cursor()
            for phone in [PHONE_OWNER, PHONE_SHARED, PHONE_NEW]:
                c.execute("DELETE FROM list_shares WHERE owner_phone = %s OR shared_with_phone = %s", (phone, phone))
                c.execute("DELETE FROM list_items WHERE phone_number = %s", (phone,))
                c.execute("DELETE FROM lists WHERE phone_number = %s", (phone,))
                c.execute("DELETE FROM conversation_analysis WHERE log_id IN (SELECT id FROM logs WHERE phone_number = %s)", (phone,))
                c.execute("DELETE FROM logs WHERE phone_number = %s", (phone,))
                c.execute("DELETE FROM reminders WHERE phone_number = %s", (phone,))
                c.execute("DELETE FROM recurring_reminders WHERE phone_number = %s", (phone,))
                c.execute("DELETE FROM memories WHERE phone_number = %s", (phone,))
                c.execute("DELETE FROM smart_nudges WHERE phone_number = %s", (phone,))
                c.execute("DELETE FROM support_tickets WHERE phone_number = %s", (phone,))
                c.execute("DELETE FROM users WHERE phone_number = %s", (phone,))
                c.execute("DELETE FROM onboarding_progress WHERE phone_number = %s", (phone,))
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


@pytest.fixture
def premium_owner():
    """Create a premium onboarded user (the list owner)."""
    from models.user import create_or_update_user
    create_or_update_user(
        PHONE_OWNER,
        first_name="Brad",
        last_name="Owner",
        zip_code="10001",
        timezone="America/New_York",
        onboarding_complete=True,
        premium_status="premium",
    )
    return {"phone": PHONE_OWNER, "first_name": "Brad"}


@pytest.fixture
def free_user():
    """Create a free tier onboarded user (shared list recipient)."""
    from models.user import create_or_update_user
    create_or_update_user(
        PHONE_SHARED,
        first_name="Jane",
        last_name="Free",
        zip_code="90210",
        timezone="America/Los_Angeles",
        onboarding_complete=True,
        premium_status="free",
    )
    return {"phone": PHONE_SHARED, "first_name": "Jane"}


@pytest.fixture
def owner_list(premium_owner):
    """Create a list owned by the premium user."""
    from models.list_model import create_list, add_list_item
    list_id = create_list(PHONE_OWNER, "Grocery List")
    add_list_item(list_id, PHONE_OWNER, "Milk")
    add_list_item(list_id, PHONE_OWNER, "Eggs")
    return {"list_id": list_id, "name": "Grocery List"}


# =====================================================
# MODEL LAYER TESTS
# =====================================================


class TestShareListModel:
    """Tests for list_model sharing functions."""

    def test_share_list_success(self, premium_owner, free_user, owner_list):
        from models.list_model import share_list
        success, msg = share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        assert success is True
        assert "sent" in msg.lower() or "re-sent" in msg.lower()

    def test_share_list_duplicate(self, premium_owner, free_user, owner_list):
        from models.list_model import share_list
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        success, msg = share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        assert success is False
        assert "pending" in msg.lower() or "already" in msg.lower()

    def test_share_list_not_owner(self, premium_owner, free_user, owner_list):
        from models.list_model import share_list
        success, msg = share_list(PHONE_SHARED, owner_list["list_id"], PHONE_OWNER)
        assert success is False
        assert "not found" in msg.lower()

    def test_accept_share(self, premium_owner, free_user, owner_list):
        from models.list_model import share_list, accept_share, get_shared_lists_for_user
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        success, msg = accept_share(PHONE_SHARED, owner_list["list_id"])
        assert success is True

        shared = get_shared_lists_for_user(PHONE_SHARED)
        assert len(shared) == 1
        assert shared[0][1] == "Grocery List"

    def test_decline_share(self, premium_owner, free_user, owner_list):
        from models.list_model import share_list, decline_share, get_pending_shares
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)

        pending = get_pending_shares(PHONE_SHARED)
        assert len(pending) == 1

        success, msg = decline_share(PHONE_SHARED, owner_list["list_id"])
        assert success is True

        pending = get_pending_shares(PHONE_SHARED)
        assert len(pending) == 0

    def test_leave_shared_list(self, premium_owner, free_user, owner_list):
        from models.list_model import share_list, accept_share, leave_shared_list, get_shared_lists_for_user
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        accept_share(PHONE_SHARED, owner_list["list_id"])

        success, msg = leave_shared_list(PHONE_SHARED, owner_list["list_id"])
        assert success is True

        shared = get_shared_lists_for_user(PHONE_SHARED)
        assert len(shared) == 0

    def test_unshare_specific_user(self, premium_owner, free_user, owner_list):
        from models.list_model import share_list, accept_share, unshare_list, get_list_members
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        accept_share(PHONE_SHARED, owner_list["list_id"])

        success, msg = unshare_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        assert success is True

        members = get_list_members(owner_list["list_id"])
        assert len(members) == 0

    def test_unshare_all(self, premium_owner, free_user, owner_list):
        from models.list_model import share_list, unshare_list_all
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)

        success, msg = unshare_list_all(PHONE_OWNER, owner_list["list_id"])
        assert success is True
        assert "1 user" in msg

    def test_get_list_members(self, premium_owner, free_user, owner_list):
        from models.list_model import share_list, get_list_members
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)

        members = get_list_members(owner_list["list_id"])
        assert len(members) == 1
        assert members[0][0] == PHONE_SHARED
        assert members[0][1] == "pending"


class TestAccessibleList:
    """Tests for get_accessible_list_by_name and can_user_access_list."""

    def test_own_list_accessible(self, premium_owner, owner_list):
        from models.list_model import get_accessible_list_by_name
        result = get_accessible_list_by_name(PHONE_OWNER, "Grocery List")
        assert result is not None
        list_id, name, is_shared, owner_phone = result
        assert name == "Grocery List"
        assert is_shared is False
        assert owner_phone is None

    def test_shared_list_accessible(self, premium_owner, free_user, owner_list):
        from models.list_model import share_list, accept_share, get_accessible_list_by_name
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        accept_share(PHONE_SHARED, owner_list["list_id"])

        result = get_accessible_list_by_name(PHONE_SHARED, "Grocery List")
        assert result is not None
        list_id, name, is_shared, owner_phone = result
        assert name == "Grocery List"
        assert is_shared is True
        assert owner_phone == PHONE_OWNER

    def test_pending_share_not_accessible(self, premium_owner, free_user, owner_list):
        from models.list_model import share_list, get_accessible_list_by_name
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        # Don't accept

        result = get_accessible_list_by_name(PHONE_SHARED, "Grocery List")
        assert result is None

    def test_can_user_access_list_owner(self, premium_owner, owner_list):
        from models.list_model import can_user_access_list
        has_access, is_owner = can_user_access_list(PHONE_OWNER, owner_list["list_id"])
        assert has_access is True
        assert is_owner is True

    def test_can_user_access_list_shared(self, premium_owner, free_user, owner_list):
        from models.list_model import share_list, accept_share, can_user_access_list
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        accept_share(PHONE_SHARED, owner_list["list_id"])

        has_access, is_owner = can_user_access_list(PHONE_SHARED, owner_list["list_id"])
        assert has_access is True
        assert is_owner is False

    def test_can_user_access_list_no_access(self, premium_owner, free_user, owner_list):
        from models.list_model import can_user_access_list
        has_access, is_owner = can_user_access_list(PHONE_SHARED, owner_list["list_id"])
        assert has_access is False
        assert is_owner is False


class TestSharedListItemOperations:
    """Tests for item operations on shared lists."""

    def test_add_item_to_shared_list(self, premium_owner, free_user, owner_list):
        from models.list_model import share_list, accept_share, add_list_item, get_list_items
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        accept_share(PHONE_SHARED, owner_list["list_id"])

        # Shared user adds item
        add_list_item(owner_list["list_id"], PHONE_SHARED, "Bread")
        items = get_list_items(owner_list["list_id"])
        item_texts = [item[1] for item in items]
        assert "Bread" in item_texts

    def test_complete_item_on_shared_list(self, premium_owner, free_user, owner_list):
        from models.list_model import share_list, accept_share, mark_item_complete_by_list_id, get_list_items
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        accept_share(PHONE_SHARED, owner_list["list_id"])

        success = mark_item_complete_by_list_id(owner_list["list_id"], "Milk")
        assert success is True

        items = get_list_items(owner_list["list_id"])
        for item_id, text, completed in items:
            if text == "Milk":
                assert completed is True

    def test_delete_item_from_shared_list(self, premium_owner, free_user, owner_list):
        from models.list_model import share_list, accept_share, delete_list_item_by_list_id, get_list_items
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        accept_share(PHONE_SHARED, owner_list["list_id"])

        success = delete_list_item_by_list_id(owner_list["list_id"], "Eggs")
        assert success is True

        items = get_list_items(owner_list["list_id"])
        item_texts = [item[1] for item in items]
        assert "Eggs" not in item_texts


# =====================================================
# TIER SERVICE TESTS
# =====================================================


class TestShareListTierGating:
    """Tests for Premium-only gating on shared lists."""

    def test_premium_can_share(self, premium_owner):
        from services.tier_service import can_share_list
        allowed, msg = can_share_list(PHONE_OWNER)
        assert allowed is True
        assert msg is None

    def test_free_cannot_share(self, free_user):
        from services.tier_service import can_share_list
        allowed, msg = can_share_list(PHONE_SHARED)
        assert allowed is False
        assert "premium" in msg.lower() or "upgrade" in msg.lower()


class TestSharedListsBetaOptIn:
    """Tests for the shared lists beta whitelist + opt-in flag."""

    def test_premium_blocked_when_whitelist_set_and_not_opted_in(self, premium_owner):
        from services.tier_service import can_share_list
        with patch("config.SHARED_LISTS_BETA_PHONES", ["+15550001111"]):
            allowed, msg = can_share_list(PHONE_OWNER)
        assert allowed is False
        assert "beta" in msg.lower()

    def test_premium_allowed_via_whitelist(self, premium_owner):
        from services.tier_service import can_share_list
        with patch("config.SHARED_LISTS_BETA_PHONES", [PHONE_OWNER]):
            allowed, msg = can_share_list(PHONE_OWNER)
        assert allowed is True

    def test_premium_allowed_via_opt_in_flag(self, premium_owner):
        from services.tier_service import can_share_list
        from models.user import create_or_update_user
        create_or_update_user(PHONE_OWNER, shared_lists_beta_opt_in=True)
        with patch("config.SHARED_LISTS_BETA_PHONES", ["+15550001111"]):
            allowed, msg = can_share_list(PHONE_OWNER)
        assert allowed is True

    def test_opt_in_does_not_bypass_premium_check(self, free_user):
        """Free-tier user with opt-in flag still can't share (premium check runs after)."""
        from services.tier_service import can_share_list
        from models.user import create_or_update_user
        create_or_update_user(PHONE_SHARED, shared_lists_beta_opt_in=True)
        with patch("config.SHARED_LISTS_BETA_PHONES", ["+15550001111"]):
            allowed, msg = can_share_list(PHONE_SHARED)
        assert allowed is False
        assert "premium" in msg.lower() or "upgrade" in msg.lower()

    def test_get_shared_lists_beta_opt_in_defaults_false(self, premium_owner):
        from models.user import get_shared_lists_beta_opt_in
        assert get_shared_lists_beta_opt_in(PHONE_OWNER) is False

    def test_get_shared_lists_beta_opt_in_reflects_flag(self, premium_owner):
        from models.user import create_or_update_user, get_shared_lists_beta_opt_in
        create_or_update_user(PHONE_OWNER, shared_lists_beta_opt_in=True)
        assert get_shared_lists_beta_opt_in(PHONE_OWNER) is True


class TestSharedListsBetaKeywordHandlers:
    """Tests for JOIN / LEAVE SHARED LISTS keyword handlers."""

    @pytest.mark.asyncio
    async def test_premium_can_join(self, simulator, sms_capture, ai_mock, onboarded_user):
        from models.user import create_or_update_user, get_shared_lists_beta_opt_in
        phone = onboarded_user['phone']
        create_or_update_user(phone, premium_status="premium")

        result = await simulator.send_message(phone, "JOIN SHARED LISTS")
        assert "beta" in result['output'].lower() or "active" in result['output'].lower()
        assert get_shared_lists_beta_opt_in(phone) is True

    @pytest.mark.asyncio
    async def test_free_tier_blocked_from_joining(self, simulator, sms_capture, ai_mock, onboarded_user):
        from models.user import get_shared_lists_beta_opt_in
        phone = onboarded_user['phone']
        # onboarded_user defaults to free tier

        result = await simulator.send_message(phone, "JOIN SHARED LISTS")
        assert "premium" in result['output'].lower() or "upgrade" in result['output'].lower()
        assert get_shared_lists_beta_opt_in(phone) is False

    @pytest.mark.asyncio
    async def test_join_twice_is_idempotent(self, simulator, sms_capture, ai_mock, onboarded_user):
        from models.user import create_or_update_user, get_shared_lists_beta_opt_in
        phone = onboarded_user['phone']
        create_or_update_user(phone, premium_status="premium")

        await simulator.send_message(phone, "JOIN SHARED LISTS")
        result = await simulator.send_message(phone, "JOIN SHARED LISTS")
        assert "already" in result['output'].lower()
        assert get_shared_lists_beta_opt_in(phone) is True

    @pytest.mark.asyncio
    async def test_leave_clears_opt_in(self, simulator, sms_capture, ai_mock, onboarded_user):
        from models.user import create_or_update_user, get_shared_lists_beta_opt_in
        phone = onboarded_user['phone']
        create_or_update_user(phone, premium_status="premium")

        await simulator.send_message(phone, "JOIN SHARED LISTS")
        result = await simulator.send_message(phone, "LEAVE SHARED LISTS")
        assert "left" in result['output'].lower() or "leav" in result['output'].lower()
        assert get_shared_lists_beta_opt_in(phone) is False

    @pytest.mark.asyncio
    async def test_leave_when_not_opted_in_is_noop(self, simulator, sms_capture, ai_mock, onboarded_user):
        phone = onboarded_user['phone']
        result = await simulator.send_message(phone, "LEAVE SHARED LISTS")
        assert "not" in result['output'].lower()


# =====================================================
# LIMIT ENFORCEMENT TESTS
# =====================================================


class TestShareLimits:
    """Tests for shared list limits."""

    def test_max_members_per_list(self, premium_owner, owner_list):
        """Test that a list can't be shared with more than SHARED_LIST_MAX_MEMBERS users."""
        from models.list_model import share_list
        from models.user import create_or_update_user
        from config import SHARED_LIST_MAX_MEMBERS

        # Create and share with max users
        for i in range(SHARED_LIST_MAX_MEMBERS):
            phone = f"+1555000000{i}"
            create_or_update_user(phone, first_name=f"User{i}", onboarding_complete=True)
            success, msg = share_list(PHONE_OWNER, owner_list["list_id"], phone)
            assert success is True, f"Failed to share with user {i}: {msg}"

        # Try to share with one more
        extra_phone = "+15550000099"
        create_or_update_user(extra_phone, first_name="Extra", onboarding_complete=True)
        success, msg = share_list(PHONE_OWNER, owner_list["list_id"], extra_phone)
        assert success is False
        assert "max" in msg.lower() or str(SHARED_LIST_MAX_MEMBERS) in msg

        # Cleanup extra users
        from database import get_db_connection, return_db_connection
        conn = get_db_connection()
        c = conn.cursor()
        for i in range(SHARED_LIST_MAX_MEMBERS):
            c.execute("DELETE FROM users WHERE phone_number = %s", (f"+1555000000{i}",))
        c.execute("DELETE FROM users WHERE phone_number = %s", (extra_phone,))
        conn.commit()
        return_db_connection(conn)

    def test_max_shared_lists_per_owner(self, premium_owner, free_user):
        """Test that an owner can't share more than SHARED_LIST_MAX_PER_USER lists."""
        from models.list_model import create_list, share_list
        from config import SHARED_LIST_MAX_PER_USER

        for i in range(SHARED_LIST_MAX_PER_USER):
            list_id = create_list(PHONE_OWNER, f"List {i}")
            success, msg = share_list(PHONE_OWNER, list_id, PHONE_SHARED)
            assert success is True, f"Failed to share list {i}: {msg}"

        # Try to share one more
        extra_list_id = create_list(PHONE_OWNER, "Extra List")
        success, msg = share_list(PHONE_OWNER, extra_list_id, PHONE_SHARED)
        assert success is False
        assert "limit" in msg.lower()

    def test_reshare_after_decline(self, premium_owner, free_user, owner_list):
        """Test that a declined share can be re-sent."""
        from models.list_model import share_list, decline_share
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        decline_share(PHONE_SHARED, owner_list["list_id"])

        # Re-share should work
        success, msg = share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        assert success is True
        assert "sent" in msg.lower()


# =====================================================
# HANDLER TESTS
# =====================================================


class TestSharedListHandlers:
    """Tests for shared list handler functions."""

    def test_format_all_lists_display_with_shared(self, premium_owner, free_user, owner_list):
        from models.list_model import share_list, accept_share, create_list
        from routes.handlers.lists import format_all_lists_display

        # Free user has own list + shared list
        create_list(PHONE_SHARED, "My Personal List")
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        accept_share(PHONE_SHARED, owner_list["list_id"])

        display = format_all_lists_display(PHONE_SHARED)
        assert "My Personal List" in display
        assert "[Shared] Grocery List" in display

    def test_format_all_lists_display_no_lists(self, free_user):
        from routes.handlers.lists import format_all_lists_display
        display = format_all_lists_display(PHONE_SHARED)
        assert "don't have any lists" in display.lower()

    def test_normalize_phone(self):
        from routes.handlers.lists import _normalize_phone
        assert _normalize_phone("555-123-4567") == "+15551234567"
        assert _normalize_phone("15551234567") == "+115551234567" or _normalize_phone("15551234567") == "+15551234567"
        assert _normalize_phone("+15551234567") == "+15551234567"

    def test_format_phone(self):
        from routes.handlers.lists import _format_phone
        assert _format_phone("+15551234567") == "(555) 123-4567"


# =====================================================
# NAME PROMPT FLOW TESTS
# =====================================================


class TestShareNamePromptFlow:
    """Tests for the name prompt flow when sharing a list."""

    def test_set_and_get_share_name(self, premium_owner, free_user, owner_list):
        """Test that a name can be set and retrieved for a share."""
        from models.list_model import share_list, set_share_name, get_share_name
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)

        set_share_name(owner_list["list_id"], PHONE_SHARED, "Jane")
        name = get_share_name(owner_list["list_id"], PHONE_SHARED)
        assert name == "Jane"

    def test_get_list_members_includes_name(self, premium_owner, free_user, owner_list):
        """Test that get_list_members returns the shared_with_name."""
        from models.list_model import share_list, set_share_name, get_list_members
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        set_share_name(owner_list["list_id"], PHONE_SHARED, "Jane")

        members = get_list_members(owner_list["list_id"])
        assert len(members) == 1
        assert members[0][3] == "Jane"

    def test_pending_share_name_handler(self, premium_owner, free_user, owner_list):
        """Test that handle_pending_share_name saves name and sends invitation."""
        import json
        from models.list_model import share_list, get_share_name
        from models.user import create_or_update_user
        from routes.handlers.lists import handle_pending_share_name

        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)

        # Set up pending state (as handle_share_list would)
        pending_data = json.dumps({
            'list_id': owner_list["list_id"],
            'shared_with_phone': PHONE_SHARED,
            'list_name': "Grocery List",
        })
        create_or_update_user(PHONE_OWNER, pending_share_name=pending_data)

        # Simulate owner replying with a name
        result = handle_pending_share_name(PHONE_OWNER, "Jane")
        assert result is not None
        assert "Jane" in result
        assert "Grocery List" in result

        # Name should be saved on the share
        name = get_share_name(owner_list["list_id"], PHONE_SHARED)
        assert name == "Jane"

    def test_no_pending_share_name_returns_none(self, premium_owner):
        """Test that handle_pending_share_name returns None when no pending state."""
        from routes.handlers.lists import handle_pending_share_name
        result = handle_pending_share_name(PHONE_OWNER, "Jane")
        assert result is None

    def test_accept_notification_uses_share_name(self, premium_owner, free_user, owner_list, sms_capture):
        """Test that accept notification uses the owner-assigned name."""
        from models.list_model import share_list, accept_share, set_share_name
        from routes.handlers.lists import handle_accept_share

        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        set_share_name(owner_list["list_id"], PHONE_SHARED, "Jane")
        sms_capture.messages.clear()

        handle_accept_share(PHONE_SHARED, "ACCEPT")

        # Owner notification should use "Jane" not the phone number
        owner_msgs = [m for m in sms_capture.messages if m["to"] == PHONE_OWNER]
        assert len(owner_msgs) >= 1
        assert "Jane" in owner_msgs[0]["message"]
        assert "accepted" in owner_msgs[0]["message"].lower()


# =====================================================
# SHARE BY NAME TESTS
# =====================================================


class TestShareByName:
    """Tests for sharing a list using a previously-known recipient name."""

    def test_get_known_recipients_finds_match(self, premium_owner, free_user, owner_list):
        """Known recipient lookup finds a previous share by name."""
        from models.list_model import share_list, set_share_name, get_known_recipients
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        set_share_name(owner_list["list_id"], PHONE_SHARED, "Jane")

        matches = get_known_recipients(PHONE_OWNER, "Jane")
        assert len(matches) == 1
        assert matches[0][0] == PHONE_SHARED
        assert matches[0][1] == "Jane"

    def test_get_known_recipients_case_insensitive(self, premium_owner, free_user, owner_list):
        """Name lookup is case-insensitive."""
        from models.list_model import share_list, set_share_name, get_known_recipients
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        set_share_name(owner_list["list_id"], PHONE_SHARED, "Jane")

        matches = get_known_recipients(PHONE_OWNER, "jane")
        assert len(matches) == 1

    def test_get_known_recipients_no_match(self, premium_owner, owner_list):
        """Unknown name returns empty list."""
        from models.list_model import get_known_recipients
        matches = get_known_recipients(PHONE_OWNER, "Nobody")
        assert len(matches) == 0

    def test_share_by_known_name_skips_name_prompt(self, premium_owner, free_user, owner_list, sms_capture):
        """Sharing with a known name resolves to phone and sends invitation immediately."""
        from models.list_model import share_list, set_share_name, create_list, get_list_members
        from routes.handlers.lists import handle_share_list

        # First share establishes the name
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        set_share_name(owner_list["list_id"], PHONE_SHARED, "Jane")

        # Create a second list to share by name
        second_list_id = create_list(PHONE_OWNER, "Vacation List")
        sms_capture.messages.clear()

        ai_response = {
            "action": "share_list",
            "list_name": "Vacation List",
            "shared_with_name": "Jane",
        }
        result = handle_share_list(PHONE_OWNER, "Share vacation list with Jane", ai_response)

        # Should send invitation immediately (no name prompt)
        assert "Invitation sent" in result
        assert "Jane" in result

        # Invitation SMS should have been sent to the shared user
        invite_msgs = [m for m in sms_capture.messages if m["to"] == PHONE_SHARED]
        assert len(invite_msgs) >= 1
        assert "Jane" in invite_msgs[0]["message"]

    def test_share_by_unknown_name_asks_for_phone(self, premium_owner, owner_list):
        """Sharing with an unknown name asks for the phone number."""
        from routes.handlers.lists import handle_share_list

        ai_response = {
            "action": "share_list",
            "list_name": "Grocery List",
            "shared_with_name": "Mom",
        }
        result = handle_share_list(PHONE_OWNER, "Share grocery list with Mom", ai_response)

        assert "phone number" in result.lower()
        assert "Mom" in result

    def test_share_by_unknown_name_then_provide_phone(self, premium_owner, free_user, owner_list, sms_capture):
        """After asking for phone, owner provides it → share created + invitation sent."""
        from routes.handlers.lists import handle_share_list, handle_pending_share_name
        from models.list_model import get_share_name

        ai_response = {
            "action": "share_list",
            "list_name": "Grocery List",
            "shared_with_name": "Mom",
        }
        handle_share_list(PHONE_OWNER, "Share grocery list with Mom", ai_response)
        sms_capture.messages.clear()

        # Owner provides phone number
        result = handle_pending_share_name(PHONE_OWNER, PHONE_SHARED)

        assert "Invitation sent" in result
        assert "Mom" in result

        # Name should be saved
        name = get_share_name(owner_list["list_id"], PHONE_SHARED)
        assert name == "Mom"

        # Invitation SMS should have been sent
        invite_msgs = [m for m in sms_capture.messages if m["to"] == PHONE_SHARED]
        assert len(invite_msgs) >= 1
        assert "Mom" in invite_msgs[0]["message"]


# =====================================================
# CASCADE DELETE TESTS
# =====================================================


class TestCascadeDelete:
    """Tests for cascade behavior when lists are deleted."""

    def test_delete_shared_list_removes_shares(self, premium_owner, free_user, owner_list):
        """When owner deletes a list, all shares should be cascade-deleted."""
        from models.list_model import share_list, accept_share, delete_list, get_shared_lists_for_user
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        accept_share(PHONE_SHARED, owner_list["list_id"])

        # Verify share exists
        shared = get_shared_lists_for_user(PHONE_SHARED)
        assert len(shared) == 1

        # Owner deletes the list
        delete_list(PHONE_OWNER, "Grocery List")

        # Share should be gone (cascade delete)
        shared = get_shared_lists_for_user(PHONE_SHARED)
        assert len(shared) == 0


# =====================================================
# OWNER DOWNGRADE (READ-ONLY) TESTS
# =====================================================


class TestOwnerDowngradeReadOnly:
    """Tests for shared lists becoming read-only when owner loses Premium."""

    def test_is_shared_list_read_only_premium_owner(self, premium_owner, owner_list):
        """Shared list with Premium owner is NOT read-only."""
        from models.list_model import is_shared_list_read_only
        read_only, owner_name = is_shared_list_read_only(owner_list["list_id"])
        assert read_only is False

    def test_is_shared_list_read_only_free_owner(self, premium_owner, free_user, owner_list):
        """Shared list becomes read-only when owner downgrades to free."""
        from models.list_model import is_shared_list_read_only, share_list, accept_share
        from models.user import create_or_update_user

        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        accept_share(PHONE_SHARED, owner_list["list_id"])

        # Downgrade owner
        create_or_update_user(PHONE_OWNER, premium_status="free", trial_end_date=None)

        read_only, owner_name = is_shared_list_read_only(owner_list["list_id"])
        assert read_only is True
        assert owner_name == "Brad"

    def test_shared_user_can_still_view_after_downgrade(self, premium_owner, free_user, owner_list):
        """Shared user can still view a shared list after owner downgrades."""
        from models.list_model import share_list, accept_share, get_accessible_list_by_name, get_list_items
        from models.user import create_or_update_user

        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        accept_share(PHONE_SHARED, owner_list["list_id"])

        # Downgrade owner
        create_or_update_user(PHONE_OWNER, premium_status="free", trial_end_date=None)

        # Shared user can still find and view the list
        accessible = get_accessible_list_by_name(PHONE_SHARED, "Grocery List")
        assert accessible is not None
        items = get_list_items(accessible[0])
        assert len(items) == 2  # Milk and Eggs from owner_list fixture

    def test_owner_can_still_delete_after_downgrade(self, premium_owner, free_user, owner_list):
        """Owner can still delete their shared list even after downgrading."""
        from models.list_model import share_list, accept_share, delete_list, get_shared_lists_for_user
        from models.user import create_or_update_user

        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
        accept_share(PHONE_SHARED, owner_list["list_id"])

        # Downgrade owner
        create_or_update_user(PHONE_OWNER, premium_status="free", trial_end_date=None)

        # Owner can still delete
        delete_list(PHONE_OWNER, "Grocery List")
        shared = get_shared_lists_for_user(PHONE_SHARED)
        assert len(shared) == 0


# =====================================================
# NON-USER INVITATION FLOW TESTS
# =====================================================


class TestNonUserInvitationFlow:
    """Tests for sharing a list with someone who isn't on Remyndrs yet."""

    @pytest.mark.asyncio
    async def test_non_user_onboarding_shows_shared_list_welcome(self, premium_owner, owner_list, simulator, sms_capture):
        """Non-user replying YES gets a customized welcome mentioning the shared list."""
        from models.list_model import share_list
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_NEW)

        # Non-user texts YES — enters onboarding step 0
        result = await simulator.send_message(PHONE_NEW, "YES")
        assert "Brad" in result["output"] or "shared" in result["output"].lower()
        assert "Grocery List" in result["output"]
        assert "name" in result["output"].lower()

    @pytest.mark.asyncio
    async def test_non_user_full_onboarding_auto_accepts_share(self, premium_owner, owner_list, simulator, sms_capture):
        """Non-user completes onboarding → pending share is auto-accepted → owner notified."""
        from models.list_model import share_list, get_shared_lists_for_user

        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_NEW)
        sms_capture.messages.clear()

        # Step 0: initial message
        await simulator.send_message(PHONE_NEW, "YES")

        # Step 1: provide name
        await simulator.send_message(PHONE_NEW, "Sarah")

        # Step 2: provide ZIP → completes onboarding
        result = await simulator.send_message(PHONE_NEW, "90210")

        # Completion message should mention the shared list
        assert "Grocery List" in result["output"]
        assert "all set" in result["output"].lower() or "set" in result["output"].lower()

        # Share should now be accepted
        shared = get_shared_lists_for_user(PHONE_NEW)
        assert len(shared) == 1
        assert shared[0][1] == "Grocery List"

        # Owner should have been notified
        owner_msgs = [m for m in sms_capture.messages if m["to"] == PHONE_OWNER]
        assert any("joined" in m["message"].lower() or "accepted" in m["message"].lower() for m in owner_msgs)

    @pytest.mark.asyncio
    async def test_non_user_referral_source_tagged(self, premium_owner, owner_list, simulator, sms_capture):
        """Non-user invited via shared list gets tagged with 'shared-list' referral source."""
        from models.list_model import share_list
        from database import get_db_connection, return_db_connection

        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_NEW)

        # Complete onboarding (referral set after user record exists)
        await simulator.send_message(PHONE_NEW, "YES")
        await simulator.send_message(PHONE_NEW, "Sarah")

        # Check referral source (set during step 1 when user record exists)
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT referral_source FROM users WHERE phone_number = %s", (PHONE_NEW,))
        row = c.fetchone()
        return_db_connection(conn)

        assert row is not None
        assert row[0] == "shared-list"

    @pytest.mark.asyncio
    async def test_non_user_without_pending_share_gets_normal_welcome(self, simulator, sms_capture):
        """A non-user without pending shares gets the normal welcome message."""
        result = await simulator.send_message(PHONE_NEW, "Hi")
        assert "welcome" in result["output"].lower() or "remyndrs" in result["output"].lower()
        # Should NOT mention any shared list
        assert "shared" not in result["output"].lower()

    @pytest.mark.asyncio
    async def test_non_user_declines_invitation_with_no(self, premium_owner, owner_list, simulator, sms_capture):
        """Non-user replying NO declines the share and does NOT start onboarding."""
        from models.list_model import share_list, get_pending_shares
        share_list(PHONE_OWNER, owner_list["list_id"], PHONE_NEW)
        sms_capture.messages.clear()

        result = await simulator.send_message(PHONE_NEW, "No")

        # Should get a polite decline message, NOT onboarding
        assert "declined" in result["output"].lower() or "no problem" in result["output"].lower()
        assert "name" not in result["output"].lower()

        # Pending share should be gone
        pending = get_pending_shares(PHONE_NEW)
        assert len(pending) == 0

        # Owner should be notified
        owner_msgs = [m for m in sms_capture.messages if m["to"] == PHONE_OWNER]
        assert any("declined" in m["message"].lower() for m in owner_msgs)

    @pytest.mark.asyncio
    async def test_non_user_no_without_pending_share_goes_to_onboarding(self, simulator, sms_capture):
        """Non-user saying NO without a pending share just enters normal onboarding."""
        result = await simulator.send_message(PHONE_NEW, "No")
        # Should enter onboarding, not get a decline message
        assert "welcome" in result["output"].lower() or "name" in result["output"].lower()


# =====================================================
# REGRESSION: Non-owner edit paths on shared lists
# See GitHub issue / Heather's bug report 2026-04-18:
# shared list recipient tried to add item, got disambiguation menu
# that didn't include the shared list.
# =====================================================


@pytest.fixture
def accepted_share(premium_owner, free_user, owner_list):
    """Share owner_list with free_user and auto-accept."""
    from models.list_model import share_list, accept_share
    share_list(PHONE_OWNER, owner_list["list_id"], PHONE_SHARED)
    accept_share(PHONE_SHARED, owner_list["list_id"])
    return owner_list


class TestSharedListEditPaths:
    """Non-owner operations on an accepted shared list must work end-to-end."""

    def test_add_to_list_adds_item_to_shared_list(self, accepted_share):
        from routes.handlers.lists import handle_add_to_list
        from models.list_model import get_list_items

        with patch('routes.handlers.lists.log_interaction'):
            reply = handle_add_to_list(
                phone_number=PHONE_SHARED,
                incoming_msg="Add cheese to the Grocery List",
                ai_response={"list_name": "Grocery List", "item_text": "cheese"},
            )

        assert "cheese" in reply.lower()
        assert "shared" in reply.lower()  # UI tells recipient it's a shared list
        items = [i[1] for i in get_list_items(accepted_share["list_id"])]
        assert "cheese" in items

    def test_add_to_list_does_not_create_duplicate_for_non_owner(self, accepted_share):
        """Non-owner adding to a shared list must not silently auto-create a personal duplicate."""
        from routes.handlers.lists import handle_add_to_list
        from models.list_model import get_lists

        before = {l[1] for l in get_lists(PHONE_SHARED)}
        with patch('routes.handlers.lists.log_interaction'):
            handle_add_to_list(
                phone_number=PHONE_SHARED,
                incoming_msg="Add cheese to Grocery List",
                ai_response={"list_name": "Grocery List", "item_text": "cheese"},
            )
        after = {l[1] for l in get_lists(PHONE_SHARED)}
        assert after == before, "Shared-list add created a phantom personal list for the non-owner"

    def test_show_list_displays_shared(self, accepted_share):
        from routes.handlers.lists import handle_show_list

        with patch('routes.handlers.lists.log_interaction'):
            reply = handle_show_list(
                phone_number=PHONE_SHARED,
                incoming_msg="Show Grocery List",
                ai_response={"list_name": "Grocery List"},
            )
        assert "[Shared]" in reply
        assert "Milk" in reply
        assert "Eggs" in reply

    def test_add_item_ask_list_includes_shared_in_menu(self, accepted_share):
        """The Heather-bug regression: disambiguation menu must include accessible shared lists."""
        from routes.handlers.lists import handle_add_item_ask_list, create_list

        # Give the recipient a couple of personal lists too so we get the menu branch
        create_list(PHONE_SHARED, "honey do list")
        create_list(PHONE_SHARED, "birthday list")

        with patch('routes.handlers.lists.log_interaction'):
            reply = handle_add_item_ask_list(
                phone_number=PHONE_SHARED,
                incoming_msg="Add cheese",
                ai_response={"item_text": "cheese"},
            )
        assert "Which list" in reply
        assert "[Shared] Grocery List" in reply, (
            f"Disambiguation menu missing shared list. Got: {reply}"
        )

    def test_complete_item_on_shared_list(self, accepted_share):
        from routes.handlers.lists import handle_complete_item
        from models.list_model import get_list_items

        with patch('routes.handlers.lists.log_interaction'):
            reply = handle_complete_item(
                phone_number=PHONE_SHARED,
                incoming_msg="Check off Milk on Grocery List",
                ai_response={"list_name": "Grocery List", "item_text": "Milk"},
            )
        assert "milk" in reply.lower()
        completed = [i for i in get_list_items(accepted_share["list_id"]) if i[2]]
        assert any("milk" in item[1].lower() for item in completed)

    def test_non_owner_cannot_delete_shared_list(self, accepted_share):
        from routes.handlers.lists import handle_delete_list
        from models.list_model import get_list_by_name

        with patch('routes.handlers.lists.log_interaction'):
            reply = handle_delete_list(
                phone_number=PHONE_SHARED,
                incoming_msg="Delete Grocery List",
                ai_response={"list_name": "Grocery List"},
            )
        assert "owner" in reply.lower()
        # List still exists on the owner's side
        assert get_list_by_name(PHONE_OWNER, "Grocery List") is not None

    def test_non_owner_cannot_rename_shared_list(self, accepted_share):
        from routes.handlers.lists import handle_rename_list
        from models.list_model import get_list_by_name

        with patch('routes.handlers.lists.log_interaction'):
            reply = handle_rename_list(
                phone_number=PHONE_SHARED,
                incoming_msg="Rename Grocery List to Food",
                ai_response={"old_name": "Grocery List", "new_name": "Food"},
            )
        assert "owner" in reply.lower()
        assert get_list_by_name(PHONE_OWNER, "Grocery List") is not None

    def test_non_owner_cannot_clear_shared_list(self, accepted_share):
        from routes.handlers.lists import handle_clear_list
        from models.list_model import get_list_items

        with patch('routes.handlers.lists.log_interaction'):
            reply = handle_clear_list(
                phone_number=PHONE_SHARED,
                incoming_msg="Clear Grocery List",
                ai_response={"list_name": "Grocery List"},
            )
        assert "owner" in reply.lower()
        # Items still there
        assert len(get_list_items(accepted_share["list_id"])) == 2

    @pytest.mark.asyncio
    async def test_show_then_delete_by_number_targets_shared_list(self, accepted_share, simulator, ai_mock):
        """After 'Show <shared>', 'Delete N' should target the shared list, not fall back to
        a cross-type disambiguation menu. Regression for Heather's screenshot bug."""
        from models.list_model import get_list_items
        ai_mock.set_response("show grocery list", {
            "action": "show_list",
            "list_name": "Grocery List",
        })
        await simulator.send_message(PHONE_SHARED, "Show Grocery List")

        result = await simulator.send_message(PHONE_SHARED, "Delete 1")
        out = result["output"].lower()
        assert "what would you like to delete" not in out, (
            f"Delete-by-number lost shared-list context. Got: {result['output']}"
        )
        # Should ask for YES/CANCEL confirmation targeted at the shared list
        assert "milk" in out and ("yes" in out or "remove" in out), (
            f"Expected confirmation prompt for Milk from shared list. Got: {result['output']}"
        )

        confirm = await simulator.send_message(PHONE_SHARED, "YES")
        confirm_out = confirm["output"].lower()
        assert "couldn't delete" not in confirm_out, (
            f"YES after Delete-by-number failed on shared list. Got: {confirm['output']}"
        )
        assert "removed" in confirm_out or "milk" in confirm_out
        items_after = get_list_items(accepted_share["list_id"])
        assert not any(item[1].lower() == "milk" for item in items_after), (
            f"Milk still on shared list after YES: {items_after}"
        )

    @pytest.mark.asyncio
    async def test_check_by_number_targets_shared_list(self, accepted_share, simulator, ai_mock):
        """After 'Show <shared>', 'Check 1' should check off item on the shared list."""
        from models.list_model import get_list_items
        ai_mock.set_response("show grocery list", {
            "action": "show_list",
            "list_name": "Grocery List",
        })
        await simulator.send_message(PHONE_SHARED, "Show Grocery List")

        result = await simulator.send_message(PHONE_SHARED, "Check 1")
        out = result["output"].lower()
        assert "checked off" in out, f"Check-by-number failed on shared list. Got: {result['output']}"
        completed = [i for i in get_list_items(accepted_share["list_id"]) if i[2]]
        assert any("milk" in item[1].lower() for item in completed)

    @pytest.mark.asyncio
    async def test_end_to_end_add_to_shared_list_via_sms(self, accepted_share, simulator, ai_mock):
        """End-to-end via the SMS webhook — catches inline-dispatch bugs in main.py.

        Regression guard for a crash where main.py's add_to_list inline logic fell into
        an else branch that indexed list_info[0] while list_info was None (shared-list
        match had been stored in a separate variable).
        """
        ai_mock.set_response("add cheese to grocery list", {
            "action": "add_to_list",
            "list_name": "Grocery List",
            "item_text": "cheese",
        })
        result = await simulator.send_message(PHONE_SHARED, "Add cheese to Grocery List")
        out = result["output"].lower()
        assert "something went wrong" not in out, f"Handler crashed: {result['output']}"
        assert "cheese" in out, f"Item not added: {result['output']}"

    def test_ai_context_includes_shared_lists(self, accepted_share):
        """AI prompt must include shared lists so the model can route messages to them."""
        from services.ai_service import process_with_ai

        # Capture the prompt passed to OpenAI without actually calling it
        captured = {}

        class _FakeResp:
            def __init__(self):
                self.choices = [type('C', (), {'message': type('M', (), {'content': '{"action":"help","response":"ok"}'})()})()]
                self.usage = type('U', (), {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2})()

        def fake_create(**kwargs):
            captured['messages'] = kwargs.get('messages', [])
            return _FakeResp()

        with patch('services.ai_service.OpenAI') as mock_openai:
            mock_openai.return_value.chat.completions.create = fake_create
            process_with_ai("hi", PHONE_SHARED, {})

        system_prompt = next((m['content'] for m in captured.get('messages', []) if m['role'] == 'system'), "")
        assert "[Shared] Grocery List" in system_prompt, (
            f"AI system prompt missing shared list context. Got:\n{system_prompt[:800]}"
        )
