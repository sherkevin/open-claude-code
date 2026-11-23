/** Real Chromium UI regression using controlled HTTP fixtures. Zero upstream/model calls.
 * Run from repository root: node client/tests/model-selection.mjs
 * Requires the same .local/browser Playwright setup as rename-race.mjs.
 */
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';

const CLIENT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ROOT = path.dirname(CLIENT);
const OUTPUT = path.join(ROOT, '.runtime', 'client-model-selection', new Date().toISOString().replace(/[:.]/g, '-'));
await mkdir(OUTPUT, { recursive: true });
const requireBrowser = createRequire(path.join(process.env.PLAYWRIGHT_MODULE_DIR || path.join(ROOT, '.local/browser'), 'package.json'));
const { chromium } = requireBrowser('playwright');
const executablePath = process.env.BROWSER_EXECUTABLE || ['C:/Program Files/Google/Chrome/Application/chrome.exe', '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser'].find(candidate => existsSync(candidate));
const models = [
  { id: 'deepseek-v4.1-flash', label: 'deepseek-v4.1-flash', available: true, reasoning_levels: ['low', 'high', 'max'].map(id => ({ id, label: id })), default_reasoning_level: 'high' },
  { id: 'glm-5.3', label: 'GLM-5.3', available: true, reasoning_levels: [{ id: 'default', label: '默认' }], default_reasoning_level: 'default' },
  { id: 'dano-v2.1', label: 'Dano-v2.1', available: true, reasoning_levels: ['light', 'high', 'max'].map(id => ({ id, label: id })), default_reasoning_level: 'high' },
];
const config = { brand_name: '模型交互验收', upload_max_bytes: 20971520, auth_types: ['none'], model_label: 'Dano-v2.1', default_model_id: 'dano-v2.1', default_reasoning_level: 'high' };
const user = { id: 'user-model-test', username: 'model-test', display_name: '测试', role: 'admin', enabled: true };
const assistant = { id: 'assistant-model-test', name: '模型交互验收助手', description: '仅测试真实客户端选择器与服务端状态。', enabled: true, access: { all_members: true, user_ids: [] }, connections: [], instructions: '', revision: 1 };
const timestamp = '2026-11-05T00:00:00Z';
const baseConversation = { id: 'conversation-model-test', assistant_id: assistant.id, title: '模型选择验收', archived: false, active_run_id: null, created_at: timestamp, updated_at: timestamp };
let current = null;
let rejectPatch = false;
let glmUnavailable = false;
let messagePosts = 0;
let patchRequests = [];
let createRequests = [];
let releasePendingPatch;
let holdNextPatch = false;
const failures = [];
let server, browser;
try {
  server = await createServer({ root: CLIENT, configFile: path.join(CLIENT, 'vite.config.ts'), logLevel: 'error', server: { host: '127.0.0.1', port: 18124, strictPort: true } });
  await server.listen();
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on('pageerror', error => failures.push(error.message));
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const endpoint = new URL(request.url()).pathname.replace('/api/v1', '');
    const method = request.method();
    const reply = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (endpoint === '/config') return reply(config);
    if (endpoint === '/auth/me') return reply(user);
    if (endpoint === '/models') return reply(models.map(model => model.id === 'glm-5.3' && glmUnavailable ? { ...model, available: false, unavailable_reason: '测试线路尚未启用' } : model));
    if (endpoint === '/assistants') return reply([assistant]);
    if (endpoint === `/assistants/${assistant.id}/skills`) return reply([]);
    if (endpoint === '/conversations' && method === 'GET') return reply(current ? [current] : []);
    if (endpoint === '/conversations' && method === 'POST') {
      const body = request.postDataJSON(); createRequests.push(body);
      current = { ...baseConversation, ...body, model_label: models.find(model => model.id === body.model_id).label };
      return reply(current, 201);
    }
    if (endpoint === `/conversations/${baseConversation.id}/messages` && method === 'GET') return reply([]);
    if (endpoint === `/conversations/${baseConversation.id}` && method === 'GET') return reply(current);
    if (endpoint === `/conversations/${baseConversation.id}` && method === 'PATCH') {
      const body = request.postDataJSON(); patchRequests.push(body);
      if (holdNextPatch) { holdNextPatch = false; await new Promise(resolve => { releasePendingPatch = resolve; }); }
      if (rejectPatch) { rejectPatch = false; return reply({ error: { code: 'MODEL_UNAVAILABLE', message: '测试：模型切换失败，原选择保持不变。' } }, 503); }
      current = { ...current, ...body, model_label: models.find(model => model.id === body.model_id).label };
      return reply(current);
    }
    if (endpoint === `/conversations/${baseConversation.id}/context`) return reply({ source: 'native_runtime', status: 'available', total_tokens: 60, max_tokens: 1000, percentage: 6, auto_compact_enabled: true, auto_compact_threshold: null, model: 'private-provider-route-SECRET', measured_at: timestamp, cached: true, error: null, last_compaction: null });
    if (endpoint === `/conversations/${baseConversation.id}/messages` && method === 'POST') { messagePosts += 1; failures.push('Selector interaction submitted a message'); return reply({}, 500); }
    if (endpoint === '/runs/run-active-model-test') return reply({ id: 'run-active-model-test', conversation_id: baseConversation.id, status: 'running', assistant_message_id: null, error: null, usage: null, approvals: [], created_at: timestamp, completed_at: null });
    if (endpoint === '/runs/run-active-model-test/events') return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ': fixture heartbeat\n\n' });
    failures.push(`Unexpected request: ${method} ${endpoint}`);
    return reply({}, 500);
  });
  const url = `http://127.0.0.1:${server.httpServer.address().port}`;
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.getByRole('button').filter({ has: page.getByRole('heading', { name: assistant.name, exact: true }) }).click();
  assert.equal(createRequests.length, 0, 'Opening an assistant must not create a session');
  await page.locator('.session-page').getByRole('button', { name: '新建会话', exact: true }).first().click();
  const model = page.getByRole('combobox', { name: '模型', exact: true });
  const level = page.getByRole('combobox', { name: '推理级别', exact: true });
  await model.waitFor();
  await page.waitForFunction(() => document.querySelector('select[aria-label="模型"]')?.value === 'dano-v2.1');
  assert.deepEqual(await model.locator('option').allTextContents(), models.map(model => model.label));
  assert.deepEqual(await level.locator('option').allTextContents(), ['light', 'high', 'max']);
  await level.selectOption('max');
  await page.getByRole('button', {name: '开始会话', exact:true}).click();
  await page.getByText('正在恢复对话…', { exact: true }).waitFor({ state: 'hidden' });
  assert.equal(createRequests[0].model_id, 'dano-v2.1'); assert.equal(createRequests[0].reasoning_level, 'max');
  assert.equal(await level.inputValue(), 'max');
  assert.match(await page.locator('.composer-footnote').innerText(), /Dano-v2.1 · max/);
  await model.selectOption('glm-5.3');
  await page.waitForFunction(() => document.querySelector('select[aria-label="模型"]')?.value === 'glm-5.3');
  assert.equal(await level.inputValue(), 'default'); assert(await level.isDisabled());
  assert(!/Dano/.test(await page.locator('.composer-footnote').innerText()), 'GLM must keep its own public identity');
  await model.selectOption('deepseek-v4.1-flash');
  await page.waitForFunction(() => document.querySelector('select[aria-label="模型"]')?.value === 'deepseek-v4.1-flash');
  assert.equal(await level.inputValue(), 'high');
  await level.selectOption('low');
  await page.waitForFunction(() => document.querySelector('select[aria-label="推理级别"]')?.value === 'low');
  rejectPatch = true;
  await model.selectOption('dano-v2.1');
  await page.getByText('测试：模型切换失败，原选择保持不变。', { exact: true }).waitFor();
  assert.equal(await model.inputValue(), 'deepseek-v4.1-flash'); assert.equal(await level.inputValue(), 'low');
  holdNextPatch = true;
  await model.selectOption('dano-v2.1');
  await page.getByText('正在保存…', { exact: true }).waitFor();
  assert(await model.isDisabled()); assert(await level.isDisabled());
  assert.equal(await page.getByRole('button', { name: '上下文', exact: true }).count(), 0);
  await page.getByLabel('输入消息', { exact: true }).fill('不会发送的草稿');
  assert(await page.getByRole('button', { name: '发送消息', exact: true }).isDisabled());
  releasePendingPatch();
  await page.waitForFunction(() => document.querySelector('select[aria-label="模型"]')?.value === 'dano-v2.1');
  await level.selectOption('light');
  await page.waitForFunction(() => document.querySelector('select[aria-label="推理级别"]')?.value === 'light');
  await page.getByRole('button', { name: '内容轨迹', exact: true }).click();
  const context = page.getByRole('dialog', { name: /轨迹$/ });
  await context.getByRole('tab', { name: '上下文', exact: true }).click();
  await context.getByText('模型信息暂不可用', { exact: true }).waitFor();
  assert(!(await context.innerText()).includes('private-provider-route'));
  assert(await model.isDisabled());
  await context.getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByRole('button', { name: '会话管理', exact: true }).click();
  await page.locator('.history-item').filter({ hasText: baseConversation.title }).click();
  await page.getByText('正在恢复对话…', { exact: true }).waitFor({ state: 'hidden' });
  assert.equal(await model.inputValue(), 'dano-v2.1'); assert.equal(await level.inputValue(), 'light');
  await page.screenshot({ path: path.join(OUTPUT, 'desktop-model-selection.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForFunction(() => document.querySelector('.sidebar').getBoundingClientRect().right <= 1);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, 'Mobile page must not overflow');
  assert(await model.isVisible()); assert(await level.isVisible());
  await page.screenshot({ path: path.join(OUTPUT, 'mobile-model-selection.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  glmUnavailable = true;
  current = { ...current, active_run_id: 'run-active-model-test' };
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.locator('.history-item').filter({ hasText: baseConversation.title }).click();
  await page.getByText('正在恢复对话…', { exact: true }).waitFor({ state: 'hidden' });
  assert(await model.isDisabled()); assert(await level.isDisabled());
  assert(await model.locator('option[value="glm-5.3"]').isDisabled());
  assert.match(await model.locator('option[value="glm-5.3"]').innerText(), /暂不可用/);
  assert.equal(await model.inputValue(), 'dano-v2.1'); assert.equal(await level.inputValue(), 'light');
  assert.equal(messagePosts, 0); assert.equal(createRequests.length, 1); assert.equal(patchRequests.length, 6);
  assert.deepEqual(failures, []);
  await writeFile(path.join(OUTPUT, 'report.json'), JSON.stringify({ status: 'passed', source: 'real Chromium with controlled HTTP fixtures', model_calls: 0, message_posts: messagePosts, create_requests: createRequests.length, patch_requests: patchRequests.length, unexpected_errors: failures }, null, 2));
  console.log(`Evidence: ${OUTPUT}`);
  console.log('PASS actual Chromium model selectors: three options, create/effort, provider labels, switch failure, pending lock, context redaction, history, active-run lock, unavailable option, mobile overflow; 0 message/model calls.');
} finally {
  releasePendingPatch?.();
  await browser?.close();
  await server?.close();
}
