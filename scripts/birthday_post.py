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
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Set
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
PEOPLE_JSON = ROOT / "people.json"
WALL_JSON = ROOT / "wall.json"

# A pool of self-hosted happy-birthday GIFs. Earlier we used direct
# Giphy URLs but several had been removed upstream and rendered as
# Giphy's "THIS CONTENT IS NOT AVAILABLE" placeholder. Mirroring the
# files into the repo means the rotation is stable for as long as the
# repo is published. Add more by dropping a .gif in wall-media/birthday/
# and listing it here. Paths are repo-root-relative so wall.html's
# mediaUrl() handler resolves them correctly.
BIRTHDAY_GIFS = [
    "wall-media/birthday/gif1.gif",
    "wall-media/birthday/gif2.gif",
    "wall-media/birthday/gif3.gif",
    "wall-media/birthday/gif4.gif",
    "wall-media/birthday/gif5.gif",
]

SYSTEM_EMAIL = "togetherbook@system"
SYSTEM_NAME = "TogetherBook"

# Twenty thoughtful birthday messages. The script picks one at random
# per person, skipping any template whose fingerprint (last 40 chars)
# already appears in a birthday post from the past 7 days. {name} is
# substituted in at post time.
MESSAGES = [
    "Happy Birthday {name}! \U0001F382 Hope today brings everything you wish for and a little more besides. Have a brilliant day.",
    "Wishing you the happiest of birthdays, {name}! \U0001F388 Take a moment today to do something just for you — you've earned it.",
    "Happy Birthday {name}! \U0001F389 Another year of being completely irreplaceable around here. Enjoy every minute of your day.",
    "{name}, happy birthday! \U0001F973 Hoping today is filled with cake, laughter, and at least one moment that makes you grin from ear to ear.",
    "Happy Birthday {name}! \U0001F382 The whole team is glad you were born — work would be far less fun without you. Have a great one.",
    "Wishing you a wonderful birthday, {name}! \U0001F388 May the year ahead bring good news, good company, and plenty of reasons to celebrate.",
    "Happy Birthday {name}! \U0001F389 Don't worry about getting older — you're still way ahead of where most of us were at your age. Enjoy the day.",
    "{name}, have a brilliant birthday! \U0001F382 Here's to another year of you doing your thing and making the rest of us look good in the process.",
    "Happy Birthday {name}! \U0001F388 May your inbox stay quiet, your coffee stay hot, and your day be everything you hope for.",
    "Wishing you a fantastic birthday, {name}! \U0001F973 We're lucky to have you on the team — hope today is as great as you are.",
    "Happy Birthday {name}! \U0001F382 Step away from the laptop for a bit today. The work will still be here tomorrow; your birthday won't.",
    "{name}, happy birthday! \U0001F389 Sending you cake-shaped wishes and a hope that the year ahead is your best one yet.",
    "Happy Birthday {name}! \U0001F388 Hope your day is filled with all the things you love and none of the things you don't.",
    "Wishing you a very happy birthday, {name}! \U0001F382 Thank you for being part of what makes this team brilliant. Have a great one.",
    "Happy Birthday {name}! \U0001F973 Take the long lunch. Leave early. Order the second slice. Today's the day for all of it.",
    "{name}, happy birthday! \U0001F389 Wishing you twelve months of fortunate timing, kind colleagues, and an absurd amount of luck.",
    "Happy Birthday {name}! \U0001F388 Hope someone you love bakes you something delicious today, and that it's exactly the right kind of sweet.",
    "Wishing you a wonderful birthday, {name}! \U0001F382 Whatever you're up to, here's hoping it's everything you wanted and nothing you didn't.",
    "Happy Birthday {name}! \U0001F973 The whole team is sending you a quiet, slightly off-key chorus of Happy Birthday from across the wall. Have a great one.",
    "{name}, happy birthday! \U0001F389 A year wiser, a year warmer, a year nearer to whatever brilliant thing you're working toward. Enjoy the day.",
]


def pick_unused_message(posts: list, now_utc: datetime, name: str, exclude: Set[str]) -> str:
    """Pick a MESSAGES template not used by any birthday post in the past
    7 days, and not already chosen in this run. Falls back to least-
    recently-used if the pool is exhausted. Returns the template string
    (caller substitutes the name in)."""
    cutoff = now_utc - timedelta(days=7)
    recent_bodies: List[str] = []
    body_seen_at = {t: datetime.min.replace(tzinfo=timezone.utc) for t in MESSAGES}
    for p in posts:
        if not (p.get("id") or "").startswith("post_birthday_"):
            continue
        when = parse_iso(p.get("created_at") or "")
        if when is None:
            continue
        body = p.get("body") or ""
        for t in MESSAGES:
            fp = t[-40:]   # tail of the template — independent of {name}
            if fp in body:
                if when > body_seen_at[t]:
                    body_seen_at[t] = when
                if when >= cutoff:
                    recent_bodies.append(t)
    used_recent = set(recent_bodies)
    candidates = [t for t in MESSAGES if t not in used_recent and t not in exclude]
    if not candidates:
        # Pool exhausted in 7 days — fall back to least-recently-used.
        candidates = sorted(
            (t for t in MESSAGES if t not in exclude),
            key=lambda t: body_seen_at[t],
        )
    if not candidates:
        candidates = list(MESSAGES)
    return random.choice(candidates) if len(candidates) > 1 else candidates[0]


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
    used_gifs: Set[str] = set()
    used_msgs: Set[str] = set()
    added = 0
    for person in birthday_people:
        slug = (person.get("url_slug") or person.get("id") or "unknown").lower()
        post_id = f"post_birthday_{year}_{slug}"
        if post_id in existing_ids:
            continue
        name = person.get("name") or slug
        gif = pick_unused_gif(posts, now_utc, used_gifs)
        used_gifs.add(gif)
        template = pick_unused_message(posts, now_utc, name, used_msgs)
        used_msgs.add(template)
        body = template.format(name=name)
        post = {
            "id": post_id,
            "author_email": SYSTEM_EMAIL,
            "author_name": SYSTEM_NAME,
            "created_at": created_at,
            "body": body,
            "photos": [gif],
            "channel": None,
            "reactions": {},
            "comments": [],
        }
        posts.insert(0, post)
        added += 1
        print(f"Added birthday post for {name} ({post_id}) — gif {gif.rsplit('/', 1)[-1]}, msg {template[:30]!r}")

    if added:
        wall_doc["updated_at"] = iso_z(datetime.now(timezone.utc))
        WALL_JSON.write_text(json.dumps(wall_doc, indent=2, ensure_ascii=False) + "\n")
        print(f"Wrote {added} new birthday post(s) to wall.json")
    else:
        print(f"All {len(birthday_people)} birthday post(s) already exist for {now_uk.date().isoformat()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
