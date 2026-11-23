/** Real Chromium regression with fixture responses. Set BROWSER_EXECUTABLE and optionally PUPPETEER_MODULE. */
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { createServer } from 'vite';

const executablePath = process.env.BROWSER_EXECUTABLE;
if (!executablePath) throw new Error('Set BROWSER_EXECUTABLE to a local Chromium executable.');
const { default: puppeteer } = await import(process.env.PUPPETEER_MODULE || 'puppeteer-core');
const root = fileURLToPath(new URL('..', import.meta.url));
const at = '2026-11-22T10:00:00Z';
const longTitle = `订单统计 ${'UnbrokenLongTitle'.repeat(10)}`;
const table = `| ${Array.from({ length: 8 }, (_, i) => `字段 ${i + 1}`).join(' | ')} |\n${'| --- '.repeat(8)}|\n${'| 订单数据 '.repeat(8)}|`;
const output = `${table}\n\n- 核对付款\n\n![外部图片](https://example.invalid/pixel.gif) ![内部附件](/api/v1/files/test/content) [来源](/api/v1/sources/test)`;
const child = (status, display_title, fields = {}) => ({ child_id: 'shared-id', role: 'harness_analysis', status, display_title, ...fields });
const plan = (run, children) => ({ plan_id: run, children, counts: {} });
const latest = plan('latest', [child('succeeded', longTitle, { result: { output } }), child('failed', '核对库存', { child_id: 'failed', stop_reason: 'executor_error' })]);
const older = plan('older', [child('succeeded', '上一轮的结果', { result: { output: '上一轮正文' } })]);
const runs = [{ id: 'latest', status: 'succeeded', created_at: at }, { id: 'older', status: 'succeeded', created_at: '2026-11-21T10:00:00Z' }];
const fixture = `<!doctype html><html lang="zh-CN"><meta name="viewport" content="width=device-width, initial-scale=1"><div class="zhixing-theme" id="root"></div>
<script type="module">
import React from '/node_modules/.vite/deps/react.js';
import ReactDOM from '/node_modules/.vite/deps/react-dom_client.js';
import ConversationTasks from '/src/ConversationTasks.tsx';
import '/src/styles.css';
import '/src/workbench-theme.css';
import '/src/enterprise-theme.css';
import '/src/composer.css';
window.summaryDrafts = [];
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(ConversationTasks, {
  conversationId: 'fixture', runId: 'current', canDelegate: true, onClose: () => {},
  onSummarize: result => window.summaryDrafts.push(result)
}));
</script></html>`;

let browser;
let server;
let latestAvailable = true;
let deferredOldRead;
let current = plan('current', []);
let scheduled = [];
const operations = [];
const errors = [];

try {
  server = await createServer({ root, logLevel: 'error', server: { host: '127.0.0.1', port: 0 }, plugins: [{
    name: 'subagent-regression',
    configureServer(vite) {
      vite.middlewares.use('/subagent-test.html', async (_, response) => {
        response.setHeader('Content-Type', 'text/html; charset=utf-8');
        response.end(await vite.transformIndexHtml('/subagent-test.html', fixture));
      });
    },
  }] });
  await server.listen();
  browser = await puppeteer.launch({ executablePath, headless: true });
  const page = await browser.newPage();
  await page.setViewport({ width: 360, height: 720, isMobile: true, hasTouch: true });
  await page.setRequestInterception(true);
  page.on('pageerror', error => errors.push(error.message));
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  const reply = (request, body, status = 200) => request.respond({ status, contentType: 'application/json', body: JSON.stringify(body) }).catch(error => {
    if (!/Invalid InterceptionId|Target closed/i.test(error.message)) errors.push(error.message);
  });
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.protocol === 'data:') return;
    const method = request.method();
    const atPath = (path, verb = 'GET') => url.pathname === `/api/v1${path}` && method === verb;
    const body = () => JSON.parse(request.postData());
    if (atPath('/conversations/fixture/subagent-plans')) return void reply(request, runs);
    if (atPath('/conversations/fixture/schedules')) return void reply(request, scheduled);
    if (atPath('/conversations/fixture/schedules', 'POST')) {
      operations.push(['schedule', body()]);
      scheduled = [{ ...body(), id: 'schedule', status: 'scheduled' }];
      return void reply(request, scheduled[0], 201);
    }
    if (atPath('/conversations/fixture/schedules/schedule', 'DELETE')) {
      operations.push(['remove schedule']);
      scheduled = [];
      return void reply(request, {}, 200);
    }
    if (atPath('/runs/latest/trace')) return void reply(request, { run: { id: 'latest', approvals: [] }, events: [] });
    if (atPath('/runs/latest/subagents')) return void reply(request, latestAvailable ? latest : {}, latestAvailable ? 200 : 503);
    if (atPath('/runs/older/subagents')) {
      if (deferredOldRead) return void reply(request, older);
      deferredOldRead = () => reply(request, older);
      return;
    }
    if (atPath('/runs/current/subagents')) return void reply(request, current);
    if (atPath('/runs/current/subagents', 'POST')) {
      operations.push(['start', body()]);
      current = plan('current', [child('running', '本轮订单核查')]);
      return void reply(request, current, 202);
    }
    if (atPath('/runs/current/subagents/cancel', 'POST')) {
      operations.push(['cancel']);
      current = plan('current', [child('cancelled', '本轮订单核查', { stop_reason: 'parent_cancelled' })]);
      return void reply(request, plan('current', [child('cancelled', undefined, { stop_reason: 'parent_cancelled' })]));
    }
    if (url.origin !== origin || url.pathname.startsWith('/api/v1/')) {
      errors.push(`Unexpected request ${method} ${url.href}`);
      return void request.abort();
    }
    void request.continue();
  });

  const text = (selector, expected) => page.waitForFunction((selector, expected) =>
    document.querySelector(selector)?.textContent.includes(expected), { timeout: 5000 }, selector, expected);
  const click = label => page.locator(`button::-p-text(${label})`).setTimeout(5000).click();
  await page.goto(`${origin}/subagent-test.html`, { waitUntil: 'networkidle0' });
  const headings = await page.$$eval('.task-sidebar-section h3', nodes => nodes.map(node => node.textContent));
  assert.deepEqual(headings, ['定时任务', '子任务']);
  const history = '.task-sidebar-history';
  await page.click(history + ' > summary');
  const row = history + ' .task-sidebar-child-row';
  await text(row, longTitle);
  const disclosure = await page.$(row);
  await disclosure.focus();
  await page.keyboard.press('Enter');
  await page.waitForSelector('.task-sidebar-detail table');
  const result = await page.$('.subagent-progress-result');
  const accessible = await page.accessibility.snapshot({root: result, interestingOnly: false});
  assert.ok(accessible.name.includes('子任务结果'));
  const narrow = await page.evaluate(() => {
    const result = document.querySelector('.subagent-progress-result');
    return { resultScrolls: result.scrollWidth > result.clientWidth, fits: document.documentElement.scrollWidth <= innerWidth,
      visibleList: Boolean(result.querySelector('li')?.getBoundingClientRect().height),
      interactiveResources: result.querySelectorAll('a, img, .answer-sources').length };
  });
  assert.deepEqual(narrow, { resultScrolls: true, fits: true, visibleList: true, interactiveResources: 0 });
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => document.activeElement?.className === 'task-sidebar-child-row');

  latestAvailable = false;
  await page.click('[aria-label="刷新子任务"]');
  await text('.task-sidebar-read-error', '显示上次读取的记录');
  latestAvailable = true;
  await page.click('[aria-label="刷新子任务"]');
  await page.waitForSelector('.task-sidebar-read-error', { hidden: true });
  await click('用结果起草汇总');
  assert.deepEqual(await page.evaluate(() => window.summaryDrafts), [`${longTitle}\n${output}`]);

  const olderRequest = page.waitForRequest(request => new URL(request.url()).pathname === '/api/v1/runs/older/subagents');
  await page.select('select', 'older');
  await olderRequest;
  await page.select('select', 'latest');
  await text(row, longTitle);
  await deferredOldRead();
  assert.ok((await page.$eval(row, node => node.textContent)).includes(longTitle));
  await page.select('select', 'older');
  await text(row, '上一轮的结果');

  await click('分配子任务');
  await page.locator('.task-sidebar-section:nth-child(2) textarea').fill(' 核查订单\n\n 汇总客户 ');
  await click('启动子任务');
  await text('.task-sidebar-child-row', '本轮订单核查');
  await click('取消子任务');
  await text('.task-sidebar-child-row', '已取消');

  await click('新建');
  await page.locator('.task-sidebar-section:first-child textarea').fill('回访订单');
  const tomorrow = new Date(Date.now()+86400_000).toISOString().slice(0,16);
  await page.$eval('input[type="datetime-local"]', (input, when) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, when);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }, tomorrow);
  await click('设定时间');
  await text('.task-sidebar-schedule-row', '回访订单');
  await page.click('[aria-label="取消定时任务 回访订单"]');
  await page.waitForSelector('.task-sidebar-schedule-row', { hidden: true });
  assert.deepEqual(operations, [
    ['start', { mode: 'harness', tasks: ['核查订单', '汇总客户'] }], ['cancel'],
    ['schedule', { text: '回访订单', scheduled_at: new Date(tomorrow).toISOString() }], ['remove schedule'],
  ]);
  assert.deepEqual(errors, []);
  console.log('PASS subagent sidebar results, mobile layout, keyboard and accessibility, stale retry, run isolation, and existing actions');
} finally {
  await browser?.close();
  await server?.close();
}
