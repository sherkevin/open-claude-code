import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import AssistantConfiguration, { assistantDraft, saveAssistantConfiguration, setBindingReadOnlyConfirmation, toggleBindingTool, toggleDiscoveredBindingTools, type AssistantResources } from './AssistantConfiguration';
import { ApiError } from './api';
import { constraintDraftKey } from './ToolArgumentConstraints';
import ToolReadOnlyConfirmation, { makeReadOnlyConfirmation, readOnlyConfirmationState, remoteReadOnlyLabel, RemoteToolDeclaration } from './ToolReadOnlyConfirmation';
import type { Assistant, Connection, MCPTool, ReadOnlyConfirmation } from './types';

const tool:MCPTool={name:'query',description:'按条件读取',inputSchema:{type:'object',properties:{limit:{type:'integer'}}},definition_digest:'definition-v1',read_only_confirmation_eligible:true};
const connection:Connection={id:'source',name:'业务数据',transport:'http',url:'https://mcp.example',auth_type:'none',credential_present:false,enabled:true,status:'connected',tools:[tool],last_error:null,last_tested_at:null,revision:4,scope:'team'};
const confirmation:ReadOnlyConfirmation={connection_revision:4,definition_digest:'definition-v1',basis:'已核查接口说明及专用只读账号权限。'};
const assistant:Assistant={id:'assistant',name:'业务助手',description:'',instructions:'',can_edit:true,scope:'private',enabled:true,access:{all_members:true,user_ids:[]},connections:[{connection_id:'source',allowed_tools:['query','missing'],auto_approved_tools:[],read_only_confirmations:{query:confirmation,missing:{...confirmation,definition_digest:'missing-v1'}}}],revision:7};
const resources:AssistantResources={connections:[connection],knowledge:[],skills:[],users:[],models:[]};
const noop=()=>{};
afterEach(()=>vi.unstubAllGlobals());

describe('host read-only confirmations',()=>{
  it('does not treat a remote hint or automatic approval as host confirmation',()=>{
    const hinted={...tool,annotations:{readOnlyHint:true}};
    expect(remoteReadOnlyLabel(hinted)).toBe('远端声明：只读（仅提示）');
    expect(readOnlyConfirmationState(connection,hinted)).toBe('unconfirmed');
    const html=renderToStaticMarkup(<AssistantConfiguration value={{...assistant,connections:[{connection_id:'source',allowed_tools:['query'],auto_approved_tools:['query']}]}} resources={{...resources,connections:[{...connection,tools:[hinted]}]}} admin={false} onSaved={noop} onDirtyChange={noop} onSavingChange={noop} onReload={noop}/>);
    expect(html).toContain('工作流只读确认 · 未确认');
    expect(html).toContain('远端声明：只读（仅提示）');
    expect(html).toContain('免确认执行只影响审批，不代表只读');
    expect(html).not.toContain('当前定义已由宿主确认');
  });
  it('requires an explicit basis and a verified current definition; a missing hint alone neither grants nor prevents confirmation',()=>{
    expect(makeReadOnlyConfirmation(connection,tool,'  核查了接口和账号只读权限  ')).toEqual({...confirmation,basis:'核查了接口和账号只读权限'});
    expect(()=>makeReadOnlyConfirmation(connection,tool,' ')).toThrow('1–500');
    expect(()=>makeReadOnlyConfirmation(connection,tool,'中'.repeat(501))).toThrow('1–500');
    expect(()=>makeReadOnlyConfirmation(connection,tool,'😀'.repeat(500))).not.toThrow();
    expect(()=>makeReadOnlyConfirmation(connection,{...tool,read_only_confirmation_eligible:undefined},'已检查')).toThrow('尚不能确认');
    expect(()=>makeReadOnlyConfirmation(connection,{...tool,definition_digest:null},'已检查')).toThrow('尚不能确认');
  });
  it('refuses remote non-read-only/destructive tools and unavailable connections even if a stale eligible flag is true',()=>{
    for(const annotations of [{readOnlyHint:false},{readOnlyHint:true,destructiveHint:true}])expect(()=>makeReadOnlyConfirmation(connection,{...tool,annotations},'已检查')).toThrow('不能确认为只读');
    for(const item of [{...connection,enabled:false},{...connection,status:'failed' as const},{...connection,status:'untested' as const}])expect(()=>makeReadOnlyConfirmation(item,tool,'已检查')).toThrow('尚未就绪');
  });
  it('marks confirmation stale after a definition, connection, eligibility, or availability change',()=>{
    expect(readOnlyConfirmationState(connection,tool,confirmation)).toBe('current');
    expect(readOnlyConfirmationState({...connection,revision:5},tool,confirmation)).toBe('stale');
    expect(readOnlyConfirmationState(connection,{...tool,definition_digest:'definition-v2'},confirmation)).toBe('stale');
    expect(readOnlyConfirmationState(connection,{...tool,read_only_confirmation_eligible:false},confirmation)).toBe('stale');
    expect(readOnlyConfirmationState({...connection,status:'failed'},tool,confirmation)).toBe('stale');
    expect(readOnlyConfirmationState(connection,tool,{...confirmation,basis:''})).toBe('stale');
  });
  it('applies confirmation only to the selected authorized assistant binding without granting approval or other tool access',()=>{
    const original=[{connection_id:'source',allowed_tools:['query'],auto_approved_tools:[]}];
    const next=setBindingReadOnlyConfirmation(original,'source','query',confirmation);
    expect(next[0].allowed_tools).toEqual(['query']);expect(next[0].auto_approved_tools).toEqual([]);
    expect(next[0].read_only_confirmations).toEqual({query:confirmation});
    expect(setBindingReadOnlyConfirmation(original,'source','unknown',confirmation)).toEqual(original);
    expect(original[0]).not.toHaveProperty('read_only_confirmations');
  });
  it('removes confirmations on explicit single or bulk revocation and preserves undiscovered entries',()=>{
    const next=toggleDiscoveredBindingTools(assistant.connections,'source',['query'],false);
    expect(next[0].allowed_tools).toEqual(['missing']);
    expect(next[0].read_only_confirmations).toEqual({missing:assistant.connections[0].read_only_confirmations!.missing});
    expect(toggleBindingTool(next,'source','missing',false)[0].read_only_confirmations).toEqual({});
    expect(toggleBindingTool(assistant.connections,'source','query',true,true)[0].read_only_confirmations).toEqual(assistant.connections[0].read_only_confirmations);
    const removed=setBindingReadOnlyConfirmation(assistant.connections,'source','query',null)[0];
    expect(removed.allowed_tools).toEqual(['query','missing']);expect(removed.auto_approved_tools).toEqual([]);expect(removed.read_only_confirmations).not.toHaveProperty('query');
  });
  it('keeps applied confirmation in independent drafts and includes the existing optimistic lock on save',async()=>{
    const baseline=assistantDraft(assistant,resources);const draft=assistantDraft(assistant,resources);
    draft.connections[0].read_only_confirmations!.query.basis='检查更新后的说明';
    expect(baseline.connections[0].read_only_confirmations!.query.basis).toBe(confirmation.basis);
    const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify({...assistant,revision:8}),{status:200}));vi.stubGlobal('fetch',fetch);
    await saveAssistantConfiguration('assistant',7,baseline,draft);
    const body=JSON.parse(fetch.mock.calls[0][1].body);
    expect(body.expected_revision).toBe(7);expect(body.connections[0].read_only_confirmations.query.basis).toBe('检查更新后的说明');
    expect(body.connections[0].read_only_confirmations.missing.definition_digest).toBe('missing-v1');
  });
  it('blocks saving a valid old confirmation while new basis text is unapplied, including a blank edit',async()=>{
    const fetch=vi.fn();vi.stubGlobal('fetch',fetch);
    const draft=assistantDraft(assistant,resources);
    for(const text of ['新依据',''])await expect(saveAssistantConfiguration('assistant',7,draft,draft,{}, {[constraintDraftKey('source','query')]:text})).rejects.toThrow('未应用的只读确认依据');
    expect(fetch).not.toHaveBeenCalled();
  });
  it('preserves a server rejection instead of treating a stale confirmation as saved',async()=>{
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(JSON.stringify({error:{code:'READ_ONLY_CONFIRMATION_STALE',message:'工具定义已更新'}}),{status:409})));
    const draft=assistantDraft(assistant,resources);
    await expect(saveAssistantConfiguration('assistant',7,draft,draft)).rejects.toMatchObject({code:'READ_ONLY_CONFIRMATION_STALE',status:409});
    expect(new ApiError('工具定义已更新',409,'READ_ONLY_CONFIRMATION_STALE')).toBeInstanceOf(Error);
  });
  it('clearly separates saved, pending, and invalid states, and does not offer any mutation to read-only viewers',()=>{
    const base={connection,tool,value:confirmation,savedValue:confirmation,disabled:false,onDraftChange:noop,onApply:noop,onDiscard:noop,onRemove:noop};
    const saved=renderToStaticMarkup(<ToolReadOnlyConfirmation {...base}/>);
    expect(saved).toContain('当前定义已由宿主确认');expect(saved).toContain('服务端会重新核验');
    const staged=renderToStaticMarkup(<ToolReadOnlyConfirmation {...base} savedValue={undefined}/>);
    expect(staged).toContain('当前定义已确认，待保存');
    const stale=renderToStaticMarkup(<ToolReadOnlyConfirmation {...base} connection={{...connection,revision:5}}/>);
    expect(stale).toContain('定义或连接状态已变更，需重新核查');expect(stale).toContain('原确认依据（当前不生效）');expect(stale).not.toContain('当前定义已由宿主确认');
    const readOnly=renderToStaticMarkup(<ToolReadOnlyConfirmation {...base} draftText="已核查" disabled/>);
    expect(readOnly).toContain('<textarea rows="3" disabled=""');expect((readOnly.match(/<button[^>]*disabled=""/g)||[])).toHaveLength(3);
    const unknown=renderToStaticMarkup(<RemoteToolDeclaration tool={tool}/>);
    expect(unknown).toContain('远端未声明是否只读');expect(unknown).not.toContain('确认当前定义到草稿');
  });
});
