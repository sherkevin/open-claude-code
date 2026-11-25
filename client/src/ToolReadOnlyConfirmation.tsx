import { Button, ErrorNotice, Field } from './components';
import type { Connection, MCPTool, ReadOnlyConfirmation } from './types';

export type ReadOnlyDrafts = Record<string, string>;
export const remoteReadOnlyLabel = (tool: MCPTool) => tool.annotations?.readOnlyHint === true ? '远端声明：只读（仅提示）' : tool.annotations?.readOnlyHint === false ? '远端声明：非只读（仅提示）' : '远端未声明是否只读';

export function readOnlyConfirmationIssue(connection: Connection, tool: MCPTool): string {
  if (!connection.enabled || connection.status !== 'connected') return '连接尚未就绪，请先启用并测试连接。';
  if (tool.annotations?.readOnlyHint === false || tool.annotations?.destructiveHint === true) return '远端明确声明非只读或有破坏性操作，不能确认为只读。';
  if (tool.read_only_confirmation_eligible !== true || !tool.definition_digest) return tool.read_only_confirmation_reason || '当前工具定义尚不能确认，请重新测试连接并核查。';
  return '';
}

export function readOnlyConfirmationState(connection: Connection, tool: MCPTool, value?: ReadOnlyConfirmation): 'unconfirmed' | 'current' | 'stale' {
  if (!value) return 'unconfirmed';
  return !readOnlyConfirmationIssue(connection, tool) && value.connection_revision === connection.revision && value.definition_digest === tool.definition_digest && typeof value.basis === 'string' && !!value.basis.trim() ? 'current' : 'stale';
}

export function makeReadOnlyConfirmation(connection: Connection, tool: MCPTool, basis: string): ReadOnlyConfirmation {
  const issue = readOnlyConfirmationIssue(connection, tool);
  if (issue) throw new Error(issue);
  const text = basis.trim();
  if (!text || Array.from(text).length > 500) throw new Error('请填写 1–500 字的核查依据。');
  return { connection_revision: connection.revision, definition_digest: tool.definition_digest!, basis: text };
}

export function assertNoPendingReadOnlyDrafts(drafts: ReadOnlyDrafts): void {
  if (Object.keys(drafts).length) throw new Error('还有未应用的只读确认依据，请确认到草稿或撤销编辑，再保存助手。');
}

export function RemoteToolDeclaration({ tool }: { tool: MCPTool }) {
  return <div className="tool-readonly-declaration"><p>{remoteReadOnlyLabel(tool)}{tool.annotations?.destructiveHint === true ? '；远端声明含破坏性操作' : ''}</p><small>远端提示不能作为只读授权。旧工作流免确认不代表工具只读；会话使用连接页的只读核验。</small>{tool.definition_digest && <small className="tool-definition-digest" title={tool.definition_digest}>当前定义指纹：{tool.definition_digest}</small>}</div>;
}

export default function ToolReadOnlyConfirmation({ connection, tool, value, savedValue, draftText, disabled, onDraftChange, onApply, onDiscard, onRemove }: {
  connection: Connection; tool: MCPTool; value?: ReadOnlyConfirmation; savedValue?: ReadOnlyConfirmation; draftText?: string; disabled: boolean;
  onDraftChange: (text: string) => void; onApply: (confirmation: ReadOnlyConfirmation) => void; onDiscard: () => void; onRemove: () => void;
}) {
  const state = readOnlyConfirmationState(connection, tool, value);
  const pending = draftText !== undefined;
  const changed = JSON.stringify(value) !== JSON.stringify(savedValue);
  const label = state === 'current' ? (changed ? '当前定义已确认，待保存' : '当前定义已由宿主确认') : state === 'stale' ? '定义或连接状态已变更，需重新核查' : (changed ? '已移除，待保存' : '未确认');
  const issue = readOnlyConfirmationIssue(connection, tool);
  let confirmation: ReadOnlyConfirmation | null = null;
  let basisError = '';
  if (pending && !issue) {
    try { confirmation = makeReadOnlyConfirmation(connection, tool, draftText ?? ''); } catch (error) { basisError = (error as Error).message; }
  }
  return <details className="resource-advanced tool-readonly-confirmation" aria-label={`${tool.name} 工作流只读确认`}>
    <summary>工作流只读确认 · {label}</summary>
    <RemoteToolDeclaration tool={tool}/>
    <p className="section-help">此确认仅用于本助手。确认前须核查整个工具在所有允许参数下均仅读取，且服务端使用只读账号。填写依据不会改变远端权限，也不能替代只读账号或服务端权限控制。</p>
    <p className="section-help">旧有管理员确认在尚未使用连接级核验时仍可供会话自动审批使用。推荐在 MCP 连接页统一核验，不必为每个助手重复确认。执行时服务端会重新核验。</p>
    {value && <p className="tool-confirmation-basis"><strong>{state === 'current' ? '确认依据' : '原确认依据（当前不生效）'}</strong>{value.basis}</p>}
    {issue && <p className="section-help" role="status">{issue}</p>}
    <Field label={`${tool.name} 只读核查依据`} hint="填写实际检查过的接口说明、权限配置或验证记录，1–500 字。不要填写凭据。">
      <textarea rows={3} value={draftText ?? ''} disabled={disabled || !!issue} aria-invalid={!!basisError} onChange={event => { if (!disabled && !issue) onDraftChange(event.target.value); }} placeholder="按实际核查结果填写；不从远端提示自动确认"/>
    </Field>
    <ErrorNotice message={basisError}/>
    {pending && <p className="section-help" role="status">依据尚未应用，暂不能保存助手。确认到草稿后，仍须保存助手才生效。</p>}
    <div className="card-toolbar">
      <Button variant="secondary" disabled={disabled || !confirmation} onClick={() => { if (!disabled && confirmation) onApply(confirmation); }}>确认当前定义到草稿</Button>
      <Button variant="ghost" disabled={disabled || !pending} onClick={() => { if (!disabled) onDiscard(); }}>撤销依据编辑</Button>
      {value && <Button variant="ghost" disabled={disabled} onClick={() => { if (!disabled) onRemove(); }}>移除只读确认</Button>}
    </div>
    {changed && !pending && <p className="section-help">保存助手后生效。工具授权和会话审批档位各自管理。</p>}
  </details>;
}
