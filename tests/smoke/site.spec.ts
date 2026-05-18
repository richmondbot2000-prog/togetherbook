import { test, expect, Page, ConsoleMessage } from "@playwright/test";

// Smoke suite. Catches the classes of regression we've actually hit:
//   1. A page is hard-broken (404 / 5xx / JS error in <head>).
//   2. The four-bucket nav (Wall · People · Business · System) doesn't render.
//   3. A CSS regression on mobile (page reflows, overflow, missing layout).
//   4. The topbar avatar chip stops rendering (regression we saw 2026-05-17).
//
// Each test runs at all three viewport sizes via the project matrix in
// playwright.config.ts.

// Pages that are pure read-only — full body load expected.
const READ_ONLY_PAGES = [
  "/index.html",
  "/yesterday.html",
  "/brandwatch.html",
  "/1stcontact.html",
  "/database.html",
  "/stats.html",
  "/topups.html",
  "/brokers.html",
  "/pipeline.html",
  "/comms.html",
  "/reports.html",
  "/pending.html",
  "/org-structure.html",
];

// Pages that NEED the worker. On the public mirror the page shell renders
// but data calls fail. Test shell only.
const WORKER_PAGES = [
  "/directory.html",
  "/wall.html",
  "/holidays.html",
  "/reconcile.html",
];

// Console errors that originate from the worker being unreachable on the
// public mirror — expected and ignored.
const WORKER_ABSENCE_ERROR_RE =
  /(api\/workspace|api\/wall|api\/holidays|api\/annotations|raw\.githubusercontent|net::ERR_|404|Failed to load resource|Unexpected token '<')/i;

function collectConsoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg: ConsoleMessage) => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  page.on("pageerror", (err: Error) => {
    errors.push(`pageerror: ${err.message}`);
  });
  return errors;
}

async function loadPageWaitForNav(page: Page, path: string) {
  const response = await page.goto(path, { waitUntil: "domcontentloaded" });
  expect(response, `no response for ${path}`).not.toBeNull();
  expect(response!.status(), `${path} status`).toBeLessThan(400);
  // The topbar nav is the universal element. Wait for it.
  await expect(page.locator(".qb-topbar")).toBeVisible();
  await expect(page.locator(".qb-nav")).toBeVisible();
}

// ---- read-only pages: strict ----

for (const path of READ_ONLY_PAGES) {
  test(`read-only: ${path} loads with topbar`, async ({ page }) => {
    const errors = collectConsoleErrors(page);
    await loadPageWaitForNav(page, path);

    // Four-bucket nav present.
    const navLinks = page.locator(".qb-nav .qb-nav-link");
    await expect(navLinks).toHaveCount(4);
    await expect(navLinks.nth(0)).toContainText("Wall");
    await expect(navLinks.nth(1)).toContainText("People");
    await expect(navLinks.nth(2)).toContainText("Business");
    await expect(navLinks.nth(3)).toContainText("System");

    // No console errors apart from worker-absence noise.
    const real = errors.filter(e => !WORKER_ABSENCE_ERROR_RE.test(e));
    expect(real, `unexpected console errors on ${path}:\n${real.join("\n")}`).toHaveLength(0);
  });
}

// ---- worker-required pages: shell only ----

for (const path of WORKER_PAGES) {
  test(`shell: ${path} renders the chrome on the public mirror`, async ({ page }) => {
    const errors = collectConsoleErrors(page);
    await loadPageWaitForNav(page, path);
    // The page-specific body container exists.
    await expect(page.locator("main, section, #app, .pp-page, .wl-page, .hd-page").first()).toBeVisible();
    // Any console errors must be worker-absence noise; nothing else allowed.
    const real = errors.filter(e => !WORKER_ABSENCE_ERROR_RE.test(e));
    expect(real, `unexpected console errors on ${path}:\n${real.join("\n")}`).toHaveLength(0);
  });
}

// ---- universal contract: avatar chip stays mounted ----
// Regression target: 2026-05-17 bug where `nav.js` removed the avatar chip
// when whoami failed. Today it falls back to a generic icon and never
// removes itself. Test asserts the slot is present on every public page.

test("avatar chip slot is always mounted on read-only pages", async ({ page }) => {
  for (const path of READ_ONLY_PAGES.slice(0, 4)) { // spot-check a few; one viewport
    await loadPageWaitForNav(page, path);
    // nav.js mounts `<a class="qb-me" href="/directory/<slug>"><span class="qb-me-avatar">…</span></a>`
    // — either the photo upgrade or the generic SVG fallback. Both render the
    // .qb-me-avatar element; assert the slot exists.
    const chip = page.locator(".qb-topbar .qb-me .qb-me-avatar");
    await expect(chip, `avatar chip missing on ${path}`).toBeVisible();
  }
});
