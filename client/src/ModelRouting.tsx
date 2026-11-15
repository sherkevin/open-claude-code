import type { ReactNode } from 'react';
import { ChevronDown } from 'lucide-react';
import type { ModelOption, ModelSelection, Run } from './types';
import { publicModelLabel } from './models';

export function ModelRoutingControl({ automatic, disabled = false, onChange }: {
  automatic: boolean; disabled?: boolean; onChange: (automatic: boolean) => void;
}) {
  return <div className="model-routing-control"><label><span>选模方式</span><select aria-label="选模方式" value={automatic ? 'auto' : 'manual'} disabled={disabled} onChange={event => onChange(event.target.value === 'auto')}><option value="auto">自动选择</option><option value="manual">手动固定</option></select></label><p>{automatic ? '根据当前任务选择模型，实际使用的模型显示在会话顶部。' : '使用你选择的模型和推理级别，不自动切换。'}</p></div>;
}

export function ModelSettingsDisclosure({ automatic, selection, models, loading = false, pending = false, children }: {
  automatic: boolean; selection: ModelSelection; models: ModelOption[]; loading?: boolean; pending?: boolean; children: ReactNode;
}) {
  const model = models.find(item => item.id === selection.model_id);
  const level = model?.reasoning_levels.find(item => item.id === selection.reasoning_level)?.label;
  const label = loading ? '正在读取模型设置…' : automatic ? '自动选择' : `手动固定 · ${publicModelLabel(models, selection.model_id)}${level ? ` · ${level}` : ''}`;
  return <details className="chat-model-settings"><summary><span className="model-settings-current">{label}</span><span className="model-settings-action">{pending ? '正在保存…' : '调整模型'}<ChevronDown size={14}/></span></summary><div className="model-settings-body">{children}</div></details>;
}

export function RunModelIdentity({ run, models }: { run: Run; models: ModelOption[] }) {
  if (run.execution_kind === 'product_metadata' || run.model_call_count === 0) return <span className="model-run-identity">本次无模型调用</span>;
  const model = models.find(item => item.id === run.model_id);
  const level = model?.reasoning_levels.find(item => item.id === run.reasoning_level)?.label;
  return <span className="model-run-identity"><small>本次任务模型</small><strong>{run.model_id ? publicModelLabel(models, run.model_id) : '模型信息待确认'}{level && ` · ${level}`}</strong></span>;
}
