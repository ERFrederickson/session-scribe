import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

from session_scribe.config import Config
from session_scribe.controller import Controller
from session_scribe.session import Session

trashed = []
sys.modules["send2trash"] = types.SimpleNamespace(
    send2trash=lambda p: (trashed.append(p), shutil.rmtree(p)))


class DeleteTests(unittest.TestCase):
    def test_delete_rules(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(Path(d) / "config.json")
            cfg.settings.data_dir = str(Path(d) / "data")
            c = Controller(cfg)
            s = Session.create(cfg.sessions_dir, "Test")
            s.meta["status"] = "transcribed"
            s.save()
            other = Path(d) / "not-a-session"
            other.mkdir()

            self.assertIsNotNone(c.delete_session(str(other)))       # outside sessions folder
            self.assertTrue(other.exists())

            c.jobs[str(s.dir)] = {"status": "running"}
            self.assertIn("transcribed", c.delete_session(str(s.dir)))  # busy
            self.assertTrue(s.dir.exists())

            c.jobs[str(s.dir)] = {"status": "done"}
            self.assertIsNone(c.delete_session(str(s.dir)))
            self.assertFalse(s.dir.exists())
            self.assertEqual(c.sessions(), [])
            self.assertNotIn(str(s.dir), c.jobs)


if __name__ == "__main__":
    unittest.main()
