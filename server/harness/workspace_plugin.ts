/** Locked host bridge for the pinned official Harness, loaded by a dsh profile.
 * No tool performs business I/O in Node. The host authorizes and executes it.
 * Upstream source imports deliberately share dsh's source resolver/instances.
 */
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { installUsageObserver } from './usage_observer.ts'
import { forkWorkspaceSession } from './fork_session.ts'

export const name = 'zhixing-workspace-host'
export const inject = ['tools', 'agents', 'workingDirectory', 'loader', 'sdkAppStartup', 'tokenMeter', 'sessionProjections', 'commands', 'sessions', 'attachments']

const ALLOWED_TOOLS = new Set([
  'knowledge_search', 'knowledge_read', 'web_search', 'web_read', 'generate_xlsx', 'generate_image',
  'file_read', 'file_write', 'file_search',
  'ocr', 'image_search',
  'memory_list', 'memory_read', 'memory_write',
  'ask_user', 'review_plan',
  'delegate_tasks', 'schedule_create', 'schedule_list', 'schedule_view',
])
const EXTERNAL_TOOL_NAME = /^external_[0-9a-f]{32}$/
const SOURCE_COMMIT = 'fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159'
const MAX_TOOL_DEFINITION_BYTES = 524_288
const MAX_SCHEMA_DEPTH = 128
const MAX_ARGUMENT_BYTES = 1_048_576
const MAX_RESULT_BYTES = 2_097_152

const isRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)

/** Read only the bound, already-restored session; no prompt or request assembly. */
export function readWorkspaceContext(ctx: any, config: any, params: unknown, initialized: boolean): unknown {
  if (!initialized || !isRecord(params) || Object.keys(params).join(',') !== 'sessionId'
      || params.sessionId !== config.sessionId) throw new Error('Unauthorized context request')
  const agent = ctx.agents.get(config.sessionId)
  const session = agent?.session
  if (session?.id !== config.sessionId || !Number.isSafeInteger(session.seq)
      || session.seq < 1 || session.seq > 100_000) throw new Error('Context session unavailable')
  const header = session.requestHeader()?.config
  const selected = agent.model.current()
  const routeMatches = header !== undefined && selected !== undefined
    && ['provider', 'model', 'reasoningEffort'].every(key => header[key] === selected[key])
  const result: any = { schema: 'workspace-context/v1', sourceCommit: SOURCE_COMMIT,
    sessionId: config.sessionId, routeMatches, projectionSeq: session.seq - 1,
    totalTokens: null, observedPromptTokens: null, baselineKind: 'none', breakdown: null }
  if (!routeMatches) return result
  // Both reads are synchronous and share one log cut. Never return nodes,
  // headers, usage extras, adapter capacity defaults or the cumulative bill.
  const snapshot = ctx.sessionProjections.snapshot(session, ['contextPressure', 'contextBreakdown'])
  const measurement = ctx.tokenMeter.measure(session)
  if (snapshot.asOfSeq !== session.seq - 1 || measurement.logRevision !== session.seq) {
    throw new Error('Inconsistent context snapshot')
  }
  const numeric = (value: unknown): boolean => Number.isSafeInteger(value) && (value as number) >= 0 && (value as number) <= 1e12
  if (!['none', 'usage', 'estimated'].includes(measurement.baseline?.kind)) throw new Error('Invalid context measurement')
  if (measurement.baseline.kind !== 'none') {
    const pressure = snapshot.values.contextPressure
    const breakdown = snapshot.values.contextBreakdown
    if (!numeric(measurement.totalTokens) || !isRecord(pressure) || !isRecord(breakdown)
        || (pressure.pressureTokens !== undefined && !numeric(pressure.pressureTokens))
        || !['systemTokens', 'toolsTokens', 'messageTokens'].every(key => numeric(breakdown[key]))) {
      throw new Error('Invalid context measurement')
    }
    result.totalTokens = measurement.totalTokens
    // The fixed adapter defaults missing usage fields to zero. Its pressure
    // projection cannot prove original provider field presence, even nonzero
    // aggregates may combine real and defaulted buckets. Keep observation unknown.
    result.baselineKind = measurement.baseline.kind
    result.breakdown = { systemTokens: breakdown.systemTokens, toolsTokens: breakdown.toolsTokens,
      messageTokens: breakdown.messageTokens }
  }
  if (Buffer.byteLength(JSON.stringify(result), 'utf8') > 8192) throw new Error('Context result exceeds bound')
  return result
}

/** External schemas are opaque metadata, never executable code or resolvers. */
function assertLocalSchemaReferences(root: Record<string, unknown>): void {
  const pending: { value: unknown, depth: number }[] = [{ value: root, depth: 0 }]
  while (pending.length > 0) {
    const { value, depth } = pending.pop()!
    if (depth > MAX_SCHEMA_DEPTH) throw new Error('External tool schema exceeds the supported depth')
    if (isRecord(value)) {
      if (Object.hasOwn(value, '$id')) {
        throw new Error('External tool schemas cannot change the reference base URI')
      }
      for (const key of ['$ref', '$dynamicRef', '$recursiveRef']) {
        if (Object.hasOwn(value, key) && (typeof value[key] !== 'string' || !value[key].startsWith('#'))) {
          throw new Error('External tool schema references must stay local')
        }
      }
      for (const item of Object.values(value)) pending.push({ value: item, depth: depth + 1 })
    } else if (Array.isArray(value)) {
      for (const item of value) pending.push({ value: item, depth: depth + 1 })
    }
  }
}

/** Mount the official RPC server alongside only host-dispatched workspace tools. */
export async function apply(ctx: any, config: any): Promise<void> {
  if (config.sourceCommit !== SOURCE_COMMIT || typeof config.sessionId !== 'string'
      || !Array.isArray(config.tools) || !Number.isSafeInteger(config.maxSteps) || config.maxSteps < 1) {
    throw new Error('Invalid locked workspace profile')
  }
  const sourceImport = (path: string) => import(pathToFileURL(resolve(config.sourceDir, path)).href)
  const [{ JsonRpcLineTransport }, { HarnessSdkJsonRpcServer }, schema, { createUserMessage }] = await Promise.all([
    sourceImport('packages/sdk/protocol/src/index.ts'),
    sourceImport('packages/sdk/server/src/server.ts'),
    sourceImport('packages/core/tools/src/json-schema.ts'),
    sourceImport('packages/llm/llm/src/message.ts'),
  ])
  const transport = new JsonRpcLineTransport(process.stdin, process.stdout)
  const server = new HarnessSdkJsonRpcServer(ctx, transport, { maxTokensAsSuccess: false })
  const allowed = new Set<string>()
  const lifetime = new AbortController()
  const owned = (agent: any): boolean => agent?.session?.id === config.sessionId
  const streamFinishes = new Map<string, boolean>()
  const streamRequests = new Map<string, string>()
  const requestVisibleText = new Map<string, boolean>()
  const recoveryTurns = new Set<number>()
  let currentTurn: number | undefined
  let turnWithTool: number | undefined
  let turnWithVisibleText: number | undefined
  let initialized = false
  let acceptingSteer = false
  const steerMessages = new Map<string, string>()
  const steerRequests = new Set<string>()
  ctx.on('session/event', (session: any, event: any) => {
    if (session.id !== config.sessionId) return
    if (event.type === 'turn/end') acceptingSteer = false
    if (event.type !== 'user/message') return
    const nativeId = event.data?.id
    const requestId = steerMessages.get(nativeId)
    if (requestId !== undefined) {
      steerMessages.delete(nativeId)
      transport.notify('workspace.steer', { sessionId: config.sessionId, requestId })
    }
  })
  const usage = installUsageObserver({ endpoint: config.usageEndpoint, sessionId: config.sessionId,
    onReceipt: receipt => transport.notify('workspace.usage', receipt) })
  ctx.effect(() => () => usage.restore())

  for (const definition of config.tools) {
    if (!isRecord(definition) || Object.keys(definition).sort().join(',') !== 'description,input_schema,name'
        || typeof definition.name !== 'string' || typeof definition.description !== 'string'
        || definition.description.length === 0 || !isRecord(definition.input_schema)
        || definition.input_schema.type !== 'object'
        || Buffer.byteLength(JSON.stringify(definition), 'utf8') > MAX_TOOL_DEFINITION_BYTES) {
      throw new Error('Invalid workspace tool metadata')
    }
    const external = definition.name.length === 41 && EXTERNAL_TOOL_NAME.test(definition.name)
    if ((!ALLOWED_TOOLS.has(definition.name) && !external) || allowed.has(definition.name)) {
      throw new Error('Unreviewed or duplicate workspace tool')
    }
    // Upstream's strict schema subset rejects $defs/$ref and common MCP
    // constraints. Raw registration preserves those schemas for the model;
    // Python validates every external call before any approval or operation.
    if (external) assertLocalSchemaReferences(definition.input_schema)
    else schema.assertObjectJsonSchema(definition.input_schema)
    allowed.add(definition.name)
    ctx.tools.register({
      name: definition.name,
      description: definition.description,
      parameters: definition.input_schema,
      output: {
        schema: {
          type: 'object', properties: { text: { type: 'string' } }, required: ['text'], additionalProperties: false,
        },
        render: (_arguments: unknown, value: { text: string }) => [{ type: 'text', text: value.text }],
      },
      async execute(arguments_: unknown, exec: any) {
        if (!initialized || !owned(exec.agent)) throw new Error('Workspace tool session is not authorized')
        if (!isRecord(arguments_)
            || (!external && schema.validateJsonSchemaValue(definition.input_schema, arguments_).length)
            || Buffer.byteLength(JSON.stringify(arguments_), 'utf8') > MAX_ARGUMENT_BYTES) {
          throw new Error('Workspace tool arguments are invalid')
        }
        turnWithTool = currentTurn
        const timeout = definition.name === 'delegate_tasks' ? config.delegationTimeoutMs ?? config.toolTimeoutMs : config.toolTimeoutMs
        const signal = AbortSignal.any([exec.signal, lifetime.signal, AbortSignal.timeout(timeout)])
        const cancellation = () => transport.notify('workspace.tool_cancelled', {
          sessionId: config.sessionId, callId: exec.callId,
        })
        signal.addEventListener('abort', cancellation, { once: true })
        try {
          const result: any = await transport.request('workspace/tool', {
            schema: 'workspace-tool/v1', sessionId: config.sessionId, callId: exec.callId,
            toolName: definition.name, arguments: arguments_,
          }, signal)
          signal.throwIfAborted()
          if (result === null || typeof result !== 'object' || !Array.isArray(result.content)
              || result.content.some((item: any) => item?.type !== 'text' || typeof item.text !== 'string')
              || Buffer.byteLength(JSON.stringify(result), 'utf8') > MAX_RESULT_BYTES) {
            throw new Error('Invalid workspace host response')
          }
          const text = result.content.map((item: any) => item.text).join('\n')
          if (result.is_error === true || result.isError === true) throw new Error(text || 'Workspace tool denied')
          return { text }
        } finally {
          signal.removeEventListener('abort', cancellation)
        }
      },
    })
  }
  // Registry restrictions supplement the explicit composition: a mistakenly
  // loaded native tool must never gain execution from a model's invented name.
  ctx.tools.guard((exec: any) => owned(exec.agent) && allowed.has(exec.name)
    ? undefined : 'Tool is outside the locked workspace capability set')

  ctx.on('agent/pre-step', async ({ agent, step }: any, next: any) => {
    if (!owned(agent)) throw new Error('Foreign Harness session refused')
    if (step > config.maxSteps) {
      transport.notify('workspace.limit', { sessionId: config.sessionId, code: 'MAX_STEPS' })
      throw new Error('Workspace model step limit reached')
    }
    return next()
  })
  ctx.on('agent/assistant-stream', ({ agent, frame }: any) => {
    if (!owned(agent)) return
    const common = { sessionId: config.sessionId, stepId: frame.attemptId, revision: frame.revision }
    if (frame.type === 'start') {
      currentTurn = frame.turn
      acceptingSteer = true
      streamFinishes.set(frame.attemptId, false)
      const requestKey = `${frame.turn}:${frame.step}`
      streamRequests.set(frame.attemptId, requestKey)
      requestVisibleText.set(requestKey, false)
      transport.notify('workspace.stream', { ...common, event: 'start', turn: frame.turn, step: frame.step })
    } else if (frame.type === 'chunk' && frame.chunk.type === 'text-delta') {
      const requestKey = streamRequests.get(frame.attemptId)
      if (requestKey !== undefined && frame.chunk.text.length > 0) {
        requestVisibleText.set(requestKey, true)
        turnWithVisibleText = currentTurn
      }
      transport.notify('workspace.stream', { ...common, event: 'text_delta', text: frame.chunk.text })
    } else if (frame.type === 'chunk' && frame.chunk.type === 'finish') {
      streamFinishes.set(frame.attemptId, true)
    } else if (frame.type === 'end') {
      const finishObserved = streamFinishes.get(frame.attemptId) === true
      streamFinishes.delete(frame.attemptId)
      streamRequests.delete(frame.attemptId)
      transport.notify('workspace.stream', { ...common, event: 'end', outcome: frame.outcome, finishObserved })
    }
  })

  // Gate the official request-level recovery before its retry listener. Retrying
  // this request preserves earlier tool results; resending the whole turn does not.
  ctx.on('agent/request-error', async ({ agent, turn, step, failure, signal }: any, next: any) => {
    const overflow = failure?.code === 'CONTEXT_WINDOW_EXCEEDED'
    if (!owned(agent) || signal.aborted || lifetime.signal.aborted
        || (!overflow && !(config.requestRetryCodes ?? ['TRANSPORT']).includes(failure?.code)) || recoveryTurns.has(turn)
        || (overflow && (turn === turnWithTool || turn === turnWithVisibleText))
        || requestVisibleText.get(`${turn}:${step}`) !== false) return
    const result = await next()
    if (result?.kind === 'retry') recoveryTurns.add(turn)
    return result
  }, { prepend: true })
  ctx.on('workspace/compaction-result', (agent: any, result: any) => {
    if (owned(agent) && initialized && !lifetime.signal.aborted) {
      transport.notify('workspace.compaction', { sessionId: config.sessionId, ...result })
    }
  })

  let closing: Promise<void> | undefined
  const close = (): Promise<void> => closing ??= (async () => {
    lifetime.abort()
    await server.shutdown()
    await transport.flush()
    await ctx.root.fiber.dispose()
    process.exit(0)
  })()

  transport.onRequest(async (method: string, params: any) => {
    if (method === 'initialize') {
      await ctx.loader.await()
      const registered = ctx.tools.definitions().map((tool: any) => tool.name)
      if (registered.length !== allowed.size || registered.some((tool: string) => !allowed.has(tool))) {
        throw new Error('Unexpected tools in locked Harness profile')
      }
      if (initialized || resolve(params.cwd ?? '') !== config.cwd || params.provider !== 'deepseek-official'
          || params.model !== config.model || params.approvalRequests !== true) {
        throw new Error('Harness initialization does not match its pinned workspace')
      }
      const result = await server.handleRequest(method, params)
      initialized = true
      return result
    }
    if (method === 'workspace/inspect') {
      return { schema: 'workspace-profile/v1', sourceCommit: SOURCE_COMMIT, sessionId: config.sessionId,
        tools: [...allowed], maxSteps: config.maxSteps, initialized }
    }
    if (method === 'workspace/usage') {
      if (!initialized || !isRecord(params) || Object.keys(params).sort().join(',') !== 'action,runId,sessionId'
          || params.sessionId !== config.sessionId || typeof params.runId !== 'string'
          || !/^[A-Za-z0-9_-]{1,128}$/u.test(params.runId)
          || !['begin', 'snapshot'].includes(params.action as string)) {
        throw new Error('Unauthorized usage receipt request')
      }
      if (params.action === 'begin') usage.begin(params.runId)
      return { schema: 'workspace-usage/v1', sessionId: config.sessionId, ...usage.snapshot(params.runId) }
    }
    if (method === 'shutdown') {
      const result = await server.handleRequest(method, params)
      setImmediate(() => { void close() })
      return result
    }
    if (method === 'workspace/context') return readWorkspaceContext(ctx, config, params, initialized)
    if (method === 'workspace/fork' && initialized) return forkWorkspaceSession(ctx, config, params)
    if (method === 'workspace/steer') {
      if (!initialized || !isRecord(params) || Object.keys(params).sort().join(',') !== 'requestId,sessionId,text'
          || params.sessionId !== config.sessionId || typeof params.requestId !== 'string' || !/^[a-f0-9]{32}$/u.test(params.requestId)
          || typeof params.text !== 'string' || params.text.length > 8000 || !params.text.trim()) {
        throw new Error('Invalid current-turn input')
      }
      if (steerRequests.has(params.requestId)) return { accepted: true }
      const agent = ctx.agents.get(config.sessionId)
      if (!acceptingSteer || agent?.status !== 'running' || !owned(agent) || steerRequests.size >= 10) return { accepted: false }
      const message = createUserMessage({ content: [{ type: 'text', text: params.text }], source: { kind: 'user' } })
      steerRequests.add(params.requestId)
      steerMessages.set(message.id, params.requestId)
      agent.steer(message)
      return { accepted: true }
    }
    if (method === 'workspace/compact') {
      if (!initialized || !isRecord(params)
          || Object.keys(params).sort().join(',') !== 'instructions,sessionId'
          || params.sessionId !== config.sessionId || typeof params.instructions !== 'string'
          || params.instructions.length > 2000 || Buffer.byteLength(params.instructions, 'utf8') > 8192
          || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/u.test(params.instructions)) {
        throw new Error('Unauthorized or invalid compaction request')
      }
      const agent = ctx.agents.get(config.sessionId)
      if (agent?.session?.id !== config.sessionId) throw new Error('Compaction session unavailable')
      const focus = params.instructions.trim()
      const execution = await ctx.commands.execute(
        agent, focus.length === 0 ? '/compact' : `/compact ${focus}`, [], lifetime.signal,
      )
      if (execution === undefined) throw new Error('Compaction command unavailable')
      return execution
    }
    if (!initialized || !['session/prompt', 'session/resume', 'session/wait'].includes(method)
        || params.sessionId !== config.sessionId) {
      throw new Error('Unsupported or unauthorized workspace RPC request')
    }
    if (method === 'session/prompt') {
      let imageCount = 0, imageBytes = 0
      const valid = Array.isArray(params.contentBlocks) && params.contentBlocks.length > 0
        && params.contentBlocks.length <= 41 && params.contentBlocks.every((block: any) => {
          if (block?.type === 'text') return typeof block.text === 'string'
            && Object.keys(block).every(key => ['type', 'text'].includes(key))
          if (block?.type !== 'image' || typeof block.data !== 'string'
              || !['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(block.mimeType)
              || Object.keys(block).some(key => !['type', 'data', 'mimeType', 'name'].includes(key))
              || (block.name !== undefined && (typeof block.name !== 'string' || block.name.length > 240
                || /[\\/\\\\\x00-\x1f]/.test(block.name)))
              || block.data.length === 0 || block.data.length > 4 * Math.ceil(5 * 1024 * 1024 / 3)
              || block.data.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(block.data)) return false
          const bytes = Buffer.from(block.data, 'base64')
          if (bytes.toString('base64') !== block.data || bytes.length > 5 * 1024 * 1024) return false
          imageCount += 1
          imageBytes += bytes.length
          return imageCount <= 20 && imageBytes <= 10 * 1024 * 1024
        })
      if (!valid) throw new Error('Workspace prompts accept literal text and bounded encoded raster images only')
    }
    return server.handleRequest(method, params)
  })
  ctx.effect(() => {
    transport.start()
    return async () => {
      lifetime.abort()
      await server.shutdown()
      transport.close()
    }
  }, 'workspace.jsonrpc')
}
