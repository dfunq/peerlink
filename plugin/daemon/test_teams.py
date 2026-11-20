"""Tests for teamslink and the daemon's Teams wiring, against a fake Microsoft sign-in and Graph.
Run: python3 -m unittest discover -s plugin/daemon"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PEERLINK_HOME", tempfile.mkdtemp(prefix="peerlink-teams-test-"))

import chatlink  # noqa: E402
import teamslink  # noqa: E402

ME, OTHER = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
CHAT = "19:abc@thread.v2"


def iso(ts):
    return teamslink.iso(ts)


class FakeMicrosoft:
    """Sign-in and Graph in memory: records each call, and serves a chat's messages changed after the asked time."""

    def __init__(self):
        self.calls, self.messages, self.n, self.pending, self.expired_once = [], {CHAT: []}, 1000, 1, False
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.calls.append(("GET", self.path, None, self.headers.get("Authorization")))
                self.send(*outer.get(self.path, self.headers.get("Authorization")))

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = (dict(urllib.parse.parse_qsl(raw.decode())) if "urlencoded" in (self.headers.get("Content-Type") or "")
                        else json.loads(raw or b"{}"))
                outer.calls.append(("POST", self.path, body, self.headers.get("Authorization")))
                self.send(*outer.post(self.path, body))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def add(self, text, user=ME, name="Alex", created=None, html_body=None, attachments=None, chat=CHAT):
        self.n += 1
        ts = created or time.time()
        m = {"id": str(self.n), "messageType": "message", "createdDateTime": iso(ts), "lastModifiedDateTime": iso(ts),
             "from": {"user": {"id": user, "displayName": name}}, "attachments": attachments or [],
             "body": {"contentType": "html", "content": html_body if html_body is not None else f"<p>{text}</p>"}}
        self.messages.setdefault(chat, []).append(m)
        return m["id"]

    def get(self, path, auth):
        if auth == "Bearer stale":
            return 401, {"error": {"code": "InvalidAuthenticationToken", "message": "expired"}}
        u = urllib.parse.urlparse(path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path.endswith("/me"):
            return 200, {"id": ME, "displayName": "Alex"}
        if u.path.endswith("/me/chats"):
            return 200, {"value": [{"id": CHAT, "topic": "", "members": [{"userId": ME, "displayName": "Alex"}, {"userId": OTHER, "displayName": "Pat"}]}]}
        if "/chats/" in u.path and u.path.endswith("/messages"):
            chat = urllib.parse.unquote(u.path.split("/chats/", 1)[1].rsplit("/messages", 1)[0])
            if chat not in self.messages:
                return 404, {"error": {"code": "NotFound", "message": "no chat"}}
            after = q.get("$filter", "").rsplit(" ", 1)[-1]
            return 200, {"value": [m for m in self.messages[chat] if m["lastModifiedDateTime"] > after][::-1]}
        return 404, {"error": {"code": "NotFound", "message": path}}

    def post(self, path, body):
        if path.endswith("/devicecode"):
            return 200, {"device_code": "dev", "user_code": "ABCD-1234", "verification_uri": "https://microsoft.com/devicelogin", "interval": 1, "expires_in": 900}
        if path.endswith("/token"):
            if body.get("grant_type", "").endswith("device_code"):
                if self.pending:
                    self.pending -= 1
                    return 400, {"error": "authorization_pending"}
                return 200, {"access_token": "fresh", "refresh_token": "r1", "expires_in": 3600}
            return 200, {"access_token": "fresh", "refresh_token": "r2", "expires_in": 3600}
        if path.endswith("/messages"):
            chat = urllib.parse.unquote(path.split("/chats/", 1)[1].rsplit("/messages", 1)[0])
            self.n += 1
            now = iso(time.time())
            self.messages.setdefault(chat, []).append({"id": str(self.n), "messageType": "message", "createdDateTime": now, "lastModifiedDateTime": now,
                                                       "from": {"user": {"id": ME, "displayName": "Alex"}}, "body": body["body"], "attachments": body.get("attachments", [])})
            return 201, {"id": str(self.n)}
        if path.endswith("/setReaction") or path.endswith("/unsetReaction"):
            return 204, {}
        return 404, {"error": {"code": "NotFound", "message": path}}


class TeamsTest(unittest.TestCase):
    def setUp(self):
        self.ms = FakeMicrosoft()
        self.addCleanup(self.ms.server.shutdown)
        for name, value in (("AUTH", self.ms.url), ("GRAPH", self.ms.url + "/v1.0"), ("POLL_S", 0.05)):
            old = getattr(teamslink, name)
            setattr(teamslink, name, value)
            self.addCleanup(setattr, teamslink, name, old)
        self.got, self.strangers, self.presses, self.commands, self.logs = [], [], [], [], []

    def bot(self, refresh="r0"):
        auth = teamslink.Auth("", "", refresh)
        bot = teamslink.Bot(auth, self.got.append, lambda *a: self.logs.append(a), self.presses.append, lambda: [f"teams:{CHAT}"])
        bot.on_stranger = self.strangers.append

        def command(cmd):
            self.commands.append(cmd)
            return "All good.", None
        bot.on_command = command
        self.addCleanup(bot.close)
        self.wait(lambda: bot.state == "online" and CHAT in bot.began)
        return bot

    def wait(self, cond, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.02)
        self.fail(f"timed out; logs: {self.logs}")

    def posts(self):
        return [c for c in self.ms.calls if c[0] == "POST" and c[1].endswith("/messages")]

    def test_markdown_becomes_teams_html_and_html_becomes_text(self):
        self.assertEqual(teamslink.to_html("**hi**\nthere\n```\na\nb\n```"), "<b>hi</b><br>there<br><pre>a\nb</pre>")
        self.assertEqual(teamslink.to_text('<p>Hi &amp; <b>bye</b></p><p>two<br>three</p><attachment id="9"></attachment>'), "Hi & bye\ntwo\nthree")

    def test_a_channel_id_says_teams(self):
        self.assertEqual(chatlink.platform(f"teams:{CHAT}"), "teams")
        self.assertEqual(chatlink.name(f"teams:{CHAT}"), "Teams")
        self.assertEqual(teamslink.chat_of(f"teams:{CHAT}"), CHAT)

    def test_signing_in_waits_for_the_code_then_has_the_tokens(self):
        code = teamslink.start_sign_in("", "")
        self.assertEqual(code["user_code"], "ABCD-1234")
        sent = [c for c in self.ms.calls if c[1].endswith("/devicecode")][0]
        self.assertEqual(sent[1], "/organizations/oauth2/v2.0/devicecode")
        self.assertEqual(sent[2]["client_id"], teamslink.CLIENT_ID)
        self.assertIn("Chat.ReadWrite", sent[2]["scope"])
        self.assertIsNone(teamslink.finish_sign_in("", "", "dev"))
        self.assertEqual(teamslink.finish_sign_in("", "", "dev")["refresh_token"], "r1")
        with self.assertRaises(teamslink.TeamsError):
            teamslink.start_sign_in("not-an-id", "")

    def test_a_new_refresh_token_is_kept_and_a_stale_access_token_is_renewed(self):
        kept = []
        auth = teamslink.Auth("", "", "r0", kept.append)
        auth.access, auth.until = "stale", time.time() + 999
        self.assertEqual(teamslink.graph(auth, "GET", "/me")["id"], ME)
        self.assertEqual((auth.access, kept), ("fresh", ["r2"]))

    def test_your_message_reaches_the_daemon_and_posts_of_its_own_and_old_ones_dont(self):
        self.ms.add("from before", created=time.time() - 3600)
        bot = self.bot()
        teamslink.post("", f"teams:{CHAT}", "**api**: done")
        self.ms.add("written by hand elsewhere", html_body=teamslink.MARK + "<p>an old post</p>")
        mid = self.ms.add("Which file defines the invoice model?")
        self.wait(lambda: self.got)
        time.sleep(0.2)
        self.assertEqual([g["text"] for g in self.got], ["Which file defines the invoice model?"])
        g = self.got[0]
        self.assertEqual((g["channel_id"], g["message_id"], g["author_id"], g["mentioned"]), (f"teams:{CHAT}", mid, ME, True))
        self.assertTrue(self.posts()[0][2]["body"]["content"].startswith(teamslink.MARK))
        self.assertEqual(bot.chats, {f"teams:{CHAT}": "Pat"})

    def test_someone_else_is_kept_out_until_allowed(self):
        bot = self.bot()
        self.ms.add("hello agents", user=OTHER, name="Pat")
        self.wait(lambda: self.strangers)
        self.assertEqual(self.got, [])
        self.assertEqual((self.strangers[0]["id"], self.strangers[0]["name"]), (OTHER, "Pat"))
        bot.allowed_users = {OTHER}
        self.ms.add("hello again", user=OTHER, name="Pat")
        self.wait(lambda: self.got)
        self.assertEqual(self.got[0]["author"], "Pat")

    def test_a_question_lists_its_options_and_a_number_answers_it(self):
        self.bot()
        asked = teamslink.post("", f"teams:{CHAT}", "Deploy now?", buttons=[("Yes", "pl:m5:0"), ("No", "pl:m5:1")])["id"]
        self.assertIn("1. Yes<br>2. No", self.posts()[-1][2]["body"]["content"])
        self.ms.add("2")
        self.wait(lambda: self.presses)
        p = self.presses[0]
        self.assertEqual((p["custom_id"], p["message"]["id"], p["channel_id"]), ("pl:m5:1", asked, f"teams:{CHAT}"))
        self.assertEqual(self.got, [])
        teamslink.answer_press(p)
        self.ms.add("2")  # with the question closed, a number is just a message
        self.wait(lambda: self.got)
        self.assertEqual(self.got[0]["text"], "2")

    def test_a_command_in_words_is_answered_in_the_chat(self):
        self.bot()
        mid = self.ms.add("peerlink status")
        self.wait(lambda: self.commands and len(self.posts()) == 1)
        self.assertEqual(self.commands[0]["sub"], "status")
        body = self.posts()[0][2]
        self.assertIn("All good.", body["body"]["content"])
        self.assertEqual(body["attachments"][0]["id"], mid)  # it quotes the command

    def test_a_reply_names_the_message_it_quotes_and_shared_files_are_named(self):
        self.bot()
        self.ms.add("see this", html_body='<attachment id="77"></attachment><p>see this</p>',
                    attachments=[{"id": "77", "contentType": "messageReference"}, {"contentType": "reference", "name": "plan.docx"}])
        self.wait(lambda: self.got)
        self.assertEqual(self.got[0]["ref"], "77")
        self.assertIn("plan.docx", self.got[0]["text"])


class DaemonTeamsTest(unittest.TestCase):
    def test_teams_is_set_up_from_the_browser_only(self):
        from rig import Rig
        with Rig(env={"PEERLINK_TEST": "0", "PEERLINK_TEST_ANSWER": "no"}) as rig:
            code, out = rig.call("GET", "/api/teams")
            self.assertEqual((code, out["configured"], out["state"]), (200, False, "unconfigured"))
            for method, path in (("POST", "/api/teams/sign-in"), ("PUT", "/api/teams"), ("DELETE", "/api/teams")):
                code, out = rig.call(method, path, {})
                self.assertEqual(code, 403, path)
                self.assertIn("web UI", out["error"])


if __name__ == "__main__":
    unittest.main()
