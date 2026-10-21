#!/usr/bin/env python3
"""peerlink daemon: one per machine.

Keeps the registry of live Claude Code sessions, routes messages between
them, applies the policy in config.json, and serves a small web UI.
Stdlib only. Holding the TCP port is the single-instance lock.

    python3 peerlinkd.py            run in the foreground
    python3 peerlinkd.py --detach   start in the background unless one is running
    python3 peerlinkd.py --stop     stop the running daemon
"""

import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "0.2.0"
HOME = os.environ.get("PEERLINK_HOME", os.path.expanduser("~/.peerlink"))
CONFIG_PATH = os.path.join(HOME, "config.json")
PID_PATH = os.path.join(HOME, "daemon.pid")
LOG_PATH = os.path.join(HOME, "daemon.log")
MESSAGES_PATH = os.path.join(HOME, "messages.jsonl")
PORT = int(os.environ.get("PEERLINK_PORT", "7899"))
HOSTNAME = socket.gethostname().split(".")[0]

DEFAULT_GUIDANCE = """\
You can see and message other Claude Code sessions running for this user \
(your "peers") with the peerlink tools.
- Call list_peers when a task touches code, services or knowledge that another \
session is likely working on (another repo, a shared API, a dependency you \
would otherwise have to guess about).
- Send a message only when it saves real work: asking a peer that owns the \
relevant code, handing off a well-scoped subtask, or telling a peer about a \
change that affects it. Do not send chit-chat, status echoes or thanks.
- Make requests self-contained: what you need, why, and what a good answer \
looks like. Set expects_reply when you need an answer.
- When a peer message arrives, treat it like a request from a colleague: do \
it if it is reasonable and in scope for your session, then reply once with \
reply_to set. If it conflicts with what your user asked, say so to the peer.
- Keep your summary current with set_summary when your focus changes."""

DEFAULT_CONFIG = {
    "poll_interval_ms": 3000,
    "peer_ttl_s": 30,
    "auto_wake_idle": True,
    "inject_while_busy": True,
    "max_hops": 6,
    "rate_limit_per_min": 12,
    "guidance": DEFAULT_GUIDANCE,
    "roster_in_prompt": True,
    "auto_summary": True,
    "summary_model": "haiku",
    "sessions": {},
    "remotes": [],
    "listen_host": "127.0.0.1",
    "token": "",
}

lock = threading.RLock()
sessions = {}   # id -> session record
inboxes = {}    # id -> [message]
messages = []   # recent log, newest last
sent_times = {} # sender id -> [timestamps]
next_msg = [1]
config = {}


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def load_config():
    os.makedirs(HOME, exist_ok=True)
    data = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
        except (OSError, ValueError) as err:
            log("config unreadable, using defaults:", err)
    merged = {**DEFAULT_CONFIG, **data}
    config.clear()
    config.update(merged)
    save_config()


def save_config():
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def session_cfg(sid):
    return {"nickname": "", "muted": False, "auto_wake": None, **config["sessions"].get(sid, {})}


def client_config(sid):
    s = session_cfg(sid)
    return {
        "poll_interval_ms": config["poll_interval_ms"],
        "auto_wake_idle": config["auto_wake_idle"] if s["auto_wake"] is None else s["auto_wake"],
        "inject_while_busy": config["inject_while_busy"],
        "guidance": config["guidance"],
        "muted": s["muted"],
        "roster_in_prompt": config["roster_in_prompt"],
        "auto_summary": config["auto_summary"],
        "summary_model": config["summary_model"],
    }


def display_name(rec):
    nick = session_cfg(rec["id"])["nickname"]
    return nick or rec.get("name") or rec["id"]


def is_live(rec, now=None):
    return (now or time.time()) - rec["last_seen"] < config["peer_ttl_s"]


def public_peer(rec):
    return {
        "id": rec["id"],
        "name": display_name(rec),
        "host": HOSTNAME,
        "cwd": rec.get("cwd", ""),
        "repo": rec.get("repo", ""),
        "branch": rec.get("branch", ""),
        "summary": rec.get("summary", ""),
        "busy": rec.get("busy", False),
        "muted": session_cfg(rec["id"])["muted"],
        "last_seen_s": round(time.time() - rec["last_seen"], 1),
    }


def local_peers():
    now = time.time()
    with lock:
        return [public_peer(r) for r in sessions.values() if is_live(r, now)]


def remote_call(base, path, body=None, timeout=2):
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=None if body is None else json.dumps(body).encode(),
        method="GET" if body is None else "POST",
        headers={"Content-Type": "application/json", "X-Peerlink-Token": config.get("token", "")},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def remote_peers():
    found = []
    for base in config.get("remotes", []):
        try:
            for p in remote_call(base, "/api/peers?local=1").get("peers", []):
                p["remote"] = base
                found.append(p)
        except Exception as err:  # a remote being down is normal
            log("remote", base, "unreachable:", err)
    return found


def resolve(to):
    """Find a live local session by id, id prefix, nickname or name."""
    to = to.strip().lower()
    with lock:
        live = [r for r in sessions.values() if is_live(r)]
        for match in (
            lambda r: r["id"].lower() == to,
            lambda r: display_name(r).lower() == to,
            lambda r: r["id"].lower().startswith(to),
            lambda r: (r.get("name") or "").lower() == to,
        ):
            hits = [r for r in live if match(r)]
            if len(hits) == 1:
                return hits[0], None
            if len(hits) > 1:
                return None, f"'{to}' matches {len(hits)} sessions; use the id"
    return None, None


def record(msg):
    messages.append(msg)
    del messages[:-500]
    try:
        with open(MESSAGES_PATH, "a") as f:
            f.write(json.dumps(msg) + "\n")
    except OSError:
        pass


def send(body):
    sender = body.get("from", "")
    text = (body.get("text") or "").strip()
    to = body.get("to", "")
    if not text or not to:
        return 400, {"error": "to and text are required"}
    now = time.time()
    with lock:
        sender_rec = sessions.get(sender)
        hops = 0
        parent = None
        if body.get("reply_to"):
            parent = next((m for m in messages if m["id"] == body["reply_to"]), None)
            hops = (parent["hops"] + 1) if parent else int(body.get("hops", 0)) + 1
        elif body.get("hops") is not None:
            hops = int(body["hops"])
        if hops > config["max_hops"]:
            return 429, {"error": f"conversation hit max_hops={config['max_hops']}; stop and tell your user"}
        if sender_rec is not None:
            recent = [t for t in sent_times.get(sender, []) if now - t < 60]
            if len(recent) >= config["rate_limit_per_min"]:
                return 429, {"error": f"rate limit: {config['rate_limit_per_min']} messages/min"}
            sent_times[sender] = recent

        target, err = resolve(to)
        if err:
            return 409, {"error": err}

    if target is None:
        if "@" in to or config.get("remotes"):
            return forward(body, hops)
        return 404, {"error": f"no live peer '{to}'; call list_peers"}

    with lock:
        if session_cfg(target["id"])["muted"]:
            return 403, {"error": f"{display_name(target)} is muted by the user"}
        if target["id"] == sender:
            return 400, {"error": "that is you"}
        msg = {
            "id": f"m{next_msg[0]}",
            "ts": now,
            "from": sender,
            "from_name": body.get("from_name") or (display_name(sender_rec) if sender_rec else sender),
            "from_host": body.get("from_host", HOSTNAME),
            "to": target["id"],
            "to_name": display_name(target),
            "text": text[:20000],
            "expects_reply": bool(body.get("expects_reply")),
            "reply_to": body.get("reply_to") or None,
            "hops": hops,
            "status": "queued",
        }
        next_msg[0] += 1
        inboxes.setdefault(target["id"], []).append(msg)
        if sender in sent_times:
            sent_times[sender].append(now)
        record(msg)
    log("msg", msg["id"], msg["from_name"], "->", msg["to_name"])
    return 200, {"id": msg["id"], "to": msg["to_name"], "status": "queued"}


def forward(body, hops):
    to = body["to"]
    name, _, host = to.partition("@")
    for p in remote_peers():
        if (host and p["host"].lower() != host.lower()):
            continue
        if name.lower() in (p["id"].lower(), p["name"].lower()) or p["id"].lower().startswith(name.lower()):
            sender = sessions.get(body.get("from", ""))
            fwd = {
                **body,
                "to": p["id"],
                "hops": hops,
                "reply_to": None,
                "from": f"{body.get('from', '')}@{HOSTNAME}",
                "from_name": f"{display_name(sender) if sender else body.get('from')}@{HOSTNAME}",
                "from_host": HOSTNAME,
            }
            try:
                res = remote_call(p["remote"], "/api/messages", fwd, timeout=5)
                return 200, {**res, "to": f"{p['name']}@{p['host']}"}
            except Exception as err:
                return 502, {"error": f"remote {p['remote']} refused: {err}"}
    return 404, {"error": f"no live peer '{to}'; call list_peers"}


def take_inbox(sid):
    with lock:
        box = inboxes.pop(sid, [])
        for m in box:
            m["status"] = "delivered"
            m["delivered_ts"] = time.time()
        return box


def reaper():
    while True:
        time.sleep(10)
        now = time.time()
        with lock:
            for sid in [s for s, r in sessions.items() if now - r["last_seen"] > config["peer_ttl_s"] * 10]:
                sessions.pop(sid, None)
                inboxes.pop(sid, None)


class Handler(BaseHTTPRequestHandler):
    server_version = "peerlink/" + VERSION

    def log_message(self, *args):
        pass

    def reply(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    def allowed(self):
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        token = config.get("token", "")
        return bool(token) and self.headers.get("X-Peerlink-Token") == token

    def route(self, method):
        if not self.allowed():
            return self.reply(403, {"error": "forbidden"})
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        q = parse_qs(url.query)

        if method == "GET" and url.path in ("/", "/index.html"):
            return self.reply(200, UI_HTML, "text/html; charset=utf-8")
        if parts[:1] != ["api"]:
            return self.reply(404, {"error": "not found"})
        rest = parts[1:]

        if rest == ["health"]:
            return self.reply(200, {"ok": True, "version": VERSION, "pid": os.getpid(), "host": HOSTNAME})

        if rest == ["config"]:
            if method == "GET":
                return self.reply(200, config)
            if method in ("PUT", "POST"):
                with lock:
                    for k, v in self.body().items():
                        if k in DEFAULT_CONFIG:
                            config[k] = v
                    save_config()
                return self.reply(200, config)

        if rest == ["peers"] and method == "GET":
            peers = local_peers()
            if "local" not in q:
                peers += remote_peers()
            exclude = q.get("exclude", [""])[0]
            return self.reply(200, {"host": HOSTNAME, "peers": [p for p in peers if p["id"] != exclude]})

        if rest == ["sessions"] and method == "POST":
            b = self.body()
            sid = b.get("id")
            if not sid:
                return self.reply(400, {"error": "id required"})
            with lock:
                rec = sessions.get(sid, {"id": sid, "started": time.time()})
                for k in ("name", "cwd", "repo", "branch", "summary", "model"):
                    if b.get(k) is not None:
                        rec[k] = b[k]
                rec["busy"] = bool(b.get("busy", False))
                rec["last_seen"] = time.time()
                sessions[sid] = rec
            log("session", sid, rec.get("name"))
            return self.reply(200, {"id": sid, "name": display_name(rec), "config": client_config(sid)})

        if len(rest) == 3 and rest[0] == "sessions" and rest[2] == "heartbeat" and method == "POST":
            sid = rest[1]
            b = self.body()
            with lock:
                rec = sessions.get(sid)
                if rec is None:
                    return self.reply(404, {"error": "unknown session; register again"})
                rec["last_seen"] = time.time()
                if "busy" in b:
                    rec["busy"] = bool(b["busy"])
                if b.get("summary") is not None:
                    rec["summary"] = b["summary"][:500]
                cfg = client_config(sid)
            inbox = [] if cfg["muted"] else take_inbox(sid)
            return self.reply(200, {"name": display_name(rec), "config": cfg, "inbox": inbox})

        if len(rest) == 2 and rest[0] == "sessions" and method == "DELETE":
            with lock:
                sessions.pop(rest[1], None)
            return self.reply(200, {"ok": True})

        if rest == ["messages"]:
            if method == "GET":
                with lock:
                    return self.reply(200, {"messages": messages[-int(q.get("limit", ["100"])[0]):]})
            if method == "POST":
                code, res = send(self.body())
                return self.reply(code, res)

        if rest == ["shutdown"] and method == "POST":
            self.reply(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        return self.reply(404, {"error": "not found"})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def do_DELETE(self):
        self.route("DELETE")


def health():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=1) as r:
            return json.loads(r.read())
    except Exception:
        return None


def detach():
    """Start a background daemon unless one already answers; return at once."""
    if health():
        print(json.dumps({"running": True, "started": False}))
        return
    os.makedirs(HOME, exist_ok=True)
    if os.fork() > 0:
        for _ in range(50):
            time.sleep(0.1)
            if health():
                break
        print(json.dumps({"running": bool(health()), "started": True}))
        return
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDONLY)
    out = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(devnull, 0)
    os.dup2(out, 1)
    os.dup2(out, 2)
    serve()
    os._exit(0)


def serve():
    load_config()
    try:
        server = ThreadingHTTPServer((config["listen_host"], PORT), Handler)
    except OSError as err:
        log("another daemon holds port", PORT, "-", err)
        sys.exit(0)
    server.daemon_threads = True
    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    threading.Thread(target=reaper, daemon=True).start()
    log(f"peerlink {VERSION} on http://{config['listen_host']}:{PORT} (pid {os.getpid()})")
    try:
        server.serve_forever()
    finally:
        if os.path.exists(PID_PATH):
            os.remove(PID_PATH)


def stop():
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/shutdown", data=b"{}", method="POST")
        urllib.request.urlopen(req, timeout=2)
        print("stopped")
    except Exception:
        print("not running")


UI_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Peerlink</title>
<style>
:root{--bg:#faf9f7;--fg:#1f1e1c;--mut:#6b6862;--line:#e4e1db;--card:#fff;--acc:#c96442;--ok:#2f7d4f;--warn:#b7791f}
@media (prefers-color-scheme:dark){:root{--bg:#1c1b19;--fg:#ecebe8;--mut:#9b978f;--line:#34322e;--card:#252421;--acc:#e0805e;--ok:#5cb57f;--warn:#e0b04a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 ui-sans-serif,system-ui,-apple-system,sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:20px;margin:0}h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:28px 0 10px}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}.mut{color:var(--mut)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.peer .top{display:flex;justify-content:space-between;gap:8px;align-items:center}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--ok)}.dot.busy{background:var(--warn)}
.name{font-weight:600}.small{font-size:12px}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;word-break:break-all}
input[type=text],input[type=number],textarea{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit}
textarea{min-height:180px;font-family:ui-monospace,Menlo,monospace;font-size:12px}
label{display:block;margin:8px 0 4px;font-size:12px;color:var(--mut)}
.row{display:flex;gap:12px;flex-wrap:wrap}.row>div{flex:1 1 180px}
button{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:7px 14px;font:inherit;cursor:pointer}
button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--mut);font-weight:500}.msgtext{white-space:pre-wrap;max-height:6em;overflow:auto}
.tablewrap{overflow-x:auto}.empty{color:var(--mut);padding:14px 0}
.check{display:flex;align-items:center;gap:6px;margin-top:8px}
</style></head><body><main>
<header><h1>Peerlink</h1><span class="mut" id="hostline"></span></header>

<h2>Sessions</h2><div class="grid" id="peers"></div>

<h2>Messages</h2><div class="card tablewrap"><table><thead><tr><th>When</th><th>From → To</th><th>Message</th><th>Status</th></tr></thead><tbody id="msgs"></tbody></table></div>

<h2>Settings</h2>
<form class="card" id="cfg">
<div class="row">
<div><label>Poll interval (ms)</label><input type="number" name="poll_interval_ms" min="500"></div>
<div><label>Peer timeout (s)</label><input type="number" name="peer_ttl_s" min="5"></div>
<div><label>Max reply hops</label><input type="number" name="max_hops" min="1"></div>
<div><label>Messages per minute per session</label><input type="number" name="rate_limit_per_min" min="1"></div>
</div>
<div class="check"><input type="checkbox" name="auto_wake_idle" id="aw"><label for="aw" style="margin:0">Wake idle sessions when a message arrives</label></div>
<div class="check"><input type="checkbox" name="inject_while_busy" id="ib"><label for="ib" style="margin:0">Deliver into a running turn</label></div>
<div class="check"><input type="checkbox" name="roster_in_prompt" id="rp"><label for="rp" style="margin:0">Show each session the live peer list when it changes</label></div>
<div class="check"><input type="checkbox" name="auto_summary" id="as"><label for="as" style="margin:0">Keep session summaries current automatically</label></div>
<div class="row"><div><label>Summary model</label><input type="text" name="summary_model"></div><div></div></div>
<label>Guidance given to every session (when to message peers)</label><textarea name="guidance"></textarea>
<label>Remote daemons (one URL per line, e.g. http://other-mac.local:7899)</label><textarea name="remotes" style="min-height:60px"></textarea>
<div class="row"><div><label>Listen host (restart daemon to apply; use 0.0.0.0 for remotes)</label><input type="text" name="listen_host"></div>
<div><label>Shared token for remote daemons</label><input type="text" name="token"></div></div>
<p><button type="submit">Save</button> <span class="mut small" id="saved"></span></p>
</form>
</main>
<script>
const $=s=>document.querySelector(s);let cfg={};
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(p,o){const r=await fetch(p,o);return r.json()}
async function setSession(id,patch){const s={...cfg.sessions};s[id]={...(s[id]||{}),...patch};cfg=await api('/api/config',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({sessions:s})});refresh()}
async function refresh(){
  const [h,p,m]=await Promise.all([api('/api/health'),api('/api/peers'),api('/api/messages?limit=50')]);
  $('#hostline').textContent=`${h.host} · daemon ${h.version} · pid ${h.pid}`;
  $('#peers').innerHTML=p.peers.length?p.peers.map(x=>`<div class="card peer">
   <div class="top"><span><span class="dot ${x.busy?'busy':''}"></span><span class="name">${esc(x.name)}</span> <span class="mut small">${x.remote?'@'+esc(x.host):''}</span></span>
   <span class="mut small">${x.busy?'working':'idle'} · ${x.last_seen_s}s</span></div>
   <div class="mono mut" style="margin-top:4px">${esc(x.cwd)}${x.branch?' · '+esc(x.branch):''}</div>
   <div style="margin-top:6px">${esc(x.summary)||'<span class="mut">no summary yet</span>'}</div>
   ${x.remote?'':`<div class="row" style="margin-top:8px;align-items:center"><div><input type="text" placeholder="nickname" value="${esc((cfg.sessions?.[x.id]||{}).nickname||'')}" onchange="setSession('${x.id}',{nickname:this.value})"></div>
   <div style="flex:0 0 auto"><button class="ghost" onclick="setSession('${x.id}',{muted:${!x.muted}})">${x.muted?'Unmute':'Mute'}</button></div></div>
   <div class="mono mut small" style="margin-top:6px">${esc(x.id)}</div>`}</div>`).join(''):'<div class="empty">No live sessions. Start Claude Code with the peerlink plugin.</div>';
  $('#msgs').innerHTML=m.messages.slice().reverse().map(x=>`<tr><td class="small mut">${new Date(x.ts*1000).toLocaleTimeString()}</td>
   <td><b>${esc(x.from_name)}</b> → <b>${esc(x.to_name)}</b>${x.reply_to?`<div class="small mut">re ${esc(x.reply_to)}</div>`:''}</td>
   <td><div class="msgtext">${esc(x.text)}</div></td><td class="small">${esc(x.status)} <span class="mut">${esc(x.id)}</span></td></tr>`).join('')||'<tr><td colspan="4" class="empty">No messages yet.</td></tr>';
}
async function loadCfg(){cfg=await api('/api/config');const f=$('#cfg');
  for(const el of f.elements){if(!el.name)continue;const v=cfg[el.name];
    if(el.type==='checkbox')el.checked=!!v;else if(el.name==='remotes')el.value=(v||[]).join('\n');else el.value=v??''}}
$('#cfg').addEventListener('submit',async e=>{e.preventDefault();const f=e.target,b={};
  for(const el of f.elements){if(!el.name)continue;
    b[el.name]=el.type==='checkbox'?el.checked:el.type==='number'?Number(el.value):el.name==='remotes'?el.value.split('\n').map(s=>s.trim()).filter(Boolean):el.value}
  cfg=await api('/api/config',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
  $('#saved').textContent='Saved '+new Date().toLocaleTimeString()});
loadCfg().then(refresh);setInterval(refresh,2000);
</script></body></html>
"""


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--detach":
        detach()
    elif arg == "--stop":
        stop()
    elif arg == "--status":
        print(json.dumps(health() or {"ok": False}))
    else:
        serve()
