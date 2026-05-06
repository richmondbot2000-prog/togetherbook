"""
Generate yesterday-payouts.json — names / states / zips of US borrowers whose
loans paid out yesterday (or on Friday if today is a Sunday or Monday, since
no payouts run on weekends).

Used by yesterday.html on the kids site.

Required env vars: same set the row-counts scan uses
(FABRIC_SQL_ENDPOINT, FABRIC_TENANT_ID, FABRIC_CLIENT_ID, FABRIC_CLIENT_SECRET).
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

import pyodbc


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


QUERY = """
DECLARE @days_back int = CASE DATENAME(weekday, GETDATE())
                          WHEN 'Sunday' THEN 2
                          WHEN 'Monday' THEN 3
                          ELSE 1 END;
DECLARE @target date = DATEADD(day, -@days_back, CAST(GETDATE() AS date));

SELECT
  c.FirstName              AS first_name,
  c.StateCounty            AS state,
  c.Postcode               AS zip,
  li.LoanAmountAtInception AS amount,
  CAST(li.LoanAgreementDateLocal AS date) AS payout_date
FROM dbo.LoanAtInception li
JOIN dbo.Loan      l  ON l.LoanBookID = li.LoanBookID
JOIN dbo.Customer  c  ON c.LoanBookID = li.LoanBookID AND c.RelationToBrw IS NULL
JOIN dbo.Lenders   le ON le.LenderID  = l.LenderID
WHERE le.Country = 'USA'
  AND CAST(li.LoanAgreementDateLocal AS date) = @target
ORDER BY c.StateCounty, c.FirstName
"""


def main() -> None:
    started = datetime.datetime.utcnow()
    print(f"# scan_yesterday_payouts start ({started.isoformat()}Z)", flush=True)

    conn = pyodbc.connect(conn_str(), timeout=30)
    conn.timeout = 120
    cur = conn.cursor()
    cur.execute(QUERY)
    rows = cur.fetchall()

    items = []
    target = None
    for first_name, state, zipcode, amount, payout_date in rows:
        target = payout_date  # all rows share the same date by construction
        items.append({
            "first_name": (first_name or "").strip(),
            "state":      (state or "").strip(),
            "zip":        (zipcode or "").strip(),
            "amount":     float(amount) if amount is not None else 0.0,
        })
    conn.close()

    payload = {
        "schema_version": 1,
        "updated_at": started.isoformat() + "Z",
        "target_date": target.isoformat() if target else None,
        "totals": {"borrowers": len(items)},
        "items": items,
    }

    out_path = Path(os.environ.get("OUT", "yesterday-payouts.json")).resolve()
    out_path.write_text(json.dumps(payload, indent=2))
    print(
        f"# wrote {out_path} — {len(items)} borrowers for {payload['target_date']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
