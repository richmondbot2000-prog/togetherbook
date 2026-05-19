import { test, expect, Page, ConsoleMessage, Route } from "@playwright/test";

// Functional coverage for the Person profile Accounts tab.
//
// The static smoke server has no Cloudflare Worker, so every /api/workspace/*
// call is stubbed via page.route(). Each scenario builds its own people +
// google-accounts + annotations + pending-transfers + whoami fixtures so the
// rendered DOM is fully deterministic. POST stubs capture the request body
// per scenario and the test asserts what landed on the wire.
//
// Tab reached via /user.html?email=...&tab=accounts. The /directory/<slug>
// SPA-shim path needs 404.html and only fires on GitHub Pages; we hit
// user.html directly so the local http-server can serve it.

type PersonFixture = {
  id: number;
  url_slug: string;
  name: string;
  given?: string;
  family?: string;
  main_google_email: string;
  alt_google_emails?: string[];
  external_google_email?: string;
  auth0_id?: string;
  access_level?: string;
  suspended?: boolean;
  on_payroll?: boolean;
  role?: string;
};

type AccountFixture = {
  id: number;
  person_id: number;
  email: string;
  tenant: "letme" | "together" | "external";
  is_primary?: boolean;
  google_user_id?: string;
  suspended?: boolean;
  deletion_time?: string;
  aliases?: string[];
  aliases_editable?: string[];
};

type Scenario = {
  people: PersonFixture[];
  accounts: AccountFixture[];
  pendingTransfers?: { source_email: string; target_email?: string }[];
  annotations?: Record<string, { forward_to?: string; directory_photo_uploaded_at?: string }>;
  adminEmails?: string[];
  whoami: { email: string; is_admin: boolean };
};

type StubbedRequest = { action: string; body: any };

async function stubProfile(page: Page, scen: Scenario): Promise<StubbedRequest[]> {
  const captured: StubbedRequest[] = [];

  const ok = (data: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(data) });

  await page.route(/\/api\/workspace\/table\?file=people/, (route: Route) => route.fulfill(ok({ people: scen.people })));
  await page.route(/\/api\/workspace\/table\?file=google-accounts/, (route: Route) => route.fulfill(ok({ records: scen.accounts })));
  await page.route(/\/api\/workspace\/table\?file=payroll-data/, (route: Route) => route.fulfill(ok({ records: [] })));
  await page.route(/\/api\/workspace\/table\?file=warehouse-activity/, (route: Route) => route.fulfill(ok({ records: [] })));
  await page.route(/\/api\/workspace\/payroll\b/, (route: Route) => route.fulfill(ok({ rows: [] })));
  await page.route(/\/api\/workspace\/whoami\b/, (route: Route) => route.fulfill(ok({ ok: true, ...scen.whoami })));

  await page.route(/\/staff\.json(\?|$)/, (route: Route) => route.fulfill(ok({ users: [] })));
  await page.route(/\/wall\.json(\?|$)/, (route: Route) => route.fulfill(ok({ posts: [] })));
  await page.route(/\/annotations\.json(\?|$)/, (route: Route) => route.fulfill(ok({ annotations: scen.annotations || {} })));
  await page.route(/\/admins\.json(\?|$)/, (route: Route) => route.fulfill(ok({ admins: scen.adminEmails || [] })));
  await page.route(/\/pending-transfers\.json(\?|$)/, (route: Route) => route.fulfill(ok({ entries: scen.pendingTransfers || [] })));
  await page.route(/\/workspace-actions\.json(\?|$)/, (route: Route) => route.fulfill(ok({ actions: [] })));

  // Generic POST capture for every other /api/workspace/<action>.
  await page.route(/\/api\/workspace\/[a-z0-9-]+$/, async (route: Route) => {
    const req = route.request();
    if (req.method() !== "POST") return route.fallback();
    const url = new URL(req.url());
    const action = url.pathname.replace(/^\/api\/workspace\//, "");
    let body: any = {};
    try { body = req.postDataJSON(); } catch { body = {}; }
    captured.push({ action, body });
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true }) });
  });

  return captured;
}

function collectConsoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg: ConsoleMessage) => { if (msg.type() === "error") errors.push(msg.text()); });
  page.on("pageerror", (err: Error) => { errors.push("pageerror: " + err.message); });
  return errors;
}

const NOISE_RE = /(api\/workspace|api\/wall|api\/holidays|api\/annotations|raw\.githubusercontent|net::ERR_|404|Failed to load resource|Unexpected token)/i;

async function gotoAccounts(page: Page, email: string) {
  // Stub confirm() + alert() before the page script runs so destructive-
  // action handlers proceed past their confirm checks deterministically.
  await page.addInitScript(() => {
    (window as any).__alerts = [];
    Object.defineProperty(window, "confirm", {
      configurable: true,
      writable: true,
      value: () => true,
    });
    Object.defineProperty(window, "alert", {
      configurable: true,
      writable: true,
      value: (msg: string) => { (window as any).__alerts.push(msg); },
    });
  });
  const resp = await page.goto("/user.html?email=" + encodeURIComponent(email) + "&tab=accounts", { waitUntil: "domcontentloaded" });
  expect(resp).not.toBeNull();
  expect(resp!.status()).toBeLessThan(400);
  await page.waitForSelector(".up-acct, .up-empty", { timeout: 8000 });
}

function adminViewer(extra: Partial<Scenario> = {}): Scenario {
  return {
    people: [
      {
        id: 1, url_slug: "jane.doe", name: "Jane Doe", given: "Jane", family: "Doe",
        main_google_email: "jane.doe@letme.com",
        alt_google_emails: ["jane.doe@togetherloans.com"],
        external_google_email: "jane.doe@gmail.com",
        auth0_id: "auth0|abc123",
        access_level: "staff",
      },
    ],
    accounts: [
      { id: 11, person_id: 1, email: "jane.doe@letme.com", tenant: "letme", is_primary: true,
        google_user_id: "g-jane-letme-001",
        aliases: ["jane.doe@letme.com", "j.doe@letme.com", "jane@letme.co.uk"],
        aliases_editable: ["j.doe@letme.com"] },
      { id: 12, person_id: 1, email: "jane.doe@togetherloans.com", tenant: "together",
        google_user_id: "g-jane-tl-002" },
      { id: 13, person_id: 1, email: "jane.doe@gmail.com", tenant: "external" },
    ],
    adminEmails: ["admin@letme.com", "jane.doe@letme.com"],
    whoami: { email: "admin@letme.com", is_admin: true },
    annotations: {
      "jane.doe@letme.com": { forward_to: "boss@letme.com" },
    },
    ...extra,
  };
}

test.describe("Accounts tab - render: admin viewer + full Person", () => {
  test.beforeEach(async ({ page }) => {
    await stubProfile(page, adminViewer());
    await gotoAccounts(page, "jane.doe@letme.com");
  });

  test("renders one row per non-alias-only account in primary -> letme -> together -> external order", async ({ page }) => {
    const rows = page.locator(".up-acct");
    await expect(rows).toHaveCount(3);
    await expect(rows.nth(0).locator(".up-acct-email")).toHaveText("jane.doe@letme.com");
    await expect(rows.nth(1).locator(".up-acct-email")).toHaveText("jane.doe@togetherloans.com");
    await expect(rows.nth(2).locator(".up-acct-email")).toHaveText("jane.doe@gmail.com");
  });

  test("primary Letme row shows Live + Primary + Letme + forwarding-to badges", async ({ page }) => {
    const row = page.locator('.up-acct[data-acc-email="jane.doe@letme.com"]');
    const badges = row.locator(".up-acct-badges");
    await expect(badges).toContainText("Live");
    await expect(badges).toContainText("Primary");
    await expect(badges).toContainText("Letme");
    await expect(badges).toContainText("boss@letme.com");
  });

  test("Together row shows Together tenant badge", async ({ page }) => {
    const tl = page.locator('.up-acct[data-acc-email="jane.doe@togetherloans.com"] .up-acct-badges');
    await expect(tl).toContainText("Together");
  });

  test("External Gmail row shows External Gmail + Gmail badges", async ({ page }) => {
    const ext = page.locator('.up-acct[data-acc-email="jane.doe@gmail.com"] .up-acct-badges');
    await expect(ext).toContainText("External Gmail");
    await expect(ext).toContainText("Gmail");
  });
});

test.describe("Accounts tab - alias chips", () => {
  test.beforeEach(async ({ page }) => {
    await stubProfile(page, adminViewer());
    await gotoAccounts(page, "jane.doe@letme.com");
  });

  test("editable alias chip has the remove + to-group buttons", async ({ page }) => {
    const row = page.locator('.up-acct[data-acc-email="jane.doe@letme.com"]');
    const editable = row.locator(".up-acct-alias-chip:not(.up-acct-alias-chip--locked)");
    await expect(editable).toHaveCount(1);
    await expect(editable.locator("[data-acc-alias-remove]")).toHaveCount(1);
    await expect(editable.locator("[data-acc-alias-group]")).toHaveCount(1);
  });

  test("locked alias chip has no remove or to-group button", async ({ page }) => {
    const row = page.locator('.up-acct[data-acc-email="jane.doe@letme.com"]');
    const locked = row.locator(".up-acct-alias-chip--locked");
    await expect(locked).toHaveCount(1);
    await expect(locked.locator("[data-acc-alias-remove]")).toHaveCount(0);
    await expect(locked.locator("[data-acc-alias-group]")).toHaveCount(0);
  });
});

test.describe("Accounts tab - action wiring (admin viewer, live row)", () => {
  let captured: StubbedRequest[];
  test.beforeEach(async ({ page }) => {
    captured = await stubProfile(page, adminViewer());
    await gotoAccounts(page, "jane.doe@letme.com");
  });

  test("Suspend on the Together row POSTs suspend-no-forward with tenant=togetherloans", async ({ page }) => {
    const tl = page.locator('.up-acct[data-acc-email="jane.doe@togetherloans.com"]');
    await tl.locator('[data-acc-action="suspend-now"]').click();
    await page.waitForTimeout(200);
    const hit = captured.find(c => c.action === "suspend-no-forward");
    expect(hit).toBeTruthy();
    expect(hit!.body).toMatchObject({ email: "jane.doe@togetherloans.com", tenant: "togetherloans" });
  });

  test("Turn off forwarding on the primary POSTs disable-forwarding", async ({ page }) => {
    const row = page.locator('.up-acct[data-acc-email="jane.doe@letme.com"]');
    await row.locator('[data-acc-action="disable-forwarding"]').click();
    await page.waitForTimeout(150);
    const hit = captured.find(c => c.action === "disable-forwarding");
    expect(hit).toBeTruthy();
    expect(hit!.body).toMatchObject({ email: "jane.doe@letme.com" });
  });

  test("Delete account POSTs delete-account", async ({ page }) => {
    const tl = page.locator('.up-acct[data-acc-email="jane.doe@togetherloans.com"]');
    await tl.locator('[data-acc-action="delete-now"]').click();
    await page.waitForTimeout(150);
    const hit = captured.find(c => c.action === "delete-account");
    expect(hit).toBeTruthy();
    expect(hit!.body).toMatchObject({ email: "jane.doe@togetherloans.com" });
  });
});

test.describe("Accounts tab - reset-password (bug-fix: password generated client-side)", () => {
  let captured: StubbedRequest[];
  test.beforeEach(async ({ page }) => {
    captured = await stubProfile(page, adminViewer());
    await gotoAccounts(page, "jane.doe@letme.com");
  });

  test("Reset password POSTs reset-password with a non-empty password field", async ({ page }) => {
    const tl = page.locator('.up-acct[data-acc-email="jane.doe@togetherloans.com"]');
    await tl.locator('[data-acc-action="reset-password"]').click();
    await page.waitForTimeout(150);
    const hit = captured.find(c => c.action === "reset-password");
    expect(hit).toBeTruthy();
    expect(typeof hit!.body.password).toBe("string");
    expect((hit!.body.password as string).length).toBeGreaterThanOrEqual(12);
  });

  test("Reset password alert surfaces the same password we sent", async ({ page }) => {
    const tl = page.locator('.up-acct[data-acc-email="jane.doe@togetherloans.com"]');
    await tl.locator('[data-acc-action="reset-password"]').click();
    await page.waitForTimeout(200);
    const hit = captured.find(c => c.action === "reset-password");
    const alerts: string[] = await page.evaluate(() => (window as any).__alerts || []);
    expect(hit).toBeTruthy();
    expect(alerts.some(a => a.includes(hit!.body.password))).toBe(true);
  });
});

test.describe("Accounts tab - Make primary (rename-user)", () => {
  let captured: StubbedRequest[];
  test.beforeEach(async ({ page }) => {
    captured = await stubProfile(page, adminViewer());
    await gotoAccounts(page, "jane.doe@letme.com");
  });

  test("clicking Make primary on the alt POSTs rename-user with current+new emails", async ({ page }) => {
    const tl = page.locator('.up-acct[data-acc-email="jane.doe@togetherloans.com"]');
    await tl.locator('[data-acc-action="promote-primary"]').click();
    await page.waitForTimeout(150);
    const hit = captured.find(c => c.action === "rename-user");
    expect(hit).toBeTruthy();
    expect(hit!.body).toMatchObject({
      current_email: "jane.doe@letme.com",
      new_email: "jane.doe@togetherloans.com",
    });
  });
});

test.describe("Accounts tab - inline forms (target picker)", () => {
  let captured: StubbedRequest[];
  test.beforeEach(async ({ page }) => {
    captured = await stubProfile(page, adminViewer());
    await gotoAccounts(page, "jane.doe@letme.com");
  });

  test("Add forwarding opens an inline form and POSTs add-forwarding with route_to", async ({ page }) => {
    const tl = page.locator('.up-acct[data-acc-email="jane.doe@togetherloans.com"]');
    await tl.locator('[data-acc-action="forward"]').click();
    const form = tl.locator(".up-acct-form");
    await expect(form).toBeVisible();
    await form.locator("[data-acc-target]").fill("cover@togetherloans.com");
    await form.locator("[data-acc-confirm]").click();
    await page.waitForTimeout(150);
    const hit = captured.find(c => c.action === "add-forwarding");
    expect(hit).toBeTruthy();
    expect(hit!.body).toMatchObject({ email: "jane.doe@togetherloans.com", route_to: "cover@togetherloans.com" });
  });

  test("Transfer Drive ownership POSTs data-transfer with target_email", async ({ page }) => {
    const tl = page.locator('.up-acct[data-acc-email="jane.doe@togetherloans.com"]');
    await tl.locator('[data-acc-action="transfer-drive"]').click();
    const form = tl.locator(".up-acct-form");
    await expect(form).toBeVisible();
    await form.locator("[data-acc-target]").fill("succ@letme.com");
    await form.locator("[data-acc-confirm]").click();
    await page.waitForTimeout(150);
    const hit = captured.find(c => c.action === "data-transfer");
    expect(hit).toBeTruthy();
    expect(hit!.body).toMatchObject({ email: "jane.doe@togetherloans.com", target_email: "succ@letme.com" });
  });

  test("Convert to group POSTs convert-to-group with forward_to", async ({ page }) => {
    const tl = page.locator('.up-acct[data-acc-email="jane.doe@togetherloans.com"]');
    await tl.locator('[data-acc-action="convert-to-group"]').click();
    const form = tl.locator(".up-acct-form");
    await expect(form).toBeVisible();
    await form.locator("[data-acc-forward]").fill("group.member@togetherloans.com");
    await form.locator("[data-acc-confirm]").click();
    await page.waitForTimeout(150);
    const hit = captured.find(c => c.action === "convert-to-group");
    expect(hit).toBeTruthy();
    expect(hit!.body).toMatchObject({ email: "jane.doe@togetherloans.com", forward_to: "group.member@togetherloans.com" });
  });
});

test.describe("Accounts tab - alias actions", () => {
  let captured: StubbedRequest[];
  test.beforeEach(async ({ page }) => {
    captured = await stubProfile(page, adminViewer());
    await gotoAccounts(page, "jane.doe@letme.com");
  });

  test("+ Add alias opens form and POSTs user-alias-add", async ({ page }) => {
    const row = page.locator('.up-acct[data-acc-email="jane.doe@letme.com"]');
    await row.locator("[data-acc-alias-add]").click();
    const form = row.locator(".up-acct-form");
    await expect(form).toBeVisible();
    await form.locator("[data-acc-alias-input]").fill("jane.alias@letme.com");
    await form.locator("[data-acc-confirm]").click();
    await page.waitForTimeout(150);
    const hit = captured.find(c => c.action === "user-alias-add");
    expect(hit).toBeTruthy();
    expect(hit!.body).toMatchObject({ user_email: "jane.doe@letme.com", alias: "jane.alias@letme.com" });
  });

  test("cross button on editable alias chip POSTs user-alias-remove", async ({ page }) => {
    const row = page.locator('.up-acct[data-acc-email="jane.doe@letme.com"]');
    await row.locator('[data-acc-alias-remove="j.doe@letme.com"]').click();
    await page.waitForTimeout(150);
    const hit = captured.find(c => c.action === "user-alias-remove");
    expect(hit).toBeTruthy();
    expect(hit!.body).toMatchObject({ user_email: "jane.doe@letme.com", alias: "j.doe@letme.com" });
  });

  test("to-group button POSTs alias-to-group with group_name", async ({ page }) => {
    const row = page.locator('.up-acct[data-acc-email="jane.doe@letme.com"]');
    await row.locator('[data-acc-alias-group="j.doe@letme.com"]').click();
    const form = row.locator(".up-acct-form");
    await expect(form).toBeVisible();
    await form.locator("[data-acc-group-name]").fill("J Doe Team");
    await form.locator("[data-acc-confirm]").click();
    await page.waitForTimeout(150);
    const hit = captured.find(c => c.action === "alias-to-group");
    expect(hit).toBeTruthy();
    expect(hit!.body).toMatchObject({ user_email: "jane.doe@letme.com", alias: "j.doe@letme.com", group_name: "J Doe Team" });
  });
});

test.describe("Accounts tab - unlink + isMine suppression", () => {
  test("Unlink button (admin only) POSTs google-account-delete with the row id", async ({ page }) => {
    const captured = await stubProfile(page, adminViewer());
    await gotoAccounts(page, "jane.doe@letme.com");
    const tl = page.locator('.up-acct[data-acc-email="jane.doe@togetherloans.com"]');
    const unlink = tl.locator('[data-acc-unlink="12"]');
    await expect(unlink).toHaveCount(1);
    await unlink.click();
    await page.waitForTimeout(150);
    const hit = captured.find(c => c.action === "google-account-delete");
    expect(hit).toBeTruthy();
    expect(hit!.body).toMatchObject({ id: "12" });
  });

  test("isMine suppresses suspend / promote / convert / transfer / delete on the viewer's own row", async ({ page }) => {
    await stubProfile(page, { ...adminViewer(), whoami: { email: "jane.doe@letme.com", is_admin: true } });
    await gotoAccounts(page, "jane.doe@letme.com");
    const row = page.locator('.up-acct[data-acc-email="jane.doe@letme.com"]');
    for (const a of ["suspend-now", "promote-primary", "convert-to-group", "transfer-drive", "delete-now"]) {
      await expect(row.locator('[data-acc-action="' + a + '"]')).toHaveCount(0);
    }
    const tl = page.locator('.up-acct[data-acc-email="jane.doe@togetherloans.com"]');
    await expect(tl.locator('[data-acc-action="suspend-now"]')).toHaveCount(1);
  });
});

test.describe("Accounts tab - state-driven button sets", () => {
  test("suspended account: Unsuspend + Delete; no Suspend / Make primary / Transfer Drive", async ({ page }) => {
    const scen = adminViewer();
    scen.accounts[1].suspended = true;
    await stubProfile(page, scen);
    await gotoAccounts(page, "jane.doe@letme.com");
    const tl = page.locator('.up-acct[data-acc-email="jane.doe@togetherloans.com"]');
    await expect(tl.locator('[data-acc-action="unsuspend"]')).toHaveCount(1);
    await expect(tl.locator('[data-acc-action="delete-now"]')).toHaveCount(1);
    await expect(tl.locator('[data-acc-action="suspend-now"]')).toHaveCount(0);
    await expect(tl.locator('[data-acc-action="promote-primary"]')).toHaveCount(0);
    await expect(tl.locator('[data-acc-action="transfer-drive"]')).toHaveCount(0);
    await expect(tl.locator(".up-acct-badges")).toContainText("Suspended");
  });

  test("Unsuspend POSTs the unsuspend action with { email }", async ({ page }) => {
    const scen = adminViewer();
    scen.accounts[1].suspended = true;
    const captured = await stubProfile(page, scen);
    await gotoAccounts(page, "jane.doe@letme.com");
    const tl = page.locator('.up-acct[data-acc-email="jane.doe@togetherloans.com"]');
    await tl.locator('[data-acc-action="unsuspend"]').click();
    await page.waitForTimeout(150);
    const hit = captured.find(c => c.action === "unsuspend");
    expect(hit).toBeTruthy();
    expect(hit!.body).toMatchObject({ email: "jane.doe@togetherloans.com" });
  });
});

test.describe("Accounts tab - deleted account: Recover (bug-fix: user_id wired)", () => {
  test("deleted row shows only Recover, other actions suppressed", async ({ page }) => {
    const scen = adminViewer();
    scen.accounts[1].deletion_time = "2026-05-10T10:00:00Z";
    scen.accounts[1].google_user_id = "g-jane-tl-002";
    await stubProfile(page, scen);
    await gotoAccounts(page, "jane.doe@letme.com");
    const tl = page.locator('.up-acct[data-acc-email="jane.doe@togetherloans.com"]');
    await expect(tl.locator(".up-acct-badges")).toContainText("Deleted");
    await expect(tl.locator('[data-acc-action="recover"]')).toHaveCount(1);
    for (const a of ["suspend-now","unsuspend","delete-now","promote-primary","transfer-drive","forward","convert-to-group"]) {
      await expect(tl.locator('[data-acc-action="' + a + '"]')).toHaveCount(0);
    }
  });

  test("Recover POSTs /recover with user_id from the row (worker requires the immutable id)", async ({ page }) => {
    const scen = adminViewer();
    scen.accounts[1].deletion_time = "2026-05-10T10:00:00Z";
    scen.accounts[1].google_user_id = "g-jane-tl-002";
    const captured = await stubProfile(page, scen);
    await gotoAccounts(page, "jane.doe@letme.com");
    const tl = page.locator('.up-acct[data-acc-email="jane.doe@togetherloans.com"]');
    await tl.locator('[data-acc-action="recover"]').click();
    await page.waitForTimeout(150);
    const hit = captured.find(c => c.action === "recover");
    expect(hit).toBeTruthy();
    expect(hit!.body.user_id).toBe("g-jane-tl-002");
  });
});

test.describe("Accounts tab - pending-transfer + alias-only filtering", () => {
  test("pending-transfer account shows Transferring + disabled button + destructive actions suppressed", async ({ page }) => {
    const scen = adminViewer();
    scen.pendingTransfers = [{ source_email: "jane.doe@togetherloans.com" }];
    await stubProfile(page, scen);
    await gotoAccounts(page, "jane.doe@letme.com");
    const tl = page.locator('.up-acct[data-acc-email="jane.doe@togetherloans.com"]');
    await expect(tl.locator(".up-acct-badges")).toContainText("Transferring");
    const transferring = tl.locator(".up-acct-actions button:has-text('Transferring')");
    await expect(transferring).toBeDisabled();
    for (const a of ["suspend-now","delete-now","promote-primary","transfer-drive","convert-to-group"]) {
      await expect(tl.locator('[data-acc-action="' + a + '"]')).toHaveCount(0);
    }
  });

  test("alias-only row (no google_user_id, not primary, not external) is filtered out of the list", async ({ page }) => {
    const scen = adminViewer();
    scen.accounts.push({ id: 99, person_id: 1, email: "ghost-alias@letme.co.uk", tenant: "letme", is_primary: false });
    await stubProfile(page, scen);
    await gotoAccounts(page, "jane.doe@letme.com");
    await expect(page.locator('.up-acct[data-acc-email="ghost-alias@letme.co.uk"]')).toHaveCount(0);
  });
});

test.describe("Accounts tab - minimal Person (only Letme primary)", () => {
  function minimalScen(): Scenario {
    return {
      people: [{ id: 2, url_slug: "min.user", name: "Min User", main_google_email: "min@letme.com" }],
      accounts: [{ id: 21, person_id: 2, email: "min@letme.com", tenant: "letme", is_primary: true, google_user_id: "g-min-1" }],
      adminEmails: ["admin@letme.com"],
      whoami: { email: "admin@letme.com", is_admin: true },
    };
  }

  test("renders one row, no aliases, + Add Letme suppressed, + Add Together/external present", async ({ page }) => {
    await stubProfile(page, minimalScen());
    await gotoAccounts(page, "min@letme.com");
    await expect(page.locator(".up-acct")).toHaveCount(1);
    await expect(page.locator(".up-acct-aliases")).toHaveCount(0);
    await expect(page.locator('[data-acc-add="letme"]')).toHaveCount(0);
    await expect(page.locator('[data-acc-add="together"]')).toHaveCount(1);
    await expect(page.locator('[data-acc-add="external"]')).toHaveCount(1);
  });

  test("+ Add Together opens form and POSTs google-account-set", async ({ page }) => {
    const captured = await stubProfile(page, minimalScen());
    await gotoAccounts(page, "min@letme.com");
    await page.locator('[data-acc-add="together"]').click();
    await page.locator("#upAcctAddEmail").fill("min@togetherloans.com");
    await page.locator("#upAcctAddSave").click();
    await page.waitForTimeout(150);
    const hit = captured.find(c => c.action === "google-account-set");
    expect(hit).toBeTruthy();
    expect(hit!.body).toMatchObject({ person_id: 2, email: "min@togetherloans.com", tenant: "together" });
  });
});

test.describe("Accounts tab - non-admin viewer", () => {
  test("rows render but every admin-only control is hidden", async ({ page }) => {
    const scen = adminViewer({ whoami: { email: "viewer@letme.com", is_admin: false } });
    await stubProfile(page, scen);
    await gotoAccounts(page, "jane.doe@letme.com");
    await expect(page.locator(".up-acct")).toHaveCount(3);
    await expect(page.locator(".up-acct-actions")).toHaveCount(0);
    await expect(page.locator("[data-acc-unlink]")).toHaveCount(0);
    await expect(page.locator(".up-acct-alias-x")).toHaveCount(0);
    await expect(page.locator(".up-acct-alias-group")).toHaveCount(0);
    await expect(page.locator(".up-acct-add-row")).toHaveCount(0);
    await expect(page.locator('[data-edit-field="auth0_id"]')).toHaveCount(0);
  });
});

test.describe("Accounts tab - Auth0 ID card (admin viewer)", () => {
  test("renders an editable input pre-filled with auth0_id and saves via people-set", async ({ page }) => {
    const captured = await stubProfile(page, adminViewer());
    await gotoAccounts(page, "jane.doe@letme.com");
    const card = page.locator('[data-edit-field="auth0_id"]');
    await expect(card).toHaveCount(1);
    const input = card.locator('input[name="auth0_id"]');
    await expect(input).toHaveValue("auth0|abc123");
    await input.fill("auth0|xyz999");
    await card.locator('[data-edit-save="auth0_id"]').click();
    await page.waitForTimeout(200);
    const hit = captured.find(c => c.action === "people-set" && c.body && c.body.auth0_id);
    expect(hit).toBeTruthy();
    expect(hit!.body).toMatchObject({ id: 1, auth0_id: "auth0|xyz999" });
  });
});

test.describe("Accounts tab - page-level invariants", () => {
  test("Accounts tab pill in the tab strip is active", async ({ page }) => {
    await stubProfile(page, adminViewer());
    await gotoAccounts(page, "jane.doe@letme.com");
    await expect(page.locator('[data-tab="accounts"]')).toHaveClass(/is-active/);
  });

  test("no unexpected console errors with full fixtures loaded", async ({ page }) => {
    const errors = collectConsoleErrors(page);
    await stubProfile(page, adminViewer());
    await gotoAccounts(page, "jane.doe@letme.com");
    const real = errors.filter(e => !NOISE_RE.test(e));
    expect(real, "unexpected console errors:\n" + real.join("\n")).toHaveLength(0);
  });
});
