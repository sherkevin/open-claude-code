import { useEffect, useRef, useState } from 'react';
import { Database, RefreshCw } from 'lucide-react';
import { api, errorText } from './api';
import { Button, ErrorNotice, Loader, Modal } from './components';
import BusinessResults, { resultsFromEvents } from './BusinessResult';
import type { Message, StreamEvent } from './types';

type ResultTarget = { conversationId: string; runId: string; messageId: string };
type HistoricalResult = { runId: string; events: StreamEvent[]; status: 'succeeded' };
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);

/** Offer one entry per past answer; the authenticated trace confirms the final message on demand. */
export function historicalResultMessages(messages: Message[], conversationId: string, currentRunId?: string) {
  const latest = new Map<string, string>();
  for (const message of messages) {
    if (message.conversation_id === conversationId && message.role === 'assistant' && message.run_id && message.run_id !== currentRunId && (message.text || message.file_ids.length)) {
      latest.set(message.run_id, message.id);
    }
  }
  return new Set(latest.values());
}

export function parseHistoricalResult(value: unknown, target: ResultTarget): HistoricalResult {
  if (!object(value) || value.schema_version !== 1 || !object(value.run) || !Array.isArray(value.events)) throw new Error('执行记录格式无法识别，请重新读取。');
  const run = value.run;
  if (run.id !== target.runId || run.conversation_id !== target.conversationId || run.assistant_message_id !== target.messageId) throw new Error('这份执行记录与当前回答不一致，未展示业务结果。');
  if (run.status !== 'succeeded') throw new Error('这次任务未成功完成，不能展示为已完成的业务结果。');
  if (!value.events.every(event => object(event) && event.run_id === target.runId && Number.isSafeInteger(event.id) && typeof event.type === 'string' && typeof event.created_at === 'string' && object(event.data))) throw new Error('执行记录中的事件无法核对，未展示业务结果。');
  const events = value.events as StreamEvent[];
  if (events.some(event => event.type === 'run.completed' && event.data.status !== 'succeeded')) throw new Error('执行状态不一致，未展示业务结果。');
  return { runId: target.runId, status: 'succeeded', events };
}

export async function loadHistoricalResult(target: ResultTarget, signal?: AbortSignal) {
  const value = await api<unknown>(`/runs/${encodeURIComponent(target.runId)}/trace`, { signal, cache: 'no-store' });
  return parseHistoricalResult(value, target);
}

export default function HistoricalBusinessResults(props: ResultTarget) {
  // Switching the target discards the previous view before any new data can render.
  return <HistoricalResultAction key={`${props.conversationId}:${props.runId}:${props.messageId}`} {...props}/>;
}

function HistoricalResultAction(target: ResultTarget) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<HistoricalResult | null>(null);
  const [error, setError] = useState('');
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const load = async () => {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    setOpen(true); setLoading(true); setResult(null); setError('');
    try {
      const next = await loadHistoricalResult(target, controller.signal);
      if (!controller.signal.aborted) setResult(next);
    } catch (e) { if (!controller.signal.aborted) setError(errorText(e)); }
    finally { if (!controller.signal.aborted) setLoading(false); }
  };
  const close = () => { request.current?.abort(); setOpen(false); setResult(null); setError(''); setLoading(false); };
  const hasResults = result && resultsFromEvents(result.events, result.runId, result.status).length > 0;
  return <>
    <Button variant="ghost" onClick={() => void load()}><Database size={15}/>查看本次业务结果</Button>
    {open && <Modal title="本次业务结果" description="历史查询结果。数据时间与查询范围见各结果卡片。" wide onClose={close}>
      <div className="modal-body">
        {loading ? <Loader text="正在读取本次业务结果…"/> : error ? <><ErrorNotice message={error}/><Button variant="secondary" onClick={() => void load()}><RefreshCw size={15}/>重新读取</Button></> : result ? hasResults ? <BusinessResults events={result.events} runId={result.runId} status={result.status}/> : <p className="section-help">这次回答没有可展示的业务结果卡片。请查看回答正文和引用来源。</p> : null}
      </div>
    </Modal>}
  </>;
}
