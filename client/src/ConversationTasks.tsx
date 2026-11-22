import { useEffect, useRef, useState } from 'react';
import { api, errorText, post, remove, timeText } from './api';
import { Button, ErrorNotice, Field, Modal } from './components';
import { transientReadError } from './runReadback';
import SubagentProgress, { isSubagentActive, subagentTitle, type SubagentPlan } from './SubagentProgress';

type Schedule = { id: string; text: string; scheduled_at: string; status: string; run_id: string | null; error: string | null };
type PlanRun = { id: string; created_at: string; status: string };
const statusText: Record<string, string> = {
  scheduled: '等待到期', pending: '待发送', queued: '排队中', running: '执行中', cancelling: '正在取消',
  awaiting_approval: '等待审批', succeeded: '完成', failed: '失败', cancelled: '已取消', interrupted: '已中断',
};
const refreshInterval = 1500;

interface ConversationTasksProps {
  conversationId: string; runId?: string; canDelegate: boolean; onSummarize: (results: string) => void; onClose: () => void;
}

export default function ConversationTasks({ onClose, ...props }: ConversationTasksProps) {
  return <Modal title="计划与子任务" wide onClose={onClose}>
    <ConversationTaskBody key={props.conversationId} {...props}/>
  </Modal>;
}

function ConversationTaskBody({ conversationId, runId, canDelegate, onSummarize }: Omit<ConversationTasksProps, 'onClose'>) {
  const [schedules, setSchedules] = useState<Schedule[] | null>(null);
  const [text, setText] = useState('');
  const [when, setWhen] = useState('');
  const [planRuns, setPlanRuns] = useState<PlanRun[]>([]);
  const [focusedRun, setFocusedRun] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const reading = useRef<AbortController | null>(null);
  const conversationPath = `/conversations/${encodeURIComponent(conversationId)}`;
  const schedulePath = `${conversationPath}/schedules`;
  const teamHistoryPath = `${conversationPath}/subagent-plans`;
  const currentIsListed = planRuns.some(item => item.id === runId);
  const selection = focusedRun === runId || planRuns.some(item => item.id === focusedRun) ? focusedRun : '';
  const teamRunId = selection || planRuns[0]?.id || runId;

  useEffect(() => {
    if (busy) return;
    const controller = new AbortController();
    reading.current = controller;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      const { signal } = controller;
      await Promise.allSettled([
        api<Schedule[]>(schedulePath, { signal }).then(items => { if (!signal.aborted) setSchedules(items); }),
        api<PlanRun[]>(teamHistoryPath, { signal }).then(items => { if (!signal.aborted) setPlanRuns(items); }),
      ]);
      if (!signal.aborted) timer = setTimeout(refresh, refreshInterval);
    };
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [schedulePath, teamHistoryPath, busy]);

  async function act(action: () => Promise<unknown>) {
    reading.current?.abort();
    setBusy(true);
    setError('');
    try { await action(); }
    catch (failure) { setError(errorText(failure)); }
    finally { setBusy(false); }
  }

  async function schedule() {
    const date = new Date(when);
    if (!Number.isFinite(date.getTime())) { setError('请选择执行时间'); return; }
    await act(async () => {
      await post(schedulePath, { text: text.trim(), scheduled_at: date.toISOString() });
      setText(''); setWhen('');
      setSchedules(await api<Schedule[]>(schedulePath));
    });
  }

  return <div className="modal-body conversation-tasks">
    {teamRunId && <section aria-label="子任务">
      {(planRuns.length > 1 || (planRuns.length > 0 && runId && !currentIsListed)) && <Field label="查看轮次">
        <select value={selection} onChange={event => setFocusedRun(event.target.value)}>
          <option value="">最近有子任务的一轮</option>
          {runId && !currentIsListed && <option value={runId}>当前一轮</option>}
          {planRuns.map(item => <option key={item.id} value={item.id}>{timeText(item.created_at)} · {statusText[item.status] || item.status}</option>)}
        </select>
      </Field>}
      <ConversationTaskPlan key={teamRunId} runId={teamRunId} canDelegate={canDelegate && teamRunId === runId} onSummarize={onSummarize}/>
    </section>}
    <section aria-label="计划任务">
      <h3>计划任务</h3>
      <ErrorNotice message={error}/>
      <p>按本地时间安排一次文字任务。到期后进入会话队列，执行时无法调用工具。</p>
      <form onSubmit={event => { event.preventDefault(); void schedule(); }}>
        <Field label="任务内容"><textarea value={text} onChange={event => setText(event.target.value)} required maxLength={50000} rows={2}/></Field>
        <Field label="执行时间"><input value={when} onChange={event => setWhen(event.target.value)} type="datetime-local" required/></Field>
        <Button type="submit" busy={busy} disabled={!text.trim() || !when}>设定时间</Button>
      </form>
      <ul>{schedules?.map(item => <li key={item.id}>
        <span>{item.text} <small>{timeText(item.scheduled_at)} · {statusText[item.status] || item.status}</small></span>
        {item.error && <small>{item.error}</small>}
        {(item.status === 'scheduled' || item.status === 'pending') && <Button variant="ghost" disabled={busy} onClick={() => void act(async () => {
          await remove(`${schedulePath}/${encodeURIComponent(item.id)}`);
          setSchedules(await api<Schedule[]>(schedulePath));
        })}>撤销</Button>}
      </li>)}</ul>
    </section>
  </div>;
}

function selectedPlan(snapshot: SubagentPlan, runId: string) {
  if (snapshot?.plan_id !== runId) throw new Error('未读到所选轮次的子任务。');
  return snapshot;
}

function ConversationTaskPlan({ runId, canDelegate, onSummarize }: { runId: string; canDelegate: boolean; onSummarize: ConversationTasksProps['onSummarize'] }) {
  const [plan, setPlan] = useState<SubagentPlan | null>(null);
  const [stale, setStale] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [delegatedText, setDelegatedText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const reading = useRef<AbortController | null>(null);
  const path = `/runs/${encodeURIComponent(runId)}/subagents`;
  const tasks = delegatedText.split('\n').map(task => task.trim()).filter(Boolean);
  const completed = plan?.children.flatMap((child, index) => child.status === 'succeeded' && typeof child.result?.output === 'string' && child.result.output.trim()
    ? [`${subagentTitle(child, index)}\n${child.result.output}`] : []) ?? [];

  useEffect(() => {
    if (busy) return;
    const controller = new AbortController();
    reading.current = controller;
    const { signal } = controller;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const snapshot = selectedPlan(await api<SubagentPlan>(path, { signal, cache: 'no-store' }), runId);
        if (signal.aborted) return;
        setPlan(snapshot);
        setStale(false);
      } catch (failure) {
        if (signal.aborted) return;
        if (!transientReadError(failure)) setPlan(null);
        setStale(true);
      } finally {
        if (!signal.aborted) timer = setTimeout(refresh, refreshInterval);
      }
    }
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [path, runId, attempt, busy]);

  function retry() {
    reading.current?.abort();
    setAttempt(value => value + 1);
  }

  async function changePlan(action: 'start' | 'cancel') {
    reading.current?.abort();
    setBusy(true);
    setError('');
    try {
      const start = action === 'start';
      const response = start ? await post<SubagentPlan>(path, { mode: 'harness', tasks }) : await post<SubagentPlan>(`${path}/cancel`);
      const next = selectedPlan(response, runId);
      // The mutation response may omit titles that are available on the GET route.
      setPlan(previous => ({ ...next, children: next.children.map(child => ({
        ...child, display_title: child.display_title ?? previous?.children.find(item => item.child_id === child.child_id)?.display_title,
      })) }));
      setStale(false);
      if (start) setDelegatedText('');
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy(false);
    }
  }

  const hasChildren = !!plan?.children.length;
  const canCancel = plan?.children.some(child => isSubagentActive(child.status));
  return <>
    {plan && hasChildren ? <SubagentProgress plan={plan} stale={stale} onRetry={retry}/> : <>
      <h3>子任务</h3>
      {stale ? <div className="conversation-task-actions">
        <p role="status">暂未读到子任务进度。</p>
        <Button variant="ghost" onClick={retry}>重新读取</Button>
      </div> : <p role="status">{plan ? '这一轮暂无子任务。' : '正在读取子任务…'}</p>}
    </>}
    <ErrorNotice message={error}/>
    {(completed.length > 0 || canCancel) && <div className="conversation-task-actions">
      {!!completed.length && <Button variant="secondary" onClick={() => onSummarize(completed.join('\n\n'))}>用结果起草汇总</Button>}
      {canCancel && <Button variant="secondary" busy={busy} onClick={() => void changePlan('cancel')}>取消子任务</Button>}
    </div>}
    {canDelegate && !hasChildren && <form onSubmit={event => { event.preventDefault(); void changePlan('start'); }}>
      <Field label="分配文字分析"><textarea value={delegatedText} onChange={event => setDelegatedText(event.target.value)} placeholder="每行一个任务，最多四项" rows={2} maxLength={8004}/></Field>
      <Button type="submit" variant="secondary" busy={busy} disabled={!tasks.length || tasks.length > 4 || tasks.some(task => task.length > 2000)}>启动子任务</Button>
    </form>}
  </>;
}
