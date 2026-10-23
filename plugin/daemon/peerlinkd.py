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
import shutil
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

VERSION = "0.8.4"
# A fingerprint of this file: an agent whose plugin ships different code at the same version replaces the daemon.
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
import discordlink  # noqa: E402  (beside this file; installs nothing)

# A fingerprint of the daemon's code, this file and discordlink.py together.
_h = hashlib.sha256()
for _name in ("peerlinkd.py", "discordlink.py"):
    with open(os.path.join(_here, _name), "rb") as _f:
        _h.update(_f.read())
CODE = _h.hexdigest()[:16]
HOME = os.environ.get("PEERLINK_HOME", os.path.expanduser("~/.peerlink"))
CONFIG_PATH = os.path.join(HOME, "config.json")
PID_PATH = os.path.join(HOME, "daemon.pid")
LOG_PATH = os.path.join(HOME, "daemon.log")
MESSAGES_PATH = os.path.join(HOME, "messages.jsonl")
ISSUES_PATH = os.path.join(HOME, "issues.jsonl")
TASKS_PATH = os.path.join(HOME, "tasks.json")
SPAWNS_PATH = os.path.join(HOME, "spawns.json")
TERMINATIONS_PATH = os.path.join(HOME, "terminations.jsonl")
DISCORD_PATH = os.path.join(HOME, "discord.json")  # the bot token and the channel mapping, 0600
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
# You, the person these agents work for: an agent reaches you as "user" or by your login name.
USER_NAME = os.environ.get("USER") or os.path.basename(os.path.expanduser("~")) or "you"
USER_ALIASES = {"user", "you", USER_NAME.lower()}
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
- To hand a peer a piece of work, send_message with kind "task". A task \
that comes to you ("task t3") is yours until you call update_task with \
done or failed and a short note; the note goes to whoever asked.
- To tell your whole space something everyone needs (a shared API change, \
a freeze), send_message with kind "announcement" and to "space". Nobody \
replies, so keep them rare.
- When work belongs in a folder no agent is running in, request_spawn asks \
your user to start one there; nothing starts without their approval.
- When you need your user to do something only they can (a login, an \
approval, a decision), send_message to "user": kind "task" for something to \
do, expects_reply for a question. It waits in their Inbox, and on Discord \
when you have a channel there; their answer comes back to you.
- If peerlink itself misbehaves (a reply that never comes, a message that \
reached the wrong agent, an error from these tools), call report_issue with \
the message ids and agent names, then carry on with your task. When \
peerlink gets in your way or could work better, report_issue with type \
"idea"."""

DEFAULT_CONFIG = {
    "poll_interval_ms": 3000,
    "peer_ttl_s": 30,
    "auto_wake_idle": True,
    "inject_while_busy": True,
    "max_hops": 6,
    "rate_limit_per_min": 12,
    "announce_per_hour": 6,  # announcements each agent may send: each one reaches a whole space
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
tasks = {}      # id -> task, see new_task()
feedback = {}   # id -> a bug report or idea about peerlink, see report_issue()
next_task = [1]
announce_times = {}  # agent id -> when it last announced, for announce_per_hour
spawns = {}          # id -> a request to start an agent, see request_spawn()
spawn_spaces = {}    # folder -> the space an approved agent joins when it registers there
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
        "discord": routes_for(rec.get("cwd", "")) if rec and discord.get("token") else [],
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
        "open_tasks": sum(1 for t in tasks.values() if t["status"] in TASK_OPEN and t["owner_cwd"] and t["owner_cwd"] == rec.get("cwd")),
        "reach": sorted(reach(rec)),
        "busy": rec.get("busy", False),
        # The plugin version the agent runs ('' before 0.6.6): one behind the daemon needs /reload-plugins.
        "plugin": rec.get("plugin", ""),
        "context": rec.get("context"),  # percent of the context window in use, when the agent reports it
        "stale": rec.get("plugin", "") != VERSION,
        "muted": session_cfg(rec["id"])["muted"],
        "terminating": bool(rec.get("terminate")),
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


# -- Discord ---------------------------------------------------------------------------------------
# The mapping: each Discord channel has one master agent (your messages go to it) and any members (they can
# post there, and "name: ..." addresses one directly). Agents are kept by folder, so a mapping survives a
# restart, /clear or a new session. Direct messages go to `dm`.
discord = {"token": "", "app_id": "", "user": "", "enabled": True, "routes": [], "dm": "",
           # What agents may do beyond text, each a switch on the Channels page.
           "buttons": True, "files_out": True, "files_in": True, "max_file_mb": 10,
           # Bot user IDs whose posts reach agents as yours do (a voice assistant, say); other bots are ignored.
           "trusted_bots": []}
DISCORD_FILES = os.path.join(HOME, "files", "discord")  # what you attach on Discord, for the agent to read
discord_bot = [None]


def load_discord():
    try:
        with open(DISCORD_PATH) as f:
            discord.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_discord():
    fd = os.open(DISCORD_PATH + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(discord, f, indent=2)
    os.replace(DISCORD_PATH + ".tmp", DISCORD_PATH)


def start_discord():
    old = discord_bot[0]
    if old:
        old.close()
    discord_bot[0] = None
    if discord.get("token") and discord.get("enabled", True):
        discord_bot[0] = discordlink.Bot(discord["token"], on_discord, log, on_press)
        discord_bot[0].trusted_bots = set(discord.get("trusted_bots") or [])


def folder_name(cwd):
    return (config.get("folder_names") or {}).get(cwd) or os.path.basename(cwd.rstrip("/")) or cwd


def discord_channel_label(channel_id):
    bot = discord_bot[0]
    name = bot.channel_name(channel_id) if bot else ""
    return f"#{name}" if name else "Discord"


def routes_for(cwd):
    """The channels an agent's folder is mapped into, and whether it's their master."""
    out = []
    for r in discord.get("routes", []):
        if r.get("master") == cwd or cwd in (r.get("members") or []):
            out.append({"channel_id": r["channel_id"], "channel": discord_channel_label(r["channel_id"]),
                        "master": r.get("master") == cwd, "kinds": r.get("kinds") or []})
    if discord.get("dm") == cwd:
        out.append({"channel_id": "dm", "channel": "direct messages", "master": True})
    return out


KINDS = ("message", "task", "announcement")


def carries(route, kind):
    """A channel with no kinds set carries everything; one set to some kinds carries only those."""
    return not route.get("kinds") or kind in route["kinds"]


def kind_channels(kind, cwds=(), space=""):
    """Channels set aside for one kind (#tasks, #announcements) that follow these folders, or this space."""
    out = []
    for r in discord.get("routes", []):
        if kind not in (r.get("kinds") or []):
            continue
        folders = {r.get("master")} | set(r.get("members") or [])
        if folders & set(c for c in cwds if c) or (space and r.get("master") and space_of({"id": "", "cwd": r["master"]})[0] == space):
            out.append(r["channel_id"])
    return out


def post_async(posts):
    """[(channel, text, reply_to)] posted off the request thread: Discord's latency is none of the caller's business."""
    if not posts or not discord.get("token") or not discord_bot[0]:
        return
    def run():
        for channel, text, ref in posts:
            try:
                discordlink.post(discord["token"], channel, text, ref)
            except discordlink.DiscordError as err:
                log("discord", "post failed:", err)
    threading.Thread(target=run, daemon=True).start()


def announce(sender_rec, space, text, hops=0, from_discord=None):
    """One message to every live agent in a space, read at the start of each one's next turn; nobody replies."""
    with lock:
        now = time.time()
        recipients = [r for r in sessions.values() if is_live(r, now) and r["id"] != (sender_rec or {}).get("id") and space in reach(r)]
        if from_discord:
            sender = {"from": f"discord:{from_discord['channel_id']}", "from_name": f"{from_discord['author']} on Discord", "from_host": "discord",
                      "from_cwd": "", "from_space": "discord", "discord": {"channel_id": from_discord["channel_id"], "message_id": from_discord["message_id"]}}
        else:
            sender = {"from": sender_rec["id"], "from_name": display_name(sender_rec), "from_host": HOSTNAME,
                      "from_cwd": sender_rec.get("cwd", ""), "from_space": space_of(sender_rec)[0]}
        msg = {"id": f"m{next_msg[0]}", "ts": now, **sender, "to": f"space:{space}", "to_name": f"everyone in {space}",
               "to_cwd": "", "to_space": space, "text": text[:20000], "expects_reply": False, "reply_to": None,
               "hops": hops, "status": "queued" if recipients else "delivered", "v": 2, "kind": "announcement",
               "recipients": [display_name(r) for r in recipients]}
        next_msg[0] += 1
        for r in recipients:
            inboxes.setdefault(r["id"], []).append(msg)
        record(msg)
    log("announce", msg["id"], msg["from_name"], "->", space, len(recipients))
    if not from_discord:
        post_async([(c, f"📣 **{msg['from_name']}** to everyone in {space}: {text}", None)
                    for c in kind_channels("announcement", [msg["from_cwd"]], space)])
    return msg


def save_discord_files(d):
    """Files you attached, saved where the agent can read them; the lines that tell it so."""
    if not d.get("files"):
        return ""
    if not discord.get("files_in", True):
        return "\n\n(Files were attached on Discord, but saving them is turned off on the Channels page.)"
    limit = int(float(discord.get("max_file_mb") or 10) * 1024 * 1024)
    folder = os.path.join(DISCORD_FILES, d["message_id"])
    lines = []
    for f in d["files"][:10]:
        name = os.path.basename(f["filename"]) or "file"
        try:
            data = discordlink.download(f["url"], limit)
        except OSError as err:
            lines.append(f"- {name}: couldn't download it ({err})")
            continue
        if data is None:
            lines.append(f"- {name}: over the {discord.get('max_file_mb', 10)} MB limit, not saved")
            continue
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "wb") as out:
            out.write(data)
        lines.append(f"- {path} ({len(data):,} bytes)")
    return "\n\nAttached on Discord:\n" + "\n".join(lines)


def queue_for_folder(cwd, msg):
    """Deliver to the folder's live agent, or hold it for the next one there (adopt() hands it over)."""
    agent = successor(cwd)
    msg["to"] = agent["id"] if agent else f"folder:{cwd}"
    msg["to_name"] = display_name(agent) if agent else folder_name(cwd)
    inboxes.setdefault(msg["to"], []).append(msg)
    record(msg)
    return agent


def on_discord(d):
    """A message from you on Discord: to the channel's master, or to a member it names first."""
    bot = discord_bot[0]
    if not bot:
        return
    # Your own words can work the Inbox: "/inbox", "1 done", or a reply to a task posted for you. Never a bot's.
    if not d.get("bot") and (d["dm"] or d["mentioned"] or bot.reads_messages) and inbox_command(d):
        return
    if d["dm"]:
        route = {"channel_id": d["channel_id"], "master": discord.get("dm"), "members": []}
    else:
        route = next((r for r in discord.get("routes", []) if r["channel_id"] == d["channel_id"]), None)
        # Without the Message Content switch only @mentions carry text; with it, every message in a mapped channel.
        if route is None or not (d["mentioned"] or bot.reads_messages):
            return
    text, cwd = d["text"], route.get("master")
    # "storefront: ..." or "@storefront ..." addresses a member of this channel by name.
    head, sep, rest = text.partition(":")
    first = text.split(None, 1)
    for candidate, body in ((head.strip().lstrip("@"), rest.strip()) if sep else ("", ""),
                            (first[0].lstrip("@"), first[1] if len(first) > 1 else "") if first and first[0].startswith("@") else ("", "")):
        if not candidate:
            continue
        for member in [route.get("master")] + list(route.get("members") or []):
            agent = successor(member or "")
            names = {folder_name(member or "").lower(), (display_name(agent) if agent else "").lower()} - {""}
            if member and candidate.lower() in names:
                text, cwd = body or text, member
                break
        else:
            continue
        break
    if not cwd:
        discordlink.post(discord["token"], d["channel_id"], "No agent is mapped to this yet: pick one in the peerlink web UI, under Channels.", d["message_id"])
        return
    text += save_discord_files(d)
    if not text.strip():
        return
    # A "Task:" or "Announcement:" prefix says what it is; a channel set to one kind makes everything that kind.
    kinds = route.get("kinds") or []
    prefix = re.match(r"\s*(task|announcement|announce)\s*:\s*", text, re.I)
    kind = ("announcement" if prefix.group(1).lower().startswith("announce") else "task") if prefix else ""
    if kind and not carries(route, kind):
        kind = ""
    if not kind:
        kind = "message" if carries(route, "message") else "task" if "task" in kinds else "announcement"
    if kind == "announcement":
        space = space_of({"id": "", "cwd": cwd})[0]
        msg = announce(None, space, text[prefix.end():] if prefix else text, from_discord=d)
        n = len(msg["recipients"])
        discordlink.post(discord["token"], d["channel_id"], f"Announced to {n} agent{'' if n == 1 else 's'} in {space}." if n else f"No agent in {space} is running; nobody heard it.", d["message_id"])
        return
    with lock:
        msg = {
            "id": f"m{next_msg[0]}", "ts": time.time(),
            "from": f"discord:{d['channel_id']}", "from_name": f"{d['author']} on Discord", "from_host": "discord",
            "from_cwd": "", "to_cwd": cwd, "from_space": "discord", "to_space": "",
            "text": text[:20000], "expects_reply": True, "reply_to": None, "hops": 0, "status": "queued", "v": 2,
            "discord": {"channel_id": d["channel_id"], "message_id": d["message_id"]},
        }
        next_msg[0] += 1
        task = new_task(msg, {"id": "", "name": msg["from_name"], "cwd": "", "discord": msg["discord"]}) if kind == "task" else None
        agent = queue_for_folder(cwd, msg)
        msg["to_space"] = space_of(agent)[0] if agent else ""
        if task:
            task["owner"], task["space"] = msg["to_name"], msg["to_space"]
            save_tasks()
    log("discord in", msg["id"], d["author"], "->", msg["to_name"])
    set_reaction(msg, "👀")
    if not agent:
        discordlink.post(discord["token"], d["channel_id"], f"{folder_name(cwd)} isn't running right now. Your message waits up to an hour for it.", d["message_id"])


# A message from Discord shows where it's at, as a bot does: 👀 heard, 🤔 thinking, 🛠️ using tools,
# ❓ asked you something, ✅ answered. The agent's plugin reports its activity with each heartbeat; the daemon
# swaps the reaction on every open Discord message to that agent. Discord calls go through a queue on their own
# thread, so a heartbeat never waits on Discord.
ACTIVITY_EMOJI = {"thinking": "🤔", "tools": "🛠️", "asking": "❓"}
reactions = {}  # message id -> the emoji it shows now
reaction_jobs = []
reaction_wake = threading.Event()


def set_reaction(msg, emoji):
    d = msg.get("discord")
    if not d or reactions.get(msg["id"]) == emoji:
        return
    old = reactions.get(msg["id"])
    reactions[msg["id"]] = emoji
    reaction_jobs.append((d["channel_id"], d["message_id"], old, emoji))
    reaction_wake.set()


def reaction_worker():
    while True:
        reaction_wake.wait()
        reaction_wake.clear()
        while reaction_jobs:
            channel, message, old, new = reaction_jobs.pop(0)
            if old:
                discordlink.unreact(discord["token"], channel, message, old)
            if new:
                discordlink.react(discord["token"], channel, message, new)


def discord_activity(rec, activity):
    """Show an agent's activity on the Discord messages it still has to answer."""
    emoji = ACTIVITY_EMOJI.get(activity)
    if not emoji:
        return
    answered = {m.get("reply_to") for m in messages if m.get("to", "").startswith("discord:")}
    for m in messages[-200:]:
        closed = tasks.get(m.get("task") or "", {}).get("status") in TASK_CLOSED
        if m.get("discord") and m.get("to") == rec["id"] and m.get("status") == "delivered" and m["id"] not in answered and not closed:
            set_reaction(m, emoji)


def read_files(paths):
    """The files an agent attaches: (name, bytes) each, or an error saying which one and why."""
    if not discord.get("files_out", True):
        return None, "attaching files to Discord is turned off on the Channels page"
    limit = float(discord.get("max_file_mb") or 10) * 1024 * 1024
    out = []
    for p in paths[:10]:
        p = os.path.expanduser(str(p))
        if not os.path.isfile(p):
            return None, f"no file at {p}"
        if os.path.getsize(p) > limit:
            return None, f"{p} is over the {discord.get('max_file_mb', 10)} MB limit"
        with open(p, "rb") as f:
            out.append((os.path.basename(p), f.read()))
    return out, None


def post_to_discord(sender_rec, channel_id, text, reply_ref=None, reply_to=None, hops=0, options=None, files=None, expects_reply=False):
    """An agent's message out to Discord, labelled with who said it, and recorded like any other."""
    name = display_name(sender_rec)
    uploads, err = read_files(files) if files else (None, None)
    if err:
        return 400, {"error": err}
    with lock:
        mid = f"m{next_msg[0]}"
        next_msg[0] += 1
    use_buttons = bool(options) and discord.get("buttons", True)
    body = f"**{name}**: {text}" if not reply_ref else text
    if options and not use_buttons:
        body += "\n" + "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
    try:
        sent = discordlink.post(discord["token"], channel_id, body, reply_ref,
                                discordlink.buttons(options, f"pl:{mid}") if use_buttons else None, uploads)
    except discordlink.DiscordError as err:
        return 502, {"error": f"Discord refused it: {err}"}
    with lock:
        msg = {
            "id": mid, "ts": time.time(), "from": sender_rec["id"], "from_name": name,
            "from_host": HOSTNAME, "to": f"discord:{channel_id}", "to_name": f"{discord_channel_label(channel_id)} on Discord",
            "from_cwd": sender_rec.get("cwd", ""), "to_cwd": "", "from_space": space_of(sender_rec)[0], "to_space": "discord",
            "text": text[:20000], "expects_reply": bool(use_buttons) or expects_reply, "reply_to": reply_to, "hops": hops, "status": "delivered", "v": 2,
            "options": list(options or []), "files": [os.path.expanduser(str(f)) for f in files or []],
            "discord_out": {"channel_id": channel_id, "message_id": str((sent[-1] or {}).get("id", "")) if sent else ""},
        }
        record(msg)
    return 200, {"id": msg["id"], "to": msg["to_name"], "status": "delivered"}


def on_press(press):
    """You pressed one of an agent's buttons: the choice goes to that agent as your answer to its message,
    and the buttons are disabled with yours highlighted, so it reads as answered."""
    if press["custom_id"].startswith("pt:"):
        return on_task_press(press)
    if press["custom_id"].startswith("ps:"):
        return on_spawn_press(press)
    if press["custom_id"].startswith("pk:"):
        _, sid, idx = (press["custom_id"].split(":") + ["", ""])[:3]
        code, out = terminate(sid, f"Discord ({press['author']})") if idx == "0" else (200, {"kept": True})
        rows = (press["message"] or {}).get("components") or []
        for row in rows:
            for b in row.get("components", []):
                b["disabled"], b["style"] = True, 3 if b.get("custom_id") == press["custom_id"] else 2
        discordlink.answer_interaction(press["id"], press["token"], 7, {"components": rows})
        if code != 200:
            discordlink.post(discord["token"], press["channel_id"], out.get("error", "Couldn't end it."))
        return
    _, mid, idx = (press["custom_id"].split(":") + ["", ""])[:3]
    asked = next((m for m in messages if m["id"] == mid), None)
    options = (asked or {}).get("options") or []
    if not asked or not idx.isdigit() or int(idx) >= len(options):
        discordlink.answer_interaction(press["id"], press["token"], 4, {"content": "That question isn't open anymore.", "flags": 64})
        return
    choice = options[int(idx)]
    rows = (press["message"] or {}).get("components") or []
    for row in rows:
        for b in row.get("components", []):
            b["disabled"] = True
            b["style"] = 3 if b.get("custom_id") == press["custom_id"] else 2
    discordlink.answer_interaction(press["id"], press["token"], 7, {"components": rows})
    with lock:
        msg = {
            "id": f"m{next_msg[0]}", "ts": time.time(),
            "from": f"discord:{press['channel_id']}", "from_name": f"{press['author']} on Discord", "from_host": "discord",
            "from_cwd": "", "to_cwd": asked.get("from_cwd", ""), "from_space": "discord", "to_space": asked.get("from_space", ""),
            "text": f"Chose: {choice}", "expects_reply": False, "reply_to": mid, "hops": 0, "status": "queued", "v": 2,
            "discord": {"channel_id": press["channel_id"], "message_id": (asked.get("discord_out") or {}).get("message_id", "")},
        }
        next_msg[0] += 1
        queue_for_folder(asked.get("from_cwd", ""), msg)
    log("discord press", msg["id"], press["author"], "->", msg["to_name"], choice)


def send_discord(sender_rec, to, text, parent, hops, options=None, files=None, expects_reply=False):
    """`to: "discord"` or `"discord#name"`, or a reply to a message from Discord. None when it's not for Discord."""
    if not sender_rec or not discord.get("token") or not discord_bot[0]:
        return None
    if parent and parent.get("discord") and parent.get("to_cwd") == sender_rec.get("cwd"):
        d = parent["discord"]
        out = post_to_discord(sender_rec, d["channel_id"], text, d["message_id"], parent["id"], hops, options, files, expects_reply)
        if out[0] == 200:
            set_reaction(parent, "✅")
        return out
    word = to.strip().lower()
    if not (word == "discord" or word.startswith("discord#")):
        return None
    mine = [r for r in routes_for(sender_rec.get("cwd", "")) if r["channel_id"] != "dm" and carries(r, "message")]
    if word.startswith("discord#"):
        want = "#" + word.split("#", 1)[1].lstrip("#")
        mine = [r for r in mine if r["channel"].lower() == want]
    if not mine:
        return 404, {"error": "this agent isn't mapped to a Discord channel that carries messages; the mapping is in the web UI, under Channels"}
    if len(mine) > 1:
        return 409, {"error": "this agent is in several Discord channels: say which, e.g. " + ", ".join(f"discord{r['channel']}" for r in mine)}
    return post_to_discord(sender_rec, mine[0]["channel_id"], text, None, None, hops, options, files, expects_reply)


def user_send(sender_rec, word, text, kind, parent_task, expects_reply, hops, options, files):
    """A message or task for you: it waits in your Inbox, and is posted to Discord when the agent has a channel there."""
    chans = [r for r in routes_for(sender_rec.get("cwd", "")) if r["channel_id"] != "dm" and carries(r, kind)] if discord.get("token") and discord_bot[0] else []
    if word.startswith("discord#"):
        chans = [r for r in chans if r["channel"].lower() == "#" + word.split("#", 1)[1].lstrip("#")]
    if word.startswith("discord") and not chans:
        return 404, {"error": "this agent isn't mapped to that Discord channel; send it to \"user\" to put it in their Inbox, or map a channel under Channels"}
    channel = chans[0]["channel_id"] if chans else ""
    if kind != "task" and channel:
        return post_to_discord(sender_rec, channel, text, None, None, hops, options, files, expects_reply)
    if options:
        text += "\n" + "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
    if files:
        text += "\n\nFiles:\n" + "\n".join(f"- {os.path.abspath(os.path.expanduser(f))}" for f in files)
    with lock:
        msg = {"id": f"m{next_msg[0]}", "ts": time.time(), "from": sender_rec["id"], "from_name": display_name(sender_rec),
               "from_host": HOSTNAME, "to": "user", "to_name": USER_NAME, "from_cwd": sender_rec.get("cwd", ""), "to_cwd": "",
               "from_space": space_of(sender_rec)[0], "to_space": "", "text": text[:20000],
               "expects_reply": expects_reply or kind == "task", "reply_to": None, "hops": hops, "status": "delivered", "v": 2}
        next_msg[0] += 1
        task = None
        if kind == "task":
            task = new_task(msg, {"id": sender_rec["id"], "name": msg["from_name"], "cwd": msg["from_cwd"]}, parent_task)
            task["user"], task["owner"] = True, USER_NAME
            save_tasks()
        record(msg)
    if task and channel:
        try:
            sent = discordlink.post(discord["token"], channel, f"**{msg['from_name']}** has a task for you ({task['id']}): {text}",
                                    None, discordlink.buttons(["Done", "Couldn't do it"], f"pt:{task['id']}"))
            with lock:
                task["discord_out"] = {"channel_id": channel, "message_id": str((sent[-1] or {}).get("id", "")) if sent else ""}
                save_tasks()
        except discordlink.DiscordError as err:
            log("discord", "task post failed:", err)
    log("to user", msg["id"], msg["from_name"], task["id"] if task else "")
    out = {"id": msg["id"], "to": USER_NAME, "status": "waiting in their Inbox"}
    if task:
        out["task"] = task["id"]
    return 200, out


def inbox_items():
    """Everything waiting on you: tasks you own, questions agents wait on, new feedback; the longest waiting first."""
    now = time.time()
    with lock:
        out = [{"kind": "task", "id": t["id"], "who": t["requester"].get("name", ""), "since": t["created"], "title": t["title"], "text": t["text"]}
               for t in tasks.values() if t.get("user") and t["status"] in TASK_OPEN]
        recent = [m for m in messages if now - m["ts"] < 3 * 86400]
        replied = {m.get("reply_to") for m in recent if m.get("reply_to")}
        for i, m in enumerate(recent):
            if not m.get("expects_reply") or m.get("task") or m.get("inbox_done") or m["id"] in replied:
                continue
            if not (m.get("to") == "user" or str(m.get("to", "")).startswith("discord:")):
                continue
            # On Discord you answer by writing in the channel, not always as a reply: anything you send that agent after it counts.
            if any(str(n.get("from", "")).startswith("discord:") and n.get("to_cwd") == m.get("from_cwd") for n in recent[i + 1:]):
                continue
            out.append({"kind": "question", "id": m["id"], "who": m["from_name"], "since": m["ts"], "title": task_title_of(m["text"])[:120], "text": m["text"]})
        out += [{"kind": "feedback", "id": f["id"], "who": f.get("from_name") or "an agent", "since": f["ts"], "title": task_title_of(f["text"])[:120],
                 "text": f["text"], "type": f.get("type", "bug")} for f in feedback.values() if f.get("status") == "new"]
        out += [{"kind": "approval", "id": x["id"], "who": x["requester"].get("name", ""), "since": x["created"],
                 "title": f"Start an agent in {tilde_path(x['dir'])}", "text": f"{x['purpose']}\n\nSpace: {x['space']}\nFirst prompt: {x['prompt']}"}
                for x in spawns.values() if x["status"] == "pending"]
    order = {"approval": 0, "task": 1, "question": 2, "feedback": 3}
    return sorted(out, key=lambda x: (order[x["kind"]], x["since"]))


def inbox_act(body, discord_from=None):
    """Done or failed for your task, an answer or dismissal for a question, accept or dismiss for feedback."""
    kind, iid, action, text = body.get("kind"), body.get("id", ""), body.get("action", ""), (body.get("text") or "").strip()
    if kind == "task":
        if action not in ("done", "failed"):
            return 400, {"error": "a task is done or failed"}
        t = tasks.get(iid)
        if not t or not t.get("user"):
            return 404, {"error": f"{iid} isn't a task of yours"}
        code, out = update_task(iid, {"status": action, "note": text}, web=True)
        if code == 200:
            close_task_buttons(t)
        return code, out
    if kind == "approval":
        if action not in ("approve", "deny"):
            return 400, {"error": "a request to start an agent is approved or denied"}
        return decide_spawn(iid, action == "approve", "Discord" if discord_from else "the web UI")
    if kind == "feedback":
        if action not in ("accepted", "dismissed"):
            return 400, {"error": "feedback is accepted or dismissed"}
        return update_feedback(iid, {"status": action, **({"note": text} if text else {})})
    if kind == "question":
        with lock:
            m = next((x for x in messages if x["id"] == iid), None)
            if m is None:
                return 404, {"error": f"no message {iid}"}
            if action == "dismiss":
                m["inbox_done"] = True
                append_log({"id": iid, "inbox_done": True})
                return 200, {"ok": True}
            if action != "reply" or not text:
                return 400, {"error": "reply with text, or dismiss"}
            if discord_from:
                d = discord_from
                msg = {"from": f"discord:{d['channel_id']}", "from_name": f"{d['author']} on Discord", "from_host": "discord",
                       "discord": {"channel_id": d["channel_id"], "message_id": d["message_id"]}}
            else:
                msg = {"from": "user", "from_name": f"{USER_NAME} (web UI)", "from_host": HOSTNAME}
            msg.update({"id": f"m{next_msg[0]}", "ts": time.time(), "from_cwd": "", "to_cwd": m.get("from_cwd", ""),
                        "from_space": "", "to_space": m.get("from_space", ""), "text": text[:20000], "expects_reply": False,
                        "reply_to": iid, "hops": 0, "status": "queued", "v": 2})
            next_msg[0] += 1
            agent = queue_for_folder(m.get("from_cwd", ""), msg)
        return 200, {"id": msg["id"], "to": msg["to_name"], "waiting": not agent}
    return 400, {"error": "kind is approval, task, question or feedback"}


def close_task_buttons(t):
    """Grey out the Done / Couldn't do it buttons on a task's Discord post once it's settled elsewhere."""
    out = t.get("discord_out") or {}
    if not out.get("message_id") or not discord.get("token"):
        return
    def edit():
        try:
            discordlink.call(discord["token"], "PATCH", f"/channels/{out['channel_id']}/messages/{out['message_id']}", {"components": []})
        except discordlink.DiscordError as err:
            log("discord", "could not clear task buttons:", err)
    threading.Thread(target=edit, daemon=True).start()


def on_task_press(press):
    _, tid, idx = (press["custom_id"].split(":") + ["", ""])[:3]
    t = tasks.get(tid)
    if not t or t["status"] not in TASK_OPEN:
        discordlink.answer_interaction(press["id"], press["token"], 4, {"content": f"Task {tid} is already {t['status'] if t else 'gone'}.", "flags": 64})
        return
    action = "done" if idx == "0" else "failed"
    rows = (press["message"] or {}).get("components") or []
    for row in rows:
        for b in row.get("components", []):
            b["disabled"] = True
            b["style"] = 3 if b.get("custom_id") == press["custom_id"] else 2
    discordlink.answer_interaction(press["id"], press["token"], 7, {"components": rows})
    update_task(tid, {"status": action}, web=True)
    log("discord press", tid, press["author"], action)


inbox_lists = {}  # Discord channel -> (when, [(kind, id)]): what "1 done" refers to after "/inbox"
INBOX_VERBS = {"done": ("task", "done"), "failed": ("task", "failed"), "fail": ("task", "failed"), "accept": ("feedback", "accepted"),
               "dismiss": (None, "dismiss"), "approve": ("approval", "approve"), "deny": ("approval", "deny")}


def inbox_command(d):
    """Handle what you write on Discord to work your Inbox. True when it was one of those, not a message for an agent."""
    text, channel = d["text"].strip(), d["channel_id"]
    say = lambda line: discordlink.post(discord["token"], channel, line, d["message_id"])
    # A reply to a task posted for you settles it: "done", "done: it's set", "failed: no access".
    task = next((t for t in tasks.values() if t.get("user") and d.get("ref") and (t.get("discord_out") or {}).get("message_id") == d["ref"]), None)
    if task:
        verb, _, note = text.partition(":")
        verb = verb.strip().lower()
        action = "done" if verb in ("done", "✅", "ok", "yes") else "failed" if verb in ("failed", "fail", "no", "can't", "cant") else ""
        if action and task["status"] in TASK_OPEN:
            inbox_act({"kind": "task", "id": task["id"], "action": action, "text": note.strip()})
            say(f"Task {task['id']} marked {action}; {task['requester'].get('name') or 'the agent'} has been told.")
            return True
        return False
    kill = re.match(r"^/?kill\s+@?(\S+)\s*$", text, re.I)
    if kill:
        name = kill.group(1).lower()
        with lock:
            agent = next((r for r in sessions.values() if is_live(r) and display_name(r).lower() == name), None)
        if not agent:
            say(f"No running agent is called {kill.group(1)}.")
            return True
        discordlink.post(discord["token"], channel, f"End **{display_name(agent)}** ({tilde_path(agent.get('cwd', ''))})? It finishes its current turn first.",
                         d["message_id"], discordlink.buttons(["End it", "Keep it"], f"pk:{agent['id']}"))
        return True
    if text.lower() in ("/inbox", "inbox"):
        items = inbox_items()
        inbox_lists[channel] = (time.time(), [(i["kind"], i["id"]) for i in items])
        if not items:
            say("Your Inbox is empty.")
            return True
        icon = {"approval": "🚀", "task": "☑️", "question": "❓", "feedback": "💡"}
        lines = [f"**Inbox** ({len(items)})"] + [
            f"{n}. {icon[i['kind']]} {i['id']} · {i['who']} · {ago_text(time.time() - i['since'])}: {i['title']}" for n, i in enumerate(items[:20], 1)]
        lines.append('Answer with the number: "1 approve" or "1 deny", "2 done" or "2 failed: why", "3 <your answer>", "4 accept" or "4 dismiss".')
        say("\n".join(lines))
        return True
    listed = inbox_lists.get(channel)
    match = re.match(r"^(\d+)[\s.:)]+(.+)$", text, re.S)
    if not (listed and match and time.time() - listed[0] < 3600):
        return False
    n, rest = int(match.group(1)), match.group(2).strip()
    if not 1 <= n <= len(listed[1]):
        say(f"There's no {n} in your Inbox; send /inbox for the list.")
        return True
    kind, iid = listed[1][n - 1]
    verb, _, note = rest.partition(":")
    want = INBOX_VERBS.get(verb.strip().lower())
    if want and want[0] in (None, kind):
        action = want[1] if want[1] != "dismiss" else ("dismissed" if kind == "feedback" else "dismiss")
        if kind == "task" and action == "dismiss":
            action = "failed"
        if kind == "approval" and action == "dismiss":
            action = "deny"
        code, out = inbox_act({"kind": kind, "id": iid, "action": action, "text": note.strip()})
    elif kind == "question":
        code, out = inbox_act({"kind": kind, "id": iid, "action": "reply", "text": rest}, discord_from=d)
    else:
        say(f'{iid} is a {kind}: answer {"done or failed" if kind == "task" else "approve or deny" if kind == "approval" else "accept or dismiss"}.')
        return True
    say(f"{iid}: {out.get('error')}" if code != 200 else f"{iid}: {action if want else 'answer sent'}.")
    return True


def ago_text(s):
    return f"{int(s)}s" if s < 90 else f"{round(s / 60)}m" if s < 5400 else f"{round(s / 3600)}h" if s < 129600 else f"{round(s / 86400)}d"


def send(body):
    sender = body.get("from", "")
    text = (body.get("text") or "").strip()
    to = body.get("to", "")
    if not text or not (to or body.get("reply_to")):
        return 400, {"error": "text, and to or reply_to, are required"}
    kind = body.get("kind") or "message"
    if kind not in KINDS:
        return 400, {"error": "kind is message, task or announcement"}
    parent_task = body.get("parent_task") or ""
    if parent_task and parent_task not in tasks:
        return 400, {"error": f"no task {parent_task}"}
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

    options = [str(o) for o in body.get("options") or []][:25]
    files = [str(f) for f in body.get("files") or []][:10]
    if kind == "announcement":
        if not sender_rec:
            return 400, {"error": "announcements come from agents on this machine"}
        word = to.strip().lower()
        space = space_of(sender_rec)[0] if word in ("space", "everyone", "all", "") else clean_space(word.split(":", 1)[1] if word.startswith("space:") else word)
        if space not in reach(sender_rec):
            return 403, {"error": f"you can announce to {' or '.join(sorted(reach(sender_rec)))}, not {space}"}
        with lock:
            sent = [t for t in announce_times.get(sender, []) if now - t < 3600]
            if len(sent) >= int(config.get("announce_per_hour") or 6):
                return 429, {"error": f"you've sent {len(sent)} announcements this hour; the limit is {config.get('announce_per_hour')}. Message the agents who need it instead"}
            announce_times[sender] = sent + [now]
        msg = announce(sender_rec, space, text, hops)
        n = len(msg["recipients"])
        return 200, {"id": msg["id"], "to": f"everyone in {space} ({n} agent{'' if n == 1 else 's'})", "status": "queued"}
    # A reply to something you sent from the web UI comes back to you.
    if parent and parent.get("from") == "user":
        to = "user"
    word = to.strip().lower()
    if sender_rec and (word in USER_ALIASES or (kind == "task" and word.startswith("discord") and not parent)):
        return user_send(sender_rec, word, text, kind, parent_task, bool(body.get("expects_reply")), hops, options, files)
    out = send_discord(sender_rec, to, text, parent, hops, options, files, bool(body.get("expects_reply")))
    if out is not None:
        return out
    # Between agents, options and files read as text: a numbered list, and paths (the filesystem is shared).
    if options:
        text += "\n" + "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
    if files:
        text += "\n\nFiles:\n" + "\n".join(f"- {os.path.abspath(os.path.expanduser(f))}" for f in files)
    with lock:
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
            elif parent.get("from_cwd"):
                # Nobody there right now (a daemon restart, an agent reloading): it waits for the folder's next agent.
                target = {"id": f"folder:{parent['from_cwd']}", "cwd": parent["from_cwd"], "name": parent.get("from_name") or ""}
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
            "expects_reply": bool(body.get("expects_reply")) or kind == "task",
            "reply_to": body.get("reply_to") or None,
            "hops": hops,
            "status": "queued",
            "v": 2,  # logs older than this never recorded delivery, so they are not replayed
        }
        next_msg[0] += 1
        if kind == "task":
            new_task(msg, {"id": sender, "name": msg["from_name"], "cwd": msg["from_cwd"]}, parent_task)
            post_async([(c, f"📋 **{msg['from_name']}** → **{msg['to_name']}**, task {msg['task']}: {tasks[msg['task']]['title']}", None)
                        for c in kind_channels("task", [msg["from_cwd"], msg["to_cwd"]])])
        inboxes.setdefault(target["id"], []).append(msg)
        if sender in sent_times:
            sent_times[sender].append(now)
        record(msg)
    log("msg", msg["id"], msg["from_name"], "->", msg["to_name"])
    return 200, {"id": msg["id"], "to": msg["to_name"], "status": "queued", "task": msg.get("task")}


def report_issue(body):
    """Log a problem with peerlink itself, and pass it to the agent that maintains peerlink when one is live.

    The maintainer is reached whatever its space: a report about broken routing must not depend on routing."""
    text = (body.get("text") or "").strip()
    if not text:
        return 400, {"error": "text is required"}
    kind = "idea" if body.get("type") == "idea" else "bug"
    with lock:
        sender = sessions.get(body.get("from", ""))
        n = max([int(i[1:]) for i in feedback if i[1:].isdigit()] or [0]) + 1
        issue = {
            "id": f"i{n}",
            "type": kind,
            "status": "new",
            "note": "",
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
        feedback[issue["id"]] = issue
        # A bug goes to the maintainer at once, to fix while it's fresh; an idea waits for you to accept it.
        agent = maintainer(exclude=issue["from"]) if kind == "bug" else None
        if agent:
            msg = {
                "id": f"m{next_msg[0]}",
                "ts": time.time(),
                "from": issue["from"],
                "from_name": issue["from_name"] or "peerlink",
                "from_host": HOSTNAME,
                "to": agent["id"],
                "to_name": display_name(agent),
                "text": f"peerlink bug {issue['id']} from {issue['from_name'] or 'an agent'} (space {issue['space'] or '?'}, plugin {issue['plugin'] or '?'}, daemon {VERSION}):\n{text}"
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


def maintainer(exclude=""):
    """The live agent that works on peerlink: the one named by the issue_agent setting."""
    want = clean_space(config.get("issue_agent")) if config.get("issue_agent") else ""
    return next((r for r in sessions.values() if want and is_live(r) and r["id"] != exclude and display_name(r).lower() == want), None)


def maintainer_folder():
    """Where peerlink's maintainer works, live or not: accepted feedback becomes a task there."""
    agent = maintainer()
    if agent:
        return agent.get("cwd", "")
    want = clean_space(config.get("issue_agent") or "")
    named = [cwd for cwd, name in (config.get("folder_names") or {}).items() if clean_space(name) == want]
    seen = [cwd for cwd, d in dir_summaries.items() if clean_space(d.get("name") or os.path.basename(cwd)) == want]
    return (named or seen or [""])[0]


def load_feedback():
    """issues.jsonl holds each report, then a line for every change to its status or note."""
    try:
        with open(ISSUES_PATH) as f:
            lines = f.readlines()
    except OSError:
        return
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if "text" in entry:
            feedback[entry["id"]] = {"type": "bug", "status": "new", "note": "", **entry}
        elif entry.get("id") in feedback:
            feedback[entry["id"]].update(entry)


def update_feedback(iid, body):
    """Triage from the web UI: accept (it becomes a task for the maintainer), dismiss, mark done, or note."""
    status = body.get("status") or ""
    if status and status not in ("new", "accepted", "done", "dismissed"):
        return 400, {"error": "status is new, accepted, done or dismissed"}
    with lock:
        item = feedback.get(iid)
        if item is None:
            return 404, {"error": f"no feedback {iid}"}
        change = {"id": iid, "updated": time.time()}
        if "note" in body:
            change["note"] = str(body.get("note") or "").strip()[:2000]
        if status:
            change["status"] = status
        if status == "accepted" and not item.get("task"):
            cwd = maintainer_folder()
            if not cwd:
                return 409, {"error": f"no folder is known for the peerlink maintainer ('{config.get('issue_agent')}'): start it once, or set issue_agent in Settings"}
            note = change.get("note", item.get("note", ""))
            msg = {"id": f"m{next_msg[0]}", "ts": time.time(), "from": "peerlink", "from_name": "peerlink", "from_host": HOSTNAME,
                   "from_cwd": "", "to_cwd": cwd, "from_space": "", "to_space": "",
                   "text": f"Feedback {iid} ({item['type']}, from {item.get('from_name') or 'an agent'}), accepted by your user"
                           + (f": {note}" if note else "") + f"\n\n{item['text']}",
                   "expects_reply": True, "reply_to": None, "hops": 0, "status": "queued", "v": 2}
            next_msg[0] += 1
            t = new_task(msg, {"id": "", "name": f"feedback {iid}", "cwd": ""})
            t["title"], t["feedback"] = f"{iid}: {task_title_of(item['text'])}"[:120], iid
            agent = queue_for_folder(cwd, msg)
            t["owner"], t["space"] = msg["to_name"], space_of(agent)[0] if agent else ""
            save_tasks()
            change["task"] = t["id"]
        item.update(change)
        append_log_to(ISSUES_PATH, change)
        out = dict(item)
    log("feedback", iid, "->", out["status"], out.get("task") or "")
    return 200, {"feedback": out}


def task_title_of(text):
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def append_log_to(path, entry):
    try:
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


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
    # A task is tracked by its own status below, not by whether its message was answered.
    recent = [m for m in messages if m.get("expects_reply") and not m.get("task") and now - m["ts"] < max_age and m["id"] not in answered]
    owes = [gist(m, "from_name") for m in recent if (m.get("to") in me or m.get("to_cwd") in me) and m.get("status") != "expired"]
    waiting = [gist(m, "to_name") for m in recent if m.get("from") in me or m.get("from_cwd") in me]
    def tgist(t, who):
        return {"id": t["id"], "with": who, "ago_s": round(now - t["created"]), "text": t["title"], "status": t["status"]}
    live = [t for t in tasks.values() if t["status"] in TASK_OPEN]
    mine = [tgist(t, t["requester"].get("name", "")) for t in live if t["owner_cwd"] and t["owner_cwd"] == rec.get("cwd")]
    asked = [tgist(t, t["owner"]) for t in live if t["requester"].get("id") == rec["id"] or (rec.get("cwd") and t["requester"].get("cwd") == rec.get("cwd"))]
    return {"owes": owes[-limit:], "waiting": waiting[-limit:], "tasks": mine[-limit:], "asked": asked[-limit:]}


def take_inbox(sid):
    with lock:
        box = inboxes.pop(sid, [])
        started = False
        for m in box:
            m["status"] = "delivered"
            m["delivered_ts"] = time.time()
            append_log({"id": m["id"], "status": "delivered", "delivered_ts": m["delivered_ts"]})
            t = tasks.get(m.get("task") or "")
            if t and t["status"] == "open":
                t["status"], t["updated"], started = "in_progress", time.time(), True
        if started:
            save_tasks()
        return box


# Tasks: work handed to an agent, tracked until its owner says done or failed. A task belongs to its owner's
# folder, so it outlives a /clear, a restart or a new session there, and whoever asked hears how it ended.
TASK_OPEN = ("open", "in_progress")
TASK_CLOSED = ("done", "failed", "cancelled")


def load_tasks():
    try:
        with open(TASKS_PATH) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    for t in saved:
        tasks[t["id"]] = t
        next_task[0] = max(next_task[0], int(t["id"][1:]) + 1)


def save_tasks():
    with open(TASKS_PATH + ".tmp", "w") as f:
        json.dump(list(tasks.values()), f, indent=1)
    os.replace(TASKS_PATH + ".tmp", TASKS_PATH)


def new_task(msg, requester, parent_task=""):
    """Open the task a kind=task message carries, before the message is recorded. Called with the lock held."""
    now = time.time()
    title = next((line.strip() for line in msg["text"].splitlines() if line.strip()), "")
    t = {"id": f"t{next_task[0]}", "title": re.sub(r"(?i)^task\s*:\s*", "", title)[:120], "text": msg["text"][:4000],
         "requester": requester, "owner_cwd": msg.get("to_cwd", ""), "owner": msg.get("to_name") or folder_name(msg.get("to_cwd", "")),
         "space": msg.get("to_space", ""), "status": "open", "created": now, "updated": now, "note": "",
         "msg": msg["id"], "parent": parent_task}
    next_task[0] += 1
    tasks[t["id"]] = t
    msg["kind"], msg["task"] = "task", t["id"]
    save_tasks()
    return t


def task_message(t, to_cwd, text, sender=None, reply_to=None, kind="message"):
    """A note about a task from its owner (or peerlink), queued for an agent's folder. Called with the lock held."""
    msg = {"id": f"m{next_msg[0]}", "ts": time.time(), "from": sender["id"] if sender else "peerlink",
           "from_name": display_name(sender) if sender else "peerlink", "from_host": HOSTNAME,
           "from_cwd": sender.get("cwd", "") if sender else "", "to_cwd": to_cwd,
           "from_space": space_of(sender)[0] if sender else "", "to_space": "",
           "text": text, "expects_reply": False, "reply_to": reply_to, "hops": 0, "status": "queued", "v": 2}
    if kind == "task":
        msg["kind"], msg["task"] = "task", t["id"]
    next_msg[0] += 1
    agent = queue_for_folder(to_cwd, msg)
    msg["to_space"] = space_of(agent)[0] if agent else ""
    return msg


def update_task(tid, body, web=False):
    """An owner marks its task in progress, done or failed; its requester can cancel it; the web UI can do any of
    that, reopen it, or give it to another folder."""
    status = body.get("status") or ""
    note = (body.get("note") or "").strip()[:2000]
    owner = (body.get("owner") or "").strip()
    with lock:
        t = tasks.get(tid)
        if t is None:
            return 404, {"error": f"no task {tid}"}
        if web:
            if status and status not in TASK_OPEN + TASK_CLOSED:
                return 400, {"error": "status is open, in_progress, done, failed or cancelled"}
        else:
            by = sessions.get(body.get("by") or "")
            if by is None:
                return 403, {"error": "unknown agent; register again"}
            owns = bool(t["owner_cwd"]) and by.get("cwd") == t["owner_cwd"]
            asked = by["id"] == t["requester"].get("id") or (by.get("cwd") and by.get("cwd") == t["requester"].get("cwd"))
            allowed = ({"in_progress", "done", "failed"} if owns else set()) | ({"cancelled"} if asked else set())
            if status not in allowed:
                if not allowed:
                    return 403, {"error": f"{tid} belongs to {t['owner']}; only its owner can update it (and whoever asked can cancel it)"}
                if not owns:
                    return 403, {"error": f"{tid} belongs to {t['owner']}: only it marks it done or failed; you can cancel it"}
                return 400, {"error": f"status is {' or '.join(sorted(allowed))}"}
        was = t["status"]
        handed = ""
        if web and owner and owner != t["owner_cwd"]:
            if t["owner_cwd"] and was in TASK_OPEN:
                task_message(t, t["owner_cwd"], f"Task {tid} was handed to {folder_name(owner)} in the web UI: stop work on it. {t['title']}")
            t["owner_cwd"], t["owner"] = owner, folder_name(owner)
            t.pop("user", None)
            status = status if status in TASK_OPEN else "open"
            handed = f"Task {tid} was handed to you"
        elif web and status in TASK_OPEN and was in TASK_CLOSED:
            handed = f"Task {tid} was reopened"
        if status:
            t["status"] = status
        if note:
            t["note"] = note
        t["updated"] = time.time()
        if handed:
            task_message(t, t["owner_cwd"], f"{handed}. When it's finished, call update_task with done or failed and a short note.\n\n{t['text']}", kind="task")
            t["owner"] = folder_name(t["owner_cwd"]) if not successor(t["owner_cwd"]) else display_name(successor(t["owner_cwd"]))
        closing = t["status"] in TASK_CLOSED and was not in TASK_CLOSED
        save_tasks()
        snapshot = dict(t)
    log("task", tid, was, "->", snapshot["status"], "(web)" if web else "")
    if closing:
        task_closed(snapshot, web)
    return 200, {"task": snapshot}


def task_closed(t, web=False):
    """Tell whoever asked how the task ended; Discord hears about every failure."""
    if t.get("feedback") in feedback and t["status"] == "done":
        with lock:
            change = {"id": t["feedback"], "status": "done", "updated": time.time()}
            feedback[t["feedback"]].update(change)
            append_log_to(ISSUES_PATH, change)
    mark = {"done": "✅ done", "failed": "❌ failed", "cancelled": "cancelled"}[t["status"]]
    line = f"Task {t['id']} {mark}: {t['title']}" + (f"\n{t['note']}" if t["note"] else "")
    req = t["requester"]
    d = req.get("discord")
    posts = []
    with lock:
        owner = successor(t["owner_cwd"])
        if t["status"] == "cancelled" and t["owner_cwd"]:
            # The owner stops; whoever cancelled it already knows.
            task_message(t, t["owner_cwd"], f"Task {t['id']} was cancelled{' in the web UI' if web else ' by ' + req.get('name', 'whoever asked')}: stop work on it. {t['title']}")
        elif d:
            posts.append((d["channel_id"], f"**{t['owner']}**: {line}", d["message_id"]))
            orig = next((m for m in messages if m["id"] == t["msg"]), None)
            if orig:
                set_reaction(orig, "✅" if t["status"] == "done" else "❌")
        elif req.get("cwd"):
            task_message(t, req["cwd"], line if owner else f"{t['owner']}: {line}", sender=owner, reply_to=t["msg"])
        if t["status"] == "failed" and not d and not t.get("user"):
            chans = [r for r in routes_for(t["owner_cwd"]) + routes_for(req.get("cwd", "")) if r["channel_id"] != "dm" and carries(r, "task")]
            if chans:
                posts.append((chans[0]["channel_id"], f"**{t['owner']}**: {line} (asked by {req.get('name') or 'an agent'})", None))
        # A #tasks channel follows every task of the folders in it, besides the posts above.
        already = {c for c, _, _ in posts} | ({d["channel_id"]} if d else set())
        for c in kind_channels("task", [t["owner_cwd"], req.get("cwd", "")]):
            if c not in already:
                posts.append((c, f"**{t['owner']}**: {line} (asked by {req.get('name') or 'an agent'})", None))
    if posts and discord.get("token") and discord_bot[0]:
        def post_all():
            for channel, text, ref in posts:
                try:
                    discordlink.post(discord["token"], channel, text, ref)
                except discordlink.DiscordError as err:
                    log("discord", "task post failed:", err)
        threading.Thread(target=post_all, daemon=True).start()


# Starting an agent: an agent asks, you approve (web UI or Discord, never an agent or a bot), and only then
# does the daemon start Claude Code in that folder, in tmux when it's there, else a Terminal window.
SPAWN_TTL_S = 24 * 3600


def load_spawns():
    try:
        with open(SPAWNS_PATH) as f:
            for x in json.load(f):
                spawns[x["id"]] = x
    except (OSError, ValueError, KeyError):
        pass


def save_spawns():
    with open(SPAWNS_PATH + ".tmp", "w") as f:
        json.dump(list(spawns.values()), f, indent=1)
    os.replace(SPAWNS_PATH + ".tmp", SPAWNS_PATH)


def spawn_told(sp, text):
    """Tell the agent that asked how its request went. Called with the lock held."""
    req = sp["requester"]
    if req.get("cwd"):
        task_message({"id": sp["id"]}, req["cwd"], text)


def request_spawn(body):
    by = sessions.get(body.get("by") or "")
    if by is None:
        return 403, {"error": "unknown agent; register again"}
    folder = os.path.abspath(os.path.expanduser(str(body.get("dir") or "")))
    purpose, prompt = str(body.get("purpose") or "").strip(), str(body.get("first_prompt") or "").strip()
    if not os.path.isdir(folder):
        return 400, {"error": f"{folder} isn't a folder on this machine; it has to exist already"}
    if not purpose or not prompt:
        return 400, {"error": "purpose and first_prompt are both required"}
    with lock:
        if any(x["dir"] == folder and x["status"] == "pending" for x in spawns.values()):
            return 409, {"error": f"a request to start an agent in {folder} is already waiting for your user"}
        running = successor(folder)
        n = max([int(i[1:]) for i in spawns] or [0]) + 1
        sp = {"id": f"s{n}", "dir": folder, "purpose": purpose[:1000], "prompt": prompt[:8000],
              "space": clean_space(body.get("space") or "") or space_of(by)[0],
              "requester": {"id": by["id"], "name": display_name(by), "cwd": by.get("cwd", "")},
              "status": "pending", "created": time.time(), "updated": time.time(), "note": ""}
        spawns[sp["id"]] = sp
        save_spawns()
        chans = [r for r in routes_for(by.get("cwd", "")) if r["channel_id"] != "dm" and carries(r, "message")]
    log("spawn asked", sp["id"], sp["requester"]["name"], folder)
    if chans and discord.get("token") and discord_bot[0]:
        try:
            sent = discordlink.post(discord["token"], chans[0]["channel_id"],
                                    f"**{sp['requester']['name']}** asks to start an agent in `{tilde_path(folder)}` (space {sp['space']}): {purpose}\nIts first prompt: {prompt[:600]}",
                                    None, discordlink.buttons(["Approve", "Deny"], f"ps:{sp['id']}"))
            with lock:
                sp["discord_out"] = {"channel_id": chans[0]["channel_id"], "message_id": str((sent[-1] or {}).get("id", "")) if sent else ""}
                save_spawns()
        except discordlink.DiscordError as err:
            log("discord", "spawn post failed:", err)
    note = f" An agent is already running there ({display_name(running)})." if running else ""
    return 200, {"id": sp["id"], "status": "pending", "note": note}


def tilde_path(p):
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p.startswith(home) else p


def find_claude():
    found = os.environ.get("PEERLINK_CLAUDE") or shutil.which("claude")
    for p in ([found] if found else []) + [os.path.expanduser("~/.local/bin/claude"), os.path.expanduser("~/.claude/local/claude"),
                                            "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]:
        if p and os.access(p, os.X_OK):
            return p
    return ""


def launch(sp):
    """Start Claude Code in the folder: (how, "") when it started, ("", why not) when it didn't."""
    claude = find_claude()
    if not claude:
        return "", "Claude Code (claude) isn't on this machine's PATH"
    tmux = shutil.which("tmux") or next((p for p in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux") if os.access(p, os.X_OK)), "")
    try:
        if tmux:
            name = "peerlink-" + re.sub(r"[^A-Za-z0-9_-]", "-", os.path.basename(sp["dir"]))[:30] + "-" + sp["id"]
            subprocess.run([tmux, "new-session", "-d", "-s", name, "-c", sp["dir"], claude], check=True, timeout=10, capture_output=True)
            return f"in tmux (tmux attach -t {name})", ""
        if sys.platform == "darwin":
            quote = lambda v: '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
            script = f'tell application "Terminal" to do script "cd " & quoted form of {quote(sp["dir"])} & " && " & quoted form of {quote(claude)}'
            subprocess.run(["osascript", "-e", script], check=True, timeout=15, capture_output=True)
            return "in a new Terminal window", ""
    except (OSError, subprocess.SubprocessError) as err:
        return "", f"could not start it: {err}"
    return "", "starting an agent needs tmux on this machine"


def decide_spawn(sid, approve, via):
    """Your answer to a spawn request. Only the web UI (your browser) and your own Discord presses call this."""
    with lock:
        sp = spawns.get(sid)
        if sp is None:
            return 404, {"error": f"no request {sid}"}
        if sp["status"] != "pending":
            return 409, {"error": f"{sid} is already {sp['status']}"}
        sp["status"], sp["updated"] = ("approved" if approve else "denied"), time.time()
        save_spawns()
    if approve:
        how, err = launch(sp)
        with lock:
            if err:
                sp["status"], sp["note"] = "failed", err
                spawn_told(sp, f"Your user approved {sid}, but the agent in {sp['dir']} didn't start: {err}")
            else:
                sp["status"], sp["note"] = "started", how
                spawn_spaces[sp["dir"]] = sp["space"]
                # The first prompt waits for the new agent's folder, as a message from whoever asked, so replies go back.
                req = sessions.get(sp["requester"]["id"]) or successor(sp["requester"]["cwd"])
                msg = {"id": f"m{next_msg[0]}", "ts": time.time(), "from": req["id"] if req else "peerlink",
                       "from_name": display_name(req) if req else sp["requester"]["name"], "from_host": HOSTNAME,
                       "from_cwd": sp["requester"]["cwd"], "to_cwd": sp["dir"], "from_space": sp["space"], "to_space": sp["space"],
                       "text": sp["prompt"], "expects_reply": True, "reply_to": None, "hops": 0, "status": "queued", "v": 2}
                next_msg[0] += 1
                queue_for_folder(sp["dir"], msg)
                spawn_told(sp, f"Your user approved {sid}: an agent is starting in {sp['dir']} {how}, and your first prompt is waiting for it ({msg['id']}).")
            save_spawns()
    else:
        with lock:
            spawn_told(sp, f"Your user denied {sid}, the agent you asked for in {sp['dir']}.")
    close_buttons(sp)
    log("spawn", sid, sp["status"], "via", via, sp["note"])
    return 200, {"spawn": dict(sp)}


def close_buttons(item):
    """Clear the buttons on a Discord post once its question is settled elsewhere."""
    out = item.get("discord_out") or {}
    if not out.get("message_id") or not discord.get("token"):
        return
    def edit():
        try:
            discordlink.call(discord["token"], "PATCH", f"/channels/{out['channel_id']}/messages/{out['message_id']}", {"components": []})
        except discordlink.DiscordError as err:
            log("discord", "could not clear buttons:", err)
    threading.Thread(target=edit, daemon=True).start()


def on_spawn_press(press):
    _, sid, idx = (press["custom_id"].split(":") + ["", ""])[:3]
    sp = spawns.get(sid)
    if not sp or sp["status"] != "pending":
        discordlink.answer_interaction(press["id"], press["token"], 4, {"content": f"{sid} is already {sp['status'] if sp else 'gone'}.", "flags": 64})
        return
    rows = (press["message"] or {}).get("components") or []
    for row in rows:
        for b in row.get("components", []):
            b["disabled"] = True
            b["style"] = 3 if b.get("custom_id") == press["custom_id"] else 2
    discordlink.answer_interaction(press["id"], press["token"], 7, {"components": rows})
    decide_spawn(sid, idx == "0", f"Discord ({press['author']})")


# Ending an agent: you ask (web UI, or "/kill <name>" on Discord, confirmed with a button), never an agent.
# Its plugin ends the session once the turn it's in finishes; a minute later, if it's still there, the daemon
# ends the process itself.
def terminate(sid, by):
    with lock:
        rec = sessions.get(sid)
        if rec is None or not is_live(rec):
            return 404, {"error": "that agent isn't running"}
        if rec.get("terminate"):
            return 200, {"ok": True, "already": True}
        rec["terminate"] = {"by": by, "ts": time.time()}
        entry = {"ts": time.time(), "agent": display_name(rec), "id": sid, "cwd": rec.get("cwd", ""), "by": by, "pid": rec.get("pid")}
    append_log_to(TERMINATIONS_PATH, entry)
    log("terminate", entry["agent"], "by", by)
    threading.Thread(target=hard_stop, args=(sid,), daemon=True).start()
    return 200, {"ok": True}


def hard_stop(sid, grace=60):
    time.sleep(grace)
    with lock:
        rec = sessions.get(sid)
        if rec is None or not is_live(rec):
            return
        pid = rec.get("pid")
    # Only a process that is Claude Code's: a stale pid may belong to something else by now.
    try:
        comm = subprocess.run(["ps", "-p", str(int(pid)), "-o", "comm="], capture_output=True, text=True, timeout=5).stdout.strip() if pid else ""
    except (OSError, ValueError, subprocess.SubprocessError):
        comm = ""
    if not re.search(r"claude|node", comm, re.I):
        log("terminate", sid, "still running, and its process can't be found to end it", repr(comm))
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
        time.sleep(5)
        os.kill(int(pid), 0)
        os.kill(int(pid), signal.SIGKILL)
    except OSError:
        pass
    with lock:
        sessions.pop(sid, None)
    log("terminate", sid, "process", pid, "ended")


def reaper():
    while True:
        time.sleep(10)
        now = time.time()
        with lock:
            for sid in [s for s, r in sessions.items() if now - r["last_seen"] > config["peer_ttl_s"] * 10]:
                sessions.pop(sid, None)
                expire(inboxes.pop(sid, []))
            for sp in [x for x in spawns.values() if x["status"] == "pending" and now - x["created"] > SPAWN_TTL_S]:
                sp["status"], sp["updated"] = "expired", now
                spawn_told(sp, f"Your request {sp['id']} to start an agent in {sp['dir']} expired: your user didn't answer within a day.")
                save_spawns()
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

    def discord_api(self, rest, method, q):
        bot = discord_bot[0]
        if rest == [] and method == "GET":
            token = discord.get("token", "")
            return self.reply(200, {
                "configured": bool(token), "token_hint": discordlink.hint(token), "app_id": discord.get("app_id", ""),
                "user": discord.get("user", ""), "invite_url": discordlink.invite_url(discord["app_id"]) if discord.get("app_id") else "",
                "enabled": discord.get("enabled", True),
                "state": bot.state if bot else ("off" if token else "unconfigured"), "error": bot.error if bot else "",
                "reads_messages": bool(bot and bot.reads_messages),
                "message_content_switch": discordlink.message_content_on(token) if token and q.get("check") else None,
                "routes": discord.get("routes", []), "dm": discord.get("dm", ""),
                "buttons": discord.get("buttons", True), "files_out": discord.get("files_out", True),
                "files_in": discord.get("files_in", True), "max_file_mb": discord.get("max_file_mb", 10),
                "trusted_bots": discord.get("trusted_bots", []),
            })
        if rest == [] and method == "PUT":
            b = self.body()
            if "token" in b:
                try:
                    info = discordlink.inspect_token(b["token"])
                except discordlink.DiscordError as err:
                    return self.reply(400, {"error": str(err)})
                discord.update({"token": b["token"].strip(), "app_id": info["app_id"], "user": info["user"], "enabled": True})
            if "enabled" in b:
                discord["enabled"] = bool(b["enabled"])
            if "routes" in b:
                discord["routes"] = [{"channel_id": str(r["channel_id"]), "master": r.get("master", ""),
                                      "members": [m for m in r.get("members", []) if m and m != r.get("master")],
                                      # Every kind ticked is the same as none: the channel carries everything.
                                      "kinds": [k for k in KINDS if k in (r.get("kinds") or [])] if set(r.get("kinds") or []) < set(KINDS) else []}
                                     for r in b["routes"] if r.get("channel_id")]
            if "dm" in b:
                discord["dm"] = b["dm"] or ""
            for k in ("buttons", "files_out", "files_in"):
                if k in b:
                    discord[k] = bool(b[k])
            if "max_file_mb" in b:
                discord["max_file_mb"] = max(1, min(500, float(b["max_file_mb"] or 10)))
            if "trusted_bots" in b:
                raw = b["trusted_bots"]
                ids = re.findall(r"\d{15,22}", raw if isinstance(raw, str) else " ".join(map(str, raw or [])))
                discord["trusted_bots"] = list(dict.fromkeys(ids))
                if bot:
                    bot.trusted_bots = set(discord["trusted_bots"])
            save_discord()
            if "token" in b or "enabled" in b:
                start_discord()
            return self.reply(200, {"ok": True})
        if rest == [] and method == "DELETE":
            discord.update({"token": "", "app_id": "", "user": ""})
            save_discord()
            start_discord()
            return self.reply(200, {"ok": True})
        if rest == ["channels"] and method == "GET":
            if not discord.get("token"):
                return self.reply(200, {"guilds": []})
            live_view = bot.channels_live() if bot else None
            try:
                return self.reply(200, {"guilds": live_view if live_view is not None else discordlink.channels_rest(discord["token"])})
            except discordlink.DiscordError as err:
                return self.reply(502, {"error": str(err)})
        if rest == ["test"] and method == "POST":
            channel = str(self.body().get("channel_id") or "")
            try:
                discordlink.post(discord["token"], channel, "👋 peerlink is connected. Messages here go to this channel's master agent; start with an agent's name and a colon to reach another one mapped here.")
            except discordlink.DiscordError as err:
                return self.reply(502, {"error": str(err)})
            return self.reply(200, {"ok": True})
        if rest == ["reconnect"] and method == "POST":
            start_discord()
            return self.reply(200, {"ok": True})
        return self.reply(404, {"error": "not found"})

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
                for k in ("cwd", "repo", "branch", "summary", "model", "plugin", "pid"):
                    if b.get(k) is not None:
                        rec[k] = b[k]
                # An agent you approved starting joins the space it was asked for.
                want = spawn_spaces.pop(rec.get("cwd", ""), "")
                if want and want != space_of(rec)[0] and not session_cfg(sid)["space"]:
                    config["sessions"].setdefault(sid, {})["space"] = want
                    rec.pop("_space", None)
                    save_config()
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
                if b.get("activity"):
                    discord_activity(rec, b["activity"])
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
            out = {"name": display_name(rec), "config": cfg, "inbox": inbox, "peer_count": others}
            if rec.get("terminate"):
                out["terminate"] = rec["terminate"]
            return self.reply(200, out)

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

        if rest == ["spawns"] and method == "POST":
            code, out = request_spawn(self.body())
            return self.reply(code, out)
        if rest == ["spawns"] and method == "GET":
            with lock:
                return self.reply(200, {"spawns": sorted(spawns.values(), key=lambda x: -x["created"])})
        if len(rest) == 3 and rest[0] == "sessions" and rest[2] == "terminate" and method == "POST":
            # Only you, in the browser: no agent ends another.
            if not browser or self.key_ok():
                return self.reply(403, {"error": "agents are ended from the web UI or Discord"})
            code, out = terminate(rest[1], "the web UI")
            return self.reply(code, out)
        if rest == ["inbox"] and method == "GET":
            return self.reply(200, {"items": inbox_items(), "user": USER_NAME})
        if rest == ["inbox"] and method == "POST":
            # Only you, in the browser: an agent holding the key must not close your tasks or answer for you.
            if not browser:
                return self.reply(403, {"error": "the Inbox is answered from the web UI or Discord"})
            code, out = inbox_act(self.body())
            return self.reply(code, out)
        if rest == ["tasks"] and method == "GET":
            with lock:
                return self.reply(200, {"tasks": sorted(tasks.values(), key=lambda t: -t["created"])[:500]})
        if len(rest) == 2 and rest[0] == "tasks" and method == "POST":
            code, out = update_task(rest[1], self.body(), web=bool(browser) and not self.key_ok())
            return self.reply(code, out)

        if rest == ["messages"]:
            if method == "GET":
                with lock:
                    return self.reply(200, {"messages": messages[-int(q.get("limit", ["100"])[0]):]})
            if method == "POST":
                code, res = send(self.body())
                return self.reply(code, res)

        if rest and rest[0] == "discord":
            return self.discord_api(rest[1:], method, q)

        if len(rest) == 2 and rest[0] == "issues" and method == "POST":
            code, out = update_feedback(rest[1], self.body())
            return self.reply(code, out)
        if rest == ["issues"]:
            if method == "GET":
                with lock:
                    items = sorted(feedback.values(), key=lambda i: i["ts"])[-int(q.get("limit", ["500"])[0]):]
                return self.reply(200, {"issues": items, "path": ISSUES_PATH})
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
    # A fresh interpreter in its own session, not a fork of this one: on macOS a forked child that hasn't exec'd
    # crashes the first time anything touches Objective-C, which an HTTPS call through urllib does (it brought
    # the daemon down when a Discord token was connected).
    out = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with open(os.devnull, "rb") as devnull:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve"], stdin=devnull, stdout=out, stderr=out,
                         start_new_session=True, close_fds=True)
    os.close(out)
    for _ in range(50):
        time.sleep(0.1)
        if health():
            break
    print(json.dumps({"running": bool(health()), "started": True}))


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
    load_tasks()
    load_feedback()
    load_spawns()
    load_dirs()
    file_pending_issues()
    load_discord()
    threading.Thread(target=reaction_worker, name="discord-reactions", daemon=True).start()
    start_discord()
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
        print(f"{i['id']:<5} {time.strftime('%m-%d %H:%M', time.localtime(i['ts']))}  {i.get('type', 'bug'):<4} {i.get('status', 'new'):<9} {i['from_name'] or '?'} ({i['space'] or '?'})  {i['text'][:100]}")
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
.steps{list-style:none;padding:0;margin:8px 0 16px;display:flex;flex-direction:column;gap:12px;max-width:820px}
.step{display:flex;gap:12px;align-items:flex-start;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.step-n{flex:none;width:24px;height:24px;border-radius:50%;border:1px solid var(--line);display:flex;align-items:center;justify-content:center;font-size:12px;color:var(--mut)}
.step.done .step-n{background:var(--ok);border-color:var(--ok);color:#fff}.step-body{flex:1;min-width:0}.step-t{font-weight:600;margin-bottom:4px}
.step .meta{color:var(--mut);font-size:13px;margin:4px 0 8px}.addline{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.addline .grow{flex:1;min-width:200px}
.dmap{width:100%;border-collapse:collapse;margin-top:6px}.dmap td,.dmap th{padding:6px 6px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
.kinds{display:flex;flex-direction:column;gap:2px}.kinds label{display:flex;gap:6px;align-items:center;white-space:nowrap;margin:0;line-height:1.35}.kinds input{margin:0}
.dmap th{font-size:12px;color:var(--mut);font-weight:500}.dmap select{width:100%}.chips{display:flex;flex-wrap:wrap;gap:4px;align-items:center}
.chip{display:inline-flex;gap:4px;align-items:center;font-size:12px;border:1px solid var(--line);border-radius:999px;padding:1px 4px 1px 8px}
.chip button{padding:0 5px;font-size:12px;line-height:1.4;background:transparent;color:var(--mut);border:0}
.dstate{display:inline-flex;align-items:center;gap:6px;font-size:13px;margin-bottom:8px}.dstate .dot{margin:0}
#channels a{color:var(--acc)}.dmap td:nth-child(2){min-width:190px}.dmap td:nth-child(1){min-width:150px}
#channels label.check{display:flex;gap:8px;align-items:flex-start;margin:6px 0;max-width:820px;font-size:13px}#channels label.check input{margin-top:3px}
.legend{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;font-size:13px;max-width:820px}
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
.task{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.task .mh{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;font-size:13px}.task .mh .when{margin-left:auto;color:var(--mut);font-size:12px}
.task .note{margin-top:6px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}.task .tact{margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}.task .tact select{width:auto}
.task.sub{margin-left:24px}button.danger{color:#c0392b;border-color:#c0392b}nav .count.hot:not(:empty){background:var(--acc);color:#fff;border-radius:999px;padding:1px 7px;font-weight:600}.task .ftext{margin-top:6px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;max-height:9em;overflow:auto}
.task .tact input{flex:1;min-width:160px}.tchip.new,.tchip.bug{color:var(--warn);border-color:var(--warn)}.tchip.accepted,.tchip.idea{color:var(--acc);border-color:var(--acc)}.tchip{font-size:11px;border-radius:999px;padding:1px 8px;border:1px solid var(--line);color:var(--mut)}
.tchip.open,.tchip.in_progress{color:var(--acc);border-color:var(--acc)}.tchip.done{color:#2e8b57;border-color:#2e8b57}.tchip.failed{color:#c0392b;border-color:#c0392b}
.ptabs{display:flex;gap:4px;margin-top:12px;border-bottom:1px solid var(--line)}.ptabs button{background:transparent;color:var(--mut);border:0;border-bottom:2px solid transparent;border-radius:0;padding:6px 10px}
.ptabs button.on{color:var(--fg);border-bottom-color:var(--acc)}.ptasks{margin-top:12px;display:flex;flex-direction:column;gap:8px}.ptasks .k{color:var(--mut)}
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
<a href="#inbox" data-nav="inbox">Inbox <span class="count hot" id="n-inbox"></span></a>
<a href="#spaces" data-nav="spaces">Spaces <span class="count" id="n-spaces"></span></a>
<a href="#messages" data-nav="messages">Messages <span class="count" id="n-msgs"></span></a>
<a href="#tasks" data-nav="tasks">Tasks <span class="count" id="n-tasks"></span></a>
<a href="#feedback" data-nav="feedback">Feedback <span class="count" id="n-feedback"></span></a>
<a href="#channels" data-nav="channels">Channels <span class="count" id="n-discord"></span></a>
<a href="#settings" data-nav="settings">Settings</a>
<a href="#controls" data-nav="controls">Controls</a>
</nav>
<main><div id="stalebar" style="display:none" class="small"></div><div id="offline">Can't reach the peerlink daemon. It restarts when an agent next needs it; this page reconnects by itself.</div><div id="signedout">This page is signed out. Run <span class="mono">/peerlink web</span> in any agent to open it again.</div>

<section id="agents"><h1>Agents</h1><p class="lead">Live Claude Code sessions as an org chart. Each box is a space; a lead (outlined in orange) sits in its parent's box with its team beneath it. The percentage is how full each agent's context is. An agent only sees and messages agents in its own space, and a lead also its parent's. Click an agent to see and change its settings.</p><div class="orgwrap"><div id="peers"></div><div id="detail"></div></div></section>

<section id="inbox"><h1>Inbox</h1><p class="lead">Everything waiting on you: agents asking to start another agent, tasks agents handed you, questions they're waiting on, and new feedback about peerlink. The longest waiting is first. On Discord, send <span class="mono">/inbox</span> for the same list and answer by number, like <span class="mono">1 done</span>.</p>
<div class="mlist" id="inboxlist"></div></section>

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

<section id="tasks"><h1>Tasks</h1><p class="lead">Work agents hand each other with <span class="mono">kind: task</span>, and tasks you post on Discord starting with <span class="mono">Task:</span>. A task stays open until its owner marks it done or failed, and whoever asked gets the owner's note. A task sent to a lead is the lead's to split up; its subtasks show under it.</p>
<div class="msgbar"><label class="small mut" for="tstatus">Show</label><select id="tstatus"><option value="live">Open and in progress</option><option value="done">Done</option><option value="failed">Failed</option><option value="cancelled">Cancelled</option><option value="">All</option></select>
<label class="small mut" for="towner">Owner</label><select id="towner"><option value="">Everyone</option></select>
<label class="small mut" for="tspace">Space</label><select id="tspace"><option value="">All spaces</option></select></div>
<div class="mlist" id="tasklist"></div></section>

<section id="feedback"><h1>Feedback</h1><p class="lead">Bugs and ideas agents report about peerlink itself, from any space. Accepting one makes it a task for the agent that works on peerlink; when that task is done, so is the feedback.</p>
<div class="msgbar"><label class="small mut" for="fstatus">Show</label><select id="fstatus"><option value="new">New</option><option value="accepted">Accepted</option><option value="done">Done</option><option value="dismissed">Dismissed</option><option value="">All</option></select>
<label class="small mut" for="ftype">Type</label><select id="ftype"><option value="">Bugs and ideas</option><option value="bug">Bugs</option><option value="idea">Ideas</option></select></div>
<div class="mlist" id="fblist"></div></section>

<section id="channels"><h1>Channels</h1>
<p class="lead">Places outside your terminal where you can talk to your agents.</p>
<h2>Discord</h2>
<p class="lead">Each Discord channel you map has a <b>master</b> agent, which gets your messages, and can have <b>members</b>, which post there too; start a message with a member's name and a colon to reach it instead. Only the bot's owner can talk to it.</p>
<div id="dstatus"></div>
<ol class="steps">
<li class="step" id="dstep1"><span class="step-n">1</span><div class="step-body"><div class="step-t">Create the bot and paste its token</div><div id="dstep1b"></div></div></li>
<li class="step" id="dstep2"><span class="step-n">2</span><div class="step-body"><div class="step-t">Invite it to your server</div><div id="dstep2b"></div></div></li>
<li class="step" id="dstep3"><span class="step-n">3</span><div class="step-body"><div class="step-t">Map channels to agents</div><div id="dstep3b"></div></div></li>
</ol>
<div id="dmore"></div>
</section>

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
   ${x.remote?'':`<div class="ptabs"><button class="${panelTab==='tasks'?'':'on'}" data-ptab="about">Agent</button><button class="${panelTab==='tasks'?'on':''}" data-ptab="tasks">Tasks${x.open_tasks?' · '+x.open_tasks:''}</button></div>`}
   ${panelTab==='tasks'&&!x.remote?panelTasks(x):`<div class="facts"><span class="k">Status</span><span>${x.busy?'working':'idle'}${x.muted?' · muted':''}</span>
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
    <div><button type="submit">Save</button> <span id="agentmsg" class="small"></span></div></form>
    <div class="threads"><div class="k small">End this agent</div><div class="small mut">It finishes the turn it's in, then its session ends. If it's still running a minute later, its process is stopped.</div>
    <div><button class="ghost small danger" data-terminate="${esc(x.id)}" ${x.terminating?'disabled':''}>${x.terminating?'Ending…':'Terminate'}</button></div></div>`}`}</div>`}
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
      ${x.kind==='announcement'?`<span class="chip reply">announcement · ${(x.recipients||[]).length} agents</span>`:''}${x.task?`<span class="chip reply">task ${esc(x.task)}</span>`:x.expects_reply?'<span class="chip">reply expected</span>':''}${x.reply_to?`<span class="chip reply">re ${esc(x.reply_to)}</span>`:''}${st}
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
  const [st,p,m,tk,fb,ib]=await Promise.all([api('/api/status'),api('/api/peers'),api('/api/messages?limit=100'),api('/api/tasks'),api('/api/issues'),api('/api/inbox')]);
  allTasks=tk.tasks||[];allFeedback=fb.issues||[];inbox=ib.items||[];
  const view=JSON.stringify([p.peers.map(({last_seen_s,...x})=>x),m.messages.map(x=>[x.id,x.status]),allTasks.map(t=>[t.id,t.status,t.owner,t.note]),allFeedback.map(f=>[f.id,f.status,f.note]),inbox.map(i=>i.id),sel,panelTab,cfg.sessions]);
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
  const members=n=>[...groups[n].filter(x=>!isLead(x,n)).sort(rank).sort((a,b)=>a.name.localeCompare(b.name)),
    ...(kids[n]||[]).filter(c=>c!==n).sort().map(c=>inside[c].peer)];
  const short=d=>{const t=tilde(d).split('/');return t.length>3?'…/'+t.slice(-2).join('/'):t.join('/')};
  const ctx=x=>x.context==null?'':`<span class="ctx${x.context>=80?' hi':''}" title="context window in use">${x.context}%</span>`;
  // A lead is told apart by its outline (and says which team on hover), so every card keeps its context in one place.
  const node=x=>`<button class="node${x.parent?' is-lead':''}${x.id===sel?' sel':''}" data-sel="${esc(x.id)}"${x.parent?` title="Leads the ${esc(x.space)} team"`:''}>
    <div class="nm"><span><span class="dot ${x.busy?'busy':''}"></span>${esc(x.name)}</span>${ctx(x)}</div>
    <div class="dir">${esc(short(x.cwd))}</div><div class="sum">${esc(x.summary)||'no summary yet'}</div>${x.open_tasks?`<div class="small" style="color:var(--acc)">${x.open_tasks} open task${x.open_tasks>1?'s':''}</div>`:''}${x.stale&&!x.remote?'<div class="stale">reload needed</div>':''}</button>`;
  // Every live agent gets its own card, two sessions in one folder included: a hidden one can't be found to message.
  const rank=(a,b)=>(b.busy-a.busy)||(a.last_seen_s-b.last_seen_s);
  const unit=(n,seen)=>{if(seen.has(n))return '';seen.add(n);
    const list=members(n),branches=list.some(x=>x.parent&&x.space!==n);
    const count=list.reduce((t,x)=>t+(x.parent&&x.space!==n?leads[x.space].length:1),0);
    const cols=list.map(x=>x.parent&&x.space!==n&&!seen.has(x.space)
      ?`<div class="ocol"><div class="orow stack">${leads[x.space].slice().sort(rank).map(node).join('')}</div><div class="down"></div>${unit(x.space,seen)}</div>`:`<div class="ocol">${node(x)}</div>`);
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
  livePeers=p.peers;lastMsgs=m.messages;renderMessages(lastMsgs);renderTasks();renderFeedback();renderInbox();
}
// The Inbox: one row per thing waiting on you, with the action it needs.
let inbox=[];
function renderInbox(){
  $('#n-inbox').textContent=inbox.length||'';
  if(document.activeElement?.closest?.('#inboxlist'))return;
  const label={approval:'wants a new agent',task:'task for you',question:'waiting on your answer',feedback:'feedback'};
  const btn=(a,t)=>`<button class="ghost small" data-iact="${a}">${t}</button>`;
  $('#inboxlist').innerHTML=inbox.map(i=>`<div class="task" data-ikind="${esc(i.kind)}" data-iid="${esc(i.id)}"><div class="mh"><span class="tchip ${i.kind==='task'||i.kind==='approval'?'open':i.kind==='question'?'new':esc(i.type)}">${label[i.kind]}</span><b>${esc(i.title)}</b><span class="when">${esc(i.id)} · waiting ${ago(i.since)}</span></div>
    <div class="small mut">from <b>${esc(i.who)}</b>${i.kind==='feedback'?' · '+esc(i.type):''}</div>
    ${(()=>{const more=i.text.trim().startsWith(i.title.trim())?i.text.trim().slice(i.title.trim().length).trim():i.text.trim();return more?`<div class="ftext">${esc(more)}</div>`:''})()}
    <div class="tact">${i.kind==='approval'?`${btn('approve','Approve and start it')}${btn('deny','Deny')}`:i.kind==='task'?`<input class="small" data-itext placeholder="Note for ${esc(i.who)} (optional)">${btn('done','Done')}${btn('failed',"Couldn't do it")}`
      :i.kind==='question'?`<input class="small" data-itext placeholder="Your answer">${btn('reply','Send')}${btn('dismiss','Dismiss')}`
      :`${btn('accepted','Accept')}${btn('dismissed','Dismiss')}`}</div></div>`).join('')
    ||'<div class="empty">Nothing is waiting on you.</div>'}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-iact]');if(!b)return;const row=b.closest('[data-iid]');
  const text=row.querySelector('[data-itext]')?.value||'';
  if(b.dataset.iact==='reply'&&!text.trim()){row.querySelector('[data-itext]').focus();return}
  if(b.dataset.iact==='approve'&&!confirm('Start a new Claude Code session in that folder?'))return;
  const r=await post('/api/inbox',{kind:row.dataset.ikind,id:row.dataset.iid,action:b.dataset.iact,text});
  if(r.error)alert(r.error);document.activeElement?.blur();refresh(true)});
document.addEventListener('click',async e=>{const b=e.target.closest('[data-terminate]');if(!b)return;
  const x=livePeers.find(p=>p.id===b.dataset.terminate);if(!confirm(`End ${x?x.name:'this agent'}? Its session closes after the turn it's in.`))return;
  const r=await post(`/api/sessions/${encodeURIComponent(b.dataset.terminate)}/terminate`);if(r.error)alert(r.error);refresh(true)});
document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.matches?.('#inboxlist [data-itext]')){e.preventDefault();e.target.closest('[data-iid]').querySelector('[data-iact]').click()}});
// Feedback: newest first; accepting one opens a task for peerlink's maintainer.
let allFeedback=[];
function renderFeedback(){
  $('#n-feedback').textContent=allFeedback.filter(f=>f.status==='new').length||'';
  if(document.activeElement?.closest?.('#fblist'))return;
  const st=$('#fstatus').value,ty=$('#ftype').value;
  const shown=allFeedback.filter(f=>(!st||f.status===st)&&(!ty||f.type===ty)).sort((a,b)=>b.ts-a.ts);
  const btn=(s,label)=>`<button class="ghost small" data-fact="${s}">${label}</button>`;
  $('#fblist').innerHTML=shown.map(f=>{const task=allTasks.find(t=>t.id===f.task);let [head,...rest]=f.text.split('\n');
    // A long first line is cut to a headline; the rest of it reads below.
    if(head.length>120){const cut=head.lastIndexOf(' ',120)>60?head.lastIndexOf(' ',120):120;rest=[head.slice(cut).trim(),...rest];head=head.slice(0,cut)+'…'}
    return `<div class="task" data-fb="${esc(f.id)}"><div class="mh"><span class="tchip ${esc(f.type)}">${esc(f.type)}</span><span class="tchip ${esc(f.status)}">${esc(f.status)}</span><b>${esc(head)}</b><span class="when">${esc(f.id)} · ${ago(f.ts)} ago</span></div>
      <div class="small mut">${esc(f.from_name||'an agent')}${f.space?' · '+esc(f.space):''} · plugin ${esc(f.plugin||'?')}, daemon ${esc(f.daemon||'?')}${task?` · task ${esc(task.id)} ${esc(TSTATE[task.status]||task.status)} with <b>${esc(task.owner)}</b>`:''}</div>
      ${rest.join('\n').trim()?`<div class="ftext">${esc(rest.join('\n').trim())}</div>`:''}
      <div class="tact"><input class="small" data-fnote placeholder="Note (optional)" value="${esc(f.note||'')}">
      ${f.status==='new'?btn('accepted','Accept')+btn('dismissed','Dismiss'):f.status==='accepted'?btn('done','Mark done')+btn('dismissed','Dismiss'):btn('new','Back to new')}</div></div>`}).join('')
    ||`<div class="empty">${allFeedback.length?'Nothing here.':'No feedback yet. Agents send it with report_issue.'}</div>`}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-fact]');if(!b)return;const row=b.closest('[data-fb]');
  const r=await post('/api/issues/'+encodeURIComponent(row.dataset.fb),{status:b.dataset.fact,note:row.querySelector('[data-fnote]').value});
  if(r.error)alert(r.error);document.activeElement?.blur();refresh(true)});
document.addEventListener('change',async e=>{const t=e.target;
  if(t.id==='fstatus'||t.id==='ftype'){renderFeedback();return}
  if(t.dataset.fnote!==undefined){const r=await post('/api/issues/'+encodeURIComponent(t.closest('[data-fb]').dataset.fb),{note:t.value});if(r.error)alert(r.error);t.blur();refresh(true)}});
// Tasks: one row each, its subtasks under it. The Tasks page and an agent's Tasks tab share the row.
let allTasks=[],panelTab='about';
const DKINDS=[['message','Messages'],['task','Tasks'],['announcement','Announcements']];
const TSTATE={open:'open',in_progress:'in progress',done:'done',failed:'failed',cancelled:'cancelled'};
const isLive=t=>t.status==='open'||t.status==='in_progress';
const ago=ts=>{const d=Date.now()/1000-ts;return d<90?Math.round(d)+'s':d<5400?Math.round(d/60)+'m':d<129600?Math.round(d/3600)+'h':Math.round(d/86400)+'d'};
function taskRow(t,sub){const kids=allTasks.filter(x=>x.parent===t.id);
  return `<div class="task${sub?' sub':''}" data-task="${esc(t.id)}"><div class="mh"><span class="tchip ${esc(t.status)}">${TSTATE[t.status]||esc(t.status)}</span><b>${esc(t.title)}</b><span class="when">${esc(t.id)} · ${ago(t.created)} ago</span></div>
    <div class="small mut">${esc(t.requester?.name||'?')} → <b>${esc(t.owner)}</b>${t.space?' · '+esc(t.space):''}${kids.length?` · ${kids.filter(k=>k.status==='done').length} of ${kids.length} subtasks done`:''}${!isLive(t)?' · closed '+ago(t.updated)+' ago':''}</div>
    ${t.note?`<div class="note">${esc(t.note)}</div>`:''}
    <div class="tact">${isLive(t)?'<button class="ghost small" data-tact="cancelled">Cancel</button>':'<button class="ghost small" data-tact="open">Reopen</button>'}${agentSelect('','data-treassign','Reassign to…')}</div></div>`}
// Open and in-progress first, then the rest, newest first within each.
const byLive=list=>list.slice().sort((a,b)=>(isLive(b)-isLive(a))||(b.created-a.created));
const withSubs=list=>{const ids=new Set(list.map(t=>t.id));return byLive(list.filter(t=>!t.parent||!ids.has(t.parent))).map(t=>taskRow(t,false)+byLive(list.filter(k=>k.parent===t.id)).map(k=>taskRow(k,true)).join('')).join('')};
function renderTasks(){
  const opts=(el,all,vals)=>{const keep=el.value;el.innerHTML=`<option value="">${all}</option>`+vals.map(n=>`<option ${n===keep?'selected':''}>${esc(n)}</option>`).join('')};
  opts($('#towner'),'Everyone',[...new Set(allTasks.map(t=>t.owner))].sort());
  opts($('#tspace'),'All spaces',[...new Set(allTasks.map(t=>t.space).filter(Boolean))].sort());
  const st=$('#tstatus').value,ow=$('#towner').value,sp=$('#tspace').value;
  const shown=allTasks.filter(t=>(st==='live'?isLive(t):!st||t.status===st)&&(!ow||t.owner===ow)&&(!sp||t.space===sp));
  $('#n-tasks').textContent=allTasks.filter(isLive).length||'';
  // Never redraw under an open reassign menu.
  if(document.activeElement?.matches?.('#tasklist [data-treassign]'))return;
  $('#tasklist').innerHTML=withSubs(shown)||`<div class="empty">${allTasks.length?'No tasks match.':'No tasks yet. An agent opens one with send_message and kind: task.'}</div>`}
function panelTasks(x){
  const mine=allTasks.filter(t=>t.owner_cwd&&t.owner_cwd===x.cwd),asked=allTasks.filter(t=>t.requester?.id===x.id||(x.cwd&&t.requester?.cwd===x.cwd));
  const sec=(title,list,none)=>`<div class="k small">${title}</div>${list.length?byLive(list).map(t=>taskRow(t,false)).join(''):`<div class="small mut">${none}</div>`}`;
  return `<div class="ptasks">${sec('Owns',mine,'No tasks for this agent.')}${sec('Asked for',asked,'It hasn\'t handed out any tasks.')}</div>`}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-ptab],[data-tact]');if(!b)return;
  if(b.dataset.ptab){panelTab=b.dataset.ptab;refresh(true);return}
  const id=b.closest('[data-task]').dataset.task;
  if(b.dataset.tact==='cancelled'&&!confirm(`Cancel task ${id}? Its owner is told to stop.`))return;
  const r=await post('/api/tasks/'+encodeURIComponent(id),{status:b.dataset.tact});if(r.error)alert(r.error);refresh(true)});
document.addEventListener('change',async e=>{const t=e.target;
  if(['tstatus','towner','tspace'].includes(t.id)){renderTasks();return}
  if(t.dataset.treassign!==undefined&&t.value){const id=t.closest('[data-task]').dataset.task;
    const r=await post('/api/tasks/'+encodeURIComponent(id),{owner:t.value});if(r.error)alert(r.error);t.blur();refresh(true)}});
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
// -- Discord: the setup walkthrough (a reference bot's three steps) and the channel-to-agent mapping --------
let dState=null,dGuilds=null,dDraft=null,dBusy=false;
const agentFolders=()=>{const m=new Map();for(const p of livePeers)if(!p.remote&&!m.has(p.cwd))m.set(p.cwd,{cwd:p.cwd,name:p.name,space:p.space});return [...m.values()].sort((a,b)=>a.name.localeCompare(b.name))};
const folderLabel=cwd=>{const a=agentFolders().find(x=>x.cwd===cwd);return a?`${a.name} · ${a.space}`:`${(cfg.folder_names||{})[cwd]||cwd.split('/').filter(Boolean).pop()||cwd} (not running)`};
function agentSelect(val,attrs,placeholder){const opts=agentFolders();const extra=val&&!opts.some(o=>o.cwd===val)?[{cwd:val}]:[];
  return `<select ${attrs}><option value="">${placeholder}</option>${[...opts,...extra].map(o=>`<option value="${esc(o.cwd)}" ${o.cwd===val?'selected':''}>${esc(folderLabel(o.cwd))}</option>`).join('')}</select>`}
function channelSelect(val,attrs){const gs=dGuilds||[];
  return `<select ${attrs}><option value="">Pick a channel…</option>${gs.map(g=>`<optgroup label="${esc(g.name)}">${g.channels.map(c=>`<option value="${esc(c.id)}" ${c.id===val?'selected':''}>#${esc(c.name)}</option>`).join('')}</optgroup>`).join('')}</select>`}
const chanName=id=>{for(const g of dGuilds||[])for(const c of g.channels)if(c.id===id)return '#'+c.name;return 'channel '+id};
async function loadDiscord(check){
  dState=await api('/api/discord'+(check?'?check=1':''));
  if(dState.configured&&dGuilds===null){const r=await api('/api/discord/channels');dGuilds=r.guilds||[]}
  if(!dDraft)dDraft={routes:JSON.parse(JSON.stringify(dState.routes||[])),dm:dState.dm||''};
  renderDiscord()}
function renderDiscord(){if(!dState)return;const d=dState,invited=(dGuilds||[]).length>0,mapped=(d.routes||[]).some(r=>r.master);
  $('#n-discord').textContent=d.state==='online'?'on':'';
  const label={online:'Online',connecting:'Connecting…',error:'Error',off:'Turned off',unconfigured:'Not set up'}[d.state]||d.state;
  $('#dstatus').innerHTML=d.configured?`<div class="dstate"><span class="dot ${d.state==='online'?'':'busy'}"></span><b>${esc(label)}</b>${d.user?` as ${esc(d.user)}`:''}${d.error?` · <span class="err">${esc(d.error)}</span>`:''}</div>`:'';
  $('#dstep1').classList.toggle('done',d.configured);$('#dstep2').classList.toggle('done',d.configured&&invited);$('#dstep3').classList.toggle('done',mapped);
  $('#dstep1b').innerHTML=d.configured?`<p class="meta">Token <span class="mono">${esc(d.token_hint)}</span> saved. <button class="ghost small" data-dact="forget">Disconnect</button></p>`
   :`<p class="meta">On the <a href="https://discord.com/developers/applications" target="_blank" rel="noreferrer">Developer Portal ↗</a>, click <b>New Application</b> and give it a name. Open <b>Bot</b>, click <b>Reset Token</b>, and copy it. Optional: on the same page, turn on <b>Message Content Intent</b> so you can talk to it without @mentioning it every time.</p>
     <form class="addline" id="dtoken"><input type="password" class="mono grow" autocomplete="off" spellcheck="false" placeholder="Bot token" name="token"><button type="submit" ${dBusy?'disabled':''}>${dBusy?'Checking…':'Connect'}</button></form><p class="small err" id="dtokerr"></p>`;
  $('#dstep2b').innerHTML=!d.configured?'':`<p class="meta">${invited?`In ${dGuilds.map(g=>esc(g.name)).join(', ')}. `:`Pick your server on Discord's page, then come back here. This step completes on its own. `}
    ${d.invite_url?`<a href="${esc(d.invite_url)}" target="_blank" rel="noreferrer">${invited?'Invite to another server ↗':'<button>Invite the bot ↗</button>'}</a>`:''}</p>`;
  if(!d.configured||!invited){$('#dstep3b').innerHTML='';$('#dmore').innerHTML='';return}
  const rows=dDraft.routes.map((r,i)=>`<tr>
    <td>${channelSelect(r.channel_id,`data-droute="${i}" data-dfield="channel_id"`)}</td>
    <td>${agentSelect(r.master,`data-droute="${i}" data-dfield="master"`,'Pick its master…')}</td>
    <td><div class="chips">${(r.members||[]).map((m,j)=>`<span class="chip">${esc(folderLabel(m))}<button title="Remove" data-dunmember="${i}:${j}">✕</button></span>`).join('')}
      ${agentSelect('',`data-daddmember="${i}"`,'+ add member')}</div></td>
    <td><div class="kinds">${DKINDS.map(([k,l])=>`<label class="small"><input type="checkbox" data-dkind="${i}:${k}" ${!(r.kinds||[]).length||r.kinds.includes(k)?'checked':''}> ${l}</label>`).join('')}</div></td>
    <td style="white-space:nowrap"><button class="ghost small" data-dtest="${esc(r.channel_id)}" ${r.channel_id&&d.state==='online'?'':'disabled'}>Send a test</button> <button class="ghost small" data-dremove="${i}" title="Remove this channel">✕</button></td></tr>`).join('');
  $('#dstep3b').innerHTML=`<p class="meta">Give each channel a master, which gets your messages. Members can post in the channel too, and you reach one by starting with its name and a colon (<span class="mono">storefront: how's the build?</span>). One agent can be in several channels. Agents are kept by folder, so a mapping survives restarts and <span class="mono">/clear</span>.</p>
    <table class="dmap"><thead><tr><th>Channel</th><th>Master</th><th>Members</th><th>Carries</th><th></th></tr></thead><tbody>${rows||'<tr><td colspan="5" class="mut small">No channels mapped yet.</td></tr>'}</tbody></table>
    <p class="small mut" style="margin-top:6px">A channel can carry only some kinds. One set to tasks alone, a #tasks, turns everything you post there into a task for its master, and shows every task its agents hand out or finish. One set to announcements alone posts what agents announce to the master's space, and what you post there is announced to that space. Elsewhere, start a post with <span class="mono">Task:</span> or <span class="mono">Announcement:</span>.</p>
    <div class="addline" style="margin-top:8px"><button class="ghost" data-dact="addroute">+ Map a channel</button><button class="ghost" data-dact="refresh" title="Look for new servers and channels">↻ Refresh channels</button></div>
    <div class="addline" style="margin-top:12px"><label class="small mut">Direct messages to the bot go to</label>${agentSelect(dDraft.dm,'data-ddm','Nobody (ignored)')}</div>
    <div class="addline" style="margin-top:12px"><button data-dact="save">Save mapping</button><span class="small" id="dsaved"></span></div>`;
  const reading=d.reads_messages?`<p class="meta">Every message you write in a mapped channel goes to its master; DMs never need a mention.</p>`
    :d.message_content_switch?`<p class="meta">The Message Content switch is on in Discord, but the bot connected before it was. <button class="ghost small" data-dact="reconnect">Reconnect now</button></p>`
    :`<p class="meta">${d.message_content_switch===false?'<b>Discord says the Message Content switch is off.</b> ':''}@mention the bot each time, or DM it. To talk without @mentioning, turn on <b>Message Content Intent</b> on the <a href="${d.app_id?`https://discord.com/developers/applications/${esc(d.app_id)}/bot`:'https://discord.com/developers/applications'}" target="_blank" rel="noreferrer">Bot page ↗</a> (under Privileged Gateway Intents) and save. <button class="ghost small" data-dact="check">I turned it on</button></p>`;
  $('#dmore').innerHTML=`<h2>Talking to your agents</h2>${reading}
    <p class="meta">While an agent works on your message, a reaction on it shows where it's at:</p>
    <div class="legend"><span>👀</span><span>delivered to the agent</span><span>🤔</span><span>thinking</span><span>🛠️</span><span>using tools</span><span>❓</span><span>asked you something</span><span>✅</span><span>answered</span></div>
    <p class="meta" style="margin-top:12px">An agent can also message you first, and its posts are labelled with its name. If an agent isn't running, your message waits up to an hour for it. Links in an agent's message work as usual.</p>
    <h2>What agents can send</h2>
    <label class="check"><input type="checkbox" data-dopt="buttons" ${d.buttons?'checked':''}> <span><b>Buttons</b> for choices: tap one and it goes back to the agent as your answer. Off, the choices are a numbered list you reply to.</span></label>
    <label class="check"><input type="checkbox" data-dopt="files_out" ${d.files_out?'checked':''}> <span><b>Files from agents:</b> an agent can attach files from this machine to its messages.</span></label>
    <label class="check"><input type="checkbox" data-dopt="files_in" ${d.files_in?'checked':''}> <span><b>Files to agents:</b> what you attach on Discord is saved here, in <span class="mono">~/.peerlink/files/discord</span>, and the agent gets the paths.</span></label>
    <div class="addline" style="margin-top:8px"><label class="small mut" for="dmaxmb">Largest file, either way</label><input id="dmaxmb" type="number" min="1" max="500" step="1" value="${esc(d.max_file_mb)}" style="width:90px" data-dopt="max_file_mb"> <span class="small mut">MB (Discord allows 10 MB without Nitro)</span></div>
    <h2>Bots that can post to agents</h2>
    <p class="meta">Messages from other bots are ignored, except from bots listed here by user ID (comma separated; in Discord, turn on Developer Mode, then right-click the bot and Copy User ID). Their posts reach agents the way yours do, labelled with the bot's name, so list only bots you control. This bot's own posts never do.</p>
    <div class="addline"><input id="dbots" class="mono" placeholder="e.g. 1562000000000000000" value="${esc((d.trusted_bots||[]).join(', '))}" data-dbots style="flex:1;min-width:0"></div>
    <h2>Bot</h2>
    <label class="check"><input type="checkbox" data-dact="enabled" ${d.enabled?'checked':''}> Bot running</label>`}
document.addEventListener('submit',async e=>{if(e.target.id!=='dtoken')return;e.preventDefault();const t=e.target.token.value.trim();if(!t)return;
  dBusy=true;renderDiscord();const r=await api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({token:t})});dBusy=false;
  if(r.error){renderDiscord();$('#dtokerr').textContent=r.error;return}dGuilds=null;dDraft=null;await loadDiscord()});
document.addEventListener('change',e=>{const t=e.target;if(!dDraft)return;
  if(t.dataset.droute!==undefined){dDraft.routes[+t.dataset.droute][t.dataset.dfield]=t.value;renderDiscord()}
  if(t.dataset.daddmember!==undefined&&t.value){const r=dDraft.routes[+t.dataset.daddmember];r.members=r.members||[];if(t.value!==r.master&&!r.members.includes(t.value))r.members.push(t.value);renderDiscord()}
  if(t.dataset.ddm!==undefined){dDraft.dm=t.value}
  if(t.dataset.dkind){const [i,k]=t.dataset.dkind.split(':');const r=dDraft.routes[+i];
    let ks=(r.kinds&&r.kinds.length?r.kinds:DKINDS.map(x=>x[0])).filter(x=>x!==k);if(t.checked)ks.push(k);
    // A channel carries something: unticking the last kind leaves it ticked.
    r.kinds=ks.length?DKINDS.map(x=>x[0]).filter(x=>ks.includes(x)):r.kinds;renderDiscord()}
  if(t.dataset.dopt){const v=t.type==='checkbox'?t.checked:Number(t.value);api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({[t.dataset.dopt]:v})}).then(()=>loadDiscord())}
  if(t.dataset.dbots!==undefined)api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({trusted_bots:t.value})}).then(()=>loadDiscord());
  if(t.dataset.dact==='enabled')api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({enabled:t.checked})}).then(()=>loadDiscord())});
document.addEventListener('click',async e=>{const t=e.target.closest('button');if(!t||!dDraft&&!t.dataset.dact)return;
  if(t.dataset.dremove!==undefined){dDraft.routes.splice(+t.dataset.dremove,1);renderDiscord()}
  if(t.dataset.dunmember){const [i,j]=t.dataset.dunmember.split(':').map(Number);dDraft.routes[i].members.splice(j,1);renderDiscord()}
  if(t.dataset.dtest){const r=await post('/api/discord/test',{channel_id:t.dataset.dtest});t.textContent=r.error?'Failed':'Sent ✓';if(r.error)alert(r.error)}
  const a=t.dataset.dact;if(!a)return;
  if(a==='addroute'){dDraft.routes.push({channel_id:'',master:'',members:[]});renderDiscord()}
  if(a==='refresh'){const r=await api('/api/discord/channels');dGuilds=r.guilds||[];renderDiscord()}
  if(a==='forget'&&confirm('Disconnect the Discord bot? The channel mapping is kept.')){await api('/api/discord',{method:'DELETE'});dGuilds=null;await loadDiscord()}
  if(a==='reconnect'){await post('/api/discord/reconnect');setTimeout(()=>loadDiscord(),2000)}
  if(a==='check')await loadDiscord(true);
  if(a==='save'){const bad=dDraft.routes.find(r=>!r.channel_id||!r.master);if(bad){$('#dsaved').className='small err';$('#dsaved').textContent='Every row needs a channel and a master.';return}
    await api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({routes:dDraft.routes,dm:dDraft.dm})});dState=null;dDraft=null;await loadDiscord();
    $('#dsaved').className='small ok';$('#dsaved').textContent='Saved'}});
// Invited in another tab, or back from the Developer Portal: look again when this tab gets focus.
window.addEventListener('focus',()=>{if(location.hash==='#channels'&&dState?.configured){dGuilds=null;loadDiscord(true)}});
window.addEventListener('hashchange',()=>{if(location.hash==='#discord')location.hash='#channels';if(location.hash==='#channels')loadDiscord()});

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
show();loadCfg().then(()=>refresh(true)).then(()=>{if(location.hash==='#discord')location.hash='#channels';else if(location.hash==='#channels')loadDiscord()}).catch(()=>{misses=1;$('#offline').style.display='block'}).finally(()=>setTimeout(tick,2000));
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
