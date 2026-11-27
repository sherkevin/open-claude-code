/** Exercise the actual chat memory dialog against the disposable SDK service. */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_CORE_PATH);
const origin = new URL(process.env.MEMORY_TEST_ORIGIN);
assert.equal(origin.hostname, '127.0.0.1');
const root = new URL('/project/kaiwu/', origin);
const mode = process.env.MEMORY_TEST_MODE;
const cid = process.env.MEMORY_TEST_CONVERSATION;
const browser = await chromium.launch({
  executablePath: process.env.BROWSER_EXECUTABLE,
  args: ['--disable-dev-shm-usage', '--no-sandbox'], headless: true,
});
try {
  const context = await browser.newContext();
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    return url.origin === origin.origin ? route.continue() : route.abort();
  });
  const login = await context.request.post(new URL('api/v1/auth/login', root).href, {
    data: {username: 'owner', password: 'synthetic-mem0-product-123'},
  });
  assert.equal(login.status(), 200);
  const page = await context.newPage();
  const actions = [];
  page.on('request', r => {
    if (r.method() === 'POST') actions.push(new URL(r.url()).pathname.split('/').pop());
  });
  await page.goto(root.href + '?conversation=' + cid);
  const dialog = page.getByRole('dialog', {name:'会话记忆'});
  async function openMemory() {
    await page.getByRole('button', {name:'会话记忆', exact:true}).click();
    await dialog.getByRole('button', {name:'新建文件', exact:true}).waitFor();
  }
  const panel = dialog.getByRole('region', {name:'检索会话记忆'});
  await openMemory();
  if (mode === 'disabled' || mode === 'unconfigured') {
    await page.waitForFunction(() => performance.getEntriesByType('resource')
      .some(e => e.name.endsWith('/memory/semantic/status') && e.responseEnd > 0));
    assert.equal(await panel.count(), 0);
  } else {
    const content = '备件库存警戒线为十二套。';
    await dialog.getByRole('button', {name:'新建文件', exact:true}).click();
    await page.getByRole('button', {name:'开始编辑', exact:true}).click();
    await dialog.getByRole('button', {name:'源码编辑', exact:true}).click();
    await dialog.getByRole('textbox', {name:'记忆内容', exact:true}).fill(content);
    if (mode === 'enabled') {
      await panel.getByRole('textbox', {name:'检索内容'}).fill('库存警戒线');
    }
    await dialog.getByRole('button', {name:'保存记忆', exact:true}).click();
    await dialog.getByText(/记忆文件已保存/).waitFor();
    assert.equal(actions.includes('sync'), false);
    if (mode === 'baseline') {
      assert.equal(await panel.count(), 0);
    } else if (mode === 'partial') {
      const [sync] = await Promise.all([
        page.waitForResponse(r => r.url().endsWith('/memory/semantic/sync')),
        panel.getByRole('button', {name:'同步已保存的记忆'}).click(),
      ]);
      assert.equal(sync.status(), 200);
      assert.equal((await sync.json()).status, 'partial');
      await panel.getByText(/同步结果尚未确认/).waitFor();
      assert.equal(await panel.getByText('已同步此会话保存的记忆。').count(), 0);
      assert.equal(actions.filter(x => x === 'sync').length, 1);
    } else {
      const input = panel.getByRole('textbox', {name:'检索内容'});
      const search = panel.getByRole('button', {name:'检索记忆', exact:true});
      assert.equal(await input.inputValue(), '库存警戒线');
      await search.click();
      await panel.getByText(/未取得可核验的结果/).waitFor();
      await panel.getByRole('button', {name:'同步已保存的记忆'}).click();
      await panel.getByText('已同步此会话保存的记忆。').waitFor();
      await search.click();
      await panel.getByText(content, {exact:true}).waitFor();
      const source = await panel.locator('article small').innerText();
      assert.match(source, /^MEMORY\.md · 版本 \d+$/);
      await page.reload();
      await openMemory();
      assert.equal(await panel.locator('article').count(), 0);
      await input.fill('库存警戒线');
      await search.click();
      await panel.getByText(content, {exact:true}).waitFor();
      assert.equal(await panel.locator('article small').innerText(), source);
      assert.equal(actions.filter(x=>x === 'sync').length, 1);
      assert.equal(actions.filter(x=>x === 'query').length, 3);
      if (process.env.MEMORY_SCREENSHOT) await dialog.screenshot({path: process.env.MEMORY_SCREENSHOT});
      console.log(JSON.stringify({mode, source, content, explicit_sync:1, explicit_query:3}));
    }
  }
  assert.equal(actions.includes('messages'), false);
  console.log(JSON.stringify({mode, native_editor:true, semantic_panel:mode==='enabled'||mode==='partial', model_messages:0}));
} finally {
  await browser.close();
}
