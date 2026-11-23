import { useEffect, useState } from 'react';
import { LogOut, Shield } from 'lucide-react';
import { ApiError, api, errorText, post } from '../api';
import { UsersPage } from '../AdminPages';
import { Button, ErrorNotice, Field, Loader } from '../components';
import PromptSafetyPage from '../PromptSafetyPage';
import { SidebarIcon } from '../SidebarIcon';
import type { AppConfig, User } from '../types';
import { UsagePage } from '../UsagePage';
import ActivityPage from './ActivityPage';

const sections = {
  usage: {label: '用量概览', icon: 'usage'},
  activity: {label: '操作日志', icon: 'activity'},
  policy: {label: '用户规范', icon: 'policy'},
  users: {label: '用户管理', icon: 'users'},
} as const;
type Section = keyof typeof sections;

function PolicySection({users}: {users: User[]}) {
  const [selectedUser, setSelectedUser] = useState('');
  return <>
    <div className="ops-policy-scope"><Field label="应用对象">
      <select value={selectedUser} onChange={event => setSelectedUser(event.target.value)}>
        <option value="">全局默认策略</option>
        {users.map(user => <option key={user.id} value={user.id}>{user.display_name}（{user.username}）{user.enabled ? '' : ' · 已停用'}</option>)}
      </select>
    </Field></div>
    <PromptSafetyPage key={selectedUser} userId={selectedUser}/>
  </>;
}

function AdminLogin({onLogin, message}: {onLogin: (user: User) => void; message: string}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  return <form className="ops-login" onSubmit={async event => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try { onLogin(await post<User>('/auth/login', {username, password})); setPassword(''); }
    catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  }}>
    <Shield size={28}/><h1>开物运营后台</h1>
    <p>使用管理员账号登录。</p>
    <ErrorNotice message={error || message}/>
    <Field label="账号" required><input name="username" autoComplete="username" required value={username} disabled={busy} onChange={event => setUsername(event.target.value)}/></Field>
    <Field label="密码" required><input name="password" autoComplete="current-password" type="password" required value={password} disabled={busy} onChange={event => setPassword(event.target.value)}/></Field>
    <Button type="submit" busy={busy}>登录</Button>
  </form>;
}

export default function AdminApp() {
  const [user, setUser] = useState<User | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [view, setView] = useState<Section>('usage');
  const [ready, setReady] = useState(false);
  const [trial, setTrial] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      try {
        const config = await api<AppConfig>('/config', {signal: controller.signal});
        if (config.deployment_mode === 'trial') { setTrial(true); return; }
        setUser(await api<User>('/auth/me', {signal: controller.signal}));
      } catch (cause) {
        if (!controller.signal.aborted && !(cause instanceof ApiError && cause.status === 401)) setError(errorText(cause));
      } finally { if (!controller.signal.aborted) setReady(true); }
    };
    const expired = () => {setUser(null); setUsers([]); setView('usage');};
    void load();
    window.addEventListener('session-expired', expired);
    return () => {controller.abort(); window.removeEventListener('session-expired', expired);};
  }, []);
  useEffect(() => {
    if (user?.role !== 'admin' || (view !== 'policy' && view !== 'activity')) return;
    const controller = new AbortController();
    void api<User[]>('/users', {signal: controller.signal}).then(setUsers)
      .catch(cause => {if (!controller.signal.aborted) setError(errorText(cause));});
    return () => controller.abort();
  }, [view, user]);

  if (!ready) return <div className="ops-entry"><Loader text="正在检查管理员会话…"/></div>;
  if (trial) return <div className="ops-entry"><ErrorNotice message="此部署未开放运营后台。"/></div>;
  if (user?.role !== 'admin') return <div className="ops-entry"><AdminLogin onLogin={value => {setUser(value); setError('');}} message={user ? '此账号没有运营后台权限。' : error}/></div>;
  return <div className="ops-shell">
    <aside className="ops-nav">
      <div className="ops-brand"><Shield size={21}/><span>开物运营后台</span></div>
      <nav aria-label="运营后台导航">
        {(Object.keys(sections) as Section[]).map(key => {
          const {icon, label} = sections[key];
          return <button key={key} type="button" aria-current={view === key ? 'page' : undefined} onClick={() => {setView(key); setError('');}}><SidebarIcon name={icon}/>{label}</button>;
        })}
      </nav>
    </aside>
    <div className="ops-content">
      <header className="ops-account"><span>{user.display_name || user.username}</span><Button variant="ghost" onClick={() => {
        void post('/auth/logout').then(() => {setUser(null); setUsers([]); setView('usage'); setError('');}).catch(cause => setError(errorText(cause)));
      }}><LogOut size={16}/>退出</Button></header>
      <main>
        {error && <div className="ops-error"><ErrorNotice message={error}/></div>}
        {view === 'usage' && <UsagePage admin defaultScope="all"/>}
        {view === 'activity' && <ActivityPage users={users}/>}
        {view === 'policy' && <PolicySection users={users}/>}
        {view === 'users' && <UsersPage currentUser={user}/>}
      </main>
    </div>
  </div>;
}
