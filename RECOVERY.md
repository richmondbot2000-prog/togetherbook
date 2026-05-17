# TogetherBook — Recovery Runbook

When something breaks, this is the playbook. Each scenario has: **symptom · diagnosis · fix · prevention**.

Designed for a non-technical user (or an agent without context) to follow without guessing.

---

## 0. Anatomy in one paragraph

TogetherBook is plain HTML + JSON files served by GitHub Pages, behind Cloudflare Access. Writes go through a Cloudflare Worker (`apifk-workspace-worker2`) that commits to the GitHub repo `richmondbot2000-prog/togetherbook`. The four "identity tables" are JSON files at the repo root: `people.json`, `payroll-data.json`, `google-accounts.json`, `warehouse-activity.json`. **Git history IS the database history.** Any state can be recovered by checking out an older commit.

---

## 1. "I edited a field and on refresh it shows the old value"

**Diagnosis order** (most likely first):

1. **Browser caching** — iOS Safari sometimes serves a stored copy. Force-revalidate: tap-and-hold the reload button → "Request without cache".
2. **localStorage write-through is hiding a server failure** — if you see the green "✓ Saved HH:MM" badge next to the field, the localStorage layer is showing you your edit. But did the server actually accept it? Verify:
   ```bash
   cd ~/Desktop/togetherbook && git log --oneline -5 -- people.json
   ```
   If the most recent commit doesn't have "(by <your email>)" within seconds of your save, the write didn't land. Most common reason: Worker validation rejected the edit (see §6).
3. **You're looking at a different Person's record** than you edited — easy to do after a merge or alias rename. Check the URL.
4. **Cloudflare Access expired** — your edit fired a request that got 302'd to login instead of reaching the Worker. Re-authenticate and try again.

**Fix**: see §6 if writes are silently failing.

**Prevention**: already in place — 6-layer reliability (see SPEC.md). If this is still happening, the layers themselves have regressed; run `scripts/check_schema_integrity.py` to confirm data is intact.

---

## 2. "A merge made one person's source icons disappear"

**Symptom**: After merging Person A into Person B, B's Directory row is missing the Google or warehouse chip that A had.

**Diagnosis**: an orphan — a row in `google-accounts.json` or `warehouse-activity.json` whose `person_id` points at the deleted Person A.

```bash
cd ~/Desktop/togetherbook && python3 scripts/check_schema_integrity.py
```

Look for lines like `FAIL  GoogleAccount #N person_id=X → no Person (orphan)`.

**Fix**:
```bash
cd ~/Desktop/togetherbook && python3 - <<'PY'
import json, pathlib, datetime as dt
LOSER_ID = 5     # <-- change to the orphan's person_id
WINNER_ID = 4    # <-- change to who they should be on
now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
for f in ["google-accounts.json", "warehouse-activity.json", "payroll-data.json"]:
    p = pathlib.Path(f); d = json.loads(p.read_text())
    changed = 0
    for r in d.get("records", []):
        if r.get("person_id") == LOSER_ID:
            r["person_id"] = WINNER_ID; changed += 1
    if changed:
        d["updated_at"] = now
        p.write_text(json.dumps(d, indent=2) + "\n")
        print(f"  {f}: re-pointed {changed} record(s)")
PY
git add -A && git commit -m "Recovery: re-point orphan FKs from N → M" && git push
```

Then trigger google-accounts rebuild so the file is regenerated cleanly:
```bash
cd ~/Desktop/togetherbook && python3 scripts/build_google_accounts.py --apply --commit
python3 scripts/check_schema_integrity.py  # confirm zero failures
```

**Prevention**: the worker's `doPeopleMerge` already cascades FKs across all three linked tables AND any other Person's `line_manager_id`. The daily `reconcile-people.yml` workflow runs `check_schema_integrity.py` at 06:30 UTC and fails loudly on any orphan. The next morning you'd get a GitHub Actions failure email.

---

## 3. "I uploaded a new cover/avatar and the old image still shows"

**Diagnosis order**:

1. **Browser caching the image URL** — happens when the timestamp on Person didn't update. Verify:
   ```bash
   cd ~/Desktop/togetherbook && python3 -c "
import json, pathlib
p = next(x for x in json.loads(pathlib.Path('people.json').read_text())['people'] if x.get('url_slug') == 'james.benamor')
print('cover:', p.get('cover_photo_uploaded_at'))
print('avatar:', p.get('directory_photo_uploaded_at'))"
   ```
   If the timestamp is older than your upload, the second worker call (stamp update) failed silently.

2. **File never wrote** — check `git log --since=10.minutes -- assets/covers/` (or `assets/photos/`). No commit = upload didn't reach worker.

**Fix** for case 1 (file is there, stamp didn't update):
```bash
cd ~/Desktop/togetherbook && python3 - <<'PY'
import json, pathlib, datetime as dt
URL_SLUG = "james.benamor"  # <-- change
FIELD = "cover_photo_uploaded_at"  # or "directory_photo_uploaded_at"
now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
p = pathlib.Path("people.json"); d = json.loads(p.read_text())
for person in d["people"]:
    if person.get("url_slug") == URL_SLUG:
        person[FIELD] = now
        person["updated_at"] = now
        print(f"stamped {URL_SLUG} {FIELD} = {now}")
        break
d["updated_at"] = now
p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
PY
git add people.json && git commit -m "Recovery: stamp photo timestamp" && git push
```

**Prevention**: the worker's `uploadImage` retries the stamp-write up to 3 times with backoff, and localStorage write-through means the new image renders immediately on the device that uploaded it.

---

## 4. "The whole site won't load — Cloudflare error / 502 / etc."

**Diagnosis**: usually one of three:

1. **Worker crashed at boot** — a syntax error in `workspace-worker.js` after a deploy. Check the Cloudflare dashboard: <https://dash.cloudflare.com/?to=/:account/workers-and-pages> → apifk-workspace-worker2 → "Last modified" timestamp vs now. If recent and you don't recognise the change, rollback:
   ```bash
   cd ~/Desktop/togetherbook
   git log --oneline -10 worker/workspace-worker.js
   git checkout <previous-good-sha> -- worker/workspace-worker.js
   python3 ~/.togetherbook/deploy_worker.py   # redeploy from local
   ```

2. **GitHub Pages outage** — check <https://www.githubstatus.com>. If Pages is down, the static parts of the site are down too but the Worker (for `/api/*`) keeps working. Nothing to do but wait.

3. **Cloudflare Access cookie expired** — affects only your browser. Open an incognito window + sign in fresh.

---

## 5. "A scheduled refresh workflow failed (GitHub Actions email)"

**Diagnosis**: open the failure email → click the link → "Annotations" section shows the error.

Common causes:

- **`refresh-staff-activity.yml` timeout** — warehouse query took >20min. Re-run from Actions tab (`workflow_dispatch`). If it keeps timing out, the warehouse is slow; bump the timeout in the YAML.
- **`refresh-directory.yml` 503** — Workspace Directory API rate limit. Usually transient; re-run after 10 min.
- **`reconcile-people.yml` schema failure** — `scripts/check_schema_integrity.py` found an orphan. The error message will name the bad FK. See §2 to fix manually, then re-run.
- **`refresh-payroll.yml` Fabric auth** — `FABRIC_CLIENT_SECRET` expired. Regenerate in the Azure portal + update the GitHub Actions secret.

---

## 6. "I tried to save an edit and got a validation error"

**Common worker validation errors** (visible in the red status text below the field):

| Error | Cause | Fix |
|---|---|---|
| `name is required for new people` | Tried to create a Person with no name | Type a name |
| `only one Letme Google account allowed per Person` | The Person already has a Letme email; you tried to add a second | Remove the existing one first, or this is the wrong Person |
| `only one Together Google account allowed per Person` | Same, Together side | As above |
| `self or admin required` | You're not signed in as this Person and not in the admin allowlist | Have an admin do it or sign in as the Person |
| `admin required` | The action is admin-only (delete, merge, sync access) | Have an admin do it |
| `people.json validation failed: ...` | Worker pre-commit validator caught a corrupted state (duplicate id, broken FK) | Run §2 fix; this should only happen if a bulk script wrote bad data |
| `commit failed (409): ...` | Another write hit the same SHA at the same time (rare, two admins editing simultaneously) | Retry — the second write picks up the new SHA |

---

## 7. "Reset everything to a known-good state"

Last resort. Identity tables (people / payroll-data / google-accounts / warehouse-activity / admins) are all in git. Pick a commit you trust and check it out:

```bash
cd ~/Desktop/togetherbook
git log --oneline -- people.json | head -20    # browse history
git checkout <sha> -- people.json payroll-data.json google-accounts.json warehouse-activity.json admins.json
python3 scripts/check_schema_integrity.py      # confirm clean
git add -A && git commit -m "Recovery: rollback to <sha>" && git push
```

GitHub Pages picks up the rollback on the next request. The Worker reads via the GitHub Contents API so it's instant.

Wall posts (`wall.json`), holidays (`holidays.json`), annotations (`annotations.json`) are independent — same trick works for those.

---

## 8. "I bricked admins.json and now nobody is admin"

**Symptom**: nobody (including you) can save anything. The Directory shows the "you are not an admin" banner for everyone.

**Diagnosis**: `admins.json` got committed with the wrong contents (empty list, wrong emails, etc.).

**Fix**: the worker has a hardcoded **OWNER_EMAIL failsafe**. `james.benamor@letme.com` is always admin regardless of what `admins.json` says. So you (signed in as that email) can still hit "Sync access" on the Directory toolbar, which rebuilds admins.json from people.json and recovers.

If you ALSO can't be admin because Cloudflare Access dropped you from the allowlist:

```bash
cd ~/Desktop/togetherbook && python3 - <<'PY'
import json, pathlib
ROLL_BACK_TO = "<git-sha-with-good-admins>"  # e.g. last week
import subprocess
subprocess.run(["git", "checkout", ROLL_BACK_TO, "--", "admins.json"], check=True)
subprocess.run(["git", "commit", "admins.json", "-m", "Recovery: restore admins.json"], check=True)
subprocess.run(["git", "push"], check=True)
PY
```

Then in Cloudflare dashboard, manually add `james.benamor@letme.com` to the Access app's include policy if you got locked out at the edge: <https://dash.cloudflare.com/?to=/:account/access/apps>.

---

## 9. "How do I verify everything is healthy?"

One command tells you the schema is intact:

```bash
cd ~/Desktop/togetherbook && python3 scripts/check_schema_integrity.py
```

Exit 0 = healthy. Anything else = read the failure list at the top and find the matching section above.

For workflow health: <https://github.com/richmondbot2000-prog/togetherbook/actions> — green checks on the right-hand column = healthy.

For Worker health: <https://dash.cloudflare.com/?to=/:account/workers-and-pages> → click into apifk-workspace-worker2 → "Requests" tab. Healthy = a steady trickle of 200s when you're using the site, no spikes of 4xx/5xx.

---

## 10. Who has the keys

- **GitHub**: `richmondbot2000-prog` user with PAT in macOS Keychain (for `gh` CLI). 2FA on the account.
- **Cloudflare API token**: stored in `~/.togetherbook/cloudflare.json`. Scoped to: Workers Scripts edit, Cloudflare Pages edit, D1 edit, Access apps + policies, Zone Rulesets. Can be rotated via <https://dash.cloudflare.com/profile/api-tokens>.
- **Worker secrets** (visible in Cloudflare dashboard, can't read values): GOOGLE_SERVICE_ACCOUNT_JSON, GITHUB_TOKEN, FABRIC_CLIENT_SECRET, CLOUDFLARE_API_TOKEN, GIPHY_API_KEY, IMPERSONATE_USER, IMPERSONATE_USER_TOGETHERLOANS, PAYROLL_KV.
- **GitHub Actions secrets**: FABRIC_CLIENT_SECRET, SCRAPERAPI_KEY, YOUTUBE_API_KEY, WORKSPACE_SERVICE_ACCOUNT_JSON, WORKSPACE_DELEGATE_USER, WORKSPACE_TENANTS, Brandwatch SMTP creds, dormant Telegram/Discord/HIBP/Auth0 vars.
- **Owner failsafe**: `james.benamor@letme.com` hardcoded in `worker/workspace-worker.js` as `OWNER_EMAIL`. Always admin. Cannot be locked out unless someone changes this constant + redeploys the worker.

---

_If a fix isn't here, copy the error message into a fresh Claude Code session and reference this file + SPEC.md._
