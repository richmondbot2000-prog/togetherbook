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
const ADMINS_PATH = "admins.json";
const BRANCH = "main";

// Cloudflare Access: the allowlist on book.togetherbook.net is auto-synced
// from admins.json so non-@letme.com admins can sign in from any IP. The
// account ID + Access app UID are not secret (they appear in dashboard URLs)
// so they live here as constants; the API token is a secret env var.
const CLOUDFLARE_ACCOUNT_ID = "012bbf0ed36f984997fe0854612fcb01";
const CLOUDFLARE_ACCESS_APP_ID = "cd685a63-7765-47ff-98da-26ed5a57951a";
const ACCESS_POLICY_NAME = "Letme staff + Directory admins";
const ACCESS_DOMAIN_RULE = "letme.com";

// The Owner is a special hardcoded admin who:
//   1. Is always treated as an admin even if absent from admins.json
//   2. Cannot be removed from admins by anyone (including themselves via API)
//   3. Cannot be suspended, password-reset, or have their admin flag changed
//      by anyone but themselves
// This is the "you can't lock yourself out of your own system" safety latch.
const OWNER_EMAIL = "james.benamor@letme.com";
const OWNER_PROTECTED_ACTIONS = new Set([
  "suspend-and-route",
  "suspend-no-forward",
  "reset-password",
  "admin-add",
  "admin-remove",
]);

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

    const url = new URL(req.url);
    const action = url.pathname.replace(/^\/api\/workspace\/?/, "").replace(/\/$/, "");

    const actor = (req.headers.get("Cf-Access-Authenticated-User-Email") || "").toLowerCase();
    const ownerLc = OWNER_EMAIL.toLowerCase();
    const isOwner = actor === ownerLc;
    const admins = await fetchAdmins();
    const isAdmin = admins.includes(actor);

    // `whoami` is the one open endpoint: any Cloudflare-Access-authenticated
    // user can ask "who am I, and do I have admin rights here?" so the page
    // can branch its UI accordingly. No admin check, no Google token needed.
    // Admins also get the full admin list so the page can render per-row
    // admin toggles without a second round-trip.
    if (action === "whoami") {
      return json({
        ok: true,
        email: actor,
        is_admin: isAdmin,
        is_owner: isOwner,
        owner: ownerLc,
        admins: isAdmin ? admins : null,
      }, 200, req);
    }

    // `list-admins` is admin-only but doesn't need a Google token.
    if (action === "list-admins") {
      if (!isAdmin) return json({ error: "admin required" }, 403, req);
      return json({ ok: true, admins, owner: ownerLc }, 200, req);
    }

    // Everything else requires admin status.
    if (!isAdmin) {
      return json({ error: `not authorized — ${actor || "(no email)"} is not an admin. Ask an admin to grant access.` }, 403, req);
    }

    let body;
    try { body = await req.json(); }
    catch { return json({ error: "invalid JSON body" }, 400, req); }

    // Owner-protection: suspend, password reset, and admin flag changes ON
    // the owner require the actor to BE the owner. Stops a rogue admin from
    // locking the owner out of their own system.
    if (OWNER_PROTECTED_ACTIONS.has(action)) {
      const target = ((body.email || body.target_email || "") + "").toLowerCase();
      if (target === ownerLc && !isOwner) {
        return json({ error: "this action on the owner can only be performed by the owner themselves" }, 403, req);
      }
    }

    // admin-add / admin-remove don't need a Google token; handle them before
    // we waste the JWT exchange. The owner is also protected from being
    // removed even when the actor IS the owner — we don't want to brick the
    // system by an accidental click.
    if (action === "admin-add" || action === "admin-remove") {
      if (action === "admin-remove"
          && (body.target_email || "").toLowerCase() === ownerLc) {
        return json({ error: "the owner cannot be removed from admins" }, 403, req);
      }
      let result;
      try {
        result = await modifyAdminList(env, action, body.target_email, actor);
      } catch (e) {
        result = { ok: false, error: e.message };
      }
      // On success, push the new admin list to the Cloudflare Access app
      // allowlist so the new admin can actually sign in from any IP. Non-fatal
      // if the sync fails — the admins.json commit still happened.
      if (result.ok && Array.isArray(result.admins)) {
        try {
          const sync = await syncAccessAllowlist(env, result.admins);
          result.access_sync = sync;
        } catch (e) {
          result.access_sync = { ok: false, error: e.message };
        }
      }
      try {
        await appendAudit(env, {
          ts: new Date().toISOString(),
          actor,
          action,
          target: (body.target_email || "").toLowerCase(),
          ok: !!result.ok,
          ...(result.access_sync ? { access_sync_ok: !!result.access_sync.ok } : {}),
          ...(result.ok ? {} : { error: String(result.error || "").slice(0, 300) }),
        });
      } catch (e) {}
      return json(result, result.ok ? 200 : 400, req);
    }

    if (!env.GOOGLE_SERVICE_ACCOUNT_JSON) {
      return json({ error: "GOOGLE_SERVICE_ACCOUNT_JSON secret not configured" }, 500, req);
    }
    if (!env.IMPERSONATE_USER) {
      return json({ error: "IMPERSONATE_USER var not configured" }, 500, req);
    }

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
        case "user-alias-remove":    result = await doUserAliasRemove(adminToken, body); break;
        case "alias-to-group":       result = await doAliasToGroup(adminToken, body); break;
        case "rename-user":          result = await doRenameUser(adminToken, body); break;
        case "directory-photo-upload": result = await doDirectoryPhotoUpload(env, body, actor); break;
        case "directory-photo-remove": result = await doDirectoryPhotoRemove(env, body, actor); break;
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

// Remove an editable alias from a user. nonEditableAliases (auto-generated by
// Workspace from secondary domains, or auto-created when a primary email is
// renamed) cannot be removed this way and will return 400 — surface that.
async function doUserAliasRemove(token, body) {
  if (!body.user_email) return { ok: false, error: "missing user_email" };
  if (!body.alias) return { ok: false, error: "missing alias" };
  return adminApi(
    token,
    "DELETE",
    `users/${encodeURIComponent(body.user_email)}/aliases/${encodeURIComponent(body.alias)}`,
  );
}

// Change a user's primary email. The rename is instant; Google auto-creates
// a nonEditableAlias at the old address that routes mail to the same mailbox
// for ~21 days, then expires. The page surfaces a countdown while that's
// active. Returns the updated user resource on success.
async function doRenameUser(token, body) {
  if (!body.current_email) return { ok: false, error: "missing current_email" };
  if (!body.new_email) return { ok: false, error: "missing new_email" };
  return adminApi(token, "PATCH", `users/${encodeURIComponent(body.current_email)}`, {
    primaryEmail: body.new_email,
  });
}

// One-shot helper: remove an alias from a user, then create a Group at that
// freed address with the original user as the initial member. Each step is
// atomic from Google's side; we surface partial-failure details so the page
// can show exactly where it broke (e.g. alias removal succeeded but group
// creation collided with an existing group at that address).
async function doAliasToGroup(token, body) {
  if (!body.user_email) return { ok: false, error: "missing user_email" };
  if (!body.alias) return { ok: false, error: "missing alias" };
  if (!body.group_name) return { ok: false, error: "missing group_name" };

  const rem = await adminApi(
    token,
    "DELETE",
    `users/${encodeURIComponent(body.user_email)}/aliases/${encodeURIComponent(body.alias)}`,
  );
  if (!rem.ok) return { ok: false, step: "alias-remove", error: rem.error || "alias removal failed", status: rem.status };

  // Brief propagation pause — Workspace usually frees the address in <2s.
  await new Promise(r => setTimeout(r, 1500));

  const gc = await adminApi(token, "POST", "groups", {
    email: body.alias,
    name: body.group_name,
    ...(body.description ? { description: body.description } : {}),
  });
  if (!gc.ok) return { ok: false, step: "group-create", error: gc.error || "group creation failed", status: gc.status };

  const member = body.initial_member || body.user_email;
  const mb = await adminApi(token, "POST", `groups/${encodeURIComponent(body.alias)}/members`, {
    email: member,
    role: "MEMBER",
  });
  if (!mb.ok) return { ok: false, step: "member-add", error: mb.error || "member add failed", status: mb.status, group_created: true };

  return { ok: true, group_email: body.alias, group_name: body.group_name, member };
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

/* ----------- Directory-page profile photos ----------- */

// Sanitise an email into a safe filename: lowercase, `@` -> `_at_`. The
// path remains URL-safe and uniquely reversible.
function photoFilename(email) {
  return (email || "").toString().trim().toLowerCase().replace(/@/g, "_at_") + ".jpg";
}

// Upload a photo for use only on the Directory page (does NOT touch the
// user's Google Workspace profile photo). Commits the file under
// `assets/photos/` so GitHub Pages serves it directly.
async function doDirectoryPhotoUpload(env, body, actor) {
  if (!env.GITHUB_TOKEN) {
    return { ok: false, error: "GITHUB_TOKEN secret not configured" };
  }
  const email = (body.user_email || "").toString().trim().toLowerCase();
  if (!email || !email.includes("@")) {
    return { ok: false, error: "missing or invalid user_email" };
  }
  const b64 = (body.photo_b64 || "").toString();
  if (!b64) return { ok: false, error: "missing photo_b64" };
  // GitHub's Contents API has a 100MB body limit but practical limits are
  // much smaller. A 400x400 JPEG is typically <80 KB; cap raw base64 to
  // 2 MB so a stray giant upload doesn't bloat the repo.
  if (b64.length > 2 * 1024 * 1024) {
    return { ok: false, error: `photo too large (${Math.round(b64.length / 1024)} KB base64) — resize client-side` };
  }

  const path = `assets/photos/${photoFilename(email)}`;
  return await commitFile(env, path, b64, `Directory photo: upload ${email} (by ${actor})`);
}

// Remove an uploaded Directory photo, reverting the card to the Workspace
// thumbnail (or initials placeholder).
async function doDirectoryPhotoRemove(env, body, actor) {
  if (!env.GITHUB_TOKEN) {
    return { ok: false, error: "GITHUB_TOKEN secret not configured" };
  }
  const email = (body.user_email || "").toString().trim().toLowerCase();
  if (!email || !email.includes("@")) {
    return { ok: false, error: "missing or invalid user_email" };
  }
  const path = `assets/photos/${photoFilename(email)}`;

  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "apifk-workspace-worker",
  };
  const getRes = await fetch(
    `https://api.github.com/repos/${REPO}/contents/${path}?ref=${BRANCH}`,
    { headers: ghHeaders },
  );
  if (getRes.status === 404) {
    return { ok: true, no_op: true, message: "no photo to remove" };
  }
  if (!getRes.ok) {
    return { ok: false, error: `failed to read photo: ${getRes.status}` };
  }
  const sha = (await getRes.json()).sha;
  const delRes = await fetch(
    `https://api.github.com/repos/${REPO}/contents/${path}`,
    {
      method: "DELETE",
      headers: { ...ghHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({
        message: `Directory photo: remove ${email} (by ${actor})`,
        sha,
        branch: BRANCH,
      }),
    },
  );
  if (!delRes.ok) {
    const detail = (await delRes.text()).slice(0, 200);
    return { ok: false, error: `failed to delete (${delRes.status}): ${detail}` };
  }
  return { ok: true, path };
}

// Shared helper: PUT a file to the GitHub Contents API. Handles new files
// (no SHA) and updates (must include SHA of the prior version).
async function commitFile(env, path, b64Content, message) {
  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "apifk-workspace-worker",
  };
  // Look up the existing SHA if the file is already in the repo.
  let sha = null;
  const getRes = await fetch(
    `https://api.github.com/repos/${REPO}/contents/${path}?ref=${BRANCH}`,
    { headers: ghHeaders },
  );
  if (getRes.ok) sha = (await getRes.json()).sha;
  else if (getRes.status !== 404) {
    return { ok: false, error: `pre-commit GET failed: ${getRes.status}` };
  }
  const putRes = await fetch(
    `https://api.github.com/repos/${REPO}/contents/${path}`,
    {
      method: "PUT",
      headers: { ...ghHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        content: b64Content,
        branch: BRANCH,
        sha: sha || undefined,
      }),
    },
  );
  if (!putRes.ok) {
    const detail = (await putRes.text()).slice(0, 200);
    return { ok: false, error: `commit failed (${putRes.status}): ${detail}` };
  }
  return { ok: true, path };
}

/* ----------- Cloudflare Access allowlist sync ----------- */

// Push the current admin list to the Cloudflare Access app's allow policy.
// Build the include list as: every email in `letme.com` (covers any current
// or future @letme.com staff) + every non-@letme.com admin explicitly.
// Non-fatal: if the token isn't set or the PUT fails, the admin change
// itself still succeeded — we just log + return a warning.
async function syncAccessAllowlist(env, admins) {
  if (!env.CLOUDFLARE_API_TOKEN) {
    return { ok: false, error: "CLOUDFLARE_API_TOKEN secret not configured — allowlist not synced" };
  }
  const extras = Array.from(new Set(
    (admins || [])
      .map(e => (e || "").toString().trim().toLowerCase())
      .filter(e => e && !e.endsWith("@" + ACCESS_DOMAIN_RULE)),
  )).sort();
  const include = [
    { email_domain: { domain: ACCESS_DOMAIN_RULE } },
    ...extras.map(e => ({ email: { email: e } })),
  ];

  const url = `https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/access/apps/${CLOUDFLARE_ACCESS_APP_ID}`;
  const headers = {
    "Authorization": `Bearer ${env.CLOUDFLARE_API_TOKEN}`,
    "Content-Type": "application/json",
  };

  const getRes = await fetch(url, { headers });
  if (!getRes.ok) {
    return { ok: false, error: `Access app GET failed: ${getRes.status}` };
  }
  const app = (await getRes.json()).result;
  // Strip server-managed fields before PUT.
  for (const k of ["id", "uid", "created_at", "updated_at", "aud"]) delete app[k];
  for (const p of (app.policies || [])) {
    p.name = ACCESS_POLICY_NAME;
    p.include = include;
    for (const k of ["id", "uid", "created_at", "updated_at"]) delete p[k];
  }

  const putRes = await fetch(url, { method: "PUT", headers, body: JSON.stringify(app) });
  if (!putRes.ok) {
    const detail = (await putRes.text()).slice(0, 200);
    return { ok: false, error: `Access app PUT failed (${putRes.status}): ${detail}` };
  }
  return { ok: true, include_count: include.length };
}

/* ----------- Admin list (admins.json in repo) ----------- */

// Reads admins.json off main with a 60-second edge cache. Always includes
// the hardcoded owner — that's the failsafe so an empty / missing /
// malformed admins.json never locks them out.
async function fetchAdmins() {
  let list = [];
  try {
    const res = await fetch(
      `https://raw.githubusercontent.com/${REPO}/${BRANCH}/${ADMINS_PATH}`,
      { cf: { cacheTtl: 60, cacheEverything: true } },
    );
    if (res.ok) {
      const data = await res.json();
      if (Array.isArray(data.admins)) {
        list = data.admins
          .map(e => (e || "").toString().trim().toLowerCase())
          .filter(Boolean);
      }
    }
  } catch (e) { /* fall through with empty list */ }
  const ownerLc = OWNER_EMAIL.toLowerCase();
  if (!list.includes(ownerLc)) list.push(ownerLc);
  return list;
}

// Commits an admin-add or admin-remove to admins.json via the GitHub
// Contents API. Same SHA-then-PUT pattern as appendAudit. Caller is
// responsible for the owner-protection check (see fetch handler).
async function modifyAdminList(env, action, targetRaw, actor) {
  if (!env.GITHUB_TOKEN) {
    return { ok: false, error: "GITHUB_TOKEN secret not configured on this Worker — admin list can't be modified" };
  }
  const target = (targetRaw || "").toString().trim().toLowerCase();
  if (!target || !target.includes("@")) {
    return { ok: false, error: "missing or invalid target_email" };
  }

  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "apifk-workspace-worker",
  };

  const getRes = await fetch(
    `https://api.github.com/repos/${REPO}/contents/${ADMINS_PATH}?ref=${BRANCH}`,
    { headers: ghHeaders },
  );
  let current = { schema_version: 1, updated_at: null, admins: [] };
  let sha = null;
  if (getRes.ok) {
    const data = await getRes.json();
    sha = data.sha;
    try { current = JSON.parse(atob(data.content.replace(/\s/g, ""))); }
    catch (e) {}
  } else if (getRes.status !== 404) {
    return { ok: false, error: `failed to read admins.json: ${getRes.status}` };
  }
  let admins = Array.isArray(current.admins)
    ? current.admins.map(e => (e || "").toString().trim().toLowerCase()).filter(Boolean)
    : [];

  if (action === "admin-add") {
    if (admins.includes(target)) {
      return { ok: true, admins, no_op: true, message: `${target} is already an admin` };
    }
    admins.push(target);
  } else if (action === "admin-remove") {
    if (!admins.includes(target)) {
      return { ok: true, admins, no_op: true, message: `${target} is not currently an admin` };
    }
    admins = admins.filter(e => e !== target);
  }
  admins.sort();

  const out = {
    schema_version: 1,
    updated_at: new Date().toISOString(),
    admins,
  };
  const newContent = b64Encode(JSON.stringify(out, null, 2) + "\n");
  const msg = action === "admin-add"
    ? `Admin granted: ${target} (by ${actor})`
    : `Admin revoked: ${target} (by ${actor})`;

  const putRes = await fetch(
    `https://api.github.com/repos/${REPO}/contents/${ADMINS_PATH}`,
    {
      method: "PUT",
      headers: { ...ghHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({
        message: msg,
        content: newContent,
        branch: BRANCH,
        sha: sha || undefined,
      }),
    },
  );
  if (!putRes.ok) {
    const detail = (await putRes.text()).slice(0, 200);
    return { ok: false, error: `failed to commit admins.json (${putRes.status}): ${detail}` };
  }
  return { ok: true, admins, target, action };
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
