import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ChatPage from './ChatPage';
import { CodexHistoryEntries, readCodexReceiptHistory } from './CodexReceiptHistory';
import { apiURL } from './api';
import type { AppConfig, Conversation } from './types';

afterEach(() => vi.unstubAllGlobals());

describe('ordinary conversation Codex receipt history', () => {
  const history = { recent_limit: 8, unavailable_count: 1, pending: true, receipts: [
    { request_id: 'request-b', turn_id: 'turn-b', status: 'completed', text: '第二份正文', text_truncated: false },
    { request_id: 'request-a', turn_id: 'turn-a', status: 'completed', text: '<旧正文>', text_truncated: true },
    { request_id: 'request-c', turn_id: 'turn-c', status: 'interrupted', text: '', text_truncated: true },
  ] };

  it('exposes a separate history entry on the normal chat page only for an enabled owner', () => {
    const conversation: Conversation = { id: 'c', assistant_id: 'a', title: '会话', model_id: 'm',
      model_label: 'M', reasoning_level: 'low', archived: false, active_run_id: null, created_at: '', updated_at: '' };
    const config: AppConfig = { brand_name: '开物', auth_types: [], model_label: 'M', upload_max_bytes: 1024 };
    const page = (settings: AppConfig, owner?: string) => renderToStaticMarkup(<ChatPage conversation={conversation}
      config={settings} draftOwnerId={owner} models={[]} modelsLoading={false} onChanged={() => {}} onArchived={() => {}}/>);
    expect(page({ ...config, codex_appserver: true }, 'owner')).toContain('<summary>Codex 工程回执历史</summary>');
    expect(page(config, 'owner')).not.toContain('工程回执历史');
    expect(page({ ...config, codex_appserver: true })).not.toContain('工程回执历史');
    expect(page({ ...config, codex_appserver: true, deployment_mode: 'trial' }, 'owner')).not.toContain('工程回执历史');
  });

  it('reopens with GET only and renders the same ordered receipts without a saved request id', async () => {
    const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify(history)));
    vi.stubGlobal('fetch', fetch);
    const load = async () => renderToStaticMarkup(<CodexHistoryEntries history={await readCodexReceiptHistory('own/c')}/>);
    const first = await load();
    expect(await load()).toBe(first);
    expect(fetch).toHaveBeenCalledTimes(2);
    for (const [url, init] of fetch.mock.calls) {
      expect(url).toBe(apiURL('/conversations/own%2Fc/codex/history'));
      expect(init).toMatchObject({ method: 'GET', cache: 'no-store' });
      expect(init.body).toBeUndefined();
    }
    expect(first.indexOf('第二份正文')).toBeLessThan(first.indexOf('&lt;旧正文&gt;'));
    expect(first.match(/已完成/g)).toHaveLength(2);
    expect(first).toContain('已取消');
    expect(first).toContain('仅显示此请求留存的部分正文');
    expect(first).toContain('仅留存轮次状态，正文未保存');
    expect(first).toContain('最近 8 份');
    expect(first).toContain('已淘汰的正文无法恢复');
    expect(first).toContain('1 个已登记的请求没有可读回执');
    expect(first).toContain('仍待确认最终状态');
    expect(first).not.toMatch(/发送任务|用原编号重试|<form|<time/);
  });

  it('keeps read failures distinct from a successfully read empty history', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: { code: 'CODEX_SESSION_BUSY', message: 'Codex 仅支持未归档的空闲会话' },
    }), { status: 409 })));
    await expect(readCodexReceiptHistory('busy')).rejects.toMatchObject({ code: 'CODEX_SESSION_BUSY' });
    const html = renderToStaticMarkup(<CodexHistoryEntries history={{ ...history, receipts: [], pending: false, unavailable_count: 0 }}/>);
    expect(html).toContain('当前没有可展示的已留存回执');
    expect(html).not.toMatch(/未执行|未发送|任务没有发生/);
  });

  it('distinguishes withheld text from a missing body and preserves the truncation flag', () => {
    const turn = { ...history.receipts[0], text: '', text_withheld: true, text_truncated: true };
    const html = renderToStaticMarkup(<CodexHistoryEntries history={{ ...history, receipts: [turn] }}/>);
    expect(html).toContain('正文含宿主路径或凭据标记，已隐藏');
    expect(html).toContain('留存的正文已截断');
    expect(html).not.toContain('正文未保存');
  });
});
