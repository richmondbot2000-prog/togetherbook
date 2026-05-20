# _inflight — what each Claude Code session is currently working on

_Cross-session coordination file. Each active Claude Code session adds a row here when starting non-trivial work and removes it on commit. Read this file first thing when you sit down to a session — if another session is editing what you're about to touch, work on something else (or coordinate via the Wall)._

**Format** (one row per active task):

```
| HH:MM UTC | session=<id> | scope | note |
```

`<id>` is the per-session random identifier each session generates on `/session-start` and keeps for the whole session. Same ID in your commit footers (`Session-Id: <id>`) so `git log --grep="Session-Id: <id>"` shows everything you shipped this sitting.

**Lifecycle:**
1. `/session-start` appends your row + commits this file.
2. Work in flight — your row stays.
3. `/session-end` removes your row + commits this file.

**If a row is older than 4 hours and you don't recognise it,** the agent that wrote it probably crashed or got compacted out of context. Safe to remove — the next session-start would do the same.

<!-- INFLIGHT:BEGIN -->

| HH:MM UTC | session | scope | note |
|---|---|---|---|
| _no active sessions_ | | | |

<!-- INFLIGHT:END -->
