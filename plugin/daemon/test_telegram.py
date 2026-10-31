"""Tests for telegramlink and the daemon's Telegram wiring, against a fake Bot API.
Run: python3 -m unittest discover -s plugin/daemon"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PEERLINK_HOME", tempfile.mkdtemp(prefix="peerlink-telegram-test-"))

import telegramlink  # noqa: E402
import peerlinkd  # noqa: E402

TOKEN = "123456:" + "A" * 30


class FakeTelegram:
    """The Bot API in memory: records each call, answers getUpdates from a queue, and refuses HTML it's told to."""

    def __init__(self):
        self.calls, self.updates, self.n, self.bad_html = [], [], 100, False
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                data = b"file bytes" if self.path.startswith(f"/file/bot{TOKEN}/") else b""
                self.send_response(200 if data else 404)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                method = self.path.rsplit("/", 1)[-1]
                params = json.loads(raw) if self.headers.get("Content-Type") == "application/json" else {"multipart": raw}
                ok, out = outer.answer(method, params)
                data = json.dumps({"ok": True, "result": out} if ok else {"ok": False, "error_code": 400, "description": out}).encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def answer(self, method, params):
        if method == "getUpdates":
            for _ in range(20):
                if self.updates:
                    out, self.updates = self.updates, []
                    return True, out
                time.sleep(0.02)
            return True, []
        self.calls.append((method, params))
        if method == "getMe":
            return True, {"id": 999, "username": "plbot", "can_read_all_group_messages": False}
        if method == "sendMessage":
            if self.bad_html and params.get("parse_mode"):
                return False, "Bad Request: can't parse entities: unclosed tag"
            self.n += 1
            return True, {"message_id": self.n}
        if method == "getFile":
            return True, {"file_path": "documents/a.txt"}
        return True, True

    def sent(self, method):
        return [p for m, p in self.calls if m == method]

    def close(self):
        self.server.shutdown()


def update(n, **kind):
    return {"update_id": n, **kind}


def message(text, user=7, chat=7, chat_type="private", **more):
    return {"message_id": 50, "from": {"id": user, "first_name": "Dillon"}, "chat": {"id": chat, "type": chat_type, "first_name": "Dillon", "title": "Family" if chat_type != "private" else None},
            "text": text, **more}


def wait(cond, timeout=3):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class TelegramLinkTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTelegram()
        telegramlink.API = self.fake.url

    def tearDown(self):
        self.fake.close()

    def test_markdown_becomes_telegram_html(self):
        html = telegramlink.to_html("**web**: see `a<b>` and [the PR](https://x.y/1)\n> is it ok?\n```\nx < y\n```")
        self.assertIn("<b>web</b>:", html)
        self.assertIn("<code>a&lt;b&gt;</code>", html)
        self.assertIn('<a href="https://x.y/1">the PR</a>', html)
        self.assertIn("<blockquote>is it ok?</blockquote>", html)
        self.assertIn("<pre>x &lt; y</pre>", html)
        self.assertEqual(telegramlink.to_html("snake_case and 2*3*4"), "snake_case and 2*3*4")

    def test_post_replies_with_the_first_part_puts_buttons_on_the_last_and_sends_files(self):
        out = telegramlink.post(TOKEN, "tg:-1001", "x" * 5000, "40", [("Yes", "pl:m1:0"), ("No", "pl:m1:1")], [("a.txt", b"hi")])
        first, last = self.fake.sent("sendMessage")
        self.assertEqual(first["chat_id"], "-1001")
        self.assertEqual(first["reply_parameters"]["message_id"], 40)
        self.assertNotIn("reply_markup", first)
        self.assertEqual([b["callback_data"] for b in last["reply_markup"]["inline_keyboard"][0]], ["pl:m1:0", "pl:m1:1"])
        self.assertEqual(out, {"id": "102"})
        self.assertIn(b'filename="a.txt"', self.fake.sent("sendDocument")[0]["multipart"])

    def test_html_telegram_refuses_goes_as_written(self):
        self.fake.bad_html = True
        out = telegramlink.post(TOKEN, "tg:5", "**half")
        self.assertEqual(out, {"id": "101"})
        self.assertNotIn("parse_mode", self.fake.sent("sendMessage")[-1])

    def test_a_press_shows_the_pick_or_clears_or_tells_the_presser(self):
        press = {"id": "q1", "token": TOKEN, "custom_id": "pt:t1:1", "channel_id": "tg:5",
                 "message": {"message_id": 60, "reply_markup": {"inline_keyboard": [[{"text": "Done", "callback_data": "pt:t1:0"}, {"text": "Couldn't do it", "callback_data": "pt:t1:1"}]]}}}
        telegramlink.answer_press(press)
        edit = self.fake.sent("editMessageReplyMarkup")[-1]
        self.assertEqual(edit["reply_markup"]["inline_keyboard"], [[{"text": "✓ Couldn't do it", "callback_data": "x"}]])
        telegramlink.answer_press(press, clear=True)
        self.assertNotIn("reply_markup", self.fake.sent("editMessageReplyMarkup")[-1])
        telegramlink.answer_press(press, "Task t1 is already done.")
        self.assertEqual(self.fake.sent("answerCallbackQuery")[-1], {"callback_query_id": "q1", "text": "Task t1 is already done."})

    def test_reactions_map_to_telegrams_set(self):
        telegramlink.react(TOKEN, "tg:5", "50", "🛠️")
        self.assertEqual(self.fake.sent("setMessageReaction")[-1]["reaction"], [{"type": "emoji", "emoji": "👨‍💻"}])

    def test_download(self):
        self.assertEqual(telegramlink.download(TOKEN, {"file_id": "f1", "size": 10}, 100), b"file bytes")
        self.assertIsNone(telegramlink.download(TOKEN, {"file_id": "f1", "size": 1000}, 100))

    def test_commands_parse_like_discords_options(self):
        self.assertEqual(telegramlink.parse_command("/peerlink run loop 5m check agent:web"), ("run", "loop 5m check agent:web"))
        self.assertEqual(telegramlink.command_options("run", "loop 5m check agent:web"), {"command": "loop 5m check", "agent": "web"})
        self.assertEqual(telegramlink.parse_command("/status@plbot"), ("status", ""))
        self.assertEqual(telegramlink.command_options("kill", "@web"), {"agent": "web"})
        self.assertIsNone(telegramlink.parse_command("/somethingelse"))


class TelegramBotTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTelegram()
        telegramlink.API = self.fake.url
        self.got, self.presses, self.strangers, self.commands = [], [], [], []
        self.bot = telegramlink.Bot(TOKEN, self.got.append, lambda *a: None, self.presses.append)
        self.bot.allowed_users = {"7"}
        self.bot.on_stranger = self.strangers.append
        self.bot.on_command = lambda cmd: (self.commands.append(cmd) or ("2 agents running", [("Resume now", "pr:0")]))
        self.assertTrue(wait(lambda: self.bot.state == "online"))

    def tearDown(self):
        self.bot.close()
        self.fake.close()

    def test_a_message_from_you_reaches_the_daemon_and_a_strangers_is_kept_out(self):
        self.fake.updates = [update(1, message=message("hello")), update(2, message=message("let me in", user=8))]
        self.assertTrue(wait(lambda: self.got and self.strangers))
        self.assertEqual(self.got[0], {"channel_id": "tg:7", "message_id": "50", "guild_id": "", "author": "Dillon", "author_id": "7",
                                       "text": "hello", "dm": False, "mentioned": True, "ref": "", "files": [], "bot": False})
        self.assertEqual(len(self.got), 1)
        self.assertEqual(self.strangers[0]["id"], "8")
        self.assertEqual(self.bot.chats, {"tg:7": "Dillon"})

    def test_in_a_group_only_a_mention_or_a_reply_to_the_bot_is_addressed(self):
        self.fake.updates = [update(1, message=message("chatter", chat=-5, chat_type="group")),
                             update(2, message=message("@plbot status?", chat=-5, chat_type="group"))]
        self.assertTrue(wait(lambda: len(self.got) == 2))
        self.assertEqual([(m["text"], m["mentioned"]) for m in self.got], [("chatter", False), ("status?", True)])
        self.assertEqual(self.bot.chats["tg:-5"], "Family")

    def test_a_command_is_answered_in_the_chat_with_its_buttons(self):
        self.fake.updates = [update(1, message=message("/status"))]
        self.assertTrue(wait(lambda: self.fake.sent("sendMessage")))
        self.assertEqual(self.commands[0]["sub"], "status")
        reply = self.fake.sent("sendMessage")[0]
        self.assertEqual(reply["text"], "2 agents running")
        self.assertEqual(reply["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "pr:0")
        self.assertEqual(self.got, [])

    def test_a_press_from_you_goes_to_the_daemon_and_others_are_told_no(self):
        q = {"id": "q1", "from": {"id": 7, "first_name": "Dillon"}, "data": "pl:m1:0", "message": {"message_id": 60, "chat": {"id": 7}}}
        self.fake.updates = [update(1, callback_query=q), update(2, callback_query={**q, "id": "q2", "from": {"id": 8}}),
                             update(3, callback_query={**q, "id": "q3", "data": "x"})]
        self.assertTrue(wait(lambda: self.presses and len(self.fake.sent("answerCallbackQuery")) == 2))
        self.assertEqual((self.presses[0]["custom_id"], self.presses[0]["channel_id"], self.presses[0]["author"]), ("pl:m1:0", "tg:7", "Dillon"))
        told = {a["callback_query_id"]: a["text"] for a in self.fake.sent("answerCallbackQuery")}
        self.assertIn("Only people allowed", told["q2"])
        self.assertEqual(told["q3"], "Already answered.")

    def test_a_group_that_becomes_a_supergroup_moves(self):
        moved = []
        self.bot.on_migrate = lambda old, new: moved.append((old, new))
        self.fake.updates = [update(1, message=message("", chat=-5, chat_type="group", migrate_to_chat_id=-1005))]
        self.assertTrue(wait(lambda: moved))
        self.assertEqual(moved, [("tg:-5", "tg:-1005")])


class DaemonTelegramTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTelegram()
        telegramlink.API = self.fake.url
        peerlinkd.load_discord()
        peerlinkd.discord["telegram"]["token"] = TOKEN

    def tearDown(self):
        peerlinkd.discord["telegram"].update({"token": "", "chats": {}})
        peerlinkd.discord["routes"] = []
        self.fake.close()

    def test_telegram_chats_post_through_telegram(self):
        self.assertEqual(peerlinkd.chat_post("tg:-1001", "**web**: done", None, peerlinkd.choices(["Ok"], "pl:m3")), "101")
        sent = self.fake.sent("sendMessage")[0]
        self.assertEqual((sent["chat_id"], sent["text"]), ("-1001", "<b>web</b>: done"))

    def test_send_targets(self):
        self.assertEqual(peerlinkd.chat_target("telegram"), ("Telegram", ""))
        self.assertEqual(peerlinkd.chat_target("telegram#family"), ("Telegram", "#family"))
        self.assertEqual(peerlinkd.chat_target("discord"), ("", ""))
        self.assertIsNone(peerlinkd.chat_target("discord-voice"))
        chans = [{"channel": "#ops", "platform": "Discord"}, {"channel": "#family", "platform": "Telegram"}]
        self.assertEqual(peerlinkd.chats_matching(chans, ("Telegram", "")), [chans[1]])
        self.assertEqual(peerlinkd.chats_matching(chans, ("", "#ops")), [chans[0]])

    def test_a_migrated_group_keeps_its_mapping(self):
        peerlinkd.discord["routes"] = [{"channel_id": "tg:-5", "agents": [{"cwd": "/x", "receives": True, "sends": True}]}]
        peerlinkd.discord["telegram"]["chats"] = {"tg:-5": "Family"}
        peerlinkd.telegram_migrate("tg:-5", "tg:-1005")
        self.assertEqual(peerlinkd.discord["routes"][0]["channel_id"], "tg:-1005")
        self.assertEqual(peerlinkd.discord["telegram"]["chats"], {"tg:-1005": "Family"})


if __name__ == "__main__":
    unittest.main()
