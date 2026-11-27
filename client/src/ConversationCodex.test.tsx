import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { CodexStatus, codexControls, codexRequestKey, codexTurnPayload, keepCodexRequestId, type CodexThread } from './ConversationCodex';

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
    const storage = { getItem: (key: string) => entries.get(key) ?? null, setItem: (key: string, id: string) => { entries.set(key, id); } };
    const key = codexRequestKey('a:b', 'c');
    const id = keepCodexRequestId(storage, key, null);
    expect(keepCodexRequestId(storage, key, null)).toBe(id);
    expect([...entries]).toEqual([[key, id]]);
    expect(codexRequestKey('a', 'b:c')).not.toBe(key);
    expect(codexRequestKey('a:b', 'd')).not.toBe(key);
    entries.clear();
    expect(keepCodexRequestId(storage, key, id)).toBe(id);
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
