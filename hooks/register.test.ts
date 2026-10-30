import type { On, SessionSendResult } from 'claude-code'
import { describe, expect, mock, test, type Engine } from 'claude-code/testing'

import { attention, dimmed, mark, parseWaiting, pendingRequest, startHint, taskDetail } from './panel'
import type { PanelAgent, PanelRunView, PanelSessionWait, PanelSnapshot, PanelTask } from '../types'

const CWD = 'C:/host'
const TOKEN = 'a'.repeat(40)
const RAN_OK = { stderr: '', isStdoutTruncated: false, isStderrTruncated: false }

type Options = {
  answers?: Record<string, unknown>
  serverDown?: boolean
  shim?: { exitCode: number; stdout: string }
  send?: (to: string) => SessionSendResult
  consume?: boolean
  sendAsTool?: boolean
}

function world(on: On, options: Options = {}) {
  const posts: { event: string; body: Record<string, unknown> }[] = []
  const sends: { to: string; text: string }[] = []
  const shims: string[][] = []
  const writes: Record<string, unknown>[] = []
  const ledger: Record<string, unknown>[] = []
  const clock = mock.clock(on, { now: 1_000 })
  on('fs.read', (_$, e) => {
    const path = e.path.replace(/\\/g, '/')
    if (path === `${CWD}/.sentinel-swarm/server.json`) return { value: JSON.stringify({ port: 47999 }) }
    if (path === `${CWD}/.sentinel-swarm/http-token`) return { value: `${TOKEN}\n` }
    throw new Error(`ENOENT: ${e.path}`)
  })
  on('http.fetch', (_$, e) => {
    if (options.serverDown) throw new Error('connection refused')
    const event = e.url.split('/hook/')[1] ?? ''
    posts.push({ event, body: JSON.parse(e.init?.body ?? '{}') as Record<string, unknown> })
    const answer = options.answers?.[event]
    const text = answer === undefined ? '' : JSON.stringify(answer)
    return { value: { status: 200, ok: true, headers: { 'x-sentinel-swarm-repo': encodeURIComponent(CWD) }, text } }
  })
  on('process.run', (_$, e) => {
    shims.push([...e.argv])
    const ran = options.shim ?? { exitCode: 1, stdout: '' }
    return { value: { ...RAN_OK, ...ran } }
  })
  on('session.send', async ($, e): Promise<SessionSendResult> => {
    sends.push({ to: typeof e.to === 'string' ? e.to : JSON.stringify(e.to), text: e.text })
    if (options.sendAsTool && typeof e.to !== 'string' && 'sessionId' in e.to) {
      const ran = await $.tool.call({ tool: 'SendMessage', to: 'uds:cc-msg-1', message: e.text, tool_use_id: 'toolu_plugin_1' } as never)
      if ('deny' in ran) return { isDelivered: false, reason: String(ran.deny) }
    }
    return options.send?.(e.to) ?? { isDelivered: true }
  })
  on('session.receive', (_$, e) => (options.consume ? { consumed: 'muted' } : { text: e.text }))
  on('classic.SessionStart', () => ({}))
  on('classic.PostToolUse', () => ({}))
  on('classic.Stop', () => ({}))
  on('tool.call', { tool: 'Write' }, (_$, e) => {
    writes.push({ ...e })
    return { result: { type: 'create', filePath: e.file_path, content: e.content, structuredPatch: [] } } as never
  })
  on('tool.call', { tool: 'SendMessage' }, () => ({ result: { success: true } }) as never)
  on('tool.call', { tool: /^mcp__swarm-ledger__/ }, (_$, e) => {
    ledger.push({ ...e })
    return { result: { content: [{ type: 'text', text: '{}' }] } } as never
  })
  return { clock, posts, sends, shims, writes, ledger, events: () => posts.map(p => p.event) }
}

async function start($: Engine, agentType = 'swarm-lead', sessionId = 'sess-lead') {
  return $.classic.SessionStart({ source: 'startup', agent_type: agentType, session_id: sessionId, cwd: CWD, transcript_path: 't.jsonl' } as never)
}

const DENY = (reason: string) => ({ hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: reason } })

describe('which sessions the mod acts in', () => {
  test('a session that is not a swarm role posts nothing and its tools run', async ($, on) => {
    const w = world(on, { answers: { pre_write: DENY('no') } })
    await start($, 'general-purpose', 'sess-user')
    await $.tool.call({ tool: 'Write', file_path: 'a.txt', content: 'x' })
    await $.classic.Stop({ stop_hook_active: false } as never)
    expect(w.posts).toEqual([])
    expect(w.writes.length).toBe(1)
  })
})

describe('gates', () => {
  test('a ledger deny stops the tool and names the reason', async ($, on) => {
    const w = world(on, { answers: { pre_write: DENY('only a Coder writes project files') } })
    await start($)
    const result = await $.tool.call({ tool: 'Write', file_path: 'a.txt', content: 'x' })
    expect(JSON.stringify(result)).toContain('only a Coder writes project files')
    expect(w.writes).toEqual([])
    const post = w.posts.find(p => p.event === 'pre_write')
    expect(post?.body).toMatchObject({
      session_id: 'sess-lead',
      agent_type: 'swarm-lead',
      tool_name: 'Write',
      tool_input: { file_path: 'a.txt', content: 'x' },
      sentinel_swarm_transport: 'mod',
    })
  })

  test("the Oracle's AskUserQuestion goes through pre_ask, whose deny names user_ask", async ($, on) => {
    const w = world(on, { answers: { pre_ask: DENY('this is a background session; ask with user_ask(question)') } })
    let asked = 0
    on('tool.call', { tool: 'AskUserQuestion' }, () => {
      asked += 1
      return { result: { answers: {} } } as never
    })
    await start($, 'swarm-oracle', 'sess-oracle')
    const result = await $.tool.call({ tool: 'AskUserQuestion', questions: [{ question: 'Which DB?' }] } as never)
    expect(JSON.stringify(result)).toContain('user_ask(question)')
    expect(asked).toBe(0)
    expect(w.events()).toContain('pre_ask')
  })

  test('an allow lets the tool run', async ($, on) => {
    const w = world(on)
    await start($)
    await $.tool.call({ tool: 'Write', file_path: 'a.txt', content: 'x' })
    expect(w.writes.length).toBe(1)
  })

  test('pre_ledger stamps agent_id through the updated input', async ($, on) => {
    const stamped = { hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'allow', updatedInput: { caller: 'lead', agent_id: 'sess-lead' } } }
    const w = world(on, { answers: { pre_ledger: stamped } })
    await start($)
    await $.tool.call({ tool: 'mcp__swarm-ledger__run_status', caller: 'lead', agent_id: 'forged' } as never)
    expect(w.ledger[0]).toMatchObject({ caller: 'lead', agent_id: 'sess-lead' })
  })

  test('with the server down the shim decides', async ($, on) => {
    const w = world(on, { serverDown: true, shim: { exitCode: 0, stdout: JSON.stringify(DENY('shim says no')) } })
    await start($)
    const result = await $.tool.call({ tool: 'Write', file_path: 'a.txt', content: 'x' })
    expect(JSON.stringify(result)).toContain('shim says no')
    expect(w.shims[w.shims.length - 1]).toEqual(['python3', '.sentinel-swarm/hook.py', 'hook', 'pre_write'])
    expect(w.writes).toEqual([])
  })

  test('a gate fails closed when neither the server nor the shim answers', async ($, on) => {
    const w = world(on, { serverDown: true })
    await start($)
    const result = await $.tool.call({ tool: 'Write', file_path: 'a.txt', content: 'x' })
    expect(JSON.stringify(result)).toContain('sentinel-swarm cannot check this call')
    expect(JSON.stringify(result)).toContain('/sentinel-swarm:setup')
    expect(w.writes).toEqual([])
    expect(w.shims.filter(argv => argv[3] === 'pre_write').map(argv => argv[0])).toEqual(['python3', 'python'])
  })
})

describe('classic events', () => {
  test('session_start context reaches the model', async ($, on) => {
    world(on, { answers: { session_start: { hookSpecificOutput: { hookEventName: 'SessionStart', additionalContext: 'You are lead.' } } } })
    const result = await start($)
    expect(result.additionalContext).toEqual(['You are lead.'])
  })

  test('a stop block passes through', async ($, on) => {
    world(on, { answers: { stop: { decision: 'block', reason: 'Call message_inbox.' } } })
    await start($)
    const result = await $.classic.Stop({ stop_hook_active: false } as never)
    expect(result.block).toBe('Call message_inbox.')
  })

  test('post_activity carries the time it fired; post_any does not', async ($, on) => {
    const w = world(on)
    await start($)
    await $.classic.PostToolUse({ tool_name: 'Read', tool_input: {}, tool_response: {}, tool_use_id: 'u1' } as never)
    await $.classic.PostToolUse({ tool_name: 'Write', tool_input: {}, tool_response: {}, tool_use_id: 'u2' } as never)
    const activity = w.posts.find(p => p.event === 'post_activity')
    const any = w.posts.find(p => p.event === 'post_any')
    expect(typeof activity?.body.sentinel_swarm_fired_at).toBe('string')
    expect(any?.body.sentinel_swarm_fired_at).toBeUndefined()
  })
})

const OWED = {
  wakeups: [
    { session_id: 'sess-coder', session_name: 'host-r1-coder', name: 'coder', role: 'coder', text: 'Handoff 1 returned.', wakeup_ids: [3] },
    { session_id: 'sess-oracle', session_name: 'host-oracle-1008', name: 'oracle', role: 'oracle', text: 'Phase 1 handed up. elapsed 60s', wakeup_ids: [4, 5] },
  ],
}

describe('owed wake-ups', () => {
  test('the mod pays every owed wake-up by session id after a ledger call and reports the delivered ids', async ($, on) => {
    const w = world(on, { answers: { owed: OWED } })
    await start($)
    await $.classic.PostToolUse({ tool_name: 'mcp__swarm-ledger__return_work', tool_input: {}, tool_response: {}, tool_use_id: 'u1' } as never)
    await w.clock.settle()
    expect(w.sends.map(s => s.text).sort()).toEqual(['Handoff 1 returned.', 'Phase 1 handed up. elapsed 60s'])
    expect(w.sends.find(s => s.text.startsWith('Handoff'))?.to).toContain('sess-coder')
    expect(w.sends.find(s => s.text.startsWith('Phase'))?.to).toContain('sess-oracle')
    const paid = w.posts.find(p => p.event === 'wake_sent')
    expect((paid?.body.wakeup_ids as number[]).sort()).toEqual([3, 4, 5])
  })

  test("the mod's own send passes its SendMessage gate, and a model's send to another name does not", async ($, on) => {
    const w = world(on, { answers: { owed: { wakeups: [OWED.wakeups[0]] }, pre_send_message: DENY('not in this run') }, sendAsTool: true })
    await start($)
    await $.classic.Stop({ stop_hook_active: false } as never)
    expect(w.events()).not.toContain('pre_send_message')
    expect(w.posts.find(p => p.event === 'wake_sent')?.body.wakeup_ids).toEqual([3])
    const model = await $.tool.call({ tool: 'SendMessage', to: 'nobody-here', message: 'hi' } as never)
    expect(JSON.stringify(model)).toContain('not in this run')
  })

  test('a send refused for another reason stays owed and is not retried', async ($, on) => {
    const w = world(on, { answers: { owed: { wakeups: [OWED.wakeups[0]] } }, send: () => ({ isDelivered: false, reason: 'gone' }) })
    await start($)
    await $.classic.Stop({ stop_hook_active: false } as never)
    expect(w.sends.length).toBe(1)
    expect(w.events()).not.toContain('wake_sent')
    expect(w.events().indexOf('owed')).toBeLessThan(w.events().indexOf('stop'))
  })

  test('a target that is not registered yet gets the send again after a pause', async ($, on) => {
    let tries = 0
    const w = world(on, {
      answers: { owed: { wakeups: [OWED.wakeups[0]] } },
      send: () => (++tries < 3 ? { isDelivered: false, reason: 'no live session on this machine has id sess-coder' } : { isDelivered: true }),
    })
    await start($)
    const stopped = $.classic.Stop({ stop_hook_active: false } as never)
    await w.clock.advance(2_000)
    await w.clock.advance(4_000)
    await stopped
    expect(w.sends.length).toBe(3)
    expect(w.posts.find(p => p.event === 'wake_sent')?.body.wakeup_ids).toEqual([3])
  })

  test('the Stop hook pays before it posts stop', async ($, on) => {
    const w = world(on, { answers: { owed: { wakeups: [OWED.wakeups[0]] } } })
    await start($)
    await $.classic.Stop({ stop_hook_active: false } as never)
    const order = w.events()
    expect(order.indexOf('wake_sent')).toBeLessThan(order.indexOf('stop'))
  })

  test('the Stop hook waits for a send still retrying from a ledger call', async ($, on) => {
    let tries = 0
    const w = world(on, {
      answers: { owed: { wakeups: [OWED.wakeups[0]] } },
      send: () => (++tries < 2 ? { isDelivered: false, reason: 'no live session on this machine has id sess-coder' } : { isDelivered: true }),
    })
    await start($)
    await $.classic.PostToolUse({ tool_name: 'mcp__swarm-ledger__message_post', tool_input: {}, tool_response: {}, tool_use_id: 'u1' } as never)
    const stopped = $.classic.Stop({ stop_hook_active: false } as never)
    await w.clock.advance(2_000)
    await stopped
    const order = w.events()
    expect(order.indexOf('wake_sent')).toBeLessThan(order.indexOf('stop'))
    expect(w.posts.find(p => p.event === 'stop')?.body).not.toHaveProperty('sentinel_swarm_sending')
  })

  test('a target that never registers stays owed after the last retry', async ($, on) => {
    const w = world(on, {
      answers: { owed: { wakeups: [OWED.wakeups[0]] } },
      send: () => ({ isDelivered: false, reason: 'no live session on this machine has id sess-coder' }),
    })
    await start($)
    const stopped = $.classic.Stop({ stop_hook_active: false } as never)
    await w.clock.advance(2_000)
    await w.clock.advance(4_000)
    await w.clock.advance(8_000)
    await stopped
    expect(w.sends.length).toBe(4)
    expect(w.events()).not.toContain('wake_sent')
  })
})

describe('a test run that ends while the session waits', () => {
  test('the recorded result starts a turn, and polling stops once nothing runs', async ($, on) => {
    const w = world(on, { answers: { self_owed: { text: 'Test run 3 (full tests) is recorded, passed.', running: 0 } } })
    const prompts: string[] = []
    on('prompt.submit', (_$, e) => {
      prompts.push(e.text)
      return { text: e.text }
    })
    await start($)
    await w.clock.advance(15_000)
    expect(prompts).toEqual(['Test run 3 (full tests) is recorded, passed.'])
    await w.clock.advance(15_000)
    expect(w.events().filter(event => event === 'self_owed').length).toBe(1)
  })

  test('a run still in flight keeps the poll going, and a ledger call restarts it', async ($, on) => {
    const answers: Record<string, unknown> = { self_owed: { text: null, running: 1 } }
    const w = world(on, { answers })
    const prompts: string[] = []
    on('prompt.submit', (_$, e) => {
      prompts.push(e.text)
      return { text: e.text }
    })
    await start($)
    await w.clock.advance(15_000)
    answers.self_owed = { text: null, running: 0 }
    await w.clock.advance(15_000)
    await w.clock.advance(15_000)
    expect(w.events().filter(event => event === 'self_owed').length).toBe(2)
    await $.classic.PostToolUse({ tool_name: 'mcp__swarm-ledger__tests_run', tool_input: {}, tool_response: {}, tool_use_id: 'u1' } as never)
    await w.clock.advance(15_000)
    expect(w.events().filter(event => event === 'self_owed').length).toBe(3)
    expect(prompts).toEqual([])
  })
})

describe("a model's SendMessage", () => {
  test('a swarm session name goes to the live session by its id', async ($, on) => {
    const w = world(on, { answers: { send_target: { session_id: 'sess-mgr', name: 'mgr-p1-phase-1' } } })
    await start($)
    const result = await $.session.send({ to: 'host-r1-mgr-p1-phase-1 [a1b2c3]', text: 'Handoff 1 is waiting.', origin: { kind: 'model' } } as never)
    expect(result.isDelivered).toBe(true)
    expect(w.posts.find(p => p.event === 'send_target')?.body.to).toBe('host-r1-mgr-p1-phase-1 [a1b2c3]')
    expect(w.sends.length).toBe(1)
    expect(w.sends[0]?.to).toContain('sess-mgr')
    expect(w.sends[0]?.to).not.toContain('host-r1')
  })

  test('a name the ledger does not resolve is sent as the model wrote it', async ($, on) => {
    const w = world(on)
    await start($)
    await $.session.send({ to: 'someone-else', text: 'hi', origin: { kind: 'model' } } as never)
    expect(w.sends.map(s => s.to)).toEqual(['someone-else'])
  })

  test('a send outside a swarm session asks the ledger nothing', async ($, on) => {
    const w = world(on)
    await start($, 'general-purpose', 'sess-user')
    await $.session.send({ to: 'host-r1-mgr-p1-phase-1', text: 'hi', origin: { kind: 'model' } } as never)
    expect(w.events()).not.toContain('send_target')
  })
})

describe('the inbox inside a wake-up', () => {
  const TAKEN = { claim: 'c1', messages: [{ message_id: 7, from_name: 'manager', body: 'Start module 2.' }], remaining: 0 }

  test('a delivery carries the unread messages and acknowledges the claim', async ($, on) => {
    const w = world(on, { answers: { inbox_take: TAKEN } })
    await start($)
    const result = await $.session.receive({ origin: { kind: 'peer', plugin: 'sentinel-swarm' }, text: 'Message 7 from manager is waiting.' } as never)
    expect(result.text).toContain('Start module 2.')
    expect(result.text).toContain('do not call message_inbox')
    expect(w.posts.find(p => p.event === 'inbox_ack')?.body.claim).toBe('c1')
  })

  test('a consumed delivery releases the claim', async ($, on) => {
    const w = world(on, { answers: { inbox_take: TAKEN }, consume: true })
    await start($)
    await $.session.receive({ origin: { kind: 'peer' }, text: 'ping' } as never)
    expect(w.events()).toContain('inbox_release')
    expect(w.events()).not.toContain('inbox_ack')
  })
})

describe('the swarm status tool', () => {
  function server(on: On, health: { status: number; body: unknown } | null) {
    const urls: string[] = []
    on('fs.read', (_$, e) => {
      if (e.path.replace(/\\/g, '/') === `${CWD}/.sentinel-swarm/server.json`) return { value: JSON.stringify({ port: 47999 }) }
      throw new Error(`ENOENT: ${e.path}`)
    })
    on('http.fetch', (_$, e) => {
      urls.push(e.url)
      if (health === null) throw new Error('connection refused')
      return { value: { status: health.status, ok: health.status === 200, headers: {}, text: JSON.stringify(health.body) } }
    })
    return urls
  }

  async function status($: Engine, repo: string) {
    const ran = (await $.tool.call({ tool: 'mcp__sentinel-swarm__swarm_status', repo } as never)) as { result?: unknown }
    return JSON.parse(String(ran.result)) as { running: boolean; repo: string }
  }

  test('a ledger server that answers for the repo means a run is active', async ($, on) => {
    const urls = server(on, { status: 200, body: { name: 'swarm-ledger', repo_root: 'C:\\host\\', pid: 1 } })
    expect(await status($, CWD)).toEqual({ running: true, repo: CWD })
    expect(urls).toEqual(['http://127.0.0.1:47999/health'])
  })

  test('a server that does not answer means no run is active', async ($, on) => {
    server(on, null)
    expect((await status($, CWD)).running).toBe(false)
  })

  test('a server for another repo means none is active here', async ($, on) => {
    server(on, { status: 200, body: { name: 'swarm-ledger', repo_root: 'C:/other', pid: 1 } })
    expect((await status($, CWD)).running).toBe(false)
  })

  test('a repo without server.json asks no server', async ($, on) => {
    const urls = server(on, { status: 200, body: { name: 'swarm-ledger', repo_root: 'C:/elsewhere', pid: 1 } })
    expect((await status($, 'C:/elsewhere')).running).toBe(false)
    expect(urls).toEqual([])
  })
})

describe('the run panel', () => {
  test('opens on a repo with no ledger and no server', async ($, on) => {
    const opened: string[] = []
    const launched: string[][] = []
    const clock = mock.clock(on, { now: 1_000 })
    on('ui.open', (_$, e) => {
      opened.push(e.id)
      return { value: { isPlaced: true } }
    })
    on('fs.read', (_$, e) => {
      throw new Error(`ENOENT: ${e.path}`)
    })
    on('process.run', (_$, e) => {
      launched.push([...e.argv])
      return {
      value: { ...RAN_OK, exitCode: 0, stdout: JSON.stringify({ repo: 'C:/fresh', generated_at: '', runs: [], error: 'no ledger' }) },
      }
    })
    const ran = (await $.tool.call({ tool: 'mcp__sentinel-swarm__open_panel', repo: 'C:/fresh' } as never)) as { result?: unknown }
    expect(String(ran.result)).toBe('Swarm panel opened on C:/fresh.')
    expect(opened).toEqual(['swarm-run'])
    await clock.settle()
    expect(launched[0]?.slice(-3)).toEqual(['panel', '--repo', 'C:/fresh'])
  })

  const view = (ended: boolean) => ({ run: { run_id: 3, ended_at: ended ? '2026-10-27T00:00:00Z' : null } }) as PanelRunView

  test('says how to start a run when none is active', () => {
    expect(startHint([], false)[0]).toBe('No swarm has run in this repo.')
    expect(startHint([view(true)], false)).toEqual(['No run is active.', 'To start a run, run /sentinel-swarm:run with a PRD.'])
    expect(startHint([view(false)], false)[1]).toBe('To continue it, run /sentinel-swarm:resume.')
    expect(startHint([view(false)], true)).toEqual([])
  })
})

function task(overrides: Partial<PanelTask> = {}): PanelTask {
  return { id: 'file_id:1', name: 'a.py', path: null, state: 'claimed', status: 'active', owners: [], summary: null, brief: null, review: null, ...overrides }
}

function agent(overrides: Partial<PanelAgent> = {}): PanelAgent {
  return {
    agent_id: 'sess-coder',
    name: 'coder-a',
    role: 'coder',
    model: 'sonnet',
    effort: null,
    state: 'working',
    queued: false,
    session_name: 'host-r3-coder-a',
    transcript_path: 'C:/t/coder.jsonl',
    session: 'busy',
    parked: false,
    park_reason: null,
    ended_at: null,
    end_reason: null,
    phase: null,
    module: null,
    file: null,
    activity: null,
    heartbeat_at: null,
    started_at: '2026-10-28T00:00:00Z',
    owed: 0,
    unread: 0,
    task: task(),
    tasks: [],
    children: [],
    ...overrides,
  }
}

function runView(overrides: Partial<PanelRunView> = {}): PanelRunView {
  const oracle = agent({
    agent_id: 'sess-oracle',
    name: 'oracle',
    role: 'oracle',
    session_name: 'host-oracle',
    transcript_path: null,
    task: task({ id: 'run:3', name: 'all phases', state: 'active' }),
  })
  return {
    run: { run_id: 3, state: 'active', outcome: null, branch: null, started_at: '2026-10-28T00:00:00Z', ended_at: null, elapsed_s: 60 },
    agents: [{ ...oracle, children: [agent()] }],
    counts: { live: 2 },
    waiting: { questions: [], findings: [], paused: null },
    ...overrides,
  }
}

const SESSION_WAIT: PanelSessionWait = {
  sessionId: 'sess-coder',
  name: 'host-r3-coder-a',
  waitingFor: 'permission prompt',
  request: { kind: 'tool', tool: 'Write', input: '.claude/scratch/x.py' },
}

describe('which rows the panel dims', () => {
  test('ended, released, parked, gone, approved and not-started rows dim; working rows do not', () => {
    expect(dimmed(agent())).toBe(false)
    expect(dimmed(agent({ ended_at: '2026-10-28T01:00:00Z' }))).toBe(true)
    expect(dimmed(agent({ state: 'released' }))).toBe(true)
    expect(dimmed(agent({ state: 'parked' }))).toBe(true)
    expect(dimmed(agent({ parked: true, park_reason: 'waiting for memory to resume it for a wake-up' }))).toBe(true)
    expect(dimmed(agent({ session: 'gone' }))).toBe(true)
    expect(dimmed(agent({ task: task({ state: 'approved' }) }))).toBe(true)
    expect(dimmed(agent({ queued: true, state: 'queued', session: null }))).toBe(true)
    expect(dimmed(agent({ role: 'manager', task: task({ state: 'planned' }) }))).toBe(true)
    expect(dimmed(agent({ role: 'lead', task: task({ state: 'planned' }) }))).toBe(false)
  })

  test('a waiting agent shows the warning glyph and a queued one the idle glyph', () => {
    expect(mark(agent(), true)).toEqual(['⚠', 'warning'])
    expect(mark(agent({ queued: true, session: null }))).toEqual(['○', undefined])
    expect(mark(agent({ state: 'parked' }))).toEqual(['⏸', 'warning'])
  })
})

describe('a file task', () => {
  test('its detail starts with the full path', () => {
    const lines = taskDetail(task({ path: 'src/pkg/a.py', brief: 'Write it.' }), 40)
    expect(lines[0]?.map(span => span.text).join('')).toBe('src/pkg/a.py')
    expect(lines[0]?.some(span => span.code)).toBe(true)
    expect(lines[1]).toEqual([])
  })

  test('a task without a path starts with its brief', () => {
    const lines = taskDetail(task({ brief: 'Write it.' }), 40)
    expect(lines[0]?.map(span => span.text).join('')).toBe('Write it.')
  })
})

describe('sessions waiting on a person', () => {
  test('the agents listing keeps only waiting sessions', () => {
    const listing = [
      { sessionId: 'a', name: 'host-r3-coder-a', status: 'waiting', waitingFor: 'permission prompt' },
      { sessionId: 'b', name: 'host-oracle', kind: 'background', state: 'waiting', waitingFor: 'input needed' },
      { sessionId: 'c', name: 'busy-one', status: 'busy' },
      { name: 'no-id', status: 'waiting' },
    ]
    expect(parseWaiting(JSON.stringify(listing))).toEqual([
      { sessionId: 'a', name: 'host-r3-coder-a', waitingFor: 'permission prompt' },
      { sessionId: 'b', name: 'host-oracle', waitingFor: 'input needed' },
    ])
    expect(parseWaiting('not json')).toEqual([])
  })

  const line = (entry: unknown) => JSON.stringify(entry)
  const use = (id: string, name: string, input: unknown) => line({ type: 'assistant', message: { content: [{ type: 'tool_use', id, name, input }] } })
  const result = (id: string) => line({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: id, content: 'ok' }] } })

  test('the pending request is the last tool use with no result', () => {
    const transcript = [use('t1', 'Read', { file_path: 'a.py' }), result('t1'), use('t2', 'Write', { file_path: '.claude/scratch/x.py', content: 'x' }), ''].join('\n')
    expect(pendingRequest(transcript)).toEqual({ kind: 'tool', tool: 'Write', input: '.claude/scratch/x.py' })
    expect(pendingRequest([use('t1', 'Read', {}), result('t1')].join('\n'))).toBeNull()
  })

  test('a pending question carries its options', () => {
    const asked = use('q1', 'AskUserQuestion', {
      questions: [
        {
          question: 'Which branch?',
          header: 'Branch',
          multiSelect: false,
          options: [
            { label: 'main', description: '' },
            { label: 'new', description: '' },
          ],
        },
      ],
    })
    expect(pendingRequest(asked)).toEqual({ kind: 'question', questions: [{ question: 'Which branch?', options: ['main', 'new'] }] })
  })

  test('a shell command shows its command line', () => {
    expect(pendingRequest(use('b1', 'Bash', { command: 'rm -rf build' }))).toEqual({ kind: 'tool', tool: 'Bash', input: 'rm -rf build' })
  })
})

describe('what waits on you', () => {
  test('a waiting session sits under its agent and says where to answer', () => {
    const [item, ...rest] = attention(runView(), [SESSION_WAIT])
    expect(rest).toEqual([])
    expect(item).toMatchObject({ agent: 'coder-a', title: 'waits on a permission prompt', lines: ['Write: .claude/scratch/x.py'], directiveId: null })
    expect(item?.where).toContain('agent view')
    expect(item?.where).toContain('host-r3-coder-a')
  })

  test('a session matches by its name when its id changed', () => {
    const items = attention(runView(), [{ ...SESSION_WAIT, sessionId: 'other-id' }])
    expect(items.map(i => i.agent)).toEqual(['coder-a'])
  })

  test('a session outside the run is not listed', () => {
    expect(attention(runView(), [{ ...SESSION_WAIT, sessionId: 'x', name: 'someone-else' }])).toEqual([])
  })

  test('a question sits under the Oracle, a stall and a pause sit at the top', () => {
    const view = runView({
      run: { ...runView().run, state: 'paused' },
      waiting: {
        questions: [{ directive_id: 7, question: 'Which branch?', asked_at: 'T' }],
        findings: [
          { finding_id: 1, kind: 'run_stalled', agent: 'oracle', detail: 'No agent has worked for 30 minutes.', since: 'T' },
          { finding_id: 2, kind: 'needs_user', agent: 'coder-a', detail: 'coder-a waits on a permission prompt', since: 'T' },
        ],
        paused: 'the build needs a licence key',
      },
    })
    const items = attention(view, [])
    expect(items.map(i => [i.agent, i.title])).toEqual([
      ['oracle', 'asks you (directive 7)'],
      [null, 'the run has stalled'],
      ['coder-a', 'coder-a needs you'],
      [null, 'the run is paused'],
    ])
    expect(items[0]?.directiveId).toBe(7)
  })

  test('the session wait replaces the watchdog finding for the same agent', () => {
    const view = runView({
      waiting: { questions: [], findings: [{ finding_id: 2, kind: 'needs_user', agent: 'coder-a', detail: 'waits', since: 'T' }], paused: null },
    })
    expect(attention(view, [SESSION_WAIT]).map(i => i.key)).toEqual(['wait:3:coder-a'])
  })

  test('an ended run waits on nobody', () => {
    const view = runView({
      run: { ...runView().run, ended_at: '2026-10-28T02:00:00Z' },
      waiting: { questions: [{ directive_id: 7, question: '?', asked_at: 'T' }], findings: [], paused: null },
    })
    expect(attention(view, [SESSION_WAIT])).toEqual([])
  })
})

describe('the panel draws what waits on you', () => {
  const PANE_PROPS = { title: 'Swarm', isFocused: false, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} } as const
  const TRANSCRIPT = JSON.stringify({
    type: 'assistant',
    message: { content: [{ type: 'tool_use', id: 'w1', name: 'Write', input: { file_path: '.claude/scratch/x.py' } }] },
  })
  const LISTING = JSON.stringify([{ sessionId: 'sess-coder', name: 'host-r3-coder-a', status: 'waiting', waitingFor: 'permission prompt' }])

  test('the top section and the red box show the request, and an answer goes back as a directive reply', async ($, on) => {
    const launched: string[][] = []
    const toasts: string[] = []
    const base = runView({ waiting: { questions: [{ directive_id: 7, question: 'Which branch?', asked_at: 'T' }], findings: [], paused: null } })
    const gone = agent({ agent_id: 'sess-b', name: 'coder-b', session_name: 'host-r3-coder-b', session: 'gone', task: task({ id: 'file_id:2', name: 'b.py' }) })
    const oracle = base.agents[0] as PanelAgent
    const view = { ...base, agents: [{ ...oracle, children: [...oracle.children, gone] }] }
    const snapshot: PanelSnapshot = { repo: CWD, generated_at: '', runs: [view] }
    const clock = mock.clock(on, { now: 1_000 })
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.panes', () => ({ value: [] }))
    on('ui.toast', (_$, e) => {
      toasts.push(String((e as { text?: unknown }).text ?? ''))
      return { value: undefined }
    })
    on('fs.read', (_$, e) => {
      if (e.path.replace(/\\/g, '/') === 'C:/t/coder.jsonl') return { value: TRANSCRIPT }
      throw new Error(`ENOENT: ${e.path}`)
    })
    on('process.run', (_$, e) => {
      launched.push([...e.argv])
      const stdout = e.argv[0] === 'claude' ? LISTING : e.argv.includes('directive') ? '{}' : JSON.stringify(snapshot)
      return { value: { ...RAN_OK, exitCode: 0, stdout } }
    })
    await $.tool.call({ tool: 'mcp__sentinel-swarm__open_panel', repo: CWD } as never)
    await clock.settle()

    const ui = await $.ui.mount({ plugin: 'sentinel-swarm', component: 'Pane', requestId: 'swarm-run', surface: 'terminal', props: PANE_PROPS } as never)
    expect(await ui.find({ type: 'Text', text: /Waiting on you \(2\)/ })).toBeDefined()
    const box = await ui.find({ key: 'wait:3:coder-a' })
    expect(box?.props.borderColor).toBe('error')
    expect(box?.text).toContain('Write: .claude/scratch/x.py')
    expect(box?.text).toContain('host-r3-coder-a')
    expect((await ui.find({ key: 'question:3:7' }))?.text).toContain('Which branch?')
    expect((await ui.find({ type: 'Text', text: /^a\.py$/ }))?.props.wrap).toBe('truncate-start')
    const nameOf = async (name: string) => (await ui.findAll({ type: 'Text', text: new RegExp(`^${name}$`) })).find(t => t.props.color === '#ff8700')
    expect((await nameOf('coder-b'))?.props.dimColor).toBe(true)
    expect((await nameOf('coder-a'))?.props.dimColor).toBe(false)
    expect(await ui.find({ type: 'Text', text: /dimmed: ended or not started/ })).toBeDefined()

    await (ui as unknown as { input: (target: { key: string; text: string }) => Promise<unknown> }).input({ key: 'answer:question:3:7', text: 'use main' })
    await clock.settle()
    const reply = launched.find(argv => argv.includes('directive')) ?? []
    expect(reply.slice(reply.indexOf('directive'))).toEqual([
      'directive',
      '--repo',
      CWD,
      '--source',
      'user_chat',
      '--sender',
      'run panel',
      '--reply-to',
      '7',
      '--',
      'use main',
    ])
    expect(toasts).toEqual(['Sent your answer to directive 7.'])
    await ui.unmount()
  })
})
