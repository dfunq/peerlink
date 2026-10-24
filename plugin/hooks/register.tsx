import type { EngineInterface, Register } from 'claude-code'

// Incoming peer messages read the same for the model and the person: one header line, then the text.
const MESSAGE_MARK = '↘ '
const REPLY_HINT = '(Reply with send_message, reply_to the id, only if a reply is needed.)'

const DAEMON = 'http://127.0.0.1:7899'
// The daemon this plugin ships; an older one running is replaced.
const DAEMON_VERSION = '0.8.15'
// Recent work in this folder (Claude Code's own titles, commits) counts this far back.
const HISTORY_DAYS = 14

type PeerMessage = {
  id: string
  from: string
  from_name: string
  from_host: string
  text: string
  expects_reply: boolean
  reply_to: string | null
  hops: number
  task?: string
  kind?: string
  // false: nothing in it needs a turn of its own (an FYI, a reply to a reply), so it waits for the agent's next one.
  wake?: boolean
}

type ClientConfig = {
  space: string
  space_source: 'override' | 'file' | 'rule' | 'default'
  space_from: string
  // Set when this agent leads a space nested in another: the space it also sits in and speaks for its own.
  parent?: string
  // How this agent leads its space: you picked it, its folder holds the nesting .peerlink, or it was alone there.
  leads?: '' | 'set' | 'file' | 'only'
  space_parent?: string
  // Spaces your user added this agent's folder to, on top of its own.
  also?: string[]
  // What this agent is here for: the role: lines of its folder's .peerlink.
  role?: string
  // The agent that leads this agent's space, and the leads of spaces nested inside it.
  lead?: string
  children?: { name: string; space: string }[]
  // The Discord channels this agent's folder is mapped into, and which ones it's the master of.
  discord?: { channel_id: string; channel: string; master: boolean }[]
  poll_interval_ms: number
  auto_wake_idle: boolean
  // How long an idle agent waits after another agent's message, so ones close together share a turn.
  wake_delay_s?: number
  inject_while_busy: boolean
  guidance: string
  muted: boolean
  roster_in_prompt: boolean
  auto_summary: boolean
  auto_reload?: boolean
  summary_model: string
}

type Previous = { summary: string; name: string; ts: number } | null

const SUMMARY_SYSTEM =
  'You keep the one-line status other Claude agents see for this agent. In at most 200 characters say what it is working on, in which repo or service, and what it could answer questions about. Plain text, no preamble, no quotes. If the current status is still accurate, reply exactly UNCHANGED.'

// Summaries cost a model call, and every change is resent to each peer: refresh only this often.
const SUMMARY_EVERY_TURNS = 8
const SUMMARY_MIN_GAP_MS = 10 * 60_000
const MANUAL_SUMMARY_HOLD_MS = 10 * 60_000

const HELP = `peerlink commands:
  /peerlink [status]       daemon, this agent, its summary
  /peerlink web            open the web UI (one-time sign-in link)
  /peerlink issues         the last problems agents reported with peerlink
  /peerlink update         install the latest peerlink; every agent reloads itself when idle
  /peerlink restart        restart the daemon
  /peerlink rotate-key     new key; signs out every browser

  /peerlink-spaces                 the space tree, and this agent's place in it
  /peerlink-spaces <name> | clear  override the space for this agent only
  /peerlink-spaces file <name> | clear    set the space in this project's .peerlink
  /peerlink-spaces parent <name> | clear  nest this project's space inside another

  /peerlink-peers                  list live agents
  /peerlink-peers mute <agent> | unmute <agent>

  /peerlink-agent                  this agent's name, space, wake and summary
  /peerlink-agent name <name>      rename this agent
  /peerlink-agent summary <text>   set what peers see for this agent
  /peerlink-agent wake on|off|default    whether a message starts a turn here when idle

/peerlink <subcommand> runs any of these too, as in /peerlink space file acme.`

// Module state: a hot reload starts it over and session.start registers again.
let sessionId = ''
// What the main agent is doing, for the reaction on a Discord message it's answering: 🤔 thinking, 🛠️ tools, ❓ asking.
let activity: 'thinking' | 'tools' | 'asking' = 'thinking'
let myName = ''
let busy = false
let cfg: ClientConfig | undefined
let pending: PeerMessage[] = []
// Announcements don't start a turn: they're read at the start of the next one.
let notices: PeerMessage[] = []
// Set once the user has asked, from the web UI or Discord, for this session to end.
let terminating = false
let pid: number | undefined

// This Claude Code process, so the daemon can end it if a requested exit doesn't happen.
async function processId($: EngineInterface) {
  if (pid === undefined) {
    const run = await $.process.run(['sh', '-c', 'echo $PPID'], { timeoutMs: 3000 }).catch(() => undefined)
    pid = Number(run?.stdout.trim()) || 0
  }
  return pid || undefined
}
let cancelPoll: (() => void) | undefined
let lastRoster = ''
let manualSummaryAt = -Infinity
let lastSummaryAt = -Infinity
let summarizing = false
let summary = ''
let turnsSinceSummary = 0
let hasSummarizedTalk = false
let peerCount = 0
let previous: Previous = null
let home = ''
let key = ''

async function peerlinkHome($: EngineInterface) {
  if (!home) home = (await $.env.get('PEERLINK_HOME')) ?? `${(await $.env.get('HOME')) ?? ''}/.peerlink`
  return home
}

// The peerlink version Claude Code has installed for this plugin, from its install record; '' when unknown
// (a --plugin-dir checkout has none).
async function installedVersion($: EngineInterface) {
  try {
    const record = JSON.parse(String(await $.fs.read(`${(await $.env.get('HOME')) ?? ''}/.claude/plugins/installed_plugins.json`)))
    const entry = Object.entries(record.plugins ?? record).find(([k]) => k.startsWith('peerlink@'))?.[1] as any
    return String((Array.isArray(entry) ? entry[0] : entry)?.version ?? '')
  } catch {
    return ''
  }
}

// When a newer peerlink is installed, the agent reloads itself: no one has to run /reload-plugins in every
// session. It's queued at once, busy or not; Claude Code runs a queued command when the session is next idle,
// so a busy agent isn't interrupted and one that's rarely idle still gets it. Only a newer install triggers it.
let lastInstallCheck = -Infinity
let reloading = false
async function reloadIfUpdated($: EngineInterface) {
  if (reloading || cfg?.auto_reload === false) return
  const now = await $.clock.now()
  if (now - lastInstallCheck < 30_000) return
  lastInstallCheck = now
  const installed = await installedVersion($)
  if (!installed || !isOlder(DAEMON_VERSION, installed)) return
  reloading = true
  $.ui.toast(`peerlink: updating to ${installed}`)
  $.ui.log(`peerlink: ${installed} is installed; reloading plugins to use it`)
  await $.command.run({ command: 'reload-plugins' }).catch(() => {
    reloading = false
    $.ui.log(`peerlink: ${installed} is installed; run /reload-plugins to use it`)
  })
}

async function loadKey($: EngineInterface) {
  try {
    key = String(await $.fs.read(`${await peerlinkHome($)}/key`)).trim()
  } catch {
    key = ''
  }
}

async function call($: EngineInterface, method: string, path: string, body?: unknown, retried = false): Promise<{ ok: boolean; status: number; data: any }> {
  if (!key) await loadKey($)
  const res = await $.http.fetch(DAEMON + path, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Peerlink-Key': key },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  // The key was rotated: read it again once.
  if (res.status === 401 && !retried) {
    await loadKey($)
    return call($, method, path, body, true)
  }
  let data: any = {}
  try {
    data = JSON.parse(res.text)
  } catch {}
  return { ok: res.ok, status: res.status, data }
}

function isOlder(version: string, than: string) {
  const a = version.split('.').map(Number)
  const b = than.split('.').map(Number)
  for (let i = 0; i < 3; i++) if ((a[i] ?? 0) !== (b[i] ?? 0)) return (a[i] ?? 0) < (b[i] ?? 0)
  return false
}

// The version of the daemon this agent last reached; '' until it has.
let daemonVer = ''

async function daemonHealth($: EngineInterface) {
  try {
    const health = await call($, 'GET', '/api/health')
    return health.ok ? { version: String(health.data.version ?? '0'), code: String(health.data.code ?? '') } : undefined
  } catch {
    return undefined
  }
}

// The fingerprint of the daemon this plugin ships, from the daemon itself; '' when it can't say.
async function shippedCode($: EngineInterface) {
  const run = await $.process.run(['python3', `${$.plugin.root}/daemon/peerlinkd.py`, 'fingerprint'], { timeoutMs: 5000 })
  return run.exitCode === 0 ? run.stdout.trim() : ''
}

// Start the daemon, or replace an older one. At a load or reload (`fresh`), a
// daemon of the same version running other code is replaced too, so editing
// the daemon and running /reload-plugins takes effect. A newer daemon is never
// replaced. An older plugin still running in another agent may restart its own
// daemon in the gap, so try a few times.
async function ensureDaemon($: EngineInterface, fresh = false) {
  const mine = fresh ? await shippedCode($) : ''
  let replaced = ''
  for (let attempt = 0; attempt < 4; attempt++) {
    const health = await daemonHealth($)
    const stale = !!health && (isOlder(health.version, DAEMON_VERSION) || (health.version === DAEMON_VERSION && !!mine && health.code !== mine))
    if (health && !stale) {
      const changed = daemonVer !== '' && daemonVer !== health.version
      // Starting or replacing the daemon is worth seeing: it's when peers reconnect, and the version says what code runs.
      if (attempt > 0 || changed) {
        const what = replaced ? `replaced daemon ${replaced} → ${health.version}` : `started daemon ${health.version}`
        $.ui.toast(`peerlink: ${what}`)
        $.ui.log(`peerlink: ${what}`)
      }
      daemonVer = health.version
      return true
    }
    if (health) {
      replaced ||= health.version === DAEMON_VERSION ? `${health.version} (other code)` : health.version
      await call($, 'POST', '/api/shutdown', {}).catch(() => {})
      await $.clock.sleep(300)
    }
    await $.process.run(['python3', `${$.plugin.root}/daemon/peerlinkd.py`, 'start'], { timeoutMs: 15000 })
    key = ''
  }
  return (await daemonHealth($)) !== undefined
}

async function git($: EngineInterface, cwd: string, args: string[]) {
  const run = await $.process.run(['git', ...args], { cwd, timeoutMs: 5000 })
  return run.exitCode === 0 ? run.stdout.trim() : ''
}

// What the agent's folder has checked out: "main", "feat (worktree wt-2)", "detached @ 1a2b3c4", or '' outside a repo.
// Asks git itself, so a subfolder of a repo counts, whatever $.session.repo() makes of it.
async function branchOf($: EngineInterface, cwd: string) {
  const dirs = (await git($, cwd, ['rev-parse', '--path-format=absolute', '--git-dir', '--git-common-dir', '--show-toplevel'])).split('\n')
  if (dirs.length < 2) return ''
  // Exits 1 on a detached HEAD; works on a branch with no commits yet, where rev-parse HEAD fails.
  const name = await git($, cwd, ['symbolic-ref', '--short', '-q', 'HEAD'])
  const sha = name ? '' : await git($, cwd, ['rev-parse', '--short', 'HEAD'])
  const head = name || (sha ? `detached @ ${sha}` : 'detached')
  // A linked worktree keeps its git dir apart from the main checkout's.
  const worktree = dirs[0] !== dirs[1] ? dirs[2]?.split('/').filter(Boolean).at(-1) : ''
  return worktree ? `${head} (worktree ${worktree})` : head
}

// Agents switch branches mid-session, so the heartbeat re-reads it, at most this often.
const BRANCH_EVERY_MS = 10_000
let branch = ''
let branchAt = -Infinity

async function freshBranch($: EngineInterface) {
  const now = await $.clock.now()
  if (now - branchAt < BRANCH_EVERY_MS) return undefined
  branchAt = now
  const next = await branchOf($, await agentFolder($))
  if (next === branch) return undefined
  branch = next
  return next
}

// The folder each session started in, kept across plugin reloads: the newest sessions only, to stay small.
const FOLDERS_KEPT = 50

// The folder this agent is, for its whole session. $.session.root() follows the host when it moves the session's
// directory (it can follow a shell cd), and an agent that re-registers from ~/Repos must not become "Repos".
// `succeeds`: the session this one continues after a /clear or resume, whose folder it keeps.
async function agentFolder($: EngineInterface, succeeds?: string) {
  const folders = ((await $.store.get('folders').catch(() => undefined)) ?? {}) as Record<string, string>
  if (folders[sessionId]) return folders[sessionId]
  const cwd = (succeeds && folders[succeeds]) || (await $.session.root())
  folders[sessionId] = cwd
  await $.store.set('folders', Object.fromEntries(Object.entries(folders).slice(-FOLDERS_KEPT))).catch(() => undefined)
  return cwd
}

// `succeeds`: the session id this conversation ran under before a /clear or resume, whose messages it takes over.
async function announce($: EngineInterface, succeeds?: string) {
  // Where the session started, not where the shell or host moved it since: an agent that cds elsewhere is still the same agent.
  const cwd = await agentFolder($, succeeds)
  const repo = await $.session.repo()
  branch = await branchOf($, cwd)
  branchAt = await $.clock.now()
  const res = await call($, 'POST', '/api/sessions', {
    succeeds,
    id: sessionId,
    name: cwd.split('/').filter(Boolean).at(-1) ?? 'agent',
    cwd,
    repo: repo?.remote ?? repo?.root ?? '',
    branch,
    model: await $.session.model(),
    busy,
    summary: summary || undefined,
    plugin: DAEMON_VERSION,
    pid: await processId($),
  })
  if (res.ok) {
    myName = res.data.name
    cfg = res.data.config
    previous = res.data.previous ?? null
  }
  return res.ok
}

type Frame = { id: string; from: string; re?: string; reply: boolean; task?: string; body: string }

// Reads back what frame() wrote, wherever it sits in the row's text.
function parseFrames(text: string): Frame[] {
  const frames: Frame[] = []
  const pattern = /^↘ (.+?) · (m\d+)((?: · [^\n]*)?)\n([\s\S]*?)(?=\n\n↘ |\n\n\(Reply with send_message|$)/gm
  for (const match of text.matchAll(pattern)) {
    const tags = match[3]
    frames.push({
      from: match[1],
      id: match[2],
      re: tags.match(/re (m\d+)/)?.[1],
      reply: tags.includes('reply expected'),
      task: tags.match(/task (t\d+)/)?.[1],
      body: match[4],
    })
  }
  return frames
}

// One row per message: who it's from and the text, the way a chat shows it.
function MessageRow(ui: any, arrow: '↘' | '↗', who: string, tags: string[], body: string, key: string) {
  const { Box, Text } = ui
  return (
    <Box key={key} flexDirection="column">
      <Box flexDirection="row">
        <Text dimColor>{'peerlink '}</Text>
        <Text color="claude" bold>{`${arrow} ${who}`}</Text>
        {tags.length ? <Text dimColor>{` · ${tags.join(' · ')}`}</Text> : null}
      </Box>
      <Box paddingLeft={2}>
        <Text wrap="wrap">{clip(body, 600)}</Text>
      </Box>
    </Box>
  )
}

function inboundTags(f: Frame) {
  return [f.id, f.task ? `task ${f.task}` : '', f.re ? `re ${f.re}` : '', f.task ? '' : f.reply ? 'reply expected' : ''].filter(Boolean)
}

function frame(m: PeerMessage) {
  const from = m.from.includes('@') ? m.from : m.from_name
  // A task is always answered, through update_task, so it needs no "reply expected".
  const tags = [m.id, m.kind === 'announcement' ? 'announcement' : '', m.task ? `task ${m.task}` : '', m.reply_to ? `re ${m.reply_to}` : '', m.expects_reply && !m.task ? 'reply expected' : ''].filter(Boolean)
  return `${MESSAGE_MARK}${from} · ${tags.join(' · ')}\n${m.text}`
}

function framed(batch: PeerMessage[]) {
  return `${batch.map(frame).join('\n\n')}\n\n${REPLY_HINT}`
}

// Where this agent sits, for the status line: its space, whether it leads it, and the space that one sits in.
function placeText() {
  if (!cfg?.space || cfg.space === 'default') return ''
  const inside = cfg.space_parent || cfg.parent
  return ` · ${cfg.leads ? 'leads ' : ''}${cfg.space}${inside ? ` (in ${inside})` : ''}`
}

// The terminal draws peerlink's line at the end of the hint row under the prompt; other surfaces don't draw a
// tail yet, so they get it as the plugin's pinned status instead.
let surface = ''
let statusLine = ''
function showStatus($: EngineInterface) {
  const waiting = pending.length ? ` · ${pending.length} waiting` : ''
  const next = myName ? `⇄ peerlink · ${myName}${placeText()} · ${peerCount} peer${peerCount === 1 ? '' : 's'}${waiting}${daemonVer ? ` · ${daemonVer}` : ''}` : ''
  if (next === statusLine) return
  statusLine = next
  if (surface !== 'terminal') return $.ui.status(next || undefined)
  try {
    $.ui.invalidate('ui.render')
  } catch {}
}

const shownWaiting = new Set<string>()

// When an idle agent wakes for messages from other agents, and whether a check is already set for then.
let wakeAt: number | undefined
let wakeTimer = false

async function deliver($: EngineInterface) {
  if (pending.length === 0 || !cfg) {
    wakeAt = undefined
    return
  }
  const batch = pending
  if (busy) {
    wakeAt = undefined
    if (cfg.inject_while_busy === false) return
    pending = []
    const res = await $.session.append({ message: { type: 'user', content: [{ type: 'text', text: framed(batch) }] } })
    if ('deny' in res && res.deny) {
      pending = [...batch, ...pending]
      return
    }
    // The model reads the row above, which the person never sees: show each message as a transcript line too.
    for (const m of batch) $.ui.log(frame(m).replace('\n', ': '))
    $.ui.toast(`Message from ${batch.map(m => m.from_name).join(', ')}`)
    return
  }
  if (cfg.auto_wake_idle === false) {
    // Not starting a turn is no reason to hide it: each message shows once, and is read with the next prompt.
    for (const m of batch.filter(m => !shownWaiting.has(m.id))) {
      shownWaiting.add(m.id)
      $.ui.log(`${frame(m).replace('\n', ': ')}  (wake is off: read with your next prompt)`)
      $.ui.toast(`Message from ${m.from_name}, waiting for your next prompt`)
    }
    showStatus($)
    return
  }
  // Nothing that needs a turn of its own: it's read with the agent's next turn, whatever starts it.
  if (!batch.some(m => m.wake !== false)) {
    for (const m of batch.filter(m => !shownWaiting.has(m.id))) {
      shownWaiting.add(m.id)
      $.ui.log(`${frame(m).replace('\n', ': ')}  (no reply needed: read with the next turn)`)
    }
    return
  }
  // Another agent's message waits a few seconds, so ones close together share a turn; yours never wait.
  const fromYou = batch.some(m => m.wake !== false && (m.from === 'user' || m.from.startsWith('discord:')))
  const delay = (cfg.wake_delay_s ?? 15) * 1000
  if (!fromYou && delay > 0) {
    const now = await $.clock.now()
    wakeAt ??= now + delay
    if (now < wakeAt) {
      if (!wakeTimer) {
        wakeTimer = true
        void $.clock.sleep(wakeAt - now + 50).then(() => {
          wakeTimer = false
          return deliver($)
        })
      }
      return
    }
  }
  wakeAt = undefined
  pending = []
  busy = true
  $.ui.toast(`Message from ${batch.map(m => m.from_name).join(', ')}`)
  await $.prompt.submit({ text: framed(batch) })
}

async function poll($: EngineInterface) {
  if (!sessionId) return
  // After a /clear or resume the process runs under a new session id, and no session.start says so.
  const now = await $.session.id().catch(() => sessionId)
  if (now && now !== sessionId) {
    const old = sessionId
    sessionId = now
    lastRoster = ''
    await announce($, old)
    return
  }
  let res
  try {
    res = await call($, 'POST', `/api/sessions/${sessionId}/heartbeat`, { busy, activity: busy ? activity : undefined, context: await contextPercent($), branch: await freshBranch($) })
  } catch {
    await ensureDaemon($)
    await announce($)
    return
  }
  if (res.status === 404) {
    await announce($)
    return
  }
  if (!res.ok) return
  const before = cfg?.poll_interval_ms
  cfg = res.data.config
  myName = res.data.name
  const hadPeers = peerCount > 0
  peerCount = Number(res.data.peer_count ?? 0)
  // The first peer to show up gets a real summary to read.
  if (!hadPeers && peerCount > 0 && !hasSummarizedTalk && !busy) void summarize($)
  if (res.data.terminate && !terminating) {
    // The user asked for this session to end: /exit, once the turn it's in is done.
    terminating = true
    const by = res.data.terminate.by ?? 'the user'
    $.ui.toast(`peerlink: ending this session, as asked from ${by}`)
    $.ui.log(`peerlink: ending this session after the current turn, as asked from ${by}.`)
    void $.command.run({ command: 'exit' }).catch(() => undefined)
  }
  const inbox = res.data.inbox as PeerMessage[]
  for (const m of inbox.filter(m => m.kind === 'announcement')) {
    notices.push(m)
    $.ui.log(`${frame(m).replace('\n', ': ')}  (read at the next turn)`)
  }
  pending.push(...inbox.filter(m => m.kind !== 'announcement'))
  if (cfg && before !== cfg.poll_interval_ms) startPolling($)
  void reloadIfUpdated($)
  showStatus($)
  await deliver($)
}

// How full this agent's context window is, so leads and the web UI can see a teammate about to compact.
async function contextPercent($: EngineInterface) {
  try {
    return (await $.session.usage()).context?.percent
  } catch {
    return undefined
  }
}

function oneLine(text: string, max: number) {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat
}

function peerLine(p: any) {
  const where = p.cwd.replace(/^\/Users\/[^/]+/, '~') + (p.branch ? ` [${p.branch}]` : '')
  // Only worth a mention when it's high: a nearly full agent may compact before it answers.
  const full = typeof p.context === 'number' && p.context >= 80 ? ` (context ${p.context}% full)` : ''
  // A role says what the agent is for; the summary, what it's doing now.
  const about = p.role ? `${oneLine(p.role, 120)}${p.summary ? ` · now: ${p.summary.slice(0, 100)}` : ''}` : (p.summary || 'no summary yet').slice(0, 140)
  return `- ${p.name}${p.remote ? '@' + p.host : ''}${spaceTag(p)}${full}: ${where} — ${about}`
}

// The live agents, for the model to read beside the prompt; only when the list changed since it last saw it.
async function rosterContext($: EngineInterface) {
  if (!cfg || cfg.roster_in_prompt === false || cfg.muted) return undefined
  const res = await call($, 'GET', `/api/peers?exclude=${encodeURIComponent(sessionId)}`)
  if (!res.ok) return undefined
  const peers = (res.data.peers as any[]).filter(p => !p.muted).slice(0, 12)
  const roster = peers.map(peerLine).join('\n')
  // Sent again only when who's there, or what they're for, changes: not each time a summary or context moves.
  const who = peers.map(p => [p.name, p.cwd, p.space, p.role, spaceTag(p)].join('|')).join('\n')
  if (who === lastRoster) return undefined
  lastRoster = who
  return peers.length
    ? `Peer agents now live (send_message to reach one):\n${roster}`
    : 'peerlink: no other agents are live in your space now.'
}

async function postSummary($: EngineInterface, next: string, kind: 'seed' | 'model' | 'manual') {
  summary = next.slice(0, 300)
  await call($, 'POST', `/api/sessions/${sessionId}/heartbeat`, { summary, summary_kind: kind })
}

function clip(text: string, n: number) {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > n ? `${flat.slice(0, n - 1)}…` : flat
}

// The project's own description, from its CLAUDE.md or README.
async function projectDescription($: EngineInterface, root: string) {
  for (const file of ['CLAUDE.md', 'README.md', 'readme.md', 'README']) {
    let body = ''
    try {
      body = String(await $.fs.read(`${root}/${file}`))
    } catch {
      continue
    }
    const line = body
      .split('\n')
      .map(l => l.trim())
      // `/init` starts every CLAUDE.md with the same sentence; it says nothing about the project.
      .find(l => l.length > 30 && !/^(#|<|!\[|\[!|```|\||-{3})/.test(l) && !/^This file provides guidance to Claude Code/i.test(l))
    if (line) return line.replace(/[*_`]/g, '')
  }
  return ''
}

// What the most recent earlier Claude Code session in this folder was about: its own title, kept in its transcript.
async function lastSessionTitle($: EngineInterface, cwd: string) {
  const dir = `${(await $.env.get('HOME')) ?? ''}/.claude/projects/${cwd.replace(/[^A-Za-z0-9]/g, '-')}`
  let entries: any[] = []
  try {
    entries = (await $.fs.list(dir)) as any[]
  } catch {
    return undefined
  }
  const cutoff = (await $.clock.now()) - HISTORY_DAYS * 86_400_000
  const recent = entries
    .filter(f => f.name.endsWith('.jsonl') && !f.name.startsWith(sessionId) && f.mtimeMs > cutoff)
    .sort((a, b) => b.mtimeMs - a.mtimeMs)
    .slice(0, 3)
  for (const f of recent) {
    const found = await $.process.run(['grep', '-h', '"type":"ai-title"', `${dir}/${f.name}`], { timeoutMs: 5000 })
    const line = found.stdout.trim().split('\n').at(-1)
    if (!line) continue
    try {
      const title = JSON.parse(line).aiTitle
      if (title) return { title: String(title), mtimeMs: f.mtimeMs as number }
    } catch {}
  }
  return undefined
}

// A first summary with no model call: what the last agent here did, the project, and its recent commits.
async function seedSummary($: EngineInterface, cwd: string) {
  const root = (await $.session.repo())?.root ?? cwd
  const name = root.split('/').filter(Boolean).at(-1) ?? 'project'
  const [description, title, commits] = await Promise.all([
    projectDescription($, root),
    lastSessionTitle($, cwd),
    git($, cwd, ['log', '-3', `--since=${HISTORY_DAYS}.days`, '--format=%s']),
  ])
  const parts = [description ? `${name}: ${clip(description, 110)}` : name]
  const prevMs = previous ? previous.ts * 1000 : 0
  if (previous && prevMs >= (title?.mtimeMs ?? 0)) parts.push(`last agent here: ${clip(previous.summary, 110)}`)
  else if (title) parts.push(`last session here: ${clip(title.title, 80)}`)
  if (commits) parts.push(`recent commits: ${clip(commits.split('\n').join('; '), 90)}`)
  return parts.join(' · ')
}

async function summarize($: EngineInterface) {
  const now = await $.clock.now()
  if (!cfg || cfg.auto_summary === false || summarizing || !sessionId) return
  // Nobody to read it: spend nothing.
  if (peerCount === 0) return
  if (now - manualSummaryAt < MANUAL_SUMMARY_HOLD_MS) return
  if (hasSummarizedTalk && (turnsSinceSummary < SUMMARY_EVERY_TURNS || now - lastSummaryAt < SUMMARY_MIN_GAP_MS)) return
  summarizing = true
  try {
    const messages = (await $.session.messages()) as any[]
    const transcript = messages
      .slice(-8)
      .map(m => {
        const tools = m.toolUses?.length ? ` [tools: ${m.toolUses.map((t: any) => t.tool).join(', ')}]` : ''
        return `${m.role}: ${String(m.text).slice(0, 800)}${tools}`
      })
      .join('\n\n')
      .slice(-6000)
    if (!transcript.trim()) return
    const done = await $.model.complete({
      model: cfg.summary_model || 'haiku',
      system: SUMMARY_SYSTEM,
      prompt: `Directory: ${await $.session.cwd()}\nCurrent status: ${summary || '(none)'}\n\nRecent conversation:\n${transcript}`,
      maxTokens: 100,
      effort: 'low',
      timeoutMs: 20000,
    })
    if (!done.isAnswered) return
    lastSummaryAt = now
    turnsSinceSummary = 0
    hasSummarizedTalk = true
    const next = done.text.trim()
    if (next && next !== 'UNCHANGED' && next !== summary) await postSummary($, next, 'model')
  } catch {
  } finally {
    summarizing = false
  }
}

function startPolling($: EngineInterface) {
  cancelPoll?.()
  const timer = $.clock.every(cfg?.poll_interval_ms ?? 3000, () => poll($))
  cancelPoll = () => timer.cancel()
}

function text(t: string) {
  return { result: t } as any
}

const daemonDown = () => text('The peerlink daemon is unreachable; it restarts on the next poll, try again shortly.')

async function listPeers($: EngineInterface, withIds: boolean) {
  const res = await call($, 'GET', `/api/peers?exclude=${encodeURIComponent(sessionId)}`)
  if (!res.ok) return `Daemon error: ${res.data.error ?? res.status}`
  const peers = res.data.peers as any[]
  if (peers.length === 0) return 'No other agents are live right now.'
  return peers
    .map(
      p =>
        `- ${p.name}${p.remote ? '@' + p.host : ''}${withIds ? ` (id ${p.id})` : ''}${spaceTag(p)} — ${p.busy ? 'busy' : 'idle'}${p.muted ? ', muted' : ''}\n  dir: ${p.cwd}${p.branch ? ` [${p.branch}]` : ''}${p.role ? `\n  role: ${oneLine(p.role, 400)}` : ''}\n  summary: ${p.summary || '(none)'}`,
    )
    .join('\n')
}

function spaceLine() {
  if (!cfg?.space) return 'space: default'
  const from =
    cfg.space_source === 'override' ? 'override for this agent'
    : cfg.space_source === 'file' ? `from ${cfg.space_from}`
    : cfg.space_source === 'rule' ? `folder rule ${cfg.space_from}`
    : 'no space set'
  const nest = cfg.parent ? `, leads it inside "${cfg.parent}"` : cfg.leads ? ', leads it' : cfg.lead ? `, led by ${cfg.lead}` : ''
  const also = cfg.also?.length ? `; also a member of ${cfg.also.join(', ')}` : ''
  return `space: ${cfg.space} (${from}${nest})${also}`
}

// Where a peer sits relative to this agent: a lead speaks for its space; a peer in another space is the lead's parent.
function spaceTag(p: any) {
  // `parent` alone: a peer on another machine whose daemon predates `leads`.
  if (p.leads || p.parent) return ` [leads ${p.space}]`
  return cfg && p.space && p.space !== cfg.space ? ` [${p.space}]` : ''
}

// What a place in a nested space asks of this agent; the daemon enforces who can reach whom.
function hierarchyGuidance() {
  if (!cfg) return ''
  const lines: string[] = []
  // Your user's brief for this agent, from its folder's .peerlink; other agents see it too.
  if (cfg.role) lines.push(`Your role here, as your user set it in this folder's .peerlink (other agents see it too): ${cfg.role}`)
  const chans = (cfg.discord ?? []).filter(c => c.channel_id !== 'dm')
  const dm = (cfg.discord ?? []).some(c => c.channel_id === 'dm')
  if (chans.length || dm) {
    const masters = chans.filter(c => c.master).map(c => c.channel)
    const members = chans.filter(c => !c.master).map(c => c.channel)
    lines.push(
      [
        'Your user can reach you on Discord.',
        masters.length ? `You're the channel master of ${masters.join(', ')}: their messages there come to you.` : '',
        members.length ? `You're also in ${members.join(', ')}, where they reach you by starting a message with your name.` : '',
        dm ? 'Their direct messages to the bot come to you.' : '',
        'A message from Discord reads "<name> on Discord": answer it with send_message and reply_to, and the answer is posted there.',
        chans.length
          ? `To tell them something first, send_message to "discord"${chans.length > 1 ? ` plus the channel, like "discord${chans[0]!.channel}"` : ''}.`
          : '',
        'They read it on a phone: lead with the answer, keep it short, use plain markdown, no tables. Links work, as [text](url).',
        'When you need them to decide or approve something (yes or no, merge or not, pick one), pass options rather than asking them to type a reply: they become buttons, and the tap comes back as their reply. To share a file, pass its absolute path in files. Files they attach arrive as paths you can read.',
      ]
        .filter(Boolean)
        .join(' '),
    )
  }
  if (cfg.parent)
    lines.push(
      `You lead the "${cfg.space}" space and speak for it in the "${cfg.parent}" space. Agents in "${cfg.parent}" can only reach "${cfg.space}" through you. When one asks for something, decide which agent in "${cfg.space}" owns it, delegate with send_message (or do it yourself if it is yours), and when the work is done report the outcome back to whoever asked, with reply_to their message id.`,
    )
  else if (cfg.leads)
    lines.push(
      `You lead the "${cfg.space}" space. Work sent to the space (to "space:${cfg.space}") comes to you: decide which agent in it owns the work, delegate with send_message (or do it yourself if it is yours), and report the outcome to whoever asked, with reply_to their message id.`,
    )
  else if (cfg.lead)
    lines.push(`${cfg.lead} leads the "${cfg.space}" space and relays work from outside it. Report results to ${cfg.lead}, and ask it for anything that lives outside your space.`)
  if (cfg.also?.length)
    lines.push(
      `Your user also made you a member of ${cfg.also.map(s => `"${s}"`).join(', ')}: you see and message the agents there, and they see you. Your own space is still "${cfg.space}".`,
    )
  const kids = cfg.children ?? []
  if (kids.length)
    lines.push(
      `Some spaces sit inside yours, each spoken for by its lead: ${kids.map(k => `${k.name} (leads "${k.space}")`).join(', ')}. You can't reach their agents directly: give the lead the outcome you want and let it delegate, rather than telling it which agent should do what.`,
    )
  return lines.length ? `\n${lines.join('\n')}` : ''
}

// Merged by the daemon, so two agents changing settings at once can't undo each other.
async function setAgentConfig($: EngineInterface, id: string, patch: Record<string, unknown>) {
  return call($, 'POST', `/api/sessions/${encodeURIComponent(id)}/config`, patch)
}

async function findAgent($: EngineInterface, name: string) {
  const res = await call($, 'GET', '/api/peers?local=1')
  const want = name.toLowerCase()
  const hits = ((res.data.peers ?? []) as any[]).filter(p => p.name.toLowerCase() === want || p.id === name)
  return hits.length === 1 ? hits[0] : undefined
}

// One command per topic, each with its own hint; `/peerlink <sub>` still runs every subcommand.
const COMMANDS: { name: string; description: string; argumentHint: string; args: (a: string) => string }[] = [
  {
    name: 'peerlink',
    description: 'peerlink: status, web UI, reported issues, restart, rotate-key (help lists every command)',
    argumentHint: '[status | web | update | issues | restart | rotate-key | help]',
    args: a => a || 'status',
  },
  {
    name: 'peerlink-spaces',
    description: "peerlink: the space tree, or set this agent's space, .peerlink file or parent space",
    argumentHint: '[<name> | clear | file <name> | parent <name>]',
    args: a => `spaces ${a}`,
  },
  {
    name: 'peerlink-peers',
    description: 'peerlink: list live agents, or mute and unmute one',
    argumentHint: '[mute <agent> | unmute <agent>]',
    args: a => (/^(mute|unmute)\b/.test(a) ? a : a ? 'usage peers' : 'peers'),
  },
  {
    name: 'peerlink-agent',
    description: "peerlink: this agent's name, summary and wake on message",
    argumentHint: '[name <name> | summary <text> | wake on|off|default]',
    args: a => (/^(name|summary|wake)\b/.test(a) ? a : a ? 'usage agent' : 'agent'),
  },
]

const USAGE: Record<string, string> = {
  peers: 'Usage: /peerlink-peers [mute <agent> | unmute <agent>]',
  agent: 'Usage: /peerlink-agent [name <name> | summary <text> | wake on|off|default]',
}

// The spaces on this machine as an org chart: a space lists its own agents and the leads of the spaces under it,
// each lead's team indented beneath it. Spaces with teams come first, so the top of the org is at the top.
async function spaceTree($: EngineInterface) {
  const res = await call($, 'GET', '/api/peers')
  if (!res.ok) return `Daemon error: ${res.data.error ?? res.status}`
  const peers = res.data.peers as any[]
  const groups: Record<string, any[]> = {}
  const lead: Record<string, any> = {}
  const kids: Record<string, string[]> = {}
  for (const p of peers) (groups[p.space || 'default'] ??= []).push(p)
  for (const p of peers)
    if (p.parent && !lead[p.space]) {
      lead[p.space] = p
      ;(kids[p.parent] ??= []).push(p.space)
      groups[p.parent] ??= []
    }
  const size = (n: string, seen = new Set<string>()): number =>
    seen.has(n) ? 0 : (seen.add(n), (groups[n]?.length ?? 0) + (kids[n] ?? []).reduce((t, c) => t + size(c, seen), 0))
  const order = (a: string, b: string) =>
    a === 'default' ? 1 : b === 'default' ? -1 : Number(!!kids[b]?.length) - Number(!!kids[a]?.length) || size(b) - size(a) || a.localeCompare(b)
  const seen = new Set<string>()
  const lines: string[] = []
  const tier = (space: string, pad: string) => {
    seen.add(space)
    // Every lead of a space sits with its parent (two sessions in a lead's folder are both leads).
    const isLead = (p: any) => !!lead[space] && lead[space].parent !== space && p.parent && p.space === space
    // A space's own lead comes first.
    const own = (groups[space] ?? []).filter(p => !isLead(p)).sort((a, b) => Number(!!b.leads && !b.parent) - Number(!!a.leads && !a.parent) || a.name.localeCompare(b.name))
    const leads = (kids[space] ?? []).filter(c => c !== space).sort().flatMap(c => (groups[c] ?? []).filter(p => p.parent && p.space === c))
    const all = [...own, ...leads]
    const width = Math.max(0, ...all.map(p => p.name.length))
    for (const p of all) {
      const tag = p.parent && p.space !== space ? `  leads ${p.space}` : ''
      const own = p.leads && !p.parent ? (p.leads === 'only' ? '  lead (only agent)' : '  lead') : ''
      lines.push(`${pad}${p.name.padEnd(width)}  ${p.cwd.replace(/^\/Users\/[^/]+/, '~')}${tag}${own}${p.id === sessionId ? '  ← you' : ''}`)
      if (tag && !seen.has(p.space) && p === leads.filter(l => l.space === p.space).at(-1)) {
        lines.push(`${pad}  └ ${p.space} team`)
        tier(p.space, `${pad}      `)
      }
    }
  }
  const roots = Object.keys(groups).filter(n => !lead[n] || lead[n].parent === n).sort(order)
  for (const n of [...roots, ...Object.keys(groups).sort(order)]) {
    if (seen.has(n)) continue  // a space caught in a parent loop shows at the top level, after the rest
    lines.push(n)
    tier(n, '  ')
  }
  return lines.length ? lines.join('\n') : 'No live agents.'
}

// A report reaches the daemon if it can be started; otherwise it waits in a file the daemon files when it next starts.
async function reportIssue($: EngineInterface, what: string, details: string, type = 'bug') {
  if (!what.trim()) return `Not reported: say ${type === 'idea' ? 'what you suggest' : 'what went wrong'}.`
  const context = `agent ${myName}, ${spaceLine()}, cwd ${await $.session.cwd()}`
  const body = { from: sessionId, from_name: myName, type, text: details ? `${what}\n\n${details}` : what, context, plugin: DAEMON_VERSION, space: cfg?.space ?? '' }
  if (await ensureDaemon($).catch(() => false)) {
    const res = await call($, 'POST', '/api/issues', body).catch(() => undefined)
    if (res?.ok)
      return `${type === 'idea' ? 'Suggested' : 'Reported'} as ${res.data.id}${res.data.sent_to ? ` and sent to ${res.data.sent_to}` : ''}; it's in the feedback queue your user reviews.`
  }
  const now = await $.clock.now()
  const path = `${await peerlinkHome($)}/issues-pending/${now}-${sessionId.slice(0, 8)}.json`
  await $.fs.write(path, JSON.stringify({ ...body, ts: now / 1000 }))
  return `The daemon is down, so the report was saved to ${path}; it is filed when the daemon next starts.`
}

function ago(s: number) {
  return s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)}m` : `${Math.round(s / 3600)}h`
}

async function openThreadsBlock($: EngineInterface) {
  if (!sessionId || !cfg || cfg.muted) return ''
  const res = await call($, 'GET', `/api/sessions/${sessionId}/threads`)
  if (!res.ok) return ''
  const owes = res.data.owes as any[]
  const waiting = res.data.waiting as any[]
  const tasks = (res.data.tasks ?? []) as any[]
  const asked = (res.data.asked ?? []) as any[]
  if (!owes.length && !waiting.length && !tasks.length && !asked.length) return ''
  const line = (t: any) => `- ${t.id} ${t.with} (${ago(t.ago_s)} ago): ${clip(t.text, 140)}`
  const tline = (t: any) => `- ${t.id} ${t.status === 'in_progress' ? 'in progress' : 'open'}, ${t.with} (${ago(t.ago_s)} ago): ${clip(t.text, 140)}`
  return [
    'Peer requests still open from earlier in this folder (they survive compaction and /clear):',
    ...(tasks.length ? ['Your open tasks:', ...tasks.map(tline)] : []),
    ...(owes.length ? ['You owe a reply to:', ...owes.map(line)] : []),
    ...(asked.length ? ['Tasks you handed out, still open:', ...asked.map(tline)] : []),
    ...(waiting.length ? ["You're waiting on:", ...waiting.map(line)] : []),
    ...(tasks.length ? ['Finish a task with update_task, done or failed and a short note: whoever asked gets the note.'] : []),
    ...(owes.length || waiting.length
      ? ["Answer with send_message and reply_to the id. If you already did the work, say so briefly; a waiting reply arrives as a message."]
      : []),
  ].join('\n')
}

async function runCommand($: EngineInterface, args: string): Promise<string> {
  const [sub = 'help', ...rest] = args.trim().split(/\s+/).filter(Boolean)
  const arg = rest.join(' ')
  if (sub === 'help') return HELP
  if (!(await ensureDaemon($))) return 'The peerlink daemon is not running and could not start (it needs python3).'
  switch (sub) {
    case 'web': {
      const res = await call($, 'POST', '/api/web/link', {})
      if (!res.ok || !res.data.url) return `Could not make a sign-in link: ${res.data.error ?? res.status}`
      await $.process.run(['open', res.data.url])
      return `Opened the web UI with a one-time sign-in link (valid ${res.data.expires_in_s}s).`
    }
    case 'status': {
      const st = (await call($, 'GET', '/api/status')).data
      return `daemon ${st.version} (pid ${st.pid}) · ${st.sessions} live agents · ${st.queued} queued messages · ${st.browsers} signed-in browsers\nthis agent: ${myName} · ${spaceLine()} · wake on message: ${cfg?.auto_wake_idle === false ? 'off' : 'on'}${cfg?.muted ? ' · muted' : ''}\nsummary: ${summary || '(none yet)'}`
    }
    case 'update': {
      // Pull the latest peerlink and install it; every agent then reloads itself when it's next idle.
      const steps = [
        ['claude', 'plugin', 'marketplace', 'update', 'peerlink'],
        ['claude', 'plugin', 'update', 'peerlink@peerlink'],
      ]
      const out: string[] = []
      for (const argv of steps) {
        const run = await $.process.run(argv, { timeoutMs: 120_000 })
        out.push((run.stdout || run.stderr).trim().split('\n').at(-1) ?? '')
        if (run.exitCode !== 0) return `Update stopped at \`${argv.join(' ')}\`:\n${out.join('\n')}`
      }
      lastInstallCheck = -Infinity
      void reloadIfUpdated($)
      return `${out.join('\n')}\nEvery agent picks it up by itself when it's next idle.`
    }
    case 'issues': {
      const res = await call($, 'GET', '/api/issues?limit=10')
      if (!res.ok) return `Daemon error: ${res.data.error ?? res.status}`
      const list = res.data.issues as any[]
      if (!list.length) return `No issues reported. (${res.data.path})`
      return list.map(i => `${i.id}  ${i.from_name || '?'} (${i.space || '?'}): ${clip(i.text, 140)}`).join('\n') + `\n\nAll reports: ${res.data.path}`
    }
    case 'usage':
      return USAGE[arg] ?? HELP
    case 'peers':
      return listPeers($, false)
    case 'agent':
      return `this agent: ${myName} · ${spaceLine()} · wake on message: ${cfg?.auto_wake_idle === false ? 'off' : 'on'}${cfg?.muted ? ' · muted' : ''}\nsummary: ${summary || '(none yet)'}`
    case 'spaces':
      if (rest[0] === 'help') return HELP
      if (!rest.length) return `This agent's ${spaceLine()}.\n\n${await spaceTree($)}`
      return runCommand($, `space ${arg}`)
    case 'name': {
      if (!arg) return 'Usage: /peerlink-agent name <name>'
      await setAgentConfig($, sessionId, { nickname: arg })
      await poll($)
      return `This agent is now "${myName}".`
    }
    case 'summary': {
      if (!arg) return `Summary: ${summary || '(none yet)'}`
      manualSummaryAt = await $.clock.now()
      await postSummary($, arg, 'manual')
      return 'Summary updated.'
    }
    case 'wake': {
      const value = arg === 'on' ? true : arg === 'off' ? false : arg === 'default' ? null : undefined
      if (value === undefined) return 'Usage: /peerlink-agent wake on|off|default'
      await setAgentConfig($, sessionId, { auto_wake: value })
      await poll($)
      return `Wake on message: ${cfg?.auto_wake_idle === false ? 'off' : 'on'}${value === null ? ' (the default)' : ''}.`
    }
    case 'mute':
    case 'unmute': {
      const agent = arg && (await findAgent($, arg))
      if (!agent) return `No single live agent named "${arg}". /peerlink-peers lists them.`
      await setAgentConfig($, agent.id, { muted: sub === 'mute' })
      return `${agent.name} is ${sub === 'mute' ? 'muted' : 'unmuted'}.`
    }
    case 'space': {
      const [what = '', name = ''] = rest
      if (!what) return `This agent's ${spaceLine()}.`
      if (what === 'parent') {
        if (!name) return 'Usage: /peerlink-spaces parent <name> | clear'
        const res = await call($, 'POST', `/api/sessions/${sessionId}/space-file`, { parent: name === 'clear' ? '' : name })
        if (!res.ok) return `Could not write .peerlink: ${res.data.error ?? res.status}`
        await poll($)
        return `${name === 'clear' ? 'Cleared the parent in' : 'Wrote'} ${res.data.path}. This agent's ${spaceLine()}.`
      }
      if (what === 'file') {
        if (!name) return 'Usage: /peerlink-spaces file <name> | clear'
        const res = await call($, 'POST', `/api/sessions/${sessionId}/space-file`, { space: name === 'clear' ? '' : name })
        if (!res.ok) return `Could not write .peerlink: ${res.data.error ?? res.status}`
        await poll($)
        return `${name === 'clear' ? 'Cleared the space in' : 'Wrote'} ${res.data.path}. This agent's ${spaceLine()}.`
      }
      await setAgentConfig($, sessionId, { space: what === 'clear' ? '' : what })
      await poll($)
      return `This agent's ${spaceLine()}.`
    }
    case 'restart': {
      await call($, 'POST', '/api/restart', {})
      await $.clock.sleep(1500)
      return (await ensureDaemon($)) ? 'The daemon restarted. Agents reconnect by themselves.' : 'The daemon did not come back; see ~/.peerlink/daemon.log.'
    }
    case 'rotate-key': {
      await call($, 'POST', '/api/key/rotate', {})
      await loadKey($)
      return 'New key written. Every browser is signed out; agents pick up the new key by themselves.'
    }
    default:
      return `Unknown command "${sub}".\n\n${HELP}`
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    surface = (e as any).surface ?? ''
    sessionId = await $.session.id()
    await $.tool.register({
      name: 'list_peers',
      description:
        "List live peer agents with their folder, branch, current work and whether they're busy. Peers already appear beside the prompt when they change, so call this only for ids or busy state.",
      inputSchema: { type: 'object', properties: {} },
      isDeferred: false,
    })
    await $.tool.register({
      name: 'send_message',
      description:
        'Message a peer agent by name, like messaging a teammate. It arrives at once, starting a turn there if the agent is idle. Keep it short, point to file paths rather than pasting, and set reply_to when answering.',
      inputSchema: {
        type: 'object',
        properties: {
          to: {
            type: 'string',
            description:
              'Peer agent name or id; "space:<name>" for that space\'s lead (or "space" for your own); or "user" for your user (it waits in their Inbox). Leave it out when replying, the reply goes back to the sender',
          },
          text: { type: 'string', description: 'The message: what you need and why, briefly' },
          expects_reply: { type: 'boolean', description: 'True if you need an answer' },
          reply_to: { type: 'string', description: 'Id of the peer message you are answering; the reply goes to its sender' },
          kind: {
            type: 'string',
            enum: ['message', 'task', 'announcement'],
            description:
              'message (the default); task: work you hand to that agent, tracked until its owner marks it done or failed with update_task, and you get its note then (a task to a space lead is the lead\'s to delegate); announcement: to "space" (or "space:<name>"), read by every agent there at its next turn, no replies. Announce only what everyone needs, such as a shared API change',
          },
          parent_task: { type: 'string', description: 'When delegating part of a task you own: its id (t…), so the subtask shows under it' },
          options: {
            type: 'array',
            items: { type: 'string' },
            description: 'Choices to offer, short labels, such as ["Merge both", "Hold"]. Use them whenever you ask your user to decide or approve, instead of asking them to type a reply. On Discord they are buttons and the tap comes back as a reply; to a peer, a numbered list',
          },
          files: {
            type: 'array',
            items: { type: 'string' },
            description: 'Absolute paths of files to attach. On Discord they are uploaded; to a peer, the paths are listed',
          },
        },
        required: ['text'],
      },
      isDeferred: false,
    })
    await $.tool.register({
      name: 'update_task',
      description:
        'Update a task you own (one that came to you with "task t…"): in_progress while you work on it, done when finished, or failed when you can\'t finish it. The note is your result, and it goes to whoever asked, so you don\'t need to message them as well. Whoever handed out a task can cancel it.',
      inputSchema: {
        type: 'object',
        properties: {
          id: { type: 'string', description: 'The task id, e.g. t3' },
          status: { type: 'string', enum: ['in_progress', 'done', 'failed', 'cancelled'] },
          note: { type: 'string', description: 'The result, or why it failed: briefly, with paths rather than pasted content' },
        },
        required: ['id', 'status'],
      },
      isDeferred: false,
    })
    await $.tool.register({
      name: 'request_spawn',
      description:
        "Ask your user to start a new agent (a Claude Code session) in another folder, for work that belongs there. Nothing starts until they approve it in the web UI or on Discord. If they do, the agent starts in your space, or the one you name, and gets first_prompt from you as its first message, so its replies come back to you. You're told if they deny it, or don't answer within a day.",
      inputSchema: {
        type: 'object',
        properties: {
          dir: { type: 'string', description: 'The folder to start it in; it must exist already' },
          purpose: { type: 'string', description: 'Why it is needed, in a sentence your user decides on' },
          first_prompt: { type: 'string', description: 'What the new agent should do first' },
          space: { type: 'string', description: 'The space it joins; yours if left out' },
        },
        required: ['dir', 'purpose', 'first_prompt'],
      },
      isDeferred: false,
    })
    await $.tool.register({
      name: 'report_issue',
      description:
        "Report a problem with peerlink itself (a message that never arrived or reached the wrong agent, a peer you expected that isn't listed, an error from these tools), or suggest how peerlink could work better for you. It goes into the feedback queue your user reviews; a bug also goes straight to the agent working on peerlink when one is live. Not for problems with your own task or a peer's work.",
      inputSchema: {
        type: 'object',
        properties: {
          type: { type: 'string', enum: ['bug', 'idea'], description: 'bug (the default): something broke. idea: an improvement you would like' },
          what: { type: 'string', description: 'What went wrong and what you expected, or the improvement, briefly' },
          details: { type: 'string', description: 'Message ids, agent names, exact error text' },
        },
        required: ['what'],
      },
      isDeferred: false,
    })
    await $.tool.register({
      name: 'set_summary',
      description:
        'Set the one-line summary peer agents see for you (what you are working on and what you can help with). Update it when your focus changes.',
      inputSchema: {
        type: 'object',
        properties: { summary: { type: 'string' } },
        required: ['summary'],
      },
      isDeferred: false,
    })
    for (const { name, description, argumentHint } of COMMANDS) await $.command.register({ name, description, argumentHint })
    if (await ensureDaemon($, true)) {
      await announce($)
      $.ui.log(`peerlink ${DAEMON_VERSION} · daemon ${daemonVer} · ${myName} · ${spaceLine()}`)
      startPolling($)
      showStatus($)
      if ((await $.session.turns()) > 0) {
        void poll($).then(() => summarize($))
      } else {
        void seedSummary($, e.cwd)
          .then(seed => postSummary($, seed, 'seed'))
          .catch(() => {})
      }
    } else {
      $.ui.toast('peerlink: could not start the daemon (needs python3)')
    }
    return started
  })

  on('session.end', async ($, e, next) => {
    // /clear and resume end a conversation, not the agent: the process goes on, so keep polling and let the
    // next poll register the new session as this one's successor.
    if (e.reason === 'clear' || e.reason === 'resume') return next(e)
    cancelPoll?.()
    if (sessionId) {
      try {
        await call($, 'DELETE', `/api/sessions/${sessionId}`)
      } catch {}
    }
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) {
      busy = true
      activity = 'thinking'
    }
    return next(e)
  })

  // Any tool the main agent calls: the reaction moves to 🛠️ (or ❓ for a question to you) until it returns.
  on('tool.call', async ($, e, next) => {
    if ((e as any).agentId !== undefined) return next(e)
    const tool = String((e as any).tool ?? '')
    activity = tool === 'AskUserQuestion' ? 'asking' : 'tools'
    try {
      return await next(e)
    } finally {
      activity = 'thinking'
    }
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    if (e.agentId === undefined) {
      busy = false
      turnsSinceSummary++
      void deliver($)
      void summarize($)
    }
    return done
  })

  on('prompt.submit', async ($, e, next) => {
    // Until a real summary exists, the first prompt says what this agent is for.
    if (sessionId && !e.isMeta && !e.command && !hasSummarizedTalk && (await $.session.turns()) === 0 && e.text.trim()) {
      const task = clip(e.text, 110)
      const base = summary.split(' · ')[0] ?? ''
      void postSummary($, `${base}${base ? ' · ' : ''}now: ${task}`, 'seed').catch(() => {})
    }
    if (!sessionId || e.command) return next(e)
    const roster = await rosterContext($)
    return next(roster ? { ...e, context: [...(e.context ?? []), roster] } : e)
  }).catch(($, e, next) => next(e))

  // A conversation's first context, and again after compaction or /clear: what this agent still owes peers and
  // waits on, so a reset context doesn't drop a commitment.
  on('prompt.context', async ($, e, next) => {
    const out = await next(e)
    const threads = await openThreadsBlock($).catch(() => '')
    const heard = notices.splice(0)
    const blocks = [
      ...(heard.length
        ? [{ name: 'peerlink announcements', text: `Announced to your space since your last turn (no reply expected):\n\n${heard.map(frame).join('\n\n')}` }]
        : []),
      ...(threads ? [{ name: 'peerlink open threads', text: threads }] : []),
    ]
    return blocks.length ? { ...out, blocks: [...out.blocks, ...blocks] } : out
  })

  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    if (!cfg || cfg.muted) return composed
    return {
      sections: [
        ...composed.sections,
        {
          id: 'peerlink:guidance',
          scope: 'session',
          text: `# Peer agents\nPeers know you as "${myName}"${cfg.space && cfg.space !== 'default' ? ` in the "${cfg.space}" space; you only see agents in that space${[cfg.parent, ...(cfg.also ?? [])].filter(Boolean).map(s => ` and "${s}"`).join('')}` : ''}.${hierarchyGuidance()}\n${cfg.guidance}`,
        },
      ],
    }
  })

  on('tool.call', { tool: 'mcp__peerlink__list_peers' }, async $ => text(await listPeers($, true))).catch(daemonDown)

  on('tool.call', { tool: 'mcp__peerlink__send_message' }, async ($, e) => {
    const input = e as any
    const res = await call($, 'POST', '/api/messages', {
      from: sessionId,
      from_name: myName,
      to: input.to,
      text: input.text,
      expects_reply: !!input.expects_reply,
      reply_to: input.reply_to,
      kind: input.kind === 'task' ? 'task' : undefined,
      parent_task: typeof input.parent_task === 'string' && input.parent_task ? input.parent_task : undefined,
      options: Array.isArray(input.options) ? input.options : undefined,
      files: Array.isArray(input.files) ? input.files : undefined,
    })
    if (!res.ok) return text(`Not sent: ${res.data.error ?? res.status}`)
    if (res.data.task)
      return text(`Sent to ${res.data.to} (${res.data.id}) as task ${res.data.task}. You'll get its note when it's done or failed; continue with other work meanwhile.`)
    return text(
      `Sent to ${res.data.to} (${res.data.id}).${input.expects_reply ? ' Their reply will arrive as a peer message; continue with other work meanwhile.' : ''}`,
    )
  }).catch(daemonDown)

  on('tool.call', { tool: 'mcp__peerlink__request_spawn' }, async ($, e) => {
    const input = e as any
    const res = await call($, 'POST', '/api/spawns', { by: sessionId, dir: input.dir, purpose: input.purpose, first_prompt: input.first_prompt, space: input.space })
    if (!res.ok) return text(`Not asked: ${res.data.error ?? res.status}`)
    return text(`Asked your user to approve ${res.data.id}.${res.data.note ?? ''} You'll get a message when they decide; carry on meanwhile.`)
  }).catch(daemonDown)

  on('tool.call', { tool: 'mcp__peerlink__update_task' }, async ($, e) => {
    const input = e as any
    const id = String(input.id ?? '').trim()
    const res = await call($, 'POST', `/api/tasks/${encodeURIComponent(id)}`, { by: sessionId, status: input.status, note: input.note })
    if (!res.ok) return text(`Not updated: ${res.data.error ?? res.status}`)
    const t = res.data.task
    return text(`Task ${t.id} is ${t.status.replace('_', ' ')}.${t.status === 'done' || t.status === 'failed' ? ` ${t.requester?.name || 'Whoever asked'} gets your note.` : ''}`)
  }).catch(daemonDown)

  on('tool.call', { tool: 'mcp__peerlink__report_issue' }, async ($, e) => text(await reportIssue($, String((e as any).what ?? ''), String((e as any).details ?? ''), (e as any).type === 'idea' ? 'idea' : 'bug')))

  on('tool.call', { tool: 'mcp__peerlink__set_summary' }, async ($, e) => {
    manualSummaryAt = await $.clock.now()
    await postSummary($, String((e as any).summary ?? ''), 'manual')
    return text('Summary updated.')
  }).catch(daemonDown)

  for (const c of COMMANDS)
    on('command.run', { command: c.name }, async ($, e) => ({ text: await runCommand($, c.args((e.args ?? '').trim())) })).catch(() => ({
      text: 'peerlink: the daemon did not answer that; try again shortly, or see ~/.peerlink/daemon.log.',
    }))

  // A peer message that started a turn here: one row per message instead of the raw prompt (ctrl+o shows it whole).
  // Claude Code's hint row as it draws it, then peerlink's line on its own row beneath, the lowest on screen. The
  // line is a plain Button, not a Link: a click raises it in any terminal, where a Link needs hyperlink support
  // (without it the terminal prints the URL after the text). The press opens the web UI with a fresh sign-in.
  on('ui.render', { component: 'PromptHint' }, async ($, e, next) => {
    if (!statusLine || surface !== 'terminal') return next(e)
    const ui = $.ui.resolve(e) as any
    const engine = await next(e)
    return (
      <ui.Box flexDirection="column">
        {engine}
        <ui.Button key="peerlink-status" plain dimColor onPress={() => void runCommand($, 'web')}>
          {statusLine}
        </ui.Button>
      </ui.Box>
    )
  })

  on('ui.render', { component: 'UserMessage' }, async ($, e, next) => {
    // Claude Code wraps a plugin's prompt in its own framing; the marker is inside.
    if (e.props.isExpanded || !e.props.text.includes(REPLY_HINT)) return next(e)
    const frames = parseFrames(e.props.text)
    if (!frames.length) return next(e)
    const ui = $.ui.resolve(e) as any
    return (
      <ui.Box flexDirection="column" marginTop={1}>
        {frames.map(f => MessageRow(ui, '↘', f.from, inboundTags(f), f.body, f.id))}
      </ui.Box>
    )
  })

  // The row above already says it was sent; keep the result line for failures only.
  on('ui.render', { component: 'ToolResult', props: { tool: 'mcp__peerlink__send_message' } }, async ($, e, next) => {
    const output = typeof e.props.output === 'string' ? e.props.output : ''
    if (e.props.isExpanded || e.props.isErrored || !output.startsWith('Sent to')) return next(e)
    const { Box } = $.ui.resolve(e) as any
    return <Box />
  })

  // A message this agent sends: who to and what, instead of the tool call's JSON.
  on('ui.render', { component: 'ToolUse', props: { tool: 'mcp__peerlink__send_message' } }, async ($, e, next) => {
    if (e.props.isExpanded) return next(e)
    const input = (e.props.input ?? {}) as any
    if (typeof input.to !== 'string' || typeof input.text !== 'string') return next(e)
    const output = typeof e.props.output === 'string' ? e.props.output : ''
    const sent = output.match(/^Sent to (.+?) \((m\d+)\)/)
    const failed = output.startsWith('Not sent') || e.props.isErrored
    const tags = [
      sent ? sent[2] : '',
      input.reply_to ? `re ${input.reply_to}` : '',
      input.expects_reply ? 'reply expected' : '',
      e.props.isRunning ? 'sending…' : '',
    ].filter(Boolean)
    const ui = $.ui.resolve(e) as any
    return (
      <ui.Box flexDirection="column" marginTop={1}>
        {MessageRow(ui, '↗', sent ? sent[1] : input.to, tags, input.text, 'out')}
        {failed ? <ui.Text color="error">{`  ${output || 'not sent'}`}</ui.Text> : null}
      </ui.Box>
    )
  })
}
