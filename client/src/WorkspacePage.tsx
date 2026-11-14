import { ArrowRight, ArrowUpRight, BookOpen, Clock3, Layers3, MessageSquare, Network, Plus } from 'lucide-react';
import type { Assistant, Conversation } from './types';
import { timeText } from './api';
import { Button, EmptyState } from './components';
import { ScopeBadge } from './resources';

type Props = {
  assistants: Assistant[];
  conversations: Conversation[];
  onAssistant: (assistant: Assistant) => void;
  onConversation: (conversation: Conversation) => void;
  onNew?: (assistant?: Assistant) => void;
  onNavigate: (view: 'assistants' | 'sessions' | 'knowledge' | 'connections') => void;
  canCreate?: boolean;
  canManage?: boolean;
};

export default function WorkspacePage({ assistants, conversations, onAssistant, onConversation, onNew, onNavigate, canCreate = true, canManage = true }: Props) {
  const available = assistants.filter(assistant => assistant.enabled);
  const recent = conversations.filter(conversation => !conversation.archived)
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, 5);
  const assistantNames = new Map(assistants.map(assistant => [assistant.id, assistant.name]));

  return <div className="workspace-page">
    <header className="workspace-topbar"><span>工作台</span><time dateTime={new Date().toISOString()}>{new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' }).format(new Date())}</time></header>
    <div className="workspace-content">
      <div className="workspace-heading"><div><span className="workspace-kicker">知行工作台 · 制造业务协同</span><h1>把今天最重要的事，先做清楚。</h1><p>{canCreate ? '从异常、任务或一条业务问题开始；结果会保留来源、状态和下一步。' : '选择一个已配置的业务助手，查看当前可用能力。'}</p></div>{canCreate && onNew && <Button onClick={() => onNew()}><Plus size={17}/>新建会话</Button>}</div>
      <div className="workspace-signal-grid" aria-label="工作台状态概览">
        <div className="workspace-signal workspace-signal-primary"><span>可用业务助手</span><strong>{available.length.toString().padStart(2, '0')}</strong><small>按业务范围分工</small></div>
        <div className="workspace-signal"><span>最近业务会话</span><strong>{recent.length.toString().padStart(2, '0')}</strong><small>可继续查看上下文</small></div>
        <div className="workspace-signal"><span>结果追溯</span><strong>开启</strong><small>来源与执行记录随会话保留</small></div>
      </div>
      <div className="workspace-priority-strip"><div><span className="priority-mark" aria-hidden="true"/><div><strong>先从一个具体问题开始</strong><p>助手会先缩小业务范围，再返回可核验的结果。</p></div></div><span className="priority-note">只读优先 · 结果可追溯</span></div>
      <div className="workspace-overview">
        <section className="workspace-assistants" aria-labelledby="assistants-heading">
          <div className="workspace-section-heading"><h2 id="assistants-heading">业务助手</h2>{canManage && <button className="text-action" onClick={() => onNavigate('assistants')}>管理助手<ArrowUpRight size={14}/></button>}</div>
          {available.length ? <div className="workspace-assistant-list">{available.map(assistant => <article className="workspace-assistant" key={assistant.id}>
            <div className="workspace-assistant-heading"><span className="workspace-assistant-icon"><Layers3 size={23}/></span><button className="assistant-title-link" onClick={() => onAssistant(assistant)}><h3>{assistant.name}</h3><ArrowUpRight size={16}/></button><ScopeBadge scope={assistant.scope}/></div>
            <p>{assistant.description || '查看助手的配置，开始新的会话。'}</p>
            <div className="assistant-capabilities"><span><BookOpen size={14}/>{assistant.knowledge_base_ids?.length || 0} 个知识库</span><span><Network size={14}/>{assistant.connections.length} 个业务连接</span></div>
            <div className="workspace-assistant-footer"><button className="text-action" onClick={() => canManage ? onAssistant(assistant) : onNew?.(assistant)}>{canManage ? '查看助手' : canCreate ? '开始会话' : '查看助手'}<ArrowRight size={15}/></button>{canCreate && onNew && <Button variant="secondary" onClick={() => onNew(assistant)}>开始会话</Button>}</div>
          </article>)}</div> : <EmptyState icon={<Layers3 size={28}/>} title="还没有可用助手" description="创建一个助手，即可开始会话。知识库和业务连接可按需配置。" action={<Button onClick={() => onNavigate('assistants')}>配置助手</Button>}/>}
        </section>
        <section className="workspace-resources" aria-labelledby="resources-heading"><div className="workspace-section-heading"><h2 id="resources-heading">工作资料</h2></div><div className="workspace-resource-list">
          <button onClick={() => onNavigate('knowledge')}><span className="resource-shortcut-icon"><BookOpen size={21}/></span><span><strong>知识库</strong><small>查看文件、版本和检索结果</small></span><ArrowUpRight size={16}/></button>
          <button onClick={() => onNavigate('connections')}><span className="resource-shortcut-icon"><Network size={21}/></span><span><strong>业务连接</strong><small>管理已接入的系统与工具</small></span><ArrowUpRight size={16}/></button>
        </div></section>
      </div>
      <section className="workspace-recent" aria-labelledby="recent-heading"><div className="workspace-section-heading"><div className="section-heading-with-icon"><Clock3 size={17}/><h2 id="recent-heading">最近会话</h2></div><button className="text-action" onClick={() => onNavigate('sessions')}>查看全部<ArrowRight size={15}/></button></div>
        {recent.length ? <div className="recent-conversations"><div className="recent-columns" aria-hidden="true"><span>会话</span><span>业务助手</span><span>最近更新</span><span/></div>{recent.map(conversation => <button className="recent-conversation" key={conversation.id} onClick={() => onConversation(conversation)}><span className="recent-conversation-title"><MessageSquare size={17}/><span>{conversation.title || '新会话'}</span>{conversation.active_run_id && <span className="recent-running">执行中</span>}</span><span className="recent-assistant-name">{assistantNames.get(conversation.assistant_id) || '业务助手'}</span><time dateTime={conversation.updated_at}>{timeText(conversation.updated_at)}</time><ArrowUpRight size={15}/></button>)}</div> : <div className="workspace-recent-empty"><MessageSquare size={22}/><div><h3>还没有会话</h3><p>开始一次工作后，可以在这里继续。</p></div>{canCreate && onNew && <button className="text-action" onClick={() => onNew()}>新建会话<ArrowRight size={15}/></button>}</div>}
      </section>
    </div>
  </div>;
}
