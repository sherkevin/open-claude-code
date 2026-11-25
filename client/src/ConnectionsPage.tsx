import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { AlertCircle, CheckCircle2, ChevronDown, CircleHelp, Clock3, Handshake, LoaderCircle, Link2, Network, Pencil, PlugZap, Plus, RefreshCw, Trash2, Upload } from 'lucide-react';
import { api, ApiError, errorText, patch, post, remove, timeText } from './api';
import type { Assistant, Connection, MCPTool } from './types';
import { Button, EmptyState, ErrorNotice, Field, Loader, Modal, PageHeader, SuccessNotice } from './components';
import { canEdit, ScopeBadge, ScopeField, type ResourceScope } from './resources';
import McpImportDialog from './McpImportDialog';
import ConnectionTrialDialog from './ConnectionTrialDialog';
import ConnectionReadOnlyReview, { currentReviewedTools } from './ConnectionReadOnlyReview';
import { connectionSetupContext, type ConnectionSetupContext } from './connectionHandoff';
import KaiwuHandoffDialog from './KaiwuHandoffDialog';
import { KaiwuAssistDialog, useKaiwuAssistDevices } from './KaiwuAssistDialog';
import { displayToolDescription, toolParameters } from './CapabilityGuide';
import { BusinessIcon, businessIconFor } from './BusinessIcon';
import './connections-onboarding.css';

export function connectionDiscoveryState(item: Connection) {
  if (!item.enabled) return { label: '已停用', detail: '启用后可重新测试。', complete: false, tone: 'neutral' };
  if (item.auth_type === 'oauth' && !item.credential_present) return { label: '待授权', detail: '使用供应方账号完成授权后可测试连接。', complete: false, tone: 'warning' };
  if (item.status === 'needs_auth') return { label: '认证未通过', detail: '请核对凭据和 MCP 端权限。', complete: false, tone: 'warning' };
  if (item.status === 'failed') return { label: '测试失败', detail: '请检查网络、地址与协议。', complete: false, tone: 'warning' };
  if (item.status === 'connected' && item.tools.length === 0) return { label: '未发现工具', detail: '连接已通，服务返回空工具清单。', complete: false, tone: 'warning' };
  if (item.status === 'connected') return { label: `已发现 ${item.tools.length} 个工具`, detail: '工具清单已更新；连接测试不代表业务查询已通过。', complete: true, tone: 'success' };
  return { label: '待测试', detail: '配置已保存，尚未测试。', complete: false, tone: 'neutral' };
}
export const loadConnections = (signal?: AbortSignal) => api<Connection[]>('/connections', { signal });
export function connectionTestError(error: unknown) {
  if (error instanceof ApiError && error.status === 403) return '当前账号无权测试此连接。';
  if (error instanceof ApiError && error.status === 401) return '工作台登录已失效，请重新登录后测试。';
  return `测试请求未完成：${errorText(error)}`;
}
export const testConnectionDiscovery = (item: Connection) => post<Connection>(`/connections/${encodeURIComponent(item.id)}/test`);
// Sequential discovery isolates a failed source and caps concurrency.
// This only tests connection metadata, never calls a business tool.
export async function testImportedConnections(items: Connection[], hooks: {
  onStart: (item: Connection) => void;
  onResult: (item: Connection) => void;
  onError: (item: Connection, error: string) => void;
}, isCancelled: () => boolean = () => false) {
  let passed = 0, failed = 0, matches = 0;
  for (const item of items) {
    if (isCancelled()) break;
    hooks.onStart(item);
    try {
      const next = await testConnectionDiscovery(item);
      if (next.id !== item.id) throw new Error('测试返回的连接与导入连接不一致');
      if (isCancelled()) break;
      hooks.onResult(next);
      if (next.status === 'connected' && next.tools.length) {
        passed++;
        matches += next.tools.filter(tool => tool.platform_read_default === true).length;
      } else failed++;
    } catch (cause) {
      if (isCancelled()) break;
      failed++; hooks.onError(item, connectionTestError(cause));
    }
  }
  return {passed, failed, matches};
}

export const saveConnectionConfiguration = (value: Connection | null, body: Record<string, unknown>) => value ? patch<Connection>(`/connections/${encodeURIComponent(value.id)}`, body) : post<Connection>('/connections', body);

// Only known functional descriptions are shortened. The source description and
// metadata remain available unchanged in the tool's original definition.
const concisePurposes = new Map([
  ['检索中文企业制度示例资料，返回来源编号，并标明为示例数据。', '检索中文企业制度资料，返回来源编号。'],
  ['创建并保存一条示例工单。此操作需要用户审批；request_key 用于防止重复创建。', '创建并保存工单；需要审批，支持防止重复创建。'],
  ['读取已保存的示例工单，支持精确筛选，不修改业务记录。', '按条件读取已保存的工单，不修改业务记录。'],
  ['按产线、批次和日期范围查询示例质量数据，按检验数量加权计算合格率，包含起止日期。返回数据库、数据表、字段、筛选条件、时间和汇总口径。', '按产线、批次和日期查询质量数据，计算加权合格率并返回来源。'],
]);
export function mcpToolPurpose(tool: MCPTool) {
  const description = displayToolDescription(tool.description).trim();
  return concisePurposes.get(description) || description || '用途说明未提供';
}
export function McpToolDetails({ item, tool }: { item: Connection; tool: MCPTool }) {
  const parameters = toolParameters(tool);
  const icon = businessIconFor(tool.name, tool.description);
  return <details className="mcp-tool-detail capability-tool"><summary><div className="capability-tool-label">{icon && <BusinessIcon icon={icon} size={18}/>}<div><strong>{tool.name}</strong><span>{mcpToolPurpose(tool)}</span></div></div><ChevronDown size={16}/></summary><div className="capability-tool-body">
    {parameters.length > 0 && <div className="table-container"><table><thead><tr><th>参数</th><th>要求</th><th>说明</th></tr></thead><tbody>{parameters.map(parameter => <tr key={parameter.key}><td>{parameter.label}<small>{parameter.key}</small></td><td>{parameter.required ? '必填' : '可选'} · {parameter.type}</td><td>{parameter.description || '—'}{parameter.choices.length > 0 && <small>可选值：{parameter.choices.map(String).join('、')}</small>}{parameter.defaultValue !== undefined && <small>默认值：{String(parameter.defaultValue)}</small>}</td></tr>)}</tbody></table></div>}
    <details className="resource-advanced"><summary>原始定义与数据来源</summary><pre className="text-preview">{JSON.stringify({ name: tool.name, description: tool.description, inputSchema: tool.inputSchema, annotations: tool.annotations, source_metadata: item.source_metadata }, null, 2)}</pre></details>
  </div></details>;
}
export function ConnectionGroups({ items, renderCard }: { items: Connection[]; renderCard: (item: Connection) => ReactNode }) {
  const enabled = items.filter(item => item.enabled), disabled = items.filter(item => !item.enabled);
  return <>{enabled.length ? <div className="connections-grid">{enabled.map(renderCard)}</div> : <p className="connection-empty-active">暂无启用的 MCP。</p>}{disabled.length > 0 && <details className="connections-disabled"><summary><strong>已停用（{disabled.length}）</strong><ChevronDown size={16}/></summary><div className="connections-grid">{disabled.map(renderCard)}</div></details>}</>;
}
export function ConnectionOnboardingCard({ item, testing = false, actionsBusy = false, testError = '', admin, onTest, onTools, onEdit, onDelete, onOAuth, onBusinessTrial, onReadOnlyReview, highlighted = false }: {
  item: Connection; testing?: boolean; actionsBusy?: boolean; testError?: string; admin: boolean;
  onTest: () => void; onTools: () => void; onEdit: () => void; onDelete: () => void; onOAuth?: () => void; onBusinessTrial?: () => void; onReadOnlyReview?: () => void; highlighted?: boolean;
}) {
  const discovery = connectionDiscoveryState(item), editable = canEdit(item, admin);
  const currentTestPassed = !testing && !testError && discovery.complete;
  const reviewed = currentReviewedTools(item);
  const statusLabel = testing ? '正在测试' : testError ? '本次测试未完成' : discovery.label;
  const purposes = [...new Set(item.tools.map(mcpToolPurpose))];
  const businessPurposes = purposes.filter(purpose => !purpose.startsWith('连接诊断：') && !purpose.startsWith('连接诊断专用：'));
  const functions = (businessPurposes.length ? businessPurposes : purposes).slice(0, 4);
  return <article className={`connection-card onboarding-card ${!item.enabled ? 'disabled-card' : ''}`} aria-labelledby={`connection-${item.id}`} data-connection-id={item.id} data-highlighted={highlighted || undefined} tabIndex={highlighted ? -1 : undefined}>
    <header className="connection-card-head"><span className="connection-icon"><BusinessIcon icon="data-connect" size={21}/></span><span className={`badge ${currentTestPassed ? 'green' : testError || discovery.tone === 'warning' ? 'amber' : ''}`} role="status">{testing ? <LoaderCircle size={13} className="spin"/> : currentTestPassed ? <CheckCircle2 size={13}/> : testError || discovery.tone === 'warning' ? <AlertCircle size={13}/> : <Clock3 size={13}/>} {statusLabel}</span></header>
    <div className="onboarding-card-title"><h2 id={`connection-${item.id}`}>{item.name}</h2><ScopeBadge scope={item.scope}/></div>
    <div className="connection-capability-preview">{functions.length ? <ul>{functions.map(description => <li key={description}>{description}</li>)}</ul> : <p className="section-help">{discovery.detail}</p>}</div>
    {currentTestPassed && <p className="section-help">连接测试仅验证工具发现，业务执行需另行验证。</p>}
    {currentTestPassed && item.read_only_review_revision !== undefined && <p className="section-help">在会话中选择自动审批即可使用平台已识别的只读工具；写入及未识别的操作仍需确认。{item.tools.some(tool => tool.platform_read_default !== undefined) && <span> 当前匹配可信契约：{item.tools.filter(tool => tool.platform_read_default === true).length} 个工具。</span>}</p>}
    {currentTestPassed && onReadOnlyReview && editable && <details className="resource-advanced"><summary>高级审批设置</summary><p className="section-help">已有人工核验：{reviewed.length} 个。一般业务用户无需配置；人工核验会覆盖平台默认识别。</p><Button variant="secondary" disabled={actionsBusy} onClick={onReadOnlyReview}>核验只读工具</Button></details>}
    {currentTestPassed && onBusinessTrial && <Button variant="secondary" disabled={actionsBusy} onClick={onBusinessTrial}>验证业务结果</Button>}
    {(testError || item.last_error) && <p className="inline-error">{testError || item.last_error}</p>}
    <button type="button" className="mcp-tool-list-button button ghost" onClick={onTools}>工具列表 · {item.tools.length}<ChevronDown size={15}/></button>
    <footer className="card-toolbar">
      {item.auth_type === 'oauth' && editable && <Button variant="secondary" disabled={actionsBusy || !item.enabled && !item.credential_present} onClick={onOAuth}>
        <Link2 size={15}/>{item.credential_present ? '断开授权' : '授权账号'}
      </Button>}
      {(item.auth_type !== 'oauth' || item.credential_present) && <Button variant="secondary" busy={testing} disabled={actionsBusy || !item.enabled || !editable} onClick={onTest}><PlugZap size={15}/>测试连接</Button>}
      <div className="toolbar-spacer"/>{editable && <><button type="button" className="icon-button" disabled={actionsBusy} aria-label={`编辑 ${item.name}`} onClick={onEdit}><Pencil size={16}/></button><button type="button" className="icon-button danger-text" disabled={actionsBusy} aria-label={`删除 ${item.name}`} onClick={onDelete}><Trash2 size={16}/></button></>}
    </footer>
    {item.last_tested_at && <small className="connection-time">{item.status === 'untested' ? '配置已更改，待重测' : `上次测试 ${timeText(item.last_tested_at)}`}</small>}
  </article>;
}

export default function ConnectionsPage({ admin, focusConnectionId, handoffCode, onCloseHandoff, oauthReturn, clearOAuthReturn, onStartBusinessTrial, onConfigureConnection }: {
  admin: boolean; focusConnectionId?: string; handoffCode?: string | null; onCloseHandoff?: () => void;
  oauthReturn?: { status: string; connectionId?: string } | null; clearOAuthReturn?: () => void;
  onStartBusinessTrial?: (assistant: Assistant, connection: Readonly<ConnectionSetupContext>, question: string) => void;
  onConfigureConnection?: (connection: Readonly<ConnectionSetupContext>) => void;
}) {
  const [items, setItems] = useState<Connection[]>([]), [loading, setLoading] = useState(true), [error, setError] = useState('');
  const [importing, setImporting] = useState(false), [success, setSuccess] = useState(''), [editor, setEditor] = useState<Connection | 'new' | null>(null);
  const [assisting, setAssisting] = useState(false);
  const [trial, setTrial] = useState<ConnectionSetupContext | null>(null);
  const [reviewId, setReviewId] = useState<string | null>(null);
  const review = items.find(item => item.id === reviewId);
  const assistance = useKaiwuAssistDevices(admin);
  const importTestEpoch = useRef(0);
  useEffect(() => () => { importTestEpoch.current++; }, []);
  const [testing, setTesting] = useState(''), [testErrors, setTestErrors] = useState<Record<string, string>>({}), [toolsId, setToolsId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Connection | null>(null), [busyDelete, setBusyDelete] = useState(false), [localFocusId, setLocalFocusId] = useState<string>();
  const [oauthProfiles, setOAuthProfiles] = useState<string[]>([]);
  const [oauthAction, setOAuthAction] = useState('');
  const [unlinking, setUnlinking] = useState<Connection | null>(null);
  const targetConnectionId = localFocusId || focusConnectionId || oauthReturn?.connectionId, tools = items.find(item => item.id === toolsId), located = useRef<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void api<{url:string}[]>('/connections/oauth/profiles', { signal: controller.signal })
      .then(profiles => setOAuthProfiles(profiles.map(profile => profile.url)))
      .catch(() => {});
    return () => controller.abort();
  }, []);
  const focusConnection = (id: string) => { located.current = null; setLocalFocusId(id); };
  useEffect(() => {
    if (loading || !targetConnectionId || located.current === targetConnectionId) return;
    const node = document.querySelector<HTMLElement>(`[data-connection-id="${CSS.escape(targetConnectionId)}"]`);
    if (node) { let parent = node.parentElement; while (parent) { if (parent instanceof HTMLDetailsElement) parent.open = true; parent = parent.parentElement; } node.focus({ preventScroll: true }); node.scrollIntoView({ block: 'center' }); located.current = targetConnectionId; }
  }, [targetConnectionId, loading, items]);
  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true); setError('');
    try { const result = await loadConnections(signal); if (!signal?.aborted) setItems(result); }
    catch (cause) { if (!signal?.aborted) setError(`MCP 清单读取失败：${errorText(cause)}`); }
    finally { if (!signal?.aborted) setLoading(false); }
  }, []);
  const manageOAuth = async (item: Connection) => {
    clearOAuthReturn?.();
    if (item.credential_present) { setUnlinking(item); return; }
    setOAuthAction(item.id); setError('');
    try {
      const result = await post<{ authorization_url: string }>(`/connections/${encodeURIComponent(item.id)}/oauth/start`);
      window.location.assign(result.authorization_url);
    } catch (cause) { setError(errorText(cause)); setOAuthAction(''); }
  };
  const revokeOAuth = async (item: Connection) => {
    setOAuthAction(item.id); setError(''); setSuccess('');
    try {
      const result = await post<{ issuer_revoked: boolean }>(`/connections/${encodeURIComponent(item.id)}/oauth/revoke`);
      setUnlinking(null);
      await load();
      if (result.issuer_revoked) setSuccess('已断开 MCP 授权。');
      else setError('工作台已断开授权。供应方未确认撤销，请在供应方账号设置中检查授权。');
    } catch (cause) { setError(errorText(cause)); }
    finally { setOAuthAction(''); }
  };
  useEffect(() => { const controller = new AbortController(); void load(controller.signal); return () => controller.abort(); }, [load]);
  const test = async (item: Connection) => {
    setTesting(item.id); setSuccess(''); setError('');
    setTestErrors(previous => { const next = { ...previous }; delete next[item.id]; return next; });
    try { const next = await testConnectionDiscovery(item); setItems(previous => previous.map(existing => existing.id === next.id ? next : existing)); if (next.status === 'connected') setSuccess(`已发现 ${next.tools.length} 个工具。请验证真实业务结果；自动审批由平台判断。`); }
    catch (cause) { setTestErrors(previous => ({ ...previous, [item.id]: connectionTestError(cause) })); }
    finally { setTesting(''); }
  };
  return <div className="page connections-onboarding mcp-catalog">
    <PageHeader eyebrow="" title="MCP" description="查看与管理已接入的工具服务。" actions={<>
      {admin && !!assistance.devices.length && <Button variant="secondary" disabled={!!testing || loading} onClick={() => {
        setAssisting(true);
        void assistance.refresh(undefined, true);
      }}><Handshake size={17}/>内网接入协助</Button>}
      <Button variant="secondary" disabled={!!testing || loading} onClick={() => setImporting(true)}><Upload size={17}/>导入配置</Button>
      <Button disabled={!!testing || loading} onClick={() => setEditor('new')}><Plus size={17}/>接入 MCP</Button>
    </>}/>
    <ErrorNotice message={error} onDismiss={() => setError('')}/><SuccessNotice message={success}/>
    {oauthReturn?.status === 'failed' && <ErrorNotice message="OAuth 授权未完成，请在对应连接上重新授权。" onDismiss={clearOAuthReturn}/>}
    {oauthReturn?.status === 'linked' && <SuccessNotice message="已返回工作台。请查看连接卡片上的工具发现结果。"/>}
    <div className="connections-list-heading"><h2>已启用{!loading && ` · ${items.filter(item => item.enabled).length}`}</h2><Button variant="ghost" busy={loading} disabled={!!testing} onClick={() => void load()}><RefreshCw size={16}/>刷新</Button></div>
    {loading && !items.length ? <Loader/> : !items.length ? <EmptyState icon={<Network size={32}/>} title={error ? '暂时无法读取 MCP' : '还没有 MCP'} description={error ? '请刷新重试。' : '填写服务地址，或导入已有配置。'}/> : <ConnectionGroups items={items} renderCard={item => <ConnectionOnboardingCard key={item.id} item={item} testing={testing === item.id} actionsBusy={!!testing || !!oauthAction || loading} testError={testErrors[item.id]} admin={admin} onTest={() => void test(item)} onTools={() => setToolsId(item.id)} onEdit={() => setEditor(item)} onDelete={() => setDeleting(item)} onOAuth={() => void manageOAuth(item)} onBusinessTrial={onStartBusinessTrial ? () => setTrial(connectionSetupContext(item)) : undefined} onReadOnlyReview={item.read_only_review_revision !== undefined ? () => setReviewId(item.id) : undefined} highlighted={item.id === targetConnectionId}/>}/>}
    {review && <ConnectionReadOnlyReview key={review.id} connection={review} onClose={() => setReviewId(null)} onSaved={saved => { setItems(previous => previous.map(item => item.id === saved.id ? saved : item)); setReviewId(null); setSuccess('只读核验已保存。自动审批仅适用于已授权且核验仍有效的工具。'); }}/>}
    {trial && onStartBusinessTrial && <ConnectionTrialDialog key={trial.id} target={trial} onClose={() => setTrial(null)} onStart={(assistant, target, question) => { setTrial(null); onStartBusinessTrial(assistant, target, question); }} onConfigure={onConfigureConnection ? target => { setTrial(null); onConfigureConnection(target); } : undefined}/>}
    {editor && <ConnectionEditor admin={admin} oauthProfiles={oauthProfiles} value={editor === 'new' ? null : editor} onClose={() => setEditor(null)} onSaved={async (saved, shouldTest) => { setEditor(null); focusConnection(saved.id); setItems(previous => previous.some(item => item.id === saved.id) ? previous.map(item => item.id === saved.id ? saved : item) : [...previous, saved]); setTestErrors(previous => { const next = { ...previous }; delete next[saved.id]; return next; }); setSuccess('MCP 配置已保存。'); if (shouldTest && saved.auth_type === 'oauth' && !saved.credential_present) await manageOAuth(saved); else if (shouldTest) await test(saved); }}/ >}
    {unlinking && <Modal title="断开 OAuth 授权" onClose={() => { if (!oauthAction) setUnlinking(null); }}><div className="modal-body"><p>断开「{unlinking.name}」后，已绑定的助手将停止使用该连接，直到你再次授权。</p><ErrorNotice message={error}/></div><footer className="modal-footer"><Button variant="secondary" disabled={!!oauthAction} onClick={() => setUnlinking(null)}>取消</Button><Button variant="danger" busy={!!oauthAction} onClick={() => void revokeOAuth(unlinking)}>断开授权</Button></footer></Modal>}
    {importing && <McpImportDialog admin={admin} onClose={() => setImporting(false)} onImported={async (result, shouldTest) => {
      setImporting(false);
      const imported = result.connections;
      if (imported[0]) focusConnection(imported[0].id);
      setItems(previous => [...previous.filter(item => !imported.some(next => next.id === item.id)), ...imported]);
      setError('');
      setSuccess('已导入 ' + imported.length + ' 个 MCP。');
      if (!shouldTest || !imported.length) return;
      const epoch = ++importTestEpoch.current;
      try {
        const summary = await testImportedConnections(imported, {
          onStart: item => { setTesting(item.id); setSuccess('已导入 ' + imported.length + ' 个 MCP，正在测试「' + item.name + '」。'); },
          onResult: next => setItems(previous => previous.map(item => item.id === next.id ? next : item)),
          onError: (item, message) => setTestErrors(previous => ({...previous, [item.id]: message})),
        }, () => epoch !== importTestEpoch.current);
        if (epoch === importTestEpoch.current) setSuccess('已导入 ' + imported.length + ' 个 MCP，工具发现通过 ' + summary.passed + ' 个，需检查 ' + summary.failed + ' 个；匹配可信只读契约 ' + summary.matches + ' 个工具。请验证真实业务结果。');
      } finally {
        if (epoch === importTestEpoch.current) setTesting('');
      }
    }}/ >}
    {admin && assisting && <KaiwuAssistDialog devices={assistance.devices} devicesError={assistance.error} onRefresh={() => void assistance.refresh(undefined, true)}
      onClose={() => { setAssisting(false); void assistance.refresh(); }} onImported={async item => {
        focusConnection(item.id);
        await load();
        if (item.status === 'untested') await test(item);
        else setSuccess(`已导入「${item.name}」。`);
      }}/>}
    {handoffCode && onCloseHandoff && <KaiwuHandoffDialog code={handoffCode} admin={admin} onClose={onCloseHandoff} onImported={async item => {
      onCloseHandoff();
      focusConnection(item.id);
      await load();
      if (item.status === 'untested') await test(item);
    }}/ >}
    {tools && <Modal title={`${tools.name} · 工具列表`} description={tools.last_tested_at ? `最近测试 ${timeText(tools.last_tested_at)}` : undefined} onClose={() => setToolsId(null)} wide><div className="modal-body mcp-tool-list">{!tools.tools.length ? <EmptyState icon={<CircleHelp size={26}/>} title="暂无工具" description={tools.status === 'connected' ? '服务返回空工具清单。' : '测试连接后可查看工具。'}/> : <>{tools.status !== 'connected' && <p className="section-help">以下为上次保存的工具定义，当前配置尚未通过测试。</p>}{tools.tools.map(tool => <McpToolDetails key={tool.name} item={tools} tool={tool}/>)}</>}</div><footer className="modal-footer"><Button variant="secondary" onClick={() => setToolsId(null)}>关闭</Button></footer></Modal>}
    {deleting && <Modal title="删除 MCP" onClose={() => { if (!busyDelete) setDeleting(null); }}><div className="modal-body"><p>确认删除「{deleting.name}」？删除后将无法继续调用，历史记录仍保留。</p></div><footer className="modal-footer"><Button variant="secondary" disabled={busyDelete} onClick={() => setDeleting(null)}>取消</Button><Button variant="danger" busy={busyDelete} onClick={async () => { setBusyDelete(true); try { await remove(`/connections/${encodeURIComponent(deleting.id)}`); setDeleting(null); setSuccess('MCP 已删除。'); await load(); } catch (cause) { setError(errorText(cause)); setDeleting(null); } finally { setBusyDelete(false); } }}>删除</Button></footer></Modal>}
  </div>;
}
export function ConnectionEditor({ value, admin, oauthProfiles = [], onClose, onSaved }: {
  value: Connection | null; admin: boolean; oauthProfiles?: string[];
  onClose: () => void; onSaved: (saved: Connection, shouldTest: boolean) => void | Promise<void>;
}) {
  const [scope, setScope] = useState<ResourceScope>(value?.scope || 'private');
  const [metadata, setMetadata] = useState(JSON.stringify(value?.source_metadata || {}, null, 2));
  const [name, setName] = useState(value?.name || '');
  const [url, setURL] = useState(value?.url || '');
  const [auth, setAuth] = useState<Connection['auth_type']>(value?.auth_type || 'none');
  const [credential, setCredential] = useState('');
  const [clear, setClear] = useState(false);
  const [enabled, setEnabled] = useState(value?.enabled ?? true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  return <Modal title={value ? '编辑 MCP' : '接入 MCP'} description="输入 MCP 服务地址和认证信息，保存后检查连接。" onClose={() => { if (!busy) onClose(); }}>
    <form className="connections-onboarding-editor" onSubmit={async event => {
      event.preventDefault();
      if (busy) return;
      const shouldTest = enabled && (event.nativeEvent as SubmitEvent).submitter?.getAttribute('value') === 'test';
      setBusy(true);
      setError('');
      try {
        let sourceMetadata: Record<string, string>;
        try {
          sourceMetadata = JSON.parse(metadata);
          if (!sourceMetadata || Array.isArray(sourceMetadata) || typeof sourceMetadata !== 'object' || Object.values(sourceMetadata).some(value => typeof value !== 'string')) throw new Error();
        } catch {
          setError('数据源说明须为名称与文本组成的 JSON 对象。');
          return;
        }
        const saved = await saveConnectionConfiguration(value, {
          name: name.trim(), scope, source_metadata: sourceMetadata, transport: 'http', url: url.trim(), auth_type: auth, enabled,
          ...(auth === 'bearer' && !clear && credential ? { credential } : {}),
          ...(value && auth !== 'oauth' && (clear || auth === 'none') ? { clear_credential: true } : {}),
        });
        await onSaved(saved, shouldTest);
      } catch (cause) { setError(errorText(cause)); }
      finally { setBusy(false); }
    }}>
      <div className="modal-body"><fieldset className="plain-fieldset" disabled={busy}>
        <ErrorNotice message={error}/>
        <ScopeField value={scope} onChange={setScope} admin={admin} disabled={!!value || auth === 'oauth'}/>
        <Field label="连接名称" required><input autoFocus value={name} maxLength={100} onChange={event => setName(event.target.value)} placeholder="例如：企业知识库" required/></Field>
        <Field label="认证方式"><select value={auth} disabled={value?.auth_type === 'oauth'} onChange={event => {
          const next = event.target.value as Connection['auth_type'];
          setAuth(next);
          if (next === 'oauth') {
            setScope('private');
            if (!oauthProfiles.includes(url)) setURL(oauthProfiles[0] || '');
          }
        }}>
          <option value="none">无需认证</option><option value="bearer">Bearer Token</option>
          {(oauthProfiles.length > 0 && !value || value?.auth_type === 'oauth') && <option value="oauth">OAuth 账号授权</option>}
        </select></Field>
        <Field label="MCP 服务地址" hint={auth === 'oauth' ? '使用工作台管理员已配置的服务。' : '支持 Streamable HTTP，由工作台服务端连接。填写管理员已核准的服务地址，不要在地址中填写凭据。'} required>
          {auth === 'oauth' && !value
            ? <select value={url} onChange={event => setURL(event.target.value)} required>{oauthProfiles.map(profile => <option key={profile} value={profile}>{profile}</option>)}</select>
            : <input type="url" value={url} onChange={event => setURL(event.target.value)} placeholder="https://your-service.example/mcp" readOnly={auth === 'oauth'} required/>}
        </Field>
        {auth === 'bearer' && <>
          <Field label={value?.credential_present ? '替换 Token' : 'Token'} hint={value?.credential_present ? '留空会保留已保存的凭据。保存后无法再次查看原文。' : '保存后以受控方式存储，不会展示在对话中。'}>
            <input type="password" autoComplete="new-password" value={credential} disabled={clear} onChange={event => setCredential(event.target.value)} placeholder={value?.credential_present ? '已保存凭据，输入新值以替换' : '输入 Token'} required={!value?.credential_present && !clear}/>
          </Field>
          {value?.credential_present && <label className="checkbox-row"><input type="checkbox" checked={clear} onChange={event => setClear(event.target.checked)}/>清除已保存的凭据</label>}
        </>}
        <details className="resource-advanced"><summary>数据源定位说明</summary><Field label="数据库、表与时间口径（JSON）" hint="填写连接提供的实际信息。每次取数的具体记录和筛选条件由 MCP 返回。"><textarea value={metadata} onChange={event => setMetadata(event.target.value)} rows={6} placeholder={'{"database":"质量管理库","tables":"quality_records","timezone":"Asia/Shanghai"}'}/></Field></details>
        <label className="checkbox-row separated"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)}/><span>启用此连接<small>停用后不再接受新的工具调用。</small></span></label>
        <p className="form-note">{auth === 'oauth' && !value?.credential_present ? '授权将在供应方页面完成，返回后自动读取工具清单。' : '保存并测试只读取工具清单，不运行业务工具。'}</p>
      </fieldset></div>
      <footer className="modal-footer"><Button variant="secondary" type="button" disabled={busy} onClick={onClose}>取消</Button><Button variant="secondary" busy={busy} type="submit" value="save">仅保存</Button><Button busy={busy} disabled={!enabled} type="submit" value="test"><PlugZap size={15}/>{auth === 'oauth' && !value?.credential_present ? '保存并授权' : '保存并测试'}</Button></footer>
    </form>
  </Modal>;
}
