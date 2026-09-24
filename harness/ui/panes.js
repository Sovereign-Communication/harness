// SITE-8 UI panes: consolidates the manual-fetch JSON surface into rendered
// panes, and adds the Proof Bench view (local mode). Vanilla ES module —
// same ethos as the Proof Bench site; no framework, no build.
// The existing chat app stays untouched as the default "Chat" tab.

const $ = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2), v);
    } else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const child of children) if (child) node.append(child);
  return node;
};

const money = (v) => v === null || v === undefined ? "—"
  : v === 0 ? "$0" : v < 0.01 ? `$${v.toExponential(1)}`
  : v < 1 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
const pct = (v) => v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`;

async function api(path, opts) {
  const res = await fetch(path, Object.assign(
    { headers: { accept: "application/json" } }, opts));
  let body = null;
  try { body = await res.json(); } catch (_e) { /* no body */ }
  if (!res.ok) {
    const msg = (body && body.error) ? body.error : `${path} -> HTTP ${res.status}`;
    throw new Error(msg);
  }
  return body;
}

async function postJson(path, payload) {
  return api(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// Poll a dispatched /api/runs/{id} until it stops running (simple pane
// forms don't need the live event stream the chat tab uses -- just the
// terminal result).
async function pollRunResult(runId, { intervalMs = 400, timeoutMs = 120000 } = {}) {
  const started = Date.now();
  for (;;) {
    const res = await api(`/api/runs/${runId}/result`);
    if (res.status !== "running") return res;
    if (Date.now() - started > timeoutMs) {
      throw new Error(`run ${runId} still running after ${timeoutMs}ms`);
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

function table(headers, rows) {
  return $("table", {},
    $("thead", {}, $("tr", {}, headers.map(h => $("th", {}, h)))),
    $("tbody", {}, rows.map(cells => $("tr", {},
      cells.map(c => $("td", { class: typeof c === "number" ? "num" : "" },
        String(c)))))));
}

// ---- Proof pane (SITE local mode) ---------------------------------------

async function renderProof(root) {
  root.append($("h2", {}, "Proof Bench — this harness's evidence"));
  let snapshot;
  try {
    snapshot = await api("/api/snapshot");
  } catch (err) {
    root.append($("p", { class: "muted" }, "Snapshot unavailable: ",
      $("code", {}, String(err.message))));
    return;
  }
  const m = snapshot.sessions?.[0]?.metrics;
  if (!m) {
    root.append($("p", { class: "muted" },
      "No runs in the local ledger yet — run the efficiency bench: ",
      $("code", {}, "harness bench bench/manifests/efficiency")));
    return;
  }
  const depth = m.run_depth_distribution || {};
  const maxRuns = Math.max(...Object.values(depth).map(d => d.runs), 1);
  root.append(
    $("h3", {}, "Run depth (the hourglass)"),
    $("div", { class: "pane-bars" }, Object.entries(depth).map(([tier, d]) =>
      $("div", { class: "pane-bar-row" },
        $("span", { class: "pane-bar-label" }, tier),
        $("div", { class: "pane-track" },
          $("div", { class: `pane-fill${tier === "T3" ? " t3" : ""}`,
            style: `width:${Math.max(2, (d.runs / maxRuns) * 100)}%` })),
        $("span", { class: "pane-num" }, `${d.runs} (${pct(d.share)})`)))),
    $("h3", {}, "Cost per gated task"),
    table(["tier", "median", "gated samples"],
      Object.entries(m.cost_per_gated_task || {}).map(([tier, v]) =>
        [tier, money(v.median_cost), v.samples])),
    $("h3", {}, "Escalation escape rate"),
    table(["entered at", "escaped", "samples"],
      Object.entries(m.escalation_escape_rate || {}).map(([tier, v]) =>
        [tier, pct(v.escape_rate), v.samples])),
    $("h3", {}, "Frontier warrant ledger"),
    (() => {
      const w = m.frontier_warrant_rate || {};
      return table(["frontier runs", "warranted", "rate", "flagged unwarranted"],
        [[w.frontier_runs ?? 0, w.warranted ?? 0, pct(w.warrant_rate),
          w.unwarranted_flagged ?? 0]]);
    })(),
    $("h3", {}, "Savings vs always-frontier (modeled)"),
    $("p", {},
      $("strong", {}, money(m.hourglass_savings?.actual_cost)), " actual vs ",
      $("strong", {}, money(m.hourglass_savings?.modeled_frontier_cost)),
      " modeled. ", $("span", { class: "muted small" },
        m.hourglass_savings?.basis || "")),
    $("h3", {}, "Recent escalations (traces)"),
    await renderRecentEscalations());
}

async function renderRecentEscalations() {
  try {
    const tail = await api("/api/ledger/tail?n=300");
    const escalations = (tail.entries || []).filter(e => e.event === "escalate");
    if (!escalations.length) {
      return $("p", { class: "muted" }, "No escalations in the recent ledger tail.");
    }
    return table(["seq", "from model", "to model", "directed by", "task"],
      escalations.slice(-12).reverse().map(e => {
        const directed = (e.directed_by || "verify_lane") === "jev";
        return [e.seq, e.from_model || "—", e.to_model || "—",
          directed ? `jev (conf ${e.jev_confidence ?? "?"})`
                   : "verify_lane",
          (e.task_id || "").slice(0, 18)];
      }));
  } catch (err) {
    return $("p", { class: "muted" }, `Ledger tail unavailable: ${err.message}`);
  }
}

// ---- Insights pane (rendered views over manual-fetch endpoints) ---------

const INSIGHT_SECTIONS = [
  ["Spend", "/api/spend", (d) => table(
    ["key limit", "remaining", "session spent", "ceiling"],
    [[d.key?.limit ?? "—", d.key?.remaining ?? "—",
      d.session ? `$${Number(d.session.spent).toFixed(4)}` : "—",
      d.session ? `$${Number(d.session.ceiling).toFixed(2)}` : "—"]])],
  ["Ledger participation", "/api/ledger/report", (d) => table(
    ["offers", "accepts", "completions", "escalations", "completion rate", "tracked cost"],
    [[d.offers, d.accepts, d.completions, d.escalations,
      pct(d.completion_rate), `$${Number(d.tracked_cost || 0).toFixed(4)}`]])],
  ["Trust standing", "/api/trust", (d) => {
    const rows = Object.entries(d.models || {}).slice(0, 12).map(
      ([model, v]) => [model, v.correctness ?? v.score ?? "—",
        v.strikes ?? 0, v.earn_back ?? "—"]);
    return rows.length
      ? table(["model", "correctness", "strikes", "earn-back"], rows)
      : $("p", { class: "muted" }, "No trust rows yet.");
  }],
  ["Capabilities", "/api/capabilities", (d) => {
    const rows = (d.models || d.rows || []).slice(0, 20).map(r =>
      [r.model, r.free ? "free" : "paid", r.capability ?? "—",
        r.reliability_structured ?? "—",
        r.observed?.success_rate ?? "—", r.observed?.samples ?? 0]);
    return rows.length
      ? table(["model", "tier", "capability", "reliability", "observed pass", "samples"], rows)
      : $("p", { class: "muted" }, "No capability rows (no key or empty catalog).");
  }],
  ["Rankings", "/api/rankings", (d) => d.available === false
    ? $("p", { class: "muted" }, d.note || "No rankings report available.")
    : $("pre", { class: "pane-json" }, JSON.stringify(d, null, 2).slice(0, 4000))],
];

async function renderInsights(root) {
  root.append($("h2", {}, "Insights"),
    $("p", { class: "muted" },
      "The dashboard views that used to be raw JSON fetches, rendered."));
  for (const [title, path, render] of INSIGHT_SECTIONS) {
    const section = $("section", { class: "pane-section" }, $("h3", {}, title));
    root.append(section);
    try {
      section.append(render(await api(path)));
    } catch (err) {
      section.append($("p", { class: "muted" }, `Unavailable: ${err.message}`));
    }
  }
}

// ---- Dispatch-form panes (verify / continue) -----------------------------
// DF-UI-1: the server already runs "verify" and "continue" run kinds
// (harness/server.py RUNNERS + validate_dispatch); these panes are minimal
// forms over the SAME POST /api/runs -> GET /api/runs/{id}/result contract
// the chat tab uses -- no new server logic. "bench" stays API-only (it
// takes a manifest path with no UI concept of "pick a manifest yet"; see
// docs/ui-readiness.md).

function field(label, attrs = {}) {
  const input = $("input", Object.assign({ class: "pane-input" }, attrs));
  return { row: $("label", { class: "pane-field" }, $("span", {}, label), input),
           input };
}

async function runDispatchForm(root, { kind, fields, buildArgs, resultLabel }) {
  const built = {};
  const rows = fields.map(([key, label, attrs]) => {
    const f = field(label, attrs);
    built[key] = f.input;
    return f.row;
  });
  const status = $("p", { class: "muted small" }, "");
  const out = $("pre", { class: "pane-json", hidden: true });
  const btn = $("button", { class: "pane-fetch", type: "button" }, `Run ${kind}`);
  const form = $("div", { class: "pane-section" }, ...rows, btn, status, out);
  root.append(form);

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    out.hidden = true;
    status.textContent = "Dispatching…";
    try {
      const args = buildArgs(built);
      const run = await postJson("/api/runs", { kind, args });
      status.textContent = `Run ${run.id} started; waiting…`;
      const final = await pollRunResult(run.id);
      status.textContent = `${resultLabel || "Result"}: ${final.status}`;
      out.textContent = JSON.stringify(final.result ?? final, null, 2).slice(0, 20000);
      out.hidden = false;
    } catch (err) {
      status.textContent = `Error: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  });
}

async function renderVerify(root) {
  root.append($("h2", {}, "Verify"),
    $("p", { class: "muted" },
      "Panel-verify a self-contained prompt (same run kind the CLI's ",
      $("code", {}, "harness verify"), " uses)."));
  await runDispatchForm(root, {
    kind: "verify",
    resultLabel: "Verify",
    fields: [
      ["prompt", "Prompt", { placeholder: "Self-contained question + context" }],
      ["judge", "Judge model (optional)", { placeholder: "default judge" }],
      ["panel", "Panel (comma-separated, optional)", { placeholder: "default panel" }],
      ["max_cost", "Max cost USD (optional)", { placeholder: "0.05", type: "number", step: "0.01" }],
    ],
    buildArgs: (f) => {
      const args = { prompt: f.prompt.value.trim() };
      if (f.judge.value.trim()) args.judge = f.judge.value.trim();
      if (f.panel.value.trim()) args.panel = f.panel.value.trim();
      if (f.max_cost.value) args.max_cost = Number(f.max_cost.value);
      return args;
    },
  });
}

async function renderContinue(root) {
  root.append($("h2", {}, "Continue"),
    $("p", { class: "muted" },
      "Resume a deferred/failed apply from its saved continuation state ",
      "file (same run kind the CLI's ", $("code", {}, "harness continue"),
      " uses)."));
  await runDispatchForm(root, {
    kind: "continue",
    resultLabel: "Continue",
    fields: [
      ["state", "State file path", { placeholder: "/path/to/state.json" }],
      ["instruction", "Instruction override (optional)", {}],
      ["verify", "Verify command (optional)", { placeholder: "e.g. pytest -q" }],
      ["max_rounds", "Max rounds (optional)", { placeholder: "3", type: "number", min: "1", max: "8" }],
    ],
    buildArgs: (f) => {
      const args = { state: f.state.value.trim() };
      if (f.instruction.value.trim()) args.instruction = f.instruction.value.trim();
      if (f.verify.value.trim()) args.verify = f.verify.value.trim();
      if (f.max_rounds.value) args.max_rounds = Number(f.max_rounds.value);
      return args;
    },
  });
}

// ---- Legacy API pane (the consolidated manual-fetch directory) ----------

const LEGACY_ENDPOINTS = [
  ["GET", "/api/status", "Engine status + session meta"],
  ["GET", "/api/spend", "Key + session spend view"],
  ["GET", "/api/trust", "Trust snapshot (read-only)"],
  ["GET", "/api/runs", "Active/finished runs"],
  ["GET", "/api/events", "Typed progress event stream"],
  ["GET", "/api/settings", "Settings view (no secrets)"],
  ["GET", "/api/ledger/tail?n=20", "Last N ledger entries + chain status"],
  ["GET", "/api/ledger/verify", "Ledger chain integrity"],
  ["GET", "/api/ledger/report", "Participation report"],
  ["GET", "/api/ledger/defer-stats", "Why runs deferred"],
  ["GET", "/api/capabilities", "Capability + reliability rows"],
  ["GET", "/api/models", "Live model catalog"],
  ["GET", "/api/rankings", "Rankings candidate report"],
  ["GET", "/api/snapshot", "Proof Bench snapshot (local mode)"],
  ["GET", "/api/site/demo-snapshot", "Labeled demo snapshot"],
];

async function renderLegacy(root) {
  root.append($("h2", {}, "Legacy API"),
    $("p", { class: "muted" },
      "Every raw JSON endpoint, one click each. These are the same interfaces ",
      "the app itself consumes — power users and tooling welcome."));
  const list = $("div", { class: "pane-legacy" });
  root.append(list);
  for (const [method, path, note] of LEGACY_ENDPOINTS) {
    const out = $("pre", { class: "pane-json", hidden: true });
    const btn = $("button", { class: "pane-fetch", type: "button" }, "Fetch");
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const data = await api(path);
        out.textContent = JSON.stringify(data, null, 2).slice(0, 20000);
        out.hidden = false;
      } catch (err) {
        out.textContent = String(err.message || err);
        out.hidden = false;
      } finally {
        btn.disabled = false;
      }
    });
    list.append($("div", { class: "pane-legacy-row" },
      $("code", {}, method, " ", path),
      $("span", { class: "muted small" }, note),
      btn, out));
  }
}

// ---- Tab wiring ----------------------------------------------------------

const PANES = [
  ["proof", "Proof", renderProof],
  ["insights", "Insights", renderInsights],
  ["verify", "Verify", renderVerify],
  ["continue", "Continue", renderContinue],
  ["legacy", "Legacy API", renderLegacy],
];

export function initPanes() {
  const main = document.getElementById("main-panel");
  const header = main?.querySelector("header");
  if (!main || !header) return; // unexpected DOM; leave the app untouched

  const strip = $("div", { class: "pane-tabs", role: "tablist" },
    ...[{ id: "chat", label: "Chat" }, ...PANES.map(([id, label]) =>
      ({ id, label }))].map(({ id, label }) =>
      $("button", { class: "pane-tab", type: "button", role: "tab",
        "data-pane": id }, label)));
  header.after(strip);

  const paneHost = $("div", { id: "pane-host", hidden: true });
  main.append(paneHost);

  const chatBits = ["#chat-container", "#input-footer"]
    .map(sel => main.querySelector(sel)).filter(Boolean);

  let current = "chat";
  const rendered = {};
  strip.addEventListener("click", (event) => {
    const tab = event.target.closest(".pane-tab");
    if (!tab) return;
    const id = tab.dataset.pane;
    if (id === current) return;
    current = id;
    for (const t of strip.querySelectorAll(".pane-tab")) {
      t.classList.toggle("active", t === tab);
    }
    const isChat = id === "chat";
    paneHost.hidden = isChat;
    for (const node of chatBits) node.hidden = !isChat;
    if (!isChat) {
      paneHost.replaceChildren();
      const entry = PANES.find(([pid]) => pid === id);
      if (entry && !rendered[id]) rendered[id] = true; // render once per load
      if (entry) entry[2](paneHost).catch(err =>
        paneHost.append($("p", { class: "muted" }, `Pane error: ${err.message}`)));
    }
  });
}
