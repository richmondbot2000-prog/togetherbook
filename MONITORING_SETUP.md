# Brand-monitoring stack — setup guide

Four read-only collectors that feed the Brandwatch page:

| Collector | Source | What it catches |
|---|---|---|
| `telegram_monitor.py` | Public Telegram channels | Fraud chatter, stealer logs, leaked credentials, loan-method tutorials |
| `discord_monitor.py` | Public Discord servers | Same, Discord-flavoured ("money methods" servers) |
| `hibp_monitor.py` | Have I Been Pwned domain API | Customer/employee emails appearing in third-party breaches |
| `lookalike_monitor.py` | DNSTwist + crt.sh | Phishing infrastructure being set up against your brand |

All four share `monitor.db` (gitignored). Only the redacted JSON snapshots
(`telegram-mentions.json`, `discord-mentions.json`, `security-alerts.json`)
ever get committed to the repo.

## In-repo files

| Path | What it does |
|---|---|
| `scripts/telegram_monitor.py` / `discord_monitor.py` / `hibp_monitor.py` / `lookalike_monitor.py` | The collectors. |
| `scripts/scan_telegram.py` | Reads `monitor.db` and writes `telegram-mentions.json` + `discord-mentions.json` with PII scrubbed. |
| `scripts/scan_security.py` | Reads `monitor.db` and writes `security-alerts.json` (HIBP breaches + lookalikes + CT certs). |
| `telegram-watchlist.json` / `discord-watchlist.json` / `hibp-watchlist.json` / `lookalike-watchlist.json` | Per-source configs the user edits. |
| `.github/workflows/refresh-telegram.yml` / `refresh-discord.yml` / `refresh-hibp.yml` / `refresh-lookalike.yml` | Scheduled refreshes. Each is dormant until its secrets are configured. |
| `monitor.db` *(gitignored)* | Shared SQLite store. Cached across workflow runs via `actions/cache`. |
| `monitor.session` *(gitignored)* | Telethon login cache. Equivalent to a credential. |

## What's published on the Brandwatch page

- A **Security band** at the top with three counts (HIBP breaches, active
  lookalikes, recent CT certs) and inline lists of the top items.
- Cards in the main feed for every Telegram + Discord brand mention
  (with PII redacted server-side).

## One-time setup per collector

### 1. Telegram

#### a. Dedicated phone number

Get a separate cell number (a real SIM, not VoIP — Telegram blocks most
VoIP). Register Telegram on that number. Turn on 2FA (Settings → Privacy
& Security → Two-Step Verification) with a long random password.
**Do not use a personal account.**

#### b. Create a Telegram application

1. Visit <https://my.telegram.org> and log in with the new phone number.
2. *API development tools* → create application (any name).
3. Note `api_id` and `api_hash`.

#### c. Authenticate locally

```bash
cd /path/to/APIsForKids
python3 -m venv .venv
source .venv/bin/activate
pip install 'telethon==1.*' httpx

export TG_API_ID=123456
export TG_API_HASH=your_api_hash_here

# Add channels to telegram-watchlist.json first.
python scripts/telegram_monitor.py backfill --days 1
```

Telethon prompts for the phone number + SMS code on the first run. It
writes `monitor.session` (gitignored).

#### d. Upload secrets

```bash
base64 -i monitor.session | pbcopy
```

GitHub → Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `TG_API_ID` | numeric api_id |
| `TG_API_HASH` | api_hash string |
| `TG_SESSION_B64` | base64 of `monitor.session` |
| `SLACK_WEBHOOK_URL` *(optional, shared by all four)* | Slack incoming-webhook URL |

Workflow: *Refresh telegram mentions*.

### 2. Discord

Discord's ToS prohibits "self-bots" — user accounts behaving like bots.
Three paths in preference order:

1. **Bot account on servers YOU own/admin** — fully compliant. Set the
   GitHub Actions variable `DISCORD_IS_BOT=true`. Limited to your own
   servers.
2. **Vendor** — Flashpoint et al absorb the ToS exposure.
3. **User-account self-bot** — script default. Accept account-burn risk
   and rotate when banned.

#### a. Get a Discord token

- *Bot account:* in the [Developer Portal](https://discord.com/developers/applications) create an app → Bot → copy the token.
- *User account:* not officially supported; outside the scope of this doc.

#### b. Channel/guild IDs

Discord → Settings → Advanced → Developer Mode → enable. Right-click any
channel or server → *Copy ID*. Add the snowflakes to
`discord-watchlist.json` under `channel_ids` (specific channels) or
`guild_ids` (every readable text channel in that server).

#### c. Upload secrets

| Secret | Value |
|---|---|
| `DISCORD_TOKEN` | bot or user token |
| `DISCORD_IS_BOT` *(GH variable, not secret)* | `true` if running as a bot account |

Workflow: *Refresh discord mentions*.

### 3. HIBP (Have I Been Pwned)

#### a. Verify domain ownership (free)

Visit <https://haveibeenpwned.com/DomainSearch> and follow the prompts
(DNS TXT record or HTML file upload). HIBP will only return data for
domains you've verified.

#### b. API key (paid)

Subscribe at <https://haveibeenpwned.com/API/Key>. Pricing scales by
request rate — cheapest tier is plenty.

#### c. Edit `hibp-watchlist.json`

Add your verified domains. The default already includes
`transformcredit.com` + `togetherloans.com`.

#### d. Upload secrets

| Secret | Value |
|---|---|
| `HIBP_API_KEY` | the key from HIBP |

Workflow: *Refresh HIBP breaches* runs every 6h. HIBP returns affected
local-parts but never passwords; local-parts are not exported to the
public JSON either.

### 4. Lookalike domains

No secret required to actually run. DNSTwist hits public DNS and crt.sh
is a public API. Edit `lookalike-watchlist.json`:

- `domains` — brand domains to permute
- `ct_keywords` — short distinctive strings to search in CT logs

Workflow (`refresh-lookalike.yml`) runs daily at 05:00 UTC. Active
lookalikes (registered + resolving) and new CT certificates appear in
the Security band on the Brandwatch page.

## Operational rules

- **Public only.** Joining private channels by deception is out of scope.
- **Read-only.** Don't post, react, DM, or buy anything.
- **Channel churn.** Expect to refresh watchlists monthly. Remove channels
  that produce `UsernameNotOccupied` or `ChannelPrivate` errors.
- **Rate limits.** Telethon handles `FloodWaitError` automatically; HIBP
  has a built-in 429 retry; crt.sh occasionally 502s — the script logs
  and continues.
- **PII handling.** The raw `monitor.db` contains full message text and
  HIBP local-parts. It's gitignored. The published JSONs run every
  excerpt through email / phone / SSN / card / ARef-shape redaction.
  HIBP local-parts are NEVER exported.

## What this stack does NOT do

- No Tor / onion forums
- No paid or invite-only forums
- No purchase of leaked data
- No engagement with sellers

These are vendor territory (Flashpoint, Intel 471, KELA, Searchlight).
After 90 days of data from this stack you can quantify the coverage gap
and build a vendor business case with real numbers.

## Local commands

```bash
# Telegram
python scripts/telegram_monitor.py backfill --days 30
python scripts/telegram_monitor.py watch
python scripts/telegram_monitor.py both --days 7

# Discord
python scripts/discord_monitor.py backfill --days 30
python scripts/discord_monitor.py watch

# HIBP
python scripts/hibp_monitor.py
python scripts/hibp_monitor.py --domain transformcredit.com

# Lookalike
python scripts/lookalike_monitor.py
python scripts/lookalike_monitor.py --skip-dnstwist  # only CT
python scripts/lookalike_monitor.py --skip-ct        # only DNSTwist

# Export JSONs (run after any of the above)
python scripts/scan_telegram.py   # writes telegram-mentions.json + discord-mentions.json
python scripts/scan_security.py   # writes security-alerts.json
```
