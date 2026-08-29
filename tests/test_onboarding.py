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
    ZIP_ASK_DELAY_SECONDS,
    ZIP_TIMEZONE_ASK_COPY,
    ZIP_TIMEZONE_CONFIRM,
    handle_zip_timezone_reply,
    looks_like_reminder_intent,
    send_zip_timezone_ask_now,
    should_schedule_zip_timezone_ask,
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


PREVIEW_BANNED = ["in 3 minutes", "in three minutes", "i'll ask", "i will ask"]


def _reminder_count(phone):
    from database import get_db_connection, return_db_connection
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM reminders WHERE phone_number = %s", (phone,))
        return c.fetchone()[0]
    finally:
        return_db_connection(conn)


def _assert_no_preview(output: str):
    lower = output.lower()
    for banned in PREVIEW_BANNED:
        assert banned not in lower, f"Must not preview the ZIP ask: {output}"
    assert ZIP_TIMEZONE_ASK_COPY not in output.replace("&amp;", "&")
    assert "what's your zip" not in lower


class TestZipTimezoneAskScheduled:
    """Hello and first-inbound-reminder both schedule the +3 min ZIP ping when ZIP is missing."""

    @pytest.mark.asyncio
    async def test_hello_schedules_zip_ask_not_forget_nudge(
        self, simulator, clean_test_user, sms_capture
    ):
        phone = clean_test_user
        result = await simulator.send_message(phone, "Hello")
        _assert_locked_welcome(result["output"])
        _assert_no_preview(result["output"])

        zip_asks = sms_capture.scheduled("send_zip_timezone_ask")
        assert len(zip_asks) == 1
        assert zip_asks[0]["countdown"] == ZIP_ASK_DELAY_SECONDS
        assert zip_asks[0]["args"] == [phone]
        assert sms_capture.scheduled("send_engagement_nudge") == []

        vcf = sms_capture.scheduled("send_delayed_sms")
        assert len(vcf) == 1
        assert vcf[0]["countdown"] == 3600

        assert _reminder_count(phone) == 0

    @pytest.mark.asyncio
    async def test_first_inbound_reminder_schedules_zip_ask(
        self, simulator, clean_test_user, ai_mock, sms_capture
    ):
        phone = clean_test_user
        msg = "remind me tomorrow at 2pm to call mom"
        future = (datetime.utcnow() + timedelta(days=1)).replace(
            hour=14, minute=0, second=0, microsecond=0
        )
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
        _assert_no_preview(result["output"])

        zip_asks = sms_capture.scheduled("send_zip_timezone_ask")
        assert len(zip_asks) == 1
        assert zip_asks[0]["countdown"] == ZIP_ASK_DELAY_SECONDS
        assert sms_capture.scheduled("send_engagement_nudge") == []

        # User reminder only — ZIP ping is not a reminders row
        assert _reminder_count(phone) == 1
        from models.reminder import get_pending_reminders
        pending = get_pending_reminders(phone)
        assert len(pending) == 1
        assert "call mom" in pending[0][1].lower()

    @pytest.mark.asyncio
    async def test_skip_zip_ask_when_zip_already_present(
        self, simulator, clean_test_user, sms_capture
    ):
        phone = clean_test_user
        from models.user import create_or_update_user
        create_or_update_user(
            phone,
            zip_code="10001",
            timezone="America/New_York",
            onboarding_complete=False,
            onboarding_step=0,
        )
        result = await simulator.send_message(phone, "Hello")
        _assert_locked_welcome(result["output"])
        assert sms_capture.scheduled("send_zip_timezone_ask") == []
        forget = sms_capture.scheduled("send_engagement_nudge")
        assert len(forget) == 1
        assert forget[0]["countdown"] == 300


class TestZipTimezoneReply:
    """Bare ZIP after onboarded sets tz and locked confirm; does not create a reminder."""

    @pytest.mark.asyncio
    async def test_bare_zip_after_hello_sets_timezone(
        self, simulator, clean_test_user, sms_capture
    ):
        phone = clean_test_user
        await simulator.send_message(phone, "Hello")
        result = await simulator.send_message(phone, "90210")
        text = result["output"].replace("&amp;", "&").replace("I&apos;ll", "I'll")
        assert text.strip() == ZIP_TIMEZONE_CONFIRM or ZIP_TIMEZONE_CONFIRM in text

        from models.user import get_user, get_user_timezone
        user = get_user(phone)
        assert user[4] == "90210"
        assert get_user_timezone(phone) == "America/Los_Angeles"
        assert _reminder_count(phone) == 0

    @pytest.mark.asyncio
    async def test_zip_plus4_after_onboarded(self, simulator, clean_test_user):
        phone = clean_test_user
        await simulator.send_message(phone, "Hi")
        result = await simulator.send_message(phone, "60601-1234")
        text = result["output"].replace("&amp;", "&").replace("I&apos;ll", "I'll")
        assert ZIP_TIMEZONE_CONFIRM in text
        from models.user import get_user, get_user_timezone
        assert get_user(phone)[4] == "60601"
        assert get_user_timezone(phone) == "America/Chicago"

    @pytest.mark.asyncio
    async def test_already_has_zip_does_not_treat_digits_as_timezone(
        self, simulator, onboarded_user
    ):
        phone = onboarded_user["phone"]
        result = await simulator.send_message(phone, "90210")
        text = result["output"].replace("&amp;", "&").replace("I&apos;ll", "I'll")
        assert ZIP_TIMEZONE_CONFIRM not in text
        from models.user import get_user, get_user_timezone
        assert get_user(phone)[4] == "10001"
        assert get_user_timezone(phone) == "America/New_York"

    def test_handle_zip_timezone_reply_ignores_non_zip(self, onboarded_user):
        phone = onboarded_user["phone"]
        from models.user import create_or_update_user
        create_or_update_user(phone, zip_code=None)
        assert handle_zip_timezone_reply(phone, "call mom tomorrow") is None
        assert handle_zip_timezone_reply(phone, "9021") is None


class TestZipAskNotActivation:
    """ZIP ping is not a user reminder: no row, no free-tier slot, no create-confirm."""

    @pytest.mark.asyncio
    async def test_hello_zip_ping_does_not_consume_free_tier_slot(
        self, simulator, clean_test_user
    ):
        phone = clean_test_user
        result = await simulator.send_message(phone, "Hello")
        _assert_locked_welcome(result["output"])
        assert "Got it — I'll text you at" not in result["output"].replace("&amp;", "&").replace("I&apos;ll", "I'll")

        from models.user import create_or_update_user
        from services.tier_service import can_create_reminder, get_reminders_created_today
        create_or_update_user(
            phone,
            premium_status="free",
            free_tier_version=1,
            trial_end_date=datetime.utcnow() - timedelta(days=1),
        )
        assert get_reminders_created_today(phone) == 0
        allowed, _msg = can_create_reminder(phone)
        assert allowed is True

    @pytest.mark.asyncio
    async def test_zip_ask_send_is_not_a_reminder_row(
        self, simulator, clean_test_user, sms_capture
    ):
        phone = clean_test_user
        await simulator.send_message(phone, "Hello")
        before = _reminder_count(phone)
        result = send_zip_timezone_ask_now(phone)
        assert result["status"] == "sent"
        assert _reminder_count(phone) == before == 0
        bodies = [m["message"] for m in sms_capture.get_messages_to(phone)]
        assert ZIP_TIMEZONE_ASK_COPY in bodies

    @pytest.mark.asyncio
    async def test_send_skipped_if_zip_arrives_before_ping(
        self, simulator, clean_test_user, sms_capture
    ):
        phone = clean_test_user
        await simulator.send_message(phone, "Hello")
        await simulator.send_message(phone, "10001")
        result = send_zip_timezone_ask_now(phone)
        assert result == {"status": "skipped", "reason": "already_has_zip"}
        zip_bodies = [
            m["message"] for m in sms_capture.get_messages_to(phone)
            if m["message"] == ZIP_TIMEZONE_ASK_COPY
        ]
        assert zip_bodies == []


class TestZipAskBetaCompSkip:
    """Do not pile the ZIP ping onto Saturday 9am local for the beta-comp 32."""

    def test_should_skip_saturday_morning_for_target(self):
        from services.beta_comp_downgrade import should_skip_zip_ask_for_beta_comp
        tz = __import__("pytz").timezone("America/New_York")
        saturday_930 = tz.localize(datetime(2026, 8, 29, 9, 30, 0)).astimezone(
            __import__("pytz").utc
        ).replace(tzinfo=None)
        row = {
            "premium_status": "premium",
            "stripe_subscription_id": None,
            "subscription_status": None,
            "trial_end_date": datetime(2026, 8, 29),
            "timezone": "America/New_York",
            "beta_comp_warning_sent_at": None,
            "beta_comp_downgraded_at": None,
        }
        assert should_skip_zip_ask_for_beta_comp(row, saturday_930) == "beta_comp_saturday_morning"

        thursday_930 = tz.localize(datetime(2026, 8, 27, 9, 30, 0)).astimezone(
            __import__("pytz").utc
        ).replace(tzinfo=None)
        assert should_skip_zip_ask_for_beta_comp(row, thursday_930) is None

        saturday_afternoon = tz.localize(datetime(2026, 8, 29, 15, 0, 0)).astimezone(
            __import__("pytz").utc
        ).replace(tzinfo=None)
        assert should_skip_zip_ask_for_beta_comp(row, saturday_afternoon) is None

    def test_should_skip_if_beta_comp_message_already_sent_that_morning(self):
        from services.beta_comp_downgrade import should_skip_zip_ask_for_beta_comp
        tz = __import__("pytz").timezone("America/New_York")
        saturday_930 = tz.localize(datetime(2026, 8, 29, 9, 30, 0)).astimezone(
            __import__("pytz").utc
        ).replace(tzinfo=None)
        row = {
            "premium_status": "premium",
            "stripe_subscription_id": None,
            "subscription_status": None,
            "trial_end_date": datetime(2026, 8, 29),
            "timezone": "America/New_York",
            "beta_comp_warning_sent_at": None,
            "beta_comp_downgraded_at": saturday_930,
        }
        assert should_skip_zip_ask_for_beta_comp(row, saturday_930) == "beta_comp_warning"

    def test_schedule_skipped_saturday_morning_for_beta_comp(
        self, clean_test_user, sms_capture
    ):
        phone = clean_test_user
        from unittest.mock import patch
        from models.user import create_or_update_user
        from services.onboarding_service import _schedule_post_onboarding_touchbacks
        create_or_update_user(
            phone,
            timezone="America/New_York",
            onboarding_complete=True,
            premium_status="premium",
            trial_end_date=datetime(2026, 8, 29, 16, 0, 0),
        )
        tz = __import__("pytz").timezone("America/New_York")
        saturday_858 = tz.localize(datetime(2026, 8, 29, 8, 58, 0)).astimezone(
            __import__("pytz").utc
        ).replace(tzinfo=None)
        assert should_schedule_zip_timezone_ask(phone, now_utc=saturday_858) is False

        with patch(
            "services.onboarding_service.should_schedule_zip_timezone_ask",
            return_value=False,
        ):
            _schedule_post_onboarding_touchbacks(phone)
        assert sms_capture.scheduled("send_zip_timezone_ask") == []
        forget = sms_capture.scheduled("send_engagement_nudge")
        assert len(forget) == 1
        assert forget[0]["countdown"] == 300

    def test_send_skipped_saturday_9am_beta_comp(self, clean_test_user, sms_capture):
        phone = clean_test_user
        from models.user import create_or_update_user
        create_or_update_user(
            phone,
            timezone="America/New_York",
            onboarding_complete=True,
            premium_status="premium",
            trial_end_date=datetime(2026, 8, 29, 16, 0, 0),
        )
        tz = __import__("pytz").timezone("America/New_York")
        saturday_901 = tz.localize(datetime(2026, 8, 29, 9, 1, 0)).astimezone(
            __import__("pytz").utc
        ).replace(tzinfo=None)
        result = send_zip_timezone_ask_now(phone, now_utc=saturday_901)
        assert result == {"status": "skipped", "reason": "beta_comp"}
        assert ZIP_TIMEZONE_ASK_COPY not in [m["message"] for m in sms_capture.messages]


class TestZipAskLockedCopyAndNoAreaCode:
    """Locked copy is exact. No area-code timezone guess. ET default until ZIP."""

    def test_locked_copy_strings_exact(self):
        assert ZIP_TIMEZONE_ASK_COPY == "What's your ZIP so I text you at the right time?"
        assert ZIP_TIMEZONE_CONFIRM == "Got it — I'll use that timezone from now on."
        assert ZIP_ASK_DELAY_SECONDS == 180

    def test_no_area_code_timezone_path(self):
        import inspect
        import utils.timezone as tzmod
        import services.onboarding_service as ob
        for mod in (tzmod, ob):
            source = inspect.getsource(mod).lower()
            assert "area_code" not in source
            assert "area-code" not in source
            assert "area code" not in source

    @pytest.mark.asyncio
    async def test_hello_stays_eastern_until_zip(self, simulator, clean_test_user):
        phone = clean_test_user
        await simulator.send_message(phone, "Hello")
        from models.user import get_user_timezone
        assert get_user_timezone(phone) == "America/New_York"
        assert handle_zip_timezone_reply(phone, "45202") == ZIP_TIMEZONE_CONFIRM
        assert get_user_timezone(phone) == "America/New_York"  # 45202 is Cincinnati, Eastern

