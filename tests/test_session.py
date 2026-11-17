from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest

from ds940.session import SessionManager


class SessionManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.work = Path(self.tmpdir.name)
        self.mgr = SessionManager(self.work, idle_timeout=180.0)

    def test_initial_state_is_active(self):
        self.assertTrue(self.mgr.is_active())
        self.assertGreater(self.mgr.get_remaining(), 175.0)

    def test_pause_and_wake(self):
        self.mgr.pause()
        self.assertFalse(self.mgr.is_active())
        self.assertEqual(self.mgr.get_remaining(), 0.0)

        self.mgr.touch(duration=120.0)
        self.assertTrue(self.mgr.is_active())
        self.assertGreater(self.mgr.get_remaining(), 115.0)

    def test_extend(self):
        self.mgr.pause()
        self.assertEqual(self.mgr.get_remaining(), 0.0)
        self.mgr.extend(extra_seconds=300.0)
        self.assertTrue(self.mgr.is_active())
        self.assertGreater(self.mgr.get_remaining(), 290.0)

    def test_file_command_synchronization(self):
        self.mgr.pause()
        self.assertFalse(self.mgr.is_active())

        # Simulate another process writing session-command.json
        cmd_file = self.work / "session-command.json"
        cmd_file.write_text(json.dumps({"action": "wake", "duration": 200.0}), encoding="utf-8")

        # Calling get_remaining checks command file
        rem = self.mgr.get_remaining()
        self.assertGreater(rem, 190.0)
        self.assertTrue(self.mgr.is_active())
        self.assertFalse(cmd_file.exists())  # Unlinked after reading


if __name__ == "__main__":
    unittest.main()
