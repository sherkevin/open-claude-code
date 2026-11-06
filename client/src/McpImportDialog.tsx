import { useState } from 'react';
import { ArrowLeft, ArrowRight, CheckCircle2, KeyRound, Link2, Upload } from 'lucide-react';
import { post } from './api';
import { Button, ErrorNotice, Field, Modal } from './components';
import { importErrorText, parseMcpConfig } from './integration';
import { ScopeField, type ResourceScope } from './resources';
import type { MCPImportPreview, MCPImportResult } from './types';

const sample = '{\n  "mcpServers": {\n    "knowledge": {\n      "type": "http",\n      "url": "https://mcp.example.com/mcp"\n    }\n  }\n}';

export default function McpImportDialog({ onClose, onImported,admin=false }: { admin?:boolean;onClose: () => void; onImported: (result: MCPImportResult) => void }) {
  const [scope,setScope]=useState<ResourceScope>('private');
  const [source, setSource] = useState('');
  const [preview, setPreview] = useState<MCPImportPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const close = () => { if (!busy) { setSource(''); setPreview(null); onClose(); } };
  const inspect = async () => {
    setError(''); setBusy(true);
    try { setPreview(await post<MCPImportPreview>('/connections/import/preview', { config: parseMcpConfig(source),scope })); }
    catch (e) { setError(importErrorText(e)); }
    finally { setBusy(false); }
  };
  const commit = async () => {
    setError(''); setBusy(true);
    try {
      const result = await post<MCPImportResult>('/connections/import', { config: parseMcpConfig(source),scope });
      setSource(''); setPreview(null); onImported(result);
    } catch (e) { setError(importErrorText(e)); }
    finally { setBusy(false); }
  };
  return <Modal title="导入 MCP 配置" description="复制现有配置，确认连接后一次导入。每批最多 16 个连接。" wide onClose={close}>
    <div className="modal-body">
      <ol className="import-steps" aria-label="导入步骤"><li className={!preview ? 'active' : ''}><span>1</span>粘贴配置</li><li className={preview ? 'active' : ''}><span>2</span>确认连接</li></ol>
      <ErrorNotice message={error}/>
      {!preview ? <>
        <ScopeField value={scope} onChange={setScope} admin={admin}/>
        <Field label="MCP 配置 JSON" hint="支持 OpenAI Responses 的 MCP tool 对象／数组，以及包含 mcpServers 的标准配置。" required>
          <textarea className="config-input" value={source} onChange={event => setSource(event.target.value)} placeholder={sample} rows={11} spellCheck={false} autoComplete="off" autoCorrect="off" autoCapitalize="off" disabled={busy}/>
        </Field>
        <p className="form-note"><KeyRound size={15}/>认证字段可随配置提交，确认页只显示是否提供，不显示原文。关闭窗口会清空输入。</p>
        <details className="import-help"><summary>支持的字段与认证方式</summary><p>Responses：type、server_label、server_url、authorization、allowed_tools、require_approval。</p><p>标准配置：mcpServers 下的名称、type: http、url，以及 headers.Authorization 中的 Bearer Token。</p><p>服务地址不应包含凭据或查询参数；自定义认证头和本地命令不在本次导入范围。</p></details>
      </> : <>
        <div className="import-summary"><CheckCircle2 size={20}/><div><strong>识别到 {preview.connections.length} 个连接</strong><p>{preview.format === 'openai_mcp' ? 'OpenAI Responses MCP 格式' : '标准 mcpServers 格式'} · 尚未保存或测试</p></div></div>
        <div className="import-preview-list">{preview.connections.map((connection, index) => {
          const tools = Array.isArray(connection.allowed_tools) ? connection.allowed_tools : connection.allowed_tools?.tool_names;
          return <article className="import-preview-card" key={`${index}-${connection.name}`}><div className="import-preview-title"><Link2 size={17}/><h3>{connection.name}</h3><span className="badge">HTTP MCP</span></div><p className="import-url">{connection.url}</p><dl><div><dt>认证凭据</dt><dd>{connection.auth_type === 'bearer' ? connection.credential_present ? 'Bearer · ••••••••（已提供）' : 'Bearer · 未提供' : '无需认证'}</dd></div><div><dt>源配置工具范围</dt><dd>{tools ? tools.length ? tools.join('、') : '未选择工具' : '未指定具体工具'}{!Array.isArray(connection.allowed_tools) && connection.allowed_tools?.read_only ? ' · 只读提示' : ''}</dd></div><div><dt>源配置审批要求</dt><dd>{connection.require_approval === 'always' ? '每次审批' : connection.require_approval === 'never' ? '不要求审批（不会自动应用）' : '按工具配置（不会自动应用）'}</dd></div></dl></article>;
        })}</div>
        {preview.warnings.length > 0 && <div className="import-warnings" role="note"><strong>导入说明</strong><ul>{preview.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div>}
        <p className="form-note import-permission-note"><CheckCircle2 size={16}/>只创建业务连接，不自动绑定助手或批准工具。导入后先测试连接，再到助手设置中选择允许的工具及审批方式。</p>
      </>}
    </div>
    <footer className="modal-footer"><Button variant="secondary" disabled={busy} onClick={preview ? () => { setPreview(null); setError(''); } : close}>{preview ? <><ArrowLeft size={15}/>返回修改</> : '取消'}</Button>{preview ? <Button busy={busy} onClick={() => void commit()}><Upload size={15}/>确认导入 {preview.connections.length} 个连接</Button> : <Button busy={busy} disabled={!source.trim()} onClick={() => void inspect()}>预览配置<ArrowRight size={15}/></Button>}</footer>
  </Modal>;
}
