import { LoaderCircle, Sparkles } from 'lucide-react';
import { useId } from 'react';
import type { ModelOption, ModelSelection } from './types';
import { modelSwitchRequiresNewConversation } from './models';

export default function ModelSelector({ models, value, disabled = false, pending = false, loading = false, hasHistory = false, onChange, onRecommend, recommendBusy = false }: {
  models: ModelOption[]; value: ModelSelection; disabled?: boolean; pending?: boolean; loading?: boolean; hasHistory?: boolean; onChange: (value: ModelSelection) => void; onRecommend?: () => void; recommendBusy?: boolean;
}) {
  const model = models.find(option => option.id === value.model_id);
  const locked = disabled || pending || loading;
  const switchNoteId = useId();
  const requiresNew = (id: string) => modelSwitchRequiresNewConversation(models, value.model_id, id, hasHistory);
  const restrictedSwitch = models.some(option => option.available && requiresNew(option.id));
  return <div className="model-controls" aria-label="模型设置" aria-busy={pending || loading}>
    <div className="model-selectors">
      <label className="model-select-field"><span>模型</span><select aria-label="模型" aria-describedby={restrictedSwitch ? switchNoteId : undefined} value={model?.id || ''} disabled={locked || !models.length} onChange={event => {
        const next = models.find(option => option.id === event.target.value);
        if (next?.available && !requiresNew(next.id)) onChange({ model_id: next.id, reasoning_level: next.default_reasoning_level });
      }}>{!model && <option value="">{loading ? '正在加载…' : '请选择模型'}</option>}{models.map(option => <option key={option.id} value={option.id} disabled={!option.available || requiresNew(option.id)}>{option.label}{!option.available ? ` · 暂不可用${option.unavailable_reason ? `（${option.unavailable_reason}）` : ''}` : requiresNew(option.id) ? ' · 需新对话' : ''}</option>)}</select></label>
      {model && <label className="model-select-field reasoning-select-field"><span>推理级别</span><select aria-label="推理级别" value={value.reasoning_level} disabled={locked || !model.available || model.reasoning_levels.length <= 1} onChange={event => onChange({ model_id: model.id, reasoning_level: event.target.value })}>{model.reasoning_levels.map(level => <option key={level.id} value={level.id}>{level.label}</option>)}</select></label>}
      {onRecommend && <button type="button" className="button ghost model-recommend" aria-label="为当前任务推荐模型" disabled={locked || recommendBusy || !models.some(option => option.available)} onClick={onRecommend}><Sparkles size={14}/>{recommendBusy ? '正在分析…' : '为当前任务推荐模型'}</button>}{pending && <span className="model-saving" role="status"><LoaderCircle size={14} className="spin"/>正在保存…</span>}
    </div>
    {model && !model.available && <p className="model-unavailable" role="status">{model.unavailable_reason || '此模型暂不可用，请选择其他模型。'}</p>}
    {restrictedSwitch && <p id={switchNoteId} className="model-switch-note">请新建对话使用标记“需新对话”的模型，当前对话和记忆保留。</p>}
  </div>;
}
