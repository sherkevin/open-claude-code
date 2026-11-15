/** Browser regression for failed retry submissions. Local HTTP fixtures only;
 * no model, external MCP, or production account is used.
 * Run: node client/tests/chat-retry.mjs (PLAYWRIGHT_MODULE_DIR may be supplied).
 */
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const output = path.resolve(process.env.CHAT_RETRY_REPORT_DIR || path.join(ROOT, '.runtime', 'client-chat-retry'));
await mkdir(output, { recursive: true });
const requireBrowser = createRequire(path.join(process.env.PLAYWRIGHT_MODULE_DIR || path.join(ROOT, '.local/browser'), 'package.json'));
const { chromium } = requireBrowser('playwright');
const executablePath = process.env.BROWSER_EXECUTABLE || [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
].find(candidate => existsSync(candidate));
const stamp = '2026-11-15T00:00:00Z';
const conversation = { id: 'retry-conversation', assistant_id: 'retry-assistant', title: '重试回包故障验证', archived: false, active_run_id: null, created_at: stamp, updated_at: stamp, model_id: 'fixture-model', model_label: '隔离模型夹具', reasoning_level: 'high', runtime_type: 'harness' };
const assistant = { id: conversation.assistant_id, name: '隔离验证助手', description: '客户端故障注入', connections: [], enabled: true };
const config = { brand_name: '开物', upload_max_bytes: 1024, auth_types: ['none'], model_label: '隔离模型夹具', deployment_mode: 'trial', capabilities: ['messages.send', 'runs.read'] };
const models = [{ id: 'fixture-model', label: '隔离模型夹具', available: true, reasoning_levels: [{ id: 'high', label: 'high' }], default_reasoning_level: 'high' }];
const makeMessages = id => [
  { id: `user-${id}`, conversation_id: conversation.id, run_id: id, role: 'user', text: '按客户汇总发货数量', file_ids: [], citations: [], created_at: stamp },
  { id: `answer-${id}`, conversation_id: conversation.id, run_id: id, role: 'assistant', text: '业务数据已返回，回答未完成。', file_ids: [], citations: [], created_at: stamp },
];
const makeRun = id => ({ id, conversation_id: conversation.id, status: 'failed', assistant_message_id: `answer-${id}`, error: '隔离夹具：模型整理失败', usage: null, approvals: [], created_at: stamp, completed_at: stamp });
const makeEvents = id => [
  { id: 1, run_id: id, type: 'tool.completed', created_at: stamp, data: { success: true, source_ids: [], business_result: { schema_version: 'business-result/v1', title: '已取得的发货结果', columns: [{ key: 'customer', label: '客户' }, { key: 'quantity', label: '数量', unit: '件' }], rows: [{ customer: '夹具客户', quantity: 42 }], total_rows: 1, complete: true, synthetic: true, truncated: false, warnings: [] } } },
  { id: 2, run_id: id, type: 'run.completed', created_at: stamp, data: { status: 'failed', error: '隔离夹具：模型整理失败' } },
];
const checks = [], pageErrors = [], requests = [], accepted = new Map();
const fixtureModule = 'virtual:chat-retry-fixture';
const fixtureSource = `import React from 'react';import {createRoot} from 'react-dom/client';import ChatPage from '/src/ChatPage.tsx';import '/src/styles.css';
  createRoot(document.getElementById('root')).render(React.createElement(ChatPage,{conversation:${JSON.stringify(conversation)},assistant:${JSON.stringify(assistant)},config:${JSON.stringify(config)},models:${JSON.stringify(models)},modelsLoading:false,onChanged:()=>{window.__chatChanges=(window.__chatChanges||0)+1;},onArchived:()=>{}}));`;
let messages = makeMessages('original'), mode = 'reject', server, browser;
const heldReplies = [];
let nextReadFault = null;
const readFaults = new Map(), heldRunReads = new Map();
let nextDelayedCancel = false;
const cancelRuns = new Map(), heldStreams = new Map(), heldCancelReads = new Map();
const waitForFixture = async predicate => {
  const deadline = Date.now() + 5000;
  while (!predicate()) { assert(Date.now() < deadline, 'local fixture did not reach the expected boundary'); await new Promise(resolve => setTimeout(resolve, 10)); }
};
const record = (name, assertion) => { try { assertion(); checks.push({ name, passed: true }); } catch (error) { checks.push({ name, passed: false, error: error.message }); } };
try {
  server = await createServer({ root: path.join(ROOT, 'client'), configFile: path.join(ROOT, 'client/vite.config.ts'), logLevel: 'error', plugins: [{ name: 'chat-retry-fixture', resolveId(id) { if (id === fixtureModule) return `\0${fixtureModule}`; }, load(id) { if (id === `\0${fixtureModule}`) return fixtureSource; } }], server: { host: '127.0.0.1', port: 18126, strictPort: true } });
  await server.listen();
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai' });
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.route('**/__retry_fixture__', route => route.fulfill({ contentType: 'text/html', body: `<!doctype html><html><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module">
    import RefreshRuntime from '/@react-refresh';RefreshRuntime.injectIntoGlobalHook(window);window.$RefreshReg$=()=>{};window.$RefreshSig$=()=>type=>type;window.__vite_plugin_react_preamble_installed__=true;
    </script><script type="module" src="/@id/${fixtureModule}"></script></body></html>` }));
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), endpoint = new URL(request.url()).pathname.replace('/api/v1', '');
    const reply = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (endpoint === `/conversations/${conversation.id}`) return reply(conversation);
    if (endpoint === `/conversations/${conversation.id}/messages`) {
      if (request.method() === 'GET') return reply(messages);
      const body = request.postDataJSON(); requests.push(body);
      if (mode === 'hold') await new Promise(resolve => { heldReplies.push(resolve); });
      if (mode === 'reject' || mode === 'hold') return reply({ error: { code: 'FIXTURE_REJECTED', message: '隔离夹具：请求未被受理' } }, 503);
      if (!accepted.has(body.client_request_id)) accepted.set(body.client_request_id, `accepted-${accepted.size + 1}`);
      const id = accepted.get(body.client_request_id);
      if (mode === 'lost') return route.abort('failed');
      if (nextReadFault) { readFaults.set(id, { kind: nextReadFault, consumed: false }); nextReadFault = null; }
      if (nextDelayedCancel) { cancelRuns.set(id, { status: 'running', holdNextRead: false }); nextDelayedCancel = false; }
      messages = [...messages, ...makeMessages(id)];
      return reply({ message: messages.at(-2), run_id: id }, 201);
    }
    const cancelled = /^\/runs\/([^/]+)\/cancel$/.exec(endpoint);
    if (cancelled && cancelRuns.has(cancelled[1])) {
      Object.assign(cancelRuns.get(cancelled[1]), { status: 'cancelled', holdNextRead: true });
      return reply({});
    }
    const match = /^\/runs\/([^/]+)(\/events)?$/.exec(endpoint);
    if (match) {
      const id = match[1], fault = readFaults.get(id), cancellation = cancelRuns.get(id);
      if (match[2]) {
        if (cancellation?.status === 'running') { await new Promise(resolve => { heldStreams.set(id, resolve); }); heldStreams.delete(id); }
        const events = makeEvents(id).map(event => cancellation && event.type === 'run.completed' ? { ...event, data: { status: cancellation.status } } : event);
        return route.fulfill({ contentType: 'text/event-stream', body: events.map(event => `id: ${event.id}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join('') });
      }
      if (cancellation?.holdNextRead) {
        cancellation.holdNextRead = false;
        await new Promise(resolve => { heldCancelReads.set(id, resolve); }); heldCancelReads.delete(id);
        return reply({ ...makeRun(id), status: 'cancelling', completed_at: null, error: null });
      }
      if (fault && !fault.consumed) {
        fault.consumed = true;
        await new Promise(resolve => { heldRunReads.set(id, resolve); });
        heldRunReads.delete(id);
        if (fault.kind === 'error') return reply({ error: { code: 'FIXTURE_READ_FAILED', message: '隔离夹具：任务状态暂未读回' } }, 503);
        return reply({ ...makeRun(id), status: 'running', completed_at: null, error: null });
      }
      return reply(cancellation ? { ...makeRun(id), status: cancellation.status, error: null } : makeRun(id));
    }
    return reply({ error: { code: 'UNEXPECTED_FIXTURE_REQUEST', message: endpoint } }, 500);
  });
  await page.goto(`http://127.0.0.1:18126/__retry_fixture__`);
  const retry = page.getByRole('button', { name: '重试本次任务', exact: true });
  const results = page.locator('.business-result');
  await results.waitFor(); await retry.waitFor();
  await retry.click(); await page.getByText('隔离夹具：请求未被受理', { exact: true }).waitFor();
  const retained = await results.count();
  record('failed_retry_keeps_successful_business_data', () => assert.equal(retained, 1));
  const exportButton = page.getByRole('button', { name: '导出当前结果 CSV', exact: true });
  const exportCount = await exportButton.count();
  record('failed_retry_keeps_csv_entry', () => assert.equal(exportCount, 1));
  if (exportCount) {
    const downloaded = page.waitForEvent('download'); await exportButton.click();
    const download = await downloaded, csv = await readFile(await download.path(), 'utf8');
    record('failed_retry_csv_keeps_returned_rows_and_incomplete_status', () => {
      assert(csv.includes('"夹具客户","42"'));
      assert(csv.includes('"回答状态","业务数据已返回，回答未完成"'));
    });
  }
  mode = 'lost'; const start = requests.length;
  await retry.click(); await page.getByText('暂时无法连接服务，请检查网络后重试。', { exact: true }).waitFor();
  await retry.click(); await page.getByText('暂时无法连接服务，请检查网络后重试。', { exact: true }).waitFor();
  record('uncertain_retry_reuses_request_id', () => assert.equal(requests[start].client_request_id, requests[start + 1].client_request_id));
  record('uncertain_retry_creates_one_task', () => assert.equal(accepted.size, 1));
  mode = 'reply'; await retry.click(); await page.waitForFunction(() => document.querySelector('.business-result .br-footer')?.textContent.includes('结果编号'));
  await page.waitForFunction(() => !document.querySelector('.run-outcome button')?.disabled);
  const acknowledgedId = requests.at(-1).client_request_id;
  mode = 'hold'; const beforeDoubleClick = requests.length;
  await retry.evaluate(button => { button.click(); button.click(); });
  await page.waitForFunction(() => document.querySelector('.run-outcome button')?.disabled);
  // Allow all requests from the same turn to reach the local route handler.
  await page.waitForTimeout(100);
  record('pending_retry_has_one_submission', () => assert.equal(requests.length - beforeDoubleClick, 1));
  record('new_retry_after_acknowledgement_gets_new_id', () => assert.notEqual(requests.at(-1).client_request_id, acknowledgedId));
  heldReplies.splice(0).forEach(resolve => resolve());
  await page.getByText('隔离夹具：请求未被受理', { exact: true }).waitFor();
  // A POST's first read can arrive after SSE has already reached its terminal
  // state and the finalizer has closed the stream. It must not revive the run.
  for (const action of ['retry', 'send']) {
    mode = 'reply'; nextReadFault = 'stale';
    const beforeChanges = await page.evaluate(() => window.__chatChanges || 0);
    if (action === 'retry') await retry.click();
    else {
      await page.getByRole('textbox', { name: '消息' }).fill('读取下一次发货汇总');
      await page.getByRole('button', { name: '发送消息', exact: true }).click();
    }
    await page.waitForFunction(before => (window.__chatChanges || 0) > before, beforeChanges);
    assert.equal(heldRunReads.size, 1, 'the first run read must still be pending after SSE finalization');
    for (const resolve of heldRunReads.values()) resolve();
    await page.waitForFunction(before => (window.__chatChanges || 0) > before + 1, beforeChanges);
    const status = await page.locator('.run-status-pill').getAttribute('data-state');
    record(`${action}_stale_initial_read_cannot_revive_terminal_run`, () => assert.equal(status, 'failed'));
    await page.reload();
    await results.waitFor();
    await page.waitForFunction(() => { const button = document.querySelector('.run-outcome button'); return button && !button.disabled; });
  }
  mode = 'reply'; nextReadFault = 'error';
  const beforeReadError = await page.evaluate(() => window.__chatChanges || 0);
  await retry.click();
  await page.waitForFunction(before => (window.__chatChanges || 0) > before, beforeReadError);
  assert.equal(heldRunReads.size, 1);
  for (const resolve of heldRunReads.values()) resolve();
  await page.getByText('隔离夹具：任务状态暂未读回', { exact: true }).waitFor();
  const statusAfterReadError = await page.locator('.run-status-pill').getAttribute('data-state');
  record('failed_initial_read_keeps_sse_terminal_status', () => assert.equal(statusAfterReadError, 'failed'));
  const retainedAfterReadError = await results.count();
  record('failed_initial_read_keeps_received_result', () => assert.equal(retainedAfterReadError, 1));
  // A cancellation read is not tied to the event subscription. Hold its old
  // snapshot until the user has already started and finished a newer task.
  nextDelayedCancel = true;
  const beforeCancellation = await page.evaluate(() => window.__chatChanges || 0);
  await page.getByRole('textbox', { name: '输入消息', exact: true }).fill('隔离验证：停止旧任务');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await page.waitForFunction(before => (window.__chatChanges || 0) > before, beforeCancellation);
  await waitForFixture(() => heldStreams.size === 1);
  const oldRunId = [...heldStreams.keys()][0];
  await page.getByRole('button', { name: '停止', exact: true }).click();
  await waitForFixture(() => heldCancelReads.has(oldRunId));
  heldStreams.get(oldRunId)();
  await page.waitForFunction(before => (window.__chatChanges || 0) > before + 1, beforeCancellation);
  await page.waitForFunction(() => document.querySelector('.run-status-pill')?.getAttribute('data-state') === 'cancelled');
  await page.getByRole('textbox', { name: '输入消息', exact: true }).fill('隔离验证：旧任务之后的新汇总');
  const beforeNewRun = await page.evaluate(() => window.__chatChanges || 0);
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await page.waitForFunction(before => (window.__chatChanges || 0) > before + 1, beforeNewRun);
  await page.waitForFunction(() => document.querySelector('.run-status-pill')?.getAttribute('data-state') === 'failed');
  const staleCancelResponse = page.waitForResponse(response => response.url().endsWith(`/runs/${oldRunId}`));
  heldCancelReads.get(oldRunId)();
  await (await staleCancelResponse).finished();
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const statusAfterOldRunRead = await page.locator('.run-status-pill').getAttribute('data-state');
  record('old_run_read_cannot_replace_newer_run', () => assert.equal(statusAfterOldRunRead, 'failed'));
  await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
  record('no_page_errors', () => assert.deepEqual(pageErrors, []));
} catch (error) {
  checks.push({ name: 'browser_flow', passed: false, error: error.stack });
} finally {
  heldReplies.splice(0).forEach(resolve => resolve());
  for (const resolve of heldRunReads.values()) resolve();
  for (const resolve of heldStreams.values()) resolve();
  for (const resolve of heldCancelReads.values()) resolve();
  await browser?.close(); await server?.close();
  const report = { status: checks.length > 0 && checks.every(check => check.passed) ? 'passed' : 'failed', checks, page_errors: pageErrors, submitted_requests: requests.length, unique_accepted_tasks: accepted.size, limitations: ['Controlled browser HTTP fixtures; no real model or customer MCP validation.', 'Mobile viewport emulation, not a physical phone.'] };
  await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  if (report.status !== 'passed') process.exitCode = 1;
}
