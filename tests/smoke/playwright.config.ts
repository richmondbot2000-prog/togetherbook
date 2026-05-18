import { defineConfig, devices } from "@playwright/test";
import path from "path";

// Smoke suite for the TogetherBook working tree.
//
// Why a local static server, not the live URL?
// - The canonical URL `book.togetherbook.net` sits behind Cloudflare Access.
//   No auth in CI without a service token.
// - The github.io mirror 301-redirects to the gated URL (because of the
//   `CNAME` file in the repo — Pages enforces the canonical hostname).
// So the smoke pulls in `http-server` and serves the repo root on localhost.
// That has two upsides over hitting a live URL: (1) tests the EXACT commit
// in flight, not whatever's deployed; (2) no flake from CDN propagation.
//
// Pages that depend on the worker (Directory / Wall / Holidays / Reconcile)
// have their data calls land on a null server — the test code filters out
// the resulting console noise so the shell-only assertion still passes.

const PORT = Number(process.env.SMOKE_PORT) || 4173;
const BASE_URL = process.env.SMOKE_BASE_URL || `http://127.0.0.1:${PORT}`;
const REPO_ROOT = path.resolve(__dirname, "..", "..");

export default defineConfig({
  testDir: ".",
  fullyParallel: true,
  retries: 1,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  timeout: 45_000,
  expect: { timeout: 8_000 },
  webServer: {
    // `npx http-server` serves static files. `-c-1` disables caching (so
    // hard-refresh isn't needed between local iterations). `--silent` keeps
    // the test output clean.
    command: `npx --yes http-server "${REPO_ROOT}" -p ${PORT} -c-1 --silent`,
    url: `${BASE_URL}/index.html`,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
  use: {
    baseURL: BASE_URL,
    headless: true,
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "desktop-chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } },
    },
    {
      // Tablet width. Forced to Chromium (the iPad device descriptor defaults
      // to WebKit which we don't keep installed in CI).
      name: "tablet-chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 810, height: 1080 },
        userAgent: "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        hasTouch: true,
      },
    },
    {
      // Mobile width. Same Chromium-on-touch trick.
      name: "mobile-chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 390, height: 844 },
        userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        hasTouch: true,
        isMobile: true,
        deviceScaleFactor: 3,
      },
    },
  ],
});
