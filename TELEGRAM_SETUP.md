# Telegram brand-mention monitoring

A read-only collector that watches public Telegram channels for mentions of
**Transform Credit** and **Together Loans**, stores them in a local
SQLite database, and publishes a redacted feed to the Brandwatch page.

## What's in the repo

| Path | What it does |
|---|---|
| `scripts/telegram_monitor.py` | Telethon-based collector. Backfills history and/or streams new messages live. |
| `scripts/scan_telegram.py` | Reads the monitor's SQLite database and writes a public-safe `telegram-mentions.json` (with PII scrubbed). |
| `telegram-watchlist.json` | Channels + keywords. Edit this to change what's monitored. |
| `.github/workflows/refresh-telegram.yml` | Hourly job that runs both scripts and commits the JSON. |
| `telegram-mentions.json` | Published feed consumed by `brandwatch.html`. Committed by the workflow. |
| `monitor.session` (gitignored) | Telethon login cache. Equivalent to a credential. |
| `telegram-monitor.db` (gitignored) | Raw message store. Cached across workflow runs, never committed. |

## One-time setup

You need a Telegram account and API credentials.

### 1. Get a dedicated phone number

**Do not use your personal Telegram account.** Get a separate cell number
(a real SIM, not a VoIP number — Telegram blocks most VoIP).

Install Telegram on a device, register that number, set the display name to
something neutral (e.g. *Brandwatch*), turn on 2FA in Settings → Privacy &
Security → Two-Step Verification, and use a long random password.

### 2. Create a Telegram application

1. Visit <https://my.telegram.org> and log in with that phone number.
2. Open *API development tools*.
3. Create a new application (any name; e.g. `tc-brandwatch`).
4. Note the `api_id` and `api_hash` shown.

### 3. Authenticate locally

```bash
cd /path/to/APIsForKids
python3 -m venv .venv
source .venv/bin/activate
pip install telethon==1.* httpx

export TG_API_ID=123456
export TG_API_HASH=your_api_hash_here

# Edit telegram-watchlist.json first to add the channels you want to monitor.
# Then run a small backfill — Telethon will prompt for the phone number and
# the verification code Telegram sends you.
python scripts/telegram_monitor.py backfill --days 1
```

This produces `monitor.session` (the cached login) and `telegram-monitor.db`
in the working directory. Both are gitignored.

### 4. Upload the session as a GitHub secret

The workflow runs in a fresh container each time, so it can't re-do the
phone-code dance. Encode the session and the credentials as repo secrets.

```bash
base64 -i monitor.session | pbcopy   # macOS
# or: base64 monitor.session > /tmp/sess.b64 && cat /tmp/sess.b64
```

In GitHub → Settings → Secrets and variables → Actions, add:

| Secret name | Value |
|---|---|
| `TG_API_ID` | the numeric `api_id` |
| `TG_API_HASH` | the `api_hash` string |
| `TG_SESSION_B64` | base64 of `monitor.session` |
| `SLACK_WEBHOOK_URL` *(optional)* | incoming-webhook URL for match alerts |

### 5. Trigger the workflow

In GitHub Actions → *Refresh telegram mentions* → *Run workflow*. The first
run will backfill 7 days and commit `telegram-mentions.json`. After that
the cron (`40 6-23 * * *`) keeps it warm hourly.

## Watchlist

`telegram-watchlist.json` is plain JSON:

```jsonc
{
  "channels":        ["public_channel_username", ...],   // no @ prefix
  "keywords":        ["Transform Credit", "togetherloans.com", ...],
  "regex_keywords":  ["transf[o0]rm\\s*cred[i1]t", ...]
}
```

Edit, commit, push. The next workflow run picks it up.

### Finding channels

- <https://tgstat.com> and <https://combot.org> — directories with category
  / keyword search
- <https://lyzem.com> — full-text search across public Telegram channels
- Vendor threat reports (Flashpoint, KELA, Group-IB, Intel 471) name
  channels in their writeups — pull names and add
- Categories useful for a US consumer lender: carding, fullz / PII trading,
  loan-fraud methods, synthetic identity, "money methods", refund fraud,
  account takeover
- Cross-promotion is heavy: once you're in a few, scrape `t.me/` links from
  posts to discover more

## Operational rules

- **Public only.** Joining private/invite-only channels by deception is out
  of scope.
- **Read-only.** Don't post, react, message sellers, or buy anything.
- **Channel churn.** Channels get banned or renamed constantly — refresh
  the watchlist monthly. Remove channels that produce `UsernameNotOccupied`
  or `ChannelPrivate` errors in the workflow logs.
- **Rate limits.** Telethon handles `FloodWaitError` automatically by
  sleeping; large backfills can take a while.
- **PII.** The raw `telegram-monitor.db` contains the full message text and
  is gitignored. The published `telegram-mentions.json` runs every excerpt
  through email / phone / SSN / card-number / ARef-shape redaction before
  writing. If you need stricter masking, edit `scripts/scan_telegram.py`.

## Brandwatch page integration

The page (`brandwatch.html`) fetches both `brandwatch.json` and
`telegram-mentions.json` in parallel and merges them into one feed. Each
Telegram hit becomes a card with:

- Source pill: *Telegram*
- Title: `@channelname on Telegram`
- Body: redacted excerpt
- Brand tag derived from the matched terms (TransformCredit or Together Loans)
- "Read on Telegram" link to the `t.me/channel/message_id` URL

If `telegram-mentions.json` is missing or empty, the page renders normally
with the Telegram pill showing 0.

## Local commands

```bash
# Backfill the last 30 days into the SQLite DB
python scripts/telegram_monitor.py backfill --days 30

# Stream new messages live (Ctrl-C to stop)
python scripts/telegram_monitor.py watch

# Backfill then keep watching
python scripts/telegram_monitor.py both --days 7

# Convert the SQLite DB into the public JSON
python scripts/scan_telegram.py
```

## Threat-model notes (from the spec)

This stack covers free, public sources only. For paid forums, Tor sites, or
invite-only Telegram/Discord servers, the right path is a threat-intel
vendor (Flashpoint, Intel 471, KELA, Searchlight) — they have vetted
analyst access and absorb the legal and operational complexity. Run this
first, see what hits you're missing, then scope a vendor RFP.
