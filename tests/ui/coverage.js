"use strict";

const fs = require("node:fs");
const path = require("node:path");
const v8ToIstanbul = require("v8-to-istanbul");

const repoRoot = path.resolve(__dirname, "../..");
const enabled = Boolean(process.env.HARNESS_UI_COVERAGE_OUT);
const sessions = new Map();
const coverage = { schema: 1, executable_lines: {}, modules: {} };

function relativeAsset(url) {
  try {
    const pathname = new URL(url).pathname;
    if (pathname.endsWith("/app.js")) return "harness/ui/app.js";
    if (pathname.endsWith("/panes.js")) return "harness/ui/panes.js";
    if (pathname.endsWith("/panes.css")) return "harness/ui/panes.css";
  } catch (_error) {}
  return null;
}

function mergeLines(rel, executable, executed) {
  const oldExecutable = new Set(coverage.executable_lines[rel] || []);
  const oldExecuted = new Set(coverage.modules[rel] || []);
  for (const line of executable) oldExecutable.add(line);
  for (const line of executed) oldExecuted.add(line);
  coverage.executable_lines[rel] = [...oldExecutable].sort((a, b) => a - b);
  coverage.modules[rel] = [...oldExecuted].sort((a, b) => a - b);
}

function cssLinesInRange(source, start, end) {
  const lines = [];
  let offset = 0;
  for (const [index, line] of source.split("\n").entries()) {
    const next = offset + line.length + 1;
    const value = line.trim();
    if (offset < end && next > start && value
        && !/^[{}]+$/.test(value)
        && !value.startsWith("/*") && !value.startsWith("*")
        && !value.startsWith("*/")) {
      lines.push(index + 1);
    }
    offset = next;
  }
  return lines;
}

function htmlSourceLines(source) {
  return source.split("\n").flatMap((line, index) => {
    const value = line.trim();
    if (!value || value.startsWith("<!--") || value.startsWith("-->")
        || value.startsWith("<!--")) return [];
    return [index + 1];
  });
}

async function startCoverage(page) {
  if (!enabled || sessions.has(page)) return;
  const session = await page.context().newCDPSession(page);
  const stylesheets = new Map();
  const state = { session, stylesheets, htmlLoaded: false };
  session.on("CSS.styleSheetAdded", ({ header }) => {
    stylesheets.set(header.styleSheetId, header);
  });
  page.on("response", (response) => {
    try {
      const request = response.request();
      const pathname = new URL(response.url()).pathname;
      if (request.isNavigationRequest() && (pathname === "/" || pathname === "/index.html")
          && response.status() === 200) state.htmlLoaded = true;
    } catch (_error) {}
  });
  await session.send("Profiler.enable");
  await session.send("Profiler.startPreciseCoverage", {
    callCount: true,
    detailed: true,
  });
  await session.send("DOM.enable");
  await session.send("CSS.enable");
  await session.send("CSS.startRuleUsageTracking");
  sessions.set(page, state);
}

async function collectJavaScript(state) {
  const { result } = await state.session.send("Profiler.takePreciseCoverage");
  const seen = new Set();
  for (const script of result) {
    const rel = relativeAsset(script.url);
    if (!rel) continue;
    const file = path.join(repoRoot, rel.replaceAll("/", path.sep));
    const converter = v8ToIstanbul(file);
    await converter.load();
    converter.applyCoverage(script.functions);
    const report = converter.toIstanbul();
    const entry = report[file] || Object.values(report)[0];
    if (!entry) continue;
    const executable = [];
    const executed = [];
    for (const [id, location] of Object.entries(entry.statementMap)) {
      const line = location.start.line;
      executable.push(line);
      if (entry.s[id] > 0) executed.push(line);
    }
    mergeLines(rel, executable, executed);
    seen.add(rel);
  }
  return seen;
}

async function collectCss(state) {
  const { ruleUsage } = await state.session.send("CSS.stopRuleUsageTracking");
  const byStyle = new Map();
  for (const item of ruleUsage) {
    const entry = byStyle.get(item.styleSheetId) || [];
    entry.push(item);
    byStyle.set(item.styleSheetId, entry);
  }
  const seen = new Set();
  for (const [styleSheetId, header] of state.stylesheets) {
    const rel = relativeAsset(header.sourceURL || "");
    if (rel !== "harness/ui/panes.css") continue;
    const file = path.join(repoRoot, rel.replaceAll("/", path.sep));
    const text = fs.readFileSync(file, "utf8");
    const executable = new Set();
    const executed = new Set();
    for (const item of byStyle.get(styleSheetId) || []) {
      const lines = cssLinesInRange(text, item.startOffset, item.endOffset);
      for (const line of lines) executable.add(line);
      if (item.used) for (const line of lines) executed.add(line);
    }
    mergeLines(rel, executable, executed);
    seen.add(rel);
  }
  return seen;
}

async function finishCoverage() {
  if (!enabled) return;
  for (const [page, state] of [...sessions]) {
    try {
      const seen = await collectJavaScript(state);
      for (const rel of await collectCss(state)) seen.add(rel);
      if (state.htmlLoaded) {
        const rel = "harness/ui/index.html";
        const file = path.join(repoRoot, rel.replaceAll("/", path.sep));
        const lines = htmlSourceLines(fs.readFileSync(file, "utf8"));
        mergeLines(rel, [], lines);
      }
    } finally {
      sessions.delete(page);
      await state.session.detach().catch(() => {});
    }
  }
}

function writeCoverage() {
  if (!enabled) return;
  for (const rel of ["harness/ui/app.js", "harness/ui/panes.js", "harness/ui/panes.css", "harness/ui/index.html"]) {
    if (!coverage.modules[rel]) coverage.modules[rel] = [];
    if (!coverage.executable_lines[rel]) coverage.executable_lines[rel] = [];
  }
  const output = path.resolve(process.env.HARNESS_UI_COVERAGE_OUT);
  fs.writeFileSync(output, JSON.stringify(coverage, null, 2) + "\n", "utf8");
}

module.exports = { startCoverage, finishCoverage, writeCoverage };
