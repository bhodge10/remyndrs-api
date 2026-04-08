"""
Tests for Shared Lists feature.
Covers: sharing flow, permissions, limits, accept/decline, and list operations on shared lists.
"""

import pytest
import json

PHONE_OWNER = "+15559876543"   # Premium owner (same as test_phone)
PHONE_SHARED = "+15559876544"  # Shared user (free tier)


@pytest.fixture(autouse=True)
def clean_shared_list_data():
    """Clean up shared list test data before and after each test."""
    from database import get_db_connection, return_db_connection

    def cleanup():
        conn = None
        try:
            conn = get_db_connection()
            c = conn.cursor()
            for phone in [PHONE_OWNER, PHONE_SHARED]:
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
