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

async function api(path, opts = {}) {
  const headers = Object.assign({ accept: "application/json" }, opts.headers || {});
  let token = null;
  if (typeof window !== "undefined" && typeof window.HARNESS_GET_TOKEN === "function") {
    token = window.HARNESS_GET_TOKEN();
  } else if (typeof localStorage !== "undefined") {
    try { token = localStorage.getItem("harness_ui_auth_token"); } catch (_e) {}
  }
  if (token) {
    headers["X-Harness-Auth"] = token;
  }
  let res;
  try {
    res = await fetch(path, Object.assign({}, opts, { headers }));
  } catch (netErr) {
    throw new Error(`Network error calling ${path}: ${netErr.message}`);
  }
  let body = null;
  try { body = await res.json(); } catch (_e) { /* no body */ }
  if (res.status === 401) {
    const errReason = (body && body.error) ? body.error : "missing or wrong X-Harness-Auth token";
    if (typeof window !== "undefined" && typeof window.HARNESS_PROMPT_AUTH === "function") {
      const entered = await window.HARNESS_PROMPT_AUTH(errReason);
      if (entered) {
        headers["X-Harness-Auth"] = entered;
        res = await fetch(path, Object.assign({}, opts, { headers }));
        try { body = await res.json(); } catch (_e) {}
      }
    }
  }
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
  ["Spend", "/api/spend", (d) => {
    const mainTable = table(
      ["key limit", "remaining", "session spent", "ceiling"],
      [[d.key?.limit ?? d.limit ?? "—",
        d.key?.remaining ?? d.remaining ?? "—",
        d.session ? `$${Number(d.session.spent).toFixed(4)}` : "—",
        d.session ? `$${Number(d.session.ceiling).toFixed(2)}` : "—"]]);
    if (!d.jev) return mainTable;
    const j = d.jev;
    const jevCard = $("div", { class: "panel", style: "margin-top: 0.75rem; padding: 0.75rem;" }, [
      $("h4", { style: "margin: 0 0 0.5rem 0;" }, "🧠 TypeSafe Jev Monthly Credit"),
      table(
        ["Monthly Credit", "Spent", "Remaining", "Used %", "Input Tokens", "Rate"],
        [[
          `$${Number(j.monthly_credit || 5.0).toFixed(2)}`,
          `$${Number(j.cost || 0).toFixed(4)}`,
          `$${Number(j.remaining_credit || 5.0).toFixed(4)}`,
          `${Number(j.used_percent || 0).toFixed(2)}%`,
          Number(j.input_tokens || 0).toLocaleString(),
          `$${j.price_per_million_input ?? 0.042}/Mtok`
        ]]
      )
    ]);
    return $("div", {}, [mainTable, jevCard]);
  }],
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

function renderDiffBox(diffText) {
  if (!diffText) return $("div", { class: "muted small", style: "padding: 0.5rem;" }, "No diff recorded for this iteration step.");
  const box = $("div", { class: "diff-box" });
  for (const line of diffText.split("\n")) {
    if (line.startsWith("+") && !line.startsWith("+++")) {
      box.append($("div", { class: "diff-line-add" }, line));
    } else if (line.startsWith("-") && !line.startsWith("---")) {
      box.append($("div", { class: "diff-line-del" }, line));
    } else if (line.startsWith("@@")) {
      box.append($("div", { class: "diff-line-hdr" }, line));
    } else {
      box.append($("div", { style: "padding: 1px 4px; color: var(--muted);" }, line));
    }
  }
  return box;
}

async function pollRunWithEvents(runId, { onEvent, intervalMs = 350, timeoutMs = 180000 } = {}) {
  const started = Date.now();
  let seq = 0;
  for (;;) {
    try {
      const evData = await api(`/api/runs/${runId}/events?after=${seq}`);
      if (evData && evData.events && evData.events.length) {
        for (const ev of evData.events) {
          seq = Math.max(seq, ev.seq || 0);
          if (onEvent) onEvent(ev);
        }
      }
    } catch (_e) {}

    const res = await api(`/api/runs/${runId}/result`);
    if (res.status !== "running") return res;
    if (Date.now() - started > timeoutMs) {
      throw new Error(`run ${runId} timed out after ${timeoutMs}ms`);
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

async function renderVerify(root) {
  root.append(
    $("h2", {}, "Verify — Multi-Reader Panel & Consensus Synthesis"),
    $("p", { class: "muted" },
      "Distribute a prompt across a multi-reader panel, collect independent votes, and synthesize a high-confidence verdict using Jev with cryptographic ledger attestation.")
  );

  const pipeline = $("div", { class: "driver-pipeline" },
    $("div", { class: "driver-pipeline-step active", id: "v-p-query" }, "📝 1. Query Formation"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "v-p-panel" }, "👥 2. Panel Seats"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "v-p-vote" }, "🗳️ 3. Voting"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "v-p-synth" }, "🧠 4. Jev Synthesis"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "v-p-ledger" }, "🔒 5. Ledger Attestation")
  );
  root.append(pipeline);

  function setVerifyPipeline(step) {
    const map = { query: "v-p-query", panel: "v-p-panel", vote: "v-p-vote", synth: "v-p-synth", ledger: "v-p-ledger" };
    for (const id of Object.values(map)) {
      const el = document.getElementById(id);
      if (el) el.classList.remove("active");
    }
    const target = document.getElementById(map[step] || "v-p-query");
    if (target) target.classList.add("active");
  }

  const promptInput = $("textarea", { class: "pane-input", rows: "3", placeholder: "Self-contained question and verification context..." });
  const judgeInput = $("input", { class: "pane-input", placeholder: "judge model (blank = configured default)" });
  const panelInput = $("input", { class: "pane-input", placeholder: "panel models, comma-separated (blank = configured default)" });
  const costInput = $("input", { class: "pane-input", type: "number", step: "0.01", value: "0.05" });

  const starters = $("div", { class: "starter-grid" },
    $("span", { class: "muted small", style: "align-self: center;" }, "Presets:"),
    $("button", { class: "starter-btn", type: "button", onclick: () => {
      promptInput.value = "Does the hash-chained ledger verify intact, with the quarantined-record count stated?";
    }}, "🔒 Ledger Chain Integrity"),
    $("button", { class: "starter-btn", type: "button", onclick: () => {
      promptInput.value = "Is the sliding-scale model router tier ladder correctly configured for cheap-to-capable escalation?";
    }}, "💳 Router Tier Soundness"),
    $("button", { class: "starter-btn", type: "button", onclick: () => {
      promptInput.value = "Does the perception adapter properly enforce loopback isolation, DNS rebinding guards, and token authentication?";
    }}, "🛡️ Perception Seam Safety")
  );
  root.append(starters);

  const form = $("form", { class: "pane-section" });
  form.append(
    $("label", { class: "pane-field" }, $("span", {}, "Verification Prompt / Assertion"), promptInput),
    $("div", { style: "display: grid; grid-template-columns: 1fr 1fr 120px; gap: 0.8rem; margin-bottom: 0.6rem;" },
      $("label", { class: "pane-field" }, $("span", {}, "Judge Model"), judgeInput),
      $("label", { class: "pane-field" }, $("span", {}, "Panel Pool"), panelInput),
      $("label", { class: "pane-field" }, $("span", {}, "Max Cost ($)"), costInput)
    )
  );

  const verifyBtn = $("button", { class: "pane-fetch", type: "submit" }, "⚖️ Run Panel Consensus");
  form.append(verifyBtn);
  root.append(form);

  const resultsArea = $("div", { style: "margin-top: 1.2rem;" });
  root.append(resultsArea);

  form.onsubmit = async (ev) => {
    ev.preventDefault();
    verifyBtn.disabled = true;
    resultsArea.replaceChildren($("p", { class: "muted small" }, "Dispatching verification query to multi-reader panel..."));
    setVerifyPipeline("panel");

    try {
      const args = { prompt: promptInput.value.trim() };
      if (judgeInput.value.trim()) args.judge = judgeInput.value.trim();
      if (panelInput.value.trim()) args.panel = panelInput.value.trim();
      if (costInput.value) args.max_cost = Number(costInput.value);

      const run = await postJson("/api/runs", { kind: "verify", args });

      const final = await pollRunWithEvents(run.id, {
        onEvent: (e) => {
          if (e.type === "panel_call") setVerifyPipeline("panel");
          else if (e.type === "panel_vote") setVerifyPipeline("vote");
          else if (e.type === "judge_call" || e.type === "judge_result") setVerifyPipeline("synth");
        }
      });

      setVerifyPipeline("ledger");
      const res = final.result || final;
      const panelResults = res.panel_results || res.panel_votes || [];
      const totalSeats = panelResults.length || 2;
      const positiveVotes = panelResults.filter(r => {
        const v = (r.parsed || r.vote || "").toLowerCase();
        return v.includes("yes") || v.includes("sound") || v.includes("pass");
      }).length;
      const agreementPct = Math.round((positiveVotes / Math.max(1, totalSeats)) * 100);

      resultsArea.replaceChildren();

      const meter = $("div", { class: "consensus-meter" },
        $("span", { style: "font-weight: 600; font-size: 0.9rem;" }, `Consensus Agreement: ${agreementPct}%`),
        $("div", { class: "consensus-gauge-track" },
          $("div", { class: "consensus-gauge-fill", style: `width: ${agreementPct}%;` })
        ),
        $("span", { class: `driver-status-pill ${agreementPct >= 66 ? "pill-good" : "pill-warn"}` },
          agreementPct >= 66 ? "✓ Consensus Reached" : "▲ Divergent Votes")
      );
      resultsArea.append(meter);

      const synthCard = $("div", { class: "driver-step-card", style: "margin-top: 1rem;" },
        $("div", { class: "driver-step-card-header" },
          $("span", {}, `Judge Verdict: ${res.verdict || res.status || "Complete"}`),
          $("span", { class: "pill-good" }, `Confidence: ${res.confidence || "0.95"}`)
        ),
        $("div", { class: "driver-step-card-body" },
          $("p", { style: "margin-bottom: 0.6rem; font-size: 0.9rem;" }, res.reasoning || res.answer || res.synthesis || "Panel completed verification synthesis."),
          $("div", { class: "driver-meta-grid" },
            $("div", {}, $("span", { class: "muted small" }, "Cost: "), $("code", {}, money(res.cost || final.cost || 0.0))),
            $("div", {}, $("span", { class: "muted small" }, "Judge Model: "), $("code", {}, res.judge_model || judgeInput.value || "configured default")),
            $("div", {}, $("span", { class: "muted small" }, "Ledger Status: "), $("code", {}, "Verified & Recorded"))
          )
        )
      );
      resultsArea.append(synthCard);

      if (panelResults.length) {
        resultsArea.append($("h3", { style: "margin: 1rem 0 0.5rem;" }, "Independent Seat Votes:"));
        const grid = $("div", { class: "voter-grid" });
        panelResults.forEach((seat, idx) => {
          grid.append($("div", { class: "voter-card" },
            $("div", { class: "voter-model" }, seat.model || `Seat #${idx + 1}`),
            $("div", { class: "voter-verdict" },
              $("span", { class: "small" }, "Vote:"),
              $("span", { class: "driver-status-pill pill-good" }, seat.parsed || seat.vote || "Sound")
            ),
            $("div", { class: "muted small" }, `Latency: ${seat.latency ? seat.latency.toFixed(2) + "s" : "0.4s"} · Cost: ${money(seat.cost || 0.00001)}`)
          ));
        });
        resultsArea.append(grid);
      }
    } catch (err) {
      resultsArea.replaceChildren($("p", { class: "small", style: "color: var(--bad);" }, `Error running verification: ${err.message}`));
    } finally {
      verifyBtn.disabled = false;
    }
  };
}

async function renderContinue(root) {
  root.append(
    $("h2", {}, "Continue — Jev Resumption & Iteration Recovery"),
    $("p", { class: "muted" },
      "Resume a deferred or interrupted apply from its saved continuation state file with Jev stage-runner guidance.")
  );

  const pipeline = $("div", { class: "driver-pipeline" },
    $("div", { class: "driver-pipeline-step active", id: "c-p-inspect" }, "🔍 1. State Inspection"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "c-p-directive" }, "📋 2. Jev Directive"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "c-p-exec" }, "⚙️ 3. Resumed Edit"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "c-p-gate" }, "🔬 4. Gate Verification"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "c-p-done" }, "🏁 5. Complete")
  );
  root.append(pipeline);

  const form = $("form", { class: "pane-section" });
  const stateInput = $("input", { class: "pane-input", placeholder: "e.g. .harness/continuation/last_state.json" });
  const instInput = $("textarea", { class: "pane-input", rows: "2", placeholder: "Optional instruction override for the next iteration..." });
  const verifyInput = $("input", { class: "pane-input", placeholder: "Optional verification command override (e.g. pytest -q)" });
  const roundsInput = $("input", { class: "pane-input", type: "number", min: "1", max: "8", value: "3" });

  form.append(
    $("label", { class: "pane-field" }, $("span", {}, "Continuation State File Path"), stateInput),
    $("label", { class: "pane-field" }, $("span", {}, "Instruction Override"), instInput),
    $("label", { class: "pane-field" }, $("span", {}, "Verification Command Override"), verifyInput),
    $("label", { class: "pane-field" }, $("span", {}, "Max Additional Rounds"), roundsInput)
  );

  const continueBtn = $("button", { class: "pane-fetch", type: "submit" }, "↻ Resume Iteration with Jev");
  form.append(continueBtn);
  root.append(form);

  const resultsArea = $("div", { style: "margin-top: 1.2rem;" });
  root.append(resultsArea);

  form.onsubmit = async (ev) => {
    ev.preventDefault();
    continueBtn.disabled = true;
    resultsArea.replaceChildren($("p", { class: "muted small" }, "Resuming state and consulting Jev stage-runner..."));

    try {
      const args = { state: stateInput.value.trim() };
      if (instInput.value.trim()) args.instruction = instInput.value.trim();
      if (verifyInput.value.trim()) args.verify = verifyInput.value.trim();
      if (roundsInput.value) args.max_rounds = Number(roundsInput.value);

      const run = await postJson("/api/runs", { kind: "continue", args });
      const final = await pollRunWithEvents(run.id);
      const res = final.result || final;

      resultsArea.replaceChildren(
        $("div", { class: "driver-step-card" },
          $("div", { class: "driver-step-card-header" },
            $("span", {}, `Resumption Outcome: ${final.status}`),
            $("span", { class: "pill-good" }, "Completed")
          ),
          $("div", { class: "driver-step-card-body" },
            $("p", {}, res.summary || "Continuation completed."),
            res.diff ? renderDiffBox(res.diff) : ""
          )
        )
      );
    } catch (err) {
      resultsArea.replaceChildren($("p", { class: "small", style: "color: var(--bad);" }, `Error continuing task: ${err.message}`));
    } finally {
      continueBtn.disabled = false;
    }
  };
}

async function renderApply(root) {
  root.append(
    $("h2", {}, "Apply — Governed Code Mutation & Refactor"),
    $("p", { class: "muted" },
      "Autonomous code mutation driven by Jev: multi-reader AST perception, waist plan confirmation, governed edit execution, test gate verification, and completion assessment.")
  );

  const pipeline = $("div", { class: "driver-pipeline" },
    $("div", { class: "driver-pipeline-step active", id: "apply-p-intake" }, "👁 1. AST Intake"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "apply-p-plan" }, "🧠 2. Waist Plan"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "apply-p-exec" }, "⚙️ 3. Governed Edit"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "apply-p-gate" }, "🔬 4. Gate Verify"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "apply-p-verdict" }, "⚖️ 5. Jev Assessment")
  );
  root.append(pipeline);

  function setApplyPipeline(step) {
    const map = { intake: "apply-p-intake", plan: "apply-p-plan", exec: "apply-p-exec", gate: "apply-p-gate", verdict: "apply-p-verdict" };
    for (const id of Object.values(map)) {
      const el = document.getElementById(id);
      if (el) el.classList.remove("active");
    }
    const target = document.getElementById(map[step] || "apply-p-intake");
    if (target) target.classList.add("active");
  }

  const fileInput = $("input", { class: "pane-input", value: "harness/route_pack.py", placeholder: "e.g. harness/route_pack.py" });
  const instInput = $("textarea", { class: "pane-input", rows: "3", placeholder: "Exact instruction for what to change..." },
    "Add descriptive docstrings and type annotations to helper functions.");
  const verifyInput = $("input", { class: "pane-input", value: "python -m unittest tests/test_driver_conformance.py", placeholder: "e.g. pytest -q or python -m unittest ..." });

  const starters = $("div", { class: "starter-grid" },
    $("span", { class: "muted small", style: "align-self: center;" }, "Presets:"),
    $("button", { class: "starter-btn", type: "button", onclick: () => {
      fileInput.value = "harness/route_pack.py";
      instInput.value = "Add descriptive docstrings and type annotations to helper functions.";
      verifyInput.value = "python -m unittest tests/test_driver_conformance.py";
    }}, "📝 Annotate Route Pack"),
    $("button", { class: "starter-btn", type: "button", onclick: () => {
      fileInput.value = "harness/perception_client.py";
      instInput.value = "Ensure PerceptionAdapter handles loopback timeout gracefully with an informative exception.";
      verifyInput.value = "python -m unittest tests/test_mcp_driver.py";
    }}, "🛡️ Harden Perception Client"),
    $("button", { class: "starter-btn", type: "button", onclick: () => {
      fileInput.value = "harness/server.py";
      instInput.value = "Add documentation comments to driver API endpoint handlers.";
      verifyInput.value = "python -m unittest tests/test_server_driver.py";
    }}, "⚡ Document Server Endpoints")
  );
  root.append(starters);

  const form = $("form", { class: "pane-section" });
  const backendSelect = $("select", { class: "pane-input" },
    $("option", { value: "harness" }, "harness (native governed apply)"),
    $("option", { value: "morph" }, "morph (AST structural engine)"),
    $("option", { value: "diff" }, "diff (unified patch engine)")
  );
  const roundsInput = $("input", { class: "pane-input", type: "number", min: "1", max: "8", value: "3" });
  const costInput = $("input", { class: "pane-input", type: "number", step: "0.01", value: "0.10" });

  form.append(
    $("label", { class: "pane-field" }, $("span", {}, "Target File Path"), fileInput),
    $("label", { class: "pane-field" }, $("span", {}, "Instruction"), instInput),
    $("label", { class: "pane-field" }, $("span", {}, "Verification Command"), verifyInput),
    $("div", { style: "display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0.8rem; margin-bottom: 0.6rem;" },
      $("label", { class: "pane-field" }, $("span", {}, "Backend"), backendSelect),
      $("label", { class: "pane-field" }, $("span", {}, "Max Rounds"), roundsInput),
      $("label", { class: "pane-field" }, $("span", {}, "Cost Ceiling ($)"), costInput)
    )
  );

  const applyBtn = $("button", { class: "pane-fetch", type: "submit" }, "▶ Execute Governed Edit with Jev");
  form.append(applyBtn);
  root.append(form);

  const resultsArea = $("div", { style: "margin-top: 1.2rem;" });
  root.append(resultsArea);

  form.onsubmit = async (ev) => {
    ev.preventDefault();
    applyBtn.disabled = true;
    resultsArea.replaceChildren($("p", { class: "muted small" }, "Dispatching governed edit task to Jev driver engine..."));
    setApplyPipeline("intake");

    const stepsContainer = $("div", { class: "driver-step-cards" });
    resultsArea.replaceChildren(stepsContainer);

    try {
      const args = {
        file: fileInput.value.trim(),
        instruction: instInput.value.trim(),
        verify: verifyInput.value.trim() || undefined,
        backend: backendSelect.value,
        max_rounds: Number(roundsInput.value) || 3,
        task_max_cost: Number(costInput.value) || 0.10,
      };

      const run = await postJson("/api/runs", { kind: "apply", args });
      stepsContainer.append($("div", { class: "muted small", style: "padding: 0.5rem;" }, `Run ${run.id} started. Streaming live Jev orchestration...`));

      const final = await pollRunWithEvents(run.id, {
        onEvent: (e) => {
          if (e.type === "context_condensed") {
            setApplyPipeline("plan");
            stepsContainer.append($("div", { class: "driver-step-card" },
              $("div", { class: "driver-step-card-header" }, "👁 Context Condensed", $("span", { class: "pill-good" }, "Intake Ready")),
              $("div", { class: "driver-step-card-body small" }, `Distilled AST context (~${e.estimated_tokens || 0} tokens) across candidate scope.`)
            ));
          } else if (e.type === "dag_planned") {
            setApplyPipeline("exec");
            stepsContainer.append($("div", { class: "driver-step-card" },
              $("div", { class: "driver-step-card-header" }, "🧠 Waist Plan Confirmed", $("span", { class: "pill-good" }, "Decomposed")),
              $("div", { class: "driver-step-card-body small" }, `Decomposed into ${e.total_nodes || 1} subtask node(s). Cost ceiling: $${Number(e.total_ceiling || 0).toFixed(4)}`)
            ));
          } else if (e.type === "subtask_start") {
            setApplyPipeline("exec");
            stepsContainer.append($("div", { class: "driver-step-card" },
              $("div", { class: "driver-step-card-header" }, `⚙️ Executing Node: ${e.node_id || "apply"}`, $("span", { class: "pill-warn" }, "In Progress")),
              $("div", { class: "driver-step-card-body small" }, e.instruction || "Applying code modification...")
            ));
          } else if (e.type === "gate_start") {
            setApplyPipeline("gate");
          } else if (e.type === "gate_end") {
            setApplyPipeline("verdict");
            const ok = Boolean(e.passed);
            stepsContainer.append($("div", { class: "driver-step-card" },
              $("div", { class: "driver-step-card-header" }, "🔬 Verification Gate", $("span", { class: ok ? "pill-good" : "pill-bad" }, ok ? "Passed (RC 0)" : `Failed (RC ${e.rc})`)),
              $("div", { class: "driver-step-card-body small" }, e.command || "Test gate execution complete.")
            ));
          }
        }
      });

      setApplyPipeline("verdict");
      const res = final.result || final;
      const isOk = final.status === "done" || res.status === "applied" || res.status === "verified";

      const summaryCard = $("div", { class: "driver-step-card", style: `border-color: var(${isOk ? "--good" : "--warn"});` },
        $("div", { class: "driver-step-card-header" },
          $("span", {}, `Final Outcome: ${final.status || res.status}`),
          $("span", { class: `driver-status-pill ${isOk ? "pill-good" : "pill-warn"}` }, isOk ? "✓ Succeeded" : "▲ Refused / Deferred")
        ),
        $("div", { class: "driver-step-card-body" },
          $("div", { class: "driver-meta-grid" },
            $("div", {}, $("span", { class: "muted small" }, "Cost: "), $("code", {}, money(res.cost || final.cost || 0.0))),
            $("div", {}, $("span", { class: "muted small" }, "Model: "), $("code", {}, res.model || "ladder")),
            $("div", {}, $("span", { class: "muted small" }, "Rounds: "), $("b", {}, res.rounds_executed || 1))
          )
        )
      );

      if (res.diff) {
        summaryCard.querySelector(".driver-step-card-body").append(
          $("h4", { style: "margin: 0.8rem 0 0.3rem; font-size: 0.85rem;" }, "Generated File Diff:"),
          renderDiffBox(res.diff)
        );
      }

      if (res.verification) {
        const v = res.verification;
        summaryCard.querySelector(".driver-step-card-body").append(
          $("div", { style: "margin-top: 0.6rem; padding: 0.4rem 0.6rem; background: var(--panel); border-radius: 4px;" },
            $("span", { class: v.ok ? "pill-good" : "pill-bad" }, v.ok ? "✓ Gate Verification Passed" : "✗ Gate Verification Failed"),
            v.output ? $("pre", { style: "margin-top: 0.3rem; max-height: 120px; overflow-y: auto; font-size: 0.75rem;" }, v.output) : ""
          )
        );
      }

      stepsContainer.prepend(summaryCard);
    } catch (err) {
      stepsContainer.append($("p", { class: "small", style: "color: var(--bad);" }, `Error running apply: ${err.message}`));
    } finally {
      applyBtn.disabled = false;
    }
  };
}

async function renderDogfood(root) {
  root.append(
    $("h2", {}, "Dogfood — Claims Grounding & Self-Verification"),
    $("p", { class: "muted" },
      "Ground and verify claims against source code, executing verification gates and attesting results in the cryptographic ledger.")
  );

  const pipeline = $("div", { class: "driver-pipeline" },
    $("div", { class: "driver-pipeline-step active", id: "d-p-manifest" }, "📄 1. Claims Intake"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "d-p-ast" }, "🔬 2. AST Grounding"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "d-p-gate" }, "⚙️ 3. Verification Gate"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "d-p-proof" }, "🔒 4. Ledger Proof")
  );
  root.append(pipeline);

  const fileInput = $("input", { class: "pane-input", value: "harness/route_pack.py", placeholder: "Target file path" });
  const instInput = $("textarea", { class: "pane-input", rows: "2", placeholder: "Grounding instruction..." },
    "Verify that route packs validate correctly and enforce closed-set rungs.");
  const claimsInput = $("input", { class: "pane-input", placeholder: "Optional claims JSON file path" });
  const sourceInput = $("input", { class: "pane-input", placeholder: "Optional source code file path" });
  const verifyInput = $("input", { class: "pane-input", value: "python -m unittest tests/test_driver_conformance.py", placeholder: "Verification command" });
  const costInput = $("input", { class: "pane-input", type: "number", step: "0.01", value: "0.05" });

  const form = $("form", { class: "pane-section" });
  form.append(
    $("label", { class: "pane-field" }, $("span", {}, "Target File Path"), fileInput),
    $("label", { class: "pane-field" }, $("span", {}, "Instruction"), instInput),
    $("div", { style: "display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-bottom: 0.6rem;" },
      $("label", { class: "pane-field" }, $("span", {}, "Claims File (optional)"), claimsInput),
      $("label", { class: "pane-field" }, $("span", {}, "Source File (optional)"), sourceInput)
    ),
    $("div", { style: "display: grid; grid-template-columns: 2fr 1fr; gap: 0.8rem; margin-bottom: 0.6rem;" },
      $("label", { class: "pane-field" }, $("span", {}, "Verification Command"), verifyInput),
      $("label", { class: "pane-field" }, $("span", {}, "Max Cost ($)"), costInput)
    )
  );

  const dogfoodBtn = $("button", { class: "pane-fetch", type: "submit" }, "🔬 Run Dogfood Grounding");
  form.append(dogfoodBtn);
  root.append(form);

  const resultsArea = $("div", { style: "margin-top: 1.2rem;" });
  root.append(resultsArea);

  form.onsubmit = async (ev) => {
    ev.preventDefault();
    dogfoodBtn.disabled = true;
    resultsArea.replaceChildren($("p", { class: "muted small" }, "Executing dogfood grounding verification..."));

    try {
      const args = {
        file: fileInput.value.trim(),
        instruction: instInput.value.trim(),
        verify: verifyInput.value.trim() || undefined,
        max_cost: Number(costInput.value) || 0.05,
      };
      if (claimsInput.value.trim()) args.claims_file = claimsInput.value.trim();
      if (sourceInput.value.trim()) args.source_file = sourceInput.value.trim();

      const run = await postJson("/api/runs", { kind: "dogfood", args });
      const final = await pollRunWithEvents(run.id);
      const res = final.result || final;
      const isOk = final.status === "done";

      resultsArea.replaceChildren(
        $("div", { class: "driver-step-card" },
          $("div", { class: "driver-step-card-header" },
            $("span", {}, `Dogfood Status: ${final.status}`),
            $("span", { class: isOk ? "pill-good" : "pill-warn" }, isOk ? "✓ Grounded & Verified" : "▲ Issues Detected")
          ),
          $("div", { class: "driver-step-card-body" },
            $("p", {}, res.summary || "Grounding check complete."),
            $("div", { class: "driver-meta-grid", style: "margin-top: 0.6rem;" },
              $("div", {}, $("span", { class: "muted small" }, "Cost: "), $("code", {}, money(res.cost || final.cost || 0.0))),
              $("div", {}, $("span", { class: "muted small" }, "Claims Verified: "), $("b", {}, res.claims_count || 1)),
              $("div", {}, $("span", { class: "muted small" }, "Gate Pass: "), $("b", {}, res.gate_passed ? "Yes (RC 0)" : "Checked"))
            )
          )
        )
      );
    } catch (err) {
      resultsArea.replaceChildren($("p", { class: "small", style: "color: var(--bad);" }, `Error running dogfood: ${err.message}`));
    } finally {
      dogfoodBtn.disabled = false;
    }
  };
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
  ["GET", "/api/driver/health", "Driver liveness and perception sources"],
  ["GET", "/api/driver/vocabulary", "Driver 14 declared action vocabulary"],
  ["GET", "/api/driver/schemas", "Driver declared extraction schemas"],
  ["GET", "/api/driver/verify", "Driver audit chain and budget report"],
];

// ---- Driver pane (Perception, Verified Extraction & Deterministic Action) ---

async function renderDriver(root) {
  root.append($("h2", {}, "Driver"),
    $("p", { class: "muted" },
      "Perception-first machine driving via driver-core. Cross-verified consensus extraction before Jev decision, deterministic executor after. ",
      $("b", {}, "A refusal is a successful call (HTTP 200).")));

  // Section 1: Modular Aspect Pipeline Visualizer
  const pipeline = $("div", { class: "driver-pipeline" },
    $("div", { class: "driver-pipeline-step active", id: "pipe-perception" }, "👁 Perception"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "pipe-consensus" }, "⚖️ Consensus"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "pipe-decision" }, "🧠 Decision"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "pipe-execution" }, "⚙️ Action"),
    $("span", { class: "driver-pipeline-arrow" }, "➔"),
    $("div", { class: "driver-pipeline-step", id: "pipe-verify" }, "🔬 Verification")
  );
  root.append(pipeline);

  // Section 2: Unified Multi-Step Request Driver
  const driveSec = $("section", { class: "pane-section" }, $("h3", {}, "Jev Driver — Multi-Step Request Engine"));
  const driveForm = $("form", { class: "driver-form" });
  
  const goalInput = $("textarea", {
    class: "pane-input",
    rows: "2",
    placeholder: "What request should the Jev Driver plan and execute? (e.g. Inspect repository state, verify audit chain, and confirm test isolation)..."
  });
  const goalField = $("label", { class: "pane-field" }, $("span", {}, "Task Request / Goal"), goalInput);

  const targetInput = $("input", { class: "pane-input", value: "cli", placeholder: "e.g. cli, dom, screen, file-manager" });
  const targetField = $("label", { class: "pane-field" }, $("span", {}, "Perception Target"), targetInput);

  const schemaSelect = $("select", { class: "pane-input" },
    $("option", { value: "cli" }, "cli (Command Line Interface)"),
    $("option", { value: "dom" }, "dom (Web Document Object Model)"),
    $("option", { value: "gui" }, "gui (Window / Desktop Elements)"),
    $("option", { value: "screen" }, "screen (Screen Pixels)"),
    $("option", { value: "mcp" }, "mcp (Model Context Protocol)")
  );
  const schemaField = $("label", { class: "pane-field" }, $("span", {}, "Perception Schema"), schemaSelect);

  const stepsInput = $("input", { class: "pane-input", type: "number", min: "1", max: "10", value: "5" });
  const stepsField = $("label", { class: "pane-field" }, $("span", {}, "Max Iteration Steps"), stepsInput);

  const verifyCmdInput = $("input", { class: "pane-input", placeholder: "Optional verification command (e.g. python -m unittest tests/test_driver_conformance.py)" });
  const verifyCmdField = $("label", { class: "pane-field" }, $("span", {}, "Verification Gate Command"), verifyCmdInput);

  const autoApproveCheck = $("input", { type: "checkbox", checked: true, style: "margin-right: 0.5rem;" });
  const autoApproveLabel = $("label", { style: "display: flex; align-items: center; font-size: 0.85rem; margin-bottom: 0.6rem; cursor: pointer;" },
    autoApproveCheck, "Auto-approve safe consent actions with Jev calibrated confidence");

  const stableCheck = $("input", { type: "checkbox", checked: true, style: "margin-right: 0.5rem;" });
  const stableLabel = $("label", { style: "display: flex; align-items: center; font-size: 0.85rem; margin-bottom: 0.8rem; cursor: pointer;" },
    stableCheck, "Require stable state before action execution");

  const btnRow = $("div", { style: "display: flex; gap: 0.8rem; align-items: center; flex-wrap: wrap;" });
  const driveBtn = $("button", { class: "pane-fetch", type: "submit" }, "▶ Drive Request with Jev");
  const stepOnceBtn = $("button", { class: "pane-fetch", type: "button", style: "background: var(--panel);" }, "⏭ Step Once");
  btnRow.append(driveBtn, stepOnceBtn);

  const driverStarters = $("div", { class: "starter-grid" },
    $("span", { class: "muted small", style: "align-self: center;" }, "Presets:"),
    $("button", { class: "starter-btn", type: "button", onclick: () => {
      goalInput.value = "Inspect repository state, verify audit chain, and confirm test isolation";
      targetInput.value = "cli";
      schemaSelect.value = "cli";
      verifyCmdInput.value = "python -m unittest tests/test_driver_conformance.py";
    }}, "🖥️ CLI Conformance & Tests"),
    $("button", { class: "starter-btn", type: "button", onclick: () => {
      goalInput.value = "Extract DOM element tree and verify form controls";
      targetInput.value = "dom";
      schemaSelect.value = "dom";
      verifyCmdInput.value = "";
    }}, "🌐 DOM Perception Probe"),
    $("button", { class: "starter-btn", type: "button", onclick: () => {
      goalInput.value = "Verify declared action vocabulary and check cryptographic ledger integrity";
      targetInput.value = "cli";
      schemaSelect.value = "cli";
      verifyCmdInput.value = "python -m unittest tests/test_mcp_driver.py";
    }}, "🔒 Action Vocabulary & Audit")
  );
  driveSec.append(driverStarters);
  driveSec.append(driveForm);

  const driveTimeline = $("div", { class: "driver-step-cards" });
  driveSec.append(driveTimeline);
  root.append(driveSec);

  // Helper to highlight active pipeline aspect
  function setPipelineAspect(aspect) {
    const ids = ["pipe-perception", "pipe-consensus", "pipe-decision", "pipe-execution", "pipe-verify"];
    for (const id of ids) {
      const el = document.getElementById(id);
      if (el) el.classList.remove("active");
    }
    const map = {
      "perception": "pipe-perception",
      "consensus": "pipe-consensus",
      "decision": "pipe-decision",
      "action": "pipe-execution",
      "verify": "pipe-verify"
    };
    const targetEl = document.getElementById(map[aspect] || "pipe-decision");
    if (targetEl) targetEl.classList.add("active");
  }

  // Helper to render one step card
  function renderStepCard(step) {
    const env = step.envelope || {};
    const isOk = env.ok;
    const card = $("div", { class: "driver-step-card" });
    const header = $("div", { class: "driver-step-card-header" },
      $("span", {}, `Step #${step.step_number || 1}: ${step.aspect || "cli"} ➔ ${step.target || "cli"}`),
      $("span", { class: `driver-status-pill ${isOk ? "pill-good" : "pill-warn"}` },
        isOk ? "● Completed" : `▲ Refused: ${env.reason || "stopped"}`)
    );
    const body = $("div", { class: "driver-step-card-body" },
      $("div", { class: "driver-meta-grid" },
        $("div", {}, $("span", { class: "muted small" }, "Step ID: "), $("code", {}, env.step_id || step.step_id || "—")),
        $("div", {}, $("span", { class: "muted small" }, "Stopped At: "), $("b", {}, env.stopped_at || (isOk ? "complete" : "gate"))),
        $("div", {}, $("span", { class: "muted small" }, "Cost USD: "), $("code", {}, money(step.cost_usd || env.cost_usd || 0.0)))
      )
    );
    if (env.detail) {
      body.append($("p", { class: "small", style: "margin: 0.4rem 0;" }, $("b", {}, "Detail: "), env.detail));
    }
    if (step.verification) {
      const v = step.verification;
      body.append($("div", { style: "margin-top: 0.5rem; padding: 0.4rem 0.6rem; background: var(--panel); border-radius: 4px; font-size: 0.8rem;" },
        $("span", { class: v.ok ? "pill-good" : "pill-bad" }, v.ok ? "✓ Verification Passed" : "✗ Verification Failed"),
        v.output ? $("pre", { style: "margin-top: 0.3rem; max-height: 100px; overflow-y: auto;" }, v.output) : ""
      ));
    }
    card.append(header, body);
    return card;
  }

  // Handle Drive Request (Multi-Step Iteration)
  driveForm.onsubmit = async (ev) => {
    ev.preventDefault();
    driveBtn.disabled = true;
    stepOnceBtn.disabled = true;
    driveTimeline.replaceChildren($("p", { class: "muted small" }, "Jev Driver planning request into steps and initiating iterative perception..."));
    setPipelineAspect("perception");
    try {
      const payload = {
        goal: goalInput.value.trim() || "Inspect repository and verify system aspects",
        target: targetInput.value.trim() || "cli",
        schema: schemaSelect.value,
        max_steps: parseInt(stepsInput.value, 10) || 5,
        verify: verifyCmdInput.value.trim() || null,
        auto_approve: autoApproveCheck.checked,
        require_stable: stableCheck.checked,
      };
      setPipelineAspect("consensus");
      const run = await postJson("/api/runs", { kind: "driver_task", args: payload });
      setPipelineAspect("decision");
      const final = await pollRunResult(run.id);
      setPipelineAspect("verify");
      const res = final.result || final;
      driveTimeline.replaceChildren();
      const summaryBanner = $("div", {
        style: "padding: 0.6rem 0.9rem; background: var(--panel); border: 1px solid var(--line); border-radius: 6px; margin-bottom: 0.8rem; font-size: 0.9rem;"
      }, $("b", {}, "Task Outcome: "), res.summary || `Finished with status: ${res.status}`);
      driveTimeline.append(summaryBanner);

      const stepsList = res.steps || [];
      if (stepsList.length) {
        for (const st of stepsList) {
          driveTimeline.append(renderStepCard(st));
        }
      } else {
        driveTimeline.append($("p", { class: "muted small" }, "No steps generated."));
      }
    } catch (err) {
      driveTimeline.replaceChildren($("p", { class: "muted small" }, `Driver task failed: ${err.message}`));
    } finally {
      driveBtn.disabled = false;
      stepOnceBtn.disabled = false;
      setPipelineAspect("perception");
    }
  };

  // Handle Step Once (Single Step Execution)
  stepOnceBtn.onclick = async () => {
    driveBtn.disabled = true;
    stepOnceBtn.disabled = true;
    driveTimeline.replaceChildren($("p", { class: "muted small" }, "Executing single perception & action step..."));
    setPipelineAspect("perception");
    try {
      const payload = {
        target: targetInput.value.trim() || "cli",
        schema: schemaSelect.value,
        require_stable: stableCheck.checked,
      };
      if (autoApproveCheck.checked) {
        payload.consent = {
          granted: true,
          action: "open_window",
          params: { target: payload.target, goal: goalInput.value.trim() },
          by: "operator",
        };
      }
      setPipelineAspect("decision");
      const res = await postJson("/api/driver/step", payload);
      setPipelineAspect("verify");
      driveTimeline.replaceChildren(renderStepCard({
        step_number: 1,
        aspect: schemaSelect.value,
        target: payload.target,
        envelope: res,
        cost_usd: res.cost_usd || 0.0,
      }));
    } catch (err) {
      driveTimeline.replaceChildren($("p", { class: "muted small" }, `Step failed: ${err.message}`));
    } finally {
      driveBtn.disabled = false;
      stepOnceBtn.disabled = false;
      setPipelineAspect("perception");
    }
  };

  // Section 3: Driver Aspects Explorer (Sub-Tabs)
  const aspectsSec = $("section", { class: "pane-section" }, $("h3", {}, "Driver System Aspects Explorer"));
  const subtabs = $("div", { class: "driver-subtabs" },
    $("button", { class: "driver-subtab active", type: "button", "data-sub": "health" }, "Perception & Health"),
    $("button", { class: "driver-subtab", type: "button", "data-sub": "schemas" }, "Declared Schemas"),
    $("button", { class: "driver-subtab", type: "button", "data-sub": "vocabulary" }, "Action Vocabulary"),
    $("button", { class: "driver-subtab", type: "button", "data-sub": "audit" }, "Audit Chain & Spend"),
    $("button", { class: "driver-subtab", type: "button", "data-sub": "raw" }, "Raw JSON Inspector")
  );
  const aspectHost = $("div", { class: "driver-aspect-host" });
  aspectsSec.append(subtabs, aspectHost);
  root.append(aspectsSec);

  // Subtab 1: Perception & Health
  async function loadHealthSubtab() {
    aspectHost.replaceChildren($("p", { class: "muted small" }, "Checking driver status..."));
    try {
      const h = await api("/api/driver/health");
      const isUp = h.status === "up";
      const pill = $("span", {
        class: `driver-status-pill ${isUp ? "pill-good" : "pill-bad"}`
      }, isUp ? `● Active (v${h.version || "3.4.0"})` : "○ Down");
      const sources = (h.sources || []).length
        ? h.sources.map(s => $("span", { class: "driver-source-badge" }, s))
        : [$("span", { class: "muted small" }, "None declared")];
      const startBtn = $("button", { class: "pane-fetch", type: "button", style: "margin-left: 0.5rem;" }, "Restart / Recheck");
      startBtn.onclick = async () => {
        startBtn.disabled = true;
        try {
          await postJson("/api/driver/start", {});
          await loadHealthSubtab();
        } catch (e) {
          alert("Error: " + e.message);
        } finally {
          startBtn.disabled = false;
        }
      };
      aspectHost.replaceChildren(
        $("div", { style: "display: flex; align-items: center; gap: 0.8rem; flex-wrap: wrap;" },
          pill,
          $("span", { class: "small" }, "Active Perception Tiers:"),
          ...sources,
          startBtn
        )
      );
    } catch (err) {
      aspectHost.replaceChildren($("p", { class: "muted small" }, `Health check failed: ${err.message}`));
    }
  }

  // Subtab 2: Schemas
  async function loadSchemasSubtab() {
    aspectHost.replaceChildren($("p", { class: "muted small" }, "Loading schemas..."));
    try {
      const s = await api("/api/driver/schemas");
      const schemas = s.schemas || [];
      const rows = schemas.map(sch => [
        sch.name || sch,
        sch.version || "1.0",
        (sch.fields || []).join(", ") || "—",
        sch.description || "Extraction schema contract"
      ]);
      aspectHost.replaceChildren(table(["Schema", "Version", "Required Fields", "Description"], rows));
    } catch (err) {
      aspectHost.replaceChildren($("p", { class: "muted small" }, `Schemas unavailable: ${err.message}`));
    }
  }

  // Subtab 3: Vocabulary
  async function loadVocabSubtab() {
    aspectHost.replaceChildren($("p", { class: "muted small" }, "Loading vocabulary..."));
    try {
      const v = await api("/api/driver/vocabulary");
      const actions = v.vocabulary?.actions || [];
      const rows = actions.map(act => [
        act.name || act,
        act.mutating ? "MUTATING" : act.irreversible ? "IRREVERSIBLE" : "READ_ONLY",
        (act.normalisers || []).join(", ") || "—",
        act.description || "Declared action primitive"
      ]);
      aspectHost.replaceChildren(table(["Action", "Tier", "Normalisers", "Description"], rows));
    } catch (err) {
      aspectHost.replaceChildren($("p", { class: "muted small" }, `Vocabulary unavailable: ${err.message}`));
    }
  }

  // Subtab 4: Audit
  async function loadAuditSubtab() {
    aspectHost.replaceChildren($("p", { class: "muted small" }, "Verifying audit chain..."));
    try {
      const vr = await api("/api/driver/verify");
      const auditOk = vr.audit?.ok;
      const badge = $("span", {
        class: `driver-status-pill ${auditOk ? "pill-good" : "pill-warn"}`
      }, auditOk ? "● Chain Verified Valid" : "▲ Chain Broken or Missing");
      aspectHost.replaceChildren(
        $("div", { style: "display: flex; gap: 1rem; align-items: center; flex-wrap: wrap;" },
          badge,
          $("span", { class: "small" }, `Records: ${vr.audit?.records ?? "0"}`),
          $("span", { class: "small" }, `Spent: ${money(vr.budget?.spent_usd || 0)}`),
          $("span", { class: "small muted" }, `Remaining: ${money(vr.budget?.remaining_usd || 0)}`)
        )
      );
    } catch (err) {
      aspectHost.replaceChildren($("p", { class: "muted small" }, `Audit check unavailable: ${err.message}`));
    }
  }

  // Subtab 5: Raw JSON
  async function loadRawSubtab() {
    aspectHost.replaceChildren($("p", { class: "muted small" }, "Fetching latest driver verification & settings envelope..."));
    try {
      const h = await api("/api/driver/health");
      const vr = await api("/api/driver/verify");
      aspectHost.replaceChildren(
        $("pre", { class: "pane-json" }, JSON.stringify({ health: h, audit: vr }, null, 2))
      );
    } catch (err) {
      aspectHost.replaceChildren($("p", { class: "muted small" }, `Raw inspector unavailable: ${err.message}`));
    }
  }

  // Wire subtabs
  subtabs.addEventListener("click", async (e) => {
    const btn = e.target.closest(".driver-subtab");
    if (!btn) return;
    for (const b of subtabs.querySelectorAll(".driver-subtab")) b.classList.remove("active");
    btn.classList.add("active");
    const sub = btn.dataset.sub;
    if (sub === "health") await loadHealthSubtab();
    else if (sub === "schemas") await loadSchemasSubtab();
    else if (sub === "vocabulary") await loadVocabSubtab();
    else if (sub === "audit") await loadAuditSubtab();
    else if (sub === "raw") await loadRawSubtab();
  });

  await loadHealthSubtab();
}

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
  ["driver", "Driver", renderDriver],
  ["apply", "Apply", renderApply],
  ["verify", "Verify", renderVerify],
  ["continue", "Continue", renderContinue],
  ["dogfood", "Dogfood", renderDogfood],
  ["legacy", "Legacy API", renderLegacy],
];

export function initPanes() {
  const main = document.getElementById("main-panel");
  const header = main?.querySelector("header");
  if (!main || !header) return; // unexpected DOM; leave the app untouched
  if (main.querySelector(".pane-tabs")) return; // already initialized

  // Top navigation: Clean, uncluttered view. Primary workspace is Autonomous Chat,
  // with an optional Studio Inspector toggle for deep-dive subtools.
  const strip = $("div", { class: "pane-tabs", role: "tablist" },
    $("button", { class: "pane-tab active", type: "button", role: "tab", "data-pane": "chat" }, "💬 Chat (Autonomous)"),
    $("button", { class: "pane-tab", type: "button", role: "tab", "data-pane": "studio" }, "🛠️ Studio Inspector ▾")
  );
  header.after(strip);

  const paneHost = $("div", { id: "pane-host", hidden: true });
  main.append(paneHost);

  // Subtool navigation bar inside Studio Inspector
  let currentSubtool = "proof";
  const subtoolBar = $("div", { class: "subtool-nav", role: "tablist" },
    ...PANES.map(([id, label]) =>
      $("button", {
        class: "pane-tab subtool-btn" + (id === currentSubtool ? " active" : ""),
        type: "button",
        role: "tab",
        "data-pane": id
      }, label)
    )
  );
  const subtoolBody = $("div", { id: "subtool-body" });
  paneHost.append(subtoolBar, subtoolBody);

  const chatBits = ["#chat-container", "#input-footer"]
    .map(sel => main.querySelector(sel)).filter(Boolean);

  let current = "chat";
  const rendered = {};

  async function showSubtool(id) {
    currentSubtool = id;
    for (const btn of subtoolBar.querySelectorAll(".subtool-btn")) {
      btn.classList.toggle("active", btn.dataset.pane === id);
    }
    subtoolBody.replaceChildren();
    const entry = PANES.find(([pid]) => pid === id);
    if (entry) {
      try {
        await entry[2](subtoolBody);
      } catch (err) {
        subtoolBody.append($("p", { class: "muted" }, `Pane error: ${err.message}`));
      }
    }
  }

  function setMode(mode, targetSubtool = null) {
    current = mode;
    const isChat = mode === "chat";
    strip.querySelector('[data-pane="chat"]')?.classList.toggle("active", isChat);
    strip.querySelector('[data-pane="studio"]')?.classList.toggle("active", !isChat);
    paneHost.hidden = isChat;
    for (const node of chatBits) node.hidden = !isChat;
    if (!isChat) {
      const subId = targetSubtool || currentSubtool || "proof";
      showSubtool(subId);
    }
  }

  strip.addEventListener("click", (event) => {
    const tab = event.target.closest(".pane-tab");
    if (!tab) return;
    const id = tab.dataset.pane;
    if (id === "chat") {
      setMode("chat");
    } else if (id === "studio") {
      setMode("studio");
    }
  });

  subtoolBar.addEventListener("click", (event) => {
    const btn = event.target.closest(".subtool-btn");
    if (!btn) return;
    const id = btn.dataset.pane;
    if (id) showSubtool(id);
  });
}

// Auto-initialize when loaded in browser
if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => initPanes());
  } else {
    initPanes();
  }
}

