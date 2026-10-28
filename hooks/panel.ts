import type { PanelAgent, PanelRunView, PanelTask } from '../types'

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

export function mark(agent: PanelAgent): [string, string | undefined] {
  if (agent.ended_at !== null) return ['·', undefined]
  if (agent.parked) return ['⏸', 'warning']
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

export function taskDetail(task: PanelTask, room: number): Span[][] {
  const lines = markdown(task.brief ?? 'No brief recorded.', room, task.brief === null)
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
