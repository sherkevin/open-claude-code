import { Check, ChevronDown, LoaderCircle, RotateCcw, Sparkles, Zap } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import type { ModelOption, ModelSelection } from './types';
import { modelSwitchRequiresNewConversation } from './models';
import './model-picker.css';

export function reasoningLevelLabel(level?:{id:string;label:string}) {
  if(!level)return '';
  const labels:Record<string,string>={light:'轻量',low:'低',medium:'中等',high:'高',max:'最高',default:'默认'};
  return level.label.toLowerCase()===level.id.toLowerCase() ? labels[level.id.toLowerCase()] || level.label : level.label;
}

export default function ModelSelector({ models, value, disabled = false, disabledReason, pending = false, loading = false, hasHistory = false, automatic = false, onChange, onNewConversation, onRecommend, recommendBusy = false }: {
  models: ModelOption[]; value: ModelSelection; disabled?: boolean; disabledReason?: string; pending?: boolean; loading?: boolean; hasHistory?: boolean; automatic?: boolean; onChange: (value: ModelSelection) => void; onNewConversation?: (value: ModelSelection) => void; onRecommend?: () => void; recommendBusy?: boolean;
}) {
  const model = models.find(option => option.id === value.model_id);
  const locked = disabled || pending || loading;
  const menuId = useId(), switchNoteId = useId(), reasoningId = useId();
  const trigger = useRef<HTMLButtonElement>(null), menu = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const levels = model?.reasoning_levels || [];
  const selectedIndex = Math.max(0, levels.findIndex(level => level.id === value.reasoning_level));
  const [draftIndex, setDraftIndex] = useState(selectedIndex);
  const draft = useRef(selectedIndex), submitted = useRef<string|null>(null);
  const catalogKey = levels.map(level => level.id).join('\0');
  const requiresNew = (id: string) => modelSwitchRequiresNewConversation(models, value.model_id, id, hasHistory);
  const restrictedSwitch = models.some(option => option.available && requiresNew(option.id));
  const level = levels[draftIndex] || levels[selectedIndex];

  useEffect(() => {
    draft.current = selectedIndex; setDraftIndex(selectedIndex); submitted.current = null;
    if (locked) setOpen(false);
  }, [value.model_id, value.reasoning_level, catalogKey, selectedIndex, locked]);
  useEffect(() => {
    if (!open) return;
    const current = menu.current?.querySelector<HTMLButtonElement>('[role="option"][aria-selected="true"]:not(:disabled)');
    (current || menu.current?.querySelector<HTMLButtonElement>('[role="option"]:not(:disabled)'))?.focus();
    const outsideChoice = (target:EventTarget|null) => !trigger.current?.contains(target as Node) && !menu.current?.contains(target as Node);
    const outside = (event: PointerEvent) => { if (outsideChoice(event.target)) setOpen(false); };
    const focusOutside = (event: FocusEvent) => { if (outsideChoice(event.target)) setOpen(false); };
    document.addEventListener('pointerdown', outside); document.addEventListener('focusin', focusOutside);
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('focusin', focusOutside); };
  }, [open]);

  function preview(index: number) {
    draft.current = Math.max(0, Math.min(levels.length - 1, index)); setDraftIndex(draft.current);
  }
  function cancelPreview() { draft.current = selectedIndex; setDraftIndex(selectedIndex); }
  function commit(index = draft.current) {
    const next = levels[index];
    if (locked || !model?.available || !next || (!automatic && next.id === value.reasoning_level)) return;
    const key = `${model.id}\0${next.id}`;
    if (submitted.current === key) return;
    submitted.current = key;
    onChange({ model_id: model.id, reasoning_level: next.id });
  }
  function choose(next: ModelOption) {
    if (locked || !next.available || (requiresNew(next.id) && !onNewConversation)) return;
    setOpen(false); trigger.current?.focus();
    const selection = { model_id: next.id, reasoning_level: next.id === value.model_id ? value.reasoning_level : next.default_reasoning_level };
    if (requiresNew(next.id)) onNewConversation?.(selection);
    else if (automatic || next.id !== value.model_id) onChange(selection);
  }
  return <div className="model-controls fw-model-picker" aria-label="模型设置" aria-busy={pending || loading}>
    <div className="fw-model-choice">
      <span className="fw-model-caption">模型</span>
      <button ref={trigger} type="button" className="fw-model-trigger" aria-label="选择模型" aria-haspopup="listbox" aria-expanded={open} aria-controls={menuId} aria-describedby={restrictedSwitch ? switchNoteId : undefined} disabled={locked || !models.length} onClick={() => setOpen(previous => !previous)} onKeyDown={event => {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); if (!locked && models.length) setOpen(true); }
      }}><span>{model?.label || (loading ? '正在加载…' : '请选择模型')}</span><ChevronDown size={16}/></button>
      <div ref={menu} id={menuId} className="fw-model-menu" role="listbox" aria-label="可用模型" hidden={!open} onKeyDown={event => {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus(); return; }
        if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const options = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>('[role="option"]:not(:disabled)') || []);
        if (!options.length) return;
        const current = options.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 : (current + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
        options[next]?.focus();
      }}>{models.map(option => {
        const reason = !option.available ? option.unavailable_reason || '暂不可用' : requiresNew(option.id) ? `${onNewConversation ? '新建会话使用' : '需新对话'} · 当前对话和记忆保留` : '';
        return <button key={option.id} type="button" role="option" aria-selected={option.id === value.model_id} tabIndex={option.id === value.model_id?0:-1} disabled={locked || !option.available || (requiresNew(option.id) && !onNewConversation)} className="fw-model-option" onClick={() => choose(option)}><span><strong>{option.label}</strong>{reason && <small>{reason}</small>}</span>{option.id === value.model_id && <Check size={17} aria-hidden="true"/>}</button>;
      })}</div>
    </div>
    {model && <div className="fw-reasoning-control">
      <div className="fw-reasoning-heading"><span id={reasoningId}><Zap size={15}/>推理强度</span><strong aria-live="polite">{reasoningLevelLabel(level) || '未提供档位'}</strong>{levels.length > 1 && <button type="button" className="fw-reasoning-reset" aria-label="恢复默认推理级别" title="恢复默认推理级别" disabled={locked || !model.available || value.reasoning_level === model.default_reasoning_level} onClick={() => { const index = levels.findIndex(item => item.id === model.default_reasoning_level); if (index >= 0) { preview(index); commit(index); } }}><RotateCcw size={16}/></button>}</div>
      {levels.length > 1 ? <><div className="fw-reasoning-track"><input type="range" aria-label="推理级别" aria-labelledby={reasoningId} aria-valuetext={reasoningLevelLabel(level)} min={0} max={levels.length - 1} step={1} value={draftIndex} disabled={locked || !model.available} onChange={event => preview(Number(event.target.value))} onPointerUp={() => commit()} onPointerCancel={cancelPreview} onBlur={() => commit()} onKeyDown={event => {
        if (event.key === 'Escape') { cancelPreview(); return; }
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); commit(); }
      }} onKeyUp={event => { if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) commit(); }}/></div><div className="fw-reasoning-stops" aria-hidden="true">{levels.map((item, index) => <span key={item.id} className={index === draftIndex ? 'is-current' : ''}>{reasoningLevelLabel(item)}</span>)}</div></> : <p className="fw-model-fixed-level">{level ? '此模型使用固定推理级别。' : '此模型未提供可调整的推理级别。'}</p>}
    </div>}
    {onRecommend && <button type="button" className="button ghost model-recommend fw-model-recommend" aria-label="为当前任务推荐模型" disabled={locked || recommendBusy || !models.some(option => option.available)} onClick={onRecommend}><Sparkles size={14}/>{recommendBusy ? '正在分析…' : '为当前任务推荐模型'}</button>}
    {pending && <span className="model-saving" role="status"><LoaderCircle size={14} className="spin"/>正在保存…</span>}
    {disabled && disabledReason && <p className="model-switch-note" role="status">{disabledReason}</p>}
    {model && !model.available && <p className="model-unavailable" role="status">{model.unavailable_reason || '此模型暂不可用，请选择其他模型。'}</p>}
    {automatic && <p className="model-switch-note">当前自动选择，选择模型后改为手动固定。</p>}
    {restrictedSwitch && <p id={switchNoteId} className="model-switch-note">{onNewConversation ? '点击“新建会话使用”的模型即可继续选择，当前对话和记忆保留。' : '请新建对话使用标记“需新对话”的模型，当前对话和记忆保留。'}</p>}
  </div>;
}
