"""Tests for peerlink_mcp.py against a daemon in memory. Run: python3 -m unittest discover -s plugin/daemon"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "peerlink_mcp.py")


class FakeDaemon:
    def __init__(self, space="services"):
        self.space, self.posted, self.inbox, self.acked = space, [], [], []
        self.messages = [
            {"id": "m1", "from": "p1", "to": "mcp-voice", "text": "hi"},
            {"id": "m2", "from": "p1", "to": "p2", "text": "not yours"},
        ]
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, out, code=200):
                data = json.dumps(out).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else {}

            def do_GET(self):
                if self.path.startswith("/api/peers"):
                    return self.reply({"space": outer.space, "peers": [{"id": "p1", "name": "billing", "cwd": "/r/b", "busy": False, "token": "x"}]})
                if self.path == "/api/inbox":
                    return self.reply({"items": [], "user": "Dillon"})
                if self.path == "/api/config":
                    return self.reply({"max_message_chars": 50, "token": "secret"})
                if self.path.startswith("/api/messages"):
                    return self.reply({"messages": outer.messages})
                self.reply({"error": "nope"}, 404)

            def do_POST(self):
                b = self.body()
                outer.posted.append((self.path, b))
                if self.path == "/api/sessions":
                    return self.reply({"id": b["id"], "name": b["name"], "config": {"space": outer.space}})
                if self.path.endswith("/heartbeat"):
                    outer.acked += b.get("ack") or []
                    # Handed again until acked, like the real daemon.
                    return self.reply({"inbox": [m for m in outer.inbox if m["id"] not in outer.acked]})
                if self.path == "/api/messages":
                    return self.reply({"id": "m9", "status": "queued"})
                self.reply({"error": "nope"}, 404)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()


class Client:
    """Speaks MCP over the endpoint's stdio."""

    def __init__(self, port, *extra, name="voice"):
        self.home = tempfile.mkdtemp()
        with open(os.path.join(self.home, "key"), "w") as f:
            f.write("k")
        env = {**os.environ, "PEERLINK_HOME": self.home, "PEERLINK_PORT": str(port)}
        self.p = subprocess.Popen(
            [sys.executable, "-I", SCRIPT, "--name", name, "--cwd", self.home, *extra],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        self.n = 0

    def rpc(self, method, params=None):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def tool(self, name, **args):
        res = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        text = res["content"][0]["text"]
        return res.get("isError", False), (text if res.get("isError") else json.loads(text))

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=5)
        self.p.stdout.close()
        self.p.stderr.close()


def wait_for(cond, secs=8):
    import time
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.d = FakeDaemon()
        self.c = Client(self.d.port, "--space", "services")
        self.addCleanup(self.d.stop)
        self.addCleanup(self.c.close)

    def test_registers_under_the_launch_name_and_lists_exactly_three_tools(self):
        init = self.c.rpc("initialize", {"protocolVersion": "2025-03-26"})["result"]  # registration precedes serving
        reg = next(b for p, b in self.d.posted if p == "/api/sessions")
        self.assertEqual((reg["name"], reg["had_name"], reg["id"]), ("voice", "voice", "mcp-voice"))
        self.assertEqual(init["serverInfo"]["name"], "peerlink")
        tools = self.c.rpc("tools/list")["result"]["tools"]
        self.assertEqual(sorted(t["name"] for t in tools), ["inbox", "list_peers", "send_message"])
        self.assertNotIn("kind", next(t for t in tools if t["name"] == "send_message")["inputSchema"]["properties"])

    def test_list_peers_trims_fields(self):
        err, out = self.c.tool("list_peers")
        self.assertFalse(err)
        self.assertEqual(out["peers"], [{"id": "p1", "name": "billing", "cwd": "/r/b", "busy": False}])

    def test_send_message_is_plain_and_from_this_peer(self):
        err, out = self.c.tool("send_message", to="billing", text="hello", expects_reply=True, kind="announcement")
        self.assertTrue(err)  # kind is refused, not quietly downgraded
        err, out = self.c.tool("send_message", to="billing", text="hello", expects_reply=True)
        self.assertFalse(err)
        body = next(b for p, b in self.d.posted if p == "/api/messages")
        self.assertEqual((body["from"], body["to"], body["kind"], body["expects_reply"]), ("mcp-voice", "billing", "message", True))

    def test_blocks_spaces_leads_and_discord(self):
        for to in ("space", "space:services", "Space:x", "lead", "lead:services", "discord", "discord#general"):
            err, msg = self.c.tool("send_message", to=to, text="x")
            self.assertTrue(err, to)
        self.assertFalse([1 for p, _ in self.d.posted if p == "/api/messages"])

    def test_blocks_the_user(self):
        for to in ("user", "User", "you", "dillon", " Dillon "):
            err, msg = self.c.tool("send_message", to=to, text="x")
            self.assertTrue(err, to)
        self.assertFalse([1 for p, _ in self.d.posted if p == "/api/messages"])

    def test_caps_text_at_the_daemons_limit(self):
        err, msg = self.c.tool("send_message", to="billing", text="x" * 51)
        self.assertTrue(err)
        self.assertIn("50", msg)
        self.assertFalse(self.c.tool("send_message", to="billing", text="x" * 50)[0])

    def test_reply_to_only_threads_this_peer_is_in(self):
        self.assertFalse(self.c.tool("send_message", reply_to="m1", text="ok")[0])
        err, msg = self.c.tool("send_message", reply_to="m2", text="ok")
        self.assertTrue(err)
        err, msg = self.c.tool("send_message", reply_to="m404", text="ok")
        self.assertTrue(err)

    def test_inbox_peek_keeps_messages_and_reading_acks_them(self):
        self.d.inbox.append({"id": "m5", "from": "p1", "from_name": "billing", "text": "ping", "expects_reply": True})
        self.assertTrue(wait_for(lambda: self.c.tool("inbox", peek=True)[1]["messages"]))
        first = self.c.tool("inbox", peek=True)[1]["messages"]
        again = self.c.tool("inbox", peek=True)[1]["messages"]
        self.assertEqual([m["id"] for m in first], ["m5"])
        self.assertEqual(first, again)
        self.assertEqual(self.d.acked, [])
        read = self.c.tool("inbox")[1]["messages"]
        self.assertEqual([m["id"] for m in read], ["m5"])
        self.assertEqual(self.c.tool("inbox")[1]["messages"], [])
        self.assertTrue(wait_for(lambda: "m5" in self.d.acked))
        # A reply to something it was handed is its to make even when the daemon's log has aged it out.
        self.assertFalse(self.c.tool("send_message", reply_to="m5", text="pong")[0])


class StartupTest(unittest.TestCase):
    def test_refuses_the_wrong_space(self):
        d = FakeDaemon(space="default")
        self.addCleanup(d.stop)
        c = Client(d.port, "--space", "services")
        self.assertNotEqual(c.p.wait(timeout=8), 0)
        self.assertIn("services", c.p.stderr.read())
        c.p.stdin.close(); c.p.stdout.close(); c.p.stderr.close()

    def test_name_is_required(self):
        env = {k: v for k, v in os.environ.items() if k != "PEERLINK_MCP_NAME"}
        r = subprocess.run([sys.executable, "-I", SCRIPT], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
        self.assertNotEqual(r.returncode, 0)

    def test_http_serves_localhost_only_and_rejects_foreign_hosts_and_origins(self):
        d = FakeDaemon()
        self.addCleanup(d.stop)
        import socket
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        c = Client(d.port, "--http", str(port))
        self.addCleanup(lambda: (c.p.terminate(), c.p.wait(timeout=5), c.p.stdin.close(), c.p.stdout.close(), c.p.stderr.close()))
        url = f"http://127.0.0.1:{port}/mcp"
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()

        def post(headers=None):
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **(headers or {})})
            try:
                return urllib.request.urlopen(req, timeout=3).status
            except urllib.error.HTTPError as e:
                return e.code

        self.assertTrue(wait_for(lambda: self._up(port)))
        self.assertEqual(post(), 200)
        self.assertEqual(post({"Host": "evil.example"}), 403)
        self.assertEqual(post({"Origin": "http://evil.example"}), 403)

    @staticmethod
    def _up(port):
        import socket
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return True
        except OSError:
            return False


if __name__ == "__main__":
    unittest.main()
