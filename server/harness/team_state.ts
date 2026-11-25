/** Read-only host seam for the pinned native Team root projection.
 * Mounted only by the default-off state-only product profile. Python must establish ownership and an
 * exclusive idle-session reservation before invoking this seam in a private
 * native process. It never enables Team, mutates a board, or wakes an Agent.
 */
const SOURCE_COMMIT = 'fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159'
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)

export function teamStateRequest(ctx: any, config: any, params: unknown, initialized: boolean): unknown {
  if (!initialized || !record(params) || Object.keys(params).length !== 2
    || params.action !== 'get' || typeof params.sessionId !== 'string'
    || params.sessionId !== config.sessionId || config.sourceCommit !== SOURCE_COMMIT) {
    throw new Error('Unauthorized Team state request')
  }
  const agent = ctx.agents.get(config.sessionId)
  if (!agent || agent.id !== config.sessionId || agent.session?.id !== config.sessionId
    || agent.status !== 'idle' || ctx.subagents.rootOf(agent) !== agent) {
    throw new Error('Team root unavailable')
  }
  const board = ctx.sessionProjections.snapshot(agent.session).values.agentTeam
  if (!board || board.failure !== undefined || typeof board.enabled !== 'boolean'
    || !Array.isArray(board.tasks)) throw new Error('Team projection unavailable')
  // The official projection already derives readiness, revision and advisory
  // overlap warnings. Do not build a second task graph or expose the raw log.
  const result = JSON.stringify(board)
  if (board.tasks.length > 256 || Buffer.byteLength(result, 'utf8') > 1024 * 1024) {
    throw new Error('Team projection exceeds host response limit')
  }
  return {schema:'workspace-team-state/v1', sourceCommit:SOURCE_COMMIT,
    sessionId:config.sessionId, board:JSON.parse(result), executionEnabled:false}
}
