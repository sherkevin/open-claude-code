/** Actual Kaiwu web conversation through the Huawi enterprise MCP bridge.
 *
 * This script drives the built product UI, imports the issued standard MCP
 * JSON, discovers the filtered tool, creates an assistant and sends one real
 * model-backed chat message. It never prints or persists the integration K.
 */
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const option = (name, fallback = '') => {
  const index = args.indexOf(name);
  if (index < 0) return fallback;
  assert(args[index + 1] && !args[index + 1].startsWith('--'), `Missing ${name} value`);
  return args[index + 1];
};

if (args.includes('--help')) {
  process.stdout.write('Usage: node scripts/kaiwu_chat_browser.mjs --password-file FILE --mcp-config-file FILE --report FILE --screenshots DIR [--base-url URL]\nEnvironment: PLAYWRIGHT_CORE_PATH, BROWSER_EXECUTABLE, BROWSER_RUN_TIMEOUT_MS, BROWSER_HEADLESS=false.\n');
  process.exit(0);
}

const baseURL = option('--base-url', process.env.KAIWU_BROWSER_BASE_URL || 'http://127.0.0.1:5173').replace(/\/$/, '');
const origin = new URL(baseURL);
assert(origin.protocol === 'http:' || origin.protocol === 'https:', 'Browser URL must use HTTP or HTTPS');
assert(!origin.username && !origin.password && !origin.search && !origin.hash, 'Browser URL must not contain credentials, query or fragment');
const passwordFile = option('--password-file');
const configFile = option('--mcp-config-file');
const reportPath = path.resolve(option('--report', 'docs/evidence/huawi-mcp-20261118-enterprise-bridge/browser-chat-latest.json'));
const screenshotRoot = path.resolve(option('--screenshots', 'docs/evidence/huawi-mcp-20261118-enterprise-bridge/screenshots'));
const runTimeout = Number(process.env.BROWSER_RUN_TIMEOUT_MS || 300000);
const modelID = option('--model-id', 'dano-v2.1');
const modelLabel = option('--model-label', 'Dano-v2.1');
const username = option('--username', 'browser-admin');

assert(passwordFile && existsSync(passwordFile), 'A private administrator password file is required');
assert(configFile && existsSync(configFile), 'The issued MCP configuration file is required');
const password = readFileSync(passwordFile, 'utf8').trim();
assert(password, 'Administrator password is empty');
const mcpConfig = JSON.parse(readFileSync(configFile, 'utf8'));
assert(mcpConfig && typeof mcpConfig === 'object' && mcpConfig.mcpServers && typeof mcpConfig.mcpServers === 'object', 'MCP configuration must contain mcpServers');
const servers = Object.entries(mcpConfig.mcpServers);
assert.equal(servers.length, 1, 'This acceptance requires exactly one issued MCP server');
const [connectionName, connectionConfig] = servers[0];
const authorization = connectionConfig?.headers?.Authorization || connectionConfig?.headers?.authorization || connectionConfig?.authorization || '';
assert.equal(typeof authorization, 'string', 'The MCP server authorization is missing');
const accessToken = authorization.replace(/^Bearer\s+/i, '');
assert(accessToken, 'The MCP server authorization is empty');
const secrets = [password, accessToken];
const redactText = value => {
  let text = String(value);
  for (const secret of secrets.filter(Boolean)) text = text.split(secret).join('[redacted]');
  return text.replace(/Bearer\s+[A-Za-z0-9._~+\/-]+/gi, 'Bearer [redacted]');
};
const redact = value => redactText(value).slice(0, 4000);
const cleanJSON = value => JSON.parse(redactText(JSON.stringify(value)));
const pathnameOf = (url, fallback = '') => {
  if (!url && !fallback) return '';
  try { return new URL(url || fallback, baseURL).pathname; }
  catch { return ''; }
};

const tag = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
const assistantName = `华微订单查询验收助手 ${tag}`;
const prompt = '请必须实际调用 lookup_order 工具查询 SKU-1001 的订单状态，不要猜测。工具返回后，请原样输出订单状态。';
const syntheticOrder = 'SYNTHETIC-ORDER-42';
const resources = { connection_id: '', assistant_id: '', conversation_id: '', run_id: '' };
const report = {
  status: 'running',
  started_at: new Date().toISOString(),
  base_url: baseURL,
  browser_mode: 'actual_product_ui',
  connection_name: connectionName,
  model_id: modelID,
  model_label: modelLabel,
  checks: [],
  screenshots: [],
  model_messages: 0,
  console_errors: [],
  page_errors: [],
  http_errors: [],
  network_errors: [],
  resources,
  limitations: [
    'Business MCP and order data are deterministic synthetic fixtures.',
    'The model is reached through the school-cloud loopback provider adapter, not a production Tencent Cloud TokenHub deployment.',
  ],
};

mkdirSync(path.dirname(reportPath), { recursive: true });
mkdirSync(screenshotRoot, { recursive: true });

let browser;
let context;
let page;
let loggedIn = false;

async function loadPlaywright() {
  const require = createRequire(import.meta.url);
  const candidates = [
    process.env.PLAYWRIGHT_CORE_PATH,
    '/media/data3/dengkw/t3-code/bin/t3-0.1.9-linux-x64/node_modules/playwright-core',
  ].filter(Boolean);
  for (const candidate of candidates) {
    try { return require(candidate); }
    catch (error) { if (candidate === candidates.at(-1)) throw error; }
  }
  throw new Error('Set PLAYWRIGHT_CORE_PATH to a Playwright core module');
}

function executable() {
  const candidates = [
    process.env.BROWSER_EXECUTABLE,
    '/media/data3/dengkw/tmp-archive-20261104/pw-browsers/chromium-1248/chrome-linux64/chrome',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
  ].filter(Boolean);
  const found = candidates.find(candidate => existsSync(candidate));
  assert(found, 'No Chromium executable found; set BROWSER_EXECUTABLE');
  return found;
}

function observe(target) {
  target.on('request', request => {
    if (request.method() !== 'POST') return;
    const pathname = new URL(request.url()).pathname;
    if (/\/conversations\/[^/]+\/messages$/.test(pathname)) report.model_messages += 1;
  });
  target.on('console', message => {
    if (message.type() !== 'error') return;
    const pathname = pathnameOf(message.location().url, baseURL);
    if (!loggedIn && /\/api\/v1\/auth\/me$/.test(pathname) && /401/.test(message.text())) return;
    report.console_errors.push(redact(message.text()));
  });
  target.on('pageerror', error => report.page_errors.push(redact(error.message)));
  target.on('response', response => {
    const pathname = pathnameOf(response.url());
    if (response.status() < 400) return;
    if (!pathname.startsWith('/api/')) return;
    if (!loggedIn && pathname === '/api/v1/auth/me' && response.status() === 401) return;
    report.http_errors.push({ path: pathname, status: response.status() });
  });
  target.on('requestfailed', request => {
    const pathname = pathnameOf(request.url());
    if (!pathname.startsWith('/api/')) return;
    const reason = request.failure()?.errorText || '';
    if (request.method() === 'GET' && /^net::ERR_ABORTED/.test(reason)) return;
    if (/\/runs\/[^/]+\/events$/.test(pathname) && /ABORTED|CANCELLED/i.test(reason)) return;
    report.network_errors.push({ path: pathname, reason: redact(reason) });
  });
}

async function check(name, work) {
  const started = Date.now();
  try {
    await work();
    report.checks.push({ name, status: 'passed', seconds: Number(((Date.now() - started) / 1000).toFixed(3)) });
    process.stdout.write(`PASS ${name}\n`);
  } catch (error) {
    report.checks.push({ name, status: 'failed', error: redact(error.message) });
    throw error;
  }
}

async function responseFor(method, pathname, work, timeout = 60000) {
  const wait = page.waitForResponse(response => response.request().method() === method && new URL(response.url()).pathname === pathname, { timeout });
  await work();
  const response = await wait;
  assert(response.ok(), `${method} ${pathname} returned ${response.status()}`);
  return response.status() === 204 ? null : response.json();
}

async function api(method, endpoint, data) {
  const response = await context.request.fetch(`${baseURL}/api/v1${endpoint}`, {
    method,
    data,
    headers: { Origin: origin.origin },
  });
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

async function screenshot(name) {
  await page.evaluate(async () => { await document.fonts.ready; });
  const target = path.join(screenshotRoot, `${name}.png`);
  await page.screenshot({
    path: target,
    fullPage: true,
    animations: 'disabled',
    mask: [page.locator('input[type=password]'), page.locator('textarea.config-input')],
  });
  report.screenshots.push({ name, path: target });
  return target;
}

async function screenshotElement(name, locator) {
  await page.evaluate(async () => { await document.fonts.ready; });
  const target = path.join(screenshotRoot, `${name}.png`);
  await locator.screenshot({ path: target, animations: 'disabled' });
  report.screenshots.push({ name, path: target });
  return target;
}

function completedToolCall(trace) {
  assert(Array.isArray(trace?.events), 'Persisted run trace has no event list');
  const started = trace.events.filter(event => event?.type === 'tool.started');
  const completed = trace.events.filter(event => event?.type === 'tool.completed');
  assert.equal(started.length, 1, `Expected exactly one tool.started event, got ${started.length}`);
  assert.equal(completed.length, 1, `Expected exactly one tool.completed event, got ${completed.length}`);
  const start = started[0];
  const finish = completed[0];
  const startedName = String(start.data?.tool_name || '');
  const completedName = String(finish.data?.tool_name || '');
  assert.match(startedName, /(?:^|__)lookup_order$/, `Unexpected started tool: ${startedName}`);
  assert.equal(completedName, startedName, 'Started and completed tool names differ');
  assert.deepEqual(start.data?.arguments, { sku: 'SKU-1001' }, 'Tool arguments do not match the requested synthetic SKU');
  assert(start.data?.tool_call_id, 'tool.started has no tool_call_id');
  assert.equal(finish.data?.tool_call_id, start.data.tool_call_id, 'tool.completed does not match tool.started');
  assert.equal(finish.data?.success, true, 'tool.completed did not report success');
  assert(!completedName.endsWith('__write_order'), 'A forbidden write tool was executed');
  return {
    tool_name: completedName,
    tool_call_id: String(start.data.tool_call_id),
    arguments: start.data.arguments,
    started_event_id: start.id,
    completed_event_id: finish.id,
    success: finish.data.success,
    duration_ms: finish.data.duration_ms,
  };
}

try {
  const { chromium } = await loadPlaywright();
  const binary = executable();
  browser = await chromium.launch({
    headless: process.env.BROWSER_HEADLESS !== 'false',
    executablePath: binary,
    args: process.env.BROWSER_NO_SANDBOX === 'true' ? ['--no-sandbox'] : [],
  });
  context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    reducedMotion: 'reduce',
  });
  context.setDefaultTimeout(30000);
  page = await context.newPage();
  observe(page);

  await check('login_to_actual_product', async () => {
    await page.goto(baseURL, { waitUntil: 'domcontentloaded' });
    await page.getByLabel(/^账号/).fill(username);
    await page.getByLabel(/^密码/).fill(password);
    const user = await responseFor('POST', '/api/v1/auth/login', () => page.getByRole('button', { name: '登录', exact: true }).click());
    assert.equal(user.role, 'admin');
    loggedIn = true;
    await page.getByRole('navigation', { name: '主导航' }).waitFor();
  });

  await check('import_standard_mcp_json_without_secret_leak', async () => {
    await navigate('业务连接');
    await page.getByRole('button', { name: '导入配置', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '导入 MCP 配置', exact: true });
    await dialog.getByLabel(/^MCP 配置 JSON/).fill(JSON.stringify(mcpConfig));
    const preview = await responseFor('POST', '/api/v1/connections/import/preview', () => dialog.getByRole('button', { name: '预览配置', exact: true }).click());
    assert.equal(preview.connections.length, 1);
    assert.equal(preview.connections[0].credential_present, true);
    assert(!JSON.stringify(preview).includes(accessToken), 'Preview response leaked the integration credential');
    assert(!(await dialog.innerText()).includes(accessToken), 'Preview DOM leaked the integration credential');
    await screenshot('01-mcp-import-masked-preview');
    const saved = await responseFor('POST', '/api/v1/connections/import', () => dialog.getByRole('button', { name: '确认导入 1 个连接', exact: true }).click());
    assert.equal(saved.binding_required, true);
    resources.connection_id = saved.connections[0].id;
    assert(!JSON.stringify(saved).includes(accessToken), 'Import response leaked the integration credential');
    const storage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
    assert(!storage.includes(accessToken), 'Integration credential was persisted in browser storage');
    await dialog.waitFor({ state: 'hidden' });
  });

  await check('discover_filtered_tool_and_bind_assistant', async () => {
    const card = page.getByRole('article').filter({ has: page.getByRole('heading', { name: connectionName, exact: true }) });
    const tested = await responseFor('POST', `/api/v1/connections/${resources.connection_id}/test`, () => card.getByRole('button', { name: '测试连接', exact: true }).click());
    assert.equal(tested.status, 'connected');
    const names = tested.tools.map(tool => tool.name).sort();
    assert.deepEqual(names, ['lookup_order']);
    report.discovered_tools = names;
    await screenshot('02-mcp-tool-discovery');
    await page.getByRole('button', { name: '配置助手', exact: true }).click();
    await page.getByRole('heading', { name: '助手设置', exact: true, level: 1 }).waitFor();
    await page.getByRole('button', { name: '新建助手', exact: true }).first().click();
    const dialog = page.getByRole('dialog', { name: '新建助手', exact: true });
    await dialog.getByLabel(/^助手名称/).fill(assistantName);
    await dialog.getByLabel(/^助手说明/).fill('华微企业统一入口网页对话验收专用。');
    await dialog.getByLabel(/^业务规则/).fill('必须实际调用 lookup_order 查询合成订单，原样返回工具状态，不得猜测或伪造。只允许读取，不执行写入工具。');
    const binding = dialog.locator('.binding-group').filter({ hasText: connectionName });
    await binding.locator('.binding-heading input[type=checkbox]').check();
    await binding.locator('details.binding-permissions summary').click();
    const row = binding.locator('.tool-selection-row').filter({ hasText: 'lookup_order' }).first();
    await row.locator('.checkbox-row input[type=checkbox]').check();
    const automatic = row.getByRole('checkbox', { name: `${connectionName} · lookup_order 免确认执行`, exact: true });
    assert.equal(await automatic.isChecked(), false);
    await automatic.check();
    const saved = await responseFor('POST', '/api/v1/assistants', () => dialog.locator('footer').getByRole('button', { name: '保存助手', exact: true }).click());
    resources.assistant_id = saved.id;
    assert.deepEqual(saved.connections[0].allowed_tools, ['lookup_order']);
    assert.deepEqual(saved.connections[0].auto_approved_tools, ['lookup_order']);
    await dialog.waitFor({ state: 'hidden' });
    await page.getByRole('heading', { name: assistantName, exact: true }).waitFor();
  });

  await check('create_web_conversation_with_explicit_model', async () => {
    await page.getByRole('button', { name: '新建会话', exact: true }).first().click();
    const dialog = page.getByRole('dialog', { name: '新建会话', exact: true });
    await dialog.getByLabel(/^使用助手/).selectOption(resources.assistant_id);
    await dialog.getByLabel('选模方式').selectOption('manual');
    const selector = dialog.getByRole('button', { name: '选择模型', exact: true });
    await selector.click();
    const listbox = dialog.getByRole('listbox', { name: '可用模型', exact: true });
    await listbox.getByRole('option').filter({ hasText: modelLabel }).click();
    assert((await selector.innerText()).includes(modelLabel), 'Explicit model was not selected');
    const conversation = await responseFor('POST', '/api/v1/conversations', () => dialog.getByRole('button', { name: '开始会话', exact: true }).click());
    resources.conversation_id = conversation.id;
    report.conversation_model_id = conversation.model_id;
    report.conversation_model_label = conversation.model_label;
    report.conversation_runtime = conversation.runtime_type;
    assert.equal(conversation.model_id, modelID);
    assert.equal(conversation.runtime_type, 'harness');
    await dialog.waitFor({ state: 'hidden' });
    await page.getByRole('textbox', { name: '输入消息', exact: true }).waitFor();
  });

  await check('real_model_calls_mcp_and_returns_fixture', async () => {
    await page.getByRole('textbox', { name: '输入消息', exact: true }).fill(prompt);
    const sent = await responseFor('POST', `/api/v1/conversations/${resources.conversation_id}/messages`, () => page.getByRole('button', { name: '发送消息', exact: true }).click());
    resources.run_id = sent.run_id;
    const deadline = Date.now() + runTimeout;
    let run;
    while (Date.now() < deadline) {
      run = await api('GET', `/runs/${resources.run_id}`);
      if (['succeeded', 'failed', 'cancelled', 'interrupted'].includes(run.status)) break;
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    assert.equal(run?.status, 'succeeded', `Run ended as ${run?.status || 'timeout'} ${run?.error_code || ''} ${run?.error || ''}`);
    const messages = await api('GET', `/conversations/${resources.conversation_id}/messages`);
    const answer = messages.filter(message => message.role === 'assistant' && message.run_id === resources.run_id).map(message => message.text).join('\n');
    assert(answer.includes(syntheticOrder), 'Final assistant answer did not include the MCP fixture result');
    const trace = await api('GET', `/runs/${resources.run_id}/trace`);
    const toolCall = completedToolCall(trace);
    report.chat = {
      prompt,
      answer_text: redact(answer),
      answer_contains_fixture: answer.includes(syntheticOrder),
      answer_length: answer.length,
      run_status: run.status,
      run_model_id: run.model_id,
      run_model_label: run.model_label,
      usage: run.usage,
      trace_tool_names: [toolCall.tool_name],
      tool_call_events: [toolCall],
    };
    assert.equal(run.model_id, modelID);
    assert(run.execution_kind === 'model', 'Run was not recorded as a model execution');
    assert(run.usage?.input_tokens > 0 && run.usage?.output_tokens > 0, 'Model usage receipt is missing');
    await page.getByRole('button', { name: '停止', exact: true }).waitFor({ state: 'hidden' });
    await page.locator('.message.assistant .markdown').last().waitFor();
    await screenshot('03-web-chat-answer');
    const traceButton = page.getByRole('button', { name: '执行记录', exact: true });
    if (await traceButton.count()) {
      if (await page.getByRole('button', { name: '关闭执行记录', exact: true }).count() === 0) await traceButton.click();
      const eventRows = page.locator('.event-row').filter({ hasText: 'lookup_order' });
      await eventRows.first().waitFor();
      assert((await eventRows.count()) >= 2, 'Execution panel did not render both tool start and completion events');
      await screenshot('04-web-chat-tool-trace');
      await eventRows.last().scrollIntoViewIfNeeded();
      await screenshotElement('05-web-chat-tool-events', page.locator('.execution-panel'));
      await page.getByRole('button', { name: '关闭执行记录', exact: true }).click();
    }
  });

  await check('no_unexpected_browser_errors', async () => {
    assert.equal(report.model_messages, 1, 'Expected exactly one model-backed message request');
    for (const key of ['console_errors', 'page_errors', 'http_errors', 'network_errors']) {
      assert.deepEqual(report[key], [], `Unexpected ${key}`);
    }
  });
  report.status = 'passed';
} catch (error) {
  report.status = 'failed';
  report.error = redact(error.message);
  process.stderr.write(`${report.error}\n`);
  process.exitCode = 1;
  if (page) {
    try { await screenshot('failure'); } catch {}
  }
} finally {
  report.completed_at = new Date().toISOString();
  writeFileSync(reportPath, `${JSON.stringify(cleanJSON(report), null, 2)}\n`);
  await browser?.close();
  process.stdout.write(`Report: ${reportPath}\n`);
}
