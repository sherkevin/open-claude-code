import { useCallback, useEffect, useRef, useState } from 'react';
import { ListOrdered, Pencil, X } from 'lucide-react';
import { api, errorText, patch, post } from './api';
import { Button, ErrorNotice, Field, Modal } from './components';

export type QueuedMessage = { id: string; revision: number; text: string; file_ids: string[]; skill_ids: string[]; error: string | null };
export type QueueState = { items: QueuedMessage[]; paused: boolean; archived: boolean; active_run_id: string | null; latest_run: {id: string; status: string} | null };
type QueueResult = { status: string; run_id?: string; queue: QueueState };
export const queueCanAdvance = (state: QueueState | null, ready: boolean) => Boolean(ready && state && state.items.length && !state.active_run_id && !state.paused && !state.archived);

export function useMessageQueue(conversationId: string, ready: boolean, runKey: string, onAccepted: (runId: string) => Promise<void>) {
  const [state, setState] = useState<QueueState | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const mounted = useRef(true), pending = useRef(false), version = useRef(0), callback = useRef(onAccepted);
  callback.current = onAccepted;
  const base = `/conversations/${conversationId}/queue`;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; version.current++; }; }, []);
  const refresh = useCallback(async () => {
    const id = ++version.current;
    try { const next = await api<QueueState>(base); if (mounted.current && id === version.current) { setState(next); setError(''); } return next; }
    catch (cause) { if (mounted.current && id === version.current) setError(`待发消息读取失败：${errorText(cause)}`); return null; }
  }, [base]);
  useEffect(() => { void refresh(); const timer = window.setInterval(() => { if (!pending.current) void refresh(); }, 3000); return () => clearInterval(timer); }, [refresh, runKey]);
  const dispatch = useCallback(async (resume = false) => {
    if (pending.current) return;
    pending.current = true; setBusy(true);
    try {
      const next = await refresh();
      if (!mounted.current || !next?.items.length || next.active_run_id || next.archived || (next.paused && !resume)) return;
      const item = next.items[0];
      const result = await post<QueueResult>(`${base}/${item.id}/dispatch`, { revision: item.revision, after_run_id: next.latest_run?.id || null, resume });
      if (!mounted.current) return;
      version.current++; setState(result.queue);
      if (result.status === 'accepted' && result.run_id) await callback.current(result.run_id);
    } catch (cause) { if (mounted.current) setError(`队列状态待确认：${errorText(cause)}`); }
    finally { pending.current = false; if (mounted.current) setBusy(false); }
  }, [base, refresh]);
  useEffect(() => { if (queueCanAdvance(state, ready) && !busy && !error) void dispatch(); }, [state, ready, busy, error, dispatch]);
  useEffect(() => {
    const latest = state?.latest_run?.id;
    if (ready && !busy && latest && latest !== runKey.split(':')[0]) void callback.current(latest).catch(cause => { if (mounted.current) setError(errorText(cause)); });
  }, [state?.latest_run?.id, ready, busy, runKey]);
  const enqueue = async (payload: Record<string, unknown>) => {
    const item = await post<QueuedMessage & {status:string}>(base, payload);
    if (item.status !== 'pending') throw new Error('这条待发消息已处理，请刷新会话后确认。');
    await refresh();
  };
  const change = async (item: QueuedMessage, text?: string) => {
    if (pending.current) return false;
    pending.current = true; setBusy(true);
    try {
      const next = text === undefined ? await post<QueueState>(`${base}/${item.id}/cancel`, { revision: item.revision }) : await patch<QueueState>(`${base}/${item.id}`, { revision: item.revision, text });
      if (mounted.current) { version.current++; setState(next); setError(''); } return true;
    } catch (cause) { if (mounted.current) setError(errorText(cause)); await refresh(); return false; }
    finally { pending.current = false; if (mounted.current) setBusy(false); }
  };
  return { state, error, busy, enqueue, refresh, dispatch, change };
}

export default function MessageQueue({ queue }: { queue: ReturnType<typeof useMessageQueue> }) {
  const [editor, setEditor] = useState<QueuedMessage | null>(null), [text, setText] = useState('');
  if (!queue.state?.items.length && !queue.error) return null;
  return <section className="chat-message-queue" aria-label="待发消息">
    <ErrorNotice message={queue.error}/>
    <div className="chat-queue-header"><ListOrdered size={15}/><span>待发消息 · {queue.state?.items.length || 0}</span><small>留在本会话时依次发送</small>{queue.state?.paused && <Button variant="secondary" busy={queue.busy} onClick={() => void queue.dispatch(true)}>继续队列</Button>}{queue.error && <Button variant="ghost" onClick={() => void queue.refresh()}>刷新</Button>}</div>
    {queue.state?.items.map((item, index) => <div className="chat-queue-item" key={item.id}><span>{index + 1}. {item.text}{Boolean(item.file_ids?.length) && <small> · {item.file_ids.length} 个附件</small>}</span><div className="chat-queue-actions"><button type="button" aria-label={`编辑待发消息 ${index + 1}`} disabled={queue.busy} onClick={() => { setEditor(item); setText(item.text); }}><Pencil size={14}/></button><button type="button" aria-label={`取消待发消息 ${index + 1}`} disabled={queue.busy} onClick={() => void queue.change(item)}><X size={15}/></button></div></div>)}
    {queue.state?.paused && <p className="chat-queue-note">{queue.state.items[0]?.error || '上一项未成功完成。确认后可继续剩余消息。'}</p>}
    {editor && <Modal title="编辑待发消息" onClose={() => { if (!queue.busy) setEditor(null); }}><form onSubmit={event => { event.preventDefault(); void queue.change(editor, text.trim()).then(ok => { if (ok) setEditor(null); }); }}><div className="modal-body"><Field label="消息内容"><textarea autoFocus rows={5} value={text} onChange={event => setText(event.target.value)} maxLength={50000}/></Field></div><footer className="modal-footer"><Button variant="secondary" disabled={queue.busy} onClick={() => setEditor(null)}>取消</Button><Button type="submit" busy={queue.busy} disabled={!text.trim()}>保存</Button></footer></form></Modal>}
  </section>;
}
