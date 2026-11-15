import { useEffect, useRef, useState } from 'react';
import { ArrowRight, RefreshCw } from 'lucide-react';
import { api, errorText, post } from './api';
import { Button, ErrorNotice, Field, Loader, Modal } from './components';
import ModelSelector from './ModelSelector';
import { ModelRoutingControl } from './ModelRouting';
import { modelSelection } from './models';
import { configAllows, type AppConfig, type Assistant, type Conversation, type ModelOption, type ModelSelection } from './types';

export function assistantModelSelection(models:ModelOption[],config:AppConfig,assistant:Assistant):ModelSelection {
  return modelSelection(models,{default_model_id:assistant.default_model_id||config.default_model_id,default_reasoning_level:assistant.default_reasoning_level||(assistant.default_model_id?undefined:config.default_reasoning_level)});
}
export const loadNewSessionAssistant=(assistantId:string,signal?:AbortSignal)=>api<Assistant>(`/assistants/${encodeURIComponent(assistantId)}`,{signal});
export const newSessionPayload=(trial:boolean,assistantId:string,selection:ModelSelection,automatic=!trial)=>({assistant_id:assistantId,...selection,auto_model_routing:!trial&&automatic});
export const createNewSession=(trial:boolean,assistantId:string,selection:ModelSelection,automatic=!trial)=>post<Conversation>('/conversations',newSessionPayload(trial,assistantId,selection,automatic));
export default function NewSession({assistants,initialAssistant,initialQuestion,config,models,modelsLoading,modelsError,onReloadModels,onClose,onCreated}:{assistants:Assistant[];initialAssistant?:Assistant;initialQuestion?:string;config:AppConfig;models:ModelOption[];modelsLoading:boolean;modelsError:string;onReloadModels:()=>void;onClose:()=>void;onCreated:(conversation:Conversation)=>void}) {
  const [automatic,setAutomatic]=useState(config.deployment_mode!=='trial');
  const [assistantId,setAssistantId]=useState(initialAssistant?.id||'');const [selection,setSelection]=useState<ModelSelection>(()=>modelSelection(models,config));const [loaded,setLoaded]=useState<Assistant|null>(null);const [loading,setLoading]=useState(!!initialAssistant);const [loadError,setLoadError]=useState('');const [retry,setRetry]=useState(0);const [busy,setBusy]=useState(false);const [error,setError]=useState('');const submitting=useRef(false);const applied=useRef<Assistant|null>(null);
  useEffect(()=>{
    const controller=new AbortController();setLoaded(null);setLoadError('');applied.current=null;
    if(!assistantId){setLoading(false);return()=>controller.abort();}
    setLoading(true);
    loadNewSessionAssistant(assistantId,controller.signal).then(value=>{if(!controller.signal.aborted)setLoaded(value);}).catch(e=>{if(!controller.signal.aborted)setLoadError(errorText(e));}).finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return()=>controller.abort();
  },[assistantId,retry]);
  useEffect(()=>{if(loaded?.id===assistantId&&models.length&&applied.current!==loaded){setSelection(assistantModelSelection(models,config,loaded));applied.current=loaded;}},[loaded,assistantId,models,config]);
  const canCreate=configAllows(config,'conversations.write');
  const trial=config.deployment_mode==='trial';
  const ready=canCreate&&!!loaded&&loaded.id===assistantId&&applied.current===loaded&&loaded.enabled&&!loading&&!loadError&&!modelsLoading&&models.some(m=>m.id===selection.model_id&&m.available);
  return <Modal title="新建会话" description={trial?'选择一个助手。本会话使用你明确选择的基础模型，不启用自动路由、Skills 或附件。':'选择助手和选模方式。自动模式按任务选模型；手动模式固定使用你的选择。'} onClose={()=>{if(!submitting.current)onClose();}}><form onSubmit={async e=>{e.preventDefault();if(!ready||submitting.current)return;submitting.current=true;setBusy(true);setError('');try{onCreated(await createNewSession(trial,assistantId,selection,automatic));}catch(err){setError(errorText(err));}finally{submitting.current=false;setBusy(false);}}}><div className="modal-body"><ErrorNotice message={error}/>{initialQuestion&&<div className="new-session-question"><strong>本次业务问题</strong><p>{initialQuestion}</p><small>选择助手后带入会话，确认后发送。</small></div>}{!canCreate&&<ErrorNotice message="当前受控试用未开放会话创建。"/>}<Field label="使用助手" required><select value={assistantId} onChange={e=>{setAssistantId(e.target.value);setLoaded(null);setLoading(!!e.target.value);setError('');}} disabled={busy||!canCreate} required><option value="">请选择助手</option>{assistants.filter(a=>a.enabled).map(a=><option key={a.id} value={a.id}>{a.name}</option>)}</select></Field>{loading&&<Loader text="正在读取助手的最新默认设置…"/>}{loadError&&<><ErrorNotice message={loadError}/><Button variant="secondary" onClick={()=>setRetry(value=>value+1)}><RefreshCw size={14}/>重新读取助手</Button></>}{loaded&&!loaded.enabled&&<ErrorNotice message="这个助手已停用，请选择其他助手。"/>}{!trial&&<ModelRoutingControl automatic={automatic} disabled={busy||!canCreate} onChange={setAutomatic}/>}{(trial||!automatic)&&<ModelSelector models={models} value={selection} disabled={busy||!canCreate||loading||!loaded||!!loadError} loading={modelsLoading} onChange={value=>{setAutomatic(false);setSelection(value);}}/>}{automatic&&!trial&&!modelsLoading&&loaded&&!models.some(model=>model.id===selection.model_id&&model.available)&&<ErrorNotice message="会话基础模型不可用。请切换手动选择可用模型，或联系管理员更新默认设置。"/>}{modelsError&&<><ErrorNotice message={modelsError}/><Button variant="secondary" onClick={onReloadModels}>重新加载模型</Button></>}</div><footer className="modal-footer"><Button variant="secondary" disabled={busy} onClick={onClose}>取消</Button><Button type="submit" busy={busy} disabled={!ready}>开始会话<ArrowRight size={16}/></Button></footer></form></Modal>;
}
