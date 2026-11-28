import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import BusinessResults, { BusinessResultCard, displayBusinessCell, parseBusinessResult, resultCSV, resultsFromEvents } from './BusinessResult';
import { loadHistoricalResult, parseHistoricalResult } from './HistoricalBusinessResults';
import type { StreamEvent } from './types';

// Synthetic canonical receipt only. The title, warning and count_receipt shape
// mirror the bounded handoff contract; no customer value or identifier is used.
const WARNING = '工具报告的聚合值未经独立核验，查询范围不代表业务全量。';
const canonical = (overrides: Record<string, unknown> = {}): Record<string, unknown> => ({
  schema_version: 'business-result/v1',
  title: '工具报告聚合值',
  columns: [{ key: 'count', label: '记录数', type: 'number' }],
  rows: [{ count: 0 }],
  scope: {},
  data_time: null,
  returned_rows: 1,
  total_rows: null,
  complete: null,
  synthetic: null,
  truncated: false,
  warnings: [WARNING],
  source_ids: [],
  count_receipt: { operation: 'count', scope_verified: false },
  ...overrides,
});
const target = { conversationId: 'conversation-parity', runId: 'run-parity', messageId: 'answer-parity' };
const receipt = (overrides: Record<string, unknown> = {}, data: Record<string, unknown> = {}): StreamEvent => ({
  id: 1, run_id: target.runId, type: 'tool.completed', created_at: '2026-11-28T10:00:00+08:00',
  data: { success: true, business_result: canonical(overrides), source_ids: [], ...data },
});
const completed = (): StreamEvent => ({
  id: 2, run_id: target.runId, type: 'run.completed',
  created_at: '2026-11-28T10:00:01+08:00', data: { status: 'succeeded' },
});
const trace = () => ({
  schema_version: 1,
  run: { id: target.runId, conversation_id: target.conversationId, assistant_message_id: target.messageId, status: 'succeeded' },
  events: [receipt(), completed()],
});
const card = (overrides: Record<string, unknown> = {}) =>
  renderToStaticMarkup(<BusinessResultCard result={parseBusinessResult(canonical(overrides))!} runId={target.runId} eventId={1}/>);
afterEach(() => vi.unstubAllGlobals());

describe('canonical aggregate receipt display', () => {
  it('shows the tool-reported aggregate title exactly once as the card heading', () => {
    const html = card();
    expect(html).toContain('<h3>工具报告聚合值</h3>');
    expect(html.match(/<section class="business-result br-object-result"/g) ?? []).toHaveLength(1);
    expect(html).not.toContain('宿主核验聚合值');
    expect(html.match(/class="br-warnings"/g) ?? []).toHaveLength(1);
  });

  it('renders an exact numeric zero as 0 rather than unknown or empty', () => {
    const result = parseBusinessResult(canonical())!;
    expect(result.rows).toEqual([{ count: 0 }]);
    expect(result.returned_rows).toBe(1);
    expect(displayBusinessCell(0, result.columns[0])).toBe('0');
    const html = card();
    expect(html).toContain('记录数');
    expect(html).toContain('>0</dd>');
    expect(html).toContain('已返回 1 行');
    expect(html).not.toContain('本次查询未返回记录');
    expect(resultCSV(result, target.runId, 1)).toContain('"0"');
  });

  it('keeps complete and total_rows unknown without inferring full coverage', () => {
    const result = parseBusinessResult(canonical())!;
    expect(result.complete).toBeNull();
    expect(result.total_rows).toBeNull();
    const html = card();
    expect(html).toContain('结果总行数未提供');
    expect(html).toContain('完整性未说明');
    expect(html).not.toContain('数据源标记为完整');
    expect(html).not.toContain('部分结果');
  });

  it('shows no source reference and never promotes a tool-supplied source id', () => {
    const result = parseBusinessResult(canonical())!;
    expect(result.source_ids).toEqual([]);
    const html = card();
    expect(html).toContain('本次结果未提供来源详情。');
    expect(html).not.toContain('查看依据');
    const event = receipt();
    (event.data.business_result as Record<string, unknown>).source_ids = ['tool_claimed_source'];
    const projected = resultsFromEvents([event], target.runId)[0].result;
    expect(projected.source_ids).toEqual([]);
    expect(projected.title).toBe('工具报告聚合值');
  });

  it('surfaces the bounded scope warning without claiming independent verification', () => {
    const html = card();
    expect(html).toContain(WARNING);
    expect(html).toContain('未经独立核验');
    expect(html).not.toContain('独立核验通过');
    expect(html).not.toContain('已验证');
    const many = parseBusinessResult(canonical({ warnings: Array.from({ length: 25 }, (_, index) => `提示-${index + 1}`) }))!;
    expect(many.warnings).toHaveLength(20);
    expect(many.warnings[19]).toBe('提示-20');
    const mixed = parseBusinessResult(canonical({ warnings: [WARNING, 42, null, { html: '<b>bad</b>' }] }))!;
    expect(mixed.warnings).toEqual([WARNING]);
  });

  it('rejects malformed canonical payloads and publishes no card', () => {
    expect(parseBusinessResult(null)).toBeNull();
    expect(parseBusinessResult({ ...canonical(), columns: [] })).toBeNull();
    expect(parseBusinessResult({ ...canonical(), rows: [{ count: { nested: 1 } }] })).toBeNull();
    expect(parseBusinessResult({ ...canonical(), rows: 'not-an-array' })).toBeNull();
    const failed = receipt({}, { success: false });
    expect(resultsFromEvents([failed], target.runId)).toEqual([]);
    const malformed = receipt({ rows: 'not-an-array' });
    expect(resultsFromEvents([malformed], target.runId)).toEqual([]);
  });

  it('renders an empty canonical table as no returned records without inventing a zero aggregate', () => {
    const result = parseBusinessResult(canonical({ rows: [], returned_rows: 0 }))!;
    expect(result.rows).toEqual([]);
    const html = card({ rows: [], returned_rows: 0 });
    expect(html).toContain('本次查询未返回记录');
    expect(html).not.toContain('>0</dd>');
    expect(html).not.toContain('class="br-object"');
    expect(html).toContain('class="br-evidence" open=""');
  });
});

describe('historical reload keeps one canonical receipt', () => {
  it('renders one card from an authenticated succeeded trace and reloads it unchanged', async () => {
    const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify(trace())));
    vi.stubGlobal('fetch', fetch);
    const first = await loadHistoricalResult(target);
    const second = await loadHistoricalResult(target);
    expect(first.runId).toBe(target.runId);
    expect(fetch).toHaveBeenCalledTimes(2);
    const html = renderToStaticMarkup(<BusinessResults events={first.events} runId={first.runId} status={first.status} />);
    expect(html.match(/class="business-result br-object-result"/g) ?? []).toHaveLength(1);
    expect(html).toContain('工具报告聚合值');
    expect(html).toContain(WARNING);
    expect(html).toContain('>0</dd>');
    expect(renderToStaticMarkup(<BusinessResults events={second.events} runId={second.runId} status={second.status} />)).toBe(html);
  });

  it('keeps a failed run card with the incomplete-answer notice and no duplicate', () => {
    const value = trace();
    value.run.status = 'failed';
    value.events[1].data.status = 'failed';
    const result = parseHistoricalResult(value, target);
    expect(result.status).toBe('failed');
    const html = renderToStaticMarkup(<BusinessResults events={result.events} runId={result.runId} status={result.status} />);
    expect(html).toContain('业务数据已返回，回答未完成');
    expect(html.match(/class="business-result br-object-result"/g) ?? []).toHaveLength(1);
    expect(html).toContain('>0</dd>');
    expect(resultCSV(resultsFromEvents(result.events, result.runId, result.status)[0].result, result.runId, 1, true)).toContain('业务数据已返回，回答未完成');
  });

  it('rejects a trace bound to another answer or an unfinished run', () => {
    const other = trace();
    other.run.assistant_message_id = 'another-answer';
    expect(() => parseHistoricalResult(other, target)).toThrow('当前回答不一致');
    const running = trace();
    running.run.status = 'running';
    expect(() => parseHistoricalResult(running, target)).toThrow('不能展示为已完成');
  });

  it('reloads after an authorization failure without borrowing another result', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: 'FORBIDDEN', message: '无权读取此任务' } }), { status: 403 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(trace())));
    vi.stubGlobal('fetch', fetch);
    await expect(loadHistoricalResult(target)).rejects.toMatchObject({ status: 403 });
    expect((await loadHistoricalResult(target)).runId).toBe(target.runId);
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('shows no card for an empty historical trace', () => {
    const value = trace();
    value.events = [];
    const result = parseHistoricalResult(value, target);
    expect(renderToStaticMarkup(<BusinessResults events={result.events} runId={result.runId} status={result.status} />)).toBe('');
  });
});
