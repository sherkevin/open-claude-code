/** Test actual built workbench and isolated FastAPI ticket store; no customer/model data. */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PLAYWRIGHT_CORE_PATH);
const base = process.env.FEEDBACK_TEST_URL;
assert(new URL(base).hostname === '127.0.0.1');
const root = base + '/project/kaiwu/';
const api = root+'api/v1';
const password = 'Correct-test-password-123';
const browser = await chromium.launch({executablePath:process.env.BROWSER_EXECUTABLE,headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
const checks=[];
async function login(context,username){
  const result=await context.request.post(api+'/auth/login',{data:{username,password}});
  assert.equal(result.status(),200,await result.text());
}
async function getJson(response,status){
  assert.equal(response.status(),status,await response.text());
  return response.json();
}
try {
  const reporter=await browser.newContext();
  const admin=await browser.newContext();
  await login(reporter,'reporter');
  await login(admin,'admin');
  const a=await getJson(await reporter.request.post(api+'/assistants',{data:{name:'Feedback fixture',instructions:'Synthetic only',connections:[]}}),201);
  const cid=(await getJson(await reporter.request.post(api+'/conversations',{data:{assistant_id:a.id}}),201)).id;
  const page=await reporter.newPage();
  const messages=[];
  page.on('request',r=>{if(r.method()==='POST'&&/\/messages$/.test(new URL(r.url()).pathname))messages.push(r.url());});
  await page.goto(root+'?conversation='+encodeURIComponent(cid));
  const feedback=page.getByRole('button',{name:'问题反馈',exact:true});
  await feedback.click({timeout:20000});
  const modal=page.getByRole('dialog',{name:'问题反馈'});
  await modal.getByLabel('问题描述').fill('Synthetic display issue, browser test');
  const [submitted]=await Promise.all([
    page.waitForResponse(r=>r.request().method()==='POST'&&r.url()===api+'/conversations/'+cid+'/feedback'),
    modal.getByRole('button',{name:'提交反馈'}).click()
  ]);
  const fid=(await getJson(submitted,201)).id;
  await modal.getByText('反馈已提交',{exact:true}).waitFor();
  await modal.getByText('反馈编号：'+fid,{exact:true}).waitFor();
  checks.push('actual browser submission and visible persisted receipt');
  const adminPage=await admin.newPage();
  await adminPage.goto(root);
  await adminPage.locator('details.nav-administration > summary').click({timeout:20000});
  const nav=adminPage.getByRole('button',{name:'问题反馈',exact:true});
  await nav.click({timeout:20000});
  await adminPage.getByRole('button',{name:'查看详情',exact:true}).click();
  const detail=adminPage.getByRole('dialog',{name:'反馈详情'});
  await detail.getByLabel('处理状态').selectOption('resolved');
  await detail.getByLabel('处理说明（提交者可见）').fill('Synthetic fix verified');
  adminPage.once('dialog',dialog=>dialog.accept());
  const [published]=await Promise.all([adminPage.waitForResponse(r=>r.request().method()==='PATCH'&&r.url()===api+'/admin/feedback/'+fid),detail.getByRole('button',{name:'发布处理结果'}).click()]);
  const status=await getJson(published,200);
  assert.equal(status.status,'resolved');
  assert.equal(status.resolution_note,'Synthetic fix verified');
  checks.push('admin publishes explicit resolution in real browser');
  await modal.getByRole('button',{name:'关闭',exact:true}).click();
  await page.reload();
  await feedback.click({timeout:20000});
  await modal.getByText('我的反馈记录',{exact:true}).click();
  const history=modal.getByRole('list',{name:'我的反馈记录'});
  await history.getByText('已解决',{exact:true}).waitFor({timeout:15000});
  await history.getByText(/Synthetic display issue, browser test/).first().click();
  await history.getByText(fid,{exact:true}).waitFor();
  await history.getByText('Synthetic fix verified',{exact:true}).waitFor();
  checks.push('admin resolution visible after browser reload and reopening dialog');
  const denied=await admin.request.get(api+'/conversations/'+cid+'/feedback');
  assert.equal(denied.status(),404);
  checks.push('another user including admin cannot read reporter history route');
  await modal.getByRole('button',{name:'取消',exact:true}).click();
  // Seed one synthetic answer through the existing test-only FakeRuntime.
  const created=await getJson(await reporter.request.post(api+'/conversations/'+cid+'/messages',{
    data:{client_request_id:'feedback-message-fixture',text:'Synthetic answer seed',file_ids:[]}
  }),202);
  const deadline=Date.now()+15000;
  let run;
  do{
    run=await getJson(await reporter.request.get(api+'/runs/'+created.run_id),200);
    if(['succeeded','failed','cancelled','interrupted'].includes(run.status))break;
    await new Promise(resolve=>setTimeout(resolve,150));
  }while(Date.now()<deadline);
  assert.equal(run.status,'succeeded');
  assert(run.assistant_message_id);
  await page.reload();
  const answer=page.locator('[data-message-id="'+run.assistant_message_id+'"]');
  const shortcut=answer.getByRole('button',{name:'反馈此条回答',exact:true});
  const countBefore=(await getJson(await reporter.request.get(api+'/conversations/'+cid+'/feedback'),200)).length;
  await shortcut.click({timeout:20000});
  assert.equal(await modal.getByLabel('问题发生位置').inputValue(),'message:'+run.assistant_message_id);
  assert.equal((await getJson(await reporter.request.get(api+'/conversations/'+cid+'/feedback'),200)).length,countBefore);
  await modal.getByLabel('问题描述').fill('Synthetic answer-specific issue');
  const [messageSubmitted]=await Promise.all([
    page.waitForResponse(r=>r.request().method()==='POST'&&r.url()===api+'/conversations/'+cid+'/feedback'),
    modal.getByRole('button',{name:'提交反馈'}).click()
  ]);
  const messageFid=(await getJson(messageSubmitted,201)).id;
  await modal.getByText('反馈编号：'+messageFid,{exact:true}).waitFor();
  const messageRows=await getJson(await reporter.request.get(api+'/conversations/'+cid+'/feedback'),200);
  const linked=messageRows.find(row=>row.id===messageFid);
  assert(linked&&linked.message_id===run.assistant_message_id&&linked.description==='Synthetic answer-specific issue');
  checks.push('answer shortcut targets the exact persisted message, no write before submission');
  await modal.getByRole('button',{name:'关闭',exact:true}).click();
  await feedback.click();
  assert.equal(await modal.getByLabel('问题发生位置').inputValue(),'current');
  await modal.getByRole('button',{name:'取消',exact:true}).click();
  checks.push('shared dialog resets to session header context after message feedback');
  assert.equal(messages.length,0);
  checks.push('no model message sent');
  console.log(JSON.stringify({passed:checks.length,checks}));
} finally {
  await browser.close();
}
