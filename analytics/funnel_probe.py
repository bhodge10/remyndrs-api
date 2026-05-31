"""Read-only: compute the funnel stage counts to validate the SQL before
wiring it into the admin endpoint."""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()
url = os.environ.get("ANALYTICS_DATABASE_URL") or os.environ.get("DATABASE_URL")
c = psycopg2.connect(url)
c.set_session(readonly=True, autocommit=True)
cur = c.cursor()


def scalar(sql):
    cur.execute(sql)
    return cur.fetchone()[0]


# Top of funnel: distinct people who ever started (onboarding_progress) +
# anyone who has a users row (some finishers may not be in onboarding_progress)
try:
    started = scalar("""
        SELECT COUNT(*) FROM (
            SELECT phone_number FROM onboarding_progress
            UNION
            SELECT phone_number FROM users
        ) s
    """)
except Exception as e:
    started = f"(onboarding_progress missing? {e})"

total_users = scalar("SELECT COUNT(*) FROM users")
onboarded = scalar("SELECT COUNT(*) FROM users WHERE onboarding_complete = TRUE")
activated = scalar("""
    SELECT COUNT(*) FROM users u
    WHERE u.onboarding_complete = TRUE AND (
        EXISTS(SELECT 1 FROM reminders r WHERE r.phone_number = u.phone_number) OR
        EXISTS(SELECT 1 FROM memories m WHERE m.phone_number = u.phone_number) OR
        EXISTS(SELECT 1 FROM lists l WHERE l.phone_number = u.phone_number)
    )
""")
eng30 = scalar("SELECT COUNT(*) FROM users WHERE onboarding_complete AND last_active_at >= NOW() - INTERVAL '30 days'")
hab7 = scalar("SELECT COUNT(*) FROM users WHERE onboarding_complete AND last_active_at >= NOW() - INTERVAL '7 days'")
paying = scalar("SELECT COUNT(*) FROM users WHERE stripe_subscription_id IS NOT NULL AND stripe_subscription_id <> '' AND subscription_status = 'active'")
comped = scalar("SELECT COUNT(*) FROM users WHERE premium_status = 'premium' AND (stripe_subscription_id IS NULL OR stripe_subscription_id = '')")

print(f"Signed up (started):     {started}")
print(f"  Total users rows:      {total_users}")
print(f"Onboarded:               {onboarded}")
print(f"Activated (any object):  {activated}")
print(f"Engaged (30d):           {eng30}")
print(f"Habit (7d):              {hab7}")
print(f"Paying (real Stripe):    {paying}")
print(f"Comped premium (annot):  {comped}")
c.close()
