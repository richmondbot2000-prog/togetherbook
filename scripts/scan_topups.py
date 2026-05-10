"""
Scan the Fabric warehouse for monthly Top-Up Eligibility data.

Output: `topups.json` at repo root, used by `topups.html` to render a 24-month
chart of:
  - live_loans     — distinct LoanbookIds with any LoanHistory entry that month
  - tue_eligible   — distinct LoanbookIds with at least one LoanHistory entry
                     where TueStatus=1 in that month

Source: `ReportingLoanbook.dbo.Loan_History` (~197M rows; snapshots per live
loan per day, plus one row per transaction). The query buckets by month over
the last 24 calendar months (UTC), grouping per LoanbookId so each loan
counts once per month regardless of how many snapshots it has.

Required env vars (same as scan_row_counts.py):
  FABRIC_SQL_ENDPOINT, FABRIC_TENANT_ID, FABRIC_CLIENT_ID, FABRIC_CLIENT_SECRET
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

import pyodbc

DATABASE = "ReportingLoanbook"
TABLE_SCHEMA = "dbo"
TABLE_NAME = "Loan_History"

# Filter to a single lender — Transform Credit / Together Loans (USA) per the
# wiki's canonical lender mapping. Set to None to scan all lenders.
LENDER_ID = 6
LENDER_LABEL = "Transform Credit (LenderId 6, USA)"

# Months back from today's first-of-month. 24 = include the 24 most recent
# calendar months (the current month is partial).
WINDOW_MONTHS = 24

# Datetime SQL types we'll accept for the "when did this snapshot happen" column.
TS_DATA_TYPES = ('datetime', 'datetime2', 'datetimeoffset', 'smalldatetime', 'date')

# Preference order — pick the most semantically "happened-at" name available.
TS_NAME_PREF = (
    "DateTUtc", "DateTimeUtc", "DateTimeUTC",
    "DateTLocal", "DateTimeLocal",
    "EventDateUTC", "CreatedDateUTC", "CreatedAtUTC", "ModifiedDateUTC",
    "TimestampUTC", "InsertDateUTC",
)

QUERY_TIMEOUT = 600


def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"error: {name} not set")
    return v


def conn_str() -> str:
    return (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server={env('FABRIC_SQL_ENDPOINT')},1433;"
        f"Database={DATABASE};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=20;"
        "Authentication=ActiveDirectoryServicePrincipal;"
        f"UID={env('FABRIC_CLIENT_ID')};"
        f"PWD={env('FABRIC_CLIENT_SECRET')};"
    )


def discover_timestamp_column(cur) -> str:
    """Find the best timestamp column on Loan_History via INFORMATION_SCHEMA."""
    placeholders = ",".join(["?"] * len(TS_DATA_TYPES))
    cur.execute(
        f"""
        SELECT COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
          AND DATA_TYPE IN ({placeholders})
        """,
        [TABLE_SCHEMA, TABLE_NAME, *TS_DATA_TYPES],
    )
    cols = [c for c, _ in cur.fetchall()]
    if not cols:
        sys.exit(f"error: no datetime-typed columns found on {TABLE_SCHEMA}.{TABLE_NAME}")
    chosen = next((p for p in TS_NAME_PREF if p in cols), cols[0])
    print(f"# timestamp column: {chosen}  (other datetime cols here: {[c for c in cols if c != chosen]})", flush=True)
    return chosen


def main() -> None:
    started = datetime.datetime.now(datetime.timezone.utc)
    today = started.date()
    # Earliest month we want is (today.year/month - WINDOW_MONTHS + 1)'s 1st.
    # We compute it by stepping back month-by-month so calendar arithmetic
    # behaves correctly across year boundaries.
    y, m = today.year, today.month
    for _ in range(WINDOW_MONTHS - 1):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    cutoff = datetime.date(y, m, 1)
    print(f"# scan_topups start {started.isoformat()}  cutoff={cutoff}  window={WINDOW_MONTHS} months", flush=True)

    c = pyodbc.connect(conn_str(), timeout=20)
    c.timeout = QUERY_TIMEOUT
    cur = c.cursor()
    ts = discover_timestamp_column(cur)

    # One sweep, grouped by month + LoanbookId so a loan with 30 snapshots
    # in a month counts once. Then sum to per-month rows. The CTE keeps the
    # query memory-efficient on the 197M-row source.
    lender_filter = "AND LenderId = ?" if LENDER_ID is not None else ""
    q = f"""
        WITH per_loan_month AS (
            SELECT
                DATEFROMPARTS(YEAR([{ts}]), MONTH([{ts}]), 1) AS month_start,
                LoanbookId,
                MAX(CASE WHEN TueStatus = 1 THEN 1 ELSE 0 END) AS was_tue
            FROM [{TABLE_SCHEMA}].[{TABLE_NAME}]
            WHERE [{ts}] >= ?
              AND LoanbookId IS NOT NULL
              {lender_filter}
            GROUP BY DATEFROMPARTS(YEAR([{ts}]), MONTH([{ts}]), 1), LoanbookId
        )
        SELECT
            month_start,
            COUNT(*) AS live_loans,
            SUM(was_tue) AS tue_eligible
        FROM per_loan_month
        GROUP BY month_start
        ORDER BY month_start;
    """
    params = [cutoff] + ([LENDER_ID] if LENDER_ID is not None else [])
    print(f"# running aggregation query…  lender filter: {LENDER_LABEL if LENDER_ID is not None else 'NONE (all lenders)'}", flush=True)
    cur.execute(q, params)
    rows = [(r[0], int(r[1]), int(r[2])) for r in cur.fetchall()]
    print(f"# returned {len(rows)} month rows", flush=True)
    try:
        c.close()
    except Exception:
        pass

    # Build the output: ensure every month in the window is present (zeros if
    # absent) so the chart's x-axis is dense.
    by_month = {(r[0].year, r[0].month): r for r in rows}
    series = []
    y, m = cutoff.year, cutoff.month
    for _ in range(WINDOW_MONTHS):
        bucket = by_month.get((y, m))
        if bucket:
            _, live, tue = bucket
        else:
            live, tue = 0, 0
        series.append({
            "month": f"{y:04d}-{m:02d}",
            "live_loans": live,
            "tue_eligible": tue,
        })
        m += 1
        if m == 13:
            m = 1
            y += 1

    # Totals across the window — useful headline numbers.
    total_live = sum(s["live_loans"] for s in series)
    total_tue = sum(s["tue_eligible"] for s in series)

    output = {
        "snapshot_at": started.isoformat(),
        "snapshot_date": today.isoformat(),
        "window_months": WINDOW_MONTHS,
        "cutoff_month": cutoff.isoformat(),
        "source_table": f"{DATABASE}.{TABLE_SCHEMA}.{TABLE_NAME}",
        "timestamp_column": ts,
        "lender_id": LENDER_ID,
        "lender_label": LENDER_LABEL,
        "totals": {
            "live_loans_loan_months":   total_live,   # SUM, not distinct
            "tue_eligible_loan_months": total_tue,
        },
        "series": series,
    }
    out_path = Path("topups.json")
    out_path.write_text(json.dumps(output, indent=2))
    print(f"# wrote {out_path} ({out_path.stat().st_size} bytes); months={len(series)} totals live={total_live} tue={total_tue}", flush=True)


if __name__ == "__main__":
    main()
