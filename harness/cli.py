"""Command-line interface.

Subcommands (flags are back-compatible with SCMessenger's fusion_lite.py and
morph_lite.py, plus delegate_task.py's --verify/--max-rounds):

  harness verify   panel + judge verification (with model rotation + consensus)
  harness apply    scoped code edit + verify loop + consent continuation
  harness offer    ask a model for consent on a work item
  harness defer    record a mid-task deferral / consent revocation
  harness continue continue a deferred/incomplete apply task
   harness ledger   autonomy ledger: tail | verify | report
   harness models   list live free OpenRouter models
   harness spend    key identity & spend status
   harness trust    trust & correctness standing (read-only)
  harness bench    run a manifest of known-answer tasks through the free tier

Free tier is the default: `--free`/HARNESS_USE_FREE routes everything through
the best current free models, rotating and deferring instead of failing.

Logs go to stderr; the JSON result goes to stdout (or --out <file>).
Exit codes: 0 success, 1 fatal refusal/error, 2 verification failed,
3 deferred (capability/consent) -- safe to continue.
"""
import functools
import json
import os
import subprocess
import tempfile
import time
from .consent import probe_consent
from .config import resolve_hourglass
from .errors import HarnessError
from .spend import discover_free_models
from .filesafety import VERIFY_TIMEOUT, validate_target_file, validate_verify_command
from .output import eprint
from .session import (apply_session as _session, governor_for as _governor,
                      jev_face_governor,
                      ledger_for as _ledger,
                      run_meta as _session_run_meta)
from .service import prepare_verify as _prepare_verify
from .service import run_verify as _service_verify
from .service import read_text_file as _service_read_text
from .rankings import build_rankings_report as _rankings_report
from .route_pack import validate_route_pack
from .site_export import export_bundle as _site_export_bundle
from .site_export import write_bundle as _site_export_write
from .waist import compose_plan as _compose_plan
from .jev import jev_cost
from .jev_policy import JEV_MAX_INPUT_TOKENS, aggregate_structural, policy_for
from .jev_completion import dogfood_phase, score_all_phases
from .jev_packs import (validate_log_pack, validate_operator_pack,
                        validate_repo_summary_pack)
from .repo_summary import analyze_repo as _repo_analyze
from .repo_summary import render_repo_map as _repo_render_map
from .repo_summary import write_envelope as _repo_write_envelope
from .repo_summary import write_map as _repo_write_map
from .log_analysis import analyze_log as _log_analyze, load_log_text as _log_load_text, write_analysis as _log_write_analysis
from .mission_driver import pack_probe_attempt as _mission_pack_probe
from .mission_driver import run_mission as _mission_run
from .capability import capabilities_payload as _capability_payload_owner
from .brief import build_brief, validate_brief
from .dag import TaskDAG, node_apply_kwargs
from .executor import DEFAULT_PLAN_WORKERS, PlanExecutor
from .pyramid_state import (
    dag_for_pending, load_state, node_routes_for_pending, persist_state)
from .results import terminal_exit_code
from .saturation import advise
import sys
import uuid

from ._http import HttpTransport
from .apply import validate_continuation
from .batch import BatchOptions
try:
    from .bench import load_manifest, run_bench
except Exception as _bench_import_exc:
    # Self-hosting resilience: a deferred self-edit may leave
    # harness/bench.py unimportable (SyntaxError included -- hence the
    # broad catch), and that must not break unrelated commands, notably
    # `continue`, which resumes exactly such states. Only `bench` itself
    # may fail, at use time, as a clean HarnessError (see _cmd_bench).
    # (Bound under a different name: `except ... as e` deletes e on exit.)
    load_manifest = run_bench = None
    _bench_import_error = _bench_import_exc
else:
    _bench_import_error = None
from .claims import (
    build_claims_prompt, curate_claims_from_ledger, load_claims_manifest,
    load_definitions_file,
)
from .claims import parse_claims
from .config import load_settings, shipped_model_ids
from .cli_parser import build_parser
from .cli_report import _emit, _emit_by_status, _print_capabilities_table, _print_cost_table


def _split_opt_list(value):
    """Parse a comma-separated CLI list into a clean list of model ids."""
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def _read_text(path, what):
    """CLI alias for the ONE BOM-tolerant reader (service.read_text_file).
    utf-8-sig: Windows tooling (PowerShell ``>`` redirects) emits BOM'd text;
    a leading U+FEFF would corrupt --prompt-file/--source-file input and make
    handoff JSON files fail to parse. Kept as a named seam so existing tests
    and callers keep their patch point."""
    return _service_read_text(path, what)


def _read_json(path, what):
    """Read a JSON file, presenting missing files and parse errors cleanly."""
    text = _read_text(path, what)
    try:
        return json.loads(text)
    except ValueError as e:
        raise HarnessError(f"{what} is not valid JSON: {path} ({e})") from e


# Composition lives in harness/session.py (the ONE owner); the aliases below
# keep the historical cli seams for commands and tests that patch them.


def _run_claims_verify(settings, *, prompt, task_id=None, max_tokens=None,
                       reasoning_effort=None, converge=False, judge=None,
                       convergence_model=None, specialist_pool=None,
                       reassurance_claims="", panel=None, max_cost=None):
    """Claims-verify execution via the canonical service layer. Kept as a
    named seam because `dogfood` composes it and tests patch it; the
    assembly itself (governor, ledger, pre-run look-ahead, panel_judge
    kwargs, cancelled envelope, cost/meta) has ONE owner: service.run_verify.
    Interfaces only prepare inputs and present results. Panel ordering
    (catalog seed, capability sort, degrade-to-given-order) is the panel
    lane's own job."""
    return _service_verify(
        settings, prompt=prompt, task_id=task_id,
        max_cost=max_cost, judge=judge, reasoning_effort=reasoning_effort,
        max_tokens=max_tokens, panel=panel, converge=converge,
        convergence_model=convergence_model,
        specialist_pool=specialist_pool,
        reassurance_claims=reassurance_claims)


def _run_meta(settings, gov):
    """CLI seam for session.run_meta (the ONE owner); kept so existing
    tests and callers keep their patch point."""
    return _session_run_meta(settings, gov)


def _cmd_verify(opts, settings):
    # P0 structured-claims mode: lint + auto-expand BEFORE any network call, so
    # ONE owner for prompt assembly (the canonical service layer): file
    # reading, manifest parsing, grounding, and convergence/polarity
    # derivation all live in service.prepare_verify. The CLI keeps only
    # presentation -- lint printing, the rejected exit, and the reassurance
    # banner. Error wording is preserved verbatim at the CLI boundary.
    if not (opts.claims_file or opts.prompt_file or opts.prompt):
        raise HarnessError(
            "verify requires --prompt-file/--prompt or --claims-file.")
    if opts.claims_file and not opts.source_file:
        raise HarnessError("verify --claims-file requires --source-file "
                           "(the verbatim code window the panel will review).")
    if opts.prompt is not None and not opts.prompt.strip():
        raise HarnessError("prompt is empty.")
    prepared = _prepare_verify(
        prompt=opts.prompt, prompt_file=opts.prompt_file,
        claims_file=opts.claims_file, source_file=opts.source_file,
        definitions_file=opts.definitions_file,
        claim_context=opts.claim_context)
    prompt = prepared["prompt"]
    claims_lint = prepared["claims_lint"]
    if claims_lint is not None:
        if not claims_lint["ok"]:
            for issue in claims_lint["issues"]:
                eprint(f"[claims-lint] {issue['severity'].upper()} "
                       f"{issue['code']}: {issue['message']}")
            _emit({"status": "rejected", "lint": claims_lint}, opts.out)
            sys.exit(2)
        # structured-claims mode always runs the convergence gate (deterministic
        # tally) and derives the polarity map from the manifest kinds.
        opts.converge = prepared["converge"]
        opts.reassurance_claims = prepared["reassurance_claims"]
    result = _run_claims_verify(
        settings, prompt=prompt, task_id=opts.task_id,
        max_tokens=opts.max_tokens, reasoning_effort=opts.reasoning_effort,
        converge=opts.converge, judge=opts.judge,
        convergence_model=opts.convergence_model,
        specialist_pool=(_split_opt_list(opts.specialist_pool)
                         if opts.specialist_pool else None),
        reassurance_claims=opts.reassurance_claims, max_cost=opts.max_cost)
    if claims_lint is not None:
        result["claims_grounding"] = {
            "ok": claims_lint["ok"],
            "issues": claims_lint["issues"],
            "expansions": claims_lint["expansions"],
        }
    # Terminal honesty: a fail-closed run on a saturated tier says so plainly
    # (one policy owner, harness/saturation.py).
    advise(panel_failures=result.get("panel_failures"))
    _emit(result, opts.out)


def _cmd_dogfood(opts, settings):
    """Self-hosting loop as one command: audit the harness with the harness.

    Three fail-closed phases, each reusing its existing lane:
      1. GROUND  -- hermetic claims lint of the fixture vs its source window
                    (no network; an ungrounded claim never reaches a model).
      2. VERIFY  -- live panel + convergence tally; the defect must be
                    panel-confirmed (`converged` = every required slot voted)
                    before any edit is attempted.
      3. APPLY   -- self-edit via ApplyEngine, the operator's verify command
                    as the gate; a failed run leaves the tree untouched.
    Any phase that cannot prove its precondition stops the run with the
    phase's own evidence. Exit 0 only if every phase proved its claim.
    """
    if opts.from_ledger:
        if opts.claims_file:
            raise HarnessError(
                "--from-ledger curates the claims manifest from the ledger; "
                "it cannot be combined with --claims-file")
        # Curation reads the ledger, not the live key. The curated manifest is
        # always persisted via --claims-out: the dry run is step one of the
        # chain (curate -> inspect -> dogfood --claims-file <that file>).
        if not opts.claims_out:
            raise HarnessError("--from-ledger requires --claims-out: the curated "
                               "manifest must be persisted for the next run")
        seed_ledger = _ledger(settings)
        manifest, evidence = curate_claims_from_ledger(
            seed_ledger.entries(), window=opts.evidence_window,
            max_claims=opts.max_claims)
        if not manifest["claims"]:
            _emit({"status": "nothing_to_audit", "evidence": evidence}, opts.out)
            eprint("[dogfood] the ledger evidence is not curation-worthy "
                   "(no repeated model failures). Nothing to audit.")
            sys.exit(0)
        fixture = json.dumps(manifest, indent=2)
        seed_ledger.append("dogfood_curate", task_id=opts.task_id or "dogfood-curate",
                           claims=len(manifest["claims"]),
                           evidence=evidence)
        # The verbatim source window the panel reviews IS the evidence file:
        # the manifest's provenance and rule ranking stay in scope.
        source_text = json.dumps(evidence, indent=2)
        source_path = os.path.join(
            tempfile.mkdtemp(prefix="harness-dogfood-"), "evidence.json")
        with open(source_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(source_text)
        eprint(f"[dogfood] curated {len(manifest['claims'])} claim(s) from the "
               f"ledger; evidence window written to {source_path}")
        manifest_ctx, claims = parse_claims(manifest)
        quoted = source_text
    else:
        if not opts.claims_file or not opts.source_file:
            raise HarnessError(
                "dogfood requires --claims-file and --source-file, or --from-ledger")
        manifest_ctx, claims = load_claims_manifest(opts.claims_file)
        quoted = _read_text(opts.source_file, "--source-file")
    if opts.from_ledger:
        with open(opts.claims_out, "w", encoding="utf-8", newline="\n") as f:
            f.write(fixture)
        eprint(f"[dogfood] curated manifest written to {opts.claims_out}")
    defs = load_definitions_file(opts.definitions_file) if opts.definitions_file else {}
    context = opts.claim_context if opts.claim_context is not None else manifest_ctx
    prompt, lint = build_claims_prompt(claims, quoted, source_index=defs,
                                       context=context)
    # Preflight the apply-phase inputs BEFORE any phase runs: a typo'd target
    # path or gate command must fail here, hermetically -- not after the
    # panel has been paid for.
    validate_target_file(opts.file)
    validate_verify_command(opts.verify)

    report = {"phases": []}

    def _phase(name, payload):
        # One emit site per dogfood phase: stream event AND report record.
        _emit({"dogfood_phase": name, **payload}, None)
        report["phases"].append({"phase": name, **payload})

    if not lint["ok"]:
        _phase("ground", {"status": "rejected", "lint": lint})
        report["status"] = "ungrounded"
        _emit(report, opts.out)
        sys.exit(2)
    _phase("ground", {"status": "ok", "claims": len(claims)})

    # ---- phase 2: live panel verify (defect must be panel-confirmed) ------
    reassurance = ",".join(c.claim_id for c in claims if c.kind == "reassurance")
    verdict = _run_claims_verify(
        settings, prompt=prompt, task_id=opts.task_id,
        converge=True, reassurance_claims=reassurance,
        max_cost=opts.max_cost)
    tally = verdict.get("convergence", {}).get("tally") or {}
    per_claim = tally.get("claims", {})
    confirmed = sorted(cid for cid, c in per_claim.items()
                       if c.get("converged") and c.get("verdict") == "real")
    # Same policy owner as verify: a saturated tier fail-closes the phase and
    # says why in plain language, and the report carries the verdict.
    saturated = advise(panel_failures=verdict.get("panel_failures"))
    if saturated:
        report["saturated"] = True
    _phase("verify", {"status": "ok", "confirmed_claims": confirmed,
                      "tally": tally})
    if not confirmed:
        report["status"] = "not_confirmed"
        report["verify"] = verdict
        _emit(report, opts.out)
        eprint("[dogfood] no defect survived the panel tally; nothing to apply.")
        sys.exit(3)
    report["verify"] = verdict

    # ---- phase 3: gated self-apply ----------------------------------------
    engine = _session(settings)
    result = engine.apply_batch(
        [opts.file], task_id=opts.task_id,
        options=BatchOptions(
            instruction=opts.instruction, verify_cmd=opts.verify,
            max_rounds=opts.max_rounds,
            require_consent=opts.require_consent, model=opts.model,
            max_tokens=opts.max_tokens,
            task_max_cost=opts.task_max_cost,
            allow_escalation=opts.allow_escalation,
            reasoning_effort=opts.reasoning_effort,
            renew_consent=opts.renew_consent,
            require_diff_authorization=getattr(
                opts, "require_diff_authorization", None),
            max_rotations=opts.max_rotations, backend=opts.backend,
            max_lines=opts.max_lines))
    _phase("apply", {"status": result["status"], "cost": result.get("cost")})
    report["apply"] = result
    report["status"] = ("ok" if result["status"] == "ok"
                        else "incomplete")
    _emit(report, opts.out)
    # Same status-meaning policy as _emit_by_status: one def site (results.py).
    code = terminal_exit_code(result["status"])
    if code:
        sys.exit(code)


def _cmd_brief(opts, settings=None):
    """Build the grounded context pack (MR-8 spec). Hermetic: no key, no
    network; the pack asserts nothing beyond the goal."""
    pack = build_brief(opts.goal, opts.files)
    out = {"brief": pack}
    if opts.validate:
        out["grounding_issues"] = validate_brief(pack)
        out["ok"] = not out["grounding_issues"]
    _emit_by_status(out, opts.out)


def _cmd_lint_claims(opts, settings=None):
    """Hermetic claim linting: no network, no key. Exits 2 on error issues."""
    manifest_ctx, claims = load_claims_manifest(opts.claims_file)
    quoted = _read_text(opts.source_file, "--source-file")
    defs = load_definitions_file(opts.definitions_file) if opts.definitions_file else {}
    context = opts.claim_context if opts.claim_context is not None else manifest_ctx
    prompt, report = build_claims_prompt(claims, quoted, source_index=defs,
                                         context=context)
    out = {"ok": report["ok"], "issues": report["issues"],
           "expansions": report["expansions"],
           "window_lines": len(quoted.splitlines()) if quoted.strip() else 0}
    if opts.show_prompt:
        out["prompt"] = prompt
    _emit(out, opts.out)
    for issue in report["issues"]:
        eprint(f"[claims-lint] {issue['severity'].upper()} "
               f"{issue['code']}: {issue['message']}")
    if not report["ok"]:
        sys.exit(2)


def _batch_options(opts, continuation=None):
    """The per-file session options for apply and continue, built once."""
    return BatchOptions(
        instruction=opts.instruction, edit_snippet=opts.edit_snippet,
        verify_cmd=opts.verify, max_rounds=opts.max_rounds,
        require_consent=opts.require_consent, model=opts.model,
        max_tokens=opts.max_tokens, task_max_cost=opts.task_max_cost,
        allow_escalation=opts.allow_escalation,
        reasoning_effort=opts.reasoning_effort, renew_consent=opts.renew_consent,
        require_diff_authorization=getattr(
            opts, "require_diff_authorization", None),
        max_rotations=opts.max_rotations, backend=opts.backend,
        verify_only=opts.verify_only, max_lines=opts.max_lines,
        continuation=continuation)


def _cmd_apply(opts, settings):
    # Validate persisted state before key/model setup. A malformed or ungated
    # continuation must fail without even fetching /key or /models.
    continuation = None
    if opts.continue_from:
        continuation = validate_continuation(_read_json(opts.continue_from,
                                                        "--continue-from state"))
    if not opts.file and not continuation:
        raise HarnessError("apply requires --file (or --continue-from <state.json>)")

    engine = _session(settings)
    # Multi-file batch (#12): repeated --file flags run one governed session
    # per file through the same engine/router/gate, sharing the task budget.
    files = opts.file if isinstance(opts.file, list) else ([opts.file] if opts.file else [])
    if not files and not continuation:
        raise HarnessError("apply requires --file (repeatable for multi-file batches)")
    # The engine owns the batch loop (and, on resume, replaces the file list
    # with the continuation's own target).
    result = engine.apply_batch(
        files or [None], task_id=opts.task_id,
        options=_batch_options(opts, continuation),
        keep_going=opts.keep_going)
    result["meta"] = _run_meta(settings, engine.governor)
    _emit_by_status(result, opts.out)


def _cmd_continue(opts, settings):
    # As with --continue-from, reject missing verification authority before any
    # OpenRouter key/model lookup. `validate_continuation` also unwraps a full
    # persisted result object for CLI callers.
    continuation = validate_continuation(
        _read_json(opts.state, "--state continuation"))
    engine = _session(settings)
    result = engine.apply_batch(
        [None], task_id=opts.task_id,
        options=_batch_options(opts, continuation))
    _emit_by_status(result, opts.out, continued=True)


def _cmd_offer(opts, settings):
    api_key, gov = _governor(settings)
    ledger = _ledger(settings)
    result = probe_consent(
        transport=HttpTransport(), api_key=api_key, governor=gov,
        task_id=opts.task_id or uuid.uuid4().hex[:8], task=opts.task,
        model=opts.model or settings.judge, context=opts.context,
        ledger=ledger, required=True, fallback_pool=settings.panel_pool,
        min_confidence=settings.min_confidence)
    _emit(result, opts.out)


def _cmd_defer(opts, settings):
    ledger = _ledger(settings)
    entry = ledger.append("defer_midtask", task_id=opts.task_id, reason=opts.reason,
                          category=opts.category or None, model="(deferral)")
    _emit({"status": "deferred", "task_id": opts.task_id, "reason": opts.reason,
           "category": opts.category, "ledger_entry": entry}, None)


def _cmd_issue_sort(opts, settings):
    """Thin JEV-P5 face: load the operator pack, call the ONE policy owner,
    emit combo + structural. No second Jev client, no invented buckets."""
    raw_pack = _read_json(opts.pack, "issue-sort pack")
    pack = validate_operator_pack(raw_pack)
    ledger = _ledger(settings)
    # CLI/MCP/GUI parity: a keyed face composes the shared spend governor
    # (session.jev_face_governor) so preflight can reserve, instead of
    # silently degrading to keyword fallback on a keyed machine. Unkeyed
    # runs stay governor-free and hermetic.
    gov = jev_face_governor(settings, getattr(opts, "max_cost", None))
    jev_policy = policy_for(settings, transport=HttpTransport(),
                            governor=gov, ledger=ledger)
    result, structural, combo = jev_policy.evaluate_issue_sort(
        {"issue": opts.issue}, pack, site="issue_sort")
    envelope = {
        "status": "ok" if combo.get("bucket") else "unmatched",
        "combo": combo,
        "structural": structural,
        "answers": result.answers,
        "reasons": result.reasons,
        "is_fallback": bool(combo.get("is_fallback")),
        "pack_id": combo.get("pack_id"),
        "bucket": combo.get("bucket"),
        "path_id": combo.get("path_id"),
        "suggested_next_action": combo.get("suggested_next_action"),
    }
    _emit(envelope, opts.out)


def _cmd_log_judgment(opts, settings):
    """Thin JEV-LOG face: parse ($0), judge every item via the ONE policy
    owner, aggregate to JSON. No second Jev client; unmatched stays honest."""
    raw_pack = _read_json(opts.pack, "log pack")
    try:
        pack = validate_log_pack(raw_pack)
    except ValueError as exc:
        raise HarnessError(f"log pack invalid: {exc}") from exc
    log_text = _log_load_text(opts.log)
    # Same composition as issue-sort/route: keyed runs get the shared spend
    # governor (CLI/MCP/GUI parity) so the JEV-LOG dogfood can judge live;
    # --max-cost bounds the run, unkeyed stays hermetic with no governor.
    gov = jev_face_governor(settings, getattr(opts, "max_cost", None))
    jev_policy = policy_for(settings, transport=HttpTransport(),
                            governor=gov, ledger=_ledger(settings))
    analysis = _log_analyze(
        log_text, pack, jev_policy,
        info_sample=int(getattr(opts, "info_sample", 0) or 0),
    )
    if getattr(opts, "save_to", None):
        _log_write_analysis(analysis, opts.save_to)
    _emit(analysis, opts.out)


def _cmd_route(opts, settings):
    """SITE-2 face: route a user query onto the declared model ladder.

    The ladder pack comes from --pack (operator/config owned, never
    hardcoded here); unkeyed runs answer via the deterministic tier
    heuristic with is_fallback=true. Same composition shape as issue-sort:
    policy + ledger only.
    """
    try:
        pack = validate_route_pack(_read_json(opts.pack, "route pack"))
    except ValueError as exc:
        raise HarnessError(f"route pack invalid: {exc}") from exc
    ledger = _ledger(settings)
    # CLI/MCP/GUI parity: keyed runs compose the shared spend governor so
    # preflight can reserve (see session.jev_face_governor).
    gov = jev_face_governor(settings, getattr(opts, "max_cost", None))
    jev_policy = policy_for(settings, transport=HttpTransport(),
                            governor=gov, ledger=ledger)
    result, structural, combo = jev_policy.evaluate_model_route(
        {"goal": opts.goal}, pack, site="model_route")
    envelope = {
        "status": "ok" if combo.get("rung_id") else "unroutable",
        "route": combo,
        "structural": structural,
        "answers": result.answers,
        "reasons": result.reasons,
        "is_fallback": bool(combo.get("is_fallback")),
        "pack_id": combo.get("pack_id"),
        "rung_id": combo.get("rung_id"),
        "tier": combo.get("tier"),
        "model": combo.get("model"),
        "cost_class": combo.get("cost_class"),
    }
    _emit(envelope, opts.out)


def _repo_summary_policy_factory(settings, chunk_cap, run_budget, ledger):
    """Compose the ONE policy owner for JEV-P6, chunked under HARD_MAX_COST.

    Each governor this face composes is bounded by ``jev_face_governor``
    (``settings.max_cost`` or ``--max-cost``; anything above HARD_MAX_COST
    is refused fail-closed). The operator's explicit ``--run-budget`` bounds
    the CUMULATIVE actual spend of the whole run across chunks: the factory
    returns ``None`` when the budget cannot cover one more worst-case call
    and the driver stops honestly with ``stop_reason=run_budget``. Unkeyed
    runs compose a governor-free hermetic policy and never stop early.
    """
    state = {"gov": None, "policy": None}

    def make_policy(cumulative: float):
        if not getattr(settings, "jev_api_key", None):
            if state["policy"] is None:
                state["policy"] = policy_for(
                    settings, transport=HttpTransport(), governor=None,
                    ledger=ledger)
            return state["policy"]
        worst = jev_cost(JEV_MAX_INPUT_TOKENS)
        if (run_budget is not None
                and float(cumulative) + worst > float(run_budget)):
            return None
        if state["gov"] is None or state["gov"].remaining() < worst:
            gov = jev_face_governor(settings, chunk_cap)
            if gov is None:
                return None
            state["gov"] = gov
            state["policy"] = policy_for(
                settings, transport=HttpTransport(), governor=gov,
                ledger=ledger)
        return state["policy"]

    return make_policy


def _cmd_repo_summary(opts, settings):
    """JEV-P6 face: whole-repo inventory ($0) + keyed element judgments via
    the ONE policy owner (preflight -> typed call -> settle + one jev_eval
    per element), aggregated to an envelope JSON and a REPO-MAP render."""
    raw_pack = _read_json(opts.pack, "repo-summary pack")
    try:
        pack = validate_repo_summary_pack(raw_pack)
    except ValueError as exc:
        raise HarnessError(f"repo-summary pack invalid: {exc}") from exc
    ledger = _ledger(settings)
    run_budget = getattr(opts, "run_budget", None)
    state_path = getattr(opts, "state", None)
    if not state_path and getattr(opts, "save_to", None):
        state_path = str(opts.save_to) + ".judgments.jsonl"
    make_policy = _repo_summary_policy_factory(
        settings, getattr(opts, "max_cost", None), run_budget, ledger)
    envelope = _repo_analyze(
        opts.root, pack, make_policy,
        state_path=state_path,
        file_limit=getattr(opts, "limit", None),
        symbol_limit=int(getattr(opts, "symbols", 200) or 0),
        run_budget=run_budget,
        task_id="repo-summary",
        exclude=[getattr(opts, "save_to", None),
                 getattr(opts, "map", None), state_path],
    )
    written = {}
    if getattr(opts, "save_to", None):
        written["envelope"] = _repo_write_envelope(envelope, opts.save_to)
    if getattr(opts, "map", None):
        written["map"] = _repo_write_map(_repo_render_map(envelope), opts.map)
    report = {key: envelope[key] for key in
              ("schema", "generated_at", "pack_id", "coverage", "spend",
               "axes", "attention", "nouls", "structural")}
    report["written"] = written
    report["state_path"] = state_path
    _emit(report, opts.out)


def _cmd_site_export(opts, settings):
    """SITE-1 face: verified ledger -> sanitized site-bundle-v1 JSON.

    Fail-closed end to end: refuses without --yes (the operator's explicit
    public-release affirmation), refuses on a broken chain, and scans the
    output for credential shapes before a single byte lands on disk.
    """
    if not opts.yes:
        raise HarnessError(
            "site-export publishes evidence beyond this machine; pass --yes "
            "together with a consent record you actually signed")
    import harness as _harness_pkg
    bundle = _site_export_bundle(
        opts.ledger, opts.consent, pricing_path=opts.pricing,
        harness_version=_harness_pkg.__version__)
    size = _site_export_write(bundle, opts.out)
    eprint(f"[site-export] bundle {bundle['bundle_id']} written: {opts.out} "
           f"({size} bytes, {bundle['totals']['runs']} runs, "
           f"truncated={bundle['truncated']})")


def _cmd_ledger(opts, settings):
    if opts.ledger_cmd == "tail" and opts.n < 1:
        # tail(0) is [-0:] == the whole ledger; negative n slices from the
        # front -- both silent nonsense (same class as models --limit).
        raise HarnessError("ledger tail count must be a positive integer")
    if opts.ledger_cmd == "defer-stats" and opts.window < 1:
        raise HarnessError("defer-stats window must be a positive integer")
    ledger = _ledger(settings)
    if opts.ledger_cmd == "tail":
        _emit({"entries": ledger.tail(opts.n), "count": len(ledger.entries()),
               "chain": ledger.chain_status()}, opts.out)
    elif opts.ledger_cmd == "verify":
        ok, bad = ledger.verify()
        _emit({"verified": ok, "first_bad_seq": bad,
               "chain": ledger.chain_status()}, opts.out)
    elif opts.ledger_cmd == "repair":
        kept, dropped = ledger.repair()
        _emit({"repaired": dropped > 0, "kept": kept, "dropped": dropped},
              opts.out)
    elif opts.ledger_cmd == "defer-stats":
        _emit(ledger.defer_stats(window=opts.window), opts.out)
    elif opts.ledger_cmd == "report":
        from . import trust as trust_policy
        report = ledger.participation_report()
        report["trust"] = trust_policy.trust_status(report)
        _emit(report, opts.out)


def _cmd_bench(opts, settings):
    if load_manifest is None or run_bench is None:
        raise HarnessError(
            f"bench unavailable: harness/bench.py failed to import "
            f"({_bench_import_error}); restore or repair it first")
    engine = _session(settings, opts.max_cost)
    tasks = load_manifest(opts.manifest)
    for t in tasks:
        if opts.require_consent:
            t.setdefault("require_consent", True)
        if opts.max_rounds:
            t.setdefault("max_rounds", opts.max_rounds)
    report = run_bench(engine, tasks)
    _emit(report, opts.out)
    st = report["bench"]["statuses"]
    if st.get("ok", 0) < len(tasks):
        eprint("[bench] some tasks did not pass; see report.")
        if st.get("deferred") or st.get("consent_blocked"):
            sys.exit(3)
        sys.exit(2)


def _cmd_models(opts, settings):
    if opts.limit < 1:
        raise HarnessError("--limit must be a positive integer")
    api_key, gov = _governor(settings)
    transport = HttpTransport()
    ids = discover_free_models(transport, api_key, prefer=settings.panel_pool,
                               limit=opts.limit)
    if opts.all:
        # A different cache intent (full catalog, not the free-only filter),
        # but the same verified key -- no second governor needed.
        rows = []
        for m in gov.fetch_models(refresh=True):
            p = m.get("pricing") or {}
            def _p(v):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
            rows.append({"id": m.get("id"),
                         "context": m.get("context_length"),
                         "prompt_price": _p(p.get("prompt")),
                         "completion_price": _p(p.get("completion"))})
        rows.sort(key=lambda r: r["id"] or "")
        ids = [r["id"] for r in rows]
    else:
        rows = ids
    _emit({"free_only": not opts.all, "models": rows, "count": len(rows),
           "meta": {"source": "openrouter:/models", "fetched_at": time.time(),
                    "use_free": settings.use_free}}, opts.out)


def _cmd_spend(opts, settings):
    api_key, gov = _governor(settings)
    status = gov.key_status()
    # Enrichment (Phase 1): the session-level budget view the governor
    # enforces, alongside the key-level data -- the same numbers a UI
    # dashboard needs, from the one owner.
    status["session"] = {"spent": gov.spent, "ceiling": gov.max_cost,
                         "remaining": max(0.0, gov.max_cost - gov.spent)}
    _emit(status, opts.out)


def _cmd_cost(opts, settings):
    """Cost observability snapshot: track spend by tier, model, and calculate savings."""
    ledger = _ledger(settings)
    report = ledger.cost_report(
        window=getattr(opts, "last", None),
        by_tier=bool(getattr(opts, "by_tier", False)),
        by_model=bool(getattr(opts, "by_model", False)),
        savings=bool(getattr(opts, "savings", False)),
    )
    if not getattr(opts, "json", False):
        _print_cost_table(report)
    _emit(report, opts.out)


def _cmd_mission(opts, settings):
    """HUL-A/D mission pack CLI: init | status | resume | findings | run."""
    from . import mission_record as mr
    cmd = getattr(opts, "mission_cmd", None)
    if cmd == "init":
        in_scope = _split_opt_list(getattr(opts, "in_scope", ""))
        out_scope = _split_opt_list(getattr(opts, "out_of_scope", ""))
        spec = mr.build_mission_spec(
            mission_id=opts.mission_id,
            request=opts.request,
            success_definition=opts.success_definition,
            max_cost_usd=opts.max_cost_usd,
            terminal_reserve_cost_usd=getattr(opts, "terminal_reserve_cost_usd", 0.0),
            in_scope=in_scope,
            out_of_scope=out_scope,
            persistence_root=getattr(opts, "root", "missions"),
            verifier_kind=getattr(opts, "verifier_kind", "unspecified"),
        )
        pack = mr.init_mission_pack(opts.root, spec)
        _emit(mr.pack_summary(pack), opts.out)
        return
    if cmd == "run":
        # HUL-D until-limits driver. CLI scope seat unkeyed by default.
        pack = mr.load_mission_pack(opts.root, opts.mission_id)
        run_settings = load_settings()
        run_settings.jev_api_key = None
        try:
            scope_policy = policy_for(run_settings)
        except HarnessError:
            scope_policy = None
        stall_limit = getattr(opts, "stall_limit", None)
        result = _mission_run(
            pack,
            attempt_fn=_mission_pack_probe,
            scope_policy=scope_policy,
            stall_limit=int(stall_limit) if stall_limit else 5,
            max_attempts=getattr(opts, "max_attempts", None),
            max_tokens=getattr(opts, "max_tokens", None),
            max_errors=getattr(opts, "max_errors", None),
        )
        _emit(result, opts.out)
        return
    pack = mr.load_mission_pack(opts.root, opts.mission_id)
    if cmd == "status":
        mr.write_status(pack)
        mr.write_index(pack)
        _emit(mr.pack_summary(pack), opts.out)
        return
    if cmd == "resume":
        state = mr.load_resume(pack)
        validated = mr.validate_resume(state, expected_id=pack.id)
        mr.write_resume(pack, validated)
        mr.write_status(pack)
        out = mr.pack_summary(pack)
        out["resume"] = validated
        out["resumable"] = validated["status"] not in ("terminal", "complete", "failed")
        _emit(out, opts.out)
        return
    if cmd == "findings":
        if not pack.findings_md.is_file():
            mr.ensure_findings_placeholder(pack)
        body = pack.findings_md.read_text(encoding="utf-8")
        out = mr.pack_summary(pack)
        out["findings_md"] = body
        out["terminal"] = mr.is_terminal(pack)
        _emit(out, opts.out)
        return
    raise HarnessError(f"unknown mission command: {cmd!r}")


def _cmd_trust(opts, settings):
    """Read-only trust snapshot: no key, no network, no ledger writes."""
    from . import trust as trust_policy
    ledger = _ledger(settings)
    _emit(trust_policy.trust_status(ledger.participation_report(),
                                    model=opts.model,
                                    caller=opts.caller), opts.out)


def _cmd_rankings(opts, settings):
    """Rankings-driven candidate report (advisory; no config mutation).

    One GET for the daily rankings plus the cached live catalog; with
    --probe, each proposed candidate additionally pays one governed vote.
    """
    api_key, gov = _governor(settings, opts.max_cost)
    report = _rankings_report(
        HttpTransport(), api_key, gov,
        top_n=opts.top, probe_candidates=bool(opts.probe))
    report["cost_by_model"] = gov.cost_by_model()
    report["meta"] = _run_meta(settings, gov)
    _emit(report, opts.out)


def _cmd_capabilities(opts, settings):
    if getattr(opts, "check_shipped", False):
        # Freshness validation of the SHIPPED default lanes: pure /models read
        # ($0.00, no chat call), exit 2 when any default pool id has left the
        # live catalog -- the twice-recurred stale-id defect class, now
        # machine-checked instead of audit-cadence-checked.
        api_key, gov = _governor(settings)
        catalog = {m["id"] for m in gov.fetch_models(refresh=True)}
        stale = sorted(shipped_model_ids() - catalog)
        report = {"ok": not stale, "stale": stale,
                  "checked": len(shipped_model_ids()),
                  "catalog_size": len(catalog)}
        _emit(report, opts.out)
        if stale:
            eprint("[check-shipped] stale default model ids (run will hard-fatal "
                   "at fetch_pricing): " + ", ".join(stale))
            sys.exit(2)
        return
    api_key, gov = _governor(settings, opts.max_cost)
    out = _capabilities_payload(settings, gov, api_key=api_key,
                                refresh=opts.refresh, bench=opts.bench,
                                all_models=opts.all)
    if not opts.json:
        _print_capabilities_table(out)
    _emit(out, opts.out)


def _capabilities_payload(settings, gov, api_key=None, refresh=False,
                          bench=False, all_models=False):
    """CLI seam for capability.capabilities_payload (the ONE owner); the
    session objects come from the CLI's own composition."""
    return _capability_payload_owner(
        gov, _ledger(settings), panel_pool=settings.panel_pool,
        apply_pool=settings.apply_pool, judge=settings.judge,
        api_key=api_key, transport=HttpTransport(), refresh=refresh,
        bench=bench, all_models=all_models)


def _plan_compose(settings, opts, gov, transport, api_key, *,
                  candidate_files, frontier_model, execute, confirm=None,
                  decompose_llm=None, plan_consensus=None, hourglass=None):
    """Plan-lane flow via the ONE owner (harness/waist.py): heuristic or
    cheap-LLM decomposition, then (hourglass default: on) waist
    confirmation."""
    if hourglass is None:
        hourglass = _resolve_hourglass(opts, settings)
    if confirm is None:
        confirm = hourglass["confirm"]
    if decompose_llm is None:
        decompose_llm = hourglass.get("decompose", False)
    if plan_consensus is None:
        plan_consensus = bool(getattr(opts, "plan_consensus", None))
    plan_ledger = (_ledger(settings) if hasattr(settings, "ledger_path") else None)
    jev_policy = (policy_for(settings, transport=transport,
                             governor=gov, ledger=plan_ledger)
                  if hasattr(settings, "jev_api_key") else None)
    return _compose_plan(
        transport=transport, api_key=api_key, governor=gov,
        ledger=plan_ledger if (confirm or plan_consensus) else None,
        opts_goal=opts.goal,
        candidate_files=candidate_files, frontier_model=frontier_model,
        use_free=settings.use_free,
        decompose_llm=decompose_llm,
        confirm=confirm,
        plan_consensus=plan_consensus,
        execute=execute,
        allow_escalation=bool(getattr(opts, "allow_escalation", getattr(settings, "allow_escalation", False))),
        jev_policy=jev_policy,
        # The same pinned output budget the nodes will run with, so the
        # chunk policy measures each pass against the real one.
        max_tokens=getattr(opts, "max_tokens", None))


def _resolve_hourglass(opts, settings):
    """Tri-state plan flags -> effective values (ONE mapping, owned by
    config.resolve_hourglass): an explicit flag wins, otherwise the
    settings-file default (auto-scaling hourglass: all on)."""
    return resolve_hourglass(settings, opts)


def _cmd_plan(opts, settings):
    # Decompose a high-level goal into an executable TaskDAG and optionally execute
    candidate_files = getattr(opts, "file", None)
    frontier_model = getattr(opts, "frontier_model", None) or getattr(settings, "frontier_model", None)
    execute = getattr(opts, "execute", False)
    hourglass = _resolve_hourglass(opts, settings)
    confirm = hourglass["confirm"]
    decompose_llm = hourglass["decompose"]
    plan_consensus = bool(getattr(opts, "plan_consensus", None))
    resume_path = getattr(opts, "resume", None)

    # ONE governor for the whole run when it spends: decomposition,
    # confirmation, and node execution share a single ceiling (the engine's
    # when executing; a verified standalone governor for a plan-only LLM run).
    engine = None
    if execute:
        engine = _session(settings, max_cost=getattr(opts, "max_cost", None))
        gov, transport, api_key = engine.governor, engine.transport, engine.api_key
    elif decompose_llm or confirm or plan_consensus:
        api_key, gov = _governor(settings, getattr(opts, "max_cost", None))
        transport = HttpTransport()
    else:
        gov, transport, api_key = None, None, None

    # HG-pyramid-resume: a resume run re-plans only if no state was supplied;
    # with state, the stored DAG's pending nodes are the work.
    pending_state = None
    if resume_path:
        pending_state = load_state(resume_path)

    plan_result = _plan_compose(
        settings, opts, gov, transport, api_key,
        candidate_files=candidate_files, frontier_model=frontier_model,
        execute=execute, confirm=confirm, decompose_llm=decompose_llm,
        plan_consensus=plan_consensus, hourglass=hourglass)
    if plan_result.get("status") == "refused":
        # The waist refused (or the composed ceiling / unreachable waist
        # fail-closed fired); execution must not start (exit code 2).
        _emit_by_status(plan_result, opts.out)
        return
    if not execute:
        if isinstance(plan_result.get("structural"), dict):
            plan_result["structural"] = dict(plan_result["structural"])
            plan_result["structural"]["site"] = "cli"
        _emit(plan_result, opts.out)
        return

    run_gate = None
    if isinstance(plan_result.get("dag"), dict):
        # Carry the plan-level discovered gate into the executor for the
        # default final gate.
        for n in plan_result.get("nodes") or ():
            if isinstance(n, dict) and n.get("local_gate"):
                run_gate = n["local_gate"]
                break
    stage_gate = getattr(opts, "stage_gate", None)

    if pending_state is not None:
        dag = dag_for_pending(pending_state)
        node_routes = node_routes_for_pending(pending_state, plan_result.get("nodes"))
        if not dag.nodes:
            output = {
                "status": "ok",
                "goal": pending_state.get("goal") or opts.goal,
                "total_nodes": 0,
                "completed_nodes": 0,
                "results": list((pending_state.get("node_results") or {}).values()),
                "cost": float(pending_state.get("spent") or 0.0),
                "resumed": True,
                "composed_worst_case": plan_result.get("composed_worst_case"),
            }
            _emit_by_status(output, opts.out)
            return
    else:
        dag = TaskDAG.from_dict(plan_result["dag"])
        node_routes = {n.get("node_id"): n for n in plan_result["nodes"]}

    def on_stage_done(executable_nodes, _results):
        # Optional full-suite stage gate (MR-5): the composed tree must be
        # green before dependent stages start; failure aborts the run.
        if not stage_gate:
            return
        proc = subprocess.run(stage_gate, shell=True, capture_output=True,
                              text=True, timeout=VERIFY_TIMEOUT * 6)
        if proc.returncode != 0:
            raise HarnessError(
                f"stage gate failed after a parallel stage completed; "
                f"stopping before dependent stages (gate: {stage_gate})")

    # ONE execution assembly for every lane (executor.PlanExecutor): worker
    # count, reservations, worktree isolation, final gate, and per-node
    # routing. The agent's edit lane builds the same object.
    plan_exec = PlanExecutor(
        engine, node_routes,
        parallel=hourglass["parallel"], isolate=hourglass["isolate"],
        max_workers=getattr(opts, "max_workers", DEFAULT_PLAN_WORKERS),
        keep_going=getattr(opts, "keep_going", False),
        require_diff_authorization=hourglass["require_diff_authorization"],
        route_kwargs_fn=functools.partial(
            node_apply_kwargs,
            explicit_model=getattr(opts, "model", None),
            explicit_task_max_cost=getattr(opts, "task_max_cost", None)),
        base_apply_kwargs={
            "allow_verify": True,
            "require_consent": False,
            "model": getattr(opts, "model", None),
            "max_tokens": getattr(opts, "max_tokens", None),
            "task_max_cost": getattr(opts, "task_max_cost", None),
            "allow_escalation": getattr(opts, "allow_escalation", False),
            "reasoning_effort": getattr(opts, "reasoning_effort", None),
            "renew_consent": False,
            "max_rotations": getattr(opts, "max_rotations", 3),
        },
        task_max_cost=getattr(opts, "task_max_cost", None),
        # The budget this lane is really running under (the session ceiling
        # this command resolved), so a node reservation is bounded by it
        # instead of by an unrelated nominal default.
        run_ceiling=getattr(gov, "max_cost", None),
        on_stage_done=on_stage_done,
        final_gate=getattr(opts, "final_gate", None),
        run_gate=run_gate)
    all_results = plan_exec.execute(dag)
    summary = PlanExecutor.summarize(all_results)

    # HG-pyramid-resume: persist after every execute so a later --resume can
    # skip completed ok nodes.
    if resume_path or getattr(opts, "persist_state", None):
        state_path = resume_path or getattr(opts, "persist_state", None)
        merged = dict((pending_state or {}).get("node_results") or {})
        for key, res in all_results.items():
            if key == "final_gate":
                continue
            merged[key] = res
        persist_state(
            state_path,
            goal=plan_result.get("goal") or opts.goal,
            dag=(pending_state or {}).get("dag") or plan_result.get("dag"),
            node_results=merged,
            spent=float(summary.get("total_cost") or 0.0)
            + float((pending_state or {}).get("spent") or 0.0),
            plan_nodes=plan_result.get("nodes"))

    output = {
        "status": "ok" if summary["all_ok"] else "failed",
        "goal": opts.goal,
        "total_nodes": len(dag.nodes),
        "completed_nodes": summary["completed"],
        "results": [r for key, r in all_results.items() if key != "final_gate"],
        "cost": summary["total_cost"],
        "composed_worst_case": plan_result.get("composed_worst_case"),
        "final_gate": summary.get("final_gate"),
    }
    if pending_state is not None:
        output["resumed"] = True
        output["skipped_completed"] = sorted(
            nid for nid, res in ((pending_state.get("node_results") or {}).items())
            if isinstance(res, dict) and res.get("status") in ("ok", "success"))
    structural = aggregate_structural(list(all_results.values()), site="cli")
    if structural is None and isinstance(plan_result.get("structural"), dict):
        structural = dict(plan_result["structural"])
        structural["site"] = "cli"
    if structural is not None:
        output["structural"] = structural
    _emit_by_status(output, opts.out)


def _write_jev_phase_out(opts, payload):
    if getattr(opts, "out", None):
        out_dir = os.path.dirname(opts.out)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(opts.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)


def _print_jev_phase_result(result):
    print(f"phase={result['phase']} score={result['score']}/{result['min_score']} "
          f"can_mark_complete={result['can_mark_complete']}")
    print("hard_gates:", json.dumps(result["hard_gates"]))
    if result.get("blockers"):
        print("blockers:")
        for b in result["blockers"]:
            print(f"  - {b}")
    sem = result.get("semantic") or {}
    print(f"semantic: score={sem.get('score')} fallback={sem.get('is_fallback')} "
          f"model={sem.get('model')} note={sem.get('note')}")
    sentiment = result.get("sentiment") or {}
    axes = sentiment.get("axes") or {}
    if axes:
        print("sentiment:")
        for axis in sorted(axes):
            a = axes[axis]
            print(f"  {axis}: {a.get('level')} (source={a.get('source')})")
        overall = sentiment.get("overall") or {}
        print(f"  overall: {overall.get('level')}")
    improvements = result.get("improvements") or []
    if improvements:
        print("improvements:")
        # Primary gap first for the human reading top-to-bottom; the stored
        # order (ordinal asc, then axis id) is unchanged in the JSON.
        for imp in sorted(improvements, key=lambda i: 0 if i.get("primary") else 1):
            marker = "*" if imp.get("primary") else "-"
            print(f"  {marker} [{imp.get('bucket')}] {imp.get('suggested_next_action')} "
                  f"(axis={imp.get('axis')}, level={imp.get('level')}, "
                  f"source={imp.get('source')})")


def _cmd_jev_phase(opts, settings):
    """Dogfood the JEV bar: hard gates + full sentiment buckets drive whether
    a mission phase may be marked complete (JEV-BAR)."""
    use_live = not getattr(opts, "local_only", False)
    all_phases = bool(getattr(opts, "all", False))
    phase = getattr(opts, "phase", None)
    if not all_phases and not phase:
        raise HarnessError("jev-phase requires --phase or --all")
    pack_path = getattr(opts, "pack", None)
    min_score = float(getattr(opts, "min_score", 85.0))

    if all_phases:
        jev_policy = None
        if use_live and settings is not None:
            jev_policy = policy_for(settings)
        board = score_all_phases(
            opts.repo_root, jev_policy=jev_policy, min_score=min_score,
            pack=pack_path)
        if getattr(opts, "json", False):
            print(json.dumps(board, indent=2, default=str))
        else:
            for phase_id in sorted(board["phases"]):
                result = board["phases"][phase_id]
                top = (result.get("improvements") or [None])[0]
                top_desc = (f"{top['bucket']}: {top['suggested_next_action']}"
                           if top else "-")
                print(f"{phase_id} score={result['score']} "
                      f"bar_pass={result['bar']['pass']} top_improvement={top_desc}")
            if board.get("false_complete"):
                print("false_complete:", ", ".join(board["false_complete"]))
        _write_jev_phase_out(opts, board)
        if board.get("false_complete"):
            raise HarnessError(
                "phases claim complete but fail the JEV bar: "
                + ", ".join(board["false_complete"]))
        return

    result = dogfood_phase(
        opts.repo_root,
        phase,
        evidence_path=getattr(opts, "evidence", None),
        settings=settings if use_live else None,
        use_live_jev=use_live,
        min_score=min_score,
        pack_path=pack_path,
    )
    if getattr(opts, "json", False):
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_jev_phase_result(result)
    _write_jev_phase_out(opts, result)
    if not result.get("can_mark_complete"):
        raise HarnessError(
            f"phase {result['phase']} completion score {result['score']} "
            f"< {result['min_score']} or hard gates failed — do not mark complete")


# Command -> handler. `required=True` subparsers make an unknown command
# unreachable here, so the table has no default arm; every handler takes
# (opts, settings), so a signature drift fails loudly at dispatch instead of
# silently mis-binding arguments.
_DISPATCH = {
    "verify": _cmd_verify,
    "lint-claims": _cmd_lint_claims,
    "brief": _cmd_brief,
    "apply": _cmd_apply,
    "plan": _cmd_plan,
    "dogfood": _cmd_dogfood,
    "continue": _cmd_continue,
    "offer": _cmd_offer,
    "defer": _cmd_defer,
    "issue-sort": _cmd_issue_sort,
    "log-judgment": _cmd_log_judgment,
    "repo-summary": _cmd_repo_summary,
    "route": _cmd_route,
    "site-export": _cmd_site_export,
    "ledger": _cmd_ledger,
    "models": _cmd_models,
    "bench": _cmd_bench,
    "capabilities": _cmd_capabilities,
    "spend": _cmd_spend,
    "cost": _cmd_cost,
    "trust": _cmd_trust,
    "rankings": _cmd_rankings,
    "jev-phase": _cmd_jev_phase,
    "mission": _cmd_mission,
}


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    # The UI faces exit before settings/ledger setup: they run their own
    # servers and manage their own state (serve needs no key until a
    # dispatch happens; each runner loads settings itself).
    if args[:1] == ["serve"]:
        from .server import main as _serve
        return _serve(args[1:])
    if args[:1] == ["desktop"]:
        from .ui import main as _desktop
        return _desktop(args[1:])
    ap = build_parser()
    opts = ap.parse_args(args)
    import harness.output as _output
    _output.QUIET = bool(getattr(opts, "quiet", False))
    # Phase-1 UI groundwork: optional typed event stream + color policy.
    # The sink is module-global state registered once per CLI invocation;
    # with no --events flag nothing is registered and emit() is a no-op.
    from . import events as _events
    from . import render as _render
    if getattr(opts, "events", None):
        _events.add_jsonl_sink(opts.events)
    _render.set_color_enabled(
        not getattr(opts, "no_color", False)
        and not os.environ.get("NO_COLOR"))
    try:
        settings = load_settings()
        _DISPATCH[opts.command](opts, settings)
    except HarnessError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("[interrupted] aborted by user (Ctrl-C); no further spend", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
