"""Tests for what 0.9.0 added to the daemon: a task's record and its notes, standing rules, and questions with a
default. Run: python3 -m unittest discover -s plugin/daemon"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PEERLINK_HOME", tempfile.mkdtemp(prefix="peerlink-records-test-"))

import peerlinkd  # noqa: E402


class RecordsTest(unittest.TestCase):
    """A lead (sky) hands core a task, and core hands part of it to loop: the tree most relayed messages ran along."""

    def setUp(self):
        peerlinkd.load_config()
        peerlinkd.config.update({"rate_limit_per_min": 100, "wake_for": "needed"})
        now = peerlinkd.time.time()
        for name in ("sky", "core", "loop", "other"):
            peerlinkd.sessions["s-" + name] = {"id": "s-" + name, "name": name, "cwd": "/x/" + name, "last_seen": now}
        self.keep = (dict(peerlinkd.tasks), list(peerlinkd.messages), dict(peerlinkd.rules))
        peerlinkd.messages.clear()  # other tests' leftovers aren't whole messages
        peerlinkd.inboxes.clear()

    def tearDown(self):
        for name in ("sky", "core", "loop", "other"):
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
        top = self.send("sky", to="core", kind="task", text="Ship the template engine")["task"]
        # core names the task it owns without passing parent_task: the subtask is linked all the same.
        sub = self.send("core", to="loop", kind="task", text=f"Backend PR for {top}: templates and tests")["task"]
        self.assertEqual(peerlinkd.tasks[sub]["parent"], top)
        return top, sub

    def test_a_note_from_above_reaches_everyone_under_the_task_at_once(self):
        top, sub = self.tree()
        peerlinkd.inboxes.clear()
        status, out = peerlinkd.task_note(top, {"by": "s-sky", "text": "Approved: merge with a merge commit."})
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
        status, view = peerlinkd.task_view(top, peerlinkd.sessions["s-sky"])
        self.assertEqual(status, 200)
        self.assertEqual([(t["id"], t["depth"], bool(t["blocked"])) for t in view["below"]], [(sub, 1, True)])
        self.assertEqual(self.inbox("sky"), [])
        # And the owner below sees what was decided above it.
        peerlinkd.task_note(top, {"by": "s-sky", "text": "Limit raised."})
        self.assertEqual(peerlinkd.tasks[sub]["blocked"], "Need the dev AI key's limit raised.")  # its own blocker is its own to clear
        self.assertEqual(peerlinkd.task_view(sub, peerlinkd.sessions["s-loop"])[1]["above"][0]["log"][-1]["text"], "Limit raised.")
        peerlinkd.update_task(sub, {"by": "s-loop", "status": "done", "note": "Merged."})
        self.assertEqual((peerlinkd.tasks[sub]["blocked"], peerlinkd.tasks[sub]["log"][-1]["kind"]), ("", "closed"))

    def test_your_answer_about_a_task_is_kept_on_it_and_sent_to_everyone_under_it(self):
        top, sub = self.tree()
        asked = self.send("sky", to="user", expects_reply=True, text=f"Merge {top}'s frontend PR today?")
        self.assertEqual(asked["about_task"], top)  # named in the text, not passed
        peerlinkd.inboxes.clear()
        status, _ = peerlinkd.inbox_act({"kind": "question", "id": asked["id"], "action": "reply", "text": "Yes, after CI is green."})
        self.assertEqual(status, 200)
        self.assertEqual(peerlinkd.tasks[top]["log"][-1]["kind"], "decision")
        for name in ("core", "loop"):
            self.assertIn("Yes, after CI is green.", self.inbox(name)[0])
        self.assertEqual(len(self.inbox("sky")), 1)  # the asker gets the answer itself, not the fan-out too

    def test_a_question_goes_with_its_default_when_you_dont_answer_in_time(self):
        self.assertIn("at least 10 minutes", peerlinkd.send({"from": "s-sky", "to": "user", "text": "Rebase now?", "default": "Yes", "default_after": "2m"})[1]["error"])
        self.assertIn("one of the options", peerlinkd.send({"from": "s-sky", "to": "user", "text": "Rebase now?", "options": ["Yes", "No"], "default": "Maybe"})[1]["error"])
        self.assertIn("question to your user", peerlinkd.send({"from": "s-sky", "to": "core", "text": "Rebase now?", "default": "Yes"})[1]["error"])
        asked = self.send("sky", to="user", text="Rebase the branch now?", default="Yes, rebase", default_after="30m")
        m = next(x for x in peerlinkd.messages if x["id"] == asked["id"])
        self.assertTrue(m["expects_reply"])
        self.assertIn("No answer in 30 min, and sky goes with: Yes, rebase", m["text"])
        with peerlinkd.lock:
            peerlinkd.apply_defaults(m["ts"] + 60)
        self.assertEqual(self.inbox("sky"), [])  # not yet
        with peerlinkd.lock:
            peerlinkd.apply_defaults(m["ts"] + 1801)
            peerlinkd.apply_defaults(m["ts"] + 1900)
        self.assertEqual(len(self.inbox("sky")), 1)  # once
        self.assertIn("go with your default: Yes, rebase", self.inbox("sky")[0])
        self.assertNotIn(asked["id"], [i["id"] for i in peerlinkd.inbox_items()])
        # Answered in time, the default never applies; turned off, none is taken.
        peerlinkd.inboxes.clear()
        again = self.send("sky", to="user", text="Squash too?", default="No", default_after="10m")
        peerlinkd.inbox_act({"kind": "question", "id": again["id"], "action": "reply", "text": "Yes, squash."})
        with peerlinkd.lock:
            peerlinkd.apply_defaults(peerlinkd.time.time() + 3600)
        self.assertEqual(self.inbox("sky"), ["Yes, squash."])
        peerlinkd.config["ask_defaults"] = False
        self.assertIn("turned defaults off", peerlinkd.send({"from": "s-sky", "to": "user", "text": "Go?", "default": "Yes"})[1]["error"])

    def test_a_rule_an_agent_proposes_waits_for_you_and_then_reaches_its_space(self):
        sky, other = peerlinkd.sessions["s-sky"], peerlinkd.sessions["s-other"]
        space = peerlinkd.space_of(sky)[0]
        peerlinkd.config["auto_approve_all"] = True  # no shortcut for rules
        status, out = peerlinkd.add_rule({"text": "Never  merge to main on a Friday."}, sky)
        rid = out["rule"]["id"]
        self.assertEqual((status, out["rule"]["status"], out["rule"]["space"]), (200, "pending", space))
        self.assertEqual(peerlinkd.client_config("s-sky")["rules"], [])
        self.assertIn(rid, [i["id"] for i in peerlinkd.inbox_items()])
        self.assertEqual(peerlinkd.inbox_act({"kind": "approval", "id": rid, "action": "approve"})[0], 200)
        self.assertEqual([r["text"] for r in peerlinkd.client_config("s-other")["rules"]], ["Never merge to main on a Friday."])
        self.assertIn("approved rule", self.inbox("sky")[-1])
        self.assertEqual(peerlinkd.add_rule({"text": "never merge to main on a friday."}, other)[0], 409)
        # Yours are active at once; one for another space reaches nobody here; off or deleted, it's gone.
        mine = peerlinkd.add_rule({"text": "Answer in English.", "space": "*"})[1]["rule"]
        peerlinkd.add_rule({"text": "Only for elsewhere.", "space": "elsewhere"})
        self.assertEqual(len(peerlinkd.client_config("s-sky")["rules"]), 2)
        peerlinkd.edit_rule(mine["id"], {"status": "off"})
        peerlinkd.edit_rule(rid, {"delete": True})
        self.assertEqual(peerlinkd.client_config("s-sky")["rules"], [])

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
        self.assertEqual((len(self.inbox("core")), len(self.inbox("sky"))), (2, 1))
        self.assertIn("was asked twice and hasn't answered", self.inbox("sky")[0])
        self.assertEqual(t["updated"], start)  # asking isn't an update
        # A note from the owner starts the count again; with nudges off, nobody is asked.
        peerlinkd.task_note(top, {"by": "s-core", "text": "Waiting on CI."})
        peerlinkd.inboxes.clear()
        peerlinkd.config["task_nudge_h"] = 0
        peerlinkd.last_nudge[0] = 0
        with peerlinkd.lock:
            peerlinkd.nudge_stale_tasks(t["updated"] + 5 * day)
        self.assertEqual(self.inbox("core"), [])

    def test_a_message_its_agent_is_holding_isnt_handed_out_again(self):
        self.send("sky", to="core", text="FYI: the freeze is over.")
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


if __name__ == "__main__":
    unittest.main()
