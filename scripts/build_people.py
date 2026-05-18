#!/usr/bin/env python3
"""
build_people.py — bootstrap people.json from staff.json + annotations.json.

people.json is the canonical Person table for TogetherBook. Every other
system (Wall, Holidays, Directory, Profile pages, future integrations)
should resolve identity through this file rather than through raw
Workspace records.

Strategy:
  1. Group active Google accounts by exact display name. Each cluster
     becomes one Person; the primary Google account is the highest-
     priority email (letme.co.uk > letme.com > togetherloans > other).
  2. Suspended / deleted accounts each become their own Person flagged
     access_level=former so we keep a record but mark them inactive.
  3. Folded-in fields from annotations.json: phone, address, line_manager,
     role, directory_photo_uploaded_at.
  4. payroll.start_date is left blank — the worker /api/workspace/payroll
     endpoint serves it live; we don't want stale copies sitting in git.

Re-running this script is idempotent in the sense that it overwrites
people.json; but any HAND edits to people.json (Auth0 IDs, external
Google accounts, aliases, manual access_level overrides) would be lost.
Once people.json is in active use, mutation happens via the worker; do
NOT re-run this script.
"""

from __future__ import annotations
import collections
import datetime as dt
import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

# Primary-email preference: lower number wins. Cluster picks the first.
TENANT_RANK = {
    "letme.co.uk": 0,
    "letme.com":   1,
    "togetherloans.com": 2,
    "togetherbook.net":  3,
    "rgroup.co.uk":      4,
}

ACCESS_LEVELS = ("admin", "staff", "outsider", "former")


def email_local(email: str) -> str:
    return (email or "").split("@", 1)[0].lower().strip()


def email_domain(email: str) -> str:
    parts = (email or "").split("@", 1)
    return parts[1].lower().strip() if len(parts) == 2 else ""


def tenant_score(email: str) -> int:
    return TENANT_RANK.get(email_domain(email), 99)


def slugify_for_id(name: str, fallback: str) -> str:
    """Build a stable Person id. We use the local-part of the primary email,
    which lines up with /directory/<slug> URLs already shipped."""
    return email_local(fallback) or re.sub(r"[^a-z0-9]+", ".", (name or "").lower()).strip(".")


def pick_primary(emails: list[str]) -> str:
    return sorted(emails, key=lambda e: (tenant_score(e), e.lower()))[0]


def build_people() -> dict:
    staff = json.loads((REPO / "staff.json").read_text())
    ann   = json.loads((REPO / "annotations.json").read_text()).get("annotations", {})

    # Cluster active users by display name. Anyone without a usable name
    # falls back to their own slug so they don't merge accidentally.
    active = [u for u in staff["users"] if not u.get("suspended") and not u.get("deletion_time")]
    inactive = [u for u in staff["users"] if u.get("suspended") or u.get("deletion_time")]

    clusters: dict[str, list[dict]] = collections.defaultdict(list)
    for u in active:
        key = (u.get("name") or "").strip().lower()
        if not key:
            key = "__noname__::" + (u.get("email") or "").lower()
        clusters[key].append(u)

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    people: list[dict] = []
    seen_ids: set[str] = set()

    def add_person(p: dict) -> None:
        base = p["id"]
        candidate = base
        i = 2
        while candidate in seen_ids:
            candidate = f"{base}-{i}"
            i += 1
        p["id"] = candidate
        seen_ids.add(candidate)
        people.append(p)

    # ── Active people ──────────────────────────────────────────────────
    for _, members in clusters.items():
        emails = [m.get("email", "") for m in members if m.get("email")]
        if not emails:
            continue
        primary = pick_primary(emails)
        alt = [e for e in emails if e != primary]
        # Use the active member with the most metadata as the display source.
        # When several share a name, prefer the letme.co.uk account.
        members_sorted = sorted(members, key=lambda u: tenant_score(u.get("email", "")))
        head = members_sorted[0]

        # Aggregate annotation fields across all emails in the cluster — the
        # first non-empty value wins. Order matches members_sorted so the
        # primary tenant's annotation takes precedence.
        def first(field: str) -> str:
            for u in members_sorted:
                v = (ann.get((u.get("email") or "").lower(), {}) or {}).get(field)
                if v: return v
            return ""

        photo_stamp = first("directory_photo_uploaded_at")
        admin_flag = any(u.get("admin") for u in members_sorted)
        access = "admin" if admin_flag else "staff"

        person = {
            "id": slugify_for_id(head.get("name", ""), primary),
            "name": head.get("name", "").strip(),
            "given": head.get("given", "").strip(),
            "family": head.get("family", "").strip(),
            "aliases": [],
            "main_google_email": primary,
            "alt_google_emails": alt,
            "external_google_email": "",
            "auth0_id": "",
            "access_level": access,
            "company": email_domain(primary),
            "title": head.get("title", "") or "",
            "department": head.get("department", "") or "",
            "phone": first("phone"),
            "address": first("address"),
            "start_date": "",
            "line_manager_id": "",
            "line_manager_email_raw": first("line_manager"),
            "role": first("role"),
            "directory_photo_uploaded_at": photo_stamp,
            "notes": "",
            "created_at": now,
            "updated_at": now,
        }
        add_person(person)

    # ── Inactive (suspended / deleted) people — each as their own record
    # so the history is preserved. ────────────────────────────────────────
    for u in inactive:
        email = u.get("email", "")
        if not email: continue
        person = {
            "id": slugify_for_id(u.get("name", ""), email),
            "name": u.get("name", "").strip(),
            "given": u.get("given", "").strip(),
            "family": u.get("family", "").strip(),
            "aliases": [],
            "main_google_email": email,
            "alt_google_emails": [],
            "external_google_email": "",
            "auth0_id": "",
            "access_level": "former",
            "company": email_domain(email),
            "title": u.get("title", "") or "",
            "department": u.get("department", "") or "",
            "phone": (ann.get(email.lower(), {}) or {}).get("phone", ""),
            "address": (ann.get(email.lower(), {}) or {}).get("address", ""),
            "start_date": "",
            "line_manager_id": "",
            "line_manager_email_raw": (ann.get(email.lower(), {}) or {}).get("line_manager", ""),
            "role": (ann.get(email.lower(), {}) or {}).get("role", ""),
            "directory_photo_uploaded_at": (ann.get(email.lower(), {}) or {}).get("directory_photo_uploaded_at", ""),
            "notes": "Suspended on Google Workspace at the time of bootstrap.",
            "suspended": True,
            "deletion_time": u.get("deletion_time", "") or "",
            "created_at": now,
            "updated_at": now,
        }
        add_person(person)

    # Resolve line_manager_email_raw → line_manager_id wherever we can.
    email_to_id: dict[str, str] = {}
    for p in people:
        for e in [p["main_google_email"]] + p["alt_google_emails"] + ([p.get("external_google_email")] if p.get("external_google_email") else []):
            if e: email_to_id[e.lower()] = p["id"]
    for p in people:
        raw = (p.get("line_manager_email_raw") or "").lower().strip()
        if raw and raw in email_to_id:
            p["line_manager_id"] = email_to_id[raw]

    return {
        "schema_version": 1,
        "updated_at": now,
        "source": "build_people.py bootstrap from staff.json + annotations.json",
        "people": sorted(people, key=lambda p: ((p.get("name") or "").lower(), p["id"])),
    }


def main() -> int:
    out = build_people()
    target = REPO / "people.json"
    # Pretty-print so the file diffs readably in git.
    target.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {target} — {len(out['people'])} people.")
    # Quick stat breakdown.
    by_access = collections.Counter(p["access_level"] for p in out["people"])
    print("  by access_level:", dict(by_access))
    multi = [p for p in out["people"] if p["alt_google_emails"]]
    print(f"  with alt Google accounts: {len(multi)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
