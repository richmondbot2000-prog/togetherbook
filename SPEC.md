# TogetherBOOK — Site Specification

_The source-of-truth document for `togetherbook.net` / `richmondbot2000-prog/togetherbook`. Lives in this repo so future maintainers find it next to the code. **A successor Claude or engineer should be able to pick this up cold and operate the site competently.**_

**Last reviewed:** 2026-05-15 — added Wall compact-feed / pagination / YouTube + OG link previews / Share deep-links; renamed Payout → Payouts with three-range tabs + per-capita toggle and new history scanner; new Holidays page + Line Manager field in Directory + manager team view + Approved Holiday status. Previous reviews: 2026-05-12 (source-quality analysis, brandwatch email notifications, multi-tenant Directory, Cloudflare Access).

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
   - 11.4 [Payouts page](#114-payouts-page-yesterdayhtml)
   - 11.5 [Brokers page + Source-quality analysis](#115-brokers-page--source-quality-analysis-brokershtml)
   - 11.6 [Pipeline page](#116-pipeline-page-pipelinehtml)
   - 11.7 [Comms response time](#117-comms-response-time-commshtml)
   - 11.8 [Wall page](#118-wall-page-wallhtml)
   - 11.9 [Holidays page](#119-holidays-page-holidayshtml)
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

## 0.5 Scope rule — Transform Credit / Together Loans ONLY

**Every warehouse-bound scanner MUST filter to `LenderId = 6`.** This site reports on Transform Credit / Together Loans data only. Other lenders in the same Fabric warehouse (Rapida, LendingMate, Fianceo, Tandolan, Lendingmate, ClearLoans, etc.) are out of scope.

The shared `LENDER_ID = 6` constant is the canonical value. New scanners use it. Existing scanners enforce it as:

- Tables with a direct `LenderId` column: `WHERE LenderId = 6`. This covers `Communications.Messages`, `Loanbook.Loan`, `Applications.Applications`, `Brokers.Campaigns`, `Loanbook.LoanAtInception`.
- Tables joined via Loan: `JOIN dbo.Loan l ON … WHERE l.LenderID = 6`. Used in `scan_first_contact.py` + `scan_yesterday_payouts.py`.
- The previous shortcut `JOIN Lenders … WHERE le.Country = 'USA'` is **deprecated** — TC is the only US lender today but the rule is to filter on identity, not geography. All older scanners have been migrated to `LenderID = 6` as of 2026-05-14.

If a sample renders showing other-lender content (Rapida MFA codes, etc.) the filter is missing. Trace back through each `dbo.Messages` / Loan / Application reference in the scanner and ensure the lender filter applies.

This rule is mechanically enforceable: `grep -nE "FROM dbo\.(Messages|Loan|Applications|LoanAtInception|Customers|Telephones|Emails|ESignatures|Flags)" scripts/scan_*.py` should never return a query without a paired `LenderId = 6` clause (directly on the table, or via JOIN to a table that has it).

---

## 2. URLs and hosting

| What | URL |
|---|---|
| Canonical (gated) | <https://book.togetherbook.net> — Cloudflare Access in front, only `@letme.com` Google accounts pass |
| Apex redirect | <https://togetherbook.net> 301 → `book.togetherbook.net` (Cloudflare Page Rule) |
| Public backdoor | <https://richmondbot2000-prog.github.io/togetherbook/> — same content, no login. Open by design. Pluggable by going GitHub Pro $4/mo + private source. |
| Source of truth | <https://github.com/richmondbot2000-prog/togetherbook> (public repo, `main` branch deploys via GitHub Pages) |

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
| **Wall** | `/wall.html` | First top-level item. Internal social feed (~200 staff): posts up to 10k chars + 10 media; comments + one-level replies; typographic named reactions; SVG-icon action bar; trash-icon deletes (author or admin); notification bell + topbar badge. Photo/video/GIF uploads on posts, comments AND replies. See §11.8. | `wall.json` + `wall-seen.json` + `wall-media/*` |
| **Home — About our systems** | `/index.html` | Long-scroll storybook in 7 chapters: hero · 8 helpers · 12 robots · 6 screens · 6 outside askers · loan story · 6 ground rules · 15 commandments. Section-parent for Schema + Code sub-pages. | inline (no JSON) |
| **Payouts** | `/yesterday.html` | Three-range tabs (Yesterday · Last Week · Last Year). Yesterday + Week: three Leaflet maps (clustered pins · per-state totals · per-state averages) + per-state breakdown tables. Year: aggregated layout — pin map of full-year borrowers, state-total map with `$/resident` toggle, monthly bar chart, state-avg map, summary cards. (File still `yesterday.html`; nav label is "Payouts".) | `yesterday-payouts.json` + `payouts-week.json` + `payouts-year.json` |
| **Holidays** | `/holidays.html` | Per-user fiscal-year (Apr→Mar) attendance calendar: 52 weekly rows × 7 day cells, 8 status types + manager-only Approved Holiday, default Mon-Fri working / Sat-Sun non-working / UK BH on weekday = Holiday. Admins can switch person. Line managers see a Team tab — one thin horizontal year strip per direct report, ~10 px cells, sticky name column. Change log union for managers (own days + manager-made changes to reports). See §11.9. | `holidays.json` + `annotations.json` (line_manager) |
| **Brandwatch** | `/brandwatch.html` | Public mentions across 10 sources (Trustpilot, BBB, Reddit, Bluesky, Lemmy, Hacker News, CourtListener, Google News, CFPB, YouTube) | `brandwatch.json` |
| **Reports** | `/reports.html` | Hub landing for the operational reports (1st Contact, Top Ups, Pipeline, Brokers, Comms). Section-parent. | none (links only) |
| **1st Contact** | `/1stcontact.html` | First inbound email per US borrower / GT after payout, 3-month window, redacted PII; word cloud at top. Now lives under Reports. | `first-contact.json` |
| **Directory** | `/directory.html` | **The control hub.** Every Workspace user joined with warehouse activity, payroll record, free-form annotations, and group memberships. Top-of-page panels: per-seat billing summary (live vs deleted/suspended seats + £/mo), data-quality health panel (leakage warnings). Deleted/suspended rows sink to the bottom with strikethrough email + forward chip. Full CRUD on users (Delete + forward, Delete account, Create) and Groups — Suspend was retired 2026-05-14 because Google bills suspended seats at full price; only deletion stops the charge. **Single point of reference + control for every user record across every Richmond Group system, and per-seat-billing accountability for leavers.** | `staff.json` + `staff-activity.json` + `annotations.json` + `workspace-actions.json` + `groups.json` + `PAYROLL_KV` (off-repo) |
| **TopUps** | `/topups.html` | 24-month chart of distinct Transform Credit (LenderId 6) live loans split Primary / Top-Up, with a TUE-eligible-count line overlay; "last refreshed" badge | `topups.json` |
| **Pipeline** | `/pipeline.html` | March-cohort application-pipeline analysis with two d3-sankey diagrams (Lead funnel + Application progression), per-stage drop-off table, and click-to-expand sampled customer timelines per dead-end endpoint. All PII masked server-side. | `pipeline.json` + `pipeline-samples.json` |
| **Brokers** | `/brokers.html` | Per-Broker funnel scorecard PLUS three Source-quality analysis sections — (a) Sources to consider blocking, (b) Blocked Sources to consider re-enabling, (c) Sources where we overpay. KPI band, two top-10 leaderboards (volume + paid), worst-quality leaderboard (ghost rate), sortable table with inline mini-funnel. Click row to expand stage-by-stage detail + top rejection reasons. | `brokers.json` + `source-quality.json` |
| **Comms** | `/comms.html` | Inbound→reply response-time tracker using a positive-list reply rule (only `%CRM%` and `%Responder%` ClientTypes count). Weekly Monday-anchored line chart × 4 customer-state buckets (unknown / applicant / live_loan / arrears), fixed 0-336 h Y-axis, two filter checkboxes (Exclude Robot Responder / Exclude no-reply at 14 d cap), 14-day data-maturity cutoff. Sample-messages panel + **Download full list CSV** button (`comms-full.csv` — every inbound, redacted, with result + hours + reply detail). | `comms.json` + `comms-full.csv` |
| **Schema** | `/database.html` | Full DB schema (renders `database.md` via marked.js + mermaid theme), plus per-table row counts as flipboards | `row-counts.json` + `database.md` |
| **Code** | `/stats.html` | Codebase size dashboard (Solari split-flap digits) + by-language and by-repo tables | inline manual snapshot (live refresh pending Azure access) |
| _(unlinked)_ | `/apis.html` | Per-helper detail page — kept for any deep-link bookmarks; not in nav | inline |
| _(unlinked)_ | `/robots.html` | Per-robot list page — kept for any deep-link bookmarks; not in nav | inline |

**Topbar nav (every page, restructured 2026-05-14):** primary row is `Wall · About our systems · Payout · Brandwatch · Directory · Reports`. Two of those have sub-pages (rendered as a secondary row beneath the topbar, separated by the existing fine line):

- **About our systems** → `Schema · Code`
- **Reports** → `1st Contact · Top Ups · Pipeline · Brokers · Comms`

The secondary row is sticky and only renders on pages inside a section that has sub-pages. On narrow viewports (≤960px) the hamburger drawer collapses the secondary row into an inline accordion — a top-level item with sub-pages expands its children on the first tap and navigates on the second. Logic lives in `nav.js` (shared script, included once per page); markup is driven by a `data-sub` JSON attribute on the parent link.

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
| `retry-trustpilot.yml` | manual only | `brandwatch.json` (merge) | Trustpilot only, via ScraperAPI | `SCRAPERAPI_KEY`. Use when the morning brandwatch run logged `source_status.trustpilot.ok=false` — bypasses the same-day guard because it only touches the TP fields, not the snapshot as a whole. |
| `refresh-1st-contact.yml` | hourly :00 | `first-contact.json` | Fabric warehouse | `FABRIC_*` secrets |
| `refresh-directory.yml` | hourly :00 | `staff.json` | Google Workspace Admin SDK | `WORKSPACE_SERVICE_ACCOUNT_JSON`, `WORKSPACE_DELEGATE_USER` |
| `refresh-staff-activity.yml` | hourly :15 | `staff-activity.json` | Fabric warehouse | `FABRIC_*` secrets |
| `refresh-topups.yml` | hourly :30 | `topups.json` | Fabric warehouse | `FABRIC_*` secrets |
| `refresh-brokers.yml` | hourly :35 | `brokers.json` | Fabric warehouse (`Leads` × `Brokers.Campaigns` × `Brokers.Sources`) | `FABRIC_*` secrets |
| `refresh-pipeline.yml` | hourly :45 | `pipeline.json` | Fabric warehouse | `FABRIC_*` secrets |
| `refresh-comms.yml` | hourly :05 | `comms.json` | Fabric warehouse (Communications + Loanbook + Applications) | `FABRIC_*` secrets. ~10-14 min/run; pulls inbound + paired reply variants, augments missing ARefs by phone/email lookup, samples redacted (message, reply) pairs. |
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

**Manual triggering** (any workflow): `gh workflow run <name>.yml --repo richmondbot2000-prog/togetherbook`. The `workflow_dispatch` trigger bypasses the guard so the run always does work, useful for forcing a fresh snapshot after a column-name fix or a data-source change.

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
- **Lead outcomes ≠ Applications.Status snapshots.** `dbo.LeadOutcomes` (in `ReportingApplications`) is the canonical per-lead event log of funnel progression — every milestone a lead reaches generates one row tagged with the `LeadId` that was live at the moment. `Applications.ApplicationStatusTypeId` reflects current state only and is updated by `/UpdateStatus`. For attribution-sensitive analytics (which broker drove which paid loan when multiple brokers sold the same customer), use `LeadOutcomes` per `LeadId`, not `Applications.Status` per `ARef`. See §11.5.8b for the canonical query pattern.
- **`Leads.ExpiryDateUtc`** holds the dedup expiry per lead — how long that broker can "claim" the lead. Important if you ever need to reconstruct attribution semantics manually (typically not needed since `LeadOutcomes.LeadId` already records which lead was live at outcome time).

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
| Repo | `richmondbot2000-prog/togetherbook` (public) |
| Auth on dev machine | `gh` CLI logged in as `richmondbot2000-prog` (token in macOS keychain) |
| Pages source | `main` branch, root |
| Action runner | GitHub-hosted `ubuntu-latest` |
| Secrets list | `gh secret list --repo richmondbot2000-prog/togetherbook` |

---

## 9. GitHub Actions secrets

| Secret | Used by | What it is |
|---|---|---|
| `FABRIC_CLIENT_SECRET` | all warehouse-bound workflows (row-counts, yesterday-payouts, 1st-contact, staff-activity, topups, pipeline, pipeline-samples, brokers, source-quality) | Service-principal client secret for the Fabric warehouse |
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

#### 11.1.1 Purpose

The Directory is **the single point of reference and control for every user record across every Richmond Group system**. For each person it draws together:

- their Google Workspace account(s) (identity, login, mail, Drive)
- their warehouse / loanbook activity (who's actually writing to Central Services in the last 60 days)
- their HR record from the two payroll CSVs (employer, employee #, DOB, address, mobile, start, termination)
- any free-form annotations the team has added (phone overrides, start-date overrides)

A second, equally important purpose: **per-seat billing visibility for leavers**. Deleted accounts are never hidden — they stay on the list forever, greyed out and sorted to the bottom, so an ex-employee can't silently keep costing money or holding system access.

**Important — Suspend doesn't save money** (codified 2026-05-14). Google Workspace bills suspended seats at full price; only **deletion** frees the licence. The Directory page exposes two delete actions:

1. **Delete account (no forwarding)** — calls `delete-account` on the Worker, which `DELETE`s the user via Admin SDK. Licence freed immediately; mailbox + Drive recoverable from admin.google.com for 20 days, then permanently removed.
2. **Delete account and forward mail (auto-completes in 21 days)** — calls `convert-to-group`: renames the user to a parked address, deletes the renamed user (freeing the original email's licence), and records a `pending_conversion` annotation. The daily `finalise_pending_conversions.py` cron then auto-creates a forwarding-only Group at the original address once Google's 20-day address-reuse lockout expires. ~20 days of bounced mail during that window, then mail forwards to the target forever at £0/mo.

**Optional pre-deletion transfer** (added 2026-05-14). Both delete actions accept an optional **Transfer Drive + Mail to** target. When supplied, the page calls `queue-transfer-and-delete` on the Worker which:

1. Initiates an Admin SDK Data Transfer of the leaver's Drive ownership → target. Async — Google completes the file ownership change in the background.
2. Appends an entry to `pending-transfers.json` so the Directory page renders an `⏳ Transferring + Deleting` badge on the row and the bg scanner can pick it up.
3. Returns — does NOT delete the source user yet (we need the mailbox still alive for the Gmail migration step).

The hourly `process-pending-transfers.yml` workflow runs `scripts/process_pending_transfers.py` which:

1. Walks every Gmail message in the source mailbox via the Gmail API (`messages.list` + `messages.get` format=raw + `messages.insert` on the target with `internalDateSource=dateHeader` so timestamps survive).
2. **Finishes the leaver flow** — branches on `convert_to_group_forward_to` on the pending entry:
   - If unset: plain Admin SDK `users.delete` on the source (the "Delete account (no forwarding)" path).
   - If set: rename source to `<localpart>.parked.<unix>@<domain>`, delete by immutable id, AND write a `pending_conversion` annotation to `annotations.json` so the existing daily `finalise_pending_conversions.py` cron creates the forwarding Group at the original address once Google's 20-day reuse lockout expires (the "Delete account and forward mail" path).
3. Removes the entry from `pending-transfers.json` (commits the change so the page badge clears).

Required scopes (must be added to **Domain-wide delegation** in admin.google.com → Security → API controls):

| Scope | Used by |
|---|---|
| `https://www.googleapis.com/auth/admin.datatransfer` | Worker — Drive ownership transfer queue |
| `https://www.googleapis.com/auth/gmail.readonly` | Scanner — read source mailbox |
| `https://www.googleapis.com/auth/gmail.insert` | Scanner — insert messages into target mailbox |

The legacy **Suspend** actions in the Worker (`suspend-and-route`, `suspend-no-forward`) remain wired but are no longer surfaced from the page — they exist only so historical audit-log entries still resolve, and so already-suspended accounts can still be `unsuspend`'d or transitioned to a delete.

#### 11.1.2 Data sources and refresh cadence

| Source | File / key | Owner | Refresh cadence | Failure mode |
|---|---|---|---|---|
| Google Workspace user list | `staff.json` (repo) | `scan_directory.py` | hourly via `refresh-directory.yml` | Page falls back to last good `staff.json`. Refresh failure is visible in `gh run list --workflow=refresh-directory.yml`. |
| Warehouse activity (per user, 60d) | `staff-activity.json` (repo) | `scan_staff_activity.py` | hourly :15 via `refresh-staff-activity.yml` | Page still renders Workspace rows without activity meta. |
| Annotations (notes) | `annotations.json` (repo) | `apifk-annotations-worker` writes on Save | on-demand (every Save) | Read fails silently → form starts empty; payroll-fallback still pre-fills. |
| Payroll | `PAYROLL_KV` namespace, key `current` (Cloudflare Workers KV, **NOT** in repo) | manual: user runs `scripts/scan_payroll.py` against the two CSVs in `~/Desktop/wiki/Payroll/` and pastes the JSON into KV | manual — every time HR sends a refreshed CSV (typically monthly; **monthly reminder emails go out automatically** via `.github/workflows/email-payroll-monthly.yml` to `Payroll@letme.com` on the 1st of each month) | Endpoint returns 503; page just doesn't render the Payroll section. |
| Workspace audit log (suspend / route / create / group ops) | `workspace-actions.json` (repo) | `apifk-workspace-worker` appends per action | on-demand | Page renders without the "→ forwards to X" chip. |
| Groups (Workspace group emails + members) | `groups.json` (repo) | `scan_groups.py` | hourly :20 via `refresh-groups.yml` | Groups section just stays empty until the file lands. |

Both `staff.json` and `staff-activity.json` are committed to the repo (no PII). `PAYROLL_KV` is **deliberately off-repo** because the CSVs contain DOBs, home addresses, and mobile numbers — only reachable via the Cloudflare-Access-gated endpoint `book.togetherbook.net/api/workspace/payroll`. The `github.io` public mirror cannot fetch it (no Worker route there) so the Payroll section is invisible there.

#### 11.1.3 Identity matching — joining the three sources

Workspace ↔ Activity ↔ Payroll are matched independently, because each system has a different primary key:

**Workspace → Activity** (`scan_staff_activity.py`):
- Strict: `local-part@<known-domain>` only. Known domains: `rgroup.co.uk`, `letme.co.uk`, `letme.com`, `transformcredit.com`, `togetherloans.com`, `lendingmate.ca`, `rapida.bg`, `rapidamoney.pl`, `clearloans.com.au`, `fianceo.com`, `tandolan.dk`, `tandolaina.fi`, `tando.dk`.
- Bare-name matches (`Ed`, `Sophie`, `Igor`) **deliberately excluded** — collide across staff sharing first names.
- Tenant aliases consolidated in `short_tenant()`: `togetherloans` → `transform`, `tandolan/tandolaina/tando` → `tandolan`, `rapidamoney` → `rapida`.
- Warehouse usernames that look like emails but don't map to any Workspace account are emitted as `external_users` and rendered with dashed-photo placeholders.

**Workspace → Payroll** (`payrollAllFor()` in `directory.html`, alias generation in `scan_payroll.py`):
1. Try `payrollData.by_email[lowercase email]` — direct match (4 of 60 records have email today).
2. Fall back to `payrollData.by_name[<alias>]` over a list of candidates derived from the Workspace user:
   - `"given family"` lowercase
   - `"given last-token-of-family"` (handles hyphenated family names: `Morgan Kennedy-Smith` → `morgan smith`)
   - `"first-of-name last-of-name"` lowercase
   - All of the above with apostrophes/hyphens/dots stripped (`Philip O'Neill` → `philip oneill`)
3. The scanner emits **alias keys** so the same record matches multiple Workspace names:
   - `"first last"` (full legal)
   - `"last-token-of-firsts last"` (handles multi-token first names: `Ho Chun Cyrus Leung` → `cyrus leung`, `Rachid James Benamor` → `james benamor`)
   - `"<nickname> last"` for known long-form first names (`Daniel`→`Dan`, `Philip`→`Phil`, `Maximillian`→`Max`, `Benjamin`→`Ben`, `Thomas`→`Tom`, plus ~20 more — see `NICKNAMES` map in `scripts/scan_payroll.py`).
   - Punctuation-stripped variants of each.

**Workspace ↔ Workspace** (sibling accounts — same person, multiple domains):
- The detail card groups Workspace accounts by lowercase `(given, family)` via `workspaceSiblings()` so e.g. Mourad Malki's `@letme.com` and `@clearloans.com.au` accounts both show under "Other emails" when viewing either.
- Each sibling account is **still rendered as its own row** (so per-tenant activity is visible). The merge is in the detail card only.

#### 11.1.4 Field precedence (which source wins for each field)

| Field | Master | Fallback chain |
|---|---|---|
| Login email, name, suspended, admin, photo, department, tenants | Workspace | — (Workspace is identity) |
| Employer, employee #, DOB, home address, termination | Payroll | — |
| Phone, start date | Annotation > Payroll mobile/start_date | (form pre-fills with payroll; annotation overrides when saved) |
| All other emails | Workspace siblings + Payroll email field | Displayed under "Other emails" — never silently replaces the primary |
| Forwarding target on suspended accounts | `annotations.json` (`forward_to` field set by `add-forwarding` / `suspend-and-route`); falls back to `workspace-actions.json` | — |
| Profile photo (Directory-only) | `assets/photos/<email-safe>.jpg` (when uploaded via the page) | Workspace `photo_url` (Google's `thumbnailPhotoUrl`) | Initials placeholder |
| TogetherBook admin status | `admins.json` (lowercase email list) | — (`OWNER_EMAIL` hardcoded in worker is always an admin) |

**Conflict policy: disagreements are silent**, not surfaced. E.g. if Workspace's family-name is `Smith` and payroll's is `Jones` for the same person, we just don't match payroll → it's invisible rather than wrong. If a future flag-mismatches UI is wanted, the data is all client-side and the page can grow a banner.

#### 11.1.5 Duplicate-record handling

Two design decisions worth flagging:

1. **Payroll duplicate-record policy.** `by_email` and `by_name` are **list-valued** in the scanner output. If two payroll rows share an email or a name alias, all records are kept. The page renders the primary (sorted: active first, most-recent start first) under "Payroll" and the others under "Also on payroll as" in manuscript red. **Never silently drop data** is the rule.
2. **Workspace duplicate-account policy.** A person with two Workspace seats (e.g. Mourad Malki across two tenants) renders as **two rows**. They aren't merged into one row because each seat has its own per-tenant activity record. The detail card surfaces the relationship via the "Other emails" field.

#### 11.1.6 UI layout, sort, top-of-page panels

**Top of page (above the user list)** — three at-a-glance panels:

- **Meta line** (`#dirMeta`): "Updated <iso> · N Workspace accounts · M active in DB · K extra people active in DB only".
- **Health panel** (`#dirHealthPanel`): a red-bordered card flagging data-quality issues — (a) suspended Workspace accounts with no recorded forwarding target (mail going nowhere), (b) active users with no matched payroll record (HR gap), (c) payroll records with no Workspace match (leaver-on-the-books or contractor). Each row has a one-click action ("Show them" filters to that subset or opens a list dialog). When everything is clean, the panel turns green with a single tick.
- **Billing summary** (`#dirBillingSummary`): three cards — live seats × `SEAT_GBP_PER_MONTH` (default £11, edit the JS constant for Business Plus / Enterprise), suspended count with "N of M have forwarding set", and total monthly cost with "Avoiding £X/mo from suspended accounts". Implements the "per-seat-billing visibility" half of the page's stated dual purpose.

The health panel checks four things; when all are zero, the panel turns green with a single tick:
1. Suspended accounts with no recorded forwarding target (mail going nowhere).
2. Suspended accounts still in 1+ groups (group-distributed mail still reaches them; defeats the leaver suspension's hygiene purpose).
3. Active Workspace users with no matched payroll record (HR gap?).
4. Payroll records with no matched Workspace user (leaver-on-the-books or contractor).
Plus a stale-payroll warning when `payrollData.updated_at` is over 40 days old — HR refreshes monthly, so 40+ days means the 1st-of-month cron reminder wasn't acted on.

**Filter row** (`#dirGroupRow`) — single mutually-exclusive axis, always visible (no toggle button):
- `all` — everyone (default)
- `@letme` — primary OR alias email matches `@letme.*`
- `@togetherloans` — primary OR alias email matches `@togetherloans.*`
- `CRM only` — warehouse-only entries (no Workspace account)
- `Suspended` — Workspace accounts currently suspended (excludes deleted)

Counts per pill are computed inline (`renderGroupPills`). The old multi-axis Status × Tenant × Workspace × Department filter stack was retired 2026-05-14 — too many dimensions, almost no users used them.

**Sort row** (`#dirSortRow`) — single axis driving `sortEntries()`:
- `Surname` (default)
- `Start date (newest first)` — payroll start_date
- `Last seen (most recent first)` — max of Workspace `last_login` (Gmail / sign-in) and warehouse `last_active_utc` (DB write). The list cell and the sort key both call `entryLastSeen()` so they always agree.

Within any sort, **Deleted always sink to the bottom** and **Suspended sit after Live** — these are stability rules that aren't user-overridable.

**Row form**: `[44px avatar] [name · GW icon · title · email + activity meta] [department + workspace chips]`. Activity meta surfaces last-seen + tenants + warehouse, plus phone + start_date — these last two can come from an annotation OR (fallback) from the matched payroll record. When the source is payroll rather than a saved annotation, the row meta appends ` (payroll)` so the source is obvious.

**Suspended-row styling**: `.dir-row--suspended` drops opacity to 0.55, the email is strikethrough, and a brass `→ <forward-target>` chip is appended (read from `annotations.forward_to` with fallback to `workspace-actions.json`).

**Search** (`#dirSearch`) covers name, email, every alias, title, department, plus payroll fields (first/last name in payroll, mobile, address, employee number). Single search box matches across all of them.

**Workspace edit card** surfaces (admin-only flags marked *):
- Email + Rename primary* link
- Other emails + Convert one to a group* link
- Full name, Department, Workspace tenant
- **Last login** — Google `lastLoginTime` (renders "never" for the epoch sentinel)
- Suspended (only when true), Google super-admin (only when true), TogetherBook Admin row (admin viewers only) with Make admin / Remove admin link

**Profile photo upload (admin-only).** A small Upload / Change / Remove link sits under the photo. Click → file picker → cropper modal opens with the chosen image:
- Drag-pan, scroll/slider zoom (100–400 %)
- Circular fade hint on the 360×360 stage
- Save rescales to 400×400 JPEG, ships to `directory-photo-upload`, commits to `assets/photos/<email-safe>.jpg`
- `directory_photo_uploaded_at` annotation is the cache-bust
- Locally-read JPEG is held in `localPhotoPreview` so the photo shows immediately while GitHub Pages publishes (~30-60 s)
- Override is **page-only** — it does NOT touch the user's real Workspace photo (deliberate)

#### 11.1.7 Workspace admin actions (the full verb set)

The detail card drives Cloudflare Worker `apifk-workspace-worker2` at `book.togetherbook.net/api/workspace/*`. Actions, grouped by surface:

**Suspension / lifecycle:**
- `suspend-and-route` `{ email, route_to, tenant? }` — adds `route_to` as a `forwardingAddresses` entry, enables `autoForwarding` with `disposition: leaveInInbox`, sets `suspended: true`. Atomic.
- `suspend-no-forward` `{ email, tenant? }` — suspends without setting any forwarding. Used when you want the seat fee gone but mail to bounce.
- `unsuspend` `{ email, tenant? }` — sets `suspended: false`, best-effort disables `autoForwarding`.
- `recover` `{ user_id, tenant? }` — undeletes a user inside Google's 20-day window. **Requires immutable id** (the email may have been recycled).
- `convert-to-group` `{ email, forward_to, tenant? }` — renames the user to `<localpart>.parked.<unixts>@<domain>` and deletes by immutable id. Writes a `pending_conversion` annotation with `scheduled_for = deleted_at + 20d`. The companion cron `.github/workflows/refresh-pending-conversions.yml` (daily 06:30 UTC) calls `create-forwarding-group` once the address has cleared Google's 20-day reuse lockout. Result: a forwarding-only Group at the freed address.
- `create-forwarding-group` `{ email, name, description?, forward_to, tenant? }` — internal: called by the daily cron. Not normally hit from the page.
- `create` `{ given_name, family_name, email, password, org_unit_path?, tenant? }` — creates a new Workspace user with `changePasswordAtNextLogin: true`.
- **No Delete endpoint.** Convert-to-group is the leaver workflow.

**Forwarding management** (the autoForwarding setting on a single user's Gmail):
- `add-forwarding` `{ email, route_to, tenant? }` — set forwarding on a still-live or already-suspended user.
- `disable-forwarding` `{ email, tenant? }` — turn off `autoForwarding` while keeping the user live and the `forwardingAddresses` list intact.
- `cancel-forwarding` `{ email, tenant? }` — turn off forwarding AND clear the routing annotation (used when moving a suspended account to the "black hole" list).
- `get-forwarding` `{ email, tenant? }` — read-only, used by the page on card open to detect a forwarding rule the user set themselves in Gmail.

**Groups:**
- `group-create` `{ email, name, description?, tenant? }`
- `group-delete` `{ email, tenant? }`
- `group-member-add` `{ group_email, member_email, role?, tenant? }`
- `group-member-remove` `{ group_email, member_email, tenant? }`

**Account hygiene:**
- `reset-password` `{ email, password, tenant? }` — sets a one-shot password and forces `changePasswordAtNextLogin: true`.
- `user-alias-remove` `{ user_email, alias, tenant? }` — detach an editable alias. `nonEditableAlias` (auto-aliases) reject; the page filters them out of the picker.
- `alias-to-group` `{ user_email, alias, group_name, initial_member?, description?, tenant? }` — one-shot: detach the alias, then create a Group at the freed address with the original user as initial member.
- `rename-user` `{ current_email, new_email, tenant? }` — PATCH the user's `primaryEmail`. Instant on Google's side; the old address becomes a nonEditableAlias that Google retires after ~21 days. The page writes a `rename_decay` annotation so the card shows a countdown.

**Directory-page assets (do NOT touch Workspace):**
- `directory-photo-upload` `{ user_email, photo_b64 }` — commits the JPEG to `assets/photos/<email-safe>.jpg`. Annotation `directory_photo_uploaded_at` is set client-side as a cache-bust.
- `directory-photo-remove` `{ user_email }` — deletes the file from `assets/photos/`.

**Identity + admin management:**
- `whoami` — open endpoint (any Access user). Returns `{ email, is_admin, is_owner, owner, admins }`. The page uses this on load to gate admin-only UI and surface "Signed in as X · role" under the header.
- `list-admins` — admin-only. Returns the admin email list.
- `admin-add` `{ target_email }` — admin-only. Adds to `admins.json` via GitHub Contents API. **Also auto-syncs** the Cloudflare Access allowlist (see §11.1.7c).
- `admin-remove` `{ target_email }` — admin-only. The owner cannot be removed. Auto-syncs allowlist.

**Cross-tenant routing.** Each action body may include `tenant`. The page's `workspaceAction` helper auto-injects it from either (a) the staff entry matching `body.email`, or (b) the group entry in `allGroups` matching `body.group_email`. The worker maps `tenant === "togetherloans"` to `env.IMPERSONATE_USER_TOGETHERLOANS`; everything else falls back to `env.IMPERSONATE_USER` (letme tenant). Without this, group operations on Together Loans groups return 403 from Google.

**Per-action authorisation chain:**
1. Cloudflare Access gates the route at the edge — only allowlisted emails reach the Worker. Failure: 302 to CF Access login.
2. The Worker checks `Cf-Access-Jwt-Assertion` header presence. Failure: 401.
3. The Worker fetches `admins.json` (60s edge cache, owner-failsafe) and confirms the actor is in the list. **`whoami` and `list-admins` are exempt** — non-admins need whoami to know who they are. All other actions: 403 if not admin.
4. Owner-protected actions (`suspend-and-route`, `suspend-no-forward`, `reset-password`, `admin-add`, `admin-remove`): 403 if the target is the owner and the actor is not the owner.
5. Google access tokens are minted via service-account JWT + DWD:
   - **Admin token** — impersonates the tenant's admin (`IMPERSONATE_USER` or `IMPERSONATE_USER_TOGETHERLOANS`), scope `admin.directory.user + group + group.member + apps.licensing`. Used for directory operations.
   - **Mailbox token** — impersonates the **target user**, scope `gmail.settings.basic + gmail.settings.sharing`. Used for the forwarding endpoints.
6. Failure: 502 with the Google error message.

**Audit log.** Every action appends to `workspace-actions.json` in the repo via the GitHub Contents API. The audit file is FIFO-trimmed to 2000 entries; older history persists in git (commit message is `Workspace: <action> <target> by <actor>`). The page reads `workspace-actions.json` to surface forwarding targets on suspended rows.

**Note on the long-running silent-fail bug:** the audit log silent-failed for weeks because `GITHUB_TOKEN` on the workspace worker only had `contents: read`. The same token gates `admins.json` commits + photo commits, so when admin-add was wired up in 2026-05-13 the symptom resurfaced. Token regenerated with `repo` scope on 2026-05-13; audit + admin + photo writes all working since.

#### 11.1.7c TogetherBook Admin role + Owner protection

A separate concept from Google's Workspace super-admin. Lives entirely in `admins.json` at the repo root:

```jsonc
{
  "schema_version": 1,
  "updated_at": "<ISO>",
  "admins": ["amber.cole@letme.com", "berginie.botero@togetherloans.com", ...]
}
```

The **Owner** is `james.benamor@letme.com` (hardcoded `OWNER_EMAIL` in `worker/workspace-worker.js`). They're always treated as admin even if removed from the list, and they're the only one who can change their own admin / suspended / password state.

**Page integration:**
- `fetchWhoami()` fires on load. The result populates `viewerEmail`, `viewerIsAdmin`, `viewerIsOwner`, `viewerAdmins`.
- "Signed in as X · admin/owner/read-only" indicator under the page heading (helps debug when admin controls don't appear).
- Admin-only UI hidden from non-admins: Manage Workspace section, Password section, Rename primary link, Convert-one-to-a-group link, "+ New user" button, Admin row on the edit card.
- Per-user Admin row in the Workspace section: shows `yes` / `no` plus a Make admin / Remove admin link on every row except the owner's (which shows "(owner — locked)"). Suspend buttons on the owner's card are hidden from non-owner viewers and replaced with an explanatory line.

**Cloudflare Access auto-sync.** After `admin-add` / `admin-remove` commits `admins.json`, the worker pushes the new admin list to the Access app on `book.togetherbook.net` so non-`@letme.com` admins can sign in from any IP without a dashboard click. Allowlist is built as `email_domain: letme.com` (covers all current/future letme staff) + every non-`@letme.com` admin as an explicit email entry. Implementation: `syncAccessAllowlist()` in `worker/workspace-worker.js`; constants `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_ACCESS_APP_ID` are hardcoded (not sensitive — they appear in dashboard URLs); the secret `CLOUDFLARE_API_TOKEN` is what unlocks the call. Sync is non-fatal: a failed sync still returns the successful admins.json change with `result.access_sync.error` populated.

#### 11.1.7d Directory profile photos (page-only override)

The page can store a profile photo that overrides Google's `thumbnailPhotoUrl` — only on `book.togetherbook.net`. Does NOT touch the user's actual Google Workspace profile photo (deliberate choice; Gmail/Drive/etc. keep their real photo).

**Mechanism:**
- Upload button under the photo block on every live user's card (admin-only).
- Page resizes the chosen image to 400×400 JPEG via canvas, base64-encodes, ships to `directory-photo-upload`. Worker commits to `assets/photos/<email-safe>.jpg` (where `<email-safe>` is the lowercase email with `@` → `_at_`).
- Page also saves a `directory_photo_uploaded_at` annotation (ISO timestamp) as cache-bust.
- `dirPhotoUrl(u)` returns the override URL when the annotation is set, otherwise falls back to `u.photo_url`, otherwise renders an initials placeholder.
- Remove button deletes the asset + clears the annotation.

#### 11.1.7b Group management (free, doesn't consume seats)

The Directory page also manages **Google Groups** — email addresses like `finance@rgroup.co.uk` that deliver to a list of members rather than to a single mailbox. Groups are free in Google Workspace (no per-seat fee, no member limit), so they're the right tool for shared inboxes, distribution lists, and the "forward to a team after a leaver" pattern.

**Data:** `groups.json` at the repo root, refreshed hourly :20 by `refresh-groups.yml` calling `scripts/scan_groups.py`. Schema:
```jsonc
{
  "schema_version": 1,
  "updated_at": "<ISO>",
  "totals": { "groups": N, "members": N },
  "tenants": ["letme.co.uk", ...],
  "groups": [
    {
      "email": "finance@rgroup.co.uk",
      "name": "Finance",
      "description": "...",
      "tenant": "letme.co.uk",
      "aliases": [],
      "member_count": 7,
      "members": [
        { "email": "...", "role": "MEMBER|MANAGER|OWNER", "type": "USER|GROUP|EXTERNAL", "status": "ACTIVE|SUSPENDED|..." }
      ]
    }
  ]
}
```

**Required DWD scopes** (read-only for the scanner, full read+write for the Worker):
- `admin.directory.group.readonly` + `admin.directory.group.member.readonly` (scanner)
- `admin.directory.group` + `admin.directory.group.member` (Worker)

**Worker endpoints** (all POST, same Cloudflare Access + `admins.json` gating as the user actions, all logged to `workspace-actions.json`):
- `/api/workspace/group-create` `{ email, name, description? }`
- `/api/workspace/group-delete` `{ email }`
- `/api/workspace/group-member-add` `{ group_email, member_email, role? }` (default role `MEMBER`)
- `/api/workspace/group-member-remove` `{ group_email, member_email }`

**Page integration:**
- A "Groups" section below the people list (`#dirGroupsSection`), rendering one row per group with name + email + member count.
- Each row opens a group modal (`renderGroupModalBody`) showing all members (each with a Remove button), an "Add member" form with a `<datalist>` autocomplete over active Workspace emails, and a Delete group button in a Danger zone.
- The "+ New group" button creates an empty group via `/api/workspace/group-create`.
- On every user's modal, a "Groups" section lists the brass chips for every group they're a member of (matched by primary email **and** any alias via `groupsForUser()`). Clicking a chip opens the group modal in place. The user modal itself never edits groups — that's the group modal's job, per the design rule "edit groups via the group's own card, not via the member's card."

**Member-validation rule:** the page's "Add member" datalist only includes active (non-suspended) Workspace emails from `staff.json`, so the user is guided away from typing free-text external emails. The Worker doesn't enforce this server-side though — the Admin SDK will happily accept any address — so the UI is the only guardrail today. If we want to lock it down server-side, add a check in `doGroupMemberAdd`.

**Group deletion is permanent.** Members aren't affected (they keep their accounts), but the address dies and any mail aliases pointing at it stop working. Same blast radius as a Workspace user delete, hence the confirmation strip in the Danger zone of the group modal.

#### 11.1.8 Annotations persistence

- **Storage:** `annotations.json` at the repo root. Shape `{ schema_version, updated_at, annotations: { "<email-or-username>": { ... } } }`.
- **Writes:** `book.togetherbook.net/api/annotations` → Worker `apifk-annotations-worker` (Cloudflare Worker name on the dashboard is the auto-generated `shiny-heart-00f8`). The Worker reads the current file via GitHub Contents API, merges in the new value (or deletes the key when all fields are empty), and commits back to `main`. **Field-by-field preservation:** missing fields in the payload are kept; empty-string fields are cleared; values overwrite. Lets callers do partial updates.
- **Recognised fields** (each preserved independently):
  - `phone` — string. Manual override over payroll's mobile.
  - `start_date` — `YYYY-MM-DD`. Manual override over payroll's start_date.
  - `address` — string. Manual override over payroll's address.
  - `forward_to` — string. Forwarding target for a suspended account. Set by `suspend-and-route` / `add-forwarding` flows. Used by the page to render the `→ chip` even when the audit log is incomplete.
  - `payroll_match` — object `{ employer, employee_number, first_name, last_name }`. Manual payroll-link override when name-matching can't find the right record.
  - `rename_decay` — object `{ old_address, renamed_at }`. Set when the page renames a user. Triggers the 21-day countdown shown under the name.
  - `pending_conversion` — object `{ forward_to, scheduled_for, parked_at, deleted_at }`. Set when convert-to-group runs. The daily cron clears this once the group is created.
  - `directory_photo_uploaded_at` — ISO timestamp. Set when a Directory profile photo is uploaded; used as cache-bust on the override URL.
- **github.io fallback:** the public github.io URL can read `annotations.json` but cannot write — the Worker route only exists on `book.togetherbook.net`.

#### 11.1.9 Payroll CSV ingestion (the manual process)

This is the one place where personally-identifiable HR data enters the system. It must stay manual.

1. HR sends the two spreadsheets (LetMe Property Management + Together Loans / R Group export) to `james.benamor@rgroup.co.uk`. The monthly cron at `.github/workflows/email-payroll-monthly.yml` prompts them on the 1st of each month.
2. James exports each as CSV and drops both into `~/Desktop/wiki/Payroll/`, overwriting the previous files **with the same filenames** (or, if HR has renamed them, update `LETME_FILE` / `TLRG_FILE` constants at the top of `scripts/scan_payroll.py`).
3. Run: `python3 ~/Desktop/togetherbook/scripts/scan_payroll.py | pbcopy`.
4. Cloudflare dashboard → **Workers KV → apifk-payroll → KV Pairs**. Delete the `current` entry, then **Add entry** with key `current` and paste. (Cloudflare's KV UI has no in-place edit, only View/Delete.)
5. No Worker redeploy needed — the next request reads the new value.

**Schema of the produced JSON** (don't reshape unless you also update the page):
```jsonc
{
  "schema_version": 1,
  "updated_at": "<ISO>",
  "counts": { "total": N, "letme": N, "tl_rg": N, "with_email": N },
  "by_email": { "<lowercase-email>": [<record>, ...] },   // ALWAYS list-valued
  "by_name":  { "<lowercase-alias>": [<record>, ...] }    // ALWAYS list-valued
}
```
Each record:
```jsonc
{
  "employer": "LetMe Property Management" | "Together Loans / R Group",
  "employee_number": "string|null",
  "first_name": "string",
  "last_name": "string",
  "email": "string|null",
  "dob": "YYYY-MM-DD",
  "age": int|null,
  "mobile": "string|null",
  "address": "string|null",
  "start_date": "YYYY-MM-DD",
  "termination_date": "YYYY-MM-DD|null",
  "employee_group": "string|null"
}
```

Reasonable expectation: ~60 records, ~28-30 KB minified. The 5.1 KB Worker Secret limit is why this is in KV (which supports up to 25 MB per value).

#### 11.1.10 Failure modes — what to check if the page misbehaves

| Symptom | Most likely cause | Fix |
|---|---|---|
| Payroll section missing on everyone | `PAYROLL_KV` empty / not bound; endpoint returns 503 | Re-run scanner → paste into KV → check `book.togetherbook.net/api/workspace/payroll` in browser returns JSON |
| Payroll section missing on one specific person | No match — they aren't in either CSV, OR their Workspace name is too different from payroll's legal name (no alias covers the gap) | Check unmatched list with the Python diagnostic in `scripts/scan_payroll.py`; if it's worth fixing, either rename them in Workspace, add a nickname to the `NICKNAMES` map, or accept that one record won't match |
| "Suspend + route" returns 502 | Most likely: `gmail.settings.sharing` or `.basic` scope not added in DWD | `admin.google.com → Security → Access and data control → API controls → Manage Domain-Wide Delegation` — edit the existing Client ID, add scopes `https://www.googleapis.com/auth/gmail.settings.basic` and `…/gmail.settings.sharing` |
| Any mutating action returns 401/403 | CF Access session expired, or the actor isn't in `admins.json` | Re-auth via book.togetherbook.net. Check `admins.json` in the repo. The "Signed in as X · role" line under the page header confirms what whoami sees. |
| Admin controls don't appear for a known admin | Their Cloudflare Access email differs from what's in `admins.json` (e.g. `@letme.co.uk` vs `@letme.com`) | Read the "Signed in as X" line; add that exact email to `admins.json` via Make admin button, or fix the Owner constant if they should be owner |
| `admin-add` returns 403 from GitHub | Workspace worker's `GITHUB_TOKEN` lacks `contents: write` | Regenerate at https://github.com/settings/tokens/new?scopes=repo → paste into the worker's `GITHUB_TOKEN` secret in Cloudflare → Deploy |
| `access_sync.ok = false` after admin change | `CLOUDFLARE_API_TOKEN` worker secret missing or revoked, or token lacks `Access: Apps and Policies: Edit` | Generate at https://dash.cloudflare.com/profile/api-tokens with that exact permission → paste as worker secret → Deploy |
| Forwarding chip ("→ X") missing on a suspended row | Neither `annotations.json` nor `workspace-actions.json` has a successful forward target for that email | If the suspend happened outside this UI (e.g. via Admin Console directly) there's no record. The page also calls `get-forwarding` for live users to detect autoForwarding the user set themselves in Gmail. |
| Whole page blank / 404 | GitHub Pages CNAME `book.togetherbook.net` lost its HTTPS cert (state goes `bad_authz`) | `gh api repos/richmondbot2000-prog/togetherbook/pages` to check `https_certificate.state`. If bad, in repo Settings → Pages, uncheck and re-check "Enforce HTTPS" to retry ACME. |
| Page stuck on "Loading…" forever | JS parse error preventing the data load to fire. The "+ New user" button being visible despite being admin-only is a tell. | DevTools Console will show the SyntaxError. Common cause: a duplicate `const` in `renderModalBody` from a sloppy edit. Fix and ship — the chain stays pending otherwise. |
| Person's data is right in payroll but wrong on the page | Page caches `staff.json` etc. Check the cache-bust query string (`?v=<unix-ts>` on CSS/JSON refs) bumps on every directory.html commit |
| Cross-tenant group action returns "Not Authorized" | `body.tenant` not injected (group not in `allGroups` yet, e.g. brand new group not in latest `groups.json`) | Re-run `refresh-groups.yml` then retry. The page reads `groups.json` to know each group's tenant. |

#### 11.1.11 Setup checklist (rebuilding from scratch)

If the Workers / KV / DWD ever need to be re-created:

1. **Service account** (one-time): `directory-reader@letme-directory.iam.gserviceaccount.com` — key JSON stored locally at `~/Desktop/wiki/letme-directory-f8cf5d0a941f.json` (gitignored) and pasted into both Workers' `GOOGLE_SERVICE_ACCOUNT_JSON` secret.
2. **DWD scopes** authorised in `admin.google.com` against the service account's numeric Client ID (in **both** Workspace tenants — letme + togetherloans):
   - `https://www.googleapis.com/auth/admin.directory.user`
   - `https://www.googleapis.com/auth/admin.directory.user.readonly`
   - `https://www.googleapis.com/auth/admin.directory.group`
   - `https://www.googleapis.com/auth/admin.directory.group.member`
   - `https://www.googleapis.com/auth/admin.directory.group.readonly`
   - `https://www.googleapis.com/auth/admin.directory.group.member.readonly`
   - `https://www.googleapis.com/auth/apps.licensing`
   - `https://www.googleapis.com/auth/gmail.settings.basic`
   - `https://www.googleapis.com/auth/gmail.settings.sharing`
3. **GitHub PATs** (one-time): fine-grained on the user `richmondbot2000-prog`, `Contents: read+write` on `togetherbook` only. Same token is reused by both Workers as `GITHUB_TOKEN` secret. **Watch for the `read`-only foot-gun**: if the token has `Contents: read`, the audit log, admin commits, and photo commits all silent-fail. Re-issue with write if you ever see "personal access token" 403s.
4. **Cloudflare API token** (one-time, for Access auto-sync): https://dash.cloudflare.com/profile/api-tokens → Custom token → permission `Account: Access: Apps and Policies: Edit` → scope to the togetherbook.net account. Paste into the workspace worker's `CLOUDFLARE_API_TOKEN` secret. Also save to `~/.togetherbook/cloudflare.json` (with the account ID) for ad-hoc CLI use by future Claude sessions.
5. **Cloudflare Workers** (two of them):
   - `apifk-workspace-worker2` — route `book.togetherbook.net/api/workspace/*`. Secrets: `GOOGLE_SERVICE_ACCOUNT_JSON`, `GITHUB_TOKEN`, `CLOUDFLARE_API_TOKEN`. Vars: `IMPERSONATE_USER` (`james.benamor@letme.co.uk`), `IMPERSONATE_USER_TOGETHERLOANS` (a TL super-admin). KV binding `PAYROLL_KV` → `apifk-payroll` namespace. Code: `worker/workspace-worker.js`.
   - `shiny-heart-00f8` (annotations) — route `book.togetherbook.net/api/annotations*`. Bindings: `GITHUB_TOKEN`. Code: `worker/annotations-worker.js`.
   - **Note:** `ADMIN_EMAILS` env var is **decommissioned** as of 2026-05-13. Don't re-add it. Admin list lives in `admins.json` in the repo.
6. **Cloudflare Access** application on the `togetherbook.net` zone for `book.togetherbook.net/*`. One Allow policy named `Letme staff + Directory admins` with include rules: `email_domain: letme.com` + an explicit `email` entry for every non-`@letme.com` admin. **Auto-synced** by the workspace worker on every `admin-add` / `admin-remove`. Do not edit manually after initial setup — the next admin change will overwrite.
7. **GitHub Actions secrets** for the hourly refresh: `WORKSPACE_SERVICE_ACCOUNT_JSON`, `WORKSPACE_DELEGATE_USER`, `FABRIC_CLIENT_*`, `SMTP_USERNAME` / `SMTP_PASSWORD` (for the monthly payroll-request email).

Full setup walkthroughs:
- `worker/SETUP.md` — annotations Worker (one-time, ~10 min)
- `worker/WORKSPACE_SETUP.md` — workspace Worker (one-time, ~20 min including DWD scope add)

### 11.2 TopUps page (`topups.html`)

Renders one chart and one table from `topups.json`. Lives under the **Reports** section in the nav.

**Definitions** (used in queries inside `scan_topups.py`):
- **Live loan** = LoanHistory snapshot where `(DateInArrearsUTC IS NULL OR DATEDIFF(day, DateInArrearsUTC, DateTimeUTC) < 90) AND CurrentBalance > 10`. The snapshot's own date is used so DIA-as-of-snapshot is correct for back-dated rows.
- **Primary loan** = a loan where `LoanAtInception.TopUpAmountAtInception IS NULL`. The customer's first loan.
- **Top-up loan** = `TopUpAmountAtInception IS NOT NULL`. The customer already had a live loan that was settled at this loan's payout.
- **Top-up eligible** (`TueStatus = 1` on a snapshot) = the live loan met the lender's TUE thresholds at the time the snapshot was written. Recalculated nightly by Daily Update + on every transaction by Mini Update + on every Whitebox run.
- **Top-up eligible AND logged into app** (added 2026-05-14) = subset of the above where the borrower's LoanbookId appears in `dbo.AppLoginSuccesses` at least once in the same month. The TL app is the only place a borrower can actually request a top-up, so this is the real on-ramp signal — the gap between the red and blue lines is latent revenue.

**Lender filter:** `LENDER_ID = 6` — Transform Credit / Together Loans (USA). Hardcoded in `scan_topups.py`. The script auto-discovers which warehouse table holds both `LoanbookId` AND `LenderId` columns (currently `Loan`) and uses it in a CTE pre-filter.

**Chart:** stacked SVG bars (ink-300 primary on the bottom, brass-300 top-up on top) plus a manuscript-red line tracking the TUE-eligible count plus a dashed ink-blue line tracking the TUE-eligible-AND-logged-into-app count. Hand-rolled SVG, no chart-lib dependency. ~5KB inline. Sample post-launch numbers (May 2026): ~16k TUE-eligible/month, ~10k of those logged in — ~60-65% on-ramp rate.

**Column-discovery quirk:** `AppLoginSuccesses` uses **`LoanBookId`** (capital B), while `Loan_History` uses **`LoanbookId`** (lower b). The scanner picks the column case-insensitively and aliases it inside the CTE so the outer JOIN stays clean. If the warehouse vintage doesn't have `AppLoginSuccesses` at all, the new series silently falls back to all zeros and the chart's blue line collapses onto the x-axis.

**Last-refreshed badge:** prominent cream paper plate near the top of the page, showing the snapshot timestamp in friendly format. Goes red if the snapshot is more than 36 hours old (allows a single missed overnight run).

### 11.3 Brandwatch page (`brandwatch.html`)

10 sources, each fetched independently; if a source fails the page shows an amber warning bar listing the broken sources but still renders the rest.

**Known sources blocked from cloud IPs:** Trustpilot (Cloudflare WAF; we use ScraperAPI render tier), BBB (also Cloudflare; ScraperAPI premium tier; **breaks if you also pass render**), Reddit (anonymous .json endpoint 403s; OAuth code path is wired and ready but Reddit's developer registration is impossible to complete via Google sign-in — abandoned).

**Brand precision filter:** every brand has a `precision_terms` allowlist; mentions must contain at least one to survive. `transform_credit` is intentionally strict (`transformcredit` and `transformcredit.com` only) because the two-word "Transform Credit" verb-phrase pollutes Google News results — fintech writing about "transforming credit agreement onboarding" passes any reasonable contextual gate.

**Security alerts band — collapsible lookalike list** (added 2026-05-14). The HIBP / Active-lookalikes / CT-log tile row stays as-is, but the "Active lookalike domains" list directly underneath is now wrapped in a `<details>` disclosure (`▸ Show N active lookalike domains`). The list is long and noisy (~26 DNSTwist permutations); collapsing it by default keeps the security band scannable and the page above-the-fold without dropping any data.

### 11.4 Payouts page (`yesterday.html`)

**Nav label is "Payouts"; file path is still `yesterday.html`** (kept stable so existing bookmarks survive). Renamed in the nav 2026-05-14 as part of the top-level restructure; page heading renamed from "Yesterday's payouts" to "Payouts" on 2026-05-15 when the three-range tab control was added.

**Range tabs.** A row of three italic-Newsreader tab buttons at the top of the page picks the dataset:

| Tab | JSON | Shape | Layout |
|---|---|---|---|
| Yesterday  | `yesterday-payouts.json` | per-borrower items[] (single day) | pin map + state-total map + state-avg map + per-state name tables (capped at 200 rows / state) |
| Last Week  | `payouts-week.json`      | per-borrower items[] (last 7 calendar days incl. today) | same as Yesterday — pin map + maps + tables |
| Last Year  | `payouts-year.json`      | aggregated (`by_state[]`, `by_month[]`, no items) | state-total map + monthly horizontal-bar chart (12 months, ranked by spend) + state-avg map + per-state summary cards (count + total + avg, no names) |

The active tab carries a manuscript-red rule + bold weight. The page renderer detects the dataset shape (`Array.isArray(data.by_state)`) and switches between the per-borrower layout and the aggregated layout. The Year dataset is intentionally aggregated server-side because a year of per-borrower rows (~50k @ ~150 bytes) would push the JSON over 7 MB and brick mobile Safari with that many DOM rows.

**Scanner + workflow.** Two scripts run from `.github/workflows/refresh-yesterday-payouts.yml` ("Refresh payouts") on the hourly 06:00–23:00 UTC schedule:

- `scripts/scan_yesterday_payouts.py` — single-day per-borrower scan (existing). Falls back to Friday on Sun/Mon.
- `scripts/scan_payouts_history.py` — week + year scan (added 2026-05-15). The Year query uses `DATEFROMPARTS` to compute the first day of the month 11 months before last month through the last day of last month (rolling 12 completed calendar months). Three result sets in one batch: per-state aggregates, per-month aggregates, and a single-row summary with the actual `MIN/MAX` of the payout dates landed within the window.

The workflow's guard is satisfied only when all three JSONs are current — yesterday-payouts.target_date == yesterday, payouts-week.range.to == yesterday, and payouts-year.json exists. A missing or stale history file forces a re-run, so the rolling 7-day window stays fresh every morning. The history scanner step is `continue-on-error: true` so a failure there can't block the yesterday refresh.

**Three Leaflet maps** wrapped with `position: relative; z-index: 0` so Leaflet's internal pane z-indexes (200–800) stay clamped and don't escape over the topbar / mobile drawer. The pin map drops a clustered pin per borrower (Yesterday + Week only); the state-total map labels each state's centroid with the total paid out; the state-average map (added 2026-05-12) labels each state's centroid with the average loan amount (popup shows the loan count + total so the mean is anchored to its denominator).

**Single-borrower marker** (fix 2026-05-14): the default Leaflet `L.marker()` was rendering as a broken-image + "Mark" alt text on single-borrower locations because Leaflet's image-path auto-detection misses unpkg's URL shape. Replaced with an inline-SVG `L.divIcon` (small green pin droplet matching the cluster palette) so there's no external image dependency. Cluster bubbles were unaffected — they use MarkerCluster's own DivIcon.

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

#### 11.5.8b LeadOutcomes-based canonical attribution (added 2026-05-12)

Confirmed with Kelly Black (Together Loans CEO) that **`Leads.LeadResultTypeId` is set at point of purchase but `dbo.LeadOutcomes` is the canonical lead-progress log.** Whitebox's `/UpdateStatus` writes one row per outcome event tagged with the specific `LeadId` that was live at the time. We refactored Part A of `scan_source_quality.py` accordingly:

**Old (broken):** grouped leads by ARef then took `MAX(CampaignId)` to pick a broker per ARef. Arbitrary attribution when the same ARef was sold by multiple brokers. Probe `probe_lead_attribution.py` measured this: **21% of ARefs in the 60d window were bought by 2+ brokers; ~3% of paid loans had ambiguous attribution.**

**New (canonical):** JOIN per-LeadId on `dbo.LeadOutcomes` and pivot the type column to derive cell-level funnel counts directly. No more MAX heuristic.

The new Part A query uses three CTEs:

```sql
WITH leads_w AS (
    SELECT l.LeadId, l.ARef, l.CampaignId, SR1_expr AS SR1, SR2_expr AS SR2
    FROM dbo.Leads l
    WHERE l.DateReceivedUtc >= @window_start AND l.DateReceivedUtc < @window_end
      AND l.LenderId = @lender AND l.LeadResultTypeId IN (1, 30)
),
outcomes AS (
    SELECT lo.LeadId,
           MAX(CASE WHEN lo.LeadOutcomeTypeID = 1 THEN 1 ELSE 0 END) AS reached_apply1,
           MAX(CASE WHEN lo.LeadOutcomeTypeID = 4 THEN 1 ELSE 0 END) AS reached_brw_signed,
           MAX(CASE WHEN lo.LeadOutcomeTypeID = 5 THEN 1 ELSE 0 END) AS reached_gt_passed,
           MAX(CASE WHEN lo.LeadOutcomeTypeID = 6 THEN 1 ELSE 0 END) AS reached_vc_completed,
           MAX(CASE WHEN lo.LeadOutcomeTypeID = 8 THEN 1 ELSE 0 END) AS reached_paid_out
    FROM dbo.LeadOutcomes lo
    INNER JOIN leads_w lw ON lw.LeadId = lo.LeadId
    GROUP BY lo.LeadId
),
app_loans AS (
    SELECT a.ARef, MAX(CAST(a.LoanAmount AS FLOAT)) AS LoanAmount
    FROM dbo.Applications a WHERE a.LenderId = @lender
    GROUP BY a.ARef
)
SELECT lw.CampaignId, lw.SR1, lw.SR2,
       SUM(ISNULL(o.reached_apply1, 0))       AS apply1,
       SUM(ISNULL(o.reached_brw_signed, 0))   AS brw_signed,
       SUM(ISNULL(o.reached_gt_passed, 0))    AS gt_passed,
       SUM(ISNULL(o.reached_vc_completed, 0)) AS vc_completed,
       SUM(ISNULL(o.reached_paid_out, 0))     AS paid_out,
       SUM(CASE WHEN o.reached_paid_out = 1 THEN ISNULL(al.LoanAmount, 0) ELSE 0 END) AS paid_loan_total
FROM leads_w lw
LEFT JOIN outcomes o ON o.LeadId = lw.LeadId
LEFT JOIN app_loans al ON al.ARef = lw.ARef
GROUP BY lw.CampaignId, lw.SR1, lw.SR2
```

**`LeadOutcomeTypes` enum (confirmed 2026-05-12 via `probe_lead_outcomes.py`):**

| ID | Description | Maps to our funnel as |
|---|---|---|
| 1 | Apply1 complete | `apply1` (denominator for canonical apply rate) |
| 2 | Budget plan complete | not currently surfaced |
| 3 | Payment details complete | not currently surfaced |
| 4 | BRW signed contract | `brw_signed` |
| 5 | GT passed checks | `gt_passed` |
| 6 | GT completed VC | `vc_completed` |
| 7 | Signed up to CB | not surfaced |
| 8 | **Paid out** | `paid_out` — the only outcome that earns |
| 9 | GT Added | not surfaced |
| 10 | GT Signed Contract | not surfaced |

**Output shape change** (2026-05-12): each `weak_accepted.by_broker_source[]` cell, each campaign in the drill-down, and each `sr2_breakdown[]` sub-cell now carry `apply1`, `brw_signed`, `gt_passed`, `vc_completed`, `paid_out` (counts) plus `paid_loan_total` (sum). The old `applications` field (which was effectively == `leads_purchased` and produced a meaningless ~100% apply rate) is removed. **`brokers.html`'s `_applyRate(s)` now reads `s.apply1`** for the canonical apply-rate signal.

**Verified 2026-05-12 first run after refactor:** 3,900 paid_out events attributed across 961 qualifying cells; matches the canonical 3,912 from the LeadOutcomes probe minus the 12 in Type 3 (PPC) campaigns excluded by design. Blended apply rate dropped from ~100% (broken pre-refactor) to **29.6%** (canonical post-refactor). Top "consider blocking" cells shifted: Search ROI / 550309 ($10,728/paid pre-refactor) dropped out of the top 5; Round Sky / 3565 ($4,959, 32% apply) took #1, characterised by the new "engaged but unfunded" signal.

#### 11.5.8c One-off diagnostic scripts

Two probe scripts + workflows live in the repo for re-runs if needed:

- `scripts/probe_lead_attribution.py` + `.github/workflows/probe-lead-attribution.yml` — sizes the multi-broker-ARef attribution gap and searches `INFORMATION_SCHEMA` for backing-table candidates. Manual trigger only.
- `scripts/probe_lead_outcomes.py` + `.github/workflows/probe-lead-outcomes.yml` — dumps the `dbo.LeadOutcomes` + `dbo.LeadOutcomeTypes` schemas, samples rows, and searches `INFORMATION_SCHEMA.VIEWS` across all 7 reporting DBs. Manual trigger only.

Run via `gh workflow run probe-X.yml --ref main`. Useful for any future re-investigation of attribution semantics or for finding new outcome types if the enum expands.

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

### 11.6 Pipeline page (`pipeline.html`)

March-cohort application-pipeline analysis with two d3-sankey diagrams (Lead funnel + Application progression). Powered by `scan_pipeline.py` + `scan_pipeline_samples.py`. Lives under the **Reports** section in the nav.

Per dead-end endpoint, the samples scanner pulls 25 random ARefs with their full interaction timeline (Tasks, Events, Messages, ESignatures, LeadOutcomes). Identity-links by `(FirstName, Surname, DOB)` so the timeline spans every application the same person made. **All PII masked server-side** — ARefs to last-5, surnames to `******`, DOB to `****-**-**`, addresses to state only, plus email/phone/SSN/card redaction in message bodies.

#### 11.6.1 Timeline event sources (per ARef)

| Event kind | Source | Notes |
|---|---|---|
| `application_started` | `dbo.Applications` | Label rewritten to `"Application started — sold to us by <Broker>"` once the Leads block resolves Lead → Campaign → Source → Broker friendly name. Falls back to `"(direct via website)"` for non-lead applications and `"(from purchased lead #N)"` only if the broker can't be resolved. |
| `task_completed` | `dbo.Tasks` (every completed task) | `who="BRW"` when `GtRef IS NULL`, `who="GT"` otherwise. Same-minute clusters of ≥4 tasks collapse into one `task_cluster` pseudo-event. |
| `signature` | `dbo.ESignatures` joined via `Applications.BrwEsignatureId / GtEsignatureId` | Labels `"BRW signed contract"` / `"GT signed contract"`. |
| `web_visit` | `dbo.WebBehaviours` | Body label via `format_web_event`. |
| `lead_presented` | `dbo.Leads` joined via `Applications.LeadId` | Label uses the resolved broker friendly name when available: `"Lead presented by <Broker>"`. Details include the lead's PII (name / DOB / address / amount / term / purpose / broker / campaign / result code). |
| `milestone` (added 2026-05-14) | `dbo.LeadOutcomes` joined via `LeadId` | Canonical Whitebox-driven funnel log (confirmed authoritative with Kelly Black 2026-05-12 — see CLAUDE.md §5). Type → label: `1` Apply 1 · `4` BRW signed (GT invited) · `5` GT accepted (passed credit/bank/ID) · `6` GT VC completed · `8` Paid out · others labelled by id. Rendered sage-green with a left-border on the page so funnel signposts pop out of the message stream. Pulled in addition to Tasks/ESignatures so GT-side milestones still appear when those tables miss attribution. |
| `message_in` / `message_out` | `dbo.Messages` | **Two-pass query** (added 2026-05-14): pass 1 filters by `ARef IN (...)` (catches every outbound + the small minority of inbounds with ARef set); pass 2 filters by `ExternalAddress IN (...)` against the family's phone/email map built from `Customers.Telephones` + `.Emails`. Without pass 2, ~98% of inbound messages disappear from the timeline. Dedup by `MessageId`. |

#### 11.6.2 Endpoint buckets

`furthest_endpoint(aref)` reads the per-ARef set of `(TaskTypeId, who)` pairs from `dbo.Tasks` (where `TaskTypeId IN (41, 48, 54, 62, 146)` and only completed):

| Stage check | True if |
|---|---|
| `has_apply1`   | `(41, BRW)` completed |
| `has_brw_sign` | `(48, BRW)` completed |
| `has_gt_pass`  | `(54, GT)` completed |
| `has_gt_vc`    | `(62, GT)` OR `(146, GT)` completed |
| `is_paid_out`  | `Applications.ApplicationStatusTypeId = 5` |

Bucket cascade (first-match-wins, paid_out → vc_ready → no_vc_reached → no_accepted_guarantor → dropped_before_brw_signed → abandoned_before_page1).

> Heads-up: the Tasks-driven bucket and the LeadOutcomes-driven timeline can disagree in edge cases (Whitebox writes the milestone but no matching Task row, or vice versa). LeadOutcomes is canonical for funnel attribution per the warehouse owner; the bucket logic still uses Tasks for now because that's what `scan_pipeline.py` aggregates on. Worth a follow-up to align both on LeadOutcomes.

### 11.7 Comms response time (`comms.html`)

Tracks how long customers wait for our reply across SMS and Email, split into four buckets by the customer's state **at the moment they sent the message**:

- `unknown` — sender has no ARef on the inbound row AND no phone/email match in the Customers table
- `applicant` — ARef set; no live loan; no signed-not-rejected guarantor at message time
- `live_loan` — LoanbookId tracks to a loan with balance > $10 and no arrears at the LoanHistory snapshot immediately before the message
- `arrears` — LoanbookId tracks to a loan with balance > $10 and arrears at the snapshot

The scanner: `scripts/scan_comms_response.py`. Workflow: `.github/workflows/refresh-comms.yml` (hourly :05).

**Identity back-fill — the load-bearing trick.** Inbound SMS/Email rows on `Communications.Messages` usually carry no ARef (the IMAP/SMS poller only extracts one if it can scrape the subject line). v1 of this scanner had 98% of inbounds collapsing into `unknown`. The fix: build phone→ARef + email→ARef maps from `Applications.Telephones` + `Applications.Emails` (~19 M phones, ~18 M emails) joined to `Customers.ARef`, then for every ARef-less inbound look the sender's ExternalAddress up. ~97% match rate. Phone matching uses last-10-digits to fold US country-code variants together.

**Reply pairing — locked algorithm (spec'd 2026-05-14).** For each inbound, the scanner finds the first SAME-CHANNEL outbound on `dbo.Messages` to the same `ExternalAddress` within 14 days, using a **positive list** of valid reply ClientTypes:

- `ClientType LIKE '%CRM%'` → **Replied by Human** (TogetherLoansCRM, TransformCreditCRM, etc.)
- `ClientType LIKE '%Responder%'` → **Replied by Robot** (RobotResponder, RobotResponders, AiResponder)
- **Anything else is IGNORED** — MessageFactory, UIVR, ApplyWebsite*, Whitebox, App, Dialler, Jack, internal monitors (LoanbookMonitorRobotV2, AutoPayoutProcessor, etc.). The search jumps past them as if they don't exist. The same outbound can serve as the reply for multiple waiting inbounds — each inbound is matched independently.

Channel pairing uses the dbo.Messages Description enum:

| Code | Meaning |
|---|---|
| 0 | InboundSMS |
| 1 | InboundEmail |
| 2 | InboundCall |
| 5 | OutboundSMS |
| 6 | OutboundEmail |
| 7 | OutboundCall |

SMS pairs to SMS (`0 → 5`), Email pairs to Email (`1 → 6`). Calls, letters, and push notifications never count as a reply.

Two variants captured per inbound so the page can toggle the Robot Responder filter without re-querying:
- `reply_all` — first CRM-or-Responder reply
- `reply_human` — first CRM-only reply

Beyond 14 days is treated as "no reply".

**Output schema (`comms.json`):**
```jsonc
{
  "year": 2026, "channels": ["SMS","Email"], "max_reply_minutes": 20160,
  "buckets": ["unknown","applicant","live_loan","arrears"],
  "totals_by_bucket": { "unknown": { "n_total", "n_reply_all", "n_reply_human" }, ... },
  "series": { "<bucket>": { "YYYY-MM-DD": { "n_total","n_reply_all","sum_reply_all","n_reply_human","sum_reply_human" } } },
  "samples": { "<bucket>": [ { "aref_last5","channel","received_at","message",
                                "reply_all": {"at","response_minutes","client_type","body"}|null,
                                "reply_human": {...}|null } ] }
}
```

**Chart UI:**
- Weekly Monday-anchored aggregation — daily noise (evenings, weekends) gets folded into one number per week.
- Y-axis **locked** to 0–336 h with 48-h tick increments. No auto-rescale on filter toggle, so visual comparisons across filter states are honest.
- Last 3 weeks hidden by default — replies can land up to 14 days late, so a week's stats only stabilise once every message in it is 14 d+ old.
- 4 line colours match the totals-card left borders: grey / blue / green / red.

**Two filter checkboxes drive the chart AND the sample list:**
- `Exclude Robot Responder` — chart uses `n_reply_human` / `sum_reply_human`; sample list shows `reply_human` (or "no human reply within 14 d")
- `Exclude messages that get no reply` (default ON) — chart drops unreplied; when unchecked, unreplied messages are capped at 14 d for the mean. Sample list applies the same filter.

**Sample messages.** Scanner picks 100 random inbounds per bucket from the FULL pool (replied + unreplied), redacts customer first/last names (looked up via Customers.ARef) plus regex scrubs for ARef-shape / LoanbookId-shape / phone / SSN / card / email. Only the last 5 chars of ARef are exposed. Page randomly picks 10 of the 100 on every load and on Shuffle click — no warehouse round-trip.

**Multi-database query plan.** Fabric warehouse items are separate connections so the scanner hits all three sequentially:
1. `ReportingCommunications` — single CTE pulls inbound + paired reply variants (~30 s for ~1 M rows).
2. `ReportingApplications` — phone/email→ARef maps + signed-GT check.
3. `ReportingLoanbook` — `dbo.Loan_History` for point-in-time loan state, chunked by LoanbookId.
4. `ReportingCommunications` again — sample bodies (one query for inbound bodies, 100×4×2 sequential reply lookups).

Total run-time ~17 min for the full pipeline; workflow timeout bumped to **45 min** on 2026-05-14 to absorb the new CSV-export tail. Two perf traps killed earlier runs that day, both fixed:

1. Re-fetching `ExternalAddress` per ARef-less message in chunks of 1500 took 26 min and busted the original 30-min timeout. Now `ExternalAddress` is in the first SELECT, no second pass.
2. `fetch_names_by_aref` was running 385 sequential 1500-element IN-clause queries to pull names for ~576k unique ARefs — ~13 min on the warehouse round-trip overhead. Replaced with one bulk `SELECT ARef, FirstName, Surname FROM Customers WHERE ARef IS NOT NULL` (~50 s); filter happens in Python.

**Audit-trail CSV (`comms-full.csv.gz`).** Linked from the page as the **Download full list CSV** button. The artefact on the server is **gzipped** (~77 MB; raw CSV is ~350 MB, well over GitHub's 100 MB file limit). The button click runs a `fetch + DecompressionStream("gzip") + Blob` pipeline so the user still gets a plain `.csv` save — no manual gunzip step. One row per inbound (no aggregation), PII-redacted to the same level as the on-page samples. Columns:

| Column | Notes |
|---|---|
| `inbound_datetime_utc` | UTC ISO `YYYY-MM-DD HH:MM:SS` |
| `inbound_channel` | `SMS` / `Email` |
| `inbound_body` | redacted (no length cap; names ****, ARef-shape ****, phone ****, etc.) |
| `result` | `Replied by Human` / `Replied by Robot` / `No reply` |
| `hours_to_reply` | blank when `No reply`; 2-decimal hours otherwise |
| `reply_datetime_utc` | blank when `No reply` |
| `reply_channel` | same as `inbound_channel` by spec; blank when `No reply` |
| `reply_client_type` | e.g. `TogetherLoansCRM`, `AiResponder`, `RobotResponder` |
| `reply_body` | redacted |
| `customer_aref_last5` | last 5 chars of ARef or blank |
| `customer_state_at_inbound` | `unknown` / `applicant` / `live_loan` / `arrears` / `other` |

The CSV is the auditable ground truth — every classification on the chart is one row here. The page sets the link's `?v=` query to the scan's `updated_at` so the browser never serves a stale CSV after a fresh refresh-comms run.

---

### 11.8 Wall page (`wall.html`)

Internal company social feed for the ~200 Cloudflare-Access-authenticated staff. Anyone signed in can post; reactions, comments, replies, link previews, and notifications all flow through the workspace Worker.

#### 11.8.1 Files

| Asset | Purpose |
|---|---|
| `wall.html` | the whole page — markup + CSS + JS in one file (~2,000 lines) |
| `wall.json` | append-only post store (FIFO-trimmed at 2000 posts) |
| `wall-seen.json` | per-user last-seen-at map (`{ by_user: { email: { posts: { id: at }, last_marked_at } } }`) |
| `wall-media/` | committed photo + video uploads (one file per attachment) |
| `worker/workspace-worker.js` | endpoints under `/api/wall/*` (see §11.8.4) |

#### 11.8.2 Storage model

`wall.json` shape:

```jsonc
{
  "schema_version": 1,
  "updated_at": "...",
  "posts": [
    {
      "id": "post_<ts36>_<rand>",
      "author_email": "...",
      "author_name": "...",
      "created_at": "<ISO>",
      "body": "≤10,000 chars",
      "photos": ["wall-media/img_xxx.jpg", "https://media.giphy.com/.../giphy.gif"],
      "channel": null | "channel-name",
      "reactions": { "👍": ["email1", "email2"], "❤️": [...] },
      "react_events": [
        { "actor_email": "...", "emoji": "👍", "target_kind": "post"|"comment",
          "target_id": "...", "at": "<ISO>", "kind": "added"|"removed" }
      ],
      "comments": [
        {
          "id": "com_<...>" | "reply_<...>",
          "parent_comment_id": null | "<comment-id>",
          "author_email": "...",
          "author_name": "...",
          "created_at": "<ISO>",
          "body": "≤2,000 chars (or empty if media only)",
          "photos": ["wall-media/...", "https://media.giphy.com/..."],
          "reactions": { ... }
        }
      ]
    }
  ]
}
```

Reactions are stored and displayed as **real emoji glyphs** (Facebook-style). Each glyph picks up a per-emoji CSS keyframe (`wl-thumb`, `wl-heart`, etc.) when the user hovers a like-button or it sits in a reaction pill. `REACTION_META` in `wall.html`:

| Storage key | Display label | Past tense (notifications) | Anim key |
|---|---|---|---|
| 👍 | Like      | liked              | `thumb` |
| ❤️ | Love      | loved              | `heart` |
| 😂 | Haha      | laughed at         | `haha`  |
| 😮 | Wow       | was wowed by       | `wow`   |
| 😢 | Sad       | was sad at         | `sad`   |
| 🎉 | Celebrate | celebrated         | `party` |

Tapping the action-bar Like applies the default 👍; **hovering** the Like button opens the picker (touch long-press is the fallback). Tap the same Like again to remove the reaction; tap a different emoji to switch.

#### 11.8.3 Feed layout — compact tiles, single-expand, pagination

The feed defaults to **compact tiles**, with one post fully expanded at a time. Tapping anywhere on a compact tile expands it (and collapses any other expanded post). This is the load-bearing layout decision — without it, a single 10,000-char post + 10 photos can fill the entire viewport.

| State    | Renderer                | Content shown |
|----------|-------------------------|---------------|
| Compact  | `renderPostCompact`     | head; body teaser capped at 250 chars; photo strip (max 4 thumbnails at 56 px + "+N" overflow); YouTube embed / OG link-preview if any; reaction + comment counts; most recent today-comment teaser (100 chars); action bar |
| Expanded | `renderPostExpanded`    | head; full body with line-clamp + "See more"; full photo grid; YouTube embed / OG link-preview; reaction pills; action bar; comment composer; all comments with reply composers |

Click-to-expand uses `data-expand-card` on the article + `data-stop-card-click` on action-bar / trash / link descendants. The `keydown` handler accepts Enter/Space when the article itself is focused (role=button + tabindex=0). After expanding, the post is `scrollIntoView({ block: "nearest" })`.

**Pagination:** 20 posts per page (`POSTS_PER_PAGE = 20`). A `<nav class="wl-pagination">` strip at the bottom of the feed renders `← Newer / Page N of M / Older →` italic-Newsreader buttons. Composing a new post resets to page 0 so the author sees their own post. Switching pages clears `expandedPostIds`.

**Sorting and comments:**
- Posts sort by most-recent-activity (max of `created_at` and every `comment.created_at`) descending.
- Comments sort ascending (oldest → newest); newest 2 shown by default + "View N earlier comments" toggle in expanded view.
- Replies sort ascending under each comment. One level of nesting only.
- Compact teasers use a Unicode `…` ellipsis on word boundaries (no clamp).
- Expanded full-body uses CSS `-webkit-line-clamp: 4` with a `<button class="wl-see-more">See more</button>` toggle.

**Notifications:** for the post author, every new (a) comment, (b) reply, or (c) "added" `react_event` whose `at` is greater than `wall-seen.json → by_user[viewer] → posts[postId]`. Bell badge at top-right + red `(N)` next to the **Wall** topbar link. Mark-all-read calls `/api/wall/mark-seen` which stamps each owned post id with the current timestamp.

#### 11.8.4 URL handling — linkify, YouTube embed, OG link previews

The Wall renderer treats `http(s)://` URLs inside a post body in three layered ways:

1. **Linkify in body text** — `linkifyBody()` wraps every URL in `<a class="wl-post-link" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation();">`. The `stopPropagation` is critical: without it a URL click would bubble to the compact card's expand handler.
2. **YouTube embed** — `matchYouTube()` recognises `youtu.be/<id>`, `youtube.com/watch?v=<id>`, `/embed/<id>`, and `/shorts/<id>`. Matches render a 16:9 `<iframe>` against `youtube-nocookie.com/embed/<id>` with `loading="lazy"`, `allowfullscreen`, and `referrerpolicy="strict-origin-when-cross-origin"`. Renders in both compact and expanded view.
3. **OG link-preview card** — non-YouTube URLs render a `<a class="wl-link-preview" data-link-preview="<url>">` card with a 144×96 thumbnail + host overline + 2-line clamped title + 2-line clamped description. `hydrateLinkPreviews()` walks every `[data-link-preview]` after each `renderFeed`, fetches `/api/wall/link-preview` once per URL, caches the response in an in-memory `linkPreviewCache`, and re-paints every mounted card for that URL.

Up to **3 distinct URLs per post** render their own block (deduped). The loading state is a `Loading preview…` italic placeholder.

#### 11.8.5 Share + deep-links

The action bar carries **Like / Comment / Share** on BOTH the compact and expanded views. Share copies `${location.origin}/wall.html#post-<id>` to the clipboard (`navigator.clipboard.writeText` with a `prompt()` fallback for browsers that block clipboard writes). On page load, if `location.hash` matches `#post-<id>`:

1. Find the post in the sorted feed.
2. Set `currentPage = Math.floor(idx / POSTS_PER_PAGE)`.
3. Add the post id to `expandedPostIds`.
4. After the first render, `scrollIntoView({ block: "start" })` on the matching `<article>`.

#### 11.8.6 Worker endpoints (`/api/wall/*`)

All endpoints require a Cloudflare Access JWT. Identity is the `Cf-Access-Authenticated-User-Email` header. Optional `Cf-Access-Authenticated-User-Name` improves the stored display name; falls back to email local-part. Ten endpoints total:

| Path | Body | Returns |
|---|---|---|
| `whoami` (GET-or-POST) | — | `{ email, name }` |
| `post` | `{ body, photos[], channel? }` | `{ post }` |
| `comment` | `{ post_id, body, parent_comment_id?, photos[] }` | `{ comment }` |
| `react` | `{ parent_id, parent_kind: "post"|"comment", emoji }` | `{ post_id, target_kind, reactions }` (toggles) |
| `mark-seen` | `{ at }` | stamps every post the viewer authored to `at` in `wall-seen.json`; returns `{ marked }` |
| `upload-media` | `{ data_url, kind: "photo"|"video"|"gif" }` | `{ path, kind, mime }` writes to `wall-media/<id>.<ext>` |
| `gif-search` | `{ q, limit }` | `{ results: [{id, title, preview, url}] }` proxies GIPHY (was Tenor — Google discontinued the Tenor API for new clients Jan 2026) |
| `delete` | `{ kind: "post"|"comment", id, post_id? }` | author-or-admin only; cascades to a comment's replies |
| `link-preview` | `{ url }` | `{ ok, url, host, title, description, image, site_name }` |

**`link-preview` specifics:**
- SSRF guard: refuses `localhost`, `127.*`, `10.*`, `192.168.*`, `169.254.*`, `0.0.0.0`.
- Upstream timeout: 8 s (`AbortController`).
- Response cap: 1 MB streamed read (OG meta lives in `<head>` so capping is safe).
- Meta extraction order: `og:title` → `twitter:title` → `<title>`; description from `og:description` → `twitter:description` → `<meta name="description">`; image from `og:image` → `twitter:image`; site name from `og:site_name`.
- HTML-entity decode handles `&amp; &lt; &gt; &quot; &#39; &#x27; &#NNN;` so titles render cleanly.

Both the wall.json store and wall-seen.json use the shared `updateGhJson` helper with **retry-on-409** so concurrent writers can't lose data. UTF-8 decode on read (atob → `TextDecoder('utf-8')`) so emoji round-trip cleanly.

#### 11.8.7 Worker secrets

- `GITHUB_TOKEN` — Contents API write (same token as `apifk-workspace-worker2`)
- `GIPHY_API_KEY` — only needed for the GIF picker; everything else still works without it

#### 11.8.8 Cloudflare routing

Two routes hit `apifk-workspace-worker2`:
- `book.togetherbook.net/api/workspace/*` (existing — Directory actions)
- `book.togetherbook.net/api/wall/*` (added for Wall)

Worker dispatches based on URL path inside `fetch()`.

#### 11.8.9 Media URL handling

`wall-media/*` paths in the repo are gated by Cloudflare Access on `book.togetherbook.net` (Access redirects bare `<img src>` requests to the login page → broken-image icon). All client-side renderers run paths through `mediaUrl(path)` which either:
- passes absolute URLs (GIPHY) through, or
- rewrites repo-relative paths to `https://richmondbot2000-prog.github.io/togetherbook/<path>` (the intentionally-public GitHub Pages mirror).

#### 11.8.10 Quiet Edition design conformance

The Wall went through a senior design review (commit `Wall v4`) to fit Storybook Ledger / Quiet Edition:

- Posts render as hairline-divided rows (no card chrome). The compose box is the only true card on the page; brass-500 left rule, no eyebrow (the "a new page" overline was removed 2026-05-15).
- Compose placeholder reads "Tell the team something they should know" (italic Newsreader, `--ink-500`).
- Post top/bottom hairlines use `--ink-500` (`#6B7794`, a 50%-luminance grey-blue) — replaced the original `--divider` (12%-opacity ink) which read as light brown against cream paper.
- Body in Newsreader (`--font-display`) 16/15 px @ 1.55 line-height — editorial voice consistent with the rest of the site.
- Timestamps render as italic uppercase overlines (`WED, 15 MAY 2026 · 09:36`) wrapped in `<time datetime>`.
- Reactions render as real emoji glyphs (Apple emoji on cream paper). Hovering Like opens a 6-emoji picker via paired show/hide timers + outside-click listener with anchor exclusion.
- Action bar = `Like` (thumb-icon, switches to the selected emoji when active) + `Comment` (comment-icon) + `Share` (share-icon). Three-column grid above which a hairline `--ink-300` rule sits.
- Empty state: `❦` fleuron + italic "No pages yet. Write the first.".
- Loading state: italic "Reading the wall…".
- All modals (confirm, GIF picker, lightbox) drop box-shadow / border-radius and use brass left-rule + italic-button affordances.
- Avatars carry a 1px brass-300 ring; initials fall back via attached `error` listener (not inline `onerror`).
- All touch targets ≥44 pt; focus rings pass WCAG 2.2 3:1.

---

### 11.9 Holidays page (`holidays.html`)

Internal HR-lite calendar for the ~200 staff. Each user logs in via Cloudflare Access, sees a personal fiscal-year (1 April → 31 March) calendar, and can mark each day with one of 8 statuses. Admins + line managers can edit other people's days.

#### 11.9.1 Files

| Asset | Purpose |
|---|---|
| `holidays.html` | the whole page (markup + CSS + JS in one file) |
| `holidays.json` | per-user days + audit log |
| `annotations.json` | contains the `line_manager` field per user (shared with Directory) |
| `worker/workspace-worker.js` | `/api/holidays/*` route — see §11.9.4 |
| `worker/annotations-worker.js` | accepts `line_manager` in the field whitelist |

#### 11.9.2 Storage model

`holidays.json`:

```jsonc
{
  "schema_version": 1,
  "updated_at": "...",
  "year_start": "2026-04-01",
  "year_end":   "2027-03-31",
  "by_user": {
    "user@email.com": {
      "days": { "2026-04-15": "holiday", "2026-04-16": "office", "..." : "..." }
    }
  },
  "log": [
    { "user_email": "...", "date": "2026-04-15",
      "from": null|"<status>", "to": null|"<status>",
      "changed_by": "...", "changed_at": "<ISO>" }
  ]
}
```

Log is FIFO-trimmed at 5000 entries.

**Status keys** (stored short tokens):

| Key | Label | Default colour | Notes |
|---|---|---|---|
| `office` | Worked at Office | green `#c7e7b8` | implicit default for Mon-Fri non-BH |
| `wfh` | Worked from Home | green `#b7dfc9` | |
| `non-working` | Non-Working | grey `#d8d8df` | implicit default for Sat/Sun |
| `holiday` | Paid Holiday | yellow `#f3d97a` | implicit default for UK BH on weekday |
| `half-am` | Half Day – Morning | top-half light-green, bottom-half office-green (CSS gradient) | |
| `half-pm` | Half Day – Afternoon | top-half office-green, bottom-half light-green (CSS gradient) | |
| `sick` | Sickness | red `#f5b5b5` | |
| `maternity` | Maternity | yellow `#f3d97a` | |
| `approved-holiday` | Approved Holiday | yellow + 1px `#b08a18` inner border | **manager-only** — admin OR target's line manager. Self-edit cannot apply it. |

#### 11.9.3 Page layout

- Header row carries the H1, the admin-only person dropdown, and a holiday-day count (sums `holiday` + `maternity` + `approved-holiday` overrides at +1, half-day overrides at +0.5, and default UK BHs on weekdays at +1).
- Legend strip below the header (Quiet-Edition swatches matching the table above). Each status hue is in its own family (sage / teal / amber / red / mauve / brass / slate) so two never get confused.
- When the viewer has ≥1 direct report, a **My calendar / Team — N** tab strip appears below the legend. Tab visibility updates dynamically — clicking Team re-fetches `annotations.json` from raw GH so a freshly-set Line Manager appears without a reload; a window-focus refresh (30 s debounced) catches the same case on tab-return.
  - **My calendar** view: 52 weekly rows × 7 day cells (Sun → Sat), 100 px sticky week-label column on the left. Cells are ~64 px tall. Click any in-year day → popover picker. Day numerals flip to paper (`#FDFBF4`) on saturated status fills for legibility.
  - **Team** view: a **Sunday-anchored calendar grid**. ONE shared DOW header at the top (Week of · Sun Mon Tue Wed Thu Fri Sat). Beneath it, 52-ish week blocks, each carrying its own date strip and one row per direct report. Day-of-week columns are fixed; months stagger so the 1st of each month falls in whichever DOW column it actually occupies (rendered with the month abbreviation under the date number). Every cell sits on `--paper-50` inside an `--ink-300` grid container with a 1 px gap so cell-to-cell separation is consistent regardless of adjacent colour. Scanning down a column shows e.g. "all of Sarah's Tuesdays" stacked.
- Status picker popover sits over the clicked cell. Options are the 8 self-settable statuses + (manager edits only) **Approved Holiday** + a "Reset to default" item that deletes the override.
- Change log section at the bottom shows every change as `<time> · <date> · <from> → <to> · by <name>`. In Team view, log entries label both the target person and the actor explicitly; filter is the union of (viewer's own days) ∪ (changes the viewer made to a direct report's day).
- Edits are optimistic — paint the new cell, POST `/api/holidays/set`, roll back + banner on error.

#### 11.9.4 Worker endpoints (`/api/holidays/*`)

Routed inside the shared `workspace-worker.js`. All endpoints require a Cloudflare Access JWT.

| Path | Body | Behaviour |
|---|---|---|
| `whoami` (GET/POST) | — | `{ email, name }` mirror of `/api/wall/whoami` |
| `set` (POST) | `{ email, date: "YYYY-MM-DD", status: "<key>"|null }` | Writes the day; appends log entry. `status=null` clears the override. Author-or-admin-or-line-manager only. Manager-only statuses (`approved-holiday`) refused for self-edits. |

`fetchLineManagers()` (helper inside the worker) reads `annotations.json` directly from `raw.githubusercontent.com` so the manager-of map is current within seconds of an annotation save (the GitHub Pages copy lags 30-60 s).

#### 11.9.5 Line Manager (Directory)

The "Line Manager" field is part of the per-user annotation, added to:
- The Directory Edit Card's Contact section. Edit reveals a `<datalist>`-backed email input listing every non-suspended staff member except the user being edited.
- `annotations-worker.js`'s field whitelist (`setScalar("line_manager", ...)` — clear-on-empty semantics match phone / address).

The Holidays page reads `annotations.json` from raw GH on every load and inverts the map to compute `directReports = { email | annotations[email].line_manager == viewer }`.

#### 11.9.6 Defaults + UK Bank Holidays

A baked list of 9 UK BHs for the 2026-04 → 2027-03 fiscal year (Good Friday, Easter Monday, Early May, Spring, Summer, Christmas Day, Boxing Day substitute on Mon 28 Dec, New Year's Day, Good Friday 2027). Stored as a `Set` of ISO dates in `holidays.html`. When a fiscal year rolls over, update the list — there's no programmatic Easter calculator on the page.

#### 11.9.7 Quiet Edition conformance

- Cream paper everywhere; no card chrome on the calendar grid; hairline `--ink-300` dividers.
- Newsreader italics for buttons + tab labels; JetBrains Mono for date numerals and meta lines.
- Active tab carries a manuscript-red rule + bold weight, mirroring the Payouts page selector.
- Status colours are the only saturated hues on the page; everything else is paper/ink/brass.

#### 11.9.8 Activity tab + D1-backed drill-down

The Activity tab on the Holidays page lets a manager see when each of their direct reports was actually doing work — split into 15-minute UTC buckets across the current month. Data is **permanent**, not refreshed-on-load: each day is pulled once at 12:17 UTC the day after it happens (via `refresh-staff-activity.yml`), then never changes unless an admin clicks **Refresh data**.

**Storage**: Cloudflare D1 database `apifk-activity` (UUID `cb23afae-…`) bound to `apifk-workspace-worker2` as `ACTIVITY_DB`. Three tables:

| Table | Grain | Notes |
|---|---|---|
| `activity_buckets` | `(email, iso_date, bucket)` | One row per active 15-min slot. ~34k rows today. |
| `activity_events` | `(email, iso_date, bucket, src)` | Per-source aggregate — writes / first_at / last_at / kind. ~49k rows. Powers the slot list. |
| `activity_items` | `(email, iso_date, bucket, src, record_id)` | **Per-message detail** for comm sources. Carries `body_excerpt` (first 500 chars of `MessageBody`), `comm_type` (SMS/Email/Call from the `Description` enum), `client_type`, `client_username` (= `ExternalAddress` — phone or email of the client), `campaign_name`, `auto_processed`. Powers the drill-down. |
| `activity_pulled` | `iso_date` | Audit log — when each date was last refreshed and from which source. |

**Reads** (worker, both Cf-Access-gated):
- `GET /api/workspace/activity?from=&to=&emails=` — slot grid + per-source events for the date range. Authorisation: viewer always; line manager → their direct reports; admin → anyone.
- `GET /api/workspace/activity-items?email=&date=&bucket=[&src=]` — drill-down rows for one (email, date, 15-min bucket). Same auth model.

**Writes**: the scanners run in GitHub Actions, not in the worker. Three steps in `refresh-staff-activity.yml`, all `continue-on-error: true` so a transient warehouse blip doesn't break the others:

1. `scan_staff_activity_buckets.py` — pulls every `ClientUsername`-bearing table across the 7 reporting DBs, aggregates into per-15-min buckets, merges into `staff-activity-buckets.json` (legacy path — to be retired once the worker exclusively reads from D1).
2. `scan_google_workspace_activity.py` — login/gmail/drive/meet/chat/calendar/admin from the Workspace Reports API. Merges into the same JSON with `kind: "google"`.
3. `scan_comm_items.py` — per-row pull of `ReportingCommunications.dbo.Messages` (filtered to `Description IN (5,6,7)` — outbound SMS/Email/Call) and uploads to D1's `activity_items` table. Wipes the window first (DELETE per iso_date) for idempotency, then batched `INSERT OR REPLACE` (batch size auto-set to stay under D1's 100-var statement cap).

**Admin "Refresh data" button** (top-right of the Activity tab, visible only to admins) → `POST /api/workspace/refresh-activity` → worker dispatches `refresh-staff-activity.yml` via the GitHub API with the current month as `start_date`/`end_date`. Takes ~1–2 minutes to land.

**UI** — see `holidays.html` `renderActivity()` + `toggleSlotExpand()`:
- The grid: one row per direct report, one box per day, 96 horizontal ticks (each = 15 min). Click a day → focused panel below the grid lists every active slot for that day.
- Click a slot row → inline expansion underneath fetches `/api/workspace/activity-items?…` and renders the per-message detail. Comm rows show coloured pills (SMS / Email / Call / Auto) + meta line (`ClientType`, `Client`, `Campaign`) + body excerpt. Non-comm rows fall back to "aggregate only — comm sources are seeded on the next daily refresh" because no per-row detail is captured for them yet.

**Required GH secrets** for the comm-items scanner: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `D1_ACTIVITY_DB_ID` (plus the existing `FABRIC_CLIENT_SECRET`).

**Known limits**:
- Inbound comms (`Description IN 0,1,2`) aren't currently captured — the agent identifier isn't stored on inbound rows in `Messages`. If the user wants "Comms read" tracked, that's a separate signal (e.g. mailbox read receipts, CRM open events) — not blocked but not built.
- Drill-down for non-comm sources (loanbook.Events, applications.LeadOutcomes, etc.) returns an empty list with a "coming soon" hint. To extend, generalise `scan_comm_items.py` into a multi-table scanner that emits one item-row per warehouse-row with table-specific labels.

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

1. Edit the file in `~/Desktop/togetherbook/`
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
ROOT = Path('/Users/richmondrobot/Desktop/togetherbook')
v = str(int(time.time()))
pages = ['index.html','apis.html','robots.html','yesterday.html','brandwatch.html',
         '1stcontact.html','directory.html','database.html','stats.html','topups.html',
         'brokers.html','pipeline.html']
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
gh workflow run refresh-topups.yml --repo richmondbot2000-prog/togetherbook
gh run list --workflow=refresh-topups.yml --repo richmondbot2000-prog/togetherbook --limit 1
gh run watch <run-id> --repo richmondbot2000-prog/togetherbook --exit-status
git pull --rebase
```

The `workflow_dispatch` trigger bypasses the same-day guard so the refresh always runs work, useful after a column-name fix or a data-source change.

### 15.2 Refresh ALL data immediately

```sh
for w in refresh-yesterday-payouts refresh-row-counts refresh-brandwatch \
         refresh-1st-contact refresh-directory refresh-staff-activity \
         refresh-topups; do
  gh workflow run "$w.yml" --repo richmondbot2000-prog/togetherbook
done
```

Useful after a global change (e.g. updating a shared filter or reformatting JSON output schemas).

### 15.3 Rotate a GH Actions secret

```sh
# JSON-shaped secrets (Workspace service account)
gh secret set WORKSPACE_SERVICE_ACCOUNT_JSON \
  --repo richmondbot2000-prog/togetherbook < /path/to/key.json

# Plain-string secrets (most others)
printf 'NEW_VALUE' | gh secret set FABRIC_CLIENT_SECRET --repo richmondbot2000-prog/togetherbook
```

After rotation, manually trigger any affected workflow to confirm it still runs.

### 15.4 Inspect why a workflow run failed

```sh
gh run list --workflow=refresh-topups.yml --repo richmondbot2000-prog/togetherbook --limit 5
gh run view <run-id> --repo richmondbot2000-prog/togetherbook --log-failed | tail -40
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
2. Copy across: `cp ~/Desktop/wiki/Overview/06_Database_Schema.md ~/Desktop/togetherbook/database.md`
3. Cache-bust + commit + push.

(Currently this is manual. Could be automated with another GH Actions workflow that watches the wiki repo, but the schema rarely changes — manual is fine.)

### 15.8 Replace the Quiet logo

1. Drop the new transparent PNG into `~/Desktop/togetherbook/togetherbook-logo.png` (overwriting).
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

## 16.5 Reporting audit — 2026-05-16

A 6-agent parallel audit traced every report's metrics back to the warehouse tables looking for misleading interpretations. Twelve bugs fixed and shipped; six deeper issues disclosed on the page pending DB-level verification before scanner rewrites. Detail:

**Fixed**
- **Brokers**: "Blended apply rate" KPI was hardcoded to 0.0% because it read a field removed on 2026-05-12 (`s.applications`). Switched to `s.apply1`. Per-broker `apply1_rate` denominator changed from `applications` to `leads_purchased` to match the handbook's canonical definition + the AutoBlock ≤10% threshold.
- **TopUps**: 9 leading months of phantom-zero `tue_eligible` / `live_topup` (Jun 2024 – Feb 2025) are now trimmed from the chart with an explanatory footnote — those columns weren't populated on `Loan_History` rows before ~Mar 2025, so rendering them as 0 falsely told the user the TUE programme started then. Current month renders striped bars + dashed final line segment + hollow dots + `MTD` table tag.
- **Comms response**: Robot Responder excluded by default (bot auto-acks within seconds made raw curves look magically fast). Default metric flipped Mean → Median.
- **Brandwatch**: contextual-pairs filter now applies to news-style sources (Google News, YouTube, CourtListener) — was silently dropping every legit news mention. Workflow's commit step gated on `success()` so a Send-email failure no longer marks un-emailed mentions as already-emailed.
- **Pipeline**: VC-ready stage now uses one `COUNT(DISTINCT ARef)` over task types 62 + 146 — was summing per-task DISTINCT counts and double-counting ARefs completing both variants. `leads_purchased` now includes `LeadResultTypeId = 30` (Pre-check passed) so the rejection-bucket totals reconcile with `leads_presented`.
- **Directory**: sibling Workspace accounts (same person across multiple tenant domains) no longer collapse into one row — per SPEC §11.1.5 they should be two rows so per-tenant activity stays visible. The alias-domain normalisation (clearloans → letme) was making the merge fire unintentionally.
- **`scan_staff_activity.py`**: added `togetherloans.com` + `tando.dk` to `KNOWN_DOMAINS`; relaxed the agent-username SQL filter to also accept bare-local-part identifiers (Communications.Messages stores CRM agents this way). Without the relaxation every comm sent by a CRM agent was invisible to the 60-day rollup, making real agents show as inactive on the Directory page. `active_count` 95 → 97 on the next run.
- **`refresh-staff-activity.yml`** job timeout 15 → 20 min — the Google Workspace merge step was hitting the cap and being killed.

**Flagged for follow-up (deferred — disclosed on the page)**
- `scan_brokers.py` Q3/Q4 (paid_out via `ApplicationStatusTypeId = 5` + `MAX(CampaignId)` attribution): should use `LeadOutcomes.LeadOutcomeTypeID = 8` and join via `LeadOutcomes.LeadId` like `scan_source_quality.py` already does. ~3% paid-loan mis-attribution.
- `scan_pipeline.py` Q4 (paid_out — same pattern). Also: cohort defined by `InterestingDateTimeUtc` (last status-update timestamp) rather than creation date, so cohort membership is unstable.
- `scan_yesterday_payouts.py` + `scan_payouts_history.py`: UTC `GETDATE()` vs `LoanAgreementDateLocal` (US-Eastern) gives one-day drift on early-US-morning runs. Also: ignores transaction reversals + `Cancelled`/`DNL`/`FraudRisk` flags so paid-out volume is overstated.
- `scan_brandwatch.py`: archived_reviews carry every TP review ever scraped — the 354-mention headline's 83% Trustpilot share is a storage artefact, not a real-world mix.
- `scan_pipeline_samples.py`: identity-link by `(FirstName, Surname, DOB)` alone is collision-prone at US lending scale; should require phone or email as a second key.
- `scan_brokers.py` apply1: sourced from a Tasks join that excludes ARefs not yet in `Applications`, inflating the "Ghost (no Apply 1)" share for brokers with recent volume — should pull from `LeadOutcomes`.

---

## 17. Pending / blocked work

| Item | Status | Blocker |
|---|---|---|
| Holidays v2 design | Background design agent commissioned 2026-05-15 | Sub-agent (research + ship a `holidays-v2.html` prototype alongside v1) was launched overnight. Review the resulting file + research doc in the morning; decide whether to swap it in. |
| `rgroup.co.uk` Workspace as second Directory tenant | Pending Ben Gardner's DWD setup confirm | Set up wired locally and `WORKSPACE_TENANTS` secret is configured. Ben (Super Admin on rgroup.co.uk Workspace) authorised the service account but the test fetches were still 403ing as of last check. Resume by triggering `refresh-directory.yml` and checking `fetch_errors[]` in the resulting `staff.json`. |
| Per-API response time + call count on home page | Plan ready, not built | Awaiting Kamran Kamaei's `Reader` access on the rgcore Azure subscription's Application Insights resources. |
| Type 1 (CPF) rate=0.10/0.12 ambiguity | Awaiting user clarification | A handful of campaigns have rate 0.10/0.12 and look mis-typed (probably rev-share entered under CPF). |
| Null-SR1 buy-vs-wait economic decision | Diagnosed not decided | Needs the price of a null-SR1 lead from the upstream broker. |
| Humand integration | Plan ready, not built | Awaiting Humand support's Public API key. |
| Live code-line stats | Manual snapshot in place | Either deploy `azure-function-stats/` (blocked on Azure admin) or build a `refresh-code-stats.yml` workflow. |
| Reddit OAuth | Code wired, abandoned | Reddit dev registration impossible via Google sign-in. ScraperAPI fallback is in use. |
| UK Bank Holidays after 2027 | Hard-coded for 2026-04 → 2027-03 | When the fiscal year rolls over, append next year's BHs to the `UK_BANK_HOLIDAYS` Set in `holidays.html`. |
| Sanitisation pass | Not done | Cloudflare Access reduces urgency but internal hostnames + payment partners + employee email pattern are still in the public-mirror copy. |

---

## 18. Cross-references

- `CLAUDE.md` (this repo) — working-style notes + the nightly-update directive for Claude sessions
- `CLAUDE_CONTEXT.md` (in the wiki repo) — operational notes for AI-assisted iteration; carries pending work and lessons learned in more conversational form
- `~/Desktop/wiki/Overview/07_TogetherBook_Site.md` — the wiki-wide spec entry for this site, integrated alongside other Central Services docs. **Keep this in structural sync with SPEC.md.**
- **`~/Desktop/wiki/partnerships-handbook.html`** — the Together Loans Partnerships Handbook. **Authoritative source** for partner terminology, commission types, AutoBlock thresholds, CPA target, dedup rules, scorecard semantics, and the partnership team's report library. Read this before doing anything substantive on the Brokers page or source-quality analysis.
- `~/Desktop/wiki/TogetherBOOK_handoff/wiki/README.md` — the original Quiet Edition design handoff package
- `~/Desktop/wiki/Markdown/*` — service-by-service Tettra exports, useful when adding new platform-aware pages

---

_End of spec. If you're adding a new page or workflow that isn't covered above, please update this file in the same PR._
