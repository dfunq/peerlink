"""Whole scenarios on the rig (rig.py): the real daemon in its own process, several fake agents, a /clear and a
restart. Run: python3 -m unittest discover -s plugin/daemon"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rig import Rig  # noqa: E402


class ScenarioTest(unittest.TestCase):
    def rig(self, **kw):
        rig = Rig(**kw)
        rig.__enter__()
        self.addCleanup(rig.__exit__, None, None, None)
        return rig

    def team(self, rig):
        """A lead at the top, a lead of a space inside it, and that space's worker: the chain work is relayed along."""
        sky = rig.agent("sky", space="top")
        core = rig.agent("core", space="core", parent="top")
        loop = rig.agent("loop", space="core", parent="top")
        return sky, core, loop

    def test_work_goes_down_a_tree_of_tasks_and_word_comes_back_without_a_relay(self):
        rig = self.rig()
        sky, core, loop = self.team(rig)
        # The top lead reaches the nested space only through its lead, and is told who works under it.
        self.assertEqual(sorted(sky.peers()), ["core"])
        self.assertEqual(sky.peers()["core"]["inside"], ["loop"])
        code, out = sky.send(False, to="loop", text="hi")
        self.assertEqual(code, 409)
        self.assertIn("loop", out["error"])

        top = sky.send(to="core", kind="task", text="Ship the template engine")["task"]
        core.expect("Ship the template engine")
        sub = core.send(to="loop", kind="task", text=f"Backend PR for {top}")["task"]
        loop.expect("Backend PR")

        # One note from the top reaches the worker two levels down; the lead between them sends nothing.
        code, out = sky.call("POST", f"/api/tasks/{top}/note", {"text": "Use a merge commit."})
        self.assertEqual((code, sorted(out["told"])), (200, ["core", "loop"]))
        self.assertIn(f"through your task {sub}", loop.expect("Use a merge commit.")["text"])

        # The worker's blocker is on the record the top lead reads, though the two can't message each other.
        loop.call("POST", f"/api/tasks/{sub}/note", {"text": "Need the dev key.", "blocked": True})
        core.expect("Need the dev key.")
        code, view = sky.call("GET", f"/api/tasks/{top}?by={sky.sid}")
        self.assertEqual([(t["id"], t["blocked"]) for t in view["below"]], [(sub, "Need the dev key.")])

        # The user's answer to the top lead's question goes to everyone under the task as well.
        asked = sky.send(to="user", expects_reply=True, text=f"Raise the dev key's limit for {top}?")
        self.assertEqual([i["id"] for i in rig.user("GET", "/api/inbox")["items"]], [asked["id"]])
        rig.user("POST", "/api/inbox", {"kind": "question", "id": asked["id"], "action": "reply", "text": "Yes, raise it."})
        for agent in (sky, core, loop):
            agent.expect("Yes, raise it.")
        self.assertEqual(rig.user("GET", "/api/inbox")["items"], [])

        # Closing the subtask tells whoever asked for it, and the record shows it closed.
        loop.call("POST", f"/api/tasks/{sub}", {"status": "done", "note": "Merged as abc123."})
        core.expect("Merged as abc123.")
        _, view = sky.call("GET", f"/api/tasks/{top}?by={sky.sid}")
        self.assertEqual((view["below"][0]["status"], view["below"][0]["last"]["kind"]), ("done", "closed"))

    def test_an_agent_that_clears_its_context_keeps_its_name_its_tasks_and_its_replies(self):
        rig = self.rig()
        lead, worker = rig.agent("lead", space="acme"), rig.agent("worker", space="acme")
        task = lead.send(to="worker", kind="task", text="Add /health")["task"]
        worker.expect("Add /health")
        asked = worker.send(to="lead", expects_reply=True, text="Which port?")
        lead.expect("Which port?")

        old = worker.clear()
        self.assertEqual(worker.name, "worker")
        self.assertEqual(sorted(lead.peers()), ["worker"])  # one worker, not two
        self.assertEqual(lead.peers()["worker"]["id"], worker.sid)
        # A reply to what it asked before the clear reaches the new session.
        lead.send(reply_to=asked["id"], text="8080.")
        self.assertEqual(worker.expect("8080.")["reply_to"], asked["id"])
        # Its task is still its own to close, and the lead still hears.
        code, out = worker.call("POST", f"/api/tasks/{task}", {"status": "done", "note": "Added."})
        self.assertEqual(code, 200, out)
        lead.expect("Added.")
        self.assertNotEqual(old, worker.sid)

    def test_a_daemon_restart_loses_no_message_task_rule_or_name(self):
        rig = self.rig()
        lead, worker = rig.agent("lead", space="acme"), rig.agent("worker", space="acme")
        task = lead.send(to="worker", kind="task", text="Rotate the key")["task"]
        lead.send(to="worker", text="Sent before the restart, read after it.")
        rig.user("POST", "/api/rules", {"space": "acme", "text": "Never merge on a Friday."})
        rig.restart()

        got = [m["text"] for m in rig.wait(worker.poll, "the worker's messages after the restart")]
        self.assertIn("Rotate the key", got[0])
        self.assertIn("Sent before the restart, read after it.", got)
        self.assertEqual(worker.name, "worker")
        self.assertEqual([r["text"] for r in worker.config["rules"]], ["Never merge on a Friday."])
        lead.poll()
        self.assertEqual(sorted(lead.peers()), ["worker"])
        code, out = worker.call("POST", f"/api/tasks/{task}", {"status": "done", "note": "Rotated."})
        self.assertEqual(code, 200, out)
        lead.expect("Rotated.")
        # And a new message after it gets an id above the old ones.
        self.assertGreater(int(lead.send(to="worker", text="After.")["id"][1:]), 2)

    def test_a_message_is_handed_out_again_until_acked_but_not_while_it_is_held(self):
        rig = self.rig()  # the rig's daemon hands out again after 1 s
        lead, worker = rig.agent("lead", space="acme"), rig.agent("worker", space="acme")
        mid = lead.send(to="worker", text="FYI")["id"]
        self.assertEqual([m["id"] for m in worker.poll(ack=False)], [mid])
        # Held for its next turn: the daemon is told, and doesn't send it again.
        for _ in range(4):
            time.sleep(0.4)
            self.assertEqual(worker.poll(ack=False, holding=[mid]), [])
        # It stops saying so (a reload dropped it): the message comes back.
        again = rig.wait(lambda: worker.poll(ack=False), "the message to be handed out again", 4)
        self.assertEqual([m["id"] for m in again], [mid])
        rig.call("POST", f"/api/sessions/{worker.sid}/ack", {"ids": [mid]})
        time.sleep(1.3)
        self.assertEqual(worker.poll(ack=False), [])

    def test_two_agents_in_one_folder_each_get_their_own_replies_and_tasks(self):
        rig = self.rig()
        lead = rig.agent("lead", space="acme")
        shared = rig.folder("app", space="acme")
        first, second = rig.agent("app", folder=shared), rig.agent("helper", folder=shared)
        self.assertEqual(sorted(lead.peers()), ["app", "helper"])

        asked = second.send(to="lead", expects_reply=True, text="Is staging free?")
        lead.expect("Is staging free?")
        lead.send(reply_to=asked["id"], text="Yes, until 5.")
        second.expect("Yes, until 5.")
        self.assertNotIn("Yes, until 5.", first.texts())

        task = lead.send(to="helper", kind="task", text="Seed the database")["task"]
        second.expect("Seed the database")
        self.assertNotIn("Seed the database", " ".join(first.texts()))
        self.assertEqual(first.call("POST", f"/api/tasks/{task}", {"status": "done"})[0], 403)  # not the folder's: the agent's
        self.assertEqual(second.call("POST", f"/api/tasks/{task}", {"status": "done", "note": "Seeded."})[0], 200)
        # The second one clears: it is still "helper", and the first is still "app".
        second.clear()
        self.assertEqual(sorted(lead.peers()), ["app", "helper"])
        lead.send(to="helper", text="Thanks.")
        second.expect("Thanks.")
        self.assertNotIn("Thanks.", first.texts())

    def test_you_can_make_either_of_two_agents_in_one_folder_the_lead(self):
        rig = self.rig()
        shared = rig.folder("app", space="acme")
        app, helper = rig.agent("app", folder=shared), rig.agent("helper", folder=shared)
        other = rig.agent("other", space="acme")
        # app, going by the folder's name, leads by default; you pick the other agent in the folder instead.
        rig.user("POST", "/api/spaces/acme/lead", {"cwd": shared})
        self.assertEqual(rig.user("GET", "/api/peers")["leads"]["acme"]["name"], "app")
        rig.user("POST", "/api/spaces/acme/lead", {"cwd": shared, "id": helper.sid})
        self.assertEqual(rig.user("GET", "/api/peers")["leads"]["acme"]["name"], "helper")
        self.assertEqual({n: p.get("leads") for n, p in other.peers().items()}, {"app": "", "helper": "set"})
        helper.expect("You now lead the acme space")
        app.expect("You no longer lead the acme space: helper does")
        # Work sent to the space reaches helper, and still does after it clears its context.
        other.send(to="space", text="Who owns billing?")
        helper.expect("Who owns billing?")
        self.assertNotIn("Who owns billing?", app.texts())
        helper.clear()
        other.send(to="space", text="And invoices?")
        helper.expect("And invoices?")
        self.assertNotIn("And invoices?", app.texts())

    def test_a_lead_you_clear_stays_cleared_and_a_lone_agent_is_told_it_leads(self):
        rig = self.rig()
        solo = rig.agent("solo", space="acme")
        solo.poll()
        solo.expect("You now lead the acme space, as the only agent in it")
        self.assertEqual(rig.user("GET", "/api/peers")["leads"]["acme"]["name"], "solo")
        rig.user("POST", "/api/spaces/acme/lead", {"cwd": ""})
        solo.expect("You no longer lead the acme space: it has no lead now")
        for _ in range(3):
            solo.poll()
        self.assertNotIn("acme", rig.user("GET", "/api/peers")["leads"])
        self.assertEqual(sum("as the only agent in it" in t for t in solo.texts()), 1)

    def test_an_agent_is_the_same_agent_after_claude_code_restarts_even_sharing_a_folder(self):
        rig = self.rig()
        lead = rig.agent("lead", space="acme")
        shared = rig.folder("app", space="acme")
        app, helper = rig.agent("app", folder=shared), rig.agent("helper", folder=shared)
        rig.user("POST", f"/api/sessions/{helper.sid}/config", {"role": "Seeds and migrates the database."})
        app.summary("Building the invoice page")
        helper.summary("Migrating the orders table")
        task = lead.send(to="helper", kind="task", text="Migrate the orders table")["task"]
        helper.expect("Migrate the orders table")
        asked = helper.send(to="lead", expects_reply=True, text="Lock the table first?")
        lead.expect("Lock the table first?")

        # helper quits; the answer to its question arrives while it's away, and app restarts first.
        gone = helper.sid
        helper.quit()
        lead.send(reply_to=asked["id"], text="Yes, lock it.")
        app.restart()
        self.assertEqual(app.name, "app")
        self.assertNotIn("Yes, lock it.", app.texts())  # another agent's, though it's in the same folder

        helper.restart()
        self.assertEqual(helper.name, "helper")
        self.assertEqual(helper.expect("Yes, lock it.")["reply_to"], asked["id"])
        self.assertEqual(helper.config["role"], "Seeds and migrates the database.")
        lead.poll(), app.poll()
        seen = lead.peers()
        self.assertEqual((seen["helper"]["summary"], seen["app"]["summary"]), ("Migrating the orders table", "Building the invoice page"))
        self.assertEqual(seen["helper"]["id"], helper.sid)
        self.assertNotEqual(helper.sid, gone)
        # Each is one agent across its sessions, and they are two agents.
        self.assertTrue(seen["helper"]["agent"] and seen["helper"]["agent"] != seen["app"]["agent"])
        # The task is still helper's alone to close.
        self.assertEqual(app.call("POST", f"/api/tasks/{task}", {"status": "done"})[0], 403)
        self.assertEqual(helper.call("POST", f"/api/tasks/{task}", {"status": "done", "note": "Migrated."})[0], 200)
        lead.expect("Migrated.")

        # The same holds across a daemon restart in between: who an agent is doesn't live in the daemon's memory.
        asked = helper.send(to="lead", expects_reply=True, text="Drop the old column too?")
        lead.expect("Drop the old column too?")
        agent = seen["helper"]["agent"]
        helper.clear()
        rig.restart()
        lead.poll(), helper.poll(), app.poll()
        lead.send(reply_to=asked["id"], text="Not yet.")
        helper.expect("Not yet.")
        self.assertNotIn("Not yet.", app.texts())
        self.assertEqual(lead.peers()["helper"]["agent"], agent)

    def test_chatter_stops_at_the_reply_limit_and_work_on_a_task_carries_on(self):
        rig = self.rig(config={"max_hops": 4})
        a, b = rig.agent("a", space="acme"), rig.agent("b", space="acme")

        def volley(first, limit):
            """Replies back and forth from `first`; how many went through before one was refused."""
            last, pair, n = first, (b, a), 0
            while n < limit:
                code, out = pair[n % 2].send(False, reply_to=last, text=f"reply {n + 1}")
                if code != 200:
                    return n, out["error"]
                last, n = out["id"], n + 1
                pair[n % 2].poll()
            return n, ""

        sent, why = volley(a.send(to="b", text="Morning.")["id"], 20)
        self.assertEqual(sent, 4)
        self.assertIn("max_hops=4", why)
        task = a.send(to="b", kind="task", text="Migrate the table")
        sent, why = volley(task["id"], 20)
        self.assertEqual(sent, 12)  # three times as long while the task is open
        self.assertIn(f"task {task['task']} hit its 12-reply limit", why)

    def test_a_request_the_daemon_fails_on_says_why_in_its_answer_and_its_log(self):
        rig = self.rig()
        lead = rig.agent("lead", space="acme")
        # A one-time schedule is shown with its day: the format Windows lacks is not used.
        import peerlinkd
        self.assertEqual(peerlinkd.when_text({"at": 86400 * 400}).split()[:1], ["once,"])
        self.assertNotIn("%", peerlinkd.when_text({"at": 86400 * 400}))
        code, out = rig.call("GET", "/api/messages?limit=abc")
        self.assertEqual(code, 400)
        self.assertIn("bad request: ValueError", out["error"])
        self.assertIn("peerlinkd.py:", out["error"])
        self.assertIn("request failed: GET /api/messages ValueError", rig.output())
        self.assertEqual(lead.poll(), [])  # and it goes on serving


if __name__ == "__main__":
    unittest.main()
