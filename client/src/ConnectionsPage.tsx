import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { AlertCircle, ArrowUpRight, CheckCircle2, ChevronDown, CircleHelp, Clock3, LoaderCircle, Link2, Network, Pencil, PlugZap, Plus, RefreshCw, Trash2, Upload } from 'lucide-react';
import { api, ApiError, errorText, patch, post, remove, timeText } from './api';
import type { Assistant, Connection } from './types';
import { Button, EmptyState, ErrorNotice, Field, Loader, Modal, PageHeader, SuccessNotice } from './components';
import { canEdit, ScopeBadge, ScopeField, type ResourceScope } from './resources';
import McpImportDialog from './McpImportDialog';
import { displayToolDescription, ToolCapability } from './CapabilityGuide';
import './connections-onboarding.css';

export function connectionDiscoveryState(item: Connection) {
  if (!item.enabled) return { label: '连接已停用', detail: '启用后重新测试；当前不能发起新的工具调用。', complete: false, tone: 'neutral' };
  if (item.status === 'needs_auth') return { label: '认证或权限未通过', detail: '请核对凭据和 MCP 端权限；连接成功后再验证业务结果。', complete: false, tone: 'warning' };
  if (item.status === 'failed') return { label: '连接测试失败', detail: '请检查服务端网络、地址与 Streamable HTTP 协议。具体失败阶段未确认。', complete: false, tone: 'warning' };
  if (item.status === 'connected' && item.tools.length === 0) return { label: '已连接，未发现工具', detail: '服务返回空工具清单。请检查 MCP 是否提供所需能力及工具发现权限。', complete: false, tone: 'warning' };
  if (item.status === 'connected') return { label: `已发现 ${item.tools.length} 个工具`, detail: '已取得工具清单，请继续配置助手并验证结果。', complete: true, tone: 'success' };
  return { label: '等待连接测试', detail: '配置已保存，尚未确认此配置能否连接和发现工具。', complete: false, tone: 'neutral' };
}

export function connectionBindings(item: Connection, assistants: Assistant[] | null) {
  if (assistants === null) return null;
  return assistants.flatMap(assistant => {
    const bindings = assistant.connections.filter(binding => binding.connection_id === item.id);
    if (!bindings.length) return [];
    const allowed = new Set(bindings.flatMap(binding => binding.allowed_tools));
    return [{ id: assistant.id, name: assistant.name, enabled: assistant.enabled, allowedCount: item.tools.filter(tool => allowed.has(tool.name)).length }];
  });
}

export async function loadConnectionOnboarding(signal?: AbortSignal) {
  const [connections, assistants] = await Promise.allSettled([
    api<Connection[]>('/connections', { signal }), api<Assistant[]>('/assistants', { signal }),
  ]);
  return { connections, assistants };
}

export function connectionTestError(error: unknown) {
  if (error instanceof ApiError && error.status === 403) return '当前账号无权测试此连接。请由有管理权限的成员操作；MCP 的连接状态尚未重新确认。';
  if (error instanceof ApiError && error.status === 401) return '工作台登录已失效，请重新登录后测试；这不表示 MCP 凭据有误。';
  return `测试请求未完成：${errorText(error)} MCP 的最新状态未确认。`;
}
export const testConnectionDiscovery = (item: Connection) => post<Connection>(`/connections/${encodeURIComponent(item.id)}/test`);
export const saveConnectionConfiguration = (value: Connection | null, body: Record<string, unknown>) => value ? patch<Connection>(`/connections/${encodeURIComponent(value.id)}`, body) : post<Connection>('/connections', body);

export function trialAssistants(item: Connection, assistants: Assistant[] | null) {
  if (!connectionDiscoveryState(item).complete || !assistants) return [];
  const visibleTools = new Set(item.tools.map(tool => tool.name));
  return assistants.filter(assistant => assistant.enabled && assistant.connections.some(binding =>
    binding.connection_id === item.id && binding.allowed_tools.some(tool => visibleTools.has(tool))));
}

export function ConnectionGroups({ items, renderCard }: { items: Connection[]; renderCard: (item: Connection) => ReactNode }) {
  const enabled = items.filter(item => item.enabled);
  const disabled = items.filter(item => !item.enabled);
  return <>
    {enabled.length ? <div className="connections-grid">{enabled.map(renderCard)}</div> : <p className="connection-empty-active">暂无启用连接。展开下方停用连接，可在编辑中重新启用。</p>}
    {!!disabled.length && <details className="connections-disabled"><summary><strong>已停用（{disabled.length}）</strong><span>展开查看与管理</span><ChevronDown size={16}/></summary><p>配置与工具记录保留。需要恢复时，编辑连接并勾选「启用此连接」。</p><div className="connections-grid">{disabled.map(renderCard)}</div></details>}
  </>;
}

export function ConnectionOnboardingCard({ item, assistants, bindingsLoading = false, testing = false, actionsBusy = false, testError = '', admin, onTest, onTools, onEdit, onDelete, onConfigureAssistant, canConfigureAssistant = true, onStartAssistant, highlighted = false }: {
  item: Connection; assistants: Assistant[] | null; bindingsLoading?: boolean; testing?: boolean; actionsBusy?: boolean; testError?: string; admin: boolean;
  onTest: () => void; onTools: () => void; onEdit: () => void; onDelete: () => void; onConfigureAssistant: (connection: Connection) => void; highlighted?: boolean;
  canConfigureAssistant?: boolean; onStartAssistant?: (assistant: Assistant, connection: Connection, question?: string) => void;
}) {
  const [trialAssistantId, setTrialAssistantId] = useState('');
  const discovery = connectionDiscoveryState(item);
  const bindings = connectionBindings(item, assistants);
  const editable = canEdit(item, admin);
  const currentTestPassed = !testing && !testError && discovery.complete;
  const candidates = currentTestPassed && !bindingsLoading && !actionsBusy ? trialAssistants(item, assistants) : [];
  const selectedTrial = candidates.find(assistant => assistant.id === trialAssistantId) || candidates[0];
  const bindingLabel = bindingsLoading ? '正在读取绑定' : bindings === null ? '绑定情况未确认' : bindings.length ? `已绑定 ${bindings.length} 个助手` : '尚未绑定助手';
  const usableBindings = bindings?.filter(binding => binding.enabled && binding.allowedCount > 0).length || 0;
  const previewTools = item.tools.filter(tool => !/验收专用|Acceptance-only/.test(tool.description)).slice(0, 3);
  const statusLabel = testing ? '正在测试与发现工具' : testError ? '本次测试未完成' : discovery.label;
  return <article className={`connection-card onboarding-card ${!item.enabled ? 'disabled-card' : ''}`} aria-labelledby={`connection-${item.id}`} data-connection-id={item.id} data-highlighted={highlighted || undefined} tabIndex={highlighted ? -1 : undefined}>
    <header className="connection-card-head"><span className="connection-icon"><Link2 size={22}/></span><span className={`badge ${!testing && !testError && discovery.tone === 'success' ? 'green' : testError || discovery.tone === 'warning' ? 'amber' : ''}`} role="status">{testing ? <LoaderCircle size={13} className="spin"/> : currentTestPassed ? <CheckCircle2 size={13}/> : testError || discovery.tone === 'warning' ? <AlertCircle size={13}/> : <Clock3 size={13}/>} {statusLabel}</span></header>
    <div className="onboarding-card-title"><h2 id={`connection-${item.id}`}>{item.name}</h2><ScopeBadge scope={item.scope}/></div>
    <div className="connection-meta"><span>{item.tools.length} 项工具能力</span><span>{bindingLabel}</span></div>
    {!!item.tools.length && <div className="connection-capability-preview"><strong>可用功能</strong><ul>{previewTools.map(tool=><li key={tool.name}>{displayToolDescription(tool.description)?.split(/[。\n]/)[0] || `${tool.name}（用途说明未提供）`}</li>)}</ul>{item.tools.length>previewTools.length&&<p className="section-help">另有 {item.tools.length-previewTools.length} 项能力，可在“查看工具”中展开。</p>}</div>}
    {!testError && item.last_error && <p className="inline-error">{item.last_error}</p>}

    {onStartAssistant && selectedTrial && <div className="connection-trial-action"><div><strong>开始业务查询</strong><p>使用已绑定助手查询数据、汇总与分析。</p></div><div>{candidates.length > 1 && <label><span className="sr-only">验证助手</span><select value={selectedTrial.id} onChange={event => setTrialAssistantId(event.target.value)}>{candidates.map(assistant => <option value={assistant.id} key={assistant.id}>{assistant.name}</option>)}</select></label>}<Button onClick={() => onStartAssistant(selectedTrial, item)}>开始查询<ArrowUpRight size={15}/></Button></div></div>}
    <details className="connection-trial-guide"><summary><span>连接详情与接入进度</span><ChevronDown size={15}/></summary>
    <div className="connection-time">{item.last_tested_at ? `${item.status === 'untested' ? '历史测试' : '最近测试'} ${timeText(item.last_tested_at)} · ${item.status === 'untested' ? '当前配置待重测' : '非实时状态'}` : '尚无测试时间记录'}</div>
      <dl className="connection-technical-details"><div><dt>服务地址</dt><dd>{item.url}</dd></div><div><dt>连接协议</dt><dd>Streamable HTTP</dd></div><div><dt>认证方式</dt><dd>{item.auth_type === 'bearer' ? item.credential_present ? 'Bearer Token · 已保存' : 'Bearer Token · 未保存' : '无需认证'}</dd></div></dl>
    <ol className="connection-progress" aria-label={`${item.name}接入进度`}>
      <li data-state="complete"><span className="connection-step-number">1</span><div><strong>地址与认证</strong><p>配置已保存 · 服务端连接</p></div></li>
      <li data-state={currentTestPassed ? 'complete' : 'pending'}><span className="connection-step-number">2</span><div><strong>检查工具能力</strong><span className="connection-step-status">{statusLabel}</span><p>{testing ? '正在请求 MCP 服务并读取工具定义，不执行业务查询。' : testError || discovery.detail}</p></div></li>
      <li data-state={candidates.length ? 'current' : 'pending'}><span className="connection-step-number">3</span><div><strong>配置助手并验证</strong><span className="connection-step-status">{bindingLabel}</span><p>{bindingsLoading ? '按当前账号可见的助手核对。' : bindings === null ? '未取得助手清单，不能判断是否已绑定。' : bindings.length ? `当前可见助手中，${usableBindings} 个已启用并允许调用已发现工具。` : '选择助手与允许调用的工具。'}</p></div></li>
    </ol>

      {bindings && bindings.length > 0 && <ul className="connection-bound-assistants">{bindings.map(binding => <li key={binding.id}><strong>{binding.name}</strong><span>{binding.enabled ? '已启用' : '已停用'} · 允许 {binding.allowedCount} 个已发现工具</span></li>)}</ul>}
      <p>在助手设置中选择「{item.name}」和本次任务需要的工具。再从首页进入该助手，使用已授权的业务范围和一个有标准答案的问题核对结果。</p>
      <p>工具清单已获取，不代表业务结果已经核对。调用范围与是否需要确认，由助手设置决定。</p>
    </details>
    <footer className="card-toolbar"><Button variant={!currentTestPassed && editable && item.enabled ? "primary" : "secondary"} busy={testing} disabled={actionsBusy || !item.enabled || !editable} onClick={onTest}><PlugZap size={15}/>测试连接</Button><Button variant="ghost" onClick={onTools}>查看工具<ArrowUpRight size={15}/></Button>{canConfigureAssistant && <Button variant={currentTestPassed && !candidates.length ? "primary" : "ghost"} disabled={actionsBusy} onClick={() => onConfigureAssistant(item)}>配置助手<ArrowUpRight size={15}/></Button>}<div className="toolbar-spacer"/>{editable && <><button type="button" className="icon-button" disabled={actionsBusy} aria-label={`编辑 ${item.name}`} title="编辑连接" onClick={onEdit}><Pencil size={16}/></button><button type="button" className="icon-button danger-text" disabled={actionsBusy} aria-label={`删除 ${item.name}`} title="删除连接" onClick={onDelete}><Trash2 size={16}/></button></>}</footer>
    {!editable && <p className="connection-access-note">此连接由有管理权限的成员维护，你可以查看工具和自己的助手绑定。</p>}
  </article>;
}

export default function ConnectionsPage({ onConfigureAssistant, admin, canConfigureAssistant = true, onStartAssistant, focusConnectionId }: {
  onConfigureAssistant: (connection: Connection) => void; focusConnectionId?: string; admin: boolean; canConfigureAssistant?: boolean; onStartAssistant?: (assistant: Assistant, connection: Connection, question?: string) => void;
}) {
  const [items, setItems] = useState<Connection[]>([]);
  const [assistants, setAssistants] = useState<Assistant[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [bindingsError, setBindingsError] = useState('');
  const [error, setError] = useState('');
  const [importing, setImporting] = useState(false);
  const [success, setSuccess] = useState('');
  const [editor, setEditor] = useState<Connection | 'new' | null>(null);
  const [testing, setTesting] = useState('');
  const [testErrors, setTestErrors] = useState<Record<string, string>>({});
  const [tools, setTools] = useState<Connection | null>(null);
  const [toolAssistantId, setToolAssistantId] = useState('');
  const [deleting, setDeleting] = useState<Connection | null>(null);
  const [busyDelete, setBusyDelete] = useState(false);
  const [localFocusId, setLocalFocusId] = useState<string>();
  const targetConnectionId = localFocusId || focusConnectionId;
  const located = useRef<string | null>(null);
  const focusConnection = (id: string) => { located.current = null; setLocalFocusId(id); };
  useEffect(() => {
    if (loading || !targetConnectionId || located.current === targetConnectionId) return;
    const node = document.querySelector<HTMLElement>(`[data-connection-id="${CSS.escape(targetConnectionId)}"]`);
    if (node) {
      let parent = node.parentElement;
      while (parent) { if (parent instanceof HTMLDetailsElement) parent.open = true; parent = parent.parentElement; }
      node.focus({ preventScroll: true }); node.scrollIntoView({ block: "center", behavior: "auto" }); located.current = targetConnectionId;
    }
  }, [targetConnectionId, loading, items]);
  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true); setError(''); setBindingsError(''); setAssistants(null);
    const result = await loadConnectionOnboarding(signal);
    if (signal?.aborted) return;
    if (result.connections.status === 'fulfilled') setItems(result.connections.value);
    else setError(`连接清单读取失败：${errorText(result.connections.reason)} 已显示内容如有，则为上次读取结果。`);
    if (result.assistants.status === 'fulfilled') setAssistants(result.assistants.value);
    else setBindingsError(`助手绑定情况未确认：${errorText(result.assistants.reason)}`);
    setLoading(false);
  }, []);
  useEffect(() => { const controller = new AbortController(); void load(controller.signal); return () => controller.abort(); }, [load]);
  const test = async (item: Connection) => {
    setTesting(item.id); setSuccess(''); setError('');
    setTestErrors(previous => { const next = { ...previous }; delete next[item.id]; return next; });
    try {
      const next = await testConnectionDiscovery(item);
      setItems(previous => previous.map(existing => existing.id === next.id ? next : existing));
      if (next.status === 'connected' && next.tools.length > 0) setSuccess(`「${next.name}」已发现 ${next.tools.length} 个工具。请配置助手并验证业务结果。`);
    } catch (e) { setTestErrors(previous => ({ ...previous, [item.id]: connectionTestError(e) })); }
    finally { setTesting(''); }
  };
  const toolAssistants = tools && !testing && !loading && !testErrors[tools.id] ? trialAssistants(tools,assistants) : [];
  const toolAssistant = toolAssistants.find(item=>item.id===toolAssistantId) || toolAssistants[0];
  return <div className="page connections-onboarding">
    <PageHeader eyebrow="系统接入" title="业务连接" description="连接企业系统，让助手查询和使用业务数据。" actions={<><Button variant="secondary" disabled={!!testing || loading} onClick={() => setImporting(true)}><Upload size={17}/>导入配置</Button><Button disabled={!!testing || loading} onClick={() => setEditor('new')}><Plus size={17}/>接入 MCP</Button></>}/>
    <ErrorNotice message={error} onDismiss={() => setError('')}/><ErrorNotice message={bindingsError}/><SuccessNotice message={success}/>
    <ol className="connection-route" aria-label="MCP 接入步骤"><li><span>1</span><div><strong>接入服务</strong><p>填写地址，或导入已有 MCP 配置</p></div></li><li><span>2</span><div><strong>检查连接</strong><p>验证认证，读取可用工具</p></div></li><li><span>3</span><div><strong>开始使用</strong><p>绑定助手，选择工具并提问</p></div></li></ol>
    <details className="connection-server-note"><summary><Network size={16}/>服务端如何连接 MCP<ChevronDown size={15}/></summary><p>由工作台服务端连接，不是你的浏览器。首次接入的服务域名（或 IP 地址）须由工作台管理员核准，并确保服务端可达。当前支持 Streamable HTTP、无认证或 Bearer Token；连接测试只读取工具清单。</p></details>
    <div className="connections-list-heading"><div><h2>启用的连接{!loading && ` · ${items.filter(item => item.enabled).length}`}</h2><p>显示上次连接检查结果，不代表当前在线状态。</p></div><Button variant="ghost" busy={loading} disabled={!!testing} onClick={() => void load()}><RefreshCw size={16}/>刷新状态</Button></div>
    {loading && !items.length ? <Loader/> : !items.length ? error ? <EmptyState icon={<Network size={32}/>} title="连接清单暂未取得" description="请恢复工作台访问后刷新；这不表示没有已保存的连接。"/> : <EmptyState icon={<Network size={32}/>} title="连接你的第一个业务系统" description="准备 MCP 地址与受限凭据。保存后测试工具，再选择助手和允许的能力。" action={<Button onClick={() => setEditor('new')}><Plus size={16}/>接入 MCP</Button>}/> : <ConnectionGroups items={items} renderCard={item => <ConnectionOnboardingCard key={item.id} item={item} assistants={assistants} bindingsLoading={loading} testing={testing === item.id} actionsBusy={!!testing || loading} testError={testErrors[item.id]} admin={admin} onTest={() => void test(item)} onTools={() => setTools(item)} onEdit={() => setEditor(item)} onDelete={() => setDeleting(item)} onConfigureAssistant={onConfigureAssistant} highlighted={item.id === targetConnectionId} canConfigureAssistant={canConfigureAssistant} onStartAssistant={onStartAssistant}/>}/>}
    {editor && <ConnectionEditor admin={admin} value={editor === 'new' ? null : editor} onClose={() => setEditor(null)} onSaved={async (saved, shouldTest) => {
      setEditor(null); focusConnection(saved.id); setItems(previous => previous.some(item => item.id === saved.id) ? previous.map(item => item.id === saved.id ? saved : item) : [...previous, saved]);
      setTestErrors(previous => { const next = { ...previous }; delete next[saved.id]; return next; });
      setSuccess('配置已保存。请测试连接并验证业务结果。');
      if (shouldTest) await test(saved);
    }}/>}
    {importing && <McpImportDialog admin={admin} onClose={() => setImporting(false)} onImported={result => { setImporting(false); if (result.connections[0]) focusConnection(result.connections[0].id); setSuccess(`已导入 ${result.connections.length} 个业务连接。请先测试，再配置助手允许使用的工具及审批方式。`); void load(); }}/>}
    {tools && <Modal title={`${tools.name} · 功能与使用示例`} description={tools.last_tested_at ? `最近测试 ${timeText(tools.last_tested_at)}；以下为服务提供的工具说明` : '先测试连接以发现可用工具'} onClose={() => setTools(null)} wide><div className="modal-body connection-tools onboarding-tools">
      {!tools.tools.length ? <EmptyState icon={<CircleHelp size={26}/>} title={tools.status === 'connected' ? '连接已通，工具清单为空' : '尚无可查看的工具'} description={tools.status === 'connected' ? '请核对 MCP 提供的能力及工具发现权限。空工具清单不是业务查询的空结果。' : '先处理连接或认证问题，再测试发现工具。'}/> : <>
        {tools.status !== 'connected' && <p className="section-help">以下为已保存的工具定义；当前连接尚未通过测试。</p>}
        {onStartAssistant && toolAssistant ? <label className="connection-tool-trial">使用哪个助手<select value={toolAssistant.id} onChange={event=>setToolAssistantId(event.target.value)}>{toolAssistants.map(assistant=><option key={assistant.id} value={assistant.id}>{assistant.name}</option>)}</select><span>选择示例后只填入草稿，不会自动执行。</span></label> : <p className="section-help">配置已启用的助手并授权相应工具后，可点击提问示例验证结果。</p>}
        {tools.tools.map(tool=>{const binding=toolAssistant?.connections.find(item=>item.connection_id===tools.id && item.allowed_tools.includes(tool.name)) || toolAssistant?.connections.find(item=>item.connection_id===tools.id);return <ToolCapability key={tool.name} connection={tools} tool={tool} binding={binding} onChoose={onStartAssistant && toolAssistant && binding?.allowed_tools.includes(tool.name) ? question=>{setTools(null);onStartAssistant(toolAssistant,tools,question);} : undefined}/>;})}
      </>}
    </div><footer className="modal-footer"><Button variant="secondary" onClick={() => setTools(null)}>关闭</Button></footer></Modal>}
    {deleting && <Modal title="删除业务连接" onClose={() => { if (!busyDelete) setDeleting(null); }}><div className="modal-body"><p>删除「{deleting.name}」后，绑定它的助手将无法继续调用这个连接。历史对话仍然保留。</p></div><footer className="modal-footer"><Button variant="secondary" disabled={busyDelete} onClick={() => setDeleting(null)}>保留连接</Button><Button variant="danger" busy={busyDelete} onClick={async () => { setBusyDelete(true); try { await remove(`/connections/${encodeURIComponent(deleting.id)}`); setDeleting(null); setSuccess('连接已删除。'); await load(); } catch (e) { setError(errorText(e)); setDeleting(null); } finally { setBusyDelete(false); } }}>确认删除</Button></footer></Modal>}
  </div>;
}

export function ConnectionEditor({ value, admin, onClose, onSaved }: { value: Connection | null;admin:boolean; onClose: () => void; onSaved: (saved: Connection, shouldTest: boolean) => void | Promise<void> }) {
  const [scope,setScope]=useState<ResourceScope>(value?.scope||'private');const [metadata,setMetadata]=useState(JSON.stringify(value?.source_metadata||{},null,2));
  const [name, setName] = useState(value?.name || ''); const [url, setURL] = useState(value?.url || ''); const [auth, setAuth] = useState<'none' | 'bearer'>(value?.auth_type || 'none'); const [credential, setCredential] = useState(''); const [clear, setClear] = useState(false); const [enabled, setEnabled] = useState(value?.enabled ?? true); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  return <Modal title={value ? '编辑业务连接' : '接入 MCP'} description="输入 MCP 服务地址和认证信息，保存后检查连接。" onClose={() => { if (!busy) onClose(); }}><form className="connections-onboarding-editor" onSubmit={async e => { e.preventDefault(); if (busy) return; const shouldTest = enabled && (e.nativeEvent as SubmitEvent).submitter?.getAttribute('value') === 'test'; setBusy(true); setError(''); let sourceMetadata:Record<string,string>;try{sourceMetadata=JSON.parse(metadata);if(!sourceMetadata||Array.isArray(sourceMetadata)||typeof sourceMetadata!=='object'||Object.values(sourceMetadata).some(v=>typeof v!=='string'))throw new Error();}catch{setError('数据源说明须为名称与文本组成的 JSON 对象。');setBusy(false);return;} const body = { name: name.trim(),scope,source_metadata:sourceMetadata, transport: 'http', url: url.trim(), auth_type: auth, enabled, ...(auth === 'bearer' && !clear && credential ? { credential } : {}), ...(value && (clear || auth === 'none') ? { clear_credential: true } : {}) }; try { const saved = await saveConnectionConfiguration(value, body); await onSaved(saved, shouldTest); } catch (err) { setError(errorText(err)); } finally { setBusy(false); } }}><div className="modal-body"><fieldset className="plain-fieldset" disabled={busy}><div className="connection-editor-step"><strong>第 1 步 · 地址与认证</strong><p>保存并测试后，在连接卡片中检查工具并绑定助手。</p></div><ErrorNotice message={error}/><ScopeField value={scope} onChange={setScope} admin={admin} disabled={!!value}/><Field label="连接名称" required><input autoFocus value={name} maxLength={100} onChange={e => setName(e.target.value)} placeholder="例如：企业知识库" required/></Field><Field label="MCP 服务地址" hint="支持 Streamable HTTP，由工作台服务端连接。填写管理员已核准的服务地址，不要在地址中填写凭据。" required><input type="url" value={url} onChange={e => setURL(e.target.value)} placeholder="https://your-service.example/mcp" required/></Field><Field label="认证方式"><select value={auth} onChange={e => setAuth(e.target.value as 'none' | 'bearer')}><option value="none">无需认证</option><option value="bearer">Bearer Token</option></select></Field>{auth === 'bearer' && <><Field label={value?.credential_present ? '替换 Token' : 'Token'} hint={value?.credential_present ? '留空会保留已保存的凭据。保存后无法再次查看原文。' : '保存后以受控方式存储，不会展示在对话中。'}><input type="password" autoComplete="new-password" value={credential} disabled={clear} onChange={e => setCredential(e.target.value)} placeholder={value?.credential_present ? '已保存凭据，输入新值以替换' : '输入 Token'} required={!value?.credential_present && !clear}/></Field>{value?.credential_present && <label className="checkbox-row"><input type="checkbox" checked={clear} onChange={e => setClear(e.target.checked)}/>清除已保存的凭据</label>}</>}<details className="resource-advanced"><summary>数据源定位说明</summary><Field label="数据库、表与时间口径（JSON）" hint="填写连接提供的实际信息。每次取数的具体记录和筛选条件由 MCP 返回。"><textarea value={metadata} onChange={e=>setMetadata(e.target.value)} rows={6} placeholder={'{"database":"质量管理库","tables":"quality_records","timezone":"Asia/Shanghai"}'}/></Field></details><label className="checkbox-row separated"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)}/><span>启用此连接<small>停用后，助手不能继续发起新的工具调用。</small></span></label><p className="form-note">保存只记录配置；“保存并测试”会连接 MCP 并读取工具清单，不会运行业务工具或自动绑定助手。</p></fieldset></div><footer className="modal-footer"><Button variant="secondary" type="button" disabled={busy} onClick={onClose}>取消</Button><Button variant="secondary" busy={busy} type="submit" value="save">仅保存</Button><Button busy={busy} disabled={!enabled} type="submit" value="test"><PlugZap size={15}/>保存并测试</Button></footer></form></Modal>;
}
