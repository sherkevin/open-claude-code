import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { compactionLabel, contextCanCompact, contextStatus, ContextSummary } from './ContextDialog';
import type { NativeContext, Run } from './types';
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

const measured: NativeContext = {
  source: 'harness_token_meter', status: 'partial', total_tokens: 2345,
  max_tokens: null, percentage: null, auto_compact_enabled: false, auto_compact_threshold: null,
  model: null, measured_at: '2026-11-11T07:00:00Z', cached: false, error: null,
  precision: 'estimated', baseline_kind: 'usage', observed_prompt_tokens: null, observed_at: null,
  stale: false, stale_reason: null, projection_seq: 12, manual_compact_available: false,
  breakdown: { system_tokens: 200, tools_tokens: 100, message_tokens: 700 },
};
const summary = (context: NativeContext) => renderToStaticMarkup(createElement(ContextSummary, { context, models: [] }));

describe('Harness context evidence display', () => {
  it('separates estimates from unverified provider counts, capacity and composition', () => {
    const html = summary(measured);
    expect(html).toContain('Harness 上下文估算');
    expect(html).toContain('当前上下文占用（估算 Token）');
    expect(html).toContain('运行时归一化的用量记录');
    expect(html).toContain('原始用量完整性尚未核验');
    expect(html).toContain('供应商原始输入计数保持未知');
    expect(html).toContain('2,345');
    expect(html).toContain('供应商原始输入计数');
    expect(html).not.toContain('1,900');
    expect(html).not.toContain('<dd>0</dd>');
    expect(html).toContain('已核验上下文容量');
    expect(html).toContain('未提供');
    expect(html).toContain('不应相加作为总占用');
    expect(html).toContain('不等于累计 Token 账单');
    expect(html).not.toContain('1,000,000');
    expect(html).not.toContain('供应商采样时间');
  });
  it.each(['route_changed', 'request_unobserved', 'snapshot_expired'] as const)('does not display stale quantities for %s', reason => {
    const value = { ...measured, stale: true, stale_reason: reason, cached: true, observed_prompt_tokens: 1900 };
    const html = summary(value);
    expect(html).toContain(contextStatus(value));
    expect(html).not.toContain('2,345');
    expect(html).not.toContain('1,900');
    expect(html).not.toContain('查看上下文组成估算');
    expect(html).toContain('缺失数值保持未知');
  });
  it('does not promote missing provider samples or an unknown baseline to measurements', () => {
    const html = summary({ ...measured, precision: 'unknown', baseline_kind: 'none', observed_prompt_tokens: null });
    expect(html).not.toContain('2,345');
    expect(html).not.toContain('1,900');
    expect(html).toContain('缺失数值保持未知');
    const estimate = summary({ ...measured, baseline_kind: 'estimated', observed_prompt_tokens: null });
    expect(estimate).toContain('启发式估算');
    expect(estimate).not.toContain('供应商最近的输入记录为基线');
  });
  it('retains explicit zero but never displays invalid or absent numeric fields as zero', () => {
    const html = summary({ ...measured, total_tokens: 0, observed_prompt_tokens: null, breakdown: null });
    expect(html).toContain('<dd>0</dd>');
    expect(html.match(/<dd>0<\/dd>/g)).toHaveLength(1);
    for (const value of [NaN, Infinity, -1]) {
      const invalid = summary({ ...measured, total_tokens: value, observed_prompt_tokens: null, breakdown: null });
      expect(invalid).not.toContain('<dd>0</dd>');
      expect(invalid).not.toContain('<dd>NaN</dd>');
      expect(invalid).not.toContain('<dd>∞</dd>');
      expect(invalid).not.toContain('<dd>-1</dd>');
    }
  });
  it('preserves legacy Claude reporting and obeys explicit compaction capability', () => {
    const legacy: NativeContext = { ...measured, source: 'native_runtime', status: 'available', precision: 'native_reported', total_tokens: 100, max_tokens: 1000, percentage: 10, auto_compact_enabled: true, manual_compact_available: true };
    const html = summary(legacy);
    expect(html).toContain('原生统计可用');
    expect(html).toContain('已启用');
    expect(html).not.toContain('Harness');
    expect(contextCanCompact(legacy)).toBe(true);
    expect(contextCanCompact({ ...legacy, manual_compact_available: undefined })).toBe(true);
    expect(contextCanCompact(measured)).toBe(false);
    expect(contextCanCompact({ ...measured, manual_compact_available: undefined })).toBe(false);
    expect(contextCanCompact({ ...legacy, manual_compact_available: false })).toBe(false);
    expect(contextCanCompact(null)).toBe(false);
  });
  it('does not echo an unknown private model identity', () => {
    const html = summary({ ...measured, model: 'private-provider-route-secret' });
    expect(html).not.toContain('private-provider-route-secret');
    expect(html).toContain('模型信息暂不可用');
  });
});
