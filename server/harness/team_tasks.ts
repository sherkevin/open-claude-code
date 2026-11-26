/** Owner-authorized metadata mutations on the pinned official Team board.
 *
 * This seam is mounted only by a profile that explicitly enables both the
 * reviewed read-only Team service and this narrower task switch. It calls the
 * official `agentTeams` service and reads the official session projection; it
 * never builds a second task board, enables Team tools, spawns a subagent,
 * starts a model turn or touches a business system.
 *
 * Every call is bound to the configured native session and an idle root agent.
 * Inputs are exact (no unknown keys). Only validated, allowlisted fields are
 * returned to Python; task projection extras are dropped rather than exposed.
 */
const SOURCE_COMMIT = 'fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159'
const MAX_TASKS = 256
const MAX_BOARD_BYTES = 1024 * 1024
const MAX_SUBJECT_CHARS = 200
const MAX_DESCRIPTION_CHARS = 4000
const MAX_DEPENDENCIES = 64
const MAX_WRITE_SCOPES = 64
const MAX_WRITE_SCOPE_CHARS = 300
const TASK_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/

const isRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
const exactKeys = (value: Record<string, unknown>, keys: string[]): boolean => {
  const actual = Object.keys(value).sort().join(',')
  return actual === [...keys].sort().join(',')
}
const invalid = (): never => { throw new Error('TEAM_REQUEST_INVALID') }
const rejected = (code: string): never => { throw new Error(`TEAM_TASK_REJECTED:${code}`) }
const uncertain = (): never => { throw new Error('TEAM_TASK_UNCERTAIN') }

function stringList(value: unknown): string[] {
  if (!Array.isArray(value) || value.length > 64
      || value.some(item => typeof item !== 'string' || item.length > 500)) {
    return invalid()
  }
  return value as string[]
}

// A malformed projection may have been produced by a mutation that already
// committed; it must never be reported as a safe pre-mutation rejection.
function projectionList(value: unknown): string[] {
  if (!Array.isArray(value) || value.length > 64
      || value.some(item => typeof item !== 'string' || item.length > 500)) {
    return uncertain()
  }
  return value as string[]
}

function normalizeTask(value: unknown): any {
  if (!isRecord(value) || typeof value.id !== 'string' || !TASK_ID.test(value.id)
      || !Number.isSafeInteger(value.revision) || (value.revision as number) < 1
      || typeof value.subject !== 'string' || typeof value.description !== 'string'
      || !['pending', 'in_progress', 'completed'].includes(value.status as string)) {
    return uncertain()
  }
  const ownerId = value.ownerId === undefined || value.ownerId === null
    ? null : typeof value.ownerId === 'string' ? value.ownerId : uncertain()
  const ownerName = value.ownerName === undefined || value.ownerName === null
    ? null : typeof value.ownerName === 'string' ? value.ownerName : uncertain()
  const ready = value.ready === undefined ? false
    : typeof value.ready === 'boolean' ? value.ready : uncertain()
  return {
    id: value.id, revision: value.revision, subject: value.subject,
    description: value.description, status: value.status,
    blockedBy: value.blockedBy === undefined ? [] : projectionList(value.blockedBy),
    writeScopes: value.writeScopes === undefined ? [] : projectionList(value.writeScopes),
    ownerId, ownerName, ready,
    writeScopeWarnings: value.writeScopeWarnings === undefined
      ? [] : projectionList(value.writeScopeWarnings),
  }
}

/** Read and strictly normalize the official projection for one root agent. */
function currentBoard(ctx: any, agent: any): any {
  const snapshot = ctx.sessionProjections.snapshot(agent.session).values.agentTeam
  if (!isRecord(snapshot) || snapshot.failure !== undefined
      || typeof snapshot.enabled !== 'boolean' || !Array.isArray(snapshot.tasks)
      || snapshot.tasks.length > MAX_TASKS) return uncertain()
  const tasks = snapshot.tasks.map(normalizeTask)
  const ids = new Set<string>()
  for (const task of tasks) {
    if (ids.has(task.id)) return uncertain()
    ids.add(task.id)
  }
  const board = { enabled: snapshot.enabled, tasks }
  if (Buffer.byteLength(JSON.stringify(board), 'utf8') > MAX_BOARD_BYTES) return uncertain()
  return board
}

function taskById(board: any, taskId: string): any | undefined {
  return board.tasks.find((task: any) => task.id === taskId)
}

function officialFailure(error: unknown): never {
  const code = isRecord(error) && typeof error.code === 'string' ? error.code : ''
  if (/^TEAM_[A-Z0-9_]{1,64}$/.test(code)) return rejected(code)
  // The RPC may have committed before the failure was observed. Never treat an
  // unknown native error as a safe no-op.
  return uncertain()
}

function validatedText(value: unknown, maxChars: number, singleLine = false): string {
  if (typeof value !== 'string') return invalid()
  const text = value.trim()
  if (!text || text.length > maxChars) return invalid()
  if (Buffer.byteLength(text, 'utf8') > maxChars * 4) return invalid()
  const controls = singleLine ? /[\x00-\x1f\x7f]/ : /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/
  if (controls.test(text)) return invalid()
  return text
}

function validateCreate(params: Record<string, unknown>): any {
  if (!exactKeys(params, ['action', 'sessionId', 'subject', 'description', 'blockedBy', 'writeScopes'])) {
    return invalid()
  }
  const subject = validatedText(params.subject, MAX_SUBJECT_CHARS, true)
  const description = validatedText(params.description, MAX_DESCRIPTION_CHARS)
  const blockedBy = stringList(params.blockedBy)
  const writeScopes = stringList(params.writeScopes)
  if (blockedBy.length > MAX_DEPENDENCIES || writeScopes.length > MAX_WRITE_SCOPES
      || blockedBy.some(item => !TASK_ID.test(item))
      || writeScopes.some(item => !item.trim() || item.length > MAX_WRITE_SCOPE_CHARS
        || /[\x00-\x1f\x7f]/.test(item))) {
    return invalid()
  }
  return { subject, description, blockedBy, writeScopes }
}

function validateTransition(params: Record<string, unknown>): any {
  if (!exactKeys(params, ['action', 'sessionId', 'taskId', 'expectedRevision'])
      || typeof params.taskId !== 'string' || !TASK_ID.test(params.taskId)
      || !Number.isSafeInteger(params.expectedRevision) || (params.expectedRevision as number) < 1) {
    return invalid()
  }
  return { taskId: params.taskId, expectedRevision: params.expectedRevision }
}

function receipt(config: any, action: string, board: any, task: any | null): unknown {
  return {
    schema: 'workspace-team-task/v1', sourceCommit: SOURCE_COMMIT,
    sessionId: config.sessionId, action, boardEnabled: board.enabled,
    board, task, executionEnabled: false,
  }
}

export async function teamTaskRequest(
  ctx: any, config: any, params: unknown, initialized: boolean,
): Promise<unknown> {
  if (!initialized || config.teamTasks !== true || config.sourceCommit !== SOURCE_COMMIT
      || !isRecord(params) || params.sessionId !== config.sessionId
      || !['enable', 'disable', 'create', 'claim', 'complete'].includes(params.action as string)) {
    return invalid()
  }
  const action = params.action as string
  const agent = ctx.agents.get(config.sessionId)
  if (!agent || agent.id !== config.sessionId || agent.session?.id !== config.sessionId
      || agent.status !== 'idle' || ctx.subagents.rootOf(agent) !== agent) {
    throw new Error('TEAM_ROOT_UNAVAILABLE')
  }
  if (typeof ctx.agentTeams?.setEnabled !== 'function'
      || typeof ctx.agentTeams?.createTask !== 'function'
      || typeof ctx.agentTeams?.updateTask !== 'function') {
    throw new Error('TEAM_ROOT_UNAVAILABLE')
  }
  const before = currentBoard(ctx, agent)

  if (action === 'enable' || action === 'disable') {
    if (!exactKeys(params, ['action', 'sessionId'])) return invalid()
    const desired = action === 'enable'
    if (before.enabled !== desired) {
      try {
        await ctx.agentTeams.setEnabled(agent, desired)
      } catch (error) {
        return officialFailure(error)
      }
    }
    const after = currentBoard(ctx, agent)
    if (after.enabled !== desired) return uncertain()
    return receipt(config, action, after, null)
  }

  if (!before.enabled) return rejected('TEAM_BOARD_DISABLED')

  if (action === 'create') {
    const input = validateCreate(params)
    if (before.tasks.length >= MAX_TASKS) return rejected('TEAM_TASK_LIMIT')
    const boardIds = new Set(before.tasks.map((task: any) => task.id))
    if (input.blockedBy.some((id: string) => !boardIds.has(id))) return invalid()
    const arguments_: any = { subject: input.subject, description: input.description }
    if (input.blockedBy.length) arguments_.blockedBy = input.blockedBy
    if (input.writeScopes.length) arguments_.writeScopes = input.writeScopes
    let created: any
    try {
      created = await ctx.agentTeams.createTask(agent, arguments_)
    } catch (error) {
      return officialFailure(error)
    }
    if (!isRecord(created) || typeof created.id !== 'string' || !TASK_ID.test(created.id)) {
      return uncertain()
    }
    const after = currentBoard(ctx, agent)
    const task = taskById(after, created.id)
    if (!task || task.status !== 'pending') return uncertain()
    return receipt(config, action, after, task)
  }

  const transition = validateTransition(params)
  const current = taskById(before, transition.taskId)
  if (!current) return rejected('TEAM_TASK_NOT_FOUND')
  // The official service still performs the authoritative CAS; this pre-check
  // gives the product a stable stale code without sending a doomed mutation.
  if (current.revision !== transition.expectedRevision) {
    return rejected('TEAM_TASK_STALE_REVISION')
  }
  try {
    await ctx.agentTeams.updateTask(agent, {
      taskId: transition.taskId, expectedRevision: transition.expectedRevision, action,
    })
  } catch (error) {
    return officialFailure(error)
  }
  const after = currentBoard(ctx, agent)
  const task = taskById(after, transition.taskId)
  if (!task || task.revision <= transition.expectedRevision) return uncertain()
  const expected = action === 'claim' ? 'in_progress' : 'completed'
  if (task.status !== expected) return uncertain()
  return receipt(config, action, after, task)
}
