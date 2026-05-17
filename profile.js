/*
  Shared renderer for the user profile page.

  Mounts into <div id="upRoot"> in either:
    - user.html?email=<addr>   (legacy direct link)
    - /directory/<slug>        (clean URL, served by 404.html SPA shim)

  Primary data source is /people.json (canonical Person table). Falls back
  to /staff.json for display fields where the Person record is sparse.
*/
(function () {
  const qs = new URLSearchParams(location.search || "");
  const initialTab = (qs.get("tab") || "calendar").toLowerCase();

  let targetEmail = (window.__profileEmail || qs.get("email") || "").toLowerCase().trim();
  let targetSlug  = (window.__profileSlug  || "").toLowerCase().trim();

  let people = [];
  let peopleByEmail = {};
  let peopleBySlug = {};
  let person = null;             // the resolved target Person
  let staffByEmail = {};         // fallback display source
  let wallPosts = [];
  let payrollByEmail = {};
  let viewerEmail = "";
  let viewerIsAdmin = false;
  let currentTab = "info";
  let annotationsMap = {};       // for forward_to fallback
  let pendingTransfersByEmail = {};
  let adminEmails = new Set();

  const WORKSPACE_API = "/api/workspace";

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
  }
  function emailToSlug(email) { return ((email || "").split("@")[0] || "").toLowerCase(); }
  function profileHref(email) {
    const slug = emailToSlug(email);
    return slug ? `/directory/${slug}` : "#";
  }
  function initials(name) {
    return (name || "").split(/\s+/).filter(Boolean).map(p => p.charAt(0).toUpperCase()).slice(0, 2).join("") || "?";
  }
  function dirPhotoKey(email) {
    return (email || "").toString().trim().toLowerCase().replace(/@/g, "_at_");
  }
  function avatarSrc() {
    if (!person) return "";
    if (person.directory_photo_uploaded_at && person.main_google_email) {
      return `/assets/photos/${dirPhotoKey(person.main_google_email)}.jpg?v=${encodeURIComponent(person.directory_photo_uploaded_at)}`;
    }
    const u = staffByEmail[(person.main_google_email || "").toLowerCase()];
    return (u && u.photo_url) || "";
  }
  function coverSrc() {
    if (!person || !person.cover_photo_uploaded_at || !person.main_google_email) return "";
    return `/assets/covers/${dirPhotoKey(person.main_google_email)}.jpg?v=${encodeURIComponent(person.cover_photo_uploaded_at)}`;
  }
  function svgIcon(name) {
    const paths = {
      info:    `<rect x="3" y="3" width="14" height="18" rx="1" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M7 8h6M7 12h6M7 16h4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>`,
      feed:    `<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>`,
      calendar: `<rect x="3" y="5" width="18" height="16" rx="1" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M3 9h18M8 3v4M16 3v4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>`,
      org:     `<circle cx="12" cy="5" r="3" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="5" cy="19" r="3" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="19" cy="19" r="3" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M12 8v4M12 12H5v4M12 12h7v4" stroke="currentColor" stroke-width="1.6" fill="none"/>`,
      edit:    `<path d="M14 4l6 6-9 9H5v-6l9-9z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>`,
      camera:  `<path d="M4 8h3l2-3h6l2 3h3v11H4z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><circle cx="12" cy="13.5" r="3.2" fill="none" stroke="currentColor" stroke-width="1.6"/>`,
    };
    return `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || ""}</svg>`;
  }

  function tenureLabel() {
    const start = person && person.start_date;
    if (!start) {
      const p = peopleByEmail[(person && person.main_google_email || "").toLowerCase()];
      const pr = payrollByEmail[(person && person.main_google_email || "").toLowerCase()];
      if (!pr || !pr.start_date) return "";
      return formatTenure(pr.start_date);
    }
    return formatTenure(start);
  }
  function formatTenure(start) {
    const d = new Date(start);
    if (isNaN(d.getTime())) return "";
    const now = new Date();
    let yrs = now.getFullYear() - d.getFullYear();
    if (now.getMonth() < d.getMonth() || (now.getMonth() === d.getMonth() && now.getDate() < d.getDate())) yrs -= 1;
    if (yrs < 0) return "";
    if (yrs === 0) {
      const months = (now.getFullYear() - d.getFullYear()) * 12 + (now.getMonth() - d.getMonth());
      return months <= 1 ? "Joined this month" : `Joined ${months} months ago`;
    }
    return `${yrs} year${yrs === 1 ? "" : "s"} in the organisation`;
  }

  function canEditPerson() {
    if (!person) return false;
    if (viewerIsAdmin) return true;
    const owned = [person.main_google_email, ...(person.alt_google_emails || []), person.external_google_email]
      .filter(Boolean).map(e => e.toLowerCase());
    return owned.includes(viewerEmail);
  }

  function setTab(tab) {
    currentTab = tab;
    const url = new URL(location.href);
    if (tab && tab !== "info") url.searchParams.set("tab", tab);
    else url.searchParams.delete("tab");
    history.replaceState({}, "", url.toString());
    document.querySelectorAll("[data-tab]").forEach(t => t.classList.toggle("is-active", t.dataset.tab === tab));
    renderPanel();
  }

  function renderPanel() {
    const panel = document.getElementById("upPanel");
    if (!panel) return;
    if (currentTab === "info")        panel.innerHTML = renderInfoPanel();
    else if (currentTab === "wall")   panel.innerHTML = renderFeedPanel();
    else if (currentTab === "calendar") panel.innerHTML = renderCalendarPanel();
    wirePanel();
  }

  function renderCalendarPanel() {
    const src = `/holidays.html?user=${encodeURIComponent(person.main_google_email)}&view=own&embed=1`;
    return `
      <h2 class="up-panel-title">Calendar</h2>
      <div class="up-cal-wrap">
        <iframe class="up-cal-frame" id="upCalFrame" src="${src}" title="Calendar" referrerpolicy="same-origin"></iframe>
      </div>`;
  }

  /* ─── Information panel — fields come from the Person record ──────── */
  function renderInfoPanel() {
    const editable = canEditPerson();
    const lockedBadge = editable ? "" : `<span class="up-card-hint">read-only · sign in as ${escapeHtml(person.name || person.id)} or an admin to edit</span>`;

    const lineMgr = (person.line_manager_id && people.find(p => p.id === person.line_manager_id)) || null;
    const lineMgrDisplay = lineMgr
      ? `<a class="up-mgr-link" href="${profileHref(lineMgr.main_google_email)}">${escapeHtml(lineMgr.name || lineMgr.id)}</a>`
      : (person.line_manager_email_raw
          ? `<span>${escapeHtml(person.line_manager_email_raw)}</span>`
          : `<span class="up-empty-val">No line manager</span>`);

    // Read-only "Workspace" fields summarised from the Person record + the
    // raw Workspace row.
    const u = staffByEmail[(person.main_google_email || "").toLowerCase()] || {};
    const tenants = (person.alt_google_emails || []).length
      ? `${person.main_google_email} · ${person.alt_google_emails.join(" · ")}`
      : person.main_google_email;
    const readOnly = [
      ["Main Google account", escapeHtml(person.main_google_email || "—")],
      ["Alt Google accounts", (person.alt_google_emails || []).length
                                ? escapeHtml(person.alt_google_emails.join(", "))
                                : '<span class="up-empty-val">—</span>'],
      ["External Google",     person.external_google_email ? escapeHtml(person.external_google_email) : '<span class="up-empty-val">—</span>'],
      ["Auth0 ID",            person.auth0_id ? `<code>${escapeHtml(person.auth0_id)}</code>` : '<span class="up-empty-val">—</span>'],
      ["Access level",        `<span class="up-pill up-pill--${escapeHtml(person.access_level || "staff")}">${escapeHtml(person.access_level || "staff")}</span>`],
      ["Status",              person.suspended || person.access_level === "former"
                                ? '<span class="up-pill up-pill--suspended">Suspended</span>'
                                : '<span class="up-pill up-pill--live">Live</span>'],
      ["Department",          u.department ? escapeHtml(u.department) : (person.department ? escapeHtml(person.department) : '<span class="up-empty-val">—</span>')],
      ["Start date",          person.start_date
                                ? escapeHtml(new Date(person.start_date).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" }))
                                : '<span class="up-empty-val">—</span>'],
    ];
    const readOnlyHtml = readOnly.map(([label, value]) => `
      <div class="up-field">
        <div class="up-field-label">${escapeHtml(label)}</div>
        <div class="up-field-value">${value}</div>
      </div>`).join("");

    // Editable block (Role, Phone, Address). Line manager + access_level
    // are admin-only — we show them as read-only here and admins use
    // /people.html for the deeper edits.
    function editableRow(field, label, type, value, hint) {
      const readonly = !editable;
      const editor = readonly ? "" : `
          <div class="up-field-editor" hidden>
            ${type === "textarea"
              ? `<textarea name="${field}" rows="3">${escapeHtml(value || "")}</textarea>`
              : `<input type="${type}" name="${field}" value="${escapeHtml(value || "")}">`}
            ${hint ? `<p class="up-hint">${hint}</p>` : ""}
            <div class="up-editor-row">
              <button type="button" class="up-btn-sm up-btn-sm--primary" data-edit-save="${field}">Save</button>
              <button type="button" class="up-btn-sm" data-edit-cancel="${field}">Cancel</button>
              <span class="up-edit-status" data-edit-status="${field}"></span>
            </div>
          </div>`;
      return `
        <div class="up-field" data-edit-field="${field}">
          <div class="up-field-label">${escapeHtml(label)}</div>
          <div class="up-field-display">
            <span class="up-field-value ${value ? "" : "up-empty-val"}" style="white-space:pre-wrap;">${escapeHtml(value) || "Not set"}</span>
            ${readonly ? "" : `<button type="button" class="up-link-btn" data-edit-toggle="${field}">Edit</button>`}
          </div>
          ${editor}
        </div>`;
    }

    return `
      <h2 class="up-panel-title">Information</h2>

      <div class="up-card">
        <div class="up-card-head">Editable details ${lockedBadge}</div>
        ${editableRow("role",    "Role",    "text",     person.role)}
        ${editableRow("phone",   "Phone",   "tel",      person.phone)}
        ${editableRow("address", "Address", "textarea", person.address)}
        <div class="up-field" data-edit-field="line_manager_id">
          <div class="up-field-label">Line manager</div>
          <div class="up-field-display">${lineMgrDisplay}</div>
        </div>
      </div>

      <div class="up-card">
        <div class="up-card-head">Identity & access</div>
        <div class="up-fields-grid">${readOnlyHtml}</div>
      </div>

      ${renderGoogleAccountsSection()}

      ${viewerIsAdmin ? renderExternalIdentitySection() : ""}`;
  }

  /* ─── Google accounts section (inline per-account actions) ─────── */
  function tenantFor(email) {
    const domain = ((email || "").split("@")[1] || "").toLowerCase();
    if (domain === "togetherloans.com") return "togetherloans";
    return "";
  }
  function accountState(email) {
    const e = (email || "").toLowerCase();
    const u = staffByEmail[e] || null;
    const pending = pendingTransfersByEmail[e] || null;
    const fwd = (annotationsMap[e] || {}).forward_to || "";
    return {
      exists:        !!u,
      suspended:     !!(u && u.suspended),
      deletion_time: (u && u.deletion_time) || "",
      forwarding_to: fwd || ((u && u.suspended) ? "" : ""),
      pending,
      admin:         adminEmails.has(e),
    };
  }
  function renderGoogleAccountsSection() {
    const accounts = [
      { email: person.main_google_email, role: "main" },
      ...(person.alt_google_emails || []).map(e => ({ email: e, role: "alt" })),
      ...(person.external_google_email ? [{ email: person.external_google_email, role: "external" }] : []),
    ].filter(a => a.email);
    if (!accounts.length) {
      return `<div class="up-card"><div class="up-card-head">Google accounts</div><div class="up-empty">No Google accounts linked.</div></div>`;
    }
    const rows = accounts.map(a => renderAccountRow(a.email, a.role)).join("");
    return `
      <div class="up-card">
        <div class="up-card-head">Google accounts
          ${viewerIsAdmin ? '<span class="up-card-hint">all admin actions live here · niche flows (rename / convert-to-group / alias-to-group) in <a href="/directory-legacy.html" style="color:inherit;text-decoration:underline;">legacy Directory</a></span>' : ""}
        </div>
        ${rows}
      </div>`;
  }
  function renderAccountRow(email, accRole) {
    const st = accountState(email);
    const aliases = ((staffByEmail[email.toLowerCase()] || {}).aliases || []).filter(a => a !== email);
    const aliasLine = aliases.length ? `<div class="up-acct-aliases">aliases: ${aliases.map(escapeHtml).join(", ")}</div>` : "";
    const isMine = (viewerEmail === email.toLowerCase());

    let badges = [];
    if (!st.exists && accRole === "external") badges.push(`<span class="up-acct-badge up-acct-badge--ext">External</span>`);
    else if (!st.exists)                       badges.push(`<span class="up-acct-badge up-acct-badge--missing">Not in Workspace</span>`);
    else if (st.deletion_time)                 badges.push(`<span class="up-acct-badge up-acct-badge--deleted">Deleted</span>`);
    else if (st.pending)                       badges.push(`<span class="up-acct-badge up-acct-badge--pending">Transferring</span>`);
    else if (st.suspended)                     badges.push(`<span class="up-acct-badge up-acct-badge--suspended">Suspended</span>`);
    else                                       badges.push(`<span class="up-acct-badge up-acct-badge--live">Live</span>`);
    if (accRole === "main")                    badges.push(`<span class="up-acct-badge">Main</span>`);
    if (st.admin)                              badges.push(`<span class="up-acct-badge up-acct-badge--admin">Workspace admin</span>`);
    if (st.forwarding_to)                      badges.push(`<span class="up-acct-badge up-acct-badge--forward">→ ${escapeHtml(st.forwarding_to)}</span>`);

    const actions = (viewerIsAdmin && st.exists && accRole !== "external") ? renderAccountButtons(email, st, isMine) : "";

    return `
      <div class="up-acct" data-acc-email="${escapeHtml(email)}">
        <div class="up-acct-head">
          <div>
            <div class="up-acct-email">${escapeHtml(email)}</div>
            ${aliasLine}
          </div>
          <div class="up-acct-badges">${badges.join("")}</div>
        </div>
        ${actions}
        <div class="up-acct-form" hidden></div>
      </div>`;
  }
  function renderAccountButtons(email, st, isMine) {
    const buttons = [];
    if (st.deletion_time) {
      buttons.push(`<button data-acc-action="recover">Recover</button>`);
    } else if (st.suspended) {
      buttons.push(`<button data-acc-action="unsuspend" class="up-acct-btn-primary">Unsuspend</button>`);
      if (st.forwarding_to)
        buttons.push(`<button data-acc-action="cancel-forwarding">Cancel forwarding</button>`);
      buttons.push(`<button data-acc-action="delete-now" class="danger">Delete account</button>`);
    } else if (st.pending) {
      buttons.push(`<button disabled title="Drive + Mail migration in flight">Transferring…</button>`);
    } else {
      if (st.forwarding_to) {
        buttons.push(`<button data-acc-action="disable-forwarding">Turn off forwarding</button>`);
      } else {
        buttons.push(`<button data-acc-action="forward">Add forwarding</button>`);
      }
      if (!isMine) buttons.push(`<button data-acc-action="suspend-route" class="up-acct-btn-primary">Suspend & forward</button>`);
      buttons.push(`<button data-acc-action="reset-password">Reset password</button>`);
      if (!isMine) buttons.push(`<button data-acc-action="transfer-delete" class="danger">Delete (transfer Drive + Mail first)</button>`);
    }
    return `<div class="up-acct-actions">${buttons.join("")}</div>`;
  }
  function renderExternalIdentitySection() {
    const html = [];
    html.push(`<div class="up-card"><div class="up-card-head">Other identities <span class="up-card-hint">admin-only · used to grant access without a Workspace seat</span></div>`);
    html.push(`<div class="up-field">
      <div class="up-field-label">External Google account</div>
      <div class="up-field-editor up-field-editor--open">
        <input type="email" name="external_google_email" value="${escapeHtml(person.external_google_email || "")}" placeholder="external.email@gmail.com">
        <div class="up-editor-row">
          <button type="button" class="up-btn-sm up-btn-sm--primary" data-edit-save="external_google_email">Save</button>
          <span class="up-edit-status" data-edit-status="external_google_email"></span>
        </div>
      </div>
    </div>`);
    html.push(`<div class="up-field">
      <div class="up-field-label">Auth0 ID</div>
      <div class="up-field-editor up-field-editor--open">
        <input type="text" name="auth0_id" value="${escapeHtml(person.auth0_id || "")}" placeholder="auth0|abc123…">
        <div class="up-editor-row">
          <button type="button" class="up-btn-sm up-btn-sm--primary" data-edit-save="auth0_id">Save</button>
          <span class="up-edit-status" data-edit-status="auth0_id"></span>
        </div>
      </div>
    </div>`);
    html.push(`</div>`);
    return html.join("");
  }

  function wirePanel() {
    document.querySelectorAll("[data-edit-toggle]").forEach(btn => {
      btn.addEventListener("click", () => {
        const root = btn.closest("[data-edit-field]");
        if (!root) return;
        root.querySelector(".up-field-display").hidden = true;
        const ed = root.querySelector(".up-field-editor");
        ed.hidden = false;
        const inp = ed.querySelector("input, textarea");
        if (inp) { inp.focus(); inp.select && inp.select(); }
      });
    });
    document.querySelectorAll("[data-edit-cancel]").forEach(btn => {
      btn.addEventListener("click", () => {
        const root = btn.closest("[data-edit-field]");
        if (!root) return;
        root.querySelector(".up-field-editor").hidden = true;
        root.querySelector(".up-field-display").hidden = false;
      });
    });
    document.querySelectorAll("[data-edit-save]").forEach(btn => {
      btn.addEventListener("click", () => savePersonField(btn.dataset.editSave));
    });
    document.querySelectorAll("[data-acc-action]").forEach(btn => {
      btn.addEventListener("click", () => handleAccountAction(btn));
    });
  }

  /* ─── Account action handlers (call workspace worker inline) ───── */
  async function handleAccountAction(btn) {
    const card = btn.closest(".up-acct");
    const email = card && card.dataset.accEmail;
    if (!email) return;
    const action = btn.dataset.accAction;
    const form = card.querySelector(".up-acct-form");
    const t = tenantFor(email);

    // Actions that need a target email (forward / suspend-route / delete+transfer).
    const NEEDS_TARGET = new Set(["forward", "suspend-route", "transfer-delete"]);
    if (NEEDS_TARGET.has(action)) {
      const labels = {
        "forward":          ["Forward mail to colleague", "When mail arrives at this account, deliver it to:"],
        "suspend-route":    ["Suspend & forward",        "Suspend this account and forward all mail to:"],
        "transfer-delete":  ["Transfer Drive + mail, then delete", "Drive files + Gmail will be migrated to:"],
      }[action];
      const colleagueOpts = people
        .filter(p => p.id !== person.id && p.main_google_email)
        .sort((a, b) => (a.name || "").localeCompare(b.name || ""))
        .map(p => `<option value="${escapeHtml(p.main_google_email)}">${escapeHtml(p.name || p.id)}</option>`).join("");
      form.hidden = false;
      form.innerHTML = `
        <h4>${escapeHtml(labels[0])}</h4>
        <p class="up-hint">${escapeHtml(labels[1])}</p>
        <input type="email" list="upAccTargets" placeholder="colleague.email@…" data-acc-target>
        <datalist id="upAccTargets">${colleagueOpts}</datalist>
        <div class="up-editor-row">
          <button class="up-btn-sm up-btn-sm--primary" data-acc-confirm>Confirm</button>
          <button class="up-btn-sm" data-acc-cancel>Cancel</button>
          <span class="up-edit-status" data-acc-status></span>
        </div>`;
      form.querySelector("[data-acc-cancel]").addEventListener("click", () => { form.hidden = true; form.innerHTML = ""; });
      form.querySelector("[data-acc-confirm]").addEventListener("click", () => {
        const target = (form.querySelector("[data-acc-target]").value || "").trim().toLowerCase();
        if (!target.includes("@")) {
          form.querySelector("[data-acc-status]").textContent = "Pick a target email";
          form.querySelector("[data-acc-status]").className = "up-edit-status up-edit-status--err";
          return;
        }
        runAccountAction(form, email, action, target, t);
      });
      form.querySelector("[data-acc-target]").focus();
      return;
    }

    // Instant actions — confirm and fire.
    const CONFIRM = {
      "unsuspend":          { msg: `Unsuspend ${email}? Mail will resume delivery and the seat goes back to £11/month.` },
      "cancel-forwarding":  { msg: `Stop forwarding mail from ${email}? Future mail will land in the suspended account's inbox (effectively a black hole).` },
      "disable-forwarding": { msg: `Turn off mail forwarding on ${email}? Mail will land in this account's inbox again.` },
      "delete-now":         { msg: `DELETE ${email} now?\n\nThis is permanent after 20 days. Use "Delete (transfer Drive + Mail first)" instead if the account has files to keep.` },
      "reset-password":     { msg: `Reset password for ${email}?\n\nA new password will be generated and shown — copy it now, it's only visible once.` },
      "recover":            { msg: `Recover ${email}?\n\nRestores the deleted account to live, billed £11/month from today.` },
    }[action];
    if (CONFIRM && !confirm(CONFIRM.msg)) return;
    runAccountAction(null, email, action, null, t);
  }

  async function runAccountAction(form, email, action, target, tenant) {
    const status = form && form.querySelector("[data-acc-status]");
    if (status) { status.textContent = "Working…"; status.className = "up-edit-status up-edit-status--working"; }
    const map = {
      "suspend-route":      ["suspend-and-route",  { email, route_to: target }],
      "forward":            ["add-forwarding",     { email, target }],
      "cancel-forwarding":  ["cancel-forwarding",  { email }],
      "disable-forwarding": ["disable-forwarding", { email }],
      "unsuspend":          ["unsuspend",          { email }],
      "delete-now":         ["delete-account",     { email }],
      "transfer-delete":    ["queue-transfer-and-delete", { email, target }],
      "reset-password":     ["reset-password",     { email }],
      "recover":            ["recover",            { email }],
    };
    const [act, args] = map[action] || [];
    if (!act) return;
    try {
      const res = await fetch(WORKSPACE_API, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: act, tenant, ...args }),
      });
      const out = await res.json();
      if (!res.ok || !out.ok) throw new Error(out.error || `HTTP ${res.status}`);
      if (act === "reset-password" && out.new_password) {
        alert(`Password for ${email}:\n\n${out.new_password}\n\nCopy now — it's only shown once.`);
      }
      // Refresh data from origin so badges reflect the change.
      await reloadAccountData();
      renderPanel();
    } catch (err) {
      const msg = "Failed — " + (err && err.message || err);
      if (status) { status.textContent = msg; status.className = "up-edit-status up-edit-status--err"; }
      else alert(msg);
    }
  }

  async function reloadAccountData() {
    const [staff, annFile, pending] = await Promise.all([
      fetch("/staff.json",            { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
      fetch("/annotations.json",      { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
      fetch("/pending-transfers.json",{ cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
    ]);
    if (staff && Array.isArray(staff.users)) {
      staffByEmail = {};
      for (const u of staff.users) { const k = (u.email || "").toLowerCase(); if (k) staffByEmail[k] = u; }
    }
    if (annFile && annFile.annotations) annotationsMap = annFile.annotations;
    pendingTransfersByEmail = {};
    if (pending && Array.isArray(pending.entries)) {
      for (const p of pending.entries) {
        const k = (p.source_email || "").toLowerCase();
        if (k) pendingTransfersByEmail[k] = p;
      }
    }
  }

  async function savePersonField(field) {
    const root = document.querySelector(`[data-edit-field="${field}"]`);
    if (!root) return;
    const status = root.querySelector("[data-edit-status]");
    const input  = root.querySelector("input, textarea");
    const value  = (input && input.value || "").trim();
    status.textContent = "Saving…"; status.className = "up-edit-status up-edit-status--working";
    try {
      const res = await fetch(WORKSPACE_API, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "people-set", id: person.id, [field]: value }),
      });
      const out = await res.json();
      if (!res.ok || !out.ok) throw new Error(out.error || `HTTP ${res.status}`);
      Object.assign(person, out.person);
      status.textContent = "Saved";
      status.className = "up-edit-status up-edit-status--ok";
      setTimeout(() => { renderPanel(); }, 350);
    } catch (err) {
      status.textContent = "Failed — " + (err && err.message || err);
      status.className = "up-edit-status up-edit-status--err";
    }
  }

  /* ─── Feed panel (Wall preview, read-only) ─────────────────────────── */
  function plainBody(text) {
    return String(text || "").replace(/<\/?strong>/gi, "").replace(/<\/?em>/gi, "").trim();
  }
  function postPhotoUrl(p) {
    if (!p) return "";
    if (p.media && p.media.url) return p.media.url;
    if (p.photo_url) return p.photo_url;
    return "";
  }
  function renderFeedPanel() {
    const ownedEmails = [person.main_google_email, ...(person.alt_google_emails || []), person.external_google_email]
      .filter(Boolean).map(e => e.toLowerCase());
    const posts = (wallPosts || [])
      .filter(p => ownedEmails.includes((p.author_email || "").toLowerCase()))
      .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
    if (!posts.length) return `<h2 class="up-panel-title">Wall</h2><div class="up-empty">No posts yet.</div>`;

    const photo = avatarSrc();
    const avatarHtml = photo ? `<img src="${escapeHtml(photo)}" alt="">` : `<span>${escapeHtml(initials(person.name))}</span>`;
    const cards = posts.map(p => {
      const ts = p.created_at ? new Date(p.created_at).toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
      const href = `/wall.html?post=${encodeURIComponent(p.id)}`;
      const body = escapeHtml(plainBody(p.body));
      const mediaUrl = postPhotoUrl(p);
      const mediaHtml = mediaUrl ? `<div class="up-fp-media"><img src="${escapeHtml(mediaUrl)}" alt="" loading="lazy"></div>` : "";
      const commentN = Array.isArray(p.comments) ? p.comments.length : 0;
      const reactN = (p.reactions || []).reduce((s, r) => s + (r.count || (r.users && r.users.length) || 0), 0);
      const meta = [
        commentN ? `${commentN} comment${commentN === 1 ? "" : "s"}` : "",
        reactN   ? `${reactN} reaction${reactN === 1 ? "" : "s"}` : "",
      ].filter(Boolean).join(" · ");
      return `
        <a class="up-fp" href="${href}">
          <div class="up-fp-head">
            <div class="up-fp-avatar">${avatarHtml}</div>
            <div>
              <div class="up-fp-name">${escapeHtml(person.name || person.id)}</div>
              <div class="up-fp-time">${escapeHtml(ts)}</div>
            </div>
          </div>
          ${body ? `<div class="up-fp-body">${body}</div>` : ""}
          ${mediaHtml}
          ${meta ? `<div class="up-fp-meta">${escapeHtml(meta)}</div>` : ""}
          <div class="up-fp-open">Open on Wall →</div>
        </a>`;
    }).join("");
    return `<h2 class="up-panel-title">Wall (${posts.length})</h2><div class="up-fp-list">${cards}</div>`;
  }

  /* ─── Page assembly ────────────────────────────────────────────────── */
  function renderEmpty(msg) {
    const root = document.getElementById("upRoot");
    if (root) root.innerHTML = `<div class="up-error">${escapeHtml(msg)}</div>`;
  }

  function renderProfile() {
    // Resolve target: slug → Person, email → Person.
    if (targetSlug && peopleBySlug[targetSlug]) person = peopleBySlug[targetSlug];
    else if (targetEmail && peopleByEmail[targetEmail]) person = peopleByEmail[targetEmail];

    if (!person) {
      renderEmpty(`No person matched "${targetSlug || targetEmail || "(missing)"}".`);
      return;
    }

    targetEmail = (person.main_google_email || "").toLowerCase();
    const editable = canEditPerson();
    const photo = avatarSrc();
    const avatar = photo
      ? `<img src="${escapeHtml(photo)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('span'),{textContent:'${escapeHtml(initials(person.name))}'}))">`
      : escapeHtml(initials(person.name));
    const cover = coverSrc();
    const role = person.role || person.title || "";
    const tenure = tenureLabel();
    const subline = [
      role ? `<span class="up-role">${escapeHtml(role)}</span>` : "",
      tenure ? `<span class="up-tenure">${escapeHtml(tenure)}</span>` : "",
    ].filter(Boolean).join("");

    document.getElementById("upRoot").innerHTML = `
      <div class="up-header">
        <div class="up-cover" ${cover ? `style="background-image: linear-gradient(135deg, rgba(44,62,102,0.0) 0%, rgba(44,62,102,0.18) 100%), url('${escapeHtml(cover)}'); background-size: cover; background-position: center;"` : ""}>
          ${editable ? `
            <button class="up-cover-edit" type="button" id="upCoverEdit" title="Change cover photo">${svgIcon("camera")} Edit cover</button>
            <input type="file" id="upCoverInput" accept="image/*" hidden>
          ` : ""}
        </div>
        <div class="up-info">
          <div class="up-id">
            <div class="up-avatar-wrap">
              <div class="up-avatar">${avatar}</div>
              ${editable ? `
                <button class="up-avatar-edit" type="button" id="upAvatarEdit" title="Change avatar">${svgIcon("camera")}</button>
                <input type="file" id="upAvatarInput" accept="image/*" hidden>
              ` : ""}
            </div>
            <div class="up-headline">
              <h1 class="up-name">${escapeHtml(person.name || person.id)}</h1>
              <div class="up-subline">${subline || `<span class="up-tenure">${escapeHtml(person.main_google_email || person.id)}</span>`}</div>
            </div>
          </div>
          <div class="up-actions">
            <a class="up-btn" href="/org-structure.html?center=${encodeURIComponent(person.main_google_email)}">${svgIcon("org")} Org chart</a>
            ${viewerIsAdmin ? `<a class="up-btn up-btn--primary" href="/directory.html">${svgIcon("edit")} All people</a>` : ""}
          </div>
        </div>
      </div>

      <div class="up-body">
        <nav class="up-tabs" aria-label="Profile sections">
          <a class="up-tab" data-tab="calendar" href="?tab=calendar">${svgIcon("calendar")}<span>Calendar</span></a>
          <a class="up-tab" data-tab="info"     href="?tab=info">${svgIcon("info")}<span>Info</span></a>
          <a class="up-tab" data-tab="wall"     href="?tab=wall">${svgIcon("feed")}<span>Wall</span></a>
        </nav>
        <section class="up-panel" id="upPanel"></section>
      </div>`;

    document.title = `${person.name || person.id} — BOOK Profile`;

    document.querySelectorAll("[data-tab]").forEach(t => {
      t.addEventListener("click", e => { e.preventDefault(); setTab(t.dataset.tab); });
    });
    if (editable) wirePhotoUploads();
    setTab(["info","wall","calendar"].includes(initialTab) ? initialTab : "calendar");
  }

  /* ─── Photo uploads (avatar + cover) ──────────────────────────────── */
  function wirePhotoUploads() {
    const ave = document.getElementById("upAvatarEdit");
    const avi = document.getElementById("upAvatarInput");
    if (ave && avi) {
      ave.addEventListener("click", () => avi.click());
      avi.addEventListener("change", () => uploadImage(avi.files && avi.files[0], { kind: "avatar" }));
    }
    const cove = document.getElementById("upCoverEdit");
    const covi = document.getElementById("upCoverInput");
    if (cove && covi) {
      cove.addEventListener("click", () => covi.click());
      covi.addEventListener("change", () => uploadImage(covi.files && covi.files[0], { kind: "cover" }));
    }
  }

  function readImage(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload  = () => {
        const img = new Image();
        img.onload  = () => resolve(img);
        img.onerror = () => reject(new Error("could not decode image"));
        img.src = r.result;
      };
      r.onerror = () => reject(new Error("could not read file"));
      r.readAsDataURL(file);
    });
  }

  function resizeToJpegB64(img, opts) {
    // Cover (1600×500 max, crop centred) or avatar (400×400 square crop).
    const isCover = opts.kind === "cover";
    const tw = isCover ? 1600 : 400;
    const th = isCover ? 500  : 400;
    const sAspect = img.width / img.height;
    const tAspect = tw / th;
    let sx = 0, sy = 0, sw = img.width, sh = img.height;
    if (sAspect > tAspect) {
      sw = img.height * tAspect; sx = (img.width - sw) / 2;
    } else {
      sh = img.width / tAspect; sy = (img.height - sh) / 2;
    }
    const c = document.createElement("canvas");
    c.width = tw; c.height = th;
    c.getContext("2d").drawImage(img, sx, sy, sw, sh, 0, 0, tw, th);
    return c.toDataURL("image/jpeg", 0.85).split(",")[1];
  }

  async function uploadImage(file, opts) {
    if (!file) return;
    try {
      const img = await readImage(file);
      const b64 = resizeToJpegB64(img, opts);
      const action = opts.kind === "cover" ? "cover-photo-upload" : "directory-photo-upload";
      const res = await fetch(WORKSPACE_API, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, user_email: person.main_google_email, photo_b64: b64, tenant: (person.company || "").includes("togetherloans") ? "togetherloans" : "" }),
      });
      const out = await res.json();
      if (!res.ok || !out.ok) throw new Error(out.error || `HTTP ${res.status}`);
      // Record the timestamp on the Person record so caches bust.
      const stamp = new Date().toISOString();
      const field = opts.kind === "cover" ? "cover_photo_uploaded_at" : "directory_photo_uploaded_at";
      const setRes = await fetch(WORKSPACE_API, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "people-set", id: person.id, [field]: stamp }),
      });
      const setOut = await setRes.json();
      if (!setRes.ok || !setOut.ok) throw new Error(setOut.error || `HTTP ${setRes.status}`);
      Object.assign(person, setOut.person);
      renderProfile();
    } catch (err) {
      alert("Upload failed: " + (err && err.message || err));
    }
  }

  // Calendar iframe sends {type:"holidaysEmbedSize", height: N} as its
  // content reflows; resize so the panel grows with it and avoids nested
  // scrollbars.
  window.addEventListener("message", e => {
    if (!e || !e.data || e.data.type !== "holidaysEmbedSize") return;
    const frame = document.getElementById("upCalFrame");
    if (!frame) return;
    const h = Math.max(360, Math.min(4000, Math.round(e.data.height || 0)));
    if (h) frame.style.height = h + "px";
  });

  /* ─── Boot ────────────────────────────────────────────────────────── */
  Promise.all([
    fetch("/people.json",           { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/staff.json",            { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/wall.json",             { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/api/workspace/payroll", { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/api/workspace/whoami",  { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/annotations.json",      { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/admins.json",           { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/pending-transfers.json",{ cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
  ]).then(([peopleFile, staff, wallFile, payroll, who, annFile, adminsFile, pending]) => {
    if (peopleFile && Array.isArray(peopleFile.people)) {
      people = peopleFile.people;
      for (const p of people) {
        const slug = (p.id || emailToSlug(p.main_google_email)).toLowerCase();
        if (slug && !peopleBySlug[slug]) peopleBySlug[slug] = p;
        for (const e of [p.main_google_email, ...(p.alt_google_emails || []), p.external_google_email].filter(Boolean)) {
          peopleByEmail[e.toLowerCase()] = p;
        }
      }
    }
    if (staff && Array.isArray(staff.users)) {
      for (const u of staff.users) {
        const k = (u.email || "").toLowerCase();
        if (k) staffByEmail[k] = u;
      }
    }
    if (wallFile && Array.isArray(wallFile.posts)) wallPosts = wallFile.posts;
    if (payroll && Array.isArray(payroll.rows)) {
      for (const r of payroll.rows) {
        const e = (r.email || "").toLowerCase();
        if (e) payrollByEmail[e] = r;
      }
    } else if (payroll && payroll.by_email) {
      payrollByEmail = payroll.by_email;
    }
    if (who) { viewerEmail = (who.email || "").toLowerCase(); viewerIsAdmin = !!who.is_admin; }
    if (annFile && annFile.annotations) annotationsMap = annFile.annotations;
    if (adminsFile && Array.isArray(adminsFile.admins)) {
      adminEmails = new Set(adminsFile.admins.map(e => (e || "").toLowerCase()));
    }
    if (pending && Array.isArray(pending.entries)) {
      for (const p of pending.entries) {
        const k = (p.source_email || "").toLowerCase();
        if (k) pendingTransfersByEmail[k] = p;
      }
    }
    renderProfile();
  }).catch(err => renderEmpty("Failed to load: " + String(err)));
})();
