import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import SubagentProgress, { type SubagentChild } from './SubagentProgress';

const child = (status: string, index: number): SubagentChild => ({
  child_id: `child-${index}`, role: 'harness_analysis', status, stop_reason: null,
});

function render(children: SubagentChild[], stale = false) {
  return renderToStaticMarkup(<SubagentProgress plan={{ plan_id: 'run-1', counts: {}, children }} stale={stale} onRetry={() => {}}/>);
}

describe('subagent progress', () => {
  it.each([
    [['queued'], '排队中'],
    [['running', 'succeeded'], '执行中'],
    [['awaiting_approval'], '等待审批'],
    [['cancelling', 'running'], '正在取消'],
    [['succeeded'], '全部完成'],
    [['failed'], '执行失败'],
    [['cancelled'], '已取消'],
    [['interrupted'], '已中断'],
    [['succeeded', 'failed', 'cancelled', 'interrupted'], '部分完成'],
    [['cancelled', 'failed'], '任务已结束'],
    [['other'], '状态未确认'],
  ] as const)('reports %j as %s in the live summary', (statuses, summary) => {
    expect(render(statuses.map(child))).toContain(`role="status" class="subagent-progress-summary"><span>${summary}</span>`);
  });

  it('shows names, counts, failure reasons and actual token data from older snapshots', () => {
    const html = render([
      { ...child('succeeded', 0), display_title: '核对订单', observed_usage: { input_tokens: null, output_tokens: 0 } },
      { ...child('failed', 1), display_title: null, stop_reason: 'approval_denied' },
      { ...child('cancelled', 2), role: 'synthetic_readonly', stop_reason: 'parent_cancelled' },
    ], true);
    expect(html).toContain('1/3 项完成 · 1 项失败 · 1 项已取消');
    expect(html).toContain('核对订单');
    expect(html).toContain('分析任务 2');
    expect(html).toContain('本机验证 3');
    expect(html).toContain('审批未通过，任务已停止。');
    expect(html).toContain('父任务取消后，子任务也已取消。');
    expect(html).toContain('输出 0 token');
    expect(html).toContain('刷新失败，显示上次读取的进度。');
    expect(html).toContain('<button type="button">重新读取</button>');
    expect(html.match(/<summary tabindex="0">/g)).toHaveLength(3);
  });

  it('marks only rows with a saved result and explains unnamed assignments', () => {
    const html = render([
      { ...child('succeeded', 0), display_title: '订单导出', result: { output: '2 份说明。' } },
      { ...child('running', 1), display_title: '发票预览' },
      { ...child('failed', 2), result: { output: '  ' }, stop_reason: 'executor_error' },
    ]);
    expect(html).toContain('订单导出');
    expect(html).toContain('已完成 · 有结果');
    expect(html.match(/ · 有结果/g)).toHaveLength(1);
    expect(html.match(/这项委派没有可展示的主题。/g)).toHaveLength(1);
    expect(html).toContain('执行子任务时出错。');
  });

  it('retains table and list markup and renders references and images as inert text', () => {
    const output = [
      '| 客户 | 订单 |\n| --- | --- |\n| 甲 | 8 |',
      '- 核对付款\n- 复查库存',
      '[来源](/api/v1/sources/example) [原网页](https://example.invalid/) https://example.invalid/bare',
      '![外部图片](https://example.invalid/pixel.png) ![附件图片](/api/v1/files/example/content)',
      '<img src="https://example.invalid/raw.png">',
    ].join('\n\n');
    const html = render([{ ...child('succeeded', 0), result: { output } }]);
    expect(html).toContain('<table>');
    expect(html).toContain('<th>订单</th>');
    expect(html).toContain('<li>复查库存</li>');
    expect(html).toContain('附件图片');
    expect(html).not.toMatch(/<(?:a|img)\b|\b(?:href|src)="|answer-sources|source-inline/);
  });

  it('shows output-limited children as interrupted with any partial text and reported tokens', () => {
    const usage = { input_tokens: 12, output_tokens: 800 };
    const html = render([
      { ...child('interrupted', 0), stop_reason: 'max_tokens', result: { output: '确认库存 8 件。' }, observed_usage: usage },
      { ...child('interrupted', 1), stop_reason: 'max_tokens', result: { output: '' }, observed_usage: usage },
    ]);
    expect(html).toContain('0/2 项完成 · 2 项已中断');
    expect(html.match(/子任务达到模型输出上限/g)).toHaveLength(2);
    expect(html.match(/输入 12 · 输出 800 token/g)).toHaveLength(2);
    expect(html).toContain('确认库存 8 件。');
    expect(html.match(/aria-label="子任务结果"/g)).toHaveLength(1);
  });

  it('renders actual structured output and omits empty plans', () => {
    expect(render([{ ...child('succeeded', 0), result: { output: { orders: 0 } } }])).toContain('&quot;orders&quot;: 0');
    expect(render([])).toBe('');
  });
});
