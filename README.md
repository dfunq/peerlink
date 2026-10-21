# peerlink

Let your Claude Code sessions find each other and message each other to get work done.

Each session gets three tools:

| Tool | What it does |
| --- | --- |
| `list_peers` | Lists the other live sessions: name, directory, branch, what each is working on, and whether it's busy |
| `send_message` | Sends a message to a peer by name or id. The message is delivered into a busy turn, or wakes an idle session |
| `set_summary` | Sets the one-line summary that peers see for this session |

Sessions treat each other as teammates. Each one knows who else is live and what they're doing, and messages a peer when that peer clearly owns what it needs. Guidance in the system prompt sets the tone and says when a message is worth sending. You can edit that guidance in the web UI.

## How sessions know about each other

- **Names.** A session is named after its folder, such as `web` or `billing-api`. A suffix is added only when two live sessions share a folder name.
- **Summaries.** Each session has a one-line summary that peers can read:
  - A new session starts with its project's own description, taken from `CLAUDE.md` or the README, with no model call.
  - The first prompt adds the task the session is working on.
  - After that, a low-effort Haiku call rewrites the summary every few turns.
  - A resumed session summarizes its history as soon as it starts.
- **Peer list.** A short list of live peers is added beside the prompt, but only when that list has changed since the session last saw it.

## Keeping token costs down

- The peer list is only sent when it changes, at about one line per peer.
- A summary is only written when at least one peer is live to read it. It's refreshed at most every 3 turns and 2 minutes, from the last 8 messages. If the model answers `UNCHANGED`, nothing is updated, so peers aren't sent a new peer list for nothing.
- The guidance is a fixed system-prompt section, so the prompt cache keeps it cheap.
- Messages are short and point to file paths instead of pasting code or logs, because local peers share the filesystem. Acknowledgements and thanks are discouraged.
- Message framing is a single tag carrying `id`, `from` and an optional `re` and `reply`.

## How it works

```
 Claude session A ─┐                        ┌─ Claude session B
   (mod: tools,    │  HTTP 127.0.0.1:7899   │
    poll, deliver) ├──────► peerlinkd ◄─────┤
 Claude session C ─┘   one per machine      └─ ...
                       registry · inboxes · policy · web UI
```

- **One daemon per machine.** `plugin/daemon/peerlinkd.py` is Python stdlib only. Holding the TCP port is the lock, so a second copy exits. The first session that starts launches the daemon (`--detach`), and every session restarts it if it goes away.
- **Sessions** register on start and send a heartbeat every `poll_interval_ms`. The heartbeat reply carries the session's inbox and its current config, so changes made in the UI apply within a few seconds without a restart.
- **Delivery.** If a session is mid-turn, the message is appended to the running turn. If the session is idle, the message starts a new turn (set by `auto_wake_idle`).
- **Loop safety.** Replies carry a hop count (`max_hops`), each session has a rate limit, and you can mute a session from the UI.
- **Several machines (optional).** List other daemons under `remotes`, set `listen_host` to `0.0.0.0`, and set a shared `token`. Peers on those machines then show up as `name@host`.

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

## Web UI and config

Run `/peerlink` in any session, or open <http://127.0.0.1:7899>. The UI shows live sessions, the message log, and the settings. Settings are stored in `~/.peerlink/config.json`, and the daemon's log is at `~/.peerlink/daemon.log`.

| Setting | Default | |
| --- | --- | --- |
| `poll_interval_ms` | 3000 | Heartbeat and inbox poll interval |
| `peer_ttl_s` | 30 | A session is shown as gone after this long without a heartbeat |
| `auto_wake_idle` | true | Start a turn in an idle session when a message arrives |
| `inject_while_busy` | true | Deliver into a running turn |
| `max_hops` | 6 | Maximum length of a reply chain |
| `rate_limit_per_min` | 12 | Messages per session per minute |
| `roster_in_prompt` | true | Add the live peer list beside the prompt when it changes |
| `auto_summary` | true | Keep summaries current with a cheap model call |
| `summary_model` | haiku | Model used for summaries |
| `guidance` | empty (built-in text) | Text added to every session's system prompt |
| `sessions` | `{}` | Per-session `nickname`, `muted` and `auto_wake` |
| `remotes`, `listen_host`, `token` | | Settings for linking daemons across machines |

Daemon CLI:

```
python3 plugin/daemon/peerlinkd.py            # run in the foreground
python3 plugin/daemon/peerlinkd.py --detach   # start in the background unless one is already running
python3 plugin/daemon/peerlinkd.py --status
python3 plugin/daemon/peerlinkd.py --stop
```

`PEERLINK_PORT` and `PEERLINK_HOME` override the port and the data directory.

## Layout

```
.claude-plugin/marketplace.json   makes this repo installable
plugin/.claude-plugin/plugin.json
plugin/hooks/hooks.json           points at the hooks module
plugin/hooks/register.ts          tools, polling, delivery, system-prompt guidance
plugin/daemon/peerlinkd.py        daemon and web UI
```
