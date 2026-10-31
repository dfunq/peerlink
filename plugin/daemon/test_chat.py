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


if __name__ == "__main__":
    unittest.main()
