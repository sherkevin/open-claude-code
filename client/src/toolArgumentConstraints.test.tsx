import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import AssistantConfiguration, { assistantChanges, assistantDraft, retainPendingConstraintDrafts, saveAssistantConfiguration, setBindingArgumentConstraint, toggleBindingTool, toggleDiscoveredBindingTools, type AssistantResources } from './AssistantConfiguration';
import ToolArgumentConstraints, { assertNoPendingConstraintDrafts, constraintDraftKey, constraintText, MAX_CONSTRAINT_BYTES, parseArgumentConstraint, updateConstraintDraft } from './ToolArgumentConstraints';
import type { Assistant, AssistantBinding } from './types';

const limit={type:'object',properties:{count:{type:'integer',maximum:20}}};
const hiddenLimit={type:'object',properties:{state:{const:'pending'}}};
const bindings:AssistantBinding[]=[{connection_id:'connection',allowed_tools:['query','unavailable'],auto_approved_tools:['query'],argument_constraints:{query:limit,unavailable:hiddenLimit}}];
const assistant:Assistant={id:'assistant',name:'业务助手',description:'',instructions:'',can_edit:true,scope:'private',enabled:true,access:{all_members:true,user_ids:[]},connections:bindings,knowledge_base_ids:[],skill_ids:[],web_search_enabled:false,default_model_id:null,default_reasoning_level:null,revision:7};
const resources:AssistantResources={connections:[{id:'connection',name:'业务数据',transport:'http',url:'https://mcp.example',auth_type:'none',credential_present:false,enabled:true,status:'connected',tools:[{name:'query',description:'读取数据',inputSchema:{type:'object',properties:{count:{type:'integer'},trace_id:{type:'string'}}}},{name:'not-allowed',description:'未授权',inputSchema:{type:'object'}}],last_error:null,last_tested_at:null,revision:1,scope:'team'}],knowledge:[],skills:[],users:[],models:[]};
afterEach(()=>vi.unstubAllGlobals());

describe('tool request constraints',()=>{
  it('accepts an object schema without injecting customer parameters and rejects ambiguous deletion or malformed drafts',()=>{
    expect(parseArgumentConstraint(JSON.stringify(limit))).toEqual(limit);
    for(const text of ['', '  ', '{"type":', '[]', 'false', '"text"', '{}', '{"type":"string"}'])expect(()=>parseArgumentConstraint(text)).toThrow();
    expect(()=>parseArgumentConstraint(JSON.stringify({type:'object',description:'中'.repeat(MAX_CONSTRAINT_BYTES/2)}))).toThrow('32,000 字节');
  });
  it('accepts exactly 32,000 UTF-8 bytes and rejects 32,001 bytes rather than counting characters',()=>{
    const encoder=new TextEncoder();
    const empty=JSON.stringify({type:'object',description:''});
    const remaining=32_000-encoder.encode(empty).byteLength;
    const text=JSON.stringify({type:'object',description:'中'.repeat(Math.floor(remaining/3))+'a'.repeat(remaining%3)});
    const tooLong=text.replace('"description":"','"description":"a');
    expect(MAX_CONSTRAINT_BYTES).toBe(32_000);
    expect(text.length).toBeLessThan(32_000);
    expect(encoder.encode(text).byteLength).toBe(32_000);
    expect(encoder.encode(tooLong).byteLength).toBe(32_001);
    expect(()=>parseArgumentConstraint(text)).not.toThrow();
    expect(()=>parseArgumentConstraint(tooLong)).toThrow('32,000 字节');
  });
  it('retains a pending invalid edit and blocks all saving rather than silently reusing the old applied restriction',async()=>{
    const fetch=vi.fn();vi.stubGlobal('fetch',fetch);
    const key=constraintDraftKey('connection','query');
    const pending=updateConstraintDraft({},key,'{"type":',limit);
    const baseline=assistantDraft(assistant,resources);
    await expect(saveAssistantConfiguration(assistant.id,7,baseline,baseline,pending)).rejects.toThrow('未应用');
    expect(fetch).not.toHaveBeenCalled();
    expect(pending[key]).toBe('{"type":');
    expect(()=>assertNoPendingConstraintDrafts(pending)).toThrow();
    const restored=updateConstraintDraft(pending,key,constraintText(limit),limit);
    expect(()=>assertNoPendingConstraintDrafts(restored)).not.toThrow();
    expect(assistant.connections[0].argument_constraints?.query).toEqual(limit);
  });
  it('requires valid but unapplied edits to be applied or discarded before the main save as well',async()=>{
    const fetch=vi.fn();vi.stubGlobal('fetch',fetch);
    const baseline=assistantDraft(assistant,resources);
    const pending=updateConstraintDraft({},constraintDraftKey('connection','query'),JSON.stringify({type:'object',properties:{count:{maximum:10}}}),limit);
    await expect(saveAssistantConfiguration(assistant.id,7,baseline,baseline,pending)).rejects.toThrow('未应用');
    expect(fetch).not.toHaveBeenCalled();
  });
  it('deep copies nested restrictions and preserves unseen restrictions and the optimistic lock when applying an edit',async()=>{
    const baseline=assistantDraft(assistant,resources);const draft=assistantDraft(assistant,resources);
    ((draft.connections[0].argument_constraints!.query.properties as Record<string,Record<string,unknown>>).count).maximum=10;
    expect((baseline.connections[0].argument_constraints!.query.properties as Record<string,Record<string,unknown>>).count.maximum).toBe(20);
    expect((limit.properties.count).maximum).toBe(20);
    const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify({...assistant,revision:8}),{status:200}));vi.stubGlobal('fetch',fetch);
    await saveAssistantConfiguration(assistant.id,7,baseline,draft);
    const body=JSON.parse(fetch.mock.calls[0][1].body);
    expect(body.expected_revision).toBe(7);expect(body.connections[0].argument_constraints.query.properties.count.maximum).toBe(10);
    expect(body.connections[0].argument_constraints.unavailable).toEqual(hiddenLimit);
  });
  it('removes only explicitly revoked tools and their constraints, including bulk changes, while preserving undiscovered tools',()=>{
    const bulk=toggleDiscoveredBindingTools(bindings,'connection',['query','not-allowed'],false)[0];
    expect(bulk.allowed_tools).toEqual(['unavailable']);expect(bulk.auto_approved_tools).toEqual([]);expect(bulk.argument_constraints).toEqual({unavailable:hiddenLimit});
    const cleared=toggleBindingTool([bulk],'connection','unavailable',false)[0];
    expect(cleared.argument_constraints).toEqual({});expect(cleared.allowed_tools).toEqual([]);
    expect(bindings[0].argument_constraints?.query).toEqual(limit);
    expect(toggleBindingTool(bindings,'connection','query',false,true)[0].argument_constraints).toEqual(bindings[0].argument_constraints);
  });
  it('explicitly clears a restriction without revoking its tool and will not apply a restriction to an unauthorized tool',()=>{
    const next=setBindingArgumentConstraint(bindings,'connection','query',null);
    expect(next[0].argument_constraints).toEqual({unavailable:hiddenLimit});expect(next[0].allowed_tools).toEqual(['query','unavailable']);
    expect(setBindingArgumentConstraint(bindings,'connection','not-allowed',limit)).toEqual(bindings);
    const baseline=assistantDraft(assistant,resources);const draft={...baseline,connections:next};
    expect(assistantChanges(baseline,draft).connections?.[0].argument_constraints).toEqual({unavailable:hiddenLimit});
  });
  it('clears only pending drafts belonging to explicitly removed bindings',()=>{
    const query=constraintDraftKey('connection','query');const missing=constraintDraftKey('connection','unavailable');
    const pending={[query]:'invalid',[missing]:'still pending'};
    expect(retainPendingConstraintDrafts(pending,toggleBindingTool(bindings,'connection','query',false))).toEqual({[missing]:'still pending'});
    expect(retainPendingConstraintDrafts(pending,[])).toEqual({});
  });
  it('keeps the advanced editor folded, shows the actual discovered schema, and does not offer editors for unauthorized tools',()=>{
    const html=renderToStaticMarkup(<AssistantConfiguration value={assistant} resources={resources} admin={false} onSaved={()=>{}} onDirtyChange={()=>{}} onSavingChange={()=>{}} onReload={()=>{}}/>);
    expect(html).toContain('aria-label="query 请求限制"');expect(html).toContain('trace_id');expect(html).toContain('查看工具实际参数定义');
    expect(html).not.toContain('aria-label="not-allowed 请求限制"');expect(html).not.toContain('aria-label="unavailable 请求限制"');
    expect(html).toContain('未发现的工具：unavailable');expect(html).toContain('原请求限制仍保留');
    expect(html).not.toContain('class="resource-advanced" aria-label="query 请求限制" open');
  });
  it('disables all restriction changes for a read-only assistant and keeps invalid drafts visibly blocked',()=>{
    const html=renderToStaticMarkup(<ToolArgumentConstraints toolName="query" inputSchema={{type:'object'}} value={limit} draftText="{" disabled onDraftChange={()=>{}} onApply={()=>{}} onDiscard={()=>{}} onRemove={()=>{}}/>);
    expect(html).toContain('aria-invalid="true"');expect(html).toContain('JSON 格式不完整');expect(html).toContain('暂不能保存助手');expect(html).toContain('<textarea rows="7" disabled=""');
    expect((html.match(/<button[^>]*disabled=""/g)||[])).toHaveLength(3);
    const form=renderToStaticMarkup(<AssistantConfiguration value={{...assistant,can_edit:false}} resources={resources} admin={false} onSaved={()=>{}} onDirtyChange={()=>{}} onSavingChange={()=>{}} onReload={()=>{}}/>);
    expect(form).toContain('<fieldset class="plain-fieldset" disabled="">');expect(form).not.toContain('保存助手</button>');
  });
});
