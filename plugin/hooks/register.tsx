import type { EngineInterface, Register } from 'claude-code'

// Incoming peer messages read the same for the model and the person: one header line, then the text.
const MESSAGE_MARK = '↘ '
const REPLY_HINT = '(Reply with send_message, reply_to the id, only if a reply is needed.)'

const DAEMON = 'http://127.0.0.1:7899'
// The daemon this plugin ships; an older one running is replaced.
const DAEMON_VERSION = '0.5.0'
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
  /peerlink web            open the web UI (one-time sign-in link)
  /peerlink status         daemon, this agent, peers
  /peerlink peers          list live agents
  /peerlink name <name>    rename this agent
  /peerlink summary <text> set what peers see for this agent
  /peerlink wake on|off|default   whether a message starts a turn here when idle
  /peerlink mute <agent> | unmute <agent>
  /peerlink space                 this agent's space and where it comes from
  /peerlink space <name> | clear  override the space for this agent only
  /peerlink space file <name> | clear   set the space in this project's .peerlink
  /peerlink restart        restart the daemon
  /peerlink rotate-key     new key; signs out every browser
In a shell: ~/.peerlink/bin/peerlink help`

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

async function daemonVersion($: EngineInterface) {
  try {
    const health = await call($, 'GET', '/api/health')
    return health.ok ? String(health.data.version ?? '0') : undefined
  } catch {
    return undefined
  }
}

// Start the daemon, or replace an older one. An older plugin still running in
// another agent may restart its own daemon in the gap, so try a few times.
async function ensureDaemon($: EngineInterface) {
  for (let attempt = 0; attempt < 4; attempt++) {
    const version = await daemonVersion($)
    if (version !== undefined && !isOlder(version, DAEMON_VERSION)) return true
    if (version !== undefined) {
      await call($, 'POST', '/api/shutdown', {}).catch(() => {})
      await $.clock.sleep(300)
    }
    await $.process.run(['python3', `${$.plugin.root}/daemon/peerlinkd.py`, 'start'], { timeoutMs: 15000 })
    key = ''
  }
  return (await daemonVersion($)) !== undefined
}

async function git($: EngineInterface, cwd: string, args: string[]) {
  const run = await $.process.run(['git', ...args], { cwd, timeoutMs: 5000 })
  return run.exitCode === 0 ? run.stdout.trim() : ''
}

async function announce($: EngineInterface) {
  const cwd = await $.session.cwd()
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
  $.ui.status(myName ? `${myName} · ${peerCount} peer${peerCount === 1 ? '' : 's'}${waiting}` : undefined)
}

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
    return
  }
  if (cfg.auto_wake_idle === false) {
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
  return `- ${p.name}${p.remote ? '@' + p.host : ''}: ${where} — ${(p.summary || 'no summary yet').slice(0, 140)}`
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
      .find(l => l.length > 30 && !/^(#|<|!\[|\[!|```|\||-{3})/.test(l))
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
        `- ${p.name}${p.remote ? '@' + p.host : ''}${withIds ? ` (id ${p.id})` : ''} — ${p.busy ? 'busy' : 'idle'}${p.muted ? ', muted' : ''}\n  dir: ${p.cwd}${p.branch ? ` [${p.branch}]` : ''}\n  summary: ${p.summary || '(none)'}`,
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
  return `space: ${cfg.space} (${from})`
}

async function setAgentConfig($: EngineInterface, id: string, patch: Record<string, unknown>) {
  const current = await call($, 'GET', '/api/config')
  const sessions = { ...(current.data.sessions ?? {}) }
  sessions[id] = { ...(sessions[id] ?? {}), ...patch }
  return call($, 'PUT', '/api/config', { sessions })
}

async function findAgent($: EngineInterface, name: string) {
  const res = await call($, 'GET', '/api/peers?local=1')
  const want = name.toLowerCase()
  const hits = ((res.data.peers ?? []) as any[]).filter(p => p.name.toLowerCase() === want || p.id === name)
  return hits.length === 1 ? hits[0] : undefined
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
    case 'peers':
      return listPeers($, false)
    case 'name': {
      if (!arg) return 'Usage: /peerlink name <name>'
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
      if (value === undefined) return 'Usage: /peerlink wake on|off|default'
      await setAgentConfig($, sessionId, { auto_wake: value })
      await poll($)
      return `Wake on message: ${cfg?.auto_wake_idle === false ? 'off' : 'on'}${value === null ? ' (the default)' : ''}.`
    }
    case 'mute':
    case 'unmute': {
      const agent = arg && (await findAgent($, arg))
      if (!agent) return `No single live agent named "${arg}". /peerlink peers lists them.`
      await setAgentConfig($, agent.id, { muted: sub === 'mute' })
      return `${agent.name} is ${sub === 'mute' ? 'muted' : 'unmuted'}.`
    }
    case 'space': {
      const [what = '', name = ''] = rest
      if (!what) return `This agent's ${spaceLine()}.`
      if (what === 'file') {
        if (!name) return 'Usage: /peerlink space file <name> | clear'
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
          to: { type: 'string', description: 'Peer agent name or id' },
          text: { type: 'string', description: 'The message: what you need and why, briefly' },
          expects_reply: { type: 'boolean', description: 'True if you need an answer' },
          reply_to: { type: 'string', description: 'Id of the peer message you are answering' },
        },
        required: ['to', 'text'],
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
    await $.command.register({
      name: 'peerlink',
      description: 'peerlink: web UI, status, peers, name, space, wake, mute, restart, rotate-key',
      argumentHint: 'web | status | peers | name <name> | space [name] | wake on|off | mute <agent> | help',
    })
    if (await ensureDaemon($)) {
      await announce($)
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
          text: `# Peer agents\nPeers know you as "${myName}"${cfg.space && cfg.space !== 'default' ? ` in the "${cfg.space}" space; you only see agents in that space` : ''}.\n${cfg.guidance}`,
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

  on('tool.call', { tool: 'mcp__peerlink__set_summary' }, async ($, e) => {
    manualSummaryAt = await $.clock.now()
    await postSummary($, String((e as any).summary ?? ''), 'manual')
    return text('Summary updated.')
  }).catch(daemonDown)

  on('command.run', { command: 'peerlink' }, async ($, e) => ({ text: await runCommand($, e.args ?? '') }))

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
