"""Tests for what 0.9.0 added to the daemon: a task's record and its notes, standing rules, and questions with a
default. Run: python3 -m unittest discover -s plugin/daemon"""
import os
import sys
import tempfile
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PEERLINK_HOME", tempfile.mkdtemp(prefix="peerlink-records-test-"))

import peerlinkd  # noqa: E402


class RecordsTest(unittest.TestCase):
    """A lead hands core a task, and core hands part of it to loop: the tree most relayed messages ran along."""

    def setUp(self):
        peerlinkd.load_config()
        peerlinkd.config.update({"rate_limit_per_min": 100, "wake_for": "needed"})
        now = peerlinkd.time.time()
        for name in ("lead", "core", "loop", "other"):
            peerlinkd.sessions["s-" + name] = {"id": "s-" + name, "name": name, "cwd": "/x/" + name, "last_seen": now}
        self.keep = (dict(peerlinkd.tasks), list(peerlinkd.messages), dict(peerlinkd.rules))
        peerlinkd.messages.clear()  # other tests' leftovers aren't whole messages
        peerlinkd.inboxes.clear()

    def tearDown(self):
        for name in ("lead", "core", "loop", "other"):
            peerlinkd.sessions.pop("s-" + name, None)
        peerlinkd.inboxes.clear()
        for store, was in zip((peerlinkd.tasks, peerlinkd.rules), (self.keep[0], self.keep[2])):
            store.clear()
            store.update(was)
        peerlinkd.messages[:] = self.keep[1]

    def send(self, frm, **body):
        status, out = peerlinkd.send({"from": "s-" + frm, **body})
        self.assertEqual(status, 200, out)
        return out

    def inbox(self, name):
        return [m["text"] for m in peerlinkd.inboxes.get("s-" + name, [])]

    def tree(self):
        top = self.send("lead", to="core", kind="task", text="Ship the template engine")["task"]
        # core names the task it owns without passing parent_task: the subtask is linked all the same.
        sub = self.send("core", to="loop", kind="task", text=f"Backend PR for {top}: templates and tests")["task"]
        self.assertEqual(peerlinkd.tasks[sub]["parent"], top)
        return top, sub

    def test_a_note_from_above_reaches_everyone_under_the_task_at_once(self):
        top, sub = self.tree()
        peerlinkd.inboxes.clear()
        status, out = peerlinkd.task_note(top, {"by": "s-lead", "text": "Approved: merge with a merge commit."})
        self.assertEqual((status, out["direction"], sorted(out["told"])), (200, "down", ["core", "loop"]))
        self.assertIn("Approved: merge with a merge commit.", self.inbox("core")[0])
        self.assertIn(f"through your task {sub}", self.inbox("loop")[0])  # no relay by core
        self.assertEqual(peerlinkd.tasks[top]["log"][-1]["kind"], "note")
        # Someone on no task above it can't post on it, nor read it.
        self.assertEqual(peerlinkd.task_note(top, {"by": "s-other", "text": "hi"})[0], 403)
        self.assertEqual(peerlinkd.task_view(top, peerlinkd.sessions["s-other"])[0], 403)

    def test_an_owners_progress_waits_for_the_next_turn_and_a_blocker_starts_one(self):
        top, sub = self.tree()
        peerlinkd.inboxes.clear()
        peerlinkd.task_note(sub, {"by": "s-loop", "text": "PR #166 is open."})
        progress = peerlinkd.inboxes["s-core"][-1]
        self.assertEqual((progress["wake"], progress["expects_reply"]), (False, False))
        peerlinkd.task_note(sub, {"by": "s-loop", "text": "Need the dev AI key's limit raised.", "blocked": True})
        self.assertTrue(peerlinkd.inboxes["s-core"][-1]["wake"])
        self.assertEqual(peerlinkd.tasks[sub]["blocked"], "Need the dev AI key's limit raised.")
        # The lead two levels up reads it all on the record, without a message from anyone.
        status, view = peerlinkd.task_view(top, peerlinkd.sessions["s-lead"])
        self.assertEqual(status, 200)
        self.assertEqual([(t["id"], t["depth"], bool(t["blocked"])) for t in view["below"]], [(sub, 1, True)])
        self.assertEqual(self.inbox("lead"), [])
        # And the owner below sees what was decided above it.
        peerlinkd.task_note(top, {"by": "s-lead", "text": "Limit raised."})
        self.assertEqual(peerlinkd.tasks[sub]["blocked"], "Need the dev AI key's limit raised.")  # its own blocker is its own to clear
        self.assertEqual(peerlinkd.task_view(sub, peerlinkd.sessions["s-loop"])[1]["above"][0]["log"][-1]["text"], "Limit raised.")
        peerlinkd.update_task(sub, {"by": "s-loop", "status": "done", "note": "Merged."})
        self.assertEqual((peerlinkd.tasks[sub]["blocked"], peerlinkd.tasks[sub]["log"][-1]["kind"]), ("", "closed"))

    def test_your_answer_about_a_task_is_kept_on_it_and_sent_to_everyone_under_it(self):
        top, sub = self.tree()
        asked = self.send("lead", to="user", expects_reply=True, text=f"Merge {top}'s frontend PR today?")
        self.assertEqual(asked["about_task"], top)  # named in the text, not passed
        peerlinkd.inboxes.clear()
        status, _ = peerlinkd.inbox_act({"kind": "question", "id": asked["id"], "action": "reply", "text": "Yes, after CI is green."})
        self.assertEqual(status, 200)
        self.assertEqual(peerlinkd.tasks[top]["log"][-1]["kind"], "decision")
        for name in ("core", "loop"):
            self.assertIn("Yes, after CI is green.", self.inbox(name)[0])
        self.assertEqual(len(self.inbox("lead")), 1)  # the asker gets the answer itself, not the fan-out too

    def test_a_question_goes_with_its_default_when_you_dont_answer_in_time(self):
        self.assertIn("at least 10 minutes", peerlinkd.send({"from": "s-lead", "to": "user", "text": "Rebase now?", "default": "Yes", "default_after": "2m"})[1]["error"])
        self.assertIn("one of the options", peerlinkd.send({"from": "s-lead", "to": "user", "text": "Rebase now?", "options": ["Yes", "No"], "default": "Maybe"})[1]["error"])
        self.assertIn("question to your user", peerlinkd.send({"from": "s-lead", "to": "core", "text": "Rebase now?", "default": "Yes"})[1]["error"])
        asked = self.send("lead", to="user", text="Rebase the branch now?", default="Yes, rebase", default_after="30m")
        m = next(x for x in peerlinkd.messages if x["id"] == asked["id"])
        self.assertTrue(m["expects_reply"])
        self.assertIn("No answer in 30 min, and lead goes with: Yes, rebase", m["text"])
        with peerlinkd.lock:
            peerlinkd.apply_defaults(m["ts"] + 60)
        self.assertEqual(self.inbox("lead"), [])  # not yet
        with peerlinkd.lock:
            peerlinkd.apply_defaults(m["ts"] + 1801)
            peerlinkd.apply_defaults(m["ts"] + 1900)
        self.assertEqual(len(self.inbox("lead")), 1)  # once
        self.assertIn("go with your default: Yes, rebase", self.inbox("lead")[0])
        self.assertNotIn(asked["id"], [i["id"] for i in peerlinkd.inbox_items()])
        # Answered in time, the default never applies; turned off, none is taken.
        peerlinkd.inboxes.clear()
        again = self.send("lead", to="user", text="Squash too?", default="No", default_after="10m")
        peerlinkd.inbox_act({"kind": "question", "id": again["id"], "action": "reply", "text": "Yes, squash."})
        with peerlinkd.lock:
            peerlinkd.apply_defaults(peerlinkd.time.time() + 3600)
        self.assertEqual(self.inbox("lead"), ["Yes, squash."])
        peerlinkd.config["ask_defaults"] = False
        self.assertIn("turned defaults off", peerlinkd.send({"from": "s-lead", "to": "user", "text": "Go?", "default": "Yes"})[1]["error"])

    def test_a_rule_an_agent_proposes_waits_for_you_and_then_reaches_its_space(self):
        lead, other = peerlinkd.sessions["s-lead"], peerlinkd.sessions["s-other"]
        space = peerlinkd.space_of(lead)[0]
        peerlinkd.config["auto_approve_all"] = True  # no shortcut for rules
        status, out = peerlinkd.add_rule({"text": "Never  merge to main on a Friday."}, lead)
        rid = out["rule"]["id"]
        self.assertEqual((status, out["rule"]["status"], out["rule"]["space"]), (200, "pending", space))
        self.assertEqual(peerlinkd.client_config("s-lead")["rules"], [])
        self.assertIn(rid, [i["id"] for i in peerlinkd.inbox_items()])
        self.assertEqual(peerlinkd.inbox_act({"kind": "approval", "id": rid, "action": "approve"})[0], 200)
        self.assertEqual([r["text"] for r in peerlinkd.client_config("s-other")["rules"]], ["Never merge to main on a Friday."])
        self.assertIn("approved rule", self.inbox("lead")[-1])
        self.assertEqual(peerlinkd.add_rule({"text": "never merge to main on a friday."}, other)[0], 409)
        # Yours are active at once; one for another space reaches nobody here; off or deleted, it's gone.
        mine = peerlinkd.add_rule({"text": "Answer in English.", "space": "*"})[1]["rule"]
        peerlinkd.add_rule({"text": "Only for elsewhere.", "space": "elsewhere"})
        self.assertEqual(len(peerlinkd.client_config("s-lead")["rules"]), 2)
        peerlinkd.edit_rule(mine["id"], {"status": "off"})
        peerlinkd.edit_rule(rid, {"delete": True})
        self.assertEqual(peerlinkd.client_config("s-lead")["rules"], [])

    def test_a_task_nobody_touched_is_raised_with_its_owner_then_with_whoever_asked(self):
        top, _ = self.tree()
        t = peerlinkd.tasks[top]
        peerlinkd.tasks = {top: t}  # only this one, whatever else the tree made
        self.addCleanup(lambda: setattr(peerlinkd, "tasks", self.keep[0]))
        peerlinkd.inboxes.clear()
        day, start = 86400, t["updated"]

        def at(days):
            peerlinkd.last_nudge[0] = 0
            with peerlinkd.lock:
                peerlinkd.nudge_stale_tasks(start + days * day)

        at(0.5)
        self.assertEqual(self.inbox("core"), [])  # not quiet for a day yet
        at(1.1)
        at(1.2)  # asked once a day, not at every pass
        self.assertEqual(len(self.inbox("core")), 1)
        self.assertIn(f"Task {top} (Ship the template engine) is yours and hasn't moved in", self.inbox("core")[0])
        at(2.2)
        at(3.3)  # two asks unanswered: whoever asked hears, once
        at(4.4)
        self.assertEqual((len(self.inbox("core")), len(self.inbox("lead"))), (2, 1))
        self.assertIn("was asked twice and hasn't answered", self.inbox("lead")[0])
        self.assertEqual(t["updated"], start)  # asking isn't an update
        # A note from the owner starts the count again; with nudges off, nobody is asked.
        peerlinkd.task_note(top, {"by": "s-core", "text": "Waiting on CI."})
        peerlinkd.inboxes.clear()
        peerlinkd.config["task_nudge_h"] = 0
        peerlinkd.last_nudge[0] = 0
        with peerlinkd.lock:
            peerlinkd.nudge_stale_tasks(t["updated"] + 5 * day)
        self.assertEqual(self.inbox("core"), [])

    def test_an_ask_older_than_the_messages_kept_in_memory_can_still_be_closed_by_its_senders_folder(self):
        # lead asked on Discord, then restarted (a new session id) and 500+ messages went by: the ask is only in the log.
        old = self.send("lead", to="core", text="Move the folder back?", expects_reply=True, options=["Moved it", "Later"])["id"]
        peerlinkd.messages[:] = [m for m in peerlinkd.messages if m["id"] != old]
        peerlinkd.sessions["s-lead2"] = {"id": "s-lead2", "name": "lead", "cwd": "/x/lead", "last_seen": peerlinkd.time.time()}
        try:
            self.assertEqual(peerlinkd.close_threads("s-other", [old]), (200, {"closed": [], "not_yours": [old]}))
            self.assertEqual(peerlinkd.close_threads("s-lead2", [old]), (200, {"closed": [old]}))
            self.assertTrue(peerlinkd.logged({old})[old]["handled"])
        finally:
            peerlinkd.sessions.pop("s-lead2", None)

    def test_a_message_its_agent_is_holding_isnt_handed_out_again(self):
        self.send("lead", to="core", text="FYI: the freeze is over.")
        first = peerlinkd.take_inbox("s-core", track=True)
        self.addCleanup(lambda: peerlinkd.handed.pop("s-core", None))
        mid = first[0]["id"]
        m, ts = peerlinkd.handed["s-core"][mid]
        peerlinkd.handed["s-core"][mid] = (m, ts - 2 * peerlinkd.REDELIVER_S)  # long enough ago to hand out again
        peerlinkd.ack("s-core", [], [mid])  # the plugin still has it, waiting for its next turn
        self.assertEqual(peerlinkd.take_inbox("s-core", track=True), [])
        peerlinkd.handed["s-core"][mid] = (m, ts - 2 * peerlinkd.REDELIVER_S)
        self.assertEqual([x["id"] for x in peerlinkd.take_inbox("s-core", track=True)], [mid])  # it stopped saying so: a reload lost it
        peerlinkd.ack("s-core", [mid])
        self.assertEqual(peerlinkd.handed["s-core"], {})


    def restart(self):
        """The daemon dies (kill -9: nothing in memory is saved) and starts again from its log."""
        peerlinkd.messages.clear()
        peerlinkd.inboxes.clear()
        peerlinkd.handed.clear()
        peerlinkd.load_messages()

    def test_a_message_handed_out_but_not_acked_survives_a_daemon_crash(self):
        peerlinkd.append_log({"id": "m-old", "ts": peerlinkd.time.time(), "to": "s-core", "text": "from an older daemon",
                              "status": "delivered", "v": 2})  # handed out before tracking existed: left alone
        lost = self.send("lead", to="core", text="Deploy is blocked on you.")["id"]
        kept = self.send("lead", to="core", text="FYI, all green.")["id"]
        self.assertEqual({m["id"] for m in peerlinkd.take_inbox("s-core", track=True)}, {lost, kept})
        peerlinkd.ack("s-core", [kept])  # the plugin got this one into the session; the daemon died before the other's ack
        self.restart()
        self.assertEqual([m["id"] for m in peerlinkd.inboxes.get("s-core", [])], [lost])
        again = peerlinkd.take_inbox("s-core", track=True)
        self.assertEqual([m["id"] for m in again], [lost])
        self.assertEqual(again[0]["text"], "Deploy is blocked on you.")
        peerlinkd.ack("s-core", [lost])
        self.restart()  # acked now: a second restart hands nothing out
        self.assertEqual(peerlinkd.inboxes.get("s-core", []), [])
        # A plugin that doesn't ack never gets it twice either.
        old = self.send("lead", to="core", text="no acks here")["id"]
        self.assertEqual([m["id"] for m in peerlinkd.take_inbox("s-core")], [old])
        self.restart()
        self.assertEqual(peerlinkd.inboxes.get("s-core", []), [])


class LogIndexTest(unittest.TestCase):
    """Reading old messages from the log reads on from where the last read stopped, follows rotation, and
    close_threads reads it without holding the lock."""

    def setUp(self):
        self.was = peerlinkd.MESSAGES_PATH, peerlinkd.log_index
        peerlinkd.MESSAGES_PATH = os.path.join(tempfile.mkdtemp(prefix="peerlink-logindex-"), "messages.jsonl")
        peerlinkd.log_index = peerlinkd.LogIndex()

    def tearDown(self):
        peerlinkd.MESSAGES_PATH, peerlinkd.log_index = self.was

    def msg(self, i, frm="a", to="b", **more):
        peerlinkd.append_log({"id": f"m{i}", "ts": i, "from": "s-" + frm, "from_name": frm, "to": "s-" + to,
                              "to_name": to, "text": f"hi {i}", **more})

    def test_a_read_parses_only_what_was_written_since_the_last_one(self):
        for i in range(200):
            self.msg(i, *(("a", "b") if i % 2 else ("a", "c")))
        self.assertEqual(len(peerlinkd.conversation("n:a", "n:b")), 100)
        size = os.path.getsize(peerlinkd.MESSAGES_PATH)
        self.assertEqual(peerlinkd.log_index.parsed, size)
        peerlinkd.append_log({"id": "m1", "handled": True})
        self.msg(500)
        self.assertTrue(peerlinkd.logged({"m1"})["m1"]["handled"])
        self.assertEqual(len(peerlinkd.conversation("n:a", "n:b")), 101)
        self.assertEqual(peerlinkd.log_index.parsed, os.path.getsize(peerlinkd.MESSAGES_PATH))  # only the new lines

    def test_a_half_written_line_waits_and_a_rotation_is_read_again(self):
        self.msg(1)
        with open(peerlinkd.MESSAGES_PATH, "a") as f:
            f.write('{"id": "m2", "ts": 2, "text": "half')
        self.assertEqual(set(peerlinkd.logged()), {"m1"})
        with open(peerlinkd.MESSAGES_PATH, "a") as f:
            f.write(' done", "from": "s-a", "from_name": "a", "to": "s-b", "to_name": "b"}\n')
        self.assertEqual(peerlinkd.logged({"m2"})["m2"]["text"], "half done")
        with peerlinkd.lock:
            peerlinkd.rewrite_log(peerlinkd.MESSAGES_PATH, [{**peerlinkd.logged({"m2"})["m2"], "handled": True}])
        self.msg(3)
        self.assertEqual(set(peerlinkd.logged()), {"m1", "m2", "m3"})
        self.assertTrue(peerlinkd.logged({"m2"})["m2"]["handled"])
        self.assertEqual([m["id"] for m in peerlinkd.conversation("n:a", "n:b")], ["m1", "m2", "m3"])

    def test_a_message_whose_recipient_was_named_later_is_in_that_conversation(self):
        self.msg(1, to="the agent starting for s7")
        peerlinkd.append_log({"id": "m1", "to": "s-c", "to_name": "c"})  # the started agent took it
        self.assertEqual([m["id"] for m in peerlinkd.conversation("n:a", "n:c")], ["m1"])
        self.assertEqual(peerlinkd.conversation("n:a", "n:the agent starting for s7"), [])

    def test_closing_an_old_thread_reads_the_log_without_the_lock(self):
        peerlinkd.sessions["s-a"] = {"id": "s-a", "name": "a", "cwd": "/x/a", "last_seen": peerlinkd.time.time()}
        self.addCleanup(peerlinkd.sessions.pop, "s-a", None)
        self.msg(7)
        held = []
        real = peerlinkd.LogIndex.read

        def read(index, *args, **kw):
            held.append(peerlinkd.lock._is_owned())
            return real(index, *args, **kw)

        with unittest.mock.patch.object(peerlinkd.LogIndex, "read", read):
            self.assertEqual(peerlinkd.close_threads("s-a", ["m7"]), (200, {"closed": ["m7"]}))
        self.assertEqual(held, [False])
        self.assertTrue(peerlinkd.logged({"m7"})["m7"]["handled"])


class RemotesTest(unittest.TestCase):
    """Other machines are asked at once and cached, and a send that fails is tried again."""

    def setUp(self):
        self.was = list(peerlinkd.config.get("remotes", []))
        peerlinkd.config["remotes"] = ["http://r1", "http://r2", "http://r3"]
        peerlinkd.remote_cache.clear()
        self.addCleanup(peerlinkd.remote_cache.clear)
        was = list(peerlinkd.messages)
        peerlinkd.messages.clear()  # other tests' leftovers aren't whole messages
        self.addCleanup(lambda: peerlinkd.messages.__setitem__(slice(None), was))

    def tearDown(self):
        peerlinkd.config["remotes"] = self.was

    def test_remotes_are_asked_at_once_and_kept_for_a_few_seconds(self):
        calls = []

        def call(base, path, body=None, timeout=2):
            calls.append(base)
            peerlinkd.time.sleep(0.3)
            return {"peers": [{"id": "p-" + base[-2:], "name": base[-2:], "host": base[-2:]}]}

        with unittest.mock.patch.object(peerlinkd, "remote_call", call):
            t = peerlinkd.time.time()
            self.assertEqual([p["name"] for p in peerlinkd.remote_peers()], ["r1", "r2", "r3"])
            self.assertLess(peerlinkd.time.time() - t, 0.6)  # one wait, not three
            peerlinkd.remote_peers()
            peerlinkd.snapshot()
            self.assertEqual(len(calls), 3)  # from the cache
            peerlinkd.remote_peers(0)
            self.assertEqual(len(calls), 6)

    def test_a_send_that_fails_is_tried_again_and_one_refused_is_not(self):
        peerlinkd.config["remotes"] = ["http://r1"]
        peerlinkd.remote_cache["http://r1"] = (peerlinkd.time.time(), [{"id": "p-bob", "name": "bob", "host": "r1", "remote": "http://r1"}])
        sent = []

        def call(base, path, body=None, timeout=2):
            sent.append(body)
            if len(sent) < 3:
                raise peerlinkd.urllib.error.URLError("timed out")
            return {"id": "m77", "status": "queued"}

        with unittest.mock.patch.object(peerlinkd, "remote_call", call), unittest.mock.patch.object(peerlinkd.time, "sleep"):
            code, out = peerlinkd.forward({"from": "s-x", "to": "bob@r1", "text": "hi"}, 0, None)
        self.assertEqual((code, out["id"], len(sent)), (200, "m77", 3))
        self.assertEqual({b["origin_id"] for b in sent}, {sent[0]["origin_id"]})  # the same message each time, for the far side to drop copies
        copy = next(m for m in peerlinkd.messages if m["id"] == sent[0]["origin_id"])
        self.assertEqual((copy["status"], copy["origin_id"], copy["to_name"]), ("forwarded", "m77", "bob@r1"))

        def refuse(base, path, body=None, timeout=2):
            sent.append(body)
            raise peerlinkd.urllib.error.HTTPError(base, 403, "forbidden", {}, None)

        sent.clear()
        with unittest.mock.patch.object(peerlinkd, "remote_call", refuse):
            code, _ = peerlinkd.forward({"from": "s-x", "to": "bob@r1", "text": "hi"}, 0, None)
        self.assertEqual((code, len(sent)), (502, 1))


class PeerCountTest(unittest.TestCase):
    """A heartbeat counts the agents that share a space with it, without building every peer's record."""

    def setUp(self):
        peerlinkd.load_config()
        self.keep = dict(peerlinkd.sessions), list(peerlinkd.messages)
        peerlinkd.sessions.clear()
        now = peerlinkd.time.time()
        for i in range(40):
            sid = f"pc-{i}"
            peerlinkd.sessions[sid] = {"id": sid, "name": f"a{i}", "cwd": f"/x/a{i}", "last_seen": now - (400 if i % 9 == 0 else 0),
                                       "space": ("top", "core", "loop")[i % 3]}
        for i in range(300):  # a busy log: what building each peer's open threads walks through
            peerlinkd.messages.append({"id": f"mpc{i}", "ts": now, "from": f"pc-{i % 40}", "to": f"pc-{(i + 1) % 40}",
                                       "text": "x", "expects_reply": True, "v": 2, "status": "delivered"})

    def tearDown(self):
        peerlinkd.sessions.clear()
        peerlinkd.sessions.update(self.keep[0])
        peerlinkd.messages[:] = self.keep[1]

    def old_count(self, sid):
        with peerlinkd.lock:
            mine = peerlinkd.reach(peerlinkd.sessions[sid])
        return sum(1 for p in peerlinkd.local_peers() if p["id"] != sid and peerlinkd.peer_reach(p) & mine)

    def test_the_count_is_the_same_and_far_cheaper(self):
        for sid in peerlinkd.sessions:
            self.assertEqual(peerlinkd.peer_count(sid), self.old_count(sid), sid)
        self.assertEqual(peerlinkd.peer_count("nobody"), 0)
        t = peerlinkd.time.perf_counter
        t0 = t()
        for _ in range(20):
            self.old_count("pc-1")
        old = (t() - t0) / 20
        t0 = t()
        for _ in range(20):
            peerlinkd.peer_count("pc-1")
        new = (t() - t0) / 20
        print(f"\nheartbeat peer count, 40 agents: {old * 1000:.2f} ms before, {new * 1000:.3f} ms now")
        self.assertLess(new * 5, old)


class StateFileTest(unittest.TestCase):
    """State files are written whole or not at all, and a damaged one is kept and recovered, never just replaced."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="peerlink-state-")
        self.path = os.path.join(self.dir, "tasks.json")

    def test_a_damaged_file_is_kept_and_the_last_good_copy_loaded(self):
        peerlinkd.write_json(self.path, [{"id": "t1"}])
        peerlinkd.write_json(self.path, [{"id": "t1"}, {"id": "t2"}])
        with open(self.path, "w") as f:
            f.write('[{"id": "t1"}, {"id"')  # torn by a disk or an editor
        self.assertEqual(peerlinkd.read_json(self.path, []), [{"id": "t1"}])  # path.bak: the copy before the last save
        kept = [n for n in os.listdir(self.dir) if ".corrupt-" in n]
        self.assertEqual(len(kept), 1)
        with open(os.path.join(self.dir, kept[0])) as f:
            self.assertEqual(f.read(), '[{"id": "t1"}, {"id"')
        # No backup either: defaults, and the damaged file is still kept.
        os.remove(self.path + ".bak")
        self.assertEqual(peerlinkd.read_json(self.path, "default"), "default")
        self.assertTrue(any(".corrupt-" in n for n in os.listdir(self.dir)))

    def test_a_crash_mid_write_leaves_the_old_file_whole(self):
        peerlinkd.write_json(self.path, {"keep": "me"})
        real = os.replace
        def dies(src, dst):
            if dst == self.path:
                raise KeyboardInterrupt("killed between fsync and rename")
            return real(src, dst)
        peerlinkd.os.replace = dies
        try:
            with self.assertRaises(KeyboardInterrupt):
                peerlinkd.write_json(self.path, {"keep": "the new one"})
        finally:
            peerlinkd.os.replace = real
        self.assertEqual(peerlinkd.read_json(self.path), {"keep": "me"})
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])
        # A kill -9 at that point can't clean up: its tmp file is left behind and ignored.
        with open(self.path + ".x.tmp", "w") as f:
            f.write("{half")
        self.assertEqual(peerlinkd.read_json(self.path), {"keep": "me"})

    def test_writers_at_once_never_share_a_tmp_file(self):
        import threading
        errors = []
        def write(i):
            try:
                for _ in range(20):
                    peerlinkd.write_json(self.path, {"writer": i, "pad": "x" * 5000})
            except Exception as e:
                errors.append(repr(e))
        ts = [threading.Thread(target=write, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        self.assertIn(peerlinkd.read_json(self.path)["writer"], range(8))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_a_rotated_key_leaves_no_copy(self):
        old = (peerlinkd.KEY_PATH, peerlinkd.HOME)
        peerlinkd.KEY_PATH, peerlinkd.HOME = os.path.join(self.dir, "key"), self.dir
        try:
            peerlinkd.write_key()
            peerlinkd.write_key()
            self.assertEqual(sorted(os.listdir(self.dir)), ["key"])
        finally:
            peerlinkd.KEY_PATH, peerlinkd.HOME = old


class LocalNameTest(unittest.TestCase):
    def test_the_web_ui_answers_to_its_local_name_and_nothing_a_page_could_pick(self):
        keep = peerlinkd.config.get("local_name")
        try:
            p = peerlinkd.PORT
            peerlinkd.config["local_name"] = "peerlink.local"
            self.assertEqual(peerlinkd.web_hosts(), {f"{h}:{p}" for h in ("127.0.0.1", "localhost", "[::1]", "peerlink.local", "peerlink.localhost")})
            for bad in ("evil.com", "peerlink.local.evil.com", "", "peer link.local"):
                peerlinkd.config["local_name"] = bad
                self.assertEqual(peerlinkd.local_name(), "")
                self.assertEqual(peerlinkd.web_hosts(), {f"{h}:{p}" for h in ("127.0.0.1", "localhost", "[::1]")})
            self.assertEqual(peerlinkd.link_host(), "127.0.0.1")  # no name: links stay on 127.0.0.1
        finally:
            peerlinkd.config["local_name"] = keep


class FolderNameTest(unittest.TestCase):
    def setUp(self):
        peerlinkd.load_config()

    def test_renaming_one_of_several_agents_in_a_folder_leaves_the_folder_and_its_channel_with_the_lead(self):
        now = peerlinkd.time.time()
        keep = dict(peerlinkd.config.get("folder_names") or {})
        lead = {"id": "s-lead", "name": "repo", "cwd": "/x/repo", "last_seen": now - 5}
        worker = {"id": "s-work", "name": "repo", "cwd": "/x/repo", "last_seen": now}
        peerlinkd.sessions.update({"s-lead": lead, "s-work": worker})
        try:
            peerlinkd.config["sessions"].setdefault("s-work", {})["nickname"] = worker["name"] = "repo-worker"
            self.assertFalse(peerlinkd.name_folder(worker, "repo-worker"))
            self.assertEqual(peerlinkd.folder_name("/x/repo"), "repo")
            self.assertEqual(peerlinkd.successor("/x/repo")["id"], "s-lead")  # Discord mapped to the folder reaches the lead
            peerlinkd.sessions.pop("s-lead")
            self.assertTrue(peerlinkd.name_folder(worker, "repo-worker"))  # alone there, the folder takes its name
            self.assertEqual(peerlinkd.folder_name("/x/repo"), "repo-worker")
        finally:
            for sid in ("s-lead", "s-work"):
                peerlinkd.sessions.pop(sid, None)
                peerlinkd.config["sessions"].pop(sid, None)
            peerlinkd.config["folder_names"] = keep


if __name__ == "__main__":
    unittest.main()

class SpawnToldTest(unittest.TestCase):
    def setUp(self):
        peerlinkd.load_config()

    def test_word_of_a_started_agent_goes_to_the_agent_that_asked_not_the_one_going_by_its_folder(self):
        folder = tempfile.mkdtemp(prefix="peerlink-shared-")
        now = peerlinkd.time.time()
        keep = (dict(peerlinkd.config.get("folder_names") or {}), list(peerlinkd.config.get("auto_approve_folders") or []),
                peerlinkd.launch, peerlinkd.save_spawns)
        lead = {"id": "s-lead", "name": "repo", "cwd": folder, "last_seen": now - 5}
        worker = {"id": "s-work", "name": "repo-worker", "cwd": folder, "last_seen": now}
        peerlinkd.sessions.update({"s-lead": lead, "s-work": worker})
        peerlinkd.config["folder_names"] = {**keep[0], folder: os.path.basename(folder)}
        lead["name"] = os.path.basename(folder)
        peerlinkd.config["auto_approve_folders"] = [folder]
        peerlinkd.launch = lambda sp: ("in tmux (tmux attach -t x)", None)
        peerlinkd.save_spawns = lambda: None
        peerlinkd.inboxes.clear()
        try:
            status, out = peerlinkd.request_spawn({"by": "s-work", "dir": folder, "purpose": "click-through", "first_prompt": "Run it."})
            self.assertEqual((status, out["status"]), (200, "started"))
            told = [m["text"] for m in peerlinkd.inboxes.get("s-work", [])]
            self.assertTrue(any("was approved automatically" in t and "your first prompt is waiting" in t for t in told), told)
            self.assertFalse(any("approved" in m["text"] for m in peerlinkd.inboxes.get("s-lead", [])))
        finally:
            peerlinkd.sessions.pop("s-lead", None)
            peerlinkd.sessions.pop("s-work", None)
            peerlinkd.config["folder_names"], peerlinkd.config["auto_approve_folders"] = keep[0], keep[1]
            peerlinkd.launch, peerlinkd.save_spawns = keep[2], keep[3]
            peerlinkd.inboxes.clear()
