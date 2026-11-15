import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from 'react';

export const SIDEBAR_DEFAULT_WIDTH = 244;
export const SIDEBAR_MIN_WIDTH = 220;
export const SIDEBAR_MAX_WIDTH = 360;
export const SIDEBAR_STORAGE_KEY = 'kaiwu.sidebar.v1';
type Preference = { width: number; collapsed: boolean };
export const clampSidebarWidth = (value: number) => Number.isFinite(value) ? Math.max(SIDEBAR_MIN_WIDTH, Math.min(SIDEBAR_MAX_WIDTH, Math.round(value))) : SIDEBAR_DEFAULT_WIDTH;
export function parseSidebarPreference(raw: string | null): Preference {
  try { const value = JSON.parse(raw || 'null'); return { width: typeof value?.width === 'number' ? clampSidebarWidth(value.width) : SIDEBAR_DEFAULT_WIDTH, collapsed: value?.collapsed === true }; }
  catch { return { width: SIDEBAR_DEFAULT_WIDTH, collapsed: false }; }
}
export function sidebarWidthForKey(width: number, key: string, shift = false): number | null {
  if (key === 'Home') return SIDEBAR_MIN_WIDTH;
  if (key === 'End') return SIDEBAR_MAX_WIDTH;
  if (key === 'ArrowLeft' || key === 'ArrowRight') return clampSidebarWidth(width + (key === 'ArrowLeft' ? -1 : 1) * (shift ? 32 : 16));
  return null;
}

/** Persist only layout preference; dragging updates one CSS variable per frame. */
export function useSidebarLayout() {
  const [preference, setPreference] = useState<Preference>(() => {
    try { return parseSidebarPreference(window.localStorage.getItem(SIDEBAR_STORAGE_KEY)); }
    catch { return parseSidebarPreference(null); }
  });
  const [resizing, setResizing] = useState(false);
  const shellRef = useRef<HTMLDivElement>(null);
  const drag = useRef<{ id: number; x: number; start: number; width: number; handle: HTMLDivElement } | null>(null);
  const frame = useRef<number | null>(null);
  useEffect(() => { try { window.localStorage.setItem(SIDEBAR_STORAGE_KEY, JSON.stringify(preference)); } catch { /* Layout still works if storage is unavailable. */ } }, [preference]);
  useEffect(() => () => { if (frame.current !== null) cancelAnimationFrame(frame.current); }, []);
  const finish = (event: PointerEvent<HTMLDivElement>) => {
    const current = drag.current; if (!current || current.id !== event.pointerId) return;
    if (frame.current !== null) { cancelAnimationFrame(frame.current); frame.current = null; }
    drag.current = null; setResizing(false);
    setPreference(previous => ({ ...previous, width: current.width }));
    shellRef.current?.style.setProperty('--sidebar-width', `${current.width}px`);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };
  return {
    shellRef, collapsed: preference.collapsed, resizing,
    style: { '--sidebar-width': `${preference.width}px` } as CSSProperties,
    toggle: () => setPreference(previous => ({ ...previous, collapsed: !previous.collapsed })),
    separator: {
      role: 'separator', tabIndex: 0, 'aria-orientation': 'vertical' as const,
      'aria-label': '调整侧栏宽度', 'aria-controls': 'workspace-sidebar',
      'aria-valuemin': SIDEBAR_MIN_WIDTH, 'aria-valuemax': SIDEBAR_MAX_WIDTH, 'aria-valuenow': preference.width,
      title: '拖动调整侧栏宽度；方向键微调；双击恢复默认',
      onPointerDown: (event: PointerEvent<HTMLDivElement>) => {
        if (event.button !== 0 || !window.matchMedia('(min-width:761px)').matches || preference.collapsed) return;
        event.preventDefault(); event.currentTarget.focus(); event.currentTarget.setPointerCapture(event.pointerId);
        drag.current = { id: event.pointerId, x: event.clientX, start: preference.width, width: preference.width, handle: event.currentTarget }; setResizing(true);
      },
      onPointerMove: (event: PointerEvent<HTMLDivElement>) => {
        const current = drag.current; if (!current || event.pointerId !== current.id) return;
        current.width = clampSidebarWidth(current.start + event.clientX - current.x);
        if (frame.current !== null) return;
        frame.current = requestAnimationFrame(() => { frame.current = null; if (!drag.current) return; shellRef.current?.style.setProperty('--sidebar-width', `${drag.current.width}px`); drag.current.handle.setAttribute('aria-valuenow', String(drag.current.width)); });
      },
      onPointerUp: finish, onPointerCancel: finish, onLostPointerCapture: finish,
      onDoubleClick: () => setPreference(previous => ({ ...previous, width: SIDEBAR_DEFAULT_WIDTH })),
      onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => { const next = sidebarWidthForKey(preference.width, event.key, event.shiftKey); if (next === null) return; event.preventDefault(); setPreference(previous => ({ ...previous, width: next })); },
    },
  };
}
