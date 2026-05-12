# Setting up the annotations Worker (one-time)

The Directory page's "Save" button on the detail card POSTs to a Cloudflare
Worker at `book.togetherbook.net/api/annotations`. The Worker uses a GitHub
fine-grained token to commit changes to `annotations.json` in this repo. The
Directory page reads `annotations.json` directly from the repo on every load.

Roughly 10-15 minutes of clicking through dashboards. You only do this once.

## Step 1 — Create a GitHub fine-grained token

1. https://github.com/settings/personal-access-tokens/new
2. **Token name:** `apifk-annotations-worker`
3. **Resource owner:** `richmondbot2000-prog`
4. **Expiration:** 1 year (max). Set a reminder; renewing is the same flow.
5. **Repository access:** "Only select repositories" → `togetherbook`
6. **Repository permissions:**
   - **Contents:** Read and write
7. Generate, copy the token (starts `github_pat_…`). You won't see it again.

## Step 2 — Create the Cloudflare Worker

1. https://dash.cloudflare.com → your account → **Workers & Pages** → **Create**
2. Choose **Hello World** → **Worker**
3. Name: `apifk-annotations-worker`
4. Click **Deploy** (with the default code — you'll replace it)
5. After deploy: **Edit code** → delete everything → paste the contents of
   `worker/annotations-worker.js` from this repo → **Save and deploy**

## Step 3 — Add the GitHub token as a Worker secret

1. Worker page → **Settings** → **Variables and Secrets** → **Add variable**
2. Type: **Secret**
3. Name: `GITHUB_TOKEN`
4. Value: paste the `github_pat_…` from Step 1
5. **Save**, then **Redeploy** the Worker so it picks up the secret.

## Step 4 — Wire the route

1. Cloudflare dashboard → `togetherbook.net` zone → **Workers Routes**
2. **Add route**:
   - Route: `book.togetherbook.net/api/annotations*`
   - Worker: `apifk-annotations-worker`
3. Save.

## Step 5 — Verify

Open `https://book.togetherbook.net/directory.html`, click a row, type a phone
number in the detail card, click **Save**. You should see "Saved to the team
directory" in the modal toast. Within ~30 seconds the repo will show a new
commit on `main` like `Directory note: set <email>`, and the value will appear
inline on the row when the page next loads.

If the save fails:

- **401 not authenticated via Cloudflare Access** — the route isn't behind
  Access, or you opened the page on `richmondbot2000-prog.github.io` (the
  public github.io URL doesn't go through Access; use `book.togetherbook.net`).
- **500 worker GITHUB_TOKEN secret not configured** — Step 3 secret didn't
  save, or the Worker hasn't been redeployed since the secret was added.
- **502 failed to commit** — token doesn't have `Contents: write` on this
  repo (check Step 1), or the token has expired.

## Notes

- The Worker doesn't need a `wrangler.toml` — everything is configured through
  the dashboard. If you ever want to move to local-deploy via `wrangler`, the
  config in this folder is the only piece of code.
- Cache-busting isn't required for `annotations.json`: the Directory page
  fetches it with `cache: 'no-store'` and the Worker's POST response carries
  the freshly-committed copy, so the page updates immediately on save.
- Token rotation: when the GH PAT nears expiry, generate a new one and replace
  the `GITHUB_TOKEN` secret in Step 3, then redeploy.
