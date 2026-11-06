import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import BenchmarkPage, { serializeBenchmarkReport, syntheticStage0Benchmark } from './BenchmarkPage';

describe('synthetic benchmark evidence page', () => {
  it('renders measured chart values and keeps missing metrics as pending', () => {
    const html = renderToStaticMarkup(<BenchmarkPage admin/>);
    expect(html).toContain('合成夹具运行器');
    expect(html).toContain('任务正确性');
    expect(html).toContain('100%');
    expect(html).toContain('待测');
    expect(html).toContain('客户现有助手');
    expect(html).toContain('仅使用 synthetic-industrial-v1');
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
    expect(html).not.toContain('synthetic-industrial-v1');
  });

  it('exports only the validated synthetic report payload', () => {
    const output = serializeBenchmarkReport(syntheticStage0Benchmark);
    expect(JSON.parse(output)).toMatchObject({ synthetic: true, fixture_id: 'synthetic-industrial-v1', task_count: 4 });
    expect(output).toContain('not_measured');
    expect(output).not.toContain('customer-secret');
  });
});
