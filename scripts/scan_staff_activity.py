"""
Enrich staff.json with last-60-days activity data from the Fabric warehouse.

For each Workspace user we know about, we look up their `ClientUsername`
identities across every reporting database (matching local-part@<known-domain>)
and aggregate:

  - `last_active_utc`  — most recent write across any tenant identity (≤60d only)
  - `tenants`          — distinct tenant domains they showed up under
  - `top_warehouse`    — reporting DB they wrote to most in the window
  - `writes_60d`       — total writes in the 60-day window

Only staff with at least one write in the last 60 days end up in the output —
that's the "active" filter the directory page uses.

Output:  staff-activity.json   at repo root
Inputs:  staff.json            (Workspace user list)

Required env vars (same as scan_row_counts.py):
  FABRIC_SQL_ENDPOINT, FABRIC_TENANT_ID, FABRIC_CLIENT_ID, FABRIC_CLIENT_SECRET
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import pyodbc


# Reporting DBs that have ClientUsername columns (per the probe).
DATABASES = [
    "ReportingApplications",
    "ReportingBrokers",
    "ReportingCentralCrm",
    "ReportingCommunications",
    "ReportingLoanbook",
    "ReportingPayments",
    "Whitebox",
]

# Domains that ClientUsername values use in production. Discovered via the
# probe — internal staff log writes from rgroup.co.uk *and* per-tenant CRM
# emails. We try every domain so cross-tenant operators get fully counted.
KNOWN_DOMAINS = [
    "rgroup.co.uk",        # legacy internal / RG
    "letme.co.uk",         # current internal
    "letme.com",            # most Workspace primary emails
    "transformcredit.com", # Together Loans / Transform Credit tenant
    "lendingmate.ca",      # Lending Mate
    "rapida.bg",
    "rapidamoney.pl",
    "clearloans.com.au",
    "fianceo.com",
    "tandolan.dk",
    "tandolaina.fi",
]

# Days of activity to consider — anything older is excluded entirely.
WINDOW_DAYS = 60

# Datetime SQL types we accept for the activity timestamp.
TS_DATA_TYPES = ('datetime', 'datetime2', 'datetimeoffset', 'smalldatetime', 'date')

# Preference order when a table has multiple datetime columns — pick the
# most semantically "happened-at" one we can find.
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


def candidate_usernames(staff_email: str) -> set[str]:
    """All ClientUsername values that plausibly belong to this Workspace user.

    We deliberately exclude bare local-part / first-name matches because they
    risk colliding across staff with the same first name. Strict
    local-part@known-domain matching only.
    """
    local = staff_email.split("@")[0].lower()
    out = {f"{local}@{d}" for d in KNOWN_DOMAINS}
    out.add(staff_email.lower())  # the Workspace email itself
    return out


def domain_from_username(un: str) -> str:
    """Strip the local part to get the tenant tag (used in the `tenants` field)."""
    if "@" in un:
        return un.split("@")[1].lower()
    return ""


def short_tenant(domain: str) -> str:
    """Short label for the tenant pill on the directory card."""
    return {
        "rgroup.co.uk": "rgroup",
        "letme.co.uk": "letme",
        "letme.com": "letme",
        "transformcredit.com": "transform",
        "lendingmate.ca": "lendingmate",
        "rapida.bg": "rapida",
        "rapidamoney.pl": "rapida",
        "clearloans.com.au": "clearloans",
        "fianceo.com": "fianceo",
        "tandolan.dk": "tandolan",
        "tandolaina.fi": "tandolaina",
    }.get(domain, domain.split(".")[0])


def short_warehouse(db: str) -> str:
    """Friendly label for the top warehouse field. e.g. ReportingLoanbook → loanbook."""
    return db.removeprefix("Reporting").lower()


def find_tables_and_timestamp(cur, database: str) -> list[tuple[str, str, str]]:
    """Return [(schema, table, ts_col), ...] for every table in this DB that
    has a ClientUsername column plus at least one datetime-typed column.

    The timestamp column is picked by:
      1) name match against TS_NAME_PREF (in order), then
      2) first datetime-typed column we find.
    """
    # First: every table that has a ClientUsername column.
    cur.execute("""
        SELECT TABLE_SCHEMA, TABLE_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE COLUMN_NAME = 'ClientUsername'
    """)
    cu_tables = {(s, t) for s, t in cur.fetchall()}
    if not cu_tables:
        return []

    # Second: every datetime-ish column on any table.
    placeholders = ",".join(["?"] * len(TS_DATA_TYPES))
    cur.execute(
        f"""
        SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE DATA_TYPE IN ({placeholders})
        """,
        list(TS_DATA_TYPES),
    )
    ts_by_table: dict[tuple[str, str], list[str]] = defaultdict(list)
    for s, t, c, _ in cur.fetchall():
        ts_by_table[(s, t)].append(c)

    out: list[tuple[str, str, str]] = []
    for st in cu_tables:
        cols = ts_by_table.get(st, [])
        if not cols:
            continue
        # Try name preferences first
        chosen = next((p for p in TS_NAME_PREF if p in cols), None)
        if not chosen:
            chosen = cols[0]   # fall back to whichever datetime column is first
        out.append((st[0], st[1], chosen))
    return out


def scan_database(database: str, all_candidates: set[str], cutoff: datetime.datetime) -> dict[str, dict]:
    """Return { username_lower -> { writes, last_at, tables_seen[] } }."""
    print(f"# {database}", flush=True)
    aggregated: dict[str, dict] = defaultdict(lambda: {"writes": 0, "last_at": None, "tables": set()})
    try:
        c = pyodbc.connect(conn_str(database), timeout=20)
        c.timeout = QUERY_TIMEOUT
        cur = c.cursor()
    except Exception as e:
        print(f"  ! connect failed: {e}", flush=True)
        return aggregated

    try:
        targets = find_tables_and_timestamp(cur, database)
        print(f"  - {len(targets)} table(s) with ClientUsername + timestamp", flush=True)
    except Exception as e:
        print(f"  ! schema scan failed: {e}", flush=True)
        try: c.close()
        except: pass
        return aggregated

    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    # SQL IN-list of candidate usernames. Fabric SQL Server has a limit
    # around 2,100 parameters per query; we have <100 staff × 12 domains < 1500
    # so a single IN list is fine. Lowercase comparison via LOWER().
    cands_csv = ", ".join(["?"] * len(all_candidates))
    cands_lower = [c.lower() for c in all_candidates]

    for schema, table, ts in targets:
        try:
            q = (
                f"SELECT LOWER(ClientUsername) AS un, "
                f"       COUNT_BIG(*) AS writes, "
                f"       MAX([{ts}]) AS last_at "
                f"FROM [{schema}].[{table}] "
                f"WHERE [{ts}] >= ? AND LOWER(ClientUsername) IN ({cands_csv}) "
                f"GROUP BY LOWER(ClientUsername)"
            )
            cur.execute(q, [cutoff_str, *cands_lower])
            rows = cur.fetchall()
            for un, writes, last_at in rows:
                bucket = aggregated[un]
                bucket["writes"] += int(writes or 0)
                if last_at is not None:
                    if bucket["last_at"] is None or last_at > bucket["last_at"]:
                        bucket["last_at"] = last_at
                bucket["tables"].add(f"{schema}.{table}")
        except Exception as e:
            print(f"  ! {schema}.{table}: {e}", flush=True)
            continue
    try: c.close()
    except: pass
    return aggregated


def main() -> None:
    started = datetime.datetime.now(datetime.timezone.utc)
    cutoff = started - datetime.timedelta(days=WINDOW_DAYS)
    print(f"# scan_staff_activity start {started.isoformat()}  cutoff={cutoff.isoformat()}", flush=True)

    staff = json.loads(Path("staff.json").read_text())["users"]
    print(f"# staff.json: {len(staff)} users", flush=True)

    # username (lowercase) -> staff record
    username_to_staff: dict[str, dict] = {}
    all_candidates: set[str] = set()
    for u in staff:
        for cand in candidate_usernames(u["email"]):
            cand = cand.lower()
            # If two staff have identical candidates (shouldn't, since local-parts
            # are unique within Workspace), keep the first.
            username_to_staff.setdefault(cand, u)
            all_candidates.add(cand)

    print(f"# candidates per staff: ~{len(all_candidates) // max(len(staff), 1)} | total: {len(all_candidates)}", flush=True)

    # email (lowercase) -> activity record
    activity: dict[str, dict] = defaultdict(lambda: {
        "writes_60d": 0,
        "last_active_utc": None,
        "by_db": defaultdict(int),
        "tenants_set": set(),
    })

    for db in DATABASES:
        per_user = scan_database(db, all_candidates, cutoff)
        for un, agg in per_user.items():
            staff_record = username_to_staff.get(un)
            if not staff_record:
                continue
            email = staff_record["email"].lower()
            entry = activity[email]
            entry["writes_60d"] += agg["writes"]
            if agg["last_at"] is not None:
                if entry["last_active_utc"] is None or agg["last_at"] > entry["last_active_utc"]:
                    entry["last_active_utc"] = agg["last_at"]
            entry["by_db"][db] += agg["writes"]
            entry["tenants_set"].add(short_tenant(domain_from_username(un)))

    # Materialize for JSON
    out_users = []
    for email, entry in activity.items():
        if entry["writes_60d"] == 0:
            continue
        top_db = max(entry["by_db"].items(), key=lambda kv: kv[1])[0]
        out_users.append({
            "email": email,
            "writes_60d": entry["writes_60d"],
            "last_active_utc": entry["last_active_utc"].isoformat() if entry["last_active_utc"] else None,
            "tenants": sorted(t for t in entry["tenants_set"] if t),
            "top_warehouse": short_warehouse(top_db),
        })
    out_users.sort(key=lambda u: u["writes_60d"], reverse=True)

    output = {
        "snapshot_at": started.isoformat(),
        "snapshot_date": started.date().isoformat(),
        "window_days": WINDOW_DAYS,
        "cutoff_utc": cutoff.isoformat(),
        "active_count": len(out_users),
        "active_users": out_users,
    }
    out_path = Path("staff-activity.json")
    out_path.write_text(json.dumps(output, indent=2, default=str))
    print(f"# wrote {out_path} ({out_path.stat().st_size} bytes); {len(out_users)} active staff", flush=True)


if __name__ == "__main__":
    main()
