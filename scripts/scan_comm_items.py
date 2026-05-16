"""Pull per-message detail from ReportingCommunications.dbo.Messages
for the requested window and upload directly to Cloudflare D1's
`activity_items` table.

This is the drill-down counterpart to scan_staff_activity_buckets.py.
The buckets scanner aggregates writes per 15-min cell; THIS scanner
captures the per-row detail (first 500 chars of MessageBody,
ComType, ClientType, ClientUsername, CampaignName, AutoProcessed)
so the Holidays/Activity page can let a manager click a slot and
see what was actually sent.

Schema mapping (verified against the wiki + prior probes):
    Description (INT)
        0 InboundSMS  1 InboundEmail  2 InboundCall
        5 OutboundSMS 6 OutboundEmail 7 OutboundCall
    Staff agent       ClientUsername  (bare local-part for CRM)
    Client identifier ExternalAddress (phone or email)
    Body              MessageBody
    ClientType        ClientType
    Campaign          CampaignName
    AutoProcessed     AutoProcessed   (BIT, nullable)
    Timestamp         UTCTime

Window logic mirrors the buckets scanner: ACTIVITY_START_DATE /
ACTIVITY_END_DATE env vars (default = yesterday UTC), inclusive.

Env vars required:
    FABRIC_SQL_ENDPOINT, FABRIC_TENANT_ID, FABRIC_CLIENT_ID,
    FABRIC_CLIENT_SECRET
    CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, D1_ACTIVITY_DB_ID
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pyodbc

# Reuse the domain-variant + conn_str helpers from the buckets scanner
# so the two stay in lockstep.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_staff_activity_buckets import (  # type: ignore
    domain_local_variants,
    conn_str,
    parse_date,
)

DB = "ReportingCommunications"
TBL = "[dbo].[Messages]"
SRC = "communications.Messages"

DESC_LABEL = {
    0: "SMS",   1: "Email", 2: "Call",
    5: "SMS",   6: "Email", 7: "Call",
}
DESC_DIR = {
    0: "in",  1: "in",  2: "in",
    5: "out", 6: "out", 7: "out",
}

D1_VARS_CAP = 100


def d1_query(account, token, db_id, sql, params=None):
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{db_id}/query"
    body = json.dumps({"sql": sql, "params": params or []}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]


def main() -> None:
    started = datetime.datetime.now(datetime.timezone.utc)
    today = started.date()
    yesterday = today - datetime.timedelta(days=1)

    start = parse_date(os.environ.get("ACTIVITY_START_DATE")) or datetime.datetime(
        yesterday.year, yesterday.month, yesterday.day, tzinfo=datetime.timezone.utc)
    end_inclusive = parse_date(os.environ.get("ACTIVITY_END_DATE")) or datetime.datetime(
        yesterday.year, yesterday.month, yesterday.day, tzinfo=datetime.timezone.utc)
    end_exclusive = end_inclusive + datetime.timedelta(days=1)
    start_str = start.strftime("%Y-%m-%d %H:%M:%S")
    end_str   = end_exclusive.strftime("%Y-%m-%d %H:%M:%S")

    print(f"# scan_comm_items: {start.date()} → {end_inclusive.date()} (inclusive)", flush=True)

    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID") or sys.exit("CLOUDFLARE_ACCOUNT_ID missing")
    token   = os.environ.get("CLOUDFLARE_API_TOKEN")  or sys.exit("CLOUDFLARE_API_TOKEN missing")
    db_id   = os.environ.get("D1_ACTIVITY_DB_ID")     or sys.exit("D1_ACTIVITY_DB_ID missing")

    staff_path = Path("staff.json")
    if not staff_path.exists():
        sys.exit("staff.json missing — cannot map ClientUsernames to staff")
    staff = json.loads(staff_path.read_text())
    # Build a reverse map: any-variant → workspace_email
    un_to_email = {}
    for u in staff.get("users", []):
        email = (u.get("email") or "").lower()
        if not email:
            continue
        for un in domain_local_variants(email):
            un_to_email[un] = email
    print(f"# {len(un_to_email):,} username variants for {len(staff.get('users') or []):,} staff", flush=True)

    print(f"# connecting to {DB} …", flush=True)
    c = pyodbc.connect(conn_str(DB), timeout=20)
    c.timeout = 240
    cur = c.cursor()

    # Pull the columns directly. Inbound and outbound — agent identifier
    # only populates on Description IN (5,6,7) for outbound; for inbound
    # we don't have a staff binding so they won't roll up. We still
    # query all six so any future agent-on-inbound table change works.
    q = f"""
      SELECT LOWER(ClientUsername) AS un,
             CAST(UTCTime AS DATE) AS dt,
             (DATEPART(HOUR, UTCTime)*4 + DATEPART(MINUTE, UTCTime)/15) AS bucket,
             CONVERT(VARCHAR(33), UTCTime, 126) AS occurred_at,
             CAST([MessageId] AS NVARCHAR(64)) AS record_id,
             [Description] AS desc_enum,
             ClientType,
             ExternalAddress,
             CampaignName,
             AutoProcessed,
             LEFT(ISNULL(MessageBody, ''), 500) AS body_excerpt
      FROM {DB}.{TBL}
      WHERE UTCTime >= ? AND UTCTime < ?
        AND ClientUsername IS NOT NULL
        AND (ClientUsername LIKE '%@%' OR ClientUsername LIKE '%.%')
        AND [Description] IN (5, 6, 7)
    """
    print(f"# querying Messages …", flush=True)
    t0 = time.time()
    cur.execute(q, [start_str, end_str])
    rows = cur.fetchall()
    print(f"# {len(rows):,} row(s) in {time.time()-t0:.1f}s", flush=True)
    try: c.close()
    except: pass

    # Roll each row to a staff Workspace email; drop rows we can't map.
    items = []
    skipped = 0
    for un, dt, bucket, occurred_at, record_id, desc_enum, client_type, external, campaign, auto, body in rows:
        email = un_to_email.get((un or "").lower())
        if not email:
            skipped += 1
            continue
        iso = dt.isoformat() if dt else None
        if not iso or bucket is None:
            continue
        comm_type = DESC_LABEL.get(int(desc_enum or -1)) or ""
        direction = DESC_DIR.get(int(desc_enum or -1)) or ""
        items.append((
            email, iso, int(bucket), SRC,
            occurred_at or "", str(record_id),
            f"comm.{direction}",      # kind, e.g. comm.out / comm.in
            comm_type,                # SMS/Email/Call
            (client_type or None),
            (external or None),       # client_username column (we surface ExternalAddress)
            (campaign or None),
            (1 if auto else 0) if auto is not None else None,
            body or None,
        ))
    print(f"# mapped {len(items):,} item(s) ({skipped:,} skipped — no staff match)", flush=True)

    # Step 1 — wipe existing items for the window. One DELETE per
    # iso_date so the request stays well under D1's row scan cost.
    iso_dates = []
    d = start.date()
    while d <= end_inclusive.date():
        iso_dates.append(d.isoformat())
        d += datetime.timedelta(days=1)

    print(f"# wiping existing comm items for {len(iso_dates)} day(s) …", flush=True)
    for iso in iso_dates:
        out = d1_query(account, token, db_id,
                       "DELETE FROM activity_items WHERE iso_date = ? AND src = ?",
                       [iso, SRC])
        if not out.get("success"):
            print(f"  ! delete failed for {iso}: {out.get('errors')}", flush=True)
            sys.exit(1)

    if not items:
        print("# nothing to insert.", flush=True)
        return

    # Step 2 — batched INSERT OR REPLACE, parallel over the batches.
    # D1 caps each statement at 100 SQL variables (so ~7 rows / call for
    # the 13-column items), and the round-trip is ~30/s serial — the
    # 15-min job timeout was hit at ~21k rows in the prior run. Eight
    # concurrent workers cut that to ~3 min for the same payload while
    # staying well under any sensible RPS ceiling on D1.
    cols = 13
    batch_size = max(1, (D1_VARS_CAP - 2) // cols)
    sql_head = (
        "INSERT OR REPLACE INTO activity_items "
        "(email, iso_date, bucket, src, occurred_at, record_id, kind, "
        " comm_type, client_type, client_username, campaign_name, "
        " auto_processed, body_excerpt) VALUES "
    )
    batches = list(chunked(items, batch_size))
    print(f"# {len(batches):,} batches × {batch_size} rows; 8-way parallel", flush=True)
    t0 = time.time()
    inserted = 0
    failed = []
    progress_every = max(1, len(batches) // 20)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {}
        for idx, chunk in enumerate(batches):
            placeholders = ",".join(["(" + ",".join(["?"] * cols) + ")"] * len(chunk))
            params = [v for row in chunk for v in row]
            fut = ex.submit(d1_query, account, token, db_id, sql_head + placeholders, params)
            futures[fut] = (idx, len(chunk))
        done = 0
        for fut in as_completed(futures):
            idx, n = futures[fut]
            out = fut.result()
            if not out.get("success"):
                failed.append((idx, out.get("errors") or out))
            else:
                inserted += n
            done += 1
            if done % progress_every == 0:
                elapsed = time.time() - t0
                rate = inserted / max(elapsed, 0.001)
                print(f"    {done:,}/{len(batches):,} batches · {inserted:,}/{len(items):,} rows ({rate:,.0f}/s)", flush=True)
    elapsed = time.time() - t0
    if failed:
        print(f"# {len(failed):,} batches FAILED — first error:")
        print(json.dumps(failed[0][1], indent=2)[:1500])
    print(f"# inserted {inserted:,}/{len(items):,} comm item(s) in {elapsed:.1f}s", flush=True)
    if failed and not inserted:
        sys.exit(1)


if __name__ == "__main__":
    main()
