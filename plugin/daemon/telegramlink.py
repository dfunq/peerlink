"""peerlink's Telegram bot: talk to your agents from Telegram, in a group or a private chat with the bot.

Standard library only, like the rest of the daemon: the Bot API over urllib, and updates by long polling
(getUpdates), so nothing needs a public URL.

**Setup is one paste.** Make a bot with @BotFather, paste its token on the Channels page, and the daemon asks
Telegram who the bot is.

**Who may talk.** Nobody, until you allow them: anyone can find a Telegram bot by name. A message from someone not
on the list gets no answer and reaches no agent; the Channels page shows who tried, with an Allow button. So you
pair by messaging the bot, then allowing yourself.

**Reading messages.** In a private chat the bot sees everything. In a group, Telegram's privacy mode shows it only
commands, @mentions and replies to it, unless you turn privacy off with @BotFather (/setprivacy), or make the bot
an admin of the group.

Chats are `tg:<chat id>` to the daemon; the token lives in discord.json, 0600, and is never echoed back.
"""
import html
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request

from chatlink import ChatError

API = os.environ.get("PEERLINK_TELEGRAM_API", "https://api.telegram.org")
LIMIT = 4096
POLL_S = 25  # how long one getUpdates waits for news; a bot that's replaced stops within this

# Telegram lets a bot react only from a fixed set: peerlink's marks, mapped to the nearest one there.
REACTIONS = {"👀": "👀", "🤔": "🤔", "🛠️": "👨‍💻", "💤": "😴", "❓": "🙏", "✅": "👍", "❌": "👎"}
# The /peerlink subcommands, which also work on their own: /status, /inbox and the rest.
SUBS = ("status", "inbox", "peers", "spaces", "run", "clear", "kill", "mute", "unmute", "schedules", "cancel", "resume")
COMMANDS = [("peerlink", "peerlink: status, inbox, peers, spaces, run, clear, kill, schedules…"),
            ("status", "The daemon, how many agents run, and the usage limit"),
            ("inbox", "What's waiting on you: approvals, tasks, questions"),
            ("peers", "The agents running now"),
            ("spaces", "The spaces, their leads and agents"),
            ("schedules", "Scheduled prompts, with their next run"),
            ("resume", "Lift a usage-limit hold, after switching accounts")]
ANSWERED = "x"  # the button left on a message once it's answered


class TelegramError(ChatError):
    def __init__(self, text, code=0, retry_after=0, migrate_to=None):
        super().__init__(text)
        self.code, self.retry_after, self.migrate_to = code, retry_after, migrate_to


def hint(token):
    return f"…{token[-4:]}" if token else ""


def chat_of(channel_id):
    """The Bot API's chat id for a peerlink channel id: "tg:-100123" -> "-100123"."""
    return str(channel_id).split(":", 1)[1] if str(channel_id).startswith("tg:") else str(channel_id)


# -- the Bot API ---------------------------------------------------------------

def call(token, method, params=None, files=None, timeout=15, retried=False):
    """One Bot API call; with `files` ({field: (name, bytes)}) the params go as multipart, as uploads must."""
    url = f"{API}/bot{token}/{method}"
    if files:
        boundary = "peerlink" + os.urandom(8).hex()
        parts = []
        for k, v in (params or {}).items():
            v = v if isinstance(v, str) else json.dumps(v)
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for field, (name, data) in files.items():
            safe = name.replace('"', "'").replace("\r", "").replace("\n", "")
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{safe}"\r\n'
                         f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n")
        data, ctype = b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"
    else:
        data, ctype = json.dumps(params or {}).encode(), "application/json"
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as err:
        try:
            out = json.loads(err.read() or b"{}")
        except ValueError:
            out = {"ok": False, "error_code": err.code, "description": str(err)}
    except (urllib.error.URLError, OSError) as err:
        raise TelegramError(f"couldn't reach Telegram: {err}")
    if out.get("ok"):
        return out.get("result")
    code, desc, extra = out.get("error_code", 0), out.get("description", "?"), out.get("parameters") or {}
    if code == 429 and not retried and extra.get("retry_after", 99) <= 5:
        time.sleep(extra["retry_after"] + 0.05)
        return call(token, method, params, files, timeout, True)
    if code in (401, 404) and method == "getMe":
        raise TelegramError("Telegram rejected that token. Copy it again from @BotFather (/mybots, then API Token).", code)
    raise TelegramError(f"Telegram answered {code}: {desc[:200]}", code, extra.get("retry_after", 0), extra.get("migrate_to_chat_id"))


def inspect_token(token):
    """Who the token is. Raises TelegramError."""
    token = token.strip()
    if not re.fullmatch(r"\d+:[\w-]{20,}", token):
        raise TelegramError("that doesn't look like a bot token; @BotFather gives one like 123456789:AAE…")
    me = call(token, "getMe")
    return {"user": me.get("username", "?"), "user_id": str(me.get("id")),
            "reads_groups": bool(me.get("can_read_all_group_messages"))}


# -- text: agents write Discord-style markdown; Telegram takes a little HTML ------------------------

def chunks(text, limit=3500):
    """Text split under Telegram's 4096 characters (with room for the HTML), at a paragraph or line break."""
    text = text.strip() or "(empty)"
    out = []
    while len(text) > limit:
        cut = max(text.rfind("\n\n", 0, limit), text.rfind("\n", 0, limit))
        cut = cut if cut > limit // 2 else limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    out.append(text)
    return out


def to_html(text):
    """Markdown as Telegram HTML: code blocks and spans, bold, italics, strikethrough, links, headings as bold lines,
    and "> " quotes. What it doesn't know stays as written."""
    out = []
    for i, part in enumerate(re.split(r"```(?:[\w+-]*\n)?(.*?)```", text, flags=re.S)):
        if i % 2:
            out.append(f"<pre>{html.escape(part.strip(chr(10)), quote=False)}</pre>")
            continue
        spans = []

        def keep(m):
            spans.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
            return f"\x00{len(spans) - 1}\x00"
        part = html.escape(re.sub(r"`([^`\n]+)`", keep, part), quote=False)
        part = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>', part)
        part = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", part)
        part = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", part)
        part = re.sub(r"~~(.+?)~~", r"<s>\1</s>", part)
        part = re.sub(r"(?m)^#{1,3} +(.+)$", r"<b>\1</b>", part)
        part = re.sub(r"(?m)((?:^&gt; ?.*(?:\n|$))+)", lambda m: "<blockquote>" + re.sub(r"(?m)^&gt; ?", "", m.group(1)).rstrip("\n") + "</blockquote>\n", part)
        out.append(re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], part))
    return "".join(out).strip()


def keyboard(buttons):
    """[(label, id)] as an inline keyboard, three to a row; ids over Telegram's 64 bytes are cut there."""
    rows = []
    for i, (label, data) in enumerate(buttons[:24]):
        if i % 3 == 0:
            rows.append([])
        rows[-1].append({"text": str(label)[:60], "callback_data": data.encode()[:64].decode(errors="ignore")})
    return {"inline_keyboard": rows}


# -- the calls every chat platform offers ---------------------------------------

def send_text(token, chat, text, reply_to=None, markup=None):
    params = {"chat_id": chat, "text": to_html(text), "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
    if reply_to:
        params["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
    if markup:
        params["reply_markup"] = markup
    try:
        return call(token, "sendMessage", params)
    except TelegramError as err:
        if err.code != 400 or "parse" not in str(err).lower():
            raise
    # Markdown that made HTML Telegram can't read goes as it was written.
    params.pop("parse_mode")
    params["text"] = text
    return call(token, "sendMessage", params)


def post(token, channel_id, text, reply_to=None, buttons=None, files=None):
    """Post text to a chat, split as needed; the first part replies to `reply_to`, the last carries the buttons,
    and the files follow it. {"id": the id of the part with the buttons}."""
    chat, sent = chat_of(channel_id), None
    parts = chunks(text)
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        sent = send_text(token, chat, part, reply_to if i == 0 else None, keyboard(buttons) if last and buttons else None)
    for name, data in files or []:
        call(token, "sendDocument", {"chat_id": chat}, files={"document": (name, data)}, timeout=120)
    return {"id": str((sent or {}).get("message_id", ""))}


def clear_buttons(token, channel_id, message_id):
    call(token, "editMessageReplyMarkup", {"chat_id": chat_of(channel_id), "message_id": int(message_id)})


def answer_press(press, text=None, clear=False):
    """Acknowledge a button press. With `text`, a notice only the presser sees; with `clear`, the buttons go;
    otherwise they're replaced by one that shows the pick, as Telegram has no disabled buttons."""
    token = press["token"]
    call(token, "answerCallbackQuery", {"callback_query_id": press["id"], **({"text": text} if text else {})})
    if text:
        return
    msg = press.get("message") or {}
    params = {"chat_id": chat_of(press["channel_id"]), "message_id": int(msg.get("message_id") or 0)}
    if not clear:
        rows = ((msg.get("reply_markup") or {}).get("inline_keyboard")) or []
        label = next((b.get("text", "") for row in rows for b in row if b.get("callback_data") == press["custom_id"]), "")
        params["reply_markup"] = {"inline_keyboard": [[{"text": f"✓ {label}" if label else "✓ Answered", "callback_data": ANSWERED}]]}
    try:
        call(token, "editMessageReplyMarkup", params)
    except TelegramError:
        pass  # the message may be too old to edit; the press itself went through


def react(token, channel_id, message_id, emoji):
    try:
        call(token, "setMessageReaction", {"chat_id": chat_of(channel_id), "message_id": int(message_id),
                                           "reaction": [{"type": "emoji", "emoji": REACTIONS.get(emoji, "👀")}]})
    except (TelegramError, ValueError):
        pass  # a reaction is a courtesy


def unreact(token, channel_id, message_id, emoji):
    # A bot has one reaction per message; taking it off is setting none.
    try:
        call(token, "setMessageReaction", {"chat_id": chat_of(channel_id), "message_id": int(message_id), "reaction": []})
    except (TelegramError, ValueError):
        pass


def download(token, file, limit):
    """An attachment's bytes ({file_id, filename, size} as Bot reports it), up to `limit`; None when it's bigger.
    The Bot API serves files up to 20 MB."""
    if file.get("size") and file["size"] > limit:
        return None
    path = call(token, "getFile", {"file_id": file["file_id"]}).get("file_path", "")
    req = urllib.request.Request(f"{API}/file/bot{token}/{path}")
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(limit + 1)
    return None if len(data) > limit else data


# -- updates, by long polling -----------------------------------------------------------------------

def person(user):
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or user.get("username") or "someone"
    return name


def chat_title(chat):
    return chat.get("title") or person(chat)


def parse_command(text):
    """("sub", "rest") for "/peerlink sub rest" or "/sub rest", None when it isn't a peerlink command."""
    word, _, rest = text.strip().partition(" ")
    name = word[1:].split("@", 1)[0].lower()
    if name == "peerlink":
        sub, _, rest = rest.strip().partition(" ")
        return sub.lower(), rest.strip()
    return (name, rest.strip()) if name in SUBS else None


def command_options(sub, rest):
    """A command's options from what was typed after it, named as Discord's /peerlink names them."""
    if sub == "run":
        agent = re.search(r"(?:^|\s)agent:(\S+)", rest)
        line = (rest[:agent.start()] + rest[agent.end():]).strip() if agent else rest
        return {"command": line, **({"agent": agent.group(1)} if agent else {})}
    if sub == "cancel":
        return {"id": rest} if rest else {}
    if sub in ("clear", "kill", "mute", "unmute"):
        return {"agent": rest.lstrip("@")} if rest else {}
    return {}


class Bot:
    """Long polling in a thread of its own, retrying with backoff; outward calls are plain Bot API calls.

    `on_message(msg)` gets each message from an allowed person: {channel_id ("tg:<chat>"), message_id, author,
    author_id, text, dm, mentioned, ref, files, bot}, as discordlink's Bot reports Discord's.
    """

    def __init__(self, token, on_message, log, on_press=None):
        self.token = token
        self.on_message = on_message
        self.on_press = on_press or (lambda press: None)
        self.log = log
        self.state = "connecting"
        self.error = ""
        self.user = ""
        self.user_id = ""
        self.reads_messages = False  # in groups: Telegram's privacy mode is off
        self.allowed_users = set()
        self.trusted_bots = set()  # Telegram doesn't show a bot other bots' messages; kept for the shared interface
        self.chats = {}  # "tg:<id>" -> title, every chat the bot has heard from
        self.on_command = lambda cmd: ("", None)
        self.on_autocomplete = lambda cmd: []
        self.on_chat = lambda channel_id, title: None  # a chat seen for the first time, or renamed
        self.on_stranger = lambda who: None  # someone not allowed wrote: {id, name, username, chat}
        self.on_migrate = lambda old, new: None  # a group became a supergroup, with a new id
        self.offset = 0
        self.stop = threading.Event()
        self.jobs = queue.Queue()
        threading.Thread(target=self._work, name="telegram-messages", daemon=True).start()
        self.thread = threading.Thread(target=self._run, name="telegram", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()

    def channels_live(self):
        """The chats the bot has heard from, as one group for the Channels page."""
        chans = sorted(({"id": cid, "name": name} for cid, name in self.chats.items()), key=lambda c: c["name"].lower())
        return [{"id": "telegram", "name": "Telegram", "channels": chans}]

    def channel_name(self, channel_id):
        return self.chats.get(channel_id, "")

    def _run(self):
        backoff = 2
        while not self.stop.is_set():
            try:
                me = inspect_token(self.token)
                self.user, self.user_id, self.reads_messages = me["user"], me["user_id"], me["reads_groups"]
                call(self.token, "deleteWebhook")  # updates can come one way only
                try:
                    call(self.token, "setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
                except TelegramError as err:
                    self.log("telegram", "couldn't set the command list:", err)
                self.state, self.error = "online", ""
                self.log("telegram", "online as", self.user)
                backoff = 2
                while not self.stop.is_set():
                    updates = call(self.token, "getUpdates", {"offset": self.offset, "timeout": POLL_S,
                                                              "allowed_updates": ["message", "callback_query", "my_chat_member"]},
                                   timeout=POLL_S + 15)
                    for u in updates or []:
                        self.offset = max(self.offset, u["update_id"] + 1)
                        try:
                            self._dispatch(u)
                        except Exception as err:  # one bad update must not stop the others
                            self.log("telegram", "update failed:", err)
            except TelegramError as err:
                if self.stop.is_set():
                    return
                self.log("telegram", "connection dropped:", err)
                if err.code in (401, 404):
                    self.state, self.error = "error", "Telegram rejected the token; paste a new one."
                    return
                self.state = "error" if err.code else "connecting"
                self.error = ("another program is reading this bot's updates; stop it, or use another bot"
                              if err.code == 409 else str(err))
            if self.stop.wait(backoff):
                return
            backoff = min(backoff * 2, 120)

    def _work(self):
        while True:
            fn, arg = self.jobs.get()
            try:
                fn(arg)
            except Exception as err:
                self.log("telegram", getattr(fn, "__name__", "handler"), "failed:", err)

    def _seen(self, chat):
        cid, title = f"tg:{chat['id']}", chat_title(chat)
        if self.chats.get(cid) != title:
            self.chats[cid] = title
            self.on_chat(cid, title)
        return cid

    def _dispatch(self, u):
        if "my_chat_member" in u:
            self._seen(u["my_chat_member"]["chat"])
        elif "callback_query" in u:
            self._press(u["callback_query"])
        elif "message" in u:
            self._message(u["message"])

    def _press(self, q):
        user, msg = q.get("from") or {}, q.get("message") or {}
        if str(user.get("id")) not in self.allowed_users:
            return call(self.token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "Only people allowed on peerlink's Channels page can answer this."})
        if q.get("data") == ANSWERED:
            return call(self.token, "answerCallbackQuery", {"callback_query_id": q["id"], "text": "Already answered."})
        threading.Thread(target=self._guarded, args=(self.on_press, {
            "id": q["id"], "token": self.token, "custom_id": q.get("data", ""),
            "channel_id": f"tg:{(msg.get('chat') or {}).get('id')}", "message": msg, "author": person(user),
        }), daemon=True).start()

    def _guarded(self, fn, arg):
        try:
            fn(arg)
        except Exception as err:
            self.log("telegram", getattr(fn, "__name__", "handler"), "failed:", err)

    def _message(self, m):
        chat, user = m.get("chat") or {}, m.get("from") or {}
        if m.get("migrate_to_chat_id"):
            new = f"tg:{m['migrate_to_chat_id']}"
            self.chats[new] = self.chats.pop(f"tg:{chat['id']}", chat_title(chat))
            return self.on_migrate(f"tg:{chat['id']}", new)
        cid = self._seen(chat)
        text = m.get("text") or m.get("caption") or ""
        if not user or user.get("is_bot"):
            return
        aid = str(user["id"])
        if aid not in self.allowed_users:
            if text or m.get("document") or m.get("photo"):
                self.on_stranger({"id": aid, "name": person(user), "username": user.get("username", ""), "chat": cid, "chat_name": self.chats.get(cid, "")})
            return
        private = chat.get("type") == "private"
        command = parse_command(text) if text.startswith("/") else None
        addressed = re.match(r"/\S+@(\S+)", text)
        if command and not (addressed and addressed.group(1).lower() != self.user.lower()):
            sub, rest = command
            return threading.Thread(target=self._guarded, args=(self._command, {
                "sub": sub, "options": command_options(sub, rest), "focused": "", "channel_id": cid, "dm": False,
                "author": person(user), "message_id": str(m["message_id"])}), daemon=True).start()
        if text.split(" ", 1)[0].split("@", 1)[0] == "/start":
            return post(self.token, cid, "peerlink is connected. Map this chat to an agent on the Channels page of peerlink's web UI, "
                                         "and your messages here go to it.", str(m["message_id"]))
        mention = f"@{self.user}".lower()
        mentioned = private or mention in text.lower() or str((m.get("reply_to_message") or {}).get("from", {}).get("id")) == self.user_id
        if self.user:
            text = re.sub(re.escape(mention), "", text, flags=re.I).strip()
        files = []
        if m.get("document"):
            d = m["document"]
            files.append({"file_id": d["file_id"], "filename": d.get("file_name") or "file", "size": d.get("file_size", 0)})
        if m.get("photo"):
            p = max(m["photo"], key=lambda x: x.get("file_size", 0))
            files.append({"file_id": p["file_id"], "filename": f"photo-{m['message_id']}.jpg", "size": p.get("file_size", 0)})
        for kind, ext in (("video", "mp4"), ("audio", "mp3"), ("voice", "ogg")):
            if m.get(kind):
                f = m[kind]
                files.append({"file_id": f["file_id"], "filename": f.get("file_name") or f"{kind}-{m['message_id']}.{ext}", "size": f.get("file_size", 0)})
        if not text and not files:
            return
        self.jobs.put((self.on_message, {
            "channel_id": cid, "message_id": str(m["message_id"]), "guild_id": "", "author": person(user),
            "author_id": aid, "text": text, "dm": False, "mentioned": mentioned,
            "ref": str((m.get("reply_to_message") or {}).get("message_id") or ""), "files": files, "bot": False,
        }))

    def _command(self, cmd):
        if not cmd["sub"]:
            text, rows = "Say which: " + ", ".join(SUBS) + ". Like /peerlink run loop 5m check the deploy", None
        else:
            try:
                text, rows = self.on_command(cmd)
            except Exception as err:
                self.log("telegram", "command failed:", err)
                text, rows = f"That failed: {err}", None
        post(self.token, cmd["channel_id"], text or "Done.", cmd["message_id"], rows)
