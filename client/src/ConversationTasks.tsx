import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ArrowLeft, CalendarClock, ChevronRight, GitFork, Plus, RefreshCw } from 'lucide-react';
import { ApiError, api, errorText, post, remove, timeText } from './api';
import { Button, ErrorNotice, Field } from './components';
import ScheduleEditor from './ScheduleEditor';
import ScheduleRunNow from './ScheduleRunNow';
import { toolLabel } from './display';
import { isSubagentActive, SubagentResult, subagentStatus, subagentTitle } from './SubagentProgress';
import type { SubagentChild, SubagentPlan } from './SubagentProgress';
import type { StreamEvent } from './types';
import './conversation-tasks.css';

export interface ConversationTasksProps {
  conversationId: string;
  runId?: string;
  canDelegate: boolean;
  onSummarize: (results: string) => void;
  events?: StreamEvent[];
  canSchedule?: boolean;
  canScheduleAgent?: boolean;
}

export type Schedule = { id: string; text: string; scheduled_at: string; status: string; error?: string | null; recurrence?: string; execution_mode?: string; series_id?: string; skipped_count?: number; rule_expression?: string; coalesced_batches?: number; timezone?: string; edit_token?: string; manual_at?: string|null };
type PlanRun = { id: string; created_at: string; status: string };
type ChildSelection = { plan: SubagentPlan; childId: string };
type SelectChild = (selection: ChildSelection, trigger: HTMLButtonElement) => void;
type TaskSnapshot<T> = { path: string; value?: T; error?: string; accessLost?: boolean };
type ReadOptions<T> = {
  enabled?: boolean;
  refreshKey?: string;
  read?: (path: string, signal: AbortSignal) => Promise<T>;
  nextRead?: (value: T) => number | undefined;
};

const statusLabels = new Map([
  ['scheduled', '等待到期'], ['paused','已暂停'], ['exhausted','周期规则已结束'], ['pending', '待发送'], ['queued', '排队中'], ['running', '执行中'], ['cancelling', '正在取消'],
  ['awaiting_approval', '等待审批'], ['succeeded', '已完成'], ['failed', '失败'], ['cancelled', '已取消'], ['interrupted', '已中断'],
]);
const statusLabel = (status: string) => statusLabels.get(status) ?? '状态未确认';
export const canCancelSchedule = (status: string) => status === 'scheduled' || status === 'pending' || status === 'paused';

function fetchTask<T>(path: string, signal: AbortSignal) {
  return api<T>(path, { signal, cache: 'no-store' });
}

function useTaskRead<T>(path: string | undefined, { enabled = true, refreshKey, read = fetchTask, nextRead }: ReadOptions<T> = {}) {
  const [state, setState] = useState<TaskSnapshot<T>>();
  const [revision, setRevision] = useState(0);
  const stopRef = useRef<() => void>();
  const currentPath = useRef(path);
  currentPath.current = path;
  const snapshot = state?.path === path ? state : undefined;

  function refresh() {
    if (currentPath.current !== path) return;
    stopRef.current?.();
    setRevision(value => value + 1);
  }

  function update(change: (previous: T | undefined) => T | undefined) {
    if (!path || currentPath.current !== path) return;
    setState(previous => ({ path, value: change(previous?.path === path ? previous.value : undefined) }));
  }

  useEffect(() => {
    if (!path || !enabled) return;
    const route = path;
    let request: AbortController | undefined;
    let timer: number | undefined;

    function stop() {
      request?.abort();
      window.clearTimeout(timer);
    }

    async function load() {
      if (document.hidden) return;
      request = new AbortController();
      const { signal } = request;
      try {
        const value = await read(route, signal);
        if (signal.aborted) return;
        setState({ path: route, value });
        const delay = nextRead?.(value);
        if (delay !== undefined) timer = window.setTimeout(load, delay);
      } catch (failure) {
        if (signal.aborted) return;
        const accessLost = failure instanceof ApiError && [401, 403, 404].includes(failure.status);
        setState(previous => ({
          path: route,
          value: !accessLost && previous?.path === route ? previous.value : undefined,
          error: errorText(failure),
          accessLost,
        }));
      }
    }

    function visibilityChanged() {
      stop();
      void load();
    }

    stopRef.current = stop;
    document.addEventListener('visibilitychange', visibilityChanged);
    void load();
    return () => {
      stop();
      stopRef.current = undefined;
      document.removeEventListener('visibilitychange', visibilityChanged);
    };
  }, [path, enabled, refreshKey, revision, read, nextRead]);

  return { ...snapshot, refresh, update };
}

export function scheduleInterval(schedules: Schedule[]) {
  const now = Date.now();
  let delay = Infinity;
  for (const task of schedules) {
    if (task.status === 'scheduled') {
      const due = Date.parse(task.scheduled_at);
      if (Number.isFinite(due)) delay = Math.min(delay, due > now ? due - now + 1000 : 10_000);
    } else if (task.status === 'pending' || isSubagentActive(task.status)) {
      delay = Math.min(delay, 8000);
    }
  }
  return Number.isFinite(delay) ? Math.min(delay, 2_147_483_647) : undefined;
}

function ReadError({ error, hasValue }: { error?: string; hasValue?: boolean }) {
  return error ? <p className="task-sidebar-read-error" role="status">
    {hasValue ? '刷新失败，显示上次读取的记录。' : '暂时无法读取。'} {error}
  </p> : null;
}

function RefreshButton({ label, onClick }: { label: string; onClick: () => void }) {
  return <Button variant="ghost" className="task-sidebar-icon-button" aria-label={label} title={label} onClick={onClick}>
    <RefreshCw size={14} aria-hidden="true"/>
  </Button>;
}

export function scheduleLifecycleConfirmation(action: "pause"|"resume") {
  return action==="pause"?"暂停未来周期，已经进入队列或启动的任务不受影响。":"恢复后只安排未来周期，不补跑、不重试上次任务。若上次失败，请先确认可能产生的影响。确认恢复？";
}
export function changeScheduleLifecycle(path:string,id:string,action:"pause"|"resume") {
  return post<Pick<Schedule,"id"|"status"|"scheduled_at">>(path+"/"+encodeURIComponent(id)+"/"+action,action==="resume"?{acknowledge_failure:true}:{});
}

export function ScheduleRow({ task, busy, onCancel, onLifecycle, onEdit, onRunNow }: { task: Schedule; busy: boolean; onCancel: (id: string) => void; onLifecycle: (task: Schedule, action: "pause"|"resume") => void; onEdit?: (task:Schedule)=>void; onRunNow?: (task:Schedule)=>void }) {
  const title = task.text.trim().split('\n', 1)[0];
  const preview = title.length > 120 ? `${title.slice(0, 120)}…` : title;
  return <li className="task-sidebar-schedule-row">
    <details className="task-sidebar-schedule">
      <summary tabIndex={0}>
        <span>
          <span className="task-sidebar-schedule-text">{preview}</span>
          <small><time dateTime={task.scheduled_at}>{timeText(task.scheduled_at)}</time> · {statusLabel(task.status)}{task.recurrence==='daily'?' · 每天':task.recurrence==='weekly'?' · 每周':task.recurrence==='cron'?' · Cron':task.recurrence==='rrule'?' · RRULE':''}{task.execution_mode==='agent'?' · 智能体':''}</small>
        </span>
        <ChevronRight size={15} aria-hidden="true"/>
      </summary>
      <p>{task.text}</p>{!!task.skipped_count&&<p className="section-help">已跳过 {task.skipped_count} 个错过或重叠的周期。</p>}
      {task.rule_expression&&<pre style={{whiteSpace:"pre-wrap",overflowWrap:"anywhere"}}>{task.rule_expression}</pre>}{!!task.coalesced_batches&&<p className="section-help">已合并 {task.coalesced_batches} 批错过的周期（仅补最近一次）。</p>}
      {task.manual_at&&<p className="section-help">手动立即执行：{timeText(task.manual_at)}</p>}
      {task.error && <p className="task-sidebar-read-error">{task.error}</p>}
    </details>
    {task.edit_token&&task.status==="scheduled"&&!task.series_id&&onRunNow&&<Button variant="ghost" disabled={busy} onClick={()=>onRunNow(task)}>立即执行</Button>}
    {task.edit_token&&onEdit&&<Button variant="ghost" disabled={busy} onClick={()=>onEdit(task)}>编辑</Button>}
    {task.recurrence && task.recurrence!=="once" && ["scheduled","paused"].includes(task.status) &&
      <Button variant="ghost" disabled={busy} onClick={()=>onLifecycle(task,task.status==="paused"?"resume":"pause")}>{task.status==="paused"?"恢复":"暂停"}</Button>}
    {canCancelSchedule(task.status) && <Button variant="ghost" disabled={busy} aria-label={`取消定时任务 ${preview}`} onClick={() => onCancel(task.id)}>取消</Button>}
  </li>;
}

function ScheduledTasks({ path, visible, canSchedule, canScheduleAgent }: { path: string; visible: boolean; canSchedule: boolean; canScheduleAgent: boolean }) {
  const heading = useId();
  const formId = useId();
  const { value: schedules, error, refresh, update } = useTaskRead<Schedule[]>(path, { enabled: visible, nextRead: scheduleInterval });
  const [creating, setCreating] = useState(false);
  const [text, setText] = useState('');
  const [when, setWhen] = useState('');
  const [recurrence,setRecurrence]=useState<'once'|'daily'|'weekly'|'cron'|'rrule'>('once');
  const [ruleExpression,setRuleExpression]=useState('');
  const advanced=recurrence==='cron'||recurrence==='rrule';
  const [zone,setZone]=useState(()=>Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC');
  const [executionMode,setExecutionMode]=useState<'text'|'agent'>(canScheduleAgent?'agent':'text');
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const [editing,setEditing]=useState<Schedule|null>(null);
  const [runningNow,setRunningNow]=useState<Schedule|null>(null);
  const createButton = useRef<HTMLButtonElement>(null);
  const textInput = useRef<HTMLTextAreaElement>(null);
  const returnToCreate = useRef(false);

  useEffect(() => {
    if (creating) textInput.current?.focus();
    else if (returnToCreate.current) {
      createButton.current?.focus();
      returnToCreate.current = false;
    }
  }, [creating]);

  async function act(action: () => Promise<void>) {
    setBusy(true);
    setActionError('');
    try { await action(); }
    catch (failure) { setActionError(errorText(failure)); }
    finally { setBusy(false); refresh(); }
  }

  function schedule(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = text.trim();
    const date = new Date(when);
    if (!canSchedule || busy || !content) return;
    if (!advanced && (!Number.isFinite(date.getTime()) || date.getTime() <= Date.now())) {
      setActionError('请选择未来的执行时间。');
      return;
    }
    void act(async () => {
      const timing=advanced?{rule_expression:ruleExpression.trim()}:{scheduled_at:date.toISOString()};
      const task = await post<Pick<Schedule, 'id' | 'status' | 'scheduled_at' | 'recurrence' | 'execution_mode'>>(path, {text:content,...timing,recurrence,execution_mode:canScheduleAgent?executionMode:'text',timezone:advanced?zone:Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC'});
      update(previous => [{ ...task, text:content, scheduled_at:task.scheduled_at??(!advanced?date.toISOString():''), ...(advanced?{rule_expression:ruleExpression.trim(),timezone:zone}:{}) }, ...(previous ?? []).filter(item => item.id !== task.id)]);
      setRuleExpression('');
      setText('');
      setWhen('');
      returnToCreate.current = true;
      setCreating(false);
    });
  }

  function cancel(id: string) {
    if (busy) return;
    void act(async () => {
      await remove(`${path}/${encodeURIComponent(id)}`);
      update(previous => previous?.map(task => task.id === id ? { ...task, status: 'cancelled' } : task));
    });
  }

  function lifecycle(task: Schedule, action: "pause"|"resume") {
    if (busy || !window.confirm(scheduleLifecycleConfirmation(action))) return;
    void act(async () => {
      const result=await changeScheduleLifecycle(path,task.id,action);
      update(previous=>previous?.map(item=>item.id===task.id?{...item,...result,error:action==="resume"?null:item.error}:item));
    });
  }

  const upcoming = schedules?.filter(task => canCancelSchedule(task.status) || isSubagentActive(task.status)) ?? [];
  const history = schedules?.filter(task => !canCancelSchedule(task.status) && !isSubagentActive(task.status)) ?? [];

  return <section className="task-sidebar-section" aria-labelledby={heading}>
    <header className="task-sidebar-heading">
      <h3 id={heading}><CalendarClock size={16} aria-hidden="true"/>定时任务</h3>
      <div className="task-sidebar-actions">
        <button type="button" className="button ghost" ref={createButton} aria-expanded={creating} aria-controls={formId}
          disabled={busy || !canSchedule} title={canSchedule ? undefined : '当前会话无法安排新任务'} onClick={() => { setCreating(value => !value); setActionError(''); }}>
          <Plus size={15} aria-hidden="true"/>新建
        </button>
        <RefreshButton label="刷新定时任务" onClick={refresh}/>
      </div>
    </header>
    <ReadError error={error} hasValue={!!schedules}/>
    {runningNow&&<ScheduleRunNow task={runningNow} path={path} onClose={()=>{setRunningNow(null);refresh();}} onQueued={refresh}/>}
    {editing&&<ScheduleEditor task={editing} path={path} onClose={()=>setEditing(null)} onSaved={()=>{setEditing(null);refresh();}}/>}
    <ErrorNotice message={actionError}/>
    <div id={formId} hidden={!creating}>
      <form className="task-sidebar-form" onSubmit={schedule}>
        <Field label="任务内容"><textarea ref={textInput} value={text} onChange={event => setText(event.target.value)} required maxLength={50000} rows={2}/></Field>
        <Field label="执行频率"><select value={recurrence} onChange={e=>setRecurrence(e.target.value as typeof recurrence)}><option value="once">仅一次</option><option value="daily">每天</option><option value="weekly">每周</option>{canScheduleAgent&&<><option value="cron">高级 · Cron</option><option value="rrule">高级 · RRULE</option></>}</select></Field>
        {!advanced?<Field label="执行时间（本地时间）"><input value={when} onChange={event => setWhen(event.target.value)} type="datetime-local" required/></Field>:<>
          <Field label="时区"><input value={zone} onChange={e=>setZone(e.target.value)} maxLength={100} required placeholder="Asia/Shanghai"/></Field>
          <Field label={recurrence==='cron'?'Cron 规则':'RRULE 规则'} hint={recurrence==='cron'?'五段表达式，如 0 9 * * 1-5（工作日 9 点）。':'两行 DTSTART 与 RRULE；实际时区由 DTSTART 指定。'}><textarea rows={3} value={ruleExpression} onChange={e=>setRuleExpression(e.target.value)} maxLength={2048} required placeholder={recurrence==='cron'?'0 9 * * 1-5':'DTSTART;TZID=Asia/Shanghai:20261201T090000\nRRULE:FREQ=MONTHLY;BYMONTHDAY=1'}/></Field>
          <p className="task-sidebar-muted">由官方 Harness 规则计算首次执行时间，创建后查看实际时间。</p>
        </>}
        {canScheduleAgent&&<Field label="执行方式"><select value={executionMode} onChange={e=>setExecutionMode(e.target.value as 'text'|'agent')}><option value="agent">智能体任务（工具、子任务）</option><option value="text">仅文字回答</option></select></Field>}
        <p className="task-sidebar-muted">到期进入会话队列，沿用当前助手的权限和审批策略。周期任务不会堆积重复执行，失败后暂停。取消周期不影响已经启动的执行。</p>
        <Button type="submit" busy={busy} disabled={!text.trim() || (advanced?!ruleExpression.trim()||!zone.trim():!when)}>设定时间</Button>
      </form>
    </div>
    {!schedules && !error && <p className="task-sidebar-muted" role="status">正在读取定时任务…</p>}
    {schedules?.length === 0 && <p className="task-sidebar-empty">暂无定时任务。<br/>可安排一条消息在指定时间执行。</p>}
    {!!upcoming.length && <ul className="task-sidebar-list">
      {upcoming.map(task => <ScheduleRow key={task.id} task={task} busy={busy} onCancel={cancel} onLifecycle={lifecycle} onEdit={canSchedule?setEditing:undefined} onRunNow={canSchedule?setRunningNow:undefined}/>)}
    </ul>}
    {!!history.length && <details className="task-sidebar-history">
      <summary tabIndex={0}><ChevronRight size={15} aria-hidden="true"/>历史定时任务 <small>{history.length} 条</small></summary>
      <ul className="task-sidebar-list">
        {history.map(task => <ScheduleRow key={task.id} task={task} busy={busy} onCancel={cancel} onLifecycle={lifecycle} onEdit={canSchedule?setEditing:undefined} onRunNow={canSchedule?setRunningNow:undefined}/>)}
      </ul>
    </details>}
  </section>;
}

function checkedPlan(plan: SubagentPlan, runId: string) {
  if (plan?.plan_id !== runId || !Array.isArray(plan.children)) throw new Error('未读到所选轮次的子任务。');
  return plan;
}

const planPath = (runId: string) => `/runs/${encodeURIComponent(runId)}/subagents`;
async function readPlan(path: string, signal: AbortSignal) {
  const runId = decodeURIComponent(path.split('/')[2]);
  return checkedPlan(await fetchTask<SubagentPlan>(path, signal), runId);
}

const activePlanInterval = (plan: SubagentPlan) => plan.children.some(child => isSubagentActive(child.status)) ? 3000 : undefined;
const currentPlanInterval = (plan: SubagentPlan) => plan.children.length ? activePlanInterval(plan) : 5000;

function usePlan(runId: string | undefined, canDelegate = false, enabled = true) {
  return useTaskRead<SubagentPlan>(runId ? planPath(runId) : undefined, {
    enabled, read: readPlan, nextRead: canDelegate ? currentPlanInterval : activePlanInterval,
  });
}

type PlanActionsProps = {
  runId: string;
  plan: SubagentPlan;
  canDelegate: boolean;
  onSummarize: ConversationTasksProps['onSummarize'];
  onChange: (next?: SubagentPlan) => void;
};

function PlanActions({ runId, plan, canDelegate, onSummarize, onChange }: PlanActionsProps) {
  const formId = useId();
  const [creating, setCreating] = useState(false);
  const [delegatedText, setDelegatedText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const input = useRef<HTMLTextAreaElement>(null);
  const tasks = delegatedText.split('\n').map(task => task.trim()).filter(Boolean);
  const validTasks = tasks.length > 0 && tasks.length <= 4 && tasks.every(task => task.length <= 2000);
  const canCancel = plan.children.some(child => isSubagentActive(child.status) && child.status !== 'cancelling');
  const completed = plan.children.flatMap((child, index) => {
    const output = child.result?.output;
    return child.status === 'succeeded' && typeof output === 'string' && output.trim() ? [`${subagentTitle(child, index)}\n${output}`] : [];
  });

  useEffect(() => { if (creating && canDelegate) input.current?.focus(); }, [creating, canDelegate]);

  async function changePlan(action: 'start' | 'cancel') {
    if (busy || (action === 'start' && (!canDelegate || !validTasks)) || (action === 'cancel' && !canCancel)) return;
    setBusy(true);
    setError('');
    let next: SubagentPlan | undefined;
    try {
      const path = planPath(runId);
      next = checkedPlan(action === 'start'
        ? await post<SubagentPlan>(path, { mode: 'harness', tasks })
        : await post<SubagentPlan>(`${path}/cancel`), runId);
      if (action === 'start') {
        setCreating(false);
        setDelegatedText('');
      }
    } catch (failure) { setError(errorText(failure)); }
    finally { setBusy(false); onChange(next); }
  }

  const showCreate = canDelegate && !plan.children.length;
  return <>
    <ErrorNotice message={error}/>
    {(completed.length > 0 || canCancel || showCreate) && <div className="task-sidebar-actions">
      {!!completed.length && <Button variant="secondary" onClick={() => onSummarize(completed.join('\n\n'))}>用结果起草汇总</Button>}
      {canCancel && <Button variant="secondary" busy={busy} onClick={() => void changePlan('cancel')}>取消子任务</Button>}
      {showCreate && <Button variant="ghost" aria-expanded={creating} aria-controls={formId} onClick={() => setCreating(value => !value)}>
        <Plus size={15} aria-hidden="true"/>分配子任务
      </Button>}
    </div>}
    {showCreate && <div id={formId} hidden={!creating}>
      <form className="task-sidebar-form" onSubmit={event => { event.preventDefault(); void changePlan('start'); }}>
        <Field label="分配文字分析"><textarea ref={input} value={delegatedText} onChange={event => setDelegatedText(event.target.value)}
          placeholder="每行一项，最多四项" rows={2} maxLength={8004}/></Field>
        <Button type="submit" variant="secondary" busy={busy} disabled={!validTasks}>启动子任务</Button>
      </form>
    </div>}
  </>;
}

function ChildRows({ plan, onSelect }: { plan: SubagentPlan; onSelect: SelectChild }) {
  if (!plan.children.length) return null;
  return <ul className="task-sidebar-list">
    {plan.children.map((child, index) => {
      const { icon: Icon, label, tone } = subagentStatus(child.status);
      return <li key={child.child_id}>
        <button type="button" className="task-sidebar-child-row" onClick={event => onSelect({ plan, childId: child.child_id }, event.currentTarget)}>
          <Icon className="subagent-progress-status-icon" data-tone={tone} size={16} aria-hidden="true"/>
          <span>{subagentTitle(child, index)}</span>
          <small>{label}</small><ChevronRight size={15} aria-hidden="true"/>
        </button>
      </li>;
    })}
  </ul>;
}

type SubagentTasksProps = Pick<ConversationTasksProps, 'runId' | 'canDelegate' | 'onSummarize'> & {
  path: string;
  visible: boolean;
  heading: string;
  onSelect: SelectChild;
};

function SubagentTasks({ path, runId, canDelegate, onSummarize, visible, heading, onSelect }: SubagentTasksProps) {
  const history = useTaskRead<PlanRun[]>(path, { refreshKey: runId, enabled: visible });
  const current = usePlan(runId, canDelegate, visible);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [chosenRun, setChosenRun] = useState('');
  const pastRuns = history.value?.filter(item => item.id !== runId) ?? [];
  const pastRunId = pastRuns.find(item => item.id === chosenRun)?.id ?? pastRuns[0]?.id;
  const past = usePlan(historyOpen ? pastRunId : undefined, false, visible);

  function refresh() {
    history.refresh();
    current.refresh();
    past.refresh();
  }

  function planChanged(target: typeof current, next?: SubagentPlan) {
    if (next) target.update(previous => ({
      ...next,
      children: next.children.map(child => ({
        ...child, display_title: child.display_title ?? previous?.children.find(item => item.child_id === child.child_id)?.display_title,
      })),
    }));
    target.refresh();
    history.refresh();
  }

  return <section className="task-sidebar-section" aria-labelledby={heading}>
    <header className="task-sidebar-heading">
      <h3 id={heading} tabIndex={-1}><GitFork size={16} aria-hidden="true"/>子任务</h3>
      <RefreshButton label="刷新子任务" onClick={refresh}/>
    </header>
    <ReadError error={history.error} hasValue={!!history.value}/>
    <ReadError error={current.error} hasValue={!!current.value}/>
    {((runId && !current.value && !current.error) || (!runId && !history.value && !history.error)) &&
      <p className="task-sidebar-muted" role="status">正在读取子任务…</p>}
    {(current.value?.children.length === 0 || (!runId && history.value && !pastRuns.length)) &&
      <p className="task-sidebar-empty">
        {runId ? '当前轮次暂无子任务。' : '暂无子任务。'}
        {!pastRuns.length && !canDelegate && <><br/>助手分派后会显示在这里。</>}
      </p>}
    {current.value && runId && <>
      <ChildRows plan={current.value} onSelect={onSelect}/>
      <PlanActions key={runId} runId={runId} plan={current.value} canDelegate={canDelegate && !current.error}
        onSummarize={onSummarize} onChange={next => planChanged(current, next)}/>
    </>}
    {pastRunId && <details className="task-sidebar-history" open={historyOpen} onToggle={event => setHistoryOpen(event.currentTarget.open)}>
      <summary tabIndex={0}><ChevronRight size={15} aria-hidden="true"/>历史子任务 <small>{pastRuns.length} 轮</small></summary>
      {historyOpen && <>
        {pastRuns.length > 1 ? <Field label="查看轮次">
          <select value={pastRunId} onChange={event => setChosenRun(event.target.value)}>
            {pastRuns.map(item => <option key={item.id} value={item.id}>{timeText(item.created_at)} · {statusLabel(item.status)}</option>)}
          </select>
        </Field> : <p className="task-sidebar-muted">{timeText(pastRuns[0].created_at)} · {statusLabel(pastRuns[0].status)}</p>}
        <ReadError error={past.error} hasValue={!!past.value}/>
        {!past.value && !past.error && <p className="task-sidebar-muted" role="status">正在读取本轮子任务…</p>}
        {past.value && <>
          <ChildRows plan={past.value} onSelect={onSelect}/>
          {!past.value.children.length && <p className="task-sidebar-muted">本轮没有可查看的子任务。</p>}
          <PlanActions key={pastRunId} runId={pastRunId} plan={past.value} canDelegate={false}
            onSummarize={onSummarize} onChange={next => planChanged(past, next)}/>
        </>}
      </>}
    </details>}
  </section>;
}

type TraceApproval = {
  id: string;
  subagent_child_id?: string | null;
  tool_name?: string;
  status?: string;
  created_at?: string;
  decided_at?: string | null;
};
type TaskTrace = { run: { id: string; approvals?: TraceApproval[] }; events: StreamEvent[] };
type TaskRecord = { id: string; name?: string; status: string; at?: string };

const publicString = (value: unknown) => typeof value === 'string' && value.trim() ? value.trim() : undefined;
const approvalStatuses = new Map([['pending', '等待审批'], ['allowed', '已允许'], ['denied', '已拒绝'], ['expired', '已过期']]);
const approvalDecisions = new Map([['timeout', '已过期'], ['parent_cancelled', '已取消'], ['cancelled', '已取消']]);

type ChildRecords = { tools: TaskRecord[]; approvals: TaskRecord[] };
export function childTaskRecords(trace: TaskTrace, childId: string, known?: ChildRecords): ChildRecords {
  const tools = new Map(known?.tools.map(record => [record.id, record]));
  const approvals = new Map(known?.approvals.map(record => [record.id, record]));
  for (const approval of trace.run.approvals ?? []) {
    if (approval.subagent_child_id !== childId) continue;
    approvals.set(approval.id, {
      id: approval.id, name: publicString(approval.tool_name), at: approval.decided_at || approval.created_at,
      status: approvalStatuses.get(approval.status ?? '') ?? '状态未确认',
    });
  }
  for (const event of trace.events) {
    if (event.run_id !== trace.run.id || !event.data) continue;
    const { data, type } = event;
    if ((data.subagent_child_id ?? data.child_id) !== childId || (data.plan_id && data.plan_id !== trace.run.id)) continue;
    if (['tool.started', 'tool.dispatched', 'tool.completed', 'tool.discarded'].includes(type)) {
      const id = publicString(data.tool_call_id) ?? String(event.id);
      const name = publicString(data.tool_name) ?? tools.get(id)?.name;
      const status = type === 'tool.completed'
        ? data.success === true ? '调用成功' : data.success === false ? '调用失败' : '结果未确认'
        : type === 'tool.discarded' ? '结果未交付' : type === 'tool.dispatched' ? '已派发' : '已发起';
      tools.set(id, { id, name, status, at: event.created_at });
    }
    if (['subagent.approval.required', 'subagent.approval.denied', 'subagent.approval.resolved'].includes(type)) {
      const id = publicString(data.approval_id);
      if (!id) continue;
      const previous = approvals.get(id);
      const request = data.approval as TraceApproval | undefined;
      const name = previous?.name ?? publicString(request?.tool_name);
      const ended = approvalDecisions.get(publicString(data.decision_source) ?? '');
      const decision = data.decision === 'allow' ? '已允许' : data.decision === 'deny' || type === 'subagent.approval.denied' ? '已拒绝' : '状态未确认';
      const status = type === 'subagent.approval.required' ? '等待审批' : ended ?? decision;
      approvals.set(id, { id, name, status, at: publicString(data.decision_at) ?? event.created_at });
    }
  }
  return { tools: [...tools.values()], approvals: [...approvals.values()] };
}

async function readTrace(path: string, signal: AbortSignal) {
  const trace = await fetchTask<TaskTrace>(path, signal);
  if (trace?.run?.id !== decodeURIComponent(path.split('/')[2]) || !Array.isArray(trace.events)) throw new Error('未读到本轮公开记录。');
  return trace;
}

function RecordList({ rows, fallback }: { rows: TaskRecord[]; fallback: string }) {
  return <ul className="task-sidebar-list">
    {rows.map(record => <li className="task-sidebar-record" key={record.id}>
      <span>{record.name ? toolLabel(record.name) : fallback}</span>
      <small>{record.status}
        {record.at && Number.isFinite(Date.parse(record.at)) && <> · <time dateTime={record.at}>{timeText(record.at)}</time></>}
      </small>
    </li>)}
  </ul>;
}

function ChildTaskDetail({ selection, events, onBack }: { selection: ChildSelection; events?: StreamEvent[]; onBack: () => void }) {
  const heading = useId();
  const titleRef = useRef<HTMLHeadingElement>(null);
  const { plan_id: runId } = selection.plan;
  const plan = usePlan(runId);
  const children = plan.value?.children ?? (plan.accessLost ? [] : selection.plan.children);
  const index = children.findIndex(task => task.child_id === selection.childId);
  const child: SubagentChild | undefined = children[index];
  const readRecords = useCallback(async (path: string, signal: AbortSignal) => {
    const trace = await readTrace(path, signal);
    return { through: trace.events.reduce((max, event) => Math.max(max, event.id), 0), records: childTaskRecords(trace, selection.childId) };
  }, [selection.childId]);
  const trace = useTaskRead(`/runs/${encodeURIComponent(runId)}/trace`, {
    enabled: Boolean(child), read: readRecords, refreshKey: child?.status,
  });
  const records = useMemo(() => {
    if (!child || !trace.value) return null;
    const { through, records: saved } = trace.value;
    const recent = events?.filter(event => event.run_id === runId && event.id > through);
    return recent?.length ? childTaskRecords({ run: { id: runId }, events: recent }, child.child_id, saved) : saved;
  }, [child, events, runId, trace.value]);
  const status = child && subagentStatus(child.status);
  const Icon = status?.icon;

  useEffect(() => { titleRef.current?.focus(); }, []);

  return <section className="task-sidebar-detail" aria-labelledby={heading} onKeyDown={event => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    event.preventDefault();
    event.stopPropagation();
    onBack();
  }}>
    <header className="task-sidebar-heading">
      <Button variant="ghost" onClick={onBack}><ArrowLeft size={16} aria-hidden="true"/>返回任务列表</Button>
      <RefreshButton label="刷新子任务详情" onClick={() => { plan.refresh(); trace.refresh(); }}/>
    </header>
    <h3 id={heading} ref={titleRef} tabIndex={-1}>{child ? subagentTitle(child, index) : '子任务详情'}</h3>
    {status && Icon && <p className="task-sidebar-detail-status" role="status">
      <Icon className="subagent-progress-status-icon" data-tone={status.tone} size={16} aria-hidden="true"/>{status.label}
    </p>}
    <ReadError error={plan.error} hasValue={!!child}/>
    {child ? <div className="task-sidebar-detail-block">
      <h4>结果</h4>
      <SubagentResult child={child}/>
    </div> : <p className="task-sidebar-muted">找不到这项子任务。</p>}
    {child && <ReadError error={trace.error} hasValue={!!records}/>}
    {child && !records && !trace.error && <p className="task-sidebar-muted" role="status">正在读取工具与审批记录…</p>}
    {records && <>
      <div className="task-sidebar-detail-block">
        <h4>工具调用</h4>
        {records.tools.length ? <RecordList rows={records.tools} fallback="工具调用"/> : <p className="task-sidebar-muted">没有公开的工具调用记录。</p>}
      </div>
      <div className="task-sidebar-detail-block">
        <h4>审批记录</h4>
        {records.approvals.length ? <RecordList rows={records.approvals} fallback="子任务审批"/> : <p className="task-sidebar-muted">没有公开的审批记录。</p>}
      </div>
    </>}
  </section>;
}

function ConversationTaskBody({ conversationId, runId, canDelegate, canSchedule = true, canScheduleAgent = false, events, onSummarize }: ConversationTasksProps) {
  const heading = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const [selection, setSelection] = useState<ChildSelection | null>(null);
  const path = `/conversations/${encodeURIComponent(conversationId)}`;

  useEffect(() => {
    if (selection || !trigger.current) return;
    const fallback = root.current?.querySelector<HTMLElement>('.task-sidebar-section h3[tabindex="-1"]');
    (trigger.current.isConnected ? trigger.current : fallback)?.focus();
    trigger.current = null;
  }, [selection]);

  return <div className="conversation-task-sidebar" ref={root}>
    <div hidden={!!selection}>
      <ScheduledTasks canSchedule={canSchedule} canScheduleAgent={canScheduleAgent} path={`${path}/schedules`} visible={!selection}/>
      <SubagentTasks path={`${path}/subagent-plans`} runId={runId} canDelegate={canDelegate} onSummarize={onSummarize}
        heading={heading} visible={!selection} onSelect={(child, button) => { trigger.current = button; setSelection(child); }}/>
    </div>
    {selection && <ChildTaskDetail events={events} selection={selection} onBack={() => setSelection(null)}/>}
  </div>;
}

export default function ConversationTasks(props: ConversationTasksProps) {
  return <ConversationTaskBody key={props.conversationId} {...props}/>;
}
