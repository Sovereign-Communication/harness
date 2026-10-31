"""Issue 427: mission readers must not skip a damaged record.

``_load_jsonl`` used to catch a malformed line and continue, so a corrupted
middle line was presented as a complete stream. That stream feeds resume
attempt counts, STATUS and ``pack_summary``, which means corrupted evidence
could be reported as a complete receipt history. These tests pin the refusal
at every consumer, and that a refusal never rewrites the bytes.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from harness import mission_record as mr
from harness.errors import HarnessError
from harness.mission_driver import run_mission


def _spec(mission_id="m-integrity", root="missions"):
    return mr.build_mission_spec(
        mission_id=mission_id,
        request="Prove mission readers refuse damaged streams",
        success_definition="A malformed record stops status and resume",
        max_cost_usd=0.50,
        terminal_reserve_cost_usd=0.05,
        in_scope=["mission_record.py"],
        out_of_scope=["live provider calls"],
        persistence_root=root,
        verifier_kind="hermetic-local",
    )


class MissionIntegrityTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "missions"
        root.mkdir(parents=True, exist_ok=True)
        self.pack = mr.init_mission_pack(root, _spec())

    def _write(self, path, text):
        # open(…, newline=) rather than Path.write_text(newline=), which
        # only grew that argument in Python 3.10; this suite supports 3.9.
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def _receipts(self, text):
        self._write(self.pack.receipts_path, text)

    def test_a_valid_stream_still_loads(self):
        self._receipts(json.dumps({"kind": "attempt", "id": "r1"}) + "\n"
                       + json.dumps({"kind": "attempt", "id": "r2"}) + "\n")
        self.assertEqual([r["id"] for r in mr.load_receipts(self.pack)],
                         ["r1", "r2"])

    def test_a_malformed_middle_row_is_refused_by_path_and_line(self):
        self._receipts(json.dumps({"kind": "attempt", "id": "r1"}) + "\n"
                       + "not json\n"
                       + json.dumps({"kind": "attempt", "id": "r2"}) + "\n")
        with self.assertRaises(HarnessError) as ctx:
            mr.load_receipts(self.pack)
        message = str(ctx.exception)
        self.assertIn(str(self.pack.receipts_path), message)
        self.assertIn(":2:", message)
        self.assertIn("malformed", message)

    def test_a_non_object_row_is_refused(self):
        self._receipts(json.dumps({"kind": "attempt"}) + "\n"
                       + "[1, 2, 3]\n")
        with self.assertRaises(HarnessError) as ctx:
            mr.load_receipts(self.pack)
        self.assertIn(":2:", str(ctx.exception))
        self.assertIn("JSON object", str(ctx.exception))

    def test_an_interrupted_final_row_fails_closed(self):
        self._receipts(json.dumps({"kind": "attempt", "id": "r1"}) + "\n"
                       + '{"kind": "attempt", "id": "r2"')
        with self.assertRaises(HarnessError) as ctx:
            mr.load_receipts(self.pack)
        self.assertIn("interrupted final record", str(ctx.exception))

    def test_invalid_utf8_is_refused_with_path_and_line(self):
        damaged = b'{"valid": true}\n{"bad": "\xff"}\n'
        self.pack.receipts_path.write_bytes(damaged)
        before = self.pack.receipts_path.read_bytes()
        with self.assertRaises(HarnessError) as ctx:
            mr.load_receipts(self.pack)
        message = str(ctx.exception)
        self.assertIn(str(self.pack.receipts_path), message)
        self.assertIn(":2:", message)
        self.assertIn("malformed UTF-8", message)
        self.assertEqual(self.pack.receipts_path.read_bytes(), before)

    def test_refusal_preserves_the_bytes(self):
        text = (json.dumps({"kind": "attempt", "id": "r1"}) + "\n"
                + "garbage\n")
        self._receipts(text)
        before = self.pack.receipts_path.read_bytes()
        for reader in (mr.load_receipts,
                       lambda pack: mr.build_resume_state(pack),
                       mr.generate_status_md,
                       mr.pack_summary):
            with self.subTest(reader=getattr(reader, "__name__", "lambda")):
                with self.assertRaises(HarnessError):
                    reader(self.pack)
                self.assertEqual(self.pack.receipts_path.read_bytes(), before)

    def test_a_damaged_jev_evaluation_stream_also_blocks_status(self):
        self._write(self.pack.jev_evals_path, '{"axis": "confidence"}\n'
                                              '{"axis": broken\n')
        with self.assertRaises(HarnessError):
            mr.load_jev_evals(self.pack)
        with self.assertRaises(HarnessError):
            mr.build_resume_state(self.pack)
        with self.assertRaises(HarnessError):
            mr.generate_status_md(self.pack)
        with self.assertRaises(HarnessError):
            mr.pack_summary(self.pack)

    def test_append_refuses_a_damaged_existing_stream_without_changing_bytes(self):
        for path, append in (
                (self.pack.receipts_path, mr.append_receipt),
                (self.pack.jev_evals_path, mr.append_jev_eval)):
            with self.subTest(path=path.name):
                self._write(path, '{"valid": true}\nnot json\n')
                before = path.read_bytes()
                with self.assertRaises(HarnessError):
                    append(self.pack, {"new": "record"})
                self.assertEqual(path.read_bytes(), before)

    def test_driver_refuses_damaged_streams_before_terminal_side_effects(self):
        for stream_name in ("receipts.jsonl", "jev-evals.jsonl"):
            with self.subTest(stream=stream_name):
                root = Path(self.tmp.name) / stream_name.replace(".", "-")
                pack = mr.init_mission_pack(
                    root, _spec(mission_id="m-driver-" + stream_name.split(".")[0],
                                root=str(root)))
                damaged = (pack.receipts_path if stream_name == "receipts.jsonl"
                           else pack.jev_evals_path)
                self._write(damaged, '{"valid": true}\nnot json\n')
                tracked = (pack.receipts_path, pack.jev_evals_path,
                           pack.resume_path, pack.status_md,
                           pack.findings_md, pack.index_md)
                before = {item: item.read_bytes() for item in tracked}
                called = []

                def attempt(_context):
                    called.append(True)
                    return {"ok": True}

                with self.assertRaises(HarnessError):
                    run_mission(pack, attempt_fn=attempt, max_attempts=0)
                self.assertEqual(called, [])
                self.assertEqual(
                    {item: item.read_bytes() for item in tracked}, before)
                self.assertEqual(damaged.read_bytes(), b'{"valid": true}\nnot json\n')


    def test_resume_cli_refuses_damaged_histories_before_rewriting_resume(self):
        for stream_name in ("receipts.jsonl", "jev-evals.jsonl"):
            with self.subTest(stream=stream_name):
                root = Path(self.tmp.name) / ("resume-" + stream_name.replace(".", "-"))
                pack = mr.init_mission_pack(
                    root, _spec(mission_id="m-resume-" + stream_name.split(".")[0],
                                root=str(root)))
                # A valid but non-canonical resume file makes an early rewrite
                # observable even when the stored counters remain unchanged.
                resume_before = (
                    "{\"status\":\"in_progress\",\"mission_id\":\"" + pack.id
                    + "\",\"schema_version\":1,\"receipts_seq\":0,"
                    + "\"attempts\":0,\"spent\":0.0,\"updated_at\":\"fixed\"}\n"
                ).encode("utf-8")
                pack.resume_path.write_bytes(resume_before)
                damaged = (pack.receipts_path if stream_name == "receipts.jsonl"
                           else pack.jev_evals_path)
                damaged.write_bytes(b'{"valid": true}\nnot json\n')
                tracked = (pack.receipts_path, pack.jev_evals_path,
                           pack.resume_path, pack.status_md, pack.findings_md,
                           pack.index_md)
                before = {item: item.read_bytes() for item in tracked}

                result = subprocess.run(
                    [sys.executable, "-m", "harness.cli", "mission", "resume",
                     "--id", pack.id, "--root", str(root)],
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace",
                )

                self.assertEqual(result.returncode, 1)
                self.assertIn(f"{damaged}:2:", result.stderr)
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                self.assertEqual(
                    {item: item.read_bytes() for item in tracked}, before)

    def test_mark_terminal_refuses_damaged_histories_before_writing(self):
        for stream_name in ("receipts.jsonl", "jev-evals.jsonl"):
            with self.subTest(stream=stream_name):
                root = Path(self.tmp.name) / ("terminal-" + stream_name.replace(".", "-"))
                pack = mr.init_mission_pack(
                    root, _spec(mission_id="m-terminal-" + stream_name.split(".")[0],
                                root=str(root)))
                mr.write_resume(pack, mr.build_resume_state(pack))
                mr.write_status(pack)
                damaged = (pack.receipts_path if stream_name == "receipts.jsonl"
                           else pack.jev_evals_path)
                self._write(damaged, '{"valid": true}\nnot json\n')
                tracked = (pack.receipts_path, pack.jev_evals_path,
                           pack.resume_path, pack.status_md, pack.findings_md)
                before = {item: item.read_bytes() for item in tracked}

                with self.assertRaises(HarnessError):
                    mr.mark_terminal(pack, outcome="complete")

                self.assertEqual(
                    {item: item.read_bytes() for item in tracked}, before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
