import type { EngineInterface, Register, SessionSendAddress } from 'claude-code'

import type { SentinelSwarmSession } from '../types'

type Answer = Record<string, unknown> | null
type Owed = {
  session_id: string
  text: string
  wakeup_ids: number[]
}

const ROLE_TYPE = /^swarm-(oracle|manager|lead|coder|driver)$/
const LEDGER_COMMAND_HOOK = /hook\.py hook \w/
const LEDGER_TOOL = /^mcp__swarm-ledger__/
const TOKEN = /^[A-Za-z0-9_-]{32,128}$/
const REPO_HEADER = 'X-Sentinel-Swarm-Repo'
const TRANSPORT_KEY = 'sentinel_swarm_transport'
const FIRED_AT_KEY = 'sentinel_swarm_fired_at'
const RECORDS = '.sentinel-swarm'
const SHIM = `${RECORDS}/hook.py`
const PYTHONS = ['python3', 'python']
const FAST_MS = 10_000
const SLOW_MS: Readonly<Record<string, number>> = { stop: 40_000, session_end: 40_000 }
const SHIM_MS = 50_000
const SEND_MS = 15_000
const SEND_RETRY_MS = [2_000, 4_000, 8_000]
const NOT_YET_LIVE = /no live session/i
const WRITE_TOOLS = ['Write', 'Edit', 'MultiEdit', 'NotebookEdit']
const WRITE_TOOL = /^(Write|Edit|MultiEdit|NotebookEdit)$/
const SHELL_TOOL = /^(Bash|PowerShell)$/
// Must equal SYNC_POST_TOOLS in mcp/src/swarm_ledger/hooks/events.py.
const SYNC_POST_TOOLS = ['SendMessage', 'PushNotification', 'Monitor', ...WRITE_TOOLS]
const RECEIVED_FROM = ['peer', 'peer-send-message', 'coordinator']
const sessionRef = { plugin: 'sentinel-swarm', key: 'session' } as const

let session: SentinelSwarmSession | null | undefined
const sending = new Set<number>()
const paying = new Set<Promise<void>>()
const sendingTexts = new Map<string, number>()

export const register: Register = on => {
  on('classic.SessionStart', async ($, e, next) => {
    const s = await adopt($, e)
    if (s === null) return next(e)
    const answer = await runHook($, s, 'session_start', { ...e, [TRANSPORT_KEY]: 'mod' }).catch(() => null)
    const result = await next(e)
    const text = field(answer, 'additionalContext')
    return text === undefined ? result : { ...result, additionalContext: [...(result.additionalContext ?? []), text] }
  })

  on('tool.call', { tool: 'Agent' }, ($, e, next) => judge($, e, next, 'pre_agent')).catch(($, e, next) =>
    next.called ? next(e) : { deny: cannotCheck(next.error) },
  )
  on('tool.call', { tool: WRITE_TOOL }, ($, e, next) => judge($, e, next, 'pre_write')).catch(($, e, next) =>
    next.called ? next(e) : { deny: cannotCheck(next.error) },
  )
  on('tool.call', { tool: SHELL_TOOL }, ($, e, next) => judge($, e, next, 'pre_shell')).catch(($, e, next) =>
    next.called ? next(e) : { deny: cannotCheck(next.error) },
  )
  on('tool.call', { tool: 'Monitor' }, ($, e, next) => judge($, e, next, 'pre_monitor')).catch(($, e, next) =>
    next.called ? next(e) : { deny: cannotCheck(next.error) },
  )
  // $.session.send runs as a SendMessage call to the target's pipe address, not its session id;
  // the ledger checked the recipient before the mod sent, so the mod's own text passes.
  on('tool.call', { tool: 'SendMessage' }, ($, e, next) =>
    isOwnSend(e) ? next(e) : judge($, e, next, 'pre_send_message'),
  ).catch(
    ($, e, next) => (next.called ? next(e) : { deny: cannotCheck(next.error) }),
  )
  on('tool.call', { tool: 'Skill' }, ($, e, next) => judge($, e, next, 'pre_skill', 'driver')).catch(
    ($, e, next) => (next.called ? next(e) : { deny: cannotCheck(next.error) }),
  )
  on('tool.call', { tool: LEDGER_TOOL }, ($, e, next) => judge($, e, next, 'pre_ledger')).catch(($, e, next) =>
    next.called ? next(e) : { deny: cannotCheck(next.error) },
  )

  on('classic.PostToolUse', async ($, e, next) => {
    const s = await active($)
    if (s === null) return next(e)
    const payload = { ...e, [TRANSPORT_KEY]: 'mod' }
    if (SYNC_POST_TOOLS.includes(e.tool_name)) {
      await runHook($, s, 'post_any', payload).catch(() => null)
    } else {
      void runHook($, s, 'post_activity', { ...payload, [FIRED_AT_KEY]: new Date().toISOString() }).catch(() => null)
    }
    if (s.role === 'coder' && SHELL_TOOL.test(e.tool_name)) {
      await runHook($, s, 'post_shell', payload).catch(() => null)
    }
    if (LEDGER_TOOL.test(e.tool_name)) void pay($, s)
    return next(e)
  })

  on('classic.PreCompact', async ($, e, next) => {
    const s = await active($)
    if (s !== null) await runHook($, s, 'pre_compact', { ...e, [TRANSPORT_KEY]: 'mod' }).catch(() => null)
    return next(e)
  })

  on('classic.Stop', async ($, e, next) => {
    const s = await active($)
    if (s === null) return next(e)
    await Promise.all(paying)
    await pay($, s)
    const answer = await runHook($, s, 'stop', { ...e, [TRANSPORT_KEY]: 'mod' }).catch(() => null)
    const result = await next(e)
    const reason = isRecord(answer) && answer.decision === 'block' ? String(answer.reason ?? '') : undefined
    return reason ? { ...result, block: reason } : result
  })

  on('classic.SessionEnd', async ($, e, next) => {
    const s = await active($)
    if (s !== null) await runHook($, s, 'session_end', { ...e, [TRANSPORT_KEY]: 'mod' }).catch(() => null)
    return next(e)
  })

  on('session.receive', async ($, e, next) => {
    const s = await active($)
    if (s === null || e.agentId !== undefined || !RECEIVED_FROM.includes(e.origin.kind)) return next(e)
    const taken = await post($, s, 'inbox_take', base(s)).catch(() => null)
    const claim = isRecord(taken) && typeof taken.claim === 'string' ? taken.claim : null
    if (claim === null) return next(e)
    let result: Awaited<ReturnType<typeof next>>
    try {
      result = await next({ ...e, text: withInbox(e.text, taken) })
    } catch (err) {
      await post($, s, 'inbox_release', { ...base(s), claim }).catch(() => null)
      throw err
    }
    const settle = result.consumed === undefined ? 'inbox_ack' : 'inbox_release'
    await post($, s, settle, { ...base(s), claim }).catch(() => null)
    return result
  }).catch(($, e, next) => next(e))
}

async function judge<E extends object, R>(
  $: EngineInterface,
  e: E,
  next: (e: E) => Promise<R>,
  event: string,
  role?: string,
): Promise<R | { deny: string }> {
  const s = await active($)
  if (s === null || (role !== undefined && s.role !== role)) return next(e)
  const { tool, tool_use_id, agentId, ...input } = e as Record<string, unknown>
  const payload = {
    ...base(s),
    ...(typeof agentId === 'string' ? { agent_id: agentId, agent_type: undefined } : {}),
    hook_event_name: 'PreToolUse',
    tool_name: tool,
    tool_input: input,
    tool_use_id,
  }
  let answer: Answer
  try {
    answer = await runHook($, s, event, payload)
  } catch (err) {
    return { deny: cannotCheck(err) }
  }
  const reason = field(answer, 'permissionDecisionReason', 'deny')
  if (reason !== undefined) return { deny: reason }
  const updated = hookOutput(answer)?.updatedInput
  if (!isRecord(updated)) return next(e)
  return next({ tool, tool_use_id, ...updated } as E)
}

async function adopt(
  $: EngineInterface,
  e: { session_id: string; transcript_path: string; cwd: string; agent_type?: string; agent_id?: string },
): Promise<SentinelSwarmSession | null> {
  const role = ROLE_TYPE.exec(e.agent_type ?? '')?.[1]
  let found: SentinelSwarmSession | null = null
  if (role !== undefined && e.agent_id === undefined) {
    const file = await $.fs.read(`${e.cwd}/.claude/agents/${e.agent_type}.md`).catch(() => '')
    if (!LEDGER_COMMAND_HOOK.test(frontmatter(file))) {
      found = { sessionId: e.session_id, agentType: e.agent_type ?? '', role, cwd: e.cwd, transcriptPath: e.transcript_path }
    }
  }
  session = found
  await $.state.set(sessionRef, found).catch(() => undefined)
  return found
}

async function active($: EngineInterface): Promise<SentinelSwarmSession | null> {
  if (session === undefined) {
    const held = await $.state.get(sessionRef).catch(() => undefined)
    session = held?.value ?? null
  }
  return session
}

function frontmatter(text: string): string {
  const lines = text.split(/\r?\n/)
  if (lines[0]?.trim() !== '---') return ''
  const end = lines.indexOf('---', 1)
  return end === -1 ? '' : lines.slice(1, end).join('\n')
}

function base(s: SentinelSwarmSession): Record<string, unknown> {
  return {
    session_id: s.sessionId,
    transcript_path: s.transcriptPath,
    cwd: s.cwd,
    agent_type: s.agentType,
    [TRANSPORT_KEY]: 'mod',
  }
}

async function runHook($: EngineInterface, s: SentinelSwarmSession, event: string, payload: Record<string, unknown>): Promise<Answer> {
  try {
    return await post($, s, event, payload)
  } catch {
    return viaShim($, s, event, payload)
  }
}

async function post($: EngineInterface, s: SentinelSwarmSession, event: string, payload: Record<string, unknown>): Promise<Answer> {
  const [port, token] = await Promise.all([serverPort($, s.cwd), serverToken($, s.cwd)])
  const answer = await within(
    $,
    SLOW_MS[event] ?? FAST_MS,
    $.http.fetch(`http://127.0.0.1:${port}/hook/${event}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
        [REPO_HEADER]: encodeURIComponent(s.cwd),
      },
      body: JSON.stringify(payload),
    }),
  )
  if (answer.status !== 200 || !(REPO_HEADER.toLowerCase() in answer.headers)) {
    throw new Error(`the ledger server answered ${answer.status}`)
  }
  return parse(answer.text)
}

async function viaShim($: EngineInterface, s: SentinelSwarmSession, event: string, payload: Record<string, unknown>): Promise<Answer> {
  let reason = 'no Python interpreter ran the shim'
  for (const python of PYTHONS) {
    const ran = await $.process
      .run([python, SHIM, 'hook', event], { cwd: s.cwd, stdin: JSON.stringify(payload), timeoutMs: SHIM_MS })
      .catch((err: unknown) => {
        reason = errorText(err)
        return undefined
      })
    if (ran === undefined) continue
    if (ran.exitCode === 0) return parse(ran.stdout)
    reason = `${python} ${SHIM} exited with status ${ran.exitCode}`
  }
  throw new Error(reason)
}

async function serverPort($: EngineInterface, cwd: string): Promise<number> {
  const info: unknown = JSON.parse(await $.fs.read(`${cwd}/${RECORDS}/server.json`))
  const port = isRecord(info) ? Number(info.port) : NaN
  if (!Number.isInteger(port) || port <= 0 || port >= 65536) throw new Error('server.json names no port')
  return port
}

async function serverToken($: EngineInterface, cwd: string): Promise<string> {
  const token = (await $.fs.read(`${cwd}/${RECORDS}/http-token`)).trim()
  if (!TOKEN.test(token)) throw new Error('the ledger token is missing or malformed')
  return token
}

function within<T>($: EngineInterface, ms: number, work: Promise<T>): Promise<T> {
  let timer: { cancel: () => void } | undefined
  const late = new Promise<never>((_, reject) => {
    timer = $.clock.after(ms, () => reject(new Error(`no answer within ${ms / 1000} s`)))
  })
  return Promise.race([work, late]).finally(() => timer?.cancel())
}

function pay($: EngineInterface, s: SentinelSwarmSession): Promise<void> {
  const work = payOwed($, s).catch(() => undefined)
  paying.add(work)
  void work.finally(() => paying.delete(work))
  return work
}

async function payOwed($: EngineInterface, s: SentinelSwarmSession): Promise<void> {
  const answer = await post($, s, 'owed', base(s)).catch(() => null)
  const listed = isRecord(answer) && Array.isArray(answer.wakeups) ? (answer.wakeups as Owed[]) : []
  const owed = listed
    .map(wakeup => ({ ...wakeup, wakeup_ids: wakeup.wakeup_ids.filter(id => !sending.has(id)) }))
    .filter(wakeup => wakeup.wakeup_ids.length > 0)
  if (owed.length === 0) return
  const ids = owed.flatMap(wakeup => wakeup.wakeup_ids)
  ids.forEach(id => sending.add(id))
  try {
    const delivered = (await Promise.all(owed.map(wakeup => deliver($, wakeup)))).flat()
    if (delivered.length > 0) await post($, s, 'wake_sent', { ...base(s), wakeup_ids: delivered }).catch(() => null)
  } finally {
    ids.forEach(id => sending.delete(id))
  }
}

function isOwnSend(e: { message?: unknown }): boolean {
  return typeof e.message === 'string' && sendingTexts.has(e.message)
}

async function deliver($: EngineInterface, wakeup: Owed): Promise<number[]> {
  sendingTexts.set(wakeup.text, (sendingTexts.get(wakeup.text) ?? 0) + 1)
  try {
    return await sendWithRetry($, wakeup)
  } finally {
    const left = (sendingTexts.get(wakeup.text) ?? 1) - 1
    if (left > 0) sendingTexts.set(wakeup.text, left)
    else sendingTexts.delete(wakeup.text)
  }
}

async function sendWithRetry($: EngineInterface, wakeup: Owed): Promise<number[]> {
  const to: SessionSendAddress = { sessionId: wakeup.session_id }
  for (const pause of [0, ...SEND_RETRY_MS]) {
    if (pause > 0) await $.clock.sleep(pause)
    const sent = await within($, SEND_MS, $.session.send({ to, text: wakeup.text })).catch(() => undefined)
    if (sent?.isDelivered) return wakeup.wakeup_ids
    // A session registers 10 to 14 s after it starts; a send before that finds no live session.
    if (sent === undefined || !NOT_YET_LIVE.test(sent.reason)) return []
  }
  return []
}

function withInbox(text: string, taken: Answer): string {
  const messages = isRecord(taken) && Array.isArray(taken.messages) ? taken.messages : []
  const remaining = isRecord(taken) && typeof taken.remaining === 'number' ? taken.remaining : 0
  const rest = remaining > 0 ? ` ${remaining} more wait: call message_inbox for them.` : ''
  return (
    `${text}\n\nsentinel-swarm read your inbox with this wake-up and marked these messages read; ` +
    `do not call message_inbox for them.${rest}\nmessage_inbox() returned: ${JSON.stringify({ messages, remaining })}`
  )
}

function parse(text: string): Answer {
  if (text.trim() === '') return null
  const value: unknown = JSON.parse(text)
  return isRecord(value) ? value : null
}

function hookOutput(answer: Answer): Record<string, unknown> | undefined {
  const output = answer?.hookSpecificOutput
  return isRecord(output) ? output : undefined
}

function field(answer: Answer, name: string, decision?: string): string | undefined {
  const output = hookOutput(answer)
  if (output === undefined || (decision !== undefined && output.permissionDecision !== decision)) return undefined
  const value = output[name]
  return typeof value === 'string' ? value : decision !== undefined ? 'denied by sentinel-swarm' : undefined
}

function cannotCheck(err: unknown): string {
  return `sentinel-swarm cannot check this call: ${errorText(err)}; run /sentinel-swarm:setup`
}

function errorText(err: unknown): string {
  if (err instanceof Error) return err.message
  if (isRecord(err) && typeof err.message === 'string') return err.message
  return String(err)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
