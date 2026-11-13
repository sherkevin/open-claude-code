import { useCallback, useEffect, useState } from 'react';
import { Ban, Check, History, Package, Plus, RefreshCw, ShieldCheck } from 'lucide-react';
import { api, errorText, post } from './api';
import { Button, EmptyState, ErrorNotice, Field, Loader, Modal, PageHeader, SuccessNotice } from './components';
import type { PluginEvent, PluginManifest, PluginPermission } from './types';

export const permissionLabels: Record<PluginPermission, string> = {
  'workspace.files.read': '读取当前会话工作区文件',
  'workspace.files.write': '写入当前会话工作区文件',
  'knowledge.read': '读取已授权知识库',
  'web.read': '读取公开网页',
  'mcp.connection.read': '读取业务连接',
  'artifact.write': '生成可下载产物',
  'memory.read': '读取当前会话记忆',
};

export const permissionOptions = Object.keys(permissionLabels) as PluginPermission[];

export const pluginStatus = (status: PluginManifest['status']) => ({
  pending: { label: '待审核', tone: 'amber', description: '来源、版本、许可证与权限尚未批准，不能被运行时使用。' },
  approved: { label: '已批准', tone: 'green', description: '管理员已核验来源、版本、许可证和声明权限。' },
  revoked: { label: '已撤销', tone: '', description: '授权已撤销，后续不得再被运行时加载。' },
}[status]);

export const isDigest = (value: string) => /^[0-9a-f]{64}$/.test(value);
export const isSecureSource = (value: string) => {
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'https:' && !!parsed.hostname && !parsed.username && !parsed.password && !parsed.hash;
  } catch {
    return false;
  }
};

export default function PluginsPage() {
  const [items, setItems] = useState<PluginManifest[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [registering, setRegistering] = useState(false);
  const [decision, setDecision] = useState<{ plugin: PluginManifest; action: 'approve' | 'revoke' } | null>(null);
  const [events, setEvents] = useState<{ plugin: PluginManifest; items: PluginEvent[] } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setItems(await api<PluginManifest[]>('/admin/plugins'));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const openEvents = async (plugin: PluginManifest) => {
    setError('');
    try {
      setEvents({ plugin, items: await api<PluginEvent[]>(`/admin/plugins/${plugin.id}/events`) });
    } catch (e) {
      setError(errorText(e));
    }
  };

  return <div className="page">
    <PageHeader
      eyebrow="第三方能力治理"
      title="插件治理"
      description="登记插件的来源、版本、许可证摘要和申请权限；管理员逐项核验后才能批准，撤销后立即失效。服务端不会自动下载或执行插件代码。"
      actions={<><Button variant="secondary" busy={loading} onClick={() => void load()}><RefreshCw size={16}/>刷新</Button><Button onClick={() => setRegistering(true)}><Plus size={17}/>登记插件</Button></>}
    />
    <ErrorNotice message={error} onDismiss={() => setError('')}/>
    <SuccessNotice message={success}/>
    {loading ? <Loader/> : !items.length ? <EmptyState icon={<Package size={30}/>} title="还没有登记插件" description="登记公开插件的版本与权限申请，提交管理员审核。"/> : <div className="resource-grid">{items.map(item => {
      const status = pluginStatus(item.status);
      return <article className="resource-card" key={item.id}>
        <div className="resource-top"><Package size={23}/><span className={`badge ${status.tone}`}><ShieldCheck size={12}/>{status.label}</span></div>
        <h2>{item.name} <small>v{item.version}</small></h2>
        <p>{item.review_note || status.description}</p>
        <div className="resource-meta">
          <span>许可证：{item.license}</span>
          <span>登记版本：r{item.revision}</span>
          <span>{item.declared_permissions.length} 项权限申请</span>
        </div>
        <dl className="plugin-facts">
          <div><dt>来源</dt><dd><a href={item.source_url} target="_blank" rel="noopener noreferrer">{item.source_url}</a></dd></div>
          <div><dt>摘要</dt><dd className="hash-line">{item.package_sha256}</dd></div>
          <div><dt>权限</dt><dd>{item.declared_permissions.length ? item.declared_permissions.map(permission => permissionLabels[permission]).join('、') : '未申请权限'}</dd></div>
          <div><dt>时间</dt><dd>{item.updated_at}</dd></div>
        </dl>
        <footer>
          {item.status === 'pending' && <Button onClick={() => setDecision({ plugin: item, action: 'approve' })}><Check size={15}/>审核并批准</Button>}
          {item.status !== 'revoked' && <Button variant="danger" onClick={() => setDecision({ plugin: item, action: 'revoke' })}><Ban size={15}/>撤销授权</Button>}
          <Button variant="ghost" onClick={() => void openEvents(item)}><History size={15}/>审核记录</Button>
        </footer>
      </article>;
    })}</div>}
    {registering && <RegisterPlugin onClose={() => setRegistering(false)} onSaved={() => { setRegistering(false); setSuccess('插件已登记，等待管理员核验来源、版本、许可证和权限。'); void load(); }}/>}
    {decision && <PluginDecision plugin={decision.plugin} action={decision.action} onClose={() => setDecision(null)} onSaved={() => { setSuccess(decision.action === 'approve' ? '插件已批准。' : '插件授权已撤销。'); setDecision(null); void load(); }}/>}
    {events && <Modal title={`${events.plugin.name} · 审核记录`} description={`v${events.plugin.version} · ${events.plugin.package_sha256.slice(0, 12)}…`} wide onClose={() => setEvents(null)}><div className="modal-body">{!events.items.length ? <EmptyState icon={<History size={28}/>} title="没有审核记录" description="登记、批准和撤销都会写入不可变的审计记录。"/> : <div className="table-container"><table><thead><tr><th>动作</th><th>时间</th><th>审核说明</th></tr></thead><tbody>{events.items.map(event => <tr key={event.id}><td>{event.action === 'registered' ? '登记' : event.action === 'approved' ? '批准' : '撤销'}</td><td>{event.created_at}</td><td>{String(event.details.review_note || '—')}</td></tr>)}</tbody></table></div>}</div><footer className="modal-footer"><Button variant="secondary" onClick={() => setEvents(null)}>关闭</Button></footer></Modal>}
  </div>;
}

function RegisterPlugin({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState('');
  const [version, setVersion] = useState('');
  const [source, setSource] = useState('');
  const [license, setLicense] = useState('MIT');
  const [digest, setDigest] = useState('');
  const [permissions, setPermissions] = useState<PluginPermission[]>([]);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const invalidDigest = digest.length > 0 && !isDigest(digest);
  const invalidSource = source.length > 0 && !isSecureSource(source);
  const toggle = (permission: PluginPermission) => setPermissions(value => value.includes(permission) ? value.filter(item => item !== permission) : [...value, permission]);
  return <Modal title="登记插件" description="只登记公开来源的固定版本；登记不会下载或执行插件代码。" wide onClose={() => { if (!busy) onClose(); }}>
    <form onSubmit={async event => {
      event.preventDefault();
      if (!isDigest(digest) || !isSecureSource(source)) { setError('请填写合法的 HTTPS 来源和 64 位小写十六进制摘要。'); return; }
      setBusy(true); setError('');
      try {
        await post('/admin/plugins', {
          name: name.trim(), version: version.trim(), source_url: source.trim(), license: license.trim(),
          package_sha256: digest.trim(), declared_permissions: permissions, review_note: note,
        });
        onSaved();
      } catch (e) {
        setError(errorText(e));
      } finally {
        setBusy(false);
      }
    }}>
      <div className="modal-body">
        <ErrorNotice message={error}/>
        <div className="two-col">
          <Field label="插件名称" required><input value={name} onChange={e => setName(e.target.value)} maxLength={100} required/></Field>
          <Field label="版本" required hint="固定版本，例如 1.2.0；同一版本不能更换摘要。"><input value={version} onChange={e => setVersion(e.target.value)} maxLength={64} required placeholder="1.2.0"/></Field>
        </div>
        <Field label="来源地址" required hint="必须是 HTTPS 地址，不接受内嵌凭据或锚点。" ><input value={source} onChange={e => setSource(e.target.value)} maxLength={2048} required placeholder="https://github.com/example/plugin"/></Field>
        {invalidSource && <small className="field-error">来源必须是有效的 HTTPS 地址。</small>}
        <div className="two-col">
          <Field label="许可证" required hint="当前只允许 MIT、Apache-2.0、BSD、ISC 等宽松许可证。"><input value={license} onChange={e => setLicense(e.target.value)} maxLength={64} required/></Field>
          <Field label="安装包 SHA-256" required hint="64 位小写十六进制摘要。"><input value={digest} onChange={e => setDigest(e.target.value.trim().toLowerCase())} maxLength={64} required spellCheck={false} placeholder="a1b2…" className="mono-input"/></Field>
        </div>
        {invalidDigest && <small className="field-error">摘要必须是 64 位小写十六进制字符。</small>}
        <Field label="申请的权限" hint="只勾选插件实际需要的最小权限；批准后仍由服务端逐项强制校验。">
          <div className="permission-grid">{permissionOptions.map(permission => <label className="checkbox-row" key={permission}><input type="checkbox" checked={permissions.includes(permission)} onChange={() => toggle(permission)}/><span>{permissionLabels[permission]}<small>{permission}</small></span></label>)}</div>
        </Field>
        <Field label="审核说明" hint="说明来源可信度、许可证核验结果和权限最小化理由。"><textarea rows={4} value={note} onChange={e => setNote(e.target.value)} maxLength={2000}/></Field>
      </div>
      <footer className="modal-footer"><Button variant="secondary" type="button" disabled={busy} onClick={onClose}>取消</Button><Button type="submit" busy={busy}>提交审核</Button></footer>
    </form>
  </Modal>;
}

function PluginDecision({ plugin, action, onClose, onSaved }: { plugin: PluginManifest; action: 'approve' | 'revoke'; onClose: () => void; onSaved: () => void }) {
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const approve = action === 'approve';
  return <Modal title={approve ? `批准 ${plugin.name} v${plugin.version}` : `撤销 ${plugin.name} v${plugin.version}`} description={approve ? '批准前请确认来源、版本、许可证和声明权限都已核验。' : '撤销后，运行时不能再使用这个插件版本。'} onClose={() => { if (!busy) onClose(); }}>
    <form onSubmit={async event => {
      event.preventDefault(); setBusy(true); setError('');
      try {
        await post(`/admin/plugins/${plugin.id}/${action}`, { review_note: note });
        onSaved();
      } catch (e) {
        setError(errorText(e));
      } finally {
        setBusy(false);
      }
    }}>
      <div className="modal-body">
        <ErrorNotice message={error}/>
        <dl className="plugin-review">
          <div><dt>许可证</dt><dd>{plugin.license}</dd></div>
          <div><dt>摘要</dt><dd className="hash-line">{plugin.package_sha256}</dd></div>
          <div><dt>申请权限</dt><dd>{plugin.declared_permissions.length ? plugin.declared_permissions.map(permission => permissionLabels[permission]).join('、') : '未申请权限'}</dd></div>
          <div><dt>来源</dt><dd>{plugin.source_url}</dd></div>
        </dl>
        <Field label="审核说明" required hint={approve ? '记录许可证和权限核验结论。' : '记录撤销原因，例如上游安全公告。'}><textarea rows={4} value={note} onChange={e => setNote(e.target.value)} maxLength={2000} required/></Field>
      </div>
      <footer className="modal-footer"><Button variant="secondary" type="button" disabled={busy} onClick={onClose}>取消</Button><Button variant={approve ? 'primary' : 'danger'} type="submit" busy={busy}>{approve ? '确认批准' : '确认撤销'}</Button></footer>
    </form>
  </Modal>;
}
