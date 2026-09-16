/* harness UI: no framework, no build step. Talks only to /api/*. */
"use strict";

// ---- auth token (desktop shell passes it via the URL fragment) ----------
const TOKEN = location.hash ? decodeURIComponent(location.hash.slice(1)) : null;

async function api(path, opts = {}) {
  const headers = Object.assign({"Content-Type": "application/json"}, opts.headers || {});
  if (TOKEN) headers["X-Harness-Auth"] = TOKEN;
  const res = await fetch(path, Object.assign({}, opts, {headers}));
  let body = null;
  try { body = await res.json(); } catch (_e) { /* non-JSON error page */ }
  if (!res.ok) {
    const msg = (body && body.error) ? body.error : `${res.status} ${res.statusText}`;
    throw new Error(msg);
  }
  return body;
}

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const fmtCost = (v) => (typeof v === "number") ? `$${v.toFixed(6)}` : String(v ?? "–");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const statusClass = (s) => "s-" + String(s || "").replace(/[^a-z_]/g, "");

// ---- view routing --------------------------------------------------------
$$("nav a").forEach((a) => a.addEventListener("click", () => {
  $$("nav a").forEach((x) => x.classList.remove("active"));
  a.classList.add("active");
  $$(".view").forEach((v) => v.classList.remove("active"));
  $(`#view-${a.dataset.view}`).classList.add("active");
  const refresh = VIEW_REFRESH[a.dataset.view];
  if (refresh) refresh();
}));

// ---- live events ---------------------------------------------------------
let lastSeq = 0;
const EVENT_DETAIL = {
  preflight: (e) => `worst-case ${fmtCost(e.worst_case)} vs ceiling ${fmtCost(e.ceiling)}`,
  attempt_start: (e) => `${e.model} round ${e.round}`,
  panel_call: (e) => `${e.model}`,
  panel_vote: (e) => `${e.model} ${fmtCost(e.cost)} ${e.truncated ? "(truncated)" : ""}`,
  judge_call: (e) => `${e.model}`,
  judge_result: (e) => `${e.model}: ${e.status}`,
  rotation: (e) => `${e.model || ""} ${e.reason}${e.error ? " — " + String(e.error).slice(0, 80) : ""}`,
  readiness: (e) => `${e.model}: ${e.decision}`,
  consent_result: (e) => `${e.decision}${e.fail_closed ? " (fail-closed)" : ""}`,
  gate_start: (e) => `${e.command || ""}`,
  gate_end: (e) => e.passed ? "PASS" : `FAIL rc=${e.rc}`,
  escalation_rung: (e) => `rung ${e.rung ?? ""} ${e.model || ""}`,
  spend_check: (e) => e.lane === "key"
    ? `key limit ${fmtCost(e.limit)} remaining ${fmtCost(e.remaining)}`
    : `spent ${fmtCost(e.spent)} / ${fmtCost(e.ceiling)}`,
  bench_task: (e) => `${e.name}: ${e.phase}${e.status ? " -> " + e.status : ""}`,
  terminal: (e) => `${e.status} cost ${fmtCost(e.cost)}`,
  run_accepted: (e) => `${e.kind} (${e.ui_run})`,
  run_finished: (e) => `${e.kind}: ${e.status}`,
  run_cancel_requested: (e) => `${e.ui_run}`,
  pool_filtered: (e) => `${e.lane || ""}: ${e.reason} — ${(e.models || []).join(", ")}`,
  rankings_probe: (e) => e.phase === "start"
    ? `probing ${e.model}`
    : `${e.model}: ${e.ok ? "PASS" : "FAIL"} ${fmtCost(e.cost)}`,
};

function renderEvent(e) {
  const div = document.createElement("div");
  div.className = "ev";
  const time = new Date((e.ts || 0) * 1000).toLocaleTimeString();
  const detailFn = EVENT_DETAIL[e.type];
  const detail = detailFn ? detailFn(e) : JSON.stringify(Object.fromEntries(Object.entries(e).filter(([k]) => !["ts", "seq", "type", "task_id"].includes(k)))).slice(0, 120);
  div.innerHTML = `<span class="t">${esc(time)}</span>` +
    `<span class="type type-${esc(e.type)} ${esc(e.type)}">${esc(e.type)}</span>` +
    `<span class="detail">${esc(detail)}</span>`;
  div.dataset.seq = e.seq;
  return div;
}

async function pollEvents() {
  try {
    const data = await api(`/api/events?after=${lastSeq}`);
    if (data.events && data.events.length) {
      const log = $("#live-events");
      for (const e of data.events) {
        lastSeq = Math.max(lastSeq, e.seq);
        log.prepend(renderEvent(e));
      }
      while (log.children.length > 300) log.removeChild(log.lastChild);
    }
  } catch (_e) { /* server restarting; next tick retries */ }
}

// ---- dashboard -----------------------------------------------------------
async function refreshDashboard() {
  try {
    const [spend, chain, runs] = await Promise.all([
      api("/api/spend"), api("/api/ledger/verify"), api("/api/runs"),
    ]);
    const s = spend.session || {};
    $("#d-spent").textContent = fmtCost(s.spent);
    $("#d-ceiling").textContent = `ceiling ${fmtCost(s.ceiling)}`;
    const frac = s.ceiling ? Math.min(1, (s.spent || 0) / s.ceiling) : 0;
    const bar = $("#d-spend-bar");
    bar.style.width = `${(frac * 100).toFixed(1)}%`;
    bar.style.background = frac > 0.85 ? "var(--red)" : frac > 0.5 ? "var(--yellow)" : "var(--green)";
    $("#d-key-limit").textContent = fmtCost(spend.limit);
    $("#d-key-remaining").textContent = `remaining ${fmtCost(spend.remaining)} (resets ${spend.limit_reset || "–"})`;
    $("#d-chain").textContent = chain.verified ? "OK" : "BROKEN";
    $("#d-chain").style.color = chain.verified ? "var(--green)" : "var(--red)";
    const c = chain.chain || {};
    const seg = Array.isArray(c.segments) ? c.segments.length : c.segments;
    $("#d-chain-detail").textContent = `${seg ?? "?"} segment(s), ${c.entries ?? "?"} entries`;
    const r = runs.runs || [];
    const active = r.filter((x) => x.status === "running").length;
    $("#d-runs").textContent = r.length;
    $("#d-runs-detail").textContent = active ? `${active} running` : "all settled";
  } catch (e) {
    $("#d-ceiling").textContent = `(${e.message})`;
  }
}

// ---- dispatch ------------------------------------------------------------
let dispatchKind = "apply";
$$(".tab").forEach((b) => b.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  dispatchKind = b.dataset.kind;
  syncKindFields();
}));

function syncKindFields() {
  const isApplyLike = dispatchKind === "apply" || dispatchKind === "continue";
  $("#f-file-label").hidden = dispatchKind !== "apply";
  $("#f-state-label").hidden = dispatchKind !== "continue";
  $("#f-manifest-label").hidden = dispatchKind !== "bench";
  $("#f-verify-label").hidden = !isApplyLike;
  $("#f-instruction-label").hidden = dispatchKind === "verify";
  $("#f-prompt-label").hidden = dispatchKind !== "verify";
  $("#f-promptfile-label").hidden = dispatchKind !== "verify";
  $("#f-claims-label").hidden = dispatchKind !== "verify";
  $("#f-claims-row").hidden = dispatchKind !== "verify";
  $("#f-source-row").hidden = dispatchKind !== "verify";
  $("#f-defs-row").hidden = dispatchKind !== "verify";
  $("#f-ctx-row").hidden = dispatchKind !== "verify";
  $("#f-meta-row").hidden = !isApplyLike;
  $("#confirm-box").hidden = true;
  $("#btn-dispatch").disabled = true;
  $("#dispatch-result").hidden = true;
}

let pendingArgs = null;
$("#btn-review").addEventListener("click", () => {
  const fd = new FormData($("#dispatch-form"));
  const args = {};
  for (const [k, v] of fd.entries()) {
    if (typeof v === "string" && v.trim() !== "") args[k] = v.trim();
  }
  for (const k of ["verify_only", "require_consent"]) args[k] = !!fd.get(k);
  // Client-side required-field gate: the server refuses these too, but
  // reviewing an empty form ("file: ?") invites dispatching nothing.
  const required = {
    apply: "file", verify: "prompt", continue: "state", bench: "manifest",
  }[dispatchKind];
  const hasRequired = required === "prompt"
    ? !!(args.prompt || args.prompt_file || args.claims_file) : !!args[required];
  if (!hasRequired) {
    const out = $("#dispatch-result");
    out.hidden = false;
    out.textContent = `nothing to review: '${required}' is required`;
    return;
  }
  // Claims mode additionally requires the source window it is grounded in.
  if (dispatchKind === "verify" && args.claims_file && !args.source_file) {
    const out = $("#dispatch-result");
    out.hidden = false;
    out.textContent = "nothing to review: 'source_file' is required with claims_file";
    return;
  }
  const summary = {
    apply: () => `file: ${args.file}\ninstruction: ${args.instruction || "(none)"}` +
      `\nverify: ${args.verify || "(none — ungated)"}\nbackend: ${args.backend || "harness"}` +
      `${args.verify_only ? "\nverify-only preview: NO file write, NO gate" : ""}` +
      `${args.require_consent ? "\nconsent: required" : ""}`,
    verify: () => (args.claims_file
      ? `claims: ${args.claims_file}\nsource: ${args.source_file}` +
        `\ndefinitions: ${args.definitions_file || "(none)"}`
      : `prompt: ${(args.prompt || args.prompt_file).slice(0, 200)}`),
    continue: () => `state: ${args.state}\ninstruction: ${args.instruction || "(from state)"}`,
    bench: () => `manifest: ${args.manifest}`,
  }[dispatchKind]();
  $("#confirm-summary").textContent = summary;
  $("#confirm-box").hidden = false;
  pendingArgs = args;
  $("#btn-dispatch").disabled = false;
});

$("#dispatch-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (!pendingArgs) return;
  // Consume the pending args synchronously: a second rapid click must not
  // POST the same dispatch twice (two real runs for one confirmation).
  const args = pendingArgs;
  pendingArgs = null;
  try {
    const run = await api("/api/runs", {
      method: "POST", body: JSON.stringify({kind: dispatchKind, args}),
    });
    $("#confirm-box").hidden = true;
    $("#btn-dispatch").disabled = true;
    const out = $("#dispatch-result");
    out.hidden = false;
    out.textContent = `dispatched run ${run.id} (task ${run.task_id}) — see Runs`;
    lastSeq = 0;
    await refreshRuns();
  } catch (e) {
    const out = $("#dispatch-result");
    out.hidden = false;
    out.textContent = `dispatch refused: ${e.message}`;
  }
});

// ---- runs ----------------------------------------------------------------
// Human verdict summary for a settled run: the fields a reader needs first,
// with the full JSON envelope behind a toggle (raw dump stays reachable).
function resultSummary(r) {
  const votes = Array.isArray(r.panel_results) ? r.panel_results.length : null;
  const failures = Array.isArray(r.panel_failures) ? r.panel_failures.length : 0;
  const verdict = r.verdict && typeof r.verdict === "object"
    ? r.verdict : {decision: r.verdict};
  const status = r.judge_synthesis_status || r.status;
  const cost = (typeof r.actual_cost === "number") ? fmtCost(r.actual_cost)
    : "–";
  const ceiling = r.max_cost_ceiling != null ? ` of ${fmtCost(r.max_cost_ceiling)} ceiling` : "";
  const judge = (r.meta && r.meta.judge) || r.judge_model || "";
  const row = (k, v, cls) =>
    `<div class="sum-row"><span class="k">${esc(k)}</span>` +
    `<span class="${cls || ""}">${v}</span></div>`;
  let html =
    row("verdict", esc(verdict.decision ?? "–"),
      String(verdict.decision) === "yes" ? "sum-yes" : String(verdict.decision) === "no" ? "sum-no" : "") +
    row("panel", votes == null ? "–" : `${votes} vote(s), ${failures} failure(s)`) +
    row("judge", `${esc(judge)} · ${esc(status)}`) +
    row("cost", `${esc(cost)}${ceiling}`) +
    (verdict.confidence ? row("confidence", esc(verdict.confidence)) : "") +
    (r.lint ? row("lint", r.lint.ok ? "ok" : `rejected (${esc(
      (r.lint.issues || []).map((i) => i.claim_id || i.code || "?").join(", "))})`) : "");
  const synth = (r.judge_synthesis || "").trim();
  if (synth) html += `<div class="sum-row"><span class="k">synthesis</span><span>${esc(synth.slice(0, 400))}${synth.length > 400 ? "…" : ""}</span></div>`;
  // Change preview for apply/continue results: what changed in the touched
  // file, computed server-side from content the run already held (see
  // results._content_diff). Read-only evidence, like every other row here.
  if (r.diff) {
    const body = r.diff.split("\n").slice(0, 400).map((line) => {
      const cls = line.startsWith("+") ? "ln-add"
        : line.startsWith("-") ? "ln-del"
        : (line.startsWith("@@") ? "ln-meta" : "ln-ctx");
      return `<div class="${cls}">${esc(line) || "&nbsp;"}</div>`;
    }).join("");
    const note = r.diff.split("\n").length > 400
      ? `<div class="dim">… diff truncated at 400 lines (full diff in the raw JSON)</div>` : "";
    html += `<div class="sum-row"><span class="k">changes</span></div>` +
      (r.file ? `<div class="d-file dim mono">${esc(r.file)}</div>` : "") +
      `<div class="diff-view mono">${body}</div>${note}`;
  } else if (r.status === "ok" && r.changed === false) {
    html += row("changes", "none (model proposal matched the current content)");
  }
  const reasons = Array.isArray(verdict.reasons) ? verdict.reasons
    : Array.isArray(r.reasons) ? r.reasons : [];
  if (reasons.length) html += row("reasons", reasons.map((x) => esc(x)).join("; "));
  html += `<details class="sum-json"><summary>raw JSON</summary><pre class="mono dim">${
    esc(JSON.stringify(r, null, 2).slice(0, 6000))}</pre></details>`;
  return html;
}

async function refreshRuns() {
  try {
    const data = await api("/api/runs");
    const list = $("#runs-list");
    list.innerHTML = "";
    for (const run of (data.runs || [])) {
      const panel = document.createElement("div");
      panel.className = "panel";
      const head = `<div class="row" style="justify-content:space-between">
        <div><strong>${esc(run.kind)}</strong>
        <span class="${statusClass(run.status)}">${esc(run.status)}</span>
        <span class="dim mono">${esc(run.id)} · task ${esc(run.task_id)}</span></div>
        <div>${run.status === "running" && !run.cancelled
          ? `<button data-cancel="${esc(run.id)}">Cancel</button>` : ""}
        ${run.status !== "running" ? `<button data-result="${esc(run.id)}">Result</button>` : ""}</div>
      </div>`;
      panel.innerHTML = head + `<div class="run-detail mono dim"></div>`;
      panel.querySelectorAll("[data-cancel]").forEach((b) =>
        b.addEventListener("click", async () => {
          await api(`/api/runs/${b.dataset.cancel}/cancel`, {method: "POST", body: "{}"});
          refreshRuns();
        }));
      panel.querySelectorAll("[data-result]").forEach((b) =>
        b.addEventListener("click", async () => {
          const full = await api(`/api/runs/${b.dataset.result}/result`);
          const det = panel.querySelector(".run-detail");
          det.innerHTML = (full.result != null) ? resultSummary(full.result) : "";
          if (full.result == null && full.error) det.textContent = full.error;
        }));
      list.appendChild(panel);
    }
    if (!(data.runs || []).length) {
      list.innerHTML = `<div class="dim">No runs yet — dispatch one from the Dispatch tab.</div>`;
    }
  } catch (e) {
    $("#runs-list").innerHTML = `<div class="dim">${esc(e.message)}</div>`;
  }
}

// ---- ledger --------------------------------------------------------------
async function refreshLedger() {
  try {
    const [tail, report, deferStats] = await Promise.all([
      api("/api/ledger/tail?n=30"), api("/api/ledger/report"),
      api("/api/ledger/defer-stats"),
    ]);
    const rows = (tail.entries || []).map((e) =>
      `<tr><td>${esc(e.seq)}</td><td>${esc(e.event)}</td>` +
      `<td>${esc(e.model || e.caller || "")}</td><td class="dim">${esc(JSON.stringify(
        Object.fromEntries(Object.entries(e).filter(
          ([k]) => !["seq", "ts", "event", "model", "caller", "hash", "prev_hash", "signature"].includes(k)
        ))).slice(0, 120))}</td></tr>`).join("");
    $("#ledger-tail").innerHTML =
      `<table><tr><th>seq</th><th>event</th><th>model</th><th>fields</th></tr>${rows}</table>`;
    const models = report.per_model || {};
    const cal = report.calibration || {};
    const mrows = Object.entries(models).map(([m, v]) => {
      const c = cal[m] || {};
      return `<tr><td>${esc(m)}</td><td>${esc(String(v.accepts ?? 0))}</td>` +
        `<td>${esc(String(v.declines ?? 0))}</td><td>${esc(String(v.completions ?? 0))}</td>` +
        `<td>${esc(String(v.unusable_outputs ?? 0))}</td>` +
        `<td>${esc(fmtNum(c.confidence_precision))}</td></tr>`;
    }).join("");
    $("#ledger-report").innerHTML = mrows
      ? `<table><tr><th>model</th><th>accepts</th><th>declines</th><th>completions</th><th>unusable</th><th>precision</th></tr>${mrows}</table>`
      : `<div class="dim">No participation history yet.</div>`;
    const ds = deferStats.defer_stats || {};
    const rate = (typeof ds.panel_defer_rate === "number")
      ? `${(ds.panel_defer_rate * 100).toFixed(1)}%` : "–";
    const mid = Object.entries(ds.defer_midtask_by_category || {})
      .map(([k, v]) => `${esc(k)}: ${v}`).join(", ") || "none";
    $("#ledger-defer").innerHTML =
      `<table><tr><th>panel runs</th><th>deferred</th><th>defer rate</th>` +
      `<th>mid-task</th><th>consent-blocked</th><th>total</th></tr>` +
      `<tr><td>${esc(ds.panel_runs ?? 0)}</td><td>${esc(ds.panel_deferred ?? 0)}</td>` +
      `<td>${rate}</td><td class="dim">${mid}</td>` +
      `<td>${esc(ds.consent_blocked_total ?? 0)}</td>` +
      `<td><b>${esc(ds.defer_total ?? 0)}</b></td></tr></table>`;
  } catch (e) {
    $("#ledger-report").innerHTML = `<div class="dim">${esc(e.message)}</div>`;
  }
}
function fmtNum(v) { return (typeof v === "number") ? v.toFixed(2) : "–"; }

$("#btn-ledger-verify").addEventListener("click", async () => {
  try {
    const r = await api("/api/ledger/verify");
    $("#ledger-verify-out").textContent = r.verified
      ? "chain OK" : `BROKEN at seq ${r.first_bad_seq}`;
  } catch (e) {
    $("#ledger-verify-out").textContent = e.message;
  }
});

// ---- trust ----------------------------------------------------------------
$("#btn-trust-refresh").addEventListener("click", refreshTrust);
async function refreshTrust() {
  try {
    const caller = $("#trust-caller").value;
    const r = await api("/api/trust" + (caller ? `?caller=${encodeURIComponent(caller)}` : ""));
    const h = r.host || {};
    const scale = r.scale || {};
    $("#trust-host").textContent =
      `host trust ${h.score ?? "–"} (${(h.reasons || []).join("; ") || "no reasons"}) · ` +
      `scale ${scale.min}..${scale.max}, refuse ≤ ${scale.refuse_at_or_below}`;
    const table = r.per_caller || {};
    const rows = Object.entries(table).map(([caller, v]) =>
      `<tr><td>${esc(caller)}</td><td>${esc(String(v.score ?? "–"))}</td>` +
      `<td>${esc(String(v.completions ?? 0))}</td>` +
      `<td>${esc(String(v.trust_gates ?? 0))}</td></tr>`).join("");
    $("#trust-out").innerHTML = rows
      ? `<table><tr><th>caller</th><th>trust</th><th>completions</th><th>strikes</th></tr>${rows}</table>`
      : `<div class="dim">No caller history yet.</div>`;
    // Keep the filter populated with every caller the ledger has seen.
    for (const c of Object.keys(table)) {
      if (![...$("#trust-caller").options].some((o) => o.value === c)) {
        const opt = document.createElement("option");
        opt.value = c; opt.textContent = c;
        $("#trust-caller").appendChild(opt);
      }
    }
  } catch (e) {
    $("#trust-out").innerHTML = `<div class="dim">${esc(e.message)}</div>`;
    $("#trust-host").textContent = "";
  }
}

// ---- models / capabilities / settings ------------------------------------
$("#trust-caller").addEventListener("change", refreshTrust);
async function loadModels() {
  try {
    const r = await api("/api/models?limit=60");
    $("#models-out").innerHTML = `<table><tr><th>#</th><th>free model id</th></tr>` +
      r.models.map((m, i) => `<tr><td>${i + 1}</td><td>${esc(m)}</td></tr>`).join("") +
      `</table>`;
  } catch (e) {
    $("#models-out").innerHTML = `<div class="dim">${esc(e.message)}</div>`;
  }
}
$("#btn-models-refresh").addEventListener("click", loadModels);

async function loadCapabilities() {
  try {
    const r = await api("/api/capabilities");
    const rows = (r.models || []).map((m) =>
      `<tr><td>${esc(m.model)}</td><td>${m.context ? m.context.toLocaleString() : "–"}</td>` +
      `<td>${m.reasoning ? "Y" : "n"}</td><td>${m.capability.toFixed(2)}</td>` +
      `<td>${m.json_reliable.toFixed(2)}</td><td>${m.reliability_structured.toFixed(2)}</td></tr>`).join("");
    $("#capabilities-out").innerHTML =
      `<table><tr><th>model</th><th>ctx</th><th>rsn</th><th>cap</th><th>json</th><th>rel</th></tr>${rows}</table>`;
  } catch (e) {
    $("#capabilities-out").innerHTML = `<div class="dim">${esc(e.message)}</div>`;
  }
}
$("#btn-capabilities").addEventListener("click", loadCapabilities);

// ---- rankings (read-only mirror) ------------------------------------------
async function loadRankings() {
  try {
    const r = await api("/api/rankings");
    const out = $("#rankings-out");
    if (!r.available) {
      out.innerHTML = `<div class="dim">${esc(r.error || r.note)}</div>`;
      return;
    }
    const rep = r.report;
    const w = rep.window || {};
    const head = `<div class="dim" style="margin:6px 0">report ${esc(r.latest)}` +
      ` · window ${esc(w.start || "?")} → ${esc(w.end || "?")} (${esc(String(w.days ?? "?"))} days)` +
      `${(r.reports || []).length > 1 ? ` · ${r.reports.length} report(s) on disk` : ""}</div>`;
    const rows = (rep.top || []).map((t) =>
      `<tr><td>${esc(t.slug)}</td><td class="mono">${esc(String(t.total_tokens ?? ""))}</td>` +
      `<td>${esc(t.trend || "")}</td></tr>`).join("");
    let html = head +
      `<h2>Top by traffic</h2>` +
      (rows ? `<table><tr><th>model</th><th>total tokens</th><th>trend</th></tr>${rows}</table>`
            : `<div class="dim">No ranked models in this report.</div>`);
    const ranked = rep.ranked_in_catalog || [];
    if (ranked.length) {
      html += `<h2>Ranked ∩ live catalog</h2><table><tr><th>slug</th><th>catalog id</th><th>tokens</th><th>trend</th></tr>` +
        ranked.map((c) => `<tr><td>${esc(c.slug)}</td><td>${esc(c.model_id)}</td>` +
          `<td class="mono">${esc(String(c.total_tokens ?? ""))}</td><td>${esc(c.trend || "")}</td></tr>`).join("") +
        `</table>`;
    }
    const proposed = rep.proposed_candidates || [];
    if (proposed.length) {
      html += `<h2>Proposed candidates (advisory)</h2><table><tr><th>catalog id</th><th>tokens</th><th>probe</th></tr>` +
        proposed.map((c) => {
          const p = c.probe;
          const verdict = p ? (p.ok ? `pass (${esc(p.detail)})` : `fail — ${esc(p.detail)}`) : "not probed";
          return `<tr><td>${esc(c.model_id)}</td><td class="mono">${esc(String(c.total_tokens ?? ""))}</td>` +
            `<td>${verdict}</td></tr>`;
        }).join("") + `</table>`;
    }
    out.innerHTML = html;
  } catch (e) {
    $("#rankings-out").innerHTML = `<div class="dim">${esc(e.message)}</div>`;
  }
}

api("/api/settings").then((r) => {
  $("#settings-out").textContent = JSON.stringify(r.settings, null, 2);
}).catch((e) => { $("#settings-out").textContent = e.message; });
api("/api/status").then((r) => {
  $("#status-out").textContent =
    `server up since ${new Date(r.started_at * 1000).toLocaleString()} · ` +
    `${r.runs.length} run(s) · ${r.events_buffered} event(s) buffered · ` +
    (r.auth_required ? "token required" : "no token set");
}).catch((e) => { $("#status-out").textContent = e.message; });

// ---- boot ----------------------------------------------------------------
// Every view refreshes when entered (cheap operator fix: an operator watching
// a run settle must not manually re-poll Ledger/Models for the new state).
const VIEW_REFRESH = {
  dashboard: refreshDashboard, runs: refreshRuns, ledger: refreshLedger,
  trust: refreshTrust, capabilities: loadCapabilities, models: loadModels,
  rankings: loadRankings,
};

const FOOTER_NOTE = TOKEN
  ? "token-protected session (desktop shell)" : "local session — add --auth-token to require a token";
$("#footer-note").textContent = FOOTER_NOTE;

pollEvents();
setInterval(pollEvents, 1500);
refreshDashboard();
setInterval(refreshDashboard, 5000);
// Runs must re-render while any run is unsettled, or a finished run shows
// "running" forever (playtest: the Result button never appeared).
setInterval(() => {
  if (document.querySelector('nav a.active[data-view="runs"]') &&
      document.querySelector("#runs-list .s-running")) refreshRuns();
}, 2000);

