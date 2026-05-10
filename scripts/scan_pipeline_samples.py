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

# Whitelist of user-meaningful TaskTypeIds. Anything not in here is hidden so
# we don't drown the timeline in 25 system tasks all stamped at the same
# minute when the application terminates.
TASK_WHITELIST: dict[int, str] = {
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


def format_web_event(blob, provider) -> str:
    """Render a WebBehaviours.WebEventObject value as a single-line label."""
    if blob is None:
        return f"Web visit ({provider})" if provider else "Web visit"
    raw = str(blob).strip()
    if not raw:
        return f"Web visit ({provider})" if provider else "Web visit"
    # Try to parse as JSON and pull common fields.
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        # Not JSON — clip and stringify.
        snippet = raw[:120]
        return f"Web: {snippet}" + (f" [{provider}]" if provider else "")
    if isinstance(obj, dict):
        bits = []
        # Most-likely useful keys, in priority order
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
            return f"Web: {label}" + (f" [{provider}]" if provider else "")
    # Fallback: stringify a short summary
    snippet = raw[:120]
    return f"Web: {snippet}" + (f" [{provider}]" if provider else "")


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

    # ──────────── (b) Application started event ─────────────────────
    for aref in all_sampled:
        info = aref_to_app[aref]
        if not info['created']:
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
        if info["purpose"] is not None:
            details.append(["Loan purpose", str(info["purpose"])])
        interactions[aref].append({
            "kind": "application_started",
            "at": iso(info['created']),
            "label": "Application started" + (f" (from purchased lead #{info['lead_id']})" if info['lead_id'] is not None else " (direct via website)"),
            "details": details,
        })

    # ──────────── (c) Tasks ────────────────────────────────────────
    print("# pulling Tasks for sampled ARefs…", flush=True)
    placeholder_in = ",".join(str(t) for t in TASK_WHITELIST.keys())
    for chunk in chunked(all_sampled, 1500):
        ph = ",".join(["?"] * len(chunk))
        cur.execute(
            f"""
            SELECT [{tasks_aref}], [{tasks_type}], [{tasks_gtref}],
                   [{tasks_done}], [{tasks_descr}]
            FROM dbo.Tasks
            WHERE [{tasks_aref}] IN ({ph})
              AND [{tasks_done}] IS NOT NULL
              AND [{tasks_type}] IN ({placeholder_in})
            """,
            chunk,
        )
        for aref, ttid, gtref, done, descr in cur.fetchall():
            ttid = int(ttid)
            who = "GT" if gtref is not None else "BRW"
            human_label = TASK_WHITELIST.get(ttid, f"Task #{ttid}")
            descr_str = (descr or "").strip()
            # Tasks.Description is the bare suffix ("All", "ColumboPage" etc).
            # Prepend the whitelist human label so the timeline reads cleanly.
            label_stem = human_label
            if descr_str and descr_str.lower() not in human_label.lower():
                label_stem = f"{human_label} ({descr_str})"
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
                    info = aref_to_app[aref]
                    if info["loan_amount"] is not None:
                        details.append(["Loan amount requested", f"${info['loan_amount']:,.0f}"])
                    if info["term"] is not None:
                        details.append(["Term requested", f"{info['term']} months"])
                    if info["purpose"] is not None:
                        details.append(["Loan purpose", str(info["purpose"])])
                if details:
                    ev["details"] = details
            interactions[aref].append(ev)

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
                kept.append({
                    "kind": "task_cluster",
                    "at": cluster_evs[0]["at"],
                    "label": f"Application closed — {cluster_size} tasks finalised in bulk",
                    "items": bullets,
                })
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
        for aref in all_sampled:
            info = aref_to_app[aref]
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
                    interactions[aref].append({
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
        for chunk in chunked(all_sampled, 1500):
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
                # WebEventObject is typically a JSON blob — try to extract a
                # human label like "page=/apply" or "event=apply1_submit".
                label = format_web_event(evobj, prov)
                interactions[aref].append({
                    "kind": "web_visit",
                    "at": iso(dt),
                    "label": label,
                })

    apps_conn.close()

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
    print(f"# Messages chosen: aref={msg_aref} dt={msg_dt} ct={msg_ctype} body={msg_body} subj={msg_subject} descr={msg_descr}", flush=True)
    if msg_aref and msg_dt:
        for chunk in chunked(all_sampled, 1500):
            ph = ",".join(["?"] * len(chunk))
            body_sql = f", [{msg_body}]" if msg_body else ", NULL"
            descr_sql = f", [{msg_descr}]" if msg_descr else ", NULL"
            ctype_sql = f", [{msg_ctype}]" if msg_ctype else ", NULL"
            subj_sql = f", [{msg_subject}]" if msg_subject else ", NULL"
            cur.execute(
                f"""
                SELECT [{msg_aref}], [{msg_dt}]{body_sql}{descr_sql}{ctype_sql}{subj_sql}
                FROM dbo.Messages
                WHERE [{msg_aref}] IN ({ph})
                  AND [{msg_dt}] IS NOT NULL
                """,
                chunk,
            )
            for aref, dt, body, descr, ctype, subj in cur.fetchall():
                ctype_str = (ctype or "").strip()
                descr_int = int(descr) if descr is not None and str(descr).strip() != "" else None
                # Description enum: 0/1/2 = inbound (SMS/Email/Call), 3+ = outbound
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
                if is_robot:
                    msg["label"] = f"Message from {ctype_str}Bot"
                    msg["redacted"] = True
                else:
                    body_text = (body or "").strip() if body else ""
                    if subj:
                        body_text = f"[{subj}] {body_text}".strip()
                    if not body_text:
                        body_text = "(empty body)"
                    if len(body_text) > 4000:
                        body_text = body_text[:4000] + " …[truncated]"
                    msg["body"] = body_text
                interactions[aref].append(msg)
    comm_conn.close()

    # ──────────── Sort + assemble output ───────────────────────────
    for aref, evs in interactions.items():
        evs.sort(key=lambda e: e["at"] or "")

    out_endpoints = {}
    for ep, arefs in samples.items():
        rows = []
        for aref in arefs:
            cust = customers_by_aref.get(aref, {})
            brw = cust.get("BRW", {})
            rows.append({
                "aref": aref,
                "brw_name": fmt_name(brw) or None,
                "interaction_count": len(interactions.get(aref, [])),
                "interactions": interactions.get(aref, []),
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
