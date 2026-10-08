export type SentinelSwarmSession = {
  sessionId: string
  agentType: string
  role: string
  cwd: string
  transcriptPath: string
}

declare module 'claude-code' {
  interface PluginState {
    'sentinel-swarm': { session: SentinelSwarmSession | null }
  }
}
