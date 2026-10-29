#!/usr/bin/env python3
"""A standalone MCP endpoint for peerlink, for clients that aren't Claude Code (a Discord voice bot on the Agent SDK).

It registers one peer with the local peerlinkd under a name set at launch, keeps it alive, and offers three tools:
send_message, list_peers and inbox. The name comes from the command line or the environment, never from the caller,
and no tool takes an identity. It speaks MCP over stdio, or over HTTP on 127.0.0.1 with --http PORT.

    peerlink_mcp.py --name voice [--space services] [--cwd DIR] [--http PORT]
"""
import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

VERSION = "0.8.56"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
HEARTBEAT_S = 2.0
SEEN_MAX = 2000
HOME = os.environ.get("PEERLINK_HOME", os.path.expanduser("~/.peerlink"))
TOOLS = [
    {
        "name": "list_peers",
        "description": "List the live peer agents you can message, with their name, folder, current work and whether they're busy.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_message",
        "description": "Message a peer agent by name. It arrives at once, starting a turn there if the agent is idle. Keep it short. "
        "Set reply_to when answering a message from inbox.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Peer agent name or id, as list_peers shows it. Leave it out when replying: the reply goes to the sender"},
                "text": {"type": "string", "description": "The message: what you need and why, briefly"},
                "expects_reply": {"type": "boolean", "description": "True if you need an answer"},
                "reply_to": {"type": "string", "description": "Id of a message you received or sent, which you are answering"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "inbox",
        "description": "Messages and replies addressed to you that you haven't read yet. Call it to poll. Reading marks them read; "
        "with peek true they stay unread, so a turn that fails before it handled them doesn't lose them.",
        "inputSchema": {
            "type": "object",
            "properties": {"peek": {"type": "boolean", "description": "Leave the messages unread"}},
        },
    },
]


class Refused(Exception):
    """A call the endpoint won't make, said to the model as the tool's error."""


def log(*a):
    print("peerlink-mcp", *a, file=sys.stderr, flush=True)


class Daemon:
    """The local peerlinkd, as one registered peer."""

    def __init__(self, name, cwd, space, port, home):
        self.name, self.cwd, self.want_space = name, cwd, space
        self.base = f"http://127.0.0.1:{port}"
        self.key_path = os.path.join(home, "key")
        self.id = f"mcp-{name}"
        self.lock = threading.Lock()
        self.pending = {}  # id -> message handed by the daemon, not yet read
        self.to_ack = []   # read, and the daemon not yet told
        self.seen = {}     # id -> True for every message addressed to us, so a reply_to on one is ours to make
        self.read = {}     # id -> True for those read, which the daemon may still hand again until it hears the ack
        self.space = ""
        self.limit = 0
        self.registered = False

    def call(self, method, path, body=None, timeout=5):
        try:
            with open(self.key_path) as f:
                key = f.read().strip()
        except OSError:
            key = ""
        req = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Content-Type": "application/json", "X-Peerlink-Key": key},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as err:
            try:
                return err.code, json.loads(err.read() or b"{}")
            except ValueError:
                return err.code, {"error": err.reason}
        except (urllib.error.URLError, OSError) as err:
            raise Refused(f"peerlink daemon isn't reachable ({err}); is peerlinkd running?")

    def register(self):
        code, out = self.call("POST", "/api/sessions", {
            "id": self.id, "name": self.name, "cwd": self.cwd, "pid": os.getpid(), "plugin": f"peerlink-mcp {VERSION}",
            "summary": "A client outside Claude Code, over MCP", "busy": False,
        })
        if code != 200:
            raise Refused(f"registering as {self.name} failed: {out.get('error', code)}")
        space = (out.get("config") or {}).get("space", "")
        if self.want_space and space != self.want_space:
            raise Refused(f"{self.cwd} is in space '{space}', not '{self.want_space}': put that folder in the space in the web UI, or pass --cwd for one that is")
        if out.get("name") != self.name:
            log(f"the daemon named this peer {out.get('name')}, not {self.name}: another peer holds the name, or the folder has a nickname")
        self.registered = True
        self.space = space
        self.refresh_limit()
        log(f"registered as {out.get('name')} in space {space}")

    def refresh_limit(self):
        code, out = self.call("GET", "/api/config")
        if code == 200:
            self.limit = int(out.get("max_message_chars") or 0)

    def heartbeat(self):
        with self.lock:
            acks = list(self.to_ack)
        # "ack" present tells the daemon to hand a message again until it is acked, so one never read is not lost.
        code, out = self.call("POST", f"/api/sessions/{self.id}/heartbeat", {"busy": False, "ack": acks})
        if code == 404:
            self.register()
            return
        if code != 200:
            return
        with self.lock:
            self.to_ack = [m for m in self.to_ack if m not in acks]
            for m in out.get("inbox") or []:
                mid = m.get("id")
                if not mid or mid in self.read:
                    continue
                self.pending.setdefault(mid, m)
                self.seen[mid] = True
            while len(self.seen) > SEEN_MAX:
                self.seen.pop(next(iter(self.seen)))

    def run_heartbeats(self):
        while True:
            time.sleep(HEARTBEAT_S)
            try:
                self.heartbeat()
            except Refused as err:
                log(err)
            except Exception as err:  # keep the peer alive through anything odd
                log("heartbeat failed:", err)

    # Tools

    def list_peers(self, args):
        code, out = self.call("GET", f"/api/peers?exclude={self.id}")
        if code != 200:
            raise Refused(out.get("error", f"list_peers failed ({code})"))
        keep = ("id", "name", "host", "cwd", "branch", "summary", "busy", "muted")
        return {"space": out.get("space"), "peers": [{k: p.get(k) for k in keep if k in p} for p in out.get("peers", [])]}

    def send_message(self, args):
        text = str(args.get("text") or "").strip()
        to = str(args.get("to") or "").strip()
        reply_to = str(args.get("reply_to") or "").strip()
        if not text:
            raise Refused("text is required")
        if not to and not reply_to:
            raise Refused("to is required unless reply_to names a message sent to you")
        # Only a plain message to one peer: no announcements or tasks, nothing to a space, its lead, or a Discord channel.
        if args.get("kind") not in (None, "", "message"):
            raise Refused("this endpoint sends plain messages only")
        word = to.lower()
        if word.startswith(("space", "lead")):
            raise Refused("this endpoint can't message a space or its lead: name the peer, as list_peers shows it")
        if word == "discord" or word.startswith("discord#"):
            raise Refused("this endpoint can't post to Discord: name a peer")
        if self.limit and len(text) > self.limit:
            raise Refused(f"message is {len(text)} characters, over the {self.limit} limit: shorten it")
        if reply_to:
            self.check_thread(reply_to)
        body = {"from": self.id, "text": text, "kind": "message", "expects_reply": bool(args.get("expects_reply"))}
        if to:
            body["to"] = to
        if reply_to:
            body["reply_to"] = reply_to
        code, out = self.call("POST", "/api/messages", body)
        if code != 200:
            raise Refused(out.get("error", f"send failed ({code})"))
        return out

    def check_thread(self, mid):
        with self.lock:
            if mid in self.seen:
                return
        code, out = self.call("GET", "/api/messages?limit=1000")
        parent = next((m for m in (out.get("messages") or []) if m.get("id") == mid), None) if code == 200 else None
        if not parent or self.id not in (parent.get("from"), parent.get("to")):
            raise Refused(f"{mid} isn't a message you sent or received")

    def inbox(self, args):
        peek = bool(args.get("peek"))
        with self.lock:
            items = list(self.pending.values())
            if not peek:
                for m in items:
                    self.pending.pop(m["id"], None)
                    self.to_ack.append(m["id"])
                    self.read[m["id"]] = True
                while len(self.read) > SEEN_MAX:
                    self.read.pop(next(iter(self.read)))
        keep = ("id", "from_name", "from", "text", "expects_reply", "reply_to", "kind", "task", "ts")
        return {"messages": [{k: m[k] for k in keep if k in m} for m in items]}


def handle(daemon, msg):
    """One JSON-RPC message in, its response out (None for a notification)."""
    mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, text):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}

    if mid is None:
        return None  # notifications/initialized, cancelled, ...
    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({
            "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "peerlink", "version": VERSION},
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in {t["name"] for t in TOOLS}:
            return err(-32602, f"unknown tool {name}")
        try:
            out = getattr(daemon, name)(args)
            return ok({"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}]})
        except Refused as e:
            return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
    return err(-32601, f"method not found: {method}")


def serve_stdio(daemon):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        res = handle(daemon, msg) if isinstance(msg, dict) else None
        if res is not None:
            sys.stdout.write(json.dumps(res) + "\n")
            sys.stdout.flush()


def serve_http(daemon, port):
    local = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body=None):
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            # A page that rebinds its DNS to 127.0.0.1 still sends its own Host; a browser always sends an Origin.
            if self.headers.get("Host", "") not in local or self.headers.get("Origin"):
                return self.send(403, {"error": "forbidden"})
            if urlparse(self.path).path.rstrip("/") not in ("", "/mcp"):
                return self.send(404, {"error": "not found"})
            try:
                msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            except ValueError:
                return self.send(400, {"error": "bad json"})
            if isinstance(msg, list):
                res = [r for r in (handle(daemon, m) for m in msg if isinstance(m, dict)) if r is not None]
                return self.send(200, res) if res else self.send(202)
            res = handle(daemon, msg) if isinstance(msg, dict) else None
            return self.send(200, res) if res is not None else self.send(202)

        def do_GET(self):
            self.send(405, {"error": "POST JSON-RPC to /mcp"})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    log(f"listening on http://127.0.0.1:{port}/mcp")
    srv.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(description="peerlink as an MCP endpoint for clients outside Claude Code")
    ap.add_argument("--name", default=os.environ.get("PEERLINK_MCP_NAME"), help="the peer's name (or PEERLINK_MCP_NAME)")
    ap.add_argument("--space", default=os.environ.get("PEERLINK_MCP_SPACE"), help="the space it must be in (or PEERLINK_MCP_SPACE): refuses to start elsewhere")
    ap.add_argument("--cwd", default=os.environ.get("PEERLINK_MCP_CWD") or os.getcwd(), help="the folder it acts from; its folder decides its space")
    ap.add_argument("--http", type=int, metavar="PORT", help="serve MCP over HTTP on 127.0.0.1 instead of stdio")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PEERLINK_PORT", "7899")), help="the peerlink daemon's port")
    a = ap.parse_args(argv)
    if not a.name or not a.name.strip():
        ap.error("--name (or PEERLINK_MCP_NAME) is required")
    daemon = Daemon(a.name.strip(), os.path.abspath(a.cwd), (a.space or "").strip(), a.port, HOME)
    try:
        daemon.register()
    except Refused as e:
        log(e)
        return 1
    threading.Thread(target=daemon.run_heartbeats, daemon=True).start()
    try:
        serve_http(daemon, a.http) if a.http else serve_stdio(daemon)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
