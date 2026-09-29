# Naming Audit — GLOSSARY (draft, proposed)

**Status of this document: PROPOSAL for the repo owner. Not a decree.**

Every entry is `proposed` unless marked otherwise. Nothing here has been changed in the source.
Where a rename would break something a machine reads (CLI, ledger, JSON, URL, config key), the
compatibility approach is stated explicitly — those are the entries where a plain rename is the
wrong move.

Canonical-name tie-breaks applied throughout, in order:
1. Prefer the name that a newcomer can read without opening the file.
2. Prefer the most-used name, when it is also clear.
3. Prefer the name that matches the public API surface (CLI flag, exported symbol).
4. When two clear names tie, prefer the one already carried by the widest blast radius.

---

## 1. Cost limit for a single run

| | |
|---|---|
| **Canonical (proposed)** | `run_max_cost` |
| **Known aliases** | `max_cost` (140 refs), `max_cost_usd` (51), `run_budget` (25), `run_ceiling` (15) |
| **Where** | `spend.SpendGovernor.max_cost`; `config`; `cli_parser.py:317` `--run-budget`; `cli.py:621,626,632`; `executor.py:418,461-467`; `agent.py:1135` |
| **Why** | `max_cost*` is 7× the usage of the other two and is the name on the governor. `executor.py:461` already writes the equivalence in a comment: *"`run_ceiling` is the budget this lane is really running under (the session's `max_cost`)."* |
| **Risk** | **Medium — public surface in three places, not one.** Do **not** rename: (a) the `--run-budget` CLI flag (`cli_parser.py:317`); (b) the `max_cost` **HTTP request key** (`server.py:217,218,286`); (c) the `max_cost` **published MCP schema field** (`mcp_schemas.py:165`) — a published schema consumed by external agents, i.e. public API in the strictest sense. Ship `run_max_cost` in the **Python API only**, keeping the JSON key and MCP field byte-identical. `Executor.run_ceiling` → `.run_max_cost` is internal (15 refs) and safe to rename directly; carry `run_budget` as a keyword alias for one release. No DB column or `/api/` path carries this concept. |

## 2. Any upper bound the system will not exceed

| | |
|---|---|
| **Canonical (proposed)** | `max_<unit>` for the number; `budget` only for a *drawable pool* |
| **Known aliases** | `budget` (329), `ceiling` (205), `limit` (112), `cap` (67), plus the `max_*` family (`max_tokens` 162, `max_cost` 133, `max_input_tokens` 81) |
| **Where** | `run_ceiling`, `cost_ceiling`, `plan_ceiling`, `node_ceiling`, `phase_ceiling`, `total_ceiling`, `ceiling_fraction`, `tier_cost_ceiling`; `hard_cap`; `chunk_cap` |
| **Why** | The repo already draws this line in `token_budget.py:3-7` ("never prices anything") and `spend.py`. `ceiling` (205) is the family to fold in — `run_ceiling` is proven by its own comment to be `max_cost`. |
| **Risk** | **Low–Medium.** Most `ceiling` uses are function-local. Exceptions: `Executor.run_ceiling` (internal), and `max_price` which is an **OpenRouter gateway field name — keep as-is**, it is not ours. |

## 3. Price band of a model

| | |
|---|---|
| **Canonical (proposed)** | `price_band` / `PRICE_T0`…`PRICE_T3` |
| **Known aliases** | `tier` (`"T0"`–`"T3"` strings), `TIER_0`…`TIER_3`, `TIER_COST_BANDS`, `classify_model_tier`, `get_tier_route`, `next_tier` |
| **Where** | `routing_table.py:10-13, 40-53, 108, 135, 146, 160` |
| **Why** | The module's own dict is literally named `TIER_COST_BANDS` (`routing_table.py:48`) and its docstring says *"Cost band labels for telemetry and reporting"*. `price_band` is unambiguous against complexity tiers and costs nothing to read. |
| **Risk** | **Low.** The string values `"T0"`–`"T3"` are internal. `get_tier_route` / `next_tier` / `classify_task_tier` (this copy) have **no production callers** — see entry 4 — so they can be deleted rather than renamed. |

## 4. Classify a task's complexity

| | |
|---|---|
| **Canonical (proposed)** | `complexity_tier`, function `classify_complexity_tier` |
| **Known aliases** | `classify_task_tier` (live, `sliding_scale`), `classify_task_tier` (dead, `routing_table`), `complexity_tier` (27 internal uses), `route_tier` |
| **Where** | `sliding_scale.py:33-35, 119, 243, 259, 329`; `router.py:59, 83` |
| **Why** | `complexity_tier` is already the internal term; promoting it to the public name costs nothing. The bare verb+noun `classify_task_tier` is ambiguous *because* two different functions answer to it. |
| **Risk** | **Low.** Rename the live `sliding_scale.classify_task_tier` (2 production callers) and **delete** the `routing_table` duplicate. See L1-3. |

## 5. Execution mode / strategy of a run

| | |
|---|---|
| **Canonical (proposed)** | `lane` |
| **Known aliases** | `task_type` (`"verify"`/`"code"`), the `"tier"` key of `Router.route()`'s return value (`"panel"`/`"apply"`) |
| **Where** | `router.py:100-107`; `config.effective_lane_policy` (8); `call_lane` (21); `verify_lane` (3); `agent_edit_lane` (2); `task_type == "verify"` |
| **Why** | `lane` is already the dominant word (259) and every value in question is a lane. `Router.route()`'s own docstring says *"cheap lane first"* — the code disagrees with its own docstring. |
| **Risk** | **Low–Medium.** The returned dict may be serialized into plan JSON — **verify before renaming** (open question Q4). If it is, emit both keys for one release. |

## 6. A step on the cheap→capable escalation ladder

| | |
|---|---|
| **Canonical (proposed)** | `rung` |
| **Known aliases** | `tier` (in `router.py:11-12` prose: *"de-escalates back to the last **tier** that needed escalation"*), `bucket` (in `escalation.py:55`: *"the frontier **bucket**"*), `_escalation_rung` |
| **Where** | `escalation.py` (rung vocabulary throughout); `router.py:130-148` (`advance_escalation_rung`, `de_escalate_to_rung`, `current_escalation_rung`); `rung_id` (48), `target_rung` (22), `start_rung` (10), `current_rung` (7) |
| **Why** | `rung` is already the consistent code-level term (230 refs) and matches the roadmaps' "ladder" metaphor. The aliases appear only in prose, so the fix is documentation. |
| **Risk** | **Low.** Prose-only. Also rename the `_escalation_rung` private attribute to `_rung` for consistency. |

## 7. An ordered list of candidate models to try in turn

| | |
|---|---|
| **Canonical (proposed)** | `pool` |
| **Known aliases** | `ladder` (used interchangeably in `router.py:9` and `sliding_scale.py`'s `tier_model_ladder`) |
| **Where** | `apply_pool`, `panel_pool`, `escalation_pool`; `tier_model_ladder`; `Router.route_tier` returns `{"pool": ladder}` |
| **Why** | `pool` (120) is the noun used for the object; `ladder` is the metaphor for the ordering. Pick one noun. `pool` wins on frequency and on being a real thing you hold. |
| **Risk** | **Low.** Note `tier_model_ladder` is a *function* that returns a pool — rename to `tier_model_pool` to match. |

## 8. The Jev evaluation lane recorded on a judgment

| | |
|---|---|
| **Canonical (proposed)** | `judgment_kind` (parameter), constants named `<VALUE>_SITE` kept as-is for now |
| **Known aliases** | `site` (parameter), `structural.site` (persisted key), `SITE-1`/`SITE-2` (roadmap slice IDs) |
| **Where** | `JevPolicy.evaluate_*(..., site=...)` — 15 literal `site="…"` values; 10 `*_SITE` constants in `jev_packs.py`, `route_pack.py`; written to the ledger as `structural.site=…` |
| **Why** | `site` is already taken by the **website** (`server.SITE_ROOT` → `site/public`, `site_export.py`, `site_aggregate.py`) and by **roadmap slice IDs** (`SITE-1`, `SITE-2`). Three meanings, one word. |
| **Risk** | **High — serialized format + URL.** `structural.site` lands in ledger `jev_eval` events and flows into site-export JSON; the website also exposes `/api/site/demo-snapshot`, where `site` means the *website* (a fourth confirmed meaning, on public surface). **Do not rename the persisted key or the URL.** Recommended path: rename only the *Python parameter* to `judgment_kind`, keep writing `"site"` in the ledger record. Add `ISSUE_SORT_SITE`/`COMPLETION_SITE`/`ROUTER_SITE`/`ROUTE_SITE` constants for the four literal-only sites. Leave `SITE-1`/`SITE-2` (historical prose) and `SITE_ROOT`/`SITE_TYPES` (website) alone. ⚠ **See the inference flag in `FINDINGS.md` L1-4** — whether Jev `site` is deliberate house vocabulary is undetermined, so treat this rename as optional until the owner answers. |

## 9. Form a verdict by asking a model

| | |
|---|---|
| **Canonical (proposed)** | `evaluate_` |
| **Known aliases** | `assess_` (3), `score_` (2), `judge` (noun only) |
| **Where** | 23 `JevPolicy.evaluate_*` methods; `orchestrator.assess_completion:83`; `orchestrator.assess_completion_nouls:142` |
| **Why** | 23 vs 3, and it is the prefix on the subsystem's public API. `assess_completion_nouls` is a pure delegating wrapper over `evaluate_completion_nouls` (verified at `orchestrator.py:150-153`). |
| **Risk** | **Low.** 3 call sites, all internal. Keep the wrapper; rename it by role to `completion_noul_verdict` so it reads as an adapter rather than a second implementation. |

## 10. Assert that something is well-formed before proceeding

| | |
|---|---|
| **Canonical (proposed)** | `validate_` (shape) · `verify` (run the gate / the lane / the CLI command) · `lint_` (style only) |
| **Known aliases** | `check_` (4), `verify_` (4) |
| **Where** | 35 `validate_` defs incl. `validation.validate_text:70`, `mission_record.validate_mission_spec:134`, `filesafety.validate_verify_command:39`, `gate_runner.validate_gate:127`; 4 `check_` incl. `trust.check_mutation`; CLI `verify` and `lint-claims` |
| **Why** | The repo already uses `validate_` for verification-adjacent checks, so the split is latent, not invented. `verify` is genuinely triple-purpose (function prefix, CLI command, lane name) and should be protected rather than renamed. |
| **Risk** | **Low** for function names. **Never rename the `verify` CLI command or the `verify` lane value** (`Router.route(task_type="verify")`), and note `verify` is also a **URL path segment** — `/api/ledger/verify` — so it is triple-public, not double. |

## 11. The subject a Jev method is judging

| | |
|---|---|
| **Canonical (proposed)** | One named parameter per method: `issue`, `log_item`, `repo_element`, `route_query`, `phase_evidence`, `mission_state` |
| **Known aliases** | `state` (six methods), `mission_state` (one method — the correct precedent) |
| **Where** | `jev_policy.py:1329, 1493, 1547, 1663, 1703, 1831, 1874, 2032, 2044, 2153, 2185, 2252` |
| **Why** | `evaluate_scope(self, mission_state, …)` at `:1356` already does this correctly in the same class. Following the in-repo precedent beats inventing a new convention. |
| **Risk** | **Medium.** Internal Python API, but `JevPolicy` has 26 importers. `state` is **not** persisted or serialized — it is a parameter name only — so the rename is safe if done with the type narrowing in L1-1 (do them together). |

## 12. Run state — five Python concepts, one name, plus two persisted ones

| Concept | Canonical (proposed) | Where it is today | Physically persisted? |
|---|---|---|---|
| Persisted DAG-run envelope | `dag_state` (or promote to a `PyramidState` TypedDict) | `pyramid_state.py:64,74,90,115` — param `state`, type `Dict[str, Any]` | No |
| Live apply run (attributes: `.current_content`, `.round_no`, `.backup`) | `apply_run` | `apply_gate.py:38,66,72,97,114,186,220` — param `state`, type = object | No |
| Deferral / continuation record | `continuation_state` | `continuation.py:27,38` — param `state` | No |
| Mission resume record | `resume_state` | `mission_record.py:697,713` — param `state` | No |
| Jev subject | see entry 11 | `jev_policy.py` — param `state`, type `Any` | No |
| **OC handoff task lifecycle** | **do not rename** | SQLite columns `tasks.state`, `outbox.state` (`examples/oc_handoff/worker.py:59,74`); read at `jev_completion.py:532,535` | **YES — DB** |
| **A file path passed over HTTP** | **do not rename** | JSON key `state` → `open(args["state"], …)` at `server.py:221,294` | **YES — public API** |

**Why:** seven concepts, one name, and the first two Python ones are *different types* under the
same word — a `Dict` in `pyramid_state` and an attribute-bearing object in `apply_gate`. A reader
who learns the shape in one module will mis-predict the other.

**Risk:** **Low for the five Python parameters, HIGH for the two persisted ones.** The proposed
renames touch parameters only and imply no migration. But `state` is *also* a live SQLite column
in two tables and a public HTTP request key — a reader who greps for `state` and renames it
wholesale would break both. The HTTP `state` key is the one place a rename would be a breaking
API change, and it is not even the same concept as the others: it is a **path**, not a state.

## 13. The operating-system shim

| | |
|---|---|
| **Canonical (proposed)** | `os_shim` (module `harness/os_shim.py`) |
| **Known aliases** | `osal` (`harness/osal.py`, 447 LOC) |
| **Where** | imported by `chat.py`, `filesafety.py`, `config.py`, `render.py` and others; referenced in `README.md:739,740,742` |
| **Why** | `OSAL` is never expanded anywhere in this repo — not in the module docstring (*"The ONE place Harness talks to the operating system."*), not in `README.md`, not in `docs/`. It is also not a true abstraction layer: it is a fail-closed shim with Harness-specific policy (`norm_path`, `is_within`, `keyfile_is_insecure`, `HARDEN_REUSE`). `os_shim` is shorter and more accurate. |
| **Risk** | **Low.** Pure module rename plus 3 `README.md` line updates. **Avoid `os.py`** — it would shadow the stdlib module inside the package. |

## 14. A "no/yes probability" answer from the TypeSafe schema

| | |
|---|---|
| **Canonical (proposed)** | `noul` — **keep, do not rename**; add a definition |
| **Known aliases** | `nouls` (plural), `_noul` (private prefix), `budget_noul`, `noul_tally`, `noul_value` |
| **Where** | ~153 identifiers in `harness/`; public via `JevPolicy.evaluate_completion_nouls`, `_decision_noul` |
| **Why** | Deliberate, load-bearing domain term from TypeSafe. The **only** definitions are in `docs/jev-roadmap.md:47,53` — nothing in the code says what it is. |
| **Risk** | **N/A — documentation only.** Add a one-line definition to `harness/jev.py` next to the answer types. A rename would be expensive and probably wrong. |

## 15. Who runs the work — four modules, two of which claim the same name

| Concept | Canonical (proposed) | Where it is today |
|---|---|---|
| Drive a natural-language prompt to completion, no UI | `chat_agent` | `harness/agent.py` (1 289 LOC) — docstring line 1: *"Autonomous agent orchestrator"* |
| Policy around a planned edit: triage, completion judgment, bounded rounds | `orchestrator` (unchanged) | `harness/orchestrator.py` — docstring line 1: *"Autonomous orchestration policy"* |
| Iterate attempts against a mission pack until a limit is hit | `mission_driver` (unchanged) | `harness/mission_driver.py` |
| Concurrent execution + file locking | `executor` (unchanged) | `harness/executor.py` |

**Why:** `agent.py` claims the noun that `orchestrator.py` owns; two modules both read as "the
orchestrator".
**Risk:** **Low–Medium.** `harness.agent` has 3 in-repo importers, but is a plausible external
entry point. Grep `docs/` and `README.md` for `harness.agent` / `from harness import agent` before
renaming.

## 16. Result / rendering / progress output — **already consistent, no action**

| Concept | Canonical | Where |
|---|---|---|
| The shape of a terminal run outcome (builders) | `results` | `harness/results.py` |
| Progress chatter and `--quiet` enforcement (stderr) | `output` | `harness/output.py` |
| Result envelope → human tables (the pretty-printer) | `render` | `harness/render.py` |

Recorded explicitly because the three names invite exactly the synonym-cluster finding this audit
is looking for, and the repo has already answered it in three module docstrings. **No change
proposed** — this is the pattern the other entries should be moved toward.

---

## Entries I deliberately did not propose

- **`--run-budget` CLI flag** — public surface; keep the flag name, rename only the Python symbol.
- **`verify` CLI command and `verify` lane** — public surface and core domain vocabulary.
- **`max_price`** — an OpenRouter gateway parameter, not our vocabulary.
- **`SITE-1` / `SITE-2` roadmap slice IDs** — historical labels in prose; renaming rewrites history.
- **`SITE_ROOT` / `SITE_TYPES`** — these genuinely mean the website, which is the meaning `site`
  should keep.
- **Ledger key `structural.site`** — persisted; rename the parameter, not the key.
- **`noul`** — see entry 14.
