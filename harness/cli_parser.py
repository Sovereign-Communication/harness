"""The CLI's argparse surface: one owner for flag construction.

cli.py keeps handlers, dispatch, and presentation; every subparser and
flag lives here, so a surface change lands in exactly one module.
Import note: this module needs only argparse -- it must not import
harness.service or any face module (it is the bottom of the cli stack).
"""
import argparse


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
    p.add_argument("--require-attestation", dest="require_diff_authorization",
                   action="store_true", default=None,
                   help="an independent verifier model must allow the exact "
                        "resulting content before every write (fail-closed)")
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
    # --keep-going deliberately diverges from this helper: it is defined on
    # the apply parser only, because the multi-file batch is the only
    # multi-file surface (continue is one file by construction, dogfood runs
    # its own manifest). Do not 'fix' it back into lockstep here.
    # See run_batch's keep_going contract (harness/batch.py).


def _add_output_flags(p):
    """--out (JSON report destination) + --quiet (stderr progress off), plus
    the Phase-1 UI groundwork flags: --events (typed JSONL progress stream for
    UI consumers) and --no-color (strip ANSI from rich stderr rendering).
    Every subcommand that produces a report or progress output takes all."""
    p.add_argument("--out", default=None)
    p.add_argument("--quiet", action="store_true",
                   help="suppress stderr progress notes; report only")
    p.add_argument("--events", default=None, metavar="FILE",
                   help="append typed JSONL progress events to FILE (UI telemetry; "
                        "the JSON result is unchanged)")
    p.add_argument("--no-color", action="store_true",
                   help="disable ANSI color in stderr rendering (NO_COLOR is honored too)")


def build_parser():
    """The harness parser, built fresh per call: every subcommand and flag,
    nothing else. Returns the configured ArgumentParser."""
    ap = argparse.ArgumentParser(
        prog="harness",
        description="Cost-bounded multi-model verification & coding harness with AI sovereignty.")
    sub = ap.add_subparsers(dest="command", required=True)

    # Structured so --help lists the UI faces alongside the data commands.
    sub.add_parser("serve", help="Local web UI + JSON API over the core (loopback; "
                                 "--auth-token optional, HARNESS_UI_AUTH_TOKEN)")
    sub.add_parser("desktop", help="Native desktop window over the same web UI "
                                   "(pywebview; browser fallback)")

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
    pa.add_argument("--keep-going", dest="keep_going", action="store_true", default=False,
                    help="multi-file batch: continue past a failed file; per-file results "
                         "are preserved and the batch still fails honestly (fail-fast "
                         "remains the default)")
    pa.add_argument("--continue-from", default=None, help="resume a deferred task from state.json")
    _add_output_flags(pa)

    pp = sub.add_parser("plan", help="Decompose a goal into an executable DAG with sliding-scale tier routing")
    pp.add_argument("--goal", required=True, help="high-level goal or instruction to decompose and execute")
    pp.add_argument("--execute", action="store_true", default=False, help="execute the planned DAG instead of previewing")
    pp.add_argument("--parallel", dest="parallel", action="store_true", default=None,
                    help="execute independent subtasks concurrently (default: on)")
    pp.add_argument("--no-parallel", dest="parallel", action="store_false",
                    help="execute stages serially (shared-tree mutex)")
    pp.add_argument("--max-workers", type=int, default=4, help="thread pool worker count for parallel execution")
    pp.add_argument("--frontier-model", default=None, help="frontier model or alias for Tier 2 nodes (e.g. fable-5.1, gpt-6)")
    pp.add_argument("--file", action="append", default=None, help="constrain candidate target files")
    pp.add_argument("--decompose-llm", dest="decompose_llm", action="store_true",
                    default=None,
                    help="author the DAG with the cheapest tier-appropriate model "
                         "(schema-validated; default: on when the hourglass is "
                         "active; on --execute a decompose failure falls back to the "
                         "heuristic with a loud note, a plan-only preview fails loudly)")
    pp.add_argument("--no-decompose-llm", dest="decompose_llm", action="store_false",
                    help="force heuristic decomposition even when the hourglass is on")
    pp.add_argument("--confirm", dest="confirm", action="store_true", default=None,
                    help="confirm the plan at the frontier waist before execution: "
                         "condensed brief + bounded file-window round-trips; "
                         "approve/amend/split/refuse verdict (default: on; spends "
                         "against the run's ceiling; a refused plan never executes)")
    pp.add_argument("--no-confirm", dest="confirm", action="store_false",
                    help="skip the frontier waist confirmation")
    pp.add_argument("--plan-consensus", dest="plan_consensus", action="store_true",
                    default=None,
                    help="cheap plan-soundness check before the waist "
                         "(governed_text + JSON sound/reasons; unsound forces "
                         "the waist amend path; ledgered plan_consensus)")
    pp.add_argument("--no-plan-consensus", dest="plan_consensus", action="store_false",
                    help="skip the cheap plan-consensus pre-check")
    pp.add_argument("--final-gate", dest="final_gate", default=None,
                    help="command run after the whole DAG completes (default: on "
                         "when a verify command was discovered/declared -- run-level "
                         "gate or a discovered suite command)")
    pp.add_argument("--no-final-gate", dest="final_gate", action="store_false",
                    help="skip the post-DAG final verification gate")
    pp.add_argument("--resume", dest="resume", default=None,
                    help="path to a pyramid state JSON; re-dispatch only nodes "
                         "that are not already completed ok")
    pp.add_argument("--isolate", dest="isolate", action="store_true", default=None,
                    help="isolate parallel-stage nodes in git worktrees + local branches "
                         "(default: on; overlap-free concurrent nodes only -- nodes "
                         "that share declared target files serialize under the "
                         "shared-tree mutex; merge conflicts fail the node, never "
                         "force-merge; unavailable git degrades to shared-tree mutex execution)")
    pp.add_argument("--no-isolate", dest="isolate", action="store_false",
                    help="execute parallel stages in the shared tree (mutex only)")
    pp.add_argument("--no-attestation", dest="require_diff_authorization",
                    action="store_false", default=None,
                    help="skip diff-bound write attestation on node writes "
                         "(--require-attestation, the default, asks an independent "
                         "verifier model to allow the exact resulting content first)")
    pp.add_argument("--stage-gate", dest="stage_gate", default=None,
                    help="command to run after each parallel stage completes (e.g. the "
                         "full test suite); a failing gate stops the run before "
                         "dependent stages start")
    pp.add_argument("--keep-going", dest="keep_going", action="store_true", default=False, help="continue past a failed subtask")
    _add_engine_flags(pp, max_tokens_default=4096)
    _add_output_flags(pp)

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

    pis = sub.add_parser(
        "issue-sort",
        help="Sort an issue into an operator-declared bucket pack "
             "(JEV-P5; never invents buckets or actions)")
    pis.add_argument("--issue", required=True,
                     help="issue or deferral note text to sort")
    pis.add_argument("--pack", required=True,
                     help="path to the operator bucket pack JSON")
    _add_output_flags(pis)

    plog = sub.add_parser(
        "log-judgment",
        help="JEV-LOG single-pass log analysis against a frozen operator "
             "pack (never invents buckets, levels, or actions)")
    plog.add_argument("--log", required=True,
                      help="path to the raw runtime log dump")
    plog.add_argument("--pack", required=True,
                      help="path to the frozen operator log pack JSON")
    plog.add_argument("--info-sample", type=int, default=0,
                      help="also judge every Nth INFO record (default: WARN/ERROR only)")
    plog.add_argument("--save-to", default=None,
                      help="also write the aggregate JSON artifact to this path")
    _add_output_flags(plog)

    proute = sub.add_parser("route", help="Route a query onto the declared model ladder "
                                           "(cheapest capable rung; unkeyed = deterministic "
                                           "tier heuristic)")
    proute.add_argument("--goal", required=True,
                        help="the request to route")
    proute.add_argument("--pack", required=True,
                        help="operator route pack: {id, rungs: [{rung_id, tier, model, "
                             "cost_class, observed_success?, samples?, guidance?, notes?}]}")
    _add_output_flags(proute)

    psite = sub.add_parser("site-export", help="Verified ledger -> sanitized site-bundle-v1 "
                                              "JSON for the Proof Bench public site "
                                              "(fail-closed: consent + chain verify + secret scan)")
    psite.add_argument("--ledger", required=True)
    psite.add_argument("--consent", required=True)
    psite.add_argument("--pricing", default=None,
                       help="optional pricing snapshot JSON (USD per Mtok, from models --all)")
    psite.add_argument("--yes", action="store_true",
                       help="affirm public release of the sanitized bundle (required)")
    _add_output_flags(psite)

    pl = sub.add_parser("ledger", help="Autonomy ledger")
    pls = pl.add_subparsers(dest="ledger_cmd", required=True)
    for _lc in ("verify", "report"):
        _p = pls.add_parser(_lc)
        _add_output_flags(_p)
    _p = pls.add_parser("tail", help="show the last N ledger entries (default 20)")
    _p.add_argument("n", nargs="?", type=int, default=20)
    _add_output_flags(_p)
    _p = pls.add_parser("defer-stats", help="aggregate WHY runs deferred "
                                            "(panel defer rate, mid-task "
                                            "categories, consent outcomes)")
    _p.add_argument("window", nargs="?", type=int, default=500,
                    help="how many of the most recent entries to scan (default 500)")
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

    pbr = sub.add_parser("brief",
                         help="Build a grounded context pack for a goal "
                              "(hermetic: no network; MR-8 grounding spec)")
    pbr.add_argument("goal")
    pbr.add_argument("--file", dest="files", action="append", default=[],
                     help="source file to cite as a window (repeatable)")
    pbr.add_argument("--validate", action="store_true",
                     help="run the grounding lint over the built pack")
    _add_output_flags(pbr)

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

    pcost = sub.add_parser("cost", help="Track spend by tier, model, and calculate savings vs. naive frontier baseline")
    pcost.add_argument("--last", default=None,
                       help="filter by time window (e.g. '24h', '7d', '30m') or entry count limit")
    pcost.add_argument("--by-tier", action="store_true",
                       help="break down spend by tier (T0..T3)")
    pcost.add_argument("--by-model", action="store_true",
                       help="break down spend by model")
    pcost.add_argument("--savings", action="store_true",
                       help="calculate savings vs. naive frontier-only baseline")
    pcost.add_argument("--json", action="store_true",
                       help="emit raw JSON output only")
    _add_output_flags(pcost)

    prank = sub.add_parser(
        "rankings",
        help="Daily OpenRouter rankings: top models, climbers, and "
             "evidence-driven pool-candidate proposals")
    prank.add_argument("--top", type=int, default=None,
                       help="how many ranked models to report (default: 15)")
    prank.add_argument("--probe", action="store_true",
                       help="gate each proposed candidate through the one-vote "
                            "probe (billable, spend-governed)")
    prank.add_argument("--max-cost", type=float, default=None,
                       help="session cost ceiling in dollars (default: configured max_cost)")
    _add_output_flags(prank)

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

    pjphase = sub.add_parser(
        "jev-phase",
        help="Dogfood Jev 0-100 phase completion score; STATUS complete only "
             "if hard gates pass and score >= min-score")
    pjphase.add_argument("--phase", required=True,
                         help="phase id (JEV-P1, P2, JEV-COMPLETION, ...)")
    pjphase.add_argument("--repo-root", default=".",
                         help="repo root containing docs/jev-roadmap.md and tests")
    pjphase.add_argument("--evidence", default=None,
                         help="optional JSON evidence overrides")
    pjphase.add_argument("--min-score", type=float, default=85.0,
                         help="threshold for can_mark_complete (default 85)")
    pjphase.add_argument("--local-only", action="store_true",
                         help="skip live Jev; use code gates + local semantic score")
    pjphase.add_argument("--json", action="store_true", help="emit raw JSON only")
    _add_output_flags(pjphase)

    # HUL-A/D mission pack surface (run = HUL-D until-limits driver).
    pmiss = sub.add_parser(
        "mission",
        help="Mission pack (HUL-A/D): init | status | resume | findings | run")
    pms = pmiss.add_subparsers(dest="mission_cmd", required=True)
    pmi = pms.add_parser("init", help="create missions/<id>/ pack from mission fields")
    pmi.add_argument("--id", dest="mission_id", required=True, help="mission id")
    pmi.add_argument("--request", required=True, help="the mission request text")
    pmi.add_argument("--success", dest="success_definition", required=True,
                     help="success definition (what 'done' means)")
    pmi.add_argument("--max-cost", dest="max_cost_usd", type=float, required=True,
                     help="limits.max_cost_usd for the mission pack")
    pmi.add_argument("--terminal-reserve", dest="terminal_reserve_cost_usd",
                     type=float, default=0.0,
                     help="terminal_reserve.cost_usd stored in mission.yaml + budget.json")
    pmi.add_argument("--in-scope", default="",
                     help="comma-separated scope.in_scope entries")
    pmi.add_argument("--out-of-scope", default="",
                     help="comma-separated scope.out_of_scope entries")
    pmi.add_argument("--root", default="missions",
                     help="pack parent directory (default: missions)")
    pmi.add_argument("--verifier-kind", default="unspecified",
                     help="verifier.kind recorded in mission.yaml")
    _add_output_flags(pmi)
    for _sub in ("status", "resume", "findings"):
        _p = pms.add_parser(_sub, help=f"mission {_sub}")
        _p.add_argument("--id", dest="mission_id", required=True, help="mission id")
        _p.add_argument("--root", default="missions",
                        help="pack parent directory (default: missions)")
        _add_output_flags(_p)
    prun = pms.add_parser(
        "run",
        help="HUL-D until-limits driver: attempts until limits/stall/success")
    prun.add_argument("--id", dest="mission_id", required=True, help="mission id")
    prun.add_argument("--root", default="missions",
                      help="pack parent directory (default: missions)")
    prun.add_argument("--max-attempts", dest="max_attempts", type=int, default=None)
    prun.add_argument("--stall-limit", dest="stall_limit", type=int, default=5)
    prun.add_argument("--max-tokens", dest="max_tokens", type=int, default=None)
    prun.add_argument("--max-errors", dest="max_errors", type=int, default=None)
    _add_output_flags(prun)

    return ap
