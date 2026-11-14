import { useEffect, useRef, useState } from 'react';
import { BookOpen, Check, ChevronDown, Globe, Network, RefreshCw, Settings2, Sparkles } from 'lucide-react';
import { api, ApiError, errorText, patch, post } from './api';
import { Button, ErrorNotice, Field } from './components';
import ApprovalPolicyPanel from './ApprovalPolicyPanel';
import { canEdit, ScopeBadge, ScopeField, type ResourceScope } from './resources';
import type { Assistant, AssistantBinding, AssistantCapabilities, AssistantCapabilityKind, Connection, KnowledgeBase, ModelOption, Skill, User } from './types';

const CAPABILITY_KINDS: AssistantCapabilityKind[] = ['search', 'fetch', 'ocr', 'image_search'];
const CAPABILITY_LABELS: Record<AssistantCapabilityKind, string> = {search: '网页搜索', fetch: '网页读取', ocr: '文档识别', image_search: '图片搜索'};
const CAPABILITY_HINTS: Record<AssistantCapabilityKind, string> = {
  search: '搜索公开网页并登记来源。', fetch: '读取指定公开网页正文。',
  ocr: '把图片或扫描文档上传到管理员配置的识别服务。', image_search: '按关键词或图片查找公开图片。',
};
export function assistantCapabilities(value: Assistant | null): AssistantCapabilities {
  const stored = value?.capabilities || {};
  const legacy = Boolean(value?.web_search_enabled);
  const result: AssistantCapabilities = {};
  for (const kind of CAPABILITY_KINDS) {
    if (stored[kind]) result[kind] = {...stored[kind]};
    else if (!Object.keys(stored).length && (kind === 'search' || kind === 'fetch')) result[kind] = {enabled: legacy};
  }
  return result;
}

export interface AssistantResources { connections:Connection[]; knowledge:KnowledgeBase[]; skills:Skill[]; users:User[]; models:ModelOption[] }
export async function loadAssistantResources(admin:boolean,signal?:AbortSignal):Promise<AssistantResources> {
  const [connections,knowledge,skills,users,models]=await Promise.all([api<Connection[]>('/connections',{signal}),api<KnowledgeBase[]>('/knowledge-bases',{signal}),api<Skill[]>('/skills',{signal}),admin?api<User[]>('/users',{signal}):Promise.resolve([]),api<ModelOption[]>('/models',{signal})]);
  return {connections,knowledge,skills,users,models};
}
export interface AssistantDraft {
  name:string;description:string;instructions:string;scope:ResourceScope;enabled:boolean;
  connections:AssistantBinding[];knowledge_base_ids:string[];skill_ids:string[];web_search_enabled:boolean;capabilities:AssistantCapabilities;
  default_model_id:string|null;default_reasoning_level:string|null;access:{all_members:boolean;user_ids:string[]};
}
export function assistantDraft(value:Assistant|null,resources:AssistantResources):AssistantDraft {
  return {name:value?.name||'',description:value?.description||'',instructions:value?.instructions||'',scope:value?.scope||'private',enabled:value?.enabled??true,
    connections:value?.connections.map(binding=>({...binding,allowed_tools:[...binding.allowed_tools],auto_approved_tools:[...binding.auto_approved_tools]}))||[],
    knowledge_base_ids:value?[...(value.knowledge_base_ids||[])]:resources.knowledge.filter(k=>k.enabled&&k.default_for_new_assistants).map(k=>k.id),
    skill_ids:[...(value?.skill_ids||[])],web_search_enabled:value?.web_search_enabled||false,capabilities:assistantCapabilities(value),
    default_model_id:value?.default_model_id||null,default_reasoning_level:value?.default_reasoning_level||null,
    access:{all_members:value?.access.all_members??true,user_ids:[...(value?.access.user_ids||[])]}};
}
export function assistantChanges(baseline:AssistantDraft,draft:AssistantDraft):Partial<AssistantDraft> {
  const normalized={...draft,name:draft.name.trim()};
  return Object.fromEntries((Object.keys(normalized) as Array<keyof AssistantDraft>).filter(key=>JSON.stringify(normalized[key])!==JSON.stringify(baseline[key])).map(key=>[key,normalized[key]]));
}
export async function saveAssistantConfiguration(id:string|null,revision:number|undefined,baseline:AssistantDraft,draft:AssistantDraft):Promise<Assistant> {
  return id?patch<Assistant>(`/assistants/${encodeURIComponent(id)}`,{...assistantChanges(baseline,draft),expected_revision:revision}):post<Assistant>('/assistants',{...draft,name:draft.name.trim()});
}
export function toggleBindingTool(bindings:AssistantBinding[],id:string,name:string,checked:boolean,automatic=false) {
  return bindings.map(binding=>binding.connection_id!==id?binding:automatic?{...binding,auto_approved_tools:checked?[...new Set([...binding.auto_approved_tools,name])]:binding.auto_approved_tools.filter(tool=>tool!==name)}:{...binding,allowed_tools:checked?[...new Set([...binding.allowed_tools,name])]:binding.allowed_tools.filter(tool=>tool!==name),auto_approved_tools:checked?binding.auto_approved_tools:binding.auto_approved_tools.filter(tool=>tool!==name)});
}
const toggleId=(ids:string[],id:string,checked:boolean)=>checked?[...new Set([...ids,id])]:ids.filter(value=>value!==id);

export function MissingBinding({label,onRemove,disabled}:{label:string;onRemove:()=>void;disabled:boolean}) {return <div className="missing-binding"><div><strong>{label}</strong><small>当前无法读取此绑定。已保留原配置，可明确解除后再保存。</small></div><Button variant="ghost" disabled={disabled} onClick={onRemove}>解除绑定</Button></div>;}

export default function AssistantConfiguration({value,resources,admin,onSaved,onDirtyChange,onSavingChange,onReload,onCancel,inline=false}:{value:Assistant|null;resources:AssistantResources;admin:boolean;onSaved:(saved:Assistant)=>void;onDirtyChange:(dirty:boolean)=>void;onSavingChange:(saving:boolean)=>void;onReload:()=>void;onCancel?:()=>void;inline?:boolean}) {
  const editable=!value||canEdit(value,admin);const [baseline,setBaseline]=useState(()=>assistantDraft(value,resources));const [baselineRevision,setBaselineRevision]=useState(value?.revision);const [draft,setDraft]=useState(()=>assistantDraft(value,resources));
  const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [conflict,setConflict]=useState(false);const submitting=useRef(false);const mounted=useRef(true);
  const callbacks=useRef({onDirtyChange,onSavingChange});callbacks.current={onDirtyChange,onSavingChange};
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;callbacks.current.onDirtyChange(false);callbacks.current.onSavingChange(false);};},[]);
  const dirty=Object.keys(assistantChanges(baseline,draft)).length>0;
  useEffect(()=>{callbacks.current.onDirtyChange(dirty);},[dirty]);
  const update=<K extends keyof AssistantDraft>(key:K,next:AssistantDraft[K])=>{if(!editable||submitting.current)return;setDraft(previous=>({...previous,[key]:next}));};
  const capabilityGrant=(kind:AssistantCapabilityKind)=>draft.capabilities[kind]||{enabled:false};
  const setCapability=(kind:AssistantCapabilityKind,patch:Partial<NonNullable<AssistantDraft['capabilities'][AssistantCapabilityKind]>>)=>update('capabilities',{...draft.capabilities,[kind]:{...capabilityGrant(kind),...patch}});
  const compatible=(item:{scope?:string})=>draft.scope==='private'||item.scope!=='private';
  const visibleConnections=resources.connections.filter(item=>compatible(item)&&(item.enabled||draft.connections.some(binding=>binding.connection_id===item.id)));const visibleKnowledge=resources.knowledge.filter(item=>compatible(item)&&(item.enabled||draft.knowledge_base_ids.includes(item.id)));const visibleSkills=resources.skills.filter(item=>compatible(item)&&(item.enabled||draft.skill_ids.includes(item.id)));
  const selectedModel=resources.models.find(m=>m.id===draft.default_model_id);
  const save=async()=>{
    if(!editable||submitting.current||!draft.name.trim()||(value&&!dirty))return;
    submitting.current=true;setBusy(true);callbacks.current.onSavingChange(true);setError('');setConflict(false);
    try{
      const saved=await saveAssistantConfiguration(value?.id||null,baselineRevision,baseline,draft);
      if(!mounted.current)return;
      const next=assistantDraft(saved,resources);setBaseline(next);setBaselineRevision(saved.revision);setDraft(next);callbacks.current.onDirtyChange(false);onSaved(saved);
    }catch(e){if(mounted.current){setError(errorText(e));setConflict(e instanceof ApiError&&e.code==='ASSISTANT_CONFLICT');}}
    finally{submitting.current=false;if(mounted.current){setBusy(false);callbacks.current.onSavingChange(false);}}
  };
  const saveButton=<Button type="submit" busy={busy} disabled={!editable||!draft.name.trim()||!!value&&!dirty}><Check size={16}/>{busy?'保存中…':'保存助手'}</Button>;
  return <form className={`assistant-configuration ${inline?'inline-configuration':''}`} onSubmit={e=>{e.preventDefault();void save();}}>
    <div className="configuration-savebar"><div><strong>{editable?'能力配置':'助手能力'}</strong><small>{busy?'正在保存配置，请稍候。':dirty?'有未保存的修改':editable?'配置会用于后续任务，已有会话记录保留。':'你可以使用此助手，配置由拥有编辑权限的成员维护。'}</small></div>{editable&&saveButton}</div>
    <ErrorNotice message={error}/>{conflict&&<div className="configuration-conflict"><p>你的草稿仍保留。重新加载会读取最新配置，请先核对需要保留的修改。</p><Button variant="secondary" disabled={busy} onClick={onReload}><RefreshCw size={14}/>重新加载配置</Button></div>}
    <fieldset className="plain-fieldset" disabled={!editable||busy}>
      <section className="configuration-section" aria-label="数据连接与工具"><header><span className="capability-section-icon"><Network size={21}/></span><div><h2>数据连接与工具</h2><p>选择数据连接、允许调用的工具和审批方式。</p></div><span className="configuration-count">{draft.connections.length} 个连接</span></header>
        {!visibleConnections.length&&<p className="section-help">还没有可绑定的业务连接。可在“业务连接”添加或导入 MCP 后返回配置。</p>}
        {visibleConnections.map(connection=>{const binding=draft.connections.find(b=>b.connection_id===connection.id);const missingTools=binding?.allowed_tools.filter(name=>!connection.tools.some(t=>t.name===name))||[];return <div className="binding-group" key={connection.id}><label className="checkbox-row binding-heading"><input type="checkbox" checked={!!binding} disabled={!connection.enabled&&!binding} onChange={e=>update('connections',e.target.checked?[...draft.connections,{connection_id:connection.id,allowed_tools:[],auto_approved_tools:[]}]:draft.connections.filter(b=>b.connection_id!==connection.id))}/><span>{connection.name}<small>{connection.enabled?`${connection.tools.length} 个已发现工具`:'连接已停用，原绑定保留'}</small></span><ScopeBadge scope={connection.scope}/>{binding&&<ChevronDown size={16}/>}</label>{binding&&<details className="binding-permissions"><summary>工具权限 · 已启用 {binding.allowed_tools.length}/{connection.tools.length}{binding.allowed_tools.length===0&&<span>请展开选择工具</span>}<ChevronDown size={14}/></summary><div className="binding-tools">{connection.tools.length>0&&<><div className="tool-selection-header"><label className="checkbox-row"><input type="checkbox" checked={connection.tools.every(t=>binding.allowed_tools.includes(t.name))} onChange={e=>{const discovered=connection.tools.map(t=>t.name);update('connections',draft.connections.map(b=>b.connection_id!==connection.id?b:{...b,allowed_tools:e.target.checked?[...new Set([...b.allowed_tools,...discovered])]:b.allowed_tools.filter(name=>!discovered.includes(name)),auto_approved_tools:e.target.checked?b.auto_approved_tools:b.auto_approved_tools.filter(name=>!discovered.includes(name))}));}}/>选择已发现工具</label><span>免确认执行</span></div>{connection.tools.map(tool=><div className="tool-selection-row" key={tool.name}><label className="checkbox-row"><input type="checkbox" checked={binding.allowed_tools.includes(tool.name)} onChange={e=>update('connections',toggleBindingTool(draft.connections,connection.id,tool.name,e.target.checked))}/><span>{tool.name}<small>{tool.description||'暂无说明'}</small></span></label><label className="auto-approval-toggle"><input type="checkbox" aria-label={`${connection.name} · ${tool.name} 免确认执行`} checked={binding.auto_approved_tools.includes(tool.name)} disabled={!binding.allowed_tools.includes(tool.name)} onChange={e=>update('connections',toggleBindingTool(draft.connections,connection.id,tool.name,e.target.checked,true))}/><span/></label></div>)}</>}{!connection.tools.length&&<p className="section-help">尚未发现工具，请在业务连接中测试连接。原有工具选择不会自动移除。</p>}{missingTools.map(name=><MissingBinding key={name} label={`未发现的工具：${name}`} disabled={!editable||busy} onRemove={()=>update('connections',toggleBindingTool(draft.connections,connection.id,name,false))}/>)}</div></details>}</div>;})}
        {draft.connections.filter(binding=>!visibleConnections.some(c=>c.id===binding.connection_id)).map((binding,index)=><MissingBinding key={binding.connection_id} label={`无法访问的数据连接 ${index+1}`} disabled={!editable||busy} onRemove={()=>update('connections',draft.connections.filter(b=>b.connection_id!==binding.connection_id))}/>)}
      </section>
      <div className="configuration-duo">
        <section className="configuration-section" aria-label="知识库"><header><span className="capability-section-icon"><BookOpen size={21}/></span><div><h2>知识库</h2><p>选择助手可以查询的资料集合。</p></div><span className="configuration-count">{draft.knowledge_base_ids.length}</span></header><div className="configuration-options">{visibleKnowledge.map(k=><label className="checkbox-row resource-option" key={k.id}><input type="checkbox" checked={draft.knowledge_base_ids.includes(k.id)} disabled={!k.enabled&&!draft.knowledge_base_ids.includes(k.id)} onChange={e=>update('knowledge_base_ids',toggleId(draft.knowledge_base_ids,k.id,e.target.checked))}/><span>{k.name}<small>{k.enabled?`${k.document_count} 份文件`:'已停用，原绑定保留'}{k.default_for_new_assistants?' · 公司默认资料':''}</small></span><ScopeBadge scope={k.scope}/></label>)}{!visibleKnowledge.length&&<p className="section-help">还没有可绑定的知识库。创建并上传资料后可在这里选择。</p>}{draft.knowledge_base_ids.filter(id=>!visibleKnowledge.some(k=>k.id===id)).map((id,index)=><MissingBinding key={id} label={`无法访问的知识库 ${index+1}`} disabled={!editable||busy} onRemove={()=>update('knowledge_base_ids',draft.knowledge_base_ids.filter(item=>item!==id))}/>)}</div></section>
        <section className="configuration-section" aria-label="Skills"><header><span className="capability-section-icon"><Sparkles size={21}/></span><div><h2>Skills</h2><p>绑定工作方法，在会话中用 @ 调用。</p></div><span className="configuration-count">{draft.skill_ids.length}</span></header><div className="configuration-options">{visibleSkills.map(skill=><label className="checkbox-row resource-option" key={skill.id}><input type="checkbox" checked={draft.skill_ids.includes(skill.id)} disabled={!skill.enabled&&!draft.skill_ids.includes(skill.id)} onChange={e=>update('skill_ids',toggleId(draft.skill_ids,skill.id,e.target.checked))}/><span>{skill.name}<small>v{skill.revision} · {skill.enabled?skill.description:'已停用，原绑定保留'}</small></span><ScopeBadge scope={skill.scope}/></label>)}{!visibleSkills.length&&<p className="section-help">还没有可绑定的 Skill。可在 Skills 页创建或导入。</p>}{draft.skill_ids.filter(id=>!visibleSkills.some(skill=>skill.id===id)).map((id,index)=><MissingBinding key={id} label={`无法访问的 Skill ${index+1}`} disabled={!editable||busy} onRemove={()=>update('skill_ids',draft.skill_ids.filter(item=>item!==id))}/>)}</div></section>
      </div>
      <section className="configuration-section web-capability" aria-label="外部能力"><header><span className="capability-section-icon"><Globe size={21}/></span><div><h2>外部能力</h2><p>逐项授权网页搜索、网页读取、文档识别和图片搜索。最终权限还受管理员全局配置、部署模式和本次任务快照约束。</p></div><span className="configuration-count">{CAPABILITY_KINDS.filter(kind=>capabilityGrant(kind).enabled).length} / 4 已启用</span></header>
        <div className="capability-grant-grid">{CAPABILITY_KINDS.map(kind=>{const grant=capabilityGrant(kind);return <div className="capability-grant" key={kind}>
          <label className="checkbox-row"><input type="checkbox" aria-label={`启用 ${CAPABILITY_LABELS[kind]}`} checked={grant.enabled} onChange={e=>setCapability(kind,{enabled:e.target.checked})}/><span>{CAPABILITY_LABELS[kind]}<small>{CAPABILITY_HINTS[kind]}</small></span></label>
          {admin&&grant.enabled&&<label className="checkbox-row auto-approval-toggle"><input type="checkbox" aria-label={`${CAPABILITY_LABELS[kind]} 免确认执行`} checked={Boolean(grant.auto_approved)} onChange={e=>setCapability(kind,{auto_approved:e.target.checked})}/><span>免确认执行（管理员）</span></label>}
          {grant.enabled&&<details><summary>此助手的外发、域名与额度</summary>
            <p className="section-help">只能在管理员全局范围内进一步收窄，保存后显示实际生效的权限。变更后需重新发起任务。</p>
            {([['query','查询词外发'],['url','网页地址外发'],['upload','文件上传外发']] as const).map(([key,label])=><Field key={key} label={label}><select aria-label={`${CAPABILITY_LABELS[kind]} ${label}`} value={grant.egress?.[key]??''} onChange={e=>setCapability(kind,{egress:{...grant.egress,[key]:e.target.value||undefined}})}>
              <option value="">遵循全局策略</option><option value="deny">禁止</option><option value="approval">每次人工确认</option><option value="allow">允许（受全局上限约束）</option>
            </select></Field>)}
            <Field label="来源域名范围"><select aria-label={`${CAPABILITY_LABELS[kind]} 域名范围`} value={grant.domains?.mode??''} onChange={e=>setCapability(kind,{domains:e.target.value?{mode:e.target.value as 'public'|'allowlist',hosts:grant.domains?.hosts||[]}:undefined})}>
              <option value="">遵循全局策略</option><option value="public">公开域名（仍受全局限制）</option><option value="allowlist">指定域名</option>
            </select></Field>
            {grant.domains?.mode==='allowlist'&&<Field label="允许的主机名" hint="每行一个精确主机名，不包含网址路径。"><textarea rows={3} aria-label={`${CAPABILITY_LABELS[kind]} 允许的主机名`} value={grant.domains.hosts.join('\n')} onChange={e=>setCapability(kind,{domains:{mode:'allowlist',hosts:e.target.value.split(/[\s,]+/).filter(Boolean)}})}/></Field>}
            {([['calls_per_run','每次任务调用上限',10000],['calls_per_day','每日调用上限',100000],['max_upload_bytes','单文件上传上限（字节）',209715200]] as const).map(([key,label,max])=><Field key={key} label={label} hint="留空遵循全局上限。"><input type="number" aria-label={`${CAPABILITY_LABELS[kind]} ${label}`} min={key==='max_upload_bytes'?0:1} max={max} value={grant.limits?.[key]??''} onChange={e=>setCapability(kind,{limits:{...grant.limits,[key]:e.target.value===''?undefined:Number(e.target.value)}})}/></Field>)}
          </details>}
        </div>;})}</div>
      </section>
      <details className="configuration-secondary" open={!value}><summary><Settings2 size={17}/><span>基本信息、默认模型与访问权限</span><ChevronDown size={16}/></summary><div className="configuration-secondary-body"><ScopeField value={draft.scope} onChange={next=>update('scope',next)} admin={admin} disabled={!!value}/><div className="two-col"><Field label="助手名称" required><input value={draft.name} maxLength={100} required onChange={e=>update('name',e.target.value)}/></Field><Field label="一句话说明"><input value={draft.description} maxLength={1000} onChange={e=>update('description',e.target.value)}/></Field></div><Field label="业务规则" hint="描述工作目标、事实依据、步骤和结果要求。"><textarea rows={5} maxLength={20000} value={draft.instructions} onChange={e=>update('instructions',e.target.value)}/></Field><div className="two-col"><Field label="默认模型"><select value={draft.default_model_id||''} onChange={e=>{const id=e.target.value||null;setDraft(previous=>({...previous,default_model_id:id,default_reasoning_level:resources.models.find(m=>m.id===id)?.default_reasoning_level||null}));}}><option value="">使用工作台默认设置</option>{draft.default_model_id&&!selectedModel&&<option value={draft.default_model_id}>当前默认模型（暂不可用）</option>}{resources.models.map(model=><option key={model.id} value={model.id} disabled={!model.available}>{model.label}</option>)}</select></Field><Field label="默认推理级别"><select value={draft.default_reasoning_level||''} disabled={!draft.default_model_id} onChange={e=>update('default_reasoning_level',e.target.value||null)}><option value="">模型默认</option>{draft.default_reasoning_level&&!selectedModel?.reasoning_levels.some(level=>level.id===draft.default_reasoning_level)&&<option value={draft.default_reasoning_level}>当前级别（暂不可用）</option>}{selectedModel?.reasoning_levels.map(level=><option key={level.id} value={level.id}>{level.label}</option>)}</select></Field></div>{draft.scope==='team'&&<><label className="checkbox-row"><input type="checkbox" checked={draft.access.all_members} onChange={e=>update('access',{all_members:e.target.checked,user_ids:e.target.checked?[]:draft.access.user_ids})}/>允许所有成员使用</label>{!draft.access.all_members&&<div className="member-picker">{resources.users.filter(user=>user.enabled||draft.access.user_ids.includes(user.id)).map(user=><label className="checkbox-row" key={user.id}><input type="checkbox" checked={draft.access.user_ids.includes(user.id)} onChange={e=>update('access',{...draft.access,user_ids:toggleId(draft.access.user_ids,user.id,e.target.checked)})}/>{user.display_name||user.username}{!user.enabled&&<small>已停用</small>}</label>)}{draft.access.user_ids.filter(id=>!resources.users.some(user=>user.id===id)).length>0&&<p className="section-help">另有无法读取的成员授权，原授权已保留。</p>}</div>}</>}<label className="checkbox-row separated"><input type="checkbox" checked={draft.enabled} onChange={e=>update('enabled',e.target.checked)}/>启用助手</label></div></details>
    </fieldset>
    {value?.id&&admin&&<ApprovalPolicyPanel assistantId={value.id} admin={admin}/>}
    {editable&&<footer className="configuration-footer"><span>{dirty?'有未保存的修改':'配置已保存'}</span>{onCancel&&<Button variant="secondary" disabled={busy} onClick={onCancel}>取消</Button>}{saveButton}</footer>}
  </form>;
}
