"""Read-only: list non-Premium users who have active recurring reminders.

Recurring reminders are a Premium-only feature. A user can end up free-tier
with active recurring rows after a Premium trial ends. This report shows who
they are and, matching the app's own logic (services.tier_service), splits them
into:

  WILL PAUSE     - free tier, no active trial -> generation now skips them
  STILL FIRING   - active trial (app treats as Premium) -> not affected

"Premium" here means premium_status IN ('premium','family'). An active trial is
trial_end_date > NOW(), which get_user_tier() treats as Premium.

Read-only session; never writes. Joins recurring_reminders on phone_number
(that table has no phone_hash). Phone numbers are plaintext in prod; first_name
may be encrypted (shown raw, may be blank/ciphertext).
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()
url = os.environ.get("ANALYTICS_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not url:
    raise SystemExit("Set ANALYTICS_DATABASE_URL or DATABASE_URL in .env")

conn = psycopg2.connect(url)
conn.set_session(readonly=True, autocommit=True)
cur = conn.cursor()

cur.execute("""
    SELECT
      u.phone_number,
      u.first_name,
      COALESCE(u.premium_status, '(null)')                       AS premium_status,
      u.trial_end_date,
      (u.trial_end_date IS NOT NULL AND u.trial_end_date > NOW()) AS active_trial,
      rr.id,
      rr.reminder_text,
      rr.recurrence_type,
      rr.recurrence_day,
      rr.reminder_time,
      rr.last_generated_date
    FROM users u
    JOIN recurring_reminders rr
      ON rr.phone_number = u.phone_number
     AND rr.active = TRUE
    WHERE COALESCE(u.premium_status, 'free') NOT IN ('premium', 'family')
    ORDER BY active_trial, u.phone_number, rr.id
""")
rows = cur.fetchall()

# Group rows by user
users = {}  # phone -> dict
for (phone, name, status, trial_end, active_trial, rid, text, rtype, rday,
     rtime, last_gen) in rows:
    u = users.setdefault(phone, {
        "name": name, "status": status, "trial_end": trial_end,
        "active_trial": active_trial, "recurring": [],
    })
    u["recurring"].append({
        "id": rid, "text": text, "type": rtype, "day": rday,
        "time": str(rtime) if rtime else None, "last_gen": last_gen,
    })

will_pause = {p: u for p, u in users.items() if not u["active_trial"]}
still_firing = {p: u for p, u in users.items() if u["active_trial"]}


def dump(title, group):
    print(f"\n=== {title} ({len(group)} users, "
          f"{sum(len(u['recurring']) for u in group.values())} recurring) ===")
    if not group:
        print("  (none)")
        return
    for phone, u in group.items():
        nm = (u["name"] or "").strip() or "(no name)"
        trial = u["trial_end"].date() if u["trial_end"] else "-"
        print(f"\n  {phone}  {nm}   tier={u['status']}  trial_end={trial}")
        for r in u["recurring"]:
            day = f" day={r['day']}" if r["day"] is not None else ""
            lg = r["last_gen"] or "never"
            print(f"      [{r['id']}] {r['type']}{day} @ {r['time']} "
                  f"last_generated={lg} :: {r['text']}")


dump("WILL PAUSE (free, no active trial)", will_pause)
dump("STILL FIRING (active trial = treated as Premium)", still_firing)

print(f"\nTotal non-Premium users with active recurring reminders: {len(users)}")
print(f"  will pause now : {len(will_pause)}")
print(f"  still firing   : {len(still_firing)} (active trial)")

conn.close()
