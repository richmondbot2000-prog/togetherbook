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

/* Current-user avatar chip — always renders at the top-right of the
   topbar. Whoami fills in a photo + a /directory/<slug> link; when
   whoami is unavailable (public mirror, cookies blocked, network
   error) the chip stays put with a generic icon and falls back to
   linking the People landing so the user can find themselves. */
(function () {
  const bar = document.querySelector('.qb-topbar');
  if (!bar) return;
  if (bar.querySelector('.qb-me')) return;

  // One-time stylesheet — kept tiny so it doesn't fight quiet.css.
  // `order: 99` keeps the chip last in the flex bar regardless of
  // where in DOM order we appended it (so mobile + desktop both keep
  // it on the right end of the topbar).
  if (!document.getElementById('qbMeStyle')) {
    const s = document.createElement('style');
    s.id = 'qbMeStyle';
    s.textContent = `
      .qb-me { display: inline-flex; align-items: center; gap: 8px;
               text-decoration: none; flex: 0 0 auto; order: 99; margin-left: 10px; }
      .qb-me-avatar { width: 36px; height: 36px; border-radius: 50%;
                      background: var(--paper-200, #F5ECD4);
                      display: inline-flex; align-items: center; justify-content: center;
                      font: 600 13px/1 'Inter', sans-serif;
                      color: var(--ink-700, #2C3E66);
                      overflow: hidden;
                      border: 1px solid var(--ink-300, #B6BECF);
                      transition: border-color 140ms ease; }
      .qb-me:hover .qb-me-avatar { border-color: var(--brass-500, #C8973F); }
      .qb-me-avatar img { width: 100%; height: 100%; object-fit: cover; display: block; }
      .qb-me-avatar svg { width: 60%; height: 60%; color: var(--ink-500, #6B779A); }
      @media (max-width: 960px) { .qb-me { margin-left: 6px; }
                                  .qb-me-avatar { width: 32px; height: 32px; font-size: 11px; } }
    `;
    document.head.appendChild(s);
  }

  const FALLBACK_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">' +
      '<circle cx="12" cy="8" r="4"/>' +
      '<path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6" stroke-linecap="round"/>' +
    '</svg>';

  const slot = document.createElement('a');
  slot.className = 'qb-me';
  slot.setAttribute('aria-label', 'My profile');
  // Default state: generic icon, link to the People page. This is what
  // every viewer sees instantly while whoami resolves, and it stays as
  // the fallback if whoami can't tell us who they are.
  slot.href = '/directory.html';
  slot.title = 'My profile';
  slot.innerHTML = '<span class="qb-me-avatar" aria-hidden="true">' + FALLBACK_ICON + '</span>';
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
    if (!email) return; // keep the fallback icon + /directory.html href
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
      img.onerror = () => { av.innerHTML = ''; av.textContent = initials(name); };
    } else {
      av.innerHTML = '';
      av.textContent = initials(name);
    }
  }).catch(() => { /* keep the fallback icon */ });
})();

