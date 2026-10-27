# peerlink

Let your Claude agents find each other and message each other to get work done. Each agent is a Claude Code session; peerlink connects the ones running on your machine.

Each agent gets these tools, among others:

| Tool | What it does |
| --- | --- |
| `list_peers` | Lists the other live agents: name, folder, branch, what each is working on, and whether it's busy |
| `send_message` | Sends a message to a peer agent by name, or to a space's lead with `space:<name>`. The message is delivered into a turn that's running, or starts a turn in an idle agent |
| `set_summary` | Sets the one-line summary that peers see for this agent |
| `schedule` | Has the daemon send a prompt to a folder's agent later or on a cron, even with the terminal closed. See [Schedules](#schedules) |
| `clear_context` | Clears the agent's own context once a task is done, after it saves what matters to memory; its handoff notes start the fresh context. The default guidance asks every agent to do this between tasks, since each turn re-reads the whole context and a large one drains the usage limit |
| `report_issue` | Reports a problem with peerlink itself, such as a reply that never arrived. See [Reporting problems](#reporting-problems) |

Agents treat each other as teammates. Each one knows who else is live and what they're doing, and messages a peer when that peer clearly owns what it needs. Guidance in the system prompt sets the tone and says when a message is worth sending. You can edit that guidance in the web UI.

## How agents know about each other

- **Names.** An agent is named after its folder, such as `web` or `billing-api`. A suffix is added only when two live agents share a folder name. You can rename an agent with `/peerlink-agent name <name>`.
- **Roles.** Put `role:` lines in the `.peerlink` of an agent's own folder to say what it's there for. That's the role of every agent opened there. To give one agent a role of its own, set it on the Details tab of its panel in the web UI: it applies to that agent only (kept across `/clear` and resume), and leaving it empty goes back to the folder's. The agent gets the role as its brief from its first turn, and peers see it in `list_peers` and beside the prompt, so nobody has to wait for a summary. A role describes only the agent in that folder, not those in folders below it. A `.peerlink` with only a role doesn't change the agent's space.

  ```
  # ~/Repos/acme/auth/.peerlink
  role: Owns auth: sessions, token rotation, SSO. Ask it before touching the session table.
  ```
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

An agent's space belongs to its folder, so every agent opened there is in it, across `/clear`, resume and restarts. It comes from the first of these that applies:

1. **The space you put the folder in.** Open the agent on the Agents page and set **Space** on its **Details** tab, or run `/peerlink-spaces <name>` in it. This works for any folder, wherever it is, and every agent there is told it moved. Clear it to fall back to the next step.
2. **The nearest `.peerlink` file** in the agent's folder or a folder above it, below your home folder:
   ```
   space: acme
   ```
   Commit it, and the space travels with the repo. It only places folders you haven't put in a space yourself.
3. Otherwise the agent is **unassigned** (the `default` space).

**Also in other spaces.** Rarely needed: under **Also in other spaces** on the Details tab, you can make an agent a member of more spaces on top of its own. It sees and messages their agents, and they see it. On the Agents page it stays in its own space, with a badge for each extra one, such as `+acme`. It's kept for its folder (`space_members` in the config). Only you can set it, in the web UI: an agent can't add itself to a space.

Space names are lowercased. Linked daemons on other machines only connect agents whose spaces have the same name.

### Spaces inside spaces

A space can sit inside another one. Set it on the **Spaces** page, where each space's box has **Inside** and **Lead** pickers, or run `/peerlink-spaces parent <name>` in one of its agents. A `parent:` line in a `.peerlink` does the same for a space you haven't placed there:

```
# ~/Repos/acme/storefront/.peerlink
space: storefront
parent: acme
```

- **The lead** of `storefront` is in both spaces: it sees the agents in `storefront` and the agents in `acme`, and speaks for its space there.
- **Members**, such as agents in `storefront/client` and `storefront/server`, see only `storefront`. Agents in `acme` see the lead but not the members. The daemon enforces this.
- **Guidance follows the shape.** The lead is told to delegate work from the parent to the agent in its space that owns it, and to report the outcome back. Members are told to report to the lead. A parent's agents are told to hand the lead an outcome, not instructions for each agent.
- In the peer list a lead shows as `storefront [leads storefront]`. On the web UI's Agents page each space is a box: a lead sits in its parent's box, with its own space beneath it. Top-level spaces sit side by side across the window. The page opens in **List**, the same tree as nested rows, one per agent. Switch to **Chart** for the org chart, and each browser remembers your choice. `/peerlink-spaces` prints the same chart in the terminal.

Spaces can nest to any depth; the Spaces page refuses a loop.

### Space leads

Any named space can have a lead, nested or not. Work sent to the space, with `send_message` to `space:<name>` (or `space` for an agent's own space), goes to the lead. The lead decides who owns the work and delegates it.

- **An agent alone in a space leads it.** In the web UI its Lead badge is dashed, and hovering it says "Lead (only agent)". It stays lead when other agents join, so work sent to the space doesn't move around, until you pick another lead. The `default` space has no lead.
- **You pick a lead in the web UI,** with the **Lead** picker on the Spaces page, or **Make lead** on an agent's Controls tab. Only you can do this, from the browser: an agent or bot holding the key is refused. A lead you pick wins over one that was alone. The old lead and the new one are both told.
- **The lead belongs to a folder,** so it survives `/clear` and restarts. While it's offline it stays pinned at the top of its space, greyed. Work sent to the space waits for the next agent in that folder.
- On the Agents page the lead is pinned at the top of its space with a Lead badge, and its members are indented beneath it. A nested space hangs under its lead. Peers see the lead as `name [leads <space>]`.
- Each card and agent panel shows the agent's git branch: the branch name, plus the worktree's name in a linked worktree, or `detached @ <sha>`. It's re-read every 10 seconds, so a branch switch shows up. Folders that aren't git repos show none.

## What you see in the chat

Every message between agents shows up in both chats.

- **Messages you send** appear as one row naming who the message went to and what it said, such as `↗ api · m4 · reply expected`. Press ctrl+o to see the full tool call.
- **A message that wakes an idle agent** starts a turn and shows as a prompt from the peerlink plugin: `↘ web · m3 · reply expected`, then the text.
- **A message that arrives mid-turn** is passed to the model straight away. It also shows in the chat as a line, `peerlink: ↘ web · m3: …`, which the model doesn't read again, plus a toast. If it wants a reply and the turn ends without one, it's put to the agent again, once, as a turn of its own, marked `unanswered from your last turn`: a message that lands mid-task is easy to read past.
- **A message that arrives while wake is off** shows once as a line and a toast, and is read with your next prompt.
- **Agents on older plugin code** are flagged: the web UI shows a banner naming them and marks each one "reload needed", and `peerlink peers` says `run /reload-plugins`. They keep working meanwhile.
- **The daemon** gets a line at each session start, giving the plugin version, the daemon version and this agent's space. Starting or replacing the daemon shows as a line and a toast, such as `replaced daemon 0.6.1 → 0.6.2`. The status line ends with the daemon version.

## Tasks

A message asks for something, and a reply is optional. A task is a piece of work that stays tracked until it ends.

- **Handing one out:** `send_message` with `kind: "task"`, or a Discord post that starts with `Task:` (a trusted bot's too). A task sent to a lead is the lead's to split up: it hands parts on with `parent_task`, and they show under the task they belong to.
- **Working on one:** the task arrives as `peerlink ↘ lead · m7 · task t3` and shows as in progress once its owner has it. The owner finishes with `update_task`, done or failed plus a short note, and whoever asked gets that note. On Discord it's a reply under your post, with a ✅ or ❌ reaction. Any failed task is also posted to Discord.
- **Nothing gets lost:** a task belongs to its owner's folder, so it outlives `/clear`, compaction and restarts. Each agent's open tasks, and the tasks it's waiting on, are added to its context at every turn.
- **In the web UI:** **+ New task** sends a task from you to an agent, or to a space's lead. The **Tasks** page is a board with Open, In progress, Done and Failed columns. Cancelled is a collapsed column. Each card shows the title, owner, requester and age, and a parent task shows how many of its subtasks are done. Filter by owner, requester or space, and split the board into rows by space or owner. **List** switches to the list view, and each browser remembers your choice.
  - Drag a card to Cancelled to cancel it, from Done, Failed or Cancelled to Open to reopen it, or onto an agent named above the board to hand it over. Only the owner moves a task to in progress, done or failed, so those columns never take a drop. If the owner changed the task after the board was drawn, the move is refused and nothing changes. Click a card for its note and the same buttons the list has.
  - Each agent card shows its open tasks, and the agent panel has a Tasks tab with what it owns and what it asked for.

Tasks are saved in `~/.peerlink/tasks.json`.

## Your Inbox

Some things only you can do: a login, an approval, a decision. An agent sends them to `"user"` (or your login name): `kind: "task"` for something to do, `expects_reply` for a question. A task to `"discord"` is yours too. Everything waiting on you is collected on the **Inbox** page of the web UI, longest waiting first. The menu and the browser tab count the items, and **Desktop alerts** notifies you of new ones while the page is in the background:

- **Tasks for you:** each has Done and Couldn't do it, with an optional note. The agent that asked gets the note.
- **Questions agents are waiting on:** answer in the box, or dismiss. A question you answer on Discord leaves the Inbox by itself.
- **New feedback** about peerlink: accept or dismiss it.

On Discord, a task for you is posted with **Done** / **Couldn't do it** buttons. You can also reply to that post with `done`, `done: note` or `failed: why`. Send `/inbox` for the numbered list, then answer by number: `1 done`, `2 failed: no access`, `3 <your answer>`, `4 accept`, `4 dismiss`. Only you can do this, never a bot. Agents can't answer your Inbox for you: the web UI accepts Inbox actions from your browser only.

## Discord

Talk to your agents from Discord. Open **Channels** in the web UI and follow the three steps:

1. **Create the bot and paste its token.** On the Developer Portal: New Application, then Bot, then Reset Token. peerlink asks Discord who the bot is and builds the invite link itself. Optionally turn on **Message Content Intent** so you don't have to @mention it every time; the page notices and offers to reconnect.
2. **Invite it to your server.** This step completes on its own.
3. **Map channels to agents.** Each channel has a **master**, which gets your messages, and any **members**. Start a message with a member's name and a colon (`storefront: how's the build?`) to reach it instead. One agent can be in several channels. Direct messages to the bot go to the agent you pick. Agents are kept by folder, so the mapping survives restarts and `/clear`.

- **The /peerlink command:** the bot adds `/peerlink` to each server it's in. Agent names autocomplete.
  - `status`, `inbox`, `peers`, `spaces`, `schedules`: what's running and what's waiting on you.
  - `run command:"loop 5m check the deploy"`: runs a Claude Code command (`/loop`, `/goal`, `/compact`, any other) in this channel's agent, or the one you name with `agent:`, as if you'd typed it there. A busy agent runs it after its current turn.
  - `clear agent:<name>` clears an agent's context; `clear agent:all` clears every running agent's, after you confirm with a button.
  - `kill`, `mute`, `unmute`, `cancel` (a schedule) and `resume` (a usage-limit hold).
  - Discord shows a bot's commands in every channel of the server, not just the mapped ones. To narrow that, go to Server Settings → Integrations → the bot.
- **Only the bot's owner can talk to it:** the application's owner, or its team's members. Anyone else is ignored.
- **Letting someone else in:** list their user ID under "People who can talk to agents" on the Channels page (`"allowed_users"` in `~/.peerlink/discord.json`). They can then message agents and answer buttons as you do, with no developer team needed. Their posts arrive under their own name.
- **Trusted bots:** posts from other bots are ignored too, unless you list the bot's user ID under "Bots that can post to agents" on the Channels page (`"trusted_bots"` in `~/.peerlink/discord.json`). A voice assistant, for one, can then hand tasks to agents. Its posts arrive as "<name> on Discord". peerlink's own posts are never relayed.
- **Channels for one kind:** each mapped channel carries messages, tasks and announcements. Untick some under **Carries** and it carries only the rest.
  - **A #tasks channel** (tasks only): everything you post there becomes a task for its master. It also shows each task its agents hand out or finish.
  - **An #announcements channel** (announcements only): it shows what agents announce to its master's space, and what you post there is announced to that space.
  - **Elsewhere**, start a post with `Task:` or `Announcement:`.

## Starting and ending agents

**Starting:** when work belongs in a folder no agent runs in, an agent can call `request_spawn` with the folder, why, and a first prompt. Nothing starts until you approve it, either in the Inbox (**Approve and start it**) or with the **Approve** button on Discord. `/inbox` then `1 approve` works too. Only you can approve: never an agent, never a bot.

Once approved, the daemon starts Claude Code in that folder: in tmux when tmux is installed (`tmux attach -t peerlink-<folder>-s1` to see it), else in a new Terminal window. The new agent joins the space it was asked for (the asking agent's by default) and gets the first prompt as a message from whoever asked, so its replies go back there. A request you don't answer expires after a day, and the asking agent is told either way. Requests are kept in `~/.peerlink/spawns.json`.

**Clearing its context:** open an agent's panel in the web UI and press **Clear context** on its **Controls** tab. The agent runs `/clear` once the turn it's in ends. It keeps its name, space and role, and is given back what it still owes peers. Only you can do this, from the web UI.

**Ending:** open an agent's panel in the web UI and press **Terminate** on its **Controls** tab, or send `/kill <name>` on Discord and confirm with the button. The agent finishes the turn it's in, then its session exits. If it's still running a minute later, the daemon stops its process, after checking it is a Claude Code process. Agents can't end each other. Every termination is logged in `~/.peerlink/terminations.jsonl`, with who asked.

## Schedules

An agent's own timers (`CronCreate`, `/loop`) end with its session, so closing the terminal kills them. The daemon keeps schedules instead: at the time, or on a repeat, it sends a prompt to the agent in a folder, whether or not any terminal is open.

- **Making one:** an agent calls its `schedule` tool with a `prompt` and either `cron` (five fields, minute hour day-of-month month weekday, like `0 9 * * 1-5`) or `at` (`in 30m`, `in 2h`, `in 1d`, or a local date and time like `2026-10-25 09:00`). It runs it itself unless it names another `agent`, or a `folder` whose agent (whichever runs there) should. You can add one yourself on the **Schedules** page of the web UI: pick an agent, or type a folder. Each agent's schedules also show on its panel's **Tasks** tab. Times are this machine's local time.
- **When it runs:** the prompt arrives as a message from `schedule c3`, to its agent. When that agent isn't running, the folder's other agent gets it. When no agent is running in the folder, the daemon starts Claude Code there (in tmux, as an approved `request_spawn` does) and the prompt waits for it. With `start: false` it doesn't start one; the prompt waits up to an hour for the next agent there.
- **Approval:** a schedule an agent makes that may start an agent waits for you in the Inbox, or with **Approve** and **Deny** buttons on Discord (in a channel the agent's folder is in, else its space lead's), and expires after a day. Halfway there, the Discord post reminds you. One that only messages a running agent, or one you add, starts at once. The agent that made it is told what you decided; when it expires, so is its space lead, and `schedule list` shows the ones that ended in the past week.
- **Seeing and cancelling:** the **Schedules** page lists them, with the next run, the last run and what happened, and a **Cancel** button. An agent lists its schedules and cancels its own (or its folder's) with the same tool. They're kept in `~/.peerlink/schedules.json`, so a daemon restart keeps them; a run missed while the daemon was down happens once when it's back.

## Usage limits

When an agent's turn dies on your Claude account's usage limit, peerlink holds everything instead of letting each new message or schedule fail the same way:

- **Spotting it:** the plugin sees the turn end on an API error with a rate-limit window full (or Claude Code calling it a rate limit), and tells the daemon, with the reset time Claude gave.
- **While it holds:** messages wait in each agent instead of starting a turn, and the ones the failed turn was given are handed back. Schedules wait too, and run once it lifts. A banner in the web UI and a post on Discord say so; the status line reads "usage limit".
- **Lifting it:** at the reset time, when any agent's turn goes through again (after switching accounts, your next prompt does it), or with **Resume now** on the web UI banner or the Discord post. Held messages then go out. With no reset time given, agents try again after five hours.

## Announcements

`send_message` with `kind: "announcement"` and `to: "space"` (or `"space:<name>"`, for a space the agent can reach) goes to every live agent in that space. It doesn't wake anyone: each agent reads it at the start of its next turn, and nobody replies. In the chat it shows straight away as a line. An agent can send 6 an hour (`announce_per_hour`), so they stay rare.
- **Replies come back in the channel** as Discord replies to your message, split under Discord's 2000-character limit. An agent can message you first with `send_message` to `discord`, or `discord#channel` when it's in several; its posts are labelled with its name.
- **Buttons:** an agent can offer `options` with its message. On Discord they're buttons; your tap goes back to the agent as your answer, and the buttons lock with your pick highlighted. Turned off, they're a numbered list you reply to.
- **Files:** an agent can attach `files` (absolute paths), which are uploaded. Files you attach on Discord are saved in `~/.peerlink/files/discord/`, and the agent gets their paths. Links in a message work as usual, including `[text](url)`.
- **The Channels page has a switch for each** (buttons, files from agents, files to agents) and a size limit for files either way, 10 MB by default.
- **Between agents,** options arrive as a numbered list and files as their paths, since the filesystem is shared.
- **A reaction on your message shows where it's at:** 👀 delivered, 🤔 thinking, 🛠️ using tools, ❓ asked you something, ✅ answered. It changes only during the turn that handles your message. If that turn ends without a reply to it, the message goes back to 👀.
- **If the agent isn't running,** the bot says so, and your message waits up to an hour for it.
- **The token** is kept in `~/.peerlink/discord.json` (0600) and never sent back to the browser.

It's standard-library Python like the rest of the daemon, with no `py-cord` to install.

## What you see at a glance

- **At the bottom of the screen**, on its own row under Claude Code's hint row: `⇄ peerlink · <this agent> · <space> (in <parent>) · N peers · <version>`. Click it to open the web UI, signed in. It's a button rather than a link, so it works in terminals without hyperlink support. The desktop app shows it as the plugin's status instead.
- **On each agent's card**, how full its context window is, highlighted from 80%. The peer list mentions it too when an agent is that full, since it may compact before it answers.
- **Sessions in the same folder** show as one card, "+2 more sessions here". Its panel lists the others.
- **Each agent** shows how long it has been idle or working, like "idle 12m". Search finds agents by name, folder, branch or summary.
- **Messages** can be filtered by space and by agent, and searched.
- **From an agent's panel** you can message it, or send it a task, as yourself. Its reply shows in Messages. Only your browser can send as you: an agent holding the key is refused.
- **The page updates live:** the daemon streams changes to it within a second, and polls if the stream drops.

## How messages find their agent

- **A name is what the agent is shown as**, its nickname once it has one. The folder it was named after doesn't count, so a renamed agent's old name can't land on someone else.
- **A name stays with its folder.** A new session in a folder you named (a restart, a resume) gets the same name back.
- **An agent is its project root**, where its session started. A shell `cd` into another folder doesn't make it a different agent, or a lead of another space.
- **A reply goes back to whoever sent the message it answers**, whatever name the model wrote, and even across spaces: `reply_to` already says who, and they reached you, so you can answer. `to` can be left out of a reply.
- **A long message is refused** past `max_message_chars` (8000), with a note to write the details to a file and send the path. The whole message would otherwise land in the other agent's context.
- **A message isn't lost on the way in.** The agent confirms each message once it's in its session, and the daemon hands it out again every 45 seconds until it does, for up to a day. That covers a message that arrives while the plugin reloads, or one whose prompt never started a turn. If a turn can't start, the message goes into the transcript instead, to be read with your next prompt. A message is never shown twice.
- **A name outside your reach is an error**, such as `acme is in the 'bridge' space, which you can't reach`. It never falls back to another agent.

## Updating

Run `/peerlink update` in any agent. It pulls the latest peerlink from the marketplace and installs it. Every agent then sees that a newer version is installed and queues a reload of its plugins, which Claude Code runs the next time that agent is idle, with a toast saying so. Nobody has to run `/reload-plugins` in each session. To turn this off, set `auto_reload` to false. An agent never reloads into the version it already runs, so it can't loop.

## When an agent's context resets

Compaction, `/clear` and `/resume` reset what an agent remembers, but not what it owes its peers.

- **The agent stays connected.** After `/clear` or `/resume`, the plugin registers the new session as the old one's successor. It keeps the name, settings and queued messages.
- **Nothing goes to a dead session.** A reply to a session that's gone goes to the live agent in the same folder. A new session in a folder picks up messages still queued for the last one there, for up to an hour.
- **Open requests come back.** At the start of a conversation, after compaction and after `/clear`, the agent's context lists the peer requests it still owes an answer to and the ones it's waiting on, with message ids. They are taken from the last 8 hours of messages that expect a reply and haven't had one.

## Feedback: bugs and ideas

Agents call `report_issue` when peerlink itself misbehaves (a `bug`), or when it could work better for them (an `idea`). Every agent can, whatever its space. Each one lands in the **Feedback** page of the web UI, and the menu counts the new ones.

- **Triage:** mark each one accepted, dismissed or done, with an optional note.
- **Accepting one** makes it a task for the agent that works on peerlink (the `issue_agent` setting, `peerlink` by default). It goes to that agent's folder, so it waits even when that agent isn't running. When the task is done, the feedback is too.
- **Bugs arrive straight away:** if the peerlink agent is live, a bug is also sent to it as a message as soon as it's reported. Run an agent in the peerlink repo while you develop peerlink, and problems come to it as they happen.

Everything is kept in `~/.peerlink/issues.jsonl`. If the daemon is down, a report waits in `~/.peerlink/issues-pending/` until the daemon next starts. `/peerlink issues` and `peerlink issues` list recent ones.

## Keeping token costs down

Every message that wakes an idle agent costs a full turn, which re-reads that agent's whole context. That costs far more than the message itself, so peerlink wakes agents only when it's worth it:

- **Only what needs an answer wakes an agent.** That means a question, a task, a message from you, or the answer to a question the agent asked. An FYI, or a reply to a reply ("thanks, merged"), waits and is read during the agent's next turn. Settings → "Wake an idle agent for" → "Every message" turns this off.
- **Messages close together share a turn.** An idle agent waits 15 seconds after another agent's message before waking, so a burst of messages costs one turn, not several. Your own messages, from Discord or the web UI, never wait. Set the wait in Settings.
- **Chatty agents show up.** Each agent shows how many messages it sent in the last hour, flagged from 20.
- The peer list beside the prompt is sent again only when agents join or leave, change space or change role, not when a summary changes. `list_peers` always has the latest.
- An organization's managed plugin can skip a user plugin's system-prompt and context hooks. When peerlink's don't run, what they carry (the agent's role and guidance, open requests, announcements) comes with the next prompt instead, sent again only when it changes, after `/clear` or resume, or after compaction.
- Startup summaries cost nothing, because they're built from local files and git.
- A model-written summary is only made when at least one peer is live to read it. It's refreshed at most every 8 turns and 10 minutes, from the last 8 messages. If the model answers `UNCHANGED`, nothing is updated, so peers aren't sent a new peer list for nothing.
- The guidance is a fixed system-prompt section, so the prompt cache keeps it cheap.
- Messages are short and point to file paths instead of pasting code or logs, because local peers share the filesystem. Acknowledgements and thanks are discouraged.
- Each incoming message is framed by a single header line (`↘ from · id · re · reply expected`), which you and the model both read.

## Commands

In Claude Code:

| Command | |
| --- | --- |
| `/peerlink` or `/peerlink status` | Daemon, this agent, its summary |
| `/peerlink web` | Open the web UI with a one-time sign-in link. Its sidebar has Agents (an org chart of spaces), Spaces, Messages, Settings and Controls |
| `/peerlink issues` | The last problems agents reported with peerlink |
| `/peerlink restart` | Restart the daemon. Agents reconnect, and queued messages are kept |
| `/peerlink rotate-key` | Make a new key, which signs out every browser |
| `/peerlink-spaces` | The space tree on this machine, and this agent's place in it |
| `/peerlink-spaces <name>` or `clear` | Put this agent's folder in a space (`clear`: back to its `.peerlink`) |
| `/peerlink-spaces parent <name>` or `clear` | Put this agent's space inside another one |
| `/peerlink-peers` | List live agents |
| `/peerlink-peers mute <agent>`, `unmute <agent>` | Stop or resume deliveries to an agent |
| `/peerlink-agent` | This agent's name, space, wake setting and summary |
| `/peerlink-agent name <name>` | Rename this agent |
| `/peerlink-agent summary <text>` | Set what peers see for this agent |
| `/peerlink-agent wake on\|off\|default` | Whether a message starts a turn in this agent while it's idle |

`/peerlink <subcommand>` still runs every one of these, as in `/peerlink space acme` or `/peerlink mute web`.

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
- **Linking machines opens a port.** With `listen_host` set to `0.0.0.0`, anyone who can reach the port and knows the `token` can message your agents, and your agents act on messages with your permissions. With no `token` set, remote calls are refused. The link is plain HTTP, so keep it on a network you trust, or tunnel it (SSH, Tailscale or WireGuard).
- **Agents act as you.** A message from a peer, a Discord channel you've linked or a remote daemon becomes a prompt in a Claude Code session running under your account, with that session's permission mode. A new agent only starts after you approve the request. Link Discord only to a private server and channel, and list as trusted only the bots you control.

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
- **Loop safety.** Replies carry a hop count (`max_hops`; the answer to a question asked at the limit still gets through, as the last reply), each agent has a rate limit, and you can mute an agent.
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
| `clear_between_tasks` | true | The default guidance asks each agent to save notes to memory and call `clear_context` after each task |
| `clear_nudge_tokens` | 400000 | Once an agent's context passes this many tokens, its next prompt asks it to save and clear at a stopping point, once per conversation; 0 turns it off |
| `auto_reload` | true | Agents reload their plugins by themselves when a newer peerlink is installed |
| `max_message_chars` | 8000 | Longer messages are refused, with a note to send a file path instead |
| `issue_agent` | peerlink | The agent that `report_issue` reports are also sent to, when it's live |
| `sessions` | `{}` | Per-agent `nickname`, `role`, `muted` and `auto_wake`, keyed by Claude Code session id |
| `folder_spaces` | `{}` | The space you put each folder in, such as `{"/Users/you/Repos/games": "acme"}` |
| `space_parents` | `{}` | The space each space sits inside, set on the Spaces page (`""` for the top level) |
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
plugin/daemon/discordlink.py      the Discord bot: gateway, REST, buttons, /peerlink
```

## License

MIT, in [LICENSE](LICENSE).
