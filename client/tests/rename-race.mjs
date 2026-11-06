/** Real Chromium regression: a delayed initial GET must not undo a saved rename.
 * Uses controlled HTTP responses, the actual App/ChatPage and the actual DOM.
 * No product server, credentials or model requests are used.
 * Prerequisite: npm install --prefix .local/browser playwright and local Chrome,
 * or set PLAYWRIGHT_MODULE_DIR / BROWSER_EXECUTABLE.
 */
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';

const CLIENT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ROOT = path.dirname(CLIENT);
const requireBrowser = createRequire(path.join(process.env.PLAYWRIGHT_MODULE_DIR || path.join(ROOT, '.local/browser'), 'package.json'));
const { chromium } = requireBrowser('playwright');
const executablePath = process.env.BROWSER_EXECUTABLE || [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
].find(candidate => existsSync(candidate));
const config = { brand_name: '竞态回归测试', upload_max_bytes: 20971520, auth_types: ['none'], model_label: 'Dano-v2.1', default_model_id: 'dano-v2.1', default_reasoning_level: 'high' };
const user = { id: 'user-rename-test', username: 'rename-test', display_name: '测试', role: 'admin', enabled: true };
const assistant = { id: 'assistant-rename-test', name: '标题竞态验收助手', description: '仅测试真实客户端异步状态。', enabled: true, access: { all_members: true, user_ids: [] }, connections: [], instructions: '', revision: 1 };
const models = [{ id: 'dano-v2.1', label: 'Dano-v2.1', available: true, reasoning_levels: ['light', 'high', 'max'].map(id => ({ id, label: id })), default_reasoning_level: 'high' }];
const original = { model_id: 'dano-v2.1', model_label: 'Dano-v2.1', reasoning_level: 'high', id: 'conversation-rename-test', assistant_id: assistant.id, title: '新对话', archived: false, active_run_id: null, created_at: '2026-11-05T00:00:00Z', updated_at: '2026-11-05T00:00:00Z' };
const savedTitle = '已保存的新标题';
let current = null;
let releaseInitialDetail;
let detailWasRequested;
let patchWasSent = false;
const detailRequested = new Promise(resolve => { detailWasRequested = resolve; });
const detailRelease = new Promise(resolve => { releaseInitialDetail = resolve; });
const failures = [];
let server;
let browser;
try {
  server = await createServer({ root: CLIENT, configFile: path.join(CLIENT, 'vite.config.ts'), logLevel: 'error', server: { host: '127.0.0.1', port: 18124, strictPort: true } });
  await server.listen();
  const port = server.httpServer.address().port;
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  const page = await browser.newPage();
  page.on('pageerror', error => failures.push(error.message));
  page.on('console', message => { if (message.type() === 'error') failures.push(message.text()); });
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const endpoint = url.pathname.replace('/api/v1', '');
    const reply = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (endpoint === '/config') return reply(config);
    if (endpoint === '/models') return reply(models);
    if (endpoint === '/auth/me') return reply(user);
    if (endpoint === '/assistants') return reply([assistant]);
    if (endpoint === `/assistants/${assistant.id}/skills`) return reply([]);
    if (endpoint === '/conversations' && method === 'GET') return reply(current ? [current] : []);
    if (endpoint === '/conversations' && method === 'POST') { current = { ...original }; return reply(current, 201); }
    if (endpoint === `/conversations/${original.id}/messages` && method === 'GET') return reply([]);
    if (endpoint === `/conversations/${original.id}` && method === 'PATCH') {
      assert.equal(request.postDataJSON().title, savedTitle);
      patchWasSent = true;
      current = { ...current, title: savedTitle, updated_at: '2026-11-05T00:00:01Z' };
      return reply(current);
    }
    if (endpoint === `/conversations/${original.id}` && method === 'GET') {
      // Capture the stale response before PATCH, then deliver it after PATCH succeeds.
      detailWasRequested();
      await detailRelease;
      return reply(original);
    }
    failures.push(`Unexpected API request: ${method} ${endpoint}`);
    return reply({ error: { code: 'UNEXPECTED_TEST_REQUEST' } }, 500);
  });
  await page.goto(`http://127.0.0.1:${port}`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('button').filter({ has: page.getByRole('heading', { name: assistant.name, exact: true }) }).click();
  await page.locator('.session-page').getByRole('button', {name:'新建会话',exact:true}).first().click();
  await page.getByRole('button',{name:'开始会话',exact:true}).click();
  await detailRequested;
  await page.getByRole('button', { name: '重命名对话', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '重命名对话', exact: true });
  await dialog.getByLabel('对话名称', { exact: true }).fill(savedTitle);
  await dialog.getByRole('button', { name: '保存名称', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert(patchWasSent, 'The regression must actually complete a rename PATCH');
  await page.getByRole('heading', { name: savedTitle, exact: true, level: 1 }).waitFor();
  releaseInitialDetail();
  await page.getByText('正在恢复对话…', { exact: true }).waitFor({ state: 'hidden' });
  assert.equal(await page.locator('.chat-heading h1').innerText(), savedTitle, 'A late initial GET overwrote the saved title');
  assert.equal(await page.locator('.history-item span').innerText(), savedTitle, 'Sidebar did not refresh the saved title');
  await page.getByRole('button', { name: '重命名对话', exact: true }).click();
  assert.equal(await dialog.getByLabel('对话名称', { exact: true }).inputValue(), savedTitle);
  await dialog.getByLabel('对话名称', { exact: true }).fill('尚未保存的草稿');
  assert.equal(await page.locator('.chat-heading h1').innerText(), savedTitle, 'Editing a draft changed the saved header');
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  assert.equal(await page.locator('.chat-heading h1').innerText(), savedTitle, 'Cancel restored a stale parent title');
  assert.deepEqual(failures, [], 'Browser errors or unexpected API calls were recorded');
  console.log('PASS actual Chromium rename race: late GET, persisted header/sidebar, draft isolation and cancel');
} finally {
  releaseInitialDetail?.();
  await browser?.close();
  await server?.close();
}
