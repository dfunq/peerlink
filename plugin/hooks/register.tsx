import type { EngineInterface, Register } from 'claude-code'

// Incoming peer messages read the same for the model and the person: one header line, then the text.
const MESSAGE_MARK = '↘ '
const REPLY_HINT = '(Reply with send_message, reply_to the id, only if a reply is needed.)'

const DAEMON = 'http://127.0.0.1:7899'
// The daemon this plugin ships; an older one running is replaced.
const DAEMON_VERSION = '0.6.4'
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
}

type ClientConfig = {
  space: string
  space_source: 'override' | 'file' | 'rule' | 'default'
  space_from: string
  // Set when this agent leads its space: the space it also sits in and speaks for its own.
  parent?: string
  // The agent that leads this agent's space, and the leads of spaces nested inside it.
  lead?: string
  children?: { name: string; space: string }[]
  poll_interval_ms: number
  auto_wake_idle: boolean
  inject_while_busy: boolean
  guidance: string
  muted: boolean
  roster_in_prompt: boolean
  auto_summary: boolean
  summary_model: string
}

type Previous = { summary: string; name: string; ts: number } | null

const SUMMARY_SYSTEM =
  'You keep the one-line status other Claude agents see for this agent. In at most 200 characters say what it is working on, in which repo or service, and what it could answer questions about. Plain text, no preamble, no quotes. If the current status is still accurate, reply exactly UNCHANGED.'

// Summaries cost a model call, and every change is resent to each peer: refresh only this often.
const SUMMARY_EVERY_TURNS = 3
const SUMMARY_MIN_GAP_MS = 2 * 60_000
const MANUAL_SUMMARY_HOLD_MS = 10 * 60_000

const HELP = `peerlink commands:
  /peerlink [status]       daemon, this agent, its summary
  /peerlink web            open the web UI (one-time sign-in link)
  /peerlink issues         the last problems agents reported with peerlink
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
let myName = ''
let busy = false
let cfg: ClientConfig | undefined
let pending: PeerMessage[] = []
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

async function announce($: EngineInterface) {
  // The project root, not the shell's directory: an agent that cds into a subfolder is still the same agent.
  const cwd = await $.session.root()
  const repo = await $.session.repo()
  const res = await call($, 'POST', '/api/sessions', {
    id: sessionId,
    name: cwd.split('/').filter(Boolean).at(-1) ?? 'agent',
    cwd,
    repo: repo?.remote ?? repo?.root ?? '',
    branch: repo ? await git($, cwd, ['rev-parse', '--abbrev-ref', 'HEAD']) : '',
    model: await $.session.model(),
    busy,
    summary: summary || undefined,
  })
  if (res.ok) {
    myName = res.data.name
    cfg = res.data.config
    previous = res.data.previous ?? null
  }
  return res.ok
}

type Frame = { id: string; from: string; re?: string; reply: boolean; body: string }

// Reads back what frame() wrote, wherever it sits in the row's text.
function parseFrames(text: string): Frame[] {
  const frames: Frame[] = []
  const pattern = /^↘ (\S+) · (m\d+)((?: · [^\n]*)?)\n([\s\S]*?)(?=\n\n↘ |\n\n\(Reply with send_message|$)/gm
  for (const match of text.matchAll(pattern)) {
    const tags = match[3]
    frames.push({ from: match[1], id: match[2], re: tags.match(/re (m\d+)/)?.[1], reply: tags.includes('reply expected'), body: match[4] })
  }
  return frames
}

// One row per message: who it's from and the text, the way a chat shows it.
function MessageRow(ui: any, arrow: '↘' | '↗', who: string, tags: string[], body: string, key: string) {
  const { Box, Text } = ui
  return (
    <Box key={key} flexDirection="column">
      <Box flexDirection="row">
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
  return [f.id, f.re ? `re ${f.re}` : '', f.reply ? 'reply expected' : ''].filter(Boolean)
}

function frame(m: PeerMessage) {
  const from = m.from.includes('@') ? m.from : m.from_name
  const tags = [m.id, m.reply_to ? `re ${m.reply_to}` : '', m.expects_reply ? 'reply expected' : ''].filter(Boolean)
  return `${MESSAGE_MARK}${from} · ${tags.join(' · ')}\n${m.text}`
}

function framed(batch: PeerMessage[]) {
  return `${batch.map(frame).join('\n\n')}\n\n${REPLY_HINT}`
}

function showStatus($: EngineInterface) {
  const waiting = pending.length ? ` · ${pending.length} waiting` : ''
  $.ui.status(myName ? `${myName} · ${peerCount} peer${peerCount === 1 ? '' : 's'}${waiting}${daemonVer ? ` · peerlink ${daemonVer}` : ''}` : undefined)
}

const shownWaiting = new Set<string>()

async function deliver($: EngineInterface) {
  if (pending.length === 0 || !cfg) return
  const batch = pending
  if (busy) {
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
  pending = []
  busy = true
  $.ui.toast(`Message from ${batch.map(m => m.from_name).join(', ')}`)
  await $.prompt.submit({ text: framed(batch) })
}

async function poll($: EngineInterface) {
  if (!sessionId) return
  let res
  try {
    res = await call($, 'POST', `/api/sessions/${sessionId}/heartbeat`, { busy })
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
  pending.push(...(res.data.inbox as PeerMessage[]))
  if (cfg && before !== cfg.poll_interval_ms) startPolling($)
  showStatus($)
  await deliver($)
}

function peerLine(p: any) {
  const where = p.cwd.replace(/^\/Users\/[^/]+/, '~') + (p.branch ? ` [${p.branch}]` : '')
  return `- ${p.name}${p.remote ? '@' + p.host : ''}${spaceTag(p)}: ${where} — ${(p.summary || 'no summary yet').slice(0, 140)}`
}

// The live agents, for the model to read beside the prompt; only when the list changed since it last saw it.
async function rosterContext($: EngineInterface) {
  if (!cfg || cfg.roster_in_prompt === false || cfg.muted) return undefined
  const res = await call($, 'GET', `/api/peers?exclude=${encodeURIComponent(sessionId)}`)
  if (!res.ok) return undefined
  const peers = (res.data.peers as any[]).filter(p => !p.muted).slice(0, 12)
  const roster = peers.map(peerLine).join('\n')
  if (roster === lastRoster) return undefined
  lastRoster = roster
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
        `- ${p.name}${p.remote ? '@' + p.host : ''}${withIds ? ` (id ${p.id})` : ''}${spaceTag(p)} — ${p.busy ? 'busy' : 'idle'}${p.muted ? ', muted' : ''}\n  dir: ${p.cwd}${p.branch ? ` [${p.branch}]` : ''}\n  summary: ${p.summary || '(none)'}`,
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
  const nest = cfg.parent ? `, leads it inside "${cfg.parent}"` : cfg.lead ? `, led by ${cfg.lead}` : ''
  return `space: ${cfg.space} (${from}${nest})`
}

// Where a peer sits relative to this agent: a lead speaks for its space; a peer in another space is the lead's parent.
function spaceTag(p: any) {
  if (p.parent) return ` [leads ${p.space}]`
  return cfg && p.space && p.space !== cfg.space ? ` [${p.space}]` : ''
}

// What a place in a nested space asks of this agent; the daemon enforces who can reach whom.
function hierarchyGuidance() {
  if (!cfg) return ''
  const lines: string[] = []
  if (cfg.parent)
    lines.push(
      `You lead the "${cfg.space}" space and speak for it in the "${cfg.parent}" space. Agents in "${cfg.parent}" can only reach "${cfg.space}" through you. When one asks for something, decide which agent in "${cfg.space}" owns it, delegate with send_message (or do it yourself if it is yours), and when the work is done report the outcome back to whoever asked, with reply_to their message id.`,
    )
  else if (cfg.lead)
    lines.push(`${cfg.lead} leads the "${cfg.space}" space and relays work from outside it. Report results to ${cfg.lead}, and ask it for anything that lives outside your space.`)
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
    argumentHint: '[status | web | issues | restart | rotate-key | help]',
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
    const own = (groups[space] ?? []).filter(p => !isLead(p)).sort((a, b) => a.name.localeCompare(b.name))
    const leads = (kids[space] ?? []).filter(c => c !== space).sort().flatMap(c => (groups[c] ?? []).filter(p => p.parent && p.space === c))
    const all = [...own, ...leads]
    const width = Math.max(0, ...all.map(p => p.name.length))
    for (const p of all) {
      const tag = p.parent && p.space !== space ? `  leads ${p.space}` : ''
      lines.push(`${pad}${p.name.padEnd(width)}  ${p.cwd.replace(/^\/Users\/[^/]+/, '~')}${tag}${p.id === sessionId ? '  ← you' : ''}`)
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
async function reportIssue($: EngineInterface, what: string, details: string) {
  if (!what.trim()) return 'Not reported: say what went wrong.'
  const context = `agent ${myName}, ${spaceLine()}, cwd ${await $.session.cwd()}`
  const body = { from: sessionId, from_name: myName, text: details ? `${what}\n\n${details}` : what, context, plugin: DAEMON_VERSION, space: cfg?.space ?? '' }
  if (await ensureDaemon($).catch(() => false)) {
    const res = await call($, 'POST', '/api/issues', body).catch(() => undefined)
    if (res?.ok) return `Reported as ${res.data.id}${res.data.sent_to ? ` and sent to ${res.data.sent_to}` : ''}. Logged in ${res.data.logged}.`
  }
  const now = await $.clock.now()
  const path = `${await peerlinkHome($)}/issues-pending/${now}-${sessionId.slice(0, 8)}.json`
  await $.fs.write(path, JSON.stringify({ ...body, ts: now / 1000 }))
  return `The daemon is down, so the report was saved to ${path}; it is filed when the daemon next starts.`
}

async function runCommand($: EngineInterface, args: string): Promise<string> {
  const [sub = 'help', ...rest] = args.trim().split(/\s+/).filter(Boolean)
  const arg = rest.join(' ')
  if (sub === 'help') return HELP
  if (!(await ensureDaemon($))) return 'The peerlink daemon is not running and could not start (it needs python3).'
  switch (sub) {
    case 'web': {
      const res = await call($, 'POST', '/api/web/link', {})
      if (!res.ok) return `Could not make a sign-in link: ${res.data.error ?? res.status}`
      await $.process.run(['open', res.data.url])
      return `Opened the web UI with a one-time sign-in link (valid ${res.data.expires_in_s}s).`
    }
    case 'status': {
      const st = (await call($, 'GET', '/api/status')).data
      return `daemon ${st.version} (pid ${st.pid}) · ${st.sessions} live agents · ${st.queued} queued messages · ${st.browsers} signed-in browsers\nthis agent: ${myName} · ${spaceLine()} · wake on message: ${cfg?.auto_wake_idle === false ? 'off' : 'on'}${cfg?.muted ? ' · muted' : ''}\nsummary: ${summary || '(none yet)'}`
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
          to: { type: 'string', description: 'Peer agent name or id; leave it out when replying, the reply goes back to the sender' },
          text: { type: 'string', description: 'The message: what you need and why, briefly' },
          expects_reply: { type: 'boolean', description: 'True if you need an answer' },
          reply_to: { type: 'string', description: 'Id of the peer message you are answering; the reply goes to its sender' },
        },
        required: ['text'],
      },
      isDeferred: false,
    })
    await $.tool.register({
      name: 'report_issue',
      description:
        "Report a problem with peerlink itself: a message that never arrived or reached the wrong agent, a peer you expected that isn't listed, an error from these tools. It's logged for the people fixing peerlink and sent to the agent working on peerlink when one is live. Not for problems with your own task or a peer's work.",
      inputSchema: {
        type: 'object',
        properties: {
          what: { type: 'string', description: 'What went wrong and what you expected, briefly' },
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
    cancelPoll?.()
    if (sessionId) {
      try {
        await call($, 'DELETE', `/api/sessions/${sessionId}`)
      } catch {}
    }
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) busy = true
    return next(e)
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

  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    if (!cfg || cfg.muted) return composed
    return {
      sections: [
        ...composed.sections,
        {
          id: 'peerlink:guidance',
          scope: 'session',
          text: `# Peer agents\nPeers know you as "${myName}"${cfg.space && cfg.space !== 'default' ? ` in the "${cfg.space}" space; you only see agents in that space${cfg.parent ? ` and "${cfg.parent}"` : ''}` : ''}.${hierarchyGuidance()}\n${cfg.guidance}`,
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
    })
    if (!res.ok) return text(`Not sent: ${res.data.error ?? res.status}`)
    return text(
      `Sent to ${res.data.to} (${res.data.id}).${input.expects_reply ? ' Their reply will arrive as a peer message; continue with other work meanwhile.' : ''}`,
    )
  }).catch(daemonDown)

  on('tool.call', { tool: 'mcp__peerlink__report_issue' }, async ($, e) => text(await reportIssue($, String((e as any).what ?? ''), String((e as any).details ?? ''))))

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
