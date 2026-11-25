/** Isolated, real pinned Team service/JSONL projection and host read seam.
 * No external model, native model tools, production profile or customer data.
 */
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { resolve, join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { mkdtempSync, realpathSync } from 'node:fs'
import { teamStateRequest } from '../../server/harness/team_state.ts'

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
const {bindSubagentActivation} = await load('packages/subagent/subagent/tests/manager-internals.ts')
const {default:AgentLoop} = await load('packages/core/agent-loop/src/index.ts')
const {SessionId} = await load('packages/core/session/src/index.ts')
const persistence = await load('packages/session/session-persistence-jsonl/src/index.ts')
const {default:SubagentService} = await load('packages/subagent/subagent/src/index.ts')
const spawn = await load('packages/subagent/subagent-spawn-in-process/src/index.ts')
const {default:TeamService} = await load('packages/team/agent-team/src/index.ts')
const {MockAdapter} = await load('packages/core/agent-loop/tests/mock-adapter.ts')
const root = mkdtempSync(join(tmp,'team-state-'))
const contexts: any[] = []
const passed: string[] = []
const check = (name:string) => passed.push(name)
const config = (id:string) => ({sessionId:id,sourceCommit:pin})
const read = (ctx:any,id:string) => teamStateRequest(ctx,config(id),{action:'get',sessionId:id},true) as any
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
  const lead = await ctx.agentLoop.create(SessionId('host-team-a'),{provider:'mock',model:'mock'})
  const other = await ctx.agentLoop.create(SessionId('host-team-b'),{provider:'mock',model:'mock'})
  assert.deepEqual(read(ctx,lead.id).board,{enabled:false,tasks:[]})
  check('new root defaults off')
  for (const input of [
    {action:'get',sessionId:other.id},
    {action:'enable',sessionId:lead.id},
    {action:'get',sessionId:lead.id,extra:true},
  ]) assert.throws(()=>teamStateRequest(ctx,config(lead.id),input,true),/Unauthorized/)
  assert.throws(()=>teamStateRequest(ctx,config(lead.id),{action:'get',sessionId:lead.id},false),/Unauthorized/)
  assert.throws(()=>teamStateRequest(ctx,{...config(lead.id),sourceCommit:'wrong'},{action:'get',sessionId:lead.id},true),/Unauthorized/)
  check('uninitialized, mismatched root, mutation and invalid source rejected')
  await ctx.agentTeams.setEnabled(lead,true)
  const a = await ctx.agentTeams.createTask(lead,{subject:'review',description:'synthetic',writeScopes:['server/a']})
  const b = await ctx.agentTeams.createTask(lead,{subject:'implement',description:'synthetic',blockedBy:[a.id]})
  assert.equal(read(ctx,lead.id).board.tasks[1].ready,false)
  const claimed = await ctx.agentTeams.updateTask(lead,{taskId:a.id,expectedRevision:a.revision,action:'claim'})
  await assert.rejects(ctx.agentTeams.updateTask(lead,{taskId:a.id,expectedRevision:a.revision,action:'complete'}),{code:'TEAM_TASK_STALE_REVISION'})
  await ctx.agentTeams.updateTask(lead,{taskId:a.id,expectedRevision:claimed.revision,action:'complete'})
  assert.equal(read(ctx,lead.id).board.tasks[1].ready,true)
  check('official board CAS and dependency readiness retained in host view')
  assert.deepEqual(read(ctx,other.id).board,{enabled:false,tasks:[]})
  check('distinct root boards isolated')
  // Upstream's idle resident fixture exercises real rootOf without a model turn.
  const child = await ctx.agents.create({
    sessionId:SessionId('host-team-child'), agentOptions:{provider:'mock',model:'mock'},
    meta:{origin:'subagent',parentSession:lead.id},
    setup:(_ctx:any,agent:any)=>bindSubagentActivation(ctx,agent,lead),
  })
  assert.equal(ctx.subagents.rootOf(child.agent),lead)
  assert.throws(()=>read(ctx,child.agent.id),/Team root unavailable/)
  await child.dispose()
  check('resident descendant cannot masquerade as a root')
  const otherBefore = other.session.seq
  other.session.append('team/task',{version:999,teamId:other.id,task:{}})
  assert.throws(()=>read(ctx,other.id),/Team projection unavailable/)
  assert.equal(other.session.seq,otherBefore+1)
  check('invalid persisted board fails closed')

  const before = lead.session.seq
  const detached = read(ctx,lead.id)
  detached.board.tasks[0].subject='tampered'
  assert.equal(read(ctx,lead.id).board.tasks[0].subject,'review')
  assert.equal(lead.session.seq,before)
  assert.equal(adapter.requests.length,0)
  check('detached read does not append or call model')
  await ctx.agentTeams.setEnabled(lead,false)
  const saved = read(ctx,lead.id).board
  assert.equal(saved.enabled,false)
  assert.equal(saved.tasks.length,2)
  await ctx.sessions.flush(lead.session)
  await ctx.fiber.dispose()
  contexts.pop()
  const second = await stack()
  const resumed = await second.ctx.agents.resume({resumeSessionId:SessionId('host-team-a'),agentOptions:{provider:'mock',model:'mock'}})
  assert.deepEqual(read(second.ctx,resumed.agent.id).board,saved)
  assert.equal(second.adapter.requests.length,0)
  check('disabled board recovered from actual JSONL after context restart, no execution')
  await resumed.dispose()
  console.log(JSON.stringify({upstream:pin,passed:passed.length,checks:passed,storage:root}))
} finally {
  for (const ctx of contexts.reverse()) await ctx.fiber.dispose()
}
