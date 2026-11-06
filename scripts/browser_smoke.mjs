/**
 * Real-browser acceptance against the running client/server.
 * Install Playwright under .local/browser. No source mocking or fake responses.
 * Credentials: SMOKE_ADMIN_PASSWORD, SMOKE_PASSWORD_FILE, or --password-file.
 * REAL_MODEL=true opts into one provider-backed tool query. Secrets are never logged.
 */
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const argv = process.argv.slice(2);
const option = (name, fallback = '') => {
  const index = argv.indexOf(name);
  if (index < 0) return fallback;
  if (!argv[index + 1] || argv[index + 1].startsWith('--')) throw new Error(`Missing value for ${name}`);
  return argv[index + 1];
};
if (argv.includes('--help')) {
  process.stdout.write(`Usage: node scripts/browser_smoke.mjs [--base-url URL] [--username NAME] [--password-file PRIVATE_FILE] [--credentials-file PRIVATE_JSON]\n\nEnvironment: SMOKE_BASE_URL (default http://127.0.0.1:18081), SMOKE_ADMIN_USERNAME (admin), SMOKE_ADMIN_PASSWORD, SMOKE_PASSWORD_FILE, SMOKE_MCP_URL (http://127.0.0.1:8767/mcp), DEMO_MCP_TOKEN, REAL_MODEL=true, KEEP_RESOURCES=true, BROWSER_EXECUTABLE, PLAYWRIGHT_MODULE_DIR, BROWSER_REPORT, BROWSER_SCREENSHOTS.\nPasswords and tokens must not be passed as command-line values. REAL_MODEL invokes a real provider and incurs usage.\n`);
  process.exit(0);
}

const baseURL = option('--base-url', process.env.SMOKE_BASE_URL || 'http://127.0.0.1:18081').replace(/\/$/, '');
const parsedBase = new URL(baseURL);
assert(['http:', 'https:'].includes(parsedBase.protocol) && !parsedBase.username && !parsedBase.password, 'Base URL must be HTTP(S) without embedded credentials');
const mcpURL = process.env.SMOKE_MCP_URL || 'http://127.0.0.1:8767/mcp';
const screenshotDir = path.resolve(ROOT, process.env.BROWSER_SCREENSHOTS || '.runtime/screenshots');
const reportPath = path.resolve(ROOT, process.env.BROWSER_REPORT || '.runtime/browser-smoke.json');
const realModel = process.env.REAL_MODEL === 'true';
const keepResources = process.env.KEEP_RESOURCES === 'true';
const runTimeout = Number(process.env.BROWSER_RUN_TIMEOUT_MS || 240000);
const token = process.env.DEMO_MCP_TOKEN || '';
let username = option('--username', process.env.SMOKE_ADMIN_USERNAME || 'admin');
let password = process.env.SMOKE_ADMIN_PASSWORD || '';
const passwordFile = option('--password-file', process.env.SMOKE_PASSWORD_FILE || '');
const credentialFile = option('--credentials-file', process.env.SMOKE_CREDENTIALS_FILE || '');
assert(!(passwordFile && credentialFile), 'Use one credentials file source');
if (credentialFile) {
  let data;
  try { data = JSON.parse(readFileSync(credentialFile, 'utf8')); }
  catch { throw new Error('Cannot read a valid private credential JSON file'); }
  username = data.username;
  password = data.password;
} else if (passwordFile) password = readFileSync(passwordFile, 'utf8').trim();
assert(typeof username === 'string' && username && typeof password === 'string' && password, 'Set SMOKE_ADMIN_PASSWORD or a private credential/password file');

const sensitive = [password, token].filter(Boolean);
const clean = value => {
  let text = String(value);
  for (const secret of sensitive) text = text.split(secret).join('[redacted]');
  return text.replace(/Bearer\s+[A-Za-z0-9._~+\/-]+/gi, 'Bearer [redacted]').slice(0, 4000);
};
const regex = value => new RegExp(value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
const tag = new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 14) + '-' + Math.random().toString(16).slice(2, 7);
const connectionName = `浏览器验收连接 ${tag}`;
const assistantName = `浏览器验收助手 ${tag}`;
const conversationName = `浏览器验收会话 ${tag}`;
const resources = {};
const report = {
  started_at: new Date().toISOString(), tag, real_model_requested: realModel,
  status: 'running', checks: [], screenshots: [], resources,
  console_errors: [], page_errors: [], http_errors: [], request_failures: [],
  expected_initial_auth_401: 0,
  model_message_submissions: 0,
  limitations: ['Only explicitly synthetic MCP data is used; customer business MCP authorization and server-restart recovery are outside this browser check.'],
};
mkdirSync(screenshotDir, { recursive: true });
mkdirSync(path.dirname(reportPath), { recursive: true });

let browser;
let desktop;
let mobile;
let page;
let mobilePage;
let loggedIn = false;

async function loadPlaywright() {
  const locations = [process.env.PLAYWRIGHT_MODULE_DIR, path.join(ROOT, '.local', 'browser'), ROOT].filter(Boolean);
  for (const location of locations) {
    try {
      const require = createRequire(path.join(path.resolve(location), 'package.json'));
      return require('playwright');
    } catch (error) {
      if (error.code !== 'MODULE_NOT_FOUND') throw error;
    }
  }
  throw new Error('Playwright is not installed. Install it under .local/browser or set PLAYWRIGHT_MODULE_DIR.');
}

function chromeExecutable() {
  if (process.env.BROWSER_EXECUTABLE) {
    assert(existsSync(process.env.BROWSER_EXECUTABLE), 'BROWSER_EXECUTABLE does not exist');
    return process.env.BROWSER_EXECUTABLE;
  }
  const candidates = process.platform === 'win32'
    ? [process.env.PROGRAMFILES, process.env['PROGRAMFILES(X86)'], process.env.LOCALAPPDATA].filter(Boolean).map(root => path.join(root, 'Google', 'Chrome', 'Application', 'chrome.exe'))
    : ['/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/opt/google/chrome/chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'];
  return candidates.find(candidate => existsSync(candidate));
}

function observe(target, label) {
  target.on('request', request => {
    if (request.method() === 'POST' && /^\/api\/v1\/conversations\/[^/]+\/messages$/.test(new URL(request.url()).pathname)) report.model_message_submissions += 1;
  });
  target.on('console', message => {
    if (message.type() !== 'error') return;
    const location = message.location();
    const expected = !loggedIn && /\/api\/v1\/auth\/me(?:\?|$)/.test(location.url || '') && /401/.test(message.text());
    if (expected) return; // Documented unauthenticated bootstrap response; not an application failure.
    report.console_errors.push({ page: label, message: clean(message.text()), location: clean(location.url || '') });
  });
  target.on('pageerror', error => report.page_errors.push({ page: label, message: clean(error.message) }));
  target.on('response', response => {
    if (response.status() < 400) return;
    const pathname = new URL(response.url()).pathname;
    if (!loggedIn && pathname === '/api/v1/auth/me' && response.status() === 401) {
      report.expected_initial_auth_401 += 1;
      return;
    }
    report.http_errors.push({ page: label, method: response.request().method(), path: pathname, status: response.status() });
  });
  target.on('requestfailed', request => {
    const failure = request.failure()?.errorText || 'unknown';
    // Closing an EventSource on successful completion or page navigation is expected.
    if (/\/runs\/[^/]+\/events/.test(new URL(request.url()).pathname) && /ABORTED|CANCELLED|cancelled/i.test(failure)) return;
    report.request_failures.push({ page: label, path: new URL(request.url()).pathname, reason: clean(failure) });
  });
}

async function check(name, action) {
  const started = Date.now();
  try {
    await action();
    report.checks.push({ name, status: 'passed', seconds: (Date.now() - started) / 1000 });
    process.stdout.write(`PASS ${name}\n`);
  } catch (error) {
    report.checks.push({ name, status: 'failed', seconds: (Date.now() - started) / 1000, detail: clean(error.message) });
    process.stderr.write(`FAIL ${name}: ${clean(error.message)}\n`);
    throw error;
  }
}

async function screenshot(target, name) {
  await target.evaluate(async () => { if (document.fonts) await document.fonts.ready; });
  const destination = path.join(screenshotDir, `${name}.png`);
  await target.screenshot({ path: destination, fullPage: true, animations: 'disabled', mask: [target.locator('input[type=password]')] });
  report.screenshots.push({ name, path: destination, viewport: target.viewportSize() });
}

async function noPageError(target) {
  const alerts = await target.getByRole('alert').allTextContents();
  assert.equal(alerts.length, 0, `Visible application error: ${alerts.map(clean).join(' | ')}`);
}

async function navigation(target, name) {
  const open = target.getByRole('button', { name: '打开导航', exact: true });
  if (await open.isVisible() && !await target.getByRole('navigation', { name: '主导航' }).isVisible()) await open.click();
  // On a translated offscreen sidebar Playwright may consider the nav visible; use the CSS viewport
  // toggle when the button itself is outside the viewport, without changing application state by JS.
  const nav = target.getByRole('navigation', { name: '主导航' });
  const button = nav.getByRole('button', { name, exact: true });
  if (await open.isVisible()) {
    const box = await button.boundingBox();
    if (!box || box.x < 0) await open.click();
  }
  await button.click();
  if (name !== '工作台') await target.getByRole('heading', { name, exact: true, level: 1 }).waitFor();
  await noPageError(target);
}

async function responseFor(target, method, pathname, action) {
  const pending = target.waitForResponse(response => response.request().method() === method && new URL(response.url()).pathname === pathname, { timeout: 45000 });
  await action();
  const response = await pending;
  assert(response.ok(), `${method} ${pathname} returned HTTP ${response.status()}`);
  return response.status() === 204 ? null : response.json();
}

async function noHorizontalOverflow(target) {
  const sizes = await target.evaluate(() => ({ viewport: window.innerWidth, document: document.documentElement.scrollWidth }));
  assert(sizes.document <= sizes.viewport + 2, `Page overflows horizontally (${sizes.document} > ${sizes.viewport})`);
}

async function cleanup() {
  if (!desktop || !loggedIn) return;
  const errors = [];
  const api = async (method, pathname, data) => {
    const response = await desktop.request.fetch(`${baseURL}/api/v1${pathname}`, { method, data, headers: { Origin: parsedBase.origin } });
    if (!response.ok() && response.status() !== 404) errors.push({ method, path: pathname, status: response.status() });
  };
  if (resources.run_id) {
    try {
      const response = await desktop.request.get(`${baseURL}/api/v1/runs/${resources.run_id}`);
      if (response.ok()) {
        const run = await response.json();
        if (!['succeeded', 'failed', 'cancelled', 'interrupted'].includes(run.status)) await api('POST', `/runs/${resources.run_id}/cancel`);
      }
    } catch (error) { errors.push({ operation: 'cancel', error: clean(error.message) }); }
  }
  if (!keepResources) {
    for (const [method, pathname, data] of [
      ...(resources.conversation_id ? [['PATCH', `/conversations/${resources.conversation_id}`, { archived: true }]] : []),
      ...(resources.assistant_id ? [['DELETE', `/assistants/${resources.assistant_id}`]] : []),
      ...(resources.connection_id ? [['DELETE', `/connections/${resources.connection_id}`]] : []),
    ]) {
      try { await api(method, pathname, data); } catch (error) { errors.push({ operation: pathname, error: clean(error.message) }); }
    }
  }
  report.cleanup = { retained: keepResources, errors, note: 'Uploaded synthetic attachment bytes remain as acceptance evidence; no unrelated resource is removed.' };
}

try {
  const { chromium } = await loadPlaywright();
  const executablePath = chromeExecutable();
  browser = await chromium.launch({ headless: process.env.BROWSER_HEADLESS !== 'false', ...(executablePath ? { executablePath } : {}), ...(process.env.BROWSER_NO_SANDBOX === 'true' ? { args: ['--no-sandbox'] } : {}) });
  desktop = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai', reducedMotion: 'reduce' });
  desktop.setDefaultTimeout(20000);
  page = await desktop.newPage();
  observe(page, 'desktop');

  await check('desktop_login', async () => {
    await page.goto(baseURL, { waitUntil: 'domcontentloaded' });
    await page.getByRole('heading', { name: '登录你的工作台', exact: true }).waitFor();
    await screenshot(page, '01-desktop-login');
    await page.getByLabel(/^账号/).fill(username);
    await page.getByLabel(/^密码/).fill(password);
    const user = await responseFor(page, 'POST', '/api/v1/auth/login', () => page.getByRole('button', { name: '登录', exact: true }).click());
    assert.equal(user.role, 'admin', 'Browser acceptance requires an administrator');
    loggedIn = true;
    await page.getByRole('navigation', { name: '主导航' }).waitFor();
    await page.getByRole('heading', { level: 1 }).waitFor();
    await noPageError(page);
    await screenshot(page, '02-desktop-workspace');
  });

  await check('create_mcp_connection_and_discover_tools', async () => {
    await navigation(page, '业务连接');
    await page.getByRole('button', { name: '新增连接', exact: true }).first().click();
    const dialog = page.getByRole('dialog', { name: '新增业务连接', exact: true });
    await dialog.getByLabel(/^连接名称/).fill(connectionName);
    await dialog.getByLabel(/^MCP 服务地址/).fill(mcpURL);
    await dialog.getByLabel(/^认证方式/).selectOption(token ? 'bearer' : 'none');
    if (token) await dialog.getByLabel(/^Token/).fill(token);
    const connection = await responseFor(page, 'POST', '/api/v1/connections', () => dialog.getByRole('button', { name: '保存连接', exact: true }).click());
    resources.connection_id = connection.id;
    await dialog.waitFor({ state: 'hidden' });
    const card = page.getByRole('article').filter({ has: page.getByRole('heading', { name: connectionName, exact: true }) });
    await card.waitFor();
    const tested = await responseFor(page, 'POST', `/api/v1/connections/${connection.id}/test`, () => card.getByRole('button', { name: '测试连接', exact: true }).click());
    assert.equal(tested.status, 'connected', 'MCP test did not connect');
    for (const name of ['search_knowledge', 'create_ticket', 'list_tickets', 'slow_lookup']) assert(tested.tools.some(tool => tool.name === name), `MCP tool not discovered: ${name}`);
    await card.getByText('已连接', { exact: true }).waitFor();
    await noPageError(page);
    await screenshot(page, '03-desktop-connections');
    await card.getByRole('button', { name: '工具', exact: true }).click();
    const toolsDialog = page.getByRole('dialog', { name: `${connectionName} · 可用工具`, exact: true });
    await toolsDialog.getByText('search_knowledge', { exact: true }).waitFor();
    await screenshot(page, '04-desktop-tool-discovery');
    await toolsDialog.getByRole('button', { name: '关闭', exact: true }).click();
  });

  await check('create_assistant_and_bind_permissions', async () => {
    await navigation(page, '助手设置');
    await page.getByRole('button', { name: '新建助手', exact: true }).first().click();
    const dialog = page.getByRole('dialog', { name: '新建业务助手', exact: true });
    await dialog.getByLabel(/^助手名称/).fill(assistantName);
    await dialog.getByLabel(/^一句话说明/).fill('浏览器验收专用：连接合成演示知识与演示工单。');
    await dialog.getByLabel(/^业务规则/).fill('所有资料均为合成验收数据。使用 search_knowledge 查询演示制度，并明确给出来源编号。不得编造查询结果。创建工单必须等待人工审批。');
    await dialog.getByRole('checkbox', { name: regex(connectionName) }).check();
    await dialog.getByRole('checkbox', { name: '选择全部工具', exact: true }).check();
    for (const tool of ['search_knowledge', 'list_tickets', 'slow_lookup']) await dialog.getByRole('checkbox', { name: `${tool} 免确认执行`, exact: true }).check();
    assert.equal(await dialog.getByRole('checkbox', { name: 'create_ticket 免确认执行', exact: true }).isChecked(), false, 'Write tool must retain approval');
    await screenshot(page, '05-desktop-assistant-permissions');
    const assistant = await responseFor(page, 'POST', '/api/v1/assistants', () => dialog.getByRole('button', { name: '保存助手', exact: true }).click());
    resources.assistant_id = assistant.id;
    assert.equal(assistant.connections.length, 1);
    assert.equal(assistant.connections[0].connection_id, resources.connection_id);
    assert(assistant.connections[0].allowed_tools.includes('create_ticket'));
    assert(!assistant.connections[0].auto_approved_tools.includes('create_ticket'));
    await dialog.waitFor({ state: 'hidden' });
    await page.getByRole('heading', { name: assistantName, exact: true }).waitFor();
    await noPageError(page);
    await screenshot(page, '06-desktop-assistants');
  });

  await check('chat_workspace_attachment_and_history', async () => {
    await navigation(page, '工作台');
    const card = page.getByRole('button').filter({ has: page.getByRole('heading', { name: assistantName, exact: true }) });
    const conversation = await responseFor(page, 'POST', '/api/v1/conversations', () => card.click());
    resources.conversation_id = conversation.id;
    await page.getByRole('textbox', { name: '输入消息', exact: true }).waitFor();
    await page.getByRole('button', { name: '重命名对话', exact: true }).click();
    const renameDialog = page.getByRole('dialog', { name: '重命名对话', exact: true });
    await renameDialog.getByLabel('对话名称', { exact: true }).fill(conversationName);
    await responseFor(page, 'PATCH', `/api/v1/conversations/${conversation.id}`, () => renameDialog.getByRole('button', { name: '保存名称', exact: true }).click());
    await renameDialog.waitFor({ state: 'hidden' });
    await page.getByRole('heading', { name: conversationName, exact: true }).waitFor();
    const attachmentName = `browser-acceptance-${tag}.txt`;
    const file = await responseFor(page, 'POST', '/api/v1/files', () => page.locator('input[type=file]').setInputFiles({ name: attachmentName, mimeType: 'text/plain', buffer: Buffer.from('浏览器验收附件：这是一份合成演示资料。', 'utf8') }));
    resources.attachment_id = file.id;
    await page.getByRole('button', { name: regex(attachmentName) }).first().click();
    const preview = page.getByRole('dialog', { name: attachmentName, exact: true });
    await preview.getByText('浏览器验收附件：这是一份合成演示资料。', { exact: true }).waitFor();
    assert.equal(report.model_message_submissions, 0, 'Previewing an attachment must not submit a model message');
    await screenshot(page, '07-desktop-attachment-preview');
    await preview.getByRole('button', { name: '关闭窗口', exact: true }).click();
    await page.getByRole('button', { name: `移除 ${attachmentName}`, exact: true }).click();
    assert.equal(report.model_message_submissions, 0, 'Removing an attachment must not submit a model message');
    await page.getByRole('button', { name: '执行记录', exact: true }).click();
    await page.getByRole('heading', { name: '执行记录', exact: true }).waitFor();
    await screenshot(page, '08-desktop-chat');
    await noPageError(page);
  });

  if (realModel) {
    await check('real_model_query_rendered_in_browser', async () => {
      await page.getByRole('textbox', { name: '输入消息', exact: true }).fill('请调用 search_knowledge 查询演示设备报修流程。回答普通故障的响应时间并注明知识条目编号，不要创建工单。');
      const submitted = await responseFor(page, 'POST', `/api/v1/conversations/${resources.conversation_id}/messages`, () => page.getByRole('button', { name: '发送消息', exact: true }).click());
      resources.run_id = submitted.run_id;
      await page.waitForFunction(() => {
        const state = document.querySelector('.execution-state .badge')?.textContent || '';
        return ['已完成', '执行失败', '已停止', '执行已中断'].some(label => state.includes(label));
      }, null, { timeout: runTimeout });
      assert.equal((await page.locator('.execution-state .badge').innerText()).trim(), '已完成', 'Model task did not complete successfully');
      await page.locator('.message.assistant .markdown').filter({ hasText: 'DEMO-KB-001' }).waitFor();
      const answer = await page.locator('.message.assistant .markdown').allTextContents();
      assert(answer.some(text => text.includes('4') && text.includes('DEMO-KB-001')), 'Rendered answer lacks known response time and source');
      assert((await page.locator('.event-list').innerText()).includes('search_knowledge'), 'Tool execution is not visible in the UI');
      await noPageError(page);
      await screenshot(page, '09-desktop-real-model-result');
    });
  } else report.limitations.push('REAL_MODEL was not enabled: the browser check did not submit a paid model turn.');

  await check('members_and_usage_navigation', async () => {
    await navigation(page, '成员管理');
    await page.getByRole('table').waitFor();
    assert((await page.getByRole('table').innerText()).includes(username), 'Administrator is missing from member table');
    await screenshot(page, '10-desktop-members');
    await navigation(page, '用量记录');
    await page.getByText('任务执行', { exact: true }).waitFor();
    await page.getByText('成功完成', { exact: true }).waitFor();
    await noPageError(page);
    await screenshot(page, '11-desktop-usage');
  });

  await check('mobile_workspace_connections_and_chat', async () => {
    mobile = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, locale: 'zh-CN', timezoneId: 'Asia/Shanghai', reducedMotion: 'reduce', storageState: await desktop.storageState() });
    mobile.setDefaultTimeout(20000);
    mobilePage = await mobile.newPage();
    observe(mobilePage, 'mobile');
    await mobilePage.goto(baseURL, { waitUntil: 'domcontentloaded' });
    await mobilePage.getByRole('button', { name: '打开导航', exact: true }).waitFor();
    await mobilePage.getByRole('heading', { name: assistantName, exact: true }).waitFor();
    await noHorizontalOverflow(mobilePage);
    await screenshot(mobilePage, '12-mobile-workspace');
    await navigation(mobilePage, '业务连接');
    await mobilePage.getByRole('heading', { name: connectionName, exact: true }).waitFor();
    await noHorizontalOverflow(mobilePage);
    await screenshot(mobilePage, '13-mobile-connections');
    await mobilePage.getByRole('button', { name: '打开导航', exact: true }).click();
    await mobilePage.getByRole('button', { name: regex(conversationName) }).click();
    await mobilePage.getByRole('heading', { name: conversationName, exact: true }).waitFor();
    await mobilePage.getByRole('textbox', { name: '输入消息', exact: true }).waitFor();
    // Capture restored content, not the initial conversation-loading placeholder.
    await mobilePage.getByText('正在恢复对话…', { exact: true }).waitFor({ state: 'hidden' });
    if (realModel) await mobilePage.locator('.message.assistant .markdown').filter({ hasText: 'DEMO-KB-001' }).first().waitFor({ state: 'visible' });
    await noPageError(mobilePage);
    await noHorizontalOverflow(mobilePage);
    await screenshot(mobilePage, '14-mobile-chat');
  });

  await check('no_unexpected_browser_errors', async () => {
    // The initial /auth/me 401 is expected and recorded separately. Everything else fails the check.
    assert.equal(report.page_errors.length, 0, 'Browser JavaScript page errors were recorded');
    assert.equal(report.console_errors.length, 0, 'Unexpected browser console errors were recorded');
    assert.equal(report.http_errors.length, 0, 'Unexpected HTTP error responses were recorded');
    assert.equal(report.request_failures.length, 0, 'Unexpected network failures were recorded');
    assert.equal(report.model_message_submissions, realModel ? 1 : 0, 'Unexpected model message submission count');
  });
  report.status = 'passed';
} catch (error) {
  report.status = 'failed';
  report.error = clean(error.message || error);
  process.stderr.write(`Browser acceptance failed: ${report.error}\n`);
  if (page && !page.isClosed()) {
    try { await screenshot(page, 'failure-desktop'); } catch {}
  }
  if (mobilePage && !mobilePage.isClosed()) {
    try { await screenshot(mobilePage, 'failure-mobile'); } catch {}
  }
} finally {
  try { await cleanup(); } catch (error) { report.cleanup = { error: clean(error.message) }; report.status = 'failed'; }
  if (report.cleanup?.errors?.length) report.status = 'failed';
  if (loggedIn && page && !page.isClosed()) {
    try {
      await page.getByRole('button', { name: '退出登录', exact: true }).click();
      await page.getByRole('heading', { name: '登录你的工作台', exact: true }).waitFor();
      report.logout_verified = true;
    } catch (error) { report.logout_verified = false; report.logout_error = clean(error.message); report.status = 'failed'; }
  }
  await mobile?.close().catch(() => {});
  await desktop?.close().catch(() => {});
  await browser?.close().catch(() => {});
  report.finished_at = new Date().toISOString();
  writeFileSync(reportPath, JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
  process.stdout.write(`Report: ${reportPath}\n`);
  process.exitCode = report.status === 'passed' ? 0 : 1;
}
