#!/usr/bin/env python3
"""
build_google_accounts.py — bootstrap google-accounts.json from
people.json (the email fields) + staff.json (rich per-account state).

Schema produced:
  {
    schema_version: 1,
    updated_at: ISO,
    records: [
      {
        id,              # integer PK
        person_id,       # FK -> people.id  (null if orphan)
        email,           # canonical login address
        tenant,          # "letme" | "together" | "external"
        is_primary,      # bool — true for Person.main_google_email
        google_user_id,  # Google's internal user ID (when present)
        name,            # Google display name from staff.json
        photo_url,       # Google thumbnail URL
        suspended,       # bool
        deletion_time,   # ISO string or ""
        last_login,      # ISO string or ""
        aliases,         # array of alias addresses
        synced_at        # ISO — last time we mirrored from staff.json
      }
    ]
  }

Two kinds of source for a row:
  - "letme" / "together" tenants come from staff.json — fully sync'd
  - "external" tenant comes from Person.external_google_email — admin-set,
    no Workspace mirror

USAGE
  python3 scripts/build_google_accounts.py            # dry-run
  python3 scripts/build_google_accounts.py --apply
  python3 scripts/build_google_accounts.py --apply --commit
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib, subprocess

REPO     = pathlib.Path(__file__).resolve().parent.parent
PEOPLE   = REPO / "people.json"
STAFF    = REPO / "staff.json"
OUT      = REPO / "google-accounts.json"


def tenant_of(email: str) -> str:
    dom = (email or "").split("@", 1)[-1].lower()
    if dom in ("letme.co.uk", "letme.com"): return "letme"
    if dom == "togetherloans.com":          return "together"
    return "external"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply",  action="store_true")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    if args.commit: args.apply = True

    ppl = json.loads(PEOPLE.read_text())["people"]
    staff = {u["email"].lower(): u for u in json.loads(STAFF.read_text())["users"]}

    now = now_iso()
    records = []
    next_id = 1
    by_email = {}  # email -> existing record (so we don't double-add)

    # First pass: every Person's emails become rows.
    for p in ppl:
        emails = []
        if p.get("main_google_email"):
            emails.append((p["main_google_email"].lower(), True))   # primary
        for e in (p.get("alt_google_emails") or []):
            if e: emails.append((e.lower(), False))
        if p.get("external_google_email"):
            emails.append((p["external_google_email"].lower(), False))

        for email, is_primary in emails:
            if email in by_email:
                # Same email on two Persons — shouldn't happen, prefer first.
                continue
            u = staff.get(email, {})
            tenant = tenant_of(email)
            rec = {
                "id":             next_id,
                "person_id":      p["id"],
                "email":          email,
                "tenant":         tenant,
                "is_primary":     is_primary,
                "google_user_id": u.get("id", "") or "",
                "name":           u.get("name", "") or "",
                "photo_url":      u.get("photo_url", "") or "",
                "suspended":      bool(u.get("suspended")) if u else False,
                "deletion_time":  u.get("deletion_time", "") or "",
                "last_login":     u.get("last_login", "") or "",
                "aliases":        list(u.get("aliases") or []),
                "synced_at":      now if u else "",
            }
            records.append(rec)
            by_email[email] = rec
            next_id += 1

    linked = sum(1 for r in records if r["person_id"] is not None)
    by_tenant = {}
    for r in records:
        by_tenant[r["tenant"]] = by_tenant.get(r["tenant"], 0) + 1
    print(f"google-accounts: {len(records)} record(s)")
    print(f"  linked to Persons: {linked}")
    print(f"  by tenant: {by_tenant}")

    # Persons in staff.json with no link to any Person — these are
    # orphans (would show in the Reconcile page). We don't add them
    # here; the Reconcile flow's "Link to person" creates the row.

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to write.\n")
        return

    out = {"schema_version": 1, "updated_at": now, "records": records}
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"\n✓ Wrote {OUT.relative_to(REPO)}")

    if args.commit:
        msg = (f"Schema: introduce google-accounts.json ({len(records)} records)\n\n"
               "One row per Google account, FK person_id -> people.id, "
               "tenant ∈ {letme, together, external}. Mirrors live data "
               "from staff.json for letme/together; external is admin-set. "
               "people.json email fields kept as denormalised pointers "
               "for the transition; google-accounts becomes authoritative.")
        subprocess.run(["git", "add", "google-accounts.json"], cwd=REPO, check=True)
        subprocess.run(["git", "commit", "-m", msg], cwd=REPO, check=True)
        subprocess.run(["git", "pull", "--rebase", "--quiet"], cwd=REPO, check=True)
        subprocess.run(["git", "push"], cwd=REPO, check=True)
        print("✓ Committed + pushed.\n")


if __name__ == "__main__":
    main()
