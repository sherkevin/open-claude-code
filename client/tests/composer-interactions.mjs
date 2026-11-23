/** Real Chromium + actual ChatPage, controlled HTTP only; no model/provider calls. */
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';

const puppeteerPath = process.env.PUPPETEER_MODULE || 'puppeteer-core';
const browserExecutable = process.env.BROWSER_EXECUTABLE;
if (!browserExecutable) throw new Error('Set BROWSER_EXECUTABLE to an installed Chromium or Chrome executable. No browser is downloaded by this test.');
const { default: puppeteer } = await import(puppeteerPath);
const clientRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const output = process.env.COMPOSER_QA_OUTPUT || path.join(clientRoot, '../.runtime/composer-qa');
await mkdir(output, { recursive: true });
const at = '2026-11-15T10:00:00Z';
const models = [{id:'dano-v2.1',label:'Dano-v2.1',available:true,reasoning_levels:[{id:'light',label:'轻量'},{id:'high',label:'深入'},{id:'max',label:'最高'}],default_reasoning_level:'high'}];
const config = {brand_name:'开物',upload_max_bytes:20971520,auth_types:['none'],model_label:'Dano-v2.1',deployment_mode:'normal'};
const streamedConfig = {...config, chat_attachment_upload_max_bytes:50*1024**2, chat_attachment_limits:{max_files:10,max_file_bytes:50*1024**2,max_image_bytes:50*1024**2,max_total_bytes:200*1024**2,max_image_total_bytes:200*1024**2}};
const assistant = {id:'assistant',name:'工业业务助手',description:'查阅业务资料与业务数据。',instructions:'',enabled:true,access:{all_members:true,user_ids:[]},connections:[],knowledge_base_ids:[],revision:1};
const conversation = id => ({id,assistant_id:assistant.id,title:'订单与质量分析',archived:false,model_id:'dano-v2.1',reasoning_level:'high',auto_model_routing:false,has_native_session:true,runtime_type:'harness',active_run_id:null,created_at:at,updated_at:at});
const run = (id,status) => ({id:`run-${id}`,conversation_id:id,status,kind:'chat',model_id:'dano-v2.1',reasoning_level:'high',assistant_message_id:`answer-${id}`,error:null,usage:null,approvals:[],created_at:at,completed_at:status==='succeeded'?at:null});
const userMessage = (id,text='查询本周订单') => ({id:`user-${id}`,role:'user',text,run_id:`run-${id}`,file_ids:[],created_at:at});
const answer = id => ({id:`answer-${id}`,role:'assistant',text:'已按客户汇总本周订单。共有 8 个客户，结果可在来源中核对。',run_id:`run-${id}`,file_ids:[],citations:[],created_at:at});
const states = new Map([
  ['history',{status:'succeeded',messages:[userMessage('history'),answer('history')]}],
  ...['recent','draft-race','failure','trial','trial-hot','empty-response','network-error'].map(id=>[id,{status:'running',messages:[userMessage(id)]}]),
  ['other',{status:null,messages:[]}],
  ['forked',{status:null,messages:[userMessage('history'),answer('history')].map(message => ({...message,run_id:null}))}],
  ['streamed',{status:null,messages:[]}],
  ['legacy',{status:null,messages:[]}],
  ['receipt-lost',{status:null,messages:[]}],
  ['upload-order',{status:null,messages:[]}],
]);
const completions = [], followups = [], sends = [], failures = [], passed = [];
const uploads = [];
const clipboardPng = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNQ6vgPAAJ5AaqZk5OyAAAAAElFTkSuQmCC', 'base64');
const clipboardNote = Buffer.from('Clipboard document');
let browser, server, page;
const harness = `<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"/><style>html,body,#root{margin:0;height:100%;width:100%}.qa-shell{height:100%;display:flex;flex-direction:column}.qa-shell>.chat-page{flex:1;min-height:0;width:100%}</style></head><body><div id="root"></div><script type="module">
import React from '/node_modules/.vite/deps/react.js';
import ReactDOM from '/node_modules/.vite/deps/react-dom_client.js';
import ChatPage from '/src/ChatPage.tsx';
import '/src/styles.css';import '/src/workbench-theme.css';import '/src/enterprise-theme.css';import '/src/ai-detail.css';
const assistant=${JSON.stringify(assistant)}, models=${JSON.stringify(models)}, config=${JSON.stringify(config)}, streamedConfig=${JSON.stringify(streamedConfig)};
function Harness(){const[id,setId]=React.useState('history');const[caps,setCaps]=React.useState(['messages.send','models.complete','runs.read']);window.switchComposerConversation=setId;window.setComposerCapabilities=setCaps;return React.createElement('main',{className:'zhixing-theme qa-shell'},React.createElement(ChatPage,{key:id,draftOwnerId:'composer-test-user',conversation:{...${JSON.stringify(conversation('history'))},id,runtime_type:id==='legacy'?'claude':'harness'},assistant,models,config:id.startsWith('trial')?{...config,deployment_mode:'trial',capabilities:caps}:['streamed','legacy'].includes(id)?streamedConfig:config,modelsLoading:false,onChanged:()=>{},onArchived:()=>{},onOpenConversation:child=>setId(child.id)}));}
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(React.StrictMode,null,React.createElement(Harness)));
</script></body></html>`;

try {
  server = await createServer({root:clientRoot,configFile:path.join(clientRoot,'vite.config.ts'),logLevel:'error',server:{host:'127.0.0.1',port:4191,strictPort:true},plugins:[{name:'composer-test-entry',configureServer(vite){
    vite.middlewares.use('/composer-qa.html',async(req,res,next)=>{if(req.method!=='GET')return next();res.setHeader('Content-Type','text/html');res.end(await vite.transformIndexHtml('/composer-qa.html',harness));});
    vite.middlewares.use('/api/v1/files', async (req,res,next) => {
      if (req.method !== 'POST') return next();
      const parts = [];
      for await (const piece of req) parts.push(piece);
      const body = Buffer.concat(parts);
      const binary = new URL(req.url, 'http://localhost').pathname === '/binary';
      const name = binary ? decodeURIComponent(req.headers['x-upload-filename'] || '') : /name="file"; filename="([^"\r\n]+)"/.exec(body.toString('latin1'))?.[1];
      const content = name?.endsWith('.txt') ? clipboardNote : clipboardPng;
      if (!name || (binary ? req.headers['content-type'] !== 'application/octet-stream' || !body.equals(content) : !body.includes(content))) {
        failures.push(`Attachment missing or invalid in ${binary ? 'binary' : 'multipart'} upload`);
      }
      const file = {id:`clipboard-${uploads.length + 1}`,name,size:content.length,mime_type:content===clipboardNote?'text/plain':'image/png',created_at:at};
      uploads.push(file);
      if (name === 'slow-first.txt') await new Promise(resolve => setTimeout(resolve, 350));
      res.writeHead(201, {'Content-Type':'application/json'}).end(JSON.stringify(file));
    });
  }}]});
  await server.listen();
  browser = await puppeteer.launch({headless:true,executablePath:browserExecutable});
  page = await browser.newPage();
  await page.setViewport({width:1440,height:1000,deviceScaleFactor:1});
  await page.emulateMediaFeatures([{name:'prefers-reduced-motion',value:'reduce'}]);
  page.on('pageerror',error=>failures.push(error.message));
  await page.evaluateOnNewDocument(()=>{
    window.composerStreams=[];
    window.EventSource=class extends EventTarget{constructor(url){super();this.url=url;this.closed=false;window.composerStreams.push(this);queueMicrotask(()=>this.onopen?.());}close(){this.closed=true;}};
  });
  await page.setRequestInterception(true);
  const reply = (request,body,status=200) => request.respond({status,contentType:'application/json',body:JSON.stringify(body)}).catch(error=>{if(!/Invalid InterceptionId|Target closed|Request is already handled/i.test(error.message))throw error;});
  page.on('request',request=>{
    const pathname=new URL(request.url()).pathname;
    if(!pathname.startsWith('/api/v1/'))return void request.continue();
    const endpoint=pathname.slice('/api/v1'.length), method=request.method();
    if (['/files','/files/binary'].includes(endpoint) && method==='POST') return void request.continue();
    const storedFile = endpoint.match(/^\/files\/(clipboard-\d+)(\/content)?$/);
    if (storedFile) {
      if (storedFile[2]) return void request.respond({status:200,contentType:'image/png',body:clipboardPng});
      return void reply(request,uploads.find(file=>file.id===storedFile[1]));
    }
    if (endpoint === '/conversations/history/fork' && method === 'POST') {
      const body = JSON.parse(request.postData());
      if (body.message_id !== 'answer-history' || !body.client_request_id) return void reply(request, {}, 422);
      return void reply(request, conversation('forked'), 201);
    }
    if(endpoint.endsWith('/skills') || endpoint.endsWith('/interactions'))return void reply(request,[]);
    if(endpoint.endsWith('/trace'))return void reply(request,{events:[]});
    if (/^\/conversations\/[^/]+\/queue$/.test(endpoint) && method==='GET') {
      return void reply(request, {items:[],paused:false,archived:false,active_run_id:null,latest_run:null});
    }
    if(endpoint==='/models/complete'){completions.push({request,body:JSON.parse(request.postData())});return;}
    if(endpoint==='/models/followups'){followups.push({request,body:JSON.parse(request.postData())});return;}
    const match=endpoint.match(/^\/conversations\/([^/]+)(\/messages)?$/);
    if(match){const id=match[1],state=states.get(id);if(!state)return void reply(request,{},404);
      if(match[2]&&method==='POST'){sends.push({request,id,body:JSON.parse(request.postData())});return;}
      if(match[2])return void reply(request,state.messages);
      return void reply(request,{...conversation(id),runtime_type:id==='legacy'?'claude':'harness',active_run_id:state.status==='running'||state.status==='queued'?`run-${id}`:null});
    }
    const r=endpoint.match(/^\/runs\/run-(.+)$/);if(r&&states.has(r[1]))return void reply(request,run(r[1],states.get(r[1]).status));
    failures.push(`Unexpected API ${method} ${endpoint}`);void reply(request,{error:{message:'Unexpected fixture request'}},404);
  });
  const wait = predicate => page.waitForFunction(predicate,{timeout:5000});
  const input = () => page.$('textarea[aria-label="输入消息"]');
  const fill = async text => {const node=await input();await node.focus();await node.evaluate((el,value)=>{const setter=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;setter.call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));},text);};
  const value = () => page.$eval('textarea[aria-label="输入消息"]',el=>el.value);
  const switchTo = async id => {await page.evaluate(value=>window.switchComposerConversation(value),id);await wait(()=>!document.querySelector('.loader'));await page.waitForSelector('textarea[aria-label="输入消息"]');await new Promise(resolve=>setTimeout(resolve,70));};
  const finish = async(id,status='succeeded')=>{states.set(id,{status,messages:[userMessage(id),...(status==='succeeded'?[answer(id)]:[])]});await page.evaluate(({id,status})=>{const stream=[...window.composerStreams].reverse().find(s=>s.url.includes(`/runs/run-${id}/events`)&&!s.closed);if(!stream)throw new Error('Missing live stream');stream.dispatchEvent(new MessageEvent('run.completed',{data:JSON.stringify({id:99,run_id:`run-${id}`,type:'run.completed',created_at:'2026-11-15T10:00:01Z',data:{status}})}));},{id,status});};
  const response = async(index,suffix)=>{
    const {request, body} = completions[index];
    const suggestion = `${body.text} ${suffix}`;
    await reply(request,{suggestion,router:{mode:'provider'}});
    return suggestion;
  };
  await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/composer-qa.html`,{waitUntil:'domcontentloaded'});
  await page.waitForSelector('textarea[aria-label="输入消息"]');
  await wait(()=>!document.body.textContent.includes('正在恢复会话'));
  assert.equal(followups.length,0,'Opening history must not request followups');passed.push('历史会话不自动请求');
  const modelBottom=await page.evaluate(()=>!!document.querySelector('.composer-actions .chat-model-settings'));
  assert(modelBottom,'Model pill must remain inside composer bottom toolbar');

  await fill('');await page.keyboard.press('Tab');assert.equal(await value(),'最近有哪些订单需要跟进？');assert.equal(completions.length,0);
  await (await input()).focus();await page.keyboard.down('Shift');await page.keyboard.press('Tab');await page.keyboard.up('Shift');assert.notEqual(await page.evaluate(()=>document.activeElement?.tagName),'TEXTAREA');assert.equal(completions.length,0);passed.push('空输入 Tab 采用本地建议，ShiftTab 保持焦点导航');
  await fill('正在输入');await page.evaluate(()=>{const node=document.querySelector('textarea');node.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));node.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,keyCode:229,bubbles:true,cancelable:true}));node.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',isComposing:true,keyCode:229,bubbles:true,cancelable:true}));node.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true}));});
  assert.equal(completions.length,0);assert.equal(sends.length,0);passed.push('中文组合输入不补全、不发送');

  await fill('查询订单');await page.keyboard.press('Tab');await wait(()=>document.body.textContent.includes('正在补全'));
  assert.equal(await value(),'查询订单');await fill('我已经改成查库存');await response(0,'过期的订单建议');await new Promise(resolve=>setTimeout(resolve,80));
  assert.equal(await value(),'我已经改成查库存');assert.equal(await page.$('.composer-completion.has-preview'),null);passed.push('继续输入丢弃旧补全');
  await page.keyboard.press('Tab');await wait(()=>document.body.textContent.includes('正在补全'));const acceptedSuggestion=await response(1,'按仓库查询当前可用库存，并列出数据来源。');
  await page.waitForSelector('.composer-completion.has-preview');assert.equal(await value(),'我已经改成查库存');await page.keyboard.press('Tab');assert.equal(await value(),acceptedSuggestion);passed.push('Tab 先预览再采用');

  await fill('查询质量');await page.keyboard.press('Tab');await wait(()=>document.body.textContent.includes('正在补全'));await page.keyboard.press('Tab');
  assert(await page.evaluate(()=>document.activeElement?.classList.contains('attach-button')),'Pending Tab must reach the stable attachment control');await page.evaluate(()=>{window.composerFocusedBeforeResponse=document.activeElement;});await response(2,'按产线统计检验数量与合格率。');await page.waitForSelector('.composer-completion.has-preview');
  assert(await page.evaluate(()=>document.activeElement===window.composerFocusedBeforeResponse),`Response must not steal focus: ${JSON.stringify(await page.evaluate(()=>({before:window.composerFocusedBeforeResponse?.outerHTML,after:document.activeElement?.outerHTML,connected:window.composerFocusedBeforeResponse?.isConnected})))}`);passed.push('补全等待期间 Tab 可离开且响应不抢焦点');
  await (await input()).focus();await page.keyboard.press('Escape');

  const messageRequest = id => page.waitForRequest(request => request.method() === 'POST' && new URL(request.url()).pathname === `/api/v1/conversations/${id}/messages`);
  const sendReady = () => wait(()=>document.querySelector('button[aria-label="发送消息"]')?.disabled === false);
  await fill('发送时不要丢草稿');await page.keyboard.press('Tab');await wait(()=>document.body.textContent.includes('正在补全'));
  await sendReady();
  const firstSend = messageRequest('history'); await page.keyboard.press('Enter'); await firstSend;
  await wait(()=>document.querySelector('button[aria-label="发送消息"]')?.disabled);assert.equal(sends.length,1);await fill('发送过程中写的下一条草稿');
  const sent=sends[0];const msg=userMessage(sent.id,sent.body.text);states.set(sent.id,{status:'queued',messages:[msg]});await reply(sent.request,{message:msg,run_id:`run-${sent.id}`});await response(3,'不得覆盖新草稿');await new Promise(resolve=>setTimeout(resolve,120));
  assert.equal(await value(),'发送过程中写的下一条草稿');assert.equal(await page.$('.composer-completion.has-preview'),null);passed.push('发送与延迟补全不覆盖后续草稿');

  await switchTo('other');await fill('旧会话请求');await page.keyboard.press('Tab');await wait(()=>document.body.textContent.includes('正在补全'));await switchTo('recent');await response(4,'不得落入新会话');await new Promise(resolve=>setTimeout(resolve,80));assert.equal(await value(),'');passed.push('换会话丢弃旧响应');
  const openSuggestions = async expectedCalls => {
    await page.waitForSelector('.composer-followups summary');
    assert.equal(followups.length, expectedCalls, 'A collapsed suggestion list must cost zero requests');
    await page.click('.composer-followups summary');
    await wait(() => document.body.textContent.includes('正在整理后续问题'));
  };
  await finish('recent');await openSuggestions(0);assert.equal(followups.length,1);assert.deepEqual(followups[0].body,{conversation_id:'recent',run_id:'run-recent'});
  await reply(followups[0].request,{suggestions:['哪些客户的订单最多？','按区域比较订单金额。','查看本周发货明细。'],router:{mode:'provider'}});await page.waitForSelector('.composer-followups button');assert.equal(sends.length,1);
  await page.click('.composer-followups summary');
  await page.click('.composer-followups summary');
  await page.waitForSelector('.composer-followups button');
  assert.equal(followups.length, 1, 'Reopening the same answer reuses its suggestions');
  await page.$eval('.composer-followups button',node=>node.click());assert.equal(await value(),'哪些客户的订单最多？');assert.equal(sends.length,1);passed.push('新轮次建议只传 IDs，点击只预填');

  await switchTo('draft-race');await finish('draft-race');await openSuggestions(1);assert.equal(followups.length,2);
  await fill('我已有下一步');await reply(followups[1].request,{suggestions:['过期建议'],router:{mode:'provider'}});await new Promise(resolve=>setTimeout(resolve,80));assert.equal(await value(),'我已有下一步');
  await fill('');await new Promise(resolve=>setTimeout(resolve,100));assert.equal(await page.$('.composer-followups[open]'),null);assert.equal(followups.length,2);passed.push('建议 pending 时输入取消，清空后无悬挂 spinner 或重复调用');

  await switchTo('failure');await finish('failure','failed');await new Promise(resolve=>setTimeout(resolve,100));assert.equal(followups.length,2);passed.push('失败任务不请求建议');
  await switchTo('trial');await finish('trial');await new Promise(resolve=>setTimeout(resolve,100));assert.equal(followups.length,2);passed.push('未开放 capability 的 trial 不请求建议');
  await switchTo('empty-response');await finish('empty-response');await openSuggestions(2);assert.equal(followups.length,3);await reply(followups[2].request,{suggestions:[],router:{mode:'unavailable'}});await wait(()=>document.body.textContent.includes('暂无后续问题'));assert.equal(await value(),'');passed.push('空建议不造模板');

  await switchTo('network-error');await finish('network-error');await openSuggestions(3);assert.equal(followups.length,4);await reply(followups[3].request,{error:{message:'辅助请求暂不可用'}},503);await page.waitForSelector('.composer-followups-notice');assert.equal(await page.$('.composer-area .notice.error'),null);await fill('继续问我的业务问题');assert.equal(await page.$('.composer-followups'),null);assert.equal(await page.$eval('button[aria-label="发送消息"]',node=>node.disabled),false);passed.push('建议请求失败不阻塞主会话或发送');

  await fill('请按客户分析本周订单，指出需要关注的变化。');await page.keyboard.press('Tab');await wait(()=>document.body.textContent.includes('正在补全'));await response(5,'请按客户汇总本周订单数量与金额，列出与上周相比变化较大的客户，并注明统计范围和来源。');await page.waitForSelector('.composer-completion.has-preview');
  await page.evaluate(()=>document.fonts.ready);await page.screenshot({path:path.join(output,'输入区_桌面_交互夹具.png')});
  await page.setViewport({width:390,height:844,deviceScaleFactor:1});await page.waitForFunction(()=>{const node=document.querySelector('textarea');return node.scrollHeight<=node.clientHeight+2||node.clientHeight>=158;});await page.screenshot({path:path.join(output,'输入区_手机_交互夹具.png')});
  const dimensions=await page.evaluate(()=>({viewport:innerWidth,scroll:document.documentElement.scrollWidth,composer:document.querySelector('.fw-composer').getBoundingClientRect().toJSON(),reducedMotion:matchMedia('(prefers-reduced-motion:reduce)').matches,spinnerAnimation:getComputedStyle(document.querySelector('.composer-refined .spin')||document.querySelector('.fw-composer')).animationName,textarea:{height:document.querySelector('textarea').clientHeight,scroll:document.querySelector('textarea').scrollHeight},sendRadius:getComputedStyle(document.querySelector('.send-button')).borderRadius,focusLine:getComputedStyle(document.querySelector('.fw-composer'),'::before').backgroundImage}));
  assert.equal(dimensions.scroll,dimensions.viewport,'Mobile must not overflow horizontally');assert(dimensions.textarea.scroll<=dimensions.textarea.height+2,'Resizing must recompute short draft height');assert.equal(dimensions.sendRadius,'50%');passed.push('手机缩窄后输入高度重算、无横向溢出');

  await switchTo('trial-hot');await fill('动态关闭补全');await page.keyboard.press('Tab');await wait(()=>document.body.textContent.includes('正在补全'));assert.equal(completions.length,7);
  await page.evaluate(()=>window.setComposerCapabilities(['messages.send','runs.read']));await wait(()=>!document.querySelector('.composer-completion'));
  await page.evaluate(()=>window.setComposerCapabilities(['messages.send','models.complete','models.followups','runs.read']));await (await input()).focus();await page.keyboard.press('Tab');await wait(()=>document.body.textContent.includes('正在补全'));assert.equal(completions.length,8);
  const reenabledSuggestion=await response(7,'重新启用后可正常补全。');await page.waitForSelector('.composer-completion.has-preview');await response(6,'已关闭能力的过期响应');assert.equal(await page.$eval('.composer-completion p',el=>el.textContent),reenabledSuggestion);passed.push('补全能力关闭即取消，重新开启不残留 busy，旧响应丢弃');
  await fill('');await finish('trial-hot');await openSuggestions(4);assert.equal(followups.length,5);
  await page.evaluate(()=>window.setComposerCapabilities(['messages.send','models.complete','runs.read']));await wait(()=>!document.querySelector('.composer-followups'));
  await page.evaluate(()=>window.setComposerCapabilities(['messages.send','models.complete','models.followups','runs.read']));await reply(followups[4].request,{suggestions:['不应显示的旧建议'],router:{mode:'provider'}});await new Promise(resolve=>setTimeout(resolve,80));assert.equal(await page.$('.composer-followups[open]'),null);assert.equal(followups.length,5);passed.push('后续建议能力关闭清除等待，重开不显示旧状态或重复调用');

  const paste = (text, useImage) => page.evaluate(({text,useImage,bytes}) => {
    const data = new DataTransfer();
    if (text) data.setData('text/plain',text);
    if (useImage) data.items.add(new File([new Uint8Array(bytes)],'screenshot',{type:'image/png'}));
    return document.querySelector('textarea[aria-label="输入消息"]').dispatchEvent(new ClipboardEvent('paste', {clipboardData:data,bubbles:true,cancelable:true}));
  }, {text,useImage,bytes:[...clipboardPng]});
  assert.equal(await paste('',true),true,'Trial clipboard intake remains disabled');
  await switchTo('other');
  assert.equal(await paste('普通文本',false),true,'Text-only clipboard must keep browser paste behavior');
  assert.equal(uploads.length,0);
  assert.equal(await paste('',true),false,'Image-only clipboard is handled as an attachment');
  await page.waitForSelector('.composer-files .file-chip-main');
  assert.equal(uploads[0].name,'screenshot.png');
  await page.click('.composer-files .file-chip-main');
  await wait(()=>document.querySelector('img.image-preview')?.naturalWidth>0);
  await page.click('[aria-label="关闭窗口"]');
  await page.click('[aria-label="移除 screenshot.png"]');
  await wait(()=>!document.querySelector('.composer-files .file-chip'));
  assert.equal(await paste('带文字的图片',true),true,'Mixed clipboard keeps browser text paste behavior');
  await page.waitForSelector('.composer-files .file-chip-main');
  const sentAttachment = page.waitForRequest(request => request.method()==='POST' && new URL(request.url()).pathname==='/api/v1/conversations/other/messages');
  await page.click('button[aria-label="发送消息"]');
  await sentAttachment;
  assert.deepEqual(sends[1].body.file_ids,[uploads[1].id]);
  const attached = {...userMessage('other',sends[1].body.text),file_ids:[uploads[1].id]};
  states.set('other',{status:'running',messages:[attached]});
  await reply(sends[1].request,{message:attached,run_id:'run-other'});
  await page.waitForSelector('.message.user .file-chip-main');
  passed.push('粘贴图片沿用附件上传、预览、移除与发送，普通文本和试用限制保持原行为');

  await switchTo('streamed');
  const dragUpload = page.waitForRequest(request => request.method() === 'POST' && new URL(request.url()).pathname === '/api/v1/files/binary');
  assert.equal(await page.$eval('.fw-composer', (composer, bytes) => {
    const data = new DataTransfer();
    data.items.add(new File([new Uint8Array(bytes)], '拖放截图.png', {type:'image/png'}));
    return composer.dispatchEvent(new DragEvent('drop', {dataTransfer:data,bubbles:true,cancelable:true}));
  }, [...clipboardPng]), false, 'Dropping a file on the composer consumes browser navigation');
  await dragUpload;
  await page.waitForSelector('.composer-files .file-chip-main');
  assert.equal(uploads[2].name, '拖放截图.png');
  await page.evaluate(bytes => {
    const data = new DataTransfer();
    data.items.add(new File([new Uint8Array(bytes)], 'note.txt', {type:'text/plain'}));
    document.querySelector('textarea').dispatchEvent(new ClipboardEvent('paste', {clipboardData:data,bubbles:true,cancelable:true}));
  }, [...clipboardNote]);
  await wait(()=>document.querySelectorAll('.composer-files .file-chip-main').length===2);
  assert.equal(uploads[3].name, 'note.txt');
  passed.push('拖放图片和粘贴文本文件可作为附件，服务端开启时使用二进制上传并保留中文文件名');

  await page.$eval('.fw-composer', composer => {
    const data = new DataTransfer();
    data.items.add(new File([new Uint8Array(50*1024**2+1)], 'over-limit.pdf', {type:'application/pdf'}));
    composer.dispatchEvent(new DragEvent('drop', {dataTransfer:data,bubbles:true,cancelable:true}));
  });
  await wait(()=>document.querySelector('.composer-area .notice.error')?.textContent.includes('单个附件上限（50 MiB）'));
  assert.equal(uploads.length,4,'Preflight blocks an upload one byte above the advertised maximum');
  passed.push('超出服务端公布的单文件上限 1 字节时，上传前显示 50 MiB 提示');

  await switchTo('legacy');
  const legacyUpload = page.waitForRequest(request=>request.method() === 'POST' && new URL(request.url()).pathname === '/api/v1/files');
  await paste('',true);
  await legacyUpload;
  await page.waitForSelector('.composer-files .file-chip-main');
  assert.equal(uploads[4].name,'screenshot.png');
  passed.push('Claude 会话沿用原有附件上传接口');

  states.set('history',{status:'succeeded',messages:[userMessage('history'),answer('history')]});
  await switchTo('history');
  await page.waitForSelector('[data-message-id="answer-history"]', {timeout: 8000});
  await fill('');
  await page.keyboard.press('ArrowUp');
  assert.equal(await value(), userMessage('history').text);
  await page.keyboard.press('ArrowDown');
  assert.equal(await value(), '');
  passed.push('空白编辑器用方向键召回历史问题，可回到空白草稿');
  await fill('/reference');
  await page.keyboard.press('Enter');
  await page.waitForSelector('.composer-references');
  await page.$$eval('.composer-references section button', buttons => {
    const answer = buttons.find(button => button.textContent.includes('已按客户汇总'));
    if (!answer) throw new Error('Missing answer in the reference picker: ' + document.querySelector('.composer-references').textContent);
    answer.click();
  });
  await wait(() => document.querySelector('textarea')?.value.includes('> 已按客户汇总'));
  assert.equal(await page.$('.composer-references'), null);
  await page.evaluate(() => {
    const button = [...document.querySelectorAll('.message button')].find(node => node.textContent.includes('从这条消息创建分支'));
    button.click();
  });
  await wait(() => document.querySelector('textarea')?.value === '' && document.querySelector('[data-message-id="answer-history"]'));
  passed.push('聊天输入框用斜杠命令引用一条消息，并可从已完成回答打开分支');

  await switchTo('receipt-lost');
  await fill('后台已受理，页面未收到回执');
  await sendReady();
  const before = sends.length;
  const initialReceipt = messageRequest('receipt-lost');
  await page.click('button[aria-label="发送消息"]');
  await initialReceipt;
  await wait(() => document.querySelector('button[aria-label="发送消息"]')?.disabled);
  assert.equal(sends.length, before + 1);
  const first = sends.at(-1);
  await reply(first.request, { error: { message: '响应丢失测试' } }, 503);
  await wait(() => document.querySelector('.composer-area .notice.error')?.textContent.includes('响应丢失测试'));
  await page.reload();
  await wait(() => typeof window.switchComposerConversation === 'function');
  await switchTo('receipt-lost');
  assert.equal(await value(), first.body.text);
  await sendReady();
  const repeatedReceipt = messageRequest('receipt-lost');
  await page.click('button[aria-label="发送消息"]');
  await repeatedReceipt;
  await wait(() => document.querySelector('button[aria-label="发送消息"]')?.disabled);
  assert.equal(sends.length, before + 2);
  const second = sends.at(-1);
  assert.equal(first.body.client_request_id, second.body.client_request_id);
  const accepted = userMessage('receipt-lost', first.body.text);
  states.set('receipt-lost', { status:'running', messages:[accepted] });
  await reply(second.request, { message:accepted, run_id:'run-receipt-lost' });
  await wait(() => document.querySelector('textarea')?.value === '');
  passed.push('发送回执丢失后，重新加载仍保留原草稿和请求编号，确认返回后才清空');
  await switchTo('upload-order');
  await page.$eval('textarea[aria-label="输入消息"]', (target, bytes) => {
    const data = new DataTransfer();
    for (const name of ['slow-first.txt', 'fast-second.txt']) data.items.add(new File([new Uint8Array(bytes)], name, {type:'text/plain'}));
    target.dispatchEvent(new ClipboardEvent('paste', {clipboardData:data,bubbles:true,cancelable:true}));
  }, [...clipboardNote]);
  await wait(() => document.querySelectorAll('.composer-files .file-chip-main').length === 2);
  const names = await page.$$eval('.composer-files .file-chip-main', nodes => nodes.map(node => node.textContent));
  assert.match(names[0], /slow-first\.txt/);
  assert.match(names[1], /fast-second\.txt/);
  passed.push('第一件附件上传较慢时，界面仍按用户选择的顺序排列');

  assert.deepEqual(failures,[]);
  const receipt={status:'passed',scope:'Actual ChatPage in local Chromium, controlled HTTP, zero model or customer calls',passed,completion_requests:completions.length,followup_requests:followups.length,message_requests:sends.length,dimensions,errors:failures};
  await writeFile(path.join(output,'输入区交互边界回执.json'),JSON.stringify(receipt,null,2));console.log(JSON.stringify(receipt,null,2));
} catch (error) {
  console.error('Composer checks completed before the failure:', passed);
  console.error('Captured message requests:', sends.map(item => item.id));
  if (page) console.error('Composer state:', await page.evaluate(() => ({
    active: document.activeElement?.getAttribute('aria-label'),
    text: document.querySelector('textarea')?.value,
    status: document.querySelector('.run-status-pill')?.textContent,
    messages: document.querySelector('.composer-area')?.textContent?.slice(0, 1200),
    send: document.querySelector('.send-button')?.outerHTML,
  })).catch(() => null));
  throw error;
} finally { await browser?.close(); await server?.close(); }
