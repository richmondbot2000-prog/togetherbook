"""
Decline-reasons analysis — companion to scan_pipeline.py.

Pipeline.json shows WHERE customers drop in the funnel; this scan answers
WHY at each application stage.

Two source streams:

1. **Lead rejections** — Leads.LeadResultTypeId in a 90-day window. Each
   non-purchased lead gets a result code from the LEAD_RESULT_LABELS enum
   (Already claimed, Throttled, Failed scorecard, etc.). We aggregate
   count by code so the page can rank rejection reasons by volume.

2. **Application-stage declines** — Flags rows in the same window with
   FlagTypeId in the "kill" set (Decline / DNL / Cancelled / FraudRisk).
   Each Flag carries a free-text Reason field — we group by FlagTypeId
   plus a normalised version of Reason so we can rank "top reasons" per
   flag type and produce a daily trend line.

Output: `declines.json` at the repo root.

Required env vars (same as scan_pipeline.py):
  FABRIC_SQL_ENDPOINT, FABRIC_TENANT_ID, FABRIC_CLIENT_ID, FABRIC_CLIENT_SECRET

Optional:
  DECLINE_WINDOW_DAYS  rolling window (default 90)
  DECLINE_LENDER_ID    LenderId to score (default 6 = Transform Credit)
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pyodbc

LENDER_ID = int(os.environ.get("DECLINE_LENDER_ID", "6"))
LENDER_LABEL = "Transform Credit (LenderId 6, USA)" if LENDER_ID == 6 else f"LenderId {LENDER_ID}"
WINDOW_DAYS = int(os.environ.get("DECLINE_WINDOW_DAYS", "90"))
QUERY_TIMEOUT = 600

# Lead-result enum copied from scan_pipeline.py for consistency.
LEAD_RESULT_LABELS = {
    -1: "Source excluded", 1: "Accepted (purchased)", 2: "Already claimed",
    3:  "Duplicate", 4:  "Throttled", 5:  "Invalid campaign",
    6:  "Invalid email", 7:  "Invalid phone", 8:  "Invalid name",
    9:  "Invalid loan purpose", 10: "Invalid language", 11: "No government ID",
    12: "No valid product", 13: "Existing loan", 14: "Ineligible credit-builder",
    15: "Invalid state", 16: "Blacklisted", 17: "Premium scorecard failed",
    18: "Seen too frequently", 19: "Settled loan", 20: "Rejected for lead score",
    21: "Rejected with counteroffer", 22: "Counteroffer rejected",
    23: "Bank account not validated", 24: "Invalid employment type",
    25: "Invalid pay frequency", 26: "Invalid pay type",
    27: "Invalid bank account type", 30: "Pre-check passed",
    98: "Unclaimed (CPC steal)", 99: "Discarded",
}
# These two = "purchased" → not a decline.
NON_DECLINE_RESULT_IDS = (1, 30)

# Flag types from the wiki §3.11 — the "kill" set we treat as declines.
FLAG_TYPE_LABELS = {
    1:  "Training",
    2:  "Decline",
    3:  "DNL",
    4:  "Cancelled",
    5:  "Complaint",
    6:  "FraudRisk",
    7:  "PushBack",
}
DECLINE_FLAG_TYPE_IDS = (2, 3, 4, 6)   # Decline, DNL, Cancelled, FraudRisk

# Number of reasons to keep per flag-type (after grouping).
TOP_REASONS_PER_TYPE = 20


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


def discover_columns(cur, table: str, schema: str = "dbo") -> set[str]:
    cur.execute(
        """
        SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        """,
        [schema, table],
    )
    return {r[0] for r in cur.fetchall()}


def pick(cols: set[str], *candidates: str) -> str | None:
    return next((c for c in candidates if c in cols), None)


# Free-text Reason normaliser. Real-world decline-reason strings vary in case,
# punctuation and trailing whitespace — we lowercase, trim, collapse runs of
# whitespace, and clip to a sensible length so 'BANK CHECK FAILED' and
# 'bank check failed.' fold into the same bucket.
_WS = re.compile(r"\s+")
def normalise_reason(r: str | None) -> str:
    if r is None:
        return ""
    s = _WS.sub(" ", str(r).strip())
    if not s:
        return ""
    # Strip a trailing period/full-stop that's a common artefact.
    if s.endswith("."):
        s = s[:-1].rstrip()
    return s[:160]


def main() -> None:
    started = datetime.datetime.now(datetime.timezone.utc)
    window_end = started
    window_start = started - datetime.timedelta(days=WINDOW_DAYS)
    print(
        f"# scan_declines start {started.isoformat()}  "
        f"window: {window_start.date()} → {window_end.date()} ({WINDOW_DAYS}d)  "
        f"lender: {LENDER_ID}",
        flush=True,
    )

    conn = pyodbc.connect(conn_str("ReportingApplications"), timeout=20)
    conn.timeout = QUERY_TIMEOUT
    cur = conn.cursor()

    # ─── Discover columns ─────────────────────────────────────────────
    flags_cols = discover_columns(cur, "Flags")
    leads_cols = discover_columns(cur, "Leads")
    apps_cols = discover_columns(cur, "Applications")

    print(f"# Flags ALL cols: {sorted(flags_cols)}", flush=True)

    f_aref = pick(flags_cols, "ARef")
    f_type = pick(flags_cols, "FlagTypeId")
    f_reason = pick(flags_cols, "Reason", "Description", "Note", "Comment")
    f_added = pick(flags_cols, "DateAddedUtc", "DateAddedUTC", "DateCreatedUtc")
    f_removed = pick(flags_cols, "DateRemovedUtc", "DateRemovedUTC")
    f_lender = pick(flags_cols, "LenderId")
    f_client = pick(flags_cols, "ClientType")
    f_user = pick(flags_cols, "ClientUsername", "ClientUserName")

    apps_aref = pick(apps_cols, "ARef")
    apps_lender = pick(apps_cols, "LenderId")

    leads_lender = pick(leads_cols, "LenderId")
    leads_date = pick(leads_cols, "DateReceivedUtc", "DateCreatedUtc")
    leads_result = pick(leads_cols, "LeadResultTypeId", "LeadResultId")

    print(
        f"# Flags cols: aref={f_aref} type={f_type} reason={f_reason} added={f_added} "
        f"removed={f_removed} lender={f_lender} client={f_client}",
        flush=True,
    )
    print(
        f"# Leads cols: lender={leads_lender} date={leads_date} result={leads_result}",
        flush=True,
    )

    # The warehouse Flags table doesn't always carry LenderId — fall back to
    # joining through Applications on ARef so we can still filter to our
    # lender of interest.
    if not (f_type and f_added):
        print("# Flags table missing required columns (type/added); skipping flag-decline section.", flush=True)
        f_aref = f_type = f_reason = f_added = None

    # ─── Q1: Flag-based declines ───────────────────────────────────────
    flag_buckets: dict[int, dict] = {}   # flag_type_id → {reasons: Counter, daily: Counter, total: int}
    if f_type and f_added:
        decline_ids = ",".join(str(t) for t in DECLINE_FLAG_TYPE_IDS)
        sel_extra = ", ".join([
            f"f.[{f_reason}]"  if f_reason  else "NULL",
            f"f.[{f_client}]"  if f_client  else "NULL",
            f"f.[{f_user}]"    if f_user    else "NULL",
        ])
        # Lender filter: prefer the Flags row's own LenderId; fall back to
        # joining through Applications on ARef if Flags doesn't carry it.
        if f_lender:
            sql = f"""
                SELECT f.[{f_type}], CAST(f.[{f_added}] AS date) AS d, {sel_extra}
                FROM dbo.Flags f
                WHERE f.[{f_added}] >= ? AND f.[{f_added}] < ?
                  AND f.[{f_lender}] = ?
                  AND f.[{f_type}] IN ({decline_ids})
            """
            params = [window_start, window_end, LENDER_ID]
        elif f_aref and apps_aref and apps_lender:
            print(f"# Flags.LenderId not present — joining through Applications.{apps_lender}", flush=True)
            sql = f"""
                SELECT f.[{f_type}], CAST(f.[{f_added}] AS date) AS d, {sel_extra}
                FROM dbo.Flags f
                INNER JOIN dbo.Applications a ON a.[{apps_aref}] = f.[{f_aref}]
                WHERE f.[{f_added}] >= ? AND f.[{f_added}] < ?
                  AND a.[{apps_lender}] = ?
                  AND f.[{f_type}] IN ({decline_ids})
            """
            params = [window_start, window_end, LENDER_ID]
        else:
            print("# Cannot filter Flags by lender — pulling all rows for FlagTypeIds in window", flush=True)
            sql = f"""
                SELECT f.[{f_type}], CAST(f.[{f_added}] AS date) AS d, {sel_extra}
                FROM dbo.Flags f
                WHERE f.[{f_added}] >= ? AND f.[{f_added}] < ?
                  AND f.[{f_type}] IN ({decline_ids})
            """
            params = [window_start, window_end]
        print("# Q1: pulling decline flags…", flush=True)
        cur.execute(sql, params)

        per_user_counter: Counter = Counter()  # ClientUsername → flag count (top
                                                # decliners)
        for row in cur.fetchall():
            ftype, d, reason, client, user = row
            ftype = int(ftype)
            slot = flag_buckets.setdefault(ftype, {
                "flag_type_id": ftype,
                "label":        FLAG_TYPE_LABELS.get(ftype, f"FlagType {ftype}"),
                "total":        0,
                "reasons":      Counter(),
                "daily":        Counter(),
                "by_client":    Counter(),
            })
            slot["total"] += 1
            slot["reasons"][normalise_reason(reason)] += 1
            if d:
                slot["daily"][str(d)] += 1
            if client:
                slot["by_client"][str(client).strip()] += 1
            if user:
                per_user_counter[str(user).strip().lower()] += 1

        print(
            f"#   pulled flag-declines for {len(flag_buckets)} types  total {sum(s['total'] for s in flag_buckets.values()):,}",
            flush=True,
        )
        # Per-user top decliners — useful to spot operator patterns.
        top_decliners = per_user_counter.most_common(20)
    else:
        top_decliners = []

    # ─── Q2: Lead-result breakdown ────────────────────────────────────
    lead_counter: Counter = Counter()
    lead_daily: dict[int, Counter] = defaultdict(Counter)
    leads_presented_total = 0
    leads_purchased_total = 0
    if leads_lender and leads_date and leads_result:
        print("# Q2: pulling lead rejections (LeadResultTypeId)…", flush=True)
        cur.execute(
            f"""
            SELECT [{leads_result}], CAST([{leads_date}] AS date) AS d, COUNT(*)
            FROM dbo.Leads
            WHERE [{leads_date}] >= ? AND [{leads_date}] < ?
              AND [{leads_lender}] = ?
            GROUP BY [{leads_result}], CAST([{leads_date}] AS date)
            """,
            [window_start, window_end, LENDER_ID],
        )
        for rtype, d, n in cur.fetchall():
            if rtype is None: continue
            rtype = int(rtype)
            n = int(n)
            leads_presented_total += n
            if rtype in NON_DECLINE_RESULT_IDS:
                leads_purchased_total += n
            else:
                lead_counter[rtype] += n
                if d:
                    lead_daily[rtype][str(d)] += n
        print(
            f"#   leads presented: {leads_presented_total:,}  purchased: {leads_purchased_total:,}  "
            f"declined: {sum(lead_counter.values()):,}",
            flush=True,
        )

    conn.close()

    # ─── Assemble JSON ────────────────────────────────────────────────
    # Per-flag-type: top N reasons + the daily trend line.
    flag_payload = []
    for ftype, slot in sorted(flag_buckets.items()):
        top_reasons = [
            {"reason": r or "(no reason given)", "count": n}
            for r, n in slot["reasons"].most_common(TOP_REASONS_PER_TYPE)
        ]
        # Sort daily into ascending date order.
        daily = sorted(({"date": d, "count": n} for d, n in slot["daily"].items()),
                       key=lambda x: x["date"])
        by_client = [{"client": c, "count": n} for c, n in slot["by_client"].most_common(10)]
        flag_payload.append({
            "flag_type_id": slot["flag_type_id"],
            "label":        slot["label"],
            "total":        slot["total"],
            "top_reasons":  top_reasons,
            "daily":        daily,
            "by_client":    by_client,
        })

    lead_payload = []
    for rtype, n in lead_counter.most_common():
        daily = sorted(({"date": d, "count": c} for d, c in lead_daily[rtype].items()),
                       key=lambda x: x["date"])
        lead_payload.append({
            "result_type_id": rtype,
            "label":          LEAD_RESULT_LABELS.get(rtype, f"code {rtype}"),
            "count":          n,
            "share":          n / leads_presented_total if leads_presented_total else None,
            "daily":          daily,
        })

    output = {
        "snapshot_at":     started.isoformat(),
        "snapshot_date":   started.date().isoformat(),
        "lender_id":       LENDER_ID,
        "lender_label":    LENDER_LABEL,
        "window_days":     WINDOW_DAYS,
        "window_start":    window_start.date().isoformat(),
        "window_end":      window_end.date().isoformat(),
        "leads": {
            "presented":  leads_presented_total,
            "purchased":  leads_purchased_total,
            "rejected":   sum(lead_counter.values()),
            "results":    lead_payload,
        },
        "flag_declines": {
            "total":      sum(s["total"] for s in flag_buckets.values()) if flag_buckets else 0,
            "types":      flag_payload,
            "top_decliners": [
                {"username": u, "count": n} for u, n in top_decliners
            ],
        },
    }
    out_path = Path("declines.json")
    out_path.write_text(json.dumps(output, indent=2, default=str))
    print(
        f"# wrote {out_path} ({out_path.stat().st_size:,} bytes)",
        flush=True,
    )


if __name__ == "__main__":
    main()
