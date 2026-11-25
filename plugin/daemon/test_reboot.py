"""After a reboot: the list of running agents, the one question to you, resuming only on your yes, and the opt-in
start at login. Run: python3 -m unittest discover -s plugin/daemon -k reboot"""
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PEERLINK_HOME", tempfile.mkdtemp(prefix="peerlink-reboot-test-"))

import peerlinkd as d  # noqa: E402
import reboot  # noqa: E402
from rig import Agent, Rig  # noqa: E402


def read(path):
    with open(path) as f:
        return f.read()


class RunningList(unittest.TestCase):
    NOW = 10_000.0

    def rec(self, sid, agent, seen_ago=1, **kw):
        return {"id": sid, "agent": agent, "name": agent, "cwd": f"/x/{agent}", "last_seen": self.NOW - seen_ago, **kw}

    def test_lists_live_agents_and_those_a_shutdown_may_have_ended(self):
        sessions = {"s1": self.rec("s1", "web"), "s2": self.rec("s2", "api", seen_ago=reboot.GRACE_S + 5)}
        left = {"s3": {**self.rec("s3", "ops"), "left": self.NOW - 20}, "s4": {**self.rec("s4", "old"), "left": self.NOW - reboot.GRACE_S - 1}}
        self.assertEqual([a["name"] for a in reboot.running(sessions, left, self.NOW)], ["ops", "web"])

    def test_leaves_out_terminated_agents_and_mcp_clients(self):
        sessions = {"s1": self.rec("s1", "web", terminate={"by": "you"}), "s2": self.rec("s2", "desk", plugin="peerlink-mcp 1.0"),
                    "s3": self.rec("s3", "api")}
        listed = reboot.running(sessions, {}, self.NOW, client=d.is_client)
        self.assertEqual([a["name"] for a in listed], ["api"])

    def test_one_entry_per_agent_its_newest_session(self):
        sessions = {"old": self.rec("old", "web", seen_ago=60), "new": self.rec("new", "web", seen_ago=2)}
        self.assertEqual([a["sid"] for a in reboot.running(sessions, {}, self.NOW)], ["new"])

    def test_asks_only_after_a_reboot_and_not_about_a_stale_list(self):
        snap = {"boot": 1000, "ts": self.NOW - 60, "agents": [{"sid": "s1", "cwd": "/x/web", "name": "web"}]}
        self.assertEqual(reboot.to_ask(snap, 1000 + reboot.SAME_BOOT_S - 1, self.NOW), [])  # the same boot: a daemon restart
        self.assertEqual([a["sid"] for a in reboot.to_ask(snap, 9000, self.NOW)], ["s1"])
        self.assertEqual(reboot.to_ask({**snap, "ts": self.NOW - reboot.SNAPSHOT_MAX_AGE_S - 1}, 9000, self.NOW), [])
        self.assertEqual(reboot.to_ask(None, 9000, self.NOW), [])


class StartAtLogin(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="peerlink-login-")
        os.environ["PEERLINK_TEST_AUTOSTART_HOME"] = self.home
        self.addCleanup(os.environ.pop, "PEERLINK_TEST_AUTOSTART_HOME", None)
        self.kept = d.config.get("start_at_login")
        self.addCleanup(d.config.__setitem__, "start_at_login", self.kept)

    def test_off_by_default(self):
        self.assertIs(d.DEFAULT_CONFIG["start_at_login"], False)
        self.assertIn("start_at_login", d.BROWSER_ONLY_SETTINGS)
        self.assertIn("resume_after_reboot", d.BROWSER_ONLY_SETTINGS)

    def test_turning_it_on_adds_the_entry_and_off_removes_it(self):
        for platform in ("darwin", "linux"):
            argv = ["/usr/bin/python3", "/opt/peerlink/peerlinkd.py", "start"]
            self.assertEqual(reboot.autostart_install(argv, self.home, platform), "")
            self.assertTrue(reboot.autostart_installed(self.home, platform))
            path = reboot._plist_path(self.home) if platform == "darwin" else reboot._desktop_path(self.home)
            text = read(path)
            self.assertIn("/opt/peerlink/peerlinkd.py", text)
            self.assertEqual(reboot.autostart_remove(self.home, platform), "")
            self.assertFalse(reboot.autostart_installed(self.home, platform))
            self.assertEqual(reboot.autostart_remove(self.home, platform), "")  # gone already: still fine

    @unittest.skipUnless(sys.platform in ("darwin", "linux"), "the LaunchAgent and the autostart file")
    def test_the_setting_drives_the_entry(self):
        d.config["start_at_login"] = True
        self.assertEqual(d.sync_autostart(), "")
        self.assertTrue(reboot.autostart_installed(self.home))
        self.assertIn(os.path.abspath(d.__file__), read(reboot._plist_path(self.home) if sys.platform == "darwin" else reboot._desktop_path(self.home)))
        d.config["start_at_login"] = False
        self.assertEqual(d.sync_autostart(), "")
        self.assertFalse(reboot.autostart_installed(self.home))


class TheQuestion(unittest.TestCase):
    def setUp(self):
        self.addCleanup(d.resume_ask.clear)
        d.resume_ask.clear()

    def test_a_resume_starts_claude_with_resume_and_its_id_only(self):
        self.assertEqual(d.launch_args({"resume": "5ad5fb3d-280a", "model": "opus", "effort": "max"}), ["--resume", "5ad5fb3d-280a"])

    def test_the_question_names_each_agent_folder_and_session(self):
        ask = {"agents": [{"sid": "5ad5fb3d-1111", "cwd": "/x/web", "name": "web"}, {"sid": "77adf96c-2222", "cwd": "/x/api", "name": ""}]}
        text = d.ask_view(ask)
        for part in ("web in /x/web (session 5ad5fb3d-1111)", "api in /x/api (session 77adf96c-2222)", "2 agents"):
            self.assertIn(part, text)

    def test_no_answer_within_a_day_is_a_no(self):
        d.resume_ask.update({"id": "b1", "created": time.time() - d.SPAWN_TTL_S - 1, "status": "pending", "agents": [{"sid": "s", "cwd": "/x"}]})
        self.assertTrue(d.expire_resume_ask(time.time()))
        self.assertEqual(d.resume_ask["status"], "expired")
        self.assertEqual(d.decide_resume("b1", True, "late")[0], 409)


class AfterARestart(unittest.TestCase):
    """The real daemon on the rig: agents run, the machine restarts (a new boot time and kill -9), you're asked."""

    def rig(self, boot="1000000"):
        self.sink = os.path.join(tempfile.mkdtemp(prefix="peerlink-launch-"), "launched.jsonl")
        rig = Rig(env={"PEERLINK_TEST_BOOT": boot, "PEERLINK_TEST_LAUNCH": self.sink})
        rig.__enter__()
        self.addCleanup(rig.__exit__, None, None, None)
        return rig

    def launched(self):
        try:
            with open(self.sink) as f:
                return [json.loads(line) for line in f]
        except OSError:
            return []

    def reboot(self, rig, boot="2000000"):
        rig.env["PEERLINK_TEST_BOOT"] = boot
        rig.crash()

    def team(self, rig):
        out = {}
        for name in ("web", "api", "done", "shut"):
            a = Agent(rig, name, rig.folder(name))
            a.register()
            out[name] = a
        rig.call("DELETE", f"/api/sessions/{out['done'].sid}?reason=prompt_input_exit")  # /exit: on purpose
        rig.call("DELETE", f"/api/sessions/{out['shut'].sid}?reason=other")  # a signal, as a shutdown sends
        return out

    def question(self, rig):
        return next((i for i in rig.user("GET", "/api/inbox")["items"] if i["id"].startswith("b")), None)

    def test_you_are_asked_once_and_yes_resumes_each_agent(self):
        rig = self.rig()
        team = self.team(rig)
        self.reboot(rig)
        q = self.question(rig)
        self.assertIsNotNone(q, "no question after the reboot")
        for name in ("web", "api", "shut"):
            self.assertIn(f"{name} in {team[name].cwd} (session {team[name].sid})", q["text"])
        self.assertNotIn(team["done"].sid, q["text"])
        self.assertEqual(self.launched(), [], "nothing starts before you answer")
        rig.user("POST", "/api/inbox", {"kind": "approval", "id": q["id"], "action": "approve"})
        rig.wait(lambda: len(self.launched()) == 3, "three agents to start", timeout=15)
        got = sorted((x["dir"], tuple(x["args"])) for x in self.launched())
        self.assertEqual(got, sorted((team[n].cwd, ("--resume", team[n].sid)) for n in ("web", "api", "shut")))
        self.assertIsNone(self.question(rig))
        rig.crash()  # the same boot: a daemon restart doesn't ask again
        self.assertIsNone(self.question(rig))

    def test_only_you_answer_and_no_means_nothing_starts(self):
        rig = self.rig()
        self.team(rig)
        self.reboot(rig)
        q = self.question(rig)
        code, _ = rig.call("POST", "/api/inbox", {"kind": "approval", "id": q["id"], "action": "approve"})  # an agent, with the key
        self.assertEqual(code, 403)
        for setting in ("start_at_login", "resume_after_reboot"):
            self.assertEqual(rig.call("PUT", "/api/config", {setting: True if setting == "start_at_login" else False})[0], 403)
        rig.user("POST", "/api/inbox", {"kind": "approval", "id": q["id"], "action": "deny"})
        time.sleep(1)
        self.assertEqual(self.launched(), [])

    def test_a_daemon_restart_without_a_reboot_asks_nothing(self):
        rig = self.rig()
        self.team(rig)
        rig.crash()
        self.assertIsNone(self.question(rig))


if __name__ == "__main__":
    unittest.main()
