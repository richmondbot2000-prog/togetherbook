# CLAUDE.md — How to work with this user on this repo

_This file is auto-loaded into every Claude Code session in this repo. Read all of it. The accompanying **SPEC.md** is the deep operational manual — read it on first contact to understand the site._

---

## 1. Who you're working with

A non-technical user at Richmond Group (`richmondbot2000@gmail.com`). Works on Central Services — the platform that powers Transform Credit and the other Richmond Group lenders. **They do not write code.** They have strong product instincts and will direct strategy; you handle implementation.

They use the site (`book.togetherbook.net`) as an operational dashboard and as a living explanation of how their platform works.

## 2. Working style — non-negotiable

- **Be terse.** One sentence per update is almost always enough. End-of-turn summaries should be one or two sentences max.
- **Ship to live immediately.** This repo's `main` branch deploys via GitHub Pages — every commit goes live within 30-60 seconds. The user expects you to commit + push your changes within the same turn, not stage them for later review.
- **Overnight work is welcome.** When the user says "I'm going to bed" or "do work while I'm gone", default to ambitious — multiple substantive commits over the night, each with a clear commit message they can scan in the morning. Don't wait for permission, don't write status files; let commit history be the log. Avoid scanner changes that could break the daily 07:05 source-quality refresh without being verified — UI/copy/docs changes are safer overnight.
- **Don't over-ask.** Before asking a clarifying question, spend up to a minute on read-only investigation (grep, file reads). A specific question after research beats a vague one upfront. When you do ask, AskUserQuestion with concrete options beats prose.
- **They correct bluntly when wrong.** Take it on the chin and adjust. Don't apologise or rehash — just incorporate the correction and continue.
- **Avoid emojis** in code, comments, commits, page copy, or anywhere on the site unless explicitly requested.
- **No filler.** "Working on it…" is not useful. Either do the work or state the specific obstacle.

## 3. Design philosophy — every element teaches

The user has rejected decorative-only design choices multiple times. Every UI element on this site must communicate something — an icon must teach, a colour must categorise, a chart axis must inform. If you can't articulate what an element teaches the viewer in one sentence, the user will probably ask to remove it.

The visual treatment is the **Quiet Edition** — cream paper + ink blue + brass + sparing manuscript red. Editorial / antique-book feel. No box-shadows, no transforms on hover, only colour transitions. Tokens are in `quiet-tokens.css`. See SPEC §4 for the palette.

## 4. The repo — orientation in 60 seconds

- **Location:** `/Users/richmondrobot/Desktop/togetherbook/`
- **Live URLs:** `book.togetherbook.net` (Cloudflare Access gated) + `richmondbot2000-prog.github.io/togetherbook/` (public backdoor)
- **GitHub auth:** `gh` CLI is logged in as `richmondbot2000-prog` (PAT in macOS keychain)
- **Deploy flow:** `git push` to `main` → GitHub Pages rebuild (~30s) → Cloudflare edge propagation (~10s). Cache-bust CSS/image/JSON refs on every push (see SPEC §15 — "Cache-bust pattern").
- **Data:** flat HTML files fetch JSON files at the repo root. JSON files are refreshed by `.github/workflows/refresh-*.yml` Action workflows that run Python scripts in `scripts/`. All data ultimately comes from the **Fabric data warehouse** (the Richmond Group warehouse that mirrors Central Services nightly).
- **No build step.** No SPA. No bundler. Edits are direct.

## 5. The terminology that will trip you up

This is the most error-prone bit of the schema. Get it wrong and the source-quality analysis silently lies. Full detail in SPEC §13. **The authoritative source is `~/Desktop/wiki/partnerships-handbook.html`** — read it before touching anything broker-related. The 30-second version:

| Term | Lives in DB at | Roughly |
|---|---|---|
| Partner / Broker / Source (used interchangeably by the team) | `Brokers.Sources` (table) | The affiliate company we have a contract with — Lead Economy, Search ROI, Monevo, SuperMoney, TFLI, ITMedia etc. |
| Campaign | `Brokers.Campaigns` (table) | Our per-pricing-tier agreement WITH that partner. Each partner has at least Default (Type 20) + MedallionBP (Type 24) plus optional variants. Ephemeral. |
| SourceRef1 | `Leads.SourceReference1` (column) | The partner's **parent sub-affiliate** code passed per lead. ~78k distinct values. The partner resells leads from many SR1s. |
| SourceRef2 | `Leads.SourceReference2` (column) | The **child sub-source** within an SR1 parent. Preferred for surgical blocks. |
| SourceRef3 | `Leads.SourceReference3` (column) | Partner's internal lead ID. **Never block on this.** |

Analysis unit is currently **(Broker, SourceReference1)** but a future improvement is to surface SR2 drill-down. `Brokers.SourceTypeID` is NOT the granular Source dimension — it's a 2-value enum ("Broker"/"PPC").

**Canonical KPI: blended CPA ~12%.** Below = profitable, above = eroding margin. Per the handbook this is the single most-watched number. The page currently reports `cost_per_paid_loan` in dollars — translating that to CPA % vs the 12% benchmark is one of the open improvements (SPEC §11.5.9).

**Commission types — corrected from the handbook:**
- 1 = PerFundedLoan (CPF) — *if* `rate < 1`, the scanner reinterprets as % rev-share (mis-typed data; see SPEC §11.5.3)
- 2 = PerApplication
- 3 = PerClick (CPC) — excluded from analysis
- 4 = PerAcceptedAPILead (covers static-price AND price-reject bidding variants)
- 5 = PerFundedLoanPreCheck (CPF with Pre-Check, also used as a label for CPF click campaigns)

**Lead Outcomes (the canonical attribution path)** — confirmed with Kelly Black 2026-05-12:
- `ReportingApplications.dbo.LeadOutcomes` is the per-lead event log. Each row is `(LeadId, LeadOutcomeTypeID, DateTimeUtc)`. Whitebox `/UpdateStatus` writes one row per milestone, tagged with the `LeadId` that was live at the time — so per-`LeadId` aggregation gives correct attribution even when an ARef was sold by multiple brokers. The funnel enum is in `dbo.LeadOutcomeTypes` (10 values; key ones: 1=Apply1 complete, 4=BRW signed, 5=GT passed, 6=GT VC, 8=**Paid out**).
- `scan_source_quality.py` Part A uses this canonical path; `scan_brokers.py` still uses Tasks-derived stage counts (separate consideration, not yet refactored).
- For any attribution-sensitive analytics, **don't group by ARef then MAX(CampaignId)** — that's the old broken heuristic and arbitrarily picks a broker when the customer was sold by multiple. JOIN LeadOutcomes per LeadId instead.

## 6. Tooling preferences

- **Auto-memory is on.** Save user info, working-style feedback, project context, and external references using the auto-memory format. Don't save things derivable from the code or git history.
- **Sub-agents:** spawn `Explore` for broad codebase searches (>3 queries). For targeted lookups use grep/find directly.
- **Background workflows:** the user often kicks off long-running workflow_dispatch runs. Use `ScheduleWakeup` for sensible delays (don't poll, don't pick exactly 5 min — see ScheduleWakeup tool description for cache-window heuristics). When a wakeup fires with a re-fired prompt and the user has paused or moved on, honour the pause, not the wakeup.
- **gh CLI:** `gh run view <id> --log` to read full run output. `gh workflow run <name>.yml --ref main` to trigger. `gh secret list` / `gh secret set`. The `--repo richmondbot2000-prog/togetherbook` flag is implicit when you're already cd'd into the repo.
- **Don't use the gh CLI for setting JSON-shaped secrets via stdin** — heredoc escaping is brittle. Use the GitHub Web UI for those.

## 7. Documentation responsibilities — the nightly directive

**Every overnight session, before signing off, you MUST:**

1. **Update `SPEC.md`** (this repo) with anything structurally new or changed: new pages, new analysis sections, new workflow files, new env vars / secrets, new database joins, new design decisions, new lessons learned. SPEC.md is the source of truth for "how does this site work" — keep it complete enough that a successor Claude can pick it up cold and operate competently.
2. **Update `~/Desktop/wiki/Overview/07_TogetherBook_Site.md`** to stay in structural sync. That document is the wiki-wide entry for this site, integrated alongside other Central Services docs. Pull from SPEC.md where appropriate but recognise the audience there is broader (also the engineering team).
3. **Commit and push both.** Don't stage them. If a section is genuinely incomplete, write the heading + a one-line "TBD: …" note rather than leaving a blank gap.

You don't need permission to update these — it's a standing instruction.

The user goes to sleep and trusts you to use the time productively. They'd rather come back to slightly-over-documented than under-documented. **Reading what you already shipped is a fine way to catch yourself up next morning.**

## 8. Memory references — useful pointers

- `project_togetherbook.md` — high-level project state
- `project_togetherbook_brokers.md` — Brokers page + Source-quality companion (terminology, cost models, the (Broker, SR1) decision)
- `project_togetherbook_brandwatch_email.md` — Brandwatch email notification setup
- `project_togetherbook_directory.md` — Directory page (multi-tenant Workspace)
- `project_togetherbook_topups.md` — TopUps page (TUE concept)
- `project_togetherbook_telegram.md` — Brand monitoring stack (Telegram + Discord + HIBP + Lookalike, dormant)
- `project_data_warehouse.md` — Fabric warehouse pointers
- `reference_togetherbook_paths.md` — repo location, live URL, deployment flow
- `reference_rg_wiki.md` — where source pages and overviews live in the user's filesystem
- `reference_rg_schema_diagrams.md` — ER-diagram images for all 5 Central Services DBs
- `feedback_working_style.md` — terse, few questions, push to live immediately
- `feedback_design_must_communicate.md` — every design element must teach
- `feedback_togetherbook_spec.md` — keep SPEC.md and the wiki version in sync

## 9. Common operations cheatsheet

```sh
# Force-refresh a single data file (bypasses same-day guard)
gh workflow run refresh-brokers.yml --ref main
gh run list --workflow=refresh-brokers.yml --limit 1

# Pull what the bot just committed
git pull --rebase --quiet

# Read failure-only logs from a specific run
gh run view <run-id> --log-failed | tail -40

# Bump cache-bust on one file
NEW=$(date +%s); OLD=$(grep -oE '\?v=[0-9]+' brokers.html | head -1 | sed 's/?v=//'); sed -i '' "s|?v=${OLD}|?v=${NEW}|g" brokers.html

# Manually trigger source-quality refresh + watch
gh workflow run refresh-source-quality.yml --ref main
sleep 240
gh run list --workflow=refresh-source-quality.yml --limit 1
```

## 10. Things you should NOT do

- Don't add features the user didn't ask for. Don't refactor opportunistically.
- Don't add validation or error handling for scenarios that can't happen. Trust the schema where you've already confirmed it.
- Don't write comments explaining WHAT the code does. Write them only when WHY is non-obvious (a hidden constraint, a workaround for a specific bug, a surprising invariant).
- Don't create planning, decision, or analysis documents unless the user asks. Work from conversation context, not intermediate files.
- Don't ask "should I proceed?" or "is the plan ready?" Just do the work — if the user disagrees they'll tell you.
- Don't claim work is done that you haven't verified. UI changes need a visual check; data changes need a workflow run + JSON inspection.
- Don't use `git push --force`, `git reset --hard`, `git branch -D`, or any other destructive git operation unless explicitly asked.

---

_End. SPEC.md is much longer and goes deep on every section. Read it on first contact with this repo._
