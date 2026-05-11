"""
Source-quality analysis for the Brokers page.

Two outputs over a rolling 60-day window:

A) **Weak accepted sources** — sources we currently buy leads from whose
   funnel-to-paid-out rate is statistically below the cohort median.
   Volume-gated so a 3-purchased-leads source can't dominate the list.

B) **Blocked sources to reconsider** — sources whose leads get
   LeadResultTypeId = -1 ("Source excluded") in the window. For each
   excluded person we look for an identity match in our purchased leads
   from OTHER sources in the same window. Match logic:

       SSN/GovernmentIdNumber match
     OR (PhoneNumber AND DateOfBirth) match
     OR (EmailAddress  AND DateOfBirth) match

   If a meaningful share of an excluded source's people later showed
   up under another source and paid out, that source's audience is
   actually decent — worth reconsidering the block.

Output: `source-quality.json` at repo root.

Required env vars (same set as scan_brokers.py):
  FABRIC_SQL_ENDPOINT, FABRIC_TENANT_ID, FABRIC_CLIENT_ID, FABRIC_CLIENT_SECRET

Optional:
  SQ_WINDOW_DAYS    rolling window (default 60)
  SQ_LENDER_ID      LenderId to score (default 6 = Transform Credit)
  SQ_MIN_VOLUME     min purchased leads for a source to qualify for
                    the weak-accepted ranking (default 200)
  SQ_MIN_EXCLUDED   min source-excluded leads for a source to appear
                    in the blocked-to-reconsider list (default 200)
"""
from __future__ import annotations

import datetime
import json
import os
import statistics
import sys
from pathlib import Path

import pyodbc

LENDER_ID = int(os.environ.get("SQ_LENDER_ID", "6"))
LENDER_LABEL = "Transform Credit (LenderId 6, USA)" if LENDER_ID == 6 else f"LenderId {LENDER_ID}"
WINDOW_DAYS = int(os.environ.get("SQ_WINDOW_DAYS", "60"))
MATURATION_DAYS = int(os.environ.get("SQ_MATURATION_DAYS", "30"))
# Bounceback temporal cap: a same-identity purchase only counts as a
# bounceback if it lands STRICTLY AFTER the rejection and within this
# many days. Catches "we blocked them, then they came back through
# another route shortly" — not "they were already our customer".
BOUNCEBACK_WINDOW_DAYS = int(os.environ.get("SQ_BOUNCEBACK_WINDOW_DAYS", "30"))
MIN_VOLUME = int(os.environ.get("SQ_MIN_VOLUME", "200"))
MIN_EXCLUDED = int(os.environ.get("SQ_MIN_EXCLUDED", "200"))
QUERY_TIMEOUT = 1200   # identity-match join is heavy — allow up to 20 min

PURCHASED_RESULT_IDS = (1, 30)
EXCLUDED_RESULT_ID = -1


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
    # Shift the window 30 days back so paid_out has time to mature
    # (typical funded loan completes within ~30 days of lead purchase).
    # Without this lag, recent leads look worse than they are because
    # they simply haven't had time to pay out yet.
    window_end = started - datetime.timedelta(days=MATURATION_DAYS)
    window_start = window_end - datetime.timedelta(days=WINDOW_DAYS)
    print(
        f"# scan_source_quality start {started.isoformat()}  "
        f"window: {window_start.date()} → {window_end.date()} ({WINDOW_DAYS}d, ending {MATURATION_DAYS}d ago for paid-out maturation)  "
        f"lender: {LENDER_ID}  min_volume: {MIN_VOLUME}  min_excluded: {MIN_EXCLUDED}",
        flush=True,
    )

    conn = pyodbc.connect(conn_str("ReportingApplications"), timeout=20)
    conn.timeout = QUERY_TIMEOUT
    cur = conn.cursor()

    # ─── Discover columns ─────────────────────────────────────────────
    leads_cols = discover_columns(cur, "Leads")
    apps_cols = discover_columns(cur, "Applications")
    tasks_cols = discover_columns(cur, "Tasks")

    L_aref = pick(leads_cols, "ARef")
    L_lender = pick(leads_cols, "LenderId")
    L_date = pick(leads_cols, "DateReceivedUtc", "DateCreatedUtc")
    L_result = pick(leads_cols, "LeadResultTypeId", "LeadResultId")
    L_camp = pick(leads_cols, "CampaignId")
    L_phone = pick(leads_cols, "PhoneNumber", "Phone")
    L_email = pick(leads_cols, "EmailAddress", "Email")
    L_dob = pick(leads_cols, "DateOfBirth")
    L_bid = pick(leads_cols, "BidAmount", "Price", "LeadCost")
    L_gov = pick(leads_cols, "GovernmentIdNumber", "NationalIdNumber", "SSN")
    L_nat = pick(leads_cols, "NationalIdNumber")
    L_leadid = pick(leads_cols, "LeadId", "LeadID")
    L_sr1 = pick(leads_cols, "SourceReference1")

    A_aref = pick(apps_cols, "ARef")
    A_lender = pick(apps_cols, "LenderId")
    A_status = pick(apps_cols, "ApplicationStatusTypeId", "ApplicationStatusId")
    A_loan = pick(apps_cols, "LoanAmount", "LoanAmountRequested")

    print(
        f"# Leads cols: aref={L_aref} lender={L_lender} date={L_date} "
        f"result={L_result} camp={L_camp} phone={L_phone} email={L_email} "
        f"dob={L_dob} gov={L_gov} nat={L_nat} leadid={L_leadid}",
        flush=True,
    )

    # ─── DIAGNOSTIC: SourceReference1/2/3 distribution ────────────────
    # User hypothesis: the actual upstream Source granularity lives in
    # SourceReference1/2/3 on Leads. Dump existence + distinct-value
    # counts + top-20 most-common values from the rolling window so we
    # can confirm before refactoring the analysis around them.
    sref_cols = [c for c in ("SourceReference1", "SourceReference2", "SourceReference3") if c in leads_cols]
    if not sref_cols:
        print("# SourceReference1/2/3: NONE of these columns exist on dbo.Leads", flush=True)
    else:
        print(f"# SourceReference columns present: {sref_cols}", flush=True)
        for col in sref_cols:
            try:
                cur.execute(
                    f"""
                    SELECT TOP 20 [{col}] AS v, COUNT(*) AS n
                    FROM dbo.Leads
                    WHERE [{L_date}] >= ? AND [{L_date}] < ?
                      AND [{L_lender}] = ?
                      AND [{col}] IS NOT NULL
                      AND LTRIM(RTRIM(CAST([{col}] AS NVARCHAR(200)))) <> ''
                    GROUP BY [{col}]
                    ORDER BY COUNT(*) DESC
                    """,
                    [window_start, window_end, LENDER_ID],
                )
                rows = cur.fetchall()
                cur.execute(
                    f"""
                    SELECT COUNT(DISTINCT [{col}]) AS distinct_vals,
                           SUM(CASE WHEN [{col}] IS NOT NULL AND LTRIM(RTRIM(CAST([{col}] AS NVARCHAR(200)))) <> '' THEN 1 ELSE 0 END) AS non_null_rows,
                           COUNT(*) AS total_rows
                    FROM dbo.Leads
                    WHERE [{L_date}] >= ? AND [{L_date}] < ?
                      AND [{L_lender}] = ?
                    """,
                    [window_start, window_end, LENDER_ID],
                )
                d_count, non_null, total = cur.fetchone()
                print(
                    f"#   {col}: {d_count:,} distinct values, "
                    f"{non_null:,}/{total:,} non-null rows ({(non_null/total*100 if total else 0):.1f}%)",
                    flush=True,
                )
                for v, n in rows[:20]:
                    sv = (str(v)[:60] if v is not None else "(NULL)").replace("\n", " ")
                    print(f"#     {n:>10,}  {sv!r}", flush=True)
            except Exception as e:
                print(f"#   {col}: query failed — {e}", flush=True)

    # ─── Brokers, Campaigns + SourceType lookups ──────────────────────
    # Terminology in this scanner:
    #   Source     = an upstream sub-broker our Brokers resell from. In
    #                the DB this is Brokers.Sources.SourceTypeID joined
    #                to dbo.SourceTypes for the English label.
    #   Broker     = a company we have a direct relationship with. In
    #                the DB this is a Brokers.Sources row (its SourceId).
    #   Campaign   = our pricing tier with a Broker. In the DB this is a
    #                Brokers.Campaigns row.
    #
    # Each Lead carries a CampaignId. Roll-up:
    #   Lead.CampaignId -> Campaign.SourceId -> Broker -> SourceTypeID -> Source label.
    sources: dict[int, dict] = {}            # broker_id → {friendly_name, source_type_id, source_type_name}
    campaign_to_source: dict[int, int] = {}  # campaign_id → broker_id
    campaign_meta: dict[int, dict] = {}      # campaign_id → metadata
    source_type_names: dict[int, str] = {}   # source_type_id → label

    def load_brokers_from(database: str) -> bool:
        any_loaded = False
        try:
            c2 = pyodbc.connect(conn_str(database), timeout=20)
            c2.timeout = QUERY_TIMEOUT
        except pyodbc.Error as e:
            print(f"# {database} unreachable: {e}", flush=True)
            return False
        try:
            cur2 = c2.cursor()
            # SourceTypes lookup first — needed when filling in Sources.
            stcols = discover_columns(cur2, "SourceTypes")
            if stcols and not source_type_names:
                stid = pick(stcols, "SourceTypeId", "SourceTypeID", "Id", "ID")
                stnm = pick(stcols, "FriendlyName", "Name", "SourceTypeName", "Description")
                if stid and stnm:
                    cur2.execute(f"SELECT [{stid}], [{stnm}] FROM dbo.SourceTypes")
                    for i, n in cur2.fetchall():
                        if i is None: continue
                        source_type_names[int(i)] = (str(n).strip() if n is not None else None) or f"SourceType {i}"
                    print(f"# SourceTypes from {database}: {len(source_type_names)} types loaded", flush=True)
                    any_loaded = True

            scols = discover_columns(cur2, "Sources")
            if scols and not sources:
                sid = pick(scols, "SourceId", "SourceID")
                snm = pick(scols, "FriendlyName", "ShortName", "CompanyName", "Name")
                slender = pick(scols, "LenderId")
                stid_col = pick(scols, "SourceTypeId", "SourceTypeID")
                if sid and snm:
                    sel = ", ".join([
                        f"[{sid}]",
                        f"[{snm}]",
                        f"[{slender}]" if slender else "NULL",
                        f"[{stid_col}]" if stid_col else "NULL",
                    ])
                    cur2.execute(f"SELECT {sel} FROM dbo.Sources")
                    for i, n, lid, st_id in cur2.fetchall():
                        if i is None: continue
                        if lid is not None and slender and int(lid) != LENDER_ID:
                            continue
                        st_id_int = int(st_id) if st_id is not None else None
                        sources[int(i)] = {
                            "source_id":           int(i),
                            "friendly_name":       (str(n).strip() if n is not None else "") or f"Source {i}",
                            "source_type_id":      st_id_int,
                            "source_type_name":    source_type_names.get(st_id_int) if st_id_int is not None else None,
                        }
                    print(
                        f"# Sources from {database}: {len(sources)} brokers "
                        f"({sum(1 for s in sources.values() if s.get('source_type_id') is not None)} with SourceType)",
                        flush=True,
                    )
                    any_loaded = True
            ccols = discover_columns(cur2, "Campaigns")
            if ccols and not campaign_to_source:
                if "MessageType" not in ccols:
                    cid = pick(ccols, "CampaignId", "CampaignID")
                    csrc = pick(ccols, "SourceId", "SourceID")
                    cnm = pick(ccols, "CampaignFriendlyName", "CampaignName", "FriendlyName", "Name")
                    ctyp = pick(ccols, "CommissionType", "CommissionTypeId")
                    crate = pick(ccols, "CommissionRate", "CommissionAmount", "Rate", "Price")
                    if cid and csrc:
                        sel_extra = f", [{cnm}]" if cnm else ", NULL"
                        sel_extra += f", [{ctyp}]" if ctyp else ", NULL"
                        sel_extra += f", [{crate}]" if crate else ", NULL"
                        cur2.execute(f"SELECT [{cid}], [{csrc}]{sel_extra} FROM dbo.Campaigns")
                        for i, src, nm, ct, cr in cur2.fetchall():
                            if i is None or src is None: continue
                            campaign_to_source[int(i)] = int(src)
                            campaign_meta[int(i)] = {
                                "campaign_id":    int(i),
                                "broker_id":      int(src),
                                "source_name":    (str(nm).strip() if nm is not None else None) or f"Campaign {i}",
                                "commission_type": (str(ct).strip() if ct is not None else None),
                                "commission_rate": (float(cr) if cr is not None else None),
                            }
                        print(f"# Campaign→Source: {len(campaign_to_source)} mappings from {database} (commission cols: type={ctyp}, rate={crate})", flush=True)
                        any_loaded = True
        finally:
            c2.close()
        return any_loaded

    for db in ("ReportingBrokers", "ReportingApplications"):
        load_brokers_from(db)
        if sources and campaign_to_source:
            break

    def _broker_name(bid: int | None) -> str:
        if bid is None: return "Unknown broker"
        return (sources.get(bid) or {}).get("friendly_name") or f"Broker {bid}"

    def _source_name(cid: int | None) -> str:
        if cid is None: return "Unknown source"
        return (campaign_meta.get(cid) or {}).get("source_name") or f"Campaign {cid}"

    # ─── Part A: weak-accepted (Broker, SourceReference1) ranking ────
    # Aggregation unit is (broker, sr1). Each lead carries both
    # CampaignId (→ broker) and SourceReference1. We aggregate per
    # (CampaignId, SR1) at the SQL layer so we can apply the
    # campaign-specific cost-model formula, then roll up to (broker,
    # SR1) in Python.
    sr1_sel_l = f"l.[{L_sr1}]" if L_sr1 else "NULL"
    print("# Part A: per-(Campaign, SourceReference1) purchase + paid_out counts", flush=True)
    loan_sel = f"MAX(CAST(a.[{A_loan}] AS FLOAT))" if A_loan else "NULL"
    cur.execute(
        f"""
        WITH purchased AS (
            SELECT l.[{L_aref}] AS ARef,
                   MAX(l.[{L_camp}]) AS CampaignId,
                   MAX({sr1_sel_l}) AS SR1
            FROM dbo.Leads l
            WHERE l.[{L_date}] >= ? AND l.[{L_date}] < ?
              AND l.[{L_lender}] = ?
              AND l.[{L_result}] IN ({",".join(str(x) for x in PURCHASED_RESULT_IDS)})
              AND l.[{L_aref}] IS NOT NULL
            GROUP BY l.[{L_aref}]
        ),
        with_status AS (
            SELECT p.CampaignId,
                   p.SR1,
                   p.ARef,
                   MAX(CASE WHEN a.[{A_status}] = 5 THEN 1 ELSE 0 END) AS paid,
                   {loan_sel} AS loan_amount
            FROM purchased p
            INNER JOIN dbo.Applications a ON a.[{A_aref}] = p.ARef AND a.[{A_lender}] = ?
            GROUP BY p.CampaignId, p.SR1, p.ARef
        )
        SELECT CampaignId, SR1,
               COUNT(*)                                        AS apps,
               SUM(paid)                                       AS paid_out,
               SUM(CASE WHEN paid = 1 THEN loan_amount END)    AS paid_loan_total
        FROM with_status
        GROUP BY CampaignId, SR1
        """,
        [window_start, window_end, LENDER_ID, LENDER_ID],
    )
    # keyed on (campaign_id, sr1)
    accepted_per_csr1: dict[tuple, tuple] = {}
    for cid, sr1, apps, paid, loan_total in cur.fetchall():
        cid_int = int(cid) if cid is not None else None
        sr1_norm = (str(sr1).strip() if sr1 is not None else None) or None
        accepted_per_csr1[(cid_int, sr1_norm)] = (
            int(apps), int(paid or 0), float(loan_total or 0.0)
        )
    print(f"#   (campaign, SR1) cells with purchased apps: {len(accepted_per_csr1):,}", flush=True)

    # Per-CAMPAIGN (user's "source") aggregation
    weak_data: dict[int | None, dict] = {}
    def _new_weak_slot(cid: int | None) -> dict:
        broker_id = campaign_to_source.get(cid) if cid is not None else None
        meta = campaign_meta.get(cid) or {} if cid is not None else {}
        return {
            "campaign_id":     cid,
            "source_name":     _source_name(cid),
            "broker_id":       broker_id,
            "broker_name":     _broker_name(broker_id),
            "commission_type": meta.get("commission_type"),
            "commission_rate": meta.get("commission_rate"),
            "applications":    0,
            "paid_out":        0,
        }
    # Aggregate accepted apps/paid/loan-amounts per CampaignId (the
    # campaign-level rollup we still need to apply the cost-model
    # formula, since CommissionType lives on the Campaign).
    for (cid, sr1), (apps, paid, loan_total) in accepted_per_csr1.items():
        slot = weak_data.setdefault(cid, _new_weak_slot(cid))
        slot["applications"]    += apps
        slot["paid_out"]        += paid
        slot["paid_loan_total"]  = slot.get("paid_loan_total", 0.0) + (loan_total or 0.0)

    # Per-(CampaignId, SR1) lead count + bid sum, for both per-campaign
    # cost-model derivation and the (broker, sr1) rollup at the end.
    bid_sql = f", SUM(CAST(l.[{L_bid}] AS FLOAT)) AS bid_total" if L_bid else ", NULL AS bid_total"
    cur.execute(
        f"""
        SELECT l.[{L_camp}], {sr1_sel_l} AS SR1, COUNT(*) AS purchased{bid_sql}
        FROM dbo.Leads l
        WHERE l.[{L_date}] >= ? AND l.[{L_date}] < ?
          AND l.[{L_lender}] = ?
          AND l.[{L_result}] IN ({",".join(str(x) for x in PURCHASED_RESULT_IDS)})
        GROUP BY l.[{L_camp}], {sr1_sel_l}
        """,
        [window_start, window_end, LENDER_ID],
    )
    # Holds per-(cid, sr1) lead counts + bid totals — used for the
    # (broker, sr1) rollup at the end.
    purchased_per_csr1: dict[tuple, dict] = {}
    for cid, sr1, n, bid_total in cur.fetchall():
        cid_int = int(cid) if cid is not None else None
        sr1_norm = (str(sr1).strip() if sr1 is not None else None) or None
        purchased_per_csr1[(cid_int, sr1_norm)] = {
            "leads_purchased": int(n),
            "bid_total":       float(bid_total) if bid_total is not None else None,
        }
        slot = weak_data.setdefault(cid_int, _new_weak_slot(cid_int))
        slot["leads_purchased"] = slot.get("leads_purchased", 0) + int(n)
        if bid_total is not None:
            slot["bid_total"] = slot.get("bid_total", 0.0) + float(bid_total)
    print(f"#   (campaign, SR1) cells with purchased leads: {len(purchased_per_csr1):,}", flush=True)

    # Compute paid_out_rate + cost metrics per campaign and stats across
    # the qualifying cohort.
    # CommissionType '3' is CPC (cost-per-click PPC spend) — not a broker
    # lead, so exclude those campaigns from the source-quality scorecard.
    CPC_COMMISSION_TYPE = "3"

    def _campaign_cost(slot: dict) -> tuple[float | None, str]:
        """
        Return (total_cost, model_label) based on the campaign's
        CommissionType. None means we have no defensible cost figure.

          1 = CPF   → rate × paid_out (cost per funded loan)
          2 = CPL   → rate × leads_purchased (flat per-lead price)
          4 = BID   → sum of Leads.BidAmount; fall back to
                       rate × leads_purchased if bid was never set
          5 = REV   → rate × sum(paid-out loan amount) (revenue share)
        Anything else (incl. None) → no cost.
        """
        ct = str(slot.get("commission_type"))
        rate = slot.get("commission_rate") or 0
        purchased = slot.get("leads_purchased") or 0
        paid = slot.get("paid_out") or 0
        bid_total = slot.get("bid_total")
        paid_loan_total = slot.get("paid_loan_total") or 0.0
        if ct == "1":
            return (rate * paid, "cpf")
        if ct == "2":
            return (rate * purchased, "cpl")
        if ct == "4":
            if bid_total is not None and bid_total > 0:
                return (bid_total, "bid")
            return (rate * purchased, "bid_floor")
        if ct == "5":
            return (rate * paid_loan_total, "rev_share")
        return (None, "unknown")

    qualifying: list[dict] = []
    excluded_cpc = 0
    for slot in weak_data.values():
        if (slot.get("leads_purchased") or 0) < MIN_VOLUME:
            slot["qualifies"] = False
            continue
        if str(slot.get("commission_type")) == CPC_COMMISSION_TYPE:
            slot["qualifies"] = False
            slot["excluded_reason"] = "cpc_ppc_not_a_broker"
            excluded_cpc += 1
            continue
        slot["qualifies"] = True
        slot["paid_out_rate"] = (
            slot["paid_out"] / slot["leads_purchased"]
            if slot["leads_purchased"] else 0
        )
        total_cost, model = _campaign_cost(slot)
        slot["cost_model"] = model
        if total_cost is not None:
            slot["total_cost"] = total_cost
            if slot["leads_purchased"]:
                slot["cost_per_lead"] = total_cost / slot["leads_purchased"]
            slot["cost_per_paid_loan"] = (
                total_cost / slot["paid_out"] if slot["paid_out"] else None
            )
        qualifying.append(slot)

    rates = [s["paid_out_rate"] for s in qualifying if s["paid_out_rate"] is not None]
    median_rate = statistics.median(rates) if rates else 0
    q1_rate = (
        statistics.quantiles(rates, n=4)[0] if len(rates) >= 4 else min(rates) if rates else 0
    )
    print(f"# Qualifying sources: {len(qualifying)}  median paid_out_rate: {median_rate:.5f}  Q1: {q1_rate:.5f}", flush=True)
    print(f"# Excluded {excluded_cpc} CPC/PPC campaigns (CommissionType=3) — not broker leads", flush=True)

    # ─── Rollup to (Broker, SourceReference1) ─────────────────────────
    # The actual decision unit. SourceReference1 is the upstream
    # affiliate/sub-source code set by the broker on each lead (~78k
    # distinct values, 99.5% fill rate). Same value across different
    # brokers means different things, so the key is the tuple.
    def _cell_cost(cid: int | None, leads: int, paid: int, bid_total: float | None, paid_loan_total: float) -> tuple[float | None, str]:
        if cid is None:
            return (None, "unknown")
        meta = campaign_meta.get(cid) or {}
        ct = str(meta.get("commission_type"))
        rate = meta.get("commission_rate") or 0
        if ct == "1":
            return (rate * paid, "cpf")
        if ct == "2":
            return (rate * leads, "cpl")
        if ct == "4":
            if bid_total is not None and bid_total > 0:
                return (bid_total, "bid")
            return (rate * leads, "bid_floor")
        if ct == "5":
            return (rate * paid_loan_total, "rev_share")
        return (None, "unknown")

    # Build per-(broker, sr1) totals by walking every (cid, sr1) cell.
    broker_sr1_raw: dict[tuple, dict] = {}
    for (cid, sr1), cell in purchased_per_csr1.items():
        if cid is None: continue
        meta = campaign_meta.get(cid)
        if not meta: continue
        if str(meta.get("commission_type")) == CPC_COMMISSION_TYPE:
            continue
        broker_id = campaign_to_source.get(cid)
        leads = cell["leads_purchased"]
        bid_total = cell.get("bid_total")
        apps, paid, paid_loan_total = accepted_per_csr1.get((cid, sr1), (0, 0, 0.0))
        cost_cell, model = _cell_cost(cid, leads, paid, bid_total, paid_loan_total)
        key = (broker_id, sr1)
        slot = broker_sr1_raw.setdefault(key, {
            "broker_id":         broker_id,
            "broker_name":       _broker_name(broker_id),
            "source_ref1":       sr1,
            "leads_purchased":   0,
            "applications":      0,
            "paid_out":          0,
            "paid_loan_total":   0.0,
            "total_cost":        0.0,
            "has_cost":          False,
            "_campaigns":        {},
        })
        slot["leads_purchased"]  += leads
        slot["applications"]     += apps
        slot["paid_out"]         += paid
        slot["paid_loan_total"]  += paid_loan_total
        if cost_cell is not None:
            slot["total_cost"] += cost_cell
            slot["has_cost"]   = True
        camp = slot["_campaigns"].setdefault(cid, {
            "campaign_id":    cid,
            "campaign_name":  _source_name(cid),
            "commission_type": meta.get("commission_type"),
            "commission_rate": meta.get("commission_rate"),
            "cost_model":     model,
            "leads_purchased": 0,
            "paid_out":       0,
            "total_cost":     0.0,
            "has_cost":       False,
        })
        camp["leads_purchased"] += leads
        camp["paid_out"]        += paid
        if cost_cell is not None:
            camp["total_cost"] += cost_cell
            camp["has_cost"]   = True

    # Derive cell-level rates + filter on MIN_VOLUME at the (broker, sr1)
    # level. Drop cells where source_ref1 is missing — those rows are
    # not actionable ("re-enable Broker X's no-Source code" makes no
    # sense) and they confuse the page.
    bs_qualifying: list[dict] = []
    dropped_unsourced = 0
    for key, slot in broker_sr1_raw.items():
        if key[1] is None:
            dropped_unsourced += 1
            continue
        lp = slot["leads_purchased"]
        if lp < MIN_VOLUME:
            continue
        po = slot["paid_out"]
        slot["paid_out_rate"] = po / lp if lp else 0
        if slot["has_cost"]:
            slot["cost_per_lead"]      = slot["total_cost"] / lp if lp else None
            slot["cost_per_paid_loan"] = (slot["total_cost"] / po) if po else None
        else:
            slot["total_cost"]         = None
            slot["cost_per_lead"]      = None
            slot["cost_per_paid_loan"] = None
        # Materialize campaign breakdown as a sorted list (by leads desc)
        slot["campaigns"] = sorted(slot.pop("_campaigns").values(), key=lambda c: -(c.get("leads_purchased") or 0))
        bs_qualifying.append(slot)
    bs_qualifying.sort(key=lambda s: -(s.get("cost_per_paid_loan") or 0))
    print(
        f"# (Broker, SR1) cells: {len(broker_sr1_raw):,} total; "
        f"{dropped_unsourced} dropped as no-Source; {len(bs_qualifying)} pass MIN_VOLUME={MIN_VOLUME}",
        flush=True,
    )

    # ─── Commission-model diagnostic ──────────────────────────────────
    # We don't know the semantics of CommissionType across the catalogue.
    # Dump every unique (type, rate-band) pair seen across qualifying
    # campaigns so the user can map them to cost formulas.
    print("# Commission-model diagnostic across qualifying campaigns:", flush=True)
    by_type: dict[str, list] = {}
    for s in qualifying:
        key = str(s.get("commission_type"))
        by_type.setdefault(key, []).append(s)
    for ctype, members in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        rates_seen = sorted({m.get("commission_rate") for m in members if m.get("commission_rate") is not None})
        rate_str = ", ".join(f"{r:g}" for r in rates_seen[:6])
        if len(rates_seen) > 6:
            rate_str += f", … ({len(rates_seen)} distinct)"
        if not rates_seen:
            rate_str = "(no rate set)"
        sample = members[0]
        print(
            f"#   type={ctype!r:<20} campaigns={len(members):>3}  rates={{{rate_str}}}  "
            f"e.g. {sample['broker_name']} / {sample['source_name']} (cid {sample['campaign_id']})",
            flush=True,
        )

    for s in qualifying:
        s["rate_vs_median"] = (s["paid_out_rate"] / median_rate) if median_rate else None
        # Flag rules: < Q1 = clearly low, < median/2 = severely low, < median = below average
        rate = s["paid_out_rate"]
        if rate < q1_rate:
            s["flag"] = "below Q1"
        elif rate < median_rate:
            s["flag"] = "below median"
        else:
            s["flag"] = None

    qualifying.sort(key=lambda s: s.get("paid_out_rate") or 0)

    # ─── Part B: bounceback analysis of source-excluded leads ─────────
    # Aggregate per CampaignId (the user's "source").
    print("# Part B: per-campaign source-excluded counts", flush=True)
    cur.execute(
        f"""
        SELECT l.[{L_camp}], COUNT(*) AS excluded
        FROM dbo.Leads l
        WHERE l.[{L_date}] >= ? AND l.[{L_date}] < ?
          AND l.[{L_lender}] = ?
          AND l.[{L_result}] = ?
        GROUP BY l.[{L_camp}]
        """,
        [window_start, window_end, LENDER_ID, EXCLUDED_RESULT_ID],
    )
    excluded_per_campaign: dict[int | None, int] = {}
    for cid, n in cur.fetchall():
        cid_int = int(cid) if cid is not None else None
        excluded_per_campaign[cid_int] = excluded_per_campaign.get(cid_int, 0) + int(n)
    print(f"#   campaigns with any excluded leads: {len(excluded_per_campaign):,}", flush=True)
    print(f"#   total excluded leads: {sum(excluded_per_campaign.values()):,}", flush=True)
    candidate_campaigns = {cid: n for cid, n in excluded_per_campaign.items() if n >= MIN_EXCLUDED}
    print(f"#   campaigns with >= {MIN_EXCLUDED} excluded: {len(candidate_campaigns):,}", flush=True)
    # Back-compat with later code which references candidate_sources
    candidate_sources = candidate_campaigns
    excluded_per_source = excluded_per_campaign

    # Also break out excluded counts by (campaign, SR1) so we can roll
    # up bounceback aggregation to (broker, SR1) — the analysis unit.
    if L_sr1:
        cur.execute(
            f"""
            SELECT l.[{L_camp}], {sr1_sel_l} AS SR1, COUNT(*) AS excluded
            FROM dbo.Leads l
            WHERE l.[{L_date}] >= ? AND l.[{L_date}] < ?
              AND l.[{L_lender}] = ?
              AND l.[{L_result}] = ?
            GROUP BY l.[{L_camp}], {sr1_sel_l}
            """,
            [window_start, window_end, LENDER_ID, EXCLUDED_RESULT_ID],
        )
        excluded_per_bs: dict[tuple, int] = {}
        for cid, sr1, n in cur.fetchall():
            cid_int = int(cid) if cid is not None else None
            sr1_norm = (str(sr1).strip() if sr1 is not None else None) or None
            broker_id = campaign_to_source.get(cid_int) if cid_int is not None else None
            key = (broker_id, sr1_norm)
            excluded_per_bs[key] = excluded_per_bs.get(key, 0) + int(n)
    else:
        excluded_per_bs = {}

    # Now the heavy join. We do three separate joins (one per identity
    # strategy) and union the results — SQL Server's optimiser handles
    # this far better than a single OR-joined condition.
    # Output: per excluded-SourceId, how many distinct people we later
    # purchased via OTHER sources, and how many of those paid out.
    print("# Part B: running 3-way identity-match join (this is the heavy one)…", flush=True)

    # Bounceback aggregation is keyed on (broker_id, sr1) of the
    # REJECTED lead — the unit of analysis the page surfaces.
    bounceback_per_bs: dict[tuple, dict] = {}

    def _ensure_bs_slot(broker_id, sr1):
        key = (broker_id, sr1)
        if key not in bounceback_per_bs:
            bounceback_per_bs[key] = {
                "broker_id":         broker_id,
                "broker_name":       _broker_name(broker_id),
                "source_ref1":       sr1,
                "excluded_count":    excluded_per_bs.get(key, 0),
                "match_leadids":     set(),
                "match_arefs":       set(),
                "match_paid_arefs":  set(),
                "by_strategy":       {"gov_id": 0, "phone_dob": 0, "email_dob": 0},
                "destination_keys":  {},  # (broker_id, sr1) → bounceback count
            }
        return bounceback_per_bs[key]

    # Each join: pick the rejected leads from sources with ≥MIN_EXCLUDED
    # excluded count (via Campaigns join) and match against PURCHASED leads
    # in the same window via the identity strategy. We exclude same-source
    # matches because a bounceback only counts if it came in via a
    # DIFFERENT source.
    base_join_filter = f"""
            l_e.[{L_date}] >= ? AND l_e.[{L_date}] < ?
            AND l_e.[{L_lender}] = ?
            AND l_e.[{L_result}] = ?
            AND l_p.[{L_date}] >= ? AND l_p.[{L_date}] < ?
            AND l_p.[{L_lender}] = ?
            AND l_p.[{L_result}] IN ({",".join(str(x) for x in PURCHASED_RESULT_IDS)})
            AND l_p.[{L_aref}] IS NOT NULL
    """

    # ─── Strategy: sample-and-match in Python ──────────────────────────
    # The previous version did three Leads-self-joins inside SQL Server.
    # On 60 days of leads (~75M rows) the join cardinality blew out and
    # the run was abandoned after 35 minutes. New approach:
    #
    # 1) Pull a bounded sample of source-excluded leads (top N per
    #    CampaignId, ordered by recency). Caps the rejected side at
    #    SAMPLE_PER_CAMPAIGN × n_candidate_campaigns.
    # 2) Pull every purchased lead in the window with paid-out status
    #    (~3-5M rows). One streaming query.
    # 3) Build three hash indexes in Python: by SSN, by phone+DOB, by
    #    email+DOB. Match each sampled rejected lead against all three.
    #
    # Matching becomes O(N + M) rather than O(N × M). Scales for any
    # source size by caring only about the sample, not the total
    # rejection count.
    SAMPLE_PER_CAMPAIGN = int(os.environ.get("SQ_SAMPLE_PER_CAMPAIGN", "1500"))
    print(f"# Part B: sampling up to {SAMPLE_PER_CAMPAIGN} rejected leads per candidate campaign…", flush=True)

    # Candidate set is the CampaignIds with >= MIN_EXCLUDED excluded
    # leads — we already keyed candidate_sources on CampaignId since the
    # broker→campaign refactor. (The previous version filtered by
    # checking BrokerIds against this set, which only matched by
    # numeric coincidence.)
    candidate_campaign_ids = [c for c in candidate_sources.keys() if c is not None]
    print(f"#   candidate campaigns to sample: {len(candidate_campaign_ids)}", flush=True)

    sampled: list[dict] = []
    if candidate_campaign_ids and L_phone and L_dob and L_leadid:
        # Chunk the IN-list to keep parameter count reasonable
        CHUNK = 800
        for i in range(0, len(candidate_campaign_ids), CHUNK):
            chunk_ids = candidate_campaign_ids[i:i+CHUNK]
            ph = ",".join(["?"] * len(chunk_ids))
            cur.execute(
                f"""
                SELECT * FROM (
                    SELECT l.[{L_leadid}] AS LeadId, l.[{L_camp}] AS CampaignId,
                           {sr1_sel_l}    AS SR1,
                           l.[{L_phone}]  AS Phone,
                           l.[{L_dob}]    AS DOB,
                           l.[{L_email}]  AS Email,
                           {f"l.[{L_gov}]"  if L_gov  else "NULL"} AS GovId,
                           l.[{L_date}]   AS DateReceived,
                           ROW_NUMBER() OVER (PARTITION BY l.[{L_camp}] ORDER BY l.[{L_date}] DESC) AS rn
                    FROM dbo.Leads l
                    WHERE l.[{L_date}] >= ? AND l.[{L_date}] < ?
                      AND l.[{L_lender}] = ?
                      AND l.[{L_result}] = ?
                      AND l.[{L_camp}] IN ({ph})
                ) ranked
                WHERE rn <= ?
                """,
                [window_start, window_end, LENDER_ID, EXCLUDED_RESULT_ID, *chunk_ids, SAMPLE_PER_CAMPAIGN],
            )
            for lead_id, camp_id, sr1, phone, dob, email, gov_id, date_received, _rn in cur.fetchall():
                camp_int = int(camp_id) if camp_id is not None else None
                sr1_norm = (str(sr1).strip() if sr1 is not None else None) or None
                sampled.append({
                    "lead_id":  lead_id,
                    "camp_id":  camp_int,
                    "sr1":      sr1_norm,
                    "broker_id": campaign_to_source.get(camp_int) if camp_int is not None else None,
                    "phone":    (phone or "").strip() if phone else "",
                    "dob":      dob,   # native date object
                    "email":    (email or "").strip().lower() if email else "",
                    "gov_id":   (gov_id or "").strip() if gov_id else "",
                    "date":     date_received,
                })
    print(f"#   sampled rejected leads: {len(sampled):,}", flush=True)

    # Pull purchased leads (~3-5M rows). Need: ARef, CampaignId,
    # identity fields, paid_out flag, plus BidAmount + Date for the
    # duplicate-pricing analysis (Part C).
    purchased: list[dict] = []
    if L_phone and L_dob and L_aref:
        gov_sel = f"l.[{L_gov}]"  if L_gov  else "NULL"
        bid_sel = f"l.[{L_bid}]"  if L_bid  else "NULL"
        date_sel = f"l.[{L_date}]"
        print("# Part B: pulling purchased-side leads + paid-out status…", flush=True)
        sr1_sel = sr1_sel_l
        cur.execute(
            f"""
            SELECT l.[{L_aref}] AS ARef,
                   l.[{L_camp}] AS CampaignId,
                   {sr1_sel} AS SR1,
                   l.[{L_phone}] AS Phone,
                   l.[{L_dob}]   AS DOB,
                   l.[{L_email}] AS Email,
                   {gov_sel} AS GovId,
                   {bid_sel} AS BidAmount,
                   MIN({date_sel}) AS DateReceived,
                   MAX(CASE WHEN a.[{A_status}] = 5 THEN 1 ELSE 0 END) AS paid
            FROM dbo.Leads l
            INNER JOIN dbo.Applications a
                ON a.[{A_aref}] = l.[{L_aref}] AND a.[{A_lender}] = ?
            WHERE l.[{L_date}] >= ? AND l.[{L_date}] < ?
              AND l.[{L_lender}] = ?
              AND l.[{L_result}] IN ({",".join(str(x) for x in PURCHASED_RESULT_IDS)})
              AND l.[{L_aref}] IS NOT NULL
            GROUP BY l.[{L_aref}], l.[{L_camp}], {sr1_sel}, l.[{L_phone}], l.[{L_dob}], l.[{L_email}], {gov_sel}, {bid_sel}
            """,
            [LENDER_ID, window_start, window_end, LENDER_ID],
        )
        for aref, camp_id, sr1, phone, dob, email, gov_id, bid_amount, date_received, paid in cur.fetchall():
            camp_int = int(camp_id) if camp_id is not None else None
            sr1_norm = (str(sr1).strip() if sr1 is not None else None) or None
            purchased.append({
                "aref":     aref,
                "camp_id":  camp_int,
                "sr1":      sr1_norm,
                "src_id":   campaign_to_source.get(camp_int) if camp_int is not None else None,
                "phone":    (phone or "").strip() if phone else "",
                "dob":      dob,
                "email":    (email or "").strip().lower() if email else "",
                "gov_id":   (gov_id or "").strip() if gov_id else "",
                "bid":      float(bid_amount) if bid_amount is not None else None,
                "date":     date_received,
                "paid":     bool(paid),
            })
    print(f"#   purchased leads pulled: {len(purchased):,}", flush=True)

    # Build hash indexes for fast lookup
    by_phone_dob: dict[tuple, list] = {}
    by_email_dob: dict[tuple, list] = {}
    by_gov: dict[str, list] = {}
    for p in purchased:
        if p["phone"] and p["dob"] is not None:
            by_phone_dob.setdefault((p["phone"], p["dob"]), []).append(p)
        if p["email"] and p["dob"] is not None:
            by_email_dob.setdefault((p["email"], p["dob"]), []).append(p)
        if p["gov_id"]:
            by_gov.setdefault(p["gov_id"], []).append(p)
    print(f"#   indexes: phone+dob={len(by_phone_dob):,} email+dob={len(by_email_dob):,} gov_id={len(by_gov):,}", flush=True)

    # Match each sampled rejected lead. Temporal constraint: the
    # purchase via another (broker, SR1) cell must occur STRICTLY
    # AFTER the rejection and within BOUNCEBACK_WINDOW_DAYS. Otherwise
    # we'd count people we already had as our customers (purchased
    # earlier, rejected later) as if blocking them cost us.
    # "Same source" = same (broker, SR1) tuple; cross-cell bouncebacks
    # within the same broker still count if SR1 differs.
    bounceback_delta = datetime.timedelta(days=BOUNCEBACK_WINDOW_DAYS)
    for s in sampled:
        rej_broker = s.get("broker_id")
        rej_sr1 = s.get("sr1")
        rej_key = (rej_broker, rej_sr1)
        rej_date = s.get("date")
        if rej_date is None:
            continue
        matched_arefs: set = set()
        matched_paid: set = set()
        matched_destinations: dict[tuple, int] = {}
        matched_strategies: set = set()

        def _consume_matches(matches, strat):
            if not matches: return
            for p in matches:
                p_key = (p.get("src_id"), p.get("sr1"))
                if p_key == rej_key:
                    continue   # same (broker, SR1) — not a bounceback
                p_date = p.get("date")
                if p_date is None:
                    continue
                if p_date <= rej_date:
                    continue
                if (p_date - rej_date) > bounceback_delta:
                    continue
                matched_arefs.add(p["aref"])
                if p["paid"]:
                    matched_paid.add(p["aref"])
                matched_destinations[p_key] = matched_destinations.get(p_key, 0) + 1
                matched_strategies.add(strat)

        if s["gov_id"]:
            _consume_matches(by_gov.get(s["gov_id"]), "gov_id")
        if s["phone"] and s["dob"] is not None:
            _consume_matches(by_phone_dob.get((s["phone"], s["dob"])), "phone_dob")
        if s["email"] and s["dob"] is not None:
            _consume_matches(by_email_dob.get((s["email"], s["dob"])), "email_dob")

        if not matched_arefs:
            continue
        slot = _ensure_bs_slot(rej_broker, rej_sr1)
        slot["match_leadids"].add(s["lead_id"])
        slot["match_arefs"].update(matched_arefs)
        slot["match_paid_arefs"].update(matched_paid)
        for strat in matched_strategies:
            slot["by_strategy"][strat] += 1
        for d_key, n in matched_destinations.items():
            slot["destination_keys"][d_key] = slot["destination_keys"].get(d_key, 0) + n

    print(
        f"# matched {sum(len(s['match_leadids']) for s in bounceback_per_bs.values()):,} sampled rejected leads to purchased counterparts (keyed by broker+SR1)",
        flush=True,
    )

    conn.close()

    # ─── Finalise Part B output (keyed on broker, SR1) ────────────────
    blocked_rows = []
    blocked_dropped_unsourced = 0
    for (broker_id, sr1), slot in bounceback_per_bs.items():
        if sr1 is None:
            blocked_dropped_unsourced += 1
            continue
        excluded = slot["excluded_count"]
        if excluded < MIN_EXCLUDED:
            continue
        bounce_n = len(slot["match_leadids"])
        bounce_ar = len(slot["match_arefs"])
        paid_n = len(slot["match_paid_arefs"])
        dest_rows = sorted(slot["destination_keys"].items(), key=lambda kv: -kv[1])[:5]
        top_destinations = [
            {
                "broker_id":   dk[0],
                "broker_name": _broker_name(dk[0]),
                "source_ref1": dk[1],
                "bounced_count": n,
            }
            for dk, n in dest_rows
        ]
        blocked_rows.append({
            "broker_id":            broker_id,
            "broker_name":          slot["broker_name"],
            "source_ref1":          sr1,
            "excluded_count":       excluded,
            "bounceback_leads":     bounce_n,
            "bounceback_arefs":     bounce_ar,
            "bounceback_paid":      paid_n,
            "bounceback_rate":      (bounce_n / excluded) if excluded else None,
            "bounceback_paid_rate": (paid_n / bounce_ar) if bounce_ar else None,
            "bounceback_paid_per_excluded": (paid_n / excluded) if excluded else None,
            "by_strategy":          slot["by_strategy"],
            "top_destinations":     top_destinations,
        })

    blocked_rows.sort(key=lambda r: -(r.get("bounceback_paid") or 0))
    print(f"# Blocked (Broker, SR1) cells: {len(blocked_rows)} kept ({blocked_dropped_unsourced} dropped as no-Source)", flush=True)

    # ─── Part C: duplicate-pricing analysis ───────────────────────────
    # Question this answers: "Same person bought expensive via campaign X
    # at $3 — do they later show up cheaper via another campaign at $1?
    # If yes, we overpaid; we could have waited."
    #
    # Only meaningful for upfront-paid commission models (CPL + BID).
    # CPF/REV cost nothing per-lead, only on funding — so those leads
    # being duplicated has no overpay angle.
    print("# Part C: duplicate-pricing analysis on purchased leads…", flush=True)

    def _per_lead_cost(p) -> float | None:
        meta = campaign_meta.get(p["camp_id"]) or {}
        ct = str(meta.get("commission_type"))
        rate = meta.get("commission_rate") or 0
        if ct == "4":
            # Auction-priced: actual bid if recorded, else floor rate
            return p.get("bid") if (p.get("bid") is not None and p.get("bid") > 0) else rate
        if ct == "2":
            return rate
        return None

    by_aref = {p["aref"]: p for p in purchased}
    clone_stats: dict[tuple, dict] = {}   # keyed on (broker_id, sr1) of first buy
    overspend_total = 0.0
    scanned = 0
    for p in purchased:
        cost_here = _per_lead_cost(p)
        if cost_here is None or cost_here <= 0 or p.get("date") is None:
            continue
        scanned += 1
        p_key = (p.get("src_id"), p.get("sr1"))
        match_arefs: set = set()
        if p["gov_id"]:
            for m in by_gov.get(p["gov_id"], []):
                match_arefs.add(m["aref"])
        if p["phone"] and p["dob"] is not None:
            for m in by_phone_dob.get((p["phone"], p["dob"]), []):
                match_arefs.add(m["aref"])
        if p["email"] and p["dob"] is not None:
            for m in by_email_dob.get((p["email"], p["dob"]), []):
                match_arefs.add(m["aref"])
        match_arefs.discard(p["aref"])
        if not match_arefs:
            continue

        cheapest_later: float | None = None
        cheapest_date = None
        for a in match_arefs:
            m = by_aref.get(a)
            if not m or m.get("date") is None:
                continue
            if m["date"] <= p["date"]:
                continue
            # Cross-cell only — same (broker, sr1) doesn't count as a
            # cheaper-elsewhere opportunity.
            if (m.get("src_id"), m.get("sr1")) == p_key:
                continue
            mc = _per_lead_cost(m)
            if mc is None:
                continue
            if mc < cost_here and (cheapest_later is None or mc < cheapest_later):
                cheapest_later = mc
                cheapest_date = m["date"]

        if cheapest_later is None:
            continue
        savings = cost_here - cheapest_later
        wait_days = (cheapest_date - p["date"]).days if cheapest_date and p["date"] else 0
        overspend_total += savings
        slot = clone_stats.setdefault(p_key, {
            "broker_id":     p_key[0],
            "broker_name":   _broker_name(p_key[0]),
            "source_ref1":   p_key[1],
            "_savings_list": [],
            "_wait_list":    [],
        })
        slot["_savings_list"].append(savings)
        slot["_wait_list"].append(wait_days)

    cheaper_rows = []
    cheaper_dropped_unsourced = 0
    for key, slot in clone_stats.items():
        if key[1] is None:
            cheaper_dropped_unsourced += 1
            continue
        sav = slot.pop("_savings_list")
        wait = slot.pop("_wait_list")
        n = len(sav)
        total_savings = sum(sav)
        # leads_purchased for this (broker, sr1) comes from the
        # broker_sr1_raw aggregation above (NOT campaign-level).
        total_leads = (broker_sr1_raw.get(key, {}) or {}).get("leads_purchased") or 0
        slot.update({
            "leads_with_cheaper_later": n,
            "total_savings_if_waited":  total_savings,
            "avg_savings_per_lead":     total_savings / n if n else 0,
            "median_savings_per_lead":  statistics.median(sav) if sav else 0,
            "avg_wait_days":            sum(wait) / n if n else 0,
            "median_wait_days":         statistics.median(wait) if wait else 0,
            "leads_purchased":          total_leads,
            "cheaper_clone_rate":       (n / total_leads) if total_leads else None,
        })
        cheaper_rows.append(slot)

    cheaper_rows.sort(key=lambda r: -(r.get("total_savings_if_waited") or 0))
    print(
        f"#   scanned {scanned:,} upfront-paid leads; cheaper-later clone on "
        f"{sum(r['leads_with_cheaper_later'] for r in cheaper_rows):,} leads across "
        f"{len(cheaper_rows)} (broker, SR1) cells "
        f"({cheaper_dropped_unsourced} no-Source cells dropped); "
        f"total overspend: ${overspend_total:,.0f}",
        flush=True,
    )

    # ─── Finalise Part A output ───────────────────────────────────────
    # Drop rows where weak_data is missing leads_purchased (means they had
    # apps without a Lead row, edge case).
    weak_rows = sorted(
        [s for s in qualifying if s.get("leads_purchased")],
        key=lambda s: s.get("paid_out_rate") or 0,
    )

    output = {
        "snapshot_at":   started.isoformat(),
        "snapshot_date": started.date().isoformat(),
        "lender_id":     LENDER_ID,
        "lender_label":  LENDER_LABEL,
        "window_days":   WINDOW_DAYS,
        "window_start":  window_start.date().isoformat(),
        "window_end":    window_end.date().isoformat(),
        "maturation_days": MATURATION_DAYS,
        "bounceback_window_days": BOUNCEBACK_WINDOW_DAYS,
        "min_volume_for_ranking":   MIN_VOLUME,
        "min_excluded_for_ranking": MIN_EXCLUDED,
        "weak_accepted": {
            "median_paid_out_rate":  median_rate,
            "q1_paid_out_rate":      q1_rate,
            "qualifying_sources":    len(qualifying),
            "sources":               weak_rows,            # campaign-level (legacy detail)
            "by_broker_source":      bs_qualifying,        # (Broker, SR1) rollup — primary
        },
        "blocked_to_reconsider":   blocked_rows,           # now keyed by (Broker, SR1)
        "cheaper_clones": {
            "total_overspend":      overspend_total,
            "leads_with_cheaper":   sum(r["leads_with_cheaper_later"] for r in cheaper_rows),
            "by_broker_source":     cheaper_rows,          # (Broker, SR1) — primary
        },
    }
    out_path = Path("source-quality.json")
    out_path.write_text(json.dumps(output, indent=2, default=str))
    print(
        f"# wrote {out_path} ({out_path.stat().st_size:,} bytes); "
        f"{len(weak_rows)} ranked accepted sources; "
        f"{len(blocked_rows)} blocked sources with bounceback data",
        flush=True,
    )


if __name__ == "__main__":
    main()
