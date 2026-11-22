import { useEffect, useRef, useState } from 'react';
import { errorText, post } from './api';
import { Button, ErrorNotice, Field, Loader, Modal } from './components';
import { ScopeField, type ResourceScope } from './resources';
import type { Connection } from './types';

const handoffFragment = /^#kaiwu-mcp=(kw_link_[A-Za-z0-9_-]{43})$/;

export function kaiwuHandoffCode(fragment: string): string | null {
  return fragment.match(handoffFragment)?.[1] || null;
}

type Source = { tenant_name: string; name: string; allowed_tools: string[] };
type Preview = Source | { connection: Connection };
type Redemption = { mcp_config_json: string } | { connection: Connection };

export default function KaiwuHandoffDialog({ code, admin, onClose, onImported }: {
  code: string;
  admin: boolean;
  onClose: () => void;
  onImported: (connection: Connection) => void;
}) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [name, setName] = useState('');
  const [scope, setScope] = useState<ResourceScope>('private');
  const [loading, setLoading] = useState(admin);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [config, setConfig] = useState('');
  const [copyStatus, setCopyStatus] = useState('');
  const redeeming = useRef(false);

  useEffect(() => {
    if (!admin) return;
    let active = true;
    post<Preview>('/connections/kaiwu-handoff/preview', { handoff_code: code }).then(result => {
      if (!active) return;
      setPreview(result);
      if ('name' in result) setName(result.name);
    }).catch(reason => {
      if (active) setError(errorText(reason));
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [admin, code]);

  async function redeem(action: 'copy' | 'import') {
    if (redeeming.current || !name.trim()) return;
    redeeming.current = true;
    setBusy(true);
    setError('');
    try {
      const result = await post<Redemption>('/connections/kaiwu-handoff/redeem', {
        handoff_code: code, action, name: name.trim(), scope,
      });
      if ('connection' in result) onImported(result.connection);
      else setConfig(result.mcp_config_json);
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      redeeming.current = false;
      setBusy(false);
    }
  }

  async function copyConfig() {
    try {
      await navigator.clipboard.writeText(config);
      setCopyStatus('已复制完整配置。');
    } catch {
      setCopyStatus('浏览器未能自动复制，请从文本框手动复制。');
    }
  }

  const source = preview && 'tenant_name' in preview ? preview : null;
  const imported = preview && 'connection' in preview ? preview.connection : null;
  return <Modal title="导入企业内网 MCP" wide onClose={() => { if (!redeeming.current) onClose(); }}>
    <div className="modal-body">
      <ErrorNotice message={error}/>
      {!admin && <p>请使用有权管理该企业接入的开物管理员账号打开此链接。</p>}
      {loading && <Loader text="正在读取连接器提交的来源…"/>}
      {config ? <>
        <Field label="完整 MCP 配置" hint="配置含开物访问凭据，请只交给获授权的导入人员。"><textarea className="config-input" value={config} readOnly rows={11} spellCheck={false}/></Field>
        {copyStatus && <p role="status">{copyStatus}</p>}
      </> : source ? <>
        <p>{source.tenant_name} 已提交此业务来源。导入后可测试工具列表，再在助手设置中确认工具权限。</p>
        <Field label="连接名称" required><input maxLength={100} value={name} disabled={busy} onChange={event => setName(event.target.value)}/></Field>
        <ScopeField value={scope} onChange={setScope} admin disabled={busy}/>
        <p>客户批准的工具</p>
        <ul>{source.allowed_tools.map(tool => <li key={tool}>{tool}</li>)}</ul>
      </> : imported && <p>已导入「{imported.name}」。</p>}
    </div>
    <footer className="modal-footer">
      <Button variant="secondary" disabled={busy} onClick={onClose}>关闭</Button>
      {config ? <Button onClick={() => void copyConfig()}>复制 JSON</Button> : source ? <>
        <Button variant="secondary" disabled={busy || !name.trim()} onClick={() => void redeem('copy')}>生成可复制的 JSON</Button>
        <Button busy={busy} disabled={!name.trim()} onClick={() => void redeem('import')}>导入并测试</Button>
      </> : imported && <Button onClick={() => onImported(imported)}>查看连接</Button>}
    </footer>
  </Modal>;
}
