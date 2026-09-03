"""Command-line interface.

Subcommands (flags are back-compatible with SCMessenger's fusion_lite.py and
morph_lite.py, plus delegate_task.py's --verify/--max-rounds):

  harness verify   panel + judge verification
  harness apply    scoped code edit with a verification loop + consent
  harness offer    ask a model for consent on a work item
  harness defer    record a mid-task deferral / consent revocation
  harness ledger   autonomy ledger: tail | verify | report
  harness spend    key identity & spend status

Logs go to stderr; the JSON result goes to stdout (or --out <file>).
Exit codes: 0 success, 1 fatal refusal/error, 2 verification failed.
"""
import argparse
import json
import sys
import uuid

from ._http import HttpTransport
from .apply import ApplyEngine
from .config import load_settings, resolve_api_key
from .core import HarnessError, SpendGovernor, eprint, panel_judge
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


def _cmd_verify(opts, settings):
    api_key, gov = _governor(settings)
    ledger = AutonomyLedger(settings.ledger_path)
    if opts.prompt_file:
        with open(opts.prompt_file, "r", encoding="utf-8") as f:
            prompt = f.read()
    elif opts.prompt:
        prompt = opts.prompt
    else:
        raise HarnessError("verify requires --prompt-file or --prompt")
    if not prompt.strip():
        raise HarnessError("prompt is empty.")
    result = panel_judge(
        transport=HttpTransport(), api_key=api_key, governor=gov, prompt=prompt,
        panel=(opts.panel or ",".join(settings.panel)).split(","),
        judge=opts.judge or settings.judge,
        max_tokens=opts.max_tokens,
        reasoning_effort=opts.reasoning_effort or settings.reasoning_effort,
        task_id=opts.task_id or uuid.uuid4().hex[:8], ledger=ledger)
    _emit(result, opts.out)


def _cmd_apply(opts, settings):
    api_key, gov = _governor(settings)
    ledger = AutonomyLedger(settings.ledger_path)
    router = Router(settings.panel, settings.judge, settings.apply_model,
                    settings.escalation_model, settings.allow_escalation)
    engine = ApplyEngine(HttpTransport(), api_key, gov, ledger, router,
                         settings.default_require_consent)
    result = engine.apply_edit(
        task_id=opts.task_id or uuid.uuid4().hex[:8],
        file_path=opts.file, instruction=opts.instruction,
        edit_snippet=opts.edit_snippet, verify_cmd=opts.verify,
        max_rounds=opts.max_rounds, require_consent=opts.require_consent,
        model=opts.model, max_tokens=opts.max_tokens,
        task_max_cost=opts.task_max_cost, allow_escalation=opts.allow_escalation)
    _emit(result, opts.out)
    if result["status"] in ("verify_failed",):
        sys.exit(2)


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
                          model="(deferral)")
    _emit({"status": "deferred", "task_id": opts.task_id, "reason": opts.reason,
           "ledger_entry": entry}, None)


def _cmd_ledger(opts, settings):
    ledger = AutonomyLedger(settings.ledger_path)
    if opts.ledger_cmd == "tail":
        _emit({"entries": ledger.tail(20), "count": len(ledger.entries())}, None)
    elif opts.ledger_cmd == "verify":
        ok, bad = ledger.verify()
        _emit({"verified": ok, "first_bad_seq": bad}, None)
    elif opts.ledger_cmd == "report":
        _emit(ledger.participation_report(), None)


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
    pv.add_argument("--panel")
    pv.add_argument("--judge")
    pv.add_argument("--max-tokens", type=int, default=None)
    pv.add_argument("--max-cost", type=float, default=None)
    pv.add_argument("--reasoning-effort", default=None,
                    choices=["none", "low", "medium", "high"])
    pv.add_argument("--task-id", default=None)
    pv.add_argument("--out", default=None)

    pa = sub.add_parser("apply", help="Scoped code edit with a verification loop + consent")
    pa.add_argument("--file", required=True)
    pa.add_argument("--instruction", required=True)
    pa.add_argument("--edit-snippet", default=None)
    pa.add_argument("--verify", default=None, help="verification gate command (e.g. 'cargo check')")
    pa.add_argument("--max-rounds", type=int, default=3)
    pa.add_argument("--require-consent", dest="require_consent", action="store_true", default=None)
    pa.add_argument("--no-consent", dest="require_consent", action="store_false")
    pa.add_argument("--task-id", default=None)
    pa.add_argument("--model", default=None)
    pa.add_argument("--max-tokens", type=int, default=4096)
    pa.add_argument("--task-max-cost", type=float, default=0.05)
    pa.add_argument("--allow-escalation", dest="allow_escalation", action="store_true", default=None)
    pa.add_argument("--out", default=None)

    po = sub.add_parser("offer", help="Ask a model for consent on a work item")
    po.add_argument("--task", required=True)
    po.add_argument("--task-id", default=None)
    po.add_argument("--model", default=None)
    po.add_argument("--context", default=None)
    po.add_argument("--out", default=None)

    pd = sub.add_parser("defer", help="Record a mid-task deferral / consent revocation")
    pd.add_argument("--task-id", required=True)
    pd.add_argument("--reason", default=None)

    pl = sub.add_parser("ledger", help="Autonomy ledger")
    pls = pl.add_subparsers(dest="ledger_cmd", required=True)
    pls.add_parser("tail")
    pls.add_parser("verify")
    pls.add_parser("report")

    sub.add_parser("spend", help="Key identity & spend status")

    opts = ap.parse_args(args)
    settings = load_settings()

    try:
        if opts.command == "verify":
            _cmd_verify(opts, settings)
        elif opts.command == "apply":
            _cmd_apply(opts, settings)
        elif opts.command == "offer":
            _cmd_offer(opts, settings)
        elif opts.command == "defer":
            _cmd_defer(opts, settings)
        elif opts.command == "ledger":
            _cmd_ledger(opts, settings)
        elif opts.command == "spend":
            _cmd_spend(settings)
    except HarnessError as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()