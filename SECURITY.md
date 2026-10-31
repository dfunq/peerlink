# Security policy

peerlink turns messages into prompts that Claude Code runs as you, so security bugs matter here. Thanks for reporting them responsibly.

## Reporting a vulnerability

Please don't open a public issue. Report it privately through [GitHub's vulnerability reporting](https://github.com/dfunq/peerlink/security/advisories/new) for this repository.

Include the peerlink version, your setup (single machine, linked `remotes`, Discord), and steps to reproduce. A proof of concept helps, but keep it to what shows the problem.

You'll get an answer within a week. Fixes ship in the latest version only; there are no backports.

## What counts

Examples of what we want to hear about:

- reaching the daemon's API or web UI without the key, a valid sign-in link or a session cookie
- a web page, DNS rebinding or another origin driving the daemon from a browser
- a peer, Discord user or remote daemon getting past the limits peerlink sets: its space, a mute, the hop limit, approval for starting agents, the Discord allowed-users list
- a remote daemon doing more than peer discovery and message delivery, or getting in without the shared `token`
- files in `~/.peerlink` created readable by other users

## What doesn't

These are by design and covered in [Security](README.md#security) in the README:

- Agents act on messages with your account and their session's permission mode. A peer you let in can ask your agents to do anything those agents can do.
- With `listen_host` set to `0.0.0.0`, the link between machines is plain HTTP, guarded only by the shared `token`.
- Anyone who can read your files as your user can read the key.

A prompt injection in a message that an agent then acts on is a problem in the agent's judgment, not a peerlink vulnerability. If peerlink makes such an injection easier, by hiding where a message came from for example, report it.
