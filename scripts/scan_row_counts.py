"""
Scan the Fabric data warehouse for per-table row + column counts and write
`row-counts.json` at the repo root. Used by the kids-site database page.

Run by `.github/workflows/refresh-row-counts.yml` once a week. Same script
also works locally if you set the env vars.

Required env vars:
  FABRIC_SQL_ENDPOINT
  FABRIC_TENANT_ID
  FABRIC_CLIENT_ID
  FABRIC_CLIENT_SECRET

Optional env vars:
  FABRIC_DATABASES         comma-separated DB list (default: hardcoded list below)
  PER_DB_TIMEOUT_SECONDS   query timeout per database (default 180)
  ROW_COUNTS_OUTPUT        output file path (default repo-root row-counts.json)
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pyodbc


# Default DB allowlist — the warehouses the kids site cares about.
# Update if more get added to the Fabric workspace.
DEFAULT_DATABASES = [
    "BackingTables",
    "ReportingApplications",
    "ReportingBrokers",
    "ReportingCentralCrm",
    "ReportingCommunications",
    "ReportingCreditbuilder",
    "ReportingLoanbook",
    "ReportingLookup",
    "ReportingPayments",
    "ReportingTracking",
    "Whitebox",
]


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        sys.exit(f"error: {name} not set")
    return v


def conn_str(database: str) -> str:
    return (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server={env('FABRIC_SQL_ENDPOINT')},1433;"
        f"Database={database};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=15;"
        "Authentication=ActiveDirectoryServicePrincipal;"
        f"UID={env('FABRIC_CLIENT_ID')};"
        f"PWD={env('FABRIC_CLIENT_SECRET')};"
    )


def scan_database(database: str, query_timeout: int) -> tuple[str, list[dict], dict[tuple[str, str], int], str | None]:
    """Return (db, items_with_rows, columns_map, error_or_none)."""
    try:
        c = pyodbc.connect(conn_str(database), timeout=15)
        c.timeout = query_timeout
    except Exception as e:
        return database, [], {}, f"connect failed: {e}"

    try:
        cur = c.cursor()
        # 1. List user tables
        cur.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME "
            "FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_TYPE='BASE TABLE'"
        )
        tables = [(r[0], r[1]) for r in cur.fetchall()]
        if not tables:
            return database, [], {}, None

        # 2. Column counts per table (one round-trip)
        cur.execute(
            "SELECT TABLE_SCHEMA, TABLE_NAME, COUNT(*) "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "GROUP BY TABLE_SCHEMA, TABLE_NAME"
        )
        cols = {(r[0], r[1]): int(r[2]) for r in cur.fetchall()}

        # 3. Row counts via one big UNION ALL — Fabric's columnar storage makes
        # COUNT_BIG metadata-fast per table, so this fits in a single query.
        def esc(s: str) -> str: return s.replace("'", "''")
        parts = [
            f"SELECT '{esc(s)}' AS s, '{esc(t)}' AS t, COUNT_BIG(*) AS n FROM [{s}].[{t}]"
            for s, t in tables
        ]
        cur.execute("\nUNION ALL\n".join(parts))
        rows = cur.fetchall()

        items = [
            {
                "database": database,
                "schema": r[0],
                "table": r[1],
                "rows": int(r[2] or 0),
                "columns": cols.get((r[0], r[1])),
            }
            for r in rows
        ]
        return database, items, cols, None
    except Exception as e:
        return database, [], {}, f"query failed: {type(e).__name__}: {e}"
    finally:
        try:
            c.close()
        except Exception:
            pass


def main() -> None:
    started = datetime.datetime.utcnow()
    db_list = [s.strip() for s in os.environ.get("FABRIC_DATABASES", "").split(",") if s.strip()] or DEFAULT_DATABASES
    query_timeout = int(os.environ.get("PER_DB_TIMEOUT_SECONDS", "180"))

    print(f"# scanning {len(db_list)} databases on {os.environ.get('FABRIC_SQL_ENDPOINT')}", flush=True)

    all_items: list[dict] = []
    skipped: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(scan_database, db, query_timeout): db for db in db_list}
        for f in as_completed(futures):
            db, items, _, err = f.result()
            if err:
                skipped.append({"database": db, "error": err})
                print(f"# {db}: SKIP — {err}", flush=True)
                continue
            all_items.extend(items)
            big = sum(1 for i in items if i["rows"] > 1000)
            print(f"# {db}: {len(items)} tables, {big} > 1k", flush=True)

    # Filter to tables with > 1,000 rows; sort descending
    items_over_1k = [i for i in all_items if (i["rows"] or 0) > 1000]
    items_over_1k.sort(key=lambda i: -i["rows"])

    payload = {
        "schema_version": 1,
        "updated_at": started.isoformat() + "Z",
        "snapshot_date": started.date().isoformat(),
        "source": "GitHub Actions weekly scan against Fabric warehouse",
        "duration_seconds": (datetime.datetime.utcnow() - started).total_seconds(),
        "totals": {
            "tables": len(items_over_1k),
            "rows": sum(i["rows"] for i in items_over_1k),
            "tables_total_scanned": len(all_items),
        },
        "skipped_databases": [s["database"] for s in skipped],
        "skipped_detail": skipped,
        "items": items_over_1k,
    }

    out_path = Path(os.environ.get("ROW_COUNTS_OUTPUT", "row-counts.json")).resolve()
    out_path.write_text(json.dumps(payload, indent=2))
    print(
        f"# wrote {out_path} — {len(items_over_1k)} tables, "
        f"{payload['totals']['rows']:,} rows ({payload['duration_seconds']:.1f}s)"
    )


if __name__ == "__main__":
    main()
