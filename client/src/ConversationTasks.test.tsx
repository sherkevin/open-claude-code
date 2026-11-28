import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ApiError } from './api';
import ConversationTasks, { childTaskRecords, PlanActionNotice, ScheduleRow } from './ConversationTasks';
import type { StreamEvent } from './types';

const at = '2026-11-23T01:00:00Z';
const event = (id: number, type: string, data: StreamEvent['data'], runId = 'run-1'): StreamEvent => ({
  id, type, run_id: runId, created_at: at, data,
});

describe('conversation task sidebar', () => {
  it('mounts as content in schedule and subtask order', () => {
    const html = renderToStaticMarkup(<ConversationTasks conversationId="conversation-1" canDelegate={false}
      onSummarize={() => {}}/>);
    expect(html).toContain('class="conversation-task-sidebar"');
    expect(html.indexOf('定时任务</h3>')).toBeLessThan(html.indexOf('子任务</h3>'));
    expect(html).toContain('aria-expanded="false"');
    expect(html).not.toMatch(/role="dialog"|modal-body/);
  });

  it('offers one-time, daily and weekly schedules with explicit Harness agent mode', () => {
    const html=renderToStaticMarkup(<ConversationTasks conversationId="conversation-1" canDelegate={false} canScheduleAgent onSummarize={()=>{}}/>);
    expect(html).toContain('仅一次');
    expect(html).toContain('每天');
    expect(html).toContain('每周');
    expect(html).toContain('智能体任务（工具、子任务）');
    expect(html).toContain('高级 · Cron');
    expect(html).toContain('高级 · RRULE');
    expect(html).toContain('沿用当前助手的权限和审批策略');
  });

  it('reads tool outcomes only for calls explicitly attributed to the selected child', () => {
    const result = childTaskRecords({ run: { id: 'run-1' }, events: [
      event(1, 'tool.started', { child_id: 'child-1', tool_call_id: 'read-1', tool_name: 'Read', arguments: { path: 'private' } }),
      event(2, 'tool.completed', { child_id: 'child-1', tool_call_id: 'read-1', success: false, result: 'private' }),
      event(3, 'tool.completed', { child_id: 'child-1', tool_call_id: 'other-run', success: true }, 'run-2'),
      event(4, 'tool.completed', { tool_call_id: 'parent', tool_name: 'parent-tool', success: true }),
      event(5, 'tool.completed', { child_id: 'child-2', tool_call_id: 'other-child', tool_name: 'sibling-tool', success: true }),
      event(6, 'tool.completed', { subagent_child_id: 'child-1', tool_call_id: 'read-2', tool_name: 'Glob' }),
    ] }, 'child-1');
    expect(result).toEqual({
      tools: [
        { id: 'read-1', name: 'Read', status: '调用失败', at },
        { id: 'read-2', name: 'Glob', status: '结果未确认', at },
      ],
      approvals: [],
    });
  });

  it('updates an open child detail when a successful result is withdrawn before delivery', () => {
    const known = childTaskRecords({run:{id:'run-1'},events:[
      event(2, 'tool.completed', {subagent_child_id:'c',tool_call_id:'call',tool_name:'Read',success:true}),
    ]}, 'c');
    const updated = childTaskRecords({run:{id:'run-1'},events:[
      event(3, 'tool.discarded', {subagent_child_id:'c',tool_call_id:'call',success:false}),
    ]}, 'c', known);
    expect(updated.tools).toEqual([{id:'call',name:'Read',status:'结果未交付',at}]);
  });

  it('keeps pending approval records and uses the final decision from historical events', () => {
    const result = childTaskRecords({ run: { id: 'run-1', approvals: [
      { id: 'still-pending', subagent_child_id: 'child-1', tool_name: 'Read', status: 'pending', created_at: at },
      { id: 'sibling', subagent_child_id: 'child-2', tool_name: 'Glob', status: 'pending' },
    ] }, events: [
      event(1, 'subagent.approval.required', { child_id: 'child-1', approval_id: 'old', approval: { tool_name: 'Grep', arguments: 'private' } }),
      event(2, 'subagent.approval.resolved', { child_id: 'child-1', approval_id: 'old', decision: 'deny', decision_source: 'timeout', decision_at: at }),
      event(3, 'subagent.approval.denied', { child_id: 'child-2', approval_id: 'sibling', decision: 'deny' }),
      event(4, 'subagent.approval.denied', { plan_id: 'old-plan', child_id: 'child-1', approval_id: 'wrong-plan' }),
      event(5, 'subagent.approval.resolved', { child_id: 'child-1', approval_id: 'legacy' }),
    ] }, 'child-1');
    expect(result.approvals).toEqual([
      { id: 'still-pending', name: 'Read', status: '等待审批', at },
      { id: 'old', name: 'Grep', status: '已过期', at },
      { id: 'legacy', name: undefined, status: '状态未确认', at },
    ]);
    expect(result.tools).toEqual([]);
  });
});

describe('recurring task lifecycle controls', () => {
  it('offers pause or resume only for active recurring series', () => {
    const fixture={id:'series-1',text:'周期检查',scheduled_at:at,status:'scheduled',recurrence:'daily'};
    const callbacks={busy:false,onCancel:()=>{},onLifecycle:()=>{}};
    expect(renderToStaticMarkup(<ScheduleRow task={fixture} {...callbacks}/>)).toContain('>暂停</button>');
    expect(renderToStaticMarkup(<ScheduleRow task={{...fixture,status:'paused'}} {...callbacks}/>)).toContain('>恢复</button>');
    expect(renderToStaticMarkup(<ScheduleRow task={{...fixture,recurrence:'once'}} {...callbacks}/>)).not.toContain('>暂停</button>');
    expect(renderToStaticMarkup(<ScheduleRow task={{...fixture,status:'exhausted'}} {...callbacks}/>)).not.toContain('>恢复</button>');
  });
});

describe('uncertain delegation receipts', () => {
  const lost = new ApiError('请检查网络后重试。', 0, 'NETWORK_ERROR');
  const child = { child_id: 'child-a', role: 'harness_analysis', status: 'running' };
  const plan = { plan_id: 'new-run', children: [child], counts: { running: 1 } };
  const render = (cause: unknown, snapshot?: typeof plan, action: 'start' | 'cancel' = 'start') =>
    renderToStaticMarkup(<PlanActionNotice issue={{ action, cause }} runId="new-run" snapshot={snapshot}/>);

  it('reports the confirmed number of children while leaving the submission unconfirmed', () => {
    const html = render(lost, plan);
    expect(html).toContain('本轮已读到 1 项子任务');
    expect(html).toContain('尚未确认哪些子项来自这次提交');
    expect(html).not.toContain('重试');
    expect(html).not.toContain('已启动');
  });

  it('directs the user to read the target round when the read failed, returned no rows or belongs elsewhere', () => {
    for (const snapshot of [undefined, { ...plan, children: [] }, { ...plan, plan_id: 'other-run' }]) {
      expect(render(lost, snapshot)).toContain('刷新子任务');
      expect(render(lost, snapshot)).not.toContain('1 项子任务');
    }
    expect(render(new Error('Malformed reply'), plan)).toContain('本次启动回执未收到');
    expect(render(new ApiError('网关失联', 504, 'GATEWAY_TIMEOUT'), plan)).toContain('本次启动回执未收到');
  });

  it('retains authoritative denials and the separate cancel error', () => {
    const denied = new ApiError('父任务已有不同的子任务计划', 409, 'SUBAGENT_PLAN_EXISTS');
    expect(render(denied, plan)).toContain(denied.message);
    expect(render(denied, plan)).not.toContain('回执未收到');
    expect(render(lost, plan, 'cancel')).toContain(lost.message);
  });
});
