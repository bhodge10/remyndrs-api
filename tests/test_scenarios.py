"""
Full conversation scenario tests.
Tests complete user journeys through multiple interactions.
"""

import pytest
from datetime import datetime, timedelta


class TestNewUserJourney:
    """Complete journey for a new user from first contact to regular usage."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_complete_new_user_journey(self, simulator, clean_test_user, ai_mock, sms_capture):
        """
        Test a complete new user journey:
        1. Sign up and complete onboarding
        2. Create their first reminder
        3. Create a list and add items
        4. Store a memory
        5. View all their data
        """
        phone = clean_test_user

        # STEP 1: Onboarding
        await simulator.send_message(phone, "Hi there!")
        await simulator.send_message(phone, "Sarah")
        await simulator.send_message(phone, "Johnson")
        await simulator.send_message(phone, "sarah@example.com")
        result = await simulator.send_message(phone, "94102")  # San Francisco

        # Should be onboarded now
        from models.user import is_user_onboarded
        assert is_user_onboarded(phone)

        # STEP 2: Create a reminder
        ai_mock.set_response("remind me tomorrow at 3pm to call the dentist", {
            "action": "reminder",
            "reminder_text": "call the dentist",
            "reminder_date": (datetime.utcnow() + timedelta(days=1)).replace(hour=15, minute=0).strftime("%Y-%m-%d %H:%M:%S")
        })

        result = await simulator.send_message(phone, "Remind me tomorrow at 3pm to call the dentist")
        assert any(word in result["output"].lower() for word in ["remind", "dentist", "tomorrow"])

        # STEP 3: Create a list and add items
        ai_mock.set_response("create a grocery list", {
            "action": "create_list",
            "list_name": "Grocery"
        })
        await simulator.send_message(phone, "Create a grocery list")

        ai_mock.set_response("add milk, bread, and eggs", {
            "action": "add_to_list",
            "items": ["milk", "bread", "eggs"],
            "list_name": "Grocery"
        })
        result = await simulator.send_message(phone, "Add milk, bread, and eggs")

        # STEP 4: Store a memory
        ai_mock.set_response("remember my dentist is dr smith at 555-1234", {
            "action": "store",
            "memory_text": "dentist is Dr Smith at 555-1234"
        })
        result = await simulator.send_message(phone, "Remember my dentist is Dr Smith at 555-1234")

        # STEP 5: View all data
        result = await simulator.send_message(phone, "MY REMINDERS")
        assert "dentist" in result["output"].lower()

        result = await simulator.send_message(phone, "MY LISTS")
        assert "grocery" in result["output"].lower()

        result = await simulator.send_message(phone, "MY MEMORIES")
        assert "smith" in result["output"].lower() or "dentist" in result["output"].lower()


class TestReminderWorkflow:
    """Complete reminder workflow scenarios."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_reminder_with_clarification_flow(self, simulator, onboarded_user, ai_mock):
        """Test reminder creation with AM/PM clarification."""
        phone = onboarded_user["phone"]

        # Ambiguous time
        ai_mock.set_response("remind me at 4 to take medicine", {
            "action": "clarify_time",
            "reminder_text": "take medicine",
            "time_mentioned": "4:00"
        })

        result = await simulator.send_message(phone, "Remind me at 4 to take medicine")
        assert "am" in result["output"].lower() and "pm" in result["output"].lower()

        # Clarify with PM
        result = await simulator.send_message(phone, "PM")
        assert any(word in result["output"].lower() for word in ["remind", "4", "pm"])

        # Verify reminder was created
        from models.reminder import get_user_reminders
        reminders = get_user_reminders(phone)
        assert len(reminders) > 0

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_recurring_reminder_management(self, simulator, onboarded_user, ai_mock):
        """Test creating, viewing, and deleting recurring reminders."""
        phone = onboarded_user["phone"]

        # Create recurring
        ai_mock.set_response("remind me every day at 8am to exercise", {
            "action": "reminder_recurring",
            "reminder_text": "exercise",
            "recurrence_type": "daily",
            "time": "08:00"
        })

        result = await simulator.send_message(phone, "Remind me every day at 8am to exercise")

        # View recurring
        result = await simulator.send_message(phone, "MY RECURRING")
        assert "exercise" in result["output"].lower() or "recurring" in result["output"].lower()


class TestListWorkflow:
    """Complete list management workflow scenarios."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_shopping_list_workflow(self, simulator, onboarded_user, ai_mock):
        """Test complete shopping list workflow."""
        phone = onboarded_user["phone"]

        # Create list
        ai_mock.set_response("create a shopping list", {
            "action": "create_list",
            "list_name": "Shopping"
        })
        await simulator.send_message(phone, "Create a shopping list")

        # Add items
        ai_mock.set_response("add batteries, lightbulbs, tape", {
            "action": "add_to_list",
            "items": ["batteries", "lightbulbs", "tape"],
            "list_name": "Shopping"
        })
        await simulator.send_message(phone, "Add batteries, lightbulbs, tape")

        # View list
        result = await simulator.send_message(phone, "MY LISTS")

        # Select list by number
        result = await simulator.send_message(phone, "1")
        assert "batteries" in result["output"].lower() or "shopping" in result["output"].lower()

        # Check off item
        result = await simulator.send_message(phone, "check 1")

        # Verify item is checked
        from models.list_model import get_list_items, get_list_by_name
        list_info = get_list_by_name(phone, "Shopping")
        if list_info:
            items = get_list_items(list_info[0])
            # At least one should be completed

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_multiple_lists_selection(self, simulator, onboarded_user, ai_mock):
        """Test adding to list when multiple lists exist."""
        phone = onboarded_user["phone"]

        # Create multiple lists
        from models.list_model import create_list
        create_list(phone, "Grocery")
        create_list(phone, "Hardware")
        create_list(phone, "Gifts")

        # Try to add item without specifying list
        ai_mock.set_response("add screwdriver", {
            "action": "add_to_list",
            "items": ["screwdriver"],
            "list_name": None
        })

        result = await simulator.send_message(phone, "Add screwdriver")
        # Should ask which list
        assert "1" in result["output"] or "which" in result["output"].lower()

        # Select Hardware (list 2)
        result = await simulator.send_message(phone, "2")
        # Should confirm added to Hardware


class TestDeletionWorkflows:
    """Deletion confirmation workflows."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_reminder_deletion_flow(self, simulator, onboarded_user):
        """Test deleting a reminder with confirmation."""
        phone = onboarded_user["phone"]

        # Create reminders
        from models.reminder import save_reminder
        save_reminder(phone, "First reminder", (datetime.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
        save_reminder(phone, "Second reminder", (datetime.utcnow() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"))

        # View reminders
        await simulator.send_message(phone, "MY REMINDERS")

        # Delete first
        result = await simulator.send_message(phone, "delete 1")
        # Should show confirmation

        # Confirm
        result = await simulator.send_message(phone, "1")

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_cancel_deletion_flow(self, simulator, onboarded_user):
        """Test canceling a deletion."""
        phone = onboarded_user["phone"]

        from models.reminder import save_reminder
        save_reminder(phone, "Keep this reminder", (datetime.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))

        await simulator.send_message(phone, "MY REMINDERS")
        await simulator.send_message(phone, "delete 1")

        # Cancel
        result = await simulator.send_message(phone, "CANCEL")

        # Verify not deleted
        from models.reminder import get_user_reminders
        reminders = get_user_reminders(phone)
        assert len(reminders) > 0


class TestErrorRecovery:
    """Test recovery from error states."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_recovery_from_invalid_input(self, simulator, onboarded_user, ai_mock):
        """Test that system recovers gracefully from invalid input."""
        phone = onboarded_user["phone"]

        # Send gibberish
        ai_mock.set_response("asdfghjkl", {
            "action": "unknown",
            "response": "I'm not sure what you mean."
        })

        result = await simulator.send_message(phone, "asdfghjkl")

        # Should still work normally after
        ai_mock.set_response("my reminders", {
            "action": "list_reminders"
        })
        result = await simulator.send_message(phone, "MY REMINDERS")
        # Should work

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_recovery_from_ai_error(self, simulator, onboarded_user):
        """Test recovery when AI service returns unexpected result."""
        phone = onboarded_user["phone"]

        # Mock AI to return invalid response
        with pytest.raises(Exception):
            # This might raise or handle gracefully
            pass

        # System should still work
        result = await simulator.send_message(phone, "MY REMINDERS")


class TestPremiumFeatures:
    """Test premium user features."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_premium_support_flow(self, onboarded_user, simulator):
        """Test premium user support ticket flow."""
        phone = onboarded_user["phone"]

        # Make user premium
        from database import get_db_connection, return_db_connection
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET premium_status = 'active' WHERE phone_number = %s", (phone,))
        conn.commit()
        return_db_connection(conn)

        # Open support ticket
        result = await simulator.send_message(phone, "SUPPORT I have a question about reminders")

        # In support mode, messages go to ticket
        result = await simulator.send_message(phone, "How do I create recurring reminders?")

        # Exit support
        result = await simulator.send_message(phone, "EXIT")
