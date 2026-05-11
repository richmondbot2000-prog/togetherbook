"""
Read the SQLite database produced by hibp_monitor.py + lookalike_monitor.py
and emit a public-safe `security-alerts.json` for the Brandwatch page.

Contents
- hibp.breaches[]   — per-breach summary: domain, breach_name, affected_accounts,
                      pwn_count, breach_date, data_classes. Local-parts NOT
                      exported (they're PII).
- lookalikes[]      — active lookalike domains: brand → candidate, permutation
                      type, first seen, what it resolves to.
- ct_certificates[] — recent CT log matches: brand keyword, CN, issuer, valid-
                      from, names (truncated).

Empty file with the same shape if the DB doesn't exist yet, so the Brandwatch
page can always fetch the file.
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("MONITOR_DB", "monitor.db"))
OUTPUT_PATH = Path(os.environ.get("SECURITY_JSON", "security-alerts.json"))

MAX_LOOKALIKES = 500
MAX_CT_CERTS = 500


def empty_payload(started: datetime.datetime) -> dict:
    return {
        "snapshot_at": started.isoformat(),
        "snapshot_date": started.date().isoformat(),
        "hibp": {"breach_count": 0, "affected_accounts": 0, "breaches": []},
        "lookalikes": {"active_count": 0, "domains": []},
        "ct_certificates": {"count": 0, "recent": []},
    }


def main() -> None:
    started = datetime.datetime.now(datetime.timezone.utc)
    if not DB_PATH.exists():
        OUTPUT_PATH.write_text(json.dumps(empty_payload(started), indent=2))
        print(f"# {DB_PATH} not found — wrote empty {OUTPUT_PATH}", flush=True)
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # ── HIBP breaches ─────────────────────────────────────────────────
    hibp_breaches: list[dict] = []
    affected_total = 0
    try:
        cur.execute(
            """
            SELECT b.domain, b.breach_name, COUNT(b.local_part) AS affected,
                   d.breach_date, d.pwn_count, d.data_classes, d.is_sensitive
            FROM hibp_breaches b
            LEFT JOIN hibp_breach_details d ON d.name = b.breach_name
            GROUP BY b.domain, b.breach_name
            ORDER BY (d.breach_date IS NULL), d.breach_date DESC, b.breach_name
            """
        )
        for domain, name, affected, breach_date, pwn_count, data_classes, is_sensitive in cur.fetchall():
            try:
                classes = json.loads(data_classes) if data_classes else []
            except json.JSONDecodeError:
                classes = []
            hibp_breaches.append({
                "domain": domain,
                "breach_name": name,
                "affected_accounts": int(affected),
                "breach_date": breach_date,
                "pwn_count": int(pwn_count) if pwn_count is not None else None,
                "data_classes": classes,
                "is_sensitive": bool(is_sensitive),
                "url": f"https://haveibeenpwned.com/PwnedWebsites#{name}",
            })
            affected_total += int(affected)
    except sqlite3.OperationalError:
        # tables don't exist yet
        pass

    # ── Active lookalike domains ──────────────────────────────────────
    lookalikes: list[dict] = []
    try:
        cur.execute(
            """
            SELECT brand_domain, candidate_domain, permutation_type,
                   resolves_to, nameservers, first_seen, last_seen
            FROM lookalike_domains
            WHERE is_active = 1
            ORDER BY first_seen DESC
            LIMIT ?
            """,
            [MAX_LOOKALIKES],
        )
        for brand, cand, perm, resolves, ns, first, last in cur.fetchall():
            lookalikes.append({
                "brand": brand,
                "candidate": cand,
                "permutation_type": perm,
                "resolves_to": resolves,
                "nameservers": ns,
                "first_seen": first,
                "last_seen": last,
                "urlscan": f"https://urlscan.io/search/#{cand}",
            })
    except sqlite3.OperationalError:
        pass

    # ── Recent CT certificates ────────────────────────────────────────
    ct_certs: list[dict] = []
    try:
        cur.execute(
            """
            SELECT brand_keyword, crt_sh_id, common_name, name_value,
                   issuer_name, not_before, not_after, first_seen
            FROM ct_certificates
            ORDER BY first_seen DESC
            LIMIT ?
            """,
            [MAX_CT_CERTS],
        )
        for kw, crt_id, cn, names, issuer, nb, na, first in cur.fetchall():
            ct_certs.append({
                "brand_keyword": kw,
                "crt_sh_id": int(crt_id),
                "common_name": cn,
                "name_value": (names or "")[:1000],
                "issuer": issuer,
                "not_before": nb,
                "not_after": na,
                "first_seen": first,
                "url": f"https://crt.sh/?id={crt_id}",
            })
    except sqlite3.OperationalError:
        pass

    output = {
        "snapshot_at": started.isoformat(),
        "snapshot_date": started.date().isoformat(),
        "hibp": {
            "breach_count": len(hibp_breaches),
            "affected_accounts": affected_total,
            "breaches": hibp_breaches,
        },
        "lookalikes": {
            "active_count": len(lookalikes),
            "domains": lookalikes,
        },
        "ct_certificates": {
            "count": len(ct_certs),
            "recent": ct_certs,
        },
    }
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, default=str))
    print(
        f"# wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size:,} bytes); "
        f"{len(hibp_breaches)} HIBP breaches, {len(lookalikes)} lookalikes, {len(ct_certs)} CT certs",
        flush=True,
    )
    conn.close()


if __name__ == "__main__":
    main()
