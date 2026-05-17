#!/usr/bin/env python3
"""
backfill_names.py — fill in every empty Person.name from the best
available source. Resolution order, first hit wins:

  1. existing person.name (skip — only blanks are touched)
  2. given + family on the Person record (title-cased)
  3. staff.json (Gmail account display name) for any linked Google email
  4. payroll-data.json: first_name + last_name on the most_recent record
  5. external_google_email local-part → "First Last"
  6. url_slug → "First Last"

Usage:
  python3 scripts/backfill_names.py            # dry-run
  python3 scripts/backfill_names.py --apply    # write
  python3 scripts/backfill_names.py --apply --commit
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib, re, subprocess

REPO    = pathlib.Path(__file__).resolve().parent.parent
PEOPLE  = REPO / "people.json"
STAFF   = REPO / "staff.json"
PAYROLL = REPO / "payroll-data.json"


def smart_title(s: str) -> str:
    """Title-case respecting common name patterns: hyphens, apostrophes,
    "Mc", "O'", etc."""
    if not s: return s
    parts = re.split(r"([\s\-])", s.strip())
    out = []
    for p in parts:
        if p in (" ", "-"):
            out.append(p); continue
        if not p:
            continue
        low = p.lower()
        if low.startswith("mc") and len(low) > 2:
            out.append("Mc" + low[2].upper() + low[3:])
        elif low.startswith("o'") and len(low) > 2:
            out.append("O'" + low[2].upper() + low[3:])
        else:
            out.append(low[0].upper() + low[1:])
    return "".join(out)


def derive_name(p, staff_by_email, payroll_by_id) -> tuple[str, str]:
    # (name, source) — source recorded for logging only.
    if (p.get("name") or "").strip():
        return p["name"], "existing"

    given  = (p.get("given")  or "").strip()
    family = (p.get("family") or "").strip()
    if given or family:
        return smart_title(f"{given} {family}").strip(), "given+family"

    for e in [p.get("main_google_email"), *(p.get("alt_google_emails") or []), p.get("external_google_email")]:
        if not e: continue
        u = staff_by_email.get(e.lower())
        if u and (u.get("name") or "").strip():
            return u["name"].strip(), f"google:{e}"

    rec = payroll_by_id.get(p.get("most_recent_payroll_id"))
    if rec and ((rec.get("first_name") or "").strip() or (rec.get("last_name") or "").strip()):
        return smart_title(f"{rec.get('first_name','')} {rec.get('last_name','')}").strip(), "payroll"

    for e in [p.get("external_google_email"), p.get("main_google_email")]:
        if e:
            local = e.split("@")[0]
            return smart_title(local.replace(".", " ").replace("_", " ")), f"email-local:{e}"

    slug = p.get("url_slug") or ""
    if slug:
        return smart_title(slug.replace(".", " ").replace("-", " ")), "url_slug"

    return f"Person #{p.get('id')}", "fallback"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply",  action="store_true")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    if args.commit: args.apply = True

    pf = json.loads(PEOPLE.read_text())
    staff_by_email = {u["email"].lower(): u for u in json.loads(STAFF.read_text())["users"]}
    payroll_by_id  = {r["id"]: r for r in json.loads(PAYROLL.read_text())["records"]}

    changed = []
    for p in pf["people"]:
        if (p.get("name") or "").strip():
            continue
        new_name, source = derive_name(p, staff_by_email, payroll_by_id)
        if not new_name: continue
        changed.append((p, new_name, source))

    print(f"Persons to backfill: {len(changed)}\n")
    for p, new_name, source in changed:
        print(f"  id={p['id']:>3} url_slug={p.get('url_slug',''):<30} -> {new_name!r:<32} ({source})")

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to write.\n")
        return

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for p, new_name, _ in changed:
        p["name"] = new_name
        p["updated_at"] = now
    pf["updated_at"] = now
    PEOPLE.write_text(json.dumps(pf, indent=2, ensure_ascii=False) + "\n")
    print(f"\n✓ Wrote {len(changed)} name(s).")

    if args.commit:
        msg = (f"Backfill: {len(changed)} Person.name field(s) populated\n\n"
               "Resolution order: given+family > google.name > payroll first+last > "
               "email local-part > url_slug. Names are never empty after this.")
        subprocess.run(["git", "add", "people.json"], cwd=REPO, check=True)
        subprocess.run(["git", "commit", "-m", msg], cwd=REPO, check=True)
        subprocess.run(["git", "pull", "--rebase", "--quiet"], cwd=REPO, check=True)
        subprocess.run(["git", "push"], cwd=REPO, check=True)
        print("✓ Committed + pushed.\n")


if __name__ == "__main__":
    main()
