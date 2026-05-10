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
    leads_purchased = leads_by_result.get(1, 0)
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
    # Q3: Per-application progression for purchased-lead apps.
    #     Pull per-LeadID set of LeadOutcomeTypeIDs that ever fired,
    #     limited to apps in our March cohort.
    # ─────────────────────────────────────────────────────────────────
    print("# Q3: lead-outcome stage reach for purchased-lead apps…", flush=True)
    q = f"""
        WITH cohort AS (
            SELECT [{apps_leadid_col}] AS LeadID
            FROM dbo.Applications
            WHERE [{apps_date_col}] >= ? AND [{apps_date_col}] < ?
              AND [{apps_lender_col}] = ?
              AND [{apps_leadid_col}] IS NOT NULL
        )
        SELECT lo.[{lo_type_col}] AS outcome_type, COUNT(DISTINCT lo.[{lo_lead_col}]) AS n
        FROM dbo.LeadOutcomes lo
        INNER JOIN cohort c ON c.LeadID = lo.[{lo_lead_col}]
        GROUP BY lo.[{lo_type_col}]
    """
    cur.execute(q, [month_start, month_end, LENDER_ID])
    purchased_reach = {int(r[0]): int(r[1]) for r in cur.fetchall()}
    print(f"#   purchased-cohort outcome counts: {purchased_reach}", flush=True)

    # ─────────────────────────────────────────────────────────────────
    # Q4: Per-application progression for direct apps via Tasks.
    # ─────────────────────────────────────────────────────────────────
    print("# Q4: task-completion stage reach for direct apps…", flush=True)
    q = f"""
        WITH cohort AS (
            SELECT [{apps_aref_col}] AS ARef
            FROM dbo.Applications
            WHERE [{apps_date_col}] >= ? AND [{apps_date_col}] < ?
              AND [{apps_lender_col}] = ?
              AND [{apps_leadid_col}] IS NULL
        )
        SELECT
            t.[{tasks_type_col}] AS task_type,
            CASE WHEN t.[{tasks_gtref_col}] IS NULL THEN 'BRW' ELSE 'GT' END AS who,
            COUNT(DISTINCT c.ARef) AS n
        FROM dbo.Tasks t
        INNER JOIN cohort c ON c.ARef = t.[{tasks_aref_col}]
        WHERE t.[{tasks_done_col}] IS NOT NULL
          AND t.[{tasks_type_col}] IN (41, 48, 54, 62, 146)
        GROUP BY t.[{tasks_type_col}], CASE WHEN t.[{tasks_gtref_col}] IS NULL THEN 'BRW' ELSE 'GT' END
    """
    cur.execute(q, [month_start, month_end, LENDER_ID])
    direct_task_reach = {(int(r[0]), r[1]): int(r[2]) for r in cur.fetchall()}
    print(f"#   direct-cohort task counts: {direct_task_reach}", flush=True)

    apps_conn.close()

    # ─────────────────────────────────────────────────────────────────
    # Q5: Paid-out detection for direct apps via LoanAtInception in
    #     the Loanbook reporting DB.
    # ─────────────────────────────────────────────────────────────────
    print("# Q5: paid-out detection for direct apps via LoanAtInception…", flush=True)
    apps_conn = pyodbc.connect(conn_str("ReportingApplications"), timeout=20)
    apps_conn.timeout = QUERY_TIMEOUT
    cur = apps_conn.cursor()
    cur.execute(
        f"""
        SELECT [{apps_aref_col}]
        FROM dbo.Applications
        WHERE [{apps_date_col}] >= ? AND [{apps_date_col}] < ?
          AND [{apps_lender_col}] = ?
          AND [{apps_leadid_col}] IS NULL
        """,
        [month_start, month_end, LENDER_ID],
    )
    direct_arefs = [r[0] for r in cur.fetchall()]
    apps_conn.close()
    print(f"#   direct cohort ARefs: {len(direct_arefs):,}", flush=True)

    paid_out_direct = 0
    if direct_arefs:
        lb_conn = pyodbc.connect(conn_str("ReportingLoanbook"), timeout=20)
        lb_conn.timeout = QUERY_TIMEOUT
        cur = lb_conn.cursor()
        # Discover the join column on LoanAtInception. Likely 'ARef' or 'aref'
        # but neither is guaranteed on this warehouse.
        lai_cols = discover_columns(cur, "LoanAtInception")
        print(f"#   LoanAtInception cols: {sorted(lai_cols)}", flush=True)
        aref_col_lai = pick(lai_cols, "ARef", "Aref", "aref")
        if not aref_col_lai:
            print(f"#   no ARef-shaped column on LoanAtInception; skipping paid-out detection", flush=True)
        else:
            # Process in chunks to stay under the ~2100 parameter cap.
            chunk = 1500
            for i in range(0, len(direct_arefs), chunk):
                block = direct_arefs[i:i + chunk]
                placeholders = ",".join(["?"] * len(block))
                cur.execute(
                    f"""
                    SELECT COUNT(DISTINCT [{aref_col_lai}])
                    FROM dbo.LoanAtInception
                    WHERE [{aref_col_lai}] IN ({placeholders}) AND LenderId = ?
                    """,
                    [*block, LENDER_ID],
                )
                paid_out_direct += int(cur.fetchone()[0] or 0)
        lb_conn.close()
    print(f"#   paid-out direct: {paid_out_direct:,}", flush=True)

    # ─────────────────────────────────────────────────────────────────
    # Materialise stage counts.
    # Pipeline shape:
    #   "Leads presented" → "Leads purchased" → joins shared funnel
    #     drop: "Lead rejected"
    #   "Direct applications" → joins shared funnel
    #   Shared funnel: "Apply1" → "Invite GT (BRW signed)" → "GT accepted" → "VC ready" → "Paid out"
    # Each shared stage has a count that's the SUM of purchased-cohort + direct-cohort.
    # ─────────────────────────────────────────────────────────────────

    # Purchased-cohort stage reaches (a customer counts at all earlier stages
    # too, because "reached LeadOutcome 6" implies they passed 1, 4, 5).
    pur_apply1   = purchased_reach.get(1, 0)
    pur_invite   = purchased_reach.get(4, 0)
    pur_accepted = purchased_reach.get(5, 0)
    pur_vcready  = purchased_reach.get(6, 0)
    pur_paid     = purchased_reach.get(8, 0)

    dir_apply1   = direct_task_reach.get((41, 'BRW'), 0)
    dir_invite   = direct_task_reach.get((48, 'BRW'), 0)
    dir_accepted = direct_task_reach.get((54, 'GT'),  0)
    dir_vcready  = direct_task_reach.get((62, 'GT'),  0) + direct_task_reach.get((146, 'GT'), 0)
    dir_paid     = paid_out_direct

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
