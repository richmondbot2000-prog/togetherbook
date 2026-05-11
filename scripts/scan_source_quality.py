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
    window_end = started
    window_start = started - datetime.timedelta(days=WINDOW_DAYS)
    print(
        f"# scan_source_quality start {started.isoformat()}  "
        f"window: {window_start.date()} → {window_end.date()} ({WINDOW_DAYS}d)  "
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

    A_aref = pick(apps_cols, "ARef")
    A_lender = pick(apps_cols, "LenderId")
    A_status = pick(apps_cols, "ApplicationStatusTypeId", "ApplicationStatusId")

    print(
        f"# Leads cols: aref={L_aref} lender={L_lender} date={L_date} "
        f"result={L_result} camp={L_camp} phone={L_phone} email={L_email} "
        f"dob={L_dob} gov={L_gov} nat={L_nat} leadid={L_leadid}",
        flush=True,
    )

    # ─── Brokers.Sources + Campaigns lookups ──────────────────────────
    # In the database, `Sources` rows are companies (we call them brokers
    # in the user-facing terminology), and `Campaigns` rows are the
    # granular per-source-code level beneath each broker — which is what
    # the user calls "sources". This scanner reports at the Campaign
    # level (the user's source), with the parent broker on each row.
    sources: dict[int, dict] = {}         # broker_id (DB SourceId) → {friendly_name}
    campaign_to_source: dict[int, int] = {}   # campaign_id → broker_id
    campaign_meta: dict[int, dict] = {}   # campaign_id → {campaign_id, broker_id, source_name}

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
            scols = discover_columns(cur2, "Sources")
            if scols and not sources:
                sid = pick(scols, "SourceId", "SourceID")
                snm = pick(scols, "FriendlyName", "ShortName", "CompanyName", "Name")
                slender = pick(scols, "LenderId")
                if sid and snm:
                    sel = ", ".join([f"[{sid}]", f"[{snm}]",
                                     f"[{slender}]" if slender else "NULL"])
                    cur2.execute(f"SELECT {sel} FROM dbo.Sources")
                    for i, n, lid in cur2.fetchall():
                        if i is None: continue
                        if lid is not None and slender and int(lid) != LENDER_ID:
                            continue
                        sources[int(i)] = {
                            "source_id":     int(i),
                            "friendly_name": (str(n).strip() if n is not None else "") or f"Source {i}",
                        }
                    print(f"# Sources from {database}: {len(sources)}", flush=True)
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

    # ─── Part A: weak-accepted SOURCE ranking ─────────────────────────
    # The user's terminology: a source = one of our CampaignId rows, sitting
    # beneath a broker. Aggregation is per CampaignId.
    print("# Part A: per-source (CampaignId) purchase + paid_out counts", flush=True)
    cur.execute(
        f"""
        WITH purchased AS (
            SELECT l.[{L_aref}] AS ARef, MAX(l.[{L_camp}]) AS CampaignId
            FROM dbo.Leads l
            WHERE l.[{L_date}] >= ? AND l.[{L_date}] < ?
              AND l.[{L_lender}] = ?
              AND l.[{L_result}] IN ({",".join(str(x) for x in PURCHASED_RESULT_IDS)})
              AND l.[{L_aref}] IS NOT NULL
            GROUP BY l.[{L_aref}]
        ),
        with_status AS (
            SELECT p.CampaignId,
                   p.ARef,
                   MAX(CASE WHEN a.[{A_status}] = 5 THEN 1 ELSE 0 END) AS paid
            FROM purchased p
            INNER JOIN dbo.Applications a ON a.[{A_aref}] = p.ARef AND a.[{A_lender}] = ?
            GROUP BY p.CampaignId, p.ARef
        )
        SELECT CampaignId,
               COUNT(*) AS apps,
               SUM(paid) AS paid_out
        FROM with_status
        GROUP BY CampaignId
        """,
        [window_start, window_end, LENDER_ID, LENDER_ID],
    )
    accepted_per_campaign = {
        (int(cid) if cid is not None else None): (int(apps), int(paid or 0))
        for cid, apps, paid in cur.fetchall()
    }
    print(f"#   campaigns with purchased apps: {len(accepted_per_campaign):,}", flush=True)

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
    for cid, (apps, paid) in accepted_per_campaign.items():
        slot = weak_data.setdefault(cid, _new_weak_slot(cid))
        slot["applications"] += apps
        slot["paid_out"]     += paid

    # We also need leads_purchased per campaign AND total spend (SUM of
    # Leads.BidAmount) — same query, two aggregations.
    bid_sql = f", SUM(CAST(l.[{L_bid}] AS FLOAT)) AS total_cost" if L_bid else ", NULL AS total_cost"
    cur.execute(
        f"""
        SELECT l.[{L_camp}], COUNT(*) AS purchased{bid_sql}
        FROM dbo.Leads l
        WHERE l.[{L_date}] >= ? AND l.[{L_date}] < ?
          AND l.[{L_lender}] = ?
          AND l.[{L_result}] IN ({",".join(str(x) for x in PURCHASED_RESULT_IDS)})
        GROUP BY l.[{L_camp}]
        """,
        [window_start, window_end, LENDER_ID],
    )
    for cid, n, total_cost in cur.fetchall():
        cid_int = int(cid) if cid is not None else None
        slot = weak_data.setdefault(cid_int, _new_weak_slot(cid_int))
        slot["leads_purchased"] = slot.get("leads_purchased", 0) + int(n)
        if total_cost is not None:
            slot["total_cost"] = slot.get("total_cost", 0.0) + float(total_cost)

    # Compute paid_out_rate + cost metrics per campaign and stats across
    # the qualifying cohort.
    # CommissionType '3' is CPC (cost-per-click PPC spend) — not a broker
    # lead, so exclude those campaigns from the source-quality scorecard.
    CPC_COMMISSION_TYPE = "3"
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
        total_cost = slot.get("total_cost")
        if total_cost is not None and slot["leads_purchased"]:
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

    # Now the heavy join. We do three separate joins (one per identity
    # strategy) and union the results — SQL Server's optimiser handles
    # this far better than a single OR-joined condition.
    # Output: per excluded-SourceId, how many distinct people we later
    # purchased via OTHER sources, and how many of those paid out.
    print("# Part B: running 3-way identity-match join (this is the heavy one)…", flush=True)

    bounceback_per_source: dict[int | None, dict] = {}

    def _ensure_b_slot(cid):   # keyed on CampaignId (user's "source")
        if cid not in bounceback_per_source:
            bounceback_per_source[cid] = {
                "campaign_id":     cid,
                "source_name":     _source_name(cid),
                "broker_id":       campaign_to_source.get(cid) if cid is not None else None,
                "broker_name":     _broker_name(campaign_to_source.get(cid) if cid is not None else None),
                "excluded_count":  excluded_per_campaign.get(cid, 0),
                "match_leadids":   set(),
                "match_arefs":     set(),
                "match_paid_arefs": set(),
                "by_strategy":     {"gov_id": 0, "phone_dob": 0, "email_dob": 0},
                "destination_campaigns": {},   # other campaign_id → bounceback count
            }
        return bounceback_per_source[cid]

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

    # Build the list of candidate CampaignIds: those whose SourceId rolls
    # up to one of our >=MIN_EXCLUDED-excluded sources.
    candidate_source_ids = set(candidate_sources.keys())
    candidate_campaign_ids = list({
        cid for cid, sid in campaign_to_source.items()
        if sid in candidate_source_ids
    })
    # If 'Unknown source' (None) is itself a candidate (i.e. lots of
    # source-excluded leads came in via campaigns we couldn't map), pick
    # up the unmapped CampaignIds via a separate query.
    if None in candidate_source_ids:
        cur.execute(
            f"""
            SELECT DISTINCT [{L_camp}]
            FROM dbo.Leads
            WHERE [{L_date}] >= ? AND [{L_date}] < ?
              AND [{L_lender}] = ?
              AND [{L_result}] = ?
              AND [{L_camp}] IS NOT NULL
            """,
            [window_start, window_end, LENDER_ID, EXCLUDED_RESULT_ID],
        )
        for (cid,) in cur.fetchall():
            cid_int = int(cid)
            if campaign_to_source.get(cid_int) is None:
                candidate_campaign_ids.append(cid_int)
        candidate_campaign_ids = list(set(candidate_campaign_ids))
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
                           l.[{L_phone}]  AS Phone,
                           l.[{L_dob}]    AS DOB,
                           l.[{L_email}]  AS Email,
                           {f"l.[{L_gov}]"  if L_gov  else "NULL"} AS GovId,
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
            for lead_id, camp_id, phone, dob, email, gov_id, _rn in cur.fetchall():
                sampled.append({
                    "lead_id":  lead_id,
                    "camp_id":  int(camp_id) if camp_id is not None else None,
                    "phone":    (phone or "").strip() if phone else "",
                    "dob":      dob,   # native date object
                    "email":    (email or "").strip().lower() if email else "",
                    "gov_id":   (gov_id or "").strip() if gov_id else "",
                })
    print(f"#   sampled rejected leads: {len(sampled):,}", flush=True)

    # Pull purchased leads (~3-5M rows). Need: ARef, CampaignId,
    # identity fields, paid_out flag.
    purchased: list[dict] = []
    if L_phone and L_dob and L_aref:
        gov_sel = f"l.[{L_gov}]" if L_gov else "NULL"
        print("# Part B: pulling purchased-side leads + paid-out status…", flush=True)
        cur.execute(
            f"""
            SELECT l.[{L_aref}] AS ARef,
                   l.[{L_camp}] AS CampaignId,
                   l.[{L_phone}] AS Phone,
                   l.[{L_dob}]   AS DOB,
                   l.[{L_email}] AS Email,
                   {gov_sel} AS GovId,
                   MAX(CASE WHEN a.[{A_status}] = 5 THEN 1 ELSE 0 END) AS paid
            FROM dbo.Leads l
            INNER JOIN dbo.Applications a
                ON a.[{A_aref}] = l.[{L_aref}] AND a.[{A_lender}] = ?
            WHERE l.[{L_date}] >= ? AND l.[{L_date}] < ?
              AND l.[{L_lender}] = ?
              AND l.[{L_result}] IN ({",".join(str(x) for x in PURCHASED_RESULT_IDS)})
              AND l.[{L_aref}] IS NOT NULL
            GROUP BY l.[{L_aref}], l.[{L_camp}], l.[{L_phone}], l.[{L_dob}], l.[{L_email}], {gov_sel}
            """,
            [LENDER_ID, window_start, window_end, LENDER_ID],
        )
        for aref, camp_id, phone, dob, email, gov_id, paid in cur.fetchall():
            purchased.append({
                "aref":     aref,
                "camp_id":  int(camp_id) if camp_id is not None else None,
                "src_id":   campaign_to_source.get(int(camp_id)) if camp_id is not None else None,
                "phone":    (phone or "").strip() if phone else "",
                "dob":      dob,
                "email":    (email or "").strip().lower() if email else "",
                "gov_id":   (gov_id or "").strip() if gov_id else "",
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

    # Match each sampled rejected lead. "Same source" = same CampaignId
    # (the user's source granularity). A bounceback within the same
    # broker but a different source still counts — it's still a sign
    # the audience is decent.
    for s in sampled:
        rej_camp = s["camp_id"]
        matched_arefs: set = set()
        matched_paid: set = set()
        matched_destinations: dict[int | None, int] = {}
        matched_strategies: set = set()

        def _consume_matches(matches, strat):
            if not matches: return
            for p in matches:
                if p["camp_id"] == rej_camp and rej_camp is not None:
                    continue   # same source (same CampaignId) — not a bounceback
                matched_arefs.add(p["aref"])
                if p["paid"]:
                    matched_paid.add(p["aref"])
                matched_destinations[p["camp_id"]] = matched_destinations.get(p["camp_id"], 0) + 1
                matched_strategies.add(strat)

        if s["gov_id"]:
            _consume_matches(by_gov.get(s["gov_id"]), "gov_id")
        if s["phone"] and s["dob"] is not None:
            _consume_matches(by_phone_dob.get((s["phone"], s["dob"])), "phone_dob")
        if s["email"] and s["dob"] is not None:
            _consume_matches(by_email_dob.get((s["email"], s["dob"])), "email_dob")

        if not matched_arefs:
            continue
        slot = _ensure_b_slot(rej_camp)
        slot["match_leadids"].add(s["lead_id"])
        slot["match_arefs"].update(matched_arefs)
        slot["match_paid_arefs"].update(matched_paid)
        for strat in matched_strategies:
            slot["by_strategy"][strat] += 1
        for d_camp, n in matched_destinations.items():
            if d_camp is not None:
                slot["destination_campaigns"][d_camp] = slot["destination_campaigns"].get(d_camp, 0) + n

    print(f"# matched {sum(len(s['match_leadids']) for s in bounceback_per_source.values()):,} sampled rejected leads to purchased counterparts", flush=True)

    conn.close()

    # ─── Finalise Part B output ───────────────────────────────────────
    blocked_rows = []
    for cid, slot in bounceback_per_source.items():
        excluded = slot["excluded_count"]
        if excluded < MIN_EXCLUDED:
            continue
        bounce_n = len(slot["match_leadids"])
        bounce_ar = len(slot["match_arefs"])
        paid_n = len(slot["match_paid_arefs"])
        # Top 5 destination CAMPAIGNS (the user's "sources") that recaptured these people
        dest_rows = sorted(slot["destination_campaigns"].items(), key=lambda kv: -kv[1])[:5]
        top_destinations = [
            {
                "campaign_id": d_cid,
                "source_name": _source_name(d_cid),
                "broker_id":   campaign_to_source.get(d_cid) if d_cid is not None else None,
                "broker_name": _broker_name(campaign_to_source.get(d_cid) if d_cid is not None else None),
                "bounced_count": n,
            }
            for d_cid, n in dest_rows
        ]
        blocked_rows.append({
            "campaign_id":          slot["campaign_id"],
            "source_name":          slot["source_name"],
            "broker_id":            slot["broker_id"],
            "broker_name":          slot["broker_name"],
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

    # Sort by paid-per-excluded descending — sources where blocking is costing us
    # the most paid loans rise to the top.
    blocked_rows.sort(key=lambda r: -(r.get("bounceback_paid_per_excluded") or 0))

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
        "min_volume_for_ranking":   MIN_VOLUME,
        "min_excluded_for_ranking": MIN_EXCLUDED,
        "weak_accepted": {
            "median_paid_out_rate":  median_rate,
            "q1_paid_out_rate":      q1_rate,
            "qualifying_sources":    len(qualifying),
            "sources":               weak_rows,
        },
        "blocked_to_reconsider":    blocked_rows,
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
