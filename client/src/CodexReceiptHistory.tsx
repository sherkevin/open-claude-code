import { useEffect, useState } from 'react';
import { ApiError, api } from './api';
import { Button, ErrorNotice, Loader } from './components';
import { CodexTurnResult, type CodexTurn } from './ConversationCodex';
import './codex-receipt-history.css';

type ReceiptHistory = {
  recent_limit: number;
  receipts: (CodexTurn & { request_id: string })[];
  unavailable_count: number;
  pending: boolean;
};

export function readCodexReceiptHistory(conversationId: string, signal?: AbortSignal) {
  return api<ReceiptHistory>(`/conversations/${encodeURIComponent(conversationId)}/codex/history`,
    { method: 'GET', cache: 'no-store', signal });
}

export function CodexHistoryEntries({ history }: { history: ReceiptHistory }) {
  return <>
    <p className="section-help">原生 Codex 回执按从新到旧排列。常规回执只保留最近 {history.recent_limit} 份，已淘汰的正文无法恢复。
      {history.receipts.length > history.recent_limit && '最新轮次另有一份单独保存的回执。'}</p>
    {history.pending && <p role="status">有已登记的请求仍待确认最终状态。</p>}
    {history.unavailable_count > 0 && <p>另有 {history.unavailable_count} 个已登记的请求没有可读回执，无法确认最终状态或找回正文。</p>}
    {history.receipts.length > 0 ? <ol aria-label="已留存的工程回执">
      {history.receipts.map(turn => <li key={turn.request_id}>
        <small>请求编号 <code>{turn.request_id}</code></small>
        <CodexTurnResult turn={turn}/>
      </li>)}
    </ol> : <p>当前没有可展示的已留存回执。</p>}
  </>;
}

function HistoryPanel({ conversationId }: { conversationId: string }) {
  const [history, setHistory] = useState<ReceiptHistory | null>(null);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    readCodexReceiptHistory(conversationId, controller.signal).then(result => {
      if (!controller.signal.aborted) setHistory(result);
    }).catch(cause => {
      if (!controller.signal.aborted) setError(cause instanceof ApiError ? cause.message : '工程回执暂时无法读取。');
    });
    return () => controller.abort();
  }, [conversationId, revision]);

  if (!history && !error) return <Loader text="正在读取工程回执…"/>;
  return <>
    <ErrorNotice message={error}/>
    {history && <CodexHistoryEntries history={history}/>}
    <Button variant="secondary" onClick={() => {
      setHistory(null);
      setError('');
      setRevision(value => value + 1);
    }}>刷新回执</Button>
  </>;
}

export default function CodexReceiptHistory({ conversationId, unavailableReason }: {
  conversationId: string; unavailableReason?: string;
}) {
  const [open, setOpen] = useState(false);
  return <details className="codex-receipt-history" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>Codex 工程回执历史</summary>
    {open && <div className="codex-receipt-panel">
      {unavailableReason ? <p role="status">{unavailableReason}</p> : <HistoryPanel conversationId={conversationId}/>}
    </div>}
  </details>;
}
