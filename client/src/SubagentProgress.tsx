import { ChevronRight, Circle, CircleCheck, CircleHelp, CirclePause, CircleX, Clock3, GitFork, ShieldCheck, Timer, TriangleAlert } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { AnswerMarkdown } from './Sources';
import { FailedChildFollowup } from './FailedChildFollowup';
import './subagent-progress.css';

export interface SubagentObservedUsage {
  input_tokens?: number | null;
  output_tokens?: number | null;
  input_units?: number | null;
  output_units?: number | null;
}

export interface SubagentChild {
  child_id: string;
  parent_run_id?: string;
  display_title?: string | null;
  role: string;
  status: string;
  stop_reason?: string | null;
  result?: { output?: unknown; stop_reason?: string | null } | null;
  observed_usage?: SubagentObservedUsage | null;
}

export interface SubagentPlan {
  plan_id: string;
  children: SubagentChild[];
  counts: Record<string, number>;
  mode?: string;
}

export interface SubagentProgressProps {
  plan: SubagentPlan;
  stale?: boolean;
  onRetry?: () => void;
}

const childStates = new Map<string, { label: string; icon: LucideIcon; tone?: string }>([
  ['queued', { label: '排队中', icon: Clock3 }],
  ['running', { label: '执行中', icon: Timer, tone: 'active' }],
  ['awaiting_approval', { label: '等待审批', icon: ShieldCheck, tone: 'attention' }],
  ['cancelling', { label: '正在取消', icon: CirclePause, tone: 'attention' }],
  ['succeeded', { label: '已完成', icon: CircleCheck, tone: 'success' }],
  ['failed', { label: '失败', icon: CircleX, tone: 'failure' }],
  ['cancelled', { label: '已取消', icon: Circle, tone: 'muted' }],
  ['interrupted', { label: '已中断', icon: TriangleAlert, tone: 'attention' }],
]);

const stopReasons = new Map([
  ['approval_denied', '审批未通过，任务已停止。'],
  ['parent_cancelled', '父任务取消后，子任务也已取消。'],
  ['cancelled', '子任务已取消。'],
  ['timeout', '子任务执行超时。'],
  ['max_tokens', '子任务达到模型输出上限。如有返回内容，仅为部分结果。'],
  ['executor_error', '执行子任务时出错。'],
  ['not_replayable', '子任务已中断，无法从原进度恢复。'],
  ['recovered_after_restart', '服务重启，子任务已中断。'],
]);

export function subagentTitle(child: SubagentChild, index: number) {
  if (child.display_title?.trim()) return child.display_title.trim();
  const label = child.role === 'harness_analysis' ? '分析任务' : child.role === 'synthetic_readonly' ? '本机验证' : '子任务';
  return `${label} ${index + 1}`;
}

export function isSubagentActive(status: string) {
  return ['queued', 'running', 'awaiting_approval', 'cancelling'].includes(status);
}

export function subagentStatus(status: string) {
  return childStates.get(status) ?? { label: '状态未确认', icon: CircleHelp, tone: undefined };
}

function planStatus(total: number, count: (status: string) => number) {
  if (count('cancelling')) return '正在取消';
  if (count('running')) return '执行中';
  if (count('awaiting_approval')) return '等待审批';
  if (count('queued')) return '排队中';
  const succeeded = count('succeeded');
  const failed = count('failed');
  const cancelled = count('cancelled');
  const interrupted = count('interrupted');
  if (succeeded === total) return '全部完成';
  if (succeeded + failed + cancelled + interrupted !== total) return '状态未确认';
  if (succeeded) return '部分完成';
  if (cancelled === total) return '已取消';
  if (interrupted === total) return '已中断';
  if (failed === total) return '执行失败';
  return '任务已结束';
}

function childNote(child: SubagentChild, hasOutput: boolean) {
  const reason = child.stop_reason || child.result?.stop_reason;
  if (reason && stopReasons.has(reason)) return stopReasons.get(reason);
  if (hasOutput) return null;
  if (isSubagentActive(child.status)) return '暂时没有结果。';
  return child.status === 'succeeded' ? '子任务已完成，未返回结果。' : '没有可查看的结果。';
}

function hasSubagentOutput(output: unknown) {
  return typeof output === 'string' ? Boolean(output.trim()) : output != null;
}

export function SubagentResult({ child, index }: { child: SubagentChild; index: number }) {
  const output = child.result?.output;
  const hasOutput = hasSubagentOutput(output);
  const note = childNote(child, hasOutput);
  const usage = child.observed_usage;
  const tokens = [usage?.input_tokens != null ? `输入 ${usage.input_tokens}` : '', usage?.output_tokens != null ? `输出 ${usage.output_tokens}` : ''].filter(Boolean);

  return <div className="subagent-progress-detail">
    {child.role === 'harness_analysis' && !child.display_title?.trim() && <p>这项委派没有可展示的主题。</p>}
    {note && <p>{note}</p>}
    {hasOutput && <div className="subagent-progress-result" role="group" aria-label="子任务结果" tabIndex={0}>
      {typeof output === 'string' ? <AnswerMarkdown text={output} mode="subagent"/> : <pre>{JSON.stringify(output, null, 2)}</pre>}
    </div>}
    {child.role === 'harness_analysis' && child.status === 'failed' && <FailedChildFollowup title={subagentTitle(child, index)} parentRunId={child.parent_run_id}/>}
    {!!tokens.length && <p className="subagent-progress-usage">{tokens.join(' · ')} token</p>}
  </div>;
}

function ChildDetails({ child, index }: { child: SubagentChild; index: number }) {
  const { icon: Icon, label, tone } = subagentStatus(child.status);
  return <li>
    <details className="subagent-progress-child">
      <summary tabIndex={0}>
        <Icon className="subagent-progress-status-icon" data-tone={tone} size={16} aria-hidden="true"/>
        <span className="subagent-progress-title">{subagentTitle(child, index)}</span>
        <span className="subagent-progress-child-status">{label}{hasSubagentOutput(child.result?.output) && ' · 有结果'}</span>
        <ChevronRight className="subagent-progress-chevron" size={15} aria-hidden="true"/>
      </summary>
      <SubagentResult child={child} index={index}/>
    </details>
  </li>;
}

function ChildList({ plan }: { plan: SubagentPlan }) {
  return <ul className="subagent-progress-children" key={plan.plan_id} role="list">
    {plan.children.map((child, index) => <ChildDetails key={child.child_id} child={child} index={index}/>)}
  </ul>;
}

function outcomes(children: SubagentChild[]) {
  // Older snapshots do not count every child status. Use the individual rows.
  const count = (status: string) => children.filter(child => child.status === status).length;
  const totals = [
    `${count('succeeded')}/${children.length} 项完成`,
    ...(['failed', 'cancelled', 'interrupted'] as const).flatMap(status => {
      const total = count(status);
      return total ? [`${total} 项${childStates.get(status)?.label}`] : [];
    }),
  ];
  return { status: planStatus(children.length, count), summary: totals.join(' · ') };
}

export function SubagentAnswerReceipt({ plan }: { plan?: SubagentPlan | null }) {
  if (!plan?.children.length || !plan.children.some(child => ['failed', 'cancelled', 'interrupted'].includes(child.status))
      || !plan.children.every(child => ['succeeded', 'failed', 'cancelled', 'interrupted'].includes(child.status))) return null;
  return <details className="subagent-answer-receipt" role="group" aria-label="子任务回执">
    <summary>子任务回执 · {outcomes(plan.children).summary}</summary>
    <ChildList plan={plan}/>
  </details>;
}

export default function SubagentProgress({ plan, stale = false, onRetry }: SubagentProgressProps) {
  if (!plan.children.length) return null;
  const outcome = outcomes(plan.children);

  return <section className="subagent-progress" aria-label="子任务进度">
    <header className="subagent-progress-header">
      <strong><GitFork size={16} aria-hidden="true"/>子任务进度</strong>
      <p role="status" className="subagent-progress-summary">
        <span>{outcome.status}</span><span>{outcome.summary}</span>
      </p>
    </header>
    {stale && <div className="subagent-progress-stale">
      <p role="status">刷新失败，显示上次读取的进度。</p>
      {onRetry && <button type="button" onClick={onRetry}>重新读取</button>}
    </div>}
    <ChildList plan={plan}/>
  </section>;
}
