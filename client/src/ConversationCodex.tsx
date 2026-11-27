import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { SquareTerminal } from 'lucide-react';
import { ApiError, api, clientRequestId, errorText, post } from './api';
import { CodexSteerReceipts, codexAccessDenied, steerableTurn, useCodexSteers } from './CodexSteers';
import { CodexDraftImport, type ChatEngineeringEntry } from './CodexDraftImport';
import { useCodexDraft, type DraftAttempt } from './codexDraft';
import { Button, ErrorNotice, Field, Loader, Modal } from './components';

type FileReads = { succeeded: number; failed: number; unconfirmed: number };
export type CodexTurn = { file_reads?: FileReads; turn_id: string; request_id?: string; status: string; text?: string; text_truncated: boolean; text_withheld?: boolean };
type Operation = { request_id: string | null; phase: 'preparing' | 'thread_ready' | 'running' | 'output'; file_read_started?: boolean; text?: string };
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

function FileReadResult({ reads }: { reads?: FileReads }) {
  if (!reads) return null;
  const { succeeded, failed, unconfirmed } = reads;
  if (succeeded + failed + unconfirmed === 0) return <p>本轮未记录可辨认的文件读取命令。</p>;
  return <p>文件读取命令成功 {succeeded} 次，失败 {failed} 次{unconfirmed > 0 && `，结果未确认 ${unconfirmed} 次`}。</p>;
}

export function CodexTurnResult({ turn }: { turn: CodexTurn }) {
  return <>
    <p><strong>{statusLabels[turn.status] || turn.status}</strong></p>
    <FileReadResult reads={turn.file_reads}/>
    {turn.text_withheld ? <p className="section-help">正文含宿主路径或凭据标记，已隐藏。{turn.text_truncated && '留存的正文已截断。'}</p> : <>
      {turn.text && <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{turn.text}</p>}
      {turn.text_truncated && <p className="section-help">{turn.text ? '仅显示此请求留存的部分正文。' : '仅留存轮次状态，正文未保存。'}</p>}
    </>}
  </>;
}

export function OriginalCodexReceipt({ receipt, hasRetrySnapshot = false }: { receipt: CodexReceipt; hasRetrySnapshot?: boolean }) {
  const { state, turn } = receipt;
  return <section aria-label="原请求回执" className="section-help">
    <strong>原请求回执</strong>
    <p>请求编号 <code>{receipt.request_id}</code></p>
    {state === 'recorded' && turn && <CodexTurnResult turn={turn}/>}
    {state === 'admitted_without_receipt' && <p>开物曾登记此请求，已无可读回执，无法找回正文或确认最终状态。可以另行发送新任务。</p>}
    {state === 'not_found' && <p>暂时查不到这个编号，执行结果仍未确认。{hasRetrySnapshot
      ? '已从本浏览器找回提交原文，可刷新核对或用原编号重试。' : '可刷新状态，或输入原内容后用原编号重试。'}</p>}
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
    {!terminal && operation?.file_read_started && <p>原生执行器已报告文件读取命令。</p>}
    {!terminal && operation?.phase === 'output' && operation.text && <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{operation.text}</p>}
    {!terminal && pending && !sending && !operation && !active && !thread.recovery_required &&
      <p>刷新状态可核对进度。如需重试，请使用原任务内容和原请求编号。</p>}
    {!terminal && outstanding && last && (last.text || last.text_truncated || last.file_reads) && <p>上一轮结果</p>}
    {last && <FileReadResult reads={last.file_reads}/>}
    {last?.text && <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{last.text}</p>}
    {last?.text_truncated && <p>{last.text ? '仅显示已保存的部分结果。' : '仅恢复了轮次状态，正文未保存。'}</p>}
  </div>;
}

type CodexDialogProps = { conversationId: string; ownerId: string; chatAttachments?: ReactNode; hasChatSkills?: boolean; entry?: ChatEngineeringEntry | null; onClose: () => void };
function CodexDialog({ conversationId, ownerId, chatAttachments, hasChatSkills, entry, onClose }: CodexDialogProps) {
  const path = '/conversations/' + encodeURIComponent(conversationId) + '/codex';
  const storageKey = codexRequestKey(ownerId, conversationId);
  const [thread, setThread] = useState<CodexThread | null>(null);
  const [attemptInEditor, setAttemptInEditor] = useState<string | null>(null);
  const [checkingEntry, setCheckingEntry] = useState(Boolean(entry?.text));
  const entryChecked = useCallback(() => setCheckingEntry(false), []);
  const [pending, setPending] = useState(() => readRequest(storageKey));
  const [receipt, setReceipt] = useState<CodexReceipt | null>(null);
  const [memoryOnly, setMemoryOnly] = useState(false);
  const sentText = useRef<string | null>(null);
  const [sendingId, setSendingId] = useState<string | null>(null);
  const sending = Boolean(sendingId);
  const [action, setAction] = useState('');
  const [error, setError] = useState('');
  const [readError, setReadError] = useState('');
  const [offline, setOffline] = useState(false);
  const [accessError, setAccessError] = useState('');
  const denyAccess = useCallback((cause: unknown) => setAccessError(errorText(cause)), []);
  const [identity, setIdentity] = useState<'checking' | 'owner' | 'unavailable'>('checking');
  const identityVersion = useRef(0);
  const currentOwner = useRef(false);
  const verifyOwner = useCallback(async () => {
    const version = ++identityVersion.current;
    currentOwner.current = false;
    setIdentity('checking');
    try {
      const user = await api<{ id: string }>('/auth/me', { cache: 'no-store' });
      if (version !== identityVersion.current) return;
      if (user.id !== ownerId) { denyAccess(new Error('登录账号已改变，请刷新页面后重新进入工程任务。')); return; }
      currentOwner.current = true;
      setIdentity('owner');
    } catch (cause) {
      if (version !== identityVersion.current) return;
      if (codexAccessDenied(cause)) denyAccess(cause);
      else setIdentity('unavailable');
    }
  }, [ownerId, denyAccess]);
  useEffect(() => {
    const focus = () => { void verifyOwner(); };
    const visible = () => {
      if (document.hidden) { identityVersion.current++; currentOwner.current = false; setIdentity('checking'); }
      else focus();
    };
    focus();
    window.addEventListener('focus', focus);
    document.addEventListener('visibilitychange', visible);
    return () => {
      identityVersion.current++;
      window.removeEventListener('focus', focus);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [verifyOwner]);
  const { state: saved, store } = useCodexDraft(ownerId, conversationId, Boolean(thread && !accessError && identity === 'owner'));
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
    void store.settle(id);
    sentText.current = null;
  }, [storageKey, store]);

  const refresh = useCallback(async () => {
    const version = ++generation.current;
    try {
      const result = await api<CodexThread>(path, { cache: 'no-store' });
      if (version !== generation.current) return;
      setThread(result);
      setReadError('');
      setOffline(false);
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
        void store.settle(id);
        sentText.current = null;
      }
    } catch (cause) {
      if (codexAccessDenied(cause)) denyAccess(cause);
      if (version === generation.current) {
        setReadError(errorText(cause));
        setOffline(cause instanceof ApiError && cause.status === 0);
      }
    }
  }, [path, finish, denyAccess, store]);

  useEffect(() => {
    void refresh();
    return () => { generation.current++; };
  }, [refresh]);

  const confirmedTurn = thread?.last_turn?.request_id || null;
  const original = saved.attempts.find(row => row.kind === 'turn' && row.id !== confirmedTurn && row.id !== thread?.active_turn?.request_id);
  useEffect(() => {
    if (!original || !thread || accessError || pendingId.current || requestInFlight.current) return;
    pendingId.current = original.id;
    setPending(original.id);
    void refresh();
  }, [original?.id, thread, accessError, refresh]);

  const active = thread?.active_turn;
  const operation = thread?.operation;
  const resolved = originalRequestResolved(receipt);
  const waiting = Boolean(pending && (!receipt || receipt.state === 'pending'));
  const poll = !accessError && Boolean(sending || waiting || action === '/thread' || operation || active?.can_interrupt);
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
    if (actionInFlight.current || (route === '/interrupt' && steers.inFlight.current) || (route === '/thread' && (requestInFlight.current || operation))) return;
    actionInFlight.current = true;
    setAction(route);
    setError('');
    try { await post(path + route, payload); }
    catch (cause) { if (codexAccessDenied(cause)) denyAccess(cause); else setError(errorText(cause)); }
    finally { await refresh(); actionInFlight.current = false; setAction(''); }
  }

  const steers = useCodexSteers(path, storageKey, Boolean(thread && !readError && !accessError), denyAccess);
  const steerTarget = !action && !readError ? steerableTurn(thread) : null;
  useEffect(() => {
    for (const attempt of saved.attempts) {
      if (attempt.kind === 'turn' && (attempt.id === confirmedTurn || attempt.id === active?.request_id)) void store.settle(attempt.id);
      if (attempt.kind === 'steer' && steers.receipts.some(row => row.state === 'delivered'
        && row.client_request_id === attempt.id && row.turn_id === attempt.turn)) void store.settle(attempt.id);
    }
  }, [saved.attempts, steers.receipts, confirmedTurn, active?.request_id, store]);
  const controls = thread && codexControls(thread);
  const visibleReceipt = sending && !resolved ? null : receipt;
  const latestId = operation?.request_id || active?.request_id || thread?.last_turn?.request_id;
  const sameRequestRetry = Boolean(pending && receipt?.state === 'not_found');
  const retrySnapshot = sameRequestRetry && original?.id === pending ? original : undefined;
  const busyWithTurn = Boolean(sending || operation?.request_id || active || thread?.recovery_required);
  const editingNext = Boolean(active?.can_interrupt || (busyWithTurn && saved.text));
  const echo = saved.attempts.find(row => row.id === attemptInEditor && row.kind === 'steer');
  const input = saved.text || echo?.text || '';
  const canSend = saved.ready && controls?.send && (!pending || resolved || sameRequestRetry) && !readError && !action && !sending
    && Boolean((retrySnapshot?.text ?? saved.text).trim()) && (!original || Boolean(retrySnapshot));
  function editDraft(text: string) {
    if (!currentOwner.current) return;
    setAttemptInEditor(null);
    void store.edit(saved.revision, text);
  }
  function submitSteer() {
    if (!currentOwner.current || !steerTarget || !saved.text.trim() || actionInFlight.current) return;
    void steers.submit(steerTarget, saved.text, async id => {
      setAttemptInEditor(id.client_request_id);
      return store.submit(saved.revision, { kind: 'steer', id: id.client_request_id, turn: id.turn_id, text: saved.text });
    });
  }

  async function send() {
    if (!currentOwner.current || !canSend || !thread || requestInFlight.current || actionInFlight.current) return;
    requestInFlight.current = true;
    const id = (sameRequestRetry && pending) || clientRequestId();
    const text = retrySnapshot?.text ?? saved.text;
    try {
      if (!retrySnapshot && !await store.submit(saved.revision, { kind: 'turn', id, text })) return;
      if (!store.sessionCurrent()) return;
      const { stored } = keepCodexRequestId(storageKey, id);
      sentText.current = text;
      setMemoryOnly(!stored);
      setReceipt(null);
      pendingId.current = id;
      setPending(id);
      setSendingId(id);
      setError('');
      await post<CodexTurn>(path + '/turn', codexTurnPayload(thread, id, text));
    } catch (cause) {
      if (codexAccessDenied(cause)) denyAccess(cause);
      else if (pendingId.current === id) setError(errorText(cause));
    } finally { await refresh(); requestInFlight.current = false; setSendingId(null); }
  }

  const unresolvedImport = Boolean((pending && !resolved) || saved.attempts.some(attempt =>
    attempt.kind === 'turn' ? attempt.id !== confirmedTurn :
      !steers.receipts.some(row => row.client_request_id === attempt.id && row.turn_id === attempt.turn && row.state !== 'uncertain')));
  const waitingForImport = checkingEntry && !entry?.ordinaryPending && !(saved.ready &&
    (saved.attempts.length || pending || busyWithTurn || (saved.text && saved.text !== entry?.text)));
  function attemptLabel(attempt: DraftAttempt) {
    if (attempt.kind === 'turn') {
      return attempt.id === active?.request_id ? '本轮已提交的原文' : '任务已尝试提交，结果待核对';
    }
    const found = steers.receipts.find(row => row.client_request_id === attempt.id && row.turn_id === attempt.turn);
    return found?.state === 'closed' || found?.state === 'rejected' ? '补充未送达的原文' : '补充已尝试提交，送达待核对';
  }

  if (accessError || saved.revoked) return <Modal title="Codex 工程任务" onClose={onClose}>
    <div className="modal-body"><ErrorNotice message={accessError || '登录状态已改变，请刷新页面后重新进入工程任务。'}/></div>
  </Modal>;

  if (identity !== 'owner') return <Modal title="Codex 工程任务" onClose={onClose}>
    <div className="modal-body">{identity === 'checking' ? <Loader text="正在核对当前账号…"/>
      : <><ErrorNotice message="暂时无法核对当前账号，请联网后刷新。"/><Button variant="secondary" onClick={() => void verifyOwner()}>刷新状态</Button></>}</div>
  </Modal>;
  if (!thread) return <Modal title="Codex 工程任务" onClose={onClose}>
    <div className="modal-body">{readError ? <><ErrorNotice message={readError}/>
      <Button variant="secondary" onClick={() => void refresh()}>刷新状态</Button></> : <Loader text="正在读取任务状态…"/>}</div>
  </Modal>;
  return <Modal title="Codex 工程任务" onClose={onClose}>
    <div className="modal-body">
      <ErrorNotice message={readError || error}/>
      {visibleReceipt && <OriginalCodexReceipt receipt={visibleReceipt} hasRetrySnapshot={Boolean(retrySnapshot)}/>}
      {thread && <section aria-label="最新任务状态">
        {visibleReceipt && <><strong>最新任务状态</strong>{latestId && <p>请求编号 <code>{latestId}</code></p>}</>}
        <CodexStatus thread={thread} sending={sending} pending={Boolean(pending && !visibleReceipt)} requestId={sendingId || pending} preparing={action === '/thread'}/>
      </section>}
      {pending && memoryOnly && <p className="section-help">浏览器仅在当前页面保留请求编号；刷新后可读取服务端最新任务状态。</p>}
      {thread && (!readError || offline) && <CodexSteerReceipts {...steers} offline={offline} activeTurn={active?.turn_id} lastTurn={thread.last_turn?.turn_id}/>}
      {saved.attempts.length > 0 && <section aria-label="已尝试提交的原文" className="section-help">
        {saved.attempts.map(attempt => <div key={attempt.id}>
          <strong>{attemptLabel(attempt)}</strong>
          <p>请求编号 <code>{attempt.id}</code></p>
          <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{attempt.text}</p>
        </div>)}
      </section>}
      {chatAttachments}
      {hasChatSkills && <p className="section-help">已选 Skill 保留在普通聊天中。工程任务只发送文字。</p>}
      {entry && <CodexDraftImport entry={entry} ready={saved.ready} onChecked={entryChecked}
        occupied={Boolean(saved.attempts.length || pending || busyWithTurn)}
        canEdit={!readError && !offline && !saved.revoked && !unresolvedImport && !thread.recovery_required}
        revision={saved.revision} text={saved.text} edit={store.edit}/>}
      {editingNext && active && active.request_id === sendingId && sentText.current && <details>
        <summary>查看本轮已发送的要求</summary>
        <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{sentText.current}</p>
      </details>}
      {waitingForImport ? <Loader text="正在核对并带入工程草稿…"/> : <Field label={steerTarget && editingNext ? '补充要求或下一轮任务内容' : editingNext && busyWithTurn ? '下一轮任务内容' : '任务内容'}
        hint={saved.memoryOnly ? '此浏览器暂时无法保存草稿。当前编辑仅在本页保留。'
          : steerTarget && editingNext ? '草稿保存在本浏览器。点击「补充到当前任务」交给本轮执行，或等本轮结束后另行发送。'
          : retrySnapshot ? '用原编号重试会提交上方的原文。新写的草稿单独保留。' : '未发送的草稿保存在本浏览器，仅在本人此会话中恢复。'}>
        <textarea rows={4} maxLength={20000} value={input} disabled={!saved.ready || (busyWithTurn && !editingNext)}
          onChange={e => editDraft(e.target.value)}/>
      </Field>}
      {saved.submitFailed && <p role="alert">未能保存这次提交的编号，本次没有发送。草稿仍在，可再次提交。</p>}
      {saved.conflict && <p className="section-help">另一窗口已更新草稿，请核对最新内容。</p>}
      <div className="task-sidebar-actions">
        {saved.text !== '' && <Button variant="secondary" onClick={() => editDraft('')}>清空草稿</Button>}
        <Button busy={sending && !editingNext} disabled={checkingEntry || !canSend} onClick={() => void send()}>{sameRequestRetry && !sending && controls?.send ? '用原编号重试' : '发送任务'}</Button>
        {steerTarget && editingNext && <Button variant="secondary" disabled={!saved.text.trim() || Boolean(steers.submitting)}
          onClick={submitSteer}>补充到当前任务</Button>}
        {controls?.prepare && !sending && <Button variant="secondary"
          busy={action === '/thread'} disabled={Boolean(action || readError)}
          onClick={() => void perform('/thread')}>准备会话</Button>}
        {active?.can_interrupt && <Button disabled={Boolean(action || steers.submitting || readError) || active.interrupt_requested} variant="danger"
          onClick={() => void perform('/interrupt', { turn_id: active.turn_id })}>
          {action === '/interrupt' ? '正在请求取消' : '取消当前任务'}</Button>}
        {controls?.reconcile && !sending && <Button busy={action === '/reconcile'}
          disabled={Boolean(action || readError)} onClick={() => void perform('/reconcile')}>核对并结束中断轮次</Button>}
        <Button variant="secondary" onClick={() => { void refresh(); void steers.refresh(); }}>刷新状态</Button>
      </div>
    </div>
  </Modal>;
}

type EntryProps = Omit<CodexDialogProps, 'onClose'> & { unavailable: boolean; onDismissEntry?: () => void };
export default function ConversationCodex({ unavailable, onDismissEntry, ...dialog }: EntryProps) {
  const [open, setOpen] = useState(false);
  return <>
    <button type="button" className="icon-button" title="Codex 工程任务" aria-label="Codex 工程任务"
      disabled={unavailable} onClick={() => setOpen(true)}><SquareTerminal size={17}/></button>
    {(open || dialog.entry) && !unavailable && <CodexDialog key={codexRequestKey(dialog.ownerId, dialog.conversationId)}
      {...dialog} onClose={() => { setOpen(false); onDismissEntry?.(); }}/>}
  </>;
}
