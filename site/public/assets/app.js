// Proof Bench shared app module. No build, no framework: ES modules + fetch.
// Data source: local mode (harness serve /api/*) or public mode (worker).
export const state = {
  // Local mode = served by `harness serve` (any host/port; README default
  // 127.0.0.1:8765). Detection must never guess ports -- the old gate hardcoded
  // a port harness never serves, so local mode was dead and the site always
  // fell back to the static snapshot (see the regression test in
  // tests/test_site_server.py). Explicit ?mode=public selects the worker view;
  // local mode tries /api/snapshot first and falls back to the static demo
  // snapshot (the worker 404s /api/snapshot and lands on the same fallback).
  mode: location.search.includes("mode=public") ? "public" : "local",
  snapshot: null,
};

export async function loadSnapshot() {
  if (state.snapshot) return state.snapshot;
  const sources = state.mode === "local"
    ? ["/api/snapshot", "./data/demo/snapshot.json"]
    : ["./data/demo/snapshot.json"];
  for (const url of sources) {
    try {
      const res = await fetch(url, { headers: { accept: "application/json" } });
      if (res.ok) {
        state.snapshot = await res.json();
        state.snapshotSource = url;
        return state.snapshot;
      }
    } catch { /* try the next source */ }
  }
  return null;
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2), v);
    } else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  // Child arrays must land as nodes, never stringified: append() coerces an
  // array to "[object HTMLDivElement],...". barChart and the router reasons
  // list pass arrays, so flatten before appending (nulls still skipped).
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined) continue;
    node.append(child);
  }
  return node;
}

export function money(v) {
  if (v === null || v === undefined) return "—";
  if (v === 0) return "$0";
  if (v < 0.01) return `$${v.toExponential(1)}`;
  if (v < 1) return `$${v.toFixed(4)}`;
  return `$${v.toFixed(2)}`;
}

export function pct(v) {
  return v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`;
}

export function markDemo(bannerId = "demo-banner") {
  const banner = document.getElementById(bannerId);
  if (banner) banner.dataset.active = String(Boolean(state.snapshot?.demo));
}

// The hourglass diagram: wide base (cheap rungs), narrow waist (frontier).
export function hourglass(metrics) {
  const depth = metrics?.run_depth_distribution || {};
  const tiers = ["T0", "T1", "T2", "T3"];
  const rows = tiers.map(t => ({
    tier: t, runs: depth[t]?.runs || 0, share: depth[t]?.share || 0,
  }));
  const max = Math.max(...rows.map(r => r.runs), 1);
  const cell = (r, cls) => el("div", { class: `col ${cls}` },
    el("strong", {}, r.tier), el("div", { class: "cap" },
      `${r.runs} runs · ${pct(r.share)}`));
  return el("div", { class: "hg", role: "img",
    "aria-label": "Run depth: base tiers carry most runs; frontier is rare" },
    cell(rows[0], "base"),
    el("div", { style: `width:${18 + 82 * (rows[3].runs / max)}%; border-left:3px solid var(--warn); grid-row:1/3;` }),
    cell(rows[3], "waist"),
    cell(rows[1], "base"),
    cell(rows[2], "base"));
}

export function barChart(items, { label, value, format } = {}) {
  const rows = items.filter(i => Number.isFinite(i.value));
  const max = Math.max(...rows.map(i => i.value), 1e-12);
  return el("div", { class: "bar-chart" }, rows.map(i =>
    el("div", { class: "row" },
      el("span", { class: "small" }, i.label),
      el("div", { class: "track" },
        el("div", { class: `fill ${i.tier === "T3" ? "t3" : ""}`,
          style: `width:${Math.max(2, (i.value / max) * 100)}%` })),
      el("span", { class: "mono small", style: "text-align:right" },
        i.format ?? String(i.value)))));
}

export function renderHome(root, snapshot) {
  const m = snapshot?.sessions?.[0]?.metrics;
  if (!m) {
    root.append(el("p", { class: "muted" },
      "No snapshot available yet. Run the efficiency bench and site-export, ",
      "or serve local mode via 'harness serve'."));
    return;
  }
  const savings = m.hourglass_savings || {};
  const leverage = m.jev_leverage || {};
  root.append(
    el("h2", {}, "The hourglass, measured"),
    hourglass(m),
    el("p", { class: "muted" },
      "Run depth across all submitted runs: how deep into the cost ladder ",
      "work actually had to go. Base layers carry the volume; the frontier ",
      "rung is rare — and when it appears, it must show its warrant."),
    el("h2", {}, "Cost per gated task"),
    barChart(
      Object.entries(m.cost_per_gated_task || {}).map(([tier, v]) => ({
        label: `${tier} median`, value: v.median_cost, tier,
        format: money(v.median_cost) })),
      { format: money }),
    el("p", { class: "small muted" },
      "Median cost of verify-gate-passed runs per tier. Gated runs only: a ",
      "pass without a gate is a self-report, not proof."),
    el("h2", {}, "Savings vs always-frontier"),
    el("p", {},
      el("strong", {}, money(savings.actual_cost)),
      " actual vs ",
      el("strong", {}, money(savings.modeled_frontier_cost)),
      " modeled always-frontier (",
      el("span", { class: "mono" },
        savings.savings_multiple ? `${savings.savings_multiple.toFixed(2)}x` : "—"),
      "). ",
      el("span", { class: "small muted" }, savings.basis || "")),
    el("h2", {}, "Jev leverage"),
    el("p", {},
      "Routing decisions that cost ",
      el("strong", {}, money(leverage.jev_cost)),
      " would have modeled ",
      el("strong", {}, money(leverage.modeled_generative_floor)),
      " at the cheapest generative seat (",
      el("span", { class: "mono" },
        leverage.ratio ? `${leverage.ratio}x floor` : "—"),
      "). ",
      el("span", { class: "small muted" }, leverage.basis || "")),
  );
}

export function renderTiers(root, snapshot) {
  const m = snapshot?.sessions?.[0]?.metrics;
  const warrant = m?.frontier_warrant_rate;
  const guidance = [
    ["T0", "Scout rung", "Typos, renames, formatting, docstrings, mechanical edits.",
      "If you can describe the edit in one sentence, this rung finishes it for fractions of a cent."],
    ["T1", "Distiller rung", "Structured logic, tests, parsers, multi-round fixes.",
      "The workhorse: implement-and-verify loops that pass inside a few rounds."],
    ["T2", "Specialist rung", "Concurrency, invariants, protocols, migration mechanics.",
      "Genuinely hard local reasoning — proven when cheaper rungs exhausted their rounds first."],
    ["T3", "Frontier rung", "Architecture and cross-module protocol planning — at the waist, over pre-distilled research.",
      "Unless you are formulating a new mathematical proof for quantum encryption, leave it to the rungs below — and even then the frontier seat only plans a tightly scoped implementation; it does not do what a cheaper rung can."],
  ];
  root.append(el("h2", {}, "What belongs at each rung — with proof"));
  for (const [tier, name, fits, note] of guidance) {
    const med = m?.cost_per_gated_task?.[tier];
    root.append(
      el("h3", {}, `${tier} — ${name}`,
        med ? el("span", { class: "badge" }, `median ${money(med.median_cost)} / gated task`)
            : el("span", { class: "badge" }, "no gated data yet")),
      el("p", {}, fits),
      el("p", { class: "muted" }, note));
  }
  root.append(
    el("h2", {}, "Frontier warrant ledger"),
    el("p", { class: "muted" },
      "Frontier runs are shown even when unwarranted. A warrant is evidence ",
      "that cheaper rungs verifiably tried and failed first."),
    warrant ? el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "frontier runs"), el("th", {}, "warranted"),
        el("th", {}, "warrant rate"), el("th", {}, "flagged"))),
      el("tbody", {}, el("tr", {},
        el("td", { class: "num" }, String(warrant.frontier_runs)),
        el("td", { class: "num" }, String(warrant.warranted)),
        el("td", { class: "num" }, pct(warrant.warrant_rate)),
        el("td", { class: "num" },
          warrant.unwarranted_flagged
            ? el("span", { class: "badge warn" },
                `${warrant.unwarranted_flagged} unwarranted`)
            : "0"))))
      : el("p", { class: "muted" }, "No frontier runs in this snapshot."),
    el("p", { class: "small muted" },
      "Guidance reflects routing intent; the ledger records which rung actually ran."));
}

export async function renderRouter(root, snapshot) {
  root.append(
    el("h2", {}, "Route a query — the marketplace demo"),
    el("p", { class: "muted" },
      "Type a request; the router compares it against the declared rung ladder ",
      "(tier, cost class, observed success) and picks the cheapest capable rung. ",
      "This is the same pack the harness CLI/MCP route command uses — the site ",
      "demos the product, it does not re-implement it."),
    el("form", { class: "inline", onsubmit: onSubmit },
      el("input", { type: "text", name: "goal", required: true,
        placeholder: "e.g. fix the race in the token bucket",
        "aria-label": "Your request" }),
      el("button", { type: "submit" }, "Route")),
    el("div", { id: "route-result" }));
  markDemo();

  const ladder = [
    { rung_id: "r0", tier: "T0", cost_class: "free",
      model: "declared-by-operator", guidance: ["typo", "rename", "format"] },
    { rung_id: "r1", tier: "T1", cost_class: "cheap",
      model: "declared-by-operator", guidance: ["implement", "fix", "test"] },
    { rung_id: "r2", tier: "T2", cost_class: "moderate",
      model: "declared-by-operator", guidance: ["concurrency", "invariant"] },
    { rung_id: "r3", tier: "T3", cost_class: "premium",
      model: "declared-by-operator", guidance: ["architecture"] },
  ];

  async function onSubmit(event) {
    event.preventDefault();
    const goal = new FormData(event.target).get("goal")?.trim();
    if (!goal) return;
    const out = document.getElementById("route-result");
    out.replaceChildren(el("p", { class: "muted" }, "Routing…"));
    let envelope;
    try {
      const res = await fetch("/api/route", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ goal, pack: { id: "site-demo-ladder", rungs: ladder } }),
      });
      envelope = await res.json();
      if (!res.ok) throw new Error(envelope.error || `HTTP ${res.status}`);
    } catch (err) {
      out.replaceChildren(
        el("p", {}, "Router unavailable in this view (",
          el("code", {}, String(err.message || err)), "). ",
          "Local mode: run 'harness serve'. Public mode: the worker proxies ",
          "the Jev seat."));
      return;
    }
    renderEnvelope(out, envelope);
  }

  function renderEnvelope(out, envelope) {
    const route = envelope.route || {};
    out.replaceChildren(
      el("h3", {},
        route.rung_id
          ? `Cheapest capable rung: ${route.rung_id} (${route.tier})`
          : "Unroutable against this ladder"),
      el("p", {},
        el("span", { class: `badge ${envelope.is_fallback ? "warn" : "good"}` },
          envelope.is_fallback ? "deterministic fallback (Jev seat unavailable or choice refused)" : "Jev choice"),
        route.cost_class ? el("span", { class: "badge" }, `cost: ${route.cost_class}`) : null),
      el("ul", {}, (route.reasons || []).map(r => el("li", {}, r))),
      route.guidance?.length
        ? el("p", { class: "small muted" }, "Rung guidance: ", route.guidance.join(", "))
        : null);
  }
}

export function renderTraces(root, snapshot) {
  root.append(el("h2", {}, "Escalation traces"),
    el("p", { class: "muted" },
      "Per-run cascades rebuilt from sanitized ledger evidence. Each trace ",
      "shows where a run entered, how far the ladder actually went, and — ",
      "when it escalated — the warrant: cheaper rungs verifiably tried first."));
  const sessions = snapshot?.sessions || [];
  if (!sessions.length) {
    root.append(el("p", { class: "muted" }, "No sessions in this snapshot."));
    return;
  }
  for (const session of sessions) {
    root.append(el("h3", {}, `session ${session.bundle_id || "?"}`,
      el("span", { class: "badge" }, `${session.runs} runs`)),
      hourglass(session.metrics),
      el("h4", {}, "Run depth"),
      barChart(Object.entries(session.metrics.run_depth_distribution).map(
        ([tier, v]) => ({ label: tier, value: v.runs, tier,
          format: `${v.runs} (${pct(v.share)})` }))),
      el("h4", {}, "Escalation escape rate"),
      barChart(Object.entries(session.metrics.escalation_escape_rate).map(
        ([tier, v]) => ({ label: `entered ${tier}`, value: v.escape_rate,
          tier, format: pct(v.escape_rate) }))));
    const traces = session.traces || [];
    if (traces.length) {
      root.append(el("h4", {}, "Recent escalations"));
      for (const t of [...traces].reverse()) {
        const esc = t.escalation || {};
        const directed = esc.directed_by === "jev";
        root.append(el("div", { class: "trace-card" },
          el("span", { class: `badge ${directed ? "good" : ""}` },
            directed
              ? `Jev-directed climb (confidence ${esc.jev_confidence ?? "?"})`
              : "verify-lane climb"),
          el("span", { class: "badge" },
            `entered ${t.entry_tier ?? "?"} → reached ${t.deepest_tier_reached ?? "?"}`),
          esc.target_rung != null
            ? el("span", { class: "badge" }, `target rung ${esc.target_rung}`)
            : null,
          esc.condensed_context_chars != null
            ? el("span", { class: "small muted" },
                `${esc.condensed_context_chars} chars of code-owned failure evidence carried`)
            : null,
          el("span", { class: `badge ${t.outcome === "pass" && t.gated ? "good" : "warn"}` },
            t.outcome === "pass" && t.gated ? "gated pass" : t.outcome || "?")));
      }
    }
  }
}

export function renderMethodology(root, snapshot) {
  const m = snapshot?.sessions?.[0]?.metrics;
  root.append(
    el("h2", {}, "Methodology: how proof works here"),
    el("h3", {}, "Ground truth is the verify gate"),
    el("p", {}, "A run passes only when a deterministic verification command ",
      "exits zero after the model's edit. 'Passed' means provably correct, ",
      "not self-reported. Headline metrics count gated runs only."),
    el("h3", {}, "Evidence chain"),
    el("p", {}, "Every run lives in a local, hash-chained ledger. Export runs ",
      "'ledger verify' first and ships the chain claim (verified on the ",
      "contributor's machine), the head hash, and the entry count. A receiving ",
      "server cannot re-verify a chain it does not hold — this page says so ",
      "instead of overclaiming."),
    el("h3", {}, "Sanitization"),
    el("p", {}, "Bundles are built from an allowlist of ledger events. No prompt ",
      "text, no file paths, no gate output, no error text, no caller identity. ",
      "Task ids appear only as truncated SHA-256. Secret-shaped content refuses ",
      "the export outright — never redact-and-continue."),
    el("h3", {}, "Modeled numbers say so"),
    el("p", {}, "Per-run token counts are not ledgered, so savings-vs-frontier ",
      "and Jev leverage are modeled from per-token pricing and labeled as such. ",
      "When pricing is missing they render 'unavailable' with the reason — ",
      "never a fabricated ratio."),
    el("h3", {}, "Anti-gaming"),
    el("p", {}, "Bundles dedupe by content hash; no session exceeds the ",
      "disclosed influence cap; anomalies (thin samples, atypical frontier ",
      "share) are flagged, not silently cleaned.",
      snapshot?.influence_cap
        ? el("span", {}, ` Current cap: ${Math.round(snapshot.influence_cap * 100)}%.`)
        : null),
    el("h3", {}, "Demo data"),
    el("p", {}, snapshot?.demo
      ? snapshot.demo_note || "This snapshot is labeled demo data."
      : "No demo flag on this snapshot: it claims to be live evidence."),
    el("h3", {}, "Failures are data"),
    el("p", {}, m ? "Abort rates, gate-waste and fail outcomes ride alongside "
      + `the wins: gated pass rate overall ${pct(m.gated_pass_rate?.overall?.pass_rate)}.`
      : "No data yet."),
    el("h3", {}, "License"),
    el("p", {}, "Site data: CC0. Site code: MIT with the harness repo."));
}

export function initBanner() { markDemo(); }
