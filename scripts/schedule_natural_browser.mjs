/** Browser verifies actual model-tool approval, not a mocked approval card. */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_CORE_PATH);
const base=process.env.FEEDBACK_TEST_URL;
assert.equal(new URL(base).hostname,'127.0.0.1');
const root=base+'/project/kaiwu/',api=root+'api/v1';
const op=process.env.SCHEDULE_OPERATION,decision=process.env.SCHEDULE_DECISION;
assert(['create','edit'].includes(op)&&['allow','deny'].includes(decision));
const browser=await chromium.launch({executablePath:process.env.BROWSER_EXECUTABLE,headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
async function json(response,status){assert.equal(response.status(),status,await response.text());return response.json();}
const checks=[];
try{
  const context=await browser.newContext();
  await context.route('**/*',route=>new URL(route.request().url()).origin===base?route.continue():route.abort());
  await json(await context.request.post(api+'/auth/login',{data:{username:'reporter',password:'Correct-test-password-123'}}),200);
  const a=await json(await context.request.post(api+'/assistants',{data:{name:'Schedule browser fixture',instructions:'Synthetic only',connections:[]}}),201);
  const c=await json(await context.request.post(api+'/conversations',{data:{assistant_id:a.id}}),201);
  const route=api+'/conversations/'+c.id+'/schedules';
  let original;
  if(op==='edit'){
    const created=await json(await context.request.post(route,{data:{text:'original natural edit fixture',recurrence:'daily',scheduled_at:new Date(Date.now()+86400000).toISOString()}}),201);
    original=(await json(await context.request.get(route),200)).find(row=>row.id===created.id);
    assert(original&&original.text==='original natural edit fixture');
  }
  const page=await context.newPage();
  await page.goto(root+'?conversation='+encodeURIComponent(c.id));
  await page.getByRole('textbox',{name:'输入消息'}).fill(op==='create'?'请从明天开始每天用合成数据生成日报，共两天':'请修改当前会话定时日报的指令');
  const [submitted]=await Promise.all([page.waitForResponse(r=>r.request().method()==='POST'&&r.url()===api+'/conversations/'+c.id+'/messages'),page.getByRole('button',{name:'发送消息',exact:true}).click()]);
  const run=(await json(submitted,202)).run_id;
  const label=op==='create'?'创建定时智能体任务':'确认修改定时计划';
  const card=page.locator('.approval-card').filter({has:page.getByRole('heading',{name:label,exact:true})});
  await card.waitFor({timeout:60000});
  const preview=await card.locator('pre').textContent();
  assert(preview.includes(op==='create'?'只使用合成数据生成日报':'只使用合成数据生成更新后的日报'));
  if(op==='edit')assert(preview.includes('original natural edit fixture'));
  const before=await json(await context.request.get(route),200);
  assert.equal(before.length,op==='create'?0:1);
  if(original)assert.equal(before[0].text,original.text);
  checks.push('real user message, model tool preview, no premature write');
  await page.reload();
  await card.waitFor({timeout:25000});
  const [confirmed]=await Promise.all([page.waitForResponse(r=>r.request().method()==='POST'&&/\/approvals\/[^/]+\/decision$/.test(new URL(r.url()).pathname)),card.getByRole('button',{name:decision==='allow'?'允许执行':'拒绝',exact:true}).click()]);
  await json(confirmed,200);
  const deadline=Date.now()+45000;
  let result;
  do{
    result=await json(await context.request.get(api+'/runs/'+run),200);
    if(['succeeded','failed','cancelled','interrupted'].includes(result.status))break;
    await new Promise(resolve=>setTimeout(resolve,200));
  }while(Date.now()<deadline);
  assert.equal(result.status,'succeeded',JSON.stringify(result));
  const after=await json(await context.request.get(route),200);
  assert.equal(after.length,1);
  assert.equal(after[0].text,op==='create'?'只使用合成数据生成日报':decision==='allow'?'只使用合成数据生成更新后的日报':original.text);
  if(original){assert.equal(after[0].id,original.id);assert.equal(after[0].scheduled_at,original.scheduled_at);}
  checks.push('approval survives browser reload; explicit decision governs persisted plan');
  console.log(JSON.stringify({operation:op,decision,passed:checks.length,checks}));
}finally{await browser.close();}
