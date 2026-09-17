/* Sovereign Harness: Clean Single-Chat Application Controller */
"use strict";

const TOKEN = location.hash ? decodeURIComponent(location.hash.slice(1)) : null;

// API Fetch Helper
async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (TOKEN) headers["X-Harness-Auth"] = TOKEN;
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  let body = null;
  try { body = await res.json(); } catch (_e) {}
  if (!res.ok) {
    const msg = (body && body.error) ? body.error : `${res.status} ${res.statusText}`;
    throw new Error(msg);
  }
  return body;
}

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const fmtCost = (v) => (typeof v === "number") ? `$${v.toFixed(4)}` : "$0.0000";

// Client State
let sessionId = localStorage.getItem("harness_session_id") || "sess_" + Math.random().toString(36).slice(2, 10);
localStorage.setItem("harness_session_id", sessionId);

let autoApply = localStorage.getItem("harness_auto_apply") !== "false";
let currentRunId = null;
let pollTimer = null;
let eventSeq = 0;

// Initialize
window.addEventListener("DOMContentLoaded", () => {
  setupInputHandlers();
  setupHeaderControls();
  setupStarterChips();
  loadHistory();
  pollSpend();
  setInterval(pollSpend, 8000);
});

// Setup Starter Chips
function setupStarterChips() {
  $$(".starter-chip").forEach(chip => {
    chip.addEventListener("click", () => {
      const prompt = chip.dataset.prompt;
      if (prompt) {
        $("#prompt-input").value = prompt;
        submitPrompt(prompt);
      }
    });
  });
}

// Setup Header Controls
function setupHeaderControls() {
  const modeBtn = $("#btn-mode-toggle");
  updateModeDisplay();

  modeBtn.addEventListener("click", () => {
    autoApply = !autoApply;
    localStorage.setItem("harness_auto_apply", autoApply ? "true" : "false");
    updateModeDisplay();
  });

  $("#btn-new-chat").addEventListener("click", () => {
    sessionId = "sess_" + Math.random().toString(36).slice(2, 10);
    localStorage.setItem("harness_session_id", sessionId);
    $("#chat-feed").innerHTML = "";
    $("#welcome-hero").hidden = false;
    $("#prompt-input").value = "";
    $("#prompt-input").focus();
  });
}

function updateModeDisplay() {
  const modeBtn = $("#btn-mode-toggle");
  const icon = $("#mode-icon");
  const text = $("#mode-text");
  if (autoApply) {
    modeBtn.classList.remove("review-mode");
    icon.textContent = "⚡";
    text.textContent = "Auto";
    modeBtn.title = "Autonomous mode: changes verified and applied automatically";
  } else {
    modeBtn.classList.add("review-mode");
    icon.textContent = "🛡️";
    text.textContent = "Review";
    modeBtn.title = "Review-first mode: inspect diff before applying";
  }
}

// Setup Input & Textarea Auto-growth
function setupInputHandlers() {
  const ta = $("#prompt-input");
  const form = $("#chat-form");
  const stopBtn = $("#btn-stop");

  ta.addEventListener("input", () => {
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 180) + "px";
  });

  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (ta.value.trim()) {
        form.dispatchEvent(new Event("submit"));
      }
    }
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const val = ta.value.trim();
    if (!val || currentRunId) return;
    submitPrompt(val);
  });

  stopBtn.addEventListener("click", async () => {
    if (currentRunId) {
      try {
        await api(`/api/runs/${currentRunId}/cancel`, { method: "POST" });
      } catch (_e) {}
    }
  });
}

// Submit Prompt
async function submitPrompt(prompt) {
  $("#welcome-hero").hidden = true;
  const ta = $("#prompt-input");
  ta.value = "";
  ta.style.height = "auto";

  // Append User Bubble
  appendUserMessage(prompt);

  // Append Agent Card with Live Progress Stepper
  const agentMsg = createAgentMessageCard();
  $("#chat-feed").appendChild(agentMsg.card);
  scrollToBottom();

  setInFlight(true);

  try {
    const payload = {
      prompt: prompt,
      session_id: sessionId,
      auto_apply: autoApply,
    };

    const run = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify(payload),
    });

    currentRunId = run.id;
    eventSeq = 0;
    pollExecution(run.id, agentMsg);
  } catch (err) {
    agentMsg.stepper.hidden = true;
    agentMsg.body.innerHTML = `<p style="color:var(--red);">Error starting task: ${esc(err.message)}</p>`;
    setInFlight(false);
  }
}

// Poll Execution & Events
function pollExecution(runId, agentMsg) {
  const pollInterval = 350;

  async function tick() {
    if (!currentRunId) return;
    try {
      // 1. Fetch recent events
      const evData = await api(`/api/runs/${runId}/events?after=${eventSeq}`);
      if (evData.events && evData.events.length) {
        for (const ev of evData.events) {
          eventSeq = Math.max(eventSeq, ev.seq);
          handleLiveEvent(ev, agentMsg);
        }
      }

      // 2. Check run status
      const resData = await api(`/api/runs/${runId}/result`);
      if (resData.status !== "running") {
        clearInterval(pollTimer);
        currentRunId = null;
        setInFlight(false);
        renderFinalResult(resData, agentMsg);
        pollSpend();
        return;
      }
    } catch (_e) {}
  }

  pollTimer = setInterval(tick, pollInterval);
  tick();
}

// Live Progress Stepper Updates
function handleLiveEvent(ev, agentMsg) {
  const body = agentMsg.stepperBody;
  agentMsg.stepper.hidden = false;

  let label = "";
  let icon = "✓";

  if (ev.type === "intent_classified") {
    label = `Classified intent: ${ev.intent || "general"}`;
  } else if (ev.type === "files_discovered") {
    const files = ev.target_files || [];
    label = files.length ? `Identified file scope: ${files.join(", ")}` : "No specific file scope required";
  } else if (ev.type === "context_condensed") {
    label = `Context condensed via AST MicroBrief (~${ev.estimated_tokens || 0} tokens)`;
  } else if (ev.type === "dag_planned") {
    label = `Decomposed into ${ev.total_nodes || 1} subtask(s); ceiling $${(ev.total_ceiling || 0).toFixed(4)}`;
  } else if (ev.type === "subtask_start") {
    label = `Executing subtask ${ev.node_id || ""}: ${ev.instruction || ""}`;
    icon = "⚙";
  } else if (ev.type === "subtask_retry") {
    label = `Verification failed; auto-healing retry: ${ev.error || ""}`;
    icon = "↻";
  } else if (ev.type === "subtask_finish") {
    label = `Completed subtask ${ev.node_id || ""} [${ev.status || "ok"}]`;
  }

  if (label) {
    const item = document.createElement("div");
    item.className = "step-item done";
    item.innerHTML = `<span class="step-icon">${esc(icon)}</span> <span>${esc(label)}</span>`;
    body.appendChild(item);
    scrollToBottom();
  }
}

// Render Final Response
function renderFinalResult(runRecord, agentMsg) {
  agentMsg.spinner.hidden = true;
  agentMsg.stepperTitleText.textContent = "Execution complete";

  if (runRecord.status === "cancelled") {
    agentMsg.body.innerHTML = `<p style="color:var(--yellow);">Execution cancelled by user.</p>`;
    return;
  }

  const res = runRecord.result || {};
  const responseText = res.response || runRecord.error || "Completed.";

  // Render Markdown Body
  agentMsg.body.innerHTML = renderSimpleMarkdown(responseText);

  // Render Diff if present
  if (res.diff && res.diff.trim()) {
    const diffContainer = document.createElement("div");
    diffContainer.className = "diff-container";
    diffContainer.innerHTML = `
      <div class="diff-header">
        <span>MODIFIED FILES: ${esc((res.target_files || []).join(", ") || "patch")}</span>
        <button type="button" class="btn-ghost" onclick="this.parentElement.nextElementSibling.hidden = !this.parentElement.nextElementSibling.hidden">Toggle Diff</button>
      </div>
      <div class="diff-body">${renderDiffLines(res.diff)}</div>
    `;
    agentMsg.card.appendChild(diffContainer);
  }

  // Footer Spend Pill
  const cost = res.cost ?? 0.0;
  agentMsg.footer.innerHTML = `
    <span>Model: ${esc(res.model || "Sliding-Scale Multi-Tier")}</span>
    <span>Spend: ${fmtCost(cost)}</span>
  `;
  agentMsg.footer.hidden = false;

  scrollToBottom();
}

// DOM Builders
function appendUserMessage(text) {
  const msg = document.createElement("div");
  msg.className = "message user";
  msg.innerHTML = `<div class="bubble">${esc(text)}</div>`;
  $("#chat-feed").appendChild(msg);
  scrollToBottom();
}

function createAgentMessageCard() {
  const card = document.createElement("div");
  card.className = "message agent";

  card.innerHTML = `
    <div class="card">
      <div class="stepper">
        <div class="stepper-header" onclick="this.nextElementSibling.hidden = !this.nextElementSibling.hidden">
          <div class="stepper-title">
            <span class="spinner"></span>
            <span class="stepper-title-text">Processing task...</span>
          </div>
          <span style="font-size:10px; color:var(--dim);">collapse</span>
        </div>
        <div class="stepper-body"></div>
      </div>
      <div class="markdown-body"></div>
      <div class="msg-footer" hidden></div>
    </div>
  `;

  return {
    card: card,
    stepper: card.querySelector(".stepper"),
    spinner: card.querySelector(".spinner"),
    stepperTitleText: card.querySelector(".stepper-title-text"),
    stepperBody: card.querySelector(".stepper-body"),
    body: card.querySelector(".markdown-body"),
    footer: card.querySelector(".msg-footer"),
  };
}

// Simple Markdown & Diff Helpers
function renderSimpleMarkdown(text) {
  let html = esc(text);

  // Fenced Code Blocks
  html = html.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, (_m, _lang, code) => {
    return `<pre><code>${code.trim()}</code></pre>`;
  });

  // Inline Code
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");

  // Bold
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");

  // Paragraphs
  const paragraphs = html.split(/\n\n+/);
  return paragraphs.map(p => {
    if (p.startsWith("<pre>") || p.startsWith("<ul>")) return p;
    return `<p>${p.replace(/\n/g, "<br>")}</p>`;
  }).join("");
}

function renderDiffLines(diffText) {
  return diffText.split("\n").map(line => {
    let cls = "";
    if (line.startsWith("+") && !line.startsWith("+++")) cls = "add";
    else if (line.startsWith("-") && !line.startsWith("---")) cls = "del";
    else if (line.startsWith("@@")) cls = "hdr";
    return `<div class="diff-line ${cls}">${esc(line)}</div>`;
  }).join("");
}

function setInFlight(inFlight) {
  $("#btn-send").hidden = inFlight;
  $("#btn-stop").hidden = !inFlight;
  $("#prompt-input").disabled = inFlight;
  if (!inFlight) $("#prompt-input").focus();
}

function scrollToBottom() {
  const c = $("#chat-container");
  c.scrollTop = c.scrollHeight;
}

// Load Persisted History
async function loadHistory() {
  try {
    const data = await api(`/api/chat/history?session_id=${encodeURIComponent(sessionId)}`);
    if (data.history && data.history.length) {
      $("#welcome-hero").hidden = true;
      for (const turn of data.history) {
        if (turn.prompt) appendUserMessage(turn.prompt);
        const card = createAgentMessageCard();
        card.stepper.hidden = true;
        card.body.innerHTML = renderSimpleMarkdown(turn.response || "");
        if (turn.diff) {
          const diffContainer = document.createElement("div");
          diffContainer.className = "diff-container";
          diffContainer.innerHTML = `
            <div class="diff-header">
              <span>DIFF: ${esc((turn.target_files || []).join(", "))}</span>
            </div>
            <div class="diff-body">${renderDiffLines(turn.diff)}</div>
          `;
          card.card.appendChild(diffContainer);
        }
        card.footer.innerHTML = `<span>Spend: ${fmtCost(turn.cost || 0)}</span>`;
        card.footer.hidden = false;
        $("#chat-feed").appendChild(card.card);
      }
      scrollToBottom();
    }
  } catch (_e) {}
}

// Poll Spend & Quota
async function pollSpend() {
  try {
    const spend = await api("/api/spend");
    const s = spend.session || {};
    $("#spend-val").textContent = fmtCost(s.spent || 0);
    $("#spend-limit").textContent = `/ ${fmtCost(s.ceiling || 0.05)}`;
  } catch (_e) {}
}

// ---- rankings (read-only mirror for contract pin) -------------------------
async function loadRankings() {
  try {
    const r = await api("/api/rankings");
    const out = $("#rankings-out") || document.createElement("div");
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
    const out = $("#rankings-out");
    if (out) out.innerHTML = `<div class="dim">${esc(e.message)}</div>`;
  }
}
