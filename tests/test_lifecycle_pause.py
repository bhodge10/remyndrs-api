"""
Tests for the lifecycle-message soft opt-out.

Distinct from the hard STOP opt-out: a paused user still gets the reminders
they explicitly scheduled, but proactive trial/winback/inactivity sends are
silenced. The flag is `users.lifecycle_messages_opted_out`.
"""

from unittest.mock import patch, MagicMock

import pytest


# =====================================================
# HELPER FUNCTION TESTS (mocked DB)
# =====================================================

def _conn_with_fetchone(returns):
    cur = MagicMock()
    cur.fetchone.return_value = returns
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


class TestLifecyclePauseHelpers:
    def test_pause_lifecycle_messages_sets_flag_and_timestamp(self):
        from models import user as user_model
        conn, cur = _conn_with_fetchone(None)
        with patch.object(user_model, 'get_db_connection', return_value=conn), \
             patch.object(user_model, 'return_db_connection'):
            assert user_model.pause_lifecycle_messages('+15551234567') is True
            sql = cur.execute.call_args[0][0]
            assert 'lifecycle_messages_opted_out = TRUE' in sql
            assert 'lifecycle_messages_opted_out_at = CURRENT_TIMESTAMP' in sql

    def test_resume_lifecycle_messages_clears_flag(self):
        from models import user as user_model
        conn, cur = _conn_with_fetchone(None)
        with patch.object(user_model, 'get_db_connection', return_value=conn), \
             patch.object(user_model, 'return_db_connection'):
            assert user_model.resume_lifecycle_messages('+15551234567') is True
            sql = cur.execute.call_args[0][0]
            assert 'lifecycle_messages_opted_out = FALSE' in sql
            assert 'lifecycle_messages_opted_out_at = NULL' in sql

    def test_is_lifecycle_paused_true_when_flag_set(self):
        from models import user as user_model
        conn, _ = _conn_with_fetchone((True,))
        with patch.object(user_model, 'get_db_connection', return_value=conn), \
             patch.object(user_model, 'return_db_connection'), \
             patch.object(user_model, 'ENCRYPTION_ENABLED', False):
            assert user_model.is_lifecycle_paused('+15551234567') is True

    def test_is_lifecycle_paused_false_when_flag_unset(self):
        from models import user as user_model
        conn, _ = _conn_with_fetchone((False,))
        with patch.object(user_model, 'get_db_connection', return_value=conn), \
             patch.object(user_model, 'return_db_connection'), \
             patch.object(user_model, 'ENCRYPTION_ENABLED', False):
            assert user_model.is_lifecycle_paused('+15551234567') is False

    def test_is_lifecycle_paused_false_for_unknown_user(self):
        from models import user as user_model
        conn, _ = _conn_with_fetchone(None)
        with patch.object(user_model, 'get_db_connection', return_value=conn), \
             patch.object(user_model, 'return_db_connection'), \
             patch.object(user_model, 'ENCRYPTION_ENABLED', False):
            assert user_model.is_lifecycle_paused('+15551234567') is False


# =====================================================
# LIFECYCLE QUERY GATING — make sure each scheduled task
# query has a clause excluding paused users.
# =====================================================

class TestLifecycleQueriesAreGated:
    """Every lifecycle query must filter out lifecycle_messages_opted_out users.
    This guards against forgetting to add the gate when new lifecycle tasks
    land in tasks/reminder_tasks.py.
    """

    def test_every_opted_out_clause_has_lifecycle_clause(self):
        with open('tasks/reminder_tasks.py', 'r', encoding='utf-8') as f:
            content = f.read()

        opted_out_count = content.count('AND (opted_out IS NULL OR opted_out = FALSE)')
        lifecycle_count = content.count(
            'AND (lifecycle_messages_opted_out IS NULL OR lifecycle_messages_opted_out = FALSE)'
        )
        # Every opt-out gate should be paired with a lifecycle gate.
        assert opted_out_count > 0, "Expected at least one opted_out clause"
        assert lifecycle_count == opted_out_count, (
            f"Found {opted_out_count} `opted_out` clauses but only "
            f"{lifecycle_count} `lifecycle_messages_opted_out` clauses — "
            "every lifecycle query must be gated on both."
        )


# =====================================================
# KEYWORD HANDLER TESTS (via ConversationSimulator)
# =====================================================

class TestPauseKeywordHandlers:
    @pytest.mark.asyncio
    async def test_pause_keyword_pauses_lifecycle(
        self, simulator, sms_capture, ai_mock, onboarded_user
    ):
        phone = onboarded_user['phone']
        result = await simulator.send_message(phone, "PAUSE")
        assert 'reminders you' in result['output'].lower() \
            or 'check-in' in result['output'].lower()

        from models.user import is_lifecycle_paused
        assert is_lifecycle_paused(phone) is True

    @pytest.mark.asyncio
    async def test_quiet_keyword_pauses_lifecycle(
        self, simulator, sms_capture, ai_mock, onboarded_user
    ):
        phone = onboarded_user['phone']
        await simulator.send_message(phone, "QUIET")

        from models.user import is_lifecycle_paused
        assert is_lifecycle_paused(phone) is True

    @pytest.mark.asyncio
    async def test_mute_keyword_pauses_lifecycle(
        self, simulator, sms_capture, ai_mock, onboarded_user
    ):
        phone = onboarded_user['phone']
        await simulator.send_message(phone, "MUTE")

        from models.user import is_lifecycle_paused
        assert is_lifecycle_paused(phone) is True

    @pytest.mark.asyncio
    async def test_resume_keyword_unpauses_lifecycle(
        self, simulator, sms_capture, ai_mock, onboarded_user
    ):
        phone = onboarded_user['phone']
        await simulator.send_message(phone, "PAUSE")
        result = await simulator.send_message(phone, "RESUME")
        assert 'welcome back' in result['output'].lower() \
            or 'lifecycle' in result['output'].lower()

        from models.user import is_lifecycle_paused
        assert is_lifecycle_paused(phone) is False

    @pytest.mark.asyncio
    async def test_pause_does_not_set_hard_opt_out(
        self, simulator, sms_capture, ai_mock, onboarded_user
    ):
        """Soft pause must not flip the carrier-level opted_out flag — that's
        what STOP is for, and the user explicitly wants their reminders."""
        phone = onboarded_user['phone']
        await simulator.send_message(phone, "PAUSE")

        from models.user import is_user_opted_out, is_lifecycle_paused
        assert is_lifecycle_paused(phone) is True
        assert is_user_opted_out(phone) is False


# =====================================================
# AI NATURAL-LANGUAGE PATH (via update_settings action)
# =====================================================

class TestLifecycleViaAI:
    @pytest.mark.asyncio
    async def test_natural_language_pause_sets_flag(
        self, simulator, sms_capture, ai_mock, onboarded_user
    ):
        phone = onboarded_user['phone']
        msg = "Don't text me unless it's about a reminder I asked about"
        ai_mock.set_response(msg, {
            'action': 'update_settings',
            'setting': 'lifecycle_messages',
            'value': 'paused',
            'confirmation': "Pausing lifecycle messages",
        })
        await simulator.send_message(phone, msg)

        from models.user import is_lifecycle_paused
        assert is_lifecycle_paused(phone) is True

    @pytest.mark.asyncio
    async def test_natural_language_resume_clears_flag(
        self, simulator, sms_capture, ai_mock, onboarded_user
    ):
        phone = onboarded_user['phone']
        from models.user import pause_lifecycle_messages
        pause_lifecycle_messages(phone)

        msg = "you can text me again"
        ai_mock.set_response(msg, {
            'action': 'update_settings',
            'setting': 'lifecycle_messages',
            'value': 'resumed',
            'confirmation': "Resuming lifecycle messages",
        })
        await simulator.send_message(phone, msg)

        from models.user import is_lifecycle_paused
        assert is_lifecycle_paused(phone) is False
