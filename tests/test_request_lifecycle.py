"""Request-lifecycle incident regressions (issues #200-#203, #207 slice).

Hermetic: the site probe's network seams are injected fakes, so no test
touches the network and no test spends model budget. A site availability
check must classify as ``simple-action``, run exactly one bounded probe
with zero model/decomposition calls, and answer honestly from observed
probe evidence.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness.agent import (
    AutonomousAgent,
    classify_prompt_intent,
    is_site_check_prompt,
)
from harness.errors import HarnessError, ToolCancelled
from harness.web import extract_site_target, probe_public_site


def _lane_settings(**overrides):
    from types import SimpleNamespace
    base = dict(
        jev_api_key=None,
        jev_disabled=True,
        allow_escalation=False,
        max_cost=0.05,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _resolve_public(*ips):
    def _resolve(host, port):
        return [(2, 1, 6, "", (ip, port)) for ip in ips]
    return _resolve


class SiteCheckClassifierTests(unittest.TestCase):
    def test_incident_prompt_is_simple_action(self):
        self.assertTrue(is_site_check_prompt(
            "can you check freeoffgridcalculator.com and see if it's up right now?"))
        self.assertEqual(classify_prompt_intent(
            "can you check freeoffgridcalculator.com and see if it's up right now?"),
            "simple-action")

    def test_neighboring_phrasings(self):
        neighbors = [
            "is freeoffgridcalculator.com up?",
            "check if example.com loads",
            "verify example.com resolves",
            "see if example.com is down",
            "confirm example.com is reachable",
            "is example.com down right now?",
            "check https://example.com and respond yes/no",
        ]
        for prompt in neighbors:
            with self.subTest(prompt=prompt):
                self.assertTrue(is_site_check_prompt(prompt), prompt)
                self.assertEqual(classify_prompt_intent(prompt), "simple-action")

    def test_genuine_questions_stay_conversational(self):
        for prompt in [
            "what is the hourglass pattern?",
            "explain how Jev confidence works",
            "who wrote the router?",
        ]:
            with self.subTest(prompt=prompt):
                self.assertFalse(is_site_check_prompt(prompt), prompt)
                self.assertEqual(classify_prompt_intent(prompt), "conversation")

    def test_repo_directives_are_not_site_checks(self):
        self.assertFalse(is_site_check_prompt("check if it works"))
        self.assertFalse(is_site_check_prompt("verify the plan"))
        self.assertFalse(is_site_check_prompt("check harness/cli.py for bugs"))
        self.assertEqual(classify_prompt_intent("refactor harness/calc.py"), "edit")

    def test_extract_site_target_prefers_url_then_bare_host(self):
        self.assertEqual(
            extract_site_target("check https://example.com/page now"),
            "https://example.com/page")
        self.assertEqual(
            extract_site_target("is freeoffgridcalculator.com up?"),
            "https://freeoffgridcalculator.com")
        self.assertIsNone(extract_site_target("what is up today?"))


class PublicSiteProbeTests(unittest.TestCase):
    def test_https_200_is_up(self):
        out = probe_public_site(
            "https://example.com",
            resolve_fn=_resolve_public("93.184.216.34"),
            connect_fn=lambda host, path, timeout: (200, "", host))
        self.assertEqual(out["verdict"], "up")
        self.assertTrue(out["ok"])
        self.assertEqual(out["http_status"], 200)

    def test_403_is_reachable_but_denied(self):
        out = probe_public_site(
            "https://example.com",
            resolve_fn=_resolve_public("93.184.216.34"),
            connect_fn=lambda host, path, timeout: (403, "", host))
        self.assertEqual(out["verdict"], "denied")
        self.assertTrue(out["ok"])
        self.assertIn("reachable", out["reason"])

    def test_redirect_reports_without_claiming_destination(self):
        out = probe_public_site(
            "https://example.com",
            resolve_fn=_resolve_public("93.184.216.34"),
            connect_fn=lambda host, path, timeout: (301, "https://other.example/", host))
        self.assertEqual(out["verdict"], "redirect")
        self.assertIn("not followed", out["reason"])

    def test_500_is_not_up(self):
        out = probe_public_site(
            "https://example.com",
            resolve_fn=_resolve_public("93.184.216.34"),
            connect_fn=lambda host, path, timeout: (500, "", host))
        self.assertFalse(out["ok"])
        self.assertEqual(out["http_status"], 500)

    def test_private_address_is_refused(self):
        with self.assertRaises(HarnessError) as ctx:
            probe_public_site(
                "https://internal.example",
                resolve_fn=_resolve_public("10.0.0.5"),
                connect_fn=lambda host, path, timeout: (200, "", host))
        self.assertIn("non-public", str(ctx.exception))

    def test_loopback_is_refused(self):
        with self.assertRaises(HarnessError):
            probe_public_site(
                "https://example.com",
                resolve_fn=_resolve_public("127.0.0.1"),
                connect_fn=lambda host, path, timeout: (200, "", host))

    def test_credentials_scheme_and_port_are_refused(self):
        with self.assertRaises(HarnessError):
            probe_public_site("https://user:pass@example.com",
                              resolve_fn=_resolve_public("93.184.216.34"))
        with self.assertRaises(HarnessError):
            probe_public_site("http://example.com",
                              resolve_fn=_resolve_public("93.184.216.34"))
        with self.assertRaises(HarnessError):
            probe_public_site("https://example.com:8443/",
                              resolve_fn=_resolve_public("93.184.216.34"))

    def test_cancel_raises_before_network(self):
        with self.assertRaises(ToolCancelled):
            probe_public_site(
                "https://example.com", cancel_check=lambda: True,
                resolve_fn=_resolve_public("93.184.216.34"))

    def test_dns_failure_is_a_network_verdict_not_a_raise(self):
        def _boom(host, port):
            raise OSError("no such host")
        with self.assertRaises(HarnessError):
            probe_public_site("https://example.com", resolve_fn=_boom)


class SimpleActionLaneTests(unittest.TestCase):
    def _agent(self, tmp):
        return AutonomousAgent(settings=_lane_settings(),
                               history_dir=Path(tmp), root_dir=Path(tmp))

    def test_success_answers_yes_with_zero_model_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            with patch("harness.web.probe_public_site",
                       return_value={"ok": True, "http_status": 200,
                                     "verdict": "up",
                                     "reason": "'example.com' is up (HTTPS 200)",
                                     "latency_s": 0.4, "host": "example.com",
                                     "url": "https://example.com"}) as probe, \
                 patch("harness.agent.chat",
                       side_effect=AssertionError("no model calls")), \
                 patch.object(AutonomousAgent, "_handle_edit",
                              side_effect=AssertionError("no plan lane")), \
                 patch("harness.agent.ledger_for",
                       return_value=MagicMock()):
                res = agent.run_prompt(
                    "check if example.com is up - respond yes/no",
                    session_id="s1", force_conversation=True)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["intent"], "simple-action")
        self.assertIn("Yes", res["response"])
        self.assertEqual(res["cost"], 0.0)
        self.assertEqual(probe.call_count, 1)

    def test_denied_is_still_reachable_yes(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            with patch("harness.web.probe_public_site",
                       return_value={"ok": True, "http_status": 403,
                                     "verdict": "denied",
                                     "reason": "'example.com' responds but denied page access (HTTPS 403)",
                                     "latency_s": 0.4, "host": "example.com",
                                     "url": "https://example.com"}), \
                 patch("harness.agent.ledger_for",
                       return_value=MagicMock()):
                res = agent.run_simple_action(
                    "check if example.com is up", session_id="s2",
                    probe_fn=lambda host, path, timeout: (403, "", host),
                    resolve_fn=_resolve_public("93.184.216.34"))
        self.assertEqual(res["status"], "ok")
        self.assertIn("reachable", res["response"])

    def test_timeout_answers_no(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            with patch("harness.web.probe_public_site",
                       return_value={"ok": False, "http_status": None,
                                     "verdict": "timeout",
                                     "reason": "'example.com' timed out",
                                     "latency_s": 8.0, "host": "example.com",
                                     "url": "https://example.com"}), \
                 patch("harness.agent.ledger_for",
                       return_value=MagicMock()):
                res = agent.run_simple_action("is example.com up?",
                                              session_id="s3")
        self.assertEqual(res["status"], "deferred")
        self.assertTrue(res["response"].startswith("No"))

    def test_refused_target_fails_honestly_with_zero_model_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            with patch("harness.web.probe_public_site",
                       side_effect=HarnessError(
                           "site probe refused: only https:// URLs are probed")), \
                 patch("harness.agent.chat",
                       side_effect=AssertionError("no model calls")), \
                 patch.object(AutonomousAgent, "_handle_edit",
                              side_effect=AssertionError("no plan lane")), \
                 patch("harness.agent.ledger_for",
                       return_value=MagicMock()):
                res = agent.run_simple_action("check http://example.com now",
                                              session_id="s4")
        self.assertEqual(res["status"], "deferred")
        self.assertIn("can't check", res["response"])

    def test_gui_path_selects_simple_action(self):
        # server.py routes non-edit intents with force_conversation=True.
        self.assertNotEqual(classify_prompt_intent(
            "check if example.com is up - respond yes/no"), "edit")
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            with patch("harness.web.probe_public_site",
                       return_value={"ok": True, "http_status": 200,
                                     "verdict": "up",
                                     "reason": "'example.com' is up (HTTPS 200)",
                                     "latency_s": 0.3, "host": "example.com",
                                     "url": "https://example.com"}), \
                 patch("harness.agent.chat",
                       side_effect=AssertionError("no model calls")), \
                 patch.object(AutonomousAgent, "_handle_edit",
                              side_effect=AssertionError("no plan lane")), \
                 patch("harness.agent.ledger_for",
                       return_value=MagicMock()):
                res = agent.run_prompt(
                    "check if example.com is up - respond yes/no",
                    session_id="s5", force_conversation=True)
        self.assertEqual(res["intent"], "simple-action")
        self.assertEqual(res["status"], "ok")


class EscalationGateTests(unittest.TestCase):
    def _agent(self, tmp):
        return AutonomousAgent(settings=_lane_settings(),
                               history_dir=Path(tmp), root_dir=Path(tmp))

    def test_auto_apply_alone_never_authorizes_escalation(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            # plan_required signal, default auto_apply context, paid key NOT
            # armed, no explicit execution phrase in the prompt.
            self.assertFalse(agent._escalation_authorized(
                "check if example.com is up", False, "plan_required"))

    def test_explicit_directive_or_armed_key_authorizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            self.assertTrue(agent._escalation_authorized(
                "execute the plan now", False, "needs_iteration"))
            with patch.object(AutonomousAgent, "_auto_escalation_armed",
                              return_value=True):
                self.assertTrue(agent._escalation_authorized(
                    "something deferred", True, "needs_iteration"))

    def test_quiet_prompt_with_ok_status_never_escalates(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            self.assertFalse(agent._escalation_authorized(
                "what is the capital of France?", False, "ok"))


if __name__ == "__main__":
    unittest.main()
