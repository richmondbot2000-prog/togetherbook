#!/usr/bin/env python3
"""
check_schema_integrity.py — fast cross-table FK + uniqueness checker.

Runs against people.json + payroll-data.json + google-accounts.json +
warehouse-activity.json + admins.json. Catches:
  - Person ids that aren't positive integers / aren't unique
  - url_slugs that aren't unique
  - empty names
  - line_manager_id / most_recent_payroll_id that point at nothing
  - PayrollData / GoogleAccount / Warehouse rows whose person_id
    points at a deleted Person
  - admins.json emails that don't map to an access_level=admin Person
  - Google account tenants outside {letme, together, external}

Designed to run unattended in a GitHub Actions workflow daily. Exits
non-zero if any failure found so the workflow fails loudly.

USAGE
  python3 scripts/check_schema_integrity.py
"""
from __future__ import annotations
import json, pathlib, sys

REPO = pathlib.Path(__file__).resolve().parent.parent
OWNER_EMAIL = "james.benamor@letme.com"


def load(name: str):
    p = REPO / name
    if not p.exists(): return None
    try: return json.loads(p.read_text())
    except Exception as e: print(f"!! could not parse {name}: {e}", file=sys.stderr); return None


def main() -> int:
    ppl_file = load("people.json")    or {"people": []}
    pay_file = load("payroll-data.json") or {"records": []}
    g_file   = load("google-accounts.json") or {"records": []}
    wh_file  = load("warehouse-activity.json") or {"records": []}
    adm_file = load("admins.json")    or {"admins": []}

    ppl = ppl_file.get("people", [])
    pay = pay_file.get("records", [])
    gacc = g_file.get("records", [])
    wh = wh_file.get("records", [])
    adm = adm_file.get("admins", [])

    failures = []
    warnings = []
    seen_ids = set(); seen_slugs = set()

    for p in ppl:
        pid = p.get("id")
        if not isinstance(pid, int) or pid <= 0:
            failures.append(f"Person has non-positive-int id: {pid!r} (name={p.get('name','?')!r})")
            continue
        if pid in seen_ids:
            failures.append(f"duplicate Person id {pid}")
        seen_ids.add(pid)
        slug = (p.get("url_slug") or "").lower().strip()
        if not slug:
            warnings.append(f"Person #{pid} missing url_slug")
        elif slug in seen_slugs:
            failures.append(f"duplicate url_slug {slug!r} on Person #{pid}")
        else:
            seen_slugs.add(slug)
        if not (p.get("name") or "").strip():
            failures.append(f"Person #{pid} has empty name")

    people_ids = seen_ids
    pay_ids = {r.get("id") for r in pay if isinstance(r.get("id"), int)}

    for p in ppl:
        pid = p.get("id")
        mrid = p.get("most_recent_payroll_id")
        if mrid is not None and mrid != "" and mrid not in pay_ids:
            failures.append(f"Person #{pid} most_recent_payroll_id={mrid} → no PayrollData record")
        lm = p.get("line_manager_id")
        if lm is not None and lm != "" and lm not in people_ids:
            failures.append(f"Person #{pid} line_manager_id={lm} → no Person")
        if lm == pid:
            failures.append(f"Person #{pid} ({p.get('name','?')}) is its own line_manager")

    for r in pay:
        rid = r.get("id"); pid = r.get("person_id")
        if pid is not None and pid not in people_ids:
            failures.append(f"PayrollData #{rid} person_id={pid} → no Person (orphan)")

    for g in gacc:
        rid = g.get("id"); pid = g.get("person_id")
        if pid is not None and pid not in people_ids:
            failures.append(f"GoogleAccount #{rid} ({g.get('email','?')}) person_id={pid} → no Person (orphan)")
        if g.get("tenant") not in ("letme", "together", "external"):
            failures.append(f"GoogleAccount #{rid} bad tenant={g.get('tenant')!r}")

    for w in wh:
        rid = w.get("id"); pid = w.get("person_id")
        if pid is not None and pid not in people_ids:
            failures.append(f"WarehouseActivity #{rid} person_id={pid} → no Person (orphan)")

    # admins.json should be the union of (every email on any access_level=admin
    # non-suspended Person) + the owner failsafe.
    expected_admins = {OWNER_EMAIL.lower()}
    for p in ppl:
        if p.get("access_level") != "admin": continue
        if p.get("suspended"): continue
        for e in [p.get("main_google_email"), *(p.get("alt_google_emails") or []), p.get("external_google_email")]:
            if e: expected_admins.add(e.lower())
    for e in adm:
        if (e or "").lower() not in expected_admins:
            warnings.append(f"admins.json contains {e!r} but people.json doesn't mark them admin (will be cleared on next sync)")
    missing_admins = expected_admins - {(e or "").lower() for e in adm}
    for e in missing_admins:
        if e != OWNER_EMAIL.lower():
            warnings.append(f"access_level=admin Person has email {e!r} but it's not in admins.json (will appear on next sync)")

    print(f"checked {len(ppl)} people · {len(pay)} payroll · {len(gacc)} google · {len(wh)} warehouse · {len(adm)} admins")
    print(f"failures: {len(failures)}")
    print(f"warnings: {len(warnings)}")
    for f in failures: print(f"  FAIL  {f}")
    for w in warnings: print(f"  warn  {w}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
