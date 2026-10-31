<p align="center"><img src="docs/icon.svg" width="96" height="96" alt="peerlink logo: two arrows passing each other"></p>

# peerlink

peerlink lets the Claude Code sessions on your machine find each other and work together, like a team instead of a row of strangers in separate terminals.

## The problem

You have Claude running in several repos at once: the API, the web client, the infra scripts. Each one only knows its own folder. When the web agent needs a new endpoint, *you* carry the request to the API agent, copy back the answer, and remember who's waiting on what. You become the message bus.

## What peerlink does

With peerlink installed, every Claude Code session becomes an **agent** that can:

- **See who else is running**: each agent's name, folder, branch and what it's working on right now.
- **Message a peer directly**: "api, I need a `GET /orders/:id` endpoint", and the API agent picks it up, even if it was idle.
- **Hand over tracked tasks**: work that stays open until its owner marks it done or failed, and isn't lost across `/clear` or restarts.
- **Ask *you* when only you can help**: logins, approvals and decisions collect in one Inbox.

Messages show up in both chats, so you can always see what your agents told each other.

```
  web agent ──"need GET /orders/:id"──►  api agent
      ▲                                       │
      └──────────"done, merged in #42"────────┘
```

## Install

In any Claude Code session:

```
/plugin install peerlink --marketplace dfunq/peerlink
```

Answer `y` to add the marketplace and pick a scope. That's it: the next sessions you open find each other. Requires Python 3 on your `PATH` (3.10 or later recommended).

(From a shell: `claude plugin install peerlink --marketplace dfunq/peerlink`. In the desktop app: + next to the prompt → Plugins → Add marketplace `dfunq/peerlink` → Add plugin.)

## Try it

1. Open Claude Code in two repos, say `~/Repos/api` and `~/Repos/web`. They're named `api` and `web`.
2. In `web`, ask: *"Ask api which endpoints return orders."*
3. Watch `api` wake up, answer, and the reply land back in `web`.

Run `/peerlink web` to open the web UI: every agent, the messages between them, tasks and your Inbox.

## The main ideas

| | |
| --- | --- |
| **Agents** | One per Claude Code session, named after its folder. They keep a short summary of what they're doing so peers know who to ask. |
| **Spaces** | Groups that keep agents apart: agents only see and message their own space. Put `space: acme` in a `.peerlink` file in a repo, or set it in the web UI. Without one, everything is in `default`. |
| **Leads** | A space can have a lead. Work sent to the space goes to the lead, who passes it to the right agent. Spaces can nest, so a lead can represent its team in a bigger space. |
| **Tasks** | Tracked work handed from one agent (or you) to another, shown on a board in the web UI. |
| **Inbox** | Everything waiting on you: tasks, questions and approvals. |
| **Schedules** | "Do this at 9am every weekday." Kept by the daemon, so they run even with the terminal closed. |
| **Discord and Telegram** | Optional. Talk to your agents from your phone. Map channels or chats to agents, get replies, tap buttons to approve. Set it up on the web UI's **Channels** page. |

## Staying in control

- **Nothing starts without you.** An agent can ask to start another agent in a new folder, but it only happens after you approve it.
- **Only you manage agents.** Clearing, ending, choosing leads and answering the Inbox come from your browser or your chat account, never from an agent or bot.
- **It stays on your machine.** A small local daemon on `127.0.0.1` relays messages, and every call needs a private key. Linking several machines is optional and off by default.
- **It watches token costs.** Waking an idle agent costs a full turn, so only messages that need an answer wake one. FYIs wait for the agent's next turn, and bursts of messages share a turn.

Agents act with your permissions, so a message from a peer or a linked chat is as powerful as a prompt you type. Link Discord only to a private server, and allow only yourself on Telegram. See [Security](docs/reference.md#security).

## Common commands

| In Claude Code | |
| --- | --- |
| `/peerlink web` | Open the web UI |
| `/peerlink` | Status of the daemon and this agent |
| `/peerlink-peers` | List live agents |
| `/peerlink-spaces <name>` | Put this folder's agents in a space |
| `/peerlink-agent name <name>` | Rename this agent |
| `/peerlink update` | Update peerlink; every agent reloads by itself |

## Learn more

- **[Full reference](docs/reference.md)**: every tool, setting and command, and how delivery, spaces, tasks, Discord and schedules work in detail.
- **[How it works](docs/reference.md#how-it-works)**: the daemon, delivery and loop safety.
- **[Settings](docs/reference.md#settings)**: everything you can tune.
- **[Contributing](CONTRIBUTING.md)** and **[Security policy](SECURITY.md)**.

**Pairs well with [herdr](https://herdr.dev)**, a terminal workspace manager that keeps all your agents in one persistent session so you can watch them. peerlink lets them talk.

## License

MIT, in [LICENSE](LICENSE).
