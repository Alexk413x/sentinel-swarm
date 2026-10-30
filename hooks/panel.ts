import type { PanelAgent, PanelRequest, PanelRunView, PanelSessionWait, PanelTask } from '../types'

export const MODEL = /(opus|sonnet|haiku|fable)/i
export const SPANNING_ROLES: ReadonlySet<string> = new Set(['oracle', 'manager'])
export const ROLE_COLOR: Readonly<Record<string, string>> = {
  oracle: 'cyan',
  manager: 'green',
  lead: '#af87ff',
  coder: '#ff8700',
  driver: 'yellow',
}
export const CODE_COLOR = 'suggestion'
export const WAIT_GLYPH = '⚠'
export const WAIT_COLOR = 'warning'
export const ASK_COLOR = 'error'
export const TASK_MARK: Readonly<Record<string, readonly [string, string | undefined]>> = {
  done: ['✓', 'success'],
  active: ['●', 'warning'],
  problem: ['!', 'error'],
}

export type IsOpen = (key: string, fallback: boolean) => boolean
export type Segment = { text: string; color: string | undefined }
export type Span = { text: string; bold?: boolean; italic?: boolean; code?: boolean; dim?: boolean }
export type AgentRow = {
  kind: 'agent'
  key: string
  prefix: Segment[]
  gap: Segment[] | null
  agent: PanelAgent
  toggle: { open: boolean } | null
  hidden: number
  taskOpen: boolean
  followed: boolean
}
export type TaskRow = { kind: 'task'; key: string; prefix: Segment[]; task: PanelTask; open: boolean }
export type Row = AgentRow | TaskRow
export type SessionWait = Omit<PanelSessionWait, 'request'>
export type Attention = {
  key: string
  agent: string | null
  title: string
  lines: string[]
  where: string
  directiveId: number | null
}

const INPUT_CHARS = 400

export function mark(agent: PanelAgent, waiting = false): [string, string | undefined] {
  if (agent.ended_at !== null) return ['·', undefined]
  if (waiting) return [WAIT_GLYPH, WAIT_COLOR]
  if (agent.queued) return ['○', undefined]
  if (agent.parked || agent.state === 'parked') return ['⏸', 'warning']
  if (agent.session === 'gone') return ['✕', 'error']
  if (agent.session === 'idle') return ['○', undefined]
  return ['●', undefined]
}

export function flatten(
  nodes: readonly PanelAgent[],
  withEnded: boolean,
  parentKey: string,
  isOpen: IsOpen,
  lead: readonly Segment[] = [],
  parentColor: string | undefined = undefined,
  top = true,
  spans = false,
): Row[] {
  const shown = nodes.filter(node => withEnded || visible(node))
  return shown.flatMap((agent, i) => {
    const last = !spans && i === shown.length - 1
    const color = parentColor
    const root = top && agent.role === 'oracle'
    const prefix = root ? [] : [...lead, { text: last ? '└ ' : '├ ', color }]
    const childLead = root ? [] : [...lead, { text: last ? '  ' : '│ ', color }]
    const gap = i > 0 && agent.role === 'manager' ? [...lead, { text: '│', color }] : null
    const key = `${parentKey}/${agent.name}`
    const own = ROLE_COLOR[agent.role]
    const spanning = SPANNING_ROLES.has(agent.role)
    const below = flatten(agent.children, withEnded, key, isOpen, childLead, own, false, spanning)
    const spacer = { text: spanning || below.length > 0 ? '│ ' : '  ', color: own }
    const tasks: TaskRow[] = agent.tasks.map(task => ({
      kind: 'task',
      key: `${key}#${task.id}`,
      prefix: [...childLead, spacer],
      task,
      open: isOpen(`${key}#${task.id}`, false),
    }))
    const expanded = root || isOpen(key, true)
    const toggle = root || (agent.children.length === 0 && agent.tasks.length === 0) ? null : { open: expanded }
    const agents = below.filter(row => row.kind === 'agent').length
    const section = [...tasks, ...below]
    const row: AgentRow = {
      kind: 'agent',
      key,
      prefix,
      gap,
      agent,
      toggle,
      hidden: agents,
      taskOpen: isOpen(`${key}#task`, false),
      followed: expanded && (spanning || section.length > 0),
    }
    return expanded ? [row, ...(spanning ? closeSpan(section, childLead.length) : section)] : [row]
  })
}

export function continuation(prefix: readonly Segment[]): Segment[] {
  return prefix.map(part => ({ ...part, text: part.text === '├ ' ? '│ ' : part.text === '└ ' ? '  ' : part.text }))
}

export function dimmed(agent: PanelAgent): boolean {
  return (
    agent.queued ||
    agent.ended_at !== null ||
    agent.parked ||
    agent.state === 'released' ||
    agent.state === 'parked' ||
    agent.session === 'gone' ||
    agent.task?.state === 'approved' ||
    (agent.role === 'manager' && agent.task?.state === 'planned')
  )
}

export function taskDetail(task: PanelTask, room: number): Span[][] {
  const path = task.path === null ? [] : [...wrap([{ text: task.path, code: true }], room, ''), []]
  const lines = [...path, ...markdown(task.brief ?? 'No brief recorded.', room, task.brief === null)]
  if (task.review !== null) {
    lines.push([])
    lines.push([{ text: `${task.review.kind} review: `, dim: true }, { text: task.review.outcome, bold: true }])
    if (task.review.notes) lines.push(...markdown(task.review.notes, room, false))
  }
  return lines
}

export function markdown(text: string, room: number, dim: boolean): Span[][] {
  const lines: Span[][] = []
  let fenced = false
  for (const raw of text.split('\n')) {
    if (/^\s*```/.test(raw)) {
      fenced = !fenced
      continue
    }
    if (fenced) {
      lines.push([{ text: raw, code: true }])
      continue
    }
    const heading = /^#{1,6}\s+(.*)$/.exec(raw)
    if (heading) {
      lines.push(...wrap(inline(heading[1] ?? '', dim).map(span => ({ ...span, bold: true })), room, ''))
      continue
    }
    const item = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(raw)
    if (item) {
      const marker = /\d/.test(item[2] ?? '') ? `${item[2]} ` : '• '
      const indent = ' '.repeat(Math.min(6, (item[1] ?? '').length))
      const hang = `${indent}${' '.repeat(marker.length)}`
      const body = wrap(inline(item[3] ?? '', dim), room - hang.length, hang)
      const first = body[0] ?? []
      lines.push([{ text: `${indent}${marker}`, dim: true }, ...first.slice(1)], ...body.slice(1))
      continue
    }
    lines.push(...wrap(inline(raw, dim), room, ''))
  }
  return lines
}

export function inline(text: string, dim: boolean): Span[] {
  const spans: Span[] = []
  const pattern = /(\*\*[^*]+\*\*|__[^_]+__|`[^`]+`|\*[^*\s][^*]*\*)/g
  let at = 0
  for (const match of text.matchAll(pattern)) {
    const token = match[0]
    const start = match.index ?? 0
    if (start > at) spans.push({ text: text.slice(at, start), dim })
    if (token.startsWith('`')) spans.push({ text: token.slice(1, -1), code: true })
    else if (token.startsWith('**') || token.startsWith('__')) spans.push({ text: token.slice(2, -2), bold: true, dim })
    else spans.push({ text: token.slice(1, -1), italic: true, dim })
    at = start + token.length
  }
  if (at < text.length) spans.push({ text: text.slice(at), dim })
  return spans
}

export function wrap(spans: readonly Span[], room: number, hang: string): Span[][] {
  const words: Span[][] = []
  let glued = false
  for (const span of spans) {
    const pieces = span.text.split(/(\s+)/)
    for (const piece of pieces) {
      if (piece === '') continue
      if (/^\s+$/.test(piece)) {
        glued = false
        continue
      }
      const part = { ...span, text: piece }
      const last = words[words.length - 1]
      if (glued && last !== undefined) last.push(part)
      else words.push([part])
      glued = true
    }
  }
  if (words.length === 0) return [[]]
  const width = (word: readonly Span[]) => word.reduce((n, part) => n + part.text.length, 0)
  const lines: Span[][] = []
  let current: Span[] = []
  let used = 0
  for (const word of words) {
    const size = width(word)
    if (used > 0 && used + 1 + size > Math.max(10, room)) {
      lines.push(current)
      current = []
      used = 0
    }
    if (used > 0) current.push({ text: ' ' })
    current.push(...word)
    used += (used > 0 ? 1 : 0) + size
  }
  lines.push(current)
  return lines.map((line, i) => (i === 0 || hang === '' ? [{ text: '' }, ...line] : [{ text: hang }, ...line]))
}

export function closeSpan(rows: readonly Row[], column: number): Row[] {
  const end = rows[rows.length - 1]
  if (end === undefined || end.prefix[column] === undefined) return [...rows]
  const prefix = end.prefix.map((segment, i) => (i === column ? { ...segment, text: '└ ' } : segment))
  return [...rows.slice(0, -1), { ...end, prefix }]
}

export function sectionKeys(runs: readonly PanelRunView[]): string[] {
  const walk = (nodes: readonly PanelAgent[], parent: string): string[] =>
    nodes.flatMap(node => {
      const key = `${parent}/${node.name}`
      const below = walk(node.children, key)
      return (node.children.length === 0 && node.tasks.length === 0) || node.role === 'oracle' ? below : [key, ...below]
    })
  return runs.flatMap(view => [`run:${view.run.run_id}`, ...walk(view.agents, `run:${view.run.run_id}`)])
}

export function visible(node: PanelAgent): boolean {
  return node.ended_at === null || node.children.some(visible)
}

export function duration(seconds: number | null): string {
  if (seconds === null) return '-'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m ${seconds % 60}s`
}

export const NO_LEDGER = 'no ledger'

export function startHint(runs: readonly PanelRunView[], running: boolean): readonly string[] {
  if (running) return []
  const newest = runs[0]
  const start = 'To start a run, run /sentinel-swarm:run with a PRD.'
  if (newest === undefined) return ['No swarm has run in this repo.', 'To prepare the repo, run /sentinel-swarm:setup.', start]
  if (newest.run.ended_at === null) {
    return [
      `No run is active. Run ${newest.run.run_id} stopped before it finished.`,
      'To continue it, run /sentinel-swarm:resume.',
      start,
    ]
  }
  return ['No run is active.', start]
}

export function samePath(a: string, b: string): boolean {
  const norm = (p: string) => p.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()
  return norm(a) === norm(b)
}

export function parseWaiting(stdout: string): SessionWait[] {
  let listing: unknown
  try {
    listing = JSON.parse(stdout)
  } catch {
    return []
  }
  if (!Array.isArray(listing)) return []
  return listing.flatMap(entry => {
    if (typeof entry !== 'object' || entry === null) return []
    const { sessionId, name, status, state, waitingFor } = entry as Record<string, unknown>
    if (typeof sessionId !== 'string') return []
    if (status !== 'waiting' && state !== 'waiting' && typeof waitingFor !== 'string') return []
    return [{ sessionId, name: typeof name === 'string' ? name : '', waitingFor: typeof waitingFor === 'string' ? waitingFor : 'waiting' }]
  })
}

export function liveAgents(views: readonly PanelRunView[]): PanelAgent[] {
  const walk = (nodes: readonly PanelAgent[]): PanelAgent[] =>
    nodes.flatMap(node => [...(node.ended_at === null && !node.queued ? [node] : []), ...walk(node.children)])
  return views.filter(view => view.run.ended_at === null).flatMap(view => walk(view.agents))
}

export function waitOf<W extends SessionWait>(agent: PanelAgent, waits: readonly W[]): W | undefined {
  return waits.find(wait => wait.sessionId === agent.agent_id || (agent.session_name !== null && wait.name === agent.session_name))
}

export function pendingRequest(transcript: string): PanelRequest | null {
  const answered = new Set<string>()
  const lines = transcript.split('\n')
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i] ?? ''
    if (!line.includes('"tool_use"') && !line.includes('"tool_result"')) continue
    let entry: unknown
    try {
      entry = JSON.parse(line)
    } catch {
      continue
    }
    const content = (entry as { message?: { content?: unknown } } | null)?.message?.content
    if (!Array.isArray(content)) continue
    for (const block of [...content].reverse()) {
      if (typeof block !== 'object' || block === null) continue
      const { type, id, tool_use_id, name, input } = block as Record<string, unknown>
      if (type === 'tool_result' && typeof tool_use_id === 'string') answered.add(tool_use_id)
      if (type === 'tool_use' && typeof id === 'string' && !answered.has(id)) return request(String(name ?? ''), input)
    }
  }
  return null
}

function request(tool: string, input: unknown): PanelRequest {
  const fields = typeof input === 'object' && input !== null ? (input as Record<string, unknown>) : {}
  if (tool === 'AskUserQuestion' && Array.isArray(fields.questions)) {
    const questions = fields.questions.flatMap((q: unknown) => {
      if (typeof q !== 'object' || q === null) return []
      const { question, options } = q as Record<string, unknown>
      const labels = Array.isArray(options) ? options.flatMap(label) : []
      return [{ question: String(question ?? ''), options: labels }]
    })
    return { kind: 'question', questions }
  }
  const named = [fields.command, fields.file_path, fields.notebook_path, fields.path, fields.url].find(v => typeof v === 'string')
  const text = typeof named === 'string' ? named : JSON.stringify(input ?? {})
  return { kind: 'tool', tool, input: text.length > INPUT_CHARS ? `${text.slice(0, INPUT_CHARS - 1)}…` : text }
}

function label(option: unknown): string[] {
  const value = typeof option === 'object' && option !== null ? (option as { label?: unknown }).label : undefined
  return typeof value === 'string' ? [value] : []
}

export function requestLines(request: PanelRequest | null): string[] {
  if (request === null) return ['Agent view shows the request.']
  if (request.kind === 'tool') return [`${request.tool}: ${request.input}`]
  return request.questions.flatMap(q => [q.question, ...q.options.map(option => `  • ${option}`)])
}

const WAIT_TITLE: Readonly<Record<string, string>> = {
  'permission prompt': 'waits on a permission prompt',
  'input needed': 'waits for your answer',
}

export function attention(view: PanelRunView, sessions: readonly PanelSessionWait[]): Attention[] {
  if (view.run.ended_at !== null) return []
  const run = view.run.run_id
  const agents = liveAgents([view])
  const items: Attention[] = []
  const waiting = new Set<string>()
  for (const agent of agents) {
    const wait = waitOf(agent, sessions)
    if (wait === undefined) continue
    waiting.add(agent.name)
    items.push({
      key: `wait:${run}:${agent.name}`,
      agent: agent.name,
      title: WAIT_TITLE[wait.waitingFor] ?? `waits: ${wait.waitingFor}`,
      lines: requestLines(wait.request),
      where: `Answer it in agent view (claude agents), on session ${wait.name || agent.name}.`,
      directiveId: null,
    })
  }
  const oracle = agents.find(agent => agent.role === 'oracle')?.name ?? null
  const asked = view.waiting ?? { questions: [], findings: [], paused: null }
  for (const q of asked.questions) {
    items.push({
      key: `question:${run}:${q.directive_id}`,
      agent: oracle,
      title: `asks you (directive ${q.directive_id})`,
      lines: q.question.split('\n'),
      where: `Type the answer and press Enter: it goes to the Oracle as a reply to directive ${q.directive_id}.`,
      directiveId: q.directive_id,
    })
  }
  for (const f of asked.findings) {
    if (f.kind === 'needs_user' && f.agent !== null && waiting.has(f.agent)) continue
    const stalled = f.kind === 'run_stalled'
    const agent = !stalled && agents.some(a => a.name === f.agent) ? f.agent : null
    items.push({
      key: `finding:${run}:${f.finding_id}`,
      agent,
      title: stalled ? 'the run has stalled' : `${f.agent ?? 'a session'} needs you`,
      lines: f.detail.split('\n'),
      where: stalled ? 'Check it with /sentinel-swarm:status, then message the Oracle.' : 'Answer it in agent view (claude agents).',
      directiveId: null,
    })
  }
  if (asked.paused !== null) {
    items.push({
      key: `paused:${run}`,
      agent: null,
      title: 'the run is paused',
      lines: asked.paused.split('\n'),
      where: 'Fix the cause, then message the Oracle in agent view or run /sentinel-swarm:resume.',
      directiveId: null,
    })
  }
  return items
}
