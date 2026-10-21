import type { EngineInterface, Register } from 'claude-code'

const DAEMON = 'http://127.0.0.1:7899'
// The daemon this plugin ships; an older one running is replaced.
const DAEMON_VERSION = '0.3.0'

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
  poll_interval_ms: number
  auto_wake_idle: boolean
  inject_while_busy: boolean
  guidance: string
  muted: boolean
  roster_in_prompt: boolean
  auto_summary: boolean
  summary_model: string
}

const SUMMARY_SYSTEM =
  'You keep the one-line status other Claude Code sessions see for this session. In at most 200 characters say what it is working on, in which repo or service, and what it could answer questions about. Plain text, no preamble, no quotes. If the current status is still accurate, reply exactly UNCHANGED.'

// Summaries cost a model call, and every change is resent to each peer: refresh only this often.
const SUMMARY_EVERY_TURNS = 3
const SUMMARY_MIN_GAP_MS = 2 * 60_000
const MANUAL_SUMMARY_HOLD_MS = 10 * 60_000

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

async function call($: EngineInterface, method: string, path: string, body?: unknown) {
  const res = await $.http.fetch(DAEMON + path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
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
// another session may restart its own daemon in the gap, so try a few times.
async function ensureDaemon($: EngineInterface) {
  for (let attempt = 0; attempt < 4; attempt++) {
    const version = await daemonVersion($)
    if (version !== undefined && !isOlder(version, DAEMON_VERSION)) return true
    if (version !== undefined) {
      await call($, 'POST', '/api/shutdown', {}).catch(() => {})
      await $.clock.sleep(300)
    }
    await $.process.run(['python3', `${$.plugin.root}/daemon/peerlinkd.py`, '--detach'], { timeoutMs: 15000 })
  }
  return (await daemonVersion($)) !== undefined
}

async function currentBranch($: EngineInterface, cwd: string) {
  const run = await $.process.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], { cwd, timeoutMs: 5000 })
  return run.exitCode === 0 ? run.stdout.trim() : ''
}

async function announce($: EngineInterface) {
  const cwd = await $.session.cwd()
  const repo = await $.session.repo()
  const base = cwd.split('/').filter(Boolean).at(-1) ?? 'session'
  const res = await call($, 'POST', '/api/sessions', {
    id: sessionId,
    name: base,
    cwd,
    repo: repo?.remote ?? repo?.root ?? '',
    branch: repo ? await currentBranch($, cwd) : '',
    model: await $.session.model(),
    busy,
  })
  if (res.ok) {
    myName = res.data.name
    cfg = res.data.config
  }
  return res.ok
}

function frame(m: PeerMessage) {
  const from = m.from.includes('@') ? m.from : m.from_name
  const attrs = [`id="${m.id}"`, `from="${from}"`, m.reply_to ? `re="${m.reply_to}"` : '', m.expects_reply ? 'reply="expected"' : '']
  return `<peer-message ${attrs.filter(Boolean).join(' ')}>\n${m.text}\n</peer-message>`
}

async function deliver($: EngineInterface) {
  if (pending.length === 0 || !cfg) return
  const batch = pending
  if (busy) {
    if (!cfg.inject_while_busy) return
    pending = []
    const text = `Peer message (reply with send_message, reply_to its id):\n${batch.map(frame).join('\n')}`
    const res = await $.session.append({ message: { type: 'user', content: [{ type: 'text', text }] } })
    if ('deny' in res && res.deny) pending = [...batch, ...pending]
    return
  }
  if (!cfg.auto_wake_idle) {
    $.ui.status(`peerlink: ${batch.length} message(s) waiting; ask me to check my inbox`)
    return
  }
  pending = []
  busy = true
  $.ui.toast(`Message from ${batch.map(m => m.from_name).join(', ')}`)
  await $.prompt.submit({
    text: `Peer message (reply with send_message, reply_to its id):\n${batch.map(frame).join('\n')}`,
  })
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
  await deliver($)
}

function peerLine(p: any) {
  const where = p.cwd.replace(/^\/Users\/[^/]+/, '~') + (p.branch ? ` [${p.branch}]` : '')
  return `- ${p.name}${p.remote ? '@' + p.host : ''}: ${where} — ${(p.summary || 'no summary yet').slice(0, 140)}`
}

// The live peers, for the model to read beside the prompt; only when the list changed since it last saw it.
async function rosterContext($: EngineInterface) {
  if (!cfg || cfg.roster_in_prompt === false || cfg.muted) return undefined
  const res = await call($, 'GET', `/api/peers?exclude=${encodeURIComponent(sessionId)}`)
  if (!res.ok) return undefined
  const peers = (res.data.peers as any[]).filter(p => !p.muted).slice(0, 12)
  const roster = peers.map(peerLine).join('\n')
  if (roster === lastRoster) return undefined
  lastRoster = roster
  return peers.length
    ? `Peers now live (send_message to reach one):\n${roster}`
    : 'peerlink: no other peer sessions are live now.'
}

async function postSummary($: EngineInterface, next: string) {
  summary = next.slice(0, 300)
  await call($, 'POST', `/api/sessions/${sessionId}/heartbeat`, { summary })
}

// A first summary with no model call: the project's own description, from its CLAUDE.md or README.
async function seedSummary($: EngineInterface, cwd: string) {
  const root = (await $.session.repo())?.root ?? cwd
  const name = root.split('/').filter(Boolean).at(-1) ?? 'project'
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
    if (line) return `${name}: ${line.replace(/[*_`]/g, '').slice(0, 160)}`
  }
  return `${name}${cwd !== root ? ` (${cwd.slice(root.length + 1)})` : ''}`
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
    if (next && next !== 'UNCHANGED' && next !== summary) await postSummary($, next)
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

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    sessionId = await $.session.id()
    await $.tool.register({
      name: 'list_peers',
      description:
        "List live peer sessions with their folder, branch, current work and whether they're busy. Peers already appear beside the prompt when they change, so call this only for ids or busy state.",
      inputSchema: { type: 'object', properties: {} },
      isDeferred: false,
    })
    await $.tool.register({
      name: 'send_message',
      description:
        'Message a peer session by name, like messaging a teammate. It arrives in their session at once, waking it if idle. Keep it short, point to file paths rather than pasting, and set reply_to when answering.',
      inputSchema: {
        type: 'object',
        properties: {
          to: { type: 'string', description: 'Peer name or id' },
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
        'Set the one-line summary peers see for this session (what you are working on and what you can help with). Update it when your focus changes.',
      inputSchema: {
        type: 'object',
        properties: { summary: { type: 'string' } },
        required: ['summary'],
      },
      isDeferred: false,
    })
    await $.command.register({ name: 'peerlink', description: 'Open the peerlink web UI and show daemon status' })
    if (await ensureDaemon($)) {
      await announce($)
      startPolling($)
      if ((await $.session.turns()) > 0) {
        void poll($).then(() => summarize($))
      } else {
        void seedSummary($, e.cwd).then(seed => postSummary($, seed)).catch(() => {})
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
    // Until a real summary exists, the first prompt says what this session is for.
    if (sessionId && !e.isMeta && !e.command && !hasSummarizedTalk && (await $.session.turns()) === 0 && e.text.trim()) {
      const task = e.text.trim().replace(/\s+/g, ' ').slice(0, 110)
      void postSummary($, `${summary.slice(0, 120)}${summary ? ' — ' : ''}now: ${task}`).catch(() => {})
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
          text: `# Peer sessions\nPeers know you as "${myName}".\n${cfg.guidance}`,
        },
      ],
    }
  })

  on('tool.call', { tool: 'mcp__peerlink__list_peers' }, async $ => {
    const res = await call($, 'GET', `/api/peers?exclude=${encodeURIComponent(sessionId)}`)
    if (!res.ok) return text(`Daemon error: ${res.data.error ?? res.status}`)
    const peers = res.data.peers as any[]
    if (peers.length === 0) return text('No other live sessions right now.')
    return text(
      peers
        .map(
          p =>
            `- ${p.name}${p.remote ? '@' + p.host : ''} (id ${p.id}) — ${p.busy ? 'busy' : 'idle'}${p.muted ? ', muted' : ''}\n  dir: ${p.cwd}${p.branch ? ` [${p.branch}]` : ''}\n  summary: ${p.summary || '(none)'}`,
        )
        .join('\n'),
    )
  }).catch(daemonDown)

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
    await postSummary($, String((e as any).summary ?? ''))
    return text('Summary updated.')
  }).catch(daemonDown)

  on('command.run', { command: 'peerlink' }, async $ => {
    const up = await ensureDaemon($)
    if (up) await $.process.run(['open', DAEMON + '/'])
    return { text: up ? `peerlink daemon at ${DAEMON} — you are "${myName}".` : 'peerlink daemon is not running (needs python3).' }
  })
}
