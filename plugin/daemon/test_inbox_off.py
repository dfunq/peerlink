"""The Inbox setting (inbox_enabled): on, what agents send you waits in the web UI's Inbox as before; off, it goes to
a chat channel, or the agent is told to ask you in its own terminal, and nothing is kept where you won't look.
Approvals still wait for you either way: never lost, never approved without you.
Run: python3 -m unittest discover -s plugin/daemon"""
import os
import sys
import tempfile
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PEERLINK_HOME", tempfile.mkdtemp(prefix="peerlink-inbox-off-test-"))

import discordlink  # noqa: E402
import peerlinkd  # noqa: E402
from rig import Rig  # noqa: E402


class FakeDiscord:
    def __init__(self):
        self.calls, self.n = [], 0

    def __call__(self, token, method, path, body=None, retried=False, files=None):
        self.calls.append((method, path, body))
        if method == "POST" and path.endswith("/messages"):
            self.n += 1
            return {"id": str(1000 + self.n)}
        return None

    def posts(self, channel):
        return [b for m, p, b in self.calls if m == "POST" and p == f"/channels/{channel}/messages"]


class InboxSettingTest(unittest.TestCase):
    def setUp(self):
        peerlinkd.load_config(save=False)
        peerlinkd.load_discord()
        peerlinkd.config.update({"rate_limit_per_min": 1000})
        now = peerlinkd.time.time()
        self.work = tempfile.mkdtemp(prefix="peerlink-inbox-off-work-")
        self.dirs = {n: os.path.join(self.work, n) for n in ("web", "quiet")}
        for name, cwd in self.dirs.items():
            os.makedirs(cwd, exist_ok=True)
            peerlinkd.sessions["s-" + name] = {"id": "s-" + name, "name": name, "cwd": cwd, "last_seen": now}
        self.keep = (list(peerlinkd.messages), dict(peerlinkd.tasks), dict(peerlinkd.spawns), dict(peerlinkd.rules), peerlinkd.next_msg[0])
        peerlinkd.messages.clear()
        self.fake = FakeDiscord()
        self.real_call, discordlink.call = discordlink.call, self.fake

    def tearDown(self):
        for name in self.dirs:
            peerlinkd.sessions.pop("s-" + name, None)
        peerlinkd.inboxes.clear()
        peerlinkd.messages[:] = self.keep[0]
        for store, was in zip((peerlinkd.tasks, peerlinkd.spawns, peerlinkd.rules), self.keep[1:4]):
            store.clear()
            store.update(was)
        peerlinkd.next_msg[0] = self.keep[4]
        discordlink.call = self.real_call
        peerlinkd.discord.update({"token": "", "routes": []})
        peerlinkd.discord_bot[0] = None
        peerlinkd.load_config(save=False)

    def chat(self):
        """A Discord bot, with channel 77 mapped to the web agent's folder; the quiet agent has no channel."""
        peerlinkd.discord.update({"token": "tok", "enabled": True, "routes": [peerlinkd.clean_route(
            {"channel_id": "77", "agents": [{"cwd": self.dirs["web"], "receives": True, "sends": True}]})]})
        peerlinkd.discord_bot[0] = type("Bot", (), {"reads_messages": True, "channel_name": lambda self, c: "team"})()

    def off(self):
        peerlinkd.config["inbox_enabled"] = False

    def send(self, frm, **body):
        return peerlinkd.send({"from": "s-" + frm, "to": "user", **body})

    # The setting itself
    def test_it_is_on_by_default_and_only_you_change_it(self):
        self.assertIs(peerlinkd.DEFAULT_CONFIG["inbox_enabled"], True)
        self.assertTrue(peerlinkd.inbox_on())
        self.assertIn("inbox_enabled", peerlinkd.BROWSER_ONLY_SETTINGS)
        self.assertEqual(peerlinkd.config_value("inbox_enabled", "off"), (False, None))

    def test_the_peer_prompt_says_where_a_message_to_you_goes(self):
        on = peerlinkd.default_guidance()
        self.assertIn("It waits in their Inbox", on)
        self.off()
        off = peerlinkd.default_guidance()
        self.assertNotIn("Inbox", off)
        self.assertIn("ask them in your own reply in this terminal", off)
        self.assertIn("your chat channel", off)

    # On: as before
    def test_on_a_question_without_a_channel_waits_in_the_inbox(self):
        code, out = self.send("quiet", text="Which region?", expects_reply=True)
        self.assertEqual((code, out["status"]), (200, "waiting in their Inbox"))
        self.assertEqual([i["id"] for i in peerlinkd.inbox_items() if i["kind"] == "question"], [out["id"]])
        code, out = self.send("quiet", text="Log in to the registry", kind="task")
        self.assertEqual(code, 200)
        self.assertTrue(peerlinkd.tasks[out["task"]]["user"])

    def test_on_a_plain_message_from_an_agent_without_its_own_channel_is_kept_not_posted(self):
        self.chat()
        code, out = self.send("quiet", text="FYI: deploy finished")
        self.assertEqual((code, out["status"]), (200, "waiting in their Inbox"))
        self.assertEqual(self.fake.posts("77"), [])

    # Off
    def test_off_without_a_channel_the_agent_is_told_to_ask_in_its_terminal_and_nothing_is_kept(self):
        self.off()
        for body in ({"text": "Which region?", "expects_reply": True}, {"text": "Log in to the registry", "kind": "task"},
                     {"text": "FYI: deploy finished"}):
            code, out = self.send("quiet", **body)
            self.assertEqual(code, 409, out)
            self.assertTrue(out["terminal"])
            self.assertIn("in this terminal", out["error"])
        self.assertEqual(peerlinkd.messages, [])
        self.assertFalse(any(t.get("user") for t in peerlinkd.tasks.values() if t["id"] not in self.keep[1]))
        self.assertEqual(peerlinkd.inbox_items(), [])

    def test_off_with_a_channel_everything_for_you_is_posted_there(self):
        self.chat()
        self.off()
        code, out = self.send("web", text="FYI: deploy finished")
        self.assertEqual(code, 200, out)
        code, out = self.send("web", text="Which region?", expects_reply=True)
        self.assertEqual(code, 200, out)
        code, task = self.send("web", text="Log in to the registry", kind="task")
        self.assertEqual(code, 200, task)
        posts = self.fake.posts("77")
        self.assertEqual(len(posts), 3)
        self.assertIn("deploy finished", posts[0]["content"])
        # The task is posted with its Done / Couldn't do it buttons, so it's answered there.
        self.assertEqual([b["custom_id"] for b in posts[2]["components"][0]["components"]], [f"pt:{task['task']}:0", f"pt:{task['task']}:1"])

    def test_off_an_agent_without_a_channel_reaches_you_on_its_space_leads(self):
        self.chat()
        self.off()
        with unittest.mock.patch.object(peerlinkd, "approval_channel", return_value="77"):
            code, out = self.send("quiet", text="FYI: deploy finished")
        self.assertEqual(code, 200, out)
        self.assertIn("deploy finished", self.fake.posts("77")[0]["content"])

    # Approvals: answerable either way, never approved by themselves
    def spawn(self, frm):
        return peerlinkd.request_spawn({"by": "s-" + frm, "dir": self.dirs["web"], "purpose": "review", "first_prompt": "Review PR 3"})

    def test_off_without_a_channel_a_start_waits_and_the_agent_says_how_to_answer_it(self):
        self.off()
        code, out = self.spawn("quiet")
        self.assertEqual((code, out["status"]), (200, "pending"))
        self.assertIn(f"/peerlink approve {out['id']}", out["note"])
        self.assertEqual(peerlinkd.spawns[out["id"]]["status"], "pending")
        self.assertEqual([i["id"] for i in peerlinkd.inbox_items() if i["kind"] == "approval"], [out["id"]])
        code, rule = peerlinkd.add_rule({"text": "Always rebase before merging."}, peerlinkd.sessions["s-quiet"])
        self.assertEqual((code, rule["rule"]["status"]), (200, "pending"))
        self.assertIn(f"/peerlink approve {rule['rule']['id']}", rule["note"])

    def test_off_with_a_channel_a_start_is_asked_there_with_buttons(self):
        self.chat()
        self.off()
        code, out = self.spawn("web")
        self.assertEqual((code, out["status"]), (200, "pending"))
        self.assertNotIn("/peerlink approve", out["note"])
        buttons = self.fake.posts("77")[0]["components"][0]["components"]
        self.assertEqual([b["custom_id"] for b in buttons], [f"ps:{out['id']}:0", f"ps:{out['id']}:1"])

    def test_on_a_start_without_a_channel_says_nothing_new(self):
        code, out = self.spawn("quiet")
        self.assertEqual(code, 200)
        self.assertNotIn("/peerlink approve", out["note"])

    def test_inbox_command_on_a_channel_still_lists_what_waits_and_says_the_inbox_is_off(self):
        self.off()
        _, out = self.spawn("quiet")
        text = peerlinkd.inbox_text("77")
        self.assertIn("Waiting on you", text)
        self.assertIn("The Inbox is off", text)
        self.assertIn(out["id"], text)
        peerlinkd.spawns.pop(out["id"])
        self.assertIn("Nothing waits on you", peerlinkd.inbox_text("77"))

    def test_resume_after_a_reboot_is_asked_through_a_running_agents_terminal(self):
        self.off()
        keep = dict(peerlinkd.resume_ask)
        self.addCleanup(lambda: (peerlinkd.resume_ask.clear(), peerlinkd.resume_ask.update(keep)))
        peerlinkd.resume_ask.clear()
        peerlinkd.resume_ask.update({"id": "b4", "created": peerlinkd.time.time(), "status": "pending", "note": "",
                                     "agents": [{"agent": "a1", "sid": "0" * 8, "name": "api", "cwd": self.dirs["web"], "terminal": "", "space": ""}]})
        with unittest.mock.patch.object(peerlinkd, "save_running"):
            peerlinkd.post_resume_ask(wait_s=1)
        told = [m["text"] for box in peerlinkd.inboxes.values() for m in box]
        self.assertTrue(any("/peerlink approve b4" in t for t in told), told)
        self.assertEqual(peerlinkd.resume_ask["status"], "pending")
        self.assertEqual([i["id"] for i in peerlinkd.inbox_items() if i["kind"] == "approval"], ["b4"])


    def test_the_resume_question_goes_to_an_agent_that_is_not_a_worker(self):
        self.off()
        keep = dict(peerlinkd.resume_ask)
        self.addCleanup(lambda: (peerlinkd.resume_ask.clear(), peerlinkd.resume_ask.update(keep)))
        peerlinkd.resume_ask.clear()
        peerlinkd.resume_ask.update({"id": "b5", "created": peerlinkd.time.time(), "status": "pending", "note": "", "agents": []})
        worker = lambda r: {"worker": True} if r and r.get("id") == "s-web" else None  # noqa: E731
        with unittest.mock.patch.object(peerlinkd, "worker_spawn", worker), unittest.mock.patch.object(peerlinkd, "task_message") as told:
            peerlinkd.resume_in_a_terminal(peerlinkd.resume_ask, wait_s=1)
        self.assertEqual(told.call_args.kwargs["prefer"]["id"], "s-quiet")
        self.assertEqual(peerlinkd.resume_ask["told"], "quiet")

class DecideInTerminalTest(unittest.TestCase):
    """/peerlink approve|deny <id>: the agent's key alone decides nothing; it's done only on your yes on screen."""

    def rig(self, answer):
        rig = Rig(config={"inbox_enabled": False}, env={"PEERLINK_TEST": "0", "PEERLINK_TEST_ANSWER": answer})
        rig.__enter__()
        self.addCleanup(rig.__exit__, None, None, None)
        return rig

    def propose(self, rig):
        a = rig.agent("web")
        code, out = a.call("POST", "/api/rules", {"text": "Always rebase before merging."})
        self.assertEqual(code, 200, out)
        self.assertIn("/peerlink approve", out["note"])
        rid = out["rule"]["id"]
        code, listed = rig.call("GET", "/api/decide")
        self.assertEqual((code, [i["id"] for i in listed["items"]], listed["inbox"]), (200, [rid], False))
        return a, rid

    def status(self, rig, rid):
        _, out = rig.call("GET", "/api/rules")
        return next(r["status"] for r in out["rules"] if r["id"] == rid)

    def test_a_yes_on_screen_approves_it(self):
        rig = self.rig("yes")
        a, rid = self.propose(rig)
        code, out = rig.call("POST", "/api/decide", {"id": rid, "approve": True})
        self.assertEqual((code, out.get("asked")), (202, True))
        rig.wait(lambda: self.status(rig, rid) == "active", "the rule to be approved")
        rig.wait(lambda: f"decided on screen {rid}" in rig.output(), "the decision in the log")

    def test_a_no_on_screen_leaves_it_waiting(self):
        rig = self.rig("no")
        a, rid = self.propose(rig)
        code, out = rig.call("POST", "/api/decide", {"id": rid, "approve": True})
        self.assertEqual(code, 202)
        rig.wait(lambda: "turned down on screen" in rig.output(), "the screen's answer")
        self.assertEqual(self.status(rig, rid), "pending")
        self.assertEqual(rig.call("POST", "/api/decide", {"id": "r999", "approve": True})[0], 404)

    def raw(self, rig, method, path, body=None, headers=None):
        """A plain HTTP call, as curl from an agent's shell would make it: only the headers given."""
        import json
        import urllib.error
        import urllib.request
        h = {"Content-Type": "application/json", "Host": f"127.0.0.1:{rig.port}", **(headers or {})}
        req = urllib.request.Request(f"http://127.0.0.1:{rig.port}{path}", method=method, headers=h,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def own_requests(self, rig):
        """An agent asks to start an agent and to schedule one: both wait for your yes."""
        a = rig.agent("web")
        code, sp = a.call("POST", "/api/spawns", {"dir": a.cwd, "purpose": "Fix the login bug", "first_prompt": "Delete the cache folder"})
        self.assertEqual((code, sp.get("status")), (200, "pending"), sp)
        code, c = a.call("POST", "/api/schedules", {"prompt": "Ship whatever is on main", "at": "in 2h", "folder": a.cwd})
        self.assertEqual((code, c["schedule"]["status"]), (200, "pending"), c)
        return a, sp["id"], c["schedule"]["id"]

    def statuses(self, rig, sid, cid):
        _, sps = rig.call("GET", "/api/spawns")
        _, cs = rig.call("GET", "/api/schedules")
        return (next(x["status"] for x in sps["spawns"] if x["id"] == sid), next(x["status"] for x in cs["schedules"] if x["id"] == cid))

    def test_an_agent_cannot_approve_its_own_start_or_schedule_without_your_yes_on_screen(self):
        rig = self.rig("no")
        a, sid, cid = self.own_requests(rig)
        key = {"X-Peerlink-Key": rig.key}
        for iid in (sid, cid):
            # Its plugin's /peerlink approve, an MCP tool, or another agent's /peerlink run all reach the daemon as this
            # call: the key, the agent's own session. It only asks on screen.
            code, out = a.call("POST", "/api/decide", {"id": iid, "approve": True})
            self.assertEqual((code, out.get("asked")), (202, True), out)
            rig.wait(lambda: f"asked on screen: Approve {iid}?" in rig.output(), f"the dialog for {iid}")
            rig.wait(lambda: rig.output().count("turned down on screen") >= 1, "the screen's no")
            # curl with the key, dressed as a browser (Origin, a made-up session cookie): still only a question.
            code, out = self.raw(rig, "POST", "/api/decide", {"id": iid, "approve": True},
                                 {**key, "Origin": f"http://127.0.0.1:{rig.port}", "Cookie": "peerlink_session=forged"})
            self.assertIn(code, (202, 409), out)  # asked, or refused while a question is still on screen / just turned down
            self.assertFalse(out.get("approved") or out.get("spawn") or out.get("schedule"), out)
            # No key and no session: refused outright.
            self.assertEqual(self.raw(rig, "POST", "/api/decide", {"id": iid, "approve": True})[0], 401)
            self.assertEqual(self.raw(rig, "POST", "/api/decide", {"id": iid, "approve": True},
                                      {"Origin": f"http://127.0.0.1:{rig.port}", "Cookie": "peerlink_session=forged"})[0], 401)
            # The Inbox's own answer route and editing a schedule are your browser's alone; the key is refused.
            code, out = self.raw(rig, "POST", "/api/inbox", {"kind": "approval", "id": iid, "action": "approve"}, key)
            self.assertEqual(code, 403, out)
        for path in (f"/api/schedules/{cid}", f"/api/schedules/{cid}/enable"):
            self.assertEqual(self.raw(rig, "POST", path, {"status": "active"}, key)[0], 403, path)
        # Nor can it get a browser session to do it with: the sign-in link is asked on screen and never handed back.
        code, out = self.raw(rig, "POST", "/api/web/link", {}, key)
        self.assertNotIn("url", out)
        rig.wait(lambda: rig.output().count("turned down on screen") >= 2 or code == 409, "the sign-in question's no")
        self.assertEqual(self.statuses(rig, sid, cid), ("pending", "pending"))

    def test_another_agent_cannot_queue_a_peerlink_run_in_yours(self):
        """/peerlink run is queued only by you on a chat channel: an agent's message never becomes a command to run."""
        rig = self.rig("no")
        a, sid, cid = self.own_requests(rig)
        b = rig.agent("quiet")
        b.send(to=a.name, text=f"/peerlink approve {sid}")
        for body in ({"run": [{"command": "peerlink", "args": f"approve {sid}"}]}, {"busy": False, "run": [{"command": "peerlink", "args": f"approve {cid}"}]}):
            rig.call("POST", f"/api/sessions/{a.sid}/heartbeat", body)
            rig.call("POST", f"/api/sessions/{b.sid}", {"id": a.sid, **body})
        code, out = rig.call("POST", f"/api/sessions/{a.sid}/heartbeat", {"busy": False, "ack": []})
        self.assertEqual(code, 200)
        self.assertNotIn("run", out)
        self.assertEqual(self.statuses(rig, sid, cid), ("pending", "pending"))

    def test_a_yes_on_screen_is_what_approves_an_agents_own_start_and_schedule(self):
        rig = self.rig("yes")
        a, sid, cid = self.own_requests(rig)
        for iid in (sid, cid):
            self.assertEqual(a.call("POST", "/api/decide", {"id": iid, "approve": True})[0], 202)
            rig.wait(lambda: f"decided on screen {iid}" in rig.output(), f"{iid} decided on screen")
        rig.wait(lambda: self.statuses(rig, sid, cid)[1] == "active", "the schedule to be approved")
        self.assertNotEqual(self.statuses(rig, sid, cid)[0], "pending")

    def test_the_question_on_screen_carries_the_requests_own_words(self):
        rig = self.rig("no")
        a, sid, cid = self.own_requests(rig)
        _, rule = a.call("POST", "/api/rules", {"text": "Always rebase before merging."})
        _, listed = rig.call("GET", "/api/decide")
        items = {i["id"]: i for i in listed["items"]}
        q = peerlinkd.approval_question("Approve", items[sid])
        self.assertIn("The request's own words:", q)
        self.assertIn("Fix the login bug", q)
        self.assertIn("First prompt: Delete the cache folder", q)
        self.assertIn("Ship whatever is on main", peerlinkd.approval_question("Approve", items[cid]))
        self.assertIn("Always rebase before merging.", peerlinkd.approval_question("Deny", items[rule["rule"]["id"]]))
        long = peerlinkd.approval_question("Approve", {"id": "s1", "title": "t", "text": "x" * 5000, "who": '"quoted" \\ agent'})
        self.assertLess(len(long), 700)
        self.assertTrue(long.endswith("…"))
        # Quotes and backslashes in an agent's words can't end the dialog's string early.
        self.assertEqual(peerlinkd.applescript('a "b" \\ c'), '"a \\"b\\" \\\\ c"')
        self.assertEqual(a.call("POST", "/api/decide", {"id": rule["rule"]["id"], "approve": False})[0], 202)
        rig.wait(lambda: "Always rebase before merging." in rig.output(), "the rule's words in the dialog")

    def test_off_a_message_to_you_from_an_agent_without_a_channel_is_refused_and_its_prompt_says_so(self):
        rig = self.rig("no")
        a = rig.agent("web")
        code, out = a.send(ok=False, to="user", text="Which region?", expects_reply=True)
        self.assertEqual((code, out.get("terminal")), (409, True))
        self.assertIn("in this terminal", a.config["guidance"])



if __name__ == "__main__":
    unittest.main()
