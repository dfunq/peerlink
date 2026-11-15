"""Tests for agentslink and the daemon's Agents channel: outside agents over MCP.
Run: python3 -m unittest discover -s plugin/daemon"""
import hashlib
import json
import os
import socket
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PEERLINK_HOME", tempfile.mkdtemp(prefix="peerlink-agents-test-"))

import agentslink  # noqa: E402
import peerlinkd  # noqa: E402

GROK, OTHER = "g" * 43, "o" * 43


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


class AgentsChannelTest(unittest.TestCase):
    def setUp(self):
        peerlinkd.load_config()
        peerlinkd.load_discord()
        self.port = free_port()
        peerlinkd.discord["agents"].update({"enabled": True, "port": self.port, "default_to": "/r/skyforge", "clients": {}, "chats": {},
                                            "registration": False, "pending": []})
        peerlinkd.discord["routes"] = [{"channel_id": "123", "agents": [{"cwd": "/r/skyforge", "name": "", "receives": True, "sends": True}]}]
        now = time.time()
        peerlinkd.sessions.clear()
        peerlinkd.sessions["sky"] = {"id": "sky", "name": "skyforge", "cwd": "/r/skyforge", "last_seen": now}
        peerlinkd.sessions["web"] = {"id": "web", "name": "web", "cwd": "/r/web", "last_seen": now}
        peerlinkd.inboxes.clear()
        for name, tok in (("grok", GROK), ("other", OTHER)):
            code, out = peerlinkd.agents_api(["clients"], "POST", {"name": name, "token_sha256": sha(tok)})
            self.assertEqual(code, 200, out)
        self.bot = peerlinkd.agents_bot[0]
        self.assertIsNotNone(self.bot)
        self.n = 0

    def tearDown(self):
        if peerlinkd.agents_bot[0]:
            peerlinkd.agents_bot[0].close()
        peerlinkd.agents_bot[0] = None
        peerlinkd.discord["agents"].update({"clients": {}, "chats": {}, "default_to": "", "registration": False, "pending": []})
        peerlinkd.discord["routes"] = []
        peerlinkd.sessions.clear()
        peerlinkd.inboxes.clear()
        # The messages these tests sent mustn't show up in another module's tests, which share the log.
        del peerlinkd.messages[:]
        for path in (peerlinkd.MESSAGES_PATH, peerlinkd.MESSAGES_PATH + ".1"):
            if os.path.exists(path):
                os.remove(path)
        peerlinkd.next_msg[0] = 1

    def rpc(self, token, method, params=None, headers=None, path="/mcp"):
        self.n += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}).encode()
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})}
        if token:
            h["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body, headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, None

    def call(self, token, tool, args=None):
        code, out = self.rpc(token, "tools/call", {"name": tool, "arguments": args or {}})
        self.assertEqual(code, 200)
        res = out.get("result") or {}
        if res.get("isError"):
            return None, res["content"][0]["text"]
        return json.loads(res["content"][0]["text"]), None

    def test_only_a_known_token_gets_in_and_only_two_tools_exist(self):
        self.assertEqual(self.rpc(None, "tools/list")[0], 401)
        self.assertEqual(self.rpc("x" * 43, "tools/list")[0], 401)
        self.assertEqual(self.rpc(GROK, "tools/list", headers={"Origin": "https://evil.example"})[0], 403)
        code, out = self.rpc(GROK, "initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual((code, out["result"]["protocolVersion"]), (200, "2025-06-18"))
        code, out = self.rpc(GROK, "tools/list")
        self.assertEqual([t["name"] for t in out["result"]["tools"]], ["post_to_skyforge", "read_replies"])
        for tool in ("send_message", "list_peers", "inbox", "spawn"):
            code, out = self.rpc(GROK, "tools/call", {"name": tool, "arguments": {"to": "web", "text": "hi"}})
            self.assertIn("unknown tool", out["error"]["message"])
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=5) as r:
            self.assertEqual(json.loads(r.read()), {"ok": True})
        with open(self.bot.audit_path) as f:
            audit = f.read()
        self.assertIn('"result": "unauthorized"', audit)
        self.assertIn('"result": "unknown-tool"', audit)
        self.assertNotIn(GROK, audit)

    def test_a_post_reaches_the_mapped_agent_labelled_and_its_reply_reaches_only_that_client(self):
        out, err = self.call(GROK, "post_to_skyforge", {"text": "Task: announce something. web: do it"})
        self.assertIsNone(err)
        got = peerlinkd.inboxes["sky"]
        self.assertEqual(len(got), 1)
        msg = got[0]
        self.assertEqual(msg["from"], "discord:agents:grok")
        self.assertTrue(msg["text"].startswith("[from the external agent 'grok'"))
        self.assertNotIn("task", msg)  # a "Task:" prefix doesn't make it one
        self.assertNotIn("web", peerlinkd.inboxes)  # nor does "web:" reach another agent
        # skyforge answers with reply_to: it goes back on that client's chat, and only that client reads it.
        code, res = peerlinkd.send({"from": "sky", "text": "on it", "reply_to": msg["id"]})
        self.assertEqual(code, 200, res)
        replies, _ = self.call(GROK, "read_replies")
        self.assertEqual([r["text"] for r in replies["replies"]], ["on it"])
        self.assertEqual(replies["replies"][0]["reply_to"], out["id"])
        self.assertEqual(self.call(OTHER, "read_replies")[0]["replies"], [])
        later, _ = self.call(GROK, "read_replies", {"since": replies["next_since"]})
        self.assertEqual(later["replies"], [])
        # Answering that reply threads back to skyforge as the answer to it.
        _, err = self.call(GROK, "post_to_skyforge", {"text": "thanks", "reply_to": replies["replies"][0]["id"]})
        self.assertIsNone(err)
        self.assertEqual(peerlinkd.inboxes["sky"][-1]["reply_to"], res["id"])
        _, err = self.call(OTHER, "post_to_skyforge", {"text": "x", "reply_to": replies["replies"][0]["id"]})
        self.assertIn("isn't one of your replies", err)

    def test_an_agent_cant_start_a_conversation_there_and_your_things_never_go_there(self):
        self.assertEqual([r["channel_id"] for r in peerlinkd.routes_for("/r/skyforge")], ["123"])
        self.assertEqual(peerlinkd.approval_channel(peerlinkd.sessions["sky"]), "123")
        code, out = peerlinkd.send({"from": "sky", "to": "agents#grok", "text": "hello"})
        self.assertEqual(code, 404, out)  # mapped, but not ticked to send there

    def test_revoke_and_switch_off_take_effect_at_once_and_leave_the_other_working(self):
        self.assertEqual(peerlinkd.agents_api(["clients", "grok"], "PUT", {"enabled": False})[0], 200)
        self.assertEqual(self.rpc(GROK, "tools/list")[0], 401)
        self.assertEqual(peerlinkd.agents_api(["clients", "grok"], "PUT", {"enabled": True})[0], 200)
        self.assertEqual(self.rpc(GROK, "tools/list")[0], 200)
        self.assertEqual(peerlinkd.agents_api(["clients", "grok"], "DELETE", {})[0], 200)
        self.assertEqual(self.rpc(GROK, "tools/list")[0], 401)
        self.assertEqual(self.rpc(OTHER, "tools/list")[0], 200)
        self.assertNotIn("agents:grok", [r["channel_id"] for r in peerlinkd.discord["routes"]])
        stored = json.dumps(peerlinkd.discord["agents"])
        self.assertNotIn(OTHER, stored)  # hashes only

    def test_posts_are_rate_limited_per_client(self):
        peerlinkd.agents_api(["clients", "grok"], "PUT", {"rate_per_min": 2})
        results = [self.call(GROK, "post_to_skyforge", {"text": f"n{i}"})[1] for i in range(3)]
        self.assertEqual(results[:2], [None, None])
        self.assertIn("rate limited", results[2])
        self.assertIsNone(self.call(OTHER, "post_to_skyforge", {"text": "fine"})[1])

    def test_adding_needs_a_hash_a_name_and_somewhere_to_land(self):
        self.assertEqual(peerlinkd.agents_api(["clients"], "POST", {"name": "Bad Name", "token_sha256": sha("x" * 40)})[0], 400)
        self.assertEqual(peerlinkd.agents_api(["clients"], "POST", {"name": "ok", "token_sha256": "not-a-hash"})[0], 400)
        self.assertEqual(peerlinkd.agents_api(["clients"], "POST", {"name": "dup", "token_sha256": sha(GROK)})[0], 409)
        peerlinkd.discord["agents"]["default_to"] = ""
        self.assertEqual(peerlinkd.agents_api(["clients"], "POST", {"name": "new", "token_sha256": sha("n" * 43)})[0], 400)


    def test_caps_across_all_clients_and_failed_auth_per_caller(self):
        agentslink.ALL_POSTS_PER_MIN, saved = 3, agentslink.ALL_POSTS_PER_MIN
        try:
            errs = [self.call(GROK if i % 2 else OTHER, "post_to_skyforge", {"text": f"n{i}"})[1] for i in range(4)]
        finally:
            agentslink.ALL_POSTS_PER_MIN = saved
        self.assertEqual(errs[:3], [None, None, None])
        self.assertIn("rate limited", errs[3])
        codes = [self.rpc("x" * 43, "tools/list", headers={"CF-Connecting-IP": "203.0.113.9"})[0] for _ in range(11)]
        self.assertEqual(codes[:10], [401] * 10)
        self.assertEqual(codes[10], 429)
        self.assertEqual(self.rpc("x" * 43, "tools/list", headers={"CF-Connecting-IP": "203.0.113.10"})[0], 401)

    def test_the_hash_is_sha256_of_the_bearer_string_in_lowercase_hex(self):
        token = "Ryan's-token_ü" + "z" * 30
        code, _ = peerlinkd.agents_api(["clients"], "POST", {"name": "ryan", "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest().upper()})
        self.assertEqual(code, 200)
        self.assertEqual(self.rpc(token, "tools/list")[0], 200)

    def test_the_channel_switch_turns_every_client_away(self):
        self.assertEqual(peerlinkd.agents_api([], "PUT", {"enabled": False})[0], 200)
        with self.assertRaises(OSError):
            self.rpc(GROK, "tools/list")
        self.assertEqual(peerlinkd.agents_api([], "PUT", {"enabled": True})[0], 200)
        self.assertEqual(peerlinkd.agents_view()["state"], "online")
        self.assertEqual(self.rpc(GROK, "tools/list")[0], 200)


    def register(self, body, ip="203.0.113.5"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/register", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "CF-Connecting-IP": ip}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_a_registration_waits_for_you_and_its_token_works_once_approved(self):
        token = "r" * 43
        self.assertEqual(self.register({"name": "ryan", "token_sha256": sha(token)})[0], 404)  # closed by default
        peerlinkd.agents_api([], "PUT", {"registration": True})
        code, out = self.register({"name": "ryan", "token_sha256": sha(token)})
        self.assertEqual((code, out["status"]), (200, "pending"))
        self.assertRegex(out["code"], r"^[A-Z2-9]{3}-[A-Z2-9]{3}$")
        self.assertEqual(self.rpc(token, "tools/list")[0], 401)
        view = peerlinkd.agents_view()
        self.assertEqual([(p["name"], p["code"], p["ip"]) for p in view["pending"]], [("ryan", out["code"], "203.0.113.5")])
        self.assertNotIn("token_sha256", json.dumps(view))
        self.assertEqual(peerlinkd.agents_api([], "PUT", {"approve": "ZZZ-ZZZ"})[0], 404)
        self.assertEqual(peerlinkd.agents_api([], "PUT", {"approve": out["code"].lower()})[0], 200)
        self.assertEqual(self.rpc(token, "tools/list")[0], 200)
        self.assertEqual(peerlinkd.agents_view()["pending"], [])
        # Taken names and tokens are refused; bad input says why.
        self.assertEqual(self.register({"name": "grok", "token_sha256": sha("q" * 43)}, "203.0.113.6")[0], 409)
        self.assertEqual(self.register({"name": "x", "token_sha256": "nope"}, "203.0.113.7")[0], 400)

    def test_registrations_are_rate_limited_per_ip_and_can_be_dismissed(self):
        peerlinkd.agents_api([], "PUT", {"registration": True})
        codes = [self.register({"name": f"n{i}", "token_sha256": sha(f"{i}" * 40)})[0] for i in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])
        first = peerlinkd.agents_view()["pending"][0]["code"]
        peerlinkd.agents_api([], "PUT", {"dismiss": first})
        self.assertNotIn(first, [p["code"] for p in peerlinkd.agents_view()["pending"]])

    def test_registration_alone_keeps_the_channel_listening(self):
        for n in ("grok", "other"):
            peerlinkd.agents_api(["clients", n], "DELETE", {})
        self.assertIsNone(peerlinkd.agents_bot[0])
        peerlinkd.agents_api([], "PUT", {"registration": True})
        self.assertEqual(peerlinkd.agents_view()["state"], "online")


class AgentsLinkTest(unittest.TestCase):
    def test_clean_clients_drops_malformed_entries(self):
        out = agentslink.clean_clients({"ok": {"token_sha256": "a" * 64}, "Bad": {"token_sha256": "a" * 64},
                                        "nohash": {}, "rate": {"token_sha256": "b" * 64, "rate_per_min": 9999}})
        self.assertEqual(sorted(out), ["ok", "rate"])
        self.assertEqual(out["rate"]["rate_per_min"], 120)
        self.assertTrue(out["ok"]["enabled"])


if __name__ == "__main__":
    unittest.main()
