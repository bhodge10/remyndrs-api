"""
Read-only product diagnosis script.

Connects to a PostgreSQL database in READ ONLY mode and prints a funnel /
retention / activation report. It NEVER writes. Point it at a read-only
credential via ANALYTICS_DATABASE_URL (falls back to DATABASE_URL).

Usage:
    python analytics/diagnose.py

Auto-detects PII encryption: when users.phone_hash is populated, child tables
are joined on phone_hash (a deterministic HMAC); otherwise on phone_number.
"""

import os
import sys
from contextlib import contextmanager

import psycopg2
from dotenv import load_dotenv

load_dotenv()

DB_URL = os.environ.get("ANALYTICS_DATABASE_URL") or os.environ.get("DATABASE_URL")


@contextmanager
def ro_cursor():
    """Read-only cursor. Forces the whole session to reject writes."""
    if not DB_URL:
        sys.exit("No ANALYTICS_DATABASE_URL or DATABASE_URL set in environment.")
    conn = psycopg2.connect(DB_URL)
    try:
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor()
        cur.execute("SET default_transaction_read_only = on")
        yield cur
    finally:
        conn.close()


def q(cur, sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchall()


def header(title):
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def detect_key(cur):
    """Return the column to group/join users on (phone_hash if encrypted)."""
    rows = q(cur, "SELECT COUNT(*) FROM users WHERE phone_hash IS NOT NULL")
    if rows and rows[0][0] and rows[0][0] > 0:
        return "phone_hash"
    return "phone_number"


def has_column(cur, table, col):
    rows = q(
        cur,
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, col),
    )
    return bool(rows)


def join_key(cur, table, preferred):
    """Use the preferred key only if this table actually has that column."""
    if preferred == "phone_hash" and not has_column(cur, table, "phone_hash"):
        return "phone_number"
    return preferred


def section(title, fn):
    """Run a report section; isolate failures so one bad query won't abort."""
    header(title)
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - diagnostic tool, keep going
        print(f"  [skipped: {e}]")


def main():
    with ro_cursor() as cur:
        key = detect_key(cur)
        print(f"DB connected (read-only). User join key: {key}")

        # ---- 1. OVERVIEW -------------------------------------------------
        def overview():
            (total,) = q(cur, "SELECT COUNT(*) FROM users")[0]
            (onboarded,) = q(
                cur, "SELECT COUNT(*) FROM users WHERE onboarding_complete"
            )[0]
            (premium,) = q(
                cur,
                "SELECT COUNT(*) FROM users WHERE premium_status NOT IN ('free','') "
                "AND premium_status IS NOT NULL",
            )[0]
            print(f"  Total users:          {total}")
            print(f"  Onboarding complete:  {onboarded}")
            print(f"  Premium / paid:       {premium}")
            if has_column(cur, "users", "lifecycle_messages_opted_out"):
                (optout,) = q(
                    cur,
                    "SELECT COUNT(*) FROM users WHERE lifecycle_messages_opted_out",
                )[0]
                print(f"  Lifecycle opted-out:  {optout}")
            else:
                print("  Lifecycle opted-out:  (column not deployed to prod yet)")

        section("1. OVERVIEW", overview)

        # ---- 2. ACTIVITY RECENCY ----------------------------------------
        def recency():
            sql = """
                SELECT
                  COUNT(*) FILTER (WHERE last_active_at >= NOW() - INTERVAL '1 day')  AS d1,
                  COUNT(*) FILTER (WHERE last_active_at >= NOW() - INTERVAL '7 days')  AS d7,
                  COUNT(*) FILTER (WHERE last_active_at >= NOW() - INTERVAL '14 days') AS d14,
                  COUNT(*) FILTER (WHERE last_active_at >= NOW() - INTERVAL '30 days') AS d30,
                  COUNT(*) FILTER (WHERE last_active_at <  NOW() - INTERVAL '30 days'
                                      OR last_active_at IS NULL)                       AS cold
                FROM users
            """
            d1, d7, d14, d30, cold = q(cur, sql)[0]
            print(f"  Active last 24h:   {d1}")
            print(f"  Active last 7d:    {d7}")
            print(f"  Active last 14d:   {d14}")
            print(f"  Active last 30d:   {d30}")
            print(f"  Cold (>30d/never): {cold}")

        section("2. ACTIVITY RECENCY (by last_active_at)", recency)

        # ---- 3. SIGNUP COHORTS BY WEEK ----------------------------------
        def cohorts():
            sql = """
                SELECT
                  date_trunc('week', created_at)::date AS wk,
                  COUNT(*)                                              AS signups,
                  COUNT(*) FILTER (WHERE onboarding_complete)           AS onboarded,
                  COUNT(*) FILTER (WHERE last_active_at >= created_at + INTERVAL '7 days')  AS retained_7d,
                  COUNT(*) FILTER (WHERE last_active_at >= NOW() - INTERVAL '30 days')      AS still_active,
                  COUNT(*) FILTER (WHERE premium_status NOT IN ('free','')
                                     AND premium_status IS NOT NULL)    AS paid
                FROM users
                GROUP BY 1 ORDER BY 1 DESC
                LIMIT 20
            """
            print(f"  {'week':<12}{'signup':>7}{'onbd':>6}{'ret7d':>7}{'active':>8}{'paid':>6}")
            for wk, s, o, r7, act, paid in q(cur, sql):
                print(f"  {str(wk):<12}{s:>7}{o:>6}{r7:>7}{act:>8}{paid:>6}")

        section("3. SIGNUP COHORTS BY WEEK (most recent first)", cohorts)

        # ---- 4. ACTIVATION: time to first reminder ----------------------
        def activation():
            sql = f"""
                WITH firsts AS (
                    SELECT u.{key} AS k, u.created_at AS signup,
                           MIN(r.created_at) AS first_reminder
                    FROM users u
                    LEFT JOIN reminders r ON r.{key} = u.{key}
                    GROUP BY u.{key}, u.created_at
                )
                SELECT
                  COUNT(*)                                              AS users,
                  COUNT(first_reminder)                                 AS ever_made_reminder,
                  ROUND(AVG(EXTRACT(EPOCH FROM (first_reminder - signup))/3600)::numeric, 1) AS avg_hrs,
                  ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (
                      ORDER BY EXTRACT(EPOCH FROM (first_reminder - signup))/3600))::numeric, 1) AS median_hrs
                FROM firsts
            """
            users, made, avg_h, med_h = q(cur, sql)[0]
            pct = (made / users * 100) if users else 0
            print(f"  Users:                         {users}")
            print(f"  Ever created a reminder:       {made} ({pct:.0f}%)")
            print(f"  Avg time signup->1st reminder: {avg_h} hrs")
            print(f"  Median time:                   {med_h} hrs")

        section("4. ACTIVATION (signup -> first reminder)", activation)

        # ---- 5. CHURN LIFESPAN ------------------------------------------
        def churn():
            sql = """
                SELECT
                  ROUND(AVG(EXTRACT(EPOCH FROM (last_active_at - created_at))/86400)::numeric, 1) AS avg_days,
                  ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (
                      ORDER BY EXTRACT(EPOCH FROM (last_active_at - created_at))/86400))::numeric, 1) AS median_days,
                  COUNT(*) FILTER (WHERE last_active_at < created_at + INTERVAL '1 day') AS one_and_done
                FROM users
                WHERE last_active_at IS NOT NULL
                  AND last_active_at < NOW() - INTERVAL '30 days'
            """
            avg_d, med_d, oad = q(cur, sql)[0]
            print("  (churned = no activity in 30d)")
            print(f"  Avg lifespan (signup->last active):    {avg_d} days")
            print(f"  Median lifespan:                       {med_d} days")
            print(f"  'One and done' (gone within 24h):      {oad}")

        section("5. CHURN LIFESPAN", churn)

        # ---- 6. THE SURVIVORS: active users deep-dive -------------------
        def survivors():
            # recurring_reminders has no phone_hash column; join it on phone_number.
            rk = join_key(cur, "recurring_reminders", key)
            sql = f"""
                SELECT
                  u.created_at::date                                   AS joined,
                  u.last_active_at::date                               AS last_seen,
                  u.premium_status,
                  COALESCE(u.total_messages, 0)                        AS msgs,
                  (SELECT COUNT(*) FROM reminders r       WHERE r.{key}=u.{key}) AS reminders,
                  (SELECT COUNT(*) FROM recurring_reminders rr WHERE rr.{rk}=u.{rk}) AS recurring,
                  (SELECT COUNT(*) FROM memories m        WHERE m.{key}=u.{key}) AS memories,
                  (SELECT COUNT(*) FROM lists l           WHERE l.{key}=u.{key}) AS lists,
                  (SELECT COUNT(*) FROM list_items li     WHERE li.{key}=u.{key}) AS items
                FROM users u
                WHERE u.last_active_at >= NOW() - INTERVAL '14 days'
                ORDER BY u.last_active_at DESC
            """
            rows = q(cur, sql)
            print(f"  {'joined':<12}{'lastseen':<12}{'tier':<9}{'msg':>5}{'rem':>5}{'rec':>5}{'mem':>5}{'lst':>5}{'itm':>5}")
            for joined, last, tier, msgs, rem, rec, mem, lst, itm in rows:
                print(f"  {str(joined):<12}{str(last):<12}{str(tier)[:8]:<9}{msgs:>5}{rem:>5}{rec:>5}{mem:>5}{lst:>5}{itm:>5}")
            print(f"\n  ({len(rows)} users active in last 14 days)")

        section("6. THE SURVIVORS (users active in last 14d)", survivors)

        # ---- 7. REMINDER DELIVERY HEALTH --------------------------------
        def delivery():
            sql = """
                SELECT COALESCE(delivery_status, 'null'), COUNT(*)
                FROM reminders GROUP BY 1 ORDER BY 2 DESC
            """
            print("  delivery_status breakdown:")
            for status, n in q(cur, sql):
                print(f"    {status:<14}{n}")

        section("7. REMINDER DELIVERY HEALTH", delivery)

        # ---- 8. WHAT PEOPLE ACTUALLY DO (intents, last 90d) -------------
        def intents():
            sql = """
                SELECT COALESCE(intent, 'unknown') AS intent, COUNT(*) AS n
                FROM logs
                WHERE created_at >= NOW() - INTERVAL '90 days'
                GROUP BY 1 ORDER BY 2 DESC LIMIT 20
            """
            print("  Top intents (inbound messages, last 90d):")
            for intent, n in q(cur, sql):
                print(f"    {str(intent)[:30]:<32}{n}")

        section("8. WHAT PEOPLE ACTUALLY DO (log intents)", intents)

        # ---- 9. LIFECYCLE / NUDGE ENGAGEMENT ----------------------------
        def nudges():
            sql = """
                SELECT
                  COUNT(*)                              AS sent,
                  COUNT(user_response)                  AS responded,
                  COUNT(*) FILTER (WHERE action_taken IS NOT NULL
                                     AND action_taken <> '') AS acted
                FROM smart_nudges
                WHERE sent_at >= NOW() - INTERVAL '90 days'
            """
            sent, resp, acted = q(cur, sql)[0]
            print(f"  Nudges sent (90d):  {sent}")
            print(f"  Responded:          {resp}")
            print(f"  Drove an action:    {acted}")

        section("9. SMART NUDGE ENGAGEMENT", nudges)

        print("\nDone. (No data was modified.)\n")


if __name__ == "__main__":
    main()
