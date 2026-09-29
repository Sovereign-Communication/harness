# Naming Audit — SUMMARY

Repo: `Harness` (`sovereign-harness`) · branch `freebuff/mission-b2d7eb8e-cbc3-48c4-9f35-3024cc4f462e`
· audited 2026-09-29 · **read-only** (no source file was created, modified, moved, or renamed).

Companion files: [`FINDINGS.md`](FINDINGS.md) (13 Layer 1 + 11 Layer 2 findings, plus 5
explicitly-cleared areas) · [`GLOSSARY.md`](GLOSSARY.md) (16 proposed glossary entries).

---

## 1. Scope and method

### What I covered

**First-party source — full sweep.** `harness/` (85 `.py` files, 39 178 LOC), `scripts/`,
`audits/self/`, `site/`, `bench/`, `packs/`. Every `def`/`class`, every module-level assignment,
and every public signature in `harness/` was read by search; the highest-traffic modules were
read in full.

**Priority order of the sweep** (stratified by import count, which I computed as the primary
blast-radius proxy):

| Rank | Module | Importers | Why read closely |
|---|---|---|---|
| 1 | `errors.py` | 52 | widest reach |
| 2 | `ledger.py` | 47 | core domain |
| 3 | `config.py` | 46 | every entry point |
| 4 | `spend.py` | 35 | cost domain |
| 5 | `jev.py` | 27 | core domain |
| 6 | `jev_policy.py` | 26 | core domain, 2 800 LOC — **source of the top finding** |
| 7 | `waist.py` | 15 | 2 790 LOC, second-largest module |
| 8 | `jev_packs.py` | 13 | 2 599 LOC |
| 9–12 | `router.py`, `chat.py`, `dag.py`, `panel.py` | 9–10 | routing + judging |
| 13+ | `cli.py`, `cli_parser.py`, `capability.py`, `apply*.py`, `executor.py`, `mcp.py`, `osal.py`, `routing_table.py`, `sliding_scale.py`, `route_pack.py`, `pyramid_state.py`, `mission_record.py`, `orchestrator.py`, `agent.py`, `continuation.py` | 1–8 | public API + named findings |

**Public surface.** All **30** CLI subcommands and **118** CLI flags in `cli_parser.py`; the MCP
tool surface; `harness/__init__.py` (thin — version only, so the real public API is the CLI +
module functions, and I treated it that way).

**Persisted and remote surface — added in the correction pass.** An initial version of this audit
omitted two of the four risk dimensions the brief names. They are now covered in full in the
`FINDINGS.md` **risk register**:
- **Database schema** — two SQLite databases. `.harness/oc_handoff/state.sqlite3` (DDL at
  `examples/oc_handoff/worker.py:53-78`, read at `harness/jev_completion.py:512-541`) with
  `tasks` and `outbox`; and the Proof Bench `bundles` store
  (`site/worker/migrations/0001_init.sql:3-7`). `state` and `phase` are live column names.
- **URLs** — 23 `/api/*` routes in `harness/server.py`. Checked directly: **none** of the tokens
  this report recommends renaming appears in any route path.
- **Public JSON/MCP keys** — `max_cost` (`server.py:217,218,286`; `mcp_schemas.py:165`), `state`
  (`server.py:221,294`), `phase` (`server.py:997`; `mcp_schemas.py:273,280`).
Net effect on the recommendations: **no finding requires a database migration**, but `max_cost`
is confirmed public well beyond the CLI flag, and the `state` name is now shown to span five
Python parameters *plus* two persisted surfaces.

**Tests as evidence, not as targets.** I searched `tests/` to establish *dependents* and to
confirm dead code (e.g. that `routing_table.classify_task_tier` has zero production callers), but
test files were not themselves audited.

### Tools used

`run_terminal_command` (git, `find`, `wc`, `grep -rn/-rho/-row` with regex and line-number
output, `sed -n` windows, `cat -n`), plus `code_search` and `list_directory` for discovery.
`read_files` was **unavailable in this build** (it rejected valid string arguments on every
attempt), so all file reading was done through bounded `sed`/`cat` windows and `code_search`
context flags. No AST parser, no pylint/ruff naming rules, no custom script was run.

### Deliberately skipped

- **Vendored/third-party, generated files, build output, lockfiles** — excluded per scope.
- **`CHANGELOG.md` (125 KB), `README.md` (47 KB), `docs/`** — skimmed only, for terminology and
  for cross-layer name consistency. I did **not** audit prose naming.
- **Commit-message hygiene** — out of scope per the brief.
- **`site/public/`** static assets, `harness/local_fit/` numeric kernels beyond the one numeric-
  suffix hit, and `examples/oc_handoff/worker.py` (1 004 LOC, an out-of-scope experimental
  handoff lane per `AGENTS.md`).
- **`tests/` internals** — dependents only, never targets.
- **Boiling the ocean was not attempted.** I sampled the top ~25 modules by import count and read
  every finding to its definition site. A file-by-file read of all 39 178 LOC would likely surface
  a handful more low-severity items; it would not change the top 10.

### Honest note on method

Every finding below was traced to a **definition site and at least one dependent call site**.
Where I could only establish a definition and not a use, I say so in the finding. I have tried to
keep the count of "maybes" low, and I have an explicit *Checked and deliberately NOT flagged*
section in `FINDINGS.md` covering five patterns I hunted for and did **not** find — including
`results.py`/`output.py`/`render.py`, which looked like a synonym cluster and turned out to be a
model of how to do this correctly.

---

## 2. Top 10 highest-value fixes

Effort: **S** ≈ under an hour · **M** ≈ half a day · **L** ≈ multi-day or spans a PR cycle.

| # | Fix | Findings | Effort | Why it's top |
|---|---|---|---|---|
| 1 | **Narrow and rename `JevPolicy.evaluate_*(state, …)`.** Six public methods take `state: Any` meaning six unrelated schemas, with *differing* key-probe orderings — the same dict yields different text depending on which method you called. Give each method a named, typed subject (`issue`, `log_item`, `repo_element`, `route_query`, `phase_evidence`). | L1-1, V-11 | **L** | Only finding in the audit with a live **correctness** hazard, not just a readability one. 26 importers of `jev_policy`, but the parameter is never serialized, so the blast radius is compile-time only. `evaluate_scope` already shows the right pattern 200 lines away. |
| 2 | **Split the `tier` word three ways** — `price_band` (str, `routing_table`), `complexity_tier` (int, `sliding_scale`), `lane` (str, `Router.route`'s key). | L1-2, L1-8, V-3, V-4 | **M** | `spec["tier"]` from `Router` can be an `int`, `"panel"`, or `"apply"`. 305 occurrences of the token; the actual code change is ~30 sites because most uses are local. |
| 3 | **Delete the dead `routing_table.classify_task_tier` / `next_tier` / `get_tier_route`.** | L1-3, V-3 | **S** | Cheapest high-value item on the list. Removes a second, incompatible definition of a name the live code owns, and proves it by having zero production callers. Also removes 52 lines (`routing_table.py:108-159`) and one test file. |
| 4 | **Rename the Jev `site` parameter to `judgment_kind`** — but **keep writing `structural.site` to the ledger**. | L1-4, V-5 | **M** | `site` currently means the Jev evaluation lane, the website (`SITE_ROOT` → `site/public`), a roadmap slice ID (`SITE-1`/`SITE-2`), and the URL `/api/site/demo-snapshot`. Parameter rename is cheap; the persisted key and the URL must not move. ⚠ **Now optional** — whether Jev `site` is deliberate house vocabulary is undetermined (open question 9), so confirm before spending the effort. |
| 5 | **Qualify `state` across the four non-Jev packages** — `dag_state`, `apply_run`, `continuation_state`, `resume_state`. | L1-5, V-12 | **S** | Pure parameter renames, no serialization. Small effort, high readability return, and it stops `pyramid_state`'s `Dict` from being confused with `apply_gate`'s attribute-bearing object. |
| 6 | **Converge `run_max_cost` / `run_budget` / `run_ceiling`** — Python symbols only. | L1-6, V-6, V-2 | **S–M** | The repo's own comment already equates all three. **Three names are public and frozen**, not one: the `--run-budget` flag, the `max_cost` HTTP key, and the `max_cost` MCP schema field. Only `run_ceiling` and `run_budget` are renameable. |
| 7 | **Converge the verb prefixes**: `evaluate_` over `assess_`; `validate_`/`verify`/`lint_` split. | V-1, V-7, L1-10, V-2 | **S** | 3 call sites for `assess_`. The wrapper `assess_completion_nouls` is a *pure delegating shim* over `evaluate_completion_nouls` — verified at `orchestrator.py:150-153` — so the second name adds no concept. |
| 8 | **Type `apply_edit(**kwargs)` / `apply_batch(files, **kwargs)`** behind an `ApplyRequest` dataclass. | L1-13 | **L** | The public engine entry point has an unnameable signature, and `GatePolicy` reads it via `getattr(req, "trust_combined", 0)` — the code does not know its own contract. Biggest single readability win, and the largest effort. Pair with #5, which names the same object. |
| 9 | **Split / rename `harness/waist.py`** into `plan_decomposition.py` + `plan_confirmation.py`. | L1-7 | **M** | 2 790 LOC and 15 importers, named after a roadmap metaphor that its own docstring immediately has to explain — and which covers only one of the two responsibilities listed. Big module, so a compatibility re-export matters. |
| 10 | **Rename `harness/osal.py` → `os_shim.py`**; add a one-line definition of `noul` to `harness/jev.py`. | L2-2, L2-3 | **S** | Both are cheap. `OSAL` is never expanded anywhere in the repo; `noul` appears ~153 times with its only definition in `docs/`. Neither is a rename I'd argue hard for — `noul` explicitly should **not** be renamed, only documented. |

**Not in the top 10 but worth scheduling:** `agent.py` → `chat_agent.py` (L2-1, **M** —
three in-repo importers, but check `docs/` for external references first), and the optional
`mcp._handle` handler-map refactor (L1-9, **S** — see the caveat in §4).

Total for items 1–10: roughly **2–3 weeks** of focused work, which is ~6 PRs at this repo's
apparent PR cadence. Items 2, 3, 5, 6 and 7 (≈ **M+S+S+S+S**) are the cheap 80% and touch only
internal symbols plus one flag that must not change.

---

## 3. Open questions for the repo owner

These need domain judgment I could not resolve from the code. **I did not stall on any of them** —
each has a documented default in the corresponding finding.

1. **`routing_table.py`'s price ladder (T0–T3) — is it live at all?** `classify_task_tier`,
   `next_tier` and `get_tier_route` have **zero production callers**; only `floor_model`,
   `strip_variant_suffix` and `classify_model_tier` are imported. Is the T0–T3 ladder intended as a
   fallback that was superseded by `sliding_scale`, or is it pending wiring? *Affects whether fix
   #3 is a deletion or a rename.* **My default: delete the three uncalled functions, keep the
   module for its three live helpers.**

2. **Is `JevPolicy`'s `state` polymorphism load-bearing for external callers?** The `str` shorthand
   and the five-key probe lists look like deliberate ergonomics, not accidents. **My default:
   assume ergonomic, keep a `str` fast path, but make the dict schema exact and per-method.**

3. **Which of `tier` / `rung` / `lane` is the operator-facing term?** The prose mixes all three
   ("de-escalates back to the last **tier**" in a rung paragraph). The answer determines whether
   docs or code moves first. **My default: `rung` in code, `tier` retired.**

4. **Is `Router.route()`'s return dict serialized?** `{"tier": "panel"|"apply", …}` may reach plan
   JSON or the MCP surface. **I could not verify this and it gates the safety of fix #2.** My
   default: emit both `lane` and `tier` keys for one release. *Please confirm.*

5. **Should `harness/agent.py` be renamed?** It is a plausible external import path
   (`from harness.agent import …`). Three in-repo importers. **My default: rename with a shim.**

6. **Is `noul` the term you want outsiders to learn, or internal jargon?** I recommend *not*
   renaming it and instead defining it in `harness/jev.py`. If it is meant to stay internal, a
   plain-English alias in the public `JevPolicy` docstrings would be the alternative.

7. **Is the `waist` metaphor load-bearing for the team?** Splitting `waist.py` (fix #9) only makes
   sense if "waist" is a roadmap-era name rather than current vocabulary. **My default: assume it
   is historical; split and re-export.**

8. **Is the ledger's `structural.site` key under external consumption?** Fix #4 deliberately leaves
   it alone, but if external dashboards group by it, the parameter rename should ship with a
   documented mapping rather than silently.

9. **Is `site` a reserved house word, or an accident?** *(New in the correction pass — this is the
   inference flagged in `FINDINGS.md` L1-4, and it decides whether fix #4 is worth doing.)* The
   Proof Bench tracks are genuinely named `SITE-1` (`site_export.py:1`), `SITE-2`
   (`route_pack.py:1`) and `SITE-5` (`site/worker/migrations/0001_init.sql:1`). If `site` is
   deliberate vocabulary for a *track or venue*, then `site="model_route"` is consistent house
   style and L1-4's recommended rename is wrong. If it is not, the rename stands. **My default:
   treat the rename as optional; the three-way collision itself is real either way.**

10. **Is `Router.route()`'s `{"tier": "panel"}` key a typo, or a third sense of "tier"?**
    *(New in the correction pass — the inference flagged in `FINDINGS.md` L1-8.)* I read it as a
    mis-keyed **lane**, from the method's own docstring (`router.py:101`, *"cheap lane first"*).
    The competing reading is that `tier` here means "which subsystem takes this task". I searched
    and found **no in-repo consumer** of `route()`'s return value, so the code cannot settle it.
    **My default: keep the `lane` key but ship both during a transition.**

---

## 4. Things I was uncertain about

Stated plainly, because the audit's value depends on knowing where its edges are.

- **Severity ranking is a judgement call in three places.** I rated `mcp._handle` Low-Medium
  rather than Medium because a single JSON-RPC dispatcher is a well-established convention; I
  rated `agent.py`/`orchestrator.py` Low-Medium because the collision is in docstrings rather than
  code; and I rated the `results/output/render` trio as *not a finding at all*. A reviewer
  applying a stricter bar would push the first two up and the third into the report.
- **`read_files` being unavailable** cost me some precision. Reading through `sed` windows meant I
  saw bounded regions rather than whole files; I may have missed a def or two in the long tail of
  `harness/`. It does not affect the top 10 — each of those was traced to its definition and read
  in surrounding context.
- **No AST-based analysis.** A `pylint`/`ruff` naming pass would mechanically surface things my
  regexes cannot: shadowed builtins, argument counts, functions whose name doesn't match their
  single return path. My findings are all *semantic* (name vs. actual meaning), which regexes
  happen to be good at, but a mechanical pass is a genuine gap in coverage.
- **Blast-radius counts are import-graph depth 1 only.** I counted `from harness.X import` /
  `from .X import` occurrences. Transitive dependents (e.g. everything that imports `cli.py`) are
  undercounted, so the real blast radius of a `router.py` or `config.py` rename is larger than
  stated.
- **I did not verify the 30 CLI subcommand names for synonymy** (e.g. `brief` vs `waist-brief`,
  `defer` vs `defer-stats`, `route` vs `plan`). The CLI is the public surface and deserves its own
  pass; I judged it out of reach for this audit rather than out of scope.
- **Two findings rest on docstring assertions I took at face value:** that `results.py`/`output.py`/
  `render.py` are cleanly separated, and that `spend.py`/`token_budget.py` split dollars from
  tokens. Both docstrings are unusually explicit and I found no code contradicting them, but I
  sampled rather than exhaustively traced.
- **`state` = 574 occurrences repo-wide** is a token count, not a finding count. The 574 splits
  across at least **seven** distinct concepts (L1-1, L1-5, plus the two persisted surfaces in the
  risk register) plus legitimate uses; I did not classify every one.
- **The correction pass fixed defects; it did not re-audit.** I closed the gaps a verification pass
  named — the two unchecked risk dimensions, the two missing count sets, the two unlabelled
  inferences, the misfiled finding, three bad citation blocks and three bad quantities. I did
  **not** re-sweep the source for new findings, and I have not re-verified the ~85 line
  references the pass did not touch. Treat the audit's coverage as sampled, not exhaustive —
  §1 says why.

---

## 5. One-paragraph read for the repo owner

This codebase is **disciplined about naming in its documentation and loose about it in its
parameters.** The docstrings are unusually good — several of them are effectively glossary
entries, and `results.py` / `output.py` / `render.py` / `spend.py` / `token_budget.py` are models
of what "one concept, one owner" looks like in this repo. The pain is concentrated in two places:
the `JevPolicy` evaluation methods, where a single `state: Any` parameter quietly means six
different subjects with six different key-probe orders (the one finding with a correctness
hazard), and the routing vocabulary, where `tier` is an `int` in one module, a `str` in another,
and a lane name in a third, and two different functions answer to `classify_task_tier`. Both are
fixable with renames plus type narrowing — the *code* is not the problem; the *parameters* are. The
cheapest real win is deleting 52 lines of provably-dead duplicate in `routing_table.py:108-159`, which
also dissolves the `classify_task_tier` collision for free.
