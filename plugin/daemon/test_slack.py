"""Tests for slacklink and the daemon's Slack wiring, against a fake Web API and a fake Socket Mode websocket.
Run: python3 -m unittest discover -s plugin/daemon"""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PEERLINK_HOME", tempfile.mkdtemp(prefix="peerlink-slack-test-"))

import slacklink  # noqa: E402
import peerlinkd  # noqa: E402

BOT, APP = "xoxb-1-abcd", "xapp-1-wxyz"


class FakeSocket:
    """Socket Mode's websocket: says hello, sends what the test queues, and keeps the receipts it's sent."""

    def __init__(self):
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        self.conn, self.acks, self.opened = None, [], 0
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            head = b""
            while b"\r\n\r\n" not in head:
                head += conn.recv(1024)
            key = next(l.split(b": ", 1)[1] for l in head.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key"))
            accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            self.conn, self.opened = conn, self.opened + 1
            self.push({"type": "hello"})
            threading.Thread(target=self.read, args=(conn,), daemon=True).start()

    def push(self, obj):
        data = json.dumps(obj).encode()
        head = bytes([0x81]) + (bytes([len(data)]) if len(data) < 126 else bytes([126]) + struct.pack(">H", len(data)))
        self.conn.sendall(head + data)

    def read(self, conn):
        buf = b""

        def take(n):
            nonlocal buf
            while len(buf) < n:
                chunk = conn.recv(65536)
                if not chunk:
                    raise OSError
                buf += chunk
            out, buf = buf[:n], buf[n:]
            return out
        try:
            while True:
                b1, b2 = take(2)
                n = b2 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", take(2))[0]
                mask = take(4)
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(take(n)))
                if b1 & 0x0F == 1:
                    self.acks.append(json.loads(data))
        except OSError:
            pass

    def close(self):
        self.srv.close()
        if self.conn:
            self.conn.close()


class FakeSlack:
    """The Web API in memory: records each call with its token, and answers the few methods peerlink uses."""

    def __init__(self):
        self.calls, self.n, self.refuse, self.uploaded, self.responses = [], 100, {}, [], []
        self.sock = FakeSocket()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def out(self, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.out(b"file bytes", "text/plain") if self.headers.get("Authorization") == f"Bearer {BOT}" else self.out(b"<html>", "text/html")

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                method = self.path.rsplit("/", 1)[-1]
                if method == "upload":
                    outer.uploaded.append(raw)
                    return self.out(b"OK", "text/plain")
                if method == "respond":
                    outer.responses.append(json.loads(raw))
                    return self.out(b"ok", "text/plain")
                params = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
                token = (self.headers.get("Authorization") or "").replace("Bearer ", "")
                self.out(outer.answer(method, params, token))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def answer(self, method, p, token):
        self.calls.append((method, p, token))
        if method in self.refuse:
            return {"ok": False, "error": self.refuse[method]}
        if token not in (BOT, APP):
            return {"ok": False, "error": "invalid_auth"}
        if method == "auth.test":
            return {"ok": True, "user": "peerlink", "user_id": "UBOT", "team": "Skyforge", "bot_id": "B1"}
        if method == "apps.connections.open":
            return {"ok": True, "url": f"ws://127.0.0.1:{self.sock.port}/link"} if token == APP else {"ok": False, "error": "not_allowed_token_type"}
        if method == "chat.postMessage":
            self.n += 1
            return {"ok": True, "ts": f"{self.n}.0"}
        if method == "users.conversations":
            return {"ok": True, "channels": [{"id": "C1", "name": "ops"}, {"id": "D1", "is_im": True, "user": "U7"}]}
        if method == "conversations.info":
            return {"ok": True, "channel": {"id": p["channel"], "name": "late"}}
        if method == "users.info":
            return {"ok": True, "user": {"real_name": {"U7": "Dillon", "U8": "Mallory"}.get(p["user"], p["user"])}}
        if method == "files.getUploadURLExternal":
            return {"ok": True, "upload_url": f"{self.url}/upload", "file_id": "F9"}
        if method == "conversations.history":
            return {"ok": True, "messages": [{"text": "old", "blocks": [slacklink.section("old"), slacklink.actions([("Go", "pl:m1:0")])]}]}
        return {"ok": True}

    def sent(self, method):
        return [p for m, p, _ in self.calls if m == method]

    def close(self):
        self.server.shutdown()
        self.sock.close()


def wait(cond, timeout=4):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def event(text, user="U7", channel="D1", kind="im", **more):
    return {"type": "events_api", "envelope_id": "e" + str(time.time()),
            "payload": {"event": {"type": "message", "user": user, "channel": channel, "channel_type": kind, "text": text, "ts": "50.1", **more}}}


class SlackLinkTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSlack()
        slacklink.API = self.fake.url
        self.addCleanup(self.fake.close)

    def test_markdown_becomes_mrkdwn(self):
        self.assertEqual(slacklink.to_mrkdwn("## Done\n**web**: *fast* ~~slow~~ [docs](https://x.io/a) & `a<b>` \n> quoted <x>"),
                         "*Done*\n*web*: _fast_ ~slow~ <https://x.io/a|docs> &amp; `a&lt;b&gt;` \n> quoted &lt;x&gt;")
        self.assertEqual(slacklink.to_mrkdwn("```py\nif a < b: **x**\n```"), "```if a &lt; b: **x**\n```")
        self.assertEqual(slacklink.from_slack("see <https://x.io|x.io> &amp; <#C1|ops> a &lt; b"), "see https://x.io & #ops a < b")

    def test_post_threads_a_reply_inside_a_thread_puts_buttons_on_the_last_part_and_sends_files(self):
        out = slacklink.post(BOT, "slack:C1", "a" * 2000 + "\n\n" + "b" * 2000, "50.1@40.0", [("Yes", "pl:m1:0"), ("No", "pl:m1:1")], [("a.txt", b"hi")])
        first, last = self.fake.sent("chat.postMessage")
        self.assertEqual((first["channel"], first["thread_ts"], "blocks" in first), ("C1", "40.0", False))
        blocks = json.loads(last["blocks"])
        self.assertEqual([b["action_id"] for b in blocks[1]["elements"]], ["pl:m1:0", "pl:m1:1"])
        self.assertEqual(out, {"id": "102.0"})
        self.assertEqual(self.fake.uploaded, [b"hi"])
        done = self.fake.sent("files.completeUploadExternal")[0]
        self.assertEqual((json.loads(done["files"])[0]["id"], done["channel_id"], done["thread_ts"]), ("F9", "C1", "40.0"))
        # A reply to a message in the channel itself stays in the channel.
        slacklink.post(BOT, "slack:C1", "ok", "50.1")
        self.assertNotIn("thread_ts", self.fake.sent("chat.postMessage")[-1])

    def test_buttons_come_off_a_message_it_posted_and_one_from_before_a_restart(self):
        ts = slacklink.post(BOT, "slack:C1", "Pick", None, [("Yes", "pl:m1:0")])["id"]
        slacklink.clear_buttons(BOT, "slack:C1", ts)
        up = self.fake.sent("chat.update")[0]
        self.assertEqual((up["ts"], [b["type"] for b in json.loads(up["blocks"])]), (ts, ["section"]))
        self.assertEqual(self.fake.sent("conversations.history"), [])
        slacklink.clear_buttons(BOT, "slack:C1", "9.9")
        self.assertEqual([b["type"] for b in json.loads(self.fake.sent("chat.update")[1]["blocks"])], ["section"])

    def test_a_press_shows_the_pick_or_clears_or_tells_the_presser(self):
        msg = {"ts": "60.0", "text": "Pick", "blocks": [slacklink.section("Pick"), slacklink.actions([("Yes", "pl:m1:0"), ("No", "pl:m1:1")])]}
        press = {"custom_id": "pl:m1:1", "channel_id": "slack:C1", "message": msg, "author": "Dillon", "response_url": f"{self.fake.url}/respond"}
        slacklink.answer_press(press)
        slacklink.answer_press(press, clear=True)
        slacklink.answer_press(press, "Not open anymore.")
        picked, cleared, told = self.fake.responses
        self.assertEqual((picked["replace_original"], picked["blocks"][-1]["elements"][0]["text"]), (True, "✓ No · Dillon"))
        self.assertEqual([b["type"] for b in cleared["blocks"]], ["section"])
        self.assertEqual((told["response_type"], told["replace_original"], told["text"]), ("ephemeral", False, "Not open anymore."))

    def test_reactions_use_slacks_names_and_the_first_part_of_the_id(self):
        slacklink.react(BOT, "slack:C1", "50.1@40.0", "🛠️")
        slacklink.unreact(BOT, "slack:C1", "50.1", "🛠️")
        self.assertEqual(self.fake.sent("reactions.add"), [{"channel": "C1", "timestamp": "50.1", "name": "hammer_and_wrench"}])
        self.assertEqual(self.fake.sent("reactions.remove")[0]["name"], "hammer_and_wrench")
        self.fake.refuse["reactions.add"] = "already_reacted"
        slacklink.react(BOT, "slack:C1", "50.1", "✅")  # a courtesy: no error

    def test_download_needs_the_token_and_respects_the_limit(self):
        f = {"url": f"{self.fake.url}/files/a.txt", "filename": "a.txt", "size": 10}
        self.assertEqual(slacklink.download(BOT, f, 100), b"file bytes")
        self.assertIsNone(slacklink.download(BOT, f, 5))
        with self.assertRaises(slacklink.SlackError):
            slacklink.download("xoxb-wrong", f, 100)

    def test_tokens_are_checked_by_shape_and_with_slack(self):
        self.assertEqual(slacklink.inspect_tokens(BOT, APP)["team"], "Skyforge")
        for bot, app, part in ((APP, APP, "xoxb-"), (BOT, BOT, "xapp-"), ("xoxb-nope", APP, "rejected"), (BOT, "xapp-nope", "app-level token")):
            with self.assertRaises(slacklink.SlackError) as c:
                slacklink.inspect_tokens(bot, app)
            self.assertIn(part, str(c.exception))
        self.fake.refuse["chat.postMessage"] = "not_in_channel"
        with self.assertRaises(slacklink.SlackError) as c:
            slacklink.post(BOT, "slack:C1", "hi")
        self.assertIn("/invite @peerlink", str(c.exception))

    def test_the_manifest_link_asks_for_socket_mode_the_command_and_message_events(self):
        m = json.loads(urllib.parse.parse_qs(urllib.parse.urlparse(slacklink.manifest_url()).query)["manifest_json"][0])
        self.assertTrue(m["settings"]["socket_mode_enabled"] and m["settings"]["interactivity"]["is_enabled"])
        self.assertEqual(m["features"]["slash_commands"][0]["command"], "/peerlink")
        self.assertIn("message.im", m["settings"]["event_subscriptions"]["bot_events"])
        self.assertFalse({"chat:write", "im:history", "files:read", "reactions:write", "commands"} - set(m["oauth_config"]["scopes"]["bot"]))


class SlackBotTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSlack()
        slacklink.API = self.fake.url
        self.got, self.presses, self.strangers, self.commands = [], [], [], []
        self.bot = slacklink.Bot(BOT, APP, self.got.append, lambda *a: None, self.presses.append)
        self.bot.allowed_users = {"U7"}
        self.bot.on_stranger = self.strangers.append
        self.bot.on_command = lambda cmd: (self.commands.append(cmd) or ("2 agents running", [("Resume now", "pr:0")]))
        self.addCleanup(self.fake.close)
        self.addCleanup(self.bot.close)
        self.assertTrue(wait(lambda: self.bot.state == "online"))

    def test_it_connects_with_the_app_token_and_lists_the_chats_it_is_in(self):
        self.assertEqual([t for m, _, t in self.fake.calls if m == "apps.connections.open"], [APP])
        self.assertEqual(self.bot.chats, {"slack:C1": "#ops", "slack:D1": "Dillon"})
        self.assertEqual(self.bot.channel_name("slack:C1"), "ops")
        self.assertEqual(self.bot.channels_live()[0]["id"], "slack")

    def test_a_message_from_you_reaches_the_daemon_is_receipted_and_a_strangers_is_kept_out(self):
        first = event("hello <https://x.io|x.io> &amp; all")
        self.fake.sock.push(first)
        self.fake.sock.push(event("let me in", user="U8"))
        self.assertTrue(wait(lambda: self.got and self.strangers))
        self.assertEqual(self.got, [{"channel_id": "slack:D1", "message_id": "50.1", "guild_id": "", "author": "Dillon", "author_id": "U7",
                                     "text": "hello https://x.io & all", "dm": False, "mentioned": True, "ref": "", "files": [], "bot": False}])
        self.assertEqual((self.strangers[0]["id"], self.strangers[0]["name"]), ("U8", "Mallory"))
        self.assertIn({"envelope_id": first["envelope_id"]}, self.fake.sock.acks)

    def test_in_a_channel_a_mention_is_marked_a_thread_is_kept_and_edits_and_its_own_posts_are_dropped(self):
        for e in (event("chatter", channel="C1", kind="channel"),
                  event("<@UBOT> status? ask <@U8>", channel="C1", kind="channel", ts="51.0", thread_ts="40.0",
                        files=[{"name": "a.png", "size": 3, "url_private_download": "https://files/a.png"}]),
                  event("edited", channel="C1", kind="channel", subtype="message_changed"),
                  event("mine", user="UBOT", channel="C1", kind="channel"),
                  event("another app", user="U9", channel="C1", kind="channel", bot_id="B9")):
            self.fake.sock.push(e)
        self.assertTrue(wait(lambda: len(self.got) == 2))
        time.sleep(0.2)
        self.assertEqual([(m["text"], m["mentioned"], m["message_id"], m["ref"]) for m in self.got],
                         [("chatter", False, "50.1", ""), ("status? ask @Mallory", True, "51.0@40.0", "40.0")])
        self.assertEqual(self.got[1]["files"], [{"url": "https://files/a.png", "filename": "a.png", "size": 3}])
        self.assertEqual(self.strangers, [])

    def test_a_command_is_answered_in_the_channel_with_its_buttons_or_only_to_whoever_typed_it(self):
        cmd = {"type": "slash_commands", "envelope_id": "c1", "payload": {"command": "/peerlink", "text": "run agent:web /compact", "user_id": "U7",
                                                                          "channel_id": "C1", "response_url": f"{self.fake.url}/respond"}}
        self.fake.sock.push(cmd)
        self.assertTrue(wait(lambda: self.fake.sent("chat.postMessage")))
        self.assertEqual((self.commands[0]["sub"], self.commands[0]["options"], self.commands[0]["channel_id"]), ("run", {"command": "/compact", "agent": "web"}, "slack:C1"))
        sent = self.fake.sent("chat.postMessage")[0]
        self.assertEqual((sent["text"], json.loads(sent["blocks"])[1]["elements"][0]["action_id"]), ("2 agents running", "pr:0"))
        # Where the app isn't a member, and for someone not allowed, the answer goes to the one who typed it.
        self.fake.refuse["chat.postMessage"] = "not_in_channel"
        self.fake.sock.push({**cmd, "envelope_id": "c2"})
        self.fake.sock.push({**cmd, "envelope_id": "c3", "payload": {**cmd["payload"], "user_id": "U8"}})
        self.assertTrue(wait(lambda: len(self.fake.responses) == 2))
        texts = sorted(r["text"] for r in self.fake.responses)
        self.assertEqual(texts[0], "2 agents running")
        self.assertIn("aren't allowed", texts[1])
        self.assertTrue(all(r["response_type"] == "ephemeral" for r in self.fake.responses))
        self.assertEqual(len(self.commands), 2)

    def test_a_press_from_you_goes_to_the_daemon_and_others_are_told_no(self):
        p = {"type": "block_actions", "user": {"id": "U7"}, "channel": {"id": "C1"}, "message": {"ts": "60.0", "text": "Pick", "blocks": []},
             "actions": [{"action_id": "pl:m1:0", "value": "pl:m1:0"}], "response_url": f"{self.fake.url}/respond", "trigger_id": "t1"}
        self.fake.sock.push({"type": "interactive", "envelope_id": "p1", "payload": p})
        self.fake.sock.push({"type": "interactive", "envelope_id": "p2", "payload": {**p, "user": {"id": "U8"}}})
        self.assertTrue(wait(lambda: self.presses and self.fake.responses))
        self.assertEqual(len(self.presses), 1)
        self.assertEqual((self.presses[0]["custom_id"], self.presses[0]["channel_id"], self.presses[0]["author"], self.presses[0]["message"]["ts"]),
                         ("pl:m1:0", "slack:C1", "Dillon", "60.0"))
        self.assertIn("Only people allowed", self.fake.responses[0]["text"])

    def test_it_opens_a_new_connection_when_slack_recycles_or_drops_this_one(self):
        self.fake.sock.push({"type": "disconnect", "reason": "refresh_requested"})
        self.assertTrue(wait(lambda: self.fake.sock.opened == 2))
        self.fake.sock.push(event("still here"))
        self.assertTrue(wait(lambda: self.got))
        self.fake.sock.conn.close()
        self.assertTrue(wait(lambda: self.fake.sock.opened == 3, 6))

    def test_a_rejected_token_stops_it_with_the_reason(self):
        self.fake.refuse["auth.test"] = "invalid_auth"
        bot = slacklink.Bot(BOT, APP, self.got.append, lambda *a: None)
        self.addCleanup(bot.close)
        self.assertTrue(wait(lambda: bot.state == "error"))
        self.assertIn("rejected", bot.error)


class DaemonSlackTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSlack()
        slacklink.API = self.fake.url
        peerlinkd.load_discord()
        peerlinkd.discord["slack"].update({"token": BOT, "app_token": APP})
        self.addCleanup(self.fake.close)
        self.addCleanup(lambda: peerlinkd.discord["slack"].update({"token": "", "app_token": "", "chats": {}, "strangers": []}))

    def test_slack_channels_post_through_slack(self):
        self.assertEqual(peerlinkd.chatlink.platform("slack:C1"), "slack")
        self.assertEqual(peerlinkd.chat_post("slack:C1", "**web**: done", None, peerlinkd.choices(["Ok"], "pl:m3")), "101.0")
        sent = self.fake.sent("chat.postMessage")[0]
        self.assertEqual((sent["channel"], sent["text"]), ("C1", "*web*: done"))
        self.assertEqual(peerlinkd.chat_via("slack:C1", "Dillon"), "Slack (Dillon)")

    def test_send_targets_and_settings(self):
        self.assertEqual(peerlinkd.chat_target("slack"), ("Slack", ""))
        self.assertEqual(peerlinkd.chat_target("slack#ops"), ("Slack", "#ops"))
        self.assertTrue(peerlinkd.chat_configured())
        self.assertEqual(peerlinkd.discord["slack"]["allowed_users"], [])

    def test_a_chat_it_hears_from_is_kept_and_a_stranger_is_offered_once(self):
        peerlinkd.chat_seen("slack")("slack:C1", "#ops")
        for _ in range(2):
            peerlinkd.chat_stranger("slack")({"id": "U8", "name": "Mallory", "username": "", "chat": "slack:C1", "chat_name": "#ops"})
        self.assertEqual(peerlinkd.discord["slack"]["chats"], {"slack:C1": "#ops"})
        self.assertEqual([s["id"] for s in peerlinkd.discord["slack"]["strangers"]], ["U8"])


if __name__ == "__main__":
    unittest.main()
