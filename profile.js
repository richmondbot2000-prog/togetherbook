/*
  Shared renderer for the user profile page.

  Mounts into <div id="upRoot"> in either:
    - user.html?email=<addr>   (legacy direct link)
    - /directory/<slug>        (clean URL, served by 404.html SPA shim)

  The host page sets either window.__profileEmail (resolved address) or
  leaves it blank — in which case we fall back to the ?email= query.
*/
(function () {
  const qs = new URLSearchParams(location.search || "");
  const initialTab = (qs.get("tab") || "info").toLowerCase();

  let targetEmail = (window.__profileEmail || qs.get("email") || "").toLowerCase().trim();
  let targetSlug  = (window.__profileSlug  || "").toLowerCase().trim();

  let staffByEmail = {};
  let staffBySlug  = {};
  let annotationsMap = {};
  let groupsList = [];
  let wallPosts = [];
  let payrollByEmail = {};
  let currentTab = "info";
  let dataReady = false;
  let pendingTimers = {};

  const ANNOTATIONS_API = "/api/annotations";

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
  function photoUrlFor(u) {
    if (!u || !u.email) return "";
    const email = u.email.toLowerCase();
    const ann = annotationsMap[email];
    if (ann && ann.directory_photo_uploaded_at) {
      return `/assets/photos/${dirPhotoKey(email)}.jpg?v=${encodeURIComponent(ann.directory_photo_uploaded_at)}`;
    }
    return u.photo_url || "";
  }
  function svgIcon(name) {
    const paths = {
      info:   `<rect x="3" y="3" width="14" height="18" rx="1" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M7 8h6M7 12h6M7 16h4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>`,
      feed:   `<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>`,
      groups: `<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><circle cx="9" cy="7" r="4" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>`,
      org:    `<circle cx="12" cy="5" r="3" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="5" cy="19" r="3" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="19" cy="19" r="3" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M12 8v4M12 12H5v4M12 12h7v4" stroke="currentColor" stroke-width="1.6" fill="none"/>`,
      edit:   `<path d="M14 4l6 6-9 9H5v-6l9-9z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>`,
    };
    return `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || ""}</svg>`;
  }

  function tenureLabel(payrollEntry) {
    if (!payrollEntry) return "";
    const start = payrollEntry.start_date || payrollEntry.startDate;
    if (!start) return "";
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

  function setTab(tab) {
    currentTab = tab;
    // Update URL but keep the path stable — only flip the ?tab= param.
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
    // The calendar lives in holidays.html — we embed it locked to this user.
    // embed=1 tells holidays.html to hide the page chrome; postMessage syncs
    // iframe height to its content so the panel scrolls naturally.
    const src = `/holidays.html?user=${encodeURIComponent(targetEmail)}&view=own&embed=1`;
    return `
      <h2 class="up-panel-title">Calendar</h2>
      <div class="up-cal-wrap">
        <iframe class="up-cal-frame" id="upCalFrame" src="${src}" title="Calendar" referrerpolicy="same-origin"></iframe>
      </div>`;
  }

  /* ─── Information panel ────────────────────────────────────────────── */
  function renderInfoPanel() {
    const u   = staffByEmail[targetEmail] || {};
    const ann = annotationsMap[targetEmail] || {};
    const pr  = payrollByEmail[targetEmail];

    const phoneVal = ann.phone || (pr && pr.mobile) || "";
    const phoneSrc = ann.phone ? "" : (phoneVal ? '<span class="up-src">from payroll</span>' : "");
    const addrVal = ann.address || (pr && pr.address) || "";
    const addrSrc = ann.address ? "" : (addrVal ? '<span class="up-src">from payroll</span>' : "");

    const lineMgrEmail = (ann.line_manager || "").toLowerCase().trim();
    const lineMgr = lineMgrEmail ? staffByEmail[lineMgrEmail] : null;
    const lineMgrDisplay = lineMgrEmail
      ? (lineMgr
          ? `<a class="up-mgr-link" href="${profileHref(lineMgrEmail)}">${escapeHtml(lineMgr.name || lineMgrEmail)}</a>`
          : `<span>${escapeHtml(lineMgrEmail)}</span>`)
      : `<span class="up-empty-val">No line manager</span>`;

    // Datalist for line-manager picker — every other staff member.
    const lmOptions = Object.values(staffByEmail)
      .filter(x => !x.suspended && !x.deletion_time && (x.email || "").toLowerCase() !== targetEmail)
      .sort((a, b) => (a.name || "").localeCompare(b.name || ""))
      .map(x => `<option value="${escapeHtml((x.email || "").toLowerCase())}">${escapeHtml(x.name || "")}</option>`)
      .join("");

    // Read-only block fields (department, tenant, hiring date, email).
    const readOnly = [
      ["Email",         escapeHtml(u.email || targetEmail)],
      ["Department",    u.department ? escapeHtml(u.department) : '<span class="up-empty-val">—</span>'],
      ["Tenant",        u.tenant ? escapeHtml(u.tenant) : '<span class="up-empty-val">—</span>'],
      ["Hiring date",   (pr && pr.start_date)
                          ? escapeHtml(new Date(pr.start_date).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" }))
                          : '<span class="up-empty-val">—</span>'],
    ];
    const readOnlyHtml = readOnly.map(([label, value]) => `
      <div class="up-field">
        <div class="up-field-label">${escapeHtml(label)}</div>
        <div class="up-field-value">${value}</div>
      </div>`).join("");

    return `
      <h2 class="up-panel-title">Information</h2>

      <div class="up-card">
        <div class="up-card-head">Editable details <span class="up-card-hint">click any value to change · saves to <code>annotations.json</code></span></div>

        <div class="up-field" data-edit-field="role">
          <div class="up-field-label">Role</div>
          <div class="up-field-display">
            <span class="up-field-value ${ann.role ? "" : "up-empty-val"}">${escapeHtml(ann.role) || "Not set"}</span>
            <button type="button" class="up-link-btn" data-edit-toggle="role">Edit</button>
          </div>
          <div class="up-field-editor" hidden>
            <input type="text" name="role" maxlength="80" value="${escapeHtml(ann.role || "")}" placeholder="e.g. Communications Director">
            <div class="up-editor-row">
              <button type="button" class="up-btn-sm up-btn-sm--primary" data-edit-save="role">Save</button>
              <button type="button" class="up-btn-sm" data-edit-cancel="role">Cancel</button>
              <span class="up-edit-status" data-edit-status="role"></span>
            </div>
          </div>
        </div>

        <div class="up-field" data-edit-field="phone">
          <div class="up-field-label">Phone</div>
          <div class="up-field-display">
            <span class="up-field-value ${phoneVal ? "" : "up-empty-val"}">${escapeHtml(phoneVal) || "Not set"}</span>
            ${phoneSrc}
            <button type="button" class="up-link-btn" data-edit-toggle="phone">Edit</button>
          </div>
          <div class="up-field-editor" hidden>
            <input type="tel" name="phone" value="${escapeHtml(ann.phone || "")}" placeholder="${escapeHtml(phoneVal || "+44 7…")}">
            <div class="up-editor-row">
              <button type="button" class="up-btn-sm up-btn-sm--primary" data-edit-save="phone">Save</button>
              <button type="button" class="up-btn-sm" data-edit-cancel="phone">Cancel</button>
              <span class="up-edit-status" data-edit-status="phone"></span>
            </div>
          </div>
        </div>

        <div class="up-field" data-edit-field="address">
          <div class="up-field-label">Address</div>
          <div class="up-field-display">
            <span class="up-field-value ${addrVal ? "" : "up-empty-val"}" style="white-space:pre-line;">${escapeHtml(addrVal) || "Not set"}</span>
            ${addrSrc}
            <button type="button" class="up-link-btn" data-edit-toggle="address">Edit</button>
          </div>
          <div class="up-field-editor" hidden>
            <textarea name="address" rows="3" placeholder="${escapeHtml(addrVal || "Home address")}">${escapeHtml(ann.address || "")}</textarea>
            <div class="up-editor-row">
              <button type="button" class="up-btn-sm up-btn-sm--primary" data-edit-save="address">Save</button>
              <button type="button" class="up-btn-sm" data-edit-cancel="address">Cancel</button>
              <span class="up-edit-status" data-edit-status="address"></span>
            </div>
          </div>
        </div>

        <div class="up-field" data-edit-field="line_manager">
          <div class="up-field-label">Line manager</div>
          <div class="up-field-display">
            ${lineMgrDisplay}
            <button type="button" class="up-link-btn" data-edit-toggle="line_manager">Edit</button>
          </div>
          <div class="up-field-editor" hidden>
            <input type="email" name="line_manager" list="upLineManagerOptions" autocomplete="off"
                   value="${escapeHtml(ann.line_manager || "")}"
                   placeholder="manager.email@…">
            <datalist id="upLineManagerOptions">${lmOptions}</datalist>
            <p class="up-hint">Pick a colleague from the list. This person can see + edit this user's calendar on Holidays.</p>
            <div class="up-editor-row">
              <button type="button" class="up-btn-sm up-btn-sm--primary" data-edit-save="line_manager">Save</button>
              <button type="button" class="up-btn-sm" data-edit-cancel="line_manager">Cancel</button>
              <span class="up-edit-status" data-edit-status="line_manager"></span>
            </div>
          </div>
        </div>
      </div>

      <div class="up-card">
        <div class="up-card-head">Workspace</div>
        <div class="up-fields-grid">${readOnlyHtml}</div>
      </div>`;
  }

  function wirePanel() {
    document.querySelectorAll("[data-edit-toggle]").forEach(btn => {
      btn.addEventListener("click", () => {
        const f = btn.dataset.editToggle;
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
      btn.addEventListener("click", () => saveField(btn.dataset.editSave));
    });
  }

  async function saveField(field) {
    const root = document.querySelector(`[data-edit-field="${field}"]`);
    if (!root) return;
    const status = root.querySelector("[data-edit-status]");
    const input  = root.querySelector("input, textarea");
    const value  = (input && input.value || "").trim();
    status.textContent = "Saving…"; status.className = "up-edit-status up-edit-status--working";
    try {
      const payload = { key: targetEmail };
      payload[field] = value;
      const res = await fetch(ANNOTATIONS_API, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      if (!res.ok) {
        let detail = "";
        try { detail = (await res.json()).error || ""; } catch (e) {}
        throw new Error(`HTTP ${res.status}${detail ? " — " + detail : ""}`);
      }
      const out = await res.json();
      if (out && out.all && out.all.annotations) {
        annotationsMap = out.all.annotations;
      } else if (out && out.value === null) {
        delete annotationsMap[targetEmail];
      } else if (out && out.value) {
        annotationsMap[targetEmail] = out.value;
      }
      status.textContent = "Saved";
      status.className = "up-edit-status up-edit-status--ok";
      setTimeout(() => { renderPanel(); }, 350);
    } catch (err) {
      status.textContent = "Failed — " + (err && err.message || err);
      status.className = "up-edit-status up-edit-status--err";
    }
  }

  /* ─── Feed panel (Wall preview, read-only) ─────────────────────────── */
  // Strip mention markup back to plain @local-part text so preview cards
  // don't carry unfilled HTML from the Wall renderer.
  function plainBody(text) {
    if (!text) return "";
    return String(text)
      .replace(/<\/?strong>/gi, "")
      .replace(/<\/?em>/gi, "")
      .trim();
  }
  function postPhotoUrl(p) {
    if (!p) return "";
    if (p.media && p.media.url) return p.media.url;
    if (p.photo_url) return p.photo_url;
    return "";
  }
  function renderFeedPanel() {
    const posts = (wallPosts || [])
      .filter(p => (p.author_email || "").toLowerCase() === targetEmail)
      .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
    if (!posts.length) {
      return `<h2 class="up-panel-title">Wall</h2><div class="up-empty">No posts yet.</div>`;
    }
    const u = staffByEmail[targetEmail] || {};
    const photo = photoUrlFor(u);
    const avatarHtml = photo
      ? `<img src="${escapeHtml(photo)}" alt="">`
      : `<span>${escapeHtml(initials(u.name))}</span>`;

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
              <div class="up-fp-name">${escapeHtml(u.name || targetEmail)}</div>
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

  /* ─── Groups panel ─────────────────────────────────────────────────── */
  function renderGroupsPanel() {
    const memberships = (groupsList || []).filter(g =>
      Array.isArray(g.members) && g.members.some(m => (m.email || "").toLowerCase() === targetEmail)
    );
    if (!memberships.length) {
      return `<h2 class="up-panel-title">Workspace groups</h2><div class="up-empty">Not a member of any Workspace group.</div>`;
    }
    const chips = memberships.map(g => `
      <a class="up-group-chip" href="/directory.html?email=${encodeURIComponent(g.email)}" title="${escapeHtml(g.description || g.email)}">
        ${escapeHtml(g.name || g.email)}
        <code>${escapeHtml(g.email)}</code>
      </a>`).join("");
    return `<h2 class="up-panel-title">Workspace groups (${memberships.length})</h2><div class="up-groups">${chips}</div>`;
  }

  /* ─── Page assembly ────────────────────────────────────────────────── */
  function renderEmpty(msg) {
    const root = document.getElementById("upRoot");
    if (root) root.innerHTML = `<div class="up-error">${escapeHtml(msg)}</div>`;
  }

  function renderProfile() {
    // If we were handed a slug instead of an email, resolve now that staff
    // data is loaded.
    if (!targetEmail && targetSlug) {
      const match = staffBySlug[targetSlug];
      if (match) targetEmail = (match.email || "").toLowerCase();
    }
    if (!targetEmail) { renderEmpty(`No user matched "${targetSlug || "(missing)"}".`); return; }

    const u = staffByEmail[targetEmail];
    if (!u) { renderEmpty(`No staff record found for ${targetEmail}.`); return; }

    const pr = payrollByEmail[targetEmail];
    const photo = photoUrlFor(u);
    const avatar = photo
      ? `<img src="${escapeHtml(photo)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('span'),{textContent:'${escapeHtml(initials(u.name))}'}))">`
      : escapeHtml(initials(u.name));
    const ann = annotationsMap[targetEmail] || {};
    const role = ann.role || u.title || "";
    const tenure = tenureLabel(pr);
    const subline = [
      role ? `<span class="up-role">${escapeHtml(role)}</span>` : "",
      tenure ? `<span class="up-tenure">${escapeHtml(tenure)}</span>` : "",
    ].filter(Boolean).join("");

    document.getElementById("upRoot").innerHTML = `
      <div class="up-header">
        <div class="up-cover"></div>
        <div class="up-info">
          <div class="up-id">
            <div class="up-avatar-wrap">
              <div class="up-avatar">${avatar}</div>
            </div>
            <div class="up-headline">
              <h1 class="up-name">${escapeHtml(u.name || targetEmail)}</h1>
              <div class="up-subline">${subline || `<span class="up-tenure">${escapeHtml(u.email || targetEmail)}</span>`}</div>
            </div>
          </div>
          <div class="up-actions">
            <a class="up-btn" href="/org-structure.html?center=${encodeURIComponent(targetEmail)}">${svgIcon("org")} Org chart</a>
            <a class="up-btn up-btn--primary" href="/directory.html?email=${encodeURIComponent(targetEmail)}">${svgIcon("edit")} Open in Directory</a>
          </div>
        </div>
      </div>

      <div class="up-body">
        <nav class="up-tabs" aria-label="Profile sections">
          <a class="up-tab" data-tab="calendar" href="?tab=calendar">${svgIcon("info")}<span>Calendar</span></a>
          <a class="up-tab" data-tab="info"     href="?tab=info">${svgIcon("info")}<span>Info</span></a>
          <a class="up-tab" data-tab="wall"     href="?tab=wall">${svgIcon("feed")}<span>Wall</span></a>
        </nav>
        <section class="up-panel" id="upPanel"></section>
      </div>`;

    document.title = `${u.name || u.email} — BOOK Profile`;

    document.querySelectorAll("[data-tab]").forEach(t => {
      t.addEventListener("click", e => { e.preventDefault(); setTab(t.dataset.tab); });
    });
    setTab(["info","wall","calendar"].includes(initialTab) ? initialTab : "calendar");
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

  Promise.all([
    fetch("/staff.json",       { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/annotations.json", { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/groups.json",      { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/wall.json",        { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch("/api/workspace/payroll", { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
  ]).then(([staff, annFile, groupsFile, wallFile, payroll]) => {
    if (staff && Array.isArray(staff.users)) {
      // When two tenants share a local-part we prefer the @letme.co.uk one,
      // then @letme.com, then alphabetical first.
      const slugWeight = email => {
        const e = (email || "").toLowerCase();
        if (e.endsWith("@letme.co.uk")) return 0;
        if (e.endsWith("@letme.com"))   return 1;
        return 2;
      };
      const sorted = [...staff.users].sort((a, b) => {
        const w = slugWeight(a.email) - slugWeight(b.email);
        return w !== 0 ? w : (a.email || "").localeCompare(b.email || "");
      });
      for (const u of sorted) {
        const k = (u.email || "").toLowerCase();
        if (!k) continue;
        staffByEmail[k] = u;
        const slug = emailToSlug(u.email);
        if (slug && !staffBySlug[slug]) staffBySlug[slug] = u;
      }
    }
    if (annFile && annFile.annotations && typeof annFile.annotations === "object") {
      annotationsMap = annFile.annotations;
    }
    if (groupsFile && Array.isArray(groupsFile.groups)) {
      groupsList = groupsFile.groups;
    }
    if (wallFile && Array.isArray(wallFile.posts)) {
      wallPosts = wallFile.posts;
    }
    if (payroll && Array.isArray(payroll.rows)) {
      for (const r of payroll.rows) {
        const e = (r.email || "").toLowerCase();
        if (e) payrollByEmail[e] = r;
      }
    } else if (payroll && payroll.by_email) {
      payrollByEmail = payroll.by_email;
    }
    dataReady = true;
    renderProfile();
  }).catch(err => renderEmpty("Failed to load: " + String(err)));
})();
