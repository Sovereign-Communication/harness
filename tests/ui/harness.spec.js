const { test, expect } = require("@playwright/test");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");

const repoRoot = path.resolve(__dirname, "../..");
const fixturePath = path.join(__dirname, "serve_fixture.py");
const sessionToken = "test-session-token";
let configDir;
let baseUrl;
let ownerToken;
let serverProcess;

async function findFreePort() {
  const listener = net.createServer();
  await new Promise((resolve, reject) => {
    listener.once("error", reject);
    listener.listen(0, "127.0.0.1", resolve);
  });
  const port = listener.address().port;
  await new Promise((resolve, reject) => listener.close((error) => {
    if (error) reject(error);
    else resolve();
  }));
  return port;
}

async function waitForServer() {
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    if (serverProcess.exitCode !== null) {
      throw new Error("Harness CLI fixture exited before the UI became ready.");
    }
    try {
      const response = await fetch(baseUrl);
      if (response.status === 200) return;
    } catch (_error) {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Harness CLI fixture did not serve the UI within 20 seconds.");
}

async function signInOwner(page) {
  await page.goto(baseUrl + "#" + ownerToken);
  await expect(page.locator("#auth-text")).toHaveText("Connected");
  await expect(page.locator("#auth-modal")).toBeHidden();
  expect(new URL(page.url()).hash).toBe("");
  expect(await page.locator("body").innerText()).not.toContain(ownerToken);
}

test.beforeAll(async () => {
  configDir = fs.mkdtempSync(path.join(os.tmpdir(), "harness-ui-smoke-"));
  const port = await findFreePort();
  baseUrl = "http://127.0.0.1:" + port + "/";
  const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  const env = { ...process.env };
  for (const name of Object.keys(env)) {
    if (name.startsWith("HARNESS_") || name.endsWith("_API_KEY") || name.endsWith("_TOKEN")) {
      delete env[name];
    }
  }
  Object.assign(env, {
    HARNESS_CONFIG_DIR: configDir,
    HOME: configDir,
    USERPROFILE: configDir,
    PYTHONUTF8: "1",
  });
  env.PYTHONPATH = [repoRoot, env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  serverProcess = spawn(python, [fixturePath, String(port), configDir], {
    cwd: repoRoot,
    env,
    stdio: "ignore",
  });
  await new Promise((resolve, reject) => {
    serverProcess.once("spawn", resolve);
    serverProcess.once("error", reject);
  });
  await waitForServer();
  ownerToken = fs.readFileSync(path.join(configDir, "desktop_token"), "utf8").trim();
  expect(ownerToken.length).toBeGreaterThanOrEqual(32);
  expect(fs.readFileSync(path.join(configDir, "gui-open.url"), "utf8"))
    .toContain("#" + ownerToken);
});

test.afterAll(async () => {
  if (serverProcess && serverProcess.exitCode === null) {
    serverProcess.kill();
    await new Promise((resolve) => serverProcess.once("exit", resolve));
  }
  if (configDir) {
    const target = path.resolve(configDir);
    const tempRoot = path.resolve(os.tmpdir());
    if (path.dirname(target) === tempRoot
        && path.basename(target).startsWith("harness-ui-smoke-")) {
      fs.rmSync(target, { recursive: true, force: true });
    }
  }
});

test("01 launch, load, and one-time browser pairing", async ({ page }) => {
  const requestUrls = [];
  page.on("request", (request) => requestUrls.push(request.url()));
  await page.goto(baseUrl);
  await expect(page.locator("#auth-modal")).toBeVisible();
  await expect(page.getByText("One-time pairing code", { exact: true })).toBeVisible();
  await expect(page.locator("#auth-connect-help")).toContainText("gui-open.url");
  await expect(page.locator("#auth-connect-help")).toContainText("harness serve --open");

  const ownerPage = await page.context().newPage();
  await signInOwner(ownerPage);
  await ownerPage.locator("#btn-auth-toggle").click();
  await expect(ownerPage.locator("#auth-title")).toHaveText("Pair another browser");
  await ownerPage.locator("#btn-create-pair").click();
  const codeOutput = ownerPage.locator("#auth-pair-code");
  await expect(codeOutput).toHaveText(/[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{5}-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{5}/);
  const code = await codeOutput.textContent();
  expect(code).not.toContain(ownerToken);

  await page.locator("#auth-pair-input").fill(code);
  await page.locator("#btn-connect-pair").click();
  await expect(page.locator("#auth-modal")).toBeHidden();
  await expect(page.locator("#auth-text")).toHaveText("Connected");
  expect(await page.locator("body").innerText()).not.toContain(ownerToken);
  expect(requestUrls.every((url) => !url.includes(ownerToken))).toBe(true);

  const duplicate = await page.evaluate(async (pairCode) => {
    const response = await fetch("/api/auth/pair/redeem", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: pairCode.replace(/-/g, "") }),
    });
    return response.status;
  }, code);
  expect(duplicate).toBe(401);

  await page.reload();
  await expect(page.locator("#auth-text")).toHaveText("Connected");
  await expect(page.locator("#auth-modal")).toBeHidden();
  const runtime = await page.evaluate(() => ({
    origin: window.location.origin,
    electron: typeof window.require !== "undefined",
    webview: typeof window.pywebview !== "undefined",
  }));
  expect(runtime.origin).toBe(baseUrl.slice(0, -1));
  expect(runtime.electron).toBe(false);
  expect(runtime.webview).toBe(false);
  await ownerPage.close();
});

test("02 primary chat request uses the served UI and real CLI route", async ({ page }) => {
  await signInOwner(page);
  const request = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/chat"
      && response.request().method() === "POST";
  });
  await page.locator("#prompt-input").fill("primary request");
  await page.locator("#prompt-input").press("Enter");
  const response = await request;
  expect(response.status()).toBe(201);
  const run = await response.json();
  expect(run.kind).toBe("chat");
  expect(run.id).toMatch(/^[A-Za-z0-9_-]+$/);
});

test("03 driver studio surface loads from the served UI", async ({ page }) => {
  await signInOwner(page);
  await page.locator('[data-pane="studio"]').click();
  await page.locator('.subtool-btn[data-pane="driver"]').click();
  await expect(page.locator("#subtool-body h2")).toHaveText("Driver");
  await expect(page.getByText("Task Request / Goal")).toBeVisible();
  await expect(page.locator("#subtool-body")).toContainText("Perception & Health");
});

test("04 settings and provider status expose posture without credentials", async ({ page }) => {
  await signInOwner(page);
  await expect(page.locator("#route-text")).toContainText("Primary: free");
  await page.locator('[data-pane="studio"]').click();
  await page.locator('.subtool-btn[data-pane="legacy"]').click();
  const settingsRow = page.locator(".pane-legacy-row")
    .filter({ hasText: "/api/settings" });
  await settingsRow.getByRole("button", { name: "Fetch" }).click();
  const output = settingsRow.locator("pre");
  await expect(output).toBeVisible();
  await expect(output).toContainText('"paid_key_present": false');
  await expect(output).toContainText('"jev_api_key": null');
  await expect(output).toContainText('"mcp_auth_token": null');
  await expect(output).not.toContainText(ownerToken);
  await expect(output).not.toContainText(sessionToken);
});

test("05 terminal result is rendered after the real chat dispatch completes", async ({ page }) => {
  await signInOwner(page);
  await page.locator("#prompt-input").fill("terminal result");
  await page.locator("#prompt-input").press("Enter");
  await expect(page.locator("#chat-feed")).toContainText(
    "E2E offline response: terminal result",
    { timeout: 10_000 },
  );
  await expect(page.locator("#chat-feed")).toContainText("Execution complete");
});

test("[negative-control] deliberately broken settings journey fails its UI assertion", async ({ page }) => {
  await page.route("**/api/settings", (route) => route.fulfill({
    status: 503,
    contentType: "application/json",
    body: JSON.stringify({ error: "negative control: settings route unavailable" }),
  }));
  await signInOwner(page);
  // The journey's normal assertion must fail when the settings route is broken.
  await expect(page.locator("#route-text")).toContainText("Primary: free");
});
