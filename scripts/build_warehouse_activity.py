#!/usr/bin/env python3
"""
build_warehouse_activity.py — produce warehouse-activity.json from
staff-activity.json with integer PKs + person_id FK.

Schema produced:
  {
    schema_version: 1,
    updated_at: ISO,
    records: [
      {
        id,                # integer PK
        person_id,         # FK -> people.id (null when no Person matches)
        email,             # if the source row carried one
        username,          # CRM username for external_users
        writes_60d,
        last_active_utc,
        primary_tenant,
        top_warehouse,
        source             # "active_user" | "external_user"
      }
    ]
  }

Matching strategy:
  - active_users come with an email → look up in google-accounts.json
    (which is keyed by email) to find person_id
  - external_users have a username → match by normalised name against
    Person.name / aliases

Re-run after every staff-activity.json refresh + people.json change.
Idempotent: outputs a fresh file each time.

USAGE
  python3 scripts/build_warehouse_activity.py            # dry-run
  python3 scripts/build_warehouse_activity.py --apply
  python3 scripts/build_warehouse_activity.py --apply --commit
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib, re, subprocess

REPO     = pathlib.Path(__file__).resolve().parent.parent
ACTIVITY = REPO / "staff-activity.json"
PEOPLE   = REPO / "people.json"
GOOGLE   = REPO / "google-accounts.json"
OUT      = REPO / "warehouse-activity.json"


def norm(s: str) -> str:
    if not s: return ""
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply",  action="store_true")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    if args.commit: args.apply = True

    if not ACTIVITY.exists():
        print(f"!! {ACTIVITY} missing — nothing to do.")
        return
    act = json.loads(ACTIVITY.read_text())
    ppl = json.loads(PEOPLE.read_text())["people"]
    google = json.loads(GOOGLE.read_text())["records"] if GOOGLE.exists() else []

    email_to_person = {g["email"].lower(): g["person_id"] for g in google if g.get("person_id") is not None}
    name_to_person  = {}
    for p in ppl:
        keys = set()
        if p.get("name"):    keys.add(norm(p["name"]))
        if p.get("given") and p.get("family"):
            keys.add(norm(f"{p['given']} {p['family']}"))
        for a in (p.get("aliases") or []):
            keys.add(norm(a))
        for k in keys:
            if not k: continue
            name_to_person.setdefault(k, p["id"])  # first wins

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    records = []
    next_id = 1
    matched_email = matched_name = unmatched = 0

    for a in (act.get("active_users") or []):
        email = (a.get("email") or "").lower()
        pid = email_to_person.get(email)
        if pid is not None: matched_email += 1
        else:               unmatched += 1
        records.append({
            "id":              next_id,
            "person_id":       pid,
            "email":           email,
            "username":        "",
            "writes_60d":      a.get("writes_60d", 0),
            "last_active_utc": a.get("last_active_utc", "") or "",
            "primary_tenant":  a.get("primary_tenant", "") or "",
            "top_warehouse":   a.get("top_warehouse", "") or "",
            "source":          "active_user",
        })
        next_id += 1

    for e in (act.get("external_users") or []):
        uname = (e.get("username") or "").lower()
        # First try matching the username against any name key.
        local = uname.split("@")[0]
        key1 = norm(local.replace(".", " "))
        key2 = norm(local)
        pid = name_to_person.get(key1) or name_to_person.get(key2)
        if pid is not None: matched_name += 1
        else:               unmatched += 1
        records.append({
            "id":              next_id,
            "person_id":       pid,
            "email":           e.get("email") or "",
            "username":        e.get("username") or "",
            "writes_60d":      e.get("writes_60d", 0),
            "last_active_utc": e.get("last_active_utc", "") or "",
            "primary_tenant":  e.get("primary_tenant", "") or "",
            "top_warehouse":   e.get("top_warehouse", "") or "",
            "source":          "external_user",
        })
        next_id += 1

    print(f"warehouse-activity: {len(records)} record(s)")
    print(f"  matched by email: {matched_email}")
    print(f"  matched by name:  {matched_name}")
    print(f"  unmatched:        {unmatched}")

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to write.\n")
        return

    out = {"schema_version": 1, "updated_at": now, "records": records}
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"\n✓ Wrote {OUT.relative_to(REPO)}")

    if args.commit:
        msg = (f"Schema: introduce warehouse-activity.json ({len(records)} records)\n\n"
               "Same shape as staff-activity.json but normalised — integer "
               "PK + person_id FK populated via email lookup (google-accounts.json) "
               "for active_users and name match for external_users.\n\n"
               "staff-activity.json continues to be the raw refresh target; "
               "warehouse-activity.json is the joined view consumers should "
               "read from going forward.")
        subprocess.run(["git", "add", "warehouse-activity.json"], cwd=REPO, check=True)
        subprocess.run(["git", "commit", "-m", msg], cwd=REPO, check=True)
        subprocess.run(["git", "pull", "--rebase", "--quiet"], cwd=REPO, check=True)
        subprocess.run(["git", "push"], cwd=REPO, check=True)
        print("✓ Committed + pushed.\n")


if __name__ == "__main__":
    main()
