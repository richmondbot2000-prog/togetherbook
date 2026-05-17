/* Mobile sub-page accordion for the top nav.
   On a narrow viewport (drawer open), a top-level link that carries a
   data-sub attribute (JSON array of {href, label}) expands its sub-pages
   inline on the first tap; the second tap navigates.

   Desktop (>960px) is untouched — the .qb-subnav row beneath the topbar
   handles sub-page navigation there. */
(function () {
  const links = document.querySelectorAll('.qb-nav-link[data-sub]');
  if (!links.length) return;

  links.forEach(a => {
    a.addEventListener('click', function (ev) {
      // Desktop: no interception — let the browser navigate normally.
      if (window.innerWidth > 960) return;
      // Second tap (already expanded): navigate.
      if (a.classList.contains('qb-nav-link--expanded')) return;

      let subs;
      try { subs = JSON.parse(a.dataset.sub); } catch (e) { return; }
      if (!Array.isArray(subs) || !subs.length) return;

      ev.preventDefault();

      // Collapse any other expanded sibling first.
      a.parentNode.querySelectorAll('.qb-nav-link--expanded').forEach(other => {
        if (other === a) return;
        other.classList.remove('qb-nav-link--expanded');
        const list = other.nextElementSibling;
        if (list && list.classList.contains('qb-nav-sublist')) list.remove();
      });

      a.classList.add('qb-nav-link--expanded');
      const list = document.createElement('div');
      list.className = 'qb-nav-sublist';
      const currentHref = location.pathname.split('/').pop();
      subs.forEach(s => {
        const sa = document.createElement('a');
        sa.className = 'qb-nav-sublink';
        if (s.href === currentHref) sa.classList.add('is-active');
        sa.href = s.href;
        sa.textContent = s.label;
        list.appendChild(sa);
      });
      a.parentNode.insertBefore(list, a.nextSibling);
    });
  });
})();

/* Current-user avatar chip — sits at the top-right of the topbar on
   every page. Click jumps to /directory/<slug>. Silently hides itself
   on the public mirror (no Cloudflare Access, /api/workspace/whoami 401s). */
(function () {
  const bar = document.querySelector('.qb-topbar');
  if (!bar) return;
  if (bar.querySelector('.qb-me')) return;

  // One-time stylesheet — kept tiny so it doesn't fight quiet.css.
  if (!document.getElementById('qbMeStyle')) {
    const s = document.createElement('style');
    s.id = 'qbMeStyle';
    s.textContent = `
      .qb-me { margin-left: 10px; display: inline-flex; align-items: center; gap: 8px;
               text-decoration: none; }
      .qb-me-avatar { width: 32px; height: 32px; border-radius: 50%;
                      background: var(--paper-200, #F5ECD4);
                      display: inline-flex; align-items: center; justify-content: center;
                      font: 600 13px/1 'Inter', sans-serif;
                      color: var(--ink-700, #2C3E66);
                      overflow: hidden;
                      border: 1px solid var(--ink-300, #B6BECF);
                      transition: border-color 140ms ease; }
      .qb-me:hover .qb-me-avatar { border-color: var(--brass-500, #C8973F); }
      .qb-me-avatar img { width: 100%; height: 100%; object-fit: cover; display: block; }
      @media (max-width: 720px) { .qb-me { margin-left: 6px; }
                                  .qb-me-avatar { width: 28px; height: 28px; font-size: 11px; } }
    `;
    document.head.appendChild(s);
  }

  const slot = document.createElement('a');
  slot.className = 'qb-me';
  slot.setAttribute('aria-label', 'My profile');
  slot.innerHTML = '<span class="qb-me-avatar" aria-hidden="true">…</span>';
  bar.appendChild(slot);

  function dirPhotoKey(email) {
    return (email || '').toString().trim().toLowerCase().replace(/@/g, '_at_');
  }
  function emailToSlug(email) {
    return ((email || '').split('@')[0] || '').toLowerCase();
  }
  function initials(name) {
    return (name || '').split(/\s+/).filter(Boolean)
      .map(p => p.charAt(0).toUpperCase()).slice(0, 2).join('') || '?';
  }

  Promise.all([
    fetch('/api/workspace/whoami', { cache: 'no-store' }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch('/staff.json',           { cache: 'no-store' }).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch('/annotations.json',     { cache: 'no-store' }).then(r => r.ok ? r.json() : null).catch(() => null),
  ]).then(([who, staff, ann]) => {
    const email = (who && who.email || '').toLowerCase();
    if (!email) { slot.remove(); return; }
    const slug = emailToSlug(email);
    slot.href = '/directory/' + slug;

    let photo = '', name = email;
    if (staff && Array.isArray(staff.users)) {
      const u = staff.users.find(x => (x.email || '').toLowerCase() === email);
      if (u) { name = u.name || email; photo = u.photo_url || ''; }
    }
    if (ann && ann.annotations && ann.annotations[email] && ann.annotations[email].directory_photo_uploaded_at) {
      photo = '/assets/photos/' + dirPhotoKey(email) + '.jpg?v=' + encodeURIComponent(ann.annotations[email].directory_photo_uploaded_at);
    }
    slot.title = name;
    const av = slot.querySelector('.qb-me-avatar');
    if (photo) {
      av.innerHTML = '<img alt="" src="' + photo.replace(/"/g, '&quot;') + '">';
      const img = av.querySelector('img');
      img.onerror = () => { av.textContent = initials(name); };
    } else {
      av.textContent = initials(name);
    }
  }).catch(() => { slot.remove(); });
})();

