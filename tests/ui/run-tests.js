"use strict";

const path = require("node:path");
const { spawnSync } = require("node:child_process");

const cli = path.join(__dirname, "node_modules", "@playwright", "test", "cli.js");

function runPlaywright(args) {
  return spawnSync(process.execPath, [cli, "test", "--workers=1", ...args], {
    cwd: __dirname,
    env: process.env,
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

const journeys = runPlaywright(["--grep-invert", "\\[negative-control\\]"]);
if (journeys.stdout) process.stdout.write(journeys.stdout);
if (journeys.stderr) process.stderr.write(journeys.stderr);
if (journeys.error) {
  process.stderr.write("Could not launch the Playwright smoke suite.\n");
  process.exit(1);
}
process.exit(journeys.status === null ? 1 : journeys.status);
