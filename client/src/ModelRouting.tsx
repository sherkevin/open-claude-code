import { useEffect, useRef, type ReactNode } from 'react';
import { Check, ChevronDown, LoaderCircle, SlidersHorizontal } from 'lucide-react';
import type { ModelOption, ModelSelection, Run } from './types';
import { publicModelLabel } from './models';
import { reasoningLevelLabel } from './ModelSelector';
import './model-picker.css';

export function ModelRoutingControl({ automatic, disabled = false, confirmation, onChange }: {
  automatic: boolean; disabled?: boolean; confirmation?: string; onChange: (automatic: boolean) => void;
}) {
  return <div className="model-routing-control fw-model-routing">
    <div className="fw-routing-row">
      <span>选模方式</span>
      <button type="button" role="switch" aria-label="自动选择模型" aria-checked={automatic} disabled={disabled} onClick={() => onChange(!automatic)}>
        <span className="fw-routing-track" aria-hidden="true"/>
        <span aria-hidden="true">{automatic ? '自动选择' : '手动固定'}</span>
      </button>
      <span className="fw-routing-confirmation" role="status" aria-label={confirmation}>
        {confirmation && <Check size={15} aria-hidden="true"/>}
      </span>
    </div>
    <p>{automatic ? '根据当前任务选择模型，实际使用的模型显示在会话顶部。' : '使用你选择的模型和推理级别，不自动切换。'}</p>
  </div>;
}

export function ModelSettingsDisclosure({ automatic, selection, models, loading = false, pending = false, children }: {
  automatic: boolean; selection: ModelSelection; models: ModelOption[]; loading?: boolean; pending?: boolean; children: ReactNode;
}) {
  const disclosure = useRef<HTMLDetailsElement>(null);
  const positionPanel = () => {
    const node = disclosure.current, panel = node?.querySelector<HTMLElement>('.fw-model-panel');
    if (!node?.open || !panel) return;
    // Mobile innerWidth can grow to include this very panel's overflow. Use
    // the layout/visual viewport so measuring an open panel cannot move its
    // own right boundary farther out on the next layout pass.
    const layoutWidth = document.documentElement.clientWidth || window.innerWidth;
    const viewport = window.visualViewport;
    const viewportWidth = Math.min(layoutWidth, viewport?.width || layoutWidth);
    const viewportLeft = viewport?.offsetLeft || 0;
    panel.style.maxWidth = `${Math.max(0, viewportWidth - 32)}px`;
    const left = node.getBoundingClientRect().left, width = panel.offsetWidth;
    const target = Math.max(viewportLeft + 16, Math.min(left, viewportLeft + viewportWidth - width - 16));
    panel.style.setProperty('--fw-panel-left', `${target - left}px`);
  };
  useEffect(() => {
    const outside = (event: PointerEvent) => { const node = disclosure.current; if (node?.open && !node.contains(event.target as Node)) node.open = false; };
    const focusOutside = (event: FocusEvent) => { const node = disclosure.current; if (node?.open && !node.contains(event.target as Node)) node.open = false; };
    const escape = (event: KeyboardEvent) => {
      const node = disclosure.current;
      if (event.key !== 'Escape' || event.defaultPrevented || !node?.open || !node.contains(document.activeElement)) return;
      event.preventDefault(); node.open = false; node.querySelector<HTMLElement>('summary')?.focus();
    };
    const viewport = window.visualViewport;
    document.addEventListener('pointerdown', outside); document.addEventListener('focusin', focusOutside); document.addEventListener('keydown', escape); window.addEventListener('resize', positionPanel); viewport?.addEventListener('resize', positionPanel); viewport?.addEventListener('scroll', positionPanel);
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('focusin', focusOutside); document.removeEventListener('keydown', escape); window.removeEventListener('resize', positionPanel); viewport?.removeEventListener('resize', positionPanel); viewport?.removeEventListener('scroll', positionPanel); };
  }, []);
  const model = models.find(item => item.id === selection.model_id);
  const level = reasoningLevelLabel(model?.reasoning_levels.find(item => item.id === selection.reasoning_level));
  const label = loading ? '正在读取模型设置…' : automatic ? '自动选择' : `${publicModelLabel(models, selection.model_id)}${level ? ` · ${level}` : ''}`;
  return <details ref={disclosure} className="chat-model-settings fw-model-settings" data-automatic={automatic || undefined} aria-busy={pending} onToggle={positionPanel}>
    <summary className="fw-model-pill" aria-label={`模型设置：${label}`}>
      <SlidersHorizontal size={14}/><span className="model-settings-current">{label}</span>
      <span className="model-settings-action">{pending ? <LoaderCircle size={14} className="spin" aria-label="正在保存模型设置"/> : <ChevronDown size={14}/>}</span>
    </summary>
    <div className="model-settings-body fw-model-panel" role="group" aria-label="模型与推理设置">{children}</div>
  </details>;
}

export function RunModelIdentity({ run, models }: { run: Run; models: ModelOption[] }) {
  if (run.execution_kind === 'product_metadata' || run.model_call_count === 0) return <span className="model-run-identity">本次无模型调用</span>;
  const model = models.find(item => item.id === run.model_id);
  const level = reasoningLevelLabel(model?.reasoning_levels.find(item => item.id === run.reasoning_level));
  return <span className="model-run-identity"><small>本次任务模型</small><strong>{run.model_id ? publicModelLabel(models, run.model_id) : '模型信息待确认'}{level && ` · ${level}`}</strong></span>;
}
