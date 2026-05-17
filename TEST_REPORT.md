# TogetherBook reliability test report — 2026-05-17

## Summary
- Tests run: 15 (sections 3.1 – 3.15)
- PASS: 12
- FAIL: 2 (3.1 schema integrity, 3.15.6 merge implementation)
- WARN: 2 (3.14 wall.html stale-read path, 3.4 spec/code wording quibble on uploadImage)

Headline: **one real, user-visible data-loss-style bug** — `doPeopleMerge` re-points payroll records but does **not** re-point `google-accounts.json` or `warehouse-activity.json`. Concrete victim today is Person #4 Adnan Turken: his merge of Person #5 → #4 left GoogleAccount #5 (`adnan.turken@letme.co.uk`) pointing at the deleted person_id=5, so his Directory row and Profile chips silently lose that Google source. Same latent risk for every future merge.

## Failures

- **[3.1 schema] 1 broken FK in google-accounts.json.**
  Script output: `checked 193 people, 96 payroll, 197 google, 150 warehouse, 22 admins / failures: 1 / GoogleAccount #5 person_id=5 -> no Person`.
  Provenance: merge commit `343e2c6 Workspace: people-merge 5 → 4 by james.benamor@letme.com` (2026-05-17) only modified `workspace-actions.json`. The accompanying `c4f473e People: merge 5 into 4` rewrote people.json but no companion commit ever re-pointed google-accounts.json. GoogleAccount #5 still has `person_id: 5, email: adnan.turken@letme.co.uk`. Adnan's Person record (#4) lists `adnan.turken@letme.co.uk` in `alt_google_emails` but the corresponding google-account chip will NOT render on his Directory/Profile because both pages build `googleByPersonId` by indexing on `a.person_id` (profile.js:1801, directory.html:907) and look it up by the *winner's* id (profile.js:402, directory.html:469).
  warehouse-activity.json has 0 orphans today purely by luck — the loser had no warehouse row.

- **[3.15.6 merge] `doPeopleMerge` re-points only payroll, not google-accounts or warehouse-activity.**
  Read of `worker/workspace-worker.js` lines 2230-2307. The function explicitly loops `payFile.records` and re-points `person_id` (lines 2275-2282), but never opens `google-accounts.json` or `warehouse-activity.json`. The spec for 3.15 #6 says "the 5 source icons consolidate" — they do not, on either merge path:
    - **Google chip lost on loser side.** Any google-account on the loser stays bound to the deleted id and becomes invisible (the bug above).
    - **Warehouse chip lost on loser side.** Same mechanism — warehouse-activity rows on the loser silently orphan.
  Recommended fix shape: extend `doPeopleMerge` with two more file-touches mirroring the payroll block — load + re-point + commit `google-accounts.json` and `warehouse-activity.json` after the people commit, mirroring the same "non-fatal" error capture. Also re-run `denormaliseEmailsToPerson(winner, gFile.records)` after re-pointing google rows so winner.main_google_email / alt list stays in step. Backfill script needed for today's orphan: set GoogleAccount #5.person_id = 4 and call denormalise on Person #4.

## Warnings

- **[3.14 stale-fetch] `wall.html:1664` reads `people.json` directly (not via worker proxy).**
  `nav.js:126` does the same. Spec explicitly excuses nav.js. wall.html's use is non-critical: people.json is only consulted as a fallback dictionary to resolve `@mentions` to a display name + avatar tenant (wall.html:1678-1684). Stale-read symptom would be a renamed user showing the old name in mentions for one publish cycle — annoying, not data loss. Worth migrating eventually for parity but not a regression of the recent fix.

- **[3.4 client wiring] Spec wording vs uploadImage code.**
  Spec table row reads: "`LS.set(person.id, field, stamp)` called BEFORE the network call in `uploadImage`". In profile.js:1665-1720 the LS.set actually fires AFTER the image-upload network call succeeds (line 1689) but BEFORE the stamp-write retry loop (line 1695). I read this as matching the spec's *intent* ("so the new image URL renders immediately on this device regardless of stamp-write success") — the LS.set guards against stamp-write failure, not against image-upload failure. Flagging for the spec author to align wording or move the LS.set above the image upload if the stricter interpretation was meant.

## Notes / observations

- **[3.1 schema PASS otherwise]** 193 Persons, 96 payroll, 150 warehouse, 22 admins all clean. Every url_slug unique, every id positive int unique, every line_manager_id resolves and is not self, every payroll FK resolves, every warehouse FK resolves, every google-account tenant is one of {letme,together,external}, every admins.json email maps to an `access_level=admin` non-suspended Person (or owner failsafe).

- **[3.2 worker reliability paths] All present.**
  - `/table` handler with allowlist {people,payroll-data,google-accounts,warehouse-activity} at line 183-222
  - `cf: { cacheTtl: 0, cacheEverything: false }` at line 202
  - `Cache-Control: no-store, no-cache, must-revalidate, max-age=0` at line 213
  - `X-Table-Sha` response header at line 215
  - `doPeopleSet` 1696, `doPayrollSet` 2001, `doGoogleAccountSet` 2120, `doGoogleAccountDelete` 2198, `doPeopleMerge` 2230, `syncAdminsFromPeople` 1853, `nextPersonId` 1677, `nextPayrollId` 1949, `nextGoogleAccountId` 2089, `denormaliseEmailsToPerson` 2110 — all defined.

- **[3.3 validation rules] All four doPeopleSet rules + doGoogleAccountSet rules confirmed in code.**
  - Name required on create: line 1757-1760
  - One-per-tenant guarded by `touchingEmails` flag: line 1768-1778
  - Self-edit carve-out via `PEOPLE_SELF_EDITABLE`: line 1742-1750
  - Auto-create blank payroll on `on_payroll=true` + null FK: line 1788, 1794-1807
  - doGoogleAccountSet one-per-tenant: line 2144-2152
  - doGoogleAccountSet calls `denormaliseEmailsToPerson` + commits people: line 2190-2193
  - doGoogleAccountDelete also re-syncs people after delete: line 2213-2222
  - Returns `{ok, record, person}`: line 2195

- **[3.4 client wiring] All present in profile.js.**
  - LS helper at line 27 with 5-min TTL, JSON-encoded {v,t} entries, savedLabel formatter
  - LS.set in savePersonField line 1297 (after successful save)
  - LS.set in savePayrollEdits line 903 (after successful save)
  - LS.set in uploadImage line 1689 (see warning above on spec wording)
  - LS.overlay applied in renderProfile at line 1546
  - savedBadge consumed by editableRow at line 257-275
  - Upload retry loop `for (let attempt = 1; attempt <= 3` at line 1695
  - Bonus: renderPayrollPanel separately applies `LS.get(person.id, "payroll")` at line 483-484 because the standard overlay() field list (line 52-58) excludes payroll. Worth knowing — payroll edits ARE cached and re-applied, but via a separate path; if you add a new edited-payroll field to the panel make sure savePayrollEdits.body still includes it.

- **[3.5 stale fetch] PASS.** Zero hits for direct `/people.json` / `/payroll-data.json` / `/google-accounts.json` / `/warehouse-activity.json` reads in directory.html, profile.js, reconcile.html. 12 hits across the three files for `/api/workspace/table?file=`, all with `cache: "no-store"` + no-cache headers.

- **[3.6 URL pattern] PASS.** All 21 `fetch(WORKSPACE_API …` callsites use `WORKSPACE_API + "/<action>"`. Zero bare `fetch(WORKSPACE_API,` or `fetch(WORKSPACE_API)`.

- **[3.7 Cloudflare cache rule] PASS.**
  Live rule: `(http.host eq "book.togetherbook.net" and ends_with(http.request.uri.path, ".json"))` — enabled, action=bypass cache.

- **[3.8 cover/photo file audit] PASS.**
  0 missing files across all Persons with `cover_photo_uploaded_at` or `directory_photo_uploaded_at` stamps (including stamps stored on annotations.json).

- **[3.9 import_payroll dry-run] PASS.**
  60 matched, 0 ambiguous, 0 conflicts, 0 unmatched. Match kinds: a handful of `[external_id]` (Amber Cole, David Patterson, Paul Benamor, Rachid James Benamor, Zara Benamor, Dan Sanders, Leticia Alexandre, Philip O'Neill, Brogan Hackley) followed by the rest as `[name+dob]`. No `← verify` rows.

- **[3.10 worker probes] PASS.**
  Sandbox lacks `curl`, so I substituted Python urllib. With a browser User-Agent every endpoint returns HTTP 302 → `https://togetherbook.cloudflareaccess.com/cdn-cgi/access/log`. With no User-Agent header Cloudflare Access returns 403 instead of 302 (it bot-blocks the bare urllib UA). Caveat worth knowing: a deliberately-bogus path also returns 302 in browser-UA mode — Cloudflare Access redirects BEFORE the request reaches the Worker, so 302 only proves CF Access is fronting the route, not that the Worker has the handler registered. The grep-based code checks in 3.2 are the real proof the routes exist.

- **[3.11 commit history] PASS.**
  Last 4 commits coherent: `f07de15 SPEC_TESTING.md`, `9478240 Profile Wall feed: render parity`, `f71a2a8 Refresh groups`, `4f26c2e Reliability: localStorage write-through…`. The 4f26c2e commit IS present and is the 4th-most-recent. No force-push markers, no unexplained reverts.

- **[3.12 profile rendering] PASS.**
  renderPanel try/catch with visible "Render error in <tab> tab:" fallback at line 187-199. setTab updates URL search param + classes + calls renderPanel at line 174-182. Info-panel editable rows all use editableRow (lines 356-362): name, aliases, role, phone, address, start_date, notes. renderLinkedSourcesCard exists at line 401.

- **[3.13 reconcile workflow] PASS.**
  `.github/workflows/reconcile-people.yml` runs cron `30 6 * * *`, executes build_google_accounts.py + build_warehouse_activity.py + build_admins.py, retries push up to 3× with rebase, no-op when nothing changed.

- **[3.15 user experience] PASS for 1-5, FAIL for 6.**
  1. Admin recognition: OWNER_EMAIL `james.benamor@letme.com` hardcoded at line 53; whoami sets is_admin true if email is owner OR in admins set (line 167). Confirmed in admins.json. PASS.
  2. 5 source icons: Person #91 access_level=admin, main=letme.com, alt=[togetherloans.com, letme.co.uk], on_payroll=true, most_recent_payroll_id=83, cover_stamp+dir_stamp present. PASS.
  3. Role edit + Saved badge: editableRow("role"…) at line 358; savePersonField calls LS.set at line 1297 then renderPanel; savedBadge rendered at line 275. PASS.
  4. Edit survives refresh: LS.overlay at line 1546 applies LS values during renderProfile; LS.get returns null after 5-min TTL so server eventually wins. PASS.
  5. Cover upload visible immediately: LS.set fires at line 1689 with the new stamp before the stamp-write retry; coverSrc() at line 126-129 reads `person.cover_photo_uploaded_at` which the overlay puts in place. PASS.
  6. Merge consolidates icons: **FAIL — see Failures.** doPeopleMerge only re-points payroll, leaves google + warehouse FKs dangling.

- **One nice-to-have**: the schema script could be added to a daily workflow alongside reconcile-people.yml so future merge-induced orphans get caught the next morning rather than silently rotting. ~50 lines of Python; runs in <1s.

- **One housekeeping**: the orphan GoogleAccount #5 can be repaired today by editing google-accounts.json directly (set `"person_id": 4`) + a follow-up people-set on #4 to re-denormalise `alt_google_emails`. Probably worth fixing before shipping the doPeopleMerge fix so the immediate symptom goes away.
