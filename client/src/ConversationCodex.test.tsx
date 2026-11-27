import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { CodexStatus, OriginalCodexReceipt, originalRequestResolved, codexControls, codexRequestKey, codexTurnPayload, keepCodexRequestId, type CodexThread } from './ConversationCodex';

const idle: CodexThread = { bound: false, active_turn: null, last_turn: null, recovery_required: false };
const view = (thread: CodexThread, pending = false, sending = false, preparing = false) =>
  renderToStaticMarkup(<CodexStatus thread={thread} sending={sending} pending={pending} preparing={preparing}/>);

describe('Codex first request and recovery', () => {
  it('supports a single explicit cold send and preserves the original bound turn payload', () => {
    expect(codexControls(idle)).toEqual({ send: true, prepare: true, reconcile: false });
    expect(codexControls({ ...idle, operation: null })).toEqual(codexControls(idle));
    const body = { client_request_id: 'request-001', text: 'hello' };
    expect(codexTurnPayload(idle, 'request-001', '  hello ')).toEqual({ ...body, prepare_if_needed: true });
    expect(codexTurnPayload({ ...idle, bound: true }, 'request-001', 'hello')).toEqual(body);
  });

  it('keeps only the opaque request id on refresh and reuses it for an explicit retry', () => {
    const entries = new Map<string, string>();
    const storage = () => ({ getItem: (key: string) => entries.get(key) ?? null,
      setItem: (key: string, id: string) => { entries.set(key, id); }, removeItem: (key: string) => { entries.delete(key); } });
    const key = codexRequestKey('a:b', 'c');
    const { id } = keepCodexRequestId(key, null, storage);
    expect(keepCodexRequestId(key, storage().getItem(key), storage)).toEqual({ id, stored: true });
    expect([...entries]).toEqual([[key, id]]);
    expect(codexRequestKey('a', 'b:c')).not.toBe(key);
    expect(codexRequestKey('a:b', 'd')).not.toBe(key);
    const blocked = () => { throw new Error('sessionStorage property inaccessible'); };
    expect(keepCodexRequestId(key, id, blocked)).toEqual({ id, stored: false });
    expect(keepCodexRequestId(key, null, blocked).id).not.toBe(id);
  });

  it('renders recorded, expired, pending and missing receipts without inferring an outcome', () => {
    const record = { request_id: 'old-request', state: 'recorded' as const, turn: {
      request_id: 'old-request', turn_id: 'old-turn', status: 'completed', text: '留存首段', text_truncated: true,
    } };
    const html = renderToStaticMarkup(<OriginalCodexReceipt receipt={record}/>);
    expect(html).toContain('old-request');
    expect(html).toContain('仅显示此请求留存的部分正文');
    expect(html).toContain('留存首段');
    for (const [state, message, resolved] of [
      ['admitted_without_receipt', '无法找回正文或确认最终状态', true],
      ['pending', '最终回执仍待确认', false], ['not_found', '执行结果仍未确认', false],
    ] as const) {
      const receipt = { request_id: 'old-request', state, turn: null };
      expect(originalRequestResolved(receipt)).toBe(resolved);
      const status = renderToStaticMarkup(<OriginalCodexReceipt receipt={receipt}/>);
      expect(status).toContain(message);
      expect(status).not.toMatch(/未执行|执行中|已完成/);
    }
  });

  it('distinguishes locally sending, waiting for a receipt, live preparation, and a durable native binding', () => {
    expect(view(idle, false, true)).toContain('等待开物确认');
    expect(view(idle, true)).toContain('原请求结果待确认');
    expect(view(idle, true)).not.toContain('开物已收到');
    expect(view(idle, false, false, true)).toContain('正在请求准备会话');
    const preparing: CodexThread = { ...idle, operation: { phase: 'preparing', request_id: 'request-001' } };
    expect(view(preparing)).toContain('开物已收到请求，正在准备 Codex 会话');
    expect(codexControls(preparing)).toEqual({ send: false, prepare: false, reconcile: false });
    expect(view({ ...preparing, bound: true, operation: { phase: 'thread_ready', request_id: 'request-001' } })).toContain('Codex 会话已绑定，正在启动任务');
    expect(view({ ...preparing, bound: true, operation: { phase: 'thread_ready', request_id: null } })).not.toContain('正在启动任务');
  });

  it('shows native output separately from an older completed turn', () => {
    const thread: CodexThread = { ...idle, bound: true, last_turn: { turn_id: 'old', status: 'completed', text: '上轮回答', text_truncated: false },
      operation: { request_id: 'req-new', phase: 'running' } };
    expect(view(thread)).toContain('执行中，等待首段输出');
    const output = view({ ...thread, operation: { request_id: 'req-new', phase: 'output', text: '本轮首段' } });
    expect(output).toContain('执行中，已收到首段输出');
    expect(output.indexOf('本轮首段')).toBeLessThan(output.indexOf('上一轮结果'));
    expect(output.indexOf('上一轮结果')).toBeLessThan(output.indexOf('上轮回答'));
    expect(view({ ...thread, operation: null })).toContain('已完成');
  });

  it('prevents preparing twice after reopening a manual operation without a request id', () => {
    const manual: CodexThread = { ...idle, operation: { phase: 'preparing', request_id: null } };
    expect(codexControls(manual)).toEqual({ send: false, prepare: false, reconcile: false });
  });

  it('shows the final receipt once while the same operation still owns the host', () => {
    const thread: CodexThread = { ...idle, bound: true, last_turn: {
      turn_id: 'finished', request_id: 'request-001', status: 'completed', text: '最终正文', text_truncated: false,
    }, operation: { phase: 'output', request_id: 'request-001', text: '首段预览' } };
    const html = view(thread, true, true);
    expect(html).toContain('已完成');
    expect(html.match(/最终正文/g)).toHaveLength(1);
    expect(html).not.toMatch(/上一轮结果|执行中|首段预览/);
    expect(codexControls(thread).send).toBe(false);
    const settled = renderToStaticMarkup(<CodexStatus thread={{ ...thread, operation: null }} requestId="request-001" sending pending={false} preparing={false}/>);
    expect(settled).toContain('已完成');
    expect(settled).not.toContain('正在提交');
  });

  it('allows recovery only after the live operation is gone', () => {
    const abandoned: CodexThread = { ...idle, bound: true, recovery_required: true,
      active_turn: { turn_id: 'turn-id', can_interrupt: false, interrupt_requested: false } };
    expect(codexControls(abandoned)).toEqual({ send: false, prepare: false, reconcile: true });
    expect(view(abandoned)).toContain('上次执行待核对');
    const live = { ...abandoned, operation: { request_id: 'request-001', phase: 'running' as const } };
    expect(codexControls(live).reconcile).toBe(false);
    expect(codexControls({ ...abandoned, active_turn: { ...abandoned.active_turn!, can_interrupt: true } }).reconcile).toBe(false);
  });
});
