import { useEffect, useRef, useState } from 'react';
import { ArrowRight, RefreshCw } from 'lucide-react';
import { api, errorText, post } from './api';
import { Button, ErrorNotice, Field, Loader, Modal } from './components';
import ModelSelector from './ModelSelector';
import { ModelRoutingControl } from './ModelRouting';
import { modelSelection } from './models';
import { configAllows, type AppConfig, type Assistant, type Conversation, type ModelOption, type ModelSelection } from './types';
import { loadConnectionHandoff, type ConnectionSetupContext } from './connectionHandoff';
import { defaultConversationAssistant } from './WorkspacePage';

export function assistantModelSelection(models:ModelOption[],config:AppConfig,assistant:Assistant,explicit?:ModelSelection):ModelSelection {
  // A model chosen when leaving a protected history must not be replaced by
  // the assistant default, including when the catalog changed in the meantime.
  if (explicit && !models.some(model => model.id === explicit.model_id)) return explicit;
  return modelSelection(models,{default_model_id:assistant.default_model_id||config.default_model_id,default_reasoning_level:assistant.default_reasoning_level||(assistant.default_model_id?undefined:config.default_reasoning_level)},explicit);
}
export const loadNewSessionAssistant=(assistantId:string,signal?:AbortSignal)=>api<Assistant>(`/assistants/${encodeURIComponent(assistantId)}`,{signal});
export const newSessionPayload=(trial:boolean,assistantId:string,selection:ModelSelection,automatic=!trial)=>({assistant_id:assistantId,...selection,auto_model_routing:!trial&&automatic});
export async function createNewSession(trial:boolean,assistantId:string,selection:ModelSelection,automatic=!trial,connectionSetup?:Readonly<ConnectionSetupContext>,project='') {
  if(connectionSetup){const current=await loadConnectionHandoff(connectionSetup.id,assistantId);if(!current.ready)throw new Error(current.message);}
  return post<Conversation>('/conversations',{...newSessionPayload(trial,assistantId,selection,automatic),...(project.trim()?{category:project.trim()}:{})});
}
export default function NewSession({assistants,initialAssistant,initialSelection,initialQuestion,initialProject,projects=[],connectionSetup,config,models,modelsLoading,modelsError,onReloadModels,onClose,onCreated}:{assistants:Assistant[];initialAssistant?:Assistant;initialSelection?:ModelSelection;initialQuestion?:string;initialProject?:string;projects?:string[];connectionSetup?:Readonly<ConnectionSetupContext>;config:AppConfig;models:ModelOption[];modelsLoading:boolean;modelsError:string;onReloadModels:()=>void;onClose:()=>void;onCreated:(conversation:Conversation)=>void}) {
  const [project,setProject]=useState(initialProject||'');
  const [automatic,setAutomatic]=useState(!initialSelection&&config.deployment_mode!=='trial');
  const [chosenAssistantId,setAssistantId]=useState(initialAssistant?.id||'');
  const assistantId=chosenAssistantId || defaultConversationAssistant(assistants)?.id || '';
  const [selectionOverride,setSelectionOverride]=useState<ModelSelection>();
  const [loaded,setLoaded]=useState<Assistant|null>(null);
  const [loading,setLoading]=useState(Boolean(assistantId));
  const [loadError,setLoadError]=useState('');
  const [retry,setRetry]=useState(0);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const submitting=useRef(false);
  useEffect(()=>{
    const controller=new AbortController();setLoaded(null);setLoadError('');
    if(!assistantId){setLoading(false);return()=>controller.abort();}
    setLoading(true);
    loadNewSessionAssistant(assistantId,controller.signal).then(value=>{if(!controller.signal.aborted)setLoaded(value);}).catch(e=>{if(!controller.signal.aborted)setLoadError(errorText(e));}).finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return()=>controller.abort();
  },[assistantId,retry]);
  const currentAssistant=loaded?.id===assistantId?loaded:null;
  const selection=selectionOverride || (currentAssistant
    ? assistantModelSelection(models,config,currentAssistant,assistantId===initialAssistant?.id?initialSelection:undefined)
    : initialSelection || modelSelection(models,config));
  const canCreate=configAllows(config,'conversations.write');
  const trial=config.deployment_mode==='trial';
  const ready=canCreate&&currentAssistant?.enabled&&(!connectionSetup||assistantId===initialAssistant?.id)
    &&!loading&&!loadError&&!modelsLoading&&models.some(model=>model.id===selection.model_id&&model.available);
  const assistantOptions=connectionSetup?(initialAssistant?[initialAssistant]:[]):assistants.filter(a=>a.enabled);
  return <Modal title="新建会话" description={connectionSetup?'本次验证使用所选助手。创建会话后，再输入并发送业务问题。':trial?'选择一个助手。本会话使用你明确选择的基础模型，不启用自动路由、Skills 或附件。':'选择助手和选模方式。自动模式按任务选模型；手动模式固定使用你的选择。'} onClose={()=>{if(!submitting.current)onClose();}}><form onSubmit={async e=>{e.preventDefault();if(!ready||submitting.current)return;submitting.current=true;setBusy(true);setError('');try{onCreated(await createNewSession(trial,assistantId,selection,automatic,connectionSetup,project));}catch(err){setError(errorText(err));}finally{submitting.current=false;setBusy(false);}}}><div className="modal-body"><ErrorNotice message={error}/>{connectionSetup&&<div className="new-session-question"><strong>本次核对连接：{connectionSetup.name}</strong><p>创建前会再次检查此助手的连接与工具权限。</p><small>创建会话不会自动发送问题或授权工具。</small></div>}{initialQuestion&&<div className="new-session-question"><strong>本次业务问题</strong><p>{initialQuestion}</p><small>选择助手后带入会话，确认后发送。</small></div>}{!canCreate&&<ErrorNotice message="当前工作空间未开放会话创建。"/>}<Field label="项目" hint="留空则放在未分类会话中。"><input aria-label="所属项目" list="new-session-projects" value={project} onChange={e=>setProject(e.target.value)} maxLength={100} placeholder="选择或创建项目"/><datalist id="new-session-projects">{projects.map(name=><option key={name} value={name}/>)}</datalist></Field><Field label="使用助手" required><select value={assistantId} onChange={e=>{if(connectionSetup)return;setAssistantId(e.target.value);setSelectionOverride(undefined);setLoaded(null);setLoading(!!e.target.value);setError('');}} disabled={busy||!canCreate||!!connectionSetup} required>{!assistantOptions.length&&<option value="">暂无可用助手</option>}{assistantId&&!assistantOptions.some(a=>a.id===assistantId)&&<option value={assistantId} disabled>原助手已不可用，请重新选择</option>}{assistantOptions.map(a=><option key={a.id} value={a.id}>{a.name}</option>)}</select></Field>{loading&&<Loader text="正在加载助手设置…"/>}{loadError&&<><ErrorNotice message={loadError}/><Button variant="secondary" onClick={()=>setRetry(value=>value+1)}><RefreshCw size={14}/>重新读取助手</Button></>}{loaded&&!loaded.enabled&&<ErrorNotice message={connectionSetup?'这个助手已停用，请返回助手配置检查。':'这个助手已停用，请选择其他助手。'}/>} {!trial&&<ModelRoutingControl automatic={automatic} disabled={busy||!canCreate} onChange={setAutomatic}/>}{(!automatic||trial)&&<ModelSelector models={models} value={selection} disabled={busy||!canCreate||loading||!currentAssistant||!!loadError} loading={modelsLoading} onChange={setSelectionOverride}/>}{automatic&&!trial&&!modelsLoading&&loaded&&!models.some(model=>model.id===selection.model_id&&model.available)&&<ErrorNotice message="会话基础模型不可用。请切换手动选择可用模型，或联系管理员更新默认设置。"/>}{modelsError&&<><ErrorNotice message={modelsError}/><Button variant="secondary" onClick={onReloadModels}>重新加载模型</Button></>}</div><footer className="modal-footer"><Button variant="secondary" disabled={busy} onClick={onClose}>取消</Button><Button type="submit" busy={busy} disabled={!ready}>开始会话<ArrowRight size={16}/></Button></footer></form></Modal>;
}
