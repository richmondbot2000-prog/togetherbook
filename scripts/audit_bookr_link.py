#!/usr/bin/env python3
"""Audit BookR <-> Directory linkage. Reports two lists:

  A. Live Directory Persons that don't match ANY BookR user (incl. fuzzy
     name match).
  B. BookR users with at least one future booking who don't match any
     live Directory Person.

Same scoring as the worker:
  100 = exact email,  80 = same email local-part across domains,
   60 = normalised full-name exact, 40 = given+family both in name.
"Matched" for this audit = score >= 40 (we surface anything plausible).

Read-only; never mutates Firebase or people.json. Run from CI via
.github/workflows/audit-bookr-link.yml.
"""

from __future__ import annotations
import json, os, sys, time, base64, datetime as _dt, urllib.request, urllib.parse, re
import pathlib

REPO_ROOT = pathlib.Path(os.environ.get("GITHUB_WORKSPACE") or "/Users/richmondrobot/Desktop/togetherbook")
PEOPLE_JSON_PATH = REPO_ROOT / "people.json"
BOOKR_DB_URL = "https://rg-bookr.firebaseio.com"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def get_bookr_access_token() -> str:
    raw = os.environ.get("BOOKR_SERVICE_ACCOUNT_JSON") or ""
    if not raw:
        raise SystemExit("BOOKR_SERVICE_ACCOUNT_JSON env var is required")
    sa = json.loads(raw)
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": sa.get("private_key_id")}
    claims = {
        "iss": sa["client_email"],
        "aud": "https://oauth2.googleapis.com/token",
        "scope": "https://www.googleapis.com/auth/firebase.database "
                 "https://www.googleapis.com/auth/userinfo.email",
        "iat": now, "exp": now + 3600,
    }
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    c = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{h}.{c}"
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    sig = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    jwt = f"{signing_input}.{_b64url(sig)}"
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt,
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())["access_token"]


def bookr_fetch(token: str, path: str):
    sep = "&" if "?" in path else "?"
    url = f"{BOOKR_DB_URL}{path}{sep}access_token={urllib.parse.quote(token)}"
    with urllib.request.urlopen(urllib.request.Request(url)) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def candidate_emails(p: dict) -> list:
    out = [p.get("main_google_email"), p.get("external_google_email"), *(p.get("alt_google_emails") or [])]
    return [(e or "").strip().lower() for e in out if e]


def norm_name(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def email_local(e: str) -> str:
    e = (e or "").strip().lower()
    at = e.find("@")
    return e[:at] if at > 0 else ""


def score_match(person: dict, bookr: dict) -> int:
    b_email = (bookr.get("email") or "").strip().lower()
    b_name  = norm_name(bookr.get("name"))
    p_emails = candidate_emails(person)
    if b_email and b_email in p_emails:
        return 100
    b_local = email_local(b_email)
    if b_local and any(email_local(e) == b_local for e in p_emails):
        return 80
    p_name = norm_name(person.get("name"))
    p_given = norm_name(person.get("given") or "")
    p_fam   = norm_name(person.get("family") or "")
    if p_name and b_name and p_name == b_name:
        return 60
    if p_given and p_fam and p_given in b_name and p_fam in b_name:
        return 40
    return 0


def is_live_person(p: dict) -> bool:
    if p.get("suspended"): return False
    if p.get("deletion_time"): return False
    if (p.get("access_level") or "") == "former": return False
    return True


def main() -> int:
    token = get_bookr_access_token()
    print(f"# fetching BookR /users + /cars + /properties ...", flush=True)
    bookr_users = bookr_fetch(token, "/users.json") or {}
    cars = bookr_fetch(token, "/cars.json") or {}
    props = bookr_fetch(token, "/properties.json") or {}
    print(f"# loaded {len(bookr_users)} BookR users, {len(cars)} cars, {len(props)} properties")

    today_iso = _dt.date.today().isoformat()
    # Map uid -> set of future-booked YYYY-MM-DD (any kind).
    future_dates: dict[str, set] = {}
    def collect(kind: str, branch: dict):
        for asset_id, asset in (branch or {}).items():
            bookings = ((asset or {}).get("bookings") or {})
            for date, value in bookings.items():
                if not isinstance(date, str) or len(date) < 10: continue
                if date < today_iso: continue
                if not value or value == "free": continue
                future_dates.setdefault(value, set()).add(f"{date} {kind} {asset_id}")
    collect("cars", cars)
    collect("properties", props)

    file = json.loads(PEOPLE_JSON_PATH.read_text())
    people = [p for p in (file.get("people") or []) if is_live_person(p)]
    print(f"# {len(people)} live Persons in directory")

    # Build a per-person best-score against every BookR user, and a
    # reverse map (per BookR uid -> best matched person).
    person_best: dict[int, tuple] = {}      # person_id -> (uid, score)
    uid_to_persons: dict[str, list] = {}    # uid -> [(person_id, score)]
    for p in people:
        best = (None, 0)
        for uid, bookr in bookr_users.items():
            sc = score_match(p, bookr or {})
            if sc > best[1]:
                best = (uid, sc)
            if sc >= 40:
                uid_to_persons.setdefault(uid, []).append((p["id"], sc))
        person_best[p["id"]] = best

    THRESHOLD = 40

    # ── List A — Directory people with NO BookR match (score < threshold) ──
    unmatched_people = []
    for p in people:
        uid, sc = person_best.get(p["id"], (None, 0))
        if sc < THRESHOLD:
            unmatched_people.append({
                "id": p["id"],
                "name": p.get("name") or "(no name)",
                "main_email": p.get("main_google_email") or "",
                "alt_emails": p.get("alt_google_emails") or [],
                "external_email": p.get("external_google_email") or "",
                "bookr_uid": p.get("bookr_uid") or "",
                "best_score": sc,
                "best_uid": uid or "",
            })
    unmatched_people.sort(key=lambda r: (r["name"] or "").lower())

    # ── List B — BookR users with future bookings but no Person match ──
    orphaned_bookr = []
    for uid, dates in future_dates.items():
        if uid in uid_to_persons:
            continue
        u = bookr_users.get(uid) or {}
        orphaned_bookr.append({
            "uid": uid,
            "email": u.get("email") or "",
            "name": u.get("name") or "",
            "suspended": bool(u.get("suspended")),
            "future_bookings": len(dates),
            "earliest": sorted(dates)[0] if dates else "",
        })
    orphaned_bookr.sort(key=lambda r: (-r["future_bookings"], (r["name"] or r["email"] or r["uid"]).lower()))

    print()
    print(f"==== A. {len(unmatched_people)} live Directory Persons with NO BookR match ====")
    if not unmatched_people:
        print("  (none)")
    else:
        print(f"  {'name':<28} {'main_email':<36} {'best':<5} {'best_uid'}")
        for r in unmatched_people:
            print(f"  {r['name'][:28]:<28} {r['main_email'][:36]:<36} {r['best_score']:<5} {r['best_uid']}")
    print()
    print(f"==== B. {len(orphaned_bookr)} BookR users with future bookings + no Directory Person ====")
    if not orphaned_bookr:
        print("  (none)")
    else:
        print(f"  {'name':<28} {'email':<36} {'future':<7} {'uid'}")
        for r in orphaned_bookr:
            print(f"  {r['name'][:28]:<28} {r['email'][:36]:<36} {r['future_bookings']:<7} {r['uid']}")
    print()
    # ── List C — full pairing matrix (every BookR <-> Person candidate)
    people_by_id = {p["id"]: p for p in people}
    pair_rows = []
    for uid, candidates in uid_to_persons.items():
        bu = bookr_users.get(uid) or {}
        for (pid, sc) in sorted(candidates, key=lambda x: -x[1]):
            pers = people_by_id.get(pid) or {}
            pair_rows.append({
                "score": sc,
                "bookr_name": bu.get("name") or "",
                "bookr_email": bu.get("email") or "",
                "bookr_uid": uid,
                "person_id": pid,
                "person_name": pers.get("name") or "",
                "person_email": pers.get("main_google_email") or "",
                "currently_linked": (pers.get("bookr_uid") or "") == uid,
            })
    def _sk(r):
        bucket = 0 if 40 <= r["score"] < 80 else (1 if r["score"] >= 80 else 2)
        return (bucket, -r["score"], (r["bookr_name"] or r["bookr_email"]).lower())
    pair_rows.sort(key=_sk)
    print(f"==== C. {len(pair_rows)} BookR<->Person candidate pairs (all score>=40) ====")
    print(f"  {'sc':<3} {'lnk':<4} {'BookR name':<24} {'BookR email':<32} {'->':<3} {'Person':<24} Person email")
    for r in pair_rows:
        link = "yes" if r["currently_linked"] else "no"
        print(f"  {r['score']:<3} {link:<4} {r['bookr_name'][:24]:<24} {r['bookr_email'][:32]:<32} -> {r['person_name'][:24]:<24} {r['person_email']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
