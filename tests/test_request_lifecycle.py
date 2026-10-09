"""Request-lifecycle incident regressions (issues #200-#203, #207 slice).

Hermetic: the site probe's network seams are injected fakes (or patched
stdlib socket/ssl objects that never connect), so no test touches the
network and no test spends model budget. A site availability check must
classify as ``simple-action``, run exactly one bounded probe with zero
model/decomposition calls, and answer honestly from observed probe
evidence.
"""
import socket
import tempfile
import time
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


class SiteCheckPromptEdgeTests(unittest.TestCase):
    def test_empty_prompt_is_not_a_site_check(self):
        self.assertFalse(is_site_check_prompt(""))
        self.assertFalse(is_site_check_prompt("   "))

    def test_plan_execution_phrase_with_a_host_stays_out(self):
        self.assertFalse(is_site_check_prompt("execute the plan for example.com"))
        self.assertEqual(classify_prompt_intent("execute the plan for example.com"),
                         "edit")

    def test_action_verb_without_a_target_is_not_a_site_check(self):
        self.assertFalse(is_site_check_prompt("check the weekly report"))

    def test_mutation_verb_with_a_host_is_not_a_site_check(self):
        self.assertFalse(is_site_check_prompt("fix example.com"))

    def test_bare_uptime_phrasing_without_a_verb_is_a_site_check(self):
        self.assertTrue(is_site_check_prompt("example.com reachable?"))
        self.assertEqual(classify_prompt_intent("example.com reachable?"),
                         "simple-action")

    def test_overlong_host_is_not_a_target(self):
        host = "{0}.{0}.{0}.{0}.abcdef.com".format("a" * 61)
        self.assertGreater(len(host), 253)
        self.assertIsNone(extract_site_target(f"is {host} up?"))


class NonForceSimpleActionTests(unittest.TestCase):
    def test_default_run_prompt_path_selects_simple_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_lane_settings(),
                                   history_dir=Path(tmp), root_dir=Path(tmp))
            with patch("harness.web.probe_public_site",
                       return_value={"ok": True, "http_status": 200,
                                     "verdict": "up",
                                     "reason": "'example.com' is up (HTTPS 200)",
                                     "latency_s": 0.2, "host": "example.com",
                                     "url": "https://example.com"}), \
                 patch("harness.agent.chat",
                       side_effect=AssertionError("no model calls")), \
                 patch.object(AutonomousAgent, "_handle_edit",
                              side_effect=AssertionError("no plan lane")), \
                 patch("harness.agent.ledger_for",
                       return_value=MagicMock()):
                res = agent.run_prompt("check if example.com is up",
                                       session_id="nf1")
        self.assertEqual(res["intent"], "simple-action")
        self.assertEqual(res["status"], "ok")


class ProbeRefusalEdgeTests(unittest.TestCase):
    def test_empty_url_is_refused(self):
        with self.assertRaisesRegex(HarnessError, "empty url"):
            probe_public_site("   ", resolve_fn=_resolve_public("93.184.216.34"))

    def test_url_without_a_host_is_refused(self):
        with self.assertRaisesRegex(HarnessError, "no host"):
            probe_public_site("https:///path",
                              resolve_fn=_resolve_public("93.184.216.34"))

    def test_empty_resolution_is_refused(self):
        with self.assertRaisesRegex(HarnessError, "no addresses"):
            probe_public_site("https://example.com", resolve_fn=lambda h, p: [])

    def test_unparseable_address_is_refused(self):
        def _weird(host, port):
            return [(2, 1, 6, "", (":::", port))]
        with self.assertRaisesRegex(HarnessError, "unparseable address"):
            probe_public_site("https://example.com", resolve_fn=_weird)

    def test_query_string_reaches_the_wire_path(self):
        seen = {}

        def _capture(host, path, timeout):
            seen["path"] = path
            return (200, "", host)

        out = probe_public_site("https://example.com/s?q=1",
                                resolve_fn=_resolve_public("93.184.216.34"),
                                connect_fn=_capture)
        self.assertEqual(seen["path"], "/s?q=1")
        self.assertEqual(out["verdict"], "up")

    def test_cancel_after_resolve_aborts(self):
        with self.assertRaises(ToolCancelled):
            probe_public_site("https://example.com",
                              cancel_check=iter([False, True]).__next__,
                              resolve_fn=_resolve_public("93.184.216.34"))

    def test_unusable_status_is_a_network_verdict(self):
        out = probe_public_site(
            "https://example.com",
            resolve_fn=_resolve_public("93.184.216.34"),
            connect_fn=lambda host, path, timeout: (123, "", host))
        self.assertFalse(out["ok"])
        self.assertEqual(out["verdict"], "network")
        self.assertEqual(out["http_status"], 123)


class _ScriptedSocket:
    """Hermetic stand-in for a connected socket: scripted recvs, recorded close."""

    def __init__(self, recvs):
        self._recvs = list(recvs)
        self.sent = []
        self.closed = False

    def settimeout(self, timeout):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, n):
        if not self._recvs:
            return b""
        item = self._recvs.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True


class ProbeRealPathTests(unittest.TestCase):
    """The real socket/SSL read loop with patched stdlib seams (no network)."""

    def _run(self, recvs, create_side_effect=None):
        raw, tls = _ScriptedSocket([]), _ScriptedSocket(recvs)
        ctx = MagicMock()
        ctx.wrap_socket.return_value = tls
        with patch("socket.create_connection", return_value=raw) as conn, \
             patch("ssl.create_default_context", return_value=ctx):
            if create_side_effect is not None:
                conn.side_effect = create_side_effect
            out = probe_public_site(
                "https://example.com/s?q=1",
                resolve_fn=_resolve_public("93.184.216.34"))
        return out, raw, tls, ctx

    def test_split_response_reads_to_headers_and_reports_up(self):
        out, raw, tls, ctx = self._run([
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html",
            b"\r\n\r\nhello",
        ])
        self.assertEqual(out["verdict"], "up")
        self.assertEqual(out["http_status"], 200)
        request = b"".join(tls.sent)
        self.assertIn(b"Host: example.com", request)
        self.assertIn(b"GET /s?q=1 ", request)
        self.assertTrue(raw.closed and tls.closed)
        ctx.wrap_socket.assert_called_once()

    def test_redirect_location_is_reported_not_followed(self):
        out, _, _, _ = self._run([
            b"HTTP/1.1 301 Moved\r\nLocation: https://other.example/\r\n\r\n",
        ])
        self.assertEqual(out["verdict"], "redirect")
        self.assertIn("other.example", out["reason"])
        self.assertIn("not followed", out["reason"])

    def test_recv_timeout_is_a_timeout_verdict(self):
        out, _, _, _ = self._run([socket.timeout("timed out")])
        self.assertEqual(out["verdict"], "timeout")
        self.assertIsNone(out["http_status"])

    def test_empty_body_is_an_unusable_network_verdict(self):
        out, _, _, _ = self._run([])
        self.assertFalse(out["ok"])
        self.assertEqual(out["verdict"], "network")
        self.assertIsNone(out["http_status"])

    def test_malformed_status_is_an_unusable_network_verdict(self):
        out, _, _, _ = self._run([b"HTTP/1.1 XX\r\n\r\n"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["verdict"], "network")

    def test_policy_error_before_dispatch_is_reraised(self):
        with self.assertRaises(HarnessError):
            self._run([], create_side_effect=HarnessError("nope"))

    def test_connect_timeout_maps_to_timeout(self):
        out, _, _, _ = self._run([], create_side_effect=OSError("timed out"))
        self.assertEqual(out["verdict"], "timeout")

    def test_connect_refusal_maps_to_network(self):
        out, _, _, _ = self._run([],
                                 create_side_effect=OSError("refused"))
        self.assertEqual(out["verdict"], "network")
        self.assertIn("unreachable", out["reason"])

    def test_closing_sockets_never_fails_the_probe(self):
        class _BadClose(_ScriptedSocket):
            def close(self):
                raise OSError("already closed")

        raw, tls = _BadClose([]), _BadClose([
            b"HTTP/1.1 200 OK\r\n\r\n",
        ])
        ctx = MagicMock()
        ctx.wrap_socket.return_value = tls
        with patch("socket.create_connection", return_value=raw), \
             patch("ssl.create_default_context", return_value=ctx):
            out = probe_public_site(
                "https://example.com",
                resolve_fn=_resolve_public("93.184.216.34"))
        self.assertEqual(out["verdict"], "up")


class IncidentReplayBudgetTests(unittest.TestCase):
    """#207 exit proof: the incident prompt replays within every budget.

    Hermetic tripwires fail the test on any OpenRouter, Jev-policy,
    spend-governor, or plan-lane call. Measured on the committed tree:
    1 probe, 0 model/Jev calls, millisecond wall time, honest answer.
    Both freeform surfaces (default ``run_prompt`` and the GUI
    ``force_conversation`` path from server.py) must return the same
evidence-based result.
    """

    def _replay(self, **run_kwargs):
        counts = {"probe": 0}

        def fake_probe(*args, **kwargs):
            counts["probe"] += 1
            return {"ok": True, "http_status": 200, "verdict": "up",
                    "reason": "'example.com' is up (HTTPS 200)",
                    "latency_s": 0.2, "host": "example.com",
                    "url": "https://example.com"}

        with tempfile.TemporaryDirectory() as tmp:
            agent = AutonomousAgent(settings=_lane_settings(),
                                   history_dir=Path(tmp), root_dir=Path(tmp))
            with patch("harness.web.probe_public_site",
                       side_effect=fake_probe), \
                 patch("harness.agent.chat",
                       side_effect=AssertionError("no OpenRouter calls")), \
                 patch("harness.agent.policy_for",
                       side_effect=AssertionError("no Jev calls")), \
                 patch("harness.agent.governor_for",
                       side_effect=AssertionError("no spend governance")), \
                 patch.object(AutonomousAgent, "_handle_edit",
                              side_effect=AssertionError("no plan lane")), \
                 patch("harness.agent.ledger_for",
                       return_value=MagicMock()):
                start = time.monotonic()
                res = agent.run_prompt(
                    "check if example.com is up - respond yes/no",
                    session_id="replay", **run_kwargs)
                wall = time.monotonic() - start
        return res, counts, wall

    def _assert_replay(self, res, counts, wall):
        self.assertEqual(res.get("intent"), "simple-action")
        self.assertEqual(res.get("status"), "ok")
        self.assertEqual(counts["probe"], 1)
        self.assertIn("Yes", res.get("response", ""))
        self.assertNotIn("verified", res.get("response", "").lower())
        self.assertLessEqual(counts["probe"], 6)
        self.assertLess(wall, 30)

    def test_replay_within_budgets_default_path(self):
        res, counts, wall = self._replay()
        self._assert_replay(res, counts, wall)

    def test_replay_within_budgets_gui_path(self):
        res, counts, wall = self._replay(force_conversation=True)
        self._assert_replay(res, counts, wall)


if __name__ == "__main__":
    unittest.main()
