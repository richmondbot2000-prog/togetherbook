# Overnight QA log — 2026-05-16

Comprehensive review + fix cycle initiated by the user at ~midnight BST.

## Method

1. Spawn parallel review agents, each owning one section of the site. Each agent reads SPEC.md alongside the implementation, documents expected behaviour, and reports problems with severity + reproduction.
2. Aggregate findings into the "Problems found" section below.
3. Fix issues in priority order, commit + push each fix with a descriptive message.
4. Re-spawn verification agents on the riskiest fixes.
5. Loop until confident.

## Constraints

- No browser automation (the live site is Cloudflare-Access-gated).
- Agents do static + behavioural review against SPEC.md, not live interaction.
- Avoid touching the daily 07:05 source-quality refresh scanner without explicit user verification.

---

## Problems found + fixes

_(Filled in as agents report. Format: `[SEV] page · description · fix commit hash`)_

(none yet — agents in flight)

---

## Status

- Round 1 spawned: 2026-05-16 ~00:30 BST.
