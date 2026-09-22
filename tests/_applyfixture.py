"""Shared apply-engine test fixtures: lane constants, the scripted gate
runner, and the engine env builder. Imported by test_apply.py and
test_morph.py so the fixture has one home."""
import os
import tempfile
import unittest

from harness.apply import ApplyEngine
from harness.spend import SpendGovernor
from harness.ledger import AutonomyLedger
from harness.router import Router
from tests._fake import FakeTransport, m

JUDGE = "inclusionai/ling-2.6-flash"
APPLY = "deepseek/deepseek-chat"
ESC = "qwen/qwen3-max"
CODER_A = "cohere/north-mini-code:free"
CODER_B = "z-ai/glm-5.2:free"
MORPH = "morph/morph-v3-fast"

ORIGINAL = "def add(a, b):\n    return a + b\n"
CHANGED = "def add(a, b):\n    return a + b + 0\n"
PARTIAL = "def add(a, b):\n    return a + b  # WIP\n"


def scripted_run(results):
    state = {"n": 0}

    def runner(cmd, timeout=None, cwd=None):
        rc, out = results[min(state["n"], len(results) - 1)]
        state["n"] += 1
        return rc, out

    return runner


class ApplyFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ledger_path = os.path.join(self.dir.name, "ledger.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def make_file(self, content=ORIGINAL):
        p = os.path.join(self.dir.name, "math.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def make_env(self, posts=None, run=None, router_kw=None, default_consent=True,
                 renew=False, models=None, jev_posts=None, with_jev=False,
                 jev_policy=None):
        fake = FakeTransport(models=models or [m(APPLY), m(JUDGE), m(ESC),
                                               m(CODER_A), m(CODER_B)],
                             posts=posts, jev_posts=jev_posts)
        gov = SpendGovernor(fake, "sk-test")
        ledger = AutonomyLedger(self.ledger_path)
        if with_jev and jev_policy is None:
            from harness.config import load_settings
            from harness.jev_policy import policy_for
            from harness.jev import JevEvaluator
            jev_settings = load_settings()
            jev_settings.jev_api_key = "sk-jev-test"
            evaluator = JevEvaluator(api_key="sk-jev-test", transport=fake)
            jev_policy = policy_for(jev_settings, transport=fake, governor=gov,
                                    ledger=ledger, evaluator=evaluator)
        router_args = dict(router_kw or {})
        if jev_policy is not None and "jev_policy" not in router_args:
            router_args["jev_policy"] = jev_policy
        router = Router(["a", "b"], JUDGE, APPLY, **router_args)
        engine = ApplyEngine(fake, "k", gov, ledger, router,
                             default_require_consent=default_consent,
                             default_renew_consent=renew,
                             jev_policy=jev_policy)
        if run:
            engine.run_verify = run
        return fake, gov, ledger, engine

    def leftovers(self):
        return [n for n in os.listdir(self.dir.name) if n.endswith(".tmp")]
