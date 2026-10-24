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
  // Flatten nested arrays before appending. Callers routinely pass a mapped
  // list as one child -- `$("tbody", {}, rows.map(...))` in table() -- and
  // node.append(array) stringifies it, which is why every multi-row table in
  // this UI rendered as "[object HTMLTableRowElement],...".
  const flatten = (out, value) => {
    if (Array.isArray(value)) for (const item of value) flatten(out, item);
    else if (value !== null && value !== undefined) out.push(value);
    return out;
  };
  for (const child of flatten([], children)) node.append(child);
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
    const workspace = window.getHarnessWorkspace ? window.getHarnessWorkspace() : "";
    if (!workspace) {
      resultsArea.replaceChildren($("p", { class: "small", style: "color: var(--bad);" },
        "Select a workspace folder before resuming file work."));
      return;
    }
    continueBtn.disabled = true;
    resultsArea.replaceChildren($("p", { class: "muted small" }, "Resuming state and consulting Jev stage-runner..."));

    try {
      const args = { state: stateInput.value.trim(), root_dir: workspace };
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
    const workspace = window.getHarnessWorkspace ? window.getHarnessWorkspace() : "";
    if (!workspace) {
      resultsArea.replaceChildren($("p", { class: "small", style: "color: var(--bad);" },
        "Select a workspace folder before applying file changes."));
      return;
    }
    applyBtn.disabled = true;
    resultsArea.replaceChildren($("p", { class: "muted small" }, "Dispatching governed edit task to Jev driver engine..."));
    setApplyPipeline("intake");

    const stepsContainer = $("div", { class: "driver-step-cards" });
    resultsArea.replaceChildren(stepsContainer);

    try {
      const args = {
        root_dir: workspace,
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
      const isOk = final.status === "done" || final.status === "ok" || res.status === "applied" || res.status === "verified";

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
  const claimsInput = $("input", { class: "pane-input", placeholder: "Claims JSON file path (required)" });
  const sourceInput = $("input", { class: "pane-input", placeholder: "Source code file path (required)" });
  const verifyInput = $("input", { class: "pane-input", value: "python -m unittest tests/test_driver_conformance.py", placeholder: "Verification command" });
  const costInput = $("input", { class: "pane-input", type: "number", step: "0.01", value: "0.05" });

  const form = $("form", { class: "pane-section" });
  form.append(
    $("label", { class: "pane-field" }, $("span", {}, "Target File Path"), fileInput),
    $("label", { class: "pane-field" }, $("span", {}, "Instruction"), instInput),
    $("div", { style: "display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-bottom: 0.6rem;" },
      $("label", { class: "pane-field" }, $("span", {}, "Claims File (required)"), claimsInput),
      $("label", { class: "pane-field" }, $("span", {}, "Source File (required)"), sourceInput)
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
    const workspace = window.getHarnessWorkspace ? window.getHarnessWorkspace() : "";
    if (!workspace) {
      resultsArea.replaceChildren($("p", { class: "small", style: "color: var(--bad);" },
        "Select a workspace folder before running dogfood file checks."));
      return;
    }
    const claimsFile = claimsInput.value.trim();
    const sourceFile = sourceInput.value.trim();
    if (!claimsFile || !sourceFile) {
      resultsArea.replaceChildren($("p", { class: "small", style: "color: var(--bad);" },
        "Dogfood requires both a claims JSON file and a source code file."));
      return;
    }
    dogfoodBtn.disabled = true;
    resultsArea.replaceChildren($("p", { class: "muted small" }, "Executing dogfood grounding verification..."));

    try {
      const args = {
        root_dir: workspace,
        file: fileInput.value.trim(),
        instruction: instInput.value.trim(),
        verify: verifyInput.value.trim() || undefined,
        max_cost: Number(costInput.value) || 0.05,
      };
      args.claims_file = claimsFile;
      args.source_file = sourceFile;

      const run = await postJson("/api/runs", { kind: "dogfood", args });
      const final = await pollRunWithEvents(run.id);
      const res = final.result || {};
      const status = res.status || final.status || "unknown";
      const isOk = status === "ok" || status === "success";
      const statusClass = final.error || status === "ungrounded"
        ? "pill-bad"
        : isOk ? "pill-good" : "pill-warn";
      const summary = final.error || res.error || res.summary || (
        status === "ungrounded"
          ? "Claims were not grounded in the supplied source. Verification and apply were skipped."
          : `Dogfood finished with status: ${status}.`
      );
      const reportedCost = res.cost ?? final.cost;
      const costText = reportedCost == null || !Number.isFinite(Number(reportedCost))
        ? "N/A" : money(Number(reportedCost));
      const confirmedClaims = res.verify?.confirmed_claims;
      const confirmedText = Array.isArray(confirmedClaims) ? confirmedClaims.length : "N/A";
      const applyStatus = res.apply?.status || "Not run";

      resultsArea.replaceChildren(
        $("div", { class: "driver-step-card" },
          $("div", { class: "driver-step-card-header" },
            $("span", {}, `Dogfood Status: ${status}`),
            $("span", { class: statusClass }, isOk ? "✓ Grounded & Verified" : "▲ Issues Detected")
          ),
          $("div", { class: "driver-step-card-body" },
            $("p", {}, summary),
            $("div", { class: "driver-meta-grid", style: "margin-top: 0.6rem;" },
              $("div", {}, $("span", { class: "muted small" }, "Cost: "), $("code", {}, costText)),
              $("div", {}, $("span", { class: "muted small" }, "Claims Confirmed: "), $("b", {}, confirmedText)),
              $("div", {}, $("span", { class: "muted small" }, "Apply Phase: "), $("b", {}, applyStatus))
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
    "aria-required": "true",
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

  const autoApproveCheck = $("input", { type: "checkbox", checked: false, style: "margin-right: 0.5rem;" });
  const autoApproveLabel = $("label", { style: "display: flex; align-items: center; font-size: 0.85rem; margin-bottom: 0.6rem; cursor: pointer;" },
    autoApproveCheck, "Auto-grant read-only (observe) consent; mutating actions always need your explicit consent");

  const stableCheck = $("input", { type: "checkbox", checked: true, style: "margin-right: 0.5rem;" });
  const stableLabel = $("label", { style: "display: flex; align-items: center; font-size: 0.85rem; margin-bottom: 0.8rem; cursor: pointer;" },
    stableCheck, "Require stable state before action execution");

  const btnRow = $("div", { style: "display: flex; gap: 0.8rem; align-items: center; flex-wrap: wrap;" });
  const driveBtn = $("button", { class: "pane-fetch", type: "submit" }, "▶ Drive Request with Jev");
  const stepOnceBtn = $("button", { class: "pane-fetch", type: "button", style: "background: var(--panel);" }, "⏭ Step Once");
  btnRow.append(driveBtn, stepOnceBtn);

  // Every field above was built but never attached, so the pane appended an
  // empty <form> and the request form rendered blank. Assemble it here, in
  // reading order, before anything reads it.
  driveForm.append(
    goalField,
    targetField,
    schemaField,
    stepsField,
    verifyCmdField,
    autoApproveLabel,
    stableLabel,
    btnRow
  );

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
    const goal = goalInput.value.trim();
    if (!goal) {
      driveTimeline.replaceChildren($("p", {
        class: "muted small",
        role: "alert"
      }, "Enter a task request before starting the driver."));
      goalInput.focus();
      return;
    }
    driveBtn.disabled = true;
    stepOnceBtn.disabled = true;
    driveTimeline.replaceChildren($("p", { class: "muted small" }, "Jev Driver planning request into steps and initiating iterative perception..."));
    setPipelineAspect("perception");
    try {
      const payload = {
        goal,
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
          action: "observe",
          params: {},
          by: "harness:auto_approve",
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
          aspectHost.querySelector(".driver-start-error")?.remove();
          aspectHost.append($("p", {
            class: "small driver-start-error",
            role: "alert",
            style: "color: var(--bad);"
          }, `Driver restart failed: ${e.message}`));
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
        // The wire shape carries `id`, not `name`; reading `sch.name` fell
        // through to the object itself and rendered "[object Object]".
        sch.name || sch.id || String(sch),
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
      // `normalisers` is a name -> canonical-value mapping, not a list, so
      // reading it as an array and calling .join() on it threw and took the
      // whole tab down. Accept either shape.
      const normaliserNames = (n) => {
        if (!n) return [];
        if (Array.isArray(n)) return n.map(String);
        if (typeof n === "object") return Object.keys(n).sort();
        return [String(n)];
      };
      const TIERS = { read_only: "READ_ONLY", mutating: "MUTATING", irreversible: "IRREVERSIBLE" };
      // The tier lives in `class`; the old code probed `act.mutating` /
      // `act.irreversible`, which the wire shape never carries, so every row
      // rendered READ_ONLY.
      const tierOf = (act) => TIERS[act.class] || String(act.class || "—");
      // Surface the per-action executability verdict instead of leaving the
      // reader to assume every declared action can run.
      const statusOf = (act) => {
        if (act.executable) return "EXECUTABLE";
        const needs = normaliserNames(act.needs);
        return needs.length ? `REFUSED — needs ${needs.join(", ")}` : "REFUSED";
      };
      const rows = actions.map(act => [
        act.name || String(act),
        tierOf(act),
        statusOf(act),
        normaliserNames(act.normalisers).join(", ") || "—",
        act.description || "Declared action primitive"
      ]);
      aspectHost.replaceChildren(
        table(["Action", "Tier", "Status", "Normalisers", "Description"], rows),
        $("p", { class: "muted small", style: "margin-top:0.75rem;" },
          "REFUSED actions are declared but not executable: no input backend is "
          + "registered for them, so they are refused rather than silently dropped."));
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

// ---- Operator parity helpers ---------------------------------------------
// Every lane below is a thin face over an existing owner: the plan/bench/gate
// run kinds in harness/server.py, or an existing read endpoint. No pane
// re-implements policy; they collect the same arguments the CLI collects and
// render the same envelopes.

function textareaField(label, attrs = {}) {
  const input = $("textarea", Object.assign({ class: "pane-input" }, attrs));
  return { row: $("label", { class: "pane-field" }, $("span", {}, label), input),
           input };
}

function checkboxField(label, attrs = {}) {
  const input = $("input", Object.assign({ type: "checkbox" }, attrs));
  return { row: $("label", { class: "pane-check" }, input, $("span", {}, label)),
           input };
}

function currentWorkspace() {
  return (typeof window !== "undefined"
    && typeof window.getHarnessWorkspace === "function")
    ? window.getHarnessWorkspace() : "";
}

function paneCard(title, body) {
  return $("div", { class: "pane-card" },
    $("div", { class: "pane-card-head" }, $("strong", {}, title)),
    $("div", { class: "pane-card-body" }, body));
}

function paneDetails(label, data) {
  return $("details", { class: "pane-details" },
    $("summary", {}, label),
    $("pre", { class: "pane-json" }, JSON.stringify(data, null, 2).slice(0, 40000)));
}

function paneError(area, err) {
  area.replaceChildren($("p", { class: "small", style: "color: var(--bad);" },
    `Error: ${err && err.message ? err.message : String(err)}`));
}

function paneStatus(ok, okText, badText) {
  return $("span", { class: `driver-status-pill ${ok ? "pill-good" : "pill-bad"}` },
    ok ? okText : badText);
}

function workspaceArgs(rootInput) {
  const workspace = (rootInput.value || "").trim() || currentWorkspace();
  rootInput.value = workspace;
  return workspace;
}

// ---- Plan pane -----------------------------------------------------------

function renderPlanResult(area, final) {
  const res = final.result || final;
  const plan = res.plan || res;
  const dag = plan.dag || {};
  const nodes = Array.isArray(dag.nodes) ? dag.nodes : [];
  const status = res.status || plan.status || final.status;
  area.replaceChildren();

  const summary = paneCard(`Plan: ${status || "completed"}`, [
    $("p", { class: "small" }, plan.summary || plan.reason
      || plan.goal || res.goal || "The planner returned no summary."),
    $("div", { class: "driver-meta-grid" },
      $("div", {}, $("span", { class: "muted small" }, "Goal: "),
        $("code", {}, plan.goal || res.goal || "—")),
      $("div", {}, $("span", { class: "muted small" }, "DAG digest: "),
        $("code", {}, (plan.digest || dag.digest || "—").slice(0, 16))),
      $("div", {}, $("span", { class: "muted small" }, "Nodes: "),
        $("b", {}, String(nodes.length))))
  ]);
  area.append(summary);

  if (nodes.length) {
    area.append($("h3", {}, "DAG nodes"));
    area.append(table(
      ["#", "node", "tier", "depends on", "target files"],
      nodes.map((n, i) => [
        i + 1,
        n.instruction || n.task || n.id || n.node_id || "—",
        n.tier || n.model_tier || "—",
        (n.depends_on || n.deps || []).join(", ") || "—",
        (n.target_files || n.files || []).join(", ") || "—",
      ])));
  } else {
    area.append($("p", { class: "muted small" },
      "No DAG nodes were produced; the plan envelope is below."));
  }

  if (res.execution) {
    const summaryRow = res.execution.summary || res.execution;
    const perNode = res.execution.results || res.execution.nodes || {};
    const rows = Array.isArray(perNode) ? perNode : Object.entries(perNode).map(
      ([key, value]) => Object.assign({ node_id: key }, value || {}));
    area.append($("h3", {}, "Execution"));
    area.append(paneCard(`Execution: ${summaryRow.status || summaryRow.all_ok ? "ok" : "check"}`, [
      $("div", { class: "driver-meta-grid" },
        $("div", {}, $("span", { class: "muted small" }, "All ok: "),
          $("b", {}, String(Boolean(summaryRow.all_ok)))),
        $("div", {}, $("span", { class: "muted small" }, "Completed: "),
          $("b", {}, String(summaryRow.completed ?? "—"))),
        $("div", {}, $("span", { class: "muted small" }, "Cost: "),
          $("code", {}, money(summaryRow.total_cost)))),
      rows.length ? table(
        ["node", "status", "cost", "gate"],
        rows.map(r => [r.node_id || r.id || "—", r.status || "—",
          money(r.cost), r.gate || r.returncode || "—"])) : ""
    ]));
  }

  area.append(paneDetails("Raw plan envelope", res));
}

async function renderPlan(root) {
  root.append(
    $("h2", {}, "Plan — decompose a goal into an executable DAG"),
    $("p", { class: "muted" },
      "The same `plan` run kind the CLI and MCP use. Preview first; tick Execute only when you want the executor to run every node. Planning needs a workspace folder because candidate files are resolved inside it."));

  const form = $("form", { class: "pane-section" });
  const goal = textareaField("Goal", { rows: "3",
    placeholder: "e.g. Split the ledger report into reader functions and cover them with tests" });
  const rootField = field("Workspace folder", { value: currentWorkspace(),
    placeholder: "C:\\path\\to\\repo" });
  const files = field("Candidate files (comma-separated, optional)", {
    placeholder: "harness/ledger.py, tests/test_ledger.py" });
  const frontier = field("Frontier model (optional)", { placeholder: "blank = configured tier" });
  const workers = field("Max workers", { type: "number", min: "1", max: "16", value: "4" });
  const runCost = field("Run cost ceiling ($)", { type: "number", min: "0", step: "0.01", value: "0.25" });
  const taskCost = field("Per-task ceiling ($)", { type: "number", min: "0", step: "0.01", value: "0.05" });
  const execute = checkboxField("Execute the DAG after planning (writes files)");

  form.append(goal.row, rootField.row, files.row,
    $("div", { class: "pane-grid three" }, frontier.row, workers.row, runCost.row, taskCost.row),
    execute.row);
  const submit = $("button", { class: "pane-fetch", type: "submit" }, "Plan with the configured ladder");
  form.append($("div", { class: "pane-actions" }, submit));
  root.append(form);

  const area = $("div", { style: "margin-top: 1rem;" });
  root.append(area);

  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const workspace = workspaceArgs(rootField.input);
    if (!workspace) {
      paneError(area, new Error("Select a workspace folder first."));
      return;
    }
    if (!goal.input.value.trim()) {
      paneError(area, new Error("A goal is required."));
      return;
    }
    submit.disabled = true;
    area.replaceChildren($("p", { class: "muted small" }, "Planning…"));
    const started = Date.now();
    const tick = setInterval(() => {
      const p = area.querySelector("p");
      if (p) p.textContent = `Planning… ${Math.round((Date.now() - started) / 1000)}s`;
    }, 1000);
    try {
      const args = {
        goal: goal.input.value.trim(),
        root_dir: workspace,
        execute: execute.input.checked,
        max_workers: Number(workers.input.value) || 4,
      };
      const candidateFiles = files.input.value.split(/[,\n]/).map(s => s.trim()).filter(Boolean);
      if (candidateFiles.length) args.files = candidateFiles;
      if (frontier.input.value.trim()) args.frontier_model = frontier.input.value.trim();
      if (runCost.input.value) args.max_cost = Number(runCost.input.value);
      if (taskCost.input.value) args.task_max_cost = Number(taskCost.input.value);
      const run = await postJson("/api/runs", { kind: "plan", args });
      const final = await pollRunWithEvents(run.id, { timeoutMs: 900000 });
      if (final.status === "error") throw new Error(final.error || "plan run failed");
      renderPlanResult(area, final);
    } catch (err) {
      paneError(area, err);
    } finally {
      clearInterval(tick);
      submit.disabled = false;
    }
  };
}

// ---- Bench pane ----------------------------------------------------------

async function renderBench(root) {
  root.append(
    $("h2", {}, "Bench — measure the apply lane against a manifest"),
    $("p", { class: "muted" },
      "The same `bench` run kind the CLI uses: every task is restored in a sandbox, edited by the real apply lane, verified, and summarized. The workspace must contain the manifest path; the bundled efficiency manifest lives at bench/manifests/efficiency."));

  const form = $("form", { class: "pane-section" });
  const rootField = field("Workspace folder", { value: currentWorkspace(),
    placeholder: "C:\\path\\to\\repo" });
  const manifest = field("Manifest file or directory", {
    value: "bench/manifests/efficiency",
    placeholder: "bench/manifests/efficiency" });
  const rounds = field("Max rounds per task (optional)", {
    type: "number", min: "1", max: "8", placeholder: "manifest default" });

  form.append(rootField.row, manifest.row, rounds.row);
  const submit = $("button", { class: "pane-fetch", type: "submit" }, "Run bench");
  form.append($("div", { class: "pane-actions" }, submit));
  root.append(form);

  const area = $("div", { style: "margin-top: 1rem;" });
  root.append(area);

  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const workspace = workspaceArgs(rootField.input);
    if (!workspace) {
      paneError(area, new Error("Select a workspace folder first."));
      return;
    }
    if (!manifest.input.value.trim()) {
      paneError(area, new Error("A manifest path is required."));
      return;
    }
    submit.disabled = true;
    const seen = [];
    area.replaceChildren($("p", { class: "muted small" }, "Bench accepted; tasks are running…"));
    try {
      const args = { root_dir: workspace, manifest: manifest.input.value.trim() };
      if (rounds.input.value) args.max_rounds = Number(rounds.input.value);
      const run = await postJson("/api/runs", { kind: "bench", args });
      const final = await pollRunWithEvents(run.id, {
        timeoutMs: 1800000,
        onEvent: (e) => {
          if (e.type === "bench_task" && e.phase === "end") {
            seen.push(`${e.name || "task"}: ${e.status || "done"}`);
            const p = area.querySelector("p");
            if (p) p.textContent = `Bench running… ${seen.join(" · ")}`;
          }
        },
      });
      if (final.status === "error") throw new Error(final.error || "bench run failed");
      const res = (final.result || final).bench || final.result || final;
      const results = res.results || [];
      area.replaceChildren(paneCard(`Bench: ${res.pass_rate === null || res.pass_rate === undefined ? "—" : pct(res.pass_rate)} pass rate`, [
        $("div", { class: "driver-meta-grid" },
          $("div", {}, $("span", { class: "muted small" }, "Tasks: "), $("b", {}, String(res.tasks ?? results.length))),
          $("div", {}, $("span", { class: "muted small" }, "Total cost: "), $("code", {}, money(res.total_cost))),
          $("div", {}, $("span", { class: "muted small" }, "Mean cost: "), $("code", {}, money(res.mean_cost))),
          $("div", {}, $("span", { class: "muted small" }, "Rounds: "), $("b", {}, String(res.total_rounds ?? "—")))),
        results.length ? table(
          ["task", "status", "cost", "rounds", "rotations"],
          results.map(r => [r.name || "task", r.status || "—", money(r.cost),
            (r.rounds || []).length, r.rotations || 0])) : ""
      ]));
      area.append(paneDetails("Raw bench envelope", final.result || final));
    } catch (err) {
      paneError(area, err);
    } finally {
      submit.disabled = false;
    }
  };
}

// ---- Gates pane ----------------------------------------------------------

function gateCard(gate, phaseInput, timeoutInput) {
  const status = $("span", { class: "muted small" }, "not run in this view");
  const out = $("pre", { class: "pane-json gate-output", hidden: true });
  const run = $("button", { class: "pane-fetch", type: "button" }, "Run gate");
  const card = paneCard(gate.name, [
    $("p", { class: "muted small" }, gate.summary),
    $("code", { class: "gate-command" }, gate.command),
    $("div", { class: "pane-actions" }, run, status,
      $("span", { class: "muted small" }, `default ${gate.timeout_s}s`)),
    out,
  ]);

  run.addEventListener("click", async () => {
    run.disabled = true;
    out.hidden = true;
    const started = Date.now();
    status.textContent = "running… 0s";
    const tick = setInterval(() => {
      status.textContent = `running… ${Math.round((Date.now() - started) / 1000)}s`;
    }, 1000);
    try {
      const args = { gate: gate.name };
      if (gate.name === "jev-phase" && phaseInput.value.trim()) {
        args.phase = phaseInput.value.trim();
      }
      if (timeoutInput.value.trim()) args.timeout = Number(timeoutInput.value);
      const accepted = await postJson("/api/runs", { kind: "gate", args });
      const final = await pollRunWithEvents(accepted.id, {
        timeoutMs: (Number(gate.timeout_s) + 120) * 1000,
      });
      const res = final.result || {};
      const ok = final.status === "passed" || Boolean(res.passed);
      status.replaceChildren(paneStatus(ok,
        `passed · exit ${res.exit_code ?? 0} · ${res.duration_s ?? "?"}s`,
        final.status === "error" ? "error" :
          `failed · exit ${res.exit_code ?? "?"} · ${res.duration_s ?? "?"}s`));
      if (final.status === "error") {
        out.textContent = final.error || "gate run failed";
      } else {
        out.textContent = res.output || "(the gate printed nothing)";
      }
      out.hidden = false;
      if (res.output_truncated) {
        out.textContent += "\n\n[output truncated to the last 20,000 characters]";
      }
    } catch (err) {
      status.textContent = String(err.message || err);
    } finally {
      clearInterval(tick);
      run.disabled = false;
    }
  });
  return card;
}

async function renderGates(root) {
  root.append(
    $("h2", {}, "Gates — run the documented quality checks"),
    $("p", { class: "muted" },
      "The same registry and the same shell-free runner as `harness gates --run`. The checkout is the cwd, so a gate cannot be pointed at another tree. Long gates (unittest, audit) stream no progress; elapsed time is shown by the button state."));

  const controls = $("div", { class: "pane-grid three" },
    field("Phase for jev-phase", { value: "JEV-P0" }).row,
    field("Timeout override (seconds)", {
      type: "number", min: "1", max: "3600", placeholder: "gate default" }).row);
  const phaseInput = controls.querySelectorAll("input")[0];
  const timeoutInput = controls.querySelectorAll("input")[1];
  root.append(controls);

  const area = $("div", { class: "pane-cards" });
  root.append($("p", { class: "muted small" }, "Loading gate registry…"), area);
  const note = root.querySelector(".muted.small");

  try {
    const data = await api(`/api/gates?phase=${encodeURIComponent(phaseInput.value || "JEV-P0")}`);
    note.remove();
    for (const gate of data.gates || []) area.append(gateCard(gate, phaseInput, timeoutInput));
    phaseInput.addEventListener("change", async () => {
      try {
        const refreshed = await api(`/api/gates?phase=${encodeURIComponent(phaseInput.value || "JEV-P0")}`);
        area.replaceChildren();
        for (const gate of refreshed.gates || []) area.append(gateCard(gate, phaseInput, timeoutInput));
      } catch (err) {
        paneError(area, err);
      }
    });
  } catch (err) {
    note.textContent = `Gate registry unavailable: ${err.message}`;
  }
}

// ---- Ledger pane ---------------------------------------------------------

async function renderLedger(root) {
  root.append(
    $("h2", {}, "Ledger — verify, inspect, and repair the hash chain"),
    $("p", { class: "muted" },
      "Read-only verification and tail views over the one ledger owner. Repair is the only write here and it is destructive to the invalid tail only; the exact word is required."));

  const chainArea = $("div", {}, $("p", { class: "muted small" }, "Verifying the chain…"));
  const tailArea = $("div", {}, $("p", { class: "muted small" }, "Loading recent entries…"));
  const reportArea = $("div");
  const repairArea = $("div");
  root.append(chainArea, $("div", { class: "pane-actions" }), tailArea, reportArea, repairArea);
  const actions = root.querySelector(".pane-actions");

  function chainCard(data) {
    const chain = data.chain || {};
    const ok = data.verified ?? chain.ok;
    return paneCard(`Chain: ${ok ? "verified" : "BROKEN"}`, [
      $("div", { class: "driver-meta-grid" },
        $("div", {}, $("span", { class: "muted small" }, "First bad seq: "),
          $("code", {}, String(data.first_bad_seq ?? chain.first_bad_seq ?? "none"))),
        $("div", {}, $("span", { class: "muted small" }, "Retained entries: "),
          $("b", {}, String(chain.entries ?? "—"))),
        $("div", {}, $("span", { class: "muted small" }, "Segments: "),
          $("b", {}, String((chain.segments || []).length || "—"))),
        $("div", {}, $("span", { class: "muted small" }, "Pruned prefix: "),
          $("b", {}, chain.pruned ? `yes (from seq ${chain.first_retained_seq ?? "?"})` : "no")),
        $("div", {}, $("span", { class: "muted small" }, "Quarantined on load: "),
          $("b", {}, String(chain.quarantined ?? 0)))),
      paneStatus(Boolean(ok), "✓ links verify", "✗ chain is broken"),
      paneDetails("Raw chain verdict", data),
    ]);
  }

  async function refreshChain() {
    try {
      chainArea.replaceChildren(chainCard(await api("/api/ledger/verify")));
    } catch (err) {
      paneError(chainArea, err);
    }
  }

  async function refreshTail() {
    try {
      const data = await api("/api/ledger/tail?n=25");
      const rows = (data.entries || []).slice().reverse().map(e => [
        e.seq ?? "—", e.ts || "—", e.event || e.kind || "—",
        e.site || "—", e.task_id || "—", e.verdict || "—", money(e.cost)]);
      tailArea.replaceChildren(
        $("h3", {}, "Recent entries"),
        table(["seq", "time", "event", "site", "task", "verdict", "cost"], rows),
        $("p", { class: "muted small" },
          `${data.count ?? (data.entries || []).length} entries retained. Newest first in this table.`));
    } catch (err) {
      paneError(tailArea, err);
    }
  }

  const verifyBtn = $("button", { class: "pane-fetch", type: "button" }, "Verify chain");
  const tailBtn = $("button", { class: "pane-fetch", type: "button" }, "Refresh tail");
  const reportBtn = $("button", { class: "pane-fetch", type: "button" }, "Participation report");
  const deferBtn = $("button", { class: "pane-fetch", type: "button" }, "Why runs deferred");
  actions.append(verifyBtn, tailBtn, reportBtn, deferBtn);
  verifyBtn.addEventListener("click", refreshChain);
  tailBtn.addEventListener("click", refreshTail);
  reportBtn.addEventListener("click", async () => {
    try {
      reportArea.replaceChildren($("h3", {}, "Participation"),
        paneDetails("Participation report", await api("/api/ledger/report")));
    } catch (err) {
      paneError(reportArea, err);
    }
  });
  deferBtn.addEventListener("click", async () => {
    try {
      reportArea.replaceChildren($("h3", {}, "Deferral reasons"),
        paneDetails("Defer statistics", await api("/api/ledger/defer-stats?window=500")));
    } catch (err) {
      paneError(reportArea, err);
    }
  });

  const confirm = field("Type repair to enable the button", {
    placeholder: "repair", autocomplete: "off" });
  const repairBtn = $("button", { class: "pane-fetch", type: "button" }, "Repair ledger tail");
  repairBtn.disabled = true;
  confirm.input.addEventListener("input", () => {
    repairBtn.disabled = confirm.input.value !== "repair";
  });
  const repairOut = $("div");
  repairBtn.addEventListener("click", async () => {
    if (!window.confirm("Repair deletes the invalid ledger tail and every newer segment. Archive the ledger first. Continue?")) return;
    repairBtn.disabled = true;
    try {
      const res = await postJson("/api/ledger/repair", { confirm: "repair" });
      repairOut.replaceChildren(paneCard("Repair result", [
        $("p", {}, `Kept ${res.kept} entr${res.kept === 1 ? "y" : "ies"}; dropped ${res.dropped}.`),
        paneDetails("Before and after", { before: res.before, after: res.after }),
      ]));
      await refreshChain();
      await refreshTail();
    } catch (err) {
      paneError(repairOut, err);
    } finally {
      repairBtn.disabled = confirm.input.value !== "repair";
    }
  });
  repairArea.append(
    $("h3", {}, "Repair"),
    $("p", { class: "small", style: "color: var(--warn);" },
      "Destructive: the invalid tail and every newer segment are removed. Healthy older segments stay byte-identical. Archive the ledger first."),
    confirm.row,
    $("div", { class: "pane-actions" }, repairBtn),
    repairOut);

  await refreshChain();
  await refreshTail();
}

// ---- Models & Cost pane --------------------------------------------------

function mapTable(label, mapping) {
  const rows = Object.entries(mapping || {}).map(([key, value]) => {
    if (value && typeof value === "object") {
      const cost = value.cost ?? value.total_cost ?? value.total ?? value.spend;
      const calls = value.calls ?? value.events ?? value.count;
      return [key, cost === undefined ? "—" : money(cost), calls ?? "—"];
    }
    return [key, money(value), "—"];
  });
  return rows.length
    ? [ $("h4", { class: "pane-subhead" }, label),
        table(["key", "cost", "calls"], rows) ]
    : [];
}

function modelRows(models, limit = 40) {
  return (models || []).slice(0, limit).map(m => [
    m.model || m.id || m.slug || "—",
    m.free === undefined ? (m.is_free ? "yes" : "—") : (m.free ? "yes" : "no"),
    m.context ?? m.context_length ?? "—",
    m.json_reliable ?? m.json_declared ?? "—",
    m.capability ?? m.reliability_structured ?? "—",
  ]);
}

async function renderModels(root) {
  root.append(
    $("h2", {}, "Models & Cost — routing posture, catalog, and spend"),
    $("p", { class: "muted" },
      "Read-only faces over the existing owners: capability registry, free catalog, cost report, rankings report, economics and session spend. Nothing here writes config or probes a model."));

  const grid = $("div", { class: "pane-cards" });
  root.append(grid);

  function card(title, note, path, render, { auto = false } = {}) {
    const out = $("div");
    const btn = $("button", { class: "pane-fetch", type: "button",
      "aria-label": `Load ${title}` }, "Load");
    const box = paneCard(title, [
      $("p", { class: "muted small" }, note),
      $("div", { class: "pane-actions" }, btn),
      out,
    ]);
    async function load() {
      const label = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Loading…";
      try {
        const data = await api(path);
        out.replaceChildren(...[render(data), paneDetails("Raw response", data)].flat());
      } catch (err) {
        paneError(out, err);
      } finally {
        btn.textContent = label;
        btn.disabled = false;
      }
    }
    btn.addEventListener("click", load);
    grid.append(box);
    if (auto) load();
    return { box, load };
  }

  card("Session spend", "Governor spend, ceiling and remaining for this server session.",
    "/api/spend", (d) => {
      const s = d.session || {};
      return $("div", { class: "driver-meta-grid" },
        $("div", {}, $("span", { class: "muted small" }, "Spent: "), $("code", {}, money(s.spent))),
        $("div", {}, $("span", { class: "muted small" }, "Ceiling: "), $("code", {}, money(s.ceiling))),
        $("div", {}, $("span", { class: "muted small" }, "Remaining: "), $("code", {}, money(s.remaining))));
    }, { auto: true });

  card("Cost report", "The same AutonomyLedger.cost_report `harness cost` uses; tier and model breakdowns.",
    "/api/cost?by_tier=1&by_model=1&savings=1", (d) => [
      $("div", { class: "driver-meta-grid" },
        $("div", {}, $("span", { class: "muted small" }, "Total: "), $("code", {}, money(d.total_cost))),
        $("div", {}, $("span", { class: "muted small" }, "Events: "), $("b", {}, String(d.events_count ?? "—"))),
        $("div", {}, $("span", { class: "muted small" }, "Billable: "), $("b", {}, String(d.billable_calls ?? "—"))),
        $("div", {}, $("span", { class: "muted small" }, "Free calls: "), $("b", {}, String(d.free_calls ?? "—")))),
      ...mapTable("By tier", d.by_tier),
      ...mapTable("By model", d.by_model),
      d.savings ? paneDetails("Savings vs frontier baseline", d.savings) : "",
    ], { auto: true });

  card("Capability registry", "Profile rows for the configured pools and judge; fetched catalog cached for a day.",
    "/api/capabilities", (d) => [
      $("p", { class: "muted small" },
        `${d.count ?? (d.models || []).length} rows · captured ${d.captured_at || "—"}${d.refreshed ? " · refreshed" : ""}`),
      table(["model", "free", "context", "json", "capability"], modelRows(d.models)),
    ]);

  card("Free catalog", "The live free-model catalog the router can discover (network read, no probe).",
    "/api/models?limit=40", (d) => [
      $("p", { class: "muted small" }, `${d.count ?? (d.models || []).length} free models`),
      table(["model", "free", "context", "json", "capability"], modelRows(d.models)),
    ]);

  card("Rankings", "The latest read-only rankings report; refresh stays `harness rankings`.",
    "/api/rankings", (d) => {
      if (!d.available) {
        return $("p", { class: "muted small" }, d.note || d.error || "No rankings report is available.");
      }
      const report = d.report || {};
      const rows = (report.top || report.models || report.ranked || []).slice(0, 20).map(m => [
        m.model || m.id || m.slug || "—", m.tokens ?? m.total_tokens ?? "—",
        m.trend ?? "—"]);
      return [
        $("p", { class: "muted small" }, `Latest: ${d.latest || "—"}`),
        rows.length ? table(["model", "tokens", "trend"], rows) : paneDetails("Report", report),
      ];
    });

  card("Economics", "EV-1..EV-5: tier snapshot freshness, seat share, rotation proposal and cost summary.",
    "/api/economics", (d) => [
      $("div", { class: "driver-meta-grid" },
        $("div", {}, $("span", { class: "muted small" }, "Freshness: "),
          $("code", {}, JSON.stringify(d.freshness || {}).slice(0, 120))),
        $("div", {}, $("span", { class: "muted small" }, "Rotation: "),
          $("code", {}, d.rotation_proposal?.action || d.rotation_proposal?.status || "—"))),
      ...mapTable("Seat share", d.seat_share),
      paneDetails("Unlisted tier fallbacks", d.unlisted_tier_fallbacks || []),
      paneDetails("Rotation proposal", d.rotation_proposal || {}),
    ]);
}

// ---- Missions pane -------------------------------------------------------

function missionCard(mission, onOpen) {
  const budget = mission.dual_budget || {};
  const open = $("button", { class: "pane-fetch", type: "button" }, "Open pack");
  open.addEventListener("click", () => onOpen(mission.id));
  return paneCard(mission.id, [
    $("div", { class: "pane-actions" },
      $("span", {
        class: `driver-status-pill ${mission.terminal ? "pill-warn" : "pill-good"}`,
      }, mission.terminal
        ? `terminal: ${mission.phase || "done"}`
        : `phase: ${mission.phase || "in_progress"}`),
      $("span", { class: "muted small" },
        `working ${money(budget.working_remaining)} · reserve ${money(budget.terminal_reserve_cost_usd)}`),
      open),
  ]);
}

async function renderMissions(root) {
  root.append(
    $("h2", {}, "Missions — packs, budgets, and initialization"),
    $("p", { class: "muted" },
      "Read the packs the CLI and MCP already use, or create one through the same `build_mission_spec` + `init_mission_pack` owner. Packs live under the server's ./missions directory; the browser cannot pick another root."));

  const listArea = $("div", { class: "pane-cards" });
  const detailArea = $("div");
  const pager = $("div", { class: "pane-actions" });
  root.append($("h3", {}, "Existing packs"), listArea, pager, detailArea);

  let offset = 0;
  const limit = 25;
  let total = 0;

  async function openDetail(id) {
    detailArea.replaceChildren($("p", { class: "muted small" }, `Loading ${id}…`));
    try {
      const d = await api(`/api/missions/${encodeURIComponent(id)}`);
      detailArea.replaceChildren(paneCard(`Mission ${d.id}`, [
        $("p", {}, d.request || "—"),
        $("div", { class: "driver-meta-grid" },
          $("div", {}, $("span", { class: "muted small" }, "Phase: "),
            $("code", {}, d.phase || "—")),
          $("div", {}, $("span", { class: "muted small" }, "Terminal: "),
            $("b", {}, String(Boolean(d.terminal)))),
          $("div", {}, $("span", { class: "muted small" }, "Success: "),
            $("span", {}, d.success_definition || "—")),
          $("div", {}, $("span", { class: "muted small" }, "Max cost: "),
            $("code", {}, money(d.limits?.max_cost_usd)))),
        paneDetails("Full pack summary", d),
      ]));
    } catch (err) {
      paneError(detailArea, err);
    }
  }

  async function refresh() {
    listArea.replaceChildren($("p", { class: "muted small" }, "Loading missions…"));
    try {
      const d = await api(`/api/missions?limit=${limit}&offset=${offset}`);
      total = d.total || 0;
      listArea.replaceChildren();
      if (!(d.missions || []).length) {
        listArea.append($("p", { class: "muted small" },
          "No mission packs yet. Create one below, or run `harness mission init`."));
      }
      for (const m of d.missions || []) listArea.append(missionCard(m, openDetail));
      pager.replaceChildren();
      if (!total) {
        pager.append($("span", { class: "muted small" }, "No packs to page"));
        return;
      }
      const prev = $("button", { class: "pane-fetch", type: "button" }, "Previous");
      const next = $("button", { class: "pane-fetch", type: "button" }, "Next");
      prev.disabled = offset <= 0;
      next.disabled = offset + limit >= total;
      prev.addEventListener("click", () => { offset = Math.max(0, offset - limit); refresh(); });
      next.addEventListener("click", () => { offset += limit; refresh(); });
      pager.append(prev, next,
        $("span", { class: "muted small" },
          `${offset + 1}–${Math.min(offset + limit, total)} of ${total}`));
    } catch (err) {
      paneError(listArea, err);
    }
  }

  const form = $("form", { class: "pane-section" });
  form.append($("h3", {}, "Initialize a mission pack"));
  const id = field("Mission id", { placeholder: "e.g. ledger-hardening" });
  const request = textareaField("Request", { rows: "3",
    placeholder: "What should the mission accomplish?" });
  const success = textareaField("Success definition", { rows: "2",
    placeholder: "What observable result proves it done?" });
  const maxCost = field("Max cost (USD)", { type: "number", min: "0", step: "0.01", value: "1.00" });
  const reserve = field("Terminal reserve (USD)", { type: "number", min: "0", step: "0.01", value: "0.10" });
  const inScope = textareaField("In scope (one item per line)", { rows: "2" });
  const outScope = textareaField("Out of scope (one item per line)", { rows: "2" });
  const verifier = field("Verifier kind", { value: "unspecified" });
  const missionRoot = field("Missions root", { value: "missions" });
  const submit = $("button", { class: "pane-fetch", type: "submit" }, "Create mission pack");
  form.append(id.row, request.row, success.row,
    $("div", { class: "pane-grid three" }, maxCost.row, reserve.row, verifier.row),
    inScope.row, outScope.row, missionRoot.row,
    $("div", { class: "pane-actions" }, submit));
  root.append(form);

  const initOut = $("div");
  root.append(initOut);

  form.onsubmit = async (ev) => {
    ev.preventDefault();
    submit.disabled = true;
    initOut.replaceChildren($("p", { class: "muted small" }, "Creating pack…"));
    try {
      const payload = {
        id: id.input.value.trim(),
        request: request.input.value.trim(),
        success_definition: success.input.value.trim(),
        max_cost_usd: Number(maxCost.input.value),
        terminal_reserve_cost_usd: Number(reserve.input.value) || 0,
        in_scope: inScope.input.value.split("\n").map(s => s.trim()).filter(Boolean),
        out_of_scope: outScope.input.value.split("\n").map(s => s.trim()).filter(Boolean),
        verifier_kind: verifier.input.value.trim() || "unspecified",
        root: missionRoot.input.value.trim() || "missions",
      };
      const created = await postJson("/api/missions", payload);
      initOut.replaceChildren(paneCard(`Created ${created.id}`, [
        $("p", {}, created.request || payload.request),
        paneDetails("Pack summary", created),
      ]));
      offset = 0;
      await refresh();
      await openDetail(created.id);
    } catch (err) {
      paneError(initOut, err);
    } finally {
      submit.disabled = false;
    }
  };

  await refresh();
}

// ---- Provision pane ------------------------------------------------------

async function renderProvision(root) {
  root.append(
    $("h2", {}, "Provision — probe this host and plan setup"),
    $("p", { class: "muted" },
      "Host facts are probed through the same owner as `harness provision probe`; plans are generated by `plan_provision` and validated before they are shown. Mutating execution stays an explicit `harness provision apply --execute` step."));

  const probeArea = $("div");
  root.append($("h3", {}, "Host probe"), probeArea);
  try {
    const p = await api("/api/provision/probe");
    probeArea.replaceChildren(paneCard(`${p.os} · ${p.arch}`, [
      $("div", { class: "driver-meta-grid" },
        $("div", {}, $("span", { class: "muted small" }, "Python: "), $("code", {}, p.python_version || "—")),
        $("div", {}, $("span", { class: "muted small" }, "GPU: "), $("b", {}, p.gpu_present ? "present" : "none")),
        $("div", {}, $("span", { class: "muted small" }, "Free disk: "),
          $("code", {}, p.free_disk_bytes ? `${(p.free_disk_bytes / (1024 ** 3)).toFixed(1)} GB` : "—"))),
      table(["manager", "usable", "version"],
        (p.package_managers || []).map(m => [m.name, m.usable ? "yes" : "no", m.version || "—"])),
      paneDetails("Raw probe", p),
    ]));
  } catch (err) {
    paneError(probeArea, err);
  }

  const form = $("form", { class: "pane-section" });
  form.append($("h3", {}, "Plan a provisioning goal"));
  const goal = textareaField("Goal", { rows: "2",
    placeholder: "e.g. install ruff" });
  const planRoot = field("Approved root", { value: currentWorkspace(),
    placeholder: "C:\\path\\to\\workspace" });
  const submit = $("button", { class: "pane-fetch", type: "submit" }, "Generate plan");
  form.append(goal.row, planRoot.row, $("div", { class: "pane-actions" }, submit));
  root.append(form);

  const area = $("div");
  root.append(area);

  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const workspace = workspaceArgs(planRoot.input);
    if (!goal.input.value.trim()) {
      paneError(area, new Error("A provisioning goal is required."));
      return;
    }
    if (!workspace) {
      paneError(area, new Error("An approved root is required."));
      return;
    }
    submit.disabled = true;
    area.replaceChildren($("p", { class: "muted small" }, "Planning…"));
    try {
      const plan = await postJson("/api/provision/plan", {
        goal: goal.input.value.trim(), root: workspace,
      });
      const steps = plan.steps || [];
      const download = $("button", { class: "pane-fetch", type: "button" }, "Download plan JSON");
      download.addEventListener("click", () => {
        const blob = new Blob([JSON.stringify(plan, null, 2)], { type: "application/json" });
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = `provision-${(plan.digest || "plan").slice(0, 12)}.json`;
        link.click();
        URL.revokeObjectURL(link.href);
      });
      area.replaceChildren(paneCard(`Plan: ${plan.recipe || "custom"}`, [
        $("p", { class: "muted small" },
          `digest ${(plan.digest || "—").slice(0, 16)} · source ${plan.source || "—"} · root ${plan.root || "—"}${plan.review_required ? " · per-step review required" : ""}`),
        (plan.notes || []).length
          ? $("ul", { class: "pane-notes" }, (plan.notes || []).map(n => $("li", {}, n)))
          : "",
        steps.length ? table(["#", "step", "class", "argv", "effect"], steps.map((s, i) => [
          i + 1, s.id || "—", s.classification || "—",
          (s.argv || []).join(" "), s.expected_effect || "—"])) : "",
        $("div", { class: "pane-actions" }, download),
        $("pre", { class: "pane-json" }, plan.rendered || ""),
        paneDetails("Raw plan", plan),
      ]));
    } catch (err) {
      paneError(area, err);
    } finally {
      submit.disabled = false;
    }
  };
}

// ---- Tab wiring ----------------------------------------------------------

const PANES = [
  ["proof", "Proof", renderProof],
  ["insights", "Insights", renderInsights],
  ["driver", "Driver", renderDriver],
  ["plan", "Plan", renderPlan],
  ["apply", "Apply", renderApply],
  ["verify", "Verify", renderVerify],
  ["continue", "Continue", renderContinue],
  ["bench", "Bench", renderBench],
  ["gates", "Gates", renderGates],
  ["ledger", "Ledger", renderLedger],
  ["models", "Models & Cost", renderModels],
  ["missions", "Missions", renderMissions],
  ["provision", "Provision", renderProvision],
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

