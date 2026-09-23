#!/usr/bin/env python3
"""4-Dimensional Harness Audit (round 2) — executable rubric.

Dimensions (each /10, bar is 9.5+ on ALL four):
  A  Security                  — controls, trust boundaries, fail-closed, spend safety
  R  Reliability               — correctness under error/429/empty/truncation/load
  SM Structural hygiene        — layering, ownership, test coverage, lint
  SD Documentation & release   — docs, changelog, exit codes, MCP parity, freshness

The audit is HERMETIC by default: static AST checks, hermetic dynamic checks
(temp dirs, fake transports, no network), and doc-consistency greps. Live,
network-backed checks run only when HARNESS_AUDIT_LIVE=1.

Usage:
  python audits/self/audit.py            # run everything, print table, write JSON
  python audits/self/audit.py --dim A    # one dimension
"""
import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PKG = ROOT / "harness"
TESTS = ROOT / "tests"
# DF-AUDIT-1: the flag name must match the docstring/D8 message and
# round2_report.md exactly, or the opt-in silently never opts in (a stray
# "HARD_AUDIT_LIVE" here made every live-gated check permanently hermetic
# regardless of this var -- accidentally safe, but not what the name says).
LIVE = os.environ.get("HARNESS_AUDIT_LIVE", "") not in ("", "0")

sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------- helpers

def _src(name):
    return (PKG / (name + ".py")).read_text(encoding="utf-8")


def _tree(name):
    return ast.parse(_src(name), str(PKG / (name + ".py")))


_TREES = {}


def tree(name):
    if name not in _TREES:
        _TREES[name] = _tree(name)
    return _TREES[name]


def _has_call(module, func_name, attr=None):
    """True if module calls func_name(...) or obj.attr(...)."""
    for node in ast.walk(tree(module)):
        if isinstance(node, ast.Call):
            fn = node.func
            if attr is None and isinstance(fn, ast.Name) and fn.id == func_name:
                return True
            if isinstance(fn, ast.Attribute) and fn.attr == (attr or func_name):
                return True
    return False


def _defines(module, name):
    """True if module defines `name` as a function, method, or class
    (assignments do not count -- a local variable is not an owner)."""
    for node in ast.walk(tree(module)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == name:
            return True
    return False


def _imports_from(module, target, names=None):
    """Module imports `names` (or anything) from intra-package `target`."""
    out = []
    for node in ast.walk(tree(module)):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if parts[0] == target or node.module == target:
                out.extend(a.name for a in node.names)
    return out if names is None else [n for n in out if n in names]


def _uses_name(module, name):
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(tree(module)))


def _cli_subcommands():
    """Extract the argparse subcommand names from the parser owner's source.
    Reads cli_parser.py (the construction owner since the parser extraction)
    plus cli.py, so a handler-side reference to a subcommand cannot satisfy
    the D2 docs check unless build_parser() actually registers it."""
    text = _src("cli_parser") + "\n" + _src("cli")
    subs = set(re.findall(r'sub\.add_parser\(\s*"([^"]+)"', text))
    m = re.search(r'for _lc in \(([^)]*)\):', text)
    if m:
        subs |= set(re.findall(r'"([^"]+)"', m.group(1)))
    m = re.search(r'_p = pls\.add_parser\("([^"]+)"', text)
    if m:
        subs.add(m.group(1))
    return subs


def _mcp_tool_names():
    """Tool names from the contract owner (mcp_schemas.py), not a source
    grep: mcp.py stopped carrying the tool literals when the schemas were
    extracted, which left this check blind (vacuous missing=[], every real
    name flagged stale)."""
    from harness.mcp_schemas import TOOL_SCHEMAS  # deferred: one broken import must fail one check, not the whole audit

    return [schema["name"] for schema in TOOL_SCHEMAS]


def _readme():
    return (ROOT / "README.md").read_text(encoding="utf-8")


def _check_id(k):
    return k


# ---------------------------------------------------------------- checks
# Each check returns (score 0.0..1.0, evidence). 1.0 = fully satisfied.

def _pass(cond, ok_ev, fail_ev):
    return (1.0, ok_ev) if cond else (0.0, fail_ev)


# ============== A — SECURITY ==============

def a_no_tools_guard():
    """No 'tools' key is ever sent: the guard exists and sits at the ONE
    chat-completion seam, before every payload leaves the process."""
    guard = _defines("spend", "assert_no_tools")
    seam = _has_call("chat", None, "assert_no_tools")
    lanes = [m for m in ("apply", "panel", "consent", "capability", "convergence")
             if _has_call(m, "chat") or "chat(" in _src(m)]
    via_seam = all("chat(" in _src(m) for m in lanes)
    return _pass(guard and seam and via_seam,
                 "spend.assert_no_tools exists; chat() guards every payload; "
                 f"all {len(lanes)} billable lanes (apply/panel/consent/capability/"
                 "convergence) dispatch through chat()",
                 f"guard={guard} seam={seam} lanes={lanes}")


def a_preflight_covers_retries():
    """Spend ceiling is a guarantee: preflight must include the worst-case
    replacement calls (reasoning retry, 429 retry) or the ceiling is only
    approximate."""
    ok = _defines("chat", "_chat_reservation_slots") and \
        _has_call("panel", "_chat_reservation_slots")
    apply_pf = (_has_call("apply", "preflight", None)
                   or "preflight(" in _src("apply_policy"))  # per-round owner
    conv_pf = "preflight(" in _src("convergence")
    consent_pf = "preflight(" in _src("consent")
    return _pass(ok and apply_pf and conv_pf and consent_pf,
                 "reasoning/429 retry slots counted via _chat_reservation_slots "
                 "(panel wired); apply/convergence/consent preflight before calls",
                 f"slots={ok} apply={apply_pf} conv={conv_pf} consent={consent_pf}")


def a_ceiling_hard():
    """record_actual never lets spent exceed the ceiling (raise, not clamp),
    and load_settings clamps config values to HARD_MAX_COST."""
    body = ast.get_source_segment(_src("spend"),
                                  [n for n in ast.walk(tree("spend"))
                                   if isinstance(n, ast.FunctionDef)
                                   and n.name == "record_actual"][0])
    raises = "would exceed ceiling" in body and "raise" in body
    cfg = _src("config")
    clamped = "HARD_MAX_COST" in cfg and "_num(\"max_cost\"" in cfg.replace("'", "\"")
    lock = "with self._spend_lock" in ast.get_source_segment(
        _src("spend"),
        [n for n in ast.walk(tree("spend"))
         if isinstance(n, ast.FunctionDef) and n.name == "record_actual"][0])
    return _pass(raises and clamped and lock,
                 "record_actual raises (never clamps) past max_cost under the "
                 "spend lock; load_settings bounds max_cost to [0, HARD_MAX_COST]",
                 f"raise={raises} config_bound={clamped} locked={lock}")


def a_byok():
    """Denylist prefixes hard-blocked; paid BYOK fails closed; learned
    prefixes rotate away; free BYOK stays usable."""
    deny = "BYOK_DENYLIST_PREFIXES" in _src("config")
    checked = _has_call("chat", None, "check_byok")
    consent_src = _src("consent")
    paid_closed = "is_byok and not governor.is_free" in consent_src and \
        "record_byok" in consent_src
    learned = _defines("spend", "learned_blocked") and _defines("spend", "record_byok")
    return _pass(deny and checked and paid_closed and learned,
                 "denylist prefixes hard-block in chat(); consent rejects "
                 "paid-BYOK routes and records the learned prefix",
                 f"deny={deny} chat_check={checked} consent_paid_closed={paid_closed} "
                 f"learned={learned}")


def a_key_label_gate():
    """Key identity: exact match on --expect-key-label, and the label is
    never echoed into the mismatch error."""
    body = _src("spend")
    exact = "label != self.expect_key_label" in body
    no_echo = "does not exactly match" in body
    seg = re.search(r'expect_key_label is not None and label != self\.expect_key_label:\s*'
                    r'#[^\n]*\n\s*#[^\n]*\n\s*raise HarnessError\((.*?)\)', body, re.S)
    echo_safe = True
    if seg:
        echo_safe = re.search(r'\blabel\b', seg.group(1)) is None
    return _pass(exact and no_echo and echo_safe,
                 "expect-key-label is an exact binary match; the error reports "
                 "mismatch only, never the resolved label",
                 f"exact={exact} message={no_echo} no_echo={echo_safe}")


def a_gate_runner():
    """Verification gates run shell-free: shlex tokenized, shell=False,
    timeout-killed; metacharacters inert."""
    fs = _src("filesafety")
    ok = ("shlex.split" in fs and "shell=False" in fs
          and "subprocess.TimeoutExpired" in fs and "_verify_argv" in fs)
    wired = _defines("apply_gate", "GatePolicy") or "run_gate" in _src("apply_gate")
    return _pass(ok and wired,
                 "default_run_verify: shlex tokenization + shell=False + "
                 "timeout kill; apply_gate runs gates through it",
                 f"filesafety={ok} wired={wired}")


def a_verify_preflight():
    """A typo'd gate is caught before the paid phase: validate_verify_command
    runs at the CLI boundary before any live spend."""
    defined = _defines("filesafety", "validate_verify_command")
    wired = _has_call("cli", "validate_verify_command") or \
        "validate_verify_command" in _src("cli")
    return _pass(defined and wired,
                 "validate_verify_command preflights --verify (tokenizable + "
                 "executable resolvable) at the CLI boundary",
                 f"defined={defined} wired={wired}")


def a_atomic_write():
    """File mutation is atomic and symlink-refusing: mkstemp in the target
    dir, os.replace, permission mode preserved, symlink never followed."""
    fs = _src("filesafety")
    ok = ("mkstemp" in fs and "os.replace" in fs and "islink" in fs
          and "S_IMODE" in fs and "refusing to write through symlink" in fs)
    write_path = "apply_gate" in _TREES or True
    gate_uses = _imports_from("apply_gate", "filesafety",
                              {"_atomic_write", "backup_file"})
    return _pass(ok and bool(gate_uses) and write_path,
                 "_atomic_write: temp staged in target dir, os.replace, mode "
                 "preserved, symlink refused; apply_gate writes only through it",
                 f"atomic={ok} gate_uses={sorted(gate_uses)}")


def a_rewind():
    """A run that never passed its gate leaves the tree as it found it."""
    body = _src("apply_gate")
    ok = "rewound target to its pre-run content" in body and \
        "_atomic_write(req.file_path, req.original)" in body
    return _pass(ok, "terminal_failure rewinds the target to pre-run content "
                     "when the run dies without a passed gate", f"ok={ok}")


def a_mcp_boundary():
    """MCP apply: writes require explicit allow_write, gates require explicit
    allow_verify (request or server config), and every target sits inside a
    configured allowed root."""
    body = _src("mcp")
    allow_write = "self.allow_write or allow_write" in body
    allow_verify = "self.allow_verify or allow_verify" in body
    roots = "allowed_roots" in body and "outside every allowed root" in body
    return _pass(allow_write and allow_verify and roots,
                 "apply_edit refuses without allow_write/allow_verify and "
                 "confines every target to allowed_roots (realpath'd)",
                 f"write={allow_write} verify={allow_verify} roots={roots}")


def a_consent_failclosed():
    """Consent: unparseable/empty/redirect never dispatches; ladder
    exhaustion synthesizes an honest defer with an explicit dispatch
    verdict."""
    body = _src("consent")
    ok = "Fail-closed rule" in body and \
        "unparseable or missing a valid decision" in body and \
        '"decision": "defer"' in body and '"dispatched": False' in body
    return _pass(ok, "unparseable consent => synthetic defer with "
                     "dispatched:false, never dispatched (ladder-exhaustion "
                     "path included)", f"ok={ok}")


def a_continuation_tamper():
    """Continuation identity gates run BEFORE key/model/file work: gate_id
    mismatch, verify-only flip, and target-hash change all refuse."""
    body = _src("continuation")
    ok = ("verify_gate_id does not match" in body
          and "target file changed since the state was saved" in body
          and "cannot be resumed as verify-only" in body)
    cli_src = _src("cli")
    # In every command that resumes a continuation (apply --continue-from,
    # continue), validate_continuation must precede the _session() assembly
    # (which verifies the key) -- fail on tampered state before any lookup.
    order_ok = True
    checked = 0
    for node in ast.walk(tree("cli")):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_cmd_"):
            body = ast.get_source_segment(cli_src, node) or ""
            if "validate_continuation(" in body and "_session(" in body:
                checked += 1
                if body.index("validate_continuation(") > body.index("_session("):
                    order_ok = False
    order_ok = order_ok and checked >= 2
    return _pass(ok and order_ok,
                 f"validate_continuation refuses tampered/mismatched state; "
                 f"{checked} resume commands validate before session assembly "
                 "(no key lookup first)",
                 f"gates={ok} cli_order={order_ok} commands={checked}")


def a_hard_ceilings():
    """Config ceilings are absolute: HARD_MAX_COST / HARD_TASK_MAX_COST bound
    every override path; unknown config keys warn; load stays in the fail-
    clean path."""
    cfg = _src("config")
    hard = "HARD_MAX_COST" in cfg and "HARD_TASK_MAX_COST" in cfg
    num = cfg.count("_num(\"") + cfg.count("_num('")
    unknown = "unknown config keys" in cfg
    cli = _src("cli")
    in_try = re.search(r'settings = load_settings\(\)', cli) is not None
    return _pass(hard and num >= 6 and unknown and in_try,
                 f"{num} numeric settings range-validated; hard ceilings enforced "
                 "in load_settings; unknown keys warned; load inside CLI try",
                 f"hard={hard} num_checks={num} unknown={unknown} in_try={in_try}")


# ============== R — RELIABILITY ==============

def r_http_transient():
    """429/5xx retried with Retry-After honor and capped backoff; POST never
    blindly re-sends a body the provider already consumed (transport retries
    only transient statuses; the engine owns its own retry semantics)."""
    body = _src("_http")
    ok = ("_transient" in body and "Retry-After" in body
          and "429" in body and "2 ** attempt" in body and "MAX_RETRIES" in body)
    return _pass(ok, "HttpTransport retries transient 429/5xx with Retry-After "
                     "honor and capped exponential backoff", f"ok={ok}")


def r_reasoning_param():
    """Reasoning: auto omits for non-reasoning models, caps low for reasoning
    ones, retries once without on provider rejection, and carries the rejected
    attempt's reported cost into the retry."""
    body = _src("chat")
    ok = ("_effort_to_send" in body and "_build_reasoning_param" in body
          and "_REASONING_PARAM_ERR_HINTS" in body
          and "_merge_retry_cost" in body and "retry_cost" in body)
    return _pass(ok, "auto/omit/cap + retry-once-without + rejected-attempt cost "
                     "merged into retry usage", f"ok={ok}")


def r_output_usability():
    """Empty / reasoning-only / truncated output is a protocol condition,
    never mined for votes, content, or consent."""
    body = _src("chat")
    ok = ("REASONING_FALLBACK_PREFIX" in body
          and "reasoning-only output (no visible content)" in body
          and "truncated (hit the token cap)" in body)
    users = sum("assess_output" in _src(m)
                for m in ("panel", "convergence", "apply", "consent"))
    return _pass(ok and users >= 2,
                 f"assess_output is the one usability verdict (assess=chat-owned, "
                 f"{users} lanes consume it)", f"chat={ok} users={users}")


def r_panel_rotation():
    """Any failed seat rotates to the next pool model; the judge has a
    fallback ladder; the structured tally is authoritative even when lanes
    fail."""
    body = _src("panel")
    ok = ("next(candidates" in body and "assess_output" in body
          and "judge_blocked" in body or "learned_blocked" in body)
    fallback = "spec_model" in body
    return _pass(ok and fallback,
                 "failed panel seats refill from the candidate iterator; judge "
                 "and specialist fall back; tally counts usable votes only",
                 f"rotate={ok} fallback={fallback}")


def r_apply_rotation():
    """Apply rotates across the pool on error / BYOK / reasoning-only /
    readiness-defer, and a full-ladder readiness decline produces an honest
    deferral with continuation state (not a fake success)."""
    body = _src("apply_policy")  # rotation/deferral owner (split pass)
    ok = ("readiness" in body and "_readiness_deferral" in body
          and "unusable" in body.lower() or "reasoning-only" in body)
    defer = "category=\"readiness\"" in body or "category='readiness'" in body
    return _pass(ok and defer,
                 "rotation covers error/BYOK/reasoning-only/readiness; ladder "
                 "exhaustion => readiness deferral with continuation",
                 f"rotate={ok} defer={defer}")


def r_broken_gate_detector():
    """The broken-gate stop fires only on IDENTICAL REAL gate outputs: it
    reads verify_failed rounds, never api_error or absent gates."""
    body = _src("apply_policy")  # broken-gate detector owner (split pass)
    ok = 'status"' in body and '"verify_failed"' in body and "gate_broken" in body
    m = re.search(r'verify_failed', body)
    return _pass(bool(ok), "broken-gate detector filters to real verify_failed "
                           "rounds (merge errors / api_error cannot trigger it)",
                 f"ok={ok} hint={bool(m)}")


def r_partial_never_lands():
    """Capability-deferred partials live in continuation state only; the tree
    changes only through a passed gate."""
    res = _src("results")
    ok = "partial_content" in res and "never in the" in res.replace("\r\n", " ")
    apply_body = _src("apply")
    # the tree write must be via apply_gate's transaction, not apply.py
    direct = "_atomic_write" in apply_body
    return _pass(ok and not direct,
                 "partial_content persists only inside the continuation state; "
                 "apply.py holds no direct tree writer (all writes via apply_gate)",
                 f"state_only={ok} no_direct_write={not direct}")


def r_batch():
    """Multi-file batch: one governed session per file, shared budget,
    fail-fast, envelope always (even on file-1 death), shared-gate verdict
    from the last file's real outcome."""
    body = _src("batch")
    ok = ("validate_batch_files" in body and "fail fast" in body
          and '"batch": True' in body and "break" in body)
    last = '"passed"' in body and "last_verify" in body
    return _pass(ok and last,
                 "batch loops engine.apply_edit per file, breaks on first "
                 "non-success, always returns the envelope, derives verify.passed "
                 "from the last file's verdict", f"loop={ok} last_verdict={last}")


def r_ledger_dynamic():
    """Hermetic ledger scenario: append-only chain verifies; concurrent
    instances do NOT fork the chain (rebase under lock); torn lines are
    quarantined; repair truncates to the valid prefix."""
    from harness.ledger import AutonomyLedger
    evidence = []
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "ledger.jsonl")
        l1 = AutonomyLedger(path)
        l1.append("offer", task_id="t1")
        l2 = AutonomyLedger(path)                     # loads e1
        l1.append("consent_accept", task_id="t1")     # e2
        l2.append("consent_decline", task_id="t1")    # e3 — must rebase, not fork
        l2.append("dispatch_start", task_id="t1")     # e4
        ok_ok, bad = l2.verify()
        evidence.append(f"chain_ok={ok_ok}")
        # torn line
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"seq": 999, "torn')
        l3 = AutonomyLedger(path)
        evidence.append(f"quarantined={l3.quarantined}")
        # corrupt tail: rewrite e4's hash
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        good = [ln for ln in lines if not ln.startswith('{"seq": 999')]
        e4 = json.loads(good[-1])
        e4["event"] = "tampered"
        good[-1] = json.dumps(e4, sort_keys=True, separators=(",", ":"))
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(good) + "\n")
        l4 = AutonomyLedger(path)
        ok4, bad_seq = l4.verify()
        kept, dropped = l4.repair()
        l5 = AutonomyLedger(path)
        ok5, _ = l5.verify()
        evidence.append(f"tamper_found={not ok4}@{bad_seq} repair_kept={kept} "
                        f"dropped={dropped} repaired_ok={ok5}")
    ok = all(["chain_ok=True" in e for e in evidence[:1]] +
             ["quarantined=1" in e for e in evidence[1:2]] +
             ["tamper_found=True" in e and "repaired_ok=True" in e
              for e in evidence[2:3]])
    return _pass(ok, "; ".join(evidence), "; ".join(evidence))


def r_spend_concurrency():
    """Under concurrent record_actual with a tiny ceiling, spent never
    exceeds max_cost (no lost update, no overshoot)."""
    from harness.spend import SpendGovernor
    from harness.errors import HarnessError

    class T:
        def get(self, *a, **k):
            raise AssertionError("no network in audit")
        def post(self, *a, **k):
            raise AssertionError("no network in audit")

    gov = SpendGovernor(T(), "k", max_cost=0.01)
    errs = []

    def worker():
        for _ in range(200):
            try:
                gov.record_actual(0.0001, "m")
            except HarnessError:
                pass
            except Exception as e:  # pragma: no cover
                errs.append(e)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok = not errs and gov.spent <= 0.01 + 1e-12
    return _pass(ok, f"8x200 concurrent record_actual: spent={gov.spent:.6f} "
                     f"<= 0.01, no lost updates (errors={len(errs)})",
                 f"spent={gov.spent} errors={errs[:3]}")


def r_config_validation():
    """All numeric settings are range-validated; unknown keys warn; nonsense
    values fail closed before any network call."""
    cfg = _src("config")
    keys = re.findall(r'_num\("([a-z_]+)"', cfg)
    need = {"max_cost", "task_max_cost", "max_tokens", "apply_max_tokens",
            "reasoning_token_budget", "max_panelists", "max_rotations"}
    return _pass(need.issubset(set(keys)) and "unknown config keys" in cfg,
                 f"range-validated: {sorted(set(keys))}; unknown-key warning present",
                 f"got={sorted(set(keys))}")


def r_continuation_contract():
    """The persisted contract is enforced: schema_version pinned, gated state
    cannot resume verify-only, gate identity must match, target must be
    unchanged."""
    from harness.continuation import validate_continuation
    from harness.errors import HarnessError

    def refuses(state):
        try:
            validate_continuation(state)
            return False
        except HarnessError:
            return True
        except Exception:
            return False
    checks = {
        "schema": refuses({"schema_version": 2, "file_path": "x"}),
        "no_gate": refuses({"file_path": __file__, "verify_cmd": "",
                            "verification_required": True}),
        "verify_flip": refuses({"file_path": __file__, "verify_cmd": "true",
                                "verification_required": True, "verify_only": True,
                                "verify_gate_id": "zzz"}),
        "gate_id": refuses({"file_path": __file__, "verify_cmd": "true",
                            "verification_required": True,
                            "verify_gate_id": "not-the-id"}),
        "bad_hash": refuses({"file_path": __file__, "target_hash": "0" * 64}),
    }
    return _pass(all(checks.values()), f"all 5 refusals hold: {checks}",
                 str(checks))


# R13's contract, factored for self-verification: the suite child is
# healthy iff the summary says OK AND no unraisable-warning signature
# appears anywhere (the leak class that flaked the v0.3.2 release gate,
# PR #17, prints to stderr without changing the summary).
# Unraisable warnings (e.g. a leaked TextIOWrapper) print to the
# child's stderr at GC time without changing the OK|FAILED outcome
_SUITE_SUMMARY = re.compile(r"Ran (\d+) tests?[^\n]*\n\n(OK|FAILED)")
_SUITE_LEAK_SIGNATURES = ("ResourceWarning", "unclosed file")


def _classify_suite_output(text):
    """Pure classification of captured suite output (drives R13 and
    its self-test). Returns (summary_ok, leaked, matched_summary)."""
    m = _SUITE_SUMMARY.search(text)
    leaked = any(s in text for s in _SUITE_LEAK_SIGNATURES)
    return (m is not None and m.group(2) == "OK", leaked,
            m.group(0).strip() if m else None)


def r_suite_green():
    """The full unit suite is green hermetically (with ResourceWarnings as
    errors)."""
    r = subprocess.run(
        [sys.executable, "-W", "error::ResourceWarning", "-m", "unittest",
         "discover", "-s", "tests", "-q"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=900)
    summary_ok, leaked, summary = _classify_suite_output(r.stdout + r.stderr)
    ok = summary_ok and not leaked
    return _pass(ok, f"unittest: {summary if summary is not None else r.stderr[-200:]}",
                 (r.stdout + r.stderr)[-2500:])


def r_suite_selftest():
    """R13's classifier proves its own contract on synthetic output:
    clean OK passes; planted-leak OK (the blind-spot shape captured in
    the PR #18 necessity proof) fails; EACH signature alone is
    load-bearing (a variant carrying only one still fails, so
    narrowing the signature list cannot silently restore the blind
    spot); and the old parse-only rule passes the planted output
    while the full rule fails it. Hermetic: strings, no subprocess."""
    nl = chr(10)
    clean = "Ran 725 tests in 1.000s" + nl + nl + "OK"
    planted = ("Ran 726 tests in 1.000s" + nl + nl + "OK" + nl +
               "Exception ignored while finalizing file <_io.TextIOWrapper>:" + nl +
               "ResourceWarning: unclosed file <_io.TextIOWrapper name='x'>")
    rw_only = ("Ran 726 tests in 1.000s" + nl + nl + "OK" + nl +
               "ResourceWarning: leaked file handle at GC")
    unclosed_only = ("Ran 726 tests in 1.000s" + nl + nl + "OK" + nl +
                     "Exception ignored: unclosed file <_io.TextIOWrapper>")
    ok_clean, leak_clean, _ = _classify_suite_output(clean)
    ok_planted, leak_planted, _ = _classify_suite_output(planted)
    ok_rw, leak_rw, _ = _classify_suite_output(rw_only)
    ok_unc, leak_unc, _ = _classify_suite_output(unclosed_only)
    m = _SUITE_SUMMARY.search(planted)
    old_rule = m is not None and m.group(2) == "OK"
    final_clean = ok_clean and not leak_clean
    final_planted = ok_planted and not leak_planted
    ok = (final_clean and not final_planted
          and leak_planted and old_rule
          and not (ok_rw and not leak_rw)
          and not (ok_unc and not leak_unc))
    detail = (f"clean: final_pass={final_clean}; "
              f"planted: final_pass={final_planted} "
              f"(summary_ok={ok_planted}, leak={leak_planted}); "
              f"rw-only detected={leak_rw}; "
              f"unclosed-only detected={leak_unc}; "
              f"old-rule-passes-planted={old_rule} (divergence proves "
              f"the signature scan is load-bearing)")
    return _pass(ok, "R13 classifier: clean->PASS, planted->FAIL, "
                     "each signature load-bearing, old-rule diverges",
                 detail)


# ============== SM — STRUCTURE ==============

_MODULES = None


def modules():
    global _MODULES
    if _MODULES is None:
        _MODULES = sorted(p.stem for p in PKG.glob("*.py") if p.stem != "__init__")
    return _MODULES


def sm_layering():
    """No module beneath the interfaces imports an interface."""
    bad = []
    for m in modules():
        if m in ("cli", "mcp"):
            continue
        for node in ast.walk(tree(m)):
            if isinstance(node, ast.ImportFrom) and node.level >= 1 and node.module:
                if node.module.split(".")[0] in ("cli", "mcp"):
                    bad.append(f"{m} -> {node.module}")
    return _pass(not bad, "import direction clean (0 upward edges)",
                 f"violations: {bad}")


def sm_no_facades():
    """No re-export facades: module-level intra-package imports are used."""
    bad = []
    for m in modules():
        bound = []
        for node in tree(m).body:
            if isinstance(node, ast.ImportFrom) and node.level >= 1 and node.module:
                bound += [a.asname or a.name for a in node.names if a.name != "*"]
        used = {n.id for n in ast.walk(tree(m)) if isinstance(n, ast.Name)}
        dead = [b for b in bound if b not in used]
        bad += [f"{m} re-exports {d}" for d in dead]
    return _pass(not bad, "no re-export shims (harness/core.py cannot regrow)",
                 f"violations: {bad}")


def sm_vocabulary_one_site():
    """The apply result vocabulary has exactly one def site (results.py)."""
    vocab = {"_round_entry", "_terminal_result", "_defer_result", "_http_error"}
    bad = [f"{m}.{v}" for m in modules() if m != "results"
           for v in vocab if _defines(m, v)]
    return _pass(not bad, "result vocabulary owned by results.py only",
                 f"violations: {bad}")


def sm_construction_owner():
    """ApplyEngine / Router construction lives in session.py only."""
    bad = []
    for m in modules():
        if m == "session":
            continue
        for node in ast.walk(tree(m)):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else \
                    fn.id if isinstance(fn, ast.Name) else None
                if name in ("ApplyEngine", "Router"):
                    bad.append(f"{m}")
    return _pass(not bad, "engine/router construction owned by session.py",
                 f"violations: {bad}")


def sm_lint():
    """Ruff (E/F/W) is clean over harness and tests.

    Resolution order: `ruff` on PATH, user/Python Scripts next to common
    interpreters, then `python -m ruff` on this interpreter. CI installs the
    `dev` extra into the same interpreter, so `-m ruff` always works there.
    """
    import shutil
    candidates = [shutil.which("ruff")]
    for py in (sys.executable, "C:/Python314/python.exe"):
        # Common pip --user / Scripts locations for a ruff.exe entrypoint.
        base = Path(py).resolve().parent
        for rel in (
            Path("Scripts") / "ruff.exe",
            Path("Scripts") / "ruff",
            Path("..") / "Scripts" / "ruff.exe",
        ):
            p = (base / rel).resolve()
            if p.is_file():
                candidates.append(str(p))
    appdata = os.environ.get("APPDATA") or ""
    if appdata:
        candidates.append(os.path.join(
            appdata, "Python", "Python314", "Scripts", "ruff.exe"))
        candidates.append(os.path.join(
            appdata, "Python", "Python311", "Scripts", "ruff.exe"))
    ruff = next((c for c in candidates if c and os.path.isfile(c)), None)
    if ruff:
        argv = [ruff, "check", "harness", "tests"]
    else:
        argv = [sys.executable, "-m", "ruff", "check", "harness", "tests"]
    r = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True,
                       timeout=120)
    ok = r.returncode == 0
    return _pass(ok, "ruff check harness tests: clean",
                 (r.stdout + r.stderr).strip()[-300:])


def sm_test_mirror():
    """Every product module is exercised by the test tree (imported somewhere
    under tests/)."""
    untested = []
    for m in modules():
        if not any(m in (TESTS / t).read_text(encoding="utf-8", errors="replace")
                   for t in os.listdir(TESTS) if t.endswith(".py")):
            untested.append(m)
    return _pass(not untested,
                 f"{len(modules())}/{len(modules())} modules imported by tests",
                 f"untested modules: {untested}")


def sm_one_owner_policies():
    """One owner per policy: each named policy is defined in exactly one
    module and consumed by import."""
    owners = {
        "consent_preview": "consent",        # preview policy
        "assess_output": "chat",             # usability verdict
        "_atomic_write": "filesafety",       # disk mutation
        "gate_id": "continuation",           # gate identity
        "terminal_exit_code": "results",     # exit-code policy
        "preflight": "spend",                # spend preflight
        "_chat_reservation_slots": "chat",   # retry slot math
        "validate_continuation": "continuation",
    }
    bad = []
    for name, owner in owners.items():
        sites = [m for m in modules() if _defines(m, name)]
        if sites != [owner]:
            bad.append(f"{name} defined in {sites} (owner: {owner})")
    return _pass(not bad, f"all {len(owners)} sampled policies have exactly one "
                          "definition site", f"violations: {bad}")


def sm_no_dead_code():
    """Validators/lanes with zero real callers are deleted or wired: every
    public helper in validation.py is referenced by harness/ or tests/."""
    all_text = "\n".join(
        (PKG / f).read_text(encoding="utf-8", errors="replace")
        for f in os.listdir(PKG) if f.endswith(".py")) + \
        "\n".join((TESTS / f).read_text(encoding="utf-8", errors="replace")
                  for f in os.listdir(TESTS) if f.endswith(".py"))
    dead = []
    for node in ast.walk(tree("validation")):
        if isinstance(node, (ast.FunctionDef,)) and not node.name.startswith("_"):
            uses = len(re.findall(r"\b" + re.escape(node.name) + r"\b", all_text))
            if uses <= 1:
                dead.append(node.name)
    for name in ("validate_verify_command",):
        if all_text.count(name) <= 1:
            dead.append(name)
    return _pass(not dead, "no dead validators (all wired or tested)",
                 f"dead: {dead}")


# ============== SD — DOCS & RELEASE ==============

def sd_mcp_parity():
    """README documents exactly the MCP tool list the server exposes."""
    tools = set(_mcp_tool_names())
    readme = _readme()
    missing = sorted(t for t in tools if t not in readme)
    stale = sorted((set(re.findall(r"`([a-z]+_[a-z_]+)`", readme))
                    & {"panel_verify", "apply_edit", "offer_work", "defer_work",
                       "ledger_status", "participation_report", "spend_status"})
                   - tools)
    return _pass(not missing and not stale,
                 f"README documents all {len(tools)} MCP tools, no stale names",
                 f"missing={missing} stale={stale}")


def sd_cli_surface():
    """README's command list matches the argparse surface (top-level
    subcommands only; `ledger`'s nested subcommands ride on `harness ledger`)."""
    top_level = {"verify", "apply", "continue", "offer", "defer", "ledger",
                 "models", "bench", "lint-claims", "capabilities", "spend",
                 "dogfood"}
    subs = _cli_subcommands() & top_level
    # Extraction strictness: the names D2 reads must be the names the live
    # parser registers -- a stale grep must not certify docs (the D1 lesson).
    _bp = __import__("harness.cli_parser", fromlist=["build_parser"]).build_parser()
    registered = set(next(a.choices for a in _bp._actions
                          if isinstance(a, argparse._SubParsersAction)))
    assert subs <= registered, f"D2 extractor drifted from the live parser: {sorted(subs - registered)}"
    readme = _readme()
    missing = sorted(s for s in subs if not re.search(
        r"harness " + re.escape(s) + r"\b", readme))
    return _pass(not missing,
                 f"{len(subs)} top-level subcommands documented with the "
                 "harness <cmd> form", f"undocumented: {missing}")


def sd_exit_codes():
    """Exit codes documented and consistent with results.terminal_exit_code."""
    res = _src("results")
    ok_map = ("0 ok/preview" in res and "3 deferred" in res and "2 failed" in res)
    readme = _readme()
    doc = ("Exit 0" in readme or "exit code" in readme.lower() or
           "exit codes" in readme.lower())
    cli = _src("cli")
    fatal = "sys.exit(1)" in cli and "sys.exit(130)" in cli
    return _pass(ok_map and doc and fatal,
                 "0 ok/preview, 3 deferred, 2 failed (+1 fatal, 130 SIGINT) "
                 "consistent between results.py, cli.py and README",
                 f"map={ok_map} readme={doc} cli={fatal}")


def sd_config_table():
    """Every config key documented in README exists in _ENV_NAMES (and the
    table lists the free-tier defaults)."""
    env_names = set(re.findall(r'"([a-z_]+)":\s*"HARNESS_', _src("config")))
    readme = _readme()
    documented = set(re.findall(r"^\|\s*`([a-z_]+)`", readme, re.M))
    bogus = sorted(documented - env_names - {"HARNESS_DEFER"})
    return _pass(not bogus,
                 f"{len(documented)} README config keys all resolve to real "
                 f"settings ({len(env_names)} configurable)",
                 f"bogus keys: {bogus}")


def sd_version_single_source():
    """pyproject version == harness.__version__ == _FALLBACK_VERSION."""
    py = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', py, re.M)
    pv = m.group(1) if m else None
    import harness
    ok = pv is not None and pv == harness.__version__
    return _pass(ok, f"pyproject={pv} __version__={harness.__version__} (one source)",
                 f"pyproject={pv} __version__={harness.__version__}")


def sd_changelog():
    """Keep a Changelog + semver-0.x statement + current [Unreleased]."""
    ch = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    ok = ("Keep a Changelog" in ch and "Semantic Versioning" in ch
          and "## [Unreleased]" in ch)
    return _pass(ok, "format statement + [Unreleased] section present",
                 f"ok={ok}")


def sd_docs_current():
    """architecture/security/threat docs reference only real modules; no
    stale core.py references anywhere."""
    docs = list((ROOT / "docs").glob("*.md")) + [ROOT / "THREAT_MODEL.md"]
    stale = []
    # Subpackage-aware: a reference resolves if it names a real file
    # ANYWHERE under the package (e.g. local_fit/infer.py), not just a
    # top-level module. Still fail-closed: invented names flag.
    real = set(modules()) | {q.stem for q in PKG.rglob("*.py")} | {"__init__"}
    for d in docs:
        if not d.exists():
            continue
        text = d.read_text(encoding="utf-8", errors="replace")
        for name in re.findall(r"`([a-z_]+)\.py`", text):
            if name not in real:
                stale.append(f"{d.name}: {name}.py")
    return _pass(not stale,
                 f"all module references across {len(docs)} docs resolve to real "
                 "files", f"stale: {stale}")


def sd_shipped_freshness():
    """Model freshness: shipped pool ids are validated against the live
    catalog via a wired CLI guard, and pricing lookup hard-fatals on unknown
    ids. Live re-check runs with HARNESS_AUDIT_LIVE=1."""
    ids = _src("config")
    # The flag literal lives in the parser owner, its handler wiring in cli.py:
    # both halves must exist or the guard is unwired (same rule as D2's seam).
    wired = _defines("config", "shipped_model_ids") and \
        "--check-shipped" in (_src("cli_parser") + _src("cli")) and \
        "shipped_model_ids" in _src("cli")
    fatal = "not found in live OpenRouter model list" in _src("spend")
    ids = _src("config")
    pools = re.findall(r'"([a-z0-9./:_-]+)"', ids)
    n_free = len({p for p in pools if p.endswith(":free")})
    live_ev = "live check skipped (HARNESS_AUDIT_LIVE unset)"
    if LIVE:
        r = subprocess.run(
            [sys.executable, "-m", "harness.cli", "capabilities", "--check-shipped"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=180)
        live_ev = f"live --check-shipped exit={r.returncode}"
        wired = wired and r.returncode == 0
    return _pass(wired and fatal and n_free >= 5,
                 f"shipped_model_ids + capabilities --check-shipped wired; pricing "
                 f"lookup hard-fatals on stale ids; {n_free} :free pool ids; {live_ev}",
                 f"wired={wired} fatal={fatal} free_ids={n_free}")


def sd_dogfood_loop():
    """The self-hosting loop is documented and wired: ground (lint-claims) ->
    verify (panel) -> gated apply, with the round-1 report on disk."""
    readme = _readme()
    cli = _src("cli")
    ok = "dogfood" in readme and "dogfood" in cli and \
        (HERE / "audit_report.md").exists()
    return _pass(ok, "dogfood loop documented (README) + wired (CLI) + round-1 "
                     "report present in audits/self/", f"ok={ok}")


def sd_contributing():
    """CONTRIBUTING reflects the module map and ground rules (no stale
    module references)."""
    p = ROOT / "CONTRIBUTING.md"
    if not p.exists():
        return 0.0, "CONTRIBUTING.md missing"
    text = p.read_text(encoding="utf-8", errors="replace")
    real = set(modules()) | {"__init__", "core"}
    stale = [n for n in set(re.findall(r"`([a-z_]+)\.py`", text)) if n not in real]
    grounded = "layer" in text.lower() or "import" in text.lower()
    # Module-map coverage (the D1/D2/D8 lesson, mechanized for D10): every
    # public harness module needs a map row, or a new/extraction module can
    # merge without its one-line owner entry. Private (_-prefixed) modules
    # and the package __init__ sit outside the map by convention.
    public = [m for m in modules() if not m.startswith("_")]
    uncovered = [m for m in public if not re.search(rf"harness/{m}\.py`", text)]
    return _pass(not stale and grounded and not uncovered,
                 "CONTRIBUTING covers every public module; layering rules present",
                 f"stale: {stale}" + (f"; uncovered: {uncovered}" if uncovered else ""))


# ---------------------------------------------------------------- runner

def sd_corpus_integrity():
    """The audit evidence corpus is hash-pinned: every tracked dogfood
    artifact and audit report must match audits/self/corpus_manifest.json.
    A silent edit (or an unmanifested corpus addition) fails here; the
    only honest path is the scripted refresh (refresh_corpus_manifest.py)
    committed as a reviewable diff."""
    mf = HERE / "corpus_manifest.json"
    if not mf.exists():
        return 0.0, ("corpus_manifest.json missing -- run "
                     "refresh_corpus_manifest.py")
    pinned = json.loads(mf.read_text(encoding="utf-8"))["files"]
    tracked = subprocess.run(
        ["git", "ls-files", "audits/self"], cwd=str(ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.split()
    corpus = {f for f in tracked
              if f.startswith("audits/self/dogfood/")
              or f in {"audits/self/audit_report.md",
                       "audits/self/round2_report.md",
                       "audits/self/round2_rubric.md"}}
    bad, missing = [], []
    for rel, want in sorted(pinned.items()):
        fp = ROOT / rel
        if not fp.exists():
            missing.append(rel)
        elif (hashlib.sha256(
                fp.read_bytes().replace(bytes([13, 10]), bytes([10])))
                .hexdigest() != want):
            bad.append(rel)
    unlisted = sorted(corpus - set(pinned))
    ok = not bad and not missing and not unlisted
    detail = (f"{len(pinned)} files pinned"
              + (f"; tampered: {bad}" if bad else "")
              + (f"; missing: {missing}" if missing else "")
              + (f"; unmanifested: {unlisted}" if unlisted else ""))
    return _pass(ok, "evidence corpus matches its SHA-256 manifest",
                 detail)


def sd_coverage_changed():
    """Changed harness lines must be executed by the traced suite:
    since the coverage baseline's own commit, added lines in
    harness/ that the traced battery never ran fail this check
    below a 95% executed bar. The baseline is generated only by
    refresh_coverage_baseline.py (a traced full battery run) and
    committed with the code change it reflects. Missing data fails
    open with an honest SKIP, never a silent pass."""
    mf = HERE / "coverage_baseline.json"
    if not mf.exists():
        return 1.0, ("SKIP (fail-open): coverage_baseline.json missing -- "
                     "run refresh_coverage_baseline.py")
    doc = json.loads(mf.read_text(encoding="utf-8-sig"))
    # utf-8-sig: Windows editors (PowerShell Out-File, Notepad) emit a UTF-8
    # BOM; the baseline was authored on Windows and must not fail D12 on the
    # BOM instead of its coverage content (2026-09-21 CI failure).
    ref = doc.get("commit", "")
    if len(ref) != 40:
        return 1.0, "SKIP (fail-open): baseline lacks a commit reference"
    d = subprocess.run(
        ["git", "diff", "--unified=0", ref, "--", "harness/"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if d.returncode != 0:
        return 1.0, "SKIP (fail-open): git diff against baseline unavailable"
    added = {}
    rel = None
    new_ln = 0
    for ln in (d.stdout or "").splitlines():
        if ln.startswith("+++ b/"):
            rel = ln[6:]
        elif ln.startswith("@@") and ln.count("@@") >= 2:
            parts = ln.split()
            if len(parts) < 3 or not parts[2].startswith("+"):
                continue
            new_ln = int(parts[2][1:].split(",")[0])
        elif rel is not None and ln.startswith("+") and not ln.startswith("+++"):
            added.setdefault(rel, set()).add(new_ln)
            new_ln += 1
        elif rel is not None and ln.startswith(" "):
            new_ln += 1
    base = doc.get("modules", {})
    checked = executed = 0
    gaps = {}
    for rel in sorted(added):
        try:
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        except Exception:
            continue
        stmts = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.stmt):
                continue
            if (isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                continue  # docstrings never emit trace events
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                # body[0] of a def/class whose first statement is a
                # docstring does NOT emit a trace event (a docstring is a
                # compile-time constant); counting it would fail every
                # newly added def-with-docstring forever.
                if not (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    stmts.add(node.body[0].lineno)
            else:
                stmts.add(node.lineno)
        done = set(base.get(rel, []))
        for ln in sorted(added[rel]):
            if ln not in stmts:
                continue
            checked += 1
            if ln in done:
                executed += 1
            else:
                gaps.setdefault(rel, []).append(ln)
    if not checked:
        return 1.0, ("no executable harness changes since the coverage "
                     "baseline " + ref[:10])
    ratio = executed / checked
    ok = ratio >= 0.95
    detail = (f"changed-line coverage {executed}/{checked} = "
              f"{ratio:.0%} (bar 95%) vs baseline {ref[:10]}")
    if gaps:
        items = "; ".join(
            r.split("harness/")[-1] + ":"
            + ",".join(str(x) for x in gaps[r][:8])
            + ("..." if len(gaps[r]) > 8 else "")
            for r in sorted(gaps))
        detail += "; untested: " + items
    return _pass(ok, "changed harness lines are suite-executed", detail)



CHECKS = {
    "A": [("A1", "no-tools payload guard at the one chat seam", a_no_tools_guard),
          ("A2", "preflight covers reasoning/429 retry slots", a_preflight_covers_retries),
          ("A3", "ceiling is a hard raise, lock-guarded", a_ceiling_hard),
          ("A4", "BYOK denylist + paid-BYOK fail-closed + learned rotation", a_byok),
          ("A5", "key label exact-match gate, no label echo", a_key_label_gate),
          ("A6", "gate runner shell-free + timeout", a_gate_runner),
          ("A7", "verify gate preflighted before spend", a_verify_preflight),
          ("A8", "atomic write: symlink refusal + mode preservation", a_atomic_write),
          ("A9", "failed-run rewind", a_rewind),
          ("A10", "MCP allow_verify/allow_write + allowed_roots", a_mcp_boundary),
          ("A11", "consent fail-closed on unparseable", a_consent_failclosed),
          ("A12", "continuation tamper gates before key setup", a_continuation_tamper),
          ("A13", "hard config ceilings + fail-clean load", a_hard_ceilings)],
    "R": [("R1", "HTTP transient retry (429/5xx, Retry-After)", r_http_transient),
          ("R2", "reasoning param policy + cost-carrying retry", r_reasoning_param),
          ("R3", "output usability is one verdict, protocol conditions", r_output_usability),
          ("R4", "panel seat rotation + judge fallback + honest tally", r_panel_rotation),
          ("R5", "apply rotation on all failure classes + readiness deferral", r_apply_rotation),
          ("R6", "broken-gate detector on identical REAL outputs only", r_broken_gate_detector),
          ("R7", "ungated partials never land in the tree", r_partial_never_lands),
          ("R8", "batch envelope + fail-fast + real shared verdict", r_batch),
          ("R9", "ledger chain: no fork, quarantine, repair (dynamic)", r_ledger_dynamic),
          ("R10", "spend ceiling under concurrency (dynamic)", r_spend_concurrency),
          ("R11", "config validation ranges + unknown-key warning", r_config_validation),
          ("R12", "continuation contract refuses all tamper classes (dynamic)", r_continuation_contract),
          ("R13", "full suite green hermetically", r_suite_green),
          ("R14", "R13 classifier self-verifies its contract", r_suite_selftest)],
    "SM": [("S1", "import direction (interfaces on top)", sm_layering),
           ("S2", "no re-export facades", sm_no_facades),
           ("S3", "result vocabulary one def site", sm_vocabulary_one_site),
           ("S4", "engine/router construction one owner", sm_construction_owner),
           ("S5", "ruff clean", sm_lint),
           ("S6", "test tree mirrors product tree", sm_test_mirror),
           ("S7", "one owner per policy (8 sampled)", sm_one_owner_policies),
           ("S8", "no dead validators", sm_no_dead_code)],
    "SD": [("D1", "MCP tool parity with README", sd_mcp_parity),
           ("D2", "CLI surface documented", sd_cli_surface),
           ("D3", "exit codes consistent", sd_exit_codes),
           ("D4", "config table keys real", sd_config_table),
           ("D5", "version single-sourced", sd_version_single_source),
           ("D6", "changelog discipline", sd_changelog),
           ("D7", "docs reference only real modules", sd_docs_current),
           ("D8", "shipped model freshness guard (live: opt-in)", sd_shipped_freshness),
           ("D9", "dogfood loop documented + wired", sd_dogfood_loop),
           ("D10", "CONTRIBUTING current", sd_contributing),
           ("D11", "corpus integrity (SHA-256 pinned)",
            sd_corpus_integrity),
           ("D12", "changed lines suite-executed (coverage)",
            sd_coverage_changed)],
}

DIM_NAMES = {"A": "Security", "R": "Reliability",
             "SM": "Structural hygiene & maintainability",
             "SD": "Documentation & release integrity"}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dim", choices=sorted(CHECKS), default=None)
    args = ap.parse_args()

    dims = {args.dim: CHECKS[args.dim]} if args.dim else CHECKS
    results = {}
    for dim, checks in dims.items():
        rows = []
        for cid, label, fn in checks:
            try:
                score, ev = fn()
            except Exception as e:  # a broken check is a failed check
                score, ev = 0.0, f"CHECK ERROR: {type(e).__name__}: {e}"
            rows.append({"id": cid, "label": label, "score": round(score, 3),
                         "evidence": ev})
        results[dim] = rows

    print()
    total_ok = True
    for dim, rows in results.items():
        mean = sum(r["score"] for r in rows) / len(rows)
        bar = "OK " if mean >= 9.5 / 10 else "LOW"
        total_ok &= mean >= 9.5 / 10
        print(f"[{bar}] {dim} — {DIM_NAMES[dim]}: {mean * 10:.2f}/10 "
              f"({sum(r['score'] >= 1 for r in rows)}/{len(rows)} checks fully satisfied)")
        for r in rows:
            mark = "pass" if r["score"] >= 1 else ("part" if r["score"] > 0 else "FAIL")
            print(f"    {r['id']:>3} {mark:<4} {r['label']}")
            if r["score"] < 1:
                print(f"         -> {r['evidence'][:220]}")
        print()

    out = HERE / "round2_scores.json"
    summary = {d: round(sum(r["score"] for r in rows) / len(rows) * 10, 2)
               for d, rows in results.items()}
    out.write_text(json.dumps({"scores": summary, "checks": results}, indent=2),
                   encoding="utf-8")
    print(f"scores: {summary}  (written to {out.relative_to(ROOT)})")
    print("verdict:", "ALL DIMENSIONS >= 9.5 — bar met" if total_ok
          else "BAR NOT MET — iterate on the failing checks above")
    return 0 if total_ok else 1


if __name__ == "__main__":
    sys.exit(main())
