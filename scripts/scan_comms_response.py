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
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

import pyodbc


YEAR = 2026
OUTPUT_PATH = Path("comms.json")
MAX_REPLY_MINUTES = 14 * 24 * 60   # 14 days


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


# The whole pipeline runs in one CTE-stacked query so we never pull message
# bodies to the Python runner; only ~(365 * 4) (day, bucket, …) rows come back.
# Bucket assignment is point-in-time using LoanHistory snapshots and a NOT
# EXISTS check for signed-not-rejected guarantors.
QUERY = f"""
DECLARE @from datetime2 = '{YEAR}-01-01';
DECLARE @to   datetime2 = '{YEAR+1}-01-01';
DECLARE @maxMinutes int = {MAX_REPLY_MINUTES};

WITH inbound AS (
    SELECT
        m.MessageId,
        m.ARef,
        m.LoanbookId,
        m.ExternalAddress,
        m.DateReceivedUtc,
        CASE
            WHEN m.Description = 'InboundSMS'   THEN 'SMS'
            WHEN m.Description = 'InboundEmail' THEN 'Email'
        END AS Channel
    FROM Communications.Messages m
    WHERE m.Description IN ('InboundSMS', 'InboundEmail')
      AND m.DateReceivedUtc >= @from
      AND m.DateReceivedUtc <  @to
      AND m.ExternalAddress IS NOT NULL
      AND m.ExternalAddress <> ''
),

paired AS (
    SELECT
        i.MessageId,
        i.ARef,
        i.LoanbookId,
        i.DateReceivedUtc,
        i.Channel,
        -- Minutes to the next non-Message-Factory outbound in the same channel
        -- within 14 days. NULL if no such reply landed in window.
        (
            SELECT TOP 1 DATEDIFF(MINUTE, i.DateReceivedUtc, o.DateSentUtc)
            FROM Communications.Messages o
            WHERE o.ExternalAddress = i.ExternalAddress
              AND o.Description = CASE i.Channel
                                      WHEN 'SMS'   THEN 'OutboundSMS'
                                      WHEN 'Email' THEN 'OutboundEmail'
                                  END
              AND o.DateSentUtc >  i.DateReceivedUtc
              AND o.DateSentUtc <= DATEADD(MINUTE, @maxMinutes, i.DateReceivedUtc)
              AND (o.ClientType IS NULL OR o.ClientType <> 'MessageFactory')
            ORDER BY o.DateSentUtc ASC
        ) AS ReplyMinAll,
        -- Same, but additionally exclude Reply Robot / Robot Responder. Any
        -- ClientType containing 'Responder' is treated as the reply robot,
        -- which covers RobotResponder / TogetherLoansRobotResponder / similar.
        (
            SELECT TOP 1 DATEDIFF(MINUTE, i.DateReceivedUtc, o.DateSentUtc)
            FROM Communications.Messages o
            WHERE o.ExternalAddress = i.ExternalAddress
              AND o.Description = CASE i.Channel
                                      WHEN 'SMS'   THEN 'OutboundSMS'
                                      WHEN 'Email' THEN 'OutboundEmail'
                                  END
              AND o.DateSentUtc >  i.DateReceivedUtc
              AND o.DateSentUtc <= DATEADD(MINUTE, @maxMinutes, i.DateReceivedUtc)
              AND (
                  o.ClientType IS NULL
                  OR (
                      o.ClientType <> 'MessageFactory'
                      AND o.ClientType NOT LIKE '%Responder%'
                  )
              )
            ORDER BY o.DateSentUtc ASC
        ) AS ReplyMinHuman
    FROM inbound i
),

-- Point-in-time loan state: latest LoanHistory row up to and including the
-- message timestamp. Only joined when the inbound carries a LoanbookId.
loan_state AS (
    SELECT
        p.MessageId,
        lh.CurrentBalance,
        lh.Arrears,
        lh.DateInArrearsLocal
    FROM paired p
    CROSS APPLY (
        SELECT TOP 1 lh.CurrentBalance, lh.Arrears, lh.DateInArrearsLocal
        FROM Loanbook.LoanHistory lh
        WHERE lh.LoanbookId = p.LoanbookId
          AND lh.DateTimeUtc <= p.DateReceivedUtc
        ORDER BY lh.DateTimeUtc DESC
    ) lh
    WHERE p.LoanbookId IS NOT NULL
      AND p.LoanbookId <> ''
),

-- Signed-not-rejected guarantor at message time. A GT is "signed" iff there
-- is an ESignatures row for them (GtRef IS NOT NULL on the matching Customer
-- row in Applications.Customers) with DateSignedUtc <= message time, AND no
-- active decline-shape Flag on (ARef, GtRef) at that time. Flag types
-- 2 Decline, 3 DNL, 4 Cancelled, 6 FraudRisk match the canonical "rejected"
-- definition used elsewhere.
signed_gt AS (
    SELECT DISTINCT p.MessageId
    FROM paired p
    JOIN Applications.Customers c
      ON c.ARef = p.ARef AND c.GtRef IS NOT NULL
    JOIN Applications.ESignatures e
      ON e.EsignatureId = c.EsignatureId
    WHERE p.ARef IS NOT NULL AND p.ARef <> ''
      AND e.DateSignedUtc <= p.DateReceivedUtc
      AND NOT EXISTS (
          SELECT 1 FROM Applications.Flags f
          WHERE f.ARef = p.ARef
            AND f.GtRef = c.GtRef
            AND f.FlagTypeId IN (2, 3, 4, 6)
            AND f.DateAddedUtc <= p.DateReceivedUtc
            AND (f.DateRemovedUtc IS NULL OR f.DateRemovedUtc > p.DateReceivedUtc)
      )
),

classified AS (
    SELECT
        p.MessageId,
        CAST(p.DateReceivedUtc AS date) AS Day,
        CASE
            WHEN p.ARef IS NULL OR p.ARef = '' THEN 'unknown'
            WHEN ls.CurrentBalance > 10
                 AND (ls.Arrears > 0 OR ls.DateInArrearsLocal IS NOT NULL)
                THEN 'arrears'
            WHEN ls.CurrentBalance > 10
                 AND (ls.Arrears = 0 OR ls.Arrears IS NULL)
                 AND ls.DateInArrearsLocal IS NULL
                THEN 'live_loan'
            WHEN sg.MessageId IS NULL THEN 'applicant'
            ELSE 'other'  -- ARef + signed GT but no live loan; excluded from chart
        END AS Bucket,
        p.ReplyMinAll,
        p.ReplyMinHuman
    FROM paired p
    LEFT JOIN loan_state ls ON ls.MessageId = p.MessageId
    LEFT JOIN signed_gt  sg ON sg.MessageId = p.MessageId
)

SELECT
    Day,
    Bucket,
    COUNT(*)                                                              AS N_total,
    SUM(CASE WHEN ReplyMinAll   IS NOT NULL THEN 1 ELSE 0 END)            AS N_reply_all,
    SUM(CASE WHEN ReplyMinAll   IS NOT NULL THEN ReplyMinAll ELSE 0 END)  AS Sum_reply_all,
    SUM(CASE WHEN ReplyMinHuman IS NOT NULL THEN 1 ELSE 0 END)            AS N_reply_human,
    SUM(CASE WHEN ReplyMinHuman IS NOT NULL THEN ReplyMinHuman ELSE 0 END) AS Sum_reply_human
FROM classified
GROUP BY Day, Bucket
ORDER BY Day, Bucket;
"""


def main() -> None:
    print("connecting to Fabric…", flush=True)
    cn = pyodbc.connect(conn_str("DataWarehouse"))
    try:
        cur = cn.cursor()
        print(f"running comms response query for {YEAR}…", flush=True)
        cur.execute(QUERY)
        rows = cur.fetchall()
    finally:
        cn.close()

    print(f"got {len(rows)} (day, bucket) rows", flush=True)

    # Per-bucket per-day series. `other` (ARef + signed GT + no live loan) is
    # captured but not rendered as a chart line — kept for diagnostics.
    BUCKETS = ["unknown", "applicant", "live_loan", "arrears", "other"]
    series: dict[str, dict[str, dict]] = {b: {} for b in BUCKETS}

    for day, bucket, n_total, n_all, sum_all, n_human, sum_human in rows:
        bucket = bucket if bucket in series else "other"
        iso = day.strftime("%Y-%m-%d") if hasattr(day, "strftime") else str(day)
        series[bucket][iso] = {
            "n_total":         int(n_total or 0),
            "n_reply_all":     int(n_all or 0),
            "sum_reply_all":   float(sum_all or 0.0),
            "n_reply_human":   int(n_human or 0),
            "sum_reply_human": float(sum_human or 0.0),
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
        "schema_version":  1,
        "updated_at":      datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "year":            YEAR,
        "channels":        ["SMS", "Email"],
        "buckets":         ["unknown", "applicant", "live_loan", "arrears"],
        "max_reply_minutes": MAX_REPLY_MINUTES,
        "totals_by_bucket": totals,
        "series":          series,
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
