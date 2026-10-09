const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

test("Stop clicked before /api/chat returns is sent once the run ID arrives", async () => {
  const handlers = new Map();
  const makeElement = () => ({
    hidden: false,
    disabled: false,
    title: "",
    value: "",
    style: {},
    listeners: {},
    addEventListener(type, fn) { this.listeners[type] = fn; },
    appendChild() {},
    focus() {},
  });
  const elements = new Map([
    ["#welcome-hero", makeElement()],
    ["#prompt-input", makeElement()],
    ["#chat-form", makeElement()],
    ["#btn-stop", makeElement()],
    ["#chat-feed", makeElement()],
  ]);
  const storage = new Map();
  const localStorage = {
    getItem(key) { return storage.get(key) ?? null; },
    setItem(key, value) { storage.set(key, String(value)); },
    removeItem(key) { storage.delete(key); },
  };
  const window = { addEventListener(type, fn) { handlers.set(type, fn); } };
  const document = {
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, makeElement());
      return elements.get(selector);
    },
    querySelectorAll() { return []; },
    getElementById(id) { return this.querySelector(`#${id}`); },
    createElement() { return makeElement(); },
  };
  const context = vm.createContext({ window, document, localStorage, console,
    setInterval() { return 1; }, clearInterval() {} });
  const appPath = path.join(__dirname, "..", "harness", "ui", "app.js");
  const app = fs.readFileSync(appPath, "utf8");
  vm.runInContext(app + `
    window.__test = {
      setup: setupInputHandlers,
      submit: submitPrompt,
      setApi(fn) { api = fn; },
      setHooks(hooks) {
        appendUserMessage = hooks.appendUserMessage;
        createAgentMessageCard = hooks.createAgentMessageCard;
        scrollToBottom = hooks.scrollToBottom;
        setInFlight = hooks.setInFlight;
        if (hooks.pollExecution) pollExecution = hooks.pollExecution;
      },
      poll: pollExecution,
      cancel: requestRunCancellation,
      setRunState(runId, agentMsg, generation) {
        currentRunId = runId;
        activeAgentMessage = agentMsg;
        runGeneration = generation;
      },
      state() { return { currentRunId, activeAgentMessage }; },
    };
  `, context);

  const calls = [];
  let resolveChat;
  window.__test.setApi((url) => {
    calls.push(url);
    if (url === "/api/chat") return new Promise((resolve) => { resolveChat = resolve; });
    if (url.endsWith("/cancel")) return Promise.resolve({ cancel_requested: true });
    throw new Error(`unexpected API request ${url}`);
  });
  const card = {
    card: {}, stepper: {}, body: {}, stepperBody: {},
    stepperTitleText: { textContent: "" }, activeRequests: {},
  };
  window.__test.setHooks({
    appendUserMessage() {},
    createAgentMessageCard() { return card; },
    scrollToBottom() {},
    setInFlight() {},
    pollExecution(runId) { calls.push(`poll:${runId}`); },
  });
  window.__test.setup();

  const submit = window.__test.submit("simple prompt");
  assert.deepEqual(calls, ["/api/chat"]);
  await elements.get("#btn-stop").listeners.click();
  assert.equal(window.__test.state().activeAgentMessage.stopRequested, true);
  assert.deepEqual(calls, ["/api/chat"], "stop is queued before the run ID exists");

  resolveChat({ id: "run-42" });
  await submit;
  assert.deepEqual(calls, ["/api/chat", "/api/runs/run-42/cancel", "poll:run-42"]);
});

test("polls never overlap and late run-A callbacks cannot touch run B", async () => {
  const makeElement = () => ({
    hidden: false, disabled: false, title: "", value: "", style: {},
    addEventListener() {}, appendChild() {}, focus() {},
  });
  const elements = new Map();
  const document = {
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, makeElement());
      return elements.get(selector);
    },
    querySelectorAll() { return []; },
    getElementById(id) { return this.querySelector(`#${id}`); },
    createElement() { return makeElement(); },
  };
  const timers = new Map();
  let nextTimer = 0;
  const intervals = [];
  const window = { addEventListener() {} };
  const context = vm.createContext({
    window, document, localStorage: { getItem() { return null; }, setItem() {} },
    console,
    setTimeout(fn) { const id = `timer-${++nextTimer}`; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(fn) { intervals.push(fn); return `interval-${intervals.length}`; },
    clearInterval() {},
  });
  const appPath = path.join(__dirname, "..", "harness", "ui", "app.js");
  vm.runInContext(fs.readFileSync(appPath, "utf8") + `
    handleLiveEvent = () => {};
    pollSpend = () => {};
    renderFinalResult = () => {};
    refreshSessionList = () => {};
    setInFlight = () => {};
    window.__test = {
      poll: pollExecution,
      cancel: requestRunCancellation,
      setApi(fn) { api = fn; },
      setRunState(runId, agentMsg, generation) {
        currentRunId = runId;
        activeAgentMessage = agentMsg;
        runGeneration = generation;
      },
      state() { return { currentRunId, activeAgentMessage }; },
    };
  `, context);

  let resolveEventsA;
  let resolveResultA;
  let resolveCancelA;
  let resolveEventsB;
  let resolveResultB;
  const urls = [];
  window.__test.setApi((url) => {
    urls.push(url);
    if (url === "/api/runs/A/events?after=0") return new Promise((resolve) => { resolveEventsA = resolve; });
    if (url === "/api/runs/A/result") return new Promise((resolve) => { resolveResultA = resolve; });
    if (url === "/api/runs/A/cancel") return new Promise((resolve) => { resolveCancelA = resolve; });
    if (url === "/api/runs/B/events?after=0") return new Promise((resolve) => { resolveEventsB = resolve; });
    if (url === "/api/runs/B/result") return new Promise((resolve) => { resolveResultB = resolve; });
    throw new Error(`unexpected API request ${url}`);
  });
  const messageA = { stopRequested: false, activeRequests: {}, activeRequestAt: null,
    stepperTitleText: { textContent: "" } };
  const messageB = { stopRequested: false, activeRequests: {}, activeRequestAt: null,
    stepperTitleText: { textContent: "" } };

  window.__test.setRunState("A", messageA, 1);
  window.__test.poll("A", messageA, 1);
  const lateCancel = window.__test.cancel("A", messageA);
  assert.deepEqual(urls, ["/api/runs/A/events?after=0", "/api/runs/A/cancel"]);
  assert.equal(intervals.length, 0, "polling must not use overlapping interval ticks");
  assert.equal(timers.size, 0, "the next poll waits for the active request to finish");

  resolveEventsA({ events: [] });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(urls.at(-1), "/api/runs/A/result");
  resolveResultA({ status: "completed" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(window.__test.state().currentRunId, null);

  window.__test.setRunState("B", messageB, 2);
  window.__test.poll("B", messageB, 2);
  resolveCancelA({ cancel_requested: true });
  await lateCancel;
  resolveEventsB({ events: [] });
  await new Promise((resolve) => setImmediate(resolve));
  resolveResultB({ status: "running" });
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(window.__test.state().currentRunId, "B");
  assert.equal(window.__test.state().activeAgentMessage, messageB);
  assert.equal(messageB.stopRequested, false);
  assert.equal(timers.size, 1, "run B keeps its own scheduled poll");
});
