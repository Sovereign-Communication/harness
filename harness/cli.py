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
import argparse
import json
import os
import tempfile
from .consent import probe_consent
from .errors import HarnessError
from .spend import discover_free_models
from .capability import (ensure_profiles, model_reliability, capability_fitness,
                         probe_json_reliability, capability_score)
from .filesafety import validate_target_file, validate_verify_command
from .panel import panel_judge
from .output import eprint
from .session import (apply_session as _session, governor_for as _governor,
                      ledger_for as _ledger, router_for as _router)
from .results import terminal_exit_code
from .saturation import advise, pre_run_warning
import sys
import uuid

from ._http import HttpTransport
from .apply import validate_continuation
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
from .config import (CAPABILITIES_PATH, CAPABILITIES_TTL, load_settings,
                     shipped_model_ids)


def _split_opt_list(value):
    """Parse a comma-separated CLI list into a clean list of model ids."""
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def _read_text(path, what):
    """Read a text file, turning a missing path into a presentable error."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise HarnessError(f"{what} not readable: {path} ({e.strerror or e})") from e


def _read_json(path, what):
    """Read a JSON file, presenting missing files and parse errors cleanly."""
    text = _read_text(path, what)
    try:
        return json.loads(text)
    except ValueError as e:
        raise HarnessError(f"{what} is not valid JSON: {path} ({e})") from e


def _emit(result, out):
    text = json.dumps(result, indent=2)
    if out:
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            raise HarnessError(f"cannot write --out {out}: {e}") from e
        eprint(f"[OK] result written to {out}")
    else:
        print(text)


# Composition lives in harness/session.py (the ONE owner); the aliases below
# keep the historical cli seams for commands and tests that patch them.


def _emit_by_status(result, out, *, continued=False):
    """Apply results through the ONE exit-code policy (results.py); this
    adds the resume hint a deferred run needs and the saturation advise."""
    # Terminal honesty: a run that exhausted its rounds on 429s / reasoning-
    # only responses says so plainly, with the real options (one policy
    # owner, harness/saturation.py). The result's own rounds are the
    # evidence -- a failed run always carries its api_error rounds there.
    advise(engine_rounds=result.get("rounds"))
    _emit(result, out)
    code = terminal_exit_code(result["status"])
    if code == 3:
        eprint("[apply] task deferred; resume with: harness continue --state <out.json>"
               if not continued else
               "[apply] still deferred; resume again: harness continue --state <out.json>")
    if code:
        sys.exit(code)


def _run_claims_verify(settings, *, prompt, task_id=None, max_tokens=None,
                       reasoning_effort=None, converge=False, judge=None,
                       convergence_model=None, specialist_pool=None,
                       reassurance_claims="", panel=None, max_cost=None):
    """The ONE claims-verify execution path: governor setup, panel_judge run,
    cost attribution. `verify` and `dogfood` both call this; interfaces only
    prepare inputs and present the result. Panel ordering (catalog seed,
    capability sort, degrade-to-given-order) is the panel lane's own job."""
    api_key, gov = _governor(settings, max_cost)
    ledger = _ledger(settings)
    # Pre-spend look-ahead; advice only, never a gate.
    pre_run_warning(governor=gov, ledger=ledger, use_free=settings.use_free)
    panel = (panel or ",".join(settings.panel_pool)).split(",")
    result = panel_judge(
        transport=HttpTransport(), api_key=api_key, governor=gov, prompt=prompt,
        panel=panel,
        judge=judge or settings.judge,
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort or settings.reasoning_effort,
        reasoning_token_budget=settings.reasoning_token_budget,
        task_id=task_id or uuid.uuid4().hex[:8], ledger=ledger,
        max_panelists=settings.max_panelists,
        run_convergence=converge,
        convergence_model=convergence_model or settings.convergence_model,
        specialist_pool=(specialist_pool if specialist_pool
                         else _router(settings).specialist_pool),
        claim_polarity={cid.strip(): "reassurance" for cid in
                        (reassurance_claims or "").split(",") if cid.strip()},
        free_tier=settings.use_free)
    result["cost_by_model"] = gov.cost_by_model()
    return result


def _cmd_verify(opts, settings):
    # P0 structured-claims mode: lint + auto-expand BEFORE any network call, so
    # an ungrounded claim is rejected without spending a cent.
    claims_lint = None
    prompt = None
    if opts.claims_file:
        if not opts.source_file:
            raise HarnessError("verify --claims-file requires --source-file "
                               "(the verbatim code window the panel will review).")
        manifest_ctx, claims = load_claims_manifest(opts.claims_file)
        quoted = _read_text(opts.source_file, "--source-file")
        defs = load_definitions_file(opts.definitions_file) if opts.definitions_file else {}
        context = opts.claim_context if opts.claim_context is not None else manifest_ctx
        prompt, claims_lint = build_claims_prompt(claims, quoted, source_index=defs,
                                                  context=context)
        if not claims_lint["ok"]:
            for issue in claims_lint["issues"]:
                eprint(f"[claims-lint] {issue['severity'].upper()} "
                       f"{issue['code']}: {issue['message']}")
            _emit({"status": "rejected", "lint": claims_lint}, opts.out)
            sys.exit(2)
        # structured-claims mode always runs the convergence gate (deterministic
        # tally) and derives the polarity map from the manifest kinds.
        opts.converge = True
        opts.reassurance_claims = ",".join(c.claim_id for c in claims
                                           if c.kind == "reassurance")
    elif opts.prompt_file:
        prompt = _read_text(opts.prompt_file, "--prompt-file")
    elif opts.prompt:
        prompt = opts.prompt
    else:
        raise HarnessError("verify requires --prompt-file/--prompt or --claims-file.")
    if not prompt.strip():
        raise HarnessError("prompt is empty.")
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
        [opts.file], task_id=opts.task_id, instruction=opts.instruction,
        verify_cmd=opts.verify, max_rounds=opts.max_rounds,
        require_consent=opts.require_consent, model=opts.model,
        max_tokens=opts.max_tokens, task_max_cost=opts.task_max_cost,
        allow_escalation=opts.allow_escalation,
        reasoning_effort=opts.reasoning_effort,
        renew_consent=opts.renew_consent, max_rotations=opts.max_rotations,
        backend=opts.backend, max_lines=opts.max_lines)
    _phase("apply", {"status": result["status"], "cost": result.get("cost")})
    report["apply"] = result
    report["status"] = ("ok" if result["status"] == "ok"
                        else "incomplete")
    _emit(report, opts.out)
    # Same status-meaning policy as _emit_by_status: one def site (results.py).
    code = terminal_exit_code(result["status"])
    if code:
        sys.exit(code)


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
        files or [None], task_id=opts.task_id, instruction=opts.instruction,
        edit_snippet=opts.edit_snippet, verify_cmd=opts.verify,
        max_rounds=opts.max_rounds, require_consent=opts.require_consent,
        model=opts.model, max_tokens=opts.max_tokens,
        task_max_cost=opts.task_max_cost, allow_escalation=opts.allow_escalation,
        reasoning_effort=opts.reasoning_effort, renew_consent=opts.renew_consent,
        max_rotations=opts.max_rotations, backend=opts.backend,
        verify_only=opts.verify_only, max_lines=opts.max_lines,
        continuation=continuation)
    _emit_by_status(result, opts.out)


def _cmd_continue(opts, settings):
    # As with --continue-from, reject missing verification authority before any
    # OpenRouter key/model lookup. `validate_continuation` also unwraps a full
    # persisted result object for CLI callers.
    continuation = validate_continuation(
        _read_json(opts.state, "--state continuation"))
    engine = _session(settings)
    result = engine.apply_batch(
        [None], task_id=opts.task_id, instruction=opts.instruction,
        edit_snippet=opts.edit_snippet, verify_cmd=opts.verify,
        max_rounds=opts.max_rounds, require_consent=opts.require_consent,
        model=opts.model, max_tokens=opts.max_tokens,
        task_max_cost=opts.task_max_cost, allow_escalation=opts.allow_escalation,
        reasoning_effort=opts.reasoning_effort, renew_consent=opts.renew_consent,
        max_rotations=opts.max_rotations, backend=opts.backend,
        verify_only=opts.verify_only, max_lines=opts.max_lines,
        continuation=continuation)
    _emit_by_status(result, opts.out, continued=True)


def _cmd_offer(opts, settings):
    api_key, gov = _governor(settings)
    ledger = _ledger(settings)
    result = probe_consent(
        transport=HttpTransport(), api_key=api_key, governor=gov,
        task_id=opts.task_id or uuid.uuid4().hex[:8], task=opts.task,
        model=opts.model or settings.judge, context=opts.context,
        ledger=ledger, required=True, fallback_pool=settings.panel_pool)
    _emit(result, opts.out)


def _cmd_defer(opts, settings):
    ledger = _ledger(settings)
    entry = ledger.append("defer_midtask", task_id=opts.task_id, reason=opts.reason,
                          category=opts.category or None, model="(deferral)")
    _emit({"status": "deferred", "task_id": opts.task_id, "reason": opts.reason,
           "category": opts.category, "ledger_entry": entry}, None)


def _cmd_ledger(opts, settings):
    if opts.ledger_cmd == "tail" and opts.n < 1:
        # tail(0) is [-0:] == the whole ledger; negative n slices from the
        # front -- both silent nonsense (same class as models --limit).
        raise HarnessError("ledger tail count must be a positive integer")
    ledger = _ledger(settings)
    if opts.ledger_cmd == "tail":
        _emit({"entries": ledger.tail(opts.n), "count": len(ledger.entries())}, opts.out)
    elif opts.ledger_cmd == "verify":
        ok, bad = ledger.verify()
        _emit({"verified": ok, "first_bad_seq": bad,
               "chain": ledger.chain_status()}, opts.out)
    elif opts.ledger_cmd == "repair":
        kept, dropped = ledger.repair()
        _emit({"repaired": dropped > 0, "kept": kept, "dropped": dropped},
              opts.out)
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
        ids = sorted(m["id"] for m in gov.fetch_models(refresh=True))
    _emit({"free_only": not opts.all, "models": ids, "count": len(ids)}, opts.out)


def _cmd_spend(opts, settings):
    api_key, gov = _governor(settings)
    _emit(gov.key_status(), opts.out)


def _cmd_trust(opts, settings):
    """Read-only trust snapshot: no key, no network, no ledger writes."""
    from . import trust as trust_policy
    ledger = _ledger(settings)
    _emit(trust_policy.trust_status(ledger.participation_report(),
                                    model=opts.model,
                                    caller=opts.caller), opts.out)


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
    ledger = _ledger(settings)
    profiles, fetched_at, refreshed = ensure_profiles(
        CAPABILITIES_PATH, gov.fetch_models, ttl=CAPABILITIES_TTL, force=opts.refresh)

    if opts.all:
        ordered_ids = sorted(profiles.keys())
    else:
        ordered_ids = []
        for mid in settings.panel_pool + settings.apply_pool + [settings.judge]:
            if mid not in ordered_ids:
                ordered_ids.append(mid)

    report = ledger.participation_report()

    def row(mid):
        p = profiles.get(mid)
        if p is None:
            return None
        # Single owner: model_reliability computes everything from one place.
        info = model_reliability(mid, p, report, ledger=ledger, task="structured")
        return {
            "model": mid, "free": p.free,
            "context": p.context_length, "max_source_tokens": p.max_source_tokens,
            "reasoning": p.supports_reasoning,
            "json_declared": round(p.declared_json, 2),
            "json_reliable": round(info["json_reliable"] or 0.0, 2),
            "structured_json": p.supports_structured_json,
            "capability": round(capability_score(p), 3),
            "fitness_structured": round(info["capability"], 3),
            "fitness_code": round(capability_fitness(p, "code"), 3),
            "reliability_structured": round(info["reliability"], 3),
            "observed": {
                "confidence_precision": info["calibration"],
                "success_rate": info["success"],
                "samples": info["samples"],
            },
        }

    rows = []
    for mid in ordered_ids:
        r = row(mid)
        if r is not None:
            rows.append(r)

    if opts.bench:
        bench_models = [r["model"] for r in rows if r["free"]]
        # Persist probe results as model_result events so they feed observed
        # json reliability and routing (the evidence loop), and probe reasoning
        # models with reasoning on so the probe is fair to them.
        probe = probe_json_reliability(transport=HttpTransport(), api_key=api_key,
                                       governor=gov, models=bench_models,
                                       ledger=ledger, profiles=profiles)
        # Rebuild after persistence: the displayed reliability must include the
        # evidence just collected, not the pre-probe snapshot.
        report = ledger.participation_report()
        refreshed_rows = []
        for mid in ordered_ids:
            r = row(mid)
            if r is not None:
                r["probe"] = probe.get(r["model"])
                refreshed_rows.append(r)
        rows = refreshed_rows

    out = {
        "captured_at": fetched_at, "refreshed": refreshed,
        "models": rows, "count": len(rows),
    }
    if not opts.json:
        _print_capabilities_table(out)
    _emit(out, opts.out)


def _print_capabilities_table(out):
    """Human table on stderr: stdout stays pure JSON for piping, and --quiet
    suppresses the table while the JSON report still flows."""
    rows = out["models"]
    if not rows:
        eprint("(no models in pools with capability profiles)")
        return
    hdr = f"{'model':<42} {'ctx':>9} {'rsn':>3} {'jd':>4} {'jr':>4} {'cap':>5} {'f-str':>5} {'rel':>5}"
    eprint(hdr)
    eprint("-" * len(hdr))
    for r in rows:
        probe = r.get("probe")
        probe_note = ""
        if probe is not None and probe.get("calls"):
            probe_note = (f"  probe: json={probe['json_ok_rate']} "
                          f"correct={probe['correct_rate']} err={probe['errors']}")
        jd = r["json_declared"]
        jr = r["json_reliable"]
        eprint(f"{r['model']:<42} {r['context']:>9,} {'Y' if r['reasoning'] else 'n':>3} "
               f"{jd:>4.2f} {jr:>4.2f} "
               f"{r['capability']:>5.2f} {r['fitness_structured']:>5.2f} "
               f"{r['reliability_structured']:>5.2f}{probe_note}")


def _add_engine_flags(p, *, max_tokens_default, verify_required=False):
    """Flags shared by apply, continue, and dogfood -- the dispatches into the
    apply engine. One definition keeps the surfaces in lockstep (the historical
    bug class: a flag or default fixed on one but not the others)."""
    p.add_argument("--task-id", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--verify", required=verify_required, default=None,
                   help="verification gate command (e.g. 'cargo check')")
    p.add_argument("--edit-snippet", default=None)
    p.add_argument("--max-rounds", type=int, default=3)
    p.add_argument("--require-consent", dest="require_consent", action="store_true", default=None)
    p.add_argument("--no-consent", dest="require_consent", action="store_false")
    p.add_argument("--renew-consent", dest="renew_consent", action="store_true", default=None)
    p.add_argument("--no-renew-consent", dest="renew_consent", action="store_false")
    p.add_argument("--max-tokens", type=int, default=max_tokens_default)
    p.add_argument("--task-max-cost", type=float, default=None)
    p.add_argument("--allow-escalation", dest="allow_escalation", action="store_true", default=None)
    p.add_argument("--reasoning-effort", default=None,
                   choices=["auto", "off", "none", "low", "medium", "high", "on"])
    p.add_argument("--max-rotations", type=int, default=None)
    p.add_argument("--backend", choices=["harness", "morph", "diff"], default="harness",
                   help="transformation backend; on continue, saved continuation "
                        "metadata takes precedence")
    p.add_argument("--verify-only", action="store_true",
                   help="return the proposed content without writing or running the verification gate")
    p.add_argument("--max-lines", type=int, default=500,
                   help="per-file line ceiling (1-500)")


def _add_output_flags(p):
    """--out (JSON report destination) + --quiet (stderr progress off). Every
    subcommand that produces a report or progress output takes both."""
    p.add_argument("--out", default=None)
    p.add_argument("--quiet", action="store_true",
                   help="suppress stderr progress notes; report only")


# Command -> handler. `required=True` subparsers make an unknown command
# unreachable here, so the table has no default arm; every handler takes
# (opts, settings), so a signature drift fails loudly at dispatch instead of
# silently mis-binding arguments.
_DISPATCH = {
    "verify": _cmd_verify,
    "lint-claims": _cmd_lint_claims,
    "apply": _cmd_apply,
    "dogfood": _cmd_dogfood,
    "continue": _cmd_continue,
    "offer": _cmd_offer,
    "defer": _cmd_defer,
    "ledger": _cmd_ledger,
    "models": _cmd_models,
    "bench": _cmd_bench,
    "capabilities": _cmd_capabilities,
    "spend": _cmd_spend,
    "trust": _cmd_trust,
}


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    ap = argparse.ArgumentParser(
        prog="harness",
        description="Cost-bounded multi-model verification & coding harness with AI sovereignty.")
    sub = ap.add_subparsers(dest="command", required=True)

    pv = sub.add_parser("verify", help="Panel + judge verification (back-compat with fusion_lite.py)")
    pv.add_argument("--prompt-file")
    pv.add_argument("--prompt")
    pv.add_argument("--claims-file", default=None,
                    help="JSON claims manifest (P0 self-grounding); pairs with --source-file")
    pv.add_argument("--source-file", default=None,
                    help="verbatim code window the panel reviews (line numbers = source_refs)")
    pv.add_argument("--definitions-file", default=None,
                    help="JSON map identifier -> verbatim definition for auto-expansion")
    pv.add_argument("--claim-context", default=None,
                    help="context prose naming identifiers; overrides the manifest 'context' key")
    pv.add_argument("--panel")
    pv.add_argument("--judge")
    pv.add_argument("--max-tokens", type=int, default=None)
    pv.add_argument("--max-cost", type=float, default=None)
    pv.add_argument("--reasoning-effort", default=None,
                    choices=["auto", "off", "none", "low", "medium", "high", "on"])
    pv.add_argument("--converge", action="store_true",
                    help="run the convergence-specialist step on the panel's per-claim verdicts")
    pv.add_argument("--convergence-model", default=None,
                    help="primary model for the convergence specialist (default: same as --judge)")
    pv.add_argument("--specialist-pool", default=None,
                    help="ordered fallback models for the convergence specialist, strongest "
                         "first (default: configured specialist_pool; live-validated free lane)")
    pv.add_argument("--reassurance-claims", default=None,
                    help="comma-separated claim ids phrased as reassurance ('X is correct'); "
                         "excluded from the defect convergence gate")
    pv.add_argument("--task-id", default=None)
    _add_output_flags(pv)

    pa = sub.add_parser("apply", help="Scoped code edit with a verification loop + consent continuation")
    pa.add_argument("--file", action="append", default=None,
                    help="target file; repeat the flag for a multi-file batch (one session, shared gate)")
    pa.add_argument("--instruction", default=None)
    _add_engine_flags(pa, max_tokens_default=4096)
    pa.add_argument("--continue-from", default=None, help="resume a deferred task from state.json")
    _add_output_flags(pa)

    pc = sub.add_parser("continue", help="Continue a deferred/incomplete apply task")
    pc.add_argument("--state", required=True, help="JSON state file from a deferred/verify_failed apply")
    pc.add_argument("--file", default=None)
    pc.add_argument("--instruction", default=None)
    _add_engine_flags(pc, max_tokens_default=4096)
    _add_output_flags(pc)

    po = sub.add_parser("offer", help="Ask a model for consent on a work item")
    po.add_argument("--task", required=True)
    po.add_argument("--task-id", default=None)
    po.add_argument("--model", default=None)
    po.add_argument("--context", default=None)
    _add_output_flags(po)

    pd = sub.add_parser("defer", help="Record a mid-task deferral / consent revocation")
    pd.add_argument("--task-id", required=True)
    pd.add_argument("--reason", default=None)
    pd.add_argument("--category", default=None)

    pl = sub.add_parser("ledger", help="Autonomy ledger")
    pls = pl.add_subparsers(dest="ledger_cmd", required=True)
    for _lc in ("verify", "report"):
        _p = pls.add_parser(_lc)
        _add_output_flags(_p)
    _p = pls.add_parser("tail", help="show the last N ledger entries (default 20)")
    _p.add_argument("n", nargs="?", type=int, default=20)
    _add_output_flags(_p)
    _p = pls.add_parser("repair", help="truncate the ledger to its longest valid "
                                       "hash-chain prefix (drops forked/duplicate tail)")
    _add_output_flags(_p)

    pm = sub.add_parser("models", help="List live free OpenRouter models (refreshed)")
    pm.add_argument("--limit", type=int, default=40)
    pm.add_argument("--all", action="store_true", help="list all live models, not just free")
    _add_output_flags(pm)

    pb = sub.add_parser("bench", help="Run a manifest of known-answer tasks through the free tier")
    pb.add_argument("manifest", help="task manifest: a dir of task JSONs or a single JSON file")
    pb.add_argument("--with-consent", dest="require_consent", action="store_true",
                    help="ask consent before each task (default: off -- batch/CI mode)")
    pb.add_argument("--max-rounds", type=int, default=None)
    pb.add_argument("--max-cost", type=float, default=None,
                    help="session cost ceiling in dollars (default: configured max_cost)")
    _add_output_flags(pb)

    plint = sub.add_parser("lint-claims",
                           help="Lint a claims manifest against its quoted source "
                                "(hermetic: no network)")
    plint.add_argument("--claims-file", required=True)
    plint.add_argument("--source-file", required=True)
    plint.add_argument("--definitions-file", default=None)
    plint.add_argument("--claim-context", default=None)
    plint.add_argument("--show-prompt", action="store_true")
    _add_output_flags(plint)

    pcap = sub.add_parser("capabilities", help="Model capability profiles + reliability "
                                                "(hypothesis from /models, corrected by observed evidence)")
    pcap.add_argument("--refresh", action="store_true",
                      help="force re-fetch of the live /models capability registry")
    pcap.add_argument("--check-shipped", action="store_true",
                      help="validate every shipped default pool/model id against the "
                           "live catalog ($0.00); exit 2 if any has gone stale")
    pcap.add_argument("--all", action="store_true", help="list all live models, not just the pools")
    pcap.add_argument("--bench", action="store_true",
                      help="run the empirical JSON probe on the free pool models (live, needs key)")
    pcap.add_argument("--json", action="store_true", help="emit raw JSON only (no table)")
    pcap.add_argument("--max-cost", type=float, default=None,
                      help="session cost ceiling in dollars (default: configured max_cost)")
    _add_output_flags(pcap)

    sub.add_parser("spend", help="Key identity & spend status")
    _add_output_flags(sub.choices["spend"])

    ptrust = sub.add_parser("trust", help="Trust & correctness standing from ledger history "
                                          "(read-only: no key, no network)")
    ptrust.add_argument("--model", default=None,
                        help="model id to score (default: host standing only)")
    ptrust.add_argument("--caller", default=None,
                        help="caller id to score (default: global session standing)")
    _add_output_flags(ptrust)

    pdog = sub.add_parser(
        "dogfood", help="Self-hosting loop: ground -> live panel verify -> "
                        "gated self-apply (exit 0 only if every phase proved "
                        "its claim)")
    pdog.add_argument("--claims-file", default=None,
                      help="claims manifest naming the defect (self-audit fixture)")
    pdog.add_argument("--from-ledger", action="store_true",
                      help="curate the claims manifest from the ledger's own "
                           "run evidence instead of --claims-file")
    pdog.add_argument("--claims-out", default=None,
                      help="with --from-ledger: path to persist the curated "
                           "manifest (the next run's --claims-file)")
    pdog.add_argument("--evidence-window", type=int, default=500,
                      help="how many recent ledger entries curation scans")
    pdog.add_argument("--max-claims", type=int, default=3,
                      help="maximum claims to curate")
    pdog.add_argument("--source-file", default=None,
                      help="verbatim source window the panel reviews (not "
                           "needed with --from-ledger)")
    pdog.add_argument("--definitions-file", default=None)
    pdog.add_argument("--claim-context", default=None)
    pdog.add_argument("--file", required=True,
                      help="target file for the gated self-apply phase")
    pdog.add_argument("--instruction", required=True,
                      help="edit instruction for the apply phase")
    _add_engine_flags(pdog, max_tokens_default=None, verify_required=True)
    pdog.add_argument("--max-cost", type=float, default=None,
                      help="session cost ceiling for the live verify + apply phases")
    _add_output_flags(pdog)

    opts = ap.parse_args(args)
    import harness.output as _output
    _output.QUIET = bool(getattr(opts, "quiet", False))
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
