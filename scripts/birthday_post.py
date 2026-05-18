#!/usr/bin/env python3
"""Daily birthday post generator.

Scans people.json for anyone whose date_of_birth's month+day matches
today's UK date, and adds a "Happy Birthday <name>" post (authored by
TogetherBook) to wall.json. Deduped via stable post IDs of the form
`post_birthday_<YYYY>_<url_slug>` so the daily GH-Action re-run is
idempotent and one post per person per year is the steady state.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Set
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
PEOPLE_JSON = ROOT / "people.json"
WALL_JSON = ROOT / "wall.json"

# A pool of public happy-birthday GIFs served from Giphy's CDN. The
# script rotates through this list and never reuses a GIF within a
# 7-day window — see pick_unused_gif().
BIRTHDAY_GIFS = [
    "https://media.giphy.com/media/g5R9dok94mrIvplmZd/giphy.gif",
    "https://media.giphy.com/media/26FPq3X8Y4Tn3aDz2/giphy.gif",
    "https://media.giphy.com/media/3oz8xUKsTOoyAcRO64/giphy.gif",
    "https://media.giphy.com/media/o75ajIFH0QnQC3nCeD/giphy.gif",
    "https://media.giphy.com/media/l0MYt5jPR6QX5pnqM/giphy.gif",
    "https://media.giphy.com/media/26AHONQ79FdWZhAI0/giphy.gif",
    "https://media.giphy.com/media/Qvm1IxR9ZbnD8AOQDr/giphy.gif",
    "https://media.giphy.com/media/JpG2A9P3dPHXaTYrwu/giphy.gif",
    "https://media.giphy.com/media/qWh3K1pBzbiCpc8FBP/giphy.gif",
    "https://media.giphy.com/media/26gscYNwoSGrZB7Q4/giphy.gif",
    "https://media.giphy.com/media/3o6Zt5hLEiQzMMQDqg/giphy.gif",
    "https://media.giphy.com/media/jOosNRWWzcrjxRC1KO/giphy.gif",
]

SYSTEM_EMAIL = "togetherbook@system"
SYSTEM_NAME = "TogetherBook"


def parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def pick_unused_gif(posts: list, now_utc: datetime, exclude: Set[str]) -> str:
    """Pick a GIF not used by any birthday post in the past 7 days, and
    not already chosen in this run (the `exclude` set covers Aerona +
    James getting different GIFs when their posts are created together).
    Falls back to least-recently-used if the pool is exhausted."""
    cutoff = now_utc - timedelta(days=7)
    used_recent = set()
    last_seen = {g: datetime.min.replace(tzinfo=timezone.utc) for g in BIRTHDAY_GIFS}
    for p in posts:
        if not (p.get("id") or "").startswith("post_birthday_"):
            continue
        when = parse_iso(p.get("created_at") or "")
        if when is None:
            continue
        for photo in (p.get("photos") or []):
            if photo in last_seen and when > last_seen[photo]:
                last_seen[photo] = when
            if when >= cutoff and photo in BIRTHDAY_GIFS:
                used_recent.add(photo)
    candidates = [g for g in BIRTHDAY_GIFS if g not in used_recent and g not in exclude]
    if candidates:
        return candidates[0]
    # Pool exhausted in the 7-day window — fall back to the
    # least-recently-used GIF that isn't already taken in this run.
    fallback = sorted(
        (g for g in BIRTHDAY_GIFS if g not in exclude),
        key=lambda g: last_seen[g],
    )
    return fallback[0] if fallback else BIRTHDAY_GIFS[0]


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def main() -> int:
    london = ZoneInfo("Europe/London")
    now_uk = datetime.now(london)
    md = now_uk.strftime("%m-%d")
    year = now_uk.year

    people_doc = json.loads(PEOPLE_JSON.read_text())
    people = people_doc.get("people", [])

    birthday_people = []
    for p in people:
        if p.get("suspended") or p.get("deletion_time"):
            continue
        dob = p.get("date_of_birth") or ""
        if len(dob) >= 10 and dob[5:10] == md:
            birthday_people.append(p)

    if not birthday_people:
        print(f"No birthdays on {now_uk.date().isoformat()}")
        return 0

    wall_doc = json.loads(WALL_JSON.read_text())
    posts = wall_doc.setdefault("posts", [])
    existing_ids = {p.get("id") for p in posts}

    # Stamp every post for "today" at 07:00 UK local time so the wall
    # orders them just above the start of the work day.
    created_at = iso_z(now_uk.replace(hour=7, minute=0, second=0, microsecond=0))

    now_utc = datetime.now(timezone.utc)
    used_this_run: Set[str] = set()
    added = 0
    for person in birthday_people:
        slug = (person.get("url_slug") or person.get("id") or "unknown").lower()
        post_id = f"post_birthday_{year}_{slug}"
        if post_id in existing_ids:
            continue
        name = person.get("name") or slug
        gif = pick_unused_gif(posts, now_utc, used_this_run)
        used_this_run.add(gif)
        post = {
            "id": post_id,
            "author_email": SYSTEM_EMAIL,
            "author_name": SYSTEM_NAME,
            "created_at": created_at,
            "body": (
                f"Happy Birthday {name}! \U0001F382\U0001F388\U0001F389\n\n"
                "Wishing you a wonderful day from everyone at TogetherBook."
            ),
            "photos": [gif],
            "channel": None,
            "reactions": {},
            "comments": [],
        }
        posts.insert(0, post)
        added += 1
        print(f"Added birthday post for {name} ({post_id}) — gif {gif.rsplit('/', 2)[-2]}")

    if added:
        wall_doc["updated_at"] = iso_z(datetime.now(timezone.utc))
        WALL_JSON.write_text(json.dumps(wall_doc, indent=2, ensure_ascii=False) + "\n")
        print(f"Wrote {added} new birthday post(s) to wall.json")
    else:
        print(f"All {len(birthday_people)} birthday post(s) already exist for {now_uk.date().isoformat()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
