/* live-reload.js
 *
 * Detects when a newer build of the site has been deployed than the
 * one currently loaded in the browser, and triggers a cache-bypassed
 * reload. Solves the "cmd+R doesn't show the new version" symptom that
 * comes from the HTML files NOT being cache-busted by URL — when the
 * browser serves a cached HTML, it references an old JS bundle and
 * the page misses the new build.
 *
 * Mechanics:
 *   1. On page load, fetch /version.txt with a unique cache-busting
 *      query so neither the browser cache nor the edge cache can
 *      satisfy the request. /version.txt is rewritten on every push
 *      by .github/workflows/bake-version.yml with the current git SHA.
 *   2. Compare the fetched SHA to the last SHA we saw on this device
 *      (stored in localStorage). On first ever load, just record it
 *      and stop — we have nothing to compare against.
 *   3. If the live SHA differs from the last-seen SHA, the browser is
 *      almost certainly rendering a stale cached HTML. location.replace
 *      with a fresh `?_=<timestamp>` query string forces the browser
 *      to re-fetch HTML bypassing both its own cache and any edge
 *      cache, and on the reload the new HTML's script will record the
 *      new SHA.
 *   4. sessionStorage flag prevents a loop in the pathological case
 *      where version.txt advances mid-session but the reload doesn't
 *      pick up new HTML (CDN propagation lag, etc).
 *
 * Failure modes are silent — if /version.txt is missing, the API call
 * times out, or anything else goes wrong, we leave the page alone.
 */
(function () {
  if (window.__liveReloadInit) return;
  window.__liveReloadInit = true;

  const STORAGE_KEY      = "tb.lastSeenVersion";
  const SESSION_TRIED    = "tb.reloadAttempted";

  fetch("/version.txt?_=" + Date.now(), {
    cache: "no-store",
    headers: { "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache" },
  })
    .then(function (r) { return r.ok ? r.text() : null; })
    .then(function (live) {
      if (!live) return;
      live = String(live).trim();
      if (!live || live === "bootstrap") {
        // Initial deploy hasn't baked a real SHA yet — record nothing.
        return;
      }
      let last;
      try { last = localStorage.getItem(STORAGE_KEY); } catch (e) { last = null; }
      if (!last) {
        try { localStorage.setItem(STORAGE_KEY, live); } catch (e) {}
        return;
      }
      if (last === live) {
        // Up-to-date. Clear any reload guard from a previous mismatch.
        try { sessionStorage.removeItem(SESSION_TRIED); } catch (e) {}
        return;
      }
      // Mismatch — we have a stale page. Guard against pathological
      // loops: if we've already tried to reload to this exact live
      // version this session and ended up back here, just accept the
      // new value and stop.
      let guard;
      try { guard = sessionStorage.getItem(SESSION_TRIED); } catch (e) { guard = null; }
      if (guard === live) {
        try { localStorage.setItem(STORAGE_KEY, live); } catch (e) {}
        return;
      }
      try { sessionStorage.setItem(SESSION_TRIED, live); } catch (e) {}
      // Replace with a cache-busting query so HTML + every sub-resource
      // is re-fetched bypassing every cache layer.
      var url = location.pathname + "?_=" + Date.now() + location.hash;
      location.replace(url);
    })
    .catch(function () { /* silent */ });
})();
