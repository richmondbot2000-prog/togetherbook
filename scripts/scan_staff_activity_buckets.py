"""
Per-user, per-15-min-bucket activity. Accumulates into a permanent
`staff-activity-buckets.json` — each scan only re-pulls the date range
the caller asked for (default: yesterday only), so older data never
gets overwritten unless an admin explicitly refreshes a month.

Date-range envvars (optional):
  ACTIVITY_START_DATE  YYYY-MM-DD  — first day to scan (inclusive)
  ACTIVITY_END_DATE    YYYY-MM-DD  — last day to scan (inclusive)
Default: both = yesterday UTC. Caller sets these via the
refresh-staff-activity workflow's workflow_dispatch inputs.

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

DEFAULT_LOOKBACK_DAYS = 1  # scheduled run pulls yesterday only

TS_DATA_TYPES = ('datetime', 'datetime2', 'datetimeoffset', 'smalldatetime', 'date')

TS_NAME_PREF = (
    "DateTimeUTC", "DateTimeUtc", "UTCTime", "UtcTime",
    "EventDateUTC", "CreatedDateUTC", "CreatedAtUTC",
    "EventDateTime", "DateTime", "EventDate", "Created", "CreatedAt",
    "ModifiedDateUTC", "ModifiedAtUTC", "ModifiedDate", "ModifiedAt",
    "InsertDateUTC", "InsertedAt", "Stamp", "Timestamp", "EventTime",
    "StatusTime", "ReceivedAt", "ReceivedUtc", "SentAt", "SentUtc",
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


def scan_database(database: str, start: datetime.datetime, end_exclusive: datetime.datetime, buckets_out, events_out):
    """For each table in `database` that has ClientUsername + timestamp,
    scan rows in [start, end_exclusive) and:
       - aggregate 15-min buckets into `buckets_out[un][iso] = set(bucket_idx)`
       - aggregate per-(un, date, source, bucket) write count into
         `events_out` keyed (un, iso, src, bucket) → row.
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
        for s, t, ts in targets:
            print(f"    · {s}.{t} (ts={ts})", flush=True)
    except Exception as e:
        print(f"  ! schema scan failed: {e}", flush=True)
        try: c.close()
        except: pass
        return

    start_str = start.strftime("%Y-%m-%d %H:%M:%S")
    end_str   = end_exclusive.strftime("%Y-%m-%d %H:%M:%S")
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
                f"WHERE [{ts}] >= ? AND [{ts}] < ? "
                f"  AND ClientUsername IS NOT NULL "
                f"  AND (ClientUsername LIKE '%@%' OR ClientUsername LIKE '%.%') "
                f"GROUP BY LOWER(ClientUsername), CAST([{ts}] AS DATE), "
                f"         (DATEPART(HOUR, [{ts}]) * 4 + DATEPART(MINUTE, [{ts}]) / 15)"
            )
            cur.execute(q, [start_str, end_str])
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


def parse_date(s: str | None):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s.strip(), "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    except Exception:
        sys.exit(f"error: bad date '{s}' — expected YYYY-MM-DD")


def main() -> None:
    started = datetime.datetime.now(datetime.timezone.utc)
    today = started.date()
    yesterday = today - datetime.timedelta(days=1)

    start = parse_date(os.environ.get("ACTIVITY_START_DATE")) or datetime.datetime(
        yesterday.year, yesterday.month, yesterday.day, tzinfo=datetime.timezone.utc)
    end_inclusive = parse_date(os.environ.get("ACTIVITY_END_DATE")) or datetime.datetime(
        yesterday.year, yesterday.month, yesterday.day, tzinfo=datetime.timezone.utc)
    # Scan filter is [start, end_exclusive) where end_exclusive = end+1d.
    end_exclusive = end_inclusive + datetime.timedelta(days=1)

    print(f"# scan_staff_activity_buckets start {started.isoformat()}", flush=True)
    print(f"# range: {start.date().isoformat()} → {end_inclusive.date().isoformat()} (inclusive)", flush=True)

    # All ISO dates inside the requested window — we'll wipe these from
    # the existing doc before merging so a forced refresh is idempotent.
    dates_in_window = []
    d = start.date()
    while d <= end_inclusive.date():
        dates_in_window.append(d.isoformat())
        d = d + datetime.timedelta(days=1)
    window_set = set(dates_in_window)

    all_buckets = defaultdict(lambda: defaultdict(set))   # un → iso → set(bucket)
    all_events  = {}                                       # (un, iso, src, bucket) → row
    for db in DATABASES:
        scan_database(db, start, end_exclusive, all_buckets, all_events)

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

    # Load existing doc so we MERGE rather than overwrite.
    out_path = Path("staff-activity-buckets.json").resolve()
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
        except Exception:
            existing = {}
    else:
        existing = {}
    by_email = existing.get("by_email") or {}

    # Step 1 — wipe the requested window from every existing user. A
    # forced refresh of a month should not leave stale entries from a
    # prior scan with different filters.
    for email, rec in by_email.items():
        for iso in dates_in_window:
            if rec.get("buckets") and iso in rec["buckets"]: del rec["buckets"][iso]
            if rec.get("events")  and iso in rec["events"]:  del rec["events"][iso]

    # Step 2 — merge fresh data for the window.
    for email, dates in rolled_buckets.items():
        rec = by_email.setdefault(email, {"buckets": {}, "events": {}})
        rec.setdefault("buckets", {})
        rec.setdefault("events", {})
        for iso, buckets in dates.items():
            if iso not in window_set: continue  # safety
            rec["buckets"][iso] = sorted(buckets)
    for email, dates in rolled_events.items():
        rec = by_email.setdefault(email, {"buckets": {}, "events": {}})
        rec.setdefault("events", {})
        for iso, evs in dates.items():
            if iso not in window_set: continue
            evs.sort(key=lambda e: e.get("first_at") or "")
            rec["events"][iso] = evs[:200]   # cap

    existing["schema_version"] = 2
    existing["last_pull_at"]   = started.isoformat()
    existing["by_email"]       = by_email
    existing["active_count"]   = len(by_email)
    # Append a row to a per-day-pulled log so we know which dates have
    # been scanned at least once (the page can show "last refreshed
    # YYYY-MM-DD" + grey out dates we haven't pulled yet).
    pulled = existing.setdefault("pulled", {})
    for iso in dates_in_window:
        pulled[iso] = started.isoformat()

    out_path.write_text(json.dumps(existing, indent=2))
    print(f"# wrote {out_path} — merged {len(rolled_buckets)} users across {len(dates_in_window)} day(s)", flush=True)


if __name__ == "__main__":
    main()
