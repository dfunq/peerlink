"""An agent runs in one session. A second live session in another terminal (a resumed conversation, an old
pane left running) is another agent, said once, and the two never take turns as one."""
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["PEERLINK_HOME"] = tempfile.mkdtemp(prefix="peerlink-one-session-test-")

import peerlinkd as d  # noqa: E402


class OneSessionPerAgent(unittest.TestCase):
    def setUp(self):
        d.load_config()
        for value in ("agents", "spawns", "agent_of", "sessions"):
            p = unittest.mock.patch.object(d, value, {})
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("agent_seq", [0]), ("save_config", lambda: None), ("split_warned", set()),
                            ("record", lambda m: self.said.append(m)), ("pid_alive", lambda pid: True),
                            ("lead_rec", lambda space: None)):
            p = unittest.mock.patch.object(d, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.said = []
        self.folder = os.path.join(tempfile.mkdtemp(prefix="one-"), "pixelgrid")
        os.makedirs(self.folder)

    def session(self, sid, terminal, agent=None, pid=100):
        rec = {"id": sid, "name": "pixelgrid", "cwd": self.folder, "last_seen": time.time(), "terminal": terminal, "pid": pid}
        if agent:
            rec["agent"] = d.agent_of[sid] = agent
            d.agents.setdefault(agent, {"id": agent, "name": "pixelgrid", "reg": "pixelgrid", "cwd": self.folder, "sid": sid,
                                        "seen": time.time(), "terminal": terminal})
        d.sessions[sid] = rec
        return rec

    def test_a_session_continuing_one_still_running_elsewhere_is_another_agent(self):
        self.session("old", "tmux:%0@1", agent="a4", pid=11)
        rec = self.session("new", "hc:t7@2", pid=22)
        agent, _ = d.agent_for(rec, "old")
        self.assertNotEqual(agent["id"], "a4")
        self.assertEqual(d.sessions["old"]["agent"], "a4")
        self.assertEqual(d.agents["a4"]["sid"], "old")
        self.assertEqual(len(self.said), 1)
        self.assertIn("tmux:%0@1", self.said[0]["text"])
        self.assertIn("hc:t7@2", self.said[0]["text"])
        self.assertEqual(self.said[0]["to"], "user")

    def test_registering_again_never_takes_turns_and_warns_once(self):
        self.session("old", "tmux:%0@1", agent="a4", pid=11)
        rec = self.session("new", "hc:t7@2", agent="a4", pid=22)  # bound before this fix: both on a4
        first, _ = d.agent_for(rec)
        self.assertNotEqual(first["id"], "a4")
        for _ in range(3):
            self.assertEqual(d.agent_for(d.sessions["old"])[0]["id"], "a4")
            self.assertEqual(d.agent_for(rec)[0]["id"], first["id"])
        self.assertEqual(len(self.said), 1)

    def test_a_restart_in_the_same_pane_is_the_same_agent(self):
        self.session("old", "hc:t7@2", agent="a4", pid=11)
        rec = self.session("new", "hc:t7@2", pid=22)
        with unittest.mock.patch.object(d, "pid_alive", lambda pid: int(pid) != 11):
            self.assertEqual(d.agent_for(rec, "old")[0]["id"], "a4")
        self.assertEqual(self.said, [])

    def test_a_clear_in_the_same_process_is_the_same_agent(self):
        self.session("old", "hc:t7@2", agent="a4", pid=11)
        rec = self.session("new", "hc:t7@2", pid=11)
        self.assertEqual(d.agent_for(rec, "old")[0]["id"], "a4")
        self.assertEqual(self.said, [])

    def test_a_dead_session_elsewhere_hands_over(self):
        self.session("old", "tmux:%0@1", agent="a4", pid=11)
        rec = self.session("new", "hc:t7@2", pid=22)
        with unittest.mock.patch.object(d, "pid_alive", lambda pid: int(pid) != 11):
            self.assertEqual(d.agent_for(rec, "old")[0]["id"], "a4")

    def test_running_elsewhere_without_terminals_goes_by_process(self):
        a = {"id": "a", "last_seen": time.time(), "pid": 1}
        self.assertTrue(d.running_elsewhere(a, {"id": "b", "pid": 2}))
        self.assertFalse(d.running_elsewhere(a, {"id": "b", "pid": 1}))
        self.assertFalse(d.running_elsewhere(a, {"id": "b"}))

    def test_the_lead_is_told_too(self):
        told = []
        self.session("old", "tmux:%0@1", agent="a4", pid=11)
        lead = {"id": "L", "cwd": "/lead"}
        rec = self.session("new", "hc:t7@2", pid=22)
        with unittest.mock.patch.object(d, "lead_rec", lambda space: lead), \
             unittest.mock.patch.object(d, "task_message", lambda t, cwd, text, prefer=None: told.append((cwd, text))):
            d.agent_for(rec, "old")
        self.assertEqual(len(told), 1)
        self.assertEqual(told[0][0], "/lead")


if __name__ == "__main__":
    unittest.main()
