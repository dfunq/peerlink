"""Tests for telling which started spawn a newly registering agent is: by the process tree above it, on Windows too.
Run: python3 -m unittest discover -s plugin/daemon"""
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PEERLINK_HOME", tempfile.mkdtemp(prefix="peerlink-spawn-match-test-"))

import peerlinkd  # noqa: E402

d = peerlinkd

# Two herdr terminals in one folder, each running Claude Code under a shell: terminal pid -> shell -> claude.
PARENTS = {101: 100, 100: 7, 201: 200, 200: 7, 7: 1}


class SpawnMatchTest(unittest.TestCase):
    def setUp(self):
        kept = dict(d.spawns)
        self.addCleanup(lambda: (d.spawns.clear(), d.spawns.update(kept)))
        d.spawns.clear()
        now = time.time()
        for sid, address, ago in (("s1", "t1", 20), ("s2", "t2", 10)):
            d.spawns[sid] = {"id": sid, "dir": "/x/api", "status": "started", "updated": now - ago, "hc": address}

    def on_windows(self):
        """Process parents as Windows reports them, with no ps to ask."""
        stack = unittest.mock.patch.multiple(d, windows_parents=lambda: dict(PARENTS),
                                             hc_pid=lambda address: {"t1": 100, "t2": 200}[address])
        stack.start()
        self.addCleanup(stack.stop)
        name = unittest.mock.patch.object(d.os, "name", "nt")
        name.start()
        self.addCleanup(name.stop)

    def test_descends_from_walks_windows_parents(self):
        self.on_windows()
        self.assertTrue(d.descends_from(101, 100))
        self.assertTrue(d.descends_from(100, 100))
        self.assertFalse(d.descends_from(101, 200))
        self.assertFalse(d.descends_from(0, 100))

    def test_two_spawns_in_one_folder_each_get_their_own_agent(self):
        self.on_windows()
        # The newer spawn's agent registers first: it's matched by its terminal, not by which spawn is oldest.
        self.assertEqual(d.spawn_of({"cwd": "/x/api", "pid": 201}, True)["id"], "s2")
        d.spawns["s2"]["agent_id"] = "b"
        self.assertEqual(d.spawn_of({"cwd": "/x/api", "pid": 101}, True)["id"], "s1")

    def test_an_agent_from_no_spawned_terminal_matches_none(self):
        self.on_windows()
        self.assertIsNone(d.spawn_of({"cwd": "/x/api", "pid": 7}, True))

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_windows_parents_knows_this_process(self):
        self.assertEqual(d.windows_parents().get(os.getpid()), os.getppid())


if __name__ == "__main__":
    unittest.main()
