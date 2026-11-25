/** Goal lifecycle through actual UI and pinned official native state. */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_CORE_PATH);
const base=process.env.FEEDBACK_TEST_URL;
assert.equal(new URL(base).hostname,'127.0.0.1');
const root=base+'/project/kaiwu/',api=root+'api/v1';
const browser=await chromium.launch({executablePath:process.env.BROWSER_EXECUTABLE,headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
const checks=[];
async function json(response,status){assert.equal(response.status(),status,await response.text());return response.json();}
try{
  const context=await browser.newContext();
  await context.route('**/*',route=>new URL(route.request().url()).origin===base?route.continue():route.abort());
  await json(await context.request.post(api+'/auth/login',{data:{username:'reporter',password:'Correct-test-password-123'}}),200);
  const assistant=await json(await context.request.post(api+'/assistants',{data:{name:'Goal fixture',instructions:'Synthetic only',connections:[]}}),201);
  const conversation=await json(await context.request.post(api+'/conversations',{data:{assistant_id:assistant.id}}),201);
  const path=api+'/conversations/'+conversation.id;
  const sent=await json(await context.request.post(path+'/messages',{data:{client_request_id:'goal-browser-first',text:'Goal fixture initial turn'}}),202);
  const deadline=Date.now()+30000;
  let run;
  do{
    run=await json(await context.request.get(api+'/runs/'+sent.run_id),200);
    if(['succeeded','failed','cancelled','interrupted'].includes(run.status))break;
    await new Promise(r=>setTimeout(r,100));
  }while(Date.now()<deadline);
  assert.equal(run.status,'succeeded');
  const page=await context.newPage();
  const modelSends=[];
  page.on('request',r=>{if(r.method()==='POST'&&new URL(r.url()).pathname.endsWith('/messages'))modelSends.push(r.url());});
  page.on('dialog',dialog=>void dialog.accept());
  await page.goto(root+'?conversation='+conversation.id);
  const open=page.getByRole('button',{name:'持续目标',exact:true});
  await open.click({timeout:20000});
  const modal=page.getByRole('dialog',{name:'持续目标（状态试点）'});
  await modal.getByText('尚未创建目标',{exact:true}).waitFor({timeout:30000});
  await modal.getByLabel('目标与完成条件').fill('Synthetic completion criterion');
  await modal.getByLabel('最多目标轮次').fill('4');
  async function action(label){
    const [r]=await Promise.all([page.waitForResponse(r=>r.request().method()==='POST'&&r.url()===path+'/goal'),modal.getByRole('button',{name:label,exact:true}).click()]);
    return json(r,200);
  }
  const created=await action('创建目标');
  assert.equal(created.goal.activation,'disarmed');
  assert.equal(created.continuation_driver,false);
  await modal.getByText('目标已记录',{exact:true}).waitFor();
  checks.push('actual Goal state created, never arms automatic execution');
  await modal.getByRole('button',{name:'关闭',exact:true}).click();
  await page.reload();await open.click({timeout:20000});
  await modal.getByText('目标已记录',{exact:true}).waitFor({timeout:30000});
  assert.equal(await modal.getByLabel('目标与完成条件').inputValue(),'Synthetic completion criterion');
  checks.push('state persisted and reloaded from official session');
  const paused=await action('暂停');
  assert.equal(paused.goal.phase,'paused');
  await modal.getByText('已暂停',{exact:true}).waitFor();
  const resumed=await action('恢复状态');
  assert.equal(resumed.goal.activation,'disarmed');
  assert.equal(resumed.goal.phase,'active');
  await modal.getByText('目标已记录',{exact:true}).waitFor();
  checks.push('pause and explicit resume work while continuation remains disarmed');
  const completed=await action('标记完成');
  assert.equal(completed.goal.phase,'complete');
  await modal.getByText('已标记完成',{exact:true}).waitFor();
  const cleared=await action('清除目标');
  assert.equal(cleared.goal,null);
  await modal.getByText('尚未创建目标',{exact:true}).waitFor();
  checks.push('complete and clear preserve user-controlled state');
  assert.equal(modelSends.length,0);
  checks.push('goal management sends no model message');
  console.log(JSON.stringify({passed:checks.length,checks}));
}finally{await browser.close();}
