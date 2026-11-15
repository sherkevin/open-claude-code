import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import HistoricalBusinessResults, { historicalResultMessages, loadHistoricalResult, parseHistoricalResult } from './HistoricalBusinessResults';
import BusinessResults from './BusinessResult';
import type { Message } from './types';

const target = { conversationId: 'conversation-1', runId: 'run-1', messageId: 'answer-1' };
const trace = () => ({
  schema_version: 1,
  run: { id: 'run-1', conversation_id: 'conversation-1', assistant_message_id: 'answer-1', status: 'succeeded' },
  events: [
    { id: 1, run_id: 'run-1', type: 'tool.completed', created_at: '2026-11-15T12:00:00Z', data: {
      success: true, source_ids: ['registered-source'], business_result: { schema_version: 'business-result/v1', title: '上次发货结果', columns: [{ key: 'customer', label: '客户' }], rows: [{ customer: '演示客户甲' }], synthetic: true, complete: false, source_ids: ['untrusted-source'] },
    } },
    { id: 2, run_id: 'run-1', type: 'run.completed', created_at: '2026-11-15T12:00:01Z', data: { status: 'succeeded' } },
  ],
});
const message = (id: string, overrides: Partial<Message> = {}): Message => ({ id, conversation_id: 'conversation-1', run_id: 'run-1', role: 'assistant', text: '见本次结果', file_ids: [], created_at: '', ...overrides });
afterEach(() => vi.unstubAllGlobals());

describe('historical results are loaded only on demand', () => {
  it('renders only an entry and makes no trace request on first render', () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
    const html = renderToStaticMarkup(<HistoricalBusinessResults {...target}/>);
    expect(html).toContain('查看本次业务结果');
    expect(html).not.toContain('role="dialog"');
    expect(html).not.toContain('上次发货结果');
    expect(fetch).not.toHaveBeenCalled();
  });
  it('offers one last visible assistant answer per historical run in this conversation', () => {
    const messages = [message('user', { role: 'user' }), message('early'), message('answer-1'), message('empty', { text: '' }), message('current', { run_id: 'current-run' }), message('foreign', { conversation_id: 'other' }), message('no-run', { run_id: null })];
    expect([...historicalResultMessages(messages, target.conversationId, 'current-run')]).toEqual(['answer-1']);
    expect([...historicalResultMessages([message('file-only', { text: '', file_ids: ['file'] })], target.conversationId)]).toEqual(['file-only']);
  });
  it('retains a trace entry for an empty historical answer when model transport failed after tools returned', () => {
    const messages=[message('empty-answer',{text:''}),message('new-answer',{run_id:'current-run'})];
    expect([...historicalResultMessages(messages,target.conversationId,'current-run')]).toEqual(['empty-answer']);
  });
  it('reads only the selected authenticated trace with no cache and supports cancellation', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(trace()))); vi.stubGlobal('fetch', fetch);
    const signal = new AbortController().signal;
    const result = await loadHistoricalResult(target, signal);
    expect(result.runId).toBe(target.runId);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith('/api/v1/runs/run-1/trace', expect.objectContaining({ credentials: 'include', cache: 'no-store', signal }));
    expect(fetch.mock.calls[0][1].method).toBeUndefined();
  });
  it('propagates permission failures without borrowing another result and rechecks on retry', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: 'FORBIDDEN', message: '无权读取此任务' } }), { status: 403 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(trace()))); vi.stubGlobal('fetch', fetch);
    await expect(loadHistoricalResult(target)).rejects.toMatchObject({ status: 403 });
    expect((await loadHistoricalResult(target)).runId).toBe('run-1');
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls.every(([, init]) => init.cache === 'no-store')).toBe(true);
  });
});

describe('historical trace is bound to the exact answer', () => {
  it.each(['id', 'conversation_id', 'assistant_message_id'] as const)('rejects mismatched %s', key => {
    const value = trace(); value.run[key] = 'other';
    expect(() => parseHistoricalResult(value, target)).toThrow('当前回答不一致');
  });
  it.each(['queued', 'running', 'awaiting_approval', 'cancelling', 'cancelled', 'interrupted', 'unknown'])('does not present %s as successful results', status => {
    const value = trace(); value.run.status = status;
    expect(() => parseHistoricalResult(value, target)).toThrow('不能展示为已完成');
  });
  it('preserves only valid pre-failure tool data in a historical failed run, with an incomplete-answer notice', () => {
    const value=trace();value.run.status='failed';value.events[1].data.status='failed';
    value.events.push({...value.events[0],id:3});
    const result=parseHistoricalResult(value,target);
    expect(result.status).toBe('failed');
    const html=renderToStaticMarkup(<BusinessResults events={result.events} runId={result.runId} status={result.status}/>);
    expect(html).toContain('业务数据已返回，回答未完成');
    expect(html.match(/class="business-result br-object-result"/g)).toHaveLength(1);
    expect(html).toContain('演示客户甲');expect(html).not.toContain('untrusted-source');
  });
  it('rejects mixed-run events and conflicting completion status', () => {
    const mixed = trace(); mixed.events[0].run_id = 'another-run';
    expect(() => parseHistoricalResult(mixed, target)).toThrow('事件无法核对');
    const conflict = trace(); conflict.events[1].data.status = 'failed';
    expect(() => parseHistoricalResult(conflict, target)).toThrow('状态不一致');
  });
  it.each([null, {}, { schema_version: 2, ...{ run: trace().run, events: [] } }, { schema_version: 1, run: trace().run, events: null }])('rejects malformed trace %j', value => {
    expect(() => parseHistoricalResult(value, target)).toThrow('格式无法识别');
  });
  it('reuses result validation, source registration and the historical data labels', () => {
    const result = parseHistoricalResult(trace(), target);
    const html = renderToStaticMarkup(<BusinessResults events={result.events} runId={result.runId} status={result.status}/>);
    expect(html).toContain('上次发货结果'); expect(html).toContain('演示客户甲');
    expect(html).toContain('演示测试数据'); expect(html).toContain('部分结果');
    expect(html).toContain('查看依据 1'); expect(html).not.toContain('untrusted-source');
  });
  it('allows a completed answer without business cards to show the explicit empty state', () => {
    const value = trace(); value.events = [];
    const result = parseHistoricalResult(value, target);
    expect(renderToStaticMarkup(<BusinessResults events={result.events} runId={result.runId} status={result.status}/>)).toBe('');
  });
});
