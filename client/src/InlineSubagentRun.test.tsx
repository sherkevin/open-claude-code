import { describe, expect, it } from 'vitest';
import { subagentRunForQuestion } from './ChatPage';
import type { Message } from './types';

const original: Message = { id: 'q1', conversation_id: 'c', role: 'user', text: '统计数量', file_ids: [], run_id: 'r1', created_at: '' };

describe('inline assignment to the initiating question', () => {
  it('attaches a live plan only to the question that started the run', () => {
    expect(subagentRunForQuestion(original, 'q1')).toBe('r1');
    expect(subagentRunForQuestion({ ...original, run_id: null }, 'q1', 'live')).toBe('live');
    expect(subagentRunForQuestion({ ...original, id: 'steering' }, 'q1')).toBeNull();
    expect(subagentRunForQuestion({ ...original, role: 'assistant' }, 'q1')).toBeNull();
  });
  it('recovers the authorized historical plan even for an old message with no run ID', () => {
    expect(subagentRunForQuestion({ ...original, has_subagents: true, run_id: null, subagent_run_id: 'old' })).toBe('old');
    expect(subagentRunForQuestion(original)).toBeNull();
  });
});
