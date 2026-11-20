"""peerlink's Teams channel: talk to your agents in a Microsoft Teams chat, signed in as you.

Teams has no bot token to paste, and a real Teams bot needs a public address. So this one signs in as you (Microsoft's
device code flow: a code you enter at microsoft.com/devicelogin) and reads and posts in your chats through Microsoft
Graph, by polling, so nothing needs a public URL. Standard library only, like the rest of the daemon.

**The app.** Every sign-in names an app registered in Entra. With none given it's `CLIENT_ID`, Microsoft's Graph
Command Line Tools, so there's nothing to register; a tenant that blocks it, or won't let you consent to Chat.ReadWrite,
needs an app of your own: a public client ("Allow public client flows" on) with delegated User.Read and Chat.ReadWrite.

**What you see.** Agents' posts go out as you, so each starts with the agent's name, and a mark you don't see tells
peerlink they're not yours to read back. Teams chats have no buttons: a question's options come as a numbered list,
and you answer with the number (or the option's words), quoting the question when several are open.

**Who may talk.** You always. In a group chat, others only once you allow them on the Channels page: the ones who
wrote are listed there. Chats are `teams:<chat id>` to the daemon; the refresh token lives in discord.json, 0600,
and is never echoed back.
"""
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from chatlink import ChatError
import telegramlink

AUTH = os.environ.get("PEERLINK_TEAMS_AUTH", "https://login.microsoftonline.com")
GRAPH = os.environ.get("PEERLINK_TEAMS_GRAPH", "https://graph.microsoft.com/v1.0")
# The app a sign-in names when you give none: Microsoft's own Graph Command Line Tools (Connect-MgGraph's), a public
# client anyone may sign in through, so there's nothing to register. Your tenant's sign-in log shows it by that name.
CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
TENANT = "organizations"  # any work or school account; your tenant's id or domain narrows it
SCOPES = "offline_access User.Read Chat.ReadWrite"
POLL_S = float(os.environ.get("PEERLINK_TEAMS_POLL_S", "4"))
CHATS_S = 600  # how often the list of your chats is read again
SELF_CHAT = "48:notes"  # Teams' chat with yourself
MARK = "⁣⁣"  # two invisible separators at the start of every post peerlink makes
LIMIT = 20000  # characters to a message; Teams allows about 28 KB of HTML
_bot = [None]


class TeamsError(ChatError):
    def __init__(self, text, code=0, retry_after=0):
        super().__init__(text)
        self.code, self.retry_after = code, retry_after


def channel(chat_id):
    return f"teams:{chat_id}"


def chat_of(channel_id):
    """Graph's chat id for a peerlink channel id: "teams:19:abc@thread.v2" -> "19:abc@thread.v2"."""
    return str(channel_id).split(":", 1)[1] if str(channel_id).startswith("teams:") else str(channel_id)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def when(text):
    """A Graph timestamp as seconds."""
    try:
        return datetime.strptime(re.sub(r"(\.\d{1,6})\d*Z$", r"\1Z", text or ""), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        try:
            return datetime.strptime(text or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return 0.0


# -- signing in ----------------------------------------------------------------

def _form(url, fields, timeout=20):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as err:
        try:
            return json.loads(err.read() or b"{}")
        except ValueError:
            return {"error": "http_error", "error_description": str(err)}
    except (urllib.error.URLError, OSError) as err:
        raise TeamsError(f"couldn't reach Microsoft: {err}")


def _auth_error(out):
    desc = str(out.get("error_description") or out.get("error") or "?").split("\r\n")[0]
    if "AADSTS7000218" in desc:
        desc = "the app doesn't allow public client flows; turn on Authentication > Allow public client flows in Entra"
    elif "AADSTS700016" in desc:
        desc = "no app with that client id in that tenant; check the id, or set the tenant to the one it's registered in"
    elif "AADSTS65001" in desc or "consent" in desc.lower():
        desc = "your tenant needs an admin to approve this app's permissions (User.Read, Chat.ReadWrite)"
    return TeamsError(f"Microsoft said: {desc[:300]}")


def start_sign_in(client_id, tenant=TENANT):
    """The device code to enter: {user_code, verification_uri, device_code, interval, expires_at}. Raises TeamsError."""
    client_id = (client_id or CLIENT_ID).strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", client_id):
        raise TeamsError("that doesn't look like an app (client) id; Entra shows one like 1b2c3d4e-…")
    out = _form(f"{AUTH}/{urllib.parse.quote(tenant or TENANT)}/oauth2/v2.0/devicecode", {"client_id": client_id, "scope": SCOPES})
    if "device_code" not in out:
        raise _auth_error(out)
    return {"user_code": out["user_code"], "verification_uri": out.get("verification_uri") or "https://microsoft.com/devicelogin",
            "device_code": out["device_code"], "interval": int(out.get("interval") or 5),
            "expires_at": time.time() + int(out.get("expires_in") or 900)}


def finish_sign_in(client_id, tenant, device_code):
    """The tokens once the code is entered, None while it's still waiting. Raises TeamsError when it won't come."""
    out = _form(f"{AUTH}/{urllib.parse.quote(tenant or TENANT)}/oauth2/v2.0/token", {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": client_id or CLIENT_ID, "device_code": device_code})
    if out.get("error") in ("authorization_pending", "slow_down"):
        return None
    if out.get("error") == "expired_token":
        raise TeamsError("the code expired before it was entered; start again")
    if out.get("error") in ("authorization_declined", "access_denied"):
        raise TeamsError("the sign-in was turned down")
    if "refresh_token" not in out:
        raise _auth_error(out)
    return out


class Auth:
    """A refresh token, and the access token it buys, renewed a few minutes before it runs out. `on_refresh` gets each
    new refresh token Microsoft hands back, to keep."""

    def __init__(self, client_id, tenant, refresh_token, on_refresh=None):
        self.client_id, self.tenant, self.refresh_token = client_id or CLIENT_ID, tenant or TENANT, refresh_token
        self.on_refresh = on_refresh or (lambda token: None)
        self.access, self.until = "", 0.0
        self.lock = threading.Lock()

    def take(self, tokens):
        self.access, self.until = tokens["access_token"], time.time() + int(tokens.get("expires_in") or 3600) - 300
        if tokens.get("refresh_token") and tokens["refresh_token"] != self.refresh_token:
            self.refresh_token = tokens["refresh_token"]
            self.on_refresh(self.refresh_token)

    def token(self, fresh=False):
        with self.lock:
            if fresh or not self.access or time.time() >= self.until:
                out = _form(f"{AUTH}/{urllib.parse.quote(self.tenant)}/oauth2/v2.0/token", {
                    "grant_type": "refresh_token", "client_id": self.client_id, "refresh_token": self.refresh_token, "scope": SCOPES})
                if "access_token" not in out:
                    err = _auth_error(out)
                    err.code = 401
                    raise err
                self.take(out)
            return self.access


# -- Graph -------------------------------------------------------------------------

def graph(auth, method, path, body=None, raw=False, limit=None, retried=False):
    """One Graph call, as JSON (or bytes with `raw`); a 401 renews the token once, a short 429 waits once."""
    url = path if path.startswith("http") else GRAPH + path
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {auth.token(fresh=retried == 'auth')}", **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = r.read() if limit is None else r.read(limit + 1)
            return out if raw else json.loads(out or b"{}")
    except urllib.error.HTTPError as err:
        code = err.code
        try:
            detail = json.loads(err.read() or b"{}").get("error", {})
        except ValueError:
            detail = {}
        wait = float(err.headers.get("Retry-After") or 0)
        if code == 401 and not retried:
            return graph(auth, method, path, body, raw, limit, "auth")
        if code == 429 and not retried and wait <= 10:
            time.sleep(wait or 2)
            return graph(auth, method, path, body, raw, limit, True)
        raise TeamsError(f"Teams answered {code}: {str(detail.get('message') or detail.get('code') or err)[:200]}", code, wait)
    except (urllib.error.URLError, OSError) as err:
        raise TeamsError(f"couldn't reach Teams: {err}")


# -- text: agents write Discord-style markdown; Teams takes HTML ------------------------------------

def to_html(text):
    """Markdown as Teams HTML: what Telegram's converter makes, with line breaks as <br> outside code blocks."""
    out = []
    for i, part in enumerate(re.split(r"(<pre>.*?</pre>)", telegramlink.to_html(text), flags=re.S)):
        out.append(part if i % 2 else part.replace("\n", "<br>"))
    return "".join(out)


def to_text(body):
    """A message's HTML as the plain text an agent reads."""
    s = re.sub(r"(?is)<attachment[^>]*>.*?</attachment>", "", body or "")
    s = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>", "\n", s)
    s = re.sub(r"(?is)<li[^>]*>", "- ", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def chunks(text, limit=LIMIT):
    return telegramlink.chunks(text, limit)


def ours(msg):
    """Whether peerlink posted it, by the mark at its start."""
    return MARK in ((msg.get("body") or {}).get("content") or "")[:200]


def numbered(buttons):
    return "\n".join(f"{i}. {label}" for i, (label, _) in enumerate(buttons, 1)) + "\n\nAnswer with the number."


# -- the calls every chat platform offers ---------------------------------------

def _live():
    bot = _bot[0]
    if not bot or not bot.auth:
        raise ChatError("Teams isn't signed in")
    return bot


def send(auth, chat, text, reply_to=None):
    content = MARK + to_html(text)
    body = {"body": {"contentType": "html", "content": content}}
    if reply_to:
        # A reply quotes the message it answers, as Teams' own Reply does.
        body["body"]["content"] = f'<attachment id="{reply_to}"></attachment>' + content
        body["attachments"] = [{"id": reply_to, "contentType": "messageReference", "content": json.dumps({"messageId": reply_to})}]
    try:
        return graph(auth, "POST", f"/chats/{urllib.parse.quote(chat, safe=':@')}/messages", body)
    except TeamsError as err:
        if not reply_to or err.code != 400:
            raise
    # A quote Teams won't take goes without it.
    return graph(auth, "POST", f"/chats/{urllib.parse.quote(chat, safe=':@')}/messages", {"body": {"contentType": "html", "content": content}})


def post(cred, channel_id, text, reply_to=None, buttons=None, files=None):
    """Post text to a chat, split as needed; the first part quotes `reply_to`, the last lists the options to answer
    by number. Files can't go through a chat as you without OneDrive, so they're named instead. {"id": last part's}."""
    bot, chat, sent = _live(), chat_of(channel_id), None
    if buttons:
        text = f"{text.rstrip()}\n\n{numbered(buttons)}"
    if files:
        names = ", ".join(os.path.basename(str(f[0] if isinstance(f, (tuple, list)) else f)) for f in files)
        text = f"{text.rstrip()}\n\n(Files can't be sent to Teams from here: {names})"
    for i, part in enumerate(chunks(text)):
        sent = send(bot.auth, chat, part, reply_to if i == 0 else None)
        bot.mine(sent.get("id"))
    mid = str((sent or {}).get("id", ""))
    if buttons:
        bot.ask(channel_id, mid, buttons)
    return {"id": mid}


def clear_buttons(cred, channel_id, message_id):
    bot = _bot[0]
    if bot:
        bot.asks.pop((channel_id, str(message_id)), None)


def answer_press(press, text=None, clear=False):
    """A numbered answer taken: the question is closed, and `text` (a notice for whoever answered) is posted."""
    bot = _bot[0]
    if not bot:
        return
    bot.asks.pop((press["channel_id"], str((press.get("message") or {}).get("id", ""))), None)
    if text:
        try:
            post("", press["channel_id"], text, press.get("answer_id"))
        except ChatError:
            pass
    elif press.get("answer_id"):
        react("", press["channel_id"], press["answer_id"], "✅")


def react(cred, channel_id, message_id, emoji):
    bot = _bot[0]
    if not bot or not message_id:
        return
    try:
        graph(bot.auth, "POST", f"/chats/{urllib.parse.quote(chat_of(channel_id), safe=':@')}/messages/{message_id}/setReaction", {"reactionType": emoji})
    except (TeamsError, ChatError):
        pass  # a reaction is a courtesy


def unreact(cred, channel_id, message_id, emoji):
    bot = _bot[0]
    if not bot or not message_id:
        return
    try:
        graph(bot.auth, "POST", f"/chats/{urllib.parse.quote(chat_of(channel_id), safe=':@')}/messages/{message_id}/unsetReaction", {"reactionType": emoji})
    except (TeamsError, ChatError):
        pass


def download(cred, file, limit):
    """An image pasted into the chat ({url, filename}), up to `limit`; None when it's bigger."""
    data = graph(_live().auth, "GET", file["url"], raw=True, limit=limit)
    return None if len(data) > limit else data


# -- reading your chats, by polling ------------------------------------------------------------------

def parse_command(text):
    """("sub", "rest") for "/peerlink sub", "/sub" or "peerlink sub": Teams' compose box keeps "/" for its own."""
    t = text.strip()
    if re.match(r"(?i)peerlink\s", t + " ") and not t.startswith("/"):
        t = "/" + t
    return telegramlink.parse_command(t) if t.startswith("/") else None


def chat_title(chat, me):
    if chat.get("id") == SELF_CHAT:
        return "Chat with yourself"
    if chat.get("topic"):
        return chat["topic"]
    names = [m.get("displayName") or "?" for m in chat.get("members") or [] if m.get("userId") != me]
    return ", ".join(names[:4]) + (f" +{len(names) - 4}" if len(names) > 4 else "") if names else "Chat"


class Bot:
    """Polls the chats `watch()` names, in a thread of its own, retrying with backoff; outward calls are Graph calls.

    `on_message(msg)` gets each message from you, or someone allowed, that peerlink didn't post: {channel_id
    ("teams:<chat>"), message_id, author, author_id, text, dm, mentioned, ref, files, bot}, as discordlink's Bot
    reports Discord's.
    """

    reads_messages = True

    def __init__(self, auth, on_message, log, on_press=None, watch=None):
        self.auth = auth
        self.on_message = on_message
        self.on_press = on_press or (lambda press: None)
        self.log = log
        self.watch = watch or (lambda: [])
        self.state, self.error = "connecting", ""
        self.user, self.user_id = "", ""
        self.allowed_users = set()
        self.trusted_bots = set()
        self.chats = {}  # "teams:<id>" -> title, your chats, for the mapping's picker
        self.on_command = lambda cmd: ("", None)
        self.on_autocomplete = lambda cmd: []
        self.on_chat = lambda channel_id, title: None
        self.on_stranger = lambda who: None
        self.since = {}  # chat id -> the newest change read there
        self.began = {}  # chat id -> when it was first read: what was written before is history, not news
        self.seen = {}  # message id -> when, so an edited message isn't read again
        self.posted = {}  # message id -> when, the posts peerlink made
        self.asks = {}  # (channel id, message id) -> [(label, id)]: open questions, answered by number
        self.stop = threading.Event()
        self.chats_at = 0.0
        _bot[0] = self
        self.thread = threading.Thread(target=self._run, name="teams", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if _bot[0] is self:
            _bot[0] = None

    def channels_live(self):
        chans = sorted(({"id": cid, "name": name} for cid, name in self.chats.items()), key=lambda c: c["name"].lower())
        return [{"id": "teams", "name": "Teams", "channels": chans}]

    def channel_name(self, channel_id):
        return self.chats.get(channel_id, "")

    def mine(self, mid):
        if mid:
            self.posted[str(mid)] = time.time()

    def ask(self, channel_id, mid, buttons):
        self.asks[(channel_id, mid)] = list(buttons)
        while len(self.asks) > 50:
            self.asks.pop(next(iter(self.asks)))

    def _run(self):
        backoff = 2
        while not self.stop.is_set():
            try:
                me = graph(self.auth, "GET", "/me?$select=id,displayName")
                self.user, self.user_id = me.get("displayName", "?"), me.get("id", "")
                self.state, self.error = "online", ""
                self.log("teams", "online as", self.user)
                backoff = 2
                while not self.stop.is_set():
                    if time.time() - self.chats_at > CHATS_S:
                        self._load_chats()
                    for cid in list(self.watch()):
                        if self.stop.is_set():
                            return
                        try:
                            self._poll(chat_of(cid))
                        except TeamsError as err:
                            if err.code in (401, 429) or not err.code:
                                raise
                            self.log("teams", "couldn't read", cid, err)
                    self._forget()
                    if self.stop.wait(POLL_S):
                        return
            except TeamsError as err:
                if self.stop.is_set():
                    return
                self.log("teams", "connection dropped:", err)
                if err.code == 401:
                    self.state, self.error = "error", f"Teams signed you out ({err}); sign in again."
                    return
                self.state, self.error = ("error" if err.code else "connecting"), str(err)
                if err.retry_after:
                    backoff = max(backoff, err.retry_after)
            except Exception as err:  # noqa: BLE001 - one bad poll must not stop the channel
                self.log("teams", "poll failed:", err)
            if self.stop.wait(backoff):
                return
            backoff = min(backoff * 2, 120)

    def _load_chats(self):
        self.chats_at = time.time()
        found, url = {}, "/me/chats?$expand=members&$top=50"
        for _ in range(4):
            out = graph(self.auth, "GET", url)
            for c in out.get("value") or []:
                found[channel(c["id"])] = chat_title(c, self.user_id)
            url = out.get("@odata.nextLink")
            if not url:
                break
        try:
            graph(self.auth, "GET", f"/chats/{SELF_CHAT}/messages?$top=1")
            found[channel(SELF_CHAT)] = "Chat with yourself"
        except TeamsError:
            pass  # not every tenant reaches it through Graph
        for cid, title in found.items():
            if self.chats.get(cid) != title:
                self.chats[cid] = title
                self.on_chat(cid, title)

    def _forget(self):
        old = time.time() - 6 * 3600
        for d in (self.seen, self.posted):
            for k in [k for k, t in d.items() if t < old]:
                d.pop(k, None)

    def _poll(self, chat):
        # A chat newly mapped is read from now on, not from its start.
        start = self.began.setdefault(chat, time.time() - 5)
        since = self.since.setdefault(chat, start)
        q = (f"$top=50&$orderby={urllib.parse.quote('lastModifiedDateTime desc')}"
             f"&$filter={urllib.parse.quote(f'lastModifiedDateTime gt {iso(since)}')}")
        out = graph(self.auth, "GET", f"/chats/{urllib.parse.quote(chat, safe=':@')}/messages?{q}")
        msgs = sorted(out.get("value") or [], key=lambda m: m.get("createdDateTime") or "")
        for m in msgs:
            self.since[chat] = max(self.since[chat], when(m.get("lastModifiedDateTime")))
            try:
                self._message(chat, m, start)
            except Exception as err:  # noqa: BLE001 - one bad message must not stop the others
                self.log("teams", "message failed:", err)

    def _message(self, chat, m, start):
        mid = str(m.get("id") or "")
        if not mid or mid in self.seen or m.get("messageType") != "message" or m.get("deletedDateTime"):
            return
        self.seen[mid] = time.time()
        # An edit to a message from before peerlink started, or a post of its own, isn't news.
        if when(m.get("createdDateTime")) < start or mid in self.posted or ours(m):
            return
        user = (m.get("from") or {}).get("user") or {}
        if not user.get("id"):
            return  # a bot's or an app's
        cid, aid, who = channel(chat), user["id"], user.get("displayName") or "someone"
        text = to_text((m.get("body") or {}).get("content", ""))
        if aid != self.user_id and aid not in self.allowed_users:
            if text:
                self.on_stranger({"id": aid, "name": who, "username": "", "chat": cid, "chat_name": self.chats.get(cid, "")})
            return
        ref, files, shared = "", [], []
        for a in m.get("attachments") or []:
            if a.get("contentType") == "messageReference":
                ref = str(a.get("id") or "")
            elif a.get("contentType") == "reference":
                shared.append(a.get("name") or "a file")
        for i, src in enumerate(re.findall(r'<img[^>]+src="([^"]+/hostedContents/[^"]+)"', (m.get("body") or {}).get("content", ""))):
            files.append({"url": html.unescape(src), "filename": f"image-{mid}-{i + 1}.png", "size": 0})
        if shared:
            text = (text + "\n\n" if text else "") + f"(Shared from OneDrive or SharePoint, which peerlink can't open: {', '.join(shared)})"
        command = parse_command(text)
        if command:
            sub, rest = command
            return threading.Thread(target=self._guarded, args=(self._command, {
                "sub": sub, "options": telegramlink.command_options(sub, rest), "focused": "", "channel_id": cid, "dm": False,
                "author": who, "message_id": mid}), daemon=True).start()
        if self._answer(cid, mid, ref, text, who):
            return
        if not text and not files:
            return
        self.on_message({"channel_id": cid, "message_id": mid, "guild_id": "", "author": who, "author_id": aid, "text": text,
                         "dm": False, "mentioned": True, "ref": ref, "files": files, "bot": False})

    def _answer(self, cid, mid, ref, text, who):
        """A number (or an option's words) answering an open question here: the quoted one, else the latest."""
        open_ = [(k, v) for k, v in self.asks.items() if k[0] == cid]
        if not open_ or not text:
            return False
        key, buttons = next(((k, v) for k, v in open_ if k[1] == ref), open_[-1])
        t = text.strip().rstrip(".").lower()
        pick = None
        if t.isdigit() and 1 <= int(t) <= len(buttons):
            pick = buttons[int(t) - 1]
        else:
            pick = next((b for b in buttons if b[0].strip().lower() == t), None)
        if not pick:
            return False
        threading.Thread(target=self._guarded, args=(self.on_press, {
            "id": mid, "token": "", "custom_id": pick[1], "channel_id": cid, "message": {"id": key[1]},
            "author": who, "answer_id": mid}), daemon=True).start()
        return True

    def _guarded(self, fn, arg):
        try:
            fn(arg)
        except Exception as err:  # noqa: BLE001
            self.log("teams", getattr(fn, "__name__", "handler"), "failed:", err)

    def _command(self, cmd):
        if not cmd["sub"]:
            text, rows = "Say which: " + ", ".join(telegramlink.SUBS) + ". Like: peerlink run loop 5m check the deploy", None
        else:
            try:
                text, rows = self.on_command(cmd)
            except Exception as err:  # noqa: BLE001
                self.log("teams", "command failed:", err)
                text, rows = f"That failed: {err}", None
        post("", cmd["channel_id"], text or "Done.", cmd["message_id"], rows)
