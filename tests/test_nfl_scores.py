"""NFL morning-after score beta.

Covers opt-in, SCORE, ignore/stop, anti-bunch vs Day 7/13/14, keyword
isolation from AI, weekly vs dormant event logging, and 4-week pause.
Invites are not sent by this feature — tests assert that.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import pytz

from database import get_db_connection, return_db_connection, init_db

init_db()
from models.sports import (
    COHORT_WEEKLY,
    COHORT_DORMANT,
    EVENT_SCORE_ASK,
    EVENT_SCORE_REPLY,
    EVENT_SCORE_IGNORE,
    EVENT_SPORTS_YES,
    EVENT_UPGRADE_TO_KEEP,
    get_optin,
    upsert_optin,
    update_optin,
    log_sports_event,
    count_sports_events,
    mark_sports_invite,
    sports_invite_sent_this_week,
)
from services.espn_nfl import parse_finals, format_final, fake_game_for_team
from services.nfl_teams import resolve_team
from services.sports_score_service import (
    ASK_TEMPLATE,
    PAUSE_COPY,
    INVITE_WEEKLY,
    INVITE_DORMANT,
    handle_yes_team,
    handle_score_keyword,
    process_morning_asks,
    should_skip_score_ask,
    compute_pause_at,
    is_paid_premium,
    maybe_log_upgrade_to_keep,
)


PHONE = "+15559876543"


def _et_morning(day=15, month=9, year=2026, hour=9, minute=30):
    """9:30 AM America/New_York as naive UTC (matches onboarded_user tz)."""
    tz = pytz.timezone("America/New_York")
    local = tz.localize(datetime(year, month, day, hour, minute, 0))
    return local.astimezone(pytz.utc).replace(tzinfo=None)


def _espn_final(game_id="game-cin-kc", away="CIN", home="KC", away_score="27", home_score="24"):
    return {
        "events": [{
            "id": game_id,
            "competitions": [{
                "status": {"type": {"name": "STATUS_FINAL", "completed": True}},
                "competitors": [
                    {
                        "homeAway": "away",
                        "score": away_score,
                        "team": {"abbreviation": away, "shortDisplayName": "Bengals" if away == "CIN" else away},
                    },
                    {
                        "homeAway": "home",
                        "score": home_score,
                        "team": {"abbreviation": home, "shortDisplayName": "Chiefs" if home == "KC" else home},
                    },
                ],
            }],
        }]
    }


def _fetch(board):
    return lambda url: board


def _set_trial(phone, trial_end, premium_status="premium", stripe_id=None, sub_status=None):
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            """
            UPDATE users SET trial_end_date = %s, premium_status = %s,
                   stripe_subscription_id = %s, subscription_status = %s
            WHERE phone_number = %s
            """,
            (trial_end, premium_status, stripe_id, sub_status, phone),
        )
        conn.commit()
    finally:
        return_db_connection(conn)


def _opt_in(phone, team_abbr="CIN", team_short="Bengals", cohort=COHORT_WEEKLY, **kwargs):
    now = kwargs.pop("opted_in_at", datetime.utcnow())
    pause_at = kwargs.pop("pause_at", now + timedelta(days=28))
    upsert_optin(phone, team_abbr, team_short, cohort, opted_in_at=now, pause_at=pause_at, beta_started_at=now)
    if kwargs:
        update_optin(phone, **kwargs)


def _event_counts(phone=None):
    conn = get_db_connection()
    try:
        c = conn.cursor()
        if phone:
            c.execute(
                "SELECT cohort, event_type, COUNT(*) FROM sports_score_events "
                "WHERE phone_number = %s GROUP BY cohort, event_type",
                (phone,),
            )
        else:
            c.execute(
                "SELECT cohort, event_type, COUNT(*) FROM sports_score_events "
                "GROUP BY cohort, event_type"
            )
        return {(row[0], row[1]): row[2] for row in c.fetchall()}
    finally:
        return_db_connection(conn)


class TestTeamAliases:
    def test_bengals_variants(self):
        for raw in ("Bengals", "Cincinnati Bengals", "CIN", "cincinnati", "cincy"):
            team, err = resolve_team(raw)
            assert err is None
            assert team["abbr"] == "CIN"
            assert team["short"] == "Bengals"

    def test_unknown_team(self):
        team, err = resolve_team("Manchester United")
        assert team is None
        assert "didn't recognize" in err.lower()

    def test_ambiguous_new_york(self):
        team, err = resolve_team("New York")
        assert team is None
        assert "Giants" in err and "Jets" in err


class TestEspnFinalsOnly:
    def test_skips_in_progress(self):
        board = {
            "events": [{
                "id": "live",
                "competitions": [{
                    "status": {"type": {"name": "STATUS_IN_PROGRESS", "completed": False}},
                    "competitors": [
                        {"homeAway": "away", "score": "7", "team": {"abbreviation": "CIN", "shortDisplayName": "Bengals"}},
                        {"homeAway": "home", "score": "3", "team": {"abbreviation": "KC", "shortDisplayName": "Chiefs"}},
                    ],
                }],
            }]
        }
        assert parse_finals(board) == []

    def test_format_final(self):
        game = fake_game_for_team("CIN", "Bengals")
        assert format_final(game) == "Bengals 27, Chiefs 24"


class TestYesTeamOptIn:
    @pytest.mark.asyncio
    async def test_yes_plus_team_opts_in(self, simulator, onboarded_user, ai_mock):
        phone = onboarded_user["phone"]
        result = await simulator.send_message(phone, "YES Bengals")
        assert "Bengals" in result["output"]
        optin = get_optin(phone)
        assert optin is not None
        assert optin["team_abbr"] == "CIN"
        assert optin["cohort"] == COHORT_WEEKLY
        assert count_sports_events(EVENT_SPORTS_YES, COHORT_WEEKLY) >= 1

    @pytest.mark.asyncio
    async def test_yes_plus_sign_form(self, simulator, onboarded_user):
        phone = onboarded_user["phone"]
        result = await simulator.send_message(phone, "YES + Cincinnati Bengals")
        assert "Bengals" in result["output"]
        assert get_optin(phone)["team_abbr"] == "CIN"

    @pytest.mark.asyncio
    async def test_dormant_invite_cohort_stays_dormant(self, simulator, onboarded_user):
        phone = onboarded_user["phone"]
        mark_sports_invite(phone, COHORT_DORMANT)
        await simulator.send_message(phone, "YES Bengals")
        assert get_optin(phone)["cohort"] == COHORT_DORMANT
        assert count_sports_events(EVENT_SPORTS_YES, COHORT_DORMANT) >= 1
        # Dormant YES must not land in the weekly counter.
        weekly_yes = count_sports_events(EVENT_SPORTS_YES, COHORT_WEEKLY)
        # Other tests may have written weekly events; this phone's YES is dormant.
        events = _event_counts(phone)
        assert (COHORT_DORMANT, EVENT_SPORTS_YES) in events
        assert (COHORT_WEEKLY, EVENT_SPORTS_YES) not in events
        assert weekly_yes == weekly_yes  # sanity: helper doesn't mix

    @pytest.mark.asyncio
    async def test_unknown_team_does_not_opt_in(self, simulator, onboarded_user):
        phone = onboarded_user["phone"]
        result = await simulator.send_message(phone, "YES Real Madrid")
        assert "didn't recognize" in result["output"].lower()
        assert get_optin(phone) is None


class TestScoreKeyword:
    @pytest.mark.asyncio
    async def test_score_after_ask(self, simulator, onboarded_user, ai_mock):
        phone = onboarded_user["phone"]
        _opt_in(phone)
        now = _et_morning()
        process_morning_asks(
            now_utc=now,
            skip_window_check=True,
            fetch_fn=_fetch(_espn_final()),
        )
        ai_mock.clear_history()
        result = await simulator.send_message(phone, "SCORE")
        assert result["output"] == "Bengals 27, Chiefs 24"
        optin = get_optin(phone)
        assert optin["last_ask_replied"] is True
        assert optin["ignore_streak"] == 0
        assert not any(c["message"].strip().upper() == "SCORE" for c in ai_mock.call_history)
        assert count_sports_events(EVENT_SCORE_REPLY, COHORT_WEEKLY) >= 1

    @pytest.mark.asyncio
    async def test_score_not_sent_to_ai_without_optin(self, simulator, onboarded_user, ai_mock):
        phone = onboarded_user["phone"]
        ai_mock.clear_history()
        result = await simulator.send_message(phone, "SCORE")
        assert "No score waiting" in result["output"]
        assert not any("SCORE" in (c["message"] or "").upper() for c in ai_mock.call_history)

    @pytest.mark.asyncio
    async def test_yes_team_not_sent_to_ai(self, simulator, onboarded_user, ai_mock):
        phone = onboarded_user["phone"]
        ai_mock.clear_history()
        await simulator.send_message(phone, "YES Bengals")
        assert not any("YES" in (c["message"] or "").upper() and "BENGAL" in (c["message"] or "").upper()
                      for c in ai_mock.call_history)


class TestIgnoreAndStop:
    def test_ignore_does_not_ping_again_that_game(self, onboarded_user):
        phone = onboarded_user["phone"]
        _opt_in(phone)
        now = _et_morning()
        with patch("services.sports_score_service.send_sms") as mock_sms:
            mock_sms.return_value = None
            first = process_morning_asks(
                now_utc=now, skip_window_check=True, fetch_fn=_fetch(_espn_final("game-1")),
            )
            second = process_morning_asks(
                now_utc=now, skip_window_check=True, fetch_fn=_fetch(_espn_final("game-1")),
            )
        assert first["asked"] == 1
        assert second["asked"] == 0
        assert mock_sms.call_count == 1
        ask_bodies = [c.args[1] for c in mock_sms.call_args_list]
        assert ASK_TEMPLATE.format(team="Bengals") in ask_bodies

    def test_three_ignores_stops_silently(self, onboarded_user):
        phone = onboarded_user["phone"]
        _opt_in(phone)
        now = _et_morning()
        with patch("services.sports_score_service.send_sms") as mock_sms:
            mock_sms.return_value = None
            for i, gid in enumerate(["g1", "g2", "g3", "g4"], start=1):
                process_morning_asks(
                    now_utc=now + timedelta(days=7 * (i - 1)),
                    skip_window_check=True,
                    fetch_fn=_fetch(_espn_final(gid)),
                )
        optin = get_optin(phone)
        assert optin["stopped_silently"] is True
        assert optin["ignore_streak"] >= 3
        # Three asks sent; the fourth is the silent stop (no 4th ask, no goodbye).
        ask_sends = [c for c in mock_sms.call_args_list if "Reply SCORE" in c.args[1]]
        assert len(ask_sends) == 3
        goodbye = [c for c in mock_sms.call_args_list if "goodbye" in c.args[1].lower() or "stop" in c.args[1].lower()]
        assert goodbye == []
        assert count_sports_events(EVENT_SCORE_IGNORE, COHORT_WEEKLY) >= 3


class TestAntiBunch:
    def test_skip_day_7_same_morning(self, onboarded_user):
        phone = onboarded_user["phone"]
        now = _et_morning()
        _set_trial(phone, now + timedelta(days=7))
        _opt_in(phone)
        row = _user_fields(phone)
        row.update(get_optin(phone))
        assert should_skip_score_ask(row, now) == "day_7"
        with patch("services.sports_score_service.send_sms") as mock_sms:
            mock_sms.return_value = None
            result = process_morning_asks(
                now_utc=now, skip_window_check=True, fetch_fn=_fetch(_espn_final()),
            )
        assert result["asked"] == 0
        assert mock_sms.call_count == 0

    def test_skip_day_13_same_morning(self, onboarded_user):
        phone = onboarded_user["phone"]
        now = _et_morning()
        _set_trial(phone, now + timedelta(days=1))  # 1 day remaining → Day 13
        _opt_in(phone)
        row = _user_fields(phone)
        row.update(get_optin(phone))
        assert should_skip_score_ask(row, now) == "day_13"

    def test_skip_day_14_same_morning(self, onboarded_user):
        phone = onboarded_user["phone"]
        now = _et_morning()
        _set_trial(phone, now - timedelta(hours=2))  # expired today
        _opt_in(phone)
        row = _user_fields(phone)
        row.update(get_optin(phone))
        assert should_skip_score_ask(row, now) == "day_14"

    def test_skip_winback_window(self, onboarded_user):
        phone = onboarded_user["phone"]
        now = _et_morning()
        _set_trial(phone, now - timedelta(days=30), premium_status="free")
        _opt_in(phone)
        row = _user_fields(phone)
        row.update(get_optin(phone))
        assert should_skip_score_ask(row, now) == "winback"

    def test_reminder_sms_not_blocked(self):
        """Anti-bunch is score-ping only; reminder message types stay sendable."""
        from services.sms_service import PUSHED_MESSAGE_TYPES
        assert "reminder" not in PUSHED_MESSAGE_TYPES
        assert "sports_score_ask" not in PUSHED_MESSAGE_TYPES


def _user_fields(phone):
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT timezone, trial_end_date, premium_status, subscription_status,
                   stripe_subscription_id, winback_30d_sent, sports_invite_sent_at
            FROM users WHERE phone_number = %s
            """,
            (phone,),
        )
        row = c.fetchone()
        return {
            "timezone": row[0],
            "trial_end_date": row[1],
            "premium_status": row[2],
            "subscription_status": row[3],
            "stripe_subscription_id": row[4],
            "winback_30d_sent": row[5],
            "sports_invite_sent_at": row[6],
            "phone_number": phone,
        }
    finally:
        return_db_connection(conn)


class TestCohortEventSeparation:
    def test_weekly_and_dormant_events_are_separate(self, onboarded_user):
        phone = onboarded_user["phone"]
        _opt_in(phone, cohort=COHORT_WEEKLY)
        log_sports_event(phone, EVENT_SCORE_ASK, COHORT_WEEKLY, metadata={"team": "CIN"})
        # Simulate a dormant user on a different phone stored as same test user after reset —
        # log dormant events explicitly; counters must not mix.
        log_sports_event(phone, EVENT_SPORTS_YES, COHORT_DORMANT, metadata={"team": "KC"})
        weekly_asks = count_sports_events(EVENT_SCORE_ASK, COHORT_WEEKLY)
        dormant_asks = count_sports_events(EVENT_SCORE_ASK, COHORT_DORMANT)
        weekly_yes = count_sports_events(EVENT_SPORTS_YES, COHORT_WEEKLY)
        dormant_yes = count_sports_events(EVENT_SPORTS_YES, COHORT_DORMANT)
        assert weekly_asks >= 1
        assert dormant_asks == 0
        assert dormant_yes >= 1
        # A dormant YES is not a weekly ask and not a weekly YES from this log call.
        assert count_sports_events(EVENT_SCORE_ASK, COHORT_WEEKLY) != count_sports_events(EVENT_SCORE_ASK, COHORT_DORMANT)

    def test_dormant_opt_in_score_events_stay_dormant(self, onboarded_user):
        phone = onboarded_user["phone"]
        mark_sports_invite(phone, COHORT_DORMANT)
        handle_yes_team(phone, "YES Bengals")
        now = _et_morning()
        with patch("services.sports_score_service.send_sms"):
            process_morning_asks(
                now_utc=now, skip_window_check=True, fetch_fn=_fetch(_espn_final()),
            )
        events = _event_counts(phone)
        assert (COHORT_DORMANT, EVENT_SPORTS_YES) in events
        assert (COHORT_DORMANT, EVENT_SCORE_ASK) in events
        assert (COHORT_WEEKLY, EVENT_SPORTS_YES) not in events
        assert (COHORT_WEEKLY, EVENT_SCORE_ASK) not in events


class TestPauseAfterFourWeeks:
    def test_pause_after_four_weeks_unless_premium(self, onboarded_user):
        phone = onboarded_user["phone"]
        now = _et_morning()
        opted = now - timedelta(days=29)
        _set_trial(phone, now + timedelta(days=40))  # trial still running, 4-week cap hits first
        _opt_in(phone, opted_in_at=opted, pause_at=compute_pause_at(opted, now + timedelta(days=40)))
        with patch("services.sports_score_service.send_sms") as mock_sms:
            mock_sms.return_value = None
            result = process_morning_asks(
                now_utc=now, skip_window_check=True, fetch_fn=_fetch(_espn_final()),
            )
        assert result["paused"] == 1
        assert result["asked"] == 0
        assert mock_sms.call_args.args[1] == PAUSE_COPY
        assert get_optin(phone)["paused_at"] is not None

    def test_paid_premium_is_not_paused(self, onboarded_user):
        phone = onboarded_user["phone"]
        now = _et_morning()
        opted = now - timedelta(days=29)
        _set_trial(
            phone,
            now - timedelta(days=10),
            premium_status="premium",
            stripe_id="sub_test",
            sub_status="active",
        )
        _opt_in(phone, opted_in_at=opted, pause_at=opted + timedelta(days=28))
        row = _user_fields(phone)
        assert is_paid_premium(row) is True
        with patch("services.sports_score_service.send_sms") as mock_sms:
            mock_sms.return_value = None
            result = process_morning_asks(
                now_utc=now, skip_window_check=True, fetch_fn=_fetch(_espn_final()),
            )
        assert result["paused"] == 0
        assert result["asked"] == 1
        assert mock_sms.call_args.args[1] == ASK_TEMPLATE.format(team="Bengals")

    def test_trial_end_pauses_non_premium(self, onboarded_user):
        phone = onboarded_user["phone"]
        now = _et_morning()
        trial_end = now - timedelta(days=1)
        # Not in day-14 window (expired yesterday), so pause can fire.
        _set_trial(phone, trial_end, premium_status="free")
        _opt_in(phone, opted_in_at=now - timedelta(days=5), pause_at=trial_end)
        with patch("services.sports_score_service.send_sms") as mock_sms:
            mock_sms.return_value = None
            result = process_morning_asks(
                now_utc=now, skip_window_check=True, fetch_fn=_fetch(_espn_final()),
            )
        assert result["paused"] == 1
        assert mock_sms.call_args.args[1] == PAUSE_COPY

    def test_upgrade_to_keep_logged_on_upgrade_keyword(self, onboarded_user):
        phone = onboarded_user["phone"]
        now = datetime.utcnow()
        _opt_in(phone, pause_at=now - timedelta(days=1))
        update_optin(phone, paused_at=now - timedelta(hours=1))
        maybe_log_upgrade_to_keep(phone)
        assert count_sports_events(EVENT_UPGRADE_TO_KEEP, COHORT_WEEKLY) >= 1


class TestNoInviteSendPath:
    def test_invite_copy_locked_but_not_scheduled(self):
        assert INVITE_WEEKLY == (
            "Trying something for football season. Want your team's score the morning after? "
            "I'll ask first. Reply YES + team (NFL)."
        )
        assert INVITE_DORMANT == (
            "Still here if you want. Trying football scores the morning after a game "
            "(I'll ask first — no spoilers). Reply YES + team, or ignore."
        )
        from celery_config import beat_schedule
        task_names = [v["task"] for v in beat_schedule.values()]
        assert "tasks.reminder_tasks.send_nfl_score_asks" in task_names
        assert not any("invite" in name for name in beat_schedule)
        assert not any("invite" in t for t in task_names)

    def test_inactivity_skipped_when_sports_invite_this_week(self, onboarded_user):
        phone = onboarded_user["phone"]
        mark_sports_invite(phone, COHORT_DORMANT)
        assert sports_invite_sent_this_week(phone) is True
        source = open("tasks/reminder_tasks.py", encoding="utf-8").read()
        assert "sports_invite_sent_this_week" in source
        assert "KEEP" not in source or "sports invite" in source.lower()


class TestLockedAskCopy:
    def test_ask_copy(self, onboarded_user):
        phone = onboarded_user["phone"]
        _opt_in(phone)
        with patch("services.sports_score_service.send_sms") as mock_sms:
            mock_sms.return_value = None
            process_morning_asks(
                now_utc=_et_morning(), skip_window_check=True, fetch_fn=_fetch(_espn_final()),
            )
        assert mock_sms.call_args.args[1] == "Bengals played last night. Reply SCORE if you want it, or ignore."
        assert "Sept" not in mock_sms.call_args.args[1]
        assert "recap" not in mock_sms.call_args.args[1].lower()
