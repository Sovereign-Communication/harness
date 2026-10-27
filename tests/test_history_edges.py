"""Regression coverage for bounded chat-history edge paths."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.errors import HarnessError
from harness.history import load_chat_checkpoint, load_chat_history


class ChatHistoryEdgeTests(unittest.TestCase):
    def test_history_rejects_empty_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(HarnessError, "invalid session_id"):
                load_chat_history("", history_dir=Path(tmp))

    def test_checkpoint_rejects_non_utf8_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(HarnessError, "invalid session_id"):
                load_chat_checkpoint(chr(0xD800), history_dir=Path(tmp))

    def test_checkpoint_rejects_symlinked_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint_dir = root / "chat_checkpoints"
            checkpoint_dir.mkdir()
            session_id = "symlink-checkpoint"
            checkpoint = checkpoint_dir / (
                hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".json")
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            try:
                checkpoint.symlink_to(target)
            except (NotImplementedError, OSError):
                # Windows runners without symlink privileges still exercise
                # the public loader's filesystem guard deterministically.
                with patch.object(
                    Path, "is_symlink", autospec=True,
                    side_effect=lambda path: path == checkpoint,
                ):
                    with self.assertRaisesRegex(
                        HarnessError, "request lifecycle state is invalid"):
                        load_chat_checkpoint(session_id, history_dir=root)
            else:
                with self.assertRaisesRegex(
                    HarnessError, "request lifecycle state is invalid"):
                    load_chat_checkpoint(session_id, history_dir=root)
