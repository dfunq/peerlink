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
  const state = { down: false, forget: false, threads: { owes: [] as any[], waiting: [] as any[] }, once: {} as Record<string, unknown> }
  const peers = [
    { id: 'p1', name: 'billing-api', host: 'Mac', cwd: '/Users/x/r/billing', branch: 'main', summary: 'Owns the invoice schema', busy: false, muted: false },
  ]
  on('http.fetch', (_: any, e: any) => {
    if (state.down) throw new Error('ECONNREFUSED')
    const path = new URL(e.url).pathname
    const body = e.init?.body ? JSON.parse(e.init.body) : undefined
    posted.push({ path, body })
    const ok = (data: unknown) => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(data) } })
    if (path === '/api/health') return ok({ ok: true, version: '0.8.47', code: posted.some(p => p.path === '/api/shutdown') ? 'new' : code })
    if (state.forget && path.endsWith('/heartbeat') && 'busy' in body) {
      state.forget = false
      return { value: { status: 404, ok: false, headers: {}, text: JSON.stringify({ error: 'unknown agent; register again' }) } }
    }
    if (path === '/api/sessions') return ok({ id: body.id, name: body.name, config: { poll_interval_ms: 600000, ...config }, previous })
    if (path === '/api/peers') return ok({ peers })
    if (path.endsWith('/threads')) return ok(state.threads)
    if (path === '/api/web/link') return ok({ url: 'http://127.0.0.1:7899/login?t=x', expires_in_s: 60 })
    if (path === '/api/issues') return ok({ id: 'i1', logged: '/Users/x/.peerlink/issues.jsonl', sent_to: 'peerlink' })
    if (path.startsWith('/api/tasks/')) return ok({ task: { id: path.split('/').pop(), status: body.status, requester: { name: 'acme' } } })
    if (path === '/api/messages' && body?.kind === 'task') return ok({ id: 'm9', to: body.to, status: 'queued', task: 't3' })
    if (path === '/api/schedules' && body)
      return body.cron === 'bad' ? { value: { status: 400, ok: false, headers: {}, text: JSON.stringify({ error: 'a cron has five fields' }) } }
        : ok({ schedule: { id: 'c1', status: body.start === false ? 'active' : 'pending', when_text: body.cron ? `cron ${body.cron}` : 'once, Sun Oct 25 09:00', next_run: 1_800_000 } })
    const once = state.once
    state.once = {}
    return ok({ name: 'web', config: { poll_interval_ms: 600000, ...config }, inbox: inbox.splice(0), peer_count: peerCount, ...once })
  })
  const summaries = () => posted.filter(p => p.body?.summary).map(p => p.body.summary as string)
  return { posted, peers, summaries, state }
}

function engine(on: any, { turns = 0, percent = 85, readme = '', claudeMd = '', installed = '', commits = '', fingerprint = '', transcripts = [] as { name: string; title: string; ageMs: number }[] } = {}) {
  const clock = mock.clock(on, { now: 1_000_000 })
  const calls = { model: 0 }
  const ids = { current: 'me-session' }
  const writes: { path: string; text: string }[] = []
  on('fs.write', (_: any, e: any) => {
    writes.push({ path: e.path, text: e.text })
    return { value: undefined }
  })
  mock.env(on, { HOME: '/Users/x' })
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: ids.current }))
  on('session.cwd', () => ({ value: '/Users/x/r/web' }))
  const where = { root: '/Users/x/r/web' }
  on('session.root', () => ({ value: where.root }))
  const store: Record<string, unknown> = {}
  on('store.get', (_: any, e: any) => ({ value: store[e.key ?? e] }))
  on('store.set', (_: any, e: any) => {
    store[e.key] = e.value
    return { value: undefined }
  })
  on('session.repo', () => ({ value: { root: '/Users/x/r/web', remote: null, internal: false, name: null } }))
  on('session.model', () => ({ value: 'opus' }))
  // The account's rate-limit windows, as the last API response reported them.
  const limits: { kind: string; percentUsed: number; resetsAt?: string }[] = []
  on('session.usage', () => ({ value: { context: { window: 200000, tokens: percent * 2000, percent }, rateLimits: limits } }))
  on('session.turns', () => ({ value: turns }))
  on('session.messages', () => ({ value: [{ role: 'user', text: 'build the invoice page', toolUses: [] }] }))
  // What git says about the agent's folder: dirs are git-dir, common dir and top level; null outside a repo.
  const git = { dirs: ['/Users/x/r/web/.git', '/Users/x/r/web/.git', '/Users/x/r/web'] as string[] | null, head: 'main', sha: '1a2b3c4' }
  const pinned: unknown[] = []
  on('ui.status', (_: any, e: any) => {
    pinned.push(e)
    return { value: undefined }
  })
  on('process.run', (_: any, e: any) => {
    const [cmd, ...args] = e.argv
    let stdout = ''
    let exitCode = 0
    if (cmd === 'git') {
      if (!git.dirs) exitCode = 128
      else if (args.includes('--git-dir')) stdout = git.dirs.join('\n') + '\n'
      else if (args[0] === 'symbolic-ref') git.head ? (stdout = git.head + '\n') : (exitCode = 1)
      else if (args[0] === 'rev-parse') stdout = git.sha + '\n'
    }
    if (cmd === 'git' && args[0] === 'log') stdout = commits
    if (cmd === 'python3' && args[1] === 'fingerprint') stdout = fingerprint
    if (cmd === 'grep') {
      const t = transcripts.find(t => args.at(-1).endsWith(t.name))
      if (t) stdout = JSON.stringify({ type: 'ai-title', aiTitle: t.title }) + '\n'
    }
    return { value: { exitCode, stdout, stderr: '' } }
  })
  on('fs.read', (_: any, e: any) => {
    if (readme && e.path.endsWith('/README.md')) return { value: readme }
    if (claudeMd && e.path.endsWith('/CLAUDE.md')) return { value: claudeMd }
    if (installed && e.path.endsWith('/.claude/plugins/installed_plugins.json'))
      return { value: JSON.stringify({ version: 2, plugins: { 'peerlink@peerlink': [{ version: installed }] } }) }
    throw new Error('ENOENT')
  })
  on('fs.list', (_: any, e: any) => {
    if (!transcripts.length || !e.path.endsWith('/.claude/projects/-Users-x-r-web')) throw new Error('ENOENT')
    return { value: transcripts.map(t => ({ name: t.name, kind: 'file', size: 1, mtimeMs: 1_000_000 - t.ageMs, isLink: false })) }
  })
  on('tool.register', (_: any, e: any) => ({ value: { tool: `mcp__peerlink__${e.name}` } }))
  on('command.register', () => ({ value: { command: 'peerlink' } }))
  // What the plugin submitted as a prompt of its own: a peer message waking this agent.
  const woke: string[] = []
  // Set drop to have the session refuse the plugin's prompts, as a hook that drops them does.
  const submits = { drop: '' }
  on('prompt.submit', (_: any, e: any) => {
    if (submits.drop) return { drop: submits.drop }
    if (e.text.includes('(Reply with send_message')) woke.push(e.text)
    return { text: e.text, context: e.context }
  })
  on('turn.complete', (_: any, e: any) => ({ text: e.text }))
  const answer = (text: string) =>
    on('model.complete', () => {
      calls.model++
      return { value: { isAnswered: true, text, usage: {} } }
    })
  return { clock, calls, answer, writes, ids, pinned, git, where, woke, submits, limits }
}

test('the model is told about peers beside the prompt, again only when who is there changes', async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on)
  await $.session.start(START)

  const first: any = await $.prompt.submit({ text: 'add an invoice page' })
  expect(first.context?.join('\n')).toContain('billing-api: ~/r/billing [main] — Owns the invoice schema')

  const second: any = await $.prompt.submit({ text: 'now style it' })
  expect(second.context).toBeUndefined()

  // A new summary alone isn't worth resending the list for; list_peers has it.
  daemon.peers[0].summary = 'Migrating invoices to v2'
  const third: any = await $.prompt.submit({ text: 'next' })
  expect(third.context).toBeUndefined()

  daemon.peers.push({ id: 'p2', name: 'search', host: 'Mac', cwd: '/Users/x/r/search', branch: 'main', summary: 'Indexing', busy: false, muted: false })
  const fourth: any = await $.prompt.submit({ text: 'and now' })
  expect(fourth.context?.join('\n')).toContain('search: ~/r/search [main] — Indexing')
  expect(fourth.context?.join('\n')).toContain('Migrating invoices to v2')
})

test("when an org plugin skips peerlink's compose and context hooks, the role, guidance and announcements ride on the prompt", async ($, on) => {
  const { clock } = engine(on)
  const inbox: any[] = []
  fakeDaemon(on, 1, null, inbox, { space: 'acme', space_source: 'set', space_from: '', role: 'Owns the web app.', guidance: 'Be brief.' })
  await $.session.start(START)
  await settle()
  const first: any = await $.prompt.submit({ text: 'add an invoice page' })
  const said = first.context.join('\n')
  expect(said).toContain('Peers know you as "web" in the "acme" space')
  expect(said).toContain('Your role here, as your user set it (other agents see it too): Owns the web app.')
  expect(said).toContain('Be brief.')
  // Unchanged, it isn't sent again.
  const second: any = await $.prompt.submit({ text: 'now style it' })
  expect(second.context).toBeUndefined()
  // An announcement, which prompt.context would carry, comes with the next prompt.
  inbox.push({ id: 'm8', kind: 'announcement', from: 'p1', from_name: 'billing-api', from_host: 'Mac', text: 'Deploy freeze until 5pm', expects_reply: false, reply_to: null, hops: 0 })
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  const third: any = await $.prompt.submit({ text: 'next' })
  expect(third.context.join('\n')).toContain('Deploy freeze until 5pm')
})

test('past the context size the user set, the agent is asked once to save and clear, on its next prompt', async ($, on) => {
  engine(on, { turns: 4, percent: 85 })
  on('prompt.compose', () => ({ sections: [] }))
  on('prompt.context', (_: any, e: any) => ({ blocks: e.blocks }))
  fakeDaemon(on, 1, null, [], { clear_nudge_tokens: 100000 })
  await $.session.start(START)
  await settle()
  await $.turn.complete(TURN)
  for (let i = 0; i < 5; i++) await settle()
  const next: any = await $.prompt.submit({ text: 'carry on' })
  expect(next.context.join('\n')).toContain('Your context is at 170k tokens, past the 100k your user set')
  await $.turn.complete(TURN)
  for (let i = 0; i < 5; i++) await settle()
  const after: any = await $.prompt.submit({ text: 'and again' })
  expect((after.context ?? []).join('\n')).not.toContain('past the 100k')
})

test('below the context size, or with it off, nobody is asked to clear', async ($, on) => {
  engine(on, { turns: 4, percent: 85 })
  on('prompt.compose', () => ({ sections: [] }))
  on('prompt.context', (_: any, e: any) => ({ blocks: e.blocks }))
  fakeDaemon(on, 1, null, [], { clear_nudge_tokens: 0 })
  await $.session.start(START)
  await settle()
  await $.turn.complete(TURN)
  for (let i = 0; i < 5; i++) await settle()
  const next: any = await $.prompt.submit({ text: 'carry on' })
  expect((next.context ?? []).join('\n')).not.toContain('clear_context')
})

test('when prompt.compose runs, the guidance is not sent on the prompt as well', async ($, on) => {
  engine(on)
  on('prompt.compose', () => ({ sections: [] }))
  fakeDaemon(on, 1, null, [], { space: 'acme', space_source: 'set', space_from: '', role: 'Owns the web app.' })
  await $.session.start(START)
  await settle()
  await $.prompt.compose(COMPOSE)
  const first: any = await $.prompt.submit({ text: 'add an invoice page' })
  expect(first.context?.join('\n') ?? '').not.toContain('Owns the web app.')
})

test('schedule hands the daemon a prompt for later, and says when it waits for approval', async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on)
  await $.session.start(START)
  await settle()
  const run = async (input: any) => JSON.stringify(await $.tool.call({ tool: 'mcp__peerlink__schedule', ...input } as any))
  expect(await run({ prompt: 'run the nightly report', cron: '0 2 * * *' })).toContain("waits for your user's approval")
  expect(await run({ prompt: 'check CI', at: 'in 2h', start: false })).toContain('Scheduled c1 (once, Sun Oct 25 09:00)')
  expect(await run({ prompt: 'x', cron: 'bad' })).toContain('Not scheduled: a cron has five fields')
  const sent = daemon.posted.filter(p => p.path === '/api/schedules').map(p => p.body)
  expect(sent[0]).toMatchObject({ by: 'me-session', prompt: 'run the nightly report', cron: '0 2 * * *' })
  expect(sent[1]).toMatchObject({ at: 'in 2h', start: false })
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

test('summaries refresh every 8 turns and 10 minutes, and an unchanged one is not resent', async ($, on) => {
  const { calls, answer, clock } = engine(on, { turns: 4 })
  answer('UNCHANGED')
  const daemon = fakeDaemon(on)
  await $.session.start(START)
  await settle()
  expect(calls.model).toBe(1)

  for (let i = 0; i < 7; i++) await $.turn.complete(TURN)
  await clock.advance(11 * 60_000)
  await settle()
  expect(calls.model).toBe(1)

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
    expect((await ui.find({ type: 'Text', text: /^peerlink $/ }))).toBeDefined()
    await ui.unmount()

    const fromDiscord = PROMPT.replace('↘ api', '↘ dillon on Discord')
    const discord = await $.ui.mount({ ...base, props: { text: fromDiscord, origin: { kind: 'plugin', name: 'peerlink' }, isExpanded: false } } as any)
    expect((await discord.find({ type: 'Text', text: /↘ dillon/ }))?.text).toBe('↘ dillon on Discord')
    expect((await discord.find({ type: 'Text', text: /The peerlink plugin sent a message/ }))).toBeUndefined()
    await discord.unmount()

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

test('a message that arrives mid-turn and is left unanswered is put to the agent again, once', async ($, on) => {
  const { clock, woke } = engine(on)
  mock.session(on)
  on('turn.start', (_: any, e: any) => ({ turnId: e.turnId }))
  const inbox: any[] = []
  const daemon = fakeDaemon(on, 1, null, inbox, { wake_delay_s: 0 })
  await $.session.start(START)
  await settle()
  const ask = { id: 'm26', from: 'discord:123', from_name: 'dillon on Discord', from_host: 'discord', text: 'Ask the API agent for the env files', expects_reply: true, reply_to: null, hops: 0 }
  const fyi = { id: 'm27', from: 'api-id', from_name: 'api', from_host: 'Mac', text: 'FYI: deployed', expects_reply: false, reply_to: null, hops: 0 }
  await $.turn.start({ text: 'start the dev servers', turnId: 't1' } as any)
  inbox.push(ask, fyi)
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  expect(woke).toEqual([])
  // The turn ends with m26 still owed: it comes back as a turn of its own. The FYI wanted no reply, so it doesn't.
  daemon.state.threads = { owes: [{ id: 'm26', with: 'dillon on Discord', ago_s: 30, text: 'Ask the API agent' }], waiting: [] }
  await $.turn.complete(TURN)
  for (let i = 0; i < 10; i++) await settle()
  expect(woke.length).toBe(1)
  expect(woke[0]).toContain('m26 · reply expected · unanswered from your last turn')
  expect(woke[0]).not.toContain('m27')
  // Read past again: it isn't chased a second time.
  await $.turn.complete(TURN)
  for (let i = 0; i < 10; i++) await settle()
  expect(woke.length).toBe(1)
})

test('a message answered in the turn it arrived in is not put again', async ($, on) => {
  const { clock, woke } = engine(on)
  mock.session(on)
  on('turn.start', (_: any, e: any) => ({ turnId: e.turnId }))
  const inbox: any[] = []
  fakeDaemon(on, 1, null, inbox, { wake_delay_s: 0 })
  await $.session.start(START)
  await settle()
  await $.turn.start({ text: 'start the dev servers', turnId: 't1' } as any)
  inbox.push({ id: 'm26', from: 'discord:123', from_name: 'dillon on Discord', from_host: 'discord', text: 'Status?', expects_reply: true, reply_to: null, hops: 0 })
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  await $.turn.complete(TURN)
  for (let i = 0; i < 10; i++) await settle()
  expect(woke).toEqual([])
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

test('a lead of a top-level space is told work sent to the space comes to it, and members see it tagged', async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on, 1, null, [], { space: 'acme', space_source: 'file', space_from: '/Users/x/r/.peerlink', leads: 'only' })
  Object.assign(daemon.peers[0], { space: 'acme', leads: 'set' })
  on('prompt.compose', () => ({ sections: [] }))
  await $.session.start(START)
  const first: any = await $.prompt.submit({ text: 'go' })
  expect(first.context?.join('\n')).toContain('billing-api [leads acme]: ~/r/billing')
  const composed: any = await $.prompt.compose(COMPOSE)
  const text = composed.sections.find((s: any) => s.id === 'peerlink:guidance').text
  expect(text).toContain('You lead the "acme" space. Work sent to the space (to "space:acme") comes to you')
})

test('/peerlink-spaces <name> puts the folder in a space; parent and the old file form go to the same place', async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on, 1, null, [], { space: 'acme', space_source: 'set', space_from: '/Users/x/r/web' })
  await $.session.start(START)
  await settle()
  const sent = () => daemon.posted.filter(p => p.path === '/api/sessions/me-session/space').map(p => p.body)
  const { text } = (await $.command.run({ command: 'peerlink-spaces', args: 'acme' })) as any
  expect(text).toContain('space: acme (set for this folder)')
  await $.command.run({ command: 'peerlink-spaces', args: 'file billing' })
  await $.command.run({ command: 'peerlink-spaces', args: 'parent clear' })
  await $.command.run({ command: 'peerlink-spaces', args: 'clear' })
  expect(sent()).toEqual([{ space: 'acme' }, { space: 'billing' }, { parent: '' }, { space: '' }])
})

test('/peerlink-spaces draws an org chart: a lead in its parent, its space beneath', async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on, 1, null, [], { space: 'billing', space_source: 'file', space_from: '/Users/x/r/web/.peerlink', parent: 'acme' })
  daemon.peers.splice(0, 1,
    { id: 'p0', name: 'acme', host: 'Mac', cwd: '/Users/x/r', space: 'acme', parent: '' } as any,
    { id: 'me-session', name: 'web', host: 'Mac', cwd: '/Users/x/r/web', space: 'billing', parent: 'acme' } as any,
    { id: 'p1', name: 'api', host: 'Mac', cwd: '/Users/x/r/web/api', space: 'billing', parent: '' } as any,
  )
  await $.session.start(START)
  const { text } = (await $.command.run({ command: 'peerlink-spaces' })) as any
  expect(text).toContain('acme\n  acme  ~/r\n  web   ~/r/web  leads billing  ← you\n    └ billing\n        api  ~/r/web/api')
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

test("a message that needs no reply waits for the agent's next turn instead of starting one", async ($, on) => {
  const { clock, woke } = engine(on)
  const logged: string[] = []
  on('ui.log', (_: any, e: any) => { logged.push(e.text); return { value: undefined } })
  const inbox: any[] = []
  fakeDaemon(on, 1, null, inbox)
  await $.session.start(START)
  await settle()
  inbox.push({ id: 'm7', from: 'api-id', from_name: 'api', from_host: 'Mac', text: 'Thanks, merged.', expects_reply: false, reply_to: 'm6', hops: 1, wake: false })
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  expect(woke).toEqual([])
  expect(logged.filter(l => l.includes('m7'))).toEqual(['↘ api · m7 · re m6: Thanks, merged.  (no reply needed: read with the next turn)'])
})

test("another agent's question wakes an idle agent after a short wait, and yours at once", async ($, on) => {
  const { clock, woke } = engine(on)
  const inbox: any[] = []
  fakeDaemon(on, 1, null, inbox, { wake_delay_s: 15 })
  await $.session.start(START)
  await settle()
  inbox.push({ id: 'm8', from: 'api-id', from_name: 'api', from_host: 'Mac', text: 'Which file has the schema?', expects_reply: true, reply_to: null, hops: 0, wake: true })
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  expect(woke).toEqual([])
  await clock.advance(16000)
  for (let i = 0; i < 10; i++) await settle()
  expect(woke.length).toBe(1)
  expect(woke[0]).toContain('Which file has the schema?')
})

test('your own message wakes an idle agent without waiting', async ($, on) => {
  const { clock, woke } = engine(on)
  const inbox: any[] = []
  fakeDaemon(on, 1, null, inbox, { wake_delay_s: 15 })
  await $.session.start(START)
  await settle()
  inbox.push({ id: 'm9', from: 'discord:123', from_name: 'dillon on Discord', from_host: 'discord', text: 'Status?', expects_reply: false, reply_to: null, hops: 0 })
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  expect(woke.length).toBe(1)
})

test('a message whose prompt starts no turn goes into the transcript, is acked, and a redelivery is not shown twice', async ($, on) => {
  const { clock, submits } = engine(on)
  const session = mock.session(on)
  submits.drop = 'not now'
  const inbox: any[] = []
  const daemon = fakeDaemon(on, 1, null, inbox, { wake_delay_s: 0 })
  await $.session.start(START)
  await settle()
  const msg = { id: 'm9', from: 'discord:123', from_name: 'dillon on Discord', from_host: 'discord', text: 'Status?', expects_reply: true, reply_to: null, hops: 0 }
  inbox.push(msg)
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  const rows = session.appended().filter((r: any) => r.message.type === 'user')
  expect(rows.length).toBe(1)
  expect(rows[0].message.content[0].text).toContain('Status?')
  expect(daemon.posted.some(p => p.path.endsWith('/ack') && p.body.ids.includes('m9'))).toBe(true)
  // The daemon hands it out again (its ack crossed in flight): it isn't shown a second time.
  inbox.push(msg)
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  expect(session.appended().filter((r: any) => r.message.type === 'user').length).toBe(1)
})

test('a message that wakes the agent is acked, and every heartbeat carries acks', async ($, on) => {
  const { clock, woke } = engine(on)
  const inbox: any[] = []
  const daemon = fakeDaemon(on, 1, null, inbox, { wake_delay_s: 0 })
  await $.session.start(START)
  await settle()
  inbox.push({ id: 'm7', from: 'discord:123', from_name: 'dillon on Discord', from_host: 'discord', text: 'Ping', expects_reply: true, reply_to: null, hops: 0 })
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  expect(woke.length).toBe(1)
  expect(daemon.posted.some(p => p.path.endsWith('/ack') && p.body.ids.includes('m7'))).toBe(true)
  expect(daemon.posted.filter(p => p.path.endsWith('/heartbeat') && 'busy' in p.body).every(p => Array.isArray(p.body.ack))).toBe(true)
})

test('a clear asked from the web UI runs /clear when the agent is idle', async ($, on) => {
  const { clock } = engine(on)
  const ran: string[] = []
  on('command.run', { command: 'clear' }, () => {
    ran.push('clear')
    return { text: '' }
  })
  const daemon = fakeDaemon(on)
  await $.session.start(START)
  await settle()
  daemon.state.once = { clear: { by: 'the web UI' } }
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  expect(ran).toEqual(['clear'])
})

test('an agent clears its own context between tasks, and its notes start the fresh one', async ($, on) => {
  engine(on, { turns: 4 })
  const ran: string[] = []
  on('command.run', { command: 'clear' }, () => {
    ran.push('clear')
    return { text: '' }
  })
  on('prompt.context', (_: any, e: any) => ({ blocks: e.blocks }))
  fakeDaemon(on)
  await $.session.start(START)
  await settle()
  const empty: any = await $.tool.call({ tool: 'mcp__peerlink__clear_context', notes: ' ' } as any)
  expect(JSON.stringify(empty)).toContain('Not cleared')
  await $.tool.call({ tool: 'mcp__peerlink__clear_context', notes: 'SKY-32 server half on feat/pg-053; PR not opened yet' } as any)
  expect(ran).toEqual([])
  await $.turn.complete(TURN)
  for (let i = 0; i < 5; i++) await settle()
  expect(ran).toEqual(['clear'])
  const out: any = await $.prompt.context({ blocks: [] } as any)
  expect(out.blocks.find((b: any) => b.name === 'peerlink handoff').text).toContain('SKY-32 server half on feat/pg-053')
  const again: any = await $.prompt.context({ blocks: [] } as any)
  expect(again.blocks.some((b: any) => b.name === 'peerlink handoff')).toBe(false)
})

test('a context cleared moments ago is not cleared again', async ($, on) => {
  engine(on, { turns: 1, percent: 4 })
  fakeDaemon(on)
  await $.session.start(START)
  await settle()
  const res: any = await $.tool.call({ tool: 'mcp__peerlink__clear_context', notes: 'nothing in flight' } as any)
  expect(JSON.stringify(res)).toContain('just cleared')
})

test('commands asked for on Discord run one at a time, each when the agent is idle', async ($, on) => {
  const { clock } = engine(on)
  mock.session(on)
  on('turn.start', (_: any, e: any) => ({ turnId: e.turnId }))
  const ran: string[] = []
  on('command.run', { command: 'loop' }, (_: any, e: any) => {
    ran.push(`loop ${e.args}`)
    return { text: '' }
  })
  on('command.run', { command: 'goal' }, (_: any, e: any) => {
    ran.push(`goal ${e.args}`)
    return { text: '' }
  })
  const daemon = fakeDaemon(on)
  await $.session.start(START)
  await settle()
  await $.turn.start({ text: 'working', turnId: 't1' } as any)
  daemon.state.once = { run: [{ command: 'loop', args: '5m check the deploy', by: 'Discord (DLowne)' }, { command: 'goal', args: 'ship the beta', by: 'Discord (DLowne)' }] }
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  // Busy: nothing runs until the turn ends, then the first; the second waits for the turn that one starts.
  expect(ran).toEqual([])
  await $.turn.complete(TURN)
  for (let i = 0; i < 10; i++) await settle()
  expect(ran).toEqual(['loop 5m check the deploy'])
  await $.turn.start({ text: '/loop', turnId: 't2' } as any)
  await $.turn.complete(TURN)
  for (let i = 0; i < 10; i++) await settle()
  expect(ran).toEqual(['loop 5m check the deploy', 'goal ship the beta'])
})

test('a session says which plugin and daemon versions it runs', async ($, on) => {
  engine(on)
  const logged: string[] = []
  on('ui.log', (_: any, e: any) => { logged.push(e.text); return { value: undefined } })
  fakeDaemon(on)
  await $.session.start(START)
  expect(logged.some(l => l.startsWith('peerlink 0.8.47 · daemon 0.8.47 · web'))).toBe(true)
})

test('two sessions in a lead folder both sit with the parent; the space is listed once', async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on)
  daemon.peers.splice(0, 1,
    { id: 'p0', name: 'top', host: 'Mac', cwd: '/Users/x/r', space: 'top', parent: '' } as any,
    { id: 'p1', name: 'pg', host: 'Mac', cwd: '/Users/x/r/pg', space: 'pg', parent: 'top' } as any,
    { id: 'p2', name: 'pg-2', host: 'Mac', cwd: '/Users/x/r/pg', space: 'pg', parent: 'top' } as any,
    { id: 'p3', name: 'client', host: 'Mac', cwd: '/Users/x/r/pg/client', space: 'pg', parent: '' } as any,
  )
  await $.session.start(START)
  const { text } = (await $.command.run({ command: 'peerlink-spaces' })) as any
  expect(text).toContain('top\n  top   ~/r\n  pg    ~/r/pg  leads pg\n  pg-2  ~/r/pg  leads pg\n    └ pg\n        client  ~/r/pg/client')
})

test("a CLAUDE.md's stock first line is not taken as the project's description", async ($, on) => {
  engine(on, { claudeMd: '# CLAUDE.md\n\nThis file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.\n\nA FastAPI service that renders customer sites.' })
  const daemon = fakeDaemon(on, 0)
  await $.session.start(START)
  await settle()
  expect(daemon.summaries().join('\n')).toContain('A FastAPI service that renders customer sites.')
  expect(daemon.summaries().join('\n')).not.toContain('provides guidance')
})

test('an agent is known by its project root, not the folder its shell moved to', async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on)
  await $.session.start(START)
  const reg = daemon.posted.find(p => p.path === '/api/sessions')!.body
  expect(reg.cwd).toBe('/Users/x/r/web')
  expect(reg.name).toBe('web')
  expect(reg.plugin).toBe('0.8.47')
})

test('an agent registers again under the folder it started in, wherever the host has moved its root since', async ($, on) => {
  const { clock, where } = engine(on)
  const daemon = fakeDaemon(on)
  await $.session.start(START)
  where.root = '/Users/x/r'
  daemon.state.forget = true
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  const regs = daemon.posted.filter(p => p.path === '/api/sessions').map(p => [p.body.cwd, p.body.name])
  expect(regs.length).toBe(2)
  expect(regs[1]).toEqual(['/Users/x/r/web', 'web'])
})

test("an agent is given its role from .peerlink, and peers see a peer's role beside what it's doing", async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on, 1, null, [], { space: 'acme', space_source: 'file', space_from: '/Users/x/r/.peerlink', role: 'Owns the web app: pages, styles, the build.' })
  Object.assign(daemon.peers[0], { role: 'Owns the billing API:\ninvoices and payments.' })
  on('prompt.compose', () => ({ sections: [] }))
  await $.session.start(START)
  const first: any = await $.prompt.submit({ text: 'go' })
  expect(first.context?.join('\n')).toContain('billing-api: ~/r/billing [main] — Owns the billing API: invoices and payments. · now: Owns the invoice schema')
  const composed: any = await $.prompt.compose(COMPOSE)
  expect(composed.sections.find((s: any) => s.id === 'peerlink:guidance').text).toContain('Your role here, as your user set it (other agents see it too): Owns the web app: pages, styles, the build.')
})

test('an agent you added to another space is told it is a member there, and sees it', async ($, on) => {
  engine(on)
  fakeDaemon(on, 1, null, [], { space: 'games', space_source: 'file', space_from: '/Users/x/games/.peerlink', also: ['acme'] })
  on('prompt.compose', () => ({ sections: [] }))
  await $.session.start(START)
  await $.prompt.submit({ text: 'go' })
  const composed: any = await $.prompt.compose(COMPOSE)
  const text = composed.sections.find((s: any) => s.id === 'peerlink:guidance').text
  expect(text).toContain('in the "games" space; you only see agents in that space and "acme"')
  expect(text).toContain('Your user also made you a member of "acme": you see and message the agents there, and they see you. Your own space is still "games".')
})

test('plain /peerlink shows status, not the web UI', async ($, on) => {
  engine(on)
  fakeDaemon(on)
  await $.session.start(START)
  const { text } = (await $.command.run({ command: 'peerlink' })) as any
  expect(text).not.toContain('Opened the web UI')
  expect(text).toContain('this agent: web')
})

test('open peer requests are put in the context, so compaction or /clear cannot drop them', async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on)
  daemon.state.threads = {
    owes: [{ id: 'm51', with: 'acme', ago_s: 120, text: 'What are client and server working on?' }],
    waiting: [{ id: 'm52', with: 'client', ago_s: 60, text: 'One line on what you are doing.' }],
  }
  on('prompt.context', (_: any, e: any) => ({ blocks: e.blocks }))
  await $.session.start(START)
  const out: any = await $.prompt.context({ blocks: [] } as any)
  const block = out.blocks.find((b: any) => b.name === 'peerlink open threads')
  expect(block.text).toContain('You owe a reply to:\n- m51 acme (2m ago): What are client and server working on?')
  expect(block.text).toContain("You're waiting on:\n- m52 client (60s ago)")
})

test('after /clear the agent stays registered, and its new session takes over the old one', async ($, on) => {
  const { clock, ids } = engine(on)
  const daemon = fakeDaemon(on)
  on('session.end', (_: any, e: any) => ({ sessionId: e.sessionId }))
  await $.session.start(START)
  await $.session.end({ reason: 'clear', sessionId: 'me-session' } as any)
  expect(daemon.posted.some(p => p.path === '/api/sessions/me-session' && !p.body)).toBe(false)
  ids.current = 'new-session'
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  const again = daemon.posted.filter(p => p.path === '/api/sessions').at(-1)!.body
  expect(again.id).toBe('new-session')
  expect(again.succeeds).toBe('me-session')
})

for (const [installed, reloads] of [['0.8.48', true], ['0.8.47', false]] as const)
  test(`with ${installed} installed, an agent ${reloads ? 'queues a reload of itself' : 'stays as it is'}`, async ($, on) => {
    const { clock } = engine(on, { installed })
    const daemon = fakeDaemon(on)
    const ran: string[] = []
    on('command.run', { command: 'reload-plugins' }, () => {
      ran.push('reload-plugins')
      return { text: 'Reloaded' }
    })
    await $.session.start(START)
    await clock.advance(600000)
    for (let i = 0; i < 10; i++) await settle()
    expect(ran.length).toBe(reloads ? 1 : 0)
    // The daemon hears when it was queued, at the next heartbeat, so the web UI can tell a reload that never ran.
    await clock.advance(600000)
    for (let i = 0; i < 10; i++) await settle()
    const beats = daemon.posted.filter(p => p.path.endsWith('/heartbeat') && 'busy' in p.body)
    expect(beats.at(-1)!.body.reload_queued > 0).toBe(reloads)
  })

test('each heartbeat says how full the context is, and a nearly full peer is flagged', async ($, on) => {
  const { clock } = engine(on)
  const daemon = fakeDaemon(on)
  Object.assign(daemon.peers[0], { context: 91 })
  await $.session.start(START)
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  expect(daemon.posted.find(p => p.path.endsWith('/heartbeat') && 'busy' in p.body)!.body.context).toBe(85)
  const first: any = await $.prompt.submit({ text: 'go' })
  expect(first.context?.join('\n')).toContain('billing-api (context 91% full): ~/r/billing')
})

test('the branch is sent on registering, and again when it changes', async ($, on) => {
  const { clock, git } = engine(on)
  const daemon = fakeDaemon(on)
  await $.session.start(START)
  expect(daemon.posted.find(p => p.path === '/api/sessions')!.body.branch).toBe('main')
  const beats = () => daemon.posted.filter(p => p.path.endsWith('/heartbeat') && 'busy' in p.body).map(p => p.body.branch)
  const beat = async () => {
    await clock.advance(600000)
    for (let i = 0; i < 10; i++) await settle()
    return beats().at(-1)
  }
  expect(await beat()).toBeUndefined()
  git.head = 'feat/invoices'
  expect(await beat()).toBe('feat/invoices')
  expect(await beat()).toBeUndefined()
  git.head = ''
  expect(await beat()).toBe('detached @ 1a2b3c4')
  git.head = 'fix'
  git.dirs = ['/Users/x/r/web/.git/worktrees/wt-2', '/Users/x/r/web/.git', '/Users/x/r/wt-2']
  expect(await beat()).toBe('fix (worktree wt-2)')
  git.dirs = null
  expect(await beat()).toBe('')
})

test('in the terminal, the peerlink line is a clickable row under the hint row that opens the web UI', async ($, on) => {
  const { pinned } = engine(on)
  const daemon = fakeDaemon(on, 1, null, [], { space: 'bridge', space_source: 'file', space_from: '/Users/x/r/web/.peerlink', space_parent: 'acme' })
  on('ui.render', ($: any, e: any) => {
    const { Text } = $.ui.resolve(e) as any
    return <Text>{e.props.hint}</Text>
  })
  await $.session.start(START)
  await settle()
  const hint = await $.ui.mount({ plugin: 'peerlink', surface: 'terminal', component: 'PromptHint', requestId: 'h1', props: { isDraft: false, isWorking: false, hint: '? for shortcuts' } } as any)
  expect(await hint.find({ type: 'Text', text: '? for shortcuts' })).toBeDefined()
  const button: any = await hint.find({ type: 'Button' })
  expect(JSON.stringify(button)).toContain('⇄ peerlink · web · bridge (in acme)')
  expect(JSON.stringify(button)).not.toContain('http')
  await hint.press(button)
  await settle()
  expect(daemon.posted.some(p => p.path === '/api/web/link')).toBe(true)
  await hint.unmount()
  expect(pinned.filter((p: any) => p?.text)).toEqual([])
})

test('a channel master is told how Discord reaches it', async ($, on) => {
  engine(on)
  fakeDaemon(on, 1, null, [], { discord: [{ channel_id: '1', channel: '#acme', master: true }, { channel_id: '2', channel: '#builds', master: false }] })
  on('prompt.compose', () => ({ sections: [] }))
  await $.session.start(START)
  const composed: any = await $.prompt.compose(COMPOSE)
  const text = composed.sections.find((s: any) => s.id === 'peerlink:guidance').text
  expect(text).toContain('Their messages in #acme come to you')
  expect(text).toContain('In #builds, they reach you by starting a message with your name')
  expect(text).toContain('send_message to "discord" plus the channel, like "discord#acme"')
})

test('an agent that only sends to a channel is told it can post there, not that it is reached there', async ($, on) => {
  engine(on)
  fakeDaemon(on, 1, null, [], { discord: [{ channel_id: '1', channel: '#feed', master: false, receives: false, sends: true }, { channel_id: '2', channel: '#acme', master: true, receives: true, sends: false }] })
  on('prompt.compose', () => ({ sections: [] }))
  await $.session.start(START)
  const composed: any = await $.prompt.compose(COMPOSE)
  const text = composed.sections.find((s: any) => s.id === 'peerlink:guidance').text
  expect(text).toContain('Their messages in #acme come to you')
  expect(text).not.toContain('In #feed')
  expect(text).toContain('send_message to "discord".')
})

test('a heartbeat during a turn says what the agent is doing', async ($, on) => {
  const { clock } = engine(on, { turns: 1 })
  const daemon = fakeDaemon(on)
  on('turn.start', (_: any, e: any) => ({ turnId: e.turnId }))
  await $.session.start(START)
  await settle()
  await $.turn.start({ text: 'go', turnId: 't1' } as any)
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  expect(daemon.posted.filter(p => p.path.endsWith('/heartbeat') && p.body?.busy).at(-1)!.body.activity).toBe('thinking')
})

test('send_message passes options and files along', async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on)
  await $.session.start(START)
  await $.tool.call({ tool: 'mcp__peerlink__send_message', to: 'discord', text: 'Which branch?', options: ['main', 'dev'], files: ['/tmp/plan.md'] } as any)
  const sent = daemon.posted.find(p => p.path === '/api/messages')!.body
  expect(sent.options).toEqual(['main', 'dev'])
  expect(sent.files).toEqual(['/tmp/plan.md'])
})

test('an announcement is sent as one, so a lead reaches its whole space', async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on)
  await $.session.start(START)
  await $.tool.call({ tool: 'mcp__peerlink__send_message', to: 'space', text: 'Freeze on main', kind: 'announcement' } as any)
  expect(daemon.posted.find(p => p.path === '/api/messages')!.body.kind).toBe('announcement')
})

test('a task is sent as one, and its owner closes it with update_task', async ($, on) => {
  engine(on)
  const daemon = fakeDaemon(on)
  await $.session.start(START)
  const sent = await $.tool.call({ tool: 'mcp__peerlink__send_message', to: 'api', text: 'Add a health endpoint', kind: 'task' } as any)
  expect(daemon.posted.find(p => p.path === '/api/messages')!.body.kind).toBe('task')
  expect(JSON.stringify(sent)).toContain('as task t3')
  const done = await $.tool.call({ tool: 'mcp__peerlink__update_task', id: 't3', status: 'done', note: 'Added /health' } as any)
  expect(daemon.posted.find(p => p.path === '/api/tasks/t3')!.body).toEqual({ by: 'me-session', status: 'done', note: 'Added /health' })
  expect(JSON.stringify(done)).toContain('Task t3 is done. acme gets your note.')
})

test('an incoming task shows its task id instead of reply expected', async ($, on) => {
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e) as any
    return <Text>engine</Text>
  })
  const text = PROMPT.replace('↘ api · m7 · re m6 · reply expected', '↘ api · m7 · task t3')
  const ui = await $.ui.mount({ plugin: 'peerlink', surface: 'terminal', component: 'UserMessage', requestId: 'u1', props: { text, origin: { kind: 'plugin', name: 'peerlink' }, isExpanded: false } } as any)
  expect(await ui.find({ type: 'Text', text: /m7 · task t3$/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /reply expected/ })).toBeUndefined()
})

test('an announcement does not wake the agent: it is read at the start of its next turn', async ($, on) => {
  const { clock } = engine(on)
  const logged: string[] = []
  on('ui.log', (_: any, e: any) => {
    logged.push(e.text)
    return { value: undefined }
  })
  on('prompt.context', (_: any, e: any) => ({ blocks: e.blocks }))
  const inbox: any[] = []
  fakeDaemon(on, 1, null, inbox)
  await $.session.start(START)
  await settle()
  inbox.push({ id: 'm8', from: 'api-id', from_name: 'api', from_host: 'Mac', text: 'Freeze on main until 5pm.', expects_reply: false, reply_to: null, hops: 0, kind: 'announcement' })
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  expect(logged).toContain('↘ api · m8 · announcement: Freeze on main until 5pm.  (read at the next turn)')
  const out: any = await $.prompt.context({ blocks: [] } as any)
  expect(out.blocks.find((b: any) => b.name === 'peerlink announcements').text).toContain('↘ api · m8 · announcement\nFreeze on main until 5pm.')
  const again: any = await $.prompt.context({ blocks: [] } as any)
  expect(again.blocks.find((b: any) => b.name === 'peerlink announcements')).toBeUndefined()
})

test('a turn the usage limit kills puts back what it was given, holds new messages, and the next good turn lifts it', async ($, on) => {
  const { clock, woke, limits } = engine(on)
  mock.session(on)
  on('turn.start', (_: any, e: any) => ({ turnId: e.turnId }))
  const reset = '2026-10-25T15:00:00Z'
  limits.push({ kind: 'five_hour', percentUsed: 40, resetsAt: reset })
  const inbox: any[] = []
  const daemon = fakeDaemon(on, 1, null, inbox, { wake_delay_s: 0 })
  await $.session.start(START)
  await settle()
  const msg = (id: string) => ({ id, from: 'api-id', from_name: 'api', from_host: 'Mac', text: `do ${id}`, expects_reply: true, reply_to: null, hops: 0 })
  inbox.push(msg('m1'))
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  expect(woke.length).toBe(1)
  // That turn dies on the limit: the daemon hears it, with m1 to hand back and the window's reset time.
  limits[0].percentUsed = 100
  await $.turn.complete({ ...TURN, reason: 'error' })
  for (let i = 0; i < 10; i++) await settle()
  const hit = daemon.posted.find(p => p.path.endsWith('/limit'))?.body
  expect(hit).toEqual({ resets_at: Date.parse(reset) / 1000, kind: 'five_hour', ids: ['m1'] })
  // While the daemon says the limit holds, a new message (and m1, handed out again) waits instead of waking it.
  daemon.state.once = { limit: { resets_at: Date.parse(reset) / 1000, by: 'web' } }
  inbox.push(msg('m2'), msg('m1'))
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  expect(woke.length).toBe(1)
  // You prompt it after swapping accounts and the turn goes through: the limit lifts, and the held ones go out.
  limits[0].percentUsed = 0
  await $.turn.start({ text: 'carry on', turnId: 't2' } as any)
  await $.turn.complete(TURN)
  for (let i = 0; i < 10; i++) await settle()
  expect(daemon.posted.some(p => p.path.endsWith('/limit') && p.body?.lifted)).toBe(true)
  await clock.advance(600000)
  for (let i = 0; i < 10; i++) await settle()
  expect(woke.length).toBe(2)
  expect(woke[1]).toContain('m2')
  expect(woke[1]).toContain('m1')
})
