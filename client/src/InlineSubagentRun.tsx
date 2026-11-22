import { useEffect, useState } from 'react';
import { api } from './api';
import { retryRead, transientReadError } from './runReadback';
import SubagentProgress, { isSubagentActive, type SubagentPlan } from './SubagentProgress';

// Mount only when the conversation history or the live run has confirmed a plan.
export default function InlineSubagentRun({ runId }: { runId: string }) {
  const [plan, setPlan] = useState<SubagentPlan | null>(null);
  const [stale, setStale] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const retry = () => setAttempt(previous => previous + 1);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    let timer: number | undefined;
    async function read() {
      try {
        const snapshot = await retryRead(() => api<SubagentPlan>(`/runs/${encodeURIComponent(runId)}/subagents`, { signal }), signal);
        if (signal.aborted) return;
        setPlan(snapshot);
        setStale(false);
        if (snapshot.children.some(child => isSubagentActive(child.status))) {
          timer = window.setTimeout(read, 1200);
        }
      } catch (cause) {
        if (signal.aborted) return;
        setStale(true);
        if (transientReadError(cause)) timer = window.setTimeout(read, 5000);
        else setPlan(null);
      }
    }
    void read();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [runId, attempt]);

  if (plan?.children.length) return <SubagentProgress plan={plan} stale={stale} onRetry={retry}/>;
  if (plan && !stale) return null;
  return <div className="inline-subagent-readback" role="status">
    {stale ? <>暂未读到子任务进度。 <button type="button" onClick={retry}>重新读取</button></> : '正在读取子任务…'}
  </div>;
}
