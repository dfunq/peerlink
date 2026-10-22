#!/usr/bin/env python3
"""peerlink daemon: one per machine.

Keeps the registry of live Claude agents (Claude Code sessions), routes messages between
them, applies the policy in config.json, and serves a small web UI.
Stdlib only. Holding the TCP port is the single-instance lock.

Every API call needs the key in ~/.peerlink/key (agents send it as
X-Peerlink-Key). A browser gets in through a one-time link signed with that
key (HMAC-SHA256, valid 60 s, usable once), which sets a session cookie.

    peerlink [command]   (~/.peerlink/bin/peerlink, or python3 peerlinkd.py)
"""

import hashlib
import re
import hmac
import json
import secrets
import subprocess
import os
import signal
import socket
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "0.6.14"
# A fingerprint of this file: an agent whose plugin ships different code at the same version replaces the daemon.
with open(os.path.abspath(__file__), "rb") as _f:
    CODE = hashlib.sha256(_f.read()).hexdigest()[:16]
HOME = os.environ.get("PEERLINK_HOME", os.path.expanduser("~/.peerlink"))
CONFIG_PATH = os.path.join(HOME, "config.json")
PID_PATH = os.path.join(HOME, "daemon.pid")
LOG_PATH = os.path.join(HOME, "daemon.log")
MESSAGES_PATH = os.path.join(HOME, "messages.jsonl")
ISSUES_PATH = os.path.join(HOME, "issues.jsonl")
# Reports an agent wrote while the daemon was down; the daemon files them when it starts.
PENDING_ISSUES_DIR = os.path.join(HOME, "issues-pending")
KEY_PATH = os.path.join(HOME, "key")
DIRS_PATH = os.path.join(HOME, "dirs.json")
DIR_MEMORY_S = 14 * 86400
LAUNCHER_PATH = os.path.join(HOME, "bin", "peerlink")
LINK_TTL_S = 60
WEB_SESSION_TTL_S = 12 * 3600
COOKIE = "peerlink_session"
PORT = int(os.environ.get("PEERLINK_PORT", "7899"))
HOSTNAME = socket.gethostname().split(".")[0]
SPACE_FILE = ".peerlink"
DEFAULT_SPACE = "default"
SPACE_CACHE_S = 5

OLD_GUIDANCE_V1 = """\
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

DEFAULT_GUIDANCE = """\
Other Claude agents working for this user are your peers: colleagues in \
other repos and folders on the same machine. Talk to them the way you would \
message a teammate.
- Reach out when a peer clearly owns what you need (their repo, service or \
current task) instead of guessing, or when a change of yours affects them.
- Be brief and direct. You share a filesystem with local peers: point to \
paths and line numbers instead of pasting code or logs. Put all your \
questions in one message.
- Ask for a reply only when you need one, and keep working while you wait.
- When a peer writes to you, help if it fits your user's goals, answer once \
and to the point, and skip acknowledgements, thanks and sign-offs.
- Tell your user about any peer exchange that changes your plan.
- If peerlink itself misbehaves (a reply that never comes, a message that \
reached the wrong agent, an error from these tools), call report_issue with \
the message ids and agent names, then carry on with your task."""

DEFAULT_CONFIG = {
    "poll_interval_ms": 3000,
    "peer_ttl_s": 30,
    "auto_wake_idle": True,
    "inject_while_busy": True,
    "max_hops": 6,
    "rate_limit_per_min": 12,
    "max_message_chars": 8000,
    "guidance": "",
    "issue_agent": "peerlink",
    "roster_in_prompt": True,
    "auto_summary": True,
    "auto_reload": True,
    "summary_model": "haiku",
    "sessions": {},
    "space_rules": [],
    "folder_names": {},
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
key = [""]
dir_summaries = {}  # cwd -> {summary, name, ts}: what the last agent there was doing
used_nonces = {}   # nonce -> expiry
web_sessions = {}  # cookie -> expiry; kept on disk, so a daemon restart doesn't sign browsers out
WEB_SESSIONS_PATH = os.path.join(HOME, "web_sessions.json")


def save_web_sessions():
    now = time.time()
    live = {c: e for c, e in web_sessions.items() if e > now}
    try:
        fd = os.open(WEB_SESSIONS_PATH + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(live, f)
        os.replace(WEB_SESSIONS_PATH + ".tmp", WEB_SESSIONS_PATH)
    except OSError:
        pass


def load_web_sessions():
    try:
        with open(WEB_SESSIONS_PATH) as f:
            web_sessions.update({c: float(e) for c, e in json.load(f).items() if float(e) > time.time()})
    except (OSError, ValueError, AttributeError):
        pass


def read_key():
    try:
        with open(KEY_PATH) as f:
            return f.read().strip()
    except OSError:
        return ""


def write_key():
    os.makedirs(HOME, exist_ok=True)
    fd = os.open(KEY_PATH + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_hex(32))
    os.replace(KEY_PATH + ".tmp", KEY_PATH)
    return read_key()


def load_key():
    key[0] = read_key() or write_key()


def rotate_key():
    """A new key: every browser session ends and old links stop working."""
    with lock:
        key[0] = write_key()
        web_sessions.clear()
        used_nonces.clear()
        save_web_sessions()


def sign(payload, secret):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def mint_link(secret, host="127.0.0.1"):
    nonce = secrets.token_hex(16)
    exp = int(time.time()) + LINK_TTL_S
    payload = f"{nonce}.{exp}"
    return f"http://{host}:{PORT}/login?t={payload}.{sign(payload, secret)}"


def redeem(token):
    """Check a one-time link token; on success open a browser session."""
    try:
        nonce, exp, sig = token.split(".")
        exp = int(exp)
    except ValueError:
        return None
    now = time.time()
    with lock:
        for n in [n for n, e in used_nonces.items() if e < now]:
            used_nonces.pop(n)
        if exp < now or nonce in used_nonces:
            return None
        if not hmac.compare_digest(sign(f"{nonce}.{exp}", key[0]), sig):
            return None
        used_nonces[nonce] = exp
        cookie = secrets.token_urlsafe(32)
        web_sessions[cookie] = now + WEB_SESSION_TTL_S
        save_web_sessions()
        return cookie


def load_dirs():
    try:
        with open(DIRS_PATH) as f:
            dir_summaries.update(json.load(f))
    except (OSError, ValueError):
        pass


def remember_dir(rec):
    if not rec.get("cwd") or not rec.get("summary"):
        return
    dir_summaries[rec["cwd"]] = {"summary": rec["summary"], "name": display_name(rec), "ts": time.time()}
    cutoff = time.time() - DIR_MEMORY_S
    for cwd in [c for c, d in dir_summaries.items() if d["ts"] < cutoff]:
        dir_summaries.pop(cwd)
    try:
        with open(DIRS_PATH + ".tmp", "w") as f:
            json.dump(dir_summaries, f)
        os.replace(DIRS_PATH + ".tmp", DIRS_PATH)
    except OSError:
        pass


def write_launcher():
    """~/.peerlink/bin/peerlink runs this file, wherever the plugin is installed."""
    os.makedirs(os.path.dirname(LAUNCHER_PATH), exist_ok=True)
    script = f'#!/bin/sh\nexec python3 "{os.path.abspath(__file__)}" "$@"\n'
    try:
        with open(LAUNCHER_PATH) as f:
            if f.read() == script:
                return
    except OSError:
        pass
    with open(LAUNCHER_PATH, "w") as f:
        f.write(script)
    os.chmod(LAUNCHER_PATH, 0o755)


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
    if merged.get("guidance") == OLD_GUIDANCE_V1:  # an untouched old default follows the new one
        merged["guidance"] = ""
    config.clear()
    config.update(merged)
    save_config()


def save_config():
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def session_cfg(sid):
    return {"nickname": "", "muted": False, "auto_wake": None, "space": "", **config["sessions"].get(sid, {})}


def client_config(sid):
    s = session_cfg(sid)
    rec = sessions.get(sid)
    space, source, where = space_of(rec) if rec else (DEFAULT_SPACE, "default", "")
    lead, children = hierarchy(rec) if rec else ("", [])
    return {
        "space": space,
        "space_source": source,
        "space_from": where,
        "parent": parent_of(rec) if rec else "",
        # The space this agent's space sits inside, for members too (a lead's `parent` is the same).
        "space_parent": read_space_file(where, "parent") if rec and source == "file" else "",
        "lead": lead,
        "children": children,
        "poll_interval_ms": config["poll_interval_ms"],
        "auto_wake_idle": config["auto_wake_idle"] if s["auto_wake"] is None else s["auto_wake"],
        "inject_while_busy": config["inject_while_busy"],
        "guidance": config["guidance"] or DEFAULT_GUIDANCE,
        "muted": s["muted"],
        "roster_in_prompt": config["roster_in_prompt"],
        "auto_summary": config["auto_summary"],
        "auto_reload": config.get("auto_reload", True),
        "summary_model": config["summary_model"],
    }


def clean_space(name):
    return re.sub(r"[^a-z0-9._-]+", "-", str(name or "").strip().lower()).strip("-")[:40]


def find_space_file(cwd):
    """The nearest .peerlink file at or above cwd. The home folder itself never counts: ~/.peerlink is ours."""
    home = os.path.expanduser("~")
    d = os.path.abspath(cwd) if cwd else ""
    while d and d != home and d != os.path.dirname(d):
        path = os.path.join(d, SPACE_FILE)
        if os.path.isfile(path):
            return path
        d = os.path.dirname(d)
    return None


def read_space_file(path, key="space"):
    try:
        with open(path) as f:
            for line in f:
                k, sep, v = line.partition(":") if ":" in line else line.partition("=")
                if sep and k.strip().lower() == key:
                    return clean_space(v)
    except OSError:
        pass
    return ""


def rule_space(cwd):
    """The space of the most specific folder rule that covers cwd."""
    best = None
    for rule in config.get("space_rules") or []:
        path = os.path.abspath(os.path.expanduser(str(rule.get("path", "")))) if rule.get("path") else ""
        name = clean_space(rule.get("space"))
        if name and path and (cwd == path or cwd.startswith(path.rstrip("/") + "/")):
            if best is None or len(path) > len(best[0]):
                best = (path, name)
    return best


def space_of(rec):
    """(space, source, where): this agent's override, else the nearest .peerlink, else a folder rule, else default."""
    now = time.time()
    cached = rec.get("_space")
    if cached and now - cached[0] < SPACE_CACHE_S:
        return cached[1]
    cwd = rec.get("cwd", "")
    found = (DEFAULT_SPACE, "default", "")
    override = clean_space(session_cfg(rec["id"])["space"])
    path = find_space_file(cwd)
    rule = rule_space(cwd)
    if override:
        found = (override, "override", "")
    elif path and read_space_file(path):
        found = (read_space_file(path), "file", path)
    elif rule:
        found = (rule[1], "rule", rule[0])
    # The agent in the folder of a .peerlink that names a `parent:` leads that space and also sits in the parent.
    parent = ""
    if found[1] == "file" and os.path.dirname(found[2]) == os.path.abspath(cwd or "/"):
        parent = read_space_file(found[2], "parent")
    rec["_space"] = (now, found, "" if parent == found[0] else parent)
    return found


def parent_of(rec):
    """The parent space this agent speaks for, if it leads its own space; else ''."""
    space_of(rec)
    return rec["_space"][2]


def reach(rec):
    """The spaces this agent sees and messages: its own, and the parent too when it leads its space."""
    parent = parent_of(rec)
    return {space_of(rec)[0], parent} - {""}


def peer_reach(p):
    return set(p.get("reach") or [p.get("space", DEFAULT_SPACE)])


def hierarchy(rec):
    """Who leads this agent's space, and the live leads of spaces inside it."""
    space = space_of(rec)[0]
    lead, children = "", []
    for r in sessions.values():
        if r["id"] == rec["id"] or not is_live(r) or not parent_of(r):
            continue
        if space_of(r)[0] == space:
            lead = display_name(r)
        elif parent_of(r) == space:
            children.append({"name": display_name(r), "space": space_of(r)[0]})
    return lead, children


def forget_spaces():
    with lock:
        for rec in sessions.values():
            rec.pop("_space", None)


def write_space_file(rec, space, key="space"):
    """Set or clear `space:` (or `parent:`) in the .peerlink that governs this agent's folder (the repo root for a new one)."""
    cwd = rec.get("cwd", "")
    try:
        root = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        root = ""
    root = os.path.abspath(root or cwd) if (root or cwd) else ""
    path = find_space_file(cwd)
    if path and root and os.path.dirname(path) != root and not os.path.dirname(path).startswith(root + "/"):
        if key != "space" or not space:
            raise ValueError(f"this folder's space comes from {path}, above this repo; change it there, or use an override")
        path = None  # the repo gets its own .peerlink
    if not path and key != "space":
        raise ValueError("this folder has no .peerlink space to give a parent; set its space first")
    if not path:
        if not root or root == os.path.expanduser("~"):
            raise ValueError("no project folder to put .peerlink in; use a folder rule or an override")
        path = os.path.join(root, SPACE_FILE)
    lines = []
    if os.path.exists(path):
        with open(path) as f:
            lines = [l for l in f.read().splitlines() if not re.match(rf"\s*{key}\s*[:=]", l, re.I)]
    elif space:
        lines = ["# peerlink: agents in this folder only see and message agents in the same space"]
    if space:
        lines.append(f"{key}: {space}")
    if any(l.strip() and not l.strip().startswith("#") for l in lines):
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
    elif os.path.exists(path):
        os.remove(path)
    forget_spaces()
    return path


def display_name(rec):
    nick = session_cfg(rec["id"])["nickname"]
    return nick or rec.get("name") or rec["id"]


def unique_name(name, sid):
    taken = {display_name(r).lower() for r in sessions.values() if r["id"] != sid and is_live(r)}
    candidate, n = name, 2
    while candidate.lower() in taken:
        candidate, n = f"{name}-{n}", n + 1
    return candidate


def is_live(rec, now=None):
    return (now or time.time()) - rec["last_seen"] < config["peer_ttl_s"]


def public_peer(rec):
    space, source, where = space_of(rec)
    return {
        "id": rec["id"],
        "name": display_name(rec),
        "host": HOSTNAME,
        "cwd": rec.get("cwd", ""),
        "repo": rec.get("repo", ""),
        "branch": rec.get("branch", ""),
        "summary": rec.get("summary", ""),
        "space": space,
        "space_source": source,
        "space_from": where,
        "parent": parent_of(rec),
        "file_parent": read_space_file(where, "parent") if source == "file" else "",
        # Without ages, which change every second and would redraw the page each poll.
        "threads": {k: [{f: t[f] for f in ("id", "with", "text")} for t in v] for k, v in open_threads(rec, limit=5).items()},
        "reach": sorted(reach(rec)),
        "busy": rec.get("busy", False),
        # The plugin version the agent runs ('' before 0.6.6): one behind the daemon needs /reload-plugins.
        "plugin": rec.get("plugin", ""),
        "context": rec.get("context"),  # percent of the context window in use, when the agent reports it
        "stale": rec.get("plugin", "") != VERSION,
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


def resolve(to, spaces=None):
    """Find a live local agent by id, name or id prefix; only one that shares one of `spaces` when given.

    A name is what the agent is shown as (its nickname once it has one), never the folder it was named after,
    so a renamed agent's old name can't land on someone else. A match outside `spaces` is an error, not a miss."""
    to = to.strip().lower()
    with lock:
        live = [r for r in sessions.values() if is_live(r)]
        for match in (
            lambda r: r["id"].lower() == to,
            lambda r: display_name(r).lower() == to,
            lambda r: r["id"].lower().startswith(to),
        ):
            found = [r for r in live if match(r)]
            hits = [r for r in found if spaces is None or reach(r) & spaces]
            if len(hits) == 1:
                return hits[0], None
            if len(hits) > 1:
                return None, f"'{to}' matches {len(hits)} agents; use the id"
            if found:
                return None, f"{display_name(found[0])} is in the '{space_of(found[0])[0]}' space, which you can't reach"
    return None, None


def append_log(entry):
    try:
        with open(MESSAGES_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def record(msg):
    messages.append(msg)
    del messages[:-500]
    append_log(msg)


def load_messages():
    """Replay the log so ids keep counting and undelivered messages survive a restart."""
    try:
        with open(MESSAGES_PATH) as f:
            lines = f.readlines()[-5000:]
    except OSError:
        return
    by_id = {}
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if "text" in entry:
            by_id[entry["id"]] = entry
        elif entry.get("id") in by_id:
            by_id[entry["id"]].update(entry)
    for m in by_id.values():
        try:
            next_msg[0] = max(next_msg[0], int(m["id"][1:]) + 1)
        except ValueError:
            pass
    messages.extend(list(by_id.values())[-500:])
    for m in messages:
        if m.get("status") != "queued":
            continue
        if m.get("v") == 2 and time.time() - m["ts"] < 3600:
            inboxes.setdefault(m["to"], []).append(m)
        else:
            m["status"] = "expired"


def expire(box):
    for m in box:
        m["status"] = "expired"
        append_log({"id": m["id"], "status": "expired"})


def send(body):
    sender = body.get("from", "")
    text = (body.get("text") or "").strip()
    to = body.get("to", "")
    if not text or not (to or body.get("reply_to")):
        return 400, {"error": "text, and to or reply_to, are required"}
    # A long message lands whole in the other agent's context; on a shared filesystem a path costs almost nothing.
    limit = int(config.get("max_message_chars") or 0)
    if limit and len(text) > limit and body.get("from") in sessions:
        return 413, {"error": f"message is {len(text)} characters, over the {limit} limit: write the details to a file and send its path with a short summary"}
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

        # Agents only reach their own space, and a lead its parent too. A remote daemon says which spaces its sender reaches.
        if sender_rec:
            spaces = reach(sender_rec)
        else:
            spaces = {clean_space(s) for s in body.get("spaces") or [body.get("space")]} - {""} or None
        target, err = None, None
        # A reply goes to whoever sent the message it answers, whatever name the model wrote: with two
        # similar names in view it may guess the wrong one, and reply_to already says who.
        mine = {sender, *([sender_rec.get("cwd")] if sender_rec and sender_rec.get("cwd") else [])}
        if parent and (parent.get("to") in mine or parent.get("to_cwd") in mine) and "@" not in str(parent.get("from", "")):
            back = sessions.get(parent["from"])
            if not (back and is_live(back)):
                # The sender's conversation moved on (a /clear, a resume, a new session): its folder's live agent takes it.
                back = successor(parent.get("from_cwd") or (back or {}).get("cwd", ""))
            if back:
                target = back
            else:
                err = f"{parent.get('from_name') or 'the sender'} of {parent['id']} is no longer live and no agent runs in its folder now"
        elif not to:
            err = "to is required unless reply_to names a message sent to you"
        if target is None and err is None:
            target, err = resolve(to, spaces)
        if err:
            return 409, {"error": err}

    if target is None:
        if "@" in to or config.get("remotes"):
            return forward(body, hops, spaces)
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
            "from_cwd": sender_rec.get("cwd", "") if sender_rec else "",
            "to_cwd": target.get("cwd", ""),
            "from_space": space_of(sender_rec)[0] if sender_rec else (clean_space(body.get("space")) or ""),
            "to_space": space_of(target)[0],
            "text": text[:20000],
            "expects_reply": bool(body.get("expects_reply")),
            "reply_to": body.get("reply_to") or None,
            "hops": hops,
            "status": "queued",
            "v": 2,  # logs older than this never recorded delivery, so they are not replayed
        }
        next_msg[0] += 1
        inboxes.setdefault(target["id"], []).append(msg)
        if sender in sent_times:
            sent_times[sender].append(now)
        record(msg)
    log("msg", msg["id"], msg["from_name"], "->", msg["to_name"])
    return 200, {"id": msg["id"], "to": msg["to_name"], "status": "queued"}


def report_issue(body):
    """Log a problem with peerlink itself, and pass it to the agent that maintains peerlink when one is live.

    The maintainer is reached whatever its space: a report about broken routing must not depend on routing."""
    text = (body.get("text") or "").strip()
    if not text:
        return 400, {"error": "text is required"}
    with lock:
        sender = sessions.get(body.get("from", ""))
        try:
            with open(ISSUES_PATH) as f:
                n = sum(1 for _ in f) + 1
        except OSError:
            n = 1
        issue = {
            "id": f"i{n}",
            "ts": body.get("ts") or time.time(),
            "from": body.get("from", ""),
            "from_name": body.get("from_name") or (display_name(sender) if sender else ""),
            "space": space_of(sender)[0] if sender else body.get("space", ""),
            "daemon": VERSION,
            "plugin": body.get("plugin", ""),
            "text": text[:20000],
            "context": str(body.get("context") or "")[:5000],
        }
        try:
            with open(ISSUES_PATH, "a") as f:
                f.write(json.dumps(issue) + "\n")
        except OSError as err:
            return 500, {"error": f"could not write {ISSUES_PATH}: {err}"}
        want = clean_space(config.get("issue_agent")) if config.get("issue_agent") else ""
        agent = next((r for r in sessions.values() if want and is_live(r) and r["id"] != issue["from"] and display_name(r).lower() == want), None)
        if agent:
            msg = {
                "id": f"m{next_msg[0]}",
                "ts": time.time(),
                "from": issue["from"],
                "from_name": issue["from_name"] or "peerlink",
                "from_host": HOSTNAME,
                "to": agent["id"],
                "to_name": display_name(agent),
                "text": f"peerlink issue {issue['id']} from {issue['from_name'] or 'an agent'} (space {issue['space'] or '?'}, plugin {issue['plugin'] or '?'}, daemon {VERSION}):\n{text}"
                + (f"\n\ncontext: {issue['context']}" if issue["context"] else "")
                + f"\n\nLogged in {ISSUES_PATH}.",
                "expects_reply": False,
                "reply_to": None,
                "hops": 0,
                "status": "queued",
                "v": 2,
            }
            next_msg[0] += 1
            inboxes.setdefault(agent["id"], []).append(msg)
            record(msg)
    log("issue", issue["id"], issue["from_name"], "->", display_name(agent) if agent else "log only")
    return 200, {"id": issue["id"], "logged": ISSUES_PATH, "sent_to": display_name(agent) if agent else None}


def file_pending_issues():
    try:
        names = sorted(os.listdir(PENDING_ISSUES_DIR))
    except OSError:
        return
    for name in names:
        path = os.path.join(PENDING_ISSUES_DIR, name)
        try:
            with open(path) as f:
                body = json.load(f)
            report_issue({**body, "context": f"(written while the daemon was down) {body.get('context') or ''}".strip()})
            os.remove(path)
        except (OSError, ValueError) as err:
            log("pending issue", name, "unreadable:", err)


def forward(body, hops, spaces):
    to = body["to"]
    name, _, host = to.partition("@")
    for p in remote_peers():
        if (host and p["host"].lower() != host.lower()):
            continue
        shared = peer_reach(p) & spaces if spaces is not None else None
        if spaces is not None and not shared:
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
                "space": min(shared) if shared else None,  # what daemons before 0.6 read
                "spaces": sorted(spaces) if spaces is not None else None,
            }
            try:
                res = remote_call(p["remote"], "/api/messages", fwd, timeout=5)
                return 200, {**res, "to": f"{p['name']}@{p['host']}"}
            except Exception as err:
                return 502, {"error": f"remote {p['remote']} refused: {err}"}
    return 404, {"error": f"no live peer '{to}'; call list_peers"}


def successor(cwd):
    """The live agent in a folder, the most recently seen one first: who takes over a gone session's messages."""
    live = [r for r in sessions.values() if cwd and r.get("cwd") == cwd and is_live(r)]
    return max(live, key=lambda r: r["last_seen"]) if live else None


def adopt(rec, old=None):
    """Give rec the messages still queued for sessions that are gone: `old` (one it replaces), or any in its folder."""
    moved = 0
    for sid in list(inboxes):
        if sid == rec["id"]:
            continue
        other = sessions.get(sid)
        if sid != old and other and is_live(other):
            continue
        box = [m for m in inboxes[sid] if sid == old or (m.get("to_cwd") and m.get("to_cwd") == rec.get("cwd"))]
        if not box:
            continue
        inboxes[sid] = [m for m in inboxes[sid] if m not in box]
        if not inboxes[sid]:
            inboxes.pop(sid)
        for m in box:
            m["to"], m["to_name"] = rec["id"], display_name(rec)
            append_log({"id": m["id"], "to": rec["id"], "to_name": display_name(rec)})
        inboxes.setdefault(rec["id"], []).extend(box)
        moved += len(box)
    return moved


def open_threads(rec, max_age=8 * 3600, limit=8):
    """Requests this agent's folder still owes an answer to, and ones it still waits on, from the message log."""
    me = {rec["id"], rec.get("cwd") or rec["id"]}
    now = time.time()
    answered = {m["reply_to"] for m in messages if m.get("reply_to")}
    def gist(m, who):
        return {"id": m["id"], "with": m.get(who), "ago_s": round(now - m["ts"]), "text": m["text"][:160]}
    recent = [m for m in messages if m.get("expects_reply") and now - m["ts"] < max_age and m["id"] not in answered]
    owes = [gist(m, "from_name") for m in recent if (m.get("to") in me or m.get("to_cwd") in me) and m.get("status") != "expired"]
    waiting = [gist(m, "to_name") for m in recent if m.get("from") in me or m.get("from_cwd") in me]
    return {"owes": owes[-limit:], "waiting": waiting[-limit:]}


def take_inbox(sid):
    with lock:
        box = inboxes.pop(sid, [])
        for m in box:
            m["status"] = "delivered"
            m["delivered_ts"] = time.time()
            append_log({"id": m["id"], "status": "delivered", "delivered_ts": m["delivered_ts"]})
        return box


def reaper():
    while True:
        time.sleep(10)
        now = time.time()
        with lock:
            for sid in [s for s, r in sessions.items() if now - r["last_seen"] > config["peer_ttl_s"] * 10]:
                sessions.pop(sid, None)
                expire(inboxes.pop(sid, []))
            # Messages to an agent that has gone for an hour are not coming back.
            for sid in [s for s in inboxes if s not in sessions]:
                kept = [m for m in inboxes[sid] if now - m["ts"] < 3600]
                expire([m for m in inboxes[sid] if now - m["ts"] >= 3600])
                if kept:
                    inboxes[sid] = kept
                else:
                    inboxes.pop(sid)


class Handler(BaseHTTPRequestHandler):
    server_version = "peerlink/" + VERSION

    def log_message(self, *args):
        pass

    def reply(self, code, body, ctype="application/json", headers=None):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def page(self, code, title, text):
        html = PAGE_HTML.replace("{title}", title).replace("{text}", text)
        return self.reply(code, html, "text/html; charset=utf-8")

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def host_ok(self):
        # A page on another site that rebinds its DNS to 127.0.0.1 still sends its own Host.
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}")

    def web_session(self):
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                with lock:
                    exp = web_sessions.get(value)
                    if exp and exp > time.time():
                        return value
        return None

    def key_ok(self):
        given = self.headers.get("X-Peerlink-Key", "")
        return bool(given) and hmac.compare_digest(given, key[0])

    def remote_ok(self):
        token = config.get("token", "")
        return bool(token) and hmac.compare_digest(self.headers.get("X-Peerlink-Token", ""), token)

    def same_origin(self):
        origin = self.headers.get("Origin")
        return origin is None or origin == f"http://{self.headers.get('Host', '')}"

    def route(self, method):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        q = parse_qs(url.query)
        rest = parts[1:] if parts[:1] == ["api"] else None

        if not self.is_local():
            # Another machine's daemon: only peer discovery and message delivery.
            if self.remote_ok() and (rest == ["peers"] and method == "GET" or rest == ["messages"] and method == "POST"):
                pass
            else:
                return self.reply(403, {"error": "forbidden"})
        elif not self.host_ok():
            return self.reply(403, {"error": "bad host"})

        if rest == ["health"]:
            return self.reply(200, {"ok": True, "version": VERSION, "code": CODE, "pid": os.getpid(), "host": HOSTNAME})

        if method == "GET" and url.path == "/login":
            cookie = redeem(q.get("t", [""])[0])
            if not cookie:
                return self.page(403, "Link expired", "That link has expired or was already used. Run <code>/peerlink web</code> in Claude Code, or <code>peerlink web</code> in a shell, for a new one.")
            attrs = f"{COOKIE}={cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age={WEB_SESSION_TTL_S}"
            return self.reply(302, "", "text/plain", {"Set-Cookie": attrs, "Location": "/"})

        browser = self.is_local() and self.web_session()
        if self.is_local() and not (self.key_ok() or browser):
            if method == "GET" and url.path in ("/", "/index.html"):
                return self.page(401, "Sign in", "Open the web UI with <code>/peerlink web</code> in Claude Code, or <code>peerlink web</code> in a shell. Each link works once.")
            return self.reply(401, {"error": "missing or wrong key"})
        if browser and method != "GET" and not self.same_origin():
            return self.reply(403, {"error": "cross-origin request refused"})

        if method == "GET" and url.path in ("/", "/index.html"):
            return self.reply(200, UI_HTML, "text/html; charset=utf-8")
        if rest is None:
            return self.reply(404, {"error": "not found"})

        if rest == ["web", "link"] and method == "POST" and self.key_ok():
            return self.reply(200, {"url": mint_link(key[0]), "expires_in_s": LINK_TTL_S})

        if rest == ["web", "logout"] and method == "POST":
            with lock:
                if self.body().get("all"):
                    web_sessions.clear()
                elif browser:
                    web_sessions.pop(browser, None)
                save_web_sessions()
            return self.reply(200, {"ok": True})

        if rest == ["key", "rotate"] and method == "POST":
            rotate_key()
            log("key rotated")
            return self.reply(200, {"ok": True})

        if rest == ["status"] and method == "GET":
            with lock:
                live = [r for r in sessions.values() if is_live(r)]
                queued = sum(len(b) for b in inboxes.values())
                browsers = sum(1 for e in web_sessions.values() if e > time.time())
            return self.reply(200, {
                "version": VERSION, "pid": os.getpid(), "host": HOSTNAME, "port": PORT,
                "sessions": len(live), "queued": queued, "messages": len(messages),
                "browsers": browsers, "home": HOME, "launcher": LAUNCHER_PATH,
            })

        if rest == ["config"]:
            if method == "GET":
                return self.reply(200, {**config, "default_guidance": DEFAULT_GUIDANCE})
            if method in ("PUT", "POST"):
                with lock:
                    for k, v in self.body().items():
                        if k in DEFAULT_CONFIG:
                            config[k] = v
                    save_config()
                forget_spaces()
                return self.reply(200, config)

        if rest == ["peers"] and method == "GET":
            peers = local_peers()
            if "local" not in q:
                peers += remote_peers()
            # An agent asking (exclude=its id) only sees the spaces it reaches.
            exclude = q.get("exclude", [""])[0]
            with lock:
                me = sessions.get(exclude)
                space = space_of(me)[0] if me else None
                mine = reach(me) if me else None
            peers = [p for p in peers if p["id"] != exclude and (mine is None or peer_reach(p) & mine)]
            return self.reply(200, {"host": HOSTNAME, "space": space, "peers": peers})

        if rest == ["sessions"] and method == "POST":
            b = self.body()
            sid = b.get("id")
            if not sid:
                return self.reply(400, {"error": "id required"})
            with lock:
                rec = sessions.get(sid, {"id": sid, "started": time.time()})
                if b.get("name") and not rec.get("name"):
                    # A folder keeps the name it was given: a new session there (a restart, a resume) is still "vault".
                    given = (config.get("folder_names") or {}).get(b.get("cwd") or "")
                    rec["name"] = unique_name(given or b["name"], sid)
                for k in ("cwd", "repo", "branch", "summary", "model", "plugin"):
                    if b.get(k) is not None:
                        rec[k] = b[k]
                rec["busy"] = bool(b.get("busy", False))
                rec["last_seen"] = time.time()
                sessions[sid] = rec
                old = b.get("succeeds")
                if old and old != sid:
                    # The same conversation after a /clear or resume: keep its name, settings and messages.
                    prev = sessions.pop(old, None)
                    if old in config["sessions"] and sid not in config["sessions"]:
                        config["sessions"][sid] = dict(config["sessions"][old])
                        save_config()
                    if prev and prev.get("name"):
                        rec["name"] = prev["name"]
                adopt(rec, old)
                previous = dir_summaries.get(rec.get("cwd", ""))
                if previous and previous["ts"] < time.time() - DIR_MEMORY_S:
                    previous = None
            log("agent", sid, rec.get("name"))
            return self.reply(200, {"id": sid, "name": display_name(rec), "config": client_config(sid), "previous": previous})

        if len(rest) == 3 and rest[0] == "sessions" and rest[2] == "heartbeat" and method == "POST":
            sid = rest[1]
            b = self.body()
            with lock:
                rec = sessions.get(sid)
                if rec is None:
                    return self.reply(404, {"error": "unknown agent; register again"})
                rec["last_seen"] = time.time()
                if "busy" in b:
                    rec["busy"] = bool(b["busy"])
                if isinstance(b.get("context"), (int, float)):
                    rec["context"] = max(0, min(100, int(b["context"])))
                if b.get("summary") is not None:
                    rec["summary"] = b["summary"][:500]
                    if b.get("summary_kind") in ("model", "manual"):
                        remember_dir(rec)
                cfg = client_config(sid)
            inbox = [] if cfg["muted"] else take_inbox(sid)
            with lock:
                mine = reach(rec)
            others = sum(1 for p in local_peers() if p["id"] != sid and peer_reach(p) & mine) + len(config.get("remotes", []))
            return self.reply(200, {"name": display_name(rec), "config": cfg, "inbox": inbox, "peer_count": others})

        if len(rest) == 3 and rest[0] == "sessions" and rest[2] == "config" and method == "POST":
            # One agent's settings, merged here: a page or plugin holding an old copy of every agent's
            # settings must not overwrite changes made since.
            sid = rest[1]
            b = self.body()
            with lock:
                cur = config["sessions"].setdefault(sid, {})
                for k in ("nickname", "muted", "auto_wake", "space"):
                    if k in b:
                        cur[k] = b[k]
                rec = sessions.get(sid)
                if rec and rec.get("cwd") and "nickname" in b:
                    names = config.setdefault("folder_names", {})
                    if b["nickname"]:
                        names[rec["cwd"]] = b["nickname"]
                    else:
                        names.pop(rec["cwd"], None)
                save_config()
            forget_spaces()
            with lock:
                return self.reply(200, {"config": client_config(sid), "sessions": config["sessions"]})

        if len(rest) == 3 and rest[0] == "sessions" and rest[2] == "space-file" and method == "POST":
            with lock:
                rec = sessions.get(rest[1])
            if rec is None:
                return self.reply(404, {"error": "unknown agent"})
            b = self.body()
            field = "parent" if "parent" in b else "space"
            try:
                path = write_space_file(rec, clean_space(b.get(field)), field)
            except (OSError, ValueError) as err:
                return self.reply(400, {"error": str(err)})
            log("space file", path)
            with lock:
                return self.reply(200, {"path": path, "config": client_config(rec["id"])})

        if len(rest) == 2 and rest[0] == "sessions" and method == "DELETE":
            with lock:
                sessions.pop(rest[1], None)  # its queue waits for the next agent in its folder (an hour, as on restart)
            return self.reply(200, {"ok": True})

        if len(rest) == 3 and rest[0] == "sessions" and rest[2] == "threads" and method == "GET":
            with lock:
                rec = sessions.get(rest[1])
                if rec is None:
                    return self.reply(404, {"error": "unknown agent"})
                return self.reply(200, open_threads(rec))

        if rest == ["messages"]:
            if method == "GET":
                with lock:
                    return self.reply(200, {"messages": messages[-int(q.get("limit", ["100"])[0]):]})
            if method == "POST":
                code, res = send(self.body())
                return self.reply(code, res)

        if rest == ["issues"]:
            if method == "GET":
                try:
                    with open(ISSUES_PATH) as f:
                        lines = f.readlines()[-int(q.get("limit", ["50"])[0]):]
                except OSError:
                    lines = []
                return self.reply(200, {"issues": [json.loads(l) for l in lines if l.strip()], "path": ISSUES_PATH})
            if method == "POST":
                code, res = report_issue(self.body())
                return self.reply(code, res)

        if rest == ["restart"] and method == "POST":
            self.reply(200, {"ok": True})
            threading.Thread(target=restart_self, args=(self.server,), daemon=True).start()
            return

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


def local_call(method, path, body=None, timeout=3):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Content-Type": "application/json", "X-Peerlink-Key": read_key()},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def health():
    try:
        return local_call("GET", "/api/health", timeout=1)
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


def restart_self(server):
    """Hand the port to a fresh copy of this file (it may be a newer version)."""
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "start", "--after-restart"], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.2)
    server.shutdown()


def cli_start(args):
    if "--after-restart" in args:
        for _ in range(50):
            if not health():
                break
            time.sleep(0.1)
    detach()


def serve():
    load_config()
    load_key()
    load_web_sessions()
    load_messages()
    load_dirs()
    file_pending_issues()
    try:
        write_launcher()
    except OSError as err:
        log("could not write launcher:", err)
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


def ensure_running():
    if not health():
        detach_quiet()
    if not health():
        sys.exit("peerlink: the daemon did not start; see " + LOG_PATH)


def detach_quiet():
    out = sys.stdout
    sys.stdout = open(os.devnull, "w")
    try:
        detach()
    finally:
        sys.stdout = out


def open_url(url):
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.run([opener, url], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def cli_web(args):
    ensure_running()
    url = local_call("POST", "/api/web/link", {})["url"]
    if "--print" not in args:
        open_url(url)
    print(url)
    print(f"One-time link, valid {LINK_TTL_S}s.")


def cli_status(args):
    h = health()
    if not h:
        print("peerlink: not running")
        return 1
    st = local_call("GET", "/api/status")
    print(f"peerlink {st['version']} on {st['host']}:{st['port']} (pid {st['pid']})")
    print(f"  {st['sessions']} live agents, {st['queued']} queued messages, {st['browsers']} signed-in browsers")
    print(f"  data: {st['home']}")
    return 0


def cli_issues(args):
    ensure_running()
    res = local_call("GET", "/api/issues")
    if not res["issues"]:
        print(f"No issues reported. ({res['path']})")
    for i in res["issues"][-20:]:
        print(f"{i['id']:<5} {time.strftime('%m-%d %H:%M', time.localtime(i['ts']))}  {i['from_name'] or '?'} ({i['space'] or '?'})  {i['text'][:100]}")
    return 0


def cli_peers(args):
    ensure_running()
    peers = local_call("GET", "/api/peers")["peers"]
    if not peers:
        print("No live agents.")
    for p in peers:
        state = "busy" if p["busy"] else "idle"
        flags = ", muted" if p.get("muted") else ""
        stale = f"  plugin {p.get('plugin') or 'old'}: run /reload-plugins" if p.get("stale") and not p.get("remote") else ""
        print(f"{p['name']:<20} {state}{flags:<8} {p.get('space', DEFAULT_SPACE):<12} {p['cwd']}{stale}")
        if p.get("summary"):
            print(f"{'':<20} {p['summary']}")


def cli_messages(args):
    ensure_running()
    for m in local_call("GET", "/api/messages?limit=20")["messages"]:
        when = time.strftime("%H:%M:%S", time.localtime(m["ts"]))
        print(f"{when} {m['id']:<5} {m['from_name']} -> {m['to_name']} [{m['status']}]: {m['text'][:100]}")


def cli_session_flag(args, field, value):
    if not args:
        sys.exit(f"usage: peerlink {'mute' if value else 'unmute'} <agent name>")
    ensure_running()
    want = args[0].lower()
    peers = local_call("GET", "/api/peers?local=1")["peers"]
    hits = [p for p in peers if want in (p["name"].lower(), p["id"].lower())]
    if len(hits) != 1:
        sys.exit(f"peerlink: no single live agent named '{args[0]}'")
    cfg = local_call("GET", "/api/config")
    sessions_cfg = dict(cfg.get("sessions", {}))
    sessions_cfg[hits[0]["id"]] = {**sessions_cfg.get(hits[0]["id"], {}), field: value}
    local_call("PUT", "/api/config", {"sessions": sessions_cfg})
    print(f"{hits[0]['name']}: {field} = {value}")


def cli_config(args):
    ensure_running()
    cfg = local_call("GET", "/api/config")
    cfg.pop("default_guidance", None)
    if not args:
        print(json.dumps({k: v for k, v in cfg.items() if k not in ("sessions",)}, indent=2))
        return
    name = args[0]
    if name not in DEFAULT_CONFIG:
        sys.exit(f"peerlink: unknown setting '{name}'")
    if len(args) == 1:
        print(json.dumps(cfg.get(name), indent=2))
        return
    raw = " ".join(args[1:])
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    print(json.dumps(local_call("PUT", "/api/config", {name: value}).get(name)))


def cli_stop(args):
    try:
        local_call("POST", "/api/shutdown", {})
        print("stopped (agents start it again when they next need it)")
    except Exception:
        print("not running")


def cli_restart(args):
    before = health()
    if before:
        local_call("POST", "/api/restart", {})
        for _ in range(60):
            time.sleep(0.1)
            now = health()
            if now and now.get("pid") != before.get("pid"):
                break
    else:
        detach_quiet()
    print("running" if health() else "peerlink: the daemon did not start; see " + LOG_PATH)


def cli_rotate(args):
    ensure_running()
    local_call("POST", "/api/key/rotate", {})
    print("New key written. Browsers are signed out; agents pick up the new key on their next call.")


def cli_logout(args):
    ensure_running()
    local_call("POST", "/api/web/logout", {"all": True})
    print("All browsers signed out.")


def cli_logs(args):
    try:
        with open(LOG_PATH) as f:
            print("".join(f.readlines()[-40:]), end="")
    except OSError:
        print("no log yet")


COMMANDS = {
    "web": (cli_web, "open the web UI with a one-time sign-in link (--print to only print it)"),
    "status": (cli_status, "daemon version, agents, queued messages"),
    "issues": (cli_issues, "the last 20 problems agents reported with peerlink"),
    "peers": (cli_peers, "list live agents"),
    "messages": (cli_messages, "the last 20 messages"),
    "mute": (lambda a: cli_session_flag(a, "muted", True), "mute an agent by name"),
    "unmute": (lambda a: cli_session_flag(a, "muted", False), "unmute an agent"),
    "config": (cli_config, "show settings, or `config <name> [value]` to read or set one"),
    "start": (cli_start, "start in the background"),
    "stop": (cli_stop, "stop the daemon"),
    "restart": (cli_restart, "restart the daemon"),
    "rotate-key": (cli_rotate, "new key: signs out every browser, invalidates links"),
    "logout": (cli_logout, "sign out every browser"),
    "logs": (cli_logs, "the end of the daemon log"),
    "serve": (lambda a: serve(), "run in the foreground"),
    "fingerprint": (lambda a: print(CODE), "the fingerprint of this daemon's code"),
}
ALIASES = {"--detach": "start", "--stop": "stop", "--status": "status"}


def cli_help():
    print("usage: peerlink <command>\n")
    for name, (_, text) in COMMANDS.items():
        print(f"  {name:<11} {text}")
    print(f"\nPEERLINK_HOME={HOME}  PEERLINK_PORT={PORT}")


UI_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Peerlink</title><link rel="icon" href="data:,">
<style>
:root{color-scheme:light dark;--bg:#faf9f7;--side:#f2f0ec;--fg:#1f1e1c;--mut:#6b6862;--line:#e4e1db;--card:#fff;--acc:#c96442;--ok:#2f7d4f;--warn:#b7791f}
@media (prefers-color-scheme:dark){:root{--bg:#1c1b19;--side:#161513;--fg:#ecebe8;--mut:#9b978f;--line:#34322e;--card:#252421;--acc:#e0805e;--ok:#5cb57f;--warn:#e0b04a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 ui-sans-serif,system-ui,-apple-system,sans-serif}
.app{display:flex;min-height:100vh}
nav{flex:0 0 210px;background:var(--side);border-right:1px solid var(--line);padding:18px 10px;position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:2px}
nav .brand{font-weight:700;font-size:16px;padding:0 10px 4px}nav .host{font-size:11px;color:var(--mut);padding:0 10px 14px}
nav a{display:flex;justify-content:space-between;align-items:center;padding:7px 10px;border-radius:6px;color:var(--fg);text-decoration:none}
nav a:hover{background:var(--line)}nav a.on{background:var(--card);box-shadow:0 0 0 1px var(--line);font-weight:600}
nav .count{font-size:11px;color:var(--mut);font-weight:400}
main{flex:1;min-width:0;max-width:1100px;padding:22px 24px 60px}
section{display:none}section.on{display:block}
h1{font-size:20px;margin:0 0 4px}.lead{color:var(--mut);margin:0 0 18px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:22px 0 10px}
.sub{margin-left:12px;padding-left:16px;border-left:2px solid var(--line)}.sub h2::before{content:"↳ ";color:var(--acc)}
.card.is-lead{border-color:var(--acc)}
.chart{overflow-x:auto;padding:4px 0 12px;margin-bottom:8px}
.chart+.chart{margin-top:6px}
.unit{display:inline-flex;flex-direction:column;align-items:center;border:1px dashed var(--line);border-radius:12px;padding:10px 12px 12px;min-width:0}
.unit-h{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin-bottom:10px;white-space:nowrap}
.orow{display:flex;gap:12px;align-items:flex-start;justify-content:safe center}
.ocol{display:flex;flex-direction:column;align-items:center}.orow.stack{flex-direction:column;gap:8px}
.down{width:2px;height:14px;background:var(--acc);flex:none}
.node{width:190px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px;cursor:pointer;text-align:left;font:inherit;color:inherit}
.node:hover{border-color:var(--mut)}.node.is-lead{border-color:var(--acc)}.node.sel{box-shadow:0 0 0 2px var(--acc)}
.node .nm{font-weight:600;display:flex;align-items:center;gap:4px;justify-content:space-between}
.node .tag{font-size:11px;font-weight:500;color:var(--acc);white-space:nowrap}
.node .dir{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--mut);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ctx{font-size:11px;color:var(--mut);font-weight:500}.ctx.hi{color:var(--warn)}
.node .stale{font-size:11px;color:var(--warn);margin-top:4px}
.node .sum{font-size:12px;color:var(--mut);margin-top:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sbox{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px;min-width:160px;max-width:260px;text-align:center}
.sbox.led{border-color:var(--acc)}.sbox .small{margin-top:2px}
#detail:empty{display:none}
#detail{position:fixed;top:0;right:0;bottom:0;width:380px;max-width:100%;overflow-y:auto;background:var(--bg);border-left:1px solid var(--line);box-shadow:-8px 0 24px rgba(0,0,0,.08);padding:16px;z-index:10}
#detail .panel{border:0;padding:0;background:transparent}
.panel .top{display:flex;justify-content:space-between;align-items:center;gap:8px}.panel .name{font-size:16px}
.panel .x{padding:2px 8px;font-size:13px}.facts{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;margin:10px 0 4px;font-size:13px}
.facts .k{color:var(--mut)}.threads{margin-top:8px;display:flex;flex-direction:column;gap:4px}.threads .k{color:var(--mut)}.panel form{margin-top:12px;border-top:1px solid var(--line);padding-top:12px;display:flex;flex-direction:column;gap:12px}
.panel label{display:flex;flex-direction:column;gap:4px;font-size:13px;font-weight:500}.panel label .hint{font-weight:400;font-size:12px;color:var(--mut)}
.panel label.check{flex-direction:row;align-items:center;gap:8px}.ok{color:var(--ok)}
#stalebar .mono{white-space:nowrap}#stalebar{background:var(--card);border:1px solid var(--warn);border-radius:10px;padding:10px 14px;margin-bottom:14px}
#offline{display:none;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:14px;color:var(--mut)}
#signedout{display:none;background:var(--card);border:1px solid var(--warn);border-radius:10px;padding:10px 14px;margin-bottom:14px}
.msgbar{display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap}.msgbar select{width:auto}
.mlist{display:flex;flex-direction:column;gap:8px}
.msg{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;cursor:pointer}
.msg .mh{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;font-size:13px}.msg .mh .when{margin-left:auto;color:var(--mut);font-size:12px}
.msg .mt{margin-top:6px;white-space:pre-wrap;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.msg.open .mt{display:block}.msg .chip{font-size:11px;border-radius:999px;padding:1px 8px;border:1px solid var(--line);color:var(--mut)}
.chip.queued{border-color:var(--warn);color:var(--warn)}.chip.expired{border-color:#c0392b;color:#c0392b}.chip.reply{color:var(--acc);border-color:var(--acc)}
.msg.inthread{margin-left:22px;border-left:3px solid var(--acc)}
@media (min-width:721px){body.drawer main{padding-right:404px;max-width:1504px}}
@media (max-width:720px){#detail{width:100%;border-left:0}}
.branch{display:flex;align-items:flex-start;justify-content:safe center}
.branch>.ocol{position:relative;padding:14px 6px 0}
.branch>.ocol::before{content:"";position:absolute;top:0;left:0;right:0;border-top:2px solid var(--acc)}
.branch>.ocol:first-child::before{left:50%}.branch>.ocol:last-child::before{right:50%}.branch>.ocol:only-child::before{display:none}
.branch>.ocol::after{content:"";position:absolute;top:0;left:calc(50% - 1px);width:2px;height:14px;background:var(--acc)}
@media (max-width:720px){
 .chart{overflow:visible}.unit{display:flex;width:100%;align-items:stretch}.unit-h{text-align:left}
 .orow{flex-direction:column;align-items:stretch;gap:8px}.ocol{align-items:stretch}.node{width:auto}
 .ocol>.down{display:none}.ocol>.unit{margin:8px 0 0 14px;width:auto;border-left:2px solid var(--acc);border-style:none none none solid;border-radius:0;padding:4px 0 4px 12px}
 .branch{flex-direction:column;align-items:stretch;margin-left:14px;border-left:2px solid var(--acc);padding-left:12px}
 .branch>.ocol{padding:8px 0 0}.branch>.ocol::before,.branch>.ocol::after{display:none}.sbox{max-width:none;text-align:left}
}
.mut{color:var(--mut)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;overflow-wrap:anywhere;min-width:0}
.peer .top{display:flex;justify-content:space-between;gap:8px;align-items:center}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--ok)}.dot.busy{background:var(--warn)}
.name{font-weight:600}.small{font-size:12px}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;word-break:break-all}
.pill{display:inline-block;font-size:11px;border:1px solid var(--line);border-radius:999px;padding:1px 8px;color:var(--mut)}
input[type=text],input[type=number],textarea{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit}
select{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px;font:inherit}
textarea{min-height:180px;font-family:ui-monospace,Menlo,monospace;font-size:12px}
label{display:block;margin:8px 0 4px;font-size:12px;color:var(--mut)}
.row{display:flex;gap:12px;flex-wrap:wrap}.row>div{flex:1 1 180px}
button{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:7px 14px;font:inherit;cursor:pointer}
button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line)}
details{margin-top:8px}summary{cursor:pointer;font-size:12px;color:var(--mut)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--mut);font-weight:500}.msgtext{white-space:pre-wrap;max-height:6em;overflow:auto}
.tablewrap{overflow-x:auto}.empty{color:var(--mut);padding:14px 0}
.check{display:flex;align-items:center;gap:6px;margin-top:8px}
.err{color:#c0392b}
@media (max-width:720px){.app{flex-direction:column}
 nav{position:static;height:auto;flex:none;flex-direction:row;flex-wrap:wrap;border-right:0;border-bottom:1px solid var(--line);padding:10px 12px}
 nav .brand{padding:6px 8px 6px 4px}nav .host{display:none}nav a{padding:6px 10px}nav .count{margin-left:6px}
 main{padding:16px}}
</style></head><body><div class="app">
<nav>
<div class="brand">Peerlink</div><div class="host" id="hostline"></div>
<a href="#agents" data-nav="agents">Agents <span class="count" id="n-agents"></span></a>
<a href="#spaces" data-nav="spaces">Spaces <span class="count" id="n-spaces"></span></a>
<a href="#messages" data-nav="messages">Messages <span class="count" id="n-msgs"></span></a>
<a href="#settings" data-nav="settings">Settings</a>
<a href="#controls" data-nav="controls">Controls</a>
</nav>
<main><div id="stalebar" style="display:none" class="small"></div><div id="offline">Can't reach the peerlink daemon. It restarts when an agent next needs it; this page reconnects by itself.</div><div id="signedout">This page is signed out. Run <span class="mono">/peerlink web</span> in any agent to open it again.</div>

<section id="agents"><h1>Agents</h1><p class="lead">Live Claude Code sessions as an org chart. Each box is a space; a lead (outlined in orange) sits in its parent's box with its team beneath it. The percentage is how full each agent's context is. An agent only sees and messages agents in its own space, and a lead also its parent's. Click an agent to see and change its settings.</p><div class="orgwrap"><div id="peers"></div><div id="detail"></div></div></section>

<section id="spaces"><h1>Spaces</h1>
<p class="lead">Each agent's space comes from, in order: an override for that agent, the nearest <span class="mono">.peerlink</span> file in its folder or above, a folder rule below, or <b>default</b>. A <span class="mono">.peerlink</span> with <span class="mono">parent: &lt;space&gt;</span> nests its space inside that one: the agent in that file's folder leads it and also sits in the parent.</p>
<div id="spacelist"></div>
<h2>Folder rules</h2>
<form class="card" id="rules">
<label>One per line: <span class="mono">folder = space</span>. A rule covers the folder and everything inside it; the most specific rule wins.</label>
<textarea name="space_rules" style="min-height:120px" placeholder="~/Repos/acme = acme&#10;~/Repos/tools = tools"></textarea>
<p><button type="submit">Save rules</button> <span class="mut small" id="rulesaved"></span></p>
</form>
</section>

<section id="messages"><h1>Messages</h1><p class="lead">The latest messages between agents, newest first. A reply sits under the message it answers. Click a message to read all of it.</p>
<div class="msgbar"><label class="small mut" for="msgspace">Space</label><select id="msgspace"><option value="">All spaces</option></select>
<label class="small mut" for="msgagent">Agent</label><select id="msgagent"><option value="">Everyone</option></select></div>
<div class="mlist" id="msgs"></div></section>

<section id="settings"><h1>Settings</h1><p class="lead">Changes reach every agent within a few seconds.</p>
<form class="card" id="cfg">
<div class="row">
<div><label>Poll interval (ms)</label><input type="number" name="poll_interval_ms" min="500"></div>
<div><label>Agent timeout (s)</label><input type="number" name="peer_ttl_s" min="5"></div>
<div><label>Max reply hops</label><input type="number" name="max_hops" min="1"></div>
<div><label>Messages per minute per agent</label><input type="number" name="rate_limit_per_min" min="1"></div>
</div>
<div class="check"><input type="checkbox" name="auto_wake_idle" id="aw"><label for="aw" style="margin:0">Wake idle agents when a message arrives</label></div>
<div class="check"><input type="checkbox" name="inject_while_busy" id="ib"><label for="ib" style="margin:0">Deliver into a running turn</label></div>
<div class="check"><input type="checkbox" name="roster_in_prompt" id="rp"><label for="rp" style="margin:0">Show each agent the live agent list when it changes</label></div>
<div class="check"><input type="checkbox" name="auto_summary" id="as"><label for="as" style="margin:0">Keep agent summaries current automatically</label></div>
<div class="row"><div><label>Summary model</label><input type="text" name="summary_model"></div><div></div></div>
<label>Guidance given to every agent (when to message peers)</label><textarea name="guidance" id="guidance"></textarea><div class="mut small">Leave empty to use the built-in guidance shown in grey.</div>
<label>Remote daemons (one URL per line, e.g. http://other-mac.local:7899)</label><textarea name="remotes" style="min-height:60px"></textarea>
<div class="row"><div><label>Listen host (restart the daemon to apply; 0.0.0.0 for remotes)</label><input type="text" name="listen_host"></div>
<div><label>Shared token for remote daemons</label><input type="text" name="token"></div></div>
<p><button type="submit">Save</button> <span class="mut small" id="saved"></span></p>
</form></section>

<section id="controls"><h1>Controls</h1><p class="lead">Sign-in, the key, and the daemon.</p>
<div class="card">
<div class="row" style="align-items:center">
<div><b>Browsers</b><div class="mut small" id="browsers"></div></div>
<div style="flex:0 0 auto"><button class="ghost" data-act="logout">Sign out here</button> <button class="ghost" data-act="logout-all">Sign out everywhere</button></div>
</div>
<div class="row" style="align-items:center;margin-top:12px">
<div><b>Key</b><div class="mut small">Agents and sign-in links use the key in <span class="mono" id="keypath"></span>. Rotating it signs out every browser.</div></div>
<div style="flex:0 0 auto"><button class="ghost" data-act="rotate">Rotate key</button></div>
</div>
<div class="row" style="align-items:center;margin-top:12px">
<div><b>Daemon</b><div class="mut small">Agents reconnect by themselves after a restart. Queued messages are kept.</div></div>
<div style="flex:0 0 auto"><button class="ghost" data-act="restart">Restart daemon</button></div>
</div>
<p class="mut small" id="ctlmsg"></p>
</div></section>
</main></div>
<script>
const $=s=>document.querySelector(s);let cfg={},editing=false,sel='';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const J={'Content-Type':'application/json'};
const tilde=p=>String(p||'').replace(/^\/Users\/[^/]+/,'~');
// Signed out (the cookie expired, or every browser was signed out): say so and stop polling, rather than reload.
let signedOut=false;
async function api(p,o){const r=await fetch(p,o);if(r.status===401){signedOut=true;$('#signedout').style.display='block';throw new Error('signed out')}return r.json()}
const post=(p,b)=>api(p,{method:'POST',headers:J,body:JSON.stringify(b||{})});
function show(){const id=(location.hash||'#agents').slice(1);
  document.querySelectorAll('section').forEach(s=>s.classList.toggle('on',s.id===id));
  document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('on',a.dataset.nav===id))}
// Leaving Agents, or Escape, closes the agent panel: it should never linger over another page.
const closePanel=()=>{if(!sel)return;sel='';editing=false;refresh(true)};
window.addEventListener('hashchange',()=>{show();closePanel()});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closePanel()});
function agentPanel(x,others=[]){const sc=cfg.sessions?.[x.id]||{};const fileSpace=x.space_source==='file'?x.space:'';
  const wake=sc.auto_wake===true?'on':sc.auto_wake===false?'off':'default';
  const why=x.space_source==='override'?'set for this agent only':x.space_source==='file'?'from '+tilde(x.space_from):x.space_source==='rule'?'folder rule '+tilde(x.space_from):'no space set';
  const role=x.parent?`Leads the <b>${esc(x.space)}</b> team inside <b>${esc(x.parent)}</b>`:'Member';
  const opt=(v,t)=>`<option value="${v}" ${wake===v?'selected':''}>${t}</option>`;
  return `<div class="card panel${x.parent?' is-lead':''}">
   <div class="top"><span><span class="dot ${x.busy?'busy':''}"></span><span class="name">${esc(x.name)}</span>${x.remote?` <span class="mut small">@${esc(x.host)}</span>`:''}</span><button class="ghost x" data-close title="Close">✕</button></div>
   <div class="mono mut" style="margin-top:4px">${esc(tilde(x.cwd))}${x.branch?' · '+esc(x.branch):''}</div>
   <div style="margin-top:8px">${esc(x.summary)||'<span class="mut">no summary yet</span>'}</div>
   <div class="facts"><span class="k">Status</span><span>${x.busy?'working':'idle'}${x.muted?' · muted':''}</span>
    <span class="k">Space</span><span><span class="pill">${esc(x.space||'default')}</span> <span class="mut">${esc(why)}</span></span>
    <span class="k">Role</span><span>${role}</span>
    ${x.context!=null?`<span class="k">Context</span><span>${x.context}% of its window in use${x.context>=80?' · <span style="color:var(--warn)">close to compacting</span>':''}</span>`:''}
    <span class="k">Plugin</span><span>${esc(x.plugin||'older than 0.6.6')}${x.stale&&!x.remote?' · <span style="color:var(--warn)">run /reload-plugins in this agent</span>':''}</span></div>
   ${others.length?`<div class="threads"><div class="k small">Other sessions in this folder</div>${others.map(o=>`<button class="ghost small" data-sel="${esc(o.id)}" style="text-align:left">${esc(o.name)} · ${o.busy?'working':'idle'}${o.summary?' · '+esc(o.summary.slice(0,60)):''}</button>`).join('')}</div>`:''}
   ${(x.threads&&(x.threads.owes.length||x.threads.waiting.length))?`<div class="threads"><div class="k small">Open requests</div>
    ${x.threads.owes.map(t=>`<div class="small">owes <b>${esc(t.with)}</b> a reply · <span class="mut">${esc(t.id)}: ${esc(t.text.slice(0,90))}</span></div>`).join('')}
    ${x.threads.waiting.map(t=>`<div class="small">waiting on <b>${esc(t.with)}</b> · <span class="mut">${esc(t.id)}: ${esc(t.text.slice(0,90))}</span></div>`).join('')}</div>`:''}
   ${x.remote?'<div class="small mut">This agent runs on another machine; change it there.</div>':`<form id="agentform" data-id="${esc(x.id)}" data-nick="${esc(sc.nickname||'')}" data-spacefile="${esc(fileSpace)}" data-parent="${esc(x.file_parent||'')}" data-override="${esc(sc.space||'')}" data-wake="${wake}" data-muted="${x.muted?'1':''}">
    <label>Name<input name="nickname" type="text" value="${esc(sc.nickname||'')}" placeholder="${esc(x.name)}"><span class="hint">What peers call this agent. It stays with this folder.</span></label>
    <label>Space for this folder<input name="spacefile" type="text" value="${esc(fileSpace)}" placeholder="none"><span class="hint">Saved in this repo's .peerlink. Every agent in the folder joins it.</span></label>
    <label>Inside space<input name="parent" type="text" value="${esc(x.file_parent||'')}" placeholder="none (top level)"><span class="hint">Where the whole ${esc(x.space||'default')} space sits. Changing it moves every agent in that space; the agent in the .peerlink's folder leads it.</span></label>
    <label>Space for this agent only<input name="override" type="text" value="${esc(sc.space||'')}" placeholder="none"><span class="hint">Overrides the folder's space. An agent with its own space never leads a team.</span></label>
    <label>When a message arrives<select name="wake">${opt('default','Use the default')}${opt('on','Start a turn')}${opt('off','Wait for the next prompt')}</select></label>
    <label class="check"><input name="muted" type="checkbox" ${x.muted?'checked':''}> Mute: hold every message for this agent</label>
    <div><button type="submit">Save</button> <span id="agentmsg" class="small"></span></div></form>`}</div>`}
// Messages as a conversation: newest first, each reply under the message it answers.
let msgFilter='',spaceFilter='',livePeers=[];
// A message's spaces: what it recorded when sent, else (older messages) the space of a live agent on either end.
const spacesOf=x=>{const by=n=>livePeers.find(p=>p.name===n)?.space;return [x.from_space||by(x.from_name),x.to_space||by(x.to_name)].filter(Boolean)};
function renderMessages(list){
  const opts=(el,all,vals)=>{const keep=el.value;el.innerHTML=`<option value="">${all}</option>`+vals.map(n=>`<option ${n===keep?'selected':''}>${esc(n)}</option>`).join('')};
  opts($('#msgspace'),'All spaces',[...new Set(list.flatMap(spacesOf))].sort());
  const inSpace=list.filter(x=>!spaceFilter||spacesOf(x).includes(spaceFilter));
  opts($('#msgagent'),'Everyone',[...new Set(inSpace.flatMap(x=>[x.from_name,x.to_name]))].sort());
  const shown=inSpace.filter(x=>!msgFilter||x.from_name===msgFilter||x.to_name===msgFilter);
  const byId=Object.fromEntries(shown.map(x=>[x.id,x]));const kids={};
  for(const x of shown)if(x.reply_to&&byId[x.reply_to])(kids[x.reply_to]??=[]).push(x);
  const card=(x,inthread)=>{const st=x.status==='delivered'?'':`<span class="chip ${esc(x.status)}">${esc(x.status)}</span>`;
    return `<div class="msg${inthread?' inthread':''}" data-msg="${esc(x.id)}"><div class="mh"><b>${esc(x.from_name)}</b><span class="mut">→</span><b>${esc(x.to_name)}</b>
      ${x.expects_reply?'<span class="chip">reply expected</span>':''}${x.reply_to?`<span class="chip reply">re ${esc(x.reply_to)}</span>`:''}${st}
      <span class="when">${new Date(x.ts*1000).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})} · ${esc(x.id)}</span></div>
      <div class="mt">${esc(x.text)}</div></div>`};
  const thread=(x,depth)=>card(x,depth>0)+(kids[x.id]||[]).sort((a,b)=>a.ts-b.ts).map(k=>thread(k,depth+1)).join('');
  const tops=shown.filter(x=>!(x.reply_to&&byId[x.reply_to])).sort((a,b)=>b.ts-a.ts);
  $('#msgs').innerHTML=tops.map(x=>thread(x,0)).join('')||'<div class="empty">No messages yet.</div>';
  for(const id of openMsgs)document.querySelector(`[data-msg="${CSS.escape(id)}"]`)?.classList.add('open');
}
const openMsgs=new Set();let lastMsgs=[];
document.addEventListener('click',e=>{const m=e.target.closest('.msg');if(!m)return;m.classList.toggle('open');
  m.classList.contains('open')?openMsgs.add(m.dataset.msg):openMsgs.delete(m.dataset.msg)});
document.addEventListener('change',e=>{
  if(e.target.id==='msgagent'){msgFilter=e.target.value;renderMessages(lastMsgs)}
  if(e.target.id==='msgspace'){spaceFilter=e.target.value;msgFilter='';$('#msgagent').value='';renderMessages(lastMsgs)}});
// What a render shows; the page is redrawn only when this changes, so it never flickers or loses your place.
let lastView='',lastPanel='';
async function refresh(force){
  if(signedOut)return;
  const [st,p,m]=await Promise.all([api('/api/status'),api('/api/peers'),api('/api/messages?limit=100')]);
  const view=JSON.stringify([p.peers.map(({last_seen_s,...x})=>x),m.messages.map(x=>[x.id,x.status]),sel,cfg.sessions]);
  if(view===lastView&&!force)return;lastView=view;
  $('#hostline').textContent=`${st.host} · daemon ${st.version}`;
  $('#browsers').textContent=`${st.browsers} signed in · links work once and expire after 60 s`;
  $('#keypath').textContent=st.home+'/key';
  const groups={};for(const x of p.peers)(groups[x.space||'default']??=[]).push(x);
  const names=Object.keys(groups).sort((a,b)=>a==='default'?1:b==='default'?-1:a.localeCompare(b));
  // An org chart. A space is a box of its own agents; the lead of a space inside it sits in that box too, with
  // its team's box hanging beneath it. Charts with teams in them come first, so the top of the org is at the top.
  // Two sessions open in a lead's folder are both leads: all sit in the parent's box, the team under the first.
  const inside={},kids={},leads={};for(const x of p.peers)if(x.parent){(leads[x.space]??=[]).push(x);if(!inside[x.space]){inside[x.space]={parent:x.parent,lead:x.name,peer:x};(kids[x.parent]??=[]).push(x.space)}}
  for(const n in inside)inside[n].lead=leads[n].map(x=>x.name).join(', ');
  for(const n of Object.keys(kids))if(!groups[n])groups[n]=[];
  const size=(n,seen=new Set())=>{if(seen.has(n))return 0;seen.add(n);return (groups[n]||[]).length+(kids[n]||[]).reduce((t,c)=>t+size(c,seen),0)};
  const order=(a,b)=>a==='default'?1:b==='default'?-1:((kids[b]||[]).length>0)-((kids[a]||[]).length>0)||size(b)-size(a)||a.localeCompare(b);
  const roots=Object.keys(groups).filter(n=>!inside[n]||n===inside[n].parent).sort(order);
  // A space's box: its members, then the leads of the spaces under it.
  const isLead=(x,n)=>!!(inside[n]&&inside[n].parent!==n&&x.parent&&x.space===n);
  const members=n=>[...primary(groups[n].filter(x=>!isLead(x,n)).sort(rank)).sort((a,b)=>a.name.localeCompare(b.name)),
    ...(kids[n]||[]).filter(c=>c!==n).sort().map(c=>inside[c].peer)];
  const short=d=>{const t=tilde(d).split('/');return t.length>3?'…/'+t.slice(-2).join('/'):t.join('/')};
  const ctx=x=>x.context==null?'':`<span class="ctx${x.context>=80?' hi':''}" title="context window in use">${x.context}%</span>`;
  // Sessions open in the same folder show as one card; its panel lists the others.
  const twins=x=>p.peers.filter(y=>y.id!==x.id&&y.cwd===x.cwd&&y.space===x.space);
  // A lead is told apart by its outline (and says which team on hover), so every card keeps its context in one place.
  const node=x=>{const more=twins(x).length;return `<button class="node${x.parent?' is-lead':''}${x.id===sel?' sel':''}" data-sel="${esc(x.id)}"${x.parent?` title="Leads the ${esc(x.space)} team"`:''}>
    <div class="nm"><span><span class="dot ${x.busy?'busy':''}"></span>${esc(x.name)}</span>${ctx(x)}</div>
    <div class="dir">${esc(short(x.cwd))}</div><div class="sum">${esc(x.summary)||'no summary yet'}</div>${more?`<div class="small mut">+${more} more session${more>1?'s':''} here</div>`:''}${x.stale&&!x.remote?'<div class="stale">reload needed</div>':''}</button>`};
  // One card per folder: the busiest, else most recently active, session stands for the rest.
  const primary=list=>{const seen=new Set();return list.filter(x=>{const k=x.space+'\u0000'+x.cwd;if(seen.has(k))return false;seen.add(k);return true})};
  const rank=(a,b)=>(b.busy-a.busy)||(a.last_seen_s-b.last_seen_s);
  const unit=(n,seen)=>{if(seen.has(n))return '';seen.add(n);
    const list=members(n),branches=list.some(x=>x.parent&&x.space!==n);
    const count=list.reduce((t,x)=>t+(x.parent&&x.space!==n?primary(leads[x.space]).length:1),0);
    const cols=list.map(x=>x.parent&&x.space!==n&&!seen.has(x.space)
      ?`<div class="ocol"><div class="orow stack">${primary(leads[x.space].slice().sort(rank)).map(node).join('')}</div><div class="down"></div>${unit(x.space,seen)}</div>`:`<div class="ocol">${node(x)}</div>`);
    // A team with no teams under it stacks, so each lead's column stays one card wide.
    const stack=inside[n]&&inside[n].parent!==n&&!branches;
    return `<div class="unit"><div class="unit-h">${esc(n)}${inside[n]&&inside[n].parent!==n?' team':''} · ${count}</div><div class="orow${stack?' stack':''}">${cols.join('')||'<span class="mut small">no live agents</span>'}</div></div>`};
  const sunit=(n,seen)=>{if(seen.has(n))return '';seen.add(n);
    const own=groups[n].filter(x=>!isLead(x,n)).map(x=>esc(x.name));
    const ch=(kids[n]||[]).filter(c=>c!==n&&!seen.has(c)).sort();
    return `<div class="ocol"><div class="sbox${inside[n]?' led':''}"><div class="name">${esc(n)}</div>
      ${inside[n]?`<div class="small" style="color:var(--acc)">led by ${esc(inside[n].lead)}${inside[n].parent!==n?' · inside '+esc(inside[n].parent):''}</div>`:''}
      <div class="small mut">${own.join(', ')||'no other agents'}</div></div>
      ${ch.length?`<div class="down"></div><div class="branch">${ch.map(c=>sunit(c,seen)).join('')}</div>`:''}</div>`};
  $('#n-agents').textContent=p.peers.length||'';$('#n-spaces').textContent=names.length||'';$('#n-msgs').textContent=m.messages.length||'';
  // Spaces caught in a parent loop have no root; they are shown at the top level after the rest.
  const all=(fn)=>{const seen=new Set();let out=roots.map(n=>fn(n,seen)).join('');for(const n of Object.keys(groups).sort())if(!seen.has(n))out+=fn(n,seen);return out};
  $('#peers').innerHTML=p.peers.length?all((n,seen)=>{const u=unit(n,seen);return u?`<div class="chart">${u}</div>`:''})
    :'<div class="empty">No live agents. Start Claude Code with the peerlink plugin.</div>';
  // Agents still on older plugin code: they keep working, but get fixes only after /reload-plugins.
  const behind=p.peers.filter(x=>x.stale&&!x.remote);
  $('#stalebar').style.display=behind.length?'block':'none';
  $('#stalebar').innerHTML=behind.length?`<b>${behind.length} agent${behind.length>1?'s':''} on an older peerlink plugin</b> (${behind.map(x=>esc(x.name)).join(', ')}). Update peerlink from the marketplace if needed, then run <span class="mono">/reload-plugins</span> in each.`:'';
  const picked=p.peers.find(x=>x.id===sel);
  const panel=picked?agentPanel(picked,p.peers.filter(y=>y.id!==picked.id&&y.cwd===picked.cwd&&y.space===picked.space)):'';
  // Never redraw the panel under someone editing it.
  if(panel!==lastPanel&&!(editing&&picked&&$('#agentform')?.dataset.id===picked.id)){$('#detail').innerHTML=panel;lastPanel=panel}
  // The open panel takes room beside the chart rather than covering part of it.
  document.body.classList.toggle('drawer',!!picked);
  // The default space is always listed: it's where every agent with no space set lands.
  const dflt=groups.default?'':`<div class="chart"><div class="orow" style="justify-content:flex-start"><div class="ocol"><div class="sbox"><div class="name">default</div><div class="small mut">agents with no space set · none live</div></div></div></div></div>`;
  $('#spacelist').innerHTML=all((n,seen)=>{const u=sunit(n,seen);return u?`<div class="chart"><div class="orow" style="justify-content:flex-start">${u}</div></div>`:''})+dflt;
  livePeers=p.peers;lastMsgs=m.messages;renderMessages(lastMsgs);
}
document.addEventListener('input',e=>{if(e.target.closest('#agentform'))editing=true});
document.addEventListener('change',e=>{if(e.target.closest('#agentform'))editing=true});
// Save applies only what changed, in one go, and says what happened.
document.addEventListener('submit',async e=>{if(e.target.id!=='agentform')return;e.preventDefault();
  const f=e.target,d=f.dataset,id=d.id,msg=$('#agentmsg'),v=n=>f.elements[n].value.trim();
  const patch={},errs=[];
  if(v('nickname')!==d.nick)patch.nickname=v('nickname');
  if(v('override')!==d.override)patch.space=v('override');
  if(f.elements.wake.value!==d.wake)patch.auto_wake=f.elements.wake.value==='default'?null:f.elements.wake.value==='on';
  if(f.elements.muted.checked!==!!d.muted)patch.muted=f.elements.muted.checked;
  msg.className='small';msg.textContent='Saving…';
  if(Object.keys(patch).length){const r=await post(`/api/sessions/${encodeURIComponent(id)}/config`,patch);if(r.error)errs.push(r.error);else cfg.sessions=r.sessions}
  for(const [field,key] of [['spacefile','space'],['parent','parent']])if(v(field)!==d[field]){
    const r=await post(`/api/sessions/${encodeURIComponent(id)}/space-file`,{[key]:v(field)});if(r.error)errs.push(r.error)}
  editing=false;lastPanel='';await refresh(true);
  const m2=$('#agentmsg');if(m2){m2.className='small '+(errs.length?'err':'ok');m2.textContent=errs.length?errs.join(' '):'Saved'}});
document.addEventListener('click',async e=>{const t=e.target.closest('button');if(!t)return;
  if(t.dataset.sel!==undefined){sel=sel===t.dataset.sel?'':t.dataset.sel;editing=false;refresh(true);return}
  if(t.dataset.close!==undefined){sel='';editing=false;refresh(true);return}
  const act=t.dataset.act;if(!act)return;
  if(act==='logout'){await post('/api/web/logout');location.reload()}
  if(act==='logout-all'&&confirm('Sign out every browser, including this one?')){await post('/api/web/logout',{all:true});location.reload()}
  if(act==='rotate'&&confirm('Rotate the key? Every browser is signed out; agents pick up the new key by themselves.')){await post('/api/key/rotate');location.reload()}
  if(act==='restart'){await post('/api/restart');$('#ctlmsg').textContent='Restarting…';setTimeout(()=>location.reload(),2500)}});
async function loadCfg(){cfg=await api('/api/config');const f=$('#cfg');
  for(const el of f.elements){if(!el.name)continue;const v=cfg[el.name];
    if(el.type==='checkbox')el.checked=!!v;else if(el.name==='remotes')el.value=(v||[]).join('\n');else el.value=v??''}
  $('#guidance').placeholder=cfg.default_guidance||'';
  $('#rules [name=space_rules]').value=(cfg.space_rules||[]).map(r=>`${r.path} = ${r.space}`).join('\n')}
$('#cfg').addEventListener('submit',async e=>{e.preventDefault();const f=e.target,b={};
  for(const el of f.elements){if(!el.name)continue;
    b[el.name]=el.type==='checkbox'?el.checked:el.type==='number'?Number(el.value):el.name==='remotes'?el.value.split('\n').map(s=>s.trim()).filter(Boolean):el.value}
  cfg=await api('/api/config',{method:'PUT',headers:J,body:JSON.stringify(b)});
  $('#saved').textContent='Saved '+new Date().toLocaleTimeString()});
$('#rules').addEventListener('submit',async e=>{e.preventDefault();
  const rules=e.target.space_rules.value.split('\n').map(l=>l.split('=')).filter(p=>p.length===2&&p[0].trim()&&p[1].trim()).map(([a,b])=>({path:a.trim(),space:b.trim()}));
  cfg=await api('/api/config',{method:'PUT',headers:J,body:JSON.stringify({space_rules:rules})});
  $('#rulesaved').textContent='Saved '+new Date().toLocaleTimeString();refresh()});
// Poll every 2 s; while the daemon is unreachable say so, back off to 10 s, and pick up again once it's back.
let misses=0;
async function tick(){
  try{await refresh();if(misses){misses=0;$('#offline').style.display='none';await loadCfg();await refresh(true)}}
  catch(e){if(!signedOut){misses++;$('#offline').style.display='block'}}
  if(!signedOut)setTimeout(tick,misses?10000:2000)}
show();loadCfg().then(()=>refresh(true)).catch(()=>{misses=1;$('#offline').style.display='block'}).finally(()=>setTimeout(tick,2000));
</script></body></html>
"""


PAGE_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Peerlink</title><link rel="icon" href="data:,"><style>
:root{--bg:#faf9f7;--fg:#1f1e1c;--mut:#6b6862;--line:#e4e1db}
@media (prefers-color-scheme:dark){:root{--bg:#1c1b19;--fg:#ecebe8;--mut:#9b978f;--line:#34322e}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif}
main{max-width:520px;margin:15vh auto;padding:0 16px}h1{font-size:20px}p{color:var(--mut)}
code{font-family:ui-monospace,Menlo,monospace;font-size:13px;border:1px solid var(--line);border-radius:4px;padding:1px 5px;color:var(--fg)}
</style></head><body><main><h1>{title}</h1><p>{text}</p></main></body></html>"""


if __name__ == "__main__":
    argv = sys.argv[1:]
    name = ALIASES.get(argv[0], argv[0]) if argv else "help"
    if name in ("help", "-h", "--help") or name not in COMMANDS:
        cli_help()
        sys.exit(0 if name in ("help", "-h", "--help") else 2)
    sys.exit(COMMANDS[name][0](argv[1:]) or 0)
