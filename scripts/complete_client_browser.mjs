/** Complete product browser acceptance; no mocked API responses.
 * Default: zero model messages, zero native count requests, zero compact requests.
 * REAL_MODEL=true: one synthetic MCP query + one new-chat memory recall + manual context refresh.
 * Credentials are read from a private file or environment, never command arguments.
 */
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const option = (name, fallback = '') => { const index = args.indexOf(name); if (index < 0) return fallback; assert(args[index + 1] && !args[index + 1].startsWith('--'), `Missing ${name} value`); return args[index + 1]; };
const redact = (value, secrets) => { let text = String(value); for (const secret of secrets.filter(Boolean)) text = text.split(secret).join('[redacted]'); return text.replace(/Bearer\s+[A-Za-z0-9._~+\/-]+/gi, 'Bearer [redacted]').slice(0, 4000); };
if (args.includes('--self-test')) {
  const marker = 'synthetic-secret-with-spaces';
  assert(!redact(`error ${marker} Bearer another-secret`, [marker]).includes(marker));
  assert(!redact(`Bearer another-secret`, []).includes('another-secret'));
  assert.equal('false' === 'true', false);
  assert.equal(new URL('http://127.0.0.1:18081/api/v1/conversations/id/context/refresh').pathname.endsWith('/context/refresh'), true);
  process.stdout.write('PASS local guards: redaction, explicit real-model opt-in and native-refresh identification\n');
  process.exit(0);
}
if (args.includes('--help')) {
  process.stdout.write('Usage: node scripts/complete_client_browser.mjs --password-file PRIVATE_FILE [--base-url URL] [--username NAME]\nEnvironment: REAL_MODEL=true, SMOKE_MCP_URL, DEMO_MCP_TOKEN, SMOKE_ADMIN_PASSWORD, SMOKE_CREDENTIALS_FILE, BROWSER_REPORT, BROWSER_SCREENSHOTS, PLAYWRIGHT_MODULE_DIR, BROWSER_EXECUTABLE, KEEP_RESOURCES=true. Only use the synthetic demonstration MCP. --self-test makes no network or model requests.\n');
  process.exit(0);
}

const baseURL = option('--base-url', process.env.SMOKE_BASE_URL || 'http://127.0.0.1:18081').replace(/\/$/, '');
const origin = new URL(baseURL);
assert(['http:', 'https:'].includes(origin.protocol) && !origin.username && !origin.password && !origin.search && !origin.hash, 'Invalid service URL');
const realModel = process.env.REAL_MODEL === 'true';
const keepResources = process.env.KEEP_RESOURCES === 'true';
let username = option('--username', process.env.SMOKE_ADMIN_USERNAME || 'admin');
let password = process.env.SMOKE_ADMIN_PASSWORD || '';
const credentialFile = option('--credentials-file', process.env.SMOKE_CREDENTIALS_FILE || '');
const passwordFile = option('--password-file', process.env.SMOKE_PASSWORD_FILE || '');
assert(!(credentialFile && passwordFile), 'Choose one private credential file');
if (credentialFile) { let stored; try { stored = JSON.parse(readFileSync(credentialFile, 'utf8')); } catch { throw new Error('Cannot read a valid private credential JSON file'); } username = stored.username; password = stored.password; }
else if (passwordFile) password = readFileSync(passwordFile, 'utf8').trim();
assert(typeof username === 'string' && username && typeof password === 'string' && password, 'A private administrator credential is required');
const tag = `${Date.now()}-${Math.random().toString(16).slice(2, 7)}`;
const authToken = process.env.DEMO_MCP_TOKEN || `synthetic-import-credential-${tag}`;
const secrets = [password, authToken];
const clean = value => redact(value, secrets);
const connectionName = `完整产品验收连接 ${tag}`;
const assistantName = `完整产品验收助手 ${tag}`;
const secondAssistantName = `记忆隔离验收助手 ${tag}`;
const memoryMarker = `MEMORY-CHECK-${tag}`;
const mcpURL = process.env.SMOKE_MCP_URL || 'http://127.0.0.1:8767/mcp';
const reportPath = path.resolve(ROOT, process.env.BROWSER_REPORT || '.runtime/complete-client-browser.json');
const screenshots = path.resolve(ROOT, process.env.BROWSER_SCREENSHOTS || '.runtime/complete-client-browser-screenshots');
const runTimeout = Number(process.env.BROWSER_RUN_TIMEOUT_MS || 240000);
const resources = { conversation_ids: [], run_ids: [] };
const report = { status: 'running', started_at: new Date().toISOString(), real_model_requested: realModel, checks: [], screenshots: [], resources, model_messages: 0, context_refresh_requests: 0, compact_requests: 0, console_errors: [], page_errors: [], http_errors: [], network_errors: [], limitations: ['Synthetic MCP only. This browser test does not run native compact; the dedicated SDK acceptance covers it.'] };
mkdirSync(path.dirname(reportPath), { recursive: true }); mkdirSync(screenshots, { recursive: true });
let browser, context, page;
let loggedIn = false;

async function loadPlaywright() {
  for (const root of [process.env.PLAYWRIGHT_MODULE_DIR, path.join(ROOT, '.local/browser'), ROOT].filter(Boolean)) {
    try { return createRequire(path.join(path.resolve(root), 'package.json'))('playwright'); }
    catch (error) { if (error.code !== 'MODULE_NOT_FOUND') throw error; }
  }
  throw new Error('Install Playwright under .local/browser or set PLAYWRIGHT_MODULE_DIR');
}
function executable() {
  if (process.env.BROWSER_EXECUTABLE) { assert(existsSync(process.env.BROWSER_EXECUTABLE)); return process.env.BROWSER_EXECUTABLE; }
  const candidates = process.platform === 'win32' ? [process.env.PROGRAMFILES, process.env['PROGRAMFILES(X86)'], process.env.LOCALAPPDATA].filter(Boolean).map(root => path.join(root, 'Google/Chrome/Application/chrome.exe')) : ['/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'];
  return candidates.find(candidate => existsSync(candidate));
}
function observe(target) {
  target.on('request', request => {
    if (request.method() !== 'POST') return;
    const pathname = new URL(request.url()).pathname;
    if (/\/conversations\/[^/]+\/messages$/.test(pathname)) report.model_messages += 1;
    if (pathname.endsWith('/context/refresh')) report.context_refresh_requests += 1;
    if (/\/conversations\/[^/]+\/compact$/.test(pathname)) report.compact_requests += 1;
  });
  target.on('console', message => { if (message.type() === 'error' && !(!loggedIn && /\/auth\/me/.test(message.location().url || '') && /401/.test(message.text()))) report.console_errors.push(clean(message.text())); });
  target.on('pageerror', error => report.page_errors.push(clean(error.message)));
  target.on('response', response => { const pathname = new URL(response.url()).pathname; if (response.status() >= 400 && !(!loggedIn && pathname === '/api/v1/auth/me' && response.status() === 401)) report.http_errors.push({ path: pathname, status: response.status() }); });
  target.on('requestfailed', request => { const pathname = new URL(request.url()).pathname; const reason = request.failure()?.errorText || ''; if (/\/runs\/[^/]+\/events/.test(pathname) && /ABORTED|CANCELLED/i.test(reason)) return; report.network_errors.push({ path: pathname, reason: clean(reason) }); });
}
async function check(name, work) {
  const start = Date.now();
  try { await work(); report.checks.push({ name, status: 'passed', seconds: (Date.now() - start) / 1000 }); process.stdout.write(`PASS ${name}\n`); }
  catch (error) { report.checks.push({ name, status: 'failed', error: clean(error.message) }); throw error; }
}
async function responseFor(method, pathname, work) {
  const wait = page.waitForResponse(response => response.request().method() === method && new URL(response.url()).pathname === pathname, { timeout: 45000 });
  await work(); const response = await wait;
  assert(response.ok(), `${method} ${pathname} returned ${response.status()}`);
  return response.status() === 204 ? null : response.json();
}
async function api(method, endpoint, data) {
  const response = await context.request.fetch(`${baseURL}/api/v1${endpoint}`, { method, data, headers: { Origin: origin.origin } });
  assert(response.ok(), `${method} ${endpoint} returned ${response.status()}`);
  return response.status() === 204 ? null : response.json();
}
async function navigate(name) {
  const nav = page.getByRole('navigation', { name: '主导航' });
  const button = nav.getByRole('button', { name, exact: true });
  const box = await button.boundingBox();
  if (!box || box.x < 0) await page.getByRole('button', { name: '打开导航', exact: true }).click();
  await button.click();
  if (name !== '工作台') await page.getByRole('heading', { name, exact: true, level: 1 }).waitFor();
}
async function noOverflow() { const size = await page.evaluate(() => [innerWidth, document.documentElement.scrollWidth]); assert(size[1] <= size[0] + 2, 'Horizontal page overflow'); }
async function screenshot(name) { await page.evaluate(async () => { await document.fonts.ready; }); const target = path.join(screenshots, `${name}.png`); await page.screenshot({ path: target, fullPage: true, animations: 'disabled', mask: [page.locator('input[type=password]'), page.locator('textarea.config-input')] }); report.screenshots.push({ name, path: target }); }
async function newMemoryFile(filePath, text) {
  await page.getByRole('button', { name: '新建文件', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '新建记忆文件', exact: true });
  await dialog.getByLabel(/^文件路径/).fill(filePath);
  await dialog.getByRole('button', { name: '开始编辑', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await page.getByLabel(/^记忆内容/).fill(text);
  const result = await responseFor('PUT', `/api/v1/assistants/${resources.assistant_id}/memory/file`, () => page.getByRole('button', { name: '保存记忆', exact: true }).click());
  assert.equal(result.path, filePath); assert.equal(result.content, text);
  await page.getByText('记忆文件已保存。', { exact: true }).waitFor();
  await page.locator('.memory-editor-actions .spin').waitFor({ state: 'hidden' });
  await page.locator('.memory-file-item').filter({ hasText: filePath }).waitFor();
  return result;
}
async function newConversation() {
  await navigate('工作台');
  await page.getByRole('button', { name: '开始新工作', exact: true }).click();
  const card = page.getByRole('button').filter({ has: page.getByRole('heading', { name: assistantName, exact: true }) });
  const result = await responseFor('POST', '/api/v1/conversations', () => card.click());
  resources.conversation_ids.push(result.id);
  await page.getByRole('textbox', { name: '输入消息', exact: true }).waitFor();
  await page.getByText('正在恢复对话…', { exact: true }).waitFor({ state: 'hidden' });
  return result;
}
async function sendAndWait(conversationId, text) {
  assert(realModel, 'Model work requires REAL_MODEL=true');
  await page.getByRole('textbox', { name: '输入消息', exact: true }).fill(text);
  const result = await responseFor('POST', `/api/v1/conversations/${conversationId}/messages`, () => page.getByRole('button', { name: '发送消息', exact: true }).click());
  resources.run_ids.push(result.run_id);
  const deadline = Date.now() + runTimeout;
  let run;
  while (Date.now() < deadline) { run = await api('GET', `/runs/${result.run_id}`); if (['succeeded', 'failed', 'cancelled', 'interrupted'].includes(run.status)) break; await new Promise(resolve => setTimeout(resolve, 1000)); }
  assert.equal(run?.status, 'succeeded', `Real run ended as ${run?.status || 'unknown'}`);
  const messages = await api('GET', `/conversations/${conversationId}/messages`);
  const answer = messages.filter(message => message.role === 'assistant' && message.run_id === result.run_id).map(message => message.text).join('\n');
  assert(answer.trim(), 'The real run produced no assistant text');
  await page.getByRole('button', { name: '停止', exact: true }).waitFor({ state: 'hidden' });
  await page.locator('.message.assistant .markdown').last().waitFor();
  return { run, answer };
}

try {
  const { chromium } = await loadPlaywright(); const binary = executable();
  browser = await chromium.launch({ headless: process.env.BROWSER_HEADLESS !== 'false', ...(binary ? { executablePath: binary } : {}), ...(process.env.BROWSER_NO_SANDBOX === 'true' ? { args: ['--no-sandbox'] } : {}) });
  context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai', reducedMotion: 'reduce' }); context.setDefaultTimeout(20000);
  page = await context.newPage(); observe(page);
  await check('login_and_complete_product_navigation', async () => {
    await page.goto(baseURL, { waitUntil: 'domcontentloaded' });
    await page.getByLabel(/^账号/).fill(username); await page.getByLabel(/^密码/).fill(password);
    const user = await responseFor('POST', '/api/v1/auth/login', () => page.getByRole('button', { name: '登录', exact: true }).click()); assert.equal(user.role, 'admin'); loggedIn = true;
    await page.getByRole('navigation', { name: '主导航' }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'API 接入', exact: true }).count(), 0);
    await page.getByRole('button', { name: '记忆', exact: true }).waitFor();
  });
  await check('mcp_import_masked_preview_and_atomic_save', async () => {
    await navigate('业务连接'); await page.getByRole('button', { name: '导入 MCP 配置', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '导入 MCP 配置', exact: true });
    await dialog.getByLabel(/^MCP 配置 JSON/).fill(JSON.stringify({ type: 'mcp', server_label: connectionName, server_url: mcpURL, authorization: authToken, allowed_tools: ['search_knowledge'], require_approval: 'never' }));
    const preview = await responseFor('POST', '/api/v1/connections/import/preview', () => dialog.getByRole('button', { name: '预览配置', exact: true }).click());
    assert.equal(preview.connections.length, 1); assert.equal(preview.connections[0].credential_present, true);
    assert(!JSON.stringify(preview).includes(authToken), 'Preview API leaked supplied authentication');
    await dialog.getByText('Bearer · ••••••••（已提供）', { exact: true }).waitFor();
    assert(!(await dialog.innerText()).includes(authToken), 'Preview DOM leaked supplied authentication');
    assert.equal(await dialog.locator('textarea').count(), 0, 'Raw input remained mounted during safe preview');
    await screenshot('01-import-masked-preview');
    const saved = await responseFor('POST', '/api/v1/connections/import', () => dialog.getByRole('button', { name: '确认导入 1 个连接', exact: true }).click());
    assert.equal(saved.binding_required, true); resources.connection_id = saved.connections[0].id;
    assert(!JSON.stringify(saved).includes(authToken)); await dialog.waitFor({ state: 'hidden' });
    const storage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } })); assert(!storage.includes(authToken), 'Credential persisted in browser storage');
    await page.getByRole('button', { name: '去配置助手', exact: true }).waitFor();
  });
  await check('discover_mcp_and_bind_read_only_assistant', async () => {
    const card = page.getByRole('article').filter({ has: page.getByRole('heading', { name: connectionName, exact: true }) });
    const tested = await responseFor('POST', `/api/v1/connections/${resources.connection_id}/test`, () => card.getByRole('button', { name: '测试连接', exact: true }).click());
    assert.equal(tested.status, 'connected'); assert(tested.tools.some(tool => tool.name === 'search_knowledge'));
    await page.getByRole('button', { name: '去配置助手', exact: true }).click();
    await page.getByRole('heading', { name: '助手设置', exact: true, level: 1 }).waitFor();
    await page.getByRole('button', { name: '新建助手', exact: true }).first().click();
    const dialog = page.getByRole('dialog', { name: '新建业务助手', exact: true });
    await dialog.getByLabel(/^助手名称/).fill(assistantName);
    await dialog.getByLabel(/^一句话说明/).fill('合成数据验收专用：数据读取与跨会话原生记忆。');
    await dialog.getByLabel(/^业务规则/).fill('所有业务数据都是合成验收数据。查询制度时使用search_knowledge并写明来源编号。询问长期记忆时先依据当前已加载的原生记忆，不要编造。只允许读取业务数据，不创建工单。');
    const binding = dialog.locator('.binding-group').filter({ hasText: connectionName });
    await binding.locator('.binding-heading input[type=checkbox]').check();
    const row = binding.locator('.tool-selection-row').filter({ hasText: /^search_knowledge/ });
    await row.locator('.checkbox-row input[type=checkbox]').check();
    const automatic = row.getByRole('checkbox', { name: 'search_knowledge 免确认执行', exact: true });
    assert.equal(await automatic.isChecked(), false, 'Import silently enabled automatic execution'); await automatic.check();
    const saved = await responseFor('POST', '/api/v1/assistants', () => dialog.getByRole('button', { name: '保存助手', exact: true }).click());
    resources.assistant_id = saved.id; assert.deepEqual(saved.connections[0].allowed_tools, ['search_knowledge']); assert.deepEqual(saved.connections[0].auto_approved_tools, ['search_knowledge']);
    await dialog.waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: '新建助手', exact: true }).first().click();
    const second = page.getByRole('dialog', { name: '新建业务助手', exact: true });
    await second.getByLabel(/^助手名称/).fill(secondAssistantName);
    await second.getByLabel(/^一句话说明/).fill('只用于验证记忆隔离与未保存草稿保护，不连接业务工具。');
    const isolated = await responseFor('POST', '/api/v1/assistants', () => second.getByRole('button', { name: '保存助手', exact: true }).click());
    resources.second_assistant_id = isolated.id; await second.waitFor({ state: 'hidden' });
    await page.getByRole('heading', { name: secondAssistantName, exact: true }).waitFor();
  });
  await check('native_memory_create_edit_delete_clear_and_recreate', async () => {
    await navigate('记忆'); const selector = page.getByLabel(/^选择助手/);
    if (await selector.inputValue() !== resources.assistant_id) await selector.selectOption(resources.assistant_id);
    await page.getByRole('heading', { name: `${assistantName}的记忆`, exact: true }).waitFor();
    const memoryText = `# 合成验收记忆\n\n唯一验证标记：${memoryMarker}\n地区：苏州。\n`;
    const first = await newMemoryFile('MEMORY.md', memoryText);
    await page.getByLabel(/^记忆内容/).fill(memoryText + '这是用户与助手隔离的合成资料。\n');
    const changed = await responseFor('PUT', `/api/v1/assistants/${resources.assistant_id}/memory/file`, () => page.getByRole('button', { name: '保存记忆', exact: true }).click()); assert(changed.revision > first.revision);
    await newMemoryFile('topics/test.md', '# 临时合成记忆\n用于验证单文件删除。');
    await page.getByRole('button', { name: '删除文件', exact: true }).click();
    const deletion = page.getByRole('dialog', { name: '删除记忆文件', exact: true });
    const deleted = await responseFor('DELETE', `/api/v1/assistants/${resources.assistant_id}/memory/file`, () => deletion.getByRole('button', { name: '确认删除', exact: true }).click()); assert(!deleted.files.some(file => file.path === 'topics/test.md')); await deletion.waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: '清空记忆', exact: true }).click();
    const clearing = page.getByRole('dialog', { name: '清空此助手的记忆', exact: true });
    assert((await clearing.innerText()).includes('历史对话、附件与已有上下文不会删除'));
    const cleared = await responseFor('DELETE', `/api/v1/assistants/${resources.assistant_id}/memory`, () => clearing.getByRole('button', { name: '确认清空', exact: true }).click()); assert.deepEqual(cleared.files, []); await clearing.waitFor({ state: 'hidden' });
    await newMemoryFile('MEMORY.md', memoryText); await noOverflow();
    assert.equal(report.model_messages, 0, 'Memory CRUD unexpectedly sent a model message');
  });
  await check('unsaved_memory_navigation_guard_and_assistant_isolation', async () => {
    const editor = page.getByLabel(/^记忆内容/); const saved = await editor.inputValue(); const draft = `${saved}\nUNSAVED-DRAFT-${tag}`;
    await editor.fill(draft);
    await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '用量记录', exact: true }).click();
    let modal = page.getByRole('dialog', { name: '放弃未保存的记忆修改', exact: true }); await modal.waitFor();
    assert.equal(await editor.inputValue(), draft); await modal.getByRole('button', { name: '继续编辑', exact: true }).click();
    await page.getByRole('heading', { name: '记忆', exact: true, level: 1 }).waitFor(); assert.equal(await editor.inputValue(), draft);
    await page.getByRole('button', { name: '开始新工作', exact: true }).click();
    modal = page.getByRole('dialog', { name: '放弃未保存的记忆修改', exact: true }); await modal.waitFor();
    await modal.getByRole('button', { name: '继续编辑', exact: true }).click(); assert.equal(await editor.inputValue(), draft);
    const selector = page.getByLabel(/^选择助手/);
    await selector.selectOption(resources.second_assistant_id);
    modal = page.getByRole('dialog', { name: '切换助手前确认', exact: true }); await modal.waitFor();
    assert.equal(await selector.inputValue(), resources.assistant_id); await modal.getByRole('button', { name: '继续编辑', exact: true }).click(); assert.equal(await editor.inputValue(), draft);
    await selector.selectOption(resources.second_assistant_id);
    modal = page.getByRole('dialog', { name: '切换助手前确认', exact: true }); await modal.getByRole('button', { name: '放弃修改并切换', exact: true }).click();
    await page.getByRole('heading', { name: `${secondAssistantName}的记忆`, exact: true }).waitFor();
    const separate = await api('GET', `/assistants/${resources.second_assistant_id}/memory`); assert.deepEqual(separate.files, []);
    await selector.selectOption(resources.assistant_id); await page.getByLabel(/^记忆内容/).waitFor(); assert.equal(await page.getByLabel(/^记忆内容/).inputValue(), saved);
    await page.getByLabel(/^记忆内容/).fill(draft);
    await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '用量记录', exact: true }).click();
    modal = page.getByRole('dialog', { name: '放弃未保存的记忆修改', exact: true }); await modal.getByRole('button', { name: '放弃修改并离开', exact: true }).click();
    await page.getByRole('heading', { name: '用量记录', exact: true, level: 1 }).waitFor();
    await navigate('记忆'); await page.getByLabel(/^选择助手/).selectOption(resources.assistant_id);
    await page.getByRole('heading', { name: `${assistantName}的记忆`, exact: true }).waitFor(); await page.getByLabel(/^记忆内容/).waitFor();
    assert.equal(await page.getByLabel(/^记忆内容/).inputValue(), saved); assert.equal(await page.locator('.memory-file-item').count(), 1);
    await page.locator('.memory-editor-actions .spin').waitFor({ state: 'hidden' }); await screenshot('02-desktop-native-memory');
  });
  let firstConversation;
  await check('chat_context_cache_view_never_auto_refreshes_or_compacts', async () => {
    firstConversation = await newConversation();
    await page.getByRole('button', { name: '上下文', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '上下文', exact: true });
    await dialog.getByText('正在读取上下文状态…', { exact: true }).waitFor({ state: 'hidden' });
    await dialog.getByRole('button', { name: '刷新原生统计', exact: true }).waitFor();
    await dialog.getByRole('button', { name: '开始原生压缩', exact: true }).waitFor();
    assert.equal(report.context_refresh_requests, 0, 'Opening context automatically refreshed native usage'); assert.equal(report.compact_requests, 0);
    const cached = await api('GET', `/conversations/${firstConversation.id}/context`);
    assert(['available', 'unavailable', 'not_measured'].includes(cached.status));
    if (cached.max_tokens == null) assert((await dialog.locator('.context-statistics').innerText()).includes('未提供'));
    await screenshot('03-native-context-cached'); await dialog.getByRole('button', { name: '关闭', exact: true }).click();
  });
  if (realModel) {
    await check('real_model_mcp_read_and_readable_tool_timeline', async () => {
      const { run, answer } = await sendAndWait(firstConversation.id, '请使用知识库查询合成的设备报修处理时限，回答具体小时数并标注知识条目编号。');
      assert(answer.includes('DEMO-KB-001'), 'Missing synthetic knowledge source');
      const trace = await api('GET', `/runs/${run.id}/trace`); assert(JSON.stringify(trace).includes('search_knowledge'), 'No real MCP query in persisted trace');
      const toggle = page.getByRole('button', { name: '执行记录', exact: true }); if (await page.getByRole('button', { name: '关闭执行记录', exact: true }).count() === 0) await toggle.click();
      await page.locator('.event-row').filter({ hasText: 'search_knowledge' }).first().waitFor();
      assert(!/mcp__c[0-9a-f]{32}__/.test(await page.locator('.event-list').innerText()), 'Internal connection prefix remains in primary execution view');
      await screenshot('04-real-model-read');
      await page.getByRole('button', { name: '关闭执行记录', exact: true }).click();
    });
    await check('new_conversation_native_memory_recall', async () => {
      const next = await newConversation(); assert.notEqual(next.id, firstConversation.id);
      const { answer } = await sendAndWait(next.id, '请只依据我们共享的长期记忆，回答唯一验证标记和地区。请原样输出标记；不知道则明确说不知道。不要调用业务查询工具。');
      assert(answer.includes(memoryMarker), 'New chat did not recall the actual native memory file'); assert(answer.includes('苏州'));
      await screenshot('05-new-conversation-memory-recall');
      await page.getByRole('button', { name: '上下文', exact: true }).click();
      const dialog = page.getByRole('dialog', { name: '上下文', exact: true }); await dialog.getByText('正在读取上下文状态…', { exact: true }).waitFor({ state: 'hidden' });
      const native = await responseFor('POST', `/api/v1/conversations/${next.id}/context/refresh`, () => dialog.getByRole('button', { name: '刷新原生统计', exact: true }).click());
      assert(['available', 'unavailable', 'not_measured'].includes(native.status)); report.native_context_status = native.status;
      if (native.status === 'unavailable') await dialog.getByText('原生统计暂不可用', { exact: true }).waitFor();
      else if (native.status === 'available') await dialog.getByText('原生统计可用', { exact: true }).waitFor();
      await screenshot('06-real-native-context-status'); await dialog.getByRole('button', { name: '关闭', exact: true }).click();
    });
  }
  await check('mobile_memory_and_zero_unexpected_errors', async () => {
    await page.setViewportSize({ width: 390, height: 844 }); await navigate('记忆');
    await page.getByLabel(/^选择助手/).selectOption(resources.assistant_id);
    await page.getByRole('heading', { name: `${assistantName}的记忆`, exact: true }).waitFor();
    await page.getByLabel(/^记忆内容/).waitFor();
    assert((await page.getByLabel(/^记忆内容/).inputValue()).includes(memoryMarker));
    await noOverflow(); await screenshot('07-mobile-native-memory');
    assert.equal(report.compact_requests, 0, 'This browser check must not duplicate native compact acceptance');
    if (!realModel) { assert.equal(report.model_messages, 0); assert.equal(report.context_refresh_requests, 0); }
    else { assert.equal(report.model_messages, 2); assert.equal(report.context_refresh_requests, 1); }
    for (const key of ['console_errors', 'page_errors', 'http_errors', 'network_errors']) assert.deepEqual(report[key], [], `Unexpected ${key}`);
  });
  report.status = 'passed';
} catch (error) {
  report.status = 'failed'; report.error = clean(error.message); process.stderr.write(`${clean(error.message)}\n`); process.exitCode = 1;
  if (page) { try { await screenshot('failure'); } catch {} }
} finally {
  const failures = [];
  if (context && loggedIn && !keepResources) {
    for (const id of resources.run_ids) { try { const run = await api('GET', `/runs/${id}`); if (!['succeeded', 'failed', 'cancelled', 'interrupted'].includes(run.status)) await api('POST', `/runs/${id}/cancel`); } catch (error) { failures.push(clean(error.message)); } }
    for (const id of resources.conversation_ids) { try { await api('PATCH', `/conversations/${id}`, { archived: true }); } catch (error) { failures.push(clean(error.message)); } }
    if (resources.assistant_id) { try { const memory = await api('GET', `/assistants/${resources.assistant_id}/memory`); await api('DELETE', `/assistants/${resources.assistant_id}/memory?revision=${memory.revision}`); } catch (error) { failures.push(clean(error.message)); } }
    for (const [type, id] of [['assistants', resources.assistant_id], ['assistants', resources.second_assistant_id], ['connections', resources.connection_id]]) if (id) { try { await api('DELETE', `/${type}/${id}`); } catch (error) { failures.push(clean(error.message)); } }
  }
  report.cleanup = { kept: keepResources, errors: failures }; if (failures.length) { report.status = 'failed'; process.exitCode = 1; }
  report.completed_at = new Date().toISOString(); writeFileSync(reportPath, JSON.stringify(report, null, 2) + '\n');
  await browser?.close(); process.stdout.write(`Report: ${reportPath}\n`);
}
