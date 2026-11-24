"""Whole scenarios on the rig (rig.py): the real daemon in its own process, several fake agents, a /clear and a
restart. Run: python3 -m unittest discover -s plugin/daemon"""
import os
import json
import sys
import threading
import time
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rig import Agent, Rig  # noqa: E402


class ScenarioTest(unittest.TestCase):
    def rig(self, **kw):
        rig = Rig(**kw)
        rig.__enter__()
        self.addCleanup(rig.__exit__, None, None, None)
        return rig

    def test_a_message_handed_out_but_not_acked_survives_kill_9(self):
        rig = self.rig()
        chief, core, _ = self.team(rig)
        core.poll()  # what it's told on joining, acked
        chief.send(to="core", text="Already in your session.")
        self.assertEqual([m["text"] for m in core.poll()], ["Already in your session."])  # acked
        chief.send(to="core", text="Deploy is blocked on you.")
        handed = core.poll(ack=False)  # handed over; the daemon dies before the plugin's ack
        self.assertEqual([m["text"] for m in handed], ["Deploy is blocked on you."])
        rig.crash()
        again = core.poll()
        self.assertEqual([(m["id"], m["text"]) for m in again], [(handed[0]["id"], "Deploy is blocked on you.")])
        rig.crash()  # acked this time: nothing comes back
        self.assertEqual(core.poll(), [])

    def test_an_announcement_says_who_is_idle_and_wakes_them_only_when_asked(self):
        rig = self.rig()
        _, core, loop = self.team(rig)
        loop.poll()  # idle, what it's told on joining read
        out = core.send(to="space", text="Freeze on main.", kind="announcement")
        self.assertIn("1 of them is idle and won't read it until", out["note"])
        self.assertNotIn("wake", loop.expect("Freeze on main."))  # waits for its next turn
        out = core.send(to="space", text="Everyone resume.", kind="announcement", wake=True)
        self.assertEqual(out["note"], "1 idle agent starts a turn on it")
        self.assertTrue(loop.expect("Everyone resume.")["wake"])

    def test_setting_a_summary_never_takes_messages_a_new_agent_has_waiting(self):
        # A hired worker sets its first summary a second after starting: that heartbeat once took its first prompt
        # and dropped it.
        rig = self.rig()
        chief, core, _ = self.team(rig)
        core.poll()
        chief.send(to="core", text="Your first prompt.")
        for body in ({"summary": "starting"}, {"summary": "starting", "inbox": False, "ack": []}):
            code, out = rig.call("POST", f"/api/sessions/{core.sid}/heartbeat", body)
            self.assertEqual((code, out.get("inbox", [])), (200, []))
        self.assertEqual([m["text"] for m in core.poll()], ["Your first prompt."])

    def test_an_answer_from_another_machine_threads_onto_the_question(self):
        """Alice on host a asks bob on host b; bob answers with reply_to alone, and the answer comes back
        to alice as the answer to her question, which no longer reads as waiting."""
        ports = [__import__("rig").free_port() for _ in range(2)]
        a = Rig(config={"token": "t0k", "remotes": [f"http://127.0.0.1:{ports[1]}"]}, env={"PEERLINK_HOSTNAME": "hosta"})
        b = Rig(config={"token": "t0k", "remotes": [f"http://127.0.0.1:{ports[0]}"]}, env={"PEERLINK_HOSTNAME": "hostb"})
        for rig, port in zip((a, b), ports):
            rig.port = port
            rig.env["PEERLINK_PORT"] = str(port)
            rig.__enter__()
            self.addCleanup(rig.__exit__, None, None, None)
        alice, bob = a.agent("alice"), b.agent("bob")
        alice.poll(), bob.poll()
        sent = alice.send(to="bob@hostb", text="Is the build green?", expects_reply=True)
        self.assertEqual(sent["to"], "bob@hostb")
        q = bob.expect("Is the build green?")
        self.assertEqual(q["from_name"], "alice@hosta")
        self.assertEqual(a.call("GET", f"/api/sessions/{alice.sid}/threads")[1]["waiting"][0]["with"], "bob@hostb")
        bob.send(reply_to=q["id"], text="Green.")  # no "to": reply_to says who
        ans = alice.expect("Green.")
        mine = {m["id"]: m for m in a.call("GET", "/api/messages")[1]["messages"]}
        self.assertEqual(mine[ans["reply_to"]]["text"], "Is the build green?")
        self.assertEqual(mine[ans["reply_to"]]["status"], "forwarded")
        self.assertEqual(a.call("GET", f"/api/sessions/{alice.sid}/threads")[1]["waiting"], [])
        # And alice's answer to that threads onto bob's message on b.
        alice.send(reply_to=ans["id"], text="Thanks.")
        back = bob.expect("Thanks.")
        theirs = {m["id"]: m for m in b.call("GET", "/api/messages")[1]["messages"]}
        self.assertEqual(theirs[back["reply_to"]]["text"], "Green.")
        # A send tried again after a timeout that did arrive stays one message.
        fwd = {"from": "x@hosta", "from_name": "x@hosta", "from_host": "hosta", "to": bob.sid, "text": "once", "origin_id": "m999"}
        first, again = b.call("POST", "/api/messages", fwd)[1], b.call("POST", "/api/messages", fwd)[1]
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(sum(m["text"] == "once" for m in bob.poll()), 1)

    def test_discord_settings_changed_at_once_lose_nothing(self):
        rig = self.rig()
        rig.signed_in()
        errors = []

        def change(i):
            try:
                if i % 5 == 4:
                    code, out = rig.call("DELETE", "/api/discord", user=True)
                else:
                    field = ("allowed_users", "trusted_bots")[i % 2]
                    code, out = rig.call("PUT", "/api/discord", {field: [str(10 ** 17 + i)], "max_file_mb": 1 + i}, user=True)
                if code != 200:
                    errors.append((i, code, out))
            except Exception as e:  # a reset or a 500 from a write racing another
                errors.append((i, repr(e)))
        threads = [threading.Thread(target=change, args=(i,)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        self.assertEqual(errors, [])
        rig.user("PUT", "/api/discord", {"allowed_users": ["123456789012345678"], "trusted_bots": ["223456789012345678"]})
        with open(os.path.join(rig.home, "discord.json")) as f:
            saved = json.load(f)  # whole JSON, whatever the order the writes landed in
        self.assertEqual((saved["allowed_users"], saved["trusted_bots"]), (["123456789012345678"], ["223456789012345678"]))
        self.assertEqual([n for n in os.listdir(rig.home) if n.endswith(".tmp")], [])
        rig.crash()  # and it reads back after a kill -9
        got = rig.user("GET", "/api/discord")
        self.assertEqual((got["allowed_users"], got["trusted_bots"]), (["123456789012345678"], ["223456789012345678"]))

    def team(self, rig):
        """A lead at the top, a lead of a space inside it, and that space's worker: the chain work is relayed along."""
        chief = rig.agent("chief", space="top")
        core = rig.agent("core", space="core", parent="top")
        loop = rig.agent("loop", space="core", parent="top")
        return chief, core, loop

    def started_agent(self, rig, name, sid, space, parent):
        """An agent a spawn started: its space comes from the request, in a folder whose .peerlink says `space`/`parent`."""
        agent = Agent(rig, name, rig.folder(name, space, parent))
        agent.sid = sid
        agent.register()
        rig.agents.append(agent)
        agent.poll()
        return agent

    def test_a_nested_lead_restarted_by_a_spawn_stays_inside_its_parent(self):
        """i63: a spawn names the space, and the folder's .peerlink still says which space that one sits inside."""
        sid = str(uuid.uuid4())
        rig = self.rig(config={"sessions": {sid: {"started_in": "core"}}})
        chief = rig.agent("chief", space="top")
        core = self.started_agent(rig, "core", sid, "core", "top")
        chief.send(to="core", text="By name.")
        core.expect("By name.")
        chief.send(to="space:core", text="By space.")
        core.expect("By space.")
        core.send(to="chief", text="Back up the tree.")
        chief.expect("Back up the tree.")

    def test_a_parent_line_only_counts_for_the_space_its_file_names(self):
        """An agent started in another space doesn't inherit the parent of the space its folder's .peerlink names."""
        sid = str(uuid.uuid4())
        rig = self.rig(config={"sessions": {sid: {"started_in": "elsewhere"}}})
        chief = rig.agent("chief", space="top")
        self.started_agent(rig, "core", sid, "core", "top")
        code, out = chief.send(False, to="core", text="hi")
        self.assertEqual(code, 409)
        self.assertIn("can't reach", out["error"])

    def test_only_your_browser_says_who_may_approve_for_you(self):
        """Delegated approvals are the owner's: an agent holding the key can't switch them on or grant a client, though it
        may read them, check an approval, or take one back."""
        rig = self.rig()
        for path, body in (("/api/agentchannel/delegation", {"switch": True}),
                           ("/api/agentchannel/delegation/acme-bot", {"on": True, "scopes": ["jira"]})):
            code, out = rig.call("PUT", path, body)
            self.assertEqual((code, out["error"]), (403, "delegated approvals are set from the web UI"))
        self.assertFalse(rig.call("GET", "/api/agentchannel/delegation")[1]["switch"])
        self.assertTrue(rig.user("PUT", "/api/agentchannel/delegation", {"switch": True})["switch"])
        self.assertEqual(rig.call("GET", "/api/agentchannel/delegation/acme-bot")[0], 404)
        # An agent must say which message it came on and who it is; only your browser looks one up bare.
        code, out = rig.call("GET", "/api/approvals/da1")
        self.assertEqual(code, 400)
        self.assertIn("needs the message", out["error"])
        code, out = rig.call("GET", "/api/approvals/da1?message=m1&by=x")
        self.assertEqual(code, 404)
        self.assertIn("isn't approved", out["error"])
        self.assertEqual(rig.call("POST", "/api/approvals/da1/revoke", {})[0], 404)

    def test_an_agent_closes_spaces_to_herdr_joins_but_only_you_open_one(self):
        rig = self.rig()
        self.assertEqual(rig.call("PUT", "/api/config", {"herdr_join_never": "ops"})[0], 200)
        self.assertEqual(rig.call("PUT", "/api/config", {"herdr_join_never": "ops, vault"})[0], 200)
        code, out = rig.call("PUT", "/api/config", {"herdr_join_never": "vault"})
        self.assertEqual((code, out["error"]), (403, "only the web UI takes a space off herdr_join_never"))
        self.assertEqual(rig.user("PUT", "/api/config", {"herdr_join_never": ""})["herdr_join_never"], "")

    def test_work_goes_down_a_tree_of_tasks_and_word_comes_back_without_a_relay(self):
        rig = self.rig()
        chief, core, loop = self.team(rig)
        # The top lead reaches the nested space only through its lead, and is told who works under it.
        self.assertEqual(sorted(chief.peers()), ["core"])
        self.assertEqual(chief.peers()["core"]["inside"], ["loop"])
        code, out = chief.send(False, to="loop", text="hi")
        self.assertEqual(code, 409)
        self.assertIn("loop", out["error"])

        top = chief.send(to="core", kind="task", text="Ship the template engine")["task"]
        core.expect("Ship the template engine")
        sub = core.send(to="loop", kind="task", text=f"Backend PR for {top}")["task"]
        loop.expect("Backend PR")

        # One note from the top reaches the worker two levels down; the lead between them sends nothing.
        code, out = chief.call("POST", f"/api/tasks/{top}/note", {"text": "Use a merge commit."})
        self.assertEqual((code, sorted(out["told"])), (200, ["core", "loop"]))
        self.assertIn(f"through your task {sub}", loop.expect("Use a merge commit.")["text"])

        # The worker's blocker is on the record the top lead reads, though the two can't message each other.
        loop.call("POST", f"/api/tasks/{sub}/note", {"text": "Need the dev key.", "blocked": True})
        core.expect("Need the dev key.")
        code, view = chief.call("GET", f"/api/tasks/{top}?by={chief.sid}")
        self.assertEqual([(t["id"], t["blocked"]) for t in view["below"]], [(sub, "Need the dev key.")])

        # The user's answer to the top lead's question goes to everyone under the task as well.
        asked = chief.send(to="user", expects_reply=True, text=f"Raise the dev key's limit for {top}?")
        self.assertEqual([i["id"] for i in rig.user("GET", "/api/inbox")["items"]], [asked["id"]])
        rig.user("POST", "/api/inbox", {"kind": "question", "id": asked["id"], "action": "reply", "text": "Yes, raise it."})
        for agent in (chief, core, loop):
            agent.expect("Yes, raise it.")
        self.assertEqual(rig.user("GET", "/api/inbox")["items"], [])

        # Closing the subtask tells whoever asked for it, and the record shows it closed.
        loop.call("POST", f"/api/tasks/{sub}", {"status": "done", "note": "Merged as abc123."})
        core.expect("Merged as abc123.")
        _, view = chief.call("GET", f"/api/tasks/{top}?by={chief.sid}")
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

    def test_moving_one_agent_to_another_space_leaves_the_others_in_its_folder(self):
        rig = self.rig()
        shared = rig.folder("app", space="acme")
        app, helper = rig.agent("app", folder=shared), rig.agent("helper", folder=shared)
        ops = rig.agent("ops", space="infra")
        rig.user("POST", "/api/folders/space", {"cwd": shared, "id": helper.sid, "space": "infra"})
        helper.expect("Your user moved you to the infra space")
        self.assertNotIn("moved", " ".join(app.texts()))
        spaces = {p["name"]: (p["space"], p["space_source"]) for p in rig.user("GET", "/api/peers")["peers"]}
        self.assertEqual(spaces["app"], ("acme", "file"))
        self.assertEqual(spaces["helper"], ("infra", "agent"))
        self.assertEqual(sorted(ops.peers()), ["helper"])
        # It's the agent's, not the session's: still in infra after a /clear.
        helper.clear()
        self.assertEqual(sorted(ops.peers()), ["helper"])
        # Unassigned: out of every space, though its folder's .peerlink says acme.
        rig.user("POST", "/api/folders/space", {"cwd": shared, "id": helper.sid, "space": "unassigned"})
        helper.expect("you're unassigned")
        self.assertEqual(ops.peers(), {})
        self.assertEqual(app.peers(), {})
        # Cleared, it goes back to its folder's space.
        rig.user("POST", "/api/folders/space", {"cwd": shared, "id": helper.sid, "space": ""})
        self.assertEqual(ops.peers(), {})
        self.assertEqual(sorted(app.peers()), ["helper"])

    def test_adding_one_agent_to_another_space_leaves_the_others_in_its_folder(self):
        rig = self.rig()
        shared = rig.folder("app", space="acme")
        app, helper = rig.agent("app", folder=shared), rig.agent("helper", folder=shared)
        ops = rig.agent("ops", space="infra")
        rig.user("POST", "/api/members", {"cwd": shared, "id": helper.sid, "spaces": ["infra"]})
        helper.expect("you're now also a member of infra")
        self.assertNotIn("member", " ".join(app.texts()))
        also = {p["name"]: p.get("also", []) for p in rig.user("GET", "/api/peers")["peers"]}
        self.assertEqual(also["helper"], ["infra"])
        self.assertEqual(also["app"], [])
        self.assertEqual(sorted(ops.peers()), ["helper"])
        # Cleared, for that agent alone.
        rig.user("POST", "/api/members", {"cwd": shared, "id": helper.sid, "spaces": []})
        self.assertEqual(ops.peers(), {})

    def test_two_agents_you_link_message_each_other_across_spaces_and_nobody_else_does(self):
        rig = self.rig()
        core, other = rig.agent("core", space="core"), rig.agent("other", space="core")
        services = rig.agent("services", space="mac")
        code, out = services.send(ok=False, to="core", text="before")
        self.assertEqual(code, 409)
        # Only you, in the browser.
        code, _ = rig.call("POST", "/api/links", {"id": services.sid, "names": ["core"]})
        self.assertEqual(code, 403)
        rig.user("POST", "/api/links", {"id": services.sid, "names": ["core"]})
        services.expect("Your user linked you with core")
        core.expect("Your user linked you with services")
        self.assertEqual(sorted(services.peers()), ["core"])
        self.assertIn("services", core.peers())
        self.assertNotIn("services", other.peers())
        services.send(to="core", text="the key is ready")
        core.expect("the key is ready")
        core.send(to="services", text="thanks")
        services.expect("thanks")
        code, _ = services.send(ok=False, to="other", text="not linked")
        self.assertEqual(code, 409)
        links = {p["name"]: p.get("links", []) for p in rig.user("GET", "/api/peers")["peers"]}
        self.assertEqual((links["services"], links["core"], links["other"]), (["core"], ["services"], []))
        # The agent's, not the session's: kept through a /clear.
        core.clear()
        self.assertEqual(sorted(services.peers()), ["core"])
        code, out = rig.call("POST", "/api/links", {"id": services.sid, "names": ["nobody"]}, user=True)
        self.assertEqual((code, out["error"]), (400, "no agent is called nobody"))
        # Removed, both are told and the space rule is back.
        rig.user("POST", "/api/links", {"id": services.sid, "names": []})
        services.expect("Your user removed your link with core")
        core.expect("Your user removed your link with services")
        self.assertEqual(services.peers(), {})
        code, _ = services.send(ok=False, to="core", text="after")
        self.assertEqual(code, 409)

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

    def test_a_session_resumed_in_another_folder_is_a_new_agent_there_and_leaves_the_old_one_whole(self):
        rig = self.rig()
        lead = rig.agent("lead", space="acme")
        loop, pixel = rig.agent("loop", space="acme"), rig.agent("pixel", space="acme")
        rig.user("POST", f"/api/sessions/{loop.sid}/config", {"role": "Owns the loop service."})
        agent = lead.peers()["loop"]["agent"]

        # A message for loop that it never reads; Claude Code quits; then `claude --resume <loop's id>` runs in pixel's folder.
        lead.send(to="loop", text="Ship the retry fix")
        loop.quit()
        moved = Agent(rig, "pixel", pixel.cwd)
        moved.sid = loop.sid
        moved.register()
        self.assertEqual(moved.name, "pixel-2")
        self.assertFalse(moved.config.get("role"))
        self.assertNotIn("Ship the retry fix", moved.texts())
        seen = lead.peers()
        self.assertEqual(seen["pixel-2"]["cwd"], pixel.cwd)
        self.assertNotEqual(seen["pixel-2"]["agent"], agent)
        self.assertNotIn("loop", seen)

        # loop starts again in its own folder: the same agent, with its role and the message that waited.
        loop.restart()
        self.assertEqual(loop.name, "loop")
        self.assertEqual(loop.config.get("role"), "Owns the loop service.")
        loop.expect("Ship the retry fix")
        self.assertEqual(lead.peers()["loop"]["agent"], agent)

    def test_a_session_resumed_in_another_folder_leaves_its_old_name_behind_across_a_daemon_restart(self):
        rig = self.rig()
        lead = rig.agent("lead", space="acme")
        loop, pixel = rig.agent("loop", space="acme"), rig.agent("pixel", space="acme")
        loop.quit()

        def register(sid, name, cwd, had):  # as the plugin does: it says the name it last had
            code, out = rig.call("POST", "/api/sessions", {"id": sid, "name": name, "had_name": had, "cwd": cwd, "plugin": "rig"})
            self.assertEqual(code, 200, out)
            return out["name"]
        # Resumed in pixel's folder, its plugin still says it was loop: that name stays with loop's folder.
        self.assertEqual(register(loop.sid, "pixel", pixel.cwd, "loop"), "pixel-2")
        # A new session in loop's folder is loop, and a daemon restart doesn't swap them, whoever registers first.
        fresh = Agent(rig, "loop", loop.cwd)
        fresh.register()
        self.assertEqual(fresh.name, "loop")
        rig.restart()
        self.assertEqual(register(fresh.sid, "loop", loop.cwd, None), "loop")  # a reloaded plugin has no name to say
        self.assertEqual(register(loop.sid, "pixel", pixel.cwd, "pixel-2"), "pixel-2")
        self.assertEqual(lead.peers()["loop"]["cwd"], loop.cwd)

    def test_a_plugin_that_reloads_as_it_restarts_the_daemon_keeps_its_agents_name(self):
        # A hired worker named pg-client in app-wt-x: its plugin reloaded onto a new version and restarted the daemon
        # in the same moment, so neither remembered the name, and it came back as its folder's.
        rig = self.rig()
        folder = rig.folder("app-wt-x")
        worker = Agent(rig, "pg-client", folder)
        worker.register()
        self.assertEqual(worker.name, "pg-client")
        rig.restart()
        code, out = rig.call("POST", "/api/sessions", {"id": worker.sid, "name": "app-wt-x", "had_name": None, "cwd": folder, "plugin": "rig"})
        self.assertEqual((code, out["name"]), (200, "pg-client"))

    def test_one_folder_spelled_two_ways_stays_one_agent(self):
        rig = self.rig()
        lead = rig.agent("lead", space="acme")
        real = rig.folder("loop", space="acme")
        link = real + "-link"
        os.symlink(real, link)
        loop = rig.agent("loop", folder=real)
        rig.user("POST", f"/api/sessions/{loop.sid}/config", {"role": "Owns the loop service."})
        agent = lead.peers()["loop"]["agent"]
        lead.send(to="loop", text="Ship the retry fix")
        loop.quit()
        # The same session, the same folder, by its symlink and with a trailing slash.
        for spelled in (link, real + "/"):
            again = Agent(rig, "loop", spelled)
            again.sid = loop.sid
            again.register()
            self.assertEqual((again.name, again.config.get("role")), ("loop", "Owns the loop service."))
            self.assertEqual(lead.peers()["loop"]["agent"], agent)
        again.expect("Ship the retry fix")

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
        # And it goes on serving: the only message is the one saying it leads acme, being alone in it.
        self.assertTrue(all("as the only agent in it" in m["text"] for m in lead.poll()))

    def test_a_heartbeat_that_waits_comes_back_when_a_message_does_and_keeps_the_agent_live_meanwhile(self):
        rig = self.rig(config={"peer_ttl_s": 1})
        sender, waiter = rig.agent("sender", space="acme"), rig.agent("waiter", space="acme")
        beat = lambda wait: rig.call("POST", f"/api/sessions/{waiter.sid}/heartbeat", {"busy": False, "ack": [], "wait": wait})

        start = time.time()
        code, out = beat(0.4)
        self.assertEqual((code, out["inbox"]), (200, []))
        self.assertGreaterEqual(time.time() - start, 0.35)

        got, start = {}, time.time()
        t = threading.Thread(target=lambda: got.update(out=beat(8)[1], took=time.time() - start))
        t.start()
        time.sleep(1.6)  # past peer_ttl_s: only the hold keeps it listed
        sender.poll()
        self.assertIn("waiter", sender.peers())
        sender.send(to="waiter", text="here it is")
        t.join(6)
        self.assertEqual([m["text"] for m in got["out"]["inbox"]], ["here it is"])
        self.assertLess(got["took"], 4)

        # Nonsense isn't a wait, and a second hold for the same agent doesn't stack.
        start = time.time()
        for wait in ("soon", True, -1, 0):
            self.assertEqual(beat(wait)[0], 200)
        self.assertLess(time.time() - start, 1)


    def test_two_live_sessions_resuming_one_conversation_never_take_turns(self):
        """The conversation resumed in a second terminal while the first still runs is another agent there."""
        rig = self.rig()
        cwd = rig.folder("pixelgrid")
        reg = lambda sid, term, pid, succeeds=None: rig.call("POST", "/api/sessions", {
            "id": sid, "name": "pixelgrid", "had_name": "pixelgrid" if succeeds else None, "cwd": cwd, "plugin": "rig",
            "terminal": term, "pid": pid, "succeeds": succeeds})
        self.assertEqual(reg("old", "tmux:%0@1", os.getppid())[1]["name"], "pixelgrid")
        self.assertEqual(reg("new", "hc:t7@2", os.getpid(), succeeds="old")[1]["name"], "pixelgrid-2")
        for _ in range(3):
            for sid in ("old", "new"):
                code, out = rig.call("POST", f"/api/sessions/{sid}/heartbeat", {"busy": False, "ack": []})
                self.assertEqual(code, 200, out)
        names = {p["id"]: p["name"] for p in rig.call("GET", "/api/peers?local=1")[1]["peers"]}
        self.assertEqual((names.get("old"), names.get("new")), ("pixelgrid", "pixelgrid-2"))
        self.assertIn("Two Claude sessions tried to be pixelgrid", rig.output())

if __name__ == "__main__":
    unittest.main()
