export type SentinelSwarmSession = {
  sessionId: string
  agentType: string
  role: string
  cwd: string
  transcriptPath: string
}

export type PanelAgent = {
  agent_id: string | null
  name: string
  role: string
  model: string | null
  effort: string | null
  state: string
  queued: boolean
  session_name: string | null
  transcript_path: string | null
  session: 'busy' | 'idle' | 'running' | 'gone' | null
  parked: boolean
  park_reason: string | null
  ended_at: string | null
  end_reason: string | null
  phase: string | null
  module: string | null
  file: string | null
  activity: string | null
  heartbeat_at: string | null
  started_at: string
  owed: number
  unread: number
  task: PanelTask | null
  tasks: PanelTask[]
  children: PanelAgent[]
}

export type PanelReview = { kind: string; outcome: string; notes: string | null }

export type PanelTask = {
  id: string
  name: string
  path: string | null
  state: string
  status: 'done' | 'active' | 'waiting' | 'problem'
  owners: string[]
  summary: string | null
  brief: string | null
  review: PanelReview | null
}

export type PanelRun = {
  run_id: number
  state: string
  outcome: string | null
  branch: string | null
  started_at: string
  ended_at: string | null
  elapsed_s: number | null
}

export type PanelCounts = { live?: number; working?: number; parked?: number; gone?: number; queued?: number; ended?: number }

export type PanelQuestion = { directive_id: number; question: string; asked_at: string }

export type PanelFinding = { finding_id: number; kind: string; agent: string | null; detail: string; since: string }

export type PanelPermission = {
  request_id: number
  agent: string
  tool: string
  summary: string
  reason: string | null
  asked_at: string
  expires_at: string
}

export type PanelRunWaiting = { questions: PanelQuestion[]; findings: PanelFinding[]; paused: string | null; permissions: PanelPermission[] }

export type PanelRunView = { run: PanelRun; agents: PanelAgent[]; counts: PanelCounts; waiting?: PanelRunWaiting }

export type PanelRequest =
  | { kind: 'tool'; tool: string; input: string }
  | { kind: 'question'; questions: { question: string; options: string[] }[] }

export type PanelSessionWait = { sessionId: string; name: string; waitingFor: string; request: PanelRequest | null }

export type PanelSnapshot = {
  repo: string
  generated_at: string
  runs: PanelRunView[]
  error?: string
}

export type PanelFetch = {
  snapshot: PanelSnapshot | null
  sessions: PanelSessionWait[]
  running: boolean
  problem: string | null
  at: string | null
}

declare module 'claude-code' {
  interface PluginState {
    'sentinel-swarm': {
      session: SentinelSwarmSession | null
      panelFetch: Shaped<PanelFetch>
      panelShowEnded: Shaped<boolean>
      panelRepo: Shaped<string>
      panelOpen: Shaped<Record<string, boolean>>
      panelOpenTask: Shaped<string>
    }
  }
}
