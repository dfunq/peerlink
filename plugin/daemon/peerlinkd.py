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

VERSION = "0.8.35"
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
SCHEDULES_PATH = os.path.join(HOME, "schedules.json")
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

# The default guidance's clear-between-tasks line, left out when that setting is off.
CLEAR_RULE = """\
- Keep your context small: every turn re-reads all of it, and a large one \
drains your user's usage limit fast. When you finish a task (or the work \
you were asked for) and have reported the result, save what's worth \
keeping, such as decisions, gotchas and where things are, to your memory \
or the project's notes, then call clear_context with a short handoff.
"""

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
{clear_rule}- A peerlink tool added after your session started (clear_context, \
schedule) may be listed only as a deferred tool: load it with ToolSearch, \
"select:mcp__peerlink__<name>", then call it.
- To tell your whole space something everyone needs (a shared API change, \
a freeze), send_message with kind "announcement" and to "space". Nobody \
replies, so keep them rare.
- When work belongs in a folder no agent is running in, request_spawn asks \
your user to start one there; nothing starts without their approval.
- To do something later or on a repeat, use the schedule tool, not \
CronCreate or /loop: the daemon keeps it after your session or terminal \
closes, and starts an agent in the folder if none is running.
- When you need your user to do something only they can (a login, an \
approval, a decision), send_message to "user": kind "task" for something to \
do, expects_reply for a question. It waits in their Inbox, and on Discord \
when you have a channel there; their answer comes back to you.
- If peerlink itself misbehaves (a reply that never comes, a message that \
reached the wrong agent, an error from these tools), call report_issue with \
the message ids and agent names, then carry on with your task. When \
peerlink gets in your way or could work better, report_issue with type \
"idea".""".replace("{clear_rule}", CLEAR_RULE)

DEFAULT_CONFIG = {
    "poll_interval_ms": 3000,
    "peer_ttl_s": 30,
    "auto_wake_idle": True,
    "inject_while_busy": True,
    "wake_for": "needed",  # "needed": only questions, tasks, your messages and answers to an agent's question start a turn; "all": every message
    "wake_delay_s": 15,    # an idle agent waits this long after a message from another agent, so ones close together share a turn
    "max_hops": 6,
    "rate_limit_per_min": 12,
    "announce_per_hour": 6,  # announcements each agent may send: each one reaches a whole space
    "max_message_chars": 8000,
    "guidance": "",
    "issue_agent": "peerlink",
    "roster_in_prompt": True,
    "auto_summary": True,
    "auto_reload": True,
    "clear_between_tasks": True,  # the default guidance asks agents to save notes and clear their context after each task
    "clear_nudge_tokens": 400000,  # past this many tokens of context, an agent is asked to save and clear at its next stopping point; 0 is off
    "summary_model": "haiku",
    "sessions": {},
    "folder_spaces": {},  # folder -> the space you put it in from the web UI; wins over its .peerlink
    "space_parents": {},  # space -> the space it sits inside, set on the Spaces page; wins over parent: lines
    "folder_names": {},
    "remotes": [],
    "listen_host": "127.0.0.1",
    "token": "",
}

lock = threading.RLock()
sessions = {}   # id -> session record
inboxes = {}    # id -> [message]
# Messages handed to an agent that hasn't said they reached its session yet: id -> {message id: (message, when handed)}.
# Handed again until acked, so one lost in a plugin reload or a prompt that never started a turn still arrives.
handed = {}
REDELIVER_S = float(os.environ.get("PEERLINK_REDELIVER_S", 45))
HANDED_MAX_AGE_S = 86400
messages = []   # recent log, newest last
sent_times = {} # sender id -> [timestamps]
next_msg = [1]
tasks = {}      # id -> task, see new_task()
feedback = {}   # id -> a bug report or idea about peerlink, see report_issue()
next_task = [1]
announce_times = {}  # agent id -> when it last announced, for announce_per_hour
spawns = {}          # id -> a request to start an agent, see request_spawn()
schedules = {}       # id -> a prompt the daemon sends a folder's agent later or on a repeat, see create_schedule()
# When an agent last ran in each folder. An implicit lead keeps its space this long after its folder goes quiet,
# counted from the daemon's start too, so agents reconnecting one by one after a restart don't each take it.
folder_seen = {}
LEAD_GRACE_S = 600
DAEMON_START = time.time()
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


# Settings that must be at least 1: zero would reap every agent at once or block every message.
CONFIG_MIN_1 = {"poll_interval_ms", "peer_ttl_s", "max_hops", "rate_limit_per_min", "max_message_chars"}


def config_value(name, value):
    """(value, None) with the value as the setting's own type, or (None, why it can't be)."""
    want = DEFAULT_CONFIG[name]
    if isinstance(want, bool):
        if isinstance(value, str) and value.lower() in ("true", "false", "on", "off", "yes", "no"):
            value = value.lower() in ("true", "on", "yes")
        return (value, None) if isinstance(value, bool) else (None, f"{name} is true or false")
    if isinstance(want, int):
        try:
            value = int(float(value))
        except (TypeError, ValueError):
            return None, f"{name} is a number"
        floor = 1 if name in CONFIG_MIN_1 else 0
        return (value, None) if value >= floor else (None, f"{name} is at least {floor}")
    if isinstance(want, str):
        return (value, None) if isinstance(value, str) else (None, f"{name} is text")
    return (value, None) if isinstance(value, type(want)) else (None, f"{name} is a {type(want).__name__}")


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
    for k in DEFAULT_CONFIG:
        if config_value(k, merged[k])[1]:  # a bad value saved by an older daemon: back to the default
            merged[k] = DEFAULT_CONFIG[k]
    if merged.get("guidance") == OLD_GUIDANCE_V1:  # an untouched old default follows the new one
        merged["guidance"] = ""
    config.clear()
    config.update(merged)
    save_config()


def save_config():
    tmp = f"{CONFIG_PATH}.{os.getpid()}.tmp"  # daemons starting together each write their own
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def session_cfg(sid):
    return {"nickname": "", "muted": False, "auto_wake": None, **config["sessions"].get(sid, {})}


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
        "leads": leads(rec) if rec else "",
        "also": also_in(rec) if rec else [],
        "role": role_of(rec) if rec else "",
        # The space this agent's space sits inside, for members too (a lead's `parent` is the same).
        "space_parent": space_parent(space) if rec else "",
        "lead": lead,
        "children": children,
        "discord": routes_for(rec.get("cwd", "")) if rec and discord.get("token") else [],
        "poll_interval_ms": config["poll_interval_ms"],
        "auto_wake_idle": config["auto_wake_idle"] if s["auto_wake"] is None else s["auto_wake"],
        "inject_while_busy": config["inject_while_busy"],
        "wake_delay_s": config.get("wake_delay_s", 15),
        "guidance": config["guidance"] or (DEFAULT_GUIDANCE if config.get("clear_between_tasks", True) else DEFAULT_GUIDANCE.replace(CLEAR_RULE, "")),
        "clear_nudge_tokens": config.get("clear_nudge_tokens", 400000),
        "muted": s["muted"],
        "roster_in_prompt": config["roster_in_prompt"],
        "auto_summary": config["auto_summary"],
        "auto_reload": config.get("auto_reload", True),
        "summary_model": config["summary_model"],
    }


def clean_space(name):
    return re.sub(r"[^a-z0-9._-]+", "-", str(name or "").strip().lower()).strip("-")[:40]


def find_space_file(cwd):
    """The nearest .peerlink at or above cwd that names a space. One with only a role doesn't count: it describes
    its folder's agent, not where it sits. The home folder itself never counts: ~/.peerlink is ours."""
    home = os.path.expanduser("~")
    d = os.path.abspath(cwd) if cwd else ""
    while d and d != home and d != os.path.dirname(d):
        path = os.path.join(d, SPACE_FILE)
        if os.path.isfile(path) and read_space_file(path):
            return path
        d = os.path.dirname(d)
    return None


ROLE_MAX = 1500


def role_of(rec):
    """What this agent is there for: the role you set for it in the web UI, else the folder's."""
    return session_cfg(rec.get("id", "")).get("role") or folder_role(rec)


def folder_role(rec):
    """The `role:` lines of the .peerlink in the agent's folder (not one above, which describes another agent): the
    role of every agent there that has none of its own. Read again at most every SPACE_CACHE_S, like the space."""
    now = time.time()
    cached = rec.get("_role")
    if cached and now - cached[0] < SPACE_CACHE_S:
        return cached[1]
    role = ""
    try:
        with open(os.path.join(rec.get("cwd") or "/nonexistent", SPACE_FILE)) as f:
            role = "\n".join(m.group(1).strip() for m in (re.match(r"\s*role\s*[:=](.*)", l, re.I) for l in f) if m).strip()[:ROLE_MAX]
    except OSError:
        pass
    rec["_role"] = (now, role)
    return role


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


def space_of(rec):
    """(space, source, where): the space you put this agent's folder in ('set'), else the nearest .peerlink ('file'),
    else default. A .peerlink only places folders you haven't."""
    now = time.time()
    cached = rec.get("_space")
    if cached and now - cached[0] < SPACE_CACHE_S:
        return cached[1]
    cwd = rec.get("cwd", "")
    found = (DEFAULT_SPACE, "default", "")
    chosen = clean_space((config.get("folder_spaces") or {}).get(cwd))
    path = find_space_file(cwd)
    if chosen:
        found = (chosen, "set", cwd)
    elif path and read_space_file(path):
        found = (read_space_file(path), "file", path)
    # A parent: line in the .peerlink names the space this one sits inside, unless you set it on the Spaces page.
    parent = read_space_file(found[2], "parent") if found[1] == "file" else ""
    rec["_space"] = (now, found, "" if parent == found[0] else parent)
    return found


def space_lead(space):
    """(folder, how) of a space's lead: 'set' by you, else 'only' (the first agent alone in it, see
    note_lone_leads); ('', '') when it has none."""
    chosen = (config.get("space_leads") or {}).get(space) or {}
    return (chosen["cwd"], chosen.get("how") == "set" and "set" or "only") if chosen.get("cwd") else ("", "")


def leads(rec):
    """How rec leads its space ('set' or 'only'), or '' when it doesn't."""
    folder, how = space_lead(space_of(rec)[0])
    return how if folder and rec.get("cwd") == folder else ""


def space_parent(space):
    """The space `space` sits inside: as you set on the Spaces page, else a parent: line in one of its agents'
    .peerlink; '' at the top."""
    parents = config.get("space_parents") or {}
    if space in parents:
        return parents[space]
    for r in sessions.values():
        if is_live(r) and space_of(r)[0] == space and r["_space"][2]:
            return r["_space"][2]
    return ""


def parent_of(rec):
    """The parent space this agent speaks for, if it leads a space nested in another; else ''."""
    return space_parent(space_of(rec)[0]) if leads(rec) else ""


def note_lone_leads():
    """An agent alone in its space leads it, and keeps leading when others join, until you pick another.
    The default space is where unsorted agents land, not a team, so it has no lead. Called with the lock held."""
    folders, now = {}, time.time()
    for r in sessions.values():
        if is_live(r):
            folders.setdefault(space_of(r)[0], set()).add(r.get("cwd", ""))
            folder_seen[r.get("cwd", "")] = now
    for space, cwds in folders.items():
        if space == DEFAULT_SPACE or len(cwds) != 1 or "" in cwds:
            continue
        cwd = next(iter(cwds))
        # A lead you set always wins. An implicit lead elsewhere gives way once its folder has been quiet a while, or
        # at once when its agent has moved to another space.
        folder, how = space_lead(space)
        if folder == cwd or how == "set":
            continue
        moved = any(is_live(r) and r.get("cwd") == folder for r in sessions.values())
        if folder and not moved and now - folder_seen.get(folder, DAEMON_START) < LEAD_GRACE_S:
            continue
        config.setdefault("space_leads", {})[space] = {"cwd": cwd, "how": "only", "ts": time.time()}
        save_config()
        log("lead", space, "->", cwd, "(only agent)")


def set_lead(space, cwd):
    """You pick who leads a space (or clear it, cwd ''), in the web UI. Old and new lead are told."""
    space = clean_space(space)
    if not space or space == DEFAULT_SPACE:
        return 400, {"error": "pick a named space; the default space has no lead"}
    with lock:
        if cwd and not any(is_live(r) and r.get("cwd") == cwd and space_of(r)[0] == space for r in sessions.values()):
            return 409, {"error": f"no live agent in {tilde_path(cwd)} is in the {space} space"}
        old = space_lead(space)[0]
        chosen = config.setdefault("space_leads", {})
        if cwd:
            chosen[space] = {"cwd": cwd, "how": "set", "ts": time.time()}
        else:
            chosen.pop(space, None)
        save_config()
        new = space_lead(space)[0]
        if old != new:
            if old:
                task_message(None, old, f"You no longer lead the {space} space: {folder_name(new) + ' does' if new else 'it has no lead now'}, as your user set in the web UI. Hand anything you were coordinating for the space to {folder_name(new) if new else 'the agent that owns it'}.")
            if new:
                parent = space_parent(space)
                task_message(None, new, f"You now lead the {space} space, as your user set in the web UI{' and speak for it in ' + parent if parent else ''}. Work sent to the space comes to you: decide which agent owns it, delegate with send_message, and report the outcome to whoever asked.")
        log("lead", space, "->", new or "(none)", "set in the web UI")
    return 200, {"space": space, "lead": new, "how": space_lead(space)[1]}


def leads_view():
    """Every space's lead for the web UI, an offline one too, so it stays pinned (greyed) at the top of its space."""
    with lock:
        spaces = {space_of(r)[0] for r in sessions.values() if is_live(r)} | set(config.get("space_leads") or {})
        out = {}
        for space in spaces:
            folder, how = space_lead(space)
            if folder:
                agent = successor(folder)
                out[space] = {"cwd": folder, "how": how, "name": display_name(agent) if agent else folder_name(folder),
                              "live": bool(agent and space_of(agent)[0] == space), "parent": space_parent(space)}
        return out


def also_in(rec):
    """Spaces you added this agent's folder to in the web UI, on top of its own, wherever the folder is."""
    own = space_of(rec)[0]
    return sorted(s for s in (config.get("space_members") or {}).get(rec.get("cwd") or "", []) if s != own)


def set_members(cwd, spaces):
    """You set the spaces an agent's folder is also a member of (none clears it). The agents there are told."""
    cwd = os.path.abspath(os.path.expanduser(cwd)) if cwd else ""
    if not cwd:
        return 400, {"error": "say which agent's folder"}
    spaces = sorted({clean_space(s) for s in spaces} - {"", DEFAULT_SPACE})
    with lock:
        members = config.setdefault("space_members", {})
        old = members.get(cwd, [])
        if spaces:
            members[cwd] = spaces
        else:
            members.pop(cwd, None)
        save_config()
        added, dropped = sorted(set(spaces) - set(old)), sorted(set(old) - set(spaces))
        if added or dropped:
            said = "; ".join(filter(None, [
                f"you're now also a member of {', '.join(added)}: you see and message its agents, and they see you" if added else "",
                f"you're no longer a member of {', '.join(dropped)}" if dropped else ""]))
            task_message(None, cwd, f"Your user changed your spaces in the web UI: {said}.")
        log("members", tilde_path(cwd), "->", ", ".join(spaces) or "(none)")
    return 200, {"cwd": cwd, "spaces": spaces}


def set_folder_space(cwd, space):
    """You put a folder in a space (or clear it, back to its .peerlink or default). Every agent there moves, and is
    told; the folder keeps it for agents opened there later. Called without the lock."""
    cwd = os.path.abspath(os.path.expanduser(cwd)) if cwd else ""
    if not cwd:
        return 400, {"error": "say which agent's folder"}
    space = clean_space(space)
    with lock:
        chosen = config.setdefault("folder_spaces", {})
        if (chosen.get(cwd) or "") == space:
            return 200, {"cwd": cwd, "space": space}
        if space:
            chosen[cwd] = space
        else:
            chosen.pop(cwd, None)
        save_config()
    forget_spaces()
    with lock:
        now = space_of({"id": "", "cwd": cwd})[0]
        task_message(None, cwd, f"Your user moved this folder to the {now} space. You now see and message its agents, and they see you.")
        note_lone_leads()
        log("folder space", tilde_path(cwd), "->", space or "(its .peerlink)")
    return 200, {"cwd": cwd, "space": now}


def set_parent(space, parent):
    """You set the space `space` sits inside ('' for the top level), on the Spaces page. Its lead is told."""
    space, parent = clean_space(space), clean_space(parent)
    if not space or space == DEFAULT_SPACE:
        return 400, {"error": "pick a named space"}
    if parent == space:
        return 400, {"error": "a space can't sit inside itself"}
    with lock:
        seen, p = {space}, parent
        while p:
            if p in seen:
                return 400, {"error": f"{parent} sits inside {space} already"}
            seen.add(p)
            p = space_parent(p)
        config.setdefault("space_parents", {})[space] = parent
        save_config()
        folder = space_lead(space)[0]
        if folder:
            task_message(None, folder, f"Your user put the {space} space you lead {'inside ' + parent + ': you speak for it there' if parent else 'at the top level'}.")
        log("parent", space, "->", parent or "(top)")
    return 200, {"space": space, "parent": parent}


def reach(rec):
    """The spaces this agent sees and messages: its own, the parent too when it leads its space, and any you added it to."""
    parent = parent_of(rec)
    return {space_of(rec)[0], parent, *also_in(rec)} - {""}


def peer_reach(p):
    return set(p.get("reach") or [p.get("space", DEFAULT_SPACE)])


def hierarchy(rec):
    """Who leads this agent's space, and the live leads of spaces inside it."""
    space = space_of(rec)[0]
    lead, children = "", []
    for r in sessions.values():
        if r["id"] == rec["id"] or not is_live(r) or not leads(r):
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
        "role": role_of(rec),
        "folder_role": folder_role(rec),
        "space": space,
        "space_source": source,
        "space_from": where,
        "parent": parent_of(rec),
        "leads": leads(rec),  # 'set' or 'only' when it leads its space
        "space_parent": space_parent(space),
        # Without ages, which change every second and would redraw the page each poll.
        "threads": {k: [{f: t[f] for f in ("id", "with", "text")} for t in v] for k, v in open_threads(rec, limit=5).items()},
        "open_tasks": sum(1 for t in tasks.values() if t["status"] in TASK_OPEN and t["owner_cwd"] and t["owner_cwd"] == rec.get("cwd")),
        "reach": sorted(reach(rec)),
        "also": also_in(rec),
        "busy": rec.get("busy", False),
        "busy_since": rec.get("busy_since"),
        "sent_hour": sent_last_hour(rec),
        # The plugin version the agent runs ('' before 0.6.6): one behind the daemon needs /reload-plugins.
        "plugin": rec.get("plugin", ""),
        "context": rec.get("context"),  # percent of the context window in use, when the agent reports it
        "stale": rec.get("plugin", "") != VERSION,
        "muted": session_cfg(rec["id"])["muted"],
        "terminating": bool(rec.get("terminate")),
        "last_seen_s": round(time.time() - rec["last_seen"], 1),
    }


def set_busy(rec, busy):
    """Record busy or idle, and since when, so the web UI can say "idle 12m"."""
    busy = bool(busy)
    if rec.get("busy") != busy or "busy_since" not in rec:
        rec["busy_since"] = time.time()
    rec["busy"] = busy


remote_cache = [0.0, []]  # when, peers: other machines are asked at most every 5 s, however many browsers watch


def snapshot():
    """Everything the web UI shows, in one answer."""
    now = time.time()
    if config.get("remotes") and now - remote_cache[0] > 5:
        remote_cache[:] = [now, remote_peers()]
    peers = local_peers() + (remote_cache[1] if config.get("remotes") else [])
    with lock:
        status = {"version": VERSION, "pid": os.getpid(), "host": HOSTNAME, "port": PORT,
                  "sessions": sum(1 for r in sessions.values() if is_live(r, now)), "queued": sum(len(b) for b in inboxes.values()),
                  "messages": len(messages), "browsers": sum(1 for e in web_sessions.values() if e > now), "home": HOME, "launcher": LAUNCHER_PATH}
        msgs = messages[-100:]
        task_list = sorted(tasks.values(), key=lambda t: -t["created"])[:500]
        issues = sorted(feedback.values(), key=lambda i: i["ts"])[-500:]
        sched = sorted(schedules.values(), key=lambda c: -c["created"])[:200]
        status["limit"] = limit_view()
    return {"schedules": {"schedules": [{**c, "when_text": when_text(c)} for c in sched]},"status": status, "peers": {"host": HOSTNAME, "peers": peers, "leads": leads_view()}, "messages": {"messages": msgs},
            "tasks": {"tasks": task_list}, "issues": {"issues": issues, "path": ISSUES_PATH}, "inbox": {"items": inbox_items(), "user": USER_NAME}}


def sent_last_hour(rec):
    """How many messages this agent's folder sent in the last hour: a chatty pair shows up here first."""
    cutoff, cwd = time.time() - 3600, rec.get("cwd")
    return sum(1 for m in reversed(messages[-500:]) if m.get("ts", 0) > cutoff and (m.get("from") == rec["id"] or (cwd and m.get("from_cwd") == cwd)))


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
                if not isinstance(p, dict):
                    continue
                # The web UI puts these in its markup unescaped, as the numbers they should be.
                for k in ("context", "sent_hour", "open_tasks"):
                    if p.get(k) is not None and not isinstance(p[k], (int, float)) or isinstance(p.get(k), bool):
                        p[k] = None if k == "context" else 0
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


def session_gone(sid):
    """An agent ended or stopped checking in: what it was handed but never acked goes back in its queue, which
    waits an hour for the next agent in its folder (adopt() hands it over)."""
    sessions.pop(sid, None)
    unacked = [m for m, _ in handed.pop(sid, {}).values()]
    box = inboxes.setdefault(sid, [])
    box.extend(m for m in unacked if m not in box)
    if not box:
        inboxes.pop(sid)


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
           "trusted_bots": [],
           # People's user IDs who may talk to agents and answer buttons as you do, beside the app's owner or team.
           "allowed_users": []}
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
        discord_bot[0].allowed_users = set(discord.get("allowed_users") or [])
        discord_bot[0].on_command, discord_bot[0].on_autocomplete = on_slash, on_slash_typing


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


def approval_channel(rec):
    """The Discord channel to ask your approval in for an agent: one its folder is in, else its space lead's, else
    the parent space lead's. '' when none of them has one, and the request waits in the Inbox only."""
    space = space_of(rec)[0]
    for cwd in (rec.get("cwd", ""), space_lead(space)[0], space_lead(space_parent(space))[0] if space_parent(space) else ""):
        chans = [r for r in routes_for(cwd) if r["channel_id"] != "dm" and carries(r, "message")] if cwd else []
        if chans:
            return chans[0]["channel_id"]
    return ""


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


def queue_for_folder(cwd, msg, prefer=None):
    """Deliver to `prefer` or the folder's live agent, or hold it for the next one there (adopt() hands it over)."""
    agent = prefer or successor(cwd)
    msg["to"] = agent["id"] if agent else f"folder:{cwd}"
    msg["to_name"] = display_name(agent) if agent else folder_name(cwd)
    inboxes.setdefault(msg["to"], []).append(msg)
    record(msg)
    return agent


def oneline(text):
    return " ".join(str(text).split())


def live_agents():
    with lock:
        return sorted((r for r in sessions.values() if is_live(r)), key=lambda r: display_name(r).lower())


def agent_named(name):
    want = str(name or "").lstrip("@").lower()
    return next((r for r in live_agents() if display_name(r).lower() == want), None)


def channel_agent(channel_id, dm):
    """The agent a channel's messages go to: its master's, or your DMs'."""
    cwd = discord.get("dm") if dm else next((r.get("master") for r in discord.get("routes", []) if r["channel_id"] == channel_id), "")
    with lock:
        return successor(cwd) if cwd else None


def on_slash(cmd):
    """/peerlink <sub> on Discord: (the reply, any buttons)."""
    sub, opt, who = cmd["sub"], cmd["options"], f"Discord ({cmd['author']})"
    if sub == "status":
        lim = limit_view()
        with lock:
            queued = sum(len(b) for b in inboxes.values())
        held = (f"held{', resets ' + time.strftime('%a %H:%M', time.localtime(lim['resets_at'])) if lim.get('resets_at') else ''} (/peerlink resume lifts it)") if lim else "fine"
        return f"peerlink {VERSION} on {HOSTNAME} · {len(live_agents())} agents running · {queued} messages queued · usage limit: {held}", None
    if sub == "inbox":
        return inbox_text(cmd["channel_id"]), None
    if sub == "peers":
        rows = [f"• **{display_name(r)}** {'🛠️' if r.get('busy') else '💤'}{' 🔇' if session_cfg(r['id']).get('muted') else ''} `{tilde_path(r.get('cwd', ''))}`"
                + (f": {oneline(r.get('summary') or '')[:90]}" if r.get("summary") else "") for r in live_agents()]
        return "\n".join(rows) or "No agents are running.", None
    if sub == "spaces":
        leads, by_space = leads_view(), {}
        for r in live_agents():
            by_space.setdefault(space_of(r)[0], []).append(display_name(r))
        lines = []
        for space in sorted(set(by_space) | set(leads), key=lambda x: (x == DEFAULT_SPACE, x)):
            lead, parent = leads.get(space) or {}, space_parent(space)
            head = f"**{'unassigned' if space == DEFAULT_SPACE else space}**" + (f" (in {parent})" if parent else "") + (f" · lead {lead['name']}" if lead else "")
            lines.append(f"{head}: {', '.join(by_space.get(space, [])) or 'nobody running'}")
        return "\n".join(lines) or "No spaces yet.", None
    if sub == "schedules":
        with lock:
            live = sorted((c for c in schedules.values() if c["status"] in ("active", "pending")), key=lambda c: c.get("next_run") or 0)
        rows = [f"• {c['id']} · {when_text(c)} · {c['agent'] + ' ' if c.get('agent') else ''}`{tilde_path(c['folder'])}`{' · waiting for approval' if c['status'] == 'pending' else ''}: {oneline(c['prompt'])[:90]}" for c in live]
        return ("\n".join(rows) + "\nCancel one with /peerlink cancel.") if rows else "No schedules.", None
    if sub == "cancel":
        code, out = cancel_schedule(str(opt.get("id", "")).strip())
        return (f"Cancelled {out['schedule']['id']}." if code == 200 else out["error"]), None
    if sub == "resume":
        if not usage_limit:
            return "No usage limit is held.", None
        lift_limit(f"resumed on Discord by {cmd['author']}")
        return "Lifted: agents carry on with what was held.", None
    if sub == "clear" and str(opt.get("agent", "")).lower() == "all":
        n = len(live_agents())
        return f"Clear the context of all {n} running agents? Each finishes its current turn first.", discordlink.buttons(["Clear all", "Keep"], "pa:all")
    agent = agent_named(opt["agent"]) if opt.get("agent") else channel_agent(cmd["channel_id"], cmd["dm"]) if sub == "run" else None
    if agent is None:
        return (f"No running agent is called {opt['agent']}." if opt.get("agent") else "No agent is running for this channel; name one with agent:"), None
    name = display_name(agent)
    if sub == "clear":
        code, out = clear_context(agent["id"], who)
        return (f"Clearing {name}'s context{' after its current turn' if agent.get('busy') else ''}." if code == 200 else out["error"]), None
    if sub == "kill":
        return f"End **{name}** ({tilde_path(agent.get('cwd', ''))})? It finishes its current turn first.", discordlink.buttons(["End it", "Keep it"], f"pk:{agent['id']}")
    if sub in ("mute", "unmute"):
        with lock:
            config["sessions"].setdefault(agent["id"], {})["muted"] = sub == "mute"
            save_config()
        return f"{name} {'is muted: it hears from no peers' if sub == 'mute' else 'hears from peers again'}.", None
    if sub == "run":
        line = str(opt.get("command", "")).strip().lstrip("/")
        command, _, args = line.partition(" ")
        if not command:
            return "Say which command, like loop 5m check the deploy.", None
        with lock:
            agent.setdefault("run", []).append({"command": command, "args": args.strip(), "by": who})
        log("run", f"/{line}", "in", name, "by", who)
        return f"Running `/{line}` in {name}{' after its current turn' if agent.get('busy') else ''}.", None
    return f"Unknown: {sub}", None


def on_slash_typing(cmd):
    """Choices for the option being typed in a /peerlink subcommand."""
    typed = str(cmd["options"].get(cmd["focused"]) or "").lower()
    if cmd["focused"] == "id":
        with lock:
            names = [(f"{c['id']} · {when_text(c)}: {oneline(c['prompt'])}"[:100], c["id"]) for c in schedules.values() if c["status"] in ("active", "pending")]
    else:
        names = [(display_name(r), display_name(r)) for r in live_agents()] + ([("all (every running agent)", "all")] if cmd["sub"] == "clear" else [])
    return [{"name": n, "value": v} for n, v in names if typed in n.lower()]


def on_clear_all_press(press):
    rows = (press["message"] or {}).get("components") or []
    for row in rows:
        for b in row.get("components", []):
            b["disabled"], b["style"] = True, 3 if b.get("custom_id") == press["custom_id"] else 2
    discordlink.answer_interaction(press["id"], press["token"], 7, {"components": rows})
    if press["custom_id"].endswith(":0"):
        agents = live_agents()
        for r in agents:
            clear_context(r["id"], f"Discord ({press['author']})")
        discordlink.post(discord["token"], press["channel_id"], f"Clearing {len(agents)} agents' context, each once its current turn ends.")


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


# A message from Discord shows where it's at: 👀 heard, 🤔 thinking, 🛠️ using tools,
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


# Activity shows on a Discord message only during the turn that handles it: once that turn ends it's settled,
# answered or not (a button pick, or one reply to several messages, never gets a reply_to of its own).
ACTIVITY_MAX_AGE_S = 1800  # and never on one delivered longer ago than this, as settled marks don't survive a restart


def discord_pending(rec):
    """The Discord messages rec is working on now. Called with the lock held."""
    answered = {m.get("reply_to") for m in messages if m.get("to", "").startswith("discord:")}
    now = time.time()
    for m in messages[-200:]:
        closed = tasks.get(m.get("task") or "", {}).get("status") in TASK_CLOSED
        if (m.get("discord") and m.get("to") == rec["id"] and m.get("status") == "delivered" and not m.get("settled")
                and now - m.get("delivered_ts", m["ts"]) < ACTIVITY_MAX_AGE_S and m["id"] not in answered and not closed):
            yield m


def discord_activity(rec, activity):
    """Show an agent's activity on the Discord messages it's working on."""
    emoji = ACTIVITY_EMOJI.get(activity)
    if emoji:
        for m in discord_pending(rec):
            set_reaction(m, emoji)


def clean_stale_reactions():
    """Once: take 🤔, 🛠️ and ❓ off your recent Discord messages. Daemons before 0.8.8 could leave them stuck, and
    which ones isn't recorded. A message an agent is still on gets its reaction back at the next heartbeat."""
    if config.get("reactions_cleaned") or not discord.get("token"):
        return
    cutoff = time.time() - 2 * 86400
    with lock:
        recent = [m for m in messages if m.get("discord") and str(m.get("from", "")).startswith("discord:") and m.get("ts", 0) > cutoff][-60:]
        for m in recent:
            reactions.pop(m["id"], None)
            for emoji in ACTIVITY_EMOJI.values():
                reaction_jobs.append((m["discord"]["channel_id"], m["discord"]["message_id"], emoji, None))
        config["reactions_cleaned"] = VERSION
        save_config()
    reaction_wake.set()
    log("discord", "clearing activity reactions from", len(recent), "recent messages")


def discord_turn_done(rec):
    """The agent's turn ended: what it didn't answer goes back to 👀 (delivered) and stays there."""
    for m in list(discord_pending(rec)):
        m["settled"] = True
        set_reaction(m, "👀")


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
    if press["custom_id"].startswith("pc:"):
        return on_spawn_press(press, decide_schedule, schedules)
    if press["custom_id"].startswith("pa:"):
        return on_clear_all_press(press)
    if press["custom_id"] in ("pr:0", "pl:0"):  # "pl:0" from before 0.8.33; an agent's buttons are "pl:<message>:<n>"
        discordlink.answer_interaction(press["id"], press["token"], 7, {"components": []})
        return lift_limit(f"resumed on Discord by {press['author']}")
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
        queue_for_folder(asked.get("from_cwd", ""), msg, continuation(asked.get("from", ""), asked.get("from_name", ""), asked.get("from_cwd", "")))
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
    if (word == "discord" or word.startswith("discord#")) and not chans:
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
        out += [{"kind": "approval", "id": c["id"], "who": c["requester"].get("name", ""), "since": c["created"],
                 "title": f"Schedule for {tilde_path(c['folder'])}: {when_text(c)}",
                 "text": f"{c['prompt']}\n\nIt starts an agent there when none is running."}
                for c in schedules.values() if c["status"] == "pending"]
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
        decide = decide_schedule if iid in schedules else decide_spawn
        return decide(iid, action == "approve", "Discord" if discord_from else "the web UI")
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


def inbox_text(channel):
    """Your Inbox as a numbered list; "1 done" and the like in this channel then refer to it."""
    items = inbox_items()
    inbox_lists[channel] = (time.time(), [(i["kind"], i["id"]) for i in items])
    if not items:
        return "Your Inbox is empty."
    icon = {"approval": "🚀", "task": "☑️", "question": "❓", "feedback": "💡"}
    lines = [f"**Inbox** ({len(items)})"] + [
        f"{n}. {icon[i['kind']]} {i['id']} · {i['who']} · {ago_text(time.time() - i['since'])}: {i['title']}" for n, i in enumerate(items[:20], 1)]
    lines.append('Answer with the number: "1 approve" or "1 deny", "2 done" or "2 failed: why", "3 <your answer>", "4 accept" or "4 dismiss".')
    return "\n".join(lines)


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
            if not text.startswith("/"):
                return False  # "kill it" is a message for the agent, not a command
            say(f"No running agent is called {kill.group(1)}.")
            return True
        discordlink.post(discord["token"], channel, f"End **{display_name(agent)}** ({tilde_path(agent.get('cwd', ''))})? It finishes its current turn first.",
                         d["message_id"], discordlink.buttons(["End it", "Keep it"], f"pk:{agent['id']}"))
        return True
    if text.lower() in ("/inbox", "inbox"):
        say(inbox_text(channel))
        return True
    listed = inbox_lists.get(channel)
    match = re.match(r"^(\d+)[\s.:)]+(.+)$", text, re.S)
    if not (listed and match and time.time() - listed[0] < 3600):
        return False
    n, rest = int(match.group(1)), match.group(2).strip()
    if not 1 <= n <= len(listed[1]):
        return False  # "3. also update the README": a numbered line for the agent
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
        return False  # no verb it takes: a message for the agent that happens to start with a number
    say(f"{iid}: {out.get('error')}" if code != 200 else f"{iid}: {action if want else 'answer sent'}.")
    return True


def ago_text(s):
    return f"{int(s)}s" if s < 90 else f"{round(s / 60)}m" if s < 5400 else f"{round(s / 3600)}h" if s < 129600 else f"{round(s / 86400)}d"


def lead_target(sender_rec, word, spaces):
    """(target, error) for a message or task sent to a space ("space", "space:<name>", or "lead:<name>"): its lead,
    or the lead's folder when it's offline, where it waits. Called with the lock held."""
    name = word.partition(":")[2]
    space = clean_space(name) if name else space_of(sender_rec)[0] if sender_rec else ""
    if not space:
        return None, "say which space, as space:<name>"
    # A space's lead also sits in its parent, so the parent's agents reach the space through it.
    if spaces is not None and not {space, space_parent(space)} & spaces:
        return None, f"you can reach {' and '.join(sorted(spaces))}, not the {space} space"
    folder = space_lead(space)[0]
    if not folder:
        return None, f"the {space} space has no lead: message one of its agents by name (list_peers), or ask your user to pick a lead in the web UI"
    agent = successor(folder)
    if agent and space_of(agent)[0] == space:
        return agent, None
    return {"id": f"folder:{folder}", "cwd": folder, "name": folder_name(folder)}, None


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
        # The answer to a question asked at the limit still gets through, as the thread's last word: it can't be replied to.
        last_word = hops > config["max_hops"] and kind == "message" and bool(parent and parent.get("expects_reply"))
        if hops > config["max_hops"] and not last_word:
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
    # A reply to a message from Discord goes back to that message, whether the model wrote "user" or "discord":
    # as a reply in its thread, and recorded with reply_to, so the message no longer reads as unanswered.
    if parent and parent.get("discord") and kind == "message":
        out = send_discord(sender_rec, to, text, parent, hops, options, files, bool(body.get("expects_reply")))
        if out is not None:
            return out
    # "discord" or "discord#channel", not an agent whose name starts with discord, like discord-voice.
    if sender_rec and (word in USER_ALIASES or (kind == "task" and (word == "discord" or word.startswith("discord#")) and not parent)):
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
            # The sender's conversation may have moved on (a /clear, a resume): the session that continued it takes the
            # reply, else its folder's live agent. Two agents can share a folder, so the right one is found first.
            back = continuation(parent["from"], parent.get("from_name", ""), parent.get("from_cwd", ""))
            if not back:
                back = successor(parent.get("from_cwd") or (sessions.get(parent["from"]) or {}).get("cwd", ""))
            if back:
                target = back
            elif parent.get("from_cwd"):
                # Nobody there right now (a daemon restart, an agent reloading): it waits for the folder's next agent.
                target = {"id": f"folder:{parent['from_cwd']}", "cwd": parent["from_cwd"], "name": parent.get("from_name") or ""}
            else:
                err = f"{parent.get('from_name') or 'the sender'} of {parent['id']} is no longer live and no agent runs in its folder now"
        elif not to:
            err = "to is required unless reply_to names a message sent to you"
        elif word in ("space", "lead") or word.startswith(("space:", "lead:")):
            target, err = lead_target(sender_rec, word, spaces)
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
            if str(to).strip().lower().startswith(("space", "lead")):
                return 400, {"error": "you lead that space, so this would come back to you; to reach everyone in it, send with kind \"announcement\""}
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
            "expects_reply": (bool(body.get("expects_reply")) or kind == "task") and not last_word,
            "reply_to": body.get("reply_to") or None,
            "hops": hops,
            "status": "queued",
            "v": 2,  # logs older than this never recorded delivery, so they are not replayed
        }
        if last_word:
            msg["text"] += f"\n\n(peerlink: this thread reached its {config['max_hops']}-reply limit with this answer, so it can't be answered. If more is needed, tell your user.)"
        # Worth starting a turn for: a question, a task, or the answer to a question this agent asked. An FYI, or a
        # reply to a reply, waits for the agent's next turn (wake_for "all" wakes for everything, as before 0.8.11).
        msg["wake"] = config.get("wake_for", "needed") == "all" or msg["expects_reply"] or bool(parent and parent.get("expects_reply"))
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
    out = {"id": msg["id"], "to": msg["to_name"], "status": "queued", "task": msg.get("task")}
    if last_word:
        out["note"] = f"delivered as this thread's last reply: it reached max_hops={config['max_hops']}, so neither of you can reply to it"
    return 200, out


def web_send(body):
    """A message or task from you in the web UI, to an agent (name or id) or a space's lead ("space:<name>")."""
    to, text = str(body.get("to") or "").strip(), str(body.get("text") or "").strip()
    kind = "task" if body.get("kind") == "task" else "message"
    if not to or not text:
        return 400, {"error": "say who it's for and what it says"}
    with lock:
        word = to.lower()
        if word.startswith("space:"):
            target, err = lead_target(None, word, None)
        else:
            target, err = resolve(to)
        if err or target is None:
            return 404, {"error": err or f"no live agent '{to}'"}
        msg = {"id": f"m{next_msg[0]}", "ts": time.time(), "from": "user", "from_name": f"{USER_NAME} (web UI)", "from_host": HOSTNAME,
               "from_cwd": "", "from_space": "", "to": target["id"], "to_name": display_name(target) if target["id"] in sessions else target["name"],
               "to_cwd": target.get("cwd", ""), "to_space": space_of(target)[0] if target["id"] in sessions else "",
               "text": text[:20000], "expects_reply": kind == "task", "reply_to": None, "hops": 0, "status": "queued", "v": 2}
        next_msg[0] += 1
        if kind == "task":
            new_task(msg, {"id": "user", "name": msg["from_name"], "cwd": ""})
        inboxes.setdefault(target["id"], []).append(msg)
        record(msg)
    log("web", msg["id"], "->", msg["to_name"], kind)
    return 200, {"id": msg["id"], "to": msg["to_name"], "task": msg.get("task")}


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


# A session id -> the one that continued it after a /clear or resume, so a reply to the old one finds the new.
succeeded_by = {}


def continuation(sid, name="", cwd=""):
    """The live session a message for `sid` should reach now: sid itself, what it became after /clear (followed
    through succeeded_by), or else the live agent of that name in its folder. None when none of those is live."""
    seen = set()
    while sid and sid not in seen:
        seen.add(sid)
        rec = sessions.get(sid)
        if rec and is_live(rec):
            return rec
        sid = succeeded_by.get(sid)
    named = [r for r in sessions.values() if is_live(r) and r.get("cwd") == cwd and name and display_name(r) == name]
    return max(named, key=lambda r: r["last_seen"]) if named else None


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
    # Handed to the session it replaces but never acked (it cleared or reloaded first): handed out again here.
    if old and old != rec["id"] and handed.get(old):
        mine = handed.setdefault(rec["id"], {})
        for mid, (m, _) in handed.pop(old).items():
            mine[mid] = (m, 0)
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


def take_inbox(sid, track=False):
    """New messages for this agent, marked delivered. With track (a plugin that acks), each is handed again every
    REDELIVER_S until the plugin acks it, for up to a day."""
    with lock:
        box = inboxes.pop(sid, [])
        now = time.time()
        again = []
        if track:
            mine = handed.setdefault(sid, {})
            for mid in [mid for mid, (m, ts) in mine.items() if now - m["ts"] >= HANDED_MAX_AGE_S]:
                mine.pop(mid)
            # While the usage limit holds, agents keep what they were handed on purpose: handing it out again is noise.
            again = [] if usage_limit else [m for m, ts in mine.values() if now - ts >= REDELIVER_S]
            for m in box + again:
                mine[m["id"]] = (m, now)
            if again:
                log("redeliver", ", ".join(m["id"] for m in again), "->", sid[:8])
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
        return box + again


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
    """A note about a task from its owner (or peerlink), or from peerlink about a change of role (t None),
    queued for an agent's folder. Called with the lock held."""
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
            # The page acted on what it last drew: if the owner has changed the task since, say so, don't override it.
            if body.get("expect") and body["expect"] != t["status"]:
                return 409, {"error": f"{tid} is {t['status'].replace('_', ' ')} now ({t['owner']} changed it); nothing was changed", "task": dict(t)}
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
        channel = approval_channel(by)
    log("spawn asked", sp["id"], sp["requester"]["name"], folder)
    if channel and discord.get("token") and discord_bot[0]:
        try:
            sent = discordlink.post(discord["token"], channel,
                                    f"**{sp['requester']['name']}** asks to start an agent in `{tilde_path(folder)}` (space {sp['space']}): {purpose}\nIts first prompt: {prompt[:600]}",
                                    None, discordlink.buttons(["Approve", "Deny"], f"ps:{sp['id']}"))
            with lock:
                sp["discord_out"] = {"channel_id": channel, "message_id": str((sent[-1] or {}).get("id", "")) if sent else ""}
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


def on_spawn_press(press, decide=None, items=None):
    """Approve or Deny on Discord, for a request to start an agent or a schedule that may start one."""
    decide, items = decide or decide_spawn, spawns if items is None else items
    _, sid, idx = (press["custom_id"].split(":") + ["", ""])[:3]
    sp = items.get(sid)
    if not sp or sp["status"] != "pending":
        discordlink.answer_interaction(press["id"], press["token"], 4, {"content": f"{sid} is already {sp['status'] if sp else 'gone'}.", "flags": 64})
        return
    rows = (press["message"] or {}).get("components") or []
    for row in rows:
        for b in row.get("components", []):
            b["disabled"] = True
            b["style"] = 3 if b.get("custom_id") == press["custom_id"] else 2
    discordlink.answer_interaction(press["id"], press["token"], 7, {"components": rows})
    decide(sid, idx == "0", f"Discord ({press['author']})")


# Schedules: a prompt the daemon sends a folder's agent at a time or on a cron, whether or not a terminal is open.
# When none is running there it starts one, as an approved spawn does; a schedule an agent asks for that may do
# that waits for your approval first. Times are this machine's local time.
CRON_NAMES = ({}, {}, {}, {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())},
              {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())})
CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def parse_cron(expr):
    """The five fields of a cron (minute hour day-of-month month weekday) as sets; ValueError when malformed."""
    parts = str(expr or "").lower().split()
    if len(parts) != 5:
        raise ValueError("a cron has five fields: minute hour day-of-month month weekday, like 0 9 * * 1-5")
    fields = []
    for part, (lo, hi), names in zip(parts, CRON_RANGES, CRON_NAMES):
        values = set()
        for item in part.split(","):
            rng, slash, step = item.partition("/")
            num = lambda v: names[v] if v in names else int(v)
            try:
                a, b = (lo, hi) if rng == "*" else (num(rng.split("-")[0]), num(rng.split("-")[1])) if "-" in rng else (num(rng), hi if slash else num(rng))
                step = int(step) if slash else 1
            except (ValueError, IndexError):
                raise ValueError(f"can't read {item!r} in {expr!r}")
            if not (lo <= a <= b <= hi) or step < 1:
                raise ValueError(f"{item!r} is out of range in {expr!r}")
            values.update(range(a, b + 1, step))
        fields.append(values)
    if 7 in fields[4]:
        fields[4] = (fields[4] - {7}) | {0}
    # As cron reads it: with both day fields restricted, a day matching either runs.
    fields.append((parts[2] == "*", parts[4] == "*"))
    return fields


def cron_next(expr, after):
    """The first minute after `after` (epoch seconds) the cron matches, in local time."""
    import datetime as dt
    minute, hour, dom, month, dow, (any_dom, any_dow) = parse_cron(expr)
    t = dt.datetime.fromtimestamp(after).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    for _ in range(100000):
        wd = t.isoweekday() % 7
        day_ok = (t.day in dom and wd in dow) if any_dom or any_dow else (t.day in dom or wd in dow)
        if t.month not in month:
            t = (t.replace(day=1, hour=0, minute=0) + dt.timedelta(days=32)).replace(day=1)
        elif not day_ok:
            t = (t + dt.timedelta(days=1)).replace(hour=0, minute=0)
        elif t.hour not in hour:
            t = (t + dt.timedelta(hours=1)).replace(minute=0)
        elif t.minute not in minute:
            t += dt.timedelta(minutes=1)
        else:
            return t.timestamp()
    raise ValueError(f"{expr!r} never runs")


def parse_at(text, now):
    """A one-off time: 'in 30m', 'in 2h', 'in 1d', or a local date and time like 2026-10-25 09:00."""
    import datetime as dt
    t = str(text or "").strip().lower()
    m = re.fullmatch(r"(?:in\s+)?(\d+)\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?)", t)
    if m:
        return now + int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)[0]]
    try:
        when = dt.datetime.fromisoformat(t.replace(" ", "T") if re.match(r"\d{4}-\d\d-\d\d \d", t) else t)
    except ValueError:
        raise ValueError(f"can't read {text!r}: give 'in 30m', 'in 2h', 'in 1d' or a local date and time like 2026-10-25 09:00")
    return when.timestamp()


def when_text(c):
    import datetime as dt
    if c.get("cron"):
        return f"cron {c['cron']}"
    return "once, " + dt.datetime.fromtimestamp(c.get("at") or 0).strftime("%a %b %-d %H:%M")


def load_schedules():
    try:
        with open(SCHEDULES_PATH) as f:
            for x in json.load(f):
                schedules[x["id"]] = x
    except (OSError, ValueError, KeyError):
        pass


def save_schedules():
    with open(SCHEDULES_PATH + ".tmp", "w") as f:
        json.dump(list(schedules.values()), f, indent=1)
    os.replace(SCHEDULES_PATH + ".tmp", SCHEDULES_PATH)


def schedule_told(c, text):
    """Tell the agent that made a schedule what happened to it. Called with the lock held."""
    if c["requester"].get("cwd"):
        task_message(None, c["requester"]["cwd"], text)


def create_schedule(body, by=None):
    """A schedule from an agent (`by`) or from you (None). An agent's that may start one waits for your approval.
    It goes to an agent by name (its folder's other agent, or a new one there, when it isn't running), or to a folder."""
    now = time.time()
    name = str(body.get("agent") or "").strip().lstrip("@")
    target = agent_named(name) if name else None
    if name and target is None:
        # The web UI's one field takes either: a name no agent has is a folder.
        if body.get("folder") or not os.path.isdir(os.path.expanduser(name)):
            return 400, {"error": f"{name} isn't a folder on this machine" if "/" in name else f"no running agent is called {name}; give its folder instead"}
        body, name = {**body, "folder": name}, ""
    if not name and not body.get("folder") and by:
        target = by  # an agent's own schedule is its own
    folder = os.path.abspath(os.path.expanduser(str((target or {}).get("cwd") if target else body.get("folder") or "")))
    prompt = str(body.get("prompt") or "").strip()
    cron, at = str(body.get("cron") or "").strip(), body.get("at")
    start = body.get("start") is not False
    if not os.path.isdir(folder):
        return 400, {"error": f"{folder} isn't a folder on this machine"}
    if not prompt:
        return 400, {"error": "prompt is required: what the agent should do when it runs"}
    if bool(cron) == bool(at):
        return 400, {"error": "give either cron (a repeat, like 0 9 * * 1-5) or at (once, like 'in 2h' or 2026-10-25 09:00)"}
    try:
        next_run = cron_next(cron, now) if cron else parse_at(at, now)
    except ValueError as err:
        return 400, {"error": str(err)}
    if not cron and next_run <= now:
        return 400, {"error": "that time has passed"}
    with lock:
        n = max([int(i[1:]) for i in schedules] or [0]) + 1
        c = {"id": f"c{n}", "folder": folder, "agent": display_name(target) if target else "", "prompt": prompt[:8000], "cron": cron, "at": None if cron else next_run,
             "start": start, "next_run": next_run, "last_run": None, "runs": 0, "note": "",
             "requester": {"id": by["id"], "name": display_name(by), "cwd": by.get("cwd", "")} if by else {"name": USER_NAME, "cwd": ""},
             "status": "pending" if by and start else "active", "created": now, "updated": now}
        schedules[c["id"]] = c
        save_schedules()
        channel = approval_channel(by) if by else ""
    log("schedule", c["id"], c["status"], c["agent"] or tilde_path(folder), when_text(c))
    if c["status"] == "pending" and channel and discord.get("token") and discord_bot[0]:
        try:
            sent = discordlink.post(discord["token"], channel,
                                    f"**{c['requester']['name']}** asks to schedule `{tilde_path(folder)}` ({when_text(c)}), starting an agent there when none is running: {prompt[:600]}",
                                    None, discordlink.buttons(["Approve", "Deny"], f"pc:{c['id']}"))
            with lock:
                c["discord_out"] = {"channel_id": channel, "message_id": str((sent[-1] or {}).get("id", "")) if sent else ""}
                save_schedules()
        except discordlink.DiscordError as err:
            log("discord", "schedule post failed:", err)
    return 200, {"schedule": {**c, "when_text": when_text(c)}}


def decide_schedule(cid, approve, via):
    with lock:
        c = schedules.get(cid)
        if c is None:
            return 404, {"error": f"no schedule {cid}"}
        if c["status"] != "pending":
            return 409, {"error": f"{cid} is already {c['status']}"}
        c["status"], c["updated"] = ("active" if approve else "denied"), time.time()
        if approve and c["cron"]:
            c["next_run"] = cron_next(c["cron"], time.time())  # from now, not from when it was asked for
        schedule_told(c, f"Your user {'approved' if approve else 'denied'} schedule {cid} ({when_text(c)}).")
        save_schedules()
    close_buttons(c)
    log("schedule", cid, c["status"], "via", via)
    return 200, {"schedule": dict(c)}


def cancel_schedule(cid, by=None):
    """You cancel any schedule; an agent, one it made or one for its folder."""
    with lock:
        c = schedules.get(cid)
        if c is None:
            return 404, {"error": f"no schedule {cid}"}
        if by and by["id"] != c["requester"].get("id") and by.get("cwd") not in (c["folder"], c["requester"].get("cwd")):
            return 403, {"error": f"{cid} isn't yours or your folder's to cancel"}
        if c["status"] in ("cancelled", "done", "denied", "expired"):
            return 409, {"error": f"{cid} is already {c['status']}"}
        c["status"], c["updated"], c["next_run"] = "cancelled", time.time(), None
        if by is None:
            schedule_told(c, f"Your user cancelled schedule {cid} ({when_text(c)}).")
        save_schedules()
    close_buttons(c)
    log("schedule", cid, "cancelled", "by", display_name(by) if by else "you")
    return 200, {"schedule": dict(c)}


starting = {}  # folder -> when the daemon last started an agent there for a schedule


def fire_schedule(c, now):
    """Send the prompt to the folder's agent, or hold it for one; the next run is set. Returns c when an agent
    has to be started for it. Called with the lock held."""
    msg = {"id": f"m{next_msg[0]}", "ts": now, "from": "peerlink", "from_name": f"schedule {c['id']}", "from_host": HOSTNAME,
           "from_cwd": "", "to_cwd": c["folder"], "from_space": "", "to_space": "",
           "text": f"Scheduled ({when_text(c)}; cancel with the schedule tool, id {c['id']}): {c['prompt']}",
           "expects_reply": False, "reply_to": None, "hops": 0, "status": "queued", "v": 2}
    next_msg[0] += 1
    # Its agent when that one runs; else the folder's, or the next to start there.
    own = agent_named(c.get("agent")) if c.get("agent") else None
    if own:
        msg["to"], msg["to_name"] = own["id"], display_name(own)
        inboxes.setdefault(own["id"], []).append(msg)
        record(msg)
    agent = own or queue_for_folder(c["folder"], msg)
    c["last_run"], c["runs"], c["updated"] = now, c.get("runs", 0) + 1, now
    try:
        c["next_run"] = cron_next(c["cron"], now) if c["cron"] else None
    except ValueError:
        c["next_run"] = None
    if not c["cron"]:
        c["status"] = "done"
    c["note"] = (f"sent to {display_name(agent)}" + (f" ({c['agent']} wasn't running)" if c.get("agent") and agent is not own else "")) if agent else "no agent was running there"
    save_schedules()
    log("schedule", c["id"], "ran:", c["note"])
    # One start per folder at a time: a second run while the first agent is still starting waits for it.
    if agent or not c["start"] or now - starting.get(c["folder"], 0) < 120:
        return None
    starting[c["folder"]] = now
    return c


def start_for_schedule(c):
    how, err = launch({"dir": c["folder"], "id": c["id"]})
    with lock:
        c["note"] = f"started an agent {how}" if how else f"couldn't start an agent: {err}"
        if err:
            schedule_told(c, f"Schedule {c['id']} ran, but no agent was running in {tilde_path(c['folder'])} and one couldn't start: {err}. The prompt waits there for an hour.")
        save_schedules()
    log("schedule", c["id"], c["note"])


# The account's usage limit: an agent whose turn died on it says so, and until it lifts nothing new is put to agents
# (their plugins hold what arrives) and no schedule runs. It lifts at the reset time Claude gave, when any agent's
# turn goes through again (a swapped account), or when you press Resume.
usage_limit = {}
LIMIT_FALLBACK_S = 5 * 3600  # how long a limit with no reset time holds before agents try again


def hit_limit(rec, body):
    """An agent's turn ended on the usage limit: hold everything, put back what that turn was given, tell you."""
    now = time.time()
    resets = body.get("resets_at")
    resets = resets if isinstance(resets, (int, float)) and resets > now else None
    with lock:
        mine = handed.setdefault(rec["id"], {})
        back = [m for m in messages if m["id"] in set(map(str, body.get("ids") or []))]
        for m in back:
            mine[m["id"]] = (m, 0)  # handed out again as soon as the limit lifts
        fresh = not usage_limit
        usage_limit.update({"since": usage_limit.get("since", now), "resets_at": resets or usage_limit.get("resets_at"),
                            "kind": str(body.get("kind") or usage_limit.get("kind") or ""), "by": display_name(rec)})
        chans = [r for r in routes_for(rec.get("cwd", "")) if r["channel_id"] != "dm"] or discord.get("routes", [])
    log("limit", "hit by", display_name(rec), "resets", time.strftime("%H:%M", time.localtime(resets)) if resets else "unknown",
        f"({len(back)} message{'s' if len(back) != 1 else ''} put back)")
    if fresh and chans and discord.get("token") and discord_bot[0]:
        when = f"resets {time.strftime('%a %H:%M', time.localtime(resets))}" if resets else "no reset time given"
        try:
            sent = discordlink.post(discord["token"], chans[0]["channel_id"],
                                    f"⏸️ **Usage limit reached** ({when}; hit by {display_name(rec)}). Agents hold their messages and schedules wait. "
                                    "Press Resume after switching accounts; it also lifts by itself at the reset, or when any agent's turn goes through.",
                                    None, discordlink.buttons(["Resume now"], "pr"))
            with lock:
                usage_limit["discord_out"] = {"channel_id": chans[0]["channel_id"], "message_id": str((sent[-1] or {}).get("id", "")) if sent else ""}
        except discordlink.DiscordError as err:
            log("discord", "limit post failed:", err)
    return 200, {"limit": limit_view()}


def lift_limit(why):
    with lock:
        if not usage_limit:
            return 200, {"limit": None}
        was = dict(usage_limit)
        usage_limit.clear()
    log("limit", "lifted:", why)
    close_buttons(was)
    out = was.get("discord_out") or {}
    if out.get("channel_id") and discord.get("token") and discord_bot[0]:
        threading.Thread(target=lambda: discordlink.post(discord["token"], out["channel_id"], f"▶️ Usage limit lifted ({why}): agents carry on with what was held.", out.get("message_id")),
                         daemon=True).start()
    return 200, {"limit": None}


def limit_view():
    return {k: v for k, v in usage_limit.items() if k != "discord_out"} or None


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


def ack(sid, ids):
    """The plugin says these messages reached its session: stop handing them out."""
    if isinstance(ids, list) and ids:
        with lock:
            mine = handed.get(sid, {})
            for mid in ids:
                mine.pop(str(mid), None)


def clear_context(sid, by):
    """You ask an agent to /clear its context; it does once its current turn ends."""
    with lock:
        rec = sessions.get(sid)
        if rec is None or not is_live(rec):
            return 404, {"error": "that agent isn't running"}
        rec["clear"] = {"by": by, "ts": time.time()}
        name = display_name(rec)
    log("clear", name, "by", by)
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


def remind(item):
    """Halfway to expiring, an approval nobody has answered is raised again under its Discord post. Returns when."""
    out = item.get("discord_out") or {}
    if out.get("message_id"):
        post_async([(out["channel_id"], f"Still waiting for your approval: {item['id']} expires in {SPAWN_TTL_S // 7200} hours.", out["message_id"])])
    log("approval", item["id"], "reminded")
    return time.time()


def lapse(item, what, told):
    """An approval that expired unanswered: logged, its Discord buttons closed with a note under them, and its
    space lead told as well as the agent that asked, which may be gone by now. Called with the lock held."""
    req = item["requester"]
    log("approval", item["id"], "expired", req.get("name", ""), what)
    out = item.get("discord_out") or {}
    if out.get("message_id"):
        close_buttons(item)
        post_async([(out["channel_id"], f"Expired: nobody approved {item['id']} within a day. {req.get('name') or 'The agent'} can ask again.", out["message_id"])])
    lead = space_lead(space_of({"id": req.get("id", ""), "cwd": req.get("cwd", "")})[0])[0] if req.get("cwd") else ""
    if lead and lead != req.get("cwd"):
        told({"id": item["id"], "requester": {"cwd": lead}}, f"{req.get('name') or 'An agent'}'s request {item['id']} to {what} expired: your user didn't approve it within a day. It's off unless someone asks again.")


def reaper():
    while True:
        time.sleep(10)
        try:
            reap()
        except Exception as err:  # one bad pass must not stop reaping, schedules and the limit for good
            log("reaper failed:", repr(err))


def reap():
    now = time.time()
    with lock:
        for sid in [s for s, r in sessions.items() if now - r["last_seen"] > config["peer_ttl_s"] * 10]:
            session_gone(sid)
        for sp in [x for x in spawns.values() if x["status"] == "pending"]:
            if now - sp["created"] > SPAWN_TTL_S:
                sp["status"], sp["updated"] = "expired", now
                spawn_told(sp, f"Your request {sp['id']} to start an agent in {sp['dir']} expired: your user didn't answer within a day.")
                lapse(sp, f"start an agent in {tilde_path(sp['dir'])}", spawn_told)
            elif now - sp["created"] > SPAWN_TTL_S / 2 and not sp.get("reminded"):
                sp["reminded"] = remind(sp)
            else:
                continue
            save_spawns()
        for c in [x for x in schedules.values() if x["status"] == "pending"]:
            if now - c["created"] > SPAWN_TTL_S:
                c["status"], c["updated"], c["next_run"] = "expired", now, None
                schedule_told(c, f"Your schedule {c['id']} expired: your user didn't approve it within a day.")
                lapse(c, f"schedule {tilde_path(c['folder'])} ({when_text(c)})", schedule_told)
            elif now - c["created"] > SPAWN_TTL_S / 2 and not c.get("reminded"):
                c["reminded"] = remind(c)
            else:
                continue
            save_schedules()
        lapsed = usage_limit and now >= (usage_limit.get("resets_at") or usage_limit["since"] + LIMIT_FALLBACK_S)
        # While the limit holds, a due schedule waits: it runs once the limit lifts.
        due = [] if usage_limit else [c for c in schedules.values() if c["status"] == "active" and c.get("next_run") and c["next_run"] <= now]
        starts = [fire_schedule(c, now) for c in due]
        # Messages to an agent that has gone for an hour are not coming back.
        for sid in [s for s in inboxes if s not in sessions]:
            kept = [m for m in inboxes[sid] if now - m["ts"] < 3600]
            expire([m for m in inboxes[sid] if now - m["ts"] >= 3600])
            if kept:
                inboxes[sid] = kept
            else:
                inboxes.pop(sid)
    if lapsed:
        lift_limit("its reset time came" if usage_limit.get("resets_at") else "agents try again after five hours")
    for c in filter(None, starts):
        start_for_schedule(c)


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
                "trusted_bots": discord.get("trusted_bots", []), "allowed_users": discord.get("allowed_users", []),
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
            for k in ("trusted_bots", "allowed_users"):
                if k in b:
                    raw = b[k]
                    ids = re.findall(r"\d{15,22}", raw if isinstance(raw, str) else " ".join(map(str, raw or [])))
                    discord[k] = list(dict.fromkeys(ids))
                    if bot:
                        setattr(bot, k, set(discord[k]))
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
            # ↻ Refresh asks Discord itself (fresh=1), and what it says replaces the gateway's view.
            live_view = bot.channels_live() if bot and "fresh" not in q else None
            try:
                if live_view is None:
                    live_view = discordlink.channels_rest(discord["token"])
                    if bot:
                        bot.set_channels(live_view)
                return self.reply(200, {"guilds": live_view})
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

    def stream(self, browser):
        """Server-sent events: a fresh snapshot whenever what the page shows changes (checked each second), and a
        comment every 15 s so proxies keep the connection. Ends when the browser leaves or is signed out."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        last, quiet = "", 0
        try:
            while True:
                if browser:
                    with lock:
                        if not (web_sessions.get(browser, 0) > time.time()):
                            return
                snap = snapshot()
                # Ages tick every second; they aren't a change worth sending.
                for p in snap["peers"]["peers"]:
                    p.pop("last_seen_s", None)
                body = json.dumps(snap)
                if body != last:
                    self.wfile.write(f"data: {body}\n\n".encode())
                    self.wfile.flush()
                    last, quiet = body, 0
                elif quiet >= 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    quiet = 0
                quiet += 1
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            out = json.loads(self.rfile.read(n))
        except ValueError:
            return {}
        return out if isinstance(out, dict) else {}

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

        if rest == ["snapshot"] and method == "GET":
            return self.reply(200, snapshot())
        if rest == ["stream"] and method == "GET":
            return self.stream(browser)

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
                checked = {k: config_value(k, v) for k, v in self.body().items() if k in DEFAULT_CONFIG}
                bad = [why for _, why in checked.values() if why]
                if bad:
                    return self.reply(400, {"error": "; ".join(bad)})
                with lock:
                    config.update({k: v for k, (v, _) in checked.items()})
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
            return self.reply(200, {"host": HOSTNAME, "space": space, "peers": peers, "leads": leads_view()})

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
                # An agent you approved starting joins the space it was asked for. A space set for one agent, as older
                # versions allowed, becomes its folder's.
                old_override = clean_space(config["sessions"].get(sid, {}).pop("space", ""))
                want = spawn_spaces.pop(rec.get("cwd", ""), "") or old_override
                if want and rec.get("cwd") and want != space_of(rec)[0] and not (config.get("folder_spaces") or {}).get(rec["cwd"]):
                    config.setdefault("folder_spaces", {})[rec["cwd"]] = want
                    rec.pop("_space", None)
                if want or old_override:
                    save_config()
                set_busy(rec, b.get("busy", False))
                rec["last_seen"] = time.time()
                sessions[sid] = rec
                note_lone_leads()
                old = b.get("succeeds")
                if old and old != sid:
                    # The same conversation after a /clear or resume: keep its name, settings and messages.
                    prev = sessions.pop(old, None)
                    succeeded_by[old] = sid
                    for gone in list(succeeded_by)[:-500]:
                        succeeded_by.pop(gone)
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
                    if rec.get("busy") and not b["busy"]:
                        discord_turn_done(rec)
                    set_busy(rec, b["busy"])
                if b.get("activity"):
                    discord_activity(rec, b["activity"])
                if isinstance(b.get("context"), (int, float)):
                    rec["context"] = max(0, min(100, int(b["context"])))
                if isinstance(b.get("branch"), str):
                    rec["branch"] = b["branch"][:200]
                note_lone_leads()
                if b.get("summary") is not None:
                    rec["summary"] = b["summary"][:500]
                    if b.get("summary_kind") in ("model", "manual"):
                        remember_dir(rec)
                cfg = client_config(sid)
            ack(sid, b.get("ack"))
            inbox = [] if cfg["muted"] else take_inbox(sid, track="ack" in b)
            with lock:
                mine = reach(rec)
            others = sum(1 for p in local_peers() if p["id"] != sid and peer_reach(p) & mine) + len(config.get("remotes", []))
            out = {"name": display_name(rec), "config": cfg, "inbox": inbox, "peer_count": others, "limit": limit_view()}
            if rec.get("terminate"):
                out["terminate"] = rec["terminate"]
            if rec.get("clear"):
                out["clear"] = rec.pop("clear")
            if rec.get("run"):
                out["run"] = rec.pop("run")
            return self.reply(200, out)

        if len(rest) == 3 and rest[0] == "sessions" and rest[2] == "config" and method == "POST":
            # One agent's settings, merged here: a page or plugin holding an old copy of every agent's
            # settings must not overwrite changes made since.
            sid = rest[1]
            b = self.body()
            with lock:
                cur = config["sessions"].setdefault(sid, {})
                for k in ("nickname", "muted", "auto_wake"):
                    if k in b:
                        cur[k] = b[k]
                # A role set here is this agent's alone, kept across /clear and resume; empty falls back to the folder's.
                if "role" in b:
                    cur["role"] = str(b["role"] or "").strip()[:ROLE_MAX]
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

        if len(rest) == 3 and rest[0] == "sessions" and rest[2] in ("space", "space-file") and method == "POST":
            with lock:
                rec = sessions.get(rest[1])
            if rec is None:
                return self.reply(404, {"error": "unknown agent"})
            # From /peerlink-spaces in that agent: its folder's space, or the parent of the space it's in.
            b = self.body()
            code, out = set_parent(space_of(rec)[0], b.get("parent")) if "parent" in b else set_folder_space(rec.get("cwd", ""), b.get("space"))
            with lock:
                return self.reply(code, {**out, "config": client_config(rec["id"])})

        if len(rest) == 2 and rest[0] == "sessions" and method == "DELETE":
            with lock:
                session_gone(rest[1])
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
        if rest == ["schedules"] and method == "GET":
            with lock:
                return self.reply(200, {"schedules": [{**c, "when_text": when_text(c)} for c in sorted(schedules.values(), key=lambda c: -c["created"])]})
        if rest == ["schedules"] and method == "POST":
            b = self.body()
            if browser and not self.key_ok():
                code, out = create_schedule(b)
            else:
                with lock:
                    by = sessions.get(str(b.get("by") or ""))
                if by is None:
                    return self.reply(403, {"error": "unknown agent; register again"})
                code, out = create_schedule(b, by)
            return self.reply(code, out)
        if len(rest) == 3 and rest[0] == "schedules" and rest[2] == "cancel" and method == "POST":
            if browser and not self.key_ok():
                code, out = cancel_schedule(rest[1])
            else:
                with lock:
                    by = sessions.get(str(self.body().get("by") or ""))
                if by is None:
                    return self.reply(403, {"error": "unknown agent; register again"})
                code, out = cancel_schedule(rest[1], by)
            return self.reply(code, out)
        if rest == ["spawns"] and method == "GET":
            with lock:
                return self.reply(200, {"spawns": sorted(spawns.values(), key=lambda x: -x["created"])})
        if len(rest) == 3 and rest[0] == "sessions" and rest[2] == "ack" and method == "POST":
            ack(rest[1], self.body().get("ids"))
            return self.reply(200, {"ok": True})
        if len(rest) == 3 and rest[0] == "sessions" and rest[2] == "limit" and method == "POST":
            # The agent's plugin: its turn died on the usage limit, or (lifted) one went through again.
            with lock:
                rec = sessions.get(rest[1])
            if rec is None:
                return self.reply(404, {"error": "unknown agent; register again"})
            b = self.body()
            code, out = lift_limit(f"{display_name(rec)}'s turn went through") if b.get("lifted") else hit_limit(rec, b)
            return self.reply(code, out)
        if rest == ["limit", "resume"] and method == "POST":
            # Only you, in the browser; an agent lifts it by getting a turn through.
            if not browser or self.key_ok():
                return self.reply(403, {"error": "the usage limit is resumed from the web UI or Discord"})
            code, out = lift_limit("resumed in the web UI")
            return self.reply(code, out)
        if len(rest) == 3 and rest[0] == "sessions" and rest[2] == "clear" and method == "POST":
            # Only you, in the browser: no agent wipes another's context.
            if not browser or self.key_ok():
                return self.reply(403, {"error": "an agent's context is cleared from the web UI"})
            code, out = clear_context(rest[1], "the web UI")
            return self.reply(code, out)
        if len(rest) == 3 and rest[0] == "sessions" and rest[2] == "terminate" and method == "POST":
            # Only you, in the browser: no agent ends another.
            if not browser or self.key_ok():
                return self.reply(403, {"error": "agents are ended from the web UI or Discord"})
            code, out = terminate(rest[1], "the web UI")
            return self.reply(code, out)
        if len(rest) == 3 and rest[0] == "spaces" and rest[2] == "lead" and method == "POST":
            # Only you, in the browser: no agent or bot makes itself, or another, a lead.
            if not browser or self.key_ok():
                return self.reply(403, {"error": "a space's lead is set in the web UI"})
            code, out = set_lead(rest[1], str(self.body().get("cwd") or ""))
            return self.reply(code, out)
        if rest == ["folders", "space"] and method == "POST":
            if not browser or self.key_ok():
                return self.reply(403, {"error": "a folder's space is set in the web UI"})
            b = self.body()
            code, out = set_folder_space(str(b.get("cwd") or ""), b.get("space"))
            return self.reply(code, out)
        if len(rest) == 3 and rest[0] == "spaces" and rest[2] == "parent" and method == "POST":
            if not browser or self.key_ok():
                return self.reply(403, {"error": "where a space sits is set in the web UI"})
            code, out = set_parent(rest[1], self.body().get("parent"))
            return self.reply(code, out)
        if rest == ["members"] and method == "POST":
            # Only you, in the browser: no agent or bot adds itself, or another, to a space.
            if not browser or self.key_ok():
                return self.reply(403, {"error": "an agent's spaces are set in the web UI"})
            body = self.body()
            spaces = body.get("spaces")
            code, out = set_members(str(body.get("cwd") or ""), spaces if isinstance(spaces, list) else [])
            return self.reply(code, out)
        if rest == ["web", "send"] and method == "POST":
            # Only you, in the browser: it goes out in your name.
            if not browser or self.key_ok():
                return self.reply(403, {"error": "messages from you are sent from the web UI"})
            code, out = web_send(self.body())
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
                    return self.reply(200, {"messages": messages[-max(1, int(q.get("limit", ["100"])[0])):]})
            if method == "POST":
                b = self.body()
                if not self.is_local() and "@" not in str(b.get("from", "")):
                    # Another machine's daemon forwards its agents as id@host; it can't speak as you or as a local agent.
                    return self.reply(403, {"error": "a remote message comes from id@host"})
                code, res = send(b)
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

    def handle_route(self, method):
        try:
            self.route(method)
        except (ValueError, TypeError, KeyError, AttributeError) as err:  # a malformed request, e.g. ?limit=abc
            try:
                self.reply(400, {"error": f"bad request: {err}"})
            except OSError:
                pass

    def do_GET(self):
        self.handle_route("GET")

    def do_POST(self):
        self.handle_route("POST")

    def do_PUT(self):
        self.handle_route("PUT")

    def do_DELETE(self):
        self.handle_route("DELETE")


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
    load_schedules()
    load_dirs()
    file_pending_issues()
    load_discord()
    threading.Thread(target=reaction_worker, name="discord-reactions", daemon=True).start()
    clean_stale_reactions()
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
:root{color-scheme:light dark;--bg:#faf9f7;--side:#f2f0ec;--fg:#1f1e1c;--mut:#6b6862;--line:#e4e1db;--card:#fff;--acc:#c96442;--ok:#2f7d4f;--warn:#b7791f;--bad:#c0392b;--well:rgba(127,127,127,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#1c1b19;--side:#161513;--fg:#ecebe8;--mut:#9b978f;--line:#34322e;--card:#252421;--acc:#e0805e;--ok:#5cb57f;--warn:#e0b04a;--bad:#e5675a;--well:rgba(255,255,255,.03)}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 ui-sans-serif,system-ui,-apple-system,sans-serif}
.app{display:flex;min-height:100vh}
nav{flex:0 0 210px;background:var(--side);border-right:1px solid var(--line);padding:18px 10px;position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:2px}
nav .brand{font-weight:700;font-size:16px;padding:0 10px 4px}nav .host{font-size:11px;color:var(--mut);padding:0 10px 14px}
nav a{display:flex;justify-content:space-between;align-items:center;padding:7px 10px;border-radius:6px;color:var(--fg);text-decoration:none}
nav a:hover{background:var(--line)}nav a.on{background:var(--card);box-shadow:0 0 0 1px var(--line);font-weight:600}
nav .count{font-size:11px;color:var(--mut);font-weight:400}nav .grp{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);padding:14px 10px 4px}
main{flex:1;min-width:0;max-width:1100px;padding:22px 24px 60px}
body.wide main{max-width:none}
#peers.charts{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start}#peers.charts>.chart{margin:0;max-width:100%}
.ltree{display:flex;flex-direction:column;gap:14px}
.lspace-h{display:flex;gap:8px;align-items:baseline;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin-bottom:6px}.lspace-h .small{text-transform:none;letter-spacing:0;font-weight:400}
.lsub{margin-left:14px;border-left:2px solid var(--line);padding-left:12px;display:flex;flex-direction:column;gap:6px;margin-top:6px}.lsub.led{border-left-color:var(--acc)}
.lsub>.lspace,.lflat>.lspace{margin-top:8px}.lflat{display:flex;flex-direction:column;gap:6px}
.lrow{display:grid;grid-template-columns:minmax(150px,220px) minmax(0,1fr) auto;gap:4px 14px;align-items:baseline;width:100%;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:7px 12px;text-align:left;font:inherit;color:inherit;cursor:pointer}
.lrow:hover{border-color:var(--mut)}.lrow.is-lead{border-color:var(--acc)}.lrow.sel{box-shadow:0 0 0 2px var(--acc)}.lrow.off{opacity:.5;cursor:default;border-style:dashed}
.lrow .ln{font-weight:600;display:flex;align-items:center;gap:6px;min-width:0}.lrow .ln>span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lrow .lw{font-size:12px;color:var(--mut);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.lrow .lw .mono{font-size:11px}
.lrow .lr{display:flex;gap:10px;align-items:baseline;white-space:nowrap;font-size:12px}
.lrow .badge{font-size:10px;font-weight:600;color:var(--acc);border:1px solid var(--acc);border-radius:4px;padding:0 4px}.lrow .badge.only{border-style:dashed}
@media (max-width:720px){.lrow{grid-template-columns:1fr auto}.lrow .lw{grid-column:1/-1;grid-row:2;white-space:normal}}
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
.node .nm{font-weight:600;display:flex;align-items:center;gap:6px;justify-content:space-between}.node .nm>span:first-child{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.node .tag{font-size:11px;font-weight:500;color:var(--acc);white-space:nowrap}
.node .badge{display:inline-block;font-size:10px;font-weight:600;color:var(--acc);border:1px solid var(--acc);border-radius:4px;padding:0 4px;white-space:nowrap}
.node .nm .rt{flex:none;display:inline-flex;align-items:center;gap:6px}.node .badge.only{border-style:dashed}
.lrow .badge.guest,.node .badge.guest{color:var(--mut);border-color:var(--mut);font-weight:500}
.node.off{opacity:.5;cursor:default;border-style:dashed}.node.off:hover{border-color:var(--acc)}
.under{margin-left:14px;border-left:2px solid var(--acc);padding-left:12px;margin-top:8px}
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
.node .state{font-size:11px;color:var(--mut);margin-top:4px}.hot{color:var(--warn);font-weight:600}
.node .sum{font-size:12px;color:var(--mut);margin-top:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sbox{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px;min-width:160px;max-width:260px;text-align:center}
.sbox.led{border-color:var(--acc)}.spick{display:flex;flex-direction:column;gap:4px;margin-top:8px;text-align:left}.spick label{display:flex;justify-content:space-between;align-items:center;gap:6px;font-size:12px;color:var(--mut)}.spick select{font-size:12px;padding:2px 4px;max-width:150px}.sbox .small{margin-top:2px}
#detail:empty{display:none}
#detail{position:fixed;top:0;right:0;bottom:0;width:380px;max-width:100%;overflow-y:auto;background:var(--bg);border-left:1px solid var(--line);box-shadow:-8px 0 24px rgba(0,0,0,.08);padding:16px;z-index:10}
#detail .panel{border:0;padding:0;background:transparent}
.panel .top{display:flex;justify-content:space-between;align-items:center;gap:8px}.panel .name{font-size:16px}
.panel .x{padding:2px 8px;font-size:13px}.facts{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;margin:10px 0 4px;font-size:13px}
.facts .k{color:var(--mut)}.threads{margin-top:8px;display:flex;flex-direction:column;gap:4px}.threads .k{color:var(--mut)}.panel form{margin-top:12px;border-top:1px solid var(--line);padding-top:12px;display:flex;flex-direction:column;gap:12px}
.panel label{display:flex;flex-direction:column;gap:4px;font-size:13px;font-weight:500}.panel label .hint{font-weight:400;font-size:12px;color:var(--mut)}
.panel label.check{flex-direction:row;align-items:center;gap:8px}.ok{color:var(--ok)}
#stalebar .mono{white-space:nowrap}#stalebar{background:var(--card);border:1px solid var(--warn);border-radius:10px;padding:10px 14px;margin-bottom:14px}
#limitbar{background:var(--card);border:1px solid var(--warn);border-radius:10px;padding:10px 14px;margin-bottom:14px}
#offline{display:none;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:14px;color:var(--mut)}
#signedout{display:none;background:var(--card);border:1px solid var(--warn);border-radius:10px;padding:10px 14px;margin-bottom:14px}
.msgbar{display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap}.msgbar select{width:auto}
.mlist{display:flex;flex-direction:column;gap:8px}
.task{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.task .mh{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;font-size:13px}.task .mh .when{margin-left:auto;color:var(--mut);font-size:12px}
.task .note{margin-top:6px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}.task .tact{margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}.task .tact select{width:auto}
.task.sub{margin-left:24px}button.danger{color:var(--bad);border-color:var(--bad)}nav .count.hot:not(:empty){background:var(--acc);color:#fff;border-radius:999px;padding:1px 7px;font-weight:600}.task .ftext{margin-top:6px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;max-height:9em;overflow:auto}
.task .tact input{flex:1;min-width:160px}.tchip.new,.tchip.bug{color:var(--warn);border-color:var(--warn)}.tchip.accepted,.tchip.idea{color:var(--acc);border-color:var(--acc)}.tchip{font-size:11px;border-radius:999px;padding:1px 8px;border:1px solid var(--line);color:var(--mut)}
.fl[hidden]{display:none}.seg{display:inline-flex}.seg button{border-radius:0}.seg button:first-child{border-radius:8px 0 0 8px}.seg button:last-child{border-radius:0 8px 8px 0;margin-left:-1px}.seg button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.board{display:grid;grid-template-columns:repeat(4,minmax(0,1fr)) auto;gap:10px;align-items:start;margin-bottom:14px}
.lane-h{font-size:12px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin:6px 0}
.col{background:var(--well);border:1px solid var(--line);border-radius:10px;padding:8px;min-height:80px;display:flex;flex-direction:column;gap:8px}
.col-h{font-size:12px;font-weight:600;color:var(--mut);display:flex;justify-content:space-between}
.col.can{border-color:var(--acc);border-style:dashed}.col.over,.tdrop .agent.over{background:rgba(217,119,87,.12);border-color:var(--acc)}
.col.shut{min-width:120px;max-width:130px}.col.shut .tcard{display:none}.col.shut.open{max-width:none;min-width:200px}.col.shut.open .tcard{display:block}
.col-h button{font:inherit;color:inherit;background:none;border:0;padding:0;cursor:pointer}
.tcard{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:13px;cursor:grab}
.tcard.dragging{opacity:.4}.tcard .tt{font-weight:600;overflow-wrap:anywhere}.tcard .meta{font-size:12px;color:var(--mut);margin-top:3px}.tcard .meta.by{display:flex;gap:8px}.tcard .meta.by>span:first-child{min-width:0;overflow-wrap:anywhere}.tcard .meta.by .age{margin-left:auto;white-space:nowrap}.col .none{font-size:12px;color:var(--mut);padding:4px 2px}
.tcard .bar{height:4px;border-radius:2px;background:var(--line);margin-top:6px;overflow:hidden}.tcard .bar i{display:block;height:100%;background:var(--ok)}
.tcard .more{display:none;margin-top:6px}.tcard.open .more{display:block}.tcard .more .tact{margin-top:6px;display:flex;gap:6px;flex-wrap:wrap}.tcard .more select{width:auto;max-width:100%}
.tdrop{display:none;gap:6px;flex-wrap:wrap;align-items:center;margin:0 0 10px;font-size:12px;color:var(--mut)}.tdrop.on{display:flex}
.tdrop .agent{border:1px dashed var(--line);border-radius:999px;padding:3px 10px;color:var(--fg,inherit)}
@media (max-width:760px){.board{grid-template-columns:1fr}.col.shut{max-width:none}.col{min-height:0}}
.tchip.open,.tchip.in_progress{color:var(--acc);border-color:var(--acc)}.tchip.done{color:var(--ok);border-color:var(--ok)}.tchip.failed{color:var(--bad);border-color:var(--bad)}
.ptabs{display:flex;gap:4px;margin-top:12px;border-bottom:1px solid var(--line)}.ptabs button{background:transparent;color:var(--mut);border:0;border-bottom:2px solid transparent;border-radius:0;padding:6px 10px}
.ptabs button.on{color:var(--fg);border-bottom-color:var(--acc)}.ptabs+form{border-top:0}.ptasks{margin-top:12px;display:flex;flex-direction:column;gap:8px}.ptasks .k{color:var(--mut)}
.msg{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;cursor:pointer}
.msg .mh{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;font-size:13px}.msg .mh .when{margin-left:auto;color:var(--mut);font-size:12px}
.msg .mt{margin-top:6px;white-space:pre-wrap;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.msg.open .mt{display:block}.msg .chip{font-size:11px;border-radius:999px;padding:1px 8px;border:1px solid var(--line);color:var(--mut)}
.chip.queued{border-color:var(--warn);color:var(--warn)}.chip.expired{border-color:var(--bad);color:var(--bad)}.chip.reply{color:var(--acc);border-color:var(--acc)}
.msg.inthread{margin-left:22px;border-left:3px solid var(--acc)}
@media (min-width:721px){body.drawer main{padding-right:404px;max-width:1504px}body.drawer.wide main{max-width:none}}
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
.name{font-weight:600}.small{font-size:12px}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;overflow-wrap:anywhere}
.pill{display:inline-block;font-size:11px;border:1px solid var(--line);border-radius:999px;padding:1px 8px;color:var(--mut)}
input:not([type=checkbox]):not([type=radio]),textarea{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit}
select{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px;font:inherit}
input::placeholder,textarea::placeholder{color:var(--mut);opacity:.8}
textarea{min-height:180px;font-family:ui-monospace,Menlo,monospace;font-size:12px}
label{display:block;margin:8px 0 4px;font-size:12px;color:var(--mut)}
.row{display:flex;gap:12px;flex-wrap:wrap}.row>div{flex:1 1 180px}
button{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:7px 14px;font:inherit;cursor:pointer}
button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line)}
button:hover:not(:disabled){filter:brightness(1.06)}button.ghost:hover:not(:disabled){border-color:var(--mut);filter:none}button:disabled{opacity:.55;cursor:default}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}input:focus-visible,select:focus-visible,textarea:focus-visible{outline-offset:0;border-color:var(--acc)}
.fl{display:inline-flex;gap:6px;align-items:center;white-space:nowrap}.fl label{margin:0}
input.search{width:auto;flex:1 1 220px;max-width:360px}
.panel .role{margin-top:8px;font-size:13px;white-space:pre-wrap;overflow-wrap:anywhere;border-left:2px solid var(--acc);padding-left:8px}.panel .role .k{color:var(--mut);font-size:12px}
.panel textarea[name=role]{min-height:64px;font:inherit;font-size:13px}
.sendbox{display:flex;flex-direction:column;gap:8px;margin-top:12px}.sendbox textarea{min-height:64px;font:inherit;font-size:13px}.sendbox .check{margin:0;font-weight:400}
#newtask{max-width:640px;margin:0 0 12px}#newtask[hidden]{display:none}#newtask select{width:100%}
details.adv{margin:0;border-top:1px dashed var(--line);padding-top:8px}details.adv summary{font-size:13px;color:var(--mut)}details.adv[open]{display:flex;flex-direction:column;gap:12px}
#cfg details.adv{margin-top:12px}#cfg details.adv[open]{gap:0}
button.copy{padding:0 6px;font-size:11px;margin-left:4px;font-family:inherit}
details.how{margin:-10px 0 16px;color:var(--mut)}details.how summary{font-size:13px;color:var(--acc);width:max-content}details.how p{margin:6px 0 0;max-width:760px}details.how ul{margin:6px 0 0;padding-left:18px;max-width:760px}
details{margin-top:8px}summary{cursor:pointer;font-size:12px;color:var(--mut)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--mut);font-weight:500}.msgtext{white-space:pre-wrap;max-height:6em;overflow:auto}
.tablewrap{overflow-x:auto}.empty{color:var(--mut);padding:14px 0}
.check{display:flex;align-items:center;gap:6px;margin-top:8px}
.err{color:var(--bad)}
@media (max-width:720px){.app{flex-direction:column}
 nav{position:sticky;top:0;z-index:5;height:auto;flex:none;flex-direction:row;align-items:center;overflow-x:auto;scrollbar-width:none;border-right:0;border-bottom:1px solid var(--line);padding:8px 12px}
 nav::-webkit-scrollbar{display:none}nav .brand{padding:6px 8px 6px 4px;flex:none}nav .host{display:none}nav a{padding:6px 10px;flex:none;white-space:nowrap}nav .count{margin-left:6px}
 nav .grp{padding:0;width:1px;height:18px;background:var(--line);overflow:hidden;color:transparent;flex:none;margin:0 4px}
 details.how{margin-top:-8px}
 main{padding:16px}}
</style></head><body><div class="app">
<nav>
<div class="brand">Peerlink</div><div class="host" id="hostline"></div>
<a href="#inbox" data-nav="inbox">Inbox <span class="count hot" id="n-inbox"></span></a>
<a href="#agents" data-nav="agents">Agents <span class="count" id="n-agents"></span></a>
<a href="#tasks" data-nav="tasks">Tasks <span class="count" id="n-tasks"></span></a>
<a href="#schedules" data-nav="schedules">Schedules <span class="count" id="n-sched"></span></a>
<a href="#messages" data-nav="messages">Messages <span class="count" id="n-msgs"></span></a>
<div class="grp" aria-hidden="true">Set up</div>
<a href="#spaces" data-nav="spaces">Spaces <span class="count" id="n-spaces"></span></a>
<a href="#channels" data-nav="channels">Channels <span class="count" id="n-discord"></span></a>
<div class="grp" aria-hidden="true">Peerlink</div>
<a href="#feedback" data-nav="feedback">Feedback <span class="count" id="n-feedback"></span></a>
<a href="#settings" data-nav="settings">Settings</a>
<a href="#controls" data-nav="controls">Controls</a>
</nav>
<main><div id="limitbar" style="display:none" class="small"></div><div id="stalebar" style="display:none" class="small"></div><div id="offline">Can't reach the peerlink daemon. This page reconnects once it's back.</div><div id="signedout">Signed out. Run <span class="mono">/peerlink web</span> in any agent to sign in again.</div>

<section id="agents"><h1>Agents</h1><p class="lead">Your live agents, by space. Click one to see or change it.</p>
<div class="msgbar"><span class="seg" role="group" aria-label="Layout"><button class="ghost small" data-alayout="chart">Chart</button><button class="ghost small" data-alayout="list">List</button></span>
<input id="asearch" class="search" type="search" placeholder="Search agents" title="Search by name, folder, branch or summary" aria-label="Search agents"></div>
<details class="how"><summary>How it works</summary><ul>
<li>Each space shows its lead first, outlined in orange, with its members beneath it. A space can sit inside another: its lead then sits with that space's agents, with its own space beneath it.</li>
<li>Work sent to a space goes to its lead: the agent you picked, or else the one that was alone in it. Pick leads, and which space sits inside which, on the Spaces page.</li>
<li>An agent only sees and messages agents in its own space. A lead also sees the space its space sits inside.</li>
<li>To move an agent, open it and set its space on the Details tab: its folder keeps it, for every agent opened there.</li>
<li>The percentage is how full the agent's context window is. A yellow dot means it's working, a green one that it's idle.</li></ul></details><div class="orgwrap"><div id="peers"></div><div id="detail"></div></div></section>

<section id="inbox"><h1>Inbox</h1><p class="lead">Everything waiting on you, the longest waiting first. On Discord, <span class="mono">/inbox</span> shows the same list, and you answer by number, like <span class="mono">1 done</span>.</p>
<div class="msgbar"><button class="ghost small" id="notifybtn" data-notify>Desktop alerts: off</button></div>
<div class="mlist" id="inboxlist"></div></section>

<section id="spaces"><h1>Spaces</h1>
<p class="lead">Which agents can see and message each other.</p>
<details class="how"><summary>How spaces work</summary><p>An agent is in the space you put its folder in, from its Details tab on the Agents page. A folder you haven't placed takes the space in the nearest <span class="mono">.peerlink</span> file at or above it, else it's <b>unassigned</b>. Here you pick each space's lead, which gets work sent to the space, and which space it sits inside: its lead then speaks for it there.</p></details>
<div id="spacelist"></div>
</section>

<section id="schedules"><h1>Schedules</h1><p class="lead">Prompts the daemon sends an agent at a time or on a repeat, with no terminal open. When that agent isn't running, another in its folder gets it, or a new one starts there. Agents add them with their <span class="mono">schedule</span> tool; one that may start an agent waits for you in the Inbox.</p>
<form class="card" id="schedform"><div class="addline"><input name="target" class="grow mono" type="text" placeholder="Agent, or a folder like ~/Repos/acme" list="schedfolders"><datalist id="schedfolders"></datalist>
<input name="when" type="text" placeholder="0 9 * * 1-5, or in 2h" title="A cron (minute hour day month weekday) repeats; 'in 30m', 'in 2h', 'in 1d' or 2026-10-25 09:00 runs once" style="width:190px"></div>
<textarea name="prompt" rows="2" placeholder="What the agent should do when it runs" style="margin-top:8px;width:100%"></textarea>
<div class="addline" style="margin-top:8px"><label class="check small"><input type="checkbox" name="start" checked> Start an agent when none is running</label><button type="submit">Add schedule</button><span class="small" id="schedmsg"></span></div></form>
<div id="schedlist" style="margin-top:12px;display:flex;flex-direction:column;gap:8px"></div></section>

<section id="messages"><h1>Messages</h1><p class="lead">Recent messages between agents, newest first. Click one to read it all.</p>
<div class="msgbar"><span class="fl"><label class="small mut" for="msgspace">Space</label><select id="msgspace"><option value="">All spaces</option></select></span>
<span class="fl"><label class="small mut" for="msgagent">Agent</label><select id="msgagent"><option value="">Everyone</option></select></span>
<input id="msgq" class="search" type="search" placeholder="Search messages" aria-label="Search messages"></div>
<div class="mlist" id="msgs"></div></section>

<section id="tasks"><h1>Tasks</h1><p class="lead">Work handed between agents. Click a card for details, or drag it.</p>
<details class="how"><summary>How tasks work</summary><ul>
<li>An agent hands out a task with <span class="mono">kind: task</span>. On Discord, start a post with <span class="mono">Task:</span>. A task stays open until its owner marks it done or failed, and whoever asked gets the owner's note.</li>
<li>A task sent to a lead is the lead's to split up. Its subtasks count toward it.</li>
<li>Drag a card to Cancelled to cancel it, from Done, Failed or Cancelled to Open to reopen it, or onto an agent to hand it over. Only the owner moves a task to In progress, Done or Failed. If the owner changed a task after the board was drawn, your move is refused.</li></ul></details>
<div class="msgbar"><button class="small" data-newtask>+ New task</button><span class="seg" role="group" aria-label="Layout"><button class="ghost small" data-tlayout="board">Board</button><button class="ghost small" data-tlayout="list">List</button></span>
<span id="tstatusw" class="fl"><label class="small mut" for="tstatus">Show</label><select id="tstatus"><option value="live">Open and in progress</option><option value="done">Done</option><option value="failed">Failed</option><option value="cancelled">Cancelled</option><option value="">All</option></select></span>
<span class="fl"><label class="small mut" for="towner">Owner</label><select id="towner"><option value="">Everyone</option></select></span>
<span class="fl"><label class="small mut" for="treq">Asked by</label><select id="treq"><option value="">Anyone</option></select></span>
<span class="fl"><label class="small mut" for="tspace">Space</label><select id="tspace"><option value="">All spaces</option></select></span>
<span id="tgroupw" class="fl"><label class="small mut" for="tgroup">Rows</label><select id="tgroup"><option value="">One board</option><option value="space">By space</option><option value="owner">By owner</option></select></span></div>
<form id="newtask" class="card sendbox" hidden><label>To<select name="to" required></select></label>
<label>Task<textarea name="text" rows="3" placeholder="First line is the title. Add details below it." required></textarea></label>
<div class="addline"><button type="submit">Send task</button><button type="button" class="ghost" data-newtask>Cancel</button><span class="small" id="newtaskmsg"></span></div></form>
<div id="tdrop" class="tdrop"></div>
<div class="mlist" id="tasklist"></div></section>

<section id="feedback"><h1>Feedback</h1><p class="lead">Bugs and ideas agents report about peerlink. Accept one to hand it to the peerlink agent as a task.</p>
<div class="msgbar"><span class="fl"><label class="small mut" for="fstatus">Show</label><select id="fstatus"><option value="new">New</option><option value="accepted">Accepted</option><option value="done">Done</option><option value="dismissed">Dismissed</option><option value="">All</option></select></span>
<span class="fl"><label class="small mut" for="ftype">Type</label><select id="ftype"><option value="">Bugs and ideas</option><option value="bug">Bugs</option><option value="idea">Ideas</option></select></span></div>
<div class="mlist" id="fblist"></div></section>

<section id="channels"><h1>Channels</h1>
<p class="lead">Talk to your agents from outside the terminal.</p>
<h2>Discord</h2>
<p class="lead">Each channel you map has a <b>master</b> agent that gets your messages. Only you, the bot's owner, can talk to it.</p>
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
<div class="row"><div><label for="wf">Wake an idle agent for</label><select name="wake_for" id="wf" style="width:100%"><option value="needed">Questions, tasks, your messages and answers (saves tokens)</option><option value="all">Every message</option></select></div>
<div><label for="wd">Wait before waking for another agent's message (s)</label><input type="number" name="wake_delay_s" id="wd" min="0" max="120"></div></div>
<div class="mut small">Each wake-up is a full turn that re-reads the agent's context. Waiting a few seconds lets messages that arrive together share one turn; your own messages never wait.</div>
<div class="check"><input type="checkbox" name="roster_in_prompt" id="rp"><label for="rp" style="margin:0">Show each agent the live agent list when it changes</label></div>
<div class="check"><input type="checkbox" name="auto_summary" id="as"><label for="as" style="margin:0">Keep agent summaries current automatically</label></div>
<div class="row"><div><label>Summary model</label><input type="text" name="summary_model"></div><div></div></div>
<div class="check"><input type="checkbox" name="clear_between_tasks" id="cbt"><label for="cbt" style="margin:0">Agents clear their own context after each task, saving notes to memory first</label></div>
<div class="row"><div><label for="cnt">Ask an agent to save and clear once its context passes (tokens, 0 for never)</label><input type="number" name="clear_nudge_tokens" id="cnt" min="0" step="50000"></div><div></div></div>
<div class="mut small">A large context is re-read on every turn and drains the usage limit fast. The ask arrives with the agent's next prompt, so it clears at a stopping point.</div>
<label>Guidance for every agent</label><textarea name="guidance" id="guidance"></textarea><div class="mut small">Leave empty to use the default, shown in grey.</div>
<details class="adv"><summary>Other machines</summary>
<label>Their daemons, one URL per line</label><textarea name="remotes" style="min-height:60px"></textarea>
<div class="row"><div><label>Listen host (0.0.0.0 to accept other machines; needs a restart)</label><input type="text" name="listen_host"></div>
<div><label>Shared token</label><input type="text" name="token"></div></div></details>
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
// Agents and Tasks lay out across the whole window; the reading pages keep a comfortable line length.
function show(){const id=(location.hash||'#agents').slice(1);document.body.classList.toggle('wide',id==='agents'||id==='tasks');
  document.querySelectorAll('section').forEach(s=>s.classList.toggle('on',s.id===id));
  document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('on',a.dataset.nav===id))}
// Leaving Agents, or Escape, closes the agent panel: it should never linger over another page.
const closePanel=()=>{if(!sel)return;sel='';editing=false;refresh(true)};
// A page starts at its top: the browser would otherwise scroll to the section the link names, hiding its heading.
window.addEventListener('hashchange',()=>{show();closePanel();scrollTo(0,0)});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closePanel()});
function agentPanel(x,others=[]){const sc=cfg.sessions?.[x.id]||{};const setSpace=x.space_source==='set'?x.space:'';
  const wake=sc.auto_wake===true?'on':sc.auto_wake===false?'off':'default';
  const why=x.space_source==='set'?'you put its folder here':x.space_source==='file'?'from '+tilde(x.space_from):'no space set';
  const how={set:'you picked it',only:'it was the only agent in the space'}[x.leads]||'';
  const led=leadOf[x.space];
  const role=x.leads?`Leads the <b>${esc(x.space)}</b> space${x.parent?` inside <b>${esc(x.parent)}</b>`:''} <span class="mut">· ${how}</span>`:`Member${led?` · led by ${esc(led.name)}${led.live?'':' (offline)'}`:''}`;
  // Only you, here, pick a lead: the daemon refuses it from agents and bots.
  const leadBtn=x.remote||!x.space||x.space==='default'?'':x.leads?`<button class="ghost small" data-clearlead="${esc(x.space)}">Clear lead</button>`:`<button class="ghost small" data-makelead="${esc(x.space)}" data-cwd="${esc(x.cwd)}">${led?'Make lead instead of '+esc(led.name):'Make lead of '+esc(x.space)}</button>`;
  const opt=(v,t)=>`<option value="${v}" ${wake===v?'selected':''}>${t}</option>`;
  const tab=x.remote?'about':panelTab;
  return `<div class="card panel${x.leads?' is-lead':''}">
   <div class="top"><span><span class="dot ${x.busy?'busy':''}"></span><span class="name">${esc(x.name)}</span>${x.remote?` <span class="mut small">@${esc(x.host)}</span>`:''}</span><button class="ghost x" data-close title="Close">✕</button></div>
   <div class="mono mut" style="margin-top:4px">${esc(tilde(x.cwd))}${x.branch?' · '+esc(x.branch):''} <button class="ghost small copy" data-copy="${esc(x.cwd)}" title="Copy the folder path">Copy</button></div>
   ${x.role?`<div class="role"><span class="k">Role</span> ${esc(x.role)}</div>`:''}
   <div style="margin-top:8px">${esc(x.summary)||'<span class="mut">no summary yet</span>'}</div>
   ${x.remote?'':`<div class="ptabs">${[['about','Agent'],['tasks','Tasks'+(x.open_tasks?' · '+x.open_tasks:'')],['details','Details'],['controls','Controls']].map(([k,t])=>`<button class="${tab===k?'on':''}" data-ptab="${k}">${t}</button>`).join('')}</div>`}
   ${tab==='tasks'?panelTasks(x):tab==='details'?`<form id="agentform" data-id="${esc(x.id)}" data-nick="${esc(sc.nickname||'')}" data-space="${esc(setSpace)}" data-role="${esc(sc.role||'')}" data-cwd="${esc(x.cwd)}" data-also="${esc((x.also||[]).join(', '))}">
    <label>Name<input name="nickname" type="text" value="${esc(sc.nickname||'')}" placeholder="${esc(x.name)}"><span class="hint">What other agents call it. Kept for this folder.</span></label>
    <label>Role<textarea name="role" rows="3" placeholder="${x.folder_role?'From the folder: '+esc(x.folder_role):'What this agent is here for, e.g. Owns the billing API: invoices, payments, the Stripe webhook.'}">${esc(sc.role||'')}</textarea><span class="hint">For this agent only, kept across /clear and resume. The agent gets it as its brief, and other agents see it.${x.folder_role?' Leave it empty to use the role: in its folder\'s .peerlink.':' A role: line in a folder\'s .peerlink is the role of every agent there without one of its own.'}</span></label>
    <label>Space<input name="space" type="text" list="spacenames" value="${esc(setSpace)}" placeholder="${x.space_source==='file'?esc(x.space)+' (from its .peerlink)':'unassigned'}"><span class="hint">Kept for its folder: every agent opened there joins it.${x.space_source==='file'||!setSpace?'':' Leave it empty to go back to the folder\'s .peerlink.'}</span></label>
    <details class="adv"${(x.also||[]).length?' open':''}><summary>Also in other spaces</summary>
    <label>Also a member of<input name="also" type="text" value="${esc((x.also||[]).join(', '))}" placeholder="none"><span class="hint">Rarely needed: more spaces it sees and messages, on top of its own. Separate with commas.</span></label></details>
    <datalist id="spacenames">${Object.keys(leadOf).concat(livePeers.map(p=>p.space)).filter((n,i,a)=>n&&n!=='default'&&a.indexOf(n)===i).sort().map(n=>`<option value="${esc(n)}">`).join('')}</datalist>
    <div><button type="submit">Save</button> <span id="agentmsg" class="small"></span></div></form>`:tab==='controls'?`<form id="agentform" data-id="${esc(x.id)}" data-wake="${wake}" data-muted="${x.muted?'1':''}">
    <label>When a message arrives<select name="wake">${opt('default','Use the default')}${opt('on','Start a turn')}${opt('off','Wait for the next prompt')}</select></label>
    <label class="check"><input name="muted" type="checkbox" ${x.muted?'checked':''}> Mute: hold every message for this agent</label>
    <div><button type="submit">Save</button> <span id="agentmsg" class="small"></span></div></form>
    ${leadBtn?`<div class="threads"><div class="k small">Lead</div><div class="small mut">${role}</div><div>${leadBtn}</div></div>`:''}
    <div class="threads"><div class="k small">Clear its context</div><div class="small mut">Runs /clear once its current turn ends: a fresh conversation in the same folder, keeping its name, space and role. What it still owes peers is given back to it.</div>
    <div><button class="ghost small" data-clearctx="${esc(x.id)}">Clear context</button> <span class="small ok" id="clearmsg"></span></div></div>
    <div class="threads"><div class="k small">End this agent</div><div class="small mut">It finishes its current turn, then closes. If it's still running a minute later, it's stopped.</div>
    <div><button class="ghost small danger" data-terminate="${esc(x.id)}" ${x.terminating?'disabled':''}>${x.terminating?'Ending…':'Terminate'}</button></div></div>`:`<div class="facts"><span class="k">Status</span><span>${x.busy?'working':'idle'}${x.busy_since?' for '+ago(x.busy_since):''}${x.muted?' · muted':''}</span>
    <span class="k">Space</span><span><span class="pill">${esc(x.space||'default')}</span> <span class="mut">${esc(why)}</span>${(x.also||[]).length?` <span class="mut">· also in</span> ${x.also.map(n=>`<span class="pill">${esc(n)}</span>`).join(' ')}`:''}</span>
    <span class="k">Role</span><span>${role}</span>
    <span class="k">Messages</span><span>${x.sent_hour||0} sent in the last hour${x.sent_hour>=20?' · <span style="color:var(--warn)">chatty: each one can cost its recipient a turn</span>':''}</span>
    ${x.context!=null?`<span class="k">Context</span><span>${x.context}% of its window in use${x.context>=80?' · <span style="color:var(--warn)">close to compacting</span>':''}</span>`:''}
    <span class="k">Plugin</span><span>${esc(x.plugin||'older than 0.6.6')}${x.stale&&!x.remote?' · <span style="color:var(--warn)">run /reload-plugins in this agent</span>':''}</span></div>
   ${others.length?`<div class="threads"><div class="k small">Other sessions in this folder</div>${others.map(o=>`<button class="ghost small" data-sel="${esc(o.id)}" style="text-align:left">${esc(o.name)} · ${o.busy?'working':'idle'}${o.summary?' · '+esc(o.summary.slice(0,60)):''}</button>`).join('')}</div>`:''}
   ${(x.threads&&(x.threads.owes.length||x.threads.waiting.length))?`<div class="threads"><div class="k small">Open requests</div>
    ${x.threads.owes.map(t=>`<div class="small">owes <b>${esc(t.with)}</b> a reply · <span class="mut">${esc(t.id)}: ${esc(t.text.slice(0,90))}</span></div>`).join('')}
    ${x.threads.waiting.map(t=>`<div class="small">waiting on <b>${esc(t.with)}</b> · <span class="mut">${esc(t.id)}: ${esc(t.text.slice(0,90))}</span></div>`).join('')}</div>`:''}
   ${x.remote?'':`<form id="msgform" class="sendbox" data-to="${esc(x.id)}"><label>Message ${esc(x.name)}<textarea name="text" rows="3" placeholder="It arrives as a message from you; its reply shows in Messages."></textarea></label>
    <div class="addline"><label class="check"><input type="checkbox" name="astask"> Send as a task</label><button type="submit">Send</button><span class="small ${sentNote.err?'err':'ok'}" id="sendmsg">${sentNote.to===x.id?esc(sentNote.text):''}</span></div></form>`}
   ${x.remote?'<div class="small mut">This agent runs on another machine; change it there.</div>':''}`}</div>`}
// Messages as a conversation: newest first, each reply under the message it answers.
let msgFilter='',spaceFilter='',livePeers=[],leadOf={};
// A message's spaces: what it recorded when sent, else (older messages) the space of a live agent on either end.
const spacesOf=x=>{const by=n=>livePeers.find(p=>p.name===n)?.space;return [x.from_space||by(x.from_name),x.to_space||by(x.to_name)].filter(Boolean)};
function renderMessages(list){
  const opts=(el,all,vals)=>{const keep=el.value;el.innerHTML=`<option value="">${all}</option>`+vals.map(n=>`<option ${n===keep?'selected':''}>${esc(n)}</option>`).join('')};
  opts($('#msgspace'),'All spaces',[...new Set(list.flatMap(spacesOf))].sort());
  const inSpace=list.filter(x=>!spaceFilter||spacesOf(x).includes(spaceFilter));
  opts($('#msgagent'),'Everyone',[...new Set(inSpace.flatMap(x=>[x.from_name,x.to_name]))].sort());
  const q=($('#msgq').value||'').trim().toLowerCase();
  const shown=inSpace.filter(x=>(!msgFilter||x.from_name===msgFilter||x.to_name===msgFilter)&&(!q||[x.text,x.from_name,x.to_name,x.id].join(' ').toLowerCase().includes(q)));
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
// What the page shows comes in one snapshot: pushed by /api/stream as it changes, or fetched when the stream is down.
let lastSnap=null,streamOk=false,live=null;
async function refresh(force,snap){
  if(signedOut)return;
  if(!snap){snap=await api('/api/snapshot');lastSnap=snap}
  const {status:st,peers:p,messages:m,tasks:tk,issues:fb,inbox:ib}=snap;
  allTasks=tk.tasks||[];allFeedback=fb.issues||[];inbox=ib.items||[];allSched=snap.schedules?.schedules||[];
  const lim=st.limit;$('#limitbar').style.display=lim?'block':'none';
  $('#limitbar').innerHTML=lim?`<b>⏸️ Usage limit reached</b> (${lim.resets_at?'resets '+new Date(lim.resets_at*1000).toLocaleString([], {weekday:'short',hour:'numeric',minute:'2-digit'}):'no reset time given'}; hit by ${esc(lim.by)}). Agents hold their messages and schedules wait. <button class="ghost small" data-limresume>Resume now</button> after switching accounts.`:'';
  const view=JSON.stringify([p.peers.map(({last_seen_s,...x})=>x),p.leads,m.messages.map(x=>[x.id,x.status]),allTasks.map(t=>[t.id,t.status,t.owner,t.note]),allFeedback.map(f=>[f.id,f.status,f.note]),inbox.map(i=>i.id),allSched.map(c=>[c.id,c.status,c.runs,c.note]),sel,panelTab,cfg.sessions]);
  if(view===lastView&&!force)return;lastView=view;
  $('#hostline').textContent=`${st.host} · daemon ${st.version}`;
  $('#browsers').textContent=`${st.browsers} signed in · links work once and expire after 60 s`;
  $('#keypath').textContent=st.home+'/key';
  const groups={};for(const x of p.peers)(groups[x.space||'default']??=[]).push(x);
  const lbl=n=>n==='default'?'unassigned':n;
  const names=[...new Set(p.peers.map(x=>x.space||'default'))];
  // An org chart. A space is a box of its own agents; the lead of a space inside it sits in that box too, with
  // its team's box hanging beneath it. Charts with teams in them come first, so the top of the org is at the top.
  // Two sessions open in a lead's folder are both leads: all sit in the parent's box, the team under the first.
  // A lead that's offline keeps its place at the top of its space, greyed, so you can see who work waits for.
  leadOf=p.leads||{};
  const ghost=(n,l)=>({id:'',name:l.name,cwd:l.cwd,space:n,parent:l.parent&&l.parent!==n?l.parent:'',leads:l.how,offline:true,summary:'offline; work for the space waits for it'});
  const ghosts=Object.entries(leadOf).filter(([n,l])=>!l.live).map(([n,l])=>ghost(n,l));
  const inside={},kids={},leads={};for(const x of [...p.peers,...ghosts])if(x.parent){(leads[x.space]??=[]).push(x);if(!inside[x.space]){inside[x.space]={parent:x.parent,lead:x.name,peer:x};(kids[x.parent]??=[]).push(x.space)}}
  for(const n in inside)inside[n].lead=leads[n].map(x=>x.name).join(', ');
  for(const n of Object.keys(kids))if(!groups[n])groups[n]=[];
  // An agent you added to other spaces shows once, in its own space, with a badge for each space it also joined.
  // The Spaces chart still names it in each of them, with its own space beside it.
  const joined={};for(const x of p.peers)for(const n of x.also||[])(joined[n]??=[]).push(x);
  const size=(n,seen=new Set())=>{if(seen.has(n))return 0;seen.add(n);return (groups[n]||[]).length+(kids[n]||[]).reduce((t,c)=>t+size(c,seen),0)};
  const order=(a,b)=>a==='default'?1:b==='default'?-1:((kids[b]||[]).length>0)-((kids[a]||[]).length>0)||size(b)-size(a)||a.localeCompare(b);
  const roots=Object.keys(groups).filter(n=>!inside[n]||n===inside[n].parent).sort(order);
  // A lead of a space at the top sits pinned above its members, who hang beneath it.
  const tops=n=>[...(groups[n]||[]).filter(x=>x.leads&&!x.parent),...ghosts.filter(x=>x.space===n&&!x.parent)].sort(rank);
  // A space's box: its members, then the leads of the spaces under it.
  const isLead=(x,n)=>!!(inside[n]&&inside[n].parent!==n&&x.parent&&x.space===n);
  const members=n=>[...groups[n].filter(x=>!isLead(x,n)&&!(x.leads&&!x.parent)).sort(rank).sort((a,b)=>a.name.localeCompare(b.name)),
    ...(kids[n]||[]).filter(c=>c!==n).sort().map(c=>inside[c].peer)];
  const short=d=>{const t=tilde(d).split('/');return t.length>3?'…/'+t.slice(-2).join('/'):t.join('/')};
  const state=x=>(x.busy?'working':'idle')+(x.busy_since?' '+ago(x.busy_since):'');
  // Messages sent in the last hour, flagged from 20: each one can cost its recipient a turn.
  const rate=x=>x.sent_hour?`<span class="${x.sent_hour>=20?'hot':'mut'}" title="messages sent in the last hour">${x.sent_hour}/h</span>`:'';
  const ctx=x=>x.context==null?'':`<span class="ctx${x.context>=80?' hi':''}" title="context window in use">${x.context}%</span>`;
  // A lead is told apart by its outline (and says which team on hover), so every card keeps its context in one place.
  // The badge sits in the name row, left of the context; an offline lead's clear link goes beneath.
  // An implicit lead (it was alone in the space) has a dashed badge, and says so on hover, so the name keeps its room.
  const badge=x=>(x.also||[]).map(n=>`<span class="badge guest" title="Also a member of ${esc(n)}">+${esc(n)}</span>`).join('')+(x.leads?`<span class="badge${x.leads==='only'?' only':''}" title="${x.leads==='only'?'Lead (only agent): it was the only agent in the space':'Lead'}">Lead</span>`:'');
  const clearOff=x=>x.leads&&x.offline?`<div><span class="small mut" data-clearlead="${esc(x.space)}" title="Clear this lead" style="cursor:pointer">clear</span></div>`:'';
  const node=x=>`<${x.offline?'div':'button'} class="node${x.leads?' is-lead':''}${x.offline?' off':''}${x.id&&x.id===sel?' sel':''}"${x.offline?'':` data-sel="${esc(x.id)}"`}${x.leads?` title="Leads the ${esc(x.space)} space${x.offline?' (offline)':''}"`:''}>
    <div class="nm"><span title="${esc(x.name)}">${x.offline?'':`<span class="dot ${x.busy?'busy':''}"></span>`}${esc(x.name)}</span><span class="rt">${badge(x)}${ctx(x)}</span></div>${clearOff(x)}
    <div class="dir">${esc(short(x.cwd))}</div>${x.branch?`<div class="dir" title="${esc(x.branch)}">⎇ ${esc(x.branch)}</div>`:''}<div class="sum"${x.role?` title="Role: ${esc(x.role)}"`:''}>${esc(x.summary||x.role)||'no summary yet'}</div>${x.open_tasks?`<div class="small" style="color:var(--acc)">${x.open_tasks} open task${x.open_tasks>1?'s':''}</div>`:''}${x.offline?'':`<div class="state">${state(x)}${x.sent_hour?' · '+rate(x):''}</div>`}</${x.offline?'div':'button'}>`;
  // Every live agent gets its own card, two sessions in one folder included: a hidden one can't be found to message.
  const rank=(a,b)=>(b.busy-a.busy)||(a.last_seen_s-b.last_seen_s);
  const unit=(n,seen)=>{if(seen.has(n))return '';seen.add(n);
    const list=members(n),branches=list.some(x=>x.parent&&x.space!==n);
    const top=tops(n);
    const count=top.filter(x=>!x.offline).length+list.reduce((t,x)=>t+(x.parent&&x.space!==n?leads[x.space].filter(l=>!l.offline).length:x.offline?0:1),0);
    const cols=list.map(x=>x.parent&&x.space!==n&&!seen.has(x.space)
      ?`<div class="ocol"><div class="orow stack">${leads[x.space].slice().sort(rank).map(node).join('')}</div><div class="down"></div>${unit(x.space,seen)}</div>`:`<div class="ocol">${node(x)}</div>`);
    // A team with no teams under it stacks, so each lead's column stays one card wide.
    const stack=inside[n]&&inside[n].parent!==n&&!branches;
    const row=`<div class="orow${stack?' stack':''}">${cols.join('')||'<span class="mut small">no other live agents</span>'}</div>`;
    return `<div class="unit"><div class="unit-h">${esc(lbl(n))} · ${count}</div>${top.length?`<div class="orow stack">${top.map(node).join('')}</div>${cols.length?`<div class="under">${row}</div>`:''}`:row}</div>`};
  // Each space's box picks its lead and where it sits. The lead is any folder with a live agent in the space.
  const pick=(n)=>{if(n==='default')return '';const cur=leadOf[n];const folders=[...new Map((groups[n]||[]).map(x=>[x.cwd,x.name])).entries()];
    if(cur&&!folders.some(([c])=>c===cur.cwd))folders.unshift([cur.cwd,cur.name+' (offline)']);
    const others=names.concat(Object.keys(leadOf)).filter((s,i,a)=>s!==n&&s!=='default'&&a.indexOf(s)===i).sort();const up=(groups[n]||[])[0]?.space_parent??leadOf[n]?.parent??'';
    return `<div class="spick"><label>Lead<select data-splead="${esc(n)}"><option value="">${cur?'no lead':'none yet'}</option>${folders.map(([c,nm])=>`<option value="${esc(c)}" ${cur&&cur.cwd===c?'selected':''}>${esc(nm)}${cur&&cur.cwd===c&&cur.how==='only'?' (was alone)':''}</option>`).join('')}</select></label>
      <label>Inside<select data-spparent="${esc(n)}"><option value="">top level</option>${others.map(o=>`<option ${o===up?'selected':''}>${esc(o)}</option>`).join('')}</select></label></div>`};
  const sunit=(n,seen)=>{if(seen.has(n))return '';seen.add(n);
    const own=[...groups[n].filter(x=>!isLead(x,n)&&!(x.leads&&!x.parent)).map(x=>esc(x.name)),...(joined[n]||[]).map(x=>esc(x.name)+` <span class="mut">(${esc(x.space||'default')})</span>`)];
    const ledBy=inside[n]?inside[n].lead:tops(n).map(x=>x.name+(x.offline?' (offline)':'')).join(', ');
    const ch=(kids[n]||[]).filter(c=>c!==n&&!seen.has(c)).sort();
    return `<div class="ocol"><div class="sbox${ledBy?' led':''}"><div class="name">${esc(lbl(n))}</div>
      ${ledBy?`<div class="small" style="color:var(--acc)">led by ${esc(ledBy)}</div>`:''}
      <div class="small mut">${own.join(', ')||'no other agents'}</div>${pick(n)}</div>
      ${ch.length?`<div class="down"></div><div class="branch">${ch.map(c=>sunit(c,seen)).join('')}</div>`:''}</div>`};
  $('#n-agents').textContent=p.peers.length||'';$('#n-spaces').textContent=names.length||'';$('#n-msgs').textContent=m.messages.length||'';
  // Spaces caught in a parent loop have no root; they are shown at the top level after the rest.
  const all=(fn)=>{const seen=new Set();let out=roots.map(n=>fn(n,seen)).join('');for(const n of Object.keys(groups).sort())if(!seen.has(n))out+=fn(n,seen);return out};
  // The list view: the same tree as the chart, one row per agent, each space indented under the lead that speaks for it.
  const lrow=x=>{const where=[short(x.cwd),x.branch?'⎇ '+x.branch:''].filter(Boolean).map(esc).join(' · ');
    return `<${x.offline?'div':'button'} class="lrow${x.leads?' is-lead':''}${x.offline?' off':''}${x.id&&x.id===sel?' sel':''}"${x.offline?'':` data-sel="${esc(x.id)}"`}>
      <span class="ln"><span title="${esc(x.name)}">${x.offline?'':`<span class="dot ${x.busy?'busy':''}"></span>`}${esc(x.name)}</span>${badge(x)}</span>
      <span class="lw"${x.role?` title="Role: ${esc(x.role)}"`:''}><span class="mono">${where}</span>${x.summary||x.role?' — '+esc(x.summary||x.role):x.offline?' — offline':''}</span>
      <span class="lr">${rate(x)}${x.open_tasks?`<span style="color:var(--acc)">${x.open_tasks} task${x.open_tasks>1?'s':''}</span>`:''}${x.offline?'':`<span class="mut">${state(x)}</span>`}${ctx(x)}</span></${x.offline?'div':'button'}>`};
  const lspace=(n,seen)=>{if(seen.has(n))return '';seen.add(n);
    const nested=inside[n]&&inside[n].parent!==n;
    const head=nested?(leads[n]||[]):tops(n);
    const own=[...groups[n].filter(x=>!isLead(x,n)&&!(x.leads&&!x.parent)).sort(rank).sort((a,b)=>a.name.localeCompare(b.name))];
    const ch=(kids[n]||[]).filter(c=>c!==n&&!seen.has(c)).sort();
    const body=own.map(lrow).join('')+ch.map(c=>lspace(c,seen)).join('');
    // Counted as the chart counts: a top-level lead and the leads of teams inside sit with this space; a team's own lead sits with its parent.
    const count=own.length+(nested?0:head.filter(x=>!x.offline).length)+ch.reduce((t,c)=>t+(leads[c]||[]).filter(x=>!x.offline).length,0);
    return `<div class="lspace"><div class="lspace-h">${esc(lbl(n))} · ${count}${nested?` <span class="small">inside ${esc(inside[n].parent)}</span>`:''}</div>
      ${head.slice().sort(rank).map(lrow).join('')}${!body?'':head.length?`<div class="lsub led">${body}</div>`:`<div class="lflat">${body}</div>`}</div>`};
  document.querySelectorAll('[data-alayout]').forEach(b=>b.classList.toggle('on',b.dataset.alayout===aLayout));
  $('#peers').classList.toggle('charts',aLayout==='chart'&&!($('#asearch').value||'').trim());
  // A search shows the matching agents as a flat list, whichever view is on.
  const aq=($('#asearch').value||'').trim().toLowerCase();
  const hits=aq?p.peers.filter(x=>[x.name,x.cwd,x.branch,x.summary,x.space].join(' ').toLowerCase().includes(aq)):null;
  $('#peers').innerHTML=!p.peers.length?'<div class="empty">No live agents. Start Claude Code with the peerlink plugin.</div>'
    :hits?`<div class="lspace-h">${hits.length} match${hits.length===1?'':'es'}</div><div class="lflat">${hits.map(lrow).join('')||'<div class="empty">No agent matches.</div>'}</div>`
    :aLayout==='list'?`<div class="ltree">${all(lspace)}</div>`
    :all((n,seen)=>{const u=unit(n,seen);return u?`<div class="chart">${u}</div>`:''});
  // Agents still on older plugin code: they keep working, but get fixes only after /reload-plugins.
  const behind=p.peers.filter(x=>x.stale&&!x.remote);
  $('#stalebar').style.display=behind.length?'block':'none';
  $('#stalebar').innerHTML=behind.length?`<b>${behind.length} agent${behind.length>1?'s':''} on an older peerlink plugin</b> (${behind.map(x=>esc(x.name)).join(', ')}). Run <span class="mono">/peerlink update</span> in any agent.`:'';
  const picked=p.peers.find(x=>x.id===sel);
  const panel=picked?agentPanel(picked,p.peers.filter(y=>y.id!==picked.id&&y.cwd===picked.cwd&&y.space===picked.space)):'';
  // Never redraw the panel under someone editing it.
  if(panel!==lastPanel&&!(editing&&picked&&$('#agentform')?.dataset.id===picked.id)){$('#detail').innerHTML=panel;lastPanel=panel}
  // The open panel takes room beside the chart rather than covering part of it.
  document.body.classList.toggle('drawer',!!picked);
  // The default space is always listed: it's where every agent with no space set lands.
  const dflt=groups.default?'':`<div class="chart"><div class="orow" style="justify-content:flex-start"><div class="ocol"><div class="sbox"><div class="name">unassigned</div><div class="small mut">agents whose folder has no space · none live</div></div></div></div></div>`;
  $('#spacelist').innerHTML=all((n,seen)=>{const u=sunit(n,seen);return u?`<div class="chart"><div class="orow" style="justify-content:flex-start">${u}</div></div>`:''})+dflt;
  livePeers=p.peers;lastMsgs=m.messages;renderMessages(lastMsgs);renderTasks();renderFeedback();renderInbox();renderSchedules();
}
// Schedules: what's active or waiting first, then the rest, newest first.
let allSched=[];
function renderSchedules(){
  const live=c=>c.status==='active'||c.status==='pending';
  $('#n-sched').textContent=allSched.filter(live).length||'';
  $('#schedfolders').innerHTML=livePeers.filter(x=>!x.remote).map(x=>`<option value="${esc(x.name)}">${esc(tilde(x.cwd))}</option>`).join('')+[...new Set(livePeers.filter(x=>!x.remote).map(x=>x.cwd))].map(d=>`<option value="${esc(tilde(d))}">`).join('');
  const when=t=>t?new Date(t*1000).toLocaleString([], {weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'';
  const rows=[...allSched.filter(live),...allSched.filter(c=>!live(c))];
  $('#schedlist').innerHTML=rows.map(c=>`<div class="card"><div class="addline"><b>${esc(c.id)}</b><span class="chip">${esc(c.status)}</span><span>${esc(c.when_text)}</span>${c.agent?`<b>${esc(c.agent)}</b>`:''}<span class="mono mut">${esc(tilde(c.folder))}</span>
    <span style="margin-left:auto" class="small mut">${c.next_run&&live(c)?'next '+when(c.next_run):''}</span>${live(c)?`<button class="ghost small danger" data-schedcancel="${esc(c.id)}">Cancel</button>`:''}</div>
    <div style="margin-top:6px;white-space:pre-wrap">${esc(c.prompt)}</div>
    <div class="small mut" style="margin-top:4px">by ${esc(c.requester?.name||'you')}${c.start?' · starts an agent when none is running':' · waits for an agent'}${c.runs?` · ran ${c.runs}×, last ${when(c.last_run)}${c.note?': '+esc(c.note):''}`:''}${c.status==='pending'?' · approve it in the Inbox':''}</div></div>`).join('')||'<div class="empty">No schedules yet.</div>'}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-schedcancel]');if(!b)return;
  if(!confirm(`Cancel ${b.dataset.schedcancel}?`))return;const r=await post(`/api/schedules/${encodeURIComponent(b.dataset.schedcancel)}/cancel`);if(r.error)alert(r.error);refresh(true)});
document.addEventListener('submit',async e=>{if(e.target.id!=='schedform')return;e.preventDefault();const f=e.target,v=n=>f.elements[n].value.trim();
  const w=v('when'),cron=/^\S+\s+\S+\s+\S+\s+\S+\s+\S+$/.test(w)&&!/^in\s/i.test(w)&&!/^\d{4}-/.test(w);
  const r=await post('/api/schedules',{agent:v('target'),prompt:v('prompt'),...(cron?{cron:w}:{at:w}),start:f.elements.start.checked});
  const out=$('#schedmsg');out.className='small '+(r.error?'err':'ok');out.textContent=r.error||`Added ${r.schedule.id}`;
  if(!r.error){f.elements.prompt.value='';f.elements.when.value=''}refresh(true)});
// The Inbox: one row per thing waiting on you, with the action it needs.
let inbox=[];
let seenInbox=null;
function renderInbox(){
  $('#n-inbox').textContent=inbox.length||'';
  document.title=inbox.length?`(${inbox.length}) Peerlink`:'Peerlink';
  // A desktop alert for each new item, when you've turned them on and the page isn't in front.
  const ids=new Set(inbox.map(i=>i.kind+i.id));
  if(seenInbox&&notifyOn()&&document.hidden)for(const i of inbox)if(!seenInbox.has(i.kind+i.id)){
    try{const n=new Notification(`Peerlink · ${i.who||'an agent'}`,{body:i.title,tag:i.kind+i.id});n.onclick=()=>{focus();location.hash='#inbox';n.close()}}catch{}}
  seenInbox=ids;
  $('#notifybtn').textContent=notifyOn()?'Desktop alerts: on':'Desktop alerts: off';
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
document.addEventListener('change',async e=>{const t=e.target;if(!t.dataset.splead&&!t.dataset.spparent)return;
  const r=t.dataset.spparent?await post(`/api/spaces/${encodeURIComponent(t.dataset.spparent)}/parent`,{parent:t.value})
    :await post(`/api/spaces/${encodeURIComponent(t.dataset.splead)}/lead`,{cwd:t.value});
  if(r.error)alert(r.error);lastView='';refresh(true)});
document.addEventListener('click',async e=>{const b=e.target.closest('[data-makelead],[data-clearlead]');if(!b)return;e.stopPropagation();
  const space=b.dataset.makelead||b.dataset.clearlead;
  if(b.dataset.clearlead&&!confirm(`Clear the lead of ${space}? Work sent to the space won't reach anyone until you pick a new lead.`))return;
  const r=await post(`/api/spaces/${encodeURIComponent(space)}/lead`,{cwd:b.dataset.cwd||''});if(r.error)alert(r.error);refresh(true)});
document.addEventListener('click',async e=>{const b=e.target.closest('[data-clearctx]');if(!b)return;
  const x=livePeers.find(p=>p.id===b.dataset.clearctx);if(!confirm(`Clear ${x?x.name:'this agent'}'s context? It starts a fresh conversation after the turn it's in.`))return;
  const r=await post(`/api/sessions/${encodeURIComponent(b.dataset.clearctx)}/clear`);if(r.error)alert(r.error);else{const m=$('#clearmsg');if(m)m.textContent='Asked: it clears after its current turn.'}});
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
    ||`<div class="empty">${allFeedback.length?'Nothing here.':'No feedback yet.'}</div>`}
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
// The board's layout, rows and open cards are this browser's own; storage can be missing (a private window), so every use is guarded.
const pref=(k,d)=>{try{return localStorage.getItem('peerlink.'+k)??d}catch{return d}};
var aLayout=pref('alayout','list');
const notifyOn=()=>pref('notify','')==='1'&&window.Notification&&Notification.permission==='granted';
document.addEventListener('click',async e=>{if(!e.target.closest('[data-notify]'))return;
  if(notifyOn()){try{localStorage.setItem('peerlink.notify','')}catch{}}
  else if(!window.Notification)alert('This browser has no desktop notifications.');
  else if(await Notification.requestPermission()==='granted'){try{localStorage.setItem('peerlink.notify','1')}catch{}}
  else alert('Notifications are blocked for this page. Allow them in the browser\'s site settings.');
  renderInbox()});
document.addEventListener('click',e=>{const b=e.target.closest('[data-alayout]');if(!b)return;aLayout=b.dataset.alayout;try{localStorage.setItem('peerlink.alayout',aLayout)}catch{};refresh(true)});
const keep=(k,v)=>{try{localStorage.setItem('peerlink.'+k,v)}catch{}};
let tLayout=pref('tlayout','board'),dragging=null;const openCards=new Set();
$('#tgroup').value=pref('tgroup','');
const COLS=[['open','Open'],['in_progress','In progress'],['done','Done'],['failed','Failed'],['cancelled','Cancelled']];
// What a drop on a column does: only what the web UI could already do. The owner's own statuses are never set from here.
const dropAction=(t,col)=>col==='cancelled'&&isLive(t)?'cancelled':col==='open'&&!isLive(t)?'open':'';
const reqName=t=>t.requester?.name||'?';
function taskCard(t){const kids=allTasks.filter(x=>x.parent===t.id),done=kids.filter(k=>k.status==='done').length;
  return `<div class="tcard${openCards.has(t.id)?' open':''}" draggable="true" data-task="${esc(t.id)}" data-st="${esc(t.status)}" tabindex="0" aria-label="${esc(t.title)}, ${TSTATE[t.status]}, owner ${esc(t.owner)}">
    <div class="tt">${esc(t.title)}</div>
    <div class="meta by"><span><b>${esc(t.owner)}</b> · asked by ${esc(reqName(t))}</span><span class="age" title="${new Date(t.created*1000).toLocaleString()}">${ago(t.created)}</span></div>
    ${t.parent?`<div class="meta">↳ part of ${esc(t.parent)}</div>`:''}
    ${kids.length?`<div class="meta">${done} of ${kids.length} subtasks done</div><div class="bar"><i style="width:${Math.round(100*done/kids.length)}%"></i></div>`:''}
    <div class="more">${t.space?`<div class="meta">${esc(t.id)} · ${esc(t.space)}</div>`:`<div class="meta">${esc(t.id)}</div>`}${t.note?`<div class="note">${esc(t.note)}</div>`:''}
    <div class="tact">${isLive(t)?'<button class="ghost small" data-tact="cancelled">Cancel</button>':'<button class="ghost small" data-tact="open">Reopen</button>'}${agentSelect('','data-treassign','Reassign to…')}</div></div></div>`}
function board(list){const shut=!openCards.has('col:cancelled');
  return `<div class="board">${COLS.map(([k,label])=>{const cards=list.filter(t=>t.status===k).sort((a,b)=>b.created-a.created);
    return `<div class="col${k==='cancelled'?' shut'+(shut?'':' open'):''}" data-col="${k}"><div class="col-h">${k==='cancelled'?`<button data-tcol="cancelled" aria-expanded="${!shut}">${shut?'▸':'▾'} ${label}</button>`:label}<span>${cards.length}</span></div>${cards.map(taskCard).join('')||(k==='cancelled'?'':'<div class="none">None</div>')}</div>`}).join('')}</div>`}
function renderTasks(){
  const opts=(el,all,vals)=>{const keep=el.value;el.innerHTML=`<option value="">${all}</option>`+vals.map(n=>`<option ${n===keep?'selected':''}>${esc(n)}</option>`).join('')};
  opts($('#towner'),'Everyone',[...new Set(allTasks.map(t=>t.owner))].sort());
  opts($('#treq'),'Anyone',[...new Set(allTasks.map(reqName))].sort());
  opts($('#tspace'),'All spaces',[...new Set(allTasks.map(t=>t.space).filter(Boolean))].sort());
  const st=$('#tstatus').value,ow=$('#towner').value,rq=$('#treq').value,sp=$('#tspace').value,grp=$('#tgroup').value;
  const picked=allTasks.filter(t=>(!ow||t.owner===ow)&&(!rq||reqName(t)===rq)&&(!sp||t.space===sp));
  $('#n-tasks').textContent=allTasks.filter(isLive).length||'';
  document.querySelectorAll('[data-tlayout]').forEach(b=>b.classList.toggle('on',b.dataset.tlayout===tLayout));
  $('#tstatusw').hidden=tLayout!=='list';$('#tgroupw').hidden=tLayout!=='board';
  // Never redraw under an open reassign menu or a card being dragged.
  if(dragging||document.activeElement?.matches?.('#tasklist [data-treassign]'))return;
  const none=`<div class="empty">${allTasks.length?'No tasks match.':'No tasks yet.'}</div>`;
  if(tLayout==='list'){$('#tasklist').innerHTML=withSubs(picked.filter(t=>st==='live'?isLive(t):!st||t.status===st))||none;return}
  if(!picked.length){$('#tasklist').innerHTML=none;return}
  const key=grp==='space'?t=>t.space||'no space':grp==='owner'?t=>t.owner:null;
  $('#tasklist').innerHTML=key?[...new Set(picked.map(key))].sort().map(g=>`<div class="lane-h">${esc(g)}</div>${board(picked.filter(t=>key(t)===g))}`).join(''):board(picked)}
// A board card opens in place to show its note and the same buttons as a list row, so nothing needs a drag.
document.addEventListener('click',e=>{const h=e.target.closest('[data-tcol]');if(h){const k='col:'+h.dataset.tcol;openCards.has(k)?openCards.delete(k):openCards.add(k);renderTasks();return}
  const c=e.target.closest('.tcard');if(!c||e.target.closest('.tact'))return;
  openCards.has(c.dataset.task)?openCards.delete(c.dataset.task):openCards.add(c.dataset.task);c.classList.toggle('open')});
document.addEventListener('keydown',e=>{const c=e.target.closest?.('.tcard');if(c&&e.target===c&&(e.key==='Enter'||e.key===' ')){e.preventDefault();c.click()}});
document.addEventListener('click',e=>{const b=e.target.closest('[data-tlayout]');if(!b)return;tLayout=b.dataset.tlayout;keep('tlayout',tLayout);renderTasks()});
// Dragging: the columns and agents a card may go to light up; anything else refuses the drop.
document.addEventListener('dragstart',e=>{const c=e.target.closest?.('.tcard');if(!c)return;
  dragging=allTasks.find(t=>t.id===c.dataset.task);if(!dragging)return;c.classList.add('dragging');e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain',dragging.id);
  document.querySelectorAll('.col').forEach(col=>col.classList.toggle('can',!!dropAction(dragging,col.dataset.col)));
  const agents=agentFolders().filter(a=>a.cwd!==dragging.owner_cwd);
  $('#tdrop').innerHTML=agents.length?'Hand it to: '+agents.map(a=>`<span class="agent" data-dropcwd="${esc(a.cwd)}">${esc(a.name)}</span>`).join(''):'';$('#tdrop').classList.toggle('on',agents.length>0)});
const target=e=>{if(!dragging)return null;const a=e.target.closest?.('[data-dropcwd]');if(a)return a;const col=e.target.closest?.('.col');return col&&dropAction(dragging,col.dataset.col)?col:null};
document.addEventListener('dragover',e=>{const t=target(e);document.querySelectorAll('.over').forEach(x=>x!==t&&x.classList.remove('over'));if(!t)return;e.preventDefault();t.classList.add('over')});
document.addEventListener('dragend',()=>{dragging=null;$('#tdrop').classList.remove('on');document.querySelectorAll('.can,.over,.dragging').forEach(x=>x.classList.remove('can','over','dragging'));renderTasks()});
document.addEventListener('drop',async e=>{const t=target(e),task=dragging;if(!t||!task)return;e.preventDefault();dragging=null;
  let body;
  if(t.dataset.dropcwd){if(!confirm(`Hand task ${task.id} to ${t.textContent}? ${task.owner} is told to stop.`))return;body={owner:t.dataset.dropcwd}}
  else{const st=dropAction(task,t.dataset.col);if(st==='cancelled'&&!confirm(`Cancel task ${task.id}? Its owner is told to stop.`))return;body={status:st}}
  // expect: refused if the owner changed the task since the board was drawn, so its own update is never overridden.
  const r=await post('/api/tasks/'+encodeURIComponent(task.id),{...body,expect:task.status});if(r.error)alert(r.error);refresh(true)});
function panelTasks(x){
  const mine=allTasks.filter(t=>t.owner_cwd&&t.owner_cwd===x.cwd),asked=allTasks.filter(t=>t.requester?.id===x.id||(x.cwd&&t.requester?.cwd===x.cwd));
  const sec=(title,list,none)=>`<div class="k small">${title}</div>${list.length?byLive(list).map(t=>taskRow(t,false)).join(''):`<div class="small mut">${none}</div>`}`;
  // Its schedules: named for it, or for its folder with no agent named.
  const sched=allSched.filter(c=>(c.status==='active'||c.status==='pending')&&(c.agent?c.agent===x.name:c.folder===x.cwd));
  const srow=c=>`<div class="small" style="margin:4px 0"><b>${esc(c.id)}</b> ${esc(c.when_text)}${c.agent?'':' <span class="mut">(its folder)</span>'}: ${esc(c.prompt.length>120?c.prompt.slice(0,119)+'…':c.prompt)}</div>`;
  return `<div class="ptasks">${sec('Owns',mine,'No tasks for this agent.')}${sec('Asked for',asked,'It hasn\'t handed out any tasks.')}<div class="k small">Schedules</div>${sched.length?sched.map(srow).join(''):'<div class="small mut">None. Add one on the Schedules page.</div>'}</div>`}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-ptab],[data-tact]');if(!b)return;
  if(b.dataset.ptab){panelTab=b.dataset.ptab;editing=false;refresh(true);return}
  const id=b.closest('[data-task]').dataset.task,was=allTasks.find(t=>t.id===id)?.status;
  if(b.dataset.tact==='cancelled'&&!confirm(`Cancel task ${id}? Its owner is told to stop.`))return;
  const r=await post('/api/tasks/'+encodeURIComponent(id),{status:b.dataset.tact,expect:was});if(r.error)alert(r.error);refresh(true)});
document.addEventListener('change',async e=>{const t=e.target;
  if(['tstatus','towner','treq','tspace','tgroup'].includes(t.id)){if(t.id==='tgroup')keep('tgroup',t.value);renderTasks();return}
  if(t.dataset.treassign!==undefined&&t.value){const id=t.closest('[data-task]').dataset.task;
    const r=await post('/api/tasks/'+encodeURIComponent(id),{owner:t.value});if(r.error)alert(r.error);t.blur();refresh(true)}});
// You, to an agent or a space's lead, from the panel's message box or the Tasks page's New task form.
// The panel's note outlives the redraw a send causes.
let sentNote={};
document.addEventListener('submit',async e=>{const f=e.target;if(f.id!=='msgform'&&f.id!=='newtask')return;e.preventDefault();
  const text=f.elements.text.value.trim(),out=f.id==='msgform'?$('#sendmsg'):$('#newtaskmsg');if(!text)return;
  const body=f.id==='msgform'?{to:f.dataset.to,text,kind:f.elements.astask.checked?'task':'message'}:{to:f.elements.to.value,text,kind:'task'};
  const r=await post('/api/web/send',body);
  const note=r.error||`Sent to ${r.to}${r.task?` as task ${r.task}`:''}.`;
  if(f.id==='msgform'){sentNote={to:body.to,text:note,err:!!r.error};setTimeout(()=>{if(sentNote.text===note){sentNote={};refresh(true,lastSnap||undefined)}},4000)}
  if(r.error){out.className='small err';out.textContent=r.error;return}
  f.elements.text.value='';editing=false;out.className='small ok';out.textContent=note;
  if(f.id==='newtask')setTimeout(()=>{f.hidden=true;out.textContent=''},1500);refresh(true)});
document.addEventListener('click',e=>{const b=e.target.closest('[data-newtask]');if(!b)return;const f=$('#newtask');f.hidden=!f.hidden;if(f.hidden)return;
  // Agents by name, and each space that has a lead, which hands the work on.
  const spaces=Object.entries(leadOf).filter(([n])=>n!=='default').sort();
  f.elements.to.innerHTML=`<option value="">Choose who it's for…</option>`+(spaces.length?`<optgroup label="A space's lead">${spaces.map(([n,l])=>`<option value="space:${esc(n)}">${esc(n)} (lead: ${esc(l.name)}${l.live?'':', offline'})</option>`).join('')}</optgroup>`:'')
    +`<optgroup label="An agent">${livePeers.filter(x=>!x.remote).sort((a,b)=>a.name.localeCompare(b.name)).map(x=>`<option value="${esc(x.id)}">${esc(x.name)} · ${esc(x.space||'default')}</option>`).join('')}</optgroup>`;
  f.elements.text.focus()});
document.addEventListener('click',async e=>{const b=e.target.closest('[data-copy]');if(!b)return;e.stopPropagation();
  try{await navigator.clipboard.writeText(b.dataset.copy);b.textContent='Copied'}catch{b.textContent='Copy failed'}setTimeout(()=>{b.textContent='Copy'},1200)});
document.addEventListener('input',e=>{if(e.target.closest('#agentform,#msgform'))editing=true;
  if(e.target.id==='asearch'&&lastSnap)refresh(true,lastSnap);if(e.target.id==='msgq')renderMessages(lastMsgs)});
document.addEventListener('change',e=>{if(e.target.closest('#agentform,#msgform'))editing=true});
// Save applies only what changed, in one go, and says what happened. Details and Controls each show part of the
// agent's settings in this one form, so a field the open tab doesn't show is left alone.
document.addEventListener('submit',async e=>{if(e.target.id!=='agentform')return;e.preventDefault();
  const f=e.target,d=f.dataset,id=d.id,msg=$('#agentmsg'),has=n=>!!f.elements[n],v=n=>f.elements[n].value.trim();
  const patch={},errs=[];
  if(has('nickname')&&v('nickname')!==d.nick)patch.nickname=v('nickname');
  if(has('wake')&&f.elements.wake.value!==d.wake)patch.auto_wake=f.elements.wake.value==='default'?null:f.elements.wake.value==='on';
  if(has('muted')&&f.elements.muted.checked!==!!d.muted)patch.muted=f.elements.muted.checked;
  if(has('role')&&v('role')!==d.role)patch.role=v('role');
  msg.className='small';msg.textContent='Saving…';
  if(Object.keys(patch).length){const r=await post(`/api/sessions/${encodeURIComponent(id)}/config`,patch);if(r.error)errs.push(r.error);else cfg.sessions=r.sessions}
  if(has('space')&&v('space')!==d.space){const r=await post('/api/folders/space',{cwd:d.cwd,space:v('space')});if(r.error)errs.push(r.error)}
  const also=has('also')?v('also').split(',').map(t=>t.trim()).filter(Boolean):[];
  if(has('also')&&also.join(', ')!==d.also){const r=await post('/api/members',{cwd:d.cwd,spaces:also});if(r.error)errs.push(r.error)}
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
// -- Discord: the setup walkthrough and the channel-to-agent mapping --------
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
  $('#dstep3b').innerHTML=`<p class="meta">The master gets your messages. To reach a member instead, start with its name and a colon: <span class="mono">storefront: how's the build?</span></p>
    <table class="dmap"><thead><tr><th>Channel</th><th>Master</th><th>Members</th><th>Carries</th><th></th></tr></thead><tbody>${rows||'<tr><td colspan="5" class="mut small">No channels mapped yet.</td></tr>'}</tbody></table>
    <p class="small mut" style="margin-top:6px">Limit a channel to one kind to make it a #tasks or #announcements channel: everything you post there becomes a task or an announcement. Elsewhere, start a post with <span class="mono">Task:</span> or <span class="mono">Announcement:</span>.</p>
    <div class="addline" style="margin-top:8px"><button class="ghost" data-dact="addroute">+ Map a channel</button><button class="ghost" data-dact="refresh" title="Look for new servers and channels">↻ Refresh channels</button></div>
    <div class="addline" style="margin-top:12px"><label class="small mut">Direct messages to the bot go to</label>${agentSelect(dDraft.dm,'data-ddm','Nobody (ignored)')}</div>
    <div class="addline" style="margin-top:12px"><button data-dact="save">Save mapping</button><span class="small" id="dsaved"></span></div>`;
  const reading=d.reads_messages?`<p class="meta">Everything you write in a mapped channel goes to its master. No @mention needed.</p>`
    :d.message_content_switch?`<p class="meta">The Message Content switch is on in Discord, but the bot connected before it was. <button class="ghost small" data-dact="reconnect">Reconnect now</button></p>`
    :`<p class="meta">${d.message_content_switch===false?'<b>Discord says the Message Content switch is off.</b> ':''}@mention the bot each time, or DM it. To talk without @mentioning, turn on <b>Message Content Intent</b> on the <a href="${d.app_id?`https://discord.com/developers/applications/${esc(d.app_id)}/bot`:'https://discord.com/developers/applications'}" target="_blank" rel="noreferrer">Bot page ↗</a> (under Privileged Gateway Intents) and save. <button class="ghost small" data-dact="check">I turned it on</button></p>`;
  $('#dmore').innerHTML=`<h2>Talking to your agents</h2>${reading}
    <p class="meta">A reaction on your message shows its progress:</p>
    <div class="legend"><span>👀</span><span>delivered to the agent</span><span>🤔</span><span>thinking</span><span>🛠️</span><span>using tools</span><span>❓</span><span>asked you something</span><span>✅</span><span>answered</span></div>
    <p class="meta" style="margin-top:12px">Agents can message you first, labelled with their name. If an agent isn't running, your message waits up to an hour for it.</p>
    <h2>What agents can send</h2>
    <label class="check"><input type="checkbox" data-dopt="buttons" ${d.buttons?'checked':''}> <span><b>Buttons</b> for choices: tap one and it goes back to the agent as your answer. Off, the choices are a numbered list you reply to.</span></label>
    <label class="check"><input type="checkbox" data-dopt="files_out" ${d.files_out?'checked':''}> <span><b>Files from agents:</b> an agent can attach files from this machine to its messages.</span></label>
    <label class="check"><input type="checkbox" data-dopt="files_in" ${d.files_in?'checked':''}> <span><b>Files to agents:</b> what you attach on Discord is saved here, in <span class="mono">~/.peerlink/files/discord</span>, and the agent gets the paths.</span></label>
    <div class="addline" style="margin-top:8px"><label class="small mut" for="dmaxmb">Largest file, either way</label><input id="dmaxmb" type="number" min="1" max="500" step="1" value="${esc(d.max_file_mb)}" style="width:90px" data-dopt="max_file_mb"> <span class="small mut">MB (Discord allows 10 MB without Nitro)</span></div>
    <h2>People who can talk to agents</h2>
    <p class="meta">Only you can, as the bot's owner (or its team's members). Add other people's user IDs here, separated by commas: they can then message agents and answer buttons as you do. To find one, turn on Developer Mode in Discord, right-click the person and choose Copy User ID.</p>
    <div class="addline"><input id="dusers" class="mono" placeholder="e.g. 1562000000000000000" value="${esc((d.allowed_users||[]).join(', '))}" data-dusers style="flex:1;min-width:0"></div>
    <h2>Bots that can post to agents</h2>
    <p class="meta">Other bots are ignored unless listed here. Add their user IDs, separated by commas. To find one, turn on Developer Mode in Discord, right-click the bot and choose Copy User ID. List only bots you control.</p>
    <div class="addline"><input id="dbots" class="mono" placeholder="e.g. 1562000000000000000" value="${esc((d.trusted_bots||[]).join(', '))}" data-dbots style="flex:1;min-width:0"></div>
    <h2>Bot</h2>
    <label class="check"><input type="checkbox" data-dact="enabled" ${d.enabled?'checked':''}> Bot running</label>`}
document.addEventListener('click',e=>{if(e.target.closest('[data-limresume]'))post('/api/limit/resume',{}).then(()=>refresh(true))});
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
  if(t.dataset.dusers!==undefined)api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({allowed_users:t.value})}).then(()=>loadDiscord());
  if(t.dataset.dact==='enabled')api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({enabled:t.checked})}).then(()=>loadDiscord())});
document.addEventListener('click',async e=>{const t=e.target.closest('button');if(!t||!dDraft&&!t.dataset.dact)return;
  if(t.dataset.dremove!==undefined){dDraft.routes.splice(+t.dataset.dremove,1);renderDiscord()}
  if(t.dataset.dunmember){const [i,j]=t.dataset.dunmember.split(':').map(Number);dDraft.routes[i].members.splice(j,1);renderDiscord()}
  if(t.dataset.dtest){const r=await post('/api/discord/test',{channel_id:t.dataset.dtest});t.textContent=r.error?'Failed':'Sent ✓';if(r.error)alert(r.error)}
  const a=t.dataset.dact;if(!a)return;
  if(a==='addroute'){dDraft.routes.push({channel_id:'',master:'',members:[]});renderDiscord()}
  if(a==='refresh'){const r=await api('/api/discord/channels?fresh=1');dGuilds=r.guilds||[];renderDiscord()}
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
}
$('#cfg').addEventListener('submit',async e=>{e.preventDefault();const f=e.target,b={};
  for(const el of f.elements){if(!el.name)continue;
    b[el.name]=el.type==='checkbox'?el.checked:el.type==='number'?Number(el.value):el.name==='remotes'?el.value.split('\n').map(s=>s.trim()).filter(Boolean):el.value}
  const r=await api('/api/config',{method:'PUT',headers:J,body:JSON.stringify(b)});
  if(r&&r.error){$('#saved').textContent='Not saved: '+r.error;return}
  cfg=r;$('#saved').textContent='Saved '+new Date().toLocaleTimeString()});
// Poll every 2 s; while the daemon is unreachable say so, back off to 10 s, and pick up again once it's back.
let misses=0;
function connect(){if(!window.EventSource||signedOut)return;live?.close();live=new EventSource('/api/stream');
  live.onopen=()=>{streamOk=true};
  live.onmessage=e=>{const d=JSON.parse(e.data);lastSnap=d;
    if(misses){misses=0;$('#offline').style.display='none';loadCfg().catch(()=>{})}
    refresh(false,d).catch(()=>{})};
  // The browser retries the stream by itself; until it's back, tick() polls.
  live.onerror=()=>{streamOk=false}}
// With the stream up, tick only redraws now and then, so "idle 12m" stays true; without it, it polls.
async function tick(){
  try{if(streamOk&&lastSnap)await refresh(true,lastSnap);else{await refresh();if(misses){misses=0;$('#offline').style.display='none';await loadCfg();await refresh(true)}}}
  catch(e){if(!signedOut){misses++;$('#offline').style.display='block'}}
  if(signedOut){live?.close();return}
  setTimeout(tick,streamOk?30000:misses?10000:2000)}
show();loadCfg().then(()=>refresh(true)).then(()=>{if(location.hash==='#discord')location.hash='#channels';else if(location.hash==='#channels')loadDiscord()}).catch(()=>{misses=1;$('#offline').style.display='block'}).finally(()=>{connect();setTimeout(tick,2000)});
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
