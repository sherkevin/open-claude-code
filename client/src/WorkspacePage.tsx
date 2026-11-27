import { useState } from 'react';
import { ArrowRight, ArrowUpRight, BookOpen, Clock3, Layers3, MessageSquare, Network, RefreshCw } from 'lucide-react';
import type { Assistant, Connection, Conversation } from './types';
import { timeText } from './api';
import { codexPhaseLabels, isLiveCodexPhase, useWorkspaceCodexPhases } from './useWorkspaceCodexPhases';
import { Button, EmptyState } from './components';
import AiIdentity from './AiIdentity';
import './workbench-refinement.css';

export type WorkspaceReadState = { loading: boolean; loaded: boolean; error: string };
const readyRead: WorkspaceReadState = { loading: false, loaded: true, error: '' };

export function WorkspaceReadNotice({ state, label, onRetry }: { state: WorkspaceReadState; label: string; onRetry?: () => void }) {
  if (state.error) return <div className="wr-recent-empty" role="status"><RefreshCw size={20}/><div><h3>{label}{state.loaded ? '更新未完成' : '暂未读取成功'}</h3><p>{state.loaded ? '当前仍显示上次读取的内容。' : '请重试，或先使用其他功能。'}</p></div>{onRetry && <button type="button" className="wr-text-action" onClick={onRetry}>重新读取<RefreshCw size={14}/></button>}</div>;
  if (state.loading || !state.loaded) return <p className="section-help" role="status">正在{state.loaded ? '更新' : '读取'}{label}…</p>;
  return null;
}

type Props = {
  assistants: Assistant[];
  conversations: Conversation[];
  codexEnabled?: boolean;
  assistantsRead?: WorkspaceReadState;
  conversationsRead?: WorkspaceReadState;
  onReload?: () => void;
  onAssistant: (assistant: Assistant) => void;
  onConversation: (conversation: Conversation) => void;
  onNew?: (assistant?: Assistant) => void;
  onStart?: (question: string, assistant: Assistant) => void;
  onEngineering?: (question: string, assistant: Assistant) => void;
  startingEngineering?: boolean;
  starting?: boolean;
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

export function workspaceConnectionOverview(connections: Connection[], boundIds: string[] = []) {
  const priority = { failed: 0, needs_auth: 0, connected: 1, untested: 2 };
  const bound = new Set(boundIds);
  const enabled = connections.filter(connection => connection.enabled)
    .sort((a, b) => Number(priority[a.status] !== 0) - Number(priority[b.status] !== 0)
      || Number(bound.has(b.id)) - Number(bound.has(a.id))
      || priority[a.status] - priority[b.status]);
  return { visible: enabled.slice(0, 3), enabledCount: enabled.length,
    disabledCount: connections.length - enabled.length };
}

export function WorkspaceConnectionSummary({ connections, boundIds = [], onManage }: { connections: Connection[]; boundIds?:string[]; onManage: () => void }) {
  const { visible, enabledCount, disabledCount } = workspaceConnectionOverview(connections, boundIds);
  return <>
    {visible.length ? <ul>{visible.map(connection => <li key={connection.id}><span>{connection.name}</span><small data-tone={connection.status === 'connected' && connection.tools.length ? 'success' : ['failed', 'needs_auth', 'connected'].includes(connection.status) ? 'warning' : 'neutral'}>{workspaceConnectionStatus(connection)}</small></li>)}</ul>
      : <p>{disabledCount ? '暂无启用连接，可在MCP中启用已有配置。' : '尚未配置MCP。接入 MCP 后，为助手选择所需工具。'}</p>}
    <button type="button" className="wr-text-action" onClick={onManage}>{enabledCount > visible.length ? `查看全部 ${enabledCount} 个启用连接` : connections.length ? '检查与管理连接' : '配置MCP'}<ArrowRight size={14}/></button>
    {disabledCount > 0 && <button type="button" className="wr-disabled-connections" onClick={onManage}>另有 {disabledCount} 个停用连接<ArrowUpRight size={13}/></button>}
  </>;
}

function updatedTime(value: string) {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

export function defaultConversationAssistant(assistants: Assistant[]) {
  const available = assistants.filter(assistant => assistant.enabled);
  return available.find(assistant => assistant.name === '工业业务助手') || available[0];
}

export default function WorkspacePage({
  assistants, conversations, assistantsRead = readyRead, conversationsRead = readyRead, onReload, onAssistant, onConversation, onNew, onStart, onEngineering, onNavigate,
  starting = false, startingEngineering = false, canCreate = true, canManage = true, canReadSessions = true, codexEnabled = false,
}: Props) {
  const [question, setQuestion] = useState('');
  const [assistantId, setAssistantId] = useState('');
  const available = assistants.filter(assistant => assistant.enabled);
  const selectedAssistant = assistantId ? available.find(assistant => assistant.id === assistantId) : defaultConversationAssistant(available);
  const selectionUnavailable = Boolean(assistantId && !selectedAssistant);
  const recent = canReadSessions ? conversations.filter(conversation => !conversation.archived)
    .sort((a, b) => updatedTime(b.updated_at) - updatedTime(a.updated_at)).slice(0, 5) : [];
  const codexPhases = useWorkspaceCodexPhases(codexEnabled && conversationsRead.loaded
    ? recent.filter(row => !row.active_run_id).map(row => row.id) : []);
  const runningCount = canReadSessions ? conversations.filter(conversation => !conversation.archived && conversation.active_run_id).length : 0;
  const assistantNames = new Map(assistants.map(assistant => [assistant.id, assistant.name]));
  const canAsk = canCreate && Boolean(onStart || (codexEnabled && onEngineering)) && available.length > 0;
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
          if (!starting && question.trim() && selectedAssistant) onStart?.(question.trim(), selectedAssistant);
        }}>
          <label className="wr-sr-only" htmlFor="wr-question">业务问题</label>
          <textarea id="wr-question" rows={1} value={question} readOnly={starting} onChange={event => setQuestion(event.target.value)} placeholder="输入业务问题，或描述需要查阅的资料"/>
          <div className="wr-question-footer">
            <label className="wr-assistant-select"><Layers3 size={16}/><span className="wr-sr-only">使用助手</span><select value={selectedAssistant?.id || assistantId} disabled={starting} onChange={event => setAssistantId(event.target.value)}>
              {selectionUnavailable && <option value={assistantId} disabled>原助手已不可用，请重新选择</option>}
              {available.map(assistant => <option key={assistant.id} value={assistant.id}>{assistant.name}</option>)}
            </select></label>
            {codexEnabled && onEngineering && <Button type="button" variant="secondary" busy={startingEngineering}
              disabled={starting || !selectedAssistant} onClick={() => { if (selectedAssistant) onEngineering(question, selectedAssistant); }}>工程任务</Button>}
            {onStart && <Button type="submit" busy={starting && !startingEngineering} disabled={starting || !question.trim() || selectionUnavailable}>{starting && !startingEngineering ? '正在发送' : '发送'}<ArrowRight size={16}/></Button>}
          </div>
        </form>
      </section>}

      <div className="wr-overview wr-overview-full">
        <div className="wr-main-work">
      {canReadSessions && <section className="wr-recent" aria-labelledby="wr-recent-heading" aria-busy={conversationsRead.loading}>
        <div className="wr-section-heading"><div className="wr-recent-heading"><Clock3 size={17}/><h2 id="wr-recent-heading">继续工作</h2>{runningCount > 0 && <span className="wr-running-summary">{codexEnabled && '普通对话 '}{runningCount} 项执行中</span>}</div><button type="button" className="wr-text-action" onClick={() => onNavigate('sessions')}>全部会话<ArrowRight size={14}/></button></div>
        <WorkspaceReadNotice state={conversationsRead} label="最近会话" onRetry={onReload}/>
        {recent.length ? <div className="wr-recent-list"><div className="wr-recent-columns" aria-hidden="true"><span>会话</span><span>业务助手</span><span>最近更新</span><span/></div>{recent.map(conversation => <button type="button" className="wr-recent-row" key={conversation.id} onClick={() => onConversation(conversation)}>
          <span className="wr-conversation-title"><MessageSquare size={16}/><span>{conversation.title || '新会话'}</span>{conversation.active_run_id
            ? <span className="wr-running"><span aria-hidden="true"/>执行中</span>
            : codexPhases[conversation.id] && <span className="wr-running" data-codex-live={isLiveCodexPhase(codexPhases[conversation.id])}>
              {isLiveCodexPhase(codexPhases[conversation.id]) && <span aria-hidden="true"/>}{codexPhaseLabels[codexPhases[conversation.id]]}
            </span>}</span>
          <span className="wr-conversation-assistant">{assistantNames.get(conversation.assistant_id) || '业务助手'}</span>
          {updatedTime(conversation.updated_at) ? <time dateTime={conversation.updated_at}>{timeText(conversation.updated_at)}</time> : <span className="wr-time-unknown">更新时间未提供</span>}
          <ArrowUpRight size={15}/>
        </button>)}</div> : conversationsRead.loaded && !conversationsRead.loading && !conversationsRead.error ? <div className="wr-recent-empty"><MessageSquare size={22}/><div><h3>还没有会话</h3><p>{canAsk ? '从上方提问开始，工作记录会保存在这里。' : '开始一次工作后，可以在这里继续。'}</p></div></div> : null}
      </section>}
        <section className="wr-assistants" aria-labelledby="wr-assistants-heading" aria-busy={assistantsRead.loading}>
          <div className="wr-section-heading"><h2 id="wr-assistants-heading">业务助手 <span className="wr-count">{assistantsRead.loaded ? available.length : '—'}</span></h2>{canManage && <button type="button" className="wr-text-action" onClick={() => onNavigate('assistants')}>管理助手<ArrowUpRight size={14}/></button>}</div>
          <WorkspaceReadNotice state={assistantsRead} label="业务助手" onRetry={onReload}/>
          {available.length ? <div className="wr-assistant-list">{available.map(assistant => <article className="wr-assistant" key={assistant.id}>
            <div className="wr-assistant-heading"><span className="wr-assistant-icon"><Layers3 size={19}/></span><button type="button" className="wr-title-link" title="查看能力" onClick={() => onAssistant(assistant)}><h3>{assistant.name}</h3><ArrowUpRight size={14}/></button></div>
            <p>{assistant.description || '查看助手能力，开始新的会话。'}</p>
            <div className="wr-assistant-capabilities"><span><BookOpen size={14}/>{assistant.knowledge_base_ids?.length || 0} 个知识库</span><span><Network size={14}/>已配置 {assistant.connections.length} 个 MCP</span></div>
            <div className="wr-assistant-footer">{canCreate && onNew && <Button variant="secondary" onClick={() => onNew(assistant)}>开始会话</Button>}</div>
          </article>)}</div> : assistantsRead.loaded && !assistantsRead.loading && !assistantsRead.error ? <div className="wr-empty-panel"><EmptyState icon={<Layers3 size={26}/>} title="还没有可用助手" description={canManage ? '先配置一个助手，再开始业务会话。' : '管理员开放助手后，可以在这里开始工作。'} action={canManage ? <Button onClick={() => onNavigate('assistants')}>配置助手</Button> : undefined}/></div> : null}
        </section>

        </div>

      </div>


    </div>
  </div>;
}
