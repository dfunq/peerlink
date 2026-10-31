# Contributing to peerlink

Thanks for helping. Bug reports, ideas and pull requests are all welcome.

## Reporting a bug or an idea

Open an [issue](https://github.com/dfunq/peerlink/issues). For a bug, include:

- the peerlink version (`/peerlink` in any agent, or `curl -s localhost:7899/api/health`)
- your OS and Claude Code version
- what you did, what you expected, and what happened
- the relevant lines of `~/.peerlink/daemon.log`, and message ids (`m123`) when a message went astray

Read what you paste first: logs and messages can hold your folder paths and what your agents were saying.

Inside peerlink, an agent's `report_issue` tool files bugs and ideas into your own feedback queue (see [Feedback](README.md#feedback-bugs-and-ideas)). That queue stays on your machine. To reach the maintainers, open an issue.

Security problems go through [SECURITY.md](SECURITY.md), not public issues.

## Setting up

You need Claude Code, `python3` on your `PATH`, and git. tmux is optional; with it, started agents run in tmux sessions.

```
git clone https://github.com/dfunq/peerlink
claude --plugin-dir ~/path/to/peerlink/plugin
```

The first agent that starts launches the daemon from that checkout. To try daemon changes without disturbing the daemon your real agents use, run a second one on its own port and state folder:

```
PEERLINK_HOME=/tmp/peerlink-dev PEERLINK_PORT=7998 python3 plugin/daemon/peerlinkd.py serve
```

## Layout

- `plugin/hooks/register.tsx`: the Claude Code side. Tools, `/peerlink`, polling, delivery, summaries and the guidance agents get.
- `plugin/daemon/peerlinkd.py`: the daemon, CLI and web UI. Standard-library Python only, one file.
- `plugin/daemon/discordlink.py`: the Discord bot.
- `plugin/daemon/peerlink_mcp.py`: the MCP endpoint for clients outside Claude Code.

[How it works](README.md#how-it-works) in the README covers the design.

## Tests

```
claude plugin test plugin                          # the hooks module (register.test.tsx)
python3 -m unittest discover -s plugin/daemon      # the MCP endpoint
```

Both must pass before a change is merged. Add a test for a bug fix where you can. For daemon changes with no unit tests, describe in the pull request how you checked the change against a scratch daemon.

## Making a change

- **Keep the daemon dependency-free.** No pip packages: it has to start anywhere `python3` exists.
- **Several agents can share a folder.** Never assume "the agent in folder X" is unique. Look agents up by session id, or decide which of several you mean.
- **Mind the trust boundary.** Messages from peers, Discord and remote daemons become prompts in a session running as the user. Anything that widens who can send them, or what they can trigger, needs a careful look. See [Security](README.md#security).
- **Write for the people who'll read it.** Tool descriptions and guidance text are read by agents, and UI and Discord text by people on their phones. Keep both short and plain.
- **Update the README** when behaviour, settings or commands change.

## Pull requests

- One change per pull request, with a commit subject that says what changed for users, such as "A reply to a closed thread reopens it".
- Don't bump the version. The maintainer does that at release.
- By contributing, you agree your work is licensed under the [MIT License](LICENSE).

## Releases (maintainers)

The version lives in five places, which must match:

- `VERSION` in `plugin/daemon/peerlinkd.py` and in `plugin/daemon/peerlink_mcp.py`
- `DAEMON_VERSION` in `plugin/hooks/register.tsx`
- `plugin/.claude-plugin/plugin.json`
- the expected versions in `plugin/hooks/register.test.tsx`

Bump them, run both test suites, commit and push. Users get the new version with `/peerlink update`. Running agents replace an older daemon by themselves and reload their plugins when idle.
