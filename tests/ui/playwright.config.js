const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: ".",
  testMatch: "harness.spec.js",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  reporter: "list",
  timeout: 30_000,
  expect: { timeout: 5_000 },
  use: {
    browserName: "chromium",
    headless: true,
    screenshot: "off",
    trace: "off",
    video: "off",
  },
});
