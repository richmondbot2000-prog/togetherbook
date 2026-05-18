# CLAUDE.md — How to work with this user on this repo

_Auto-loaded into every Claude Code session here. **SPEC.md** is the operational deep-dive; read it on first contact._

---

## 1. The user

Non-technical, at Richmond Group (`richmondbot2000@gmail.com`). Works on **Central Services** — the platform behind Transform Credit and the other Richmond Group lenders. They don't write code; they direct strategy, you handle implementation. Strong product instincts, blunt corrections.

They use `book.togetherbook.net` as an operational dashboard.

## 2. Working style — non-negotiable

- **Be terse.** One sentence per update. End-of-turn summary in one or two sentences.
- **Ship to live immediately.** `main` deploys via GitHub Pages — every commit goes live in 30-60s. Commit + push within the same turn, never stage for later.
- **Overnight work is welcome.** "I'm going to bed" / "do work while I'm gone" means be ambitious — multiple substantive commits, each with a clear commit message that reads as the log. Don't write status files. Avoid scanner changes that could break the 07:05 source-quality refresh without verification; UI/copy/docs changes are safer overnight.
- **Don't over-ask.** Spend up to a minute on read-only investigation (grep, file reads) before asking. When you do ask, concrete options beat prose.
- **Corrections are blunt.** Take them on the chin, adjust, continue. No apologies or rehash.
- **No emojis** anywhere unless explicitly requested.
- **No filler.** Either do the work or state the specific obstacle.

## 3. Design philosophy — every element teaches

Decorative-only choices get rejected. Every icon, colour, axis must communicate something specific. If you can't articulate what it teaches in one sentence, the user will probably want it removed.

**Quiet Edition** visual treatment: cream paper + ink blue + brass + sparing manuscript red. Editorial / antique-book feel. No box-shadows, no transforms on hover, only colour transitions. Tokens in `quiet-tokens.css`; full palette in SPEC §4.

## 4. Repo orientation in 60 seconds

- **Location:** `/Users/richmondrobot/Desktop/togetherbook/`
- **Live URLs:** `book.togetherbook.net` (Cloudflare Access gated) + `richmondbot2000-prog.github.io/togetherbook/` (public mirror)
- **GitHub auth:** `gh` CLI is logged in as `richmondbot2000-prog` (PAT in macOS keychain)
- **Deploy flow:** `git push` to `main` → GitHub Pages rebuild (~30s) → Cloudflare edge propagation (~10s). Cache-bust CSS/image/JSON refs on every push (see SPEC §15).
- **Data:** flat HTML files fetch JSON files at the repo root. JSONs are refreshed by `.github/workflows/refresh-*.yml` running Python scripts in `scripts/`. Data comes from the **Fabric data warehouse** (nightly mirror of Central Services).
- **Workers:** two Cloudflare Workers handle dynamic writes — `apifk-workspace-worker2` (sources `worker/workspace-worker.js`; routes `/api/workspace/*` + `/api/wall/*` + `/api/holidays/*`) and the annotations worker (`worker/annotations-worker.js`; route `/api/annotations*`). The annotations worker is referenced as `apifk-annotations-worker` in code/comments but its current Cloudflare-dashboard name is the auto-generated `shiny-heart-00f8`. Deploy `apifk-workspace-worker2` with `python3 ~/.togetherbook/deploy_worker.py` — it inherits existing bindings (5 secrets: `GOOGLE_SERVICE_ACCOUNT_JSON`, `GITHUB_TOKEN`, `CLOUDFLARE_API_TOKEN`, `GIPHY_API_KEY`, plus historic auth tokens + `IMPERSONATE_USER` / `IMPERSONATE_USER_TOGETHERLOANS` plain-text vars + `PAYROLL_KV` + `ACTIVITY_DB` bindings) via the Cloudflare API token in `~/.togetherbook/cloudflare.json`. No dashboard paste needed for everyday edits. The annotations worker is rarely edited and still needs the dashboard route per `worker/SETUP.md`.
- **Pages with write-back:** Wall (posts/comments/reactions), Directory (annotations + workspace actions), Holidays (per-user calendar). The rest are read-only.
- **No build step.** No SPA. No bundler. Edits are direct.

## 5. Terminology that will trip you up

The Brokers / Sources / Campaigns / SourceRef hierarchy is the most error-prone bit of the schema — get it wrong and source-quality analysis silently lies. **Authoritative source: `~/Desktop/wiki/partnerships-handbook.html`**; read before touching anything broker-related. SPEC §13 has the 30-second version including the canonical **blended CPA ~12%** KPI, the corrected commission types, and the `LeadOutcomes`-per-`LeadId` attribution rule.

## 6. Tooling preferences

- **Auto-memory is on.** Save user info, working-style feedback, project context, external references. Don't save anything derivable from code or git history.
- **Sub-agents:** spawn `Explore` for broad codebase searches (>3 queries). `general-purpose` for design / research / multi-step work. For targeted lookups use grep / find directly.
- **Background workflows:** `ScheduleWakeup` (in `/loop` mode) or `CronCreate` (one-shot) for sensible delays. Don't poll; don't pick exactly 5 min — pick 270s or 1200s+ to respect the 5-min prompt cache TTL. Honour user pauses over re-fired wakeups.
- **gh CLI:** `gh run view <id> --log` for full output. `gh workflow run <name>.yml --ref main` to trigger. `gh secret list` / `set`. Always cd'd in the repo so `--repo` is implicit.
- **Don't set JSON secrets via stdin** — heredoc escaping is brittle. GitHub Web UI for those.

## 6a. Cross-session coordination (two Claude Code sessions share this repo)

There are typically **two Claude Code sessions** working on TogetherBook at any time, sharing this repo + SPEC.md + the wiki. The user is non-technical and will NOT remember to type slash-commands; coordination must happen automatically. Rules for the agent (you):

1. **On your first non-trivial action in any new conversation, run the session-start dance silently.** Do this before any other significant work — code change, doc edit, worker deploy, etc.:
   ```bash
   cd /Users/richmondrobot/Desktop/togetherbook
   git pull --rebase --quiet
   # If .claude/session.id doesn't exist OR is older than 8 hours, generate fresh.
   if [ ! -f .claude/session.id ] || [ -z "$(find .claude/session.id -mmin -480 2>/dev/null)" ]; then
     mkdir -p .claude
     python3 -c "import secrets,string; print(''.join(secrets.choice(string.ascii_lowercase+string.digits) for _ in range(6)))" > .claude/session.id
   fi
   SID=$(cat .claude/session.id)
   # Glance at the other session's claims + recent commits.
   sed -n '/INFLIGHT:BEGIN/,/INFLIGHT:END/p' _inflight.md
   git log --since='6 hours ago' --pretty='%h %an %s' --no-merges | head -20
   echo "this session: $SID"
   ```
   If the other session has a row in `_inflight.md` that overlaps with what the user just asked for, surface it before doing anything destructive. Then append your own row to `_inflight.md` between the markers and commit it.

2. **`git config core.hooksPath .githooks`** is already set, so the pre-commit hook auto-injects `Session-Id: <id>` into every commit footer from `.claude/session.id`. You don't have to remember it; the hook does it. (Still safe to include manually — the hook is a no-op if it's already there.)

3. **On sign-off (user says "we're done", "thanks", "good night", etc.), remove your row from `_inflight.md` + commit it.** Last write wins so this is safe even if the other session's row was added meanwhile.

4. **`git rerere.enabled` is true** for this repo — repeated conflict resolutions cache and auto-resolve next time.

5. **Worker deploys are guarded.** `~/.togetherbook/deploy_worker.py` refuses to deploy if local `worker/workspace-worker.js` is behind `origin/main` (pre-deploy guard added 2026-05-18). Pull first if the guard complains. `--force` overrides — only use it if you're sure.

6. **Auto-generated blocks have markers** — `<!-- AUTO:* BEGIN/END -->` for SPEC tables (rewritten by `scripts/generate_canonical_tables.py`) and `<!-- PENDING:BEGIN/END -->` for pending lists (rewritten by `scripts/render_pending.py`). Never hand-edit between markers; the next render clobbers it. Change the source (`.github/workflows/`, `worker/workspace-worker.js`, `pending.yaml`) and re-run the script.

The `/session-start` and `/session-end` skills are available as opt-in overrides — but the standing instruction above means you do the work without the user typing anything.

## 7. Documentation responsibilities — the nightly directive

Every overnight session, before signing off, you MUST:

1. Update **`SPEC.md`** with anything structurally new or changed: pages, analysis sections, workflows, env vars, DB joins, design decisions, lessons. SPEC.md should let a successor Claude pick up cold.
2. Update **`~/Desktop/wiki/Overview/07_TogetherBook_Site.md`** to stay in structural sync (broader audience — also engineering).
3. **Commit + push both.** If a section is genuinely incomplete, write the heading + a one-line "TBD" rather than leaving a gap.

Standing instruction; no permission needed. The user would rather come back to slightly-over-documented than under-documented.

## 8. Common operations cheatsheet

```sh
# Force-refresh a data file (bypasses same-day guard)
gh workflow run refresh-brokers.yml --ref main

# Pull what the bot just committed
git pull --rebase --quiet

# Read failure-only logs from a run
gh run view <run-id> --log-failed | tail -40

# Bump cache-bust on one file
NEW=$(date +%s); python3 -c "
import re, pathlib; p = pathlib.Path('brokers.html')
p.write_text(re.sub(r'\?v=\d+', '?v=$NEW', p.read_text()))"

# Deploy the workspace worker (after editing worker/workspace-worker.js)
python3 ~/.togetherbook/deploy_worker.py
# Verifies by re-fetching settings + printing the binding list at the end.
# Cloudflare edge picks up the new version within seconds; no GH Pages
# rebuild and no browser hard-refresh required for worker-side fixes.
```

## 9. Things you should NOT do

- Don't add features the user didn't ask for. Don't refactor opportunistically.
- Don't add validation / error handling for scenarios that can't happen. Trust the schema once confirmed.
- Don't write comments explaining WHAT — only WHY when non-obvious.
- Don't create planning / decision / analysis documents unless the user asks. Work from conversation context.
- Don't ask "should I proceed?" — just do the work.
- Don't claim work is done that you haven't verified.
- Don't use `git push --force`, `git reset --hard`, `git branch -D` etc. unless explicitly asked.
- Don't commit secrets (`.env`, credentials, API keys). The `annotations.json` + `holidays.json` write-back files are public-by-design; don't put anything sensitive in them.

---

_End. SPEC.md is much longer and goes deep on every section. Read it on first contact._
