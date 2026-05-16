"""
Per-user, per-15-min-bucket activity over the last 14 days.

Sister script to `scan_staff_activity.py`. Where that produces a per-user
write-count + last-active timestamp aggregate over 60 days, this one
produces a sparse map of WHICH 15-min windows each user was active during
in the recent past. Used by the Holidays page's Activity tab to render a
24-hour-per-day strip per direct report.

Output: `staff-activity-buckets.json` at the repo root.

Shape:
```
{
  "schema_version": 1,
  "snapshot_at":    "<ISO>",
  "window_days":    14,
  "cutoff_utc":     "<ISO at start of window>",
  "by_email": {
    "alice@letme.com": {
      "2026-05-15": [32, 33, 34, 35, 60, 61, 62, 63],   // bucket indices (0=00:00, 95=23:45 UTC)
      "2026-05-16": [ ... ]
    },
    ...
  }
}
```

Bucket indexing: `bucket = hour*4 + minute/15`. UTC.

Required env vars: FABRIC_SQL_ENDPOINT, FABRIC_TENANT_ID, FABRIC_CLIENT_ID, FABRIC_CLIENT_SECRET
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import pyodbc


DATABASES = [
    "ReportingApplications",
    "ReportingBrokers",
    "ReportingCentralCrm",
    "ReportingCommunications",
    "ReportingLoanbook",
    "ReportingPayments",
    "Whitebox",
]

WINDOW_DAYS = 14   # short enough to keep the JSON under a few hundred KB

TS_DATA_TYPES = ('datetime', 'datetime2', 'datetimeoffset', 'smalldatetime', 'date')

TS_NAME_PREF = (
    "DateTimeUTC", "EventDateUTC", "CreatedDateUTC", "CreatedAtUTC",
    "EventDateTime", "DateTime", "EventDate", "Created", "CreatedAt",
    "ModifiedDateUTC", "ModifiedAtUTC", "ModifiedDate", "ModifiedAt",
    "InsertDateUTC", "InsertedAt", "Stamp", "Timestamp", "EventTime",
)

QUERY_TIMEOUT = 240


def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"error: {name} not set")
    return v


def conn_str(database: str) -> str:
    return (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server={env('FABRIC_SQL_ENDPOINT')},1433;"
        f"Database={database};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=20;"
        "Authentication=ActiveDirectoryServicePrincipal;"
        f"UID={env('FABRIC_CLIENT_ID')};"
        f"PWD={env('FABRIC_CLIENT_SECRET')};"
    )


def find_tables_and_timestamp(cur, database: str):
    """Same logic as scan_staff_activity.py: every table with ClientUsername
    + a datetime column, picking the most semantic timestamp column."""
    cur.execute("""
        SELECT TABLE_SCHEMA, TABLE_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE COLUMN_NAME = 'ClientUsername'
    """)
    cu_tables = {(s, t) for s, t in cur.fetchall()}
    if not cu_tables:
        return []

    placeholders = ",".join(["?"] * len(TS_DATA_TYPES))
    cur.execute(
        f"""
        SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE DATA_TYPE IN ({placeholders})
        """,
        list(TS_DATA_TYPES),
    )
    ts_by_table = defaultdict(list)
    for s, t, c, _ in cur.fetchall():
        ts_by_table[(s, t)].append(c)

    out = []
    for st in cu_tables:
        cols = ts_by_table.get(st, [])
        if not cols:
            continue
        chosen = next((p for p in TS_NAME_PREF if p in cols), None)
        if not chosen:
            chosen = cols[0]
        out.append((st[0], st[1], chosen))
    return out


def scan_database(database: str, cutoff: datetime.datetime, buckets_out, events_out):
    """For each table in `database` that has ClientUsername + timestamp:
       - aggregate 15-min buckets into `buckets_out[un][iso] = set(bucket_idx)`
       - aggregate per-(un, date, source) write count into `events_out`
         keyed (un, iso, source) → {count, sample_at}. Source label is
         the short warehouse name (e.g. "loanbook.Payment") so a manager
         can see "16 writes to loanbook.Payment between 09:00 and 09:15".
    """
    print(f"# {database}", flush=True)
    try:
        c = pyodbc.connect(conn_str(database), timeout=20)
        c.timeout = QUERY_TIMEOUT
        cur = c.cursor()
    except Exception as e:
        print(f"  ! connect failed: {e}", flush=True)
        return

    try:
        targets = find_tables_and_timestamp(cur, database)
        print(f"  - {len(targets)} table(s) with ClientUsername + timestamp", flush=True)
    except Exception as e:
        print(f"  ! schema scan failed: {e}", flush=True)
        try: c.close()
        except: pass
        return

    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    db_short = database.removeprefix("Reporting").lower()

    for schema, table, ts in targets:
        src = f"{db_short}.{table}"
        try:
            # Filter: ClientUsername must look like a person identifier —
            # either email-form (contains @) or dotted-name form (e.g.
            # `jack.bassilious`). The dotted form matters for
            # ReportingCommunications.dbo.Messages where agent usernames
            # are stored as bare local-parts (no @<domain>), so the
            # original @-only filter was silently dropping every comm
            # sent by an employee.
            q = (
                f"SELECT LOWER(ClientUsername) AS un, "
                f"       CAST([{ts}] AS DATE) AS dt, "
                f"       (DATEPART(HOUR, [{ts}]) * 4 + DATEPART(MINUTE, [{ts}]) / 15) AS bucket, "
                f"       COUNT_BIG(*) AS writes, "
                f"       MIN([{ts}]) AS first_at, "
                f"       MAX([{ts}]) AS last_at "
                f"FROM [{schema}].[{table}] "
                f"WHERE [{ts}] >= ? "
                f"  AND ClientUsername IS NOT NULL "
                f"  AND (ClientUsername LIKE '%@%' OR ClientUsername LIKE '%.%') "
                f"GROUP BY LOWER(ClientUsername), CAST([{ts}] AS DATE), "
                f"         (DATEPART(HOUR, [{ts}]) * 4 + DATEPART(MINUTE, [{ts}]) / 15)"
            )
            cur.execute(q, [cutoff_str])
            for un, dt, bucket, writes, first_at, last_at in cur.fetchall():
                iso = dt.isoformat() if dt else None
                if not iso or bucket is None:
                    continue
                buckets_out[un][iso].add(int(bucket))
                # Per-event entry — one row per (user, date, source,
                # bucket). Carries write count + min/max timestamps so
                # the manager-side drill-down can show e.g. "23 writes
                # to loanbook.Payment 09:00–09:14 UTC".
                key = (un, iso, src, int(bucket))
                ev = events_out.get(key)
                if ev is None:
                    events_out[key] = {
                        "src": src,
                        "bucket": int(bucket),
                        "writes": int(writes or 0),
                        "first_at": first_at.isoformat() if first_at else None,
                        "last_at":  last_at.isoformat()  if last_at  else None,
                    }
                else:
                    ev["writes"] += int(writes or 0)
                    if first_at and (not ev["first_at"] or first_at.isoformat() < ev["first_at"]): ev["first_at"] = first_at.isoformat()
                    if last_at  and (not ev["last_at"]  or last_at.isoformat()  > ev["last_at"]):  ev["last_at"]  = last_at.isoformat()
        except Exception as e:
            print(f"  ! {schema}.{table}: {e}", flush=True)
            continue
    try: c.close()
    except: pass


def domain_local_variants(workspace_email: str):
    """Every ClientUsername form this Workspace user might appear under.
    Includes:
      - the Workspace email itself
      - local-part @ every known tenant domain
      - the bare local-part (no @) — Communications.Messages stores
        agent usernames in this form for outbound CRM messages
    """
    local = workspace_email.split("@")[0].lower()
    domains = [
        "rgroup.co.uk", "letme.co.uk", "letme.com",
        "transformcredit.com", "togetherloans.com",
        "lendingmate.ca", "rapida.bg", "rapidamoney.pl",
        "clearloans.com.au", "fianceo.com",
        "tandolan.dk", "tandolaina.fi",
    ]
    s = {f"{local}@{d}" for d in domains}
    s.add(workspace_email.lower())
    s.add(local)  # bare local-part (Comms agent username form)
    return s


def main() -> None:
    started = datetime.datetime.now(datetime.timezone.utc)
    cutoff = started - datetime.timedelta(days=WINDOW_DAYS)
    print(f"# scan_staff_activity_buckets start {started.isoformat()}  cutoff={cutoff.isoformat()}", flush=True)

    all_buckets = defaultdict(lambda: defaultdict(set))   # un → iso → set(bucket)
    all_events  = {}                                       # (un, iso, src, bucket) → row
    for db in DATABASES:
        scan_database(db, cutoff, all_buckets, all_events)

    # Roll each staff member's ClientUsername variants into the Workspace
    # email. staff.json gives us the canonical list.
    staff_path = Path("staff.json")
    if not staff_path.exists():
        sys.exit("staff.json missing — cannot map ClientUsernames to staff")
    staff = json.loads(staff_path.read_text())
    rolled_buckets = defaultdict(lambda: defaultdict(set))
    rolled_events  = defaultdict(lambda: defaultdict(list))
    for u in staff.get("users", []):
        email = (u.get("email") or "").lower()
        if not email:
            continue
        variants = domain_local_variants(email)
        for un in variants:
            if un in all_buckets:
                for iso, buckets in all_buckets[un].items():
                    rolled_buckets[email][iso] |= buckets
        for (un, iso, src, bucket), row in all_events.items():
            if un in variants:
                rolled_events[email][iso].append(row)

    # Build sparse output. Events sorted by first_at; bucket arrays
    # sorted by index.
    by_email = {}
    for email, dates in rolled_buckets.items():
        rec = { "buckets": {}, "events": {} }
        for iso, buckets in sorted(dates.items()):
            rec["buckets"][iso] = sorted(buckets)
        for iso, evs in rolled_events.get(email, {}).items():
            evs.sort(key=lambda e: e.get("first_at") or "")
            # Cap at 200 detail events per day to keep the JSON bounded.
            rec["events"][iso] = evs[:200]
        by_email[email] = rec

    payload = {
        "schema_version": 1,
        "snapshot_at":    started.isoformat(),
        "window_days":    WINDOW_DAYS,
        "cutoff_utc":     cutoff.isoformat(),
        "active_count":   len(by_email),
        "by_email":       by_email,
    }
    out_path = Path("staff-activity-buckets.json").resolve()
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"# wrote {out_path} — {len(by_email)} users with bucket data over {WINDOW_DAYS} d", flush=True)


if __name__ == "__main__":
    main()
