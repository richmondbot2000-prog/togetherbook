"""
Generate yesterday-payouts.json — names / states / zips of US borrowers whose
loans paid out yesterday (or on Friday if today is a Sunday or Monday, since
no payouts run on weekends).

Used by yesterday.html on the kids site.

Required env vars: same set the row-counts scan uses
(FABRIC_SQL_ENDPOINT, FABRIC_TENANT_ID, FABRIC_CLIENT_ID, FABRIC_CLIENT_SECRET).
"""
from __future__ import annotations

import csv
import datetime
import json
import os
import sys
from pathlib import Path

import pyodbc

ZIP_CITY_CSV = Path(__file__).resolve().parent.parent / "data" / "us-zip-city.csv"


def load_zip_lookup():
    """Return {zip5: {city, county, lat, lng}} from the bundled GeoNames-derived CSV.

    The lat/lng are *city centroids* (averaged across all zips in that city),
    not zip centroids — this means publishing them does not let anyone recover
    the borrower's specific zip, only the city they live in.
    """
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

    zip_lookup = load_zip_lookup()

    items = []
    target = None
    misses = 0
    for first_name, state, zipcode, amount, payout_date in rows:
        target = payout_date  # all rows share the same date by construction
        z5 = (zipcode or "").strip()[:5].zfill(5) if (zipcode or "").strip() else ""
        info = zip_lookup.get(z5)
        if not info:
            misses += 1
        # Note: we deliberately drop the zip here. The output JSON only carries
        # first name, state, city, county, city-centroid lat/lng, and amount.
        items.append({
            "first_name": (first_name or "").strip(),
            "state":      (state or "").strip(),
            "city":       info["city"]   if info else None,
            "county":     info["county"] if info else None,
            "amount":     float(amount) if amount is not None else 0.0,
            "lat":        info["lat"]    if info else None,
            "lng":        info["lng"]    if info else None,
        })
    conn.close()
    print(f"# zip lookup misses: {misses}/{len(items)} (city blank for those rows)", flush=True)

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
