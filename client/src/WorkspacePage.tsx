import { useState } from 'react';
import { ArrowRight, ArrowUpRight, BookOpen, Clock3, Layers3, MessageSquare, Network, Plus } from 'lucide-react';
import type { Assistant, Conversation } from './types';
import { timeText } from './api';
import { Button, EmptyState } from './components';
import { ScopeBadge } from './resources';
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

function updatedTime(value: string) {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

export default function WorkspacePage({
  assistants, conversations, onAssistant, onConversation, onNew, onStart, onNavigate,
  canCreate = true, canManage = true, canReadKnowledge = true, canManageConnections = true, canReadSessions = true,
}: Props) {
  const [question, setQuestion] = useState('');
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
        {canCreate && onNew && <Button variant="secondary" onClick={() => onNew()}><Plus size={16}/>新建会话</Button>}
      </header>

      {canAsk && <section className="wr-question-panel" aria-labelledby="wr-question-heading">
        <div className="wr-question-heading"><MessageSquare size={18}/><h2 id="wr-question-heading">开始一项工作</h2></div>
        <form onSubmit={event => {
          event.preventDefault();
          if (question.trim() && !selectionUnavailable) onStart?.(question.trim(), selectedAssistant);
        }}>
          <label className="wr-sr-only" htmlFor="wr-question">业务问题</label>
          <textarea id="wr-question" rows={2} value={question} onChange={event => setQuestion(event.target.value)} placeholder="描述要查询的对象、时间范围，或需要查阅的资料…" aria-describedby="wr-question-hint"/>
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
        <section className="wr-assistants" aria-labelledby="wr-assistants-heading">
          <div className="wr-section-heading"><h2 id="wr-assistants-heading">业务助手 <span className="wr-count">{available.length}</span></h2>{canManage && <button type="button" className="wr-text-action" onClick={() => onNavigate('assistants')}>管理助手<ArrowUpRight size={14}/></button>}</div>
          {available.length ? <div className="wr-assistant-list">{available.map(assistant => <article className="wr-assistant" key={assistant.id}>
            <div className="wr-assistant-heading"><span className="wr-assistant-icon"><Layers3 size={20}/></span><button type="button" className="wr-title-link" onClick={() => onAssistant(assistant)}><h3>{assistant.name}</h3><ArrowUpRight size={14}/></button><ScopeBadge scope={assistant.scope}/></div>
            <p>{assistant.description || '查看助手能力，开始新的会话。'}</p>
            <div className="wr-assistant-capabilities"><span><BookOpen size={14}/>{assistant.knowledge_base_ids?.length || 0} 个知识库</span><span><Network size={14}/>已配置 {assistant.connections.length} 个连接</span></div>
            <div className="wr-assistant-footer"><button type="button" className="wr-text-action" onClick={() => onAssistant(assistant)}>查看能力<ArrowRight size={14}/></button>{canCreate && onNew && <Button variant="secondary" onClick={() => onNew(assistant)}>开始会话</Button>}</div>
          </article>)}</div> : <div className="wr-empty-panel"><EmptyState icon={<Layers3 size={26}/>} title="还没有可用助手" description={canManage ? '先配置一个助手，再开始业务会话。' : '管理员开放助手后，可以在这里开始工作。'} action={canManage ? <Button onClick={() => onNavigate('assistants')}>配置助手</Button> : undefined}/></div>}
        </section>

        {hasResources && <section className="wr-resources" aria-labelledby="wr-resources-heading">
          <div className="wr-section-heading"><h2 id="wr-resources-heading">资料与连接</h2></div>
          <div className="wr-resource-list">
            {canReadKnowledge && <button type="button" onClick={() => onNavigate('knowledge')}><span className="wr-resource-icon"><BookOpen size={20}/></span><span><strong>知识库</strong><small>查阅文件与检索结果</small></span><ArrowUpRight size={15}/></button>}
            {canManageConnections && <button type="button" onClick={() => onNavigate('connections')}><span className="wr-resource-icon"><Network size={20}/></span><span><strong>业务连接</strong><small>接入 MCP，检查工具能力</small></span><ArrowUpRight size={15}/></button>}
          </div>
          {canManageConnections && <p className="wr-resource-note">连接配置和工具检查，可在业务连接中查看。</p>}
        </section>}
      </div>

      {canReadSessions && <section className="wr-recent" aria-labelledby="wr-recent-heading">
        <div className="wr-section-heading"><div className="wr-recent-heading"><Clock3 size={17}/><h2 id="wr-recent-heading">最近工作</h2>{runningCount > 0 && <span className="wr-running-summary">{runningCount} 项执行中</span>}</div><button type="button" className="wr-text-action" onClick={() => onNavigate('sessions')}>全部会话<ArrowRight size={14}/></button></div>
        {recent.length ? <div className="wr-recent-list"><div className="wr-recent-columns" aria-hidden="true"><span>会话</span><span>业务助手</span><span>最近更新</span><span/></div>{recent.map(conversation => <button type="button" className="wr-recent-row" key={conversation.id} onClick={() => onConversation(conversation)}>
          <span className="wr-conversation-title"><MessageSquare size={16}/><span>{conversation.title || '新会话'}</span>{conversation.active_run_id && <span className="wr-running"><span aria-hidden="true"/>执行中</span>}</span>
          <span className="wr-conversation-assistant">{assistantNames.get(conversation.assistant_id) || '业务助手'}</span>
          {updatedTime(conversation.updated_at) ? <time dateTime={conversation.updated_at}>{timeText(conversation.updated_at)}</time> : <span className="wr-time-unknown">更新时间未提供</span>}
          <ArrowUpRight size={15}/>
        </button>)}</div> : <div className="wr-recent-empty"><MessageSquare size={22}/><div><h3>还没有会话</h3><p>开始一次工作后，可以在这里继续。</p></div>{canCreate && onNew && <button type="button" className="wr-text-action" onClick={() => onNew()}>新建会话<ArrowRight size={14}/></button>}</div>}
      </section>}
    </div>
  </div>;
}
