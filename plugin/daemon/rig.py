"""A test rig: the real daemon, in its own process on a temp home and a free port, and fake agents that talk to it
over HTTP the way the plugin does (register, heartbeat, ack, send). Scenarios in test_scenarios.py run on it, so a
change to identity, delivery or the daemon's layout is tried against several agents, a /clear and a restart, not
only against one function at a time.

    with Rig() as rig:
        lead, worker = rig.agent("lead", space="acme"), rig.agent("worker", space="acme")
        lead.send(to="worker", text="hello")
        assert worker.poll()[0]["text"] == "hello"
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(HERE, "peerlinkd.py")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Rig:
    """One daemon and the folders its agents live in. `env` overrides its environment; `config` is written to its
    config.json before it starts."""

    def __init__(self, config=None, env=None):
        self.home = tempfile.mkdtemp(prefix="peerlink-rig-")
        self.work = os.path.realpath(tempfile.mkdtemp(prefix="peerlink-rig-work-"))
        self.port = free_port()
        self.env = {**os.environ, "PEERLINK_HOME": self.home, "PEERLINK_PORT": str(self.port), "PEERLINK_REDELIVER_S": "1",
                    "PYTHONUNBUFFERED": "1", **(env or {})}
        # Fast enough that a scenario doesn't wait on wall-clock defaults; nothing here is what a scenario is about.
        self.config = {"rate_limit_per_min": 1000, "wake_for": "needed", "peer_ttl_s": 30, **(config or {})}
        self.proc = self.cookie = None
        self.agents = []

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.work, ignore_errors=True)

    # The daemon
    def start(self):
        path = os.path.join(self.home, "config.json")
        if not os.path.exists(path):
            with open(path, "w") as f:
                json.dump(self.config, f)
        self.log = open(os.path.join(self.home, "rig.log"), "a")
        self.proc = subprocess.Popen([sys.executable, DAEMON, "serve"], env=self.env, stdout=self.log, stderr=self.log)
        for _ in range(100):
            if self.proc.poll() is not None:
                raise RuntimeError("the daemon exited: " + self.output())
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/health", timeout=1) as r:
                    json.loads(r.read())
                break
            except (OSError, ValueError):
                time.sleep(0.05)
        else:
            raise RuntimeError("the daemon didn't answer: " + self.output())
        with open(os.path.join(self.home, "key")) as f:
            self.key = f.read().strip()
        self.cookie = None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if getattr(self, "log", None):
            self.log.close()

    def restart(self):
        """Stop the daemon and start it again on the same home, as an update does. Agents register again themselves
        (poll does, on the daemon's 404), as the plugin does."""
        self.stop()
        self.start()

    def output(self):
        try:
            with open(os.path.join(self.home, "rig.log")) as f:
                return f.read()[-2000:]
        except OSError:
            return ""

    def call(self, method, path, body=None, user=False):
        """One request: as an agent's plugin (the key), or as the user in the browser (a signed-in session)."""
        headers = {"Content-Type": "application/json", "Host": f"127.0.0.1:{self.port}"}
        if user:
            headers["Cookie"] = self.signed_in()
        else:
            headers["X-Peerlink-Key"] = self.key
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return err.code, json.loads(raw or b"{}")
            except ValueError:
                return err.code, {"error": raw.decode(errors="replace")}

    def signed_in(self):
        if not self.cookie:
            _, link = self.call("POST", "/api/web/link", {})

            class Stay(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None
            try:
                r = urllib.request.build_opener(Stay).open(link["url"], timeout=10)
            except urllib.error.HTTPError as err:
                r = err
            self.cookie = (r.headers.get("Set-Cookie") or "").split(";")[0]
            if not self.cookie:
                raise RuntimeError("no session cookie from the sign-in link")
        return self.cookie

    def user(self, method, path, body=None):
        """What the user does in the web UI; returns the answer, raising on a refusal."""
        code, out = self.call(method, path, body, user=True)
        if code != 200:
            raise AssertionError(f"{method} {path} as the user: {code} {out}")
        return out

    # The agents
    def folder(self, name, space="", parent=""):
        """A folder for an agent, in `space` by a .peerlink file when one is named."""
        path = os.path.join(self.work, name)
        os.makedirs(path, exist_ok=True)
        if space:
            with open(os.path.join(path, ".peerlink"), "w") as f:
                f.write(f"space: {space}\n" + (f"parent: {parent}\n" if parent else ""))
        return path

    def agent(self, name, space="", parent="", folder=None):
        a = Agent(self, name, folder or self.folder(name, space, parent))
        a.register()
        self.agents.append(a)
        return a

    def wait(self, check, what="the condition", timeout=5.0):
        """Poll until check() is truthy and return it; fail saying what never happened."""
        end = time.time() + timeout
        while time.time() < end:
            got = check()
            if got:
                return got
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}\n{self.output()[-800:]}")


class Agent:
    """One Claude Code session's plugin, as the daemon sees it."""

    def __init__(self, rig, name, cwd):
        self.rig, self.name, self.cwd = rig, name, cwd
        self.sid = str(uuid.uuid4())
        self.seen = []      # every message that reached this agent, in order
        self.config = {}

    def register(self, succeeds=None):
        code, out = self.rig.call("POST", "/api/sessions", {"id": self.sid, "name": self.name, "had_name": self.name if succeeds else None,
                                                             "cwd": self.cwd, "plugin": "rig", "succeeds": succeeds})
        if code != 200:
            raise AssertionError(f"{self.name} couldn't register: {code} {out}")
        self.name, self.config = out.get("name", self.name), out.get("config", {})
        return out

    def poll(self, ack=True, holding=()):
        """One heartbeat: the new messages (and any handed out again). Registers again when the daemon forgot it."""
        code, out = self.rig.call("POST", f"/api/sessions/{self.sid}/heartbeat", {"busy": False, "ack": [], "holding": list(holding)})
        if code == 404:
            self.register()
            code, out = self.rig.call("POST", f"/api/sessions/{self.sid}/heartbeat", {"busy": False, "ack": [], "holding": list(holding)})
        if code != 200:
            raise AssertionError(f"{self.name}'s heartbeat: {code} {out}")
        self.name, self.config = out.get("name", self.name), out.get("config", self.config)
        inbox = out.get("inbox", [])
        self.seen += inbox
        if ack and inbox:
            self.rig.call("POST", f"/api/sessions/{self.sid}/ack", {"ids": [m["id"] for m in inbox]})
        return inbox

    def texts(self):
        self.poll()
        return [m["text"] for m in self.seen]

    def expect(self, part, timeout=5.0):
        """Wait for a message containing `part`, and return it."""
        def found():
            self.poll()
            return next((m for m in self.seen if part in m["text"]), None)
        return self.rig.wait(found, f"{self.name} to get a message with {part!r}; it has {[m['text'][:60] for m in self.seen]}", timeout)

    def send(self, ok=True, **body):
        code, out = self.rig.call("POST", "/api/messages", {"from": self.sid, "from_name": self.name, **body})
        if ok and code != 200:
            raise AssertionError(f"{self.name} couldn't send {body}: {code} {out}")
        return out if ok else (code, out)

    def quit(self):
        """Claude Code exits: the plugin says so on its way out."""
        self.rig.call("DELETE", f"/api/sessions/{self.sid}")

    def restart(self):
        """Claude Code quit and started again in the folder: a new session that says nothing of the old one but the
        name it had. Messages the old session never read wait for it."""
        self.quit()
        self.sid = str(uuid.uuid4())
        self.seen = []
        code, out = self.rig.call("POST", "/api/sessions", {"id": self.sid, "name": os.path.basename(self.cwd), "had_name": self.name,
                                                             "cwd": self.cwd, "plugin": "rig"})
        if code != 200:
            raise AssertionError(f"{self.name} couldn't register: {code} {out}")
        self.name, self.config = out.get("name", self.name), out.get("config", {})
        return out

    def summary(self, text):
        self.rig.call("POST", f"/api/sessions/{self.sid}/heartbeat", {"busy": False, "summary": text, "summary_kind": "model"})

    def clear(self):
        """A /clear or resume: the same agent under a new session id, as the plugin announces it."""
        old, self.sid = self.sid, str(uuid.uuid4())
        self.register(succeeds=old)
        return old

    def call(self, method, path, body=None):
        return self.rig.call(method, path, None if body is None and method == "GET" else {"by": self.sid, **(body or {})})

    def peers(self):
        _, out = self.rig.call("GET", f"/api/peers?local=1&exclude={self.sid}")
        return {p["name"]: p for p in out["peers"]}
