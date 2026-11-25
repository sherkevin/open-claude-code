/**
 * Host-only adapter for the pinned official dsh-goal service.
 * The native session log is the sole Goal authority. No model tools,
 * continuation driver, business I/O, or second Goal state machine.
 */
const SOURCE_COMMIT = 'fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159'
const MAX_ROUNDS = 32
const record = (v: unknown): v is Record<string, any> => !!v && typeof v === 'object' && !Array.isArray(v)
const boundedText = (v: unknown): v is string => typeof v === 'string' && !!v.trim()
  && v.length <= 8000 && Buffer.byteLength(v, 'utf8') <= 24000 && !/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/u.test(v)
const validRounds = (v: unknown) => Number.isSafeInteger(v) && (v as number) >= 1 && (v as number) <= MAX_ROUNDS
const domainErrors = new Set(['GOAL_ALREADY_EXISTS','GOAL_NOT_FOUND','GOAL_STALE_REVISION','GOAL_INVALID_TRANSITION','GOAL_INVALID_EDIT',
  'GOAL_INVALID_OBJECTIVE','GOAL_INVALID_MAX_ROUNDS','GOAL_AGENT_NOT_LIVE'])
const keys = (v: Record<string,unknown>, allowed: string[]) => Object.keys(v).every(k => allowed.includes(k))

export async function goalStateRequest(ctx: any, config: any, params: unknown, initialized: boolean): Promise<unknown> {
  if (!initialized || !record(params) || params.sessionId !== config.sessionId || config.sourceCommit !== SOURCE_COMMIT
    || !keys(params,['sessionId','action','ref','objective','maxGoalRounds'])
    || !['get','create','edit','pause','resume','complete','clear'].includes(params.action)) {
    throw new Error('Unauthorized goal state request')
  }
  const agent = ctx.agents.get(config.sessionId)
  if (!agent || agent.session?.id !== config.sessionId || agent.status !== 'idle') throw new Error('Goal session unavailable')
  const service = ctx.goals
  if (!service) throw new Error('Goal service unavailable')
  const receipt = (goal: unknown, error: string|null = null) => ({
    schema: 'workspace-goal-state/v1', sourceCommit: SOURCE_COMMIT, sessionId: config.sessionId,
    goal: goal ?? null, error, continuationDriver: false,
  })
  const action = params.action
  const ref = params.ref
  if (action === 'get') {
    if (!keys(params,['sessionId','action'])) throw new Error('Invalid goal read')
    const value = service.get(agent)
    // No state-only operation may return an armed authorization.
    if (value?.activation === 'armed') throw new Error('Unexpected continuation authorization')
    return receipt(value)
  }
  if (action === 'create') {
    if (!keys(params,['sessionId','action','objective','maxGoalRounds'])
        || !boundedText(params.objective) || !validRounds(params.maxGoalRounds)) throw new Error('Invalid goal create')
  } else {
    if (!record(ref) || !keys(ref,['id','revision']) || typeof ref.id !== 'string'
        || !/^goal-[a-f0-9-]{36}$/u.test(ref.id) || !Number.isSafeInteger(ref.revision) || ref.revision < 1) {
      throw new Error('Invalid goal revision')
    }
    if (action === 'edit') {
      if ((!('objective' in params) && !('maxGoalRounds' in params))
          || ('objective' in params && !boundedText(params.objective))
          || ('maxGoalRounds' in params && !validRounds(params.maxGoalRounds))) throw new Error('Invalid goal edit')
    } else if (!keys(params,['sessionId','action','ref'])) throw new Error('Invalid goal transition')
  }
  try {
    if (action === 'create') service.create(agent,{objective:params.objective.trim(),maxGoalRounds:params.maxGoalRounds})
    else if (action === 'edit') service.edit(agent,ref,{
      ...('objective' in params ? {objective:params.objective.trim()} : {}),
      ...('maxGoalRounds' in params ? {maxGoalRounds:params.maxGoalRounds} : {}),
    })
    else if (action === 'pause') service.pause(agent,ref)
    else if (action === 'resume') service.resume(agent,ref)
    else if (action === 'complete') service.complete(agent,ref)
    else if (action === 'clear') service.clear(agent,ref)
  } catch (error: any) {
    if (domainErrors.has(error?.code)) return receipt(null,error.code)
    throw error
  }
  // State-only slice: creating or resuming the native goal never grants
  // unattended continuation. No await between transition and disarm.
  const view = service.disarm(agent)
  // Acknowledgement only after the official session persistence checkpoint.
  // A failed flush has an ambiguous outcome; caller must re-read, not replay.
  await ctx.sessions.flush(agent.session)
  if (view?.activation === 'armed') throw new Error('Goal disarm failed')
  return receipt(view)
}
