import { useCallback, useEffect, useRef, useState } from 'react';
import { SquareTerminal } from 'lucide-react';
import { api, clientRequestId, errorText, post } from './api';
import { Button, ErrorNotice, Field, Loader, Modal } from './components';

type Turn = { turn_id: string; request_id?: string; status: string; text?: string; text_truncated: boolean };
type Operation = { request_id: string | null; phase: 'preparing' | 'thread_ready' | 'running' | 'output'; text?: string };
export type CodexThread = {
  bound: boolean;
  active_turn: { turn_id: string; request_id?: string; can_interrupt?: boolean; interrupt_requested: boolean } | null;
  recovery_required: boolean;
  last_turn: Turn | null;
  operation?: Operation | null;
};

const statusLabels: Record<string, string> = {
  completed: '已完成', interrupted: '已取消', failed: '执行失败', cancelled: '已取消', canceled: '已取消',
};
const phaseLabels = {
  preparing: '开物已收到请求，正在准备 Codex 会话',
  thread_ready: 'Codex 会话已绑定，正在启动任务',
  running: '执行中，等待首段输出',
  output: '执行中，已收到首段输出',
};

export function codexRequestKey(ownerId: string, conversationId: string) {
  return 'kaiwu:codex:request:' + encodeURIComponent(ownerId) + ':' + encodeURIComponent(conversationId);
}

export function keepCodexRequestId(storage: Pick<Storage, 'getItem' | 'setItem'>, key: string, pending: string | null) {
  const id = storage.getItem(key) || pending || clientRequestId();
  storage.setItem(key, id);
  return id;
}

export function codexTurnPayload(thread: CodexThread, id: string, text: string) {
  return { client_request_id: id, text: text.trim(), ...(!thread.bound ? { prepare_if_needed: true } : {}) };
}

export function codexControls(thread: CodexThread) {
  return {
    send: !thread.operation && !thread.active_turn && !thread.recovery_required,
    prepare: !thread.bound && !thread.operation?.request_id && !thread.recovery_required,
    reconcile: thread.recovery_required && !thread.operation && !thread.active_turn?.can_interrupt,
  };
}

export function CodexStatus({ thread, sending, preparing, pending }: {
  thread: CodexThread; sending: boolean; preparing: boolean; pending: boolean;
}) {
  const { operation, active_turn: active, last_turn: last } = thread;
  const outstanding = Boolean(operation || active || sending || pending || thread.recovery_required);
  const status = active?.interrupt_requested ? '正在取消'
    : operation?.phase === 'thread_ready' && !operation.request_id ? 'Codex 会话已绑定'
    : operation?.phase === 'output' && !operation.text ? '执行中'
    : operation ? phaseLabels[operation.phase]
    : active?.can_interrupt ? '执行中' : thread.recovery_required ? '上次执行待核对'
    : sending ? '正在提交，等待开物确认' : pending ? '原请求结果待确认'
    : preparing ? '正在请求准备会话，等待开物确认' : last ? statusLabels[last.status] || last.status
    : thread.bound ? '可以发送任务' : '尚未准备会话';
  return <div role="status" className="section-help">
    <strong>{status}</strong>
    {operation?.phase === 'output' && operation.text && <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{operation.text}</p>}
    {pending && !sending && !operation && !active && !thread.recovery_required &&
      <p>刷新状态可核对进度。如需重试，请使用原任务内容和原请求编号。</p>}
    {outstanding && last && (last.text || last.text_truncated) && <p>上一轮结果</p>}
    {last?.text && <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{last.text}</p>}
    {last?.text_truncated && <p>{last.text ? '仅显示已保存的部分结果。' : '仅恢复了轮次状态，正文未保存。'}</p>}
  </div>;
}

function CodexDialog({ conversationId, ownerId, onClose }: { conversationId: string; ownerId: string; onClose: () => void }) {
  const path = '/conversations/' + encodeURIComponent(conversationId) + '/codex';
  const storageKey = codexRequestKey(ownerId, conversationId);
  const [thread, setThread] = useState<CodexThread | null>(null);
  const [draft, setDraft] = useState('');
  const [pending, setPending] = useState<string | null>(() => {
    try { return sessionStorage.getItem(storageKey); } catch { return null; }
  });
  const [sending, setSending] = useState(false);
  const [action, setAction] = useState('');
  const [error, setError] = useState('');
  const [readError, setReadError] = useState('');
  const pendingId = useRef(pending);
  const requestInFlight = useRef(false);
  const actionInFlight = useRef(false);
  const generation = useRef(0);

  const finish = useCallback((id: string) => {
    if (pendingId.current !== id) return;
    try {
      if (sessionStorage.getItem(storageKey) === id) sessionStorage.removeItem(storageKey);
    } catch { /* The server receipt is authoritative. */ }
    pendingId.current = null;
    setPending(null);
    setError('');
    setDraft('');
  }, [storageKey]);

  const refresh = useCallback(async () => {
    const version = ++generation.current;
    try {
      const result = await api<CodexThread>(path, { cache: 'no-store' });
      if (version !== generation.current) return;
      setThread(result);
      setReadError('');
      const id = pendingId.current;
      if (id && result.last_turn?.request_id === id) finish(id);
    } catch (cause) {
      if (version === generation.current) setReadError(errorText(cause));
    }
  }, [path, finish]);

  useEffect(() => {
    void refresh();
    return () => { generation.current++; };
  }, [refresh]);

  const active = thread?.active_turn;
  const operation = thread?.operation;
  const poll = Boolean(sending || pending || action === '/thread' || operation || active?.can_interrupt);
  useEffect(() => {
    if (!poll) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const read = async () => {
      await refresh();
      if (!stopped) timer = setTimeout(read, 900);
    };
    timer = setTimeout(read, 300);
    return () => { stopped = true; clearTimeout(timer); };
  }, [poll, refresh]);

  async function perform(route: string, payload?: unknown) {
    if (actionInFlight.current || (route === '/thread' && requestInFlight.current)) return;
    actionInFlight.current = true;
    setAction(route);
    setError('');
    try { await post(path + route, payload); }
    catch (cause) { setError(errorText(cause)); }
    finally { await refresh(); actionInFlight.current = false; setAction(''); }
  }

  const controls = thread && codexControls(thread);
  const canSend = controls?.send && !readError && !action && !sending && Boolean(draft.trim());

  async function send() {
    if (!canSend || !thread || requestInFlight.current || actionInFlight.current) return;
    let id: string;
    try { id = keepCodexRequestId(sessionStorage, storageKey, pending); }
    catch { setError('浏览器无法保存请求编号。请允许会话存储后再发送，以便刷新时核对结果。'); return; }
    requestInFlight.current = true;
    pendingId.current = id;
    setPending(id);
    setSending(true);
    setError('');
    try {
      await post<Turn>(path + '/turn', codexTurnPayload(thread, id, draft));
      finish(id);
    } catch (cause) { if (pendingId.current === id) setError(errorText(cause)); }
    finally { await refresh(); requestInFlight.current = false; setSending(false); }
  }

  return <Modal title="Codex 工程任务" onClose={onClose}>
    <div className="modal-body">
      <ErrorNotice message={readError || error}/>
      {!thread && !readError && <Loader text="正在读取任务状态…"/>}
      {thread && <CodexStatus thread={thread} sending={sending} pending={Boolean(pending)} preparing={action === '/thread'}/>}
      <Field label="任务内容">
        <textarea rows={4} maxLength={20000} value={draft} disabled={sending || Boolean(operation?.request_id) || Boolean(active) || Boolean(thread?.recovery_required)}
          onChange={e => setDraft(e.target.value)}/>
      </Field>
      <div className="task-sidebar-actions">
        <Button busy={sending} disabled={!canSend} onClick={() => void send()}>{pending && !sending && controls?.send ? '用原编号重试' : '发送任务'}</Button>
        {controls?.prepare && !sending && <Button variant="secondary"
          busy={action === '/thread' || Boolean(operation)} disabled={Boolean(action || readError)}
          onClick={() => void perform('/thread')}>准备会话</Button>}
        {active?.can_interrupt && <Button disabled={Boolean(action) || active.interrupt_requested} variant="danger"
          onClick={() => void perform('/interrupt', { turn_id: active.turn_id })}>取消当前任务</Button>}
        {controls?.reconcile && !sending && <Button busy={action === '/reconcile'}
          disabled={Boolean(action || readError)} onClick={() => void perform('/reconcile')}>核对并结束中断轮次</Button>}
        <Button variant="secondary" onClick={() => void refresh()}>刷新状态</Button>
      </div>
    </div>
  </Modal>;
}

export default function ConversationCodex({ conversationId, ownerId, unavailable }: { conversationId: string; ownerId: string; unavailable: boolean }) {
  const [open, setOpen] = useState(false);
  return <>
    <button type="button" className="icon-button" title="Codex 工程任务" aria-label="Codex 工程任务"
      disabled={unavailable} onClick={() => setOpen(true)}><SquareTerminal size={17}/></button>
    {open && <CodexDialog conversationId={conversationId} ownerId={ownerId} onClose={() => setOpen(false)}/>}
  </>;
}
