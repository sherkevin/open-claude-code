import { useEffect, useRef, useState } from 'react';
import { Users } from 'lucide-react';
import { ApiError, api, clientRequestId, errorText, post } from './api';
import { Button, ErrorNotice, Field, Loader, Modal, SuccessNotice } from './components';
import { waitForRead } from './runReadback';
import './team-collaboration.css';

export type NativeTeamTask = {
  id: string; revision: number; subject: string; description: string;
  status: 'pending' | 'in_progress' | 'completed';
  blockedBy: string[]; writeScopes: string[];
  ownerId?: string | null; ownerName?: string | null;
  ready: boolean; writeScopeWarnings: string[];
};
export type TeamBoard = { enabled: boolean; tasks: NativeTeamTask[] };
export type TeamAuthorization = {
  id: string; harness_session_id: string; assistant_revision: number;
  permission_mode: string; max_operations: number; expires_in_seconds: number;
  status: 'active' | 'revoked'; created_at: string; expires_at: string | null;
  revoked_at?: string | null; revoked_reason?: string | null;
};
export type TeamOperation = {
  id: string; client_request_id: string;
  action: 'enable' | 'disable' | 'create' | 'claim' | 'complete';
  status: 'pending' | 'confirmed' | 'rejected' | 'uncertain';
  error_code?: string | null; task_id?: string | null;
  task_revision?: number | null; task_status?: string | null;
  board_enabled?: boolean | null; acknowledged_at?: string | null;
  created_at: string; updated_at: string;
};
export type TeamCollaboration = {
  schema: 'team-collaboration/v1'; harness_session_id: string | null;
  authorization: TeamAuthorization | null; authorization_active: boolean;
  session_matches: boolean; permission_matches: boolean;
  operations_used: number; operations_remaining: number | null;
  operations: TeamOperation[]; unresolved_operation: TeamOperation | null;
  execution_enabled: false; task_execution_enabled: false;
  subagent_execution_enabled: false;
};
type TeamRead = { board: TeamBoard; execution_enabled: false };
type TeamTaskSummary = { id: string; revision: number | null; status: string | null };
type TeamTaskResponse = {
  schema: 'team-task-response/v1'; operation: TeamOperation;
  board: TeamBoard | null; task: TeamTaskSummary | null;
  execution_enabled: false; replayed: boolean;
};
type TaskAction = 'claim' | 'complete';

const TASK_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const statusLabels: Record<NativeTeamTask['status'], string> = {
  pending: '待认领', in_progress: '进行中', completed: '已完成',
};
const actionLabels: Record<TeamOperation['action'], string> = {
  enable: '启用任务板', disable: '停用任务板', create: '创建任务',
  claim: '认领任务', complete: '完成任务',
};
const operationLabels: Record<TeamOperation['status'], string> = {
  pending: '进行中', confirmed: '已确认', rejected: '已拒绝', uncertain: '结果未确认',
};

export function parseWriteScopes(value: string): string[] {
  const result: string[] = [];
  for (const item of value.split(/[\n,]/)) {
    const text = item.trim();
    if (!text) continue;
    if (text.length > 300 || /[\x00-\x1f\x7f]/.test(text)) throw new Error('写入范围格式无效。');
    if (!result.includes(text)) result.push(text);
  }
  if (result.length > 64) throw new Error('写入范围最多 64 项。');
  return result;
}

export function teamTaskDraft(subject: string, description: string,
                               blockedBy: string[], writeScopes: string[]) {
  const title = subject.trim(), body = description.trim();
  if (!title || title.length > 200) throw new Error('任务标题需为 1–200 个字符。');
  if (!body || body.length > 4000) throw new Error('任务说明需为 1–4000 个字符。');
  if (blockedBy.length > 64 || blockedBy.some(id => !TASK_ID.test(id))) throw new Error('依赖任务无效。');
  if (writeScopes.length > 64 || writeScopes.some(item => !item.trim() || item.length > 300)) {
    throw new Error('写入范围无效。');
  }
  return {
    subject: title, description: body,
    blocked_by: [...new Set(blockedBy)], write_scopes: [...new Set(writeScopes.map(item => item.trim()))],
  };
}

export function teamTransition(task: NativeTeamTask, action: TaskAction) {
  if (!task || !TASK_ID.test(task.id) || !Number.isInteger(task.revision) || task.revision < 1) {
    throw new Error('任务版本无效，请刷新任务板。');
  }
  // The action is carried by the endpoint path; the body only carries the
  // exact CAS revision and the idempotency fields.
  void action;
  return { expected_revision: task.revision };
}

export function operationLabel(operation: TeamOperation): string {
  return `${actionLabels[operation.action]} · ${operationLabels[operation.status]}`;
}

function taskSort(tasks: NativeTeamTask[]): NativeTeamTask[] {
  const rank = { in_progress: 0, pending: 1, completed: 2 } as const;
  return [...tasks].sort((a, b) => rank[a.status] - rank[b.status] || a.id.localeCompare(b.id));
}

export function TeamBoardView({ board }: { board: TeamBoard }) {
  return <div className="team-board-view">
    <p className="section-help">
      任务板当前{board.enabled ? '已启用' : '未启用'}，共 {board.tasks.length} 项。
      依赖任务完成前，下游任务不会被标记为可认领。
    </p>
    {board.tasks.length === 0 ? <p className="section-help">尚无任务。</p> :
      <ul className="team-task-list">
        {taskSort(board.tasks).map(task => <li key={task.id} className="team-task-item" data-status={task.status}>
          <div className="team-task-heading">
            <strong>{task.subject}</strong>
            <span className="team-task-status">{statusLabels[task.status]}</span>
          </div>
          <p>{task.description}</p>
          <div className="team-task-meta">
            <span>版本 {task.revision}</span>
            <span>{task.ready ? '依赖已满足' : '依赖未完成'}</span>
            {task.ownerName && <span>认领人 {task.ownerName}</span>}
            {task.blockedBy.length > 0 && <span>依赖 {task.blockedBy.length} 项</span>}
            {task.writeScopes.length > 0 && <span>写入范围 {task.writeScopes.join('、')}</span>}
          </div>
          {task.writeScopeWarnings.length > 0 && <p className="team-task-warning">{task.writeScopeWarnings.join('；')}</p>}
        </li>)}
      </ul>}
  </div>;
}

async function readNativeBoard(path: string, signal: AbortSignal): Promise<TeamBoard> {
  // An earlier native read can still own the actor after its browser tab closes.
  const delays = [400, 1000, 2000, 3500];
  for (let attempt = 0; ; attempt++) {
    try {
      const value = await api<TeamRead>(path, { signal, cache: 'no-store' });
      if (value.execution_enabled !== false) throw new Error('任务板回执不匹配，请刷新。');
      return value.board;
    } catch (error) {
      if (signal.aborted || !(error instanceof ApiError) || error.code !== 'TEAM_SESSION_BUSY'
          || attempt === delays.length) throw error;
      await waitForRead(delays[attempt], signal);
    }
  }
}

function TeamDialog({ conversationId, unavailable, onClose }:
  { conversationId: string; unavailable: boolean; onClose: () => void }) {
  const base = `/conversations/${encodeURIComponent(conversationId)}/team`;
  const [board, setBoard] = useState<TeamBoard | null>(null);
  const [collab, setCollab] = useState<TeamCollaboration | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [readError, setReadError] = useState('');
  const [notice, setNotice] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [maxOperations, setMaxOperations] = useState(20);
  const [minutes, setMinutes] = useState(60);
  const [confirmed, setConfirmed] = useState(false);
  const [subject, setSubject] = useState('');
  const [description, setDescription] = useState('');
  const [blockedBy, setBlockedBy] = useState<string[]>([]);
  const [scopeText, setScopeText] = useState('');
  const pending = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setReadError(''); setBoard(null); setCollab(null);
    const options = { signal: controller.signal, cache: 'no-store' } as const;
    const collaborationRead = api<TeamCollaboration>(base + '/collaboration', options).then(value => {
      if (value.schema !== 'team-collaboration/v1' || value.execution_enabled !== false
          || value.task_execution_enabled !== false) throw new Error('协作授权回执不匹配，请刷新。');
      return value;
    });
    const boardRead = unavailable ? Promise.resolve(null) : readNativeBoard(base, controller.signal);
    void Promise.allSettled([collaborationRead, boardRead]).then(([collaboration, read]) => {
      if (controller.signal.aborted) return;
      const errors = [];
      if (collaboration.status === 'fulfilled') setCollab(collaboration.value);
      else errors.push('读取协作授权失败：' + errorText(collaboration.reason));
      if (read.status === 'fulfilled') setBoard(read.value);
      else errors.push('读取任务板失败：' + errorText(read.reason));
      setReadError(errors.join(' '));
      setLoading(false);
    });
    return () => controller.abort();
  }, [base, refresh, unavailable]);

  async function run(action: () => Promise<string | void>, allowWhileBusy = false) {
    if (pending.current || (unavailable && !allowWhileBusy)) return;
    pending.current = true; setBusy(true); setError(''); setNotice('');
    try {
      const message = await action();
      if (message) setNotice(message);
      setRefresh(value => value + 1);
    } catch (reason) {
      setError(errorText(reason) + ' 请刷新任务板并核对状态后再操作。');
      setRefresh(value => value + 1);
    } finally {
      pending.current = false; setBusy(false);
    }
  }

  function needSession(): string {
    if (!collab?.harness_session_id) throw new Error('原生会话尚未建立，请先完成一次对话。');
    return collab.harness_session_id;
  }

  function applyMutation(result: TeamTaskResponse) {
    if (result.schema !== 'team-task-response/v1' || result.execution_enabled !== false) {
      throw new Error('任务板操作回执不匹配，请刷新核对。');
    }
    if (result.board) setBoard(result.board);
    return result.replayed ? '该请求已处理，未重复修改任务板。' : null;
  }

  async function authorize() {
    if (!confirmed || !collab?.harness_session_id) return;
    if (!Number.isInteger(maxOperations) || maxOperations < 1 || maxOperations > 100
        || !Number.isInteger(minutes) || minutes < 1 || minutes > 10080) {
      setError('操作次数需为 1–100，有效期需为 1–10080 分钟。'); return;
    }
    await run(async () => {
      const session = needSession();
      const result = await post<TeamCollaboration>(base + '/authorization', {
        harness_session_id: session, max_operations: maxOperations,
        expires_in_seconds: minutes * 60, confirm: true,
      });
      setCollab(result); setConfirmed(false);
      return '已记录本次任务板协作授权。';
    });
  }

  async function revoke() {
    if (!window.confirm('撤销任务板协作授权？任务板和历史会保留，之后不能再创建、认领或完成任务。')) return;
    await run(async () => {
      const result = await api<TeamCollaboration>(base + '/authorization', { method: 'DELETE' });
      setCollab(result); setConfirmed(false);
      return '授权已撤销，任务板保持原状。';
    }, true);
  }

  async function changeBoard(action: 'enable' | 'disable') {
    if (!window.confirm(action === 'enable' ? '启用官方原生任务板？此操作不会启动模型或子智能体。' : '停用官方原生任务板？已有任务和历史会保留。')) return;
    await run(async () => {
      const session = needSession();
      const result = await post<TeamTaskResponse>(base + '/board', {
        harness_session_id: session, client_request_id: clientRequestId(),
        action, confirm: true,
      });
      return applyMutation(result) || (action === 'enable' ? '任务板已启用。' : '任务板已停用。');
    });
  }

  async function createTask() {
    await run(async () => {
      const session = needSession();
      const draft = teamTaskDraft(subject, description, blockedBy, parseWriteScopes(scopeText));
      const result = await post<TeamTaskResponse>(base + '/tasks', {
        harness_session_id: session, client_request_id: clientRequestId(), ...draft,
      });
      const message = applyMutation(result);
      setSubject(''); setDescription(''); setBlockedBy([]); setScopeText('');
      return message || '任务已创建。';
    });
  }

  async function transition(task: NativeTeamTask, action: TaskAction) {
    if (action === 'complete' && !window.confirm('确认该任务已完成？此操作只更新任务板状态。')) return;
    await run(async () => {
      const session = needSession();
      const result = await post<TeamTaskResponse>(
        `${base}/tasks/${encodeURIComponent(task.id)}/${action}`,
        { harness_session_id: session, client_request_id: clientRequestId(), ...teamTransition(task, action) });
      return applyMutation(result) || (action === 'claim' ? '任务已认领。' : '任务已完成。');
    });
  }

  async function acknowledge(operation: TeamOperation) {
    await run(async () => {
      const result = await post<{ schema: 'team-operation-response/v1'; operation: TeamOperation; execution_enabled: false }>(
        `${base}/operations/${encodeURIComponent(operation.id)}/acknowledge`);
      if (result.schema !== 'team-operation-response/v1' || result.execution_enabled !== false) {
        throw new Error('确认回执不匹配，请刷新。');
      }
      return '已确认任务板当前状态，可以继续操作。';
    });
  }

  const authorization = collab?.authorization || null;
  const active = Boolean(collab?.authorization_active && collab?.session_matches && collab?.permission_matches);
  const blocked = unavailable || busy || !active;
  const unresolved = collab?.unresolved_operation || null;
  const tasks = board?.tasks || [];
  const candidates = tasks.filter(task => task.status !== 'completed');
  return <Modal title="团队任务板（协作试点）" wide onClose={() => { if (!pending.current) onClose(); }}
    description="只复用官方原生任务板和持久 JSONL 投影；不启动模型、子智能体、shell 或业务写入。">
    <div className="modal-body">
      <ErrorNotice message={readError}/>
      <ErrorNotice message={error}/>
      <SuccessNotice message={notice}/>
      {loading ? <Loader text="正在读取原生任务板…"/> : <>
        {unavailable && <p className="section-help">当前会话有任务运行或已归档，只能查看，不能修改任务板。</p>}
        <p className="section-help team-boundary">
          此面板只维护原生任务板元数据：创建、认领和完成不会唤醒助手、启动子智能体、执行 shell 或写入业务系统。
          授权是宿主侧显式、限次、限时的任务板操作许可，不等于执行许可。
        </p>
        {collab && !collab.session_matches && <p className="notice error">原生会话已变化，当前授权不再适用。请撤销后重新授权。</p>}
        {collab && collab.session_matches && !collab.permission_matches && <p className="notice error">助手或会话权限已变化，当前授权不再适用。</p>}
        {unresolved && <div className="notice error" role="alert">
          <span>上次任务板操作结果未确认，继续操作前请先核对下方官方任务板，再确认你知道当前状态。</span>
          <Button variant="secondary" disabled={busy} onClick={() => void acknowledge(unresolved)}>我已核对任务板</Button>
        </div>}

        <section className="team-section">
          <h3>授权</h3>
          {authorization ? <div className="team-authorization">
            <p>
              状态：{authorization.status === 'active' ? (collab?.authorization_active ? '生效中' : '已失效') : '已撤销'}
              ，已用 {collab?.operations_used ?? 0} / {authorization.max_operations} 次
              {collab && collab.operations_remaining !== null && <>，剩余 {collab.operations_remaining} 次</>}
            </p>
            <p>有效期至 {authorization.expires_at ? new Date(authorization.expires_at).toLocaleString('zh-CN') : '无法核验'}{authorization.revoked_reason ? `；撤销原因：${authorization.revoked_reason}` : ''}</p>
            {authorization.status === 'active' && <Button variant="danger" disabled={busy} onClick={() => void revoke()}>撤销授权</Button>}
          </div> : null}
          {(!authorization || authorization.status !== 'active') && <div className="team-authorization">
            <Field label="本次授权最多操作次数" hint="创建、认领、完成或启停任务板各计一次；结果未确认的操作也占用一次。">
              <input type="number" min={1} max={100} value={maxOperations} disabled={busy || unavailable} onChange={event => setMaxOperations(Number(event.target.value))}/>
            </Field>
            <Field label="授权有效期（分钟）" hint="最短 1 分钟，最长 7 天。">
              <input type="number" min={1} max={10080} value={minutes} disabled={busy || unavailable} onChange={event => setMinutes(Number(event.target.value))}/>
            </Field>
            <label className="checkbox-row">
              <input type="checkbox" checked={confirmed} disabled={busy || unavailable} onChange={event => setConfirmed(event.target.checked)}/>
              我明确授权本人当前空闲会话执行限次任务板元数据操作；这不授权模型、子智能体或业务写入。
            </label>
            <Button busy={busy} disabled={busy || unavailable || !confirmed || !collab?.harness_session_id} onClick={() => void authorize()}>记录任务板授权</Button>
          </div>}
        </section>

        <section className="team-section">
          <h3>官方任务板</h3>
          {board ? <>
            <div className="task-sidebar-actions">
              {board.enabled
                ? <Button variant="secondary" disabled={blocked} onClick={() => void changeBoard('disable')}>停用任务板</Button>
                : <Button disabled={blocked} onClick={() => void changeBoard('enable')}>启用任务板</Button>}
              <span className="section-help">启停只改变原生任务板开关，任务和历史保留。</span>
            </div>
            <TeamBoardView board={board}/>
            {board.enabled && <div className="team-task-actions">
              {taskSort(tasks).map(task => <div key={task.id} className="team-task-action-row">
                <span>{task.subject}</span>
                {task.status === 'pending' && <Button variant="secondary" disabled={blocked || !task.ready} title={task.ready ? '' : '依赖任务尚未完成'} onClick={() => void transition(task, 'claim')}>认领</Button>}
                {task.status === 'in_progress' && <Button variant="secondary" disabled={blocked} onClick={() => void transition(task, 'complete')}>完成</Button>}
              </div>)}
            </div>}
          </> : <p className="section-help">任务板暂不可读取。</p>}
        </section>

        <section className="team-section">
          <h3>创建任务</h3>
          <Field label="任务标题" hint="1–200 个字符；只作为任务板元数据，不会触发执行。">
            <input value={subject} maxLength={200} disabled={blocked || !board?.enabled} onChange={event => setSubject(event.target.value)}/>
          </Field>
          <Field label="任务说明" hint="1–4000 个字符。">
            <textarea rows={3} value={description} maxLength={4000} disabled={blocked || !board?.enabled} onChange={event => setDescription(event.target.value)}/>
          </Field>
          <Field label="依赖任务" hint="选择必须先完成的上游任务；依赖未满足时不能认领。">
            <select multiple size={Math.min(5, Math.max(2, candidates.length))} value={blockedBy}
              disabled={blocked || !board?.enabled || candidates.length === 0}
              onChange={event => setBlockedBy(Array.from(event.target.selectedOptions, option => option.value))}>
              {candidates.map(task => <option key={task.id} value={task.id}>{task.subject}（{statusLabels[task.status]}）</option>)}
            </select>
          </Field>
          <Field label="写入范围（可选）" hint="逗号或换行分隔，最多 64 项；仅作为任务板提示，不是文件写权限。">
            <textarea rows={2} value={scopeText} disabled={blocked || !board?.enabled} onChange={event => setScopeText(event.target.value)}/>
          </Field>
          <Button busy={busy} disabled={blocked || !board?.enabled || !subject.trim() || !description.trim()} onClick={() => void createTask()}>创建任务</Button>
        </section>

        <section className="team-section">
          <h3>操作历史</h3>
          {!collab ? <p className="section-help">操作历史暂不可读取。</p> : !collab.operations.length ? <p className="section-help">尚无任务板操作记录。</p> :
            <ul className="team-operation-list">
              {collab.operations.slice(0, 20).map(operation => <li key={operation.id} data-status={operation.status}>
                <strong>{operationLabel(operation)}</strong>
                <small>{new Date(operation.created_at).toLocaleString('zh-CN')}
                  {operation.task_id ? ` · 任务 ${operation.task_id} v${operation.task_revision ?? '?'}` : ''}
                  {operation.error_code ? ` · ${operation.error_code}` : ''}
                  {operation.acknowledged_at ? ' · 已核对' : ''}
                </small>
                {operation.status === 'uncertain' && !operation.acknowledged_at &&
                  <Button variant="secondary" disabled={busy || unavailable || !board} onClick={() => void acknowledge(operation)}>核对并确认</Button>}
              </li>)}
            </ul>}
        </section>
      </>}
    </div>
    <footer className="modal-footer">
      <Button variant="secondary" disabled={busy || loading} onClick={() => setRefresh(value => value + 1)}>刷新任务板</Button>
      <Button variant="secondary" disabled={busy} onClick={() => { if (!pending.current) onClose(); }}>关闭</Button>
    </footer>
  </Modal>;
}

export default function ConversationTeam({ conversationId, unavailable }:
  { conversationId: string; unavailable: boolean }) {
  const [open, setOpen] = useState(false);
  return <>
    <button className="icon-button" type="button" title="团队任务板（协作试点）" aria-label="团队任务板"
      onClick={() => setOpen(true)}><Users size={17}/></button>
    {open && <TeamDialog key={conversationId} conversationId={conversationId}
      unavailable={unavailable} onClose={() => setOpen(false)}/>}
  </>;
}
