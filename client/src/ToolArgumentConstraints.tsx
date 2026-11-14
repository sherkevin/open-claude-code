import { Button, ErrorNotice, Field } from './components';

export type ArgumentConstraint = Record<string, unknown>;
export type ConstraintDrafts = Record<string, string>;
export const MAX_CONSTRAINT_BYTES = 32_000;
export const constraintDraftKey = (connectionId: string, toolName: string) => JSON.stringify([connectionId, toolName]);
export const constraintText = (value?: ArgumentConstraint) => value === undefined ? '' : JSON.stringify(value, null, 2);

export function parseArgumentConstraint(text: string): ArgumentConstraint {
  if (!text.trim()) throw new Error('空白不会删除原限制。请填写 JSON Schema，或点击“移除请求限制”。');
  if (new TextEncoder().encode(text).byteLength > MAX_CONSTRAINT_BYTES) throw new Error('单个工具的请求限制不能超过 32,000 字节。');
  let value: unknown;
  try { value = JSON.parse(text); } catch { throw new Error('JSON 格式不完整，请修正后再应用。'); }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('请求限制必须是 JSON 对象。');
  if ((value as ArgumentConstraint).type !== 'object') throw new Error('请求限制的根 type 必须为 object。');
  return value as ArgumentConstraint;
}

export function updateConstraintDraft(drafts: ConstraintDrafts, key: string, text: string, applied?: ArgumentConstraint): ConstraintDrafts {
  const next = { ...drafts };
  if (text === constraintText(applied)) delete next[key];
  else next[key] = text;
  return next;
}

export function assertNoPendingConstraintDrafts(drafts: ConstraintDrafts): void {
  if (Object.keys(drafts).length) throw new Error('还有未应用的请求限制，请逐项应用到草稿或撤销编辑，再保存助手。');
}

export default function ToolArgumentConstraints({ toolName, inputSchema, value, draftText, disabled, onDraftChange, onApply, onDiscard, onRemove }: {
  toolName: string;
  inputSchema: Record<string, unknown>;
  value?: ArgumentConstraint;
  draftText?: string;
  disabled: boolean;
  onDraftChange: (text: string) => void;
  onApply: (constraint: ArgumentConstraint) => void;
  onDiscard: () => void;
  onRemove: () => void;
}) {
  const pending = draftText !== undefined;
  const text = draftText ?? constraintText(value);
  let parsed: ArgumentConstraint | null = null;
  let error = '';
  if (pending) {
    try { parsed = parseArgumentConstraint(text); } catch (failure) { error = (failure as Error).message; }
  }
  return <details className="resource-advanced" aria-label={`${toolName} 请求限制`}>
    <summary>请求限制（高级） · {pending ? '有未应用编辑' : value ? '已配置' : '未配置'}</summary>
    <p className="section-help">请求必须同时符合此限制和工具原有参数定义。这里只拒绝越界请求，不改参数、不填默认值，也不保证远端返回的数据范围。</p>
    <details className="tool-detail"><summary>查看工具实际参数定义</summary><pre>{JSON.stringify(inputSchema, null, 2)}</pre></details>
    <Field label={`${toolName} 请求限制 JSON`} hint="填写根 type 为 object 的 JSON Schema，最多 32,000 字节。仅可限制工具实际声明的参数；完整规则由服务端在保存时校验。">
      <textarea rows={7} value={text} disabled={disabled} spellCheck={false} aria-invalid={pending && !!error} onChange={event => { if (!disabled) onDraftChange(event.target.value); }} placeholder="按上方工具实际参数定义填写 JSON Schema"/>
    </Field>
    <ErrorNotice message={error}/>
    {pending && <p className="section-help" role="status">此处编辑尚未应用，暂不能保存助手。应用到草稿后仍需点击“保存助手”。</p>}
    <div className="card-toolbar">
      <Button variant="secondary" disabled={disabled || !pending || !parsed} onClick={() => { if (!disabled && parsed) onApply(parsed); }}>应用到草稿</Button>
      <Button variant="ghost" disabled={disabled || !pending} onClick={() => { if (!disabled) onDiscard(); }}>撤销编辑</Button>
      {value !== undefined && <Button variant="ghost" disabled={disabled} onClick={() => { if (!disabled) onRemove(); }}>移除请求限制</Button>}
    </div>
    {value !== undefined && <p className="section-help">移除仅清除此工具的附加请求限制，保存助手后生效；原有权限和工具参数校验继续保留。</p>}
  </details>;
}
