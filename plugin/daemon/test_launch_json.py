"""~/.peerlink/launch.json says how to run this daemon, for programs that can't run the sh launcher (a herdr mod on
Windows), and is rewritten only when it changes."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["PEERLINK_HOME"] = tempfile.mkdtemp(prefix="peerlink-launch-test-")

import peerlinkd as d  # noqa: E402


class LaunchJson(unittest.TestCase):
    def test_names_the_python_and_this_daemon(self):
        d.write_launcher()
        conf = json.load(open(d.LAUNCH_JSON))
        self.assertEqual(conf, {"python": sys.executable, "daemon": os.path.abspath(d.__file__)})
        before = os.stat(d.LAUNCH_JSON).st_mtime_ns
        d.write_launcher()
        self.assertEqual(os.stat(d.LAUNCH_JSON).st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
