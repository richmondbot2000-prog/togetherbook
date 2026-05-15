"""
Generate payouts-week.json (last 7 days, per-borrower) + payouts-year.json
(rolling 12 completed calendar months, aggregated by state + by month).

Sister script to scan_yesterday_payouts.py — refreshed by the same daily
workflow. Splits a year of payouts into two datasets:

- Week: identical shape to yesterday-payouts.json, so the page renderer
  can reuse the same pin / state / table pipeline.
- Year: per-state and per-month aggregates only. ~50k borrowers a year
  would push the JSON over 7 MB and brick the page on mobile Safari,
  so we serve aggregates instead and the page renders a state-totals
  map + state-averages map + monthly bar chart (no pin map, no per-
  borrower tables).

Date windows (all dates inclusive, evaluated against `LoanAgreementDateLocal`):

- Week  = [TODAY - 6, TODAY]  (last 7 calendar days incl. today)
- Year  = [first_of_month(this_month - 11), last_of_month(this_month - 1)]
          i.e. 12 completed calendar months ending last month.

Required env vars: same set as scan_yesterday_payouts.py
(FABRIC_SQL_ENDPOINT, FABRIC_TENANT_ID, FABRIC_CLIENT_ID, FABRIC_CLIENT_SECRET).
"""
from __future__ import annotations

import csv
import datetime
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import pyodbc

ZIP_CITY_CSV = Path(__file__).resolve().parent.parent / "data" / "us-zip-city.csv"


def load_zip_lookup():
    out = {}
    if not ZIP_CITY_CSV.exists():
        print(f"# WARNING: {ZIP_CITY_CSV} missing — no city info will be attached", flush=True)
        return out
    with open(ZIP_CITY_CSV) as f:
        reader = csv.DictReader(f)
        for row in reader:
            z = (row.get("zip") or "").strip().zfill(5)
            try:
                lat = float((row.get("lat") or "").strip())
                lng = float((row.get("lng") or "").strip())
            except ValueError:
                continue
            out[z] = {
                "city":   (row.get("city") or "").strip(),
                "county": (row.get("county") or "").strip(),
                "lat":    lat,
                "lng":    lng,
            }
    return out


def env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"error: {name} not set")
    return v


def conn_str(database: str = "ReportingLoanbook") -> str:
    return (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server={env('FABRIC_SQL_ENDPOINT')},1433;"
        f"Database={database};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=15;"
        "Authentication=ActiveDirectoryServicePrincipal;"
        f"UID={env('FABRIC_CLIENT_ID')};"
        f"PWD={env('FABRIC_CLIENT_SECRET')};"
    )


# Per-borrower (used by the Week dataset).
WEEK_QUERY = """
DECLARE @to date = CAST(GETDATE() AS date);
DECLARE @from date = DATEADD(day, -6, @to);

SELECT
  c.FirstName              AS first_name,
  c.StateCounty            AS state,
  c.Postcode               AS zip,
  li.LoanAmountAtInception AS amount,
  CAST(li.LoanAgreementDateLocal AS date) AS payout_date
FROM dbo.LoanAtInception li
JOIN dbo.Loan      l  ON l.LoanBookID = li.LoanBookID
JOIN dbo.Customer  c  ON c.LoanBookID = li.LoanBookID AND c.RelationToBrw IS NULL
WHERE l.LenderID = 6
  AND CAST(li.LoanAgreementDateLocal AS date) BETWEEN @from AND @to
ORDER BY c.StateCounty, c.FirstName
"""

# Aggregated (used by the Year dataset). Two SELECTs in one batch — pyodbc's
# nextset() pulls the second result set.
YEAR_QUERY = """
DECLARE @first_of_this_month date = DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1);
DECLARE @last_of_last_month  date = DATEADD(day, -1, @first_of_this_month);
DECLARE @first_of_last_month date = DATEADD(month, -1, @first_of_this_month);
DECLARE @year_start date = DATEADD(month, -11, @first_of_last_month);

-- per-state totals
SELECT
  c.StateCounty AS state,
  COUNT(*)      AS borrowers,
  SUM(li.LoanAmountAtInception) AS amount
FROM dbo.LoanAtInception li
JOIN dbo.Loan      l  ON l.LoanBookID = li.LoanBookID
JOIN dbo.Customer  c  ON c.LoanBookID = li.LoanBookID AND c.RelationToBrw IS NULL
WHERE l.LenderID = 6
  AND CAST(li.LoanAgreementDateLocal AS date) BETWEEN @year_start AND @last_of_last_month
GROUP BY c.StateCounty
ORDER BY c.StateCounty;

-- per-month totals
SELECT
  CONVERT(varchar(7), li.LoanAgreementDateLocal, 23) AS month_iso,
  COUNT(*)      AS borrowers,
  SUM(li.LoanAmountAtInception) AS amount
FROM dbo.LoanAtInception li
JOIN dbo.Loan      l  ON l.LoanBookID = li.LoanBookID
JOIN dbo.Customer  c  ON c.LoanBookID = li.LoanBookID AND c.RelationToBrw IS NULL
WHERE l.LenderID = 6
  AND CAST(li.LoanAgreementDateLocal AS date) BETWEEN @year_start AND @last_of_last_month
GROUP BY CONVERT(varchar(7), li.LoanAgreementDateLocal, 23)
ORDER BY month_iso;

-- summary (single row): range + grand totals
SELECT
  MIN(CAST(li.LoanAgreementDateLocal AS date)) AS range_from,
  MAX(CAST(li.LoanAgreementDateLocal AS date)) AS range_to,
  COUNT(*)      AS borrowers,
  SUM(li.LoanAmountAtInception) AS amount,
  @year_start   AS expected_from,
  @last_of_last_month AS expected_to
FROM dbo.LoanAtInception li
JOIN dbo.Loan      l  ON l.LoanBookID = li.LoanBookID
JOIN dbo.Customer  c  ON c.LoanBookID = li.LoanBookID AND c.RelationToBrw IS NULL
WHERE l.LenderID = 6
  AND CAST(li.LoanAgreementDateLocal AS date) BETWEEN @year_start AND @last_of_last_month
"""


def fetch_week(cur, zip_lookup, started):
    cur.execute(WEEK_QUERY)
    rows = cur.fetchall()
    items = []
    dates = []
    for first_name, state, zipcode, amount, payout_date in rows:
        dates.append(payout_date)
        z5 = (zipcode or "").strip()[:5].zfill(5) if (zipcode or "").strip() else ""
        info = zip_lookup.get(z5)
        items.append({
            "first_name": (first_name or "").strip(),
            "state":      (state or "").strip(),
            "city":       info["city"]   if info else None,
            "county":     info["county"] if info else None,
            "amount":     float(amount) if amount is not None else 0.0,
            "lat":        info["lat"]    if info else None,
            "lng":        info["lng"]    if info else None,
        })
    range_from = min(dates).isoformat() if dates else None
    range_to   = max(dates).isoformat() if dates else None
    total = sum(i["amount"] for i in items)
    return {
        "schema_version": 1,
        "updated_at": started.isoformat() + "Z",
        "range": {"from": range_from, "to": range_to},
        "totals": {"borrowers": len(items), "amount": total},
        "items": items,
    }


def fetch_year(cur, started):
    cur.execute(YEAR_QUERY)

    # Result set 1: per-state
    by_state = []
    for state, borrowers, amount in cur.fetchall():
        n = int(borrowers or 0)
        a = float(amount or 0.0)
        by_state.append({
            "state": (state or "").strip(),
            "borrowers": n,
            "amount": a,
            "avg": (a / n) if n else 0.0,
        })

    # Result set 2: per-month
    if not cur.nextset():
        raise RuntimeError("year query: missing 2nd result set (per-month)")
    by_month = []
    for month_iso, borrowers, amount in cur.fetchall():
        by_month.append({
            "month": (month_iso or "").strip(),
            "borrowers": int(borrowers or 0),
            "amount": float(amount or 0.0),
        })

    # Result set 3: summary (single row)
    if not cur.nextset():
        raise RuntimeError("year query: missing 3rd result set (summary)")
    summary_row = cur.fetchone()
    range_from = summary_row[0].isoformat() if summary_row and summary_row[0] else None
    range_to   = summary_row[1].isoformat() if summary_row and summary_row[1] else None
    total_n    = int(summary_row[2] or 0) if summary_row else 0
    total_amt  = float(summary_row[3] or 0.0) if summary_row else 0.0
    # If no rows landed in the window, fall back to the SQL-computed window
    # bounds (the @year_start / @last_of_last_month variables) so the page
    # can still label the range.
    if (not range_from or not range_to) and summary_row:
        if summary_row[4]: range_from = summary_row[4].isoformat()
        if summary_row[5]: range_to   = summary_row[5].isoformat()

    return {
        "schema_version": 1,
        "updated_at": started.isoformat() + "Z",
        "range": {"from": range_from, "to": range_to},
        "totals": {"borrowers": total_n, "amount": total_amt},
        "by_state": by_state,
        "by_month": by_month,
    }


def main() -> None:
    started = datetime.datetime.utcnow()
    print(f"# scan_payouts_history start ({started.isoformat()}Z)", flush=True)

    conn = pyodbc.connect(conn_str(), timeout=30)
    conn.timeout = 180
    cur = conn.cursor()

    zip_lookup = load_zip_lookup()

    week = fetch_week(cur, zip_lookup, started)
    year = fetch_year(cur, started)

    conn.close()

    out_week = Path(os.environ.get("OUT_WEEK", "payouts-week.json")).resolve()
    out_year = Path(os.environ.get("OUT_YEAR", "payouts-year.json")).resolve()
    out_week.write_text(json.dumps(week, indent=2))
    out_year.write_text(json.dumps(year, indent=2))

    print(
        f"# wrote {out_week} — {week['totals']['borrowers']} borrowers "
        f"({week['range']['from']} → {week['range']['to']})", flush=True
    )
    print(
        f"# wrote {out_year} — {year['totals']['borrowers']} borrowers across "
        f"{len(year['by_state'])} states / {len(year['by_month'])} months "
        f"({year['range']['from']} → {year['range']['to']})", flush=True
    )


if __name__ == "__main__":
    main()
