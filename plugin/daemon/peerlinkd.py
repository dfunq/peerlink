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

VERSION = "0.8.70"
# A fingerprint of this file: an agent whose plugin ships different code at the same version replaces the daemon.
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
import chatlink  # noqa: E402  (beside this file; installs nothing)
import discordlink  # noqa: E402
import telegramlink  # noqa: E402

# A fingerprint of the daemon's code, this file and the chat modules beside it together.
_h = hashlib.sha256()
for _name in ("peerlinkd.py", "chatlink.py", "discordlink.py", "telegramlink.py"):
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

# The default guidance's report_issue line, left out when the feedback setting is off (no report_issue tool then).
FEEDBACK_RULE = """\
- If peerlink itself misbehaves (a reply that never comes, a message that \
reached the wrong agent, an error from these tools), call report_issue with \
the message ids and agent names, then carry on with your task. When \
peerlink gets in your way or could work better, report_issue with type \
"idea".
"""

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
{feedback_rule}""".replace("{clear_rule}", CLEAR_RULE).replace("{feedback_rule}", FEEDBACK_RULE)

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
    "feedback": False,  # agents get report_issue, for bugs and ideas about peerlink itself; see the Feedback page
    "issue_agent": "peerlink",
    "roster_in_prompt": True,
    "auto_summary": True,
    "auto_reload": True,
    "clear_between_tasks": True,  # the default guidance asks agents to save notes and clear their context after each task
    "clear_nudge_tokens": 400000,  # past this many tokens of context, an agent is asked to save and clear at its next stopping point; 0 is off
    "summary_model": "haiku",
    "auto_approve_all": False,  # every agent's requests to start an agent, or a schedule that may, go ahead without asking you
    "auto_approve_folders": [],  # the folders whose agents' requests do; set only from the web UI
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
    dir_summaries[rec["cwd"]] = {"summary": rec["summary"], "headline": rec.get("headline", ""), "name": display_name(rec), "ts": time.time()}
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
    script = f'#!/bin/sh\nexec "{sys.executable or "python3"}" "{os.path.abspath(__file__)}" "$@"\n'
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
    if data and "feedback" not in data:  # an install from before the setting keeps reporting, as it did
        merged["feedback"] = True
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


def default_guidance():
    text = DEFAULT_GUIDANCE if config.get("clear_between_tasks", True) else DEFAULT_GUIDANCE.replace(CLEAR_RULE, "")
    return text if config.get("feedback") else text.replace(FEEDBACK_RULE, "")


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
        "discord": routes_for(rec.get("cwd", ""), display_name(rec)) if rec and chat_configured() else [],
        "poll_interval_ms": config["poll_interval_ms"],
        "auto_wake_idle": config["auto_wake_idle"] if s["auto_wake"] is None else s["auto_wake"],
        "inject_while_busy": config["inject_while_busy"],
        "wake_delay_s": config.get("wake_delay_s", 15),
        "guidance": config["guidance"] or default_guidance(),
        "feedback": config.get("feedback", False),
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
    """(space, source, where): the space an agent was started for ('started': a folder can hold agents started for
    other spaces), else the space you put its folder in ('set'), else the nearest .peerlink ('file'), else default.
    A .peerlink only places folders you haven't."""
    now = time.time()
    cached = rec.get("_space")
    if cached and now - cached[0] < SPACE_CACHE_S:
        return cached[1]
    cwd = rec.get("cwd", "")
    found = (DEFAULT_SPACE, "default", "")
    started = clean_space(config["sessions"].get(rec.get("id") or "", {}).get("started_in"))
    chosen = clean_space((config.get("folder_spaces") or {}).get(cwd))
    path = find_space_file(cwd)
    if started:
        found = (started, "started", cwd)
    elif chosen:
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
    if not (folder and rec.get("cwd") == folder):
        return ""
    # Agents sharing the lead's folder (ones it started) aren't leads too: the one going by the folder's name is.
    lead = successor(folder)
    return how if not lead or lead["id"] == rec.get("id") else ""


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
        # Agents started there for another space move with the folder too.
        for r in sessions.values():
            if r.get("cwd") == cwd:
                config["sessions"].get(r["id"], {}).pop("started_in", None)
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


def task_owner(t):
    """The live agent that owns a task: the one it was sent to, or what that one became after /clear, else the agent
    going by its owner's name in its folder, else its folder's agent. None when its folder has none running."""
    if "owner_id" not in t:  # from before 0.8.51: the agent its message went to
        t["owner_id"] = next((m.get("to", "") for m in messages if m["id"] == t.get("msg")), "")
    oid, seen = t["owner_id"], set()
    while oid and oid not in seen:
        seen.add(oid)
        rec = sessions.get(oid)
        if rec and is_live(rec):
            if oid != t["owner_id"]:
                t["owner_id"] = oid  # the chain lives only in memory: a daemon restart would lose it
            return rec
        oid = succeeded_by.get(oid)
    named = [r for r in sessions.values() if is_live(r) and r.get("cwd") == t["owner_cwd"] and display_name(r) == t.get("owner")]
    return max(named, key=lambda r: r["last_seen"]) if named else successor(t["owner_cwd"])


def owns_task(rec, t):
    """Whether this agent owns the task (see task_owner). Agents sharing a folder don't share each other's tasks."""
    if not (t["owner_cwd"] and rec.get("cwd") == t["owner_cwd"]):
        return False
    owner = task_owner(t)
    return not owner or owner["id"] == rec["id"]


def owned_task_ids(rec):
    """The open tasks this agent owns, oldest first: what it's on, whatever its summary says."""
    return [t["id"] for t in tasks.values() if t["status"] in TASK_OPEN and owns_task(rec, t)]


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
        "headline": rec.get("headline", ""),
        "summary_ts": rec.get("summary_ts"),  # when it last changed: peers see how old it is
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
        "open_tasks": len(owned_task_ids(rec)),
        "task_ids": owned_task_ids(rec),
        "reach": sorted(reach(rec)),
        "also": also_in(rec),
        "busy": rec.get("busy", False),
        "busy_since": rec.get("busy_since"),
        "sent_hour": sent_last_hour(rec),
        # The plugin version the agent runs ('' before 0.6.6): one behind the daemon needs /reload-plugins.
        "plugin": rec.get("plugin", ""),
        "context": rec.get("context"),  # percent of the context window in use, when the agent reports it
        "stale": rec.get("plugin", "") != VERSION,
        "reload_queued": rec.get("reload_queued"),
        "muted": session_cfg(rec["id"])["muted"],
        "auto_approve": rec.get("cwd", "") in (config.get("auto_approve_folders") or []),
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
    return {"schedules": {"schedules": [sched_view(c) for c in sched]},"status": status, "peers": {"host": HOSTNAME, "peers": peers, "leads": leads_view()}, "messages": {"messages": msgs},
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
                for k in ("context", "sent_hour", "open_tasks", "summary_ts"):
                    if p.get(k) is not None and not isinstance(p[k], (int, float)) or isinstance(p.get(k), bool):
                        p[k] = None if k in ("context", "summary_ts") else 0
                p["task_ids"] = [str(i)[:12] for i in p.get("task_ids") or [] if isinstance(i, str)][:20] if isinstance(p.get("task_ids"), list) else []
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


def party(m, side):
    """One end of a message, as the web UI's Messages page groups them: a chat channel by its id, anyone else by name,
    which survives an agent's /clear."""
    who = str(m.get(side) or "")
    return who if who.startswith("discord:") else "n:" + str(m.get(side + "_name") or who).lower()


def conversation(a, b, limit=500):
    """Every message between two parties still in the log (this file and the one before it), oldest first."""
    pair, by_id = {a, b}, {}
    for path in (MESSAGES_PATH + ".1", MESSAGES_PATH):
        try:
            with open(path) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if "text" in entry:
                        by_id[entry["id"]] = entry
                    elif entry.get("id") in by_id:
                        by_id[entry["id"]].update(entry)
        except OSError:
            continue
    out = [m for m in by_id.values() if {party(m, "from"), party(m, "to")} == pair or (a == b and party(m, "from") == party(m, "to") == a)]
    return sorted(out, key=lambda m: m.get("ts", 0))[-limit:]


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
# The mapping: each Discord channel has agents, each one ticked to receive (your messages reach it: the first
# such agent gets them, "name: ..." reaches another) and to send (it may post there). Agents are kept by folder,
# so a mapping survives a restart, /clear or a new session. Direct messages go to `dm`.
discord = {"token": "", "app_id": "", "user": "", "enabled": True, "routes": [], "dm": "",
           # What agents may do beyond text, each a switch on the Channels page.
           "buttons": True, "files_out": True, "files_in": True, "max_file_mb": 10,
           # Bot user IDs whose posts reach agents as yours do (a voice assistant, say); other bots are ignored.
           "trusted_bots": [],
           # People's user IDs who may talk to agents and answer buttons as you do, beside the app's owner or team.
           "allowed_users": []}
# Telegram's bot, beside Discord's in the same file. Its chats map to agents in `routes`, as "tg:<chat id>".
TELEGRAM = {"token": "", "user": "", "enabled": True,
            # Nobody may talk to agents until listed here (Telegram user ids): anyone can find a bot by name.
            "allowed_users": [],
            "chats": {},  # "tg:<id>" -> title, every chat the bot has heard from, for the mapping's picker
            "strangers": []}  # the last few people who wrote without being allowed, offered on the Channels page
DISCORD_FILES = os.path.join(HOME, "files", "discord")  # what you attach on Discord, for the agent to read
discord_bot = [None]
telegram_bot = [None]


def load_discord():
    try:
        with open(DISCORD_PATH) as f:
            discord.update(json.load(f))
    except (OSError, ValueError):
        pass
    discord["routes"] = [clean_route(r) for r in discord.get("routes", []) if r.get("channel_id")]
    tg = discord.setdefault("telegram", {})
    for k, v in TELEGRAM.items():
        tg.setdefault(k, json.loads(json.dumps(v)))


def clean_route(r):
    """A channel's mapping as {channel_id, agents: [{cwd, name, receives, sends}]}. `name` picks one agent where several
    share the folder; without it (mappings before 0.8.64) the entry is the folder's. Before 0.8.36 it was a master and
    members, both of which got messages and posted, and kinds: those become agents that do both."""
    agents = r.get("agents")
    if agents is None:
        agents = [{"cwd": c, "receives": True, "sends": True} for c in [r.get("master")] + list(r.get("members") or [])]
    out, seen = [], set()
    for a in agents:
        cwd, name = str(a.get("cwd") or ""), str(a.get("name") or "").strip()
        if cwd and (cwd, name.lower()) not in seen:
            seen.add((cwd, name.lower()))
            out.append({"cwd": cwd, "name": name, "receives": bool(a.get("receives")), "sends": bool(a.get("sends"))})
    return {"channel_id": str(r["channel_id"]), "agents": out}


def receivers(route):
    """The agents a channel's messages can reach, as its entries, in order: the first gets them unless one is named."""
    return [a for a in route.get("agents", []) if a.get("receives")]


def entry_is(a, cwd, name=""):
    """Whether a channel's entry is this agent: its folder, and its name when the entry names one. No name to check
    (a folder alone) matches any entry there."""
    return a["cwd"] == cwd and (not a.get("name") or not name or a["name"].lower() == name.lower())


def entry_agent(a):
    """The live agent a channel's entry reaches: the one it names, else the folder's."""
    if not a.get("name"):
        return successor(a["cwd"])
    named = [r for r in sessions.values() if is_live(r) and r.get("cwd") == a["cwd"] and display_name(r).lower() == a["name"].lower()]
    return max(named, key=lambda r: r["last_seen"]) if named else None


def entry_label(a):
    return a.get("name") or folder_name(a["cwd"])


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


def start_telegram():
    old = telegram_bot[0]
    if old:
        old.close()
    telegram_bot[0] = None
    tg = discord["telegram"]
    if tg.get("token") and tg.get("enabled", True):
        bot = telegram_bot[0] = telegramlink.Bot(tg["token"], on_discord, log, on_press)
        bot.allowed_users = set(tg.get("allowed_users") or [])
        bot.chats = dict(tg.get("chats") or {})
        bot.on_command = on_slash
        bot.on_chat, bot.on_stranger, bot.on_migrate = telegram_chat, telegram_stranger, telegram_migrate


def telegram_chat(channel_id, title):
    with lock:
        discord["telegram"]["chats"][channel_id] = title
        save_discord()


def telegram_stranger(who):
    """Someone not allowed wrote to the bot: they're offered on the Channels page, newest first, and nothing else."""
    log("telegram", "ignored a message from", who["name"], who["id"], "(not allowed)")
    with lock:
        tg = discord["telegram"]
        tg["strangers"] = [{**who, "ts": time.time()}] + [s for s in tg["strangers"] if s["id"] != who["id"]][:9]
        save_discord()


def telegram_migrate(old, new):
    """A Telegram group that became a supergroup has a new id: its mapping moves with it."""
    log("telegram", "chat", old, "is now", new)
    with lock:
        for r in discord.get("routes", []):
            if r["channel_id"] == old:
                r["channel_id"] = new
        chats = discord["telegram"]["chats"]
        chats[new] = chats.pop(old, chats.get(new, ""))
        save_discord()


# Chat platforms: every post, reaction and button press goes through these, which pick the platform's module by the
# channel id (chatlink): Discord's are bare digits, Telegram's "tg:<chat id>".
ChatError = chatlink.ChatError
CHAT_LINKS = {"discord": discordlink, "telegram": telegramlink}


def chat_token(platform):
    return discord.get("token", "") if platform == "discord" else (discord.get(platform) or {}).get("token", "")


def chat_link(channel_id):
    """(module, credential) for the platform a channel is on; ChatError when it isn't set up."""
    platform = chatlink.platform(channel_id)
    if platform in CHAT_LINKS and chat_token(platform):
        return CHAT_LINKS[platform], chat_token(platform)
    raise ChatError(f"{chatlink.name(channel_id)} isn't set up")


def chat_bot(channel_id):
    """The running bot for the platform a channel is on, or None."""
    platform = chatlink.platform(channel_id)
    bot = {"discord": discord_bot, "telegram": telegram_bot}.get(platform, [None])[0]
    return bot if chat_token(platform) else None


def chat_ready(channel_id=""):
    """Whether the platform a channel is on is set up with its bot running; with no channel, whether any is."""
    if channel_id:
        return chat_bot(channel_id) is not None
    return chat_bot("0") is not None or chat_bot("tg:0") is not None


def chat_configured():
    """Whether any platform has a token, running or not: agents are told their channels then."""
    return any(chat_token(p) for p in CHAT_LINKS)


def choices(options, key):
    """Options as buttons for chat_post: [(label, "key:<n>")]."""
    return [(o, f"{key}:{i}") for i, o in enumerate(options)]


def chat_post(channel_id, text, reply_to=None, buttons=None, files=None):
    """Post to a chat, split as the platform needs; the id of the last part, where the buttons and files go."""
    link, cred = chat_link(channel_id)
    return link.post(cred, channel_id, text, reply_to, buttons, files)["id"]


def chat_clear_buttons(channel_id, message_id):
    link, cred = chat_link(channel_id)
    link.clear_buttons(cred, channel_id, message_id)


def chat_answer_press(press, text=None, clear=False):
    """Acknowledge a button press: `text` only the presser sees, `clear` takes the buttons off, and otherwise they're
    disabled with the pressed one highlighted."""
    chat_link(press["channel_id"])[0].answer_press(press, text, clear)


def chat_react(channel_id, message_id, emoji, on=True):
    """A reaction is a courtesy: a platform that refuses it, or isn't set up, mustn't lose the message."""
    try:
        link, cred = chat_link(channel_id)
    except ChatError:
        return
    (link.react if on else link.unreact)(cred, channel_id, message_id, emoji)


def chat_download(channel_id, file, limit):
    link, cred = chat_link(channel_id)
    return link.download(cred, file, limit)


def chat_via(channel_id, author):
    """Who acted, for logs and notes: "Discord (Dillon)"."""
    return f"{chatlink.name(channel_id)} ({author})"


def folder_name(cwd):
    return (config.get("folder_names") or {}).get(cwd) or os.path.basename(cwd.rstrip("/")) or cwd


def discord_channel_label(channel_id):
    bot = chat_bot(channel_id)
    name = bot.channel_name(channel_id) if bot else ""
    return f"#{name}" if name else chatlink.name(channel_id)


def routes_for(cwd, name=""):
    """The channels an agent is mapped into, by its folder and name (a folder alone: any agent there): whether it
    receives and sends there, and whether it's the first receiver (`master`), which gets messages that name nobody."""
    out = []
    for r in discord.get("routes", []):
        a = next((a for a in r.get("agents", []) if entry_is(a, cwd, name)), None)
        if a:
            first = receivers(r)[:1]
            out.append({"channel_id": r["channel_id"], "channel": discord_channel_label(r["channel_id"]), "platform": chatlink.name(r["channel_id"]),
                        "master": bool(first) and entry_is(first[0], cwd, name), "receives": a["receives"], "sends": a["sends"]})
    if discord.get("dm") == cwd:
        out.append({"channel_id": "dm", "channel": "direct messages", "platform": "Discord", "master": True, "receives": True, "sends": True})
    return out


def auto_approved(rec):
    """Whether this agent's requests to start an agent, or a schedule that may, skip your approval."""
    return bool(config.get("auto_approve_all")) or (rec or {}).get("cwd", "") in (config.get("auto_approve_folders") or [])


def approval_channel(rec):
    """The Discord channel to ask your approval in for an agent: one its folder is in, else its space lead's, else
    the parent space lead's. '' when none of them has one, and the request waits in the Inbox only."""
    space = space_of(rec)[0]
    for cwd, name in ((rec.get("cwd", ""), display_name(rec)), (space_lead(space)[0], ""), (space_lead(space_parent(space))[0] if space_parent(space) else "", "")):
        chans = [r for r in routes_for(cwd, name) if r["channel_id"] != "dm"] if cwd else []
        if chans:
            return chans[0]["channel_id"]
    return ""


KINDS = ("message", "task", "announcement")


def post_async(posts):
    """[(channel, text, reply_to)] posted off the request thread: Discord's latency is none of the caller's business."""
    if not posts or not chat_ready():
        return
    def run():
        for channel, text, ref in posts:
            try:
                chat_post(channel, text, ref)
            except ChatError as err:
                log("discord", "post failed:", err)
    threading.Thread(target=run, daemon=True).start()


def announce(sender_rec, space, text, hops=0, from_discord=None):
    """One message to every live agent in a space, read at the start of each one's next turn; nobody replies."""
    with lock:
        now = time.time()
        recipients = [r for r in sessions.values() if is_live(r, now) and r["id"] != (sender_rec or {}).get("id") and space in reach(r)]
        if from_discord:
            sender = {"from": f"discord:{from_discord['channel_id']}", "from_name": f"{from_discord['author']} on {chatlink.name(from_discord['channel_id'])}", "from_host": "discord",
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
    return msg


def save_discord_files(d):
    """Files you attached, saved where the agent can read them; the lines that tell it so."""
    if not d.get("files"):
        return ""
    if not discord.get("files_in", True):
        return f"\n\n(Files were attached on {chatlink.name(d['channel_id'])}, but saving them is turned off on the Channels page.)"
    limit = int(float(discord.get("max_file_mb") or 10) * 1024 * 1024)
    # Telegram's message ids count up per chat, so its folders name the chat too.
    cid = d["channel_id"]
    folder = (os.path.join(DISCORD_FILES, d["message_id"]) if chatlink.platform(cid) == "discord"
              else os.path.join(HOME, "files", chatlink.platform(cid), f"{cid.split(':', 1)[1]}-{d['message_id']}"))
    lines = []
    for f in d["files"][:10]:
        name = os.path.basename(f["filename"]) or "file"
        try:
            data = chat_download(d["channel_id"], f, limit)
        except (OSError, ChatError) as err:
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
    return f"\n\nAttached on {chatlink.name(d['channel_id'])}:\n" + "\n".join(lines)


def queue_for_entry(a, msg, prefer=None):
    """Deliver to `prefer` or the agent a channel's entry reaches. Not running, a named one's message is held for it
    by name (adopt() hands it over), never to another agent in its folder."""
    if not a.get("name") or prefer:
        return queue_for_folder(a["cwd"], msg, prefer)
    agent = entry_agent(a)
    if agent:
        return queue_for_folder(a["cwd"], msg, agent)
    msg["to"], msg["to_name"] = f"agent:{a['name'].lower()}", a["name"]
    inboxes.setdefault(msg["to"], []).append(msg)
    record(msg)
    return None


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
    """The agent a channel's messages go to: its first receiver, or your DMs'."""
    if dm:
        with lock:
            return successor(discord["dm"]) if discord.get("dm") else None
    a = next((receivers(r)[0] for r in discord.get("routes", []) if r["channel_id"] == channel_id and receivers(r)), None)
    with lock:
        return entry_agent(a) if a else None


def on_slash(cmd):
    """/peerlink <sub> on Discord: (the reply, any buttons)."""
    sub, opt, who = cmd["sub"], cmd["options"], chat_via(cmd["channel_id"], cmd["author"])
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
        lift_limit(f"resumed on {chatlink.name(cmd['channel_id'])} by {cmd['author']}")
        return "Lifted: agents carry on with what was held.", None
    if sub == "clear" and str(opt.get("agent", "")).lower() == "all":
        n = len(live_agents())
        return f"Clear the context of all {n} running agents? Each finishes its current turn first.", choices(["Clear all", "Keep"], "pa:all")
    agent = agent_named(opt["agent"]) if opt.get("agent") else channel_agent(cmd["channel_id"], cmd["dm"]) if sub == "run" else None
    if agent is None:
        return (f"No running agent is called {opt['agent']}." if opt.get("agent") else "No agent is running for this channel; name one with agent:"), None
    name = display_name(agent)
    if sub == "clear":
        code, out = clear_context(agent["id"], who)
        return (f"Clearing {name}'s context{' after its current turn' if agent.get('busy') else ''}." if code == 200 else out["error"]), None
    if sub == "kill":
        return f"End **{name}** ({tilde_path(agent.get('cwd', ''))})? It finishes its current turn first.", choices(["End it", "Keep it"], f"pk:{agent['id']}")
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
    chat_answer_press(press)
    if press["custom_id"].endswith(":0"):
        agents = live_agents()
        for r in agents:
            clear_context(r["id"], chat_via(press["channel_id"], press["author"]))
        chat_post(press["channel_id"], f"Clearing {len(agents)} agents' context, each once its current turn ends.")


def on_discord(d):
    """A message from you on Discord or Telegram: to the chat's first receiver, or to another one it names first."""
    bot = chat_bot(d["channel_id"])
    if not bot:
        return
    # Your own words can work the Inbox: "/inbox", "1 done", or a reply to a task posted for you. Never a bot's.
    if not d.get("bot") and (d["dm"] or d["mentioned"] or bot.reads_messages) and inbox_command(d):
        return
    if d["dm"]:
        route = {"channel_id": d["channel_id"], "agents": [{"cwd": discord.get("dm"), "name": "", "receives": True, "sends": True}] if discord.get("dm") else []}
    else:
        route = next((r for r in discord.get("routes", []) if r["channel_id"] == d["channel_id"]), None)
        # Without the Message Content switch only @mentions carry text; with it, every message in a mapped channel.
        if route is None or not (d["mentioned"] or bot.reads_messages):
            return
    text, entry = d["text"], (receivers(route) or [None])[0]
    # "storefront: ..." or "@storefront ..." addresses another agent this channel reaches, by name.
    head, sep, rest = text.partition(":")
    first = text.split(None, 1)
    for candidate, body in ((head.strip().lstrip("@"), rest.strip()) if sep else ("", ""),
                            (first[0].lstrip("@"), first[1] if len(first) > 1 else "") if first and first[0].startswith("@") else ("", "")):
        if not candidate:
            continue
        for member in receivers(route):
            agent = entry_agent(member)
            names = {entry_label(member).lower(), (display_name(agent) if agent else "").lower()} - {""}
            if candidate.lower() in names:
                text, entry = body or text, member
                break
        else:
            continue
        break
    # A reply to an agent's message here goes to that agent, as the answer to it, whichever agent receives the channel,
    # unless it names another agent first.
    replied = None
    if d.get("ref") and text == d["text"]:
        with lock:
            replied = next((m for m in reversed(messages) if m.get("to") == f"discord:{d['channel_id']}" and m.get("from_cwd")
                            and (m.get("discord_out") or {}).get("message_id") == d["ref"]), None)
        if replied:
            entry = {"cwd": replied["from_cwd"], "name": replied.get("from_name", "")}
    asker = continuation(replied["from"], replied.get("from_name", ""), entry["cwd"]) if replied else None
    cwd = entry["cwd"] if entry else ""
    if not cwd:
        chat_post(d["channel_id"], "No agent here receives messages yet: tick Receives for one in the peerlink web UI, under Channels.", d["message_id"])
        return
    text += save_discord_files(d)
    if not text.strip():
        return
    # A "Task:" or "Announcement:" prefix says what it is; anything else is a message.
    prefix = re.match(r"\s*(task|announcement|announce)\s*:\s*", text, re.I)
    kind = ("announcement" if prefix.group(1).lower().startswith("announce") else "task") if prefix else "message"
    if kind == "announcement":
        space = space_of({"id": "", "cwd": cwd})[0]
        msg = announce(None, space, text[prefix.end():] if prefix else text, from_discord=d)
        n = len(msg["recipients"])
        chat_post(d["channel_id"], f"Announced to {n} agent{'' if n == 1 else 's'} in {space}." if n else f"No agent in {space} is running; nobody heard it.", d["message_id"])
        return
    with lock:
        msg = {
            "id": f"m{next_msg[0]}", "ts": time.time(),
            "from": f"discord:{d['channel_id']}", "from_name": f"{d['author']} on {chatlink.name(d['channel_id'])}", "from_host": "discord",
            "from_cwd": "", "to_cwd": cwd, "from_space": "discord", "to_space": "",
            "text": text[:20000], "expects_reply": True, "reply_to": replied["id"] if replied else None, "hops": 0, "status": "queued", "v": 2,
            "discord": {"channel_id": d["channel_id"], "message_id": d["message_id"]},
        }
        next_msg[0] += 1
        task = new_task(msg, {"id": "", "name": msg["from_name"], "cwd": "", "discord": msg["discord"]}) if kind == "task" else None
        agent = queue_for_entry(entry, msg, asker)
        msg["to_space"] = space_of(agent)[0] if agent else ""
        if task:
            task["owner"], task["space"] = msg["to_name"], msg["to_space"]
            save_tasks()
    log("discord in", msg["id"], d["author"], "->", msg["to_name"])
    set_reaction(msg, "👀")
    if not agent:
        chat_post(d["channel_id"], f"{entry_label(entry)} isn't running right now. Your message waits up to an hour for it.", d["message_id"])


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
                chat_react(channel, message, old, on=False)
            if new:
                chat_react(channel, message, new)


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


def labelled(label, text):
    """label then text, for Discord. Text that opens with a block (a "## " header, a list, a quote, a code fence)
    starts on the next line: Discord renders those only at the start of a line."""
    if re.match(r"\s*(#{1,3} |[-*] |> |```|\d+\. )", text or ""):
        return f"{label}\n{text.lstrip()}"
    return f"{label} {text}"


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
    body = text if reply_ref else labelled(f"**{name}**:", text)
    # A question in a "> " quote keeps its options in the quote too, listed even under buttons; otherwise they're
    # listed only when there are no buttons.
    quoted = text.rstrip().rsplit("\n", 1)[-1].lstrip().startswith(">")
    if options and (quoted or not use_buttons):
        body = body.rstrip() + "\n" + "\n".join(f"{'> ' if quoted else ''}{i + 1}. {o}" for i, o in enumerate(options))
    try:
        sent = chat_post(channel_id, body, reply_ref, choices(options, f"pl:{mid}") if use_buttons else None, uploads)
    except ChatError as err:
        return 502, {"error": f"{chatlink.name(channel_id)} refused it: {err}"}
    with lock:
        msg = {
            "id": mid, "ts": time.time(), "from": sender_rec["id"], "from_name": name,
            "from_host": HOSTNAME, "to": f"discord:{channel_id}", "to_name": f"{discord_channel_label(channel_id)} on {chatlink.name(channel_id)}",
            "from_cwd": sender_rec.get("cwd", ""), "to_cwd": "", "from_space": space_of(sender_rec)[0], "to_space": "discord",
            "text": text[:20000], "expects_reply": bool(use_buttons) or expects_reply, "reply_to": reply_to, "hops": hops, "status": "delivered", "v": 2,
            "options": list(options or []), "files": [os.path.expanduser(str(f)) for f in files or []],
            "discord_out": {"channel_id": channel_id, "message_id": sent},
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
        chat_answer_press(press, clear=True)
        return lift_limit(f"resumed on {chatlink.name(press['channel_id'])} by {press['author']}")
    if press["custom_id"].startswith("pk:"):
        _, sid, idx = (press["custom_id"].split(":") + ["", ""])[:3]
        code, out = terminate(sid, chat_via(press["channel_id"], press["author"])) if idx == "0" else (200, {"kept": True})
        chat_answer_press(press)
        if code != 200:
            chat_post(press["channel_id"], out.get("error", "Couldn't end it."))
        return
    _, mid, idx = (press["custom_id"].split(":") + ["", ""])[:3]
    asked = next((m for m in messages if m["id"] == mid), None)
    options = (asked or {}).get("options") or []
    if not asked or not idx.isdigit() or int(idx) >= len(options):
        chat_answer_press(press, "That question isn't open anymore.")
        return
    with lock:
        settled = asked.get("handled") or asked.get("inbox_done") or any(m.get("reply_to") == mid for m in messages)
    if settled:
        # Answered already, in the web UI, the terminal or another press: its buttons go, and this one does nothing.
        chat_answer_press(press, clear=True)
        chat_post(press["channel_id"], "That question was already answered, so this choice wasn't sent.", (asked.get("discord_out") or {}).get("message_id"))
        return
    choice = options[int(idx)]
    done = decide_relayed(choice, asked.get("text", ""), f"{chat_via(press['channel_id'], press['author'])}, on {asked.get('from_name', 'an agent')}'s question")
    chat_answer_press(press)
    with lock:
        msg = {
            "id": f"m{next_msg[0]}", "ts": time.time(),
            "from": f"discord:{press['channel_id']}", "from_name": f"{press['author']} on {chatlink.name(press['channel_id'])}", "from_host": "discord",
            "from_cwd": "", "to_cwd": asked.get("from_cwd", ""), "from_space": "discord", "to_space": asked.get("from_space", ""),
            "text": f"Chose: {choice}" + (f" (peerlink {', '.join(done)})" if done else ""), "expects_reply": False, "reply_to": mid, "hops": 0, "status": "queued", "v": 2,
            "discord": {"channel_id": press["channel_id"], "message_id": (asked.get("discord_out") or {}).get("message_id", "")},
        }
        next_msg[0] += 1
        queue_for_folder(asked.get("from_cwd", ""), msg, continuation(asked.get("from", ""), asked.get("from_name", ""), asked.get("from_cwd", "")))
    log("chat press", msg["id"], press["author"], "->", msg["to_name"], choice)


def chat_target(word):
    """A chat `to` names: "discord", "telegram", "telegram#family", as (platform name or "" for any, "#channel" or "").
    None when it names no chat. "discord" is any platform, as it was before there were others."""
    head, sep, channel = word.partition("#")
    if head not in ("discord", "telegram"):
        return None
    return ("" if head == "discord" else chatlink.NAMES[head]), ("#" + channel.lstrip("#") if sep else "")


def chats_matching(chans, target):
    platform, channel = target
    return [r for r in chans if (not platform or r.get("platform") == platform) and (not channel or r["channel"].lower() == channel)]


def send_discord(sender_rec, to, text, parent, hops, options=None, files=None, expects_reply=False):
    """`to: "discord"`, `"telegram"` or one with "#name", or a reply to a message from a chat. None when it's not for one."""
    if not sender_rec or not chat_ready():
        return None
    if parent and parent.get("discord") and parent.get("to_cwd") == sender_rec.get("cwd"):
        d = parent["discord"]
        out = post_to_discord(sender_rec, d["channel_id"], text, d["message_id"], parent["id"], hops, options, files, expects_reply)
        if out[0] == 200:
            set_reaction(parent, "✅")
        return out
    target = chat_target(to.strip().lower())
    if target is None:
        return None
    mine = chats_matching([r for r in routes_for(sender_rec.get("cwd", ""), display_name(sender_rec)) if r["channel_id"] != "dm" and r["sends"]], target)
    where = target[0] or "chat"
    if not mine:
        return 404, {"error": f"this agent isn't ticked to send in any {where} channel; the mapping is in the web UI, under Channels"}
    if len(mine) > 1:
        return 409, {"error": f"this agent is in several {where} channels: say which, e.g. " + ", ".join(f"{r['platform'].lower()}{r['channel']}" for r in mine)}
    return post_to_discord(sender_rec, mine[0]["channel_id"], text, None, None, hops, options, files, expects_reply)


def user_send(sender_rec, word, text, kind, parent_task, expects_reply, hops, options, files):
    """A message or task for you: it waits in your Inbox, and is posted to Discord when the agent has a channel there."""
    chans = [r for r in routes_for(sender_rec.get("cwd", ""), display_name(sender_rec)) if r["channel_id"] != "dm" and r["sends"]] if chat_ready() else []
    target = chat_target(word)
    if target:
        chans = chats_matching(chans, target)
    if target and not chans:
        return 404, {"error": f"this agent isn't mapped to that {target[0] or 'chat'} channel; send it to \"user\" to put it in their Inbox, or map a channel under Channels"}
    channel = chans[0]["channel_id"] if chans else ""
    # A question from an agent with no channel of its own goes where its approvals do, its space lead's channel, so it's
    # answered (and leaves the Inbox) on Discord too.
    if not channel and kind != "task" and expects_reply and word in USER_ALIASES and chat_ready():
        channel = approval_channel(sender_rec)
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
            sent = chat_post(channel, labelled(f"**{msg['from_name']}** has a task for you ({task['id']}):", text),
                             None, choices(["Done", "Couldn't do it"], f"pt:{task['id']}"))
            with lock:
                task["discord_out"] = {"channel_id": channel, "message_id": sent}
                save_tasks()
        except ChatError as err:
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
        # On Discord you answer by writing in the channel, not always as a reply: anything you send that agent after it
        # counts, and so does anything you write in the channel it was posted to, whichever agent there receives it.
        # Walked newest first, these hold the folders and channels Discord has written to since the message at hand.
        later_cwds, later_channels, questions = set(), set(), []
        for m in reversed(recent):
            if str(m.get("from", "")).startswith("discord:"):
                later_cwds.add(m.get("to_cwd"))
                later_channels.add(m["from"])
                continue
            if not m.get("expects_reply") or m.get("task") or m.get("inbox_done") or m["id"] in replied:
                continue
            if not (m.get("to") == "user" or str(m.get("to", "")).startswith("discord:")):
                continue
            if m.get("from_cwd") in later_cwds or m.get("to") in later_channels:
                continue
            questions.append(m)
        for m in reversed(questions):
            out.append({"kind": "question", "id": m["id"], "who": m["from_name"], "since": m["ts"], "title": task_title_of(m["text"])[:120], "text": m["text"]})
        out += [{"kind": "feedback", "id": f["id"], "who": f.get("from_name") or "an agent", "since": f["ts"], "title": task_title_of(f["text"])[:120],
                 "text": f["text"], "type": f.get("type", "bug")} for f in feedback.values() if f.get("status") == "new"]
        out += [{"kind": "approval", "id": x["id"], "who": x["requester"].get("name", ""), "since": x["created"],
                 "title": f"Start an agent in {tilde_path(x['dir'])}", "text": f"{x['purpose']}\n\nSpace: {x['space']}\nFirst prompt: {x['prompt']}"}
                for x in spawns.values() if x["status"] == "pending"]
        out += [{"kind": "approval", "id": c["id"], "who": c["requester"].get("name", ""), "since": c["created"],
                 "title": f"Schedule for {tilde_path(c['folder'])}: {when_text(c)}",
                 "text": f"{c['prompt']}\n\nApproving it: {start_note(c)}."}
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
        return decide(iid, action == "approve", chatlink.name(discord_from["channel_id"]) if discord_from else "the web UI")
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
                close_buttons(m)  # settled here, so its Discord post can't be answered as well
                return 200, {"ok": True}
            if action != "reply" or not text:
                return 400, {"error": "reply with text, or dismiss"}
            if discord_from:
                d = discord_from
                msg = {"from": f"discord:{d['channel_id']}", "from_name": f"{d['author']} on {chatlink.name(d['channel_id'])}", "from_host": "discord",
                       "discord": {"channel_id": d["channel_id"], "message_id": d["message_id"]}}
            else:
                msg = {"from": "user", "from_name": f"{USER_NAME} (web UI)", "from_host": HOSTNAME}
            msg.update({"id": f"m{next_msg[0]}", "ts": time.time(), "from_cwd": "", "to_cwd": m.get("from_cwd", ""),
                        "from_space": "", "to_space": m.get("from_space", ""), "text": text[:20000], "expects_reply": False,
                        "reply_to": iid, "hops": 0, "status": "queued", "v": 2})
            next_msg[0] += 1
            agent = queue_for_folder(m.get("from_cwd", ""), msg, continuation(m.get("from", ""), m.get("from_name", ""), m.get("from_cwd", "")))
        close_buttons(m)
        return 200, {"id": msg["id"], "to": msg["to_name"], "waiting": not agent}
    return 400, {"error": "kind is approval, task, question or feedback"}


def close_task_buttons(t):
    """Grey out the Done / Couldn't do it buttons on a task's Discord post once it's settled elsewhere."""
    out = t.get("discord_out") or {}
    if not out.get("message_id"):
        return
    def edit():
        try:
            chat_clear_buttons(out["channel_id"], out["message_id"])
        except ChatError as err:
            log("discord", "could not clear task buttons:", err)
    threading.Thread(target=edit, daemon=True).start()


def on_task_press(press):
    _, tid, idx = (press["custom_id"].split(":") + ["", ""])[:3]
    t = tasks.get(tid)
    if not t or t["status"] not in TASK_OPEN:
        chat_answer_press(press, f"Task {tid} is already {t['status'] if t else 'gone'}.")
        return
    action = "done" if idx == "0" else "failed"
    chat_answer_press(press)
    update_task(tid, {"status": action}, web=True)
    log("chat press", tid, press["author"], action)


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
    say = lambda line: chat_post(channel, line, d["message_id"])
    # A reply to a task posted for you settles it: "done", "done: it's set", "failed: no access".
    task = next((t for t in tasks.values() if t.get("user") and d.get("ref") and (t.get("discord_out") or {}).get("message_id") == d["ref"]
                 and (t.get("discord_out") or {}).get("channel_id") == channel), None)
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
        chat_post(channel, f"End **{display_name(agent)}** ({tilde_path(agent.get('cwd', ''))})? It finishes its current turn first.",
                  d["message_id"], choices(["End it", "Keep it"], f"pk:{agent['id']}"))
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
        limit, task_id = hop_limit(parent) if parent else (config["max_hops"], "")
        # The answer to a question asked at the limit still gets through, as the thread's last word: it can't be replied to.
        last_word = hops > limit and kind == "message" and bool(parent and parent.get("expects_reply"))
        if hops > limit and not last_word:
            if task_id:
                return 429, {"error": f"the thread of task {task_id} hit its {limit}-reply limit: report where it stands with update_task, "
                                      "or tell your user if it needs more back and forth"}
            return 429, {"error": f"conversation hit max_hops={limit}; stop and tell your user"}
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
    # "discord", "telegram" or one with "#channel", not an agent whose name starts with discord, like discord-voice.
    if sender_rec and (word in USER_ALIASES or (kind == "task" and chat_target(word) and not parent)):
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
            msg["text"] += f"\n\n(peerlink: this thread reached its {limit}-reply limit with this answer, so it can't be answered. If more is needed, tell your user.)"
        # Worth starting a turn for: a question, a task, or the answer to a question this agent asked. An FYI, or a
        # reply to a reply, waits for the agent's next turn (wake_for "all" wakes for everything, as before 0.8.11).
        msg["wake"] = config.get("wake_for", "needed") == "all" or msg["expects_reply"] or bool(parent and parent.get("expects_reply"))
        next_msg[0] += 1
        if kind == "task":
            new_task(msg, {"id": sender, "name": msg["from_name"], "cwd": msg["from_cwd"]}, parent_task)
        inboxes.setdefault(target["id"], []).append(msg)
        if sender in sent_times:
            sent_times[sender].append(now)
        record(msg)
    log("msg", msg["id"], msg["from_name"], "->", msg["to_name"])
    out = {"id": msg["id"], "to": msg["to_name"], "status": "queued", "task": msg.get("task")}
    if last_word:
        out["note"] = f"delivered as this thread's last reply: it reached its {limit}-reply limit, so neither of you can reply to it"
    return 200, out


def hop_limit(parent):
    """How many replies the thread `parent` is in may run to, and the open task it started from ('' for none). A loop
    of acknowledgements is what max_hops stops; a lead and its worker trading instructions and reports under a task
    still open pass ten replies doing real work, so a task's thread gets three times as many."""
    by_id = {m["id"]: m for m in messages}
    m, seen = parent, 0
    while m and seen < 200:
        if m.get("task"):
            t = tasks.get(m["task"])
            if t and t["status"] in TASK_OPEN:
                return config["max_hops"] * 3, m["task"]
            break
        m, seen = by_id.get(m.get("reply_to") or ""), seen + 1
    return config["max_hops"], ""


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
            change["note"] = str(body.get("note") or "").strip()[:int(config.get("max_message_chars") or 8000)]
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
    """The live agent in a folder: who gets what's sent to the folder rather than to one agent. Where agents share it,
    the one going by the folder's name (its lead, say, not the agents it started), else the most recently seen."""
    live = [r for r in sessions.values() if cwd and r.get("cwd") == cwd and is_live(r)]
    named = [r for r in live if display_name(r).lower() == folder_name(cwd).lower()]
    return max(named or live, key=lambda r: r["last_seen"]) if live else None


def tmux_pane_pid(name):
    """The process a tmux session the daemon started runs, 0 when it's gone."""
    tmux = shutil.which("tmux") or next((p for p in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux") if os.access(p, os.X_OK)), "")
    try:
        out = subprocess.run([tmux, "list-panes", "-t", f"={name}:", "-F", "#{pane_pid}"], timeout=5, capture_output=True, text=True).stdout
        return int(out.split()[0]) if out.split() else 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


def descends_from(pid, ancestor):
    """Whether process `pid` is `ancestor` or runs under it."""
    try:
        pid = int(pid or 0)
        for _ in range(12):
            if pid == ancestor:
                return True
            if pid <= 1:
                return False
            pid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], timeout=5, capture_output=True, text=True).stdout.strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return False


def spawn_of(rec, fresh):
    """The started spawn this newly registering agent is the one for: the spawn whose tmux pane it runs under, else
    (no pane to go by) the oldest unclaimed one in its folder when rec is a new conversation. None for any other."""
    cands = [sp for sp in spawns.values() if sp["status"] == "started" and not sp.get("agent_id") and sp["dir"] == rec.get("cwd")
             and time.time() - sp["updated"] < 86400]
    for sp in sorted(cands, key=lambda x: x["updated"]):
        pane = tmux_pane_pid(sp["tmux"]) if sp.get("tmux") else 0
        if (descends_from(rec.get("pid"), pane) if pane else fresh):
            return sp
    return None


def adopt(rec, old=None, spawn=None):
    """Give rec the messages still queued for sessions that are gone: `old` (one it replaces), or any in its folder.
    `spawn`: the spawn rec is the agent for, whose first prompt waits for it alone."""
    moved = 0
    for sid in list(inboxes):
        if sid == rec["id"]:
            continue
        other = sessions.get(sid)
        if sid != old and other and is_live(other):
            continue
        if sid.startswith("agent:"):
            # Held for an agent by name (a schedule's run): only that agent takes it, never another in its folder.
            box = list(inboxes[sid]) if sid == f"agent:{display_name(rec).lower()}" else []
        elif sid.startswith("spawn:"):
            # A first prompt for the agent a spawn started: that one takes it, found by its tmux pane, else the first
            # new one in that folder.
            box = list(inboxes[sid]) if spawn and sid == f"spawn:{spawn['id']}" else []
        else:
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
    ends = lambda m, a, b: {m.get(a), m.get(b)} - {None, ""}
    # Agents sharing a folder don't share each other's threads: one to an agent is that agent's (or what it became
    # after /clear), and only one whose agent is gone falls to the folder's agent.
    whose = {}
    def mine(m, side):
        sid, cwd = m.get(side) or "", m.get(side + "_cwd") or ""
        if sid == rec["id"]:
            return True
        if not cwd or cwd != rec.get("cwd"):
            return False
        if sid not in whose:
            who = continuation(sid, m.get(side + "_name") or "", cwd) or successor(cwd)
            whose[sid] = who["id"] if who else None
        return whose[sid] in (None, rec["id"])
    # Handled without a reply_to too: once the two have written to each other since, the right way round, it's done.
    # Walked newest first, `later` holds (sender, recipient) pairs of what came after the message at hand.
    later, owes, waiting = set(), [], []
    for m in reversed(messages):
        # A task is tracked by its own status below, not by whether its message was answered.
        if (m.get("expects_reply") and not m.get("task") and not m.get("handled") and now - m["ts"] < max_age
                and m["id"] not in answered):
            frm, to = ends(m, "from", "from_cwd"), ends(m, "to", "to_cwd")
            if mine(m, "to") and m.get("status") != "expired" and not any((a, b) in later for a in me for b in frm):
                owes.append(gist(m, "from_name"))
            if mine(m, "from") and not any((a, b) in later for a in to for b in me):
                waiting.append(gist(m, "to_name"))
        later.update((a, b) for a in ends(m, "from", "from_cwd") for b in ends(m, "to", "to_cwd"))
    owes.reverse()
    waiting.reverse()
    def tgist(t, who):
        return {"id": t["id"], "with": who, "ago_s": round(now - t["created"]), "text": t["title"], "status": t["status"]}
    live = [t for t in tasks.values() if t["status"] in TASK_OPEN]
    mine = [tgist(t, t["requester"].get("name", "")) for t in live if owns_task(rec, t)]
    asked = [tgist(t, t["owner"]) for t in live if t["requester"].get("id") == rec["id"] or (rec.get("cwd") and t["requester"].get("cwd") == rec.get("cwd"))]
    return {"owes": owes[-limit:], "waiting": waiting[-limit:], "tasks": mine[-limit:], "asked": asked[-limit:]}


def set_member_role(sid, to, role):
    """A space lead sets the role of an agent in its space, or in a space inside it (or its own). It's that agent's,
    kept across /clear, and wins over its folder's .peerlink role: lines; empty goes back to those."""
    with lock:
        by = sessions.get(sid)
        if by is None:
            return 404, {"error": "unknown agent; register again"}
    target, err = resolve(str(to or ""))
    if not target:
        return 404, {"error": err or f"no running agent is called {to}"}
    with lock:
        mine, space, seen = space_of(by)[0] if leads(by) else "", space_of(target)[0], set()
        allowed = target["id"] == sid
        while mine and space and space not in seen and not allowed:
            seen.add(space)
            allowed = space == mine
            space = space_parent(space)
        if not allowed:
            return 403, {"error": f"only the lead of {display_name(target)}'s space, or of a space it sits inside, sets its role"}
        role = str(role or "").strip()[:ROLE_MAX]
        config["sessions"].setdefault(target["id"], {})["role"] = role
        save_config()
        if target["id"] != sid:
            task_message(None, target.get("cwd", ""), f"{display_name(by)}, your space lead, set your role:\n{role}" if role
                         else f"{display_name(by)}, your space lead, cleared the role it set for you; your folder's applies again.", prefer=target)
    log("role", display_name(target), "set by", display_name(by))
    return 200, {"agent": display_name(target), "role": role or folder_role(target)}


def rename_self(sid, name):
    """An agent names itself: this agent only, kept across /clear and resume. Its folder's name, which the next
    agent there takes, stays as it is; you change that in the web UI."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", str(name or "").strip())[:40].strip("-")
    if not name:
        return 400, {"error": "name is letters, digits, dots, dashes or underscores"}
    with lock:
        rec = sessions.get(sid)
        if rec is None:
            return 404, {"error": "unknown agent; register again"}
        if any(r["id"] != sid and is_live(r) and display_name(r).lower() == name.lower() for r in sessions.values()):
            return 409, {"error": f"another running agent is called {name}"}
        was = display_name(rec)
        rec["name"] = name
        config["sessions"].setdefault(sid, {})["nickname"] = name
        save_config()
    forget_spaces()
    log("agent", sid, "renamed", was, "->", name)
    return 200, {"name": name, "was": was}


def close_threads(sid, ids):
    """An agent says requests it owes or waits on are handled: they leave its open threads for good."""
    with lock:
        rec = sessions.get(sid)
        if rec is None:
            return 403, {"error": "unknown agent; register again"}
        me = {sid, rec.get("cwd") or sid}
        want = {str(i).strip() for i in ids if str(i).strip()}
        if not want:
            return 400, {"error": "give the ids to close, like m123"}
        mine = [m for m in messages if m["id"] in want and ({m.get("from"), m.get("from_cwd"), m.get("to"), m.get("to_cwd")} & me)]
        for m in mine:
            m["handled"] = m["inbox_done"] = True
            append_log({"id": m["id"], "handled": True, "inbox_done": True})
    for m in mine:
        close_buttons(m)  # an ask on Discord settled another way can't be answered there twice
    missing = sorted(want - {m["id"] for m in mine})
    return 200, {"closed": [m["id"] for m in mine], **({"not_yours": missing} if missing else {})}


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
            # While the usage limit holds, or until its turn after it lifts, an agent keeps what it was handed on purpose:
            # handing it out again is noise.
            held = usage_limit or (lifting and lifting["slots"].get(sid, 0) > now)
            again = [] if held else [m for m, ts in mine.values() if now - ts >= REDELIVER_S]
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
         "msg": msg["id"], "parent": parent_task, "owner_id": msg.get("to", "") if msg.get("to", "") in sessions else ""}
    next_task[0] += 1
    tasks[t["id"]] = t
    msg["kind"], msg["task"] = "task", t["id"]
    save_tasks()
    return t


def task_message(t, to_cwd, text, sender=None, reply_to=None, kind="message", prefer=None):
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
    agent = queue_for_folder(to_cwd, msg, prefer)
    msg["to_space"] = space_of(agent)[0] if agent else ""
    return msg


def update_task(tid, body, web=False):
    """An owner marks its task in progress, done or failed; its requester can cancel it; the web UI can do any of
    that, reopen it, or give it to another folder."""
    status = body.get("status") or ""
    note = (body.get("note") or "").strip()
    owner = (body.get("owner") or "").strip()
    # Whoever asked reads the note whole, so it's never cut: one over the message limit is refused, saying so.
    limit = int(config.get("max_message_chars") or 0)
    if limit and len(note) > limit:
        return 413, {"error": f"note is {len(note)} characters, over the {limit} limit: write the details to a file and give its path with a short summary"}
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
            owns = owns_task(by, t)
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
            t["owner_cwd"], t["owner"], t["owner_id"] = owner, folder_name(owner), ""
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
        # From the agent that owns it, and to the agent that asked: not whichever goes by either folder's name.
        owner = task_owner(t)
        asker = continuation(req.get("id") or "", req.get("name") or "", req.get("cwd") or "")
        if t["status"] == "cancelled" and t["owner_cwd"]:
            # The owner stops; whoever cancelled it already knows.
            task_message(t, t["owner_cwd"], f"Task {t['id']} was cancelled{' in the web UI' if web else ' by ' + req.get('name', 'whoever asked')}: stop work on it. {t['title']}")
        elif d:
            posts.append((d["channel_id"], f"**{t['owner']}**: {line}", d["message_id"]))
            orig = next((m for m in messages if m["id"] == t["msg"]), None)
            if orig:
                set_reaction(orig, "✅" if t["status"] == "done" else "❌")
        elif req.get("cwd"):
            task_message(t, req["cwd"], line if owner else f"{t['owner']}: {line}", sender=owner, reply_to=t["msg"], prefer=asker)
        if t["status"] == "failed" and not d and not t.get("user"):
            chans = [r for r in routes_for(t["owner_cwd"], t.get("owner", "")) + routes_for(req.get("cwd", ""), req.get("name", "")) if r["channel_id"] != "dm" and r["sends"]]
            if chans:
                posts.append((chans[0]["channel_id"], f"**{t['owner']}**: {line} (asked by {req.get('name') or 'an agent'})", None))
    if posts and chat_ready():
        def post_all():
            for channel, text, ref in posts:
                try:
                    chat_post(channel, text, ref)
                except ChatError as err:
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
        name = re.sub(r"[^A-Za-z0-9_.-]", "-", str(body.get("name") or "").strip())[:40].strip("-")
        role = str(body.get("role") or "").strip()[:ROLE_MAX]
        if running and not role:
            role = purpose[:ROLE_MAX]  # not the folder's role: that one is the running agent's
        sp = {"id": f"s{n}", "dir": folder, "purpose": purpose[:1000], "prompt": prompt[:8000], "name": name, "role": role,
              "space": clean_space(body.get("space") or "") or space_of(by)[0],
              "requester": {"id": by["id"], "name": display_name(by), "cwd": by.get("cwd", "")},
              "status": "pending", "created": time.time(), "updated": time.time(), "note": ""}
        spawns[sp["id"]] = sp
        save_spawns()
        channel = approval_channel(by)
        auto = auto_approved(by)
    log("spawn asked", sp["id"], sp["requester"]["name"], folder)
    if auto:
        decide_spawn(sp["id"], True, "auto-approve")
        if channel and chat_ready(channel):
            post_async([(channel, f"**{sp['requester']['name']}** started an agent in `{tilde_path(folder)}` without asking (auto-approve is on): {purpose[:600]}", None)])
        return 200, {"id": sp["id"], "status": sp["status"], "note": f" Auto-approved: {sp['note']}"}
    if channel and chat_ready(channel):
        try:
            sent = chat_post(channel,
                             f"**{sp['requester']['name']}** asks to start an agent in `{tilde_path(folder)}` (space {sp['space']}): {purpose}\nIts first prompt: {prompt[:600]}",
                             None, choices(["Approve", "Deny"], f"ps:{sp['id']}"))
            with lock:
                sp["discord_out"] = {"channel_id": channel, "message_id": sent}
                save_spawns()
        except ChatError as err:
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


def best_python():
    """The newest Python 3 on this machine, to run the daemon: macOS's own python3 (3.9) no longer gets security fixes,
    and PATH often lists it ahead of Homebrew's. PEERLINK_PYTHON picks one outright."""
    if os.environ.get("PEERLINK_PYTHON"):
        return os.environ["PEERLINK_PYTHON"]
    best, best_ver = sys.executable, sys.version_info[:2]
    names = ["python3", "python"] + [f"python3.{n}" for n in range(20, 9, -1)]
    found = {shutil.which(n) for n in names} | {f"{d}/{n}" for d in ("/opt/homebrew/bin", "/usr/local/bin") for n in names}
    for p in sorted(p for p in found if p and os.access(p, os.X_OK)):
        try:
            out = subprocess.run([p, "-c", "import sys; print(*sys.version_info[:2])"], capture_output=True, text=True, timeout=5).stdout.split()
            ver = (int(out[0]), int(out[1]))
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            continue
        if ver > best_ver:
            best, best_ver = p, ver
    return best


def launch(sp):
    """Start Claude Code in the folder: (how, "") when it started, ("", why not) when it didn't."""
    claude = find_claude()
    if not claude:
        return "", "Claude Code (claude) isn't on this machine's PATH"
    tmux = shutil.which("tmux") or next((p for p in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux") if os.access(p, os.X_OK)), "")
    try:
        if tmux:
            name = "peerlink-" + re.sub(r"[^A-Za-z0-9_-]", "-", os.path.basename(sp["dir"]))[:30] + "-" + sp["id"]
            # One started for this before is still there: its agent hasn't registered yet, so don't start a second.
            if subprocess.run([tmux, "has-session", "-t", "=" + name], timeout=10, capture_output=True).returncode == 0:
                pane = subprocess.run([tmux, "capture-pane", "-p", "-t", f"={name}:"], timeout=10, capture_output=True, text=True).stdout
                if "trust this folder" in pane:
                    return "", f"Claude Code there is waiting for you to trust the folder: run tmux attach -t {name} and choose \"Yes, I trust this folder\""
                return f"still starting in tmux (tmux attach -t {name})", ""
            subprocess.run([tmux, "new-session", "-d", "-s", name, "-c", sp["dir"], claude], check=True, timeout=10, capture_output=True)
            sp["tmux"] = name
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
                # The first prompt waits for the new agent's folder, as a message from whoever asked, so replies go back.
                req = sessions.get(sp["requester"]["id"]) or successor(sp["requester"]["cwd"])
                msg = {"id": f"m{next_msg[0]}", "ts": time.time(), "from": req["id"] if req else "peerlink",
                       "from_name": display_name(req) if req else sp["requester"]["name"], "from_host": HOSTNAME,
                       "from_cwd": sp["requester"]["cwd"], "to_cwd": sp["dir"], "from_space": sp["space"], "to_space": sp["space"],
                       "text": sp["prompt"], "expects_reply": True, "reply_to": None, "hops": 0, "status": "queued", "v": 2}
                next_msg[0] += 1
                # Held for the agent this starts, never one already running there: that may be the one that asked.
                msg["to"], msg["to_name"] = f"spawn:{sid}", f"the agent starting for {sid}"
                inboxes.setdefault(msg["to"], []).append(msg)
                record(msg)
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
    if not out.get("message_id"):
        return
    def edit():
        try:
            chat_clear_buttons(out["channel_id"], out["message_id"])
        except ChatError as err:
            log("discord", "could not clear buttons:", err)
    threading.Thread(target=edit, daemon=True).start()


def decide_relayed(choice, asked_text, via):
    """You chose "Approve c10" (or "Deny s3", "Approve all 4") on an agent's question about a pending schedule or
    start: the press is yours, so it decides them as peerlink's own prompt would. The ids come from the choice, or
    from the question when the choice names none and either says "all" or the question names just one pending."""
    verb = re.match(r"\s*(approve|deny)\b", choice, re.I)
    if not verb:
        return []
    with lock:
        pending = lambda ids: [i for i in dict.fromkeys(ids) if (schedules if i[0] == "c" else spawns).get(i, {}).get("status") == "pending"]
        ids = pending(re.findall(r"\b([cs]\d+)\b", choice.lower()))
        if not ids:
            named = pending(re.findall(r"\b([cs]\d+)\b", asked_text.lower()))
            ids = named if re.search(r"\ball\b", choice, re.I) or len(named) == 1 else []
    approve, done = verb.group(1).lower() == "approve", []
    for i in ids:
        code, _ = (decide_schedule if i[0] == "c" else decide_spawn)(i, approve, via)
        if code == 200:
            done.append(f"{'approved' if approve else 'denied'} {i}")
    return done


def on_spawn_press(press, decide=None, items=None):
    """Approve or Deny on Discord, for a request to start an agent or a schedule that may start one."""
    decide, items = decide or decide_spawn, spawns if items is None else items
    _, sid, idx = (press["custom_id"].split(":") + ["", ""])[:3]
    sp = items.get(sid)
    if not sp or sp["status"] != "pending":
        chat_answer_press(press, f"{sid} is already {sp['status'] if sp else 'gone'}.")
        return
    chat_answer_press(press)
    decide(sid, idx == "0", chat_via(press["channel_id"], press["author"]))


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


def schedule_target(body, by=None):
    """(folder, agent name) a schedule goes to, from `agent` (a running agent's name, or a folder: the web UI's one
    field takes either) or `folder`; an agent's own schedule is its own. Raises ValueError saying what's wrong."""
    name = str(body.get("agent") or "").strip().lstrip("@")
    target = agent_named(name) if name else None
    folder = body.get("folder")
    if name and target is None:
        if folder or not os.path.isdir(os.path.expanduser(name)):
            raise ValueError(f"{name} isn't a folder on this machine" if "/" in name else f"no running agent is called {name}; give its folder instead")
        folder, name = name, ""
    if not name and not folder and by:
        target = by
    folder = os.path.abspath(os.path.expanduser(str(target.get("cwd") if target else folder or "")))
    if not os.path.isdir(folder):
        raise ValueError(f"{folder} isn't a folder on this machine")
    return folder, display_name(target) if target else ""


def schedule_when(body, now):
    """(cron, at, next run) from `cron` (a repeat) or `at` (once). Raises ValueError saying what's wrong."""
    cron, at = str(body.get("cron") or "").strip(), body.get("at")
    if bool(cron) == bool(at):
        raise ValueError("give either cron (a repeat, like 0 9 * * 1-5) or at (once, like 'in 2h' or 2026-10-25 09:00)")
    next_run = cron_next(cron, now) if cron else parse_at(at, now)
    if not cron and next_run <= now:
        raise ValueError("that time has passed")
    return cron, None if cron else next_run, next_run


def start_note(c):
    """What approving a schedule that may start an agent means, said plainly: most often the agent is already
    running and just gets the prompt."""
    who = c.get("agent") or folder_name(c["folder"])
    with lock:
        running = any(is_live(r) and display_name(r).lower() == who.lower() for r in sessions.values()) or bool(successor(c["folder"]))
    return f"it goes to {who}, and starts a new agent only if {who} isn't running ({'it is now' if running else 'it isn’t now'})"


def create_schedule(body, by=None):
    """A schedule from an agent (`by`) or from you (None). An agent's that may start one waits for your approval.
    It goes to an agent: that one wherever it runs, else a new one started in the folder it was last in."""
    now = time.time()
    prompt = str(body.get("prompt") or "").strip()
    start = body.get("start") is not False
    try:
        folder, name = schedule_target(body, by)
        if not prompt:
            raise ValueError("prompt is required: what the agent should do when it runs")
        cron, at, next_run = schedule_when(body, now)
    except ValueError as err:
        return 400, {"error": str(err)}
    with lock:
        n = max([int(i[1:]) for i in schedules] or [0]) + 1
        c = {"id": f"c{n}", "folder": folder, "agent": name, "prompt": prompt[:8000], "cron": cron, "at": at,
             "start": start, "next_run": next_run, "last_run": None, "runs": 0, "note": "",
             "requester": {"id": by["id"], "name": display_name(by), "cwd": by.get("cwd", "")} if by else {"name": USER_NAME, "cwd": ""},
             "status": "pending" if by and start and not auto_approved(by) else "active", "created": now, "updated": now}
        schedules[c["id"]] = c
        save_schedules()
        channel = approval_channel(by) if by else ""
    log("schedule", c["id"], c["status"], c["agent"] or tilde_path(folder), when_text(c))
    if by and start and c["status"] == "active" and channel and chat_ready(channel):
        post_async([(channel, f"**{c['requester']['name']}** scheduled `{tilde_path(folder)}` ({when_text(c)}) without asking (auto-approve is on): {start_note(c)}.\n{prompt[:600]}", None)])
    if c["status"] == "pending" and channel and chat_ready(channel):
        try:
            sent = chat_post(channel,
                             f"**{c['requester']['name']}** asks to schedule `{tilde_path(folder)}` ({when_text(c)}): {start_note(c)}.\n{prompt[:600]}",
                             None, choices(["Approve", "Deny"], f"pc:{c['id']}"))
            with lock:
                c["discord_out"] = {"channel_id": channel, "message_id": sent}
                save_schedules()
        except ChatError as err:
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


def edit_schedule(cid, body):
    """You change a schedule: who gets it, when, what it says, whether it starts an agent. Only what's given changes;
    its state doesn't, so a pending one still waits for approval and an ended one for Enable again."""
    now = time.time()
    with lock:
        c = schedules.get(cid)
        if c is None:
            return 404, {"error": f"no schedule {cid}"}
        try:
            if "agent" in body and str(body["agent"]).strip().lstrip("@").lower() == (c.get("agent") or "").lower():
                own = schedule_agent(c)  # unchanged, whether or not it's running now; where it runs if it is
                folder, name = (own.get("cwd") or c["folder"]) if own else c["folder"], c["agent"]
            elif "agent" in body:
                folder, name = schedule_target({"agent": body["agent"], "folder": "-"})
            else:
                folder, name = c["folder"], c.get("agent", "")
            prompt = str(body["prompt"]).strip() if "prompt" in body else c["prompt"]
            if not prompt:
                raise ValueError("prompt is required: what the agent should do when it runs")
            cron, at, next_run = schedule_when(body, now) if "cron" in body or "at" in body else (c["cron"], c.get("at"), c.get("next_run"))
        except ValueError as err:
            return 400, {"error": str(err)}
        c.update({"folder": folder, "agent": name, "prompt": prompt[:8000], "cron": cron, "at": at,
                  "start": body["start"] is not False if "start" in body else c["start"], "updated": now})
        if c["status"] in ("active", "pending"):
            c["next_run"] = next_run
        schedule_told(c, f"Your user changed schedule {cid}: now {when_text(c)}, for {c['agent'] or tilde_path(folder)}.")
        save_schedules()
    log("schedule", cid, "edited:", c["agent"] or tilde_path(folder), when_text(c))
    return 200, {"schedule": sched_view(c)}


def enable_schedule(cid):
    """You turn a cancelled, denied or expired schedule back on; it runs at its next time, needing no approval."""
    now = time.time()
    with lock:
        c = schedules.get(cid)
        if c is None:
            return 404, {"error": f"no schedule {cid}"}
        if c["status"] not in ("cancelled", "denied", "expired"):
            return 409, {"error": f"{cid} is {c['status']}"}
        next_run = cron_next(c["cron"], now) if c["cron"] else c.get("at")
        if not next_run or next_run <= now:
            return 409, {"error": f"{cid} was for {when_text(c)}, which has passed: add a new one"}
        c["status"], c["updated"], c["next_run"] = "active", now, next_run
        schedule_told(c, f"Your user turned schedule {cid} ({when_text(c)}) back on.")
        save_schedules()
    log("schedule", cid, "enabled again")
    return 200, {"schedule": dict(c)}


starting = {}  # folder -> when the daemon last started an agent there for a schedule
name_claims = {}  # folder -> names the next agents to register there take: ones started for a schedule


def schedule_agent(c):
    """The agent a schedule is for, wherever it runs; None when it isn't running."""
    return agent_named(c.get("agent")) if c.get("agent") else None


def follow_agent(rec):
    """A schedule for this agent starts it, when it isn't running, in the folder it's in now, not the one it was in
    when the schedule was made. Called with the lock held."""
    name, cwd = display_name(rec).lower(), rec.get("cwd") or ""
    moved = [c for c in schedules.values() if cwd and (c.get("agent") or "").lower() == name and c["folder"] != cwd]
    for c in moved:
        log("schedule", c["id"], "follows", c["agent"], "to", tilde_path(cwd))
        c["folder"] = cwd
    if moved:
        save_schedules()


def sched_view(c):
    """A schedule as the web UI shows it, with who'd get it if it ran now."""
    with lock:
        now = schedule_agent(c) or (None if c.get("agent") else successor(c["folder"]))
    return {**c, "when_text": when_text(c), "goes_to": display_name(now) if now else "", "running": bool(schedule_agent(c))}


def fire_schedule(c, now):
    """Send the prompt to the folder's agent, or hold it for one; the next run is set. Returns c when an agent
    has to be started for it. Called with the lock held."""
    msg = {"id": f"m{next_msg[0]}", "ts": now, "from": "peerlink", "from_name": f"schedule {c['id']}", "from_host": HOSTNAME,
           "from_cwd": "", "to_cwd": c["folder"], "from_space": "", "to_space": "",
           "text": f"Scheduled ({when_text(c)}; cancel with the schedule tool, id {c['id']}): {c['prompt']}",
           "expects_reply": False, "reply_to": None, "hops": 0, "status": "queued", "v": 2}
    next_msg[0] += 1
    own = schedule_agent(c)
    if c.get("agent"):
        # No folder on it: a gone session's messages go to its folder's other agents, and this one is for this agent only.
        msg["to_cwd"] = ""
    if own:
        c["folder"] = own.get("cwd") or c["folder"]  # where to start it next time it isn't running
        msg["to"], msg["to_name"] = own["id"], display_name(own)
        inboxes.setdefault(own["id"], []).append(msg)
        record(msg)
    elif c.get("agent"):
        # Not running: it waits for that agent (an hour, as any queued message), never another in its folder.
        msg["to"], msg["to_name"] = f"agent:{c['agent'].lower()}", c["agent"]
        inboxes.setdefault(msg["to"], []).append(msg)
        record(msg)
    agent = own or (None if c.get("agent") else queue_for_folder(c["folder"], msg))
    c["last_run"], c["runs"], c["updated"] = now, c.get("runs", 0) + 1, now
    try:
        c["next_run"] = cron_next(c["cron"], now) if c["cron"] else None
    except ValueError:
        c["next_run"] = None
    if not c["cron"]:
        c["status"] = "done"
    c["note"] = f"sent to {display_name(agent)}" if agent else f"{c['agent']} wasn't running" if c.get("agent") else "no agent was running there"
    save_schedules()
    log("schedule", c["id"], "ran:", c["note"])
    # One start per folder at a time: a second run while the first agent is still starting waits for it.
    if agent or not c["start"] or now - starting.get(c["folder"], 0) < 120:
        return None
    starting[c["folder"]] = now
    claims = name_claims.setdefault(c["folder"], [])
    if c.get("agent") and c["agent"] not in claims:
        claims.append(c["agent"])
    return c


def start_for_schedule(c):
    how, err = launch({"dir": c["folder"], "id": c["id"]})
    with lock:
        who = c.get("agent") or "an agent"
        c["note"] = f"couldn't start {who}: {err}" if err else f"{who} is {how}" if how.startswith("still") else f"started {who} {how}"
        if err:
            if c.get("agent") and c["agent"] in name_claims.get(c["folder"], []):
                name_claims[c["folder"]].remove(c["agent"])
            # Said once: the same failure on every run is noise until it changes or a start goes through.
            if err != c.get("start_err"):
                schedule_told(c, f"Schedule {c['id']} ran, but no agent was running in {tilde_path(c['folder'])} and one couldn't start: {err}. The prompt waits there for an hour.")
        c["start_err"] = err
        save_schedules()
    log("schedule", c["id"], c["note"])


# The account's usage limit: an agent whose turn died on it says so, and until it lifts nothing new is put to agents
# (their plugins hold what arrives) and no schedule runs. It lifts at the reset time Claude gave, when any agent's
# turn goes through again (a swapped account), or when you press Resume.
usage_limit = {}
LIMIT_FALLBACK_S = 5 * 3600  # how long a limit with no reset time holds before agents try again
# Once it lifts, agents holding work go back to it one at a time, LIFT_STAGGER_S apart: all of them at once is what
# drained the next window. lifting: {"at": when it lifted, "next": the next free start, "slots": agent id -> its start}.
lifting = {}
LIFT_STAGGER_S = 60


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
        lifting.clear()
        usage_limit.update({"since": usage_limit.get("since", now), "resets_at": resets or usage_limit.get("resets_at"),
                            "kind": str(body.get("kind") or usage_limit.get("kind") or ""), "by": display_name(rec)})
        chans = [r for r in routes_for(rec.get("cwd", ""), display_name(rec)) if r["channel_id"] != "dm"] or discord.get("routes", [])
    log("limit", "hit by", display_name(rec), "resets", time.strftime("%H:%M", time.localtime(resets)) if resets else "unknown",
        f"({len(back)} message{'s' if len(back) != 1 else ''} put back)")
    if fresh and chans and chat_ready(chans[0]["channel_id"]):
        when = f"resets {time.strftime('%a %H:%M', time.localtime(resets))}" if resets else "no reset time given"
        try:
            sent = chat_post(chans[0]["channel_id"],
                             f"⏸️ **Usage limit reached** ({when}; hit by {display_name(rec)}). Agents hold their messages and schedules wait. "
                             "Press Resume after switching accounts; it also lifts by itself at the reset, or when any agent's turn goes through.",
                             None, choices(["Resume now"], "pr"))
            with lock:
                usage_limit["discord_out"] = {"channel_id": chans[0]["channel_id"], "message_id": sent}
        except ChatError as err:
            log("discord", "limit post failed:", err)
    return 200, {"limit": limit_view()}


def lift_limit(why):
    with lock:
        if not usage_limit:
            return 200, {"limit": None}
        was = dict(usage_limit)
        usage_limit.clear()
        now = time.time()
        lifting.update({"at": now, "next": now, "slots": {}})
    log("limit", "lifted:", why)
    close_buttons(was)
    out = was.get("discord_out") or {}
    if out.get("channel_id") and chat_ready(out["channel_id"]):
        threading.Thread(target=lambda: chat_post(out["channel_id"], f"▶️ Usage limit lifted ({why}): agents carry on with what was held, one a minute.", out.get("message_id")),
                         daemon=True).start()
    return 200, {"limit": None}


def limit_view():
    return {k: v for k, v in usage_limit.items() if k != "discord_out"} or None


def limit_for(sid):
    """The limit as this agent's plugin holds to it: the account's while it holds, then, while agents are let go one
    at a time, its own until its turn comes. An agent holding nothing is let go at once. Called with the lock held."""
    if usage_limit or not lifting:
        return limit_view()
    now = time.time()
    slot = lifting["slots"].get(sid)
    if slot is None:
        if not (handed.get(sid) or inboxes.get(sid)):
            return None
        slot = lifting["slots"][sid] = max(lifting["next"], now)
        lifting["next"] = slot + LIFT_STAGGER_S
    return None if now >= slot else {"since": lifting["at"], "resets_at": slot, "kind": "staggered", "by": "peerlink"}


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
        if not pid:
            comm = ""
        elif os.name == "nt":
            comm = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=5).stdout.strip()
        else:
            comm = subprocess.run(["ps", "-p", str(int(pid)), "-o", "comm="], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        comm = ""
    if not re.search(r"claude|node", comm, re.I):
        log("terminate", sid, "still running, and its process can't be found to end it", repr(comm))
        return
    try:
        os.kill(int(pid), signal.SIGTERM)  # on Windows, this ends it outright
        if os.name != "nt":
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


# State files stay bounded: tidy() runs at startup and twice a day. What's ended and a month old goes; the newest of each
# kind always stays, so ids keep counting up. A log that's grown too big is rewritten, its old self kept as <name>.1.
KEEP_ENDED_S = 30 * 86400
LOG_MAX_BYTES = 5 << 20
TIDY_EVERY_S = 12 * 3600
last_tidy = [0.0]


def too_big(path, limit=LOG_MAX_BYTES):
    try:
        return os.path.getsize(path) > limit
    except OSError:
        return False


def rewrite_log(path, entries):
    """Keep `path` as path.1 and start it again with `entries`, one per line. Called with the lock held."""
    with open(path + ".tmp", "w") as f:
        f.writelines(json.dumps(e) + "\n" for e in entries)
    os.replace(path, path + ".1")
    os.replace(path + ".tmp", path)


def ended_and_old(items, live, now):
    """Ids of records whose status isn't one of `live` and that last changed a month ago, never the newest one."""
    newest = max(items, key=lambda i: int(i[1:]) if i[1:].isdigit() else 0, default=None)
    return [i for i, x in items.items() if i != newest and x.get("status") not in live
            and now - (x.get("updated") or x.get("created") or x.get("ts") or now) > KEEP_ENDED_S]


def tidy():
    now = time.time()
    last_tidy[0] = now
    with lock:
        dropped = {}
        for name, items, live, save in (("tasks", tasks, TASK_OPEN, save_tasks), ("spawns", spawns, ("pending",), save_spawns),
                                        ("schedules", schedules, ("active", "pending"), save_schedules)):
            old = ended_and_old(items, live, now)
            for i in old:
                items.pop(i)
            if old:
                save()
                dropped[name] = len(old)
        if too_big(MESSAGES_PATH):
            # What load_messages() would read back, one line per message with its latest state.
            by_id = {}
            with open(MESSAGES_PATH) as f:
                for line in f.readlines()[-5000:]:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if "text" in entry:
                        by_id[entry["id"]] = entry
                    elif entry.get("id") in by_id:
                        by_id[entry["id"]].update(entry)
            rewrite_log(MESSAGES_PATH, by_id.values())
            dropped["messages.jsonl"] = "rewritten"
        if too_big(ISSUES_PATH, 1 << 20):
            rewrite_log(ISSUES_PATH, feedback.values())
            dropped["issues.jsonl"] = "rewritten"
        if too_big(TERMINATIONS_PATH, 1 << 20):
            with open(TERMINATIONS_PATH) as f:
                rewrite_log(TERMINATIONS_PATH, [json.loads(line) for line in f.readlines()[-1000:] if line.strip()])
            dropped["terminations.jsonl"] = "rewritten"
        shown = {m["id"] for m in messages}
        for mid in [m for m in reactions if m not in shown]:
            reactions.pop(mid)
    # The daemon's own output: started in the background, its stdout is daemon.log, which moves aside for a new one.
    try:
        if too_big(LOG_PATH) and os.fstat(1).st_ino == os.stat(LOG_PATH).st_ino:
            os.replace(LOG_PATH, LOG_PATH + ".1")
            out = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.dup2(out, 1)
            os.dup2(out, 2)
            os.close(out)
            dropped["daemon.log"] = "rotated"
    except OSError:
        pass
    try:
        for d in os.listdir(DISCORD_FILES):
            path = os.path.join(DISCORD_FILES, d)
            if now - os.path.getmtime(path) > KEEP_ENDED_S:
                shutil.rmtree(path, ignore_errors=True)
                dropped["discord files"] = dropped.get("discord files", 0) + 1
    except OSError:
        pass
    if dropped:
        log("tidy:", ", ".join(f"{k} {v}" for k, v in dropped.items()))


def reaper():
    while True:
        time.sleep(10)
        try:
            reap()
        except Exception as err:  # one bad pass must not stop reaping, schedules and the limit for good
            log("reaper failed:", repr(err))


def reap():
    now = time.time()
    if now - last_tidy[0] > TIDY_EVERY_S:
        tidy()
    with lock:
        if lifting and now > lifting["next"] + 10 * LIFT_STAGGER_S:
            lifting.clear()  # everyone holding work has had its turn
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
                discord["routes"] = [clean_route(r) for r in b["routes"] if r.get("channel_id")]
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
            # Telegram's chats come as one more group, the ones its bot has heard from.
            tg = discord["telegram"]
            chats = ([{"id": "telegram", "name": "Telegram", "channels": sorted(({"id": k, "name": v} for k, v in tg["chats"].items()), key=lambda c: c["name"].lower())}]
                     if tg.get("token") else [])
            if not discord.get("token"):
                return self.reply(200, {"guilds": chats})
            # ↻ Refresh asks Discord itself (fresh=1), and what it says replaces the gateway's view.
            live_view = bot.channels_live() if bot and "fresh" not in q else None
            try:
                if live_view is None:
                    live_view = discordlink.channels_rest(discord["token"])
                    if bot:
                        bot.set_channels(live_view)
                return self.reply(200, {"guilds": live_view + chats})
            except discordlink.DiscordError as err:
                return self.reply(502, {"error": str(err), "guilds": chats})
        if rest == ["test"] and method == "POST":
            channel = str(self.body().get("channel_id") or "")
            try:
                chat_post(channel, "👋 peerlink is connected. Messages here go to this channel's first receiving agent; start with an agent's name and a colon to reach another one that receives here.")
            except ChatError as err:
                return self.reply(502, {"error": str(err)})
            return self.reply(200, {"ok": True})
        if rest == ["reconnect"] and method == "POST":
            start_discord()
            return self.reply(200, {"ok": True})
        return self.reply(404, {"error": "not found"})

    def telegram_api(self, rest, method):
        tg, bot = discord["telegram"], telegram_bot[0]
        if rest == [] and method == "GET":
            return self.reply(200, {
                "configured": bool(tg.get("token")), "token_hint": telegramlink.hint(tg.get("token", "")), "user": tg.get("user", ""),
                "enabled": tg.get("enabled", True), "state": bot.state if bot else ("off" if tg.get("token") else "unconfigured"),
                "error": bot.error if bot else "", "reads_messages": bool(bot and bot.reads_messages),
                "allowed_users": tg.get("allowed_users", []), "strangers": tg.get("strangers", []), "chats": tg.get("chats", {}),
            })
        if rest == [] and method == "PUT":
            b = self.body()
            restart = False
            with lock:
                if "token" in b:
                    try:
                        info = telegramlink.inspect_token(b["token"])
                    except telegramlink.TelegramError as err:
                        return self.reply(400, {"error": str(err)})
                    tg.update({"token": b["token"].strip(), "user": info["user"], "enabled": True})
                    restart = True
                if "enabled" in b:
                    tg["enabled"], restart = bool(b["enabled"]), True
                if "allowed_users" in b:
                    raw = b["allowed_users"]
                    tg["allowed_users"] = list(dict.fromkeys(re.findall(r"\d{3,20}", raw if isinstance(raw, str) else " ".join(map(str, raw or [])))))
                if b.get("allow"):
                    # Pairing: someone who wrote to the bot, allowed from the list of who tried.
                    who = str(b["allow"])
                    tg["allowed_users"] = list(dict.fromkeys(tg["allowed_users"] + [who]))
                    tg["strangers"] = [s for s in tg["strangers"] if s["id"] != who]
                if b.get("dismiss"):
                    tg["strangers"] = [s for s in tg["strangers"] if s["id"] != str(b["dismiss"])]
                if bot:
                    bot.allowed_users = set(tg["allowed_users"])
                save_discord()
            if restart:
                start_telegram()
            return self.reply(200, {"ok": True})
        if rest == [] and method == "DELETE":
            with lock:
                tg.update({"token": "", "user": ""})
                save_discord()
            start_telegram()
            return self.reply(200, {"ok": True})
        return self.reply(404, {"error": "not found"})

    def reply(self, code, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else body.encode() if isinstance(body, str) else json.dumps(body).encode()
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
        # The icons an installed web UI (Dock, home screen, Chrome's Install) shows: fetched without the sign-in cookie.
        if method == "GET" and url.path == "/manifest.webmanifest":
            return self.reply(200, MANIFEST, "application/manifest+json")
        if method == "GET" and url.path in ("/apple-touch-icon.png", "/icon-512.png"):
            with open(os.path.join(_here, "icons", url.path.lstrip("/")), "rb") as f:
                return self.reply(200, f.read(), "image/png", {"Cache-Control": "max-age=86400"})

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
                return self.reply(200, {**config, "default_guidance": default_guidance()})
            if method in ("PUT", "POST"):
                checked = {k: config_value(k, v) for k, v in self.body().items() if k in DEFAULT_CONFIG}
                # Only you, in the browser: no agent lets itself start agents without asking.
                if (not browser or self.key_ok()) and any(k.startswith("auto_approve") and checked[k][0] != config.get(k) for k in checked):
                    return self.reply(403, {"error": "auto-approve is set only from the web UI"})
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
                fresh = sid not in sessions and not b.get("succeeds")
                rec = sessions.get(sid, {"id": sid, "started": time.time()})
                # One a spawn started, in a folder that may already have its agent: it takes the name and role it
                # was asked for, never the folder's, which belong to the agent already there.
                spawn = spawn_of({"cwd": b.get("cwd") or "", "pid": b.get("pid")}, fresh) if sid not in sessions else None
                if spawn:
                    spawn["agent_id"] = sid
                    save_spawns()
                    if spawn.get("role") and not config["sessions"].get(sid, {}).get("role"):
                        config["sessions"].setdefault(sid, {})["role"] = spawn["role"]
                    # It joins the space it was asked for, even in a folder that's in another one.
                    if spawn.get("space"):
                        config["sessions"].setdefault(sid, {})["started_in"] = spawn["space"]
                    save_config()
                if spawn and spawn.get("name") and not rec.get("name"):
                    rec["name"] = unique_name(spawn["name"], sid)
                if b.get("name") and not rec.get("name"):
                    # A folder keeps the name it was given: a new session there (a restart, a resume) is still "vault".
                    # One registering again after a daemon restart keeps the name it had, not whichever is free first.
                    given = b.get("had_name") or (config.get("folder_names") or {}).get(b.get("cwd") or "")
                    # One the daemon started for a schedule comes up as the agent the schedule is for.
                    claims = name_claims.get(b.get("cwd") or "")
                    if claims and not b.get("succeeds"):
                        given = claims.pop(0)
                    rec["name"] = unique_name(given or b["name"], sid)
                if b.get("summary") is not None and b["summary"] != rec.get("summary"):
                    # The same summary its folder had before (a daemon restart, a resume) keeps its age.
                    known = dir_summaries.get(b.get("cwd") or rec.get("cwd") or "") or {}
                    rec["summary_ts"] = known["ts"] if known.get("summary") == b["summary"] and known.get("ts") else time.time()
                for k in ("cwd", "repo", "branch", "summary", "model", "plugin", "pid"):
                    if b.get(k) is not None:
                        rec[k] = b[k]
                # A plugin just reloaded comes back with no summary: the one this agent last had in its folder stays.
                known = dir_summaries.get(rec.get("cwd") or "") or {}
                if not rec.get("summary") and known.get("summary") and known.get("name") == display_name(rec):
                    rec.update(summary=known["summary"], headline=known.get("headline", ""), summary_ts=known.get("ts") or time.time())
                # A folder in no space yet (none set, no .peerlink) takes the one its first started agent was asked for.
                # A space set for one agent, as older versions allowed, becomes its folder's.
                old_override = clean_space(config["sessions"].get(sid, {}).pop("space", ""))
                want = (spawn or {}).get("space") or old_override
                if want and rec.get("cwd") and space_of({"cwd": rec["cwd"]})[1] == "default":
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
                # A nickname you gave this agent is its name: another live one that took it first (registering
                # sooner after a daemon restart) moves aside.
                nick = session_cfg(sid)["nickname"]
                for other in [r for r in sessions.values() if nick and r["id"] != sid and is_live(r)
                              and not session_cfg(r["id"])["nickname"] and display_name(r).lower() == nick.lower()]:
                    other["name"] = unique_name(other["name"], other["id"])
                    log("agent", other["id"], "renamed", other["name"], "for", sid)
                adopt(rec, old, spawn)
                follow_agent(rec)
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
                # When it queued /reload-plugins for a newer peerlink that hasn't run yet; 0 once none is waiting.
                if isinstance(b.get("reload_queued"), (int, float)):
                    rec["reload_queued"] = b["reload_queued"] / 1000 if b["reload_queued"] else None
                note_lone_leads()
                if b.get("summary") is not None:
                    if b["summary"][:500] != rec.get("summary"):
                        rec["summary_ts"] = time.time()
                    rec["summary"] = b["summary"][:500]
                    # A few words for lists, from the summary model; a summary set by hand has none and the web UI cuts one.
                    rec["headline"] = str(b.get("headline") or "")[:80]
                    if b.get("summary_kind") in ("model", "manual"):
                        remember_dir(rec)
                cfg = client_config(sid)
            ack(sid, b.get("ack"))
            inbox = [] if cfg["muted"] else take_inbox(sid, track="ack" in b)
            with lock:
                mine = reach(rec)
            others = sum(1 for p in local_peers() if p["id"] != sid and peer_reach(p) & mine) + len(config.get("remotes", []))
            with lock:
                held = limit_for(sid)
            out = {"name": display_name(rec), "config": cfg, "inbox": inbox, "peer_count": others, "limit": held}
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
                rec = sessions.get(sid)
                if "auto_approve" in b and rec and rec.get("cwd"):
                    if not browser or self.key_ok():
                        return self.reply(403, {"error": "auto-approve is set only from the web UI"})
                    folders = [f for f in config.get("auto_approve_folders") or [] if f != rec["cwd"]]
                    config["auto_approve_folders"] = folders + [rec["cwd"]] if b["auto_approve"] else folders
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

        if len(rest) == 3 and rest[0] == "sessions" and rest[2] == "role" and method == "POST":
            b = self.body()
            return self.reply(*set_member_role(rest[1], b.get("agent"), b.get("role")))

        if len(rest) == 3 and rest[0] == "sessions" and rest[2] == "rename" and method == "POST":
            return self.reply(*rename_self(rest[1], self.body().get("name")))

        if len(rest) == 4 and rest[0] == "sessions" and rest[2] == "threads" and rest[3] == "close" and method == "POST":
            return self.reply(*close_threads(rest[1], self.body().get("ids") or []))

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
                return self.reply(200, {"schedules": [sched_view(c) for c in sorted(schedules.values(), key=lambda c: -c["created"])]})
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
        # Only you edit a schedule, from the web UI: an agent cancels its own and asks for a new one.
        if len(rest) == 2 and rest[0] == "schedules" and method == "POST":
            if not browser:
                return self.reply(403, {"error": "only your user edits a schedule, from the web UI; cancel it and ask for a new one"})
            return self.reply(*edit_schedule(rest[1], self.body()))
        # Only you turn a schedule back on: an agent asks for a new one.
        if len(rest) == 3 and rest[0] == "schedules" and rest[2] == "enable" and method == "POST":
            if not browser:
                return self.reply(403, {"error": "only your user turns a schedule back on, from the web UI; ask for a new one with schedule"})
            return self.reply(*enable_schedule(rest[1]))
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

        if rest == ["conversation"] and method == "GET":
            return self.reply(200, {"messages": conversation(q.get("a", [""])[0], q.get("b", [""])[0])})
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
        if rest and rest[0] == "telegram":
            # Who may talk to your agents is yours to say: changes come from your browser, never an agent holding the key.
            if method != "GET" and (not browser or self.key_ok()):
                return self.reply(403, {"error": "Telegram is set up from the web UI"})
            return self.telegram_api(rest[1:], method)

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


# How a process outlives the one that starts it: its own session on macOS and Linux, its own process group and no
# console on Windows, where closing the terminal would end it otherwise.
DETACHED = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
            else {"start_new_session": True})


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
        subprocess.Popen([best_python(), "-X", "utf8", os.path.abspath(__file__), "serve"], stdin=devnull, stdout=out, stderr=out,
                         close_fds=True, **DETACHED)
    os.close(out)
    for _ in range(50):
        time.sleep(0.1)
        if health():
            break
    print(json.dumps({"running": bool(health()), "started": True}))


def restart_self(server):
    """Hand the port to a fresh copy of this file (it may be a newer version)."""
    subprocess.Popen([sys.executable, "-X", "utf8", os.path.abspath(__file__), "start", "--after-restart"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **DETACHED)
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
    start_telegram()
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
        if os.name == "nt":
            return os.startfile(url)
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


MANIFEST = {"name": "Peerlink", "short_name": "Peerlink", "start_url": "/", "display": "standalone",
            "background_color": "#faf9f7", "theme_color": "#c96442",
            "icons": [{"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
                      {"src": "/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"}]}

UI_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Peerlink</title><link rel="apple-touch-icon" href="/apple-touch-icon.png"><link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%23c96442'/><path d='M7 11.5h17M19.5 7l4.5 4.5-4.5 4.5M25 20.5H8M12.5 16 8 20.5l4.5 4.5' fill='none' stroke='%23fff' stroke-width='2.8' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<style>
:root{color-scheme:light dark;--bg:#faf9f7;--side:#f2f0ec;--fg:#1f1e1c;--mut:#6b6862;--line:#e4e1db;--card:#fff;--acc:#c96442;--ok:#2f7d4f;--warn:#b7791f;--bad:#c0392b;--well:rgba(127,127,127,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#1c1b19;--side:#161513;--fg:#ecebe8;--mut:#9b978f;--line:#34322e;--card:#252421;--acc:#e0805e;--ok:#5cb57f;--warn:#e0b04a;--bad:#e5675a;--well:rgba(255,255,255,.03)}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 ui-sans-serif,system-ui,-apple-system,sans-serif}
.app{display:flex;min-height:100vh}
nav{flex:0 0 210px;background:var(--side);border-right:1px solid var(--line);padding:18px 10px;position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:2px}
nav .brand{font-weight:700;font-size:16px;padding:0 10px 4px;display:flex;align-items:center;gap:8px}nav .brand .mark{width:20px;height:20px;flex:none}nav .host{font-size:11px;color:var(--mut);padding:0 10px 14px}
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
.lrow{display:grid;grid-template-columns:minmax(150px,230px) minmax(0,1fr) auto;gap:4px 16px;align-items:center;width:100%;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:7px 12px;text-align:left;font:inherit;color:inherit;cursor:pointer}
.lrow:hover{border-color:var(--mut)}.lrow.is-lead{border-color:var(--acc)}.lrow.sel{box-shadow:0 0 0 2px var(--acc)}.lrow.off{opacity:.5;cursor:default;border-style:dashed}
.lrow .ln{font-weight:600;display:flex;align-items:center;gap:6px;min-width:0}.lrow .ln>span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lrow .lid{display:flex;flex-direction:column;gap:1px;min-width:0}.lrow .lwhere{font-size:11px;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-left:14px}
.lrow .lw{font-size:13px;color:var(--fg);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lrow .lbusy{color:var(--warn)}.pill.acc{color:var(--acc);border-color:var(--acc)}
.panel .phead{display:flex;align-items:center;gap:10px;min-width:0}.panel .phead .plogo{flex:none;width:36px;height:36px;border-radius:10px;display:grid;place-items:center}
.panel .phead .ptitle{display:flex;flex-direction:column;min-width:0}.panel .phead .psub{font-size:12px;color:var(--mut);display:flex;align-items:center;gap:5px;flex-wrap:wrap}.panel .phead .psub .dot{margin:0}
.panel .pwhere{display:flex;align-items:center;gap:6px;margin-top:8px;font-size:12px;color:var(--mut);min-width:0}.panel .pwhere .mono{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.panel .pnow{margin-top:10px;background:var(--well);border:1px solid var(--line);border-radius:8px;padding:8px 10px;line-height:1.45}.panel .pnow .k{margin-bottom:2px}
.panel .ptiles{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:10px}.panel .ptiles>div{border:1px solid var(--line);border-radius:8px;padding:6px 10px;display:flex;flex-direction:column;gap:3px}
.panel .ptiles b{font-size:16px}.panel .ptiles .cmeter{width:100%}
.cmeter{display:inline-block;width:34px;height:5px;border-radius:3px;background:var(--line);overflow:hidden;vertical-align:middle}.cmeter i{display:block;height:100%;background:var(--ok)}.cmeter.mid i{background:var(--warn)}.cmeter.hi i{background:var(--bad)}
.lrow .lr{display:flex;gap:10px;align-items:center;white-space:nowrap;font-size:12px;justify-content:flex-end}
.lrow .badge{font-size:10px;font-weight:600;color:var(--acc);border:1px solid var(--acc);border-radius:4px;padding:0 4px}.lrow .badge.only{border-style:dashed}
@media (max-width:720px){.lrow{grid-template-columns:1fr auto}.lrow .lw{grid-column:1/-1;grid-row:2;white-space:normal}}
section{display:none}section.on{display:block}
h1{font-size:20px;margin:0 0 4px}.lead{color:var(--mut);margin:0 0 18px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:22px 0 10px}
.sub{margin-left:12px;padding-left:16px;border-left:2px solid var(--line)}.sub h2::before{content:"↳ ";color:var(--acc)}
.card.is-lead{border-color:var(--acc)}
.chart{overflow-x:auto;padding:4px 0 12px;margin-bottom:8px}
.chart+.chart{margin-top:6px}.sgrid{display:flex;flex-wrap:wrap;gap:20px;align-items:flex-start}.sgrid .chart+.chart{margin-top:0}
.unit{display:inline-flex;flex-direction:column;align-items:center;border:1px dashed var(--line);border-radius:12px;padding:10px 12px 12px;min-width:0}
.unit-h{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin-bottom:10px;white-space:nowrap}
.orow{display:flex;gap:12px;align-items:flex-start;justify-content:safe center}
.ocol{display:flex;flex-direction:column;align-items:center}.orow.stack{flex-direction:column;gap:8px}
.down{width:2px;height:14px;background:var(--acc);flex:none}
.node{width:200px;height:136px;box-sizing:border-box;display:flex;flex-direction:column;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px;cursor:pointer;text-align:left;font:inherit;color:inherit;overflow:hidden}
.node .state{margin-top:auto!important}
.node:hover{border-color:var(--mut)}.node.is-lead{border-color:var(--acc)}.node.sel{box-shadow:0 0 0 2px var(--acc)}
.node .nm{font-weight:600;display:flex;align-items:center;gap:4px 6px;justify-content:space-between;flex-wrap:wrap}.node .nm>span:first-child{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.node .tag{font-size:11px;font-weight:500;color:var(--acc);white-space:nowrap}
.node .badge{display:inline-block;font-size:10px;font-weight:600;color:var(--acc);border:1px solid var(--acc);border-radius:4px;padding:0 4px;white-space:nowrap}
.node .nm .rt{display:inline-flex;align-items:center;gap:4px;flex-wrap:wrap}.node .badge.only{border-style:dashed}
.lrow .badge.guest,.node .badge.guest{color:var(--mut);border-color:var(--mut);font-weight:500}
.node.off{opacity:.5;cursor:default;border-style:dashed}.node.off:hover{border-color:var(--acc)}
.under{margin-left:14px;border-left:2px solid var(--acc);padding-left:12px;margin-top:8px}
.node .dir{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--mut);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
details.plat{margin:0 0 12px;max-width:860px;background:var(--card);border:1px solid var(--line);border-radius:12px}
#schedlist details.plat,#inboxlist details.plat,#fblist details.plat{max-width:none;margin:0}#schedadd{max-width:none}.grpbody{display:flex;flex-direction:column;gap:8px;padding-top:12px!important}
.plat.static .psum{display:flex;align-items:center;gap:12px;padding:12px 14px;font-size:14px}.plat .pact{flex:none;display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
div.plat{margin:0 0 12px;max-width:860px;background:var(--card);border:1px solid var(--line);border-radius:12px}
#cfg details.plat .pbody{padding-top:12px;display:flex;flex-direction:column;gap:10px}#cfg .savebar{max-width:860px}
@media (max-width:640px){.plat.static .psum{flex-wrap:wrap}.plat .pact{width:100%;justify-content:flex-start}}
details.plat>summary{display:flex;align-items:center;gap:12px;padding:12px 14px;list-style:none;font-size:14px;color:var(--fg)}details.plat>summary::-webkit-details-marker{display:none}
.plat .plogo{flex:none;width:34px;height:34px;border-radius:9px;display:grid;place-items:center}.plat .plogo svg{width:20px;height:20px}
.plat .ptitle{display:flex;flex-direction:column;min-width:0;flex:1}.plat .psub{font-size:12px;color:var(--mut)}
details.plat .pstate{display:flex;align-items:center;gap:6px;font-size:13px;flex-wrap:wrap;justify-content:flex-end}details.plat .pstate .dstate{margin:0}
details.plat .chev{flex:none;width:8px;height:8px;border-right:2px solid var(--mut);border-bottom:2px solid var(--mut);transform:rotate(-45deg);transition:transform .15s;margin:0 4px}
details.plat[open] .chev{transform:rotate(45deg)}details.plat .pbody{padding:0 14px 14px;border-top:1px solid var(--line)}details.plat .pbody .steps .step{background:var(--bg)}
.mlogo{flex:none;width:22px;height:22px;border-radius:6px;display:grid;place-items:center}.mlogo svg{width:14px;height:14px}
details.conv{max-width:none;margin:0}details.conv .cnames{display:flex;align-items:center;gap:6px;flex-wrap:wrap}details.conv .psub{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
details.conv .pstate{flex-wrap:nowrap}.convbody{display:flex;flex-direction:column;gap:8px;padding-top:12px!important;max-height:70vh;overflow:auto}
.cline{border-left:3px solid var(--acc);padding:4px 0 4px 10px}.cline.other{border-left-color:var(--line)}.cline .mh{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;font-size:13px}
.cline .mh .when{margin-left:auto;color:var(--mut);font-size:12px}.cline .mt{margin-top:4px;overflow-wrap:anywhere;max-height:7.4em;overflow:hidden;cursor:pointer;-webkit-mask-image:linear-gradient(#000 70%,transparent);mask-image:linear-gradient(#000 70%,transparent)}.cline.open .mt{max-height:none;cursor:auto;-webkit-mask-image:none;mask-image:none}
.md{overflow-wrap:anywhere;line-height:1.45}.md p{margin:0 0 6px}.md>:last-child{margin-bottom:0}.md ul,.md ol{margin:0 0 6px;padding-left:20px}.md li{margin:1px 0}
.md code{font-family:ui-monospace,Menlo,monospace;font-size:.88em;background:var(--well);border:1px solid var(--line);border-radius:4px;padding:0 4px}
.md pre{margin:0 0 6px;background:var(--well);border:1px solid var(--line);border-radius:6px;padding:8px 10px;overflow:auto;max-height:20em}.md pre code{border:0;padding:0;background:none}
.md blockquote{margin:0 0 6px;padding-left:10px;border-left:3px solid var(--line);color:var(--mut)}.md .mdh{font-weight:600}.md a{color:var(--acc)}.cline .chip{font-size:11px;border-radius:999px;padding:1px 8px;border:1px solid var(--line);color:var(--mut)}
details.plat .pbody h2{font-size:14px;margin:16px 0 6px}.pill.warn{color:var(--warn);border-color:var(--warn)}
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
.node .state{font-size:11px;color:var(--mut);margin-top:4px;display:flex;align-items:center;justify-content:space-between;gap:6px}.hot{color:var(--warn);font-weight:600}
.node .sum{font-size:12px;color:var(--mut);margin-top:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.sbox{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;width:240px;box-sizing:border-box;text-align:center;display:flex;flex-direction:column;min-height:150px}
.sbox .small.mut{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}.sbox .spick{margin-top:auto;padding-top:8px}
.sbox.led{border-color:var(--acc)}.spick{display:flex;flex-direction:column;gap:4px;margin-top:8px;text-align:left}.spick label{display:flex;justify-content:space-between;align-items:center;gap:6px;font-size:12px;color:var(--mut)}.spick select{font-size:12px;padding:2px 4px;width:160px}.sbox .small{margin-top:2px}
#detail:empty{display:none}
#detail{position:fixed;top:0;right:0;bottom:0;width:380px;max-width:100%;overflow-y:auto;background:var(--bg);border-left:1px solid var(--line);box-shadow:-8px 0 24px rgba(0,0,0,.08);padding:16px;z-index:10}
#detail .panel{border:0;padding:0;background:transparent}
.panel .top{display:flex;justify-content:space-between;align-items:center;gap:8px}.panel .name{font-size:16px}
.panel .x{padding:2px 8px;font-size:13px}.facts{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;margin:10px 0 4px;font-size:13px}
.facts .k{color:var(--mut)}.threads{margin-top:8px;display:flex;flex-direction:column;gap:4px}.threads .k{color:var(--mut)}.panel form{margin-top:12px;border-top:1px solid var(--line);padding-top:12px;display:flex;flex-direction:column;gap:12px}
.panel label{display:flex;flex-direction:column;gap:4px;font-size:13px;font-weight:500}.panel label .hint{font-weight:400;font-size:12px;color:var(--mut)}
.panel label.check{flex-direction:row;align-items:center;gap:8px}.ok{color:var(--ok)}
#stalebar .mono{white-space:nowrap}#stalebar{background:var(--card);border:1px solid var(--warn);border-radius:10px;padding:8px 14px;margin-bottom:14px;align-items:flex-start;gap:10px;max-width:1050px}
#stalebar details{flex:1;margin:0}#stalebar summary{font-size:13px;color:var(--fg)}#stalebar details .small{margin-top:6px}
#limitbar{background:var(--card);border:1px solid var(--warn);border-radius:10px;padding:10px 14px;margin-bottom:14px}
#offline{display:none;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:14px;color:var(--mut)}
#signedout{display:none;background:var(--card);border:1px solid var(--warn);border-radius:10px;padding:10px 14px;margin-bottom:14px}
.msgbar{display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap}.msgbar select{width:auto}
.mlist{display:flex;flex-direction:column;gap:8px}
.task{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.task .mh{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;font-size:13px}.task .mh .when{margin-left:auto;color:var(--mut);font-size:12px}
.task .note{margin-top:6px;overflow-wrap:anywhere;font-size:13px}.task .tact{margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}.task .tact select{width:auto}
.task.sub{margin-left:24px}button.danger{color:var(--bad);border-color:var(--bad)}nav .count.hot:not(:empty){background:var(--acc);color:#fff;border-radius:999px;padding:1px 7px;font-weight:600}.ititle{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}.task .ftext{margin-top:6px;overflow-wrap:anywhere;font-size:13px;max-height:16em;overflow:auto}
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
<div class="brand"><svg class="mark" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="7" fill="var(--acc)"/><path d="M7 11.5h17M19.5 7l4.5 4.5-4.5 4.5M25 20.5H8M12.5 16 8 20.5l4.5 4.5" fill="none" stroke="#fff" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"/></svg>Peerlink</div><div class="host" id="hostline"></div>
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
<li>The bar is how full the agent's context window is: it turns yellow past half and red past 80%. A yellow dot means it's working, a green one that it's idle. Messages an hour show only when an agent is chatty, 20 or more.</li></ul></details><div class="orgwrap"><div id="peers"></div><div id="detail"></div></div></section>

<section id="inbox"><h1>Inbox</h1><p class="lead">Everything waiting on you, the longest waiting first. On Discord, <span class="mono">/inbox</span> shows the same list, and you answer by number, like <span class="mono">1 done</span>.</p>
<div class="msgbar"><button class="ghost small" id="notifybtn" data-notify>Desktop alerts: off</button></div>
<div class="mlist" id="inboxlist"></div></section>

<section id="spaces"><h1>Spaces</h1>
<p class="lead">Which agents can see and message each other.</p>
<details class="how"><summary>How spaces work</summary><p>An agent is in the space you put its folder in, from its Details tab on the Agents page. A folder you haven't placed takes the space in the nearest <span class="mono">.peerlink</span> file at or above it, else it's <b>unassigned</b>. Here you pick each space's lead, which gets work sent to the space, and which space it sits inside: its lead then speaks for it there.</p></details>
<div id="spacelist"></div>
</section>

<section id="schedules"><h1>Schedules</h1><p class="lead">Prompts the daemon sends an agent at a time or on a repeat, even with no terminal open.</p>
<details class="how"><summary>How schedules work</summary><p>Prompts the daemon sends an agent at a time or on a repeat, with no terminal open. A schedule is for one agent: it goes to that agent wherever it runs, never to another in its folder. When it isn't running, the run waits for it, or the agent is started again in its folder. Agents add them with their <span class="mono">schedule</span> tool; one that may start an agent waits for your approval. Edit any of them here.</p></details>
<details class="plat" id="schedadd"><summary><span class="plogo" style="background:var(--acc)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 5v14M5 12h14"/></svg></span><span class="ptitle"><b>Add a schedule</b><span class="psub">A prompt for one agent, once or on a repeat</span></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody" style="padding-top:12px">
<form id="schedform"><div class="addline"><select name="target" class="grow" id="schedagents" required></select>
<input name="when" type="text" placeholder="0 9 * * 1-5, or in 2h" title="A cron (minute hour day month weekday) repeats; 'in 30m', 'in 2h', 'in 1d' or 2026-10-25 09:00 runs once" style="width:190px"></div>
<textarea name="prompt" rows="2" placeholder="What the agent should do when it runs" style="margin-top:8px;width:100%"></textarea>
<div class="addline" style="margin-top:8px"><label class="check small"><input type="checkbox" name="start" checked> Start it when it isn&#39;t running</label><button type="submit">Add schedule</button><span class="small" id="schedmsg"></span></div></form></div></details>
<div id="schedlist" style="margin-top:12px;display:flex;flex-direction:column;gap:8px"></div></section>

<section id="messages"><h1>Messages</h1><p class="lead">Conversations between agents, and with you on Discord and Telegram, the latest first. Open one for its whole history.</p>
<div class="msgbar"><span class="fl"><label class="small mut" for="msgspace">Space</label><select id="msgspace"><option value="">All spaces</option></select></span>
<span class="fl"><label class="small mut" for="msgagent">Agent</label><select id="msgagent"><option value="">Everyone</option></select></span>
<input id="msgq" class="search" type="search" placeholder="Search messages" aria-label="Search messages">
<label class="check small" style="margin:0"><input type="checkbox" id="msgnosched"> Hide schedule runs</label></div>
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
<div class="msgbar"><span class="fl"><label class="small mut" for="fstatus">Show</label><select id="fstatus"><option value="">All, by status</option><option value="new">New</option><option value="accepted">Accepted</option><option value="done">Done</option><option value="dismissed">Dismissed</option></select></span>
<span class="fl"><label class="small mut" for="ftype">Type</label><select id="ftype"><option value="">Bugs and ideas</option><option value="bug">Bugs</option><option value="idea">Ideas</option></select></span></div>
<div class="mlist" id="fblist"></div></section>

<section id="channels"><h1>Channels</h1>
<p class="lead">Talk to your agents from outside the terminal.</p>
<details class="plat" id="pdiscord"><summary><span class="plogo" style="background:#5865F2"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#fff" d="M20.317 4.37a19.79 19.79 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.865-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.74 19.74 0 0 0 3.677 4.37a.07.07 0 0 0-.032.028C.533 9.046-.32 13.58.099 18.058a.082.082 0 0 0 .031.056 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994a.076.076 0 0 0-.041-.106 13.1 13.1 0 0 1-1.872-.892.077.077 0 0 1-.008-.128c.126-.094.252-.192.372-.291a.074.074 0 0 1 .078-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.009c.12.099.246.198.373.292a.077.077 0 0 1-.007.128 12.3 12.3 0 0 1-1.873.891.077.077 0 0 0-.041.107c.36.698.772 1.363 1.225 1.993a.076.076 0 0 0 .084.029 19.84 19.84 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03zM8.02 15.331c-1.183 0-2.157-1.086-2.157-2.42 0-1.333.956-2.418 2.157-2.418 1.21 0 2.176 1.095 2.157 2.419 0 1.333-.956 2.419-2.157 2.419zm7.975 0c-1.183 0-2.157-1.086-2.157-2.42 0-1.333.955-2.418 2.157-2.418 1.21 0 2.176 1.095 2.157 2.419 0 1.333-.946 2.419-2.157 2.419z"/></svg></span><span class="ptitle"><b>Discord</b><span class="psub">Channels in your server</span></span><span class="pstate" id="dstatus"></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<p class="meta">Each channel you map has agents: ones that <b>receive</b> get your messages there, ones that <b>send</b> can post there. Only you, the bot's owner, can talk to them.</p>
<ol class="steps">
<li class="step" id="dstep1"><span class="step-n">1</span><div class="step-body"><div class="step-t">Create the bot and paste its token</div><div id="dstep1b"></div></div></li>
<li class="step" id="dstep2"><span class="step-n">2</span><div class="step-body"><div class="step-t">Invite it to your server</div><div id="dstep2b"></div></div></li>
<li class="step" id="dstep3"><span class="step-n">3</span><div class="step-body"><div class="step-t">Map channels to agents</div><p class="meta">Under <b>Chats and agents</b>, below.</p></div></li>
</ol></div></details>
<details class="plat" id="ptelegram"><summary><span class="plogo" style="background:#26A5E4"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#fff" d="M4.6 11.6l13.1-5.05c.6-.22 1.13.15.93 1.06l-2.23 10.5c-.17.75-.6.93-1.23.58l-3.4-2.5-1.64 1.58c-.18.18-.33.33-.68.33l.24-3.46 6.3-5.69c.27-.24-.06-.38-.43-.14l-7.78 4.9-3.35-1.05c-.73-.23-.74-.73.15-1.07z"/></svg></span><span class="ptitle"><b>Telegram</b><span class="psub">A private chat or a group</span></span><span class="pstate" id="tgstatus"></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<p class="meta">A private chat with your bot, or a group it's in, works like a Discord channel. Telegram bots answer anyone who finds them, so peerlink ignores everyone until you allow them.</p>
<ol class="steps">
<li class="step" id="tstep1"><span class="step-n">1</span><div class="step-body"><div class="step-t">Create the bot and paste its token</div><div id="tstep1b"></div></div></li>
<li class="step" id="tstep2"><span class="step-n">2</span><div class="step-body"><div class="step-t">Message it, then allow yourself</div><div id="tstep2b"></div></div></li>
<li class="step" id="tstep3"><span class="step-n">3</span><div class="step-body"><div class="step-t">Map chats to agents</div><div id="tstep3b"></div></div></li>
</ol></div></details>
<details class="plat" id="pmap"><summary><span class="plogo" style="background:var(--acc)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" d="M4 7h11m0 0-3-3m3 3-3 3M20 17H9m0 0 3-3m-3 3 3 3"/></svg></span><span class="ptitle"><b>Chats and agents</b><span class="psub">Which agents hear and post in each channel</span></span><span class="pstate" id="mapstatus"></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<div id="dstep3b"></div></div></details>
<details class="plat" id="popts"><summary><span class="plogo" style="background:var(--mut)"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3.2" fill="none" stroke="#fff" stroke-width="2"/><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M5.6 18.4l1.8-1.8M16.6 7.4l1.8-1.8"/></svg></span><span class="ptitle"><b>Options</b><span class="psub">Progress reactions, buttons, files and who can talk</span></span><span class="pstate" id="optstatus"></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<div id="dmore"></div></div></details>
</section>

<section id="settings"><h1>Settings</h1><p class="lead">Changes reach every agent within a few seconds.</p>
<form id="cfg">
<details class="plat" id="swake"><summary><span class="plogo" style="background:var(--acc)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg></span><span class="ptitle"><b>Waking and delivery</b><span class="psub">When a message interrupts an agent, and when it waits</span></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<div class="check"><input type="checkbox" name="auto_wake_idle" id="aw"><label for="aw" style="margin:0">Wake idle agents when a message arrives</label></div>
<div class="check"><input type="checkbox" name="inject_while_busy" id="ib"><label for="ib" style="margin:0">Deliver into a running turn</label></div>
<div class="row"><div><label for="wf">Wake an idle agent for</label><select name="wake_for" id="wf" style="width:100%"><option value="needed">Questions, tasks, your messages and answers (saves tokens)</option><option value="all">Every message</option></select></div>
<div><label for="wd">Wait before waking for another agent's message (s)</label><input type="number" name="wake_delay_s" id="wd" min="0" max="120"></div></div>
<div class="mut small">Each wake-up is a full turn that re-reads the agent's context. Waiting a few seconds lets messages that arrive together share one turn; your own messages never wait.</div>
</div></details>
<details class="plat" id="sctx"><summary><span class="plogo" style="background:#7a6ff0"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 3v4M12 17v4M4.9 4.9l2.8 2.8M16.3 16.3l2.8 2.8M3 12h4M17 12h4M4.9 19.1l2.8-2.8M16.3 7.7l2.8-2.8"/></svg></span><span class="ptitle"><b>Context and usage</b><span class="psub">Clearing context so it doesn't drain your usage limit</span></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<div class="check"><input type="checkbox" name="clear_between_tasks" id="cbt"><label for="cbt" style="margin:0">Agents clear their own context after each task, saving notes to memory first</label></div>
<div class="row"><div><label for="cnt">Ask an agent to save and clear once its context passes (tokens, 0 for never)</label><input type="number" name="clear_nudge_tokens" id="cnt" min="0" step="50000"></div><div></div></div>
<div class="mut small">A large context is re-read on every turn and drains the usage limit fast. The ask arrives with the agent's next prompt, so it clears at a stopping point.</div>
</div></details>
<details class="plat" id="sapprove"><summary><span class="plogo" style="background:var(--ok)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10zM9 12l2 2 4-4"/></svg></span><span class="ptitle"><b>Approvals</b><span class="psub">Whether agents ask you before starting agents and schedules</span></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<div class="check"><input type="checkbox" name="auto_approve_all" id="aaa"><label for="aaa" style="margin:0">Let every agent start agents and schedules without asking me</label></div>
<div class="mut small">You're still told on Discord when one does. To allow only some agents, leave this off and turn it on in an agent's Controls tab.</div>
</div></details>
<details class="plat" id="ssee"><summary><span class="plogo" style="background:#3d8fd6"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"/></svg></span><span class="ptitle"><b>What agents see</b><span class="psub">The live agent list, summaries and feedback</span></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<div class="check"><input type="checkbox" name="roster_in_prompt" id="rp"><label for="rp" style="margin:0">Show each agent the live agent list when it changes</label></div>
<div class="check"><input type="checkbox" name="auto_summary" id="as"><label for="as" style="margin:0">Keep agent summaries current automatically</label></div>
<div class="row"><div><label>Summary model</label><input type="text" name="summary_model"></div><div></div></div>
<div class="check"><input type="checkbox" name="feedback" id="fbk"><label for="fbk" style="margin:0">Agents report bugs and ideas about peerlink itself, on the Feedback page</label></div>
<div class="mut small">For people developing peerlink. Agents started after you change this get the report_issue tool, or don't.</div>
</div></details>
<details class="plat" id="sguide"><summary><span class="plogo" style="background:#c58a2b"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M8 13h8M8 17h5"/></svg></span><span class="ptitle"><b>Guidance</b><span class="psub">What every agent is told about working with its peers</span></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<textarea name="guidance" id="guidance" aria-label="Guidance for every agent"></textarea><div class="mut small">Leave empty to use the default, shown in grey.</div>
</div></details>
<details class="plat" id="slimits"><summary><span class="plogo" style="background:var(--mut)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2"/></svg></span><span class="ptitle"><b>Limits and timing</b><span class="psub">Polling, timeouts and how much agents can send</span></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<div class="row">
<div><label>Poll interval (ms)</label><input type="number" name="poll_interval_ms" min="500"></div>
<div><label>Agent timeout (s)</label><input type="number" name="peer_ttl_s" min="5"></div>
<div><label>Max reply hops</label><input type="number" name="max_hops" min="1"></div>
<div><label>Messages per minute per agent</label><input type="number" name="rate_limit_per_min" min="1"></div>
</div>
</div></details>
<details class="plat" id="sremote"><summary><span class="plogo" style="background:#4f6b7a"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M5 4h14v6H5zM5 14h14v6H5zM8 7h.01M8 17h.01"/></svg></span><span class="ptitle"><b>Other machines</b><span class="psub">Linking daemons on other computers</span></span><span class="chev" aria-hidden="true"></span></summary><div class="pbody">
<label>Their daemons, one URL per line</label><textarea name="remotes" style="min-height:60px"></textarea>
<div class="row"><div><label>Listen host (0.0.0.0 to accept other machines; needs a restart)</label><input type="text" name="listen_host"></div>
<div><label>Shared token</label><input type="text" name="token"></div></div>
</div></details>
<p class="savebar"><button type="submit">Save</button> <span class="mut small" id="saved"></span></p>
</form></section>

<section id="controls"><h1>Controls</h1><p class="lead">Sign-in, the key, and the daemon.</p>
<div class="plat static"><div class="psum"><span class="plogo" style="background:#3d8fd6"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M20 21a8 8 0 0 0-16 0M12 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10z"/></svg></span><span class="ptitle"><b>Browsers</b><span class="psub"><span id="browsers"></span></span></span><span class="pact"><button class="ghost" data-act="logout">Sign out here</button> <button class="ghost" data-act="logout-all">Sign out everywhere</button></span></div></div>
<div class="plat static"><div class="psum"><span class="plogo" style="background:#c58a2b"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M15.5 7.5a4.5 4.5 0 1 1-6.36 6.36A4.5 4.5 0 0 1 15.5 7.5zM15.5 7.5 21 2M18 5l2 2"/></svg></span><span class="ptitle"><b>Key</b><span class="psub">Agents and sign-in links use the key in <span class="mono" id="keypath"></span>. Rotating it signs out every browser.</span></span><span class="pact"><button class="ghost" data-act="rotate">Rotate key</button></span></div></div>
<div class="plat static"><div class="psum"><span class="plogo" style="background:var(--acc)"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 2v10M18.4 6.6a9 9 0 1 1-12.8 0"/></svg></span><span class="ptitle"><b>Daemon</b><span class="psub">Agents reconnect by themselves after a restart. Queued messages are kept.</span></span><span class="pact"><button class="ghost" data-act="restart">Restart daemon</button></span></div></div>
<p class="mut small" id="ctlmsg"></p></section>
</main></div>
<script>
const $=s=>document.querySelector(s);let cfg={},editing=false,sel='';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// Markdown as agents write it, made safe: everything is escaped first, then a small set of marks become tags.
// Links open only http(s) URLs. plain() drops the marks, for one-line previews and titles.
const md=src=>{const keep=[];const hold=h=>{keep.push(h);return `\u0001${keep.length-1}\u0001`};
  const inline=s=>{s=s.replace(/`([^`\n]+)`/g,(_,c)=>hold(`<code>${esc(c)}</code>`))
      .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,(_,l,u)=>hold(`<a href="${esc(u)}" target="_blank" rel="noreferrer">${esc(l)}</a>`))
      .replace(/https?:\/\/[^\s<>"'`]+[^\s<>"'`.,;:!?)\]]/g,u=>hold(`<a href="${esc(u)}" target="_blank" rel="noreferrer">${esc(u)}</a>`));
    return esc(s).replace(/\*\*(?=\S)([^*\n]+?)\*\*/g,'<b>$1</b>').replace(/__(?=\S)([^_\n]+?)__/g,'<b>$1</b>')
      .replace(/(^|[^\w*])\*(?=\S)([^*\n]+?)\*(?!\w)/g,'$1<i>$2</i>').replace(/(^|[^\w_])_(?=\S)([^_\n]+?)_(?!\w)/g,'$1<i>$2</i>')
      .replace(/~~(?=\S)([^~\n]+?)~~/g,'<s>$1</s>')};
  let t=String(src??'').replace(/\r/g,'').replace(/```[^\n]*\n?([\s\S]*?)(```|$)/g,(_,c)=>`\n${hold(`<pre><code>${esc(c.replace(/\n$/,''))}</code></pre>`)}\n`);
  const out=[];let list=null,para=[];
  const flush=()=>{if(para.length){out.push(`<p>${para.join('<br>')}</p>`);para=[]}if(list){out.push(`<${list.tag}${list.start>1?` start="${list.start}"`:''}>${list.items.map(i=>`<li>${i}</li>`).join('')}</${list.tag}>`);list=null}};
  for(const line of t.split('\n')){let m;
    if(!line.trim()){flush();continue}
    if(/^\u0001\d+\u0001$/.test(line.trim())){flush();out.push(line.trim());continue}
    if((m=line.match(/^\s{0,3}#{1,6}\s+(.*)$/))){flush();out.push(`<p class="mdh">${inline(m[1])}</p>`);continue}
    if((m=line.match(/^\s*(?:[-*+]|(\d+)[.)])\s+(.*)$/))){const tag=m[1]?'ol':'ul';if(para.length||list&&list.tag!==tag)flush();
      list=list||{tag,items:[],start:+m[1]||1};list.items.push(inline(m[2]));continue}
    if((m=line.match(/^\s*>\s?(.*)$/))){flush();out.push(`<blockquote>${inline(m[1])}</blockquote>`);continue}
    if(list&&/^\s{2,}\S/.test(line)){list.items[list.items.length-1]+='<br>'+inline(line.trim());continue}
    if(list)flush();para.push(inline(line))}
  flush();return out.join('').replace(/\u0001(\d+)\u0001/g,(_,i)=>keep[+i])};
const plain=s=>String(s??'').replace(/```[^\n]*\n?/g,'').replace(/\[([^\]\n]+)\]\([^)\s]+\)/g,'$1').replace(/(\*\*|__|~~|`)/g,'').replace(/^\s{0,3}#{1,6}\s+/gm,'').replace(/\s+/g,' ').trim();
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
  const why=x.space_source==='started'?'the agent that started it asked for it':x.space_source==='set'?'you put its folder here':x.space_source==='file'?'from '+tilde(x.space_from):'no space set';
  const how={set:'you picked it',only:'it was the only agent in the space'}[x.leads]||'';
  const led=leadOf[x.space];
  const role=x.leads?`Leads the <b>${esc(x.space)}</b> space${x.parent?` inside <b>${esc(x.parent)}</b>`:''} <span class="mut">· ${how}</span>`:`Member${led?` · led by ${esc(led.name)}${led.live?'':' (offline)'}`:''}`;
  // Only you, here, pick a lead: the daemon refuses it from agents and bots.
  const leadBtn=x.remote||!x.space||x.space==='default'?'':x.leads?`<button class="ghost small" data-clearlead="${esc(x.space)}">Clear lead</button>`:`<button class="ghost small" data-makelead="${esc(x.space)}" data-cwd="${esc(x.cwd)}">${led?'Make lead instead of '+esc(led.name):'Make lead of '+esc(x.space)}</button>`;
  const opt=(v,t)=>`<option value="${v}" ${wake===v?'selected':''}>${t}</option>`;
  const tab=x.remote?'about':panelTab;
  return `<div class="card panel${x.leads?' is-lead':''}">
   <div class="top"><span class="phead"><span class="plogo" style="background:${x.leads?'var(--acc)':'var(--mut)'}"><b style="color:#fff">${initial(x.name)}</b></span>
     <span class="ptitle"><span><span class="name">${esc(x.name)}</span>${x.remote?` <span class="mut small">@${esc(x.host)}</span>`:''}</span>
     <span class="psub"><span class="dot ${x.busy?'busy':''}"></span>${x.busy?'working':'idle'}${x.busy_since?' '+ago(x.busy_since):''} · <span class="pill">${esc(x.space||'default')}</span>${x.leads?' <span class="pill acc">lead</span>':''}</span></span></span><button class="ghost x" data-close title="Close">✕</button></div>
   <div class="pwhere"><span class="mono">${esc(tilde(x.cwd))}${x.branch?' · ⎇ '+esc(x.branch):''}</span><button class="ghost small copy" data-copy="${esc(x.cwd)}" title="Copy the folder path">Copy</button></div>
   ${x.role?`<div class="role"><span class="k">Role</span> ${esc(x.role)}</div>`:''}
   <div class="pnow"><div class="k small">Now</div>${esc(x.summary)||'<span class="mut">no summary yet</span>'}</div>
   ${x.remote?'':`<div class="ptiles"><div><span class="k small">Context</span><b>${x.context??'?'}%</b>${x.context!=null?`<span class="cmeter${x.context>=80?' hi':x.context>=50?' mid':''}"><i style="width:${Math.min(100,x.context)}%"></i></span>`:''}</div>
     <div><span class="k small">Sent / hour</span><b${x.sent_hour>=20?' style="color:var(--warn)"':''}>${x.sent_hour||0}</b></div><div><span class="k small">Open tasks</span><b>${x.open_tasks||0}</b></div></div>`}
   ${x.remote?'':`<div class="ptabs">${[['about','Agent'],['tasks','Tasks'+(x.open_tasks?' · '+x.open_tasks:'')],['details','Details'],['controls','Controls']].map(([k,t])=>`<button class="${tab===k?'on':''}" data-ptab="${k}">${t}</button>`).join('')}</div>`}
   ${tab==='tasks'?panelTasks(x):tab==='details'?`<form id="agentform" data-id="${esc(x.id)}" data-nick="${esc(sc.nickname||'')}" data-space="${esc(setSpace)}" data-role="${esc(sc.role||'')}" data-cwd="${esc(x.cwd)}" data-also="${esc((x.also||[]).join(', '))}">
    <label>Name<input name="nickname" type="text" value="${esc(sc.nickname||'')}" placeholder="${esc(x.name)}"><span class="hint">What other agents call it. Kept for this folder.</span></label>
    <label>Role<textarea name="role" rows="3" placeholder="${x.folder_role?'From the folder: '+esc(x.folder_role):'What this agent is here for, e.g. Owns the billing API: invoices, payments, the Stripe webhook.'}">${esc(sc.role||'')}</textarea><span class="hint">For this agent only, kept across /clear and resume. The agent gets it as its brief, and other agents see it.${x.folder_role?' Leave it empty to use the role: in its folder\'s .peerlink.':' A role: line in a folder\'s .peerlink is the role of every agent there without one of its own.'}</span></label>
    <label>Space<input name="space" type="text" list="spacenames" value="${esc(setSpace)}" placeholder="${x.space_source==='file'?esc(x.space)+' (from its .peerlink)':x.space_source==='started'?esc(x.space)+' (started for it)':'unassigned'}"><span class="hint">Kept for its folder: every agent opened there joins it.${x.space_source==='file'||!setSpace?'':' Leave it empty to go back to the folder\'s .peerlink.'}</span></label>
    <details class="adv"${(x.also||[]).length?' open':''}><summary>Also in other spaces</summary>
    <label>Also a member of<input name="also" type="text" value="${esc((x.also||[]).join(', '))}" placeholder="none"><span class="hint">Rarely needed: more spaces it sees and messages, on top of its own. Separate with commas.</span></label></details>
    <datalist id="spacenames">${Object.keys(leadOf).concat(livePeers.map(p=>p.space)).filter((n,i,a)=>n&&n!=='default'&&a.indexOf(n)===i).sort().map(n=>`<option value="${esc(n)}">`).join('')}</datalist>
    <div><button type="submit">Save</button> <span id="agentmsg" class="small"></span></div></form>`:tab==='controls'?`<form id="agentform" data-id="${esc(x.id)}" data-wake="${wake}" data-muted="${x.muted?'1':''}" data-auto="${x.auto_approve?'1':''}">
    <label>When a message arrives<select name="wake">${opt('default','Use the default')}${opt('on','Start a turn')}${opt('off','Wait for the next prompt')}</select></label>
    <label class="check"><input name="muted" type="checkbox" ${x.muted?'checked':''}> Mute: hold every message for this agent</label>
    <label class="check"><input name="auto_approve" type="checkbox" ${x.auto_approve||cfg.auto_approve_all?'checked':''} ${cfg.auto_approve_all?'disabled title="On for every agent in Settings"':''}> Let it start agents and schedules without asking me (its folder keeps this)</label>
    <div><button type="submit">Save</button> <span id="agentmsg" class="small"></span></div></form>
    ${leadBtn?`<div class="threads"><div class="k small">Lead</div><div class="small mut">${role}</div><div>${leadBtn}</div></div>`:''}
    <div class="threads"><div class="k small">Clear its context</div><div class="small mut">Runs /clear once its current turn ends: a fresh conversation in the same folder, keeping its name, space and role. What it still owes peers is given back to it.</div>
    <div><button class="ghost small" data-clearctx="${esc(x.id)}">Clear context</button> <span class="small ok" id="clearmsg"></span></div></div>
    <div class="threads"><div class="k small">End this agent</div><div class="small mut">It finishes its current turn, then closes. If it's still running a minute later, it's stopped.</div>
    <div><button class="ghost small danger" data-terminate="${esc(x.id)}" ${x.terminating?'disabled':''}>${x.terminating?'Ending…':'Terminate'}</button></div></div>`:`<div class="facts">${x.muted?'<span class="k">Status</span><span>muted</span>':''}
    <span class="k">Space</span><span><span class="pill">${esc(x.space||'default')}</span> <span class="mut">${esc(why)}</span>${(x.also||[]).length?` <span class="mut">· also in</span> ${x.also.map(n=>`<span class="pill">${esc(n)}</span>`).join(' ')}`:''}</span>
    <span class="k">Role</span><span>${role}</span>
    ${x.sent_hour>=20?'<span class="k">Messages</span><span style="color:var(--warn)">chatty: each one can cost its recipient a turn</span>':''}
    ${x.context>=80?'<span class="k">Context</span><span style="color:var(--warn)">close to compacting</span>':''}
    <span class="k">Plugin</span><span>${esc(x.plugin||'older than 0.6.6')}${x.stale&&!x.remote?` · <span style="color:var(--warn)">${x.reload_queued?`reload queued ${ago(x.reload_queued)} ago and not run yet: `:''}run /reload-plugins in this agent</span>`:''}</span></div>
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
  const noSched=$('#msgnosched').checked;
  const shown=inSpace.filter(x=>!(noSched&&x.from==='peerlink'&&/^schedule /.test(x.from_name||''))&&(!msgFilter||x.from_name===msgFilter||x.to_name===msgFilter)&&(!q||[x.text,x.from_name,x.to_name,x.id].join(' ').toLowerCase().includes(q)));
  // One panel per conversation: the two ends of each message, newest activity first. Opened, it shows the pair's whole
  // history from the log, oldest first, as a chat reads.
  const convs=new Map();
  for(const x of shown){const a=partyOf(x,'from'),b=partyOf(x,'to'),key=[a,b].sort().join('|');
    const c=convs.get(key)||{key,a:a<b?a:b,b:a<b?b:a,names:{},list:[]};c.names[a]=x.from_name;c.names[b]=x.to_name;c.list.push(x);convs.set(key,c)}
  const rows=[...convs.values()].map(c=>({...c,last:c.list.reduce((m,x)=>x.ts>m.ts?x:m)})).sort((p,q)=>q.last.ts-p.last.ts);
  const html=rows.map(c=>{const open=openConvs.has(c.key),hist=convHist[c.key];
    const pending=c.list.filter(x=>x.status==='queued').length,waiting=c.list.some(x=>x.expects_reply&&!c.list.some(y=>y.reply_to===x.id));
    const body=!open?'':hist?hist.list.map(x=>convLine(x,c)).join('')+(hist.list.length>=500?'<div class="small mut">Older messages are in ~/.peerlink/messages.jsonl.</div>':''):'<div class="small mut">Loading…</div>';
    return `<details class="plat conv" data-conv="${esc(c.key)}" data-a="${esc(c.a)}" data-b="${esc(c.b)}" data-last="${esc(c.last.id)}" ${open?'open':''}><summary>
      <span class="ptitle"><span class="cnames">${partyLabel(c.a,c.names[c.a])}<span class="mut">↔</span>${partyLabel(c.b,c.names[c.b])}</span>
      <span class="psub">${esc(c.last.from_name)}: ${esc(plain(c.last.text).slice(0,140))}</span></span>
      <span class="pstate">${pending?`<span class="pill warn">${pending} queued</span>`:''}${waiting?'<span class="pill">reply expected</span>':''}<span class="small mut">${when(c.last.ts)}</span></span><span class="chev" aria-hidden="true"></span></summary>
      <div class="pbody convbody">${body}</div></details>`}).join('');
  $('#msgs').innerHTML=html||'<div class="empty">No messages yet.</div>';
  document.querySelectorAll('#msgs .convbody').forEach(el=>el.scrollTop=el.scrollHeight);
  for(const c of rows)if(openConvs.has(c.key)&&(!convHist[c.key]||convHist[c.key].last!==c.last.id))loadConv(c.key,c.a,c.b,c.last.id);
}
const when=ts=>new Date(ts*1000).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
const partyOf=(x,side)=>{const who=String(x[side]||'');return who.startsWith('discord:')?who:'n:'+String(x[side+'_name']||who).toLowerCase()};
// A chat channel shows its platform's logo, and its name without "#… on Telegram".
const partyLabel=(key,name)=>{if(!key.startsWith('discord:'))return `<b>${esc(name)}</b>`;const tg=key.startsWith('discord:tg:');
  return `<span class="mlogo" style="background:${tg?'#26A5E4':'#5865F2'}">${tg?TG_LOGO:DISCORD_LOGO}</span><b>${esc(String(name).replace(/^#/,'').replace(/ on (Discord|Telegram|Slack)$/,''))}</b>`};
function convLine(x,c){const st=x.status==='delivered'?'':`<span class="chip ${esc(x.status)}">${esc(x.status)}</span>`;
  return `<div class="cline${partyOf(x,'from')===c.a?'':' other'}${openLines.has(x.id)?' open':''}" data-line="${esc(x.id)}"><div class="mh"><b>${esc(x.from_name)}</b>
    ${x.kind==='announcement'?`<span class="chip reply">announcement · ${(x.recipients||[]).length} agents</span>`:''}${x.task?`<span class="chip reply">task ${esc(x.task)}</span>`:x.expects_reply?'<span class="chip">reply expected</span>':''}${x.reply_to?`<span class="chip reply">re ${esc(x.reply_to)}</span>`:''}${st}
    <span class="when">${when(x.ts)} · ${esc(x.id)}</span></div><div class="mt md">${md(x.text)}</div></div>`}
const openConvs=new Set(),convHist={},openLines=new Set();
// Schedule runs are hidden unless you've asked to see them.
try{$('#msgnosched').checked=localStorage.getItem('pl.msg.nosched')!=='0'}catch(e){$('#msgnosched').checked=true}
document.addEventListener('click',e=>{const l=e.target.closest('.cline');if(!l||e.target.closest('a')||getSelection().toString())return;l.classList.toggle('open');
  l.classList.contains('open')?openLines.add(l.dataset.line):openLines.delete(l.dataset.line)});let lastMsgs=[];
async function loadConv(key,a,b,last){convHist[key]=convHist[key]||null;
  const r=await api(`/api/conversation?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);convHist[key]={last,list:r.messages||[]};
  const el=document.querySelector(`details.conv[data-conv="${CSS.escape(key)}"] .convbody`);if(!el)return;
  const c={a};el.innerHTML=convHist[key].list.map(x=>convLine(x,c)).join('')||'<div class="small mut">Nothing in the log.</div>';el.scrollTop=el.scrollHeight}
// <details> toggles don't bubble: listened for on the way down.
document.addEventListener('toggle',e=>{const d=e.target;if(!d.matches?.('details.conv'))return;const k=d.dataset.conv;
  if(d.open){openConvs.add(k);if(!convHist[k]||convHist[k].last!==d.dataset.last)loadConv(k,d.dataset.a,d.dataset.b,d.dataset.last)}else openConvs.delete(k)},true);
document.addEventListener('change',e=>{
  if(e.target.id==='msgagent'){msgFilter=e.target.value;renderMessages(lastMsgs)}
  if(e.target.id==='msgnosched'){try{localStorage.setItem('pl.msg.nosched',e.target.checked?'1':'0')}catch(err){}renderMessages(lastMsgs)}
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
  // Context as a small meter: how full the agent's window is, its number on hover.
  // The list shows a few words; the side panel the whole summary. Older plugins send no headline: the first sentence, cut.
  const headline=x=>{if(x.headline)return x.headline;const first=String(x.summary||'').split(/(?<=[.;!?])\s|\s[—–-]\s/)[0].trim().replace(/[;:,]$/,'');
    return first.length>80?first.slice(0,first.lastIndexOf(' ',78)>40?first.lastIndexOf(' ',78):78)+'…':first};
  const ctx=x=>x.context==null?'':`<span class="cmeter${x.context>=80?' hi':x.context>=50?' mid':''}" title="${x.context}% of its context window in use"><i style="width:${Math.min(100,x.context)}%"></i></span>`;
  // A lead is told apart by its outline (and says which team on hover), so every card keeps its context in one place.
  // The badge sits in the name row, left of the context; an offline lead's clear link goes beneath.
  // An implicit lead (it was alone in the space) has a dashed badge, and says so on hover, so the name keeps its room.
  const badge=x=>(x.also||[]).map(n=>`<span class="badge guest" title="Also a member of ${esc(n)}">+${esc(n)}</span>`).join('')+(x.leads?`<span class="badge${x.leads==='only'?' only':''}" title="${x.leads==='only'?'Lead (only agent): it was the only agent in the space':'Lead'}">Lead</span>`:'');
  const clearOff=x=>x.leads&&x.offline?`<div><span class="small mut" data-clearlead="${esc(x.space)}" title="Clear this lead" style="cursor:pointer">clear</span></div>`:'';
  const node=x=>`<${x.offline?'div':'button'} class="node${x.leads?' is-lead':''}${x.offline?' off':''}${x.id&&x.id===sel?' sel':''}"${x.offline?'':` data-sel="${esc(x.id)}"`}${x.leads?` title="Leads the ${esc(x.space)} space${x.offline?' (offline)':''}"`:''}>
    <div class="nm"><span title="${esc(x.name)}">${x.offline?'':`<span class="dot ${x.busy?'busy':''}"></span>`}${esc(x.name)}</span><span class="rt">${badge(x)}</span></div>${clearOff(x)}
    <div class="dir" title="${esc(tilde(x.cwd))}${x.branch?' · '+esc(x.branch):''}">${esc(short(x.cwd))}${x.branch?` · ⎇ ${esc(x.branch)}`:''}</div><div class="sum" title="${esc(x.summary||x.role||'')}">${esc(headline(x)||x.role)||'no summary yet'}</div>${x.offline?'':`<div class="state"><span>${state(x)}${x.sent_hour?' · '+rate(x):''}${x.open_tasks?` · <span style="color:var(--acc)">${x.open_tasks} task${x.open_tasks>1?'s':''}</span>`:''}</span>${ctx(x)}</div>`}</${x.offline?'div':'button'}>`;
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
      <span class="lid"><span class="ln"><span title="${esc(x.name)}">${x.offline?'':`<span class="dot ${x.busy?'busy':''}"></span>`}${esc(x.name)}</span>${badge(x)}</span><span class="lwhere mono" title="${esc(tilde(x.cwd))}">${where}</span></span>
      <span class="lw" title="${esc(x.summary||x.role||'')}">${esc(headline(x)||x.role||(x.offline?'offline':''))}</span>
      <span class="lr">${x.busy&&!x.offline?`<span class="lbusy">${state(x)}</span>`:''}${x.sent_hour>=20?rate(x):''}${x.open_tasks?`<span class="pill acc">${x.open_tasks} task${x.open_tasks>1?'s':''}</span>`:''}${ctx(x)}</span></${x.offline?'div':'button'}>`};
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
  // Agents still on older plugin code: they keep working, but get fixes only after /reload-plugins. The daemon runs
  // the newest peerlink installed, so the fix is a reload, not an update. Each reloads itself when next idle; one
  // whose queued reload hasn't run in 5 minutes is stuck (a dialog open in its terminal, or no turn ever ending).
  const behind=p.peers.filter(x=>x.stale&&!x.remote);
  const stuck=behind.filter(x=>!x.reload_queued||Date.now()/1000-x.reload_queued>300);
  // One line, the names behind a click. Dismissed, it stays away until the set of agents on an old plugin changes.
  const staleKey=stuck.map(x=>x.name).sort().join(','),bar=$('#stalebar');let gone='';try{gone=localStorage.getItem('pl.stale.dismissed')||''}catch(e){}
  bar.style.display=stuck.length&&gone!==staleKey?'flex':'none';bar.dataset.key=staleKey;
  if(stuck.length&&bar.dataset.shown!==staleKey){bar.dataset.shown=staleKey;
    bar.innerHTML=`<details><summary><b>${stuck.length} agent${stuck.length>1?'s':''} need${stuck.length>1?'':'s'} <span class="mono">/reload-plugins</span></b> <span class="mut">for the newer peerlink plugin · show which</span></summary>
      <div class="small">${stuck.map(x=>esc(x.name)+(x.reload_queued?` <span class="mut">(reload waiting ${ago(x.reload_queued)})</span>`:'')).join(', ')}. Run <span class="mono">/reload-plugins</span> in ${stuck.length>1?'each':'it'}, or terminate it.</div></details>
      <button class="ghost small" data-stalehide title="Hide until another agent needs it">Dismiss</button>`}
  const picked=p.peers.find(x=>x.id===sel);
  const panel=picked?agentPanel(picked,p.peers.filter(y=>y.id!==picked.id&&y.cwd===picked.cwd&&y.space===picked.space)):'';
  // Never redraw the panel under someone editing it.
  if(panel!==lastPanel&&!(editing&&picked&&$('#agentform')?.dataset.id===picked.id)){$('#detail').innerHTML=panel;lastPanel=panel}
  // The open panel takes room beside the chart rather than covering part of it.
  document.body.classList.toggle('drawer',!!picked);
  // The default space is always listed: it's where every agent with no space set lands.
  const dflt=groups.default?'':`<div class="chart"><div class="orow" style="justify-content:flex-start"><div class="ocol"><div class="sbox" style="min-height:0"><div class="name">unassigned</div><div class="small mut">agents whose folder has no space · none live</div></div></div></div></div>`;
  $('#spacelist').innerHTML=`<div class="sgrid">${all((n,seen)=>{const u=sunit(n,seen);return u?`<div class="chart">${u}</div>`:''})}</div>`+dflt;
  livePeers=p.peers;lastMsgs=m.messages;renderMessages(lastMsgs);renderTasks();renderFeedback();renderInbox();renderSchedules();
}
// Schedules: what's active or waiting first; the ones that ended fold away. Each says who'd get it now.
let allSched=[],schedEdit='';
const openSched=new Set(),SCHED_ICON='<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2"/></svg>';
document.addEventListener('toggle',e=>{const d=e.target;if(!d.matches?.('details.sched'))return;d.open?openSched.add(d.dataset.sched):openSched.delete(d.dataset.sched)},true);
function renderSchedules(force){
  const live=c=>c.status==='active'||c.status==='pending';
  $('#n-sched').textContent=allSched.filter(live).length||'';
  // Pick an agent, by name: several can share a folder.
  const agents=livePeers.filter(x=>!x.remote).sort((a,b)=>a.name.localeCompare(b.name));
  const opts=(cur,extra='')=>extra+agents.map(x=>`<option value="${esc(x.name)}" ${x.name===cur?'selected':''}>${esc(x.name)} · ${esc(tilde(x.cwd))}</option>`).join('');
  const pick=$('#schedagents');if(document.activeElement!==pick){const was=pick.value;pick.innerHTML=opts(was,'<option value="">Pick an agent</option>')}
  // Never redraw a schedule under someone editing it.
  if(schedEdit&&!force)return;
  const when=t=>t?new Date(t*1000).toLocaleString([], {weekday:'short',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'';
  const local=t=>{const d=new Date(t*1000),z=n=>String(n).padStart(2,'0');return `${d.getFullYear()}-${z(d.getMonth()+1)}-${z(d.getDate())} ${z(d.getHours())}:${z(d.getMinutes())}`};
  const btn=(attr,id,label,cls='')=>`<button class="ghost small ${cls}" ${attr}="${esc(id)}">${label}</button>`;
  const actions=c=>btn('data-schededit',c.id,'Edit')+(c.status==='pending'?`<button class="ghost small" data-schedok="${esc(c.id)}" data-act="approve">Approve</button><button class="ghost small" data-schedok="${esc(c.id)}" data-act="deny">Deny</button>`
    :c.status==='active'?btn('data-schedcancel',c.id,'Cancel','danger'):['cancelled','denied','expired'].includes(c.status)?btn('data-schedon',c.id,'Enable again'):'');
  // Who gets it: the agent named while it runs in the folder, else the folder's agent, else a new one or none.
  const route=c=>c.goes_to?`goes to <b>${esc(c.goes_to)}</b>`
    :c.agent?`<span style="color:var(--warn)">${esc(c.agent)} isn't running</span>: ${c.start?`starts it again in ${esc(tilde(c.folder))}`:'the run waits an hour for it'}`
    :c.start?`no agent running there: starts one`:`no agent running there: waits for one`;
  const card=c=>schedEdit===c.id?`<form class="card" data-schedform="${esc(c.id)}"><div class="addline"><b>${esc(c.id)}</b><span class="chip">${esc(c.status)}</span>
      <select name="target" class="grow">${opts(c.agent,c.agent&&!agents.some(x=>x.name===c.agent)?`<option value="${esc(c.agent)}" selected>${esc(c.agent)} (not running)</option>`:!c.agent?`<option value="" selected>Any agent in ${esc(tilde(c.folder))}</option>`:'')}</select>
      <input name="when" type="text" value="${esc(c.cron||local(c.at))}" placeholder="0 9 * * 1-5, or in 2h" style="width:190px"></div>
    <textarea name="prompt" rows="4" style="margin-top:8px;width:100%">${esc(c.prompt)}</textarea>
    <div class="addline" style="margin-top:8px"><label class="check small"><input type="checkbox" name="start" ${c.start?'checked':''}> Start it when it isn&#39;t running</label>
      <button type="submit">Save</button><button type="button" class="ghost" data-schededit="">Cancel</button><span class="small err" data-schedmsg></span></div></form>`
    :`<details class="plat sched" data-sched="${esc(c.id)}" ${openSched.has(c.id)||c.status==='pending'?'open':''}><summary>
      <span class="plogo" style="background:${c.status==='active'?'var(--acc)':c.status==='pending'?'var(--warn)':'var(--mut)'}">${SCHED_ICON}</span>
      <span class="ptitle"><b>${esc(c.agent||tilde(c.folder))} <span class="mut" style="font-weight:400">· ${esc(c.when_text)}</span></b><span class="psub">${esc(c.prompt.replace(/\s+/g,' ').slice(0,160))}</span></span>
      <span class="pstate">${c.status==='active'?'':`<span class="pill${c.status==='pending'?' warn':''}">${esc(c.status==='pending'?'needs approval':c.status)}</span>`}<span class="small mut">${c.next_run&&live(c)?'next '+when(c.next_run):c.runs?'last '+when(c.last_run):''}</span></span><span class="chev" aria-hidden="true"></span></summary>
    <div class="pbody" style="padding-top:12px"><div class="md">${md(c.prompt)}</div>
    <div class="small mut" style="margin-top:8px">${esc(c.id)} · ${live(c)?route(c)+' · ':''}by ${esc(c.requester?.name||'you')}${c.agent?` · in <span class="mono">${esc(tilde(c.folder))}</span>`:''}${c.runs?` · ran ${c.runs}×, last ${when(c.last_run)}${c.note?': '+esc(c.note):''}`:''}${c.status==='pending'?' · waiting for your approval':''}</div>
    <div class="addline" style="margin-top:10px">${actions(c)}</div></div></details>`;
  const on=allSched.filter(live),ended=allSched.filter(c=>!live(c));
  $('#schedlist').innerHTML=(on.map(card).join('')||'<div class="empty">No schedules running.</div>')
    +(ended.length?`<details ${ended.some(c=>c.id===schedEdit)?'open':''}><summary class="small mut" style="cursor:pointer;margin-top:4px">Ended (${ended.length})</summary><div style="display:flex;flex-direction:column;gap:8px;margin-top:8px">${ended.map(card).join('')}</div></details>`:'')}
document.addEventListener('click',e=>{const b=e.target.closest('[data-schededit]');if(!b)return;schedEdit=b.dataset.schededit;renderSchedules(true)});
document.addEventListener('submit',async e=>{const f=e.target.closest('[data-schedform]');if(!f)return;e.preventDefault();const v=n=>f.elements[n].value.trim();
  const w=v('when'),cron=/^\S+\s+\S+\s+\S+\s+\S+\s+\S+$/.test(w)&&!/^in\s/i.test(w)&&!/^\d{4}-/.test(w);
  const r=await post(`/api/schedules/${encodeURIComponent(f.dataset.schedform)}`,{...(v('target')?{agent:v('target')}:{}),prompt:v('prompt'),...(cron?{cron:w}:{at:w}),start:f.elements.start.checked});
  if(r.error){f.querySelector('[data-schedmsg]').textContent=r.error;return}
  schedEdit='';refresh(true)});
document.addEventListener('click',async e=>{const b=e.target.closest('[data-schedok]');if(!b)return;
  const r=await post('/api/inbox',{kind:'approval',id:b.dataset.schedok,action:b.dataset.act});if(r.error)alert(r.error);refresh(true)});
document.addEventListener('click',async e=>{const b=e.target.closest('[data-schedon]');if(!b)return;
  const r=await post(`/api/schedules/${encodeURIComponent(b.dataset.schedon)}/enable`);if(r.error)alert(r.error);refresh(true)});
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
// A list split into panels (Inbox by agent, Feedback by status); one you open or close stays that way across redraws.
const grpState={};
document.addEventListener('toggle',e=>{const d=e.target;if(d.matches?.('details.grp'))grpState[d.dataset.grp]=d.open},true);
const initial=n=>esc(String(n||'?').replace(/^[^a-z0-9]+/i,'').slice(0,1).toUpperCase()||'?');
const grp=(key,{title,sub='',state='',bg='var(--mut)',icon,open=true},body)=>`<details class="plat grp" data-grp="${esc(key)}" ${(grpState[key]??open)?'open':''}><summary>
  <span class="plogo" style="background:${bg}">${icon||`<b style="color:#fff">${initial(title)}</b>`}</span><span class="ptitle"><b>${esc(title)}</b><span class="psub">${sub}</span></span>
  <span class="pstate">${state}</span><span class="chev" aria-hidden="true"></span></summary><div class="pbody grpbody">${body}</div></details>`;
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
  const sched=i=>i.kind==='approval'&&/^c\d+$/.test(i.id);
  const label={approval:'wants a new agent',task:'task for you',question:'waiting on your answer',feedback:'feedback'};
  const btn=(a,t)=>`<button class="ghost small" data-iact="${a}">${t}</button>`;
  const item=i=>`<div class="task" data-ikind="${esc(i.kind)}" data-iid="${esc(i.id)}"><div class="mh"><span class="tchip ${i.kind==='task'||i.kind==='approval'?'open':i.kind==='question'?'new':esc(i.type)}">${sched(i)?'wants a schedule':label[i.kind]}</span><b class="ititle">${esc(plain(i.title))}</b><span class="when">${esc(i.id)} · waiting ${ago(i.since)}</span></div>
    <div class="small mut">from <b>${esc(i.who)}</b>${i.kind==='feedback'?' · '+esc(i.type):''}</div>
    ${(()=>{// The title is the first line, cut: when it was cut, the body keeps the whole text so nothing is lost mid-word.
      const t=i.text.trim(),[first,...rest]=t.split('\n'),more=first.trim()===i.title.trim()?rest.join('\n').trim():t;return more?`<div class="ftext md">${md(more)}</div>`:''})()}
    <div class="tact">${i.kind==='approval'?`${btn('approve',sched(i)?'Approve':'Approve and start it')}${btn('deny','Deny')}`:i.kind==='task'?`<input class="small" data-itext placeholder="Note for ${esc(i.who)} (optional)">${btn('done','Done')}${btn('failed',"Couldn't do it")}`
      :i.kind==='question'?`<input class="small" data-itext placeholder="Your answer">${btn('reply','Send')}${btn('dismiss','Dismiss')}`
      :`${btn('accepted','Accept')}${btn('dismissed','Dismiss')}`}</div></div>`;
  // By who's asking, the longest waiting first; a lone item still sits in its agent's panel.
  const byWho=new Map();for(const i of inbox){const k=i.who||'an agent';if(!byWho.has(k))byWho.set(k,[]);byWho.get(k).push(i)}
  const kinds=l=>[...new Set(l.map(i=>sched(i)?'schedule':i.kind==='approval'?'new agent':i.kind))].join(', ');
  $('#inboxlist').innerHTML=[...byWho].sort((a,b)=>Math.min(...a[1].map(i=>i.since))-Math.min(...b[1].map(i=>i.since))).map(([who,l])=>grp('inbox:'+who,{title:who,sub:esc(kinds(l)),
      state:`<span class="pill warn">${l.length}</span><span class="small mut">waiting ${ago(Math.min(...l.map(i=>i.since)))}</span>`,bg:'var(--acc)'},l.map(item).join(''))).join('')
    ||'<div class="empty">Nothing is waiting on you.</div>'}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-iact]');if(!b)return;const row=b.closest('[data-iid]');
  const text=row.querySelector('[data-itext]')?.value||'';
  if(b.dataset.iact==='reply'&&!text.trim()){row.querySelector('[data-itext]').focus();return}
  if(b.dataset.iact==='approve'&&!/^c\d+$/.test(row.dataset.iid)&&!confirm('Start a new Claude Code session in that folder?'))return;
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
  const fbRow=f=>{const task=allTasks.find(t=>t.id===f.task);let [head,...rest]=f.text.split('\n');
    // A long first line is cut to a headline; the rest of it reads below.
    if(head.length>120){const cut=head.lastIndexOf(' ',120)>60?head.lastIndexOf(' ',120):120;rest=[head.slice(cut).trim(),...rest];head=head.slice(0,cut)+'…'}
    return `<div class="task" data-fb="${esc(f.id)}"><div class="mh"><span class="tchip ${esc(f.type)}">${esc(f.type)}</span><span class="tchip ${esc(f.status)}">${esc(f.status)}</span><b>${esc(head)}</b><span class="when">${esc(f.id)} · ${ago(f.ts)} ago</span></div>
      <div class="small mut">${esc(f.from_name||'an agent')}${f.space?' · '+esc(f.space):''} · plugin ${esc(f.plugin||'?')}, daemon ${esc(f.daemon||'?')}${task?` · task ${esc(task.id)} ${esc(TSTATE[task.status]||task.status)} with <b>${esc(task.owner)}</b>`:''}</div>
      ${rest.join('\n').trim()?`<div class="ftext md">${md(rest.join('\n').trim())}</div>`:''}
      <div class="tact"><input class="small" data-fnote placeholder="Note (optional)" value="${esc(f.note||'')}">
      ${f.status==='new'?btn('accepted','Accept')+btn('dismissed','Dismiss'):f.status==='accepted'?btn('done','Mark done')+btn('dismissed','Dismiss'):btn('new','Back to new')}</div></div>`};
  // All statuses: a panel each, the ones that need you open. One status picked: just its list.
  const FB={new:['New','Not looked at yet','var(--acc)',true],accepted:['Accepted','Handed to the peerlink agent as a task','var(--warn)',true],done:['Done','Fixed or built','var(--ok)',false],dismissed:['Dismissed','Not acted on','var(--mut)',false]};
  $('#fblist').innerHTML=(st?shown.map(fbRow).join(''):Object.entries(FB).map(([k,[title,sub,bg,open]])=>{const l=shown.filter(f=>f.status===k);
      return l.length?grp('fb:'+k,{title,sub,bg,open,state:`<span class="pill">${l.length}</span>`},l.map(fbRow).join('')):''}).join(''))
    ||`<div class="empty">${allFeedback.length?'Nothing here.':'No feedback yet.'}</div>`}
document.addEventListener('click',async e=>{const b=e.target.closest('[data-fact]');if(!b)return;const row=b.closest('[data-fb]');
  const r=await post('/api/issues/'+encodeURIComponent(row.dataset.fb),{status:b.dataset.fact,note:row.querySelector('[data-fnote]').value});
  if(r.error)alert(r.error);document.activeElement?.blur();refresh(true)});
document.addEventListener('change',async e=>{const t=e.target;
  if(t.id==='fstatus'||t.id==='ftype'){renderFeedback();return}
  if(t.dataset.fnote!==undefined){const r=await post('/api/issues/'+encodeURIComponent(t.closest('[data-fb]').dataset.fb),{note:t.value});if(r.error)alert(r.error);t.blur();refresh(true)}});
// Tasks: one row each, its subtasks under it. The Tasks page and an agent's Tasks tab share the row.
let allTasks=[],panelTab='about';
const TSTATE={open:'open',in_progress:'in progress',done:'done',failed:'failed',cancelled:'cancelled'};
const isLive=t=>t.status==='open'||t.status==='in_progress';
const ago=ts=>{const d=Date.now()/1000-ts;return d<90?Math.round(d)+'s':d<5400?Math.round(d/60)+'m':d<129600?Math.round(d/3600)+'h':Math.round(d/86400)+'d'};
function taskRow(t,sub){const kids=allTasks.filter(x=>x.parent===t.id);
  return `<div class="task${sub?' sub':''}" data-task="${esc(t.id)}"><div class="mh"><span class="tchip ${esc(t.status)}">${TSTATE[t.status]||esc(t.status)}</span><b>${esc(t.title)}</b><span class="when">${esc(t.id)} · ${ago(t.created)} ago</span></div>
    <div class="small mut">${esc(t.requester?.name||'?')} → <b>${esc(t.owner)}</b>${t.space?' · '+esc(t.space):''}${kids.length?` · ${kids.filter(k=>k.status==='done').length} of ${kids.length} subtasks done`:''}${!isLive(t)?' · closed '+ago(t.updated)+' ago':''}</div>
    ${t.note?`<div class="note md">${md(t.note)}</div>`:''}
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
    <div class="more">${t.space?`<div class="meta">${esc(t.id)} · ${esc(t.space)}</div>`:`<div class="meta">${esc(t.id)}</div>`}${t.note?`<div class="note md">${md(t.note)}</div>`:''}
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
  const sched=allSched.filter(c=>(c.status==='active'||c.status==='pending')&&(c.goes_to?c.goes_to===x.name:c.folder===x.cwd));
  const srow=c=>`<div class="small" style="margin:4px 0"><b>${esc(c.id)}</b> ${esc(c.when_text)}${c.agent?'':' <span class="mut">(its folder)</span>'}: ${esc(c.prompt.length>120?c.prompt.slice(0,119)+'…':c.prompt)}${c.status==='pending'?` <span class="mut">waiting for your approval</span> <button class="ghost small" data-schedok="${esc(c.id)}" data-act="approve">Approve</button><button class="ghost small" data-schedok="${esc(c.id)}" data-act="deny">Deny</button>`:''}</div>`;
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
  if(has('auto_approve')&&!f.elements.auto_approve.disabled&&f.elements.auto_approve.checked!==!!d.auto)patch.auto_approve=f.elements.auto_approve.checked;
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
// A channel maps agents by folder and name, so where several share a folder each is its own pick.
const liveAgents=()=>{const m=new Map();for(const p of livePeers)if(!p.remote&&!m.has(p.cwd+'\t'+p.name))m.set(p.cwd+'\t'+p.name,{cwd:p.cwd,name:p.name,space:p.space});return [...m.values()].sort((a,b)=>a.name.localeCompare(b.name))};
const entryLabel=e=>{if(!e.name)return folderLabel(e.cwd);const a=liveAgents().find(x=>x.cwd===e.cwd&&x.name.toLowerCase()===e.name.toLowerCase());return a?`${a.name} · ${a.space}`:`${e.name} (not running)`};
function entrySelect(attrs,placeholder){return `<select ${attrs}><option value="">${placeholder}</option>${liveAgents().map(o=>`<option value="${esc(o.cwd+'\t'+o.name)}">${esc(o.name+' · '+o.space)}</option>`).join('')}</select>`}
function agentSelect(val,attrs,placeholder){const opts=agentFolders();const extra=val&&!opts.some(o=>o.cwd===val)?[{cwd:val}]:[];
  return `<select ${attrs}><option value="">${placeholder}</option>${[...opts,...extra].map(o=>`<option value="${esc(o.cwd)}" ${o.cwd===val?'selected':''}>${esc(folderLabel(o.cwd))}</option>`).join('')}</select>`}
function channelSelect(val,attrs){const gs=dGuilds||[];
  return `<select ${attrs}><option value="">Pick a channel…</option>${gs.map(g=>`<optgroup label="${esc(g.name)}">${g.channels.map(c=>`<option value="${esc(c.id)}" ${c.id===val?'selected':''}>${g.id==='telegram'?'':'#'}${esc(c.name)}</option>`).join('')}</optgroup>`).join('')}</select>`}
const chanName=id=>{for(const g of dGuilds||[])for(const c of g.channels)if(c.id===id)return (g.id==='telegram'?'':'#')+c.name;return 'channel '+id};
async function loadDiscord(check){
  dState=await api('/api/discord'+(check?'?check=1':''));
  if(dGuilds===null){const r=await api('/api/discord/channels');dGuilds=r.guilds||[]}
  if(!dDraft)dDraft={routes:JSON.parse(JSON.stringify(dState.routes||[])),dm:dState.dm||''};
  renderDiscord()}
const DISCORD_LOGO='<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#fff" d="M20.317 4.37a19.79 19.79 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.865-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.74 19.74 0 0 0 3.677 4.37a.07.07 0 0 0-.032.028C.533 9.046-.32 13.58.099 18.058a.082.082 0 0 0 .031.056 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994a.076.076 0 0 0-.041-.106 13.1 13.1 0 0 1-1.872-.892.077.077 0 0 1-.008-.128c.126-.094.252-.192.372-.291a.074.074 0 0 1 .078-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.009c.12.099.246.198.373.292a.077.077 0 0 1-.007.128 12.3 12.3 0 0 1-1.873.891.077.077 0 0 0-.041.107c.36.698.772 1.363 1.225 1.993a.076.076 0 0 0 .084.029 19.84 19.84 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03zM8.02 15.331c-1.183 0-2.157-1.086-2.157-2.42 0-1.333.956-2.418 2.157-2.418 1.21 0 2.176 1.095 2.157 2.419 0 1.333-.956 2.419-2.157 2.419zm7.975 0c-1.183 0-2.157-1.086-2.157-2.42 0-1.333.955-2.418 2.157-2.418 1.21 0 2.176 1.095 2.157 2.419 0 1.333-.946 2.419-2.157 2.419z"/></svg>',TG_LOGO='<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="#fff" d="M4.6 11.6l13.1-5.05c.6-.22 1.13.15.93 1.06l-2.23 10.5c-.17.75-.6.93-1.23.58l-3.4-2.5-1.64 1.58c-.18.18-.33.33-.68.33l.24-3.46 6.3-5.69c.27-.24-.06-.38-.43-.14l-7.78 4.9-3.35-1.05c-.73-.23-.74-.73.15-1.07z"/></svg>';
// The Channels page's panels: open by default when there's something to do; your own opening or closing one sticks.
const panelSet=new Set();
function panels(want){for(const [id,open] of Object.entries(want)){const el=document.getElementById(id);if(!el||panelSet.has(id))continue;panelSet.add(id);
  let saved=null;try{saved=localStorage.getItem('pl.panel.'+id)}catch(e){}
  el.open=saved===null?open:saved==='1';el.addEventListener('toggle',()=>{try{localStorage.setItem('pl.panel.'+id,el.open?'1':'0')}catch(e){}})}}
const platState=(configured,state,label,user,error)=>!configured?'<span class="pill">Not set up</span>'
  :`<span class="dstate"><span class="dot ${state==='online'?'':'busy'}"></span><b>${esc(label)}</b>${user?` <span class="mut">as ${esc(user)}</span>`:''}${error?` · <span class="err">${esc(error)}</span>`:''}</span>`;
function renderDiscord(){if(!dState)return;const d=dState,invited=(dGuilds||[]).some(g=>g.id!=='telegram'),mapped=(d.routes||[]).some(r=>(r.agents||[]).length);
  const tgChats=(dGuilds||[]).some(g=>g.id==='telegram'&&g.channels.length);
  $('#n-discord').textContent=d.state==='online'?'on':'';
  const label={online:'Online',connecting:'Connecting…',error:'Error',off:'Turned off',unconfigured:'Not set up'}[d.state]||d.state;
  $('#dstatus').innerHTML=platState(d.configured,d.state,label,d.user,d.error);
  const nmap=(d.routes||[]).filter(r=>(r.agents||[]).length).length;$('#mapstatus').innerHTML=nmap?`<span class="pill">${nmap} mapped</span>`:'';
  panels({pdiscord:!d.configured||d.state==='error',pmap:true,popts:false});
  $('#dstep1').classList.toggle('done',d.configured);$('#dstep2').classList.toggle('done',d.configured&&invited);$('#dstep3').classList.toggle('done',(d.routes||[]).some(r=>!r.channel_id.startsWith('tg:')&&(r.agents||[]).length));
  $('#tstep3').classList.toggle('done',(d.routes||[]).some(r=>r.channel_id.startsWith('tg:')&&(r.agents||[]).length));
  $('#dstep1b').innerHTML=d.configured?`<p class="meta">Token <span class="mono">${esc(d.token_hint)}</span> saved. <button class="ghost small" data-dact="forget">Disconnect</button></p>`
   :`<p class="meta">On the <a href="https://discord.com/developers/applications" target="_blank" rel="noreferrer">Developer Portal ↗</a>, click <b>New Application</b> and give it a name. Open <b>Bot</b>, click <b>Reset Token</b>, and copy it. Optional: on the same page, turn on <b>Message Content Intent</b> so you can talk to it without @mentioning it every time.</p>
     <form class="addline" id="dtoken"><input type="password" class="mono grow" autocomplete="off" spellcheck="false" placeholder="Bot token" name="token"><button type="submit" ${dBusy?'disabled':''}>${dBusy?'Checking…':'Connect'}</button></form><p class="small err" id="dtokerr"></p>`;
  $('#dstep2b').innerHTML=!d.configured?'':`<p class="meta">${invited?`In ${dGuilds.filter(g=>g.id!=='telegram').map(g=>esc(g.name)).join(', ')}. `:`Pick your server on Discord's page, then come back here. This step completes on its own. `}
    ${d.invite_url?`<a href="${esc(d.invite_url)}" target="_blank" rel="noreferrer">${invited?'Invite to another server ↗':'<button>Invite the bot ↗</button>'}</a>`:''}</p>`;
  if(!(d.configured&&invited)&&!tgChats){$('#dstep3b').innerHTML='<p class="meta">Set up Discord or Telegram above; the channels and chats its bot can see are mapped to agents here.</p>';$('#dmore').innerHTML='';return}
  const rows=dDraft.routes.map((r,i)=>`<tr>
    <td><div class="addline" style="flex-wrap:nowrap">${r.channel_id?`<span class="mlogo" title="${r.channel_id.startsWith('tg:')?'Telegram':'Discord'}" style="background:${r.channel_id.startsWith('tg:')?'#26A5E4':'#5865F2'}">${r.channel_id.startsWith('tg:')?TG_LOGO:DISCORD_LOGO}</span>`:''}${channelSelect(r.channel_id,`data-droute="${i}" data-dfield="channel_id"`)}</div></td>
    <td><div class="kinds">${(r.agents||[]).map((a,j)=>`<div class="addline small"><b>${esc(entryLabel(a))}</b>
        <label class="small"><input type="checkbox" data-dflag="${i}:${j}:receives" ${a.receives?'checked':''}> Receives</label>
        <label class="small"><input type="checkbox" data-dflag="${i}:${j}:sends" ${a.sends?'checked':''}> Sends</label>
        <button class="ghost small" title="Remove" data-dunagent="${i}:${j}">✕</button></div>`).join('')}
      ${entrySelect(`data-daddagent="${i}"`,'+ add agent')}</div></td>
    <td style="white-space:nowrap"><button class="ghost small" data-dtest="${esc(r.channel_id)}" ${r.channel_id&&(r.channel_id.startsWith('tg:')?tState?.state:d.state)==='online'?'':'disabled'}>Send a test</button> <button class="ghost small" data-dremove="${i}" title="Remove this channel">✕</button></td></tr>`).join('');
  $('#dstep3b').innerHTML=`<p class="meta">Your messages go to the first agent that receives. To reach another that receives, start with its name and a colon: <span class="mono">storefront: how's the build?</span> An agent that sends posts its messages and updates there; any agent answers you where you asked.</p>
    <table class="dmap"><thead><tr><th>Channel</th><th>Agents</th><th></th></tr></thead><tbody>${rows||'<tr><td colspan="3" class="mut small">No channels mapped yet.</td></tr>'}</tbody></table>
    <p class="small mut" style="margin-top:6px">Start a post with <span class="mono">Task:</span> to hand it over as a task, or <span class="mono">Announcement:</span> to tell the receiving agent's whole space.</p>
    <div class="addline" style="margin-top:8px"><button class="ghost" data-dact="addroute">+ Map a channel</button><button class="ghost" data-dact="refresh" title="Look for new servers and channels">↻ Refresh channels</button></div>
    ${d.configured?`<div class="addline" style="margin-top:12px"><label class="small mut">Direct messages to the Discord bot go to</label>${agentSelect(dDraft.dm,'data-ddm','Nobody (ignored)')}</div>`:''}
    <div class="addline" style="margin-top:12px"><button data-dact="save">Save mapping</button><span class="small" id="dsaved"></span></div>`;
  const reading=d.reads_messages?`<p class="meta">Everything you write in a mapped channel goes to its first receiving agent. No @mention needed.</p>`
    :d.message_content_switch?`<p class="meta">The Message Content switch is on in Discord, but the bot connected before it was. <button class="ghost small" data-dact="reconnect">Reconnect now</button></p>`
    :`<p class="meta">${d.message_content_switch===false?'<b>Discord says the Message Content switch is off.</b> ':''}@mention the bot each time, or DM it. To talk without @mentioning, turn on <b>Message Content Intent</b> on the <a href="${d.app_id?`https://discord.com/developers/applications/${esc(d.app_id)}/bot`:'https://discord.com/developers/applications'}" target="_blank" rel="noreferrer">Bot page ↗</a> (under Privileged Gateway Intents) and save. <button class="ghost small" data-dact="check">I turned it on</button></p>`;
  $('#dmore').innerHTML=`<h2>Talking to your agents</h2>${d.configured?reading:''}
    <p class="meta">A reaction on your message shows its progress:</p>
    <div class="legend"><span>👀</span><span>delivered to the agent</span><span>🤔</span><span>thinking</span><span>🛠️</span><span>using tools</span><span>❓</span><span>asked you something</span><span>✅</span><span>answered</span></div>
    <p class="meta" style="margin-top:12px">Agents can message you first, labelled with their name. If an agent isn't running, your message waits up to an hour for it.</p>
    <h2>What agents can send</h2>
    <label class="check"><input type="checkbox" data-dopt="buttons" ${d.buttons?'checked':''}> <span><b>Buttons</b> for choices: tap one and it goes back to the agent as your answer. Off, the choices are a numbered list you reply to.</span></label>
    <label class="check"><input type="checkbox" data-dopt="files_out" ${d.files_out?'checked':''}> <span><b>Files from agents:</b> an agent can attach files from this machine to its messages.</span></label>
    <label class="check"><input type="checkbox" data-dopt="files_in" ${d.files_in?'checked':''}> <span><b>Files to agents:</b> what you attach is saved here, under <span class="mono">~/.peerlink/files</span>, and the agent gets the paths.</span></label>
    <div class="addline" style="margin-top:8px"><label class="small mut" for="dmaxmb">Largest file, either way</label><input id="dmaxmb" type="number" min="1" max="500" step="1" value="${esc(d.max_file_mb)}" style="width:90px" data-dopt="max_file_mb"> <span class="small mut">MB (Discord allows 10 MB without Nitro; Telegram bots get files up to 20 MB)</span></div>
    ${d.configured?`<h2>Discord: people who can talk to agents</h2>
    <p class="meta">Only you can, as the bot's owner (or its team's members). Add other people's user IDs here, separated by commas: they can then message agents and answer buttons as you do. To find one, turn on Developer Mode in Discord, right-click the person and choose Copy User ID.</p>
    <div class="addline"><input id="dusers" class="mono" placeholder="e.g. 1562000000000000000" value="${esc((d.allowed_users||[]).join(', '))}" data-dusers style="flex:1;min-width:0"></div>
    <h2>Discord: bots that can post to agents</h2>
    <p class="meta">Other bots are ignored unless listed here. Add their user IDs, separated by commas. To find one, turn on Developer Mode in Discord, right-click the bot and choose Copy User ID. List only bots you control.</p>
    <div class="addline"><input id="dbots" class="mono" placeholder="e.g. 1562000000000000000" value="${esc((d.trusted_bots||[]).join(', '))}" data-dbots style="flex:1;min-width:0"></div>
    <h2>Discord bot</h2>
    <label class="check"><input type="checkbox" data-dact="enabled" ${d.enabled?'checked':''}> Bot running</label>`:''}`}
document.addEventListener('click',e=>{if(!e.target.closest('[data-stalehide]'))return;const bar=$('#stalebar');
  try{localStorage.setItem('pl.stale.dismissed',bar.dataset.key)}catch(e){}bar.style.display='none'});
document.addEventListener('click',e=>{if(e.target.closest('[data-limresume]'))post('/api/limit/resume',{}).then(()=>refresh(true))});
document.addEventListener('submit',async e=>{if(e.target.id!=='dtoken')return;e.preventDefault();const t=e.target.token.value.trim();if(!t)return;
  dBusy=true;renderDiscord();const r=await api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({token:t})});dBusy=false;
  if(r.error){renderDiscord();$('#dtokerr').textContent=r.error;return}dGuilds=null;dDraft=null;await loadDiscord()});
document.addEventListener('change',e=>{const t=e.target;if(!dDraft)return;
  if(t.dataset.droute!==undefined){dDraft.routes[+t.dataset.droute][t.dataset.dfield]=t.value;renderDiscord()}
  if(t.dataset.daddagent!==undefined&&t.value){const r=dDraft.routes[+t.dataset.daddagent];r.agents=r.agents||[];
    // The first agent added receives and sends; after that, one that only posts here is the likelier want.
    const [cwd,name]=t.value.split('\t');
    if(!r.agents.some(a=>a.cwd===cwd&&(a.name||'').toLowerCase()===name.toLowerCase()))r.agents.push({cwd,name,receives:!r.agents.some(a=>a.receives),sends:true});renderDiscord()}
  if(t.dataset.dflag){const [i,j,f]=t.dataset.dflag.split(':');dDraft.routes[+i].agents[+j][f]=t.checked;renderDiscord()}
  if(t.dataset.ddm!==undefined){dDraft.dm=t.value}
  if(t.dataset.dopt){const v=t.type==='checkbox'?t.checked:Number(t.value);api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({[t.dataset.dopt]:v})}).then(()=>loadDiscord())}
  if(t.dataset.dbots!==undefined)api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({trusted_bots:t.value})}).then(()=>loadDiscord());
  if(t.dataset.dusers!==undefined)api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({allowed_users:t.value})}).then(()=>loadDiscord());
  if(t.dataset.dact==='enabled')api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({enabled:t.checked})}).then(()=>loadDiscord())});
document.addEventListener('click',async e=>{const t=e.target.closest('button');if(!t||!dDraft&&!t.dataset.dact)return;
  if(t.dataset.dremove!==undefined){dDraft.routes.splice(+t.dataset.dremove,1);renderDiscord()}
  if(t.dataset.dunagent){const [i,j]=t.dataset.dunagent.split(':').map(Number);dDraft.routes[i].agents.splice(j,1);renderDiscord()}
  if(t.dataset.dtest){const r=await post('/api/discord/test',{channel_id:t.dataset.dtest});t.textContent=r.error?'Failed':'Sent ✓';if(r.error)alert(r.error)}
  const a=t.dataset.dact;if(!a)return;
  if(a==='addroute'){dDraft.routes.push({channel_id:'',agents:[]});renderDiscord()}
  if(a==='refresh'){const r=await api('/api/discord/channels?fresh=1');dGuilds=r.guilds||[];renderDiscord()}
  if(a==='forget'&&confirm('Disconnect the Discord bot? The channel mapping is kept.')){await api('/api/discord',{method:'DELETE'});dGuilds=null;await loadDiscord()}
  if(a==='reconnect'){await post('/api/discord/reconnect');setTimeout(()=>loadDiscord(),2000)}
  if(a==='check')await loadDiscord(true);
  if(a==='save'){const bad=dDraft.routes.find(r=>!r.channel_id||!(r.agents||[]).some(a=>a.receives||a.sends));if(bad){$('#dsaved').className='small err';$('#dsaved').textContent='Every row needs a channel and an agent that receives or sends.';return}
    await api('/api/discord',{method:'PUT',headers:J,body:JSON.stringify({routes:dDraft.routes,dm:dDraft.dm})});dState=null;dDraft=null;await loadDiscord();
    $('#dsaved').className='small ok';$('#dsaved').textContent='Saved'}});
// -- Telegram: the bot's token, who may talk to it, and the chats it has heard from ------------------
let tState=null,tBusy=false;
async function loadTelegram(){const before=JSON.stringify(Object.keys(tState?.chats||{}));tState=await api('/api/telegram');renderTelegram();
  // A chat the bot heard from for the first time joins the mapping's picker.
  if(before!==JSON.stringify(Object.keys(tState.chats||{}))&&dState){dGuilds=null;loadDiscord()}}
let tShown=null;
function renderTelegram(){const t=tState;if(!t)return;
  const label={online:'Online',connecting:'Connecting…',error:'Error',off:'Turned off',unconfigured:'Not set up'}[t.state]||t.state;
  $('#tgstatus').innerHTML=platState(t.configured,t.state,label,t.user&&'@'+t.user,t.error)+((t.strangers||[]).length?` <span class="pill warn">${t.strangers.length} waiting</span>`:'');
  // The mapping's test buttons follow the bot's state; redrawn only when it changes, not on every poll mid-edit.
  if(dState&&t.state!==tShown){tShown=t.state;renderDiscord()}
  panels({ptelegram:!t.configured||t.state==='error'||(t.strangers||[]).length>0});
  $('#tstep1').classList.toggle('done',t.configured);$('#tstep2').classList.toggle('done',t.configured&&(t.allowed_users||[]).length>0);
  $('#tstep1b').innerHTML=t.configured?`<p class="meta">Token <span class="mono">${esc(t.token_hint)}</span> saved. <label class="small"><input type="checkbox" data-tact="enabled" ${t.enabled?'checked':''}> Bot running</label> <button class="ghost small" data-tact="forget">Disconnect</button></p>`
   :`<p class="meta">In Telegram, message <a href="https://t.me/BotFather" target="_blank" rel="noreferrer">@BotFather ↗</a>, send <span class="mono">/newbot</span>, and pick a name and a username. It answers with a token: paste it here.</p>
     <form class="addline" id="ttoken"><input type="password" class="mono grow" autocomplete="off" spellcheck="false" placeholder="123456789:AAE…" name="token"><button type="submit" ${tBusy?'disabled':''}>${tBusy?'Checking…':'Connect'}</button></form><p class="small err" id="ttokerr"></p>`;
  const strangers=(t.strangers||[]).map(x=>`<div class="addline small"><b>${esc(x.name)}</b>${x.username?` @${esc(x.username)}`:''} <span class="mono mut">${esc(x.id)}</span> <span class="mut">in ${esc(x.chat_name||x.chat)}, ${ago(x.ts)} ago</span>
      <button class="small" data-tallow="${esc(x.id)}">Allow</button><button class="ghost small" data-tdismiss="${esc(x.id)}">Dismiss</button></div>`).join('');
  $('#tstep2b').innerHTML=!t.configured?'':`<p class="meta">Open <a href="https://t.me/${esc(t.user)}" target="_blank" rel="noreferrer">@${esc(t.user)} ↗</a> and send it any message. You show up here; allow yourself, and the bot answers you from then on.</p>
    ${strangers?`<p class="meta"><b>Wrote to the bot, not allowed yet:</b></p>${strangers}`:'<p class="small mut">Nobody waiting. This updates by itself.</p>'}
    <p class="meta" style="margin-top:8px">Allowed (Telegram user ids, separated by commas): they can message agents and answer buttons as you do.</p>
    <div class="addline"><input class="mono" placeholder="e.g. 123456789" value="${esc((t.allowed_users||[]).join(', '))}" data-tusers style="flex:1;min-width:0"></div>`;
  $('#tstep3b').innerHTML=!t.configured?'':`<p class="meta">Every chat the bot has heard from, your private chat with it or a group you added it to, is listed under <b>Chats and agents</b>, below.
    ${t.reads_messages?'In groups it reads every message.':'In groups, Telegram shows the bot only commands, @mentions and replies to it. To talk without @mentioning, send <span class="mono">/setprivacy</span> to @BotFather, choose Disable, then remove the bot from the group and add it again.'}
    Commands work too: <span class="mono">/status</span>, <span class="mono">/inbox</span>, <span class="mono">/peerlink run loop 5m check the deploy</span>.</p>`}
document.addEventListener('submit',async e=>{if(e.target.id!=='ttoken')return;e.preventDefault();const tok=e.target.token.value.trim();if(!tok)return;
  tBusy=true;renderTelegram();const r=await api('/api/telegram',{method:'PUT',headers:J,body:JSON.stringify({token:tok})});tBusy=false;
  if(r.error){renderTelegram();$('#ttokerr').textContent=r.error;return}await loadTelegram()});
document.addEventListener('change',e=>{const t=e.target;
  if(t.dataset.tusers!==undefined)api('/api/telegram',{method:'PUT',headers:J,body:JSON.stringify({allowed_users:t.value})}).then(loadTelegram);
  if(t.dataset.tact==='enabled')api('/api/telegram',{method:'PUT',headers:J,body:JSON.stringify({enabled:t.checked})}).then(loadTelegram)});
document.addEventListener('click',async e=>{const t=e.target.closest('button');if(!t)return;
  if(t.dataset.tallow){await api('/api/telegram',{method:'PUT',headers:J,body:JSON.stringify({allow:t.dataset.tallow})});loadTelegram()}
  if(t.dataset.tdismiss){await api('/api/telegram',{method:'PUT',headers:J,body:JSON.stringify({dismiss:t.dataset.tdismiss})});loadTelegram()}
  if(t.dataset.tact==='forget'&&confirm('Disconnect the Telegram bot? The chat mapping and allowed people are kept.')){await api('/api/telegram',{method:'DELETE'});loadTelegram()}});
// Someone messaging the bot, or a chat it joins, shows up without a reload.
setInterval(()=>{if(location.hash==='#channels'&&tState?.configured&&!document.hidden)loadTelegram()},4000);
// Invited in another tab, or back from the Developer Portal: look again when this tab gets focus.
window.addEventListener('focus',()=>{if(location.hash==='#channels'&&dState?.configured){dGuilds=null;loadDiscord(true)}});
window.addEventListener('hashchange',()=>{if(location.hash==='#discord')location.hash='#channels';if(location.hash==='#channels'){loadDiscord();loadTelegram()}});

async function loadCfg(){cfg=await api('/api/config');const f=$('#cfg');
  panels({swake:true,sctx:true,sapprove:true,ssee:false,sguide:false,slimits:false,sremote:!!(cfg.remotes||[]).length});
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
show();loadCfg().then(()=>refresh(true)).then(()=>{if(location.hash==='#discord')location.hash='#channels';else if(location.hash==='#channels'){loadDiscord();loadTelegram()}}).catch(()=>{misses=1;$('#offline').style.display='block'}).finally(()=>{connect();setTimeout(tick,2000)});
</script></body></html>
"""


PAGE_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Peerlink</title><link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%23c96442'/><path d='M7 11.5h17M19.5 7l4.5 4.5-4.5 4.5M25 20.5H8M12.5 16 8 20.5l4.5 4.5' fill='none' stroke='%23fff' stroke-width='2.8' stroke-linecap='round' stroke-linejoin='round'/></svg>"><style>
:root{--bg:#faf9f7;--fg:#1f1e1c;--mut:#6b6862;--line:#e4e1db}
@media (prefers-color-scheme:dark){:root{--bg:#1c1b19;--fg:#ecebe8;--mut:#9b978f;--line:#34322e}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif}
main{max-width:520px;margin:15vh auto;padding:0 16px}h1{font-size:20px}p{color:var(--mut)}
code{font-family:ui-monospace,Menlo,monospace;font-size:13px;border:1px solid var(--line);border-radius:4px;padding:1px 5px;color:var(--fg)}
</style></head><body><main><h1>{title}</h1><p>{text}</p></main></body></html>"""


if __name__ == "__main__":
    # Windows reads and writes files in its own code page unless told otherwise, and an emoji in a message breaks that.
    if os.name == "nt" and not sys.flags.utf8_mode:
        sys.exit(subprocess.call([sys.executable, "-X", "utf8", *sys.argv]))
    argv = sys.argv[1:]
    name = ALIASES.get(argv[0], argv[0]) if argv else "help"
    if name in ("help", "-h", "--help") or name not in COMMANDS:
        cli_help()
        sys.exit(0 if name in ("help", "-h", "--help") else 2)
    sys.exit(COMMANDS[name][0](argv[1:]) or 0)
