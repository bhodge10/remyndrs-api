"""
Tests for reminder-first onboarding.

First inbound Hello/START/empty gets the locked welcome (no AI-powered, no
name/ZIP). A first inbound that looks like a reminder creates it and uses the
locked confirm. Leftover "finish setup first" must not trap a reminder.
"""

from datetime import datetime, timedelta

import pytest

from services.onboarding_service import (
    FIRST_REPLY_WELCOME,
    looks_like_reminder_intent,
)


def _future_reminder_date(hour=14):
    return (datetime.utcnow() + timedelta(days=1)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    ).strftime("%Y-%m-%d %H:%M:%S")


LOCKED_WELCOME_MARKERS = [
    "Welcome to Remyndrs. You just forgot something",
    "No app, no card. Reply STOP anytime.",
]
BANNED_FIRST_REPLY = ["ai-powered", "first name", "zip", "ai powered", "artificial intelligence"]


def _assert_locked_welcome(output: str):
    text = output.replace("&amp;", "&")
    for marker in LOCKED_WELCOME_MARKERS:
        assert marker in text, f"Missing locked welcome line {marker!r} in: {text}"
    lower = text.lower()
    for banned in BANNED_FIRST_REPLY:
        assert banned not in lower, f"First reply must not contain {banned!r}: {text}"
    assert "what's your" not in lower
    assert "tap to save" not in lower


def _assert_locked_reminder_confirm(output: str):
    text = output.replace("&amp;", "&")
    assert "Got it — I'll text you at " in text or "Got it — I&apos;ll text you at " in text
    assert "Pin this chat so I don't land in spam." in text or "Pin this chat so I don&apos;t land in spam." in text
    assert "No app, no card. Reply STOP anytime." in text
    lower = text.lower()
    for banned in BANNED_FIRST_REPLY:
        assert banned not in lower, f"First reply must not contain {banned!r}: {text}"
    assert "finish setup first" not in lower
    assert "tap to save" not in lower


class TestLooksLikeReminderIntent:
    """Classifier for first-inbound reminder vs greeting (no DB)."""

    def test_greetings_are_not_reminders(self):
        for msg in ["Hello", "Hi", "START", "  ", "", "YES", "hey, sign me up!", "GO"]:
            assert looks_like_reminder_intent(msg) is False, msg

    def test_explicit_remind_is_reminder(self):
        assert looks_like_reminder_intent("remind me tomorrow at 2pm to call mom") is True
        assert looks_like_reminder_intent("Remind me at 8am to take meds") is True

    def test_what_plus_when_is_reminder(self):
        assert looks_like_reminder_intent("call mom at 3pm") is True
        assert looks_like_reminder_intent("pick up kids tomorrow") is True

    def test_view_reminders_is_not_create(self):
        assert looks_like_reminder_intent("my reminders") is False
        assert looks_like_reminder_intent("show reminders") is False


class TestReminderFirstWelcome:
    """Hello / START / empty first reply is the locked welcome."""

    @pytest.mark.asyncio
    async def test_hello_first_reply_is_locked_welcome(self, simulator, clean_test_user):
        phone = clean_test_user
        result = await simulator.send_message(phone, "Hello")
        _assert_locked_welcome(result["output"])
        assert result["output"].strip() == FIRST_REPLY_WELCOME or FIRST_REPLY_WELCOME in result["output"]

        from models.user import is_user_onboarded, get_user_timezone
        assert is_user_onboarded(phone)
        assert get_user_timezone(phone) == "America/New_York"

    @pytest.mark.asyncio
    async def test_start_first_reply_is_locked_welcome(self, simulator, clean_test_user):
        phone = clean_test_user
        result = await simulator.send_message(phone, "START")
        _assert_locked_welcome(result["output"])

        from models.user import is_user_onboarded
        assert is_user_onboarded(phone)

    @pytest.mark.asyncio
    async def test_empty_first_reply_is_locked_welcome(self, simulator, clean_test_user):
        phone = clean_test_user
        result = await simulator.send_message(phone, "")
        _assert_locked_welcome(result["output"])

        from models.user import is_user_onboarded
        assert is_user_onboarded(phone)

    @pytest.mark.asyncio
    async def test_hi_does_not_ask_name_or_zip(self, simulator, clean_test_user):
        phone = clean_test_user
        result = await simulator.send_message(phone, "Hi")
        _assert_locked_welcome(result["output"])
        assert "zip" not in result["output"].lower()
        assert "first name" not in result["output"].lower()


class TestReminderFirstInboundCreates:
    """A first inbound that looks like a reminder creates it and uses locked confirm."""

    @pytest.mark.asyncio
    async def test_first_inbound_reminder_creates_and_locked_confirm(
        self, simulator, clean_test_user, ai_mock
    ):
        phone = clean_test_user
        msg = "remind me tomorrow at 2pm to call mom"
        future = (datetime.utcnow() + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
        reminder_response = {
            "action": "reminder",
            "reminder_text": "call mom",
            "reminder_date": future.strftime("%Y-%m-%d %H:%M:%S"),
            "confidence": 100,
        }
        ai_mock.set_response(msg, reminder_response)
        ai_mock.set_response("remind me tomorrow at 2:pm to call mom", reminder_response)
        ai_mock.set_response("remind me tomorrow at 2:PM to call mom", reminder_response)

        result = await simulator.send_message(phone, msg)
        _assert_locked_reminder_confirm(result["output"])
        assert "{time}" not in result["output"]

        from models.user import is_user_onboarded, get_user_timezone
        from models.reminder import get_pending_reminders
        assert is_user_onboarded(phone)
        assert get_user_timezone(phone) == "America/New_York"
        pending = get_pending_reminders(phone)
        assert len(pending) >= 1
        assert "call mom" in pending[0][1].lower()

    @pytest.mark.asyncio
    async def test_what_plus_when_first_inbound_creates(
        self, simulator, clean_test_user, ai_mock
    ):
        phone = clean_test_user
        msg = "call mom at 3pm"
        reminder_response = {
            "action": "reminder",
            "reminder_text": "call mom",
            "reminder_date": _future_reminder_date(15),
            "confidence": 100,
        }
        ai_mock.set_response(msg, reminder_response)
        ai_mock.set_response("call mom at 3:pm", reminder_response)
        ai_mock.set_response("call mom at 3:PM", reminder_response)

        result = await simulator.send_message(phone, msg)
        _assert_locked_reminder_confirm(result["output"])

        from models.reminder import get_pending_reminders
        pending = get_pending_reminders(phone)
        assert len(pending) >= 1


class TestFinishSetupFirstNoLongerTrapsReminder:
    """Leftover name/ZIP users must be able to set a reminder."""

    @pytest.mark.asyncio
    async def test_mid_setup_reminder_is_not_blocked(self, simulator, clean_test_user, ai_mock):
        phone = clean_test_user
        from models.user import create_or_update_user, is_user_onboarded
        create_or_update_user(
            phone,
            first_name="John",
            onboarding_complete=False,
            onboarding_step=1,
        )
        assert is_user_onboarded(phone) is not True

        msg = "remind me tomorrow at 2pm to call mom"
        reminder_response = {
            "action": "reminder",
            "reminder_text": "call mom",
            "reminder_date": _future_reminder_date(),
            "confidence": 100,
        }
        ai_mock.set_response(msg, reminder_response)
        ai_mock.set_response("remind me tomorrow at 2:pm to call mom", reminder_response)
        ai_mock.set_response("remind me tomorrow at 2:PM to call mom", reminder_response)

        result = await simulator.send_message(phone, msg)
        lower = result["output"].lower()
        assert "finish setup first" not in lower
        assert "first name" not in lower
        assert "zip" not in lower

        from models.reminder import get_pending_reminders
        from models.user import is_user_onboarded as onboarded
        assert onboarded(phone)
        assert len(get_pending_reminders(phone)) >= 1


class TestOnboardingEdgeCases:
    """Edge cases and special scenarios for onboarding."""

    @pytest.mark.asyncio
    async def test_already_onboarded_user(self, simulator, onboarded_user):
        """Test that already onboarded user doesn't restart onboarding."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "Hi")
        assert "first name" not in result["output"].lower()
        # Must not re-send the new-user locked welcome either
        assert "You just forgot something" not in result["output"]

    @pytest.mark.asyncio
    async def test_second_message_after_welcome_can_set_reminder(
        self, simulator, clean_test_user, ai_mock
    ):
        phone = clean_test_user
        await simulator.send_message(phone, "Hello")

        msg = "remind me tomorrow at 2pm to call mom"
        reminder_response = {
            "action": "reminder",
            "reminder_text": "call mom",
            "reminder_date": _future_reminder_date(),
            "confidence": 100,
        }
        ai_mock.set_response(msg, reminder_response)
        ai_mock.set_response("remind me tomorrow at 2:pm to call mom", reminder_response)
        ai_mock.set_response("remind me tomorrow at 2:PM to call mom", reminder_response)

        result = await simulator.send_message(phone, msg)
        lower = result["output"].lower()
        assert "finish setup first" not in lower
        from models.reminder import get_pending_reminders
        assert len(get_pending_reminders(phone)) >= 1


class TestOnboardingTimezones:
    """Timezone defaults to America/New_York when ZIP is not collected."""

    @pytest.mark.asyncio
    async def test_hello_defaults_to_eastern(self, simulator, clean_test_user):
        phone = clean_test_user
        await simulator.send_message(phone, "Hi")
        from models.user import get_user_timezone
        assert get_user_timezone(phone) == "America/New_York"


class TestBetaCompPathUntouched:
    """This PR must not change Saturday beta-comp warning/downgrade copy."""

    def test_locked_beta_comp_copy_unchanged(self):
        from services.beta_comp_downgrade import WARNING_COPY, DOWNGRADE_COPY
        assert WARNING_COPY == (
            "You've had full Premium. Saturday you go to Free "
            "(2 reminders/day, 2 lists, 5 memories, no new recurring). "
            "Existing recurring keep running.\n\n"
            "Text UPGRADE to keep unlimited — $8.99/mo or $89.99/yr ($7.50/mo)."
        )
        assert DOWNGRADE_COPY == (
            "You're on Free now: 2 reminders/day, 2 lists, 5 memories. "
            "New recurring is Premium.\n\n"
            "Text UPGRADE to get unlimited back — $8.99/mo or $89.99/yr."
        )
