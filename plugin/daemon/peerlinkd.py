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

VERSION = "0.9.21"
# A fingerprint of this file: an agent whose plugin ships different code at the same version replaces the daemon.
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
import chatlink  # noqa: E402  (beside this file; installs nothing)
import discordlink  # noqa: E402
import telegramlink  # noqa: E402
import slacklink  # noqa: E402
from cronlib import cron_next, parse_cron  # noqa: E402,F401
from webui import UI_HTML  # noqa: E402

# A fingerprint of the daemon's code: this file and the modules beside it together.
_h = hashlib.sha256()
for _name in ("peerlinkd.py", "chatlink.py", "discordlink.py", "telegramlink.py", "slacklink.py", "cronlib.py", "webui.py"):
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
RULES_PATH = os.path.join(HOME, "rules.json")
AGENTS_PATH = os.path.join(HOME, "agents.json")
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
- A task keeps a record that everyone above and below it reads with the \
task tool: look there before asking anyone where something stands. Report \
progress and blockers as notes on your task, not as messages. To tell \
everyone working under a task something, put one note on it: it reaches \
every owner beneath at once, so no lead passes it on. Hand on part of a \
task with parent_task.
- When your user gives an instruction meant for good ("from now on", \
"always", "never"), save it with the rule tool, so every agent keeps it \
after a context clear; they approve it first.
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
when you have a channel there; their answer comes back to you. Name the \
task a question is about (task): the answer is kept on it and sent to \
everyone working under it. A question about something easy to undo can \
carry a default you go with if they don't answer in time.
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
    "task_nudge_h": 24,  # an open task with no update for this many hours is raised with its owner, then with whoever asked; 0 is off
    "ask_defaults": True,  # a question to you may name a default the agent goes with when you haven't answered in time
    "ask_default_min_m": 10,  # the shortest wait, in minutes, an agent may give you before its default applies
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
        "rules": [{"id": r["id"], "text": r["text"], "space": r["space"]} for r in rules_for(rec)] if rec else [],
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
    """(space, source, where): the space you put this agent in ('agent': agents sharing a folder can sit in different
    spaces), else the one it was started for ('started'), else the space you put its folder in ('set'), else the
    nearest .peerlink ('file'), else default. A folder's space places the agents opened there you haven't placed."""
    now = time.time()
    cached = rec.get("_space")
    if cached and now - cached[0] < SPACE_CACHE_S:
        return cached[1]
    cwd = rec.get("cwd", "")
    found = (DEFAULT_SPACE, "default", "")
    mine = clean_space((config.get("agent_spaces") or {}).get(rec.get("agent") or ""))
    started = clean_space(config["sessions"].get(rec.get("id") or "", {}).get("started_in"))
    chosen = clean_space((config.get("folder_spaces") or {}).get(cwd))
    path = find_space_file(cwd)
    if mine:
        found = (mine, "agent", cwd)
    elif started:
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


def lead_rec(space):
    """The live session leading a space: the agent you picked, while it runs in the lead's folder, else the folder's
    successor (the one going by the folder's name). None when the space has no lead or nobody runs there."""
    folder = space_lead(space)[0]
    if not folder:
        return None
    picked = agent_session((config.get("space_leads") or {}).get(space, {}).get("agent") or "")
    if picked and picked.get("cwd") == folder:
        return picked
    # Agents sharing the folder may sit in other spaces: the lead is one of those in this one.
    here = [r for r in sessions.values() if r.get("cwd") == folder and is_live(r) and space_of(r)[0] == space]
    named = [r for r in here if display_name(r).lower() == folder_name(folder).lower()]
    return max(named or here, key=lambda r: r["last_seen"]) if here else successor(folder)


def leads(rec):
    """How rec leads its space ('set' or 'only'), or '' when it doesn't."""
    space = space_of(rec)[0]
    folder, how = space_lead(space)
    if not (folder and rec.get("cwd") == folder):
        return ""
    # Agents sharing the lead's folder (ones it started) aren't leads too: the one you picked is, else the one
    # going by the folder's name.
    lead = lead_rec(space)
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
        # A lead you cleared stays cleared until you pick one: the space's last agent doesn't take it back.
        folder, how = space_lead(space)
        if folder == cwd or how == "set" or ((config.get("space_leads") or {}).get(space) or {}).get("how") == "none":
            continue
        moved = any(is_live(r) and r.get("cwd") == folder for r in sessions.values())
        if folder and not moved and now - folder_seen.get(folder, DAEMON_START) < LEAD_GRACE_S:
            continue
        config.setdefault("space_leads", {})[space] = {"cwd": cwd, "how": "only", "ts": time.time()}
        save_config()
        # Told, as a lead you pick is: else it learns it leads only when it's told it no longer does.
        task_message(None, cwd, f"You now lead the {space} space, as the only agent in it. Work sent to the space comes to you: decide which agent owns it, delegate with send_message, and report the outcome to whoever asked.")
        log("lead", space, "->", cwd, "(only agent)")


def set_lead(space, cwd, sid=""):
    """You pick who leads a space (or clear it, cwd ''), in the web UI. `sid` names the agent, for a folder that
    several share; without it the folder's successor leads. Old and new lead are told."""
    space = clean_space(space)
    if not space or space == DEFAULT_SPACE:
        return 400, {"error": "pick a named space; the default space has no lead"}
    with lock:
        pick = sessions.get(sid) if sid else None
        if sid and not (pick and is_live(pick) and pick.get("cwd") == cwd and space_of(pick)[0] == space):
            return 409, {"error": f"that agent isn't running in {tilde_path(cwd)} in the {space} space"}
        if cwd and not any(is_live(r) and r.get("cwd") == cwd and space_of(r)[0] == space for r in sessions.values()):
            return 409, {"error": f"no live agent in {tilde_path(cwd)} is in the {space} space"}
        old, old_rec = space_lead(space)[0], lead_rec(space)
        chosen = config.setdefault("space_leads", {})
        if cwd:
            chosen[space] = {"cwd": cwd, "how": "set", "ts": time.time(), **({"agent": pick["agent"]} if pick and pick.get("agent") else {})}
        else:
            chosen[space] = {"cwd": "", "how": "none", "ts": time.time()}
        save_config()
        new, new_rec = space_lead(space)[0], lead_rec(space)
        # Within one folder the lead can still change hands: from the agent going by its name to another there.
        if old != new or (old_rec and new_rec and old_rec["id"] != new_rec["id"]):
            new_name = display_name(new_rec) if new_rec else folder_name(new)
            if old:
                task_message(None, old, f"You no longer lead the {space} space: {new_name + ' does' if new else 'it has no lead now'}, as your user set in the web UI. Hand anything you were coordinating for the space to {new_name if new else 'the agent that owns it'}.", prefer=old_rec)
            if new:
                parent = space_parent(space)
                task_message(None, new, f"You now lead the {space} space, as your user set in the web UI{' and speak for it in ' + parent if parent else ''}. Work sent to the space comes to you: decide which agent owns it, delegate with send_message, and report the outcome to whoever asked.", prefer=new_rec)
        log("lead", space, "->", (display_name(new_rec) + " in " if new_rec else "") + (new or "(none)"), "set in the web UI")
    return 200, {"space": space, "lead": new, "how": space_lead(space)[1]}


def leads_view():
    """Every space's lead for the web UI, an offline one too, so it stays pinned (greyed) at the top of its space."""
    with lock:
        spaces = {space_of(r)[0] for r in sessions.values() if is_live(r)} | set(config.get("space_leads") or {})
        out = {}
        for space in spaces:
            folder, how = space_lead(space)
            if folder:
                agent = lead_rec(space)
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


def set_agent_space(sid, space):
    """You put one agent in a space (or clear it, back to its folder's), in the web UI. Only that agent moves, and is
    told; it keeps the space through /clear and restarts, since it's kept for the agent, not the session. Unassigned
    (the default space) is a space you can put it in, whatever its folder's is."""
    space = clean_space(space)
    if space in ("unassigned", "none"):
        space = DEFAULT_SPACE
    with lock:
        rec = sessions.get(sid)
        if rec is None or not rec.get("agent"):
            return 404, {"error": "that agent isn't running"}
        chosen = config.setdefault("agent_spaces", {})
        if space:
            chosen[rec["agent"]] = space
        else:
            chosen.pop(rec["agent"], None)
        config["sessions"].get(sid, {}).pop("started_in", None)
        save_config()
    forget_spaces()
    with lock:
        now = space_of(rec)[0]
        where = "out of every space: you're unassigned, with the other agents in no space" if now == DEFAULT_SPACE else f"to the {now} space"
        task_message(None, rec.get("cwd", ""), f"Your user moved you {where}. You now see and message its agents, and they see you. Other agents in your folder stay where they are.", prefer=rec)
        note_lone_leads()
        log("agent space", display_name(rec), "->", space or "(its folder's)")
    return 200, {"id": sid, "space": now}


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
    # The agent it was given to, in the session it runs in now; one from before agents had ids is found by the session.
    mine = agent_session(t.get("owner_agent") or agent_of.get(t["owner_id"]))
    if mine:
        return mine
    named = [r for r in sessions.values() if is_live(r) and r.get("cwd") == t["owner_cwd"] and display_name(r) == t.get("owner")]
    return max(named, key=lambda r: r["last_seen"]) if named else successor(t["owner_cwd"])


def owns_task(rec, t):
    """Whether this agent owns the task (see task_owner). Agents sharing a folder don't share each other's tasks."""
    if not (t["owner_cwd"] and rec.get("cwd") == t["owner_cwd"]):
        return False
    owner = task_owner(t)
    if owner:
        return owner["id"] == rec["id"]
    # Its owner isn't running: the folder's agent stands in, unless the owner is another agent of this folder.
    theirs = t.get("owner_agent") or agent_of.get(t.get("owner_id") or "")
    return theirs not in agents or theirs == rec.get("agent") or not other_agent_stays(theirs, rec)


def owned_task_ids(rec):
    """The open tasks this agent owns, oldest first: what it's on, whatever its summary says."""
    return [t["id"] for t in tasks.values() if t["status"] in TASK_OPEN and owns_task(rec, t)]


def public_peer(rec):
    space, source, where = space_of(rec)
    return {
        "id": rec["id"],
        "agent": rec.get("agent", ""),  # the same through every session this agent has
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
        # When each was handed over and last moved, so a peer list shows a task nobody has touched in days.
        "task_times": {i: [int(tasks[i]["created"]), int(tasks[i]["updated"])] for i in owned_task_ids(rec)[:20]},
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
# Slack's app, likewise: `token` is the bot token (xoxb-), `app_token` the one Socket Mode connects with (xapp-).
# Its conversations map to agents in `routes`, as "slack:<channel id>".
SLACK = {"token": "", "app_token": "", "user": "", "team": "", "enabled": True,
         "allowed_users": [],  # Slack user ids; everyone in the workspace can message the app, so nobody until listed
         "trusted_bots": [],
         "chats": {},  # "slack:<id>" -> name, every conversation the app is in
         "strangers": []}
DISCORD_FILES = os.path.join(HOME, "files", "discord")  # what you attach on Discord, for the agent to read
discord_bot = [None]
telegram_bot = [None]
slack_bot = [None]


def load_discord():
    try:
        with open(DISCORD_PATH) as f:
            discord.update(json.load(f))
    except (OSError, ValueError):
        pass
    discord["routes"] = [clean_route(r) for r in discord.get("routes", []) if r.get("channel_id")]
    for key, defaults in (("telegram", TELEGRAM), ("slack", SLACK)):
        own = discord.setdefault(key, {})
        for k, v in defaults.items():
            own.setdefault(k, json.loads(json.dumps(v)))


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
        bot.on_chat, bot.on_stranger, bot.on_migrate = chat_seen("telegram"), chat_stranger("telegram"), telegram_migrate


def start_slack():
    old = slack_bot[0]
    if old:
        old.close()
    slack_bot[0] = None
    sl = discord["slack"]
    if sl.get("token") and sl.get("app_token") and sl.get("enabled", True):
        bot = slack_bot[0] = slacklink.Bot(sl["token"], sl["app_token"], on_discord, log, on_press)
        bot.allowed_users = set(sl.get("allowed_users") or [])
        bot.trusted_bots = set(sl.get("trusted_bots") or [])
        bot.chats = dict(sl.get("chats") or {})
        bot.on_command = on_slash
        bot.on_chat, bot.on_stranger = chat_seen("slack"), chat_stranger("slack")


def chat_seen(platform):
    """What a platform's bot calls with a chat it has heard from, or one renamed: it's kept for the mapping's picker."""
    def seen(channel_id, title):
        with lock:
            discord[platform]["chats"][channel_id] = title
            save_discord()
    return seen


def chat_stranger(platform):
    """What a platform's bot calls when someone not allowed wrote to it: they're offered on the Channels page, newest
    first, and nothing else happens."""
    def stranger(who):
        log(platform, "ignored a message from", who["name"], who["id"], "(not allowed)")
        with lock:
            own = discord[platform]
            fresh = not any(s["id"] == who["id"] for s in own["strangers"])
            own["strangers"] = [{**who, "ts": time.time()}] + [s for s in own["strangers"] if s["id"] != who["id"]][:9]
            if fresh:
                save_discord()
    return stranger


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
# channel id (chatlink): Discord's are bare digits, Telegram's "tg:<chat id>", Slack's "slack:<channel id>".
ChatError = chatlink.ChatError
CHAT_LINKS = {"discord": discordlink, "telegram": telegramlink, "slack": slacklink}


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
    bot = {"discord": discord_bot, "telegram": telegram_bot, "slack": slack_bot}.get(platform, [None])[0]
    return bot if chat_token(platform) else None


def chat_ready(channel_id=""):
    """Whether the platform a channel is on is set up with its bot running; with no channel, whether any is."""
    if channel_id:
        return chat_bot(channel_id) is not None
    return any(chat_bot(c) is not None for c in ("0", "tg:0", "slack:0"))


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
    """A message from you on Discord, Telegram or Slack: to the chat's first receiver, or to another one it names first."""
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
        if replied:
            user_answered(replied, text, d["author"])
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
    if press["custom_id"].startswith("pu:"):
        return on_spawn_press(press, decide_rule, rules)
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
        user_answered(asked, choice, press["author"])
    log("chat press", msg["id"], press["author"], "->", msg["to_name"], choice)


def chat_target(word):
    """A chat `to` names: "discord", "telegram", "slack", "telegram#family", as (platform name or "" for any, "#channel" or "").
    None when it names no chat. "discord" is any platform, as it was before there were others."""
    head, sep, channel = word.partition("#")
    if head not in ("discord", "telegram", "slack"):
        return None
    return ("" if head == "discord" else chatlink.NAMES[head]), ("#" + channel.lstrip("#") if sep else "")


def chats_matching(chans, target):
    platform, channel = target
    return [r for r in chans if (not platform or r.get("platform") == platform) and (not channel or r["channel"].lower() == channel)]


def send_discord(sender_rec, to, text, parent, hops, options=None, files=None, expects_reply=False):
    """`to: "discord"`, `"telegram"`, `"slack"` or one with "#name", or a reply to a message from a chat. None when it's not for one."""
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
        out += [{"kind": "approval", "id": r["id"], "who": r["requester"].get("name", ""), "since": r["created"],
                 "title": f"Standing rule for {'every space' if r['space'] == '*' else r['space']}",
                 "text": f"{r['text']}\n\nApproving it: every agent in {'every space' if r['space'] == '*' else r['space'] + ' and the spaces inside it'} gets it in its instructions, and keeps it after a context clear."}
                for r in rules.values() if r["status"] == "pending"]
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
        decide = decide_schedule if iid in schedules else decide_rule if iid in rules else decide_spawn
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
            user_answered(m, text, discord_from["author"] if discord_from else USER_NAME)
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
    agent = lead_rec(space)
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
    # A question to you may name the default its agent goes with when you haven't answered in time, said under it.
    try:
        default, wait = ask_default(body, options) if sender_rec and kind == "message" else (None, 0)
    except ValueError as err:
        return 400, {"error": str(err)}
    asks = bool(body.get("expects_reply")) or bool(default)
    shown = text + (f"\n\nNo answer in {when_wait(wait)}, and {display_name(sender_rec)} goes with: {default}" if default else "")

    def to_you(out):
        """What went to you keeps the task it's about, so your answer lands on that task, and its default."""
        code, res = out
        if code == 200 and res.get("id") and kind == "message":
            with lock:
                m = next((x for x in reversed(messages) if x["id"] == res["id"]), None)
                change = {"about_task": about_task(sender_rec, body.get("task"), text)} if m and m.get("expects_reply") else {}
                if m and default:
                    change.update(default=default, default_at=m["ts"] + wait)
                change = {k: v for k, v in change.items() if v}
                if change:
                    m.update(change)
                    append_log({"id": m["id"], **change})
            if default:
                res["note"] = f"they have {when_wait(wait)} to answer; after that you're told to go with your default"
            if change.get("about_task"):
                res["about_task"] = change["about_task"]
        return out

    # A reply to a message from Discord goes back to that message, whether the model wrote "user" or "discord":
    # as a reply in its thread, and recorded with reply_to, so the message no longer reads as unanswered.
    if parent and parent.get("discord") and kind == "message":
        out = send_discord(sender_rec, to, shown, parent, hops, options, files, asks)
        if out is not None:
            return to_you(out)
    # "discord", "telegram", "slack" or one with "#channel", not an agent whose name starts with discord, like discord-voice.
    if sender_rec and (word in USER_ALIASES or (kind == "task" and chat_target(word) and not parent)):
        return to_you(user_send(sender_rec, word, shown, kind, parent_task, asks, hops, options, files))
    out = send_discord(sender_rec, to, shown, parent, hops, options, files, asks)
    if out is not None:
        return to_you(out)
    if default:
        return 400, {"error": "a default is for a question to your user: another agent answers when it reads yours"}
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
            theirs = agents.get(agent_of.get(parent["from"]) or "")
            if not back and theirs and theirs.get("cwd"):
                # Its agent isn't running: the reply waits for that agent (adopt() hands it over), not for another
                # one in its folder.
                target = {"id": parent["from"], "cwd": theirs["cwd"], "name": theirs["name"], "held": True}
            if not back and not theirs:
                back = successor(parent.get("from_cwd") or (sessions.get(parent["from"]) or {}).get("cwd", ""))
            if back:
                target = back
            elif target:
                pass
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
            if not parent_task and sender_rec:
                # Handing on part of a task it owns, an agent names that task more often than it passes parent_task.
                mine = {i for i in re.findall(r"\bt\d+\b", text) if i in tasks and tasks[i]["status"] in TASK_OPEN and owns_task(sender_rec, tasks[i])}
                parent_task = mine.pop() if len(mine) == 1 else ""
            new_task(msg, {"id": sender, "name": msg["from_name"], "cwd": msg["from_cwd"]}, parent_task)
        inboxes.setdefault(target["id"], []).append(msg)
        if sender in sent_times:
            sent_times[sender].append(now)
        record(msg)
    log("msg", msg["id"], msg["from_name"], "->", msg["to_name"])
    out = {"id": msg["id"], "to": msg["to_name"], "status": "queued", "task": msg.get("task")}
    if target.get("held"):
        out["note"] = f"{msg['to_name']} isn't running right now: it waits up to an hour for it"
    if last_word:
        out["note"] = f"delivered as this thread's last reply: it reached its {limit}-reply limit, so neither of you can reply to it"
    return 200, out


def hop_limit(parent):
    """How many replies the thread `parent` is in may run to, and the open task it's about ('' for none). A loop of
    acknowledgements is what max_hops stops; a lead and its worker trading instructions and reports under a task
    still open pass ten replies doing real work, so a task's thread gets three times as many. A thread is a task's
    when it started from the task, or runs between the task's requester and its owner (a lead often starts one
    with a fresh message that names the task)."""
    by_id = {m["id"]: m for m in messages}
    m, seen = parent, 0
    while m and seen < 200:
        if m.get("task"):
            t = tasks.get(m["task"])
            if t and t["status"] in TASK_OPEN:
                return config["max_hops"] * 3, m["task"]
            break
        m, seen = by_id.get(m.get("reply_to") or ""), seen + 1
    pair = {parent.get("from_cwd"), parent.get("to_cwd")}
    for t in tasks.values():
        if t["status"] in TASK_OPEN and "" not in pair and len(pair) == 2 and {t.get("owner_cwd"), (t.get("requester") or {}).get("cwd")} == pair:
            return config["max_hops"] * 3, t["id"]
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
# Who an agent is, apart from the session it's running in. A session id changes with every /clear, resume and
# restart of Claude Code, and a folder can hold several agents, so neither says whose a message, a task or a setting
# is. An agent is a name in a folder; its id stays through every session it has, and through a daemon restart.
agents = {}       # agent id -> {"id", "name", "reg", "cwd", "created", "seen", "sid", summary…}
agent_of = {}     # session id -> agent id, for the sessions an agent has had lately
AGENT_SESSIONS_KEPT = 2000
AGENT_KEPT_S = 60 * 86400


def load_agents():
    try:
        with open(AGENTS_PATH) as f:
            data = json.load(f)
        for a in data.get("agents", []):
            agents[a["id"]] = a
        agent_of.update({k: v for k, v in data.get("sessions", {}).items() if v in agents})
    except (OSError, ValueError, KeyError, AttributeError):
        pass


def save_agents():
    for gone in list(agent_of)[:-AGENT_SESSIONS_KEPT]:
        agent_of.pop(gone)
    try:
        with open(AGENTS_PATH + ".tmp", "w") as f:
            json.dump({"agents": list(agents.values()), "sessions": agent_of}, f)
        os.replace(AGENTS_PATH + ".tmp", AGENTS_PATH)
    except OSError as err:
        log("could not save agents:", err)


def agent_session(aid):
    """The live session an agent is running in now, or None."""
    live = [r for r in sessions.values() if aid and r.get("agent") == aid and is_live(r)]
    return max(live, key=lambda r: r["last_seen"]) if live else None


def agent_for(rec, old=None):
    """Say which agent this session is, at registration: the one it was already, the one the session it continues
    was, else the agent of its name in its folder that isn't running elsewhere, else a new one. Returns the agent
    and the session it last ran in (None when it's new or the same). Called with the lock held."""
    now = time.time()
    aid = rec.get("agent") or agent_of.get(old or "") or agent_of.get(rec["id"])
    if aid not in agents:
        names = {display_name(rec).lower(), str(rec.get("name") or "").lower()} - {""}
        known = [a for a in agents.values() if a.get("cwd") == rec.get("cwd") and {a["name"].lower(), str(a.get("reg") or "").lower()} & names
                 and not (agent_session(a["id"]) and agent_session(a["id"])["id"] != rec["id"])]
        if known:
            aid = max(known, key=lambda a: a.get("seen", 0))["id"]
        else:
            aid = f"a{max([int(i[1:]) for i in agents] or [0]) + 1}"
            agents[aid] = {"id": aid, "created": now}
    a = agents[aid]
    before = a.get("sid") if a.get("sid") != rec["id"] else None
    a.update(name=display_name(rec), reg=rec.get("name") or "", cwd=rec.get("cwd", ""), sid=rec["id"], seen=now)
    rec["agent"] = agent_of[rec["id"]] = aid
    rec.pop("_space", None)  # worked out before it knew which agent it is; an agent's own space now counts
    return a, before


def agent_seen(rec):
    """Keep an agent's record current with its session: a new name, a summary. Called with the lock held."""
    a = agents.get(rec.get("agent") or "")
    if a is None:
        return
    changed = a.get("name") != display_name(rec)
    a["name"], a["seen"] = display_name(rec), time.time()
    if rec.get("summary") and rec["summary"] != a.get("summary"):
        a.update(summary=rec["summary"], headline=rec.get("headline", ""), summary_ts=rec.get("summary_ts") or time.time())
        changed = True
    if changed:
        save_agents()


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
    # The agent that session was, in whatever session it runs now: after a restart of Claude Code, or of the daemon.
    for first in seen:
        rec = agent_session(agent_of.get(first))
        if rec:
            return rec
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


def other_agent_stays(aid, rec):
    """Whether agent `aid`'s queue is its own to come back for, not rec's to take over: both are agents of this folder
    in their own right, rec no newcomer taking the folder over from one that's gone for good."""
    mine = agents.get(rec.get("agent") or "") or {}
    return bool(mine) and mine.get("created", 0) < agents[aid].get("seen", 0) and mine.get("name", "").lower() != agents[aid].get("name", "").lower()


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
        elif sid != old and agent_of.get(sid) in agents and agent_of[sid] != rec.get("agent") and other_agent_stays(agent_of[sid], rec):
            # Queued for another agent that shares this folder: it waits for that agent, not for whoever starts first.
            box = []
        else:
            box = [m for m in inboxes[sid] if sid == old or agent_of.get(sid) == rec.get("agent") or (m.get("to_cwd") and m.get("to_cwd") == rec.get("cwd"))]
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
         "msg": msg["id"], "parent": parent_task, "owner_id": msg.get("to", "") if msg.get("to", "") in sessions else "",
         "owner_agent": agent_of.get(msg.get("to", ""), "")}
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
            t["owner_cwd"], t["owner"], t["owner_id"], t["owner_agent"] = owner, folder_name(owner), "", ""
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
        if note or (status and status != was):
            who = USER_NAME if web else display_name(by)
            task_log(t, who, "closed" if t["status"] in TASK_CLOSED else "progress", (f"{t['status'].replace('_', ' ')}: " if status != was else "") + (note or ""))
            if not web and owns:
                t["blocked"] = ""
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


# A task's record: what was said and decided on it, read by everyone on its tree (whoever asked for it, whoever
# owns it, and the same for the tasks above and below it), so a lead needn't pass each note on by hand.
TASK_LOG_MAX = 60


def task_log(t, who, kind, text):
    """Add an entry to a task's record: progress, blocked, note, decision or closed. Called with the lock held."""
    t.setdefault("log", []).append({"ts": time.time(), "by": who, "kind": kind, "text": str(text)[:2000]})
    del t["log"][:-TASK_LOG_MAX]
    t["updated"] = time.time()


def task_chain(t):
    """The tasks above this one, the topmost first."""
    chain, seen = [], {t["id"]}
    while t.get("parent") in tasks and t["parent"] not in seen:
        t = tasks[t["parent"]]
        seen.add(t["id"])
        chain.insert(0, t)
    return chain


def task_below(t):
    """Every task under this one as (task, depth), each parent before its children."""
    out, todo, seen = [], [(c, 1) for c in tasks.values() if c.get("parent") == t["id"]], {t["id"]}
    while todo:
        c, depth = todo.pop(0)
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        out.append((c, depth))
        todo[0:0] = [(g, depth + 1) for g in tasks.values() if g.get("parent") == c["id"]]
    return out


def task_party(rec, t):
    """What this agent is to the task: 'owner', 'asker' or ''."""
    if owns_task(rec, t):
        return "owner"
    req = t["requester"]
    return "asker" if rec["id"] == req.get("id") or (rec.get("cwd") and rec.get("cwd") == req.get("cwd")) else ""


def on_task_tree(rec, t):
    """Whether this agent asked for or owns the task, or any task above or below it."""
    root = (task_chain(t) or [t])[0]
    return any(task_party(rec, x) for x in [root] + [c for c, _ in task_below(root)])


def task_brief(t, depth=0, log=False):
    entries = t.get("log") or []
    return {"id": t["id"], "title": t["title"], "owner": t["owner"], "requester": t["requester"].get("name", ""), "status": t["status"],
            "created": t["created"], "updated": t["updated"], "note": t.get("note", ""), "blocked": t.get("blocked", ""), "depth": depth,
            "log": list(entries) if log else [e for e in entries if e["kind"] in ("decision", "note")][-5:],
            "last": entries[-1] if entries else None}


def task_view(tid, by=None):
    """A task with its record, the tasks above it (with what was decided on them) and every task below it: for
    an agent on its tree, or for you (by None)."""
    with lock:
        t = tasks.get(tid)
        if t is None:
            return 404, {"error": f"no task {tid}"}
        if by is not None and not on_task_tree(by, t):
            return 403, {"error": f"{tid} isn't yours, and you're on no task above or below it"}
        return 200, {"task": {**task_brief(t, log=True), "text": t["text"]}, "above": [task_brief(x) for x in task_chain(t)],
                     "below": [task_brief(c, d) for c, d in task_below(t)]}


def task_fan_out(t, text, sender=None, skip=()):
    """Send `text` to the owner of task t and of every open task under it, once each; the names it reached.
    Called with the lock held."""
    told, seen = [], set(skip)
    for x in [t] + [c for c, _ in task_below(t) if c["status"] in TASK_OPEN]:
        if x.get("user") or not x.get("owner_cwd"):
            continue
        owner = task_owner(x)
        key = owner["id"] if owner else f"{x['owner_cwd']}|{x['owner']}"
        if key in seen:
            continue
        seen.add(key)
        via = "" if x is t else f"\n(It reaches you through your task {x['id']}: {x['title']})"
        msg = task_message(x, x["owner_cwd"], text + via, sender=sender, prefer=owner)
        msg["wake"] = True
        told.append(display_name(owner) if owner else x["owner"])
    return told


def task_note(tid, body):
    """A note on an open task, kept in its record. From its owner it goes up: progress, or what it's blocked on,
    to whoever asked. From whoever asked for it, or for a task above it, it goes down: to its owner and the owner
    of every open task under it at once."""
    text, blocked = str(body.get("text") or "").strip(), bool(body.get("blocked"))
    if not text:
        return 400, {"error": "text is required: the note"}
    limit = int(config.get("max_message_chars") or 0)
    if limit and len(text) > limit:
        return 413, {"error": f"note is {len(text)} characters, over the {limit} limit: write the details to a file and give its path with a short summary"}
    posts = []
    with lock:
        by = sessions.get(body.get("by") or "")
        if by is None:
            return 403, {"error": "unknown agent; register again"}
        t = tasks.get(tid)
        if t is None:
            return 404, {"error": f"no task {tid}"}
        if t["status"] not in TASK_OPEN:
            return 409, {"error": f"{tid} is {t['status']}: notes go on open tasks"}
        name = display_name(by)
        if owns_task(by, t):
            task_log(t, name, "blocked" if blocked else "progress", text)
            t["blocked"] = text[:300] if blocked else ""
            t["status"] = "in_progress"
            req, told = t["requester"], []
            line = f"Task {tid} ({t['title']}) is blocked. {name}:\n{text}" if blocked else f"Task {tid} ({t['title']}), progress from {name}:\n{text}"
            if req.get("discord"):
                posts.append((req["discord"]["channel_id"], f"**{name}**: {line}", req["discord"]["message_id"]))
                told = [req.get("name") or USER_NAME]
            elif req.get("cwd"):
                asker = continuation(req.get("id") or "", req.get("name") or "", req.get("cwd") or "")
                msg = task_message(t, req["cwd"], line, sender=by, reply_to=t["msg"], prefer=asker)
                # Progress is read at the asker's next turn; only a blocker is worth starting one for.
                msg["wake"], msg["expects_reply"] = blocked or config.get("wake_for", "needed") == "all", blocked
                told = [msg["to_name"]]
            save_tasks()
            out = {"task": tid, "direction": "up", "told": told}
        elif any(task_party(by, x) for x in task_chain(t) + [t]):
            task_log(t, name, "note", text)
            t["blocked"] = ""
            reach = [x for x in [t] + [c for c, _ in task_below(t) if c["status"] in TASK_OPEN]]
            told = task_fan_out(t, f"Note on task {tid} ({t['title']}) from {name}"
                                   + (f", sent to everyone working under it ({len(reach)} tasks)" if len(reach) > 1 else "") + f":\n{text}",
                                sender=by, skip={by["id"]})
            save_tasks()
            out = {"task": tid, "direction": "down", "told": told}
        else:
            return 403, {"error": f"{tid} isn't yours, and you asked for neither it nor a task above it"}
    post_async(posts)
    log("task", tid, "note", out["direction"], "->", ", ".join(out["told"]) or "nobody")
    return 200, out


def about_task(sender_rec, named, text):
    """The task a question to your user is about: the one named, else the single open task on the sender's own
    trees that its text mentions. '' for none. Called with the lock held."""
    if named:
        t = tasks.get(str(named))
        return t["id"] if t and on_task_tree(sender_rec, t) else ""
    ids = {i for i in re.findall(r"\bt\d+\b", text) if i in tasks and tasks[i]["status"] in TASK_OPEN and task_party(sender_rec, tasks[i])}
    return ids.pop() if len(ids) == 1 else ""


def user_answered(asked, answer, who):
    """Your answer to an agent's question about a task is kept on the task as a decision, and goes straight to
    everyone working under it: the agent that asked doesn't have to pass it down. Called with the lock held."""
    t = tasks.get(asked.get("about_task") or "")
    if not t or t["status"] not in TASK_OPEN:
        return
    question = task_title_of(asked.get("text", ""))[:300]
    task_log(t, who, "decision", f"{question}\n→ {answer}")
    task_fan_out(t, f"Decision from {who} on task {t['id']} ({t['title']}), asked by {asked.get('from_name') or 'an agent'}:\n> {question}\n{answer}",
                 skip={asked.get("from", "")})
    save_tasks()


# An open task nobody has touched goes stale unseen, and its owner's summary with it. After task_nudge_h without an
# update its owner is asked to say where it stands or close it; after two asks, or when the owner isn't running,
# whoever asked for it is told, so it's cancelled or handed on.
NUDGE_EVERY_S = 600
last_nudge = [0.0]


def nudge_stale_tasks(now):
    """Called with the lock held."""
    every = float(config.get("task_nudge_h") or 0) * 3600
    if not every or usage_limit or now - last_nudge[0] < NUDGE_EVERY_S:
        return
    last_nudge[0] = now
    changed = False
    for t in [x for x in tasks.values() if x["status"] in TASK_OPEN and not x.get("user") and x.get("owner_cwd")]:
        if now - t["updated"] < every or now - t.get("nudged", 0) < every:
            continue
        if t.get("nudged_for") != t["updated"]:
            t["nudged_for"], t["nudges"] = t["updated"], 0
        quiet = when_wait(now - t["updated"])
        owner, req = task_owner(t), t["requester"]
        if owner and t["nudges"] < 2:
            msg = task_message(t, t["owner_cwd"], f"Task {t['id']} ({t['title']}) is yours and hasn't moved in {quiet}. Put a note on it saying where "
                               "it stands (the task tool), or close it with update_task: done, or failed with why.", prefer=owner)
            msg["wake"] = True
        elif req.get("cwd") and not t.get("escalated_for") == t["updated"]:
            t["escalated_for"] = t["updated"]
            why = f"{t['owner']} was asked twice and hasn't answered" if owner else f"{t['owner']} isn't running"
            asker = continuation(req.get("id") or "", req.get("name") or "", req.get("cwd") or "")
            msg = task_message(t, req["cwd"], f"Task {t['id']} ({t['title']}), which you gave {t['owner']}, hasn't moved in {quiet}, and {why}. "
                               "Cancel it with update_task, or put a note on it with what's needed.", prefer=asker)
            msg["wake"] = True
        else:
            continue
        t["nudged"], t["nudges"] = now, t["nudges"] + 1
        changed = True
        log("task", t["id"], "nudged", t["nudges"], "quiet", quiet)
    if changed:
        save_tasks()


# Standing rules: what you told your agents once and meant for good ("never merge on Fridays"). A rule belongs to a
# space and reaches every agent in it and in the spaces inside it, in its instructions, so a cleared context still
# has it. You write them in the web UI; one an agent proposes waits for your approval, whatever auto-approve says.
rules = {}
RULE_MAX = 600


def load_rules():
    try:
        with open(RULES_PATH) as f:
            for x in json.load(f):
                rules[x["id"]] = x
    except (OSError, ValueError, KeyError):
        pass


def save_rules():
    with open(RULES_PATH + ".tmp", "w") as f:
        json.dump(list(rules.values()), f, indent=1)
    os.replace(RULES_PATH + ".tmp", RULES_PATH)


def rules_for(rec):
    """The active rules that reach this agent: those of every space ("*"), its own, and each space its own is inside."""
    space, chain = space_of(rec)[0], {"*"}
    while space and space not in chain:
        chain.add(space)
        space = space_parent(space)
    return [r for r in sorted(rules.values(), key=lambda r: r["created"]) if r["status"] == "active" and r["space"] in chain]


def rule_view(r):
    return {k: r[k] for k in ("id", "space", "text", "status", "created", "updated")} | {"by": r["requester"].get("name", "")}


def add_rule(body, by=None):
    """A rule from you (by None): active at once. From an agent: for its own space, pending until you approve."""
    text = " ".join(str(body.get("text") or "").split())
    if not text:
        return 400, {"error": "text is required: the rule, in a sentence"}
    if len(text) > RULE_MAX:
        return 400, {"error": f"a rule is a sentence or two: this is {len(text)} characters, over {RULE_MAX}"}
    now = time.time()
    with lock:
        space = space_of(by)[0] if by else "*" if str(body.get("space") or "*") == "*" else clean_space(body.get("space")) or "*"
        if any(r["text"].lower() == text.lower() and r["space"] == space and r["status"] in ("active", "pending") for r in rules.values()):
            return 409, {"error": "that rule is already there" + ("" if not by else ", or waiting for your user")}
        n = max([int(i[1:]) for i in rules] or [0]) + 1
        r = {"id": f"r{n}", "space": space, "text": text, "status": "pending" if by else "active", "created": now, "updated": now,
             "requester": {"id": by["id"], "name": display_name(by), "cwd": by.get("cwd", "")} if by else {"name": USER_NAME, "cwd": ""}}
        rules[r["id"]] = r
        save_rules()
        channel = approval_channel(by) if by else ""
    log("rule", r["id"], r["status"], space, r["requester"]["name"])
    if r["status"] == "pending" and channel and chat_ready(channel):
        try:
            sent = chat_post(channel, f"**{r['requester']['name']}** asks to keep this as a standing rule for every agent in **{space}**:\n> {text}",
                             None, choices(["Approve", "Deny"], f"pu:{r['id']}"))
            with lock:
                r["discord_out"] = {"channel_id": channel, "message_id": sent}
                save_rules()
        except ChatError as err:
            log("discord", "rule post failed:", err)
    return 200, {"rule": rule_view(r)}


def rule_told(r, text):
    """Tell the agent that proposed a rule what became of it. Called with the lock held."""
    if r["requester"].get("cwd"):
        task_message(None, r["requester"]["cwd"], text)


def decide_rule(rid, approve, via):
    with lock:
        r = rules.get(rid)
        if r is None:
            return 404, {"error": f"no rule {rid}"}
        if r["status"] != "pending":
            return 409, {"error": f"{rid} is already {r['status']}"}
        r["status"], r["updated"] = ("active" if approve else "denied"), time.time()
        rule_told(r, f"Your user {'approved' if approve else 'denied'} rule {rid}" + (f": every agent in {r['space']} has it from its next prompt." if approve else "."))
        save_rules()
    close_buttons(r)
    log("rule", rid, r["status"], "via", via)
    return 200, {"rule": rule_view(r)}


def edit_rule(rid, body):
    """You change a rule's text or space, turn it off or on, or delete it."""
    with lock:
        r = rules.get(rid)
        if r is None:
            return 404, {"error": f"no rule {rid}"}
        if body.get("delete"):
            rules.pop(rid)
            save_rules()
            close_buttons(r)
            return 200, {"deleted": rid}
        text = " ".join(str(body["text"]).split()) if "text" in body else r["text"]
        if not text or len(text) > RULE_MAX:
            return 400, {"error": f"a rule is a sentence or two, up to {RULE_MAX} characters"}
        status = body.get("status", r["status"])
        if status not in ("active", "off"):
            return 400, {"error": "a rule is active or off"}
        space = ("*" if str(body["space"]) == "*" else clean_space(body["space"]) or r["space"]) if "space" in body else r["space"]
        r.update({"text": text, "status": status, "space": space, "updated": time.time()})
        save_rules()
    close_buttons(r)
    return 200, {"rule": rule_view(r)}


# A question to you may carry a default: what the agent goes with when you haven't answered in time. You can turn
# that off, and set the shortest wait an agent may give you, under Settings.
def parse_wait(raw):
    """Seconds in "30m", "2h", "1d" or "in 45m"; ValueError when it's none of those."""
    m = re.fullmatch(r"\s*(?:in\s+)?(\d+)\s*(m|min|mins|minutes?|h|hours?|d|days?)\s*", str(raw or ""), re.I)
    if not m:
        raise ValueError('default_after is a wait like "30m", "2h" or "1d"')
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)[0].lower()]


def ask_default(body, options):
    """(default, seconds) a question carries, (None, 0) when it has none. Raises ValueError saying what's wrong."""
    default = str(body.get("default") or "").strip()
    if not default:
        return None, 0
    if not config.get("ask_defaults", True):
        raise ValueError("your user turned defaults off: ask without one, and wait for their answer")
    if options and default not in options:
        raise ValueError(f"default has to be one of the options: {', '.join(options)}")
    wait = parse_wait(body.get("default_after") or "30m")
    least = int(config.get("ask_default_min_m") or 0)
    if wait < least * 60:
        raise ValueError(f"default_after is at least {least} minutes: your user set that")
    return default[:300], min(wait, 7 * 86400)


def ask_settled(m):
    """Whether you've dealt with a question: answered it, dismissed it, or written to its agent or channel since.
    Called with the lock held."""
    if m.get("handled") or m.get("inbox_done"):
        return True
    for x in reversed(messages):
        if x["ts"] < m["ts"]:
            break
        if x.get("reply_to") == m["id"]:
            return True
        if str(x.get("from", "")).startswith("discord:") and (x.get("to_cwd") == m.get("from_cwd") or x["from"] == m.get("to")):
            return True
    return False


def apply_defaults(now):
    """Questions whose wait has run out unanswered: the agent is told to go with its default, the question leaves
    your Inbox, and its chat post says what happened. Called with the lock held."""
    for m in [x for x in messages if x.get("default_at") and x["default_at"] <= now and not x.get("default_done")]:
        m["default_done"] = True
        change = {"id": m["id"], "default_done": True}
        if not ask_settled(m):
            waited = when_wait(m["default_at"] - m["ts"])
            answer = {"id": f"m{next_msg[0]}", "ts": now, "from": "peerlink", "from_name": "peerlink", "from_host": HOSTNAME, "from_cwd": "",
                      "to_cwd": m.get("from_cwd", ""), "from_space": "", "to_space": m.get("from_space", ""),
                      "text": f"No answer from your user in {waited}, so go with your default: {m['default']}\nThey were told; if they answer later, their answer wins.",
                      "expects_reply": False, "reply_to": m["id"], "hops": 0, "status": "queued", "v": 2, "wake": True}
            next_msg[0] += 1
            queue_for_folder(m.get("from_cwd", ""), answer, continuation(m.get("from", ""), m.get("from_name", ""), m.get("from_cwd", "")))
            m["inbox_done"] = change["inbox_done"] = True
            user_answered(m, f"{m['default']} (the default: no answer in {waited})", "default")
            close_buttons(m)
            out = m.get("discord_out") or {}
            if out.get("message_id"):
                post_async([(out["channel_id"], f"No answer in {waited}, so **{m.get('from_name') or 'the agent'}** is going with its default: {m['default']}\nTell it if that's wrong.", out["message_id"])])
            log("default", m["id"], m.get("from_name", ""), "->", m["default"][:80])
        append_log(change)


def when_wait(s):
    if s >= 36 * 3600:
        return f"{int(round(s / 86400.0))} days"
    if s >= 2 * 3600 and s % 3600:
        return f"{int(round(s / 3600.0))} hours"
    s = int(round(s / 60.0)) * 60
    return f"{s // 86400} day{'s' if s >= 172800 else ''}" if s >= 86400 and s % 86400 == 0 else f"{s // 3600} hour{'s' if s >= 7200 else ''}" if s >= 3600 and s % 3600 == 0 else f"{max(1, s // 60)} min"


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
# parse_cron and cron_next are in cronlib.py, beside this file.


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
    at = dt.datetime.fromtimestamp(c.get("at") or 0)
    return f"once, {at:%a %b} {at.day} {at:%H:%M}"  # not %-d: Windows has no such format


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


PLAYBOOK_MAX = 16000


def playbook_path(raw, base):
    """A schedule's playbook file as an absolute path, from what was given (~ and paths relative to `base` work);
    '' when none was."""
    raw = str(raw or "").strip()
    if not raw:
        return ""
    path = os.path.expanduser(raw)
    if not os.path.isabs(path):
        if not base:
            raise ValueError(f"prompt_file {raw}: give the whole path")
        path = os.path.join(base, path)
    path = os.path.normpath(path)
    if not os.path.isfile(path):
        raise ValueError(f"prompt_file {raw}: no such file")
    return path


def read_playbook(path):
    """The playbook as it reads now, and why not ('') when it can't be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(PLAYBOOK_MAX + 1)
    except OSError as err:
        return "", err.strerror or str(err)
    if len(text) > PLAYBOOK_MAX:
        text = text[:PLAYBOOK_MAX] + f"\n\n(cut at {PLAYBOOK_MAX} characters: read the rest in the file)"
    return text.strip(), ""


def schedule_shown(c):
    """A schedule's prompt as your approval post shows it, naming the playbook it reads."""
    pf = c.get("prompt_file")
    return c["prompt"][:600] + (f"\nIt reads the playbook `{tilde_path(pf)}` fresh at each run." if pf else "")


def create_schedule(body, by=None):
    """A schedule from an agent (`by`) or from you (None). An agent's that may start one waits for your approval.
    It goes to an agent: that one wherever it runs, else a new one started in the folder it was last in."""
    now = time.time()
    prompt = str(body.get("prompt") or "").strip()
    start = body.get("start") is not False
    try:
        folder, name = schedule_target(body, by)
        # A playbook is read at each run, so rules edited in it reach the next run, as CLAUDE.md's do.
        prompt_file = playbook_path(body.get("prompt_file"), (by or {}).get("cwd") or folder)
        if prompt_file and not prompt:
            prompt = f"Follow the playbook {tilde_path(prompt_file)}."
        if not prompt:
            raise ValueError("prompt or prompt_file is required: what the agent should do when it runs")
        cron, at, next_run = schedule_when(body, now)
    except ValueError as err:
        return 400, {"error": str(err)}
    with lock:
        n = max([int(i[1:]) for i in schedules] or [0]) + 1
        c = {"id": f"c{n}", "folder": folder, "agent": name, "prompt": prompt[:8000], "prompt_file": prompt_file, "cron": cron, "at": at,
             "start": start, "next_run": next_run, "last_run": None, "runs": 0, "note": "",
             "requester": {"id": by["id"], "name": display_name(by), "cwd": by.get("cwd", "")} if by else {"name": USER_NAME, "cwd": ""},
             "status": "pending" if by and start and not auto_approved(by) else "active", "created": now, "updated": now}
        schedules[c["id"]] = c
        save_schedules()
        channel = approval_channel(by) if by else ""
    log("schedule", c["id"], c["status"], c["agent"] or tilde_path(folder), when_text(c))
    if by and start and c["status"] == "active" and channel and chat_ready(channel):
        post_async([(channel, f"**{c['requester']['name']}** scheduled `{tilde_path(folder)}` ({when_text(c)}) without asking (auto-approve is on): {start_note(c)}.\n{schedule_shown(c)}", None)])
    if c["status"] == "pending" and channel and chat_ready(channel):
        try:
            sent = chat_post(channel,
                             f"**{c['requester']['name']}** asks to schedule `{tilde_path(folder)}` ({when_text(c)}): {start_note(c)}.\n{schedule_shown(c)}",
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


def within(folder, parent):
    """Whether `folder` is `parent` or inside it."""
    folder, parent = os.path.normpath(folder or ""), os.path.normpath(parent or "")
    return bool(folder and parent) and (folder == parent or folder.startswith(parent.rstrip(os.sep) + os.sep))


def cancel_schedule(cid, by=None):
    """You cancel any schedule; an agent, one it made, or one for its folder or a folder inside it: a lead above a
    folder no agent runs in any more can still stop what's scheduled there."""
    with lock:
        c = schedules.get(cid)
        if c is None:
            return 404, {"error": f"no schedule {cid}"}
        if (by and by["id"] != c["requester"].get("id") and by.get("cwd") != c["requester"].get("cwd")
                and not within(c["folder"], by.get("cwd"))):
            return 403, {"error": f"{cid} isn't yours, and {tilde_path(c['folder'])} isn't your folder or inside it"}
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
            prompt_file = (playbook_path(body["prompt_file"], c["requester"].get("cwd") or folder) if "prompt_file" in body
                           else c.get("prompt_file", ""))
            if prompt_file and not prompt:
                prompt = f"Follow the playbook {tilde_path(prompt_file)}."
            if not prompt:
                raise ValueError("prompt or prompt_file is required: what the agent should do when it runs")
            cron, at, next_run = schedule_when(body, now) if "cron" in body or "at" in body else (c["cron"], c.get("at"), c.get("next_run"))
        except ValueError as err:
            return 400, {"error": str(err)}
        c.update({"folder": folder, "agent": name, "prompt": prompt[:8000], "prompt_file": prompt_file, "cron": cron, "at": at,
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
    text = f"Scheduled ({when_text(c)}; cancel with the schedule tool, id {c['id']}): {c['prompt']}"
    if c.get("prompt_file"):
        book, err = read_playbook(c["prompt_file"])
        text += (f"\n\nThe playbook {tilde_path(c['prompt_file'])}, as it reads now:\n\n{book}" if book
                 else f"\n\nIts playbook {tilde_path(c['prompt_file'])} couldn't be read ({err or 'it is empty'}): tell whoever keeps it.")
        # Said once: the same failure on every run is noise until it changes.
        if (err or not book) and c.get("book_err") != (err or "empty"):
            schedule_told(c, f"Schedule {c['id']} ran, but its playbook {tilde_path(c['prompt_file'])} couldn't be read ({err or 'it is empty'}).")
        c["book_err"] = (err or "empty") if not book else ""
    msg = {"id": f"m{next_msg[0]}", "ts": now, "from": "peerlink", "from_name": f"schedule {c['id']}", "from_host": HOSTNAME,
           "from_cwd": "", "to_cwd": c["folder"], "from_space": "", "to_space": "",
           "text": text, "expects_reply": False, "reply_to": None, "hops": 0, "status": "queued", "v": 2}
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


def ack(sid, ids, holding=None):
    """The plugin says these messages reached its session: stop handing them out. Ones it says it's `holding` (an
    announcement for its next turn, a message that needs no turn of its own) it still has: they aren't handed out
    again while it keeps saying so, only if it stops (a reload lost them)."""
    with lock:
        mine = handed.get(sid, {})
        for mid in ids if isinstance(ids, list) else []:
            mine.pop(str(mid), None)
        now = time.time()
        for mid in holding if isinstance(holding, list) else []:
            if str(mid) in mine:
                mine[str(mid)] = (mine[str(mid)][0], now)


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
        for aid in [a["id"] for a in agents.values() if now - a.get("seen", now) > AGENT_KEPT_S and not agent_session(a["id"])]:
            agents.pop(aid)
            for sid in [k for k, v in agent_of.items() if v == aid]:
                agent_of.pop(sid)
        save_agents()
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
        for r in [x for x in rules.values() if x["status"] == "pending" and now - x["created"] > SPAWN_TTL_S]:
            r["status"], r["updated"] = "expired", now
            rule_told(r, f"Your rule {r['id']} expired: your user didn't approve it within a day.")
            lapse(r, f"keep a standing rule for {r['space']}", rule_told)
            save_rules()
        apply_defaults(now)
        nudge_stale_tasks(now)
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
            # Telegram's and Slack's chats come as one more group each, the ones their bots know of.
            chats = [{"id": key, "name": chatlink.NAMES[key], "channels": sorted(({"id": k, "name": v} for k, v in discord[key]["chats"].items()), key=lambda c: c["name"].lower())}
                     for key in ("telegram", "slack") if discord[key].get("token")]
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

    def slack_api(self, rest, method):
        sl, bot = discord["slack"], slack_bot[0]
        if rest == [] and method == "GET":
            return self.reply(200, {
                "configured": bool(sl.get("token") and sl.get("app_token")), "token_hint": slacklink.hint(sl.get("token", "")),
                "app_token_hint": slacklink.hint(sl.get("app_token", "")), "user": sl.get("user", ""), "team": sl.get("team", ""),
                "enabled": sl.get("enabled", True), "state": bot.state if bot else ("off" if sl.get("token") else "unconfigured"),
                "error": bot.error if bot else "", "allowed_users": sl.get("allowed_users", []), "trusted_bots": sl.get("trusted_bots", []),
                "strangers": sl.get("strangers", []), "chats": sl.get("chats", {}), "manifest_url": slacklink.manifest_url(),
            })
        if rest == [] and method == "PUT":
            b = self.body()
            restart = False
            with lock:
                if "token" in b or "app_token" in b:
                    # Either token alone replaces that one; the pair is checked with Slack before it's kept.
                    bot_token = str(b.get("token") or sl.get("token") or "").strip()
                    app_token = str(b.get("app_token") or sl.get("app_token") or "").strip()
                    try:
                        info = slacklink.inspect_tokens(bot_token, app_token)
                    except slacklink.SlackError as err:
                        return self.reply(400, {"error": str(err)})
                    sl.update({"token": bot_token, "app_token": app_token, "user": info["user"], "team": info["team"], "enabled": True})
                    restart = True
                if "enabled" in b:
                    sl["enabled"], restart = bool(b["enabled"]), True
                for k in ("allowed_users", "trusted_bots"):
                    if k in b:
                        raw = b[k]
                        sl[k] = list(dict.fromkeys(re.findall(r"\b[UWB][A-Z0-9]{5,20}\b", raw if isinstance(raw, str) else " ".join(map(str, raw or [])))))
                if b.get("allow"):
                    # Pairing: someone who wrote to the app, allowed from the list of who tried.
                    who = str(b["allow"])
                    sl["allowed_users"] = list(dict.fromkeys(sl["allowed_users"] + [who]))
                    sl["strangers"] = [s for s in sl["strangers"] if s["id"] != who]
                if b.get("dismiss"):
                    sl["strangers"] = [s for s in sl["strangers"] if s["id"] != str(b["dismiss"])]
                if bot:
                    bot.allowed_users, bot.trusted_bots = set(sl["allowed_users"]), set(sl["trusted_bots"])
                save_discord()
            if restart:
                start_slack()
            return self.reply(200, {"ok": True})
        if rest == [] and method == "DELETE":
            with lock:
                sl.update({"token": "", "app_token": "", "user": "", "team": ""})
                save_discord()
            start_slack()
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
            seen = [p for p in peers if p["id"] != exclude and (mine is None or peer_reach(p) & mine)]
            # A space inside the asker's shows only its lead; the lead lists who works under it, so an agent there that
            # the asker can't reach doesn't pass for one that isn't running.
            if mine is not None:
                for p in seen:
                    if p.get("leads") and p.get("space") not in mine:
                        p["inside"] = [o["name"] for o in peers if o["id"] != p["id"] and o.get("space") == p["space"]]
            return self.reply(200, {"host": HOSTNAME, "space": space, "peers": seen, "leads": leads_view()})

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
                if not rec.get("summary") and known.get("summary") and known.get("name") == display_name(rec) and not agent_of.get(b.get("succeeds") or sid):
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
                # Which agent this is, whatever session it's in: what you set for it (a role, a nickname, muting) and
                # its summary come with it into a session that says nothing of the last one.
                agent, before = agent_for(rec, old)
                if before and before in config["sessions"] and sid not in config["sessions"]:
                    config["sessions"][sid] = dict(config["sessions"][before])
                    save_config()
                if not rec.get("summary") and agent.get("summary"):
                    rec.update(summary=agent["summary"], headline=agent.get("headline", ""), summary_ts=agent.get("summary_ts") or time.time())
                save_agents()
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
                agent_seen(rec)
                cfg = client_config(sid)
            ack(sid, b.get("ack"), b.get("holding"))
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
            b = self.body()
            code, out = set_lead(rest[1], str(b.get("cwd") or ""), str(b.get("id") or ""))
            return self.reply(code, out)
        if rest == ["folders", "space"] and method == "POST":
            if not browser or self.key_ok():
                return self.reply(403, {"error": "a folder's space is set in the web UI"})
            b = self.body()
            # With an agent's id, that agent alone; without, the folder (agents opened there later, and any not placed).
            code, out = set_agent_space(str(b["id"]), b.get("space")) if b.get("id") else set_folder_space(str(b.get("cwd") or ""), b.get("space"))
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
        if rest == ["rules"] and method == "GET":
            with lock:
                by = sessions.get(q.get("by", [""])[0])
                shown = rules_for(by) + [r for r in rules.values() if r["status"] == "pending" and r["requester"].get("id") == by["id"]] if by else sorted(rules.values(), key=lambda r: r["created"])
                return self.reply(200, {"rules": [rule_view(r) for r in shown]})
        if rest == ["rules"] and method == "POST":
            b = self.body()
            if browser and not self.key_ok():
                return self.reply(*add_rule(b))
            with lock:
                by = sessions.get(str(b.get("by") or ""))
            if by is None:
                return self.reply(403, {"error": "unknown agent; register again"})
            return self.reply(*add_rule(b, by))
        # Only you change or drop a rule: an agent proposes one, and asks you to change one.
        if len(rest) == 2 and rest[0] == "rules" and method == "POST":
            if not browser:
                return self.reply(403, {"error": "only your user changes or removes a rule, from the web UI: ask them"})
            return self.reply(*edit_rule(rest[1], self.body()))
        if len(rest) == 2 and rest[0] == "tasks" and method == "GET":
            with lock:
                by = sessions.get(q.get("by", [""])[0])
            if by is None and not (browser and not self.key_ok()):
                return self.reply(403, {"error": "unknown agent; register again"})
            return self.reply(*task_view(rest[1], by))
        if len(rest) == 3 and rest[0] == "tasks" and rest[2] == "note" and method == "POST":
            return self.reply(*task_note(rest[1], self.body()))
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
        if rest and rest[0] == "slack":
            if method != "GET" and (not browser or self.key_ok()):
                return self.reply(403, {"error": "Slack is set up from the web UI"})
            return self.slack_api(rest[1:], method)

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
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the caller left
        except Exception as err:
            # A malformed request (?limit=abc) is the caller's; anything else is a fault here. Both say what and where
            # in the log, so a page that can't load on one machine leaves a reason behind.
            import traceback
            bad = isinstance(err, (ValueError, TypeError, KeyError, AttributeError))
            where = traceback.extract_tb(err.__traceback__)[-1]
            log("request failed:", method, self.path.split("?")[0], f"{type(err).__name__}: {err}", f"({os.path.basename(where.filename)}:{where.lineno} in {where.name})")
            try:
                self.reply(400 if bad else 500, {"error": f"{'bad request' if bad else 'the daemon failed'}: {type(err).__name__}: {err} ({os.path.basename(where.filename)}:{where.lineno})"})
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
    load_rules()
    load_agents()
    load_dirs()
    file_pending_issues()
    load_discord()
    threading.Thread(target=reaction_worker, name="discord-reactions", daemon=True).start()
    clean_stale_reactions()
    start_discord()
    start_telegram()
    start_slack()
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

# The web UI page itself is in webui.py, beside this file.


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
