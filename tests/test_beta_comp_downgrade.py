"""One-shot beta-comp wall: target selection, Thu/Fri warning, Saturday flip.

Does not restore check_trial_expirations. Copy locked by Retention.
"""

from datetime import datetime
from unittest.mock import patch

import pytz

from database import get_db_connection, return_db_connection, init_db

init_db()

from models.user import create_or_update_user
from services.beta_comp_downgrade import (
    BETA_COMP_SKIP_TRIAL_END_DATES,
    BETA_COMP_TRIAL_END_DATES,
    DOWNGRADE_COPY,
    WARNING_COPY,
    is_beta_comp_target_row,
    list_beta_comp_targets,
    process_beta_comp_downgrade,
    process_beta_comp_warnings,
    should_skip_score_for_beta_comp,
)
from services.tier_service import (
    NEW_RECURRING_LIMIT_COPY,
    V1_DAILY_REMINDER_LIMIT_COPY,
    can_create_recurring_reminder,
    can_create_reminder,
)


PHONE_PREFIX = "+1555110"
INCLUDED_START = 1
INCLUDED_COUNT = 32
SEPT_PHONES = (41, 42, 43, 44)  # two 09-07, two 09-12
MANUAL_N = 51
STRIPE_N = 52
ACTIVE_NO_STRIPE_N = 53


def _phone(n: int) -> str:
    return f"{PHONE_PREFIX}{n:04d}"


def _et_morning(day, month=8, year=2026, hour=9, minute=30):
    tz = pytz.timezone("America/New_York")
    local = tz.localize(datetime(year, month, day, hour, minute, 0))
    return local.astimezone(pytz.utc).replace(tzinfo=None)


def _cleanup_cohort():
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            "DELETE FROM recurring_reminders WHERE phone_number LIKE %s",
            (PHONE_PREFIX + "%",),
        )
        c.execute(
            "DELETE FROM reminders WHERE phone_number LIKE %s",
            (PHONE_PREFIX + "%",),
        )
        c.execute(
            "DELETE FROM sms_outbound_log WHERE phone_number LIKE %s",
            (PHONE_PREFIX + "%",),
        )
        c.execute("DELETE FROM users WHERE phone_number LIKE %s", (PHONE_PREFIX + "%",))
        conn.commit()
    finally:
        return_db_connection(conn)


def _insert_user(
    phone,
    trial_end,
    premium_status="premium",
    stripe_id=None,
    sub_status=None,
    timezone="America/New_York",
):
    create_or_update_user(
        phone,
        first_name="Comp",
        timezone=timezone,
        onboarding_complete=True,
        premium_status=premium_status,
        trial_end_date=trial_end,
        stripe_subscription_id=stripe_id,
        subscription_status=sub_status,
        opted_out=False,
        lifecycle_messages_opted_out=False,
    )


def _seed_target_set():
    """32 included + 4 September + manual + stripe + active-without-stripe."""
    aug = datetime(2026, 8, 29, 16, 0, 0)
    jun = datetime(2026, 6, 18, 16, 0, 0)
    sept7 = datetime(2026, 9, 7, 16, 0, 0)
    sept12 = datetime(2026, 9, 12, 16, 0, 0)

    for i in range(INCLUDED_START, INCLUDED_START + 16):
        _insert_user(_phone(i), aug)
    for i in range(INCLUDED_START + 16, INCLUDED_START + INCLUDED_COUNT):
        _insert_user(_phone(i), jun)

    _insert_user(_phone(41), sept7)
    _insert_user(_phone(42), sept7)
    _insert_user(_phone(43), sept12)
    _insert_user(_phone(44), sept12)

    _insert_user(_phone(MANUAL_N), aug, sub_status="manual")
    _insert_user(_phone(STRIPE_N), aug, stripe_id="sub_live", sub_status="active")
    _insert_user(_phone(ACTIVE_NO_STRIPE_N), aug, sub_status="active")


def _user_row(phone):
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT premium_status, trial_end_date, subscription_status,
                   stripe_subscription_id, beta_comp_warning_sent_at,
                   beta_comp_downgraded_at, free_tier_version, timezone
            FROM users WHERE phone_number = %s
            """,
            (phone,),
        )
        row = c.fetchone()
        return {
            "premium_status": row[0],
            "trial_end_date": row[1],
            "subscription_status": row[2],
            "stripe_subscription_id": row[3],
            "beta_comp_warning_sent_at": row[4],
            "beta_comp_downgraded_at": row[5],
            "free_tier_version": row[6],
            "timezone": row[7],
            "phone_number": phone,
        }
    finally:
        return_db_connection(conn)


class TestTargetSelection:
    def setup_method(self):
        _cleanup_cohort()
        _seed_target_set()

    def teardown_method(self):
        _cleanup_cohort()

    def test_selects_32_and_skips_sept_manual_stripe(self):
        targets = list_beta_comp_targets()
        phones = {t["phone_number"] for t in targets}
        included = {_phone(i) for i in range(INCLUDED_START, INCLUDED_START + INCLUDED_COUNT)}
        assert phones == included
        assert len(targets) == 32

        for n in SEPT_PHONES:
            row = _user_row(_phone(n))
            assert is_beta_comp_target_row(row) is False
            assert _as_trial_date(row["trial_end_date"]) in BETA_COMP_SKIP_TRIAL_END_DATES

        assert is_beta_comp_target_row(_user_row(_phone(MANUAL_N))) is False
        assert is_beta_comp_target_row(_user_row(_phone(STRIPE_N))) is False
        assert is_beta_comp_target_row(_user_row(_phone(ACTIVE_NO_STRIPE_N))) is False

        # Included dates are only the two wall dates
        dates = {_as_trial_date(t["trial_end_date"]) for t in targets}
        assert dates == set(BETA_COMP_TRIAL_END_DATES)


def _as_trial_date(value):
    return value.date() if hasattr(value, "date") else value


class TestWarningOnceOnly:
    def setup_method(self):
        _cleanup_cohort()
        _insert_user(_phone(1), datetime(2026, 8, 29, 16, 0, 0))

    def teardown_method(self):
        _cleanup_cohort()

    def test_sends_thursday_skips_friday(self):
        phone = _phone(1)
        thursday = _et_morning(27)
        friday = _et_morning(28)
        with patch("services.beta_comp_downgrade.send_sms") as mock_sms:
            mock_sms.return_value = None
            first = process_beta_comp_warnings(now_utc=thursday)
            second = process_beta_comp_warnings(now_utc=friday)
        assert first["warnings_sent"] == 1
        assert second["warnings_sent"] == 0
        assert mock_sms.call_count == 1
        assert mock_sms.call_args.args[1] == WARNING_COPY
        row = _user_row(phone)
        assert row["beta_comp_warning_sent_at"] is not None
        assert row["premium_status"] == "premium"

    def test_wednesday_does_not_send(self):
        wednesday = _et_morning(26)
        with patch("services.beta_comp_downgrade.send_sms") as mock_sms:
            result = process_beta_comp_warnings(now_utc=wednesday)
        assert result["warnings_sent"] == 0
        mock_sms.assert_not_called()

    def test_reminder_same_morning_does_not_block_warning(self):
        phone = _phone(1)
        thursday = _et_morning(27)
        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute(
                "INSERT INTO sms_outbound_log (phone_number, message_type, created_at) "
                "VALUES (%s, %s, %s)",
                (phone, "reminder", thursday),
            )
            conn.commit()
        finally:
            return_db_connection(conn)
        with patch("services.beta_comp_downgrade.send_sms") as mock_sms:
            mock_sms.return_value = None
            result = process_beta_comp_warnings(now_utc=thursday)
        assert result["warnings_sent"] == 1
        mock_sms.assert_called_once()

    def test_skips_manual_and_stripe(self):
        _insert_user(_phone(MANUAL_N), datetime(2026, 8, 29, 16, 0, 0), sub_status="manual")
        _insert_user(
            _phone(STRIPE_N),
            datetime(2026, 8, 29, 16, 0, 0),
            stripe_id="sub_live",
            sub_status="active",
        )
        thursday = _et_morning(27)
        with patch("services.beta_comp_downgrade.send_sms") as mock_sms:
            mock_sms.return_value = None
            result = process_beta_comp_warnings(now_utc=thursday)
        sent_to = {c.args[0] for c in mock_sms.call_args_list}
        assert _phone(1) in sent_to
        assert _phone(MANUAL_N) not in sent_to
        assert _phone(STRIPE_N) not in sent_to
        assert result["warnings_sent"] == 1


class TestSaturdayFlipAndConfirm:
    def setup_method(self):
        _cleanup_cohort()
        _insert_user(_phone(1), datetime(2026, 8, 29, 16, 0, 0))

    def teardown_method(self):
        _cleanup_cohort()

    def test_thursday_does_not_flip(self):
        thursday = _et_morning(27)
        with patch("services.beta_comp_downgrade.send_sms") as mock_sms:
            result = process_beta_comp_downgrade(now_utc=thursday)
        assert result["downgraded"] == 0
        mock_sms.assert_not_called()
        assert _user_row(_phone(1))["premium_status"] == "premium"

    def test_saturday_flips_sends_confirm_and_is_idempotent(self):
        phone = _phone(1)
        thursday = _et_morning(27)
        saturday = _et_morning(29)
        with patch("services.beta_comp_downgrade.send_sms") as mock_sms:
            mock_sms.return_value = None
            process_beta_comp_warnings(now_utc=thursday)
            first = process_beta_comp_downgrade(now_utc=saturday)
            second = process_beta_comp_downgrade(now_utc=saturday)
        assert first["downgraded"] == 1
        assert first["confirms_sent"] == 1
        assert second["downgraded"] == 0
        bodies = [c.args[1] for c in mock_sms.call_args_list]
        assert WARNING_COPY in bodies
        assert DOWNGRADE_COPY in bodies
        assert bodies.count(DOWNGRADE_COPY) == 1

        row = _user_row(phone)
        assert row["premium_status"] == "free"
        assert row["beta_comp_downgraded_at"] is not None
        assert row["free_tier_version"] == 1
        # Trial must not still look active to get_user_tier
        assert row["trial_end_date"] <= saturday

    def test_saturday_skips_sept_manual_stripe(self):
        _insert_user(_phone(41), datetime(2026, 9, 7, 16, 0, 0))
        _insert_user(_phone(MANUAL_N), datetime(2026, 8, 29, 16, 0, 0), sub_status="manual")
        _insert_user(
            _phone(STRIPE_N),
            datetime(2026, 8, 29, 16, 0, 0),
            stripe_id="sub_live",
            sub_status="active",
        )
        saturday = _et_morning(29)
        with patch("services.beta_comp_downgrade.send_sms") as mock_sms:
            mock_sms.return_value = None
            result = process_beta_comp_downgrade(now_utc=saturday)
        flipped = {c.args[0] for c in mock_sms.call_args_list}
        assert _phone(1) in flipped
        assert _phone(41) not in flipped
        assert _phone(MANUAL_N) not in flipped
        assert _phone(STRIPE_N) not in flipped
        assert result["downgraded"] == 1
        assert _user_row(_phone(41))["premium_status"] == "premium"
        assert _user_row(_phone(MANUAL_N))["premium_status"] == "premium"
        assert _user_row(_phone(STRIPE_N))["premium_status"] == "premium"

    def test_existing_recurring_keeps_generating_after_flip(self, onboarded_user):
        phone = onboarded_user["phone"]
        create_or_update_user(
            phone,
            premium_status="premium",
            trial_end_date=datetime(2026, 8, 29, 16, 0, 0),
            stripe_subscription_id=None,
            subscription_status=None,
        )
        from models.reminder import save_recurring_reminder

        recurring_id = save_recurring_reminder(
            phone, "Daily vitamin", "daily", None, "09:00", "America/New_York"
        )
        saturday = _et_morning(29)
        with patch("services.beta_comp_downgrade.send_sms"):
            process_beta_comp_downgrade(now_utc=saturday)
        assert _user_row(phone)["premium_status"] == "free"

        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute("DELETE FROM reminders WHERE recurring_id = %s", (recurring_id,))
            conn.commit()
        finally:
            return_db_connection(conn)

        with patch("services.sms_service.send_sms"):
            from tasks.reminder_tasks import generate_recurring_reminders

            generate_recurring_reminders()

        conn = get_db_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM reminders WHERE recurring_id = %s", (recurring_id,))
            count = c.fetchone()[0]
        finally:
            return_db_connection(conn)
        assert count > 0


class TestLimitHitCopy:
    @patch("services.tier_service.BETA_MODE", False)
    @patch("services.tier_service.get_reminders_created_today", return_value=2)
    @patch("services.tier_service.get_user_free_tier_version", return_value=1)
    @patch("services.tier_service.get_user_tier", return_value="free")
    def test_v1_third_reminder_today(self, _tier, _ver, _count):
        allowed, msg = can_create_reminder("+15559876543")
        assert allowed is False
        assert msg == V1_DAILY_REMINDER_LIMIT_COPY
        assert msg == (
            "You're at 2 reminders today on Free. "
            "Text UPGRADE for unlimited, or try again tomorrow."
        )

    @patch("services.tier_service.BETA_MODE", False)
    @patch("services.tier_service.get_user_tier", return_value="free")
    def test_new_recurring_blocked_copy(self, _tier):
        allowed, msg = can_create_recurring_reminder("+15559876543")
        assert allowed is False
        assert msg == NEW_RECURRING_LIMIT_COPY
        assert msg == (
            "New repeating reminders are Premium. "
            "Text UPGRADE to set this one, or send it as a one-off."
        )


class TestSportsSkipSameMorning:
    def setup_method(self):
        _cleanup_cohort()
        _insert_user(_phone(1), datetime(2026, 8, 29, 16, 0, 0))

    def teardown_method(self):
        _cleanup_cohort()

    def test_skip_if_warning_sent_that_local_morning(self):
        phone = _phone(1)
        thursday = _et_morning(27)
        with patch("services.beta_comp_downgrade.send_sms"):
            process_beta_comp_warnings(now_utc=thursday)
        row = _user_row(phone)
        assert should_skip_score_for_beta_comp(row, thursday) == "beta_comp_warning"

    def test_skip_target_thursday_even_before_send(self):
        row = _user_row(_phone(1))
        thursday = _et_morning(27)
        assert should_skip_score_for_beta_comp(row, thursday) == "beta_comp_warning"

    def test_does_not_skip_unrelated_tuesday(self):
        row = _user_row(_phone(1))
        tuesday = _et_morning(25)
        assert should_skip_score_for_beta_comp(row, tuesday) is None


class TestCeleryScheduleIsOneShotNotTrialRestore:
    def test_check_trial_expirations_stays_unscheduled(self):
        from celery_config import beat_schedule

        task_names = [v["task"] for v in beat_schedule.values()]
        assert "tasks.reminder_tasks.check_trial_expirations" not in task_names
        assert "tasks.reminder_tasks.send_beta_comp_warnings" in task_names
        assert "tasks.reminder_tasks.send_beta_comp_downgrade" in task_names
        assert not any("invite" in t for t in task_names)

        source = open("celery_config.py", encoding="utf-8").read()
        assert '# "check-trial-expirations":' in source
        assert "check-trial-expirations" not in beat_schedule
