# Analytics & ops scripts

One-off, **read-only by default** scripts for diagnosing the product and doing
occasional data ops. They connect directly to Postgres — they do not import the
app. Run them from the repo root with the venv active.

## Connection / safety

Set these in `.env` (gitignored):

- `ANALYTICS_DATABASE_URL` — the DB to read. Use a read-only credential if you
  have one; either way the read scripts open a **read-only session** as a guard.
  Falls back to `DATABASE_URL` if unset.
- `WRITE_DATABASE_URL` — only needed by `extend_beta_grants.py --apply`. The
  Render "External Database URL" is read-write and works here. Falls back to
  `DATABASE_URL`.

The External Database URL from Render is read-write; the read scripts stay safe
by forcing a read-only session on top of it, not by relying on the credential.

## Scripts

| Script | What it does | Writes? |
|--------|--------------|---------|
| `diagnose.py` | Funnel / retention / activation / delivery / intent diagnosis (9 sections). | No |
| `funnel_probe.py` | Just the conversion-funnel stage counts (mirrors `/admin/funnel`). | No |
| `check_grants.py` | Beta premium grant lengths + expiry timing. | No |
| `extend_beta_grants.py` | Extends time-boxed beta comp grants so they don't expire. **Dry-run unless `--apply`.** | Only with `--apply` |

```bash
python analytics/diagnose.py
python analytics/funnel_probe.py
python analytics/check_grants.py
python analytics/extend_beta_grants.py            # preview (no writes)
python analytics/extend_beta_grants.py --apply    # extend (needs WRITE_DATABASE_URL)
python analytics/extend_beta_grants.py --days 60  # override extension length
```

Note: phone numbers are stored plaintext in prod; names/emails may be encrypted.
The read scripts auto-detect encryption and join on `phone_hash` when needed.
