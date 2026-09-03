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
from .apply import ApplyEngine
from .bench import load_manifest, run_bench
from .claims import (
    build_claims_prompt, lint_claims, load_claims_manifest,
    load_definitions_file, parse_claims,
)
from .config import load_settings, resolve_api_key
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


def _governor(settings):
    api_key = resolve_api_key()
    if not api_key:
        raise HarnessError(
            "no OpenRouter API key found (OPENROUTER_API_KEY env, "
            "~/.config/scmorc/openrouter*.env, or ~/.config/harness/openrouter.env).")
    gov = SpendGovernor(HttpTransport(), api_key, settings.expect_key_label,
                        settings.max_cost)
    gov.verify_key()
    return api_key, gov


def _router(settings):
    return Router(settings.panel, settings.judge, settings.apply_model,
                  settings.escalation_model, settings.allow_escalation,
                  panel_pool=settings.panel_pool, apply_pool=settings.apply_pool)


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
        with open(opts.source_file, "r", encoding="utf-8") as f:
            quoted = f.read()
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
        with open(opts.prompt_file, "r", encoding="utf-8") as f:
            prompt = f.read()
    elif opts.prompt:
        prompt = opts.prompt
    else:
        raise HarnessError("verify requires --prompt-file/--prompt or --claims-file.")
    if not prompt.strip():
        raise HarnessError("prompt is empty.")
    api_key, gov = _governor(settings)
    ledger = AutonomyLedger(settings.ledger_path)
    result = panel_judge(
        transport=HttpTransport(), api_key=api_key, governor=gov, prompt=prompt,
        panel=(opts.panel or ",".join(settings.panel_pool)).split(","),
        judge=opts.judge or settings.judge,
        max_tokens=opts.max_tokens,
        reasoning_effort=opts.reasoning_effort or settings.reasoning_effort,
        reasoning_token_budget=settings.reasoning_token_budget,
        task_id=opts.task_id or uuid.uuid4().hex[:8], ledger=ledger,
        max_panelists=settings.max_panelists,
        run_convergence=opts.converge,
        convergence_model=opts.convergence_model or settings.convergence_model,
        claim_polarity={cid.strip(): "reassurance" for cid in
                        (opts.reassurance_claims or "").split(",") if cid.strip()})
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
    with open(opts.source_file, "r", encoding="utf-8") as f:
        quoted = f.read()
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
    continuation = None
    if opts.continue_from:
        with open(opts.continue_from, "r", encoding="utf-8") as f:
            continuation = json.load(f)
    if not opts.file and not continuation:
        raise HarnessError("apply requires --file (or --continue-from <state.json>)")
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
        continuation=continuation)
    _emit(result, opts.out)
    if result["status"] == "verify_failed":
        sys.exit(2)
    if result["status"] == "deferred":
        eprint("[continue] task deferred; run with --continue-from to resume.")
        sys.exit(3)


def _cmd_continue(opts, settings):
    with open(opts.state, "r", encoding="utf-8") as f:
        continuation = json.load(f)
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
    result = engine.apply_edit(
        task_id=opts.task_id, file_path=opts.file, instruction=opts.instruction,
        edit_snippet=opts.edit_snippet, verify_cmd=opts.verify,
        max_rounds=opts.max_rounds, require_consent=opts.require_consent,
        model=opts.model, max_tokens=opts.max_tokens,
        task_max_cost=opts.task_max_cost, allow_escalation=opts.allow_escalation,
        reasoning_effort=opts.reasoning_effort, renew_consent=opts.renew_consent,
        max_rotations=opts.max_rotations, continuation=continuation)
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
        ledger=ledger, required=True)
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
                    help="model for the convergence specialist (default: same as --judge)")
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
    pa.add_argument("--continue-from", default=None, help="resume a deferred task from state.json")
    pa.add_argument("--out", default=None)

    pc = sub.add_parser("continue", help="Continue a deferred/incomplete apply task")
    pc.add_argument("--state", required=True, help="JSON state file from a deferred/verify_failed apply")
    pc.add_argument("--file", default=None)
    pc.add_argument("--instruction", default=None)
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
        elif opts.command == "spend":
            _cmd_spend(settings)
    except HarnessError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()