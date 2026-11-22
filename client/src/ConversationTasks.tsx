import { useEffect, useState } from 'react';
import { api, errorText, post, remove, timeText } from './api';
import { Button, ErrorNotice, Field, Modal } from './components';

type Schedule = { id: string; text: string; scheduled_at: string; status: string; run_id: string | null; error: string | null };
type Child = { child_id: string; role: string; status: string; stop_reason: string | null; result?: { output?: unknown }; observed_usage?: { input_tokens?: number; output_tokens?: number } };
type Plan = { counts: Record<string, number>; children: Child[] };
type PlanRun = { id: string; created_at: string; status: string };
const statusText: Record<string, string> = {
  scheduled: '等待到期', pending: '待发送', queued: '排队中', running: '执行中', cancelling: '正在取消',
  awaiting_approval: '等待审批', succeeded: '完成', failed: '失败', cancelled: '已取消', interrupted: '已中断',
};

export default function ConversationTasks({ conversationId, runId, canDelegate, onSummarize, onClose }: {
  conversationId: string; runId?: string; canDelegate: boolean; onSummarize: (results: string) => void; onClose: () => void;
}) {
  const [schedules, setSchedules] = useState<Schedule[] | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [text, setText] = useState('');
  const [when, setWhen] = useState('');
  const [delegatedText, setDelegatedText] = useState('');
  const [planRuns, setPlanRuns] = useState<PlanRun[]>([]);
  const [focusedRun, setFocusedRun] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const schedulePath = `/conversations/${conversationId}/schedules`;
  const teamRunId = focusedRun || runId;
  const teamPath = teamRunId ? `/runs/${teamRunId}/subagents` : '';
  const teamHistoryPath = `/conversations/${conversationId}/subagent-plans`;
  const tasks = delegatedText.split('\n').map(task => task.trim()).filter(Boolean);
  const completed = plan?.children.flatMap((child, index) => child.status === 'succeeded' && typeof child.result?.output === 'string'
    ? [`子任务 ${index + 1}\n${child.result.output}`] : []) || [];

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      const init = { signal: controller.signal };
      const readings = await Promise.allSettled([api<Schedule[]>(schedulePath, init), teamPath ? api<Plan>(teamPath, init) : null, api<PlanRun[]>(teamHistoryPath, init)]);
      if (!controller.signal.aborted) {
        if (readings[0].status === 'fulfilled') setSchedules(readings[0].value);
        if (readings[1].status === 'fulfilled') setPlan(readings[1].value);
        if (readings[2].status === 'fulfilled') setPlanRuns(readings[2].value);
        timer = setTimeout(refresh, 1500);
      }
    };
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [schedulePath, teamPath, teamHistoryPath]);

  async function act(action: () => Promise<unknown>) {
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

  return <Modal title="计划与子任务" wide onClose={onClose}>
    <div className="modal-body conversation-tasks">
      <ErrorNotice message={error}/>
      <section aria-label="计划任务">
        <h3>计划任务</h3>
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
            await remove(`${schedulePath}/${item.id}`);
            setSchedules(await api<Schedule[]>(schedulePath));
          })}>撤销</Button>}
        </li>)}</ul>
      </section>
      <section aria-label="子任务">
        <h3>子任务</h3>
        {!!planRuns.length && <Field label="父任务"><select value={focusedRun} onChange={event => { setPlan(null); setFocusedRun(event.target.value); }}>
          <option value="">最新一轮</option>
          {planRuns.map(item => <option key={item.id} value={item.id}>{timeText(item.created_at)} · {statusText[item.status] || item.status}</option>)}
        </select></Field>}
        {!!plan?.children.length && <>
          <p>{plan.counts.succeeded || 0}/{plan.children.length} 项完成</p>
          <ul>{plan.children.map((child, index) => <li key={child.child_id}>
            <span>{child.role === 'harness_analysis' ? '分析' : '本机验证'} {index + 1} <small>{statusText[child.status] || child.status}</small></span>
            {typeof child.result?.output === 'string' && <p>{child.result.output}</p>}
            {child.observed_usage?.output_tokens != null && <small>输出 {child.observed_usage.output_tokens} token</small>}
          </li>)}</ul>
          {!!completed.length && <Button variant="secondary" onClick={() => onSummarize(completed.join('\n\n'))}>用结果起草汇总</Button>}
        </>}
        {teamPath && canDelegate && teamRunId === runId && !plan?.children.length && <form onSubmit={event => { event.preventDefault(); void act(async () => {
          const result = await post<Plan>(teamPath, { mode: 'harness', tasks }); setPlan(result); setDelegatedText('');
        }); }}>
          <Field label="分配文字分析"><textarea value={delegatedText} onChange={event => setDelegatedText(event.target.value)} placeholder="每行一个任务，最多四项" rows={2} maxLength={8004}/></Field>
          <Button type="submit" variant="secondary" busy={busy} disabled={!tasks.length || tasks.length > 4 || tasks.some(task => task.length > 2000)}>启动子任务</Button>
        </form>}
        {teamPath && plan?.children.some(child => ['queued', 'running', 'cancelling'].includes(child.status)) &&
          <Button variant="secondary" busy={busy} onClick={() => void act(async () => { setPlan(await post<Plan>(`${teamPath}/cancel`)); })}>取消子任务</Button>}
      </section>
    </div>
  </Modal>;
}
