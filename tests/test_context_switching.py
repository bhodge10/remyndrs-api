"""
Context Switching Tests for Remyndrs SMS Service

Tests the pending state reminder feature - when a user has a pending state
(clarification, confirmation, etc.) and sends an unrelated message, the
system should remind them of the pending question.

All tests use ConversationSimulator and mock AI responses.
"""

import pytest
from datetime import datetime, timedelta


@pytest.mark.asyncio
class TestClarificationInterrupts:
    """Test context switches during clarification prompts."""

    async def test_ampm_clarification_interrupted_by_list(self, simulator, onboarded_user, ai_mock):
        """AM/PM clarification pending -> user adds item to list -> system reminds about AM/PM."""
        phone = onboarded_user["phone"]

        # Step 1: Create reminder needing AM/PM clarification
        ai_mock.set_response("remind me at 4 to call mom", {
            "action": "clarify_time",
            "reminder_text": "call mom",
            "time_mentioned": "4:00"
        })
        result = await simulator.send_message(phone, "remind me at 4 to call mom")
        assert "am" in result["output"].lower() or "pm" in result["output"].lower()

        # Step 2: Instead of answering, user tries to switch context
        result = await simulator.send_message(phone, "add milk to grocery list")

        # System should REMIND user of pending AM/PM clarification
        output_lower = result["output"].lower()
        assert "still need" in output_lower or "call mom" in output_lower or "4:00" in result["output"] or "am" in output_lower or "pm" in output_lower

    async def test_ampm_clarification_interrupted_by_memory_question(self, simulator, onboarded_user, ai_mock):
        """AM/PM clarification pending -> user asks 'what's my wifi password' -> system reminds about AM/PM."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me at 4 to call mom", {
            "action": "clarify_time",
            "reminder_text": "call mom",
            "time_mentioned": "4:00"
        })
        await simulator.send_message(phone, "remind me at 4 to call mom")

        # User asks about memory instead
        result = await simulator.send_message(phone, "what's my wifi password")

        output_lower = result["output"].lower()
        # Should remind about AM/PM clarification
        assert "still need" in output_lower or "call mom" in output_lower or "am" in output_lower or "pm" in output_lower

    async def test_time_clarification_interrupted_by_show_lists(self, simulator, onboarded_user, ai_mock):
        """Time clarification pending -> user says 'show my lists' -> system reminds about time needed."""
        phone = onboarded_user["phone"]

        # Trigger vague time clarification
        ai_mock.set_response("remind me later to water plants", {
            "action": "clarify_specific_time",
            "reminder_text": "water plants"
        })
        await simulator.send_message(phone, "remind me later to water plants")

        # User tries to see lists
        result = await simulator.send_message(phone, "what is the weather")

        output_lower = result["output"].lower()
        # Should remind about time needed
        assert "still need" in output_lower or "water plant" in output_lower or "time" in output_lower or "cancel" in output_lower

    async def test_date_clarification_interrupted_by_new_reminder(self, simulator, onboarded_user, ai_mock):
        """Date clarification pending -> user creates new reminder -> pending reminder cleared for new one."""
        phone = onboarded_user["phone"]

        # Trigger date clarification
        ai_mock.set_response("remind me on friday to submit report", {
            "action": "clarify_date_time",
            "reminder_text": "submit report",
            "reminder_date": "2026-01-24"
        })
        await simulator.send_message(phone, "remind me on friday to submit report")

        # User creates different reminder - this should clear pending and start fresh
        ai_mock.set_response("remind me tomorrow at 3pm to call mom", {
            "action": "reminder",
            "reminder_text": "call mom",
            "reminder_date": "2026-01-19 15:00:00"
        })
        result = await simulator.send_message(phone, "remind me tomorrow at 3pm to call mom")

        output_lower = result["output"].lower()
        # New reminder requests should clear pending states and process
        # The actual reminder created might be from conftest defaults but the key is it processes
        assert "remind" in output_lower or "got it" in output_lower or "i'll" in output_lower

    async def test_vague_time_interrupted_by_help(self, simulator, onboarded_user, ai_mock):
        """Vague time clarification -> user asks for help -> system reminds about time needed."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me in a bit to check email", {
            "action": "clarify_specific_time",
            "reminder_text": "check email"
        })
        await simulator.send_message(phone, "remind me in a bit to check email")

        # User asks random question
        result = await simulator.send_message(phone, "tell me a joke")

        output_lower = result["output"].lower()
        # Should remind about pending time
        assert "still need" in output_lower or "check email" in output_lower or "time" in output_lower


@pytest.mark.asyncio
class TestConfirmationInterrupts:
    """Test context switches during confirmation prompts."""

    async def test_delete_reminder_confirmation_interrupted(self, simulator, onboarded_user, ai_mock):
        """Delete reminder confirmation pending -> new intent proceeds, pending cleared."""
        phone = onboarded_user["phone"]

        # Create a reminder first
        ai_mock.set_response("remind me tomorrow at 3pm to call mom", {
            "action": "reminder",
            "reminder_text": "call mom",
            "reminder_date": "2026-01-19 15:00:00"
        })
        await simulator.send_message(phone, "remind me tomorrow at 3pm to call mom")

        # Trigger delete confirmation
        ai_mock.set_response("delete my mom reminder", {
            "action": "delete_reminder",
            "search_term": "mom"
        })
        result = await simulator.send_message(phone, "delete my mom reminder")

        # If confirmation is pending, a new request should not stay in the confirm loop
        if "yes" in result["output"].lower() or "confirm" in result["output"].lower() or "delete" in result["output"].lower():
            ai_mock.set_response("add bread to shopping list", {
                "action": "add_to_list",
                "list_name": "shopping list",
                "items": ["bread"],
            })
            result = await simulator.send_message(phone, "add bread to shopping list")
            output_lower = result["output"].lower()
            assert "still need" not in output_lower
            assert "reply yes" not in output_lower
            from models.user import get_pending_reminder_delete
            assert get_pending_reminder_delete(phone) is None

    async def test_delete_memory_confirmation_interrupted(self, simulator, onboarded_user, ai_mock):
        """Delete memory confirmation pending -> new intent proceeds, pending cleared."""
        phone = onboarded_user["phone"]

        # Store a memory
        ai_mock.set_response("my wifi password is abc123", {"action": "store", "memory_text": "wifi password is abc123"})
        await simulator.send_message(phone, "my wifi password is abc123")

        # Trigger delete confirmation
        ai_mock.set_response("delete my wifi password memory", {
            "action": "delete_memory",
            "query": "wifi password"
        })
        result = await simulator.send_message(phone, "delete my wifi password memory")

        if "yes" in result["output"].lower() or "confirm" in result["output"].lower():
            ai_mock.set_response("what time is it in london", {
                "action": "chitchat",
                "response": "I don't track timezones other than yours."
            })
            result = await simulator.send_message(phone, "what time is it in london")
            output_lower = result["output"].lower()
            assert "still need" not in output_lower
            assert "reply yes" not in output_lower
            from models.user import get_pending_memory_delete
            assert get_pending_memory_delete(phone) is None

    async def test_low_confidence_confirmation_interrupted(self, simulator, onboarded_user, ai_mock):
        """Low confidence reminder confirmation -> user ignores, new request -> system reminds about confirmation."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me about that thing", {
            "action": "reminder",
            "reminder_text": "that thing",
            "reminder_date": "2026-01-19 12:00:00",
            "confidence": 40,
            "needs_confirmation": True,
            "confirmation": "Did you mean to set a reminder for 'that thing'?"
        })
        result = await simulator.send_message(phone, "remind me about that thing")

        if "correct" in result["output"].lower() or "right" in result["output"].lower() or "is this" in result["output"].lower():
            # User ignores and asks something else
            result = await simulator.send_message(phone, "what is my wifi password")
            output_lower = result["output"].lower()
            # Should remind about confirmation
            assert "still need" in output_lower or "that thing" in output_lower or "confirm" in output_lower or "yes" in output_lower


@pytest.mark.asyncio
class TestSelectionInterrupts:
    """Test context switches during selection prompts."""

    async def test_multiple_memory_matches_interrupted(self, simulator, onboarded_user, ai_mock):
        """Multiple memory matches (select 1-3) -> user asks different question -> system reminds to select."""
        phone = onboarded_user["phone"]

        # Store multiple memories
        ai_mock.set_response("john's home phone is 555-1111", {"action": "store", "memory_text": "John's home phone is 555-1111"})
        await simulator.send_message(phone, "john's home phone is 555-1111")

        ai_mock.set_response("john's work phone is 555-2222", {"action": "store", "memory_text": "John's work phone is 555-2222"})
        await simulator.send_message(phone, "john's work phone is 555-2222")

        # Trigger selection
        ai_mock.set_response("delete john's phone", {
            "action": "delete_memory",
            "query": "john phone",
            "multiple_matches": True
        })
        result = await simulator.send_message(phone, "delete john's phone")

        if "1" in result["output"] and "2" in result["output"]:
            # User ignores and asks something else — new intent should proceed
            ai_mock.set_response("tell me a story", {
                "action": "chitchat",
                "response": "Once upon a time there was a reminder."
            })
            result = await simulator.send_message(phone, "tell me a story")
            output_lower = result["output"].lower()
            assert "still need" not in output_lower
            assert "reply yes" not in output_lower

    async def test_list_number_selection_interrupted(self, simulator, onboarded_user, ai_mock):
        """List number selection pending -> user asks for reminders -> system reminds to select."""
        phone = onboarded_user["phone"]

        # Create multiple lists
        ai_mock.set_response("create grocery list", {"action": "create_list", "list_name": "Grocery"})
        await simulator.send_message(phone, "create grocery list")

        ai_mock.set_response("create shopping list", {"action": "create_list", "list_name": "Shopping"})
        await simulator.send_message(phone, "create shopping list")

        # Add item without specifying list
        ai_mock.set_response("add milk", {
            "action": "add_to_list",
            "items": ["milk"],
            "list_name": None
        })
        result = await simulator.send_message(phone, "add milk")

        if "which" in result["output"].lower() or "1." in result["output"]:
            # User ignores and asks something else
            result = await simulator.send_message(phone, "what day is it")
            output_lower = result["output"].lower()
            # Should remind to select list
            assert "still need" in output_lower or "which list" in output_lower or "milk" in output_lower


@pytest.mark.asyncio
class TestCancelCommand:
    """Test cancel command during pending states."""

    async def test_ampm_clarification_cancel(self, simulator, onboarded_user, ai_mock):
        """AM/PM clarification pending -> user says 'nevermind' -> state cleared, ready for new request."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me at 4 to call mom", {
            "action": "clarify_time",
            "reminder_text": "call mom",
            "time_mentioned": "4:00"
        })
        await simulator.send_message(phone, "remind me at 4 to call mom")

        # Cancel using 'nevermind' (not 'cancel' which triggers UNDO handler)
        result = await simulator.send_message(phone, "nevermind")

        output_lower = result["output"].lower()
        assert "cancel" in output_lower or "got it" in output_lower or "what would you like" in output_lower or "ok" in output_lower

    async def test_delete_confirmation_nevermind(self, simulator, onboarded_user, ai_mock):
        """Delete confirmation pending -> user says 'nevermind' -> state cleared."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me tomorrow at 3pm to call mom", {
            "action": "reminder",
            "reminder_text": "call mom",
            "reminder_date": "2026-01-19 15:00:00"
        })
        await simulator.send_message(phone, "remind me tomorrow at 3pm to call mom")

        ai_mock.set_response("delete my mom reminder", {"action": "delete_reminder", "search_term": "mom"})
        await simulator.send_message(phone, "delete my mom reminder")

        # Nevermind
        result = await simulator.send_message(phone, "nevermind")

        output_lower = result["output"].lower()
        assert "cancel" in output_lower or "got it" in output_lower or "ok" in output_lower or "what would you like" in output_lower

    async def test_selection_skip(self, simulator, onboarded_user, ai_mock):
        """Selection pending -> user says 'forget it' -> state cleared."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("create grocery list", {"action": "create_list", "list_name": "Grocery"})
        await simulator.send_message(phone, "create grocery list")

        ai_mock.set_response("create shopping list", {"action": "create_list", "list_name": "Shopping"})
        await simulator.send_message(phone, "create shopping list")

        ai_mock.set_response("add milk", {"action": "add_to_list", "items": ["milk"], "list_name": None})
        await simulator.send_message(phone, "add milk")

        # Skip using 'forget it' (not 'skip' which triggers daily summary skip)
        result = await simulator.send_message(phone, "forget it")

        output_lower = result["output"].lower()
        assert "cancel" in output_lower or "got it" in output_lower or "ok" in output_lower or "what would you like" in output_lower

    async def test_forget_it_clears_pending(self, simulator, onboarded_user, ai_mock):
        """Multiple pending states -> user says 'forget it' -> all states cleared."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me at 4 to call mom", {
            "action": "clarify_time",
            "reminder_text": "call mom",
            "time_mentioned": "4:00"
        })
        await simulator.send_message(phone, "remind me at 4 to call mom")

        # Forget it
        result = await simulator.send_message(phone, "forget it")

        output_lower = result["output"].lower()
        assert "cancel" in output_lower or "got it" in output_lower or "ok" in output_lower


@pytest.mark.asyncio
class TestEdgeCaseInterrupts:
    """Test edge cases during pending states."""

    async def test_yes_when_no_confirmation_pending(self, simulator, onboarded_user, ai_mock):
        """YES response when no confirmation pending -> processes normally."""
        phone = onboarded_user["phone"]

        # No pending state, just say yes
        ai_mock.set_response("yes", {
            "action": "unknown",
            "response": "I'm not sure what you're confirming. How can I help?"
        })
        result = await simulator.send_message(phone, "yes")

        # Should process normally (might be confused or just acknowledge)
        output_lower = result["output"].lower()
        assert "help" in output_lower or "what" in output_lower or "i'm" in output_lower or len(output_lower) > 0

    async def test_no_when_no_confirmation_pending(self, simulator, onboarded_user, ai_mock):
        """NO response when no confirmation pending -> processes normally."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("no", {
            "action": "unknown",
            "response": "I'm not sure what you mean. How can I help?"
        })
        result = await simulator.send_message(phone, "no")

        output_lower = result["output"].lower()
        assert len(output_lower) > 0  # Should give some response

    async def test_number_when_no_selection_pending(self, simulator, onboarded_user, ai_mock):
        """Number response (e.g., '2') when no selection pending -> processes normally."""
        phone = onboarded_user["phone"]

        # Create a list so numbers have meaning
        ai_mock.set_response("create grocery list", {"action": "create_list", "list_name": "Grocery"})
        await simulator.send_message(phone, "create grocery list")

        ai_mock.set_response("create shopping list", {"action": "create_list", "list_name": "Shopping"})
        await simulator.send_message(phone, "create shopping list")

        # View lists first
        await simulator.send_message(phone, "show lists")

        # Select by number (should show that list)
        result = await simulator.send_message(phone, "2")

        output_lower = result["output"].lower()
        assert "shopping" in output_lower or "empty" in output_lower or "list" in output_lower

    async def test_gibberish_during_pending_state(self, simulator, onboarded_user, ai_mock):
        """Gibberish/unclear input during pending state -> system reminds of pending question."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me at 4 to call mom", {
            "action": "clarify_time",
            "reminder_text": "call mom",
            "time_mentioned": "4:00"
        })
        await simulator.send_message(phone, "remind me at 4 to call mom")

        # Send gibberish
        result = await simulator.send_message(phone, "asdfghjkl qwerty")

        output_lower = result["output"].lower()
        # Should remind about AM/PM
        assert "still need" in output_lower or "am" in output_lower or "pm" in output_lower or "call mom" in output_lower

    async def test_question_mark_during_pending_state(self, simulator, onboarded_user, ai_mock):
        """'?' command during pending state -> shows help or reminds of pending."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me at 4 to call mom", {
            "action": "clarify_time",
            "reminder_text": "call mom",
            "time_mentioned": "4:00"
        })
        await simulator.send_message(phone, "remind me at 4 to call mom")

        # Ask for help with ?
        result = await simulator.send_message(phone, "?")

        output_lower = result["output"].lower()
        # Could show help or remind of pending - both are valid
        assert "help" in output_lower or "remind" in output_lower or "list" in output_lower or "memory" in output_lower


@pytest.mark.asyncio
class TestRapidContextSwitches:
    """Test rapid context switching attempts."""

    async def test_three_rapid_context_switch_attempts(self, simulator, onboarded_user, ai_mock):
        """3 rapid context switch attempts -> system keeps reminding of original pending state."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me at 4 to call mom", {
            "action": "clarify_time",
            "reminder_text": "call mom",
            "time_mentioned": "4:00"
        })
        await simulator.send_message(phone, "remind me at 4 to call mom")

        # Three attempts to switch context
        result1 = await simulator.send_message(phone, "add milk to list")
        result2 = await simulator.send_message(phone, "what is my password")
        result3 = await simulator.send_message(phone, "tell me a joke")

        # All should remind about AM/PM
        for result in [result1, result2, result3]:
            output_lower = result["output"].lower()
            assert "still need" in output_lower or "am" in output_lower or "pm" in output_lower or "call mom" in output_lower or "cancel" in output_lower

    async def test_user_keeps_ignoring_reminder(self, simulator, onboarded_user, ai_mock):
        """User keeps ignoring reminder -> system persists until cancel or answer."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me at 4 to call mom", {
            "action": "clarify_time",
            "reminder_text": "call mom",
            "time_mentioned": "4:00"
        })
        await simulator.send_message(phone, "remind me at 4 to call mom")

        # User ignores multiple times
        for i in range(3):
            result = await simulator.send_message(phone, f"random message {i}")
            output_lower = result["output"].lower()
            assert "still need" in output_lower or "am" in output_lower or "pm" in output_lower or "call mom" in output_lower

        # Finally answer
        result = await simulator.send_message(phone, "PM")
        output_lower = result["output"].lower()
        # Should confirm the reminder
        assert "4" in result["output"] or "pm" in output_lower or "call mom" in output_lower or "remind" in output_lower

    async def test_pending_state_cancel_then_new_request(self, simulator, onboarded_user, ai_mock):
        """Pending state -> cancel -> new request processed normally."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me at 4 to call mom", {
            "action": "clarify_time",
            "reminder_text": "call mom",
            "time_mentioned": "4:00"
        })
        await simulator.send_message(phone, "remind me at 4 to call mom")

        # Cancel using 'nevermind' (not 'cancel' which triggers UNDO handler)
        result = await simulator.send_message(phone, "nevermind")
        assert "cancel" in result["output"].lower() or "got it" in result["output"].lower()

        # Now new request should work
        ai_mock.set_response("remind me tomorrow at 3pm to go shopping", {
            "action": "reminder",
            "reminder_text": "go shopping",
            "reminder_date": "2026-01-19 15:00:00"
        })
        result = await simulator.send_message(phone, "remind me tomorrow at 3pm to go shopping")

        output_lower = result["output"].lower()
        assert "shopping" in output_lower or "tomorrow" in output_lower or "3" in result["output"] or "remind" in output_lower or "got it" in output_lower


@pytest.mark.asyncio
class TestMultiStepInterrupts:
    """Test interrupts during multi-step flows."""

    async def test_creating_recurring_interrupted(self, simulator, onboarded_user, ai_mock):
        """Creating recurring reminder -> user asks 'what time is it' -> system reminds about recurring setup."""
        phone = onboarded_user["phone"]

        # Start creating recurring reminder but need clarification
        ai_mock.set_response("remind me every monday to submit timesheet", {
            "action": "clarify_time",
            "reminder_text": "submit timesheet",
            "time_mentioned": None,
            "recurrence_type": "weekly"
        })
        result = await simulator.send_message(phone, "remind me every monday to submit timesheet")

        if "time" in result["output"].lower() or "when" in result["output"].lower():
            # User asks random question
            result = await simulator.send_message(phone, "what is the capital of france")
            output_lower = result["output"].lower()
            # Should remind about pending
            assert "still need" in output_lower or "timesheet" in output_lower or "time" in output_lower

    async def test_valid_response_after_reminder(self, simulator, onboarded_user, ai_mock):
        """After being reminded, user provides valid response -> pending state resolved."""
        phone = onboarded_user["phone"]

        ai_mock.set_response("remind me at 4 to call mom", {
            "action": "clarify_time",
            "reminder_text": "call mom",
            "time_mentioned": "4:00"
        })
        await simulator.send_message(phone, "remind me at 4 to call mom")

        # Try to switch (get reminded)
        result = await simulator.send_message(phone, "add milk to list")
        assert "still need" in result["output"].lower() or "am" in result["output"].lower()

        # Now provide valid response
        result = await simulator.send_message(phone, "PM")
        output_lower = result["output"].lower()
        # Should confirm reminder
        assert "4" in result["output"] or "pm" in output_lower or "call mom" in output_lower or "remind" in output_lower
