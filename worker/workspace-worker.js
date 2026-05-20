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
const PENDING_TRANSFERS_PATH = "pending-transfers.json";
const ADMINS_PATH = "admins.json";
const WALL_PATH = "wall.json";
const WALL_SEEN_PATH = "wall-seen.json";
const BRANCH = "main";

// Cloudflare Access: the allowlist on book.togetherbook.net is auto-synced
// from admins.json so non-@letme.com admins can sign in from any IP. The
// account ID + Access app UID are not secret (they appear in dashboard URLs)
// so they live here as constants; the API token is a secret env var.
const CLOUDFLARE_ACCOUNT_ID = "012bbf0ed36f984997fe0854612fcb01";
const CLOUDFLARE_ACCESS_APP_ID = "cd685a63-7765-47ff-98da-26ed5a57951a";
const ACCESS_POLICY_NAME = "RG staff + Directory admins";
// Every RG-owned Workspace domain. Anyone with a Google account on any
// of these domains can sign into book.togetherbook.net. Outside-RG
// admins are added separately via the extras list. clearloans.com.au
// is intentionally NOT here — its tiny user list (3 active) can be
// covered by admins.json explicit entries when needed.
const ACCESS_DOMAIN_RULES = ["letme.com", "togetherloans.com", "letme.co.uk"];

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
  "delete-account",
  "data-transfer",
  "queue-transfer-and-delete",
  "reset-password",
  "admin-add",
  "admin-remove",
]);

const ADMIN_SCOPES = [
  "https://www.googleapis.com/auth/admin.directory.user",
  "https://www.googleapis.com/auth/admin.directory.group",
  "https://www.googleapis.com/auth/admin.directory.group.member",
  "https://www.googleapis.com/auth/apps.licensing",
  // Drive/Calendar data transfer (used before user deletion to preserve a
  // colleague's access to documents). Requires this scope to also be added
  // in admin.google.com → Security → API controls → Domain-wide delegation
  // → edit the existing service account Client ID → add this scope.
  "https://www.googleapis.com/auth/admin.datatransfer",
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

    // ── Wall API ──────────────────────────────────────────────────────
    // /api/wall/* lives in this same Worker but is routed separately so
    // any Cloudflare-Access-authenticated user can post / comment /
    // react. No admin gating — every logged-in viewer can use the Wall.
    const url0 = new URL(req.url);
    if (url0.pathname.startsWith("/api/wall/")) {
      return await handleWall(req, env, url0);
    }
    // ── Holidays API ──────────────────────────────────────────────────
    // /api/holidays/* — per-user attendance calendars. Any signed-in
    // user can edit their own days; admins can edit anyone's.
    if (url0.pathname.startsWith("/api/holidays/")) {
      return await handleHolidays(req, env, url0);
    }
    // /api/bookr/* — read/write rg-bookr Firebase Realtime DB without
    // touching the existing BookR app or its AirBnB/Guesty syncs.
    if (url0.pathname.startsWith("/api/bookr/")) {
      return await handleBookr(req, env, url0);
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
      // GET /api/activity — returns the 15-min bucket + detail-event
      // slice for the requested emails + date range. Replaces the
      // page's old "fetch the whole staff-activity-buckets.json from
      // raw GitHub" path; reads from the D1 binding ACTIVITY_DB.
      // Query string: ?from=YYYY-MM-DD&to=YYYY-MM-DD&emails=a@x,b@y
      // Authorisation: viewer can always read their own row;
      // line managers can read their direct reports; admins anyone.
      if (url.pathname.replace(/\/$/, "").endsWith("/activity")) {
        return await handleActivityRead(req, env, url);
      }
      // GET /api/activity-items — per-item drill-down for a single
      // (email, iso_date, bucket) cell.  For comm sources, includes
      // body excerpt + ComType + ClientType + ClientUsername +
      // CampaignName + AutoProcessed.
      if (url.pathname.replace(/\/$/, "").endsWith("/activity-items")) {
        return await handleActivityItemsRead(req, env, url);
      }
      // GET /api/workspace/whoami — mirror of the POST whoami below so
      // simple `fetch(url)` calls from the page (the avatar chip in
      // nav.js, the directory bootstrap) resolve identity without
      // having to specify method:POST. Same payload shape as the POST
      // path further down.
      if (url.pathname.replace(/\/$/, "").endsWith("/whoami")) {
        if (!req.headers.get("Cf-Access-Jwt-Assertion")) {
          return json({ error: "not authenticated via Cloudflare Access" }, 401, req);
        }
        const actor = (req.headers.get("Cf-Access-Authenticated-User-Email") || "").toLowerCase();
        const ownerLc = OWNER_EMAIL.toLowerCase();
        const isOwner = actor === ownerLc;
        const admins = await fetchAdmins();
        const isAdmin = admins.includes(actor);
        return json({
          ok: true,
          email: actor,
          is_admin: isAdmin,
          is_owner: isOwner,
          owner: ownerLc,
          admins: isAdmin ? admins : null,
        }, 200, req);
      }
      // GET /api/workspace/table?file=people|payroll-data|google-accounts|warehouse-activity
      // Authoritative read: pulls the file from the GitHub Contents API
      // at the CURRENT HEAD of main, bypassing both GitHub Pages publish
      // lag (~30-60s) and Cloudflare's edge cache. The same SHA the
      // Worker just wrote to is the SHA the next read returns.
      //
      // This is the read path for any table that must reflect the very
      // last admin write without lag — people, payroll, google-accounts,
      // warehouse-activity. Other static tables (staff, wall, holidays,
      // brokers, etc.) can keep using /<file>.json from Pages.
      if (url.pathname.replace(/\/$/, "").endsWith("/table")) {
        if (!req.headers.get("Cf-Access-Jwt-Assertion")) {
          return json({ error: "not authenticated via Cloudflare Access" }, 401, req);
        }
        const allowed = new Set(["people", "payroll-data", "google-accounts", "warehouse-activity"]);
        const want = (url.searchParams.get("file") || "").toLowerCase();
        if (!allowed.has(want)) return json({ error: `file must be one of: ${[...allowed].join(", ")}` }, 400, req);
        if (!env.GITHUB_TOKEN)  return json({ error: "GITHUB_TOKEN not configured" }, 500, req);
        try {
          const res = await fetch(
            `https://api.github.com/repos/${REPO}/contents/${want}.json?ref=${BRANCH}`,
            {
              headers: {
                "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
                "Accept": "application/vnd.github+json",
                "User-Agent": "apifk-workspace-worker",
              },
              // Don't let Cloudflare cache GitHub's response either —
              // we want every call to hit the API.
              cf: { cacheTtl: 0, cacheEverything: false },
            },
          );
          if (!res.ok) return json({ error: `github contents API: ${res.status}` }, 502, req);
          const data = await res.json();
          const bin = atob((data.content || "").replace(/\s/g, ""));
          const text = new TextDecoder("utf-8").decode(Uint8Array.from(bin, c => c.charCodeAt(0)));
          // Return the table content plus the sha + commit timestamp
          // so the client can show "as of <time>" + verify freshness.
          const headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "X-Table-Sha": data.sha || "",
          };
          for (const [k, v] of Object.entries(cors(req))) headers[k] = v;
          return new Response(text, { status: 200, headers });
        } catch (e) {
          return json({ error: `table fetch failed: ${e.message}` }, 502, req);
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

    // POST /api/workspace/refresh-activity — admin-only. Dispatches the
    // refresh-staff-activity workflow with start_date + end_date inputs
    // (defaults to "the calendar month containing today"). Lets an
    // admin click "Refresh data" on the Activity tab and trigger a
    // backfill without leaving the page.
    if (new URL(req.url).pathname.replace(/\/$/, "").endsWith("/refresh-activity")) {
      const viewer = (req.headers.get("Cf-Access-Authenticated-User-Email") || "").toLowerCase();
      const admins = await fetchAdmins();
      if (!admins.includes(viewer)) {
        return json({ error: "admin only" }, 403, req);
      }
      let body = {};
      try { body = await req.json(); } catch (e) {}
      const start_date = String(body.start_date || "").trim();
      const end_date   = String(body.end_date || "").trim();
      if (!/^\d{4}-\d{2}-\d{2}$/.test(start_date) || !/^\d{4}-\d{2}-\d{2}$/.test(end_date)) {
        return json({ error: "start_date / end_date required as YYYY-MM-DD" }, 400, req);
      }
      if (!env.GITHUB_TOKEN) {
        return json({ error: "GITHUB_TOKEN not configured on the worker" }, 500, req);
      }
      const dispatch = await fetch(
        `https://api.github.com/repos/${REPO}/actions/workflows/refresh-staff-activity.yml/dispatches`,
        {
          method: "POST",
          headers: {
            "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
            "Accept": "application/vnd.github+json",
            "User-Agent": "apifk-workspace-worker",
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ ref: BRANCH, inputs: { start_date, end_date } }),
        },
      );
      if (!dispatch.ok) {
        const detail = (await dispatch.text()).slice(0, 200);
        return json({ error: `dispatch failed: HTTP ${dispatch.status} ${detail}` }, 502, req);
      }
      return json({ ok: true, start_date, end_date, triggered_by: viewer }, 200, req);
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

    // people-set + photo uploads have a self-edit carve-out (any signed-in
    // user can edit their own record / avatar / cover), so we let them
    // through the admin gate and check ownership inside the handler. Read
    // the body now since both branches need it.
    let body;
    try { body = await req.json(); }
    catch { return json({ error: "invalid JSON body" }, 400, req); }

    const SELF_EDITABLE_ACTIONS = new Set(["people-set", "cover-photo-upload", "cover-photo-remove", "directory-photo-upload", "directory-photo-remove"]);
    if (!isAdmin && !SELF_EDITABLE_ACTIONS.has(action)) {
      return json({ error: `not authorized — ${actor || "(no email)"} is not an admin. Ask an admin to grant access.` }, 403, req);
    }

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

    // people-merge — admin-only, collapses two Person records into one.
    if (action === "people-merge") {
      if (!isAdmin) return json({ error: "admin required" }, 403, req);
      let result;
      try { result = await doPeopleMerge(env, body, actor); }
      catch (e) { result = { ok: false, error: e.message }; }
      try {
        await appendAudit(env, {
          ts: new Date().toISOString(),
          actor, action,
          target: ((body.loser_id || "") + " → " + (body.winner_id || "")).toLowerCase(),
          ok: !!result.ok,
          ...(result.ok ? {} : { error: String(result.error || "").slice(0, 300) }),
        });
      } catch (e) {}
      return json(result, result.ok ? 200 : 400, req);
    }

    // google-account-set / google-account-delete — admin-only,
    // mutate google-accounts.json + keep person.json email fields in sync.
    if (action === "google-account-set" || action === "google-account-delete") {
      let result;
      try {
        result = (action === "google-account-set")
          ? await doGoogleAccountSet(env, body, actor, isAdmin)
          : await doGoogleAccountDelete(env, body, actor, isAdmin);
      } catch (e) { result = { ok: false, error: e.message }; }
      try {
        await appendAudit(env, {
          ts: new Date().toISOString(),
          actor, action,
          target: (body.email || body.id || "").toString().toLowerCase(),
          ok: !!result.ok,
          ...(result.ok ? {} : { error: String(result.error || "").slice(0, 300) }),
        });
      } catch (e) {}
      return json(result, result.ok ? 200 : 400, req);
    }

    // payroll-set — admin-only, edits a Person's most-recent PayrollData
    // record (or creates the blank shell if Person is on_payroll=true and
    // has no record yet).
    if (action === "payroll-set") {
      if (!isAdmin) return json({ error: "admin required" }, 403, req);
      let result;
      try { result = await doPayrollSet(env, body, actor); }
      catch (e) { result = { ok: false, error: e.message }; }
      try {
        await appendAudit(env, {
          ts: new Date().toISOString(),
          actor, action,
          target: (body.person_id || "").toLowerCase(),
          ok: !!result.ok,
          ...(result.ok ? {} : { error: String(result.error || "").slice(0, 300) }),
        });
      } catch (e) {}
      return json(result, result.ok ? 200 : 400, req);
    }

    // Manual access sync — admin-only. Re-derives admins.json from
    // people.json and pushes the resulting allow-list to Cloudflare Access.
    if (action === "people-sync-access") {
      if (!isAdmin) return json({ error: "admin required" }, 403, req);
      try {
        const { file } = await fetchPeopleFile(env);
        const adminSync = await syncAdminsFromPeople(env, file, actor);
        let accessSync = null;
        if (adminSync.ok && adminSync.admins) {
          accessSync = await syncAccessAllowlist(env, adminSync.admins);
        }
        return json({ ok: !!(adminSync.ok && (accessSync ? accessSync.ok : true)),
                      admin_sync: adminSync, access_sync: accessSync }, 200, req);
      } catch (e) {
        return json({ ok: false, error: e.message }, 500, req);
      }
    }

    // People table CRUD — no Google token needed, just GitHub.
    if (action === "people-set" || action === "people-delete") {
      if (action === "people-delete" && !isAdmin) {
        return json({ error: "admin required to delete a Person record" }, 403, req);
      }
      let result;
      try {
        result = (action === "people-set")
          ? await doPeopleSet(env, body, actor, isAdmin)
          : await doPeopleDelete(env, body, actor);
      } catch (e) {
        result = { ok: false, error: e.message };
      }
      try {
        await appendAudit(env, {
          ts: new Date().toISOString(),
          actor, action,
          target: (body.id || body.main_google_email || "").toLowerCase(),
          ok: !!result.ok,
          ...(result.ok ? {} : { error: String(result.error || "").slice(0, 300) }),
        });
      } catch (e) {}
      return json(result, result.ok ? 200 : 400, req);
    }

    // Avatar / cover photo uploads — same self-or-admin gating handled
    // inside the doPhoto helpers. Goes through GitHub Contents API, no
    // Google token.
    if (action === "cover-photo-upload" || action === "cover-photo-remove") {
      let result;
      try {
        result = (action === "cover-photo-upload")
          ? await doCoverPhotoUpload(env, body, actor, isAdmin)
          : await doCoverPhotoRemove(env, body, actor, isAdmin);
      } catch (e) { result = { ok: false, error: e.message }; }
      return json(result, result.ok ? 200 : 400, req);
    }
    if (!isAdmin && (action === "directory-photo-upload" || action === "directory-photo-remove")) {
      // Self-only check for directory (avatar) photos.
      const target = ((body.user_email || "") + "").toLowerCase();
      const ok = await actorOwnsEmail(env, actor, target);
      if (!ok) return json({ error: "self or admin required for avatar changes" }, 403, req);
    }

    if (!env.GOOGLE_SERVICE_ACCOUNT_JSON) {
      return json({ error: "GOOGLE_SERVICE_ACCOUNT_JSON secret not configured" }, 500, req);
    }
    if (!env.IMPERSONATE_USER) {
      return json({ error: "IMPERSONATE_USER var not configured" }, 500, req);
    }

    // Per-tenant impersonation. Each Workspace customer (letme.com,
    // letme.co.uk, togetherloans.com) has its own super-admin set + its
    // own DWD grant on the shared service account, so we route by the
    // domain of the email being acted on. Falls back to body.tenant for
    // group/legacy actions that don't carry a user_email.
    const routingEmail = (body.user_email || body.group_email || body.email || "").toLowerCase();
    const routingDomain = routingEmail.includes("@") ? routingEmail.split("@")[1] : "";
    const tenantImpersonators = {
      "letme.com":         env.IMPERSONATE_USER,
      "letme.co.uk":       env.IMPERSONATE_USER_LETMECOUK,
      "togetherloans.com": env.IMPERSONATE_USER_TOGETHERLOANS,
    };
    let impersonate = tenantImpersonators[routingDomain];
    if (!impersonate) {
      const legacyTenant = (body.tenant || "").toLowerCase();
      if (legacyTenant === "togetherloans") impersonate = env.IMPERSONATE_USER_TOGETHERLOANS;
      else impersonate = env.IMPERSONATE_USER;
    }
    if (!impersonate) {
      return json({ error: `no impersonator configured for domain ${routingDomain || "(unknown)"} — set IMPERSONATE_USER_${(routingDomain || "").toUpperCase().replace(/\./g, "")} on the worker` }, 500, req);
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
        case "delete-account":       result = await doDelete(adminToken, body); break;
        case "data-transfer":        result = await doDataTransfer(adminToken, body); break;
        case "queue-transfer-and-delete": result = await doQueueTransferAndDelete(env, adminToken, body); break;
        case "create":               result = await doCreate(adminToken, body); break;
        case "group-create":         result = await doGroupCreate(adminToken, body); break;
        case "group-delete":         result = await doGroupDelete(adminToken, body); break;
        case "group-member-add":     result = await doGroupMemberAdd(adminToken, body); break;
        case "group-member-remove":  result = await doGroupMemberRemove(adminToken, body); break;
        case "user-alias-remove":    result = await doUserAliasRemove(adminToken, body); break;
        case "user-alias-add":       result = await doUserAliasAdd(adminToken, body); break;
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

// Delete the Workspace user via Admin SDK. This is the ONLY action that
// stops the seat charge — suspending a user does not (Google bills suspended
// seats at full price). After deletion the account is locked + recoverable
// from admin.google.com for 20 days; on day 21 the mailbox + Drive + every
// piece of data are permanently deleted by Google.
//
// The license is freed immediately, so the £11/mo charge stops as soon as
// the next billing tick.
async function doDelete(token, body) {
  if (!body.email) return { ok: false, error: "missing email" };
  return adminApi(token, "DELETE", `users/${encodeURIComponent(body.email)}`);
}

// Transfer a user's Drive ownership to another user via the Admin SDK Data
// Transfer API. Called BEFORE deletion when the admin wants to preserve a
// colleague's access to the leaver's documents.
//
// Asynchronous: returns a transfer id immediately. The actual ownership
// change happens in the background and can take hours/days for large drives.
// It's safe to delete the source user immediately after this returns —
// Google completes the queued transfer regardless of source deletion.
//
// Requires the admin.datatransfer scope in ADMIN_SCOPES *and* in the
// Workspace DWD allowlist (admin.google.com → API controls).
async function doDataTransfer(token, body) {
  if (!body.email) return { ok: false, error: "missing email (source)" };
  if (!body.target_email) return { ok: false, error: "missing target_email" };

  const src = await adminApi(token, "GET", `users/${encodeURIComponent(body.email)}`);
  if (!src.ok) return { ok: false, error: "source user fetch: " + src.error };
  if (!src.data || !src.data.id) return { ok: false, error: "source user has no immutable id" };

  const tgt = await adminApi(token, "GET", `users/${encodeURIComponent(body.target_email)}`);
  if (!tgt.ok) return { ok: false, error: "target user fetch: " + tgt.error };
  if (!tgt.data || !tgt.data.id) return { ok: false, error: "target user has no immutable id" };
  if (tgt.data.suspended) return { ok: false, error: "target user is suspended — they cannot receive transferred Drive ownership" };

  // Look up Drive's application id dynamically. Hard-coding (55656082996)
  // would also work but discovery is robust against any future change.
  const appsRes = await fetch("https://admin.googleapis.com/admin/datatransfer/v1/applications", {
    headers: { "Authorization": `Bearer ${token}` },
  });
  if (!appsRes.ok) {
    const txt = await appsRes.text();
    return {
      ok: false,
      error: "list datatransfer apps: HTTP " + appsRes.status + " " + txt.slice(0, 200) +
        " — if 403, the admin.datatransfer scope is missing from Domain-wide delegation in admin.google.com.",
    };
  }
  const appsBody = await appsRes.json();
  const driveApp = (appsBody.applications || []).find(a => (a.name || "").toLowerCase().includes("drive"));
  if (!driveApp) return { ok: false, error: "datatransfer applications list did not include Drive" };

  const insertRes = await fetch("https://admin.googleapis.com/admin/datatransfer/v1/transfers", {
    method: "POST",
    headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      oldOwnerUserId: src.data.id,
      newOwnerUserId: tgt.data.id,
      applicationDataTransfers: [
        // Omitting applicationTransferParams transfers the default set of
        // owned files. PRIVACY_LEVEL params (PRIVATE / SHARED) could restrict
        // scope; we transfer everything by default which matches what an
        // admin actually wants when off-boarding a leaver.
        { applicationId: driveApp.id, applicationTransferParams: [] },
      ],
    }),
  });
  if (!insertRes.ok) {
    const txt = await insertRes.text();
    return { ok: false, error: "insert transfer: HTTP " + insertRes.status + " " + txt.slice(0, 300) };
  }
  const tr = await insertRes.json();
  return {
    ok: true,
    data: {
      transfer_id: tr.id || "",
      status: tr.overallTransferStatusCode || "inProgress",
      source: body.email,
      target: body.target_email,
      message: "Drive transfer queued (id " + (tr.id || "?") + "). Continues in background — safe to delete the source user now.",
    },
  };
}

// One-shot deletion path that preserves the leaver's data:
//   1. Initiate the Drive transfer (Admin SDK Data Transfer API). Async —
//      Google completes it in the background even after the source user is
//      deleted.
//   2. Append an entry to pending-transfers.json so the Directory page
//      can render the "⏳ Transferring + Deleting" badge on the row AND so
//      the background scanner (scripts/process_pending_transfers.py) can
//      pick the entry up to do Gmail message migration, then call
//      delete-account, then clear the entry.
//   3. Return — does NOT delete the source user here. Deletion happens
//      after Gmail migration finishes, otherwise we lose access to the
//      mailbox we're trying to migrate from.
async function doQueueTransferAndDelete(env, token, body) {
  if (!body.email) return { ok: false, error: "missing email (source)" };
  if (!body.target_email) return { ok: false, error: "missing target_email" };

  // Reuse doDataTransfer for the Drive part — same code path, same error
  // surface. Failures here abort the queue (we don't want to claim the user
  // is being processed if step 1 didn't succeed).
  const dt = await doDataTransfer(token, body);
  if (!dt.ok) return dt;

  // Append a pending entry. The bg scanner is the authority for clearing it.
  // convert_to_group_forward_to (optional): when set, the scanner finishes
  // via convert-to-group (rename + delete + queue group creation) instead
  // of plain users.delete — so the leaver's email keeps forwarding to a
  // colleague after the 20-day reuse lockout expires.
  const entry = {
    source_email: body.email,
    target_email: body.target_email,
    drive_transfer_id: dt.data && dt.data.transfer_id || "",
    queued_at: new Date().toISOString(),
    stage: "queued",   // queued -> migrating-mail -> deleting -> done (then removed)
    tenant: (body.tenant || "").toLowerCase(),
    queued_by: body.actor || "",
    convert_to_group_forward_to: body.convert_to_group_forward_to || null,
  };
  try {
    await appendPendingTransfer(env, entry);
  } catch (e) {
    return {
      ok: false,
      error: "Drive transfer queued (id " + entry.drive_transfer_id + ") but pending-transfers.json append failed: " + (e.message || e) +
        ". Mail migration + delete will NOT run automatically. Inspect manually.",
    };
  }

  return {
    ok: true,
    data: {
      stage: "queued",
      drive_transfer_id: entry.drive_transfer_id,
      source: body.email,
      target: body.target_email,
      message: "Drive transfer queued. The hourly background job will migrate Gmail messages source → target then delete the source user.",
    },
  };
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

// Add an editable alias to a user. Errors (409 alias-in-use, 400 invalid
// address, etc.) bubble up via adminApi.
async function doUserAliasAdd(token, body) {
  if (!body.user_email) return { ok: false, error: "missing user_email" };
  if (!body.alias) return { ok: false, error: "missing alias" };
  return adminApi(
    token,
    "POST",
    `users/${encodeURIComponent(body.user_email)}/aliases`,
    { alias: body.alias },
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

  // Idempotent: alias-remove may already have run in a previous failed
  // attempt, leaving the address freed and possibly a Group already
  // created. Tolerate "not found" on the alias-remove and "already
  // exists" on the group-create + member-add so a retry completes
  // whatever's missing rather than wedging on a partial state.
  const rem = await adminApi(
    token,
    "DELETE",
    `users/${encodeURIComponent(body.user_email)}/aliases/${encodeURIComponent(body.alias)}`,
  );
  if (!rem.ok && rem.status !== 404 && !/not found|resource_id|invalid input/i.test(rem.error || "")) {
    return { ok: false, step: "alias-remove", error: rem.error || "alias removal failed", status: rem.status };
  }
  const aliasAlreadyGone = !rem.ok;

  // Brief propagation pause — Workspace usually frees the address in <2s.
  await new Promise(r => setTimeout(r, 1500));

  let groupAlreadyExisted = false;
  const gc = await adminApi(token, "POST", "groups", {
    email: body.alias,
    name: body.group_name,
    ...(body.description ? { description: body.description } : {}),
  });
  if (!gc.ok) {
    if (gc.status === 409 || /already exists|duplicate/i.test(gc.error || "")) {
      groupAlreadyExisted = true;
    } else {
      return { ok: false, step: "group-create", error: gc.error || "group creation failed", status: gc.status };
    }
  }

  const member = body.initial_member || body.user_email;
  const mb = await adminApi(token, "POST", `groups/${encodeURIComponent(body.alias)}/members`, {
    email: member,
    role: "MEMBER",
  });
  if (!mb.ok && mb.status !== 409 && !/duplicate|already exists/i.test(mb.error || "")) {
    return { ok: false, step: "member-add", error: mb.error || "member add failed", status: mb.status, group_created: !groupAlreadyExisted };
  }
  const memberAlreadyExisted = !mb.ok;

  return {
    ok: true,
    group_email: body.alias,
    group_name: body.group_name,
    member,
    note:
      (aliasAlreadyGone   ? "alias was already removed in a prior run · "  : "") +
      (groupAlreadyExisted ? "group already existed at this address · "    : "") +
      (memberAlreadyExisted ? `${member} was already a member`             : `${member} added as initial member`),
  };
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

// Cover banner photo — wider, shown at the top of /directory/<slug>. Same
// upload flow as the avatar but written to assets/covers/ and tracked via
// a separate Person field (cover_photo_uploaded_at) for cache-busting.
function coverFilename(email) {
  return (email || "").toString().trim().toLowerCase().replace(/@/g, "_at_") + ".jpg";
}

async function doCoverPhotoUpload(env, body, actor, isAdmin) {
  if (!env.GITHUB_TOKEN) return { ok: false, error: "GITHUB_TOKEN secret not configured" };
  const email = (body.user_email || "").toString().trim().toLowerCase();
  if (!email || !email.includes("@")) return { ok: false, error: "missing or invalid user_email" };
  if (!isAdmin && !(await actorOwnsEmail(env, actor, email))) {
    return { ok: false, error: "self or admin required for cover changes" };
  }
  const b64 = (body.photo_b64 || "").toString();
  if (!b64) return { ok: false, error: "missing photo_b64" };
  // Covers are larger than avatars (1500×500 ish). Cap at 3 MB base64.
  if (b64.length > 3 * 1024 * 1024) {
    return { ok: false, error: `cover too large (${Math.round(b64.length / 1024)} KB base64) — resize client-side` };
  }
  const path = `assets/covers/${coverFilename(email)}`;
  return await commitFile(env, path, b64, `Cover photo: upload ${email} (by ${actor})`);
}

async function doCoverPhotoRemove(env, body, actor, isAdmin) {
  if (!env.GITHUB_TOKEN) return { ok: false, error: "GITHUB_TOKEN secret not configured" };
  const email = (body.user_email || "").toString().trim().toLowerCase();
  if (!email || !email.includes("@")) return { ok: false, error: "missing or invalid user_email" };
  if (!isAdmin && !(await actorOwnsEmail(env, actor, email))) {
    return { ok: false, error: "self or admin required for cover changes" };
  }
  const path = `assets/covers/${coverFilename(email)}`;
  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "apifk-workspace-worker",
  };
  const getRes = await fetch(`https://api.github.com/repos/${REPO}/contents/${path}?ref=${BRANCH}`, { headers: ghHeaders });
  if (getRes.status === 404) return { ok: true, no_op: true, message: "no cover to remove" };
  if (!getRes.ok) return { ok: false, error: `failed to read cover: ${getRes.status}` };
  const sha = (await getRes.json()).sha;
  const delRes = await fetch(`https://api.github.com/repos/${REPO}/contents/${path}`, {
    method: "DELETE",
    headers: { ...ghHeaders, "Content-Type": "application/json" },
    body: JSON.stringify({ message: `Cover photo: remove ${email} (by ${actor})`, sha, branch: BRANCH }),
  });
  if (!delRes.ok) {
    const detail = (await delRes.text()).slice(0, 200);
    return { ok: false, error: `failed to delete (${delRes.status}): ${detail}` };
  }
  return { ok: true, path };
}

// Shared helper: PUT a file to the GitHub Contents API. Handles new files
// (no SHA) and updates (must include SHA of the prior version).
async function commitFile(env, path, b64Content, message) {
  try {
    const ghHeaders = {
      "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "apifk-workspace-worker",
    };
    // URL-encode each path segment so special characters in filenames
    // (e.g. "@" → "_at_" already, but periods + future paths) don't
    // make the URL parser barf with "string did not match expected
    // pattern". Slashes between segments are preserved.
    const safePath = (path || "").split("/").map(encodeURIComponent).join("/");
    // Strip any whitespace that snuck into the base64 (newlines from
    // some encoders, etc.) — GitHub Contents API rejects b64 with
    // non-alphabet characters.
    const cleanB64 = (b64Content || "").toString().replace(/\s+/g, "");

    let sha = null;
    const getRes = await fetch(
      `https://api.github.com/repos/${REPO}/contents/${safePath}?ref=${BRANCH}`,
      { headers: ghHeaders },
    );
    if (getRes.ok) sha = (await getRes.json()).sha;
    else if (getRes.status !== 404) {
      const det = (await getRes.text()).slice(0, 200);
      return { ok: false, error: `pre-commit GET failed (${getRes.status}): ${det}` };
    }
    const putRes = await fetch(
      `https://api.github.com/repos/${REPO}/contents/${safePath}`,
      {
        method: "PUT",
        headers: { ...ghHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          content: cleanB64,
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
  } catch (e) {
    return { ok: false, error: `commitFile threw: ${e.message || e}` };
  }
}

/* ----------- Cloudflare Access allowlist sync ----------- */

// Push the current admin list to the Cloudflare Access app's allow policy.
// Build the include list as: every RG Workspace domain (covers any
// current or future staff in those tenants) + every admin whose email
// doesn't already match one of those domains, listed explicitly.
// Non-fatal: if the token isn't set or the PUT fails, the admin change
// itself still succeeded — we just log + return a warning.
async function syncAccessAllowlist(env, admins) {
  if (!env.CLOUDFLARE_API_TOKEN) {
    return { ok: false, error: "CLOUDFLARE_API_TOKEN secret not configured — allowlist not synced" };
  }
  const domainSuffixes = ACCESS_DOMAIN_RULES.map(d => "@" + d);
  const extras = Array.from(new Set(
    (admins || [])
      .map(e => (e || "").toString().trim().toLowerCase())
      .filter(e => e && !domainSuffixes.some(s => e.endsWith(s))),
  )).sort();
  const include = [
    ...ACCESS_DOMAIN_RULES.map(d => ({ email_domain: { domain: d } })),
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
    try { const bin = atob(data.content.replace(/\s/g, "")); current = JSON.parse(new TextDecoder("utf-8").decode(Uint8Array.from(bin, c => c.charCodeAt(0)))); }
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

/* ----------- People table (people.json in repo) -----------
 *
 * The canonical Person record per human. Other systems (Wall, Holidays,
 * Directory, /directory/<slug> profile pages) should resolve identity
 * through this table rather than through raw Workspace accounts.
 *
 * doPeopleSet:  PATCH-or-create. body = { id, ...fields }. Missing
 *               fields are preserved; explicit nulls clear them.
 * doPeopleDelete: remove by id.
 */
const PEOPLE_PATH = "people.json";
const PEOPLE_ALLOWED_FIELDS = new Set([
  "name", "given", "family", "aliases",
  "url_slug",
  "main_google_email", "alt_google_emails", "external_google_email",
  "auth0_id",
  "access_level", "company", "title", "department", "team",
  "phone", "address", "start_date", "end_date", "date_of_birth",
  "line_manager_id", "line_manager_email_raw",
  "role", "notes",
  "directory_photo_uploaded_at", "cover_photo_uploaded_at",
  "suspended", "deletion_time",
  "on_payroll", "most_recent_payroll_id",
  "holiday_plan",
  "bookr_uids",
]);
// Fields a person can self-edit on their own profile page without admin
// rights. Tightened 2026-05-19 to match the UI: name + aliases + phone +
// address only. Photo timestamps stay because the photo-upload endpoint
// writes them on behalf of the self-uploader. Everything else (role,
// team, line manager, start date, access level, emails, auth0,
// date_of_birth, notes, etc.) is admin-only.
const PEOPLE_SELF_EDITABLE = new Set([
  "name", "aliases", "phone", "address",
  "directory_photo_uploaded_at", "cover_photo_uploaded_at",
]);
const PEOPLE_ACCESS_LEVELS = new Set(["admin", "staff", "outsider", "former"]);

// Return true if `actor` (a Cf-Access-Authenticated email) appears on the
// Person record at `targetEmail` as main / alt / external Google account.
// Used by the self-edit carve-outs.
async function actorOwnsEmail(env, actor, targetEmail) {
  if (!actor || !targetEmail) return false;
  const a = actor.toLowerCase(), t = targetEmail.toLowerCase();
  if (a === t) return true;
  try {
    const { file } = await fetchPeopleFile(env);
    const p = (file.people || []).find(p =>
      (p.main_google_email || "").toLowerCase() === t ||
      ((p.alt_google_emails || []).map(e => (e || "").toLowerCase())).includes(t) ||
      (p.external_google_email || "").toLowerCase() === t
    );
    if (!p) return false;
    const emails = [p.main_google_email, ...(p.alt_google_emails || []), p.external_google_email]
      .filter(Boolean).map(e => e.toLowerCase());
    return emails.includes(a);
  } catch (e) { return false; }
}

async function fetchPeopleFile(env) {
  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "apifk-workspace-worker",
  };
  const res = await fetch(
    `https://api.github.com/repos/${REPO}/contents/${PEOPLE_PATH}?ref=${BRANCH}`,
    { headers: ghHeaders },
  );
  if (res.status === 404) {
    return { sha: null, file: { schema_version: 1, updated_at: null, people: [] } };
  }
  if (!res.ok) throw new Error(`people.json GET failed: ${res.status}`);
  const data = await res.json();
  let file;
  try {
    const bin = atob((data.content || "").replace(/\s/g, ""));
    file = JSON.parse(new TextDecoder("utf-8").decode(Uint8Array.from(bin, c => c.charCodeAt(0))));
  } catch (e) {
    throw new Error("people.json could not be parsed: " + e.message);
  }
  if (!Array.isArray(file.people)) file.people = [];
  return { sha: data.sha, file };
}

// Pre-commit schema validator for people.json. Catches duplicate ids /
// slugs, empty names, self-line-manager loops. Throws on any failure
// so the commit is refused — bad data can never land. Cheap (linear
// pass over the ~200-row table) so we run it on every people-set.
function validatePeopleFile(file) {
  const errs = [];
  const seenIds = new Set(), seenSlugs = new Set();
  for (const p of file.people || []) {
    if (!Number.isInteger(p.id) || p.id <= 0) errs.push(`bad id on Person ${p.name || "?"}: ${p.id!== undefined ? p.id : "(missing)"}`);
    else if (seenIds.has(p.id)) errs.push(`duplicate Person id ${p.id}`);
    else seenIds.add(p.id);
    const slug = (p.url_slug || "").toLowerCase();
    if (slug) {
      if (seenSlugs.has(slug)) errs.push(`duplicate url_slug ${slug}`);
      seenSlugs.add(slug);
    }
    if (!(p.name || "").trim()) errs.push(`Person #${p.id} has empty name`);
    // Skip self-line-manager check when id is missing — otherwise we
    // emit a misleading "Person #undefined is its own line_manager"
    // alongside the primary "bad id" error.
    if (p.id != null && p.line_manager_id === p.id) errs.push(`Person #${p.id} is its own line_manager`);
  }
  // FK integrity: line_manager_id must resolve.
  for (const p of file.people || []) {
    const lm = p.line_manager_id;
    if (lm == null || lm === "") continue;
    if (!seenIds.has(Number(lm))) errs.push(`Person #${p.id} line_manager_id=${lm} → no Person`);
  }
  if (errs.length) throw new Error("people.json validation failed: " + errs.slice(0, 5).join("; "));
}

async function commitPeopleFile(env, file, sha, message) {
  validatePeopleFile(file);
  file.schema_version = 1;
  file.updated_at = new Date().toISOString();
  file.people.sort((a, b) => ((a.name || "").toLowerCase().localeCompare((b.name || "").toLowerCase())) || (a.id || "").localeCompare(b.id || ""));
  const body = JSON.stringify(file, null, 2) + "\n";
  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "apifk-workspace-worker",
    "Content-Type": "application/json",
  };
  const res = await fetch(`https://api.github.com/repos/${REPO}/contents/${PEOPLE_PATH}`, {
    method: "PUT",
    headers: ghHeaders,
    body: JSON.stringify({ message, content: b64Encode(body), branch: BRANCH, sha: sha || undefined }),
  });
  if (!res.ok) {
    const detail = (await res.text()).slice(0, 200);
    throw new Error(`people.json commit failed (${res.status}): ${detail}`);
  }
}

function normalisePeoplePatch(patch) {
  const out = {};
  for (const k of Object.keys(patch || {})) {
    if (!PEOPLE_ALLOWED_FIELDS.has(k)) continue;
    let v = patch[k];
    if (Array.isArray(v)) {
      v = Array.from(new Set(
        v.map(x => (x || "").toString().trim()).filter(Boolean)
         .map(x => k.endsWith("_emails") || k.endsWith("_email") || k === "aliases" ? x.toLowerCase() : x)
      ));
    } else if (typeof v === "string") {
      v = v.trim();
      if (k.endsWith("_email") || k.endsWith("_emails") || k === "id") v = v.toLowerCase();
    }
    out[k] = v;
  }
  // "agent" was retired 2026-05-18 (it duplicated "staff" / Standard
  // user). Coerce silently rather than 400 so any stale cached payload
  // submitted from an old tab still lands on the right value.
  if (out.access_level === "agent") out.access_level = "staff";
  if (out.access_level && !PEOPLE_ACCESS_LEVELS.has(out.access_level)) {
    throw new Error(`invalid access_level: ${out.access_level}`);
  }
  return out;
}

// Integer-id primary keys. id is server-assigned on create; url_slug is
// derived from email local-part with -2 suffix on collision and used as
// the human-readable URL form (/directory/<slug>). Updates pass id;
// creates omit id (or pass id=null).
function nextPersonId(file) {
  let max = 0;
  for (const p of file.people || []) {
    const n = Number(p.id);
    if (Number.isFinite(n) && n > max) max = n;
  }
  return max + 1;
}
function pickUrlSlug(file, baseEmail, fallbackName) {
  const local = (baseEmail || "").toString().split("@")[0].toLowerCase().trim();
  const fallback = (fallbackName || "").toString().toLowerCase().replace(/[^a-z0-9]+/g, ".").replace(/^\.+|\.+$/g, "");
  const base = local || fallback || "person";
  const taken = new Set((file.people || []).map(p => (p.url_slug || "").toLowerCase()));
  if (!taken.has(base)) return base;
  let n = 2;
  while (taken.has(`${base}-${n}`)) n++;
  return `${base}-${n}`;
}

async function doPeopleSet(env, body, actor, isAdmin) {
  if (!env.GITHUB_TOKEN) return { ok: false, error: "GITHUB_TOKEN not configured" };
  const rawId = (body || {}).id;
  const idStr = rawId == null ? "" : String(rawId).trim();

  const patch = normalisePeoplePatch(body);
  delete patch.id; // server-managed
  // url_slug coming from body is allowed but normalise it.
  if (typeof patch.url_slug === "string") patch.url_slug = patch.url_slug.trim().toLowerCase();

  const { sha, file } = await fetchPeopleFile(env);
  const now = new Date().toISOString();
  let person = idStr
    ? file.people.find(p => String(p.id) === idStr)
    : null;
  let created = false;
  if (!person) {
    if (!isAdmin) return { ok: false, error: "admin required to create a new Person" };
    if (idStr && Number.isFinite(Number(idStr))) {
      // Caller passed an id that doesn't match — explicit error rather
      // than silently creating with a different id.
      return { ok: false, error: `no Person with id ${idStr}` };
    }
    const newId = nextPersonId(file);
    const newSlug = patch.url_slug || pickUrlSlug(file, patch.main_google_email, patch.name);
    person = {
      id: newId,
      url_slug: newSlug,
      name: "", given: "", family: "", aliases: [],
      main_google_email: "", alt_google_emails: [], external_google_email: "",
      auth0_id: "", access_level: "staff", company: "",
      title: "", department: "",
      phone: "", address: "", start_date: "",
      line_manager_id: null, line_manager_email_raw: "",
      role: "", notes: "",
      directory_photo_uploaded_at: "", cover_photo_uploaded_at: "",
      suspended: false,
      on_payroll: false, most_recent_payroll_id: null,
      created_at: now, updated_at: now,
    };
    file.people.push(person);
    created = true;
    // url_slug was just set; don't let the patch overwrite it unless
    // explicitly different.
    if (!patch.url_slug) delete patch.url_slug;
  } else if (!isAdmin) {
    // Self-edit path: actor must own one of this Person's Google emails,
    // and the patch must touch only self-editable fields.
    const owned = [person.main_google_email, ...(person.alt_google_emails || []), person.external_google_email]
      .filter(Boolean).map(e => e.toLowerCase()).includes((actor || "").toLowerCase());
    if (!owned) return { ok: false, error: "self or admin required" };
    for (const k of Object.keys(patch)) {
      if (!PEOPLE_SELF_EDITABLE.has(k)) return { ok: false, error: `field "${k}" is admin-only` };
    }
  }
  // main_google_email is no longer strictly required — the bulk payroll
  // import can create a Person from a row that has no Google account
  // yet (admin fills it in later). We leave it empty by default.

  // Name is required for any new Person. Empty names break every
  // downstream list (directory, mentions, line-manager pickers).
  if (created && !((patch.name || person.name) || "").trim()) {
    file.people.pop();
    return { ok: false, error: "name is required for new people" };
  }

  // One Google account per tenant rule — but only when the patch is
  // actually changing the email fields. A people-set that only touches
  // (say) cover_photo_uploaded_at shouldn't be blocked because the
  // existing record happens to carry @letme.com + @letme.co.uk for
  // dual-sign-in. The rule is a guard against accidental DUPLICATION
  // via UI, not against existing legitimate multi-tenancy.
  const touchingEmails = patch.main_google_email !== undefined || patch.alt_google_emails !== undefined;
  if (touchingEmails) {
    const futureEmails = [
      patch.main_google_email !== undefined ? patch.main_google_email : person.main_google_email,
      ...(patch.alt_google_emails !== undefined ? patch.alt_google_emails : (person.alt_google_emails || [])),
    ].filter(Boolean).map(e => e.toLowerCase());
    const letme    = futureEmails.filter(e => e.endsWith("@letme.co.uk") || e.endsWith("@letme.com"));
    const together = futureEmails.filter(e => e.endsWith("@togetherloans.com"));
    if (letme.length > 1)    return { ok: false, error: `only one Letme Google account allowed per Person (would have ${letme.length}: ${letme.join(", ")})` };
    if (together.length > 1) return { ok: false, error: `only one Together Google account allowed per Person (would have ${together.length}: ${together.join(", ")})` };
  }
  const accessChanged =
    patch.access_level !== undefined ||
    patch.suspended    !== undefined ||
    patch.main_google_email !== undefined ||
    patch.alt_google_emails !== undefined;

  // If on_payroll is being flipped to true and this Person has no
  // PayrollData record yet, create a blank one and link it BEFORE
  // committing people.json so the link goes out in the same write.
  const turningOnPayroll = (patch.on_payroll === true) && !person.most_recent_payroll_id;

  Object.assign(person, patch, { updated_at: now });

  let createdPayrollRecord = null;
  let payrollBootstrapError = null;
  if (turningOnPayroll) {
    try {
      const { sha: paySha, file: payFile } = await fetchPayrollFile(env);
      const rec = blankPayrollRecord(person, payFile, actor, "auto-on-flag");
      payFile.records.push(rec);
      await commitPayrollFile(env, payFile, paySha, `Payroll: auto-create blank #${rec.id} for ${person.name} (#${person.id}) (by ${actor})`);
      person.most_recent_payroll_id = rec.id;
      createdPayrollRecord = rec;
    } catch (e) {
      // Don't fail people-set if the payroll bootstrap couldn't write;
      // the user can re-trigger by editing payroll on the Person page.
      payrollBootstrapError = e.message;
    }
  }

  // Auto-sync hook: new Person -> match-or-create a BookR user and
  // stash the uid(s) on the row so the commit ships with bookr_uids set.
  // Non-fatal: failure is logged but doesn't abort the Person create.
  let bookrAutoSync = null;
  if (created && env.BOOKR_SERVICE_ACCOUNT_JSON) {
    try {
      const r = await bookrMatchOrCreateForPerson(env, person);
      person.bookr_uids = Array.isArray(r.bookr_uids) ? r.bookr_uids.slice() : [];
      bookrAutoSync = { ok: true, ...r };
      console.log("bookr auto-sync ok", JSON.stringify({ person_id: person.id, bookr_uids: person.bookr_uids, added: r.added || [], created: !!r.created }));
    } catch (e) {
      bookrAutoSync = { ok: false, error: e.message };
      console.log("bookr auto-sync FAILED", JSON.stringify({ person_id: person.id, error: e.message }));
    }
  }
  // One-time normalisation: strip legacy singular bookr_uid on every
  // Person write so the migration completes organically with normal
  // traffic. Once the array is canonical, no record carries both.
  if ("bookr_uid" in person) delete person.bookr_uid;

  const msg = created
    ? `People: create #${person.id} ${person.name || person.url_slug} (by ${actor})`
    : `People: update #${person.id} ${person.name || person.url_slug} (by ${actor})`;
  await commitPeopleFile(env, file, sha, msg);

  // If anything access-relevant changed, refresh admins.json + push the
  // new allow-list to Cloudflare Access. Non-fatal: people-set itself
  // already succeeded, so a sync failure is reported back as a warning.
  const result = { ok: true, person, created };
  if (createdPayrollRecord) result.payroll_record = createdPayrollRecord;
  if (payrollBootstrapError) result.payroll_bootstrap_error = payrollBootstrapError;
  if (accessChanged) {
    try {
      const adminSync  = await syncAdminsFromPeople(env, file, actor);
      result.admin_sync = adminSync;
      if (adminSync.ok && adminSync.admins) {
        const accessSync = await syncAccessAllowlist(env, adminSync.admins);
        result.access_sync = accessSync;
      }
    } catch (e) {
      result.access_sync = { ok: false, error: e.message };
    }
  }
  return result;
}

// Derive the admin list from people.json (access_level === "admin" AND
// not suspended/former). Owner is always included as a failsafe so a bad
// people.json edit can't lock them out.
function peopleToAdminList(file) {
  const owner = OWNER_EMAIL.toLowerCase();
  const out = new Set([owner]);
  for (const p of (file.people || [])) {
    if (p.suspended) continue;
    if (p.access_level !== "admin") continue;
    if (p.main_google_email) out.add(p.main_google_email.toLowerCase());
    for (const e of (p.alt_google_emails || [])) if (e) out.add(e.toLowerCase());
    if (p.external_google_email) out.add(p.external_google_email.toLowerCase());
  }
  return Array.from(out).sort();
}

// Sync admins.json from people.json (people.json is canonical). Writes a
// fresh admins.json containing every admin email + the owner.
async function syncAdminsFromPeople(env, file, actor) {
  if (!env.GITHUB_TOKEN) return { ok: false, error: "GITHUB_TOKEN not configured" };
  const admins = peopleToAdminList(file);
  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "apifk-workspace-worker",
  };
  const getRes = await fetch(`https://api.github.com/repos/${REPO}/contents/${ADMINS_PATH}?ref=${BRANCH}`, { headers: ghHeaders });
  let sha = null;
  if (getRes.ok) sha = (await getRes.json()).sha;
  else if (getRes.status !== 404) return { ok: false, error: `admins.json GET failed: ${getRes.status}` };

  const body = JSON.stringify({
    schema_version: 1,
    updated_at: new Date().toISOString(),
    source: "synced from people.json",
    admins,
  }, null, 2) + "\n";
  const putRes = await fetch(`https://api.github.com/repos/${REPO}/contents/${ADMINS_PATH}`, {
    method: "PUT",
    headers: { ...ghHeaders, "Content-Type": "application/json" },
    body: JSON.stringify({
      message: `Admins: synced from people.json (by ${actor})`,
      content: b64Encode(body),
      branch: BRANCH,
      sha: sha || undefined,
    }),
  });
  if (!putRes.ok) {
    const detail = (await putRes.text()).slice(0, 200);
    return { ok: false, error: `admins.json PUT failed (${putRes.status}): ${detail}` };
  }
  return { ok: true, admins };
}

/* ----------- PayrollData table (payroll-data.json) -----------
 *
 * One canonical PayrollData record per Person (the most recent snapshot).
 * Historical records are kept in the same file — Person.most_recent_payroll_id
 * points to the active one. Manual admin edits via the Person page mutate
 * that record in place; bulk imports (payroll-import, not built yet) will
 * append a new row and re-point the link.
 */
const PAYROLL_DATA_PATH = "payroll-data.json";
const PAYROLL_ALLOWED_FIELDS = new Set([
  "employer", "employee_number",
  "first_name", "last_name", "email",
  "start_date", "termination_date",
  "mobile", "address",
  "annual_salary", "monthly_pay",
  "tax_code", "ni_number",
  "bank_sort_code", "bank_account_last4",
  "notes",
]);

async function fetchPayrollFile(env) {
  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "apifk-workspace-worker",
  };
  const res = await fetch(`https://api.github.com/repos/${REPO}/contents/${PAYROLL_DATA_PATH}?ref=${BRANCH}`, { headers: ghHeaders });
  if (res.status === 404) return { sha: null, file: { schema_version: 1, updated_at: null, records: [] } };
  if (!res.ok) throw new Error(`payroll-data.json GET failed: ${res.status}`);
  const data = await res.json();
  let file;
  try {
    const bin = atob((data.content || "").replace(/\s/g, ""));
    file = JSON.parse(new TextDecoder("utf-8").decode(Uint8Array.from(bin, c => c.charCodeAt(0))));
  } catch (e) { throw new Error("payroll-data.json could not be parsed: " + e.message); }
  if (!Array.isArray(file.records)) file.records = [];
  return { sha: data.sha, file };
}

function validatePayrollFile(file) {
  const errs = [];
  const seen = new Set();
  for (const r of file.records || []) {
    if (!Number.isInteger(r.id) || r.id <= 0) errs.push(`PayrollData has bad id: ${r.id}`);
    else if (seen.has(r.id)) errs.push(`duplicate PayrollData id ${r.id}`);
    else seen.add(r.id);
    if (r.person_id !== null && r.person_id !== undefined && !Number.isInteger(r.person_id)) {
      errs.push(`PayrollData #${r.id} bad person_id type: ${r.person_id}`);
    }
  }
  if (errs.length) throw new Error("payroll-data.json validation failed: " + errs.slice(0, 5).join("; "));
}

async function commitPayrollFile(env, file, sha, message) {
  validatePayrollFile(file);
  file.schema_version = 1;
  file.updated_at = new Date().toISOString();
  const body = JSON.stringify(file, null, 2) + "\n";
  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "apifk-workspace-worker",
    "Content-Type": "application/json",
  };
  const res = await fetch(`https://api.github.com/repos/${REPO}/contents/${PAYROLL_DATA_PATH}`, {
    method: "PUT",
    headers: ghHeaders,
    body: JSON.stringify({ message, content: b64Encode(body), branch: BRANCH, sha: sha || undefined }),
  });
  if (!res.ok) {
    const detail = (await res.text()).slice(0, 200);
    throw new Error(`payroll-data.json commit failed (${res.status}): ${detail}`);
  }
}

function nextPayrollId(file) {
  let max = 0;
  for (const r of file.records || []) {
    const n = Number(r.id);
    if (Number.isFinite(n) && n > max) max = n;
  }
  return max + 1;
}

function normalisePayrollPatch(patch) {
  const out = {};
  for (const k of Object.keys(patch || {})) {
    if (!PAYROLL_ALLOWED_FIELDS.has(k)) continue;
    let v = patch[k];
    if (typeof v === "string") v = v.trim();
    if (k === "email" && typeof v === "string") v = v.toLowerCase();
    out[k] = v;
  }
  return out;
}

// Build a blank payroll record for a Person who's been flagged on_payroll
// but has no record yet. Caller appends and links most_recent_payroll_id.
function blankPayrollRecord(person, file, actor, source) {
  return {
    id: nextPayrollId(file),
    person_id: person.id,
    employer: "",
    employee_number: "",
    first_name: person.given || "",
    last_name: person.family || "",
    email: person.main_google_email || "",
    start_date: person.start_date || "",
    termination_date: "",
    mobile: person.phone || "",
    address: person.address || "",
    annual_salary: null,
    monthly_pay: null,
    tax_code: "",
    ni_number: "",
    bank_sort_code: "",
    bank_account_last4: "",
    notes: "",
    imported_at: new Date().toISOString(),
    imported_by: actor || "(system)",
    source: source || "auto-blank",
  };
}

// payroll-set: admin-only edit of a Person's most-recent PayrollData record.
// If the Person is on_payroll=true and has no record yet, creates a blank
// one and applies the patch in a single commit.
async function doPayrollSet(env, body, actor) {
  if (!env.GITHUB_TOKEN) return { ok: false, error: "GITHUB_TOKEN not configured" };
  const personIdStr = ((body || {}).person_id == null ? "" : String(body.person_id)).trim();
  if (!personIdStr) return { ok: false, error: "missing person_id" };

  const patch = normalisePayrollPatch(body);

  const { sha: pSha, file: pFile } = await fetchPeopleFile(env);
  const person = (pFile.people || []).find(p => String(p.id) === personIdStr);
  if (!person) return { ok: false, error: `person ${personIdStr} not found` };
  if (!person.on_payroll) {
    return { ok: false, error: `${person.name || person.id} is not flagged on_payroll — set that to Yes first` };
  }

  const { sha, file } = await fetchPayrollFile(env);
  let rec = (person.most_recent_payroll_id != null)
    ? file.records.find(r => String(r.id) === String(person.most_recent_payroll_id))
    : null;
  let created = false;
  if (!rec) {
    rec = blankPayrollRecord(person, file, actor, "manual");
    file.records.push(rec);
    created = true;
  }
  Object.assign(rec, patch);
  if (created || person.most_recent_payroll_id !== rec.id) {
    person.most_recent_payroll_id = rec.id;
    person.updated_at = new Date().toISOString();
    await commitPeopleFile(env, pFile, pSha, `People: link payroll for ${person.id} (${person.name}) (by ${actor})`);
  }
  await commitPayrollFile(env, file, sha,
    created ? `Payroll: create blank #${rec.id} for ${person.name} (#${person.id}) + edits (by ${actor})`
            : `Payroll: edit #${rec.id} (${person.name}) (by ${actor})`);
  return { ok: true, record: rec, person_id: person.id, created };
}

/* ----------- Google accounts table (google-accounts.json) -----------
 *
 * One row per Google account on the system, FK person_id -> people.id.
 * Tenant is "letme" | "together" | "external". "one of each per person"
 * is enforced here too. Writes also keep the denormalised email fields
 * on people.json in sync (main_google_email / alt_google_emails /
 * external_google_email) for backwards compat with consumers that
 * haven't migrated yet.
 */
const GOOGLE_ACCOUNTS_PATH = "google-accounts.json";
const GOOGLE_ALLOWED_TENANTS = new Set(["letme", "together", "external"]);

async function fetchGoogleAccountsFile(env) {
  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "apifk-workspace-worker",
  };
  const res = await fetch(`https://api.github.com/repos/${REPO}/contents/${GOOGLE_ACCOUNTS_PATH}?ref=${BRANCH}`, { headers: ghHeaders });
  if (res.status === 404) return { sha: null, file: { schema_version: 1, updated_at: null, records: [] } };
  if (!res.ok) throw new Error(`google-accounts.json GET failed: ${res.status}`);
  const data = await res.json();
  let file;
  try {
    const bin = atob((data.content || "").replace(/\s/g, ""));
    file = JSON.parse(new TextDecoder("utf-8").decode(Uint8Array.from(bin, c => c.charCodeAt(0))));
  } catch (e) { throw new Error("google-accounts.json could not be parsed: " + e.message); }
  if (!Array.isArray(file.records)) file.records = [];
  return { sha: data.sha, file };
}

function validateGoogleAccountsFile(file) {
  const errs = [];
  const seen = new Set(); const seenEmails = new Set();
  for (const r of file.records || []) {
    if (!Number.isInteger(r.id) || r.id <= 0) errs.push(`GoogleAccount has bad id: ${r.id}`);
    else if (seen.has(r.id)) errs.push(`duplicate GoogleAccount id ${r.id}`);
    else seen.add(r.id);
    // Strict check — null/missing tenant is just as bad as a typo.
    // Earlier `r.tenant && ...` short-circuited on falsy tenant.
    if (!["letme", "together", "external"].includes(r.tenant)) {
      errs.push(`GoogleAccount #${r.id} bad/missing tenant: ${r.tenant === undefined ? "(missing)" : JSON.stringify(r.tenant)}`);
    }
    const email = (r.email || "").toLowerCase();
    if (email && seenEmails.has(email)) errs.push(`duplicate GoogleAccount email ${email}`);
    if (email) seenEmails.add(email);
    if (r.person_id !== null && r.person_id !== undefined && !Number.isInteger(r.person_id)) {
      errs.push(`GoogleAccount #${r.id} bad person_id type: ${r.person_id}`);
    }
  }
  if (errs.length) throw new Error("google-accounts.json validation failed: " + errs.slice(0, 5).join("; "));
}

async function commitGoogleAccountsFile(env, file, sha, message) {
  validateGoogleAccountsFile(file);
  file.schema_version = 1;
  file.updated_at = new Date().toISOString();
  const body = JSON.stringify(file, null, 2) + "\n";
  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "apifk-workspace-worker",
    "Content-Type": "application/json",
  };
  const res = await fetch(`https://api.github.com/repos/${REPO}/contents/${GOOGLE_ACCOUNTS_PATH}`, {
    method: "PUT",
    headers: ghHeaders,
    body: JSON.stringify({ message, content: b64Encode(body), branch: BRANCH, sha: sha || undefined }),
  });
  if (!res.ok) {
    const detail = (await res.text()).slice(0, 200);
    throw new Error(`google-accounts.json commit failed (${res.status}): ${detail}`);
  }
}

const WAREHOUSE_ACTIVITY_PATH = "warehouse-activity.json";
async function fetchWarehouseActivityFile(env) {
  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "apifk-workspace-worker",
  };
  const res = await fetch(`https://api.github.com/repos/${REPO}/contents/${WAREHOUSE_ACTIVITY_PATH}?ref=${BRANCH}`, { headers: ghHeaders });
  if (res.status === 404) return { sha: null, file: { schema_version: 1, updated_at: null, records: [] } };
  if (!res.ok) throw new Error(`warehouse-activity.json GET failed: ${res.status}`);
  const data = await res.json();
  let file;
  try {
    const bin = atob((data.content || "").replace(/\s/g, ""));
    file = JSON.parse(new TextDecoder("utf-8").decode(Uint8Array.from(bin, c => c.charCodeAt(0))));
  } catch (e) { throw new Error("warehouse-activity.json could not be parsed: " + e.message); }
  if (!Array.isArray(file.records)) file.records = [];
  return { sha: data.sha, file };
}
function validateWarehouseActivityFile(file) {
  const errs = [];
  const seen = new Set();
  for (const r of file.records || []) {
    if (!Number.isInteger(r.id) || r.id <= 0) errs.push(`WarehouseActivity has bad id: ${r.id}`);
    else if (seen.has(r.id)) errs.push(`duplicate WarehouseActivity id ${r.id}`);
    else seen.add(r.id);
    if (r.person_id !== null && r.person_id !== undefined && !Number.isInteger(r.person_id)) {
      errs.push(`WarehouseActivity #${r.id} bad person_id type: ${r.person_id}`);
    }
  }
  if (errs.length) throw new Error("warehouse-activity.json validation failed: " + errs.slice(0, 5).join("; "));
}

async function commitWarehouseActivityFile(env, file, sha, message) {
  validateWarehouseActivityFile(file);
  file.schema_version = 1;
  file.updated_at = new Date().toISOString();
  const body = JSON.stringify(file, null, 2) + "\n";
  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "apifk-workspace-worker",
    "Content-Type": "application/json",
  };
  const res = await fetch(`https://api.github.com/repos/${REPO}/contents/${WAREHOUSE_ACTIVITY_PATH}`, {
    method: "PUT",
    headers: ghHeaders,
    body: JSON.stringify({ message, content: b64Encode(body), branch: BRANCH, sha: sha || undefined }),
  });
  if (!res.ok) {
    const detail = (await res.text()).slice(0, 200);
    throw new Error(`warehouse-activity.json commit failed (${res.status}): ${detail}`);
  }
}

function nextGoogleAccountId(file) {
  let max = 0;
  for (const r of file.records || []) {
    const n = Number(r.id);
    if (Number.isFinite(n) && n > max) max = n;
  }
  return max + 1;
}

function tenantOfEmail(email) {
  const d = ((email || "").split("@", 2)[1] || "").toLowerCase();
  if (d === "letme.co.uk" || d === "letme.com") return "letme";
  if (d === "togetherloans.com")                return "together";
  return "external";
}

// Sync the denormalised email fields on the Person from the current set
// of google-account rows. is_primary winner becomes main_google_email;
// other letme/together rows become alt_google_emails; external row
// becomes external_google_email. Called after every google-account
// mutation.
function denormaliseEmailsToPerson(person, allAccounts) {
  const mine = allAccounts.filter(a => String(a.person_id) === String(person.id));
  const primary = mine.find(a => a.is_primary) || mine.find(a => a.tenant === "letme") || mine.find(a => a.tenant === "together") || mine[0];
  person.main_google_email = primary ? primary.email : "";
  person.alt_google_emails = mine
    .filter(a => a !== primary && a.tenant !== "external")
    .map(a => a.email);
  person.external_google_email = (mine.find(a => a.tenant === "external") || {}).email || "";
}

async function doGoogleAccountSet(env, body, actor, isAdmin) {
  if (!isAdmin) return { ok: false, error: "admin required" };
  if (!env.GITHUB_TOKEN) return { ok: false, error: "GITHUB_TOKEN not configured" };
  const personIdStr = body.person_id == null ? "" : String(body.person_id).trim();
  const email = (body.email || "").toString().trim().toLowerCase();
  const recordIdStr = body.id == null ? "" : String(body.id).trim();
  if (!email || !email.includes("@")) return { ok: false, error: "valid email required" };
  if (!personIdStr) return { ok: false, error: "person_id required" };

  const tenant = body.tenant ? String(body.tenant).toLowerCase().trim() : tenantOfEmail(email);
  if (!GOOGLE_ALLOWED_TENANTS.has(tenant)) return { ok: false, error: `tenant must be letme/together/external` };

  const { sha: pSha, file: pFile } = await fetchPeopleFile(env);
  const person = pFile.people.find(p => String(p.id) === personIdStr);
  if (!person) return { ok: false, error: `person ${personIdStr} not found` };

  const { sha, file } = await fetchGoogleAccountsFile(env);
  const now = new Date().toISOString();

  // Editing an existing row?
  let rec = recordIdStr ? file.records.find(r => String(r.id) === recordIdStr) : null;
  // ...or upserting by (person_id + email)?
  if (!rec) rec = file.records.find(r => String(r.person_id) === personIdStr && (r.email || "").toLowerCase() === email);

  // One-per-tenant enforcement (external excluded — see is_primary rules).
  const existingSameTenant = file.records.find(r =>
    String(r.person_id) === personIdStr &&
    r.tenant === tenant &&
    (rec ? r.id !== rec.id : true)
  );
  if (existingSameTenant) {
    return { ok: false, error: `${person.name} already has a ${tenant} Google account (${existingSameTenant.email}). Delete or unlink that one first.` };
  }

  if (!rec) {
    rec = {
      id: nextGoogleAccountId(file),
      person_id: person.id,
      email, tenant,
      is_primary: !!body.is_primary,
      google_user_id: body.google_user_id || "",
      name: body.name || "",
      photo_url: body.photo_url || "",
      suspended: false,
      deletion_time: "",
      last_login: "",
      aliases: Array.isArray(body.aliases) ? body.aliases : [],
      synced_at: tenant === "external" ? "" : now,
    };
    file.records.push(rec);
  } else {
    rec.email = email;
    rec.tenant = tenant;
    if (body.is_primary !== undefined) rec.is_primary = !!body.is_primary;
    if (body.name      !== undefined) rec.name = body.name;
    if (body.aliases   !== undefined) rec.aliases = body.aliases;
    rec.synced_at = tenant === "external" ? rec.synced_at : now;
  }

  // If this row was marked primary, clear the flag on every other row.
  if (rec.is_primary) {
    for (const r of file.records) {
      if (r.id !== rec.id && String(r.person_id) === personIdStr) r.is_primary = false;
    }
  }

  await commitGoogleAccountsFile(env, file, sha,
    `Google accounts: set ${email} (${tenant}) on ${person.name} (by ${actor})`);

  // Mirror back onto people.json's denormalised email fields.
  denormaliseEmailsToPerson(person, file.records);
  person.updated_at = now;
  await commitPeopleFile(env, pFile, pSha,
    `People: sync email fields for #${person.id} after google-account change (by ${actor})`);

  return { ok: true, record: rec, person: person };
}

async function doGoogleAccountDelete(env, body, actor, isAdmin) {
  if (!isAdmin) return { ok: false, error: "admin required" };
  if (!env.GITHUB_TOKEN) return { ok: false, error: "GITHUB_TOKEN not configured" };
  const idStr = body.id == null ? "" : String(body.id).trim();
  if (!idStr) return { ok: false, error: "id required" };

  const { sha, file } = await fetchGoogleAccountsFile(env);
  const rec = file.records.find(r => String(r.id) === idStr);
  if (!rec) return { ok: true, no_op: true, message: `no google-account ${idStr}` };
  const personId = rec.person_id;
  file.records = file.records.filter(r => String(r.id) !== idStr);
  await commitGoogleAccountsFile(env, file, sha,
    `Google accounts: delete #${idStr} (${rec.email}) (by ${actor})`);

  // Re-sync the denormalised fields on the affected Person.
  if (personId != null) {
    const { sha: pSha, file: pFile } = await fetchPeopleFile(env);
    const person = pFile.people.find(p => String(p.id) === String(personId));
    if (person) {
      denormaliseEmailsToPerson(person, file.records);
      person.updated_at = new Date().toISOString();
      await commitPeopleFile(env, pFile, pSha,
        `People: sync email fields for #${person.id} after google-account delete (by ${actor})`);
    }
  }
  return { ok: true, deleted: idStr, person_id: personId };
}

// people-merge: collapses two Person records into one. Common case: the
// payroll import auto-created a Person for someone who already had a
// (differently-named) Google-account Person record. Winner keeps its id +
// URL; loser is absorbed and deleted.
async function doPeopleMerge(env, body, actor) {
  if (!env.GITHUB_TOKEN) return { ok: false, error: "GITHUB_TOKEN not configured" };
  const winnerId = ((body || {}).winner_id == null ? "" : String(body.winner_id)).trim();
  const loserId  = ((body || {}).loser_id  == null ? "" : String(body.loser_id )).trim();
  if (!winnerId || !loserId) return { ok: false, error: "winner_id and loser_id are both required" };
  if (winnerId === loserId)   return { ok: false, error: "winner and loser must be different" };

  const { sha: pSha, file: pFile } = await fetchPeopleFile(env);
  const winner = pFile.people.find(p => String(p.id) === winnerId);
  const loser  = pFile.people.find(p => String(p.id) === loserId);
  if (!winner) return { ok: false, error: `winner ${winnerId} not found` };
  if (!loser)  return { ok: false, error: `loser  ${loserId}  not found` };

  // Field merge rules:
  //   - strings: keep winner's if non-empty, else take loser's
  //   - arrays:  union, dedup, drop empties
  //   - bools:   logical OR (so on_payroll/suspended/etc. propagate)
  //   - special: most_recent_payroll_id — winner wins unless winner has none
  const scalarFields = ["name","given","family","main_google_email","external_google_email",
                        "auth0_id","access_level","company","title","department",
                        "phone","address","start_date",
                        "line_manager_id","line_manager_email_raw","role","notes",
                        "directory_photo_uploaded_at","cover_photo_uploaded_at",
                        "deletion_time"];
  for (const k of scalarFields) {
    if (!winner[k] && loser[k]) winner[k] = loser[k];
  }
  // Arrays: aliases + alt_google_emails.
  const uniq = (...lists) => Array.from(new Set(lists.flat().map(x => (x || "").toString().trim()).filter(Boolean)));
  winner.aliases           = uniq(winner.aliases, loser.aliases, loser.name && loser.name !== winner.name ? [loser.name] : []);
  winner.alt_google_emails = uniq(winner.alt_google_emails, loser.alt_google_emails, loser.main_google_email ? [loser.main_google_email] : [])
                              .filter(e => e !== winner.main_google_email);
  // Bools: OR.
  if (loser.on_payroll) winner.on_payroll = true;
  if (loser.suspended  && !winner.suspended === false) winner.suspended = true;
  // Payroll link: winner keeps its own unless it has none, then take loser's.
  if (!winner.most_recent_payroll_id && loser.most_recent_payroll_id) {
    winner.most_recent_payroll_id = loser.most_recent_payroll_id;
  }
  winner.updated_at = new Date().toISOString();

  // Re-point any other Person who had this loser as their
  // line_manager — leaving them stale would orphan that FK too.
  // Walked BEFORE removing the loser so we don't lose the chance
  // to find it.
  let lineManagerRepointed = 0;
  for (const p of pFile.people) {
    if (String(p.line_manager_id) === loserId) {
      p.line_manager_id = winner.id;
      p.updated_at = new Date().toISOString();
      lineManagerRepointed++;
    }
  }

  // Remove loser from people.json.
  pFile.people = pFile.people.filter(p => String(p.id) !== loserId);

  // Re-point every FK from loser → winner across ALL three linked
  // tables. Missing any of these leaves silent orphans: a Google
  // account or warehouse-activity row whose person_id points at the
  // deleted Person, which then drops off the surviving Person's
  // source-chip set on Directory + Profile without any error message.
  // (Bug #1 from SPEC_TESTING.md run on 2026-05-17.)
  let payrollUpdated = 0, googleUpdated = 0, warehouseUpdated = 0;

  const { sha: paySha, file: payFile } = await fetchPayrollFile(env);
  for (const r of payFile.records) {
    if (String(r.person_id) === loserId) { r.person_id = winner.id; payrollUpdated++; }
  }

  const { sha: gSha, file: gFile } = await fetchGoogleAccountsFile(env);
  for (const r of gFile.records) {
    if (String(r.person_id) === loserId) { r.person_id = winner.id; googleUpdated++; }
  }

  // After re-pointing google rows, refresh the winner's denormalised
  // email fields from the now-complete set of google-accounts so
  // main_google_email / alt_google_emails / external_google_email
  // reflect the merger.
  if (googleUpdated > 0) denormaliseEmailsToPerson(winner, gFile.records);

  // warehouse-activity.json is optional — older repos may not have it.
  let whSha = null, whFile = null;
  try {
    const wh = await fetchWarehouseActivityFile(env);
    whSha = wh.sha; whFile = wh.file;
    for (const r of whFile.records) {
      if (String(r.person_id) === loserId) { r.person_id = winner.id; warehouseUpdated++; }
    }
  } catch (e) { /* file may not exist yet; skip */ }

  // Commit people first (the visible-state change), then each linked
  // table. Each commit is wrapped so a failure on one table doesn't
  // block the others — surfaced as warnings on the response.
  await commitPeopleFile(env, pFile, pSha,
    `People: merge ${loserId} into ${winnerId} (by ${actor})`);

  const warns = {};
  if (payrollUpdated > 0) {
    try {
      await commitPayrollFile(env, payFile, paySha,
        `Payroll: re-point ${payrollUpdated} record(s) from ${loserId} to ${winnerId} (by ${actor})`);
    } catch (e) { warns.payroll_sync_error = e.message; }
  }
  if (googleUpdated > 0) {
    try {
      await commitGoogleAccountsFile(env, gFile, gSha,
        `Google accounts: re-point ${googleUpdated} record(s) from ${loserId} to ${winnerId} (by ${actor})`);
    } catch (e) { warns.google_sync_error = e.message; }
  }
  if (warehouseUpdated > 0 && whFile) {
    try {
      await commitWarehouseActivityFile(env, whFile, whSha,
        `Warehouse activity: re-point ${warehouseUpdated} record(s) from ${loserId} to ${winnerId} (by ${actor})`);
    } catch (e) { warns.warehouse_sync_error = e.message; }
  }

  return {
    ok: true,
    winner_id: winner.id,
    loser_id: loserId,
    line_manager_refs_repointed: lineManagerRepointed,
    payroll_records_repointed: payrollUpdated,
    google_accounts_repointed: googleUpdated,
    warehouse_rows_repointed: warehouseUpdated,
    ...warns,
  };
}

async function doPeopleDelete(env, body, actor) {
  if (!env.GITHUB_TOKEN) return { ok: false, error: "GITHUB_TOKEN not configured" };
  const idStr = ((body || {}).id == null ? "" : String(body.id)).trim();
  if (!idStr) return { ok: false, error: "missing id" };

  const { sha, file } = await fetchPeopleFile(env);
  const before = file.people.length;
  file.people = file.people.filter(p => String(p.id) !== idStr);
  if (file.people.length === before) return { ok: true, no_op: true, message: `no person with id ${idStr}` };
  await commitPeopleFile(env, file, sha, `People: delete #${idStr} (by ${actor})`);
  return { ok: true, deleted: idStr };
}

/* ----------- Pending Drive + Mail transfers ----------- */

// Append a single entry to pending-transfers.json. The Directory page reads
// the file on load to render the in-flight badge; the background scanner
// (scripts/process_pending_transfers.py) is the authority for clearing
// entries once the migration + delete completes. Throws on permanent failure
// so the caller can surface the issue back to the admin (the Drive transfer
// will already have been queued — they need to know mail migration won't
// proceed automatically).
async function appendPendingTransfer(env, entry) {
  if (!env.GITHUB_TOKEN) {
    throw new Error("GITHUB_TOKEN not configured on the worker — cannot persist pending-transfers entry");
  }
  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "apifk-workspace-worker",
  };
  const getRes = await fetch(
    `https://api.github.com/repos/${REPO}/contents/${PENDING_TRANSFERS_PATH}?ref=${BRANCH}`,
    { headers: ghHeaders },
  );
  let current = { schema_version: 1, updated_at: null, entries: [] };
  let sha = null;
  if (getRes.ok) {
    const data = await getRes.json();
    sha = data.sha;
    try { const bin = atob(data.content.replace(/\s/g, "")); current = JSON.parse(new TextDecoder("utf-8").decode(Uint8Array.from(bin, c => c.charCodeAt(0)))); }
    catch (e) { /* fresh file */ }
  } else if (getRes.status !== 404) {
    throw new Error(`pending-transfers.json read failed: HTTP ${getRes.status}`);
  }
  current.schema_version = 1;
  current.entries = Array.isArray(current.entries) ? current.entries : [];
  // Replace any existing entry for this source_email (re-queuing).
  const srcLc = (entry.source_email || "").toLowerCase();
  current.entries = current.entries.filter(e => (e.source_email || "").toLowerCase() !== srcLc);
  current.entries.push(entry);
  current.updated_at = entry.queued_at;
  const newContent = b64Encode(JSON.stringify(current, null, 2) + "\n");
  const msg = `Pending transfer queued: ${entry.source_email} -> ${entry.target_email}`;
  const putRes = await fetch(`https://api.github.com/repos/${REPO}/contents/${PENDING_TRANSFERS_PATH}`, {
    method: "PUT",
    headers: { ...ghHeaders, "Content-Type": "application/json" },
    body: JSON.stringify({
      message: msg,
      content: newContent,
      branch: BRANCH,
      sha: sha || undefined,
    }),
  });
  if (!putRes.ok) {
    const detail = (await putRes.text()).slice(0, 200);
    throw new Error(`pending-transfers.json commit failed: HTTP ${putRes.status} ${detail}`);
  }
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
    try { const bin = atob(data.content.replace(/\s/g, "")); current = JSON.parse(new TextDecoder("utf-8").decode(Uint8Array.from(bin, c => c.charCodeAt(0)))); }
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

/* ────────────────────────────── Wall ────────────────────────────── */
/*
 * /api/wall/whoami     GET  — returns { email, name } of the signed-in viewer.
 * /api/wall/post       POST — { body, photos?[], channel? } → { post }
 * /api/wall/comment    POST — { post_id, body, parent_comment_id? } → { comment }
 * /api/wall/react      POST — { parent_id, parent_kind ("post"|"comment"), emoji }
 *                              → { post_id, target_kind, reactions }   (toggles)
 * /api/wall/mark-seen  POST — { at } → marks all of caller's own posts as seen
 *                              up to that timestamp in wall-seen.json
 *
 * Storage: wall.json + wall-seen.json in the repo, written via GitHub
 * Contents API with retry-on-409 (same pattern as workspace-actions.json).
 */

async function handleWall(req, env, url) {
  const action = url.pathname.replace(/^\/api\/wall\/?/, "").replace(/\/$/, "");

  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: cors(req) });
  }
  if (!req.headers.get("Cf-Access-Jwt-Assertion")) {
    return json({ error: "not authenticated via Cloudflare Access" }, 401, req);
  }

  const viewerEmail = (req.headers.get("Cf-Access-Authenticated-User-Email") || "").toLowerCase();
  const viewerName  = (req.headers.get("Cf-Access-Authenticated-User-Name") || "").trim();

  if (action === "whoami") {
    return json({ ok: true, email: viewerEmail, name: viewerName }, 200, req);
  }
  if (req.method !== "POST") {
    return json({ error: "method not allowed" }, 405, req);
  }

  let body = {};
  try { body = await req.json(); } catch (e) { return json({ error: "bad JSON body" }, 400, req); }

  try {
    switch (action) {
      case "post":         return json(await wallPost(env, viewerEmail, viewerName, body), 200, req);
      case "comment":      return json(await wallComment(env, viewerEmail, viewerName, body), 200, req);
      case "react":        return json(await wallReact(env, viewerEmail, body), 200, req);
      case "mark-seen":    return json(await wallMarkSeen(env, viewerEmail, body), 200, req);
      case "seen-event":   return json(await wallSeenEvent(env, viewerEmail, body), 200, req);
      case "upload-media": return json(await wallUploadMedia(env, viewerEmail, body), 200, req);
      case "gif-search":   return json(await wallGifSearch(env, body), 200, req);
      case "delete":       return json(await wallDelete(env, viewerEmail, body), 200, req);
      case "edit":         return json(await wallEdit(env, viewerEmail, body), 200, req);
      case "poll-vote":    return json(await wallPollVote(env, viewerEmail, body), 200, req);
      case "link-preview": return json(await wallLinkPreview(env, body), 200, req);
      default:             return json({ error: `unknown wall action: ${action}` }, 404, req);
    }
  } catch (e) {
    return json({ ok: false, error: e.message || String(e) }, 500, req);
  }
}

// Accept a base64-encoded blob + content-type, write to wall-media/ via
// GitHub Contents API, return the public path. Caller is responsible for
// client-side resize / format normalisation — Worker just stores bytes.
async function wallUploadMedia(env, viewerEmail, body) {
  if (!viewerEmail) throw new Error("not authenticated");
  const dataUrl = (body.data_url || "").toString();
  const kind = (body.kind || "photo").toString();
  if (!dataUrl.startsWith("data:")) throw new Error("data_url must be a data: URL");
  const m = dataUrl.match(/^data:([^;]+);base64,(.+)$/);
  if (!m) throw new Error("data_url not in 'data:<mime>;base64,<bytes>' form");
  const mime = m[1];
  const b64  = m[2];
  // 25 MB hard cap on the base64 string (≈ 18 MB binary).
  if (b64.length > 25 * 1024 * 1024) throw new Error("media too large (max ~18 MB)");
  const extByMime = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp",
    "image/gif": "gif", "image/svg+xml": "svg",
    "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
  };
  const ext = extByMime[mime];
  if (!ext) throw new Error(`unsupported media type: ${mime}`);
  if (!env.GITHUB_TOKEN) throw new Error("GITHUB_TOKEN not configured on the worker");

  const id = wallId(kind === "video" ? "vid" : "img");
  const path = `wall-media/${id}.${ext}`;
  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "apifk-workspace-worker",
  };
  const putRes = await fetch(`https://api.github.com/repos/${REPO}/contents/${path}`, {
    method: "PUT",
    headers: { ...ghHeaders, "Content-Type": "application/json" },
    body: JSON.stringify({
      message: `Wall: media upload by ${viewerEmail}`,
      content: b64,
      branch: BRANCH,
    }),
  });
  if (!putRes.ok) {
    const detail = (await putRes.text()).slice(0, 200);
    throw new Error(`media upload failed: HTTP ${putRes.status} ${detail}`);
  }
  return { ok: true, path, kind, mime };
}

// Delete a post or comment. Authorisation: the caller must be the author
// of the target, OR a TogetherBook admin (admins.json membership).
async function wallDelete(env, viewerEmail, body) {
  if (!viewerEmail) throw new Error("not authenticated");
  const kind = body.kind === "comment" ? "comment" : "post";
  const id = (body.id || "").toString();
  if (!id) throw new Error("missing id");

  const admins = await fetchAdmins();
  const isAdmin = admins.includes(viewerEmail);

  await updateWallJson(env, doc => {
    if (kind === "post") {
      const posts = doc.posts || [];
      const idx = posts.findIndex(p => p.id === id);
      if (idx < 0) throw new Error("post not found");
      const post = posts[idx];
      if (!isAdmin && (post.author_email || "").toLowerCase() !== viewerEmail) {
        throw new Error("not allowed — you can only delete your own posts");
      }
      posts.splice(idx, 1);
    } else {
      const pid = (body.post_id || "").toString();
      const post = (doc.posts || []).find(p => p.id === pid);
      if (!post) throw new Error("post not found");
      const idx = (post.comments || []).findIndex(c => c.id === id);
      if (idx < 0) throw new Error("comment not found");
      const comment = post.comments[idx];
      if (!isAdmin && (comment.author_email || "").toLowerCase() !== viewerEmail) {
        throw new Error("not allowed — you can only delete your own comments");
      }
      // Drop the comment AND any replies to it (one level only).
      post.comments = post.comments.filter(c => c.id !== id && c.parent_comment_id !== id);
    }
  }, `Wall: ${kind} ${id} deleted by ${viewerEmail}`);

  return { ok: true, kind, id };
}

// Edit a post or comment body in place. Same author-or-admin gate as
// wallDelete; rejects an empty body when there's no media to fall back to.
// Stamps edited_at so the UI can show an "(edited)" tag on the timestamp.
async function wallEdit(env, viewerEmail, body) {
  if (!viewerEmail) throw new Error("not authenticated");
  const kind = body.kind === "comment" ? "comment" : "post";
  const id = (body.id || "").toString();
  const newBody = (body.body == null ? "" : String(body.body));
  if (!id) throw new Error("missing id");
  if (newBody.length > 10000) throw new Error("body too long");

  const editedAt = new Date().toISOString();

  await updateWallJson(env, doc => {
    if (kind === "post") {
      const post = (doc.posts || []).find(p => p.id === id);
      if (!post) throw new Error("post not found");
      // Edit is author-only — admins can delete but not rewrite someone
      // else's words. Stricter than delete on purpose.
      if ((post.author_email || "").toLowerCase() !== viewerEmail) {
        throw new Error("not allowed — only the author can edit a post");
      }
      if (!newBody.trim() && (!post.photos || !post.photos.length)) {
        throw new Error("a post needs either text or a photo");
      }
      post.body = newBody;
      post.edited_at = editedAt;
    } else {
      const pid = (body.post_id || "").toString();
      const post = (doc.posts || []).find(p => p.id === pid);
      if (!post) throw new Error("post not found");
      const c = (post.comments || []).find(x => x.id === id);
      if (!c) throw new Error("comment not found");
      if ((c.author_email || "").toLowerCase() !== viewerEmail) {
        throw new Error("not allowed — only the author can edit a comment");
      }
      if (!newBody.trim() && (!c.photos || !c.photos.length)) {
        throw new Error("a comment needs either text or a photo");
      }
      c.body = newBody;
      c.edited_at = editedAt;
    }
  }, `Wall: ${kind} ${id} edited by ${viewerEmail}`);

  return { ok: true, kind, id, edited_at: editedAt };
}

// Cast or remove a vote on a post's attached poll. Clicking the option
// you've already voted for removes the vote; clicking any other option
// changes it. One vote per user per poll.
async function wallPollVote(env, viewerEmail, body) {
  if (!viewerEmail) throw new Error("not authenticated");
  const id = (body.post_id || "").toString();
  const idx = parseInt(body.option_index, 10);
  if (!id) throw new Error("missing post_id");
  if (Number.isNaN(idx) || idx < 0 || idx > 100) throw new Error("invalid option_index");

  await updateWallJson(env, doc => {
    const post = (doc.posts || []).find(p => p.id === id);
    if (!post) throw new Error("post not found");
    if (!post.poll || !Array.isArray(post.poll.options)) throw new Error("post has no poll");
    if (idx >= post.poll.options.length) throw new Error("option_index out of range");
    post.poll.votes = post.poll.votes || {};
    if (post.poll.votes[viewerEmail] === idx) {
      delete post.poll.votes[viewerEmail];
    } else {
      post.poll.votes[viewerEmail] = idx;
    }
  }, `Wall: poll vote ${id} by ${viewerEmail}`);

  return { ok: true };
}

// Server-side OpenGraph scraper for the page's link-preview cards. The
// page can't fetch arbitrary URLs from the browser (CORS), so the Worker
// pulls the page on its behalf, extracts og:title / og:description /
// og:image / og:site_name, and returns the structured result. Caps the
// fetch at 1 MB / 8 s to avoid the Worker hammering big pages.
async function wallLinkPreview(env, body) {
  const u = (body.url || "").toString().trim();
  if (!/^https?:\/\//i.test(u)) throw new Error("url must be http:// or https://");
  let target;
  try { target = new URL(u); } catch (e) { throw new Error("malformed url"); }
  // Cheap SSRF guard — refuse private / metadata ranges.
  if (/^(localhost|127\.|10\.|192\.168\.|169\.254\.|0\.0\.0\.0)/i.test(target.hostname)) {
    throw new Error("blocked host");
  }
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  let html = "";
  try {
    const res = await fetch(target.toString(), {
      method: "GET",
      headers: {
        "User-Agent": "Mozilla/5.0 (compatible; TogetherBookWall/1.0; +https://book.togetherbook.net)",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en,en-GB;q=0.9",
      },
      signal: ctrl.signal,
      redirect: "follow",
    });
    if (!res.ok) throw new Error(`upstream HTTP ${res.status}`);
    // Read at most 1 MB. Many sites are >5 MB but OG tags are in <head>.
    const reader = res.body.getReader();
    const chunks = [];
    let total = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.length;
      chunks.push(value);
      if (total >= 1024 * 1024) { try { reader.cancel(); } catch (e) {} break; }
    }
    html = new TextDecoder("utf-8").decode(concatUint8(chunks));
  } finally {
    clearTimeout(timer);
  }

  // <head> stops at the first <body> tag — restrict scraping there.
  const headEnd = html.search(/<\/head>|<body[\s>]/i);
  const head = headEnd > 0 ? html.slice(0, headEnd) : html.slice(0, 50000);

  const meta = (name) => {
    // Match <meta property="og:foo" content="bar"> OR content-first ordering.
    const re1 = new RegExp(`<meta[^>]+(?:property|name)\\s*=\\s*["']${name}["'][^>]*content\\s*=\\s*["']([^"']+)["']`, "i");
    const re2 = new RegExp(`<meta[^>]+content\\s*=\\s*["']([^"']+)["'][^>]*(?:property|name)\\s*=\\s*["']${name}["']`, "i");
    const m = head.match(re1) || head.match(re2);
    return m ? m[1].trim() : "";
  };
  const stripTags = s => s.replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();
  const decode = s => s
    .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&#x27;/gi, "'")
    .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(parseInt(n, 10)))
    .replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCharCode(parseInt(n, 16)));

  let title = decode(meta("og:title") || meta("twitter:title"));
  if (!title) {
    const t = head.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
    if (t) title = decode(stripTags(t[1]));
  }
  const description = decode(meta("og:description") || meta("twitter:description") || meta("description"));
  const image = decode(meta("og:image") || meta("twitter:image"));
  const siteName = decode(meta("og:site_name"));

  return {
    ok: true,
    url: target.toString(),
    host: target.host,
    title:       title.slice(0, 300),
    description: description.slice(0, 600),
    image:       image && image.startsWith("//") ? "https:" + image : image,
    site_name:   siteName.slice(0, 80),
  };
}

function concatUint8(chunks) {
  let len = 0; for (const c of chunks) len += c.length;
  const out = new Uint8Array(len);
  let o = 0;
  for (const c of chunks) { out.set(c, o); o += c.length; }
  return out;
}

// Proxy GIPHY v1 /search (or /trending for empty query). Was Tenor in v3
// but Google announced Tenor API service discontinuation in Jan 2026 —
// new API clients aren't being accepted, so we moved to GIPHY which still
// has an open developer-key flow. API key stays a Worker secret.
async function wallGifSearch(env, body) {
  const q = (body.q || "").toString().trim();
  const limit = Math.min(parseInt(body.limit, 10) || 24, 50);
  if (!env.GIPHY_API_KEY) {
    throw new Error("GIPHY_API_KEY not configured on the worker — add it in Cloudflare Worker secrets");
  }
  const endpoint = q ? "search" : "trending";
  const url = new URL(`https://api.giphy.com/v1/gifs/${endpoint}`);
  url.searchParams.set("api_key", env.GIPHY_API_KEY);
  url.searchParams.set("limit", String(limit));
  url.searchParams.set("rating", "pg-13");
  if (q) url.searchParams.set("q", q);
  const res = await fetch(url.toString());
  if (!res.ok) throw new Error(`GIPHY search failed: HTTP ${res.status}`);
  const data = await res.json();
  const results = (data.data || []).map(r => ({
    id: r.id,
    title: r.title || "",
    // tinygif-equivalent for the picker grid (small, fast):
    preview: r.images?.fixed_height_small?.url || r.images?.fixed_width_small?.url || r.images?.preview_gif?.url,
    // Full-size for the actual post:
    url: r.images?.original?.url || r.images?.downsized?.url,
    width:  parseInt(r.images?.original?.width  || "0", 10),
    height: parseInt(r.images?.original?.height || "0", 10),
  })).filter(r => r.preview && r.url);
  return { ok: true, results };
}

function wallId(prefix) {
  const ts = Date.now().toString(36);
  const rnd = Math.random().toString(36).slice(2, 8);
  return `${prefix}_${ts}_${rnd}`;
}

async function wallPost(env, viewerEmail, viewerName, body) {
  if (!viewerEmail) throw new Error("not authenticated");
  const text = (body.body || "").trim();
  if (text.length > 10000) throw new Error("body exceeds 10,000 chars");
  const photos = Array.isArray(body.photos) ? body.photos.slice(0, 10) : [];
  const channel = (body.channel || "").toString().slice(0, 40) || null;

  // Optional poll. Validate shape: a question (1-200 chars) + 2-10 non-empty options
  // (each ≤ 100 chars). Reject silently if the shape is wrong.
  let poll = null;
  if (body.poll && typeof body.poll === "object") {
    const q = (body.poll.question || "").toString().trim();
    const rawOpts = Array.isArray(body.poll.options) ? body.poll.options : [];
    const opts = rawOpts.map(o => (o || "").toString().trim()).filter(o => o.length).slice(0, 10);
    if (q.length >= 1 && q.length <= 200 && opts.length >= 2) {
      poll = {
        question: q.slice(0, 200),
        options:  opts.map(o => o.slice(0, 100)),
        votes:    {},
        created_at: new Date().toISOString(),
      };
    }
  }

  // A post needs SOMETHING — body text, attached media, or a poll.
  if (!text && photos.length === 0 && !poll) throw new Error("empty post");

  const now = new Date().toISOString();
  const newPost = {
    id: wallId("post"),
    author_email: viewerEmail,
    author_name:  viewerName || viewerEmail.split("@")[0],
    created_at:   now,
    body:         text,
    photos,
    channel,
    reactions:    {},
    comments:     [],
  };
  if (poll) newPost.poll = poll;

  await updateWallJson(env, doc => {
    doc.posts = Array.isArray(doc.posts) ? doc.posts : [];
    doc.posts.unshift(newPost);
    // FIFO-trim to keep the file under GitHub's 100 MB limit. ~2000 posts
    // headroom at ~5 KB/post including a few short comments.
    if (doc.posts.length > 2000) doc.posts = doc.posts.slice(0, 2000);
  }, `Wall: post by ${viewerEmail}`);

  return { ok: true, post: newPost };
}

async function wallComment(env, viewerEmail, viewerName, body) {
  if (!viewerEmail) throw new Error("not authenticated");
  const pid = (body.post_id || "").toString();
  const text = (body.body || "").trim();
  const parent = body.parent_comment_id ? body.parent_comment_id.toString() : null;
  if (!pid)  throw new Error("missing post_id");
  const photos = Array.isArray(body.photos) ? body.photos.slice(0, 4) : [];
  if (!text && !photos.length) throw new Error("empty comment");
  if (text.length > 2000) throw new Error("body exceeds 2,000 chars");

  const newComment = {
    id: wallId(parent ? "reply" : "com"),
    parent_comment_id: parent,
    author_email: viewerEmail,
    author_name:  viewerName || viewerEmail.split("@")[0],
    created_at:   new Date().toISOString(),
    body:         text,
    photos,
    reactions:    {},
  };

  await updateWallJson(env, doc => {
    const post = (doc.posts || []).find(p => p.id === pid);
    if (!post) throw new Error("post not found");
    post.comments = post.comments || [];
    post.comments.push(newComment);
  }, `Wall: comment on ${pid} by ${viewerEmail}`);

  return { ok: true, comment: newComment };
}

async function wallReact(env, viewerEmail, body) {
  if (!viewerEmail) throw new Error("not authenticated");
  const parentId = (body.parent_id || "").toString();
  const kind = body.parent_kind === "comment" ? "comment" : "post";
  const emoji = (body.emoji || "").toString();
  if (!parentId) throw new Error("missing parent_id");
  if (!emoji)    throw new Error("missing emoji");
  if ([...emoji].length > 4) throw new Error("emoji too long");

  let resultPostId = null;
  let resultReactions = null;

  const now = new Date().toISOString();
  await updateWallJson(env, doc => {
    const posts = doc.posts || [];
    let hostPost = null;
    let added = false;

    // Sweep the viewer off every OTHER emoji on this target before
    // adding the new one — a user has at most one active reaction per
    // post/comment (Facebook model). Removed entries get a "removed"
    // react_event so the audit log stays accurate.
    const sweepOtherEmojis = (rx) => {
      for (const [otherEmoji, emails] of Object.entries(rx)) {
        if (otherEmoji === emoji) continue;
        const i = (emails || []).map(e => (e || "").toLowerCase()).indexOf(viewerEmail);
        if (i < 0) continue;
        emails.splice(i, 1);
        if (emails.length === 0) delete rx[otherEmoji];
        hostPost.react_events = hostPost.react_events || [];
        hostPost.react_events.push({
          actor_email: viewerEmail,
          emoji: otherEmoji,
          target_kind: kind,
          target_id: parentId,
          at: now,
          kind: "removed",
        });
      }
    };

    if (kind === "post") {
      const post = posts.find(p => p.id === parentId);
      if (!post) throw new Error("post not found");
      post.reactions = post.reactions || {};
      hostPost = post;
      const set = new Set((post.reactions[emoji] || []).map(e => e.toLowerCase()));
      const wasMine = set.has(viewerEmail);
      if (wasMine) {
        set.delete(viewerEmail);
      } else {
        sweepOtherEmojis(post.reactions);
        set.add(viewerEmail);
        added = true;
      }
      if (set.size === 0) delete post.reactions[emoji];
      else post.reactions[emoji] = Array.from(set);
      resultPostId = post.id;
      resultReactions = post.reactions;
    } else {
      let foundPost = null, foundComment = null;
      for (const p of posts) {
        const c = (p.comments || []).find(c => c.id === parentId);
        if (c) { foundPost = p; foundComment = c; break; }
      }
      if (!foundComment) throw new Error("comment not found");
      foundComment.reactions = foundComment.reactions || {};
      hostPost = foundPost;
      const set = new Set((foundComment.reactions[emoji] || []).map(e => e.toLowerCase()));
      const wasMine = set.has(viewerEmail);
      if (wasMine) {
        set.delete(viewerEmail);
      } else {
        sweepOtherEmojis(foundComment.reactions);
        set.add(viewerEmail);
        added = true;
      }
      if (set.size === 0) delete foundComment.reactions[emoji];
      else foundComment.reactions[emoji] = Array.from(set);
      resultPostId = foundPost.id;
      resultReactions = foundComment.reactions;
    }

    // Append a react event to the host post so the page can render this
    // reaction in the post-author's notification feed. Removals are
    // recorded too so we can trim the log later if needed; the page only
    // counts additions newer than seen-at.
    hostPost.react_events = hostPost.react_events || [];
    hostPost.react_events.push({
      actor_email: viewerEmail,
      emoji,
      target_kind: kind,
      target_id: parentId,
      at: now,
      kind: added ? "added" : "removed",
    });
    // FIFO-trim — keep the most recent 200 events per post.
    if (hostPost.react_events.length > 200) {
      hostPost.react_events = hostPost.react_events.slice(-200);
    }
  }, `Wall: react ${emoji} on ${parentId} by ${viewerEmail}`);

  return { ok: true, post_id: resultPostId, target_kind: kind, reactions: resultReactions };
}

async function wallMarkSeen(env, viewerEmail, body) {
  if (!viewerEmail) throw new Error("not authenticated");
  const at = (body.at || new Date().toISOString()).toString();

  // Read wall.json to find every post the viewer either authored OR was
  // mentioned in (via "@<viewer-local>"). Both are events the viewer "owns"
  // for notification purposes — without stamping them as seen, the bell
  // re-shows the same events forever across reloads.
  const viewerLocal = (viewerEmail.split("@")[0] || "").toLowerCase();
  const escLocal = viewerLocal.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const mentionRx = viewerLocal
    ? new RegExp(`(^|\\s)@${escLocal}(?![a-z0-9._-])`, "i")
    : null;
  let postIds = [];
  try {
    const wallRes = await fetch(
      `https://raw.githubusercontent.com/${REPO}/${BRANCH}/${WALL_PATH}?_=${Date.now()}`,
      { headers: { "User-Agent": "apifk-workspace-worker" } },
    );
    if (wallRes.ok) {
      const wall = await wallRes.json();
      const ids = new Set();
      for (const p of (wall.posts || [])) {
        if ((p.author_email || "").toLowerCase() === viewerEmail) { ids.add(p.id); continue; }
        if (mentionRx && mentionRx.test(p.body || "")) { ids.add(p.id); continue; }
        for (const c of (p.comments || [])) {
          if (mentionRx && mentionRx.test(c.body || "")) { ids.add(p.id); break; }
        }
      }
      postIds = [...ids];
    }
  } catch (e) { /* fall through with empty list */ }

  await updateGhJson(env, WALL_SEEN_PATH, doc => {
    doc.schema_version = 1;
    doc.by_user = doc.by_user || {};
    const me = doc.by_user[viewerEmail] = doc.by_user[viewerEmail] || { posts: {} };
    me.posts = me.posts || {};
    for (const pid of postIds) me.posts[pid] = at;
    me.last_marked_at = at;
  }, `Wall: mark-seen by ${viewerEmail}`);

  return { ok: true, at, marked: postIds.length };
}

// Per-notification "I clicked this one" marker. Persists a stable event ID
// into by_user[viewer].seen_events so the client can filter it out on next
// render — survives reloads, distinct from the bulk mark-seen timestamp.
// Capped at 2000 most-recent IDs per user to bound storage growth.
async function wallSeenEvent(env, viewerEmail, body) {
  if (!viewerEmail) throw new Error("not authenticated");
  const evId = (body.event_id || "").toString().trim();
  if (!evId) throw new Error("event_id required");
  if (evId.length > 240) throw new Error("event_id too long");

  await updateGhJson(env, WALL_SEEN_PATH, doc => {
    doc.schema_version = 1;
    doc.by_user = doc.by_user || {};
    const me = doc.by_user[viewerEmail] = doc.by_user[viewerEmail] || { posts: {} };
    const arr = Array.isArray(me.seen_events) ? me.seen_events : [];
    if (!arr.includes(evId)) {
      arr.push(evId);
      me.seen_events = arr.length > 2000 ? arr.slice(-2000) : arr;
    } else {
      me.seen_events = arr;
    }
  }, `Wall: notif seen ${evId} by ${viewerEmail}`);

  return { ok: true, event_id: evId };
}

/** Read → mutate → write wall.json with retry-on-409 (sha conflict). */
async function updateWallJson(env, mutate, commitMsg) {
  return updateGhJson(env, WALL_PATH, doc => {
    doc.schema_version = 1;
    doc.posts = Array.isArray(doc.posts) ? doc.posts : [];
    mutate(doc);
    doc.updated_at = new Date().toISOString();
  }, commitMsg);
}

/** Generic read → mutate → write JSON via GitHub Contents API.
 *  Retries up to 3 times on 409 (someone else committed in between). */
async function updateGhJson(env, path, mutate, commitMsg) {
  if (!env.GITHUB_TOKEN) throw new Error("GITHUB_TOKEN not configured on the worker");
  const ghHeaders = {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "apifk-workspace-worker",
  };
  for (let attempt = 0; attempt < 4; attempt++) {
    const getRes = await fetch(
      `https://api.github.com/repos/${REPO}/contents/${path}?ref=${BRANCH}&_=${Date.now()}`,
      { headers: ghHeaders },
    );
    let doc = {};
    let sha = null;
    if (getRes.ok) {
      const meta = await getRes.json();
      sha = meta.sha;
      try {
        // atob gives back a Latin-1-interpreted binary string. For emojis +
        // any non-ASCII we have to decode the underlying bytes as UTF-8.
        const bin = atob(meta.content.replace(/\s/g, ""));
        const bytes = Uint8Array.from(bin, c => c.charCodeAt(0));
        doc = JSON.parse(new TextDecoder("utf-8").decode(bytes));
      } catch (e) { doc = {}; }
    } else if (getRes.status !== 404) {
      throw new Error(`read ${path} failed: HTTP ${getRes.status}`);
    }
    mutate(doc);
    const newContent = b64Encode(JSON.stringify(doc, null, 2) + "\n");
    const putRes = await fetch(`https://api.github.com/repos/${REPO}/contents/${path}`, {
      method: "PUT",
      headers: { ...ghHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({
        message: commitMsg,
        content: newContent,
        branch: BRANCH,
        sha: sha || undefined,
      }),
    });
    if (putRes.ok) return doc;
    if (putRes.status === 409 || putRes.status === 422) {
      // sha conflict — re-read and retry.
      await new Promise(r => setTimeout(r, 200 * (attempt + 1)));
      continue;
    }
    const detail = (await putRes.text()).slice(0, 200);
    throw new Error(`write ${path} failed: HTTP ${putRes.status} ${detail}`);
  }
  throw new Error(`write ${path} failed after retries (concurrent writers)`);
}

/* ============================================================
 * Holidays API
 *
 * /api/holidays/whoami      GET  — { email, name } (mirror of /api/wall/whoami)
 * /api/holidays/set         POST — { email, date: "YYYY-MM-DD", status: "office"|...|null }
 *                                  → { ok, log_entry, updated_at }
 *                                  status === null clears the day back to the
 *                                  natural default (weekend / BH / office).
 *
 * Authorisation: a caller can always set days on their own email; admins
 * (admins.json members) can set days for any user.
 *
 * Storage: holidays.json at the repo root, with shape
 *   { schema_version, updated_at, year_start, year_end,
 *     by_user: { "email": { days: { "YYYY-MM-DD": "<status>" } } },
 *     log: [ { user_email, date, from, to, changed_by, changed_at } ] }
 * Log is FIFO-trimmed at 5000 entries.
 * ============================================================ */

const HOLIDAYS_PATH = "holidays.json";
const HOLIDAYS_SEEN_PATH = "holidays-seen.json";
const HOLIDAY_LOG_MAX = 5000;
const HOLIDAY_STATUSES = new Set([
  "office", "wfh", "non-working", "holiday",
  // Legacy half-am / half-pm kept for old records; new part-* statuses
  // replace them in the picker.
  "half-am", "half-pm",
  "part-paid-unpaid", "part-holiday-paid",
  "sick", "maternity",
  // approved-holiday is manager-only (enforced below). Storage-side it
  // looks identical to any other override.
  "approved-holiday",
]);
const MANAGER_ONLY_STATUSES = new Set(["approved-holiday"]);

// Reads annotations.json from raw.githubusercontent.com and returns the
// map of email → line_manager email (lowercased). Used by the holidays
// handler to decide whether a non-admin viewer can edit a particular
// target's day.
async function fetchLineManagers() {
  const url = "https://raw.githubusercontent.com/richmondbot2000-prog/togetherbook/main/annotations.json";
  try {
    const res = await fetch(`${url}?ts=${Date.now()}`, {
      headers: { "User-Agent": "apifk-workspace-worker" },
      cf: { cacheTtl: 0, cacheEverything: false },
    });
    if (!res.ok) return {};
    const doc = await res.json();
    const out = {};
    for (const [k, v] of Object.entries((doc && doc.annotations) || {})) {
      const mgr = (v && v.line_manager) ? String(v.line_manager).toLowerCase() : "";
      if (mgr) out[k.toLowerCase()] = mgr;
    }
    return out;
  } catch (e) {
    return {};
  }
}

// GET /api/workspace/activity — D1-backed replacement for the
// raw-GitHub staff-activity-buckets.json fetch. Returns the same
// { by_email, pulled, last_pull_at } shape the page already reads.
async function handleActivityRead(req, env, url) {
  if (!env.ACTIVITY_DB) {
    return json({ error: "ACTIVITY_DB binding not configured on this worker" }, 503, req);
  }
  const viewer = (req.headers.get("Cf-Access-Authenticated-User-Email") || "").toLowerCase();
  if (!viewer) return json({ error: "not authenticated" }, 401, req);

  const fromIso = (url.searchParams.get("from") || "").slice(0, 10);
  const toIso   = (url.searchParams.get("to")   || "").slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(fromIso) || !/^\d{4}-\d{2}-\d{2}$/.test(toIso)) {
    return json({ error: "from/to must be YYYY-MM-DD" }, 400, req);
  }
  const requested = (url.searchParams.get("emails") || viewer)
    .split(",").map(e => e.trim().toLowerCase()).filter(Boolean);

  const admins = await fetchAdmins();
  const isAdmin = admins.includes(viewer);
  let emails = [];
  if (isAdmin) {
    emails = requested;
  } else {
    const lms = await fetchLineManagers();
    emails = requested.filter(e => e === viewer || lms[e] === viewer);
  }
  if (!emails.length) return json({ by_email: {}, pulled: {}, last_pull_at: null }, 200, req);

  const emailPh = emails.map(() => "?").join(",");
  const bucketsRes = await env.ACTIVITY_DB
    .prepare(`SELECT email, iso_date, bucket FROM activity_buckets
              WHERE email IN (${emailPh}) AND iso_date BETWEEN ? AND ?`)
    .bind(...emails, fromIso, toIso).all();
  const eventsRes = await env.ACTIVITY_DB
    .prepare(`SELECT email, iso_date, bucket, src, writes, first_at, last_at, kind
              FROM activity_events
              WHERE email IN (${emailPh}) AND iso_date BETWEEN ? AND ?`)
    .bind(...emails, fromIso, toIso).all();
  const pulledRes = await env.ACTIVITY_DB
    .prepare(`SELECT iso_date, pulled_at FROM activity_pulled
              WHERE iso_date BETWEEN ? AND ?`)
    .bind(fromIso, toIso).all();

  const by_email = {};
  for (const e of emails) by_email[e] = { buckets: {}, events: {} };
  for (const r of bucketsRes.results || []) {
    const slot = by_email[r.email]; if (!slot) continue;
    (slot.buckets[r.iso_date] ||= []).push(r.bucket);
  }
  for (const slot of Object.values(by_email)) {
    for (const d of Object.keys(slot.buckets)) slot.buckets[d].sort((a, b) => a - b);
  }
  for (const r of eventsRes.results || []) {
    const slot = by_email[r.email]; if (!slot) continue;
    (slot.events[r.iso_date] ||= []).push({
      bucket: r.bucket, src: r.src, writes: r.writes,
      first_at: r.first_at, last_at: r.last_at, kind: r.kind || undefined,
    });
  }
  const pulled = {};
  let last_pull_at = null;
  for (const r of pulledRes.results || []) {
    pulled[r.iso_date] = r.pulled_at;
    if (!last_pull_at || r.pulled_at > last_pull_at) last_pull_at = r.pulled_at;
  }
  return json({ by_email, pulled, last_pull_at }, 200, req);
}

// GET /api/workspace/activity-items?email=&date=&bucket=[&src=]
// Returns the per-message detail rows for one (email, iso_date,
// bucket) cell. Same auth/authorisation model as /activity.
async function handleActivityItemsRead(req, env, url) {
  if (!env.ACTIVITY_DB) {
    return json({ error: "ACTIVITY_DB binding not configured" }, 503, req);
  }
  const viewer = (req.headers.get("Cf-Access-Authenticated-User-Email") || "").toLowerCase();
  if (!viewer) return json({ error: "not authenticated" }, 401, req);

  const target = (url.searchParams.get("email") || "").toLowerCase().trim();
  const iso    = (url.searchParams.get("date")  || "").slice(0, 10);
  const bucket = parseInt(url.searchParams.get("bucket") || "", 10);
  const srcFilter = url.searchParams.get("src") || "";
  if (!target || !/^\d{4}-\d{2}-\d{2}$/.test(iso) || !Number.isInteger(bucket)) {
    return json({ error: "need email, date (YYYY-MM-DD), bucket (int 0-95)" }, 400, req);
  }

  const admins = await fetchAdmins();
  const isAdmin = admins.includes(viewer);
  let allowed = (target === viewer) || isAdmin;
  if (!allowed) {
    const lms = await fetchLineManagers();
    allowed = lms[target] === viewer;
  }
  if (!allowed) return json({ error: "not authorised for this user" }, 403, req);

  let sql = `SELECT src, occurred_at, record_id, kind, comm_type, client_type,
                    client_username, campaign_name, auto_processed, body_excerpt
             FROM activity_items
             WHERE email = ? AND iso_date = ? AND bucket = ?`;
  const params = [target, iso, bucket];
  if (srcFilter) { sql += " AND src = ?"; params.push(srcFilter); }
  sql += " ORDER BY occurred_at ASC, record_id ASC LIMIT 500";

  const out = await env.ACTIVITY_DB.prepare(sql).bind(...params).all();
  return json({ items: out.results || [] }, 200, req);
}

async function handleHolidays(req, env, url) {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: cors(req) });
  }
  const action = url.pathname.replace(/^\/api\/holidays\/?/, "").split("/")[0];
  if (!req.headers.get("Cf-Access-Jwt-Assertion")) {
    return json({ error: "not authenticated via Cloudflare Access" }, 401, req);
  }

  const viewerEmail = (req.headers.get("Cf-Access-Authenticated-User-Email") || "").toLowerCase();
  const viewerName  = (req.headers.get("Cf-Access-Authenticated-User-Name") || "").trim();

  if (action === "whoami") {
    return json({ ok: true, email: viewerEmail, name: viewerName }, 200, req);
  }
  if (req.method !== "POST") {
    return json({ error: "method not allowed" }, 405, req);
  }

  let body = {};
  try { body = await req.json(); } catch (e) { return json({ error: "bad JSON body" }, 400, req); }

  try {
    switch (action) {
      case "set":        return json(await holidaysSet(env, viewerEmail, body), 200, req);
      case "seen-event": return json(await holidaysSeenEvent(env, viewerEmail, body), 200, req);
      default:           return json({ error: `unknown holidays action: ${action}` }, 404, req);
    }
  } catch (e) {
    return json({ ok: false, error: e.message || String(e) }, 500, req);
  }
}

async function holidaysSet(env, viewerEmail, body) {
  if (!viewerEmail) throw new Error("not authenticated");
  const target = (body.email || "").toString().trim().toLowerCase();
  const date   = (body.date || "").toString().trim();
  // status === null means "clear" — back to the natural default.
  let status = body.status;
  if (status !== null && status !== undefined) {
    status = String(status);
    if (!HOLIDAY_STATUSES.has(status)) throw new Error(`unknown status: ${status}`);
  } else {
    status = null;
  }
  if (!target) throw new Error("missing email");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new Error("date must be YYYY-MM-DD");

  const admins = await fetchAdmins();
  const isAdmin = admins.includes(viewerEmail);
  let isManagerOfTarget = false;
  if (target !== viewerEmail && !isAdmin) {
    const lineManagers = await fetchLineManagers();
    isManagerOfTarget = (lineManagers[target] || "") === viewerEmail;
    if (!isManagerOfTarget) {
      throw new Error("not allowed — you must be the user's line manager or an admin");
    }
  }
  // Manager-only statuses can only be set by admin / a line manager.
  if (status && MANAGER_ONLY_STATUSES.has(status)) {
    if (target === viewerEmail) {
      throw new Error("'approved-holiday' can only be set by a line manager or an admin");
    }
    // (target !== viewerEmail) — we already verified admin OR manager above.
  }

  let logEntry = null;
  let updatedAt = null;

  // Optional note. Only meaningful when status is one of the part-*
  // variants but the worker doesn't enforce that — it just stores
  // what the page sends (empty string clears).
  const noteRaw = (body.note === undefined || body.note === null) ? undefined : String(body.note);
  const note = noteRaw !== undefined ? noteRaw.slice(0, 100) : undefined;

  await updateGhJson(env, HOLIDAYS_PATH, doc => {
    doc.schema_version = doc.schema_version || 1;
    doc.by_user = doc.by_user || {};
    doc.by_user[target] = doc.by_user[target] || {};
    doc.by_user[target].days = doc.by_user[target].days || {};
    doc.by_user[target].notes = doc.by_user[target].notes || {};
    const days  = doc.by_user[target].days;
    const notes = doc.by_user[target].notes;
    const prev = days[date] || null;
    if (status === null) {
      delete days[date];
      delete notes[date];
    } else {
      days[date] = status;
    }
    if (note !== undefined) {
      if (note) notes[date] = note;
      else delete notes[date];
    }
    updatedAt = new Date().toISOString();
    doc.updated_at = updatedAt;
    logEntry = {
      user_email: target,
      date,
      from: prev,
      to: status,
      changed_by: viewerEmail,
      changed_at: updatedAt,
    };
    doc.log = doc.log || [];
    doc.log.push(logEntry);
    if (doc.log.length > HOLIDAY_LOG_MAX) {
      doc.log = doc.log.slice(-HOLIDAY_LOG_MAX);
    }
  }, `Holidays: ${viewerEmail} set ${target} ${date} → ${status || "(default)"}`);

  return { ok: true, log_entry: logEntry, updated_at: updatedAt };
}

// Per-notification "I've seen this one" marker for the Holidays bell.
// Stable string IDs computed client-side from log entries; persisted
// under by_user[viewer].seen_events in holidays-seen.json. Same pattern
// as wallSeenEvent — capped at 2000 most-recent IDs per user.
async function holidaysSeenEvent(env, viewerEmail, body) {
  if (!viewerEmail) throw new Error("not authenticated");
  const evId = (body.event_id || "").toString().trim();
  if (!evId) throw new Error("event_id required");
  if (evId.length > 240) throw new Error("event_id too long");

  await updateGhJson(env, HOLIDAYS_SEEN_PATH, doc => {
    doc.schema_version = 1;
    doc.by_user = doc.by_user || {};
    const me = doc.by_user[viewerEmail] = doc.by_user[viewerEmail] || { seen_events: [] };
    const arr = Array.isArray(me.seen_events) ? me.seen_events : [];
    if (!arr.includes(evId)) {
      arr.push(evId);
      me.seen_events = arr.length > 2000 ? arr.slice(-2000) : arr;
    } else {
      me.seen_events = arr;
    }
  }, `Holidays: notif seen ${evId} by ${viewerEmail}`);

  return { ok: true, event_id: evId };
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

/* BookR: read+write rg-bookr Realtime DB. See SPEC for endpoints. */
const BOOKR_AVAIL_PATH = "bookr-asset-availability.json";
async function fetchBookrAvailability(env) {
  // Read via the GitHub Contents API at HEAD of main — same trick the
  // people.json / payroll-data.json paths use. Avoids raw.githubusercontent
  // CDN lag (which can serve stale data for minutes after a commit),
  // which would make the admin toggle look like it had no effect.
  if (!env || !env.GITHUB_TOKEN) return { cars: {}, properties: {} };
  try {
    const res = await fetch(
      `https://api.github.com/repos/${REPO}/contents/${BOOKR_AVAIL_PATH}?ref=${BRANCH}&_=${Date.now()}`,
      {
        headers: {
          "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
          "Accept": "application/vnd.github+json",
          "User-Agent": "apifk-workspace-worker",
        },
        cf: { cacheTtl: 0, cacheEverything: false },
      },
    );
    if (!res.ok) return { cars: {}, properties: {} };
    const meta = await res.json();
    const bin = atob((meta.content || "").replace(/\s/g, ""));
    const bytes = Uint8Array.from(bin, c => c.charCodeAt(0));
    const doc = JSON.parse(new TextDecoder("utf-8").decode(bytes));
    return { cars: doc.cars || {}, properties: doc.properties || {} };
  } catch (e) { return { cars: {}, properties: {} }; }
}
const BOOKR_DB_URL = "https://rg-bookr.firebaseio.com";
let _bookrToken = { value: null, exp: 0 };

async function handleBookr(req, env, url) {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors(req) });
  if (!req.headers.get("Cf-Access-Jwt-Assertion")) return json({ error: "not authenticated via Cloudflare Access" }, 401, req);
  if (!env.BOOKR_SERVICE_ACCOUNT_JSON) return json({ error: "BOOKR_SERVICE_ACCOUNT_JSON not configured on worker" }, 503, req);
  const viewerEmail = (req.headers.get("Cf-Access-Authenticated-User-Email") || "").toLowerCase();
  const action = url.pathname.replace(/^\/api\/bookr\/?/, "").split("/")[0];
  try {
    if (action === "whoami")   return json(await bookrWhoami(env, viewerEmail), 200, req);
    if (action === "users")    return json(await bookrUsers(env), 200, req);
    if (action === "assets")   return json(await bookrAssets(env), 200, req);
    if (action === "all-bookings") {
      const filter = url.searchParams.get("type");
      const f      = url.searchParams.get("from");
      const tt     = url.searchParams.get("to");
      return json(await bookrAllBookings(env, filter, f, tt), 200, req);
    }
    if (action === "bookings") {
      const t  = url.searchParams.get("type");
      const id = url.searchParams.get("asset");
      const f  = url.searchParams.get("from");
      const tt = url.searchParams.get("to");
      return json(await bookrBookingsRange(env, t, id, f, tt), 200, req);
    }
    if (action === "comments") {
      const t  = url.searchParams.get("type");
      const id = url.searchParams.get("asset");
      return json(await bookrComments(env, t, id), 200, req);
    }
    if (req.method !== "POST") return json({ error: "method not allowed" }, 405, req);
    let body = {};
    try { body = await req.json(); } catch (e) { return json({ error: "bad JSON body" }, 400, req); }
    if (action === "book")    return json(await bookrBook(env, viewerEmail, body),    200, req);
    if (action === "cancel")  return json(await bookrCancel(env, viewerEmail, body),  200, req);
    if (action === "comment") return json(await bookrComment(env, viewerEmail, body), 200, req);
    if (action === "user-match-or-create") return json(await bookrUserMatchOrCreate(env, viewerEmail, body), 200, req);
    if (action === "user-link")            return json(await bookrUserLink(env, viewerEmail, body),         200, req);
    if (action === "user-add")             return json(await bookrUserAdd(env, viewerEmail, body),          200, req);
    if (action === "asset-availability")   return json(await bookrSetAssetAvailability(env, viewerEmail, body), 200, req);
    if (action === "user-unlink")          return json(await bookrUserUnlink(env, viewerEmail, body),       200, req);
    return json({ error: `unknown bookr action: ${action}` }, 404, req);
  } catch (e) {
    return json({ ok: false, error: e.message || String(e) }, 500, req);
  }
}

async function getBookrAccessToken(env) {
  const now = Math.floor(Date.now() / 1000);
  if (_bookrToken.value && now < _bookrToken.exp - 60) return _bookrToken.value;
  const sa = JSON.parse(env.BOOKR_SERVICE_ACCOUNT_JSON);
  if (!sa.client_email || !sa.private_key) throw new Error("BOOKR_SERVICE_ACCOUNT_JSON missing client_email / private_key");
  const header = { alg: "RS256", typ: "JWT", kid: sa.private_key_id };
  const claims = {
    iss: sa.client_email,
    aud: "https://oauth2.googleapis.com/token",
    scope: "https://www.googleapis.com/auth/firebase.database https://www.googleapis.com/auth/userinfo.email",
    iat: now,
    exp: now + 3600,
  };
  const enc = o => base64url(new TextEncoder().encode(JSON.stringify(o)));
  const signingInput = `${enc(header)}.${enc(claims)}`;
  const pemBody = sa.private_key
    .replace(/-----BEGIN PRIVATE KEY-----/g, "")
    .replace(/-----END PRIVATE KEY-----/g, "")
    .replace(/\s+/g, "");
  const keyBytes = Uint8Array.from(atob(pemBody), c => c.charCodeAt(0));
  const key = await crypto.subtle.importKey(
    "pkcs8", keyBytes, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["sign"],
  );
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(signingInput));
  const jwt = `${signingInput}.${base64url(new Uint8Array(sig))}`;
  const res = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer", assertion: jwt }),
  });
  if (!res.ok) throw new Error(`bookr token exchange ${res.status}: ${(await res.text()).slice(0, 300)}`);
  const tok = (await res.json()).access_token;
  _bookrToken = { value: tok, exp: now + 3500 };
  return tok;
}

async function bookrFetch(env, path, init = {}) {
  const token = await getBookrAccessToken(env);
  const u = `${BOOKR_DB_URL}${path}${path.includes("?") ? "&" : "?"}access_token=${encodeURIComponent(token)}`;
  const res = await fetch(u, init);
  if (!res.ok) {
    const t = await res.text();
    throw new Error(`Firebase ${init.method || "GET"} ${path}: ${res.status} ${t.slice(0, 200)}`);
  }
  const txt = await res.text();
  if (!txt) return null;
  try { return JSON.parse(txt); } catch (e) { return txt; }
}

async function bookrFindUidByEmail(env, candidateEmails) {
  const all = await bookrFetch(env, "/users.json");
  const lowered = candidateEmails.filter(Boolean).map(e => e.toLowerCase());
  for (const [uid, u] of Object.entries(all || {})) {
    const e = ((u && u.email) || "").toLowerCase();
    if (e && lowered.includes(e)) {
      return { uid, name: (u && u.name) || "", email: (u && u.email) || "", suspended: !!(u && u.suspended) };
    }
  }
  return null;
}

async function bookrWhoami(env, viewerEmail) {
  if (!viewerEmail) return { ok: true, email: "", uid: null, uids: [], error: "no Cf-Access email" };
  const candidates = [viewerEmail];
  let person = null;
  try {
    const { file } = await fetchPeopleFile(env);
    const ve = viewerEmail.toLowerCase();
    person = (file.people || []).find(p =>
      [p.main_google_email, ...(p.alt_google_emails || []), p.external_google_email]
        .filter(Boolean).map(e => e.toLowerCase()).includes(ve)
    ) || null;
    if (person) candidates.push(person.main_google_email, ...(person.alt_google_emails || []), person.external_google_email);
  } catch (e) { /* best-effort */ }
  // Person-stored uids are authoritative (they cover work + personal +
  // cross-domain emails); email match is a fallback for non-Persons.
  const uids = new Set(personBookrUids(person));
  let firstHit = null;
  if (uids.size === 0) {
    const hit = await bookrFindUidByEmail(env, candidates);
    if (hit) { uids.add(hit.uid); firstHit = hit; }
  }
  // Hydrate name from /users for whichever uid we return as the primary.
  let primary = Array.from(uids)[0] || null;
  let name = "", suspended = false;
  if (primary) {
    if (firstHit && firstHit.uid === primary) { name = firstHit.name; suspended = firstHit.suspended; }
    else {
      try {
        const u = await bookrUserExists(env, primary);
        if (u) name = u.name;
      } catch (e) {}
    }
  }
  if (uids.size === 0) {
    return { ok: true, email: viewerEmail, uid: null, uids: [], error: "no BookR user with a matching email" };
  }
  return { ok: true, email: viewerEmail, uid: primary, uids: Array.from(uids), name, suspended };
}

async function bookrUsers(env) {
  const all = await bookrFetch(env, "/users.json");
  const out = {};
  for (const [uid, u] of Object.entries(all || {})) {
    out[uid] = { name: (u && u.name) || "", email: (u && u.email) || "", suspended: !!(u && u.suspended) };
  }
  return { ok: true, users: out };
}

async function bookrAssets(env) {
  const [cars, props, avail] = await Promise.all([
    bookrFetch(env, "/cars.json"),
    bookrFetch(env, "/properties.json"),
    fetchBookrAvailability(env),
  ]);
  const flatten = (kind, dict) => Object.entries(dict || {}).map(([id, a]) => ({
    id, type: kind,
    title: a.title || "", sub_title: a.sub_title || "", address: a.address || "",
    code: a.code || "", description: a.description || "", key_information: a.key_information || "",
    latitude: a.latitude || "", longitude: a.longitude || "",
    notice: a.notice || "", safe: a.safe || "",
    listing_id: a.listing_id || a.listingId || "",
    minimum_age: a.minimum_age || "",
    price: (a.price === undefined ? null : a.price),
    // Availability: default true; only false when the asset id is
    // explicitly flagged in bookr-asset-availability.json. The admin
    // page (/bookr-admin.html) is the only place that writes this.
    available: (avail[kind] && avail[kind][id] === false) ? false : true,
  }));
  return { ok: true, cars: flatten("cars", cars), properties: flatten("properties", props) };
}

async function bookrSetAssetAvailability(env, viewerEmail, body) {
  const admins = await fetchAdmins();
  if (!admins.includes((viewerEmail || "").toLowerCase())) throw new Error("admin required");
  const type = body && body.type;
  const id   = body && body.id;
  const available = !!(body && body.available);
  if (!["cars", "properties"].includes(type)) throw new Error("type must be cars|properties");
  if (!id) throw new Error("missing id");
  let final = null;
  await updateGhJson(env, BOOKR_AVAIL_PATH, doc => {
    doc.cars       = doc.cars       || {};
    doc.properties = doc.properties || {};
    if (available) {
      // True is the default -- store it by deleting the explicit-false
      // entry, which keeps the file small over time.
      delete doc[type][id];
    } else {
      doc[type][id] = false;
    }
    final = doc;
  }, `BookR: ${available ? "enable" : "disable"} ${type}/${id} (by ${viewerEmail})`);
  return { ok: true, type, id, available, file: final };
}

async function bookrBookingsRange(env, type, id, from, to) {
  if (!["cars", "properties"].includes(type)) throw new Error(`type must be cars|properties, got ${type}`);
  if (!id) throw new Error("missing asset");
  const all = (await bookrFetch(env, `/${type}/${encodeURIComponent(id)}/bookings.json`)) || {};
  const out = {};
  for (const [date, value] of Object.entries(all)) {
    if (from && date < from) continue;
    if (to   && date > to)   continue;
    out[date] = value;
  }
  return { ok: true, bookings: out };
}

async function bookrResolveTargetUid(env, viewerEmail, body) {
  const admins = await fetchAdmins();
  const isAdmin = admins.includes(viewerEmail);
  if (isAdmin && body.target_user_uid) return body.target_user_uid;
  if (isAdmin && body.target_email) {
    const who = await bookrWhoami(env, body.target_email);
    if (!who.uid) throw new Error(`no BookR user matches ${body.target_email}`);
    return who.uid;
  }
  const own = await bookrWhoami(env, viewerEmail);
  if (!own.uid) throw new Error("your TogetherBook email isn't linked to a BookR user");
  return own.uid;
}

async function bookrBook(env, viewerEmail, body) {
  const type = body.type, asset = body.asset, date = body.date;
  if (!["cars", "properties"].includes(type)) throw new Error("type must be cars|properties");
  if (!asset) throw new Error("missing asset");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new Error("date must be YYYY-MM-DD");
  const uid = await bookrResolveTargetUid(env, viewerEmail, body);
  const path = `/${type}/${encodeURIComponent(asset)}/bookings/${date}.json`;
  await bookrFetch(env, path, { method: "PUT", body: JSON.stringify(uid), headers: { "Content-Type": "application/json" } });
  return { ok: true, type, asset, date, uid };
}

async function bookrCancel(env, viewerEmail, body) {
  const type = body.type, asset = body.asset, date = body.date;
  if (!["cars", "properties"].includes(type)) throw new Error("type must be cars|properties");
  if (!asset) throw new Error("missing asset");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new Error("date must be YYYY-MM-DD");
  const admins = await fetchAdmins();
  const isAdmin = admins.includes(viewerEmail);
  const path = `/${type}/${encodeURIComponent(asset)}/bookings/${date}.json`;
  if (!isAdmin) {
    const own = await bookrWhoami(env, viewerEmail);
    if (!own.uid) throw new Error("your TogetherBook email isn't linked to a BookR user");
    const current = await bookrFetch(env, path);
    if (current !== own.uid) throw new Error("you can only cancel your own bookings");
  }
  // Match BookR's existing convention: cancellations set the date to "free".
  await bookrFetch(env, path, { method: "PUT", body: JSON.stringify("free"), headers: { "Content-Type": "application/json" } });
  return { ok: true, type, asset, date };
}

async function bookrComments(env, type, id) {
  if (!["cars", "properties"].includes(type)) throw new Error("type must be cars|properties");
  if (!id) throw new Error("missing asset");
  const all = (await bookrFetch(env, `/${type}/${encodeURIComponent(id)}/comments.json`)) || {};
  const out = Object.entries(all).map(([cid, c]) => ({
    id: cid,
    author_email: (c && c.author_email) || "",
    author_name:  (c && c.author_name)  || "",
    body:         (c && c.body)         || "",
    ts:           (c && c.ts)           || 0,
  }));
  out.sort((a, b) => (b.ts || 0) - (a.ts || 0));
  return { ok: true, type, asset: id, comments: out };
}

async function bookrComment(env, viewerEmail, body) {
  const type = body.type, asset = body.asset, text = (body.body || "").toString().trim();
  if (!["cars", "properties"].includes(type)) throw new Error("type must be cars|properties");
  if (!asset) throw new Error("missing asset");
  if (!text) throw new Error("empty comment body");
  if (text.length > 4000) throw new Error("comment too long (max 4000 chars)");
  let authorName = "";
  try {
    const who = await bookrWhoami(env, viewerEmail);
    authorName = (who && who.name) || "";
  } catch (e) {}
  if (!authorName) {
    try {
      const { file } = await fetchPeopleFile(env);
      const ve = (viewerEmail || "").toLowerCase();
      const person = (file.people || []).find(p =>
        [p.main_google_email, ...(p.alt_google_emails || []), p.external_google_email]
          .filter(Boolean).map(e => e.toLowerCase()).includes(ve));
      if (person) authorName = person.name || "";
    } catch (e) {}
  }
  const entry = {
    author_email: viewerEmail || "",
    author_name:  authorName,
    body:         text,
    ts:           Date.now(),
  };
  const res = await bookrFetch(env, `/${type}/${encodeURIComponent(asset)}/comments.json`, {
    method: "POST",
    body: JSON.stringify(entry),
    headers: { "Content-Type": "application/json" },
  });
  return { ok: true, type, asset, comment: { id: (res && res.name) || "", ...entry } };
}

async function bookrAllBookings(env, filter, from, to) {
  const want = (!filter || filter === "all") ? ["cars", "properties"]
    : filter === "cars" ? ["cars"]
    : filter === "properties" ? ["properties"]
    : null;
  if (!want) throw new Error("type must be all|cars|properties");
  const out = { cars: {}, properties: {} };
  for (const kind of want) {
    const branch = (await bookrFetch(env, `/${kind}.json`)) || {};
    for (const [id, asset] of Object.entries(branch)) {
      const all = (asset && asset.bookings) || {};
      const slice = {};
      for (const [date, value] of Object.entries(all)) {
        if (from && date < from) continue;
        if (to   && date > to)   continue;
        slice[date] = value;
      }
      out[kind][id] = slice;
    }
  }
  return { ok: true, bookings: out };
}

/* ----------- BookR <-> Person link helpers -----------
 * A TogetherBook-minted BookR user gets a Firebase RTDB push key as its
 * uid rather than a Firebase Auth uid; the BookR mobile app cannot sign
 * in as that record until a Firebase Auth account is provisioned for
 * the same email separately. Bookings made for them still work because
 * BookR keys those on whatever string is in /users.
 *
 * A Person can carry MANY BookR uids (work email, personal email, cross-
 * domain alt). Canonical storage is `Person.bookr_uids: string[]`.
 * Legacy singular `bookr_uid` is read-tolerated via personBookrUids() and
 * stripped from each record the next time it's written (peopleSet).
 */
function personBookrUids(p) {
  if (!p) return [];
  if (Array.isArray(p.bookr_uids)) {
    return p.bookr_uids.filter(x => typeof x === "string" && x.trim()).map(x => x.trim());
  }
  if (typeof p.bookr_uid === "string" && p.bookr_uid.trim()) return [p.bookr_uid.trim()];
  return [];
}

function personCandidateEmails(p) {
  return [p.main_google_email, ...(p.alt_google_emails || []), p.external_google_email]
    .filter(Boolean).map(e => e.toString().toLowerCase());
}

function normName(s) {
  return (s || "").toString().toLowerCase()
    .normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, " ").trim();
}
function emailLocalPart(e) {
  const at = (e || "").indexOf("@");
  return at > 0 ? e.slice(0, at).toLowerCase() : "";
}
// Score a BookR user against a Person:
//   100 exact email, 80 same local-part, 60 normalised name exact,
//   40 given+family both in name, 0 no signal.
function scoreBookrUserForPerson(person, bookrUser) {
  const bEmail = (bookrUser.email || "").toLowerCase();
  const bName  = normName(bookrUser.name);
  if (!bEmail && !bName) return 0;
  const pEmails = personCandidateEmails(person);
  if (bEmail && pEmails.includes(bEmail)) return 100;
  if (bEmail) {
    const bLocal = emailLocalPart(bEmail);
    if (bLocal && pEmails.some(e => emailLocalPart(e) === bLocal)) return 80;
  }
  const pName  = normName(person.name);
  const pGiven = normName(person.given || "");
  const pFam   = normName(person.family || "");
  if (pName && bName && pName === bName) return 60;
  if (pGiven && pFam && bName.includes(pGiven) && bName.includes(pFam)) return 40;
  return 0;
}
async function bookrRankCandidatesForPerson(env, person, limit) {
  const all = await bookrFetch(env, "/users.json") || {};
  const out = [];
  for (const [uid, u] of Object.entries(all)) {
    const score = scoreBookrUserForPerson(person, u || {});
    if (score > 0) {
      out.push({
        uid,
        score,
        email: (u && u.email) || "",
        name: (u && u.name) || "",
        suspended: !!(u && u.suspended),
      });
    }
  }
  out.sort((a, b) => b.score - a.score || (a.email || a.name).localeCompare(b.email || b.name));
  return limit ? out.slice(0, limit) : out;
}

async function bookrUserExists(env, uid) {
  if (!uid) return null;
  const u = await bookrFetch(env, `/users/${encodeURIComponent(uid)}.json`);
  if (!u) return null;
  return { uid, email: (u && u.email) || "", name: (u && u.name) || "" };
}

// Multi-uid matcher for one Person. Returns shape:
//   { bookr_uids, added, already_linked_uids, stale_uids, candidates,
//     matched?, created?, already_linked?, needs_review?, no_candidates? }
//
// Behaviour:
//   - existing array is the baseline; uids missing from /users are
//     reported as stale_uids and dropped from the returned array.
//   - every candidate >= min_score_auto_link (default 80) joins the
//     union, deduped, order preserved (existing first, then new).
//   - top fuzzy candidate (40..79) with nothing already linked and
//     nothing newly added -> needs_review.
//   - zero candidates at any score + nothing linked + create_if_no_match
//     -> mints a fresh BookR user from main_google_email.
async function bookrMatchOrCreateForPerson(env, person, opts) {
  opts = opts || {};
  const allowCreate = !!opts.create_if_no_match;
  const minScoreAutoLink = (typeof opts.min_score_auto_link === "number") ? opts.min_score_auto_link : 80;
  const existing = personBookrUids(person);
  const liveExisting = [];
  const staleUids = [];
  for (const uid of existing) {
    const u = await bookrUserExists(env, uid);
    if (u) liveExisting.push(uid); else staleUids.push(uid);
  }
  const ranked = await bookrRankCandidatesForPerson(env, person, 25);
  const confident = ranked.filter(r => r.score >= minScoreAutoLink);
  const union = liveExisting.slice();
  const added = [];
  for (const c of confident) {
    if (!union.includes(c.uid)) { union.push(c.uid); added.push(c.uid); }
  }
  const top = ranked[0];
  if (added.length === 0 && liveExisting.length === 0) {
    if (top && top.score > 0 && top.score < minScoreAutoLink) {
      return { needs_review: true, score: top.score, candidates: ranked,
        bookr_uids: union, added: [], already_linked_uids: liveExisting, stale_uids: staleUids };
    }
    if (ranked.length === 0) {
      if (!allowCreate) {
        return { no_candidates: true, candidates: [],
          bookr_uids: union, added: [], already_linked_uids: liveExisting, stale_uids: staleUids };
      }
      const primary = (person.main_google_email || personCandidateEmails(person)[0] || "").toString();
      if (!primary) throw new Error("cannot create BookR user: Person has no email");
      const body = { email: primary, name: person.name || "", mobile: person.phone || "0", last_online: 0, suspended: false };
      const res = await bookrFetch(env, "/users.json", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const newUid = res && res.name;
      if (!newUid) throw new Error("BookR user create returned no push key");
      union.push(newUid);
      added.push(newUid);
      return { created: true, bookr_uids: union, added,
        already_linked_uids: liveExisting, stale_uids: staleUids, candidates: ranked };
    }
  }
  const allAlreadyLinked = added.length === 0 && staleUids.length === 0 && liveExisting.length > 0;
  return { matched: added.length > 0, already_linked: allAlreadyLinked,
    bookr_uids: union, added, already_linked_uids: liveExisting, stale_uids: staleUids,
    candidates: ranked, score: top ? top.score : 0 };
}

// Writes bookr_uids onto one Person row inside people.json. Standalone
// fetch -> mutate -> commit so callers don't trip peopleSet's admin-sync
// side-effects on what is a pure link metadata change. Pass null to clear.
async function bookrWritePersonUids(env, personId, bookrUids, actor) {
  const { sha, file } = await fetchPeopleFile(env);
  const p = (file.people || []).find(x => String(x.id) === String(personId));
  if (!p) throw new Error(`no Person with id ${personId}`);
  const arr = Array.isArray(bookrUids)
    ? Array.from(new Set(bookrUids.filter(x => typeof x === "string" && x.trim()).map(x => x.trim())))
    : [];
  p.bookr_uids = arr;
  if ("bookr_uid" in p) delete p.bookr_uid;
  p.updated_at = new Date().toISOString();
  const msg = arr.length
    ? `People: link Person #${p.id} to ${arr.length} BookR uid(s) (by ${actor})`
    : `People: clear BookR uids on Person #${p.id} (by ${actor})`;
  await commitPeopleFile(env, file, sha, msg);
  return p;
}

async function bookrUserMatchOrCreate(env, viewerEmail, body) {
  const admins = await fetchAdmins();
  if (!admins.includes((viewerEmail || "").toLowerCase())) throw new Error("admin required");
  const pid = body && body.person_id;
  if (!pid) throw new Error("missing person_id");
  const { file } = await fetchPeopleFile(env);
  const person = (file.people || []).find(p => String(p.id) === String(pid));
  if (!person) throw new Error(`no Person with id ${pid}`);
  const result = await bookrMatchOrCreateForPerson(env, person, {
    create_if_no_match: !!(body && body.create_if_no_match),
  });
  if (result.needs_review) {
    return { ok: true, needs_review: true, score: result.score, candidates: result.candidates, bookr_uids: result.bookr_uids, already_linked_uids: result.already_linked_uids, stale_uids: result.stale_uids };
  }
  if (result.no_candidates) {
    return { ok: true, no_candidates: true, candidates: [], bookr_uids: result.bookr_uids, already_linked_uids: result.already_linked_uids, stale_uids: result.stale_uids };
  }
  const before = personBookrUids(person);
  const after = result.bookr_uids || [];
  const changed = before.length !== after.length || before.some((u, i) => u !== after[i]);
  if (changed) await bookrWritePersonUids(env, person.id, after, viewerEmail);
  return {
    ok: true,
    matched: !!result.matched,
    created: !!result.created,
    already_linked: !!result.already_linked,
    score: result.score || null,
    bookr_uids: after,
    added: result.added || [],
    already_linked_uids: result.already_linked_uids || [],
    stale_uids: result.stale_uids || [],
    candidates: result.candidates || [],
  };
}

// Admin reset: replaces the entire bookr_uids array with [body.bookr_uid].
// Kept for backward compatibility; user-add is preferred for incremental
// additions.
async function bookrUserLink(env, viewerEmail, body) {
  const admins = await fetchAdmins();
  if (!admins.includes((viewerEmail || "").toLowerCase())) throw new Error("admin required");
  const pid = body && body.person_id;
  const uid = ((body && body.bookr_uid) || "").toString().trim();
  if (!pid) throw new Error("missing person_id");
  if (!uid) throw new Error("missing bookr_uid");
  const existing = await bookrUserExists(env, uid);
  if (!existing) throw new Error(`no BookR user with uid ${uid}`);
  await bookrWritePersonUids(env, pid, [uid], viewerEmail);
  return { ok: true, person_id: pid, bookr_uids: [uid] };
}

// Append a uid to the array if not already present. Idempotent.
async function bookrUserAdd(env, viewerEmail, body) {
  const admins = await fetchAdmins();
  if (!admins.includes((viewerEmail || "").toLowerCase())) throw new Error("admin required");
  const pid = body && body.person_id;
  const uid = ((body && body.bookr_uid) || "").toString().trim();
  if (!pid) throw new Error("missing person_id");
  if (!uid) throw new Error("missing bookr_uid");
  const existing = await bookrUserExists(env, uid);
  if (!existing) throw new Error(`no BookR user with uid ${uid}`);
  const { file } = await fetchPeopleFile(env);
  const person = (file.people || []).find(p => String(p.id) === String(pid));
  if (!person) throw new Error(`no Person with id ${pid}`);
  const current = personBookrUids(person);
  if (current.includes(uid)) {
    return { ok: true, person_id: pid, bookr_uids: current, added: false };
  }
  const next = current.concat([uid]);
  await bookrWritePersonUids(env, pid, next, viewerEmail);
  return { ok: true, person_id: pid, bookr_uids: next, added: true };
}

// Remove one uid (body.bookr_uid) or all (omit). Returns the post-state.
async function bookrUserUnlink(env, viewerEmail, body) {
  const admins = await fetchAdmins();
  if (!admins.includes((viewerEmail || "").toLowerCase())) throw new Error("admin required");
  const pid = body && body.person_id;
  if (!pid) throw new Error("missing person_id");
  const uid = ((body && body.bookr_uid) || "").toString().trim();
  const { file } = await fetchPeopleFile(env);
  const person = (file.people || []).find(p => String(p.id) === String(pid));
  if (!person) throw new Error(`no Person with id ${pid}`);
  const current = personBookrUids(person);
  if (!uid) {
    await bookrWritePersonUids(env, pid, [], viewerEmail);
    return { ok: true, person_id: pid, bookr_uids: [], cleared: true };
  }
  if (!current.includes(uid)) throw new Error(`bookr_uid ${uid} not on Person #${pid}`);
  const next = current.filter(u => u !== uid);
  await bookrWritePersonUids(env, pid, next, viewerEmail);
  return { ok: true, person_id: pid, bookr_uids: next, removed: uid };
}
