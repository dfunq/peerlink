"""A herdr sync pass in the daemon: reads live agents and hc, moves terminals by relabelling, names spaces and tabs
with the app open, remembers the herdr space ids, and never runs while herdr is off."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["PEERLINK_HOME"] = tempfile.mkdtemp(prefix="peerlink-herdr-sync-test-")

import peerlinkd as d  # noqa: E402


class Pass(unittest.TestCase):
    def setUp(self):
        d.load_config()
        for value in ("sessions", "agents", "agent_of"):
            p = unittest.mock.patch.object(d, value, {})
            p.start()
            self.addCleanup(p.stop)
        p = unittest.mock.patch.object(d, "save_config", lambda: None)
        p.start()
        self.addCleanup(p.stop)
        root = tempfile.mkdtemp(prefix="hsync-")
        self.tools, self.ops = os.path.join(root, "peerlink"), os.path.join(root, "services")
        for f, space in ((self.tools, "tools"), (self.ops, "ops")):
            os.makedirs(f)
            d.config.setdefault("folder_spaces", {})[f] = space
        d.config["herdr_spaces"] = {}
        self.session("s1", "peerlink", self.tools, "hc:t8@100")
        self.session("s2", "services", self.ops, "hc:t9@200")
        self.labels = {"t8": {"herdr.space": "spmac", "herdr.tab": "tb8"}, "t9": {"herdr.space": "spmac", "herdr.tab": "tb9"}}
        self.pids = {"t8": 100, "t9": 200}
        self.app_open = True
        self.ran = []
        self.broken = set()
        self.label_fails = set()

    def session(self, sid, name, cwd, terminal):
        d.sessions[sid] = {"id": sid, "name": name, "cwd": cwd, "terminal": terminal, "agent": "a-" + name,
                           "last_seen": time.time(), "started": time.time()}

    def fake_run(self, argv, **kw):
        tool, args = os.path.basename(argv[0]), argv[1:]
        out, code = "", 0
        if tool == "hc" and args[:1] == ["ls"]:
            out = json.dumps({"terminals": [{"address": a, "state": "live"} for a in self.labels]})
        elif tool == "hc" and args[:1] == ["get"]:
            if args[1] in self.broken:
                return subprocess.CompletedProcess(argv, 1, "", "gone")
            out = json.dumps({"address": args[1], "state": "live", "pid": self.pids[args[1]], "labels": self.labels[args[1]]})
        elif tool == "hc" and args[:1] == ["label"]:
            self.ran.append(args)
            if args[1] in self.label_fails:
                return subprocess.CompletedProcess(argv, 1, "", "no")
            self.labels[args[1]].update(dict(kv.split("=", 1) for kv in args[2:]))
        elif tool == "herdr-app" and args == ["ls"]:
            if not self.app_open:
                code = 1
            else:
                spaces = {}
                for a, l in self.labels.items():
                    spaces.setdefault(l["herdr.space"], {}).setdefault(l["herdr.tab"], []).append({"terminal": a})
                out = json.dumps({"spaces": [{"id": s, "name": "mac" if s == "spmac" else "x", "machine": None,
                                              "tabs": [{"id": t, "name": "1", "terminals": ts} for t, ts in tabs.items()]}
                                             for s, tabs in spaces.items()]})
        elif tool == "herdr-app":
            self.ran.append(args)
        return subprocess.CompletedProcess(argv, code, out, "")

    def run_pass(self, apply=True):
        with unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), \
                unittest.mock.patch.object(d, "herdr_app", lambda: "/bin/herdr-app"), \
                unittest.mock.patch.object(d.subprocess, "run", self.fake_run):
            return d.herdr_sync_pass(apply=apply)

    def test_preview_changes_nothing(self):
        r = self.run_pass(apply=False)
        self.assertTrue(any("move peerlink (t8) to herdr space tools" in x for x in r["actions"]))
        self.assertEqual(self.ran, [])
        self.assertEqual(d.config["herdr_spaces"], {})

    def test_a_pass_splits_the_space_then_settles(self):
        self.run_pass()
        self.assertEqual(d.config["herdr_spaces"]["ops"], "spmac")
        tools = d.config["herdr_spaces"]["tools"]
        self.assertEqual(self.labels["t8"]["herdr.space"], tools)
        self.assertEqual(self.labels["t8"]["peerlink.space"], "tools")
        self.assertIn(["do", "space.rename", "--id", "spmac", "--name", "ops"], self.ran)
        park = self.ran.index(["do", "terminal.park", "--terminal", "t8"])
        self.assertTrue(self.ran[park + 1][:2] == ["label", "t8"])  # parked, then relabelled
        self.ran.clear()
        self.run_pass()  # the moved tab shows up now: it gets its names
        self.assertIn(["do", "space.rename", "--id", tools, "--name", "tools"], self.ran)
        self.assertTrue(any(a[:2] == ["do", "tab.rename"] and a[-1] == "peerlink" for a in self.ran))

    def test_a_reused_address_is_left_alone(self):
        self.pids["t8"] = 999  # t8's shell ended and hc gave the address to something else
        self.run_pass()
        self.assertFalse([a for a in self.ran if a[:2] == ["label", "t8"]])

    def test_without_the_app_terminals_still_move(self):
        self.app_open = False
        r = self.run_pass()
        self.assertEqual(self.labels["t8"]["herdr.space"], d.config["herdr_spaces"]["tools"])
        self.assertFalse([a for a in self.ran if a[0] == "do"])  # nothing shown, so nothing parked
        self.assertTrue(any("isn't open" in n for n in r["notes"]))

    def test_a_terminal_hc_cant_read_stops_the_pass(self):
        self.labels["t5"], self.pids["t5"] = {"herdr.space": "spmac", "herdr.tab": "tb8"}, 500  # a shell in t8's tab
        self.broken = {"t5"}
        r = self.run_pass()
        self.assertIn("error", r)
        self.assertEqual(self.ran, [])

    def test_deleting_a_space_forgets_its_herdr_space(self):
        d.config["herdr_spaces"] = {"gone": "sp1", "ops": "sp2"}
        d.delete_space("gone")
        self.assertEqual(d.config["herdr_spaces"], {"ops": "sp2"})

    def test_a_session_without_its_terminal_uses_its_agents(self):
        d.sessions["s1"].pop("terminal")  # back after a daemon restart, before its plugin registers again
        d.agents["a-peerlink"] = {"id": "a-peerlink", "terminal": "hc:t8@100"}
        self.run_pass()
        self.assertEqual(self.labels["t8"]["peerlink.space"], "tools")

    def test_a_failed_relabel_after_parking_gives_the_pane_back(self):
        self.label_fails = {"t8"}
        r = self.run_pass()
        self.assertIn(["do", "terminal.keep", "--terminal", "t8", "--place", "tab"], self.ran)
        self.assertTrue(any("t8" in f and "pane is back" in f for f in r["failed"]))

    def test_off_when_herdr_is_off(self):
        with unittest.mock.patch.dict(d.config, {"use_herdr": False}):
            self.assertIn("error", d.herdr_sync_pass())


if __name__ == "__main__":
    unittest.main()
