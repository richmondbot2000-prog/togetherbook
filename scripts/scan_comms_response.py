"""
Generate comms.json — daily response-time stats for customer-initiated
messages, split into 4 buckets by the customer's state at the moment they
sent the message:

  - unknown    : sender has no ARef on the inbound row
  - applicant  : ARef set; no live loan (balance > $10); no signed-not-rejected
                 guarantor on the application at message time
  - live_loan  : LoanbookId tracks to a loan with CurrentBalance > $10 AND no
                 arrears (Arrears = 0 and DateInArrearsLocal IS NULL) at the
                 most recent LoanHistory snapshot before the message timestamp
  - arrears    : LoanbookId tracks to a loan with CurrentBalance > $10 AND
                 either Arrears > 0 or DateInArrearsLocal IS NOT NULL at the
                 most recent LoanHistory snapshot before the message timestamp

For each inbound message, the "response" is the FIRST outbound message in the
same channel (SMS->SMS, Email->Email) to the same ExternalAddress that was
sent *after* the inbound and within 14 days, EXCLUDING Message Factory sends.
We capture TWO reply variants per message so the page can toggle Reply Robot
in or out without re-querying:

  - reply_all      : any reply, including Robot Responder ("Reply Robot")
  - reply_human    : human-agent only — also excludes ClientType LIKE
                     '%Responder%' alongside MessageFactory

Anything beyond 14 days is treated as 'no reply within 14 days'; the page
caps these to 14 days when the "include no-reply" filter is on.

Required env vars: FABRIC_SQL_ENDPOINT, FABRIC_TENANT_ID, FABRIC_CLIENT_ID,
FABRIC_CLIENT_SECRET.

The three reporting databases live in Fabric as separate items so each is
queried via its own connection; loan state + signed-GT info is joined on
the Python side because Fabric warehouse items can't cross-join with the
ServicePrincipal auth flow we use elsewhere.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import pyodbc


YEAR = 2026
OUTPUT_PATH = Path("comms.json")
MAX_REPLY_MINUTES = 14 * 24 * 60   # 14 days
ARREARS_FLAG_TYPES = (2, 3, 4, 6)  # Decline, DNL, Cancelled, FraudRisk


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
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=15;"
        "Authentication=ActiveDirectoryServicePrincipal;"
        f"UID={env('FABRIC_CLIENT_ID')};"
        f"PWD={env('FABRIC_CLIENT_SECRET')};"
    )


# Step 1: inbound + paired-outbound, all inside ReportingCommunications.
# Returns one row per inbound — small enough to bring back to Python.
COMMS_QUERY = f"""
DECLARE @from datetime2 = '{YEAR}-01-01';
DECLARE @to   datetime2 = '{YEAR+1}-01-01';
DECLARE @maxMinutes int = {MAX_REPLY_MINUTES};

-- Warehouse-actual schema (NOT the wiki's logical view):
--   dbo.Messages.Description is an INT enum: 0=SMS-in, 1=Email-in, 2=Call-in,
--   3+ = outbound (different subtype per channel). UTCTime is the single
--   timestamp column (no separate Received/Sent). Channel of outbound is
--   inferred by ExternalAddress shape — same address used for inbound +
--   outbound, so pairing by ExternalAddress is implicitly channel-equivalent.
WITH inbound AS (
    SELECT
        m.MessageId,
        m.ARef,
        m.LoanbookId,
        m.ExternalAddress,
        m.UTCTime,
        CASE
            WHEN m.Description = 0 THEN 'SMS'
            WHEN m.Description = 1 THEN 'Email'
        END AS Channel
    FROM dbo.Messages m
    WHERE m.Description IN (0, 1)
      AND m.UTCTime >= @from
      AND m.UTCTime <  @to
      AND m.ExternalAddress IS NOT NULL
      AND m.ExternalAddress <> ''
)
SELECT
    i.MessageId,
    i.ARef,
    i.LoanbookId,
    i.UTCTime AS DateReceivedUtc,
    i.Channel,
    -- Minutes to first non-MessageFactory reply within 14 days
    (
        SELECT TOP 1 DATEDIFF(MINUTE, i.UTCTime, o.UTCTime)
        FROM dbo.Messages o
        WHERE o.ExternalAddress = i.ExternalAddress
          AND o.Description >= 3
          AND o.UTCTime >  i.UTCTime
          AND o.UTCTime <= DATEADD(MINUTE, @maxMinutes, i.UTCTime)
          AND (o.ClientType IS NULL OR o.ClientType <> 'MessageFactory')
        ORDER BY o.UTCTime ASC
    ) AS ReplyMinAll,
    -- Same but additionally excluding Reply Robot (ClientType LIKE '%Responder%')
    (
        SELECT TOP 1 DATEDIFF(MINUTE, i.UTCTime, o.UTCTime)
        FROM dbo.Messages o
        WHERE o.ExternalAddress = i.ExternalAddress
          AND o.Description >= 3
          AND o.UTCTime >  i.UTCTime
          AND o.UTCTime <= DATEADD(MINUTE, @maxMinutes, i.UTCTime)
          AND (
              o.ClientType IS NULL
              OR (
                  o.ClientType <> 'MessageFactory'
                  AND o.ClientType NOT LIKE '%Responder%'
              )
          )
        ORDER BY o.UTCTime ASC
    ) AS ReplyMinHuman
FROM inbound i;
"""


def fetch_inbound_paired():
    print("[comms] connecting + fetching inbound + paired outbound…", flush=True)
    cn = pyodbc.connect(conn_str("ReportingCommunications"), timeout=30)
    try:
        cur = cn.cursor()
        cur.execute(COMMS_QUERY)
        rows = cur.fetchall()
    finally:
        cn.close()
    out = []
    for r in rows:
        out.append({
            "MessageId":       r[0],
            "ARef":            (r[1] or "").strip() if r[1] else "",
            "LoanbookId":      (r[2] or "").strip() if r[2] else "",
            "DateReceivedUtc": r[3],
            "Channel":         r[4],
            "ReplyMinAll":     r[5],
            "ReplyMinHuman":   r[6],
        })
    print(f"[comms] {len(out)} inbound rows", flush=True)
    return out


def chunked(seq, n):
    """Yield n-sized chunks from seq."""
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def pick_column(cur, schema: str, table: str, *candidates: str) -> str | None:
    """Return the first column name from `candidates` that exists on the
    given table (case-insensitive). The Fabric warehouse has multiple
    schema vintages; column names drift (e.g. `DateTimeUtc` vs `DateTUtc`).
    """
    cur.execute(
        """
        SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        """,
        (schema, table),
    )
    cols = {r[0].lower(): r[0] for r in cur.fetchall()}
    for c in candidates:
        if c.lower() in cols:
            return cols[c.lower()]
    return None


def fetch_loan_history(loanbook_ids: set[str]) -> dict[str, list]:
    """For each LoanbookId observed, fetch all Loan_History snapshots so the
    Python code can find the latest one ≤ each message's timestamp.

    Columns are discovered via INFORMATION_SCHEMA.COLUMNS because the
    warehouse has multiple schema vintages. Loan_History is huge so we chunk
    the LoanbookId list and accumulate rows."""
    if not loanbook_ids:
        return {}
    print(f"[loanbook] fetching Loan_History for {len(loanbook_ids)} loans…", flush=True)
    cn = pyodbc.connect(conn_str("ReportingLoanbook"), timeout=30)
    try:
        cur = cn.cursor()
        ts_col = pick_column(cur, "dbo", "Loan_History",
                             "DateTimeUtc", "DateTUtc", "DateTimeUTC")
        bal_col = pick_column(cur, "dbo", "Loan_History",
                              "CurrentBalance", "Balance", "OutstandingBalance")
        arr_col = pick_column(cur, "dbo", "Loan_History",
                              "Arrears", "TotalInArrears", "CurrentArrears")
        dia_col = pick_column(cur, "dbo", "Loan_History",
                              "DateInArrearsLocal", "DateInArrearsUTC", "DateInArrearsUtc")
        if not (ts_col and bal_col):
            sys.exit(f"error: couldn't find required columns on Loan_History "
                     f"(ts={ts_col}, balance={bal_col})")
        arr_sql = f"[{arr_col}]" if arr_col else "NULL"
        dia_sql = f"[{dia_col}]" if dia_col else "NULL"

        history: dict[str, list] = defaultdict(list)
        for chunk in chunked(loanbook_ids, 1500):
            ph = ",".join("?" * len(chunk))
            cur.execute(
                f"""
                SELECT LoanbookId, [{ts_col}], [{bal_col}], {arr_sql}, {dia_sql}
                FROM dbo.Loan_History
                WHERE LoanbookId IN ({ph})
                """,
                list(chunk),
            )
            for lb, dt, bal, arr, dia in cur.fetchall():
                history[lb].append((dt, float(bal or 0.0), float(arr or 0.0), dia))

        # Sort each loan's history by timestamp so loan_state_at can scan
        # forward.
        for lb in history:
            history[lb].sort(key=lambda r: r[0])
    finally:
        cn.close()
    total = sum(len(v) for v in history.values())
    print(f"[loanbook] {total} history rows across {len(history)} loans", flush=True)
    return history


def fetch_signed_gt(arefs: set[str]) -> set[str]:
    """Return the set of ARefs for which a guarantor was signed (ESignatures
    with GtRef NOT NULL) and not currently rejected (no active Decline / DNL /
    Cancelled / FraudRisk flag). Point-in-time accuracy is approximated to
    'now' — see module docstring for a v1 trade-off note.
    """
    if not arefs:
        return set()
    print(f"[apps] checking signed-GT status for {len(arefs)} ARefs…", flush=True)
    cn = pyodbc.connect(conn_str("ReportingApplications"), timeout=30)
    try:
        cur = cn.cursor()
        # Discover the relevant columns on dbo.Applications + dbo.ESignatures.
        # Warehouse vintages drift; in particular dbo.Applications.GtEsignatureId
        # is not always present. When the join column is missing we degrade
        # gracefully — every ARef just stays in 'applicant' for the bucket
        # classifier and v1 still works.
        gt_es_col = pick_column(cur, "dbo", "Applications",
                                "GtEsignatureId", "GtESignatureId", "GTEsignatureId")
        gt_ref_col = pick_column(cur, "dbo", "Applications", "GtRef")
        es_id_col = pick_column(cur, "dbo", "ESignatures", "EsignatureId", "ESignatureId")
        es_signed_col = pick_column(cur, "dbo", "ESignatures",
                                    "DateSignedUtc", "DateSignedUTC", "SignedDateUtc")
        if not (gt_es_col and gt_ref_col and es_id_col and es_signed_col):
            print(f"  signed-GT columns not present "
                  f"(gt_es={gt_es_col}, gt_ref={gt_ref_col}, "
                  f"es_id={es_id_col}, es_signed={es_signed_col}); "
                  f"skipping the check — every ARef will fall back to 'applicant'",
                  flush=True)
            return set()
        flag_list = ",".join(str(f) for f in ARREARS_FLAG_TYPES)
        result: set[str] = set()
        for chunk in chunked(arefs, 1500):
            ph = ",".join("?" * len(chunk))
            cur.execute(
                f"""
                SELECT DISTINCT a.ARef
                FROM dbo.Applications a
                JOIN dbo.ESignatures e
                  ON e.[{es_id_col}] = a.[{gt_es_col}]
                WHERE a.ARef IN ({ph})
                  AND a.[{gt_ref_col}] IS NOT NULL
                  AND a.[{gt_es_col}] IS NOT NULL
                  AND e.[{es_signed_col}] IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM dbo.Flags f
                      WHERE f.ARef = a.ARef
                        AND f.GtRef = a.[{gt_ref_col}]
                        AND f.FlagTypeId IN ({flag_list})
                        AND f.DateRemovedUtc IS NULL
                  );
                """,
                list(chunk),
            )
            for r in cur.fetchall():
                if r[0]: result.add(r[0].strip())
    finally:
        cn.close()
    print(f"[apps] {len(result)} ARefs have a signed-not-rejected guarantor", flush=True)
    return result


def loan_state_at(history: list, ts: datetime.datetime) -> tuple[float, float, object] | None:
    """Find the latest LoanHistory snapshot at or before `ts`. Returns
    (CurrentBalance, Arrears, DateInArrearsLocal) or None if no prior row."""
    if not history:
        return None
    # history is sorted by DateTimeUtc ascending — binary search would be
    # faster, but linear from the end is plenty for ~365 rows/loan.
    chosen = None
    for row in history:
        if row[0] <= ts:
            chosen = row
        else:
            break
    if not chosen:
        return None
    return chosen[1], chosen[2], chosen[3]


def classify(inbound, loan_history, signed_gt_arefs) -> str:
    aref = inbound["ARef"]
    if not aref:
        return "unknown"
    lb = inbound["LoanbookId"]
    if lb:
        snap = loan_state_at(loan_history.get(lb) or [], inbound["DateReceivedUtc"])
        if snap is not None:
            bal, arr, dia = snap
            if bal > 10 and (arr > 0 or dia is not None):
                return "arrears"
            if bal > 10:
                return "live_loan"
    # ARef but no live loan
    if aref in signed_gt_arefs:
        return "other"  # ARef + signed GT but no live loan — excluded
    return "applicant"


def main() -> None:
    inbounds = fetch_inbound_paired()

    loanbook_ids = {i["LoanbookId"] for i in inbounds if i["LoanbookId"]}
    arefs = {i["ARef"] for i in inbounds if i["ARef"]}
    loan_history = fetch_loan_history(loanbook_ids)
    signed_gt = fetch_signed_gt(arefs)

    # Aggregate by (Day, Bucket)
    BUCKETS = ["unknown", "applicant", "live_loan", "arrears", "other"]
    agg: dict[tuple[str, str], dict] = defaultdict(lambda: {
        "n_total":         0,
        "n_reply_all":     0,
        "sum_reply_all":   0.0,
        "n_reply_human":   0,
        "sum_reply_human": 0.0,
    })

    for i in inbounds:
        bucket = classify(i, loan_history, signed_gt)
        day = i["DateReceivedUtc"].strftime("%Y-%m-%d")
        cell = agg[(day, bucket)]
        cell["n_total"] += 1
        if i["ReplyMinAll"] is not None:
            cell["n_reply_all"] += 1
            cell["sum_reply_all"] += float(i["ReplyMinAll"])
        if i["ReplyMinHuman"] is not None:
            cell["n_reply_human"] += 1
            cell["sum_reply_human"] += float(i["ReplyMinHuman"])

    # Pivot to per-bucket series
    series: dict[str, dict[str, dict]] = {b: {} for b in BUCKETS}
    for (day, bucket), cell in agg.items():
        if bucket not in series:
            bucket = "other"
        series[bucket][day] = {
            "n_total":         cell["n_total"],
            "n_reply_all":     cell["n_reply_all"],
            "sum_reply_all":   round(cell["sum_reply_all"], 1),
            "n_reply_human":   cell["n_reply_human"],
            "sum_reply_human": round(cell["sum_reply_human"], 1),
        }

    totals = {
        b: {
            "n_total":       sum(v["n_total"] for v in days.values()),
            "n_reply_all":   sum(v["n_reply_all"] for v in days.values()),
            "n_reply_human": sum(v["n_reply_human"] for v in days.values()),
        }
        for b, days in series.items()
    }

    out = {
        "schema_version":    1,
        "updated_at":        datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "year":              YEAR,
        "channels":          ["SMS", "Email"],
        "buckets":           ["unknown", "applicant", "live_loan", "arrears"],
        "max_reply_minutes": MAX_REPLY_MINUTES,
        "totals_by_bucket":  totals,
        "series":            series,
    }

    OUTPUT_PATH.write_text(
        json.dumps(out, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    summary = ", ".join(
        f"{b}: total={totals[b]['n_total']} (all-reply={totals[b]['n_reply_all']}, human-reply={totals[b]['n_reply_human']})"
        for b in out["buckets"]
    )
    print(f"wrote {OUTPUT_PATH}: {summary}", flush=True)


if __name__ == "__main__":
    main()
