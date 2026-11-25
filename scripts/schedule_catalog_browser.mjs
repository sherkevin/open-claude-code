/** Real browser + isolated FastAPI/SQLite, no model or customer access. */
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
async function login(context,username){await json(await context.request.post(api+'/auth/login',{data:{username,password:'Correct-test-password-123'}}),200);}
async function fixture(context,name,task,recurrence){
  const a=await json(await context.request.post(api+'/assistants',{data:{name,instructions:'Synthetic only',connections:[]}}),201);
  const c=await json(await context.request.post(api+'/conversations',{data:{assistant_id:a.id,title:name}}),201);
  const s=await json(await context.request.post(api+'/conversations/'+c.id+'/schedules',{data:{text:task,recurrence,scheduled_at:new Date(Date.now()+3600000).toISOString()}}),201);
  return {c,s};
}
try{
  const member=await browser.newContext(),admin=await browser.newContext();
  await login(member,'reporter');await login(admin,'admin');
  const first=await fixture(member,'Catalog A','Fixture plan alpha','daily');
  const second=await fixture(member,'Catalog B','Fixture plan beta','once');
  await fixture(admin,'Other account','Private fixture gamma','once');
  const page=await member.newPage();
  const sends=[];
  page.on('request',r=>{if(r.method()==='POST'&&/\/messages$/.test(new URL(r.url()).pathname))sends.push(r.url());});
  page.on('dialog',d=>void d.accept());
  await page.goto(root);
  await page.getByRole('button',{name:'定时任务',exact:true}).click({timeout:20000});
  await page.getByRole('heading',{name:'定时任务中心'}).waitFor();
  const row=page.locator('.approval-card').filter({hasText:'Fixture plan alpha'});
  await row.waitFor();
  await page.locator('.task-sidebar-schedule-text').filter({hasText:/^Fixture plan beta$/}).waitFor();
  assert.equal(await page.getByText('Private fixture gamma',{exact:true}).count(),0);
  checks.push('real global catalog lists two owner sessions, excludes other account');
  await Promise.all([page.waitForResponse(r=>r.url().endsWith('/'+first.s.id+'/pause')&&r.status()===200),row.getByRole('button',{name:'暂停',exact:true}).click()]);
  await row.getByRole('button',{name:'恢复',exact:true}).waitFor();
  await Promise.all([page.waitForResponse(r=>r.url().endsWith('/'+first.s.id+'/resume')&&r.status()===200),row.getByRole('button',{name:'恢复',exact:true}).click()]);
  await row.getByRole('button',{name:'暂停',exact:true}).waitFor();
  checks.push('pause and resume reuse scoped API, state refreshes');
  const row2=page.locator('.approval-card').filter({hasText:'Fixture plan beta'});
  await Promise.all([page.waitForResponse(r=>r.request().method()==='DELETE'&&r.url().endsWith('/'+second.s.id)&&r.status()===204),row2.getByRole('button',{name:/取消定时任务/}).click()]);
  await row2.waitFor({state:'detached'});
  await page.getByRole('button',{name:'执行历史',exact:true}).click();
  await row2.getByText(/已取消/).waitFor();
  checks.push('cancelled task moves to history');
  await page.getByRole('button',{name:'活动计划与任务',exact:true}).click();
  await row.getByRole('button',{name:'打开关联会话'}).click();
  await page.waitForURL(url=>url.searchParams.get('conversation')===first.c.id);
  await page.getByRole('button',{name:'问题反馈',exact:true}).waitFor();
  checks.push('open exact owning conversation');
  assert.equal(sends.length,0);
  checks.push('no model messages emitted by management');
  console.log(JSON.stringify({passed:checks.length,checks}));
}finally{await browser.close();}
