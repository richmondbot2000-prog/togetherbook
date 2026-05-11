# TogetherBOOK — Site Specification

_The source-of-truth document for `togetherbook.net` / `richmondbot2000-prog/APIsForKids`. Lives in this repo so future maintainers find it next to the code._

**Last reviewed:** 2026-05-10

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
10. [The Workspace + GCP setup (Directory page)](#10-the-workspace--gcp-setup-directory-page)
11. [Specific page details](#11-specific-page-details)
12. [Concept reference: Top-Up Eligibility (TUE)](#12-concept-reference-top-up-eligibility-tue)
13. [Dev workflow + procedures](#13-dev-workflow--procedures)
14. [Lessons learned](#14-lessons-learned)
15. [Pending / blocked work](#15-pending--blocked-work)
16. [Cross-references](#16-cross-references)

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

**Identity provider for the gate**: plain Google IdP (not Workspace). OAuth client lives in the Brandwatch GCP project. Authorised redirect URI: `https://togetherbook.cloudflareaccess.com/cdn-cgi/access/callback`. The single Access policy is `Letme staff = Include: emails ending in @letme.com`.

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
| **Brokers** | `/brokers.html` | Per-affiliate-source 90-day scorecard. KPI band, top-10 leaderboard chart, sortable table with inline mini-funnel + volume share + lead→paid ratio + funded $. Click row to see stage-by-stage detail and top rejection reasons. | `brokers.json` |
| **Schema** | `/database.html` | Full DB schema (renders `database.md` via marked.js + mermaid theme), plus per-table row counts as flipboards | `row-counts.json` + `database.md` |
| **Code** | `/stats.html` | Codebase size dashboard (Solari split-flap digits) + by-language and by-repo tables | inline manual snapshot (live refresh pending Azure access) |
| _(unlinked)_ | `/apis.html` | Per-helper detail page — kept for any deep-link bookmarks; not in nav | inline |
| _(unlinked)_ | `/robots.html` | Per-robot list page — kept for any deep-link bookmarks; not in nav | inline |

**Topbar nav (every page):** `About our systems · Yesterday · Brandwatch · 1stContact · Directory · TopUps · Pipeline · Brokers · Schema · Code`. Plus a hamburger drawer ≤960px viewport.

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
| `refresh-pipeline.yml` | hourly :45 | `pipeline.json` | Fabric warehouse | `FABRIC_*` secrets |
| `refresh-pipeline-samples.yml` | hourly :50 | `pipeline-samples.json` | Fabric warehouse (PII-masked output) | `FABRIC_*` secrets |
| `refresh-telegram.yml` | hourly :40 | `telegram-mentions.json` | Public Telegram channels via Telethon | `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_B64` (dormant until set) |

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
| `scan_telegram.py` + `telegram_monitor.py` | Public Telegram channels via Telethon (read-only on a dedicated account) → local SQLite → public-safe JSON | `telegram-mentions.json` | Match list configurable via `telegram-watchlist.json`. Excerpts go through email / phone / SSN / card / ARef-shape redaction before being written. Workflow dormant until the user runs the local auth dance and uploads `TG_SESSION_B64`. |

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
| `FABRIC_CLIENT_SECRET` | row-counts, yesterday-payouts, 1st-contact, staff-activity, topups | Service-principal client secret for the Fabric warehouse |
| `SCRAPERAPI_KEY` | brandwatch | ScraperAPI residential-proxy API key (for Trustpilot / BBB / Reddit which 403 cloud IPs) |
| `YOUTUBE_API_KEY` | brandwatch | YouTube Data API v3 key |
| `WORKSPACE_SERVICE_ACCOUNT_JSON` | directory | Full JSON key for `directory-reader@letme-directory.iam.gserviceaccount.com` (Google Cloud SA with domain-wide delegation) |
| `WORKSPACE_DELEGATE_USER` | directory | `james.benamor@letme.co.uk` — the Workspace super-admin the SA impersonates |

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

## 13. Dev workflow + procedures

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

### Cache-bust pattern

```python
import re, time
from pathlib import Path
ROOT = Path('/Users/richmondrobot/Desktop/APIsForKids')
v = str(int(time.time()))
pages = ['index.html','apis.html','robots.html','yesterday.html','brandwatch.html',
         '1stcontact.html','directory.html','database.html','stats.html','topups.html']
for f in pages:
    p = ROOT / f
    s = p.read_text()
    for t in ['quiet-tokens.css','quiet.css','quiet-extras.css','quiet-legacy.css',
              'style.css','togetherbook-logo.png']:
        s = re.sub(rf'(["\']{re.escape(t)})(\?v=\d+)?(["\'])', rf'\1?v={v}\3', s)
    p.write_text(s)
```

### 13.1 Manually refresh a single page's data

```sh
gh workflow run refresh-topups.yml --repo richmondbot2000-prog/APIsForKids
gh run list --workflow=refresh-topups.yml --repo richmondbot2000-prog/APIsForKids --limit 1
gh run watch <run-id> --repo richmondbot2000-prog/APIsForKids --exit-status
git pull --rebase
```

The `workflow_dispatch` trigger bypasses the same-day guard so the refresh always runs work, useful after a column-name fix or a data-source change.

### 13.2 Refresh ALL data immediately

```sh
for w in refresh-yesterday-payouts refresh-row-counts refresh-brandwatch \
         refresh-1st-contact refresh-directory refresh-staff-activity \
         refresh-topups; do
  gh workflow run "$w.yml" --repo richmondbot2000-prog/APIsForKids
done
```

Useful after a global change (e.g. updating a shared filter or reformatting JSON output schemas).

### 13.3 Rotate a GH Actions secret

```sh
# JSON-shaped secrets (Workspace service account)
gh secret set WORKSPACE_SERVICE_ACCOUNT_JSON \
  --repo richmondbot2000-prog/APIsForKids < /path/to/key.json

# Plain-string secrets (most others)
printf 'NEW_VALUE' | gh secret set FABRIC_CLIENT_SECRET --repo richmondbot2000-prog/APIsForKids
```

After rotation, manually trigger any affected workflow to confirm it still runs.

### 13.4 Inspect why a workflow run failed

```sh
gh run list --workflow=refresh-topups.yml --repo richmondbot2000-prog/APIsForKids --limit 5
gh run view <run-id> --repo richmondbot2000-prog/APIsForKids --log-failed | tail -40
```

The `--log-failed` flag returns only the failing step's stdout/stderr — much faster than scrolling through the full log.

### 13.5 Add a new lender to the TopUps chart

Currently hardcoded to `LENDER_ID = 6` in `scripts/scan_topups.py`. To support another lender:

1. Edit `scan_topups.py`: change `LENDER_ID` and `LENDER_LABEL` (or accept them as env vars).
2. Trigger the workflow.
3. The page picks up `lender_label` from the JSON automatically — the lead text is data-driven.

To support multiple lenders side-by-side, restructure: add a `lenders[]` array to the JSON output, expose a tenant pill row similar to the directory page, render one bar series per lender. Plan for it being mostly empty for non-Transform-Credit lenders since the TUE program is currently TC-only.

### 13.6 Update the database schema doc (`database.md`)

`database.md` is mirrored from `~/Desktop/wiki/Overview/06_Database_Schema.md`. To update:

1. Edit the wiki version first (canonical).
2. Copy across: `cp ~/Desktop/wiki/Overview/06_Database_Schema.md ~/Desktop/APIsForKids/database.md`
3. Cache-bust + commit + push.

(Currently this is manual. Could be automated with another GH Actions workflow that watches the wiki repo, but the schema rarely changes — manual is fine.)

### 13.7 Replace the Quiet logo

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

## 14. Lessons learned

A short list of footguns to avoid, kept brief; longer detail in `~/Desktop/wiki/CLAUDE_CONTEXT.md` §9.

- **GitHub Pages + browser cache lies for ~10 minutes.** Always cache-bust CSS+image links.
- **Hamburger toggle:** inline `onclick` only, never both inline AND `addEventListener` on the same element — they double-fire.
- **Leaflet z-indexes escape `.leaflet-container`.** Wrap maps in `position: relative; z-index: 0`.
- **For first-run JSON files, `git diff --quiet -- file` returns 0** even though the file is brand new and untracked. Stage first, then `git diff --cached --quiet`.
- **Workspace tenant `letme.co.uk` ≠ user emails.** Most user primaries are on `@letme.com` alias. Use `customer='my_customer'`, never `domain=`.
- **`admin.google.com` Domain-Wide Delegation** silently rejects single-scope adds with `Can't add OAuth client X with 1 scope`. Add two scopes at once.
- **Activity-scan timestamp columns vary by table.** Don't hardcode column names — query `INFORMATION_SCHEMA` for any datetime-typed column, prefer known names from a fallback list.
- **`ClientUsername` matching:** strict `local-part@<known-domain>` only. Bare first-name matches are too risky given multiple staff share first names.
- **The `Loan_History` table doesn't have a `DIA` column** — DIA is computed inline as `DATEDIFF(day, DateInArrearsUTC, DateTimeUTC)`, with NULL `DateInArrearsUTC` meaning "not in arrears".
- **`LenderId` is on `Loan`, not `LoanAtInception`.** `TopUpAmountAtInception` is on `LoanAtInception`, not `Loan`. Auto-discover via `INFORMATION_SCHEMA` rather than guess.
- **Trustpilot caps public pagination at page 10**, and uses `experiencedDate` not `publishedDate` for review dates. BBB stores dates as a `{day, month, year}` zero-padded string dict.
- **Free-tier GH Actions cron silently skips under load.** Use `0 6-23 * * *` + a guard step instead of a single daily slot.

---

## 15. Pending / blocked work

| Item | Status | Blocker |
|---|---|---|
| Per-API response time + call count on home page | Plan ready, not built | Awaiting Kamran Kamaei's response to the email request for `Reader` access on the rgcore Azure subscription's Application Insights resources. Plan: GH Actions workflow that queries each App Insights resource for yesterday's `requests \| where timestamp > ago(1d) \| summarize count(), avg(duration) by cloud_RoleName`, writes `api-stats.json`, renders a `48ms · 2.1M calls` line under each helper card. |
| Humand integration | Plan ready, not built | Awaiting Humand support's response to the email request for Public API access + a production API key. Plan: pull people + org chart + birthdays/anniversaries; enrich directory cards with manager line, team tag, joined date, birthday. |
| Live code-line stats | Function code written, GH Actions equivalent not built | Currently shows a manual snapshot from 2026-05-06 (1.65M lines / 12.3K files / 45 repos). Either deploy `azure-function-stats/` (blocked on Azure admin) or build a `refresh-code-stats.yml` GH Actions workflow using the existing DevOps PAT. |
| Three engineering specs (drafts) | Not implemented | `SPEC_AppInsights_CustomDimensions.md` + `SPEC_CentralStats_APIs.md` + `SPEC_QueueTelemetry_Tracing.md` in `~/Desktop/wiki/Overview/`. Need a developer + reviewer to build. |
| Reddit OAuth | Code wired, abandoned | `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` would activate the OAuth code path. Reddit's developer registration is impossible to complete via Google sign-in (verified-email + non-OAuth + separate bot account + Data API form). Currently using ScraperAPI residential-proxy fallback. |
| Sanitisation pass | Not done | Internal hostnames (`*.api.rgcore.com`), Slack channels, payment partners (BridgePay/Checkout/GoCardless/Twilio/Vonage/Veriff/Sendgrid), `LenderId 6 = Together Loans/TransformCredit`, employee email pattern — all live on the site. Less of a concern now that the canonical URL is gated behind Cloudflare Access. |

---

## 16. Cross-references

- `CLAUDE_CONTEXT.md` (in the wiki repo) — operational notes for AI-assisted iteration; carries pending work and lessons learned in more conversational form
- `~/Desktop/wiki/Overview/07_APIsForKids_Site.md` — the wiki-wide spec entry for this site, integrated alongside other Central Services docs
- `~/Desktop/wiki/TogetherBOOK_handoff/wiki/README.md` — the original Quiet Edition design handoff package
- `~/Desktop/wiki/Markdown/*` — service-by-service Tettra exports, useful when adding new platform-aware pages

---

_End of spec. If you're adding a new page or workflow that isn't covered above, please update this file in the same PR._
