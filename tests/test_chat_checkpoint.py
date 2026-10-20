"""Durable continuation state for interrupted conversational requests."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.errors import HarnessError
from harness.history import (
    CHAT_CHECKPOINT_SCHEMA,
    MAX_CHAT_CHECKPOINT_BYTES,
    clear_chat_checkpoint,
    load_chat_history,
    load_chat_checkpoint,
    save_chat_turn,
    save_chat_checkpoint,
)


class ChatCheckpointTests(unittest.TestCase):
    def test_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = save_chat_checkpoint("session-1", {
                "route": "hourglass", "status": "interrupted",
                "prompt": "audit this repo", "stage": "jev_review",
                "answer": "candidate",
            }, Path(tmp))

            loaded = load_chat_checkpoint("session-1", Path(tmp))
            self.assertEqual(loaded["prompt"], "audit this repo")
            self.assertEqual(loaded["schema"], CHAT_CHECKPOINT_SCHEMA)
            self.assertEqual(saved.parent.name, "chat_checkpoints")
            self.assertTrue(clear_chat_checkpoint("session-1", Path(tmp)))
            self.assertIsNone(load_chat_checkpoint("session-1", Path(tmp)))
            self.assertFalse(clear_chat_checkpoint("session-1", Path(tmp)))

    def test_opaque_ids_are_hashed_without_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            for opaque in ("../outside", "folder\\session", "bad:name",
                           "x" * 512):
                with self.subTest(session_id=opaque):
                    saved = save_chat_checkpoint(
                        opaque, {"prompt": "x"}, Path(tmp))
                    self.assertEqual(saved.parent,
                                     Path(tmp) / "chat_checkpoints")
                    loaded = load_chat_checkpoint(opaque, Path(tmp))
                    self.assertEqual(loaded["prompt"], "x")
            self.assertFalse((Path(tmp) / "outside.json").exists())
            for invalid in ("", " " * 3, "x" * 513):
                with self.subTest(session_id=invalid):
                    with self.assertRaises(HarnessError):
                        save_chat_checkpoint(invalid, {"prompt": "x"}, Path(tmp))
            path = save_chat_turn(
                "folder\\mcp-session", {"prompt": "hello"}, Path(tmp))
            self.assertEqual(path.parent, Path(tmp))
            self.assertTrue(path.name.startswith("opaque_"))
            self.assertEqual(load_chat_history(
                "folder\\mcp-session", Path(tmp))[0]["prompt"], "hello")
            with self.assertRaises(HarnessError):
                save_chat_checkpoint("large", {
                    "prompt": "x", "answer": "a" * MAX_CHAT_CHECKPOINT_BYTES,
                }, Path(tmp))
            with self.assertRaises(HarnessError):
                save_chat_checkpoint("nan", {
                    "prompt": "x", "known_cost": float("nan"),
                }, Path(tmp))

    def test_rejects_corrupt_or_foreign_session_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = save_chat_checkpoint(
                "session-1", {"prompt": "x"}, Path(tmp))
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(HarnessError):
                load_chat_checkpoint("session-1", Path(tmp))
            path.write_text(json.dumps({
                "schema": CHAT_CHECKPOINT_SCHEMA,
                "session_id": "another-session", "prompt": "x",
            }), encoding="utf-8")
            with self.assertRaises(HarnessError):
                load_chat_checkpoint("session-1", Path(tmp))

            path.write_text("x" * (MAX_CHAT_CHECKPOINT_BYTES + 1),
                            encoding="utf-8")
            with self.assertRaises(HarnessError):
                load_chat_checkpoint("session-1", Path(tmp))

    def test_rejects_invalid_checkpoint_shapes_and_completed_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(HarnessError):
                save_chat_checkpoint("bad", [], root)
            with self.assertRaises(HarnessError):
                save_chat_checkpoint("bad\ud800", {"prompt": "x"}, root)

            path = save_chat_checkpoint("session", {"prompt": "x"}, root)
            path.write_text(json.dumps({"schema": "wrong", "prompt": "x"}),
                            encoding="utf-8")
            with self.assertRaises(HarnessError):
                load_chat_checkpoint("session", root)
            path.write_text(json.dumps({
                "schema": CHAT_CHECKPOINT_SCHEMA, "session_id": "session",
                "status": "complete", "prompt": "x",
            }), encoding="utf-8")
            self.assertIsNone(load_chat_checkpoint("session", root))
            path.write_text(json.dumps({
                "schema": CHAT_CHECKPOINT_SCHEMA, "session_id": "session",
                "status": "interrupted", "prompt": "  ",
            }), encoding="utf-8")
            with self.assertRaises(HarnessError):
                load_chat_checkpoint("session", root)

    def test_history_rejects_surrogate_session_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HarnessError):
                save_chat_turn("bad\ud800", {"prompt": "x"}, Path(tmp))

    def test_checkpoint_directory_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "chat_checkpoints").mkdir()
            with patch.object(Path, "is_symlink", return_value=True), \
                    self.assertRaises(HarnessError):
                save_chat_checkpoint("session", {"prompt": "x"}, Path(tmp))

    def test_deleting_chat_session_removes_pending_checkpoint(self):
        from harness.history import delete_chat_session

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"HARNESS_CONFIG_DIR": tmp}):
                history = Path(tmp) / "chat_history"
                history.mkdir()
                session_id = "sess_resume"
                (history / f"{session_id}.jsonl").write_text(
                    "{}\n", encoding="utf-8")
                save_chat_checkpoint(session_id, {
                    "prompt": "audit this repo", "status": "interrupted",
                }, history)

                self.assertTrue(delete_chat_session(session_id))
                self.assertIsNone(load_chat_checkpoint(session_id, history))


if __name__ == "__main__":
    unittest.main()
