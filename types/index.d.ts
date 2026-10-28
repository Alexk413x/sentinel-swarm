export type SentinelSwarmSession = {
  sessionId: string
  agentType: string
  role: string
  cwd: string
  transcriptPath: string
}

export type PanelAgent = {
  name: string
  role: string
  model: string | null
  effort: string | null
  state: string
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

export type PanelCounts = { live?: number; working?: number; parked?: number; gone?: number; ended?: number }

export type PanelRunView = { run: PanelRun; agents: PanelAgent[]; counts: PanelCounts }

export type PanelSnapshot = {
  repo: string
  generated_at: string
  runs: PanelRunView[]
  error?: string
}

export type PanelFetch = { snapshot: PanelSnapshot | null; running: boolean; problem: string | null; at: string | null }

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
