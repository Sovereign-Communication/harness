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
  harness bench    run a manifest of known-answer tasks through the free tier

Free tier is the default: `--free`/HARNESS_USE_FREE routes everything through
the best current free models, rotating and deferring instead of failing.

Logs go to stderr; the JSON result goes to stdout (or --out <file>).
Exit codes: 0 success, 1 fatal refusal/error, 2 verification failed,
3 deferred (capability/consent) -- safe to continue.
"""
import argparse
import json
import sys
import uuid

from ._http import HttpTransport
from .apply import ApplyEngine, validate_continuation
from .bench import load_manifest, run_bench
from .claims import (
    build_claims_prompt, lint_claims, load_claims_manifest,
    load_definitions_file, parse_claims,
)
from .config import load_settings, resolve_api_key


def _split_opt_list(value):
    """Parse a comma-separated CLI list into a clean list of model ids."""
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def _read_text(path, what):
    """Read a text file, turning a missing path into a presentable error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise HarnessError(f"{what} not readable: {path} ({e.strerror or e})")


def _read_json(path, what):
    """Read a JSON file, presenting missing files and parse errors cleanly."""
    text = _read_text(path, what)
    try:
        return json.loads(text)
    except ValueError as e:
        raise HarnessError(f"{what} is not valid JSON: {path} ({e})")
from .core import (
    HarnessError, SpendGovernor, eprint, panel_judge, discover_free_models,
)
from .consent import probe_consent
from .ledger import AutonomyLedger
from .router import Router


def _emit(result, out):
    text = json.dumps(result, indent=2)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        eprint(f"[OK] result written to {out}")
    else:
        print(text)


def _governor(settings, max_cost_override=None):
    api_key = resolve_api_key()
    if not api_key:
        raise HarnessError(
            "no OpenRouter API key found (OPENROUTER_API_KEY env, "
            "~/.config/scmorc/openrouter*.env, or ~/.config/harness/openrouter.env).")
    max_cost = settings.max_cost if max_cost_override is None else max_cost_override
    gov = SpendGovernor(HttpTransport(), api_key, settings.expect_key_label,
                        max_cost)
    gov.verify_key()
    return api_key, gov


def _router(settings):
    return Router(settings.panel, settings.judge, settings.apply_model,
                  settings.escalation_model, settings.allow_escalation,
                  panel_pool=settings.panel_pool, apply_pool=settings.apply_pool,
                  specialist_pool=settings.specialist_pool,
                  convergence_model=settings.convergence_model)


def _capability_context(settings, gov, ledger):
    """Return (profiles, report) for capability-aware routing, or (None, None)
    when capability data is unavailable (no network / models fetch failure).
    The call is free: /models is already fetched & cached by the governor."""
    try:
        from .capability import build_profiles_from_models
        models = gov.fetch_models()
        profiles = build_profiles_from_models(models)
        report = ledger.participation_report()
        return profiles, report
    except Exception as e:
        eprint(f"[capability] unavailable ({e}); routing on the given order.")
        return None, None


def _order_pool(pool, profiles, report, ledger, task, free_tier):
    """Order a pool by observed-corrected reliability. Empty-safe."""
    from .capability import order_pool as _op
    ordered = _op(pool, profiles, report, ledger=ledger, task=task, free_tier=free_tier)
    return ordered if ordered else pool


def _cmd_verify(opts, settings):
    # P0 structured-claims mode: lint + auto-expand BEFORE any network call, so
    # an ungrounded claim is rejected without spending a cent.
    claims = None
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
    api_key, gov = _governor(settings, opts.max_cost)
    ledger = AutonomyLedger(settings.ledger_path)
    profiles, report = _capability_context(settings, gov, ledger)
    panel = (opts.panel or ",".join(settings.panel_pool)).split(",")
    if profiles is not None:
        panel = _order_pool(panel, profiles, report, ledger,
                            "structured" if opts.converge else "default",
                            settings.use_free)
    router = _router(settings)
    result = panel_judge(
        transport=HttpTransport(), api_key=api_key, governor=gov, prompt=prompt,
        panel=panel,
        judge=opts.judge or settings.judge,
        max_tokens=opts.max_tokens,
        reasoning_effort=opts.reasoning_effort or settings.reasoning_effort,
        reasoning_token_budget=settings.reasoning_token_budget,
        task_id=opts.task_id or uuid.uuid4().hex[:8], ledger=ledger,
        max_panelists=settings.max_panelists,
        run_convergence=opts.converge,
        convergence_model=opts.convergence_model or settings.convergence_model,
        specialist_pool=(_split_opt_list(opts.specialist_pool) if opts.specialist_pool
                         else router.specialist_pool),
        claim_polarity={cid.strip(): "reassurance" for cid in
                        (opts.reassurance_claims or "").split(",") if cid.strip()},
        capability_profiles=profiles, report=report, free_tier=settings.use_free)
    if claims_lint is not None:
        result["claims_grounding"] = {
            "ok": claims_lint["ok"],
            "issues": claims_lint["issues"],
            "expansions": claims_lint["expansions"],
        }
    _emit(result, opts.out)


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

    api_key, gov = _governor(settings)
    ledger = AutonomyLedger(settings.ledger_path)
    profiles, report = _capability_context(settings, gov, ledger)
    router = _router(settings)
    backend = continuation.get("backend", opts.backend) if continuation else opts.backend
    if profiles is not None and backend == "harness":
        router.apply_pool = _order_pool(router.apply_pool, profiles, report, ledger,
                                        "code", settings.use_free)
        if not opts.model and router.apply_pool:
            router.apply_model = router.apply_pool[0]
    engine = ApplyEngine(
        HttpTransport(), api_key, gov, ledger, router,
        default_require_consent=settings.default_require_consent,
        default_renew_consent=settings.renew_consent,
        reasoning_effort=settings.reasoning_effort,
        reasoning_token_budget=settings.reasoning_token_budget,
        default_max_rotations=settings.max_rotations,
        default_task_max_cost=settings.task_max_cost)
    result = engine.apply_edit(
        task_id=opts.task_id, file_path=opts.file, instruction=opts.instruction,
        edit_snippet=opts.edit_snippet, verify_cmd=opts.verify,
        max_rounds=opts.max_rounds, require_consent=opts.require_consent,
        model=opts.model, max_tokens=opts.max_tokens,
        task_max_cost=opts.task_max_cost,
        allow_escalation=opts.allow_escalation,
        reasoning_effort=opts.reasoning_effort,
        renew_consent=opts.renew_consent,
        max_rotations=opts.max_rotations,
        continuation=continuation, backend=opts.backend,
        verify_only=opts.verify_only, max_lines=opts.max_lines)
    _emit(result, opts.out)
    if result["status"] == "verify_failed":
        sys.exit(2)
    if result["status"] == "deferred":
        eprint("[continue] task deferred; run with --continue-from to resume.")
        sys.exit(3)


def _cmd_continue(opts, settings):
    # As with --continue-from, reject missing verification authority before any
    # OpenRouter key/model lookup. `validate_continuation` also unwraps a full
    # persisted result object for CLI callers.
    continuation = validate_continuation(
        _read_json(opts.state, "--state continuation"))
    api_key, gov = _governor(settings)
    ledger = AutonomyLedger(settings.ledger_path)
    router = _router(settings)
    profiles, report = _capability_context(settings, gov, ledger)
    if profiles is not None and continuation.get("backend", opts.backend) == "harness":
        router.apply_pool = _order_pool(router.apply_pool, profiles, report, ledger,
                                        "code", settings.use_free)
        if not opts.model and router.apply_pool:
            router.apply_model = router.apply_pool[0]
    engine = ApplyEngine(
        HttpTransport(), api_key, gov, ledger, router,
        default_require_consent=settings.default_require_consent,
        default_renew_consent=settings.renew_consent,
        reasoning_effort=settings.reasoning_effort,
        reasoning_token_budget=settings.reasoning_token_budget,
        default_max_rotations=settings.max_rotations,
        default_task_max_cost=settings.task_max_cost)
    result = engine.apply_edit(
        task_id=opts.task_id, file_path=opts.file, instruction=opts.instruction,
        edit_snippet=opts.edit_snippet, verify_cmd=opts.verify,
        max_rounds=opts.max_rounds, require_consent=opts.require_consent,
        model=opts.model, max_tokens=opts.max_tokens,
        task_max_cost=opts.task_max_cost, allow_escalation=opts.allow_escalation,
        reasoning_effort=opts.reasoning_effort, renew_consent=opts.renew_consent,
        max_rotations=opts.max_rotations, continuation=continuation,
        backend=opts.backend, verify_only=opts.verify_only, max_lines=opts.max_lines)
    _emit(result, opts.out)
    if result["status"] == "verify_failed":
        sys.exit(2)
    if result["status"] == "deferred":
        eprint("[continue] still deferred; run --continue-from again to resume.")
        sys.exit(3)


def _cmd_offer(opts, settings):
    api_key, gov = _governor(settings)
    ledger = AutonomyLedger(settings.ledger_path)
    result = probe_consent(
        transport=HttpTransport(), api_key=api_key, governor=gov,
        task_id=opts.task_id or uuid.uuid4().hex[:8], task=opts.task,
        model=opts.model or settings.judge, context=opts.context,
        ledger=ledger, required=True, fallback_pool=settings.panel_pool)
    _emit(result, opts.out)


def _cmd_defer(opts, settings):
    ledger = AutonomyLedger(settings.ledger_path)
    entry = ledger.append("defer_midtask", task_id=opts.task_id, reason=opts.reason,
                          category=opts.category or None, model="(deferral)")
    _emit({"status": "deferred", "task_id": opts.task_id, "reason": opts.reason,
           "category": opts.category, "ledger_entry": entry}, None)


def _cmd_ledger(opts, settings):
    ledger = AutonomyLedger(settings.ledger_path)
    if opts.ledger_cmd == "tail":
        _emit({"entries": ledger.tail(20), "count": len(ledger.entries())}, None)
    elif opts.ledger_cmd == "verify":
        ok, bad = ledger.verify()
        _emit({"verified": ok, "first_bad_seq": bad}, None)
    elif opts.ledger_cmd == "report":
        _emit(ledger.participation_report(), None)


def _cmd_bench(opts, settings):
    api_key, gov = _governor(settings)
    ledger = AutonomyLedger(settings.ledger_path)
    engine = ApplyEngine(
        HttpTransport(), api_key, gov, ledger, _router(settings),
        default_require_consent=settings.default_require_consent,
        default_renew_consent=settings.renew_consent,
        reasoning_effort=settings.reasoning_effort,
        reasoning_token_budget=settings.reasoning_token_budget,
        default_max_rotations=settings.max_rotations,
        default_task_max_cost=settings.task_max_cost)
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
    api_key, gov = _governor(settings)
    transport = HttpTransport()
    ids = discover_free_models(transport, api_key, prefer=settings.panel_pool,
                               limit=opts.limit)
    if opts.all:
        gov2 = SpendGovernor(transport, api_key)
        ids = sorted(m["id"] for m in gov2.fetch_models())
    _emit({"free_only": not opts.all, "models": ids, "count": len(ids)}, None)


def _cmd_spend(settings):
    api_key, gov = _governor(settings)
    _emit(gov.key_status(), None)


def _cmd_capabilities(opts, settings):
    from .capability import (ensure_profiles, model_reliability, capability_fitness,
                             probe_json_reliability, capability_score)
    from .config import CAPABILITIES_PATH, CAPABILITIES_TTL
    api_key, gov = _governor(settings)
    ledger = AutonomyLedger(settings.ledger_path)
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
    rows = out["models"]
    if not rows:
        print("(no models in pools with capability profiles)")
        return
    hdr = f"{'model':<42} {'ctx':>9} {'rsn':>3} {'jd':>4} {'jr':>4} {'cap':>5} {'f-str':>5} {'rel':>5}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        probe = r.get("probe")
        probe_note = ""
        if probe is not None and probe.get("calls"):
            probe_note = (f"  probe: json={probe['json_ok_rate']} "
                          f"correct={probe['correct_rate']} err={probe['errors']}")
        jd = r["json_declared"]
        jr = r["json_reliable"]
        print(f"{r['model']:<42} {r['context']:>9,} {'Y' if r['reasoning'] else 'n':>3} "
              f"{jd:>4.2f} {jr:>4.2f} "
              f"{r['capability']:>5.2f} {r['fitness_structured']:>5.2f} "
              f"{r['reliability_structured']:>5.2f}{probe_note}")


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
                         "first (default: configured specialist_pool; free lane leads with GLM-5.2)")
    pv.add_argument("--reassurance-claims", default=None,
                    help="comma-separated claim ids phrased as reassurance ('X is correct'); "
                         "excluded from the defect convergence gate")
    pv.add_argument("--task-id", default=None)
    pv.add_argument("--out", default=None)

    pa = sub.add_parser("apply", help="Scoped code edit with a verification loop + consent continuation")
    pa.add_argument("--file", default=None)
    pa.add_argument("--instruction", default=None)
    pa.add_argument("--edit-snippet", default=None)
    pa.add_argument("--verify", default=None, help="verification gate command (e.g. 'cargo check')")
    pa.add_argument("--max-rounds", type=int, default=3)
    pa.add_argument("--require-consent", dest="require_consent", action="store_true", default=None)
    pa.add_argument("--no-consent", dest="require_consent", action="store_false")
    pa.add_argument("--renew-consent", dest="renew_consent", action="store_true", default=None)
    pa.add_argument("--no-renew-consent", dest="renew_consent", action="store_false")
    pa.add_argument("--task-id", default=None)
    pa.add_argument("--model", default=None)
    pa.add_argument("--max-tokens", type=int, default=4096)
    pa.add_argument("--task-max-cost", type=float, default=None)
    pa.add_argument("--allow-escalation", dest="allow_escalation", action="store_true", default=None)
    pa.add_argument("--reasoning-effort", default=None,
                    choices=["auto", "off", "none", "low", "medium", "high", "on"])
    pa.add_argument("--max-rotations", type=int, default=None)
    pa.add_argument("--backend", choices=["harness", "morph"], default="harness",
                    help="transformation backend; 'morph' uses Morph V3 Fast's structured edit prompt")
    pa.add_argument("--verify-only", action="store_true",
                    help="return the proposed content without writing or running the verification gate")
    pa.add_argument("--max-lines", type=int, default=500,
                    help="per-file line ceiling (1-500)")
    pa.add_argument("--continue-from", default=None, help="resume a deferred task from state.json")
    pa.add_argument("--out", default=None)

    pc = sub.add_parser("continue", help="Continue a deferred/incomplete apply task")
    pc.add_argument("--state", required=True, help="JSON state file from a deferred/verify_failed apply")
    pc.add_argument("--file", default=None)
    pc.add_argument("--instruction", default=None)
    pc.add_argument("--edit-snippet", default=None)
    pc.add_argument("--verify", default=None)
    pc.add_argument("--max-rounds", type=int, default=3)
    pc.add_argument("--require-consent", dest="require_consent", action="store_true", default=None)
    pc.add_argument("--no-consent", dest="require_consent", action="store_false")
    pc.add_argument("--renew-consent", dest="renew_consent", action="store_true", default=None)
    pc.add_argument("--no-renew-consent", dest="renew_consent", action="store_false")
    pc.add_argument("--task-id", default=None)
    pc.add_argument("--model", default=None)
    pc.add_argument("--max-tokens", type=int, default=4096)
    pc.add_argument("--task-max-cost", type=float, default=None)
    pc.add_argument("--allow-escalation", dest="allow_escalation", action="store_true", default=None)
    pc.add_argument("--reasoning-effort", default=None,
                    choices=["auto", "off", "none", "low", "medium", "high", "on"])
    pc.add_argument("--max-rotations", type=int, default=None)
    pc.add_argument("--backend", choices=["harness", "morph"], default="harness",
                    help="backend for a new continuation; saved continuation metadata takes precedence")
    pc.add_argument("--verify-only", action="store_true",
                    help="return the proposed content without writing or running the verification gate")
    pc.add_argument("--max-lines", type=int, default=500,
                    help="per-file line ceiling (1-500)")
    pc.add_argument("--out", default=None)

    po = sub.add_parser("offer", help="Ask a model for consent on a work item")
    po.add_argument("--task", required=True)
    po.add_argument("--task-id", default=None)
    po.add_argument("--model", default=None)
    po.add_argument("--context", default=None)
    po.add_argument("--out", default=None)

    pd = sub.add_parser("defer", help="Record a mid-task deferral / consent revocation")
    pd.add_argument("--task-id", required=True)
    pd.add_argument("--reason", default=None)
    pd.add_argument("--category", default=None)

    pl = sub.add_parser("ledger", help="Autonomy ledger")
    pls = pl.add_subparsers(dest="ledger_cmd", required=True)
    pls.add_parser("tail")
    pls.add_parser("verify")
    pls.add_parser("report")

    pm = sub.add_parser("models", help="List live free OpenRouter models (refreshed)")
    pm.add_argument("--limit", type=int, default=40)
    pm.add_argument("--all", action="store_true", help="list all live models, not just free")

    pb = sub.add_parser("bench", help="Run a manifest of known-answer tasks through the free tier")
    pb.add_argument("manifest", help="task manifest: a dir of task JSONs or a single JSON file")
    pb.add_argument("--with-consent", dest="require_consent", action="store_true",
                    help="ask consent before each task (default: off -- batch/CI mode)")
    pb.add_argument("--max-rounds", type=int, default=None)
    pb.add_argument("--out", default=None)

    plint = sub.add_parser("lint-claims",
                           help="Lint a claims manifest against its quoted source "
                                "(hermetic: no network)")
    plint.add_argument("--claims-file", required=True)
    plint.add_argument("--source-file", required=True)
    plint.add_argument("--definitions-file", default=None)
    plint.add_argument("--claim-context", default=None)
    plint.add_argument("--show-prompt", action="store_true")
    plint.add_argument("--out", default=None)

    pcap = sub.add_parser("capabilities", help="Model capability profiles + reliability "
                                                "(hypothesis from /models, corrected by observed evidence)")
    pcap.add_argument("--refresh", action="store_true",
                      help="force re-fetch of the live /models capability registry")
    pcap.add_argument("--all", action="store_true", help="list all live models, not just the pools")
    pcap.add_argument("--bench", action="store_true",
                      help="run the empirical JSON probe on the free pool models (live, needs key)")
    pcap.add_argument("--json", action="store_true", help="emit raw JSON only (no table)")
    pcap.add_argument("--out", default=None)

    sub.add_parser("spend", help="Key identity & spend status")

    opts = ap.parse_args(args)
    settings = load_settings()

    try:
        if opts.command == "verify":
            _cmd_verify(opts, settings)
        elif opts.command == "lint-claims":
            _cmd_lint_claims(opts, settings)
        elif opts.command == "apply":
            _cmd_apply(opts, settings)
        elif opts.command == "continue":
            _cmd_continue(opts, settings)
        elif opts.command == "offer":
            _cmd_offer(opts, settings)
        elif opts.command == "defer":
            _cmd_defer(opts, settings)
        elif opts.command == "ledger":
            _cmd_ledger(opts, settings)
        elif opts.command == "models":
            _cmd_models(opts, settings)
        elif opts.command == "bench":
            _cmd_bench(opts, settings)
        elif opts.command == "capabilities":
            _cmd_capabilities(opts, settings)
        elif opts.command == "spend":
            _cmd_spend(settings)
    except HarnessError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()