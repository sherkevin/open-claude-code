import { describe, expect, it } from 'vitest';
import { compactionLabel } from './ContextDialog';
import type { Run } from './types';
import { compactionStatus, compactionStatusLabel } from './display';

const run: Run = { id: 'compact-test', conversation_id: 'conversation-test', kind: 'compact', status: 'succeeded', assistant_message_id: null, error: null, usage: null, approvals: [], created_at: '2026-11-05T00:00:00Z', completed_at: '2026-11-05T00:00:01Z', compaction: null };
const metadata = { trigger: 'manual', pre_tokens: null, before: null, after: null };

describe('native compaction evidence', () => {
  it('does not describe an ordinary successful run as completed compaction', () => {
    expect(compactionLabel(run)).toBe('未提供原生压缩结果');
    expect(compactionLabel({ ...run, compaction: { ...metadata, status: 'not_needed' } })).toBe('未发生压缩（可能历史较少）');
    expect(compactionLabel({ ...run, compaction: { ...metadata, status: 'completed' } })).toBe('已发生原生上下文压缩');
  });
  it('preserves an actual compact boundary when later steps fail or are stopped', () => {
    const compacted = { ...run, compaction: { ...metadata, status: 'completed' as const } };
    expect(compactionLabel({ ...compacted, status: 'failed' })).toBe('已发生原生上下文压缩，后续步骤失败');
    expect(compactionLabel({ ...compacted, status: 'cancelled' })).toBe('已发生原生上下文压缩，后续步骤已停止');
    expect(compactionLabel({ ...compacted, status: 'interrupted' })).toBe('已发生原生上下文压缩，后续步骤已中断');
    expect(compactionLabel({ ...run, status: 'running' })).toBe('正在执行原生压缩');
    expect(compactionLabel({ ...run, status: 'cancelling' })).toBe('正在停止压缩');
    expect(compactionStatusLabel('completed', 'cancelled')).toBe('已发生原生上下文压缩，后续步骤已停止');
  });
  it('keeps all native result states distinct and never maps failures to a no-op', () => {
    expect(compactionStatusLabel('failed')).toBe('原生压缩失败');
    expect(compactionStatusLabel('cancelled')).toBe('原生压缩已停止');
    expect(compactionStatusLabel('interrupted')).toBe('原生压缩已中断');
    expect(compactionStatusLabel('started')).toBe('正在执行原生压缩');
    expect(compactionStatusLabel(undefined)).toBe('未提供原生压缩结果');
    expect(compactionStatus('unrecognized-provider-result')).toBe('unknown');
  });
});
