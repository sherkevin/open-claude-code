import { useCallback, useEffect, useRef, useState } from 'react';
import { SquareTerminal } from 'lucide-react';
import { api, clientRequestId, errorText, post } from './api';
import { Button, ErrorNotice, Field, Loader, Modal } from './components';

export type CodexTurn = { turn_id: string; request_id?: string; status: string; text?: string; text_truncated: boolean; text_withheld?: boolean };
type Operation = { request_id: string | null; phase: 'preparing' | 'thread_ready' | 'running' | 'output'; text?: string };
export type CodexThread = {
  bound: boolean;
  active_turn: { turn_id: string; request_id?: string; can_interrupt?: boolean; interrupt_requested: boolean } | null;
  recovery_required: boolean;
  last_turn: CodexTurn | null;
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

type StorageAccess = () => Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
const tabRequests = new Map<string, string>();
const browserStorage: StorageAccess = () => sessionStorage;

function readRequest(key: string, storage: StorageAccess = browserStorage) {
  if (tabRequests.has(key)) return tabRequests.get(key)!;
  try { return storage().getItem(key); } catch { return null; }
}

export function keepCodexRequestId(key: string, previous: string | null, storage: StorageAccess = browserStorage) {
  const id = previous || clientRequestId();
  tabRequests.set(key, id);
  try {
    storage().setItem(key, id);
    return { id, stored: true };
  } catch {
    return { id, stored: false };
  }
}

function forgetRequest(key: string, id: string) {
  if (tabRequests.get(key) === id) tabRequests.delete(key);
  try { if (sessionStorage.getItem(key) === id) sessionStorage.removeItem(key); } catch { /* Memory-only tab. */ }
}

export type CodexReceipt = {
  request_id: string;
  state: 'recorded' | 'admitted_without_receipt' | 'not_found' | 'pending';
  turn: CodexTurn | null;
};

export function originalRequestResolved(receipt: CodexReceipt | null) {
  return receipt?.state === 'recorded' || receipt?.state === 'admitted_without_receipt';
}

export function CodexTurnResult({ turn }: { turn: CodexTurn }) {
  return <>
    <p><strong>{statusLabels[turn.status] || turn.status}</strong></p>
    {turn.text_withheld ? <p className="section-help">正文含宿主路径或凭据标记，已隐藏。{turn.text_truncated && '留存的正文已截断。'}</p> : <>
      {turn.text && <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{turn.text}</p>}
      {turn.text_truncated && <p className="section-help">{turn.text ? '仅显示此请求留存的部分正文。' : '仅留存轮次状态，正文未保存。'}</p>}
    </>}
  </>;
}

export function OriginalCodexReceipt({ receipt }: { receipt: CodexReceipt }) {
  const { state, turn } = receipt;
  return <section aria-label="原请求回执" className="section-help">
    <strong>原请求回执</strong>
    <p>请求编号 <code>{receipt.request_id}</code></p>
    {state === 'recorded' && turn && <CodexTurnResult turn={turn}/>}
    {state === 'admitted_without_receipt' && <p>开物曾登记此请求，已无可读回执，无法找回正文或确认最终状态。可以另行发送新任务。</p>}
    {state === 'not_found' && <p>暂时查不到这个编号，执行结果仍未确认。可刷新状态，或输入原内容后用原编号重试。</p>}
    {state === 'pending' && <p>开物已登记此请求，最终回执仍待确认。</p>}
  </section>;
}

export function codexTurnPayload(thread: CodexThread, id: string, text: string) {
  return { client_request_id: id, text: text.trim(), ...(!thread.bound ? { prepare_if_needed: true } : {}) };
}

export function codexControls(thread: CodexThread) {
  return {
    send: !thread.operation && !thread.active_turn && !thread.recovery_required,
    prepare: !thread.bound && !thread.operation && !thread.recovery_required,
    reconcile: thread.recovery_required && !thread.operation && !thread.active_turn?.can_interrupt,
  };
}

export function CodexStatus({ thread, sending, preparing, pending, requestId }: {
  thread: CodexThread; sending: boolean; preparing: boolean; pending: boolean; requestId?: string | null;
}) {
  const { operation, active_turn: active, last_turn: last } = thread;
  const outstanding = Boolean(operation || active || sending || pending || thread.recovery_required);
  // A terminal receipt can reach disk before the native process releases its lock.
  const currentId = operation ? operation.request_id : active?.request_id || requestId;
  const terminal = Boolean(last?.request_id && last.request_id === currentId && statusLabels[last.status]);
  const status = terminal && last ? statusLabels[last.status] : active?.interrupt_requested ? '正在取消'
    : operation?.phase === 'thread_ready' && !operation.request_id ? 'Codex 会话已绑定'
    : operation?.phase === 'output' && !operation.text ? '执行中'
    : operation ? phaseLabels[operation.phase]
    : active?.can_interrupt ? '执行中' : thread.recovery_required ? '上次执行待核对'
    : sending ? '正在提交，等待开物确认' : pending ? '原请求结果待确认'
    : preparing ? '正在请求准备会话，等待开物确认' : last ? statusLabels[last.status] || last.status
    : thread.bound ? '可以发送任务' : '尚未准备会话';
  return <div role="status" className="section-help">
    <strong>{status}</strong>
    {!terminal && operation?.phase === 'output' && operation.text && <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{operation.text}</p>}
    {!terminal && pending && !sending && !operation && !active && !thread.recovery_required &&
      <p>刷新状态可核对进度。如需重试，请使用原任务内容和原请求编号。</p>}
    {!terminal && outstanding && last && (last.text || last.text_truncated) && <p>上一轮结果</p>}
    {last?.text && <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{last.text}</p>}
    {last?.text_truncated && <p>{last.text ? '仅显示已保存的部分结果。' : '仅恢复了轮次状态，正文未保存。'}</p>}
  </div>;
}

function CodexDialog({ conversationId, ownerId, onClose }: { conversationId: string; ownerId: string; onClose: () => void }) {
  const path = '/conversations/' + encodeURIComponent(conversationId) + '/codex';
  const storageKey = codexRequestKey(ownerId, conversationId);
  const [thread, setThread] = useState<CodexThread | null>(null);
  const [draft, setDraft] = useState('');
  const [pending, setPending] = useState(() => readRequest(storageKey));
  const [receipt, setReceipt] = useState<CodexReceipt | null>(null);
  const [memoryOnly, setMemoryOnly] = useState(false);
  const sentText = useRef<string | null>(null);
  const [sendingId, setSendingId] = useState<string | null>(null);
  const sending = Boolean(sendingId);
  const [action, setAction] = useState('');
  const [error, setError] = useState('');
  const [readError, setReadError] = useState('');
  const pendingId = useRef(pending);
  const requestInFlight = useRef(false);
  const actionInFlight = useRef(false);
  const generation = useRef(0);

  const finish = useCallback((id: string) => {
    if (pendingId.current !== id) return;
    forgetRequest(storageKey, id);
    pendingId.current = null;
    setPending(null);
    setReceipt(null);
    setError('');
    const submitted = sentText.current;
    setDraft(current => current.trim() === submitted ? '' : current);
    sentText.current = null;
  }, [storageKey]);

  const refresh = useCallback(async () => {
    const version = ++generation.current;
    try {
      const result = await api<CodexThread>(path, { cache: 'no-store' });
      if (version !== generation.current) return;
      setThread(result);
      setReadError('');
      const id = pendingId.current;
      if (!id) return;
      if (result.last_turn?.request_id === id) { finish(id); return; }
      if (result.operation?.request_id === id || result.active_turn?.request_id === id) {
        setReceipt(null);
        return;
      }
      const lookup = await api<CodexReceipt>(path + '/requests/' + encodeURIComponent(id), { cache: 'no-store' });
      if (version !== generation.current || pendingId.current !== id) return;
      if (lookup.request_id !== id || (lookup.state === 'recorded' && lookup.turn?.request_id !== id)) {
        throw new Error('回执的请求编号不一致，请刷新核对。');
      }
      setReceipt(lookup);
      if (originalRequestResolved(lookup)) {
        setError('');
        const submitted = sentText.current;
        if (submitted !== null) {
          setDraft(current => current.trim() === submitted ? '' : current);
          sentText.current = null;
        }
      }
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
  const resolved = originalRequestResolved(receipt);
  const waiting = Boolean(pending && (!receipt || receipt.state === 'pending'));
  const poll = Boolean(sending || waiting || action === '/thread' || operation || active?.can_interrupt);
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
    if (actionInFlight.current || (route === '/thread' && (requestInFlight.current || operation))) return;
    actionInFlight.current = true;
    setAction(route);
    setError('');
    try { await post(path + route, payload); }
    catch (cause) { setError(errorText(cause)); }
    finally { await refresh(); actionInFlight.current = false; setAction(''); }
  }

  const controls = thread && codexControls(thread);
  const visibleReceipt = sending && !resolved ? null : receipt;
  const latestId = operation?.request_id || active?.request_id || thread?.last_turn?.request_id;
  const sameRequestRetry = Boolean(pending && receipt?.state === 'not_found');
  const canSend = controls?.send && (!pending || resolved || sameRequestRetry) && !readError && !action && !sending && Boolean(draft.trim());

  async function send() {
    if (!canSend || !thread || requestInFlight.current || actionInFlight.current) return;
    const { id, stored } = keepCodexRequestId(storageKey, sameRequestRetry ? pending : null);
    sentText.current = draft.trim();
    setMemoryOnly(!stored);
    setReceipt(null);
    requestInFlight.current = true;
    pendingId.current = id;
    setPending(id);
    setSendingId(id);
    setError('');
    try {
      await post<CodexTurn>(path + '/turn', codexTurnPayload(thread, id, draft));
    } catch (cause) { if (pendingId.current === id) setError(errorText(cause)); }
    finally { await refresh(); requestInFlight.current = false; setSendingId(null); }
  }

  return <Modal title="Codex 工程任务" onClose={onClose}>
    <div className="modal-body">
      <ErrorNotice message={readError || error}/>
      {!thread && !readError && <Loader text="正在读取任务状态…"/>}
      {visibleReceipt && <OriginalCodexReceipt receipt={visibleReceipt}/>}
      {thread && <section aria-label="最新任务状态">
        {visibleReceipt && <><strong>最新任务状态</strong>{latestId && <p>请求编号 <code>{latestId}</code></p>}</>}
        <CodexStatus thread={thread} sending={sending} pending={Boolean(pending && !visibleReceipt)} requestId={sendingId || pending} preparing={action === '/thread'}/>
      </section>}
      {pending && memoryOnly && <p className="section-help">浏览器仅在当前页面保留请求编号；刷新后可读取服务端最新任务状态。</p>}
      <Field label="任务内容">
        <textarea rows={4} maxLength={20000} value={draft} disabled={sending || Boolean(operation?.request_id) || Boolean(active) || Boolean(thread?.recovery_required)}
          onChange={e => setDraft(e.target.value)}/>
      </Field>
      <div className="task-sidebar-actions">
        <Button busy={sending} disabled={!canSend} onClick={() => void send()}>{sameRequestRetry && !sending && controls?.send ? '用原编号重试' : '发送任务'}</Button>
        {controls?.prepare && !sending && <Button variant="secondary"
          busy={action === '/thread'} disabled={Boolean(action || readError)}
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
