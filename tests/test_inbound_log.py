"""
Tests for inbound SMS webhook logging (sms_inbound_log).

Every validated /sms hit must leave a row before any processing, so
top-of-funnel texts are measurable even when handling fails later.
"""

import pytest
from unittest.mock import patch

from database import get_db_connection, return_db_connection
from services.sms_service import log_inbound_sms


def _fetch_rows(phone):
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute(
            'SELECT body_preview, is_new_user FROM sms_inbound_log '
            'WHERE phone_number = %s ORDER BY id',
            (phone,)
        )
        return c.fetchall()
    finally:
        return_db_connection(conn)


def _clear_rows(phone):
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('DELETE FROM sms_inbound_log WHERE phone_number = %s', (phone,))
        conn.commit()
    finally:
        return_db_connection(conn)


class TestInboundLogging:

    @pytest.mark.asyncio
    async def test_unknown_number_logged_as_new_user(self, simulator, clean_test_user):
        """A first-ever text leaves a row flagged is_new_user before onboarding runs."""
        phone = clean_test_user
        _clear_rows(phone)

        await simulator.send_message(phone, "Hi")

        rows = _fetch_rows(phone)
        assert len(rows) == 1
        assert rows[0][0] == "Hi"
        assert rows[0][1] is True
        _clear_rows(phone)

    @pytest.mark.asyncio
    async def test_existing_user_logged_as_known(self, simulator, onboarded_user):
        """Texts from an onboarded user are logged with is_new_user False."""
        phone = onboarded_user["phone"]
        _clear_rows(phone)

        await simulator.send_message(phone, "Show Memories")

        rows = _fetch_rows(phone)
        assert len(rows) == 1
        assert rows[0][1] is False
        _clear_rows(phone)

    @pytest.mark.asyncio
    async def test_every_message_gets_a_row(self, simulator, clean_test_user):
        """Multiple messages in one conversation each leave their own row."""
        phone = clean_test_user
        _clear_rows(phone)

        await simulator.send_message(phone, "Hi")
        await simulator.send_message(phone, "John")
        await simulator.send_message(phone, "90210")

        rows = _fetch_rows(phone)
        assert len(rows) == 3
        assert [r[0] for r in rows] == ["Hi", "John", "90210"]
        # First message arrived before the user row existed; later ones after
        assert rows[0][1] is True
        _clear_rows(phone)

    def test_never_raises_on_db_failure(self):
        """Logging must never break the webhook, even with the DB down."""
        with patch('database.get_db_connection', side_effect=RuntimeError('db down')):
            log_inbound_sms("+15550001111", "Hello", "SMtest123")

    def test_long_body_truncated(self, clean_test_user):
        """Bodies are stored as a preview, capped at 160 chars."""
        phone = clean_test_user
        _clear_rows(phone)

        log_inbound_sms(phone, "x" * 500, None)

        rows = _fetch_rows(phone)
        assert len(rows) == 1
        assert len(rows[0][0]) == 160
        _clear_rows(phone)
