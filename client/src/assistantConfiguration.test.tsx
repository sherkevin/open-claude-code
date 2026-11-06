import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import AssistantConfiguration, { assistantChanges, assistantDraft, loadAssistantResources, saveAssistantConfiguration, toggleBindingTool, type AssistantResources } from './AssistantConfiguration';
import { assistantModelSelection } from './NewSession';
import type { AppConfig, Assistant, Connection, KnowledgeBase, ModelOption, Skill } from './types';

const models:ModelOption[]=[{id:'dano-v2.1',label:'Dano-v2.1',available:true,reasoning_levels:['light','high','max'].map(id=>({id,label:id})),default_reasoning_level:'high'},{id:'glm-5.3',label:'GLM-5.3',available:true,reasoning_levels:[{id:'default',label:'默认'}],default_reasoning_level:'default'}];
const connection:Connection={id:'connection',name:'生产数据',transport:'http',url:'https://mcp.example',auth_type:'none',credential_present:false,enabled:true,status:'connected',tools:[{name:'query',description:'读取数据',inputSchema:{}}],last_error:null,last_tested_at:null,revision:1,scope:'team'};
const knowledge:KnowledgeBase={id:'knowledge',name:'公司资料',description:'',enabled:true,default_for_new_assistants:true,document_count:1,scope:'team'};
const skill:Skill={id:'skill',name:'quality-review',description:'质量检查',instructions:'检查来源',enabled:true,revision:1,required_tools:[],scope:'team'};
const resources:AssistantResources={connections:[connection],knowledge:[knowledge],skills:[skill],models,users:[]};
const assistant:Assistant={id:'assistant',name:'工业助手',description:'',instructions:'只使用可核对的来源',scope:'private',can_edit:true,enabled:true,access:{all_members:false,user_ids:['hidden-user']},connections:[{connection_id:'connection',allowed_tools:['query','missing-tool'],auto_approved_tools:['missing-tool']},{connection_id:'hidden-connection',allowed_tools:['read'],auto_approved_tools:[]}],knowledge_base_ids:['knowledge','hidden-knowledge'],skill_ids:['skill','hidden-skill'],web_search_enabled:false,default_model_id:'dano-v2.1',default_reasoning_level:'max',revision:7};
afterEach(()=>vi.unstubAllGlobals());

describe('assistant configuration safety',()=>{
  it('preserves unavailable bindings and full nested grants when changing only a name',()=>{
    const baseline=assistantDraft(assistant,resources);const draft=assistantDraft(assistant,resources);draft.name='  新名称  ';
    expect(assistantChanges(baseline,draft)).toEqual({name:'新名称'});
    expect(draft.connections[1].connection_id).toBe('hidden-connection');expect(draft.knowledge_base_ids).toContain('hidden-knowledge');expect(draft.skill_ids).toContain('hidden-skill');expect(draft.access.user_ids).toContain('hidden-user');
    draft.connections[0].allowed_tools.push('extra');draft.access.user_ids.push('extra-user');
    expect(baseline.connections[0].allowed_tools).not.toContain('extra');expect(assistant.connections[0].allowed_tools).not.toContain('extra');expect(baseline.access.user_ids).toEqual(['hidden-user']);
  });
  it('sends explicit clears and entire nested bindings without removing unseen tools',()=>{
    const baseline=assistantDraft(assistant,resources);const draft=assistantDraft(assistant,resources);
    draft.connections=toggleBindingTool(draft.connections,'connection','query',false);draft.default_model_id=null;draft.default_reasoning_level=null;draft.skill_ids=[];
    const changes=assistantChanges(baseline,draft);
    expect(changes).toEqual({connections:[{connection_id:'connection',allowed_tools:['missing-tool'],auto_approved_tools:['missing-tool']},assistant.connections[1]],default_model_id:null,default_reasoning_level:null,skill_ids:[]});
    expect(toggleBindingTool(draft.connections,'connection','missing-tool',false)[0]).toEqual({connection_id:'connection',allowed_tools:[],auto_approved_tools:[]});
  });
  it('submits the baseline revision with only changed top-level fields and keeps conflicts explicit without retry',async()=>{
    const baseline=assistantDraft(assistant,resources);const draft={...baseline,web_search_enabled:true};
    const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify({error:{code:'ASSISTANT_CONFLICT',message:'助手配置已更新'}}),{status:409}));vi.stubGlobal('fetch',fetch);
    await expect(saveAssistantConfiguration(assistant.id,7,baseline,draft)).rejects.toMatchObject({status:409,code:'ASSISTANT_CONFLICT'});
    expect(fetch).toHaveBeenCalledTimes(1);const [url,request]=fetch.mock.calls[0];expect(url).toBe('/api/v1/assistants/assistant');expect(request.method).toBe('PATCH');expect(JSON.parse(request.body)).toEqual({expected_revision:7,web_search_enabled:true});expect(draft.web_search_enabled).toBe(true);expect(baseline.web_search_enabled).toBe(false);
  });
  it('renders all capabilities inline, preserves missing entries, and disables the complete form for a read-only assistant',()=>{
    const html=renderToStaticMarkup(<AssistantConfiguration value={{...assistant,can_edit:false}} resources={resources} admin inline onSaved={()=>{}} onDirtyChange={()=>{}} onSavingChange={()=>{}} onReload={()=>{}}/>);
    for(const label of ['数据连接与工具','知识库','Skills','网页搜索'])expect(html).toContain(`aria-label="${label}"`);
    expect(html).toContain('<fieldset class="plain-fieldset" disabled="">');expect(html).not.toContain('保存助手');expect(html).toContain('无法访问的数据连接');expect(html).toContain('无法访问的知识库');expect(html).toContain('无法访问的 Skill');expect(html).toContain('未发现的工具：missing-tool');expect(html).toContain('<details class="binding-permissions">');expect(html).not.toContain('<details class="binding-permissions" open');
  });
  it('keeps bound disabled resources visible while hiding unrelated disabled choices',()=>{
    const data={...resources,connections:[{...connection,enabled:false},{...connection,id:'unused',name:'无关停用连接',enabled:false}],knowledge:[{...knowledge,enabled:false},{...knowledge,id:'unused',name:'无关停用知识库',enabled:false}],skills:[{...skill,enabled:false},{...skill,id:'unused',name:'无关停用Skill',enabled:false}]};
    const html=renderToStaticMarkup(<AssistantConfiguration value={assistant} resources={data} admin={false} onSaved={()=>{}} onDirtyChange={()=>{}} onSavingChange={()=>{}} onReload={()=>{}}/>);
    expect(html).toContain('生产数据');expect(html).toContain('公司资料');expect(html).toContain('quality-review');expect(html).not.toContain('无关停用');expect(html).toContain('原绑定保留');
  });
  it('loads current authorized resources with the same cancellation signal and does not request member data for non-admins',async()=>{
    const fetch=vi.fn().mockImplementation(()=>Promise.resolve(new Response('[]',{status:200})));vi.stubGlobal('fetch',fetch);const controller=new AbortController();await loadAssistantResources(false,controller.signal);
    expect(fetch.mock.calls.map(([url])=>url).sort()).toEqual(['/api/v1/connections','/api/v1/knowledge-bases','/api/v1/models','/api/v1/skills']);expect(fetch.mock.calls.every(([,init])=>init.signal===controller.signal)).toBe(true);
  });
  it('uses an assistant model and its supported default level, while null defaults inherit the workspace',()=>{
    const config:AppConfig={brand_name:'工作台',auth_types:[],model_label:'',upload_max_bytes:1,default_model_id:'dano-v2.1',default_reasoning_level:'light'};
    expect(assistantModelSelection(models,config,assistant)).toEqual({model_id:'dano-v2.1',reasoning_level:'max'});
    expect(assistantModelSelection(models,config,{...assistant,default_reasoning_level:null})).toEqual({model_id:'dano-v2.1',reasoning_level:'high'});
    expect(assistantModelSelection(models,config,{...assistant,default_model_id:'glm-5.3',default_reasoning_level:null})).toEqual({model_id:'glm-5.3',reasoning_level:'default'});
    expect(assistantModelSelection(models,config,{...assistant,default_model_id:null,default_reasoning_level:null})).toEqual({model_id:'dano-v2.1',reasoning_level:'light'});
  });
});
