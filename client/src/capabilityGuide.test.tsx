import { isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { AssistantCapabilityGuide, ExampleQuestions, ToolCapability, assistantExamples, displayToolDescription, toolApprovalSummary, toolExample, toolParameters, usableAssistantTools } from './CapabilityGuide';
import { ConnectionOnboardingCard } from './ConnectionsPage';
import type { Assistant, AssistantBinding, Connection, KnowledgeBase, MCPTool } from './types';

const tool: MCPTool = { name:'shipment_summary',description:'查询演示业务日期2026-11-14向哪些客户发货及数量，按客户汇总；数量单位为件。',annotations:{readOnlyHint:true},definition_digest:'digest',read_only_confirmation_eligible:true,inputSchema:{type:'object',properties:{dataset_rows:{type:'integer',enum:[10000,100000],default:10000},region:{type:'string',enum:['全部','华北'],default:'全部'},customer_id:{type:'string',description:'实际客户编号'}},required:['customer_id']} };
const connection: Connection = {id:'c1',name:'订单演示服务',url:'https://example.test/mcp',transport:'http',auth_type:'none',credential_present:false,enabled:true,status:'connected',tools:[tool],last_error:null,last_tested_at:null,revision:2};
const binding: AssistantBinding = {connection_id:'c1',allowed_tools:['shipment_summary'],auto_approved_tools:[]};
const assistant: Assistant = {id:'a1',name:'业务助手',description:'',instructions:'',enabled:true,access:{all_members:true,user_ids:[]},connections:[binding],knowledge_base_ids:['k1'],revision:1};
const kb: KnowledgeBase = {id:'k1',name:'设备说明资料',description:'',enabled:true,default_for_new_assistants:false,document_count:3};

describe('known demo descriptions are translated for display only',()=>{
  it.each([
    ['Search synthetic Chinese company rules; always returns source IDs and labels data as demo.', '检索中文企业制度示例资料'],
    ['Persist one synthetic ticket. This WRITE requires user approval; request_key prevents duplicate creation.', '此操作需要用户审批'],
    ['Read persisted synthetic tickets, optionally by exact request_key. No business record is changed.', '支持精确筛选'],
    ['Query line/batch quality counts and calculate the weighted yield for an inclusive date range. Returns database/table/fields/filter/time/aggregation provenance.', '按检验数量加权计算合格率'],
    ['Acceptance-only bounded read delay, useful for checking cancellation. Does not create or modify records.', '用于检查取消操作'],
  ])('translates the complete known description: %s',(original,expected)=>{
    const translated=displayToolDescription(original);expect(translated).toContain(expected);expect(translated).not.toContain('测试版');expect(translated).not.toContain('合成');
  });
  it('leaves unknown or changed descriptions exactly as received',()=>{
    for(const description of ['Read live tickets from the customer system.', 'Search synthetic Chinese company rules; always returns source IDs and labels data as demo. Extra conditions apply.', '', 'toString']) expect(displayToolDescription(description)).toBe(description);
  });
  it('uses the same translation in the connection summary and tool purpose without changing the original definition',()=>{
    const known={...tool,name:'create_ticket',description:'Persist one synthetic ticket. This WRITE requires user approval; request_key prevents duplicate creation.',annotations:{readOnlyHint:false,destructiveHint:false}};
    const connected={...connection,tools:[known]};const before=JSON.stringify(known);
    const detail=renderToStaticMarkup(<ToolCapability connection={connected} tool={known} binding={{...binding,allowed_tools:['create_ticket']}}/>);
    const card=renderToStaticMarkup(<ConnectionOnboardingCard item={connected} assistants={[]} admin={false} onTest={()=>{}} onTools={()=>{}} onEdit={()=>{}} onDelete={()=>{}} onConfigureAssistant={()=>{}}/>);
    expect(detail).toContain('创建并保存一条示例工单');expect(detail).toContain('此操作需要用户审批');expect(detail).toContain('调用前需要审批');expect(card).toContain('创建并保存一条示例工单');
    expect(toolExample(connected,known).question).toContain('先不要调用或执行');expect(JSON.stringify(known)).toBe(before);
    const custom={...known,description:'Read live tickets from the customer system.'};
    const unknown=renderToStaticMarkup(<ToolCapability connection={connected} tool={custom}/>);
    expect(unknown).toContain(custom.description);expect(unknown).not.toContain('创建并保存一条示例工单');
  });
});

describe('capabilities follow saved permissions and evidence',()=>{
  it('includes only enabled, connected and explicitly granted tools and deduplicates bindings',()=>{
    expect(usableAssistantTools({...assistant,connections:[binding,binding]},[connection])).toHaveLength(1);
    for(const changed of [{enabled:false},{status:'untested' as const},{tools:[]}]) expect(usableAssistantTools(assistant,[{...connection,...changed}])).toEqual([]);
    expect(usableAssistantTools({...assistant,connections:[{...binding,allowed_tools:[]}]},[connection])).toEqual([]);
  });
  it('keeps remote hints, host confirmation and approval as different claims',()=>{
    expect(toolApprovalSummary(connection,tool,binding)).toEqual({readOnly:'远端声明：只读（仅提示）',approval:'调用前需要审批'});
    expect(toolApprovalSummary(connection,tool,{...binding,auto_approved_tools:[tool.name]}).approval).toContain('免确认');
    const confirmed={...binding,read_only_confirmations:{[tool.name]:{connection_revision:2,definition_digest:'digest',basis:'核过只读账号'}}};
    expect(toolApprovalSummary(connection,tool,confirmed).readOnly).toBe('当前定义已确认只读');
    expect(toolApprovalSummary({...connection,revision:3},tool,confirmed).readOnly).toBe('远端声明：只读（仅提示）');
    expect(toolApprovalSummary(connection,tool).approval).toBe('审批方式由所选助手决定');
  });
  it('explains actual required fields, choices, defaults and declared limits',()=>{
    const params=toolParameters(tool);expect(params.find(p=>p.key==='customer_id')).toMatchObject({label:'客户编号',required:true,type:'文本',description:'实际客户编号'});
    expect(params.find(p=>p.key==='dataset_rows')).toMatchObject({choices:[10000,100000],defaultValue:10000});
    const html=renderToStaticMarkup(<ToolCapability connection={connection} tool={tool} binding={binding}/>);
    expect(html).toContain('可选值：');expect(html).toContain('客户编号');expect(html).toContain('调用前需要审批');expect(html).toContain('完整参数定义');
  });
  it('does not expose unrecognized connection metadata as data scope',()=>{
    const html=renderToStaticMarkup(<ToolCapability connection={{...connection,source_metadata:{database:'订单库',api_key:'do-not-display'}}} tool={tool}/>);
    expect(html).toContain('订单库');expect(html).not.toContain('do-not-display');
  });
});

describe('questions come from declared capabilities, never a tool name alone',()=>{
  it('uses the date and purpose in the description plus declared defaults, and asks for missing required fields',()=>{
    const example=toolExample(connection,tool,binding);expect(example.title).toBe('按客户查看发货数量');expect(example.question).toContain('10,000条示例数据');expect(example.question).toContain('2026-11-14');expect(example.question).toContain('区域“全部”');expect(example.question).toContain('缺少必要条件时请先问我');
  });
  it('does not select enum values without defaults or invent dates',()=>{
    const changed={...tool,description:'查询客户发货数量',inputSchema:{properties:{region:{enum:['全部','华北']}}}};
    const example=toolExample(connection,changed,binding);expect(example.question).not.toContain('华北');expect(example.question).not.toContain('2026');expect(example.question).not.toContain('“全部”');
  });
  it('does not seed a wider default scope when the assistant has additional constraints',()=>{
    const restricted={...binding,argument_constraints:{[tool.name]:{type:'object',properties:{region:{const:'华北'}}}}};
    const example=toolExample(connection,tool,restricted);expect(example.question).not.toContain('区域“全部”');expect(example.question).not.toContain('10,000');expect(example.question).toContain('缺少必要条件');
  });
  it('never promises querying, diagnosis or orders based only on a suggestive name',()=>{
    const unknown={...tool,name:'shipment_diagnosis_magic',description:''};
    const example=toolExample(connection,unknown);expect(example.title).toBe('了解这个工具');expect(example.question).toContain('先不要调用或执行');expect(example.question).not.toContain('按客户');
    expect(toolExample(connection,{...tool,description:'为上传图像生成预览'}).question).toContain('先不要调用该工具');
  });
  it('offers preparation, not execution, for unknown, destructive or dedicated failure tools',()=>{
    for(const changed of [{annotations:{}},{annotations:{readOnlyHint:true,destructiveHint:true}},{description:'超限验收专用：故意返回201行'}]) expect(toolExample(connection,{...tool,...changed}).question).toContain('先不要调用或执行');
    expect(assistantExamples(assistant,[{...connection,tools:[{...tool,description:'取消验收专用，等待20秒'}]}],[])).toEqual([]);
  });
  it('uses only bound, enabled knowledge with documents and does not invent its contents',()=>{
    const examples=assistantExamples({...assistant,connections:[]},[],[kb]);expect(examples).toHaveLength(1);expect(examples[0].question).toContain('设备说明资料');expect(examples[0].question).not.toContain('4 个工作小时');
    expect(assistantExamples(assistant,[],[{...kb,id:'unbound'}])).toEqual([]);
    expect(assistantExamples(assistant,[],[{...kb,enabled:false}])).toEqual([]);
    expect(assistantExamples(assistant,[],[{...kb,document_count:0}])).toEqual([]);
  });
  it('dispatches the exact selected question through the existing callback only',()=>{
    const choose=vi.fn();const fetch=vi.fn();vi.stubGlobal('fetch',fetch);
    try {
      const example=toolExample(connection,tool,binding);const tree=ExampleQuestions({examples:[example],onChoose:choose});
      const buttons:ReactElement<Record<string,unknown>>[]=[];
      const visit=(node:ReactNode):void=>{if(Array.isArray(node)){node.forEach(visit);return;}if(!isValidElement<Record<string,unknown>>(node))return;if(node.type==='button')buttons.push(node);visit(node.props.children as ReactNode);};
      visit(tree);expect(buttons).toHaveLength(1);(buttons[0].props.onClick as ()=>void)();expect(choose).toHaveBeenCalledWith(example.question);expect(fetch).not.toHaveBeenCalled();
    } finally { vi.unstubAllGlobals(); }
  });
  it('disables samples for a stopped assistant, without mislabelling data as connected',()=>{
    const html=renderToStaticMarkup(<AssistantCapabilityGuide assistant={{...assistant,enabled:false}} connections={[connection]} knowledge={[kb]} onChoose={()=>{}}/>);
    expect(html).toContain('disabled=""');expect(html).toContain('点击填入新会话');expect(html).not.toContain('业务验证通过');
  });
});
