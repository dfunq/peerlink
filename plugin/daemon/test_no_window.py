"""On Windows the daemon runs console programs without a window of their own.
Run: python3 -m unittest discover -s plugin/daemon"""
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PEERLINK_HOME", tempfile.mkdtemp(prefix="peerlink-no-window-test-"))

import peerlinkd  # noqa: E402


@unittest.skipUnless(os.name == "nt", "Windows only")
class NoWindowTest(unittest.TestCase):
    def flags_for(self, **kwargs):
        seen = {}
        with unittest.mock.patch.object(peerlinkd, "_popen_init", lambda self, *a, **kw: seen.update(kw)):
            subprocess.Popen.__init__(object.__new__(subprocess.Popen), ["hc"], **kwargs)
        return seen.get("creationflags", 0)

    def test_console_programs_get_no_window(self):
        self.assertTrue(self.flags_for() & subprocess.CREATE_NO_WINDOW)

    def test_detached_and_new_console_are_left_alone(self):
        for flag in (subprocess.DETACHED_PROCESS, subprocess.CREATE_NEW_CONSOLE):
            self.assertEqual(self.flags_for(creationflags=flag), flag)


if __name__ == "__main__":
    unittest.main()
