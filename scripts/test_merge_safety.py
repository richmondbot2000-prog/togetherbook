#!/usr/bin/env python3
"""Merge-safety test suite.

Goal: protect the site from a specific class of bug that bites whenever
two Workspace accounts are merged into a single TogetherBook Person.

After a merge the Person's active `main_google_email` often differs from
the email under which the original data was indexed: photos on disk are
keyed by upload email (e.g. `..._at_togetherloans.com.jpg`), annotations
+ holidays are dicts keyed by email, wall posts/reactions carry the
authoring email at write time. Any lookup that uses `main_google_email`
in isolation will 404 the photo, miss the annotation, drop the holiday
allowance, double-count the reactor, or silently render initials.

This suite is the safety net. It runs in CI on every push to main and
every PR. It performs four classes of check:

  1. Data integrity — Persons themselves are well-formed (no email in two
     Persons; unique id / url_slug / main_google_email).
  2. Cross-file reference integrity — every email referenced from
     annotations.json, holidays.json, wall.json resolves to a Person (or
     to staff.json, for raw Workspace records that haven't been adopted
     into a Person yet).
  3. Per-Person fragmentation report — surfaces Persons whose photo,
     cover, holidays, or wall posts live on an alt email rather than the
     main, so reviewers can spot which lookup paths *must* walk linked
     emails to be correct. Always emitted as warnings, never failures.
  4. Code lint — regex-scans the repo for the bug pattern: building a
     `/assets/photos/...jpg` or `/assets/covers/...jpg` URL from
     `main_google_email` (or any single email) without first walking all
     linked emails. Flags directly-vulnerable code.

Exit code 0 if every FAIL-class check passed; non-zero otherwise.
Warnings are informational and do not affect the exit code.

Run locally with `python3 scripts/test_merge_safety.py`.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent

SYSTEM_AUTHORS = {"togetherbook@system"}
# "Removed account" placeholder author seen in older wall posts.
TOMBSTONE_AUTHORS = {"removed@removed", "deleted@deleted"}


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def load_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"WARN: failed to parse {path}: {e}", file=sys.stderr)
        return None


def linked_emails(p: dict) -> List[str]:
    out: List[str] = []
    main = (p.get("main_google_email") or "").strip().lower()
    if main:
        out.append(main)
    for e in (p.get("alt_google_emails") or []):
        e = (e or "").strip().lower()
        if e and e not in out:
            out.append(e)
    ext = (p.get("external_google_email") or "").strip().lower()
    if ext and ext not in out:
        out.append(ext)
    return out


# ──────────────────────────────────────────────────────────────────────
# Result accumulator
# ──────────────────────────────────────────────────────────────────────

class Check:
    """One named check. .fail() entries break CI, .warn() entries are
    surfaced but don't fail the run. .info() is just a counted note."""

    def __init__(self, name: str, *, summary: str = ""):
        self.name = name
        self.summary = summary
        self.failures: List[str] = []
        self.warnings: List[str] = []
        self.info_lines: List[str] = []

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def info(self, msg: str) -> None:
        self.info_lines.append(msg)

    @property
    def passed(self) -> bool:
        return not self.failures

    def report(self) -> None:
        tag = "PASS" if self.passed else "FAIL"
        head = f"[{tag}] {self.name}"
        if self.summary:
            head += f"  ({self.summary})"
        print(head)
        for ln in self.failures:
            print(f"   FAIL  {ln}")
        for ln in self.warnings:
            print(f"   warn  {ln}")
        for ln in self.info_lines:
            print(f"   info  {ln}")


# ──────────────────────────────────────────────────────────────────────
# Check implementations
# ──────────────────────────────────────────────────────────────────────

def check_email_uniqueness(people: List[dict]) -> Check:
    """T1 — every email belongs to at most one Person."""
    c = Check("T1: every email belongs to at most one Person")
    seen: Dict[str, str] = {}
    for p in people:
        pid = p.get("id") or "<no-id>"
        for e in linked_emails(p):
            owner = seen.get(e)
            if owner and owner != pid:
                c.fail(f"email {e!r} belongs to both Person {owner} and {pid}")
            seen[e] = pid
    c.summary = f"{len(seen)} linked emails across {len(people)} Persons"
    return c


def check_person_uniqueness(people: List[dict]) -> Check:
    """T2 — Person id, url_slug, main_google_email each unique."""
    c = Check("T2: id / url_slug / main_google_email are unique per Person")
    for field, label in (("id", "id"), ("url_slug", "url_slug"), ("main_google_email", "main_google_email")):
        counts: Dict[str, int] = {}
        for p in people:
            v = p.get(field)
            if not v:
                continue
            v = v.lower() if field != "id" else v
            counts[v] = counts.get(v, 0) + 1
        dupes = {k: n for k, n in counts.items() if n > 1}
        for k, n in dupes.items():
            c.fail(f"{label} {k!r} appears {n}× in people.json")
    return c


def check_person_required_fields(people: List[dict]) -> Check:
    """T3 — every Person has the minimal fields the UI relies on. Some
    Persons are name-only stubs (e.g. family members in the org chart
    with no Workspace account) so we deliberately don't require any
    email field — those records still need id / name / url_slug so
    every UI surface that iterates people.json can render them."""
    c = Check("T3: every Person has id, name, url_slug")
    for p in people:
        pid = p.get("id") or "<no-id>"
        for field in ("id", "name", "url_slug"):
            if not p.get(field):
                c.fail(f"Person {pid}: missing {field}")
    return c


def check_annotations_resolve(annotations: dict, email_to_person: Dict[str, str], staff_emails: Set[str]) -> Check:
    """T4 — every annotation key is either a Person email or a known
    Workspace email. Pure orphans are warned about because they will
    silently fail to render."""
    c = Check("T4: annotations.json email keys resolve to a Person or staff.json")
    orphans = 0
    for email in annotations.keys():
        e = (email or "").strip().lower()
        if not e:
            continue
        if e not in email_to_person and e not in staff_emails:
            c.warn(f"orphan annotation {e!r} — not in any Person and not in staff.json")
            orphans += 1
    c.summary = f"{len(annotations)} annotation keys, {orphans} orphan(s)"
    return c


def check_holidays_resolve(holidays_doc: dict, email_to_person: Dict[str, str]) -> Check:
    """T5 — every holidays.by_user key resolves to a Person."""
    c = Check("T5: holidays.json by_user keys resolve to a Person")
    by_user = holidays_doc.get("by_user") or {}
    orphans = 0
    for email in by_user.keys():
        e = (email or "").strip().lower()
        if not e:
            continue
        if e not in email_to_person:
            c.warn(f"orphan holidays entry {e!r} — not in any Person")
            orphans += 1
    c.summary = f"{len(by_user)} holiday users, {orphans} orphan(s)"
    return c


def check_wall_resolve(wall_doc: dict, email_to_person: Dict[str, str]) -> Check:
    """T6 — wall.json post/comment authors + reaction emails resolve."""
    c = Check("T6: wall.json author_email + reaction emails resolve to a Person or system")
    orphans = 0

    def is_ok(email: str) -> bool:
        return email in email_to_person or email in SYSTEM_AUTHORS or email in TOMBSTONE_AUTHORS

    def walk_reactions(reactions: dict, ctx: str) -> None:
        nonlocal orphans
        if not isinstance(reactions, dict):
            return
        for emoji, emails in reactions.items():
            if not isinstance(emails, list):
                continue
            for raw in emails:
                e = (raw or "").strip().lower()
                if e and not is_ok(e):
                    c.warn(f"{ctx}: reaction {emoji} by orphan {e!r}")
                    orphans += 1

    posts = wall_doc.get("posts") or []
    for post in posts:
        pid = post.get("id") or "<no-id>"
        ae = (post.get("author_email") or "").strip().lower()
        if ae and not is_ok(ae):
            c.warn(f"post {pid}: author_email {ae!r} not in any Person")
            orphans += 1
        walk_reactions(post.get("reactions") or {}, f"post {pid}")
        for com in (post.get("comments") or []):
            cid = com.get("id") or "<no-id>"
            ce = (com.get("author_email") or "").strip().lower()
            if ce and not is_ok(ce):
                c.warn(f"post {pid} comment {cid}: author_email {ce!r} not in any Person")
                orphans += 1
            walk_reactions(com.get("reactions") or {}, f"post {pid} comment {cid}")

    c.summary = f"{len(posts)} posts checked, {orphans} orphan reference(s)"
    return c


def check_line_managers_resolve(annotations: dict, email_to_person: Dict[str, str]) -> Check:
    """T7 — manager pointers point at known Persons."""
    c = Check("T7: line_manager_email + manager_email annotations resolve to a Person")
    orphans = 0
    for email, ann in annotations.items():
        if not isinstance(ann, dict):
            continue
        for key in ("line_manager_email", "manager_email"):
            ref = (ann.get(key) or "").strip().lower()
            if ref and ref not in email_to_person:
                c.warn(f"annotation for {email!r}: {key}={ref!r} is not in any Person")
                orphans += 1
    c.summary = f"{orphans} unresolved manager pointer(s)"
    return c


def check_reaction_dedup(wall_doc: dict, email_to_person: Dict[str, str]) -> Check:
    """T8 — a reaction list shouldn't contain two emails belonging to
    the same Person. If it does, naive counters double-count and the
    'is-mine' check still works but the displayed total is wrong."""
    c = Check("T8: no wall reaction list contains two emails from the same Person")
    dupes = 0

    def scan(reactions: dict, ctx: str) -> None:
        nonlocal dupes
        if not isinstance(reactions, dict):
            return
        for emoji, emails in reactions.items():
            if not isinstance(emails, list):
                continue
            owners: Dict[str, List[str]] = {}
            for raw in emails:
                e = (raw or "").strip().lower()
                pid = email_to_person.get(e)
                if pid:
                    owners.setdefault(pid, []).append(e)
            for pid, es in owners.items():
                if len(es) > 1:
                    c.fail(f"{ctx} {emoji}: Person {pid} appears via multiple emails {es}")
                    dupes += 1

    for post in (wall_doc.get("posts") or []):
        pid = post.get("id") or "<no-id>"
        scan(post.get("reactions") or {}, f"post {pid}")
        for com in (post.get("comments") or []):
            scan(com.get("reactions") or {}, f"post {pid} comment {com.get('id')}")

    c.summary = f"{dupes} reaction list(s) with duplicate Person"
    return c


def check_fragmentation_report(people: List[dict], annotations: dict, holidays_doc: dict, wall_doc: dict) -> Check:
    """T9 — informational. Lists every Person whose photo / cover /
    holidays / wall posts live on a *non-main* linked email. These are
    the Persons every email-keyed lookup must walk all emails for; if a
    new feature ships using `main_google_email` alone, it will silently
    miss data for these specific people."""
    c = Check("T9: per-Person data fragmentation across linked emails (informational)")
    by_user = holidays_doc.get("by_user") or {}
    posts = wall_doc.get("posts") or []
    poster_emails: Dict[str, Set[str]] = {}
    for post in posts:
        ae = (post.get("author_email") or "").strip().lower()
        if ae:
            poster_emails.setdefault(ae, set()).add(post.get("id") or "")
        for com in (post.get("comments") or []):
            ce = (com.get("author_email") or "").strip().lower()
            if ce:
                poster_emails.setdefault(ce, set()).add(post.get("id") or "")

    fragmented_count = 0
    for p in people:
        emails = linked_emails(p)
        if len(emails) <= 1:
            continue
        main = emails[0]
        alt_only = emails[1:]
        bits: List[str] = []
        photo_on_alt = [e for e in alt_only if (annotations.get(e) or {}).get("directory_photo_uploaded_at")]
        cover_on_alt = [e for e in alt_only if (annotations.get(e) or {}).get("cover_photo_uploaded_at")]
        # Also note when main itself has no entry but an alt does — that's the bug-trigger.
        photo_on_main = bool((annotations.get(main) or {}).get("directory_photo_uploaded_at"))
        cover_on_main = bool((annotations.get(main) or {}).get("cover_photo_uploaded_at"))
        if photo_on_alt and not photo_on_main:
            bits.append(f"photo on alt {photo_on_alt[0]}")
        if cover_on_alt and not cover_on_main:
            bits.append(f"cover on alt {cover_on_alt[0]}")
        holiday_alt = [e for e in alt_only if e in by_user]
        if holiday_alt and main not in by_user:
            bits.append(f"holidays on alt {holiday_alt[0]}")
        post_alt = [e for e in alt_only if e in poster_emails]
        post_main = main in poster_emails
        if post_alt and not post_main:
            bits.append(f"posts on alt {post_alt[0]}")
        if bits:
            fragmented_count += 1
            c.warn(f"{p.get('name', p.get('id'))} ({main}): {'; '.join(bits)}")
    c.summary = f"{fragmented_count} Person(s) with data on alt emails only"
    return c


# Heuristic regex for the bug pattern — building a /assets/photos|covers/
# URL whose substitution slot references an email (typically
# `main_google_email`) without an obvious surrounding "walk all linked
# emails" structure.
RE_PHOTO_PATH = re.compile(
    r"/assets/(?:photos|covers)/\$\{[^}]*\}",
    re.IGNORECASE,
)
RE_EMAIL_IN_LINE = re.compile(r"main_google_email|\.email\b|dirphotokey\(", re.IGNORECASE)
# Patterns that, if present inside the *containing function*, mean this
# code is walking linked emails before building the URL — i.e. the bug
# is not present here. New safe patterns must be structural (loops,
# explicit alt walk) — not just incidental name matches.
RE_SAFE_HINTS = (
    "for (const e of",
    "for (const eraw of",
    "alt_google_emails",
    "photoemail",
    "photo_email",
    "linked email",          # comment shorthand from wall.html's fix
)
RE_FUNCTION_DECL = re.compile(r"\bfunction\b")


def _enclosing_function_start(lines: List[str], i: int) -> int:
    """Walk backwards from line i (1-indexed) to find the start of the
    enclosing function — defined as the nearest `function` keyword.
    Falls back to max(0, i-30) when the enclosing context is an arrow
    function (which has no `function` keyword) so the lint still has a
    sensible window to check for the safe-walk pattern."""
    for j in range(i - 1, max(-1, i - 400), -1):
        if RE_FUNCTION_DECL.search(lines[j]):
            return j
    return max(0, i - 30)


def check_code_lint() -> Check:
    """T10 — scan repo HTML/JS files for the bug pattern. The window
    examined is the *enclosing function only*, so a safe walk in a
    neighbouring function can't accidentally whitelist a vulnerable
    one (which is what masked profile.js coverSrc when this used a
    flat 18-line window)."""
    c = Check("T10: source files don't build /assets/photos|covers/ URLs from a single email without walking linked alts")
    skip_substrings = ("legacy", "-legacy", "_legacy", ".min.")
    files = sorted(list(ROOT.glob("*.html")) + list(ROOT.glob("*.js")))
    scanned = 0
    findings = 0
    for f in files:
        name = f.name
        if any(s in name for s in skip_substrings):
            continue
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        lines = text.splitlines()
        scanned += 1
        for i, line in enumerate(lines, start=1):
            if not RE_PHOTO_PATH.search(line):
                continue
            if not RE_EMAIL_IN_LINE.search(line):
                continue  # static path, not email-derived → not vulnerable
            fn_start = _enclosing_function_start(lines, i)
            window = "\n".join(lines[fn_start:i + 1]).lower()
            if any(hint in window for hint in RE_SAFE_HINTS):
                continue
            c.fail(f"{name}:{i}: /assets/photos|covers/ URL is built from a single email; walk all linked emails first. → {line.strip()[:130]}")
            findings += 1
    c.summary = f"scanned {scanned} files, {findings} vulnerable pattern(s)"
    return c


# ──────────────────────────────────────────────────────────────────────
# Driver
# ──────────────────────────────────────────────────────────────────────

def main() -> int:
    people_doc = load_json(ROOT / "people.json") or {}
    annotations_doc = load_json(ROOT / "annotations.json") or {}
    holidays_doc = load_json(ROOT / "holidays.json") or {}
    wall_doc = load_json(ROOT / "wall.json") or {}
    staff_doc = load_json(ROOT / "staff.json") or {}

    people = people_doc.get("people") or []
    annotations = annotations_doc.get("annotations") or {}
    staff_emails = {(u.get("email") or "").lower() for u in (staff_doc.get("users") or []) if u.get("email")}

    email_to_person: Dict[str, str] = {}
    for p in people:
        pid = p.get("id") or "<no-id>"
        for e in linked_emails(p):
            email_to_person.setdefault(e, pid)

    checks: List[Check] = [
        check_email_uniqueness(people),
        check_person_uniqueness(people),
        check_person_required_fields(people),
        check_annotations_resolve(annotations, email_to_person, staff_emails),
        check_holidays_resolve(holidays_doc, email_to_person),
        check_wall_resolve(wall_doc, email_to_person),
        check_line_managers_resolve(annotations, email_to_person),
        check_reaction_dedup(wall_doc, email_to_person),
        check_fragmentation_report(people, annotations, holidays_doc, wall_doc),
        check_code_lint(),
    ]

    print()
    print("=== TogetherBook merge-safety test suite ===")
    print(f"people.json: {len(people)} Persons / {len(email_to_person)} linked emails")
    print(f"annotations.json: {len(annotations)} entries")
    print(f"holidays.json: {len(holidays_doc.get('by_user') or {})} entries")
    print(f"wall.json: {len(wall_doc.get('posts') or [])} posts")
    print()

    failed = 0
    total_warnings = 0
    for chk in checks:
        chk.report()
        total_warnings += len(chk.warnings)
        if not chk.passed:
            failed += 1

    print()
    print(f"=== {len(checks)} check(s) — {failed} failed, {total_warnings} warning(s) ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
