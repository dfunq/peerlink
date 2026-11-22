"""Agent ids are never handed out twice, so a new worker can't inherit an old worker's spawn (its lead, task and
worktree) through a reused id; and a worker's spawn is the one that started it, never an older one."""
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["PEERLINK_HOME"] = tempfile.mkdtemp(prefix="peerlink-agent-ids-test-")

import peerlinkd as d  # noqa: E402


class AgentIds(unittest.TestCase):
    def setUp(self):
        d.load_config()
        for target, value in ((d, "agents"), (d, "spawns"), (d, "agent_of")):
            p = unittest.mock.patch.object(target, value, {})
            p.start()
            self.addCleanup(p.stop)
        p = unittest.mock.patch.object(d, "agent_seq", [0])
        p.start()
        self.addCleanup(p.stop)

    def test_a_deleted_agents_id_is_never_handed_out_again(self):
        for _ in range(3):
            aid = d.new_agent_id()
            d.agents[aid] = {"id": aid}
        d.agents.pop("a3")
        self.assertEqual(d.new_agent_id(), "a4")

    def test_the_counter_survives_a_restart(self):
        d.agents["a7"] = {"id": "a7"}
        d.save_agents()
        d.agents.clear()
        d.agent_seq[0] = 0
        d.load_agents()
        d.agents.pop("a7")
        self.assertEqual(d.new_agent_id(), "a8")

    def test_ids_an_old_spawn_still_holds_are_skipped(self):
        # From before the counter: agent a30 was deleted, and the highest left is a5.
        d.agents["a5"] = {"id": "a5"}
        d.spawns["s12"] = {"id": "s12", "agent": "a30", "worker": True, "status": "retired"}
        d.save_agents()
        d.agent_seq[0] = 0
        d.load_agents()
        self.assertEqual(d.new_agent_id(), "a31")


    def test_deleting_an_agent_clears_it_from_the_spawns_that_named_it(self):
        d.agents["a30"] = {"id": "a30", "cwd": "/tmp/nowhere"}
        d.spawns["s12"] = {"id": "s12", "agent": "a30", "worker": True, "status": "retired"}
        with unittest.mock.patch.object(d, "save_spawns", lambda: None), unittest.mock.patch.object(d, "save_config", lambda: None):
            code, _ = d.delete_agent("a30")
        self.assertEqual(code, 200)
        self.assertNotIn("agent", d.spawns["s12"])


class WorkerSpawn(unittest.TestCase):
    def setUp(self):
        p = unittest.mock.patch.object(d, "spawns", {})
        p.start()
        self.addCleanup(p.stop)

    def test_a_worker_gets_its_own_spawn_not_an_older_one_with_the_same_agent(self):
        d.spawns["s12"] = {"id": "s12", "agent": "a30", "agent_id": "old-session", "worker": True, "status": "retired", "created": 1}
        d.spawns["s44"] = {"id": "s44", "agent": "a30", "agent_id": "new-session", "worker": True, "status": "started", "created": 2}
        self.assertEqual(d.worker_spawn({"id": "new-session", "agent": "a30"})["id"], "s44")
        # After a /clear its session is new, and the running spawn of its agent is still the one, not the retired one.
        self.assertEqual(d.worker_spawn({"id": "after-clear", "agent": "a30"})["id"], "s44")

    def test_a_retired_spawn_is_no_ones_worker_spawn_by_agent(self):
        d.spawns["s12"] = {"id": "s12", "agent": "a30", "agent_id": "old-session", "worker": True, "status": "retired", "created": 1}
        self.assertIsNone(d.worker_spawn({"id": "someone-else", "agent": "a30"}))


if __name__ == "__main__":
    unittest.main()
