# Naming Audit — FINDINGS

Repo: `Harness` (`sovereign-harness`), branch `freebuff/mission-b2d7eb8e-…`, audited 2026-09-29.
Read-only audit. No source file was modified.

Severity scale: **High** = public API or core domain · **Medium** = internal but widely used ·
**Low** = local / cosmetic.

Counts are `grep -row <token> --include='*.py' harness/ scripts/ audits/ site/` unless stated.

---

# Persisted & public surface — risk register

Every rename below is scored against the four risk dimensions the brief names: **public API**
(CLI, HTTP, MCP), **serialized format** (ledger JSON, site-export JSON), **database schema**,
and **URLs**. This register is the evidence base for those risk ratings; individual findings
reference it rather than re-deriving it.

## Database schema — two SQLite databases exist

Neither is created by `harness/`. Both are written by code outside the main package's import
graph, which is exactly why a rename can be "obviously safe" in Python and still break a
migration.

**DB-1 · `.harness/oc_handoff/state.sqlite3`** — DDL defined at
`examples/oc_handoff/worker.py:53-78` (`DB_SCHEMA`); read by `harness/jev_completion.py:512-541`.

```sql
CREATE TABLE IF NOT EXISTS tasks (          -- worker.py:54-70
    task_id TEXT PRIMARY KEY,   nonce TEXT NOT NULL UNIQUE,
    manifest_sha256 TEXT NOT NULL UNIQUE,   repo_sha TEXT NOT NULL,
    state TEXT NOT NULL,         -- worker.py:59   <-- PERSISTED COLUMN
    phase TEXT NOT NULL,         -- worker.py:60   <-- PERSISTED COLUMN
    worktree_path TEXT,  branch TEXT,  expected_sha256 TEXT,  jev_json TEXT,
    commit_sha TEXT,  receipt_json TEXT,  error_code TEXT,
    created_at INTEGER NOT NULL,  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (         -- worker.py:71-77
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    receipt_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending', -- worker.py:74   <-- PERSISTED COLUMN
    created_at INTEGER NOT NULL,  delivered_at INTEGER
);
```

**Exhaustive value enumeration over `worker.py`** — these are every literal value either column is
ever assigned or compared against in that file, not a sample:

- `state ∈ {processing, complete, uncertain, failed, pending, delivered}`
  — `processing` (675, 708), `complete` (636, 714), `uncertain` (699, 708), `failed` (781, 799,
  806, 814, 821, 832), `pending` (916, and the `outbox` column default at 74), `delivered` (936).
- `phase ∈ {committing, writing, evaluating, complete, failed}`
  — `committing` (859; also compared in Python at 680), `writing` (841), `evaluating` (787),
  `complete` (636), `failed` (781, 799, 806, 814, 821, 832).

**Schema ownership.** `harness/jev_completion.py` opens this database **read-only** —
`db_uri = "file:…?mode=ro"` (`:529`) — and issues **no DDL of its own** (zero `CREATE TABLE`
statements in the module). The schema is owned entirely by the handoff worker. That is worth
stating plainly because `examples/oc_handoff/worker.py` is a file the original audit pass
**deliberately excluded** as an out-of-scope experimental OC handoff lane (see `SUMMARY.md` §1);
the schema only became visible on a second pass. A reader auditing only `harness/` would find the
`state` column exists and no code in that package that defines or migrates it.

The reader at `jev_completion.py:532-535` joins the two tables and filters
`WHERE t.state='complete' AND t.phase='complete'`.

**DB-2 · Proof Bench bundle store (SITE-5)** — `site/worker/migrations/0001_init.sql:3-7`:
`bundles(bundle_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, payload TEXT NOT NULL)`.
**No column name overlaps any concept in this report.** Listed for completeness.

## Per-concept verdict — what is physically persisted

| Concept (finding) | Physically persisted? | Unmovable without a migration? |
|---|---|---|
| `state` — Jev subject (L1-1) | **Yes** — `tasks.state`, `outbox.state` | The **columns**. The Python *parameters* are not persisted and are safe to rename. |
| `state` — pyramid / apply / continuation / resume (L1-5) | No — parameter names only | No |
| `state` — HTTP request body (see below) | **Yes** — public JSON key | Yes |
| `phase` | **Yes** — `tasks.phase` | Yes. Not a rename target in this report; listed so the coupling is visible. |
| `tier` (L1-2, V-3) | No | No |
| `lane` / `rung` / `pool` (V-4) | No | No |
| `budget` / `ceiling` (V-2) | No | No |
| `site` — Jev lane (L1-4) | **Yes** — ledger JSON `structural.site`; also `/api/site/demo-snapshot` in a URL | Yes |
| cost limit `max_cost` (L1-6, V-6) | **Yes** — HTTP param + published MCP schema field | Yes |

**Consequence for L1-1 and L1-5:** the recommended renames are *parameter* renames
(`issue`, `log_item`, `repo_element`, `route_query`, `phase_evidence`; `dag_state`, `apply_run`,
`continuation_state`, `resume_state`). None of them touches a column. **No migration is implied
by any finding in this report** — but a reader who greps for `state` and renames it wholesale
would break DB-1, and that is the specific mistake this register exists to prevent.

## Public API — JSON keys and published MCP schema

| Key | Where | Note |
|---|---|---|
| `max_cost` | `server.py:217`, `:218`, `:286`; `mcp_schemas.py:165` | HTTP body param **and** published MCP field (`{"type": "number", "minimum": 0, "maximum": 0.25}`) |
| `state` | `server.py:221`, `:294` | **means a filesystem path**, not a run state — `open(args["state"], encoding="utf-8")` |
| `phase` | `server.py:997`; `mcp_schemas.py:273`, `:280` | `{"required": ["phase"]}` — a required public MCP field |

The `state` key at `server.py:294` is a **seventh concept** for the word `state`, and the only one
that is a public API key. It is the one place a `state` rename would be a breaking change.

## URLs — 23 `/api/*` routes

`/api/capabilities`, `/api/chat`, `/api/chat/history`, `/api/chat/session/delete`,
`/api/chat/sessions`, `/api/cost`, `/api/events`, `/api/jev-phase`, `/api/ledger/defer-stats`,
`/api/ledger/report`, `/api/ledger/tail`, `/api/ledger/verify`, `/api/missions`, `/api/models`,
`/api/rankings`, `/api/route`, `/api/runs`, `/api/settings`, `/api/site/demo-snapshot`,
`/api/snapshot`, `/api/spend`, `/api/status`, `/api/trust`. (Plus the static mount `/site/`.)

**Checked directly: none of the tokens this report recommends renaming appears in any route
path.** Verified for `tier`, `budget`, `ceiling`, `run_budget`, `run_ceiling`, `state`, `req`,
`args`, `kwargs` — all zero. **No recommended rename reaches a URL.**

Three route tokens *are* coupled to names this report reasons about, and must not move:
- `/api/ledger/verify` carries `verify` — the lane and CLI name V-2 explicitly protects.
- `/api/jev-phase` carries `phase` — the same word as the DB-1 column, so a phase rename would
  be a URL change *and* a migration.
- `/api/site/demo-snapshot` carries `site` **meaning the website** — a fourth independent
  confirmation, on public surface, that `site` is overloaded (L1-4).

---

# Layer 1 — Nameability

*Can one sentence describe what this holds/does, without "or" and without "sometimes"? Where no,
record a finding.*

---

## L1-1 — `state` in `JevPolicy` is seven different subjects under one name

**Severity: High** · **Blast radius: 574 occurrences of `state` repo-wide; 6 public methods of the
core judging policy; `harness.jev_policy` is imported by 26 files.**

| Location | Name |
|---|---|
| `harness/jev_policy.py:1547` | `evaluate_issue_sort(self, state, pack, …)` |
| `harness/jev_policy.py:1703` | `evaluate_log_item(self, state, pack, …)` |
| `harness/jev_policy.py:1874` | `evaluate_repo_summary(self, state, pack, …)` |
| `harness/jev_policy.py:2044` | `evaluate_model_route(self, state, pack, …)` |
| `harness/jev_policy.py:2185` | `evaluate_phase_completion(self, state, pack, …)` |
| helpers `:1329 _scope_state_facts`, `:1493 _issue_sort_text`, `:1663 _log_item_text`, `:1831 _repo_element_text`, `:2032 _route_query_text`, `:2153 _completion_state_text` | all `(state: Any)` |

**Evidence — the accepted shapes differ per call site, and so do the key probes:**

```
jev_policy.py:1493  _issue_sort_text:  for key in ("issue", "text", "prompt", "note", "reason")
jev_policy.py:1663  _log_item_text:    for key in ("item", "text", "issue", "reason", "note")
jev_policy.py:2032  _route_query_text: for key in ("goal", "query", "prompt", "issue", "text")
jev_policy.py:1831  _repo_element_text: state.get("path"|"kind"|"summary"|"symbols"|"headings"
                                                |"element_kind"|"symbol"|"module_summary")
jev_policy.py:2153  _completion_state_text: state.get("phase"|"status_row"|"open_blockers"
                                                |"tests_missing"|"pr_merged"|"local_gates_green"
                                                |"ci_green"|"notes")
jev_policy.py:1329  _scope_state_facts: state.get("mission_id"|"request"|"success_definition"
                                                |"scope"|"verifier_holds")
```

Every helper additionally begins with `if isinstance(state, str): return state` and ends with
`return "" if state is None else str(state)`. So the parameter is **`str` or `dict` (of six
different schemas) or `None`**, annotated `Any`.

**This is not only a style problem — it is a silent-corruption hazard.** The probe lists are
ordered and they differ, so the *same dict* yields *different text* depending on which method you
called. Take `{"issue": "a", "text": "b"}`:

- `evaluate_issue_sort` probes `("issue", …)` → `"a"`
- `evaluate_log_item` probes `("item", "text", …)` → `"b"`  ← different answer
- `evaluate_model_route` probes `("goal", "query", "prompt", "issue", "text")` → `"a"`

Reproduced directly against the package:

```
INPUT dict = {'issue': 'a', 'text': 'b'}
  JevPolicy._issue_sort_text(d)    -> 'a'
  JevPolicy._log_item_text(d)      -> 'b'
  JevPolicy._route_query_text(d)   -> 'a'
  JevPolicy._repo_element_text(d)  -> ''
  JevPolicy._completion_state_text(d) -> 'pr_merged=None local_gates_green=None ci_green=None'
```

No caller is told which convention applies, and nothing validates it. `evaluate_phase_completion`
compounds it: at `:2261` it *separately* reaches past the helper and does
`state.get("phase") if isinstance(state, dict) else None`, so `state` is both the text source and
a field container.

**Note the repo already has the right precedent in the same class**: `evaluate_scope(self,
mission_state, …)` at `:1356` names the subject. That one name is correct; the other five are not.

**Remediation (design, not cosmetics).** Narrow the type per method and name the subject:
- `evaluate_issue_sort(issue: IssueSortInput)` — accept `str` **or** a single documented TypedDict;
  drop the five-key probe.
- `evaluate_log_item(log_item: LogItemDict)` — a `log_items.LogItem` shape, not free dict.
- `evaluate_repo_summary(element: RepoElementDict)` — the shape `repo_items` already produces.
- `evaluate_model_route(query: RouteQuery)` — `str` or `{"goal"|"query"}`; pick one.
- `evaluate_phase_completion(phase_evidence: PhaseEvidenceDict)` — the `phase/status_row/…` shape.
If the `str` shorthand must stay for ergonomics, keep it but funnel through **one** shared
`_subject_text(value, keys)` helper and **require the caller to pass the key set**, so the
divergence is at least explicit and unit-testable. Preferred: add a `subject_kind: Literal[...]`
or make each method take exactly one typed parameter.

---

## L1-2 — `tier` is an `int` in one module and a `str` in another, and a third thing in the same class

**Severity: High** · **Blast radius: 305 occurrences of `tier`; `harness.router` imported by 10 files.**

Three unrelated namespaces share the word:

**(a) Price tier — `str`, module `harness/routing_table.py:10-13`**
```python
TIER_0 = "T0"  # Free models ($0)
TIER_1 = "T1"  # Ultra-cheap (:floor, $0.05-$0.30/M)
TIER_2 = "T2"  # Mid-tier reasoning ($0.50-$3.00/M)
TIER_3 = "T3"  # Frontier specialist ($2.00-$10.00/M)
```
Also `:108 classify_task_tier(...) -> str`, `:135 next_tier(current_tier: str) -> Optional[str]`,
`:146 get_tier_route(tier: str, …)`, `:160 classify_model_tier(model_id) -> str`.

**(b) Complexity tier — `int`, module `harness/sliding_scale.py:33-35`**
```python
TIER_0_SCOUT = 0
TIER_1_DISTILLER = 1
TIER_2_FRONTIER = 2
```
Also `:119 classify_task_tier(…)`, `:243 resolve_tier_recommended_model(tier: int, …)`,
`:259 tier_model_ladder(tier: int, …)`, `:329 tier_cost_ceiling(tier: int, …)`.

**(c) Execution lane — `str`, module `harness/router.py`, *same class, two methods***
```python
router.py:59   def route_tier(self, tier: int):  …  return {"tier": tier, …}          # int 0/1/2
router.py:100  def route(self, task_type):       …  return {"tier": "panel", …}      # a lane name
                                                             {"tier": "apply", …}
```
`route()`'s own docstring says *"Return the spec for a task type: cheap **lane** first."* — the
docstring calls it a lane; the key calls it a tier.

A caller reading `spec["tier"]` from `Router` gets **an int, the string `"panel"`, or the string
`"apply"` depending on which method produced it.**

**Remediation.** These are three different concepts; give each its own word:
- price tier → **`price_band`** / `PRICE_T0..T3` (its own docstring at `routing_table.py:47` already
  calls them "cost band labels" and `TIER_COST_BANDS` exists).
- complexity tier → **`complexity_tier`** (already the term used at `sliding_scale.py` internals,
  27 occurrences of `complexity_tier`) — just apply it to the public params.
- `Router.route()`'s key → **`lane`**, matching `effective_lane_policy`, `call_lane`, and the
  `"panel"`/`"apply"` values, which are lanes.

---

## L1-3 — `classify_task_tier` is defined twice with incompatible contracts; the second is dead

**Severity: High** · **Blast radius: name collision across two modules; 5 direct references.**

| | `harness/sliding_scale.py:119` | `harness/routing_table.py:108` |
|---|---|---|
| Signature | `classify_task_tier(instruction, target_files, diff_size, dependency_depth, is_leaf, previous_failures, use_free, custom_frontier, jev_route)` | `classify_task_tier(prompt: str, target_files=None)` |
| Returns | classification object whose `.tier` is **int 0/1/2** | **`str` `"T0"`–`"T3"`** |
| Production callers | `router.py:83`, `sliding_scale.py:354` | **none** |

Verified dead-code check — the only references to the `routing_table` version are
`tests/test_routing_table.py:51-66` and `:75-83`. The same for `get_tier_route` (only
`tests/test_routing_table.py:76,83`). By contrast `floor_model` and `strip_variant_suffix` from
that module *are* live (`chat.py:15`, `ledger_analytics.py:424`, `site_export.py:202`).

So the *name* `classify_task_tier` is owned by the live sliding-scale version; the price-tier
version is a second, unreferenced definition that a reader cannot distinguish without checking
every import.

**Remediation.** Delete `routing_table.classify_task_tier`, `next_tier`, and `get_tier_route`
(or rename to `classify_price_band` / `next_price_band` / `price_band_route` and keep them
intentionally). If they are kept, add a module docstring line stating that the price-tier ladder
is *not* the live classifier. Deleting is the smaller change in practice — it removes a
dead 52-line block (`routing_table.py:108-159`: `classify_task_tier` at `:108`, `next_tier` at
`:135`, and `get_tier_route` at `:146-157`) and its test file.

---

## L1-4 — `site` means three unrelated things

**Severity: High** · **Blast radius: 314 occurrences; 15 distinct `site="…"` literals; 10 `SITE_*` constants.**

1. **A Jev evaluation lane.** `evaluate_model_route(self, state, pack, *, site: str = ROUTE_QUERY_SITE)`
   (`jev_policy.py:2044`). Docstrings: `structural.site=log_factor`, `=repo_summary`,
   `=model_route`, `=phase_completion`, `=issue_sort`, `=audit_dimensions`.
2. **The public website.** `server.py:79 SITE_ROOT = … "site", "public"`; modules
   `site_export.py`, `site_aggregate.py`; CLI command `harness site-export`.
3. **A roadmap slice ID.** `site_export.py:1` — *"sanitized evidence bundles for the Proof Bench
   site (SITE-1)"*; `route_pack.py:1` — *"(SITE-2)"*.

A reader who sees `site=` in `jev_policy` and `SITE_ROOT` in `server` has no way to know these are
unrelated. Worse, meaning (1) is **persisted**: `structural.site=…` is written into ledger
`jev_eval` events and flows out through site export.

**Remediation.** Rename the Jev parameter to **`judgment_kind`** (or `eval_kind`) across
`JevPolicy.evaluate_*`; reserve `site` for the website. Per the risk register, this is a
**parameter-only** change: the ledger key `structural.site` and the URL `/api/site/demo-snapshot`
do not move. Roadmap-slice IDs (SITE-1/SITE-2) are historical labels in prose only; leave them.

> ⚠ **INFERENCE, NOT ESTABLISHED — intended meaning could not be determined.** The brief for this
> audit asks that ambiguity be marked rather than invented, so: **I could not determine whether
> the Jev `site` word is a deliberate house term or an accident.** The counter-evidence is real
> and I am recording it against my own recommendation — the Proof Bench tracks are literally
> named `SITE-1` (`site_export.py:1`) and `SITE-2` (`route_pack.py:1`), and
> `site/worker/migrations/0001_init.sql:1` carries a third, `SITE-5`. A reader could equally
> conclude "site" is house vocabulary for *track/venue* and is used consistently on purpose, in
> which case this finding is wrong.
> **What would settle it:** whether `site="model_route"` and `site="hul_scope"` and
> `SITE-2` are meant to denote the same kind of thing. Ask the owner whether "site" is a
> reserved word in this codebase. **Until answered, treat the `judgment_kind` rename as
> optional** — the L1-4 *collision* (three meanings) stands on its own regardless of which word
> is canonical.

---

---

## L1-5 — `state` also collides across four non-Jev packages, and across two *types*

**Severity: Medium-High** · **Blast radius: the remaining bulk of the 574 `state` occurrences.**

| Location | What `state` actually is | Type |
|---|---|---|
| `harness/pyramid_state.py:64,74,90,115` | the persisted pyramid run envelope (keys `goal`, `dag`, `node_results`, `spent`) | `Dict[str, Any]` |
| `harness/apply_gate.py:38,66,72,97,114,186,220` | the **live apply run** — attribute access `state.current_content`, `state.round_no`, `state.backup` | an object with attributes |
| `harness/continuation.py:27,38` | `normalize_continuation(state)` / `validate_continuation(state)` — deferral/continuation record | dict |
| `harness/mission_record.py:697,713` | `write_resume(pack, state)` / `validate_resume(state)` — mission resume record | dict |

The `pyramid_state` vs `apply_gate` pair is the sharp one: **the same name is a `Dict` in one
module and an attribute-bearing object in another**, so a reader who learns the shape in one place
will mis-predict the other.

**Remediation.** Qualify the parameter names, keeping `state` for none of them:
- `dag_state` (pyramid_state) — or `PyramidState`, promoted to a TypedDict/dataclass
- `apply_run` (apply_gate)
- `continuation_state` (continuation)
- `resume_state` (mission_record)

---

## L1-6 — One dollar limit, three names: `max_cost` / `run_budget` / `run_ceiling`

**Severity: Medium** · **Blast radius: `max_cost` 140, `run_budget` 25, `run_ceiling` 15.**

The repo equates them itself, in a comment:

> `harness/executor.py:461` — *"`run_ceiling` is the budget this lane is really running under
> (the session's `max_cost`)."*

| Name | Where |
|---|---|
| `max_cost` / `max_cost_usd` | `config.py`, `SpendGovernor.max_cost`, `cli.py:626` |
| `run_budget` | `cli_parser.py:317` (`--run-budget`), `cli.py:621,632`, `executor` factory arg |
| `run_ceiling` | `executor.py:418,467`; `cli.py:1146`; `agent.py:1135` |

**Remediation.** Canonical **`run_max_cost`** (matches the dominant `max_cost` family, 140 vs 25
vs 15).

**Scope corrected after review — `max_cost` is public surface in two more places than the CLI
flag.** Three of the four names are unmovable without breaking a consumer:

| Name | Surface | Renameable? |
|---|---|---|
| `max_cost` | config; **HTTP request key** `server.py:217,218,286`; **published MCP schema field** `mcp_schemas.py:165` | **No** |
| `--run-budget` | CLI flag `cli_parser.py:317` | **No** |
| `run_ceiling` | `Executor.run_ceiling` — internal attribute (`executor.py:418,467`) | Yes |
| `run_budget` | Python param `cli.py:621,632` — internal | Yes, with an alias |

So the rename is **Python-symbol-only**: ship `run_max_cost` in the Python API, keep the
`max_cost` JSON key and the MCP field name byte-identical, and carry `run_budget` as a
keyword-accepting alias for one release. The MCP field is the sharpest constraint — it is a
*published schema* consumed by external agents, so it is a public API in the strictest sense.
Per the risk register, no DB column and no `/api/` path carries this concept.

---

## L1-7 — `harness/waist.py` (2 790 lines) is named after a metaphor and holds two responsibilities

**Severity: Medium** · **Blast radius: 15 importers of `harness.waist`; 2 790 LOC — the second
largest module in the repo.**

Its own docstring, line 1:
> `"""Plan-confirmation waist (M2) and LLM decomposition lane (M1).`

"Waist" is a shape metaphor from the roadmap (`docs/jev-roadmap.md`), not a description of what
the module does. A reader meeting `from .waist import …` learns nothing. Worse, the docstring
itself enumerates two unrelated responsibilities — decomposition (M1) and confirmation (M2) —
so the name can only ever cover one of them.

**Remediation.** Split by the responsibilities the docstring already names, and name for behaviour:
- `plan_decomposition.py` — the `decompose_via_llm` lane
- `plan_confirmation.py` — the one-round-trip plan confirm/repair waist
and re-export from a thin `waist.py` for one deprecation cycle. If a split is too costly, at
minimum rename to `plan_waist.py` so the noun qualifies.

---

## L1-8 — `Router.route()` returns a dict with a `tier` key that is a lane name (detail of L1-2)

**Severity: Medium** · **Blast radius: `harness.router` — 10 importers.**

```python
router.py:100  def route(self, task_type):
router.py:101      """Return the spec for a task type: cheap lane first."""
router.py:102      if task_type == "verify":
router.py:103          return {"tier": "panel", "panel": self.panel_pool, "judge": self.judge}
router.py:104      if task_type == "code":
router.py:105          return {"tier": "apply", "model": self.apply_model, "pool": self.apply_pool}
```

Consumers reading `spec["tier"]` get `"panel"` or `"apply"` — neither is a tier in either tier
system. Same method, same class as `route_tier` which returns an int under the same key.

**Remediation.** `{"lane": "panel"|"apply", …}`. Emit both keys for one release if the owner
confirms an external consumer exists; per the risk register this dict reaches no `/api/` path
and no DB column, so the exposure is limited to whatever already deserializes it.

> ⚠ **INFERENCE, NOT ESTABLISHED — intended meaning could not be determined.** I read
> `{"tier": "panel"}` as a mis-keyed **lane**, on the strength of the method's own docstring
> (`"cheap lane first"`, `router.py:101`) and the fact that `panel`/`apply` are exactly the
> values `Router` uses for `panel_pool`/`apply_pool`. **The competing reading is that `tier` is
> deliberate** — meaning "which subsystem takes this task", a *third* sense distinct from both
> tier systems in L1-2. Under that reading the key is accurate and only the word is overloaded,
> which would make this a glossary problem rather than a rename.
> **What would settle it:** whether any consumer reads `spec["tier"]` from `Router.route`
> specifically (as opposed to `Router.route_tier`). I searched and found **no in-repo consumer
> of `route()`'s return value at all** — which is exactly why I cannot separate the two readings.
> See open question Q4.

---

## L1-9 — `mcp.py:_handle` is a five-responsibility catch-all

**Severity: Low-Medium** · **Blast radius: the MCP server's central dispatcher; 1 139-LOC module.**

`harness/mcp.py:325 def _handle(self, msg)` — a 43-line function that in one body performs
protocol-version negotiation, caller capture, capability advertisement, cancellation handling,
ping, `tools/list`, `tools/call`, and method-not-found error construction.

```python
mcp.py:325  def _handle(self, msg):
mcp.py:355      if method == "notifications/cancelled":
mcp.py:356          return self._handle_cancellation(msg, is_notification)
mcp.py:364      if method == "tools/call":
mcp.py:365          return self._call_tool(msg)
mcp.py:366      return {"jsonrpc": "2.0", "id": msg.get("id"),
mcp.py:367              "error": {"code": -32601, "message": f"Method not found: {method}"}}
```

I rate this **Low-Medium** rather than higher, and want to be explicit that this is a judgement
call: a single JSON-RPC dispatcher is a common and defensible convention, and the linear
`if method ==` chain is easy to read top-to-bottom. The cost is that adding a method means
editing the one function every other method lives in.

**Remediation (optional).** Replace the chain with a `{"tools/call": self._call_tool,
"ping": self._ping, …}` handler map plus a small `_error(...)` helper, keeping `_handle` as the
single 6-line entry point. Purely structural; no public API change.

---

## L1-10 — `assess_completion_nouls` and `evaluate_completion_nouls` are the same operation

**Severity: Medium** · **Blast radius: 2 definitions, 1 delegating call, ~8 test references.**

`harness/orchestrator.py:142` is a thin wrapper whose entire job is to call the other one:
```python
orchestrator.py:150  if jev_policy is not None:
orchestrator.py:151      result, structural = jev_policy.evaluate_completion_nouls(
orchestrator.py:152          goal, state_summary, named_artifacts=named_artifacts,
orchestrator.py:153          root_dir=root_dir, site="completion")
```
versus `harness/jev_policy.py:1088 def evaluate_completion_nouls(self, goal, state_summary, …)`
with the **same parameters** and the same docstring first line (*"JEV-P3-completion: … nouls before
the generative judge"*). The wrapper is a shape adapter, but its name restates the whole operation
in a second vocabulary. Alongside it, `orchestrator.assess_completion` (`:83`) and
`orchestrator.assess_completion_nouls` (`:142`) are a third and fourth verb for "form a completion
verdict" against `jev_policy.evaluate_completion_nouls`.

**Remediation.** Keep the wrapper — the adapter is legitimate — but name it for what it does:
`assess_completion_nouls` → **`completion_noul_verdict`** (or `as_completion_verdict`). It is an
adapter, not a second implementation. See also V-1 in Layer 2.

---

## L1-11 — `GatePolicy.runner` is a method named as if it were an attribute

**Severity: Low** · **Blast radius: `harness/apply_gate.py:34`, one call site inside the class.**

```python
apply_gate.py:34   def runner(self, req):
apply_gate.py:35       return bound_gate(req.continuation_gate, req.verify_cmd,
apply_gate.py:36                             req.task_runner)
```
It is a method that returns a callable, so `gate_policy.runner` reads as a stored runner object
but is a verb phrase. Its siblings in the same class are all verbs (`write_candidate`, `run_gate`,
`apply_candidate`, `preview`, `terminal_failure`).

**Remediation.** `def bind_runner(self, req)` or `def build_runner(self, req)`.

---

## L1-12 — `Agent` dispatch methods share the `_handle_*` prefix with `MCP._handle`

**Severity: Low** · **Blast radius: `harness/agent.py:559,762,904`.**

`agent._handle_conversation`, `_handle_audit`, `_handle_edit` are well-named per-mode dispatchers —
**these are fine and I am not recommending a change to them.** Recording only the collision: the
same `_handle` prefix means "MCP JSON-RPC method dispatcher" in `mcp.py:325` and "per-mode agent
entry point" in `agent.py`. If L1-9 is addressed by introducing a handler map, prefer
`_dispatch_request` in `mcp.py` so the prefix stays unambiguous.

---

## L1-13 — `apply_edit(**kwargs)` / `apply_batch(files, **kwargs)` are unnameable public signatures

**Severity: Medium** · **Blast radius: `harness/apply.py:175,396` — the public engine entry points,
called from CLI, MCP, server, and agent.**

```python
apply.py:175  def apply_edit(self, **kwargs):
apply.py:396  def apply_batch(self, files, **kwargs):
apply.py:194  def _prepare(self, kwargs):
```

`GatePolicy` then reads ~10 attributes off whatever `req` turns out to be, several via
`getattr(req, "trust_combined", 0)` (`apply_gate.py:44`) and `getattr(req, "model", None)`
(`apply_gate.py:46`) — i.e. the code does not itself know the full attribute set. **`kwargs`
cannot be described in one sentence**; it is "whatever the callers happen to pass", and the
`getattr` defaults tell us the contract is undeclared.

**Remediation (design — narrow the type).** Introduce a single `ApplyRequest` dataclass/NamedTuple
and type both entry points with it. This is the highest-effort item in the report but also the one
that most directly matches the Layer 1 definition of unnameable.
**Risk: Medium.** `kwargs` callers are spread across CLI/MCP/server/agent, so a keyword-compatible
dataclass that accepts `**kwargs` on the outside and exposes attributes on the inside is the
low-risk path. Per the risk register, `ApplyRequest` is a **new** type and therefore does not touch
DB-1, the ledger, or any `/api/` path — the MCP `max_cost` field keeps its name inside it.

---

# Layer 2 — Canonical vocabulary

*One concept, several names. For each cluster: the concept, every name found, a recommended
canonical name, and risk. Occurrence counts as noted.*

---

## V-1 — `assess_` vs `evaluate_` vs `judge` for "form a judgment"

**Concept:** ask a model (or a policy) to reach a verdict on something, and return a structured
result.

| Name | Occurrences (def prefixes) | Representative locations |
|---|---|---|
| `evaluate_` | **23** | `JevPolicy.evaluate_*` (23 methods, `jev_policy.py`), `executor.evaluate` |
| `validate_` | 35 | see V-2 — deliberately excluded, different meaning |
| `assess_` | **3** | `orchestrator.assess_completion:83`, `assess_completion_nouls:142` |
| `score_` | 2 | `jev_completion` |
| `rank_` / `decide_` / `choose_` | 1 each | scattered |

**Recommended canonical: `evaluate_`.** Reasons: 23 vs 3 is decisive; it is the prefix on the
public `JevPolicy` API that the whole judging subsystem hangs off; and `assess_completion_nouls`
is a pure delegating wrapper over `evaluate_completion_nouls` (L1-10), so the second name adds no
concept. **Risk: Low** — 3 call sites, all internal. Rename `assess_completion` →
`evaluate_completion_freeform` (or just `completion_verdict`, to mark the non-Jev fallback) and
`assess_completion_nouls` → `completion_noul_verdict`.

---

## V-2 — `validate_` vs `verify_` vs `check_` vs `lint_`

**Concept:** assert that something is well-formed / acceptable before proceeding.

| Name | Occurrences (def prefixes) | Representative locations |
|---|---|---|
| `validate_` | **35** | `validation.validate_text:70`, `mission_record.validate_mission_spec:134`, `jev_packs.validate_operator_pack`, `filesafety.validate_verify_command:39` |
| `verify_` | 4 | `gate_runner.validate_gate` is `validate_`; but `verify` is also a **CLI command**, a **lane name** (`task_type == "verify"`), and `verify_lane` |
| `check_` | 4 | `trust.check_mutation`, `capability` checks |
| `lint_claims` | CLI `lint-claims` | `claims.py` |

**Recommended canonical: `validate_` for shape checks; reserve `verify` for the gate/lane/CLI
concept; `lint_` for style-only checks.** Evidence that this split is already the intent:
`filesafety.validate_verify_command` and `gate_runner.validate_gate` both use `validate_` even
though they concern verification. **Risk: Low** (internal functions only; the `verify` CLI command
and the `verify` lane name are public and must NOT be renamed).

---

## V-3 — "a maximum" is `budget` / `ceiling` / `limit` / `cap` / `max_*`

**Concept:** an upper bound the system will not exceed.

| Name | Count | Representative locations |
|---|---|---|
| `budget` | **329** | `run_budget`, `TokenBudget`, `token_budget`, `stage_budget`, `load_budget`, `dual_budget_envelope`, `budget_noul` |
| `ceiling` | **205** | `run_ceiling:15`, `cost_ceiling:20`, `plan_ceiling:10`, `node_ceiling:9`, `phase_ceiling:8`, `total_ceiling:5`, `ceiling_fraction:5`, `tier_cost_ceiling:4` |
| `limit` | 112 | `max_lines`, `max_bytes`, `max_attempts`, `max_items`, `max_files` |
| `cap` | 67 | `hard_cap:6`, `chunk_cap`, `max_price` (gateway cap) |
| `max_*` | — | `max_tokens:162`, `max_cost:133`, `max_input_tokens:81`, `max_cost_usd:51` |

**Recommended canonical: `max_<unit>` for the number itself (`max_cost_usd`, `max_input_tokens`),
and `budget` only for a *pool* of money/tokens that is drawn down over a run.** That is already
the distinction `token_budget.py` draws in its own docstring ("Token allowances … never prices
anything"). The outliers to fold in are the **`ceiling` family (205)** — `run_ceiling` in
particular is already proven by its own comment to be `max_cost` (L1-6) — plus `hard_cap`.
Keep `max_price`: it is an OpenRouter gateway field name, not ours. **Risk: Medium** — 205
`ceiling` occurrences, but most are local variables inside one function; the only attribute is
`Executor.run_ceiling` (15 refs).

---

## V-4 — `tier` / `lane` / `rung` / `pool` for "a step on the model ladder"

**Concept:** a position in the ordered cheap→capable model ladder, and the ordered candidate list
it draws from.

| Name | Count | Meaning as actually used |
|---|---|---|
| `tier` | 305 | price band (`routing_table`, str) **and** complexity class (`sliding_scale`, int) — see L1-2 |
| `lane` | 259 | **execution mode**: free-distill / diff / frontier / panel / apply / verify. `effective_lane_policy:8`, `call_lane:21`, `task_type == "verify"` |
| `rung` | 230 | **escalation step index**: `rung_id:48`, `target_rung:22`, `start_rung:10`, `current_rung:7` |
| `pool` | 120 | **ordered candidate list**: `apply_pool`, `panel_pool`, `escalation_pool` |

These are *mostly* genuinely distinct, which is why this is Medium rather than High — but the
docstrings blur them. `harness/router.py:11-12`:
> *"the system de-escalates back to the last **tier** that needed escalation"*

…in a docstring whose surrounding text is entirely about **rungs** (`"judge-directed rung-by-rung
stepping"`, `:10`). And `escalation.py:45-56` alternates freely: *"`TIER_2_FRONTIER`; a ladder rung
index is its production form"* and *"`confidence < 0.5` -> … climb toward the ladder's most capable
rung (the frontier bucket)"*.

**Recommended canonical, one word per concept:**
- `tier` → **`price_band`** or **`complexity_tier`** (L1-2) — retire the bare word.
- `lane` → keep (execution mode).
- `rung` → keep (escalation step).
- `pool` → keep (ordered candidate list).

Then fix the docstrings, which are where the confusion is actually transmitted. **Risk: Low** —
the four words are already largely separated in code; this is a docstring + `tier`-family rename.

---

## V-5 — `site` / `SITE_*` constant naming is internally inconsistent, and three sites bypass it

**Concept:** the Jev evaluation lane recorded on each judgment.

| Name | Value | Where |
|---|---|---|
| `LOG_FACTOR_SITE` | `"log_factor"` | `jev_packs.py:1338` — **constant matches value** |
| `ROUTE_QUERY_SITE` | `"model_route"` | `route_pack.py:22` — **constant ≠ value** |
| `REPO_SUMMARY_SITE` | `"repo_summary"` | `jev_packs.py:1403` — matches |
| `PHASE_COMPLETION_SITE` | `"phase_completion"` | `jev_packs.py:1569` — matches |
| `HUL_SCOPE_SITE` | `"hul_scope"` | `jev_packs.py:1265` — matches |
| *(no constant)* | `"issue_sort"` | 3 literal uses |
| *(no constant)* | `"completion"` | `orchestrator.py:151` |
| *(no constant)* | `"router"`, `"route"`, `"cli"`, `"agent"`, `"mcp"`, `"apply"`, `"batch"`, `"claims"`, `"answer"`, `"waist"`, `"hourglass"` | 1 each |

Seven constants, but only `ROUTE_QUERY_SITE` breaks the "constant name = value" rule (it is named
for the *question* — "route query" — while its value is the *lane* — "model_route"). And four
Jev sites (`issue_sort`, `completion`, `router`, `route`) are raw literals despite the module
already establishing the constant pattern.

**Recommended canonical:** constants named `<VALUE>_SITE` (so `MODEL_ROUTE_SITE = "model_route"`),
and **every** Jev site promoted to a constant. Note the unrelated `server.py:79 SITE_ROOT` (the
website) and `SITE_TYPES` (MIME map) must be excluded from this rule — see L1-4.
**Risk: Low** for the constant-name fix; the literal→constant promotion is a pure refactor.

---

## V-6 — `run_budget` / `run_ceiling` / `max_cost`

Covered in detail as **L1-6**. Recorded here as a vocabulary cluster: concept = "the dollar limit
for one run"; names = `max_cost` (140), `run_budget` (25), `run_ceiling` (15); canonical =
`run_max_cost`.

**Public surface at risk is three names, not one:** the `--run-budget` CLI flag
(`cli_parser.py:317`), the `max_cost` HTTP request key (`server.py:217,218,286`), and the
`max_cost` MCP schema field (`mcp_schemas.py:165`). Only `run_ceiling` and `run_budget` are
renameable. Per the risk register, no DB column and no `/api/` path carries this concept.

---

## V-7 — `assess_completion` / `evaluate_completion_nouls`

**Concept:** decide whether the mission goal is complete, before and independently of the
generative judge.

Covered as **L1-10** and **V-1**. Occurrence counts (word-boundary, `harness/` / `tests/`):

| Name | `harness/` | `tests/` | Role |
|---|---|---|---|
| `assess_completion` | 2 | 5 | the free-form judge call (`orchestrator.py:83`) |
| `assess_completion_nouls` | 2 | 4 | a **pure delegating adapter** over the next row (`orchestrator.py:142`) |
| `evaluate_completion_nouls` | 2 | 11 | the real implementation (`jev_policy.py:1088`) |

Canonical prefix = `evaluate_` (23 defs vs 3 for `assess_`); rename the adapter by role
(`completion_noul_verdict`) rather than restating the verb. **Risk: Low** — 2 definitions and
3 production call sites, all internal; the highest test-density name in the cluster is the one
the report does *not* recommend changing.

---

## V-8 — `req` vs `request` vs `args` vs `kwargs`

**Concept:** the parameters of one apply/verify run.

Occurrence counts (word-boundary, `harness/`): `args` **254**, `req` **312**, `request` **187**,
`kwargs` **89**. The clusters are sharply separated by file, which is why this is a rename of
*domains* rather than a global find-and-replace:

| Name | Count | Where it concentrates | Representative locations |
|---|---|---|---|
| `req` | 312 | `apply_policy.py` (147), `apply_gate.py` (90) | `apply_gate.py:34,38,66,72,97,114,186,220` (8 params in `GatePolicy` alone); `escalation.py:246`; `web.py:65`; `_http.py:22` |
| `args` | 254 | `mcp.py` (100), `server.py` (90) | `server.py:243,263,291,306,328` — `run_apply_task(task_id, args, cancel_check)`; `dag.py` (20) |
| `request` | 187 | `waist.py` (25), `jev_packs.py` (20), `mcp.py` (11), `_http.py` (11) | the Jev scope-facts key `"request"`; the HTTP sense in `web.py:65` |
| `**kwargs` | 89 | `apply.py` (40), `dag.py` (20) | `apply.py:175 def apply_edit(self, **kwargs)`, `:396 def apply_batch(self, files, **kwargs)` — see **L1-13** |

**Note for the rename plan:** in `server.py`, `args` is the *deserialized HTTP request body*, and
the **keys inside it are the public API** (`max_cost` at `server.py:217`; `state` at `:221`).
Renaming the Python parameter `args` is safe; renaming anything it carries is not.

**Recommended canonical: `request`** in the apply/gate domain; `args` → `request` in
`server.py`'s five `run_*_task` functions. `apply_edit(**kwargs)` / `apply_batch(**kwargs)` is a
separate, larger issue — an untyped catch-all signature on the public engine — flagged below.
**Risk: Low** (local parameter names; `args` in `server.py` is internal to the task wrappers).

---

## L2-1 — Two modules both call themselves "the orchestrator"

**Severity: Low-Medium** · **Blast radius: `harness/agent.py` (1 289 LOC) and `harness/orchestrator.py`,
plus `mission_driver.py` and `executor.py`.**

```
agent.py:1         # Autonomous agent orchestrator (#PR-Chat-1)
orchestrator.py:1  """Autonomous orchestration policy.
```
and two further "who runs the work" modules: `mission_driver.py` (*"HUL-D until-limits mission
driver"*) and `executor.py` (*"Concurrent multi-threaded executor and file-locking manager"*).

These are four distinct roles, but only two of them say so in their name, and `agent.py` claims
the name that `orchestrator.py` owns. **Recommended:** `agent.py` → **`chat_agent.py`** (its actual
job: drive NL prompts to conclusion with no UI) or `prompt_agent.py`; keep `orchestrator.py`,
`mission_driver.py`, `executor.py` as-is. **Risk: Low-Medium** — `harness.agent` is imported by
3 modules; it is also plausibly referenced in docs and by external consumers, so check before
renaming.

---

## L2-2 — `harness/osal.py`: an acronym that is never expanded

**Severity: Low** · **Blast radius: 447 LOC; referenced in `README.md:739,740,742`; imported by
`chat.py`, `filesafety.py`, `config.py`, `render.py` and others.**

`OSAL` is standard industry jargon (OS Abstraction Layer) but it is **never expanded anywhere in
this repo** — not in the module docstring (*"The ONE place Harness talks to the operating
system."*), not in `README.md`, not in `docs/`. A reader who does not already know the acronym
cannot tell what the module is for.

**Recommended canonical: `os_shim`** (or `platform_io`; **not** `os.py`, which would shadow the
stdlib). The module is not an abstraction layer over the OS — it is a thin, fail-closed shim with
Harness-specific policy (`norm_path`, `is_within`, `keyfile_is_insecure`, `HARDEN_REUSE`), so
`os_shim` is both shorter and more accurate than `osal`. **Risk: Low** — pure module rename;
`README.md` needs 3 line updates.

---

## L2-3 — `noul`: 81 uses in code, defined only in `docs/`

**Severity: Low** · **Blast radius: 81 `noul` + 42 `nouls` + 30 `_noul` = ~153 identifiers in
`harness/`; the term is load-bearing in the public `JevPolicy` API
(`evaluate_completion_nouls`, `_decision_noul`, `budget_noul`, `noul_tally`).**

`noul` is the TypeSafe "no/yes probability" answer type. The definition exists only in
`docs/jev-roadmap.md:47,53` (*"Typed answers (`noul` / `score` / `choice`)"*; *"Noul = yes-probability
(no confidence)"*). Nothing in the code says what it is.

This is **not** a rename recommendation — `noul` is a deliberate, load-bearing domain term and
renaming it would be expensive and probably wrong. The recommendation is a **glossary entry plus a
one-line definition in `harness/jev.py`**, where the answer types are defined. **Risk: N/A**
(documentation only).

---

# Checked and deliberately NOT flagged

Recorded so the absence of a finding is not mistaken for an unexamined area.

- **`results.py` / `output.py` / `render.py`** — three similarly-named modules, but they are cleanly
  separated and each says so at the top: `results.py` *"the one owner of every shape a run terminal
  emits"* (builders), `output.py` *"one owner for progress chatter and `--quiet`"* (stderr),
  `render.py` *"the ONE pretty-printer"* (result → human tables). **No synonym cluster.** This is
  the repo's naming done well.
- **`spend.py` (dollars) vs `token_budget.py` (tokens) vs `tokens.py` (estimation)** — three modules
  whose names could collide, but the split is explicit and correct. `token_budget.py:3-7`:
  *"Independent of `SpendGovernor`, which owns DOLLARS. The two limits answer different questions
  and neither can raise the other."* **No finding.**
- **Numeric suffixes (`temp`, `temp2`, `result2`)** — swept `harness/`, `scripts/`, `audits/`,
  `site/`, `bench/`, `packs/`. The only hits are `harness/local_fit/infer.py:127,129` (`row2`, a
  genuine second matrix row in a hand-rolled matmul). **Essentially clean** — this codebase does
  not have that problem.
- **Tautological names (`dataObject`, `userInfoData`, `…Info`, `…Data` suffixes)** — swept. The only
  hits are `provider_obj` (`chat.py:298-304`) and `axis_info` (`jev_completion.py`, 10 uses) and
  `chain_info` (`agent.py:771-783`); all are locally meaningful, none are `dataObject`-grade.
  `data` appears 172 times repo-wide, which is unremarkable.
- **`Union[...]`** — exactly one occurrence in first-party code
  (`scripts/validate_handoff_scope.py:449 is_handoff_path(path: Union[Path, str])`), and that union
  is honest (it is tested for both). The `Any`-as-everything problem in this repo is expressed as
  **untyped parameters and `Dict[str, Any]`, not as `Union`** — which is why V/L1-1 is framed around
  `Any` + divergent runtime probes.
