// workspace-worker.js — Cloudflare Worker that performs Google Workspace
// admin actions (suspend / unsuspend / delete / create user) on behalf of
// the Directory page.
//
// Routes (POST):
//   /api/workspace/suspend    { email }
//   /api/workspace/unsuspend  { email }
//   /api/workspace/delete     { email }
//   /api/workspace/create     { given_name, family_name, email, password,
//                               org_unit_path? }
//
// Auth chain:
//   1. Cloudflare Access already gates the route — only @letme.com sessions
//      reach the Worker. Presence of `Cf-Access-Jwt-Assertion` is verified
//      as a cheap "did this come through Access?" check.
//   2. ADMIN_EMAILS (comma-separated secret) limits destructive actions to
//      named admins (the read-only Directory page is open to all @letme.com
//      but suspend/delete is not).
//
// Google auth: service-account JWT with domain-wide delegation. Impersonates
// IMPERSONATE_USER. Same service account JSON used by scan_directory.py.
//
// Audit: every action appends an entry to workspace-actions.json on `main`
// via the GitHub Contents API. Commit message reflects the action so
// `git log workspace-actions.json` is also a usable audit trail.

const REPO = "richmondbot2000-prog/APIsForKids";
const AUDIT_PATH = "workspace-actions.json";
const BRANCH = "main";

const SCOPES = [
  "https://www.googleapis.com/auth/admin.directory.user",
  "https://www.googleapis.com/auth/apps.licensing",
].join(" ");

export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors(req) });
    }
    if (req.method !== "POST") {
      return json({ error: "method not allowed" }, 405, req);
    }
    if (!req.headers.get("Cf-Access-Jwt-Assertion")) {
      return json({ error: "not authenticated via Cloudflare Access" }, 401, req);
    }

    const actor = (req.headers.get("Cf-Access-Authenticated-User-Email") || "").toLowerCase();
    const adminEmails = (env.ADMIN_EMAILS || "")
      .split(",").map(s => s.trim().toLowerCase()).filter(Boolean);
    if (!adminEmails.includes(actor)) {
      return json({ error: `not authorized — ${actor || "(no email)"} is not in ADMIN_EMAILS` }, 403, req);
    }

    if (!env.GOOGLE_SERVICE_ACCOUNT_JSON) {
      return json({ error: "GOOGLE_SERVICE_ACCOUNT_JSON secret not configured" }, 500, req);
    }
    if (!env.IMPERSONATE_USER) {
      return json({ error: "IMPERSONATE_USER var not configured" }, 500, req);
    }

    const url = new URL(req.url);
    const action = url.pathname.replace(/^\/api\/workspace\/?/, "").replace(/\/$/, "");

    let body;
    try { body = await req.json(); }
    catch { return json({ error: "invalid JSON body" }, 400, req); }

    let token;
    try { token = await getGoogleAccessToken(env); }
    catch (e) { return json({ error: "google token exchange failed: " + e.message }, 502, req); }

    let result;
    try {
      switch (action) {
        case "suspend":   result = await doSuspend(token, body); break;
        case "unsuspend": result = await doUnsuspend(token, body); break;
        case "delete":    result = await doDelete(token, body); break;
        case "create":    result = await doCreate(token, body); break;
        default:
          return json({ error: `unknown action: ${action}` }, 404, req);
      }
    } catch (e) {
      result = { ok: false, error: e.message };
    }

    // Audit log (non-fatal if it fails — the Workspace action already happened).
    try {
      await appendAudit(env, {
        ts: new Date().toISOString(),
        actor,
        action,
        target: body.email || body.primaryEmail || "",
        ok: !!result.ok,
        ...(result.ok ? {} : { error: String(result.error || "").slice(0, 300) }),
      });
    } catch (e) { /* swallow audit failures */ }

    return json(result, result.ok ? 200 : 502, req);
  },
};

/* ----------- Google action implementations ----------- */

async function doSuspend(token, body) {
  if (!body.email) return { ok: false, error: "missing email" };
  return adminApi(token, "PUT", `users/${encodeURIComponent(body.email)}`, { suspended: true });
}
async function doUnsuspend(token, body) {
  if (!body.email) return { ok: false, error: "missing email" };
  return adminApi(token, "PUT", `users/${encodeURIComponent(body.email)}`, { suspended: false });
}
async function doDelete(token, body) {
  if (!body.email) return { ok: false, error: "missing email" };
  return adminApi(token, "DELETE", `users/${encodeURIComponent(body.email)}`);
}
async function doCreate(token, body) {
  const need = ["given_name", "family_name", "email", "password"];
  for (const f of need) if (!body[f]) return { ok: false, error: `missing ${f}` };
  return adminApi(token, "POST", "users", {
    primaryEmail: body.email,
    name: { givenName: body.given_name, familyName: body.family_name },
    password: body.password,
    changePasswordAtNextLogin: true,
    ...(body.org_unit_path ? { orgUnitPath: body.org_unit_path } : {}),
  });
}

async function adminApi(token, method, pathSuffix, payload) {
  const res = await fetch(
    `https://admin.googleapis.com/admin/directory/v1/${pathSuffix}`,
    {
      method,
      headers: {
        "Authorization": `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: payload !== undefined ? JSON.stringify(payload) : undefined,
    },
  );
  const text = await res.text();
  if (!res.ok) {
    let detail = text;
    try { detail = JSON.parse(text).error?.message || text; } catch (e) {}
    return { ok: false, status: res.status, error: detail.slice(0, 500) };
  }
  let data = null;
  if (text) { try { data = JSON.parse(text); } catch (e) { data = text; } }
  return { ok: true, data };
}

/* ----------- Google service-account JWT → access token ----------- */

async function getGoogleAccessToken(env) {
  const sa = JSON.parse(env.GOOGLE_SERVICE_ACCOUNT_JSON);
  if (!sa.client_email || !sa.private_key) {
    throw new Error("service account JSON missing client_email / private_key");
  }
  const now = Math.floor(Date.now() / 1000);
  const header = { alg: "RS256", typ: "JWT", kid: sa.private_key_id };
  const claims = {
    iss: sa.client_email,
    sub: env.IMPERSONATE_USER,
    aud: "https://oauth2.googleapis.com/token",
    scope: SCOPES,
    iat: now,
    exp: now + 3600,
  };
  const enc = o => base64url(new TextEncoder().encode(JSON.stringify(o)));
  const signingInput = `${enc(header)}.${enc(claims)}`;

  // Import the PEM private key.
  const pemBody = sa.private_key
    .replace(/-----BEGIN PRIVATE KEY-----/g, "")
    .replace(/-----END PRIVATE KEY-----/g, "")
    .replace(/\s+/g, "");
  const keyBytes = Uint8Array.from(atob(pemBody), c => c.charCodeAt(0));
  const key = await crypto.subtle.importKey(
    "pkcs8",
    keyBytes,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    key,
    new TextEncoder().encode(signingInput),
  );
  const jwt = `${signingInput}.${base64url(new Uint8Array(sig))}`;

  const res = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
      assertion: jwt,
    }),
  });
  if (!res.ok) {
    throw new Error(`token exchange ${res.status}: ${(await res.text()).slice(0, 300)}`);
  }
  return (await res.json()).access_token;
}

/* ----------- Audit log ----------- */

async function appendAudit(env, entry) {
  if (!env.GITHUB_TOKEN) return;
  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "apifk-workspace-worker",
  };
  const getRes = await fetch(
    `https://api.github.com/repos/${REPO}/contents/${AUDIT_PATH}?ref=${BRANCH}`,
    { headers: ghHeaders },
  );
  let current = { schema_version: 1, updated_at: null, actions: [] };
  let sha = null;
  if (getRes.ok) {
    const data = await getRes.json();
    sha = data.sha;
    try { current = JSON.parse(atob(data.content.replace(/\s/g, ""))); }
    catch (e) { /* fresh file */ }
  }
  current.schema_version = 1;
  current.actions = Array.isArray(current.actions) ? current.actions : [];
  current.actions.push(entry);
  // FIFO-trim to keep the file small.
  if (current.actions.length > 2000) current.actions = current.actions.slice(-2000);
  current.updated_at = entry.ts;
  const newContent = b64Encode(JSON.stringify(current, null, 2) + "\n");
  const msg = `Workspace: ${entry.action} ${entry.target} by ${entry.actor}${entry.ok ? "" : " (failed)"}`;
  await fetch(`https://api.github.com/repos/${REPO}/contents/${AUDIT_PATH}`, {
    method: "PUT",
    headers: { ...ghHeaders, "Content-Type": "application/json" },
    body: JSON.stringify({
      message: msg,
      content: newContent,
      branch: BRANCH,
      sha: sha || undefined,
    }),
  });
}

/* ----------- helpers ----------- */

function cors(req) {
  const origin = req.headers.get("Origin") || "*";
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Cf-Access-Jwt-Assertion",
    "Vary": "Origin",
  };
}
function json(obj, status, req) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...cors(req) },
  });
}
function base64url(bytes) {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/=+$/g, "").replace(/\+/g, "-").replace(/\//g, "_");
}
function b64Encode(s) {
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}
