"""A session the daemon started for an agent (a schedule's, a standing agent's restart) is that agent, even beside
another agent in its folder; and a default lead is pinned only to the agent that leads, never to a worker there."""
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["PEERLINK_HOME"] = tempfile.mkdtemp(prefix="peerlink-identity-claims-test-")

import peerlinkd as d  # noqa: E402


class IdentityClaims(unittest.TestCase):
    def setUp(self):
        d.load_config()
        for value in ("agents", "spawns", "agent_of", "sessions"):
            p = unittest.mock.patch.object(d, value, {})
            p.start()
            self.addCleanup(p.stop)
        p = unittest.mock.patch.object(d, "agent_seq", [0])
        p.start()
        self.addCleanup(p.stop)
        p = unittest.mock.patch.object(d, "save_config", lambda: None)
        p.start()
        self.addCleanup(p.stop)
        self.folder = os.path.join(tempfile.mkdtemp(prefix="claims-"), "crew")
        os.makedirs(self.folder)

    def session(self, sid, name, agent=None, terminal=""):
        rec = {"id": sid, "name": name, "cwd": self.folder, "last_seen": time.time(), "started": time.time(), "terminal": terminal}
        if agent:
            rec["agent"] = agent
            d.agents.setdefault(agent, {"id": agent, "name": name, "reg": name, "cwd": self.folder, "sid": sid, "seen": time.time()})
        d.sessions[sid] = rec
        return rec

    def test_a_schedules_start_beside_another_agent_is_the_schedules_agent(self):
        d.agents["a1"] = {"id": "a1", "name": "crew", "reg": "crew", "cwd": self.folder, "sid": "old", "seen": 1, "terminal": "hc:t1@1"}
        self.session("q", "quality", agent="a2", terminal="hc:t2@2")
        rec = self.session("new", "crew", terminal="hc:t9@9")
        rec["_claim"] = "crew"
        self.assertEqual(d.agent_for(rec)[0]["id"], "a1")
        self.assertNotIn("_claim", rec)

    def test_a_plain_start_beside_another_agent_is_a_new_agent(self):
        d.agents["a1"] = {"id": "a1", "name": "crew", "reg": "crew", "cwd": self.folder, "sid": "old", "seen": 1, "terminal": "hc:t1@1"}
        self.session("q", "quality", agent="a2", terminal="hc:t2@2")
        rec = self.session("new", "crew", terminal="hc:t9@9")
        self.assertNotIn(d.agent_for(rec)[0]["id"], ("a1", "a2"))

    def test_a_default_lead_is_never_pinned_to_a_worker(self):
        d.config.setdefault("folder_spaces", {})[self.folder] = "crew"
        d.config.setdefault("space_leads", {})["crew"] = {"cwd": self.folder, "how": "only"}
        self.session("w", "fix-1", agent="a5")
        d.spawns["s1"] = {"id": "s1", "dir": self.folder, "worker": True, "status": "started", "agent_id": "w", "agent": "a5",
                          "created": time.time(), "updated": time.time()}
        d.note_lone_leads()
        self.assertNotIn("agent", d.config["space_leads"]["crew"])
        self.session("lead", "crew", agent="a6")
        d.note_lone_leads()
        self.assertEqual(d.config["space_leads"]["crew"].get("agent"), "a6")


    def test_a_huge_pid_counts_as_alive(self):
        self.assertTrue(d.pid_alive(10 ** 30))


if __name__ == "__main__":
    unittest.main()
