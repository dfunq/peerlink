# peerlink

Let your Claude agents find each other and message each other to get work done. Each agent is a Claude Code session; peerlink connects the ones running on your machine.

Each agent gets four tools:

| Tool | What it does |
| --- | --- |
| `list_peers` | Lists the other live agents: name, folder, branch, what each is working on, and whether it's busy |
| `send_message` | Sends a message to a peer agent by name. The message is delivered into a turn that's running, or starts a turn in an idle agent |
| `set_summary` | Sets the one-line summary that peers see for this agent |
| `report_issue` | Reports a problem with peerlink itself, such as a reply that never arrived. See [Reporting problems](#reporting-problems) |

Agents treat each other as teammates. Each one knows who else is live and what they're doing, and messages a peer when that peer clearly owns what it needs. Guidance in the system prompt sets the tone and says when a message is worth sending. You can edit that guidance in the web UI.

## How agents know about each other

- **Names.** An agent is named after its folder, such as `web` or `billing-api`. A suffix is added only when two live agents share a folder name. You can rename an agent with `/peerlink-agent name <name>`.
- **Summaries.** Each agent has a one-line summary that peers can read.
  - When a new agent starts, it builds a first summary from what's already known about its folder, with no model call:
    - what the last agent in this folder was doing, which the daemon remembers for 14 days
    - otherwise, Claude Code's own title for the most recent session in this folder (`ai-title` in `~/.claude/projects/…`)
    - the project's description from `CLAUDE.md` or the README
    - the last three commit subjects from the past 14 days
  - The first prompt adds the task the agent is working on.
  - After that, a low-effort Haiku call rewrites the summary every few turns.
  - A resumed session summarizes its history as soon as it starts.
- **Peer list.** A short list of live agents is added beside the prompt, but only when that list has changed since the agent last saw it.

## Spaces

Spaces keep groups of agents apart. An agent only sees and messages agents in its own space: the peer list, `list_peers` and `send_message` all stop at the space's edge, and the daemon enforces it. Agents with no space set are in `default`, so nothing changes until you set one.

An agent's space comes from the first of these that applies:

1. **An override for that agent.** Set it with `/peerlink-spaces <name>`, or under Manage on the agent's card in the web UI. It lasts as long as the session's config entry.
2. **The nearest `.peerlink` file** in the agent's folder or a folder above it, below your home folder:
   ```
   space: acme
   ```
   Commit it, and the space travels with the repo. `/peerlink-spaces file <name>`, or "Space for this folder" in the web UI, writes it for you: it edits the repo's own `.peerlink`, or creates one at the repo root. It never rewrites a `.peerlink` above the repo, because that file sets the space for other folders too.
3. **A folder rule**, in the Spaces page of the web UI or `space_rules` in the config. For example, `~/Repos/acme = acme` covers that folder and everything inside it. The most specific rule wins.

Space names are lowercased. Linked daemons on other machines only connect agents whose spaces have the same name.

### Nested spaces

A space can sit inside another one. Add `parent:` to its `.peerlink`:

```
# ~/Repos/acme/storefront/.peerlink
space: storefront
parent: acme
```

- **The lead** is the agent running in the folder that holds that `.peerlink`, here `~/Repos/acme/storefront`. It's in both spaces: it sees the agents in `storefront` and the agents in `acme`.
- **Members**, such as agents in `storefront/client` and `storefront/server`, see only `storefront`. Agents in `acme` see the lead but not the members. The daemon enforces this.
- **Guidance follows the shape.** The lead is told to delegate work from the parent to the agent in its space that owns it, and to report the outcome back. Members are told to report to the lead. A parent's agents are told to hand the lead an outcome, not instructions for each agent.
- In the peer list a lead shows as `storefront [leads storefront]`. The web UI's Agents page is an org chart: each space is a box, a lead sits in its parent's box, and its team hangs beneath it. `/peerlink-spaces` prints the same chart in the terminal.
- Click an agent in the web UI to open its settings: its name, the space for its folder, the space that one sits inside, a space for this agent only, wake and mute. Save changes only what you edited.

Spaces can nest to any depth. `/peerlink-spaces parent <name>` writes the key for you, and `/peerlink-spaces` prints the tree. An agent with a space override is never a lead.

## What you see in the chat

Every message between agents shows up in both chats.

- **Messages you send** appear as one row naming who the message went to and what it said, such as `↗ api · m4 · reply expected`. Press ctrl+o to see the full tool call.
- **A message that wakes an idle agent** starts a turn and shows as a prompt from the peerlink plugin: `↘ web · m3 · reply expected`, then the text.
- **A message that arrives mid-turn** is passed to the model straight away. It also shows in the chat as a line, `peerlink: ↘ web · m3: …`, which the model doesn't read again, plus a toast.
- **A message that arrives while wake is off** shows once as a line and a toast, and is read with your next prompt.
- **Agents on older plugin code** are flagged: the web UI shows a banner naming them and marks each one "reload needed", and `peerlink peers` says `run /reload-plugins`. They keep working meanwhile.
- **The daemon** gets a line at each session start, giving the plugin version, the daemon version and this agent's space. Starting or replacing the daemon shows as a line and a toast, such as `replaced daemon 0.6.1 → 0.6.2`. The status line ends with the daemon version.

## Discord

Talk to your agents from Discord. Open **Channels** in the web UI and follow the three steps:

1. **Create the bot and paste its token.** On the Developer Portal: New Application, then Bot, then Reset Token. peerlink asks Discord who the bot is and builds the invite link itself. Optionally turn on **Message Content Intent** so you don't have to @mention it every time; the page notices and offers to reconnect.
2. **Invite it to your server.** This step completes on its own.
3. **Map channels to agents.** Each channel has a **master**, which gets your messages, and any **members**. Start a message with a member's name and a colon (`storefront: how's the build?`) to reach it instead. One agent can be in several channels. Direct messages to the bot go to the agent you pick. Agents are kept by folder, so the mapping survives restarts and `/clear`.

- **Only the bot's owner can talk to it:** the application's owner, or its team's members. Anyone else is ignored.
- **Trusted bots:** posts from other bots are ignored too, unless you list the bot's user ID under "Bots that can post to agents" on the Channels page (`"trusted_bots"` in `~/.peerlink/discord.json`). A voice assistant, for one, can then hand tasks to agents. Its posts arrive as "<name> on Discord". peerlink's own posts are never relayed.
- **Replies come back in the channel** as Discord replies to your message, split under Discord's 2000-character limit. An agent can message you first with `send_message` to `discord`, or `discord#channel` when it's in several; its posts are labelled with its name.
- **Buttons:** an agent can offer `options` with its message. On Discord they're buttons; your tap goes back to the agent as your answer, and the buttons lock with your pick highlighted. Turned off, they're a numbered list you reply to.
- **Files:** an agent can attach `files` (absolute paths), which are uploaded. Files you attach on Discord are saved in `~/.peerlink/files/discord/`, and the agent gets their paths. Links in a message work as usual, including `[text](url)`.
- **The Channels page has a switch for each** (buttons, files from agents, files to agents) and a size limit for files either way, 10 MB by default.
- **Between agents,** options arrive as a numbered list and files as their paths, since the filesystem is shared.
- **A reaction on your message shows where it's at:** 👀 delivered, 🤔 thinking, 🛠️ using tools, ❓ asked you something, ✅ answered.
- **If the agent isn't running,** the bot says so, and your message waits up to an hour for it.
- **The token** is kept in `~/.peerlink/discord.json` (0600) and never sent back to the browser.

It's standard-library Python like the rest of the daemon, with no `py-cord` to install. The design follows a reference bot.

## What you see at a glance

- **At the bottom of the screen**, on its own row under Claude Code's hint row: `⇄ peerlink · <this agent> · <space> (in <parent>) · N peers · <version>`. Click it to open the web UI, signed in. It's a button rather than a link, so it works in terminals without hyperlink support. The desktop app shows it as the plugin's status instead.
- **On each agent's card**, how full its context window is, highlighted from 80%. The peer list mentions it too when an agent is that full, since it may compact before it answers.
- **Sessions in the same folder** show as one card, "+2 more sessions here". Its panel lists the others.
- **Messages** can be filtered by space and by agent.

## How messages find their agent

- **A name is what the agent is shown as**, its nickname once it has one. The folder it was named after doesn't count, so a renamed agent's old name can't land on someone else.
- **A name stays with its folder.** A new session in a folder you named (a restart, a resume) gets the same name back.
- **An agent is its project root**, where its session started. A shell `cd` into another folder doesn't make it a different agent, or a lead of another team.
- **A reply goes back to whoever sent the message it answers**, whatever name the model wrote, and even across spaces: `reply_to` already says who, and they reached you, so you can answer. `to` can be left out of a reply.
- **A long message is refused** past `max_message_chars` (8000), with a note to write the details to a file and send the path. The whole message would otherwise land in the other agent's context.
- **A name outside your reach is an error**, such as `acme is in the 'bridge' space, which you can't reach`. It never falls back to another agent.

## Updating

Run `/peerlink update` in any agent. It pulls the latest peerlink from the marketplace and installs it. Every agent then sees that a newer version is installed and queues a reload of its plugins, which Claude Code runs the next time that agent is idle, with a toast saying so. Nobody has to run `/reload-plugins` in each session. To turn this off, set `auto_reload` to false. An agent never reloads into the version it already runs, so it can't loop.

## When an agent's context resets

Compaction, `/clear` and `/resume` reset what an agent remembers, but not what it owes its peers.

- **The agent stays connected.** After `/clear` or `/resume`, the plugin registers the new session as the old one's successor. It keeps the name, settings and queued messages.
- **Nothing goes to a dead session.** A reply to a session that's gone goes to the live agent in the same folder. A new session in a folder picks up messages still queued for the last one there, for up to an hour.
- **Open requests come back.** At the start of a conversation, after compaction and after `/clear`, the agent's context lists the peer requests it still owes an answer to and the ones it's waiting on, with message ids. They are taken from the last 8 hours of messages that expect a reply and haven't had one.

## Reporting problems

Agents call `report_issue` when peerlink itself misbehaves. Each report is logged to `~/.peerlink/issues.jsonl`. If an agent named `peerlink` is live (the `issue_agent` setting), the report is also sent to it as a message, whatever space it's in. Run an agent in the peerlink repo while you develop peerlink, and problems come to it as they happen. If the daemon is down, the report waits in `~/.peerlink/issues-pending/` until the daemon next starts. `/peerlink issues` and `peerlink issues` list recent reports.

## Keeping token costs down

- The peer list is only sent when it changes, at about one line per agent.
- Startup summaries cost nothing, because they're built from local files and git.
- A model-written summary is only made when at least one peer is live to read it. It's refreshed at most every 3 turns and 2 minutes, from the last 8 messages. If the model answers `UNCHANGED`, nothing is updated, so peers aren't sent a new peer list for nothing.
- The guidance is a fixed system-prompt section, so the prompt cache keeps it cheap.
- Messages are short and point to file paths instead of pasting code or logs, because local peers share the filesystem. Acknowledgements and thanks are discouraged.
- Each incoming message is framed by a single header line (`↘ from · id · re · reply expected`), which you and the model both read.

## Commands

In Claude Code:

| Command | |
| --- | --- |
| `/peerlink` or `/peerlink status` | Daemon, this agent, its summary |
| `/peerlink web` | Open the web UI with a one-time sign-in link. Its sidebar has Agents (an org chart of spaces and teams), Spaces, Messages, Settings and Controls |
| `/peerlink issues` | The last problems agents reported with peerlink |
| `/peerlink restart` | Restart the daemon. Agents reconnect, and queued messages are kept |
| `/peerlink rotate-key` | Make a new key, which signs out every browser |
| `/peerlink-spaces` | The space tree on this machine, and this agent's place in it |
| `/peerlink-spaces <name>` or `clear` | Override the space for this agent only |
| `/peerlink-spaces file <name>` or `clear` | Set the space in this project's `.peerlink` |
| `/peerlink-spaces parent <name>` or `clear` | Nest this project's space inside another one |
| `/peerlink-peers` | List live agents |
| `/peerlink-peers mute <agent>`, `unmute <agent>` | Stop or resume deliveries to an agent |
| `/peerlink-agent` | This agent's name, space, wake setting and summary |
| `/peerlink-agent name <name>` | Rename this agent |
| `/peerlink-agent summary <text>` | Set what peers see for this agent |
| `/peerlink-agent wake on\|off\|default` | Whether a message starts a turn in this agent while it's idle |

`/peerlink <subcommand>` still runs every one of these, as in `/peerlink space file acme` or `/peerlink mute web`.

In a shell, the daemon writes a launcher to `~/.peerlink/bin/peerlink`. You can add that folder to your `PATH`.

```
peerlink web          # open the UI (--print to only print the link)
peerlink status
peerlink issues       # problems agents reported
peerlink peers
peerlink messages     # the last 20
peerlink mute <agent> / unmute <agent>
peerlink config [name [value]]
peerlink start | stop | restart | logs
peerlink rotate-key | logout
```

## Security

- The daemon listens on `127.0.0.1` only. Every API call has to carry the key in `~/.peerlink/key`, a 0600 file that's created on first start. Agents send it as `X-Peerlink-Key`.
- **The browser** gets in through a one-time link from `peerlink web`, signed with the key using HMAC-SHA256. The link is valid for 60 seconds and works once. It sets an `HttpOnly`, `SameSite=Strict` session cookie that lasts 12 hours. Sign-ins are kept in `~/.peerlink/web_sessions.json` (0600), so restarting the daemon doesn't sign you out.
- **Other web pages** can't drive the daemon. Requests whose `Host` header isn't `127.0.0.1` or `localhost` are refused, which blocks DNS rebinding. Browser writes from another origin are refused too.
- **Rotating the key** signs out every browser and makes outstanding links useless. Running agents pick up the new key by themselves.
- **Remote daemons** (optional) only get peer discovery and message delivery, and only with the shared `token`.

## How it works

```
 Claude agent A ─┐                        ┌─ Claude agent B
   (mod: tools,  │  HTTP 127.0.0.1:7899   │
    poll, deliver)├──────► peerlinkd ◄─────┤
 Claude agent C ─┘   one per machine      └─ ...
                     registry · inboxes · policy · web UI
```

- **One daemon per machine.** `plugin/daemon/peerlinkd.py` is Python stdlib only. Holding the TCP port is the lock, so a second copy exits. The first agent that starts launches the daemon. Every agent restarts it if it goes away, and replaces it if it's an older version. When an agent loads or reloads the plugin (`/reload-plugins`), it also replaces a daemon of the same version that's running different code, going by a fingerprint of `peerlinkd.py`. A newer daemon is never replaced.
- **Agents** register on start and send a heartbeat every `poll_interval_ms`. The heartbeat reply carries the agent's inbox and its current config, so changes made in the UI apply within a few seconds without a restart.
- **Delivery.** If an agent is mid-turn, the message is appended to the running turn. If the agent is idle, the message starts a new turn (set by `auto_wake_idle`). Messages are logged to `~/.peerlink/messages.jsonl`, and undelivered ones survive a daemon restart.
- **Loop safety.** Replies carry a hop count (`max_hops`), each agent has a rate limit, and you can mute an agent.
- **Several machines (optional).** List other daemons under `remotes`, set `listen_host` to `0.0.0.0`, and set a shared `token`. Agents on those machines then show up as `name@host`.

## Install

At the prompt of a Claude Code session in a terminal:

```
/plugin install peerlink --marketplace dfunq/peerlink
```

Answer `y` to add the marketplace, then pick a scope. From a shell:

```
claude plugin install peerlink --marketplace dfunq/peerlink
```

In the desktop app: click + next to the prompt box, then Plugins, then Add marketplace with `dfunq/peerlink`, then Add plugin.

This repository is private. It only installs for people who can read it on GitHub and whose machine already has a git credential for github.com (an SSH key, or `gh auth login` followed by `gh auth setup-git`).

For local development:

```
claude --plugin-dir ~/Repos/peerlink/plugin
```

Requires `python3` on your `PATH`.

## Settings

You can change these in the web UI, with `peerlink config <name> <value>`, or in `~/.peerlink/config.json`. The daemon's log is at `~/.peerlink/daemon.log`.

| Setting | Default | |
| --- | --- | --- |
| `poll_interval_ms` | 3000 | Heartbeat and inbox poll interval |
| `peer_ttl_s` | 30 | An agent is shown as gone after this long without a heartbeat |
| `auto_wake_idle` | true | Start a turn in an idle agent when a message arrives |
| `inject_while_busy` | true | Deliver into a running turn |
| `max_hops` | 6 | Maximum length of a reply chain |
| `rate_limit_per_min` | 12 | Messages per agent per minute |
| `roster_in_prompt` | true | Add the live agent list beside the prompt when it changes |
| `auto_summary` | true | Keep summaries current with a cheap model call |
| `summary_model` | haiku | Model used for summaries |
| `guidance` | empty (built-in text) | Text added to every agent's system prompt |
| `auto_reload` | true | Agents reload their plugins by themselves when a newer peerlink is installed |
| `max_message_chars` | 8000 | Longer messages are refused, with a note to send a file path instead |
| `issue_agent` | peerlink | The agent that `report_issue` reports are also sent to, when it's live |
| `sessions` | `{}` | Per-agent `nickname`, `muted`, `auto_wake` and `space`, keyed by Claude Code session id |
| `space_rules` | `[]` | Folder rules, such as `[{"path": "~/Repos/acme", "space": "acme"}]` |
| `folder_names` | `{}` | The name each folder's agents get, kept when you name an agent |
| `remotes`, `listen_host`, `token` | | Settings for linking daemons across machines |

`PEERLINK_PORT` and `PEERLINK_HOME` override the port and the data directory.

## Layout

```
.claude-plugin/marketplace.json   makes this repo installable
plugin/.claude-plugin/plugin.json
plugin/hooks/hooks.json           points at the hooks module
plugin/hooks/register.tsx         tools, /peerlink, polling, delivery, summaries, guidance, chat rows
plugin/hooks/register.test.tsx    tests (claude plugin test plugin)
plugin/daemon/peerlinkd.py        daemon, CLI and web UI
```
