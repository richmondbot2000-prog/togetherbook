/* Mobile sub-page panel for the top nav.
   Every top-level link with `data-sub` renders its sub-pages eagerly as
   a sibling block, so the drawer is always at its maximum height and
   doesn't grow/shrink as the user taps around. Parent links navigate
   on the first tap — no accordion expand step.

   Desktop (>960px) is untouched — the .qb-subnav row beneath the topbar
   handles sub-page navigation there, and the .qb-nav-sublist nodes are
   hidden via CSS. */
(function () {
  const links = document.querySelectorAll('.qb-nav-link[data-sub]');
  if (!links.length) return;
  const currentHref = location.pathname.split('/').pop();
  links.forEach(a => {
    // Don't double-render if a sublist is already in place from a prior run.
    const next = a.nextElementSibling;
    if (next && next.classList && next.classList.contains('qb-nav-sublist')) return;
    let subs;
    try { subs = JSON.parse(a.dataset.sub); } catch (e) { return; }
    if (!Array.isArray(subs) || !subs.length) return;
    const list = document.createElement('div');
    list.className = 'qb-nav-sublist';
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

  // Move the hamburger to the end of the topbar so it always sits as
  // the second-to-last child, immediately before the avatar chip we're
  // about to append. This makes the right-edge cluster a pure DOM-order
  // arrangement instead of relying on flex `order` alone (which some
  // mobile browsers ignore when `position: fixed` siblings are in the
  // mix). Brand stays at the left, drawer is fixed-positioned anyway.
  const hamburger = bar.querySelector('.qb-hamburger');
  if (hamburger) bar.appendChild(hamburger);

  // One-time stylesheet — kept tiny so it doesn't fight quiet.css.
  // `order: 99` keeps the chip last in the flex bar regardless of
  // where in DOM order we appended it (so mobile + desktop both keep
  // it on the right end of the topbar).
  if (!document.getElementById('qbMeStyle')) {
    const s = document.createElement('style');
    s.id = 'qbMeStyle';
    s.textContent = `
      /* Sits next to the System nav-link on desktop with the same
         22 px gap the nav uses between its own items, and ~14 px from
         the hamburger on narrow screens. */
      .qb-me { display: inline-flex; align-items: center; gap: 8px;
               text-decoration: none; flex: 0 0 auto; order: 99; margin-left: 22px; }
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
      /* On narrow screens the hamburger and avatar form a right-edge
         cluster: hamburger gets the auto margin so it pushes itself
         (and the avatar trailing it) to the right of the topbar, and
         the avatar then sits one 14 px gap away. */
      @media (max-width: 960px) { .qb-me { margin-left: 14px; }
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
    fetch('/people.json',          { cache: 'no-store' }).then(r => r.ok ? r.json() : null).catch(() => null),
  ]).then(([who, staff, ann, peopleFile]) => {
    const email = (who && who.email || '').toLowerCase();
    if (!email) return; // keep the fallback icon + /directory.html href

    // Locate this viewer's Person record so we can walk ALL their
    // linked Google emails (main + alts + external) when looking for an
    // uploaded directory photo. Without this, an upload made under
    // @letme.co.uk would be invisible to a viewer signed in as
    // @letme.com — and the avatar would silently fall back to the
    // Google profile photo while the profile-page hero shows the
    // upload (profile.js walks all candidates).
    const people = (peopleFile && peopleFile.people) || [];
    const matchEmail = (p, e) => {
      const all = [p.main_google_email, ...(p.alt_google_emails || []), p.external_google_email]
        .filter(Boolean).map(x => x.toLowerCase());
      return all.includes(e);
    };
    const person = people.find(p => matchEmail(p, email)) || null;
    const candidates = person
      ? [person.main_google_email, ...(person.alt_google_emails || []), person.external_google_email]
          .filter(Boolean).map(x => x.toLowerCase())
      : [email];
    // Prefer the Person's URL slug for the profile link so the chip
    // always points at the canonical /directory/<slug> regardless of
    // which alias the viewer signed in with.
    const slug = (person && person.url_slug) || emailToSlug(email);
    const profileHref = '/directory/' + slug;
    slot.href = profileHref;

    // The mobile People sub-menu carries a "Your Page" placeholder whose
    // href we rewrite once we know the viewer's slug. The data-yourpage
    // marker lives inside the JSON of `data-sub` on the People nav link.
    document.querySelectorAll('.qb-nav-link[data-sub]').forEach(a => {
      try {
        const subs = JSON.parse(a.dataset.sub);
        let dirty = false;
        subs.forEach(s => {
          if (s['data-yourpage']) { s.href = profileHref; dirty = true; }
        });
        if (dirty) a.dataset.sub = JSON.stringify(subs);
      } catch (e) { /* malformed JSON — leave alone */ }
    });

    let photo = '', name = (person && person.name) || email;
    // Google profile photo as the baseline — picked from whichever
    // staff.json row matches one of the candidate emails.
    if (staff && Array.isArray(staff.users)) {
      for (const e of candidates) {
        const u = staff.users.find(x => (x.email || '').toLowerCase() === e);
        if (u) {
          if (!person) name = u.name || email;
          if (u.photo_url) { photo = u.photo_url; break; }
        }
      }
    }
    // Uploaded photo wins — same lookup profile.js uses for the hero
    // avatar, so the two surfaces never drift apart.
    if (ann && ann.annotations) {
      for (const e of candidates) {
        const a = ann.annotations[e];
        if (a && a.directory_photo_uploaded_at) {
          photo = '/assets/photos/' + dirPhotoKey(e) + '.jpg?v=' + encodeURIComponent(a.directory_photo_uploaded_at);
          break;
        }
      }
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

/* Birthday banner — when today is anyone's birthday (UK local date),
   drop a celebratory strip in just under the topbar with balloons, a
   short happy-birthday GIF, and the person's name. Multi-birthday
   days list every person. Falls back silently if people.json is
   missing or no one has today as their date_of_birth. */
(function () {
  const bar = document.querySelector('.qb-topbar');
  if (!bar || document.querySelector('.qb-birthday-banner')) return;

  fetch('/people.json', { cache: 'no-store' })
    .then(r => r.ok ? r.json() : null)
    .catch(() => null)
    .then(doc => {
      if (!doc || !Array.isArray(doc.people)) return;
      const now = new Date();
      const md = String(now.getMonth() + 1).padStart(2, '0') + '-' +
                 String(now.getDate()).padStart(2, '0');
      const birthdays = doc.people.filter(p => {
        if (p.suspended || p.deletion_time) return false;
        const dob = p.date_of_birth || '';
        return dob.length >= 10 && dob.slice(5, 10) === md;
      });
      if (!birthdays.length) return;

      // Tag <body> so birthday-only CSS overrides can fire without
      // affecting any normal day. Used to grow the brand-logo slot
      // so the TogetherBOOK wordmark inside the wider birthday logo
      // (which also carries balloons + "Happy Birthday" script) stays
      // at the same visual size as the everyday wordmark.
      document.body.classList.add('qb-is-birthday');

      // Inject styles once.
      if (!document.getElementById('qbBirthdayStyle')) {
        const s = document.createElement('style');
        s.id = 'qbBirthdayStyle';
        s.textContent = `
          /* Birthday logo is ~3.5× wider than the standalone wordmark
             because it bundles balloons + a "Happy Birthday" script.
             Grow the logo's rendered height (and the topbar with it)
             so the wordmark portion lands at the normal reading size.
             Topbar values are tuned to keep the brand visually centred
             and avoid clipping. */
          body.qb-is-birthday .qb-topbar    { height: 104px; }
          body.qb-is-birthday .qb-brand-logo { height: 88px; }
          @media (max-width: 960px) {
            body.qb-is-birthday .qb-topbar    { height: 88px; }
            body.qb-is-birthday .qb-brand-logo { height: 72px; }
          }
          @media (max-width: 480px) {
            body.qb-is-birthday .qb-topbar    { height: 70px; }
            body.qb-is-birthday .qb-brand-logo { height: 54px; }
          }

          .qb-birthday-banner {
            display: flex; align-items: center; justify-content: center;
            gap: 14px; padding: 10px 18px;
            background: linear-gradient(90deg,#FFE7C2 0%,#FBD89A 50%,#FFE7C2 100%);
            border-bottom: 1px solid var(--brass-500, #C8973F);
            font: 600 16px/1.2 var(--font-display, 'Newsreader', serif);
            color: var(--ink-900, #11192E);
            position: sticky; top: 104px; z-index: 999;
          }
          @media (max-width: 960px) { .qb-birthday-banner { top: 88px; font-size: 14px; padding: 8px 12px; gap: 8px; } }
          @media (max-width: 480px) { .qb-birthday-banner { top: 70px; } }
          .qb-bd-balloon {
            display: inline-block; font-size: 22px;
            animation: qbBalloon 2.4s ease-in-out infinite alternate;
            transform-origin: bottom center;
          }
          .qb-bd-balloon:nth-child(2) { animation-delay: 0.6s; }
          .qb-bd-balloon:nth-child(3) { animation-delay: 1.2s; }
          @keyframes qbBalloon {
            0%   { transform: translateY(0) rotate(-3deg); }
            100% { transform: translateY(-6px) rotate(3deg); }
          }
          .qb-bd-gif { height: 28px; width: auto; border-radius: 4px; }
          .qb-bd-text a {
            color: inherit; text-decoration: none;
            border-bottom: 1px dotted var(--brass-600, #A47829);
          }
          .qb-bd-text a:hover { border-bottom-style: solid; }
        `;
        document.head.appendChild(s);
      }

      // Build the names list — link each to their /directory/<slug>
      // so the banner doubles as a quick way to post on their wall.
      const namePieces = birthdays.map(p => {
        const slug = p.url_slug || (p.main_google_email || '').split('@')[0].toLowerCase();
        const href = '/directory/' + slug;
        const safe = (p.name || slug).replace(/&/g, '&amp;').replace(/</g, '&lt;');
        return `<a href="${href}">${safe}</a>`;
      });
      const namesHtml = namePieces.length === 1 ? namePieces[0]
        : namePieces.slice(0, -1).join(', ') + ' and ' + namePieces[namePieces.length - 1];

      const banner = document.createElement('div');
      banner.className = 'qb-birthday-banner';
      banner.setAttribute('role', 'note');
      banner.innerHTML =
        '<span class="qb-bd-balloon" aria-hidden="true">🎈</span>' +
        '<span class="qb-bd-balloon" aria-hidden="true">🎂</span>' +
        '<img class="qb-bd-gif" alt="" src="/wall-media/birthday/gif1.gif">' +
        '<span class="qb-bd-text">Happy Birthday ' + namesHtml + '!</span>' +
        '<span class="qb-bd-balloon" aria-hidden="true">🎉</span>';

      bar.parentNode.insertBefore(banner, bar.nextSibling);

      // Swap the topbar logo for the birthday variant. Falls back to
      // the original on 404 so dropping the file at /togetherbook-
      // logo-birthday.png is the only step needed to switch it on.
      const logos = document.querySelectorAll('.qb-brand-logo');
      logos.forEach(img => {
        const original = img.src;
        const birthdaySrc = '/togetherbook-logo-birthday.png?v=' + Date.now();
        img.onerror = () => { img.onerror = null; img.src = original; };
        img.src = birthdaySrc;
      });
    });
})();

