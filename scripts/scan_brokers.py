"""
Per-broker conversion scorecard.

For every Brokers `Sources` row, aggregate a 90-day funnel:

    Leads presented  →  Leads purchased  →  Application started
       →  Apply1 (page 1)  →  BRW signed  →  GT accepted  →  VC ready
       →  Paid out

Plus average loan amount on paid-out loans and the leading lead-rejection
reasons. Brokers are linked via Leads.CampaignId → Campaigns.SourceId →
Sources.FriendlyName.

The aggregation runs entirely server-side (CTEs over Leads / Applications
/ Tasks); we never pull the multi-million-row Leads table to the runner.

Output: `brokers.json` at the repo root.

Required env vars:
  FABRIC_SQL_ENDPOINT, FABRIC_TENANT_ID, FABRIC_CLIENT_ID, FABRIC_CLIENT_SECRET

Optional env vars:
  BROKER_WINDOW_DAYS  rolling window (default 90)
  BROKER_LENDER_ID    LenderId to score (default 6 = Transform Credit)
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

import pyodbc

LENDER_ID = int(os.environ.get("BROKER_LENDER_ID", "6"))
LENDER_LABEL = "Transform Credit (LenderId 6, USA)" if LENDER_ID == 6 else f"LenderId {LENDER_ID}"
WINDOW_DAYS = int(os.environ.get("BROKER_WINDOW_DAYS", "90"))
QUERY_TIMEOUT = 900   # broker queries hit big tables; allow up to 15 min

# Per the wiki §3.10 / pipeline.json: TaskTypeIds that mark each progression
# stage when completed. GtRef NULL → BRW task, NOT NULL → GT task.
STAGE_TASK_IDS = {
    "apply1":      [(41, "BRW")],
    "brw_signed":  [(48, "BRW")],
    "gt_accepted": [(54, "GT")],
    "vc_ready":    [(62, "GT"), (146, "GT")],
}

# Lead-result enum from scan_pipeline.py. We only count 1 and 30 as
# "purchased" (Accepted / Pre-check passed → an ARef is allocated).
PURCHASED_RESULT_IDS = (1, 30)


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


def open_db(database: str) -> pyodbc.Connection | None:
    """Try to open a database; return None if connect fails (DB doesn't
    exist or no permission)."""
    try:
        c = pyodbc.connect(conn_str(database), timeout=20)
        c.timeout = QUERY_TIMEOUT
        return c
    except pyodbc.Error as e:
        print(f"# could not open {database}: {e}", flush=True)
        return None


def main() -> None:
    started = datetime.datetime.now(datetime.timezone.utc)
    window_end = started
    window_start = started - datetime.timedelta(days=WINDOW_DAYS)
    print(
        f"# scan_brokers start {started.isoformat()}  "
        f"window: {window_start.date()} → {window_end.date()} ({WINDOW_DAYS}d)  "
        f"lender: {LENDER_ID}",
        flush=True,
    )

    apps_conn = open_db("ReportingApplications")
    if apps_conn is None:
        sys.exit("error: ReportingApplications database unreachable")
    apps_cur = apps_conn.cursor()

    # ─── Discover the column names we need ────────────────────────────
    leads_cols = discover_columns(apps_cur, "Leads")
    apps_table_cols = discover_columns(apps_cur, "Applications")
    tasks_cols = discover_columns(apps_cur, "Tasks")

    leads_aref = pick(leads_cols, "ARef")
    leads_lender = pick(leads_cols, "LenderId")
    leads_date = pick(leads_cols, "DateReceivedUtc", "DateCreatedUtc")
    leads_result = pick(leads_cols, "LeadResultTypeId", "LeadResultId")
    leads_camp = pick(leads_cols, "CampaignId")
    leads_amount = pick(leads_cols, "LoanAmount", "LoanAmountRequested")
    leads_purpose = pick(leads_cols, "LoanPurposeId", "LoanPurpose")
    leads_state = pick(leads_cols, "State")
    apps_aref = pick(apps_table_cols, "ARef")
    apps_lender = pick(apps_table_cols, "LenderId")
    apps_status = pick(apps_table_cols, "ApplicationStatusTypeId", "ApplicationStatusId")
    apps_loan = pick(apps_table_cols, "LoanAmount", "LoanAmountRequested")
    tasks_aref = pick(tasks_cols, "ARef")
    tasks_type = pick(tasks_cols, "TaskTypeId")
    tasks_done = pick(tasks_cols, "DateCompletedUtc")
    tasks_gtref = pick(tasks_cols, "GtRef")

    print(
        f"# Leads cols: aref={leads_aref} lender={leads_lender} date={leads_date} "
        f"result={leads_result} camp={leads_camp} amount={leads_amount}",
        flush=True,
    )
    print(
        f"# Applications cols: aref={apps_aref} lender={apps_lender} "
        f"status={apps_status} loan={apps_loan}",
        flush=True,
    )
    print(
        f"# Tasks cols: aref={tasks_aref} type={tasks_type} done={tasks_done} gtref={tasks_gtref}",
        flush=True,
    )

    required = [leads_aref, leads_lender, leads_date, leads_result, leads_camp,
                apps_aref, apps_lender, apps_status,
                tasks_aref, tasks_type, tasks_done, tasks_gtref]
    if not all(required):
        sys.exit("error: required columns missing — see diagnostic output above")

    # ─── Load Sources + Campaigns lookups (Brokers namespace) ──────────
    sources: dict[int, dict] = {}
    campaign_to_source: dict[int, int] = {}
    campaign_meta: dict[int, dict] = {}

    def load_brokers_from(database: str) -> bool:
        global_loaded = False
        conn = open_db(database)
        if conn is None:
            return False
        try:
            cur = conn.cursor()
            # Sources
            cols = discover_columns(cur, "Sources")
            if cols and not sources:
                sid = pick(cols, "SourceId", "SourceID")
                snm = pick(cols, "FriendlyName", "ShortName", "CompanyName", "Name")
                scomp = pick(cols, "CompanyName")
                susr = pick(cols, "Username")
                slender = pick(cols, "LenderId")
                sweb = pick(cols, "CompanyWebsiteUrl", "WebsiteUrl", "Website")
                sphone = pick(cols, "PhoneNumber", "Phone")
                semail = pick(cols, "EmailAddress", "Email")
                if sid and snm:
                    sel_extra = ", ".join([
                        f"[{scomp}]" if scomp else "NULL",
                        f"[{susr}]" if susr else "NULL",
                        f"[{slender}]" if slender else "NULL",
                        f"[{sweb}]" if sweb else "NULL",
                        f"[{sphone}]" if sphone else "NULL",
                        f"[{semail}]" if semail else "NULL",
                    ])
                    cur.execute(
                        f"SELECT [{sid}], [{snm}], {sel_extra} FROM dbo.Sources"
                    )
                    for i, n, comp, usr, lid, web, phone, email in cur.fetchall():
                        if i is None: continue
                        # Filter to LenderId if we have it
                        if lid is not None and slender and int(lid) != LENDER_ID:
                            continue
                        sources[int(i)] = {
                            "source_id": int(i),
                            "friendly_name": (str(n).strip() if n is not None else "") or f"Source {i}",
                            "company_name": (str(comp).strip() if comp is not None else None) or None,
                            "username": (str(usr).strip() if usr is not None else None) or None,
                            "lender_id": int(lid) if lid is not None else None,
                            "website_url": (str(web).strip() if web is not None else None) or None,
                            "phone": (str(phone).strip() if phone is not None else None) or None,
                            "email": (str(email).strip() if email is not None else None) or None,
                        }
                    print(f"# Sources from {database}: {len(sources)} (lender {LENDER_ID})", flush=True)
                    global_loaded = True
            # Campaigns
            cols = discover_columns(cur, "Campaigns")
            if cols and not campaign_to_source:
                cid = pick(cols, "CampaignId", "CampaignID")
                csrc = pick(cols, "SourceId", "SourceID")
                cnm = pick(cols, "CampaignFriendlyName", "CampaignName", "FriendlyName", "Name")
                ctype = pick(cols, "CommissionType")
                crate = pick(cols, "CommissionRate")
                # If MessageType is present we're looking at the CRM
                # Campaigns table, not the Brokers one — skip.
                if "MessageType" in cols:
                    print(f"# skipping CRM Campaigns table in {database}", flush=True)
                elif cid and csrc:
                    sel_extra = ", ".join([
                        f"[{cnm}]" if cnm else "NULL",
                        f"[{ctype}]" if ctype else "NULL",
                        f"[{crate}]" if crate else "NULL",
                    ])
                    cur.execute(
                        f"SELECT [{cid}], [{csrc}], {sel_extra} FROM dbo.Campaigns"
                    )
                    for i, src, nm, t, r in cur.fetchall():
                        if i is None: continue
                        i_int = int(i)
                        if src is not None:
                            campaign_to_source[i_int] = int(src)
                        campaign_meta[i_int] = {
                            "campaign_id": i_int,
                            "source_id": int(src) if src is not None else None,
                            "friendly_name": (str(nm).strip() if nm is not None else None) or None,
                            "commission_type": (str(t).strip() if t is not None else None) or None,
                            "commission_rate": float(r) if r is not None else None,
                        }
                    print(f"# Campaigns from {database}: {len(campaign_meta)}", flush=True)
                    global_loaded = True
        finally:
            conn.close()
        return global_loaded

    for db_candidate in ("ReportingBrokers", "ReportingApplications"):
        load_brokers_from(db_candidate)
        if sources and campaign_to_source:
            break

    if not sources:
        print("# WARNING: no Sources loaded — output will show CampaignId numbers", flush=True)
    if not campaign_to_source:
        print("# WARNING: no Campaign→Source mapping loaded — sources won't aggregate", flush=True)

    # ─── Q1: per-campaign lead totals in the window ─────────────────
    print("# Q1: per-campaign lead presented + purchased + last-seen", flush=True)
    apps_cur.execute(
        f"""
        SELECT [{leads_camp}],
               COUNT(*) AS presented,
               SUM(CASE WHEN [{leads_result}] IN ({",".join(str(x) for x in PURCHASED_RESULT_IDS)})
                        THEN 1 ELSE 0 END) AS purchased,
               MAX([{leads_date}]) AS last_lead_at
        FROM dbo.Leads
        WHERE [{leads_date}] >= ? AND [{leads_date}] < ?
          AND [{leads_lender}] = ?
        GROUP BY [{leads_camp}]
        """,
        [window_start, window_end, LENDER_ID],
    )
    leads_per_campaign = {}
    for camp_id, presented, purchased, last_lead in apps_cur.fetchall():
        camp_id_int = int(camp_id) if camp_id is not None else None
        leads_per_campaign[camp_id_int] = {
            "presented": int(presented or 0),
            "purchased": int(purchased or 0),
            "last_lead_at": last_lead.isoformat() if last_lead and hasattr(last_lead, "isoformat") else (str(last_lead) if last_lead else None),
        }
    print(
        f"#   campaigns with leads: {len(leads_per_campaign):,}  "
        f"total presented: {sum(v['presented'] for v in leads_per_campaign.values()):,}  "
        f"total purchased: {sum(v['purchased'] for v in leads_per_campaign.values()):,}",
        flush=True,
    )

    # ─── Q2: per-campaign rejection-reason breakdown (for top-3 per source)
    print("# Q2: per-campaign rejection reasons (top-5)", flush=True)
    apps_cur.execute(
        f"""
        SELECT [{leads_camp}], [{leads_result}], COUNT(*) AS n
        FROM dbo.Leads
        WHERE [{leads_date}] >= ? AND [{leads_date}] < ?
          AND [{leads_lender}] = ?
          AND [{leads_result}] NOT IN ({",".join(str(x) for x in PURCHASED_RESULT_IDS)})
        GROUP BY [{leads_camp}], [{leads_result}]
        """,
        [window_start, window_end, LENDER_ID],
    )
    rejections_per_campaign: dict[int | None, list[tuple[int, int]]] = {}
    for camp_id, rtype, n in apps_cur.fetchall():
        if rtype is None: continue
        camp_id_int = int(camp_id) if camp_id is not None else None
        rejections_per_campaign.setdefault(camp_id_int, []).append((int(rtype), int(n)))

    # ─── Q3: per-campaign application + stage aggregation ───────────
    # CTE: purchased_arefs (Leads in window with ARef + a 'purchased' result)
    # joined to Applications + Tasks, max() per ARef to get stage reach.
    print("# Q3: per-campaign application stages + paid-out counts (big query)", flush=True)
    stage_sql = f"""
        WITH purchased_arefs AS (
            SELECT l.[{leads_aref}] AS ARef,
                   MAX(l.[{leads_camp}]) AS CampaignId
            FROM dbo.Leads l
            WHERE l.[{leads_date}] >= ? AND l.[{leads_date}] < ?
              AND l.[{leads_lender}] = ?
              AND l.[{leads_result}] IN ({",".join(str(x) for x in PURCHASED_RESULT_IDS)})
              AND l.[{leads_aref}] IS NOT NULL
            GROUP BY l.[{leads_aref}]
        ),
        per_aref AS (
            SELECT p.CampaignId,
                   p.ARef,
                   MAX(a.[{apps_status}]) AS status,
                   MAX(CAST(a.[{apps_loan}] AS FLOAT)) AS loan_amount,
                   MAX(CASE WHEN t.[{tasks_type}] = 41 AND t.[{tasks_gtref}] IS NULL THEN 1 ELSE 0 END) AS apply1,
                   MAX(CASE WHEN t.[{tasks_type}] = 48 AND t.[{tasks_gtref}] IS NULL THEN 1 ELSE 0 END) AS brw_sign,
                   MAX(CASE WHEN t.[{tasks_type}] = 54 AND t.[{tasks_gtref}] IS NOT NULL THEN 1 ELSE 0 END) AS gt_pass,
                   MAX(CASE WHEN t.[{tasks_type}] IN (62,146) AND t.[{tasks_gtref}] IS NOT NULL THEN 1 ELSE 0 END) AS vc
            FROM purchased_arefs p
            INNER JOIN dbo.Applications a ON a.[{apps_aref}] = p.ARef AND a.[{apps_lender}] = ?
            LEFT JOIN dbo.Tasks t ON t.[{tasks_aref}] = p.ARef AND t.[{tasks_done}] IS NOT NULL
            GROUP BY p.CampaignId, p.ARef
        )
        SELECT CampaignId,
               COUNT(*) AS apps,
               SUM(CASE WHEN status = 5 THEN 1 ELSE 0 END) AS paid_out,
               SUM(apply1) AS apply1,
               SUM(brw_sign) AS brw_signed,
               SUM(gt_pass) AS gt_accepted,
               SUM(vc) AS vc_ready,
               SUM(CASE WHEN status = 5 THEN loan_amount ELSE 0 END) AS paid_out_loan_total,
               AVG(CAST(loan_amount AS FLOAT)) AS avg_loan_amount
        FROM per_aref
        GROUP BY CampaignId
    """
    apps_cur.execute(stage_sql, [window_start, window_end, LENDER_ID, LENDER_ID])
    stages_per_campaign = {}
    for row in apps_cur.fetchall():
        cid, apps_n, paid, ap1, brw, gt, vc, paid_loan_total, avg_loan = row
        cid_int = int(cid) if cid is not None else None
        stages_per_campaign[cid_int] = {
            "applications": int(apps_n or 0),
            "apply1": int(ap1 or 0),
            "brw_signed": int(brw or 0),
            "gt_accepted": int(gt or 0),
            "vc_ready": int(vc or 0),
            "paid_out": int(paid or 0),
            "paid_out_loan_total": float(paid_loan_total or 0),
            "avg_loan_amount": float(avg_loan or 0),
        }
    print(
        f"#   campaigns with apps: {len(stages_per_campaign):,}  "
        f"total apps: {sum(v['applications'] for v in stages_per_campaign.values()):,}  "
        f"total paid-out: {sum(v['paid_out'] for v in stages_per_campaign.values()):,}",
        flush=True,
    )

    apps_conn.close()

    # ─── Roll up campaign-level data to source-level rows ────────────
    sources_data: dict[int | None, dict] = {}

    def source_slot(sid: int | None) -> dict:
        if sid not in sources_data:
            meta = sources.get(sid) if sid is not None else None
            sources_data[sid] = {
                "source_id":      sid,
                "friendly_name":  (meta or {}).get("friendly_name") if meta else (f"Source {sid}" if sid is not None else "Unknown source"),
                "company_name":   (meta or {}).get("company_name") if meta else None,
                "username":       (meta or {}).get("username") if meta else None,
                "website_url":    (meta or {}).get("website_url") if meta else None,
                "phone":          (meta or {}).get("phone") if meta else None,
                "email":          (meta or {}).get("email") if meta else None,
                "campaign_ids":   [],
                "leads_presented": 0,
                "leads_purchased": 0,
                "applications":    0,
                "apply1":          0,
                "brw_signed":      0,
                "gt_accepted":     0,
                "vc_ready":        0,
                "paid_out":        0,
                "paid_out_loan_total": 0.0,
                "avg_loan_amount_sum":  0.0,  # weighted by apps count → averaged at end
                "avg_loan_amount_n":    0,
                "rejection_counts": {},   # result_id → count
                "last_lead_at":    None,
            }
        return sources_data[sid]

    for cid, leads in leads_per_campaign.items():
        sid = campaign_to_source.get(cid) if cid is not None else None
        slot = source_slot(sid)
        slot["leads_presented"] += leads["presented"]
        slot["leads_purchased"] += leads["purchased"]
        if cid is not None and cid not in slot["campaign_ids"]:
            slot["campaign_ids"].append(cid)
        last = leads.get("last_lead_at")
        if last and (slot["last_lead_at"] is None or last > slot["last_lead_at"]):
            slot["last_lead_at"] = last

    for cid, stages in stages_per_campaign.items():
        sid = campaign_to_source.get(cid) if cid is not None else None
        slot = source_slot(sid)
        for k in ("applications", "apply1", "brw_signed", "gt_accepted", "vc_ready", "paid_out"):
            slot[k] += stages[k]
        slot["paid_out_loan_total"] += stages["paid_out_loan_total"]
        if stages["applications"] > 0 and stages["avg_loan_amount"]:
            slot["avg_loan_amount_sum"] += stages["avg_loan_amount"] * stages["applications"]
            slot["avg_loan_amount_n"]   += stages["applications"]

    for cid, reasons in rejections_per_campaign.items():
        sid = campaign_to_source.get(cid) if cid is not None else None
        slot = source_slot(sid)
        for rtype, n in reasons:
            slot["rejection_counts"][rtype] = slot["rejection_counts"].get(rtype, 0) + n

    # Finalise: weighted-avg loan amount, rate columns, top-3 rejections
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

    rows = []
    for sid, slot in sources_data.items():
        apps_n = slot["applications"]
        slot["avg_loan_amount"] = (
            slot["avg_loan_amount_sum"] / slot["avg_loan_amount_n"]
            if slot["avg_loan_amount_n"] > 0 else None
        )
        slot.pop("avg_loan_amount_sum", None)
        slot.pop("avg_loan_amount_n", None)
        # Conversion rates (denominators are 0-safe)
        slot["purchase_rate"]    = (slot["leads_purchased"] / slot["leads_presented"]) if slot["leads_presented"] else None
        slot["app_rate"]         = (slot["applications"]    / slot["leads_purchased"]) if slot["leads_purchased"] else None
        slot["apply1_rate"]      = (slot["apply1"]          / slot["applications"]) if apps_n else None
        slot["brw_signed_rate"]  = (slot["brw_signed"]      / slot["applications"]) if apps_n else None
        slot["gt_accepted_rate"] = (slot["gt_accepted"]     / slot["applications"]) if apps_n else None
        slot["vc_ready_rate"]    = (slot["vc_ready"]        / slot["applications"]) if apps_n else None
        slot["paid_out_rate"]    = (slot["paid_out"]        / slot["applications"]) if apps_n else None
        slot["lead_to_paid_rate"]= (slot["paid_out"]        / slot["leads_presented"]) if slot["leads_presented"] else None
        # Top-3 rejection reasons (count, label)
        rejections = sorted(slot["rejection_counts"].items(), key=lambda kv: -kv[1])[:3]
        slot["top_rejections"] = [
            {"label": LEAD_RESULT_LABELS.get(rid, f"code {rid}"), "code": rid, "count": n}
            for rid, n in rejections
        ]
        slot.pop("rejection_counts", None)
        rows.append(slot)

    rows.sort(key=lambda r: -r["leads_presented"])

    # ─── Totals ───────────────────────────────────────────────────────
    def total(key: str) -> int | float:
        return sum(r.get(key) or 0 for r in rows)

    totals = {
        "sources":          len([r for r in rows if r.get("leads_presented")]),
        "leads_presented":  int(total("leads_presented")),
        "leads_purchased":  int(total("leads_purchased")),
        "applications":     int(total("applications")),
        "apply1":           int(total("apply1")),
        "brw_signed":       int(total("brw_signed")),
        "gt_accepted":      int(total("gt_accepted")),
        "vc_ready":         int(total("vc_ready")),
        "paid_out":         int(total("paid_out")),
        "paid_out_loan_total": float(total("paid_out_loan_total")),
    }
    if totals["leads_presented"]:
        totals["purchase_rate"]    = totals["leads_purchased"] / totals["leads_presented"]
        totals["lead_to_paid_rate"]= totals["paid_out"]        / totals["leads_presented"]
    if totals["applications"]:
        totals["paid_out_rate"] = totals["paid_out"] / totals["applications"]

    output = {
        "snapshot_at":      started.isoformat(),
        "snapshot_date":    started.date().isoformat(),
        "lender_id":        LENDER_ID,
        "lender_label":     LENDER_LABEL,
        "window_days":      WINDOW_DAYS,
        "window_start":     window_start.date().isoformat(),
        "window_end":       window_end.date().isoformat(),
        "stages": [
            "leads_presented", "leads_purchased", "applications",
            "apply1", "brw_signed", "gt_accepted", "vc_ready", "paid_out",
        ],
        "stage_labels": {
            "leads_presented": "Leads presented",
            "leads_purchased": "Leads purchased",
            "applications":    "Applications started",
            "apply1":           "Page 1 complete",
            "brw_signed":       "BRW signed",
            "gt_accepted":      "GT accepted",
            "vc_ready":         "VC ready",
            "paid_out":         "Paid out",
        },
        "totals":           totals,
        "sources":          rows,
    }
    out_path = Path("brokers.json")
    out_path.write_text(json.dumps(output, indent=2, default=str))
    print(
        f"# wrote {out_path} ({out_path.stat().st_size:,} bytes); "
        f"{len(rows):,} broker rows; total presented {totals['leads_presented']:,}, paid-out {totals['paid_out']:,}",
        flush=True,
    )


if __name__ == "__main__":
    main()
