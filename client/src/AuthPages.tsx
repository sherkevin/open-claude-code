import { useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { ArrowRight, BookOpen, ChartNoAxesCombined, Eye, EyeOff, Network, RefreshCw, ShieldCheck } from 'lucide-react';
import { errorText, post } from './api';
import type { AppConfig, User } from './types';
import { Button, ErrorNotice, Loader } from './components';
import Brand from './Brand';

function AuthFrame({ name, children }: { name: string; children: ReactNode }) {
  return <div className="auth-page zhixing-theme">
    <header className="auth-header"><Brand name={name}/><span>业务 · 知识 · 协作</span></header>
    <main className="auth-main">
      <div className="auth-surface">
        <section className="auth-context" aria-labelledby="auth-context-title">
          <span className="auth-kicker">企业业务助手</span>
          <h2 id="auth-context-title">业务与知识，<br/>在同一处协作。</h2>
          <p>连接业务系统，查阅企业资料，<br className="auth-desktop-break"/>整理带来源的分析结果。</p>
          <ul className="auth-capabilities">
            <li><Network size={19}/><div><strong>连接业务系统</strong><span>接入已授权的数据与工具</span></div></li>
            <li><BookOpen size={19}/><div><strong>查阅企业知识</strong><span>查找资料，回看原始依据</span></div></li>
            <li><ChartNoAxesCombined size={19}/><div><strong>整理分析结果</strong><span>查看图表，导出业务数据</span></div></li>
          </ul>
        </section>
        <section className="auth-panel">{children}</section>
      </div>
    </main>
    <footer className="auth-footer"><span>燧石开物 · {name}</span><span>简体中文</span></footer>
  </div>;
}

export function Login({ config, onLogin }: { config: AppConfig; onLogin: (user: User) => void }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [visible, setVisible] = useState(false);
  const [capsLock, setCapsLock] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const submitting = useRef(false);
  return <AuthFrame name={config.brand_name}>
    <form className="auth-form" aria-labelledby="auth-title" aria-busy={busy} onSubmit={async event => {
      event.preventDefault();
      if (submitting.current) return;
      submitting.current = true;
      setBusy(true); setError('');
      try { onLogin(await post<User>('/auth/login', { username, password })); }
      catch (err) { setError(errorText(err)); }
      finally { submitting.current = false; setBusy(false); }
    }}>
      <h1 id="auth-title">登录工作空间</h1>
      <p className="auth-description">使用你的工作账号继续。</p>
      <ErrorNotice message={error}/>
      <div className="auth-field"><label htmlFor="auth-username">账号</label>
        <input id="auth-username" name="username" autoComplete="username" autoCapitalize="none" spellCheck={false} placeholder="输入工作账号" value={username} onChange={e => setUsername(e.target.value)} required autoFocus disabled={busy}/>
      </div>
      <div className="auth-field"><label htmlFor="auth-password">密码</label>
        <div className="auth-password">
          <input id="auth-password" name="password" type={visible ? 'text' : 'password'} autoComplete="current-password" placeholder="输入登录密码" value={password} onChange={e => setPassword(e.target.value)} onKeyUp={e => setCapsLock(e.getModifierState('CapsLock'))} onBlur={() => setCapsLock(false)} aria-describedby={capsLock ? 'auth-caps-lock' : undefined} required disabled={busy}/>
          <button type="button" className="auth-password-toggle" aria-label={visible ? '隐藏密码' : '显示密码'} aria-pressed={visible} onClick={() => setVisible(value => !value)} disabled={busy}>{visible ? <EyeOff size={18}/> : <Eye size={18}/>}</button>
        </div>
        {capsLock && <span id="auth-caps-lock" className="auth-caps-lock" role="status">大写锁定已开启</span>}
      </div>
      <Button type="submit" busy={busy} className="auth-submit">{busy ? '正在登录…' : '登录'}{!busy && <ArrowRight size={18}/>}</Button>
      <p className="auth-help"><ShieldCheck size={16}/><span>无法登录？请联系工作台管理员。</span></p>
    </form>
  </AuthFrame>;
}

export function FeishuLogin({ config, busy, error, onRetry, onPassword }: { config: AppConfig; busy: boolean; error: string; onRetry: () => void; onPassword: () => void }) {
  return <AuthFrame name={config.brand_name}><div className="auth-form auth-feishu" aria-busy={busy}>
    <span className="auth-kicker">飞书工作台</span>
    <h1>连接工作空间</h1>
    <p className="auth-description">{busy ? '正在获取你的飞书工作身份…' : '使用飞书身份访问已授权的知识和助手。'}</p>
    <ErrorNotice message={error}/>
    {busy && <Loader text="等待飞书授权…"/>}
    <div className="auth-feishu-actions">
      {!busy && <Button onClick={onRetry}><RefreshCw size={16}/>重新获取飞书身份</Button>}
      <Button variant="secondary" onClick={onPassword}>使用工作台账号登录</Button>
    </div>
  </div></AuthFrame>;
}
