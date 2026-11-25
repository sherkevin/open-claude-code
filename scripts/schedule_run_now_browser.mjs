/** Real browser + isolated FastAPI/SQLite/FIFO, with synthetic runtime. */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH);
const base=process.env.FEEDBACK_TEST_URL;
assert.equal(new URL(base).hostname,'127.0.0.1');
const root=base+'/project/kaiwu/',api=root+'api/v1';
const browser=await chromium.launch({executablePath:process.env.BROWSER_EXECUTABLE,headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
const checks=[];
async function json(response,status){assert.equal(response.status(),status,await response.text());return response.json();}
async function finished(context,oid){
  for(let n=0;n<80;n++){
    const page=await json(await context.request.get(api+'/schedules?scope=history'),200);
    const row=page.items.find(row=>row.id===oid&&row.status==='succeeded');
    if(row)return row;
    await new Promise(resolve=>setTimeout(resolve,125));
  }
  throw new Error('Manual occurrence did not finish');
}
try{
  const context=await browser.newContext();
  await json(await context.request.post(api+'/auth/login',{data:{username:'reporter',password:'Correct-test-password-123'}}),200);
  const a=await json(await context.request.post(api+'/assistants',{data:{name:'Run now fixture',instructions:'Synthetic only',connections:[]}}),201);
  const c=await json(await context.request.post(api+'/conversations',{data:{assistant_id:a.id}}),201);
  const route=api+'/conversations/'+c.id+'/schedules';
  const first=await json(await context.request.post(route,{data:{text:'Once fixture',recurrence:'once',scheduled_at:new Date(Date.now()+3600000).toISOString()}}),201);
  const second=await json(await context.request.post(route,{data:{text:'Series fixture',recurrence:'daily',scheduled_at:new Date(Date.now()+7200000).toISOString()}}),201);
  const before=(await json(await context.request.get(route),200)).find(x=>x.id===second.id);
  const page=await context.newPage();
  const sends=[];
  page.on('request',r=>{if(r.method()==='POST'&&/\/messages$/.test(new URL(r.url()).pathname))sends.push(r.url());});
  await page.goto(root);
  await page.getByRole('button',{name:'定时任务',exact:true}).click({timeout:20000});
  const row=page.locator('.approval-card').filter({hasText:'Once fixture'});
  await row.getByRole('button',{name:'立即执行',exact:true}).click();
  const modal=page.getByRole('dialog',{name:'立即执行定时任务'});
  await modal.getByText(/原时间不会再次触发/).waitFor();
  const [res]=await Promise.all([page.waitForResponse(r=>r.request().method()==='POST'&&r.url()===route+'/'+first.id+'/run-now'),modal.getByRole('button',{name:'确认立即执行'}).click()]);
  const receipt=await json(res,202);
  assert.equal(receipt.occurrence_id,first.id);
  await modal.getByText('执行实例编号：'+first.id,{exact:true}).waitFor();
  await modal.getByRole('button',{name:'关闭',exact:true}).click();
  const completed=await finished(context,first.id);
  assert(completed.run_id&&completed.manual_at);
  checks.push('global immediate run receipt and same one-shot identity');
  await page.getByRole('button',{name:'执行历史',exact:true}).click();
  const history=page.locator('.approval-card').filter({hasText:'Once fixture'});
  await history.getByText(/已完成/).waitFor();
  await history.locator('summary').click();
  await history.getByText(/手动立即执行/).waitFor();
  checks.push('actual completed status and manual trigger visible in history');
  await page.getByRole('button',{name:'活动计划与任务',exact:true}).click();
  const series=page.locator('.approval-card').filter({hasText:'Series fixture'});
  await series.getByRole('button',{name:'打开关联会话'}).click();
  await page.waitForURL(u=>u.searchParams.get('conversation')===c.id);
  const sidebar=page.getByRole('button',{name:'会话侧栏',exact:true});
  if(await sidebar.getAttribute('aria-expanded')!=='true')await sidebar.click();
  const button=page.locator('.task-sidebar-schedule-row').filter({hasText:'Series fixture'}).getByRole('button',{name:'立即执行',exact:true});
  await button.click();
  await modal.getByText(/额外执行一次/).waitFor();
  const [seriesRes]=await Promise.all([page.waitForResponse(r=>r.request().method()==='POST'&&r.url()===route+'/'+second.id+'/run-now'),modal.getByRole('button',{name:'确认立即执行'}).click()]);
  const extra=await json(seriesRes,202);
  assert.notEqual(extra.occurrence_id,second.id);
  await modal.getByText('执行实例编号：'+extra.occurrence_id,{exact:true}).waitFor();
  await finished(context,extra.occurrence_id);
  const after=(await json(await context.request.get(route),200)).find(x=>x.id===second.id);
  assert.equal(after.scheduled_at,before.scheduled_at);
  assert.equal(sends.length,0);
  checks.push('conversation uses same confirmation, extra occurrence preserves normal cycle and uses FIFO');
  console.log(JSON.stringify({passed:checks.length,checks}));
}finally{await browser.close();}
