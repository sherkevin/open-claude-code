import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { delegatedTasks } from './ConversationTasks';
import { childFollowupDraft, FailedChildComposer } from './FailedChildFollowup';
import { SubagentResult, type SubagentChild } from './SubagentProgress';

describe('following up one failed assignment', () => {
  it('prepares a new question from a public topic and requirements entered by the member', () => {
    expect(childFollowupDraft('', '发票预览', '   ')).toBeNull();
    expect(childFollowupDraft('', '发票预览', '只核对格式。')).toBe('本轮请直接回答「发票预览」这一项。\n只核对格式。');
    const previous = ' 原有草稿。\n  ';
    expect(childFollowupDraft(previous, '分析任务 2', '核对日期。')).toBe(previous + '\n\n本轮请直接回答「分析任务 2」这一项。\n核对日期。');
    expect(childFollowupDraft('x'.repeat(50000), '发票预览', '保留原文')).toBeNull();
  });

  it('a transferred multiline draft is exactly one entry in the existing delegation form', () => {
    const request = '复核第一页。\n核对金额格式。';
    expect(delegatedTasks(request, true)).toEqual([request]);
    expect(delegatedTasks(request, false)).toEqual(['复核第一页。', '核对金额格式。']);
    expect(delegatedTasks('  ', true)).toEqual([]);
  });

  it('offers another question only on an authorized, failed Harness child', () => {
    const composer = { text: '', hasSelections: false, blocked: '', canDelegate: true, transfer: vi.fn(), write: vi.fn() };
    const child: SubagentChild = { child_id: 'child', role: 'harness_analysis', status: 'failed', stop_reason: 'executor_error' };
    const render = (value: SubagentChild, enabled: boolean) => renderToStaticMarkup(
      <FailedChildComposer.Provider value={enabled ? composer : null}><SubagentResult child={value} index={1}/></FailedChildComposer.Provider>);
    expect(render(child, true)).toContain('就此项另问');
    expect(render(child, true)).toContain('执行子任务时出错。');
    expect(render(child, false)).not.toContain('就此项另问');
    for (const status of ['succeeded', 'running', 'cancelled', 'interrupted']) {
      expect(render({ ...child, status }, true)).not.toContain('就此项另问');
    }
    expect(render({ ...child, role: 'synthetic_readonly' }, true)).not.toContain('就此项另问');
    expect(composer.transfer).not.toHaveBeenCalled();
    expect(composer.write).not.toHaveBeenCalled();
  });
});
