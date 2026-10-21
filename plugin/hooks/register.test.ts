import { expect, mock, test } from 'claude-code/testing'

// A daemon in memory: what the plugin posts, and the peers it answers.
function fakeDaemon(on: any) {
  const posted: { path: string; body: any }[] = []
  const peers = [
    { id: 'p1', name: 'billing-api', host: 'Mac', cwd: '/Users/x/r/billing', branch: 'main', summary: 'Owns the invoice schema', busy: false, muted: false },
  ]
  on('http.fetch', (_: any, e: any) => {
    const path = new URL(e.url).pathname
    const body = e.init?.body ? JSON.parse(e.init.body) : undefined
    posted.push({ path, body })
    const ok = (data: unknown) => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(data) } })
    if (path === '/api/health') return ok({ ok: true, version: '0.2.0' })
    if (path === '/api/sessions') return ok({ id: body.id, name: 'me', config: { poll_interval_ms: 60000 } })
    if (path === '/api/peers') return ok({ peers })
    return ok({ name: 'me', config: { poll_interval_ms: 60000 }, inbox: [] })
  })
  return { posted, peers }
}

function engine(on: any) {
  mock.clock(on)
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'me-session' }))
  on('session.cwd', () => ({ value: '/Users/x/r/web' }))
  on('session.repo', () => ({ value: null }))
  on('session.model', () => ({ value: 'opus' }))
  on('session.turns', () => ({ value: 1 }))
  on('tool.register', (_: any, e: any) => ({ value: { tool: `mcp__peerlink__${e.name}` } }))
  on('command.register', () => ({ value: { command: 'peerlink' } }))
  on('prompt.submit', (_: any, e: any) => ({ text: e.text, context: e.context }))
}

test('the model is told about peers beside the prompt, once per change', async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on)
  await $.session.start({ cwd: '/Users/x/r/web', surface: 'terminal', isInteractive: true })

  const first: any = await $.prompt.submit({ text: 'add an invoice page' })
  expect(first.context?.join('\n')).toContain('billing-api: ~/r/billing [main] — Owns the invoice schema')

  const second: any = await $.prompt.submit({ text: 'now style it' })
  expect(second.context).toBeUndefined()

  daemon.peers[0].summary = 'Migrating invoices to v2'
  const third: any = await $.prompt.submit({ text: 'next' })
  expect(third.context?.join('\n')).toContain('Migrating invoices to v2')
})

test('a finished turn refreshes the summary peers see', async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on)
  on('session.messages', () => ({ value: [{ role: 'user', text: 'build the invoice page in web', toolUses: [] }] }))
  on('model.complete', () => ({
    value: { isAnswered: true, text: 'Building the invoice page in the web app', usage: {} },
  }))
  on('turn.complete', (_: any, e: any) => ({ text: e.text }))
  await $.session.start({ cwd: '/Users/x/r/web', surface: 'terminal', isInteractive: true })

  await $.turn.complete({ turnId: 't1', answer: 'done', text: 'done', durationMs: 1, isAborted: false, reason: 'end_turn' } as any)
  await new Promise(r => setTimeout(r, 0))

  const summaries = daemon.posted.filter(p => p.body?.summary).map(p => p.body.summary)
  expect(summaries).toContain('Building the invoice page in the web app')
})
