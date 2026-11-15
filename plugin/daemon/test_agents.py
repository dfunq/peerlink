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
        peerlinkd.discord["agents"].update({"enabled": True, "port": self.port, "default_to": "/r/hq", "clients": {}, "chats": {}})
        peerlinkd.discord["routes"] = [{"channel_id": "123", "agents": [{"cwd": "/r/hq", "name": "", "receives": True, "sends": True}]}]
        now = time.time()
        peerlinkd.sessions.clear()
        peerlinkd.sessions["chief"] = {"id": "chief", "name": "hq", "cwd": "/r/hq", "last_seen": now}
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
        peerlinkd.discord["agents"].update({"clients": {}, "chats": {}, "default_to": ""})
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
        self.assertEqual([t["name"] for t in out["result"]["tools"]], ["post_message", "read_replies"])
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
        out, err = self.call(GROK, "post_message", {"text": "Task: announce something. web: do it"})
        self.assertIsNone(err)
        got = peerlinkd.inboxes["chief"]
        self.assertEqual(len(got), 1)
        msg = got[0]
        self.assertEqual(msg["from"], "discord:agents:grok")
        self.assertTrue(msg["text"].startswith("[from the external agent 'grok'"))
        self.assertNotIn("task", msg)  # a "Task:" prefix doesn't make it one
        self.assertNotIn("web", peerlinkd.inboxes)  # nor does "web:" reach another agent
        # hq answers with reply_to: it goes back on that client's chat, and only that client reads it.
        code, res = peerlinkd.send({"from": "chief", "text": "on it", "reply_to": msg["id"]})
        self.assertEqual(code, 200, res)
        replies, _ = self.call(GROK, "read_replies")
        self.assertEqual([r["text"] for r in replies["replies"]], ["on it"])
        self.assertEqual(replies["replies"][0]["reply_to"], out["id"])
        self.assertEqual(self.call(OTHER, "read_replies")[0]["replies"], [])
        later, _ = self.call(GROK, "read_replies", {"since": replies["next_since"]})
        self.assertEqual(later["replies"], [])
        # Answering that reply threads back to hq as the answer to it.
        _, err = self.call(GROK, "post_message", {"text": "thanks", "reply_to": replies["replies"][0]["id"]})
        self.assertIsNone(err)
        self.assertEqual(peerlinkd.inboxes["chief"][-1]["reply_to"], res["id"])
        _, err = self.call(OTHER, "post_message", {"text": "x", "reply_to": replies["replies"][0]["id"]})
        self.assertIn("isn't one of your replies", err)

    def test_an_agent_cant_start_a_conversation_there_and_your_things_never_go_there(self):
        self.assertEqual([r["channel_id"] for r in peerlinkd.routes_for("/r/hq")], ["123"])
        self.assertEqual(peerlinkd.approval_channel(peerlinkd.sessions["chief"]), "123")
        code, out = peerlinkd.send({"from": "chief", "to": "agents#grok", "text": "hello"})
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
        results = [self.call(GROK, "post_message", {"text": f"n{i}"})[1] for i in range(3)]
        self.assertEqual(results[:2], [None, None])
        self.assertIn("rate limited", results[2])
        self.assertIsNone(self.call(OTHER, "post_message", {"text": "fine"})[1])

    def test_adding_needs_a_hash_a_name_and_somewhere_to_land(self):
        self.assertEqual(peerlinkd.agents_api(["clients"], "POST", {"name": "Bad Name", "token_sha256": sha("x" * 40)})[0], 400)
        self.assertEqual(peerlinkd.agents_api(["clients"], "POST", {"name": "ok", "token_sha256": "not-a-hash"})[0], 400)
        self.assertEqual(peerlinkd.agents_api(["clients"], "POST", {"name": "dup", "token_sha256": sha(GROK)})[0], 409)
        peerlinkd.discord["agents"]["default_to"] = ""
        self.assertEqual(peerlinkd.agents_api(["clients"], "POST", {"name": "new", "token_sha256": sha("n" * 43)})[0], 400)


class AgentsLinkTest(unittest.TestCase):
    def test_clean_clients_drops_malformed_entries(self):
        out = agentslink.clean_clients({"ok": {"token_sha256": "a" * 64}, "Bad": {"token_sha256": "a" * 64},
                                        "nohash": {}, "rate": {"token_sha256": "b" * 64, "rate_per_min": 9999}})
        self.assertEqual(sorted(out), ["ok", "rate"])
        self.assertEqual(out["rate"]["rate_per_min"], 120)
        self.assertTrue(out["ok"]["enabled"])


if __name__ == "__main__":
    unittest.main()
