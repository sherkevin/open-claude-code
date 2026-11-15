import { useEffect, useState } from 'react';
import { ArrowRight, ArrowUpRight, BookOpen, Clock3, Layers3, MessageSquare, Network, RefreshCw } from 'lucide-react';
import type { Assistant, Connection, Conversation } from './types';
import { api, timeText } from './api';
import { Button, EmptyState } from './components';
import { ScopeBadge } from './resources';
import AiIdentity from './AiIdentity';
import './workbench-refinement.css';

type Props = {
  assistants: Assistant[];
  conversations: Conversation[];
  onAssistant: (assistant: Assistant) => void;
  onConversation: (conversation: Conversation) => void;
  onNew?: (assistant?: Assistant) => void;
  onStart?: (question: string, assistant?: Assistant) => void;
  onNavigate: (view: 'assistants' | 'sessions' | 'knowledge' | 'connections') => void;
  canCreate?: boolean;
  canManage?: boolean;
  canReadKnowledge?: boolean;
  canManageConnections?: boolean;
  canReadSessions?: boolean;
};

export function workspaceConnectionStatus(connection: Connection) {
  if (!connection.enabled) return '已停用';
  if (connection.status === 'needs_auth') return '认证待处理';
  if (connection.status === 'failed') return '测试失败';
  if (connection.status === 'connected') return connection.tools.length ? `已发现 ${connection.tools.length} 个工具` : '未发现工具';
  return '待测试';
}

export function workspaceConnectionOverview(connections: Connection[]) {
  const priority = { failed: 0, needs_auth: 0, connected: 1, untested: 2 };
  const enabled = connections.filter(connection => connection.enabled)
    .sort((a, b) => priority[a.status] - priority[b.status]);
  return { visible: enabled.slice(0, 4), enabledCount: enabled.length,
    disabledCount: connections.length - enabled.length };
}

export function WorkspaceConnectionSummary({ connections, onManage }: { connections: Connection[]; onManage: () => void }) {
  const { visible, enabledCount, disabledCount } = workspaceConnectionOverview(connections);
  return <>
    {visible.length ? <ul>{visible.map(connection => <li key={connection.id}><span>{connection.name}</span><small data-tone={connection.status === 'connected' && connection.tools.length ? 'success' : ['failed', 'needs_auth', 'connected'].includes(connection.status) ? 'warning' : 'neutral'}>{workspaceConnectionStatus(connection)}</small></li>)}</ul>
      : <p>{disabledCount ? '暂无启用连接，可在业务连接中启用已有配置。' : '尚未配置业务连接。接入 MCP 后，为助手选择所需工具。'}</p>}
    <button type="button" className="wr-text-action" onClick={onManage}>{enabledCount > visible.length ? `查看全部 ${enabledCount} 个启用连接` : connections.length ? '检查与管理连接' : '配置业务连接'}<ArrowRight size={14}/></button>
    {disabledCount > 0 && <button type="button" className="wr-disabled-connections" onClick={onManage}>另有 {disabledCount} 个停用连接<ArrowUpRight size={13}/></button>}
  </>;
}

function updatedTime(value: string) {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

export default function WorkspacePage({
  assistants, conversations, onAssistant, onConversation, onNew, onStart, onNavigate,
  canCreate = true, canManage = true, canReadKnowledge = true, canManageConnections = true, canReadSessions = true,
}: Props) {
  const [question, setQuestion] = useState('');
  const [connections, setConnections] = useState<Connection[] | null>(null);
  const [connectionError, setConnectionError] = useState(false);
  const [connectionRevision, setConnectionRevision] = useState(0);
  useEffect(() => {
    if (!canManageConnections) return;
    const controller = new AbortController();
    setConnections(null); setConnectionError(false);
    void api<Connection[]>('/connections', { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) setConnections(value);
    }).catch(() => { if (!controller.signal.aborted) setConnectionError(true); });
    return () => controller.abort();
  }, [canManageConnections, connectionRevision]);
  const [assistantId, setAssistantId] = useState('');
  const available = assistants.filter(assistant => assistant.enabled);
  const selectedAssistant = available.find(assistant => assistant.id === assistantId);
  const selectionUnavailable = Boolean(assistantId && !selectedAssistant);
  const recent = canReadSessions ? conversations.filter(conversation => !conversation.archived)
    .sort((a, b) => updatedTime(b.updated_at) - updatedTime(a.updated_at)).slice(0, 5) : [];
  const runningCount = canReadSessions ? conversations.filter(conversation => !conversation.archived && conversation.active_run_id).length : 0;
  const assistantNames = new Map(assistants.map(assistant => [assistant.id, assistant.name]));
  const hasResources = canReadKnowledge || canManageConnections;
  const canAsk = canCreate && Boolean(onStart) && available.length > 0;
  const today = new Date();

  return <div className="workspace-page wr-workspace">
    <header className="wr-topbar">
      <span>工作台</span>
      <time dateTime={today.toISOString()}>{new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' }).format(today)}</time>
    </header>
    <div className="wr-content">
      <header className="wr-heading">
        <div><h1>业务工作台</h1><p>{canCreate ? '提出问题，或继续最近的工作。' : '查看可用助手与最近的工作。'}</p></div>
      </header>

      {canAsk && <section className="wr-question-panel" aria-labelledby="wr-question-heading">
        <div className="wr-question-heading"><AiIdentity size="small"/><h2 id="wr-question-heading">提问</h2></div>
        <form onSubmit={event => {
          event.preventDefault();
          if (question.trim() && !selectionUnavailable) onStart?.(question.trim(), selectedAssistant);
        }}>
          <label className="wr-sr-only" htmlFor="wr-question">业务问题</label>
          <textarea id="wr-question" rows={1} value={question} onChange={event => setQuestion(event.target.value)} placeholder="输入业务问题，或描述需要查阅的资料" aria-describedby="wr-question-hint"/>
          <div className="wr-question-footer">
            <label className="wr-assistant-select"><Layers3 size={16}/><span className="wr-sr-only">使用助手</span><select value={assistantId} onChange={event => setAssistantId(event.target.value)}>
              <option value="">稍后选择助手</option>
              {selectionUnavailable && <option value={assistantId} disabled>原助手已不可用，请重新选择</option>}
              {available.map(assistant => <option key={assistant.id} value={assistant.id}>{assistant.name}</option>)}
            </select></label>
            <span id="wr-question-hint" className="wr-question-hint">进入会话后确认并发送</span>
            <Button type="submit" disabled={!question.trim() || selectionUnavailable}>继续<ArrowRight size={16}/></Button>
          </div>
        </form>
      </section>}

      <div className={`wr-overview${hasResources ? '' : ' wr-overview-full'}`}>
        <div className="wr-main-work">
      {canReadSessions && <section className="wr-recent" aria-labelledby="wr-recent-heading">
        <div className="wr-section-heading"><div className="wr-recent-heading"><Clock3 size={17}/><h2 id="wr-recent-heading">继续工作</h2>{runningCount > 0 && <span className="wr-running-summary">{runningCount} 项执行中</span>}</div><button type="button" className="wr-text-action" onClick={() => onNavigate('sessions')}>全部会话<ArrowRight size={14}/></button></div>
        {recent.length ? <div className="wr-recent-list"><div className="wr-recent-columns" aria-hidden="true"><span>会话</span><span>业务助手</span><span>最近更新</span><span/></div>{recent.map(conversation => <button type="button" className="wr-recent-row" key={conversation.id} onClick={() => onConversation(conversation)}>
          <span className="wr-conversation-title"><MessageSquare size={16}/><span>{conversation.title || '新会话'}</span>{conversation.active_run_id && <span className="wr-running"><span aria-hidden="true"/>执行中</span>}</span>
          <span className="wr-conversation-assistant">{assistantNames.get(conversation.assistant_id) || '业务助手'}</span>
          {updatedTime(conversation.updated_at) ? <time dateTime={conversation.updated_at}>{timeText(conversation.updated_at)}</time> : <span className="wr-time-unknown">更新时间未提供</span>}
          <ArrowUpRight size={15}/>
        </button>)}</div> : <div className="wr-recent-empty"><MessageSquare size={22}/><div><h3>还没有会话</h3><p>{canAsk ? '从上方提问开始，工作记录会保存在这里。' : '开始一次工作后，可以在这里继续。'}</p></div></div>}
      </section>}
        <section className="wr-assistants" aria-labelledby="wr-assistants-heading">
          <div className="wr-section-heading"><h2 id="wr-assistants-heading">业务助手 <span className="wr-count">{available.length}</span></h2>{canManage && <button type="button" className="wr-text-action" onClick={() => onNavigate('assistants')}>管理助手<ArrowUpRight size={14}/></button>}</div>
          {available.length ? <div className="wr-assistant-list">{available.map(assistant => <article className="wr-assistant" key={assistant.id}>
            <div className="wr-assistant-heading"><AiIdentity size="small"/><button type="button" className="wr-title-link" onClick={() => onAssistant(assistant)}><h3>{assistant.name}</h3><ArrowUpRight size={14}/></button><ScopeBadge scope={assistant.scope}/></div>
            <p>{assistant.description || '查看助手能力，开始新的会话。'}</p>
            <div className="wr-assistant-capabilities"><span><BookOpen size={14}/>{assistant.knowledge_base_ids?.length || 0} 个知识库</span><span><Network size={14}/>已配置 {assistant.connections.length} 个连接</span></div>
            <div className="wr-assistant-footer"><button type="button" className="wr-text-action" onClick={() => onAssistant(assistant)}>查看能力<ArrowRight size={14}/></button>{canCreate && onNew && <Button variant="secondary" onClick={() => onNew(assistant)}>开始会话</Button>}</div>
          </article>)}</div> : <div className="wr-empty-panel"><EmptyState icon={<Layers3 size={26}/>} title="还没有可用助手" description={canManage ? '先配置一个助手，再开始业务会话。' : '管理员开放助手后，可以在这里开始工作。'} action={canManage ? <Button onClick={() => onNavigate('assistants')}>配置助手</Button> : undefined}/></div>}
        </section>

        </div>
        {hasResources && <section className="wr-resources" aria-labelledby="wr-resources-heading">
          <div className="wr-section-heading"><h2 id="wr-resources-heading">连接与资料</h2></div>
          {canManageConnections && <div className="wr-connection-panel">
            <div className="wr-connection-title"><strong>业务连接状态</strong><button type="button" className="icon-button" aria-label="重新读取连接状态" onClick={() => setConnectionRevision(value => value + 1)}><RefreshCw size={15}/></button></div>
            {connectionError ? <p role="status">连接状态暂未取得，请重新读取。</p> : connections === null ? <p role="status">正在读取连接状态…</p> : <WorkspaceConnectionSummary connections={connections} onManage={() => onNavigate('connections')}/>}
            <small className="wr-snapshot-note">上次检查结果，不代表当前在线状态。</small>
          </div>}
          <div className="wr-resource-list">
            {canReadKnowledge && <button type="button" onClick={() => onNavigate('knowledge')}><span className="wr-resource-icon"><BookOpen size={20}/></span><span><strong>知识库</strong><small>查阅文件与检索结果</small></span><ArrowUpRight size={15}/></button>}

          </div>

        </section>}
      </div>


    </div>
  </div>;
}
