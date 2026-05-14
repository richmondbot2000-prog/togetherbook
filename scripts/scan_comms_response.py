"""
Generate comms.json + comms-full.csv — response-time stats for customer-
initiated messages, split into 4 buckets by the customer's state at the
moment they sent the message:

  - unknown    : sender has no ARef on the inbound row
  - applicant  : ARef set; no live loan (balance > $10); no signed-not-rejected
                 guarantor on the application at message time
  - live_loan  : LoanbookId tracks to a loan with CurrentBalance > $10 AND no
                 arrears (Arrears = 0 and DateInArrearsLocal IS NULL) at the
                 most recent LoanHistory snapshot before the message timestamp
  - arrears    : LoanbookId tracks to a loan with CurrentBalance > $10 AND
                 either Arrears > 0 or DateInArrearsLocal IS NOT NULL at the
                 most recent LoanHistory snapshot before the message timestamp

Reply algorithm (locked spec, 2026-05-14):
  1. Take every inbound message (LenderId=6, SMS or Email).
  2. For each, scan forward on the same customer for the FIRST outbound
     message in the SAME channel (SMS→SMS, Email→Email) where the
     ClientType matches '%CRM%' or '%Responder%'. Every other ClientType
     (MessageFactory, UIVR, ApplyWebsite*, Whitebox, App, Dialler, Jack,
     internal monitors, etc.) is IGNORED — the search skips past as if
     those rows didn't exist.
  3. If a qualifying outbound exists within 14 days → classify:
        ClientType LIKE '%CRM%'      → Replied by Human
        ClientType LIKE '%Responder%' → Replied by Robot
     Otherwise → No reply.
  4. The same outbound can serve as the reply for multiple waiting inbounds;
     each inbound is evaluated independently.

We capture TWO reply variants per inbound so the page can toggle Reply Robot
in or out without re-querying:
  - reply_all   : first CRM-or-Responder reply
  - reply_human : first CRM-only reply (i.e. the human-agent view)

comms-full.csv is the audit-trail export: one row per inbound, PII-redacted,
showing the reply (or lack of one) it received. Linked from comms.html via
the "Download full list CSV" button.

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
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

import pyodbc


YEAR = 2026
OUTPUT_PATH = Path("comms.json")
CSV_PATH = Path("comms-full.csv")
MAX_REPLY_MINUTES = 14 * 24 * 60   # 14 days
ARREARS_FLAG_TYPES = (2, 3, 4, 6)  # Decline, DNL, Cancelled, FraudRisk

# This site reports ONLY Transform Credit / Together Loans data — LenderId 6.
# Every warehouse query that pulls business data MUST filter by this. Other
# lenders (Rapida, LendingMate, Fianceo, Tandolan, etc.) live in the same
# warehouse but are out of scope. See SPEC.md §0.5.
LENDER_ID = 6

# Description enum on dbo.Messages — an INT, not a string. Confirmed values
# from the diagnostic cross-tab (run 25868213776, 2026-05-14):
#   0 = InboundSMS, 1 = InboundEmail, 2 = InboundCall,
#   5 = OutboundSMS, 6 = OutboundEmail, 7 = OutboundCall, 8/10 = other outbound
# We pair only SMS→SMS and Email→Email — calls / letters / push notifications
# are NOT replies for this analysis, even if they go to the same address.
DESC_INBOUND_SMS    = 0
DESC_INBOUND_EMAIL  = 1
DESC_OUTBOUND_SMS   = 5
DESC_OUTBOUND_EMAIL = 6


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
# Returns one row per inbound. We pull the FULL inbound body + reply body so
# the same dataset feeds both the aggregate chart AND the row-level CSV
# export — no second pass over the warehouse.
COMMS_QUERY = f"""
DECLARE @from datetime2 = '{YEAR}-01-01';
DECLARE @to   datetime2 = '{YEAR+1}-01-01';
DECLARE @maxMinutes int = {MAX_REPLY_MINUTES};
DECLARE @lender   int      = {LENDER_ID};

-- Warehouse-actual schema (NOT the wiki's logical view):
--   dbo.Messages.Description is an INT enum: 0=SMS-in, 1=Email-in, 2=Call-in,
--   5=SMS-out, 6=Email-out. UTCTime is the single timestamp column.
--
-- Reply matching uses a POSITIVE LIST: an outbound only counts as a reply if
-- ClientType matches '%CRM%' (human via CRM) or '%Responder%' (Robot /
-- AiResponder). Everything else — MessageFactory, UIVR, ApplyWebsite*,
-- Whitebox, App, Dialler, Jack, internal monitors — is invisible to the
-- search and the algorithm skips past it.
WITH inbound AS (
    SELECT
        m.MessageId,
        m.ARef,
        m.LoanbookId,
        m.ExternalAddress,
        m.UTCTime,
        m.MessageBody,
        m.Description AS InboundDesc,
        CASE
            WHEN m.Description = 0 THEN 'SMS'
            WHEN m.Description = 1 THEN 'Email'
        END AS Channel
    FROM dbo.Messages m
    WHERE m.Description IN (0, 1)
      AND m.LenderId = @lender
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
    i.ExternalAddress,
    i.MessageBody AS InboundBody,
    r_all.UTCTime  AS ReplyAllTs,
    DATEDIFF(MINUTE, i.UTCTime, r_all.UTCTime) AS ReplyMinAll,
    r_all.MessageBody AS ReplyAllBody,
    r_all.ClientType  AS ReplyAllClientType,
    r_hum.UTCTime  AS ReplyHumanTs,
    DATEDIFF(MINUTE, i.UTCTime, r_hum.UTCTime) AS ReplyMinHuman
FROM inbound i
OUTER APPLY (
    -- First CRM-or-Responder reply (the "reply_all" view)
    SELECT TOP 1 o.UTCTime, o.MessageBody, o.ClientType
    FROM dbo.Messages o
    WHERE o.ExternalAddress = i.ExternalAddress
      AND o.LenderId = @lender
      AND o.Description = CASE i.InboundDesc WHEN 0 THEN 5 WHEN 1 THEN 6 END
      AND o.UTCTime >  i.UTCTime
      AND o.UTCTime <= DATEADD(MINUTE, @maxMinutes, i.UTCTime)
      AND (o.ClientType LIKE '%CRM%' OR o.ClientType LIKE '%Responder%')
    ORDER BY o.UTCTime ASC
) r_all
OUTER APPLY (
    -- First CRM-only reply (the "reply_human" view)
    SELECT TOP 1 o.UTCTime
    FROM dbo.Messages o
    WHERE o.ExternalAddress = i.ExternalAddress
      AND o.LenderId = @lender
      AND o.Description = CASE i.InboundDesc WHEN 0 THEN 5 WHEN 1 THEN 6 END
      AND o.UTCTime >  i.UTCTime
      AND o.UTCTime <= DATEADD(MINUTE, @maxMinutes, i.UTCTime)
      AND o.ClientType LIKE '%CRM%'
    ORDER BY o.UTCTime ASC
) r_hum;
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
            "MessageId":           r[0],
            "ARef":                (r[1] or "").strip() if r[1] else "",
            "LoanbookId":          (r[2] or "").strip() if r[2] else "",
            "DateReceivedUtc":     r[3],
            "Channel":             r[4],
            "ExternalAddress":     (r[5] or "").strip() if r[5] else "",
            "InboundBody":         r[6] or "",
            "ReplyAllTs":          r[7],
            "ReplyMinAll":         r[8],
            "ReplyAllBody":        r[9] or "",
            "ReplyAllClientType": (r[10] or "").strip() if r[10] else "",
            "ReplyHumanTs":        r[11],
            "ReplyMinHuman":       r[12],
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


def normalise_phone(s: str) -> str:
    """Strip everything non-digit. Use the LAST 10 digits as the lookup key
    so a US-format Twilio +1XXXX matches a Customers.Telephones row stored
    without country code. International numbers will need wider tooling
    later but US-only is fine for v1."""
    digits = "".join(c for c in (s or "") if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def fetch_contact_to_aref() -> tuple[dict[str, str], dict[str, str]]:
    """Build lookup maps so an ARef-less inbound can be re-identified by
    the sender's phone or email.

    Returns (phone_map, email_map). When a contact maps to multiple ARefs
    (customer applied multiple times) we keep the lexicographically max
    ARef — a proxy for "most recent" since ARef embeds a timestamp in its
    leading digits. Good enough for v1; the customer's bucket is the same
    on either application anyway in practice.
    """
    print("[apps] building contact -> ARef maps…", flush=True)
    cn = pyodbc.connect(conn_str("ReportingApplications"), timeout=30)
    phone_map: dict[str, str] = {}
    email_map: dict[str, str] = {}
    try:
        cur = cn.cursor()
        # Warehouse vintages drift; discover the actual column names.
        tel_num_col = pick_column(cur, "dbo", "Telephones",
                                  "Number", "PhoneNumber", "TelephoneNumber",
                                  "Telephone", "Phone")
        tel_cust_col = pick_column(cur, "dbo", "Telephones",
                                   "CustomerId", "CustomerID")
        em_addr_col = pick_column(cur, "dbo", "Emails",
                                  "Email", "EmailAddress", "Address")
        em_cust_col = pick_column(cur, "dbo", "Emails",
                                  "CustomerId", "CustomerID")
        cust_id_col = pick_column(cur, "dbo", "Customers",
                                  "CustomerId", "CustomerID")
        cust_aref_col = pick_column(cur, "dbo", "Customers", "ARef")
        print(f"  discovered: Telephones.{tel_num_col}/{tel_cust_col}, "
              f"Emails.{em_addr_col}/{em_cust_col}, "
              f"Customers.{cust_id_col}/{cust_aref_col}", flush=True)
        if not (tel_num_col and tel_cust_col and cust_id_col and cust_aref_col):
            print("  required phone columns missing — skipping phone lookup", flush=True)
        else:
            cur.execute(f"""
                SELECT t.[{tel_num_col}], c.[{cust_aref_col}]
                FROM dbo.Telephones t
                JOIN dbo.Customers c ON c.[{cust_id_col}] = t.[{tel_cust_col}]
                WHERE t.[{tel_num_col}] IS NOT NULL
                  AND c.[{cust_aref_col}] IS NOT NULL
            """)
            for num, aref in cur.fetchall():
                key = normalise_phone(num)
                if not key: continue
                aref = (aref or "").strip()
                if not aref: continue
                cur_aref = phone_map.get(key)
                if cur_aref is None or aref > cur_aref:
                    phone_map[key] = aref
        if not (em_addr_col and em_cust_col):
            print("  required email columns missing — skipping email lookup", flush=True)
        else:
            cur.execute(f"""
                SELECT e.[{em_addr_col}], c.[{cust_aref_col}]
                FROM dbo.Emails e
                JOIN dbo.Customers c ON c.[{cust_id_col}] = e.[{em_cust_col}]
                WHERE e.[{em_addr_col}] IS NOT NULL
                  AND c.[{cust_aref_col}] IS NOT NULL
            """)
            for email, aref in cur.fetchall():
                key = (email or "").strip().lower()
                if not key: continue
                aref = (aref or "").strip()
                if not aref: continue
                cur_aref = email_map.get(key)
                if cur_aref is None or aref > cur_aref:
                    email_map[key] = aref
    finally:
        cn.close()
    print(f"[apps] phone_map={len(phone_map)} entries, email_map={len(email_map)} entries", flush=True)
    return phone_map, email_map


def fetch_aref_to_loanbook() -> dict[str, str]:
    """ARef -> latest LoanbookId. The ARef ↔ LoanbookId link lives in different
    tables depending on warehouse vintage:
      - dbo.Loan in ReportingLoanbook may carry ARef (informational copy)
      - dbo.ESignatures in ReportingApplications carries LoanbookId (written
        back at payout)
      - dbo.Applications.LoanbookId may exist directly

    Try the Loanbook side first; if no ARef column there, fall back to the
    Applications side. Take lexicographically max LoanbookId per ARef so we
    bucket against the LATEST loan."""
    print("[loanbook] building ARef -> LoanbookId map…", flush=True)
    aref_to_lb: dict[str, str] = {}

    # Try Loanbook.dbo.Loan.ARef
    cn = pyodbc.connect(conn_str("ReportingLoanbook"), timeout=30)
    try:
        cur = cn.cursor()
        loan_aref = pick_column(cur, "dbo", "Loan", "ARef", "Aref")
        loan_lb = pick_column(cur, "dbo", "Loan", "LoanbookId", "LoanBookId", "LoanbookID")
        if loan_aref and loan_lb:
            cur.execute(f"""
                SELECT [{loan_aref}], [{loan_lb}]
                FROM dbo.Loan
                WHERE [{loan_aref}] IS NOT NULL AND [{loan_lb}] IS NOT NULL
            """)
            for aref, lb in cur.fetchall():
                aref = (aref or "").strip()
                lb = (lb or "").strip()
                if not (aref and lb): continue
                if lb > (aref_to_lb.get(aref) or ""):
                    aref_to_lb[aref] = lb
            print(f"  via Loanbook.Loan: {len(aref_to_lb)} mappings", flush=True)
    finally:
        cn.close()

    # Fall back to / supplement with Applications.ESignatures.LoanbookId
    if not aref_to_lb:
        cn = pyodbc.connect(conn_str("ReportingApplications"), timeout=30)
        try:
            cur = cn.cursor()
            es_lb = pick_column(cur, "dbo", "ESignatures", "LoanbookId", "LoanBookId", "LoanbookID")
            if es_lb:
                # ESignatures has CustomerId; Customers has ARef. Join through.
                cur.execute(f"""
                    SELECT c.ARef, e.[{es_lb}]
                    FROM dbo.ESignatures e
                    JOIN dbo.Customers c ON c.CustomerId = e.CustomerId
                    WHERE c.ARef IS NOT NULL AND e.[{es_lb}] IS NOT NULL
                """)
                for aref, lb in cur.fetchall():
                    aref = (aref or "").strip()
                    lb = (lb or "").strip()
                    if not (aref and lb): continue
                    if lb > (aref_to_lb.get(aref) or ""):
                        aref_to_lb[aref] = lb
                print(f"  via Applications.ESignatures: {len(aref_to_lb)} mappings", flush=True)
        finally:
            cn.close()

    print(f"[loanbook] {len(aref_to_lb)} ARef -> LoanbookId mappings total", flush=True)
    return aref_to_lb


def print_outbound_clienttype_diagnostic() -> None:
    """One-shot: dump every (Description, ClientType) pair on outbound rows
    so we can verify the Description→channel integer mapping. Also reports
    counts by Description alone so the int enum is visible at a glance.
    Helps catch cases like the OutboundCall transcript being mistaken for
    an SMS reply (fix shipped 2026-05-14)."""
    print("[diag] 2026 message Description enum distribution (LenderId 6)…", flush=True)
    cn = pyodbc.connect(conn_str("ReportingCommunications"), timeout=30)
    try:
        cur = cn.cursor()
        cur.execute(f"""
            DECLARE @from datetime2 = '{YEAR}-01-01';
            DECLARE @to   datetime2 = '{YEAR+1}-01-01';
            SELECT Description, COUNT(*) AS n
            FROM dbo.Messages
            WHERE LenderId = {LENDER_ID}
              AND UTCTime >= @from
              AND UTCTime <  @to
            GROUP BY Description
            ORDER BY Description
        """)
        for desc, n in cur.fetchall():
            print(f"  Description={desc}  n={n}", flush=True)
        print("[diag] 2026 outbound (Description, ClientType) cross-tab (LenderId 6)…", flush=True)
        cur.execute(f"""
            DECLARE @from datetime2 = '{YEAR}-01-01';
            DECLARE @to   datetime2 = '{YEAR+1}-01-01';
            SELECT Description, ISNULL(ClientType, '(null)') AS ct, COUNT(*) AS n
            FROM dbo.Messages
            WHERE Description >= 3
              AND LenderId = {LENDER_ID}
              AND UTCTime >= @from
              AND UTCTime <  @to
            GROUP BY Description, ClientType
            ORDER BY Description, n DESC
        """)
        for desc, ct, n in cur.fetchall():
            print(f"  Description={desc}  ClientType={ct!r:<40} n={n}", flush=True)
    finally:
        cn.close()


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


# --------------------------------------------------------------------------
# Message-sample / redaction (for the page's "10 examples per bucket" panel)

# Scanner picks 100 candidates per bucket; the page then randomly draws 10
# of those on every reload so the user sees a fresh sample each time without
# hitting the warehouse on click.
SAMPLES_PER_BUCKET = 100
SAMPLE_BODY_CAP    = 320     # chars

RX_ARE_F     = re.compile(r"\b\d{22}\b")
RX_LOANBOOK  = re.compile(r"\b\d{4,8}[A-Z]{3,6}\b")
RX_SSN       = re.compile(r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b")
RX_EMAIL     = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
RX_CARD      = re.compile(r"\b(?:\d[ -]?){13,19}\b")
RX_PHONE     = re.compile(r"(?<!\d)\+?\d[\d\s\-().]{7,}\d(?!\d)")


def redact(body: str, names, cap: int | None = SAMPLE_BODY_CAP) -> str:
    """PII-redact a message body. `names` is an iterable of names to scrub
    (whole-word case-insensitive). `cap` truncates the result; pass None to
    keep the full body (used by the CSV audit export where we want every
    word of the original message preserved)."""
    if not body: return ""
    s = body
    s = RX_CARD.sub("****", s)
    s = RX_SSN.sub("****", s)
    s = RX_ARE_F.sub("****", s)
    s = RX_LOANBOOK.sub("****", s)
    s = RX_EMAIL.sub("****@****", s)
    s = RX_PHONE.sub("****", s)
    for n in names:
        if len(n) < 3: continue
        s = re.compile(r"\b" + re.escape(n) + r"\b", re.IGNORECASE).sub("****", s)
    if cap is not None and len(s) > cap:
        s = s[:cap].rstrip() + " …[truncated]"
    return s


def fetch_names_by_aref(arefs: set[str]) -> dict[str, set[str]]:
    """For every ARef in the inbound set, pull first/last name. Used by the
    full-list CSV so each row redacts only that customer's own name. Returns
    {aref: {first, last}}; missing ARefs map to an empty set."""
    if not arefs:
        return {}
    print(f"[csv] fetching names for {len(arefs)} ARefs (CSV redaction)…", flush=True)
    out: dict[str, set[str]] = {}
    cn = pyodbc.connect(conn_str("ReportingApplications"), timeout=30)
    try:
        cur = cn.cursor()
        for chunk in chunked(arefs, 1500):
            ph = ",".join("?" * len(chunk))
            cur.execute(
                f"""SELECT ARef, FirstName, Surname
                    FROM dbo.Customers WHERE ARef IN ({ph})""",
                list(chunk),
            )
            for aref, fn, sn in cur.fetchall():
                aref = (aref or "").strip()
                if not aref: continue
                bag = out.setdefault(aref, set())
                if fn and fn.strip(): bag.add(fn.strip())
                if sn and sn.strip(): bag.add(sn.strip())
    finally:
        cn.close()
    print(f"[csv] name map built ({len(out)} ARefs resolved)", flush=True)
    return out


def write_full_csv(inbounds: list, loan_history, signed_gt, names_by_aref: dict[str, set[str]]) -> None:
    """Audit-trail export: one row per inbound, PII-redacted. Linked from
    comms.html as the "Download full list CSV" button. Streamed to disk so
    we never materialise the full body of every message in a single string.

    Result column uses the reply_all view (first CRM-or-Responder reply):
      ClientType LIKE '%CRM%'        -> Replied by Human
      ClientType LIKE '%Responder%'  -> Replied by Robot
      No qualifying reply in 14 days -> No reply
    """
    import csv as _csv
    print(f"[csv] writing {CSV_PATH} ({len(inbounds)} rows)…", flush=True)
    n_human = n_robot = n_none = 0
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f, quoting=_csv.QUOTE_MINIMAL)
        w.writerow([
            "inbound_datetime_utc",
            "inbound_channel",
            "inbound_body",
            "result",
            "hours_to_reply",
            "reply_datetime_utc",
            "reply_channel",
            "reply_client_type",
            "reply_body",
            "customer_aref_last5",
            "customer_state_at_inbound",
        ])
        for i in inbounds:
            bucket = classify(i, loan_history, signed_gt)
            aref = i.get("ARef") or ""
            row_names = names_by_aref.get(aref, set())
            in_body = redact(i.get("InboundBody") or "", row_names, cap=None)

            ct = i.get("ReplyAllClientType") or ""
            mins = i.get("ReplyMinAll")
            if mins is None or not ct:
                result = "No reply"
                hours = ""
                r_ts = ""
                r_ct = ""
                r_body = ""
                n_none += 1
            else:
                ct_l = ct.lower()
                if "responder" in ct_l:
                    result = "Replied by Robot"
                    n_robot += 1
                elif "crm" in ct_l:
                    result = "Replied by Human"
                    n_human += 1
                else:
                    result = "Replied (uncategorised)"  # shouldn't happen — positive list filters this out
                hours = f"{mins / 60:.2f}"
                ts = i.get("ReplyAllTs")
                r_ts = ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(ts, "strftime") else (str(ts) if ts else "")
                r_ct = ct
                r_body = redact(i.get("ReplyAllBody") or "", row_names, cap=None)

            in_ts = i.get("DateReceivedUtc")
            in_ts_s = in_ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(in_ts, "strftime") else str(in_ts)
            w.writerow([
                in_ts_s,
                i.get("Channel") or "",
                in_body,
                result,
                hours,
                r_ts,
                i.get("Channel") or "" if result != "No reply" else "",
                r_ct,
                r_body,
                aref[-5:] if aref else "",
                bucket,
            ])
    print(
        f"[csv] {CSV_PATH} written — Human={n_human}, Robot={n_robot}, NoReply={n_none}",
        flush=True,
    )


def sample_messages(inbounds, signed_gt, loan_history) -> dict:
    """Pick up to SAMPLES_PER_BUCKET inbounds per bucket from the FULL pool
    (regardless of whether a reply landed). For each, capture both reply
    variants — first non-MF reply ("all") and first non-MF non-Responder
    reply ("human") — so the page can filter the sample list by the same
    two checkboxes that drive the chart, without re-querying."""
    rng = random.Random()  # non-deterministic — pool reshuffles each scan
    pools = defaultdict(list)
    for i in inbounds:
        b = classify(i, loan_history, signed_gt)
        if b in ("unknown", "applicant", "live_loan", "arrears"):
            pools[b].append(i)
    chosen: dict[str, list] = {}
    for b, pool in pools.items():
        if not pool: chosen[b] = []; continue
        chosen[b] = rng.sample(pool, min(SAMPLES_PER_BUCKET, len(pool)))

    # Flatten for bulk fetch
    flat = [(b, i) for b, lst in chosen.items() for i in lst]
    if not flat:
        return {b: [] for b in ("unknown", "applicant", "live_loan", "arrears")}

    msg_ids = [i["MessageId"] for _, i in flat]
    print(f"[samples] fetching bodies + replies for {len(msg_ids)} inbounds…", flush=True)
    inbound_info = {}
    reply_all_info: dict = {}
    reply_human_info: dict = {}
    cn = pyodbc.connect(conn_str("ReportingCommunications"), timeout=60)
    try:
        cur = cn.cursor()
        # Subject lives on Messages in some warehouses but not all. Discover.
        subj_col = pick_column(cur, "dbo", "Messages",
                               "Subject", "MessageTitle", "MessageSubject")
        subj_sql_sel = f", [{subj_col}]" if subj_col else ", '' AS Subject"
        # 1. Inbound rows
        ph = ",".join("?" * len(msg_ids))
        cur.execute(
            f"""SELECT MessageId, UTCTime, ExternalAddress, MessageBody{subj_sql_sel}, Description
                FROM dbo.Messages WHERE MessageId IN ({ph})""",
            msg_ids,
        )
        for mid, ts, ext, body, subj, desc in cur.fetchall():
            inbound_info[mid] = {
                "ts": ts, "ext": (ext or "").strip(),
                "body": body or "", "subj": subj or "",
                "desc": desc,
            }
        # 2. Two reply variants per inbound — POSITIVE-LIST filter:
        # reply_all  = first CRM-or-Responder same-channel outbound within 14 d.
        # reply_human = first CRM-only same-channel outbound within 14 d.
        # Sequential queries; bounded (100 × 4 buckets × 2 ≈ 800 queries).
        for mid, info in inbound_info.items():
            cur.execute(
                f"""SELECT TOP 1 UTCTime, MessageBody{subj_sql_sel}, ClientType
                    FROM dbo.Messages
                    WHERE ExternalAddress = ?
                      AND LenderId = {LENDER_ID}
                      AND Description = CASE WHEN ? = 0 THEN 5 WHEN ? = 1 THEN 6 END
                      AND UTCTime >  ?
                      AND UTCTime <= DATEADD(MINUTE, ?, ?)
                      AND (ClientType LIKE '%CRM%' OR ClientType LIKE '%Responder%')
                    ORDER BY UTCTime ASC""",
                info["ext"], info["desc"], info["desc"], info["ts"], MAX_REPLY_MINUTES, info["ts"],
            )
            r = cur.fetchone()
            if r:
                reply_all_info[mid] = {
                    "ts": r[0], "body": r[1] or "",
                    "subj": r[2] or "", "client_type": (r[3] or "").strip(),
                }
            cur.execute(
                f"""SELECT TOP 1 UTCTime, MessageBody{subj_sql_sel}, ClientType
                    FROM dbo.Messages
                    WHERE ExternalAddress = ?
                      AND LenderId = {LENDER_ID}
                      AND Description = CASE WHEN ? = 0 THEN 5 WHEN ? = 1 THEN 6 END
                      AND UTCTime >  ?
                      AND UTCTime <= DATEADD(MINUTE, ?, ?)
                      AND ClientType LIKE '%CRM%'
                    ORDER BY UTCTime ASC""",
                info["ext"], info["desc"], info["desc"], info["ts"], MAX_REPLY_MINUTES, info["ts"],
            )
            r = cur.fetchone()
            if r:
                reply_human_info[mid] = {
                    "ts": r[0], "body": r[1] or "",
                    "subj": r[2] or "", "client_type": (r[3] or "").strip(),
                }
    finally:
        cn.close()

    # 3. Pull first + last names for redaction. Use all ARefs we sampled.
    sample_arefs = {i["ARef"] for _, i in flat if i["ARef"]}
    names: set[str] = set()
    if sample_arefs:
        cn = pyodbc.connect(conn_str("ReportingApplications"), timeout=30)
        try:
            cur = cn.cursor()
            for chunk in chunked(sample_arefs, 1500):
                ph = ",".join("?" * len(chunk))
                cur.execute(
                    f"""SELECT FirstName, Surname FROM dbo.Customers WHERE ARef IN ({ph})""",
                    list(chunk),
                )
                for fn, sn in cur.fetchall():
                    if fn: names.add(fn.strip())
                    if sn: names.add(sn.strip())
        finally:
            cn.close()
    print(f"[samples] pulled {len(names)} names for redaction", flush=True)

    def fmt_reply(reply, info):
        if not reply: return None
        body = (reply["subj"] + " :: " + reply["body"]) if reply["subj"] else reply["body"]
        gap_min = int((reply["ts"] - info["ts"]).total_seconds() / 60)
        return {
            "at": reply["ts"].strftime("%Y-%m-%d %H:%M") if hasattr(reply["ts"], "strftime") else str(reply["ts"]),
            "response_minutes": gap_min,
            "client_type": reply["client_type"],
            "body": redact(body, names),
        }

    out: dict[str, list] = {b: [] for b in ("unknown", "applicant", "live_loan", "arrears")}
    for bucket, i in flat:
        mid = i["MessageId"]
        info = inbound_info.get(mid)
        if not info: continue
        in_body = (info["subj"] + " :: " + info["body"]) if info["subj"] else info["body"]
        aref = i["ARef"] or ""
        out[bucket].append({
            "aref_last5":  aref[-5:] if aref else None,
            "channel":     "Email" if info["desc"] == 1 else "SMS",
            "received_at": info["ts"].strftime("%Y-%m-%d %H:%M") if hasattr(info["ts"], "strftime") else str(info["ts"]),
            "message":     redact(in_body, names),
            "reply_all":   fmt_reply(reply_all_info.get(mid), info),
            "reply_human": fmt_reply(reply_human_info.get(mid), info),
        })
    return out


def main() -> None:
    print_outbound_clienttype_diagnostic()
    inbounds = fetch_inbound_paired()

    # AUGMENTATION: most inbounds land without an ARef on the row because the
    # IMAP/SMS parser couldn't extract one. Look the sender's email/phone up
    # in Applications.Customers contact tables and back-fill ARef + LoanbookId
    # so they classify into the right bucket instead of all collapsing into
    # 'unknown'.
    phone_map, email_map = fetch_contact_to_aref()
    aref_to_lb = fetch_aref_to_loanbook()
    augmented = 0
    needed = 0
    for msg in inbounds:
        if msg["ARef"]:
            continue
        needed += 1
        ext = msg.get("ExternalAddress", "")
        if not ext: continue
        derived_aref = None
        if "@" in ext:
            derived_aref = email_map.get(ext.lower())
        else:
            derived_aref = phone_map.get(normalise_phone(ext))
        if not derived_aref: continue
        msg["ARef"] = derived_aref
        if not msg["LoanbookId"]:
            lb = aref_to_lb.get(derived_aref)
            if lb: msg["LoanbookId"] = lb
        augmented += 1
    print(
        f"[augment] back-filled ARef on {augmented} of {needed} ARef-less "
        f"inbounds ({(augmented*100)//max(needed,1)}%)",
        flush=True,
    )

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

    # Weekly stats with median (median doesn't decompose from daily sums, so
    # we compute it server-side here over the raw per-message response_minutes
    # within each (week, bucket) cell). Cheap because everything is already in
    # memory.
    import statistics
    from datetime import timedelta
    raw: dict[tuple[str, str], dict] = defaultdict(lambda: {
        "total": 0,
        "all_replied": [],
        "human_replied": [],
        "no_reply_all": 0,
        "no_reply_human": 0,
    })
    for i in inbounds:
        bucket = classify(i, loan_history, signed_gt)
        if bucket not in ("unknown", "applicant", "live_loan", "arrears"):
            continue
        d = i["DateReceivedUtc"]
        monday = d - timedelta(days=d.weekday())
        wk = monday.strftime("%Y-%m-%d")
        cell = raw[(wk, bucket)]
        cell["total"] += 1
        if i["ReplyMinAll"] is not None:
            cell["all_replied"].append(float(i["ReplyMinAll"]))
        else:
            cell["no_reply_all"] += 1
        if i["ReplyMinHuman"] is not None:
            cell["human_replied"].append(float(i["ReplyMinHuman"]))
        else:
            cell["no_reply_human"] += 1

    def variant_stats(replied_vals, no_reply_count):
        result = {"n_replied": len(replied_vals)}
        if replied_vals:
            result["mean_replied"] = round(statistics.mean(replied_vals), 1)
            result["median_replied"] = round(statistics.median(replied_vals), 1)
        else:
            result["mean_replied"] = None
            result["median_replied"] = None
        # 'inc' = no-reply messages capped at 14 d
        inc = replied_vals + [MAX_REPLY_MINUTES] * no_reply_count
        if inc:
            result["mean_inc"] = round(statistics.mean(inc), 1)
            result["median_inc"] = round(statistics.median(inc), 1)
        else:
            result["mean_inc"] = None
            result["median_inc"] = None
        return result

    weekly_stats: dict[str, dict[str, dict]] = {b: {} for b in BUCKETS}
    for (wk, bucket), cell in raw.items():
        if bucket not in weekly_stats:
            bucket = "other"
        weekly_stats[bucket][wk] = {
            "n_total": cell["total"],
            "all":   variant_stats(cell["all_replied"],   cell["no_reply_all"]),
            "human": variant_stats(cell["human_replied"], cell["no_reply_human"]),
        }

    # Sample 10 message/reply pairs per bucket for visual inspection on the
    # page. Bodies are PII-redacted.
    samples = sample_messages(inbounds, signed_gt, loan_history)
    out = {
        "schema_version":    1,
        "updated_at":        datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "year":              YEAR,
        "channels":          ["SMS", "Email"],
        "buckets":           ["unknown", "applicant", "live_loan", "arrears"],
        "max_reply_minutes": MAX_REPLY_MINUTES,
        "totals_by_bucket":  totals,
        "series":            series,
        "weekly_stats":      weekly_stats,
        "samples":           samples,
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

    # Full-list CSV audit export — built from the same inbound rows so the
    # CSV and the chart always tell the same story for the same scan.
    csv_arefs = {i["ARef"] for i in inbounds if i.get("ARef")}
    names_by_aref = fetch_names_by_aref(csv_arefs)
    write_full_csv(inbounds, loan_history, signed_gt, names_by_aref)


if __name__ == "__main__":
    main()
