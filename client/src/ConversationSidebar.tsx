import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { PanelRightClose } from 'lucide-react';
import { bindModalLayer } from './components';
import './conversation-sidebar.css';

const desktopQuery = '(min-width: 1180px)';
const desktop = () => window.matchMedia(desktopQuery).matches;
function watchWidth(notify: () => void) {
  const media = window.matchMedia(desktopQuery);
  media.addEventListener('change', notify);
  return () => media.removeEventListener('change', notify);
}

export function useConversationSidebar() {
  const docked = useSyncExternalStore(watchWidth, desktop, () => false);
  const [desktopOpen, setDesktopOpen] = useState(true);
  const [drawerOpen, setDrawerOpen] = useState(false);
  return { docked, open: docked ? desktopOpen : drawerOpen, setOpen: docked ? setDesktopOpen : setDrawerOpen };
}

export default function ConversationSidebar({ docked, onClose, children }: {
  docked: boolean; onClose: () => void; children: ReactNode;
}) {
  const panel = useRef<HTMLElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!docked && panel.current) return bindModalLayer(panel.current, () => close.current());
  }, [docked]);
  return <>
    {!docked && <div className="conversation-sidebar-backdrop" aria-hidden="true" onClick={onClose}/>}
    <aside id="conversation-sidebar" ref={panel} className={`conversation-sidebar ${docked ? 'docked' : 'drawer'}`} tabIndex={-1}
      role={docked ? 'complementary' : 'dialog'} aria-modal={docked ? undefined : true} aria-label="会话侧栏">
      {!docked && <header className="conversation-sidebar-heading"><h2>会话侧栏</h2>
        <button type="button" className="icon-button" aria-label="关闭会话侧栏" title="关闭会话侧栏" onClick={onClose}><PanelRightClose size={17}/></button>
      </header>}
      <div className="conversation-sidebar-body">{children}</div>
    </aside>
  </>;
}
