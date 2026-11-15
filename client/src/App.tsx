import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowUpRight, BookOpen, ChartNoAxesCombined, FolderOpen, House, LogOut, Menu, Network, Plus, RefreshCw, Settings2, ShieldCheck, Sparkles, Users, X } from 'lucide-react';
import { api, errorText, post, timeText } from './api';
import { configAllows, type AppConfig, type Assistant, type Conversation, type ModelOption, type User } from './types';
import { Button, ErrorNotice, Loader, Modal } from './components';
import ChatPage from './ChatPage';
import ConnectionsPage from './ConnectionsPage';
import MemoryPage from './MemoryPage';
import AssistantsPage from './AssistantsPage';
import SessionsPage from './SessionsPage';
import SkillsPage from './SkillsPage';
import KnowledgePage from './KnowledgePage';
import { ApprovalsPage, UsersPage, UsagePage } from './AdminPages';
import BenchmarkPage from './BenchmarkPage';
import NewSession from './NewSession';
import AssistantDetailPage from './AssistantDetailPage';
import WorkspacePage from './WorkspacePage';
import CapabilitySettingsPage from './CapabilitySettingsPage';
import { canManageCapabilities } from './capabilitySettings';
import { isFeishuEmbed, requestFeishuCode } from './feishu';
import Brand from './Brand';
import { Login, FeishuLogin } from './AuthPages';
import { connectionSetupContext, connectionTrialSession, type ConnectionSetupContext } from './connectionHandoff';

type View = 'workspace' | 'sessions' | 'assistant-detail' | 'connections' | 'assistants' | 'knowledge' | 'skills' | 'users' | 'usage' | 'memory' | 'benchmark' | 'approvals' | 'capability-settings';
const fallbackConfig: AppConfig = { brand_name: 'Flintworks', upload_max_bytes: 20 * 1024 * 1024, auth_types: ['none', 'bearer'], model_label: '', feishu_h5: { enabled: false, app_id: '', path: '/feishu' } };


export default function App() {
  const feishuEmbed = isFeishuEmbed();
  const [config,setConfig]=useState(fallbackConfig);const [user,setUser]=useState<User|null>(null);const [initializing,setInitializing]=useState(true);const [initialError,setInitialError]=useState('');const [error,setError]=useState('');const [view,setView]=useState<View>('workspace');
  const [feishuAuth,setFeishuAuth]=useState<'idle'|'authorizing'>('idle');const [feishuError,setFeishuError]=useState('');const [feishuFallback,setFeishuFallback]=useState(false);
  const [assistants,setAssistants]=useState<Assistant[]>([]);const [conversations,setConversations]=useState<Conversation[]>([]);const [models,setModels]=useState<ModelOption[]>([]);const [modelsLoading,setModelsLoading]=useState(true);const [modelsError,setModelsError]=useState('');
  const [selected,setSelected]=useState<Conversation|null>(null);const [assistantDetail,setAssistantDetail]=useState<Assistant|null>(null);const [sidebarOpen,setSidebarOpen]=useState(false);const [newSession,setNewSession]=useState<{assistant?:Assistant;question?:string;connectionSetup?:Readonly<ConnectionSetupContext>}|null>(null);
  const [initialDraft,setInitialDraft]=useState<{conversationId:string;text:string}|null>(null);
  const [connectionSetup,setConnectionSetup]=useState<ConnectionSetupContext|null>(null);
  const [memoryDirty,setMemoryDirty]=useState(false);const [memoryEpoch,setMemoryEpoch]=useState(0);const memorySavingRef=useRef(false);const [assistantDirty,setAssistantDirty]=useState(false);const [assistantSaving,setAssistantSaving]=useState(false);const savingRef=useRef(false);const [draftEpoch,setDraftEpoch]=useState(0);const [pendingKind,setPendingKind]=useState<'memory'|'assistant'>('memory');const [pendingNavigation,setPendingNavigation]=useState<(()=>void)|null>(null);
  const changeMemorySaving=useCallback((saving:boolean)=>{memorySavingRef.current=saving;},[]);
  const changeAssistantSaving=useCallback((saving:boolean)=>{savingRef.current=saving;setAssistantSaving(saving);},[]);
  useEffect(()=>{const guard=(event:BeforeUnloadEvent)=>{if(memoryDirty||assistantDirty||savingRef.current||memorySavingRef.current){event.preventDefault();event.returnValue='';}};window.addEventListener('beforeunload',guard);return()=>window.removeEventListener('beforeunload',guard);},[memoryDirty,assistantDirty]);
  const initialize=useCallback(async()=>{setInitializing(true);setInitialError('');setFeishuError('');try{const conf=await api<AppConfig>('/config');setConfig(conf);document.title=conf.brand_name;try{setUser(await api<User>('/auth/me'));}catch(e){if(!(e&&typeof e==='object'&&'status'in e&&e.status===401))throw e;setUser(null);if(feishuEmbed&&!feishuFallback){if(!conf.feishu_h5?.enabled){setFeishuError('服务端还没有开启飞书网页应用，请先完成应用凭据和白名单配置。');}else{setFeishuAuth('authorizing');try{const code=await requestFeishuCode(conf.feishu_h5.app_id);setUser(await post<User>('/feishu/h5/login',{code}));setFeishuAuth('idle');}catch(authError){setFeishuAuth('idle');setFeishuError(errorText(authError));}}}}}catch(e){setInitialError(errorText(e));}finally{setInitializing(false);}},[feishuEmbed,feishuFallback]);
  useEffect(()=>{void initialize();const expired=()=>{setUser(null);setSelected(null);setInitialDraft(null);setConnectionSetup(null);setConversations([]);setAssistants([]);setNewSession(null);setAssistantDetail(null);setPendingNavigation(null);setMemoryDirty(false);setAssistantDirty(false);setAssistantSaving(false);savingRef.current=false;memorySavingRef.current=false;setView('workspace');};window.addEventListener('session-expired',expired);return()=>window.removeEventListener('session-expired',expired);},[initialize]);
  const refresh=useCallback(async()=>{if(!user)return;const results=await Promise.allSettled([api<Assistant[]>('/assistants'),api<Conversation[]>('/conversations')]);if(results[0].status==='fulfilled'){setAssistants(results[0].value);const values=results[0].value;setAssistantDetail(previous=>previous?values.find(a=>a.id===previous.id)||previous:null);}else setError(errorText(results[0].reason));if(results[1].status==='fulfilled')setConversations(results[1].value);else setError(errorText(results[1].reason));},[user]);
  useEffect(()=>{void refresh();},[refresh]);
  const refreshModels=useCallback(async()=>{if(!user)return;setModelsLoading(true);setModelsError('');try{setModels(await api<ModelOption[]>('/models'));}catch(e){setModelsError(errorText(e));}finally{setModelsLoading(false);}},[user]);useEffect(()=>{void refreshModels();},[refreshModels]);
  const protectMemory=(action:()=>void)=>{if(memorySavingRef.current){setError('会话记忆正在保存，请稍候再离开。');return;}if(savingRef.current){setError('助手配置正在保存，请稍候再离开。');return;}if(assistantDirty||(view==='memory'&&memoryDirty)){setPendingKind(assistantDirty?'assistant':'memory');setPendingNavigation(()=>action);}else action();};
  const snapshotAssistant=(assistant:Assistant)=>{setAssistantDetail(assistant);setAssistants(previous=>previous.map(item=>item.id===assistant.id?assistant:item));};
  const showView=(next:View)=>{if(next!=='assistants'&&next!=='connections')setConnectionSetup(null);setView(next);setSidebarOpen(false);setError('');};const navigate=(next:View)=>{if(next==='workspace'){protectMemory(()=>{setSelected(null);showView('workspace');});return;}if(next===view){setSidebarOpen(false);return;}protectMemory(()=>showView(next));};
  const newChat=(assistant?:Assistant)=>protectMemory(()=>{setNewSession({assistant});setSidebarOpen(false);});
  const startConnectionAssistant=(assistant:Assistant,connection:Readonly<ConnectionSetupContext>)=>protectMemory(()=>{setAssistants(previous=>previous.some(item=>item.id===assistant.id)?previous.map(item=>item.id===assistant.id?assistant:item):[...previous,assistant]);setNewSession(connectionTrialSession(assistant,connection));setSidebarOpen(false);});
  const startConfiguredAssistant=(assistant:Assistant)=>{if(!connectionSetup){setError('本次连接信息已失效，请返回连接页重新开始。');return;}startConnectionAssistant(assistant,connectionSetup);};
  const openConversation=(conversation:Conversation)=>protectMemory(()=>{setInitialDraft(null);setSelected(conversation);showView('workspace');});
  const openAssistant=(assistant:Assistant)=>protectMemory(()=>{setAssistantDetail(assistant);showView('assistant-detail');});
  const logout=async()=>{try{await post('/auth/logout');setUser(null);setSelected(null);setInitialDraft(null);setConnectionSetup(null);setAssistants([]);setConversations([]);setView('workspace');}catch(e){setError(errorText(e));}};
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
    ...(configAllows(config,'knowledge.read')?[{id:'knowledge' as View,label:'知识库',icon:<BookOpen size={18}/>}]:[]),
    ...(configAllows(config,'connections.write')?[{id:'connections' as View,label:'业务连接',icon:<Network size={18}/>}]:[]),
    ...(canManageCapabilities(admin,config)?[{id:'capability-settings' as View,label:'能力与来源',icon:<Settings2 size={18}/>}]:[]),
    ...(configAllows(config,'skills.write')?[{id:'skills' as View,label:'Skills',icon:<Sparkles size={18}/>}]:[]),
    ...(configAllows(config,'memory.write')?[{id:'memory' as View,label:'记忆',icon:<BookOpen size={18}/>}]:[]),
    ...(configAllows(config,'usage.read')?[{id:'usage' as View,label:'用量记录',icon:<ChartNoAxesCombined size={18}/>}]:[]),
    ...(admin&&configAllows(config,'benchmark.read')?[{id:'benchmark' as View,label:'对标基准',icon:<ChartNoAxesCombined size={18}/>}]:[]),
    ...(admin&&configAllows(config,'approvals.read')?[{id:'approvals' as View,label:'待审批操作',icon:<ShieldCheck size={18}/>}]:[]),
  ];
  return <div className="app-shell zhixing-theme">{sidebarOpen&&<button className="sidebar-scrim" aria-label="关闭导航" onClick={()=>setSidebarOpen(false)}/>}
    <aside className={`sidebar ${sidebarOpen?'open':''}`}><div className="sidebar-brand"><Brand name={config.brand_name} compact/><button className="icon-button mobile-only" aria-label="关闭导航" onClick={()=>setSidebarOpen(false)}><X size={20}/></button></div>{canCreateConversation&&<button className="new-conversation" onClick={()=>newChat()}><Plus size={18}/>新建会话<ArrowUpRight size={16}/></button>}<nav aria-label="主导航">{navigation.map(item=><button key={item.id} className={`nav-item ${(view==='workspace'&&selected?'sessions':view)===item.id?'selected':''}`} aria-current={(view==='workspace'&&selected?'sessions':view)===item.id?'page':undefined} onClick={()=>navigate(item.id)}>{item.icon}{item.label}</button>)}{admin&&canManageUsers&&<button className={`nav-item ${view==='users'?'selected':''}`} onClick={()=>navigate('users')}><Users size={18}/>成员管理</button>}</nav><div className="history-heading"><span>最近会话</span><button className="icon-button" aria-label="刷新对话" onClick={()=>void refresh()}><RefreshCw size={13}/></button></div><div className="conversation-history">{!conversations.filter(c=>!c.archived).length?<p className="sidebar-hint">从一项具体工作开始，<br/>会话会保存在这里。</p>:conversations.filter(c=>!c.archived).slice(0,30).map(c=><button title={c.title} key={c.id} className={`history-item ${selected?.id===c.id&&view==='workspace'?'selected':''}`} onClick={()=>openConversation(c)}><span>{c.title||'新会话'}</span><small>{timeText(c.updated_at)}</small></button>)}</div><div className="sidebar-bottom"><div className="account"><span className="avatar">{(user.display_name||user.username).slice(0,1)}</span><div><strong>{user.display_name||user.username}</strong><small>{admin?'工作台管理员':'工作台成员'}</small></div><button className="icon-button" aria-label="退出登录" onClick={()=>protectMemory(()=>{void logout();})}><LogOut size={17}/></button></div></div></aside>
    <main className="main"><div className="mobile-bar"><button className="icon-button" onClick={()=>setSidebarOpen(true)} aria-label="打开导航"><Menu size={21}/></button><span>{config.brand_name}</span><span className="mobile-avatar">{user.display_name.slice(0,1)}</span></div>{error&&<div className="global-notice"><ErrorNotice message={error} onDismiss={()=>setError('')}/></div>}
    {view==='workspace'&&(selected?<ChatPage key={selected.id} conversation={selected} assistant={assistants.find(a=>a.id===selected.assistant_id)} config={config} models={models} modelsLoading={modelsLoading} initialDraft={initialDraft?.conversationId===selected.id?initialDraft.text:undefined} onChanged={refresh} onArchived={()=>{setSelected(null);showView(configAllows(config,'conversations.read')?'sessions':'workspace');}}/>:<WorkspacePage assistants={assistants} conversations={conversations} onAssistant={openAssistant} onConversation={openConversation} onNew={newChat} onNavigate={navigate} canCreate={canCreateConversation} canManage={canManageAssistants} canReadKnowledge={configAllows(config,'knowledge.read')} canManageConnections={configAllows(config,'connections.write')} canReadSessions={configAllows(config,'conversations.read')} onStart={(question,assistant)=>protectMemory(()=>{setNewSession({assistant,question});setSidebarOpen(false);})}/>) }
    {view==='assistant-detail'&&assistantDetail&&<AssistantDetailPage key={assistantDetail.id} assistantId={assistantDetail.id} assistants={assistants} admin={admin} onOpen={openConversation} onNew={newChat} onChanged={refresh} onSnapshot={snapshotAssistant} onDirtyChange={setAssistantDirty} onSavingChange={changeAssistantSaving} requestNavigation={protectMemory} draftEpoch={draftEpoch}/>}
    {view==='sessions'&&<SessionsPage assistants={assistants} onOpen={openConversation} onNew={newChat} onChanged={refresh}/>}
    {view==='connections'&&<ConnectionsPage admin={admin} canConfigureAssistant={canManageAssistants} focusConnectionId={connectionSetup?.id} onStartAssistant={canCreateConversation?startConnectionAssistant:undefined} onConfigureAssistant={connection=>{if(canManageAssistants)protectMemory(()=>{setConnectionSetup(connectionSetupContext(connection));showView('assistants');});}}/>}
    {view==='capability-settings'&&canManageCapabilities(admin,config)&&<CapabilitySettingsPage admin={admin} config={config}/>}
    {view==='assistants'&&<AssistantsPage admin={admin} connectionSetup={connectionSetup} onReturnToConnection={()=>navigate('connections')} onStartAssistant={canCreateConversation?startConfiguredAssistant:undefined} onChanged={refresh} onOpen={openAssistant} onDirtyChange={setAssistantDirty} onSavingChange={changeAssistantSaving} requestNavigation={protectMemory} draftEpoch={draftEpoch}/>}
    {view==='knowledge'&&<KnowledgePage admin={admin} maxUploadBytes={config.upload_max_bytes} readOnly={!configAllows(config,'knowledge.write')} allowedRetrievalModes={config.allowed_retrieval_modes}/>}{view==='skills'&&canManageAssistants&&<SkillsPage admin={admin}/>} {view==='benchmark'&&admin&&configAllows(config,'benchmark.read')&&<BenchmarkPage admin={admin}/>}
    {view==='users'&&admin&&canManageUsers&&<UsersPage currentUser={user}/>} {view==='approvals'&&admin&&configAllows(config,'approvals.read')&&<ApprovalsPage/>} {view==='memory'&&configAllows(config,'memory.write')&&<MemoryPage key={memoryEpoch} assistants={assistants} onDirtyChange={setMemoryDirty} onSavingChange={changeMemorySaving}/>} {view==='usage'&&configAllows(config,'usage.read')&&<UsagePage admin={admin}/>}
    </main>
    {newSession&&<NewSession assistants={assistants} initialAssistant={newSession.assistant} initialQuestion={newSession.question} connectionSetup={newSession.connectionSetup} config={config} models={models} modelsLoading={modelsLoading} modelsError={modelsError} onReloadModels={refreshModels} onClose={()=>setNewSession(null)} onCreated={conversation=>{setInitialDraft(newSession.question?{conversationId:conversation.id,text:newSession.question}:null);setNewSession(null);setSelected(conversation);showView('workspace');void refresh();}}/>}
    {pendingNavigation&&<Modal title={pendingKind==='assistant'?'放弃未保存的助手配置':'放弃未保存的记忆修改'} onClose={()=>setPendingNavigation(null)}><div className="modal-body"><p>{pendingKind==='assistant'?'当前助手配置有未保存的修改。请先保存，或明确选择放弃草稿。':'当前记忆有未保存的修改。离开页面会丢弃这些草稿，请先保存或明确选择放弃。'}</p></div><footer className="modal-footer"><Button variant="secondary" onClick={()=>setPendingNavigation(null)}>继续编辑</Button><Button variant="danger" onClick={()=>{const action=pendingNavigation;if(assistantSaving||memorySavingRef.current)return;setPendingNavigation(null);if(pendingKind==='assistant'){setAssistantDirty(false);setDraftEpoch(value=>value+1);}else{setMemoryDirty(false);setMemoryEpoch(value=>value+1);}action();}}>放弃修改并离开</Button></footer></Modal>}
  </div>;
}
