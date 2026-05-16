# Overnight QA log — 2026-05-16

Comprehensive review + fix cycle. Five parallel review agents each owned one
section of the site (Wall · Holidays · Directory · Payouts · cross-cutting +
remaining pages), read SPEC.md alongside the implementation, and reported back
with confirmed-working features + a defect table.

All severity-HIGH and most severity-MED findings have now been fixed and pushed.

## Round 1 — Holidays fixes (`cdafb2a`)

| Sev | Issue | Fix |
|---|---|---|
| HIGH | `countHolidayDays` ignored `approved-holiday`, `half-am`, `half-pm`. Counter never moved when those were set. | Added them as `+1` for `approved-holiday`, `+0.5` for half-days. `formatHolidayCount` renders fractional totals. |
| HIGH | `setupViewTabs` was one-shot — viewer made someone's manager mid-session never saw the Team tab. | Made idempotent with `viewTabsWired` latch; tab visibility now tracks `directReports.size`. Added a window-focus refresh (30 s debounced) so the Team tab can appear on return without a reload. |
| MED | `openTeamPicker` keydown listener never released through the click-outside path — stale listeners stacked up after repeated cell clicks. | Assigned `pop._cleanup` the same way the own-view picker does. |

## Round 2 — Wall / Directory / cross-cutting highs (`9261074`)

| Sev | Issue | Fix |
|---|---|---|
| HIGH | **XSS in Wall compact-tile body teaser.** `linkifyBody(body250)` was being fed raw post body — its header even said callers must escape first. | Swapped to `enrichBody` which escapes before linkifying. |
| HIGH | **Wall multi-reaction stacking.** Clicking a second emoji on the same post stored BOTH reactions; live `wall.json` already showed this. | Worker `wallReact` now sweeps the viewer off every other emoji on the target before adding the new one + emits a `removed` `react_event` for each cleared entry. |
| HIGH | **Wall notification-bell jump broken.** Clicking a bell entry only did `openPostIds.add + renderFeed` — it never paged to the post, never expanded, never scrolled. | Extracted `jumpToPost(postId)` helper; initial-load hash, bell clicks, and a new `hashchange` listener all call it. |
| HIGH | **Directory `pending_conversion` data-loss.** Field silently dropped — neither `saveAnnotationRemote` nor the annotations worker whitelisted it. The immediate convert-to-group leaver flow never created the forwarding Group. | Added pass-through on the page side; the worker now applies `rename_decay`-style object-update semantics on `pending_conversion`. |
| HIGH | **Directory "Suspend + keep this forward" button** still surfaced on live users despite the 2026-05-14 suspend retirement (Google bills suspended seats at full price). | Removed the button. Hint rewritten to direct users to a Delete option instead. |
| HIGH | **`var(--manuscript-red)`** in `reports.html` referenced an undefined token — hover border-left silently dropped. | Replaced with `--red-500`. |
| HIGH | **apis.html + robots.html** carried the pre-restructure flat nav (no Wall, no Reports parent, no `data-sub`, no nav.js). | Replaced with the canonical nav block + added `nav.js` script tag. |

## Round 3 — concurrency, optimistic UX, layout polish (`a1fe6a2`)

| Sev | Issue | Fix |
|---|---|---|
| HIGH | **annotations-worker.js had no retry-on-409.** Two concurrent saves silently dropped one. | Wrapped the read-merge-write in a 4-attempt retry loop matching `updateGhJson`. Each retry re-reads so parallel writers' fields are preserved. |
| MED | Wall "Mark all read" had no rollback path — on worker error the UI kept the badge cleared until reload, refresh then showed the old badge reappear. | Snapshot `lastSeenByPost` before mutating; restore + re-render on error. |
| MED | Wall comment + reply submit ran `sortPosts()` which bubbled the post off the user's current page; focus ended up on a detached node. | Now jumps `currentPage` to the post's new index so it stays visible; re-queries the composer after render to restore focus on the fresh input. |
| MED | `pipeline.html` crashed on partial payload (missing `leads.presented` etc.) — entire page stuck on "Loading…". | Coerce to `0` before `.toLocaleString()`. |
| MED | Brandwatch hero logos broken-image when google.com favicon service unreachable. | Added `onerror` handler that hides the parent so the card just renders without the logo. |
| MED | Payouts monthly bar chart was chronological but SPEC §11.4 says "12 months ranked by spend". | Sort descending by amount. |
| MED | Payouts stale-tab fetch race — slow Year fetch could repaint over a newer Yesterday/Week selection. | Guard with `currentRange !== key` re-check in both `.then` and `.catch` handlers. |
| MED | `nav.js` had no cache-bust on any page — pinned to whatever the browser cached on first load. | Bumped every page's `nav.js` reference to a fresh `?v=`. |

## Remaining (deferred — LOW severity, mostly cosmetic / edge-case)

- Wall: hover-picker timer can race the comment-on click handler (anchor may detach before the popover mounts).
- Wall: `wallMarkSeen` is a no-op for non-authors — toast still says "Marked all as read".
- Wall: "See more / Show less" toggle on long posts isn't currently wired against a `.is-clamped` class so the spec'd 4-line clamp doesn't fire.
- Wall: dead `wl-bookmark` icon, dead `.wl-compose-eyebrow` CSS block.
- Wall: `linkifyBody` regex greedily eats trailing punctuation.
- Directory: billing-summary one-line strip vs SPEC §11.1.6 three-card description (drift in either direction is acceptable).
- Directory: `lineManagerName` doesn't flag orphaned (deleted-staff) line-manager pointers.
- Directory: `autoSaveNotesIfChanged` passes empty string for `start_date` — fragile but harmless today.
- Directory: `adminEmailOptions` includes deleted (`deletion_time` set) users in the forward/transfer/add-to-group datalist until next staff.json refresh.
- Directory: unsuspend's "forwarding turned off" message asserts success even when the Gmail mailbox token exchange swallows an error.
- Payouts: Leaflet maps not `.remove()`'d on tab switch / capita toggle — memory grows until GC.
- Payouts: state-total-mode pin popup says "N loans" (plural-only); avg-state map handles singular properly.
- Payouts: pin-count vs `totals.borrowers` drift when zip-lookup misses; consider surfacing "M mapped of N" on the lead line.
- Payouts: year-only rebuild not supported by the workflow; minor.
- Payouts: date strings parsed as UTC midnight; negative-UTC users see a one-day-off date in the lead.
- Holidays: SPEC §11.9.3 still describes the pre-Sunday-anchored layout — documentation drift.
- Holidays: worker doesn't seed `year_start` / `year_end` on doc init — page falls back to hardcoded values.
- Holidays: `fromStatus` arg dead in `saveDayFor` / `saveDay`.
- Holidays: `fetchLineManagers` uses a hardcoded URL rather than `${REPO}/${BRANCH}` constants.
- Holidays: comment claims `2027-03-29` is "out of window" but it's in-window and counted.
- Cross-cutting: brand-logo PNG cache-bust is `1778791697` on every page (not bumped alongside CSS).
- Cross-cutting: `apis.html` + `robots.html` are orphaned (no inbound links).
- Cross-cutting: `apis.html` missing `quiet-legacy.css`.
- Cross-cutting: `brokers.html` has a `?bust=` query on top of `cache: no-store` — redundant noise.

## Worker redeploys pending (user must do manually)

Two worker files changed. Both need a paste-and-deploy in Cloudflare:
- `worker/workspace-worker.js` — Wall multi-reaction sweep fix.
- `worker/annotations-worker.js` — `pending_conversion` field + retry-on-409 loop.

The page-side changes are live; the worker fixes only take effect after the user redeploys.

## SPEC.md / wiki sync

§11.9.3 (Holidays team view) still documents the pre-Sunday-anchored
horizontal-strip layout. Re-sync needed during a future quiet period.
