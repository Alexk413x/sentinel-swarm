import type { On, SessionSendResult } from 'claude-code'
import { describe, expect, mock, test, type Engine } from 'claude-code/testing'

import { ALLOWED_REASON, DENIED_REASON, agentFacts, attention, dimmed, directiveLine, flatten, holdForPanel, mark, parseWaiting, startHint, stepLines, taskDetail } from './panel'
import type { PanelAgent, PanelDirective, PanelPermission, PanelRequest, PanelRunView, PanelSessionWait, PanelSnapshot, PanelTask } from '../types'

// The test engine resolves a path on the host system before the fs handlers below receive it:
// '/host' gains the current drive on Windows, and a drive-letter path is relative elsewhere.
// So the fake root has no drive, and the handlers compare paths without one.
const CWD = '/host'
const hostPath = (path: string) => path.replace(/\\/g, '/').replace(/^[A-Za-z]:/, '')
const TOKEN = 'a'.repeat(40)
const RAN_OK = { stderr: '', isStdoutTruncated: false, isStderrTruncated: false }

type Options = {
  answers?: Record<string, unknown | ((body: Record<string, unknown>) => unknown)>
  serverDown?: boolean
  shim?: { exitCode: number; stdout: string }
  send?: (to: string) => SessionSendResult | Promise<SessionSendResult>
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
    const path = hostPath(e.path)
    if (path === `${CWD}/.sentinel-swarm/server.json`) return { value: JSON.stringify({ port: 47999 }) }
    if (path === `${CWD}/.sentinel-swarm/http-token`) return { value: `${TOKEN}\n` }
    throw new Error(`ENOENT: ${e.path}`)
  })
  on('http.fetch', (_$, e) => {
    if (options.serverDown) throw new Error('connection refused')
    const event = e.url.split('/hook/')[1] ?? ''
    const body = JSON.parse(e.init?.body ?? '{}') as Record<string, unknown>
    posts.push({ event, body })
    const given = options.answers?.[event]
    const answer = typeof given === 'function' ? given(body) : given
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
    return (await options.send?.(e.to)) ?? { isDelivered: true }
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

describe('an MCP elicitation', () => {
  const ASKED = { mcp_server_name: 'other', message: 'Approve?', mode: 'form', requested_schema: {} } as never

  test('is refused in a swarm role session, with the ledger tools named', async ($, on) => {
    const w = world(on)
    const shown: string[] = []
    on('classic.Elicitation', (_$, e) => {
      shown.push(e.message)
      return {}
    })
    await start($)
    const posted = w.posts.length
    const result = await $.classic.Elicitation(ASKED)
    expect(result.block).toContain('sentinel-swarm refuses an MCP elicitation')
    expect(result.block).toContain('message_post')
    expect(shown).toEqual([])
    expect(w.posts.length).toBe(posted)
  })

  test('reaches the user in a session that is not a swarm role', async ($, on) => {
    const w = world(on)
    const shown: string[] = []
    on('classic.Elicitation', (_$, e) => {
      shown.push(e.message)
      return {}
    })
    await start($, 'general-purpose', 'sess-user')
    const result = await $.classic.Elicitation(ASKED)
    expect(result.block).toBeUndefined()
    expect(shown).toEqual(['Approve?'])
    expect(w.posts).toEqual([])
  })
})

describe('a later SessionStart', () => {
  test('without an agent_type does not switch the gates off in an adopted session', async ($, on) => {
    const w = world(on, { answers: { pre_write: DENY('no') } })
    await start($)
    await $.classic.SessionStart({ source: 'compact', session_id: 'sess-lead', cwd: CWD, transcript_path: 't.jsonl' } as never)
    const result = await $.tool.call({ tool: 'Write', file_path: 'a.txt', content: 'x' })
    expect(JSON.stringify(result)).toContain('no')
    expect(w.writes).toEqual([])
  })

  test('from a subagent changes nothing and posts nothing', async ($, on) => {
    const w = world(on, { answers: { pre_write: DENY('no') } })
    await start($)
    const before = w.posts.length
    await $.classic.SessionStart({ source: 'startup', session_id: 'sess-lead', agent_id: 'sub-1', agent_type: 'swarm-coder', cwd: CWD, transcript_path: 't.jsonl' } as never)
    expect(w.posts.length).toBe(before)
    const result = await $.tool.call({ tool: 'Write', file_path: 'a.txt', content: 'x' })
    expect(JSON.stringify(result)).toContain('no')
  })

  test('from another session without a role ends the adoption', async ($, on) => {
    const w = world(on, { answers: { pre_write: DENY('no') } })
    await start($)
    await $.classic.SessionStart({ source: 'startup', session_id: 'sess-other', cwd: CWD, transcript_path: 't.jsonl' } as never)
    await $.tool.call({ tool: 'Write', file_path: 'a.txt', content: 'x' })
    expect(w.writes.length).toBe(1)
  })
})

describe('the ledger server endpoint', () => {
  test('is read once, and again after a failed post finds the server on a new port', async ($, on) => {
    const reads: string[] = []
    const urls: string[] = []
    let port = 47999
    on('fs.read', (_$, e) => {
      const path = hostPath(e.path)
      reads.push(path)
      if (path === `${CWD}/.sentinel-swarm/server.json`) return { value: JSON.stringify({ port }) }
      if (path === `${CWD}/.sentinel-swarm/http-token`) return { value: `${TOKEN}\n` }
      throw new Error(`ENOENT: ${e.path}`)
    })
    on('http.fetch', (_$, e) => {
      urls.push(e.url)
      if (!e.url.includes(`:${port}/`)) throw new Error('connection refused')
      return { value: { status: 200, ok: true, headers: { 'x-sentinel-swarm-repo': encodeURIComponent(CWD) }, text: '' } }
    })
    on('process.run', () => ({ value: { ...RAN_OK, exitCode: 1, stdout: '' } }))
    on('classic.SessionStart', () => ({}))
    on('classic.PreCompact', () => ({}))
    await start($)
    await $.classic.PreCompact({} as never)
    await $.classic.PreCompact({} as never)
    expect(reads.filter(path => path.endsWith('server.json')).length).toBe(1)
    port = 48123
    await $.classic.PreCompact({} as never)
    expect(urls[urls.length - 1]).toBe('http://127.0.0.1:48123/hook/pre_compact')
    expect(reads.filter(path => path.endsWith('server.json')).length).toBe(2)
    await $.classic.PreCompact({} as never)
    expect(reads.filter(path => path.endsWith('server.json')).length).toBe(2)
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

describe("a subagent's call and a gate's deny", () => {
  const CALLS = [
    { event: 'pre_agent', call: { tool: 'Agent', subagent_type: 'general-purpose', prompt: 'x' } },
    { event: 'pre_write', call: { tool: 'Write', file_path: 'a.txt', content: 'x' } },
    { event: 'pre_shell', call: { tool: 'Bash', command: 'ls' } },
    { event: 'pre_shell', call: { tool: 'PowerShell', command: 'ls' } },
    { event: 'pre_monitor', call: { tool: 'Monitor', command: 'sleep 1' } },
    { event: 'pre_send_message', call: { tool: 'SendMessage', to: 'someone', message: 'hi' } },
    { event: 'pre_skill', call: { tool: 'Skill', skill: 'map-explore' } },
    { event: 'pre_ledger', call: { tool: 'mcp__swarm-ledger__run_status' } },
  ]

  for (const { event, call } of CALLS) {
    test(`a deny from the server stops ${call.tool} as { deny }`, async ($, on) => {
      const w = world(on, { answers: { [event]: DENY(`no ${event}`) } })
      await start($, 'swarm-driver', 'sess-driver')
      const result = await $.tool.call(call as never)
      expect(result).toEqual({ deny: `no ${event}` })
      expect(w.events().filter(name => name !== 'session_start')).toEqual([event])
    })

    test(`a subagent's ${call.tool} call posts the loop's id and the parent session, without agent_type`, async ($, on) => {
      const w = world(on, { answers: { [event]: DENY('no') } })
      await start($, 'swarm-driver', 'sess-driver')
      await $.tool.call({ ...call, agentId: 'sub-loop-1' } as never)
      const body = w.posts.find(p => p.event === event)?.body
      expect(body).toMatchObject({ agent_id: 'sub-loop-1', session_id: 'sess-driver', hook_event_name: 'PreToolUse' })
      expect(body).not.toHaveProperty('agent_type')
    })
  }

  test("the main thread's call carries no agent_id and keeps its agent_type", async ($, on) => {
    const w = world(on, { answers: { pre_write: DENY('no') } })
    await start($, 'swarm-driver', 'sess-driver')
    await $.tool.call({ tool: 'Write', file_path: 'a.txt', content: 'x' } as never)
    const body = w.posts.find(p => p.event === 'pre_write')?.body
    expect(body).toMatchObject({ session_id: 'sess-driver', agent_type: 'swarm-driver' })
    expect(body).not.toHaveProperty('agent_id')
  })
})

describe('a permission prompt held for the run panel', () => {
  const ASK = { decision: 'ask', reason: 'Write outside the allowed working directories' } as const
  const OPENED = { request_id: 4, hold_seconds: 300, wait_seconds: 25, expires_at: '2026-10-30T12:05:00.000Z' }
  const OUTSIDE = { file_path: 'C:/elsewhere/x.py', content: 'x' }

  function beneath(on: On, verdict: Record<string, unknown> = ASK) {
    on('tool.check', () => verdict as never)
  }

  async function check($: Engine, tool = 'Write', approver: string | null = 'person', call: string | null = 'toolu_1') {
    return $.tool.check({
      tool,
      input: OUTSIDE,
      ...(approver === null ? {} : { approver }),
      ...(call === null ? {} : { tool_use_id: call }),
    } as never)
  }

  test('an allow from the panel lets the call run', async ($, on) => {
    const w = world(on, { answers: { permission_request: OPENED, permission_wait: { state: 'allowed', note: null } } })
    beneath(on)
    await start($)
    expect(await check($)).toMatchObject({ decision: 'allow', reason: ALLOWED_REASON })
    expect(w.posts.find(p => p.event === 'permission_request')?.body).toMatchObject({
      session_id: 'sess-lead',
      tool_name: 'Write',
      tool_input: OUTSIDE,
      reason: ASK.reason,
    })
    expect(w.posts.find(p => p.event === 'permission_wait')?.body.request_id).toBe(4)
    expect(w.events()).not.toContain('permission_close')
  })

  test('a deny from the panel refuses the call with its note', async ($, on) => {
    world(on, { answers: { permission_request: OPENED, permission_wait: { state: 'denied', note: 'write it under src' } } })
    beneath(on)
    await start($)
    expect(await check($)).toMatchObject({ decision: 'deny', reason: `${DENIED_REASON}: write it under src` })
  })

  test('at the deadline the request expires and Claude Code asks as it would have', async ($, on) => {
    const w = world(on, { answers: { permission_request: OPENED, permission_wait: { state: 'open', note: null } } })
    beneath(on)
    await start($)
    expect(await check($)).toMatchObject(ASK)
    expect(w.events().filter(event => event === 'permission_wait').length).toBe(13)
    expect(w.posts.find(p => p.event === 'permission_close')?.body).toMatchObject({ request_id: 4, state: 'expired' })
  })

  test('a request the ledger closed falls back to the ask', async ($, on) => {
    const w = world(on, { answers: { permission_request: OPENED, permission_wait: { state: 'expired', note: null } } })
    beneath(on)
    await start($)
    expect(await check($)).toMatchObject(ASK)
    expect(w.events().filter(event => event === 'permission_wait').length).toBe(1)
  })

  test('with the ledger unreachable, or no hold, Claude Code asks', async ($, on) => {
    const w = world(on, { serverDown: true })
    beneath(on)
    await start($)
    expect(await check($)).toMatchObject(ASK)
    expect(w.shims.some(argv => argv.includes('permission_request'))).toBe(false)
  })

  test('a ledger that records no request leaves the ask alone', async ($, on) => {
    const w = world(on)
    beneath(on)
    await start($)
    expect(await check($)).toMatchObject(ASK)
    expect(w.events()).toEqual(['session_start', 'permission_request'])
  })

  test("a plugin's ask, a host approver, a query, a question, an allow and a deny are not held", async ($, on) => {
    const verdict: { current: Record<string, unknown> } = { current: { ...ASK, plugin: 'other-plugin' } }
    const w = world(on, { answers: { permission_request: OPENED, permission_wait: { state: 'allowed', note: null } } })
    on('tool.check', () => verdict.current as never)
    await start($)
    expect(await check($)).toMatchObject({ decision: 'ask', plugin: 'other-plugin' })
    verdict.current = ASK
    expect(await check($, 'Write', 'host')).toMatchObject(ASK)
    expect(await check($, 'Write', null)).toMatchObject(ASK)
    expect(await check($, 'Write', 'person', null)).toMatchObject(ASK)
    expect(await check($, 'AskUserQuestion')).toMatchObject(ASK)
    expect(await check($, 'ExitPlanMode')).toMatchObject(ASK)
    verdict.current = { decision: 'deny', reason: 'denied by a rule' }
    expect(await check($)).toMatchObject({ decision: 'deny', reason: 'denied by a rule' })
    verdict.current = { decision: 'allow' }
    expect(await check($)).toMatchObject({ decision: 'allow' })
    expect(w.events()).toEqual(['session_start'])
  })

  test('a session that is not a swarm role is not held', async ($, on) => {
    const w = world(on, { answers: { permission_request: OPENED, permission_wait: { state: 'allowed', note: null } } })
    beneath(on)
    await start($, 'general-purpose', 'sess-user')
    expect(await check($)).toMatchObject(ASK)
    expect(w.posts).toEqual([])
  })

  test('an interrupt withdraws the request and hands back the ask', async () => {
    const closed: [number, string][] = []
    const controller = new AbortController()
    const held = holdForPanel(
      {
        open: async () => OPENED,
        wait: () => new Promise(() => undefined),
        close: async (id, state) => {
          closed.push([id, state])
          return null
        },
      },
      ASK,
      controller.signal,
    )
    controller.abort()
    expect(await held).toEqual(ASK)
    expect(closed).toEqual([[4, 'withdrawn']])
  })

  test('a failing poll expires the request and never allows', async () => {
    const closed: string[] = []
    const verdict = await holdForPanel(
      {
        open: async () => OPENED,
        wait: async () => {
          throw new Error('the ledger server answered 500')
        },
        close: async (_id, state) => {
          closed.push(state)
          return null
        },
      },
      ASK,
      new AbortController().signal,
    )
    expect(verdict).toEqual(ASK)
    expect(closed).toEqual(['expired'])
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

  test('a send refused on an unreadable ~/.claude.json is tried again after a pause', async ($, on) => {
    let tries = 0
    const w = world(on, {
      answers: { owed: { wakeups: [OWED.wakeups[0]] } },
      send: () => (++tries < 2 ? { isDelivered: false, reason: 'JSON Parse error: Unexpected EOF' } : { isDelivered: true }),
    })
    await start($)
    const stopped = $.classic.Stop({ stop_hook_active: false } as never)
    await w.clock.advance(2_000)
    await stopped
    expect(w.sends.length).toBe(2)
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

  test('a send that lands after the wait is marked sent then, and is not sent twice', async ($, on) => {
    let land: (sent: SessionSendResult) => void = () => undefined
    const w = world(on, {
      answers: { owed: { wakeups: [OWED.wakeups[0]] } },
      send: () => new Promise<SessionSendResult>(resolve => (land = resolve)),
    })
    await start($)
    const stopped = $.classic.Stop({ stop_hook_active: false } as never)
    await w.clock.advance(15_000)
    await stopped
    expect(w.events()).not.toContain('wake_sent')
    await $.classic.Stop({ stop_hook_active: false } as never)
    expect(w.sends.length).toBe(1)
    land({ isDelivered: true })
    await w.clock.settle()
    expect(w.posts.find(p => p.event === 'wake_sent')?.body.wakeup_ids).toEqual([3])
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

  test('an idle session the ledger asks to poll keeps polling with no test run, and delivers a wake-up that arrives later', async ($, on) => {
    const answers: Record<string, unknown> = { self_owed: { text: null, running: 0, poll: true } }
    const w = world(on, { answers })
    const prompts: string[] = []
    on('prompt.submit', (_$, e) => {
      prompts.push(e.text)
      return { text: e.text }
    })
    await start($)
    await w.clock.advance(15_000)
    await w.clock.advance(15_000)
    expect(w.events().filter(event => event === 'self_owed').length).toBe(2)
    answers.self_owed = { text: 'The run resumed. Read directive_inbox.', running: 0, poll: true }
    await w.clock.advance(15_000)
    expect(prompts).toEqual(['The run resumed. Read directive_inbox.'])
    answers.self_owed = { text: null, running: 0, poll: false }
    await w.clock.advance(15_000)
    await w.clock.advance(15_000)
    expect(w.events().filter(event => event === 'self_owed').length).toBe(4)
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
      if (hostPath(e.path) === `${CWD}/.sentinel-swarm/server.json`) return { value: JSON.stringify({ port: 47999 }) }
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
    const urls = server(on, { status: 200, body: { name: 'swarm-ledger', repo_root: '\\host\\', pid: 1 } })
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
    expect(launched.find(argv => argv.includes('panel'))?.slice(-3)).toEqual(['panel', '--repo', 'C:/fresh'])
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
  return { id: 'file_id:1', name: 'a.py', path: null, state: 'claimed', status: 'active', owners: [], summary: null, has_brief: false, brief: null, review: null, ...overrides }
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
    request: null,
    recent: null,
    children: [],
    ...overrides,
  }
}

const WRITE_REQUEST: PanelRequest = { kind: 'tool', tool: 'Write', input: '.claude/scratch/x.py' }

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
    agents: [{ ...oracle, children: [agent({ request: WRITE_REQUEST })] }],
    counts: { live: 2 },
    waiting: { questions: [], findings: [], paused: null, permissions: [] },
    ...overrides,
  }
}

const HELD: PanelPermission = {
  request_id: 4,
  agent: 'coder-a',
  tool: 'Write',
  summary: 'C:/elsewhere/x.py',
  reason: 'Write outside the allowed working directories',
  asked_at: '2026-10-30T12:00:00.000Z',
  expires_at: '2026-10-30T12:05:00.000Z',
}

const SESSION_WAIT: PanelSessionWait = { sessionId: 'sess-coder', name: 'host-r3-coder-a', waitingFor: 'permission prompt' }

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

describe('the phases the Oracle has not started', () => {
  const phases = (count: number) => Array.from({ length: count }, (_, i) => task({ id: `phase_id:${i + 1}`, name: `p${i + 1}-x`, state: 'planned', status: 'waiting' }))
  const oracleWith = (count: number) => agent({ agent_id: 'sess-oracle', name: 'oracle', role: 'oracle', tasks: phases(count) })
  const closed = () => false

  test('sit under one group row that starts closed for a long list', () => {
    const rows = flatten([oracleWith(9)], false, 'run:3', (key, fallback) => fallback)
    expect(rows.map(row => row.kind)).toEqual(['agent', 'group'])
    expect(rows[1]).toMatchObject({ kind: 'group', key: 'run:3/oracle/phases', label: 'phases not started', count: 9, open: false })
  })

  test('start open for a short list, and the group key closes them', () => {
    const open = flatten([oracleWith(3)], false, 'run:3', (key, fallback) => fallback)
    expect(open.map(row => row.kind)).toEqual(['agent', 'group', 'task', 'task', 'task'])
    expect(flatten([oracleWith(3)], false, 'run:3', closed).map(row => row.kind)).toEqual(['agent', 'group'])
  })
})

describe('the agent detail card', () => {
  const NOW = Date.parse('2026-11-14T10:10:00Z')

  test('lists what the agent is, what it does, and how to open its session', () => {
    const facts = agentFacts(agent({ activity: 'tests_run', heartbeat_at: '2026-11-14T10:09:30Z', file: 'src/a.ts', unread: 2 }), NOW)
    expect(facts).toEqual([
      'coder · sonnet',
      'ledger working · session busy (host-r3-coder-a)',
      'doing: tests_run · last heartbeat 30s ago',
      'works on src/a.ts',
      '2 unread · 0 wake-up(s) owed',
    ])
  })

  test('shows the latest steps with their age', () => {
    expect(stepLines([{ at: '2026-11-14T10:05:00Z', kind: 'error', text: '1 failed' }], NOW)).toEqual(['✕ 1 failed  · 5m 0s ago'])
    expect(stepLines(null, NOW)).toEqual(['Reading its transcript...'])
    expect(stepLines([], NOW)).toEqual(['No steps recorded in its transcript yet.'])
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
      { sessionId: 'd', name: 'host-oracle-2', kind: 'background', status: 'idle', state: 'blocked' },
      { sessionId: 'e', name: 'idle-one', status: 'idle', state: 'working' },
    ]
    expect(parseWaiting(JSON.stringify(listing))).toEqual([
      { sessionId: 'a', name: 'host-r3-coder-a', waitingFor: 'permission prompt' },
      { sessionId: 'b', name: 'host-oracle', waitingFor: 'input needed' },
      { sessionId: 'd', name: 'host-oracle-2', waitingFor: 'blocked' },
    ])
    expect(parseWaiting('not json')).toEqual([])
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

  test('a waiting or blocked session with no pending request is not listed', () => {
    const base = runView()
    const oracle = base.agents[0] as PanelAgent
    const view = { ...base, agents: [{ ...oracle, children: [agent({ request: null })] }] }
    expect(attention(view, [SESSION_WAIT, { ...SESSION_WAIT, waitingFor: 'blocked' }])).toEqual([])
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
        permissions: [],
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
      waiting: { questions: [], findings: [{ finding_id: 2, kind: 'needs_user', agent: 'coder-a', detail: 'waits', since: 'T' }], paused: null, permissions: [] },
    })
    expect(attention(view, [SESSION_WAIT]).map(i => i.key)).toEqual(['wait:3:coder-a'])
  })

  test('a held permission request sits under its agent with Allow and Deny', () => {
    const view = runView({
      waiting: {
        questions: [],
        findings: [{ finding_id: 2, kind: 'needs_user', agent: 'coder-a', detail: 'waits on a held request', since: 'T' }],
        paused: null,
        permissions: [HELD],
      },
    })
    const items = attention(view, [])
    expect(items.map(i => i.key)).toEqual(['permission:3:4'])
    expect(items[0]).toMatchObject({
      agent: 'coder-a',
      title: 'asks to run Write (permission request 4)',
      lines: ['Write: C:/elsewhere/x.py', 'Write outside the allowed working directories'],
      directiveId: null,
      permissionId: 4,
    })
    expect(items[0]?.where).toContain('Allow or Deny it here')
    expect(items[0]?.where).toContain('agent view')
  })

  test('an ended run waits on nobody', () => {
    const view = runView({
      run: { ...runView().run, ended_at: '2026-10-28T02:00:00Z' },
      waiting: { questions: [{ directive_id: 7, question: '?', asked_at: 'T' }], findings: [], paused: null, permissions: [] },
    })
    expect(attention(view, [SESSION_WAIT])).toEqual([])
  })
})

describe('your directives', () => {
  const NOW = Date.parse('2026-11-14T10:10:00Z')
  const sent = (overrides: Partial<PanelDirective> = {}): PanelDirective => ({
    directive_id: 7,
    source: 'user_chat',
    sender: 'alex',
    body: 'Scope it to phase 1.',
    status: 'unread',
    at: '2026-11-14T10:05:00Z',
    note: null,
    outcome: null,
    ...overrides,
  })

  test('each line names the state, its age, the note, and the start of the text', () => {
    expect(directiveLine(sent(), NOW)).toBe('#7 unread, sent 5m 0s ago · Scope it to phase 1.')
    expect(directiveLine(sent({ status: 'seen', at: '2026-11-14T10:08:00Z' }), NOW)).toBe('#7 seen 2m 0s ago · Scope it to phase 1.')
    expect(directiveLine(sent({ status: 'queued', note: 'after the review' }), NOW)).toBe('#7 queued 5m 0s ago: after the review · Scope it to phase 1.')
    expect(directiveLine(sent({ status: 'resolved', outcome: 'applied', note: 'Updated phase 1.' }), NOW)).toBe('#7 applied 5m 0s ago: Updated phase 1. · Scope it to phase 1.')
    expect(directiveLine(sent({ status: 'resolved' }), NOW)).toContain('#7 answered')
    expect(directiveLine(sent({ status: 'needs_user', note: 'Which branch?' }), NOW)).toContain('waits on your answer: Which branch?')
    expect(directiveLine(sent({ body: 'x'.repeat(80) }), NOW)).toContain(`${'x'.repeat(39)}…`)
  })

  test('the panel lists them under the run', async ($, on) => {
    const at = new Date(Date.now() - 300_000).toISOString()
    const view = runView({ directives: [sent({ at }), sent({ directive_id: 8, status: 'queued', note: 'after the review', at })] })
    const snapshot: PanelSnapshot = { repo: CWD, generated_at: '', runs: [view] }
    const clock = mock.clock(on, { now: 1_000 })
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.panes', () => ({ value: [] }))
    on('process.run', (_$, e) => ({ value: { ...RAN_OK, exitCode: 0, stdout: e.argv[0] === 'claude' ? '[]' : JSON.stringify(snapshot) } }))
    await $.tool.call({ tool: 'mcp__sentinel-swarm__open_panel', repo: CWD } as never)
    await clock.settle()

    const ui = await $.ui.mount({ plugin: 'sentinel-swarm', component: 'Pane', requestId: 'swarm-run', surface: 'terminal', props: { title: 'Swarm', isFocused: false, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} } } as never)
    expect(await ui.find({ type: 'Text', text: /Your directives/ })).toBeDefined()
    expect((await ui.find({ key: 'directive:3:7' }))?.text).toMatch(/#7 unread, sent 5m \d+s ago/)
    expect((await ui.find({ key: 'directive:3:8' }))?.text).toMatch(/#8 queued 5m \d+s ago: after the review/)
    await ui.unmount()
  })
})

describe('the panel draws what waits on you', () => {
  const PANE_PROPS = { title: 'Swarm', isFocused: false, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} } as const
  const LISTING = JSON.stringify([{ sessionId: 'sess-coder', name: 'host-r3-coder-a', status: 'waiting', waitingFor: 'permission prompt' }])

  test('the top section and the red box show the request, and an answer goes back as a directive reply', async ($, on) => {
    const launched: string[][] = []
    const toasts: string[] = []
    const base = runView({ waiting: { questions: [{ directive_id: 7, question: 'Which branch?', asked_at: 'T' }], findings: [], paused: null, permissions: [] } })
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
    expect((await ui.find({ key: 'question:3:7' }))?.text).toContain('The Oracle reads it at its next step.')
    const asked = launched.filter(argv => argv.includes('panel')).pop() ?? []
    expect(asked).toContain('--waiting=sess-coder')
    expect(asked).toContain('--waiting=host-r3-coder-a')
    await ui.unmount()
  })

  test('a send that fails says so in the box and keeps the answer', async ($, on) => {
    const view = runView({ waiting: { questions: [{ directive_id: 7, question: 'Which branch?', asked_at: 'T' }], findings: [], paused: null, permissions: [] } })
    const snapshot: PanelSnapshot = { repo: CWD, generated_at: '', runs: [view] }
    const clock = mock.clock(on, { now: 1_000 })
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.panes', () => ({ value: [] }))
    on('ui.toast', () => ({ value: undefined }))
    on('process.run', (_$, e) => {
      if (e.argv.includes('directive')) return { value: { ...RAN_OK, exitCode: 1, stdout: '', stderr: 'cannot submit the directive: no active run\n' } }
      return { value: { ...RAN_OK, exitCode: 0, stdout: e.argv[0] === 'claude' ? '[]' : JSON.stringify(snapshot) } }
    })
    await $.tool.call({ tool: 'mcp__sentinel-swarm__open_panel', repo: CWD } as never)
    await clock.settle()

    const ui = await $.ui.mount({ plugin: 'sentinel-swarm', component: 'Pane', requestId: 'swarm-run', surface: 'terminal', props: PANE_PROPS } as never)
    await (ui as unknown as { input: (target: { key: string; text: string }) => Promise<unknown> }).input({ key: 'answer:question:3:7', text: 'use main' })
    await clock.settle()
    const box = await ui.find({ key: 'question:3:7' })
    expect(box?.text).toContain('Not sent')
    expect(box?.text).toContain('no active run')
    expect((await ui.find({ key: 'answer:question:3:7' }))?.props.value).toBe('use main')
    await ui.unmount()
  })

  test('clicking an agent opens its detail card, which reads its recent steps', async ($, on) => {
    const launched: string[][] = []
    const steps = [{ at: null, kind: 'tool', text: 'Bash npx vitest run src/a.test.ts' }]
    const plain = runView()
    const oracle = plain.agents[0] as PanelAgent
    const detailed = { ...plain, agents: [{ ...oracle, children: [agent({ recent: steps as PanelAgent['recent'] })] }] }
    const clock = mock.clock(on, { now: 1_000 })
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.panes', () => ({ value: [] }))
    on('process.run', (_$, e) => {
      launched.push([...e.argv])
      const view = e.argv.includes('--detail=sess-coder') ? detailed : plain
      const stdout = e.argv[0] === 'claude' ? '[]' : JSON.stringify({ repo: CWD, generated_at: '', runs: [view] })
      return { value: { ...RAN_OK, exitCode: 0, stdout } }
    })
    await $.tool.call({ tool: 'mcp__sentinel-swarm__open_panel', repo: CWD } as never)
    await clock.settle()

    const ui = await $.ui.mount({ plugin: 'sentinel-swarm', component: 'Pane', requestId: 'swarm-run', surface: 'terminal', props: PANE_PROPS } as never)
    expect(await ui.find({ key: 'run:3/oracle/coder-a:card' })).toBeUndefined()
    await ui.press({ key: 'run:3/oracle/coder-a' })
    await clock.settle()
    const card = await ui.find({ key: 'run:3/oracle/coder-a:card' })
    expect(card?.text).toContain('ledger working · session busy (host-r3-coder-a)')
    expect(card?.text).toContain('Bash npx vitest run src/a.test.ts')
    expect(card?.text).toContain('run claude agents and open host-r3-coder-a')
    expect(launched.some(argv => argv.includes('--detail=sess-coder'))).toBe(true)

    await ui.press({ key: 'run:3/oracle/coder-a:card-close' })
    await clock.settle()
    expect(await ui.find({ key: 'run:3/oracle/coder-a:card' })).toBeUndefined()
    await ui.unmount()
  })

  test('Allow and Deny answer a held permission request through the launcher', async ($, on) => {
    const launched: string[][] = []
    const toasts: string[] = []
    const view = runView({ waiting: { questions: [], findings: [], paused: null, permissions: [HELD] } })
    const snapshot: PanelSnapshot = { repo: CWD, generated_at: '', runs: [view] }
    const clock = mock.clock(on, { now: 1_000 })
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.panes', () => ({ value: [] }))
    on('ui.toast', (_$, e) => {
      toasts.push(String((e as { text?: unknown }).text ?? ''))
      return { value: undefined }
    })
    on('fs.read', (_$, e) => {
      throw new Error(`ENOENT: ${e.path}`)
    })
    on('process.run', (_$, e) => {
      launched.push([...e.argv])
      const refused = e.argv.includes('--deny')
      const stdout = e.argv[0] === 'claude' ? '[]' : e.argv.includes('permission') ? '{}' : JSON.stringify(snapshot)
      return { value: { ...RAN_OK, exitCode: refused ? 1 : 0, stdout, stderr: refused ? 'cannot answer the permission request: permission request 4 is allowed, not open\n' : '' } }
    })
    await $.tool.call({ tool: 'mcp__sentinel-swarm__open_panel', repo: CWD } as never)
    await clock.settle()

    const ui = await $.ui.mount({ plugin: 'sentinel-swarm', component: 'Pane', requestId: 'swarm-run', surface: 'terminal', props: PANE_PROPS } as never)
    const box = await ui.find({ key: 'permission:3:4' })
    expect(box?.props.borderColor).toBe('error')
    expect(box?.text).toContain('Write: C:/elsewhere/x.py')
    await ui.press({ key: 'allow:permission:3:4' })
    await clock.settle()
    const allowed = launched.find(argv => argv.includes('permission')) ?? []
    expect(allowed.slice(allowed.indexOf('permission'))).toEqual(['permission', '--repo', CWD, '--id', '4', '--allow'])
    expect(toasts).toEqual(['Allowed permission request 4.'])

    await ui.press({ key: 'deny:permission:3:4' })
    await clock.settle()
    expect(launched.some(argv => argv.includes('--deny'))).toBe(true)
    expect(toasts[1]).toContain('Permission request 4 was not answered')
    expect(toasts[1]).toContain('not open')
    await ui.unmount()
  })

  const TOKEN_FILE = `${CWD}/.sentinel-swarm/http-token`
  const SERVER_FILE = `${CWD}/.sentinel-swarm/server.json`
  const ROUTE = 'http://127.0.0.1:47999/panel'
  const served = (text: string, headers: Record<string, string> = { 'x-sentinel-swarm-repo': encodeURIComponent(CWD) }) => ({
    value: { status: 200, ok: true, headers, text },
  })

  type Launch = (argv: readonly string[]) => { exitCode: number; stdout: string }

  function withServer(
    on: On,
    answer: (url: string) => ReturnType<typeof served> | { value: { status: number; ok: boolean; headers: Record<string, string>; text: string } },
    launch: Launch = () => ({ exitCode: 1, stdout: '' }),
  ) {
    const urls: string[] = []
    const headers: Record<string, string>[] = []
    const launched: string[][] = []
    const clock = mock.clock(on, { now: 1_000 })
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.panes', () => ({ value: [] }))
    on('ui.toast', () => ({ value: undefined }))
    on('fs.read', (_$, e) => {
      const path = hostPath(e.path)
      if (path === SERVER_FILE) return { value: JSON.stringify({ port: 47999 }) }
      if (path === TOKEN_FILE) return { value: `${TOKEN}\n` }
      throw new Error(`ENOENT: ${e.path}`)
    })
    on('http.fetch', (_$, e) => {
      urls.push(e.url)
      headers.push((e.init?.headers ?? {}) as Record<string, string>)
      if (e.url.endsWith('/health')) return served(JSON.stringify({ name: 'swarm-ledger', repo_root: CWD }), {})
      return answer(e.url)
    })
    on('process.run', (_$, e) => {
      launched.push([...e.argv])
      return { value: { ...RAN_OK, ...launch(e.argv) } }
    })
    return { clock, urls, headers, launched }
  }

  test('reads the snapshot over the ledger server route and keeps the launcher as the fallback', async ($, on) => {
    const snapshot: PanelSnapshot = { repo: CWD, generated_at: '', runs: [runView()] }
    let routeAnswers = true
    const { clock, urls, headers, launched } = withServer(
      on,
      () => (routeAnswers ? served(JSON.stringify(snapshot)) : { value: { status: 404, ok: false, headers: {}, text: 'Not Found' } }),
      argv => ({ exitCode: 0, stdout: argv[0] === 'claude' ? '[]' : JSON.stringify(snapshot) }),
    )
    await $.tool.call({ tool: 'mcp__sentinel-swarm__open_panel', repo: CWD } as never)
    await clock.settle()

    expect(urls).toContain(ROUTE)
    expect(headers[urls.indexOf(ROUTE)]).toMatchObject({ Authorization: `Bearer ${TOKEN}`, 'X-Sentinel-Swarm-Repo': encodeURIComponent(CWD) })
    expect(launched.some(argv => argv.includes('panel'))).toBe(false)

    routeAnswers = false
    const ui = await $.ui.mount({ plugin: 'sentinel-swarm', component: 'Pane', requestId: 'swarm-run', surface: 'terminal', props: PANE_PROPS } as never)
    await ui.press({ key: 'refresh' })
    await clock.settle()
    expect(launched.some(argv => argv.includes('panel'))).toBe(true)
    expect(await ui.find({ type: 'Text', text: /Run 3/ })).toBeDefined()
    await ui.unmount()
  })

  test('a task brief is fetched when its row opens, and only then', async ($, on) => {
    const plain = runView()
    const oracle = plain.agents[0] as PanelAgent
    const withBrief = (brief: string | null) => ({
      ...plain,
      agents: [{ ...oracle, children: [agent({ task: task({ has_brief: true, brief }) })] }],
    })
    const { clock, urls } = withServer(on, url =>
      served(JSON.stringify({ repo: CWD, generated_at: '', runs: [withBrief(url.includes('brief=file_id%3A1') ? 'Write the parser.' : null)] })),
    )
    await $.tool.call({ tool: 'mcp__sentinel-swarm__open_panel', repo: CWD } as never)
    await clock.settle()
    expect(urls.filter(url => url.startsWith(ROUTE)).every(url => !url.includes('brief='))).toBe(true)

    const ui = await $.ui.mount({ plugin: 'sentinel-swarm', component: 'Pane', requestId: 'swarm-run', surface: 'terminal', props: PANE_PROPS } as never)
    expect(await ui.find({ key: 'run:3/oracle/coder-a#task:0' })).toBeUndefined()
    await ui.press({ key: 'run:3/oracle/coder-a#task' })
    await clock.settle()
    expect(urls.some(url => url.startsWith(ROUTE) && url.includes('brief=file_id%3A1'))).toBe(true)
    expect((await ui.find({ key: 'run:3/oracle/coder-a#task:0' }))?.text.replace(/\s+/g, ' ')).toContain('Write the parser.')

    await ui.press({ key: 'run:3/oracle/coder-a#task' })
    await clock.settle()
    const before = urls.length
    await ui.press({ key: 'refresh' })
    await clock.settle()
    expect(urls.slice(before).filter(url => url.startsWith(ROUTE)).every(url => !url.includes('brief='))).toBe(true)
    expect(await ui.find({ key: 'run:3/oracle/coder-a#task:0' })).toBeUndefined()
    await ui.unmount()
  })

  test('a task whose brief has not arrived says so, and one with no brief says none', () => {
    const said = (lines: ReturnType<typeof taskDetail>) => lines.map(line => line.map(span => span.text).join('')).join(' ').replace(/\s+/g, ' ')
    expect(said(taskDetail(task({ has_brief: true, brief: null }), 40))).toContain('Reading the brief...')
    expect(said(taskDetail(task({ has_brief: false, brief: null }), 40))).toContain('No brief recorded.')
  })

  const QUESTION = { questions: [{ directive_id: 7, question: 'Which branch?', asked_at: 'T' }], findings: [], paused: null, permissions: [] }

  async function askedPanel($: Engine, on: On) {
    const snapshot: PanelSnapshot = { repo: CWD, generated_at: '', runs: [runView({ waiting: QUESTION })] }
    const toasts: string[] = []
    const clock = mock.clock(on, { now: 1_000 })
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.panes', () => ({ value: [] }))
    on('ui.toast', (_$, e) => {
      toasts.push(String((e as { text?: unknown }).text ?? ''))
      return { value: undefined }
    })
    on('process.run', (_$, e) => ({ value: { ...RAN_OK, exitCode: 0, stdout: e.argv[0] === 'claude' ? '[]' : JSON.stringify(snapshot) } }))
    await $.tool.call({ tool: 'mcp__sentinel-swarm__open_panel', repo: CWD } as never)
    await clock.settle()
    const ui = await $.ui.mount({ plugin: 'sentinel-swarm', component: 'Pane', requestId: 'swarm-run', surface: 'terminal', props: PANE_PROPS } as never)
    return { ui, clock, toasts }
  }

  test('the answer field takes the focus first, and a press on the question asks for it', async ($, on) => {
    const { ui, clock, toasts } = await askedPanel($, on)
    expect((await ui.find({ key: 'answer:question:3:7' }))?.props.autoFocus).toBe(true)
    expect((await ui.find({ key: 'ask:question:3:7:0' }))?.text).toContain('Which branch?')
    await ui.press({ key: 'ask:question:3:7:0' })
    await clock.settle()
    expect(toasts).toHaveLength(1)
    expect(toasts[0]).toContain('The answer field did not take focus: ')
    await ui.unmount()
  })

  test('a focus the pane cannot take is shown in the panel, not dropped', async ($, on) => {
    const { ui, clock } = await askedPanel($, on)
    await ui.press({ key: 'ask:question:3:7' })
    await clock.settle()
    expect((await ui.find({ type: 'Text', text: /did not take focus/ }))?.props.color).toBe('error')
    await ui.unmount()
  })

  test('an empty answer says it was not sent', async ($, on) => {
    const { ui, clock } = await askedPanel($, on)
    await (ui as unknown as { input: (target: { key: string; text: string }) => Promise<unknown> }).input({ key: 'answer:question:3:7', text: '   ' })
    await clock.settle()
    expect((await ui.find({ key: 'question:3:7' }))?.text).toContain('Not sent: the answer is empty.')
    await ui.unmount()
  })

  test('a failed read shows its reason at the top of the panel', async ($, on) => {
    const clock = mock.clock(on, { now: 1_000 })
    on('ui.open', () => ({ value: { isPlaced: true } }))
    on('ui.panes', () => ({ value: [] }))
    on('fs.read', (_$, e) => {
      throw new Error(`ENOENT: ${e.path}`)
    })
    on('process.run', () => ({ value: { ...RAN_OK, exitCode: 1, stdout: '', stderr: 'no ledger reader\n' } }))
    await $.tool.call({ tool: 'mcp__sentinel-swarm__open_panel', repo: CWD } as never)
    await clock.settle()
    const ui = await $.ui.mount({ plugin: 'sentinel-swarm', component: 'Pane', requestId: 'swarm-run', surface: 'terminal', props: PANE_PROPS } as never)
    expect((await ui.find({ type: 'Text', text: /no ledger reader/ }))?.props.color).toBe('error')
    await ui.unmount()
  })
})

