"""
Have I Been Pwned domain monitor.

Queries HIBP's domain-search API for breached accounts on owned domains,
stores results in the monitor SQLite database, and alerts on new breaches
that weren't seen on previous runs.

Setup:
- You must verify domain ownership at https://haveibeenpwned.com/DomainSearch
  before HIBP will return data for it.
- Get an API key from https://haveibeenpwned.com/API/Key (paid subscription).
- Designed to run on a schedule (cron, every 6h is plenty).

What HIBP returns:
- A map of local-part (the bit before @) → list of breach names.
- The actual passwords/data are NOT exposed via the domain API. You get the
  fact that user@yourdomain was in BreachX, not the contents.

Operational notes:
- Treat the local-parts as PII. Store with access controls.
- HIBP rate limits API keys; this script runs sequentially with retries.

Required env vars:
    HIBP_API_KEY            from https://haveibeenpwned.com/API/Key

Optional:
    SLACK_WEBHOOK_URL       Slack alerts on new breaches
    MONITOR_DB              SQLite path (default 'monitor.db')
    HIBP_CONFIG             watchlist json path (default 'hibp-watchlist.json')
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


def _required(var: str) -> str:
    v = os.environ.get(var)
    if not v:
        sys.exit(f"error: {var} not set")
    return v


HIBP_API_KEY = _required("HIBP_API_KEY")
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL")
DB_PATH = Path(os.environ.get("MONITOR_DB", "monitor.db"))
CONFIG_PATH = Path(os.environ.get("HIBP_CONFIG", "hibp-watchlist.json"))

HIBP_BASE = "https://haveibeenpwned.com/api/v3"
USER_AGENT = "brand-monitor/1.0"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hibp")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hibp_breaches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    local_part TEXT NOT NULL,
    breach_name TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE(domain, local_part, breach_name)
);

CREATE TABLE IF NOT EXISTS hibp_breach_details (
    name TEXT PRIMARY KEY,
    title TEXT,
    breach_date TEXT,
    added_date TEXT,
    pwn_count INTEGER,
    data_classes TEXT,
    description TEXT,
    is_sensitive INTEGER,
    is_verified INTEGER,
    last_synced TEXT
);

CREATE INDEX IF NOT EXISTS idx_hibp_domain ON hibp_breaches(domain);
CREATE INDEX IF NOT EXISTS idx_hibp_breach ON hibp_breaches(breach_name);
"""


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def hibp_get(client: httpx.Client, path: str) -> dict | list | None:
    """GET with HIBP-friendly retries on 429/5xx."""
    url = f"{HIBP_BASE}/{path}"
    for attempt in range(5):
        r = client.get(
            url,
            headers={"hibp-api-key": HIBP_API_KEY, "user-agent": USER_AGENT},
            timeout=30,
        )
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        if r.status_code == 429:
            wait = int(r.headers.get("retry-after", 6))
            log.warning("HIBP rate-limited, sleeping %ds", wait)
            time.sleep(wait)
            continue
        if 500 <= r.status_code < 600:
            log.warning("HIBP %d on %s, retry %d", r.status_code, path, attempt)
            time.sleep(2 ** attempt)
            continue
        log.error("HIBP %d on %s: %s", r.status_code, path, r.text[:200])
        return None
    log.error("HIBP gave up on %s after retries", path)
    return None


def sync_breach_details(client: httpx.Client, conn: sqlite3.Connection, name: str) -> None:
    """Cache breach metadata so alerts can include severity context."""
    cur = conn.execute(
        "SELECT last_synced FROM hibp_breach_details WHERE name = ?", (name,)
    ).fetchone()
    if cur:
        return
    data = hibp_get(client, f"breach/{name}")
    if not data:
        return
    conn.execute(
        """
        INSERT OR REPLACE INTO hibp_breach_details
        (name, title, breach_date, added_date, pwn_count, data_classes,
         description, is_sensitive, is_verified, last_synced)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data.get("Name"),
            data.get("Title"),
            data.get("BreachDate"),
            data.get("AddedDate"),
            data.get("PwnCount"),
            json.dumps(data.get("DataClasses", [])),
            (data.get("Description") or "")[:500],
            1 if data.get("IsSensitive") else 0,
            1 if data.get("IsVerified") else 0,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def slack_alert_new_breach(
    http: httpx.Client,
    *,
    domain: str,
    breach_name: str,
    affected_count: int,
    breach_details: dict | None,
) -> None:
    if not SLACK_WEBHOOK:
        return
    severity = ":rotating_light:"
    fields = [f"*Domain:* {domain}", f"*Affected accounts:* {affected_count}"]
    if breach_details:
        try:
            classes = json.loads(breach_details.get("data_classes") or "[]")
        except json.JSONDecodeError:
            classes = []
        sensitive_classes = {"Passwords", "Credit cards", "Bank account numbers", "Social security numbers"}
        if any(c in sensitive_classes for c in classes):
            severity = ":fire::rotating_light:"
        fields += [
            f"*Breach date:* {breach_details.get('breach_date', 'unknown')}",
            f"*Total breach size:* {breach_details.get('pwn_count', 'unknown'):,}" if breach_details.get('pwn_count') else "",
            f"*Data exposed:* {', '.join(classes)}" if classes else "",
        ]
        fields = [f for f in fields if f]
    payload = {
        "text": f"{severity} New HIBP breach affecting {domain}: {breach_name}",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{severity} *New HIBP breach: {breach_name}*\n"
                        + "\n".join(fields)
                        + f"\n<https://haveibeenpwned.com/PwnedWebsites#{breach_name}|Breach details>"
                    ),
                },
            },
        ],
    }
    try:
        r = http.post(SLACK_WEBHOOK, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        log.warning("Slack alert failed: %s", e)


def sync_domain(client: httpx.Client, conn: sqlite3.Connection, domain: str) -> None:
    log.info("Syncing HIBP for %s", domain)
    data = hibp_get(client, f"breacheddomain/{domain}")
    if data is None:
        log.info("No breaches for %s (or domain not verified)", domain)
        return

    now = datetime.now(timezone.utc).isoformat()
    new_breaches: dict[str, int] = {}

    for local_part, breach_names in data.items():
        for breach_name in breach_names:
            row = conn.execute(
                "SELECT id FROM hibp_breaches WHERE domain=? AND local_part=? AND breach_name=?",
                (domain, local_part, breach_name),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE hibp_breaches SET last_seen=? WHERE id=?",
                    (now, row[0]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO hibp_breaches (domain, local_part, breach_name, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (domain, local_part, breach_name, now, now),
                )
                new_breaches[breach_name] = new_breaches.get(breach_name, 0) + 1
    conn.commit()

    for breach_name, count in new_breaches.items():
        sync_breach_details(client, conn, breach_name)
        details = conn.execute(
            "SELECT name, breach_date, added_date, pwn_count, data_classes FROM hibp_breach_details WHERE name=?",
            (breach_name,),
        ).fetchone()
        details_dict = None
        if details:
            details_dict = {
                "name": details[0],
                "breach_date": details[1],
                "added_date": details[2],
                "pwn_count": details[3],
                "data_classes": details[4],
            }
        slack_alert_new_breach(
            client,
            domain=domain,
            breach_name=breach_name,
            affected_count=count,
            breach_details=details_dict,
        )
        log.info("NEW breach %s on %s: %d affected accounts", breach_name, domain, count)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", help="sync a single domain (overrides config)")
    args = parser.parse_args()

    if args.domain:
        domains = [args.domain]
    else:
        if not CONFIG_PATH.exists():
            log.error("Missing config %s", CONFIG_PATH)
            sys.exit(1)
        domains = json.loads(CONFIG_PATH.read_text()).get("domains", [])

    if not domains:
        log.error("No domains configured")
        sys.exit(1)

    conn = init_db(DB_PATH)
    with httpx.Client() as client:
        for d in domains:
            sync_domain(client, conn, d)
            time.sleep(2)
    conn.close()
    log.info("HIBP sync complete")


if __name__ == "__main__":
    main()
