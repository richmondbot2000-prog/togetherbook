#!/usr/bin/env python3
"""
import_payroll.py — manual terminal import of payroll spreadsheets into
the canonical PayrollData table (payroll-data.json) + relinks each
matched Person via most_recent_payroll_id.

USAGE

    # dry-run (default): prints match report, writes nothing
    python3 scripts/import_payroll.py <csv> [<csv> …]

    # apply: writes payroll-data.json + people.json
    python3 scripts/import_payroll.py <csv> [<csv> …] --apply

    # apply and commit/push in one step
    python3 scripts/import_payroll.py <csv> [<csv> …] --apply --commit

Per-row behaviour
- Email match wins. Falls back to normalised first+last name match.
- Matched row → new PayrollData record + link via most_recent_payroll_id
  + on_payroll = true on the Person.
- Unmatched row → printed to stderr; no file change for it. Run again
  after manually adding the Person.

Idempotency
- Re-running the same spreadsheet creates fresh records. That's
  intentional: each spreadsheet is a "snapshot in time". Old records
  stay in payroll-data.json for audit; most_recent_payroll_id points
  to the newest.

Date handling
- The big "EmployeeDetails" export uses American M/D/Y. The shorter
  "LetMe Property Management" export uses "DD MMM YYYY". Both are
  normalised to ISO YYYY-MM-DD.

Employer
- Inferred from the source file name unless --employer is passed.
"""

from __future__ import annotations
import argparse
import csv
import datetime as dt
import json
import pathlib
import random
import re
import string
import subprocess
import sys
import unicodedata
from typing import Optional

REPO       = pathlib.Path(__file__).resolve().parent.parent
PEOPLE     = REPO / "people.json"
PAYROLL    = REPO / "payroll-data.json"


# ─── Name normalisation ──────────────────────────────────────────────
def norm_name(s: str) -> str:
    if not s: return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ─── Date parsers ────────────────────────────────────────────────────
def parse_us_slash_date(s: str) -> Optional[str]:
    s = (s or "").strip()
    if not s: return None
    try:
        d = dt.datetime.strptime(s, "%m/%d/%Y").date()
        return d.isoformat()
    except ValueError:
        return None


def parse_uk_text_date(s: str) -> Optional[str]:
    s = (s or "").strip()
    if not s: return None
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ─── Per-file row extractors ─────────────────────────────────────────
def detect_format(path: pathlib.Path) -> str:
    """Return 'big' for the EmployeeDetails M/D/Y CSV, 'letme_prop' for
    the small LetMe Property Management one. Detection is done from the
    first non-blank line of the file."""
    with path.open() as f:
        head = f.read(300)
    if "Employee Contact Details" in head:
        return "letme_prop"
    if head.startswith("First name,Surname,External Id"):
        return "big"
    raise SystemExit(f"don't know how to read {path.name}: head was {head[:80]!r}")


def rows_big(path: pathlib.Path):
    with path.open(newline="") as f:
        for r in csv.DictReader(f):
            first = (r.get("First name")  or "").strip()
            last  = (r.get("Surname")     or "").strip()
            if not first and not last: continue
            addr_parts = [r.get("Postal address line 1"),
                          r.get("Postal address line 2"),
                          r.get("Postal City"),
                          r.get("Postal County"),
                          r.get("Postal postcode")]
            addr = "\n".join(p.strip() for p in addr_parts if (p or "").strip())
            yield {
                "first_name":   first,
                "last_name":    last,
                "email":        "",
                "employee_number": (r.get("External Id") or "").strip(),
                "mobile":       re.sub(r"\s+", "", (r.get("Mobile") or "")) or "",
                "address":      addr,
                "start_date":   parse_us_slash_date(r.get("Start date") or "") or "",
                "date_of_birth": parse_us_slash_date(r.get("Date of birth") or "") or "",
                "termination_date": "",
            }


def rows_letme_prop(path: pathlib.Path):
    # First 4 lines are a banner; row 5 (0-indexed 4) is the header.
    with path.open(newline="") as f:
        lines = f.readlines()
    # Find header line.
    header_idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("Employee Number,First Name,Last Name"):
            header_idx = i; break
    if header_idx is None:
        raise SystemExit(f"{path.name}: couldn't find header row")
    reader = csv.DictReader(lines[header_idx:])
    for r in reader:
        first = (r.get("First Name") or "").strip()
        last  = (r.get("Last Name") or "").strip()
        if not first and not last: continue
        yield {
            "first_name":   first,
            "last_name":    last,
            "email":        (r.get("Email") or "").strip().lower(),
            "employee_number": (r.get("Employee Number") or "").strip(),
            "mobile":       "",
            "address":      (r.get("Address") or "").strip(),
            "start_date":   parse_uk_text_date(r.get("Start Date") or "") or "",
            "date_of_birth": parse_uk_text_date(r.get("Date of Birth") or "") or "",
            "termination_date": parse_uk_text_date(r.get("Termination Date") or "") or "",
        }


def employer_from_path(path: pathlib.Path) -> str:
    n = path.name.lower()
    if "letme_property" in n or "property_management" in n:
        return "LetMe Property Management Limited"
    if "employeedetails" in n:
        return "LetMe Ltd"
    return path.stem.split(".")[0]


def read_rows(path: pathlib.Path):
    fmt = detect_format(path)
    emp = employer_from_path(path)
    extractor = {"big": rows_big, "letme_prop": rows_letme_prop}[fmt]
    for r in extractor(path):
        r["employer"] = emp
        yield r


# ─── Matching ────────────────────────────────────────────────────────
def build_indexes(people):
    by_email = {}
    by_name  = {}
    for p in people:
        for e in [p.get("main_google_email"), *(p.get("alt_google_emails") or []), p.get("external_google_email")]:
            if e: by_email[e.lower()] = p
        # Name keys: "first last" + "first last_token_last" (handles
        # hyphenated surnames). Also include aliases.
        keys = set()
        first  = (p.get("given") or "").strip()
        last   = (p.get("family") or "").strip()
        name   = (p.get("name") or "").strip()
        if first and last:
            keys.add(norm_name(f"{first} {last}"))
            fam_toks = re.split(r"[-\s]+", last)
            if len(fam_toks) > 1:
                keys.add(norm_name(f"{first} {fam_toks[-1]}"))
        if name and name != f"{first} {last}":
            parts = name.split()
            if len(parts) >= 2:
                keys.add(norm_name(f"{parts[0]} {parts[-1]}"))
            keys.add(norm_name(name))
        for a in (p.get("aliases") or []):
            keys.add(norm_name(a))
        for k in keys:
            if not k: continue
            by_name.setdefault(k, []).append(p)
    return by_email, by_name


def match_row(row, by_email, by_name):
    if row["email"]:
        hit = by_email.get(row["email"])
        if hit: return ("email", hit)
    key = norm_name(f"{row['first_name']} {row['last_name']}")
    candidates = by_name.get(key, [])
    if len(candidates) == 1: return ("name", candidates[0])
    if len(candidates) > 1:  return ("ambiguous", candidates)
    return ("none", None)


# ─── PayrollData record build ────────────────────────────────────────
def new_payroll_id() -> str:
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"pay_{int(dt.datetime.now().timestamp())}{rand}"


def build_record(row, person, source: str, actor: str) -> dict:
    return {
        "id":              new_payroll_id(),
        "person_id":       person["id"],
        "employer":        row["employer"],
        "employee_number": row.get("employee_number") or "",
        "first_name":      row["first_name"],
        "last_name":       row["last_name"],
        "email":           row.get("email") or "",
        "start_date":      row.get("start_date") or "",
        "termination_date": row.get("termination_date") or "",
        "mobile":          row.get("mobile") or "",
        "address":         row.get("address") or "",
        "annual_salary":   None,
        "monthly_pay":     None,
        "tax_code":        "",
        "ni_number":       "",
        "bank_sort_code":  "",
        "bank_account_last4": "",
        "notes":           "",
        "date_of_birth":   row.get("date_of_birth") or "",
        "imported_at":     dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "imported_by":     actor,
        "source":          source,
    }


# ─── Main ────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="+", type=pathlib.Path, help="payroll CSV(s) to import")
    ap.add_argument("--apply",  action="store_true", help="actually write files (default: dry-run report only)")
    ap.add_argument("--commit", action="store_true", help="git commit + push after applying (implies --apply)")
    ap.add_argument("--actor", default="import_payroll.py terminal run", help="recorded in imported_by")
    ap.add_argument("--create-missing", action="store_true",
                    help="auto-create a Person record for any unmatched row (slug=first.last, no Google email — fill in later)")
    args = ap.parse_args()
    if args.commit: args.apply = True

    people_file  = json.loads(PEOPLE.read_text())
    payroll_file = json.loads(PAYROLL.read_text()) if PAYROLL.exists() else {"schema_version": 1, "updated_at": None, "records": []}
    if not isinstance(people_file.get("people"), list):
        raise SystemExit("people.json: missing or malformed 'people' list")
    if not isinstance(payroll_file.get("records"), list):
        payroll_file["records"] = []
    people = people_file["people"]
    people_by_id = {p["id"]: p for p in people}

    by_email, by_name = build_indexes(people)

    matched, ambiguous, unmatched = [], [], []
    for csv_path in args.csv:
        if not csv_path.exists():
            print(f"!! not found: {csv_path}", file=sys.stderr); continue
        source = f"manual:{csv_path.name}:{dt.date.today().isoformat()}"
        for row in read_rows(csv_path):
            kind, hit = match_row(row, by_email, by_name)
            if kind == "email" or kind == "name":
                matched.append((csv_path.name, row, hit, kind, source))
            elif kind == "ambiguous":
                ambiguous.append((csv_path.name, row, hit, source))
            else:
                unmatched.append((csv_path.name, row, source))

    # ── Report ──────────────────────────────────────────────────────
    print(f"\n=== Match report — {sum(1 for _ in args.csv)} file(s) ===\n")
    print(f"  matched:    {len(matched)}")
    print(f"  ambiguous:  {len(ambiguous)}")
    print(f"  unmatched:  {len(unmatched)}\n")
    if matched:
        print("Matched (will create + relink):")
        for fname, row, person, kind, _ in matched:
            print(f"  [{kind:5}]  {row['first_name']} {row['last_name']:<22}  ->  {person['id']:<24}  ({person.get('name','')})")
    if ambiguous:
        print("\nAmbiguous (skipped — pick one manually first):")
        for fname, row, hits, _ in ambiguous:
            ids = ", ".join(h["id"] for h in hits)
            print(f"  {row['first_name']} {row['last_name']}  ->  {ids}")
    if unmatched:
        print("\nUnmatched (skipped — create the Person first, or add an alias):")
        for fname, row, _ in unmatched:
            print(f"  {row['first_name']} {row['last_name']}   (file: {fname})")

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to write files.\n")
        return

    # ── Auto-create Persons for unmatched rows when --create-missing ─
    created_people = []
    if args.create_missing and unmatched:
        existing_ids = {p["id"] for p in people}
        now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for fname, row, source in list(unmatched):
            base_slug = norm_name(f"{row['first_name']} {row['last_name']}").replace(" ", ".")
            slug, n = base_slug, 2
            while slug in existing_ids:
                slug = f"{base_slug}-{n}"; n += 1
            new_person = {
                "id":              slug,
                "name":            f"{row['first_name']} {row['last_name']}".strip(),
                "given":           row["first_name"],
                "family":          row["last_name"],
                "aliases":         [],
                "main_google_email":     "",
                "alt_google_emails":     [],
                "external_google_email": row.get("email") or "",
                "auth0_id":              "",
                "access_level":          "staff",
                "company":               row.get("employer") or "",
                "title":                 "",
                "department":            "",
                "phone":                 row.get("mobile") or "",
                "address":               row.get("address") or "",
                "start_date":            row.get("start_date") or "",
                "line_manager_id":       "",
                "line_manager_email_raw": "",
                "role":                  "",
                "notes":                 "Person auto-created from payroll import — no Google account linked yet. Set main_google_email when known.",
                "directory_photo_uploaded_at": "",
                "cover_photo_uploaded_at":     "",
                "on_payroll":              True,
                "most_recent_payroll_id":  "",
                "suspended":               False,
                "created_at":              now_iso,
                "updated_at":              now_iso,
            }
            people.append(new_person)
            existing_ids.add(slug)
            created_people.append(new_person)
            # Promote into matched so the standard flow creates the PayrollData row + relinks.
            matched.append((fname, row, new_person, "auto-created", source))
            unmatched.remove((fname, row, source))
        print(f"\n✓ Auto-created {len(created_people)} Person record(s) for unmatched rows.")

    # ── Apply ───────────────────────────────────────────────────────
    new_records = []
    for fname, row, person, kind, source in matched:
        rec = build_record(row, person, source, args.actor)
        new_records.append(rec)
        # Relink the Person.
        person["most_recent_payroll_id"] = rec["id"]
        person["on_payroll"] = True
        person["updated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    payroll_file["records"].extend(new_records)
    payroll_file["updated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payroll_file["schema_version"] = 1

    PAYROLL.write_text(json.dumps(payroll_file, indent=2, ensure_ascii=False) + "\n")
    people_file["updated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    PEOPLE.write_text(json.dumps(people_file, indent=2, ensure_ascii=False) + "\n")

    print(f"\n✓ Wrote {len(new_records)} new PayrollData record(s)")
    print(f"✓ Relinked {len(new_records)} Person(s) via most_recent_payroll_id + on_payroll=true")

    if args.commit:
        files = ", ".join(p.name for p in args.csv)
        msg = (f"Payroll import: {len(new_records)} record(s) from {files}\n\n"
               f"Source: {files}\n"
               f"Matched: {len(matched)} (email + name)\n"
               f"Ambiguous: {len(ambiguous)}\n"
               f"Unmatched: {len(unmatched)}\n")
        subprocess.run(["git", "add", "people.json", "payroll-data.json"], cwd=REPO, check=True)
        subprocess.run(["git", "commit", "-m", msg], cwd=REPO, check=True)
        subprocess.run(["git", "pull", "--rebase", "--quiet"], cwd=REPO, check=True)
        subprocess.run(["git", "push"], cwd=REPO, check=True)
        print("✓ Committed + pushed.\n")


if __name__ == "__main__":
    main()
