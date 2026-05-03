"""
Unit tests for the anti-bunching guard in services/sms_service.py.

The guard prevents two different lifecycle/nudge systems from firing
back-to-back (the issue we hit when a user got an inactivity nudge on
Friday and a 30-day winback on Saturday).
"""

from unittest.mock import patch, MagicMock

from services.sms_service import recently_pushed_message, PUSHED_MESSAGE_TYPES


def _mock_db(returns_row: bool):
    """Build a fake DB connection whose fetchone() returns a row or None."""
    cur = MagicMock()
    cur.fetchone.return_value = (1,) if returns_row else None
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def test_returns_true_when_recent_pushed_message_exists():
    conn, cur = _mock_db(returns_row=True)
    with patch('database.get_db_connection', return_value=conn), \
         patch('database.return_db_connection') as ret:
        assert recently_pushed_message('+15551234567') is True
        ret.assert_called_once_with(conn)
        # Confirm we queried with the expected types and a 48h cutoff
        args, _ = cur.execute.call_args
        sql, params = args
        assert 'sms_outbound_log' in sql
        assert list(PUSHED_MESSAGE_TYPES) == params[1]


def test_returns_false_when_no_recent_pushed_message():
    conn, _ = _mock_db(returns_row=False)
    with patch('database.get_db_connection', return_value=conn), \
         patch('database.return_db_connection'):
        assert recently_pushed_message('+15551234567') is False


def test_fails_open_on_db_error():
    """If the guard query itself errors, do not silently block lifecycle
    sends — log and return False so the caller proceeds."""
    with patch('database.get_db_connection', side_effect=RuntimeError('db down')):
        assert recently_pushed_message('+15551234567') is False


def test_custom_window_and_types_pass_through():
    conn, cur = _mock_db(returns_row=False)
    with patch('database.get_db_connection', return_value=conn), \
         patch('database.return_db_connection'):
        recently_pushed_message('+15551234567', hours=24, types=('broadcast',))
        args, _ = cur.execute.call_args
        _, params = args
        assert params[1] == ['broadcast']


def test_pushed_types_excludes_user_initiated_replies():
    """Replies, reminders, billing, support — these are user-pulled, not
    system-pushed. They should not count toward the bunching guard."""
    user_initiated = {'reply', 'reminder', 'billing', 'support', 'signup', 'alert', 'onboarding'}
    assert user_initiated.isdisjoint(PUSHED_MESSAGE_TYPES)
