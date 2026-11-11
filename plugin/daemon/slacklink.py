"""peerlink's Slack app: talk to your agents from Slack, in a channel the app is in or a direct message with it.

Standard library only, like the rest of the daemon: the Web API over urllib, and events over Socket Mode (a
websocket Slack opens for the app), so nothing needs a public URL.

**Setup is an app and two tokens.** The Channels page links to Slack with the app's manifest filled in (scopes, the
message events, the /peerlink command, Socket Mode). You create the app, make an app-level token (xapp-…, scope
connections:write), install it to the workspace for the bot token (xoxb-…), and paste both.

**Who may talk.** Nobody, until you allow them: everyone in the workspace can message the app. A message from
someone not on the list gets no answer and reaches no agent; the Channels page shows who tried, with an Allow
button. So you pair by messaging the app, then allowing yourself.

**Reading messages.** The app sees every message in a channel it was invited to (/invite @peerlink) and in its
direct messages.

Chats are `slack:<channel id>` to the daemon. A message's id is its `ts`, or `ts@thread_ts` for one inside a thread,
so an answer to it goes into that thread. Both tokens live in discord.json, 0600, and are never echoed back.
"""
import json
import os
import queue
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from chatlink import ChatError
from discordlink import WebSocket
from telegramlink import SUBS, chunks, command_options

API = os.environ.get("PEERLINK_SLACK_API", "https://slack.com/api")
PART = 2900          # a section block holds 3000 characters
PING_S = 30          # our ping to Slack, so a dead socket is noticed
QUIET_S = 75         # no frame for this long (not even a pong): the socket is dead
CHATS_EVERY_S = 60   # how often the conversations the app is in are listed again
REACTIONS = {"👀": "eyes", "🤔": "thinking_face", "🛠️": "hammer_and_wrench", "💤": "zzz", "❓": "question",
             "✅": "white_check_mark", "❌": "x"}
SCOPES = ["chat:write", "commands", "channels:history", "groups:history", "im:history", "mpim:history",
          "channels:read", "groups:read", "im:read", "mpim:read", "users:read", "reactions:write", "files:read", "files:write"]
MANIFEST = {
    "display_information": {"name": "peerlink", "description": "Talk to your Claude Code agents"},
    "features": {
        "bot_user": {"display_name": "peerlink", "always_online": True},
        "app_home": {"messages_tab_enabled": True, "messages_tab_read_only_enabled": False},
        "slash_commands": [{"command": "/peerlink", "description": "peerlink: your agents",
                            "usage_hint": "status | inbox | peers | spaces | schedules | run … | clear … | resume", "should_escape": False}],
    },
    "oauth_config": {"scopes": {"bot": SCOPES}},
    "settings": {
        "event_subscriptions": {"bot_events": ["message.channels", "message.groups", "message.im", "message.mpim"]},
        "interactivity": {"is_enabled": True},
        "socket_mode_enabled": True, "org_deploy_enabled": False, "token_rotation_enabled": False,
    },
}
# What a refusal means for whoever set the app up; anything else is shown as Slack's own word for it.
WHY = {
    "invalid_auth": "Slack rejected that token; copy it again",
    "not_authed": "Slack rejected that token; copy it again",
    "token_revoked": "that token was revoked; install the app again and paste the new one",
    "account_inactive": "that token's app was removed from the workspace",
    "not_in_channel": "the app isn't in that channel: type /invite @peerlink there",
    "channel_not_found": "the app can't see that channel: type /invite @peerlink there",
    "is_archived": "that channel is archived",
    "missing_scope": "the app lacks a permission; create it from the Channels page's link, or add the scope and reinstall",
}
posted = {}  # (channel, ts) -> (text, blocks) of messages posted with buttons, to take the buttons off later


class SlackError(ChatError):
    def __init__(self, text, code=""):
        super().__init__(text)
        self.code = code


def hint(token):
    return f"…{token[-4:]}" if token else ""


def chat_of(channel_id):
    """Slack's channel id for a peerlink channel id: "slack:C0123" -> "C0123"."""
    return str(channel_id).split(":", 1)[1] if str(channel_id).startswith("slack:") else str(channel_id)


def ids(message_id):
    """(ts, thread_ts or "") from a message id: "171.5" or "171.5@170.1"."""
    ts, _, thread = str(message_id or "").partition("@")
    return ts, thread


def manifest_url():
    """Slack's "create an app" page with peerlink's manifest filled in."""
    return "https://api.slack.com/apps?new_app=1&manifest_json=" + urllib.parse.quote(json.dumps(MANIFEST, separators=(",", ":")))


# -- the Web API -----------------------------------------------------------------

def call(token, method, params=None, timeout=15, retried=False):
    """One Web API call, its arguments form-encoded (every method takes that; lists and objects go as JSON)."""
    data = urllib.parse.urlencode({k: v if isinstance(v, str) else json.dumps(v) for k, v in (params or {}).items() if v is not None}).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data, method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as err:
        wait = float(err.headers.get("Retry-After") or 99) if err.code == 429 else 99
        if wait <= 5 and not retried:
            time.sleep(wait + 0.05)
            return call(token, method, params, timeout, True)
        raise SlackError(f"Slack answered {err.code} to {method}", "ratelimited" if err.code == 429 else str(err.code))
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise SlackError(f"couldn't reach Slack: {err}")
    if out.get("ok"):
        return out
    code = str(out.get("error") or "?")
    need = f" ({out['needed']})" if out.get("needed") else ""
    raise SlackError(WHY.get(code, f"Slack refused {method}: {code}") + need, code)


def inspect_tokens(bot_token, app_token):
    """Who the bot token is, once both tokens are checked with Slack. Raises SlackError."""
    bot_token, app_token = bot_token.strip(), app_token.strip()
    if not bot_token.startswith("xoxb-"):
        raise SlackError("the bot token starts with xoxb-; it's under OAuth & Permissions once the app is installed")
    if not app_token.startswith("xapp-"):
        raise SlackError("the app-level token starts with xapp-; make one under Basic Information, with the scope connections:write")
    me = call(bot_token, "auth.test")
    try:
        call(app_token, "apps.connections.open")
    except SlackError as err:
        raise SlackError(f"the app-level token: {err}. Check that Socket Mode is on and the token has connections:write", err.code)
    return {"user": me.get("user", "?"), "user_id": me.get("user_id", ""), "team": me.get("team", ""), "bot_id": me.get("bot_id", "")}


# -- text: agents write Discord-style markdown; Slack has its own ("mrkdwn") ----------------------------

def esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_mrkdwn(text):
    """Markdown as Slack mrkdwn: bold, italics, strikethrough, links and headings (as bold lines). Code and "> "
    quotes are the same in both. What it doesn't know stays as written."""
    out = []
    for i, part in enumerate(re.split(r"(```.*?```)", text, flags=re.S)):
        if i % 2:
            out.append("```" + esc(re.sub(r"^```[\w+-]*\n", "```", part)[3:-3]) + "```")
            continue
        kept = []

        def keep(made):
            kept.append(made)
            return f"\x00{len(kept) - 1}\x00"
        part = re.sub(r"`([^`\n]+)`", lambda m: keep(f"`{esc(m.group(1))}`"), part)
        part = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", lambda m: keep(f"<{m.group(2)}|{esc(m.group(1))}>"), part)
        part = re.sub(r"(?m)^(> ?)", lambda m: keep(m.group(1)), part)
        part = esc(part)
        part = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"_\1_", part)
        part = re.sub(r"\*\*(.+?)\*\*", r"*\1*", part)
        part = re.sub(r"~~(.+?)~~", r"~\1~", part)
        part = re.sub(r"(?m)^#{1,3} +(.+)$", lambda m: "*" + m.group(1).replace("*", "") + "*", part)
        out.append(re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], part))
    return "".join(out).strip()


def from_slack(text):
    """What someone typed, without Slack's wrapping: <https://x|label> is the address, &amp; is &."""
    text = re.sub(r"<((?:https?|mailto):[^|>]+)(?:\|[^>]*)?>", r"\1", text or "")
    text = re.sub(r"<#\w+\|([^>]*)>", r"#\1", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def actions(buttons):
    """[(label, id)] as an actions block; Slack takes 25 buttons, 75 characters each."""
    return {"type": "actions", "elements": [
        {"type": "button", "text": {"type": "plain_text", "text": str(label)[:75] or "?", "emoji": True}, "action_id": str(data)[:255], "value": str(data)[:2000]}
        for label, data in buttons[:25]]}


def section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}


def without_buttons(blocks, note=""):
    """A message's blocks with the buttons gone, and a small line in their place when there's something to say."""
    out = [b for b in blocks or [] if b.get("type") != "actions"]
    return out + ([{"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}] if note else [])


# -- the calls every chat platform offers -------------------------------------------

def post(token, channel_id, text, reply_to=None, buttons=None, files=None):
    """Post text to a chat, split as needed; the last part carries the buttons, and the files follow it. A reply to a
    message inside a thread goes into the thread. {"id": the ts of the part with the buttons}."""
    chat, sent = chat_of(channel_id), {}
    ts, thread = ids(reply_to)
    parts = chunks(text, PART)
    for i, part in enumerate(parts):
        body = to_mrkdwn(part)
        params = {"channel": chat, "text": body, "thread_ts": thread or None, "unfurl_links": False, "unfurl_media": False}
        if buttons and i == len(parts) - 1:
            params["blocks"] = [section(body), actions(buttons)]
        sent = call(token, "chat.postMessage", params)
        if "blocks" in params:
            posted[(chat, sent.get("ts", ""))] = (body, params["blocks"])
            while len(posted) > 500:
                posted.pop(next(iter(posted)))
    for name, data in files or []:
        up = call(token, "files.getUploadURLExternal", {"filename": name, "length": str(len(data))})
        req = urllib.request.Request(up["upload_url"], data=data, method="POST", headers={"Content-Type": "application/octet-stream"})
        try:
            urllib.request.urlopen(req, timeout=120).read()
        except (urllib.error.URLError, OSError) as err:
            raise SlackError(f"couldn't upload {name} to Slack: {err}")
        call(token, "files.completeUploadExternal", {"files": [{"id": up["file_id"], "title": name}], "channel_id": chat, "thread_ts": thread or None})
    return {"id": str(sent.get("ts", ""))}


def clear_buttons(token, channel_id, message_id):
    chat, ts = chat_of(channel_id), ids(message_id)[0]
    known = posted.pop((chat, ts), None)
    if not known:
        # Posted before the daemon last started: ask Slack what the message was.
        found = call(token, "conversations.history", {"channel": chat, "latest": ts, "oldest": ts, "inclusive": True, "limit": 1}).get("messages") or []
        if not found or not found[0].get("blocks"):
            return
        known = (found[0].get("text", ""), found[0]["blocks"])
    call(token, "chat.update", {"channel": chat, "ts": ts, "text": known[0], "blocks": without_buttons(known[1])})


def respond(url, body):
    """Answer through the address Slack gave with a press or a command (it works wherever the message is)."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except (urllib.error.URLError, OSError) as err:
        raise SlackError(f"couldn't answer on Slack: {err}")


def answer_press(press, text=None, clear=False):
    """Acknowledge a button press (Slack already has its receipt). With `text`, a line only the presser sees; with
    `clear`, the buttons go; otherwise a line that shows the pick replaces them."""
    if not press.get("response_url"):
        return
    try:
        if text:
            return respond(press["response_url"], {"response_type": "ephemeral", "replace_original": False, "text": text})
        msg = press.get("message") or {}
        label = next((b.get("text", {}).get("text", "") for blk in msg.get("blocks") or [] for b in blk.get("elements") or []
                      if b.get("action_id") == press["custom_id"]), "")
        note = "" if clear else f"✓ {esc(label)} · {esc(press.get('author', ''))}" if label else "✓ Answered"
        posted.pop((chat_of(press["channel_id"]), msg.get("ts", "")), None)
        respond(press["response_url"], {"replace_original": True, "text": msg.get("text", ""), "blocks": without_buttons(msg.get("blocks"), note)})
    except SlackError:
        pass  # the press itself went through


def react(token, channel_id, message_id, emoji):
    try:
        call(token, "reactions.add", {"channel": chat_of(channel_id), "timestamp": ids(message_id)[0], "name": REACTIONS.get(emoji, "eyes")})
    except SlackError:
        pass  # a reaction is a courtesy


def unreact(token, channel_id, message_id, emoji):
    try:
        call(token, "reactions.remove", {"channel": chat_of(channel_id), "timestamp": ids(message_id)[0], "name": REACTIONS.get(emoji, "eyes")})
    except SlackError:
        pass


def download(token, file, limit):
    """An attachment's bytes ({url, filename, size} as Bot reports it), up to `limit`; None when it's bigger."""
    if file.get("size") and file["size"] > limit:
        return None
    req = urllib.request.Request(file["url"], headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            # Without files:read Slack answers with its sign-in page, not the file.
            if "text/html" in (r.headers.get("Content-Type") or "") and not file.get("filename", "").lower().endswith((".html", ".htm")):
                raise SlackError("Slack wouldn't give the file: the app needs the files:read permission")
            data = r.read(limit + 1)
    except (urllib.error.URLError, OSError) as err:
        raise SlackError(f"couldn't fetch the file from Slack: {err}")
    return None if len(data) > limit else data


# -- events, over Socket Mode -----------------------------------------------------------------------

class Bot:
    """Socket Mode in a thread of its own, reconnecting with backoff; outward calls are plain Web API calls.

    `on_message(msg)` gets each message from an allowed person: {channel_id ("slack:<channel>"), message_id, author,
    author_id, text, dm, mentioned, ref, files, bot}, as discordlink's Bot reports Discord's.
    """

    def __init__(self, token, app_token, on_message, log, on_press=None):
        self.token, self.app_token = token, app_token
        self.on_message = on_message
        self.on_press = on_press or (lambda press: None)
        self.log = log
        self.state = "connecting"
        self.error = ""
        self.user = ""
        self.user_id = ""
        self.team = ""
        self.reads_messages = True  # every message in a channel the app is in reaches it
        self.allowed_users = set()
        self.trusted_bots = set()  # user ids of other apps' bots whose posts reach agents as yours do
        self.chats = {}  # "slack:<id>" -> name, every conversation the app is in
        self.people = {}  # user id -> name
        self.on_command = lambda cmd: ("", None)
        self.on_autocomplete = lambda cmd: []
        self.on_chat = lambda channel_id, title: None  # a conversation seen for the first time, or renamed
        self.on_stranger = lambda who: None  # someone not allowed wrote: {id, name, username, chat}
        self.ws = None
        self.stop = threading.Event()
        self.jobs = queue.Queue()
        threading.Thread(target=self._work, name="slack-messages", daemon=True).start()
        self.thread = threading.Thread(target=self._run, name="slack", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        ws = self.ws
        if ws:
            try:
                ws.sock.close()
            except OSError:
                pass

    def channels_live(self):
        """The conversations the app is in, as one group for the Channels page."""
        chans = sorted(({"id": cid, "name": name} for cid, name in self.chats.items()), key=lambda c: c["name"].lower())
        return [{"id": "slack", "name": "Slack", "channels": chans}]

    def channel_name(self, channel_id):
        return self.chats.get(channel_id, "").lstrip("#")

    def person(self, uid):
        if uid and uid not in self.people:
            try:
                u = call(self.token, "users.info", {"user": uid}).get("user") or {}
                prof = u.get("profile") or {}
                self.people[uid] = u.get("real_name") or prof.get("real_name") or prof.get("display_name") or u.get("name") or uid
            except SlackError:
                return uid
        return self.people.get(uid, uid or "someone")

    def _name(self, c):
        if c.get("is_im"):
            return self.person(c.get("user", ""))
        return ("" if c.get("is_mpim") else "#") + (c.get("name") or c.get("id", ""))

    def _seen(self, cid, name):
        if name and self.chats.get(cid) != name:
            self.chats[cid] = name
            self.on_chat(cid, name)

    def list_chats(self):
        """Every conversation the app is in, asked of Slack: a channel shows on the Channels page once it's invited."""
        cursor = ""
        for _ in range(10):
            out = call(self.token, "users.conversations", {"types": "public_channel,private_channel,mpim,im", "limit": 200,
                                                             "exclude_archived": True, "cursor": cursor or None})
            for c in out.get("channels") or []:
                self._seen(f"slack:{c['id']}", self._name(c))
            cursor = (out.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break

    def _chat(self, channel):
        cid = f"slack:{channel}"
        if cid not in self.chats:
            try:
                self._seen(cid, self._name(call(self.token, "conversations.info", {"channel": channel}).get("channel") or {}))
            except SlackError:
                pass
        return cid

    def _run(self):
        backoff = 2
        while not self.stop.is_set():
            try:
                me = call(self.token, "auth.test")
                self.user, self.user_id, self.team = me.get("user", ""), me.get("user_id", ""), me.get("team", "")
                url = call(self.app_token, "apps.connections.open")["url"]
                try:
                    self.list_chats()
                except SlackError as err:
                    self.log("slack", "couldn't list its channels:", err)
                self.ws = ws = WebSocket(url)
                ws.sock.settimeout(QUIET_S)
                threading.Thread(target=self._keep, args=(ws,), name="slack-ping", daemon=True).start()
                while not self.stop.is_set():
                    env = ws.recv()
                    kind = env.get("type")
                    if kind == "hello":
                        self.state, self.error = "online", ""
                        self.log("slack", "online as", self.user, "in", self.team)
                        backoff = 2
                        continue
                    if kind == "disconnect":
                        break  # Slack is recycling the connection: open another
                    if env.get("envelope_id"):
                        ws.send({"envelope_id": env["envelope_id"]})  # the receipt, within Slack's three seconds
                    try:
                        self._dispatch(kind, env.get("payload") or {})
                    except Exception as err:  # one bad event must not stop the others
                        self.log("slack", "event failed:", err)
                self._shut(ws)
                continue
            except SlackError as err:
                if self.stop.is_set():
                    return
                self.log("slack", "connection dropped:", err)
                if err.code in ("invalid_auth", "not_authed", "token_revoked", "account_inactive"):
                    self.state, self.error = "error", f"{err}."
                    return
                self.state, self.error = ("error" if err.code else "connecting"), str(err)
            except (OSError, ValueError) as err:  # the socket: closed, timed out, or sent what isn't JSON
                if self.stop.is_set():
                    return
                self.log("slack", "connection dropped:", err)
                self.state = "connecting"
            self._shut(self.ws)
            if self.stop.wait(backoff):
                return
            backoff = min(backoff * 2, 120)

    def _shut(self, ws):
        if ws:
            try:
                ws.sock.close()
            except OSError:
                pass
        if self.ws is ws:
            self.ws = None

    def _keep(self, ws):
        """While this socket is the one in use: ping it, so silence means it died, and list the channels again."""
        last = time.time()
        while not self.stop.wait(PING_S) and self.ws is ws:
            try:
                ws._frame(0x9, b"")
            except OSError:
                return
            if time.time() - last >= CHATS_EVERY_S:
                last = time.time()
                try:
                    self.list_chats()
                except SlackError:
                    pass

    def _work(self):
        while True:
            fn, arg = self.jobs.get()
            self._guarded(fn, arg)

    def _guarded(self, fn, arg):
        try:
            fn(arg)
        except Exception as err:
            self.log("slack", getattr(fn, "__name__", "handler"), "failed:", err)

    def _dispatch(self, kind, p):
        if kind == "events_api":
            event = p.get("event") or {}
            if event.get("type") == "message":
                self.jobs.put((self._message, event))
        elif kind == "interactive" and p.get("type") == "block_actions":
            threading.Thread(target=self._guarded, args=(self._press, p), daemon=True).start()
        elif kind == "slash_commands":
            threading.Thread(target=self._guarded, args=(self._command, p), daemon=True).start()

    def _stranger(self, uid, cid):
        self.on_stranger({"id": uid, "name": self.person(uid), "username": "", "chat": cid, "chat_name": self.chats.get(cid, "")})

    def _press(self, p):
        user, msg = p.get("user") or {}, p.get("message") or p.get("container") or {}
        channel = (p.get("channel") or {}).get("id") or (p.get("container") or {}).get("channel_id", "")
        act = (p.get("actions") or [{}])[0]
        if user.get("id") not in self.allowed_users:
            return respond(p["response_url"], {"response_type": "ephemeral", "replace_original": False,
                                               "text": "Only people allowed on peerlink's Channels page can answer this."})
        self.on_press({"id": p.get("trigger_id", ""), "token": self.token, "custom_id": act.get("action_id") or act.get("value", ""),
                       "channel_id": f"slack:{channel}", "message": msg, "author": self.person(user.get("id", "")),
                       "response_url": p.get("response_url", "")})

    def _message(self, e):
        # Edits, deletions, joins and the like carry a subtype; a message with a file is "file_share".
        if e.get("subtype") not in (None, "file_share", "thread_broadcast", "bot_message"):
            return
        uid, channel = e.get("user", ""), e.get("channel", "")
        if not channel or uid == self.user_id:
            return
        is_bot = bool(e.get("bot_id"))
        if is_bot and uid not in self.trusted_bots:
            return
        cid = self._chat(channel)
        text = e.get("text") or ""
        if not is_bot and uid not in self.allowed_users:
            if uid and (text or e.get("files")):
                self._stranger(uid, cid)
            return
        mention = f"<@{self.user_id}>"
        mentioned = e.get("channel_type") == "im" or (bool(self.user_id) and mention in text)
        if self.user_id:
            text = text.replace(mention, "")
        text = from_slack(re.sub(r"<@(\w+)>", lambda m: "@" + self.person(m.group(1)), text)).strip()
        files = [{"url": f.get("url_private_download") or f.get("url_private", ""), "filename": f.get("name") or "file", "size": f.get("size", 0)}
                 for f in e.get("files") or [] if f.get("url_private_download") or f.get("url_private")]
        if not text and not files:
            return
        ts, thread = e.get("ts", ""), e.get("thread_ts", "")
        inside = bool(thread) and thread != ts
        self.on_message({
            "channel_id": cid, "message_id": f"{ts}@{thread}" if inside else ts, "guild_id": "", "author": self.person(uid),
            "author_id": uid, "text": text, "dm": False, "mentioned": mentioned, "ref": thread if inside else "", "files": files, "bot": is_bot,
        })

    def _command(self, p):
        uid, cid = p.get("user_id", ""), self._chat(p.get("channel_id", ""))
        url = p.get("response_url", "")

        def only_you(text):
            if url:
                respond(url, {"response_type": "ephemeral", "text": text})
        if uid not in self.allowed_users:
            self._stranger(uid, cid)
            return only_you("You aren't allowed to talk to these agents yet. Allow yourself on the Channels page of peerlink's web UI.")
        sub, _, rest = from_slack(p.get("text") or "").strip().partition(" ")
        if not sub:
            return only_you("Say which: " + ", ".join(SUBS) + ". Like `/peerlink run loop 5m check the deploy`")
        cmd = {"sub": sub.lower(), "options": command_options(sub.lower(), rest.strip()), "focused": "", "channel_id": cid, "dm": False,
               "author": self.person(uid), "message_id": ""}
        try:
            text, rows = self.on_command(cmd)
        except Exception as err:
            self.log("slack", "command failed:", err)
            text, rows = f"That failed: {err}", None
        try:
            post(self.token, cid, text or "Done.", None, rows)
        except SlackError as err:
            # The app isn't in the channel the command was typed in: answer the one who typed it.
            if err.code not in ("not_in_channel", "channel_not_found"):
                raise
            body = to_mrkdwn(text or "Done.")[:PART]
            respond(url, {"response_type": "ephemeral", "text": body, **({"blocks": [section(body), actions(rows)]} if rows else {})})
