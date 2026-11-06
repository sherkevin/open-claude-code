import { useEffect, useState } from 'react';
import { Check, RefreshCw, ShieldCheck } from 'lucide-react';
import { api, errorText } from './api';
import { Button, ErrorNotice, Field } from './components';
import type { HighRiskApprovalPolicy, ModelApprovalPolicy } from './types';

/** Parse administrator-entered comma/newline lists without creating wildcard entries. */
export function parsePolicyList(value: string): string[] {
  return [...new Set(value.split(/[\n,]/).map(item => item.trim()).filter(item => item && item !== '*' && item.toLowerCase() !== 'all'))];
}

export function modelPolicyDefaults(assistantId: string): ModelApprovalPolicy {
  return { assistant_id: assistantId, enabled: false, reviewer_mode: 'synthetic', model_id: 'synthetic-policy-v1', allowed_tools: [], allowed_data_scopes: [], max_amount: 0, allowed_operations: [], revision: 0 };
}

export function highRiskPolicyDefaults(assistantId: string): HighRiskApprovalPolicy {
  return { assistant_id: assistantId, enabled: false, policy_id: `high-risk:${assistantId}`, allowed_tools: [], admin_allowlist: [], required_approvals: 2, revision: 0 };
}

function listText(value: string[]) { return value.join(', '); }

/**
 * Administrator-only policy controls.  The server remains authoritative: this
 * panel only edits explicit lists and never accepts a wildcard or credential.
 */
export default function ApprovalPolicyPanel({ assistantId, admin }: { assistantId: string; admin: boolean }) {
  const [model, setModel] = useState<ModelApprovalPolicy>(() => modelPolicyDefaults(assistantId));
  const [highRisk, setHighRisk] = useState<HighRiskApprovalPolicy>(() => highRiskPolicyDefaults(assistantId));
  const [loading, setLoading] = useState(admin);
  const [busy, setBusy] = useState<'model' | 'high-risk' | null>(null);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  useEffect(() => {
    if (!admin) return;
    const controller = new AbortController();
    setLoading(true); setError('');
    Promise.all([
      api<ModelApprovalPolicy>(`/assistants/${encodeURIComponent(assistantId)}/model-approval`, { signal: controller.signal }),
      api<HighRiskApprovalPolicy>(`/assistants/${encodeURIComponent(assistantId)}/high-risk-approval`, { signal: controller.signal }),
    ]).then(([nextModel, nextHighRisk]) => {
      setModel(nextModel); setHighRisk(nextHighRisk);
    }).catch(reason => { if (!controller.signal.aborted) setError(errorText(reason));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [assistantId, admin]);

  if (!admin) return null;

  const saveModel = async () => {
    setBusy('model'); setError(''); setSuccess('');
    try {
      const saved = await api<ModelApprovalPolicy>(`/assistants/${encodeURIComponent(assistantId)}/model-approval`, {
        method: 'PUT',
        body: JSON.stringify({
          enabled: model.enabled,
          reviewer_mode: 'synthetic',
          model_id: model.model_id || 'synthetic-policy-v1',
          allowed_tools: model.allowed_tools,
          allowed_data_scopes: model.allowed_data_scopes,
          max_amount: Number.isFinite(model.max_amount) && model.max_amount >= 0 ? model.max_amount : 0,
          allowed_operations: model.allowed_operations,
        }),
      });
      setModel(saved); setSuccess('模型代审策略已保存。');
    } catch (reason) { setError(errorText(reason)); } finally { setBusy(null); }
  };

  const saveHighRisk = async () => {
    setBusy('high-risk'); setError(''); setSuccess('');
    try {
      const saved = await api<HighRiskApprovalPolicy>(`/assistants/${encodeURIComponent(assistantId)}/high-risk-approval`, {
        method: 'PUT',
        body: JSON.stringify({ enabled: highRisk.enabled, policy_id: highRisk.policy_id, allowed_tools: highRisk.allowed_tools, admin_allowlist: highRisk.admin_allowlist, required_approvals: 2 }),
      });
      setHighRisk(saved); setSuccess('高风险双人审批策略已保存。');
    } catch (reason) { setError(errorText(reason)); } finally { setBusy(null); }
  };

  return <section className="configuration-section" aria-label="审批策略">
    <header><span className="capability-section-icon"><ShieldCheck size={21}/></span><div><h2>审批策略</h2><p>默认拒绝；这里只配置显式白名单，不会调用外部模型或暴露凭据。</p></div></header>
    {loading&&<p className="section-help">正在读取审批策略…</p>}<ErrorNotice message={error}/>{success && <p className="form-note"><Check size={15}/>{success}</p>}
    <div className="configuration-duo">
      <div className="approval-policy-card" aria-label="模型代审策略">
        <h3>模型代审（仅低风险）</h3>
        <p className="section-help">仅允许 Read、Glob、Grep；中高风险和未知工具始终转人工。</p>
        <label className="checkbox-row"><input type="checkbox" checked={model.enabled} onChange={event => setModel(previous => ({ ...previous, enabled: event.target.checked }))}/><span>启用本地合成代审<small>未启用或异常时默认拒绝</small></span></label>
        <Field label="低风险工具（逗号分隔）"><input value={listText(model.allowed_tools)} onChange={event => setModel(previous => ({ ...previous, allowed_tools: parsePolicyList(event.target.value) }))} placeholder="Read, Glob, Grep"/></Field>
        <Field label="数据范围白名单"><input value={listText(model.allowed_data_scopes)} onChange={event => setModel(previous => ({ ...previous, allowed_data_scopes: parsePolicyList(event.target.value) }))} placeholder="workspace, knowledge:quality"/></Field>
        <Field label="操作白名单"><input value={listText(model.allowed_operations)} onChange={event => setModel(previous => ({ ...previous, allowed_operations: parsePolicyList(event.target.value) }))} placeholder="read, list"/></Field>
        <Field label="金额上限（本地策略单位）"><input type="number" min="0" step="0.01" value={model.max_amount} onChange={event => setModel(previous => ({ ...previous, max_amount: Math.max(0, Number(event.target.value) || 0) }))}/></Field>
        <Button type="button" busy={busy === 'model'} disabled={busy !== null} onClick={() => void saveModel()}><RefreshCw size={15}/>保存模型代审</Button>
      </div>
      <div className="approval-policy-card" aria-label="高风险双人审批策略">
        <h3>高风险双人审批</h3>
        <p className="section-help">必须两名不同的启用管理员；空白名单保持拒绝，不是全员放行。</p>
        <label className="checkbox-row"><input type="checkbox" checked={highRisk.enabled} onChange={event => setHighRisk(previous => ({ ...previous, enabled: event.target.checked }))}/><span>启用双人审批<small>策略变更会使旧审批失效</small></span></label>
        <Field label="高风险工具白名单"><input value={listText(highRisk.allowed_tools)} onChange={event => setHighRisk(previous => ({ ...previous, allowed_tools: parsePolicyList(event.target.value) }))} placeholder="mcp__danger__delete"/></Field>
        <Field label="管理员用户标识白名单"><textarea rows={3} value={listText(highRisk.admin_allowlist)} onChange={event => setHighRisk(previous => ({ ...previous, admin_allowlist: parsePolicyList(event.target.value) }))} placeholder="每行一个管理员用户标识"/></Field>
        <p className="form-note"><ShieldCheck size={15}/>固定要求：两名不同管理员，发起人不能批准自己的请求。</p>
        <Button type="button" busy={busy === 'high-risk'} disabled={busy !== null} onClick={() => void saveHighRisk()}><RefreshCw size={15}/>保存双人审批</Button>
      </div>
    </div>
  </section>;
}
