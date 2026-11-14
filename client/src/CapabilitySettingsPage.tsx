import { useEffect, useReducer, useState } from 'react';
import { Globe, KeyRound, RefreshCw, ShieldCheck, Stethoscope } from 'lucide-react';
import { errorText } from './api';
import { Button, ErrorNotice, Field, Loader, PageHeader, SuccessNotice } from './components';
import {
  capabilityInitialState, capabilityLabels, capabilityPageReducer, canManageCapabilities, deleteProviderCredential,
  loadCapabilitySettings, providerLabels, runProviderDiagnostics, saveCapabilitySettings, saveProviderCredential,
} from './capabilitySettings';
import type {
  CapabilityConfiguration, CapabilityKind, CapabilityPageState, CapabilityRoute, DiagnosticResponse, EgressMode, ProviderId,
} from './capabilitySettings';
import type { AppConfig } from './types';
import './capability-settings.css';

const KINDS: CapabilityKind[] = ['search', 'fetch', 'ocr', 'image_search'];
const PROVIDERS_BY_KIND: Record<CapabilityKind, ProviderId[]> = {
  search: ['bing_rss', 'searxng', 'brave', 'deepseek_api'], fetch: ['public_http', 'deepseek_api'],
  ocr: ['deepseek_api'], image_search: ['deepseek_webtools', 'brave_images'],
};
const EGRESS_OPTIONS: Array<{value: EgressMode; label: string}> = [
  {value: 'deny', label: '禁止外发'}, {value: 'approval', label: '每次人工审批'}, {value: 'allow', label: '允许'},
];

export default function CapabilitySettingsPage({admin, config}: {admin: boolean; config: AppConfig}) {
  const allowed = canManageCapabilities(admin, config);
  const [state, dispatch] = useReducer(capabilityPageReducer, capabilityInitialState);
  useEffect(() => {
    if (!allowed) return;
    let live = true;
    loadCapabilitySettings().then(value => {if (live) dispatch({type: 'loaded', value});})
      .catch(error => {if (live) dispatch({type: 'error', error: errorText(error)});});
    return () => {live = false;};
  }, [allowed]);
  const reload = async () => {
    dispatch({type: 'load'});
    try {dispatch({type: 'loaded', value: await loadCapabilitySettings()});}
    catch (error) {dispatch({type: 'error', error: errorText(error)});}
  };
  const save = async () => {
    if (!state.value || state.phase === 'saving') return;
    dispatch({type: 'save'});
    try {dispatch({type: 'saved', value: await saveCapabilitySettings(state.value)});}
    catch (error) {dispatch({type: 'error', error: errorText(error)});}
  };
  return <CapabilitySettingsView allowed={allowed} state={state} onReload={() => void reload()} onSave={() => void save()}
    onChange={value => dispatch({type: 'edit', value})} onReloaded={reload}/>;
}

function RouteEditor({kind, value, busy, onEdit}: {kind: CapabilityKind; value: CapabilityRoute; busy: boolean; onEdit: (route: CapabilityRoute) => void}) {
  const route = value;
  const edit = (patch: Partial<CapabilityRoute>) => onEdit({...route, ...patch});
  const fixedRetry = kind === 'ocr' || kind === 'image_search';
  const providers = Object.entries(providerLabels).filter(([id]) => PROVIDERS_BY_KIND[kind].includes(id as ProviderId));
  return <fieldset disabled={busy}>
    <label className="checkbox-row"><input type="checkbox" checked={route.enabled} onChange={e => edit({enabled: e.target.checked})}/>允许此能力</label>
    <Field label="首选提供方"><select value={route.preferred} onChange={e => edit({preferred: e.target.value as ProviderId, fallbacks: route.fallbacks.filter(id => id !== e.target.value)})}>
      {providers.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
    </select></Field>
    <Field label="回退服务" hint="仅服务故障或限流时回退；权限、域名、审批或安全拒绝不会触发回退。">
      <select disabled={fixedRetry} value={fixedRetry ? '' : route.fallbacks[0] || ''} onChange={e => edit({fallbacks: e.target.value ? [e.target.value as ProviderId] : []})}>
        <option value="">不回退</option>{providers.filter(([id]) => id !== route.preferred).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
      </select>
    </Field>
    <div className="two-col">
      <Field label="调用总超时（秒）" hint="首选和回退共用，1–60 秒。"><input type="number" min={1} max={60} step={1} required value={route.timeout_seconds} onChange={e => edit({timeout_seconds: Number(e.target.value)})}/></Field>
      <Field label="最大尝试次数" hint={fixedRetry ? '文档识别远端在本地取消后仍可能继续计费，固定为 1，不允许自动重试。' : '包含首选与回退，1–3 次。'}>
        <input type="number" min={1} max={3} step={1} disabled={fixedRetry} value={fixedRetry ? 1 : route.max_attempts} onChange={e => edit({max_attempts: Number(e.target.value)})}/>
      </Field>
    </div>
    <Field label="域名策略" hint="公开：依赖服务器公网地址校验；白名单：只允许下列精确主机名，留空即全部拒绝。">
      <select value={route.domains.mode} onChange={e => edit({domains: {...route.domains, mode: e.target.value as 'public' | 'allowlist'}})}>
        <option value="public">公开互联网</option><option value="allowlist">仅白名单主机</option>
      </select>
    </Field>
    {route.domains.mode === 'allowlist' && <Field label="允许的主机名" hint="每行或用逗号分隔，只写主机名，不含协议、路径或端口。">
      <textarea rows={3} value={route.domains.hosts.join('\n')} onChange={e => edit({domains: {...route.domains, hosts: e.target.value.split(/[\s,]+/).map(item => item.trim()).filter(Boolean)}})}/>
    </Field>}
    <div className="two-col">
      <Field label="查询外发" hint="搜索词会发送给上游提供方。"><select value={route.egress.query} onChange={e => edit({egress: {...route.egress, query: e.target.value as EgressMode}})}>{EGRESS_OPTIONS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></Field>
      <Field label="网页地址外发"><select value={route.egress.url} onChange={e => edit({egress: {...route.egress, url: e.target.value as EgressMode}})}>{EGRESS_OPTIONS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></Field>
    </div>
    <Field label="文件外发（上传）" hint={kind === 'ocr' || kind === 'image_search' ? '上传的图片或文档会离开本服务器；默认禁止，需管理员明确同意。' : '当前能力不上传文件；仍按更严格值生效。'}>
      <select value={route.egress.upload} onChange={e => edit({egress: {...route.egress, upload: e.target.value as EgressMode}})}>{EGRESS_OPTIONS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select>
    </Field>
    <div className="two-col">
      <Field label="每次任务调用上限"><input type="number" min={1} max={10000} value={route.limits.calls_per_run} onChange={e => edit({limits: {...route.limits, calls_per_run: Number(e.target.value)}})}/></Field>
      <Field label="每日调用上限"><input type="number" min={1} max={100000} value={route.limits.calls_per_day} onChange={e => edit({limits: {...route.limits, calls_per_day: Number(e.target.value)}})}/></Field>
    </div>
    {(kind === 'ocr' || kind === 'image_search') && <Field label="单文件上传上限（字节）" hint="0 表示不额外限制，但总上限仍受部署上传限制约束。">
      <input type="number" min={0} max={209715200} value={route.limits.max_upload_bytes} onChange={e => edit({limits: {...route.limits, max_upload_bytes: Number(e.target.value)}})}/>
    </Field>}
  </fieldset>;
}

function CredentialPanel({value, busy, onChanged}: {value: CapabilityConfiguration; busy: boolean; onChanged: () => Promise<void>}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [working, setWorking] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [diagnostics, setDiagnostics] = useState<DiagnosticResponse | null>(null);
  const act = async (provider: ProviderId, action: () => Promise<unknown>, message: string) => {
    setWorking(provider); setError(''); setNotice('');
    try {await action(); await onChanged(); setNotice(message);}
    catch (reason) {setError(errorText(reason));}
    finally {setWorking('');}
  };
  return <section className="capability-settings-card capability-credentials">
    <header><KeyRound size={20}/><h2>提供方密钥与诊断</h2><span className="badge">只写，不回显</span></header>
    <p>密钥只提交到服务端加密存储，读取接口不会返回密钥。保存或删除需要 HTTPS 通道，并受版本冲突保护。</p>
    <ErrorNotice message={error}/><SuccessNotice message={notice}/>
    {value.providers.map(provider => {
      const credential = value.credentials[provider.id];
      return <div className="credential-row" key={provider.id}>
        <div><strong>{providerLabels[provider.id]}</strong><small>{provider.configured ? '服务依赖已配置' : '服务依赖未配置'} · 密钥{credential.present ? '已保存' : '未保存'} · 验证{provider.verification === 'verified' ? '已通过' : provider.verification === 'failed' ? '失败' : '未测试'}</small></div>
        <input type="password" autoComplete="new-password" aria-label={`${providerLabels[provider.id]} 新密钥`} value={drafts[provider.id] || ''} onChange={e => setDrafts({...drafts, [provider.id]: e.target.value})}/>
        <Button variant="secondary" disabled={!!working || busy || !(drafts[provider.id] || '').trim()} onClick={() => void act(provider.id, () => saveProviderCredential(provider.id, credential.revision, drafts[provider.id]), `${providerLabels[provider.id]} 密钥已保存；验证状态已重置，请运行诊断。`)}>保存密钥</Button>
        <Button variant="ghost" disabled={!!working || busy || !credential.present} onClick={() => void act(provider.id, () => deleteProviderCredential(provider.id, credential.revision), `${providerLabels[provider.id]} 密钥已删除。`)}>删除</Button>
        <Button variant="ghost" disabled={!!working || busy} onClick={() => void act(provider.id, async () => setDiagnostics(await runProviderDiagnostics(provider.id)), '诊断已完成。')}><Stethoscope size={15}/>诊断</Button>
        <span className="credential-revision">版本 {credential.revision}</span>
      </div>;
    })}
    {diagnostics && <div className="diagnostic-results"><strong>最近一次诊断：{providerLabels[diagnostics.provider_id]}</strong>
      {diagnostics.results.map(item => <p key={item.capability}>{capabilityLabels[item.capability as CapabilityKind] || item.capability}：{item.status === 'verified' ? '通过' : item.status === 'failed' ? `失败（${item.error_code || '未知'}）` : item.status === 'not_configured' ? '未配置，未发起外部请求' : '尚无可用适配器'}{item.latency_ms !== null ? ` · ${item.latency_ms} 毫秒` : ''}</p>)}
      <small>诊断只使用固定公开样例，不接受任意目标地址，单次尝试、总时限 15 秒，不记录响应正文或密钥。</small>
    </div>}
  </section>;
}

export function CapabilitySettingsView({allowed, state, onReload, onSave, onChange, onReloaded}: {
  allowed: boolean; state: CapabilityPageState; onReload: () => void; onSave: () => void;
  onChange: (value: CapabilityConfiguration) => void; onReloaded?: () => Promise<void>;
}) {
  if (!allowed) return <div className="page"><ErrorNotice message="仅普通部署的工作台管理员可以管理能力配置。"/></div>;
  const value = state.value;
  const busy = state.phase === 'loading' || state.phase === 'saving';
  const edit = (kind: CapabilityKind, route: CapabilityRoute) => {if (value) onChange({...value, [kind]: route});};
  return <div className="page capability-settings-page">
    <PageHeader eyebrow="系统能力" title="能力与来源" description="为所有模型配置共用的外部检索、读取、识别与图片搜索线路。调用仍受助手权限和部署策略约束。"
      actions={<Button variant="secondary" disabled={busy} onClick={onReload}><RefreshCw size={16}/>重新加载</Button>}/>
    <ErrorNotice message={state.error}/><SuccessNotice message={state.saved ? '配置已保存并立即用于后续工具调用；这不代表外部线路已验证。' : ''}/>
    {state.phase === 'loading' ? <Loader text="正在读取能力配置…"/> : !value ? <div className="info-strip">暂未取得配置。请重新加载；不会使用空配置覆盖服务端。</div> : <>
      <div className="info-strip"><ShieldCheck size={20}/><p>配置版本 {value.revision} · {value.origin === 'environment' ? '沿用部署环境默认值，尚未覆盖' : '管理员配置'}。网页搜索与读取在管理员显式保存前保持历史默认；文档识别与图片搜索默认关闭，文件外发默认拒绝。</p></div>
      <div className="capability-settings-grid">
        {KINDS.map(kind => <section key={kind} className="capability-settings-card">
          <header><Globe size={20}/><h2>{capabilityLabels[kind]}</h2><span className="badge">{!value[kind].enabled ? '已停用' : value.providers.find(p => p.id === value[kind].preferred)?.configured ? '已启用 · 待验证' : '首选服务未配置'}</span></header>
          <p>{kind === 'search' ? '返回候选网页及摘要，并登记真实外部来源。' : kind === 'fetch' ? '读取公开网页正文，保留原始网页链接和证据摘录。' : kind === 'ocr' ? '上传图片或扫描文档到已配置的识别服务；远端可能继续计费，因此固定不重试。' : '按关键词或反向图片查找相似公开图片。'}</p>
          <RouteEditor kind={kind} value={value[kind]} busy={busy} onEdit={route => edit(kind, route)}/>
        </section>)}
        {onReloaded && <CredentialPanel value={value} busy={busy} onChanged={onReloaded}/>}
      </div>
      <div className="capability-policy-note"><strong>权限与来源</strong><p>助手仍须单独开启对应能力，并通过成员访问权限检查。全局停用、域名收紧或审批要求会在每次实际尝试前、提供方响应后重新校验；权限拒绝不会触发回退。HTTP 重定向会重新检查目标域名，带凭据的请求不跨源转发授权头。</p><p>额度按每次真实尝试原子预占，重试和回退分别计数。已有 MCP 的逐工具授权和高风险审批保持不变。</p></div>
      <form onSubmit={e => {e.preventDefault(); onSave();}}><Button type="submit" busy={state.phase === 'saving'} disabled={busy || KINDS.some(k => !Number.isInteger(value[k].timeout_seconds) || value[k].timeout_seconds < 1 || value[k].timeout_seconds > 60)}>保存并应用</Button></form>
    </>}
  </div>;
}
