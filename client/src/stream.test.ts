import { describe, expect, it } from 'vitest';
import { appendEvent, pendingApprovalIds, streamText } from './stream';
import type { StreamEvent } from './types';

const event = (id: number, type: string, data: Record<string, unknown>, run_id = 'run-1'): StreamEvent => ({ id, type, data, run_id, created_at: '2026-11-05T01:00:00Z' });

describe('persisted SSE replay', () => {
  it('does not duplicate answer text when a reconnect replays the last acknowledged delta', () => {
    const first = event(1, 'message.delta', { message_id: 'message-1', text: '业务' });
    const second = event(2, 'message.delta', { message_id: 'message-1', text: '结果' });
    let events: StreamEvent[] = [];
    for (const next of [first, second, second, event(3, 'message.delta', { text: '已返回' })]) events = appendEvent(events, next);
    expect(streamText(events)).toBe('业务结果已返回');
    expect(events).toHaveLength(3);
  });
  it('orders persisted events by their server cursor and ignores malformed cursors', () => {
    let events = appendEvent([], event(3, 'message.delta', { text: 'C' }));
    events = appendEvent(events, event(1, 'message.delta', { text: 'A' }));
    events = appendEvent(events, event(2, 'message.delta', { text: 'B' }));
    events = appendEvent(events, event(-1, 'message.delta', { text: 'bad' }));
    events = appendEvent(events, event(Number.NaN, 'message.delta', { text: 'bad' }));
    expect(streamText(events)).toBe('ABC');
  });
  it('does not include tool output or malformed delta data in assistant prose', () => {
    expect(streamText([event(1, 'tool.completed', { text: 'private-tool-result' }), event(2, 'message.delta', { text: { malformed: true } }), event(3, 'message.delta', { text: '答案' })])).toBe('答案');
  });
  it('replaces draft source markers with final canonical citations on reconnect', () => {
    const events=[event(1,'message.delta',{text:'结果[[source:abc]]'}),event(2,'message.updated',{text:'结果[1](/api/v1/sources/abc)',citations:[{id:'abc'}]}),event(3,'run.completed',{status:'succeeded'})];
    expect(streamText(events)).toBe('结果[1](/api/v1/sources/abc)');
    expect(streamText(appendEvent(events,events[1]))).toBe('结果[1](/api/v1/sources/abc)');
  });
});

describe('approval lifecycle after reload', () => {
  it('restores only unresolved approvals from a replay', () => {
    const events = [event(1, 'approval.required', { id: 'approval-a' }), event(2, 'approval.required', { id: 'approval-b' }), event(3, 'approval.resolved', { approval_id: 'approval-a', decision: 'deny' })];
    expect([...pendingApprovalIds(events)]).toEqual(['approval-b']);
  });
  it('invalidates every pending approval when a run becomes terminal', () => {
    for (const status of ['succeeded', 'cancelled', 'interrupted', 'failed']) {
      const events = [event(1, 'approval.required', { id: 'approval-a' }), event(2, 'run.completed', { status })];
      expect(pendingApprovalIds(events).size).toBe(0);
    }
  });
});
