"""
Generate first-contact.json — the first inbound email we received from each
US borrower or guarantor AFTER their loan was paid out, going back 3 months.

Used by 1stcontact.html on the kids site.

Privacy note: this writes to a public JSON file served via GitHub Pages.
We aggressively redact direct identifiers (card numbers, SSNs, ARefs,
phone numbers, email addresses, URLs, the customer's own first / middle /
last name, and the ExternalName parsed from the email's From: header).
Indirect identifiers in free-text bodies (third-party names, employer,
dates of birth in prose, addresses) cannot be detected reliably and may
remain — see the README note in this folder.

Required env vars: same set the row-counts scan uses
(FABRIC_SQL_ENDPOINT, FABRIC_TENANT_ID, FABRIC_CLIENT_ID, FABRIC_CLIENT_SECRET).
"""
from __future__ import annotations

import datetime
import html
import json
import os
import re
import sys
from pathlib import Path

import pyodbc

MAX_BODY_CHARS  = 1200      # cap per email so the JSON stays under a few MB
MAX_ITEMS_OUT   = 200       # most recent N first-contacts on the page
MONTHS_BACK     = 3

# LenderId → display label. The Lenders table's name column isn't documented
# in the wiki I had access to when writing this; hard-mapping by id is more
# reliable than guessing the column name. Add new IDs here as they appear.
LENDER_LABELS = {
    6: "Together Loans / TransformCredit",
}


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


# ---- Step 1: paid-out loans + their customers (one row per BRW + per GT) ----
LOANS_QUERY = f"""
DECLARE @cutoff date = DATEADD(month, -{MONTHS_BACK}, CAST(GETDATE() AS date));

SELECT
  li.LoanBookID,
  CAST(li.LoanAgreementDateLocal AS date) AS PayoutDate,
  l.LenderID,
  c.GtRef,
  c.RelationToBrw,
  c.FirstName,
  c.MiddleName,
  c.LastName
FROM dbo.LoanAtInception li
JOIN dbo.Loan      l  ON l.LoanBookID = li.LoanBookID
JOIN dbo.Customer  c  ON c.LoanBookID = li.LoanBookID
JOIN dbo.Lenders   le ON le.LenderID  = l.LenderID
WHERE le.Country = 'USA'
  AND CAST(li.LoanAgreementDateLocal AS date) >= @cutoff
"""


# ---- Step 2: inbound emails in the same window with a LoanbookId attached ----
EMAILS_QUERY = f"""
DECLARE @cutoff datetime = DATEADD(month, -{MONTHS_BACK}, GETDATE());

SELECT
  m.LoanbookId,
  m.ARef,
  m.GtRef,
  m.UTCTime,
  m.MessageTitle,
  m.MessageBody,
  m.ExternalAddress,
  m.ExternalName
FROM dbo.Messages m
WHERE m.Description = 'InboundEmail'
  AND m.UTCTime >= @cutoff
  AND m.LoanbookId IS NOT NULL
ORDER BY m.LoanbookId, ISNULL(m.GtRef, 0), m.UTCTime ASC
"""


# --------------------------------------------------------------------------
# PII redaction

REDACTED = "****"

# Patterns ordered most-specific-first so an earlier sub doesn't mangle a later one.
RX_CARD     = re.compile(r"\b(?:\d[ -]?){13,19}\b")
RX_SSN      = re.compile(r"\b\d{3}[\s-]?\d{2}[\s-]?\d{4}\b")
RX_LONG_ID  = re.compile(r"\b\d{13,}\b")           # ARef (22 digits) + similar long account refs
RX_PHONE    = re.compile(
    r"(?:(?:\+?1[\s.-]?)?)?\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b"
)
RX_EMAIL    = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
RX_URL      = re.compile(r"https?://\S+")          # account links often carry tokens
RX_HTML_TAG = re.compile(r"<[^>]+>")
RX_WS       = re.compile(r"\s+")
# Date-of-birth-ish phrases — best-effort. "DOB: 04/12/1985", "born 1985"
RX_DOB_PROSE = re.compile(
    r"\b(?:DOB|D\.O\.B\.?|date of birth|born(?:\s+on)?)\s*[:\-]?\s*"
    r"(?:\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{4})",
    re.IGNORECASE,
)
# US street addresses — number + words + suffix. Best-effort.
RX_STREET = re.compile(
    r"\b\d{1,6}\s+[A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*){0,4}\s+"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|Boulevard|Blvd|Court|Ct|Way|Place|Pl|Highway|Hwy|Parkway|Pkwy|Terrace|Ter|Circle|Cir)\b\.?",
    re.IGNORECASE,
)


def _redact_text(text: str, name_parts: list[str]) -> str:
    if not text:
        return ""

    # Strip HTML tags (many emails are HTML-formatted) before pattern matching.
    text = html.unescape(text)
    text = RX_HTML_TAG.sub(" ", text)
    # Collapse whitespace early so multi-line patterns are easier to match.
    text = RX_WS.sub(" ", text).strip()

    # Direct identifiers — apply in this order; later patterns shouldn't undo earlier ones.
    text = RX_CARD.sub(REDACTED, text)
    text = RX_SSN.sub(REDACTED, text)
    text = RX_LONG_ID.sub(REDACTED, text)
    text = RX_PHONE.sub(REDACTED, text)
    text = RX_EMAIL.sub(REDACTED, text)
    text = RX_URL.sub(REDACTED, text)
    text = RX_DOB_PROSE.sub(REDACTED, text)
    text = RX_STREET.sub(REDACTED, text)

    # Customer's own name parts. Word-boundary, case-insensitive. Skip parts
    # shorter than 3 chars so initials and short common nouns aren't blown
    # away ("So", "Lee" the verb vs "Lee" the surname is still risky but
    # 3-char minimum is a reasonable trade-off).
    for part in name_parts:
        if not part:
            continue
        clean = part.strip()
        if len(clean) < 3:
            continue
        text = re.sub(r"\b" + re.escape(clean) + r"\b", REDACTED, text, flags=re.IGNORECASE)

    return text.strip()


def _split_external_name(ext_name: str) -> list[str]:
    """ExternalName is the parsed sender-name from the email's From: header.
    Often the customer's own name. Split into pieces so each part is redacted
    individually (covers "Bob Smith" → "Bob" and "Smith" both stripped).
    """
    if not ext_name:
        return []
    return [p for p in re.split(r"[\s,;]+", ext_name) if p]


# --------------------------------------------------------------------------
# Driver

def main() -> None:
    started = datetime.datetime.utcnow()
    print(f"# scan_first_contact start ({started.isoformat()}Z)", flush=True)

    # Step 1 — pull paid-out loans + customer name parts.
    conn = pyodbc.connect(conn_str("ReportingLoanbook"), timeout=30)
    conn.timeout = 120
    cur = conn.cursor()
    cur.execute(LOANS_QUERY)

    # Key: (LoanBookID, gtref_or_0). Value: dict with payout_date, lender,
    # role (BRW/GT), and the name parts to redact.
    loans: dict[tuple[int, int], dict] = {}
    for (loanbook_id, payout_date, lender_id,
         gtref, relation_to_brw, first_name, middle_name, last_name) in cur.fetchall():
        gtkey = int(gtref) if gtref else 0
        # GT rows have RelationToBrw set; BRW rows have it NULL (per the
        # yesterday-payouts script's convention).
        role = "BRW" if relation_to_brw is None else "GT"
        lid = int(lender_id) if lender_id else None
        loans[(int(loanbook_id), gtkey)] = {
            "payout_date":  payout_date,
            "lender_id":    lid,
            "lender_name":  LENDER_LABELS.get(lid, f"Lender {lid}" if lid else "Unknown lender"),
            "role":         role,
            "name_parts":   [
                (first_name  or "").strip(),
                (middle_name or "").strip(),
                (last_name   or "").strip(),
            ],
        }
    conn.close()
    print(f"# loans paid out in last {MONTHS_BACK} months: {len(loans)}", flush=True)

    if not loans:
        _write_empty(started)
        return

    # Step 2 — pull inbound emails in the same window and walk them ordered
    # by (LoanbookId, GtRef, UTCTime ASC) so the first row per key is the
    # earliest email — and that's our first-contact candidate.
    conn = pyodbc.connect(conn_str("ReportingCommunications"), timeout=30)
    conn.timeout = 300
    cur = conn.cursor()
    cur.execute(EMAILS_QUERY)

    seen_keys: set[tuple[int, int]] = set()
    first_contacts: list[dict] = []
    skipped_no_loan      = 0
    skipped_before_payout = 0

    for (loanbook_id, aref, gtref, utc_time, subject, body,
         ext_addr, ext_name) in cur.fetchall():
        if loanbook_id is None:
            continue
        gtkey = int(gtref) if gtref else 0
        key   = (int(loanbook_id), gtkey)
        if key in seen_keys:
            continue
        loan = loans.get(key)
        if loan is None:
            skipped_no_loan += 1
            continue
        # Compare on date level — emails carry datetime; payout is a date.
        email_date = utc_time.date() if hasattr(utc_time, "date") else utc_time
        if email_date < loan["payout_date"]:
            # This email pre-dates the payout — keep scanning for the next
            # one for this customer (don't add to seen_keys yet).
            skipped_before_payout += 1
            continue
        seen_keys.add(key)

        days_after = (email_date - loan["payout_date"]).days
        name_parts = list(loan["name_parts"]) + _split_external_name(ext_name or "")

        first_contacts.append({
            "received_utc":      utc_time.isoformat() if utc_time else None,
            "received_date":     email_date.isoformat(),
            "days_after_payout": days_after,
            "role":              loan["role"],            # "BRW" | "GT"
            "lender_name":       loan["lender_name"],
            "subject":           _redact_text(subject or "", name_parts)[:200],
            "body":              _redact_text(body    or "", name_parts)[:MAX_BODY_CHARS],
        })
    conn.close()
    print(
        f"# inbound-email pass — first contacts: {len(first_contacts)}, "
        f"skipped_no_loan: {skipped_no_loan}, "
        f"skipped_before_payout: {skipped_before_payout}",
        flush=True,
    )

    # Sort newest first, cap to MAX_ITEMS_OUT.
    first_contacts.sort(key=lambda r: r["received_utc"] or "", reverse=True)
    if len(first_contacts) > MAX_ITEMS_OUT:
        first_contacts = first_contacts[:MAX_ITEMS_OUT]

    # Lender + role breakdowns for the front-end filter pills.
    by_lender: dict[str, int] = {}
    by_role:   dict[str, int] = {"BRW": 0, "GT": 0}
    for fc in first_contacts:
        by_lender[fc["lender_name"]] = by_lender.get(fc["lender_name"], 0) + 1
        by_role[fc["role"]] = by_role.get(fc["role"], 0) + 1

    payload = {
        "schema_version":   1,
        "updated_at":       started.isoformat() + "Z",
        "snapshot_date":    started.date().isoformat(),
        "window_months":    MONTHS_BACK,
        "totals": {
            "items":     len(first_contacts),
            "by_lender": by_lender,
            "by_role":   by_role,
        },
        "items": first_contacts,
    }

    out_path = Path(os.environ.get("OUT", "first-contact.json")).resolve()
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(
        f"# wrote {out_path} — {len(first_contacts)} first-contact items",
        flush=True,
    )


def _write_empty(started: datetime.datetime) -> None:
    payload = {
        "schema_version": 1,
        "updated_at":     started.isoformat() + "Z",
        "snapshot_date":  started.date().isoformat(),
        "window_months":  MONTHS_BACK,
        "totals":         {"items": 0, "by_lender": {}, "by_role": {"BRW": 0, "GT": 0}},
        "items":          [],
    }
    out_path = Path(os.environ.get("OUT", "first-contact.json")).resolve()
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"# wrote empty {out_path} — no paid-out loans in window", flush=True)


if __name__ == "__main__":
    main()
