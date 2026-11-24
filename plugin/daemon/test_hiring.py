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
        self.assertEqual(os.path.normcase(os.path.realpath(d.spawns[sp]["dir"])), os.path.normcase(os.path.realpath(path)))
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

    def test_dir_naming_the_repo_puts_the_worktree_beside_it(self):
        repo = self.repo()
        sp = self.hire(dir=repo, worktree={"repo": repo, "branch": "fix-relay"})
        self.assertEqual(os.path.basename(d.spawns[sp]["dir"]), "app-wt-fix-relay")
        # The repo can come from dir alone.
        other = self.hire(1, dir=repo, worktree={"branch": "fix-copy"})
        self.assertEqual(d.git(d.spawns[other]["dir"], "rev-parse", "--abbrev-ref", "HEAD"), (0, "fix-copy"))

    def test_a_session_reloaded_onto_newer_tools_is_told_what_they_take_once(self):
        rec = d.sessions["s-lead"]
        rec["plugin"] = "0.9.65"
        with d.lock:
            d.tool_changes_notice(rec, "0.9.65", "0.9.67")
            d.tool_changes_notice(rec, "0.9.66", "0.9.67")
            d.tool_changes_notice(rec, "", "0.9.67")
        told = [m for m in d.inboxes.get("s-lead", []) if m["from"] == "peerlink"]
        self.assertEqual(len(told), 1)
        self.assertEqual(told[0]["kind"], "announcement")  # read at its next turn, never starts one
        self.assertIn("worktree", told[0]["text"])

    def test_a_worktree_beside_its_repo_sits_in_its_repos_space(self):
        repo = self.repo()
        for folder, text in ((os.path.dirname(repo), "space: hq\n"), (repo, "space: shopfront\nparent: hq\nrole: the lead\n")):
            with open(os.path.join(folder, ".peerlink"), "w") as f:
                f.write(text)
        wt = repo + "-wt-fix"
        d.git(repo, "worktree", "add", "-q", "-b", "fix", wt)
        self.assertEqual(os.path.realpath(d.find_space_file(wt)), os.path.realpath(os.path.join(repo, ".peerlink")))
        rec = {"id": "s-x", "cwd": wt}
        self.assertEqual(d.space_of(rec)[:2], ("shopfront", "file"))
        self.assertEqual(d.folder_role(rec), "")  # the lead's role stays the lead's
        # A plain folder beside the repo still finds the one above it.
        plain = repo + "-notes"
        os.mkdir(plain)
        self.assertEqual(d.find_space_file(plain), os.path.join(os.path.dirname(repo), ".peerlink"))

    def test_a_hire_says_its_model_and_effort_and_gets_them(self):
        status, out = d.request_spawn({"by": "s-lead", "dir": self.dirs[0], "purpose": "p", "first_prompt": "f",
                                        "model": "opus", "effort": "high", "worker": True})
        self.assertEqual((status, out["model"], out["effort"]), (200, "opus", "high"))
        self.assertIn("--model opus --effort high", out["note"])
        self.assertEqual(d.launch_args(d.spawns[out["id"]]), ["--model", "opus", "--effort", "high"])
        for bad in ({"effort": "huge"}, {"model": "opus; rm -rf ~"}):
            status, out = d.request_spawn({"by": "s-lead", "dir": self.dirs[1], "purpose": "p", "first_prompt": "f", **bad})
            self.assertEqual(status, 400, bad)

    def test_a_folder_is_marked_trusted_only_when_nothing_above_it_is(self):
        home = tempfile.mkdtemp()
        conf = os.path.join(home, ".claude.json")
        with open(conf, "w") as f:
            json.dump({"projects": {os.path.join(home, "Repos"): {"hasTrustDialogAccepted": True}}, "other": 1}, f)
        with unittest.mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            self.real_trust(os.path.join(home, "Repos", "app-wt-x"))
            with open(conf) as f:
                self.assertEqual(len(json.load(f)["projects"]), 1)  # under a trusted folder already
            self.real_trust(os.path.join(home, "work", "app-wt-y"))
            with open(conf) as f:
                data = json.load(f)
        self.assertTrue(data["projects"][d.claude_project_key(os.path.join(home, "work", "app-wt-y"))]["hasTrustDialogAccepted"])
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
    def setUp(self):
        # No login shell to ask about an alias, unless a test says otherwise; no herdr app to name tabs.
        for target, fake in (("user_shell", lambda: ""), ("name_herdr_tab", lambda *a: None)):
            p = unittest.mock.patch.object(d, target, fake)
            p.start()
            self.addCleanup(p.stop)

    def test_claude_starts_through_your_shell_when_you_have_an_alias_for_it(self):
        def run_with(out):
            return lambda argv, **kw: unittest.mock.Mock(returncode=0, stdout=out, stderr="")
        with unittest.mock.patch.object(d, "user_shell", lambda: "/bin/zsh"), unittest.mock.patch.dict(os.environ, {"PEERLINK_CLAUDE": ""}):
            with unittest.mock.patch.object(d.subprocess, "run", run_with("claude: alias\n")):
                self.assertEqual(d.claude_argv("/bin/claude"), ["/bin/zsh", "-lic", "claude"])
                self.assertEqual(d.claude_argv("/bin/claude", ["--model", "opus"]), ["/bin/zsh", "-lic", "claude --model opus"])
            with unittest.mock.patch.object(d.subprocess, "run", run_with("claude: command\n")):
                self.assertEqual(d.claude_argv("/bin/claude"), ["/bin/claude"])
                self.assertEqual(d.claude_argv("/bin/claude", ["--effort", "low"]), ["/bin/claude", "--effort", "low"])

    def test_on_windows_claude_starts_through_powershell_when_your_profile_defines_it(self):
        def run_with(outs):
            return lambda argv, **kw: unittest.mock.Mock(returncode=0, stdout=outs[os.path.basename(argv[0])], stderr="")
        which = {"pwsh": "C:/ps7/pwsh.exe", "powershell": "C:/ps5/powershell.exe"}
        with unittest.mock.patch.object(d, "IS_WINDOWS", True), unittest.mock.patch.dict(os.environ, {"PEERLINK_CLAUDE": ""}), \
                unittest.mock.patch.object(d.shutil, "which", lambda n: which.get(n)):
            # Only Windows PowerShell's profile has it: that one starts it, with the spawn's flags quoted.
            with unittest.mock.patch.object(d.subprocess, "run", run_with({"pwsh.exe": "Application\n", "powershell.exe": "Function\r\n"})):
                self.assertEqual(d.claude_argv("C:/bin/claude.exe", ["--model", "opus[1m]"]),
                                 ["C:/ps5/powershell.exe", "-NoLogo", "-Command", "claude '--model' 'opus[1m]'"])
            with unittest.mock.patch.object(d.subprocess, "run", run_with({"pwsh.exe": "profile says hi\nAlias\n", "powershell.exe": ""})):
                self.assertEqual(d.claude_argv("C:/bin/claude.exe"), ["C:/ps7/pwsh.exe", "-NoLogo", "-Command", "claude"])
            with unittest.mock.patch.object(d.subprocess, "run", run_with({"pwsh.exe": "Application\n", "powershell.exe": "Application\n"})):
                self.assertEqual(d.claude_argv("C:/bin/claude.exe", ["--effort", "low"]), ["C:/bin/claude.exe", "--effort", "low"])
        self.assertEqual(d.ps_quote("it's"), "'it''s'")

    def test_an_agent_starts_in_a_herdr_terminal_that_outlives_the_daemons(self):
        sp = {"id": "s9", "dir": "/x/api", "name": "shop-client", "space": "shopfront"}
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

    def test_a_hire_opens_in_its_leads_herdr_app_space(self):
        d.sessions["s-lead9"] = {"id": "s-lead9", "name": "lead9", "cwd": "/x/lead9", "pid": 77, "last_seen": time.time()}
        self.addCleanup(d.sessions.pop, "s-lead9", None)
        sp = {"id": "s9", "dir": "/x/api", "name": "shop-client", "space": "shopfront", "requester": {"id": "s-lead9", "cwd": "/x/lead9"}}
        terms = {"t1": {"cwd": "/x/lead9", "labels": {"herdr.space": "sp-other"}, "foreground": {"pid": 5}},
                 "t2": {"cwd": "/x/lead9", "labels": {"herdr.space": "sp-lead"}, "foreground": {"pid": 77}}}
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if argv[1] == "ls":
                out = {"terminals": [{"address": a, "cwd": t["cwd"], "state": "live"} for a, t in terms.items()]}
            elif argv[1] == "get":
                out = {"state": "live", **terms.get(argv[2], {})}
            else:
                out = {"address": "t42"}
            return unittest.mock.Mock(returncode=0, stdout=json.dumps(out), stderr="")
        with unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), \
                unittest.mock.patch.object(d.subprocess, "run", run):
            self.assertEqual(d.launch(sp)[1], "")
        argv = next(a for a in calls if a[1] == "run")
        self.assertIn("herdr.space=sp-lead", argv)
        self.assertTrue(any(a.startswith("herdr.tab=tb") for a in argv))  # its own tab, named after it
        self.assertEqual(argv[-1], "/bin/claude")

    def test_a_worker_nests_under_its_leads_terminal(self):
        d.sessions["s-lead9"] = {"id": "s-lead9", "name": "lead9", "cwd": "/x/lead9", "pid": 77, "terminal": "hc:t2@50",
                                 "last_seen": time.time()}
        self.addCleanup(d.sessions.pop, "s-lead9", None)
        for worker, lead_pid, parented in ((True, 50, True), (True, 51, False), (False, 50, False)):
            with self.subTest(worker=worker, lead_pid=lead_pid):
                sp = {"id": "s9", "dir": "/x/api", "name": "shop-client", "space": "shopfront", "worker": worker,
                      "requester": {"id": "s-lead9", "cwd": "/x/lead9"}}
                calls = []

                def run(argv, **kw):
                    calls.append(argv)
                    out = {"terminals": []} if argv[1] == "ls" else {"state": "live", "pid": lead_pid} if argv[1] == "get" else {"address": "t42"}
                    return unittest.mock.Mock(returncode=0, stdout=json.dumps(out), stderr="")
                with unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), \
                        unittest.mock.patch.object(d.subprocess, "run", run):
                    self.assertEqual(d.launch(sp)[1], "")
                argv = next(a for a in calls if a[1] == "run")
                self.assertIn("--outlive-parent", argv)
                if parented:  # its lead's terminal still runs the lead's shell: nested under it, quietly
                    self.assertEqual(argv[argv.index("--parent") + 1], "t2")
                    self.assertIn("--no-notify", argv)
                    self.assertNotIn("--no-parent", argv)
                else:  # not a worker, or the address now belongs to another shell
                    self.assertIn("--no-parent", argv)
                    self.assertNotIn("--parent", argv)

    def test_any_agent_but_the_lead_nests_under_its_spaces_lead(self):
        d.sessions["s-lead9"] = {"id": "s-lead9", "name": "lead9", "cwd": "/x/lead9", "pid": 77, "terminal": "hc:t2@50", "last_seen": time.time()}
        d.sessions["s-boss"] = {"id": "s-boss", "name": "boss", "cwd": "/x/boss", "pid": 78, "terminal": "hc:t5@55", "last_seen": time.time()}
        for sid in ("s-lead9", "s-boss"):
            self.addCleanup(d.sessions.pop, sid, None)
        leads = {"shopfront": {"cwd": "/x/lead9", "how": "set"}}
        pids = {"t2": 50, "t5": 55}
        cases = (("a standing agent in the space", {"dir": "/x/api", "space": "shopfront", "requester": {"id": "s-boss"}}, "t2"),
                 ("the lead restarting", {"dir": "/x/lead9", "space": "shopfront", "requester": {"id": "s-boss"}}, ""),
                 ("a worker in a space with no lead running", {"dir": "/x/api", "space": "games", "worker": True, "requester": {"id": "s-boss"}}, "t5"),
                 ("an agent in no space", {"dir": "/x/api", "space": "", "requester": {}}, ""))
        for why, extra, parent in cases:
            with self.subTest(why), unittest.mock.patch.dict(d.config, {"space_leads": leads}):
                sp = {"id": "s9", "name": "shop-client", **extra}
                calls = []

                def run(argv, **kw):
                    calls.append(argv)
                    out = {"terminals": []} if argv[1] == "ls" else {"state": "live", "pid": pids.get(argv[2], 0)} if argv[1] == "get" else {"address": "t42"}
                    return unittest.mock.Mock(returncode=0, stdout=json.dumps(out), stderr="")
                with unittest.mock.patch.object(d, "find_claude", lambda: "/bin/claude"), unittest.mock.patch.object(d, "find_hc", lambda: "/bin/hc"), \
                        unittest.mock.patch.object(d.subprocess, "run", run), unittest.mock.patch.object(d, "space_of", lambda r: ("hq", "", "")), \
                        unittest.mock.patch.object(d, "lead_rec", lambda space: d.sessions["s-lead9"] if space == "shopfront" else None):
                    self.assertEqual(d.launch(sp)[1], "")
                argv = next(a for a in calls if a[1] == "run")
                self.assertEqual(argv[argv.index("--parent") + 1] if "--parent" in argv else "", parent)

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
