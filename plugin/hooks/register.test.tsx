import { expect, mock, test } from 'claude-code/testing'

const START = { cwd: '/Users/x/r/web', surface: 'terminal', isInteractive: true } as const
const COMPOSE = { model: 'opus', promptModel: 'opus', surfaces: ['terminal'], tools: [], outputStyle: null, traits: [] } as any
const TURN = { turnId: 't', answer: 'done', text: 'done', durationMs: 1, isAborted: false, reason: 'end_turn' } as any

// Lets the plugin's unawaited work (polls, summaries) run to its end.
async function settle() {
  for (let i = 0; i < 30; i++) await new Promise(r => setTimeout(r, 0))
}

// A daemon in memory: what the plugin posts, and the peers it answers.
function fakeDaemon(on: any, peerCount = 1, previous: unknown = null, inbox: unknown[] = [], config: Record<string, unknown> = {}, code = '') {
  const posted: { path: string; body: any }[] = []
  const state = { down: false }
  const peers = [
    { id: 'p1', name: 'billing-api', host: 'Mac', cwd: '/Users/x/r/billing', branch: 'main', summary: 'Owns the invoice schema', busy: false, muted: false },
  ]
  on('http.fetch', (_: any, e: any) => {
    if (state.down) throw new Error('ECONNREFUSED')
    const path = new URL(e.url).pathname
    const body = e.init?.body ? JSON.parse(e.init.body) : undefined
    posted.push({ path, body })
    const ok = (data: unknown) => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(data) } })
    if (path === '/api/health') return ok({ ok: true, version: '0.6.2', code: posted.some(p => p.path === '/api/shutdown') ? 'new' : code })
    if (path === '/api/sessions') return ok({ id: body.id, name: body.name, config: { poll_interval_ms: 600000, ...config }, previous })
    if (path === '/api/peers') return ok({ peers })
    if (path === '/api/issues') return ok({ id: 'i1', logged: '/Users/x/.peerlink/issues.jsonl', sent_to: 'peerlink' })
    return ok({ name: 'web', config: { poll_interval_ms: 600000, ...config }, inbox: inbox.splice(0), peer_count: peerCount })
  })
  const summaries = () => posted.filter(p => p.body?.summary).map(p => p.body.summary as string)
  return { posted, peers, summaries, state }
}

function engine(on: any, { turns = 0, readme = '', commits = '', fingerprint = '', transcripts = [] as { name: string; title: string; ageMs: number }[] } = {}) {
  const clock = mock.clock(on, { now: 1_000_000 })
  const calls = { model: 0 }
  const writes: { path: string; text: string }[] = []
  on('fs.write', (_: any, e: any) => {
    writes.push({ path: e.path, text: e.text })
    return { value: undefined }
  })
  mock.env(on, { HOME: '/Users/x' })
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'me-session' }))
  on('session.cwd', () => ({ value: '/Users/x/r/web' }))
  on('session.repo', () => ({ value: { root: '/Users/x/r/web', remote: null, internal: false, name: null } }))
  on('session.model', () => ({ value: 'opus' }))
  on('session.turns', () => ({ value: turns }))
  on('session.messages', () => ({ value: [{ role: 'user', text: 'build the invoice page', toolUses: [] }] }))
  on('ui.status', () => ({ value: undefined }))
  on('process.run', (_: any, e: any) => {
    const [cmd, ...args] = e.argv
    let stdout = ''
    if (cmd === 'git' && args[0] === 'rev-parse') stdout = 'main\n'
    if (cmd === 'git' && args[0] === 'log') stdout = commits
    if (cmd === 'python3' && args[1] === 'fingerprint') stdout = fingerprint
    if (cmd === 'grep') {
      const t = transcripts.find(t => args.at(-1).endsWith(t.name))
      if (t) stdout = JSON.stringify({ type: 'ai-title', aiTitle: t.title }) + '\n'
    }
    return { value: { exitCode: 0, stdout, stderr: '' } }
  })
  on('fs.read', (_: any, e: any) => {
    if (readme && e.path.endsWith('/README.md')) return { value: readme }
    throw new Error('ENOENT')
  })
  on('fs.list', (_: any, e: any) => {
    if (!transcripts.length || !e.path.endsWith('/.claude/projects/-Users-x-r-web')) throw new Error('ENOENT')
    return { value: transcripts.map(t => ({ name: t.name, kind: 'file', size: 1, mtimeMs: 1_000_000 - t.ageMs, isLink: false })) }
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
  return { clock, calls, answer, writes }
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

test("a new agent picks up what happened in its folder: Claude Code's session title and recent commits", async ($, on) => {
  const { calls } = engine(on, {
    commits: 'Add invoice PDF export\nFix tax rounding\n',
    transcripts: [
      { name: 'older.jsonl', title: 'Old work', ageMs: 5000 },
      { name: 'newer.jsonl', title: 'Invoice PDF export', ageMs: 1000 },
    ],
  })
  const daemon = fakeDaemon(on)
  await $.session.start(START)
  await settle()

  expect(daemon.summaries()).toContain('web · last session here: Invoice PDF export · recent commits: Add invoice PDF export; Fix tax rounding')
  expect(calls.model).toBe(0)
})

test('the last agent in the same folder is preferred when it is the newest news', async ($, on) => {
  engine(on, { transcripts: [{ name: 'a.jsonl', title: 'Older title', ageMs: 60_000 }] })
  const daemon = fakeDaemon(on, 1, { summary: 'Wiring Stripe webhooks into the invoice service', name: 'web', ts: 1_000 })
  await $.session.start(START)
  await settle()

  expect(daemon.summaries()).toContain('web · last agent here: Wiring Stripe webhooks into the invoice service')
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

const PROMPT =
  'The peerlink plugin sent a message:\n↘ api · m7 · re m6 · reply expected\nWhich file defines the invoice model?\n\n(Reply with send_message, reply_to the id, only if a reply is needed.)\nThis is how Claude Code surfaces a prompt a plugin submits between turns.'

test('an incoming peer message draws as a chat row, on every surface', async ($, on) => {
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e) as any
    return <Text>engine</Text>
  })
  for (const surface of ['terminal', 'desktop'] as const) {
    const base = { plugin: 'peerlink', surface, component: 'UserMessage', requestId: 'u1' } as const
    const ui = await $.ui.mount({ ...base, props: { text: PROMPT, origin: { kind: 'plugin', name: 'peerlink' }, isExpanded: false } } as any)
    expect((await ui.find({ type: 'Text', text: /↘ api/ }))?.text).toBe('↘ api')
    expect((await ui.find({ type: 'Text', text: /m7 · re m6 · reply expected/ }))).toBeDefined()
    expect((await ui.find({ type: 'Text', text: /Which file defines the invoice model\?/ }))).toBeDefined()
    await ui.unmount()

    const expanded = await $.ui.mount({ ...base, props: { text: PROMPT, origin: { kind: 'plugin', name: 'peerlink' }, isExpanded: true } } as any)
    expect((await expanded.find({ type: 'Text', text: /engine/ }))).toBeDefined()
    await expanded.unmount()
  }
})

test('a sent message draws as who it went to and what it said', async ($, on) => {
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({
      plugin: 'peerlink', surface, component: 'ToolUse', requestId: 't1',
      props: {
        tool: 'mcp__peerlink__send_message',
        input: { to: 'api', text: 'Is the API on v2 yet?', expects_reply: true },
        output: 'Sent to api (m4). Their reply will arrive as a peer message; continue with other work meanwhile.',
        isRunning: false, isErrored: false, isInterrupted: false, isExpanded: false,
      },
    } as any)
    expect((await ui.find({ type: 'Text', text: /↗ api/ }))?.text).toBe('↗ api')
    expect((await ui.find({ type: 'Text', text: /m4 · reply expected/ }))).toBeDefined()
    await ui.unmount()

    const failed = await $.ui.mount({
      plugin: 'peerlink', surface, component: 'ToolUse', requestId: 't2',
      props: {
        tool: 'mcp__peerlink__send_message', input: { to: 'nobody', text: 'hi' },
        output: "Not sent: no live peer 'nobody'; call list_peers",
        isRunning: false, isErrored: false, isInterrupted: false, isExpanded: false,
      },
    } as any)
    expect((await failed.find({ type: 'Text', text: /Not sent/ }))).toBeDefined()
    await failed.unmount()
  }
})

test('a message that arrives mid-turn reaches the model and is shown to the person', async ($, on) => {
  const { clock } = engine(on, { turns: 1 })
  const session = mock.session(on)
  const logged: string[] = []
  on('ui.log', (_: any, e: any) => {
    logged.push(e.text)
    return { value: undefined }
  })
  on('turn.start', (_: any, e: any) => ({ turnId: e.turnId }))
  on('model.complete', () => ({ value: { isAnswered: true, text: 'UNCHANGED', usage: {} } }))
  const inbox: any[] = []
  const dbg = fakeDaemon(on, 1, null, inbox)
  await $.session.start(START)
  await settle()

  await $.turn.start({ text: 'refactor billing', turnId: 't9' } as any)
  inbox.push({ id: 'm5', from: 'api-id', from_name: 'api', from_host: 'Mac', text: 'Heads up: /invoices now paginates.', expects_reply: false, reply_to: null, hops: 0 })
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()

  const rows = session.appended().map((r: any) => ({ type: r.message.type, text: r.message.content[0].text }))
  expect(rows.find(r => r.type === 'user')?.text).toContain('↘ api · m5\nHeads up: /invoices now paginates.')
  expect(logged).toContain('↘ api · m5: Heads up: /invoices now paginates.')
})

test('a delivered message hides its redundant result line, a failed one keeps it', async ($, on) => {
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e) as any
    return <Text>engine</Text>
  })
  for (const surface of ['terminal', 'desktop'] as const) {
    const base = { plugin: 'peerlink', surface, component: 'ToolResult', requestId: 'r1' } as const
    const sent = await $.ui.mount({ ...base, props: { tool: 'mcp__peerlink__send_message', output: 'Sent to api (m4).', isErrored: false } } as any)
    expect(await sent.find({ type: 'Text', text: /engine|Sent/ })).toBeUndefined()
    await sent.unmount()
    const failed = await $.ui.mount({ ...base, props: { tool: 'mcp__peerlink__send_message', output: 'Not sent: muted', isErrored: false } } as any)
    expect(await failed.find({ type: 'Text', text: /engine/ })).toBeDefined()
    await failed.unmount()
  }
})

test('an agent in a space is told so in its system prompt', async ($, on) => {
  engine(on)
  fakeDaemon(on, 1, null, [], { space: 'acme', space_source: 'file', space_from: '/Users/x/r/.peerlink', guidance: 'Be brief.' })
  on('prompt.compose', () => ({ sections: [] }))
  await $.session.start(START)
  const composed: any = await $.prompt.compose(COMPOSE)
  const text = composed.sections.find((s: any) => s.id === 'peerlink:guidance').text
  expect(text).toContain('Peers know you as "web" in the "acme" space')
})

test('the default space adds nothing to the system prompt', async ($, on) => {
  engine(on)
  fakeDaemon(on, 1, null, [], { space: 'default', space_source: 'default', space_from: '', guidance: 'Be brief.' })
  on('prompt.compose', () => ({ sections: [] }))
  await $.session.start(START)
  const composed: any = await $.prompt.compose(COMPOSE)
  const text = composed.sections.find((s: any) => s.id === 'peerlink:guidance').text
  expect(text).toBe('# Peer agents\nPeers know you as "web".\nBe brief.')
})

test('a lead is told to delegate down and report back up', async ($, on) => {
  engine(on)
  fakeDaemon(on, 1, null, [], { space: 'storefront', space_source: 'file', space_from: '/Users/x/r/web/.peerlink', parent: 'acme', guidance: 'Be brief.' })
  on('prompt.compose', () => ({ sections: [] }))
  await $.session.start(START)
  const composed: any = await $.prompt.compose(COMPOSE)
  const text = composed.sections.find((s: any) => s.id === 'peerlink:guidance').text
  expect(text).toContain('in the "storefront" space; you only see agents in that space and "acme"')
  expect(text).toContain('You lead the "storefront" space and speak for it in the "acme" space')
})

test('a parent space sees its child leads, tagged, and is told to hand them outcomes', async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on, 1, null, [], {
    space: 'acme', space_source: 'file', space_from: '/Users/x/r/.peerlink',
    children: [{ name: 'billing-api', space: 'billing' }], guidance: 'Be brief.',
  })
  Object.assign(daemon.peers[0], { space: 'billing', parent: 'acme' })
  on('prompt.compose', () => ({ sections: [] }))
  await $.session.start(START)
  const first: any = await $.prompt.submit({ text: 'ship invoices' })
  expect(first.context?.join('\n')).toContain('billing-api [leads billing]: ~/r/billing')
  const composed: any = await $.prompt.compose(COMPOSE)
  const text = composed.sections.find((s: any) => s.id === 'peerlink:guidance').text
  expect(text).toContain('billing-api (leads "billing")')
})

test('/peerlink-spaces draws an org chart: a lead in its parent, its team beneath', async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on, 1, null, [], { space: 'billing', space_source: 'file', space_from: '/Users/x/r/web/.peerlink', parent: 'acme' })
  daemon.peers.splice(0, 1,
    { id: 'p0', name: 'acme', host: 'Mac', cwd: '/Users/x/r', space: 'acme', parent: '' } as any,
    { id: 'me-session', name: 'web', host: 'Mac', cwd: '/Users/x/r/web', space: 'billing', parent: 'acme' } as any,
    { id: 'p1', name: 'api', host: 'Mac', cwd: '/Users/x/r/web/api', space: 'billing', parent: '' } as any,
  )
  await $.session.start(START)
  const { text } = (await $.command.run({ command: 'peerlink-spaces' })) as any
  expect(text).toContain('acme\n  acme  ~/r\n  web       ~/r/web  leads billing  ← you\n    └ billing team\n        api  ~/r/web/api')
})

test('topic commands reject what belongs to another topic', async ($, on) => {
  engine(on)
  fakeDaemon(on)
  await $.session.start(START)
  const { text } = (await $.command.run({ command: 'peerlink-peers', args: 'rename x' } as any)) as any
  expect(text).toContain('Usage: /peerlink-peers')
})

test('a load replaces a daemon of the same version running other code', async ($, on) => {
  const { clock } = engine(on, { fingerprint: 'new' })
  const daemon = fakeDaemon(on, 1, null, [], {}, 'old')
  const started = $.session.start(START)
  await settle()
  await clock.advance(1000)
  await started
  expect(daemon.posted.filter(p => p.path === '/api/shutdown').length).toBe(1)
})

test('a load leaves a daemon running the same code alone', async ($, on) => {
  engine(on, { fingerprint: 'same' })
  const daemon = fakeDaemon(on, 1, null, [], {}, 'same')
  await $.session.start(START)
  expect(daemon.posted.some(p => p.path === '/api/shutdown')).toBe(false)
})

test('report_issue logs the problem and says who it went to', async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on)
  await $.session.start(START)
  const res: any = await $.tool.call({ tool: 'mcp__peerlink__report_issue', what: 'reply to m19 never arrived', details: 'sent m19 to bridge' } as any)
  expect(JSON.stringify(res)).toContain('Reported as i1 and sent to peerlink')
  const sent = daemon.posted.find(p => p.path === '/api/issues')!.body
  expect(sent.text).toBe('reply to m19 never arrived\n\nsent m19 to bridge')
  expect(sent.context).toContain('agent web')
})

test('with the daemon down, a report waits in a file for it', async ($, on) => {
  const { writes } = engine(on)
  const daemon = fakeDaemon(on)
  await $.session.start(START)
  daemon.state.down = true
  const res: any = await $.tool.call({ tool: 'mcp__peerlink__report_issue', what: 'daemon keeps dying' } as any)
  expect(JSON.stringify(res)).toContain('issues-pending')
  expect(writes[0]!.path).toContain('/.peerlink/issues-pending/')
  expect(JSON.parse(writes[0]!.text).text).toBe('daemon keeps dying')
})

test('with wake off, a message still shows once, and says it waits for the next prompt', async ($, on) => {
  const { clock } = engine(on)
  const logged: string[] = []
  const toasts: string[] = []
  on('ui.log', (_: any, e: any) => { logged.push(e.text); return { value: undefined } })
  on('ui.toast', (_: any, e: any) => { toasts.push(e.text ?? e.message ?? JSON.stringify(e)); return { value: undefined } })
  const inbox: any[] = []
  fakeDaemon(on, 1, null, inbox, { auto_wake_idle: false })
  await $.session.start(START)
  await settle()
  inbox.push({ id: 'm7', from: 'api-id', from_name: 'api', from_host: 'Mac', text: 'Schema changed.', expects_reply: false, reply_to: null, hops: 0 })
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  expect(logged.filter(l => l.includes('m7'))).toEqual(['↘ api · m7: Schema changed.  (wake is off: read with your next prompt)'])
  expect(toasts.some(t => t.includes('api'))).toBe(true)
})

test('a session says which plugin and daemon versions it runs', async ($, on) => {
  engine(on)
  const logged: string[] = []
  on('ui.log', (_: any, e: any) => { logged.push(e.text); return { value: undefined } })
  fakeDaemon(on)
  await $.session.start(START)
  expect(logged.some(l => l.startsWith('peerlink 0.6.2 · daemon 0.6.2 · web'))).toBe(true)
})
