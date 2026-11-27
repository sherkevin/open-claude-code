import { useEffect, useState } from 'react';
import { ApiError, api } from './api';

export const codexPhaseLabels = {
  preparing: '正在准备工程会话',
  thread_ready: '工程会话已就绪',
  running: '工程任务执行中',
  output: '工程任务已有输出',
  interrupting: '工程任务正在取消',
  recovery_required: '工程任务待核对',
  completed: '工程任务已完成',
  interrupted: '工程任务已取消',
  failed: '工程任务失败',
  unavailable: '工程状态待刷新',
};

export type WorkspaceCodexPhase = keyof typeof codexPhaseLabels;
type PhaseMap = Record<string, WorkspaceCodexPhase>;
const livePhases = new Set<WorkspaceCodexPhase>(['preparing', 'thread_ready', 'running', 'output', 'interrupting']);
export const isLiveCodexPhase = (phase: WorkspaceCodexPhase) => livePhases.has(phase);

function pageIsVisible() {
  return document.visibilityState !== 'hidden';
}

export function useWorkspaceCodexPhases(ids: string[]): PhaseMap {
  const query = new URLSearchParams(ids.map(id => ['conversation_id', id])).toString();
  const [read, setRead] = useState<{ query: string; phases: PhaseMap }>({ query: '', phases: {} });

  useEffect(() => {
    if (!query) return;
    const rows = new URLSearchParams(query).getAll('conversation_id');
    let request: AbortController | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;

    function suspend() {
      clearTimeout(timer);
      request?.abort();
      request = null;
    }

    async function poll() {
      if (stopped || !pageIsVisible() || request) return;
      const current = new AbortController();
      request = current;
      let delay = 15000;
      try {
        const result = await api<{ phases: (WorkspaceCodexPhase | null)[] }>('/workspace/codex-phases?' + query,
          { cache: 'no-store', signal: current.signal });
        if (current.signal.aborted) return;
        if (!Array.isArray(result.phases) || result.phases.length !== rows.length) throw new Error('Invalid phases');
        const phases: PhaseMap = {};
        result.phases.forEach((phase, index) => {
          if (phase && Object.prototype.hasOwnProperty.call(codexPhaseLabels, phase)) phases[rows[index]] = phase;
        });
        setRead({ query, phases });
        if (Object.values(phases).some(isLiveCodexPhase)) delay = 1500;
      } catch (cause) {
        if (current.signal.aborted) return;
        if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) {
          stopped = true;
          setRead({ query, phases: {} });
        } else {
          setRead(previous => ({
            query,
            phases: Object.fromEntries(Object.keys(previous.query === query ? previous.phases : {})
              .map(id => [id, 'unavailable' as const])),
          }));
        }
      } finally {
        if (request === current) request = null;
        if (!stopped && !current.signal.aborted && pageIsVisible()) timer = setTimeout(poll, delay);
      }
    }

    function onVisibility() {
      suspend();
      setRead({ query, phases: {} });
      if (!stopped && pageIsVisible()) void poll();
    }

    // An identical list can be shown again after a feature/permission change.
    setRead({ query, phases: {} });
    void poll();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      stopped = true;
      suspend();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [query]);

  return query && read.query === query ? read.phases : {};
}
