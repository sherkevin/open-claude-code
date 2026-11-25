/**
 * B2.3 isolated probe: the real pinned official Goal round driver with a
 * synthetic host lease and a synthetic LLM. No product code, scheduler,
 * network, real model, customer data, or production process is involved.
 *
 * What it proves:
 *   1. The official `agent/pre-step` hook is a usable host-admission seam:
 *      an unauthorized positive goal round is vetoed before it can enter
 *      history, and no model request is dispatched.
 *   2. With one explicit synthetic authorization exactly one round is
 *      admitted; the next automatic round is refused by the host cap.
 *   3. Native `roundsStarted`, `revision` and activation come from the real
 *      official Goal service/projection, not a fake driver.
 *   4. A stale authorization (old revision) cannot admit a resumed goal.
 *   5. Unknown authoritative usage fails closed: automatic continuation to a
 *      second round is refused and no settled cost is ever fabricated.
 *   6. Unloading/rebuilding the driver never replays a round; explicit
 *      re-authorization plus resume is required.
 *
 * Required pinned source (commit fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159):
 *   packages/goal/goal-round-driver/src/index.ts
 *   packages/goal/goal/src/index.ts
 *   packages/core/agent-loop/src/index.ts
 *   packages/test-support/agent-loop-testkit/src/index.ts
 *   packages/core/session/src/index.ts
 *   packages/core/agent/src/index.ts
 *   packages/llm/llm/src/index.ts
 *   node_modules/tsx/dist/esm/index.mjs   (tsx ESM resolver)
 *   tsconfig.json                        (TSX_TSCONFIG_PATH)
 *
 * Run (all temporary paths stay under data3):
 *   SOURCE=/media/data3/dengkw/projects/open-claude-code/.runtime/third_party/deepseek-harness
 *   NODE=/media/data3/dengkw/projects/open-claude-code/.runtime/harness-toolchain/node-v24.9.0-linux-x64/bin/node
 *   TSX_TSCONFIG_PATH="$SOURCE/tsconfig.json" "$NODE" \
 *     --import "$SOURCE/node_modules/tsx/dist/esm/index.mjs" \
 *     scripts/oss/harness_goal_native_driver_seam_probe.mts \
 *     --source "$SOURCE" --work-dir /media/data3/dengkw/acceptance/kaiwu-goal-native-driver-seam-20261126
 *
 * `--print-requirements` prints the exact module list and resolver command.
 */

import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { existsSync, realpathSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const PIN = 'fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159'
const SOURCE_COMMIT_SCHEMA = 'goal-native-driver-seam-report/v1'

/** Official package entry points this probe imports by absolute file URL. */
const REQUIRED_MODULES = [
  'vendor/cordis/src/index.ts',
  'packages/goal/goal-round-driver/src/index.ts',
  'packages/goal/goal/src/index.ts',
  'packages/core/agent-loop/src/index.ts',
  'packages/test-support/agent-loop-testkit/src/index.ts',
  'packages/core/session/src/index.ts',
  'packages/core/agent/src/index.ts',
  'packages/llm/llm/src/index.ts',
] as const

/** Exact pinned paths used when a manifest name or export condition differs. */
const FALLBACK_ENTRIES: Readonly<Record<string, string | undefined>> = {
  '@deepseek-ai/cordis': 'vendor/cordis/src/index.ts',
  '@deepseek-ai/dsh-goal-round-driver': 'packages/goal/goal-round-driver/src/index.ts',
  '@deepseek-ai/dsh-goal': 'packages/goal/goal/src/index.ts',
  '@deepseek-ai/dsh-agent-loop': 'packages/core/agent-loop/src/index.ts',
  '@deepseek-ai/dsh-agent-loop-testkit': 'packages/test-support/agent-loop-testkit/src/index.ts',
  '@deepseek-ai/dsh-session': 'packages/core/session/src/index.ts',
  '@deepseek-ai/dsh-agent': 'packages/core/agent/src/index.ts',
  '@deepseek-ai/dsh-llm': 'packages/llm/llm/src/index.ts',
}

interface Options {
  readonly source: string
  readonly workDir: string
  readonly printRequirements: boolean
}

interface ScenarioResult {
  readonly name: string
  readonly ok: boolean
  readonly error?: string
  readonly modelRequests?: number
  readonly positiveRounds?: number[]
  readonly checkpoint?: unknown
}

/** Module bindings resolved from the pinned source; assigned once at startup. */
let Context: any
let AgentLoop: any
let GoalService: any
let SessionId: any
let LlmAdapter: any
let agentEvents: any
let mountAgentLoopTestDependencies: any
let goalRoundDriver: any

/** Parse the small CLI surface without importing a general-purpose parser. */
function parseOptions(argv: readonly string[]): Options {
  let source: string | undefined
  let workDir: string | undefined
  let printRequirements = false
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index]
    if (argument === '--source') source = argv[++index]
    else if (argument === '--work-dir') workDir = argv[++index]
    else if (argument === '--print-requirements') printRequirements = true
    else throw new Error(`Unknown probe argument: ${argument}`)
  }
  if (source === undefined) throw new Error('--source is required')
  if (workDir === undefined && !printRequirements) throw new Error('--work-dir is required')
  return { source: resolve(source), workDir: resolve(workDir ?? '.'), printRequirements }
}

/** Resolve the exact reviewed source entries; never substitute a built artifact. */
function resolveOfficialPackage(source: string, name: string): string {
  const relative = FALLBACK_ENTRIES[name]
  if (!relative || !existsSync(resolve(source, relative))) throw new Error(`Pinned source is missing ${name}: ${relative}`)
  return resolve(source, relative)
}

/** Refuse to run against anything but the reviewed commit and a clean tree. */
function verifyPin(source: string): string {
  const head = execFileSync('git', ['-C', source, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim()
  if (head !== PIN) throw new Error(`Pinned source HEAD ${head} does not match ${PIN}`)
  // `git diff --quiet` exits non-zero when tracked source is modified.
  execFileSync('git', ['-C', source, 'diff', '--quiet', 'HEAD', '--'], { encoding: 'utf8' })
  return head
}

/** Load every official module once, by absolute source URL. */
async function loadOfficialModules(source: string): Promise<void> {
  const driverEntry = resolveOfficialPackage(source, '@deepseek-ai/dsh-goal-round-driver')
  const resolveFromDriver = (name: string) => resolveOfficialPackage(source, name)
  const [cordis, agentLoopModule, goalModule, llmModule, sessionModule, agentModule, testkitModule, driverModule] =
    await Promise.all([
      import(pathToFileURL(resolveFromDriver('@deepseek-ai/cordis')).href),
      import(pathToFileURL(resolveFromDriver('@deepseek-ai/dsh-agent-loop')).href),
      import(pathToFileURL(resolveFromDriver('@deepseek-ai/dsh-goal')).href),
      import(pathToFileURL(resolveFromDriver('@deepseek-ai/dsh-llm')).href),
      import(pathToFileURL(resolveFromDriver('@deepseek-ai/dsh-session')).href),
      import(pathToFileURL(resolveFromDriver('@deepseek-ai/dsh-agent')).href),
      import(pathToFileURL(resolveFromDriver('@deepseek-ai/dsh-agent-loop-testkit')).href),
      import(pathToFileURL(driverEntry).href),
    ])
  // CJS interop is possible depending on the pinned package build; resolve the
  // named export first and fall back to the module default.
  const named = (module: any, name: string): any => module?.[name] ?? module?.default?.[name]
  Context = named(cordis, 'Context')
  AgentLoop = agentLoopModule.default ?? agentLoopModule.AgentLoop
  GoalService = goalModule.default ?? goalModule.GoalService
  SessionId = named(sessionModule, 'SessionId')
  LlmAdapter = named(llmModule, 'LlmAdapter')
  agentEvents = named(agentModule, 'agentEvents')
  mountAgentLoopTestDependencies = named(testkitModule, 'mountAgentLoopTestDependencies')
  goalRoundDriver = typeof driverModule.apply === 'function' ? driverModule : driverModule.default
  const requiredExports = [
    ['@deepseek-ai/cordis#Context', Context],
    ['@deepseek-ai/dsh-agent-loop#default', AgentLoop],
    ['@deepseek-ai/dsh-goal#default', GoalService],
    ['@deepseek-ai/dsh-session#SessionId', SessionId],
    ['@deepseek-ai/dsh-llm#LlmAdapter', LlmAdapter],
    ['@deepseek-ai/dsh-agent#agentEvents', agentEvents],
    ['@deepseek-ai/dsh-agent-loop-testkit#mountAgentLoopTestDependencies', mountAgentLoopTestDependencies],
    ['@deepseek-ai/dsh-goal-round-driver#apply', goalRoundDriver.apply],
  ] as const
  for (const [label, value] of requiredExports) {
    if (typeof value !== 'function') throw new Error(`Pinned Harness export ${label} is not a function`)
  }
}

/** One successful assistant text response for the synthetic adapter. */
function textResponse(text: string): any[] {
  return [
    { type: 'block-start', index: 0, blockType: 'text' },
    { type: 'block-end', index: 0, block: { type: 'text', text } },
    { type: 'finish', reason: { kind: 'stop' } },
  ]
}

/** In-process model script; it never opens a socket or reads a credential. */
interface ScriptedAdapter {
  readonly requests: any[]
  stream(options: any): AsyncIterable<any>
}

/**
 * Build the adapter only after the pinned `LlmAdapter` base has been assigned.
 * A top-level class declaration would evaluate before the dynamic import.
 */
function createScriptedAdapter(script: any[][]): ScriptedAdapter {
  return new (class extends LlmAdapter {
    readonly requests: any[] = []

    constructor(private readonly scriptEntries: any[][]) {
      super()
    }

    override async * stream(options: any): AsyncIterable<any> {
      this.requests.push(options)
      const entry = this.scriptEntries.shift()
      if (entry === undefined) throw new Error('ScriptedAdapter: script exhausted')
      for (const chunk of entry) yield chunk
    }
  })(script)
}

interface HostAuthorization {
  readonly authorizationId: string
  readonly goalId: string
  readonly goalRevision: number
  readonly maxRounds: number
  readonly runId: string
  readonly leaseId: string
  readonly createdAt: string
  revoked: boolean
}

interface RoundCheckpoint {
  readonly authorizationId: string
  readonly leaseId: string
  readonly runId: string
  readonly goalId: string
  readonly goalRevision: number
  readonly roundIndex: number
  readonly admittedAt: string
  readonly modelRequestsAtAdmission: number
  readonly usageStatus: 'unknown'
}

interface HostRejection {
  readonly round: number
  readonly code: string
}

/**
 * Synthetic host lease. It is a policy fence, not a driver or scheduler: the
 * official driver still reserves and queues the round, and this fence either
 * lets the proposed step enter history or vetoes it at `agent/pre-step`.
 *
 * `maxRounds` is the number of rounds one explicit authorization may admit.
 * A second automatic round under the same authorization is always refused:
 * either by the round cap, or by `GOAL_USAGE_UNVERIFIED` because the synthetic
 * model has no authoritative cost source.
 */
class SyntheticHostLease {
  private authorization: HostAuthorization | undefined
  private readonly checkpoints: RoundCheckpoint[] = []
  private readonly rejections: HostRejection[] = []
  private readonly admittedRounds = new Set<number>()
  private admitted = 0
  readonly flushes: number[] = []
  readonly usageStatus = 'unknown' as const

  constructor(
    private readonly ctx: any,
    private readonly adapter: { requests: unknown[] },
    private readonly checkpointFile: string,
  ) {}

  /** Issue one explicit authorization bound to the exact current revision. */
  authorize(goal: any, options: { readonly maxRounds: number; readonly runId?: string }): HostAuthorization {
    this.authorization = {
      authorizationId: `auth-${randomUUID()}`,
      goalId: goal.id,
      goalRevision: goal.revision,
      maxRounds: options.maxRounds,
      runId: options.runId ?? `run-${randomUUID()}`,
      leaseId: `lease-${randomUUID()}`,
      createdAt: new Date().toISOString(),
      revoked: false,
    }
    this.admitted = 0
    this.admittedRounds.clear()
    this.persist()
    return this.authorization
  }

  /** Read the exact live native goal and apply the host admission rules. */
  private fence(agent: any, source: any): { readonly admitted: true } | { readonly admitted: false; readonly code: string } {
    const authorization = this.authorization
    if (authorization === undefined) return { admitted: false, code: 'GOAL_AUTHORIZATION_MISSING' }
    if (authorization.revoked) return { admitted: false, code: 'GOAL_AUTHORIZATION_REVOKED' }
    const goal = this.ctx.goals.get(agent)
    if (goal === undefined || goal.id !== authorization.goalId || goal.revision !== authorization.goalRevision) {
      return { admitted: false, code: 'GOAL_STALE_REVISION' }
    }
    if (goal.phase !== 'active' || goal.activation !== 'armed') {
      return { admitted: false, code: 'GOAL_NOT_ACTIVE' }
    }
    if (source.goalId !== goal.id || source.revision !== goal.revision) {
      return { admitted: false, code: 'GOAL_MESSAGE_REVISION_MISMATCH' }
    }
    // Mirrors the official strict fold: only the exact next native round.
    if (source.round !== goal.roundsStarted + 1) {
      return { admitted: false, code: 'GOAL_NATIVE_ROUND_FENCE' }
    }
    if (this.admittedRounds.has(source.round)) {
      return { admitted: false, code: 'GOAL_DUPLICATE_ROUND' }
    }
    if (this.admitted >= authorization.maxRounds) {
      return { admitted: false, code: 'GOAL_ROUND_LIMIT' }
    }
    // Automatic continuation requires a settled, authoritative usage receipt.
    // The synthetic model never supplies one, so a second automatic round is
    // refused rather than being priced as a fake zero.
    if (this.admitted >= 1 && this.usageStatus !== 'settled') {
      return { admitted: false, code: 'GOAL_USAGE_UNVERIFIED' }
    }
    return { admitted: true }
  }

  /** Downstream pre-step hook; registered after the official driver. */
  readonly preStep = async ({ agent, messages }: any, next: any): Promise<any> => {
    const submitted = messages.find((message: any) => message.source?.kind === 'goal' && message.source.round > 0)
    if (submitted === undefined) return next()
    const source = submitted.source
    const decision = this.fence(agent, source)
    if (!decision.admitted) {
      this.rejections.push({ round: source.round, code: decision.code })
      this.persist()
      return { kind: 'reject' as const }
    }
    const downstream = await next()
    if (downstream.kind === 'enter') {
      const authorization = this.authorization!
      this.admitted += 1
      this.admittedRounds.add(source.round)
      this.checkpoints.push({
        authorizationId: authorization.authorizationId,
        leaseId: authorization.leaseId,
        runId: authorization.runId,
        goalId: source.goalId,
        goalRevision: source.revision,
        roundIndex: source.round,
        admittedAt: new Date().toISOString(),
        modelRequestsAtAdmission: this.adapter.requests.length,
        usageStatus: this.usageStatus,
      })
      this.persist()
    }
    return downstream
  }

  /** Durable host-side round record; the official session log stays authoritative. */
  private persist(): void {
    writeFileSync(this.checkpointFile, `${JSON.stringify({
      schema: 'host-goal-round-checkpoint/v1',
      authorization: this.authorization ?? null,
      rounds: this.checkpoints,
      rejections: this.rejections,
      usageStatus: this.usageStatus,
      settledCostMicros: null,
    }, null, 2)}\n`)
  }

  report(): unknown {
    return {
      authorization: this.authorization ?? null,
      rounds: this.checkpoints,
      rejections: this.rejections,
      usageStatus: this.usageStatus,
      settledCostMicros: null,
    }
  }
}

interface Harness {
  readonly ctx: any
  readonly driver: any
  readonly adapter: ScriptedAdapter
  readonly agent: any
  readonly lease: SyntheticHostLease
  readonly removeFence: () => boolean
}

/** Mount the official loop/testkit, Goal service, driver, and synthetic model. */
async function withHarness(
  label: string,
  script: any[][],
  workDir: string,
  body: (harness: Harness) => Promise<unknown>,
): Promise<unknown> {
  const ctx = new Context()
  try {
    await mountAgentLoopTestDependencies(ctx)
    await ctx.plugin(GoalService)
    const driver = await ctx.plugin(goalRoundDriver)
    await ctx.plugin(AgentLoop, { agents: [] })
    const adapter = createScriptedAdapter(script)
    ctx.llm.registerAdapter(['mock'], adapter)
    const agent = await ctx.agentLoop.create(SessionId(`goal-seam-${label}-${randomUUID()}`), {
      provider: 'mock',
      model: 'mock',
    })
    const lease = new SyntheticHostLease(ctx, adapter, join(workDir, `${label}-checkpoint.json`))
    // Registered after the official driver, so it runs inside the driver's
    // own `next()` and its veto is observed by the driver as a step rejection.
    const removeFence = ctx.on('agent/pre-step', lease.preStep)
    ctx.on('session/flush', () => { lease.flushes.push(adapter.requests.length) })
    return await body({ ctx, driver, adapter, agent, lease, removeFence })
  } finally {
    await ctx.fiber.dispose()
  }
}

/** Throwing assertion that names the exact refused property. */
function expect(name: string, condition: boolean, detail?: unknown): void {
  if (condition) return
  throw new Error(`${name}${detail === undefined ? '' : `: ${JSON.stringify(detail)}`}`)
}

/** Poll the real native goal view without a fake projection. */
async function waitForGoal(
  ctx: any,
  agent: any,
  predicate: (goal: any) => boolean,
  label: string,
  timeoutMs = 10_000,
): Promise<any> {
  const deadline = Date.now() + timeoutMs
  let last: any
  while (Date.now() < deadline) {
    last = ctx.goals.get(agent)
    if (predicate(last)) return last
    await new Promise((resolveWait) => setTimeout(resolveWait, 10))
  }
  throw new Error(`Timed out waiting for ${label}; last goal=${JSON.stringify(last)}`)
}

/** Positive goal rounds durably admitted into the real session log. */
function positiveRounds(agent: any): number[] {
  return agent.session.snapshotEvents()
    .filter((event: any) => event.type === 'user/message'
      && event.data?.source?.kind === 'goal'
      && event.data.source.round > 0)
    .map((event: any) => event.data.source.round)
}

async function unauthorizedPositiveRound(workDir: string): Promise<unknown> {
  return withHarness('unauthorized-positive-round', [textResponse('must not run')], workDir, async ({ ctx, adapter, agent, lease }) => {
    const goal = ctx.goals.create(agent, { objective: 'An unauthorized round must be refused', maxGoalRounds: 8 })
    const blocked = await waitForGoal(ctx, agent, (current) => current?.phase === 'blocked', 'unauthorized round block')
    expect('unauthorized round blocks the goal through the official driver', blocked.blockedReason?.code === 'prompt-rejected', blocked.blockedReason)
    expect('unauthorized round dispatches no model request', adapter.requests.length === 0)
    expect('unauthorized round admits no positive user/message', positiveRounds(agent).length === 0, positiveRounds(agent))
    expect('unauthorized round preserves native roundsStarted=0', blocked.roundsStarted === 0)
    expect('unauthorized round records the host rejection code', lease.report() !== undefined
      && (lease.report() as any).rejections.some((rejection: HostRejection) => rejection.code === 'GOAL_AUTHORIZATION_MISSING'))
    expect('unauthorized round blocks through exactly one official revision edge', blocked.revision === goal.revision + 1, blocked)
    return { modelRequests: adapter.requests.length, positiveRounds: positiveRounds(agent), checkpoint: lease.report() }
  })
}

async function singleAuthorizedRoundThenHostCap(workDir: string): Promise<unknown> {
  return withHarness('single-authorized-round', [textResponse('round one')], workDir, async ({ ctx, adapter, agent, lease }) => {
    const goal = ctx.goals.create(agent, { objective: 'Exactly one explicitly authorized round', maxGoalRounds: 8 })
    lease.authorize(goal, { maxRounds: 1 })
    const blocked = await waitForGoal(ctx, agent, (current) => current?.phase === 'blocked', 'single round block')
    expect('single authorized round dispatches exactly one model request', adapter.requests.length === 1, adapter.requests.length)
    expect('single authorized round advances native roundsStarted to 1', blocked.roundsStarted === 1, blocked.roundsStarted)
    expect('single authorized round admits exactly one positive user/message', JSON.stringify(positiveRounds(agent)) === '[1]', positiveRounds(agent))
    expect('single authorized round writes one host checkpoint', (lease.report() as any).rounds.length === 1
      && (lease.report() as any).rounds[0].roundIndex === 1)
    expect('next automatic round is refused by the host round cap', (lease.report() as any).rejections
      .some((rejection: HostRejection) => rejection.round === 2 && rejection.code === 'GOAL_ROUND_LIMIT'))
    expect('next automatic round dispatches no second model request', adapter.requests.length === 1)
    expect('official driver maps the host veto to a blocked goal', blocked.blockedReason?.code === 'prompt-rejected', blocked.blockedReason)
    expect('official driver flushes the settled round checkpoint before the next round', lease.flushes.includes(1), lease.flushes)
    return { modelRequests: adapter.requests.length, positiveRounds: positiveRounds(agent), checkpoint: lease.report() }
  })
}

async function resumeRevisionBinding(workDir: string): Promise<unknown> {
  return withHarness('resume-revision-binding', [textResponse('authorized after resume')], workDir, async ({ ctx, adapter, agent, lease }) => {
    const created = ctx.goals.create(agent, { objective: 'Resume must bind the current revision', maxGoalRounds: 8 })
    // Stop the first drive before it can reserve a round, then exercise the
    // two explicit native revision edges.
    ctx.goals.disarm(agent)
    const paused = ctx.goals.pause(agent, { id: created.id, revision: created.revision })
    expect('pause advances the native revision', paused.revision === created.revision + 1, paused)
    const resumed = ctx.goals.resume(agent, { id: paused.id, revision: paused.revision })
    expect('resume advances the native revision again', resumed.revision === paused.revision + 1, resumed)
    expect('resume arms the native goal', resumed.phase === 'active' && resumed.activation === 'armed', resumed)
    // The synthetic authorization still names the pre-pause revision.
    lease.authorize(created, { maxRounds: 1 })
    const staleBlocked = await waitForGoal(ctx, agent, (current) => current?.phase === 'blocked', 'stale revision block')
    expect('stale authorization is refused at pre-step', (lease.report() as any).rejections
      .some((rejection: HostRejection) => rejection.code === 'GOAL_STALE_REVISION'))
    expect('stale authorization dispatches no model request', adapter.requests.length === 0)
    expect('stale authorization leaves native roundsStarted at zero', staleBlocked.roundsStarted === 0)
    expect('official block advances exactly one revision past the resumed goal', staleBlocked.revision === resumed.revision + 1, staleBlocked)
    // A new explicit authorization at the current revision is required.
    const resumedAgain = ctx.goals.resume(agent, { id: staleBlocked.id, revision: staleBlocked.revision })
    lease.authorize(resumedAgain, { maxRounds: 1 })
    const final = await waitForGoal(ctx, agent, (current) => current?.phase === 'blocked', 're-authorized resume block')
    expect('re-authorized resume dispatches exactly one model request', adapter.requests.length === 1, adapter.requests.length)
    expect('re-authorized resume advances native roundsStarted to 1', final.roundsStarted === 1, final.roundsStarted)
    expect('re-authorized resume admits exactly one positive user/message', JSON.stringify(positiveRounds(agent)) === '[1]', positiveRounds(agent))
    return { modelRequests: adapter.requests.length, positiveRounds: positiveRounds(agent), checkpoint: lease.report() }
  })
}

async function unknownUsageBlocksAutomaticContinuation(workDir: string): Promise<unknown> {
  return withHarness('unknown-usage', [textResponse('round one'), textResponse('must not run')], workDir, async ({ ctx, adapter, agent, lease }) => {
    const goal = ctx.goals.create(agent, { objective: 'Unknown usage must fail closed', maxGoalRounds: 8 })
    lease.authorize(goal, { maxRounds: 2 })
    const blocked = await waitForGoal(ctx, agent, (current) => current?.phase === 'blocked', 'unknown usage block')
    expect('unknown usage allows the explicitly authorized first round', adapter.requests.length === 1, adapter.requests.length)
    expect('unknown usage refuses the automatic second round', (lease.report() as any).rejections
      .some((rejection: HostRejection) => rejection.round === 2 && rejection.code === 'GOAL_USAGE_UNVERIFIED'))
    expect('unknown usage is never reported as a settled cost', (lease.report() as any).usageStatus === 'unknown'
      && (lease.report() as any).settledCostMicros === null)
    expect('unknown usage leaves native roundsStarted at one', blocked.roundsStarted === 1, blocked.roundsStarted)
    expect('unknown usage admits no duplicate user/message', JSON.stringify(positiveRounds(agent)) === '[1]', positiveRounds(agent))
    return { modelRequests: adapter.requests.length, positiveRounds: positiveRounds(agent), checkpoint: lease.report() }
  })
}

async function noReplayAfterDriverRebuild(workDir: string): Promise<unknown> {
  return withHarness('driver-rebuild', [textResponse('round one'), textResponse('explicitly resumed round')], workDir, async ({ ctx, driver, adapter, agent, lease, removeFence }) => {
    const goal = ctx.goals.create(agent, { objective: 'Rebuild must not replay a round', maxGoalRounds: 8 })
    lease.authorize(goal, { maxRounds: 1 })
    const firstBlocked = await waitForGoal(ctx, agent, (current) => current?.phase === 'blocked', 'first round block')
    expect('first authorization runs exactly one round', adapter.requests.length === 1, adapter.requests.length)
    // Unload the official driver: it disarms and drops process-local state.
    await driver.dispose()
    expect('driver unload disarms the native goal', ctx.goals.get(agent)?.activation === 'disarmed')
    // Rebuild the driver over the live Agent and emit the session-start edge
    // a host restart would publish.
    // The old fence would be outside the rebuilt driver's middleware chain.
    // Re-register only after the new driver is mounted and disarmed.
    removeFence()
    const rebuilt = await ctx.plugin(goalRoundDriver)
    ctx.on('agent/pre-step', lease.preStep)
    await agentEvents(ctx, agent).serial('agent/created', { source: 'resume' })
    await new Promise((resolveTurn) => setImmediate(resolveTurn))
    expect('rebuild without explicit authorization dispatches no model request', adapter.requests.length === 1, adapter.requests.length)
    expect('rebuild leaves the native goal disarmed', ctx.goals.get(agent)?.activation === 'disarmed')
    // Only an explicit human re-authorization plus resume may continue.
    const resumed = ctx.goals.resume(agent, { id: firstBlocked.id, revision: firstBlocked.revision })
    lease.authorize(resumed, { maxRounds: 1 })
    const secondBlocked = await waitForGoal(ctx, agent, (current) => current?.phase === 'blocked', 'rebuilt resume block')
    expect('explicit re-authorization runs exactly one more round', adapter.requests.length === 2, adapter.requests.length)
    expect('explicit re-authorization advances native roundsStarted to 2', secondBlocked.roundsStarted === 2, secondBlocked.roundsStarted)
    expect('rebuilt driver admits no duplicate user/message', JSON.stringify(positiveRounds(agent)) === '[1,2]', positiveRounds(agent))
    expect('rebuilt driver refuses the next round through the downstream fence', secondBlocked.blockedReason?.code === 'prompt-rejected'
      && (lease.report() as any).rejections.some((rejection: HostRejection) => rejection.round === 3 && rejection.code === 'GOAL_ROUND_LIMIT'))

    await rebuilt.dispose()
    return { modelRequests: adapter.requests.length, positiveRounds: positiveRounds(agent), checkpoint: lease.report() }
  })
}

const SCENARIOS = [
  { name: 'unauthorized-positive-round-is-refused', run: unauthorizedPositiveRound },
  { name: 'single-authorized-round-then-host-cap', run: singleAuthorizedRoundThenHostCap },
  { name: 'resume-revision-binding', run: resumeRevisionBinding },
  { name: 'unknown-usage-blocks-automatic-continuation', run: unknownUsageBlocksAutomaticContinuation },
  { name: 'no-replay-after-driver-rebuild', run: noReplayAfterDriverRebuild },
] as const

/** Static description of the reviewed source and resolver, for evidence logs. */
function requirements(source: string): unknown {
  return {
    schema: 'goal-native-driver-seam-requirements/v1',
    sourceCommit: PIN,
    source,
    node: process.execPath,
    tsxResolver: join(source, 'node_modules/tsx/dist/esm/index.mjs'),
    tsconfig: join(source, 'tsconfig.json'),
    modules: REQUIRED_MODULES,
    command: `TSX_TSCONFIG_PATH="${join(source, 'tsconfig.json')}" node --import "${join(source, 'node_modules/tsx/dist/esm/index.mjs')}" scripts/oss/harness_goal_native_driver_seam_probe.mts --source "${source}" --work-dir <data3-work-dir>`,
    temporaryPathPolicy: 'all probe files stay under --work-dir, which must be inside /media/data3',
  }
}

/** Write one JSON document and exit only after stdout has drained. */
function emit(report: unknown, code: number): void {
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`, () => { process.exit(code) })
}

async function main(): Promise<void> {
  let options: Options
  try {
    options = parseOptions(process.argv.slice(2))
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`, () => { process.exit(64) })
    return
  }
  if (options.printRequirements) {
    emit(requirements(options.source), 0)
    return
  }
  try {
    if (!realpathSync(options.workDir).startsWith('/media/data3/')) throw new Error('Probe requires an existing data3 work directory')
    verifyPin(options.source)
    await loadOfficialModules(options.source)
  } catch (error) {
    emit({
      schema: SOURCE_COMMIT_SCHEMA,
      ok: false,
      stage: 'load',
      error: error instanceof Error ? error.message : String(error),
      requirements: requirements(options.source),
    }, 2)
    return
  }
  const scenarios: ScenarioResult[] = []
  let failures = 0
  for (const scenario of SCENARIOS) {
    try {
      const summary = await scenario.run(options.workDir) as Omit<ScenarioResult, 'name' | 'ok'>
      scenarios.push({ name: scenario.name, ok: true, ...summary })
    } catch (error) {
      failures += 1
      scenarios.push({
        name: scenario.name,
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      })
    }
  }
  emit({
    schema: SOURCE_COMMIT_SCHEMA,
    ok: failures === 0,
    sourceCommit: PIN,
    source: options.source,
    node: process.execPath,
    tsxResolver: join(options.source, 'node_modules/tsx/dist/esm/index.mjs'),
    scenarios,
    paidModelCalls: 0,
    network: 'none: in-process synthetic adapter only',
    usageStatus: 'unknown',
    settledCostMicros: null,
    notes: [
      'Official dsh-goal-round-driver, dsh-goal, and the official session projection/checkpoint service are the real pinned modules.',
      'The synthetic LLM adapter is in-process and never opens a socket or reads a credential.',
      'The synthetic host lease is a downstream agent/pre-step veto, not a second driver or scheduler.',
      'Unknown authoritative usage is never treated as a settled cost; automatic continuation fails closed.',
    ],
  }, failures === 0 ? 0 : 1)
}

main().catch((error: unknown) => {
  emit({
    schema: SOURCE_COMMIT_SCHEMA,
    ok: false,
    stage: 'unexpected',
    error: error instanceof Error ? error.message : String(error),
  }, 3)
})
