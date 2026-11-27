"""Tests for set_goal: who may run /goal in another agent, the goal kept to one line, and the log of who set it.
Run: python3 -m unittest discover -s plugin/daemon -k test_goal"""
import copy
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PEERLINK_HOME", tempfile.mkdtemp(prefix="peerlink-goal-test-"))

import peerlinkd  # noqa: E402

d = peerlinkd


class GoalTest(unittest.TestCase):
    """In the acme space: lead leads it, dev and peer are members, hirer hired worker. outsider sits in beta."""

    def setUp(self):
        d.load_config()
        self.kept = copy.deepcopy(({k: d.config.get(k) for k in ("space_leads", "agent_spaces", "agent_links")}, d.spawns, d.messages))
        self.addCleanup(self.restore)
        now = time.time()
        spaces = {"lead": "acme", "dev": "acme", "peer": "acme", "hirer": "acme", "worker": "acme", "outsider": "beta"}
        for n, space in spaces.items():
            d.sessions["s-" + n] = {"id": "s-" + n, "name": n, "cwd": "/x/" + n, "agent": "a-" + n, "last_seen": now}
            d.agent_of["s-" + n] = "a-" + n
        d.config["agent_spaces"] = {"a-" + n: space for n, space in spaces.items()}
        d.config["space_leads"] = {"acme": {"cwd": "/x/lead", "how": "set", "agent": "a-lead"}}
        d.spawns.clear()
        d.spawns["sp1"] = {"id": "sp1", "worker": True, "agent_id": "s-worker", "agent": "a-worker", "status": "started",
                           "created": now, "updated": now, "dir": "/x/worker",
                           "requester": {"id": "s-hirer", "agent": "a-hirer", "name": "hirer", "cwd": "/x/hirer"}}

    def restore(self):
        conf, spawns, messages = self.kept
        d.config.update(conf)
        for sid in [s for s in d.sessions if s.startswith("s-")]:
            d.sessions.pop(sid)
        d.spawns.clear()
        d.spawns.update(spawns)
        d.messages[:] = messages

    def goal(self, by, agent, text="ship the fix"):
        return d.set_goal("s-" + by, {"agent": agent, "goal": text})

    def runs(self, name):
        return d.sessions["s-" + name].get("run", [])

    def test_the_space_lead_sets_a_members_goal_and_it_is_logged_as_from_the_lead(self):
        status, out = self.goal("lead", "dev")
        self.assertEqual(status, 200, out)
        self.assertEqual(self.runs("dev"), [{"command": "goal", "args": "ship the fix", "by": "lead", "by_agent": "a-lead"}])
        logged = d.messages[-1]
        self.assertEqual(logged["from_agent"], "a-lead")
        self.assertEqual((logged["id"], logged["kind"], logged["from_name"], logged["to_name"], logged["text"]),
                         (out["id"], "goal", "lead", "dev", "/goal ship the fix"))
        self.assertNotIn(logged, d.inboxes.get("s-dev", []))  # logged, not a message that starts a turn of its own

    def test_an_agent_sets_its_own_goal(self):
        for agent in ("", "worker", "s-worker"):
            with self.subTest(agent=agent):
                d.sessions["s-worker"].pop("run", None)
                status, out = self.goal("worker", agent)
                self.assertEqual(status, 200, out)
                self.assertEqual(self.runs("worker")[0]["command"], "goal")

    def test_the_lead_that_hired_a_worker_sets_its_goal(self):
        status, out = self.goal("hirer", "worker")
        self.assertEqual(status, 200, out)
        self.assertEqual(self.runs("worker")[0]["by"], "hirer")

    def test_a_member_who_doesnt_lead_the_target_is_refused(self):
        for by, target in (("peer", "dev"), ("peer", "worker"), ("dev", "lead"), ("worker", "hirer"), ("worker", "lead")):
            with self.subTest(by=by, target=target):
                status, out = self.goal(by, target)
                self.assertEqual(status, 403, out)
                self.assertIn("lead", out["error"])
                self.assertEqual(self.runs(target), [])

    def test_an_agent_in_another_space_is_refused(self):
        status, out = self.goal("outsider", "dev")
        self.assertEqual(status, 404, out)
        self.assertIn("can't reach", out["error"])
        # Leading its own space doesn't make it the lead of anyone in acme.
        d.config["space_leads"]["beta"] = {"cwd": "/x/outsider", "how": "set", "agent": "a-outsider"}
        d.sessions["s-dev"].pop("_space", None)
        status, out = self.goal("outsider", "dev")
        self.assertIn(status, (403, 404), out)
        self.assertEqual(self.runs("dev"), [])

    def test_a_newline_or_control_character_cant_start_a_second_command(self):
        for text in ("ship the fix\n/exit", "ship the fix\r\n/logout", "ship the fix\x1b[2K\x00/clear", "ship the fix /exit",
                     "ship the fix\u0085/exit", "  ship\tthe   fix​ /exit  "):
            with self.subTest(text=repr(text)):
                d.sessions["s-dev"].pop("run", None)
                status, out = self.goal("lead", "dev", text)
                self.assertEqual(status, 200, out)
                [run] = self.runs("dev")
                self.assertEqual(run["command"], "goal")
                self.assertRegex(run["args"], r"^ship the fix [^\n]*/\w+$")
                self.assertFalse(any(ord(c) < 32 or 0x7f <= ord(c) < 0xa0 or c in "  ​" for c in run["args"]))

    def test_an_empty_or_overlong_goal_is_refused_not_cut(self):
        for text, code in (("", 400), (" \n\t ", 400), (None, 400), ("x" * (d.GOAL_MAX_CHARS + 1), 413)):
            with self.subTest(code=code):
                status, out = self.goal("lead", "dev", text)
                self.assertEqual(status, code, out)
        self.assertEqual(self.runs("dev"), [])
        self.assertEqual(self.goal("lead", "dev", "x" * d.GOAL_MAX_CHARS)[0], 200)

    def test_a_busy_agents_goal_waits_for_its_turn_to_end(self):
        d.sessions["s-dev"]["busy"] = True
        status, out = self.goal("lead", "dev")
        self.assertEqual(status, 200, out)
        self.assertTrue(out["queued"])
        self.assertIn("after its current turn", out["note"])
        self.assertEqual(len(self.runs("dev")), 1)  # handed out on its next heartbeat, and the plugin runs it when idle

    def test_only_goal_is_ever_sent(self):
        status, out = d.set_goal("s-lead", {"agent": "dev", "goal": "ship", "command": "exit"})
        self.assertEqual(status, 200, out)
        self.assertEqual([r["command"] for r in self.runs("dev")], ["goal"])

    def voice(self, space="acme"):
        """An outside MCP client (peerlink_mcp.py, a voice bot) registered as a session, the way it really registers."""
        d.sessions["s-voice"] = {"id": "s-voice", "name": "voice", "cwd": "/x/voice", "agent": "a-voice", "last_seen": time.time(),
                                 "plugin": "peerlink-mcp 0.9.114"}
        d.agent_of["s-voice"] = "a-voice"
        d.config["agent_spaces"]["a-voice"] = space
        self.assertTrue(d.is_client(d.sessions["s-voice"]))

    def test_an_outside_mcp_client_is_refused_whatever_its_role_or_reach(self):
        self.voice()
        cases = {
            "member": lambda: None,
            "space lead": lambda: d.config["space_leads"].__setitem__("acme", {"cwd": "/x/voice", "how": "set", "agent": "a-voice"}),
            "hirer": lambda: d.spawns["sp1"].__setitem__("requester", {"id": "s-voice", "agent": "a-voice", "name": "voice", "cwd": "/x/voice"}),
            "linked from another space": lambda: (d.config["agent_spaces"].__setitem__("a-voice", "beta"),
                                                  d.config.__setitem__("agent_links", [["a-voice", "a-dev"]])),
        }
        for role, grant in cases.items():
            with self.subTest(role=role):
                self.setUp()
                self.voice()
                grant()
                for s in d.sessions.values():
                    s.pop("_space", None)
                if role in ("space lead", "hirer"):  # the role really holds: is_boss alone would let it through
                    self.assertTrue(d.is_boss(d.sessions["s-voice"], d.sessions["s-worker" if role == "hirer" else "s-dev"]))
                if role == "linked from another space":  # the link really gives it reach
                    self.assertIs(d.resolve("dev", d.reach(d.sessions["s-voice"]), d.sessions["s-voice"])[0], d.sessions["s-dev"])
                for target in ("dev", "worker", "voice", ""):
                    status, out = self.goal("voice", target)
                    self.assertEqual(status, 403, out)
                    self.assertIn("outside MCP client", out["error"])
                self.assertEqual((self.runs("dev"), self.runs("worker"), self.runs("voice")), ([], [], []))
                self.assertFalse(any(m.get("kind") == "goal" for m in d.messages))

    def test_an_outside_mcp_client_is_never_given_a_goal(self):
        self.voice()
        status, out = self.goal("lead", "voice")
        self.assertEqual(status, 400, out)
        self.assertEqual(self.runs("voice"), [])

    def test_an_agents_channel_client_has_no_way_to_set_a_goal(self):
        # It talks over the Agents channel's own MCP server, whose tools are posting and reading only, and it has no
        # session: set_goal looks the caller up by session, so its name or channel id is an unknown caller.
        import agentslink
        names = {t["name"] for t in agentslink.TOOLS} | {agentslink.POST_TO_CLIENT["name"]}
        self.assertFalse(any("goal" in n or "run" in n for n in names), names)
        for caller in ("grok-x", "agents:grok-x"):
            self.assertEqual(d.set_goal(caller, {"agent": "dev", "goal": "x"})[0], 404)
        self.assertEqual(self.runs("dev"), [])

    def test_a_target_that_ended_after_lookup_is_refused(self):
        real = d.resolve
        def ended(*a, **k):
            rec, err = real(*a, **k)
            rec["last_seen"] = 0  # it drops between the lookup and the lock
            return rec, err
        d.resolve = ended
        self.addCleanup(setattr, d, "resolve", real)
        status, out = self.goal("lead", "dev")
        self.assertEqual(status, 404, out)
        self.assertIn("isn't running", out["error"])
        self.assertEqual(self.runs("dev"), [])

    def test_an_unknown_caller_or_target_is_refused(self):
        self.assertEqual(d.set_goal("s-nobody", {"agent": "dev", "goal": "x"})[0], 404)
        self.assertEqual(self.goal("lead", "ghost")[0], 404)


if __name__ == "__main__":
    unittest.main()
