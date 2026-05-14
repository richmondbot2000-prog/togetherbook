"""
Build per-endpoint sample customer interaction histories for the Pipeline page.

For each "dead end" on the application-progression Sankey (the red drop nodes
where a customer's journey with us ended), pick 25 random ARefs that ended
there and pull their full interaction timeline from the warehouse.

Event sources:
  - Applications (creation event + BRW Apply1 details: name, address, loan)
  - Tasks (filtered to user-meaningful milestones, with Description as label)
  - ESignatures (joined via Applications.BrwEsignatureId / GtEsignatureId)
  - WebBehaviours (web visits)
  - Communications.Messages (inbound + outbound)

Robot-typed outbound messages get redacted to a one-line stub:
  "<date, time, Message from {ClientType}Bot>".
Inbound messages and outbound messages from a human agent keep their body.

Endpoints (strict mutual exclusion — each customer = furthest stage reached):
  - abandoned_before_page1     — App started, no Task 41 GtRef=null done
  - dropped_before_brw_signed  — Apply1 done, no Task 48 GtRef=null done
  - no_accepted_guarantor      — BRW signed, no Task 54 GtRef!=null done
  - no_vc_reached              — GT credit check done, no Task 62/146 GT done
  - vc_ready_not_paid_out      — GT VC done, ApplicationStatusTypeId != 5

Output: `pipeline-samples.json` at repo root.

Required env vars: FABRIC_SQL_ENDPOINT, FABRIC_TENANT_ID, FABRIC_CLIENT_ID,
FABRIC_CLIENT_SECRET.
"""
from __future__ import annotations

import datetime
import json
import os
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

import pyodbc

LENDER_ID = 6
LENDER_LABEL = "Transform Credit (LenderId 6, USA)"
WINDOW_YEAR = 2026
WINDOW_MONTH = 3
SAMPLE_SIZE = 25
RNG_SEED = 20260510  # deterministic so re-runs produce the same sample

QUERY_TIMEOUT = 600

# Robots: ClientType values whose outbound messages get redacted to a
# one-line stub. Anything else (TogetherLoansCRM = human agent in CRM,
# TogetherLoansWebsite = the customer themselves, etc.) keeps its body.
ROBOT_CLIENT_TYPES = {
    "MessageFactory",
    "AutoReconcileProcessor",
    "RobotResponders",
    "MessageReplyBot",
    "AutoCollectCards",
    "MFSenderRobot",
    "PaymentsFactory",
    "CardDeactivator",
    "DailyUpdate",
    "MiniUpdate",
    "MonitorRobot",
    "WhiteboxRun",
    "TogetherLoansWhitebox",
    "SensitiveDataDeleter",
    "EmailInWebjob",
    "TranscriptionRobot",
}

# Fallback labels for TaskTypeIds when the TaskTypes lookup table isn't
# available. Used only if dbo.TaskTypes has no row for an id we encounter.
TASK_FALLBACK_LABELS: dict[int, str] = {
    41:  "Page 1 details (Apply1)",
    48:  "Sign contract",
    49:  "Bank linked",
    54:  "Credit check",
    55:  "Columbo page",
    57:  "Card linked",
    62:  "Verbal contract (GT)",
    63:  "Payout",
    65:  "Caseworker review",
    73:  "Fraud check",
    103: "Bank check",
    127: "Payout approved",
    134: "Direct ID review",
    135: "Payout ready",
    138: "ID verify",
    144: "Pending payoff",
    146: "Verbal contract (BRW)",
    149: "Bank check (savings)",
    150: "Verbal contract (top-up)",
    173: "Verbal contract (medallion)",
}

# If 4+ whitelisted tasks for the same ARef have DateCompletedUtc within
# CLUSTER_WINDOW_S of each other, we collapse them into a single
# "Application closed" pseudo-event so the timeline doesn't show 30
# entries all at exactly the same minute.
CLUSTER_WINDOW_S = 90
CLUSTER_MIN_TASKS = 4


# ───────────────────── connection helpers ─────────────────────────────

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


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def iso(d) -> str | None:
    if d is None:
        return None
    if hasattr(d, "isoformat"):
        return d.isoformat()
    return str(d)


# Token expansions for the message-factory campaign suffixes:
#   (B)→BRW (G)→GT  (S)→SMS  (E)→Email  (D0..D30)→DSIT0..30
# Anything we don't recognise is passed through verbatim. Bracketing is
# stripped — "INVITE GT (B)(S)(D1)(CST)" becomes "INVITE GT BRW SMS DSIT1 CST".
def expand_campaign_tokens(name: str | None) -> tuple[str, str | None]:
    """Return (expanded_name, derived_channel) where derived_channel is
    'SMS', 'Email' or None depending on campaign code."""
    if not name:
        return "", None
    channel: str | None = None

    def replace_one(m):
        nonlocal channel
        tok = m.group(1).strip()
        u = tok.upper()
        if u == "B":  return "BRW"
        if u == "G":  return "GT"
        if u == "S":
            channel = "SMS"
            return "SMS"
        if u == "E":
            channel = "Email"
            return "Email"
        m2 = re.fullmatch(r"D(\d{1,2})", u)
        if m2:
            return f"DSIT{m2.group(1)}"
        return tok

    expanded = re.sub(r"\(([^)]+)\)", lambda m: " " + replace_one(m), name)
    expanded = re.sub(r"\s+", " ", expanded).strip()
    return expanded, channel


def format_web_event(blob, provider) -> str:
    """Render a WebBehaviours.WebEventObject value as a single-line label.

    In practice this column often just holds the customer's ARef as a bare
    string and Provider holds the page/form name (e.g. 'brdetails',
    'gtdetails', 'apply'). When that's the case, the page name is the
    useful part — collapse to just "Web visit: brdetails".
    """
    page = (provider or "").strip() or None
    if blob is None:
        return f"Web visit: {page}" if page else "Web visit"
    raw = str(blob).strip()
    if not raw:
        return f"Web visit: {page}" if page else "Web visit"

    # If the blob is just the customer's ARef (22 hex chars) or any
    # short opaque token, ignore it and lean on the Provider.
    if raw.replace('"', "").isalnum() and len(raw) < 30:
        return f"Web visit: {page}" if page else f"Web visit ({raw})"

    # Try JSON. Pull a couple of useful keys if it's a dict.
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict):
        bits = []
        for key in ("event", "eventName", "EventName", "page", "pageUrl",
                    "PageUrl", "url", "Url", "path", "Path", "name", "Name",
                    "type", "action"):
            v = obj.get(key)
            if v and str(v) not in bits:
                bits.append(str(v))
            if len(bits) >= 3:
                break
        if bits:
            label = " · ".join(bits)
            return f"Web: {label}" + (f" [{page}]" if page else "")
    # Fallback: short raw snippet + page tag.
    snippet = raw[:120]
    return f"Web: {snippet}" + (f" [{page}]" if page else "")


# ─────────────────────── main ──────────────────────────────────────────

def main() -> None:
    started = datetime.datetime.now(datetime.timezone.utc)
    month_start = datetime.date(WINDOW_YEAR, WINDOW_MONTH, 1)
    if WINDOW_MONTH == 12:
        month_end = datetime.date(WINDOW_YEAR + 1, 1, 1)
    else:
        month_end = datetime.date(WINDOW_YEAR, WINDOW_MONTH + 1, 1)
    print(f"# scan_pipeline_samples start {started.isoformat()}  window: {month_start} → {month_end}", flush=True)

    rng = random.Random(RNG_SEED)

    # ─────────────────────────────────────────────────────────────────
    # Phase 1 — March cohort + stage reach + bucketing
    # ─────────────────────────────────────────────────────────────────
    apps_conn = pyodbc.connect(conn_str("ReportingApplications"), timeout=20)
    apps_conn.timeout = QUERY_TIMEOUT
    cur = apps_conn.cursor()

    apps_cols = discover_columns(cur, "Applications")
    tasks_cols = discover_columns(cur, "Tasks")
    customers_cols = discover_columns(cur, "Customers")
    addresses_cols = discover_columns(cur, "Addresses")
    es_cols = discover_columns(cur, "ESignatures")
    wb_cols = discover_columns(cur, "WebBehaviours")

    # Try to load a LoanPurpose lookup so the timeline shows English instead
    # of a numeric type id. Look for any lookup table whose name contains
    # "purpose" / "reason" and has both an Id-like and Name-like column.
    loan_purpose_labels: dict[int, str] = {}
    cur.execute(
        """
        SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = 'dbo'
          AND (TABLE_NAME LIKE '%urpose%' OR TABLE_NAME LIKE '%LoanReason%')
        """
    )
    purpose_tables = [r[0] for r in cur.fetchall()]
    print(f"# Candidate loan-purpose lookup tables: {purpose_tables}", flush=True)
    for tname in purpose_tables:
        try:
            tcols = discover_columns(cur, tname)
            id_col = pick(tcols, "LoanPurposeTypeId", "LoanPurposeId", "Id", "TypeId")
            name_col = pick(tcols, "LoanPurposeDescription", "PurposeDescription",
                            "PurposeName", "Description", "Name", "Label",
                            "DisplayName", "Text")
            if not (id_col and name_col):
                print(f"#   {tname}: missing id/name pair (cols={sorted(tcols)})", flush=True)
                continue
            cur.execute(f"SELECT [{id_col}], [{name_col}] FROM dbo.[{tname}]")
            for tid, nm in cur.fetchall():
                if tid is None: continue
                loan_purpose_labels[int(tid)] = str(nm) if nm is not None else f"#{tid}"
            print(f"#   {tname}: loaded {len(loan_purpose_labels)} purpose labels", flush=True)
            for k in sorted(loan_purpose_labels.keys()):
                print(f"#     {k:4d}: {loan_purpose_labels[k]!r}", flush=True)
            if loan_purpose_labels:
                break
        except Exception as e:
            print(f"#   {tname}: lookup failed: {e}", flush=True)

    # ──────── TaskTypes lookup ─────────────────────────────────────
    # Per the Applications schema diagram: dbo.TaskTypes has
    # (TaskTypeId, TaskName, ParentTaskTypeId, SubTaskName).
    task_type_labels: dict[int, str] = {}
    try:
        tt_cols = discover_columns(cur, "TaskTypes")
    except Exception:
        tt_cols = set()
    if tt_cols:
        tt_id = pick(tt_cols, "TaskTypeId", "TaskTypeID", "Id")
        tt_name = pick(tt_cols, "TaskName", "Name")
        tt_sub = pick(tt_cols, "SubTaskName", "SubName")
        if tt_id and tt_name:
            sub_sql = f", [{tt_sub}]" if tt_sub else ", NULL"
            cur.execute(f"SELECT [{tt_id}], [{tt_name}]{sub_sql} FROM dbo.TaskTypes")
            for tid, name, sub in cur.fetchall():
                if tid is None: continue
                n = (name or "").strip()
                s = (sub or "").strip() if sub else ""
                if n and s and s.lower() != "all":
                    label = f"{n}: {s}"
                elif n:
                    label = n
                else:
                    label = f"Task #{tid}"
                task_type_labels[int(tid)] = label
            print(f"# TaskTypes: loaded {len(task_type_labels)} labels", flush=True)
    else:
        print("# TaskTypes table not found — falling back to hardcoded labels", flush=True)

    # ──────── Brokers Sources + Campaigns lookups ──────────────────
    # Sources.FriendlyName turns 'broker 421' into the affiliate's name.
    # Brokers.Campaigns.CampaignFriendlyName gives the campaign label.
    # BrokerStatuses.BrokerStatusDescription resolves the broker status code.
    broker_sources: dict[int, str] = {}
    broker_campaigns: dict[int, dict] = {}
    broker_statuses: dict[int, str] = {}

    def _try_load_brokers(database: str) -> bool:
        try:
            probe = pyodbc.connect(conn_str(database), timeout=10)
            probe.timeout = 60
        except Exception as e:
            print(f"# Brokers probe in {database}: connect failed ({e})", flush=True)
            return False
        loaded_any = False
        try:
            pc = probe.cursor()
            # Sources
            if not broker_sources:
                cols = discover_columns(pc, "Sources")
                if cols:
                    sid = pick(cols, "SourceId", "SourceID")
                    sname = pick(cols, "FriendlyName", "ShortName", "CompanyName", "Name")
                    if sid and sname:
                        pc.execute(f"SELECT [{sid}], [{sname}] FROM dbo.Sources")
                        for i, n in pc.fetchall():
                            if i is not None: broker_sources[int(i)] = (n or "").strip() or f"Source {i}"
                        print(f"# Brokers.Sources in {database}: loaded {len(broker_sources)} rows", flush=True)
                        loaded_any = True
            # Campaigns (Brokers namespace — has CampaignFriendlyName, not MessageType)
            if not broker_campaigns:
                cols = discover_columns(pc, "Campaigns")
                if cols:
                    cid = pick(cols, "CampaignId", "CampaignID")
                    cname = pick(cols, "CampaignFriendlyName", "CampaignName", "FriendlyName")
                    csrc = pick(cols, "SourceId", "SourceID")
                    if cid and cname:
                        src_sql = f", [{csrc}]" if csrc else ", NULL"
                        pc.execute(f"SELECT [{cid}], [{cname}]{src_sql} FROM dbo.Campaigns")
                        for i, n, s in pc.fetchall():
                            if i is None: continue
                            broker_campaigns[int(i)] = {
                                "name": (n or "").strip() or None,
                                "source_id": int(s) if s is not None else None,
                            }
                        if broker_campaigns:
                            print(f"# Brokers.Campaigns in {database}: loaded {len(broker_campaigns)} rows", flush=True)
                            loaded_any = True
            # BrokerStatuses
            if not broker_statuses:
                cols = discover_columns(pc, "BrokerStatuses")
                if cols:
                    bsid = pick(cols, "BrokerStatusId", "BrokerStatusID", "Id")
                    bsname = pick(cols, "BrokerStatusDescription", "Description", "Name")
                    if bsid and bsname:
                        pc.execute(f"SELECT [{bsid}], [{bsname}] FROM dbo.BrokerStatuses")
                        for i, n in pc.fetchall():
                            if i is not None: broker_statuses[int(i)] = (n or "").strip() or f"Status {i}"
                        print(f"# Brokers.BrokerStatuses in {database}: loaded {len(broker_statuses)} rows", flush=True)
                        loaded_any = True
        finally:
            probe.close()
        return loaded_any

    for db_candidate in ("ReportingBrokers", "ReportingApplications"):
        _try_load_brokers(db_candidate)
        if broker_sources and broker_campaigns and broker_statuses:
            break

    apps_aref = pick(apps_cols, "ARef")
    apps_lender = pick(apps_cols, "LenderId")
    apps_date = pick(apps_cols, "DateCreatedUtc", "InterestingDateTimeUtc", "InterestingDateTimeUTC")
    apps_status = pick(apps_cols, "ApplicationStatusTypeId", "ApplicationStatusId")
    apps_leadid = pick(apps_cols, "LeadId", "LeadID")
    apps_brw_cust = pick(apps_cols, "BrwCustomerId")
    apps_gt_cust = pick(apps_cols, "GtCustomerId")
    apps_brw_es = pick(apps_cols, "BrwEsignatureId")
    apps_gt_es = pick(apps_cols, "GtEsignatureId")
    apps_loan_amt = pick(apps_cols, "LoanAmountRequested", "LoanAmount")
    apps_term = pick(apps_cols, "TermRequested", "Term")
    apps_purpose = pick(apps_cols, "LoanPurposeTypeId", "LoanPurpose", "LoanReasonId", "LoanReason", "PurposeId", "Purpose")

    tasks_aref = pick(tasks_cols, "ARef", "Aref")
    tasks_type = pick(tasks_cols, "TaskTypeId", "TaskTypeID")
    tasks_done = pick(tasks_cols, "DateCompletedUtc", "DateCompletedUTC")
    tasks_gtref = pick(tasks_cols, "GtRef", "GTRef")
    tasks_descr = pick(tasks_cols, "Description")

    print(f"# Applications cols (relevant): aref={apps_aref} lender={apps_lender} date={apps_date} status={apps_status} brwc={apps_brw_cust} gtc={apps_gt_cust} brw_es={apps_brw_es} gt_es={apps_gt_es} loan={apps_loan_amt} term={apps_term} purpose={apps_purpose}", flush=True)
    print(f"# Tasks cols: aref={tasks_aref} type={tasks_type} done={tasks_done} gtref={tasks_gtref} descr={tasks_descr}", flush=True)
    print(f"# Customers cols: {sorted(customers_cols)}", flush=True)
    print(f"# Addresses cols: {sorted(addresses_cols)}", flush=True)
    print(f"# ESignatures cols: {sorted(es_cols)}", flush=True)
    print(f"# WebBehaviours cols: {sorted(wb_cols)}", flush=True)

    # All March-cohort ARefs + their current ApplicationStatus + customer ids + signature ids
    print("# Q1: cohort ARefs with status / customers / esigs / loan asks", flush=True)
    select_extras = ", ".join([
        f"[{apps_status}]" if apps_status else "NULL",
        f"[{apps_date}]"   if apps_date   else "NULL",
        f"[{apps_leadid}]" if apps_leadid else "NULL",
        f"[{apps_brw_cust}]" if apps_brw_cust else "NULL",
        f"[{apps_gt_cust}]"  if apps_gt_cust  else "NULL",
        f"[{apps_brw_es}]"   if apps_brw_es   else "NULL",
        f"[{apps_gt_es}]"    if apps_gt_es    else "NULL",
        f"[{apps_loan_amt}]" if apps_loan_amt else "NULL",
        f"[{apps_term}]"     if apps_term     else "NULL",
        f"[{apps_purpose}]"  if apps_purpose  else "NULL",
    ])
    cur.execute(
        f"""
        SELECT [{apps_aref}], {select_extras}
        FROM dbo.Applications
        WHERE [{apps_date}] >= ? AND [{apps_date}] < ?
          AND [{apps_lender}] = ?
          AND [{apps_aref}] IS NOT NULL
        """,
        [month_start, month_end, LENDER_ID],
    )
    aref_to_app: dict[str, dict] = {}
    for row in cur.fetchall():
        aref, status, dt, leadid, brwc, gtc, brwes, gtes, loan_amt, term, purpose = row
        aref_to_app[aref] = {
            "status_id": int(status) if status is not None else None,
            "created": dt,
            "lead_id": leadid,
            "brw_customer_id": brwc,
            "gt_customer_id": gtc,
            "brw_esig_id": brwes,
            "gt_esig_id": gtes,
            "loan_amount": float(loan_amt) if loan_amt is not None else None,
            "term": int(term) if term is not None else None,
            "purpose": purpose,
        }
    print(f"#   cohort size: {len(aref_to_app):,}", flush=True)

    # Stage reach per ARef
    print("# Q2: task-completion reach per ARef", flush=True)
    cur.execute(
        f"""
        SELECT t.[{tasks_aref}], t.[{tasks_type}],
               CASE WHEN t.[{tasks_gtref}] IS NULL THEN 'BRW' ELSE 'GT' END AS who
        FROM dbo.Tasks t
        WHERE t.[{tasks_aref}] IN (
            SELECT [{apps_aref}]
            FROM dbo.Applications
            WHERE [{apps_date}] >= ? AND [{apps_date}] < ?
              AND [{apps_lender}] = ?
              AND [{apps_aref}] IS NOT NULL
        )
          AND t.[{tasks_done}] IS NOT NULL
          AND t.[{tasks_type}] IN (41, 48, 54, 62, 146)
        """,
        [month_start, month_end, LENDER_ID],
    )
    aref_stages: dict[str, set] = defaultdict(set)
    for r in cur.fetchall():
        aref, ttid, who = r[0], int(r[1]), r[2]
        aref_stages[aref].add((ttid, who))
    print(f"#   ARefs with ≥1 stage event: {len(aref_stages):,}", flush=True)

    apps_conn.close()

    def furthest_endpoint(aref: str) -> str:
        s = aref_stages.get(aref, set())
        info = aref_to_app[aref]
        has_apply1   = (41, 'BRW') in s
        has_brw_sign = (48, 'BRW') in s
        has_gt_pass  = (54, 'GT')  in s
        has_gt_vc    = (62, 'GT')  in s or (146, 'GT') in s
        is_paid_out  = info['status_id'] == 5
        if is_paid_out:                   return "paid_out"
        if has_gt_vc:                     return "vc_ready_not_paid_out"
        if has_gt_pass:                   return "no_vc_reached"
        if has_brw_sign:                  return "no_accepted_guarantor"
        if has_apply1:                    return "dropped_before_brw_signed"
        return "abandoned_before_page1"

    by_endpoint: dict[str, list[str]] = defaultdict(list)
    for aref in aref_to_app:
        by_endpoint[furthest_endpoint(aref)].append(aref)
    print(f"# bucket sizes: { {k: len(v) for k, v in by_endpoint.items()} }", flush=True)

    DEAD_ENDS = [
        "abandoned_before_page1",
        "dropped_before_brw_signed",
        "no_accepted_guarantor",
        "no_vc_reached",
        "vc_ready_not_paid_out",
    ]
    samples: dict[str, list[str]] = {}
    all_sampled: list[str] = []
    for ep in DEAD_ENDS:
        pool = by_endpoint.get(ep, [])
        n = min(SAMPLE_SIZE, len(pool))
        samples[ep] = rng.sample(pool, n) if n > 0 else []
        all_sampled.extend(samples[ep])
        print(f"#   {ep}: pool={len(pool):,}  sampled={n}", flush=True)

    if not all_sampled:
        print("# no samples; writing empty file", flush=True)
        Path("pipeline-samples.json").write_text(json.dumps({"endpoints": {}}, indent=2))
        return

    # ─────────────────────────────────────────────────────────────────
    # Phase 2 — pull interaction events for the sampled ARefs.
    # ─────────────────────────────────────────────────────────────────
    apps_conn = pyodbc.connect(conn_str("ReportingApplications"), timeout=20)
    apps_conn.timeout = QUERY_TIMEOUT
    cur = apps_conn.cursor()

    interactions: dict[str, list[dict]] = defaultdict(list)

    # ──────────── (a) Customer + Address details ──────────────────
    # Need BRW + GT details for each sampled ARef. Customers join on ARef
    # (BRW = GtRef NULL, GT = GtRef NOT NULL). Addresses join on CustomerId,
    # AddressType=1 (current) preferred.
    cust_aref = pick(customers_cols, "ARef", "Aref")
    cust_id = pick(customers_cols, "CustomerId")
    cust_gtref = pick(customers_cols, "GtRef", "GTRef")
    cust_first = pick(customers_cols, "FirstName")
    cust_middle = pick(customers_cols, "MiddleName")
    cust_sur = pick(customers_cols, "Surname", "LastName")
    cust_dob = pick(customers_cols, "DateOfBirth")
    cust_relation = pick(customers_cols, "RelationToBrw", "RelationshipToBrw")

    addr_cust = pick(addresses_cols, "CustomerId")
    addr_type = pick(addresses_cols, "Type", "AddressType")
    addr_body = pick(addresses_cols, "AddressBody")  # warehouse single-column form
    addr_l1 = pick(addresses_cols, "AddressLine1")
    addr_l2 = pick(addresses_cols, "AddressLine2")
    addr_city = pick(addresses_cols, "City")
    addr_state = pick(addresses_cols, "State")
    addr_post = pick(addresses_cols, "Postcode", "PostCode", "ZipCode", "Zip")

    customers_by_aref: dict[str, dict] = defaultdict(dict)  # {aref: {"BRW": {...}, "GT": {...}}}

    if cust_aref and cust_id and cust_gtref:
        print("# pulling Customers for sampled ARefs…", flush=True)
        for chunk in chunked(all_sampled, 1500):
            ph = ",".join(["?"] * len(chunk))
            cols_sel = ", ".join([
                f"[{cust_aref}]",
                f"[{cust_id}]",
                f"[{cust_gtref}]",
                f"[{cust_first}]"   if cust_first   else "NULL",
                f"[{cust_middle}]"  if cust_middle  else "NULL",
                f"[{cust_sur}]"     if cust_sur     else "NULL",
                f"[{cust_dob}]"     if cust_dob     else "NULL",
                f"[{cust_relation}]" if cust_relation else "NULL",
            ])
            cur.execute(
                f"SELECT {cols_sel} FROM dbo.Customers WHERE [{cust_aref}] IN ({ph})",
                chunk,
            )
            for r in cur.fetchall():
                aref, cid, gtref, fn, mn, sn, dob, rel = r
                role = "GT" if gtref is not None else "BRW"
                customers_by_aref[aref][role] = {
                    "customer_id": cid,
                    "first_name": fn,
                    "middle_name": mn,
                    "surname": sn,
                    "date_of_birth": iso(dob) if dob else None,
                    "relation_to_brw": rel,
                }

    # ──────────── (a.5) Identity-link: find all ARefs for the same person ──
    # Use (FirstName, Surname, DateOfBirth) on the BRW customer record.
    # This lets us show the customer's full history across every application
    # they've made — not just the March 2026 cohort one we sampled.
    identity_to_primary: dict[tuple, str] = {}
    for aref, roles in customers_by_aref.items():
        brw = roles.get("BRW")
        if not brw: continue
        fn = (brw.get("first_name") or "").lower().strip()
        sn = (brw.get("surname") or "").lower().strip()
        dob = (brw.get("date_of_birth") or "")[:10]
        if fn and sn and dob and len(dob) == 10:
            identity_to_primary[(fn, sn, dob)] = aref

    aref_to_primary: dict[str, str] = {a: a for a in all_sampled}
    if identity_to_primary:
        print(f"# identity-link: looking up related ARefs for {len(identity_to_primary)} people…", flush=True)
        related: dict[tuple, set[str]] = defaultdict(set)
        keys = list(identity_to_primary.keys())
        for ck in chunked(keys, 80):
            conditions = []
            params = []
            for fn, sn, dob in ck:
                conditions.append("(LOWER([" + cust_first + "])=? AND LOWER([" + cust_sur + "])=? AND CONVERT(date, [" + cust_dob + "])=?)")
                params.extend([fn, sn, dob])
            sql = f"""
                SELECT [{cust_aref}], LOWER([{cust_first}]), LOWER([{cust_sur}]),
                       CONVERT(varchar(10), [{cust_dob}], 23)
                FROM dbo.Customers
                WHERE [{cust_gtref}] IS NULL
                  AND ({" OR ".join(conditions)})
            """
            cur.execute(sql, params)
            for ar, fn, sn, dob in cur.fetchall():
                related[(fn, sn, dob)].add(ar)
        # For each identity, point every related ARef at the sampled primary.
        added = 0
        for key, primary in identity_to_primary.items():
            for ar in related.get(key, set()):
                if ar not in aref_to_primary:
                    aref_to_primary[ar] = primary
                    added += 1
        print(f"#   added {added} historical ARefs (family size now {len(aref_to_primary)})", flush=True)

    family_arefs = list(aref_to_primary.keys())

    # ──────────── (a.6) Pull Application + Customer for new ARefs ─────
    new_arefs = [a for a in family_arefs if a not in aref_to_app]
    if new_arefs:
        print(f"# pulling Applications for {len(new_arefs)} historical ARefs…", flush=True)
        for chunk in chunked(new_arefs, 1500):
            ph = ",".join(["?"] * len(chunk))
            cur.execute(
                f"""
                SELECT [{apps_aref}], {select_extras}
                FROM dbo.Applications
                WHERE [{apps_aref}] IN ({ph})
                """,
                chunk,
            )
            for row in cur.fetchall():
                aref, status, dt, leadid, brwc, gtc, brwes, gtes, loan_amt, term, purpose = row
                aref_to_app[aref] = {
                    "status_id": int(status) if status is not None else None,
                    "created": dt,
                    "lead_id": leadid,
                    "brw_customer_id": brwc,
                    "gt_customer_id": gtc,
                    "brw_esig_id": brwes,
                    "gt_esig_id": gtes,
                    "loan_amount": float(loan_amt) if loan_amt is not None else None,
                    "term": int(term) if term is not None else None,
                    "purpose": purpose,
                }

        # Customers for those new ARefs (so the BRW name etc on each app is correct)
        if cust_aref:
            for chunk in chunked(new_arefs, 1500):
                ph = ",".join(["?"] * len(chunk))
                cols_sel = ", ".join([
                    f"[{cust_aref}]", f"[{cust_id}]", f"[{cust_gtref}]",
                    f"[{cust_first}]"   if cust_first   else "NULL",
                    f"[{cust_middle}]"  if cust_middle  else "NULL",
                    f"[{cust_sur}]"     if cust_sur     else "NULL",
                    f"[{cust_dob}]"     if cust_dob     else "NULL",
                    f"[{cust_relation}]" if cust_relation else "NULL",
                ])
                cur.execute(
                    f"SELECT {cols_sel} FROM dbo.Customers WHERE [{cust_aref}] IN ({ph})",
                    chunk,
                )
                for r in cur.fetchall():
                    aref, cid, gtref, fn, mn, sn, dob, rel = r
                    role = "GT" if gtref is not None else "BRW"
                    customers_by_aref.setdefault(aref, {})[role] = {
                        "customer_id": cid,
                        "first_name": fn, "middle_name": mn, "surname": sn,
                        "date_of_birth": iso(dob) if dob else None,
                        "relation_to_brw": rel,
                    }

    # All BRW + GT customer ids → fetch addresses
    all_cids = [v["customer_id"] for d in customers_by_aref.values() for v in d.values()]
    addresses_by_cid: dict[int, dict] = {}
    if addr_cust and all_cids:
        print(f"# pulling Addresses for {len(all_cids)} customers…", flush=True)
        for chunk in chunked(all_cids, 1500):
            ph = ",".join(["?"] * len(chunk))
            cols_sel = ", ".join([
                f"[{addr_cust}]",
                f"[{addr_body}]" if addr_body else "NULL",
                f"[{addr_l1}]"   if addr_l1   else "NULL",
                f"[{addr_l2}]"   if addr_l2   else "NULL",
                f"[{addr_city}]" if addr_city else "NULL",
                f"[{addr_state}]" if addr_state else "NULL",
                f"[{addr_post}]" if addr_post else "NULL",
                f"[{addr_type}]" if addr_type else "NULL",
            ])
            cur.execute(
                f"SELECT {cols_sel} FROM dbo.Addresses WHERE [{addr_cust}] IN ({ph})",
                chunk,
            )
            for r in cur.fetchall():
                cid, body, l1, l2, city, state, post, atype = r
                # Prefer Type=1 (current). Otherwise keep the first we see.
                if cid not in addresses_by_cid or atype == 1:
                    addresses_by_cid[cid] = {
                        "body": body,
                        "line1": l1, "line2": l2, "city": city,
                        "state": state, "postcode": post,
                    }

    def fmt_addr(a: dict) -> str:
        if not a: return ""
        # Prefer the structured columns if present; fall back to AddressBody.
        if a.get("line1") or a.get("city"):
            bits = [a.get("line1"), a.get("line2"), a.get("city"), a.get("state"), a.get("postcode")]
            return ", ".join(b for b in bits if b)
        body = (a.get("body") or "").strip()
        if body:
            extras = [a.get("state"), a.get("postcode")]
            extras = [e for e in extras if e and e not in body]
            return body + (", " + ", ".join(extras) if extras else "")
        bits = [a.get("state"), a.get("postcode")]
        return ", ".join(b for b in bits if b)

    def fmt_name(c: dict) -> str:
        if not c: return ""
        return " ".join(b for b in [c.get("first_name"), c.get("middle_name"), c.get("surname")] if b)

    def fmt_purpose(p):
        if p is None: return None
        try:
            return loan_purpose_labels.get(int(p), str(p))
        except (ValueError, TypeError):
            return str(p)

    def record(source_aref: str, ev: dict) -> None:
        """Tag an event with its source ARef and bucket it under the primary."""
        ev["aref"] = source_aref
        primary = aref_to_primary.get(source_aref, source_aref)
        interactions[primary].append(ev)

    # ──────────── (b) Application started events for every ARef in family ───
    for aref in family_arefs:
        info = aref_to_app.get(aref)
        if not info or not info.get('created'):
            continue
        details = []
        brw = customers_by_aref.get(aref, {}).get("BRW")
        if brw:
            name = fmt_name(brw)
            if name: details.append(["BRW name", name])
            addr = addresses_by_cid.get(brw["customer_id"]) if brw else None
            if addr:
                a = fmt_addr(addr)
                if a: details.append(["BRW address", a])
        if info["loan_amount"] is not None:
            details.append(["Loan amount requested", f"${info['loan_amount']:,.0f}"])
        if info["term"] is not None:
            details.append(["Term requested", f"{info['term']} months"])
        p_label = fmt_purpose(info["purpose"])
        if p_label:
            details.append(["Loan purpose", p_label])
        record(aref, {
            "kind": "application_started",
            "at": iso(info['created']),
            # Label gets enriched with the broker friendly name once the
            # Leads block resolves Lead → Campaign → Source below. We
            # stash lead_id as a structured field so the post-process
            # pass can find it.
            "label": "Application started" + (
                f" (from purchased lead #{info['lead_id']})" if info['lead_id'] is not None
                else " (direct via website)"
            ),
            "lead_id": info['lead_id'],
            "details": details,
        })

    # ──────────── (c) Tasks ────────────────────────────────────────
    print(f"# pulling Tasks for {len(family_arefs)} ARefs (incl. historical)…", flush=True)
    for chunk in chunked(family_arefs, 1500):
        ph = ",".join(["?"] * len(chunk))
        cur.execute(
            f"""
            SELECT [{tasks_aref}], [{tasks_type}], [{tasks_gtref}],
                   [{tasks_done}], [{tasks_descr}]
            FROM dbo.Tasks
            WHERE [{tasks_aref}] IN ({ph})
              AND [{tasks_done}] IS NOT NULL
            """,
            chunk,
        )
        for aref, ttid, gtref, done, descr in cur.fetchall():
            ttid = int(ttid)
            who = "GT" if gtref is not None else "BRW"
            # Prefer warehouse TaskTypes.TaskName; fall back to our hardcoded
            # labels for any id missing from the lookup; final fallback the
            # Tasks.Description column.
            human_label = (
                task_type_labels.get(ttid)
                or TASK_FALLBACK_LABELS.get(ttid)
                or (descr or "").strip()
                or f"Task #{ttid}"
            )
            label_stem = human_label
            ev = {
                "kind": "task_completed",
                "at": iso(done),
                "label": f"{who} completed: {label_stem}",
                "task_type_id": ttid,
                "who": who,
            }
            # Attach name/address details to Apply1 events for richer rendering.
            if ttid == 41:
                role = "GT" if who == "GT" else "BRW"
                cust = customers_by_aref.get(aref, {}).get(role)
                addr = addresses_by_cid.get(cust["customer_id"]) if cust else None
                details = []
                if cust:
                    n = fmt_name(cust)
                    if n: details.append([f"{role} name", n])
                    if cust.get("date_of_birth"):
                        details.append([f"{role} DOB", cust["date_of_birth"][:10]])
                    if role == "GT" and cust.get("relation_to_brw") is not None:
                        details.append(["Relation to BRW", str(cust["relation_to_brw"])])
                if addr:
                    a = fmt_addr(addr)
                    if a: details.append([f"{role} address", a])
                if role == "BRW":
                    info = aref_to_app.get(aref) or {}
                    if info.get("loan_amount") is not None:
                        details.append(["Loan amount requested", f"${info['loan_amount']:,.0f}"])
                    if info.get("term") is not None:
                        details.append(["Term requested", f"{info['term']} months"])
                    p_label = fmt_purpose(info.get("purpose"))
                    if p_label:
                        details.append(["Loan purpose", p_label])
                if details:
                    ev["details"] = details
            record(aref, ev)

    # ──────────── (d) Same-minute task clustering ──────────────────
    # If a single ARef has CLUSTER_MIN_TASKS+ task events whose timestamps
    # fall within CLUSTER_WINDOW_S of one another, replace them with one
    # "system sweep" pseudo-event listing the labels.
    def collapse_clusters(events: list[dict]) -> list[dict]:
        # Pick out task events
        tasks = [e for e in events if e["kind"] == "task_completed"]
        others = [e for e in events if e["kind"] != "task_completed"]
        if len(tasks) < CLUSTER_MIN_TASKS:
            return events
        tasks.sort(key=lambda e: e["at"])

        kept: list[dict] = []
        i = 0
        while i < len(tasks):
            j = i
            t0 = datetime.datetime.fromisoformat(tasks[i]["at"].replace("Z", "+00:00")) if tasks[i]["at"] else None
            while j + 1 < len(tasks) and t0:
                tj = datetime.datetime.fromisoformat(tasks[j + 1]["at"].replace("Z", "+00:00"))
                if (tj - t0).total_seconds() <= CLUSTER_WINDOW_S:
                    j += 1
                else:
                    break
            cluster_size = j - i + 1
            if cluster_size >= CLUSTER_MIN_TASKS:
                cluster_evs = tasks[i:j + 1]
                bullets = [e["label"] for e in cluster_evs]
                # Cluster only collapses when all events share the same source
                # ARef (otherwise we'd hide cross-application activity).
                ar0 = cluster_evs[0].get("aref")
                same_aref = all(e.get("aref") == ar0 for e in cluster_evs)
                if same_aref:
                    kept.append({
                        "kind": "task_cluster",
                        "at": cluster_evs[0]["at"],
                        "label": f"Application closed — {cluster_size} tasks finalised in bulk",
                        "items": bullets,
                        "aref": ar0,
                    })
                else:
                    kept.extend(cluster_evs)
            else:
                kept.extend(tasks[i:j + 1])
            i = j + 1
        return others + kept

    for aref in list(interactions.keys()):
        interactions[aref] = collapse_clusters(interactions[aref])

    # ──────────── (e) ESignatures via Apps.BrwEsignatureId / GtEsignatureId
    es_id_col = pick(es_cols, "EsignatureId", "ESignatureId")
    es_signed = pick(es_cols, "DateSignedUtc", "DateSignedUTC", "DateSignedLocal")
    es_ip = pick(es_cols, "IpAddress")
    if es_id_col and es_signed and (apps_brw_es or apps_gt_es):
        print("# pulling ESignatures via Applications join…", flush=True)
        # Build map: esig_id → (aref, role)
        esig_map: dict[int, tuple[str, str]] = {}
        for aref in family_arefs:
            info = aref_to_app.get(aref) or {}
            if info.get("brw_esig_id") is not None:
                esig_map[int(info["brw_esig_id"])] = (aref, "BRW")
            if info.get("gt_esig_id") is not None:
                esig_map[int(info["gt_esig_id"])] = (aref, "GT")
        if esig_map:
            for chunk in chunked(list(esig_map.keys()), 1500):
                ph = ",".join(["?"] * len(chunk))
                ip_sql = f", [{es_ip}]" if es_ip else ", NULL"
                cur.execute(
                    f"""
                    SELECT [{es_id_col}], [{es_signed}]{ip_sql}
                    FROM dbo.ESignatures
                    WHERE [{es_id_col}] IN ({ph})
                      AND [{es_signed}] IS NOT NULL
                    """,
                    chunk,
                )
                for esid, signed, ip in cur.fetchall():
                    pair = esig_map.get(int(esid))
                    if not pair: continue
                    aref, role = pair
                    record(aref, {
                        "kind": "signature",
                        "at": iso(signed),
                        "label": f"{role} signed contract" + (f" (IP {ip})" if ip else ""),
                    })

    # ──────────── (f) WebBehaviours ────────────────────────────────
    wb_aref = pick(wb_cols, "ARef", "Aref")
    wb_dt = pick(wb_cols, "DateCreatedUtc", "DateTimeUtc", "DateTimeUTC")
    wb_event = pick(wb_cols, "WebEventObject", "EventObject", "EventBody", "Body")
    wb_provider = pick(wb_cols, "Provider")
    print(f"# WebBehaviours chosen: aref={wb_aref} dt={wb_dt} event={wb_event} provider={wb_provider}", flush=True)
    if wb_aref and wb_dt:
        for chunk in chunked(family_arefs, 1500):
            ph = ",".join(["?"] * len(chunk))
            event_sql = f", [{wb_event}]" if wb_event else ", NULL"
            prov_sql = f", [{wb_provider}]" if wb_provider else ", NULL"
            cur.execute(
                f"""
                SELECT [{wb_aref}], [{wb_dt}]{event_sql}{prov_sql}
                FROM dbo.WebBehaviours
                WHERE [{wb_aref}] IN ({ph})
                  AND [{wb_dt}] IS NOT NULL
                """,
                chunk,
            )
            for aref, dt, evobj, prov in cur.fetchall():
                label = format_web_event(evobj, prov)
                record(aref, {
                    "kind": "web_visit",
                    "at": iso(dt),
                    "label": label,
                })

    # ──────────── (f.5) Lead presentations ──────────────────────────
    # For every ARef in the family that came from a purchased lead,
    # emit a "Lead presented" event with the lead's broker, name, address,
    # loan amount and result. This gives the timeline a starting point
    # before "Application started" for affiliate-sourced applications.
    leads_cols = discover_columns(cur, "Leads")
    print(f"# Leads cols: {sorted(leads_cols)}", flush=True)
    leads_id = pick(leads_cols, "LeadId", "LeadID")
    leads_aref = pick(leads_cols, "ARef", "Aref")
    leads_dt = pick(leads_cols, "DateCreatedUtc", "DateCreatedUTC", "DateReceivedUtc", "InterestingDateTimeUtc")
    leads_result = pick(leads_cols, "LeadResultTypeId", "LeadResultId", "ResultTypeId")
    # Leads in the warehouse holds CampaignId (Brokers-namespace) but not
    # BrokerId or SourceId directly — we resolve those via Brokers.Campaigns.
    leads_broker = pick(leads_cols, "BrokerId", "CampaignId")
    leads_source = pick(leads_cols, "SourceId", "sourcehistoryid")
    leads_amount = pick(leads_cols, "LoanAmountRequested", "LoanAmount", "RequestedLoanAmount")
    leads_term = pick(leads_cols, "TermRequested", "Term")
    leads_purpose = pick(leads_cols, "LoanPurposeTypeId", "LoanPurposeId", "LoanPurpose")
    leads_first = pick(leads_cols, "FirstName")
    leads_sur = pick(leads_cols, "Surname", "LastName")
    leads_dob = pick(leads_cols, "DateOfBirth")
    leads_addr = pick(leads_cols, "AddressBody", "AddressLine1", "Address")
    leads_state = pick(leads_cols, "State")
    leads_post = pick(leads_cols, "Postcode", "PostCode", "Zip", "ZipCode")
    print(f"# Leads chosen: id={leads_id} aref={leads_aref} dt={leads_dt} result={leads_result} broker={leads_broker}", flush=True)

    # Try to resolve LeadResultTypeId → English from a lookup table
    lead_result_labels: dict[int, str] = {}
    cur.execute(
        """
        SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME LIKE '%LeadResult%'
        """
    )
    for tname in [r[0] for r in cur.fetchall()]:
        try:
            tcols = discover_columns(cur, tname)
            id_col = pick(tcols, "LeadResultTypeId", "LeadResultId", "Id", "TypeId")
            name_col = pick(tcols, "LeadResultDescription", "Description", "Name", "Label", "DisplayName")
            if not (id_col and name_col):
                continue
            cur.execute(f"SELECT [{id_col}], [{name_col}] FROM dbo.[{tname}]")
            for tid, nm in cur.fetchall():
                if tid is None: continue
                lead_result_labels[int(tid)] = str(nm) if nm is not None else f"#{tid}"
            print(f"#   {tname}: loaded {len(lead_result_labels)} lead-result labels", flush=True)
            if lead_result_labels:
                break
        except Exception as e:
            print(f"#   {tname}: lookup failed: {e}", flush=True)

    if leads_id and leads_dt and (leads_aref or True):
        # Two paths: (1) join by Lead.ARef (post-purchase), (2) join by
        # Application.LeadId (we have these LeadIds in aref_to_app).
        lead_ids_to_aref: dict[int, str] = {}
        for aref in family_arefs:
            info = aref_to_app.get(aref) or {}
            if info.get("lead_id") is not None:
                lead_ids_to_aref[int(info["lead_id"])] = aref
        # Populated during the leads-fetch loop below; consumed by the
        # post-process pass that rewrites every application_started label
        # to include the broker friendly name.
        lead_to_broker_name: dict[int, str] = {}
        if lead_ids_to_aref:
            print(f"# pulling Leads for {len(lead_ids_to_aref)} LeadIds…", flush=True)
            sel_cols = ", ".join([
                f"[{leads_id}]",
                f"[{leads_dt}]",
                f"[{leads_result}]" if leads_result else "NULL",
                f"[{leads_broker}]" if leads_broker else "NULL",
                f"[{leads_source}]" if leads_source else "NULL",
                f"[{leads_amount}]" if leads_amount else "NULL",
                f"[{leads_term}]"   if leads_term   else "NULL",
                f"[{leads_purpose}]" if leads_purpose else "NULL",
                f"[{leads_first}]"  if leads_first  else "NULL",
                f"[{leads_sur}]"    if leads_sur    else "NULL",
                f"[{leads_dob}]"    if leads_dob    else "NULL",
                f"[{leads_addr}]"   if leads_addr   else "NULL",
                f"[{leads_state}]"  if leads_state  else "NULL",
                f"[{leads_post}]"   if leads_post   else "NULL",
            ])
            for chunk in chunked(list(lead_ids_to_aref.keys()), 1500):
                ph = ",".join(["?"] * len(chunk))
                cur.execute(
                    f"SELECT {sel_cols} FROM dbo.Leads WHERE [{leads_id}] IN ({ph})",
                    chunk,
                )
                for row in cur.fetchall():
                    lid, dt, rtype, broker, source, amt, term, purpose, fn, sn, dob, addr, state, post = row
                    aref = lead_ids_to_aref.get(int(lid))
                    if not aref: continue
                    details = []
                    name = " ".join(b for b in [fn, sn] if b)
                    if name: details.append(["Lead name", name])
                    if dob:
                        details.append(["Lead DOB", iso(dob)[:10] if dob else ""])
                    addr_bits = [addr, state, post]
                    addr_str = ", ".join(b for b in addr_bits if b)
                    if addr_str: details.append(["Lead address", addr_str])
                    if amt is not None:
                        details.append(["Lead loan amount", f"${float(amt):,.0f}"])
                    if term is not None:
                        details.append(["Lead term", f"{int(term)} months"])
                    p_label = fmt_purpose(purpose)
                    if p_label:
                        details.append(["Lead purpose", p_label])
                    # Resolve broker via CampaignId → Campaigns.SourceId →
                    # Sources.FriendlyName. The Lead.CampaignId we have here
                    # is the Brokers-namespace campaign, not the Message
                    # Factory one.
                    resolved_source_id = None
                    resolved_campaign_name = None
                    if broker is not None and int(broker) in broker_campaigns:
                        meta_c = broker_campaigns[int(broker)]
                        resolved_campaign_name = meta_c.get("name")
                        resolved_source_id = meta_c.get("source_id")
                    if source is None and resolved_source_id is not None:
                        source = resolved_source_id
                    if source is not None:
                        try:
                            s_label = broker_sources.get(int(source))
                        except (ValueError, TypeError):
                            s_label = None
                        details.append(["Broker", s_label or f"Source {source}"])
                        # Remember this so the application_started label can
                        # surface the broker name inline instead of just the
                        # opaque "from purchased lead #N".
                        if s_label:
                            try:
                                lead_to_broker_name[int(lid)] = s_label
                            except (ValueError, TypeError):
                                pass
                    if resolved_campaign_name:
                        details.append(["Broker campaign", resolved_campaign_name])
                    elif broker is not None:
                        # Couldn't resolve — show raw CampaignId for traceability
                        details.append(["Broker campaign id", str(broker)])
                    if rtype is not None:
                        try:
                            r_label = lead_result_labels.get(int(rtype), f"Result code {int(rtype)}")
                        except (ValueError, TypeError):
                            r_label = str(rtype)
                        details.append(["Lead result", r_label])
                    broker_name_for_label = lead_to_broker_name.get(int(lid))
                    record(aref, {
                        "kind": "lead_presented",
                        "at": iso(dt),
                        "label": (
                            f"Lead presented by {broker_name_for_label}"
                            if broker_name_for_label
                            else f"Lead #{lid} presented by broker"
                        ),
                        "details": details,
                    })

    apps_conn.close()

    # ──────────── Enrich application_started labels with broker names ──
    # The application_started events were recorded before the Leads block
    # ran (so the lead_id was known but not which broker sold it). Now
    # that we have lead_to_broker_name, rewrite the labels so the timeline
    # shows "Application started — sold by Lead Economy" instead of the
    # opaque "(from purchased lead #666447305)".
    if lead_to_broker_name:
        patched = 0
        for primary, events in interactions.items():
            for ev in events:
                if ev.get("kind") != "application_started":
                    continue
                lid = ev.get("lead_id")
                if lid is None:
                    continue
                bname = lead_to_broker_name.get(int(lid))
                if bname:
                    ev["label"] = f"Application started — sold to us by {bname}"
                    patched += 1
        print(f"# enriched {patched} application_started labels with broker names", flush=True)

    # ──────────── (g) Messages ─────────────────────────────────────
    print("# pulling Messages from ReportingCommunications…", flush=True)
    comm_conn = pyodbc.connect(conn_str("ReportingCommunications"), timeout=20)
    comm_conn.timeout = QUERY_TIMEOUT
    cur = comm_conn.cursor()
    msg_cols = discover_columns(cur, "Messages")
    msg_aref = pick(msg_cols, "ARef", "Aref")
    msg_dt = pick(msg_cols, "UtcTime", "DateTimeUtc", "DateTimeUTC", "DateCreatedUtc", "DateSentUtc", "LocalDateTime", "StatusTime")
    msg_ctype = pick(msg_cols, "ClientType")
    msg_descr = pick(msg_cols, "Description")
    msg_body = pick(msg_cols, "MessageBody", "Body", "Content", "Message")
    msg_subject = pick(msg_cols, "MessageTitle", "Subject")
    msg_campaign = pick(msg_cols, "CampaignName", "Campaign")
    msg_camp_id = pick(msg_cols, "CampaignId", "CampaignID")
    print(f"# Messages chosen: aref={msg_aref} dt={msg_dt} ct={msg_ctype} body={msg_body} subj={msg_subject} descr={msg_descr} campaign={msg_campaign} campId={msg_camp_id}", flush=True)

    # ──────────── Campaign lookup (authoritative SMS/Email/Letter/Push)
    # Per wiki §9.1 the Message Factory Campaigns table is in Central CRM.
    # Probe Communications first (Messages lives here), then a few other
    # likely warehouse DBs.
    campaign_meta: dict[int, dict] = {}

    def load_campaigns_from(database: str) -> int:
        try:
            probe = pyodbc.connect(conn_str(database), timeout=10)
            probe.timeout = 60
        except Exception as e:
            print(f"# Campaigns probe in {database}: connect failed ({e})", flush=True)
            return 0
        try:
            pc = probe.cursor()
            cols = discover_columns(pc, "Campaigns")
            if not cols:
                print(f"# Campaigns probe in {database}: no Campaigns table", flush=True)
                return 0
            camp_id = pick(cols, "CampaignId", "CampaignID")
            camp_name = pick(cols, "CampaignName", "Name")
            camp_type = pick(cols, "MessageType", "Type", "Channel")
            camp_desc = pick(cols, "Description", "DisplayName", "Label")
            print(f"# Campaigns probe in {database}: cols id={camp_id} name={camp_name} type={camp_type} desc={camp_desc}", flush=True)
            if not (camp_id and (camp_type or camp_desc)):
                return 0
            sel = ", ".join([
                f"[{camp_id}]",
                f"[{camp_name}]" if camp_name else "NULL",
                f"[{camp_type}]" if camp_type else "NULL",
                f"[{camp_desc}]" if camp_desc else "NULL",
            ])
            pc.execute(f"SELECT {sel} FROM dbo.Campaigns")
            n = 0
            for cid, name, mtype, desc in pc.fetchall():
                if cid is None: continue
                campaign_meta[int(cid)] = {
                    "name": name,
                    "type": (mtype or "").strip() or None,
                    "description": (desc or "").strip() or None,
                }
                n += 1
            print(f"#   {database}.Campaigns: loaded {n} rows", flush=True)
            return n
        finally:
            probe.close()

    # First list every campaign-related table we can see in
    # ReportingCommunications so we know what's actually available.
    try:
        cur.execute(
            """
            SELECT TABLE_SCHEMA, TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_NAME LIKE '%ampaign%' OR TABLE_NAME LIKE '%essage%'
            ORDER BY TABLE_NAME
            """
        )
        comms_tables = [(r[0], r[1]) for r in cur.fetchall()]
        print(f"# ReportingCommunications campaign/message tables: {comms_tables}", flush=True)
    except Exception as e:
        print(f"# table-list probe failed: {e}", flush=True)

    for db_candidate in (
        "ReportingCommunications",  # next to Messages
        "ReportingCRM",
        "ReportingApplications",
        "ReportingMessageFactory",
        "ReportingAdmin",
    ):
        if load_campaigns_from(db_candidate):
            break

    # Per-message MessageType from MessagesToSend (per wiki §9.2). This table
    # may have been pruned for already-sent messages, so it's a best-effort
    # backfill for any campaigns we couldn't resolve.
    msg_type_by_msgid: dict[int, str] = {}
    try:
        mts_cols = discover_columns(cur, "MessagesToSend")
    except Exception:
        mts_cols = set()
    if mts_cols:
        mts_id = pick(mts_cols, "MessageId", "MessagesToSendId")
        mts_type = pick(mts_cols, "MessageType", "Type", "Channel")
        mts_camp = pick(mts_cols, "CampaignId")
        print(f"# MessagesToSend cols: id={mts_id} type={mts_type} camp={mts_camp}", flush=True)
        if mts_id and mts_type:
            cur.execute(f"SELECT TOP 1 [{mts_type}] FROM dbo.MessagesToSend")
            sample = cur.fetchall()
            print(f"#   MessagesToSend sample type: {sample}", flush=True)
            # Could be huge — load distinct (CampaignId, MessageType) pairs as
            # a campaign-level fallback rather than per-message.
            if mts_camp:
                cur.execute(
                    f"SELECT DISTINCT [{mts_camp}], [{mts_type}] FROM dbo.MessagesToSend WHERE [{mts_type}] IS NOT NULL"
                )
                added = 0
                for cid, mtype in cur.fetchall():
                    if cid is None: continue
                    cid_i = int(cid)
                    if cid_i not in campaign_meta:
                        campaign_meta[cid_i] = {"name": None, "type": (mtype or "").strip() or None, "description": None}
                        added += 1
                print(f"#   filled {added} campaign types from MessagesToSend", flush=True)

    if not campaign_meta:
        print("# No Campaign metadata available — falling back to (S)/(E) heuristic", flush=True)
    else:
        print(f"# Total campaigns with metadata: {len(campaign_meta)}", flush=True)
    # ── Build phone+email → ARef map for inbound-message back-fill ─────
    # Customer-initiated SMS / Email / Call rows usually have NO ARef on
    # the dbo.Messages row (the IMAP / SMS poller can't extract one) — the
    # customer is identified only by ExternalAddress (their phone or email).
    # Without this lookup, the timeline only shows messages WE sent and
    # the customer's voice disappears entirely.
    msg_ext = pick(msg_cols, "ExternalAddress", "FromAddress", "ContactAddress")
    msg_id_col = pick(msg_cols, "MessageId", "MessageID", "Id")

    def _norm_phone(s):
        d = "".join(c for c in (s or "") if c.isdigit())
        return d[-10:] if len(d) >= 10 else d

    external_to_aref: dict[str, str] = {}   # normalised key -> ARef
    if msg_ext:
        try:
            apps2 = pyodbc.connect(conn_str("ReportingApplications"), timeout=20)
            apps2.timeout = QUERY_TIMEOUT
            ac = apps2.cursor()
            tel_cols = discover_columns(ac, "Telephones")
            em_cols = discover_columns(ac, "Emails")
            tel_num = pick(tel_cols, "PhoneNumber", "Number", "TelephoneNumber", "Telephone", "Phone")
            tel_cust = pick(tel_cols, "CustomerId", "CustomerID")
            em_addr = pick(em_cols, "EmailAddress", "Email", "Address")
            em_cust = pick(em_cols, "CustomerId", "CustomerID")
            all_cids_for_contact = []
            cid_to_aref: dict[int, str] = {}
            for aref, roles in customers_by_aref.items():
                for v in roles.values():
                    cid = v.get("customer_id")
                    if cid is not None:
                        all_cids_for_contact.append(int(cid))
                        cid_to_aref[int(cid)] = aref
            if all_cids_for_contact and tel_num and tel_cust:
                for chunk in chunked(all_cids_for_contact, 1500):
                    phc = ",".join(["?"] * len(chunk))
                    ac.execute(
                        f"SELECT [{tel_num}], [{tel_cust}] FROM dbo.Telephones WHERE [{tel_cust}] IN ({phc})",
                        chunk,
                    )
                    for num, cid in ac.fetchall():
                        k = _norm_phone(num)
                        if k and int(cid) in cid_to_aref:
                            external_to_aref[k] = cid_to_aref[int(cid)]
            if all_cids_for_contact and em_addr and em_cust:
                for chunk in chunked(all_cids_for_contact, 1500):
                    phc = ",".join(["?"] * len(chunk))
                    ac.execute(
                        f"SELECT [{em_addr}], [{em_cust}] FROM dbo.Emails WHERE [{em_cust}] IN ({phc})",
                        chunk,
                    )
                    for em, cid in ac.fetchall():
                        k = (em or "").strip().lower()
                        if k and int(cid) in cid_to_aref:
                            external_to_aref[k] = cid_to_aref[int(cid)]
            apps2.close()
            print(f"# external→ARef map built for inbound back-fill: {len(external_to_aref)} entries", flush=True)
        except Exception as e:
            print(f"# inbound back-fill skipped (contact-info load failed): {e}", flush=True)

    seen_msg_keys: set = set()   # MessageId (or fallback tuple) for dedup

    def _process_message_row(aref, dt, body, descr, ctype, subj, campaign, camp_id_val, msg_id):
        # Dedup so the inbound-by-ExternalAddress pass below doesn't double-
        # count rows that already came through the ARef-IN pass above.
        key = ("id", msg_id) if msg_id is not None else ("k", aref, str(dt), (body or "")[:40])
        if key in seen_msg_keys:
            return
        seen_msg_keys.add(key)

        ctype_str = (ctype or "").strip()
        descr_int = int(descr) if descr is not None and str(descr).strip() != "" else None
        # Description enum: 0/1/2 = inbound (SMS/Email/Call), 5+ = outbound
        is_inbound = descr_int in (0, 1, 2)
        channel = {0: "SMS in", 1: "Email in", 2: "Call in"}.get(descr_int)
        if not channel:
            channel = "Outbound" + (f" ({ctype_str})" if ctype_str else "")
        is_robot = (not is_inbound) and (ctype_str in ROBOT_CLIENT_TYPES)
        msg = {
            "kind": "message_in" if is_inbound else "message_out",
            "at": iso(dt),
            "channel": channel,
            "client_type": ctype_str or None,
        }
        campaign_str = (campaign or "").strip() if campaign else ""
        campaign_clean, derived_channel = expand_campaign_tokens(campaign_str)
        meta = None
        if camp_id_val is not None:
            try:
                meta = campaign_meta.get(int(camp_id_val))
            except (ValueError, TypeError):
                meta = None
        authoritative_channel = (meta or {}).get("type")
        campaign_desc = (meta or {}).get("description")
        if not is_inbound and (authoritative_channel or derived_channel):
            msg["channel"] = authoritative_channel or derived_channel
            msg["channel_kind"] = authoritative_channel or derived_channel
        elif is_inbound:
            msg["channel_kind"] = {0: "SMS", 1: "Email", 2: "Call"}.get(descr_int)
        if is_robot:
            label = f"Message from {ctype_str}Bot"
            if campaign_clean:
                label += f" — {campaign_clean}"
            if campaign_desc and campaign_desc.lower() not in (campaign_clean or "").lower():
                label += f" — {campaign_desc}"
            msg["label"] = label
            msg["redacted"] = True
        else:
            body_text = (body or "").strip() if body else ""
            if subj:
                body_text = f"[{subj}] {body_text}".strip()
            if campaign_clean:
                body_text = f"({campaign_clean}) {body_text}".strip()
            if not body_text:
                body_text = "(empty body)"
            if len(body_text) > 4000:
                body_text = body_text[:4000] + " …[truncated]"
            msg["body"] = body_text
        record(aref, msg)

    # Pass 1: ARef-IN — catches every outbound (always has ARef) + the small
    # minority of inbounds whose ARef was set on the row.
    if msg_aref and msg_dt:
        for chunk in chunked(family_arefs, 1500):
            ph = ",".join(["?"] * len(chunk))
            body_sql = f", [{msg_body}]" if msg_body else ", NULL"
            descr_sql = f", [{msg_descr}]" if msg_descr else ", NULL"
            ctype_sql = f", [{msg_ctype}]" if msg_ctype else ", NULL"
            subj_sql = f", [{msg_subject}]" if msg_subject else ", NULL"
            camp_sql = f", [{msg_campaign}]" if msg_campaign else ", NULL"
            campid_sql = f", [{msg_camp_id}]" if msg_camp_id else ", NULL"
            id_sql = f", [{msg_id_col}]" if msg_id_col else ", NULL"
            cur.execute(
                f"""
                SELECT [{msg_aref}], [{msg_dt}]{body_sql}{descr_sql}{ctype_sql}{subj_sql}{camp_sql}{campid_sql}{id_sql}
                FROM dbo.Messages
                WHERE [{msg_aref}] IN ({ph})
                  AND [{msg_dt}] IS NOT NULL
                """,
                chunk,
            )
            for aref, dt, body, descr, ctype, subj, campaign, camp_id_val, msg_id in cur.fetchall():
                _process_message_row(aref, dt, body, descr, ctype, subj, campaign, camp_id_val, msg_id)

    # Pass 2: ExternalAddress-IN — picks up customer-initiated SMS/Email/Call
    # rows that had no ARef on the source row (~98% of inbounds per the
    # comms scanner). Without this the timeline shows only our side.
    if msg_ext and msg_dt and external_to_aref:
        ext_keys = list(external_to_aref.keys())
        n_inbound_added = 0
        for chunk in chunked(ext_keys, 1500):
            ph = ",".join(["?"] * len(chunk))
            body_sql = f", [{msg_body}]" if msg_body else ", NULL"
            descr_sql = f", [{msg_descr}]" if msg_descr else ", NULL"
            ctype_sql = f", [{msg_ctype}]" if msg_ctype else ", NULL"
            subj_sql = f", [{msg_subject}]" if msg_subject else ", NULL"
            camp_sql = f", [{msg_campaign}]" if msg_campaign else ", NULL"
            campid_sql = f", [{msg_camp_id}]" if msg_camp_id else ", NULL"
            id_sql = f", [{msg_id_col}]" if msg_id_col else ", NULL"
            cur.execute(
                f"""
                SELECT [{msg_ext}], [{msg_dt}]{body_sql}{descr_sql}{ctype_sql}{subj_sql}{camp_sql}{campid_sql}{id_sql}
                FROM dbo.Messages
                WHERE [{msg_ext}] IN ({ph})
                  AND [{msg_dt}] IS NOT NULL
                  AND [{msg_descr}] IN (0, 1, 2)
                """,
                chunk,
            )
            for ext, dt, body, descr, ctype, subj, campaign, camp_id_val, msg_id in cur.fetchall():
                ext_key = (ext or "").strip().lower() if (ext and "@" in (ext or "")) else _norm_phone(ext)
                aref = external_to_aref.get(ext_key)
                if not aref:
                    continue
                before = len(seen_msg_keys)
                _process_message_row(aref, dt, body, descr, ctype, subj, campaign, camp_id_val, msg_id)
                if len(seen_msg_keys) > before:
                    n_inbound_added += 1
        print(f"# back-filled {n_inbound_added} inbound messages by ExternalAddress", flush=True)

    comm_conn.close()

    # ──────────── Sort + assemble output ───────────────────────────
    for aref, evs in interactions.items():
        evs.sort(key=lambda e: e["at"] or "")

    # Build inverse: primary → list of all family ARefs
    primary_to_family: dict[str, list[str]] = defaultdict(list)
    for ar, primary in aref_to_primary.items():
        primary_to_family[primary].append(ar)

    # ── PII redaction ────────────────────────────────────────────────
    # Mask all PII before writing JSON. Everything served to the browser
    # is masked at source, so the live page can't leak PII via
    # devtools/network inspection. Visible:
    #   - ARefs: last 5 chars only ('***************251627' style)
    #   - Surnames: replaced with ****** in both display fields and any
    #     mention inside message bodies / details.
    #   - Phone numbers (E.164 / local US formats): replaced with *******
    #   - Email addresses and DOBs: kept for now per the user's spec.
    ARE_RE_FULL = re.compile(r"\b\d{18,24}\b")  # ARefs are 22 digits but be lenient
    PHONE_RE = re.compile(
        r"(?<!\w)(?:\+?1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}(?!\w)"
    )

    def mask_aref(a):
        if not a: return a
        s = str(a)
        if len(s) <= 5: return s
        return "*" * (len(s) - 5) + s[-5:]

    def mask_arefs_in_text(text):
        if not text: return text
        return ARE_RE_FULL.sub(lambda m: mask_aref(m.group(0)), str(text))

    def mask_phones_in_text(text):
        if not text: return text
        return PHONE_RE.sub("*******", str(text))

    def mask_surname_in_text(text, surnames):
        if not text: return text
        out = str(text)
        for sn in surnames:
            if sn and len(sn) >= 3:
                out = re.sub(re.escape(sn), "******", out, flags=re.IGNORECASE)
        return out

    def mask_full_name_string(name):
        """'ALEX PERRY' → 'ALEX ******'. Keeps every part except the last."""
        if not name: return name
        parts = str(name).split()
        if len(parts) <= 1:
            return "******"
        return " ".join(parts[:-1] + ["******"])

    def redact_text(text, surnames):
        """Apply ARef + phone + surname masking to a free-text string."""
        t = mask_arefs_in_text(text)
        t = mask_phones_in_text(t)
        t = mask_surname_in_text(t, surnames)
        return t

    US_STATES = {
        "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
        "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
        "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
        "TX","UT","VT","VA","WA","WV","WI","WY","DC","PR","GU","VI","AS","MP",
    }

    def keep_only_state(addr_str: str) -> str:
        """Extract the 2-letter US state code from an address; mask everything else."""
        if not addr_str: return "******"
        for tok in re.split(r"[,\s]+", addr_str):
            t = tok.strip().upper()
            if t in US_STATES:
                return t
        return "******"

    def redact_detail_pair(k, v, surnames):
        """Mask the value of a details [key, value] pair appropriately."""
        if v is None: return [k, v]
        sv = str(v)
        kl = k.lower()
        if "name" in kl and "campaign" not in kl:
            return [k, mask_full_name_string(sv)]
        if "dob" in kl or "birth" in kl:
            return [k, "****-**-**"]
        if "address" in kl:
            return [k, keep_only_state(sv)]
        # Default: apply text redaction (ARef + phone + surname).
        return [k, redact_text(sv, surnames)]

    out_endpoints = {}
    for ep, arefs in samples.items():
        rows = []
        for aref in arefs:
            cust = customers_by_aref.get(aref, {})
            brw = cust.get("BRW", {})
            family = sorted(primary_to_family.get(aref, [aref]))

            # Build the set of surnames to scrub from this customer's
            # message bodies / detail values. Includes BRW + GT surnames
            # from every linked ARef in the family.
            surnames: set[str] = set()
            for fam_aref in family:
                fam_cust = customers_by_aref.get(fam_aref, {})
                for role_data in fam_cust.values():
                    sn = (role_data.get("surname") or "").strip()
                    if sn:
                        surnames.add(sn)

            # Walk each event and replace text fields with masked versions.
            masked_events = []
            for ev in interactions.get(aref, []):
                me = dict(ev)
                if "aref" in me:
                    me["aref"] = mask_aref(me["aref"])
                if "label" in me and me["label"]:
                    me["label"] = redact_text(me["label"], surnames)
                if "body" in me and me["body"]:
                    me["body"] = redact_text(me["body"], surnames)
                if "channel" in me and me["channel"]:
                    me["channel"] = mask_arefs_in_text(me["channel"])
                if "items" in me and isinstance(me["items"], list):
                    me["items"] = [redact_text(t, surnames) for t in me["items"]]
                if "details" in me and isinstance(me["details"], list):
                    me["details"] = [redact_detail_pair(k, v, surnames) for k, v in me["details"]]
                masked_events.append(me)

            # Mask the customer-summary fields too.
            display_name = None
            first = (brw.get("first_name") or "").strip()
            if first:
                display_name = f"{first} ******"
            elif brw.get("surname"):
                display_name = "******"

            rows.append({
                "aref": mask_aref(aref),
                "brw_name": display_name,
                "all_arefs": [mask_aref(a) for a in family],
                "application_count": len(family),
                "interaction_count": len(masked_events),
                "interactions": masked_events,
            })
        out_endpoints[ep] = rows

    output = {
        "snapshot_at": started.isoformat(),
        "snapshot_date": started.date().isoformat(),
        "lender_id": LENDER_ID,
        "lender_label": LENDER_LABEL,
        "month": f"{WINDOW_YEAR:04d}-{WINDOW_MONTH:02d}",
        "month_label": month_start.strftime("%B %Y"),
        "sample_size_per_endpoint": SAMPLE_SIZE,
        "endpoints": out_endpoints,
        "endpoint_labels": {
            "abandoned_before_page1":     "Abandoned before page 1",
            "dropped_before_brw_signed":  "Dropped before BRW signed",
            "no_accepted_guarantor":      "No accepted guarantor",
            "no_vc_reached":              "No VC reached",
            "vc_ready_not_paid_out":      "VC ready but not paid out",
        },
    }
    out_path = Path("pipeline-samples.json")
    out_path.write_text(json.dumps(output, indent=2, default=str))
    total_events = sum(len(c["interactions"]) for ep_list in out_endpoints.values() for c in ep_list)
    print(f"# wrote {out_path} ({out_path.stat().st_size:,} bytes); total events {total_events:,}", flush=True)


if __name__ == "__main__":
    main()
