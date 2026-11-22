# peerlink reference

Everything peerlink does, in detail. For what it is and how to start, see the [README](../README.md).

## Tools each agent gets

| Tool | What it does |
| --- | --- |
| `list_peers` | Lists the other live agents: name, folder, branch, what each is working on, and whether it's busy |
| `send_message` | Sends a message to a peer agent by name, to a space's lead with `space:<name>`, to your Inbox with `user`, or to Discord. The message is delivered into a turn that's running, or starts a turn in an idle agent. With `kind` it can hand over a [task](#tasks) or make an [announcement](#announcements) to a whole space. `options` become buttons on Discord, and `files` attaches files |
| `update_task` | Marks a task the agent owns as in progress, done or failed, with a note that goes back to whoever asked. Whoever handed out a task can cancel it. See [Tasks](#tasks) |
| `close_thread` | Drops requests from the agent's open threads that are already handled, such as a question answered in the terminal instead of on Discord |
| `set_summary` | Sets the one-line summary that peers see for this agent |
| `request_spawn` | Asks you to start a new agent in another folder, with a first prompt and optionally a name, role and space. Nothing starts until you approve. With `worker: true` it hires a worker for one piece of work. See [Starting and ending agents](#starting-and-ending-agents) |
| `schedule` | Has the daemon send a prompt to an agent later or on a cron, even with the terminal closed, and lists or cancels schedules. See [Schedules](#schedules) |
| `clear_context` | Clears the agent's own context once a task is done, after it saves what matters to memory; its handoff notes start the fresh context, and a turn starts on them at once, so the next piece of work begins without anyone writing to it. The default guidance asks every agent to do this between tasks, since each turn re-reads the whole context and a large one drains the usage limit |
| `report_issue` | Reports a bug in peerlink itself, such as a reply that never arrived, or an idea for improving it. Only when the `feedback` setting is on; see [Feedback: bugs and ideas](#feedback-bugs-and-ideas) |
| `rename` | Changes the agent's own name, such as when it shares a folder with another agent. Kept across `/clear` |
| `retire` | For a worker: ends its own session once its work is done and its tasks are closed. For the lead that hired it: ends a stuck worker |
| `link` | For a [space lead](#space-leads): links one of its agents with an agent in another space once that space's lead agrees, so the two work together directly. See "Links leads broker" under Spaces |
| `set_role` | Sets the agent's own role, such as when you ask it to, or, for a [space lead](#space-leads), the role of an agent in its space or a space inside it. Kept across `/clear`, and another agent whose role is set is told |

`rename` and `set_role` are deferred: Claude Code loads them when an agent needs them, so they don't take up room in every prompt. `report_issue` is for people developing peerlink, so it's off unless you turn it on.

Agents treat each other as teammates. Each one knows who else is live and what they're doing, and messages a peer when that peer clearly owns what it needs. Guidance in the system prompt sets the tone and says when a message is worth sending. You can edit that guidance in the web UI.

## How agents know about each other

- **Names.** An agent is named after its folder, such as `web` or `billing-api`. A suffix is added only when two live agents share a folder name. You can rename an agent with `/peerlink-agent name <name>`. Renaming one in the web UI names its folder too, so the next agent there takes the name, but not while other agents are live in that folder: then the folder keeps its name, and a chat channel mapped to the folder still reaches the agent going by it.
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

**Linked agents.** To let two agents talk without changing anyone's space, open one on the Agents page and list the other under **Linked with other agents** on its Details tab. The two then see each other in `list_peers` and message each other, whatever spaces they're in, and both are told. Nobody else's reach changes, and announcements still go by space. A link is kept for the agents (`agent_links` in the config), so it lasts through `/clear` and restarts. It works between agents on this machine. On the Agents page each shows a badge such as `↔core`.

**Links leads broker.** A [space lead](#space-leads) can link one of its agents (in its space or a space inside it) with an agent in another space, using the `link` tool. The other agent's lead agrees by calling `link` from its side, and only then are the two linked. A lead over both links them at once. Both agents are told who the other is and why, and a short line is posted to your channel. A link made for a task ends when that task closes, and either lead can remove one with `remove: true`. A link you set in the web UI stays yours: leads can't remove it, and you can remove any link there. An ask nobody agrees to lapses after a day, or when the daemon restarts.

**Deleting a space.** Press **Delete space** in its box on the Spaces page. Every setting that names it goes: its folders and agents fall back to their `.peerlink` space, else unassigned, its lead is cleared, and spaces inside it move to the top. A running agent whose `.peerlink` still names the space stays in it, and you're told which folders those are.

Space names are lowercased. Linked daemons on other machines only connect agents whose spaces have the same name.

A git worktree takes the space of its main checkout's `.peerlink`, wherever the worktree sits. So `~/Repos/app-wt-fix` is in app's space, not in that of a `.peerlink` above `~/Repos`. The role lines in that file stay with the main checkout's agent.

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
3. **Map channels to agents.** Each channel lists agents, each ticked to **Receive**, **Send**, or both. Each message goes to every agent that receives, and each answers on its own; start one with a receiving agent's name and a colon (`storefront: how's the build?`) or reply to one of its posts to reach that agent alone. A task (`Task: ...`) goes to the first agent that receives, since a task has one owner. An agent that sends can post its messages and updates there unprompted, and answers you there. One that receives but doesn't send reads along without answering, to keep notes or records while another agent handles it; it answers there only when you name it or reply to its post. One agent can be in several channels. Direct messages to the bot go to the agent you pick. Agents are kept by folder, so the mapping survives restarts and `/clear`.

- **The /peerlink command:** the bot adds `/peerlink` to each server it's in. Agent names autocomplete.
  - `status`, `inbox`, `peers`, `spaces`, `schedules`: what's running and what's waiting on you.
  - `run command:"loop 5m check the deploy"`: runs a Claude Code command (`/loop`, `/goal`, `/compact`, any other) in this channel's agent, or the one you name with `agent:`, as if you'd typed it there. A busy agent runs it after its current turn.
  - `clear agent:<name>` clears an agent's context; `clear agent:all` clears every running agent's, after you confirm with a button.
  - `kill`, `mute`, `unmute`, `cancel` (a schedule) and `resume` (a usage-limit hold).
  - Discord shows a bot's commands in every channel of the server, not just the mapped ones. To narrow that, go to Server Settings → Integrations → the bot.
- **Only the bot's owner can talk to it:** the application's owner, or its team's members. Anyone else is ignored.
- **Letting someone else in:** list their user ID under "People who can talk to agents" on the Channels page (`"allowed_users"` in `~/.peerlink/discord.json`). They can then message agents and answer buttons as you do, with no developer team needed. Their posts arrive under their own name.
- **Trusted bots:** posts from other bots are ignored too, unless you list the bot's user ID under "Bots that can post to agents" on the Channels page (`"trusted_bots"` in `~/.peerlink/discord.json`). A voice assistant, for one, can then hand tasks to agents. Its posts arrive as "<name> on Discord". peerlink's own posts are never relayed.
- **Tasks and announcements:** start a post with `Task:` to hand it to the receiving agent as a task, or `Announcement:` to tell its whole space. (Before 0.8.36 a channel could be limited to one kind; a mapping from then is read as agents that both receive and send.)
- **Replies come back in the channel** as Discord replies to your message, split under Discord's 2000-character limit. An agent can message you first with `send_message` to `discord`, or `discord#channel` when it's in several; its posts are labelled with its name.
- **Buttons:** an agent can offer `options` with its message. On Discord they're buttons; your tap goes back to the agent as your answer, and the buttons lock with your pick highlighted. Turned off, they're a numbered list you reply to.
- **Files:** an agent can attach `files` (absolute paths), which are uploaded. Files you attach on Discord are saved in `~/.peerlink/files/discord/`, and the agent gets their paths. Links in a message work as usual, including `[text](url)`.
- **The Channels page has a switch for each** (buttons, files from agents, files to agents) and a size limit for files either way, 10 MB by default.
- **Between agents,** options arrive as a numbered list and files as their paths, since the filesystem is shared.
- **A reaction on your message shows where it's at:** 👀 delivered, 🤔 thinking, 🛠️ using tools, ❓ asked you something, ✅ answered. It changes only during the turn that handles your message. If that turn ends without a reply to it, the message goes back to 👀.
- **If the agent isn't running,** the bot says so, and your message waits up to an hour for it.
- **The token** is kept in `~/.peerlink/discord.json` (0600) and never sent back to the browser.

It's standard-library Python like the rest of the daemon, with no `py-cord` to install.

## Telegram

Talk to your agents from Telegram, in a private chat with your bot or a group it's in. Open **Channels** in the web UI:

1. **Create the bot and paste its token.** Message [@BotFather](https://t.me/BotFather), send `/newbot`, pick a name and a username, and paste the token it gives you.
2. **Message it, then allow yourself.** Anyone can find a Telegram bot by name, so peerlink ignores everyone until you allow them. Send your bot any message: you show up on the Channels page under "Wrote to the bot, not allowed yet", with an **Allow** button. A message from someone not allowed gets no answer and reaches no agent. You can also list Telegram user ids by hand.
3. **Map chats to agents,** under **Channel ⇄ Agent Mappings**, as Discord channels are. Every chat the bot has heard from is listed: your private chat with it, or a group you added it to.

- **In groups,** Telegram shows a bot only commands, @mentions and replies to it. To talk without @mentioning it, send `/setprivacy` to @BotFather, choose Disable, then remove the bot from the group and add it again.
- **Everything else works as on Discord:** replies, buttons (a picked one collapses to "✓ choice", since Telegram has no greyed-out buttons), files both ways (the Bot API takes files up to 20 MB), approvals, tasks, `Task:` and `Announcement:` posts, and the Inbox verbs.
- **Reactions** use Telegram's fixed set: 👀 delivered, 🤔 thinking, 👨‍💻 using tools, 🙏 asked you something, 👍 answered, 👎 failed.
- **Commands:** `/peerlink <sub>` with the same subcommands as Discord's, like `/peerlink run loop 5m check the deploy agent:web`. `/status`, `/inbox`, `/peers`, `/spaces`, `/schedules` and `/resume` work on their own too.
- **Agents send** to `"telegram"`, or `"telegram#<chat>"` when they're in several chats. `"discord"` still means any chat the agent sends in.
- **Setup changes** (the token, who's allowed) come from your browser only: an agent holding the key can't let anyone in.
- A group that Telegram upgrades to a supergroup gets a new id; its mapping moves with it.
- The token is kept beside Discord's, in `~/.peerlink/discord.json` (0600), and never sent back to the browser. Updates come by long polling, so nothing needs a public URL.


## Slack

Talk to your agents from Slack, in a direct message with your app or a channel it's in. Open **Channels** in the web UI:

1. **Create the app and paste its two tokens.** The page's button opens Slack with the app's manifest filled in (Socket Mode, the `/peerlink` command, the message events and permissions). Create it in your workspace, then copy two tokens: an **app-level token** (`xapp-…`, made under Basic Information with the scope `connections:write`) and the **bot token** (`xoxb-…`, shown after Install to Workspace).
2. **Message it, then allow yourself.** Everyone in the workspace can message the app, so peerlink ignores everyone until you allow them. Send the app any message: you show up on the Channels page under "Wrote to the app, not allowed yet", with an Allow button.
3. **Map channels to agents**, under Channel ⇄ Agent Mappings. The app's direct messages and every channel it was invited to (`/invite @peerlink`) are listed.

- **Nothing needs a public URL:** events come over Socket Mode, a websocket the daemon opens to Slack.
- **In channels** the app reads every message, so you needn't @mention it.
- **Threads:** a message you write inside a thread is answered in that thread; one in the channel is answered in the channel.
- **Everything else works as on Discord:** replies, buttons (a picked one collapses to "✓ choice · who"), files both ways, approvals, tasks, and `/peerlink status`, `inbox`, `peers`, `run`, `clear` and the rest, typed as `/peerlink <sub>`.
- **Reactions** are 👀 delivered, 🤔 thinking, 🛠️ using tools, ❓ asked you something, ✅ answered, ❌ failed.
- **Agents send** to `"slack"`, or `"slack#<channel>"` when they're in several.
- **Allowed people** are Slack member ids (`U…`); another app's bot can be trusted by its user id through `trusted_bots` in `~/.peerlink/discord.json`.

## Teams

Talk to your agents in a Microsoft Teams chat. There's no bot and nothing to register: peerlink signs in as you and reads and posts in the chats you map, through Microsoft Graph. Open **Channels** in the web UI:

1. **Sign in with Microsoft.** You get a code to enter at microsoft.com/devicelogin, then sign in with your work account. The sign-in goes through Microsoft's own **Graph Command Line Tools** app (the one PowerShell's `Connect-MgGraph` uses), asking for `User.Read` and `Chat.ReadWrite`. That name is what your company's sign-in log shows. If your company blocks that app, or won't let you approve chat access yourself, use **Use your own app instead** with an Entra app of your own: a public client ("Allow public client flows" on) with those two delegated permissions, and `http://localhost` as a Mobile and desktop redirect URI. If your company blocks signing in with a code ("You don't have access to this" after you sign in), use **Sign in in this browser** instead, in a browser on the computer peerlink runs on: Microsoft's own sign-in page opens and sends you back to a one-time localhost port (authorization code with PKCE). A rule that wants a company-managed device blocks both; that's your IT admin's to allow.
2. **Map chats to agents**, under Channel ⇄ Agent Mappings. Your chats are listed, with **Chat with yourself** where your tenant allows it. Only mapped chats are read, every few seconds.

- **Your agents post as you,** each post starting with the agent's name. A mark you can't see tells peerlink which posts are its own, so it never reads them back as yours.
- **Questions** come as a numbered list, because Teams chats have no buttons. Answer with the number or the option's words. Quote the question when several are open.
- **Commands are typed as words,** `peerlink status` or `peerlink run loop 5m check the deploy`, because Teams keeps `/` for its own.
- **Files:** images you paste reach the agent. Files shared from OneDrive or SharePoint are named but not opened, and agents can't send files to Teams.
- **In a group chat,** only you are heard until you allow others. Whoever writes is listed on the Channels page with an **Allow** button. Allowed people are Entra user ids.
- **Agents send** to `"teams"`, or `"teams#<chat>"` when they're in several.
- **Messages from before peerlink started reading a chat are not delivered.** A message written while the daemon restarts may be missed.
- **Setup is from your browser only.** The refresh token is kept in `~/.peerlink/discord.json` (0600) and never sent back to the browser. Signing out keeps the chat mappings.

## Agents (outside agents over MCP)

An AI agent that isn't on this machine, any MCP client (Grok, another company's bot), can talk to one of your agents through the **Agents** channel. Each client is a chat of its own, `agents:<name>`, mapped to agents under Channel ⇄ Agent Mappings like any other chat.

- **Two tools, nothing else.** `post_message(text, reply_to?)` posts into the client's chat, so it reaches the agent mapped to receive it. `read_replies(since?, wait_s?)` returns that agent's replies to this client, oldest first, with a `next_since` to pass next time. With `wait_s` (up to 90 s, under Cloudflare's 100 s cutoff), a read that finds nothing new holds the request open until a reply comes or the time is up, so `post_message` then `read_replies(since=<post id>, wait_s=90)` gets the answer in one call. At most 2 reads per client and 10 in all may wait at once; one over that is refused at once and can be retried without `wait_s`. The client gets no peers, no names to send to, no Inbox commands, tasks, announcements, spawns, schedules or rules. Any other tool fails with "unknown tool".
- **Linked clients.** To let two clients talk straight to each other, list one under **Linked with** on the other's row in the Agents section of the Channels page. Both then get a third tool, `post_to_client(to, text, reply_to?)`. A message sent with it lands in the other client's `read_replies` with `from` set to the sender, and its id is the same on both sides, so either can name it in `reply_to` or `since`. No agent here sees those messages. The posts count against the sender's post limit and are written to the audit log as `relay` lines, without their text. Switch either client off or revoke it and the tool goes away. Only you can set links (`links` in the agents section of discord.json).
- **Labelled and untrusted.** A post reaches the agent as `grok on Agents`, starting with a line that says it's from an external agent and is untrusted input. Because that label comes first, a `Task:` or `name:` prefix in the text does nothing.
- **Your agents only answer.** A new client's mapping has Sends off: the agent answers with `reply_to`, which goes back on that chat, but can't start a conversation there. Your approvals, questions, limit notices and `"discord"` sends never go to an Agents chat.
- **Transport:** MCP Streamable HTTP, with JSON responses, at `http://127.0.0.1:7931/mcp` (`port` in the section). Health is at `/healthz`, and registration at `/register`. It listens on this machine only, so put a tunnel with its own access control (Cloudflare Access) in front of it. A request with an `Origin` header (a browser) is refused.
- **Auth:** `Authorization: Bearer <token>`, one token per client. Only the token's sha256 is kept, in `~/.peerlink/discord.json` (0600):

  ```json
  "agents": {"enabled": true, "port": 7931, "default_to": "/path/to/the/agent",
             "clients": {"grok": {"token_sha256": "<64 hex>", "enabled": true, "rate_per_min": 6, "added": "2026-11-14T…Z"}},
             "chats": {"agents:grok": "grok"}}
  ```

- **Adding and revoking** go through the daemon, never by editing the file: the daemon keeps the file in memory and writes it back, so a hand edit is lost. The change counts from the next request.

  ```
  peerlink agents add <name> --token-file <file> [--rate N] [--replace]   # reads the token from the file, or makes one there (0600); only its hash is sent
  peerlink agents add <name> --token-sha256 <hex>   # a client that made its own token sends only its hash: sha256 of the exact bearer string (UTF-8, no newline), hex
  peerlink agents revoke <name>     # token refused at once; its chat's mapping goes too
  peerlink agents off <name> / on <name>
  peerlink agents channel off / on   # the whole channel: nothing listens while it's off
  peerlink agents dismiss <code>     # turn down a registration (approving one is only in the web UI)
  peerlink agents list
  ```

  A caller with the key may add and revoke clients and switch the channel. Where new clients land (`default_to`) and its port are set only from the web UI.
- **Registering.** With **Accept registrations** on (in the web UI, off by default), a client can ask to connect by itself. It makes its own token and sends only the token's hash:

  ```
  curl -X POST https://<host>/register -H 'Content-Type: application/json' \
       -d "{\"name\": \"partner-bot\", \"token_sha256\": \"$(printf %s "$TOKEN" | shasum -a 256 | cut -d' ' -f1)\"}"
  # {"status": "pending", "code": "K7Q-4MD", ...}
  ```

  The request shows under Channels → Agents with its name, IP and pairing code, and you get a ping in your space lead's chat. Approve it only when the client's owner tells you that code themselves. After you approve it, the token works on `/mcp`, and until then it gets 401. Requests expire after 24 hours. Each IP may register 3 times an hour, and all callers together 10 times an hour. At most 10 requests wait at once.
- **Limits and audit.** Each client has its own limit on posts (`rate_per_min`, default 6, and 60 an hour) and on reads (60 a minute). All clients together are capped at 20 posts, 120 reads and 240 requests a minute. Failed sign-ins and registrations are limited per caller address: Cloudflare's `CF-Connecting-IP` counts only on a connection from this machine (the tunnel) and only when it is a valid address. Failed sign-ins are capped at 10 a minute per caller IP and 30 a minute overall; past that, the answer is 429. Each client's token is checked against every stored hash in constant time, with no early exit. Every call is a line in `~/.peerlink/agents/audit.jsonl`: time, client, IP (`CF-Connecting-IP` behind Cloudflare), tool and result. Tokens and message text are never written there. Changes to who may call are written there too, as lines with `admin` (add, approve, dismiss, change, revoke, new-token, channel) and `via` (`web` for your browser, `key` for the CLI or an agent holding the key), whether or not the channel is running; never a token or its hash. Replies are kept for `read_replies` in `~/.peerlink/agents/replies/<name>.jsonl`.
- **Delegated approvals.** Off by default. A client you trust can pass on your OK for narrow dev work: `dev.start`, `dev.change`, `dev.deploy` or `jira`, on dev or staging only. You switch it on, and tick the client and its kinds of work on its mapping under Channels, from your browser only. The grant lasts 7 days. A new token, a re-pointed mapping, a link to another client or revoking the client clears it. Prod, money, secrets, deletions and peerlink's own settings are never delegable. Each approval needs a structured `approval` on `post_message`, made with that client's own token. peerlink stamps it as `daN`, valid for 30 minutes, at most 5 an hour and 20 a day, and posts it to you with **Undo + revoke**. New approvals are stamped only from 08:00 to 21:00 local time. One stamped at 20:59 still counts until 21:29, because the hours limit when an approval is given, not when it's used.
  - **One message, one agent.** An approval counts only on the message it was stamped on, and only for the agent that message went to (or a later session of that agent). Agents confirm one with `check_approval(id, message)`. The same `daN` copied into another message, for another action or another agent, is refused.
  - **Never-delegate list.** Clients registered through `/register` are never delegable. To rule out others, list them in `~/.peerlink/never_delegate.txt`, one name or shell pattern a line (`grok-pat`, `*-alex*`), with `#` for notes. It stays on your machine. At every load, and whenever a client is added, the daemon puts each client the file names on the list, clears any grant it had, and writes an `admin: never-delegate` line to the audit log. The web UI shows the list read-only. Only editing that file changes it.

## Starting and ending agents

**Starting:** when work belongs in a folder no agent runs in, an agent can call `request_spawn` with the folder, why, and a first prompt. Nothing starts until you approve it, either in the Inbox (**Approve and start it**) or with the **Approve** button on Discord. `/inbox` then `1 approve` works too. Only you can approve: never an agent, never a bot.

Once approved, the daemon starts Claude Code in that folder: in a herdr terminal when herdr's `hc` is installed and **Start agents in the herdr app** is on in Settings. It outlives the terminal the daemon was started from, and is removed when the agent exits. In the herdr app it opens as a tab named after the agent, in the space of the agent that asked for it, else in tmux when tmux is installed (`tmux attach -t peerlink-<folder>-s1` to see it), else in a new Terminal window. When `claude` is an alias or function in your login shell (zsh or bash), the agent starts through that shell, so your own flags apply; otherwise the `claude` program runs as is. The new agent joins the space it was asked for (the asking agent's by default) and gets the first prompt as a message from whoever asked, so its replies go back there. A request you don't answer expires after a day, and the asking agent is told either way. Requests are kept in `~/.peerlink/spawns.json`.

**Workers:** an agent can hire a worker for one piece of work by passing `worker: true` to `request_spawn`. A worker can't start agents or schedules itself, is told who hired it, and calls `retire` when its work is done: that ends its session once the turn finishes and tells the lead which tasks it closed. It can't retire while it still owns an open task. The lead that hired a worker can retire a stuck one by name, and only workers are ever retired this way. Two settings under **Approvals**, which only you change in the web UI, keep hiring in bounds: **Workers running at once** (`max_live_spawns`, 0 for no cap) queues hires past the cap and starts them as others retire, and the **night guard** (`night_guard`, like `22-8`) refuses new workers in those hours. Approval works as for any start: auto-approve skips it.

**Worktrees:** `request_spawn` can take `worktree: {repo, branch, base}` in place of an existing folder. When the agent starts (not before, so a denied request leaves nothing behind), the daemon fetches the base's remote, best effort, and adds a git worktree for the branch: a new branch from `base` (the repo's HEAD by default), or the existing branch checked out. It goes beside the repo as `<repo>-wt-<branch>` when `dir` is left out or names the repo itself (then `worktree.repo` can be left out too), else in `dir`, a new folder that mustn't exist yet. Any branch name git takes works; peerlink doesn't impose a case or a form. If Claude Code trusts no folder above it, the daemon marks the new folder trusted, so the agent doesn't stop at the "trust this folder" question with nobody there to answer. A worktree is never removed for you: when a worker retires, its lead is told the command to remove it once the work is merged. Links leads made for a worker end when it retires.

**Upgrading while agents run:** agents reload themselves onto a new peerlink when idle. A session that reloads can keep showing the older tool parameters until it restarts. The calls still work with the new parameters, and the daemon tells that session once, at its next turn, what the tools now take.

**Restarting an agent in its herdr terminal** (to load new tool schemas, or a new model or flags), keeping its conversation:
1. Check it's idle: `hc get <terminal> --json` shows a state other than working.
2. Run `kill -TERM <claude pid>` (the pane's foreground pid). Claude exits cleanly and the pane drops to a shell.
3. Run `env -u HC_TERMINAL_ID -u HC_SCOPE hc send <terminal> "claude <your flags> --resume <session id>"`. The session id is the agent's peerlink id, from the Agents page or `/api/peers`, not the `--resume` in the pane's old command line. Without the `env -u`, `hc send` from inside herdr delivers the text as a note to whatever runs there, not as typed input.

**Clearing its context:** open an agent's panel in the web UI and press **Clear context** on its **Controls** tab. The agent runs `/clear` once the turn it's in ends. It keeps its name, space and role, and is given back what it still owes peers. Only you can do this, from the web UI.

**Ending:** open an agent's panel in the web UI and press **Terminate** on its **Controls** tab, or send `/kill <name>` on Discord and confirm with the button. The agent finishes the turn it's in, then its session exits. If it's still running a minute later, the daemon stops its process, after checking it is a Claude Code process. Agents can't end each other. Every termination is logged in `~/.peerlink/terminations.jsonl`, with who asked.

**Deleting an offline agent:** the Agents page lists the agents that aren't running under **Offline agents**, kept 60 days. **Delete** removes one with its settings, the spaces and links you set for it, and a lead it held in a folder where nothing runs now. Messages queued for it still wait for its folder's next agent. A running agent can't be deleted, because it would register again at once; end it first. An offline lead pinned at the top of its space has a **Remove** button that does the same, and if no record of the agent is left, it just stops leading.

**Starting clean:** on the Controls page, tick what to clear and press **Start clean**. You can clear spaces, leads and links, offline agents, message history (messages still waiting to be delivered stay), tasks, and schedules. Rules, settings, chat channels, outside clients, the key and running agents are never touched. The files it changes are first copied to `~/.peerlink/backups/clean-<time>/`; to undo it, stop the daemon and copy them back. Like deleting agents and spaces, it works only from your browser. An agent holding the key is refused.

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
- **A message isn't lost on the way in.** The agent confirms each message once it's in its session, and the daemon hands it out again every 45 seconds until it does, for up to a day. That covers a message that arrives while the plugin reloads, or one whose prompt never started a turn. If a turn can't start, the message goes into the transcript instead, to be read with your next prompt. A minute after a message's prompt seemed to start a turn, the agent checks that it's in the transcript, and puts it to the agent again, once, if not. A message is never shown twice.
- **A name outside your reach is an error**, such as `acme is in the 'bridge' space, which you can't reach`. It never falls back to another agent.

## Updating

Run `/peerlink update` in any agent. It pulls the latest peerlink from the marketplace and installs it. Every agent then sees that a newer version is installed and queues a reload of its plugins, which Claude Code runs the next time that agent is idle, with a toast saying so. Nobody has to run `/reload-plugins` in each session. To turn this off, set `auto_reload` to false. An agent never reloads into the version it already runs, so it can't loop.

## When an agent's context resets

Compaction, `/clear` and `/resume` reset what an agent remembers, but not what it owes its peers.

- **The agent stays connected.** After `/clear` or `/resume`, the plugin registers the new session as the old one's successor. It keeps the name, settings and queued messages.
- **Nothing goes to a dead session.** A reply to a session that's gone goes to the live agent in the same folder. A new session in a folder picks up messages still queued for the last one there, for up to an hour.
- **Open requests come back.** At the start of a conversation, after compaction and after `/clear`, the agent's context lists the peer requests it still owes an answer to and the ones it's waiting on, with message ids. They are taken from the last 8 hours of messages that expect a reply and haven't had one.

## Feedback: bugs and ideas

This is for people developing peerlink. Turn it on with the `feedback` setting (Settings in the web UI, or `peerlink config feedback true`); it's off by default. Reports stay on your machine, so to tell the peerlink maintainers about a bug, open a [GitHub issue](https://github.com/dfunq/peerlink/issues) instead.

With it on, agents call `report_issue` when peerlink itself misbehaves (a `bug`), or when it could work better for them (an `idea`). Every agent can, whatever its space. Each one lands in the **Feedback** page of the web UI, and the menu counts the new ones. Agents that start after you turn it on get the tool.

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
| `/peerlink web` | Open the web UI: you say **Open** in a dialog on screen, and your browser opens signed in. Its sidebar has Agents (an org chart of spaces), Spaces, Messages, Settings and Controls |
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
peerlink web          # open the UI: you're asked on screen, then your browser opens signed in
peerlink status
peerlink issues       # problems agents reported
peerlink peers
peerlink messages     # the last 20
peerlink mute <agent> / unmute <agent>
peerlink config [name [value]]
peerlink agents add <name> --token-file <file> | revoke <name> | list
peerlink start | stop | restart | logs
peerlink rotate-key | logout
```

## Security

- The daemon listens on `127.0.0.1` only. Every API call has to carry the key in `~/.peerlink/key`, a 0600 file that's created on first start. Agents send it as `X-Peerlink-Key`.
- **The browser** gets in through a one-time link that only the daemon sees. `/peerlink web` or `peerlink web` asks you on this machine's screen ("Open the peerlink web UI in your browser?", with Cancel as the default button). Say **Open** and the daemon opens the link in your default browser itself. Whoever asked, an agent or a shell, never gets the link. The link is signed with HMAC-SHA256 using a secret that lives only in the daemon's memory, not with the key. It is valid for 60 seconds and works once. It sets an `HttpOnly`, `SameSite=Strict` session cookie that lasts 12 hours. Sign-ins are kept in `~/.peerlink/web_sessions.json` (0600) as hashes of the cookies, so restarting the daemon doesn't sign you out, and the file signs nobody in.
- **What only you may do** (answering the Inbox, approving starts, leads, spaces, links, auto-approve, Discord, Telegram, Slack and Agents-channel settings) needs your browser's session, and a request that carries the key is refused even with a cookie. Every browser write must carry its `Origin`. **Restarting, stopping and rotating the key** go through from your browser. From anywhere else (an agent, `/peerlink restart`, `peerlink stop`) you're asked on screen first. The one exception is a plugin replacing the daemon with the newer build it ships, which an update needs. Only one question is on screen at a time, and after a no the next one waits 30 seconds.
- **The limit of this fence.** Agents run as your OS user. One that's determined can still read your browser's cookie store, kill the daemon's process, or click a dialog with computer use. The checks above stop an agent that simply holds the key or follows an injected prompt. To fence agents off completely, run them as another OS user.
- **Paired clients** (such as a herdr mod) can do what your browser does without a browser. A client calls `POST /api/owner/pair` with its name and gets back a pairing id, a 6-digit code and a secret. It shows the code, and you're asked in two places at once: Touch ID on this Mac (or your password), and on Discord with Approve and Deny. Approve only if the client shows the code the prompt names. Only presses from you and your Discord `allowed_users` count. The client polls `GET /api/owner/pair/<id>` with its secret as `X-Pair-Secret`. Once you approve, it gets a token, once, and sends it as `Authorization: Owner <token>`, from this machine only.
  - The token is never written to disk. The daemon keeps only its sha256, in memory, so a restart signs every client out. It also ends after 12 hours unused, and when the client pairs again.
  - A pairing doesn't start when nothing could approve it, with no Discord channel to ask in and no Touch ID helper. The client is told why.
  - Only one pairing can be pending. A second start cancels both and warns you on Discord. At most 3 pairings start an hour.
  - **Default-deny.** With the token, reads go through. Only a short list of changes goes through without asking: answering the Inbox (questions, tasks, feedback, and denying a request), clearing or ending a worker, and unpairing. Approving anything in the Inbox (a start, a schedule, a rule) asks again. Every other change asks you again for that exact call: the daemon answers `428` with a `step_up` id, and your Discord prompt shows the whole call, in a code block so nothing in it is formatted or hidden. A call too long to show in one message is refused (`413`), not asked about. A settings change is shown as each setting, old → new. A prompt short enough for Touch ID's dialog is offered there too. Once you approve, the client repeats the same request with `X-Owner-Step-Up: <id>`. The approval covers that exact request once, for 2 minutes. A settings write that changes nothing needs no approval, and an unknown setting is refused.
  - **Never with the token:** rotating the key, Start clean, signing in or out of the web UI, Discord, Telegram, Slack, Teams and Agents-channel settings, changing the settings that open the daemon to other machines (`listen_host`, `token`, `remotes`), restart and stop. A client can't read these either. It never sees the `token` or `remotes` settings, in a read or in a write's reply, and what it sends for them is ignored. Approval prompts show control and bidi characters as `\uXXXX`, so a prompt reads as the call acts. An Inbox approval prompt says what approving does: a schedule's timing, folder and whether it starts agents, or a start's folder, worktree, space, whether it's a worker, and its first prompt. Starting or cancelling a pairing is refused for any request that carries an `Origin` other than the daemon's own, `null` included, so no web page can start one. Note that the key, which agents hold, can still change `listen_host`, `token` and `remotes` through the API. Only the auto-approve settings, the worker cap and the night guard are browser-only for the key.
  - Every write a paired client makes is logged in `daemon.log` with its name and pairing. A decision it makes is recorded as by "herdr (pairing op-…)". See and revoke paired clients at `GET /api/owner/devices` and `POST /api/owner/revoke`, or sign them all out with `/peerlink unpair` on Discord. Pairing prompts and alerts go to the Discord channel in `owner_channel` in your Discord settings, or else the first Discord channel you've mapped.
  - **Touch ID helper.** A small Swift program (`plugin/daemon/touchid/main.swift`), never shipped as a binary. When the daemon starts on a Mac with the Xcode command-line tools (found through `xcode-select -p` and `xcrun --find swiftc`, never `PATH`), it builds the helper to `~/.peerlink/bin/peerlink-touchid` when there's no recorded build of this source: a first install, an update that changed the source, an earlier build that failed, or a helper you deleted. Only that build records a helper in `~/.peerlink/integrity.json`, and only a recorded helper is used. Without the command-line tools there's no helper, and Discord is how you approve. The helper accepts a finger on this Mac's Touch ID only (`deviceOwnerAuthenticationWithBiometrics`, with no password button and no reuse of an earlier press). Your password and an Apple Watch don't count, and a Mac with no enrolled sensor has no Touch ID: Discord is how you approve there. The helper closes its dialog after 80 seconds. A probe that shows no dialog, run at start and again in the background every 5 minutes, checks that it can ask in this session. A helper that can't ask doesn't count as a way to approve. The daemon sends it a nonce and a one-time HMAC key on stdin, and counts its answer only from a zero exit with the nonce and HMAC-SHA256 right. It warns you on Discord, once per change, if `peerlinkd.py` changed while its version didn't, if the helper isn't the one it built (delete it and restart peerlink to rebuild it), or if the helper's source changed while peerlink's version didn't. In the last two cases the helper isn't used. An update that changes the source rebuilds the helper and says so on Discord.
  - **What pairing doesn't defend against.** Agents run as you, so the same fence limit applies. Pairing holds against an agent that acts through peerlink's tools and API. It doesn't hold against one that edits files: **an agent that edits `peerlinkd.py` defeats pairing entirely.** The daemon checks every approval, Discord presses included, so an edited daemon can mint tokens or approve anything. The same goes for an agent that edits `integrity.json`, `main.swift` or the helper. Your Discord press is the root of trust on every OS. Touch ID is a faster second way to answer, nothing more. The checksums make an edit loud only while the edited daemon still runs the check, and the warning comes after the change. Some edits leave no sign at all: deleting `integrity.json` and swapping `main.swift` gets the swapped source built and trusted at the next start, and writing a planted helper's sha into `integrity.json` gets it trusted at once. Bumping the version along with `main.swift` looks like an update, and its only sign is the Discord note that the helper was rebuilt. An agent can read a client's memory if it gets the macOS developer-tools permission, which it can't grant itself. And an agent can start a pairing of its own. The code is the guard: approve only when your client shows the code the prompt names. The helper's HMAC proves the answer came from whatever ran at the helper's path, not that a person pressed. The checksum is what ties that path to the source.
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
- **Several machines (optional).** List other daemons under `remotes`, set `listen_host` to `0.0.0.0` (or reach each other through a tunnel, which arrives as localhost), and set a shared `token`. Agents on those machines then show up as `name@host`. A reply to one of them needs only `reply_to`, and threads onto the message it answers on both sides; a send that fails to connect is tried up to 3 times, and the other daemon keeps one copy.

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

Requires Python 3 (`python3` on your `PATH`). The daemon runs on the newest Python 3 it finds, including Homebrew's when PATH lists macOS's own first; set `PEERLINK_PYTHON` to choose one. Use a Python that still gets security fixes (3.10 or later): macOS's built-in 3.9 doesn't.

**Pairs well with [herdr](https://herdr.dev).** herdr is a terminal workspace manager for AI coding agents: it keeps your agents in panes and tabs of one persistent session, so you can watch them all. peerlink lets them talk to each other.

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
| `feedback` | false | Agents get `report_issue`, and the guidance asks them to report problems with peerlink itself. Installs from before this setting keep it on |
| `issue_agent` | peerlink | The agent that `report_issue` reports are also sent to, when it's live |
| `sessions` | `{}` | Per-agent `nickname`, `role`, `muted` and `auto_wake`, keyed by Claude Code session id |
| `folder_spaces` | `{}` | The space you put each folder in, such as `{"/Users/you/Repos/games": "acme"}` |
| `space_parents` | `{}` | The space each space sits inside, set on the Spaces page (`""` for the top level) |
| `folder_names` | `{}` | The name each folder's agents get, kept when you name an agent |
| `local_name` | peerlink.local | The web UI's name on this machine: `http://peerlink.local:7899`. The daemon publishes it over Bonjour (`dns-sd` on macOS, `avahi-publish` on Linux), pointing at 127.0.0.1, with no sudo or hosts file. `peerlink.localhost:7899` also works in Chrome, Edge and Firefox on any OS. Sign-in links use the name once it resolves. Empty turns it off. A restart applies it |
| `remotes`, `listen_host`, `token` | | Settings for linking daemons across machines |

`PEERLINK_PORT` and `PEERLINK_HOME` override the port and the data directory, and `PEERLINK_HOSTNAME` the host name other machines know this one by.

## An MCP endpoint for other clients

`plugin/daemon/peerlink_mcp.py` lets a client that isn't Claude Code (a Discord voice bot on the Claude Agent SDK, say) take part as one peer. It registers with the local daemon under a name set when it starts, keeps itself alive, and offers three tools: `send_message`, `list_peers` and `inbox`. It needs only Python 3 and a running daemon.

- **Waiting is cheap.** While a caller waits in `inbox`, the endpoint asks the daemon to hold its heartbeat (`"wait"`, up to 20 s), so a message arrives as soon as it's queued. It reports the version of the plugin it shipped in.
- **Identity is config.** `--name` (or `PEERLINK_MCP_NAME`) names the peer. `--space` (or `PEERLINK_MCP_SPACE`) is the space it must be in. If it's in another (its folder's, `--cwd`, or one you set for the peer), it still registers, so you can find it on the web UI's Agents page and move it, but its tools refuse until you do; it notices the move within seconds. No tool takes a name or space.
- **`send_message`** sends a plain message to one peer. It refuses `space…` and `lead…` targets, `user` (your Inbox), `discord` targets, other kinds (tasks, announcements), text over `max_message_chars`, and a `reply_to` on a message the peer neither sent nor received.
- **`inbox`** returns the unread messages and replies. Read ones are acknowledged to the daemon. With `peek: true` they stay unread, so a turn that fails before handling them loses nothing. Clients poll; nothing is pushed.
- **Transport:** stdio by default, or `--http PORT` on 127.0.0.1 only (a foreign `Host` or any `Origin` is refused).

An Agent SDK server entry, with each tool allowed by its full name:

```json
{"claude": {"mcp_servers": {"peerlink": {"type": "stdio", "command": "python3",
  "args": ["/path/to/plugin/daemon/peerlink_mcp.py", "--name", "voice", "--space", "services", "--cwd", "/path/to/voice"]}},
  "allowed_tools": ["mcp__peerlink__send_message", "mcp__peerlink__list_peers", "mcp__peerlink__inbox"]}}
```

Or `{"type": "http", "url": "http://127.0.0.1:7901/mcp"}` for one started with `--http 7901`. Tests: `python3 -m unittest discover -s plugin/daemon`.

## Layout

```
.claude-plugin/marketplace.json   makes this repo installable
plugin/.claude-plugin/plugin.json
plugin/hooks/hooks.json           points at the hooks module
plugin/hooks/register.tsx         tools, /peerlink, polling, delivery, summaries, guidance, chat rows
plugin/hooks/register.test.tsx    tests (claude plugin test plugin)
plugin/daemon/peerlinkd.py        daemon, CLI and web UI
plugin/daemon/chatlink.py         what the chat platforms share: errors, which platform a channel id is on
plugin/daemon/discordlink.py      the Discord bot: gateway, REST, buttons, /peerlink
plugin/daemon/telegramlink.py     the Telegram bot: long polling, buttons, commands, reactions
plugin/daemon/test_chat.py        tests for the chat layer, against a fake Discord
plugin/daemon/test_telegram.py    tests for the Telegram bot, against a fake Bot API
plugin/daemon/slacklink.py        the Slack app: Socket Mode, buttons, the /peerlink command, reactions
plugin/daemon/teamslink.py        Teams, signed in as you: device-code sign-in, Graph polling, numbered answers
plugin/daemon/test_slack.py       tests for the Slack app, against a fake Web API and websocket
plugin/daemon/test_teams.py       tests for Teams, against a fake Microsoft sign-in and Graph
plugin/daemon/agentslink.py       the Agents channel: outside agents over MCP, two tools, per-client tokens
plugin/daemon/test_agents.py      its tests
plugin/daemon/peerlink_mcp.py     an MCP endpoint for clients outside Claude Code
plugin/daemon/test_peerlink_mcp.py  its tests (python3 -m unittest)
```

