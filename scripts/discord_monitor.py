"""
Discord brand-mention monitor.

Monitors public Discord servers for mentions of brand keywords, persists hits
to the same SQLite database as the Telegram collector, and pushes Slack alerts.

Two modes:
    backfill   Pull recent message history from configured channels
    watch      Stream new messages live and alert on matches
    both       Backfill then watch

Operational notes:
- Use a DEDICATED Discord account. Do not use a personal account.
- Only join servers via PUBLIC invite links that are openly shared. Do not
  use deception to gain access to private servers.
- Self-bots (user accounts with bot-like behaviour) violate Discord's ToS
  and will get the account banned. Three sensible paths in preference order:
    1. Run as a real bot account (preferred for servers YOU own or admin) —
       set DISCORD_IS_BOT=true.
    2. Use a paid commercial monitoring service.
    3. Run a user-account monitor and accept account-burn risk (this default).
- Read-only. Do not post, react, DM, or engage with sellers.

Required env vars:
    DISCORD_TOKEN          bot token (preferred) or user token
    DISCORD_IS_BOT         "true" if running as a bot account (default false)

Optional:
    SLACK_WEBHOOK_URL      Slack alerts on matches
    MONITOR_DB             SQLite path (default 'monitor.db')
    DISCORD_CONFIG         watchlist json path (default 'discord-watchlist.json')
    BACKFILL_DAYS          history window (default 30)
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import discord
import httpx


def _required(var: str) -> str:
    v = os.environ.get(var)
    if not v:
        sys.exit(f"error: {var} not set")
    return v


DISCORD_TOKEN = _required("DISCORD_TOKEN")
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL")
DB_PATH = Path(os.environ.get("MONITOR_DB", "monitor.db"))
CONFIG_PATH = Path(os.environ.get("DISCORD_CONFIG", "discord-watchlist.json"))
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "30"))
IS_BOT = os.environ.get("DISCORD_IS_BOT", "false").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("discord-monitor")


# ─── Storage (same schema as the Telegram collector) ─────────────────────
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
CREATE INDEX IF NOT EXISTS idx_messages_source   ON messages(source);
"""


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "source" not in cols:
        log.info("Migrating schema: adding 'source' and 'sender_name' columns")
        conn.execute("ALTER TABLE messages ADD COLUMN source TEXT DEFAULT 'telegram'")
        conn.execute("ALTER TABLE messages ADD COLUMN sender_name TEXT")
        conn.commit()
    return conn


def store_message(
    conn: sqlite3.Connection,
    *,
    msg: discord.Message,
    matches: list[str],
) -> bool:
    """Insert a Discord message. Returns True if newly inserted."""
    guild_name = msg.guild.name if msg.guild else "DM"
    channel_label = (
        f"{guild_name}#{msg.channel.name}"
        if hasattr(msg.channel, "name") else f"{guild_name}#{msg.channel.id}"
    )
    link = msg.jump_url
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO messages
                (source, channel, channel_id, message_id, posted_at,
                 collected_at, sender_id, sender_name, text, matched_terms,
                 has_media, raw_link)
                VALUES ('discord', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_label,
                    msg.channel.id,
                    msg.id,
                    msg.created_at.isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                    msg.author.id,
                    str(msg.author),
                    msg.content or "",
                    json.dumps(matches),
                    1 if msg.attachments else 0,
                    link,
                ),
            )
        return True
    except sqlite3.IntegrityError:
        return False


# ─── Watchlist & matching ────────────────────────────────────────────────
def load_config(path: Path) -> dict:
    return json.loads(path.read_text())


def compile_patterns(cfg: dict) -> list[re.Pattern]:
    patterns = [re.compile(re.escape(k), re.IGNORECASE) for k in cfg.get("keywords", [])]
    patterns += [re.compile(k, re.IGNORECASE) for k in cfg.get("regex_keywords", [])]
    return patterns


def find_matches(text: str, patterns: Iterable[re.Pattern]) -> list[str]:
    if not text:
        return []
    hits: list[str] = []
    for p in patterns:
        for m in p.finditer(text):
            hits.append(m.group(0))
    return hits


# ─── Alerting ────────────────────────────────────────────────────────────
async def send_slack_alert(
    http: httpx.AsyncClient,
    *,
    msg: discord.Message,
    matches: list[str],
) -> None:
    if not SLACK_WEBHOOK:
        return
    guild_name = msg.guild.name if msg.guild else "DM"
    channel_label = (
        f"{guild_name} / #{msg.channel.name}"
        if hasattr(msg.channel, "name") else f"{guild_name} / {msg.channel.id}"
    )
    excerpt = (msg.content or "")[:500]
    payload = {
        "text": f":rotating_light: Discord brand mention in {channel_label}",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":rotating_light: *Discord mention in `{channel_label}`*\n"
                        f"*Matched:* `{', '.join(sorted(set(matches)))}`\n"
                        f"*Author:* {msg.author}\n"
                        f"*Posted:* {msg.created_at.isoformat()}\n"
                        f"*Link:* <{msg.jump_url}|open in Discord>"
                    ),
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```{excerpt}```"},
            },
        ],
    }
    try:
        r = await http.post(SLACK_WEBHOOK, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        log.warning("Slack alert failed: %s", e)


# ─── Monitor ─────────────────────────────────────────────────────────────
class Monitor(discord.Client):
    def __init__(self, *, cfg: dict, mode: str, days: int, **kw):
        super().__init__(**kw)
        self.cfg = cfg
        self.mode = mode
        self.days = days
        self.patterns = compile_patterns(cfg)
        self.conn = init_db(DB_PATH)
        self.http_session: httpx.AsyncClient | None = None
        self.channel_ids: set[int] = set(cfg.get("channel_ids", []))
        self.guild_ids: set[int] = set(cfg.get("guild_ids", []))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)
        self.http_session = httpx.AsyncClient()
        if self.mode in ("backfill", "both"):
            await self._backfill()
        if self.mode == "backfill":
            await self.close()

    async def _backfill(self) -> None:
        since = datetime.now(timezone.utc) - timedelta(days=self.days)
        log.info("Backfilling Discord history since %s", since.date())

        targets = []
        for cid in self.channel_ids:
            ch = self.get_channel(cid)
            if ch is None:
                try:
                    ch = await self.fetch_channel(cid)
                except discord.NotFound:
                    log.error("Channel %d not found / not accessible", cid)
                    continue
                except discord.Forbidden:
                    log.error("Channel %d: forbidden", cid)
                    continue
            targets.append(ch)

        for guild_id in self.guild_ids:
            guild = self.get_guild(guild_id)
            if guild is None:
                log.warning("Guild %d not visible — are you a member?", guild_id)
                continue
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).read_message_history:
                    targets.append(ch)

        for ch in targets:
            new_count = matched_count = 0
            try:
                async for msg in ch.history(limit=None, after=since):
                    matches = find_matches(msg.content or "", self.patterns)
                    if store_message(self.conn, msg=msg, matches=matches):
                        new_count += 1
                        if matches:
                            matched_count += 1
                            await send_slack_alert(self.http_session, msg=msg, matches=matches)
            except discord.Forbidden:
                log.error("Cannot read history for %s", ch)
                continue
            log.info("Backfill %s: %d new, %d matched", ch, new_count, matched_count)

    async def on_message(self, msg: discord.Message) -> None:
        if self.mode == "backfill":
            return
        if self.channel_ids and msg.channel.id not in self.channel_ids:
            if not (msg.guild and msg.guild.id in self.guild_ids):
                return
        matches = find_matches(msg.content or "", self.patterns)
        if store_message(self.conn, msg=msg, matches=matches) and matches:
            log.info("MATCH in %s: %s", msg.channel, sorted(set(matches)))
            await send_slack_alert(self.http_session, msg=msg, matches=matches)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["backfill", "watch", "both"])
    parser.add_argument("--days", type=int, default=BACKFILL_DAYS)
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        log.error("Missing config at %s", CONFIG_PATH)
        sys.exit(1)

    cfg = load_config(CONFIG_PATH)

    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.messages = True

    monitor = Monitor(cfg=cfg, mode=args.mode, days=args.days, intents=intents)
    monitor.run(DISCORD_TOKEN, bot=IS_BOT)


if __name__ == "__main__":
    main()
