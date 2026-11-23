import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { Check, ChevronDown, KeyRound, ListChecks, MousePointerClick } from 'lucide-react';
import { errorText, patch } from './api';
import { Button, ErrorNotice, Modal } from './components';
import type { Conversation, PermissionMode, StreamEvent } from './types';
import './session-permission-mode.css';

const modes = {
  manual: { label: '手动审批', short: '手动', icon: MousePointerClick, description: '需要审批的操作会暂停，由你确认。' },
  auto: { label: '自动审批', short: '自动', icon: ListChecks, description: '自动批准管理员已核验的只读工具。' },
  full: { label: '完全权限', short: '完全权限', icon: KeyRound, description: '可直接写入工作区和生成文件。外部操作仍按审批策略执行。' },
} as const;

export default function SessionPermissionMode({ conversation, disabled }: { conversation: Conversation; disabled: boolean }) {
  const [saved, setSaved] = useState<PermissionMode | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const listId = useId();
  const options = conversation.permission_modes ?? [];
  const mode = saved && options.includes(saved) ? saved : conversation.permission_mode ?? 'manual';
  const current = modes[mode];
  const CurrentIcon = current.icon;
  const locked = disabled || pending;
  const visible = open && !locked && options.length > 1;
  const canChooseFull = options.includes('full');
  const optionKey = options.join(',');

  useEffect(() => { setSaved(null); }, [conversation.permission_mode, conversation.id]);
  useEffect(() => {
    if (disabled || options.length < 2) setOpen(false);
    if (disabled || !canChooseFull) setConfirming(false);
  }, [disabled, options.length, canChooseFull]);

  useLayoutEffect(() => {
    const anchor = trigger.current;
    const panel = menu.current;
    if (!visible || !anchor || !panel) return;
    function position() {
      if (!anchor || !panel) return;
      const { left, top, bottom } = anchor.getBoundingClientRect();
      const x = Math.max(12, Math.min(left, document.documentElement.clientWidth - panel.offsetWidth - 12));
      panel.style.left = `${x - left}px`;
      const above = top - 20;
      const below = window.innerHeight - bottom - 20;
      const opensUp = above >= panel.scrollHeight || above >= below;
      panel.dataset.side = opensUp ? 'top' : 'bottom';
      panel.style.maxHeight = `${Math.max(0, opensUp ? above : below)}px`;
      // Scroll the list itself so its current option stays visible on a short screen.
      const row = panel.querySelector<HTMLElement>('[role="option"]:focus') ?? panel.querySelector<HTMLElement>('[aria-selected="true"]');
      if (row) {
        const bounds = panel.getBoundingClientRect();
        const item = row.getBoundingClientRect();
        if (item.top < bounds.top) panel.scrollTop += item.top - bounds.top - 4;
        else if (item.bottom > bounds.bottom) panel.scrollTop += item.bottom - bounds.bottom + 4;
      }
    }
    position();
    panel.querySelector<HTMLElement>('[aria-selected="true"]')?.focus({ preventScroll: true });
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !panel.contains(event.target) && !anchor.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', outside);
    window.addEventListener('resize', position);
    return () => {
      document.removeEventListener('pointerdown', outside);
      window.removeEventListener('resize', position);
    };
  }, [visible, mode, optionKey]);

  if (options.length < 2) return null;

  const save = async (next: PermissionMode) => {
    if (locked || !options.includes(next)) return;
    setPending(true);
    setError('');
    try {
      const updated = await patch<Conversation>(`/conversations/${conversation.id}`, { permission_mode: next });
      setSaved(updated.permission_mode ?? next);
      setConfirming(false);
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setPending(false);
    }
  };

  function closeMenu() {
    setOpen(false);
    trigger.current?.focus();
  }

  function choose(next: PermissionMode) {
    closeMenu();
    if (next === mode || locked) return;
    if (next === 'full') { setError(''); setConfirming(true); }
    else void save(next);
  }

  function navigate(event: KeyboardEvent<HTMLDivElement>) {
    const rows = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="option"]'));
    const index = rows.indexOf(document.activeElement as HTMLButtonElement);
    switch (event.key) {
      case 'Escape': event.preventDefault(); closeMenu(); return;
      case 'Tab': closeMenu(); return;
      case 'ArrowDown': rows[(index + 1) % rows.length].focus(); break;
      case 'ArrowUp': rows[(index + rows.length - 1) % rows.length].focus(); break;
      case 'Home': rows[0].focus(); break;
      case 'End': rows[rows.length - 1].focus(); break;
      default: return;
    }
    event.preventDefault();
  }

  return <div className="session-permission-mode">
    <button ref={trigger} type="button" className="permission-trigger" data-mode={mode} disabled={disabled} aria-disabled={locked} aria-busy={pending}
      aria-label={`会话权限，${current.label}`} title={`${current.label} · ${current.description}`}
      aria-haspopup="listbox" aria-expanded={visible} aria-controls={visible ? listId : undefined}
      onClick={() => { if (!locked) setOpen(!open); }} onKeyDown={event => {
        if (locked) return;
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); setOpen(true); }
      }}>
      <CurrentIcon size={16} strokeWidth={1.7} aria-hidden="true"/>
      <span className="permission-name">{current.label}</span><span className="permission-name-short">{current.short}</span>
      <ChevronDown size={12} strokeWidth={1.7} className="permission-chevron" aria-hidden="true"/>
    </button>
    {visible && <div ref={menu} id={listId} className="permission-list" role="listbox" aria-label="会话权限" onKeyDown={navigate}>
      {options.map(value => {
        const { icon: Icon, label, description } = modes[value];
        return <button key={value} type="button" role="option" aria-selected={value === mode} tabIndex={-1}
          className="permission-option" data-mode={value} onClick={() => choose(value)}>
          <Icon className="permission-symbol" size={19} strokeWidth={1.65} aria-hidden="true"/>
          <span className="permission-copy"><strong>{label}</strong><small>{description}</small></span>
          {value === mode && <Check className="permission-mark" size={15} strokeWidth={2} aria-hidden="true"/>}
        </button>;
      })}
    </div>}
    {!confirming && <ErrorNotice message={error} onDismiss={() => setError('')}/>}
    {confirming && <Modal title="开启完全权限" onClose={() => { if (!pending) setConfirming(false); }}>
      <div className="modal-body"><p>{modes.full.description}</p><ErrorNotice message={error}/></div>
      <footer className="modal-footer"><Button type="button" variant="secondary" disabled={pending} onClick={() => setConfirming(false)}>取消</Button><Button type="button" busy={pending} disabled={disabled} onClick={() => void save('full')}>开启完全权限</Button></footer>
    </Modal>}
  </div>;
}

export function MCPAvailabilityNotice({ events }: { events: StreamEvent[] }) {
  const connections = [...new Map(events.filter(event => event.type === 'mcp.unavailable')
    .map(event => [String(event.data.connection_id), event.data])).values()];
  if (!connections.length) return null;
  return <div className="mcp-availability-notice" role="status">
    {connections.map(connection => <p key={String(connection.connection_id)}><strong>{String(connection.name || '业务连接')}暂不可用</strong> · {String(connection.reason || '请查看连接状态')}</p>)}
  </div>;
}
