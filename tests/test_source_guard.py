"""Offline checks for frozen byte and snapshot tamper detection."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import source_guard


class SourceGuardTests(unittest.TestCase):
    def test_byte_change_and_missing_source_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen = root / "frozen.py"
            original = b"# mixed\r\nx = 1\n"
            frozen.write_bytes(original)
            index = root / "index.json"
            index.write_text(json.dumps(dict(
                source_root=str(root / "unavailable"), archive="absent.zip", archive_sha256="",
                files=[dict(path="frozen.py", frozen=True, sha256=source_guard.digest(original))])), encoding="utf-8")
            with patch.object(source_guard, "ROOT", root), patch.object(source_guard, "INDEX", index), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(source_guard.check(), 0)
                self.assertEqual(source_guard.check(require_main=True), 1)
                frozen.write_bytes(original.replace(b"\r\n", b"\n"))
                self.assertEqual(source_guard.check(), 1)
                frozen.unlink()
                self.assertEqual(source_guard.check(), 1)

    def test_snapshot_change_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "original.zip"
            archive.write_bytes(b"snapshot")
            index = root / "index.json"
            index.write_text(json.dumps(dict(source_root=str(root / "unavailable"),
                archive="original.zip", archive_sha256=source_guard.digest(b"snapshot"), files=[])), encoding="utf-8")
            with patch.object(source_guard, "ROOT", root), patch.object(source_guard, "INDEX", index), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(source_guard.check(), 0)
                archive.write_bytes(b"tampered")
                self.assertEqual(source_guard.check(), 1)
