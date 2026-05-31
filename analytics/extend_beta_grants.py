"""
One-off: extend the beta premium grants so they don't expire on 2026-06-01.

The ~28-31 beta users were given 90-day comp premium that all lands on
2026-06-01. We're pushing that out to buy time to build a real conversion
offer (no proven Stripe pay path yet). Engaged users keep premium; nothing
gets yanked on Monday.

SAFE BY DEFAULT: running with no flags only PREVIEWS (read-only). Pass --apply
to actually write. Writes require a read-write URL in WRITE_DATABASE_URL
(falls back to DATABASE_URL); the preview uses ANALYTICS_DATABASE_URL.

  python analytics/extend_beta_grants.py            # dry-run preview
  python analytics/extend_beta_grants.py --apply    # actually extend (needs write URL)
  python analytics/extend_beta_grants.py --days 60  # override extension length

Targets: premium_status='premium' with a time-boxed trial_end_date that is
expiring soon, and NOT a real payer or a permanent 'manual' comp. Idempotent:
users already extended past the new date are skipped.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

import psycopg2
from dotenv import load_dotenv

load_dotenv()

parser = argparse.ArgumentParser()
parser.add_argument("--apply", action="store_true", help="actually write (default: dry-run)")
parser.add_argument("--days", type=int, default=90, help="days from today to extend to (default 90)")
parser.add_argument("--expiring-before", default="2026-07-01",
                    help="only touch grants expiring before this date (default 2026-07-01)")
args = parser.parse_args()

new_end = (datetime.utcnow() + timedelta(days=args.days)).date()

read_url = os.environ.get("ANALYTICS_DATABASE_URL") or os.environ.get("DATABASE_URL")
write_url = os.environ.get("WRITE_DATABASE_URL") or os.environ.get("DATABASE_URL")
url = write_url if args.apply else read_url
if not url:
    sys.exit("No database URL set (ANALYTICS_DATABASE_URL / WRITE_DATABASE_URL / DATABASE_URL).")

# WHERE clause for the beta grants we want to extend
WHERE = """
    premium_status = 'premium'
    AND trial_end_date IS NOT NULL
    AND trial_end_date < %s
    AND (subscription_status IS NULL OR subscription_status NOT IN ('active', 'manual'))
    AND (stripe_subscription_id IS NULL OR stripe_subscription_id = '')
    AND trial_end_date::date < %s
"""
params = (args.expiring_before, new_end)

conn = psycopg2.connect(url)
conn.set_session(readonly=not args.apply, autocommit=False)
cur = conn.cursor()

# Preview the affected rows
cur.execute(f"""
    SELECT RIGHT(phone_number, 4) AS last4, premium_since::date, trial_end_date::date
    FROM users WHERE {WHERE}
    ORDER BY trial_end_date
""", params)
rows = cur.fetchall()

print(f"New expiry target: {new_end}  (today + {args.days} days)")
print(f"Only touching grants expiring before: {args.expiring_before}")
print(f"Matched {len(rows)} beta grant(s) to extend:\n")
print(f"  {'phone':<8}{'granted':<13}{'current_expiry':<14}")
for last4, since, end in rows:
    print(f"  ...{last4:<5}{str(since):<13}{str(end):<14}")

if not args.apply:
    print("\nDRY RUN — nothing written. Re-run with --apply (and a write URL) to extend.")
    conn.close()
    sys.exit(0)

# Apply
cur.execute(f"""
    UPDATE users
    SET trial_end_date = %s,
        trial_warning_7d_sent = FALSE,
        trial_warning_1d_sent = FALSE,
        trial_warning_0d_sent = FALSE
    WHERE {WHERE}
""", (new_end,) + params)
updated = cur.rowcount
conn.commit()
print(f"\nAPPLIED: extended {updated} grant(s) to {new_end}. "
      f"Trial-warning flags reset so a future warning can fire cleanly.")
conn.close()
