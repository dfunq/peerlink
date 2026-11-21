"""Tests for hiring workers: the worker flag and its limits, retire, the cap and its queue, the night guard, and
starting agents in herdr's terminals. Run: python3 -m unittest discover -s plugin/daemon"""
import copy
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PEERLINK_HOME", tempfile.mkdtemp(prefix="peerlink-hiring-test-"))

import peerlinkd  # noqa: E402

d = peerlinkd


class HiringTest(unittest.TestCase):
    """lead hires workers into folders under /tmp; other is an agent that hired nobody."""

    def setUp(self):
        d.load_config()
        d.load_discord()
        self.kept = copy.deepcopy(({k: d.config.get(k) for k in ("max_live_spawns", "night_guard", "auto_approve_folders")}, d.spawns, d.tasks, d.messages))
        self.addCleanup(self.restore)
        self.dirs = [tempfile.mkdtemp(prefix="hire-") for _ in range(3)]
        d.config.update({"rate_limit_per_min": 100, "auto_approve_folders": ["/x/lead"], "max_live_spawns": 0, "night_guard": ""})
        now = time.time()
        for n in ("lead", "other"):
            d.sessions["s-" + n] = {"id": "s-" + n, "name": n, "cwd": "/x/" + n, "agent": "a-" + n, "last_seen": now}
        d.agent_of.update({"s-lead": "a-lead", "s-other": "a-other"})
        d.spawns.clear()
        d.inboxes.clear()
        launched = self.launched = []

        def fake_launch(sp):
            launched.append(sp["id"])
            return "in a test terminal", ""
        self.trusted, self.real_trust = [], d.trust_folder
        for target, fake in (("launch", fake_launch), ("hard_stop", lambda *a, **k: None), ("trust_folder", self.trusted.append)):
            p = unittest.mock.patch.object(d, target, fake)
            p.start()
            self.addCleanup(p.stop)

    def restore(self):
        conf, spawns, tasks, messages = self.kept
        d.config.update(conf)
        for sid in [s for s in d.sessions if s.startswith("s-")]:
            d.sessions.pop(sid)
        for store, was in ((d.spawns, spawns), (d.tasks, tasks)):
            store.clear()
            store.update(was)
        d.messages[:] = messages
        d.inboxes.clear()

    def hire(self, i=0, **extra):
        status, out = d.request_spawn({"by": "s-lead", "dir": self.dirs[i], "purpose": "one ticket", "first_prompt": "Do the ticket",
                                       "name": f"w{i}", "worker": True, **extra})
        self.assertEqual(status, 200, out)
        return out["id"]

    def register(self, spawn_id, name):
        """The started worker's agent checks in, as the plugin's registration would record it."""
        sid, aid = "s-" + name, "a-" + name
        d.sessions[sid] = {"id": sid, "name": name, "cwd": d.spawns[spawn_id]["dir"], "agent": aid, "last_seen": time.time()}
        d.agent_of[sid] = aid
        d.spawns[spawn_id].update(agent_id=sid, agent=aid)
        return sid

    def told(self, name):
        return [m["text"] for m in d.inboxes.get("s-" + name, [])]

    def test_a_worker_is_started_and_cant_hire_or_schedule(self):
        sp = self.hire()
        self.assertEqual((d.spawns[sp]["status"], d.spawns[sp]["worker"]), ("started", True))
        sid = self.register(sp, "w0")
        refused = d.request_spawn({"by": sid, "dir": self.dirs[1], "purpose": "help", "first_prompt": "go"})
        self.assertEqual(refused[0], 403)
        self.assertIn("ask your lead", refused[1]["error"])
        self.assertEqual(d.create_schedule({"prompt": "check later", "at": "in 1h"}, d.sessions[sid])[0], 403)
        # The block is the agent's: a cleared context, a new session of the same agent, is still a worker.
        d.sessions["s-w0b"] = dict(d.sessions[sid], id="s-w0b")
        self.assertEqual(d.request_spawn({"by": "s-w0b", "dir": self.dirs[1], "purpose": "help", "first_prompt": "go"})[0], 403)
        self.assertEqual(d.client_config(sid)["hired_by"], "lead")
        self.assertEqual(d.client_config("s-lead")["hired_by"], "")

    def test_past_the_cap_a_hire_queues_and_starts_when_a_worker_is_gone(self):
        d.config["max_live_spawns"] = 1
        first = self.hire(0)
        sid = self.register(first, "w0")
        second = self.hire(1)
        self.assertEqual(d.spawns[second]["status"], "queued")
        self.assertIn(f"{second} is queued: 1 of 1 workers", self.told("lead")[-1])
        self.assertEqual(self.launched, [first])
        # A standing agent started the old way isn't capped.
        standing = d.request_spawn({"by": "s-lead", "dir": self.dirs[2], "purpose": "restart a lead", "first_prompt": "go", "name": "lead2"})[1]["id"]
        self.assertEqual(d.spawns[standing]["status"], "started")
        d.start_queued()
        self.assertEqual(d.spawns[second]["status"], "queued")
        d.sessions.pop(sid)  # the first worker retired
        d.start_queued()
        self.assertEqual(d.spawns[second]["status"], "started")
        self.assertIn(f"{second} left the queue", self.told("lead")[-1])

    def test_a_worker_retires_once_its_tasks_are_closed_and_its_lead_hears_how_they_ended(self):
        sp = self.hire()
        sid = self.register(sp, "w0")
        status, out = d.send({"from": "s-lead", "to": "w0", "kind": "task", "text": "Fix the login copy"})
        self.assertEqual(status, 200, out)
        tid = out["task"]
        status, out = d.retire(sid, {})
        self.assertEqual(status, 409)
        self.assertIn(f"you still own {tid}", out["error"])
        self.assertEqual(d.update_task(tid, {"by": sid, "status": "done", "note": "Fixed."})[0], 200)
        with unittest.mock.patch.object(d, "terminate", wraps=d.terminate) as ended:
            self.assertEqual(d.retire(sid, {})[0], 200)
            ended.assert_called_once()
        self.assertEqual(d.spawns[sp]["status"], "retired")
        self.assertIn(f"w0 retired, {tid} done.", self.told("lead"))

    def test_only_the_lead_that_hired_a_worker_retires_it_and_a_standing_agent_never_is(self):
        sp = self.hire()
        sid = self.register(sp, "w0")
        self.assertEqual(d.retire("s-other", {"agent": "w0"})[0], 403)
        status, out = d.retire("s-lead", {})
        self.assertEqual(status, 403)
        self.assertIn("you stay running", out["error"])
        status, out = d.retire("s-lead", {"agent": "w0"})
        self.assertEqual(status, 200, out)
        self.assertTrue(d.sessions[sid].get("terminate"))

    def repo(self):
        root = tempfile.mkdtemp(prefix="wt-")
        repo = os.path.join(root, "app")
        os.mkdir(repo)
        for args in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "start"]):
            d.subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)
        return repo

    def test_a_worker_starts_in_a_new_worktree_made_when_it_starts_and_kept_when_it_retires(self):
        repo = self.repo()
        sp = self.hire(dir="", worktree={"repo": repo, "branch": "fix/login-copy", "base": "main"})
        path = repo + "-wt-fix-login-copy"
        self.assertEqual(d.spawns[sp]["dir"], os.path.realpath(path) if os.path.realpath(repo) != repo else path)
        wt = d.spawns[sp]["worktree"]
        self.assertTrue(wt["created"] and os.path.isdir(wt["path"]))
        self.assertEqual(d.git(wt["path"], "rev-parse", "--abbrev-ref", "HEAD"), (0, "fix/login-copy"))
        self.assertEqual(self.trusted, [wt["path"]])
        # A lead-made link with another agent ends when it retires; its worktree stays, and the lead is told how to remove it.
        sid = self.register(sp, "w0")
        d.config.setdefault("agent_links", []).append(["a-other", "a-w0"])
        d.config.setdefault("link_info", {})["a-other|a-w0"] = {"by": ["lead"], "why": "", "task": "", "ts": 0}
        self.assertEqual(d.retire(sid, {})[0], 200)
        self.assertNotIn(["a-other", "a-w0"], d.config["agent_links"])
        self.assertTrue(os.path.isdir(wt["path"]))
        self.assertIn(f"worktree remove", self.told("lead")[-1])
        # An existing branch is checked out rather than made again; a folder in the way, or no repo, is refused up front.
        d.git(repo, "branch", "fix/other")
        other = self.hire(1, dir="", worktree={"repo": repo, "branch": "fix/other"})
        self.assertEqual(d.git(d.spawns[other]["dir"], "rev-parse", "--abbrev-ref", "HEAD"), (0, "fix/other"))
        busy = tempfile.mkdtemp()
        status, out = d.request_spawn({"by": "s-lead", "dir": busy, "purpose": "p", "first_prompt": "f", "worktree": {"repo": repo, "branch": "x"}})
        self.assertEqual(status, 400)
        self.assertIn("isn't a worktree on x", out["error"])
        self.assertEqual(d.request_spawn({"by": "s-lead", "purpose": "p", "first_prompt": "f", "worktree": {"repo": busy, "branch": "x"}})[0], 400)

    def test_a_folder_is_marked_trusted_only_when_nothing_above_it_is(self):
        home = tempfile.mkdtemp()
        conf = os.path.join(home, ".claude.json")
        with open(conf, "w") as f:
            json.dump({"projects": {os.path.join(home, "Repos"): {"hasTrustDialogAccepted": True}}, "other": 1}, f)
        with unittest.mock.patch.dict(os.environ, {"HOME": home}):
            self.real_trust(os.path.join(home, "Repos", "app-wt-x"))
            with open(conf) as f:
                self.assertEqual(len(json.load(f)["projects"]), 1)  # under a trusted folder already
            self.real_trust(os.path.join(home, "work", "app-wt-y"))
            with open(conf) as f:
                data = json.load(f)
        self.assertTrue(data["projects"][os.path.join(home, "work", "app-wt-y")]["hasTrustDialogAccepted"])
        self.assertEqual(data["other"], 1)

    def test_the_night_guard_holds_its_hours_and_refuses_hires_in_them(self):
        at = lambda h: time.mktime((2026, 11, 21, h, 30, 0, 0, 0, -1))
        d.config["night_guard"] = "22-8"
        self.assertEqual([d.night_guard_on(at(h)) for h in (21, 22, 3, 8)], [False, True, True, False])
        d.config["night_guard"] = "1-5"
        self.assertEqual([d.night_guard_on(at(h)) for h in (0, 1, 4, 5)], [False, True, True, False])
        with unittest.mock.patch.object(d, "night_guard_on", lambda now=None: True):
            status, out = d.request_spawn({"by": "s-lead", "dir": self.dirs[0], "purpose": "p", "first_prompt": "f", "worker": True})
        self.assertEqual(status, 409)
        self.assertIn("night guard", out["error"])
        self.assertEqual(d.config_value("night_guard", "25-3")[0], None)
        self.assertEqual(d.config_value("night_guard", " 22-8 "), ("22-8", None))
        self.assertIn("max_live_spawns", d.BROWSER_ONLY_SETTINGS)


class HerdrLaunchTest(unittest.TestCase):
    def test_an_agent_starts_in_a_herdr_terminal_that_outlives_the_daemons(self):
        sp = {"id": "s9", "dir": "/x/api", "name": "pg-client", "space": "pixelgrid"}
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            out = {"address": "t42"} if argv[1] == "run" else {"state": "live", "pid": 10, "foreground": {"pid": 11}}
            return unittest.mock.Mock(returncode=0, stdout=json.dumps(out), stderr="")
        with unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), \
                unittest.mock.patch.object(d.subprocess, "run", run):
            how, err = d.launch(sp)
            self.assertEqual((how, err, sp["hc"]), ("in the herdr app (hc attach t42)", "", "t42"))
            self.assertEqual(d.hc_pid("t42"), 11)
        argv = calls[0]
        for flag in ("--bg", "--no-parent", "--outlive-parent", "--rm"):
            self.assertIn(flag, argv)
        self.assertNotIn("--keep", argv)
        self.assertIn("peerlink.spawn=s9", argv)
        self.assertEqual(argv[argv.index("--cwd") + 1], "/x/api")
        self.assertEqual(argv[-1], "/bin/claude")

    def test_when_hc_fails_it_falls_back_to_tmux(self):
        sp = {"id": "s9", "dir": "/x/api", "name": "", "space": ""}
        seen = []

        def run(argv, **kw):
            seen.append(os.path.basename(argv[0]))
            if argv[0] == "/bin/hc":
                return unittest.mock.Mock(returncode=1, stdout="", stderr="no server")
            return unittest.mock.Mock(returncode=1 if "has-session" in argv else 0, stdout="", stderr="")
        with unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), \
                unittest.mock.patch.object(d.shutil, "which", lambda n: "/bin/tmux" if n == "tmux" else None), \
                unittest.mock.patch.object(d.subprocess, "run", run):
            how, err = d.launch(sp)
        self.assertEqual(err, "")
        self.assertTrue(how.startswith("in tmux"), how)
        self.assertEqual(seen[0], "hc")

    def test_with_herdr_turned_off_hc_is_never_tried(self):
        sp = {"id": "s9", "dir": "/x/api", "name": "", "space": ""}
        seen = []

        def run(argv, **kw):
            seen.append(os.path.basename(argv[0]))
            return unittest.mock.Mock(returncode=1 if "has-session" in argv else 0, stdout="", stderr="")
        with unittest.mock.patch.dict(d.config, {"use_herdr": False}), unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), \
                unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), \
                unittest.mock.patch.object(d.shutil, "which", lambda n: "/bin/tmux" if n == "tmux" else None), \
                unittest.mock.patch.object(d.subprocess, "run", run):
            how, err = d.launch(sp)
        self.assertTrue(how.startswith("in tmux"), how)
        self.assertNotIn("hc", seen)


if __name__ == "__main__":
    unittest.main()
