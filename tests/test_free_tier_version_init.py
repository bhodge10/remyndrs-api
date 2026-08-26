"""
Guard: init_db must not re-apply the one-time free_tier_version v1 backfill.

New users inherit column default 2 (3 reminders/week). A repeating
UPDATE ... SET free_tier_version = 1 WHERE free_tier_version = 2
converts them to grandfathered v1 (2 reminders/day) on every process boot.
"""

import ast
import re
from pathlib import Path

import pytest

_DATABASE_PY = Path(__file__).resolve().parents[1] / "database.py"

_RESET_V2_TO_V1 = re.compile(
    r"UPDATE\s+users\s+SET\s+free_tier_version\s*=\s*1\b",
    re.IGNORECASE,
)

_PROBE_PHONE = "+15550000002"


def _sql_literals_in_init_db():
    tree = ast.parse(_DATABASE_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "init_db":
            return [
                n.value
                for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
    raise AssertionError("init_db not found in database.py")


class TestInitDbDoesNotResetFreeTierV2:
    def test_column_default_2_stays_in_migrations(self):
        sql = _sql_literals_in_init_db()
        assert any(
            "ADD COLUMN IF NOT EXISTS free_tier_version INTEGER DEFAULT 2" in stmt
            for stmt in sql
        ), "Expected free_tier_version column (default 2) to remain in init_db migrations"

    def test_migrations_do_not_set_version_2_users_back_to_1(self):
        for stmt in _sql_literals_in_init_db():
            assert _RESET_V2_TO_V1.search(stmt) is None, (
                "init_db must not re-run the one-time v1 backfill on boot: "
                f"{' '.join(stmt.split())}"
            )


class TestInitDbPreservesExistingV2User:
    def test_user_with_free_tier_version_2_stays_2_after_init_db(self):
        from database import get_db_connection, init_db, return_db_connection
        from models.user import create_or_update_user

        try:
            conn = get_db_connection()
            return_db_connection(conn)
        except Exception as e:
            pytest.skip(f"test DB unavailable: {e}")

        def _version():
            conn = get_db_connection()
            try:
                c = conn.cursor()
                c.execute(
                    "SELECT free_tier_version FROM users WHERE phone_number = %s",
                    (_PROBE_PHONE,),
                )
                row = c.fetchone()
                return row[0] if row else None
            finally:
                return_db_connection(conn)

        def _cleanup():
            conn = get_db_connection()
            try:
                c = conn.cursor()
                c.execute("DELETE FROM users WHERE phone_number = %s", (_PROBE_PHONE,))
                conn.commit()
            finally:
                return_db_connection(conn)

        _cleanup()
        try:
            create_or_update_user(
                _PROBE_PHONE, free_tier_version=2, first_name="V2Probe"
            )
            assert _version() == 2
            init_db()
            assert _version() == 2, (
                f"init_db changed free_tier_version from 2 to {_version()}"
            )
        finally:
            _cleanup()
