import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, SessionSendAddress, SessionSendResult } from 'claude-code'

import {
  ASK_COLOR,
  CODE_COLOR,
  MODEL,
  NO_LEDGER,
  ROLE_COLOR,
  SPANNING_ROLES,
  TASK_MARK,
  WAIT_COLOR,
  WAIT_GLYPH,
  attention,
  closeSpan,
  continuation,
  dimmed,
  duration,
  flatten,
  holdForPanel,
  inline,
  liveAgents,
  mark,
  markdown,
  parseWaiting,
  pendingRequest,
  samePath,
  sectionKeys,
  startHint,
  taskDetail,
  visible,
  waitOf,
  wrap,
} from './panel'
import type { AgentRow, Attention, HoldLedger, IsOpen, Row, Segment, Span, TaskRow } from './panel'
import type { PanelFetch, PanelSessionWait, PanelSnapshot, PanelTask, PanelRunView, SentinelSwarmSession } from '../types'

type Answer = Record<string, unknown> | null
type Owed = {
  session_id: string
  text: string
  wakeup_ids: number[]
}

const ROLE_TYPE = /^swarm-(oracle|manager|lead|coder|driver)$/
const LEDGER_TOOL = /^mcp__swarm-ledger__/
const TOKEN = /^[A-Za-z0-9_-]{32,128}$/
const REPO_HEADER = 'X-Sentinel-Swarm-Repo'
const TRANSPORT_KEY = 'sentinel_swarm_transport'
const FIRED_AT_KEY = 'sentinel_swarm_fired_at'
const RECORDS = '.sentinel-swarm'
const SHIM = `${RECORDS}/hook.py`
const PYTHONS = ['python3', 'python']
const FAST_MS = 10_000
const SLOW_MS: Readonly<Record<string, number>> = { stop: 40_000, session_end: 40_000, permission_wait: 40_000 }
const SHIM_MS = 50_000
const SEND_MS = 15_000
const SEND_RETRY_MS = [2_000, 4_000, 8_000]
const NOT_YET_LIVE = /no live session/i
const SELF_WAKE_MS = 15_000
// Many sessions starting at once can leave ~/.claude.json truncated until a later write completes it.
const CONFIG_UNREADABLE = /JSON Parse error|\.claude\.json\S*\s+is\s+corrupt/i
const WRITE_TOOLS = ['Write', 'Edit', 'MultiEdit', 'NotebookEdit']
const WRITE_TOOL = new RegExp(`^(${WRITE_TOOLS.join('|')})$`)
const SHELL_TOOL = /^(Bash|PowerShell)$/
// Must equal SYNC_POST_TOOLS in mcp/src/swarm_ledger/hooks/events.py.
const SYNC_POST_TOOLS = ['SendMessage', 'PushNotification', 'Monitor', ...WRITE_TOOLS]
const RECEIVED_FROM = ['peer', 'peer-send-message', 'coordinator']
// A hook's allow does not dismiss the dialog of a tool that requires the person.
const PERSON_TOOLS: ReadonlySet<string> = new Set(['AskUserQuestion', 'ExitPlanMode'])
const PRE_TOOL_GATES: readonly {
  tool: string | RegExp
  event: string
  role?: string
  skip?: (e: object) => boolean
}[] = [
  { tool: 'Agent', event: 'pre_agent' },
  { tool: 'AskUserQuestion', event: 'pre_ask' },
  { tool: WRITE_TOOL, event: 'pre_write' },
  { tool: SHELL_TOOL, event: 'pre_shell' },
  { tool: 'Monitor', event: 'pre_monitor' },
  // $.session.send runs as a SendMessage call to the target's pipe address, not its session id;
  // the ledger checked the recipient before the mod sent, so the mod's own text passes.
  { tool: 'SendMessage', event: 'pre_send_message', skip: isOwnSend },
  { tool: 'Skill', event: 'pre_skill', role: 'driver' },
  { tool: LEDGER_TOOL, event: 'pre_ledger' },
]
const sessionRef = { plugin: 'sentinel-swarm', key: 'session' } as const

let session: SentinelSwarmSession | null | undefined
const sending = new Set<number>()
const paying = new Set<Promise<void>>()
const sendingTexts = new Map<string, number>()
let selfWakePending = true
let selfWakeTimer = false

const PANE = 'swarm-run'
const TITLE = 'Swarm'
const OPEN_TOOL = 'mcp__sentinel-swarm__open_panel'
const STATUS_TOOL = 'mcp__sentinel-swarm__swarm_status'
const LEDGER_SERVER = 'swarm-ledger'
const REFRESH_MS = 5_000
const RUN_MS = 30_000
const HEALTH_MS = 2_000
const LAUNCH_PYTHONS: readonly (readonly string[])[] = [['py', '-3'], ['python3'], ['python']]
const REPO_INPUT = {
  type: 'object',
  properties: { repo: { type: 'string', description: 'Absolute path of the host repo' } },
} as const

const fetched = atom(
  { plugin: 'sentinel-swarm', key: 'panelFetch' } as const,
  { snapshot: null, sessions: [], running: false, problem: null, at: null } as PanelFetch,
  { shape: 'fetch-3' },
)
const showEnded = atom({ plugin: 'sentinel-swarm', key: 'panelShowEnded' } as const, false, { shape: 'show-ended-1' })
const chosenRepo = atom({ plugin: 'sentinel-swarm', key: 'panelRepo' } as const, '', { shape: 'repo-1' })
const opened = atom({ plugin: 'sentinel-swarm', key: 'panelOpen' } as const, {} as Record<string, boolean>, { shape: 'open-1' })
const openTask = atom({ plugin: 'sentinel-swarm', key: 'panelOpenTask' } as const, '', { shape: 'open-task-1' })

let refreshing = false

export const register: Register = on => {
  on('classic.SessionStart', async ($, e, next) => {
    const s = await adopt($, e)
    if (s === null) return next(e)
    keepSelfWake($)
    const answer = await runHook($, s, 'session_start', { ...e, [TRANSPORT_KEY]: 'mod' }).catch(() => null)
    const result = await next(e)
    const text = field(answer, 'additionalContext')
    return text === undefined ? result : { ...result, additionalContext: [...(result.additionalContext ?? []), text] }
  })

  for (const { tool, event, role, skip } of PRE_TOOL_GATES) {
    on('tool.call', { tool }, ($, e, next) => (skip?.(e) ? next(e) : judge($, e, next, event, role))).catch(($, e, next) =>
      next.called ? next(e) : { deny: cannotCheck(next.error) },
    )
  }

  on('tool.check', async ($, e, next) => {
    const verdict = await next(e)
    if (verdict.decision !== 'ask' || verdict.plugin !== undefined || e.approver !== 'person' || PERSON_TOOLS.has(e.tool)) {
      return verdict
    }
    const s = await active($)
    if (s === null) return verdict
    return holdForPanel(heldIn($, s, e.tool, e.input, verdict.reason), verdict, next.signal)
  }).catch(($, e, next) => next(e))

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
    if (LEDGER_TOOL.test(e.tool_name)) {
      selfWakePending = true
      void pay($, s)
    }
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

  // SendMessage resolves a name after the hooks, and a stale or Remote Control session can carry
  // the same name: a swarm session name goes to the live session by id instead.
  on('session.send', async ($, e, next) => {
    const s = await active($)
    if (s === null || e.origin.kind !== 'model' || e.agentId !== undefined) return next(e)
    const answer = await post($, s, 'send_target', { ...base(s), to: e.to }).catch(() => null)
    const sessionId = isRecord(answer) && typeof answer.session_id === 'string' ? answer.session_id : null
    if (sessionId === null) return next(e)
    return sendOwn($, { sessionId }, e.text)
  }).catch(($, e, next) => next(e))

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

  on('session.start', async ($, e, next) => {
    await $.tool.register({
      name: 'open_panel',
      description:
        "Opens the sentinel-swarm side panel, which shows a repo's swarm runs, past and active, with each run's agents as a " +
        'tree (Oracle, Managers, Leads, Coders), their status, and the phases, modules, and files they work on. With no ' +
        'active run it shows how to start one. Call it when the user asks to see the swarm, its agents, its past runs, or ' +
        "the run's hierarchy. Pass repo to read another repo; leave it out to keep the repo the panel already reads, or " +
        'the session folder.',
      inputSchema: REPO_INPUT,
      isDeferred: true,
    })
    await $.tool.register({
      name: 'swarm_status',
      description:
        'Reports whether a sentinel-swarm run is active in a repo, as JSON {"running": boolean, "repo": string}: ' +
        "running is true while the repo's ledger server answers. Leave repo out for the session folder.",
      inputSchema: REPO_INPUT,
      isDeferred: true,
    })
    $.clock.every(REFRESH_MS, () => void refreshIfOpen($))
    if ((await active($)) !== null) keepSelfWake($)
    return next(e)
  })

  on('tool.call', { tool: OPEN_TOOL }, async ($, e) => ({ result: await openPanel($, typeof e.repo === 'string' ? e.repo : '') }))

  on('tool.call', { tool: STATUS_TOOL }, async ($, e) => {
    const repo = (typeof e.repo === 'string' && e.repo.trim()) || (await $.session.cwd())
    return { result: JSON.stringify({ running: await isRunning($, repo), repo }) }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const elements = $.ui.resolve(e)
    const { Box, Text, Button } = elements
    const Input = 'Input' in elements ? elements.Input : undefined
    const width = Math.max(20, e.props.bodyColumns)
    const { snapshot, sessions, running, problem, at } = await read($, fetched)
    const withEnded = await read($, showEnded)
    const open = await read($, opened)
    const runs = snapshot?.runs ?? []
    const asks = runs.map(view => attention(view, sessions ?? []))
    const asksOf = (name: string, index: number) => (asks[index] ?? []).filter(item => item.agent === name)
    const shownTask = await read($, openTask)
    const isOpen: IsOpen = (key, fallback) => (key.includes('#') ? key === shownTask : (open[key] ?? fallback))
    const closeTask = () => void update($, openTask, () => '')
    const toggleTask = (key: string) => void update($, openTask, current => (current === key ? '' : key))
    const toggle = (key: string, fallback: boolean) => {
      closeTask()
      void update($, opened, held => ({ ...held, [key]: !(held[key] ?? fallback) }))
    }
    const setAll = (value: boolean) => {
      closeTask()
      void update($, opened, () => Object.fromEntries(sectionKeys(runs).map(key => [key, value])))
    }

    const controls = (
      <Box flexDirection="row" gap={1}>
        <Button
          key="ended"
          onPress={() => {
            closeTask()
            void update($, showEnded, shown => !shown)
          }}
        >
          {withEnded ? 'hide ended' : 'show ended'}
        </Button>
        <Button key="collapse" onPress={() => setAll(false)}>
          collapse all
        </Button>
        <Button key="expand" onPress={() => setAll(true)}>
          expand all
        </Button>
        <Button
          key="refresh"
          onPress={() => {
            closeTask()
            void refresh($)
          }}
        >
          refresh
        </Button>
        {at !== null && <Text dimColor>updated {new Date(at).toLocaleTimeString()}</Text>}
      </Box>
    )

    const hint = at === null ? [] : startHint(runs, running)
    const howToStart = hint.length > 0 && (
      <Box flexDirection="column" borderStyle="round" borderColor={ROLE_COLOR.oracle} paddingX={1}>
        {hint.map((text, i) => (
          <Text key={String(i)} bold={i === 0} dimColor={i > 0}>
            {text}
          </Text>
        ))}
      </Box>
    )

    if (runs.length === 0) {
      const error = snapshot?.error === NO_LEDGER ? undefined : snapshot?.error
      const note = problem ?? error ?? (at === null ? 'Reading the ledger...' : undefined)
      return (
        <Box flexDirection="column">
          {controls}
          {howToStart}
          {note !== undefined && <Text dimColor>{note}</Text>}
        </Box>
      )
    }

    const segments = (parts: readonly Segment[]) =>
      parts.map((part, i) => (
        <Text key={String(i)} color={part.color}>
          {part.text}
        </Text>
      ))

    const taskButton = (key: string, task: PanelTask) => {
      const [glyph, color] = TASK_MARK[task.status] ?? ['○', undefined]
      const waiting = color === undefined
      return (
        <Box flexShrink={1} minWidth={4} flexDirection="row" marginLeft={2}>
          <Box flexShrink={1} minWidth={1}>
            <Button key={key} plain onPress={() => toggleTask(key)}>
              <Text dimColor={waiting} wrap="truncate-start">
                {task.name}
              </Text>
            </Button>
          </Box>
          <Box flexShrink={0}>
            <Text color={color} dimColor={waiting}>
              {' '}
              {glyph}
            </Text>
          </Box>
        </Box>
      )
    }

    const detailLines = (key: string, task: PanelTask, lead: readonly Segment[]) => {
      const room = Math.max(20, width - 6 - lead.reduce((n, part) => n + part.text.length, 0))
      return [...taskDetail(task, room), []].map((spans, i) => (
        <Box key={`${key}:${i}`} flexDirection="row">
          <Box flexShrink={0}>
            <Text>
              {segments(lead)}
              {'  '}
            </Text>
          </Box>
          <Button key={`${key}:${i}`} plain onPress={closeTask}>
            {spans.length === 0 || spans.every(span => span.text === '') ? (
              <Text> </Text>
            ) : (
              spans.map((span, s) => (
                <Text key={String(s)} bold={span.bold} italic={span.italic} dimColor={span.dim} color={span.code ? CODE_COLOR : undefined}>
                  {span.text}
                </Text>
              ))
            )}
          </Button>
        </Box>
      ))
    }

    const taskLine = (row: TaskRow) => (
      <Box key={row.key} flexDirection="column">
        <Box flexDirection="row">
          <Box flexShrink={0}>
            <Text>{segments(row.prefix)}</Text>
          </Box>
          <Box flexGrow={1} />
          {taskButton(row.key, row.task)}
        </Box>
        {row.open && detailLines(row.key, row.task, continuation(row.prefix))}
      </Box>
    )

    const askBox = (item: Attention, indent: number) => {
      const where =
        item.directiveId === null || Input !== undefined
          ? item.where
          : `Answer it with the directive command in /sentinel-swarm:run, with --reply-to ${item.directiveId}.`
      return (
        <Box
          key={item.key}
          flexDirection="column"
          marginLeft={indent}
          borderStyle="round"
          borderColor={ASK_COLOR}
          paddingX={1}
        >
          <Text bold color={WAIT_COLOR}>
            {WAIT_GLYPH} {item.title}
          </Text>
          {item.lines.map((text, i) => (
            <Text key={String(i)}>{text === '' ? ' ' : text}</Text>
          ))}
          <Text dimColor>{where}</Text>
          {item.permissionId !== null && (
            <Box flexDirection="row" gap={1}>
              <Button key={`allow:${item.key}`} onPress={() => void answerPermission($, item.permissionId ?? 0, true)}>
                Allow
              </Button>
              <Button key={`deny:${item.key}`} onPress={() => void answerPermission($, item.permissionId ?? 0, false)}>
                Deny
              </Button>
            </Box>
          )}
          {item.directiveId !== null && Input !== undefined && (
            <Input
              key={`answer:${item.key}`}
              label="answer: "
              placeholder="type the answer"
              submitLabel="send"
              onSubmit={value => void replyTo($, item.directiveId ?? 0, value)}
            />
          )}
        </Box>
      )
    }

    const line = (row: AgentRow, index: number) => {
      const agent = row.agent
      const role = ROLE_COLOR[agent.role]
      const asked = agent.ended_at === null ? asksOf(agent.name, index) : []
      const [glyph, alert] = mark(agent, asked.length > 0)
      const model = MODEL.exec(agent.model ?? '')?.[1]?.toLowerCase() ?? agent.model ?? 'model not recorded'
      const card = agent.queued
        ? `queued: ${agent.park_reason ?? 'waits to start'}`
        : `${model}${agent.effort ? ` · ${agent.effort} effort` : ''}`
      const badges = `${agent.unread ? ` ✉${agent.unread}` : ''}${agent.owed ? ` ↯${agent.owed}` : ''}`
      const taskKey = `${row.key}#task`
      const dim = asked.length === 0 && dimmed(agent)
      const bold = agent.role === 'oracle' || agent.role === 'manager'
      const title =
        row.toggle === null ? (
          <Text color={role} bold={bold} dimColor={dim}>
            {agent.name}
          </Text>
        ) : (
          <Button key={row.key} plain onPress={() => toggle(row.key, true)}>
            <Text color={role} bold={bold} dimColor={dim}>
              {agent.name} ({row.hidden})
            </Text>
          </Button>
        )
      const lead = [...continuation(row.prefix), { text: row.followed ? '│ ' : '  ', color: role }]
      return (
        <Box key={row.key} flexDirection="column">
          <Box flexDirection="row">
            <Box flexShrink={0}>
              <Text dimColor={dim}>
                {segments(row.prefix)}
                <Text color={alert ?? role}>{glyph} </Text>
              </Text>
            </Box>
            <Box key={`${row.key}:name`} flexShrink={0}>
              {title}
              <Box position="absolute" top={-1} left={0} display="none" hover={{ display: 'flex' }}>
                <Text inverse> {card} </Text>
              </Box>
            </Box>
            <Box flexGrow={1} flexShrink={1}>
              <Text dimColor wrap="truncate-end">
                {badges}
              </Text>
            </Box>
            {agent.task !== null && taskButton(taskKey, agent.task)}
          </Box>
          {agent.task !== null && row.taskOpen && detailLines(taskKey, agent.task, lead)}
          {asked.map(item => askBox(item, lead.reduce((n, part) => n + part.text.length, 0)))}
        </Box>
      )
    }

    const rowElements = (rows: readonly Row[], index: number) =>
      rows.flatMap(row => {
        if (row.kind === 'task') return [taskLine(row)]
        const gap =
          row.gap === null
            ? []
            : [
                <Text key={`${row.key}:gap`} wrap="truncate-end">
                  {segments(row.gap)}
                </Text>,
              ]
        return [...gap, line(row, index)]
      })

    const section = (view: PanelRunView, index: number) => {
      const { run, counts } = view
      const key = `run:${run.run_id}`
      const fallback = index === 0
      const shown = isOpen(key, fallback)
      const live = run.ended_at === null
      const rows = flatten(view.agents, withEnded || !live || !counts.live, key, isOpen)
      const outcome = run.outcome?.split(':')[0]?.trim()
      const tally = live
        ? `${counts.live ?? 0} live · ${counts.working ?? 0} working` +
          `${counts.parked ? ` · ${counts.parked} parked` : ''}${counts.gone ? ` · ${counts.gone} gone` : ''}` +
          `${counts.queued ? ` · ${counts.queued} queued` : ''} · ${counts.ended ?? 0} ended`
        : `${counts.ended ?? 0} agents`
      return (
        <Box key={key} flexDirection="column" borderStyle="round" borderColor={ROLE_COLOR.oracle} borderDimColor={!live} paddingX={1}>
          <Box flexDirection="row">
            <Box flexShrink={0}>
              <Button key={key} plain onPress={() => toggle(key, fallback)}>
                <Text bold color={ROLE_COLOR.oracle}>
                  {shown ? '▾ ' : '▸ '}Run {run.run_id}
                </Text>
              </Button>
            </Box>
            <Text dimColor wrap="truncate-end">
              {' '}
              · {outcome ?? run.state} · {duration(run.elapsed_s)} · {tally}
              {run.branch ? ` · ${run.branch}` : ''}
            </Text>
          </Box>
          {shown && rows.length === 0 && <Text dimColor>No live agents. Show ended to see the whole run.</Text>}
          {shown && rowElements(rows, index)}
        </Box>
      )
    }

    const waitingItems = runs.flatMap((view, i) => (asks[i] ?? []).map(item => ({ item, run: view.run.run_id })))
    const waitingOnYou = waitingItems.length > 0 && (
      <Box flexDirection="column" borderStyle="round" borderColor={ASK_COLOR} paddingX={1}>
        <Text bold color={WAIT_COLOR}>
          {WAIT_GLYPH} Waiting on you ({waitingItems.length})
        </Text>
        {waitingItems.map(({ item, run }) => (
          <Box key={`top:${item.key}`} flexDirection="column">
            <Text wrap="truncate-end">
              <Text bold>{item.agent ?? `Run ${run}`}</Text> {item.title}
            </Text>
            {item.agent === null ? (
              [...item.lines, item.where].map((text, i) => (
                <Text key={String(i)} dimColor>
                  {'  '}
                  {text}
                </Text>
              ))
            ) : (
              <Text dimColor wrap="truncate-end">
                {'  '}
                {item.lines[0] ?? ''} · details under its row
              </Text>
            )}
          </Box>
        ))}
      </Box>
    )

    return (
      <Box flexDirection="column">
        {controls}
        {waitingOnYou}
        <Box flexDirection="column" borderStyle="round" borderColor="inactive" paddingX={1}>
          <Text dimColor wrap="truncate-end">
            agents ● busy ○ idle ⏸ parked ✕ gone · ended <Text color={WAIT_COLOR}>{WAIT_GLYPH}</Text> waiting on you ✉ unread ↯ wake-up owed
          </Text>
          <Text dimColor wrap="truncate-end">
            work ○ waiting <Text color="warning">●</Text> in progress <Text color="success">✓</Text> done <Text color="error">!</Text> incomplete
          </Text>
          <Text dimColor wrap="truncate-end">
            dimmed: ended or not started
          </Text>
        </Box>
        {howToStart}
        {runs.map(section)}
        {problem !== null && (
          <Text color="error" wrap="truncate-end">
            {problem}
          </Text>
        )}
      </Box>
    )
  })
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

function heldIn($: EngineInterface, s: SentinelSwarmSession, tool: string, input: unknown, reason: string | undefined): HoldLedger {
  return {
    open: () => post($, s, 'permission_request', { ...base(s), tool_name: tool, tool_input: input, reason }),
    wait: requestId => post($, s, 'permission_wait', { ...base(s), request_id: requestId }),
    close: (requestId, state) => post($, s, 'permission_close', { ...base(s), request_id: requestId, state }),
  }
}

async function adopt(
  $: EngineInterface,
  e: { session_id: string; transcript_path: string; cwd: string; agent_type?: string; agent_id?: string },
): Promise<SentinelSwarmSession | null> {
  const role = ROLE_TYPE.exec(e.agent_type ?? '')?.[1]
  const found: SentinelSwarmSession | null =
    role !== undefined && e.agent_id === undefined
      ? { sessionId: e.session_id, agentType: e.agent_type ?? '', role, cwd: e.cwd, transcriptPath: e.transcript_path }
      : null
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

// A test run's result arrives while the session may sit idle; a plugin prompt waits for the
// running turn to end, so it never interrupts one.
function keepSelfWake($: EngineInterface): void {
  if (selfWakeTimer) return
  selfWakeTimer = true
  $.clock.every(SELF_WAKE_MS, () => void wakeSelf($))
}

async function wakeSelf($: EngineInterface): Promise<void> {
  if (!selfWakePending) return
  const s = await active($)
  if (s === null) return
  const answer = await post($, s, 'self_owed', base(s)).catch(() => null)
  if (!isRecord(answer)) return
  selfWakePending = typeof answer.running === 'number' && answer.running > 0
  if (typeof answer.text === 'string' && answer.text !== '') await $.prompt.submit({ text: answer.text }).catch(() => undefined)
}

function isOwnSend(e: { message?: unknown }): boolean {
  return typeof e.message === 'string' && sendingTexts.has(e.message)
}

async function deliver($: EngineInterface, wakeup: Owed): Promise<number[]> {
  const to: SessionSendAddress = { sessionId: wakeup.session_id }
  for (const pause of [0, ...SEND_RETRY_MS]) {
    if (pause > 0) await $.clock.sleep(pause)
    const sent = await sendOwn($, to, wakeup.text).catch(() => undefined)
    if (sent?.isDelivered) return wakeup.wakeup_ids
    // A session registers 10 to 14 s after it starts; a send before that finds no live session.
    if (sent === undefined || !(NOT_YET_LIVE.test(sent.reason) || CONFIG_UNREADABLE.test(sent.reason))) return []
  }
  return []
}

async function sendOwn($: EngineInterface, to: SessionSendAddress, text: string): Promise<SessionSendResult> {
  sendingTexts.set(text, (sendingTexts.get(text) ?? 0) + 1)
  try {
    return await within($, SEND_MS, $.session.send({ to, text }))
  } finally {
    const left = (sendingTexts.get(text) ?? 1) - 1
    if (left > 0) sendingTexts.set(text, left)
    else sendingTexts.delete(text)
  }
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

async function openPanel($: EngineInterface, args: string): Promise<string> {
  const asked = args.trim().replace(/^["']|["']$/g, '')
  if (asked !== '') await update($, chosenRepo, () => asked)
  const repo = await repoOf($)
  const opened = await $.ui.open({ id: PANE, title: TITLE })
  void refresh($)
  return opened.isPlaced ? `Swarm panel opened on ${repo}.` : `The swarm panel waits to be placed: ${opened.reason}`
}

async function repoOf($: EngineInterface): Promise<string> {
  return (await read($, chosenRepo)) || (await $.session.cwd())
}

async function isRunning($: EngineInterface, repo: string): Promise<boolean> {
  try {
    const info: unknown = JSON.parse(await $.fs.read(`${repo}/${RECORDS}/server.json`))
    const port = isRecord(info) ? Number(info.port) : NaN
    if (!Number.isInteger(port) || port <= 0 || port >= 65536) return false
    const answer = await within($, HEALTH_MS, $.http.fetch(`http://127.0.0.1:${port}/health`))
    const health: unknown = answer.status === 200 ? JSON.parse(answer.text) : null
    return isRecord(health) && health.name === LEDGER_SERVER && samePath(String(health.repo_root ?? ''), repo)
  } catch {
    return false
  }
}

async function refreshIfOpen($: EngineInterface): Promise<void> {
  const panes = await $.ui.panes()
  if (panes.some(pane => pane.id === PANE)) await refresh($)
}

async function refresh($: EngineInterface): Promise<void> {
  if (refreshing) return
  refreshing = true
  try {
    const repo = await repoOf($)
    const [snapshot, running] = await Promise.all([
      fetchSnapshot($, `${$.plugin.root}/mcp/launch/ledger.py`, repo).catch((err: unknown) => (err instanceof Error ? err : new Error(String(err)))),
      isRunning($, repo),
    ])
    const sessions = snapshot instanceof Error ? [] : await waitingSessions($, snapshot).catch(() => [])
    const at = new Date().toISOString()
    await update($, fetched, held =>
      snapshot instanceof Error ? { ...held, running, problem: snapshot.message, at } : { snapshot, sessions, running, problem: null, at },
    ).catch(() => undefined)
  } finally {
    refreshing = false
  }
}

async function fetchSnapshot($: EngineInterface, launcher: string, repo: string): Promise<PanelSnapshot> {
  return JSON.parse(await launch($, launcher, ['panel', '--repo', repo])) as PanelSnapshot
}

async function launch($: EngineInterface, launcher: string, args: readonly string[]): Promise<string> {
  let reason = 'no Python interpreter ran the launcher'
  for (const python of LAUNCH_PYTHONS) {
    const ran = await $.process.run([...python, '-I', '-S', launcher, ...args], { timeoutMs: RUN_MS }).catch((err: unknown) => {
      reason = err instanceof Error ? err.message : String(err)
      return undefined
    })
    if (ran === undefined) continue
    if (ran.exitCode !== 0) {
      reason = `${python.join(' ')} exited with status ${ran.exitCode}: ${ran.stderr.trim().split('\n').pop() ?? ''}`
      continue
    }
    return ran.stdout
  }
  throw new Error(reason)
}

async function waitingSessions($: EngineInterface, snapshot: PanelSnapshot): Promise<PanelSessionWait[]> {
  const agents = liveAgents(snapshot.runs)
  if (agents.length === 0) return []
  const ran = await $.process.run(['claude', 'agents', '--json'], { timeoutMs: RUN_MS }).catch(() => undefined)
  if (ran === undefined || ran.exitCode !== 0) return []
  const waits = parseWaiting(ran.stdout)
  return Promise.all(
    agents.flatMap(agent => {
      const wait = waitOf(agent, waits)
      if (wait === undefined) return []
      const path = agent.transcript_path
      const text = path === null ? Promise.resolve(null) : $.fs.read(path).catch(() => null)
      return [text.then(body => ({ ...wait, request: body === null ? null : pendingRequest(body) }))]
    }),
  )
}

async function replyTo($: EngineInterface, directiveId: number, text: string): Promise<void> {
  if (text.trim() === '') return
  const repo = await repoOf($)
  const args = ['directive', '--repo', repo, '--source', 'user_chat', '--sender', 'run panel', '--reply-to', String(directiveId), '--', text]
  try {
    await launch($, `${$.plugin.root}/mcp/launch/ledger.py`, args)
    $.ui.toast(`Sent your answer to directive ${directiveId}.`)
  } catch (err) {
    $.ui.toast(`Your answer to directive ${directiveId} was not sent: ${errorText(err)}`)
  }
  await refresh($)
}

async function answerPermission($: EngineInterface, requestId: number, allow: boolean): Promise<void> {
  const repo = await repoOf($)
  const args = ['permission', '--repo', repo, '--id', String(requestId), allow ? '--allow' : '--deny']
  try {
    await launch($, `${$.plugin.root}/mcp/launch/ledger.py`, args)
    $.ui.toast(`${allow ? 'Allowed' : 'Denied'} permission request ${requestId}.`)
  } catch (err) {
    $.ui.toast(`Permission request ${requestId} was not answered: ${errorText(err)}`)
  }
  await refresh($)
}
