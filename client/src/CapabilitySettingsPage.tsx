import { useEffect, useReducer } from 'react';
import { Globe, RefreshCw, ShieldCheck } from 'lucide-react';
import { errorText } from './api';
import { Button, ErrorNotice, Field, Loader, PageHeader, SuccessNotice } from './components';
import { capabilityInitialState, capabilityPageReducer, canManageCapabilities, loadCapabilitySettings, providerLabels, saveCapabilitySettings } from './capabilitySettings';
import type { CapabilityConfiguration, CapabilityKind, CapabilityPageState, CapabilityRoute, ProviderId } from './capabilitySettings';
import type { AppConfig } from './types';
import './capability-settings.css';

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
    onChange={value => dispatch({type: 'edit', value})}/>;
}

export function CapabilitySettingsView({allowed, state, onReload, onSave, onChange}: {
  allowed: boolean; state: CapabilityPageState; onReload: () => void; onSave: () => void; onChange: (value: CapabilityConfiguration) => void;
}) {
  if (!allowed) return <div className="page"><ErrorNotice message="仅普通部署的工作台管理员可以管理能力配置。"/></div>;
  const value = state.value;
  const busy = state.phase === 'loading' || state.phase === 'saving';
  const edit = (kind: CapabilityKind, route: CapabilityRoute) => {if (value) onChange({...value, [kind]: route});};
  return <div className="page capability-settings-page">
    <PageHeader eyebrow="系统能力" title="能力与来源" description="为所有模型配置共用的外部检索线路。调用仍受助手权限和部署策略约束。"
      actions={<Button variant="secondary" disabled={busy} onClick={onReload}><RefreshCw size={16}/>重新加载</Button>}/>
    <ErrorNotice message={state.error}/><SuccessNotice message={state.saved ? '配置已保存并立即用于后续工具调用；这不代表外部线路已验证。' : ''}/>
    {state.phase === 'loading' ? <Loader text="正在读取能力配置…"/> : !value ? <div className="info-strip">暂未取得配置。请重新加载；不会使用空配置覆盖服务端。</div> : <>
      <div className="info-strip"><ShieldCheck size={20}/><p>配置版本 {value.revision} · {value.origin === 'environment' ? '沿用部署环境默认值，尚未覆盖' : '管理员配置'}。密钥和服务地址仍由部署管理员在服务端配置，本页不读取或保存密钥。</p></div>
      <div className="capability-settings-grid">
        {(['search', 'fetch'] as const).map(kind => <section key={kind} className="capability-settings-card">
          <header><Globe size={20}/><h2>{kind === 'search' ? '网页搜索' : '网页读取'}</h2><span className="badge">{!value[kind].enabled ? '已停用' : value.providers.find(p => p.id === value[kind].preferred)?.configured ? '已启用 · 待验证' : '首选服务未配置'}</span></header>
          <p>{kind === 'search' ? '返回候选网页及摘要，并登记真实外部来源。' : '读取公开网页正文，保留原始网页链接和证据摘录。'}</p>
          <fieldset disabled={busy}>
            <label className="checkbox-row"><input type="checkbox" checked={value[kind].enabled} onChange={e => edit(kind, {...value[kind], enabled: e.target.checked})}/>允许此能力</label>
            <Field label={`${kind === 'search' ? '网页搜索' : '网页读取'}首选提供方`}><select value={value[kind].preferred} onChange={e => edit(kind, {...value[kind], preferred: e.target.value as ProviderId, fallbacks: value[kind].fallbacks.filter(id => id !== e.target.value)})}>
              {value.providers.filter(p => p.capabilities.includes(kind)).map(p => <option key={p.id} value={p.id} disabled={!p.configured}>{providerLabels[p.id]} · {p.configured ? '已配置，待验证' : '未配置'}</option>)}
            </select></Field>
            <Field label="回退服务" hint="仅服务故障或限流时回退；权限或安全拒绝不会触发回退。">
              <select value={value[kind].fallbacks[0] || ''} onChange={e => edit(kind, {...value[kind], fallbacks: e.target.value ? [e.target.value as ProviderId] : []})}>
                <option value="">不回退</option>{value.providers.filter(p => p.capabilities.includes(kind) && p.id !== value[kind].preferred).map(p => <option key={p.id} value={p.id} disabled={!p.configured}>{providerLabels[p.id]} · {p.configured ? '已配置，待验证' : '未配置'}</option>)}
              </select>
            </Field>
            <Field label="调用总超时（秒）" hint="首选和回退共用此时间上限，范围 1–60 秒。"><input type="number" min={1} max={60} step={1} required value={value[kind].timeout_seconds} onChange={e => edit(kind, {...value[kind], timeout_seconds: Number(e.target.value)})}/></Field>
          </fieldset>
        </section>)}
        <section className="capability-settings-card"><header><h2>文档识别</h2><span className="badge amber">独立工具未接入</span></header><p>对话中的独立文档识别尚未接入，不能在此启用。</p><p>知识库摄取的外部识别回退：{value.ocr.knowledge_fallback_configured ? '已配置，待验证' : '未配置'}。本页保存不会开启文档外发。</p></section>
        <section className="capability-settings-card"><header><h2>图片搜索</h2><span className="badge amber">未接入</span></header><p>尚无可调用的独立图片搜索适配器，不提供无效开关。</p></section>
      </div>
      <div className="capability-policy-note"><strong>权限与来源</strong><p>助手仍须开启“网页搜索”，并通过成员访问权限检查。全局停用在工具执行时重新校验；回退不会绕过权限。网页链接只来自服务端登记的真实结果。</p><p>本页不提供额度计费、域名白名单或独立文档识别授权；这些能力尚未接入此配置流程。已有 MCP 的逐工具授权和高风险审批保持不变。</p></div>
      <form onSubmit={e => {e.preventDefault(); onSave();}}><Button type="submit" busy={state.phase === 'saving'} disabled={busy || (['search','fetch'] as const).some(k => !Number.isInteger(value[k].timeout_seconds) || value[k].timeout_seconds < 1 || value[k].timeout_seconds > 60)}>保存并应用</Button></form>
    </>}
  </div>;
}
