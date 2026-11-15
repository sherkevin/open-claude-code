import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowUpRight, BookOpen, ChartNoAxesCombined, Database, FolderOpen, House, LogOut, Menu, Network, PanelLeftClose, PanelLeftOpen, Plus, RefreshCw, Settings2, ShieldCheck, Sparkles, Users, X } from 'lucide-react';
import { api, errorText, post, timeText } from './api';
import { configAllows, type AppConfig, type Assistant, type Conversation, type ModelOption, type ModelSelection, type User } from './types';
import { Button, ErrorNotice, Loader, Modal } from './components';
import NewSession from './NewSession';
import WorkspacePage, { type WorkspaceReadState } from './WorkspacePage';
import { deferredPage } from './DeferredPage';
import { canManageCapabilities } from './capabilitySettings';
import { isFeishuEmbed, requestFeishuCode } from './feishu';
import Brand from './Brand';
import { groupConversations } from './conversationProjects';
import { useSidebarLayout } from './sidebarLayout';
import './sidebar-controls.css';
import { Login, FeishuLogin } from './AuthPages';
import { connectionTrialSession, type ConnectionSetupContext } from './connectionHandoff';

const ChatPage = deferredPage(() => import('./ChatPage'));
const ConnectionsPage = deferredPage(() => import('./ConnectionsPage'));
const MemoryPage = deferredPage(() => import('./MemoryPage'));
const AssistantsPage = deferredPage(() => import('./AssistantsPage'));
const SessionsPage = deferredPage(() => import('./SessionsPage'));
const SkillsPage = deferredPage(() => import('./SkillsPage'));
const KnowledgePage = deferredPage(() => import('./KnowledgePage'));
const BusinessDataPage = deferredPage(() => import('./BusinessDataPage'));
const ApprovalsPage = deferredPage(async () => ({ default: (await import('./AdminPages')).ApprovalsPage }));
const UsersPage = deferredPage(async () => ({ default: (await import('./AdminPages')).UsersPage }));
const UsagePage = deferredPage(async () => ({ default: (await import('./AdminPages')).UsagePage }));
const BenchmarkPage = deferredPage(() => import('./BenchmarkPage'));
const AssistantDetailPage = deferredPage(() => import('./AssistantDetailPage'));
const CapabilitySettingsPage = deferredPage(() => import('./CapabilitySettingsPage'));

type View = 'workspace' | 'sessions' | 'assistant-detail' | 'connections' | 'assistants' | 'knowledge' | 'business-data' | 'skills' | 'users' | 'usage' | 'memory' | 'benchmark' | 'approvals' | 'capability-settings';
const fallbackConfig: AppConfig = { brand_name: 'Flint 开物', upload_max_bytes: 20 * 1024 * 1024, auth_types: ['none', 'bearer'], model_label: '', feishu_h5: { enabled: false, app_id: '', path: '/feishu' } };


const pendingWorkspaceRead: WorkspaceReadState = { loading: true, loaded: false, error: '' };

export function ConversationHistory({ conversations, state, selectedId, onOpen, onRetry }: {
  conversations: Conversation[]; state: WorkspaceReadState; selectedId?: string;
  onOpen: (conversation: Conversation) => void; onRetry: () => void;
}) {
  const visible = conversations.filter(conversation => !conversation.archived).slice(0, 30);
  return <div className="conversation-history" aria-busy={state.loading}>
    {state.loading && <p className="sidebar-hint" role="status">{state.loaded ? '正在更新最近会话…' : '正在读取最近会话…'}</p>}
    {state.error && <div className="sidebar-hint" role="status"><p>{state.loaded ? '会话更新未完成，仍显示上次读取的内容。' : '最近会话暂未读取成功。'}</p><button type="button" className="wr-text-action" onClick={onRetry}>重新读取</button></div>}
    {visible.length ? groupConversations(visible).map(group=><section className="history-project" key={group.name}><h3>{group.name||'未分类'}</h3>{group.conversations.map(conversation => <button title={conversation.title} key={conversation.id} className={`history-item ${selectedId === conversation.id ? 'selected' : ''}`} onClick={() => onOpen(conversation)}><span>{conversation.title || '新会话'}</span><small>{timeText(conversation.updated_at)}</small></button>)}</section>)
      : state.loaded && !state.loading && !state.error ? <p className="sidebar-hint">从一项具体工作开始，<br/>会话会保存在这里。</p> : null}
  </div>;
}


export default function App() {
  const feishuEmbed = isFeishuEmbed();
  const sidebarLayout = useSidebarLayout();
  const [config,setConfig]=useState(fallbackConfig);const [user,setUser]=useState<User|null>(null);const [initializing,setInitializing]=useState(true);const [initialError,setInitialError]=useState('');const [error,setError]=useState('');const [view,setView]=useState<View>('workspace');
  const [feishuAuth,setFeishuAuth]=useState<'idle'|'authorizing'>('idle');const [feishuError,setFeishuError]=useState('');const [feishuFallback,setFeishuFallback]=useState(false);
  const [assistants,setAssistants]=useState<Assistant[]>([]);const [conversations,setConversations]=useState<Conversation[]>([]);const [models,setModels]=useState<ModelOption[]>([]);const [modelsLoading,setModelsLoading]=useState(true);const [modelsError,setModelsError]=useState('');
  const [assistantsRead,setAssistantsRead]=useState<WorkspaceReadState>(pendingWorkspaceRead);
  const [conversationsRead,setConversationsRead]=useState<WorkspaceReadState>(pendingWorkspaceRead);
  const workspaceRequest=useRef<AbortController|null>(null);
  const resetWorkspace=useCallback(()=>{workspaceRequest.current?.abort();workspaceRequest.current=null;setAssistants([]);setConversations([]);setAssistantsRead(pendingWorkspaceRead);setConversationsRead(pendingWorkspaceRead);},[]);
  const [selected,setSelected]=useState<Conversation|null>(null);const [assistantDetail,setAssistantDetail]=useState<Assistant|null>(null);const [sidebarOpen,setSidebarOpen]=useState(false);const [newSession,setNewSession]=useState<{assistant?:Assistant;question?:string;project?:string;selection?:ModelSelection;connectionSetup?:Readonly<ConnectionSetupContext>}|null>(null);
  const [initialDraft,setInitialDraft]=useState<{conversationId:string;text:string}|null>(null);
  const [connectionSetup,setConnectionSetup]=useState<ConnectionSetupContext|null>(null);
  const [memoryDirty,setMemoryDirty]=useState(false);const [memoryEpoch,setMemoryEpoch]=useState(0);const memorySavingRef=useRef(false);const [assistantDirty,setAssistantDirty]=useState(false);const [assistantSaving,setAssistantSaving]=useState(false);const savingRef=useRef(false);const [draftEpoch,setDraftEpoch]=useState(0);const [pendingKind,setPendingKind]=useState<'memory'|'assistant'>('memory');const [pendingNavigation,setPendingNavigation]=useState<(()=>void)|null>(null);
  const changeMemorySaving=useCallback((saving:boolean)=>{memorySavingRef.current=saving;},[]);
  const changeAssistantSaving=useCallback((saving:boolean)=>{savingRef.current=saving;setAssistantSaving(saving);},[]);
  useEffect(()=>{const guard=(event:BeforeUnloadEvent)=>{if(memoryDirty||assistantDirty||savingRef.current||memorySavingRef.current){event.preventDefault();event.returnValue='';}};window.addEventListener('beforeunload',guard);return()=>window.removeEventListener('beforeunload',guard);},[memoryDirty,assistantDirty]);
  const initialize=useCallback(async()=>{setInitializing(true);setInitialError('');setFeishuError('');try{const conf=await api<AppConfig>('/config');setConfig(conf);document.title=conf.brand_name;try{setUser(await api<User>('/auth/me'));}catch(e){if(!(e&&typeof e==='object'&&'status'in e&&e.status===401))throw e;setUser(null);if(feishuEmbed&&!feishuFallback){if(!conf.feishu_h5?.enabled){setFeishuError('服务端还没有开启飞书网页应用，请先完成应用凭据和白名单配置。');}else{setFeishuAuth('authorizing');try{const code=await requestFeishuCode(conf.feishu_h5.app_id);setUser(await post<User>('/feishu/h5/login',{code}));setFeishuAuth('idle');}catch(authError){setFeishuAuth('idle');setFeishuError(errorText(authError));}}}}}catch(e){setInitialError(errorText(e));}finally{setInitializing(false);}},[feishuEmbed,feishuFallback]);
  useEffect(()=>{void initialize();const expired=()=>{setUser(null);setSelected(null);setInitialDraft(null);setConnectionSetup(null);resetWorkspace();setNewSession(null);setAssistantDetail(null);setPendingNavigation(null);setMemoryDirty(false);setAssistantDirty(false);setAssistantSaving(false);savingRef.current=false;memorySavingRef.current=false;setView('workspace');};window.addEventListener('session-expired',expired);return()=>window.removeEventListener('session-expired',expired);},[initialize,resetWorkspace]);
  const refresh=useCallback(async()=>{
    if(!user)return;
    workspaceRequest.current?.abort();
    const controller=new AbortController();workspaceRequest.current=controller;
    setAssistantsRead(previous=>({...previous,loading:true,error:''}));
    setConversationsRead(previous=>({...previous,loading:true,error:''}));
    await Promise.allSettled([
      api<Assistant[]>('/assistants',{signal:controller.signal}).then(values=>{
        if(controller.signal.aborted)return;
        setAssistants(values);setAssistantsRead({loading:false,loaded:true,error:''});
        setAssistantDetail(previous=>previous?values.find(a=>a.id===previous.id)||previous:null);
      }).catch(reason=>{if(!controller.signal.aborted)setAssistantsRead(previous=>({...previous,loading:false,error:errorText(reason)}));}),
      api<Conversation[]>('/conversations',{signal:controller.signal}).then(values=>{
        if(controller.signal.aborted)return;
        setConversations(values);setConversationsRead({loading:false,loaded:true,error:''});
      }).catch(reason=>{if(!controller.signal.aborted)setConversationsRead(previous=>({...previous,loading:false,error:errorText(reason)}));}),
    ]);
    if(workspaceRequest.current===controller)workspaceRequest.current=null;
  },[user]);
  useEffect(()=>{void refresh();return()=>{workspaceRequest.current?.abort();};},[refresh]);
  const refreshModels=useCallback(async()=>{if(!user)return;setModelsLoading(true);setModelsError('');try{setModels(await api<ModelOption[]>('/models'));}catch(e){setModelsError(errorText(e));}finally{setModelsLoading(false);}},[user]);useEffect(()=>{void refreshModels();},[refreshModels]);
  const protectMemory=(action:()=>void)=>{if(memorySavingRef.current){setError('会话记忆正在保存，请稍候再离开。');return;}if(savingRef.current){setError('助手配置正在保存，请稍候再离开。');return;}if(assistantDirty||memoryDirty){setPendingKind(assistantDirty?'assistant':'memory');setPendingNavigation(()=>action);}else action();};
  const snapshotAssistant=(assistant:Assistant)=>{setAssistantDetail(assistant);setAssistants(previous=>previous.map(item=>item.id===assistant.id?assistant:item));};
  const showView=(next:View)=>{if(next!=='assistants'&&next!=='connections')setConnectionSetup(null);setView(next);setSidebarOpen(false);setError('');};const navigate=(next:View)=>{if(next==='workspace'){protectMemory(()=>{setSelected(null);showView('workspace');});return;}if(next===view){setSidebarOpen(false);return;}protectMemory(()=>showView(next));};
  const newChat=(assistant?:Assistant,question?:string,project?:string,selection?:ModelSelection)=>protectMemory(()=>{setNewSession({assistant,question,project,selection});setSidebarOpen(false);});
  const startConnectionAssistant=(assistant:Assistant,connection:Readonly<ConnectionSetupContext>,question?:string)=>protectMemory(()=>{setAssistants(previous=>previous.some(item=>item.id===assistant.id)?previous.map(item=>item.id===assistant.id?assistant:item):[...previous,assistant]);setNewSession({...connectionTrialSession(assistant,connection),...(question?{question}: {})});setSidebarOpen(false);});
  const startConfiguredAssistant=(assistant:Assistant)=>{if(!connectionSetup){setError('本次连接信息已失效，请返回连接页重新开始。');return;}startConnectionAssistant(assistant,connectionSetup);};
  const openConversation=(conversation:Conversation)=>protectMemory(()=>{setInitialDraft(null);setSelected(conversation);showView('workspace');});
  const openAssistant=(assistant:Assistant)=>protectMemory(()=>{setAssistantDetail(assistant);showView('assistant-detail');});
  const logout=async()=>{try{await post('/auth/logout');setUser(null);setSelected(null);setInitialDraft(null);setConnectionSetup(null);resetWorkspace();setView('workspace');}catch(e){setError(errorText(e));}};
  if(initializing)return <div className="boot zhixing-theme"><Brand name={config.brand_name}/><Loader text="正在连接工作台…"/></div>;
  if(initialError)return <div className="boot zhixing-theme"><Brand name={config.brand_name}/><ErrorNotice message={initialError}/><Button onClick={()=>void initialize()}><RefreshCw size={16}/>重新连接</Button></div>;
  if(!user){if(feishuEmbed&&!feishuFallback)return <FeishuLogin config={config} busy={feishuAuth==='authorizing'} error={feishuError} onRetry={()=>void initialize()} onPassword={()=>{setFeishuFallback(true);setFeishuError('');}}/>;return <Login config={config} onLogin={setUser}/>;}
  const admin=user.role==='admin';
  const canCreateConversation=configAllows(config,'conversations.write');
  const canManageAssistants=configAllows(config,'assistants.write');
  const canManageUsers=configAllows(config,'users.write');
  const navigation:Array<{id:View;label:string;icon:JSX.Element}>= [
    {id:'workspace',label:'首页',icon:<House size={18}/>},
    ...(configAllows(config,'conversations.read')?[{id:'sessions' as View,label:'业务会话',icon:<FolderOpen size={18}/>}]:[]),
    ...(canManageAssistants?[{id:'assistants' as View,label:'助手设置',icon:<Settings2 size={18}/>}]:[]),
    ...(configAllows(config,'business_data.read')?[{id:'business-data' as View,label:'业务数据',icon:<Database size={18}/>}]:[]),
    ...(configAllows(config,'knowledge.read')?[{id:'knowledge' as View,label:'知识库',icon:<BookOpen size={18}/>}]:[]),
    ...(configAllows(config,'connections.write')?[{id:'connections' as View,label:'MCP',icon:<Network size={18}/>}]:[]),
    ...(canManageCapabilities(admin,config)?[{id:'capability-settings' as View,label:'能力与来源',icon:<Settings2 size={18}/>}]:[]),
    ...(configAllows(config,'skills.write')?[{id:'skills' as View,label:'Skills',icon:<Sparkles size={18}/>}]:[]),
    ...(configAllows(config,'usage.read')?[{id:'usage' as View,label:'用量记录',icon:<ChartNoAxesCombined size={18}/>}]:[]),
    ...(admin&&configAllows(config,'benchmark.read')?[{id:'benchmark' as View,label:'对标基准',icon:<ChartNoAxesCombined size={18}/>}]:[]),
    ...(admin&&configAllows(config,'approvals.read')?[{id:'approvals' as View,label:'待审批操作',icon:<ShieldCheck size={18}/>}]:[]),
  ];
  const secondaryViews = new Set<View>(['capability-settings','benchmark','approvals']);
  const navItem=(item: typeof navigation[number])=><button key={item.id} aria-label={item.label} title={item.label} className={`nav-item ${(view==='workspace'&&selected?'sessions':view)===item.id?'selected':''}`} aria-current={(view==='workspace'&&selected?'sessions':view)===item.id?'page':undefined} onClick={()=>navigate(item.id)}>{item.icon}<span className="sidebar-label">{item.label}</span></button>;
  return <div ref={sidebarLayout.shellRef} style={sidebarLayout.style} className={`app-shell zhixing-theme${sidebarLayout.collapsed?' sidebar-collapsed':''}${sidebarLayout.resizing?' is-sidebar-resizing':''}`}>{sidebarOpen&&<button className="sidebar-scrim" aria-label="关闭导航" onClick={()=>setSidebarOpen(false)}/>}
    <aside id="workspace-sidebar" className={`sidebar ${sidebarOpen?'open':''}`}><div className="sidebar-brand"><Brand name={config.brand_name} compact/><button type="button" className="sidebar-layout-toggle" aria-label={sidebarLayout.collapsed?'展开侧栏':'收起侧栏'} title={sidebarLayout.collapsed?'展开侧栏':'收起侧栏'} aria-expanded={!sidebarLayout.collapsed} aria-controls="workspace-sidebar" onClick={sidebarLayout.toggle}>{sidebarLayout.collapsed?<PanelLeftOpen size={19}/>:<PanelLeftClose size={19}/>}</button><button className="icon-button mobile-only" aria-label="关闭导航" onClick={()=>setSidebarOpen(false)}><X size={20}/></button></div>{canCreateConversation&&<button aria-label="新建会话" title="新建会话" className="new-conversation" disabled={!assistantsRead.loaded&&!assistants.length} onClick={()=>newChat()}><Plus size={18}/><span className="sidebar-label">新建会话</span><ArrowUpRight size={16}/></button>}<div className="sidebar-scroll"><nav aria-label="主导航">{navigation.filter(item=>!secondaryViews.has(item.id)&&item.id!=='assistants').map(navItem)}<div className="nav-divider"/>{navigation.filter(item=>item.id==='assistants').map(navItem)}{(navigation.some(item=>secondaryViews.has(item.id))||(admin&&canManageUsers))&&<details className="nav-administration" open={secondaryViews.has(view)||view==='users'?true:undefined}><summary title="工作空间管理"><Settings2 size={18}/><span className="sidebar-label">工作空间管理</span></summary>{navigation.filter(item=>secondaryViews.has(item.id)).map(navItem)}{admin&&canManageUsers&&<button aria-label="成员管理" title="成员管理" className={`nav-item ${view==='users'?'selected':''}`} onClick={()=>navigate('users')}><Users size={18}/><span className="sidebar-label">成员管理</span></button>}</details>}</nav><div className="history-heading"><span>最近会话</span><button className="icon-button" aria-label="刷新对话" disabled={conversationsRead.loading} onClick={()=>void refresh()}><RefreshCw size={13}/></button></div><ConversationHistory conversations={conversations} state={conversationsRead} selectedId={view==='workspace'?selected?.id:undefined} onOpen={openConversation} onRetry={()=>void refresh()}/></div><div className="sidebar-bottom"><div className="account"><span className="avatar">{(user.display_name||user.username).slice(0,1)}</span><div><strong>{user.display_name||user.username}</strong><small>{admin?'工作台管理员':'工作台成员'}</small></div><button className="icon-button" aria-label="退出登录" onClick={()=>protectMemory(()=>{void logout();})}><LogOut size={17}/></button></div></div><div className="sidebar-resizer" {...sidebarLayout.separator}/></aside>
    <main className="main"><div className="mobile-bar"><button className="icon-button" onClick={()=>setSidebarOpen(true)} aria-label="打开导航"><Menu size={21}/></button><Brand name={config.brand_name} compact/><span className="mobile-avatar">{user.display_name.slice(0,1)}</span></div>{error&&<div className="global-notice"><ErrorNotice message={error} onDismiss={()=>setError('')}/></div>}
    {view==='workspace'&&(selected?<ChatPage key={`${selected.id}:${memoryEpoch}`} conversation={selected} assistant={assistants.find(a=>a.id===selected.assistant_id)} config={config} models={models} modelsLoading={modelsLoading} initialDraft={initialDraft?.conversationId===selected.id?initialDraft.text:undefined} onNewConversation={canCreateConversation?(selection,draft)=>newChat(assistants.find(a=>a.id===selected.assistant_id),draft,selected.category,selection):undefined} onChanged={refresh} onMemoryDirtyChange={setMemoryDirty} onMemorySavingChange={changeMemorySaving} onArchived={()=>{setSelected(null);showView(configAllows(config,'conversations.read')?'sessions':'workspace');}}/>:<WorkspacePage assistants={assistants} conversations={conversations} assistantsRead={assistantsRead} conversationsRead={conversationsRead} onReload={()=>void refresh()} onAssistant={openAssistant} onConversation={openConversation} onNew={newChat} onNavigate={navigate} canCreate={canCreateConversation} canManage={canManageAssistants} canReadKnowledge={configAllows(config,'knowledge.read')} canManageConnections={configAllows(config,'connections.write')} canReadSessions={configAllows(config,'conversations.read')} onStart={(question,assistant)=>protectMemory(()=>{setNewSession({assistant,question});setSidebarOpen(false);})}/>) }
    {view==='assistant-detail'&&assistantDetail&&<AssistantDetailPage key={assistantDetail.id} assistantId={assistantDetail.id} assistants={assistants} admin={admin} onOpen={openConversation} onNew={newChat} onChanged={refresh} onSnapshot={snapshotAssistant} onDirtyChange={setAssistantDirty} onSavingChange={changeAssistantSaving} requestNavigation={protectMemory} draftEpoch={draftEpoch}/>}
    {view==='sessions'&&<SessionsPage assistants={assistants} onOpen={openConversation} onNew={(assistant,project)=>newChat(assistant,undefined,project)} onChanged={refresh}/>}
    {view==='business-data'&&<BusinessDataPage onConnections={()=>navigate('connections')}/>}
    {view==='connections'&&<ConnectionsPage admin={admin}/> }
    {view==='capability-settings'&&canManageCapabilities(admin,config)&&<CapabilitySettingsPage admin={admin} config={config}/>}
    {view==='assistants'&&<AssistantsPage admin={admin} connectionSetup={connectionSetup} onReturnToConnection={()=>navigate('connections')} onStartAssistant={canCreateConversation?startConfiguredAssistant:undefined} onChanged={refresh} onOpen={openAssistant} onDirtyChange={setAssistantDirty} onSavingChange={changeAssistantSaving} requestNavigation={protectMemory} draftEpoch={draftEpoch}/>}
    {view==='knowledge'&&<KnowledgePage admin={admin} maxUploadBytes={config.upload_max_bytes} readOnly={!configAllows(config,'knowledge.write')} allowedRetrievalModes={config.allowed_retrieval_modes}/>}{view==='skills'&&canManageAssistants&&<SkillsPage admin={admin}/>} {view==='benchmark'&&admin&&configAllows(config,'benchmark.read')&&<BenchmarkPage admin={admin}/>}
    {view==='users'&&admin&&canManageUsers&&<UsersPage currentUser={user}/>} {view==='approvals'&&admin&&configAllows(config,'approvals.read')&&<ApprovalsPage/>} {view==='memory'&&configAllows(config,'memory.write')&&<MemoryPage key={memoryEpoch} assistants={assistants} onDirtyChange={setMemoryDirty} onSavingChange={changeMemorySaving}/>} {view==='usage'&&configAllows(config,'usage.read')&&<UsagePage admin={admin}/>}
    </main>
    {newSession&&<NewSession assistants={assistants} initialAssistant={newSession.assistant} initialSelection={newSession.selection} initialQuestion={newSession.question} initialProject={newSession.project} projects={[...new Set(conversations.map(item=>item.category).filter((value):value is string=>!!value))]} connectionSetup={newSession.connectionSetup} config={config} models={models} modelsLoading={modelsLoading} modelsError={modelsError} onReloadModels={refreshModels} onClose={()=>setNewSession(null)} onCreated={conversation=>{setInitialDraft(newSession.question?{conversationId:conversation.id,text:newSession.question}:null);setNewSession(null);setSelected(conversation);showView('workspace');void refresh();}}/>}
    {pendingNavigation&&<Modal title={pendingKind==='assistant'?'放弃未保存的助手配置':'放弃未保存的记忆修改'} onClose={()=>setPendingNavigation(null)}><div className="modal-body"><p>{pendingKind==='assistant'?'当前助手配置有未保存的修改。请先保存，或明确选择放弃草稿。':'当前记忆有未保存的修改。离开页面会丢弃这些草稿，请先保存或明确选择放弃。'}</p></div><footer className="modal-footer"><Button variant="secondary" onClick={()=>setPendingNavigation(null)}>继续编辑</Button><Button variant="danger" onClick={()=>{const action=pendingNavigation;if(assistantSaving||memorySavingRef.current)return;setPendingNavigation(null);if(pendingKind==='assistant'){setAssistantDirty(false);setDraftEpoch(value=>value+1);}else{setMemoryDirty(false);setMemoryEpoch(value=>value+1);}action();}}>放弃修改并离开</Button></footer></Modal>}
  </div>;
}
