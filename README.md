# peerlink

Let your Claude agents find each other and message each other to get work done. Each agent is a Claude Code session; peerlink connects the ones running on your machine.

Each agent gets three tools:

| Tool | What it does |
| --- | --- |
| `list_peers` | Lists the other live agents: name, folder, branch, what each is working on, and whether it's busy |
| `send_message` | Sends a message to a peer agent by name. The message is delivered into a turn that's running, or starts a turn in an idle agent |
| `set_summary` | Sets the one-line summary that peers see for this agent |

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
   Commit it, and the space travels with the repo. `/peerlink-spaces file <name>`, or "Space for this folder" in the web UI, writes it for you: it edits the `.peerlink` that already applies, or creates one at the repo root.
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
- In the peer list a lead shows as `storefront [leads storefront]`. The web UI draws spaces as a tree.

Spaces can nest to any depth. `/peerlink-spaces parent <name>` writes the key for you, and `/peerlink-spaces` prints the tree. An agent with a space override is never a lead.

## What you see in the chat

Every message between agents shows up in both chats.

- **Messages you send** appear as one row naming who the message went to and what it said, such as `↗ api · m4 · reply expected`. Press ctrl+o to see the full tool call.
- **A message that wakes an idle agent** starts a turn and shows as a prompt from the peerlink plugin: `↘ web · m3 · reply expected`, then the text.
- **A message that arrives mid-turn** is passed to the model straight away. It also shows in the chat as a line, `peerlink: ↘ web · m3: …`, which the model doesn't read again.

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
| `/peerlink web` | Open the web UI with a one-time sign-in link. Its sidebar has Agents (drawn as a space tree), Spaces, Messages, Settings and Controls |
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
peerlink peers
peerlink messages     # the last 20
peerlink mute <agent> / unmute <agent>
peerlink config [name [value]]
peerlink start | stop | restart | logs
peerlink rotate-key | logout
```

## Security

- The daemon listens on `127.0.0.1` only. Every API call has to carry the key in `~/.peerlink/key`, a 0600 file that's created on first start. Agents send it as `X-Peerlink-Key`.
- **The browser** gets in through a one-time link from `peerlink web`, signed with the key using HMAC-SHA256. The link is valid for 60 seconds and works once. It sets an `HttpOnly`, `SameSite=Strict` session cookie that lasts 12 hours.
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

- **One daemon per machine.** `plugin/daemon/peerlinkd.py` is Python stdlib only. Holding the TCP port is the lock, so a second copy exits. The first agent that starts launches the daemon. Every agent restarts it if it goes away, and replaces it if it's an older version.
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
| `sessions` | `{}` | Per-agent `nickname`, `muted`, `auto_wake` and `space`, keyed by Claude Code session id |
| `space_rules` | `[]` | Folder rules, such as `[{"path": "~/Repos/acme", "space": "acme"}]` |
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
