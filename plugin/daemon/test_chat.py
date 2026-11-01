"""Tests for the chat layer: chatlink, discordlink's shared calls, and the daemon's dispatcher, against a fake
Discord REST API. Run: python3 -m unittest discover -s plugin/daemon"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["PEERLINK_HOME"] = tempfile.mkdtemp(prefix="peerlink-chat-test-")

import chatlink  # noqa: E402
import discordlink  # noqa: E402
import peerlinkd  # noqa: E402


class FakeDiscord:
    """Stands in for discordlink.call: records each call and answers a post with a fresh message id."""

    def __init__(self):
        self.calls, self.n = [], 0

    def __call__(self, token, method, path, body=None, retried=False, files=None):
        self.calls.append((token, method, path, body, files))
        if method == "POST" and path.endswith("/messages"):
            self.n += 1
            return {"id": str(1000 + self.n)}
        return None


class ChatTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDiscord()
        self.real_call, discordlink.call = discordlink.call, self.fake
        peerlinkd.discord.update({"token": "tok", "enabled": True})

    def tearDown(self):
        discordlink.call = self.real_call
        peerlinkd.discord.update({"token": ""})
        peerlinkd.discord_bot[0] = None

    def test_platform_from_channel_id(self):
        self.assertEqual(chatlink.platform("123456789012345678"), "discord")
        self.assertEqual(chatlink.platform("tg:-1001234"), "telegram")
        self.assertEqual(chatlink.platform("slack:C0123"), "slack")
        self.assertEqual(chatlink.platform("dm"), "discord")
        self.assertEqual(chatlink.name("tg:5"), "Telegram")
        self.assertTrue(issubclass(discordlink.DiscordError, chatlink.ChatError))

    def test_post_splits_and_puts_buttons_on_the_last_part(self):
        sent = peerlinkd.chat_post("42", "x" * 3000, "7", peerlinkd.choices(["Yes", "No"], "pl:m9"))
        self.assertEqual(sent, "1002")  # the last part's id, where the buttons are
        first, last = self.fake.calls[0][3], self.fake.calls[1][3]
        self.assertEqual(first["message_reference"]["message_id"], "7")
        self.assertNotIn("components", first)
        self.assertNotIn("message_reference", last)
        ids = [b["custom_id"] for b in last["components"][0]["components"]]
        self.assertEqual(ids, ["pl:m9:0", "pl:m9:1"])
        self.assertEqual(self.fake.calls[0][2], "/channels/42/messages")

    def test_clear_buttons(self):
        peerlinkd.chat_clear_buttons("42", "1001")
        self.assertEqual(self.fake.calls, [("tok", "PATCH", "/channels/42/messages/1001", {"components": []}, None)])

    def test_answer_press_highlights_the_pick_clears_or_tells_privately(self):
        rows = [{"type": 1, "components": [{"custom_id": "pt:t1:0", "style": 1}, {"custom_id": "pt:t1:1", "style": 1}]}]
        press = {"id": "i1", "token": "it", "custom_id": "pt:t1:1", "channel_id": "42", "message": {"components": rows}}
        peerlinkd.chat_answer_press(press)
        _, _, path, body, _ = self.fake.calls[-1]
        self.assertEqual(path, "/interactions/i1/it/callback")
        self.assertEqual(body["type"], 7)
        self.assertEqual([(b["disabled"], b["style"]) for b in body["data"]["components"][0]["components"]], [(True, 2), (True, 3)])
        peerlinkd.chat_answer_press(press, clear=True)
        self.assertEqual(self.fake.calls[-1][3], {"type": 7, "data": {"components": []}})
        peerlinkd.chat_answer_press(press, "Task t1 is already done.")
        self.assertEqual(self.fake.calls[-1][3], {"type": 4, "data": {"content": "Task t1 is already done.", "flags": 64}})

    def test_platforms_not_set_up_are_refused(self):
        with self.assertRaises(chatlink.ChatError):
            peerlinkd.chat_post("tg:5", "hi")
        peerlinkd.discord["token"] = ""
        with self.assertRaises(chatlink.ChatError):
            peerlinkd.chat_post("42", "hi")
        peerlinkd.chat_react("42", "1", "👀")  # a courtesy: no error, and nothing sent
        self.assertEqual(self.fake.calls, [])

    def test_labels_name_the_platform(self):
        self.assertEqual(peerlinkd.chat_via("42", "Dillon"), "Discord (Dillon)")
        self.assertEqual(peerlinkd.chat_via("slack:C1", "Dillon"), "Slack (Dillon)")
        self.assertEqual(peerlinkd.discord_channel_label("tg:5"), "Telegram")

    def test_a_channel_can_name_one_of_the_agents_sharing_a_folder(self):
        peerlinkd.load_config()  # PEERLINK_HOME is a fresh temp folder: the defaults
        now = peerlinkd.time.time()
        for sid, name in (("s-vault", "vault"), ("s-tg", "telegram")):
            peerlinkd.sessions[sid] = {"id": sid, "name": name, "cwd": "/x/Vault", "last_seen": now}
        self.addCleanup(lambda: [peerlinkd.sessions.pop(k, None) for k in ("s-vault", "s-tg")])
        route = peerlinkd.clean_route({"channel_id": "tg:5", "agents": [
            {"cwd": "/x/Vault", "name": "telegram", "receives": True, "sends": True},
            {"cwd": "/x/Vault", "receives": False, "sends": True}]})  # a folder alone, as before 0.8.64
        self.assertEqual(len(route["agents"]), 2)
        peerlinkd.discord["routes"] = [route]
        self.addCleanup(lambda: peerlinkd.discord.update({"routes": []}))
        self.assertEqual(peerlinkd.entry_agent(route["agents"][0])["id"], "s-tg")
        self.assertEqual(peerlinkd.entry_agent(route["agents"][1])["id"], "s-vault")  # the folder's own agent
        self.assertTrue(peerlinkd.routes_for("/x/Vault", "telegram")[0]["master"])
        self.assertFalse(peerlinkd.routes_for("/x/Vault", "vault")[0]["master"])
        self.assertEqual(peerlinkd.channel_agent("tg:5", False)["id"], "s-tg")
        # Not running, the named agent's message waits for it by name, not for another agent in its folder.
        peerlinkd.sessions["s-tg"]["last_seen"] = 0
        msg = {"id": "m-test", "text": "hi", "to_cwd": "/x/Vault"}
        self.assertIsNone(peerlinkd.queue_for_entry(route["agents"][0], msg))
        self.assertEqual(msg["to"], "agent:telegram")
        peerlinkd.inboxes.pop("agent:telegram", None)

    def test_a_conversation_is_both_ends_of_every_message_in_the_log(self):
        log = peerlinkd.MESSAGES_PATH
        self.addCleanup(lambda: [os.remove(f) for f in (log, log + ".1") if os.path.exists(f)])
        rows = [(log + ".1", {"id": "m1", "ts": 1, "from": "s1", "from_name": "skyforge", "to": "s2", "to_name": "services", "text": "a"}),
                (log, {"id": "m2", "ts": 2, "from": "s3", "from_name": "services", "to": "s4", "to_name": "Skyforge", "text": "b"}),
                (log, {"id": "m2", "status": "delivered"}),  # an update to m2, as the daemon appends them
                (log, {"id": "m3", "ts": 3, "from": "discord:tg:5", "from_name": "Dillon on Telegram", "to": "s4", "to_name": "skyforge", "text": "c"})]
        for path, row in rows:
            with open(path, "a") as f:
                f.write(peerlinkd.json.dumps(row) + "\n")
        both = peerlinkd.conversation("n:services", "n:skyforge")  # by name: the same pair after a /clear
        self.assertEqual([(m["id"], m.get("status")) for m in both], [("m1", None), ("m2", "delivered")])
        self.assertEqual([m["id"] for m in peerlinkd.conversation("n:skyforge", "discord:tg:5")], ["m3"])

    def test_a_thread_under_an_open_task_may_run_three_times_as_long(self):
        peerlinkd.load_config()
        peerlinkd.config["max_hops"] = 10
        now = peerlinkd.time.time()
        for sid, name in (("s-lead", "lead"), ("s-work", "worker")):
            peerlinkd.sessions[sid] = {"id": sid, "name": name, "cwd": "/x/" + name, "last_seen": now}
        peerlinkd.tasks["t-hop"] = {"id": "t-hop", "status": "in_progress"}
        # The task, then twelve replies between the lead and its worker; the last is the lead's, at hop 12.
        thread = [{"id": "h0", "task": "t-hop", "hops": 0, "from": "s-lead", "to": "s-work", "reply_to": None}]
        for n in range(1, 13):
            a, b = ("s-work", "s-lead") if n % 2 else ("s-lead", "s-work")
            thread.append({"id": f"h{n}", "hops": n, "from": a, "to": b, "reply_to": f"h{n - 1}", "expects_reply": False})
        peerlinkd.messages.extend(thread)

        def cleanup():
            for k in ("s-lead", "s-work"):
                peerlinkd.sessions.pop(k, None)
                peerlinkd.inboxes.pop(k, None)
            peerlinkd.tasks.pop("t-hop", None)
            peerlinkd.messages[:] = [m for m in peerlinkd.messages if m not in thread and not str(m.get("text", "")).startswith("status")]
        self.addCleanup(cleanup)
        self.assertEqual(peerlinkd.hop_limit(thread[-1]), (30, "t-hop"))
        status, out = peerlinkd.send({"from": "s-work", "reply_to": "h12", "text": "status: run timed at 4 min"})
        self.assertEqual(status, 200, out)
        # Once the task is done, the thread has the usual limit, and the refusal says what to do.
        peerlinkd.tasks["t-hop"]["status"] = "done"
        self.assertEqual(peerlinkd.hop_limit(thread[-1]), (10, ""))
        status, out = peerlinkd.send({"from": "s-work", "reply_to": "h12", "text": "status: again"})
        self.assertEqual(status, 429)
        self.assertIn("max_hops=10", out["error"])
        peerlinkd.tasks["t-hop"]["status"] = "open"
        peerlinkd.config["max_hops"] = 3
        status, out = peerlinkd.send({"from": "s-work", "reply_to": "h12", "text": "status: once more"})
        self.assertIn("task t-hop hit its 9-reply limit", out["error"])


if __name__ == "__main__":
    unittest.main()
