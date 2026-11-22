"""Agents that share a folder keep their own identity across restarts: a new process is known by the terminal it
runs in, a name claims nothing while another agent runs in the folder, and a default lead is pinned to its agent."""
import os
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rig import Rig  # noqa: E402


class SharedFolder(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(env={"PEERLINK_TEST": "1"}).__enter__()
        self.addCleanup(self.rig.__exit__, None, None, None)
        self.folder = tempfile.mkdtemp(prefix="shared-folder-")

    def start(self, terminal=None, folder=None, pid=None):
        """A new Claude Code process in the folder: a fresh session id, no /clear behind it."""
        sid = str(uuid.uuid4())
        body = {"id": sid, "name": os.path.basename(folder or self.folder), "cwd": folder or self.folder, "plugin": "rig"}
        if terminal:
            body["terminal"] = terminal
        if pid:
            body["pid"] = pid
        code, out = self.rig.call("POST", "/api/sessions", body)
        self.assertEqual(code, 200, out)
        return sid

    def stop(self, sid):
        self.rig.call("DELETE", f"/api/sessions/{sid}")

    def name(self, sid):
        self.rig.call("POST", f"/api/sessions/{sid}/heartbeat", {"busy": False, "ack": [], "holding": []})
        peers = self.rig.call("GET", "/api/peers?local=1")[1]["peers"]
        return next(p for p in peers if p["id"] == sid)

    def rename(self, sid, nick):
        code, out = self.rig.call("POST", f"/api/sessions/{sid}/rename", {"name": nick})
        self.assertEqual(code, 200, out)

    def test_two_agents_restarting_in_the_other_order_each_get_their_own_agent_back(self):
        a, b = self.start("hc:t1"), self.start("hc:t2")
        self.rename(a, "lead")
        self.rename(b, "helper")
        agent_a, agent_b = self.name(a)["agent"], self.name(b)["agent"]
        self.assertNotEqual(agent_a, agent_b)
        self.stop(a)
        self.stop(b)
        # They come back the other way round: the helper's terminal first.
        b2, a2 = self.start("hc:t2"), self.start("hc:t1")
        self.assertEqual((self.name(b2)["agent"], self.name(b2)["name"]), (agent_b, "helper"))
        self.assertEqual((self.name(a2)["agent"], self.name(a2)["name"]), (agent_a, "lead"))

    def test_without_a_terminal_a_new_process_beside_a_running_agent_is_a_new_agent(self):
        a, b = self.start(), self.start()
        self.rename(b, "helper")
        agent_b = self.name(b)["agent"]
        self.stop(b)
        # The helper's agent is free, but a fresh session beside the running one can't tell it's the helper.
        c = self.start()
        self.assertNotEqual(self.name(c)["agent"], agent_b)
        self.assertNotEqual(self.name(c)["agent"], self.name(a)["agent"])

    def test_a_numbered_stand_in_name_claims_nothing(self):
        a = self.start()
        b = self.start()  # takes "<folder>-2"
        agent_b = self.name(b)["agent"]
        self.stop(a)
        self.stop(b)
        c = self.start()  # alone now, and named "<folder>": the folder's first agent, not the -2 one
        self.assertNotEqual(self.name(c)["agent"], agent_b)

    def test_an_agent_alone_in_its_folder_still_comes_back_by_name(self):
        folder = tempfile.mkdtemp(prefix="own-folder-")
        a = self.start(folder=folder)
        self.rename(a, "solo")
        agent = self.name(a)["agent"]
        self.stop(a)
        again = self.start(folder=folder)
        self.assertEqual(self.name(again)["agent"], agent)

    def test_a_default_lead_is_pinned_to_its_agent(self):
        with open(os.path.join(self.folder, ".peerlink"), "w") as f:
            f.write("space: crew\n")
        a = self.start("hc:t1")
        self.name(a)  # a heartbeat, after which the lead knows its agent
        lead = self.rig.user("GET", "/api/config")["space_leads"]["crew"]
        self.assertEqual((lead["how"], lead.get("agent")), ("only", self.name(a)["agent"]))

    def test_a_new_tab_reusing_a_closed_tabs_tty_is_not_the_agent_that_ran_there(self):
        a, b = self.start("tty:ttys003@100"), self.start("tty:ttys004@200")
        self.rename(b, "helper")
        agent_b = self.name(b)["agent"]
        self.stop(b)
        # The tab closed, and a new one got its tty: the shell under it is another.
        c = self.start("tty:ttys004@300")
        self.assertNotEqual(self.name(c)["agent"], agent_b)
        self.assertNotEqual(self.name(c)["agent"], self.name(a)["agent"])

    def test_a_restart_after_an_unclean_exit_takes_its_agent_back(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        a, b = self.start("hc:t1@100", pid=gone.pid), self.start("hc:t2@200", pid=os.getpid())
        self.rename(a, "lead")
        agent_a = self.name(a)["agent"]
        # a was killed (no DELETE): its session still looks live, but its process is gone.
        a2 = self.start("hc:t1@100", pid=os.getpid())
        self.assertEqual((self.name(a2)["agent"], self.name(a2)["name"]), (agent_a, "lead"))
        self.assertNotEqual(self.name(b)["agent"], agent_a)

    def test_a_process_still_running_in_the_same_pane_keeps_its_agent(self):
        # A claude -p inside a running session's pane shares its terminal key: the running one keeps its agent.
        a = self.start("hc:t1@100", pid=os.getpid())
        agent_a = self.name(a)["agent"]
        nested = self.start("hc:t1@100", pid=os.getpid())
        self.assertNotEqual(self.name(nested)["agent"], agent_a)
        self.assertEqual(self.name(a)["agent"], agent_a)

    def test_a_numbered_name_from_another_folder_still_finds_its_agent(self):
        root = tempfile.mkdtemp(prefix="same-base-")
        first, second = os.path.join(root, "a", "web"), os.path.join(root, "b", "web")
        os.makedirs(first)
        os.makedirs(second)
        self.start(folder=first)
        b = self.start(folder=second)  # web-2: web runs in the other folder
        self.assertEqual(self.name(b)["name"], "web-2")
        agent_b = self.name(b)["agent"]
        self.stop(b)
        again = self.start(folder=second)
        self.assertEqual(self.name(again)["agent"], agent_b)

    def test_without_a_terminal_the_folders_agent_comes_back_by_name_beside_another(self):
        a, b = self.start(), self.start()
        self.rename(b, "helper")
        agent_a = self.name(a)["agent"]
        self.stop(a)
        a2 = self.start()
        self.assertEqual(self.name(a2)["agent"], agent_a)


    def test_a_second_process_in_a_running_agents_pane_doesnt_take_the_pane_from_it(self):
        a = self.start("hc:t1@100", pid=os.getpid())
        self.rename(a, "lead")
        agent_a = self.name(a)["agent"]
        nested = self.start("hc:t1@100", pid=os.getpid())  # a second claude in the same shell (ctrl-Z)
        self.assertNotEqual(self.name(nested)["agent"], agent_a)
        self.stop(nested)
        self.stop(a)
        again = self.start("hc:t1@100", pid=os.getpid())
        self.assertEqual(self.name(again)["agent"], agent_a)

    def test_without_a_terminal_a_numbered_name_beside_its_base_still_finds_its_agent(self):
        a, b = self.start(), self.start()  # the folder's name, and name-2
        agent_b = self.name(b)["agent"]
        self.stop(b)
        again = self.start()  # name-2 again, while the first still runs
        self.assertEqual(self.name(again)["agent"], agent_b)
        self.assertNotEqual(self.name(again)["agent"], self.name(a)["agent"])


if __name__ == "__main__":
    unittest.main()
