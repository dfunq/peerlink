import type { EngineInterface, Register } from 'claude-code'

const DAEMON = 'http://127.0.0.1:7899'
// The daemon this plugin ships; an older one running is replaced.
const DAEMON_VERSION = '0.2.0'

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
  'You write the one-line status other Claude Code sessions see for this session. In at most 200 characters say what it is working on, in which repo or service, and what it could answer questions about. Plain text, no preamble, no quotes.'

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

async function ensureDaemon($: EngineInterface) {
  try {
    const health = await call($, 'GET', '/api/health')
    if (health.ok && !isOlder(String(health.data.version ?? '0'), DAEMON_VERSION)) return true
    if (health.ok) {
      await call($, 'POST', '/api/shutdown', {})
      await $.clock.sleep(500)
    }
  } catch {}
  const run = await $.process.run(['python3', `${$.plugin.root}/daemon/peerlinkd.py`, '--detach'], {
    timeoutMs: 15000,
  })
  return run.exitCode === 0
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
    name: `${base}-${sessionId.slice(0, 4)}`,
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
  const ask = m.expects_reply
    ? `They expect a reply: answer with ${'mcp__peerlink__send_message'} (to: "${m.from}", reply_to: "${m.id}").`
    : `No reply is required unless you need something from them (to: "${m.from}", reply_to: "${m.id}").`
  return `<peer-message id="${m.id}" from="${m.from_name}" hops="${m.hops}">\n${m.text}\n</peer-message>\n${ask}`
}

async function deliver($: EngineInterface) {
  if (pending.length === 0 || !cfg) return
  const batch = pending
  if (busy) {
    if (!cfg.inject_while_busy) return
    pending = []
    const text = `Message(s) from peer Claude sessions arrived while you were working:\n\n${batch.map(frame).join('\n\n')}`
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
    text: `You received message(s) from peer Claude sessions. Handle them per the peerlink guidance, then reply if needed.\n\n${batch.map(frame).join('\n\n')}`,
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
  pending.push(...(res.data.inbox as PeerMessage[]))
  if (cfg && before !== cfg.poll_interval_ms) startPolling($)
  await deliver($)
}

function peerLine(p: any) {
  const where = p.cwd.replace(/^\/Users\/[^/]+/, '~') + (p.branch ? ` [${p.branch}]` : '')
  return `- ${p.name}${p.remote ? '@' + p.host : ''}: ${where} — ${(p.summary || 'no summary yet').slice(0, 160)}`
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
    ? `Live peer sessions (peerlink). Message one with mcp__peerlink__send_message when it saves real work:\n${roster}`
    : 'peerlink: no other peer sessions are live now.'
}

async function summarize($: EngineInterface) {
  const now = await $.clock.now()
  if (!cfg || cfg.auto_summary === false || summarizing || !sessionId) return
  if (now - manualSummaryAt < 10 * 60_000 || now - lastSummaryAt < 60_000) return
  summarizing = true
  try {
    const messages = (await $.session.messages()) as any[]
    const transcript = messages
      .slice(-12)
      .map(m => {
        const tools = m.toolUses?.length ? ` [tools: ${m.toolUses.map((t: any) => t.tool).join(', ')}]` : ''
        return `${m.role}: ${String(m.text).slice(0, 1500)}${tools}`
      })
      .join('\n\n')
      .slice(-12000)
    if (!transcript.trim()) return
    const done = await $.model.complete({
      model: cfg.summary_model || 'haiku',
      system: SUMMARY_SYSTEM,
      prompt: `Directory: ${await $.session.cwd()}\n\nRecent conversation:\n${transcript}`,
      maxTokens: 120,
      effort: 'low',
      timeoutMs: 20000,
    })
    if (done.isAnswered && done.text.trim()) {
      lastSummaryAt = now
      await call($, 'POST', `/api/sessions/${sessionId}/heartbeat`, { summary: done.text.trim().slice(0, 300) })
    }
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
        'List the other live Claude Code sessions (peers) for this user: name, working directory, repo, branch, what each is working on, and whether it is busy. Use it to find who owns code or knowledge you need before guessing or duplicating work.',
      inputSchema: { type: 'object', properties: {} },
      isDeferred: false,
    })
    await $.tool.register({
      name: 'send_message',
      description:
        'Send a message to a peer Claude session by name or id (from list_peers, or the from address on a peer message). It is delivered into their session, waking it if idle. Use for well-scoped requests, hand-offs, or heads-ups that save real work. Set reply_to when answering a peer message.',
      inputSchema: {
        type: 'object',
        properties: {
          to: { type: 'string', description: 'Peer name or id' },
          text: { type: 'string', description: 'Self-contained message: what you need, why, and what a good answer looks like' },
          expects_reply: { type: 'boolean', description: 'True if you need an answer back' },
          reply_to: { type: 'string', description: 'The id of the peer message you are answering, if any' },
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
      void deliver($)
      void summarize($)
    }
    return done
  })

  on('prompt.submit', async ($, e, next) => {
    // Seed a summary from the person's first prompt so peers see something useful.
    if (sessionId && !e.isMeta && !e.command && (await $.session.turns()) === 0 && e.text.trim()) {
      void call($, 'POST', `/api/sessions/${sessionId}/heartbeat`, {
        busy: true,
        summary: `Started on: ${e.text.trim().slice(0, 160)}`,
      }).catch(() => {})
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
          text: `# Peer sessions\nYou are "${myName}" (id ${sessionId}).\n${cfg.guidance}`,
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
    const summary = String((e as any).summary ?? '').slice(0, 500)
    manualSummaryAt = await $.clock.now()
    const res = await call($, 'POST', `/api/sessions/${sessionId}/heartbeat`, { busy, summary })
    return text(res.ok ? 'Summary updated.' : `Daemon error: ${res.status}`)
  }).catch(daemonDown)

  on('command.run', { command: 'peerlink' }, async $ => {
    const up = await ensureDaemon($)
    if (up) await $.process.run(['open', DAEMON + '/'])
    return { text: up ? `peerlink daemon at ${DAEMON} — you are "${myName}".` : 'peerlink daemon is not running (needs python3).' }
  })
}
