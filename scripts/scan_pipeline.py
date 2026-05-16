"""
Scan the Fabric warehouse for the application pipeline funnel — March 2026,
Transform Credit (LenderId 6) only.

Two entry points converge into a shared progression:

  Leads presented   ─┬─→  Leads purchased ─┐
                     │                     │
                     └─→  Lead rejected    │
                                           ├──→  Apply1 complete ──→ BRW signed
   Direct apps ─────────────────────→  ────┘                              │
                                                                          ↓
                                                            GT passed checks
                                                                          │
                                                                          ↓
                                                                    VC ready
                                                                          │
                                                                          ↓
                                                                    Paid out

Strict-stage definitions (per the user's choice):
  - Apply1            = LeadOutcome 1 (purchased) / Task 41 GtRef=null (direct)
  - Invite GT         = LeadOutcome 4 = BRW signed contract (purchased)
                      / Task 48 GtRef=null (direct)
  - GT accepted       = LeadOutcome 5 = GT passed credit/bank/ID (purchased)
                      / Task 54 GtRef!=null (direct)
  - VC ready          = LeadOutcome 6 = GT completed VC (purchased)
                      / Task 62 OR 146, GtRef!=null (direct)
  - Paid out          = LeadOutcome 8 = Paid out (purchased)
                      / LoanAtInception row exists for the ARef under LenderId=6 (direct)

Output: `pipeline.json` at repo root.

Required env vars (same set as scan_row_counts.py):
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

LENDER_ID = 6
LENDER_LABEL = "Transform Credit (LenderId 6, USA)"

# Per the wiki's LeadResultTypeId enum on Applications.Leads.
LEAD_RESULT_LABELS = {
    -1: "Source excluded",
    1:  "Accepted (purchased)",
    2:  "Already claimed",
    3:  "Duplicate",
    4:  "Throttled",
    5:  "Invalid campaign",
    6:  "Invalid email address",
    7:  "Invalid phone number",
    8:  "Invalid name",
    9:  "Invalid loan purpose",
    10: "Invalid language",
    11: "No national / government ID",
    12: "No valid product",
    13: "Existing loan",
    14: "Ineligible for credit-builder",
    15: "Invalid state",
    16: "Blacklisted",
    17: "Premium scorecard failed",
    18: "Seen too frequently",
    19: "Settled loan",
    20: "Rejected for lead score",
    21: "Rejected with counteroffer",
    22: "Counteroffer rejected",
    23: "Bank account not validated",
    24: "Invalid employment type",
    25: "Invalid pay frequency",
    26: "Invalid pay type",
    27: "Invalid bank account type",
    30: "Pre-check passed",
    98: "Unclaimed (CPC steal)",
    99: "Discarded",
}

# Window — March of the current calendar year. Fixed (not rolling) to match
# the user's brief "applications made in March".
WINDOW_YEAR = 2026
WINDOW_MONTH = 3

QUERY_TIMEOUT = 600


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


def main() -> None:
    started = datetime.datetime.now(datetime.timezone.utc)
    month_start = datetime.date(WINDOW_YEAR, WINDOW_MONTH, 1)
    if WINDOW_MONTH == 12:
        month_end = datetime.date(WINDOW_YEAR + 1, 1, 1)
    else:
        month_end = datetime.date(WINDOW_YEAR, WINDOW_MONTH + 1, 1)
    print(f"# scan_pipeline start {started.isoformat()}  window: {month_start} to {month_end} (exclusive)", flush=True)

    # ─────────────────────────────────────────────────────────────────
    # Phase 1 — connect to ReportingApplications
    # ─────────────────────────────────────────────────────────────────
    apps_conn = pyodbc.connect(conn_str("ReportingApplications"), timeout=20)
    apps_conn.timeout = QUERY_TIMEOUT
    cur = apps_conn.cursor()

    apps_cols = discover_columns(cur, "Applications")
    leads_cols = discover_columns(cur, "leads")
    lo_cols = discover_columns(cur, "LeadOutcomes")
    tasks_cols = discover_columns(cur, "Tasks")
    print(f"# Applications cols: {sorted(apps_cols)}", flush=True)
    print(f"# leads cols: {sorted(leads_cols)}", flush=True)
    print(f"# LeadOutcomes cols: {sorted(lo_cols)}", flush=True)
    print(f"# Tasks cols: {sorted(tasks_cols)}", flush=True)

    apps_date_col = pick(apps_cols, "InterestingDateTimeUtc", "InterestingDateTimeUTC", "DateCreatedUtc", "DateCreatedUTC", "CreatedDateTimeUtc")
    apps_lender_col = pick(apps_cols, "LenderId")
    apps_aref_col = pick(apps_cols, "ARef")
    apps_leadid_col = pick(apps_cols, "LeadID", "LeadId")

    leads_date_col = pick(leads_cols, "DateCreatedUTC", "DateCreatedUtc", "DateReceivedUTC", "DateReceivedUtc")
    leads_lender_col = pick(leads_cols, "LenderId")
    leads_result_col = pick(leads_cols, "LeadResultTypeId", "LeadResultTypeID")
    leads_id_col = pick(leads_cols, "LeadID", "LeadId")

    lo_lead_col = pick(lo_cols, "LeadID", "LeadId")
    lo_type_col = pick(lo_cols, "LeadOutcomeTypeID", "LeadOutcomeTypeId")

    tasks_aref_col = pick(tasks_cols, "ARef", "Aref")
    tasks_type_col = pick(tasks_cols, "TaskTypeID", "TaskTypeId")
    tasks_done_col = pick(tasks_cols, "DateCompletedUTC", "DateCompletedUtc")
    tasks_gtref_col = pick(tasks_cols, "GtRef", "GTRef")

    if not all([apps_date_col, apps_lender_col, apps_aref_col, apps_leadid_col,
                leads_date_col, leads_lender_col, leads_result_col, leads_id_col,
                lo_lead_col, lo_type_col,
                tasks_aref_col, tasks_type_col, tasks_done_col, tasks_gtref_col]):
        sys.exit(f"error: required columns missing — apps_lender={apps_lender_col} leads_lender={leads_lender_col} ...")

    # ─────────────────────────────────────────────────────────────────
    # Q1: Leads presented in March, TC, with their result type.
    #     Whole-cohort entry point for the lead funnel.
    # ─────────────────────────────────────────────────────────────────
    print("# Q1: leads presented in March (TC) by result type…", flush=True)
    q = f"""
        SELECT [{leads_result_col}] AS result_id, COUNT(*) AS n
        FROM dbo.leads
        WHERE [{leads_date_col}] >= ? AND [{leads_date_col}] < ?
          AND [{leads_lender_col}] = ?
        GROUP BY [{leads_result_col}]
    """
    cur.execute(q, [month_start, month_end, LENDER_ID])
    leads_by_result = {int(r[0]) if r[0] is not None else None: int(r[1]) for r in cur.fetchall()}
    leads_presented = sum(leads_by_result.values())
    # Result IDs 1 and 30 are both effectively "purchased":
    #   1  = Sold to Customer (canonical)
    #   30 = Pre-check passed (lead accepted into pipeline)
    # The page (`pipeline.html` `categoriseResult` + `bucketReason`)
    # treats both as purchased; counting only 1 here makes the
    # rejection-bucket sum disagree with `leads_presented`.
    leads_purchased = leads_by_result.get(1, 0) + leads_by_result.get(30, 0)
    leads_rejected = leads_presented - leads_purchased
    print(f"#   presented={leads_presented:,} purchased={leads_purchased:,} rejected={leads_rejected:,}", flush=True)
    print(f"#   by_result_type: {leads_by_result}", flush=True)

    # ─────────────────────────────────────────────────────────────────
    # Q2: Applications created in March, TC. Split by LeadID NULL/NOT NULL
    #     (= direct vs purchased-lead pathway) — this is the
    #     "applications started" cohort the funnel measures progression on.
    # ─────────────────────────────────────────────────────────────────
    print("# Q2: applications created in March (TC) by entry path…", flush=True)
    q = f"""
        SELECT
            CASE WHEN [{apps_leadid_col}] IS NULL THEN 'direct' ELSE 'purchased' END AS entry_path,
            COUNT(*) AS n
        FROM dbo.Applications
        WHERE [{apps_date_col}] >= ? AND [{apps_date_col}] < ?
          AND [{apps_lender_col}] = ?
        GROUP BY CASE WHEN [{apps_leadid_col}] IS NULL THEN 'direct' ELSE 'purchased' END
    """
    cur.execute(q, [month_start, month_end, LENDER_ID])
    apps_by_path = {r[0]: int(r[1]) for r in cur.fetchall()}
    apps_purchased = apps_by_path.get('purchased', 0)
    apps_direct = apps_by_path.get('direct', 0)
    print(f"#   apps purchased={apps_purchased:,}  direct={apps_direct:,}", flush=True)

    # ─────────────────────────────────────────────────────────────────
    # Q3: Per-application stage reach via Tasks — uniform across BOTH
    #     entry paths so the funnel is consistently measured.
    #     Stages:
    #       Apply1     = Task 41 GtRef=null completed
    #       BRW signed = Task 48 GtRef=null completed (= Invite GT)
    #       GT passed  = Task 54 GtRef!=null completed (= GT accepted)
    #       VC done    = Task 62 OR 146 GtRef!=null completed (= VC ready)
    # ─────────────────────────────────────────────────────────────────
    print("# Q3: task-completion stage reach for ALL March cohort apps…", flush=True)
    q = f"""
        WITH cohort AS (
            SELECT [{apps_aref_col}] AS ARef,
                   CASE WHEN [{apps_leadid_col}] IS NULL THEN 'direct' ELSE 'purchased' END AS entry_path
            FROM dbo.Applications
            WHERE [{apps_date_col}] >= ? AND [{apps_date_col}] < ?
              AND [{apps_lender_col}] = ?
        )
        SELECT
            c.entry_path,
            t.[{tasks_type_col}] AS task_type,
            CASE WHEN t.[{tasks_gtref_col}] IS NULL THEN 'BRW' ELSE 'GT' END AS who,
            COUNT(DISTINCT c.ARef) AS n
        FROM dbo.Tasks t
        INNER JOIN cohort c ON c.ARef = t.[{tasks_aref_col}]
        WHERE t.[{tasks_done_col}] IS NOT NULL
          AND t.[{tasks_type_col}] IN (41, 48, 54, 62, 146)
        GROUP BY c.entry_path, t.[{tasks_type_col}],
                 CASE WHEN t.[{tasks_gtref_col}] IS NULL THEN 'BRW' ELSE 'GT' END
    """
    cur.execute(q, [month_start, month_end, LENDER_ID])
    task_reach = {(r[0], int(r[1]), r[2]): int(r[3]) for r in cur.fetchall()}
    print(f"#   task counts: {task_reach}", flush=True)

    # Q3b: VC-ready needs a SEPARATE DISTINCT count across tasks 62 OR
    # 146 (per wiki §1137 these are alternative VC flow variants for
    # the same application). Summing the per-task COUNT(DISTINCT ARef)
    # values from Q3 double-counts any ARef that completed both
    # variants, inflating the "VC ready" funnel stage.
    q_vc = f"""
        WITH cohort AS (
            SELECT [{apps_aref_col}] AS ARef,
                   CASE WHEN [{apps_leadid_col}] IS NULL THEN 'direct' ELSE 'purchased' END AS entry_path
            FROM dbo.Applications
            WHERE [{apps_date_col}] >= ? AND [{apps_date_col}] < ?
              AND [{apps_lender_col}] = ?
        )
        SELECT
            c.entry_path,
            COUNT(DISTINCT c.ARef) AS n
        FROM dbo.Tasks t
        INNER JOIN cohort c ON c.ARef = t.[{tasks_aref_col}]
        WHERE t.[{tasks_done_col}] IS NOT NULL
          AND t.[{tasks_type_col}] IN (62, 146)
          AND t.[{tasks_gtref_col}] IS NOT NULL
        GROUP BY c.entry_path
    """
    cur.execute(q_vc, [month_start, month_end, LENDER_ID])
    vc_reach = {r[0]: int(r[1]) for r in cur.fetchall()}
    print(f"#   VC ready (distinct ARefs over tasks 62|146): {vc_reach}", flush=True)

    # ─────────────────────────────────────────────────────────────────
    # Q4: Paid-out — uniform via ApplicationStatusTypeId = 5 (LiveLoan)
    #     on the Applications table, current-state. This avoids the
    #     LoanAtInception join (which uses LoanBookID, not ARef) and
    #     gives a like-for-like measure across both cohorts.
    # ─────────────────────────────────────────────────────────────────
    print("# Q4: paid-out via ApplicationStatusTypeId = 5 (LiveLoan)…", flush=True)
    q = f"""
        SELECT
            CASE WHEN [{apps_leadid_col}] IS NULL THEN 'direct' ELSE 'purchased' END AS entry_path,
            COUNT(*) AS n
        FROM dbo.Applications
        WHERE [{apps_date_col}] >= ? AND [{apps_date_col}] < ?
          AND [{apps_lender_col}] = ?
          AND ApplicationStatusTypeId = 5
        GROUP BY CASE WHEN [{apps_leadid_col}] IS NULL THEN 'direct' ELSE 'purchased' END
    """
    cur.execute(q, [month_start, month_end, LENDER_ID])
    paid_by_path = {r[0]: int(r[1]) for r in cur.fetchall()}
    print(f"#   paid-out by path: {paid_by_path}", flush=True)

    apps_conn.close()

    # ─────────────────────────────────────────────────────────────────
    # Materialise stage counts.
    # Pipeline shape:
    #   "Leads presented" → "Leads purchased" → joins shared funnel
    #     drop: "Lead rejected"
    #   "Direct applications" → joins shared funnel
    #   Shared funnel: "Apply1" → "Invite GT (BRW signed)" → "GT accepted" → "VC ready" → "Paid out"
    # Each shared stage has a count that's the SUM of purchased-cohort + direct-cohort.
    # ─────────────────────────────────────────────────────────────────

    pur_apply1   = task_reach.get(('purchased', 41, 'BRW'), 0)
    pur_invite   = task_reach.get(('purchased', 48, 'BRW'), 0)
    pur_accepted = task_reach.get(('purchased', 54, 'GT'),  0)
    pur_vcready  = vc_reach.get('purchased', 0)
    pur_paid     = paid_by_path.get('purchased', 0)

    dir_apply1   = task_reach.get(('direct',    41, 'BRW'), 0)
    dir_invite   = task_reach.get(('direct',    48, 'BRW'), 0)
    dir_accepted = task_reach.get(('direct',    54, 'GT'),  0)
    dir_vcready  = vc_reach.get('direct', 0)
    dir_paid     = paid_by_path.get('direct', 0)

    apps_started_total = apps_purchased + apps_direct
    apply1_total       = pur_apply1   + dir_apply1
    invite_total       = pur_invite   + dir_invite
    accepted_total     = pur_accepted + dir_accepted
    vcready_total      = pur_vcready  + dir_vcready
    paid_total         = pur_paid     + dir_paid

    output = {
        "snapshot_at": started.isoformat(),
        "snapshot_date": started.date().isoformat(),
        "lender_id": LENDER_ID,
        "lender_label": LENDER_LABEL,
        "month": f"{WINDOW_YEAR:04d}-{WINDOW_MONTH:02d}",
        "month_label": month_start.strftime("%B %Y"),
        "leads": {
            "presented": leads_presented,
            "purchased": leads_purchased,
            "rejected":  leads_rejected,
            "by_result_type": leads_by_result,
            "by_result_label": [
                {
                    "type_id": tid,
                    "label":   LEAD_RESULT_LABELS.get(tid, f"Unknown ({tid})"),
                    "count":   n,
                    "is_purchased": tid == 1,
                }
                for tid, n in sorted(leads_by_result.items(), key=lambda kv: -kv[1])
            ],
        },
        "apps": {
            "purchased_path": apps_purchased,
            "direct_path":    apps_direct,
            "total":          apps_started_total,
        },
        "stages": [
            {
                "key": "leads_presented",
                "label": "Leads presented",
                "count": leads_presented,
                "kind": "entry",
            },
            {
                "key": "leads_purchased",
                "label": "Leads purchased",
                "count": leads_purchased,
                "kind": "transition",
                "from": "leads_presented",
                "drop_label": "Lead rejected",
                "drop_count": leads_rejected,
            },
            {
                "key": "direct_apps",
                "label": "Direct applications",
                "count": apps_direct,
                "kind": "entry",
            },
            {
                "key": "apps_started",
                "label": "Application started",
                "count": apps_started_total,
                "kind": "convergence",
                "feeds_from": ["leads_purchased", "direct_apps"],
                "comment": "Note: leads_purchased counts cookies; not every purchased lead becomes an Application within the month."
                           " Application count uses Applications.LeadID NULL split.",
            },
            {
                "key": "apply1",
                "label": "Apply1 complete (Page 1)",
                "count": apply1_total,
                "kind": "stage",
                "from": "apps_started",
                "drop_label": "Abandoned before page 1",
                "drop_count": max(0, apps_started_total - apply1_total),
                "purchased": pur_apply1,
                "direct":    dir_apply1,
            },
            {
                "key": "invite_gt",
                "label": "Invite guarantor (BRW signed)",
                "count": invite_total,
                "kind": "stage",
                "from": "apply1",
                "drop_label": "Dropped before BRW signed",
                "drop_count": max(0, apply1_total - invite_total),
                "purchased": pur_invite,
                "direct":    dir_invite,
            },
            {
                "key": "gt_accepted",
                "label": "Guarantor accepted (passed checks)",
                "count": accepted_total,
                "kind": "stage",
                "from": "invite_gt",
                "drop_label": "No accepted guarantor",
                "drop_count": max(0, invite_total - accepted_total),
                "purchased": pur_accepted,
                "direct":    dir_accepted,
            },
            {
                "key": "vc_ready",
                "label": "VC ready",
                "count": vcready_total,
                "kind": "stage",
                "from": "gt_accepted",
                "drop_label": "No VC reached",
                "drop_count": max(0, accepted_total - vcready_total),
                "purchased": pur_vcready,
                "direct":    dir_vcready,
            },
            {
                "key": "paid_out",
                "label": "Paid out",
                "count": paid_total,
                "kind": "stage",
                "from": "vc_ready",
                "drop_label": "VC ready but not paid out",
                "drop_count": max(0, vcready_total - paid_total),
                "purchased": pur_paid,
                "direct":    dir_paid,
            },
        ],
    }
    out_path = Path("pipeline.json")
    out_path.write_text(json.dumps(output, indent=2, default=str))
    print(f"# wrote {out_path} ({out_path.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
