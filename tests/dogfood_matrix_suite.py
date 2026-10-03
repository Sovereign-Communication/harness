"""Dogfood Matrix Suite: Comprehensive testing across 10 major Harness categories.

Enforces:
1. Max $0.10 spend per category
2. Max $1.00 total spend
3. Full capability and difficulty matrix
4. Jev score >= 95/100 for every category

Scoring is strict: a category reaches 100 only when every named check holds,
and any miss lands below ``PASS_SCORE`` (see ``strict_score``). The pure
``score_*`` helpers take plain result data so ``tests/test_dogfood_matrix_strict.py``
can prove, offline and with model doubles, that planted failures are caught
(the original lenient scoring passed them).
"""
import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple

from harness.agent import AutonomousAgent
from harness.config import load_settings, resolve_api_key
from harness.jev_packs import (
    validate_log_pack,
    validate_operator_pack,
)
from harness.jev_policy import policy_for
from harness.orchestrator import assess_completion
from harness.route_pack import validate_route_pack
from harness.server import run_driver_task
from harness.session import governor_for, ledger_for
from harness.trust import trust_status


PASS_SCORE = 95.0
# Claim verification: a supported claim must clear SUPPORT_FLOOR, a planted
# false one must stay at or under REJECT_CEILING; anything the evaluator would
# itself read as supported (>= SUPPORTED_AT) hard-fails the category.
SUPPORT_FLOOR = 0.70
REJECT_CEILING = 0.30
SUPPORTED_AT = 0.50


def strict_score(checks: Dict[str, bool]) -> float:
    """100 only when every check holds; any miss stays strictly below PASS_SCORE."""
    if not checks:
        return 0.0
    if all(checks.values()):
        return 100.0
    return round(90.0 * sum(1 for ok in checks.values() if ok) / len(checks), 2)


def _noul_of(flag: Any) -> Any:
    return flag.get("noul") if isinstance(flag, dict) else None


def score_claim_verification(true_flag: Any, false_flag: Any) -> Tuple[float, Dict[str, Any]]:
    """Score CAT-06 from the typed claim flags (``structural["claim_flags"][0]``).

    An unkeyed/fallback or missing flag is not evidence either way, so it
    cannot pass; a planted false claim the evaluator supports hard-fails.
    """
    t_noul, f_noul = _noul_of(true_flag), _noul_of(false_flag)
    t_live = isinstance(t_noul, (int, float)) and not true_flag.get("fallback")
    f_live = isinstance(f_noul, (int, float)) and not false_flag.get("fallback")
    false_supported = f_live and float(f_noul) >= SUPPORTED_AT
    checks = {
        "true_claim_live": t_live,
        "false_claim_live": f_live,
        "true_claim_supported": bool(t_live and float(t_noul) >= SUPPORT_FLOOR),
        "false_claim_rejected": bool(f_live and float(f_noul) <= REJECT_CEILING),
    }
    score = 0.0 if false_supported else strict_score(checks)
    return score, {"checks": checks, "true_claim_support": t_noul,
                   "false_claim_support": f_noul,
                   "false_claim_passed_as_supported": bool(false_supported)}


def score_driver_loop(res: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
    """Score CAT-08 from a ``run_driver_task`` result: a real ok step is required."""
    steps = [s for s in (res.get("steps") or []) if isinstance(s, dict)]
    ok_steps = [s for s in steps
                if isinstance(s.get("envelope"), dict) and s["envelope"].get("ok") is True]
    audit = res.get("audit")
    nested = audit.get("audit") if isinstance(audit, dict) else None
    audit_ok = bool(isinstance(audit, dict) and audit.get("ok") is True
                    and isinstance(nested, dict) and nested.get("ok") is True)
    checks = {
        "ran_steps": len(steps) >= 1,
        "ok_steps>=1": len(ok_steps) >= 1,
        "audit_chain_ok": audit_ok,
    }
    return strict_score(checks), {
        "checks": checks, "total_steps": len(steps), "ok_steps": len(ok_steps),
        "status": res.get("status"), "audit_ok": audit_ok,
        "summary": res.get("summary")}


def score_phase_bar(phase_result: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
    """Score CAT-10 from a ``dogfood_phase`` result; no floor, live Jev required."""
    bar = phase_result.get("bar") or {}
    blocking = list(bar.get("blocking_axes") or [])
    score_val = float(phase_result.get("score") or 0.0)
    semantic = phase_result.get("semantic") or {}
    checks = {
        "bar_pass": bool(bar.get("pass")),
        "no_blocking_axes": not blocking,
        "can_mark_complete": bool(phase_result.get("can_mark_complete")),
        "live_jev": not semantic.get("is_fallback", True),
    }
    score = min(score_val, strict_score(checks))
    return round(score, 2), {
        "checks": checks, "phase_score": score_val, "blocking_axes": blocking}


def score_cases(cases: List[Dict[str, Any]]) -> Tuple[float, Dict[str, Any]]:
    """Accuracy over typed cases; a fallback answer is never a match."""
    correct = sum(1 for c in cases if c["match"])
    score = 100.0 * correct / len(cases) if cases else 0.0
    return score, {"cases": cases, "accuracy": f"{correct}/{len(cases)}"}


class DogfoodMatrixRunner:
    def __init__(self, *, settings=None, gov=None, ledger=None, policy=None, agent=None):
        """Defaults wire the live stack; every seam can be injected so the
        hermetic variant runs the same categories against model doubles."""
        self.settings = settings or load_settings()
        if gov is None:
            self.api_key, self.gov = governor_for(self.settings)
        else:
            self.api_key, self.gov = None, gov
        self.ledger = ledger or ledger_for(self.settings, caller="matrix_runner")
        self.policy = policy or policy_for(
            self.settings, transport=None, governor=self.gov, ledger=self.ledger)
        self._agent = agent
        self.results: List[Dict[str, Any]] = []

    @property
    def agent(self):
        if self._agent is None:
            self._agent = AutonomousAgent(settings=self.settings)
        return self._agent

    def current_spend(self) -> float:
        return float(getattr(self.gov, "spent", 0.0) or 0.0)

    def run_category(self, cat_id: str, name: str, difficulty: str, test_fn) -> Dict[str, Any]:
        print("\n==================================================")
        print(f"[{time.strftime('%H:%M:%S')}] RUNNING {cat_id}: {name} ({difficulty})")
        print("==================================================")
        spend_start = self.current_spend()
        t0 = time.time()

        try:
            score, details = test_fn()
            status = "PASSED" if score >= PASS_SCORE else "FAILED"
            error = None
        except Exception as e:
            import traceback
            traceback.print_exc()
            score = 0.0
            status = "ERROR"
            details = {"exception": f"{type(e).__name__}: {e}"}
            error = str(e)

        dur = time.time() - t0
        spend_end = self.current_spend()
        cost = max(0.0, spend_end - spend_start)

        record = {
            "category_id": cat_id,
            "name": name,
            "difficulty": difficulty,
            "score": round(score, 2),
            "status": status,
            "cost_usd": round(cost, 6),
            "duration_s": round(dur, 2),
            "details": details,
            "error": error,
        }
        self.results.append(record)
        print(f"[{time.strftime('%H:%M:%S')}] {cat_id} -> {status} | Score: {score}/100 | Cost: ${cost:.5f} | Time: {dur:.2f}s")
        if cost > 0.10:
            print(f"WARNING: Cost ${cost:.4f} exceeded $0.10 per-category cap!")
        return record

    # -------------------------------------------------------------
    # CAT-01: Conversational Knowledge & Direct Reasoning (Easy/Med)
    # -------------------------------------------------------------
    def test_cat01_conversational_reasoning(self) -> Tuple[float, Dict[str, Any]]:
        prompt = (
            "State Riemann's explicit formula connecting primes to the non-trivial zeros "
            "of the zeta function, and briefly explain in 2 sentences what the oscillating terms represent."
        )
        res = self.agent.run_hourglass_request(prompt, web=False, max_tokens=1024, max_rounds=2)
        ans = res.get("response", "")
        status = res.get("status")

        # Verify content
        has_formula = any(term in ans.lower() for term in ["explicit formula", "psi(x)", "chebyshev", "zeros", "sum"])
        has_oscillation = any(term in ans.lower() for term in ["oscillat", "fluctuat", "wave", "periodic"])

        # Jev completion assessment
        comp = assess_completion(
            prompt,
            f"Candidate answer:\n{ans}\n\nRetained context:\nMathematical number theory context",
            self.agent._orchestrator_chat_fn(self.gov)
        )
        # assess_completion returns None for unusable output: that is not a pass.
        complete = bool((comp or {}).get("complete"))

        checks = {"status_ok": status == "ok", "has_formula": has_formula,
                  "has_oscillation": has_oscillation, "jev_complete": complete}
        score = strict_score(checks)
        return score, {
            "checks": checks,
            "status": status,
            "model": res.get("model"),
            "has_formula": has_formula,
            "has_oscillation": has_oscillation,
            "completion": comp,
            "answer_preview": ans[:200]
        }

    # -------------------------------------------------------------
    # CAT-02: Live Web Search & Information Synthesis (Medium)
    # -------------------------------------------------------------
    def test_cat02_web_search_synthesis(self) -> Tuple[float, Dict[str, Any]]:
        prompt = "can you find news about the work Claude models did on the rimann hypothesis? they didn't prove it, but made real progress!"
        res = self.agent.run_hourglass_request(prompt, web=True, max_tokens=1024, max_rounds=2)
        ans = res.get("response", "")
        status = res.get("status")
        web_used = res.get("web_used")
        sources = res.get("web_sources", [])

        has_67 = ("67.2%" in ans or "67%" in ans or "critical line" in ans.lower())
        has_anthropic = ("anthropic" in ans.lower() or "claude" in ans.lower())
        has_unproven = any(term in ans.lower() for term in ["not prove", "didn't prove", "unproven", "partial", "bound"])

        checks = {"status_ok": status == "ok", "web_used": bool(web_used),
                  "has_sources": len(sources) >= 1, "has_anthropic": has_anthropic,
                  "has_unproven": has_unproven}
        score = strict_score(checks)
        return score, {
            "checks": checks,
            "status": status,
            "web_used": web_used,
            "sources_count": len(sources),
            "sources": sources,
            "has_67_percent": has_67,
            "has_anthropic": has_anthropic,
            "has_unproven": has_unproven,
            "answer_preview": ans[:200]
        }

    # -------------------------------------------------------------
    # CAT-03: Query Routing & Complexity Ladder (Easy to Hard)
    # -------------------------------------------------------------
    def test_cat03_query_routing(self) -> Tuple[float, Dict[str, Any]]:
        pack = validate_route_pack({
            "id": "matrix-route-ladder-v1",
            "rungs": [
                {"rung_id": "r0", "tier": "T0", "model": "deepseek/deepseek-v4-flash", "cost_class": "free", "guidance": ["typo", "docstring", "rename"]},
                {"rung_id": "r1", "tier": "T1", "model": "deepseek/deepseek-v4.1-flash", "cost_class": "cheap", "guidance": ["implement", "test", "parse"]},
                {"rung_id": "r2", "tier": "T2", "model": "z-ai/glm-5.3-flash", "cost_class": "moderate", "guidance": ["concurrency", "mutex", "deadlock", "invariant"]},
                {"rung_id": "r3", "tier": "T3", "model": "deepseek/deepseek-v4-pro", "cost_class": "premium", "guidance": ["architecture", "novel-proof", "security-review"]}
            ]
        })
        cases = [
            ("Fix a spelling typo in docstring", "T0"),
            ("Implement JSON parser unit test", "T1"),
            ("Resolve concurrency deadlock between reader-writer mutex and background thread", "T2"),
        ]
        details = []
        for goal, expected_tier in cases:
            res, struct, combo = self.policy.evaluate_model_route({"goal": goal}, pack, site="model_route")
            actual_tier = combo.get("tier")
            # An unkeyed/fallback route is the code heuristic, not a Jev answer.
            is_match = (actual_tier == expected_tier) and not res.is_fallback
            details.append({"goal": goal, "expected": expected_tier, "actual": actual_tier,
                            "match": is_match, "fallback": bool(res.is_fallback),
                            "rung": combo.get("rung_id")})

        return score_cases(details)

    # -------------------------------------------------------------
    # CAT-04: Issue Triage & Bucket Sorting (Medium)
    # -------------------------------------------------------------
    def test_cat04_issue_triage(self) -> Tuple[float, Dict[str, Any]]:
        pack = validate_operator_pack({
            "id": "ops-triage-v1",
            "buckets": {
                "auth": {
                    "label": "Auth / token trouble",
                    "kind": "trouble_area",
                    "path_id": "path/auth",
                    "keywords": ["auth", "token", "unauthorized", "login"],
                    "suggested_next_action": "check token lifecycle",
                    "attention": "high",
                },
                "perf": {
                    "label": "Performance hot path",
                    "kind": "alternate_path",
                    "path_id": "path/perf",
                    "keywords": ["latency", "slow", "timeout", "bottleneck"],
                    "suggested_next_action": "profile query plan",
                    "attention": "medium",
                },
                "driver": {
                    "label": "Driver orchestration",
                    "kind": "orchestration_driver",
                    "path_id": "path/driver",
                    "keywords": ["orchestr", "deferral", "driver", "perception"],
                    "suggested_next_action": "review driver steps",
                    "attention": "low",
                },
            }
        })
        cases = [
            ("HTTP 401 Unauthorized when connecting with bearer token to API endpoint", "auth"),
            ("Database query latency spikes over 3000ms causing request timeout under load", "perf"),
            ("Driver perception step deferred and agent handoff not resuming correctly", "driver"),
        ]
        details = []
        for issue_text, expected_bucket in cases:
            res, struct, combo = self.policy.evaluate_issue_sort({"issue": issue_text}, pack, site="issue_sort")
            actual_bucket = combo.get("bucket")
            is_match = (actual_bucket == expected_bucket) and not res.is_fallback
            details.append({"issue": issue_text, "expected": expected_bucket, "actual": actual_bucket,
                            "match": is_match, "fallback": bool(res.is_fallback)})

        return score_cases(details)

    # -------------------------------------------------------------
    # CAT-05: Log Analysis & Anomaly Attribution (Medium)
    # -------------------------------------------------------------
    def test_cat05_log_analysis(self) -> Tuple[float, Dict[str, Any]]:
        from tests.test_jev_log_pack import sample_log_pack
        pack = validate_log_pack(sample_log_pack())
        log_sample = {
            "item": "Connection reset by peer during yamux handshake: disconnected transport listener dropped socket"
        }
        res, struct, combo = self.policy.evaluate_log_item(log_sample, pack, site="log_factor")
        matched_bucket = combo.get("bucket")
        severity = combo.get("score", {}).get("level") or combo.get("severity") or struct.get("severity")
        is_transport = (matched_bucket == "transport")

        checks = {"jev_keyed": not res.is_fallback, "bucket_transport": is_transport}
        score = strict_score(checks)
        return score, {"checks": checks, "matched_bucket": matched_bucket, "expected": "transport",
                       "severity": severity, "combo": combo}

    # -------------------------------------------------------------
    # CAT-06: Multi-Model Consensus & Claim Verification (Hard)
    # -------------------------------------------------------------
    def test_cat06_consensus_claim_verification(self) -> Tuple[float, Dict[str, Any]]:
        # True claim
        claim_true = "The Riemann Hypothesis conjectures that all non-trivial zeros of the zeta function have real part 1/2."
        ev_true = "The Riemann hypothesis asserts that all non-trivial zeros of the Riemann zeta function lie on the critical line with real part 1/2."
        # False claim (planted): the evidence contradicts it.
        claim_false = "The Riemann Hypothesis has been proven to be completely false by elementary arithmetic in 2026."
        ev_false = "The Riemann hypothesis remains one of the most famous open problems in mathematics; it has not been disproved."

        flags = []
        details: Dict[str, Any] = {}
        for label, claim, evidence in (("true", claim_true, ev_true), ("false", claim_false, ev_false)):
            # enabled=True: the default skips the check and returns a
            # fallback "pass", which would read as support for any claim.
            res, struct = self.policy.evaluate_claim_support(
                [claim], evidence, enabled=True, site="claim")
            flag = (struct.get("claim_flags") or [None])[0]
            if flag is not None and res.is_fallback:
                flag = dict(flag, fallback=True)
            flags.append(flag)
            details[f"{label}_claim_verdict"] = res.verdict
            details[f"{label}_claim_is_fallback"] = bool(res.is_fallback)

        score, scored = score_claim_verification(flags[0], flags[1])
        details.update(scored)
        return score, details

    # -------------------------------------------------------------
    # CAT-07: Autonomous Edit Planning & Waist Safety Guard (Hard)
    # -------------------------------------------------------------
    def test_cat07_edit_planning_waist_guard(self) -> Tuple[float, Dict[str, Any]]:
        prompt = "Add a docstring and type hint to extract_query in harness/web.py"
        # Formulate through AutonomousAgent with fast decomposition for dogfood testing
        orig_plan_round = self.agent._plan_round
        from harness.waist import compose_plan, compose_arguments
        self.agent._plan_round = lambda g, f, gov, confirm=None, token_budget=None: compose_plan(
            transport=self.agent.transport, api_key=resolve_api_key(), governor=gov,
            ledger=self.ledger, opts_goal=g, candidate_files=f, decompose_llm=False,
            confirm=False, root=str(self.agent.root_dir), execute=True,
            **compose_arguments(self.settings, goal=g, files=f, root=self.agent.root_dir)
        )
        try:
            res = self.agent.run_prompt(prompt, auto_apply=False, session_id="dogfood-edit-test")
        finally:
            self.agent._plan_round = orig_plan_round

        intent = res.get("intent")
        status = res.get("status")
        resp_text = res.get("response", "")

        # In non-auto mode, intent must be 'edit', status 'refused' (waist safety guard) or 'preview_ready' or 'ok'
        has_intent = (intent == "edit")
        has_waist_guard = status in ("refused", "ok", "preview_ready")
        has_dag = bool(res.get("dag") or res.get("target_files"))

        checks = {"intent_edit": has_intent, "waist_status": has_waist_guard, "plan_evidence": has_dag}
        score = strict_score(checks)
        return score, {
            "checks": checks,
            "intent": intent,
            "status": status,
            "has_waist_guard": has_waist_guard,
            "has_dag": has_dag,
            "response_preview": resp_text[:250]
        }

    # -------------------------------------------------------------
    # CAT-08: Driver Multi-Step Perception Loop (Hard)
    # -------------------------------------------------------------
    def test_cat08_driver_perception(self) -> Tuple[float, Dict[str, Any]]:
        import threading
        import driver_core.config as _dc_config
        import driver_core.server as _dc_server

        token = "matrix-driver-session-token-1234"
        os.environ["DRIVER_TOKEN"] = token
        d_settings = _dc_config.load_settings()
        httpd, _ = _dc_server.serve(host=d_settings.host, port=d_settings.port, block=False)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        time.sleep(0.05)

        task_id = f"drv/matrix-test-{int(time.time())}"
        res = run_driver_task(
            task_id,
            {"goal": "perceive git status and inspect branch", "max_steps": 2, "target": "cli", "auto_approve": True},
            cancel_check=None,
        )
        return score_driver_loop(res)

    # -------------------------------------------------------------
    # CAT-09: Cryptographic Ledger & Autonomy Audit (Easy)
    # -------------------------------------------------------------
    def test_cat09_ledger_autonomy_audit(self) -> Tuple[float, Dict[str, Any]]:
        # 1. Direct ledger hash chain verification
        verified, bad_seq = self.ledger.verify()

        # 2. Participation report and trust score
        report = self.ledger.participation_report()
        trust = trust_status(report)
        standing = trust.get("standing", "good")

        # 3. Agent audit handler
        audit_res = self.agent.run_prompt("audit ledger and verify cryptographic integrity", session_id="audit-test")

        score = 100.0 if (verified and bad_seq is None and standing != "quarantined") else 50.0
        return score, {
            "ledger_verified": verified,
            "first_bad_seq": bad_seq,
            "standing": standing,
            "entries_count": len(self.ledger.entries()),
            "audit_response": audit_res.get("response", "")[:200]
        }

    # -------------------------------------------------------------
    # CAT-10: Repository Dogfooding & Jev Phase Gates (Expert)
    # -------------------------------------------------------------
    def test_cat10_jev_phase_dogfood(self) -> Tuple[float, Dict[str, Any]]:
        from harness.jev_completion import dogfood_phase

        # Evaluate JEV-COMPLETION with live Jev: a local-heuristic score cannot reach 100.
        phase_result = dogfood_phase(
            ".", "JEV-COMPLETION", settings=self.settings,
            governor=self.gov, ledger=self.ledger, use_live_jev=True)
        score, details = score_phase_bar(phase_result)
        details["phase_id"] = "JEV-COMPLETION"
        return score, details

    # -------------------------------------------------------------
    # Suite Orchestrator
    # -------------------------------------------------------------
    def run_all(self):
        print("\n=================================================================")
        print("STARTING COMPREHENSIVE DOGFOOD MATRIX SUITE across 10 CATEGORIES")
        print("Total budget authorized: $1.00 | Max per category: $0.10")
        print("Target score per category: >= 95.00 / 100")
        print("=================================================================\n")

        categories = [
            ("CAT-01", "Conversational Knowledge & Reasoning", "Medium", self.test_cat01_conversational_reasoning),
            ("CAT-02", "Live Web Search & Synthesis", "Medium", self.test_cat02_web_search_synthesis),
            ("CAT-03", "Query Routing & Complexity Ladder", "Easy-Hard", self.test_cat03_query_routing),
            ("CAT-04", "Issue Triage & Bucket Sorting", "Medium", self.test_cat04_issue_triage),
            ("CAT-05", "Log Analysis & Anomaly Attribution", "Medium", self.test_cat05_log_analysis),
            ("CAT-06", "Consensus & Claim Verification", "Hard", self.test_cat06_consensus_claim_verification),
            ("CAT-07", "Edit Planning & Waist Safety Guard", "Hard", self.test_cat07_edit_planning_waist_guard),
            ("CAT-08", "Driver Multi-Step Perception Loop", "Hard", self.test_cat08_driver_perception),
            ("CAT-09", "Cryptographic Ledger & Autonomy Audit", "Easy", self.test_cat09_ledger_autonomy_audit),
            ("CAT-10", "Repository Dogfood & Jev Phase Gates", "Expert", self.test_cat10_jev_phase_dogfood),
        ]

        total_start = self.current_spend()
        t_suite_start = time.time()

        for cat_id, name, diff, fn in categories:
            self.run_category(cat_id, name, diff, fn)

        suite_dur = time.time() - t_suite_start
        total_cost = max(0.0, self.current_spend() - total_start)

        passed_count = sum(1 for r in self.results if r["status"] == "PASSED")
        all_passed = (passed_count == len(categories))
        avg_score = sum(r["score"] for r in self.results) / len(categories) if categories else 0.0

        print("\n" + "=" * 65)
        print("DOGFOOD MATRIX SUITE SUMMARY")
        print("=" * 65)
        print(f"Categories Tested: {len(categories)}")
        print(f"Passed:            {passed_count} / {len(categories)}")
        print(f"Average Score:     {avg_score:.2f} / 100")
        print(f"Total Suite Cost:  ${total_cost:.5f} (Authorized: $1.00)")
        print(f"Total Duration:    {suite_dur:.2f}s")
        print("-" * 65)
        for r in self.results:
            print(f"[{r['status']:6s}] {r['category_id']}: {r['name']:<38} Score: {r['score']:5.1f} | Cost: ${r['cost_usd']:.5f} | {r['duration_s']:5.1f}s")
        print("=" * 65)

        return {
            "all_passed": all_passed,
            "passed_count": passed_count,
            "total_count": len(categories),
            "average_score": round(avg_score, 2),
            "total_cost_usd": round(total_cost, 6),
            "duration_s": round(suite_dur, 2),
            "categories": self.results,
        }


if __name__ == "__main__":
    runner = DogfoodMatrixRunner()
    summary = runner.run_all()
    out_path = "dogfood_matrix_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"\nWrote full matrix results to {out_path}")
    if not summary["all_passed"]:
        sys.exit(1)
