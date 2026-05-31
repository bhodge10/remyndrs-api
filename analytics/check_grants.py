"""Read-only check: how long were beta premium grants, and when do they expire?"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()
url = os.environ.get("ANALYTICS_DATABASE_URL") or os.environ.get("DATABASE_URL")
c = psycopg2.connect(url)
c.set_session(readonly=True, autocommit=True)
cur = c.cursor()


def show(label, sql):
    cur.execute(sql)
    print(label)
    for r in cur.fetchall():
        print("   ", r)
    print()


show("Granted length distribution (trial_end - premium_since, in days):", """
  SELECT (trial_end_date::date - premium_since::date) AS days_granted, COUNT(*)
  FROM users
  WHERE premium_status='premium' AND premium_since IS NOT NULL AND trial_end_date IS NOT NULL
  GROUP BY 1 ORDER BY 1
""")

show("Per-user grant detail (premium_since, trial_end, days):", """
  SELECT premium_since::date, trial_end_date::date,
         (trial_end_date::date - premium_since::date) AS days
  FROM users
  WHERE premium_status='premium' AND premium_since IS NOT NULL AND trial_end_date IS NOT NULL
  ORDER BY trial_end_date
""")

show("Expiry timing vs today (2026-05-30):", """
  SELECT
    COUNT(*) FILTER (WHERE trial_end_date < NOW())                                            AS already_expired,
    COUNT(*) FILTER (WHERE trial_end_date >= NOW() AND trial_end_date < NOW() + INTERVAL '14 days')   AS next_14d,
    COUNT(*) FILTER (WHERE trial_end_date >= NOW() + INTERVAL '14 days' AND trial_end_date < NOW() + INTERVAL '30 days') AS d15_30,
    COUNT(*) FILTER (WHERE trial_end_date >= NOW() + INTERVAL '30 days')                       AS later
  FROM users WHERE premium_status='premium' AND trial_end_date IS NOT NULL
""")

show("Premium comps with NO trial_end_date:", """
  SELECT COUNT(*) FROM users WHERE premium_status='premium' AND trial_end_date IS NULL
""")

show("subscription_status of premium comps:", """
  SELECT COALESCE(subscription_status,'(null)'), COUNT(*)
  FROM users WHERE premium_status='premium' GROUP BY 1
""")

c.close()
