let authTokenOverride;

function getAuthToken() {
  if (authTokenOverride !== undefined) return authTokenOverride;
  if (typeof location !== "undefined") {
    if (location.hash && location.hash.length > 1) {
      const h = decodeURIComponent(location.hash.slice(1));
      if (h && !h.startsWith("/")) {
        let stored = false;
        try {
          localStorage.setItem("harness_ui_auth_token", h);
          stored = true;
        } catch (_e) {}
        if (stored && window.history && window.history.replaceState) {
          try {
            window.history.replaceState(window.history.state, "",
              window.location.pathname + window.location.search);
          } catch (_e) {}
        }
        return h;
      }
    }
  }
  // The fragment is only a launch handoff. Remove it from the address bar and
  // browser history before any API request; the token is sent in a header.
  try {
    if (typeof history !== "undefined" && history.replaceState) {
      history.replaceState(history.state, "", location.pathname + location.search);
    }
  } catch (_e) {}
}

function setAuthToken(token) {
  const t = token ? String(token).trim() : null;
  authTokenOverride = t;
  if (t) {
    try { localStorage.setItem("harness_ui_auth_token", t); } catch (_e) {}
  } else {
    try { localStorage.removeItem("harness_ui_auth_token"); } catch (_e) {}
  }
  updateAuthDisplay();
}

window.HARNESS_GET_TOKEN = getAuthToken;
window.HARNESS_SET_TOKEN = setAuthToken;

let isAuthPromptOpen = false;
let authPromptPromise = null;
let authPromptGeneration = 0;

function promptForAuthToken(errMsg) {
  if (isAuthPromptOpen && authPromptPromise) return authPromptPromise;
  isAuthPromptOpen = true;
  const generation = ++authPromptGeneration;
  authPromptPromise = new Promise((resolve) => {
    const modal = document.getElementById("auth-modal");
    if (!modal) {
      const entered = window.prompt("The Harness server requires an authorization token (X-Harness-Auth). Enter token:", getAuthToken() || "");
      if (entered) setAuthToken(entered);
      isAuthPromptOpen = false;
      resolve(entered);
      return;
    }
    const errText = document.getElementById("auth-error-msg");
    if (errText && errMsg) errText.textContent = errMsg;
    const input = document.getElementById("auth-token-input");
    const feedback = document.getElementById("auth-feedback");
    if (input) input.value = getAuthToken() || "";
    if (feedback) {
      feedback.textContent = "";
      feedback.hidden = true;
    }
    modal.hidden = false;
    if (input) input.focus();

    const saveBtn = document.getElementById("btn-save-token");
    const cancelBtn = document.getElementById("btn-cancel-token");
    if (saveBtn) saveBtn.disabled = false;

    const cleanup = () => {
      if (generation !== authPromptGeneration || !isAuthPromptOpen) return;
      modal.hidden = true;
      isAuthPromptOpen = false;
      authPromptPromise = null;
    };

    if (input) {
      input.onkeydown = (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          if (saveBtn) saveBtn.click();
        } else if (e.key === "Escape") {
          e.preventDefault();
          if (cancelBtn) cancelBtn.click();
        }
      };
    }

    if (saveBtn) {
      saveBtn.onclick = async () => {
        const val = input ? input.value.trim() : "";
        if (!val) {
          if (feedback) {
            feedback.textContent = "Enter the local UI token to continue.";
            feedback.hidden = false;
          }
          if (input) input.focus();
          return;
        }
        saveBtn.disabled = true;
        if (feedback) {
          feedback.textContent = "Checking token…";
          feedback.hidden = false;
        }
        try {
          const check = await fetch("/api/status", {
            headers: { "X-Harness-Auth": val }, cache: "no-store",
          });
          if (generation !== authPromptGeneration || !isAuthPromptOpen) return;
          if (check.status === 401) {
            if (feedback) feedback.textContent =
              "That token was not accepted. Check the launch instructions above and try again.";
            return;
          }
          if (!check.ok) {
            if (feedback) feedback.textContent =
              `Harness could not validate the token (HTTP ${check.status}). Try again.`;
            return;
          }
          setAuthToken(val);
          cleanup();
          pollRoute();
          pollSpend();
          refreshSessionList();
          resolve(val);
        } catch (_e) {
          if (generation === authPromptGeneration && isAuthPromptOpen && feedback) {
            feedback.textContent =
              "Could not reach Harness to check the token. Confirm the server is running and try again.";
          }
        } finally {
          if (generation === authPromptGeneration && isAuthPromptOpen) {
            saveBtn.disabled = false;
          }
        }
      };
    }
    if (cancelBtn) {
      cancelBtn.onclick = () => {
        cleanup();
        resolve(null);
      };
    }
  });
  return authPromptPromise;
}

window.HARNESS_PROMPT_AUTH = promptForAuthToken;

function updateAuthDisplay() {
  const btn = document.getElementById("btn-auth-toggle");
  const text = document.getElementById("auth-text");
  const icon = document.getElementById("auth-icon");
  const tok = getAuthToken();
  if (btn && text) {
    if (tok) {
      text.textContent = "Auth set";
      if (icon) icon.textContent = "🔒";
      btn.title = "X-Harness-Auth token configured. Click to view or change.";
    } else {
      text.textContent = "Auth";
      if (icon) icon.textContent = "🔓";
      btn.title = "No X-Harness-Auth token set. Click to configure.";
    }
  }
}

// API Fetch Helper
async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  const token = getAuthToken();
  if (token) headers["X-Harness-Auth"] = token;
  let res;
  try {
    res = await fetch(path, Object.assign({}, opts, { headers }));
  } catch (netErr) {
    throw new Error(`Network error calling ${path}: ${netErr.message}`);
  }
  let body = null;
  try { body = await res.json(); } catch (_e) {}
  if (res.status === 401) {
    const errReason = (body && body.error) ? body.error : "missing or wrong X-Harness-Auth token";
    const entered = await promptForAuthToken(errReason);
    if (entered) {
      headers["X-Harness-Auth"] = entered;
      res = await fetch(path, Object.assign({}, opts, { headers }));
      try { body = await res.json(); } catch (_e) {}
    }
  }
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

// A fresh page starts review-first. Auto Apply is a deliberate page-local choice.
localStorage.removeItem("harness_auto_apply");
let autoApply = false;
// Mirror of the server-side ``allow_escalation`` setting (single source of
// truth: /api/settings). The old localStorage flag is gone -- it desynced
// from the server and clicking the route badge changed nothing real.
let paidEnabled = null;
let webEnabled = localStorage.getItem("harness_web_enabled") === "true";
// Retire the old profile-global workspace: blank now means chat-only.
localStorage.removeItem("harness_workdir");
const workspaceKey = (id) => `harness_workspace:${id}`;
let workDir = sessionStorage.getItem(workspaceKey(sessionId)) || "";
window.getHarnessWorkspace = () => workDir.trim();
let currentRunId = null;
let activeAgentMessage = null;
let pollTimer = null;
let runGeneration = 0;
let eventSeq = 0;
let routeRequestSeq = 0;

// Initialize
window.addEventListener("DOMContentLoaded", () => {
  setupInputHandlers();
  setupHeaderControls();
  setupSidebar();
  setupStarterChips();
  loadHistory();
  pollSpend();
  pollRoute();
  setInterval(pollSpend, 8000);
  setInterval(pollRoute, 15000);
});

// Setup Sidebar (session list, workdir)
function setupSidebar() {
  const workdirInput = $("#workdir-input");
  if (workdirInput) {
    workdirInput.value = workDir;
    workdirInput.addEventListener("change", () => {
      workDir = workdirInput.value.trim();
      if (workDir) sessionStorage.setItem(workspaceKey(sessionId), workDir);
      else sessionStorage.removeItem(workspaceKey(sessionId));
    });
  }
  refreshSessionList();
}

// Session list: fetch + render
async function refreshSessionList() {
  const list = $("#session-list");
  if (!list) return;
  try {
    const data = await api("/api/chat/sessions");
    const sessions = data.sessions || [];
    list.innerHTML = "";
    if (!sessions.length) {
      list.innerHTML = `<div class="session-empty">No conversations yet</div>`;
      return;
    }
    for (const s of sessions) {
      const item = document.createElement("div");
      item.className = "session-item" + (s.id === sessionId ? " active" : "");
      const when = new Date(s.updated_at * 1000).toLocaleString();
      item.innerHTML = `
        <span class="session-preview" title="${esc(s.preview)} · ${esc(when)}">${esc(s.preview)}</span>
        <button type="button" class="session-delete" title="Delete conversation">✕</button>
      `;
      item.querySelector(".session-delete").addEventListener("click", (e) => {
        e.stopPropagation();
        deleteSession(s.id);
      });
      item.addEventListener("click", () => switchSession(s.id));
      list.appendChild(item);
    }
  } catch (_e) {
    list.innerHTML = `<div class="session-empty">Session list unavailable</div>`;
  }
}

async function switchSession(id) {
  if (id === sessionId) return;
  sessionId = id;
  localStorage.setItem("harness_session_id", sessionId);
  workDir = sessionStorage.getItem(workspaceKey(sessionId)) || "";
  const workdirInput = $("#workdir-input");
  if (workdirInput) workdirInput.value = workDir;
  autoApply = false;
  updateModeDisplay();
  $("#chat-feed").innerHTML = "";
  $("#welcome-hero").hidden = false;
  await loadHistory();
  refreshSessionList();
}

async function deleteSession(id) {
  if (!confirm("Delete this conversation?")) return;
  try {
    await api("/api/chat/session/delete", { method: "POST", body: JSON.stringify({ session_id: id }) });
    if (id === sessionId) {
      sessionId = "sess_" + Math.random().toString(36).slice(2, 10);
      localStorage.setItem("harness_session_id", sessionId);
      workDir = "";
      autoApply = false;
      const workdirInput = $("#workdir-input");
      if (workdirInput) workdirInput.value = "";
      updateModeDisplay();
      $("#chat-feed").innerHTML = "";
      $("#welcome-hero").hidden = false;
    }
    refreshSessionList();
  } catch (_e) {}
}

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
    updateModeDisplay();
  });

  const routeBtn = $("#route-badge");
  if (routeBtn) {
    updateRouteDisplay();
    // Persisted toggle (POST /api/settings -> config.json). Plain click
    // switches the primary route posture (free <-> paid); Shift+click arms
    // or disarms automatic paid escalation. The server's response is the
    // new truth -- the UI re-renders from what was actually persisted.
    routeBtn.addEventListener("click", async (ev) => {
      if (!backendSettings || routeBtn.disabled) return;
      const s = backendSettings;
      const body = ev.shiftKey
        ? { allow_escalation: !s.allow_escalation }
        : { use_free: s.use_free === false };
      routeBtn.disabled = true;
      try {
        const data = await api("/api/settings", {
          method: "POST",
          body: JSON.stringify(body),
        });
        backendSettings = data.settings || backendSettings;
        if (backendSettings && typeof backendSettings.allow_escalation === "boolean") {
          paidEnabled = backendSettings.allow_escalation;
        }
      } catch (e) {
        routeBtn.title = "Toggle failed: " + e.message;
      } finally {
        routeBtn.disabled = false;
      }
      updateRouteDisplay();
    });
  }

  // Price-cap popover: click the spend meter to adjust the run ceiling
  // (slider for quick range, text input for exact values; server validates
  // against the hard ceiling and refuses out-of-range values).
  const meter = $("#spend-meter");
  const capPop = $("#cap-popover");
  if (meter && capPop) {
    meter.classList.add("clickable");
    meter.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (capPop.hidden) openCapPopover();
      else closeCapPopover();
    });
    $("#cap-slider").addEventListener("input", () => {
      const v = Number($("#cap-slider").value);
      $("#cap-text").value = v.toFixed(2);
      $("#cap-slider-val").textContent = fmtCost(v);
    });
    $("#cap-text").addEventListener("input", () => {
      const v = Number($("#cap-text").value);
      if (Number.isFinite(v) && v >= CAP_SLIDER_MIN && v <= CAP_SLIDER_MAX) {
        $("#cap-slider").value = String(v);
        $("#cap-slider-val").textContent = fmtCost(v);
      }
    });
    $("#cap-text").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") $("#cap-save").click();
    });
    $("#cap-save").addEventListener("click", () => saveCap($("#cap-text").value));
    document.addEventListener("click", (ev) => {
      if (!capPop.hidden && !capPop.contains(ev.target)) closeCapPopover();
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && !capPop.hidden) closeCapPopover();
    });
  }


  $("#btn-new-chat").addEventListener("click", () => {
    sessionId = "sess_" + Math.random().toString(36).slice(2, 10);
    localStorage.setItem("harness_session_id", sessionId);
    workDir = "";
    autoApply = false;
    const workdirInput = $("#workdir-input");
    if (workdirInput) workdirInput.value = "";
    updateModeDisplay();
    $("#chat-feed").innerHTML = "";
    $("#welcome-hero").hidden = false;
    $("#prompt-input").value = "";
    $("#prompt-input").focus();
    refreshSessionList();
  });

  const webBtn = $("#btn-web-toggle");
  if (webBtn) {
    updateWebDisplay();
    webBtn.addEventListener("click", () => {
      webEnabled = !webEnabled;
      localStorage.setItem("harness_web_enabled", webEnabled ? "true" : "false");
      updateWebDisplay();
    });
  }

  const authBtn = $("#btn-auth-toggle");
  if (authBtn) {
    updateAuthDisplay();
    authBtn.addEventListener("click", () => {
      promptForAuthToken("Enter or update your X-Harness-Auth token:");
    });
  }
}

let backendSettings = null;

function updateRouteDisplay() {
  const badge = $("#route-badge");
  const text = $("#route-text");
  const icon = $("#route-icon");
  if (!badge || !text) return;

  const s = backendSettings || {};
  const hasPaidKey = s.paid_key_present;

  if (!backendSettings || typeof paidEnabled !== "boolean") {
    badge.classList.add("disarmed");
    text.textContent = "Route: loading...";
    badge.title = "Routing posture is loading from the server.";
    return;
  }

  if (s.use_free === false) {
    badge.classList.remove("disarmed");
    if (icon) icon.textContent = "💳";
    text.textContent = "Primary: paid";
    badge.title = "Paid primary route. Click to switch to free primary; Shift+click to arm/disarm paid escalation.";
    if (s.allow_escalation === false) badge.classList.add("disarmed");
    return;
  }

  if (hasPaidKey === false) {
    badge.classList.add("disarmed");
    if (icon) icon.textContent = "💳";
    text.textContent = "Primary: free · escalation unavailable";
    badge.title = "No paid API key configured; free tier only.";
    return;
  }

  if (paidEnabled) {
    badge.classList.remove("disarmed");
    if (icon) icon.textContent = "💳";
    text.textContent = "Primary: free · paid escalation armed";
    badge.title = "Paid escalation ARMED: automatic fallback to cheapest capable paid model on limits (Shift+click to disarm)";
  } else {
    badge.classList.add("disarmed");
    if (icon) icon.textContent = "💳";
    text.textContent = "Primary: free · escalation disarmed";
    badge.title = "Paid escalation DISARMED: free tier only; ask before entering paid rungs (Shift+click to arm)";
  }
}


function updateWebDisplay() {
  const btn = $("#btn-web-toggle");
  if (!btn) return;
  const icon = $("#web-icon");
  const text = $("#web-text");
  if (webEnabled) {
    btn.classList.remove("web-off");
    icon.textContent = "🌐";
    text.textContent = "Web on";
    btn.title = "Web tools ON: chat may search and fetch allowlisted pages for this run";
  } else {
    btn.classList.add("web-off");
    icon.textContent = "🌐";
    text.textContent = "Web off";
    btn.title = "Web tools OFF: the agent has no internet access and will say so";
  }
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
    const agentMsg = activeAgentMessage;
    if (!agentMsg) return;
    agentMsg.stopRequested = true;
    stopBtn.disabled = true;
    stopBtn.title = "Stop requested; waiting for the active request to close...";
    agentMsg.stepperTitleText.textContent = "Stop requested; closing the active request...";
    // /api/chat creates the run before returning its ID. Preserve this stop
    // intent and send it as soon as that response arrives.
    if (currentRunId) await requestRunCancellation(currentRunId, agentMsg);
  });
}

async function requestRunCancellation(runId, agentMsg) {
  try {
    const result = await api(`/api/runs/${runId}/cancel`, { method: "POST" });
    if (currentRunId !== runId || activeAgentMessage !== agentMsg) return false;
    if (!result.cancel_requested) throw new Error("server did not accept the stop request");
    agentMsg.activeRequests = {};
    agentMsg.activeRequestAt = null;
    return true;
  } catch (_e) {
    if (currentRunId !== runId || activeAgentMessage !== agentMsg) return false;
    const stopBtn = $("#btn-stop");
    stopBtn.disabled = false;
    stopBtn.title = "Stop execution";
    agentMsg.stopRequested = false;
    agentMsg.stepperTitleText.textContent = "Stop request failed; try again.";
    return false;
  }
}

// Submit Prompt
async function submitPrompt(prompt) {
  const generation = ++runGeneration;
  if (pollTimer !== null) clearTimeout(pollTimer);
  pollTimer = null;
  $("#welcome-hero").hidden = true;
  const ta = $("#prompt-input");
  ta.value = "";
  ta.style.height = "auto";

  // Append User Bubble
  appendUserMessage(prompt);

  // Append Agent Card with Live Progress Stepper
  const agentMsg = createAgentMessageCard();
  activeAgentMessage = agentMsg;
  $("#chat-feed").appendChild(agentMsg.card);
  scrollToBottom();

  setInFlight(true);

  try {
    const payload = {
      prompt: prompt,
      session_id: sessionId,
      auto_apply: autoApply,
      web: webEnabled,
    };
    if (typeof paidEnabled === "boolean") payload.allow_paid = paidEnabled;
    if (workDir) payload.root_dir = workDir;

    const run = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (generation !== runGeneration || activeAgentMessage !== agentMsg) return;
    currentRunId = run.id;
    eventSeq = 0;
    if (agentMsg.stopRequested) await requestRunCancellation(run.id, agentMsg);
    if (generation !== runGeneration || currentRunId !== run.id
        || activeAgentMessage !== agentMsg) return;
    pollExecution(run.id, agentMsg, generation);
      } catch (err) {
        if (generation !== runGeneration || activeAgentMessage !== agentMsg) return;
        agentMsg.stepper.hidden = true;
        agentMsg.body.innerHTML = `<p style="color:var(--red);">Error starting task: ${esc(err.message)}</p>`;
        activeAgentMessage = null;
        setInFlight(false);
      }
    }

// Poll Execution & Events
function pollExecution(runId, agentMsg, generation = runGeneration) {
  const pollInterval = 350;
  let stopped = false;

  function ownsRun() {
    return !stopped && generation === runGeneration
      && currentRunId === runId && activeAgentMessage === agentMsg;
  }

  async function tick() {
    if (!ownsRun()) return;
    pollTimer = null;
    try {
      // 1. Fetch recent events
      const evData = await api(`/api/runs/${runId}/events?after=${eventSeq}`);
      if (!ownsRun()) return;
      if (evData.events && evData.events.length) {
        for (const ev of evData.events) {
          eventSeq = Math.max(eventSeq, ev.seq);
          handleLiveEvent(ev, agentMsg);
        }
      }
      if (agentMsg.stopRequested) {
        agentMsg.stepperTitleText.textContent = "Stop requested; closing the active request...";
      } else if (Object.keys(agentMsg.activeRequests || {}).length) {
        updateActiveProviderProgress(agentMsg);
      }

      // 2. Check run status
      const resData = await api(`/api/runs/${runId}/result`);
      if (!ownsRun()) return;
      if (resData.status !== "running") {
        stopped = true;
        if (pollTimer !== null) clearTimeout(pollTimer);
        pollTimer = null;
        currentRunId = null;
        agentMsg.stopRequested = false;
        agentMsg.activeRequests = {};
        agentMsg.activeRequestAt = null;
        activeAgentMessage = null;
        setInFlight(false);
        renderFinalResult(resData, agentMsg);
        pollSpend();
        // The session file is persisted at turn end; refresh so the new
        // conversation appears in the sidebar with its preview.
        refreshSessionList();
        return;
      }
    } catch (_e) {}

    // Schedule only after the current poll has completely finished. This
    // prevents overlapping requests and makes late responses harmless once
    // another run owns the UI.
    if (ownsRun()) {
      pollTimer = setTimeout(tick, pollInterval);
    }
  }

  tick();
}

// Live Progress Stepper Updates
const PHASE_OK_STATUSES = new Set(["ok", "applied", "verified", "completed", "merged"]);

function phaseKey(agentMsg, nodeId) {
  return `r${agentMsg.orchRound || 1}:${nodeId || "?"}`;
}

function ensurePhase(agentMsg, nodeId, target, instruction) {
  const key = phaseKey(agentMsg, nodeId);
  if (agentMsg.phaseItems[key]) return agentMsg.phaseItems[key];
  if (!agentMsg.phaseList) {
    agentMsg.phaseList = document.createElement("div");
    agentMsg.phaseList.className = "phase-list";
    agentMsg.stepperBody.appendChild(agentMsg.phaseList);
  }
  const row = document.createElement("div");
  row.className = "step-item";
  const excerpt = String(instruction || target || "").slice(0, 70);
  row.innerHTML = `<span class="step-icon">·</span> <span>${esc(String(nodeId || "?"))}`
    + `${target ? ` → ${esc(String(target))}` : ""}${excerpt ? `: ${esc(excerpt)}` : ""}</span>`;
  const glyph = row.querySelector(".step-icon");
  agentMsg.phaseList.appendChild(row);
  const phase = { row, glyph };
  agentMsg.phaseItems[key] = phase;
  scrollToBottom();
  return phase;
}

function setPhase(agentMsg, nodeId, glyph, done) {
  const phase = agentMsg.phaseItems[phaseKey(agentMsg, nodeId)];
  if (!phase) return;
  phase.glyph.textContent = glyph;
  phase.row.className = `step-item${done ? " done" : ""}`;
}

function activateModularAspect(agentMsg, aspectId, label, icon) {
  const bar = agentMsg.aspectBar || (agentMsg.card && agentMsg.card.querySelector(".aspect-pipeline-bar"));
  if (!bar) return;
  bar.hidden = false;

  const existing = bar.querySelector(`[data-aspect="${aspectId}"]`);
  if (existing) {
    if (label) existing.textContent = `${icon ? icon + " " : ""}${label}`;
    const allChips = bar.querySelectorAll(".aspect-chip");
    allChips.forEach(c => {
      if (c === existing) {
        c.classList.remove("done");
        c.classList.add("active");
      } else {
        c.classList.remove("active");
        c.classList.add("done");
      }
    });
    return;
  }

  // Mark all previous chips done
  const prevChips = bar.querySelectorAll(".aspect-chip");
  prevChips.forEach(c => {
    c.classList.remove("active");
    c.classList.add("done");
  });

  // If there are already chips, add an arrow separator
  if (prevChips.length > 0) {
    const arrow = document.createElement("span");
    arrow.className = "aspect-arrow";
    arrow.textContent = "➔";
    bar.appendChild(arrow);
  }

  // Create new active modular chip
  const chip = document.createElement("span");
  chip.className = "aspect-chip active";
  chip.dataset.aspect = aspectId;
  chip.textContent = `${icon ? icon + " " : ""}${label}`;
  bar.appendChild(chip);
}

function handleLiveEvent(ev, agentMsg) {
  const body = agentMsg.stepperBody;
  agentMsg.stepper.hidden = false;

  let label = "";
  let icon = "✓";
  let active = false;

  if (ev.type === "chat_turn_start") {
    label = "Request accepted; preparing context and routing.";
    agentMsg.stepperTitleText.textContent = "Preparing your request...";
  } else if (ev.type === "chat_resume") {
    label = `Resuming the interrupted ${ev.route || "conversation"} request from its saved context.`;
    agentMsg.stepperTitleText.textContent = "Resuming your request...";
  } else if (ev.type === "chat_resume_unavailable") {
    label = "No interrupted chat request is saved to resume.";
    agentMsg.stepperTitleText.textContent = "Ready for a new request.";
  } else if (ev.type === "hourglass_request") {
    label = `Request review started (${ev.intent || "general"}; up to ${ev.max_rounds || 1} answer round(s)).`;
    agentMsg.stepperTitleText.textContent = "Reviewing your request...";
  } else if (ev.type === "model_request_start") {
    if (!agentMsg.activeRequests) agentMsg.activeRequests = {};
    const requestId = String(ev.request_id || `event-${ev.seq || Date.now()}`);
    agentMsg.activeRequests[requestId] = {
      model: ev.model || "provider",
      attempt: ev.attempt || "request",
      startedAt: Date.now(),
    };
    updateActiveProviderProgress(agentMsg);
    const retry = ev.attempt === "reasoning_retry" ? " (retry without reasoning)" : "";
    label = `Calling ${ev.model || "provider"}${retry}; waiting for the response.`;
    if (!agentMsg.stopRequested) {
      updateActiveProviderProgress(agentMsg);
    }
  } else if (ev.type === "model_request_end") {
    const requests = agentMsg.activeRequests || {};
    let requestId = ev.request_id ? String(ev.request_id) : null;
    if (!requestId || !Object.prototype.hasOwnProperty.call(requests, requestId)) {
      requestId = Object.keys(requests).find((key) =>
        requests[key].model === (ev.model || "provider") &&
        requests[key].attempt === (ev.attempt || "request"));
    }
    if (requestId) delete requests[requestId];
    agentMsg.activeRequests = requests;
    const remainingRequests = Object.keys(requests).length;
    if (remainingRequests) updateActiveProviderProgress(agentMsg);
    else agentMsg.activeRequestAt = null;
    const seconds = Number(ev.duration_s || 0).toFixed(1);
    label = ev.outcome === "response"
      ? `${ev.model || "Provider"} replied HTTP ${ev.http_status} in ${seconds}s.`
      : `${ev.model || "Provider"} request ${ev.outcome || "ended"} after ${seconds}s.`;
    if (!agentMsg.stopRequested && !remainingRequests) {
      agentMsg.stepperTitleText.textContent = ev.outcome === "response"
        ? "Assessing the response..." : `Provider request ${ev.outcome || "ended"}.`;
    }
  } else if (ev.type === "provider_http_attempt") {
    const requestId = String(ev.request_id || "");
    const request = (agentMsg.activeRequests || {})[requestId];
    if (request) {
      request.wireAttempt = ev.wire_attempt;
      request.httpPhase = ev.phase;
      request.httpStatus = ev.http_status;
      request.retryReason = ev.retry_reason;
      request.retryDelay = ev.delay_s;
    }
    const model = ev.model || (request && request.model) || "provider";
    const wire = ev.wire_attempt ? ` attempt ${ev.wire_attempt}/${ev.max_wire_attempts || "?"}` : "";
    if (ev.phase === "start") {
      label = `${model}: sending provider request${wire}.`;
    } else if (ev.phase === "response") {
      label = `${model}: provider replied HTTP ${ev.http_status}${wire} in ${Number(ev.duration_s || 0).toFixed(1)}s.`;
    } else if (ev.phase === "retry_wait") {
      label = `${model}: HTTP ${ev.http_status}; retrying ${ev.retry_reason || "transient failure"} after ${Number(ev.delay_s || 0).toFixed(1)}s${wire}.`;
      if (!agentMsg.stopRequested) agentMsg.stepperTitleText.textContent = "Waiting before provider retry...";
    } else if (ev.phase === "cancelled") {
      label = `${model}: provider request cancelled${ev.usage_unknown ? "; usage is unknown" : ""}${wire}.`;
    } else if (ev.phase === "error") {
      label = `${model}: provider request failed (${ev.error_type || "network error"})${wire}${ev.usage_unknown ? "; usage is unknown" : ""}.`;
    }
  } else if (ev.type === "provider_usage_unknown") {
    const held = Number(ev.reserved_cost || 0);
    label = `${ev.model || "Provider"}: usage is unknown after dispatch; $${held.toFixed(6)} held against this run's budget.`;
  } else if (ev.type === "rotation") {
    const detail = String(ev.note || ev.reason || "provider response unusable").slice(0, 140);
    label = `${ev.model || "Model"} failed: ${detail}`;
    agentMsg.stepperTitleText.textContent = "Trying the next configured model...";
  } else if (ev.type === "answer_judged") {
    const score = Number(ev.confidence);
    const threshold = Number(ev.confidence_threshold);
    const scoreText = Number.isFinite(score)
      ? `; sufficiency ${Math.round(score * 100)}%` : "";
    const thresholdText = Number.isFinite(threshold)
      ? ` (threshold ${Math.round(threshold * 100)}%)` : "";
    const disposition = ev.status === "pass" ? "accepted" :
      ev.status === "unverified" ? "returned without semantic review" :
        ev.status === "needs_iteration" ? "another answer pass requested" :
          "stopped without another answer pass";
    label = `Jev ${disposition} at round ${ev.round || "?"}${scoreText}${thresholdText}.`;
    agentMsg.stepperTitleText.textContent = ev.status === "pass"
      ? "Jev accepted the answer." : ev.status === "unverified"
        ? "Returning the answer without Jev review."
        : ev.status === "needs_iteration"
          ? "Preparing another bounded answer attempt."
          : "Returning the answer with review status.";
  } else if (ev.type === "request_route_selected") {
    label = ev.native
      ? `Jev suggested ${ev.selected || "a route"}; using ${ev.effective || "the bounded answer lane"}.`
      : `Jev routing unavailable; using ${ev.effective || "the bounded answer lane"}.`;
    agentMsg.stepperTitleText.textContent = "Choosing the smallest useful context...";
  } else if (ev.type === "chat_escalated") {
    label = `Handing off to ${ev.target || "another lane"}: ${String(ev.reason || "").slice(0, 140)}`;
    agentMsg.stepperTitleText.textContent = `Switching to ${ev.target || "another lane"}...`;
  } else if (ev.type === "research_unavailable") {
    label = `Web research stopped before model retries: ${String(ev.reason || "no usable source").slice(0, 160)}`;
    agentMsg.stepperTitleText.textContent = "Web research unavailable; stopping early.";
  } else if (ev.type === "simple_action_route_start") {
    label = "Jev is selecting the request workflow...";
    icon = "⌛";
    active = true;
    agentMsg.stepperTitleText.textContent = "Jev is selecting a bounded action...";
    activateModularAspect(agentMsg, "simple_action_route", "Jev Choice", "⌛");
  } else if (ev.type === "simple_action_route_complete") {
    label = ev.selected === "simple-action"
      ? "Jev selected the one-probe simple-action workflow"
      : `Jev selected ${ev.selected || "no usable workflow"}; no site probe will run`;
    icon = ev.selected === "simple-action" && !ev.fallback ? "✓" : "⚠";
    agentMsg.stepperTitleText.textContent = ev.selected === "simple-action"
      ? "Preparing the site check..." : "Site check stopped before a probe";
    activateModularAspect(agentMsg, "simple_action_route", "Jev Choice", icon);
  } else if (ev.type === "simple_action_route_unavailable") {
    label = "Jev could not select a workflow; no site probe was sent";
    icon = "⚠";
    activateModularAspect(agentMsg, "simple_action_route", "Jev Unavailable", icon);
  } else if (ev.type === "simple_action_start") {
    label = "Preparing one bounded site-availability check";
    icon = "⌛";
    active = true;
    agentMsg.stepperTitleText.textContent = "Checking site availability...";
    activateModularAspect(agentMsg, "simple_action", "Site Check", "⌛");
  } else if (ev.type === "simple_action_preflight_start") {
    label = `Checking public HTTPS target safety for ${ev.target || "the site"}...`;
    icon = "⌛";
    active = true;
    agentMsg.stepperTitleText.textContent = "Checking target safety...";
    activateModularAspect(agentMsg, "simple_action_preflight", "Safety Check", "⌛");
  } else if (ev.type === "simple_action_preflight_complete") {
    label = "Target passed HTTPS and public-address checks; Jev can route the request";
    agentMsg.stepperTitleText.textContent = "Jev is selecting the request workflow...";
    activateModularAspect(agentMsg, "simple_action_preflight", "Safety Check", "✓");
  } else if (ev.type === "simple_action_probe_start") {
    label = `Sending one HTTPS probe to ${ev.target || "the site"}...`;
    icon = "⌛";
    active = true;
    agentMsg.stepperTitleText.textContent = "Checking the HTTPS endpoint...";
    activateModularAspect(agentMsg, "simple_action_probe", "HTTPS Probe", "⌛");
  } else if (ev.type === "simple_action_probe_complete") {
    const status = Number.isInteger(ev.http_status)
      ? `HTTP ${ev.http_status}` : "no HTTP response";
    const details = ev.verdict === "denied" ? " (access denied; endpoint responded)"
      : ev.verdict === "redirect" ? " (redirect; destination not checked)"
      : ev.verdict === "rate_limited" ? " (rate limited)"
      : ev.verdict === "server_error" ? " (server error)" : "";
    const elapsed = Number.isFinite(ev.latency_s)
      ? ` in ${ev.latency_s.toFixed(2)}s` : "";
    label = `HTTPS probe received ${status}${details}${elapsed}`;
    agentMsg.stepperTitleText.textContent = "Jev is checking the observed response...";
    activateModularAspect(agentMsg, "simple_action_probe", `Probe: ${status}`, "✓");
  } else if (ev.type === "simple_action_probe_refused") {
    label = "HTTPS probe stopped by the public-site safety check";
    icon = "⚠";
    activateModularAspect(agentMsg, "simple_action_probe", "Probe Refused", "⚠");
  } else if (ev.type === "simple_action_jev_start") {
    label = "Jev is judging whether the response shows the site is reachable...";
    icon = "⌛";
    active = true;
    agentMsg.stepperTitleText.textContent = "Jev is judging the response...";
    activateModularAspect(agentMsg, "simple_action_jev", "Jev Judgment", "⌛");
  } else if (ev.type === "simple_action_jev_complete") {
    const probability = Number.isFinite(ev.http_response_received)
      ? ev.http_response_received : null;
    if (probability === null) {
      label = "Jev could not provide a usable yes/no judgment; result stays inconclusive";
      icon = "⚠";
    } else {
      const pct = (probability * 100).toFixed(1);
      const thresholdMet = probability >= 0.99;
      label = `Jev yes probability ${pct}%${thresholdMet ? "; 99% threshold met" : "; below 99% threshold"}`;
      icon = thresholdMet ? "✓" : "…";
    }
    agentMsg.stepperTitleText.textContent = "Site-check result ready";
    activateModularAspect(agentMsg, "simple_action_jev", "Jev Judgment", icon);
  } else if (ev.type === "simple_action_jev_unavailable") {
    label = "Jev is unavailable; the site check will remain inconclusive";
    icon = "⚠";
    activateModularAspect(agentMsg, "simple_action_jev", "Jev Unavailable", "⚠");
  } else if (ev.type === "simple_action_complete") {
    label = ev.status === "ok"
      ? `Site check complete${ev.answer ? `: ${ev.answer}` : ""}`
      : "Site check inconclusive";
    icon = ev.status === "ok" ? "✓" : "⚠";
    agentMsg.stepperTitleText.textContent = "Execution complete";
    activateModularAspect(agentMsg, "simple_action", label, icon);
  } else if (ev.type === "web_search") {
    label = ev.phase === "start" ? `Web search: ${ev.query || ""}` : `Web search completed (${ev.results || 0} results)`;
    icon = "🌐";
    activateModularAspect(agentMsg, "web_search", ev.phase === "start" ? "Web Search" : `Search (${ev.results || 0} hits)`, "🌐");
  } else if (ev.type === "web_fetch") {
    label = ev.phase === "start" ? `Fetching allowlisted page: ${ev.url || ""}` : `Retrieved ${ev.chars || 0} chars from primary source`;
    icon = "📖";
    activateModularAspect(agentMsg, "web_fetch", ev.phase === "start" ? "Fetch Source" : `Source (${ev.chars || 0} chars)`, "📖");
  } else if (ev.type === "intent_classified") {
    label = `Classified intent: ${ev.intent || "general"}`;
    activateModularAspect(agentMsg, "intent", `Intent: ${ev.intent || "plan"}`, "🧠");
  } else if (ev.type === "files_discovered") {
    const files = ev.target_files || [];
    label = files.length ? `Identified file scope: ${files.join(", ")}` : "No specific file scope required";
    activateModularAspect(agentMsg, "perception", files.length ? `Scope (${files.length} files)` : "Scope", "👁");
  } else if (ev.type === "context_condensed") {
    label = `Context condensed via AST MicroBrief (~${ev.estimated_tokens || 0} tokens)`;
    activateModularAspect(agentMsg, "context", "AST Context", "📑");
  } else if (ev.type === "dag_planned") {
    label = `Decomposed into ${ev.total_nodes || 1} subtask(s); ceiling $${(ev.total_ceiling || 0).toFixed(4)}`;
    agentMsg.stepperTitleText.textContent = `Executing ${ev.total_nodes || 1} subtask(s)...`;
    activateModularAspect(agentMsg, "plan", `Plan (${ev.total_nodes || 1} subtasks)`, "📋");
    (ev.nodes || []).forEach(n => ensurePhase(agentMsg, n.node_id, n.target, n.instruction));
  } else if (ev.type === "subtask_start") {
    label = `Executing subtask ${ev.node_id || ""}: ${ev.instruction || ""}`;
    icon = "⚙";
    activateModularAspect(agentMsg, `action_${ev.node_id || "step"}`, `Action: ${ev.node_id || "step"}`, "⚙️");
    ensurePhase(agentMsg, ev.node_id, ev.target, ev.instruction);
    setPhase(agentMsg, ev.node_id, "⚙", false);
    agentMsg.stepperTitleText.textContent = `Executing subtask ${ev.node_id || ""}...`;
  } else if (ev.type === "subtask_retry") {
    label = `Verification failed; auto-healing retry: ${ev.error || ""}`;
    icon = "↻";
    activateModularAspect(agentMsg, `action_${ev.node_id || "step"}`, `Retry: ${ev.node_id || ""}`, "↻");
    setPhase(agentMsg, ev.node_id, "↻", false);
  } else if (ev.type === "subtask_finish") {
    label = `Completed subtask ${ev.node_id || ""} [${ev.status || "ok"}]`;
    const ok = PHASE_OK_STATUSES.has(ev.status || "ok");
    activateModularAspect(agentMsg, `action_${ev.node_id || "step"}`, `Subtask ${ev.node_id || ""} [${ev.status || "ok"}]`, ok ? "✓" : "▲");
    setPhase(agentMsg, ev.node_id, ok ? "✓" : "✗", ok);
  } else if (ev.type === "gate_start") {
    label = `Executing verification gate: ${ev.command || ""}`;
    icon = "🔬";
    activateModularAspect(agentMsg, "verify", "Gate Verify", "🔬");
  } else if (ev.type === "gate_end") {
    const ok = Boolean(ev.passed);
    label = `Verification gate ${ok ? "passed (RC 0)" : `failed (RC ${ev.rc})`}`;
    icon = ok ? "✓" : "✗";
    activateModularAspect(agentMsg, "verify", ok ? "Gate Passed" : `Gate Failed (RC ${ev.rc})`, ok ? "✓" : "✗");
  } else if (ev.type === "driver_task_start") {
    label = `Jev Driver initiated request (${ev.max_steps || 5} max steps)`;
    icon = "🚗";
    activateModularAspect(agentMsg, "driver_init", `Driver (${ev.max_steps || 5} steps)`, "🚗");
  } else if (ev.type === "driver_step_start") {
    label = `Jev Driver step #${ev.step}: probing ${ev.schema || "cli"} ➔ ${ev.target || "cli"}`;
    icon = "👁";
    activateModularAspect(agentMsg, `driver_${ev.step}`, `Step ${ev.step}: ${ev.schema || "probe"}`, "👁");
  } else if (ev.type === "driver_step_complete") {
    label = `Jev Driver step #${ev.step} complete (${ev.stopped_at || "verified"})`;
    icon = ev.ok ? "✓" : "▲";
    activateModularAspect(agentMsg, `driver_${ev.step}`, `Step ${ev.step} ${ev.ok ? "Done" : "Refused"}`, ev.ok ? "✓" : "▲");
  } else if (ev.type === "panel_call") {
    label = `Panel seat query sent to ${ev.model || "panel"}`;
    icon = "👥";
    activateModularAspect(agentMsg, "consensus", "Panel Consensus", "👥");
  } else if (ev.type === "panel_vote") {
    label = `Panel vote received from ${ev.model || "panel"}`;
    icon = "🗳️";
    activateModularAspect(agentMsg, "consensus", "Panel Vote", "🗳️");
  } else if (ev.type === "judge_call" || ev.type === "judge_result") {
    label = `Consensus synthesis with judge ${ev.model || "judge"}`;
    icon = "⚖️";
    activateModularAspect(agentMsg, "consensus", "Judge Synthesis", "⚖️");
  } else if (ev.type === "orchestration_round") {
    const goalExcerpt = String(ev.goal || "").slice(0, 80);
    label = `Orchestrator round ${ev.round || "?"}: re-planning remaining scope${goalExcerpt ? `: ${goalExcerpt}` : ""}`;
    icon = "↻";
    activateModularAspect(agentMsg, `orch_${ev.round || 1}`, `Round ${ev.round || 1}`, "↻");
    agentMsg.orchRound = ev.round || (agentMsg.orchRound || 1) + 1;
    agentMsg.stepperTitleText.textContent = `Orchestrator round ${ev.round || "?"}: driving remaining scope...`;
  } else if (ev.type === "orchestration_note") {
    label = ev.note || "";
    icon = "ℹ";
  } else if (ev.type === "paid_consent_required") {
    label = ev.message || "Free tier limits reached. Waiting for paid fallback approval...";
    icon = "⚠️";
    showConsentBanner(currentRunId, ev.message);
  } else if (ev.type === "paid_consent_responded") {
    label = ev.approved ? "Paid fallback approved by operator." : "Paid fallback denied by operator.";
    icon = ev.approved ? "✓" : "✕";
    hideConsentBanner();
  }

  if (label) {
    const item = document.createElement("div");
    item.className = active ? "step-item active" : "step-item done";
    item.innerHTML = `<span class="step-icon">${esc(icon)}</span> <span>${esc(label)}</span>`;
    body.appendChild(item);
    scrollToBottom();
  }
}

function showConsentBanner(runId, message) {
  const banner = $("#consent-banner");
  if (!banner) return;
  const msgEl = $("#consent-msg");
  if (msgEl && message) msgEl.textContent = message;
  banner.hidden = false;

  const btnApprove = $("#btn-consent-approve");
  const btnDeny = $("#btn-consent-deny");

  if (btnApprove) {
    btnApprove.onclick = async () => {
      hideConsentBanner();
      if (!runId) return;
      try {
        await api(`/api/runs/${runId}/consent`, {
          method: "POST",
          body: JSON.stringify({ approved: true })
        });
      } catch (_e) {}
    };
  }

  if (btnDeny) {
    btnDeny.onclick = async () => {
      hideConsentBanner();
      if (!runId) return;
      try {
        await api(`/api/runs/${runId}/consent`, {
          method: "POST",
          body: JSON.stringify({ approved: false })
        });
      } catch (_e) {}
    };
  }
}

function hideConsentBanner() {
  const banner = $("#consent-banner");
  if (banner) banner.hidden = true;
}

// Render Final Response
function renderFinalResult(runRecord, agentMsg) {
  hideConsentBanner();
  agentMsg.spinner.hidden = true;
  agentMsg.stepperTitleText.textContent = runRecord.status === "error"
    ? "Execution stopped" : "Execution complete";

  if (runRecord.status === "cancelled") {
    const notice = runRecord.error || "Execution cancelled by user.";
    agentMsg.body.innerHTML = `<p style="color:var(--yellow);">${esc(notice)}</p>`;
    return;
  }

  const res = runRecord.result || {};
  const responseText = res.response || runRecord.error || "Completed.";

  if (runRecord.status === "error") {
    const kind = String(runRecord.error_kind || "harness_error");
    agentMsg.body.innerHTML = `<p style="color:var(--red);"><strong>${esc(kind)}</strong>: ${esc(responseText)}</p>`;
    if (kind === "provider_spend_limit") {
      const billed = Number(runRecord.known_cost_usd || 0);
      const stopNote = document.createElement("div");
      stopNote.className = "deferred-note";
      stopNote.innerHTML = `<p style="color:var(--yellow); margin:8px 0 0;">
        <strong>Stopped: provider spend limit</strong>${billed > 0 ? `<br><span>Known billed usage: $${billed.toFixed(6)}</span>` : ""}
      </p>`;
      agentMsg.body.appendChild(stopNote);
    }
    return;
  }

  // Render Markdown Body
  agentMsg.body.innerHTML = renderSimpleMarkdown(responseText);
  // An honest deferral is an outcome, not an error: show the reason and the
  // resume path the lane owes the operator.
  if (res.status === "deferred") {
    const deferNote = document.createElement("div");
    deferNote.className = "deferred-note";
    deferNote.innerHTML = `
      <p style="color:var(--yellow); margin:8px 0 0;">
        <strong>Deferred</strong>${res.defer_reason ? ` — ${esc(res.defer_reason)}` : ""}<br>
        <span style="color:var(--fg); opacity:0.8;">${esc(res.next_step || "resume via the plan lane")}</span>
      </p>`;
    agentMsg.body.appendChild(deferNote);
  }

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
  const paidProvenance = res.escalated_model
    ? `<span title="Evidence-bound paid rung used">Escalated: ${esc(res.escalated_model)}`
      + `${res.escalation_family ? ` (${esc(res.escalation_family)})` : ""}</span>`
    : "";
  agentMsg.footer.innerHTML = `
    <span>Model: ${esc(res.model || "Sliding-Scale Multi-Tier")}</span>
    ${paidProvenance}
    ${res.web_used ? `<span title="Live web evidence was retrieved for this answer">🌐 web</span>` : ``}
    <span>Spend: ${fmtCost(cost)}</span>
  `;
  agentMsg.footer.hidden = false;

  // Aspect pipeline completion
  const aspectBar = agentMsg.aspectBar || (agentMsg.card && agentMsg.card.querySelector(".aspect-pipeline-bar"));
  if (aspectBar && !aspectBar.hidden) {
    activateModularAspect(agentMsg, "verdict", res.status === "deferred" ? "Deferred" : "Response", res.status === "deferred" ? "⏸" : "🏁");
    const chips = aspectBar.querySelectorAll(".aspect-chip");
    chips.forEach(c => {
      c.classList.remove("active");
      c.classList.add("done");
    });
  }

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
        <div class="stepper-header" onclick="this.nextElementSibling.nextElementSibling.hidden = !this.nextElementSibling.nextElementSibling.hidden">
          <div class="stepper-title">
            <span class="spinner"></span>
            <span class="stepper-title-text">Starting request...</span>
          </div>
          <span style="font-size:10px; color:var(--dim);">collapse</span>
        </div>
        <div class="aspect-pipeline-bar" hidden></div>
        <div class="stepper-body"></div>
      </div>
      <div class="markdown-body"></div>
      <div class="msg-footer" hidden></div>
    </div>
  `;

  return {
    card: card,
    stepper: card.querySelector(".stepper"),
    aspectBar: card.querySelector(".aspect-pipeline-bar"),
    spinner: card.querySelector(".spinner"),
    stepperTitleText: card.querySelector(".stepper-title-text"),
    stepperBody: card.querySelector(".stepper-body"),
    body: card.querySelector(".markdown-body"),
    footer: card.querySelector(".msg-footer"),
    phaseList: null,
    phaseItems: {},
    orchRound: 1,
    activeRequestAt: null,
    activeRequests: {},
    stopRequested: false,
  };
}

function updateActiveProviderProgress(agentMsg) {
  const active = Object.values(agentMsg.activeRequests || {});
  if (!active.length) {
    agentMsg.activeRequestAt = null;
    return;
  }
  const earliest = Math.min(...active.map((request) => request.startedAt));
  agentMsg.activeRequestAt = earliest;
  const elapsed = Math.max(0, Math.floor((Date.now() - earliest) / 1000));
  const models = [...new Set(active.map((request) => request.model || "provider"))];
  const summary = active.length === 1
    ? `${models[0]} (${active[0].attempt})`
    : `${active.length} provider requests (${models.slice(0, 3).join(", ")}${models.length > 3 ? ", ..." : ""})`;
  if (!agentMsg.stopRequested) {
    agentMsg.stepperTitleText.textContent = `Waiting for ${summary} (${elapsed}s)...`;
  }
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
  $("#btn-stop").disabled = false;
  $("#btn-stop").title = "Stop execution";
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

// Poll routing posture and budget semantics. A configured paid key does not
// mean every request uses a paid model: free is primary when use_free=true,
// and paid models are entered only on an evidence-backed escalation walk.
async function pollRoute() {
  const requestSeq = ++routeRequestSeq;
  try {
    const data = await api("/api/settings");
    if (requestSeq !== routeRequestSeq) return;
    const s = data.settings || {};
    if (typeof s.use_free !== "boolean") {
      throw new Error("routing posture is incomplete");
    }
    backendSettings = s;
    paidEnabled = typeof s.allow_escalation === "boolean"
      ? s.allow_escalation : null;
    updateRouteDisplay();
  } catch (_e) {
    if (requestSeq !== routeRequestSeq) return;
    const text = $("#route-text");
    const badge = $("#route-badge");
    if (text) text.textContent = "Route: unavailable";
    if (badge) badge.title = "Routing posture unavailable";
  }
}


// Poll Spend & Quota
async function pollSpend() {
  try {
    const spend = await api("/api/spend");
    // /api/spend answers with {error} rather than throwing when it cannot
    // build the envelope (no key, unverified key). Treating that as a payload
    // left every counter at $0, so the header read "nothing spent" when the
    // truth was "cannot tell". Say which one it is.
    if (spend && spend.error) {
      markSpendUnavailable(spend.error);
      return;
    }
    const s = spend.session || {};
    $("#spend-val").textContent = fmtCost(s.spent || 0);
    $("#spend-limit").textContent = `/ ${fmtCost(s.ceiling || 0.05)}`;
    markSpendAvailable();
    if (spend.jev) {
      const j = spend.jev;
      const elVal = $("#jev-spend-val");
      const elLim = $("#jev-spend-limit");
      const elMeter = $("#jev-spend-meter");
      if (elVal) elVal.textContent = `Jev: ${fmtCost(j.cost || 0)}`;
      if (elLim) elLim.textContent = `/ $${Number(j.monthly_credit || 5.0).toFixed(2)}`;
      if (elMeter) {
        elMeter.title = `TypeSafe Jev: ${fmtCost(j.cost || 0)} spent of $${Number(j.monthly_credit || 5.0).toFixed(2)} monthly credit (${Number(j.input_tokens || 0).toLocaleString()} tokens, ${(j.used_percent || 0).toFixed(1)}% used, $${Number(j.remaining_credit || 5.0).toFixed(4)} remaining)`;
      }
    }
  } catch (err) {
    markSpendUnavailable((err && err.message) || "spend status unavailable");
  }
}

// A spent of $0 and an unreadable spent both used to render as "$0.0000".
// Show "—" and the reason instead, so the header never claims zero spend it
// cannot actually measure.
function markSpendUnavailable(reason) {
  const val = $("#spend-val");
  const limit = $("#spend-limit");
  const jevVal = $("#jev-spend-val");
  const jevLimit = $("#jev-spend-limit");
  if (val) { val.textContent = "—"; val.title = `Spend unavailable: ${reason}`; }
  if (limit) { limit.textContent = "unavailable"; }
  if (jevVal) { jevVal.textContent = "Jev: —"; jevVal.title = `Jev credit unavailable: ${reason}`; }
  if (jevLimit) { jevLimit.textContent = ""; }
}

function markSpendAvailable() {
  const val = $("#spend-val");
  if (val) val.title = "";
  const jevVal = $("#jev-spend-val");
  if (jevVal) jevVal.title = "";
}

// ---- price-cap popover -----------------------------------------------------
// The run ceiling lives in config (max_cost, hard-capped at 1.00); the
// slider covers the practical chat range 0.01..1.00 and the text input
// allows exact values, which the server validates fail-closed.
const CAP_SLIDER_MIN = 0.01;
const CAP_SLIDER_MAX = 1.00;

function openCapPopover() {
  const pop = $("#cap-popover");
  const current = (backendSettings && backendSettings.max_cost) || 0.05;
  $("#cap-text").value = current.toFixed(2);
  $("#cap-slider").max = String(CAP_SLIDER_MAX);
  $("#cap-slider").min = String(CAP_SLIDER_MIN);
  $("#cap-slider").step = "0.01";
  $("#cap-slider").value = String(Math.min(
    CAP_SLIDER_MAX, Math.max(CAP_SLIDER_MIN, current)));
  $("#cap-slider-val").textContent = fmtCost(current);
  $("#cap-hard-max").textContent = fmtCost(CAP_SLIDER_MAX);
  $("#cap-error").textContent = "";
  $("#cap-error").hidden = true;
  pop.hidden = false;
  $("#cap-text").focus();
  $("#cap-text").select();
}

function closeCapPopover() {
  $("#cap-popover").hidden = true;
}

async function saveCap(raw) {
  const errEl = $("#cap-error");
  errEl.hidden = true;
  const v = Number(raw);
  if (!Number.isFinite(v) || v <= 0) {
    errEl.textContent = "Enter a positive dollar amount";
    errEl.hidden = false;
    return;
  }
  try {
    const data = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ max_cost: v }),
    });
    backendSettings = data.settings || backendSettings;
    if (backendSettings && typeof backendSettings.max_cost === "number") {
      $("#spend-limit").textContent = `/ ${fmtCost(backendSettings.max_cost)}`;
    }
    closeCapPopover();
  } catch (e) {
    errEl.textContent = e.message;
    errEl.hidden = false;
  }
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
