import { useCallback, useEffect, useRef, useState } from 'react';
import { SquareTerminal } from 'lucide-react';
import { api, clientRequestId, errorText, post } from './api';
import { Button, ErrorNotice, Field, Loader, Modal } from './components';

type Turn = { turn_id: string; status: string; text?: string; text_truncated: boolean };
type Thread = {
  bound: boolean;
  active_turn: { turn_id: string; can_interrupt?: boolean; interrupt_requested: boolean } | null;
  recovery_required: boolean;
  last_turn: Turn | null;
};

const statusLabels: Record<string, string> = {
  completed: '已完成', interrupted: '已取消', failed: '执行失败', cancelled: '已取消', canceled: '已取消',
};

function CodexDialog({ conversationId, onClose }: { conversationId: string; onClose: () => void }) {
  const path = '/conversations/' + encodeURIComponent(conversationId) + '/codex';
  const [thread, setThread] = useState<Thread | null>(null);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const request = useRef<{ id: string; text: string } | null>(null);
  const generation = useRef(0);
  const refresh = useCallback(async () => {
    const version = ++generation.current;
    try {
      const result = await api<Thread>(path, { cache: 'no-store' });
      if (version === generation.current) setThread(result);
    } catch (cause) {
      if (version === generation.current) {
        setThread(null);
        setError(errorText(cause));
      }
    }
  }, [path]);

  useEffect(() => {
    void refresh();
    return () => { generation.current++; };
  }, [refresh]);

  const active = thread?.active_turn;
  useEffect(() => {
    if (!sending && !active?.can_interrupt) return;
    // Schedule after each read so a slow request cannot overlap the next one.
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await refresh();
      if (!stopped) timer = setTimeout(poll, 900);
    };
    timer = setTimeout(poll, 300);
    return () => { stopped = true; clearTimeout(timer); };
  }, [sending, active?.can_interrupt, refresh]);

  async function action(route: string, payload?: unknown) {
    if (busy) return;
    setBusy(true);
    setError('');
    try { await post(path + route, payload); }
    catch (cause) { setError(errorText(cause)); }
    finally { await refresh(); setBusy(false); }
  }

  async function send() {
    if (sending || busy || !thread?.bound || thread.recovery_required || !draft.trim()) return;
    const text = draft.trim();
    if (request.current?.text !== text) request.current = { id: clientRequestId(), text };
    setSending(true);
    setError('');
    try {
      await post<Turn>(path + '/turn', { client_request_id: request.current.id, text });
      request.current = null;
      setDraft('');
    } catch (cause) { setError(errorText(cause)); }
    finally { await refresh(); setSending(false); }
  }

  const last = thread?.last_turn;
  return <Modal title="Codex 工程任务" onClose={onClose}>
    <div className="modal-body">
      <ErrorNotice message={error}/>
      {!thread && !error && <Loader text="正在读取任务状态…"/>}
      {thread && <div role="status" className="section-help">
        <strong>{active?.interrupt_requested ? '正在取消' : active?.can_interrupt ? '执行中'
          : thread.recovery_required ? '上次执行待核对' : sending ? '正在提交'
          : last ? statusLabels[last.status] || last.status : thread.bound ? '可以发送任务' : '尚未准备会话'}</strong>
        {(active || sending || thread.recovery_required) && last?.text && <p>上一轮结果</p>}
        {last?.text && <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{last.text}</p>}
        {last?.text_truncated && <p>{last.text ? '仅显示已保存的部分结果。' : '仅恢复了轮次状态，正文未保存。'}</p>}
      </div>}
      {thread?.bound && !thread.recovery_required && <Field label="任务内容">
        <textarea rows={4} maxLength={20000} value={draft} disabled={sending || busy} onChange={e => setDraft(e.target.value)}/>
      </Field>}
      <div className="task-sidebar-actions">
        {thread && !thread.bound && <Button busy={busy} onClick={() => void action('/thread')}>准备会话</Button>}
        {thread?.bound && !thread.recovery_required && <Button busy={sending} disabled={busy || !draft.trim()} onClick={() => void send()}>发送任务</Button>}
        {active?.can_interrupt && <Button disabled={busy || active.interrupt_requested} variant="danger"
          onClick={() => void action('/interrupt', { turn_id: active.turn_id })}>取消当前任务</Button>}
        {thread?.recovery_required && !active?.can_interrupt && <Button busy={busy} onClick={() => void action('/reconcile')}>核对并结束中断轮次</Button>}
        <Button disabled={busy} variant="secondary" onClick={() => { setError(''); void refresh(); }}>刷新状态</Button>
      </div>
    </div>
  </Modal>;
}

export default function ConversationCodex({ conversationId, unavailable }: { conversationId: string; unavailable: boolean }) {
  const [open, setOpen] = useState(false);
  return <>
    <button type="button" className="icon-button" title="Codex 工程任务" aria-label="Codex 工程任务"
      disabled={unavailable} onClick={() => setOpen(true)}><SquareTerminal size={17}/></button>
    {open && <CodexDialog conversationId={conversationId} onClose={() => setOpen(false)}/>}
  </>;
}
