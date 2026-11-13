import { appendFile, mkdir, writeFile, stat } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const mode = process.env.BROWSER_MODE;
const previewUrl = process.env.BROWSER_PREVIEW_URL;
const previewPort = Number(process.env.BROWSER_PREVIEW_PORT);
const outputDir = process.env.BROWSER_OUTPUT_DIR;
const reportPath = process.env.BROWSER_REPORT_PATH;
const username = process.env.BROWSER_USERNAME;
const password = process.env.BROWSER_PASSWORD;
const diagnosticPath = path.join(outputDir || '.', `browser-${mode || 'unknown'}.diag.log`);

async function diagnostic(message) {
  await appendFile(diagnosticPath, `${new Date().toISOString()} ${message}\n`).catch(() => null);
}

await diagnostic('module loaded');

const report = {
  mode,
  status: 'failed',
  checks: [],
  screenshots: [],
  console_errors: [],
  page_errors: [],
  request_failures: [],
  http_errors: [],
  issues: [],
};

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function check(name, operation) {
  const started = Date.now();
  try {
    const detail = await operation();
    report.checks.push({ name, status: 'passed', duration_ms: Date.now() - started, detail: detail ?? null });
    return detail;
  } catch (error) {
    report.checks.push({
      name,
      status: 'failed',
      duration_ms: Date.now() - started,
      error: String(error?.stack || error).slice(0, 2000),
    });
    throw error;
  }
}

async function screenshot(page, name, fullPage = true) {
  const target = path.join(outputDir, `${name}.png`);
  await page.screenshot({ path: target, fullPage });
  report.screenshots.push(target);
  return target;
}

async function waitRun(page, runId, allowed, timeout = 30000) {
  const started = Date.now();
  let last = null;
  while (Date.now() - started < timeout) {
    const result = await page.evaluate(async (id) => {
      const response = await fetch(`/api/v1/runs/${encodeURIComponent(id)}`, { credentials: 'include' });
      return { status: response.status, body: await response.json() };
    }, runId);
    last = result;
    if (result.status === 200 && allowed.includes(result.body.status)) return result.body;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`run ${runId} did not reach ${allowed.join('/')} within ${timeout}ms: ${JSON.stringify(last)}`);
}

async function submit(page, text, via = 'enter') {
  const textarea = page.getByLabel('输入消息');
  await textarea.waitFor({ state: 'visible' });
  await textarea.fill(text);
  await page.waitForFunction(() => {
    const button = document.querySelector('button[aria-label="发送消息"]');
    return button && !button.disabled;
  }, null, { timeout: 15000 });
  const responsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === 'POST' && /\/api\/v1\/conversations\/[^/]+\/messages$/.test(url.pathname);
  }, { timeout: 15000 });
  if (via === 'enter') await textarea.press('Enter');
  else await page.getByRole('button', { name: '发送消息' }).click();
  const response = await responsePromise;
  assert(response.status() === 202, `message submission returned HTTP ${response.status()}`);
  const payload = await response.json();
  assert(payload.run_id, 'submission did not return a run_id');
  return payload.run_id;
}

async function login(page) {
  await page.goto(previewUrl, { waitUntil: 'networkidle' });
  await page.getByRole('heading', { name: '登录你的工作台' }).waitFor();
  await page.locator('input[name="username"]').fill(username);
  await page.locator('input[name="password"]').fill(password);
  await page.getByRole('button', { name: /^登录/ }).click();
  await page.getByRole('button', { name: /新建会话/ }).waitFor({ timeout: 15000 });
}

async function startConversation(page) {
  const existingDialog = page.getByRole('dialog', { name: '新建会话' });
  if (!(await existingDialog.isVisible().catch(() => false))) {
    const card = page.locator('.assistant-card').filter({ hasText: '华微浏览器合成助手' });
    if (await card.count()) await card.click();
    else await page.getByRole('button', { name: /新建会话/ }).click();
  }
  const dialog = page.getByRole('dialog', { name: '新建会话' });
  await dialog.waitFor();
  const select = dialog.locator('select').first();
  const optionValue = await select.locator('option').filter({ hasText: '华微浏览器合成助手' }).getAttribute('value');
  assert(optionValue, 'synthetic assistant option is missing');
  if (await select.inputValue() !== optionValue) await select.selectOption(optionValue);
  const start = dialog.getByRole('button', { name: /开始会话/ });
  await start.waitFor({ state: 'visible' });
  try {
    await page.waitForFunction(() => {
      const dialog = [...document.querySelectorAll('[role="dialog"]')]
        .find(item => item.textContent?.includes('新建会话'));
      const button = dialog && [...dialog.querySelectorAll('button')]
        .find(item => item.textContent?.includes('开始会话'));
      return button && !button.disabled;
    }, null, { timeout: 15000 });
  } catch (error) {
    const state = await page.evaluate(async () => {
      const dialog = [...document.querySelectorAll('[role="dialog"]')]
        .find(item => item.textContent?.includes('新建会话'));
      const button = dialog && [...dialog.querySelectorAll('button')]
        .find(item => item.textContent?.includes('开始会话'));
      const selects = dialog ? [...dialog.querySelectorAll('select')].map(select => ({
        value: select.value,
        disabled: select.disabled,
        options: [...select.options].map(option => ({
          value: option.value,
          text: option.textContent,
          disabled: option.disabled,
        })),
      })) : [];
      const [config, models, assistants] = await Promise.all([
        fetch('/api/v1/config', { credentials: 'include' }).then(response => response.json()),
        fetch('/api/v1/models', { credentials: 'include' }).then(response => response.json()),
        fetch('/api/v1/assistants', { credentials: 'include' }).then(response => response.json()),
      ]);
      return {
        dialog_text: dialog?.textContent?.slice(0, 2000) || null,
        button: button ? { disabled: button.disabled, text: button.textContent } : null,
        selects,
        config,
        models,
        assistants: assistants.map(item => ({
          id: item.id,
          enabled: item.enabled,
          default_model_id: item.default_model_id,
          default_reasoning_level: item.default_reasoning_level,
        })),
      };
    });
    throw new Error(`start conversation never became ready: ${JSON.stringify(state).slice(0, 6000)}`, { cause: error });
  }
  await start.click();
  await page.getByLabel('输入消息').waitFor({ timeout: 15000 });
}

async function verifyTrial(page) {
  await check('trial_login_and_preloaded_assistant', async () => {
    await login(page);
    assert(await page.locator('.assistant-card').filter({ hasText: '华微浏览器合成助手' }).count() === 1, 'preloaded assistant is not visible');
    assert(await page.locator('.nav-item').filter({ hasText: '助手设置' }).count() === 0, 'trial unexpectedly exposes assistant administration');
    assert(await page.locator('.nav-item').filter({ hasText: '待审批操作' }).count() === 0, 'trial unexpectedly exposes approvals');
    return { assistant_cards: await page.locator('.assistant-card').count() };
  });
  await screenshot(page, 'trial-01-workspace-desktop');

  await check('trial_start_preloaded_conversation', () => startConversation(page));
  assert(await page.locator('.attach-button').filter({ hasText: '添加附件' }).count() === 0, 'trial unexpectedly exposes uploads');

  await check('trial_enter_submission_reaches_server', async () => {
    const runId = await submit(page, '浏览器回车提交');
    await page.getByText('浏览器回车提交', { exact: true }).waitFor();
    const run = await waitRun(page, runId, ['succeeded']);
    const messages = await page.evaluate(async () => {
      const conversations = await (await fetch('/api/v1/conversations', { credentials: 'include' })).json();
      const response = await fetch(`/api/v1/conversations/${conversations[0].id}/messages`, { credentials: 'include' });
      return response.json();
    });
    assert(messages.some(item => item.role === 'user' && item.text === '浏览器回车提交'), 'Enter submission was not persisted by the server');
    await page.getByText(/浏览器合成回复：浏览器回车提交/).waitFor({ timeout: 15000 });
    return { run_id: run.id, status: run.status };
  });
  await screenshot(page, 'trial-02-enter-submission');

  await check('trial_send_button_submission', async () => {
    const runId = await submit(page, '浏览器按钮提交', 'button');
    await waitRun(page, runId, ['succeeded']);
    await page.getByText(/浏览器合成回复：浏览器按钮提交/).waitFor({ timeout: 15000 });
    return { run_id: runId };
  });

  await check('trial_stream_is_incremental', async () => {
    await page.evaluate(() => {
      globalThis.__ocStreamSamples = [];
      const sample = () => {
        if (!document.querySelector('.typing-indicator')) return;
        const values = [...document.querySelectorAll('.message.assistant .markdown')];
        globalThis.__ocStreamSamples.push((values.at(-1)?.textContent || '').length);
      };
      globalThis.__ocStreamObserver = new MutationObserver(sample);
      globalThis.__ocStreamObserver.observe(document.body, {
        childList: true,
        characterData: true,
        subtree: true,
      });
    });
    const runId = await submit(page, '浏览器流式');
    await page.locator('.typing-indicator').waitFor({ timeout: 10000 });
    await waitRun(page, runId, ['succeeded']);
    const samples = await page.evaluate(() => {
      globalThis.__ocStreamObserver?.disconnect();
      return globalThis.__ocStreamSamples;
    });
    assert(new Set(samples).size >= 2, `streaming answer did not grow between DOM samples: ${samples}`);
    return { stream_samples: samples, run_id: runId };
  });

  await check('trial_cancel_and_terminal_state', async () => {
    const runId = await submit(page, '浏览器取消');
    await page.getByRole('button', { name: '停止' }).waitFor({ timeout: 10000 });
    await page.getByRole('button', { name: '停止' }).click();
    const run = await waitRun(page, runId, ['cancelled']);
    await page.getByText('已停止', { exact: false }).waitFor({ timeout: 15000 });
    return { run_id: run.id, status: run.status };
  });
  await screenshot(page, 'trial-03-cancel');

  await check('trial_knowledge_evidence_card', async () => {
    const runId = await submit(page, '浏览器知识证据');
    await waitRun(page, runId, ['succeeded']);
    const source = page.locator('.answer-sources .source-card').last();
    await source.waitFor({ timeout: 15000 });
    await source.click();
    const dialog = page.getByRole('dialog');
    await dialog.waitFor();
    await dialog.getByText(/引用时的内容/).waitFor();
    const excerpt = await dialog.locator('.source-excerpt').textContent();
    assert(excerpt?.includes('P-101'), 'evidence dialog did not show the synthetic knowledge excerpt');
    return { run_id: runId, source_cards: await page.locator('.answer-sources .source-card').count(), excerpt: excerpt?.slice(0, 120) };
  });
  await screenshot(page, 'trial-04-evidence-card');
  await page.getByRole('dialog').getByRole('button', { name: '关闭窗口' }).click();

  await check('trial_knowledge_search_and_document_access', async () => {
    await page.locator('.nav-item').filter({ hasText: '知识库' }).click();
    const card = page.locator('.resource-card').filter({ hasText: '华微浏览器合成资料' });
    await card.waitFor();
    await card.getByRole('button', { name: /查看资料/ }).click();
    const modes = await page.getByLabel('检索方式').locator('option').evaluateAll(items => items.map(item => item.value));
    assert(JSON.stringify(modes) === JSON.stringify(['bm25', 'hybrid']) || JSON.stringify(modes) === JSON.stringify(['hybrid', 'bm25']), `unexpected trial retrieval modes: ${modes}`);
    await page.getByLabel('检索知识库').fill('P-101 维护周期');
    await page.getByRole('button', { name: /^检索$/ }).click();
    await page.locator('.knowledge-hit').first().waitFor({ timeout: 10000 });
    const text = await page.locator('.knowledge-hit').first().textContent();
    assert(text?.includes('P-101'), 'knowledge search result did not include P-101');
    const link = page.locator('.knowledge-hit-title a').filter({ hasText: 'synthetic-browser-manual.md' }).first();
    const href = await link.getAttribute('href');
    assert(href, 'knowledge document link is missing');
    const content = await page.evaluate(async (url) => {
      const response = await fetch(url, { credentials: 'include' });
      return { status: response.status, text: await response.text() };
    }, href);
    assert(content.status === 200 && content.text.includes('P-101'), 'knowledge document content was not accessible');
    return { modes, hits: await page.locator('.knowledge-hit').count(), document_status: content.status };
  });

  await check('trial_image_lightbox', async () => {
    const imageButton = page.locator('.knowledge-thumbnail-button').first();
    await imageButton.waitFor();
    await imageButton.click();
    const dialog = page.getByRole('dialog');
    await dialog.waitFor();
    const image = dialog.locator('img.graph-preview-image');
    await image.waitFor();
    await page.waitForFunction(() => {
      const image = document.querySelector('dialog img.graph-preview-image, .modal img.graph-preview-image');
      return image && image.complete && image.naturalWidth > 0;
    }, null, { timeout: 10000 });
    const natural = await image.evaluate(node => ({ width: node.naturalWidth, height: node.naturalHeight, src: node.currentSrc }));
    assert(natural.width > 0 && natural.height > 0, 'image lightbox did not render the image');
    return natural;
  });
  await screenshot(page, 'trial-05-image-lightbox');
  await page.getByRole('dialog').getByRole('button', { name: '关闭窗口' }).click();

  await check('trial_production_static_assets', async () => {
    const scripts = await page.locator('script[src]').evaluateAll(items => items.map(item => item.src));
    assert(scripts.some(value => /\/assets\/index-[^/]+\.js$/.test(value)), `production client bundle is missing: ${scripts}`);
    assert(!scripts.some(value => value.includes('/@vite/client')), 'development Vite client was served');
    const performance = await page.evaluate(() => ({
      dcl: performance.timing.domContentLoadedEventEnd - performance.timing.navigationStart,
      load: performance.timing.loadEventEnd - performance.timing.navigationStart,
    }));
    return { scripts, performance };
  });

  await check('trial_narrow_viewport_submission', async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload({ waitUntil: 'networkidle' });
    await page.getByLabel('打开导航').waitFor();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    assert(overflow <= 2, `narrow viewport has ${overflow}px horizontal overflow`);
    await page.getByLabel('打开导航').click();
    const history = page.locator('.history-item').first();
    await history.waitFor();
    await history.click();
    const runId = await submit(page, '窄屏按钮提交', 'button');
    await waitRun(page, runId, ['succeeded']);
    await page.getByText(/浏览器合成回复：窄屏按钮提交/).waitFor({ timeout: 15000 });
    return { overflow, run_id: runId, viewport: { width: 390, height: 844 } };
  });
  await screenshot(page, 'trial-06-narrow-chat');
}

async function verifyNormal(page) {
  await check('normal_login_and_start_conversation', async () => {
    await login(page);
    await page.getByRole('button', { name: /新建会话/ }).click();
    await startConversation(page);
  });

  await check('normal_human_approval', async () => {
    const runId = await submit(page, '浏览器审批');
    const card = page.locator('.approval-card').filter({ hasText: '请确认这项操作' });
    await card.waitFor({ timeout: 15000 });
    const argumentsText = await card.locator('pre').textContent();
    assert(argumentsText?.includes('browser-approval.txt'), 'approval card did not show immutable arguments');
    await card.getByRole('button', { name: '允许执行' }).click();
    const run = await waitRun(page, runId, ['succeeded']);
    await page.getByText(/审批完成：合成写入已允许/).waitFor({ timeout: 15000 });
    return { run_id: run.id, status: run.status, arguments: argumentsText };
  });
  await screenshot(page, 'normal-01-approval');

  await check('normal_excel_artifact_download', async () => {
    const runId = await submit(page, '浏览器Excel');
    await waitRun(page, runId, ['succeeded']);
    const section = page.locator('.artifact-section');
    await section.waitFor({ timeout: 15000 });
    const chip = section.locator('.file-chip-main').filter({ hasText: 'browser-acceptance.xlsx' });
    await chip.waitFor();
    await chip.click();
    const dialog = page.getByRole('dialog');
    await dialog.waitFor();
    const downloadPromise = page.waitForEvent('download');
    await dialog.getByRole('link', { name: /下载原文件/ }).click();
    const download = await downloadPromise;
    const saved = path.join(outputDir, 'browser-acceptance-downloaded.xlsx');
    await download.saveAs(saved);
    const info = await stat(saved);
    assert(info.size > 0, 'downloaded Excel is empty');
    assert(download.suggestedFilename().endsWith('.xlsx'), 'download filename is not xlsx');
    return { run_id: runId, suggested_filename: download.suggestedFilename(), bytes: info.size, saved_path: saved };
  });
  await screenshot(page, 'normal-02-excel-preview');
  await page.getByRole('dialog').getByRole('button', { name: '关闭窗口' }).click();

  await check('normal_approval_queue_page', async () => {
    await page.locator('.nav-item').filter({ hasText: '待审批操作' }).click();
    await page.getByRole('heading', { name: '待审批操作', exact: true }).waitFor();
    await page.getByText('暂无待审批操作').waitFor({ timeout: 10000 });
    return { queue: 'empty_after_decision' };
  });
  await screenshot(page, 'normal-03-approval-queue');
}

async function main() {
  await diagnostic('main started');
  await mkdir(outputDir, { recursive: true });
  const vite = await import(pathToFileURL(process.env.VITE_MODULE_ENTRY).href);
  const playwright = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE_ENTRY).href);
  await diagnostic('modules imported');
  const previewServer = await vite.preview({
    root: process.cwd(),
    logLevel: 'silent',
    preview: {
      host: '127.0.0.1',
      port: previewPort,
      strictPort: true,
      proxy: {
        '/api': {
          target: process.env.BROWSER_BASE_URL,
          changeOrigin: false,
        },
      },
    },
  });
  await diagnostic('preview started');
  let context;
  try {
    const browser = await playwright.chromium.launch({
      headless: true,
      executablePath: process.env.PLAYWRIGHT_CHROMIUM,
      args: ['--no-sandbox', '--disable-dev-shm-usage'],
    });
    await diagnostic('browser launched');
    context = await browser.newContext({
      viewport: { width: 1440, height: 1000 },
      locale: 'zh-CN',
      acceptDownloads: true,
    });
    const page = await context.newPage();
    page.on('console', message => {
      if (message.type() === 'error') {
        report.console_errors.push({ text: message.text(), location: message.location() });
      }
    });
    page.on('pageerror', error => report.page_errors.push({ message: error.message, stack: error.stack }));
    page.on('requestfailed', request => report.request_failures.push({
      url: request.url(),
      method: request.method(),
      failure: request.failure()?.errorText || 'unknown',
    }));
    page.on('response', response => {
      if (response.status() >= 400) {
        report.http_errors.push({
          url: response.url(),
          method: response.request().method(),
          status: response.status(),
        });
      }
    });

    await screenshot(page, `${mode}-00-login`, false).catch(() => null);
    if (mode === 'trial') await verifyTrial(page);
    else if (mode === 'normal') await verifyNormal(page);
    else throw new Error(`unknown browser mode: ${mode}`);

    const unexpectedHttp = report.http_errors.filter(item => !(/\/api\/v1\/auth\/me$/.test(new URL(item.url).pathname) && item.status === 401));
    const unexpectedConsole = report.console_errors.filter(item => !/auth\/me|401 \(Unauthorized\)/i.test(`${item.text} ${item.location?.url || ''}`));
    const unexpectedFailures = report.request_failures.filter(item => !(
      item.failure === 'net::ERR_ABORTED'
      && (/\/events$/.test(new URL(item.url).pathname) || item.url.includes('/api/v1/auth/me'))
    ));
    assert(unexpectedHttp.length === 0, `unexpected HTTP errors: ${JSON.stringify(unexpectedHttp).slice(0, 1000)}`);
    assert(unexpectedConsole.length === 0, `unexpected console errors: ${JSON.stringify(unexpectedConsole).slice(0, 1000)}`);
    assert(report.page_errors.length === 0, `page errors: ${JSON.stringify(report.page_errors).slice(0, 1000)}`);
    assert(unexpectedFailures.length === 0, `failed requests: ${JSON.stringify(unexpectedFailures).slice(0, 1000)}`);
    report.status = report.checks.some(item => item.status === 'failed') ? 'failed' : 'passed';
  } catch (error) {
    report.issues.push(String(error?.stack || error).slice(0, 4000));
    report.status = 'failed';
  } finally {
    await diagnostic('cleanup started');
    await context?.close().catch(() => null);
    await new Promise((resolve, reject) => previewServer.httpServer.close(error => error ? reject(error) : resolve()));
    report.checked_at = new Date().toISOString();
    await writeFile(reportPath, JSON.stringify(report, null, 2));
    await diagnostic('report written');
  }
  process.exit(report.status === 'passed' ? 0 : 1);
}

try {
  await main();
} catch (error) {
  report.status = 'failed';
  report.issues.push(String(error?.stack || error).slice(0, 4000));
  try {
    await mkdir(outputDir, { recursive: true });
    await writeFile(reportPath, JSON.stringify(report, null, 2));
  } catch (writeError) {
    console.error('could not write browser report', writeError);
  }
  console.error(error?.stack || error);
  process.exit(1);
}
