/**
 * Proof Bench worker (SITE-5): ONE Cloudflare Worker with Static Assets.
 *
 * Serves the static site + three API endpoints:
 *   POST /api/submit    — validated, consent-checked, deduped bundle intake (D1 + KV fold)
 *   GET  /api/aggregate — the rolled-up aggregate (KV; snapshot fallback)
 *   POST /api/route     — Jev-seat proxy (rate-limited, hash-cached, budget-capped)
 *
 * Contract mirror: validation here mirrors harness/site_export.py's fail-closed
 * rules (schema, consent, chain claim, size); the rollup fold mirrors
 * harness/site_aggregate.fold_rollup — parity is pinned by shared fixture
 * vectors in tests/fixtures/site_fold_vectors.json so the two sides cannot drift.
 * Raw query text is never persisted (route responses cache by sha256(goal+pack)).
 */
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/api/submit" && request.method === "POST") {
      return handleSubmit(request, env);
    }
    if (url.pathname === "/api/aggregate" && request.method === "GET") {
      return handleAggregate(env);
    }
    if (url.pathname === "/api/route" && request.method === "POST") {
      return handleRoute(request, env);
    }
    return env.ASSETS.fetch(request);
  },
};

const MAX_BODY = 512 * 1024;
const BUNDLE_SCHEMA = "site-bundle-v1";
const CONSENT_SCHEMA = "site-consent-v1";

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

// ---- bundle-v1 validation (mirror of the Python side's essential gates) ----

export function validateBundle(bundle) {
  if (!bundle || typeof bundle !== "object" || Array.isArray(bundle)) {
    return "bundle must be a JSON object";
  }
  if (bundle.schema !== BUNDLE_SCHEMA) {
    return `schema must be ${BUNDLE_SCHEMA}`;
  }
  if (typeof bundle.bundle_id !== "string" || !/^[0-9a-f]{16}$/.test(bundle.bundle_id)) {
    return "bundle_id must be 16 hex chars";
  }
  const consent = bundle.consent;
  if (!consent || consent.schema !== CONSENT_SCHEMA ||
      typeof consent.accepted_at !== "string" || !consent.accepted_at ||
      typeof consent.surface !== "string" || !consent.surface) {
    return `consent block missing or not ${CONSENT_SCHEMA}`;
  }
  const chain = bundle.chain;
  if (!chain || chain.verified_claim !== true ||
      typeof chain.head_hash !== "string" || !chain.head_hash) {
    return "chain claim missing (verified_claim + head_hash required)";
  }
  if (!Array.isArray(bundle.runs)) return "runs must be an array";
  if (bundle.runs.length > 5000) return "too many runs";
  for (const run of bundle.runs) {
    const bad = validateRun(run);
    if (bad) return `run invalid: ${bad}`;
  }
  // Credential shapes refuse the whole bundle (same denylist as export).
  const blob = JSON.stringify(bundle);
  if (/sk-or-v1-[0-9a-zA-Z]{16,}/.test(blob) ||
      /apikey_[0-9a-zA-Z]{8,}/.test(blob) ||
      /-----BEGIN [A-Z ]*PRIVATE KEY-----/.test(blob)) {
    return "credential-shaped content refused";
  }
  return null;
}

function validateRun(run) {
  if (!run || typeof run !== "object") return "not an object";
  for (const key of ["run_id", "task_ref", "lane", "outcome"]) {
    if (typeof run[key] !== "string" || !run[key]) return `${key} must be a non-empty string`;
  }
  if (!["pass", "fail", "deferred", "aborted"].includes(run.outcome)) {
    return "outcome outside vocabulary";
  }
  if (typeof run.cost !== "number" || run.cost < 0 || !Number.isFinite(run.cost)) {
    return "cost must be a finite non-negative number";
  }
  if (typeof run.gated !== "boolean") return "gated must be boolean";
  if (run.escalation !== undefined && run.escalation !== null &&
      typeof run.escalation !== "object") return "escalation must be object";
  return null;
}

// ---- rollup fold (parity: harness/site_aggregate.fold_rollup) ----

export function foldRollup(previous, bundle) {
  const rollup = previous && typeof previous === "object" ? previous : {};
  const bundles = new Set(rollup.bundles || []);
  bundles.add(bundle.bundle_id);
  const depth = { ...(rollup.depth || {}) };
  let totalRuns = rollup.total_runs || 0;
  let totalCost = rollup.total_cost || 0;
  for (const run of bundle.runs || []) {
    totalRuns += 1;
    totalCost += run.cost || 0;
    const tier = typeof run.deepest_tier_reached === "string" &&
      /^T[0-3]$/.test(run.deepest_tier_reached) ? run.deepest_tier_reached : "unknown";
    depth[tier] = (depth[tier] || 0) + 1;
  }
  return {
    schema: "site-rollup-v1",
    contributors: bundles.size,
    bundles: [...bundles].sort(),
    total_runs: totalRuns,
    total_cost: Math.round(totalCost * 1e9) / 1e9,
    depth,
  };
}

async function handleSubmit(request, env) {
  const { success } = await env.SUBMIT_LIMIT.limit({ key: ipKey(request) });
  if (!success) return json({ error: "rate limited" }, 429);
  const raw = await request.text();
  if (raw.length > MAX_BODY) return json({ error: "bundle exceeds 512KB" }, 413);
  let bundle;
  try { bundle = JSON.parse(raw); } catch { return json({ error: "invalid JSON" }, 400); }
  const problem = validateBundle(bundle);
  if (problem) return json({ error: problem }, 422);
  const dedupeKey = `bundle:${bundle.bundle_id}`;
  if (await env.ROLLUP.get(dedupeKey)) {
    return json({ status: "duplicate", bundle_id: bundle.bundle_id });
  }
  await env.DB.prepare(
    "INSERT INTO bundles (bundle_id, received_at, payload) VALUES (?, ?, ?)")
    .bind(bundle.bundle_id, new Date().toISOString(), raw).run();
  await env.ROLLUP.put(dedupeKey, "1", { expirationTtl: 60 * 60 * 24 * 30 });
  const rollup = foldRollup(
    await env.ROLLUP.get("rollup", "json"), bundle);
  await env.ROLLUP.put("rollup", JSON.stringify(rollup));
  return json({ status: "accepted", bundle_id: bundle.bundle_id,
                contributors: rollup.contributors });
}

async function handleAggregate(env) {
  const rollup = await env.ROLLUP.get("rollup", "json");
  if (!rollup) return json({ schema: "site-rollup-v1", contributors: 0,
                             bundles: [], total_runs: 0, total_cost: 0,
                             depth: {} });
  return json(rollup);
}

// ---- route proxy: Jev-only, hash-cached, budget-capped ----

const TIER_TAGS = {
  T3: ["architecture", "cross-module-protocol", "security-review", "novel-proof"],
  T2: ["concurrency", "race", "deadlock", "mutex", "thread", "invariant",
       "protocol", "performance", "migration"],
  T0: ["typo", "rename", "format", "docstring", "style"],
  T1: ["implement", "fix", "test", "refactor", "parse", "validate"],
};

// Parity with harness/route_pack.tier_floor_for_goal (scan order matters:
// high tiers first, then mechanical T0 before generic-verb T1).
export function tierFloorForGoal(goal) {
  const text = String(goal || "").toLowerCase();
  if (!text) return "T0";
  for (const tier of ["T3", "T2", "T0"]) {
    if (TIER_TAGS[tier].some(tag => text.includes(tag))) return tier;
  }
  if (TIER_TAGS.T1.some(tag => text.includes(tag))) return "T1";
  return "T0";
}

function chooseRung(pack, tier) {
  const order = { T0: 0, T1: 1, T2: 2, T3: 3 };
  const floor = order[tier] ?? 4;
  for (const rung of pack.rungs || []) {
    if ((order[rung.tier] ?? 4) >= floor) return rung.rung_id;
  }
  return null;
}

function ipKey(request) {
  return request.headers.get("cf-connecting-ip") || "anonymous";
}

async function withinDailyBudget(env, cap) {
  const today = new Date().toISOString().slice(0, 10);
  const key = `route-budget:${today}`;
  const spent = Number(await env.ROLLUP.get(key) || 0);
  if (spent >= cap) return false;
  await env.ROLLUP.put(key, String(spent + 1), { expirationTtl: 60 * 60 * 48 });
  return true;
}

async function handleRoute(request, env) {
  const { success } = await env.ROUTE_LIMIT.limit({ key: ipKey(request) });
  if (!success) return json({ error: "rate limited" }, 429);
  let body;
  try { body = await request.json(); } catch { return json({ error: "invalid JSON" }, 400); }
  const goal = String(body.goal || "").trim();
  const pack = body.pack;
  if (!goal || !pack || !Array.isArray(pack.rungs) || !pack.rungs.length) {
    return json({ error: "goal and a non-empty rungs pack are required" }, 422);
  }
  if (goal.length > 10000) return json({ error: "goal too long" }, 422);

  const cacheKey = `route:${await sha256hex(goal + "|" + JSON.stringify(pack))}`;
  const cached = await env.ROLLUP.get(cacheKey, "json");
  if (cached) return json(cached);

  if (!await withinDailyBudget(env, Number(env.ROUTE_DAILY_BUDGET || 200))) {
    return fallbackEnvelope(goal, pack,
      "daily route budget exhausted; deterministic heuristic served");
  }

  let envelope = null;
  if (env.JEV_ENDPOINT && env.JEV_KEY) {
    try {
      const res = await fetch(env.JEV_ENDPOINT, {
        method: "POST",
        headers: { "authorization": `Bearer ${env.JEV_KEY}`,
                   "content-type": "application/json" },
        body: JSON.stringify({
          model: env.JEV_MODEL || "jev-latest",
          messages: [{ role: "user", content: routePrompt(goal, pack) }],
        }),
      });
      if (res.ok) {
        const data = await res.json();
        envelope = parseJevChoice(data, pack, goal);
      }
    } catch { /* fall through to deterministic fallback */ }
  }
  if (!envelope) {
    envelope = fallbackEnvelope(goal, pack, "Jev seat unavailable");
  }
  await env.ROLLUP.put(cacheKey, JSON.stringify(envelope), { expirationTtl: 86400 });
  return json(envelope);
}

function routePrompt(goal, pack) {
  const criteria = pack.rungs.map(r =>
    `- ${r.rung_id}: tier ${r.tier}, ${r.cost_class} cost`).join("\n");
  return (
    "Choose the cheapest capable rung for the request. Answer with JSON " +
    `{"choice": "<rung_id>"} and nothing else. Declared rungs (only these):\n` +
    `${criteria}\n\nRequest: ${goal}`);
}

function parseJevChoice(data, pack, goal) {
  try {
    const content = data?.choices?.[0]?.message?.content || "";
    const match = content.match(/"choice"\s*:\s*"([^"]+)"/);
    const declared = new Set(pack.rungs.map(r => r.rung_id));
    if (match && declared.has(match[1])) {
      const rung = pack.rungs.find(r => r.rung_id === match[1]);
      return { status: "ok", route: { rung_id: rung.rung_id, tier: rung.tier,
        cost_class: rung.cost_class, guidance: rung.guidance || [] },
        is_fallback: false, reasons: ["Jev choice from declared ladder"] };
    }
    if (match) {
      return fallbackEnvelope(goal, pack,
        `out-of-ladder choice refused: ${match[1]}`);
    }
  } catch { /* fall through */ }
  return null;
}

function fallbackEnvelope(goal, pack, reason) {
  const tier = tierFloorForGoal(goal);
  const rungId = chooseRung(pack, tier);
  return {
    status: rungId ? "ok" : "unroutable",
    route: rungId
      ? (() => {
          const rung = pack.rungs.find(r => r.rung_id === rungId);
          return { rung_id: rung.rung_id, tier: rung.tier,
                   cost_class: rung.cost_class, guidance: rung.guidance || [] };
        })()
      : { rung_id: null, tier, cost_class: null, guidance: [] },
    is_fallback: true,
    reasons: [reason, `heuristic tier floor ${tier}`],
  };
}

async function sha256hex(text) {
  const digest = await crypto.subtle.digest(
    "SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)]
    .map(b => b.toString(16).padStart(2, "0")).join("");
}
