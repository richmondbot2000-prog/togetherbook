# TogetherBook reliability test report — 2026-05-17 (run 3)

## Summary
- Tests run: 15 (SPEC §3.1–§3.15) + 6 targeted re-verification items + 17 black-box validator probes
- PASS: 38
- FAIL: 0
- WARN: 2 (both minor / pre-existing, not regressions from commit `53da5ee`)

Commit under test: **`53da5ee`** "Worker: write-time schema validation + line_manager FK cascade in merge".
(The brief mentioned commit `2c0fc3c` — no such hash exists in the repo. `53da5ee` is the latest commit on `main` and matches the brief's description of "two more defensive layers": four `validate*File()` write-time validators plus `doPeopleMerge` cascading `line_manager_id` from loser to winner.)

System is genuinely solid. Both new defensive layers do exactly what they advertise.

## Failures
*(none)*

## Warnings

- **[validator gap] `validateGoogleAccountsFile` accepts a record with missing/null tenant.**
  `worker/workspace-worker.js:2119` reads `if (r.tenant && !["letme","together","external"].includes(r.tenant))` — the `r.tenant &&` short-circuit means a record with `tenant: null` or no `tenant` key at all passes the write-time validator. Today no such row exists (197 google accounts all pass schema_integrity), but the next-day reconcile `check_schema_integrity.py:94` is strict (`g.get("tenant") not in (...)`) so the safety net catches it within 24 h. Worth tightening to `if (!["letme","together","external"].includes(r.tenant))` for parity. Not a regression — the gap pre-dates `53da5ee`.

- **[validator cosmetic] Person with no `id` triggers a second misleading error.**
  When a Person has no `id` field, the validator emits both `bad id on Person <name>: (missing)` AND `Person #undefined is its own line_manager` because the `p.line_manager_id === p.id` test at `worker/workspace-worker.js:1646` resolves `undefined === undefined`. Result is still a 400 with the right primary message, but the secondary error noise could mislead a maintainer. Cheap fix: skip the self-line-manager check when `p.id == null`. Not a regression — code path didn't exist before `53da5ee`.

## Targeted re-verification items from brief

| # | Item | Result | Evidence |
|---|---|---|---|
| 1 | `validatePeopleFile` exists and is called BEFORE the PUT in `commitPeopleFile` | PASS | Defined `worker/workspace-worker.js:1633`. Called on line `:1658`. `fetch(... PUT ...)` is on `:1669` — so the validator is the first statement in the helper, runs strictly before the network write. |
| 2 | `validatePayrollFile` ditto | PASS | Defined `:1957`. Called `:1972`. PUT `:1982`. |
| 3 | `validateGoogleAccountsFile` ditto | PASS | Defined `:2112`. Called `:2133`. PUT `:2143`. |
| 4 | `validateWarehouseActivityFile` ditto | PASS | Defined `:2173`. Called `:2188`. PUT `:2198`. |
| 5 | `doPeopleMerge` walks `pFile.people` BEFORE removing the loser to re-point `line_manager_id` | PASS | Walk loop `:2396-2402` (`for (const p of pFile.people) { if (String(p.line_manager_id) === loserId) ... lineManagerRepointed++; }`). Loser removal at `:2405` (`pFile.people = pFile.people.filter(...)`). Walk strictly precedes filter. Inline comment at `:2391-2394` documents the ordering intent. |
| 6 | Response includes the new `lineManagerRepointed` counter | PASS | Response key `line_manager_refs_repointed` at `:2471`, backed by JS variable `lineManagerRepointed` declared at `:2395` and incremented at `:2400`. The brief used both names — the variable matches `lineManagerRepointed`, the JSON key matches `line_manager_refs_repointed`. |
| 7 | `scripts/check_schema_integrity.py` still exits 0 | PASS | `python3 scripts/check_schema_integrity.py` → `checked 193 people · 96 payroll · 197 google · 150 warehouse · 22 admins / failures: 0 / warnings: 0 / exit=0`. |
| 8 | Most recent reconcile workflow run passed | PASS | `gh run list --workflow=reconcile-people.yml --limit=2 --json status,conclusion,createdAt -q .` → both most-recent runs `[{"conclusion":"success","createdAt":"2026-05-17T22:41:43Z","status":"completed"},{"conclusion":"success","createdAt":"2026-05-17T22:33:54Z","status":"completed"}]`. The most-recent run was triggered against commit `53da5ee` (the code under test). |
| 9 | Schema is clean — no orphans in any of the four tables | PASS | 0 broken FKs across 193 Persons / 96 payroll / 197 google-accounts / 150 warehouse. Also: 10 Persons have a `line_manager_id` set, 0 orphaned, 0 self-managing. |
| 10 | Two recent test reports exist: `TEST_REPORT.md` and `TEST_REPORT_v2.md` | PASS | `ls -la TEST_REPORT*.md` → `TEST_REPORT.md` (run 1, 11186 bytes, 2026-05-17 23:19) and `TEST_REPORT_v2.md` (run 2, 10225 bytes, 2026-05-17 23:41). |

## Black-box validator probes (extracted + node-evaluated)

Extracted the four `validate*File` function bodies (lines 1633-1655, 1957-1969, 2112-2130, 2173-2185) and ran them in node against 17 hand-crafted fixtures. **All 17 PASS — the validators reject exactly what they promise:**

| Fixture | Expected message | Result |
|---|---|---|
| `ppl-dup-id`: two persons share `id:1` | "duplicate Person id" | PASS |
| `ppl-bad-id`: `id:0` | "bad id" | PASS |
| `ppl-no-id`: missing `id` field | "bad id" | PASS (plus the cosmetic dup error in Warnings) |
| `ppl-neg-id`: `id:-5` | "bad id" | PASS |
| `ppl-float-id`: `id:1.5` | "bad id" | PASS |
| `ppl-str-id`: `id:"1"` | "bad id" | PASS (correctly rejects string id) |
| `ppl-dup-slug`: two persons share `url_slug:"x"` | "duplicate url_slug" | PASS |
| `ppl-empty-name`: `name:""` | "empty name" | PASS |
| `ppl-self-lm`: `line_manager_id:5` on Person `id:5` | "own line_manager" | PASS |
| `ppl-dangling-lm`: `line_manager_id:99` with no such person | "line_manager_id=99 → no Person" | PASS |
| `ppl-clean`: 2 valid persons + 1 valid FK | accept | PASS |
| `ppl-empty` / `ppl-missing-key`: `{people:[]}` / `{}` | accept | PASS (correctly tolerant) |
| `ppl-str-lm-ok`: `line_manager_id:"1"` resolving to int id 1 | accept | PASS (lenient string→Number coercion works) |
| `pay-dup-id`, `pay-bad-id`, `pay-bad-pid`, `pay-clean` | accept/reject as expected | 4/4 PASS |
| `g-dup-id`, `g-bad-tenant`, `g-dup-email` (incl. case-insensitive), `g-clean` | accept/reject as expected | 4/4 PASS |
| `wh-dup-id`, `wh-bad-pid`, `wh-clean` | accept/reject as expected | 3/3 PASS |

Bad data **cannot** land via the worker as long as these helpers are the only commit path — and they are (greps below confirmed).

## SPEC §3 test grid

### 3.1 Schema integrity — PASS
`scripts/check_schema_integrity.py` → 193 people / 96 payroll / 197 google / 150 warehouse / 22 admins. **0 failures, 0 warnings, exit 0**. The orphan GoogleAccount #5 flagged in run 1 is still gone.

### 3.2 Worker reliability paths — PASS
All required functions / strings present in `worker/workspace-worker.js`:
- `/table` handler at `:183` (`pathname.replace(/\/$/, "").endsWith("/table")`)
- `cf: { cacheTtl: 0, cacheEverything: false }` at `:202` (and `:3300` for a second proxy-read use)
- `"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"` at `:213`
- `"X-Table-Sha": data.sha || ""` at `:215`
- `doPeopleSet`, `doPayrollSet`, `doGoogleAccountSet`, `doGoogleAccountDelete`, `doPeopleMerge`, `syncAdminsFromPeople`, `nextPersonId`, `nextPayrollId`, `nextGoogleAccountId`, `denormaliseEmailsToPerson` — all defined and unchanged in shape from run 2.

### 3.3 Worker validation rules — PASS
- `"name is required for new people"` literal at `worker/workspace-worker.js:1759` (line shifted by validator additions).
- `touchingEmails` guard wraps the one-per-tenant check.
- `PEOPLE_SELF_EDITABLE` self-edit carve-out present.
- Auto-create blank payroll on `on_payroll=true + null FK` present (commits via the now-validating `commitPayrollFile` → `validatePayrollFile`).
- `doGoogleAccountSet` one-per-tenant + `denormaliseEmailsToPerson` mirror still wired.

### 3.4 Client wiring — localStorage + persistent badge — PASS
All checks pass exactly as run 2. No changes to `profile.js` in `53da5ee` (`git show --stat` confirms — only worker + TEST_REPORT_v2.md changed).

### 3.5 Client wiring — reads via Worker proxy — PASS
`grep -nE 'fetch\("/people\.json|fetch\("/payroll-data\.json|...' directory.html profile.js reconcile.html` → **zero hits**. `grep -nE 'fetch\("/api/workspace/table\?file='` → **12 hits**.

### 3.6 URL pattern + dispatch — PASS
`grep -nE 'fetch\(WORKSPACE_API[, )]'` → zero bare hits. All 20 `fetch(WORKSPACE_API …` callsites pass an action segment.

### 3.7 Cloudflare cache rule — PASS
`PASS — rule live: (http.host eq "book.togetherbook.net" and ends_with(http.request.uri.path, ".json"))`

### 3.8 Cover/photo file audit — PASS
`missing files: 0` across every Person with a `cover_photo_uploaded_at` or `directory_photo_uploaded_at` stamp.

### 3.9 Import script correctness — PASS
`scripts/import_payroll.py …` → **matched: 60, ambiguous: 0, conflicts: 0, unmatched: 0**. Zero `← verify` markers, zero errors.

### 3.10 Worker probes — PASS
All six endpoints return HTTP 302 (redirect to Cloudflare Access). None 404.
```
table?file=people                   HTTP 302 -> togetherbook.cloudflareaccess.com
table?file=payroll-data             HTTP 302 -> togetherbook.cloudflareaccess.com
table?file=google-accounts          HTTP 302 -> togetherbook.cloudflareaccess.com
table?file=warehouse-activity       HTTP 302 -> togetherbook.cloudflareaccess.com
whoami                              HTTP 302 -> togetherbook.cloudflareaccess.com
payroll                             HTTP 302 -> togetherbook.cloudflareaccess.com
```

### 3.11 Recent commits + tampering — PASS
`git log --oneline -15` is coherent. `4f26c2e` ("Reliability: localStorage write-through…") still present. Latest commit `53da5ee` ("Worker: write-time schema validation + line_manager FK cascade in merge") added 94 lines to `worker/workspace-worker.js` and 128 lines to `TEST_REPORT_v2.md`. No reverts, no force-push markers, no anomalies.

### 3.12 Profile rendering paths — PASS
`renderPanel` try/catch with `Render error in <currentTab>` fallback, `setTab` URL/class/render dispatch, `editableRow(...)` on every Info-tab field, `renderLinkedSourcesCard()` rendering linked sources — all present and unchanged.

### 3.13 Daily reconcile workflow — PASS
`.github/workflows/reconcile-people.yml`: cron `30 6 * * *`, sequence build_google_accounts → build_warehouse_activity → build_admins → **`Verify cross-table schema integrity` (runs `check_schema_integrity.py`)** → commit-if-changed with rebase-retry up to 3×.

### 3.14 Stale-fetch detection — PASS
Repo-wide grep returns exactly **one** hit: `nav.js:` reading `/people.json` (acceptable per spec §0 — nav is read-only). All three migrated pages (directory.html, profile.js, reconcile.html) are clean.

### 3.15 User experience — PASS
Spot-checks against `worker/workspace-worker.js` + `profile.js`:
1. Admin recognition — `OWNER_EMAIL` hardcoded + James in admins.json. ✓
2. 5 source chips — `renderLinkedSourcesCard()` iterates google + payroll + warehouse links. ✓
3. Edit role with green badge — `editableRow` shows `savedBadge` from `LS.savedLabel`; `savePersonField` writes `LS.set` after success. ✓
4. Edit survives refresh — `/api/workspace/table` is `cache: "no-store"`; LS overlay re-applied on render. ✓
5. Cover upload immediately visible — `LS.set` before network call; stamp-save loop retries 3×. ✓
6. Two-Person merge — `doPeopleMerge` now re-points payroll + google + warehouse FKs PLUS line_manager_id PLUS commits all four tables through the validating helpers. Response shape: `{ok, winner_id, loser_id, line_manager_refs_repointed, payroll_records_repointed, google_accounts_repointed, warehouse_rows_repointed, [warns...]}`. ✓

## Notes / observations

- **Validators are placed at the bottleneck.** A grep for `commitPeopleFile(`, `commitPayrollFile(`, `commitGoogleAccountsFile(`, `commitWarehouseActivityFile(` showed those are the **only** code paths that PUT to GitHub for those four files — every mutation (people-set, people-delete, people-merge, payroll-set, google-account-set, google-account-delete) routes through one of them. So the validator coverage is total: there is no way to write bad data without going through a validator first.

- **doPeopleMerge now does six things atomically per merge (caller's perspective):** field merge, `line_manager_id` cascade (NEW in `53da5ee`), loser removal, payroll FK repoint, google FK repoint + denormalise, warehouse FK repoint. All emerge as one HTTP response. The per-table commits happen in sequence; if any fail mid-way the people commit has already landed, and the response carries a per-table `*_sync_error` warning (no exception thrown). This is the correct trade-off: the visible state change happens first, the supporting FK cleanups are best-effort with a warning shape the UI can surface.

- **The dispatcher surfaces validator errors as HTTP 400 with the message intact.** `route` catches the thrown error at `:374` / similar and returns `json({ ok: false, error: e.message }, 400, req)`. The UI gets a clean string like `people.json validation failed: duplicate url_slug james.benamor` rather than a 500.

- **Layer 6 (write-time validators) + layer 7 (daily integrity job) form true belt-and-braces.** A bug in any future Worker code that constructs bad data is caught at commit time. A bad data row that somehow slips through (or is hand-edited in the repo) is caught within 24 h by the daily reconcile job. Both layers have been exercised today: the validators by 17 fixture probes; the daily job by an `OK` run at 22:41:43Z against commit `53da5ee`.

- **One pre-existing minor cosmetic bug noticed.** `commitPeopleFile` at `worker/workspace-worker.js:1661` uses `(a.id || "").localeCompare(b.id || "")` as a sort tiebreaker. `a.id` is an integer, not a string — calling `.localeCompare` on it would throw `TypeError`. Only triggers as a tiebreaker when two people have identical names — which `validatePeopleFile` would not block (names need only be non-empty, not unique). Provenance: commit `4dde974` (pre-existing). Not a regression from `53da5ee`. Recommend `String(a.id || "").localeCompare(String(b.id || ""))` if anyone touches that sort line.

- **Conclusion.** Layers 6 and 7 are real defences, not theatre. The validators reject every bad-shape I could think of; the merge cascade closes the same orphan class for `line_manager_id` that `c6fe2f7` closed for payroll/google/warehouse. The system is materially harder to break today than it was at run 2.
