import { useRef, useState } from 'react';
import { post, errorText } from './api';
import { Button, ErrorNotice, Field, Modal } from './components';
import type { Connection, MCPTool, ReadOnlyConfirmation } from './types';

export function currentReviewedTools(connection: Connection): string[] {
  if (!connection.enabled || connection.status !== 'connected') return [];
  return connection.tools.filter(tool => {
    const review = connection.read_only_certifications?.[tool.name];
    return tool.session_read_only_review_eligible === true && review && review.connection_revision === connection.revision &&
      review.definition_digest === tool.definition_digest;
  }).map(tool => tool.name);
}

export function connectionReviewPayload(connection: Connection, names: string[], basis: string) {
  const tools: Record<string, ReadOnlyConfirmation> = {};
  const existing = new Set(currentReviewedTools(connection));
  for (const name of names) {
    const tool = connection.tools.find(item => item.name === name);
    if (!tool || tool.session_read_only_review_eligible !== true || !tool.definition_digest) throw new Error('工具无法核验，请刷新连接。');
    const text = existing.has(name) ? connection.read_only_certifications![name].basis : basis.trim();
    if (!text || Array.from(text).length > 500) throw new Error('请填写1–500字的实际核查依据。');
    tools[name] = { connection_revision: connection.revision, definition_digest: tool.definition_digest, basis: text };
  }
  return { expected_connection_revision: connection.revision, expected_review_revision: connection.read_only_review_revision ?? 0, tools };
}

const reviewState = (connection: Connection, tool: MCPTool, current: Set<string>) => current.has(tool.name)
  ? '已核验记录 · 自动模式执行前复核'
  : connection.read_only_certifications?.[tool.name] ? '原核验已失效 · 需重新核验' : '逐次确认';

export default function ConnectionReadOnlyReview({ connection, onClose, onSaved }: {
  connection: Connection; onClose: () => void; onSaved: (connection: Connection) => void;
}) {
  const [names, setNames] = useState(() => currentReviewedTools(connection));
  const [basis, setBasis] = useState('');
  const [attested, setAttested] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const pending = useRef(false);
  const current = new Set(currentReviewedTools(connection));
  const changed = names.length !== current.size || names.some(name => !current.has(name))
    || Object.keys(connection.read_only_certifications || {}).length !== current.size;
  const newTools = names.some(name => !current.has(name));
  const [overrideDefault, setOverrideDefault] = useState(false);
  const ready = (changed || overrideDefault) && (!newTools || !!basis.trim() && attested);
  const save = async () => {
    if (!ready || pending.current) return;
    pending.current = true; setBusy(true); setError('');
    try {
      onSaved(await post<Connection>('/connections/' + encodeURIComponent(connection.id) + '/read-only-review', connectionReviewPayload(connection, names, basis)));
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      pending.current = false; setBusy(false);
    }
  };
  return <Modal title="核验只读工具" description={'「' + connection.name + '」只需核验一次；自动审批模式下，已核验且已授权的只读工具直接执行。'} wide onClose={() => { if (!pending.current) onClose(); }}>
    <div className="modal-body">
      <ErrorNotice message={error}/>
      <p className="section-help">请对照供应方接口契约及权限，确认所选工具在所有允许参数下只读取业务数据。名称、描述和一次成功测试均不能单独证明只读。通用执行、写入及无法确认的工具必须逐次审批。</p>
      <fieldset className="plain-fieldset" disabled={busy}>
        {connection.tools.map(tool => <div className="binding-group" key={tool.name}>
          <label className="checkbox-row"><input type="checkbox" checked={names.includes(tool.name)} disabled={tool.session_read_only_review_eligible !== true}
            onChange={event => setNames(previous => event.target.checked ? [...previous, tool.name] : previous.filter(name => name !== tool.name))}/>
            <span><strong>{tool.name}</strong><small>{tool.session_read_only_review_eligible === true ? reviewState(connection, tool, current) : tool.session_read_only_review_reason || '不能自动审批'}</small></span>
          </label>
          <p className="section-help">{tool.description}</p>
          <details className="resource-advanced"><summary>查看参数定义</summary><pre className="text-preview">{JSON.stringify(tool.inputSchema, null, 2)}</pre></details>
        </div>)}
        {newTools && <>
          <Field label="核验依据" hint="供应方文档、只读权限或已核查的接口契约，不填写密码、Token或业务数据。" required>
            <textarea value={basis} rows={3} maxLength={500} onChange={event => setBasis(event.target.value)} placeholder="填写真实核查记录"/>
          </Field>
          <label className="checkbox-row"><input type="checkbox" checked={attested} onChange={event => setAttested(event.target.checked)}/><span>我已核实所选工具只读且不会改变业务数据，确认用于自动审批。</span></label>
        </>}
      </fieldset>
      <label className="checkbox-row"><input type="checkbox" checked={overrideDefault} disabled={busy} onChange={event => setOverrideDefault(event.target.checked)}/><span>使用本次人工配置覆盖平台默认识别（不勾选任何工具即全部逐次审批）。</span></label>
      <p className="form-note">个人连接仅由其所有者核验，团队连接仅由管理员核验。保存人工配置后，以人工选择为准；连接或工具定义变化后需重新核验。不会扩大助手的授权范围。</p>
    </div>
    <footer className="modal-footer">
      <Button variant="secondary" disabled={busy} onClick={onClose}>取消</Button>
      <Button busy={busy} disabled={!ready} onClick={() => void save()}>保存只读核验</Button>
    </footer>
  </Modal>;
}
