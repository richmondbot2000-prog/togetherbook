"""
Telegram brand-mention monitor.

Monitors a list of public Telegram channels for mentions of specified brand
keywords, persists hits to SQLite, and pushes alerts to Slack.

Two modes:
    backfill   Pull historical messages from each channel (default 30 days)
    watch      Stream new messages live and alert on matches
    both       Backfill then watch

Required env vars:
    TG_API_ID      from my.telegram.org
    TG_API_HASH    from my.telegram.org

Optional env vars:
    TG_SESSION         session file name (default 'monitor')
    SLACK_WEBHOOK_URL  Slack incoming webhook for alerts
    MONITOR_DB         SQLite path (default 'telegram-monitor.db')
    MONITOR_CONFIG     watchlist json path (default 'telegram-watchlist.json')
    BACKFILL_DAYS      history window (default 30)

Operational notes (per the spec):
- Only join PUBLIC channels (openly-shared public username or t.me/joinchat).
- Use a dedicated Telegram account; never use a personal account.
- Read-only — do not engage with or message sellers.
- The session file is equivalent to a login credential — keep it private.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import httpx
from telethon import TelegramClient, events
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    UsernameNotOccupiedError,
)
from telethon.tl.types import Channel, Message


# ─── Config ──────────────────────────────────────────────────────────────
def _required(var: str) -> str:
    v = os.environ.get(var)
    if not v:
        sys.exit(f"error: {var} not set")
    return v


API_ID = int(_required("TG_API_ID"))
API_HASH = _required("TG_API_HASH")
SESSION_NAME = os.environ.get("TG_SESSION", "monitor")
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL")
DB_PATH = Path(os.environ.get("MONITOR_DB", "monitor.db"))
CONFIG_PATH = Path(os.environ.get("MONITOR_CONFIG", "telegram-watchlist.json"))
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "30"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tg-monitor")


# ─── Watchlist & matching ────────────────────────────────────────────────
@dataclass
class Watchlist:
    channels: list[str]
    keywords: list[str]
    regex_keywords: list[str]

    @classmethod
    def load(cls, path: Path) -> "Watchlist":
        data = json.loads(path.read_text())
        return cls(
            channels=data.get("channels", []),
            keywords=data.get("keywords", []),
            regex_keywords=data.get("regex_keywords", []),
        )

    def compiled_patterns(self) -> list[re.Pattern]:
        patterns = [re.compile(re.escape(k), re.IGNORECASE) for k in self.keywords]
        patterns += [re.compile(k, re.IGNORECASE) for k in self.regex_keywords]
        return patterns


def find_matches(text: str, patterns: Iterable[re.Pattern]) -> list[str]:
    if not text:
        return []
    hits: list[str] = []
    for p in patterns:
        for m in p.finditer(text):
            hits.append(m.group(0))
    return hits


# ─── Storage ─────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'telegram',
    channel TEXT NOT NULL,
    channel_id INTEGER,
    message_id INTEGER NOT NULL,
    posted_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    sender_id INTEGER,
    sender_name TEXT,
    text TEXT,
    matched_terms TEXT,
    has_media INTEGER DEFAULT 0,
    raw_link TEXT,
    UNIQUE(source, channel, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_posted_at ON messages(posted_at);
CREATE INDEX IF NOT EXISTS idx_messages_channel  ON messages(channel);
CREATE INDEX IF NOT EXISTS idx_messages_matched  ON messages(matched_terms);
CREATE INDEX IF NOT EXISTS idx_messages_source   ON messages(source);
"""


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    # Migrate older databases that lack the source / sender_name columns.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "source" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN source TEXT DEFAULT 'telegram'")
    if "sender_name" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN sender_name TEXT")
    conn.commit()
    return conn


def store_message(
    conn: sqlite3.Connection,
    *,
    channel: str,
    channel_id: int | None,
    msg: Message,
    matches: list[str],
) -> bool:
    """Insert a message. Returns True if newly inserted."""
    link = f"https://t.me/{channel}/{msg.id}"
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO messages
                (source, channel, channel_id, message_id, posted_at, collected_at,
                 sender_id, text, matched_terms, has_media, raw_link)
                VALUES ('telegram', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel,
                    channel_id,
                    msg.id,
                    msg.date.isoformat() if msg.date else None,
                    datetime.now(timezone.utc).isoformat(),
                    msg.sender_id,
                    msg.message or "",
                    json.dumps(matches),
                    1 if msg.media else 0,
                    link,
                ),
            )
        return True
    except sqlite3.IntegrityError:
        return False


# ─── Alerting ────────────────────────────────────────────────────────────
async def send_slack_alert(
    client: httpx.AsyncClient,
    *,
    channel: str,
    msg: Message,
    matches: list[str],
) -> None:
    if not SLACK_WEBHOOK:
        return
    link = f"https://t.me/{channel}/{msg.id}"
    text_excerpt = (msg.message or "")[:500]
    payload = {
        "text": f":rotating_light: Brand mention in *{channel}*",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":rotating_light: *Mention in `{channel}`*\n"
                        f"*Matched:* `{', '.join(sorted(set(matches)))}`\n"
                        f"*Posted:* {msg.date.isoformat() if msg.date else 'unknown'}\n"
                        f"*Link:* <{link}|open in Telegram>"
                    ),
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```{text_excerpt}```"},
            },
        ],
    }
    try:
        r = await client.post(SLACK_WEBHOOK, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        log.warning("Slack alert failed: %s", e)


# ─── Backfill ────────────────────────────────────────────────────────────
async def backfill_channel(
    tg: TelegramClient,
    http: httpx.AsyncClient,
    conn: sqlite3.Connection,
    channel: str,
    patterns: list[re.Pattern],
    since: datetime,
) -> None:
    log.info("Backfilling %s since %s", channel, since.date())
    try:
        entity = await tg.get_entity(channel)
    except (UsernameNotOccupiedError, ChannelPrivateError, ValueError) as e:
        log.error("Cannot access channel %s: %s", channel, e)
        return

    channel_id = entity.id if isinstance(entity, Channel) else None
    new_count = 0
    matched_count = 0

    try:
        async for msg in tg.iter_messages(entity, offset_date=None):
            if msg.date and msg.date < since:
                break
            matches = find_matches(msg.message or "", patterns)
            inserted = store_message(
                conn,
                channel=channel,
                channel_id=channel_id,
                msg=msg,
                matches=matches,
            )
            if inserted:
                new_count += 1
                if matches:
                    matched_count += 1
                    await send_slack_alert(
                        http, channel=channel, msg=msg, matches=matches
                    )
    except FloodWaitError as e:
        log.warning("Flood wait %ds on %s — pausing", e.seconds, channel)
        await asyncio.sleep(e.seconds + 5)

    log.info(
        "Backfill %s done: %d new messages, %d matched",
        channel,
        new_count,
        matched_count,
    )


# ─── Live watch ──────────────────────────────────────────────────────────
async def watch(
    tg: TelegramClient,
    http: httpx.AsyncClient,
    conn: sqlite3.Connection,
    watchlist: Watchlist,
) -> None:
    patterns = watchlist.compiled_patterns()

    entities = []
    for ch in watchlist.channels:
        try:
            entities.append(await tg.get_entity(ch))
        except Exception as e:
            log.error("Skipping channel %s: %s", ch, e)

    if not entities:
        log.error("No channels resolved — nothing to watch")
        return

    @tg.on(events.NewMessage(chats=entities))
    async def handler(event: events.NewMessage.Event) -> None:
        msg: Message = event.message
        chat = await event.get_chat()
        channel_name = getattr(chat, "username", None) or str(chat.id)
        matches = find_matches(msg.message or "", patterns)
        store_message(
            conn,
            channel=channel_name,
            channel_id=chat.id,
            msg=msg,
            matches=matches,
        )
        if matches:
            log.info("MATCH in %s: %s", channel_name, sorted(set(matches)))
            await send_slack_alert(http, channel=channel_name, msg=msg, matches=matches)

    log.info("Watching %d channels — Ctrl-C to stop", len(entities))
    await tg.run_until_disconnected()


# ─── Entry ───────────────────────────────────────────────────────────────
async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["backfill", "watch", "both"])
    parser.add_argument("--days", type=int, default=BACKFILL_DAYS,
                        help="how many days of history to backfill")
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        log.error("Missing config at %s", CONFIG_PATH)
        sys.exit(1)

    watchlist = Watchlist.load(CONFIG_PATH)
    log.info(
        "Loaded %d channels, %d keywords, %d regexes",
        len(watchlist.channels),
        len(watchlist.keywords),
        len(watchlist.regex_keywords),
    )

    conn = init_db(DB_PATH)
    tg = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await tg.start()

    async with httpx.AsyncClient() as http:
        if args.mode in ("backfill", "both"):
            since = datetime.now(timezone.utc) - timedelta(days=args.days)
            patterns = watchlist.compiled_patterns()
            for ch in watchlist.channels:
                await backfill_channel(tg, http, conn, ch, patterns, since)

        if args.mode in ("watch", "both"):
            await watch(tg, http, conn, watchlist)

    await tg.disconnect()
    conn.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped")
