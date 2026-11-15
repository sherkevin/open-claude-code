import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import BenchmarkPage, { serializeBenchmarkReport, syntheticStage0Benchmark } from './BenchmarkPage';

describe('synthetic benchmark evidence page', () => {
  it('renders measured chart values and keeps missing metrics as pending', () => {
    const html = renderToStaticMarkup(<BenchmarkPage admin/>);
    expect(html).toContain('离线基准运行器');
    expect(html).toContain('任务正确性');
    expect(html).toContain('100%');
    expect(html).toContain('待测');
    expect(html).toContain('客户现有助手');
    expect(html).toContain('仅使用 synthetic-ablation-industrial-v1');
    expect(html).toContain('最简单 Agent（客户现有助手的消融替身）');
    expect(html).toContain('上下文记忆能力（越高越好）');
    expect(html).toContain('模型响应速度（真实模型待测）');
    expect(html).toContain('knowledge_retrieval');
    expect(html).toContain('图片语义任务未测');
    expect(html).toContain('需要获批视觉模型和脱敏图片样例');
  });

  it('uses an honest empty state when no validated report is supplied', () => {
    const html = renderToStaticMarkup(<BenchmarkPage admin report={null}/>);
    expect(html).toContain('还没有可视化报告');
    expect(html).toContain('不会被估算');
    expect(html).not.toContain('100%');
  });

  it('does not expose the benchmark to non-admin users', () => {
    const html = renderToStaticMarkup(<BenchmarkPage admin={false}/>);
    expect(html).toContain('需要管理员权限');
    expect(html).not.toContain('对比键');
    expect(html).not.toContain('synthetic-ablation-industrial-v1');
  });

  it('exports only the validated synthetic report payload', () => {
    const output = serializeBenchmarkReport(syntheticStage0Benchmark);
    expect(JSON.parse(output)).toMatchObject({ synthetic: true, fixture_id: 'synthetic-ablation-industrial-v1', fixture_version: '1.1.0', task_count: 4, passed_tasks: 3 });
    expect(output).toContain('not_measured');
    expect(output).not.toContain('customer-secret');
  });

  it('shows compressed-context accounting instead of charging compression tokens', () => {
    const enhanced = syntheticStage0Benchmark.participants.find(item => item.id === 'enhanced-agent');
    const baseline = syntheticStage0Benchmark.participants.find(item => item.id === 'simple-agent-baseline');
    expect(enhanced?.metrics.find(metric => metric.id === 'tokens')?.value).toBeLessThan(baseline?.metrics.find(metric => metric.id === 'tokens')?.value ?? Infinity);
    expect(enhanced?.metrics.find(metric => metric.id === 'compression-savings')?.value).toBeGreaterThan(0);
    expect(renderToStaticMarkup(<BenchmarkPage admin/>)).toContain('压缩过程不计入');
  });
});
