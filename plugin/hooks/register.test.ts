import { expect, mock, test } from 'claude-code/testing'

const START = { cwd: '/Users/x/r/web', surface: 'terminal', isInteractive: true } as const
const TURN = { turnId: 't', answer: 'done', text: 'done', durationMs: 1, isAborted: false, reason: 'end_turn' } as any

// Lets the plugin's unawaited work (polls, summaries) run to its end.
async function settle() {
  for (let i = 0; i < 30; i++) await new Promise(r => setTimeout(r, 0))
}

// A daemon in memory: what the plugin posts, and the peers it answers.
function fakeDaemon(on: any, peerCount = 1) {
  const posted: { path: string; body: any }[] = []
  const peers = [
    { id: 'p1', name: 'billing-api', host: 'Mac', cwd: '/Users/x/r/billing', branch: 'main', summary: 'Owns the invoice schema', busy: false, muted: false },
  ]
  on('http.fetch', (_: any, e: any) => {
    const path = new URL(e.url).pathname
    const body = e.init?.body ? JSON.parse(e.init.body) : undefined
    posted.push({ path, body })
    const ok = (data: unknown) => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(data) } })
    if (path === '/api/health') return ok({ ok: true, version: '0.3.0' })
    if (path === '/api/sessions') return ok({ id: body.id, name: body.name, config: { poll_interval_ms: 600000 } })
    if (path === '/api/peers') return ok({ peers })
    return ok({ name: 'web', config: { poll_interval_ms: 600000 }, inbox: [], peer_count: peerCount })
  })
  const summaries = () => posted.filter(p => p.body?.summary).map(p => p.body.summary as string)
  return { posted, peers, summaries }
}

function engine(on: any, { turns = 0, readme = '' } = {}) {
  const clock = mock.clock(on, { now: 1_000_000 })
  const calls = { model: 0 }
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'me-session' }))
  on('session.cwd', () => ({ value: '/Users/x/r/web' }))
  on('session.repo', () => ({ value: { root: '/Users/x/r/web', remote: null, internal: false, name: null } }))
  on('session.model', () => ({ value: 'opus' }))
  on('session.turns', () => ({ value: turns }))
  on('session.messages', () => ({ value: [{ role: 'user', text: 'build the invoice page', toolUses: [] }] }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: 'main\n', stderr: '' } }))
  on('fs.read', (_: any, e: any) => {
    if (readme && e.path.endsWith('/README.md')) return { value: readme }
    throw new Error('ENOENT')
  })
  on('tool.register', (_: any, e: any) => ({ value: { tool: `mcp__peerlink__${e.name}` } }))
  on('command.register', () => ({ value: { command: 'peerlink' } }))
  on('prompt.submit', (_: any, e: any) => ({ text: e.text, context: e.context }))
  on('turn.complete', (_: any, e: any) => ({ text: e.text }))
  const answer = (text: string) =>
    on('model.complete', () => {
      calls.model++
      return { value: { isAnswered: true, text, usage: {} } }
    })
  return { clock, calls, answer }
}

test('the model is told about peers beside the prompt, once per change', async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on)
  await $.session.start(START)

  const first: any = await $.prompt.submit({ text: 'add an invoice page' })
  expect(first.context?.join('\n')).toContain('billing-api: ~/r/billing [main] — Owns the invoice schema')

  const second: any = await $.prompt.submit({ text: 'now style it' })
  expect(second.context).toBeUndefined()

  daemon.peers[0].summary = 'Migrating invoices to v2'
  const third: any = await $.prompt.submit({ text: 'next' })
  expect(third.context?.join('\n')).toContain('Migrating invoices to v2')
})

test('a new session describes itself from its README, with no model call', async ($, on) => {
  const { calls, answer } = engine(on, {
    readme: '# Web\n\n[![ci](x)](y)\n\nThe customer-facing web app: checkout, invoices and account pages.\n',
  })
  answer('should not be asked')
  const daemon = fakeDaemon(on)
  await $.session.start(START)
  await settle()

  expect(daemon.summaries()).toContain('web: The customer-facing web app: checkout, invoices and account pages.')
  expect(calls.model).toBe(0)
})

test('a resumed session summarizes its history as it starts', async ($, on) => {
  const { answer } = engine(on, { turns: 4 })
  answer('Building the invoice page in the web app')
  const daemon = fakeDaemon(on)
  await $.session.start(START)
  await settle()

  expect(daemon.summaries()).toContain('Building the invoice page in the web app')
})

test('with no peers to read it, no summary is paid for', async ($, on) => {
  const { calls, answer } = engine(on, { turns: 4 })
  answer('anything')
  fakeDaemon(on, 0)
  await $.session.start(START)
  for (let i = 0; i < 5; i++) await $.turn.complete(TURN)
  await settle()

  expect(calls.model).toBe(0)
})

test('summaries refresh every few turns, and an unchanged one is not resent', async ($, on) => {
  const { calls, answer, clock } = engine(on, { turns: 4 })
  answer('UNCHANGED')
  const daemon = fakeDaemon(on)
  await $.session.start(START)
  await settle()
  expect(calls.model).toBe(1)

  await $.turn.complete(TURN)
  await $.turn.complete(TURN)
  await settle()
  expect(calls.model).toBe(1)

  await clock.advance(3 * 60_000)
  await $.turn.complete(TURN)
  await settle()
  expect(calls.model).toBe(2)
  expect(daemon.summaries()).toEqual([])
})
