import json
import tempfile
from unittest import mock

from harness.config import load_settings
from harness.jev_policy import policy_for
from harness.ledger import AutonomyLedger

import tests.test_hourglass_jev_integrations as T

tmp = tempfile.mkdtemp()
ledger = AutonomyLedger(tmp + "/ledger.jsonl")
gov = T.RecordingGovernor()

with mock.patch("harness.config.CONFIG_DIR", tmp), \
        mock.patch("harness.config.resolve_api_key", return_value=None):
    settings = load_settings({"jev_api_key": None})

policy = policy_for(settings, transport=None, governor=gov, ledger=ledger,
                    evaluator=T.FakeEvaluator({}))
print("policy.keyed =", policy.keyed)
result, structural = policy.evaluate_hourglass_stage(
    "context_intake", {"request": "x"})
print("is_fallback =", result.is_fallback)
print("result.status =", result.status)
print(json.dumps({k: v for k, v in structural.items()
                  if k not in ("declared_signals",)},
                 indent=2, default=str))
print("ledger events:", [e["event"] for e in ledger.entries()])
