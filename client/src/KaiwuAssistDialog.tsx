import { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api, errorText, post, timeText } from './api';
import { Button, ErrorNotice, Field, Modal, SuccessNotice } from './components';
import { ScopeField, type ResourceScope } from './resources';
import type { Connection } from './types';
import './kaiwu-assist.css';

const path = '/connections/kaiwu-assist';

type Device = {
  id: string;
  tenant_name: string;
  device_label?: string;
  online: boolean;
  active_session_id?: string;
};

type Job = {
  id: string;
  action: 'discover' | 'connect' | 'command';
  status: 'queued' | 'running' | 'succeeded' | 'failed';
  code?: string;
  stage?: 'initialize' | 'tools/list' | 'connect';
  http_status?: number;
  authorization?: boolean;
  command?: string;
  timeout_seconds?: number;
  output?: string;
  exit_code?: number;
  truncated?: boolean;
  duration_ms?: number;
  finished_at?: string;
};

type DynamicAuth = { app_id: string; channel: string; user_id: string; hmac_key: string };

type Session = {
  id: string;
  name: string;
  url: string;
  status: 'pending' | 'active' | 'stopped' | 'expired';
  expires_at: string;
  tenant_name: string;
  latest_job: Job | null;
  discovered_tools: string[];
  imported_connection_id?: string;
};

const sessionLabels = {
  pending: '等待客户授权',
  active: '协助中',
  stopped: '协助已结束',
  expired: '授权已到期',
};

function describeJob(job: Job): string {
  if (job.status === 'queued') return '等候连接器接收。';
  if (job.status === 'running') return job.action === 'command'
    ? '连接器正在客户电脑上执行命令，完成后会自动回传输出。'
    : job.action === 'discover' ? '客户电脑正在读取工具。' : '客户电脑正在保存服务并建立连接。';
  if (job.action === 'command') {
    if (job.code === 'command_timeout') return '命令执行超时，连接器已停止该进程。';
    if (job.status === 'succeeded') return '命令执行完成，输出已自动回传。';
    if (job.code === 'canceled') return '命令已取消。';
    return '命令执行失败，结果已自动回传。';
  }
  if (job.status === 'succeeded') return job.action === 'discover' ? '工具读取成功，请选择允许接入的工具。' : '业务服务已连接，可以导入开物。';
  switch (job.http_status) {
    case 401: return '业务服务拒绝了认证。请核对这个服务的访问令牌。';
    case 403: return '业务服务拒绝了访问。请核对服务权限和来源限制。';
    case 404: return '业务服务返回 404。请核对部署地址和完整路径。';
    case 405: return '该地址不接受 MCP 读取工具所需的 POST 请求。请核对接入地址。';
  }
  if (job.code === 'no_tools') return '服务没有返回工具。';
  if (job.code === 'canceled') return '这次操作已取消。';
  return job.action === 'discover' ? '工具读取失败，请核对服务地址、认证和连接状态。' : '连接失败，请确认客户电脑和开物的连接状态。';
}

function deviceName(device: Device): string {
  const status = device.active_session_id ? '已发起协助' : device.online ? '在线' : '等待本机发起';
  return [device.tenant_name, device.device_label || device.id, status].join(' · ');
}

export function useKaiwuAssistDevices(enabled: boolean) {
  const [devices, setDevices] = useState<Device[]>([]);
  const [error, setError] = useState('');
  const refresh = useCallback(async (signal?: AbortSignal, showError = false) => {
    try {
      const response = await api<{devices: Device[]}>(path + '/devices', { signal, redirect: 'error' });
      if (!signal?.aborted) {
        setDevices(response.devices);
        setError('');
      }
    } catch (cause) {
      if (!signal?.aborted && showError) setError(errorText(cause));
    }
  }, []);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [enabled, refresh]);

  return { devices, error, refresh };
}

export function KaiwuAssistDialog({ devices, devicesError, onRefresh, onClose, onImported }: {
  devices: Device[];
  devicesError: string;
  onRefresh: () => void;
  onClose: () => void;
  onImported: (connection: Connection) => void;
}) {
  const [deviceID, setDeviceID] = useState('');
  const [session, setSession] = useState<Session | null>(null);
  const [bearer, setBearer] = useState('');
  const [signature, setSignature] = useState<DynamicAuth | null>(null);
  const [tools, setTools] = useState<string[]>([]);
  const [command, setCommand] = useState('');
  const [commandTimeout, setCommandTimeout] = useState(60);
  const [commandHistory, setCommandHistory] = useState<Job[]>([]);
  const [scope, setScope] = useState<ResourceScope>('private');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const [imported, setImported] = useState<Connection | null>(null);
  const mutating = useRef(false);
  const epoch = useRef(0);
  const device = devices.find(item => item.id === deviceID);
  const job = session?.latest_job;
  const active = session?.status === 'active';
  const hasTarget = !!session?.url;
  const canUseMcp = active && hasTarget;
  const sessionID = session?.id;
  const jobBusy = job?.status === 'queued' || job?.status === 'running';
  const connected = job?.action === 'connect' && job.status === 'succeeded';
  const importedID = imported?.id || session?.imported_connection_id;
  const selected = tools.filter(tool => session?.discovered_tools.includes(tool));
  const signatureReady = !signature || !!(signature.app_id.trim() && signature.channel.trim() &&
    signature.user_id.trim() && signature.hmac_key.trim());

  useEffect(() => {
    setCommandHistory([]);
    setCommand('');
  }, [sessionID]);

  useEffect(() => {
    if (!job || job.action !== 'command' || job.status === 'queued' || job.status === 'running') return;
    setCommandHistory(previous => previous.some(item => item.id === job.id)
      ? previous
      : [...previous.slice(-19), job]);
  }, [job]);

  useEffect(() => {
    if (!sessionID || !active) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      const requestEpoch = epoch.current;
      try {
        if (mutating.current) return;
        const next = await api<Session>(path + '/sessions/' + encodeURIComponent(sessionID), { signal: controller.signal, redirect: 'error' });
        if (!controller.signal.aborted && requestEpoch === epoch.current) {
          setSession(next);
          setPollError('');
          if (next.status !== 'active' || !next.url) {
            setBearer('');
            setSignature(null);
          }
        }
      } catch (cause) {
        if (!controller.signal.aborted && requestEpoch === epoch.current) setPollError(errorText(cause));
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(poll, 2500);
      }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [active, sessionID]);

  async function run(action: () => Promise<void>) {
    if (mutating.current) return;
    epoch.current += 1;
    mutating.current = true;
    setBusy(true);
    setError('');
    try { await action(); }
    catch (cause) { setError(errorText(cause)); }
    finally { mutating.current = false; setBusy(false); }
  }

  function switchDevice(id: string) {
    epoch.current += 1;
    setDeviceID(id);
    setSession(null);
    setBearer('');
    setSignature(null);
    setTools([]);
    setError('');
    setPollError('');
    setImported(null);
  }

  function update(next: Session) {
    setSession(next);
    setPollError('');
  }

  async function join() {
    const offeredSession = device?.active_session_id;
    if (!offeredSession) return;
    await run(async () => {
      update(await post<Session>(path + '/sessions/' + encodeURIComponent(offeredSession) + '/claim'));
    });
  }

  async function submitJob(action: Job['action']) {
    if (!session || !canUseMcp || jobBusy || (action === 'discover' && !signatureReady)) return;
    await run(async () => {
      const payload = action === 'discover' ? {
        action,
        ...(bearer.trim() && { bearer_token: bearer.trim() }),
        ...(signature && { dynamic_auth: {
          app_id: signature.app_id.trim(), channel: signature.channel.trim(),
          user_id: signature.user_id.trim(), hmac_key: signature.hmac_key,
        } }),
      } : { action, tools: selected };
      if (action === 'discover') {
        setBearer('');
        setSignature(previous => previous && { ...previous, hmac_key: '' });
      }
      update(await post<Session>(path + '/sessions/' + encodeURIComponent(session.id) + '/jobs', payload));
      if (action === 'discover') setTools([]);
    });
  }

  async function sendCommand() {
    if (!session || !active || jobBusy) return;
    const text = command.trim();
    if (!text) return;
    await run(async () => {
      update(await post<Session>(path + '/sessions/' + encodeURIComponent(session.id) + '/jobs', {
        action: 'command', command: text, timeout_seconds: commandTimeout,
      }));
      setCommand('');
    });
  }

  async function stop() {
    if (!session || !active) return;
    await run(async () => {
      update(await post<Session>(path + '/sessions/' + encodeURIComponent(session.id) + '/stop'));
      setBearer('');
      setSignature(null);
      setCommand('');
    });
  }

  async function importConnection() {
    if (!session || !canUseMcp || !connected || importedID) return;
    await run(async () => {
      const result = await post<{connection: Connection}>(path + '/sessions/' + encodeURIComponent(session.id) + '/import', { scope });
      setImported(result.connection);
    });
  }

  function close() {
    if (mutating.current) return;
    onClose();
    if (imported) onImported(imported);
  }

  return <Modal title="内网接入协助" wide onClose={close}>
    <div className="modal-body kaiwu-assist">
      <ErrorNotice message={error || pollError || devicesError}/>
      {!session && <>
        <Field label="客户设备" required>
          <select value={deviceID} onChange={event => switchDevice(event.target.value)} disabled={busy}>
            <option value="">选择客户设备</option>
            {devices.map(item => <option key={item.id} value={item.id}>{deviceName(item)}</option>)}
          </select>
        </Field>
        {device?.active_session_id
          ? <p role="status">客户已在连接器发起协助，可以进入协助。</p>
          : <p role="status">请客户先在本机连接器点击「开始协助」，再刷新设备。</p>}
      </>}
      {session && <>
        <section className="kaiwu-assist-status" aria-label="协助状态">
          <strong role="status">{sessionLabels[session.status]}</strong>
          <p>{session.tenant_name}{hasTarget && ` · ${session.name}`}</p>
          {hasTarget && <code>{session.url}</code>}
          {active && <small>开放至 {timeText(session.expires_at)}</small>}
        </section>
        {active && !hasTarget && <p role="status">客户已开始协助。可直接在下方执行排查命令；需要接入 MCP 时，再让客户在本机确认服务地址。</p>}
        {active && <section className="kaiwu-assist-cmd" aria-label="远程 CMD">
          <div>
            <h3>远程 CMD</h3>
            <p className="muted">命令由客户连接器自动执行并把输出回传。客户能在本机看到每条命令和输出，随时可以停止协助。</p>
          </div>
          <Field label="命令" hint="例如 ipconfig /all、netstat -ano | findstr 4010、curl -vk https://内网地址/mcp">
            <input value={command} maxLength={4000} spellCheck={false} autoComplete="off" disabled={busy || jobBusy}
              placeholder="输入要在客户电脑上执行的命令"
              onChange={event => setCommand(event.target.value)}
              onKeyDown={event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); void sendCommand(); } }}/>
          </Field>
          <div className="kaiwu-assist-cmd-actions">
            <label className="kaiwu-assist-timeout">超时
              <select value={commandTimeout} disabled={busy || jobBusy} onChange={event => setCommandTimeout(Number(event.target.value))}>
                <option value={30}>30 秒</option>
                <option value={60}>60 秒</option>
                <option value={120}>120 秒</option>
              </select>
            </label>
            <Button variant="secondary" busy={jobBusy && job?.action === 'command'} disabled={busy || jobBusy || !command.trim() || !!pollError}
              onClick={() => void sendCommand()}>发送命令</Button>
          </div>
          {(job?.action === 'command' && jobBusy) || commandHistory.length > 0
            ? <div className="kaiwu-assist-terminal" role="log" aria-live="polite" aria-label="命令输出">
                {commandHistory.map(item => <div key={item.id} className="kaiwu-assist-entry">
                  <div className="kaiwu-assist-prompt"><span>C:\&gt;</span><code>{item.command}</code></div>
                  {item.output ? <pre>{item.output}</pre> : <p className="muted">（无输出）</p>}
                  <small>{item.status === 'succeeded' ? '完成' : item.code === 'command_timeout' ? '超时' : '失败'}
                    {' · '}退出码 {item.exit_code ?? '—'}{' · '}{item.duration_ms ?? 0} ms{item.truncated ? ' · 输出已截断' : ''}</small>
                </div>)}
                {job?.action === 'command' && jobBusy && <div className="kaiwu-assist-entry">
                  <div className="kaiwu-assist-prompt"><span>C:\&gt;</span><code>{job.command}</code></div>
                  <p className="muted">{describeJob(job)}</p>
                </div>}
              </div>
            : <p className="muted">还没有执行命令。</p>}
        </section>}
        {hasTarget && job && job.action !== 'command' && <section className="kaiwu-assist-result" aria-label="最近一次操作结果">
          <p role="status">{describeJob(job)}</p>
          {(job.stage || job.http_status || job.finished_at || job.authorization !== undefined) && <details>
            <summary>查看请求结果</summary>
            <dl>
              {job.stage && <><dt>步骤</dt><dd>{job.stage === 'initialize' ? '初始化 (initialize)' : job.stage === 'tools/list' ? '读取工具 (tools/list)' : '连接服务'}</dd></>}
              {job.http_status && <><dt>响应</dt><dd>HTTP {job.http_status}</dd></>}
              {job.authorization !== undefined && <><dt>认证</dt><dd>{job.authorization ? '已使用 Bearer 访问令牌' : '未使用 Bearer 访问令牌'}</dd></>}
              {job.finished_at && <><dt>完成时间</dt><dd>{timeText(job.finished_at)}</dd></>}
            </dl>
          </details>}
        </section>}
        {canUseMcp && !connected && !importedID && <div className="kaiwu-assist-action">
          <Field label="Bearer 访问令牌" hint="留空可使用同一地址在客户设备上已保存的令牌。">
            <input type="password" value={bearer} autoComplete="new-password" spellCheck={false} maxLength={8192} disabled={busy || jobBusy} onChange={event => setBearer(event.target.value)} />
          </Field>
          <label className="kaiwu-assist-toggle">
            <input type="checkbox" checked={!!signature} disabled={busy || jobBusy} onChange={event =>
              setSignature(event.target.checked ? { app_id: 'MCP', channel: '', user_id: '', hmac_key: '' } : null)
            }/>
            使用华微动态签名
          </label>
          {signature && <div className="kaiwu-assist-signature">
            <Field label="应用标识（appid）" required>
              <input value={signature.app_id} maxLength={256} disabled={busy || jobBusy} onChange={event => setSignature({ ...signature, app_id: event.target.value })}/>
            </Field>
            <Field label="渠道编号（channel）" required>
              <input value={signature.channel} maxLength={256} disabled={busy || jobBusy} onChange={event => setSignature({ ...signature, channel: event.target.value })}/>
            </Field>
            <Field label="访问身份（userid）" required>
              <input value={signature.user_id} maxLength={256} disabled={busy || jobBusy} onChange={event => setSignature({ ...signature, user_id: event.target.value })}/>
            </Field>
            <Field label="签名密钥" required>
              <input type="password" value={signature.hmac_key} autoComplete="new-password" spellCheck={false} maxLength={4096} disabled={busy || jobBusy} onChange={event => setSignature({ ...signature, hmac_key: event.target.value })}/>
            </Field>
          </div>}
          <Button variant="secondary" busy={jobBusy && job?.action === 'discover'} disabled={busy || jobBusy || !!pollError || !signatureReady} onClick={() => void submitJob('discover')}>读取工具</Button>
        </div>}
        {canUseMcp && !connected && !importedID && !!session.discovered_tools.length && <fieldset className="kaiwu-assist-tools" disabled={busy || jobBusy}>
          <legend>允许开物调用的工具</legend>
          {session.discovered_tools.map(tool => <label key={tool}>
            <input type="checkbox" checked={selected.includes(tool)} onChange={event => {
              const checked = event.target.checked;
              setTools(previous => checked ? [...previous, tool] : previous.filter(item => item !== tool));
            }} />
            <code>{tool}</code>
          </label>)}
        </fieldset>}
        {canUseMcp && connected && !importedID && <ScopeField admin value={scope} onChange={setScope} disabled={busy || !!pollError}/>}
        {importedID && <SuccessNotice message="已导入开物。可在 MCP 列表查看这项连接。"/>}
      </>}
    </div>
    <footer className="modal-footer">
      <Button variant="secondary" disabled={busy} onClick={close}>{imported ? '查看连接' : '关闭窗口'}</Button>
      {!session && <>
        <Button variant="ghost" disabled={busy} onClick={() => { setError(''); onRefresh(); }}><RefreshCw size={15}/>刷新设备</Button>
        <Button busy={busy} disabled={!device?.active_session_id} onClick={() => void join()}>进入协助</Button>
      </>}
      {session && !active && !importedID && <Button onClick={() => {
        switchDevice(''); onRefresh();
      }}>返回设备列表</Button>}
      {active && <Button variant="secondary" disabled={busy} onClick={() => void stop()}>结束协助</Button>}
      {session && canUseMcp && !connected && !importedID && !!session.discovered_tools.length && <Button busy={jobBusy && job?.action === 'connect'} disabled={busy || jobBusy || !selected.length || !!pollError} onClick={() => void submitJob('connect')}>保存并连接</Button>}
      {canUseMcp && connected && !importedID && <Button busy={busy} disabled={!!pollError} onClick={() => void importConnection()}>导入开物</Button>}
    </footer>
  </Modal>;
}
