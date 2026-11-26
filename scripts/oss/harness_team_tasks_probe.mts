/** Isolated real pinned Team task lifecycle seam over official JSONL projection.
 *
 * Loads the pinned upstream Team service and the same fixture stack used by
 * harness_team_state_probe.mts. It drives only the host seam; no model turn,
 * subagent, shell, business system, production profile or customer data.
 *
 * Usage:
 *   node --import <source>/node_modules/tsx/dist/esm/index.mjs \
 *     scripts/oss/harness_team_tasks_probe.mts <pinned-source> <data3-tmp>
 */
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { resolve, join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { mkdtempSync, realpathSync } from 'node:fs'
import { teamStateRequest } from '../../server/harness/team_state.ts'
import { teamTaskRequest } from '../../server/harness/team_tasks.ts'

const pin = 'fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159'
const source = resolve(process.argv[2] || '.')
const tmp = realpathSync(process.argv[3] || '.')
assert(tmp.startsWith('/media/data3/'), 'test storage must be on data3')
assert.equal(execFileSync('git',['-C',source,'rev-parse','HEAD'],{encoding:'utf8'}).trim(),pin)
execFileSync('git',['-C',source,'diff','--quiet','HEAD','--'])
const load = (path: string) => import(pathToFileURL(resolve(source,path)).href)
const {Context} = await load('vendor/cordis/src/index.ts')
const {mountAgentLoopTestDependencies} = await load('packages/test-support/agent-loop-testkit/src/index.ts')
const {mountWorkingDirectoryFixture} = await load('packages/subagent/subagent/tests/working-directory-fixture.ts')
const {TestSessionQuery} = await load('packages/team/agent-team/tests/test-session-query.ts')
const {default:AgentLoop} = await load('packages/core/agent-loop/src/index.ts')
const {SessionId} = await load('packages/core/session/src/index.ts')
const persistence = await load('packages/session/session-persistence-jsonl/src/index.ts')
const {default:SubagentService} = await load('packages/subagent/subagent/src/index.ts')
const spawn = await load('packages/subagent/subagent-spawn-in-process/src/index.ts')
const {default:TeamService} = await load('packages/team/agent-team/src/index.ts')
const {MockAdapter} = await load('packages/core/agent-loop/tests/mock-adapter.ts')
const root = mkdtempSync(join(tmp,'team-tasks-'))
const contexts: any[] = []
const passed: string[] = []
const check = (name:string) => passed.push(name)
const config = (id:string) => ({sessionId:id,sourceCommit:pin,teamTasks:true})
const call = (ctx:any,id:string,params:any) => teamTaskRequest(ctx,config(id),params,true)
const read = (ctx:any,id:string) => teamStateRequest(ctx,{sessionId:id,sourceCommit:pin},
  {action:'get',sessionId:id},true) as any
async function stack() {
  const ctx = new Context()
  contexts.push(ctx)
  await mountAgentLoopTestDependencies(ctx)
  await mountWorkingDirectoryFixture(ctx)
  await ctx.plugin(persistence,{root,compression:'none'})
  await ctx.plugin(TestSessionQuery)
  await ctx.plugin(AgentLoop,{agents:[]})
  await ctx.plugin(SubagentService)
  await ctx.plugin(spawn,{providerName:'spawn'})
  await ctx.plugin(TeamService,{defaultEnabled:false,maxTasks:32})
  const adapter = new MockAdapter([])
  ctx.llm.registerAdapter(['mock'],adapter)
  return {ctx,adapter}
}
try {
  const {ctx,adapter} = await stack()
  const lead = await ctx.agentLoop.create(SessionId('host-tasks-a'),{provider:'mock',model:'mock'})
  const other = await ctx.agentLoop.create(SessionId('host-tasks-b'),{provider:'mock',model:'mock'})
  await assert.rejects(call(ctx,lead.id,{action:'create',sessionId:lead.id,subject:'too early',
    description:'synthetic',blockedBy:[],writeScopes:[]}),/TEAM_TASK_REJECTED:TEAM_BOARD_DISABLED/)
  check('create rejected before board enable, without a model turn')
  for (const params of [
    {action:'enable',sessionId:other.id},
    {action:'enable',sessionId:lead.id,extra:true},
    {action:'enable',sessionId:lead.id,confirm:true},
    {action:'delete',sessionId:lead.id},
  ]) await assert.rejects(call(ctx,lead.id,params),/TEAM_REQUEST_INVALID/)
  await assert.rejects(teamTaskRequest(ctx,{...config(lead.id),teamTasks:false},
    {action:'enable',sessionId:lead.id},true),/TEAM_REQUEST_INVALID/)
  await assert.rejects(teamTaskRequest(ctx,{...config(lead.id),sourceCommit:'wrong'},
    {action:'enable',sessionId:lead.id},true),/TEAM_REQUEST_INVALID/)
  await assert.rejects(teamTaskRequest(ctx,config(lead.id),{action:'enable',sessionId:lead.id},false),
    /TEAM_REQUEST_INVALID/)
  check('switch, source, initialization, session and exact-key guards fail closed')

  const enabled = await call(ctx,lead.id,{action:'enable',sessionId:lead.id}) as any
  assert.equal(enabled.boardEnabled,true)
  assert.deepEqual(enabled.board,{enabled:true,tasks:[]})
  assert.equal((await call(ctx,lead.id,{action:'enable',sessionId:lead.id}) as any).boardEnabled,true)
  await assert.rejects(call(ctx,lead.id,{action:'claim',sessionId:lead.id,
    taskId:'task-missing',expectedRevision:1,ownerId:'forged'}),/TEAM_REQUEST_INVALID/)
  await assert.rejects(call(ctx,lead.id,{action:'claim',sessionId:lead.id,
    taskId:'task-missing',expectedRevision:1}),/TEAM_TASK_REJECTED:TEAM_TASK_NOT_FOUND/)
  check('official board enable is idempotent and unknown task fields cannot cross')

  const a = (await call(ctx,lead.id,{action:'create',sessionId:lead.id,subject:'review',
    description:'synthetic metadata',blockedBy:[],writeScopes:['server/a']}) as any).task
  const b = (await call(ctx,lead.id,{action:'create',sessionId:lead.id,subject:'implement',
    description:'synthetic metadata',blockedBy:[a.id],writeScopes:[]}) as any).task
  assert.equal(read(ctx,lead.id).board.tasks.find((task:any)=>task.id===b.id).ready,false)
  const claimed = (await call(ctx,lead.id,{action:'claim',sessionId:lead.id,
    taskId:a.id,expectedRevision:a.revision}) as any).task
  assert.equal(claimed.status,'in_progress')
  await assert.rejects(call(ctx,lead.id,{action:'complete',sessionId:lead.id,
    taskId:a.id,expectedRevision:a.revision}),/TEAM_TASK_REJECTED:TEAM_TASK_STALE_REVISION/)
  const completed = (await call(ctx,lead.id,{action:'complete',sessionId:lead.id,
    taskId:a.id,expectedRevision:claimed.revision}) as any).task
  assert.equal(completed.status,'completed')
  assert.equal(read(ctx,lead.id).board.tasks.find((task:any)=>task.id===b.id).ready,true)
  check('create/dependency/claim/complete and stale CAS retained by the official service')

  assert.deepEqual(read(ctx,other.id).board,{enabled:false,tasks:[]})
  assert.equal(adapter.requests.length,0)
  check('distinct root boards isolated and zero model requests')

  const saved = read(ctx,lead.id).board
  await ctx.sessions.flush(lead.session)
  await ctx.fiber.dispose()
  contexts.pop()
  const second = await stack()
  const resumed = await second.ctx.agents.resume({
    resumeSessionId:SessionId('host-tasks-a'),agentOptions:{provider:'mock',model:'mock'}})
  assert.deepEqual(read(second.ctx,resumed.agent.id).board,saved)
  assert.equal(second.adapter.requests.length,0)
  check('enabled board and task history recovered from actual JSONL across context restart')

  const resumedId = resumed.agent.id
  await call(second.ctx,resumedId,{action:'enable',sessionId:resumedId})
  const currentB = read(second.ctx,resumedId).board.tasks.find((task:any)=>task.id===b.id)
  const claimedB = (await call(second.ctx,resumedId,{action:'claim',sessionId:resumedId,
    taskId:currentB.id,expectedRevision:currentB.revision}) as any).task
  const completedB = (await call(second.ctx,resumedId,{action:'complete',sessionId:resumedId,
    taskId:currentB.id,expectedRevision:claimedB.revision}) as any).task
  assert.equal(completedB.status,'completed')
  assert.equal(second.adapter.requests.length,0)
  check('further board mutation after restart still starts no model turn')
  await resumed.dispose()
  console.log(JSON.stringify({upstream:pin,passed:passed.length,checks:passed,storage:root}))
} finally {
  for (const ctx of contexts.reverse()) await ctx.fiber.dispose()
}
