import { useCallback, useEffect, useState } from 'react';
import { Check, Copy, Pencil, Plus, RefreshCw, ShieldCheck, UserPlus, Users, X } from 'lucide-react';
import { api, errorText, patch, post } from './api';
import type { Approval, UsageField, UsageTotals, User } from './types';
import { Button, EmptyState, ErrorNotice, Field, Loader, Modal, PageHeader, SuccessNotice } from './components';

export function UsersPage({ currentUser }: { currentUser: User }) {
  const [items, setItems] = useState<User[]>([]); const [loading, setLoading] = useState(true); const [error, setError] = useState(''); const [success, setSuccess] = useState(''); const [editor, setEditor] = useState<User | 'new' | null>(null); const [inviting,setInviting]=useState(false);
  const load = useCallback(async () => { try { setItems(await api<User[]>('/users')); } catch (e) { setError(errorText(e)); } finally { setLoading(false); } }, []);
  useEffect(() => { void load(); }, [load]);
  return <div className="page"><PageHeader eyebrow="一起把工作做好" title="成员管理" description="为团队开通账号，管理角色与使用状态。" actions={<><Button variant="secondary" onClick={()=>setInviting(true)}><UserPlus size={17}/>邀请成员</Button><Button onClick={() => setEditor('new')}><Plus size={17}/>添加成员</Button></>}/><ErrorNotice message={error} onDismiss={() => setError('')}/><SuccessNotice message={success}/>{loading ? <Loader/> : !items.length ? <EmptyState icon={<Users size={30}/>} title="还没有成员" description="为团队添加第一个工作账号。"/> : <div className="table-container"><table><thead><tr><th>成员</th><th>角色</th><th>状态</th><th className="align-right">操作</th></tr></thead><tbody>{items.map(user => <tr key={user.id}><td><div className="member-cell"><span className="avatar">{(user.display_name || user.username).slice(0, 1)}</span><span><strong>{user.display_name || user.username}{user.id === currentUser.id && <small className="you-label">你</small>}</strong><small>{user.username}</small></span></div></td><td>{user.role === 'admin' ? '管理员' : '成员'}</td><td><span className={`badge ${user.enabled ? 'green' : ''}`}><span className="tiny-dot"/>{user.enabled ? '已启用' : '已停用'}</span></td><td className="align-right"><Button variant="ghost" onClick={() => setEditor(user)}><Pencil size={15}/>编辑</Button></td></tr>)}</tbody></table></div>}{editor && <UserEditor value={editor === 'new' ? null : editor} currentUser={currentUser} onClose={() => setEditor(null)} onSaved={async () => { setEditor(null); setSuccess('成员信息已保存。请通过安全渠道将初始密码告知成员。'); await load(); }}/>}{inviting&&<RegistrationInvite onClose={()=>setInviting(false)}/>}</div>;
}

export function RegistrationInvite({onClose}:{onClose:()=>void}) {
  const [value,setValue]=useState<{enabled:boolean;invite_code?:string}|null>(null),[error,setError]=useState(''),[copied,setCopied]=useState(false);
  useEffect(()=>{const controller=new AbortController();void api<{enabled:boolean;invite_code?:string}>('/users/registration-invite',{signal:controller.signal,cache:'no-store'}).then(data=>{if(!controller.signal.aborted)setValue(data);}).catch(err=>{if(!controller.signal.aborted)setError(errorText(err));});return()=>controller.abort();},[]);
  const link=typeof window==='undefined'?'':`${window.location.origin}${window.location.pathname}?register=1`;
  return <Modal title="邀请成员" onClose={onClose}><div className="modal-body"><ErrorNotice message={error}/>{!value&&!error?<Loader text="正在读取邀请信息…"/>:value?.enabled&&value.invite_code?<><p className="section-help">将注册链接和邀请码发给需要加入的同事。新账号是普通成员，可使用已分配给成员的团队资源。</p><Field label="注册链接"><input value={link} readOnly aria-label="注册链接" onFocus={e=>e.target.select()}/></Field><Field label="邀请码"><input value={value.invite_code} readOnly autoComplete="off" spellCheck={false} aria-label="邀请码" onFocus={e=>e.target.select()}/></Field><p className="section-help">邀请码用于加入当前工作空间，请只发给受邀成员。</p><SuccessNotice message={copied?'邀请信息已复制。':''}/></>:value?<p>当前未开放邀请注册，请联系工作台维护人员开启。</p>:null}</div><footer className="modal-footer"><Button variant="secondary" onClick={onClose}>关闭</Button>{value?.enabled&&value.invite_code&&<Button onClick={async()=>{try{if(!navigator.clipboard)throw new Error('当前浏览器不支持自动复制，请选中文本复制。');await navigator.clipboard.writeText(`注册链接：${link}\n邀请码：${value.invite_code}`);setCopied(true);setError('');}catch(err){setError(errorText(err));}}}><Copy size={15}/>复制邀请信息</Button>}</footer></Modal>;
}

function UserEditor({ value, currentUser, onClose, onSaved }: { value: User | null; currentUser: User; onClose: () => void; onSaved: () => void }) {
  const [username, setUsername] = useState(value?.username || ''); const [name, setName] = useState(value?.display_name || ''); const [role, setRole] = useState<'admin' | 'member'>(value?.role || 'member'); const [enabled, setEnabled] = useState(value?.enabled ?? true); const [password, setPassword] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  return <Modal title={value ? '编辑成员' : '添加成员'} onClose={() => { if (!busy) onClose(); }}><form onSubmit={async e => { e.preventDefault(); setBusy(true); setError(''); try { if (value) await patch(`/users/${value.id}`, { display_name: name, role, enabled, ...(password ? { password } : {}) }); else await post('/users', { username: username.trim(), display_name: name.trim(), role, password }); onSaved(); } catch (err) { setError(errorText(err)); } finally { setBusy(false); } }}><div className="modal-body"><ErrorNotice message={error}/><Field label="登录账号" required><input autoComplete="off" value={username} onChange={e => setUsername(e.target.value)} disabled={!!value} required maxLength={80}/></Field><Field label="显示名称" required><input value={name} onChange={e => setName(e.target.value)} required maxLength={120}/></Field><Field label={value ? '重设密码' : '初始密码'} hint={value ? '留空保留原密码。新密码至少 10 个字符。' : '至少 10 个字符。请通过安全渠道告知成员。'} required={!value}><input type="password" autoComplete="new-password" minLength={10} value={password} onChange={e => setPassword(e.target.value)} required={!value}/></Field><Field label="角色"><select value={role} disabled={value?.id === currentUser.id} onChange={e => setRole(e.target.value as 'admin' | 'member')}><option value="member">成员 · 使用已授权的助手</option><option value="admin">管理员 · 管理连接、助手与成员</option></select></Field>{value && <label className="checkbox-row separated"><input type="checkbox" checked={enabled} disabled={value.id === currentUser.id} onChange={e => setEnabled(e.target.checked)}/><span>启用账号<small>停用后，该成员将无法继续使用工作台。</small></span></label>}</div><footer className="modal-footer"><Button variant="secondary" type="button" disabled={busy} onClick={onClose}>取消</Button><Button type="submit" busy={busy}>保存成员</Button></footer></form></Modal>;
}

const numberText = (value: unknown) => Number.isFinite(Number(value)) ? Number(value).toLocaleString('zh-CN') : '—';
const costText = (value: unknown) => value === null || value === undefined ? '未提供' : Number.isFinite(Number(value)) ? `$${Number(value).toFixed(4)}` : '未提供';
export const approvalProgress = (approval: Approval) => ({ votes: approval.approver_ids?.length || 0, required: approval.required_approvals || 2 });

export function usageValue(usage: UsageTotals, field: UsageField): string {
  const value = usage[field];
  if (value === null || value === undefined || typeof value !== 'number' || !Number.isFinite(value) || (usage.field_coverage?.[field]?.unknown_runs ?? 0) > 0) return '未完整报告';
  return field === 'estimated_cost_usd' ? costText(value) : numberText(value);
}
export function usageCoverage(usage: UsageTotals, field: UsageField): string {
  const coverage = usage.field_coverage?.[field];
  if (!coverage) return '报告覆盖范围未提供';
  const label = `${coverage.known_runs} / ${usage.usage_records ?? usage.total_runs} ${usage.usage_records === undefined ? '个任务' : '条记录'}已报告`;
  const subtotal = usage.known_subtotals?.[field];
  if (typeof subtotal !== 'number' || !Number.isFinite(subtotal) || (coverage.known_runs === 0 && subtotal === 0)) return label;
  return `已知小计 ${field === 'estimated_cost_usd' ? costText(subtotal) : numberText(subtotal)} · ${label}`;
}

export { UsagePage } from './UsagePage';

export function ApprovalsPage() {
  const [items, setItems] = useState<Approval[]>([]); const [loading, setLoading] = useState(true); const [busy, setBusy] = useState(''); const [error, setError] = useState('');
  const load = useCallback(async () => { setLoading(true); setError(''); try { setItems(await api<Approval[]>('/approvals/pending')); } catch (e) { setError(errorText(e)); } finally { setLoading(false); } }, []);
  useEffect(() => { void load(); }, [load]);
  const decide = async (approval: Approval, decision: 'allow' | 'deny') => { setBusy(approval.id); setError(''); try { await post(`/approvals/${approval.id}/decision`, { decision }); await load(); } catch (e) { setError(errorText(e)); } finally { setBusy(''); } };
  return <div className="page"><PageHeader eyebrow="高风险控制" title="待审批操作" description="这里只显示分配给当前管理员的高风险双人审批请求。" actions={<Button variant="secondary" busy={loading} onClick={() => void load()}><RefreshCw size={16}/>刷新</Button>}/><ErrorNotice message={error}/>{loading ? <Loader/> : !items.length ? <EmptyState icon={<ShieldCheck size={30}/>} title="暂无待审批操作" description="当前没有分配给你的高风险双人审批请求。"/> : <div className="approval-queue">{items.map(approval => { const { votes, required } = approvalProgress(approval); return <section className="approval-card" key={approval.id}><div className="approval-title"><ShieldCheck size={18}/><strong>高风险操作 · 双人审批</strong></div><h3>{approval.tool_name}</h3><p className="approval-impact">管理员投票进度：{votes}/{required} · 参数不可修改</p><details><summary>查看参数摘要</summary><pre>{JSON.stringify(approval.arguments, null, 2)}</pre></details><small>有效至 {approval.expires_at}</small><div className="approval-actions"><Button variant="secondary" disabled={busy === approval.id} onClick={() => void decide(approval, 'deny')}><X size={15}/>拒绝</Button><Button busy={busy === approval.id} disabled={busy !== ''} onClick={() => void decide(approval, 'allow')}><Check size={15}/>投票允许</Button></div></section>;})}</div>}</div>;
}
