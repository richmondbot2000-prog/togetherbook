"""
Read the SQLite database produced by telegram_monitor.py and emit a public-
safe `telegram-mentions.json` for the Brandwatch page.

What goes in the JSON
- Only rows that matched at least one watchlist term are exported (we don't
  publish the entire firehose).
- Each row: channel, posted_at, matched terms, t.me link, and a redacted
  text excerpt (first 500 chars after PII scrubbing).

PII redaction (server-side, same patterns as the Pipeline page)
- Email addresses → ***@***
- Phone numbers (E.164 / US local) → *******
- Credit-card-like 13–19 digit numbers → ****-****-****-NNNN (last 4)
- US SSN-style 9-digit numbers → ***-**-NNNN (last 4)
- 18–24 digit numbers (ARef-shape) → last 5 visible
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(os.environ.get("MONITOR_DB", "telegram-monitor.db"))
OUTPUT_PATH = Path(os.environ.get("MENTIONS_JSON", "telegram-mentions.json"))
EXCERPT_CHARS = 500
MAX_MENTIONS = 1000  # safety cap; newest first

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}(?!\w)"
)
CC_RE = re.compile(r"\b(?:\d[\s\-]?){12,18}\d\b")  # 13-19 digit cards
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
LONG_DIGIT_RE = re.compile(r"\b\d{18,24}\b")


def mask_aref_like(m: re.Match) -> str:
    s = m.group(0)
    return "*" * (len(s) - 5) + s[-5:] if len(s) > 5 else s


def mask_cc(m: re.Match) -> str:
    s = re.sub(r"[\s\-]", "", m.group(0))
    if len(s) < 13:
        return m.group(0)
    return "****-****-****-" + s[-4:]


def mask_ssn(m: re.Match) -> str:
    return "***-**-" + m.group(0)[-4:]


def redact(text: str) -> str:
    if not text:
        return text
    out = EMAIL_RE.sub("***@***", text)
    out = SSN_RE.sub(mask_ssn, out)
    out = CC_RE.sub(mask_cc, out)
    out = LONG_DIGIT_RE.sub(mask_aref_like, out)
    out = PHONE_RE.sub("*******", out)
    return out


def main() -> None:
    started = datetime.datetime.now(datetime.timezone.utc)
    if not DB_PATH.exists():
        print(f"# {DB_PATH} not found — writing empty mentions file", flush=True)
        OUTPUT_PATH.write_text(json.dumps({
            "snapshot_at": started.isoformat(),
            "snapshot_date": started.date().isoformat(),
            "mention_count": 0,
            "channel_count": 0,
            "mentions": [],
        }, indent=2))
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT channel, channel_id, message_id, posted_at, sender_id,
               text, matched_terms, has_media, raw_link
        FROM messages
        WHERE matched_terms IS NOT NULL
          AND matched_terms <> '[]'
        ORDER BY posted_at DESC
        LIMIT ?
        """,
        [MAX_MENTIONS],
    )
    rows = cur.fetchall()

    mentions = []
    channels_seen: set[str] = set()
    for channel, channel_id, message_id, posted_at, sender_id, text, matched_terms, has_media, raw_link in rows:
        try:
            terms = json.loads(matched_terms) if matched_terms else []
        except json.JSONDecodeError:
            terms = []
        excerpt = (text or "")[:EXCERPT_CHARS]
        redacted = redact(excerpt)
        if redacted and len(text or "") > EXCERPT_CHARS:
            redacted += "…"
        mentions.append({
            "channel": channel,
            "channel_url": f"https://t.me/{channel}",
            "message_url": raw_link or f"https://t.me/{channel}/{message_id}",
            "posted_at": posted_at,
            "matched": sorted(set(terms)),
            "has_media": bool(has_media),
            "excerpt": redacted,
        })
        if channel:
            channels_seen.add(channel)

    # Channel-level summary (counts per channel, newest hit per channel).
    by_channel: dict[str, dict] = {}
    for m in mentions:
        ch = m["channel"] or "?"
        slot = by_channel.setdefault(ch, {"channel": ch, "channel_url": m["channel_url"], "count": 0, "latest_at": None})
        slot["count"] += 1
        if not slot["latest_at"] or (m["posted_at"] and m["posted_at"] > slot["latest_at"]):
            slot["latest_at"] = m["posted_at"]

    output = {
        "snapshot_at": started.isoformat(),
        "snapshot_date": started.date().isoformat(),
        "mention_count": len(mentions),
        "channel_count": len(channels_seen),
        "channels": sorted(by_channel.values(), key=lambda c: -c["count"]),
        "mentions": mentions,
    }
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, default=str))
    print(
        f"# wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size:,} bytes); "
        f"{len(mentions):,} mentions across {len(channels_seen)} channels",
        flush=True,
    )
    conn.close()


if __name__ == "__main__":
    main()
