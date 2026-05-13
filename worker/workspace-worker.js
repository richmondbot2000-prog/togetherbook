// workspace-worker.js — Cloudflare Worker that performs Google Workspace
// admin actions on behalf of the Directory page.
//
// Routes (POST):
//   /api/workspace/suspend-and-route  { email, route_to }
//     1. Add route_to as a forwardingAddress on the user's mailbox
//        (in-domain addresses auto-verify; cross-domain needs verification).
//     2. Enable autoForwarding so incoming mail forwards to route_to.
//     3. Suspend the Workspace user (Admin SDK).
//   /api/workspace/unsuspend          { email }
//     Unsuspends and disables autoForwarding.
//   /api/workspace/create             { given_name, family_name, email,
//                                       password, org_unit_path? }
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

const REPO = "richmondbot2000-prog/togetherbook";
const AUDIT_PATH = "workspace-actions.json";
const BRANCH = "main";

const ADMIN_SCOPES = [
  "https://www.googleapis.com/auth/admin.directory.user",
  "https://www.googleapis.com/auth/admin.directory.group",
  "https://www.googleapis.com/auth/admin.directory.group.member",
  "https://www.googleapis.com/auth/apps.licensing",
].join(" ");
// Gmail settings scope — needed for forwardingAddresses + autoForwarding.
// The Worker impersonates the *target user* (not the admin) for these calls
// because mailbox-settings APIs run as the mailbox owner under DWD.
// Both .basic and .sharing are needed: .sharing for write operations on
// forwardingAddresses + autoForwarding; .basic for reads of the same. Google's
// docs say .sharing covers GET too but in practice the GET returns 403 without
// .basic — so we request both in the JWT.
const GMAIL_SCOPES = [
  "https://www.googleapis.com/auth/gmail.settings.basic",
  "https://www.googleapis.com/auth/gmail.settings.sharing",
].join(" ");

export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors(req) });
    }

    // GET /api/workspace/payroll — returns the payroll JSON stored in the
    // PAYROLL_KV namespace under the key `current`. Behind Cloudflare Access
    // only; the github.io public URL never hits this route, so the PII never
    // leaks via Pages. KV is used (not a Secret) because the JSON is ~28 KB,
    // which exceeds the 5.1 KB Worker Secret limit.
    if (req.method === "GET") {
      const url = new URL(req.url);
      if (url.pathname.replace(/\/$/, "").endsWith("/payroll")) {
        if (!req.headers.get("Cf-Access-Jwt-Assertion")) {
          return json({ error: "not authenticated via Cloudflare Access" }, 401, req);
        }
        if (!env.PAYROLL_KV) {
          return json({ error: "PAYROLL_KV binding not configured yet" }, 503, req);
        }
        const raw = await env.PAYROLL_KV.get("current");
        if (!raw) {
          return json({ error: "PAYROLL_KV has no value at key 'current' yet" }, 503, req);
        }
        try {
          return json(JSON.parse(raw), 200, req);
        } catch (e) {
          return json({ error: "PAYROLL_KV 'current' is not valid JSON: " + e.message }, 500, req);
        }
      }
      return json({ error: "unknown GET endpoint" }, 404, req);
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

    // Tenant-aware impersonation: the page sends body.tenant for any
    // user-targeting action. Together Loans uses a different super-admin
    // because each Workspace customer has its own admin set. SA is shared.
    const tenant = (body.tenant || "").toLowerCase();
    let impersonate = env.IMPERSONATE_USER;
    if (tenant === "togetherloans") {
      if (!env.IMPERSONATE_USER_TOGETHERLOANS) {
        return json({ error: "IMPERSONATE_USER_TOGETHERLOANS var not configured (needed for Together Loans actions)" }, 500, req);
      }
      impersonate = env.IMPERSONATE_USER_TOGETHERLOANS;
    }

    let adminToken;
    try { adminToken = await getGoogleAccessToken(env, impersonate, ADMIN_SCOPES); }
    catch (e) { return json({ error: "google admin token exchange failed (impersonating " + impersonate + "): " + e.message }, 502, req); }

    let result;
    try {
      switch (action) {
        case "suspend-and-route":    result = await doSuspendAndRoute(env, adminToken, body); break;
        case "add-forwarding":       result = await doAddForwarding(env, adminToken, body); break;
        case "disable-forwarding":   result = await doDisableForwarding(env, body); break;
        case "cancel-forwarding":    result = await doCancelForwarding(env, adminToken, body); break;
        case "get-forwarding":       result = await doGetForwarding(env, body); break;
        case "unsuspend":            result = await doUnsuspend(env, adminToken, body); break;
        case "recover":              result = await doRecover(adminToken, body); break;
        case "reset-password":       result = await doResetPassword(adminToken, body); break;
        case "convert-to-group":     result = await doConvertToGroup(adminToken, body); break;
        case "create-forwarding-group": result = await doCreateForwardingGroup(adminToken, body); break;
        case "suspend-no-forward":   result = await doSuspendNoForward(adminToken, body); break;
        case "create":               result = await doCreate(adminToken, body); break;
        case "group-create":         result = await doGroupCreate(adminToken, body); break;
        case "group-delete":         result = await doGroupDelete(adminToken, body); break;
        case "group-member-add":     result = await doGroupMemberAdd(adminToken, body); break;
        case "group-member-remove":  result = await doGroupMemberRemove(adminToken, body); break;
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
        target: body.email || body.primaryEmail || body.group_email || "",
        ok: !!result.ok,
        ...(body.route_to ? { route_to: body.route_to } : {}),
        ...(body.member_email ? { member_email: body.member_email } : {}),
        ...(result.ok ? {} : { error: String(result.error || "").slice(0, 300) }),
      });
    } catch (e) { /* swallow audit failures */ }

    return json(result, result.ok ? 200 : 502, req);
  },
};

/* ----------- Google action implementations ----------- */

async function doSuspendAndRoute(env, adminToken, body) {
  if (!body.email) return { ok: false, error: "missing email" };
  if (!body.route_to) return { ok: false, error: "missing route_to" };

  // Get a mailbox-owner token (DWD-impersonate the target user).
  let mailboxToken;
  try {
    mailboxToken = await getGoogleAccessToken(env, body.email, GMAIL_SCOPES);
  } catch (e) {
    return { ok: false, error: "gmail token exchange failed for " + body.email + ": " + e.message };
  }

  // 1. Add the forwarding address. In-domain auto-verifies; cross-domain
  //    returns 200 with verificationStatus=pending. Already-registered
  //    returns 409 — treat as fine.
  const addRes = await gmailApi(mailboxToken, body.email, "POST", "settings/forwardingAddresses",
    { forwardingEmail: body.route_to });
  if (!addRes.ok && addRes.status !== 409) {
    return { ok: false, error: "addForwardingAddress: " + addRes.error };
  }

  // 2. Enable autoForwarding.
  const fwdRes = await gmailApi(mailboxToken, body.email, "PUT", "settings/autoForwarding", {
    enabled: true,
    emailAddress: body.route_to,
    disposition: "leaveInInbox",
  });
  if (!fwdRes.ok) return { ok: false, error: "setAutoForwarding: " + fwdRes.error };

  // 3. Suspend the user via Admin SDK.
  const susRes = await adminApi(adminToken, "PUT", `users/${encodeURIComponent(body.email)}`, { suspended: true });
  if (!susRes.ok) return { ok: false, error: "suspendUser: " + susRes.error };

  return { ok: true, data: { suspended: true, forwarded_to: body.route_to } };
}

// Add forwarding to an already-suspended user. Google blocks impersonating
// suspended users for Gmail API calls, so we unsuspend → set forwarding →
// re-suspend. The unsuspend window is ~2 seconds. If the Gmail step fails
// we still re-suspend so the user ends up where they started.
async function doAddForwarding(env, adminToken, body) {
  if (!body.email) return { ok: false, error: "missing email" };
  if (!body.route_to) return { ok: false, error: "missing route_to" };

  // Step 1: unsuspend
  const u1 = await adminApi(adminToken, "PUT", `users/${encodeURIComponent(body.email)}`, { suspended: false });
  if (!u1.ok) return { ok: false, error: "unsuspend (step 1): " + u1.error };

  let forwardErr = null;
  try {
    const mailboxToken = await getGoogleAccessToken(env, body.email, GMAIL_SCOPES);
    const addRes = await gmailApi(mailboxToken, body.email, "POST", "settings/forwardingAddresses",
      { forwardingEmail: body.route_to });
    if (!addRes.ok && addRes.status !== 409) {
      forwardErr = "addForwardingAddress: " + addRes.error;
    } else {
      const fwdRes = await gmailApi(mailboxToken, body.email, "PUT", "settings/autoForwarding", {
        enabled: true,
        emailAddress: body.route_to,
        disposition: "leaveInInbox",
      });
      if (!fwdRes.ok) forwardErr = "setAutoForwarding: " + fwdRes.error;
    }
  } catch (e) {
    forwardErr = "gmail step: " + (e.message || e);
  }

  // Step 3: re-suspend (always, even if forwarding failed)
  const u2 = await adminApi(adminToken, "PUT", `users/${encodeURIComponent(body.email)}`, { suspended: true });
  if (!u2.ok && !forwardErr) {
    return { ok: false, error: "re-suspend (step 3): " + u2.error + ". User is currently UNSUSPENDED — investigate." };
  }
  if (forwardErr) return { ok: false, error: forwardErr };
  return { ok: true, data: { suspended: true, forwarded_to: body.route_to } };
}

// Read the current autoForwarding state for an active user. Returns
// { enabled, emailAddress, disposition } or empty {} if nothing set.
// Suspended users can't be impersonated for the Gmail API — caller should
// fall back to the audit-log-based forwardingByEmail in that case.
async function doGetForwarding(env, body) {
  if (!body.email) return { ok: false, error: "missing email" };
  try {
    const mailboxToken = await getGoogleAccessToken(env, body.email, GMAIL_SCOPES);
    const res = await gmailApi(mailboxToken, body.email, "GET", "settings/autoForwarding");
    if (!res.ok) return { ok: false, error: res.error, status: res.status };
    return { ok: true, data: res.data || {} };
  } catch (e) {
    return { ok: false, error: "gmail get failed: " + (e.message || e) };
  }
}

// Disable autoForwarding for an active user. Standalone — used when you set
// up forwarding on yourself for testing and want to switch it back off
// without going to Gmail settings. Best-effort: no-op if nothing was set.
async function doDisableForwarding(env, body) {
  if (!body.email) return { ok: false, error: "missing email" };
  try {
    const mailboxToken = await getGoogleAccessToken(env, body.email, GMAIL_SCOPES);
    const res = await gmailApi(mailboxToken, body.email, "PUT", "settings/autoForwarding", { enabled: false });
    if (!res.ok) return { ok: false, error: "setAutoForwarding: " + res.error };
    return { ok: true, data: { autoForwarding: false } };
  } catch (e) {
    return { ok: false, error: "gmail token / call failed: " + (e.message || e) };
  }
}

// Cancel forwarding on a SUSPENDED user — leaves the account suspended
// (in the black-hole list, mail goes nowhere). Same pattern as add-forwarding:
// briefly unsuspend, disable autoForwarding, re-suspend. ~2 seconds.
async function doCancelForwarding(env, adminToken, body) {
  if (!body.email) return { ok: false, error: "missing email" };
  const u1 = await adminApi(adminToken, "PUT", `users/${encodeURIComponent(body.email)}`, { suspended: false });
  if (!u1.ok) return { ok: false, error: "unsuspend (step 1): " + u1.error };
  let fwdErr = null;
  try {
    const mailboxToken = await getGoogleAccessToken(env, body.email, GMAIL_SCOPES);
    const res = await gmailApi(mailboxToken, body.email, "PUT", "settings/autoForwarding", { enabled: false });
    if (!res.ok) fwdErr = "setAutoForwarding off: " + res.error;
  } catch (e) {
    fwdErr = "gmail step: " + (e.message || e);
  }
  // Always re-suspend, even on failure.
  const u2 = await adminApi(adminToken, "PUT", `users/${encodeURIComponent(body.email)}`, { suspended: true });
  if (!u2.ok && !fwdErr) return { ok: false, error: "re-suspend (step 3): " + u2.error + ". User is currently UNSUSPENDED — investigate." };
  if (fwdErr) return { ok: false, error: fwdErr };
  return { ok: true, data: { suspended: true, autoForwarding: false } };
}

async function doUnsuspend(env, adminToken, body) {
  if (!body.email) return { ok: false, error: "missing email" };
  // Unsuspend first (so subsequent mailbox calls have a live account).
  const susRes = await adminApi(adminToken, "PUT", `users/${encodeURIComponent(body.email)}`, { suspended: false });
  if (!susRes.ok) return { ok: false, error: "unsuspendUser: " + susRes.error };
  // Best-effort: disable autoForwarding (we don't fail the action if this errors).
  try {
    const mailboxToken = await getGoogleAccessToken(env, body.email, GMAIL_SCOPES);
    await gmailApi(mailboxToken, body.email, "PUT", "settings/autoForwarding", { enabled: false });
  } catch (e) { /* swallow */ }
  return { ok: true, data: { suspended: false } };
}

// Recover a deleted user (within Workspace's 20-day undelete window).
// Requires the user's immutable id, not the email — the email may have been
// recycled. Restores into the root OU by default.
async function doRecover(token, body) {
  if (!body.user_id) return { ok: false, error: "missing user_id" };
  return adminApi(token, "POST", `users/${encodeURIComponent(body.user_id)}/undelete`, {
    orgUnitPath: body.org_unit_path || "/",
  });
}

// Reset a user's password. Caller supplies the new password (so the page can
// show + copy it once); we don't generate one on the server. Forces password
// change on next sign-in so the new password is just for the handover.
async function doResetPassword(token, body) {
  if (!body.email) return { ok: false, error: "missing email" };
  if (!body.password) return { ok: false, error: "missing password" };
  return adminApi(token, "PUT", `users/${encodeURIComponent(body.email)}`, {
    password: body.password,
    changePasswordAtNextLogin: true,
  });
}

// Fully automated convert-to-group. Sidesteps Google's 20-day email lockout
// (which applies to a deleted user's PRIMARY email) by renaming first:
//   1. Rename the user: primary <local>@<domain> -> <local>.parked.<ts>@<domain>
//   2. Delete the renamed user. The PARKED primary goes into 20-day lockout.
//      The ORIGINAL address — which was an auto-created nonEditableAlias on
//      the renamed user — is released immediately (per Google docs:
//      "Aliases of deleted users are not reserved").
//   3. Create the Group at the freed original address.
//   4. Add the forward target as a member.
// Step 1 of convert-to-group: rename the user to a parked address, then
// delete the renamed user. Google then locks the original address for
// 20 days. After that period a daily cron creates the Group + adds the
// forward target as a member (see scripts/finalise_pending_conversions.py).
//
// The page is expected to also record the pending conversion in
// annotations.json (so the cron has everything it needs: forward_to +
// scheduled_for). The Worker only handles the part that requires admin
// API access; everything else is page + cron.
async function doConvertToGroup(token, body) {
  if (!body.email) return { ok: false, error: "missing email" };
  if (!body.forward_to) return { ok: false, error: "missing forward_to" };
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(body.forward_to)) {
    return { ok: false, error: "forward_to is not a valid email" };
  }
  const [local, domain] = body.email.split("@");
  if (!local || !domain) return { ok: false, error: "email parse failed" };
  const parkedEmail = `${local}.parked.${Math.floor(Date.now() / 1000)}@${domain}`;

  // Look up the immutable user id for the delete step (the renamed primary
  // isn't immediately queryable).
  let userId = body.user_id || null;
  if (!userId) {
    const fetched = await adminApi(token, "GET", `users/${encodeURIComponent(body.email)}`);
    if (!fetched.ok) return { ok: false, error: "fetch user (to capture id): " + fetched.error };
    userId = fetched.data && fetched.data.id;
    if (!userId) return { ok: false, error: "user record has no immutable id — can't proceed safely" };
  }

  // Rename to a parked address.
  const ren = await adminApi(token, "PUT", `users/${encodeURIComponent(body.email)}`, {
    primaryEmail: parkedEmail,
  });
  if (!ren.ok) return { ok: false, error: "rename user: " + ren.error };

  // Delete the renamed user. The original address goes into 20-day lockout —
  // unavoidable in Google's API surface. The page records a pending
  // conversion which the daily cron will finalise once the window expires.
  const del = await adminApi(token, "DELETE", `users/${encodeURIComponent(userId)}`);
  if (!del.ok) {
    return { ok: false, error: "delete parked user (id=" + userId + ", renamed to " + parkedEmail + "): " + del.error };
  }

  // Compute the scheduled finalisation date (20 days + a 1-hour cushion).
  const scheduledFor = new Date(Date.now() + (20 * 24 + 1) * 3600 * 1000).toISOString();
  return {
    ok: true,
    data: {
      stage: "pending",
      original_email: body.email,
      forward_to: body.forward_to,
      parked_at: parkedEmail,
      scheduled_for: scheduledFor,
      message: "User deleted. Mail will bounce until " + scheduledFor.slice(0, 10) +
               " (Google's 20-day address-reuse lockout). The cron will create the forwarding group on that date.",
    },
  };
}

// Create a forwarding-only Group at an address that's already been freed
// (by the admin manually deleting the user with "Make email available for
// reuse immediately" ticked in admin.google.com). Adds the forward target
// as a member. The delete step has to be manual because the API can't tick
// that checkbox; if we tried to delete the user here we'd hit the 20-day
// address lockout.
// Just suspend the user — no forwarding setup. Used as the 'I want them
// offboarded but mail can bounce' action. Symmetrical with unsuspend.
async function doSuspendNoForward(token, body) {
  if (!body.email) return { ok: false, error: "missing email" };
  return adminApi(token, "PUT", `users/${encodeURIComponent(body.email)}`, { suspended: true });
}

async function doCreateForwardingGroup(token, body) {
  if (!body.email) return { ok: false, error: "missing email" };
  if (!body.forward_to) return { ok: false, error: "missing forward_to" };
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(body.forward_to)) {
    return { ok: false, error: "forward_to is not a valid email" };
  }
  const local = body.email.split("@")[0] || "ex-employee";
  const groupName = body.name || (local.replace(/[._-]+/g, " ") + " (ex-employee)");
  const groupDescription = body.description ||
    `Forwarding-only group at ${body.email}. Created on ${new Date().toISOString().slice(0, 10)} after the Workspace user was offboarded.`;
  const grp = await adminApi(token, "POST", "groups", {
    email: body.email,
    name: groupName,
    description: groupDescription,
  });
  if (!grp.ok) {
    return {
      ok: false,
      error: "create group: " + grp.error +
        " — if 'Entity already exists', the user wasn't fully deleted (or wasn't deleted with the 'free email immediately' option ticked). Open admin.google.com, delete the user with that option, then retry.",
    };
  }
  const mem = await adminApi(token, "POST", `groups/${encodeURIComponent(body.email)}/members`, {
    email: body.forward_to,
    role: "MEMBER",
  });
  if (!mem.ok) {
    return { ok: false, error: "group created but member add failed: " + mem.error };
  }
  return { ok: true, data: { group_email: body.email, member: body.forward_to } };
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

/* ----------- Group actions ----------- */

async function doGroupCreate(token, body) {
  if (!body.email) return { ok: false, error: "missing email" };
  if (!body.name) return { ok: false, error: "missing name" };
  return adminApi(token, "POST", "groups", {
    email: body.email,
    name: body.name,
    ...(body.description ? { description: body.description } : {}),
  });
}

async function doGroupDelete(token, body) {
  if (!body.email) return { ok: false, error: "missing email" };
  return adminApi(token, "DELETE", `groups/${encodeURIComponent(body.email)}`);
}

async function doGroupMemberAdd(token, body) {
  if (!body.group_email) return { ok: false, error: "missing group_email" };
  if (!body.member_email) return { ok: false, error: "missing member_email" };
  return adminApi(token, "POST", `groups/${encodeURIComponent(body.group_email)}/members`, {
    email: body.member_email,
    role: body.role || "MEMBER",
  });
}

async function doGroupMemberRemove(token, body) {
  if (!body.group_email) return { ok: false, error: "missing group_email" };
  if (!body.member_email) return { ok: false, error: "missing member_email" };
  return adminApi(token, "DELETE",
    `groups/${encodeURIComponent(body.group_email)}/members/${encodeURIComponent(body.member_email)}`);
}

async function gmailApi(token, userEmail, method, suffix, payload) {
  const res = await fetch(
    `https://gmail.googleapis.com/gmail/v1/users/${encodeURIComponent(userEmail)}/${suffix}`,
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

async function getGoogleAccessToken(env, subject, scope) {
  const sa = JSON.parse(env.GOOGLE_SERVICE_ACCOUNT_JSON);
  if (!sa.client_email || !sa.private_key) {
    throw new Error("service account JSON missing client_email / private_key");
  }
  const now = Math.floor(Date.now() / 1000);
  const header = { alg: "RS256", typ: "JWT", kid: sa.private_key_id };
  const claims = {
    iss: sa.client_email,
    sub: subject,
    aud: "https://oauth2.googleapis.com/token",
    scope: scope,
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
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
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
