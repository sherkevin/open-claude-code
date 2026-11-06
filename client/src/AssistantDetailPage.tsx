import { useState } from 'react';
import { Bot, MessageSquare, Plus, RefreshCw, Settings2 } from 'lucide-react';
import AssistantConfiguration from './AssistantConfiguration';
import useAssistantConfiguration from './useAssistantConfiguration';
import SessionsPage from './SessionsPage';
import { Button, ErrorNotice, Loader, PageHeader, SuccessNotice } from './components';
import { canEdit, ScopeBadge } from './resources';
import type { Assistant, Conversation } from './types';

export interface AssistantDraftNavigation {
  onDirtyChange:(dirty:boolean)=>void;onSavingChange:(saving:boolean)=>void;
  requestNavigation:(action:()=>void)=>void;draftEpoch:number;
}
export default function AssistantDetailPage({assistantId,assistants,admin,onOpen,onNew,onChanged,onSnapshot,onDirtyChange,onSavingChange,requestNavigation,draftEpoch}:{assistantId:string;assistants:Assistant[];admin:boolean;onOpen:(conversation:Conversation)=>void;onNew:(assistant?:Assistant)=>void;onChanged:()=>void;onSnapshot:(assistant:Assistant)=>void}&AssistantDraftNavigation) {
  const {snapshot,loading,error,generation,reload,saved}=useAssistantConfiguration(assistantId,admin);
  const [tab,setTab]=useState<'capabilities'|'sessions'>('capabilities');const [success,setSuccess]=useState('');const [saving,setSaving]=useState(false);
  if(loading)return <div className="page"><Loader text="正在读取助手与可用能力…"/></div>;
  if(error||!snapshot?.assistant)return <div className="page"><PageHeader title="助手详情" eyebrow="配置与会话" description="读取最新的助手能力配置与会话。"/><ErrorNotice message={error||'无法读取助手配置。'}/><Button variant="secondary" onClick={reload}><RefreshCw size={16}/>重新加载</Button></div>;
  const assistant=snapshot.assistant;const editable=canEdit(assistant,admin);
  return <div className="page assistant-detail-page"><PageHeader eyebrow="助手工作空间" title={assistant.name} description={assistant.description||'配置助手的知识、数据与工作方法，分别管理每项工作的会话。'} actions={<Button disabled={saving||!assistant.enabled} onClick={()=>onNew(assistant)}><Plus size={17}/>新建会话</Button>}/>
    <div className="assistant-detail-meta"><ScopeBadge scope={assistant.scope}/><span className={`badge ${assistant.enabled?'green':''}`}>{assistant.enabled?'已启用':'已停用'}</span><span>{editable?'可以编辑配置':'只读配置'} · v{assistant.revision}</span></div>
    <div className="assistant-detail-tabs" role="tablist" aria-label="助手详情"><button role="tab" aria-selected={tab==='capabilities'} className={tab==='capabilities'?'selected':''} disabled={saving} onClick={()=>{if(tab!=='capabilities')requestNavigation(()=>{setTab('capabilities');setSuccess('');});}}><Settings2 size={17}/>能力配置</button><button role="tab" aria-selected={tab==='sessions'} className={tab==='sessions'?'selected':''} disabled={saving} onClick={()=>{if(tab!=='sessions')requestNavigation(()=>{setTab('sessions');setSuccess('');});}}><MessageSquare size={17}/>会话</button></div>
    {tab==='capabilities'?<div role="tabpanel" aria-label="能力配置"><SuccessNotice message={success}/><AssistantConfiguration key={`${assistantId}-${generation}-${draftEpoch}`} value={assistant} resources={snapshot.resources} admin={admin} inline onDirtyChange={dirty=>{onDirtyChange(dirty);if(dirty)setSuccess('');}} onSavingChange={value=>{setSaving(value);onSavingChange(value);}} onSaved={value=>{saved(value);onSnapshot(value);setSuccess('助手配置已保存。');onChanged();}} onReload={()=>requestNavigation(()=>{setSuccess('');reload();})}/></div>:<div role="tabpanel" aria-label="会话"><SessionsPage embedded assistants={assistants} assistant={assistant} onOpen={onOpen} onNew={onNew} onChanged={onChanged}/></div>}
    {!editable&&tab==='capabilities'&&<p className="section-help"><Bot size={14}/> 你可以新建会话使用已配置的能力。</p>}
  </div>;
}
