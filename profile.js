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
  const initialTab = (qs.get("tab") || "info").toLowerCase();

  let targetEmail = (window.__profileEmail || qs.get("email") || "").toLowerCase().trim();
  let targetSlug  = (window.__profileSlug  || "").toLowerCase().trim();

  /* ─── localStorage write-through ──────────────────────────────────
   * Every successful save also stashes the edit in localStorage under
   * tbk.edit.<personId>.<field>. On render we overlay any entry < 5
   * minutes old over the server-returned Person. So even if the
   * server response gets lost in transit, or some cache layer serves
   * a stale copy, the user's OWN session always sees their own edit
   * correctly. The 5-minute TTL means once the new server copy is
   * confirmed stable, the localStorage entry expires and we trust
   * the server again. */
  const LS = {
    KEY: (pid, field) => `tbk.edit.${pid}.${field}`,
    TTL_MS: 5 * 60 * 1000,
    set(pid, field, value) {
      try { localStorage.setItem(this.KEY(pid, field), JSON.stringify({ v: value, t: Date.now() })); }
      catch (e) {}
    },
    get(pid, field) {
      try {
        const raw = localStorage.getItem(this.KEY(pid, field));
        if (!raw) return null;
        const o = JSON.parse(raw);
        if (Date.now() - (o.t || 0) > this.TTL_MS) { localStorage.removeItem(this.KEY(pid, field)); return null; }
        return o;
      } catch (e) { return null; }
    },
    savedLabel(pid, field) {
      const e = this.get(pid, field);
      if (!e) return null;
      const d = new Date(e.t);
      return `Saved ${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}`;
    },
    overlay(p) {
      if (!p) return p;
      const out = { ...p };
      const fields = [
        "name","given","family","aliases","role","phone","address",
        "start_date","date_of_birth","notes","company","title","department",
        "directory_photo_uploaded_at","cover_photo_uploaded_at",
        "external_google_email","auth0_id","access_level","suspended","on_payroll",
        "line_manager_id","most_recent_payroll_id",
      ];
      for (const f of fields) {
        const e = this.get(p.id, f);
        if (e) out[f] = e.v;
      }
      return out;
    },
  };

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
  let auditActions = [];   // workspace-actions.json, append-only audit log
  let payrollRecordsById = {};   // PayrollData rows keyed by id
  let payrollByPersonId = {};    // most-recent PayrollData row per person_id
  let googleByPersonId = {};     // pid -> [google-account rows]
  let warehouseByPersonId = {};  // pid -> best warehouse-activity row

  const WORKSPACE_API = "/api/workspace";

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
  }
  function emailToSlug(email) { return ((email || "").split("@")[0] || "").toLowerCase(); }
  function profileHrefForPerson(p) {
    return p && p.url_slug ? `/directory/${p.url_slug}` : "#";
  }
  function profileHref(email) {
    const p = peopleByEmail[(email || "").toLowerCase()];
    if (p && p.url_slug) return `/directory/${p.url_slug}`;
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
    // The photo could have been uploaded against any of the Person's
    // Google emails. annotations.json is keyed by the upload email, so
    // walk all of them and use the first that has a stamp.
    const candidates = [person.main_google_email, ...(person.alt_google_emails||[]), person.external_google_email].filter(Boolean);
    for (const e of candidates) {
      const ann = annotationsMap[e.toLowerCase()];
      if (ann && ann.directory_photo_uploaded_at) {
        return `/assets/photos/${dirPhotoKey(e)}.jpg?v=${encodeURIComponent(ann.directory_photo_uploaded_at)}`;
      }
    }
    if (person.directory_photo_uploaded_at && person.main_google_email) {
      return `/assets/photos/${dirPhotoKey(person.main_google_email)}.jpg?v=${encodeURIComponent(person.directory_photo_uploaded_at)}`;
    }
    const u = staffByEmail[(person.main_google_email || "").toLowerCase()];
    return (u && u.photo_url) || "";
  }
  function coverSrc() {
    if (!person) return "";
    // Covers, like profile photos, are keyed by the email the upload
    // happened under. For merged Persons that's often an alt rather
    // than the current main_google_email — walk all linked emails and
    // use the first one annotations.json has a cover timestamp for.
    const candidates = [person.main_google_email, ...(person.alt_google_emails||[]), person.external_google_email].filter(Boolean);
    for (const e of candidates) {
      const ann = annotationsMap[e.toLowerCase()];
      if (ann && ann.cover_photo_uploaded_at) {
        return `/assets/covers/${dirPhotoKey(e)}.jpg?v=${encodeURIComponent(ann.cover_photo_uploaded_at)}`;
      }
    }
    if (person.cover_photo_uploaded_at && person.main_google_email) {
      return `/assets/covers/${dirPhotoKey(person.main_google_email)}.jpg?v=${encodeURIComponent(person.cover_photo_uploaded_at)}`;
    }
    return "";
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
    try {
      if (currentTab === "info")          panel.innerHTML = renderInfoPanel();
      else if (currentTab === "wall")     panel.innerHTML = renderFeedPanel();
      else if (currentTab === "calendar") panel.innerHTML = renderCalendarPanel();
      else if (currentTab === "accounts") panel.innerHTML = renderAccountsPanel();
    } catch (err) {
      panel.innerHTML = `<div class="up-error" style="padding:24px;">
        Render error in <strong>${escapeHtml(currentTab)}</strong> tab:<br>
        ${escapeHtml((err && err.name) || "Error")}: ${escapeHtml((err && err.message) || String(err))}
      </div>`;
      console.error("renderPanel failed for tab", currentTab, err);
    }
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
    const lockedBadge = editable ? "" : `<span class="up-card-hint">read-only · sign in as ${escapeHtml(person.name || person.url_slug)} or an admin to edit</span>`;

    const lineMgr = (person.line_manager_id != null && people.find(p => String(p.id) === String(person.line_manager_id))) || null;
    const lineMgrDisplay = lineMgr
      ? `<a class="up-mgr-link" href="${profileHrefForPerson(lineMgr)}">${escapeHtml(lineMgr.name || lineMgr.url_slug)}</a>`
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
      const savedLabel = LS.savedLabel(person.id, field);
      const savedBadge = savedLabel
        ? `<span class="up-saved-badge" title="Your last edit to this field — overlaid from local cache for 5 min so it can't appear to revert">✓ ${escapeHtml(savedLabel)}</span>`
        : "";
      // Per-row Save/Cancel buttons are gone — the whole card now has a
      // single Edit button at the top that flips every editor on at once
      // and one Save at the bottom that submits them as a batch.
      const editor = readonly ? "" : `
          <div class="up-field-editor" hidden>
            ${type === "textarea"
              ? `<textarea name="${field}" rows="3" data-orig="${escapeHtml(value || "")}">${escapeHtml(value || "")}</textarea>`
              : `<input type="${type}" name="${field}" value="${escapeHtml(value || "")}" data-orig="${escapeHtml(value || "")}">`}
            ${hint ? `<p class="up-hint">${hint}</p>` : ""}
          </div>`;
      return `
        <div class="up-field" data-edit-field="${field}">
          <div class="up-field-label">${escapeHtml(label)} ${savedBadge}</div>
          <div class="up-field-display">
            <span class="up-field-value ${value ? "" : "up-empty-val"}" style="white-space:pre-wrap;">${escapeHtml(value) || "Not set"}</span>
          </div>
          ${editor}
        </div>`;
    }

    // Line-manager picker (admin only). The display falls back to the
    // read-only chip / "No line manager" when the viewer isn't admin.
    const lineMgrOptions = people
      .filter(x => x.id !== person.id)
      .sort((a, b) => (a.name || "").localeCompare(b.name || ""))
      .map(x => `<option value="${escapeHtml(x.id)}" ${String(x.id) === String(person.line_manager_id) ? "selected" : ""}>${escapeHtml(x.name || x.url_slug)}</option>`)
      .join("");
    const lineMgrEditor = viewerIsAdmin ? `
      <div class="up-field-editor" hidden>
        <select name="line_manager_id" data-orig="${escapeHtml(String(person.line_manager_id || ""))}">
          <option value="">(none)</option>${lineMgrOptions}
        </select>
      </div>` : "";

    // Access-level editor (admin only).
    const accessLevels = [
      ["admin",    "Admin"],
      ["staff",    "Standard user"],
      ["outsider", "Outsider"],
      ["former",   "Former (no access)"],
    ];
    const accessLevelEditor = viewerIsAdmin ? `
      <div class="up-field-editor" hidden>
        <select name="access_level" data-orig="${escapeHtml(person.access_level || "")}">
          ${accessLevels.map(([v, l]) => `<option value="${v}" ${person.access_level === v ? "selected" : ""}>${l}</option>`).join("")}
        </select>
      </div>` : "";

    // Admin-controls card: Suspend toggle + Delete person. Visible only
    // to admins; calls people-set / people-delete (people-delete is
    // admin-gated in the worker, see line 396).
    const adminControls = viewerIsAdmin ? `
      <div class="up-card up-card--danger">
        <div class="up-card-head">Admin controls</div>
        <div class="up-field">
          <div class="up-field-label">Status</div>
          <div class="up-field-display">
            <span class="up-pill up-pill--${person.suspended ? "suspended" : "live"}">${person.suspended ? "Suspended" : "Live"}</span>
            <button type="button" class="up-link-btn" data-person-suspend="${person.suspended ? "unsuspend" : "suspend"}">
              ${person.suspended ? "Reactivate" : "Suspend access"}
            </button>
            <span class="up-edit-status" data-edit-status="suspended"></span>
          </div>
        </div>
        <div class="up-field">
          <div class="up-field-label">Danger zone</div>
          <div class="up-field-display">
            <button type="button" class="up-btn-sm up-btn-sm--danger" data-person-delete="${escapeHtml(person.id)}">Delete person</button>
            <span class="up-card-hint">Removes the Person record. Linked Google accounts are NOT touched — use the per-account Delete button below for those.</span>
            <span class="up-edit-status" data-edit-status="delete"></span>
          </div>
        </div>
      </div>` : "";

    return `
      <h2 class="up-panel-title">Information</h2>

      <div class="up-card up-card--editable" data-edit-card>
        <div class="up-card-head">
          Editable details ${lockedBadge}
          ${editable ? `<button type="button" class="up-link-btn up-card-edit-toggle" data-card-edit>Edit</button>` : ""}
        </div>
        ${editableRow("name",       "Display name", "text",     person.name)}
        ${editableRow("aliases",    "Aliases",      "text",     (person.aliases || []).join(", "), "Comma-separated — used in name search + mentions")}
        ${editableRow("role",       "Role",         "text",     person.role)}
        ${editableRow("phone",      "Phone",        "tel",      person.phone)}
        ${editableRow("address",    "Address",      "textarea", person.address)}
        ${editableRow("start_date", "Start date",   "date",     person.start_date)}
        <div class="up-field" data-edit-field="line_manager_id">
          <div class="up-field-label">Line manager</div>
          <div class="up-field-display">${lineMgrDisplay}</div>
          ${lineMgrEditor}
        </div>
        <div class="up-field" data-edit-field="access_level">
          <div class="up-field-label">Access level</div>
          <div class="up-field-display">
            <span class="up-pill up-pill--${escapeHtml(person.access_level || "staff")}">${escapeHtml(person.access_level || "staff")}</span>
          </div>
          ${accessLevelEditor}
        </div>
        ${editable ? `
          <div class="up-card-edit-footer" hidden>
            <button type="button" class="up-btn-sm up-btn-sm--primary" data-card-save>Save</button>
            <button type="button" class="up-btn-sm" data-card-cancel>Cancel</button>
            <span class="up-edit-status" data-card-status></span>
          </div>` : ""}
      </div>

      <div class="up-card">
        <div class="up-card-head">Identity & access</div>
        <div class="up-fields-grid">${readOnlyHtml}</div>
      </div>

      ${adminControls}

      ${renderActivityCard()}

      ${viewerIsAdmin ? renderMergeCard() : ""}`;
  }

  /* ─── Recent activity card (audit log filtered to this Person) ──── */
  function renderActivityCard() {
    if (!auditActions.length) return "";
    const emails = [person.main_google_email, ...(person.alt_google_emails || []), person.external_google_email]
      .filter(Boolean).map(e => e.toLowerCase());
    const idStr = String(person.id);
    const slug = (person.url_slug || "").toLowerCase();
    const matches = auditActions.filter(a => {
      const t = (a.target || "").toString().toLowerCase();
      if (!t) return false;
      if (emails.some(e => t === e || t.includes(e))) return true;
      // Person-id matches: "people-set #91", "people-merge 5 → 4", etc.
      if (t.includes(idStr)) return true;
      // Slug match for older entries.
      if (slug && t.includes(slug)) return true;
      return false;
    }).slice(-20).reverse();
    if (!matches.length) {
      return `
        <div class="up-card">
          <div class="up-card-head">Recent activity <span class="up-card-hint">audit log filtered to this Person · last 20 entries</span></div>
          <div class="up-empty">No admin actions recorded for this Person yet.</div>
        </div>`;
    }
    const rows = matches.map(a => {
      const d = new Date(a.ts);
      const when = `${d.toLocaleDateString("en-GB", { day: "numeric", month: "short" })} ${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}`;
      const okBadge = a.ok ? '<span class="up-act-ok">✓</span>' : '<span class="up-act-fail">✗</span>';
      return `
        <div class="up-act-row">
          <span class="up-act-when">${escapeHtml(when)}</span>
          ${okBadge}
          <span class="up-act-action">${escapeHtml(a.action || "")}</span>
          <span class="up-act-target">${escapeHtml(a.target || "")}</span>
          <span class="up-act-actor">by ${escapeHtml((a.actor || "").split("@")[0])}</span>
          ${a.error ? `<span class="up-act-error" title="${escapeHtml(a.error)}">⚠</span>` : ""}
        </div>`;
    }).join("");
    return `
      <div class="up-card">
        <div class="up-card-head">Recent activity <span class="up-card-hint">audit log filtered to this Person · last 20 entries</span></div>
        <div class="up-act-list">${rows}</div>
      </div>`;
  }

  /* ─── Linked sources strip (same icons as the directory row) ───── */
  const SRC_GOOGLE_SVG = `<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M21.6 12.23c0-.68-.06-1.34-.18-1.98H12v3.75h5.4a4.62 4.62 0 0 1-2 3.03v2.51h3.23c1.9-1.74 2.97-4.31 2.97-7.31z"/><path fill="currentColor" d="M12 22c2.7 0 4.96-.9 6.62-2.46l-3.23-2.51c-.9.6-2.05.96-3.39.96-2.6 0-4.81-1.76-5.6-4.12H3.06v2.6A10 10 0 0 0 12 22z"/><path fill="currentColor" d="M6.4 13.87a6 6 0 0 1 0-3.74V7.53H3.06a10 10 0 0 0 0 8.94l3.34-2.6z"/><path fill="currentColor" d="M12 5.88c1.47 0 2.79.51 3.83 1.5l2.87-2.87A10 10 0 0 0 12 2a10 10 0 0 0-8.94 5.53l3.34 2.6c.79-2.36 3-4.25 5.6-4.25z"/></svg>`;
  const SRC_WAREHOUSE_SVG = `<svg viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="5" rx="8" ry="2.5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M4 5v6c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5V5M4 11v6c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5v-6" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>`;
  const SRC_PAYROLL_SVG = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16 7H9c-1.7 0-3 1.3-3 3s1.3 3 3 3h2M8 17h7c1.7 0 3-1.3 3-3s-1.3-3-3-3h-2M12 4v3M12 17v3" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>`;
  function srcIconSvg(kind) {
    return (kind === "letme" || kind === "together" || kind === "gmail") ? SRC_GOOGLE_SVG
      : kind === "warehouse" ? SRC_WAREHOUSE_SVG
      : kind === "payroll"   ? SRC_PAYROLL_SVG
      : "";
  }
  function renderLinkedSourcesCard() {
    const accts = googleByPersonId[person.id] || [];
    const letme    = (accts.find(a => a.tenant === "letme")    || {}).email || "";
    const together = (accts.find(a => a.tenant === "together") || {}).email || "";
    const gmail    = (accts.find(a => a.tenant === "external") || {}).email || person.external_google_email || "";
    const wh       = warehouseByPersonId[person.id] || null;
    const warehouseHint = wh ? (wh.email || wh.username || `record #${wh.id}`) : "";
    const payrollLabel = person.on_payroll
      ? (person.most_recent_payroll_id ? `Payroll record #${person.most_recent_payroll_id}` : "Marked on payroll · no record yet")
      : "";

    function row(kind, label, value) {
      const present = !!value;
      return `
        <div class="up-src-row">
          <span class="pp-src-chip pp-src-chip--${kind} ${present ? "on" : "off"}" title="${escapeHtml(label)}">${srcIconSvg(kind)}</span>
          <span class="up-src-label">${escapeHtml(label)}</span>
          <span class="up-src-value">${present ? escapeHtml(value) : '<span class="up-empty-val">not linked</span>'}</span>
        </div>`;
    }
    return `
      <div class="up-card">
        <div class="up-card-head">Linked sources <span class="up-card-hint">one record per source · manage in <a href="?tab=accounts" style="color:inherit;text-decoration:underline;">Accounts</a> · <a href="?tab=payroll" style="color:inherit;text-decoration:underline;">Payroll</a></span></div>
        <div class="up-src-list">
          ${row("letme",     "Google Letme",    letme)}
          ${row("together",  "Google Together", together)}
          ${row("gmail",     "External Gmail",  gmail)}
          ${row("warehouse", "Warehouse CRM",   warehouseHint)}
          ${row("payroll",   "Payroll",         payrollLabel)}
        </div>
      </div>`;
  }

  function renderMergeCard() {
    // Skip self in the picker, sort by name.
    const opts = people
      .filter(p => p.id !== person.id)
      .sort((a, b) => (a.name || "").localeCompare(b.name || ""))
      .map(p => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name || p.id)} · ${escapeHtml(p.main_google_email || "(no email)")}</option>`)
      .join("");
    return `
      <div class="up-card">
        <div class="up-card-head">Merge this Person <span class="up-card-hint">use when two Person records are the same human (e.g. a payroll-only Person + a Google-only Person)</span></div>
        <p class="up-hint">Pick the OTHER Person below. This Person (<strong>${escapeHtml(person.name || person.url_slug)}</strong>) will be absorbed and deleted — every Google account, alias, and PayrollData row moves to the surviving record. The chosen Person keeps its <code>/directory/&lt;slug&gt;</code> URL.</p>
        <select class="up-pay-input" id="upMergeTarget"><option value="">— pick the surviving Person —</option>${opts}</select>
        <div class="up-editor-row">
          <button type="button" class="up-btn-sm up-btn-sm--primary" id="upMergeGo">Merge into selected</button>
          <span class="up-edit-status" data-edit-status="merge"></span>
        </div>
      </div>`;
  }

  /* ─── Accounts tab — 6 source boxes, collapsed by default ─────── */
  // Each linked-source row is its own <details> box: summary always
  // visible (label · value), body expands on click. Six fixed boxes:
  // Google Letme, Google Together, External Gmail, Warehouse CRM,
  // Payroll, Auth0 ID — placeholder shown when the source is unlinked
  // so the layout stays stable.
  function renderAccountsPanel() {
    return `
      <h2 class="up-panel-title">Accounts</h2>
      <div class="up-src-boxes">
        ${renderGoogleBox("letme",    "Google · Letme")}
        ${renderGoogleBox("together", "Google · Together")}
        ${renderGoogleBox("external", "External Gmail")}
        ${renderWarehouseBox()}
        ${renderPayrollBox()}
        ${renderAuth0Box()}
      </div>
      <div class="up-acct-add-form" id="upAcctAddForm" hidden></div>
    `;
  }

  function renderGoogleBox(tenant, label) {
    const acct = (googleByPersonId[person.id] || []).find(a => a.tenant === tenant && (a.is_primary || a.google_user_id || tenant === "external"));
    if (acct) return renderAccountRow(acct);
    // Unlinked placeholder.
    const addBtn = viewerIsAdmin
      ? `<button class="up-btn-sm" data-acc-add="${tenant}">+ Add ${escapeHtml(label.replace(/^Google · /, ""))}</button>`
      : "";
    return `
      <details class="up-src-box up-src-box--empty">
        <summary class="up-src-box-summary">
          <span class="up-src-box-label">${srcBoxLogo(tenant)}<span class="up-src-label-text">${escapeHtml(label)}</span></span>
          <span class="up-src-box-value"><span class="up-empty-val">not linked</span></span>
        </summary>
        <div class="up-src-box-body">
          <p class="up-hint">No ${escapeHtml(label)} account is linked to this Person.</p>
          ${addBtn}
        </div>
      </details>`;
  }

  function renderWarehouseBox() {
    const wh = warehouseByPersonId[person.id] || null;
    const summary = wh
      ? `<span>${escapeHtml(wh.email || wh.username || `record #${wh.id}`)}</span>${wh.last_active_utc ? ` <span class="up-card-hint">· last active ${escapeHtml(wh.last_active_utc.slice(0, 10))}</span>` : ""}`
      : `<span class="up-empty-val">not linked</span>`;
    const body = wh ? `
      <div class="up-fields-grid">
        ${wh.id        ? `<div class="up-field"><div class="up-field-label">Record ID</div><div class="up-field-value">#${escapeHtml(wh.id)}</div></div>` : ""}
        ${wh.email     ? `<div class="up-field"><div class="up-field-label">Email</div><div class="up-field-value">${escapeHtml(wh.email)}</div></div>` : ""}
        ${wh.username  ? `<div class="up-field"><div class="up-field-label">Username</div><div class="up-field-value">${escapeHtml(wh.username)}</div></div>` : ""}
        ${wh.last_active_utc ? `<div class="up-field"><div class="up-field-label">Last active (UTC)</div><div class="up-field-value">${escapeHtml(wh.last_active_utc)}</div></div>` : ""}
      </div>
      <p class="up-hint">Warehouse CRM identity — sourced from the nightly Fabric mirror. Read-only here.</p>
    ` : `<p class="up-hint">No Warehouse CRM record matched to this Person yet. Matching is by email — verify the linked Google addresses cover what's in the warehouse.</p>`;
    return `
      <details class="up-src-box up-src-box--warehouse${wh ? "" : " up-src-box--empty"}">
        <summary class="up-src-box-summary">
          <span class="up-src-box-label">Warehouse CRM</span>
          <span class="up-src-box-value">${summary}</span>
        </summary>
        <div class="up-src-box-body">${body}</div>
      </details>`;
  }

  function renderPayrollBox() {
    const onPayroll = !!person.on_payroll;
    let rec = payrollByPersonId[person.id] || (person.most_recent_payroll_id ? payrollRecordsById[person.most_recent_payroll_id] : null);
    // Local-cache overlay so the user's last edit appears immediately even
    // if the GitHub Pages refresh hasn't propagated yet.
    const lsRec = LS.get(person.id, "payroll");
    if (lsRec && lsRec.v) rec = { ...(rec || {}), ...lsRec.v };

    const recId = (rec && rec.id) || person.most_recent_payroll_id || "";
    const summary = onPayroll
      ? `<span>Payroll record ${recId ? `#${escapeHtml(String(recId))}` : "(no record yet)"}</span>`
      : `<span class="up-empty-val">not on payroll</span>`;

    // Provenance/refresh wording is verbatim from how the system actually
    // works today (per worker/workspace-worker.js:1970-1988 and the
    // start_date propagation in savePersonField/savePersonCard).
    const provenance = `
      <p class="up-hint up-pay-provenance">
        Payroll data lives in <code>payroll-data.json</code> in the site repo — one canonical row per Person, linked from <code>Person.most_recent_payroll_id</code>. Edits below write straight to that row via the workspace worker; there is no scheduled external refresh today, so manual edits are not overwritten. When the planned bulk <code>payroll-import</code> (not yet built) ships, fresh imports will <em>append</em> a new row and re-point <code>most_recent_payroll_id</code> — your previous hand-edits stay preserved on the older row in history. <strong>Start date</strong> is mirrored to <code>Person.start_date</code>, so editing it here updates the Info tab's Start date too; the rest of the fields are payroll-only and don't appear elsewhere on the Person page.
      </p>`;

    if (!onPayroll) {
      const offToggle = viewerIsAdmin ? `
        <div class="up-editor-row">
          <button type="button" class="up-btn-sm up-btn-sm--primary" data-payroll-toggle="on">Mark as ON payroll</button>
          <span class="up-edit-status" data-edit-status="on_payroll"></span>
        </div>` : "";
      return `
        <details class="up-src-box up-src-box--payroll up-src-box--empty">
          <summary class="up-src-box-summary">
            <span class="up-src-box-label">Payroll</span>
            <span class="up-src-box-value">${summary}</span>
          </summary>
          <div class="up-src-box-body">
            ${provenance}
            <p class="up-hint">This Person isn't on payroll. Marking them on creates a blank row in <code>payroll-data.json</code>; you can then fill it in.</p>
            ${offToggle}
          </div>
        </details>`;
    }

    // On-payroll path: render every payroll field as text by default;
    // Edit (admin-only) swaps the read block for the same form that the
    // old Payroll tab used.
    const r = rec || { employer:"", employee_number:"", first_name:person.given||"", last_name:person.family||"", email:person.main_google_email||"", start_date:"", termination_date:"", mobile:person.phone||"", address:person.address||"", annual_salary:null, monthly_pay:null, tax_code:"", ni_number:"", bank_sort_code:"", bank_account_last4:"", notes:"" };
    const readHtml = `<div class="up-fields-grid">${payrollReadOnlyHtml(r)}</div>`;
    const editHtml = viewerIsAdmin ? `
      <div class="up-pay-edit" hidden>
        ${payrollEditorHtml(r)}
        <div class="up-editor-row">
          <button type="button" class="up-btn-sm up-btn-sm--primary" data-payroll-save>Save</button>
          <button type="button" class="up-btn-sm" data-payroll-edit-cancel>Cancel</button>
          <span class="up-edit-status" data-edit-status="payroll"></span>
        </div>
      </div>` : "";
    const editToggle = viewerIsAdmin ? `
      <button type="button" class="up-link-btn up-card-edit-toggle" data-payroll-edit>Edit</button>` : "";
    const onToggle = viewerIsAdmin ? `
      <div class="up-editor-row">
        <button type="button" class="up-btn-sm" data-payroll-toggle="off">Mark as NOT on payroll</button>
        <span class="up-card-hint">Doesn't delete the row — just unflags the Person.</span>
        <span class="up-edit-status" data-edit-status="on_payroll"></span>
      </div>` : "";
    return `
      <details class="up-src-box up-src-box--payroll">
        <summary class="up-src-box-summary">
          <span class="up-src-box-label">Payroll</span>
          <span class="up-src-box-value">${summary}</span>
        </summary>
        <div class="up-src-box-body">
          ${provenance}
          <div class="up-pay-toolbar">
            <span class="up-card-hint">${recId ? `Record <code>#${escapeHtml(String(recId))}</code>` : "No record yet — save any change to create one"}</span>
            ${editToggle}
          </div>
          <div class="up-pay-read">${readHtml}</div>
          ${editHtml}
          <hr class="up-pay-divider">
          ${onToggle}
        </div>
      </details>`;
  }

  function renderAuth0Box() {
    const id = person.auth0_id || "";
    const summary = id
      ? `<span><code>${escapeHtml(id)}</code></span>`
      : `<span class="up-empty-val">not set</span>`;
    const editor = viewerIsAdmin ? `
      <div class="up-field" data-edit-field="auth0_id">
        <div class="up-field-editor up-field-editor--open">
          <input type="text" name="auth0_id" value="${escapeHtml(id)}" placeholder="auth0|abc123…">
          <div class="up-editor-row">
            <button type="button" class="up-btn-sm up-btn-sm--primary" data-edit-save="auth0_id">Save</button>
            <span class="up-edit-status" data-edit-status="auth0_id"></span>
          </div>
        </div>
      </div>` : `<p class="up-hint">${id ? `<code>${escapeHtml(id)}</code>` : "Not set."} Admin-only to edit.</p>`;
    return `
      <details class="up-src-box up-src-box--auth0${id ? "" : " up-src-box--empty"}">
        <summary class="up-src-box-summary">
          <span class="up-src-box-label">Auth0 ID</span>
          <span class="up-src-box-value">${summary}</span>
        </summary>
        <div class="up-src-box-body">
          <p class="up-hint">Auth0 grants access to CRM / Admin Site / Reporting / VVC without consuming a Workspace seat. Useful for outsiders.</p>
          ${editor}
        </div>
      </details>`;
  }

  /* ─── Payroll tab — most-recent PayrollData record + editor ────── */
  function renderPayrollPanel() {
    const onPayroll = !!person.on_payroll;
    let rec = payrollByPersonId[person.id] || (person.most_recent_payroll_id ? payrollRecordsById[person.most_recent_payroll_id] : null);
    // Overlay any localStorage'd payroll edit for this Person so the
    // user's own session always shows their most recent save, even
    // if the server hasn't propagated.
    const lsRec = LS.get(person.id, "payroll");
    if (lsRec && lsRec.v) rec = { ...(rec || {}), ...lsRec.v };

    if (!viewerIsAdmin) {
      if (!onPayroll) return `<h2 class="up-panel-title">Payroll</h2><div class="up-empty">This person is not on payroll.</div>`;
      if (!rec)       return `<h2 class="up-panel-title">Payroll</h2><div class="up-empty">No payroll record yet — ask an admin to fill it in.</div>`;
      return `
        <h2 class="up-panel-title">Payroll</h2>
        <div class="up-card">
          <div class="up-card-head">Most recent payroll record <span class="up-card-hint">read-only · admins can edit</span></div>
          <div class="up-fields-grid">${payrollReadOnlyHtml(rec)}</div>
        </div>`;
    }

    // Admin view: on_payroll toggle + editable fields (or "create blank" when on_payroll=true but no record).
    const toggleBtn = `
      <button type="button" class="up-btn-sm ${onPayroll ? "" : "up-btn-sm--primary"}" data-payroll-toggle="${onPayroll ? "off" : "on"}">
        ${onPayroll ? "Mark as NOT on payroll" : "Mark as ON payroll"}
      </button>`;

    if (!onPayroll) {
      return `
        <h2 class="up-panel-title">Payroll</h2>
        <div class="up-card">
          <div class="up-card-head">On payroll? <span class="up-pill up-pill--suspended">No</span></div>
          <p class="up-hint">This person isn't currently flagged as on payroll. Mark them on payroll to start a record — a blank line will be created in PayrollData and you can fill in the fields.</p>
          <div class="up-editor-row">${toggleBtn}<span class="up-edit-status" data-edit-status="on_payroll"></span></div>
        </div>`;
    }

    // on_payroll = true. If no record yet, the worker creates one on next edit.
    const blank = !rec;
    const r = rec || { employer:"", employee_number:"", first_name:person.given||"", last_name:person.family||"", email:person.main_google_email||"", start_date:"", termination_date:"", mobile:person.phone||"", address:person.address||"", annual_salary:null, monthly_pay:null, tax_code:"", ni_number:"", bank_sort_code:"", bank_account_last4:"", notes:"" };
    return `
      <h2 class="up-panel-title">Payroll</h2>
      <div class="up-card">
        <div class="up-card-head">Most recent payroll record <span class="up-card-hint">${blank ? "no record yet — save any change to create one" : `record id <code>${escapeHtml(r.id || person.most_recent_payroll_id || "")}</code>`}</span></div>
        ${payrollEditorHtml(r)}
        <div class="up-editor-row">
          <button type="button" class="up-btn-sm up-btn-sm--primary" data-payroll-save>Save</button>
          <span class="up-edit-status" data-edit-status="payroll"></span>
        </div>
      </div>
      <div class="up-card">
        <div class="up-card-head">On payroll? <span class="up-pill up-pill--live">Yes</span></div>
        <p class="up-hint">Turning this off does NOT delete the existing PayrollData record — it just stops showing the editor here and unmarks the Person from payroll views.</p>
        <div class="up-editor-row">${toggleBtn}<span class="up-edit-status" data-edit-status="on_payroll"></span></div>
      </div>`;
  }

  function payrollFields() {
    return [
      ["employer",            "Employer"],
      ["employee_number",     "Employee number"],
      ["first_name",          "First name"],
      ["last_name",           "Last name"],
      ["email",               "Payroll email"],
      ["start_date",          "Start date"],
      ["termination_date",    "Termination date"],
      ["mobile",              "Mobile"],
      ["address",             "Address"],
      ["annual_salary",       "Annual salary"],
      ["monthly_pay",         "Monthly pay"],
      ["tax_code",            "Tax code"],
      ["ni_number",           "NI number"],
      ["bank_sort_code",      "Bank sort code"],
      ["bank_account_last4",  "Bank account (last 4)"],
      ["notes",               "Notes"],
    ];
  }
  function payrollReadOnlyHtml(r) {
    return payrollFields().map(([k, label]) => {
      const v = r[k];
      const display = v == null || v === "" ? '<span class="up-empty-val">—</span>' : escapeHtml(String(v));
      return `<div class="up-field"><div class="up-field-label">${escapeHtml(label)}</div><div class="up-field-value">${display}</div></div>`;
    }).join("");
  }
  function payrollEditorHtml(r) {
    const rows = payrollFields().map(([k, label]) => {
      const inputType = /date/i.test(k) ? "date" : (/salary|pay/.test(k) ? "number" : "text");
      const val = (r && r[k] != null) ? r[k] : "";
      // Address shows 5 lines so a full UK address fits without
      // scrolling; notes stays compact at 3.
      const rowCount = k === "address" ? 5 : (k === "notes" ? 3 : null);
      const field = rowCount
        ? `<textarea class="up-pay-input" name="${k}" rows="${rowCount}">${escapeHtml(val)}</textarea>`
        : `<input class="up-pay-input" type="${inputType}" name="${k}" value="${escapeHtml(val)}">`;
      return `<div class="up-pay-row"><label class="up-field-label">${escapeHtml(label)}</label>${field}</div>`;
    }).join("");
    return `<div class="up-pay-grid">${rows}</div>`;
  }

  /* ─── Google accounts section (inline per-account actions) ─────── */
  function tenantFor(email) {
    const domain = ((email || "").split("@")[1] || "").toLowerCase();
    if (domain === "togetherloans.com") return "togetherloans";
    return "";
  }
  function accountState(rec) {
    // rec is a google-accounts.json row — has its own suspended /
    // deletion_time / aliases / etc. Pending-transfer + forwarding
    // state still come from outside (pending-transfers.json +
    // annotations.json) since they aren't on the row yet.
    const e = (rec.email || "").toLowerCase();
    const pending = pendingTransfersByEmail[e] || null;
    const fwd = (annotationsMap[e] || {}).forward_to || "";
    return {
      exists:        rec.tenant !== "external",
      suspended:     !!rec.suspended,
      deletion_time: rec.deletion_time || "",
      forwarding_to: fwd,
      pending,
      admin:         adminEmails.has(e),
    };
  }
  function renderGoogleAccountsSection() {
    // Filter out alias-only rows: when a secondary domain is attached to
    // a Workspace tenant, the directory scan picks up an "account" at the
    // alias address that resolves to the same underlying Google user as
    // the primary. Such rows have no google_user_id and is_primary=false,
    // and the actions on them (Delete / Transfer / Make primary) all
    // fail on Google's side because there's nothing distinct to act on.
    // The alias is already shown as a chip under the primary row.
    // External Gmails legitimately have no google_user_id, so spare them.
    const accts = (googleByPersonId[person.id] || [])
      .filter(a => a.is_primary || a.google_user_id || a.tenant === "external")
      .sort((a, b) => {
        // primary first, then letme, then together, then external.
        if (a.is_primary !== b.is_primary) return a.is_primary ? -1 : 1;
        const order = { letme: 0, together: 1, external: 2 };
        return (order[a.tenant] ?? 9) - (order[b.tenant] ?? 9);
      });
    const haveLetme    = accts.some(a => a.tenant === "letme");
    const haveTogether = accts.some(a => a.tenant === "together");
    const haveExternal = accts.some(a => a.tenant === "external");

    const rows = accts.length
      ? accts.map(a => renderAccountRow(a)).join("")
      : `<div class="up-empty">No Google accounts linked yet.</div>`;
    const addButtons = !viewerIsAdmin ? "" : `
      <div class="up-acct-add-row">
        ${haveLetme    ? "" : `<button class="up-btn-sm" data-acc-add="letme">+ Add Letme Google</button>`}
        ${haveTogether ? "" : `<button class="up-btn-sm" data-acc-add="together">+ Add Together Google</button>`}
        ${haveExternal ? "" : `<button class="up-btn-sm" data-acc-add="external">+ Add external Gmail (login alternative)</button>`}
      </div>`;
    return `
      <div class="up-card">
        <div class="up-card-head">Google accounts
          ${viewerIsAdmin ? '<span class="up-card-hint">one row per linked account · niche flows in <a href="/directory-legacy.html" style="color:inherit;text-decoration:underline;">legacy Directory</a></span>' : ""}
        </div>
        ${rows}
        ${addButtons}
        <div class="up-acct-add-form" id="upAcctAddForm" hidden></div>
      </div>`;
  }
  function renderAccountRow(rec) {
    const st = accountState(rec);
    const email = rec.email;
    const aliases = (rec.aliases || []).filter(a => a !== email);
    // Aliases auto-generated by Workspace (secondary domains) live in
    // nonEditableAliases on the Admin SDK response and can't be removed
    // via API — only by removing the whole secondary domain. The scanner
    // splits them into aliases_editable; if that field is missing (older
    // data), treat all as editable so we don't break legacy rows.
    const editableSet = rec.aliases_editable
      ? new Set(rec.aliases_editable)
      : null;
    const isEditableAlias = (a) => editableSet ? editableSet.has(a) : true;
    const isMine = (viewerEmail === email.toLowerCase());

    // Editable alias chips. Each chip has a × to remove the alias and a
    // small "→ group" link that promotes the alias to a Workspace Group
    // at the same address (alias is freed → group created with the
    // user as initial member). Admin-only; the section is read-only
    // for non-admins. The "+ Add alias" button opens an inline form
    // wired by handleAddAlias(). Non-editable aliases (Workspace
    // secondary-domain auto-aliases) render as a plain chip with a
    // tooltip explaining why no actions are offered.
    const aliasChips = aliases.map(a => {
      const editable = isEditableAlias(a);
      const chipCls = editable ? "up-acct-alias-chip" : "up-acct-alias-chip up-acct-alias-chip--locked";
      const chipTitle = editable ? "" : ` title="Auto-created by Workspace because this is a secondary domain on the tenant. Remove the domain in admin.google.com to clear all aliases on it; individual removal isn't possible."`;
      const actions = (viewerIsAdmin && rec.tenant !== "external" && editable) ? `
          <button class="up-acct-alias-x" title="Remove alias" data-acc-alias-remove="${escapeHtml(a)}">×</button>
          <button class="up-acct-alias-group" title="Convert this alias into a forwarding Group at the same address" data-acc-alias-group="${escapeHtml(a)}">→ group</button>
        ` : "";
      return `
      <span class="${chipCls}"${chipTitle}>
        <span class="up-acct-alias-email">${escapeHtml(a)}</span>
        ${actions}
      </span>`;
    }).join("");
    // "+ Add alias" used to live inline at the end of the chip list, which
    // was unfindable on accounts with many chips. It's now the first
    // button in the action row below so it sits with the other
    // account-level actions.
    const aliasBlock = aliases.length ? `
      <div class="up-acct-aliases">
        <span class="up-acct-aliases-label">Aliases:</span>
        ${aliasChips}
      </div>` : "";

    // Status / state badges only — tenant identity is conveyed by the
    // coloured Google / Gmail logo on the left of the summary, so the
    // old "LETME" / "TOGETHER" / "EXTERNAL" pill is gone.
    let badges = [];
    if (st.deletion_time)                      badges.push(`<span class="up-acct-badge up-acct-badge--deleted">Deleted</span>`);
    else if (st.pending)                       badges.push(`<span class="up-acct-badge up-acct-badge--pending">Transferring</span>`);
    else if (st.suspended)                     badges.push(`<span class="up-acct-badge up-acct-badge--suspended">Suspended</span>`);
    else                                       badges.push(`<span class="up-acct-badge up-acct-badge--live">Live</span>`);
    if (rec.is_primary)                        badges.push(`<span class="up-acct-badge">Primary</span>`);
    if (st.admin)                              badges.push(`<span class="up-acct-badge up-acct-badge--admin">Workspace admin</span>`);
    if (st.forwarding_to)                      badges.push(`<span class="up-acct-badge up-acct-badge--forward">→ ${escapeHtml(st.forwarding_to)}</span>`);

    const actions = (viewerIsAdmin && rec.tenant !== "external") ? renderAccountButtons(email, st, isMine, rec) : "";
    const adminUnlink = viewerIsAdmin
      ? `<a href="#" class="up-acct-unlink-link" data-acc-unlink="${escapeHtml(rec.id)}" title="Remove this Google account row from the Person (does not touch the Workspace account itself)">Unlink</a>`
      : "";

    return `
      <details class="up-src-box up-acct" data-acc-email="${escapeHtml(email)}" data-acc-id="${escapeHtml(rec.id)}" data-acc-is-primary="${rec.is_primary ? "1" : "0"}">
        <summary class="up-src-box-summary">
          <span class="up-src-box-label">${srcBoxLogo(rec.tenant)}<span class="up-src-label-text">${escapeHtml(srcBoxLabel(rec.tenant))}</span></span>
          <span class="up-src-box-value">
            <span class="up-acct-email">${escapeHtml(email)}</span>
            <span class="up-acct-badges">${badges.join("")}</span>
          </span>
          ${adminUnlink}
        </summary>
        <div class="up-src-box-body">
          ${aliasBlock}
          ${actions}
          <div class="up-acct-form" hidden></div>
        </div>
      </details>`;
  }

  // Brand-coloured logos used in the per-source box labels. Inline SVG
  // so they stay crisp at any DPI without an extra network request.
  const GOOGLE_WORKSPACE_LOGO = `<svg class="up-src-logo" viewBox="0 0 48 48" aria-hidden="true">
    <path fill="#FFC107" d="M43.611 20.083H42V20H24v8h11.303c-1.649 4.657-6.08 8-11.303 8-6.627 0-12-5.373-12-12s5.373-12 12-12c3.059 0 5.842 1.154 7.961 3.039l5.657-5.657C34.046 6.053 29.268 4 24 4 12.955 4 4 12.955 4 24s8.955 20 20 20 20-8.955 20-20c0-1.341-.138-2.65-.389-3.917z"/>
    <path fill="#FF3D00" d="M6.306 14.691l6.571 4.819C14.655 15.108 18.961 12 24 12c3.059 0 5.842 1.154 7.961 3.039l5.657-5.657C34.046 6.053 29.268 4 24 4 16.318 4 9.656 8.337 6.306 14.691z"/>
    <path fill="#4CAF50" d="M24 44c5.166 0 9.86-1.977 13.409-5.192l-6.19-5.238A11.91 11.91 0 0 1 24 36c-5.202 0-9.619-3.317-11.283-7.946l-6.522 5.025C9.505 39.556 16.227 44 24 44z"/>
    <path fill="#1976D2" d="M43.611 20.083H42V20H24v8h11.303a12.04 12.04 0 0 1-4.087 5.571l.003-.002 6.19 5.238C36.971 39.205 44 34 44 24c0-1.341-.138-2.65-.389-3.917z"/>
  </svg>`;
  const GMAIL_LOGO = `<svg class="up-src-logo" viewBox="0 0 48 48" aria-hidden="true">
    <path fill="#4CAF50" d="M45 16.2l-5 2.75-5 4.75V40h7a3 3 0 0 0 3-3V16.2z"/>
    <path fill="#1E88E5" d="M3 16.2l3.614 1.71L13 19.75V40H6a3 3 0 0 1-3-3V16.2z"/>
    <polygon fill="#E53935" points="35,11.2 24,19.45 13,11.2 12,17 13,22.75 24,31 35,22.75 36,17"/>
    <path fill="#C62828" d="M3 12.298V16.2l10 7.55V11.2L9.876 8.859A4.298 4.298 0 0 0 3 12.298z"/>
    <path fill="#FBC02D" d="M45 12.298V16.2l-10 7.55V11.2l3.124-2.341A4.298 4.298 0 0 1 45 12.298z"/>
  </svg>`;
  function srcBoxLogo(tenant) {
    if (tenant === "letme" || tenant === "together") return GOOGLE_WORKSPACE_LOGO;
    if (tenant === "external") return GMAIL_LOGO;
    return "";
  }
  function srcBoxLabel(tenant) {
    if (tenant === "letme")    return "Google · Letme";
    if (tenant === "together") return "Google · Together";
    if (tenant === "external") return "External Gmail";
    return "Google";
  }
  function renderAccountButtons(email, st, isMine, rec) {
    // Each entry is [button-html, one-line rationale]. The rationale
    // renders inline on the page so the admin doesn't have to guess
    // what a button does. Compound "do-this-AND-that" buttons have
    // been split into their atomic parts — chain them yourself if you
    // want both effects.
    const items = [];
    if (st.deletion_time) {
      items.push([
        `<button data-acc-action="recover">Recover</button>`,
        "Restore this user before Google's 20-day cleanup permanently wipes the data.",
      ]);
      return renderActionList(items);
    }
    items.push([
      `<button data-acc-alias-add="1">+ Add alias</button>`,
      "Make another email address also deliver to this same mailbox.",
    ]);
    if (st.pending) {
      items.push([
        `<button disabled>Transferring…</button>`,
        "Drive + Mail migration in flight — wait for completion before further changes.",
      ]);
      return renderActionList(items);
    }
    if (st.suspended) {
      items.push([
        `<button data-acc-action="unsuspend" class="up-acct-btn-primary">Unsuspend</button>`,
        "Re-enable sign-in. Mail resumes normal delivery. The seat goes back to £11/month.",
      ]);
      if (st.forwarding_to) {
        items.push([
          `<button data-acc-action="cancel-forwarding">Cancel forwarding</button>`,
          "Stop forwarding inbound mail. Future mail lands in this (suspended) mailbox — effectively a black hole until you unsuspend.",
        ]);
      }
      items.push([
        `<button data-acc-action="delete-now" class="danger">Delete account</button>`,
        "Permanently remove. Google keeps the data for 20 days (recoverable) then wipes it. Seat charge stops at next billing tick.",
      ]);
      return renderActionList(items);
    }
    // Live (normal) account
    if (st.forwarding_to) {
      items.push([
        `<button data-acc-action="disable-forwarding">Turn off forwarding</button>`,
        `Stop forwarding to ${escapeHtml(st.forwarding_to)}. Inbound mail returns to this inbox.`,
      ]);
    } else {
      items.push([
        `<button data-acc-action="forward">Add forwarding</button>`,
        "Copy every incoming mail to a colleague. This inbox keeps receiving too — useful for handovers, audit visibility, or holiday cover.",
      ]);
    }
    if (!isMine) {
      items.push([
        `<button data-acc-action="suspend-now">Suspend</button>`,
        "Block sign-in. Mail still queues in the mailbox (nothing is lost) and the £11/month seat keeps billing. Reversible. Pair with Add forwarding if you also want covers.",
      ]);
    }
    items.push([
      `<button data-acc-action="reset-password">Reset password</button>`,
      "Generate a new password for the user. Use for lockouts or suspected account compromise. The new password is shown once — copy it then.",
    ]);
    if (rec && !rec.is_primary && !isMine) {
      items.push([
        `<button data-acc-action="promote-primary">Make primary</button>`,
        "Rename in Workspace so this becomes the user's main email. The old primary becomes a forwarding alias for ~21 days, then expires.",
      ]);
    }
    if (!isMine) {
      items.push([
        `<button data-acc-action="convert-to-group">Convert to group</button>`,
        "Replace this single mailbox with a Workspace Group at the same address. Mail then fans out to whoever you add as members. The original mailbox + Drive are deleted.",
      ]);
    }
    if (!isMine) {
      items.push([
        `<button data-acc-action="transfer-drive">Transfer Drive ownership</button>`,
        "Move ownership of every Drive file the user owns to a colleague. The user's account itself isn't touched — do this BEFORE Delete account if you want the documents preserved.",
      ]);
    }
    if (!isMine) {
      items.push([
        `<button data-acc-action="delete-now" class="danger">Delete account</button>`,
        "Permanently remove the user. Stops the £11/month seat charge immediately. Drive + Gmail are kept by Google for 20 days then wiped — do Transfer Drive first if you want to preserve files.",
      ]);
    }
    return renderActionList(items);
  }

  function renderActionList(items) {
    return `<div class="up-acct-actions">${items.map(([btn, why]) => `
      <div class="up-acct-action-row">
        <div class="up-acct-action-btn">${btn}</div>
        <p class="up-acct-action-rationale">${why}</p>
      </div>`).join("")}</div>`;
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
    // Per-field Edit/Save/Cancel still used by the Auth0 ID + external-Gmail
    // fields in Other identities (one field per editor, no card-level batch).
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
    // Card-level batch edit on the Editable details card. One Edit at the
    // top flips every row's editor on; one Save at the bottom submits all
    // changed fields as a single people-set call.
    document.querySelectorAll("[data-card-edit]").forEach(btn => {
      btn.addEventListener("click", () => {
        const card = btn.closest("[data-edit-card]");
        if (!card) return;
        card.querySelectorAll(".up-field-display").forEach(el => el.hidden = true);
        card.querySelectorAll(".up-field-editor").forEach(el => el.hidden = false);
        const footer = card.querySelector(".up-card-edit-footer");
        if (footer) footer.hidden = false;
        btn.hidden = true;
        const first = card.querySelector(".up-field-editor input, .up-field-editor textarea, .up-field-editor select");
        if (first) { first.focus(); first.select && first.select(); }
      });
    });
    document.querySelectorAll("[data-card-cancel]").forEach(btn => {
      btn.addEventListener("click", () => {
        const card = btn.closest("[data-edit-card]");
        if (!card) return;
        // Revert each input back to its data-orig value so the user
        // sees their unsaved typing thrown away.
        card.querySelectorAll(".up-field-editor input, .up-field-editor textarea, .up-field-editor select").forEach(inp => {
          inp.value = inp.dataset.orig != null ? inp.dataset.orig : inp.defaultValue;
        });
        card.querySelectorAll(".up-field-editor").forEach(el => el.hidden = true);
        card.querySelectorAll(".up-field-display").forEach(el => el.hidden = false);
        const footer = card.querySelector(".up-card-edit-footer");
        if (footer) footer.hidden = true;
        const toggle = card.querySelector("[data-card-edit]");
        if (toggle) toggle.hidden = false;
        const status = card.querySelector("[data-card-status]");
        if (status) { status.textContent = ""; status.className = "up-edit-status"; }
      });
    });
    document.querySelectorAll("[data-card-save]").forEach(btn => {
      btn.addEventListener("click", () => savePersonCard(btn.closest("[data-edit-card]")));
    });
    document.querySelectorAll("[data-acc-action]").forEach(btn => {
      btn.addEventListener("click", () => handleAccountAction(btn));
    });
    document.querySelectorAll("[data-acc-add]").forEach(btn => {
      btn.addEventListener("click", () => openAccountAdd(btn.dataset.accAdd));
    });
    document.querySelectorAll("[data-acc-unlink]").forEach(btn => {
      // Unlink button lives inside the <summary> — preventDefault keeps
      // the box from toggling open/closed when this is clicked.
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        unlinkGoogleAccount(btn.dataset.accUnlink);
      });
    });
    document.querySelectorAll("[data-acc-alias-add]").forEach(btn => {
      btn.addEventListener("click", () => handleAliasAction(btn, "add"));
    });
    document.querySelectorAll("[data-acc-alias-remove]").forEach(btn => {
      btn.addEventListener("click", () => handleAliasAction(btn, "remove"));
    });
    document.querySelectorAll("[data-acc-alias-group]").forEach(btn => {
      btn.addEventListener("click", () => handleAliasAction(btn, "to-group"));
    });
    // Payroll box edit toggle — swaps the read-only field grid for the
    // editable form already produced by payrollEditorHtml.
    document.querySelectorAll("[data-payroll-edit]").forEach(btn => {
      btn.addEventListener("click", () => {
        const box = btn.closest(".up-src-box--payroll");
        if (!box) return;
        box.querySelector(".up-pay-read").hidden = true;
        const ed = box.querySelector(".up-pay-edit");
        if (ed) ed.hidden = false;
        btn.hidden = true;
      });
    });
    document.querySelectorAll("[data-payroll-edit-cancel]").forEach(btn => {
      btn.addEventListener("click", () => {
        const box = btn.closest(".up-src-box--payroll");
        if (!box) return;
        box.querySelector(".up-pay-read").hidden = false;
        const ed = box.querySelector(".up-pay-edit");
        if (ed) ed.hidden = true;
        const toggle = box.querySelector("[data-payroll-edit]");
        if (toggle) toggle.hidden = false;
        const status = box.querySelector('[data-edit-status="payroll"]');
        if (status) { status.textContent = ""; status.className = "up-edit-status"; }
      });
    });
    // Wall-tab feed cards — outer wrapper is an <article>, not an <a>,
    // so nested links + the YouTube iframe render as valid HTML.
    // Clicks on the card itself (outside any inner <a>, <iframe>,
    // <button>) navigate to the post on the main Wall.
    document.querySelectorAll("[data-fp-href]").forEach(card => {
      card.addEventListener("click", (e) => {
        if (e.target.closest('a, iframe, button')) return;
        location.href = card.dataset.fpHref;
      });
      card.addEventListener("keydown", (e) => {
        if (e.target !== card) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          location.href = card.dataset.fpHref;
        }
      });
    });
    if (typeof hydrateFeedLinkPreviews === "function") hydrateFeedLinkPreviews();
    document.querySelectorAll("[data-payroll-toggle]").forEach(btn => {
      btn.addEventListener("click", () => togglePayroll(btn.dataset.payrollToggle === "on"));
    });
    // Admin controls — Suspend / Reactivate + Delete Person.
    document.querySelectorAll("[data-person-suspend]").forEach(btn => {
      btn.addEventListener("click", () => suspendPerson(btn.dataset.personSuspend === "suspend"));
    });
    document.querySelectorAll("[data-person-delete]").forEach(btn => {
      btn.addEventListener("click", () => deletePerson());
    });
    const saveBtn = document.querySelector("[data-payroll-save]");
    if (saveBtn) saveBtn.addEventListener("click", savePayrollEdits);
    const mergeBtn = document.getElementById("upMergeGo");
    if (mergeBtn) mergeBtn.addEventListener("click", runMerge);
  }

  async function runMerge() {
    const sel = document.getElementById("upMergeTarget");
    const status = document.querySelector('[data-edit-status="merge"]');
    const winnerId = sel && sel.value;
    if (!winnerId) {
      status.textContent = "Pick a Person first.";
      status.className = "up-edit-status up-edit-status--err";
      return;
    }
    const winner = people.find(p => String(p.id) === String(winnerId));
    if (!confirm(`Merge ${person.name || person.url_slug} INTO ${winner.name || winner.url_slug}?\n\n` +
                 `- ${person.name || person.url_slug} will be deleted.\n` +
                 `- Their Google account(s), aliases, payroll record(s), phone/address/etc. all move to ${winner.name || winner.url_slug}.\n` +
                 `- ${winner.name || winner.url_slug} keeps its /directory/${winner.url_slug} URL.\n\n` +
                 `This is permanent (well, recoverable from git history but no in-app undo).`)) return;
    status.textContent = "Merging…"; status.className = "up-edit-status up-edit-status--working";
    try {
      const res = await fetch(WORKSPACE_API + "/people-merge", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ winner_id: winnerId, loser_id: person.id  }),
      });
      const out = await res.json();
      if (!res.ok || !out.ok) throw new Error(out.error || `HTTP ${res.status}`);
      // Redirect to the surviving Person's page.
      location.href = "/directory/" + encodeURIComponent(winner.url_slug);
    } catch (err) {
      status.textContent = "Failed — " + err.message;
      status.className = "up-edit-status up-edit-status--err";
    }
  }

  async function togglePayroll(turnOn) {
    const status = document.querySelector('[data-edit-status="on_payroll"]');
    if (status) { status.textContent = "Saving…"; status.className = "up-edit-status up-edit-status--working"; }
    try {
      const res = await fetch(WORKSPACE_API + "/people-set", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: person.id, on_payroll: turnOn  }),
      });
      const out = await res.json();
      if (!res.ok || !out.ok) throw new Error(out.error || `HTTP ${res.status}`);
      Object.assign(person, out.person);
      LS.set(person.id, "on_payroll", turnOn);
      if (out.payroll_record) {
        payrollRecordsById[out.payroll_record.id] = out.payroll_record;
        payrollByPersonId[person.id] = out.payroll_record;
        LS.set(person.id, "most_recent_payroll_id", out.payroll_record.id);
      }
      renderPanel();
    } catch (err) {
      if (status) { status.textContent = "Failed — " + err.message; status.className = "up-edit-status up-edit-status--err"; }
    }
  }

  async function savePayrollEdits() {
    const status = document.querySelector('[data-edit-status="payroll"]');
    status.textContent = "Saving…"; status.className = "up-edit-status up-edit-status--working";
    const payload = { action: "payroll-set", person_id: person.id };
    document.querySelectorAll(".up-pay-input").forEach(inp => {
      payload[inp.name] = inp.value;
    });
    try {
      const res = await fetch(WORKSPACE_API + "/payroll-set", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const out = await res.json();
      if (!res.ok || !out.ok) throw new Error(out.error || `HTTP ${res.status}`);
      payrollRecordsById[out.record.id] = out.record;
      payrollByPersonId[person.id] = out.record;
      if (out.created) person.most_recent_payroll_id = out.record.id;
      // Write-through every payroll field that was just sent so the
      // editor reflects the saved state immediately even on a stale
      // refresh. Keyed under a synthetic "payroll" namespace per
      // Person so all the fields can be overlaid as a unit.
      LS.set(person.id, "payroll", out.record);
      const stamp = new Date();
      status.textContent = `Saved at ${String(stamp.getHours()).padStart(2,"0")}:${String(stamp.getMinutes()).padStart(2,"0")}:${String(stamp.getSeconds()).padStart(2,"0")}`;
      status.className = "up-edit-status up-edit-status--ok up-edit-status--persistent";
      setTimeout(() => renderPanel(), 350);
    } catch (err) {
      status.textContent = "Failed — " + err.message;
      status.className = "up-edit-status up-edit-status--err";
    }
  }

  /* ─── Account action handlers (call workspace worker inline) ───── */
  // List every linked Google account on every Person (from google-accounts.json
  // via googleByPersonId), plus any external Gmail tracked on the Person record
  // but not in google-accounts. Excludes only the address being acted on —
  // same-Person addresses must stay in the list because "forward my work
  // account → my other work account" is a routine consolidation flow.
  function colleagueDatalist(excludeEmail) {
    const exclude = (excludeEmail || "").toLowerCase();
    const tenantLabel = { letme: "Letme", together: "Together", external: "Gmail" };
    const rows = [];
    const seen = new Set();
    for (const p of people) {
      const name = p.name || p.id;
      const accts = googleByPersonId[p.id] || [];
      for (const a of accts) {
        const e = (a.email || "").toLowerCase();
        if (!e || e === exclude || seen.has(e)) continue;
        seen.add(e);
        const tag = tenantLabel[a.tenant] || a.tenant || "";
        rows.push({ email: a.email, name, label: tag ? `${name} · ${tag}` : name });
      }
      // External Gmail can live on the Person record without a google-accounts row.
      const ext = p.external_google_email;
      if (ext && !seen.has(ext.toLowerCase()) && ext.toLowerCase() !== exclude) {
        seen.add(ext.toLowerCase());
        rows.push({ email: ext, name, label: `${name} · Gmail` });
      }
    }
    rows.sort((a, b) => a.name.localeCompare(b.name) || a.email.localeCompare(b.email));
    return rows.map(r => `<option value="${escapeHtml(r.email)}">${escapeHtml(r.label)}</option>`).join("");
  }
  // Cryptographically-strong password generator for the Reset-password
  // action. 16 chars from a base62 + symbol alphabet, drawn from crypto
  // getRandomValues. Worker requires the caller to supply the password so
  // the page can show it to the admin exactly once at the same moment it
  // takes effect on Google's side.
  function generateAccountPassword() {
    const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*-_";
    const out = new Array(16);
    const buf = new Uint32Array(16);
    crypto.getRandomValues(buf);
    for (let i = 0; i < 16; i++) out[i] = alphabet[buf[i] % alphabet.length];
    return out.join("");
  }

  async function handleAccountAction(btn) {
    const card = btn.closest(".up-acct");
    const email = card && card.dataset.accEmail;
    if (!email) return;
    const action = btn.dataset.accAction;
    const form = card.querySelector(".up-acct-form");
    const t = tenantFor(email);
    // Resolve the underlying google-accounts row so handlers that need the
    // immutable google_user_id (Recover) can pull from it without re-fetching.
    const accId = card && card.dataset.accId;
    const rec = (googleByPersonId[person.id] || []).find(a => String(a.id) === String(accId)) || null;

    // Single-target prompt actions: forward / transfer-drive.
    const NEEDS_TARGET = new Set(["forward", "transfer-drive"]);
    if (NEEDS_TARGET.has(action)) {
      const labels = {
        "forward":          ["Forward mail to colleague",    "When mail arrives at this account, deliver it to:"],
        "transfer-drive":   ["Transfer Drive ownership",     "Move all this user's Drive files to:"],
      }[action];
      form.hidden = false;
      form.innerHTML = `
        <h4>${escapeHtml(labels[0])}</h4>
        <p class="up-hint">${escapeHtml(labels[1])}</p>
        <input type="email" list="upAccTargets" placeholder="colleague.email@…" data-acc-target>
        <datalist id="upAccTargets">${colleagueDatalist(email)}</datalist>
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

    // Two-target form: Delete + 21-day group conversion. Drive + Mail
    // go to the handover colleague now; the freed email becomes a
    // forwarding Group ~21 days later, delivering to the second
    // address. Implemented via the existing queue-transfer-and-delete
    // worker action with `convert_to_group_forward_to` set so the
    // background scanner finishes the conversion when Google's 20-day
    // reuse lockout expires.
    if (action === "delete-and-group") {
      form.hidden = false;
      form.innerHTML = `
        <h4>Delete &amp; replace with a forwarding group in 21 days</h4>
        <p class="up-hint">Drive files + Gmail migrate now to the handover colleague; the freed email becomes a forwarding Group ~21 days later, delivering to whoever you pick.</p>
        <label class="up-field-label" style="margin-top:6px;">Handover colleague (gets Drive + Mail now)</label>
        <input type="email" list="upAccTargets" placeholder="handover.colleague@…" data-acc-target>
        <label class="up-field-label" style="margin-top:6px;">Forward future mail to (the group's member)</label>
        <input type="email" list="upAccTargets" placeholder="forward.target@…" data-acc-forward>
        <datalist id="upAccTargets">${colleagueDatalist(email)}</datalist>
        <div class="up-editor-row">
          <button class="up-btn-sm up-btn-sm--primary" data-acc-confirm>Queue it</button>
          <button class="up-btn-sm" data-acc-cancel>Cancel</button>
          <span class="up-edit-status" data-acc-status></span>
        </div>`;
      form.querySelector("[data-acc-cancel]").addEventListener("click", () => { form.hidden = true; form.innerHTML = ""; });
      form.querySelector("[data-acc-confirm]").addEventListener("click", () => {
        const target  = (form.querySelector("[data-acc-target]").value  || "").trim().toLowerCase();
        const forward = (form.querySelector("[data-acc-forward]").value || "").trim().toLowerCase();
        const status  = form.querySelector("[data-acc-status]");
        if (!target.includes("@") || !forward.includes("@")) {
          status.textContent = "Both addresses are required";
          status.className = "up-edit-status up-edit-status--err";
          return;
        }
        runAccountAction(form, email, "delete-and-group", target, t, { convert_to_group_forward_to: forward });
      });
      form.querySelector("[data-acc-target]").focus();
      return;
    }

    // Convert primary to a forwarding Group. No Drive/Mail migration —
    // anything in the deleted mailbox is lost; the freed email becomes
    // a Group ~20 days later (Google's reuse lockout). The admin
    // picks a single colleague who receives the forwarded mail; more
    // members can be added in admin.google.com once the Group exists.
    if (action === "convert-to-group") {
      form.hidden = false;
      form.innerHTML = `
        <h4>Convert ${escapeHtml(email)} to a forwarding group</h4>
        <p class="up-hint">The user account is deleted; Google holds the address for 20 days, then a Group is auto-created at the same address with the colleague below as the first member. Add more members in admin.google.com afterwards.</p>
        <label class="up-field-label">Forward future mail to</label>
        <input type="email" list="upAccTargets" placeholder="colleague.email@…" data-acc-forward>
        <datalist id="upAccTargets">${colleagueDatalist(email)}</datalist>
        <div class="up-editor-row">
          <button class="up-btn-sm up-btn-sm--primary" data-acc-confirm>Convert</button>
          <button class="up-btn-sm" data-acc-cancel>Cancel</button>
          <span class="up-edit-status" data-acc-status></span>
        </div>`;
      form.querySelector("[data-acc-cancel]").addEventListener("click", () => { form.hidden = true; form.innerHTML = ""; });
      form.querySelector("[data-acc-confirm]").addEventListener("click", () => {
        const forward = (form.querySelector("[data-acc-forward]").value || "").trim().toLowerCase();
        const status  = form.querySelector("[data-acc-status]");
        if (!forward.includes("@")) { status.textContent = "Forward target required"; status.className = "up-edit-status up-edit-status--err"; return; }
        runAccountAction(form, email, "convert-to-group", null, t, { forward_to: forward });
      });
      form.querySelector("[data-acc-forward]").focus();
      return;
    }

    // Promote an alt account → primary via Google's rename-user. The
    // freed primary becomes a non-editable alias that forwards for
    // ~21 days, then expires (per Google's standard rename behaviour).
    if (action === "promote-primary") {
      const primaryRow = card.parentElement.querySelector('.up-acct[data-acc-is-primary="1"]');
      const currentPrimary = primaryRow && primaryRow.dataset.accEmail;
      if (!currentPrimary) { alert("No current primary account to swap with."); return; }
      if (!confirm(`Make ${email} the primary email?\n\nRenames the user from ${currentPrimary} to ${email}. Google auto-creates a non-editable alias at ${currentPrimary} that forwards to the same mailbox for ~21 days, then expires.`)) return;
      runAccountAction(null, currentPrimary, "promote-primary", email, t, { new_email: email });
      return;
    }

    // Instant actions — confirm and fire.
    const CONFIRM = {
      "suspend-now":        { msg: `Suspend ${email}?\n\nUser can't sign in but mail keeps queuing in the mailbox (nothing is lost). The seat still bills £11/month while suspended. Reversible at any time.` },
      "unsuspend":          { msg: `Unsuspend ${email}? Mail will resume delivery and the seat goes back to £11/month.` },
      "cancel-forwarding":  { msg: `Stop forwarding mail from ${email}? Future mail will land in the suspended account's inbox (effectively a black hole).` },
      "disable-forwarding": { msg: `Turn off mail forwarding on ${email}? Mail will land in this account's inbox again.` },
      "delete-now":         { msg: `DELETE ${email} now?\n\nThis is permanent after 20 days. Run Transfer Drive ownership first if the account has files worth keeping.` },
      "reset-password":     { msg: `Reset password for ${email}?\n\nA new password will be generated and shown — copy it now, it's only visible once.` },
      "recover":            { msg: `Recover ${email}?\n\nRestores the deleted account to live, billed £11/month from today.` },
    }[action];
    if (CONFIRM && !confirm(CONFIRM.msg)) return;
    const extra = action === "recover" ? { user_id: rec && rec.google_user_id || "" } : undefined;
    runAccountAction(null, email, action, null, t, extra);
  }

  async function runAccountAction(form, email, action, target, tenant, extra) {
    const status = form && form.querySelector("[data-acc-status]");
    if (status) { status.textContent = "Working…"; status.className = "up-edit-status up-edit-status--working"; }
    // Action → [worker route, body]. `extra` lets callers slot extra
    // fields onto the body without rewriting the map.
    // reset-password is the only action that needs a value plumbed through
    // the map; compute it before the map literal so it's in scope when
    // the args object is constructed.
    const generatedPw = action === "reset-password" ? generateAccountPassword() : "";
    const map = {
      "forward":            ["add-forwarding",     { email, route_to: target }],
      "cancel-forwarding":  ["cancel-forwarding",  { email }],
      "disable-forwarding": ["disable-forwarding", { email }],
      "suspend-now":        ["suspend-no-forward", { email }],
      "unsuspend":          ["unsuspend",          { email }],
      "delete-now":         ["delete-account",     { email }],
      "transfer-drive":     ["data-transfer",      { email, target_email: target }],
      // reset-password: worker installs `password` on the account; the page
      // shows the same string to the admin via the `new_password` field on
      // the success response (see post-success branch below).
      "reset-password":     ["reset-password",     { email, password: generatedPw }],
      // recover needs the immutable Google id (email may have been recycled
      // during the 20-day window). The caller passes it via extra from
      // handleAccountAction, which already has the google-accounts row in
      // scope.
      "recover":            ["recover",            { email }],
      // Convert primary email to a forwarding Group. `forward_to`
      // arrives via `extra` from the convert-to-group form below.
      "convert-to-group":   ["convert-to-group",   { email }],
      // Promote alt → primary. email = current primary; target =
      // new primary (the alt we're promoting).
      "promote-primary":    ["rename-user",        { current_email: email, new_email: target }],
    };
    const [act, args] = map[action] || [];
    if (!act) return;
    const body = { tenant, ...args, ...(extra || {}) };
    try {
      const res = await fetch(WORKSPACE_API + "/" + act, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const out = await res.json();
      if (!res.ok || !out.ok) throw new Error(out.error || `HTTP ${res.status}`);
      if (act === "reset-password") {
        const shown = out.new_password || generatedPw;
        if (shown) alert(`Password for ${email}:\n\n${shown}\n\nCopy now — it's only shown once.`);
      }
      // Optimistic local-state patch — Google's truth is now updated but
      // google-accounts.json (and the badge/state UI we render from it)
      // is only regenerated by the hourly reconcile-people workflow.
      // Patch the in-memory record so the next renderPanel reflects the
      // new state immediately. See "high-priority bug audit" 2026-05-19.
      patchLocalAccountAfterAction(action, email, target, extra, out);
      await reloadAccountData();
      renderPanel();
    } catch (err) {
      const msg = "Failed — " + (err && err.message || err);
      if (status) { status.textContent = msg; status.className = "up-edit-status up-edit-status--err"; }
      else alert(msg);
    }
  }

  // Single place to patch googleByPersonId + annotationsMap so all the
  // single-account actions in runAccountAction reflect on-page immediately
  // without waiting for the hourly directory scan to refresh
  // google-accounts.json.
  function patchLocalAccountAfterAction(action, email, target, extra, out) {
    const lowerEmail = (email || "").toLowerCase();
    const list = googleByPersonId[person.id] || [];
    const acct = list.find(a => (a.email || "").toLowerCase() === lowerEmail);
    const setForward = (val) => {
      annotationsMap[lowerEmail] = { ...(annotationsMap[lowerEmail] || {}), forward_to: val };
    };
    switch (action) {
      case "suspend-now":
        if (acct) acct.suspended = true;
        break;
      case "unsuspend":
        if (acct) acct.suspended = false;
        break;
      case "delete-now":
        if (acct) acct.deletion_time = new Date().toISOString();
        break;
      case "recover":
        if (acct) acct.deletion_time = "";
        break;
      case "forward":
        setForward((target || "").toLowerCase());
        break;
      case "cancel-forwarding":
      case "disable-forwarding":
        setForward("");
        break;
      case "convert-to-group":
        // The user account is deleted in this flow; the new Group lives
        // on the Google side but isn't represented in google-accounts.json.
        if (acct) acct.deletion_time = new Date().toISOString();
        setForward("");
        break;
      case "promote-primary": {
        // email = current primary, target = the alt that's becoming primary.
        // After Google's rename: the row with email === target keeps its
        // address but is now the primary; the row with email === email
        // (old primary) is renamed by Google to a non-editable alias and
        // its primary status is gone. We can't faithfully reproduce
        // Google's auto-alias here, so just flip the is_primary flags on
        // both rows — the hourly scan will catch up with full accuracy.
        const newPrimary = list.find(a => (a.email || "").toLowerCase() === (target || "").toLowerCase());
        if (acct) acct.is_primary = false;
        if (newPrimary) newPrimary.is_primary = true;
        break;
      }
      // reset-password / transfer-drive: no on-page state change beyond
      // the alert / queued transfer; nothing to patch.
    }
  }

  // Alias chip handlers — Add / Remove / Port to Group. Driven by the
  // data-acc-alias-{add,remove,group} buttons in the chip block.
  async function handleAliasAction(btn, kind) {
    const card = btn.closest(".up-acct");
    const userEmail = card && card.dataset.accEmail;
    const t = tenantFor(userEmail);
    const form = card.querySelector(".up-acct-form");
    if (kind === "add") {
      form.hidden = false;
      form.innerHTML = `
        <h4>Add alias to ${escapeHtml(userEmail)}</h4>
        <p class="up-hint">A new email that delivers to this same mailbox. Must be on a Workspace domain you control (e.g. @letme.com, @letme.co.uk).</p>
        <input type="email" placeholder="new.alias@letme.com" data-acc-alias-input>
        <div class="up-editor-row">
          <button class="up-btn-sm up-btn-sm--primary" data-acc-confirm>Add</button>
          <button class="up-btn-sm" data-acc-cancel>Cancel</button>
          <span class="up-edit-status" data-acc-status></span>
        </div>`;
      form.querySelector("[data-acc-cancel]").addEventListener("click", () => { form.hidden = true; form.innerHTML = ""; });
      form.querySelector("[data-acc-confirm]").addEventListener("click", () => {
        const alias  = (form.querySelector("[data-acc-alias-input]").value || "").trim().toLowerCase();
        const status = form.querySelector("[data-acc-status]");
        if (!alias.includes("@")) { status.textContent = "Valid email required"; status.className = "up-edit-status up-edit-status--err"; return; }
        runWorkspace(form, "user-alias-add", { user_email: userEmail, alias, tenant: t }, () => {
          patchLocalAccountAliases(userEmail, a => {
            a.aliases          = Array.from(new Set([...(a.aliases || []),          alias])).sort();
            a.aliases_editable = Array.from(new Set([...(a.aliases_editable || []), alias])).sort();
          });
        });
      });
      form.querySelector("[data-acc-alias-input]").focus();
      return;
    }
    if (kind === "remove") {
      const alias = btn.dataset.accAliasRemove;
      if (!alias) return;
      if (!confirm(`Remove the alias ${alias} from ${userEmail}?\n\nMail sent to ${alias} after removal bounces back to the sender. Non-editable aliases (auto-created by Workspace) can't be removed this way and will surface an error.`)) return;
      runWorkspace(null, "user-alias-remove", { user_email: userEmail, alias, tenant: t }, () => {
        patchLocalAccountAliases(userEmail, a => {
          a.aliases          = (a.aliases          || []).filter(x => x !== alias);
          a.aliases_editable = (a.aliases_editable || []).filter(x => x !== alias);
        });
      });
      return;
    }
    if (kind === "to-group") {
      const alias = btn.dataset.accAliasGroup;
      if (!alias) return;
      form.hidden = false;
      form.innerHTML = `
        <h4>Convert alias to forwarding group</h4>
        <p class="up-hint">Removes ${escapeHtml(alias)} as an alias of ${escapeHtml(userEmail)} and creates a Workspace Group at the same address. ${escapeHtml(userEmail)} is added as the initial member.</p>
        <label class="up-field-label">Group display name</label>
        <input type="text" placeholder="e.g. ${escapeHtml((alias.split("@")[0]||"team").replace(/[._-]+/g, " "))}" data-acc-group-name>
        <div class="up-editor-row">
          <button class="up-btn-sm up-btn-sm--primary" data-acc-confirm>Convert</button>
          <button class="up-btn-sm" data-acc-cancel>Cancel</button>
          <span class="up-edit-status" data-acc-status></span>
        </div>`;
      form.querySelector("[data-acc-cancel]").addEventListener("click", () => { form.hidden = true; form.innerHTML = ""; });
      form.querySelector("[data-acc-confirm]").addEventListener("click", () => {
        const name   = (form.querySelector("[data-acc-group-name]").value || "").trim();
        const status = form.querySelector("[data-acc-status]");
        if (!name) { status.textContent = "Group name required"; status.className = "up-edit-status up-edit-status--err"; return; }
        runWorkspace(form, "alias-to-group", { user_email: userEmail, alias, group_name: name, tenant: t }, () => {
          patchLocalAccountAliases(userEmail, a => {
            a.aliases          = (a.aliases          || []).filter(x => x !== alias);
            a.aliases_editable = (a.aliases_editable || []).filter(x => x !== alias);
          });
        });
      });
      form.querySelector("[data-acc-group-name]").focus();
      return;
    }
  }

  // Generic worker caller — same status-text + reload-on-success
  // pattern as runAccountAction but for actions whose body is known at
  // the call site (no map needed).
  async function runWorkspace(form, action, payload, onSuccess) {
    const status = form && form.querySelector("[data-acc-status]");
    if (status) { status.textContent = "Working…"; status.className = "up-edit-status up-edit-status--working"; }
    try {
      const res = await fetch(WORKSPACE_API + "/" + action, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const out = await res.json();
      if (!res.ok || !out.ok) throw new Error(out.error || `HTTP ${res.status}`);
      // Optimistic local-state patch BEFORE the re-render. The scan that
      // produces google-accounts.json runs hourly, so without this patch
      // alias add/remove/to-group succeeds at Google but the page keeps
      // showing the stale snapshot until the next refresh.
      if (onSuccess) onSuccess(out);
      await reloadAccountData();
      renderPanel();
    } catch (err) {
      const msg = "Failed — " + (err && err.message || err);
      if (status) { status.textContent = msg; status.className = "up-edit-status up-edit-status--err"; }
      else alert(msg);
    }
  }

  // Patch googleByPersonId in-place so alias chip lists update immediately
  // after a worker write, rather than waiting for the hourly directory scan.
  function patchLocalAccountAliases(userEmail, mutator) {
    const list = googleByPersonId[person.id] || [];
    const target = list.find(a => (a.email || "").toLowerCase() === (userEmail || "").toLowerCase());
    if (target) mutator(target);
  }

  function openAccountAdd(tenant) {
    const form = document.getElementById("upAcctAddForm");
    if (!form) return;
    const label = tenant === "external" ? "External Gmail (login alternative)"
                : tenant === "letme"    ? "Letme Google account"
                : "Together Google account";
    const placeholder = tenant === "external" ? "jane.doe@gmail.com"
                      : tenant === "letme"    ? "jane.doe@letme.com"
                      : "jane.doe@togetherloans.com";
    form.hidden = false;
    form.innerHTML = `
      <h4>Add ${escapeHtml(label)}</h4>
      <input type="email" id="upAcctAddEmail" placeholder="${escapeHtml(placeholder)}">
      <div class="up-editor-row">
        <button class="up-btn-sm up-btn-sm--primary" id="upAcctAddSave">Link</button>
        <button class="up-btn-sm" id="upAcctAddCancel">Cancel</button>
        <span class="up-edit-status" id="upAcctAddStatus"></span>
      </div>`;
    document.getElementById("upAcctAddCancel").addEventListener("click", () => { form.hidden = true; form.innerHTML = ""; });
    document.getElementById("upAcctAddSave").addEventListener("click", async () => {
      const email = (document.getElementById("upAcctAddEmail").value || "").trim().toLowerCase();
      const status = document.getElementById("upAcctAddStatus");
      if (!email.includes("@")) {
        status.textContent = "Enter a valid email"; status.className = "up-edit-status up-edit-status--err"; return;
      }
      status.textContent = "Linking…"; status.className = "up-edit-status up-edit-status--working";
      try {
        const res = await fetch(WORKSPACE_API + "/google-account-set", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ person_id: person.id, email, tenant  }),
      });
        const out = await res.json();
        if (!res.ok || !out.ok) throw new Error(out.error || `HTTP ${res.status}`);
        // Append the new row to our local index and re-render.
        (googleByPersonId[person.id] ||= []).push(out.record);
        Object.assign(person, out.person);
        renderPanel();
      } catch (err) {
        status.textContent = "Failed — " + err.message; status.className = "up-edit-status up-edit-status--err";
      }
    });
    document.getElementById("upAcctAddEmail").focus();
  }

  async function unlinkGoogleAccount(id) {
    const acct = (googleByPersonId[person.id] || []).find(a => String(a.id) === String(id));
    if (!acct) return;
    if (!confirm(`Unlink ${acct.email} from this Person?\n\nThis only removes the row in google-accounts.json. The Workspace account itself isn't touched — use Suspend / Delete actions for that.`)) return;
    try {
      const res = await fetch(WORKSPACE_API + "/google-account-delete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id  }),
      });
      const out = await res.json();
      if (!res.ok || !out.ok) throw new Error(out.error || `HTTP ${res.status}`);
      googleByPersonId[person.id] = (googleByPersonId[person.id] || []).filter(a => String(a.id) !== String(id));
      renderPanel();
    } catch (err) { alert("Unlink failed: " + err.message); }
  }

  async function reloadAccountData() {
    const [staff, annFile, pending] = await Promise.all([
      fetch("/staff.json",            { cache: "no-store", headers: { "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache" } }).then(r => r.ok ? r.json() : null).catch(() => null),
      fetch("/annotations.json",      { cache: "no-store", headers: { "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache" } }).then(r => r.ok ? r.json() : null).catch(() => null),
      fetch("/pending-transfers.json",{ cache: "no-store", headers: { "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache" } }).then(r => r.ok ? r.json() : null).catch(() => null),
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

  // Batch save for the Editable details card: collects every changed
  // field, fires one people-set, then propagates start_date to
  // PayrollData (same logic as the per-field save used to do).
  async function savePersonCard(card) {
    if (!card) return;
    const status = card.querySelector("[data-card-status]");
    const changes = {};
    card.querySelectorAll(".up-field-editor input, .up-field-editor textarea, .up-field-editor select").forEach(inp => {
      const field = inp.name;
      if (!field) return;
      let value = inp.value || "";
      const orig = inp.dataset.orig != null ? inp.dataset.orig : "";
      if (inp.tagName !== "SELECT" && inp.type !== "date") value = value.trim();
      if (value === orig) return;
      let payload = value;
      if (field === "aliases") {
        payload = Array.from(new Set(value.split(",").map(s => s.trim()).filter(Boolean)));
      } else if (field === "line_manager_id") {
        payload = value === "" ? null : value;
      }
      changes[field] = payload;
    });
    if (Object.keys(changes).length === 0) {
      // Nothing changed — just exit edit mode without a server roundtrip.
      card.querySelectorAll(".up-field-editor").forEach(el => el.hidden = true);
      card.querySelectorAll(".up-field-display").forEach(el => el.hidden = false);
      const footer = card.querySelector(".up-card-edit-footer");
      if (footer) footer.hidden = true;
      const toggle = card.querySelector("[data-card-edit]");
      if (toggle) toggle.hidden = false;
      return;
    }
    if (status) { status.textContent = "Saving…"; status.className = "up-edit-status up-edit-status--working"; }
    try {
      const res = await fetch(WORKSPACE_API + "/people-set", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: person.id, ...changes }),
      });
      const out = await res.json();
      if (!res.ok || !out.ok) throw new Error(out.error || `HTTP ${res.status}`);
      Object.assign(person, out.person);
      for (const [f, v] of Object.entries(changes)) LS.set(person.id, f, v);
      if ("start_date" in changes && person.on_payroll && person.most_recent_payroll_id) {
        try {
          const payRes = await fetch(WORKSPACE_API + "/payroll-set", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "payroll-set", person_id: person.id, start_date: changes.start_date || "" }),
          });
          const payOut = await payRes.json();
          if (payRes.ok && payOut.ok && payOut.record) {
            payrollRecordsById[payOut.record.id] = payOut.record;
            payrollByPersonId[person.id] = payOut.record;
          }
        } catch (e) { /* non-fatal; People was already saved */ }
      }
      const stamp = new Date();
      if (status) {
        status.textContent = `Saved at ${String(stamp.getHours()).padStart(2,"0")}:${String(stamp.getMinutes()).padStart(2,"0")}:${String(stamp.getSeconds()).padStart(2,"0")}`;
        status.className = "up-edit-status up-edit-status--ok up-edit-status--persistent";
      }
      setTimeout(() => { renderPanel(); }, 350);
    } catch (err) {
      if (status) {
        status.textContent = "Failed — " + (err && err.message || err);
        status.className = "up-edit-status up-edit-status--err";
      }
    }
  }

  async function savePersonField(field) {
    const root = document.querySelector(`[data-edit-field="${field}"]`);
    if (!root) return;
    const status = root.querySelector("[data-edit-status]");
    const input  = root.querySelector("input, textarea, select");
    let value  = input ? (input.value || "") : "";
    // Trim free-text inputs; <select> / type=date keep their raw value.
    if (input && input.tagName !== "SELECT" && input.type !== "date") value = value.trim();
    // Field-specific coercions on the way out to the worker:
    let payloadValue = value;
    if (field === "aliases") {
      // Comma-split → trimmed, de-duped, empty-stripped array.
      payloadValue = Array.from(new Set(
        value.split(",").map(s => s.trim()).filter(Boolean)
      ));
    } else if (field === "line_manager_id") {
      // Empty-option → null; numeric ids keep their string form
      // (the worker accepts both Number and string ids).
      payloadValue = value === "" ? null : value;
    }
    status.textContent = "Saving…"; status.className = "up-edit-status up-edit-status--working";
    try {
      const res = await fetch(WORKSPACE_API + "/people-set", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: person.id, [field]: payloadValue  }),
      });
      const out = await res.json();
      if (!res.ok || !out.ok) throw new Error(out.error || `HTTP ${res.status}`);
      Object.assign(person, out.person);
      // Write-through to localStorage so the user's own session sees
      // the new value regardless of any cache layer in front of the
      // server. Re-applied at render time via LS.overlay.
      LS.set(person.id, field, payloadValue);
      // start_date lives in two places — People.start_date AND the active
      // PayrollData record. Updating one alone leaves them inconsistent
      // and the Payroll tab will look "reverted". Propagate.
      if (field === "start_date" && person.on_payroll && person.most_recent_payroll_id) {
        try {
          const payRes = await fetch(WORKSPACE_API + "/payroll-set", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "payroll-set", person_id: person.id, start_date: payloadValue || "" }),
          });
          const payOut = await payRes.json();
          if (payRes.ok && payOut.ok && payOut.record) {
            payrollRecordsById[payOut.record.id] = payOut.record;
            payrollByPersonId[person.id] = payOut.record;
          }
        } catch (e) { /* non-fatal; People was already saved */ }
      }
      const stamp = new Date();
      status.textContent = `Saved at ${String(stamp.getHours()).padStart(2,"0")}:${String(stamp.getMinutes()).padStart(2,"0")}:${String(stamp.getSeconds()).padStart(2,"0")}`;
      status.className = "up-edit-status up-edit-status--ok up-edit-status--persistent";
      setTimeout(() => { renderPanel(); }, 350);
    } catch (err) {
      status.textContent = "Failed — " + (err && err.message || err);
      status.className = "up-edit-status up-edit-status--err";
    }
  }

  // Admin-only: flip the Person record's `suspended` flag via people-set.
  // Doesn't touch the linked Google accounts — those have their own
  // Suspend / Delete buttons on the Google accounts card.
  async function suspendPerson(turnOn) {
    const status = document.querySelector('[data-edit-status="suspended"]');
    if (status) { status.textContent = "Working…"; status.className = "up-edit-status up-edit-status--working"; }
    try {
      const res = await fetch(WORKSPACE_API + "/people-set", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: person.id, suspended: turnOn }),
      });
      const out = await res.json();
      if (!res.ok || !out.ok) throw new Error(out.error || `HTTP ${res.status}`);
      Object.assign(person, out.person);
      LS.set(person.id, "suspended", turnOn);
      renderPanel();
    } catch (err) {
      if (status) { status.textContent = "Failed — " + err.message; status.className = "up-edit-status up-edit-status--err"; }
    }
  }

  // Admin-only: delete the Person record + redirect to the People list.
  // Linked Google accounts + payroll records aren't touched here —
  // admins should delete those from their own rows.
  async function deletePerson() {
    const status = document.querySelector('[data-edit-status="delete"]');
    const name = person.name || person.url_slug;
    if (!confirm(`Delete the Person record for ${name}?\n\n` +
                 `This removes the row from people.json only. The linked Google account(s) and payroll record(s) are NOT touched — delete those from their own rows first if you want a full off-boarding.\n\n` +
                 `Recoverable from git history but no in-app undo.`)) return;
    if (status) { status.textContent = "Deleting…"; status.className = "up-edit-status up-edit-status--working"; }
    try {
      const res = await fetch(WORKSPACE_API + "/people-delete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: person.id }),
      });
      const out = await res.json();
      if (!res.ok || !out.ok) throw new Error(out.error || `HTTP ${res.status}`);
      location.href = "/directory.html";
    } catch (err) {
      if (status) { status.textContent = "Failed — " + err.message; status.className = "up-edit-status up-edit-status--err"; }
    }
  }

  /* ─── Feed panel (Wall preview, read-only) ─────────────────────────── */
  function plainBody(text) {
    return String(text || "").replace(/<\/?strong>/gi, "").replace(/<\/?em>/gi, "").trim();
  }
  // Mirrors wall.html's matchYouTube + URL_RX so YouTube videos in a
  // post body embed as an iframe inside the per-person feed too,
  // instead of just showing the raw URL in the body text.
  const _FP_URL_RX = /\b(https?:\/\/[^\s<>"']+)/g;
  function matchYouTubeId(url) {
    let m = url.match(/youtu\.be\/([\w-]{6,})/);                                  if (m) return m[1];
    m = url.match(/youtube\.com\/(?:watch\?[^"\s]*?\bv=|embed\/|shorts\/|v\/)([\w-]{6,})/); if (m) return m[1];
    return null;
  }
  function feedBodyHtml(rawText) {
    // Plain text + linkify http(s) URLs + render `**foo**` as <strong>
    // (mirrors wall.html's enrichBody({bold:true}) for post bodies).
    // Order: escapeHtml first → bold replace + linkify on the already-
    // escaped string so `**` stays literal in the input but is
    // interpreted as a marker by the regex.
    let html = escapeHtml(plainBody(rawText));
    html = html.replace(_FP_URL_RX, (m) => {
      const safe = m.replace(/"/g, "&quot;");
      return `<a href="${safe}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation();">${m}</a>`;
    });
    // Bold pass — non-greedy, single-line, doesn't merge consecutive
    // spans or honour a dangling `**`. Same regex wall.html uses.
    html = html.replace(/\*\*([^*\n][^*\n]*?)\*\*/g, '<strong>$1</strong>');
    return html;
  }
  function feedLinkBlocksHtml(rawText) {
    const urls = (rawText || "").match(_FP_URL_RX) || [];
    if (!urls.length) return "";
    const seen = new Set();
    const blocks = [];
    for (const url of urls) {
      if (seen.has(url)) continue; seen.add(url);
      const yt = matchYouTubeId(url);
      if (yt) {
        blocks.push(`<div class="up-fp-yt"><iframe src="https://www.youtube-nocookie.com/embed/${escapeHtml(yt)}" loading="lazy" allow="accelerometer; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe></div>`);
      } else {
        // OG-preview placeholder card — hydrated by hydrateFeedLinkPreviews()
        // after the feed mounts. Matches wall.html's .wl-link-preview
        // shape so we can reuse the same worker `link-preview` action.
        let host = "";
        try { host = new URL(url).host; } catch (e) {}
        const safeUrl = url.replace(/"/g, "&quot;");
        blocks.push(`<a class="up-fp-linkprev" href="${safeUrl}" target="_blank" rel="noopener noreferrer" data-fp-linkprev="${safeUrl}">
          <div class="up-fp-linkprev-thumb"><div class="up-fp-linkprev-empty">Loading preview…</div></div>
          <div class="up-fp-linkprev-body">
            <div class="up-fp-linkprev-host">${escapeHtml(host)}</div>
            <div class="up-fp-linkprev-title">${escapeHtml(url)}</div>
            <div class="up-fp-linkprev-desc">Loading…</div>
          </div>
        </a>`);
      }
      if (blocks.length >= 2) break;
    }
    return blocks.join("");
  }
  // Session cache so re-renders don't re-hit the worker per URL.
  const _fpLinkPreviewCache = {};
  function hydrateFeedLinkPreviews() {
    document.querySelectorAll("[data-fp-linkprev]").forEach(el => {
      if (el.dataset.fpLpDone === "1") return;
      const url = el.dataset.fpLinkprev;
      const cached = _fpLinkPreviewCache[url];
      if (cached) { paintFeedLinkPreview(el, cached); return; }
      el.dataset.fpLpDone = "1";
      fetch("/api/wall/link-preview", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      })
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (!data) return;
          _fpLinkPreviewCache[url] = data;
          // Re-paint every currently-mounted instance of the same URL.
          document.querySelectorAll("[data-fp-linkprev]").forEach(node => {
            if (node.dataset.fpLinkprev === url) paintFeedLinkPreview(node, data);
          });
        })
        .catch(() => { /* leave placeholder */ });
    });
  }
  function paintFeedLinkPreview(el, data) {
    const title = data.title || data.url || "";
    const desc  = data.description || "";
    const img   = data.image || "";
    const host  = data.site_name || data.host || "";
    const thumb = el.querySelector(".up-fp-linkprev-thumb");
    const body  = el.querySelector(".up-fp-linkprev-body");
    if (thumb) {
      thumb.innerHTML = img
        ? `<img src="${escapeHtml(img)}" alt="" loading="lazy" onerror="this.parentElement.innerHTML='<div class=&quot;up-fp-linkprev-empty&quot;>no preview</div>';">`
        : `<div class="up-fp-linkprev-empty">no preview</div>`;
    }
    if (body) {
      body.innerHTML = `
        <div class="up-fp-linkprev-host">${escapeHtml(host)}</div>
        <div class="up-fp-linkprev-title">${escapeHtml(title)}</div>
        ${desc ? `<div class="up-fp-linkprev-desc">${escapeHtml(desc)}</div>` : ""}`;
    }
  }
  function postPhotoUrl(p) {
    if (!p) return "";
    // GIFs (giphy/tenor) come in as absolute https URLs — pass them
    // through untouched. Uploaded photos are repo-root-relative paths
    // like `wall-media/img_…jpg`; everything else falls through to
    // legacy fields.
    if (Array.isArray(p.photos) && p.photos[0]) {
      const path = String(p.photos[0]);
      if (/^https?:/i.test(path)) return path;
      return "/" + path.replace(/^\/+/, "");
    }
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
      const body = feedBodyHtml(p.body);
      const linkBlocks = feedLinkBlocksHtml(p.body);
      const mediaUrl = postPhotoUrl(p);
      const mediaHtml = mediaUrl ? `<div class="up-fp-media"><img src="${escapeHtml(mediaUrl)}" alt="" loading="lazy"></div>` : "";
      // Comments here = top-level comments on the post; ignore replies
      // for the preview meta line so the count matches what the Wall
      // page surfaces under each post.
      const commentN = Array.isArray(p.comments)
        ? p.comments.filter(c => !c.parent_comment_id).length
        : 0;
      // wall.json schema: reactions is a dict keyed by emoji, with each
      // value an array of reactor emails. Dedupe by Person.id so a
      // merged Person reacting from two of their accounts counts once.
      const reactN = (() => {
        const seen = new Set();
        for (const arr of Object.values(p.reactions || {})) {
          if (!Array.isArray(arr)) continue;
          for (const eRaw of arr) {
            const e = (eRaw || "").toLowerCase();
            if (!e) continue;
            const person = peopleByEmail[e];
            const key = person ? "p:" + person.id : "e:" + e;
            seen.add(key);
          }
        }
        return seen.size;
      })();
      const meta = [
        commentN ? `${commentN} comment${commentN === 1 ? "" : "s"}` : "",
        reactN   ? `${reactN} reaction${reactN === 1 ? "" : "s"}` : "",
      ].filter(Boolean).join(" · ");
      // Outer wrapper is an <article>, NOT an <a>. The body contains
      // linkified URLs (e.g. https://togetherloans.com/) and the
      // YouTube iframe contains nested <a> tags too — putting all of
      // that inside an outer <a> made the browser auto-close / split
      // anchors, which is why the body was rendering entirely as a
      // link and the iframe spilled past the card. Card is still
      // clickable via the data-fp-href handler wired in wirePanel();
      // the explicit "Open on Wall →" anchor at the bottom is the
      // semantic link.
      return `
        <article class="up-fp" data-fp-href="${escapeHtml(href)}" role="link" tabindex="0">
          <div class="up-fp-head">
            <div class="up-fp-avatar">${avatarHtml}</div>
            <div>
              <div class="up-fp-name">${escapeHtml(person.name || person.url_slug)}</div>
              <div class="up-fp-time">${escapeHtml(ts)}</div>
            </div>
          </div>
          ${body ? `<div class="up-fp-body">${body}</div>` : ""}
          ${linkBlocks}
          ${mediaHtml}
          ${meta ? `<div class="up-fp-meta">${escapeHtml(meta)}</div>` : ""}
          <a class="up-fp-open" href="${escapeHtml(href)}">Open on Wall →</a>
        </article>`;
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

    // Overlay any localStorage recent edit so the user's own session
    // is guaranteed-correct even if some cache layer served stale.
    if (person) person = LS.overlay(person);

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
              <h1 class="up-name">${escapeHtml(person.name || person.url_slug)}</h1>
              <div class="up-subline">${subline || `<span class="up-tenure">${escapeHtml(person.main_google_email || person.id)}</span>`}</div>
            </div>
          </div>
        </div>
      </div>

      <div class="up-body">
        <nav class="up-tabs" aria-label="Profile sections">
          <a class="up-tab" data-tab="info"     href="?tab=info">${svgIcon("info")}<span>Info</span></a>
          <a class="up-tab" data-tab="wall"     href="?tab=wall">${svgIcon("feed")}<span>Wall</span></a>
          <a class="up-tab" data-tab="accounts" href="?tab=accounts">${svgIcon("org")}<span>Accounts</span></a>
          <a class="up-tab" data-tab="calendar" href="?tab=calendar">${svgIcon("calendar")}<span>Calendar</span></a>
        </nav>
        <section class="up-panel" id="upPanel"></section>
      </div>`;

    document.title = `${person.name || person.url_slug} — BOOK Profile`;

    document.querySelectorAll("[data-tab]").forEach(t => {
      t.addEventListener("click", e => { e.preventDefault(); setTab(t.dataset.tab); });
    });
    if (editable) wirePhotoUploads();
    // Stale ?tab=payroll URLs from before the tab was retired (2026-05-19)
    // redirect into the Accounts tab where payroll now lives as one of the
    // six source boxes.
    const initial = initialTab === "payroll" ? "accounts" : initialTab;
    setTab(["wall","info","accounts","calendar"].includes(initial) ? initial : "info");
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
    let step = "init";
    try {
      step = `read file (${file.type || "?"}, ${Math.round(file.size/1024)}kB)`;
      const img = await readImage(file);
      step = `resize ${img.width}×${img.height} → ${opts.kind === "cover" ? "1600×500" : "400×400"}`;
      const b64 = resizeToJpegB64(img, opts);
      step = `encoded b64 (${Math.round(b64.length/1024)}kB)`;
      const action = opts.kind === "cover" ? "cover-photo-upload" : "directory-photo-upload";
      step = `POST /api/workspace/${action}`;
      const res = await fetch(WORKSPACE_API + "/" + action, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, user_email: person.main_google_email, photo_b64: b64, tenant: (person.company || "").includes("togetherloans") ? "togetherloans" : "" }),
      });
      step = `read response (${res.status})`;
      const out = await res.json();
      step = `worker reply ok=${out.ok}`;
      if (!res.ok || !out.ok) throw new Error(out.error || `HTTP ${res.status}`);
      const stamp = new Date().toISOString();
      const field = opts.kind === "cover" ? "cover_photo_uploaded_at" : "directory_photo_uploaded_at";
      // localStorage write-through BEFORE the network call so the new
      // image shows immediately on this device regardless of whether
      // the stamp-write succeeds (the JPEG itself is already on disk).
      LS.set(person.id, field, stamp);

      // Retry the timestamp-write up to 3 times with backoff — this is
      // the call that previously failed silently and left the file on
      // disk without a fresh URL cache-bust.
      let setOut = null, lastErr = null;
      for (let attempt = 1; attempt <= 3; attempt++) {
        step = `POST /api/workspace/people-set ${field} (try ${attempt}/3)`;
        try {
          const setRes = await fetch(WORKSPACE_API + "/people-set", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "people-set", id: person.id, [field]: stamp }),
          });
          const txt = await setRes.text();
          let parsed; try { parsed = JSON.parse(txt); } catch (e) { throw new Error(`non-JSON response: ${txt.slice(0,120)}`); }
          if (!setRes.ok || !parsed.ok) throw new Error(parsed.error || `HTTP ${setRes.status}`);
          setOut = parsed;
          break;
        } catch (e) {
          lastErr = e;
          if (attempt < 3) await new Promise(r => setTimeout(r, 600 * attempt));
        }
      }
      if (!setOut) throw lastErr || new Error("stamp save failed after 3 attempts");
      Object.assign(person, setOut.person);
      renderProfile();
    } catch (err) {
      alert("Upload failed at step [" + step + "]\n\n"
            + (err && err.name ? err.name + ": " : "")
            + (err && err.message || err));
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
    fetch("/api/workspace/table?file=people",           { cache: "no-store", headers: { "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache" } }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/staff.json",            { cache: "no-store", headers: { "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache" } }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/wall.json",             { cache: "no-store", headers: { "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache" } }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/api/workspace/payroll", { cache: "no-store", headers: { "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache" } }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/api/workspace/whoami",  { cache: "no-store", headers: { "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache" } }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/annotations.json",      { cache: "no-store", headers: { "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache" } }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/admins.json",           { cache: "no-store", headers: { "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache" } }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/pending-transfers.json",{ cache: "no-store", headers: { "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache" } }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/api/workspace/table?file=payroll-data",     { cache: "no-store", headers: { "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache" } }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/api/workspace/table?file=google-accounts",  { cache: "no-store", headers: { "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache" } }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/api/workspace/table?file=warehouse-activity",{ cache: "no-store", headers: { "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache" } }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/workspace-actions.json", { cache: "no-store", headers: { "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache" } }).then(r => r.ok ? r.json() : null).catch(() => null),
  ]).then(([peopleFile, staff, wallFile, payroll, who, annFile, adminsFile, pending, payrollFile, gaccounts, warehouse, auditFile]) => {
    if (peopleFile && Array.isArray(peopleFile.people)) {
      people = peopleFile.people;
      for (const p of people) {
        const slug = (p.url_slug || emailToSlug(p.main_google_email)).toLowerCase();
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
    if (payrollFile && Array.isArray(payrollFile.records)) {
      for (const r of payrollFile.records) payrollRecordsById[r.id] = r;
      for (const p of people) {
        if (p.most_recent_payroll_id && payrollRecordsById[p.most_recent_payroll_id]) {
          payrollByPersonId[p.id] = payrollRecordsById[p.most_recent_payroll_id];
        }
      }
    }
    if (gaccounts && Array.isArray(gaccounts.records)) {
      for (const a of gaccounts.records) {
        if (a.person_id == null) continue;
        (googleByPersonId[a.person_id] ||= []).push(a);
      }
    }
    if (warehouse && Array.isArray(warehouse.records)) {
      for (const w of warehouse.records) {
        if (w.person_id == null) continue;
        const cur = warehouseByPersonId[w.person_id];
        if (!cur || (w.last_active_utc || "") > (cur.last_active_utc || "")) {
          warehouseByPersonId[w.person_id] = w;
        }
      }
    }
    if (auditFile && Array.isArray(auditFile.actions)) auditActions = auditFile.actions;
    renderProfile();
  }).catch(err => renderEmpty("Failed to load: " + String(err)));
})();
