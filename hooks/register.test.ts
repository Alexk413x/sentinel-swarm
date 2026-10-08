import type { On, SessionSendResult } from 'claude-code'
import { describe, expect, mock, test, type Engine } from 'claude-code/testing'

const CWD = 'C:/host'
const TOKEN = 'a'.repeat(40)
const ROLE_FILE = '---\nname: swarm-lead\ntools: Read\n---\n\n# Lead\n'
const COMMAND_ROLE_FILE =
  '---\nname: swarm-lead\nhooks:\n  Stop:\n    - hooks:\n        - type: command\n' +
  '          command: "python3 .sentinel-swarm/hook.py hook stop || python .sentinel-swarm/hook.py hook stop"\n---\n'
const RAN_OK = { stderr: '', isStdoutTruncated: false, isStderrTruncated: false }

type Options = {
  answers?: Record<string, unknown>
  roleFile?: string
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
    if (path.startsWith(`${CWD}/.claude/agents/`)) return { value: options.roleFile ?? ROLE_FILE }
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

  test('a role file that still carries the ledger command hooks keeps the mod out', async ($, on) => {
    const w = world(on, { roleFile: COMMAND_ROLE_FILE, answers: { pre_write: DENY('no') } })
    await start($)
    await $.tool.call({ tool: 'Write', file_path: 'a.txt', content: 'x' })
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
