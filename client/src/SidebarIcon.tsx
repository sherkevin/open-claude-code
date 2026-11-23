import './sidebar-icon.css';

const panelFrame = 'M2 4h20v16H2zm7 2.5v11h10.5v-11z';

const artwork = {
  home: {
    ink: 'M1 11.5 12 2l11 9.5-1.8 2.1-1.7-1.5V22h-15v-9.9l-1.7 1.5zM7 10v9.5h10V10l-5-4.3z',
    accent: 'M10 14h4v5.5h-4z',
  },
  sessions: {
    ink: 'M2 4h8l2.5 3H22v14H2zm2.5 5.5v9h15v-9z',
    accent: 'M7 13h6v2.5H7z',
  },
  assistants: {
    ink: 'M3 6h18v14H3zm2.5 2.5v9h13v-9zM7 11.5h3v3H7zm7 0h3v3h-3z',
    accent: 'M10.5 2h3v4h-3z',
  },
  businessData: {
    ink: 'M3 2h18v5H3zm0 7.5h18v5H3zm0 7.5h18v5H3zm2.5 1.75v1.5H11v-1.5zM5.5 4v1H11V4z',
    accent: 'M15.5 10.75H19v2.5h-3.5z',
  },
  knowledge: {
    ink: 'M2 3h7l3 2 3-2h7v16h-7l-3 2-3-2H2zm2.5 2.5v11H9l1.75 1.2V7.2L8.25 5.5zm10.75 0-2 1.7v10.5L15 16.5h4.5v-11z',
    accent: 'M15.5 6.5H18V13l-1.25-1-1.25 1z',
  },
  mcp: {
    ink: 'M7 2h2.5v5h5V2H17v5h3v5l-4.75 4.5h-2V22h-2.5v-5.5h-2L4 12V7h3zm-.5 7.5v1.45L9.75 14h4.5l3.25-3.05V9.5z',
    accent: 'M9.5 7h5v2.5h-5z',
  },
  capabilities: {
    ink: 'M2 5h4V2.5h4V5h12v2.5H10V10H6V7.5H2zM2 16.5h20V19H2z',
    accent: 'M14 14h4v7.5h-4z',
  },
  skills: {
    ink: 'M3 3h8v8H3zm2.5 2.5v3h3v-3zM3 13h8v8H3zm2.5 2.5v3h3v-3zM13 13h8v8h-8zm2.5 2.5v3h3v-3z',
    accent: 'M14 3h7v7h-7z',
  },
  usage: {
    ink: 'M1.5 2.5H4v17h18.5V22h-21zm5 10h3.5V17H6.5zm5.5-5h3.5V17H12zm5.5-5H21V17h-3.5z',
    accent: 'M17.5 2.5H21v4h-3.5z',
  },
  benchmark: {
    ink: 'M2 2h20v20H2zm2.5 2.5v15h15v-15zM7 13h2.5v4H7zm3.75-2h2.5v6h-2.5zm3.75-2h2.5v8h-2.5z',
    accent: 'M6 7h12v2H6z',
  },
  approvals: {
    ink: 'M9 2h6l2 3v3l-2 3v2h4l2 2v2.5H3V15l2-2h4v-2L7 8V5z',
    accent: 'M3 19.5h18V22H3z',
  },
  administration: {
    ink: 'M7 3h10v5h5v13H2V8h5zm2.5 2.5V8h5V5.5zm-5 5v8h15v-8z',
    accent: 'M10 12h4v3h-4z',
  },
  activity: {
    ink: 'M4 2h11l5 5v15H4zm2.5 2.5v15h11v-11h-4V4.5zM9 11h6v2H9zm0 4h6v2H9z',
    accent: 'M13.5 2v6.5H20z',
  },
  policy: {
    ink: 'M3 4h6l3-2 3 2h6v10l-3 4-6 4-6-4-3-4zm2.5 2.5v6.65l2.3 3L12 19l4.2-2.85 2.3-3V6.5h-4.25L12 5l-2.25 1.5z',
    accent: 'm7.75 11 1.65-1.65 2.1 2.1 3.3-3.3 1.65 1.65-4.95 4.95z',
  },
  users: {
    ink: 'M4 3h7v7H4zm0 9h7l3 3v6H1v-6zm10 0h5l4 4v5h-6v-7z',
    accent: 'M14 4h6v6h-6z',
  },
  collapse: {
    ink: panelFrame,
    accent: 'M18 10.75h-3.75l1.1-1.1-1.6-1.6L9.8 12l3.95 3.95 1.6-1.6-1.1-1.1H18z',
  },
  expand: {
    ink: panelFrame,
    accent: 'M10.5 10.75h3.75l-1.1-1.1 1.6-1.6L18.7 12l-3.95 3.95-1.6-1.6 1.1-1.1H10.5z',
  },
  menu: {
    ink: 'M3 5h18v2.5H3zm0 5.75h18v2.5H3zm0 5.75h18V19H3z',
    accent: 'M15.5 16.5H21V19h-5.5z',
  },
  close: {
    ink: 'M4 6.1 6.1 4 12 9.9 17.9 4 20 6.1 14.1 12l3.25 3.25-2.1 2.1L12 14.1 6.1 20 4 17.9 9.9 12z',
    accent: 'm15.25 17.35 2.1-2.1L20 17.9 17.9 20z',
  },
  newConversation: {
    ink: 'M2 3h20v15H10l-5.5 4v-4H2zm2.5 2.5v10H7v1.6l2.2-1.6h10.3v-10z',
    accent: 'M10.75 6.5h2.5v2.75H16v2.5h-2.75v2.75h-2.5v-2.75H8v-2.5h2.75z',
  },
  launch: {
    ink: 'M8 4h12v12h-2.5V8.25L5.25 20 3.5 18.25 15.75 6.5H8z',
    accent: 'M16.5 4H20v3.5h-2.5v-1h-1z',
  },
  refresh: {
    ink: 'M7 3h9l4 4-1.8 1.8-3.2-3.3H8L5 8.5v2H2.5v-3zm12 10.5h2.5v3L17 21H8l-4-4 1.8-1.8L9 18.5h7l3-3zM2 13h7v2.5H4.5V20H2z',
    accent: 'M19.5 4H22v7h-7V8.5h4.5z',
  },
  logout: {
    ink: 'M2 3h12v5h-2.5V5.5h-7v13h7V16H14v5H2z',
    accent: 'm17 6 6 6-6 6-1.8-1.8 2.95-2.95H8v-2.5h10.15L15.2 7.8z',
  },
} as const;

export type SidebarIconName = keyof typeof artwork;

export function SidebarIcon({ name, size = 18 }: { name: SidebarIconName; size?: number }) {
  const { ink, accent } = artwork[name];
  return (
    <svg className="kaiwu-sidebar-icon" width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path fillRule="evenodd" d={ink}/>
      <path className="kaiwu-sidebar-icon-accent" d={accent}/>
    </svg>
  );
}
