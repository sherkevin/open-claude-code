import { useCallback, useEffect, useRef, useState } from 'react';
import { api, clientRequestId, post } from './api';
import type { CodexThread } from './ConversationCodex';

export type SteerId = { client_request_id: string; turn_id: string };
export type SteerReceipt = SteerId & { state: 'delivered' | 'uncertain' | 'closed' | 'rejected' };
const labels: Record<SteerReceipt['state'], string> = {
  delivered: '已送达目标轮次的执行器',
  uncertain: '送达结果待核对',
  closed: '目标轮次已无法接收，补充要求未送达',
  rejected: '执行器拒绝了补充要求，未送达',
};

export function steerableTurn(thread: CodexThread | null) {
  const active = thread?.active_turn;
  if (!thread?.bound || !active?.can_interrupt || active.interrupt_requested) return null;
  if (thread?.last_turn?.turn_id === active.turn_id) return null;
  return active.turn_id;
}

// Each tab writes a distinct key for each explicit submission. No draft text is stored.
const pendingIds = new Map<string, string>();
function prefix(scope: string) { return 'kaiwu:codex:steer:' + scope + ':'; }
export function rememberSteer(scope: string, id: SteerId) {
  const key = prefix(scope) + id.client_request_id;
  pendingIds.set(key, id.turn_id);
  try { localStorage.setItem(key, id.turn_id); } catch { /* Remains available in this page. */ }
}

function forgetSteer(scope: string, id: SteerId) {
  const key = prefix(scope) + id.client_request_id;
  if (pendingIds.get(key) === id.turn_id) pendingIds.delete(key);
  try { if (localStorage.getItem(key) === id.turn_id) localStorage.removeItem(key); } catch { /* Memory only. */ }
}

export function pendingSteers(scope: string): SteerId[] {
  const keyPrefix = prefix(scope);
  const result = new Map<string, string>();
  for (const [key, turn] of pendingIds) if (key.startsWith(keyPrefix)) result.set(key, turn);
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key?.startsWith(keyPrefix)) {
        const turn = localStorage.getItem(key);
        if (turn) result.set(key, turn);
      }
    }
  } catch { /* Memory only. */ }
  return Array.from(result, ([key, turn_id]) => ({ client_request_id: key.slice(keyPrefix.length), turn_id }));
}

export function sameSteer(a: SteerId, b: SteerId) {
  return a.client_request_id === b.client_request_id && a.turn_id === b.turn_id;
}

export function useCodexSteers(path: string, scope: string, allowed: boolean) {
  const [receipts, setReceipts] = useState<SteerReceipt[]>([]);
  const [unconfirmed, setUnconfirmed] = useState<SteerId[]>([]);
  const [submitting, setSubmitting] = useState<string | null>(null);
  const [readFailed, setReadFailed] = useState(false);
  const inFlight = useRef(false);
  const version = useRef(0);

  const refresh = useCallback(async () => {
    if (!allowed) return;
    const ticket = ++version.current;
    try {
      const data = await api<{ receipts: SteerReceipt[] }>(path + '/steers', { cache: 'no-store' });
      if (ticket !== version.current) return;
      for (const receipt of data.receipts) forgetSteer(scope, receipt);
      setUnconfirmed(pendingSteers(scope));
      setReceipts(data.receipts);
      setReadFailed(false);
    } catch {
      if (ticket !== version.current) return;
      setReadFailed(true);
      setReceipts([]);
      setUnconfirmed(pendingSteers(scope));
    }
  }, [allowed, path, scope]);

  useEffect(() => {
    void refresh();
    return () => { version.current++; };
  }, [refresh]);

  async function submit(turn: string, draft: string, onAttempt: (id: SteerId) => void) {
    const text = draft.trim();
    if (!allowed || !text || inFlight.current) return;
    inFlight.current = true;
    version.current++;
    const id = { client_request_id: clientRequestId(), turn_id: turn };
    rememberSteer(scope, id);
    setSubmitting(id.client_request_id);
    setUnconfirmed(pendingSteers(scope));
    onAttempt(id);
    try {
      const receipt = await post<SteerReceipt>(path + '/steer', { ...id, text });
      if (!sameSteer(id, receipt) || !Object.hasOwn(labels, receipt.state)) throw new Error('补充回执不匹配');
      version.current++;
      forgetSteer(scope, receipt);
      setReceipts(previous => [receipt, ...previous.filter(item => !sameSteer(item, receipt))]);
      setUnconfirmed(pendingSteers(scope));
      setReadFailed(false);
    } catch {
      await refresh();
    } finally {
      inFlight.current = false;
      setSubmitting(null);
    }
  }
  return { receipts, unconfirmed, submitting, readFailed, refresh, submit, inFlight };
}

type ViewProps = Pick<ReturnType<typeof useCodexSteers>, 'receipts' | 'unconfirmed' | 'submitting' | 'readFailed'> & {
  activeTurn?: string; lastTurn?: string;
};
export function CodexSteerReceipts({ receipts, unconfirmed, submitting, readFailed, activeTurn, lastTurn }: ViewProps) {
  if (!receipts.length && !unconfirmed.length && !readFailed) return null;
  const identity = (id: SteerId) => <small>
    请求编号 <code>{id.client_request_id}</code> · {id.turn_id === lastTurn ? '最近已结束轮次' : id.turn_id === activeTurn ? '当前轮次' : '所属轮次'} <code>{id.turn_id}</code>
  </small>;
  return <section aria-label="补充要求回执" className="section-help">
    <strong>补充要求回执</strong>
    {readFailed && <p>暂时无法读取补充回执。可点击「刷新状态」核对。</p>}
    <ul>{unconfirmed.map(id => <li key={id.client_request_id}>
      {submitting === id.client_request_id ? '正在提交补充要求' : readFailed ? '送达情况未知' : '查不到该次回执，送达未确认'}
      <br/>{identity(id)}
    </li>)}
    {receipts.map(receipt => <li key={receipt.client_request_id}>
      {labels[receipt.state] ?? '送达情况未知'}
      {receipt.state === 'delivered' && '。模型是否采用，请查看所属轮次的回答。'}
      <br/>{identity(receipt)}
    </li>)}</ul>
  </section>;
}
