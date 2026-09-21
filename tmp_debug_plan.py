import os, tempfile
from unittest.mock import patch, MagicMock
from types import SimpleNamespace
from harness.cli import _cmd_plan
from tests.test_planning_surface import TestPlanningSurface
t=TestPlanningSurface("test_cli_cmd_plan_execute_isolated_parallel_with_stage_gate"); t.setUp()
def fake_apply(**kwargs):
    runner=kwargs.get("task_runner")
    if runner is not None:
        runner("git status --porcelain")
    return {"status":"ok","cost":0.001}
mock_engine=MagicMock(); mock_engine.apply_edit.side_effect=fake_apply
opts=SimpleNamespace(goal="Update the modules",file=["iso_a.py","iso_b.py"],frontier_model=None,execute=True,parallel=True,max_workers=2,max_cost=1.0,keep_going=False,out=None,model=None,max_tokens=None,task_max_cost=None,allow_escalation=False,reasoning_effort=None,max_rotations=3,isolate=True,stage_gate="git rev-parse HEAD")
settings=SimpleNamespace(use_free=False,frontier_model=None,hourglass_confirm=True,hourglass_require_attestation=False,ledger_path=os.path.join(tempfile.mkdtemp(),"l.jsonl"))
with patch("harness.cli._session", return_value=mock_engine), patch("harness.cli._compose_plan", side_effect=t._canned_plan) as cp, patch("harness.cli._emit_by_status") as mock_emit:
    _cmd_plan(opts, settings)
print("emit", mock_emit.call_args)
if mock_emit.call_args:
    import json
    print(json.dumps(mock_emit.call_args[0][0], indent=2)[:2500])
print("compose kwargs keys", cp.call_args[1].keys() if cp.call_args and cp.call_args[1] else cp.call_args)
