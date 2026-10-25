"""peerlink's Discord bot: a channel per agent, so you can talk to your agents from your phone.

Written for the standard library because the daemon installs nothing: REST over urllib, and the gateway over a small
WebSocket client below instead of py-cord.

**Setup is one paste.** Given a bot token, the daemon asks Discord who the bot is and which application it
belongs to, builds the invite link itself, and lists the channels the bot can see once it is invited: no
Developer Mode, no copied IDs.

**Who may talk.** Only the application's owner, or its team's members for a team-owned app, read from the
application when the bot connects. Whoever can paste this bot's token into this daemon already owns it, so that
needs no setting. The owner can let other people in by user ID (`allowed_users`). Anyone else is ignored.

**Reading messages.** Without Discord's Message Content switch a bot sees the text of a message only when it is
@mentioned or DMed. The daemon asks Discord whether the switch is on and asks for the intent only then: asking
without it closes the gateway (4014).

The token lives in discord.json, 0600, beside the key; the API never echoes it, only its last four characters.
"""
import base64
import json
import os
import random
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.request

API = os.environ.get("PEERLINK_DISCORD_API", "https://discord.com/api/v10")
GATEWAY = os.environ.get("PEERLINK_DISCORD_GATEWAY", "wss://gateway.discord.gg/?v=10&encoding=json")

# View Channel (1<<10) + Send Messages (1<<11) + Read Message History (1<<16) + Add Reactions (1<<6), from the
# permissions table in Discord's docs. Reactions show where a message is at: 👀 delivered, ✅ answered.
INVITE_PERMISSIONS = (1 << 6) | (1 << 10) | (1 << 11) | (1 << 16)
# GUILDS, GUILD_MESSAGES, DIRECT_MESSAGES; MESSAGE_CONTENT (1<<15) is added only when the switch is on.
INTENTS = (1 << 0) | (1 << 9) | (1 << 12)
MESSAGE_CONTENT = 1 << 15
# GATEWAY_MESSAGE_CONTENT (1<<18) for a verified app, _LIMITED (1<<19) for one in under 100 servers: the switch.
MESSAGE_CONTENT_FLAGS = (1 << 18) | (1 << 19)
# Text and announcement channels: the two a bot can post in.
POSTABLE = {0, 5}
LIMIT = 2000


class DiscordError(ValueError):
    pass


def hint(token):
    return f"…{token[-4:]}" if token else ""


# -- REST --------------------------------------------------------------------

def call(token, method, path, body=None, retried=False, files=None):
    """A REST call; with `files` ([(name, bytes)]) the body goes as multipart, as uploads must."""
    if files:
        boundary = "peerlink" + os.urandom(8).hex()
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\nContent-Type: application/json\r\n\r\n'.encode()
                 + json.dumps(body or {}).encode() + b"\r\n"]
        for i, (name, data) in enumerate(files):
            safe = name.replace('"', "'").replace("\r", "").replace("\n", "")
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files[{i}]"; filename="{safe}"\r\n'
                         f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n")
        data, ctype = b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"
    else:
        data, ctype = (None if body is None else json.dumps(body).encode()), "application/json"
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bot {token}", "Content-Type": ctype,
                 "User-Agent": "DiscordBot (https://github.com/dfunq/peerlink, 1)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as err:
        text = err.read().decode(errors="replace")
        if err.code == 429 and not retried:
            # Per-route limits reset in well under a second; one wait is the fix, not a loop.
            try:
                wait = float(json.loads(text).get("retry_after", 1))
            except ValueError:
                wait = 1.0
            if wait <= 5:
                time.sleep(wait + 0.05)
                return call(token, method, path, body, True, files)
            raise DiscordError(f"Discord is rate limiting this bot; try again in {wait:.0f}s")
        if err.code == 401:
            raise DiscordError("Discord rejected that token. Copy it again from the Bot page of your application "
                               "(Reset Token shows it once).")
        raise DiscordError(f"Discord answered {err.code}: {text[:200]}")
    except (urllib.error.URLError, OSError) as err:
        raise DiscordError(f"couldn't reach Discord: {err}")


def invite_url(app_id):
    return ("https://discord.com/oauth2/authorize"
            f"?client_id={app_id}&scope=bot&permissions={INVITE_PERMISSIONS}")


def inspect_token(token):
    """Who the token is, its application, and the link that invites it. Raises DiscordError."""
    token = token.strip()
    if not token:
        raise DiscordError("paste the bot's token")
    me = call(token, "GET", "/users/@me")
    app = call(token, "GET", "/oauth2/applications/@me")
    app_id = str(app.get("id") or me.get("id"))
    return {"user": me.get("username", "?"), "user_id": str(me.get("id")), "app_id": app_id,
            "invite_url": invite_url(app_id)}


def owners(token):
    """The Discord user ids allowed to talk to the bot: the app's owner, or its team's members."""
    app = call(token, "GET", "/oauth2/applications/@me")
    team = app.get("team") or {}
    ids = {str(m["user"]["id"]) for m in team.get("members", []) if m.get("user")}
    if not ids and app.get("owner"):
        ids.add(str(app["owner"]["id"]))
    return ids


def message_content_on(token):
    """Whether Discord's Message Content switch is on for this app; None when Discord couldn't be asked."""
    try:
        flags = int(call(token, "GET", "/oauth2/applications/@me").get("flags") or 0)
    except DiscordError:
        return None
    return bool(flags & MESSAGE_CONTENT_FLAGS)


def channels_rest(token):
    out = []
    for g in call(token, "GET", "/users/@me/guilds") or []:
        found = call(token, "GET", f"/guilds/{g['id']}/channels") or []
        chans = sorted((c for c in found if c.get("type") in POSTABLE),
                       key=lambda c: (c.get("position", 0), c.get("name", "")))
        out.append({"id": str(g["id"]), "name": g.get("name", "?"),
                    "channels": [{"id": str(c["id"]), "name": c.get("name", "?")} for c in chans]})
    return out


def chunks(text, limit=LIMIT - 50):
    """Text split for Discord's 2000-character messages, at a paragraph or line break where it can be."""
    text = text.strip() or "(empty)"
    out = []
    while len(text) > limit:
        cut = max(text.rfind("\n\n", 0, limit), text.rfind("\n", 0, limit))
        cut = cut if cut > limit // 2 else limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    out.append(text)
    return out


def buttons(options, prefix):
    """Options as Discord buttons, five to a row, at most 25; each button's id is `prefix:<index>`."""
    rows = []
    for i, label in enumerate(options[:25]):
        if i % 5 == 0:
            rows.append({"type": 1, "components": []})
        rows[-1]["components"].append({"type": 2, "style": 1, "label": str(label)[:80], "custom_id": f"{prefix}:{i}"})
    return rows


def post(token, channel_id, text, reply_to=None, components=None, files=None):
    """Post text to a channel, split as needed; the first part replies to `reply_to`, the last carries the
    buttons and the files, so they sit under the whole message."""
    sent = []
    parts = chunks(text)
    for i, part in enumerate(parts):
        body = {"content": part, "allowed_mentions": {"parse": []}}
        if i == 0 and reply_to:
            body["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
        last = i == len(parts) - 1
        if last and components:
            body["components"] = components
        sent.append(call(token, "POST", f"/channels/{channel_id}/messages", body, files=files if last else None))
    return sent


def answer_interaction(interaction_id, interaction_token, kind, data=None):
    """Acknowledge a button press within Discord's three seconds: 7 rewrites the message it was on."""
    call("", "POST", f"/interactions/{interaction_id}/{interaction_token}/callback", {"type": kind, **({"data": data} if data else {})})


def download(url, limit):
    """An attachment's bytes, up to `limit`; None when it's bigger. Discord's CDN links need no token."""
    req = urllib.request.Request(url, headers={"User-Agent": "DiscordBot (https://github.com/dfunq/peerlink, 1)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(limit + 1)
    return None if len(data) > limit else data


def react(token, channel_id, message_id, emoji):
    try:
        call(token, "PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/"
                           f"{urllib.request.quote(emoji)}/@me")
    except DiscordError:
        pass  # a reaction is a courtesy; a missing permission must not lose the message


def unreact(token, channel_id, message_id, emoji):
    try:
        call(token, "DELETE", f"/channels/{channel_id}/messages/{message_id}/reactions/"
                              f"{urllib.request.quote(emoji)}/@me")
    except DiscordError:
        pass


# -- the gateway, over a minimal WebSocket client ------------------------------------------------

class WebSocket:
    """Just enough RFC 6455 for Discord's gateway: TLS, text frames out (masked), text in, ping/pong, close."""

    def __init__(self, url):
        scheme, _, rest = url.partition("://")
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        port = int(port or (443 if scheme == "wss" else 80))
        raw = socket.create_connection((host, port), timeout=30)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if scheme == "wss" else raw
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("gateway closed during the handshake")
            head += chunk
        head, _, self.buf = head.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise ConnectionError("gateway refused the upgrade: " + status.decode(errors="replace"))
        self.lock = threading.Lock()
        self.sock.settimeout(None)

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("gateway closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, opcode, payload):
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self.lock:
            self.sock.sendall(head + mask + body)

    def send(self, obj):
        self._frame(0x1, json.dumps(obj).encode())

    def recv(self):
        """The next text message, decoded; raises ConnectionError on close, with the close code when given."""
        message = b""
        while True:
            b1, b2 = self._read(2)
            opcode, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1000
                raise ConnectionError(f"close {code}")
            if opcode == 0x9:
                self._frame(0xA, payload)
                continue
            if opcode in (0x1, 0x2, 0x0):
                message += payload
                if b1 & 0x80:
                    return json.loads(message)

    def close(self):
        try:
            self._frame(0x8, struct.pack(">H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class Bot:
    """The gateway connection, in a thread of its own, reconnecting with backoff.

    `on_message(msg)` is called with each message from an owner: {channel_id, message_id, guild_id, author,
    author_id, text, dm, mentioned}. Everything outward (posting, reacting) is plain REST from any thread.
    """

    def __init__(self, token, on_message, log, on_press=None):
        self.token = token
        self.on_message = on_message
        self.on_press = on_press or (lambda press: None)
        self.log = log
        self.state = "connecting"
        self.error = ""
        self.user_id = ""
        self.user = ""
        self.owner_ids = set()
        self.trusted_bots = set()  # bot user IDs whose posts count as the owner's; empty by default
        self.allowed_users = set()  # people the owner lets in by user ID, beside the app's owner or team
        self.reads_messages = False
        self.guild_channels = {}  # guild id -> {"name", "channels": {id: (position, name)}}
        self.stop = threading.Event()
        self.ws = None
        self.thread = threading.Thread(target=self._run, name="discord", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.ws:
            self.ws.close()

    def channels_live(self):
        """The servers and channels the gateway reported, or None before it has."""
        if self.state != "online":
            return None
        return [{"id": gid, "name": g["name"],
                 "channels": [{"id": cid, "name": name} for cid, (_, name) in sorted(g["channels"].items(), key=lambda c: c[1])]}
                for gid, g in self.guild_channels.items()]

    def set_channels(self, guilds):
        """Replace what the gateway reported with a fresh listing from REST (channels_rest), already in order."""
        self.guild_channels = {g["id"]: {"name": g["name"], "channels": {c["id"]: (i, c["name"]) for i, c in enumerate(g["channels"])}}
                               for g in guilds}

    def channel_name(self, channel_id):
        for g in self.guild_channels.values():
            if channel_id in g["channels"]:
                return g["channels"][channel_id][1]
        return ""

    def _run(self):
        backoff = 2
        while not self.stop.is_set():
            try:
                self.owner_ids = owners(self.token)
                self.reads_messages = bool(message_content_on(self.token))
                self._session()
                backoff = 2
            except ConnectionError as err:
                code = str(err)
                if "4004" in code:
                    self.state, self.error = "error", "Discord rejected the token; paste a new one."
                    return
                if "4014" in code:
                    # Asked for message content without the switch: go on without it.
                    self.reads_messages = False
                self.state, self.error = "connecting", f"reconnecting ({code})"
            except (DiscordError, OSError, ValueError) as err:
                self.state, self.error = "error", str(err)
            if self.stop.wait(backoff):
                return
            backoff = min(backoff * 2, 120)

    def _session(self):
        self.ws = ws = WebSocket(GATEWAY)
        hello = ws.recv()
        interval = hello["d"]["heartbeat_interval"] / 1000
        seq = [None]
        acked = [True]

        def beat():
            time.sleep(interval * random.random())
            while not self.stop.is_set():
                if not acked[0]:
                    ws.close()  # a zombied connection: reconnect
                    return
                acked[0] = False
                try:
                    ws.send({"op": 1, "d": seq[0]})
                except OSError:
                    return
                if self.stop.wait(interval):
                    return

        threading.Thread(target=beat, name="discord-heartbeat", daemon=True).start()
        intents = INTENTS | (MESSAGE_CONTENT if self.reads_messages else 0)
        ws.send({"op": 2, "d": {"token": self.token, "intents": intents,
                                "properties": {"os": "macos", "browser": "peerlink", "device": "peerlink"}}})
        while not self.stop.is_set():
            ev = ws.recv()
            if ev.get("s") is not None:
                seq[0] = ev["s"]
            op = ev.get("op")
            if op == 11:
                acked[0] = True
            elif op == 1:
                ws.send({"op": 1, "d": seq[0]})
            elif op in (7, 9):
                ws.close()
                raise ConnectionError(f"op {op}")
            elif op == 0:
                self._dispatch(ev.get("t"), ev.get("d") or {})

    def _dispatch(self, kind, d):
        if kind == "READY":
            self.user_id = str(d["user"]["id"])
            self.user = d["user"].get("username", "")
            self.state, self.error = "online", ""
            self.log("discord", "online as", self.user)
        elif kind in ("GUILD_CREATE", "GUILD_UPDATE"):
            self.guild_channels[str(d["id"])] = {
                "name": d.get("name", "?"),
                "channels": {str(c["id"]): (c.get("position", 0), c.get("name", "?")) for c in d.get("channels", []) if c.get("type") in POSTABLE}}
        elif kind == "GUILD_DELETE":
            self.guild_channels.pop(str(d.get("id")), None)
        elif kind in ("CHANNEL_CREATE", "CHANNEL_UPDATE", "CHANNEL_DELETE"):
            # A channel added, renamed or removed after the bot connected; without this it shows only after a restart.
            g = self.guild_channels.get(str(d.get("guild_id")))
            if g is not None:
                g["channels"].pop(str(d.get("id")), None)
                if kind != "CHANNEL_DELETE" and d.get("type") in POSTABLE:
                    g["channels"][str(d["id"])] = (d.get("position", 0), d.get("name", "?"))
        elif kind == "MESSAGE_CREATE":
            author = d.get("author") or {}
            aid = str(author.get("id"))
            # The owner, someone they let in, or a bot they trust by ID; never this bot itself, so its own posts can't loop.
            if aid == self.user_id or aid not in (self.trusted_bots if author.get("bot") else self.owner_ids | self.allowed_users):
                return
            mentioned = any(str(m.get("id")) == self.user_id for m in d.get("mentions", []))
            text = d.get("content") or ""
            for tag in (f"<@{self.user_id}>", f"<@!{self.user_id}>"):
                text = text.replace(tag, "")
            text = text.strip()
            files = [{"url": a.get("url"), "filename": a.get("filename", "file"), "size": a.get("size", 0)}
                     for a in d.get("attachments", []) if a.get("url")]
            if not text and not files:
                return
            try:
                self.on_message({
                    "channel_id": str(d["channel_id"]), "message_id": str(d["id"]),
                    "guild_id": str(d.get("guild_id") or ""), "author": author.get("global_name") or author.get("username", "you"),
                    "author_id": str(author["id"]), "text": text, "dm": not d.get("guild_id"), "mentioned": mentioned,
                    "ref": str((d.get("message_reference") or {}).get("message_id") or ""),  # the message it replies to
                    "files": files, "bot": bool(author.get("bot")),
                })
            except Exception as err:  # one bad message must not drop the connection
                self.log("discord", "message handler failed:", err)
        elif kind == "INTERACTION_CREATE" and d.get("type") == 3:
            user = (d.get("member") or {}).get("user") or d.get("user") or {}
            if str(user.get("id")) not in self.owner_ids | self.allowed_users:
                # Only the owner's press counts, or an allowed user's; anyone else is told so, privately (flag 64).
                answer_interaction(d["id"], d["token"], 4, {"content": "Only the bot's owner can answer this.", "flags": 64})
                return
            try:
                self.on_press({
                    "id": d["id"], "token": d["token"], "custom_id": (d.get("data") or {}).get("custom_id", ""),
                    "channel_id": str(d.get("channel_id")), "message": d.get("message") or {},
                    "author": user.get("global_name") or user.get("username", "you"),
                })
            except Exception as err:
                self.log("discord", "button handler failed:", err)
