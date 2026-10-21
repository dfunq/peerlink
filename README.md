# peerlink

Let your Claude Code sessions find each other and message each other to get work done.

Each session gets three tools:

| Tool | What it does |
| --- | --- |
| `list_peers` | Lists the other live sessions: name, directory, branch, what each is working on, and whether it's busy |
| `send_message` | Sends a message to a peer by name or id. The message is delivered into a busy turn, or wakes an idle session |
| `set_summary` | Sets the one-line summary that peers see for this session |

Guidance in the system prompt tells the model **when** messaging is worth it: asking the session that owns the code, handing off a well-scoped subtask, or warning a peer about a change that affects it. You can edit that guidance in the web UI.

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
| `guidance` | (text) | Text added to every session's system prompt |
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
