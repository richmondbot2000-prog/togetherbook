# TogetherBOOK — Site Specification

_The source-of-truth document for `togetherbook.net` / `richmondbot2000-prog/APIsForKids`. Lives in this repo so future maintainers find it next to the code. **A successor Claude or engineer should be able to pick this up cold and operate the site competently.**_

**Last reviewed:** 2026-05-12 (overnight rewrite to cover the source-quality analysis, brandwatch email notifications, multi-tenant Directory, and Cloudflare Access policy update)

## Contents

1. [What this is](#1-what-this-is)
2. [URLs and hosting](#2-urls-and-hosting)
3. [Pages](#3-pages)
4. [Visual design — Quiet Edition](#4-visual-design--quiet-edition)
5. [Data refresh pipelines](#5-data-refresh-pipelines)
   - 5.1 [Cron schedules explained](#51-cron-schedules-explained)
6. [Scanner scripts](#6-scanner-scripts)
7. [Database schema reference](#7-database-schema-reference)
8. [External APIs and data sources](#8-external-apis-and-data-sources)
9. [GitHub Actions secrets](#9-github-actions-secrets)
10. [Workspace + GCP setup (Directory page)](#10-workspace--gcp-setup-directory-page)
11. [Specific page details](#11-specific-page-details)
   - 11.1 [Directory page](#111-directory-page-directoryhtml)
   - 11.2 [TopUps page](#112-topups-page-topupshtml)
   - 11.3 [Brandwatch page](#113-brandwatch-page-brandwatchhtml)
   - 11.4 [Yesterday page](#114-yesterday-page-yesterdayhtml)
   - 11.5 [Brokers page + Source-quality analysis](#115-brokers-page--source-quality-analysis-brokershtml)
   - 11.6 [Declines page](#116-declines-page-declineshtml)
   - 11.7 [Pipeline page](#117-pipeline-page-pipelinehtml)
12. [Concept reference: Top-Up Eligibility (TUE)](#12-concept-reference-top-up-eligibility-tue)
13. [Concept reference: Brokers / Sources / Campaigns terminology](#13-concept-reference-brokers--sources--campaigns-terminology)
14. [Brandwatch email notifications](#14-brandwatch-email-notifications)
15. [Dev workflow + procedures](#15-dev-workflow--procedures)
16. [Lessons learned](#16-lessons-learned)
17. [Pending / blocked work](#17-pending--blocked-work)
18. [Cross-references](#18-cross-references)

---

## 1. What this is

A static-hosted internal site (cream paper / ink-blue / brass theme) that explains Richmond Group's `Central Services` lending platform in plain English, plus serves as a live operations dashboard. Data refreshes automatically from the Fabric data warehouse and a few external sources via GitHub Actions; the site is deployed via GitHub Pages.

**Users:** Richmond Group internal staff (the canonical URL is gated behind Cloudflare Access to `@letme.com` Google logins). Originally framed for non-technical staff and onboarding engineers.

---

## 2. URLs and hosting

| What | URL |
|---|---|
| Canonical (gated) | <https://book.togetherbook.net> — Cloudflare Access in front, only `@letme.com` Google accounts pass |
| Apex redirect | <https://togetherbook.net> 301 → `book.togetherbook.net` (Cloudflare Page Rule) |
| Public backdoor | <https://richmondbot2000-prog.github.io/APIsForKids/> — same content, no login. Open by design. Pluggable by going GitHub Pro $4/mo + private source. |
| Source of truth | <https://github.com/richmondbot2000-prog/APIsForKids> (public repo, `main` branch deploys via GitHub Pages) |

**Cloudflare Access setup**: Cloudflare One team `togetherbook` (Free plan). DNS for `book.togetherbook.net` proxied (orange cloud) to GitHub Pages IPs `185.199.108-111.153`. Cloudflare Universal SSL serves HTTPS to users; the GitHub Pages backend stays on HTTP. (We routed around `bad_authz` on Pages' Let's Encrypt for 12+ hours by enabling Cloudflare proxy.)

**Identity provider for the gate**: plain Google IdP (not Workspace). OAuth client lives in the Brandwatch GCP project. Authorised redirect URI: `https://togetherbook.cloudflareaccess.com/cdn-cgi/access/callback`.

**Access policies on app `book`** (configured 2026-05-11, both Allow policies — they OR together):

| Policy | Include | Require | Effect |
|---|---|---|---|
| Letme staff | Email ending in `@letme.com` | — | Lets any Letme employee in from anywhere |
| RG group from office | Email ending in `@transformcredit.com` OR `@togetherloans.com` OR `@rgroup.co.uk` | IP range `62.254.12.244/32` | Lets the wider Richmond Group team in **only from the office static IP** |

To edit: Zero Trust dashboard (`one.dash.cloudflare.com`) → **Access → Applications → `book` → Configure → Policies**.

Identity-provider note for policy 2: all three additional domains (`transformcredit.com`, `togetherloans.com`, `rgroup.co.uk`) are also on Google Workspace, so Google login authenticates them too. No OTP fallback IdP is needed. If those Workspaces ever migrate away from Google, add a one-time-PIN IdP and re-test.

---

## 3. Pages

The site is a flat set of HTML files. **No router, no SPA, no build step.** Each page is a self-contained `.html` file that fetches its own JSON data and renders it inline.

| Page | URL | What it shows | Data file(s) |
|---|---|---|---|
| **Home — About our systems** | `/index.html` | Long-scroll storybook in 7 chapters: hero · 8 helpers · 12 robots · 6 screens · 6 outside askers · loan story · 6 ground rules · 15 commandments | inline (no JSON) |
| **Yesterday's payouts** | `/yesterday.html` | Two Leaflet maps of US borrowers paid out yesterday + per-state breakdown tables | `yesterday-payouts.json` |
| **Brandwatch** | `/brandwatch.html` | Public mentions across 10 sources (Trustpilot, BBB, Reddit, Bluesky, Lemmy, Hacker News, CourtListener, Google News, CFPB, YouTube) | `brandwatch.json` |
| **1stContact** | `/1stcontact.html` | First inbound email per US borrower / GT after payout, 3-month window, redacted PII; word cloud at top | `first-contact.json` |
| **Directory** | `/directory.html` | All 61 letme.* Workspace users + 140 warehouse-only operators who write into the platform but aren't in Workspace; sorted by primary tenant (transform → rgroup-cluster → other → inactive); filterable by tenant + department | `staff.json` + `staff-activity.json` |
| **TopUps** | `/topups.html` | 24-month chart of distinct Transform Credit (LenderId 6) live loans split Primary / Top-Up, with a TUE-eligible-count line overlay; "last refreshed" badge | `topups.json` |
| **Pipeline** | `/pipeline.html` | March-cohort application-pipeline analysis with two d3-sankey diagrams (Lead funnel + Application progression), per-stage drop-off table, and click-to-expand sampled customer timelines per dead-end endpoint. All PII masked server-side. | `pipeline.json` + `pipeline-samples.json` |
| **Brokers** | `/brokers.html` | Per-Broker funnel scorecard PLUS three Source-quality analysis sections — (a) Sources to consider blocking, (b) Blocked Sources to consider re-enabling, (c) Sources where we overpay. KPI band, two top-10 leaderboards (volume + paid), worst-quality leaderboard (ghost rate), sortable table with inline mini-funnel. Click row to expand stage-by-stage detail + top rejection reasons. | `brokers.json` + `source-quality.json` |
| **Declines** | `/declines.html` | 90-day decline-reasons analysis. Lead rejections by `LeadResultTypeId` + application-stage declines from `Flags` (Decline / DNL / Cancelled / FraudRisk). Per flag-type cards with top reasons, daily trend SVG, and ClientType breakdown. | `declines.json` |
| **Schema** | `/database.html` | Full DB schema (renders `database.md` via marked.js + mermaid theme), plus per-table row counts as flipboards | `row-counts.json` + `database.md` |
| **Code** | `/stats.html` | Codebase size dashboard (Solari split-flap digits) + by-language and by-repo tables | inline manual snapshot (live refresh pending Azure access) |
| _(unlinked)_ | `/apis.html` | Per-helper detail page — kept for any deep-link bookmarks; not in nav | inline |
| _(unlinked)_ | `/robots.html` | Per-robot list page — kept for any deep-link bookmarks; not in nav | inline |

**Topbar nav (every page):** `About our systems · Yesterday · Brandwatch · 1stContact · Directory · TopUps · Pipeline · Brokers · Declines · Schema · Code`. Plus a hamburger drawer ≤960px viewport.

---

## 4. Visual design — Quiet Edition

Editorial / antique-book treatment. Originally designed by Claude Design (handoff package archived in `~/Desktop/wiki/TogetherBOOK_handoff/`).

**Token files (load order matters):**
1. `quiet-tokens.css` — palette + font imports (Newsreader/Inter/JetBrains Mono) + spacing scale + motion. Source of truth.
2. `quiet.css` — `qb-*` component CSS: topbar, hero, fleurons, helpers grid, robot rows, commandments, helper detail, chat block.
3. `quiet-extras.css` — site-specific extensions: density overrides, brand logo sizing, hamburger nav, screens/outside grids, story + rules layouts.
4. `quiet-legacy.css` — alias bridge for tool-page inline styles still using `--bg`, `--ink`, `--accent`. Loaded after `style.css` on tool pages so the legacy palette gets remapped to Quiet equivalents.
5. `style.css` — original Futurama-era stylesheet. Kept on tool pages so their page-specific selectors still resolve.

**Palette:**
- Paper: `--paper-50` `#FDFBF4` · `--paper-100` `#FBF6E9` (page) · `--paper-200` `#F5ECD4` · `--paper-300` `#ECDFB6`
- Ink: `--ink-500` `#6B7794` · `--ink-700` `#2C3E66` · `--ink-800` `#1B2A4E` (body) · `--ink-900` `#11192E` (titles)
- Brass: `--brass-300` `#E2BF74` · `--brass-500` `#C8973F` · `--brass-600` `#A47829`
- Manuscript red: `--red-500` `#C0392B` — sparing
- Tags: `--teal-500`, `--sage-500`

**Type:** Newsreader for body + display, Inter for tiny uppercase overlines, JetBrains Mono for code/identifiers/endpoints.

**No `box-shadow` anywhere. No transforms on hover. Only colour + border-colour transitions, 140ms.**

**Density rules** (overrides in `quiet-extras.css`): container max-width 1200, helpers grid 3-col, robots/commandments/story/rules 2-col, halved hero/section paddings, single 16px gutter at `.qb-page`. Per-section side padding is zeroed so headers/cards/text/tool-page sections all align flush.

**Brand:** dot-matrix `togetherbook-logo.png` wordmark in the topbar (transparent PNG, trimmed to glyph bounding box). Heights: 50/42/32 desktop/tablet/phone. Topbar heights: 72/64/56.

**Cache-busting:** every CSS link and the logo `<img src>` carries `?v=<unix-ts>` updated on every push. Without this, GitHub Pages' 600s CDN cache + browser cache hold old CSS for too long during iterative changes. Bump pattern: a small Python regex run inline with each commit (see git history for examples).

---

## 5. Data refresh pipelines

All refresh workflows live in `.github/workflows/refresh-*.yml`. They share a common pattern:

- **Cron:** `<minute> 6-23 * * *` — fires hourly 06:00–23:00 UTC. The 18-attempts-per-day pattern is resilience against GitHub Actions free-tier cron silently skipping under load.
- **Guard:** the first step compares today's date (UTC) against the `snapshot_date` field in the existing JSON output. If they match AND the trigger isn't `workflow_dispatch`, it sets `skip=true` and every subsequent step uses `if: steps.guard.outputs.skip != 'true'` so 17 of 18 firings exit cleanly.
- **Manual trigger always runs:** `workflow_dispatch` bypasses the guard so you can refresh on demand without waiting for tomorrow.
- **Auto-commit + retry-on-rebase:** the final step stages the JSON, commits with a descriptive message, and pushes. If a sibling refresh workflow committed in parallel, the push gets rebased and retried (up to 3 times).

| Workflow | Cadence (UTC) | Output | Source | Auth |
|---|---|---|---|---|
| `refresh-yesterday-payouts.yml` | hourly :00 | `yesterday-payouts.json` | Fabric warehouse | `FABRIC_*` secrets |
| `refresh-row-counts.yml` | hourly :00 | `row-counts.json` | Fabric warehouse | `FABRIC_*` secrets |
| `refresh-brandwatch.yml` | hourly :00 | `brandwatch.json` | Trustpilot, BBB, Reddit, Bluesky, Lemmy, HN, CourtListener, Google News, CFPB, YouTube | `SCRAPERAPI_KEY`, `YOUTUBE_API_KEY` |
| `refresh-1st-contact.yml` | hourly :00 | `first-contact.json` | Fabric warehouse | `FABRIC_*` secrets |
| `refresh-directory.yml` | hourly :00 | `staff.json` | Google Workspace Admin SDK | `WORKSPACE_SERVICE_ACCOUNT_JSON`, `WORKSPACE_DELEGATE_USER` |
| `refresh-staff-activity.yml` | hourly :15 | `staff-activity.json` | Fabric warehouse | `FABRIC_*` secrets |
| `refresh-topups.yml` | hourly :30 | `topups.json` | Fabric warehouse | `FABRIC_*` secrets |
| `refresh-brokers.yml` | hourly :35 | `brokers.json` | Fabric warehouse (`Leads` × `Brokers.Campaigns` × `Brokers.Sources`) | `FABRIC_*` secrets |
| `refresh-declines.yml` | hourly :50 | `declines.json` | Fabric warehouse (`Leads` rejections + `Flags`-via-`Applications`-lender-join) | `FABRIC_*` secrets |
| `refresh-pipeline.yml` | hourly :45 | `pipeline.json` | Fabric warehouse | `FABRIC_*` secrets |
| `refresh-pipeline-samples.yml` | hourly :50 | `pipeline-samples.json` | Fabric warehouse (PII-masked output) | `FABRIC_*` secrets |
| `refresh-source-quality.yml` | daily 07:05 | `source-quality.json` | Fabric warehouse (heavy join over 60d of Leads) | `FABRIC_*` secrets. Daily not hourly — analysis takes ~3-5 min and the underlying signal is stable over a day. 45-min timeout configured. |
| `refresh-telegram.yml` | hourly :40 | `telegram-mentions.json` | Public Telegram channels via Telethon | `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_B64` (dormant until set) |
| `refresh-discord.yml` | hourly :45 | `discord-mentions.json` | Public Discord servers via discord.py | `DISCORD_TOKEN` (dormant until set) |
| `refresh-hibp.yml` | every 6h :20 | `security-alerts.json` (hibp section) | Have I Been Pwned domain API | `HIBP_API_KEY` (dormant until set) |
| `refresh-lookalike.yml` | daily 05:00 | `security-alerts.json` (lookalikes + ct sections) | DNSTwist + crt.sh | no secret required (gated on watchlist having domains) |

Schedules are deliberately staggered (`:00`, `:15`, `:30`, `:35`, `:40`, `:45`, `:50`) so simultaneous warehouse-heavy queries don't pile up.

### 5.1 Cron schedules explained

GitHub Actions schedules are written in classic 5-field cron syntax (UTC):

```
┌───────── minute (0 - 59)
│ ┌───────── hour (0 - 23)
│ │ ┌───────── day of month (1 - 31)
│ │ │ ┌───────── month (1 - 12)
│ │ │ │ ┌───────── day of week (0 - 6)
│ │ │ │ │
0 6-23 * * *   →  every hour at minute 0, from 06:00 through 23:00 UTC, every day
15 6-23 * * *  →  every hour at :15, from 06:00 through 23:00 UTC, every day
30 6-23 * * *  →  every hour at :30, same window
```

**Why hourly with a guard, not a single daily slot?** GitHub Actions' free-tier cron is best-effort — under high load on the runner pool, individual scheduled firings get silently dropped. A single daily 02:00 slot might miss days. The 18-firings-per-day pattern + a same-day guard means: the FIRST run that succeeds after 06:00 UTC writes the day's snapshot; the next 17 see `snapshot_date == today` and exit cleanly. Cost is negligible (each guard-skip is a 5-second job).

**Why minute staggering?** All Fabric-warehouse-bound workflows pay the ODBC connection setup tax (~10s) and run heavy aggregations. Running three of them on the :00 minute hits warehouse throughput and slows them all down. Spreading to :00 / :15 / :30 amortises the load.

**Time zone note:** GitHub Actions cron is always in UTC, never in the runner's local TZ. The 06:00–23:00 window roughly maps to "fully covered by the time anyone's awake in London" while leaving 00:00–05:00 UTC quiet (which is overnight in the UK and late-evening to early-morning across the US).

**Manual triggering** (any workflow): `gh workflow run <name>.yml --repo richmondbot2000-prog/APIsForKids`. The `workflow_dispatch` trigger bypasses the guard so the run always does work, useful for forcing a fresh snapshot after a column-name fix or a data-source change.

---

## 6. Scanner scripts

Each refresh workflow runs one Python script under `scripts/`. They all read env vars from the workflow step (which loads them from GH secrets) and write a single JSON file at the repo root.

| Script | Reads | Writes | Notes |
|---|---|---|---|
| `scan_row_counts.py` | INFORMATION_SCHEMA + `COUNT_BIG(*)` on every table in 10 reporting DBs | `row-counts.json` | Threadpool parallel by database; 240s per-DB timeout; surfaces databases that timed out |
| `scan_yesterday_payouts.py` | `Loanbook.LoanAtInception` filtered to yesterday's `LoanAgreementDate`, joined to lender/state lookups | `yesterday-payouts.json` | Drops PII; returns city/state/amount only |
| `scan_brandwatch.py` | 10 source fetchers — see `fetch_*` in the file | `brandwatch.json` | Each source independently caught and reported in `source_status`; uses ScraperAPI for residential-IP fetches against Trustpilot/BBB/Reddit |
| `scan_first_contact.py` | `Communications.Messages` joined to `Loanbook.Loan` | `first-contact.json` | 90-day window; PII-redacted snippets; word cloud source |
| `scan_directory.py` | Workspace Directory API `users.list` with `customer='my_customer'` | `staff.json` | Auth via service account `directory-reader@letme-directory.iam.gserviceaccount.com` impersonating `james.benamor@letme.co.uk`; covers all alias domains |
| `scan_staff_activity.py` | `ClientUsername` columns across 7 reporting DBs in a 60-day window | `staff-activity.json` | Strict `local-part@<known-domain>` matching against `staff.json` to attribute writes; emits `external_users` for unmatched email-shaped usernames; outputs `primary_tenant` for sort ordering |
| `scan_topups.py` | `Loanbook.Loan_History` (~197M rows) JOINed to `Loan` (LenderId) and `LoanAtInception` (TopUpAmountAtInception) | `topups.json` | One CTE-grouped query per month over 24 months; live filter: DIA<90 + balance>$10; classifies primary vs top-up; pulls TUE thresholds for the lender |
| `scan_pipeline.py` | `Applications` (March cohort) × `Leads` × `Tasks` | `pipeline.json` | Lead-result enum aggregated for the Sankey funnel; strict-stage definitions per TaskTypeId + GtRef; produces the data for the two-Sankey diagram on the Pipeline page |
| `scan_pipeline_samples.py` | Same tables + `Customers` + `Addresses` + `ESignatures` + `WebBehaviours` + `Communications.Messages` + `Brokers.Sources` + `Brokers.Campaigns` + `LoanPurposes` + `LeadResultTypes` + `TaskTypes` + `BrokerStatuses` | `pipeline-samples.json` | Per dead-end endpoint: 25 random ARefs with full interaction timeline. Identity-links by (FirstName, Surname, DOB) so the timeline spans every application the same person made. All PII masked server-side: ARefs to last-5, surnames to ******, DOB to ****-**-**, addresses to state only, plus email / phone / SSN / card-number redaction in message bodies. |
| `scan_brokers.py` | `Leads` × `Brokers.Campaigns` × `Brokers.Sources` × `Applications` × `Tasks` × `BrokerStatuses` | `brokers.json` | Server-side CTE aggregation over 90-day window — never pulls multi-million-row Leads to the runner. Per source: 7-stage funnel + average loan + paid-out $ + top-3 rejection reasons. Tunable via `BROKER_WINDOW_DAYS` / `BROKER_LENDER_ID` env vars. |
| `scan_declines.py` | `Leads` (LeadResultTypeId) + `Flags` joined to `Applications` for lender filter | `declines.json` | 90-day window. Reason text is whitespace + case + trailing-period normalised before group-by so 'BANK CHECK FAILED' and 'bank check failed.' fold into the same bucket. Emits top reasons per flag type + daily trend per type + ClientType breakdown + top-20 ClientUsernames raising declines. |
| `scan_source_quality.py` | `Leads` × `Brokers.Sources` × `Brokers.Campaigns` × `dbo.SourceTypes` × `Applications` | `source-quality.json` | **The heaviest scanner.** Three analyses keyed on **(Broker, SourceReference1)** cells over a 60-day window ending 30 days ago (maturation lag). See §11.5 for the full spec — too detailed to compress here. Tunables: `SQ_WINDOW_DAYS`, `SQ_MATURATION_DAYS`, `SQ_BOUNCEBACK_WINDOW_DAYS`, `SQ_MIN_VOLUME`, `SQ_MIN_EXCLUDED`, `SQ_SAMPLE_PER_CAMPAIGN`, `SQ_NULL_SR1_SAMPLE`. |
| `diff_brandwatch_mentions.py` | `brandwatch.json` + `brandwatch-seen.json` | `notify-mentions.json` + updated `brandwatch-seen.json` | Tracks already-notified mention IDs. Filters out sources `bbb` and `reviewcentre` before notification. See §14 for the email integration. |
| `scan_telegram.py` + `telegram_monitor.py` + `discord_monitor.py` | Public Telegram channels (Telethon) + public Discord servers (discord.py), read-only on dedicated accounts → shared `monitor.db` → public-safe `telegram-mentions.json` + `discord-mentions.json` | `telegram-mentions.json`, `discord-mentions.json` | Match lists in `telegram-watchlist.json` / `discord-watchlist.json`. Excerpts go through email / phone / SSN / card / ARef-shape redaction before being written. Workflows dormant until secrets configured. |
| `scan_security.py` + `hibp_monitor.py` + `lookalike_monitor.py` | Have I Been Pwned domain API + DNSTwist permutation generator + crt.sh CT log searches | `security-alerts.json` | HIBP breach counts/domains (never local-parts), active lookalike domains, recent CT certificates. Each collector's workflow is dormant until its secrets / config are set. |

Common patterns:
- **Schema discovery via `INFORMATION_SCHEMA.COLUMNS`** rather than hardcoded column names — the warehouse has multiple vintages and column names drift (`TUEMaxBalance` vs `TUE_MaxBalance`).
- **Connection string:** Active Directory Service Principal auth via `pyodbc` ODBC Driver 18, secrets from env.

---

## 7. Database schema reference

All site data comes from **one** database system: the **Fabric data warehouse**, which mirrors the Central Services production OLTP databases nightly. We never read from production directly — the warehouse is the agreed read target for analytics workloads.

### 7.1 Connection details

| | Value |
|---|---|
| Endpoint | `d76gbn5gtayeppltcrsxsvkluq-ruqkpz7fkwpelmnjh4kbg2lqpi.datawarehouse.fabric.microsoft.com` |
| Tenant ID | `b760fc1f-98a6-4730-bd73-146579554ba4` |
| Client ID | `b8fb2709-6e42-4a1e-91e6-8a3807a9884d` |
| Client secret | `${{ secrets.FABRIC_CLIENT_SECRET }}` |
| Auth | Active Directory Service Principal |
| Driver | ODBC Driver 18 for SQL Server (`pyodbc`) |
| Port | 1433 (TLS, `Encrypt=yes`) |

The non-secret values are inline in every workflow's `env:` block; only `FABRIC_CLIENT_SECRET` lives in GH Actions secrets.

### 7.2 Reporting databases (10 total)

The warehouse exposes one reporting database per Central Services source (per `scripts/scan_row_counts.py`):

| Database | Mirrors | Notable tables we read |
|---|---|---|
| `ReportingApplications` | `Central Applications` | `Applications`, `Customer`, `ApplicationHistory`, `Events` |
| `ReportingBrokers` | `Central Brokers` | `Sources`, `Events` |
| `ReportingCentralCrm` | `Central CRM` | `Lenders`, `Endpoints`, `Events`, `Tasks` |
| `ReportingCommunications` | `Central Communications` | `Messages`, `Events` |
| `ReportingCreditbuilder` | `Central CreditBuilder` | (we don't currently read from this) |
| `ReportingLoanbook` | `Central Loanbook` | `Loan_History`, `LoanAtInception`, `Loan`, `Transactions`, `Customer`, `Events` |
| `ReportingLookup` | shared lookup tables | (lender / state / product-type lookups) |
| `ReportingPayments` | `Central Payments` | `Events` |
| `ReportingTracking` | activity tracking | (we don't currently read) |
| `Whitebox` | TL White Box | `Events` |

### 7.3 Tables read by each scanner

This is the canonical reference for "where does the data on each page come from".

| Page → JSON file | Tables read | Notes |
|---|---|---|
| Schema → `row-counts.json` | `INFORMATION_SCHEMA.TABLES` + `COUNT_BIG(*)` on every user table in all 10 reporting DBs | Per-DB threadpool with 240s timeout; surfaces databases that timed out separately |
| Yesterday → `yesterday-payouts.json` | `ReportingLoanbook.dbo.LoanAtInception` filtered to `LoanAgreementDateLocal = yesterday`; joined to `ReportingLoanbook.dbo.Customer` and lender / state lookups | LenderId 6 only (Transform Credit / USA); strips PII; emits city/state/amount only |
| 1stContact → `first-contact.json` | `ReportingCommunications.dbo.Messages` (`Description = 1` = inbound email) joined to `ReportingLoanbook.dbo.Loan` for the payout date; per-loan first-inbound within 90 days post-payout | PII redacted in snippets; word cloud derived from snippets |
| Directory → `staff.json` | _Not from Fabric_ — Workspace Admin SDK Directory API | See §8.3 |
| Directory → `staff-activity.json` | `ClientUsername` columns across `ReportingApplications`, `ReportingBrokers`, `ReportingCentralCrm`, `ReportingCommunications`, `ReportingLoanbook`, `ReportingPayments`, `Whitebox` (any table with both `ClientUsername` and a datetime column, discovered via `INFORMATION_SCHEMA`) | 60-day window; matches against `staff.json` emails using strict `local-part@<known-domain>` join |
| TopUps → `topups.json` | `ReportingLoanbook.dbo.Loan_History` (~197M rows) JOINed to `Loan` (for `LenderId` filter) and `LoanAtInception` (for `TopUpAmountAtInception` flag and TUE thresholds) | Single CTE-grouped query bucketing by month for 24 months; live filter `DIA<90 + balance>$10` |

### 7.4 Key columns in tables we read

| Table | Column | Notes |
|---|---|---|
| `Loan_History` | `LoanbookId` | FK to `Loan` and `LoanAtInception` |
| | `DateTimeUTC` / `DateTimeLocal` | Snapshot timestamp |
| | `DateInArrearsUTC` / `DateInArrearsLocal` | NULL if not in arrears; populated when a loan first goes into arrears. DIA is computed inline as `DATEDIFF(day, DateInArrearsUTC, DateTimeUTC)` |
| | `CurrentBalance` | Decimal balance at snapshot |
| | `Arrears` | Decimal amount in arrears |
| | `TueStatus` | BIT — true if the loan was top-up eligible at this snapshot |
| | `TransactionID` | Non-null only on rows generated by Mini Update from a transaction; null on Daily Update snapshot rows |
| `LoanAtInception` | `LoanbookId` | One row per loan; immutable post-payout |
| | `LenderId` | _Not present_ — use the `Loan` table to filter by lender |
| | `LoanAgreementDate` / `LoanAgreementDateLocal` | The "loan starts" date |
| | `Originator` | `'TopUp'` for top-up loans (also detectable via `TopUpAmountAtInception`) |
| | `TopUpAmountAtInception` | Decimal — net cash to customer above settling the old loan; NULL on primary loans |
| | `TUE_MaxBalance` / `TUE_ArrearsPosition` / `TUEDaysOld` | Per-loan TUE thresholds (note `TUE_*` underscore variant on the warehouse, not the wiki's `TUE*`) |
| `Loan` | `LoanbookId` | One row per loan, mutable current-state mirror |
| | `LenderId` | INT FK; `6 = Transform Credit / Together Loans / USA` |
| | `TUE_MaxBalance` / `TUE_ArrearsPosition` / `TUEDaysOld` | Same per-loan thresholds, also stored here |
| `Messages` | `LoanbookId`, `ARef`, `GtRef` | Conversation keys |
| | `Description` | INT enum: `0=InboundSms, 1=InboundEmail, 2=InboundCall`. Filter on `= 1`, not `= 'InboundEmail'` |
| | `DateTimeUTC` | When the message was received |
| `Applications` | `ARef` | 22-char alphanumeric customer identifier; stable across re-applications via de-dupe |
| | `TueStatus` | BIT — true if THIS application IS a top-up |
| | `TuePayOff` | Decimal — existing-loan balance to settle |
| (everything) | `ClientType`, `ClientUsername` | Audit fields on every write — commandment #2 |

The full schema lives in `database.md` (rendered in the live site under `/database.html` with Mermaid ER diagrams).

### 7.5 Gotchas

- **`ClientUsername` shape varies:** per-tenant agent emails (`*@transformcredit.com`, `*@lendingmate.ca`, etc.), legacy internal `*@rgroup.co.uk`, robot identifiers (`Migrator`, `Postman`, `auto-collect-cards`), and bare first names (`Ed`, `Sophie`). For the directory enrichment we match strict `local-part@<known-domain>` only.
- **`Loanbook.Customer.Surname`, not `LastName`.**
- **`LoanbookId` is alphanumeric** (`010100AQUA`), don't `int()` it.
- **`Lenders.Name` doesn't exist** — hard-map `LenderId → label` in code.
- **`Loanbook.Customer.GtRef` doesn't exist** — `GtRef` is only on `Messages`.

---

## 8. External APIs and data sources

### 8.1 Fabric data warehouse

Covered in §7. ~99% of all data on the site comes from here.

### 8.2 ScraperAPI (residential proxy)

Used only by `scripts/scan_brandwatch.py` to fetch sources that 403 from cloud IPs.

| | Value |
|---|---|
| Base URL | `https://api.scraperapi.com/` |
| Auth | API key in query string (`?api_key=…`) |
| Secret | `${{ secrets.SCRAPERAPI_KEY }}` |
| Tier rules (verified 2026-05-07/08) | `basic` (1 credit) — country-coded residential IP, works for plain JSON like Reddit's `.json`. `render` (10 credits) — `&render=true`, Trustpilot needs this (Cloudflare JS challenge). `premium` (25 credits) — `&premium=true`, BBB needs this **but breaks if you also pass render**. |
| Used for | Trustpilot, BBB, Reddit search |

### 8.3 Google Workspace Admin SDK (Directory API)

Used only by `scripts/scan_directory.py`.

| | Value |
|---|---|
| API | `https://admin.googleapis.com/admin/directory/v1/users` |
| SDK | `google-api-python-client` (`build('admin', 'directory_v1', …)`) |
| Auth | Service-account credentials with domain-wide delegation, impersonating a Workspace super-admin |
| Service account | `directory-reader@letme-directory.iam.gserviceaccount.com` (in GCP project `letme-directory` under org `letme.co.uk`) |
| OAuth client ID | `116293508437634653191` (registered in `admin.google.com` Domain-Wide Delegation) |
| Scopes | `https://www.googleapis.com/auth/admin.directory.user.readonly`, `https://www.googleapis.com/auth/admin.directory.group.readonly` |
| Impersonates | `james.benamor@letme.co.uk` |
| Query | `users().list(customer='my_customer', maxResults=500, orderBy='email', projection='full', viewType='admin_view')` paginated via `nextPageToken` |
| JSON key | `${{ secrets.WORKSPACE_SERVICE_ACCOUNT_JSON }}` (full JSON file contents); local archive at `~/Desktop/wiki/letme-directory-f8cf5d0a941f.json` (gitignored) |
| Delegate user | `${{ secrets.WORKSPACE_DELEGATE_USER }}` = `james.benamor@letme.co.uk` |
| Rate limits | Workspace Directory API quota is per-customer; we use a few hundred units/day for the daily refresh — well below the 2400 read units/100s limit |

### 8.4 YouTube Data API v3

Used only by `scripts/scan_brandwatch.py` for the YouTube source.

| | Value |
|---|---|
| Base URL | `https://www.googleapis.com/youtube/v3/search` |
| Auth | API key in query string (`?key=…`) |
| Secret | `${{ secrets.YOUTUBE_API_KEY }}` |
| Daily quota | 10,000 units (one search costs 100 units) — we do ~2 searches per brand per refresh, so ~4 searches/day = 400 units. Well under quota. |

### 8.5 Other public sources (no auth)

Used by `scripts/scan_brandwatch.py`, all read-only HTTP GETs:

| Source | Endpoint | Notes |
|---|---|---|
| Bluesky | `https://api.bsky.app/xrpc/app.bsky.feed.searchPosts` | Public unauth host (the previous `public.api.bsky.app` retired in 2025) |
| Lemmy (lemmy.world) | `https://lemmy.world/api/v3/search` | Public unauth |
| Hacker News (Algolia) | `https://hn.algolia.com/api/v1/search` | Public unauth |
| CourtListener | `https://www.courtlistener.com/api/rest/v3/search/` | Public unauth |
| Google News (RSS) | `https://news.google.com/rss/search` | Public unauth, RSS feed |
| CFPB Consumer Complaint DB | `https://www.consumerfinance.gov/data-research/consumer-complaints/search/api/v1/` | Public unauth |

### 8.6 Cloudflare (no API calls; only DNS / Access config)

The Cloudflare side of the gating is provisioned manually:
- DNS: 4 A records `book.togetherbook.net → 185.199.108-111.153` (GitHub Pages IPs), proxied (orange cloud)
- Access: app `book` covering `book.togetherbook.net`, single policy `Letme staff` = Email ending in `@letme.com`
- Identity provider: plain Google IdP, OAuth client in the Brandwatch GCP project

There's no Cloudflare API integration. If we ever need one, the old DNS-flip token is at `~/.cf-token-togetherbook` (can be deleted; it completed its job).

### 8.7 GitHub (deploy + secrets)

| | Value |
|---|---|
| Repo | `richmondbot2000-prog/APIsForKids` (public) |
| Auth on dev machine | `gh` CLI logged in as `richmondbot2000-prog` (token in macOS keychain) |
| Pages source | `main` branch, root |
| Action runner | GitHub-hosted `ubuntu-latest` |
| Secrets list | `gh secret list --repo richmondbot2000-prog/APIsForKids` |

---

## 9. GitHub Actions secrets

| Secret | Used by | What it is |
|---|---|---|
| `FABRIC_CLIENT_SECRET` | all warehouse-bound workflows (row-counts, yesterday-payouts, 1st-contact, staff-activity, topups, pipeline, pipeline-samples, brokers, declines, source-quality) | Service-principal client secret for the Fabric warehouse |
| `SCRAPERAPI_KEY` | brandwatch | ScraperAPI residential-proxy API key (for Trustpilot / BBB / Reddit which 403 cloud IPs) |
| `YOUTUBE_API_KEY` | brandwatch | YouTube Data API v3 key |
| `WORKSPACE_SERVICE_ACCOUNT_JSON` | directory | Full JSON key for `directory-reader@letme-directory.iam.gserviceaccount.com` (Google Cloud SA with domain-wide delegation) |
| `WORKSPACE_DELEGATE_USER` | directory (legacy single-tenant path) | `james.benamor@letme.co.uk` — the Workspace super-admin the SA impersonates. Retained as a fallback when `WORKSPACE_TENANTS` is unset. |
| `WORKSPACE_TENANTS` | directory (multi-tenant path) | JSON array, one entry per Workspace tenant. Schema: `[{"name":"letme","delegate":"james.benamor@letme.co.uk","domain":"letme.co.uk"},{"name":"rgroup","delegate":"ben.gardner@rgroup.co.uk","domain":"rgroup.co.uk"}]`. Each tenant must have DWD enabled and the SA Client ID (`116293508437634653191`) plus user.readonly scope authorised in that Workspace's Admin Console. |
| `SMTP_USERNAME` | brandwatch email | `noreply@togetherbook.net` — the Workspace user that sends new-mention notification emails. See §14. |
| `SMTP_PASSWORD` | brandwatch email | 16-character Gmail App Password for the `noreply@togetherbook.net` account. Generated at `myaccount.google.com/apppasswords` while signed in as that user; requires 2-Step Verification turned on. |

Non-secret values (`FABRIC_SQL_ENDPOINT`, `FABRIC_TENANT_ID`, `FABRIC_CLIENT_ID`) are inline in workflow `env:` blocks.

---

## 10. The Workspace + GCP setup (Directory page)

The Directory page is the most involved data dependency. Setup steps, recorded for posterity:

1. **Workspace tenant:** `letme.co.uk` with alias domains (`letme.com`, `rapida.bg`, `clearloans.com.au`, etc.). Most users have `@letme.com` primary emails — query with `customer='my_customer'`, never `domain=`.
2. **Google Cloud project:** `letme-directory` in the `letme.co.uk` org.
3. **Admin SDK API:** enabled on that project.
4. **Service account:** `directory-reader@letme-directory.iam.gserviceaccount.com`. JSON key generated once, archived locally at `~/Desktop/wiki/letme-directory-f8cf5d0a941f.json` (gitignored), uploaded to GH as `WORKSPACE_SERVICE_ACCOUNT_JSON`.
5. **Domain-Wide Delegation** in `admin.google.com → Security → Access and data control → API controls → Manage Domain-Wide Delegation`:
   - Client ID: `116293508437634653191`
   - Scopes: `https://www.googleapis.com/auth/admin.directory.user.readonly,https://www.googleapis.com/auth/admin.directory.group.readonly`
   - **Workspace quirk:** single-scope adds were silently rejected by `admin.google.com` with the misleading error `Can't add OAuth client X with 1 scope`. Adding two scopes at once let the entry land. The unused second scope is harmless.

---

## 11. Specific page details

### 11.1 Directory page (`directory.html`)

Merges two JSON files:
- `staff.json` — every Workspace user (61 in May 2026)
- `staff-activity.json` — per-user 60-day warehouse activity (writes_60d, last_active_utc, tenants[], primary_tenant, top_warehouse) plus a parallel list of `external_users` (warehouse-only people not in Workspace)

**Sort order:**
1. Has activity? (active first, then inactive Workspace)
2. Within active: `primary_tenant` priority — `transform` / `together` (rank 0), `rgroup` / `rgdc` / `letme` (rank 1), everything else (rank 2)
3. Within tenant: `writes_60d` desc
4. Inactive Workspace accounts at the end, alphabetical by name

**Identity matching strategy** (in `scan_staff_activity.py`):
- Strict: `local-part@<known-domain>` only. Known domains: `rgroup.co.uk`, `letme.co.uk`, `letme.com`, `transformcredit.com`, `togetherloans.com`, `lendingmate.ca`, `rapida.bg`, `rapidamoney.pl`, `clearloans.com.au`, `fianceo.com`, `tandolan.dk`, `tandolaina.fi`, `tando.dk`.
- Bare-name matches (`Ed`, `Sophie`, `Igor`) deliberately excluded — would collide across staff sharing first names.
- Aliases consolidated in `short_tenant()`: `togetherloans` → `transform`, `tandolan/tandolaina/tando` → `tandolan`, `rapidamoney` → `rapida`.

**Card variants:**
- Workspace staff: full photo + name + title + department + email + activity block
- Warehouse-only: dashed-border placeholder photo, derived display name from local-part, italic "no Workspace account" tag, activity block

### 11.2 TopUps page (`topups.html`)

Renders one chart and one table from `topups.json`.

**Definitions** (used in queries inside `scan_topups.py`):
- **Live loan** = LoanHistory snapshot where `(DateInArrearsUTC IS NULL OR DATEDIFF(day, DateInArrearsUTC, DateTimeUTC) < 90) AND CurrentBalance > 10`. The snapshot's own date is used so DIA-as-of-snapshot is correct for back-dated rows.
- **Primary loan** = a loan where `LoanAtInception.TopUpAmountAtInception IS NULL`. The customer's first loan.
- **Top-up loan** = `TopUpAmountAtInception IS NOT NULL`. The customer already had a live loan that was settled at this loan's payout.
- **Top-up eligible** (`TueStatus = 1` on a snapshot) = the live loan met the lender's TUE thresholds at the time the snapshot was written. Recalculated nightly by Daily Update + on every transaction by Mini Update + on every Whitebox run.

**Lender filter:** `LENDER_ID = 6` — Transform Credit / Together Loans (USA). Hardcoded in `scan_topups.py`. The script auto-discovers which warehouse table holds both `LoanbookId` AND `LenderId` columns (currently `Loan`) and uses it in a CTE pre-filter.

**Chart:** stacked SVG bars (ink-300 primary on the bottom, brass-300 top-up on top) plus a manuscript-red line tracking the TUE-eligible count. Hand-rolled SVG, no chart-lib dependency. ~5KB inline.

**Last-refreshed badge:** prominent cream paper plate near the top of the page, showing the snapshot timestamp in friendly format. Goes red if the snapshot is more than 36 hours old (allows a single missed overnight run).

### 11.3 Brandwatch page (`brandwatch.html`)

10 sources, each fetched independently; if a source fails the page shows an amber warning bar listing the broken sources but still renders the rest.

**Known sources blocked from cloud IPs:** Trustpilot (Cloudflare WAF; we use ScraperAPI render tier), BBB (also Cloudflare; ScraperAPI premium tier; **breaks if you also pass render**), Reddit (anonymous .json endpoint 403s; OAuth code path is wired and ready but Reddit's developer registration is impossible to complete via Google sign-in — abandoned).

**Brand precision filter:** every brand has a `precision_terms` allowlist; mentions must contain at least one to survive. `transform_credit` is intentionally strict (`transformcredit` and `transformcredit.com` only) because the two-word "Transform Credit" verb-phrase pollutes Google News results — fintech writing about "transforming credit agreement onboarding" passes any reasonable contextual gate.

### 11.4 Yesterday page (`yesterday.html`)

Two Leaflet maps wrapped with `position: relative; z-index: 0` so Leaflet's internal pane z-indexes (200–800) stay clamped and don't escape over the topbar / mobile drawer. Per-state breakdown tables below.

### 11.5 Brokers page + Source-quality analysis (`brokers.html`)

The single most complex page on the site. It overlays TWO data sources:

1. **`brokers.json`** — per-Broker 90-day funnel scorecard (from `scan_brokers.py`)
2. **`source-quality.json`** — three analytical recommendations + ghost-rate leaderboard signal (from `scan_source_quality.py`)

**Read the terminology section (§13) BEFORE editing anything on this page.** Confusing "Broker" / "Source" / "Campaign" will silently produce wrong recommendations.

#### 11.5.1 Layout, top to bottom

1. **KPI band** (6 tiles): Leads presented · Bought · Applications · Paid out · Lead→paid ratio · Funded $ total
2. **Three top-10 leaderboards side-by-side** (added third 2026-05-12): **Lead volume**, **Paid-out loans**, **Cost per loan** (cheapest first). The cost leaderboard aggregates per-broker total spend from `source-quality.json`'s `by_broker_source[]` (sum of total_cost across the broker's SR1 cells), divides by paid_out. PPC and Organic excluded (no per-lead price). Brokers below $50 total spend filtered as data noise. Each leaderboard has a **"Show all sources" expander** that toggles to the full ranked list.
3. **Outcome distribution** (full-width stacked-bar chart, replaced the "highest ghost rate" leaderboard on 2026-05-12) — every active broker's leads_purchased pool stacked by deepest stage reached. Mutually-exclusive buckets summing to 100%: Ghost (no Apply 1) · Apply 1 · BRW signed · GT accepted · VC Ready · Paid out. Colours: red → brass → sage gradient. Clickable legend: tap any stage to sort by that stage's share descending; tap again to flip ascending. Default sort = Ghost descending (worst first). Top 10 by default with a "Show all sources" expander. **No volume gate, no broker-type filter** — includes Organic/direct/Unknown source per user direction. The old single-metric ghost-rate leaderboard collapsed too much information; this chart shows the full distribution and the user picks the slice that matters.
4. **Source-quality intro panel** (added 2026-05-12) — names the shared frame for all four sections below: (Broker, SourceReference1) cell, 60-day window ending 30 days ago, bounceback cap. Surfaces analysis-run timestamp and an `STALE` red badge if the daily refresh missed (snapshot >36h old). Includes a **broker-name filter** that applies to all four sections in sync; filter value persists via `localStorage` so it survives hourly page refreshes.
5. **Best value Sources — buy more here** (added 2026-05-12) — positive mirror of "consider blocking." Top 15 cells by LOWEST cost-per-paid-loan; styled `is-good` (green). Same volume gate as the blocking list.
6. **Sources to consider blocking** (left column of the recommend-grid, from `source-quality.json`)
7. **Blocked Sources to consider re-enabling** (right column, from `source-quality.json`) — added 2026-05-12: each row carries a `≈ $X originated via fallback` figure (paid bouncebacks × cohort average paid-loan amount, pulled from `brokers.json` totals)
8. **Sources where we overpay** (full-width section below the grid, from `source-quality.json`)
9. **Per-Broker sortable table** with click-to-expand rows showing stage-by-stage retention + top decline reasons

#### 11.5.2 The three source-quality recommendations

All three are aggregated by **(Broker, SourceReference1)** — the user's "Source" granularity (see §13). Window is **60 days ending 30 days ago** so paid-out outcomes have time to mature. CPC/PPC campaigns (`CommissionType=3`) are excluded from analysis since they aren't broker leads. Cells where `SourceReference1` is null/blank are filtered out (not actionable — you can't re-enable "Broker X's no-Source code").

**(a) Sources to consider blocking** — `weak_accepted.by_broker_source`
- Threshold: cost per paid loan **>= $600**
- Sorted descending so worst overpayers lead
- Each row expands to show constituent Campaigns (campaign-level cost detail is informative here per user direction)
- Per-cell cost is computed using the campaign's CommissionType: see §11.5.3

**(b) Blocked Sources to consider re-enabling** — `blocked_to_reconsider`
- Operates on `LeadResultTypeId = -1` ("Source excluded") rejections
- Sampled rejected leads identity-match (SSN | Phone+DOB | Email+DOB) against later purchases via OTHER (Broker, SR1) cells
- Temporal filter: match must occur **strictly after** the rejection AND **within 30 days** (`SQ_BOUNCEBACK_WINDOW_DAYS`). This is critical — earlier purchases mean they were already our customer, not a missed opportunity
- Sorted by `bounceback_paid` descending — sources where blocking is forfeiting the most funded loans
- **No campaign-level drill-down on this section** per user direction (blocking decisions happen at Broker/Source level)

**(c) Sources where we overpay** — `cheaper_clones.by_broker_source`
- Walks every upfront-paid lead (CPL + BID commission models — CPF and REV don't have upfront costs)
- For each, identity-matches against LATER purchases in the window
- When a cheaper version of the same person reappears via a DIFFERENT (Broker, SR1) cell, the original buy was an overpay
- Reports total overspend, average overspend per overpaid lead, median wait days

#### 11.5.3 Cost-model derivation (CommissionType enum)

The DB's `Brokers.Campaigns.CommissionType` is an integer enum. **Canonical names from the Partnerships Handbook** (`~/Desktop/wiki/partnerships-handbook.html`):

| ID | Canonical name | Cost formula used by `scan_source_quality.py` | Notes |
|---|---|---|---|
| 1 | `PerFundedLoan` (CPF) | `rate × paid_out_count` | Paid when a loan is funded. The 0.10/0.12 outliers we saw look like they may have meant percentage-of-loan (i.e. should be type 5), but the literal multiplication is defensible. |
| 2 | `PerApplication` | `rate × leads_purchased` | Paid per submitted application. Small population in our window (2 campaigns). I called this "CPL" in older code — it's actually per-application, not per-lead. |
| 3 | `PerClick` (CPC) | **Excluded from analysis** | Click traffic — LendingTree / MoneyLion / Google PPC / Bing PPC. The handbook explicitly notes click campaigns "are excluded from CPF reporting dashboards." Same approach here. |
| 4 | `PerAcceptedAPILead` | `SUM(Leads.BidAmount)` with fallback to `rate × leads_purchased` | Covers BOTH static-price API campaigns (Example 1 in handbook: fixed $0.80/lead, scorecard-gated) AND price-reject bidding campaigns (Example 2: $10 floor with counteroffer flow). The distinction is the `WithPriceRejectBidding` flag on the campaign, not the commission type. `Leads.BidAmount` is set on the bidding variant. |
| 5 | `PerFundedLoanPreCheck` | `rate × SUM(paid-out loan amount)` | CPF + Pre-Check API. Per handbook: "we pay X% of the funded loan value." Looser DSIT-10 reclaim logic vs the standard PerFundedLoan. **Also used as a label** for CPF click campaigns where the partner doesn't actually call the Pre-Check API but the campaign is paid per funded loan — done this way to keep them in CPF reporting (Type 3 PerClick is excluded from CPF dashboards). |

For null / unknown types the cell has no cost figure and `cost_per_paid_loan` is omitted.

**The 12% blended CPA target:** Per handbook, "the target is a blended CPA of around 12%. Below this level the programme is profitable after accounting for operational costs, bad debt, and all other origination costs. Above it, we are eroding margin." This is the canonical KPI for the source-quality work; the current page reports `cost_per_paid_loan` in dollars but doesn't explicitly compare against the 12% threshold. See §11.5.9 for proposed improvements.

#### 11.5.4 Ghost rate (worst-quality leaderboard)

The old "highest decline rate" leaderboard rewarded the wrong thing: a broker whose leads NEVER engage has zero declines and ranked as "best quality". A broker who brings real, engaged customers sees real declines downstream AND real paid loans.

New metric: **ghost rate = (leads_purchased − applications) / leads_purchased** — the share of purchased leads who never even started an application. Plus each row shows the broker's paid count as the downstream signal.

This is computed client-side in `brokers.html` from `brokers.json` fields; no scanner change needed.

#### 11.5.5 Null-SR1 rejection analysis (Part B.5)

Answers the question: "are we systematically rejecting good leads because the broker didn't pass a SourceReference1?"

- Samples up to 20,000 rejected leads where `SourceReference1` is null/blank
- Identity-matches against the in-memory purchased-lead index (same SSN/Phone+DOB/Email+DOB strategy)
- Same temporal filter (post-rejection, 30d)
- Extrapolates by `total_population / sample_size`
- Surfaces in `source-quality.json` under top-level key `null_sr1_analysis`

First-run findings (2026-05-11): ~258k null-SR1 rejected leads in 60d → estimated ~77 funded loans those people came back through other channels and paid. Modest, but not zero. The decision is whether buying null-SR1 leads upfront would save money vs picking them up later via the back door.

#### 11.5.6 Workflow tunables (env vars on `refresh-source-quality.yml`)

| Var | Default | Meaning |
|---|---|---|
| `SQ_WINDOW_DAYS` | 60 | Analysis window length in days |
| `SQ_MATURATION_DAYS` | 30 | How many days back the window ends, to let paid-out outcomes settle |
| `SQ_BOUNCEBACK_WINDOW_DAYS` | 30 | Max days between rejection and a bounceback to count |
| `SQ_MIN_VOLUME` | 200 | Per-cell minimum purchased leads to qualify for ranking |
| `SQ_MIN_EXCLUDED` | 200 | Per-cell minimum excluded leads to qualify for Blocked analysis |
| `SQ_SAMPLE_PER_CAMPAIGN` | 1500 | Sampling cap for rejected leads per candidate campaign in Part B |
| `SQ_NULL_SR1_SAMPLE` | 20000 | Sampling cap for null-SR1 rejection analysis |
| `SQ_LENDER_ID` | 6 | Lender filter (Transform Credit) |

#### 11.5.7 JSON output shape

Top-level keys in `source-quality.json`:
- `snapshot_at`, `snapshot_date`, `lender_id`, `lender_label`
- `window_days`, `window_start`, `window_end`, `maturation_days`, `bounceback_window_days`
- `min_volume_for_ranking`, `min_excluded_for_ranking`
- `weak_accepted`: `{ median_paid_out_rate, q1_paid_out_rate, qualifying_sources, sources[], by_broker_source[] }` — the **page consumes `by_broker_source`** as the primary; `sources[]` is legacy per-campaign detail
- `blocked_to_reconsider[]` — already keyed at (Broker, SR1)
- `null_sr1_analysis`: `{ rejected_total, sample_size, sample_bounceback_arefs, sample_bounceback_paid, scale_factor, estimated_bouncebacks, estimated_paid_loans }`
- `cheaper_clones`: `{ total_overspend, leads_with_cheaper, by_broker_source[] }`

#### 11.5.8 Performance notes

- The scanner takes 3-5 minutes typically. Initial implementation timed out at 35min on a 3-way SQL self-join over 75M rows; rewrote as sample-and-match-in-Python (O(N+M) hash lookup instead of O(N×M) join). Don't revert to SQL self-joins.
- Memory: holds 2-3M purchased leads in RAM with identity indexes. Runner has 7GB, fits comfortably.

#### 11.5.9 Partnership-handbook context for this page

The Brokers page is fundamentally a dashboard companion to the canonical reports at **`reporting.rgcore.com`** — specifically:

- **Spending on Leads** — the most-used report; "shows ROI on all leads bought in a given period, with a trend and end-of-period CPA prediction." Target: ~12% blended CPA.
- **Affiliate Sub-Source Report (SourceRef1)** — apply rates, CPA, 6-month trend, LastSeen per SR1 per campaign.
- **Affiliate Sub-Source Report (SourceRef2)** — same at SR2 level for drilling into a problem SR1.
- **Bad Sub-Affiliates** — flags poor apply-rate sources that need blocking; surfaces the ones the AutoBlock Robot caught and ones it didn't.
- **Blocked Refs with Accepts** — payouts that occurred AFTER a source was blocked (i.e. our `blocked_to_reconsider` analysis but more direct).

**Hard rules from the handbook the page should reflect:**

- **Bottom of ping tree by design.** We sit at or near the bottom of ping trees. Our accept rate WILL look low vs mainstream lenders — this is structural, not a problem. "What matters is funded loans and revenue per lead sold, not accept rate." The page should not penalise low-accept-rate cells; it should reward high-funded-loan-rate cells.
- **5-day average time to fund, 10-day window for most conversions.** Currently the page uses a 30-day maturation lag — comfortably longer than the 10-day window. Confirms the lag was the right call.
- **30-day default dedup window, extendable to 45 days** if a lead progresses through the funnel. Important caveat for the cheaper-clones analysis (see §11.5.10).
- **AutoBlock thresholds** (run nightly at 04:00 local):
  - `SourceRef1 alone (no SR2)`: 50+ accepts + ≤10% apply rate + (0 payouts OR CPA >20%)
  - `SourceRef1 (with SR2 present)`: 150+ accepts + ≤10% apply rate + (0 payouts OR CPA >20%)
  - `SourceRef2`: 50+ accepts + ≤10% apply rate + (0 payouts OR CPA >20%)
- **The 10–20% apply-rate manual zone:** "The autoblock only triggers at ≤10% apply rate. Sources with a poor apply rate between 10–20% will not be autoblocked. Use the Bad Sub-Affiliates report regularly to catch these." Our page should highlight cells in this zone explicitly as "manual review needed."

#### 11.5.10 Cheaper-clones / overpay analysis — known caveat

The overpay analysis finds same-identity matches across (Broker, SR1) cells within the analysis window. Per the handbook, **a 30-day dedup window prevents us from buying the same customer twice across any source.** If our cheaper-clones analysis is finding within-30-day duplicates, the implication is one of:

1. Our identity match (SSN OR Phone+DOB OR Email+DOB) is finding **broader matches than their precise dedup** — same person with slightly different lead data across submissions.
2. **Cross-product dedup doesn't apply** — Default (Type 20) and MedallionBP (Type 24) may dedup separately, allowing the same customer to be purchased once per product within the window.
3. The dedup was bypassed because the first lead was DECLINED (not accepted) — declines don't set a dedup expiry, so the same customer arriving later via another source IS buyable.

This is the most likely explanation for option 3: most of our cheaper-clones matches are likely cases where one (Broker, SR1) cell sent a lead we declined (price-reject or scorecard fail), then a different cell sent the same person later and we bought them. That IS a legitimate overpay insight — but the framing should be "we bought them where we could have got them cheaper" rather than "we bought them twice." Worth tightening the page copy.

#### 11.5.11 Improvements landed 2026-05-12 (handbook overhaul)

After reading the Partnerships Handbook, 10 of 13 candidate improvements shipped. Status of each:

| # | Improvement | Status | Commit |
|---|---|---|---|
| 1 | CPA % as a primary metric | ✓ shipped | aa21739 |
| 2 | 12% CPA threshold benchmark + colour coding (green ≤12% · brass 12-20% · red >20%) | ✓ shipped | aa21739 |
| 3 | Apply rate prominent per cell, with zone badges | ✓ shipped | aa21739 |
| 4 | "AutoBlock would catch" badge on matching cells | ✓ shipped | aa21739, refined in 1c66aa8 (uses 150-threshold when SR2 children present, 50 when SR1 bare) |
| 5 | Manual-review zone (10–20% apply rate) as its own section | ✓ shipped | aa21739 |
| 6 | LastSeen per cell | ✓ shipped | 0e1bfdd (scanner) + 50ad86f (UI) — colour: ink ≤3d · brass 7-30d · red ≥30d |
| 7 | SR2 child drill-down beneath each SR1 row | ✓ shipped | 0e1bfdd (scanner) + 50ad86f (UI) — top 6 SR2 children with apply rate / cost-per-paid / paid-purchased |
| 8 | 6-month per-cell trend | **deferred** — requires monthly-bucketed scanner with 180d window; bigger reshape than overnight scope |
| 9 | Blended-CPA headline KPI | ✓ shipped | aa21739 (4 tiles: blended CPA, blended apply, paid loans + originated $, spend + cell count) |
| 10 | Tighten cheaper-clones copy | ✓ shipped | aa21739 (retitled "Sources where we paid for a previously-declined customer"; reframed per the dedup explanation) |
| 11 | State filter | **deferred** — per-state aggregation would multiply (cid, sr1, sr2) cardinality by ~24; needs separate scanner pass |
| 12 | Cross-link to reporting.rgcore.com on each section | ✓ shipped | aa21739 (Spending on Leads / Affiliate Sub-Source / Blocked Refs with Accepts / Bad Sub-Affiliates) |
| 13 | AutoBlock Robot's recent-activity feed | **deferred** — autoblock runs in the admin platform (`admin.rgcore.com`), not the warehouse; no read path yet. Asking the platform team to expose autoblock events would close this. |

For the three deferred items: each needs either a new data source or a substantial scanner reshape. Document trade-offs here when picking them back up.

### 11.6 Declines page (`declines.html`)

90-day rolling analysis of two distinct decline pools (powered by `scan_declines.py`):

1. **Lead rejections** — `Leads.LeadResultTypeId` enum (negative values + specific "rejected" types). Grouped by reason text.
2. **Application-stage declines** — `Flags` table joined to `Applications` (for lender filter). FlagTypeId 2 = Decline, 3 = DNL (do not lend), 4 = Cancelled, 6 = FraudRisk.

For each pool: top reasons table, daily trend SVG, ClientType breakdown, and top-20 ClientUsernames raising decline flags.

**Reason-text normalisation** in the scanner: lowercase + trim + strip trailing period, so `'BANK CHECK FAILED'` and `'bank check failed.'` fold into the same bucket. Critical — without this the top-reasons distribution fragments.

### 11.7 Pipeline page (`pipeline.html`)

March-cohort application-pipeline analysis with two d3-sankey diagrams (Lead funnel + Application progression). Powered by `scan_pipeline.py` + `scan_pipeline_samples.py`.

Per dead-end endpoint, the samples scanner pulls 25 random ARefs with their full interaction timeline (Tasks, Events, Messages, ESignatures). Identity-links by `(FirstName, Surname, DOB)` so the timeline spans every application the same person made. **All PII masked server-side** — ARefs to last-5, surnames to `******`, DOB to `****-**-**`, addresses to state only, plus email/phone/SSN/card redaction in message bodies.

---

## 12. Concept reference: Top-Up Eligibility (TUE)

Distilled from `~/Desktop/wiki/Markdown/Central_Loanbook.md` and the Glossary, since the TopUps page rests on understanding this:

A customer's **live loan** becomes **top-up eligible** (`TueStatus = Y` on `Loanbook.Loan_History`) when ALL of:

1. **Arrears = 0** (current).
2. **`CurrentBalance` < `TUEMaxBalance`** (set per-loan at payout; 0 means "this loan can never become TUE").
3. **Every prior `LoanHistory.TotalInArrears` < `TUEArrearsPosition`** (no past arrears worse than this threshold).
4. **Loan older than `TUEDaysOld` days** from `LoanAtInception.LoanAgreementDate`.
5. **State allows lending** (`Loanbook.States.IsLending = true` OR `isLendingTopUp = true`).
6. **No blocking flags** (DNC, DNL).
7. **Not on `TueOverride` blacklist**. `TueOverride` whitelist forces TUE = true regardless of all the above.

A **top-up loan** is a brand-new loan that pays off the customer's existing live loan and gives them more cash on top. The two are linked only via shared `ARef` in `Loans`. Settlement: an `AddTransaction` on the OLD loanbook with `PaymentMethodId = 11 (TopUp)` and amount = `TuePayOff`. The new loan gets `Originator = 'TopUp'` and `TopUpAmountAtInception = LoanAmount - TuePayOff`.

Per-loan TUE parameters (`TUEMaxBalance`, `TUEArrearsPosition`, `TUEDaysOld`) are configured per-lender at loan inception. For Transform Credit they vary across 397 distinct triples; the most common (covering ~12K of TC's loans) is **balance ≤ $7,249 / prior arrears ≤ $178 / loan ≥ 365 days old**.

**Important platform finding** (from the data): of the 182,601 TUE-eligible loan-months across all lenders in the 24-month window, 182,600 are Transform Credit. Effectively the entire TUE program is TC-only; Lending Mate / Rapida / Fianceo / Tandolan / Clearloans don't have TUE configured.

---

## 13. Concept reference: Brokers / Sources / Campaigns / SourceRefs terminology

This is the single most error-prone part of the schema because the DB names don't match the business terminology, and the business terminology itself uses "source" and "broker" interchangeably. **Get this wrong and the source-quality analysis silently lies.**

**Canonical reference:** `~/Desktop/wiki/partnerships-handbook.html` — the Together Loans Partnerships Handbook. Read it for the full picture; key extract below.

### 13.1 Hierarchy from the handbook

| Term | DB location | What it actually is |
|---|---|---|
| **Partner / Broker / Source** | `Brokers.Sources` (table) → one row per | A company we have a direct contractual relationship with. The affiliate we pay invoices to. The handbook uses **"sources" and "brokers" interchangeably** for this layer. Examples: "Lead Economy", "Search ROI, LLC", "TFLI", "Monevo", "SuperMoney". |
| **Campaign** | `Brokers.Campaigns` (table) → one row per | Our pricing tier with a Broker. Each Broker has at least two Campaigns (Default Type 20 + MedallionBP Type 24 are mandatory), plus optional variants (price-reject, scorecard-gated, frequency-filter, click-traffic). Different commission models / price points / quality gates. Each lead's `Leads.CampaignId` joins to one Campaign. The cost model (`CommissionType` + `CommissionRate`) lives on the Campaign. |
| **SourceRef1** | `Leads.SourceReference1` (column) | The **parent sub-affiliate** code the Broker passes through per lead. ~78k distinct values, 99.5% fill rate. The Broker resells leads from many SourceRef1s — these are sub-affiliates within their network. The Excluded Refs feature lets us block at this level (parent block). |
| **SourceRef2** | `Leads.SourceReference2` | The **child sub-source** within a SourceRef1. ~8.3M distinct values, 80% fill rate. Used for surgical blocks when a parent SR1 has one bad child. Preferred for blocks over SR1 because affiliates spin up new children when a parent gets blocked. |
| **SourceRef3** | `Leads.SourceReference3` | Partner's internal lead ID. ~18M distinct (near per-lead), 30% fill rate. **NEVER block on SR3.** Per the handbook: "Used by partners as their own internal lead ID tracking field." |

**The right analysis unit:** the Excluded Refs admin tool lets you block at SR1 OR SR2. So the **decision unit on this page should ideally support (Broker, SR1, SR2) hierarchy** rather than collapsing to (Broker, SR1) only. Currently we aggregate at (Broker, SR1); a future improvement is to surface SR2 drill-down underneath each SR1 row.

**What `Brokers.SourceTypeID` is NOT:** I initially mistook this for the Source granularity. It is not. The lookup table `dbo.SourceTypes` only contains 2 values ("Broker", "PPC") — a high-level categorisation of the Broker itself, not the sub-affiliate dimension. Do not use this for the Source-quality rollup.

**Join paths used by `scan_source_quality.py`:**
- `Leads.CampaignId` → `Brokers.Campaigns.CampaignId` (gets commission_type + rate + name)
- `Brokers.Campaigns.SourceId` → `Brokers.Sources.SourceId` (gets broker friendly_name)
- `Leads.SourceReference1` is read directly off the Lead row, no join

**Where the Campaign lookup lives:** `Brokers.Campaigns` exists in BOTH `ReportingBrokers` and `ReportingApplications` databases. The scanner tries `ReportingBrokers` first. Note: `ReportingApplications` also has a different table also called `Campaigns` (with a `MessageType` column) — used for marketing campaigns, NOT broker pricing tiers. The scanner detects this by checking for `MessageType` in the columns and skips it.

**Ephemerality:** Per the user, "when we shut down one campaign we tend to start another similar one with that Broker." This means **Campaign-level rankings decay quickly** — a campaign with bad numbers gets killed and the same audience is re-spun under a new CampaignId. (Broker, SR1) is much more stable across that churn, which is why it's the right analysis unit even though Campaign is the per-lead joined dimension.

---

## 14. Brandwatch email notifications

Wired 2026-05-11. New brand mentions (excluding BBB and Reviewcentre — those generate routine review-monitoring noise the team handles separately) trigger an email to **james.benamor@rgroup.co.uk** and **compliance@togetherloans.com** as soon as the hourly brandwatch refresh finds them.

### 14.1 Architecture

The notification step lives inside the existing `.github/workflows/refresh-brandwatch.yml` workflow, after the scan completes and before the JSON commit. Flow:

1. **`scripts/diff_brandwatch_mentions.py`** runs. It reads `brandwatch.json` (the live snapshot) and `brandwatch-seen.json` (the state file tracking notified mention IDs). Outputs:
   - `notify-mentions.json` — list of new mentions (after BBB/Reviewcentre filter)
   - Updated `brandwatch-seen.json` (adds every newly-observed ID, including filtered ones, so they don't re-trigger next run)
   - Stdout `has_new=true|false`
2. If `has_new=true`, the workflow builds a small HTML email body (one table row per new mention) and shells out to `dawidd6/action-send-mail@v3`
3. SMTP send via `smtp.gmail.com:465` (TLS), authenticated as the `noreply@togetherbook.net` Workspace user using an App Password
4. The seen-state file is committed alongside `brandwatch.json` so the next run remembers what's been notified

### 14.2 Sender mailbox setup

`noreply@togetherbook.net` was created as a regular Workspace user in the **togetherloans.com Google Workspace**. The togetherbook.net domain was added as a **secondary domain** to that Workspace (Admin Console → Account → Domains → Manage domains → Add a domain → Secondary domain, then verify ownership via TXT record at the registrar — Cloudflare DNS for this domain).

No MX records added; the mailbox is send-only. If reply-capable mail is ever needed, add the standard Google MX records + SPF TXT.

2-Step Verification is on for the account. App Password is generated at `myaccount.google.com/apppasswords` while signed in as the user (incognito session recommended). The 16-character password lives in the GH secret `SMTP_PASSWORD`.

### 14.3 What's excluded from notifications

Sources with `source == 'bbb' | 'reviewcentre' | 'review_centre'` (case-insensitive). Hardcoded in `EXCLUDED_SOURCES` at the top of `scripts/diff_brandwatch_mentions.py`. To change:

```python
EXCLUDED_SOURCES = {"bbb", "reviewcentre", "review_centre"}
```

To add/remove email recipients, edit the `to:` field on the `Send email` step in `.github/workflows/refresh-brandwatch.yml`.

### 14.4 Testing the path

To force a test send when no new mentions are coming in organically, pop an ID from `brandwatch-seen.json`:

```python
import json, pathlib
brand = json.load(open('brandwatch.json'))
seen = json.load(open('brandwatch-seen.json'))
target = next(m for m in brand['mentions']
              if (m.get('source') or '').lower() not in {'bbb','reviewcentre','review_centre'}
              and m.get('id') in seen['ids'])
seen['ids'] = [i for i in seen['ids'] if i != target['id']]
pathlib.Path('brandwatch-seen.json').write_text(json.dumps(seen, indent=2))
```

Then `git add brandwatch-seen.json && git commit && git push && gh workflow run refresh-brandwatch.yml`.

First arrival from this brand-new sender will likely land in spam — flag as Not Spam once and Gmail trains.

### 14.5 To pause notifications without breaking the scan

Comment out the `Send email` step in `.github/workflows/refresh-brandwatch.yml`. The diff step still runs and the seen-state still updates, so no backlog accumulates.

---

## 15. Dev workflow + procedures

### Editing copy or HTML

1. Edit the file in `~/Desktop/APIsForKids/`
2. **Bump cache-bust query strings on every CSS link and the logo `<img src>` on every page** (see git history for the small Python regex pattern). Without this, GitHub Pages' 600s CDN cache + browser cache will hold old CSS.
3. `git add -A && git commit -m "summary" && git push`
4. ~30s later GitHub Pages rebuilds; another ~10s for Cloudflare Access' edge to propagate.
5. Tell the user to hard-refresh (Cmd+Shift+R / iOS Safari → Settings → Advanced → Website Data → search domain → Delete).

### Adding a new auto-refresh workflow

Follow the established pattern in `refresh-row-counts.yml`:
1. Write `scripts/scan_X.py` that takes env vars, runs SQL/API calls, writes `X.json` at repo root with a top-level `snapshot_date` field.
2. Copy `refresh-row-counts.yml` to `refresh-X.yml`. Adjust:
   - `name` and `concurrency.group`
   - cron minute (stagger with the other workflows)
   - guard step's filename (`X.json`)
   - env block (FABRIC_* or whatever auth)
   - the `python scripts/scan_X.py` invocation
   - commit message
3. Push. The workflow will appear in the Actions tab; trigger it manually first time with `gh workflow run refresh-X.yml`.

### Cache-bust pattern (multi-page)

```python
import re, time
from pathlib import Path
ROOT = Path('/Users/richmondrobot/Desktop/APIsForKids')
v = str(int(time.time()))
pages = ['index.html','apis.html','robots.html','yesterday.html','brandwatch.html',
         '1stcontact.html','directory.html','database.html','stats.html','topups.html',
         'brokers.html','declines.html','pipeline.html']
for f in pages:
    p = ROOT / f
    s = p.read_text()
    for t in ['quiet-tokens.css','quiet.css','quiet-extras.css','quiet-legacy.css',
              'style.css','togetherbook-logo.png']:
        s = re.sub(rf'(["\']{re.escape(t)})(\?v=\d+)?(["\'])', rf'\1?v={v}\3', s)
    p.write_text(s)
```

### Cache-bust pattern (single-page, terminal one-liner)

```sh
NEW=$(date +%s)
OLD=$(grep -oE '\?v=[0-9]+' brokers.html | head -1 | sed 's/?v=//')
sed -i '' "s|?v=${OLD}|?v=${NEW}|g" brokers.html
```

### JSON cache-busting (for fetches inside HTML)

If a page fetches a JSON file (e.g. `brokers.json`, `source-quality.json`), the fetch URL must include `?bust=` + a fresh timestamp so the GitHub Pages CDN doesn't serve stale JSON when the scanner has just updated it:

```js
fetch("source-quality.json?bust=" + Date.now(), { cache: "no-store" })
```

Without this, you'll see the page render with an old JSON schema while the new scanner code expects new fields, producing visible "undefined" cells. Diagnosed and fixed on the brokers page after multiple incidents 2026-05-10/11.

### 15.1 Manually refresh a single page's data

```sh
gh workflow run refresh-topups.yml --repo richmondbot2000-prog/APIsForKids
gh run list --workflow=refresh-topups.yml --repo richmondbot2000-prog/APIsForKids --limit 1
gh run watch <run-id> --repo richmondbot2000-prog/APIsForKids --exit-status
git pull --rebase
```

The `workflow_dispatch` trigger bypasses the same-day guard so the refresh always runs work, useful after a column-name fix or a data-source change.

### 15.2 Refresh ALL data immediately

```sh
for w in refresh-yesterday-payouts refresh-row-counts refresh-brandwatch \
         refresh-1st-contact refresh-directory refresh-staff-activity \
         refresh-topups; do
  gh workflow run "$w.yml" --repo richmondbot2000-prog/APIsForKids
done
```

Useful after a global change (e.g. updating a shared filter or reformatting JSON output schemas).

### 15.3 Rotate a GH Actions secret

```sh
# JSON-shaped secrets (Workspace service account)
gh secret set WORKSPACE_SERVICE_ACCOUNT_JSON \
  --repo richmondbot2000-prog/APIsForKids < /path/to/key.json

# Plain-string secrets (most others)
printf 'NEW_VALUE' | gh secret set FABRIC_CLIENT_SECRET --repo richmondbot2000-prog/APIsForKids
```

After rotation, manually trigger any affected workflow to confirm it still runs.

### 15.4 Inspect why a workflow run failed

```sh
gh run list --workflow=refresh-topups.yml --repo richmondbot2000-prog/APIsForKids --limit 5
gh run view <run-id> --repo richmondbot2000-prog/APIsForKids --log-failed | tail -40
```

The `--log-failed` flag returns only the failing step's stdout/stderr — much faster than scrolling through the full log.

### 15.5 Add a new lender to the TopUps chart

Currently hardcoded to `LENDER_ID = 6` in `scripts/scan_topups.py`. To support another lender:

1. Edit `scan_topups.py`: change `LENDER_ID` and `LENDER_LABEL` (or accept them as env vars).
2. Trigger the workflow.
3. The page picks up `lender_label` from the JSON automatically — the lead text is data-driven.

To support multiple lenders side-by-side, restructure: add a `lenders[]` array to the JSON output, expose a tenant pill row similar to the directory page, render one bar series per lender. Plan for it being mostly empty for non-Transform-Credit lenders since the TUE program is currently TC-only.

### 15.6 Add another Workspace tenant to the Directory page

The Directory scanner (`scan_directory.py`) now supports multiple Google Workspace tenants via the `WORKSPACE_TENANTS` JSON-array secret. To add a new tenant (e.g. when a new acquired company's Workspace needs to feed into the Directory):

1. **In the target Workspace's Admin Console** (signed in as a Super Admin of THAT tenant):
   - Security → Access and data control → API controls → Manage Domain-Wide Delegation → Add new
   - Client ID: `116293508437634653191` (the existing service account's OAuth client — same one across all tenants)
   - OAuth scopes: `https://www.googleapis.com/auth/admin.directory.user.readonly` (single scope works here when the entry's being added; the multi-scope quirk only bit the first-tenant setup)
   - Wait ~10 minutes for propagation
2. **Update the `WORKSPACE_TENANTS` secret in this repo:**
   ```json
   [
     {"name":"letme","delegate":"james.benamor@letme.co.uk","domain":"letme.co.uk"},
     {"name":"rgroup","delegate":"ben.gardner@rgroup.co.uk","domain":"rgroup.co.uk"},
     {"name":"NEW","delegate":"super-admin@newdomain.com","domain":"newdomain.com"}
   ]
   ```
   Each entry's `delegate` must be a Super Admin user IN that Workspace. The service account impersonates them via DWD.
3. Trigger `refresh-directory.yml`. The output `staff.json` will gain a `tenants[]` field at the top level enumerating which tenants were fetched, plus per-user `tenant` tags. Failures are surfaced in `fetch_errors[]` so partial successes don't break the page.

The legacy single-tenant fallback (`WORKSPACE_DELEGATE_USER`) is retained for backwards compatibility — if `WORKSPACE_TENANTS` is unset, the scanner reverts to the original single-tenant code path.

### 15.7 Update the database schema doc (`database.md`)

`database.md` is mirrored from `~/Desktop/wiki/Overview/06_Database_Schema.md`. To update:

1. Edit the wiki version first (canonical).
2. Copy across: `cp ~/Desktop/wiki/Overview/06_Database_Schema.md ~/Desktop/APIsForKids/database.md`
3. Cache-bust + commit + push.

(Currently this is manual. Could be automated with another GH Actions workflow that watches the wiki repo, but the schema rarely changes — manual is fine.)

### 15.8 Replace the Quiet logo

1. Drop the new transparent PNG into `~/Desktop/APIsForKids/togetherbook-logo.png` (overwriting).
2. **Trim transparent padding before deploying** — the logo's visible glyph height should equal its image height. Pillow snippet:
   ```python
   from PIL import Image
   img = Image.open('togetherbook-logo.png').convert('RGBA')
   px = img.load()
   for y in range(img.height):
       for x in range(img.width):
           r, g, b, a = px[x, y]
           if 0.299*r + 0.587*g + 0.114*b > 200:
               px[x, y] = (r, g, b, 0)
   bbox = img.getbbox()
   if bbox:
       img.crop((max(0,bbox[0]-12), max(0,bbox[1]-12),
                 min(img.width,bbox[2]+12), min(img.height,bbox[3]+12))
       ).save('togetherbook-logo.png', optimize=True)
   ```
3. Cache-bust + commit + push.

---

## 16. Lessons learned

A short list of footguns to avoid, kept brief; longer detail in `~/Desktop/wiki/CLAUDE_CONTEXT.md` §9.

### Site / deploy

- **GitHub Pages + browser cache lies for ~10 minutes.** Always cache-bust CSS+image links AND any JSON the page fetches (`?bust=` + Date.now()).
- **Hamburger toggle:** inline `onclick` only, never both inline AND `addEventListener` on the same element — they double-fire.
- **Leaflet z-indexes escape `.leaflet-container`.** Wrap maps in `position: relative; z-index: 0`.
- **For first-run JSON files, `git diff --quiet -- file` returns 0** even though the file is brand new and untracked. Stage first, then `git diff --cached --quiet`.

### Workspace / Directory

- **Workspace tenant `letme.co.uk` ≠ user emails.** Most user primaries are on `@letme.com` alias. Use `customer='my_customer'`, never `domain=`.
- **`admin.google.com` Domain-Wide Delegation** silently rejects single-scope adds with `Can't add OAuth client X with 1 scope`. Add two scopes at once on first setup. Subsequent edits accept single-scope changes fine.
- **Multi-tenant DWD setup:** each new Workspace's Super Admin must authorise the SAME service account Client ID separately. The SA itself doesn't need re-keying; it just impersonates a delegate in each tenant.
- **App Password requires 2-Step Verification on.** Google hides the App Passwords page from accounts without 2SV. Direct URL `myaccount.google.com/apppasswords` works once 2SV is enabled.

### Data / queries

- **Activity-scan timestamp columns vary by table.** Don't hardcode column names — query `INFORMATION_SCHEMA` for any datetime-typed column, prefer known names from a fallback list.
- **`ClientUsername` matching:** strict `local-part@<known-domain>` only. Bare first-name matches are too risky given multiple staff share first names.
- **The `Loan_History` table doesn't have a `DIA` column** — DIA is computed inline as `DATEDIFF(day, DateInArrearsUTC, DateTimeUTC)`, with NULL `DateInArrearsUTC` meaning "not in arrears".
- **`LenderId` is on `Loan`, not `LoanAtInception`.** `TopUpAmountAtInception` is on `LoanAtInception`, not `Loan`. Auto-discover via `INFORMATION_SCHEMA` rather than guess.
- **`Brokers.Sources.SourceTypeID` only has 2 values** ("Broker", "PPC"). It is NOT the granular Source dimension. Use `Leads.SourceReference1` instead. See §13.
- **`Brokers.Campaigns` exists in TWO databases** (`ReportingBrokers` AND `ReportingApplications`). The `ReportingApplications` one has a `MessageType` column and is for marketing campaigns, not broker pricing tiers. Detect via column presence and skip it.

### Workflows

- **Free-tier GH Actions cron silently skips under load.** Use `0 6-23 * * *` + a guard step instead of a single daily slot. (Exception: `refresh-source-quality.yml` is daily 07:05 because the analysis is heavy.)
- **gh CLI heredoc body input is brittle** with multi-line content and shell-quoting. For long values (like JSON secrets), use the GitHub Web UI instead. Confirmed pain 2026-05-10 with `WORKSPACE_TENANTS`.

### Source-quality scanner

- **3-way SQL self-join over 75M Leads rows times out at 35min.** Use the sample-and-match-in-Python pattern instead — O(N+M) via hash indexes, not O(N×M) via join cardinality.
- **The (Broker, SR1) refactor left a candidate-filter bug** in Part B where CampaignId set was being compared against BrokerId values (different keyspaces, only random hits survive). Always check that filter dimensions match keyspace when refactoring aggregation units.
- **`source-quality.json` JSON cache-busting is mandatory** on the brokers page fetches. Page renders before scanner updates → schema mismatch → visible "undefined" cells. Diagnosed and fixed 2026-05-10.
- **Bounceback temporal constraint matters:** without `purchase_date > rejection_date`, you double-count people who were already our customers before the rejection. Cuts noise ~72% per the 2026-05-11 fix.
- **Window-end maturation lag matters too:** measuring "last 60 days ending today" understates paid_out for recent buys because they haven't had time to fund yet. Shift the window to end 30 days ago. Cuts cost-per-paid noise on recent volume.

### Brandwatch

- **Trustpilot caps public pagination at page 10**, and uses `experiencedDate` not `publishedDate` for review dates. BBB stores dates as a `{day, month, year}` zero-padded string dict.
- **Reddit anonymous .json endpoint 403s from cloud IPs.** OAuth code path is wired but Reddit's developer registration is impossible via Google sign-in. Use ScraperAPI residential proxy fallback.
- **Bbb on ScraperAPI:** use `&premium=true` only, NOT `&render=true&premium=true` — the combination breaks. Trustpilot needs `&render=true` (without premium).
- **First arrival from a brand-new SMTP sender lands in spam.** Mark "Not Spam" once and Gmail trains. Allow ~30min for the spam-reputation update to propagate.

### Conceptual

- **Decline-rate is NOT a quality metric.** A broker whose leads never engage has zero declines and looks great by that measure. Use ghost rate `(purchased - applications) / purchased` instead. See §11.5.4.

---

## 17. Pending / blocked work

| Item | Status | Blocker |
|---|---|---|
| `rgroup.co.uk` Workspace as second Directory tenant | Pending Ben Gardner's DWD setup confirm | Set up wired locally and `WORKSPACE_TENANTS` secret is configured. Ben (Super Admin on rgroup.co.uk Workspace) authorised the service account but the test fetches were still 403ing as of last check — likely needs verification that he's actually Super Admin (not Admin) and that the OAuth scope string is exactly `https://www.googleapis.com/auth/admin.directory.user.readonly`. Resume by triggering `refresh-directory.yml` and checking `fetch_errors[]` in the resulting `staff.json`. |
| Per-API response time + call count on home page | Plan ready, not built | Awaiting Kamran Kamaei's response to the email request for `Reader` access on the rgcore Azure subscription's Application Insights resources. Plan: GH Actions workflow that queries each App Insights resource for yesterday's `requests \| where timestamp > ago(1d) \| summarize count(), avg(duration) by cloud_RoleName`, writes `api-stats.json`, renders a `48ms · 2.1M calls` line under each helper card. |
| Type 1 (CPF) rate=0.10/0.12 ambiguity | Awaiting user clarification | The `scan_source_quality.py` CommissionType decoder treats type 1 literally as `rate × paid_out`. A handful of campaigns have rate 0.10/0.12 which produce near-zero cost and look mis-typed (probably type 5 rev-share entries entered under type 1). Resume by asking the user whether those should be re-coded as rev-share, or whether the literal CPF interpretation stands. |
| Null-SR1 buy-vs-wait economic decision | Diagnosed not decided | 2026-05-11 analysis found ~77 funded loans per 60d that came back through proper-SR1 channels after we rejected the null-SR1 lead. Decision is whether to buy null-SR1 leads upfront. Needs the price of a null-SR1 lead from the upstream broker to compute net economics. |
| Humand integration | Plan ready, not built | Awaiting Humand support's response to the email request for Public API access + a production API key. Plan: pull people + org chart + birthdays/anniversaries; enrich directory cards with manager line, team tag, joined date, birthday. |
| Live code-line stats | Function code written, GH Actions equivalent not built | Currently shows a manual snapshot from 2026-05-06 (1.65M lines / 12.3K files / 45 repos). Either deploy `azure-function-stats/` (blocked on Azure admin) or build a `refresh-code-stats.yml` GH Actions workflow using the existing DevOps PAT. |
| Three engineering specs (drafts) | Not implemented | `SPEC_AppInsights_CustomDimensions.md` + `SPEC_CentralStats_APIs.md` + `SPEC_QueueTelemetry_Tracing.md` in `~/Desktop/wiki/Overview/`. Need a developer + reviewer to build. |
| Reddit OAuth | Code wired, abandoned | `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` would activate the OAuth code path. Reddit's developer registration is impossible to complete via Google sign-in (verified-email + non-OAuth + separate bot account + Data API form). Currently using ScraperAPI residential-proxy fallback. |
| Sanitisation pass | Not done | Internal hostnames (`*.api.rgcore.com`), Slack channels, payment partners (BridgePay/Checkout/GoCardless/Twilio/Vonage/Veriff/Sendgrid), `LenderId 6 = Together Loans/TransformCredit`, employee email pattern — all live on the site. Less of a concern now that the canonical URL is gated behind Cloudflare Access. |

---

## 18. Cross-references

- `CLAUDE.md` (this repo) — working-style notes + the nightly-update directive for Claude sessions
- `CLAUDE_CONTEXT.md` (in the wiki repo) — operational notes for AI-assisted iteration; carries pending work and lessons learned in more conversational form
- `~/Desktop/wiki/Overview/07_APIsForKids_Site.md` — the wiki-wide spec entry for this site, integrated alongside other Central Services docs. **Keep this in structural sync with SPEC.md.**
- **`~/Desktop/wiki/partnerships-handbook.html`** — the Together Loans Partnerships Handbook. **Authoritative source** for partner terminology, commission types, AutoBlock thresholds, CPA target, dedup rules, scorecard semantics, and the partnership team's report library. Read this before doing anything substantive on the Brokers page or source-quality analysis.
- `~/Desktop/wiki/TogetherBOOK_handoff/wiki/README.md` — the original Quiet Edition design handoff package
- `~/Desktop/wiki/Markdown/*` — service-by-service Tettra exports, useful when adding new platform-aware pages
- `~/Desktop/wiki/Markdown/*` — service-by-service Tettra exports, useful when adding new platform-aware pages

---

_End of spec. If you're adding a new page or workflow that isn't covered above, please update this file in the same PR._
