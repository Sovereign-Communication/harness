"use strict";

const path = require("node:path");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");

const cli = path.join(__dirname, "node_modules", "@playwright", "test", "cli.js");

function runPlaywright(args, env = process.env) {
  return spawnSync(process.execPath, [cli, "test", "--workers=1", ...args], {
    cwd: __dirname,
    env,
    encoding: "utf8",
  });
}

const negative = runPlaywright(["--grep", "\\[negative-control\\]"]);
const negativeOutput = (negative.stdout || "") + (negative.stderr || "");
const negativeConfirmed = negative.status === 1
  && negativeOutput.includes("negative-control")
  && /Expected substring:\s*"Primary: free"/.test(negativeOutput)
  && /Received string:\s*"Route: unavailable"/.test(negativeOutput);

if (!negativeConfirmed) {
  process.stderr.write(
    "Negative control did not prove that the broken settings journey fails its expected UI assertion.\n",
  );
  process.exit(1);
}

process.stdout.write(
  "Negative control confirmed: the intentionally broken settings journey failed its UI assertion.\n",
);

const coveragePath = path.join(__dirname, "ui-coverage.json");
try { fs.rmSync(coveragePath, { force: true }); } catch (_error) {}
const journeyEnv = { ...process.env, HARNESS_UI_COVERAGE_OUT: coveragePath };
const journeys = runPlaywright(["--grep-invert", "\\[negative-control\\]"], journeyEnv);
if (journeys.stdout) process.stdout.write(journeys.stdout);
if (journeys.stderr) process.stderr.write(journeys.stderr);
if (journeys.error) {
  process.stderr.write("Could not launch the Playwright smoke suite.\n");
  process.exit(1);
}
if (journeys.status !== 0) process.exit(journeys.status === null ? 1 : journeys.status);

let coverage;
try {
  coverage = JSON.parse(fs.readFileSync(coveragePath, "utf8"));
} catch (_error) {
  process.stderr.write("The Playwright journeys did not write their browser coverage report.\n");
  process.exit(1);
}
const required = [
  "harness/ui/app.js",
  "harness/ui/panes.js",
  "harness/ui/panes.css",
  "harness/ui/index.html",
];
for (const rel of required) {
  if (!Array.isArray(coverage.modules?.[rel]) || coverage.modules[rel].length === 0) {
    process.stderr.write("Browser coverage is missing a required UI asset.\n");
    process.exit(1);
  }
}
process.stdout.write("Browser line coverage emitted for the served UI assets.\n");
