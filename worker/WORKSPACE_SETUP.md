# Setting up the workspace-management Worker (one-time)

This is a separate Worker from `apifk-annotations-worker`. The action buttons
on the Directory detail card (Suspend / Unsuspend / Delete / Create user) hit
`book.togetherbook.net/api/workspace/*` and this Worker performs the action
against Google Workspace using a service-account JWT.

Roughly 15 minutes of dashboard clicks. You only do this once.

## Step 1 — Reuse the existing GitHub PAT (or generate a new one)

The audit log writes to `workspace-actions.json` in the repo via the same
GitHub Contents API used by the annotations Worker. The token already issued
to `apifk-annotations-worker` (`Contents: read+write` on this repo) is
sufficient — you can paste the same value, or generate a separate token if
you'd prefer separate rotation. Same flow as before:
https://github.com/settings/personal-access-tokens/new

## Step 2 — Get the service-account JSON ready

You already have the JSON file for `directory-reader@letme-directory.iam.gserviceaccount.com`
on this Mac at `~/Desktop/wiki/letme-directory-f8cf5d0a941f.json` (the one
that's also `WORKSPACE_SERVICE_ACCOUNT_JSON` in GitHub Secrets). The Worker
needs the **whole JSON file contents**, not a path.

Copy it to clipboard now:

```bash
cat ~/Desktop/wiki/letme-directory-f8cf5d0a941f.json | pbcopy
```

## Step 3 — Create the Worker

1. https://dash.cloudflare.com → **Workers & Pages** → **Create**
2. **Start with Hello World!** → **Get started**
3. Name: `apifk-workspace-worker` → **Deploy**
4. Click **Edit code** on the success screen
5. Select all the placeholder code (Cmd+A) → delete
6. Paste the contents of `worker/workspace-worker.js` from this repo:
   ```bash
   cat ~/Desktop/APIsForKids/worker/workspace-worker.js | pbcopy
   ```
7. **Deploy** (top-right)

## Step 4 — Add the secrets and variables

Worker page → **Settings** → **Variables and Secrets** → **+ Add** (one per row, redeploy after all four are set):

| Type     | Name                          | Value                                                                 |
|----------|-------------------------------|-----------------------------------------------------------------------|
| Secret   | `GOOGLE_SERVICE_ACCOUNT_JSON` | The full JSON file contents from Step 2                               |
| Secret   | `GITHUB_TOKEN`                | The same `github_pat_…` value as the annotations Worker (or a new one)|
| Variable | `IMPERSONATE_USER`            | `james.benamor@letme.co.uk`                                           |
| Variable | `ADMIN_EMAILS`                | `james.benamor@letme.com` (comma-separated if you add more later)     |

(`IMPERSONATE_USER` and `ADMIN_EMAILS` are non-secret — they're fine as plain
variables. You can also paste them as Secrets if you'd prefer.)

After saving all four, **Redeploy** the Worker.

## Step 5 — Wire the route

Cloudflare dashboard → `togetherbook.net` zone → **Workers Routes** → **Add route**

- Route: `book.togetherbook.net/api/workspace/*`
- Worker: `apifk-workspace-worker`

Save.

## Step 6 — Tighten Cloudflare Access on the management route (optional but recommended)

By default the existing `book.togetherbook.net` Access policy admits any
`@letme.com` email. The Worker enforces `ADMIN_EMAILS` itself, so even
without a tighter CF Access policy, only James can perform actions. But if
you'd prefer destructive endpoints to also be gated at the edge:

1. Cloudflare → **Zero Trust** → **Access** → **Applications**
2. **Add an application** → **Self-hosted**
3. Application name: `apifk-workspace-management`
4. Application domain: `book.togetherbook.net/api/workspace`
5. Policy: **Allow** group / email list — add `james.benamor@letme.com`
6. Save

## Step 7 — Verify

Open `https://book.togetherbook.net/directory.html`, click a row (any test
user you're OK suspending and immediately unsuspending), click **Manage →
Suspend**, confirm the prompt. You should see "Suspended" in the modal toast
and a new commit on `main` like `Workspace: suspend <email> by james.benamor@letme.com`.

If the action fails:

- **401 not authenticated** — the route isn't behind CF Access, or you're
  not logged in via `book.togetherbook.net`.
- **403 not authorized** — your CF-Access email isn't in `ADMIN_EMAILS`. Add
  it (comma-separated) in Step 4 and redeploy.
- **502 google token exchange failed** — the service account JSON secret is
  malformed, or DWD scopes aren't authorised. Re-check the
  `domain-wide-delegation` page; the scopes should include
  `admin.directory.user` and `apps.licensing`.
- **502 with `forbidden` in the details** — the impersonated user
  (`IMPERSONATE_USER`) isn't a Super Admin. Confirm via Admin Console →
  Admin roles → Super Admin → Admins.

## Notes

- **Why a separate Worker from `apifk-annotations-worker`?** Cleaner blast
  radius. If the workspace Worker has a bug, it can't accidentally clobber
  the annotations file or vice-versa. They share the GitHub token by
  coincidence, not by coupling.
- **License-based actions** (archive a leaver → Archived User license) are
  not in this first cut. Adding them is a follow-up — the scope is already
  authorised, just need to wire the Licensing API endpoints into the Worker.
- **Audit log** lives at `workspace-actions.json` in the repo. It's
  FIFO-trimmed to the last 2000 actions in the Worker to keep the file
  small; older history stays in git via the commit log.
