"""
Lookalike domain monitor.

Combines two complementary techniques to catch domain-based brand abuse:

1. DNSTwist-style permutation generation — produces typo, homoglyph, TLD-swap,
   and insertion variants of your brand domains, then resolves each via DNS to
   see which are actually registered.
2. Certificate Transparency monitoring via crt.sh — queries the public CT log
   index for any certificate ever issued matching `%brand%`. New certificates
   are a leading indicator: phishing sites need TLS, so a cert appears 12-72h
   before the phishing page goes live.

Stores all findings in the monitor SQLite database. Alerts on:
- Newly-resolving permutations (registered since last run)
- New CT log entries for brand keywords

Runs as a scheduled job — daily is enough for most use cases.

Setup:
    pip install dnstwist tldextract httpx

DNSTwist usage notes:
- DNSTwist generates thousands of permutations for a long brand name. Only a
  fraction will resolve. Resolving ones are the interesting subset.
- A resolving domain doesn't mean it's malicious — could be parking, defensive
  registration, or coincidence. Alert and let a human triage.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL")
DB_PATH = Path(os.environ.get("MONITOR_DB", "monitor.db"))
CONFIG_PATH = Path(os.environ.get("LOOKALIKE_CONFIG", "lookalike-watchlist.json"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lookalike")

SCHEMA = """
CREATE TABLE IF NOT EXISTS lookalike_domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_domain TEXT NOT NULL,
    candidate_domain TEXT NOT NULL,
    permutation_type TEXT,
    resolves_to TEXT,
    nameservers TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    UNIQUE(brand_domain, candidate_domain)
);

CREATE TABLE IF NOT EXISTS ct_certificates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_keyword TEXT NOT NULL,
    crt_sh_id INTEGER NOT NULL,
    common_name TEXT,
    name_value TEXT,
    issuer_name TEXT,
    not_before TEXT,
    not_after TEXT,
    first_seen TEXT NOT NULL,
    UNIQUE(crt_sh_id)
);

CREATE INDEX IF NOT EXISTS idx_lookalike_brand ON lookalike_domains(brand_domain);
CREATE INDEX IF NOT EXISTS idx_ct_keyword      ON ct_certificates(brand_keyword);
"""


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


# ─── DNSTwist ────────────────────────────────────────────────────────────
def run_dnstwist(domain: str) -> list[dict]:
    """Run dnstwist as a subprocess. Returns the parsed JSON output."""
    log.info("Running dnstwist for %s", domain)
    try:
        result = subprocess.run(
            ["dnstwist", "--format", "json", "--registered", domain],
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        log.error("dnstwist timed out for %s", domain)
        return []
    except FileNotFoundError:
        log.error("dnstwist not installed — pip install dnstwist")
        return []
    if result.returncode != 0:
        log.error("dnstwist failed: %s", result.stderr[:500])
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error("dnstwist returned invalid JSON")
        return []


def sync_dnstwist(http: httpx.Client, conn: sqlite3.Connection, domain: str) -> None:
    candidates = run_dnstwist(domain)
    log.info("dnstwist found %d registered permutations for %s", len(candidates), domain)

    now = datetime.now(timezone.utc).isoformat()
    new_candidates: list[dict] = []
    seen_today: set[str] = set()

    for c in candidates:
        candidate_domain = c.get("domain", "")
        if not candidate_domain or candidate_domain == domain:
            continue
        seen_today.add(candidate_domain)
        resolves_to = ",".join(c.get("dns_a", []) + c.get("dns_aaaa", []))
        nameservers = ",".join(c.get("dns_ns", []))
        perm_type = c.get("fuzzer", "")

        row = conn.execute(
            "SELECT id FROM lookalike_domains WHERE brand_domain=? AND candidate_domain=?",
            (domain, candidate_domain),
        ).fetchone()
        if row:
            conn.execute(
                """
                UPDATE lookalike_domains
                SET last_seen=?, resolves_to=?, nameservers=?, is_active=1
                WHERE id=?
                """,
                (now, resolves_to, nameservers, row[0]),
            )
        else:
            conn.execute(
                """
                INSERT INTO lookalike_domains
                (brand_domain, candidate_domain, permutation_type,
                 resolves_to, nameservers, first_seen, last_seen, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (domain, candidate_domain, perm_type, resolves_to, nameservers, now, now),
            )
            new_candidates.append({
                "domain": candidate_domain,
                "type": perm_type,
                "resolves_to": resolves_to,
            })

    # Mark domains no longer resolving as inactive
    previously_active = {
        r[0] for r in conn.execute(
            "SELECT candidate_domain FROM lookalike_domains WHERE brand_domain=? AND is_active=1",
            (domain,),
        ).fetchall()
    }
    gone = previously_active - seen_today
    for d in gone:
        conn.execute(
            "UPDATE lookalike_domains SET is_active=0 WHERE brand_domain=? AND candidate_domain=?",
            (domain, d),
        )
    conn.commit()

    for c in new_candidates:
        slack_alert_new_lookalike(http, brand=domain, candidate=c)


def slack_alert_new_lookalike(http: httpx.Client, *, brand: str, candidate: dict) -> None:
    if not SLACK_WEBHOOK:
        return
    payload = {
        "text": f":eyes: New lookalike domain registered for {brand}: {candidate['domain']}",
        "blocks": [{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":eyes: *New lookalike registered for `{brand}`*\n"
                    f"*Candidate:* `{candidate['domain']}`\n"
                    f"*Permutation type:* {candidate['type']}\n"
                    f"*Resolves to:* {candidate['resolves_to'] or '_no A/AAAA_'}\n"
                    f"<https://urlscan.io/search/#{candidate['domain']}|Check on urlscan.io>"
                ),
            },
        }],
    }
    try:
        http.post(SLACK_WEBHOOK, json=payload, timeout=10).raise_for_status()
    except Exception as e:
        log.warning("Slack alert failed: %s", e)


# ─── Certificate Transparency via crt.sh ─────────────────────────────────
def query_crt_sh(http: httpx.Client, keyword: str) -> list[dict]:
    log.info("Querying crt.sh for %%%s%%", keyword)
    try:
        r = http.get(
            "https://crt.sh/",
            params={"q": f"%{keyword}%", "output": "json"},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error("crt.sh query failed for %s: %s", keyword, e)
        return []


def sync_ct(http: httpx.Client, conn: sqlite3.Connection, keyword: str) -> None:
    certs = query_crt_sh(http, keyword)
    log.info("crt.sh returned %d certificates for %s", len(certs), keyword)

    now = datetime.now(timezone.utc).isoformat()
    new_certs: list[dict] = []

    for c in certs:
        crt_id = c.get("id")
        if not crt_id:
            continue
        existing = conn.execute(
            "SELECT id FROM ct_certificates WHERE crt_sh_id=?", (crt_id,)
        ).fetchone()
        if existing:
            continue

        name_value = c.get("name_value", "") or ""
        common_name = c.get("common_name", "") or ""
        if keyword.lower() not in name_value.lower() and keyword.lower() not in common_name.lower():
            continue

        conn.execute(
            """
            INSERT INTO ct_certificates
            (brand_keyword, crt_sh_id, common_name, name_value,
             issuer_name, not_before, not_after, first_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                keyword, crt_id, common_name, name_value,
                c.get("issuer_name"), c.get("not_before"), c.get("not_after"), now,
            ),
        )
        new_certs.append(c)

    conn.commit()
    for c in new_certs:
        slack_alert_new_cert(http, keyword=keyword, cert=c)


def slack_alert_new_cert(http: httpx.Client, *, keyword: str, cert: dict) -> None:
    if not SLACK_WEBHOOK:
        return
    name_value = (cert.get("name_value") or "")[:500]
    payload = {
        "text": f":lock: New certificate matching {keyword}",
        "blocks": [{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":lock: *New CT log entry matching `{keyword}`*\n"
                    f"*CN:* `{cert.get('common_name', '')}`\n"
                    f"*Issuer:* {cert.get('issuer_name', '')}\n"
                    f"*Valid from:* {cert.get('not_before', '')}\n"
                    f"*Names:*\n```{name_value}```\n"
                    f"<https://crt.sh/?id={cert.get('id')}|View on crt.sh>"
                ),
            },
        }],
    }
    try:
        http.post(SLACK_WEBHOOK, json=payload, timeout=10).raise_for_status()
    except Exception as e:
        log.warning("Slack alert failed: %s", e)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-dnstwist", action="store_true")
    parser.add_argument("--skip-ct", action="store_true")
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        log.error("Missing config %s", CONFIG_PATH)
        sys.exit(1)

    cfg = json.loads(CONFIG_PATH.read_text())
    domains = cfg.get("domains", [])
    ct_keywords = cfg.get("ct_keywords", [])

    conn = init_db(DB_PATH)
    with httpx.Client() as http:
        if not args.skip_dnstwist:
            for d in domains:
                sync_dnstwist(http, conn, d)
        if not args.skip_ct:
            for kw in ct_keywords:
                sync_ct(http, conn, kw)
                time.sleep(2)
    conn.close()
    log.info("Lookalike sync complete")


if __name__ == "__main__":
    main()
